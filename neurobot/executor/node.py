"""motion_executor v1 — rclpy-узел вокруг guard.Guard (PLAN §2.4 v1, §13.3).

Пишет желаемую скорость в /neurobot/cmd_vel_req; единственный писатель /cmd_vel — safety_gate (neurobot/safety).
Входы: /cmd_vel_gesture и /cmd_vel_agent (Twist), /scan (LaserScan) и /odom (Odometry, след для отъезда из тупика) с
QoS сенсоров (совместим и с reliable, и с best-effort издателем), /neurobot/safety_state (String JSON safety_gate).
Выходы: /neurobot/cmd_vel_req (Twist или TwistStamped — параметр cmd_vel_type), /neurobot/motion_state и
/neurobot/events (String JSON: task_end при отмене задачи по safety_gate, начало и конец отъезда из тупика). Имена тем
меняются ремапом (--ros-args -r).

Публикация: при смене команды — сразу из колбэка входа (без ожидания тика, L2); на тике 20 Гц — каждую
ненулевую команду, нули в покое — каждый второй тик (10 Гц). Явный ноль — при старте (до READY), на
SIGTERM/SIGINT и при исключении (finally в main). WATCHDOG — от тика.
"""
from __future__ import annotations

import json
import logging
import math
import signal
import sys
import threading
import time
import uuid

import rclpy
from geometry_msgs.msg import Twist, TwistStamped
from nav_msgs.msg import Odometry
from rclpy.exceptions import ParameterException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import LaserScan
from std_msgs.msg import String

from .guard import Command, Guard, Params, parse_safety
from .sd_notify import Notifier

log = logging.getLogger("motion_executor")

EXIT_CONFIG = 2
CMD_VEL_REQ = "/neurobot/cmd_vel_req"

# В узле без safety_gate задачи не исполняются (require_safety); Guard() без параметров — правила v1 без него.
GUARD_DEFAULTS = Params(require_safety=True)

# ROS-параметры и умолчания (README)
DEFAULTS = (
    ("cmd_vel_type", "Twist"),  # Twist (робот, Foxy; стенд neurobot) | TwistStamped
    ("frame_id", "base_link"),  # header.frame_id для TwistStamped
    ("rate_hz", 20.0),          # тик автомата
    ("state_hz", 2.0),          # /neurobot/motion_state без изменений
) + tuple(GUARD_DEFAULTS._asdict().items())

POSITIVE = ("rate_hz", "state_hz", "input_timeout", "scan_timeout", "d_stop", "decel", "sector_deg",
            "safety_timeout", "start_quiet_s", "escape_speed", "escape_dist")
NON_NEGATIVE = ("react_s", "max_gap_deg", "escape_after_s")


class ConfigError(Exception):
    pass


def check_types(p: dict) -> dict:
    """Типы как у умолчаний (на Foxy rclpy их не сверяет: строка «false» в allow_blind была бы истинной);
    целое годится для дробного. Возвращает значения с дробными, приведёнными к float."""
    out = {}
    for name, default in DEFAULTS:
        v = p[name]
        if isinstance(default, bool):
            ok = isinstance(v, bool)
        elif isinstance(default, float):
            ok = isinstance(v, (int, float)) and not isinstance(v, bool)
            v = float(v) if ok else v
        else:
            ok = isinstance(v, str)
        if not ok:
            raise ConfigError("{}: ожидается {}, получено {!r}".format(name, type(default).__name__, v))
        out[name] = v
    return out


