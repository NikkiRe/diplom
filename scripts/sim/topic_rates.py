#!/usr/bin/env python3
"""(в VM) Частоты тем ROS 2 и RTF симуляции без накладных расходов `ros2 topic hz`.

Подписки raw=True (сообщения не десериализуются — замер не упирается в Python на /camera/image_raw),
QoS reliable/keep_last(10). Частота — по стенному времени и по времени симуляции (/clock);
RTF = Δsim / Δwall за окно. Печатает JSON.
  python3 topic_rates.py --seconds 20 [--topics /scan /odom ...]
"""
import argparse
import json
import sys
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy
from rosidl_runtime_py.utilities import get_message

DEFAULT = ["/scan", "/odom", "/imu", "/camera/image_raw", "/camera/camera_info", "/joint_states", "/tf", "/cmd_vel"]


class Rates(Node):
    def __init__(self, topics):
        super().__init__("sim_topic_rates")
        self.stats = {}
        self.sim_t = None
        self.subs = []
        # RELIABLE: публикаторы моста (ros_gz_bridge, image_bridge) — reliable; с BEST_EFFORT большие кадры камеры
        # (921 КБ, фрагментация UDP в Cyclone DDS) не доходят вовсе.
        qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST,
                         durability=DurabilityPolicy.VOLATILE)
        types = dict(self.get_topic_names_and_types())
        self.missing = []
        for t in topics:
            if t not in types:
                self.missing.append(t)
                continue
            mt = get_message(types[t][0])
            self.stats[t] = {"type": types[t][0], "n": 0, "bytes": 0, "t_first": None, "t_last": None}
            self.subs.append(self.create_subscription(mt, t, self._cb(t), qos, raw=True))
        clk = get_message("rosgraph_msgs/msg/Clock")
        self.clock_n = 0
        self.subs.append(self.create_subscription(clk, "/clock", self._on_clock, qos))

    def _cb(self, topic):
        st = self.stats[topic]

        def cb(raw):
            now = time.monotonic()
            st["n"] += 1
            st["bytes"] += len(raw)
            if st["t_first"] is None:
                st["t_first"] = now
            st["t_last"] = now

        return cb

    def _on_clock(self, msg):
        self.clock_n += 1
        self.sim_t = msg.clock.sec + msg.clock.nanosec * 1e-9


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--warmup", type=float, default=2.0)
    ap.add_argument("--topics", nargs="+", default=DEFAULT)
    a = ap.parse_args()
    rclpy.init()
    # Дать графу ROS обнаружиться, иначе get_topic_names_and_types пуст.
    probe = rclpy.create_node("sim_topic_rates_probe")
    t_end = time.monotonic() + 5
    while time.monotonic() < t_end and len(probe.get_topic_names_and_types()) < 3:
        rclpy.spin_once(probe, timeout_sec=0.2)
    probe.destroy_node()
    node = Rates(a.topics)
    t_end = time.monotonic() + a.warmup
    while time.monotonic() < t_end:
        rclpy.spin_once(node, timeout_sec=0.05)
    for st in node.stats.values():
        st.update(n=0, bytes=0, t_first=None, t_last=None)
    node.clock_n = 0
    w0, s0 = time.monotonic(), node.sim_t
    t_end = w0 + a.seconds
    while time.monotonic() < t_end:
        rclpy.spin_once(node, timeout_sec=0.05)
    w1, s1 = time.monotonic(), node.sim_t
    wall = w1 - w0
    out = {"wall_s": round(wall, 3), "missing": node.missing}
    if s0 is not None and s1 is not None:
        sim = s1 - s0
        out.update(sim_s=round(sim, 3), rtf=round(sim / wall, 4), clock_hz_wall=round(node.clock_n / wall, 1))
    topics = {}
    for t, st in node.stats.items():
        r = {"type": st["type"], "n": st["n"], "hz_wall": round(st["n"] / wall, 2)}
        if out.get("sim_s"):
            r["hz_sim"] = round(st["n"] / out["sim_s"], 2)
        if st["n"]:
            r["bytes_per_msg"] = round(st["bytes"] / st["n"])
            r["kib_per_s"] = round(st["bytes"] / wall / 1024, 1)
        topics[t] = r
    out["topics"] = topics
    print(json.dumps(out, ensure_ascii=False, indent=1))
    node.destroy_node()
    rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
