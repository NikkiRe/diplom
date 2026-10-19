"""(в ВМ sim, root, окружение ROS) Помощник приёмки П3 пульта на двойнике (accept_p3.py зовёт его через limactl).

  twin_p3.py home                     робот — на старт мира lab (как набор «датчики работают»), safety_gate взведён
  twin_p3.py counters                 состояние и счётчики safety_gate (stop_now, estop_engaged, estop_released,
                                      release_denied) по одному safety_state
  twin_p3.py release [--watch S]      снять защёлку как человек (sudo neurobot estop release в псевдотерминале) с
                                      моментами по стенным часам ВМ: подсказка, ответ «release», выход команды;
                                      --watch — ещё и состояния safety_gate в графе до S с после выхода
  twin_p3.py sample --seconds S --interval I --out FILE
                                      ресурсы шлюза пульта раз в I с, S с, строки JSONL в FILE: PSS, USS, RSS процессов
                                      юнита (/proc/PID/smaps_rollup), CPU и память его cgroup, байты сокетов порта
                                      пульта (ss -tin), клиенты SSE и буфер (/healthz)
  twin_p3.py k1 --label L --out DIR [--seed S] [--gestures N] [--negatives M] [--tail-s T] [--fit]
                                      короткий прогон K1: робот на старт, таймлайн gesturegen (класс 0 — «полный
                                      оборот» на месте: робот не уезжает от старта; --fit — жест целиком в кадре: без
                                      него сеть лаборатории относила круги к классу 1 и робот ехал вперёд, к стене;
                                      расписание одно для всех прогонов),
                                      наблюдатель жестов (neurobot/sim/gestures/observer.py) → K1 = t0 → первая
                                      ненулевая /cmd_vel по metrics.latency (без ранних, RTF ≥ 0,95); итог — DIR/k1.json
DDS и темы — из env-файла пакета (/etc/neurobot/neurobot.env). Числа — стенд в общей ВМ, ориентир.
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, "/opt/neurobot/lib")
from neurobot_deploy import config  # noqa: E402

import p3lib  # noqa: E402

CFG = config.load()
UNIT = "neurobot-ui"
TWIN_SRC = "/var/lib/neurobot-twin/src"          # снимок проекта, из которого работает двойник (setup_twin.sh)
WORLD_SDF = "/var/lib/neurobot-twin/run/world.sdf"
START = (-1.0, 1.0, 0.01, -math.pi / 2)           # мир lab: как sensors.START — на юг, впереди 1,9 м свободно
WALL_REGEX = "wall|table|chair|person|box"        # как neurobot/sim/runs/stacks.py
FOOTPRINT_R = 0.195


def _props(unit: str, names: str) -> dict:
    out = subprocess.run(["systemctl", "show", unit, "-p", names], capture_output=True, text=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _health(port: str) -> dict:
    try:
        c = http.client.HTTPConnection("127.0.0.1", int(port), timeout=2)
        c.request("GET", "/healthz", headers={"Host": "127.0.0.1:" + port})
        doc = json.loads(c.getresponse().read())
        c.close()
        return doc
    except (OSError, ValueError):
        return {}


def sample_once(port: str) -> dict:
    """Одна выборка ресурсов шлюза: сначала сокеты (до своего запроса /healthz — он в счёт трафика не попадает)."""
    t, m = time.time(), time.monotonic()
    ss = subprocess.run(["ss", "-tinH", "state", "established", "( sport = :%s )" % port], capture_output=True,
                        text=True).stdout
    p = _props(UNIT, "MainPID,ControlGroup,NRestarts,MemoryCurrent,MemoryPeak,ActiveState")
    row = {"t": round(t, 3), "m": round(m, 3), "active": p.get("ActiveState"), "socks": p3lib.ss_sockets(ss),
           "nrestarts": int(p.get("NRestarts") or 0)}
    cg = p.get("ControlGroup") or ""
    if row["active"] == "active" and cg:
        base = "/sys/fs/cgroup" + cg
        pids = [x for x in _read(base + "/cgroup.procs").split() if x]
        mem = [p3lib.smaps_rollup(_read("/proc/%s/smaps_rollup" % pid)) for pid in pids]
        stat = dict(line.split() for line in _read(base + "/memory.stat").splitlines() if len(line.split()) == 2)
        cpu = dict(line.split() for line in _read(base + "/cpu.stat").splitlines() if len(line.split()) == 2)
        row.update(pids=len(pids), pss_kib=sum(x["pss"] for x in mem), uss_kib=sum(x["uss"] for x in mem),
                   rss_kib=sum(x["rss"] for x in mem), cpu_usec=int(cpu.get("usage_usec", 0)),
                   mem_current=int(p.get("MemoryCurrent") or 0) // 1024,
                   mem_peak=int(p["MemoryPeak"]) // 1024 if (p.get("MemoryPeak") or "").isdigit() else None,
                   anon_kib=int(stat.get("anon", 0)) // 1024, file_kib=int(stat.get("file", 0)) // 1024)
        # разбор CPU: сам процесс шлюза (utime + stime, все его потоки) и дождавшиеся дочерние (cutime + cstime:
        # systemctl show опросчика юнитов, doctor по кнопке) — в тиках ядра
        st = (_read("/proc/%s/stat" % p.get("MainPID")).rsplit(")", 1) + [""])[1].split()
        if len(st) > 14:
            row.update(main_ticks=int(st[11]) + int(st[12]), child_ticks=int(st[13]) + int(st[14]),
                       hz=os.sysconf("SC_CLK_TCK"))
        h = _health(port)
        row.update(clients=h.get("clients"), buffer_kib=h.get("buffer_kib"), buffer_s=h.get("buffer_s"))
    row["load1"] = float(_read("/proc/loadavg").split()[0] or 0)
    return row


def cmd_sample(a) -> int:
    port = CFG.get("NEUROBOT_UI_PORT") or "18120"
    end = time.monotonic() + a.seconds
    with open(a.out, "a", encoding="utf-8") as f:
        while True:
            t0 = time.monotonic()
            f.write(json.dumps(sample_once(port), ensure_ascii=False) + "\n")
            f.flush()
            if t0 + a.interval > end:
                break
            time.sleep(max(0.0, t0 + a.interval - time.monotonic()))
    return 0


def _twin():
    """Обвязка наборов двойника (neurobot/sim/suites/harness.py) под своим именем: в корне проекта есть пакет
    harness (статистика разбора жестов), и модуль наборов не должен его заслонять. rclpy и окружение DDS
    поднимаются при загрузке."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("suites_harness", os.path.join(ROOT, "neurobot", "sim", "suites",
                                                                                 "harness.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Twin("neurobot_ui_p3_%d" % os.getpid())


