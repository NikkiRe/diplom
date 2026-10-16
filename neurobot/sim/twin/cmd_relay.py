#!/usr/bin/env python3
"""(в ВМ sim) Звено двойника между twist_mux и diff_drive_controller — узел twin_cmd_relay.

/diff_cont/cmd_vel_unstamped (Twist, выход twist_mux) → /diff_cont/cmd_vel (TwistStamped, вход diff_drive_controller).
На роботе (Foxy, use_stamped_vel: false) контроллер читает /diff_cont/cmd_vel_unstamped сам; в Jazzy входа без метки
нет. Метка времени — нулевая: контроллер Jazzy подставляет своё время приёма («Received TwistStamped with zero
timestamp…», один раз в журнале), и cmd_vel_timeout отсчитывается от прихода команды, как на роботе. Поэтому узлу не
нужно время симуляции: готовый twist_stamper с use_sim_time разбирает /clock на 1 кГц и ест ≈25 % ядра [замер].

Начальная команда — ноль, как у контроллера Foxy: после configure его опорная скорость — пустой Twist
(ros2_controllers foxy, diff_drive_controller.cpp:406), и /odom идёт в каждом такте. Контроллер Jazzy до первой
команды держит опорную скорость NaN и при cmd_vel_timeout 0 (hold) не публикует ни /odom, ни TF odom → base_link.
Поэтому, пока нет ни /odom, ни команд от twist_mux, а у /diff_cont/cmd_vel есть подписчик, узел раз в 0,5 с шлёт
нулевой TwistStamped: неактивный контроллер команды отбрасывает, и одной посылки мало. Первое /odom или первая команда
прекращают это до конца работы узла.
Параметр: frame_id (base_link).
"""
from __future__ import annotations

PRIME_PERIOD_S = 0.5


def main():
    import os
    import sys

    import rclpy
    from geometry_msgs.msg import Twist, TwistStamped
    from nav_msgs.msg import Odometry
    from rclpy.executors import ExternalShutdownException
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from twin_config import DIFF_CMD, MUX_OUT

    class CmdRelay(Node):
        def __init__(self):
            super().__init__("twin_cmd_relay")
            self.frame_id = str(self.declare_parameter("frame_id", "base_link").value)
            self.pub = self.create_publisher(TwistStamped, DIFF_CMD, 10)
            self.create_subscription(Twist, MUX_OUT, self.on_cmd, 10)
            self.odom = self.create_subscription(Odometry, "/odom", self.primed, qos_profile_sensor_data)
            self.prime = self.create_timer(PRIME_PERIOD_S, self.on_prime)

        def stamped(self, twist):
            out = TwistStamped()
            out.header.frame_id = self.frame_id
            out.twist = twist
            return out

        def on_cmd(self, msg):
            self.primed()
            self.pub.publish(self.stamped(msg))

        def on_prime(self):
            if self.pub.get_subscription_count():
                self.pub.publish(self.stamped(Twist()))

        def primed(self, _msg=None):
            if self.prime is not None:
                self.destroy_timer(self.prime)
                self.destroy_subscription(self.odom)
                self.prime = self.odom = None

    rclpy.init()
    node = CmdRelay()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
