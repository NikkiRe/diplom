"""Узел rclpy живого шлюза пульта (docs/ui/PLAN.md §3, П2) и точка входа `python -m neurobot.ui live`.

Читает граф сам, без rosbridge агента: /neurobot/safety_state (в нём и запрос исполнителя req), perception_health,
gesture, events, /cmd_vel (выход safety_gate — он нужен для задержки жеста), /scan (сырым, разбор — только кадров,
которые пойдут в поток: 5 Гц из 10), /tf_static. Лишних подписок нет: на A57 каждый колбэк rclpy — заметная доля
ядра [оценка ревью, замер — П6]. Пишет только в live.PUBLISHERS — /neurobot/stop_now и
/neurobot/estop (engage): издатели создаются из этой константы при старте (первый СТОП не ждёт обнаружения DDS) с QoS
reliable + volatile. Аргументы ROS из командной строки узел не принимает (ремап превратил бы издателя стопа в
писателя /cmd_vel); имя узла — NEUROBOT_UI_NODE, не neurobot_*: так CLI называет свои служебные узлы, и проверки
писателей их пропускают. После READY издателей узла сверяет `neurobot wait-graph ui`, во время работы — doctor.

Опросчик (свой поток, не rclpy): юниты — systemctl show раз в 5 с, RSS и CPU процессов юнитов — /proc раз в 10 с.
API rclpy — только то, что есть в ветке foxy (Node(enable_rosout, start_parameter_services, use_global_arguments),
create_subscription(raw=True), rclpy.serialization.deserialize_message, count_subscribers,
get_publisher_names_and_types_by_node, SingleThreadedExecutor); Python 3.8. Совместимость с Foxy проверена только
статически (README пульта): сырой скан без raw или serialization — обычная подписка.
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from typing import List, Optional, Sequence, Tuple

import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from sensor_msgs.msg import LaserScan
from std_msgs.msg import String
from tf2_msgs.msg import TFMessage

from neurobot.executor.sd_notify import Notifier
from neurobot.ui import gateway, live, server

log = logging.getLogger("neurobot-ui")

GESTURE_TOPIC = "/neurobot/gesture"     # perception v2 (predict_live), имя в коде лаборатории с нашим патчем
# Fast DDS — только UDP: SHM между пользователями neurobot-ui и jetson данных не пропускает (fastdds_udp.xml)
FASTDDS_UDP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fastdds_udp.xml")
EVENTS_TOPIC = "/neurobot/events"       # исполнитель (task_end, отъезд)
TICK_S = live.SILENT_S / 2
UNITS_S, MEM_S = 5.0, 10.0


def msg_class(type_name: str):
    """'geometry_msgs/msg/Twist' → класс сообщения."""
    pkg, _, name = type_name.split("/")
    return getattr(importlib.import_module(pkg + ".msg"), name)


class GatewayNode(Node):
    def __init__(self, cfg, lv: live.Live):
        super().__init__(cfg.get("NEUROBOT_UI_NODE"), use_global_arguments=False, enable_rosout=False,
                         start_parameter_services=False)
        self.live = lv
        self._lock = threading.Lock()
        volatile = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE)
        self._pubs = {topic: self.create_publisher(String, topic, volatile) for topic in live.PUBLISHERS}
        sensors = qos_profile_sensor_data
        latched = QoSProfile(depth=100, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                             reliability=ReliabilityPolicy.RELIABLE)
        for topic, fn, qos in ((cfg.get("NEUROBOT_SAFETY_STATE_TOPIC"), lv.on_safety, sensors),
                               (cfg.get("NEUROBOT_HEALTH_TOPIC"), lv.on_health, sensors),
                               (GESTURE_TOPIC, lv.on_gesture, 10), (EVENTS_TOPIC, lv.on_event, 10)):
            self.create_subscription(String, topic, lambda m, fn=fn: self._json(m, fn), qos)
        self.create_subscription(msg_class(cfg.get("NEUROBOT_CMD_VEL_TYPE")), cfg.get("NEUROBOT_CMD_VEL"),
                                 lambda m: self._cmd("out", m), sensors)
        self._deserialize = None
        try:        # скан ≈5,5 КБ, 10 Гц: разбирать только те, что пойдут в поток
            from rclpy.serialization import deserialize_message
            self.create_subscription(LaserScan, cfg.get("NEUROBOT_SCAN_TOPIC"), self._scan_raw, sensors, raw=True)
            self._deserialize = deserialize_message
        except (ImportError, TypeError):
            self.create_subscription(LaserScan, cfg.get("NEUROBOT_SCAN_TOPIC"), self._scan, sensors)
        self.create_subscription(TFMessage, "/tf_static", self._tf, latched)

    # -- входы

    @staticmethod
    def _json(msg, fn) -> None:
        try:
            doc = json.loads(msg.data)
        except ValueError:
            return
        if isinstance(doc, dict):
            fn(doc, time.time())

    def _cmd(self, role: str, msg) -> None:
        tw = msg.twist if hasattr(msg, "twist") else msg
        self.live.on_cmd(role, tw.linear.x, tw.angular.z, time.time())

    def _scan(self, msg, t: Optional[float] = None) -> None:
        t = time.time() if t is None else t
        if self.live.want_scan(t):
            self.live.on_scan(msg.header.frame_id, msg.angle_min, msg.angle_increment, msg.ranges, msg.range_min,
                              msg.range_max, t)

    def _scan_raw(self, data: bytes) -> None:
        t = time.time()
        if self.live.want_scan(t):
            self._scan(self._deserialize(data, LaserScan), t)

    def _tf(self, msg) -> None:
        self.live.on_tf_static([
            (tr.header.frame_id, tr.child_frame_id,
             (tr.transform.translation.x, tr.transform.translation.y, tr.transform.translation.z),
             (tr.transform.rotation.x, tr.transform.rotation.y, tr.transform.rotation.z, tr.transform.rotation.w))
            for tr in msg.transforms])

    # -- действия (gateway.Gateway зовёт из потока HTTP)

    @property
    def name(self) -> str:
        return self.get_name()

    def publishers(self) -> List[str]:
        """Темы, в которые узел пишет, по графу (самопроверка /healthz)."""
        return sorted(t for t, _ in self.get_publisher_names_and_types_by_node(self.get_name(), self.get_namespace()))

    def _publish(self, topic: str, doc: dict, copies: int) -> int:
        data = json.dumps(doc, ensure_ascii=False)
        with self._lock:
            pub = self._pubs[topic]
            for _ in range(copies):
                pub.publish(String(data=data))
            return self.count_subscribers(topic)

    def stop(self, doc: dict) -> int:
        return self._publish(live.STOP_TOPIC, doc, live.STOP_COPIES)

    def engage(self, doc: dict) -> int:
        if doc.get("cmd") != "engage":
            raise ValueError("пульт ставит защёлку и только")
        return self._publish(live.ESTOP_TOPIC, doc, live.ESTOP_COPIES)


class Poller(threading.Thread):
    """Юниты (systemctl show, без root) и ресурсы (/proc) — не в потоке rclpy: подпроцесс systemctl — десятки мс."""

    def __init__(self, cfg, lv: live.Live, stop: threading.Event, plan: Sequence[Tuple[str, str]]):
        super().__init__(name="ui-poller", daemon=True)
        from neurobot_deploy.sd import Systemd
        self.sd = Systemd()
        self.lv, self.stop_evt, self.plan = lv, stop, list(plan)
        self.proc = live.ProcReader({unit: comp for comp, unit in self.plan})

    def run(self) -> None:
        next_units = next_mem = 0.0
        while not self.stop_evt.is_set():
            now = time.monotonic()
            if now >= next_units:
                next_units = now + UNITS_S
                try:
                    show = self.sd.show([u for _, u in self.plan], ("Id", "ActiveState", "SubState", "NRestarts"))
                    self.lv.on_units(gateway.units_rows(show, self.plan), time.time())
                except Exception as e:  # noqa: BLE001 — опросчик не должен ронять шлюз
                    log.warning("юниты не прочитаны: %s", e)
            if now >= next_mem:
                next_mem = now + MEM_S
                self.lv.on_mem(self.proc.sample(), time.time(), self.proc.clk_tck)
            self.stop_evt.wait(0.5)


def unit_plan(cfg) -> List[Tuple[str, str]]:
    """Юниты стека для пульта: состав env-файла в порядке запуска и сторож; сам пульт — нет."""
    from neurobot_deploy import render
    plan = [(c, u) for c, u in cfg.plan() if c != "ui"]
    if "neurobot-watchdog.service" in render.enable_list(cfg):
        plan.append(("watchdog", "neurobot-watchdog.service"))
    return plan


def dds_environ(env: dict) -> Optional[str]:
    """RMW — Fast DDS (по умолчанию в Foxy и Jazzy), своего профиля в env-файле нет — профиль «только UDP» шлюза.
    Возвращает поставленный путь или None."""
    rmw = env.get("RMW_IMPLEMENTATION") or "rmw_fastrtps_cpp"
    if rmw.startswith("rmw_fastrtps") and "FASTRTPS_DEFAULT_PROFILES_FILE" not in env:
        env["FASTRTPS_DEFAULT_PROFILES_FILE"] = FASTDDS_UDP
        return FASTDDS_UDP
    return None


def main(argv: Optional[List[str]] = None) -> int:
    from neurobot_deploy import config
    p = argparse.ArgumentParser(prog="python -m neurobot.ui live", description="живой шлюз пульта (robot / двойник)")
    p.add_argument("--env", help="env-файл (по умолчанию $NEUROBOT_ENV или /etc/neurobot/neurobot.env)")
    p.add_argument("--port", type=int, help="порт на 127.0.0.1 (по умолчанию NEUROBOT_UI_PORT)")
    p.add_argument("--state-dir", help="каталог токена и аудита (по умолчанию $STATE_DIRECTORY юнита)")
    a, extra = p.parse_known_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    if extra:
        log.warning("аргументы %s не действуют: аргументов ROS пульт не принимает (ремап сделал бы издателя стопа "
                    "писателем скорости)", " ".join(extra))
    try:
        cfg = config.load(a.env)
        port = a.port or cfg.port("NEUROBOT_UI_PORT")
        doctor_s = cfg.num("NEUROBOT_UI_DOCTOR_S")
    except config.EnvError as e:
        log.error("env: %s", e)
        return 2
    state_dir = a.state_dir or os.environ.get("STATE_DIRECTORY", "").split(":")[0] or cfg.get("NEUROBOT_UI_STATE_DIR")
    bad = server.verify_vendor()
    if bad:
        log.error("web/vendor не совпадает с SHA256SUMS: %s", "; ".join(bad))
        return 2
    os.environ.update(cfg.ros_environ())       # DDS — из env-файла, как у CLI
    if dds_environ(os.environ):
        log.info("Fast DDS: шлюз — только UDP (%s): общая память между пользователями данных не пропускает",
                 FASTDDS_UDP)
    hub = live.Hub()
    lv = live.Live(hub, not_robot=cfg.get("NEUROBOT_UI_NOT_ROBOT").split())
    try:
        auth = gateway.Auth(state_dir)
    except OSError as e:
        log.error("каталог состояния %s: %s", state_dir, e)
        return 2
    stop = threading.Event()
    rclpy.init(args=[])      # без аргументов ROS: ремапов издателей не бывает
    # свои обработчики поверх rclpy: на Jazzy rclpy по сигналу сам закрывает контекст, и выход был бы с ошибкой
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    node = None
    srv = None
    notifier = Notifier()
    # подпроцессы (systemctl show, doctor) не должны слать systemd уведомления от имени юнита (NotifyAccess=main)
    os.environ.pop("NOTIFY_SOCKET", None)
    try:
        node = GatewayNode(cfg, lv)
        run = "%s · домен %s" % (socket.gethostname(), cfg.ros_environ().get("ROS_DOMAIN_ID", "0"))
        doctor = gateway.Doctor([os.path.join(cfg.get("NEUROBOT_PREFIX"), "bin", "neurobot"), "doctor", "--json"],
                                doctor_s)
        gw = gateway.Gateway(hub, lv, node, auth, cfg.get("NEUROBOT_UI_SOURCE"), run,
                             audit=gateway.Audit(os.path.join(state_dir, "audit.jsonl")), doctor=doctor,
                             bundle=cfg.get("NEUROBOT_UI_BUNDLE") or None)
        srv = gateway.make_server(gw, port)
        threading.Thread(target=srv.serve_forever, name="ui-http", daemon=True).start()
        Poller(cfg, lv, stop, unit_plan(cfg)).start()

        def tick() -> None:
            lv.tick(time.time())
            notifier.watchdog()
        node.create_timer(TICK_S, tick)
        notifier.ready()
        log.info("пульт: http://127.0.0.1:%d/ (источник %s, узел %s, пишет только в %s)", port, gw.source, node.name,
                 ", ".join(live.PUBLISHERS))
        ex = SingleThreadedExecutor()
        ex.add_node(node)
        while not stop.is_set() and rclpy.ok():
            ex.spin_once(timeout_sec=0.1)
    except (OSError, ValueError) as e:
        log.error("%s", e)
        return 2
    finally:
        notifier.stopping()
        stop.set()
        if srv is not None:
            gw.stopping.set()
            srv.shutdown()
            srv.server_close()
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0
