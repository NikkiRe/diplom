"""(ВМ sim) Однократный издатель /robot_description для заместителя базы (launch/base_proxy.launch.py).

controller_manager в Jazzy берёт URDF только из темы /robot_description (transient local), а robot_state_publisher
робота работает в основном домене (перенесённый узел двойника). Узел публикует файл, ждёт подписчика до 60 с и
выходит — второго robot_state_publisher, которого на роботе нет, в бюджете не появляется.
  python3 describe.py <robot.urdf>
"""
from __future__ import annotations

import sys
import time

import rclpy
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String


def main() -> int:
    if len(sys.argv) != 2:
        print("describe: нужен путь к URDF", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as f:
        urdf = f.read()
    rclpy.init()
    node = rclpy.create_node("budget_describe")
    qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL, reliability=ReliabilityPolicy.RELIABLE)
    pub = node.create_publisher(String, "/robot_description", qos)
    pub.publish(String(data=urdf))
    deadline = time.monotonic() + 60
    while pub.get_subscription_count() == 0 and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.1)
    ok = pub.get_subscription_count() > 0
    time.sleep(1.0)                       # доставка подписчику, появившемуся последним
    if not ok:
        print("describe: за 60 с нет подписчика /robot_description", file=sys.stderr)
    node.destroy_node()
    rclpy.try_shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
