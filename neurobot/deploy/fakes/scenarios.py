#!/usr/bin/env python3
"""Сценарии супервизии на подставных компонентах (ВМ sim после setup_stand.sh; root). Проверяет то, что start_test
не покрывает: рестарты, зависания, падение зависимости, защёлку, лишнего писателя скорости и работу сторожа.

    sudo scenarios.py [--out ФАЙЛ.jsonl] [сценарий …]      по умолчанию — все по порядку
  verify    systemd-analyze verify всех установленных юнитов neurobot
  order     холодный старт: каждый уровень (драйверы, safety_gate, исполнитель, …) стартует после готовности
            предыдущего; doctor «ОК»
  kill      kill -9 safety_gate: рестарт, явный ноль в ExecStopPost, время до нового READY
  hang      safety_gate перестаёт слать WATCHDOG: systemd убивает его по WatchdogSec; doctor называет safety
  estop     neurobot estop engage → doctor называет safety/estop_engaged; kill -9 safety_gate → после рестарта
            защёлка на месте (файл StateDirectory); release без терминала — отказ; release в терминале (script) со
            словом release → защёлка снята, doctor «ОК»
  remap     исполнитель без ремапа /cmd_vel в команде — отказ precheck (config_invalid); ремап в команде есть, но
            узел его не применяет (--ignore-remap) — после READY wait-graph снимает исполнитель и распознавание
            (cmd_vel_writer), они не остаются active, doctor называет cmd_vel_writer, а метка wait-graph не даёт
            precheck запустить их снова с той же командой (процесс стартует один раз); команду вернули — метка
            снимается, всё поднимается само
  override  лишний издатель /cmd_vel_tracker: doctor называет safety/cmd_vel_override, после рестарта safety_gate не
            стартует (precheck); издатель ушёл — всё поднимается само
  dep       MCP-сервер не поднимается: doctor называет mcp, PicoClaw — следствие; после починки всё поднимается само
  mcp_hang  MCP-сервер виснет целиком: сторож перезапускает его по пробе (mcp_down)
  tool_hang висит только вызов инструмента (tools/list отвечает): вызов PicoClaw без завершения > 60 с — сторож
            перезапускает MCP-сервер (agent_hung), вызов получает ошибку
  worker    kill -9 воркера лаборатории: systemd поднимает его сам, пока «грузит модель» (503) — doctor называет его;
            второй экземпляр не появляется (N7)
  channel   PicoClaw пишет «Long poll failed … deadline exceeded» (канал VK оглох): сторож перезапускает PicoClaw
            (agent_channel_lost), ключ из URL в журнал сторожа не попадает
Подмена команд — drop-in с EnvironmentFile в /run (как у start_test): её снимает finally сценария (SIGTERM — тоже), а
подмены прогона, убитого SIGKILL, — начало следующего. Строка JSON на сценарий (отказ systemctl или процесса — ok false
и error, остальные сценарии идут дальше); код 1 — хотя бы один не прошёл.
Времена — ориентир (ВМ, подставные процессы), не результат.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from neurobot_deploy import report  # noqa: E402
from neurobot_deploy.config import load, quote  # noqa: E402
from neurobot_deploy.sd import Systemd, SystemdError  # noqa: E402

NB = "/opt/neurobot/bin/neurobot"
FAKE = "/usr/bin/python3 /opt/neurobot/lib/fakes/fake_component.py"
WORK = "/run/neurobot-scenarios"
DROPIN = "/run/systemd/system/{}.d/60-neurobot-scenario.conf"
SAFETY = "neurobot-safety.service"
EXECUTOR = "neurobot-executor.service"
PERCEPTION = "neurobot-perception.service"
TRACKER = "nb-scenario-tracker"  # лишний издатель /cmd_vel_tracker сценария override (systemd-run)
sd = Systemd()
cfg = load()


class ScenarioError(RuntimeError):
    """Сценарий нельзя провести (нет процесса и т. п.): он не прошёл, следующие идут."""


PLAN = cfg.plan()
UNITS = [u for _, u in PLAN]


def sh(*args, check=True) -> str:
    return subprocess.run(list(args), capture_output=True, text=True, check=check).stdout


def prop(unit: str, name: str) -> str:
    return sd.show([unit], ("Id", name)).get(unit, {}).get(name, "")


def mono(unit: str, name: str) -> float:
    return int(prop(unit, name) or 0) / 1e6


def kill9(unit: str) -> None:
    """kill -9 главного процесса юнита. У неактивного юнита MainPID=0, а kill(0, SIGKILL) убил бы всю группу
    процессов самого сценария."""
    pid = prop(unit, "MainPID")
    if not pid.isdigit() or int(pid) <= 0:
        raise ScenarioError("{}: нет главного процесса (MainPID={!r}) — kill -9 некому".format(unit, pid))
    os.kill(int(pid), signal.SIGKILL)


def doctor() -> dict:
    out = sh(NB, "doctor", "--json", check=False)
    try:
        return json.loads(out)
    except ValueError:
        return {"ok": False, "root": {"component": "doctor", "code": "bad_json", "text": out[-200:]}}


def wait(pred, timeout: float, period: float = 0.5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = pred()
        if got:
            return got
        time.sleep(period)
    return None


def doctor_ok():
    d = doctor()
    return d if d.get("ok") else None


def root_is(component: str, code: str = ""):
    """Предикат для wait: doctor назвал этот компонент (и код, если задан)."""
    def pred():
        d = doctor()
        r = d.get("root") or {}
        return d if r.get("component") == component and (not code or r.get("code") == code) else None
    return pred


def override(unit: str, **env) -> None:
    os.makedirs(WORK, exist_ok=True)
    env_file = os.path.join(WORK, unit + ".env")
    with open(env_file, "w", encoding="utf-8") as f:
        f.write("".join("{}={}\n".format(k, quote(v)) for k, v in env.items()))
    path = DROPIN.format(unit)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("[Service]\nEnvironmentFile={}\n".format(env_file))
    sd.ctl("daemon-reload")


def clear() -> None:
    """Снять подмены сценариев: drop-in'ы, их env-файлы и лишнего издателя override."""
    sh("systemctl", "stop", TRACKER, check=False)
    for unit in UNITS:
        for path in (DROPIN.format(unit), os.path.join(WORK, unit + ".env")):
            if os.path.exists(path):
                os.remove(path)
    sd.ctl("daemon-reload")


