"""Граф ROS 2 через rclpy (Foxy и Jazzy): издатели и подписчики тем, наблюдение для doctor, явный ноль, защёлка.

rclpy импортируется только здесь и только при вызове (cli заранее подгружает окружение ROS); свой Context на
каждый вызов, чтобы не задевать глобальное состояние (у сторожа — один постоянный, TopicWatch). Используются только
API, которые есть в Foxy:
get_publishers_info_by_topic, get_subscriptions_info_by_topic, get_topic_names_and_types, get_node_names,
count_subscribers, create_subscription(raw=True), SingleThreadedExecutor(context=).
"""
from __future__ import annotations

import importlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from .config import Config

# Состояние safety_gate слушаем не дольше: по контракту оно приходит 5 Гц.
STATE_WAIT_S = 2.0
# Имя узла приходит в граф DDS позже его конечных точек (ros_discovery_info): только что запущенный узел какое-то время
# виден как _NODE_NAME_UNKNOWN_ — такой снимок досматриваем, иначе свой узел сошёл бы за чужого писателя.
UNKNOWN = "_NODE_NAME_UNKNOWN_"
UNRESOLVED_WAIT_S = 2.0


class RosUnavailable(RuntimeError):
    pass


def _rclpy():
    try:
        import rclpy
        import rclpy.executors  # noqa: F401 — подмодули: дальше они нужны атрибутами rclpy.executors и rclpy.qos
        import rclpy.qos  # noqa: F401
    except ImportError as e:
        raise RosUnavailable("rclpy недоступен ({}); проверьте NEUROBOT_ROS_SETUP".format(e))
    return rclpy


def msg_class(type_name: str):
    """'geometry_msgs/msg/Twist' -> класс сообщения."""
    parts = type_name.split("/")
    if len(parts) != 3 or parts[1] != "msg":
        raise ValueError("тип сообщения: ожидалось пакет/msg/Имя, получено {!r}".format(type_name))
    return getattr(importlib.import_module("{}.msg".format(parts[0])), parts[2])


@contextmanager
def session(cfg: Config, role: str) -> Iterator:
    os.environ.update(cfg.ros_environ())
    rclpy = _rclpy()
    ctx = rclpy.Context()
    # без журнала rcl в файл: всё нужное уходит в stderr; без HOME и ROS_LOG_DIR (systemd-run, cron) rcl_logging_spdlog
    # не находит каталог и init падает [опыт, ВМ: doctor из systemd-run — ros_unavailable]; флаг есть и в Foxy
    rclpy.init(args=["--ros-args", "--disable-external-lib-logs"], context=ctx)
    node = rclpy.create_node("neurobot_{}_{}".format(role, os.getpid()), context=ctx)
    try:
        yield node
    finally:
        node.destroy_node()
        rclpy.shutdown(context=ctx)


def _spin(node, seconds: float, until: Callable[[], bool] = lambda: False) -> None:
    ex = _rclpy().executors.SingleThreadedExecutor(context=node.context)
    ex.add_node(node)
    end = time.monotonic() + seconds
    while time.monotonic() < end and not until():
        ex.spin_once(timeout_sec=min(0.1, max(0.0, end - time.monotonic())))
    ex.remove_node(node)


def _endpoints(node, topic: str) -> Dict[str, List[str]]:
    return {"pub": [i.node_name for i in node.get_publishers_info_by_topic(topic)],
            "sub": [i.node_name for i in node.get_subscriptions_info_by_topic(topic)]}


def _better(new: List[str], cur: List[str]) -> bool:
    return len(new) > len(cur) or (len(new) == len(cur) and new.count(UNKNOWN) < cur.count(UNKNOWN))


def _settle(node, topics: List[str], seconds: float) -> Dict[str, Dict[str, List[str]]]:
    """{тема: {"pub": [...], "sub": [...]}} — узел на каждую конечную точку. Граф DDS собирается со временем: смотрим
    seconds с и берём самый полный снимок каждой стороны; если в нём остались узлы без имени — ещё до
    UNRESOLVED_WAIT_S, пока имена не придут."""
    out: Dict[str, Dict[str, List[str]]] = {t: {"pub": [], "sub": []} for t in topics}
    end = time.monotonic() + seconds
    while True:
        for t in topics:
            for side, names in _endpoints(node, t).items():
                if _better(names, out[t][side]):
                    out[t][side] = names
        unresolved = any(UNKNOWN in names for ends in out.values() for names in ends.values())
        if time.monotonic() >= end + (UNRESOLVED_WAIT_S if unresolved else 0.0):
            return out
        time.sleep(0.25)


