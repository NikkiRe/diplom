#!/usr/bin/env python3
"""Подменный издатель /scan отдельным процессом (в ВМ sim; узел — свой участник DDS, как чужая программа на роботе).

    python3 scan_spoof.py ИМЯ_УЗЛА ГЦ [nan|inf]

Берёт метаданные первого настоящего скана (в нём есть конечные дальности) и публикует его копию, где все лучи — NaN
(«нет данных») или +inf («свободно»), с частотой ГЦ под именем ИМЯ_УЗЛА: с именем драйвера (urg_node2) — подмена,
которую safety_gate видит как второй издатель драйвера. Остановка — SIGINT или SIGTERM. Логи — stderr.
"""
from __future__ import annotations

import logging
import math
import sys

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import LaserScan

log = logging.getLogger("scan_spoof")


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s: %(message)s")
    try:
        name, hz = sys.argv[1], float(sys.argv[2])
        ok = len(sys.argv) in (3, 4) and sys.argv[3:] in ([], ["nan"], ["inf"]) and hz > 0.0
    except (IndexError, ValueError):
        ok = False
    if not ok:
        log.error("аргументы: ИМЯ_УЗЛА ГЦ [nan|inf], ГЦ > 0")
        return 2
    value = math.inf if sys.argv[3:] == ["inf"] else math.nan
    rclpy.init()
    node = rclpy.create_node(name)
    pub = node.create_publisher(LaserScan, "/scan", qos_profile_sensor_data)
    fake = []

    def on_scan(msg: LaserScan) -> None:
        if not fake and any(math.isfinite(r) for r in msg.ranges):
            msg.ranges = [value] * len(msg.ranges)
            fake.append(msg)
            log.info("подмена /scan от %s: %d лучей %s, %g Гц", name, len(msg.ranges), value, hz)

    def tick() -> None:
        if fake:
            fake[0].header.stamp = node.get_clock().now().to_msg()
            pub.publish(fake[0])
    node.create_subscription(LaserScan, "/scan", on_scan, qos_profile_sensor_data)
    node.create_timer(1.0 / hz, tick)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    node.destroy_node()
    rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
