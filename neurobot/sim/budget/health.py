"""(ВМ sim) Здоровье стека во время замера: частоты и состояния тем робота → JSONL раз в interval.

  python3 -m neurobot.sim.budget.health --out health.jsonl [--interval 1]

Подписки (best effort — совместимы с любым издателем): /neurobot/perception_health (fps, state),
/neurobot/safety_state (state), /neurobot/gesture, /neurobot/motion_state, /neurobot/cmd_vel_req, /cmd_vel
(число и ненулевые), /diff_cont/cmd_vel_unstamped, /twin/truth (rtf двойника). Строка окна: число сообщений по
темам, последние в окне fps и state perception, счёт состояний safety_gate, ненулевые /cmd_vel, rtf. По этим
строкам report.py отличает «работает» от «деградирует»: кадры/с, молчание perception, 5 Гц safety_state.
Выход — по SIGTERM/SIGINT; ошибки — в stderr.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from collections import Counter
from typing import Dict, List, Optional

TOPICS = {
    "health": "/neurobot/perception_health",
    "safety": "/neurobot/safety_state",
    "gesture": "/neurobot/gesture",
    "motion": "/neurobot/motion_state",
    "cmd_req": "/neurobot/cmd_vel_req",
    "cmd_vel": "/cmd_vel",
    "mux_out": "/diff_cont/cmd_vel_unstamped",
    "truth": "/twin/truth",
}


class Window:
    """Сводка сообщений за окно; flush() отдаёт строку и начинает новое окно. Значения (fps, state, rtf) — только
    из сообщений этого окна: замолчавший издатель не оставляет в строках старых чисел."""

    def __init__(self) -> None:
        self.counts: Counter = Counter()
        self.safety: Counter = Counter()
        self.nonzero_cmd = 0
        self.last: Dict[str, object] = {}

    def add_json(self, key: str, text: str) -> None:
        self.counts[key] += 1
        try:
            d = json.loads(text)
        except ValueError:
            self.counts[key + "_bad"] += 1
            return
        if not isinstance(d, dict):
            self.counts[key + "_bad"] += 1
        elif key == "health":
            self.last["fps"], self.last["perception"] = d.get("fps"), d.get("state")
        elif key == "safety":
            self.safety[str(d.get("state"))] += 1
        elif key == "truth" and d.get("rtf") is not None:
            self.last["rtf"] = d["rtf"]
        elif key == "gesture":
            self.last["gesture"] = d.get("name")

    def add_twist(self, key: str, lin: float, ang: float) -> None:
        self.counts[key] += 1
        if key == "cmd_vel" and (lin or ang):
            self.nonzero_cmd += 1

    def flush(self, t_wall: float, dt: float) -> dict:
        rec = {"t_wall": t_wall, "dt": dt, "n": dict(self.counts), "safety": dict(self.safety),
               "cmd_vel_nonzero": self.nonzero_cmd}
        rec.update(self.last)
        self.counts, self.safety, self.nonzero_cmd, self.last = Counter(), Counter(), 0, {}
        return rec


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", required=True)
    p.add_argument("--interval", type=float, default=1.0, help="окно, с")
    a = p.parse_args(argv)
    if not 0.2 <= a.interval <= 10:
        p.error("--interval 0,2–10 с")
    import rclpy
    from geometry_msgs.msg import Twist
    from rclpy.qos import qos_profile_sensor_data
    from std_msgs.msg import String

    rclpy.init()
    node = rclpy.create_node("budget_health")
    win = Window()
    for key, topic in TOPICS.items():
        if key in ("cmd_req", "cmd_vel", "mux_out"):
            node.create_subscription(Twist, topic, lambda m, k=key: win.add_twist(k, m.linear.x, m.angular.z),
                                     qos_profile_sensor_data)
        else:
            node.create_subscription(String, topic, lambda m, k=key: win.add_json(k, m.data), qos_profile_sensor_data)
    running = [True]
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: running.__setitem__(0, False))
    t_last = time.monotonic()
    with open(a.out, "a", encoding="utf-8") as out:
        while running[0]:
            rclpy.spin_once(node, timeout_sec=0.05)
            now = time.monotonic()
            if now - t_last >= a.interval:
                out.write(json.dumps(win.flush(time.time(), round(now - t_last, 3))) + "\n")
                out.flush()
                t_last = now
    node.destroy_node()
    rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
