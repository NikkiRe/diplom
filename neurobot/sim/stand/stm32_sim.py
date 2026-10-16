#!/usr/bin/env python3
"""(в ВМ sim) stm32_sim — узел стенда между /cmd_vel и входом DiffDrive в Gazebo: эмуляция цепочки
diff_drive_controller → UART → STM32 робота NeuroChip (логика и обоснование режимов — stm32_model.py).

Параметры ROS:
  cmd_vel_type   Twist | TwistStamped — тип входа (на роботе Foxy — Twist);         по умолчанию Twist
  input_topic    вход (единственный писатель — страж, исполнитель или демо);       /cmd_vel
  output_topic   вход DiffDrive через ros_gz_bridge (Twist; задаёт launch стенда);  /stand/base_cmd_vel
  timeout_ms     0 — hold (держит последнюю команду), N > 0 — тишина N мс → ноль;  0
  delay_ms       чистая задержка команды до базы;                                   0
  max_lin, max_ang, max_lin_acc, max_ang_acc — пределы, 0 — без предела [допущение: СИ];  0
  rate_hz        такт сторожа и разгона;                                            50
timeout_ms и delay_ms — в целых миллисекундах ≥ 0: дробное или логическое значение — ошибка запуска, а не тихое
усечение. Время — монотонные часы ВМ (как у микроконтроллера), не время симуляции. Команда применяется в колбэке
(при задержке — разовым таймером), без ожидания такта; выход публикуется только при изменении, первый — явный ноль.
В журнал (stderr) — предупреждения: сторож остановил базу; отброшена неконечная команда.
"""
import sys
import time

import rclpy
from geometry_msgs.msg import Twist, TwistStamped
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node

from stm32_model import Stm32Model, ms_param, num_param

TYPES = {"Twist": Twist, "TwistStamped": TwistStamped}


class Stm32Sim(Node):
    def __init__(self, **kwargs):
        super().__init__("stm32_sim", **kwargs)

        def p(name, default):
            # dynamic_typing: «-p max_lin:=1» (целое из командной строки) не должно ронять узел при умолчании 0.0;
            # тип и диапазон проверяют ms_param / num_param.
            return self.declare_parameter(name, default, ParameterDescriptor(dynamic_typing=True)).value

        type_name = p("cmd_vel_type", "Twist")
        if type_name not in TYPES:
            raise ValueError("cmd_vel_type должен быть Twist или TwistStamped, получено %r" % type_name)
        self._stamped = type_name == "TwistStamped"
        timeout_ms = ms_param("timeout_ms", p("timeout_ms", 0))
        delay_ms = ms_param("delay_ms", p("delay_ms", 0))
        rate_hz = num_param("rate_hz", p("rate_hz", 50.0))
        if rate_hz == 0:
            raise ValueError("rate_hz должен быть больше 0")
        self._delay_s = delay_ms / 1000.0
        self._model = Stm32Model(timeout_s=timeout_ms / 1000.0, delay_s=self._delay_s,
                                 **{n: num_param(n, p(n, 0.0)) for n in ("max_lin", "max_ang", "max_lin_acc",
                                                                          "max_ang_acc")})
        self._pub = self.create_publisher(Twist, str(p("output_topic", "/stand/base_cmd_vel")), 10)
        self._out = None
        self._timeouts = 0
        self._tick()
        input_topic = str(p("input_topic", "/cmd_vel"))
        self.create_subscription(TYPES[type_name], input_topic, self._on_cmd, 10)
        self.create_timer(1.0 / rate_hz, self._tick)
        self.get_logger().info("%s (%s) → база: %s, задержка %d мс" % (
            input_topic, type_name, "hold, без сторожа" if timeout_ms == 0 else "сторож %d мс" % timeout_ms,
            delay_ms))

    def _on_cmd(self, msg):
        tw = msg.twist if self._stamped else msg
        if not self._model.command(tw.linear.x, tw.angular.z, time.monotonic()):
            self.get_logger().warning("неконечная команда (NaN/inf) отброшена, всего %d" % self._model.rejected,
                                      throttle_duration_sec=1.0)
        elif self._delay_s > 0:
            self._later(self._delay_s)
        else:
            self._tick()

    def _later(self, delay_s):
        """Разовый такт через delay_s: команда доходит до базы точно, а не на ближайшем такте rate_hz."""
        timer = None

        def fire():
            timer.cancel()
            self.destroy_timer(timer)
            self._tick()

        timer = self.create_timer(delay_s, fire)

    def _tick(self):
        out = self._model.step(time.monotonic())
        if self._model.timeouts != self._timeouts:
            self._timeouts = self._model.timeouts
            self.get_logger().warning("тишина на входе дольше %.0f мс при ненулевой команде → ноль "
                                      "(срабатываний сторожа: %d)" % (self._model.timeout_s * 1000, self._timeouts))
        if out != self._out:
            msg = Twist()
            msg.linear.x, msg.angular.z = out
            self._pub.publish(msg)
            self._out = out


def main():
    rclpy.init()
    node = Stm32Sim()
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