def graph(cfg: Config, topics: List[str]) -> Dict[str, Dict[str, List[str]]]:
    """Конечные точки тем для precheck: смотрим NEUROBOT_WRITER_CHECK_S (_settle). 0 — граф не смотреть."""
    wait = cfg.num("NEUROBOT_WRITER_CHECK_S")
    if wait <= 0:
        return {t: {"pub": [], "sub": []} for t in topics}
    with session(cfg, "precheck") as node:
        return _settle(node, topics, wait)


def wait_node(cfg: Config, name: str, topics: List[str], timeout: float) -> Optional[Dict[str, Dict[str, List[str]]]]:
    """Ждать узел name в графе до timeout с, затем снять конечные точки тем (_settle, NEUROBOT_WRITER_CHECK_S);
    None — узел не появился."""
    with session(cfg, "wait") as node:
        end = time.monotonic() + timeout
        while name not in node.get_node_names():
            if time.monotonic() >= end:
                return None
            time.sleep(0.2)
        return _settle(node, topics, cfg.num("NEUROBOT_WRITER_CHECK_S"))


def _scan_stats(stats: dict, msg) -> None:
    """Копит по лучам долю негодных значений (NaN, inf, вне [range_min, range_max] — коды ошибок URG, N14)."""
    lo, hi = msg.range_min, msg.range_max
    bad = [not lo <= r <= hi for r in msg.ranges]
    geom = (msg.angle_min, msg.angle_increment, len(bad))
    if stats.get("geom") != geom:
        stats.update(geom=geom, scans=0, bad=[0] * len(bad))
    stats["scans"] += 1
    stats["bad"] = [a + b for a, b in zip(stats["bad"], bad)]
    stats["frame_id"] = msg.header.frame_id


def _json(msg) -> dict:
    try:
        data = json.loads(msg.data)
    except ValueError:
        return {"state": "bad_json"}
    return data if isinstance(data, dict) else {"state": "bad_json"}


def observe(cfg: Config, seconds: float) -> Dict[str, dict]:
    """Слушать seconds секунд темы, по которым doctor судит о прогрессе: состояние safety_gate, состояние исполнителя,
    здоровье распознавания, /scan (доля негодных лучей, frame_id) и датчики NEUROBOT_SENSORS (без разбора сообщения).
    По каждой: число сообщений, частота, возраст последнего, типы в графе, издатели и подписчики; для JSON-тем —
    последнее сообщение. Плюс конечные точки цепочки движения: /cmd_vel, /neurobot/cmd_vel_req, вход базы, входы
    twist_mux главнее /cmd_vel и геймпад."""
    rclpy = _rclpy()
    String = msg_class("std_msgs/msg/String")
    listen: Dict[str, Tuple[str, object, bool]] = {
        "safety": (cfg.get("NEUROBOT_SAFETY_STATE_TOPIC"), String, False),
        "motion": (cfg.get("NEUROBOT_MOTION_STATE_TOPIC"), String, False),
        "health": (cfg.get("NEUROBOT_HEALTH_TOPIC"), String, False),
        "scan": (cfg.get("NEUROBOT_SCAN_TOPIC"), msg_class("sensor_msgs/msg/LaserScan"), False)}
    for topic, type_name, _ in cfg.sensors():
        listen["sensor:" + topic] = (topic, msg_class(type_name), True)
    seen: Dict[str, List[float]] = {k: [] for k in listen}
    last: Dict[str, object] = {}
    scan: dict = {}
    with session(cfg, "doctor") as node:
        for key, (topic, cls, raw) in listen.items():
            def cb(msg, key=key):
                seen[key].append(time.monotonic())
                last[key] = msg
                if key == "scan":
                    _scan_stats(scan, msg)
            node.create_subscription(cls, topic, cb, rclpy.qos.qos_profile_sensor_data, raw=raw)
        t0 = time.monotonic()
        _spin(node, seconds)
        t1 = time.monotonic()
        types = dict(node.get_topic_names_and_types())
        chain = {k: t for k, t in (("cmd_vel", cfg.get("NEUROBOT_CMD_VEL")), ("cmd_vel_req", cfg.get(
            "NEUROBOT_CMD_VEL_REQ")), ("base", cfg.get("NEUROBOT_BASE_CMD_TOPIC"))) if t}
        inputs = {k: cfg.get("NEUROBOT_{}_TOPICS".format(k.upper())).split() for k in ("override", "joy")}
        ends = _settle(node, [t for t, _, _ in listen.values()] + list(chain.values()) + inputs["override"]
                      + inputs["joy"], 0.0)
        out: Dict[str, dict] = {}
        for key, (topic, _, _) in listen.items():
            out[key] = dict(ends[topic], topic=topic, count=len(seen[key]), rate_hz=len(seen[key]) / (t1 - t0),
                            age_s=t1 - seen[key][-1] if seen[key] else None, types=types.get(topic, []))
        for key in ("safety", "motion", "health"):
            if key in last:
                out[key]["last"] = _json(last[key])
        if scan:
            out["scan"].update(angle_min=scan["geom"][0], angle_increment=scan["geom"][1], frame_id=scan["frame_id"],
                               bad_frac=[b / scan["scans"] for b in scan["bad"]])
        for key, topic in chain.items():
            out[key] = dict(ends[topic], topic=topic, types=types.get(topic, []))
        for key, topics in inputs.items():
            out[key] = {t: ends[t] for t in topics}
    return out