def journal_since(unit: str, t_wall: float) -> list:
    return [e.message for e in sd.journal(unit=unit, since="@{}".format(int(t_wall) - 1), lines=300)
            if e.ts >= t_wall]


def coded(unit: str, t_wall: float, tool: str, code: str) -> list:
    """Строки причины «neurobot-<tool>: …: <code>: …» в журнале юнита после t_wall."""
    return [ln for ln in journal_since(unit, t_wall)
            for r in [report.parse(ln)] if r and (r.tool, r.code) == (tool, code)]


def levels() -> list:
    """Уровни запуска: экземпляры одного шаблона (драйверы, воркеры) — один уровень, стартуют параллельно."""
    out, prev = [], None
    for c, u in PLAN:
        kind = c.split("@", 1)[0] if "@" in c else None
        if kind and kind == prev:
            out[-1].append(u)
        else:
            out.append([u])
        prev = kind
    return out


def s_verify() -> dict:
    names = ["neurobot.target"] + UNITS + ["neurobot-watchdog.service"]  # экземпляры драйверов — по имени
    r = subprocess.run(["systemd-analyze", "verify"] + names, capture_output=True, text=True,
                       cwd="/etc/systemd/system")
    return {"ok": r.returncode == 0 and not r.stderr.strip(), "units": names, "stderr": r.stderr.strip(),
            "systemd": sd.version()}


def s_order() -> dict:
    sd.ctl("stop", "neurobot.target", *UNITS)
    sd.ctl("reset-failed", *UNITS)
    t0 = time.monotonic()
    sd.ctl("start", "neurobot.target")
    active = {u: round(mono(u, "ActiveEnterTimestampMonotonic") - t0, 3) for u in UNITS}
    starts = {u: round(mono(u, "InactiveExitTimestampMonotonic") - t0, 3) for u in UNITS}
    lv = levels()
    ok = all(min(starts[u] for u in b) >= max(active[u] for u in a) for a, b in zip(lv, lv[1:]))
    rep = wait(doctor_ok, 30)
    return {"ok": ok and bool(rep), "active_at_s": active, "start_at_s": starts, "doctor_ok": bool(rep)}


def s_kill() -> dict:
    before = int(prop(SAFETY, "NRestarts") or 0)
    t_wall, t_mono = time.time(), time.monotonic()
    kill9(SAFETY)
    back = wait(lambda: int(prop(SAFETY, "NRestarts") or 0) > before and prop(SAFETY, "SubState") == "running", 20)
    zero = coded(SAFETY, t_wall, "zero", "zero_sent")
    ok_after = wait(doctor_ok, 30)
    return {"ok": bool(back) and bool(zero) and bool(ok_after), "zero": zero[-1] if zero else None,
            "ttr_s": round(mono(SAFETY, "ActiveEnterTimestampMonotonic") - t_mono, 3)}


def s_hang() -> dict:
    override(SAFETY, NEUROBOT_SAFETY_CMD=FAKE + " safety --hang-after 3")
    try:
        sd.ctl("restart", SAFETY)
        t_wall = time.time()
        named = wait(root_is("safety"), 45)
        lines = journal_since(SAFETY, t_wall)
    finally:
        clear()
        sd.ctl("restart", SAFETY)
    wd = [ln for ln in lines if "watchdog" in ln.lower()]
    return {"ok": bool(named) and bool(wd), "doctor": (named or {}).get("root"), "journal": wd[:2]}