def cmd_home(a) -> int:
    tw = _twin()
    try:
        tw.relocate(*START)
        s = tw.last("state") or {}
        print(json.dumps({"ok": True, "state": s.get("state"), "reasons": (s.get("reasons") or [])[:2]},
                         ensure_ascii=False))
    finally:
        tw.close()
    return 0


def cmd_counters(a) -> int:
    tw = _twin()
    try:
        t0 = time.monotonic()
        tw.spin(3.0, until=lambda: bool(tw.last("state")) and tw.last("state")["t"] > t0)
        s = tw.last("state") or {}
        print(json.dumps({"state": s.get("state"), "reasons": (s.get("reasons") or [])[:2],
                          "counters": s.get("counters") or {}}, ensure_ascii=False))
    finally:
        tw.close()
    return 0


def cmd_release(a) -> int:
    """Как twin_helper.py release, но с моментами: t_prompt — CLI спросил слово, t_answer — человек ответил, t_exit —
    команда завершилась (стенные часы ВМ; ноутбук пересчитывает их в свои сдвигом по /api/meta). --watch S — ещё и
    состояния safety_gate в графе (каждое safety_state, свой узел) от начала снятия до S с после выхода команды:
    видно, был ли `unarmed` и сколько он длился, независимо от экрана."""
    tw = _twin() if a.watch > 0 else None
    off = time.time() - time.monotonic()
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("neurobot", ["neurobot", "estop", "release", "--reason", "приёмка П3 пульта: снял человек у робота"])
    n0 = len(tw.rec["state"]) if tw else 0
    text, times, end = b"", {}, time.monotonic() + 30
    while time.monotonic() < end:
        if tw:
            tw.spin(0.02)
        if not select.select([fd], [], [], 0.0 if tw else 0.05)[0]:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        text += chunk
        if "t_answer" not in times and "Введите".encode() in text:
            times["t_prompt"] = round(time.time(), 4)
            os.write(fd, b"release\n")
            times["t_answer"] = round(time.time(), 4)
    _, status = os.waitpid(pid, 0)
    times["t_exit"] = round(time.time(), 4)
    rc = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
    doc = dict(times, rc=rc, out=text.decode("utf-8", "replace")[-300:])
    if tw:
        try:
            tw.spin(a.watch)
            doc["graph"] = [{"t": round(r["t"] + off, 4), "state": r.get("state"),
                             "reason": ((r.get("reasons") or [""])[0] or "").split(":")[0]}
                            for r in tw.rec["state"][n0:]]
        finally:
            tw.close()
    print(json.dumps(doc, ensure_ascii=False))
    return rc


