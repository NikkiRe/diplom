"""Наблюдатель стенда neurobot — узел ROS 2 (на стенде Jazzy; код совместим с Foxy и Python 3.8).

Подписки: /cmd_vel (тип — --cmd-vel-type; единственный писатель — safety_gate) и дополнительные темы скоростей
(--extra-cmd, напр. заявка исполнителя /neurobot/cmd_vel_req и выход twist_mux /diff_cont/cmd_vel_unstamped),
/odom, /ground_truth/pose, /neurobot/gesture, /neurobot/perception_health. Пишет компактные события
(record.py) в JSONL с меткой времени приёма (time.time() и CLOCK_MONOTONIC ВМ sim). Файл записи — один на
прогон: существующий файл — ошибка, дописать можно только явно (--append, рестарт наблюдателя в том же
прогоне). Раз в 2 с сверяет типы издателей тем скоростей с подпиской: при расхождении DDS молча не соединяет
издателя с подпиской, и команд в записи не было бы — ошибка в stderr, событие в записи и код выхода 3. При
остановке (SIGINT/SIGTERM) закрывает серии команд и, если заданы --truth и --anchor, строит разбор
(analyze.py): --summary и --per-gesture. При старте проверяется, что их можно записать и что флаги разбора
(--summary, --per-gesture, --probe) не заданы без --truth и --anchor — иначе их молча не было бы.

Часы — стенные, не use_sim_time: источник жестов, perception, демо и страж живут по стенным часам
(time.time, sleep, таймеры), а задержку видит человек тоже в стенном времени. Сим. время из
header.stamp используется только для скоростей по истинной позе и для оценки RTF (событие rtf), по
которой разбор исключает жесты, пришедшиеся на RTF < 0,95.

  python3 -m neurobot.sim.gestures.observer --out events.jsonl --truth truth.jsonl --anchor anchor.json \\
      --summary summary.json --per-gesture gestures.jsonl [--cmd-vel-type twist|twist_stamped] \\
      [--extra-cmd /neurobot/cmd_vel_req] [--world-sdf world.sdf | --walls walls.json] [--probe probe.jsonl]
"""
from __future__ import annotations

import argparse
import math
import os
import signal
import sys
import time
from dataclasses import asdict

import rclpy
from geometry_msgs.msg import PoseStamped, Twist, TwistStamped
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from rclpy.utilities import remove_ros_args
from std_msgs.msg import String

from .analyze import add_config_args, config_from_args, expected_maneuver, run, write_results
from .clock import boot_id, mono
from .record import Recorder
from .timeline import dumps, read_jsonl
from .walls import FOOTPRINT_TB3_WAFFLE_PI, RADIUS_NEUROCHIP_BASE, load_walls, walls_from_sdf

CMD_TYPES = {"twist": Twist, "twist_stamped": TwistStamped}
CMD_TYPE_NAMES = {"twist": "geometry_msgs/msg/Twist", "twist_stamped": "geometry_msgs/msg/TwistStamped"}
EXIT_TYPE_MISMATCH = 3


def _stamp(header) -> float:
    return header.stamp.sec + header.stamp.nanosec * 1e-9


def _yaw(q) -> float:
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))


def _cmd_spec(s: str) -> tuple:
    topic, _, kind = s.partition(":")
    kind = kind or "twist"
    if kind not in CMD_TYPES:
        raise argparse.ArgumentTypeError("тип %r: twist | twist_stamped" % kind)
    return topic, kind