def s_estop() -> dict:
    engage = subprocess.run([NB, "estop", "engage", "--reason", "scenario"], capture_output=True, text=True)
    named = wait(root_is("safety", "estop_engaged"), 10)
    # рестарт safety_gate (падение, сторож systemd) защёлку не снимает: она в StateDirectory, а не в памяти процесса
    before = int(prop(SAFETY, "NRestarts") or 0)
    kill9(SAFETY)
    back = wait(lambda: int(prop(SAFETY, "NRestarts") or 0) > before and prop(SAFETY, "ActiveState") == "active", 30)
    kept = wait(root_is("safety", "estop_engaged"), 20)
    denied = subprocess.run([NB, "estop", "release"], stdin=subprocess.DEVNULL, capture_output=True, text=True)
    still = wait(root_is("safety", "estop_engaged"), 5)
    # человек у терминала: script даёт команде псевдотерминал, слово приходит с его ввода
    released = subprocess.run(["script", "-qec", NB + " estop release", "/dev/null"], input="release\n",
                              capture_output=True, text=True)
    ok_after = wait(doctor_ok, 10)
    return {"ok": engage.returncode == 0 and bool(named) and bool(back) and bool(kept) and denied.returncode == 1
            and "estop_denied" in denied.stderr and bool(still) and released.returncode == 0 and bool(ok_after),
            "engage": engage.stderr.strip()[-200:], "denied": denied.stderr.strip()[-200:],
            "released": released.stdout.strip()[-300:], "doctor_after_engage": (named or {}).get("root"),
            "kept_after_kill9": (kept or {}).get("root")}


def s_remap() -> dict:
    override(EXECUTOR, NEUROBOT_EXECUTOR_CMD=FAKE + " executor")
    try:
        sd.ctl("restart", "--no-block", EXECUTOR)
        static = wait(root_is("executor", "config_invalid"), 30)
    finally:
        clear()
    out = {"precheck_no_remap": (static or {}).get("root")}
    for unit, comp, cmd in ((EXECUTOR, "executor", FAKE + " executor --ignore-remap --ros-args -r "
                             "/cmd_vel:=/neurobot/cmd_vel_req"),
                            (PERCEPTION, "perception", FAKE + " perception --warmup 1 --ignore-remap --ros-args -r "
                             "/cmd_vel:=/cmd_vel_gesture")):
        t_wall, t_mono = time.time(), time.monotonic()
        override(unit, **{"NEUROBOT_{}_CMD".format(comp.upper()): cmd})
        try:
            sd.ctl("restart", "--no-block", unit)
            line = wait(lambda: next(iter(coded(unit, t_wall, "wait", "cmd_vel_writer")), None), 60)
            named = wait(root_is(comp, "cmd_vel_writer"), 30)
            # с меткой wait-graph следующие старты обрывает precheck: процесс с потерянным ремапом стартовал один раз
            marked = wait(lambda: len(coded(unit, t_wall, "precheck", "cmd_vel_writer")) >= 2, 30)
            runs = len(coded(unit, t_wall, "wait", "cmd_vel_writer"))
            never_active = mono(unit, "ActiveEnterTimestampMonotonic") < t_mono
        finally:
            clear()
        out[comp] = {"wait_graph": line, "doctor": (named or {}).get("root"), "precheck_refusals_ok": bool(marked),
                     "wait_graph_runs": runs, "never_active": never_active}
    healed = wait(doctor_ok, 90, period=2)
    out["ok"] = (bool(static) and all(out[c]["wait_graph"] and out[c]["doctor"] and out[c]["precheck_refusals_ok"]
                                      and out[c]["wait_graph_runs"] == 1 and out[c]["never_active"]
                                      for c in ("executor", "perception")) and bool(healed))
    return out


def s_override() -> dict:
    # журнал rcl — в tmpfs, как у юнитов: у root в systemd-run нет $HOME
    sh("systemd-run", "--unit", TRACKER, "--collect", "-p", "EnvironmentFile=" + cfg.path, "-p",
       "RuntimeDirectory=" + TRACKER, "-E", "ROS_LOG_DIR=/run/" + TRACKER, NB + "-run", "--ros", "--", *FAKE.split(),
       "publish", "--topic", "/cmd_vel_tracker", "--node", "tracker_node")
    try:
        named = wait(root_is("safety", "cmd_vel_override"), 20)
        t_wall = time.time()
        sd.ctl("restart", "--no-block", SAFETY)
        refused = wait(lambda: next((ln for ln in journal_since(SAFETY, t_wall)
                                     if "neurobot-precheck: safety: cmd_vel_override" in ln), None), 30)
    finally:
        clear()
    healed = wait(doctor_ok, 60, period=2)
    return {"ok": bool(named) and bool(refused) and bool(healed), "doctor": (named or {}).get("root"),
            "precheck": refused}