def cmd_k1(a) -> int:
    out = os.path.abspath(a.out)
    os.makedirs(os.path.join(out, "logs"), exist_ok=True)
    gest = os.path.dirname(CFG.get("NEUROBOT_GESTURES_TIMELINE"))
    port = CFG.get("NEUROBOT_UI_PORT") or "18120"
    ui = {"active": _props(UNIT, "ActiveState").get("ActiveState"), "clients_start": _health(port).get("clients")}
    tw = _twin()
    try:
        tw.systemctl("stop", "neurobot-perception")
        tw.relocate(*START)
    finally:
        tw.close()
    subprocess.run(["sudo", "-u", "neurobot", "env", "PYTHONPATH=" + TWIN_SRC, "PYTHONDONTWRITEBYTECODE=1", "python3",
                    "-m", "neurobot.sim.gestures.gesturegen", "gen", "--seed", str(a.seed), "--gestures",
                    str(a.gestures), "--negatives", str(a.negatives), "--classes", a.classes, "--shapes", a.shapes,
                    "--lead-in-s", "10", "--force", "--out-dir", gest] + (["--fit"] if a.fit else []),
                   check=True, stdout=subprocess.DEVNULL)
    anchor = os.path.join(gest, "anchor.json")
    if os.path.exists(anchor):
        os.remove(anchor)
    for name in ("timeline.jsonl", "truth.jsonl"):
        shutil.copy(os.path.join(gest, name), os.path.join(out, "schedule_" + name))
    # наблюдатель и разбор — из корня проекта (смонтирован в ВМ только для чтения; код neurobot/sim тот же, что в
    # снимке двойника, — сверено хешами): разбору нужен пакет harness, которого в снимке двойника нет
    env = dict(os.environ)
    env.update(CFG.ros_environ())
    env.update(PYTHONPATH=":".join(p for p in (ROOT, os.environ.get("PYTHONPATH", "")) if p),
               PYTHONDONTWRITEBYTECODE="1", ROS_LOG_DIR=os.path.join(out, "logs", "ros"))
    argv = ["python3", "-m", "neurobot.sim.gestures.observer", "--out", os.path.join(out, "events.jsonl"),
            "--truth", os.path.join(gest, "truth.jsonl"), "--anchor", anchor,
            "--summary", os.path.join(out, "summary.json"), "--per-gesture", os.path.join(out, "gestures.jsonl"),
            "--cmd-vel-type", "twist", "--world-sdf", WORLD_SDF, "--wall-regex", WALL_REGEX,
            "--footprint-radius", str(FOOTPRINT_R), "--extra-cmd", CFG.get("NEUROBOT_CMD_VEL_REQ"),
            "--extra-cmd", "/diff_cont/cmd_vel_unstamped"]
    with open(os.path.join(out, "logs", "observer.log"), "a") as logf:
        obs = subprocess.Popen(argv, env=env, stdout=logf, stderr=logf, cwd="/")
        time.sleep(3.0)
        if obs.poll() is not None:
            raise RuntimeError("наблюдатель жестов завершился сразу (код {}): {}".format(
                obs.returncode, _read(os.path.join(out, "logs", "observer.log"))[-400:]))
        wall0 = time.time()
        subprocess.run(["systemctl", "start", "neurobot-perception"], check=True)
        end = time.monotonic() + 180
        while not os.path.exists(anchor) and time.monotonic() < end:
            time.sleep(0.2)
        if not os.path.exists(anchor):
            obs.send_signal(signal.SIGINT)
            obs.wait(60)
            raise RuntimeError("perception не создал якорь таймлайна за 180 с")
        with open(anchor, encoding="utf-8") as f:
            anc = json.load(f)
        with open(os.path.join(gest, "truth.jsonl"), encoding="utf-8") as f:
            truth = [json.loads(x) for x in f.read().splitlines()[1:] if x.strip()]
        t_end = anc["t_mono"] + max(r["t_leave"] for r in truth) + a.tail_s
        loads = []
        while time.monotonic() < t_end:
            time.sleep(min(30.0, max(0.0, t_end - time.monotonic())))
            loads.append(float(_read("/proc/loadavg").split()[0] or 0))
            print("k1 %s: осталось %.0f с" % (a.label, max(0.0, t_end - time.monotonic())), file=sys.stderr,
                  flush=True)
        ui["clients_end"] = _health(port).get("clients")
        obs.send_signal(signal.SIGINT)
        rc = obs.wait(180)
    shutil.copy(anchor, os.path.join(out, "anchor.json"))
    sys.path.insert(0, ROOT)
    from neurobot.sim.gestures.analyze import quantile_ci
    from neurobot.sim.runs import metrics
    with open(os.path.join(out, "gestures.jsonl"), encoding="utf-8") as f:
        per = [json.loads(x) for x in f if x.strip()]
    with open(os.path.join(out, "summary.json"), encoding="utf-8") as f:
        summ = json.load(f)
    lat = metrics.latency(per, "cmd")
    vals = sorted(p["lat"]["cmd"] for p in per if p.get("kind") == "gesture" and "cmd" in (p.get("lat") or {})
                  and not p.get("early") and (p.get("rtf") is None or p["rtf"] >= 0.95))
    ci = quantile_ci(vals, 0.5) if vals else None
    doc = {"label": a.label, "ui": "on" if ui["active"] == "active" else "off", "ui_unit": ui,
           "seed": a.seed, "gestures": a.gestures, "negatives": a.negatives, "classes": a.classes,
           "shapes": a.shapes, "fit": a.fit, "observer_rc": rc,
           "wall_start": round(wall0, 3), "wall_end": round(time.time(), 3),
           "k1": lat["on_time"], "k1_p50_ci": list(ci) if ci else None, "early": lat["early"],
           "k2": metrics.latency(per, "motion")["on_time"], "reactions": summ.get("reactions"),
           "rtf": summ.get("rtf"), "recognition_msgs": (summ.get("recognition") or {}).get("msgs"),
           "observer_gaps": summ.get("observer_gaps"), "wall": summ.get("wall"),
           "load1_mean": round(sum(loads) / len(loads), 2) if loads else None,
           "label_note": "двойник: K1 — ориентир, не результат робота"}
    with open(os.path.join(out, "k1.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(json.dumps(doc, ensure_ascii=False))
    return 0 if rc == 0 else 1


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("home")
    sub.add_parser("counters")
    r = sub.add_parser("release")
    r.add_argument("--watch", type=float, default=0.0)
    s = sub.add_parser("sample")
    s.add_argument("--seconds", type=float, required=True)
    s.add_argument("--interval", type=float, default=5.0)
    s.add_argument("--out", required=True)
    k = sub.add_parser("k1")
    k.add_argument("--label", required=True)
    k.add_argument("--out", required=True)
    k.add_argument("--seed", type=int, default=1019)
    k.add_argument("--gestures", type=int, default=32)
    k.add_argument("--negatives", type=int, default=4)
    k.add_argument("--tail-s", type=float, default=16.0)
    k.add_argument("--classes", default="0")
    k.add_argument("--shapes", default="circle,c_arc,oval")
    k.add_argument("--fit", action="store_true", help="жест целиком в кадре (gesturegen --fit)")
    a = p.parse_args()
    return {"home": cmd_home, "counters": cmd_counters, "release": cmd_release, "sample": cmd_sample,
            "k1": cmd_k1}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
