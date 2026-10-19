"""(ВМ sim, root) Жест для start_test пакета (README §6): `neurobot start-test --gesture-cmd "… check …"`.

  python3 -m neurobot.sim.runs.start_gesture prepare --sched DIR [--mockllm URL]   перед start_test: таймлайн
        профиля start на место таймлайна пакета, узел кадров камеры (relay.py без отказов), PicoClaw — на mockllm,
        пакет остановлен
  python3 -m neurobot.sim.runs.start_gesture check --sched DIR --log FILE   в цикле после «ОК» doctor

check: берёт ближайший жест таймлайна, начало которого ещё впереди (якорь ставит perception этого холодного старта),
ждёт его конца t0 и не позже t0 + --window с: заявку исполнителя (/neurobot/cmd_vel_req), первую ненулевую /cmd_vel и
движение по /odom (|ω| > 0,1 рад/с или |v| > 0,05 м/с). Успех — заявка есть, и робот поехал или safety_gate был
в blocked (жест дошёл до исполнителя, а движение законно не пустила защита: робот за 100 стартов мог подъехать к
препятствию). Строка в --log — {t, seg, lat_req_s, lat_cmd_s, lat_motion_s, blocked, ok}. Код 0 — успех, 1 — нет.
Затем удаляет якорь: следующий холодный старт начнёт таймлайн заново (идущий perception держит якорь в памяти).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from neurobot.sim.gestures.clock import mono, read_anchor
from neurobot.sim.runs import stacks
from neurobot.sim.runs.metrics import segments


def pick(segs, rel_now: float, margin: float = 0.3):
    """Ближайший жест, который начнётся не раньше чем через margin с; None — таймлайн кончился."""
    for s in segs:
        if s["kind"] == "gesture" and s["t_start"] >= rel_now + margin:
            return s
    return None


def ros_log_dir(log_path: str) -> str:
    """Каталог журналов rcl рядом с журналом жестов: start_test зовёт check без HOME и ROS_LOG_DIR, и rclpy.init
    без них падает («Failed to get logging directory»)."""
    path = os.path.join(os.path.dirname(os.path.abspath(log_path)), "logs", "ros_gesture")
    os.makedirs(path, exist_ok=True)
    return path


def check(sched: str, log_path: str, window: float) -> int:
    config, _ = stacks.deploy_lib()
    os.environ.update(config.load(stacks.ENV_FILE).ros_environ())
    os.environ["ROS_LOG_DIR"] = ros_log_dir(log_path)
    a1 = stacks.A1("/var/tmp/nb-runs/start", sched, "")
    if not stacks.wait(lambda: os.path.exists(a1.anchor_path), 30):
        print("start_gesture: якоря нет — perception не вызвал детектор", file=sys.stderr)
        return 1
    anchor = read_anchor(a1.anchor_path)
    seg = pick(segments(os.path.join(sched, "truth.jsonl")), anchor.rel_of(mono()))
    if seg is None:
        print("start_gesture: таймлайн кончился — жеста впереди нет", file=sys.stderr)
        return 1
    import rclpy
    from geometry_msgs.msg import Twist
    from nav_msgs.msg import Odometry
    from std_msgs.msg import String
    rclpy.init()
    node = rclpy.create_node("runs_start_gesture")
    t0 = anchor.mono_of(seg["t0"])
    seen = {}

    def mark(key: str, moving: bool) -> None:
        if moving and mono() >= t0 and key not in seen:
            seen[key] = mono()

    def on_safety(m):
        try:
            mark("blocked", json.loads(m.data).get("state") == "blocked")
        except ValueError:
            pass
    node.create_subscription(Twist, "/neurobot/cmd_vel_req", lambda m: mark(
        "req", abs(m.linear.x) > 1e-3 or abs(m.angular.z) > 1e-3), 50)
    node.create_subscription(Twist, "/cmd_vel", lambda m: mark(
        "cmd", abs(m.linear.x) > 1e-3 or abs(m.angular.z) > 1e-3), 50)
    node.create_subscription(Odometry, "/odom", lambda m: mark(
        "motion", abs(m.twist.twist.angular.z) > 0.1 or abs(m.twist.twist.linear.x) > 0.05), 50)
    node.create_subscription(String, "/neurobot/safety_state", on_safety, 50)
    while mono() < t0 + window and not ("req" in seen and "motion" in seen):
        rclpy.spin_once(node, timeout_sec=0.05)
    node.destroy_node()
    rclpy.try_shutdown()
    rec = {"t": round(time.time(), 3), "seg": seg["seg"], "shape": seg.get("shape"), "blocked": "blocked" in seen,
           "ok": "req" in seen and ("motion" in seen or "blocked" in seen)}
    for key in ("req", "cmd", "motion"):
        rec["lat_%s_s" % key] = round(seen[key] - t0, 3) if key in seen else None
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.remove(a1.anchor_path)
    return 0 if rec["ok"] else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m neurobot.sim.runs.start_gesture", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("prepare", help="таймлайн start и узел кадров перед start_test")
    pr.add_argument("--sched", required=True, help="каталог расписания профиля start")
    pr.add_argument("--mockllm", default="", help="bench/mockllm в ВМ для PicoClaw пакета (пусто — не трогать)")
    ch = sub.add_parser("check", help="жест холодного старта (--gesture-cmd start_test)")
    ch.add_argument("--sched", required=True, help="каталог расписания профиля start")
    ch.add_argument("--log", required=True, help="журнал жестов JSONL")
    ch.add_argument("--window", type=float, default=8.0, help="ждать реакцию до t0 + N с")
    a = p.parse_args(argv)
    if a.cmd == "prepare":
        os.makedirs("/var/tmp/nb-runs/start/logs", exist_ok=True)
        stacks.A1("/var/tmp/nb-runs/start", a.sched, a.mockllm).prepare()
        return 0
    return check(a.sched, a.log, a.window)


if __name__ == "__main__":
    sys.exit(main())