def s_dep() -> dict:
    override("neurobot-mcp.service", NEUROBOT_MCP_CMD="sleep 3600", NEUROBOT_WAIT_S="4")
    try:
        sd.ctl("stop", "neurobot.target", *UNITS)
        sd.ctl("reset-failed", *UNITS)
        sd.ctl("start", "--no-block", "neurobot.target")
        named = wait(root_is("mcp", "mcp_timeout"), 60)
    finally:
        clear()
    agent = next((f for f in (named or {}).get("findings", []) if f["component"] == "picoclaw"), None)
    t0 = time.monotonic()
    healed = wait(doctor_ok, 90, period=2)
    return {"ok": bool(named) and bool(healed), "doctor_root": (named or {}).get("root"), "picoclaw": agent,
            "healed_after_fix_s": round(time.monotonic() - t0, 1) if healed else None}


def _watchdog_restart(code: str, u: str = "neurobot-mcp.service", **env) -> dict:
    override(u, **env)
    try:
        sd.ctl("restart", u)
        t_wall = time.time()
        line = wait(lambda: next((ln for ln in journal_since("neurobot-watchdog.service", t_wall)
                                  if ": {}: restart {}".format(code, u) in ln), None), 150, period=1)
    finally:
        clear()
        sd.ctl("restart", u)
    return {"ok": bool(line), "watchdog": line, "after_s": round(time.time() - t_wall, 1)}


def s_mcp_hang() -> dict:
    return _watchdog_restart("mcp_down", NEUROBOT_MCP_CMD=FAKE + " mcp --hang-after 20")


def s_tool_hang() -> dict:
    return _watchdog_restart("agent_hung", NEUROBOT_MCP_CMD=FAKE + " mcp --hang-tools-after 5")


def s_worker() -> dict:
    w = cfg.labworkers()[0]
    u = "neurobot-labworker@{}.service".format(w)
    before = int(prop(u, "NRestarts") or 0)
    t_mono = time.monotonic()
    kill9(u)
    named = wait(root_is("labworker@" + w), 20, period=0.3)
    back = wait(lambda: int(prop(u, "NRestarts") or 0) > before and prop(u, "ActiveState") == "active", 60)
    ttr = round(mono(u, "ActiveEnterTimestampMonotonic") - t_mono, 3)
    workers = sh("pgrep", "-fc", "fake_component.py worker --name " + w, check=False).strip()
    ok_after = wait(doctor_ok, 30)
    return {"ok": bool(named) and bool(back) and workers == "1" and bool(ok_after),
            "doctor_while_loading": (named or {}).get("root"), "ttr_s": ttr, "worker_processes": workers}


def s_channel() -> dict:
    rec = _watchdog_restart("agent_channel_lost", "neurobot-picoclaw.service",
                            NEUROBOT_PICOCLAW_CMD=FAKE + " picoclaw --channel-fail-after 3")
    rec["key_redacted"] = bool(rec["watchdog"]) and "fakekey" not in rec["watchdog"] and "<REDACTED>" in rec["watchdog"]
    rec["ok"] = rec["ok"] and rec["key_redacted"]
    return rec


SCENARIOS = {"verify": s_verify, "order": s_order, "kill": s_kill, "hang": s_hang, "estop": s_estop,
             "remap": s_remap, "override": s_override, "dep": s_dep, "mcp_hang": s_mcp_hang, "tool_hang": s_tool_hang,
             "worker": s_worker, "channel": s_channel}


def main() -> int:
    p = argparse.ArgumentParser(description="сценарии супервизии neurobot на подставных компонентах")
    p.add_argument("names", nargs="*", help="сценарии: {} (по умолчанию все)".format(" ".join(SCENARIOS)))
    p.add_argument("--out", help="дописать строки JSON в файл")
    args = p.parse_args()
    unknown = [n for n in args.names if n not in SCENARIOS]
    if unknown or os.geteuid() != 0:
        print("scenarios: {}".format("неизвестные сценарии {}".format(unknown) if unknown else "нужен root"),
              file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))
    clear()
    ok = True
    for name in args.names or list(SCENARIOS):
        t0 = time.monotonic()
        try:
            rec = SCENARIOS[name]()
        except (ScenarioError, SystemdError, subprocess.SubprocessError, OSError) as e:  # не прошёл, следующие идут
            rec = {"ok": False, "error": str(e)}
        rec = dict(rec, scenario=name, took_s=round(time.monotonic() - t0, 1))
        ok &= rec["ok"]
        line = json.dumps(rec, ensure_ascii=False)
        print(line, flush=True)
        if args.out:
            with open(args.out, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
