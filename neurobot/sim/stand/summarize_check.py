#!/usr/bin/env python3
"""Сводка check.sh: критерии профиля neurobot → summary.json, версии/хеши/конфиги/отклонения → manifest.json.

  python3 summarize_check.py <out_dir>        код 0 — все критерии выполнены, 1 — нет
Числа стенда — ориентир, не результат (RTF, частоты, времена stm32); критерии проверяют, что стенд собран как задумано.
Ожидания берутся оттуда же, откуда их берёт стенд: args.txt прогона (профиль и флаги), умолчания stand_config.py,
маска scan_mask.py; положения стен и препятствий — из worlds/lab_wall.sdf и lab_low.sdf. Нет данных — критерий не
выполнен (value null), а не пропущен.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

STAND = Path(__file__).resolve().parent
PROJ = STAND.parents[2]
sys.path.insert(0, str(STAND))
from scan_mask import plan  # noqa: E402
from stand_config import BASES, DEFAULT_BASE, STM32_DEFAULTS  # noqa: E402

RATES = {"/scan": (10, 0.5), "/odom": (50, 2.5), "/ground_truth/pose": (100, 5)}
ODOM_FIXED_HZ = (20, 1)
RTF_MIN = 0.95
NEAREST_M = 0.5          # ближе — лидар видит сам робот (ближайшее в lab_wall — box_left, ≈0,86 м у TB3)
STOP_EXTRA_S = 0.15      # после сторожа и задержки: такт 20 мс, торможение DiffDrive, квант /odom 20 мс
LIMIT_LIN = (0.15, 0.02)
WALL_X = 1.5             # ближняя грань wall_front (lab_wall.sdf)
FRONT_TOL = 0.05         # front_min — минимум шумного скана (σ 0,01) по ±10°
TOUCH_TOL = 0.03
CRASH_MAX_S = 20.0       # stand_procs down: SIGINT, через 10 с SIGTERM, через 15 с SIGKILL
# lab_low.sdf, ближняя грань препятствий x = 0,65; чем касается база neurochip (models/neurochip_base.sdf):
# палуба (край 0,195), мачта (−0,015 + 0,0125), голова URG (−0,015 + 0,021).
NEG_CONTACT_X = {"neg_box": 0.65 - 0.195, "neg_shelf": 0.65 - (-0.015 + 0.0125), "neg_table": 0.65 - (-0.015 + 0.021)}
NEG_UNSEEN_M = 1.0       # препятствие в 0,665 м от лидара, если бы лидар его видел; дальше — стена в 1,5 м
RUN_FILES = ("ready", "load", "rates", "probe", "probe_wall", "probe_obstacle", "crash")
FILES = ["neurobot/sim/stand", "scripts/sim/start_sim.sh", "scripts/sim/vm_start.sh", "scripts/sim/vm_stop.sh",
         "scripts/sim/launch/robot_sim.launch.py", "scripts/sim/profiles/neurobot.sh"]
DEVIATIONS = [
    "База по умолчанию — TurtleBot3 waffle_pi (геометрия, колёса, DiffDrive Gazebo), а не база NeuroChip; "
    "--base neurochip — своя простая модель по оценкам с фото (R7b §2.3: круг Ø0,39 м, колея 0,38, лидар на 0,67 м); "
    "масса, усилие мотора и тормозной путь — оценки, смысл манёвров по времени стенд не проверяет (S6, RS7)",
    "На роботе хуже, чем на TB3 (R7b RS1–RS3): лидар TB3 на ≈0,17 м видит низкие препятствия (box_left 0,4 м виден "
    "только на TB3), запас от края базы до порога демо 0,25 м — 0,12 м против ≈0,04 м у робота, слепого сектора "
    "−61…+4° нет (только флаг --scan-occluder), кодов ошибок URG нет (только --scan-codes): касания и запасы на TB3 — "
    "оптимистично",
    "Цепочки twist_mux → diff_drive_controller → UART → STM32 нет — её заменяет stm32_sim: hold (0, худший случай), "
    "сторож 300 мс (просьба PLAN) или 500 мс (cmd_vel_timeout diff_drive_controller Foxy по умолчанию, на роботе не "
    "подтверждён); чистая задержка 20 мс — [гипотеза до HIL]; пределы — допущение в СИ; приоритеты twist_mux "
    "(/cmd_vel_joy, /cmd_vel_tracker) не эмулируются",
    "Камеры D435i/RealSense нет: камера Gazebo удалена из модели; кадры даёт подставной pyrealsense2 других "
    "компонентов",
    "Лидар — gpu_lidar Gazebo с геометрией скана робота 02.06 (683 луча, ±2,0923 рад, 5,6 м, 10 Гц), шум σ 0,01 м как "
    "у TB3, а не по паспорту URG; на TB3 стоит на месте лидара TB3, ближняя граница 0,06 м вместо 0,02 (корпус LDS), "
    "frame_id base_scan вместо laser_frame (у --base neurochip — 0,02 м и laser_frame)",
    "QoS /scan: мост Gazebo публикует reliable; QoS urg_node2 на роботе не снят (ANALYSIS S5)",
    "header.stamp у /scan, /odom, /ground_truth/pose — время симуляции от старта Gazebo, на роботе — системное",
    "/odom DiffDrive считается по колёсам: робот, упёршийся в стену, в /odom продолжает «ехать» "
    "(колёса проскальзывают); движение и столкновения — по /ground_truth/pose",
    "ROS 2 Jazzy + Cyclone DDS вместо Foxy на роботе; /cmd_vel — Twist, как на Foxy",
    "Хост общий (ВМ sbc, процессы других агентов): нагрузка хоста во время замера — hold/host_load.txt",
]


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def parse_args_txt(text: str) -> dict:
    """Строки «ключ:=значение» из args.txt прогона; повтор ключа — последний важнее (как у ros2 launch)."""
    out = {}
    for line in text.splitlines():
        if ":=" in line:
            key, value = line.split(":=", 1)
            out[key] = value
    return out


def effective(args: dict) -> dict:
    """Аргументы прогона поверх умолчаний стенда."""
    out = {"base": DEFAULT_BASE, "scan_occluder": "false", "scan_codes": "false"}
    out.update(("stm32_" + n, v) for n, v in STM32_DEFAULTS)
    out.update(args)
    return out


def expected_scan(args: dict) -> dict:
    """Геометрия и маска /scan, которые должен дать прогон с такими аргументами."""
    base = BASES[args["base"]]
    n, lo, hi = int(args["lidar_samples"]), float(args["lidar_min_angle"]), float(args["lidar_max_angle"])
    masked = plan(n, lo, (hi - lo) / (n - 1), args["scan_occluder"] == "true", args["scan_codes"] == "true")
    return {"n": n, "angle_min": lo, "angle_max": hi, "range_min": float(args.get("lidar_range_min") or base.range_min),
            "range_max": float(args["lidar_range_max"]), "frame_id": base.scan_frame,
            "n_nan": sum(1 for _, v in masked if v != v), "n_below_min": sum(1 for _, v in masked if v == v)}


def _close(value, expect, tol):
    return value is not None and abs(value - expect) <= tol


def _in(value, lo, hi):
    return value is not None and lo <= value <= hi


def checks(runs: dict, starts: list) -> list:
    """runs — {прогон: {ready, load, rates, probe, probe_wall, probe_obstacle, crash, args}}; starts — [{ready, up_s}]
    → [{name, value, expect, ok}]."""
    out = []

    def add(name, value, expect, ok):
        out.append({"name": name, "value": value, "expect": expect, "ok": bool(ok)})

    def run(name):
        r = dict(runs.get(name) or {})
        r["args"] = effective(r.get("args") or {})
        ready = r.get("ready") or {}
        add(name + ": стенд готов", ready.get("missing"), "[]", ready.get("ready"))
        return r

    def rates(name, r, topics):
        vm = ((r.get("load") or {}).get("vm") or {}).get("rates") or {}
        add(name + ": RTF, окно замера", vm.get("rtf"), ">= %g" % RTF_MIN, _in(vm.get("rtf"), RTF_MIN, 1e9))
        for topic in topics:
            hz, tol = RATES[topic]
            v = (vm.get("topics") or {}).get(topic, {}).get("hz_sim")
            add("%s: %s, Гц сим." % (name, topic), v, "%g ± %g" % (hz, tol), _close(v, hz, tol))

    def scan(name, r):
        got = (r.get("ready") or {}).get("scan") or {}
        exp = expected_scan(r["args"]) if "lidar_samples" in r["args"] else {}
        for k in ("n", "frame_id", "n_nan", "n_below_min"):
            add("%s: /scan %s" % (name, k), got.get(k), exp.get(k), exp and got.get(k) == exp[k])
        for k in ("angle_min", "angle_max", "range_min", "range_max"):
            add("%s: /scan %s" % (name, k), got.get(k), exp.get(k), exp and _close(got.get(k), exp[k], 1e-5))
        add("%s: /scan ближайшее, м" % name, got.get("min_valid"), ">= %g (нет попаданий в себя)" % NEAREST_M,
            _in(got.get("min_valid"), NEAREST_M, 1e9))
        front = WALL_X - BASES[r["args"]["base"]].lidar_x
        add("%s: /scan прямо по курсу — wall_front, м" % name, got.get("front_min"), "%.3f ± %g" % (front, FRONT_TOL),
            _close(got.get("front_min"), front, FRONT_TOL))

    def stop(name, r, probe="probe"):
        a = r["args"]
        lo = (int(a["stm32_timeout_ms"]) + int(a["stm32_delay_ms"])) / 1000.0
        v = (r.get(probe) or {}).get("stop_after_s")
        add("%s: сторож %s мс, задержка %s мс — остановка после последней команды, с" % (
            name, a["stm32_timeout_ms"], a["stm32_delay_ms"]), v, "%g…%g" % (lo, lo + STOP_EXTRA_S),
            _in(v, lo, lo + STOP_EXTRA_S))

    def crash(r, what):
        c = r.get("crash") or {}
        add(what + " — группа стенда погасла, с", c.get("group_gone_after_s"), "<= %g" % CRASH_MAX_S,
            _in(c.get("group_gone_after_s"), 0, CRASH_MAX_S))
        add(what + " — gz sim в ВМ не осталось", c.get("gz_sim_left"), [], c.get("gz_sim_left") == [])

    def wall(name, r):
        pose = (r.get("probe_wall") or {}).get("pose_end")
        x = WALL_X - BASES[r["args"]["base"]].front_m
        add("%s: hold, 0,5 м/с и тишина — база упёрлась в стену, x, м" % name, pose and pose[0],
            "%.3f ± %g" % (x, TOUCH_TOL), pose is not None and _close(pose[0], x, TOUCH_TOL))

    r = run("hold")
    rates("hold", r, ("/scan", "/odom", "/ground_truth/pose"))
    scan("hold", r)
    cmd = (r.get("ready") or {}).get("cmd_vel") or {}
    want = ["geometry_msgs/msg/" + r["args"]["stm32_cmd_vel_type"]]
    add("hold: /cmd_vel тип", cmd.get("types"), want, cmd.get("types") == want)
    add("hold: /cmd_vel подписчики", cmd.get("subscribers"), ["stm32_sim"], cmd.get("subscribers") == ["stm32_sim"])
    add("hold: /cmd_vel писатели до проверки", cmd.get("publishers"), [], cmd.get("publishers") == [])
    topics = (r.get("ready") or {}).get("topics")
    cams = [t for t in topics or [] if t.startswith("/camera")]
    add("hold: камера Gazebo выключена — темы /camera*", cams, [], topics and not cams)
    p = r.get("probe") or {}
    need = 0.8 * (p.get("v_cmd_end") or 0) * p.get("silence_s", 0)   # едет почти с той же скоростью всю тишину
    add("hold: stm32 hold, тишина — не остановился", p.get("stop_after_s"), None, p and p.get("stop_after_s") is None)
    add("hold: stm32 hold, путь за тишину, м", p.get("coast_m"), ">= %.2f" % need,
        need > 0 and _in(p.get("coast_m"), need, 1e9))
    wall("hold", r)

    stop("timeout300", run("timeout300"))

    r = run("stamped500")
    cmd = (r.get("ready") or {}).get("cmd_vel") or {}
    want = ["geometry_msgs/msg/" + r["args"]["stm32_cmd_vel_type"]]
    add("stamped500: /cmd_vel тип", cmd.get("types"), want, cmd.get("types") == want)
    stop("stamped500", r)

    r = run("odom20_limits")
    odom = ((r.get("rates") or {}).get("topics") or {}).get("/odom", {}).get("hz_sim")
    add("odom20_limits: --odom-hz 20 действует, /odom Гц сим.", odom, "%g ± %g" % ODOM_FIXED_HZ,
        _close(odom, *ODOM_FIXED_HZ))
    v = (r.get("probe") or {}).get("v_cmd_end")
    add("odom20_limits: stm32 max_lin 0,15 при команде 0,2, м/с", v, "%g ± %g" % LIMIT_LIN, _close(v, *LIMIT_LIN))
    crash(r, "odom20_limits: kill -9 stm32_sim")

    r = run("neurochip")      # первый подъём после падения stm32_sim
    rates("neurochip", r, ("/scan", "/odom"))
    scan("neurochip", r)
    wall("neurochip", r)

    for name, x in NEG_CONTACT_X.items():
        r = run(name)
        front = ((r.get("ready") or {}).get("scan") or {}).get("front_min")
        add("%s: лидар не видит препятствие по курсу, front_min, м" % name, front, "> %g" % NEG_UNSEEN_M,
            _in(front, NEG_UNSEEN_M, 1e9))
        pose = (r.get("probe_obstacle") or {}).get("pose_end")
        add("%s: база въехала в препятствие, x, м" % name, pose and pose[0], "%.3f ± %g" % (x, TOUCH_TOL),
            pose is not None and _close(pose[0], x, TOUCH_TOL))
    crash(r, "neg_table: сервер gz завис (SIGSTOP), kill -9 stm32_sim")

    if starts:
        ok = sum(1 for s in starts if (s.get("ready") or {}).get("ready"))
        add("холодные подъёмы up → ready → down: готовы", "%d/%d" % (ok, len(starts)), "%d/%d" % (len(starts),
            len(starts)), ok == len(starts))
    return out


def load_brief(load: dict) -> dict:
    """RTF рядом с занятостью ВМ и хоста: RTF ниже порога при чужой нагрузке на ВМ — не свойство стенда."""
    vm = load.get("vm") or {}
    return {"rtf": (vm.get("rates") or {}).get("rtf"), "vm_cpu_cores_busy": vm.get("vm_cpu_cores_busy"),
            "sim_cpu_cores": vm.get("sim_cpu_cores"), "sim_pss_mib": vm.get("sim_pss_mib"),
            "host_vz_cpu_cores": (load.get("host") or {}).get("vz_cpu_cores")}


def _sha256_tree(rel: str) -> dict:
    """Хеши кода и данных стенда; README — документация, его правят и после прогона, в хеши он не входит."""
    root = PROJ / rel
    paths = sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts
                   and p.suffix != ".md") if root.is_dir() else [root]
    return {str(p.relative_to(PROJ)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def _text(path: Path):
    return path.read_text().strip() if path.exists() else None


def _collect(d: Path) -> dict:
    run = {k: _load(d / (k + ".json")) for k in RUN_FILES}
    run["args"] = parse_args_txt(_text(d / "args.txt") or "")
    up = re.search(r"готово за (\d+) с", _text(d / "start.txt") or "")
    run["up_s"] = int(up.group(1)) if up else None
    return run


def main():
    out = Path(sys.argv[1])
    runs = {d.name: _collect(d) for d in sorted(out.iterdir()) if d.is_dir() and d.name != "starts"}
    starts = [_collect(d) for d in sorted((out / "starts").iterdir()) if d.is_dir()] if (out / "starts").is_dir() \
        else []
    res = checks(runs, starts)
    up_s = [s["up_s"] for s in starts if s["up_s"] is not None]
    summary = {"all_ok": all(c["ok"] for c in res), "checks": res,
               "probes": {n + "/" + k: r[k] for n, r in runs.items()
                          for k in ("probe", "probe_wall", "probe_obstacle", "crash") if r.get(k)},
               "load": {n: load_brief(r["load"]) for n, r in runs.items() if r.get("load")},
               "starts": {"n": len(starts), "up_s_min": min(up_s, default=None), "up_s_max": max(up_s, default=None)},
               "status": "ориентир стенда (Gazebo на Mac), не результат"}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n")
    files = {}
    for rel in FILES:
        files.update(_sha256_tree(rel))
    manifest = {
        "what": "Э1: стенд neurobot в Gazebo (профиль neurobot, stm32_sim, база neurochip) — проверка check.sh",
        "command": _text(out / "command.txt"),
        "run_started": _text(out / "run_started.txt"),
        "run_finished": _text(out / "run_finished.txt"),
        "summarized_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "versions": {"vm": (_text(out / "versions_vm.txt") or "").splitlines(),
                     "host": (_text(out / "versions_host.txt") or "").splitlines()},
        "files_sha256": files,
        "configs": {n: (_text(out / n / "args.txt") or "").splitlines() for n in runs},
        "host_load": (_text(out / "hold" / "host_load.txt") or "").splitlines(),
        "deviations": DEVIATIONS,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")
    for c in res:
        print("%s %-70s %s (ожидание %s)" % ("OK  " if c["ok"] else "FAIL", c["name"], c["value"], c["expect"]))
    return 0 if summary["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