class MotionExecutor(Node):
    def __init__(self):
        super().__init__("motion_executor")
        try:
            p = {name: self.declare_parameter(name, default).value for name, default in DEFAULTS}
        except ParameterException as e:
            raise ConfigError(str(e)) from e
        p = check_types(p)
        if p["cmd_vel_type"] not in ("Twist", "TwistStamped"):
            raise ConfigError("cmd_vel_type: ожидается Twist или TwistStamped, получено {!r}".format(p["cmd_vel_type"]))
        for name in POSITIVE:
            if not p[name] > 0:
                raise ConfigError("{} должен быть > 0, получено {!r}".format(name, p[name]))
        for name in NON_NEGATIVE:
            if not p[name] >= 0:
                raise ConfigError("{} должен быть ≥ 0, получено {!r}".format(name, p[name]))
        self.guard = Guard(Params(**{name: p[name] for name in Params._fields}))
        self._stamped = p["cmd_vel_type"] == "TwistStamped"
        self._frame_id = p["frame_id"]
        self._state_period = 1.0 / p["state_hz"]
        self._boot = uuid.uuid4().hex[:8]
        self._seq = 0
        self._ticks = 0
        self._published = 0
        self._last = None          # последняя опубликованная пара (linear, angular)
        self._state_key = None     # (moving, by, front, cut) последнего motion_state
        self._state_t = -math.inf
        self._scan_error = False
        self._safety_error = False
        self._events = 0
        self.notifier = Notifier()

        self._pub = self.create_publisher(TwistStamped if self._stamped else Twist, CMD_VEL_REQ, 10)
        self._state_pub = self.create_publisher(String, "/neurobot/motion_state", 10)
        self._events_pub = self.create_publisher(String, "/neurobot/events", 10)
        self.create_subscription(Twist, "/cmd_vel_gesture", self._on_gesture, 10)
        self.create_subscription(Twist, "/cmd_vel_agent", self._on_agent, 10)
        self.create_subscription(LaserScan, "/scan", self._on_scan, qos_profile_sensor_data)
        self.create_subscription(Odometry, "/odom", self._on_odom, qos_profile_sensor_data)
        self.create_subscription(String, "/neurobot/safety_state", self._on_safety, qos_profile_sensor_data)

        self._publish(0.0, 0.0)
        self.notifier.ready()
        self.create_timer(1.0 / p["rate_hz"], self._tick)
        log.info("исполнитель запущен: %s", json.dumps(p, ensure_ascii=False))

    def _on_gesture(self, msg: Twist) -> None:
        now = time.monotonic()
        self.guard.gesture(now, msg.linear.x, msg.angular.z)
        self._react(now)

    def _on_agent(self, msg: Twist) -> None:
        now = time.monotonic()
        self.guard.agent(now, msg.linear.x, msg.angular.z)
        self._react(now)

    def _on_scan(self, msg: LaserScan) -> None:
        now = time.monotonic()
        try:
            self.guard.scan(now, msg.ranges, msg.angle_min, msg.angle_increment, msg.range_min, msg.range_max)
        except Exception:
            # скан не принят и устареет — вперёд будет запрещено; в журнал — первая ошибка серии
            if not self._scan_error:
                log.exception("скан не разобран")
            self._scan_error = True
            return
        self._scan_error = False
        self._react(now)

    def _on_odom(self, msg: Odometry) -> None:
        p, q = msg.pose.pose.position, msg.pose.pose.orientation
        if all(math.isfinite(v) for v in (p.x, p.y, q.x, q.y, q.z, q.w)):
            self.guard.odom(p.x, p.y, math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)))

    def _on_safety(self, msg: String) -> None:
        now = time.monotonic()
        try:
            self.guard.safety(now, parse_safety(msg.data))
        except ValueError as e:
            # не принят — устареет, и задачи отменятся как при потере safety_gate; в журнал — первая ошибка серии
            if not self._safety_error:
                log.warning("%s", e)
            self._safety_error = True
            return
        self._safety_error = False
        self._react(now)

    def _command(self, now: float) -> Command:
        """Команда стража и события, накопленные им, — в /neurobot/events."""
        cmd = self.guard.command(now)
        for event in self.guard.events:
            self._events += 1
            log.warning("%s: %s %s", " ".join(filter(None, (event["type"], event.get("task")))), event["status"],
                        "; ".join(event["reasons"]))
            self._events_pub.publish(String(data=json.dumps(dict(event, boot=self._boot, seq=self._events),
                                                            ensure_ascii=False)))
        self.guard.events.clear()
        return cmd

    def _react(self, now: float) -> None:
        cmd = self._command(now)
        if (cmd.linear, cmd.angular) != self._last:
            self._publish(cmd.linear, cmd.angular)
        self._state(cmd, now)

    def _tick(self) -> None:
        now = time.monotonic()
        cmd = self._command(now)
        self._ticks += 1
        moving = cmd.linear != 0.0 or cmd.angular != 0.0
        if moving or (cmd.linear, cmd.angular) != self._last or self._ticks % 2 == 0:
            self._publish(cmd.linear, cmd.angular)
        self._state(cmd, now)
        self.notifier.watchdog()

    def _publish(self, linear: float, angular: float) -> None:
        twist = Twist()
        twist.linear.x = float(linear)
        twist.angular.z = float(angular)
        if self._stamped:
            msg = TwistStamped()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.header.frame_id = self._frame_id
            msg.twist = twist
        else:
            msg = twist
        self._pub.publish(msg)
        self._last = (linear, angular)
        self._published += 1

    def _state(self, cmd: Command, now: float) -> None:
        moving = cmd.linear != 0.0 or cmd.angular != 0.0
        key = (moving, cmd.by, cmd.front, cmd.cut)
        if key == self._state_key and now - self._state_t < self._state_period:
            return
        self._state_key, self._state_t = key, now
        self._seq += 1
        age = self.guard.scan_age(now)
        nearest = self.guard.front.nearest
        doc = {
            "boot": self._boot, "seq": self._seq, "moving": moving, "by": cmd.by,
            "blocked_front": cmd.blocked_front, "front": cmd.front, "cut": cmd.cut,
            "linear": cmd.linear, "angular": cmd.angular,
            "scan_age_s": None if age is None else round(age, 3),
            "front_min_m": None if math.isinf(nearest) else round(nearest, 3),
            "front_gap_deg": round(self.guard.front.gap_deg, 1),
            "safety": self.guard.safety_state(now),
            "counters": dict(self.guard.counters, ticks=self._ticks, published=self._published),
        }
        self._state_pub.publish(String(data=json.dumps(doc)))

    def halt(self) -> None:
        """Явный ноль на выходе (S2) и STOPPING для systemd."""
        self.notifier.stopping()
        if rclpy.ok():
            self._publish(0.0, 0.0)
            # запас на доставку нуля до удаления писателя: не все RMW ждут подтверждений [гипотеза]
            time.sleep(0.1)
        log.info("исполнитель остановлен, ноль опубликован; счётчики %s", json.dumps(self.guard.counters))


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    rclpy.init(args=argv)
    # Свои обработчики поверх rclpy: на Jazzy rclpy по сигналу сам закрывает контекст, и ноль на выход
    # опубликовать было бы уже нельзя. Здесь сигнал лишь просит выйти из цикла, ноль шлёт finally.
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    node = None
    code = 0
    try:
        node = MotionExecutor()
        while rclpy.ok() and not stop.is_set():
            rclpy.spin_once(node, timeout_sec=0.1)
    except ConfigError as e:
        log.error("неверная конфигурация: %s", e)
        code = EXIT_CONFIG
    except Exception:
        log.exception("исполнитель упал")
        code = 1
    finally:
        if node is not None:
            node.halt()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return code