class Observer(Node):
    def __init__(self, a: argparse.Namespace, recorder: Recorder):
        super().__init__(a.node_name)
        self.rec = recorder
        self.cmd_specs = [(a.cmd_topic, a.cmd_vel_type)] + a.extra_cmd
        reliable = QoSProfile(depth=50, history=HistoryPolicy.KEEP_LAST, reliability=ReliabilityPolicy.RELIABLE)
        for topic, kind in self.cmd_specs:
            self.create_subscription(CMD_TYPES[kind], topic, self._cmd_cb(topic, kind == "twist_stamped"), reliable)
        self.create_subscription(Odometry, a.odom_topic, self._on_odom, qos_profile_sensor_data)
        self.create_subscription(PoseStamped, a.gt_topic, self._on_gt, qos_profile_sensor_data)
        self.create_subscription(String, a.gesture_topic, self._json_cb("gesture"), reliable)
        self.create_subscription(String, a.health_topic, self._json_cb("health"), reliable)
        self.create_timer(0.2, self._tick)
        self.create_timer(2.0, self.check_types)

    def _cmd_cb(self, topic: str, stamped: bool):
        def cb(msg):
            t, m = time.time(), mono()
            tw = msg.twist if stamped else msg
            self.rec.on_cmd(topic, t, m, tw.linear.x, tw.angular.z)
        return cb

    def _json_cb(self, kind: str):
        def cb(msg):
            self.rec.on_json(kind, time.time(), mono(), msg.data)
        return cb

    def _on_odom(self, msg) -> None:
        t, m = time.time(), mono()
        tw = msg.twist.twist
        self.rec.on_odom(t, m, math.hypot(tw.linear.x, tw.linear.y), tw.angular.z, _stamp(msg.header))

    def _on_gt(self, msg) -> None:
        t, m = time.time(), mono()
        p = msg.pose
        self.rec.on_gt(t, m, p.position.x, p.position.y, _yaw(p.orientation), _stamp(msg.header))

    def _tick(self) -> None:
        self.rec.tick(mono())

    def check_types(self) -> None:
        for topic, kind in self.cmd_specs:
            theirs = [info.topic_type for info in self.get_publishers_info_by_topic(topic)]
            if self.rec.on_publishers(topic, CMD_TYPE_NAMES[kind], theirs, time.time(), mono()):
                print("observer: ОШИБКА — в %s публикуют %s, а подписка %s: команд в записи не будет "
                      "(--cmd-vel-type / --extra-cmd ТЕМА:ТИП)" % (topic, ", ".join(sorted(set(theirs))),
                                                                 CMD_TYPE_NAMES[kind]), file=sys.stderr)


def _check_writable(flag: str, path: str) -> None:
    """Файл, который наблюдатель запишет в конце прогона, записать можно: каталог есть, это не каталог и права на
    запись есть (иначе ValueError) — ошибка до записи, а не после многочасового прогона."""
    d = os.path.dirname(os.path.abspath(path))
    if os.path.isdir(path) or not os.path.isdir(d) or not os.access(path if os.path.exists(path) else d, os.W_OK):
        raise ValueError("%s %s: нет каталога, это каталог или нет прав на запись" % (flag, path))


def _floats(s: str) -> tuple:
    v = tuple(float(x) for x in s.split(","))
    if len(v) != 4:
        raise argparse.ArgumentTypeError("нужно 4 числа: x_min,x_max,y_min,y_max")
    return v


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m neurobot.sim.gestures.observer", description=__doc__.split("\n")[0])
    p.add_argument("--out", required=True, help="события JSONL — новый файл на прогон")
    p.add_argument("--append", action="store_true",
                   help="дописать существующий --out (рестарт наблюдателя в том же прогоне)")
    p.add_argument("--node-name", default="neurobot_gesture_observer", help="имя узла ROS")
    p.add_argument("--cmd-vel-type", choices=sorted(CMD_TYPES), default="twist",
                   help="тип темы писателя скоростей (--cmd-topic): Twist (Foxy) или TwistStamped")
    p.add_argument("--extra-cmd", type=_cmd_spec, action="append", default=[], metavar="TOPIC[:TYPE]",
                   help="ещё тема скоростей (напр. /neurobot/cmd_vel_req); тип по умолчанию twist")
    p.add_argument("--odom-topic", default="/odom", help="одометрия колёс (Odometry)")
    p.add_argument("--gt-topic", default="/ground_truth/pose", help="истинная поза стенда (PoseStamped)")
    p.add_argument("--gesture-topic", default="/neurobot/gesture", help="сообщения perception о жестах (String JSON)")
    p.add_argument("--health-topic", default="/neurobot/perception_health", help="здоровье perception (String JSON)")
    p.add_argument("--v-thr", type=float, default=0.05, help="|v|, м/с: робот едет")
    p.add_argument("--w-thr", type=float, default=0.1, help="|ω|, рад/с: робот поворачивает")
    walls = p.add_mutually_exclusive_group()
    walls.add_argument("--walls", help="JSON-список препятствий {name, cx, cy, yaw, sx, sy} или {name, cx, cy, r}")
    walls.add_argument("--world-sdf", help="SDF мира: препятствия — модели с именем по --wall-regex")
    p.add_argument("--wall-regex", default="wall", help="имена моделей-препятствий мира (box и cylinder)")
    fp = p.add_mutually_exclusive_group()
    fp.add_argument("--footprint", type=_floats, default=FOOTPRINT_TB3_WAFFLE_PI,
                    help="контур модели стенда x_min,x_max,y_min,y_max, м (по умолчанию TB3 waffle_pi)")
    fp.add_argument("--footprint-radius", type=float,
                    help="круглый контур, радиус, м (neurochip_base стенда: %.3f)" % RADIUS_NEUROCHIP_BASE)
    p.add_argument("--touch-m", type=float, default=0.02, help="касание: зазор до стены ≤, м")
    p.add_argument("--truth", help="truth.jsonl gesturegen — для разбора при остановке")
    p.add_argument("--anchor", help="якорь таймлайна (создаёт FakeYolo)")
    p.add_argument("--probe", help="журнал подставных модулей (probe.py; есть, только если у perception задан "
                                   "NEUROBOT_FAKES_PROBE)")
    p.add_argument("--summary", help="сводка разбора JSON")
    p.add_argument("--per-gesture", help="разбор по сегментам JSONL")
    add_config_args(p)
    return p


