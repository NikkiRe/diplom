"""(ВМ sim) Узел отказов камеры для A1 (README §2.2): пересылает кадры цвета двойника в тему, которую читает perception
(`--camera ros`), и по тому же faults.json, что подставной pyrealsense2 у A0, в секундах от якоря таймлайна
вносит отказы на уровне темы — так выглядит сбой `realsense2_camera` на роботе:
  delay, stall, gone — кадров нет duration_s; freeze — тот же кадр с тем же header.stamp (perception его кадром не
  считает); black — нулевые пиксели с новой меткой; slow — не чаще fps.
До появления якоря кадры идут без отказов. Журнал — JSONL {t, m, ev: fault_start|fault_end, kind}; логи — stderr.

  python3 relay.py --faults faults.json --anchor anchor.json --out-topic /runs/camera/color/image_raw --log FILE
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import List, Optional

from neurobot.sim.gestures.clock import mono, read_anchor

DROP = ("delay", "stall", "gone")


class Faults:
    """Отказ, действующий в момент m (монотонные часы), по расписанию от якоря."""

    def __init__(self, faults: List[dict], anchor_path: str):
        self.faults, self.anchor_path, self.anchor = faults, anchor_path, None

    def active(self, m: float) -> Optional[dict]:
        if self.anchor is None:
            if not os.path.exists(self.anchor_path):
                return None
            try:
                self.anchor = read_anchor(self.anchor_path)
            except (ValueError, KeyError):
                return None
        rel = self.anchor.rel_of(m)
        for f in self.faults:
            if f["at_s"] <= rel < f["at_s"] + f.get("duration_s", 0.0):
                return f
        return None


class Relay:
    """Решение по кадру без ROS: что опубликовать (None — ничего)."""

    def __init__(self, faults: Faults, journal):
        self.faults, self.journal = faults, journal
        self.current: Optional[dict] = None
        self.last = None
        self.last_out = -1e9

    def on_frame(self, msg, m: float, black):
        f = self.faults.active(m)
        if f is not self.current:
            if self.current is not None:
                self.journal({"m": m, "ev": "fault_end", "kind": self.current["kind"]})
            if f is not None:
                self.journal({"m": m, "ev": "fault_start", "kind": f["kind"], "duration_s": f.get("duration_s")})
            self.current = f
        if f is None:
            self.last = msg
            return msg
        kind = f["kind"]
        if kind in DROP:
            return None
        if kind == "freeze":
            return self.last
        if kind == "black":
            return black(msg)
        if kind == "slow":
            if m - self.last_out < 1.0 / max(float(f.get("fps", 1.0)), 0.1):
                return None
            self.last_out = m
            return msg
        return msg


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="узел отказов камеры A1")
    p.add_argument("--faults", required=True, help="faults.json расписания (schedule.py)")
    p.add_argument("--anchor", required=True, help="якорь таймлайна прогона (его создаёт FakeYolo в perception)")
    p.add_argument("--in-topic", default="/camera/color/image_raw", help="кадры двойника")
    p.add_argument("--out-topic", required=True, help="тема для perception (ремап в его команде)")
    p.add_argument("--log", required=True, help="журнал отказов JSONL")
    a = p.parse_args(argv)
    with open(a.faults, encoding="utf-8") as f:
        faults = Faults(json.load(f), a.anchor)

    def journal(rec: dict) -> None:
        rec["t"] = time.time()
        with open(a.log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print("relay: %s" % json.dumps(rec, ensure_ascii=False), file=sys.stderr, flush=True)

    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import Image

    def black(msg):
        out = Image()
        out.header, out.height, out.width, out.encoding = msg.header, msg.height, msg.width, msg.encoding
        out.is_bigendian, out.step = msg.is_bigendian, msg.step
        out.data = bytes(len(msg.data))
        return out

    relay = Relay(faults, journal)
    rclpy.init()
    node = rclpy.create_node("runs_camera_relay")
    pub = node.create_publisher(Image, a.out_topic, qos_profile_sensor_data)

    def cb(msg):
        out = relay.on_frame(msg, mono(), black)
        if out is not None:
            pub.publish(out)
    node.create_subscription(Image, a.in_topic, cb, qos_profile_sensor_data)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
