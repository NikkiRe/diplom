#!/usr/bin/env python3
"""(в ВМ sim) scan_mask — ретранслятор скана стенда: /stand/scan_raw (мост Gazebo) → /scan с искажениями, которые
видны в скане с робота NeuroChip (R7: lidar_scan_1780424753942739755.json, 02.06.2026, urg_node2, 683 луча):
  occluder — NaN в секторе −60,8…+4,2° (лучи 168–353 скана робота). D435i стоит перед URG почти в плоскости скана
             (R7b RS3, [гипотеза, сильная]). urg_node2 отдаёт NaN на сырой 0 («нет отражения»), поэтому здесь NaN,
             а не дальность до корпуса камеры, которую вернул бы gpu_lidar;
  codes    — 0,006 м в секторе −119,9…−99,5° (лучи 0–58 скана робота, справа сзади): коды ошибок URG ниже
             range_min (R7a N14).
Сектора заданы углами (по геометрии скана робота), поэтому маска верна при любом числе лучей. Остальные поля
сообщения не меняются. Узел запускает launch стенда только при флагах --scan-occluder / --scan-codes; без них
/scan публикует мост.
  python3 scan_mask.py --ros-args -p occluder:=true -p codes:=false
"""
from __future__ import annotations

import math
import sys
from typing import List, Tuple

ROBOT_ANGLE_MIN = -2.0923497677     # скан робота 02.06: angle_min = −angle_max
ROBOT_INCREMENT = 0.006135923322    # 2π/1024
CODE_M = 0.006


def _rays(first: int, last: int) -> Tuple[float, float]:
    """Угловые границы лучей first…last скана робота (с запасом в полшага)."""
    return ROBOT_ANGLE_MIN + (first - 0.5) * ROBOT_INCREMENT, ROBOT_ANGLE_MIN + (last + 0.5) * ROBOT_INCREMENT


OCCLUDER = _rays(168, 353)
CODES = _rays(0, 58)


def indices(n: int, angle_min: float, increment: float, sector: Tuple[float, float]) -> List[int]:
    return [i for i in range(n) if sector[0] <= angle_min + i * increment <= sector[1]]


def plan(n: int, angle_min: float, increment: float, occluder: bool, codes: bool) -> List[Tuple[int, float]]:
    """Какие лучи чем заменить: [(индекс, значение)]; окклюдер важнее кодов."""
    out = {}
    if codes:
        out.update((i, CODE_M) for i in indices(n, angle_min, increment, CODES))
    if occluder:
        out.update((i, math.nan) for i in indices(n, angle_min, increment, OCCLUDER))
    return sorted(out.items())


def main():
    import rclpy
    from rcl_interfaces.msg import ParameterDescriptor
    from rclpy.executors import ExternalShutdownException
    from rclpy.node import Node
    from sensor_msgs.msg import LaserScan

    class ScanMask(Node):
        def __init__(self):
            super().__init__("scan_mask")

            def p(name, default):
                value = self.declare_parameter(name, default, ParameterDescriptor(dynamic_typing=True)).value
                if type(value) is not type(default):
                    raise ValueError("%s: ожидается %s, получено %r" % (name, type(default).__name__, value))
                return value

            self._occluder, self._codes = p("occluder", False), p("codes", False)
            self._geometry, self._plan = None, []
            # QoS как у моста Gazebo: reliable, глубина 10.
            self._pub = self.create_publisher(LaserScan, p("output_topic", "/scan"), 10)
            self.create_subscription(LaserScan, p("input_topic", "/stand/scan_raw"), self._on_scan, 10)
            self.get_logger().info("окклюдер D435i: %s, коды URG: %s" % (self._occluder, self._codes))

        def _on_scan(self, msg):
            geometry = (len(msg.ranges), msg.angle_min, msg.angle_increment)
            if geometry != self._geometry:
                self._geometry, self._plan = geometry, plan(*geometry, self._occluder, self._codes)
            for i, value in self._plan:
                msg.ranges[i] = value
            self._pub.publish(msg)

    rclpy.init()
    node = ScanMask()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