def main(argv=None) -> int:
    full = sys.argv if argv is None else ["observer"] + list(argv)
    a = build_parser().parse_args(remove_ros_args(full)[1:])
    timeline_sha256 = None
    try:
        walls = load_walls(a.walls) if a.walls else walls_from_sdf(a.world_sdf, a.wall_regex) if a.world_sdf else []
        cfg = config_from_args(a)
        if (a.summary or a.per_gesture or a.probe) and not (a.truth and a.anchor):
            raise ValueError("--summary, --per-gesture и --probe — для разбора при остановке: нужны --truth и --anchor")
        for flag, path in (("--summary", a.summary), ("--per-gesture", a.per_gesture)):
            if path:
                _check_writable(flag, path)
        if a.truth:
            header, segments = read_jsonl(a.truth)
            timeline_sha256 = header.get("timeline_sha256")
            for s in segments:
                if s["kind"] == "gesture":
                    expected_maneuver(cfg.reflex, s)
    except (OSError, ValueError) as e:
        print("observer: %s" % e, file=sys.stderr)
        return 2
    try:
        out = open(a.out, "a" if a.append else "x", encoding="utf-8", buffering=1)
    except FileExistsError:
        print("observer: %s уже есть — новый файл на прогон или --append" % a.out, file=sys.stderr)
        return 2
    except OSError as e:
        print("observer: --out: %s" % e, file=sys.stderr)
        return 2
    out.write(dumps({"header": {"observer": "neurobot.sim.gestures.observer", "pid": os.getpid(), "t": time.time(),
                                "m": mono(), "boot_id": boot_id(), "timeline_sha256": timeline_sha256,
                                "args": vars(a), "walls": [asdict(w) for w in walls]}}) + "\n")
    recorder = Recorder(lambda e: out.write(dumps(e) + "\n"), [a.cmd_topic] + [t for t, _ in a.extra_cmd], walls,
                        a.footprint if a.footprint_radius is None else a.footprint_radius, a.v_thr, a.w_thr, a.touch_m)
    rclpy.init(args=full)
    # Свои обработчики — до создания узла: SIGTERM посреди старта (systemd, обвязка прогона) иначе застал бы
    # обработчик rclpy, который гасит контекст под создаваемыми подписками.
    stop = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.append(True))
    node = Observer(a, recorder)
    print("observer: запись в %s" % a.out, file=sys.stderr)
    try:
        while not stop and rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.2)
        node.check_types()
    finally:
        recorder.close(time.time(), mono())
        out.close()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    rc = EXIT_TYPE_MISMATCH if recorder.mismatched else 0
    if a.truth and a.anchor:
        if not os.path.exists(a.anchor):
            print("observer: якоря %s нет (детектор ни разу не вызван) — разбор не построен" % a.anchor,
                  file=sys.stderr)
            return 1
        probe = a.probe
        if probe and not os.path.exists(probe):
            print("observer: журнала %s нет (у perception не задан NEUROBOT_FAKES_PROBE?) — разбор без него" % probe,
                  file=sys.stderr)
            probe = None
        try:
            per, summary = run(a.out, a.truth, a.anchor, probe, cfg)
            write_results(per, summary, a.summary, a.per_gesture)
        except (OSError, ValueError) as e:
            print("observer: разбор не построен или не записан: %s; запись %s цела — разбор офлайн (analyze)"
                  % (e, a.out), file=sys.stderr)
            return 2
        print("observer: разбор — %s" % (a.summary or "stdout"), file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
