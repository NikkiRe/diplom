#!/usr/bin/env python3
"""(в ВМ sim) Пробы двойника для check.sh: JSON в stdout, журнал в stderr. Перед пробой движения робот ставится в
позу спавна мира (сервис /world/default/set_pose); время — симуляции (/clock), если не сказано иное. В каждом ответе —
probe_rtf за всю пробу: времена валидны при RTF ≈ 1 (twist_mux, как на роботе, считает таймауты по стенным часам, а
diff_drive_controller — по часам симуляции).
  python3 twin_probe.py sensors --world lab [--seconds 20]
      темы: типы и частоты; RTF без подписчиков камеры и с ними (по часам узла и монотонным); дерево TF; лидар:
      стабильные блоки NaN и ближайшее по курсу; IMU в покое; глубина: профиль по курсу в кадре base_link; истина
  python3 twin_probe.py mux
      приоритеты twist_mux: cmd_vel + cmd_vel_joy → joy; затем один cmd_vel → он; cmd_vel + cmd_vel_tracker → tracker
  python3 twin_probe.py silence --world lab [--v 0.2] [--drive-s 2] [--wait-s 3]
      v по /cmd_vel 10 Гц, затем тишина: когда контроллер начал тормозить и обнулил выход, когда встала база (истина),
      выбег; касания и одометрия против истины за время пробы
  python3 twin_probe.py odom --world lab
      маршрут «вперёд — поворот — вперёд — поворот»: одометрия против истины (в системе позы старта)
  python3 twin_probe.py ramp --world ramp
      въезд на пандус с пола; стоянка на склоне и подъём: наклон по /camera/imu, /imu/data (madgwick) и по истине
  python3 twin_probe.py truth --world lab
      истина при сценариях сервисами мира: set_pose человека перед роботом, create и remove низкого «кабеля»; зазор,
      касания (в том числе ролика), RTF в /twin/truth
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twin_config as cfg  # noqa: E402
from truth import euler, rtf  # noqa: E402

PUB_HZ = 10.0
# Оптический кадр камеры относительно camera_link (rpy −π/2, 0, −π/2): x_opt = −y, y_opt = −z, z_opt = x.
R_LINK_OPT = np.array([[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0], [0.0, -1.0, 0.0]])


# --- чистые функции (тесты на Mac) ---

def nan_blocks(flags: Sequence[bool]) -> List[Tuple[int, int]]:
    """Непрерывные блоки True: [(первый, последний)]."""
    out: List[List[int]] = []
    for i, f in enumerate(flags):
        if not f:
            continue
        if out and out[-1][1] == i - 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return [(a, b) for a, b in out]


def merge_gaps(blocks: List[Tuple[int, int]], gap: int) -> List[Tuple[int, int]]:
    """Склеить блоки, между которыми не больше gap лучей."""
    out: List[List[int]] = []
    for a, b in blocks:
        if out and a - out[-1][1] - 1 <= gap:
            out[-1][1] = b
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def blind_summary(nan_frac: Sequence[float], stable: float = 0.9) -> dict:
    """Лучи, которые NaN не меньше чем в доле stable сканов, блоками; главный блок — самый длинный после склейки
    разрывов в 1 луч; с ним — лучи-«дыры» внутри и сравнение с блоком скана робота (R7b §3.3)."""
    flags = [f >= stable for f in nan_frac]
    blocks = nan_blocks(flags)
    merged = merge_gaps(blocks, 1)
    main = max(merged, key=lambda b: b[1] - b[0]) if merged else None
    out = {"stable_frac": stable, "blocks": [[a, b, round(cfg.scan_angle(a), 2), round(cfg.scan_angle(b), 2)]
                                             for a, b in blocks]}
    if main:
        a, b = main
        robot_a = round((cfg.ROBOT_BLIND_DEG[0] - cfg.scan_angle(0)) / (cfg.scan_angle(1) - cfg.scan_angle(0)))
        out["main"] = {"first": a, "last": b, "n": b - a + 1, "deg": [round(cfg.scan_angle(a), 2),
                                                                     round(cfg.scan_angle(b), 2)],
                       "holes": [i for i in range(a, b + 1) if not flags[i]],
                       "robot_first": robot_a, "robot_last": robot_a + cfg.ROBOT_BLIND_N - 1}
    return out


def rate(stamps: Sequence[float]) -> Optional[float]:
    """Частота по меткам времени: (n − 1) / (последняя − первая)."""
    if len(stamps) < 2 or stamps[-1] <= stamps[0]:
        return None
    return (len(stamps) - 1) / (stamps[-1] - stamps[0])


def tilt_accel(a_opt: Sequence[float]) -> float:
    """Наклон носа вверх, градусы, по показаниям акселерометра в оптическом кадре (вектор «вверх» в покое)."""
    x, _, z = R_LINK_OPT @ np.asarray(a_opt, dtype=float)
    return math.degrees(math.atan2(x, z))


def quat_matrix(x: float, y: float, z: float, w: float) -> np.ndarray:
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def tilt_orientation(q_world_opt: Sequence[float]) -> float:
    """Наклон носа вверх, градусы, по ориентации оптического кадра IMU в ENU (x, y, z, w)."""
    r_world_link = quat_matrix(*q_world_opt) @ R_LINK_OPT.T
    return math.degrees(math.asin(max(-1.0, min(1.0, r_world_link[2, 0]))))


def depth_points(depth_mm: np.ndarray, k: Sequence[float], rot: np.ndarray, trans: Sequence[float]) -> np.ndarray:
    """Точки глубины (16UC1, мм; 0 — нет данных) в кадре base_link: K — 3×3 построчно, rot/trans — поза
    оптического кадра в base_link. Возвращает N×3."""
    z = depth_mm.astype(np.float64) / 1000.0
    v, u = np.nonzero(z > 0)
    zz = z[v, u]
    pts = np.stack([(u - k[2]) * zz / k[0], (v - k[5]) * zz / k[4], zz], axis=1)
    return pts @ np.asarray(rot).T + np.asarray(trans)


def depth_profile(pts: np.ndarray, half_width: float = 0.2, step: float = 0.1, x_max: float = 4.0) -> list:
    """Профиль по курсу: полоса |y| ≤ half_width, бины по x; для каждого — число точек, медиана и максимум z."""
    sel = pts[(np.abs(pts[:, 1]) <= half_width) & (pts[:, 0] > 0) & (pts[:, 0] < x_max)]
    out = []
    for i in range(int(x_max / step)):
        b = sel[(sel[:, 0] >= i * step) & (sel[:, 0] < (i + 1) * step)]
        if len(b):
            out.append([round(i * step, 2), int(len(b)), round(float(np.median(b[:, 2])), 3),
                        round(float(b[:, 2].max()), 3)])
    return out


def stop_times(t_last: float, out: Sequence[Tuple[float, float]], truth: Sequence[Tuple[float, float]],
               v_cmd: float, v_stop: float = 0.01) -> dict:
    """После последней команды (t_last): когда выход контроллера начал падать (< 0,95·v), когда стал нулём, когда
    база встала по истине (скорость < v_stop). out, truth — (t, v); None — не случилось."""
    def first(seq, cond):
        return next((round(t - t_last, 3) for t, v in seq if t > t_last and cond(v)), None)
    return {"brake_s": first(out, lambda v: abs(v) < 0.95 * abs(v_cmd)),
            "out_zero_s": first(out, lambda v: abs(v) < 1e-3),
            "truth_stop_s": first(truth, lambda v: v < v_stop)}


def relative(start: Tuple[float, float, float], pose: Tuple[float, float, float]) -> Tuple[float, float, float]:
    """Поза pose (x, y, yaw) в системе позы start."""
    dx, dy = pose[0] - start[0], pose[1] - start[1]
    c, s = math.cos(start[2]), math.sin(start[2])
    dyaw = math.atan2(math.sin(pose[2] - start[2]), math.cos(pose[2] - start[2]))
    return c * dx + s * dy, -s * dx + c * dy, dyaw


def odom_error(odom: Sequence[Tuple[float, float, float, float]],
               truth: Sequence[Tuple[float, float, float, float]]) -> dict:
    """Одометрия против истины: (t, x, y, yaw) обеих, каждая — от своей первой позы. Для каждой позы истины берётся
    ближайшая по времени одометрия; ошибки — положения (м) и курса (°): в конце и наибольшие; путь по истине."""
    if not odom or not truth:
        return {}
    ot = np.array([o[0] for o in odom])
    pos_err, yaw_err, path = [], [], 0.0
    for i, tr in enumerate(truth):
        o = odom[int(np.argmin(np.abs(ot - tr[0])))]
        rt, ro = relative(truth[0][1:], tr[1:]), relative(odom[0][1:], o[1:])
        pos_err.append(math.hypot(rt[0] - ro[0], rt[1] - ro[1]))
        yaw_err.append(abs(math.degrees(math.atan2(math.sin(rt[2] - ro[2]), math.cos(rt[2] - ro[2])))))
        if i:
            path += math.hypot(tr[1] - truth[i - 1][1], tr[2] - truth[i - 1][2])
    return {"path_m": round(path, 3), "pos_err_end_m": round(pos_err[-1], 4), "pos_err_max_m": round(max(pos_err), 4),
            "yaw_err_end_deg": round(yaw_err[-1], 2), "yaw_err_max_deg": round(max(yaw_err), 2)}


# --- ROS ---

class Probe:
    def __init__(self, world: str):
        import rclpy
        from rclpy.parameter import Parameter
        self.rclpy = rclpy
        rclpy.init()
        self.node = rclpy.create_node("twin_probe",
                                      parameter_overrides=[Parameter("use_sim_time", Parameter.Type.BOOL, True)])
        self.spawn = cfg.WORLDS[world][1] if world in cfg.WORLDS else (0.0, 0.0, 0.01, 0.0)
        self.pubs: Dict[str, object] = {}
        self.clients: Dict[str, object] = {}
        self.log: Dict[str, list] = {}

    def now(self) -> float:
        return self.node.get_clock().now().nanoseconds * 1e-9

    def spin(self, seconds: float, wall_limit: Optional[float] = None):
        """Крутить колбэки seconds сим. секунд (не дольше wall_limit монотонных: при остановке /clock)."""
        t_end, w_end = self.now() + seconds, time.monotonic() + (wall_limit or 3 * seconds + 10)
        while self.now() < t_end and time.monotonic() < w_end:
            self.rclpy.spin_once(self.node, timeout_sec=0.005)

    def wait_clock(self, timeout_s: float = 20.0):
        t_end = time.monotonic() + timeout_s
        while self.now() <= 0 and time.monotonic() < t_end:
            self.rclpy.spin_once(self.node, timeout_sec=0.05)
        if self.now() <= 0:
            raise SystemExit("twin_probe: нет /clock за %.0f с" % timeout_s)

    def record(self, topic: str, mtype, extract, qos=None):
        from rclpy.qos import qos_profile_sensor_data
        rows = self.log.setdefault(topic, [])
        self.node.create_subscription(mtype, topic, lambda m: rows.append(extract(m)), qos or qos_profile_sensor_data)
        return rows

    def stamp(self, msg) -> float:
        return msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9

    def record_motion(self):
        """Выход контроллера, мультиплексора, одометрия и истина — для проб движения."""
        from geometry_msgs.msg import Twist, TwistStamped
        from nav_msgs.msg import Odometry
        from std_msgs.msg import String
        self.out = self.record(cfg.DIFF_OUT, TwistStamped, lambda m: (self.stamp(m), m.twist.linear.x,
                                                                      m.twist.angular.z), 10)
        self.mux = self.record(cfg.MUX_OUT, Twist, lambda m: (self.now(), m.linear.x, m.angular.z), 10)
        self.odom = self.record("/odom", Odometry, lambda m: (self.stamp(m), m.pose.pose.position.x,
                                                              m.pose.pose.position.y,
                                                              euler(m.pose.pose.orientation)[2]))
        self.truth = self.record("/twin/truth", String, lambda m: json.loads(m.data), 10)

    def publisher(self, topic: str):
        from geometry_msgs.msg import Twist
        if topic not in self.pubs:
            self.pubs[topic] = self.node.create_publisher(Twist, topic, 10)
        return self.pubs[topic]

    def drive(self, segments: Sequence[Tuple[float, Dict[str, Tuple[float, float]]]]) -> List[Tuple[str, float]]:
        """Сегменты (длительность, {тема: (v, w)}): публикация 10 Гц. Возвращает [(тема, время последней)]."""
        from geometry_msgs.msg import Twist
        last: Dict[str, float] = {}
        for dur, cmds in segments:
            for topic in cmds:
                self.publisher(topic)
            t_end, t_next = self.now() + dur, self.now()
            while self.now() < t_end:
                if self.now() >= t_next and cmds:
                    for topic, (v, w) in cmds.items():
                        msg = Twist()
                        msg.linear.x, msg.angular.z = float(v), float(w)
                        self.pubs[topic].publish(msg)
                        last[topic] = self.now()
                    t_next += 1.0 / PUB_HZ
                self.rclpy.spin_once(self.node, timeout_sec=0.002)
        return sorted(last.items())

    def call(self, srv_type, service: str, req):
        """Синхронный вызов сервиса мира; не выполнен (нет сервиса, success = false) — выход с ошибкой."""
        cli = self.clients.get(service) or self.node.create_client(srv_type, service)
        self.clients[service] = cli
        if not cli.wait_for_service(timeout_sec=10.0):
            raise SystemExit("twin_probe: нет сервиса %s" % service)
        future = cli.call_async(req)
        self.rclpy.spin_until_future_complete(self.node, future, timeout_sec=10.0)
        if not (future.done() and future.result().success):
            raise SystemExit("twin_probe: %s не выполнен" % service)

    def set_pose(self, name: str, x: float, y: float, z: float, yaw: float = 0.0, pitch: float = 0.0):
        """Модель name — в позу (сервис мира set_pose)."""
        from ros_gz_interfaces.msg import Entity
        from ros_gz_interfaces.srv import SetEntityPose
        req = SetEntityPose.Request()
        req.entity.name, req.entity.type = name, Entity.MODEL
        req.pose.position.x, req.pose.position.y, req.pose.position.z = x, y, z
        cy, sy, cp, sp = math.cos(yaw / 2), math.sin(yaw / 2), math.cos(pitch / 2), math.sin(pitch / 2)
        o = req.pose.orientation
        o.w, o.x, o.y, o.z = cy * cp, -sy * sp, cy * sp, sy * cp
        self.call(SetEntityPose, "/world/%s/set_pose" % cfg.WORLD_NAME, req)

    def reset_pose(self, pose: Optional[Tuple[float, float, float, float]] = None, pitch: float = 0.0,
                   settle_s: float = 1.5):
        """Робот — в позу (x, y, z, yaw; по умолчанию спавн мира) с тангажом pitch, затем ожидание: база встаёт на
        опоры."""
        x, y, z, yaw = pose or self.spawn
        self.set_pose(cfg.MODEL, x, y, z, yaw, pitch)
        self.spin(settle_s)

    def drive_until(self, v: float, done, max_s: float, stop_s: float = 0.5):
        """Вперёд со скоростью v по /cmd_vel, пока done() ложно и не прошло max_s сим. секунд; затем stop_s нулей."""
        t_end = self.now() + max_s
        while self.now() < t_end and not done():
            self.drive([(0.1, {cfg.CMD_VEL: (v, 0.0)})])
        self.drive([(stop_s, {cfg.CMD_VEL: (0.0, 0.0)})])

    def writers(self) -> Dict[str, List[str]]:
        """Чужие писатели входов twist_mux (до пробы их быть не должно)."""
        return {t: sorted(i.node_name for i in self.node.get_publishers_info_by_topic(t) if i.node_name != "twin_probe")
                for t in cfg.MUX_INPUTS}

    def close(self):
        self.node.destroy_node()
        self.rclpy.try_shutdown()


def probe_sensors(p: Probe, seconds: float) -> dict:
    import tf2_ros
    from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
    from rclpy.time import Time
    from rosidl_runtime_py.utilities import get_message
    from tf2_msgs.msg import TFMessage

    stamps: Dict[str, List[float]] = {t: [] for t in cfg.SENSORS}
    edges: Dict[Tuple[str, str], str] = {}
    scans, imu, latest = [], [], {}

    def on_msg(topic):
        def cb(m):
            stamps[topic].append(p.stamp(m) if hasattr(m, "header") else p.now())
            if topic == "/scan":
                scans.append(np.isnan(np.asarray(m.ranges, dtype=float)))
            elif topic == "/camera/imu":
                imu.append((m.linear_acceleration.x, m.linear_acceleration.y, m.linear_acceleration.z))
            latest[topic] = m
        return cb

    def subscribe(topics):
        for topic in topics:
            p.node.create_subscription(get_message(cfg.SENSORS[topic][0]), topic, on_msg(topic),
                                       qos_profile_sensor_data)

    def tf_cb(kind):
        return lambda m: edges.update({(t.header.frame_id, t.child_frame_id): kind for t in m.transforms})
    p.node.create_subscription(TFMessage, "/tf", tf_cb("tf"), qos_profile_sensor_data)
    p.node.create_subscription(TFMessage, "/tf_static", tf_cb("tf_static"),
                               QoSProfile(depth=100, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    tf_buf = tf2_ros.Buffer()
    tf2_ros.TransformListener(tf_buf, p.node)

    def window():
        """Окно замера: частоты по меткам сообщений, RTF — по часам узла (/clock, best effort) и монотонным."""
        for s in stamps.values():
            s.clear()
        clock = [(time.monotonic(), p.now())]
        p.spin(seconds)
        clock.append((time.monotonic(), p.now()))
        return {"rtf": round(rtf(clock) or 0.0, 4), "wall_s": round(clock[-1][0] - clock[0][0], 2),
                "hz": {t: (None if rate(s) is None else round(rate(s), 2)) for t, s in stamps.items()}}

    subscribe([t for t in cfg.SENSORS if t not in cfg.CAMERA_TOPICS])
    p.spin(2.0)
    no_cam = window()
    subscribe(cfg.CAMERA_TOPICS)
    p.spin(3.0)
    cam = window()
    types = dict(p.node.get_topic_names_and_types())
    out = {"windows": {"camera_off": no_cam, "camera_on": cam},
           "types": {t: types.get(t, []) for t in cfg.SENSORS},
           "tf_edges": sorted([a, b, k] for (a, b), k in edges.items())}
    reach = {}
    for frame in cfg.TF_FRAMES:
        try:
            tr = tf_buf.lookup_transform("odom", frame, Time()).transform.translation
            reach[frame] = [round(tr.x, 3), round(tr.y, 3), round(tr.z, 3)]
        except tf2_ros.TransformException as e:
            reach[frame] = "нет: %s" % str(e)[:80]
    out["tf_from_odom"] = reach
    if scans:
        frac = np.mean(np.array(scans), axis=0)
        s = latest["/scan"]
        r = np.asarray(s.ranges, dtype=float)
        ang = np.degrees(s.angle_min + np.arange(len(r)) * s.angle_increment)
        front = np.abs(ang) <= 30
        out["scan"] = {"n_scans": len(scans), "frame_id": s.header.frame_id, "n": len(r),
                       "angle_min": s.angle_min, "angle_max": s.angle_max, "angle_increment": s.angle_increment,
                       "range_min": s.range_min, "range_max": s.range_max,
                       "front30_nan": int(np.isnan(r[front]).sum()), "front30_n": int(front.sum()),
                       "front30_min_finite_m": (None if np.all(np.isnan(r[front]))
                                                else round(float(np.nanmin(r[front])), 3)),
                       "blind": blind_summary(frac.tolist())}
    if imu:
        a = np.mean(np.array(imu[-200:]), axis=0)
        out["imu"] = {"accel_opt_mean": [round(float(v), 3) for v in a], "tilt_accel_deg": round(tilt_accel(a), 2)}
        q = latest.get("/imu/data")
        if q is not None:
            o = q.orientation
            out["imu"]["tilt_madgwick_deg"] = round(tilt_orientation((o.x, o.y, o.z, o.w)), 2)
    if "/twin/truth" in latest:
        out["truth"] = json.loads(latest["/twin/truth"].data)
    depth, info = latest.get("/camera/depth/image_rect_raw"), latest.get("/camera/depth/camera_info")
    if depth is not None and info is not None:
        try:
            tr = tf_buf.lookup_transform("base_link", depth.header.frame_id, Time()).transform
            q, t = tr.rotation, tr.translation
            img = np.frombuffer(bytes(depth.data), dtype=np.uint16).reshape(depth.height, depth.width)
            pts = depth_points(img, list(info.k), quat_matrix(q.x, q.y, q.z, q.w), (t.x, t.y, t.z))
            out["depth"] = {"encoding": depth.encoding, "size": [depth.width, depth.height],
                            "frame_id": depth.header.frame_id, "valid_frac": round(float((img > 0).mean()), 3),
                            "profile": depth_profile(pts)}
        except tf2_ros.TransformException as e:
            out["depth"] = {"error": str(e)[:120]}
    return out


def probe_mux(p: Probe) -> dict:
    p.record_motion()
    p.reset_pose()
    writers = p.writers()
    t0 = p.now()
    phases = [("joy_over_cmd", 1.5, {"/cmd_vel": (0.1, 0.0), "/cmd_vel_joy": (0.0, 0.0)}, 0.0),
              ("cmd_alone", 1.5, {"/cmd_vel": (0.1, 0.0)}, 0.1),
              ("tracker_over_cmd", 1.5, {"/cmd_vel": (0.0, 0.0), "/cmd_vel_tracker": (0.05, 0.0)}, 0.05)]
    out, t = {"writers_before": writers}, t0
    for name, dur, cmds, expect in phases:
        p.drive([(dur, cmds)])
        seen = [v for ts, v, _ in p.mux if t + 0.6 <= ts <= t + dur]
        switch = next((round(ts - t, 3) for ts, v, _ in p.mux if ts >= t and abs(v - expect) < 1e-6), None)
        out[name] = {"expect": expect, "values_after_0_6s": sorted(set(round(v, 4) for v in seen)),
                     "first_expected_s": switch}
        t = p.now()
    p.drive([(1.0, {})])
    joy = [r for r in p.truth if t0 <= r["t"] <= t0 + phases[0][1]]
    if joy:
        out["joy_over_cmd"]["truth_moved_m"] = round(math.hypot(joy[-1]["x"] - joy[0]["x"], joy[-1]["y"] - joy[0]["y"]),
                                                     4)
    return out


def probe_silence(p: Probe, v: float, drive_s: float, wait_s: float) -> dict:
    p.record_motion()
    p.reset_pose()
    writers = p.writers()
    t0 = p.now()
    last = dict(p.drive([(drive_s, {cfg.CMD_VEL: (v, 0.0)})]))
    p.drive([(wait_s, {})])
    t_last = last[cfg.CMD_VEL]
    truth = [r for r in p.truth if r["t"] >= t0]
    at_last = min(truth, key=lambda r: abs(r["t"] - t_last))
    end = truth[-1]
    contacts = sorted({c for r in truth for c in r["contacts"]})
    t_contact = next((round(r["t"] - t_last, 3) for r in truth if r["contacts"]), None)
    odom = [(o[0], o[1], o[2], o[3]) for o in p.odom if o[0] >= t0]
    out = {"writers_before": writers, "v_cmd": v, "drive_s": drive_s, "wait_s": wait_s,
           "mux_last_after_s": round(max((ts for ts, _, _ in p.mux), default=t_last) - t_last, 3),
           **stop_times(t_last, [(ts, lin) for ts, lin, _ in p.out], [(r["t"], r["v"]) for r in truth], v),
           "coast_m": round(math.hypot(end["x"] - at_last["x"], end["y"] - at_last["y"]), 3),
           "v_truth_end": end["v"], "min_clearance_m": min((r["clearance_m"] for r in truth
                                                            if r["clearance_m"] is not None), default=None),
           "contacts": contacts, "first_contact_after_s": t_contact,
           "odom_vs_truth": odom_error(odom, [(r["t"], r["x"], r["y"], r["yaw"]) for r in truth])}
    if odom:
        out["odom_path_m"] = round(sum(math.hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(odom, odom[1:])), 3)
    p.drive([(0.5, {cfg.CMD_VEL: (0.0, 0.0)})])
    return out


def probe_odom(p: Probe) -> dict:
    p.record_motion()
    p.reset_pose()
    t0 = p.now()
    p.drive([(3.0, {cfg.CMD_VEL: (0.2, 0.0)}), (2.6, {cfg.CMD_VEL: (0.0, 0.6)}), (2.0, {cfg.CMD_VEL: (0.2, 0.0)}),
             (2.6, {cfg.CMD_VEL: (0.0, -0.6)}), (1.5, {cfg.CMD_VEL: (0.0, 0.0)})])
    truth = [(r["t"], r["x"], r["y"], r["yaw"]) for r in p.truth if r["t"] >= t0]
    odom = [o for o in p.odom if o[0] >= t0]
    return {"writers_before": p.writers(), **odom_error(odom, truth),
            "contacts": sorted({c for r in p.truth if r["t"] >= t0 for c in r["contacts"]})}


def probe_ramp(p: Probe, speed: float = 0.2, settle_s: float = 4.0) -> dict:
    """Пандус: (1) въезд с пола — докуда доехал, наибольший тангаж; (2) стоянка на склоне (поставлен сервисом мира):
    наклон по акселерометру, madgwick и истине, сползание; (3) подъём по склону и остановка — наклон снова."""
    from sensor_msgs.msg import Imu
    p.record_motion()
    imu = p.record("/camera/imu", Imu, lambda m: (p.stamp(m), m.linear_acceleration.x, m.linear_acceleration.y,
                                                   m.linear_acceleration.z))
    mad = p.record("/imu/data", Imu, lambda m: (p.stamp(m), m.orientation.x, m.orientation.y, m.orientation.z,
                                                 m.orientation.w))

    def tilts(t_from):
        acc = np.mean([r[1:] for r in imu if r[0] >= t_from], axis=0)
        tr = p.truth[-1]
        return {"x": tr["x"], "z": tr["z"], "tilt_truth_deg": round(-math.degrees(tr["pitch"]), 2),
                "tilt_accel_deg": round(tilt_accel(acc), 2),
                "tilt_madgwick_deg": round(tilt_orientation(mad[-1][1:]), 2) if mad else None}

    start_x, slope = cfg.RAMP_START_X, math.radians(cfg.RAMP_DEG)
    p.reset_pose()
    t0, x_goal = p.now(), start_x + 0.5
    p.drive_until(speed, lambda: p.truth and p.truth[-1]["x"] >= x_goal, 15.0, stop_s=1.0)
    climb = [r for r in p.truth if r["t"] >= t0]
    out = {"from_floor": {"goal_x": round(x_goal, 3), "reached": climb[-1]["x"] >= x_goal,
                          "x_max": max(r["x"] for r in climb), "x_end": climb[-1]["x"],
                          "tilt_truth_max_deg": round(max(-math.degrees(r["pitch"]) for r in climb), 2)}}
    p.reset_pose((0.0, 0.0, -start_x * math.tan(slope) + 0.003, 0.0), pitch=-slope, settle_s=0.5)
    p.drive([(settle_s, {cfg.CMD_VEL: (0.0, 0.0)})])
    x_hold = p.truth[-1]["x"]
    out["standing"] = dict(tilts(p.now() - 1.0), slide_m=None)
    p.drive([(settle_s, {cfg.CMD_VEL: (0.0, 0.0)})])
    out["standing"]["slide_m"] = round(abs(p.truth[-1]["x"] - x_hold), 4)
    p.drive([(2.0, {cfg.CMD_VEL: (0.15, 0.0)}), (settle_s, {cfg.CMD_VEL: (0.0, 0.0)})])
    out["after_climb"] = tilts(p.now() - 1.0)
    out["contacts"] = sorted({c for r in p.truth if r["t"] >= t0 for c in r["contacts"]})
    return out


def box_sdf(name: str, size: Sequence[float]) -> str:
    """SDF статичного бокса для сервиса create."""
    box = "<geometry><box><size>%g %g %g</size></box></geometry>" % tuple(size)
    return ("<sdf version='1.9'><model name='%s'><static>true</static><link name='link'><collision name='c'>%s"
            "</collision><visual name='v'>%s</visual></link></model></sdf>" % (name, box, box))


def probe_truth(p: Probe, v: float = 0.15) -> dict:
    """Истина при сценариях сервисами мира (мир lab): (1) set_pose — человек перед роботом: зазор по новой позе,
    наезд — касание; (2) create — низкий «кабель» ниже палуб перед роботом: пока формы нет, зазор null; когда форма
    известна, зазор от ролика; наезд — касание ролика; (3) remove — кабель исчез из истины; RTF из /twin/truth."""
    from ros_gz_interfaces.msg import Entity
    from ros_gz_interfaces.srv import DeleteEntity, SpawnEntity
    p.record_motion()
    t_start = p.now()

    def since(t0):
        return [r for r in p.truth if r["t"] >= t0]

    def brief(r):
        return {k: r.get(k) for k in ("x", "clearance_m", "nearest", "part", "unknown_models", "contacts")}

    def run_into(name, max_s):
        t0 = p.now()
        p.drive_until(v, lambda: bool(p.truth) and name in p.truth[-1]["contacts"], max_s)
        seg = since(t0)
        d = [r["clearance_m"] for r in seg if r["clearance_m"] is not None]
        return {"drive_contacts": sorted({c for r in seg for c in r["contacts"]}),
                "drive_min_clearance_m": min(d, default=None), "drive_x_end": seg[-1]["x"] if seg else None}

    person, home, front = cfg.TRUTH_PERSON
    p.reset_pose()
    p.set_pose(person, front[0], front[1], 0.0)
    p.spin(1.0)
    out = {"set_pose": {**brief(p.truth[-1]), **run_into(person, 6.0)}}
    p.set_pose(person, home[0], home[1], 0.0)

    name, (cx, cy), size = cfg.TRUTH_CABLE
    p.reset_pose()
    req = SpawnEntity.Request()
    req.entity_factory.name, req.entity_factory.sdf = name, box_sdf(name, size)
    req.entity_factory.pose.position.x, req.entity_factory.pose.position.y = cx, cy
    req.entity_factory.pose.position.z = size[2] / 2
    p.call(SpawnEntity, "/world/%s/create" % cfg.WORLD_NAME, req)
    t0, known = p.now(), None
    while known is None and p.now() < t0 + 5.0:
        p.spin(0.1)
        r = p.truth[-1]
        if not r["unknown_models"] and r["nearest"].startswith(name + "/"):
            known = r
    seg = since(t0)
    unknown = [r for r in seg if r["unknown_models"]]
    out["create"] = {"unknown_seen": any(name in r["unknown_models"] for r in unknown),
                     "null_while_unknown": all(r["clearance_m"] is None for r in unknown),
                     "known_after_s": None if known is None else round(known["t"] - t0, 3),
                     **(brief(known) if known else {}), **run_into(name, 8.0)}

    req = DeleteEntity.Request()
    req.entity.name, req.entity.type = name, Entity.MODEL
    p.call(DeleteEntity, "/world/%s/remove" % cfg.WORLD_NAME, req)
    p.spin(1.5)
    last = p.truth[-1]
    out["remove"] = {"gone": name not in last["unknown_models"] + last["contacts"] and
                     not last["nearest"].startswith(name + "/"), **brief(last)}
    seg = since(t_start)
    out["rtf"] = {"last": last.get("rtf"), "last_ok": last.get("rtf_ok"),
                  "ok_frac": round(sum(bool(r.get("rtf_ok")) for r in seg) / len(seg), 3) if seg else None}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("probe", choices=("sensors", "mux", "silence", "odom", "ramp", "truth"))
    ap.add_argument("--world", default="lab", help="мир прогона: поза спавна для set_pose")
    ap.add_argument("--seconds", type=float, default=20.0, help="sensors: окно замера, сим. с")
    ap.add_argument("--v", type=float, default=0.2, help="silence: скорость, м/с")
    ap.add_argument("--drive-s", type=float, default=2.0, help="silence: сколько ехать, с")
    ap.add_argument("--wait-s", type=float, default=3.0, help="silence: сколько ждать в тишине, с")
    a = ap.parse_args()
    p = Probe(a.world)
    try:
        p.wait_clock()
        start = (time.monotonic(), p.now())
        if a.probe == "sensors":
            out = probe_sensors(p, a.seconds)
        elif a.probe == "mux":
            out = probe_mux(p)
        elif a.probe == "silence":
            out = probe_silence(p, a.v, a.drive_s, a.wait_s)
        elif a.probe == "odom":
            out = probe_odom(p)
        elif a.probe == "truth":
            out = probe_truth(p)
        else:
            out = probe_ramp(p)
        out["probe_rtf"] = round(rtf([start, (time.monotonic(), p.now())]) or 0.0, 4)
    finally:
        p.close()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