def publish_zero(cfg: Config, count: int = 5, period: float = 0.05, wait: float = 0.5) -> int:
    """Явный ноль в /cmd_vel (ExecStopPost safety_gate, S2): ждём подписчика до wait с, шлём count нулей; число
    подписчиков (0 — ноль никуда не ушёл)."""
    topic, type_name = cfg.get("NEUROBOT_CMD_VEL"), cfg.get("NEUROBOT_CMD_VEL_TYPE")
    cls = msg_class(type_name)
    with session(cfg, "zero") as node:
        pub = node.create_publisher(cls, topic, 10)
        end = time.monotonic() + wait
        while node.count_subscribers(topic) == 0 and time.monotonic() < end:
            time.sleep(0.02)
        subs = node.count_subscribers(topic)
        for _ in range(count):
            msg = cls()
            if hasattr(msg, "header"):
                msg.header.stamp = node.get_clock().now().to_msg()
            pub.publish(msg)
            time.sleep(period)
    return subs


def estop_message(cmd: str, reason: str) -> dict:
    """Команда защёлки /neurobot/estop (std_msgs/String, JSON); id — чтобы safety_gate не применял повтор дважды."""
    return {"cmd": cmd, "by": "human", "reason": reason, "id": uuid.uuid4().hex, "t": round(time.time(), 3)}


def safety_state(cfg: Config, timeout: float = STATE_WAIT_S) -> Optional[dict]:
    """Последнее /neurobot/safety_state за timeout с или None."""
    return wait_safety_state(cfg, lambda _s: True, timeout)


def wait_safety_state(cfg: Config, done: Callable[[dict], bool], timeout: float) -> Optional[dict]:
    """Слушать /neurobot/safety_state до состояния, для которого done(state) истинно, или до timeout с; последнее
    состояние или None."""
    got: List[dict] = []
    with session(cfg, "estop") as node:
        node.create_subscription(msg_class("std_msgs/msg/String"), cfg.get("NEUROBOT_SAFETY_STATE_TOPIC"),
                                 lambda m: got.append(_json(m)), _rclpy().qos.qos_profile_sensor_data)
        _spin(node, timeout, until=lambda: bool(got) and done(got[-1]))
    return got[-1] if got else None


def send_estop(cfg: Config, message: dict, done: Callable[[dict], bool], timeout: float = STATE_WAIT_S,
               repeat: int = 3) -> Tuple[int, Optional[dict]]:
    """Отправить команду защёлки (repeat раз, одна и та же — с одним id) и ждать состояние, для которого done(state)
    истинно, до timeout с. (подписчиков /neurobot/estop, последнее состояние или None).
    Издатель reliable + transient_local: совместим с любой подпиской safety_gate (volatile или transient_local)."""
    qos = _rclpy().qos
    String = msg_class("std_msgs/msg/String")
    states: List[dict] = []
    with session(cfg, "estop") as node:
        topic = cfg.get("NEUROBOT_ESTOP_TOPIC")
        pub = node.create_publisher(String, topic, qos.QoSProfile(
            depth=1, reliability=qos.ReliabilityPolicy.RELIABLE, durability=qos.DurabilityPolicy.TRANSIENT_LOCAL))
        node.create_subscription(String, cfg.get("NEUROBOT_SAFETY_STATE_TOPIC"), lambda m: states.append(_json(m)),
                                 qos.qos_profile_sensor_data)
        end = time.monotonic() + 1.0
        while node.count_subscribers(topic) == 0 and time.monotonic() < end:
            time.sleep(0.02)
        subs = node.count_subscribers(topic)
        for _ in range(repeat):
            pub.publish(String(data=json.dumps(message, ensure_ascii=False)))
            _spin(node, 0.05)
        _spin(node, timeout, until=lambda: bool(states) and done(states[-1]))
    return subs, (states[-1] if states else None)


class TopicWatch:
    """Постоянный узел сторожа: возраст последнего сообщения тем. Подписка raw (кадры камеры не разбираются), QoS
    сенсоров; тип — из графа: тема, которой в графе ещё нет, подписывается на следующем spin. Пока сообщений не было,
    возраст считается от подписки (а до неё — от запуска сторожа)."""

    def __init__(self, cfg: Config, topics: List[str]):
        os.environ.update(cfg.ros_environ())
        self.rclpy = _rclpy()
        self.ctx = self.rclpy.Context()
        self.rclpy.init(args=["--ros-args", "--disable-external-lib-logs"], context=self.ctx)
        self.node = self.rclpy.create_node("neurobot_watchdog_{}".format(os.getpid()), context=self.ctx)
        self.ex = self.rclpy.executors.SingleThreadedExecutor(context=self.ctx)
        self.ex.add_node(self.node)
        self.born = time.monotonic()
        self.last: Dict[str, Optional[float]] = dict.fromkeys(topics)
        self.since: Dict[str, float] = {}
        self.subs: Dict[str, object] = {}

    def _subscribe(self) -> None:
        missing = [t for t in self.last if t not in self.subs]
        if not missing:
            return
        types = dict(self.node.get_topic_names_and_types())
        for t in missing:
            if types.get(t):
                self.subs[t] = self.node.create_subscription(
                    msg_class(types[t][0]), t, lambda _m, t=t: self.last.__setitem__(t, time.monotonic()),
                    self.rclpy.qos.qos_profile_sensor_data, raw=True)
                self.since[t] = time.monotonic()

    def spin(self, seconds: float) -> None:
        self._subscribe()
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.ex.spin_once(timeout_sec=min(0.1, max(0.0, end - time.monotonic())))

    def ages(self) -> Dict[str, float]:
        """{тема: с без сообщений}: от последнего, а пока их не было — от подписки или запуска сторожа."""
        now = time.monotonic()
        return {t: now - (last if last is not None else self.since.get(t, self.born)) for t, last in self.last.items()}

    def close(self) -> None:
        self.ex.remove_node(self.node)
        self.node.destroy_node()
        self.rclpy.shutdown(context=self.ctx)


def wait_topic(cfg: Config, topic: str, timeout: float) -> Optional[float]:
    """Первое сообщение темы любого типа (тип — из графа); время ожидания или None."""
    t0 = time.monotonic()
    got: List[float] = []
    with session(cfg, "wait") as node:
        types: List[str] = []
        while not types and time.monotonic() - t0 < timeout:
            types = dict(node.get_topic_names_and_types()).get(topic, [])
            if not types:
                time.sleep(0.2)
        if not types:
            return None
        node.create_subscription(msg_class(types[0]), topic, lambda _m: got.append(time.monotonic()),
                                 _rclpy().qos.qos_profile_sensor_data, raw=True)
        _spin(node, timeout - (time.monotonic() - t0), until=lambda: bool(got))
    return got[0] - t0 if got else None
