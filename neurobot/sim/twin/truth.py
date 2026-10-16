#!/usr/bin/env python3
"""(в ВМ sim) Истина Gazebo для проверок безопасности — узел twin_truth: поза робота, скорость, расстояние от робота до
препятствий, касания и RTF. Роботу (стражу, исполнителю, агенту) не выдаётся.

Вход: /ground_truth/pose (PosePublisher модели), /twin/contacts (датчик касаний корпуса), /twin/models (логическая
камера twin_eye над миром: имена и позы всех моделей верхнего уровня), robot_urdf (коллизии робота), world_sdf (формы
моделей мира при старте). Выход: /twin/truth — std_msgs/String, JSON с частотой rate_hz:
  {t — сим. время позы, x, y, z, roll, pitch, yaw, v — скорость по плоскости, м/с, w — по рысканию, рад/с,
   clearance_m — наименьшее расстояние от коллизий робота до препятствий (null — препятствий по высоте нет или форма
   какой-то модели ещё неизвестна), nearest — «модель/коллизия», part — «звено/коллизия» робота, unknown_models —
   модели, чья форма ещё неизвестна, contacts — модели, которых робот касался за последние 0,2 с: корпус — по датчику
   касаний Gazebo, колёса и ролики — по геометрии (зазор ≤ 2 мм; рельеф для них опора, а не препятствие), rtf —
   прирост сим. времени к приросту монотонного за последние 3 с (null — данных меньше половины окна), rtf_ok —
   rtf ≥ 0,9}.
Позы препятствий берутся из /twin/models, поэтому сервисы мира set_pose, create и remove учитываются сразу. Формы —
из world_sdf; для модели, которой там нет (create), узел запрашивает текущий мир у Gazebo (`gz service
/world/<мир>/generate_world_sdf`, ≈0,5 с), до ответа clearance_m = null. Удалённая модель забывается вместе с формой.
Расстояние считается в проекции на пол для пар, чьи высоты перекрываются (нависание стола над палубой — не касание,
над головой URG — касание); наклон робота и препятствий не учитывается. Пол и рельеф (terrain — регулярное выражение
имён моделей) в расчёт не входят.
"""
from __future__ import annotations

import codecs
import collections
import json
import math
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

TERRAIN = r"^(ground_plane|step_platform|ramp.*)$"
SUPPORT = r"wheel|caster"         # опоры робота (имя звена): касания — по геометрии, датчика у них нет
TOUCH_GEOM_M = 0.002
CONTACT_WINDOW_S = 0.2
RTF_WINDOW_S = 3.0
RTF_OK = 0.9
EYE = "twin_eye"
FETCH_RETRY_S = 2.0
# Логическая камера над миром: смотрит вниз с 15 м, поле 2,6 рад — видит начала моделей в квадрате ±54 м на полу
# (15 · tan 1,3; проверено gz.math7.Frustum в ВМ).
EYE_SDF = """  <!-- twin.launch.py: логическая камера над миром — имена и позы моделей для truth.py (/twin/models). -->
  <model name="%s"><static>true</static><pose>0 0 15 0 1.5707963 0</pose><link name="link">
    <sensor name="models" type="logical_camera"><always_on>true</always_on><update_rate>%g</update_rate>
      <topic>twin/models</topic><logical_camera><near>0.1</near><far>40</far><horizontal_fov>2.6</horizontal_fov>
        <aspect_ratio>1</aspect_ratio></logical_camera></sensor></link>%s</model>
"""
EYE_PLUGIN = '\n    <plugin filename="gz-sim-logical-camera-system" name="gz::sim::systems::LogicalCamera"/>'


class Shape(NamedTuple):
    """Тело в проекции на пол: круг (r > 0) или прямоугольник sx × sy с поворотом yaw; высоты z0…z1."""
    name: str
    cx: float
    cy: float
    yaw: float
    r: float
    sx: float
    sy: float
    z0: float
    z1: float


class Quat(NamedTuple):
    x: float
    y: float
    z: float
    w: float


Pose4 = Tuple[float, float, float, float]          # x, y, z, рыскание


def _floats(text: Optional[str], n: int) -> list:
    vals = [float(v) for v in (text or "").split()]
    return vals + [0.0] * (n - len(vals))


def _shape(name, geom, x, y, z, roll, yaw) -> Optional[Shape]:
    if geom is None:
        return None
    if geom.tag == "cylinder" or geom.find("cylinder") is not None:
        g = geom if geom.tag == "cylinder" else geom.find("cylinder")
        r = float(g.get("radius") or g.findtext("radius"))
        h = float(g.get("length") or g.findtext("length"))
        if abs(abs(roll) - math.pi / 2) < 1e-3:          # ось вдоль y звена (колесо)
            return Shape(name, x, y, yaw, 0.0, 2 * r, h, z - r, z + r)
        return Shape(name, x, y, yaw, r, 0.0, 0.0, z - h / 2, z + h / 2)
    if geom.tag == "box" or geom.find("box") is not None:
        g = geom if geom.tag == "box" else geom.find("box")
        sx, sy, sz = _floats(g.get("size") or g.findtext("size"), 3)
        return Shape(name, x, y, yaw, 0.0, sx, sy, z - sz / 2, z + sz / 2)
    if geom.tag == "sphere" or geom.find("sphere") is not None:
        g = geom if geom.tag == "sphere" else geom.find("sphere")
        r = float(g.get("radius") or g.findtext("radius"))
        return Shape(name, x, y, yaw, r, 0.0, 0.0, z - r, z + r)
    return None


def robot_parts(urdf: str) -> List[Shape]:
    """Коллизии URDF в кадре base_link (звенья на суставах от base_link; суставы колёс — в нулевом положении)."""
    root = ET.fromstring(urdf)
    origin = {"base_link": (0.0, 0.0, 0.0, 0.0, 0.0)}
    for j in root.findall("joint"):
        if j.find("parent").get("link") == "base_link":
            o = j.find("origin")
            x, y, z = _floats(o.get("xyz") if o is not None else "", 3)
            roll, _, yaw = _floats(o.get("rpy") if o is not None else "", 3)
            origin[j.find("child").get("link")] = (x, y, z, roll, yaw)
    out = []
    for link in root.findall("link"):
        if link.get("name") not in origin:
            continue
        lx, ly, lz, lroll, lyaw = origin[link.get("name")]
        for i, col in enumerate(link.findall("collision")):
            o = col.find("origin")
            cx, cy, cz = _floats(o.get("xyz") if o is not None else "", 3)
            croll, _, cyaw = _floats(o.get("rpy") if o is not None else "", 3)
            c, s = math.cos(lyaw), math.sin(lyaw)
            shape = _shape("%s/%s" % (link.get("name"), col.get("name") or i), col.find("geometry"),
                           lx + c * cx - s * cy, ly + s * cx + c * cy, lz + cz, lroll + croll, lyaw + cyaw)
            if shape:
                out.append(shape)
    return out


def model_shapes(sdf: str, skip: str = TERRAIN) -> Dict[str, Tuple[Pose4, List[Shape]]]:
    """Модели верхнего уровня мира, кроме имён по регулярному выражению skip: {имя: (поза модели, коллизии в кадре
    модели — поза звена ∘ коллизии)}. Крен и тангаж не учитываются."""
    pat = re.compile(skip)
    out = {}
    for model in ET.fromstring(sdf).find("world").findall("model"):
        name = model.get("name", "")
        if pat.search(name):
            continue
        mx, my, mz, _, _, myaw = _floats(model.findtext("pose"), 6)
        parts = []
        for link in model.findall("link"):
            lx, ly, lz, _, _, lyaw = _floats(link.findtext("pose"), 6)
            c, s = math.cos(lyaw), math.sin(lyaw)
            for col in link.findall("collision"):
                cx, cy, cz, _, _, cyaw = _floats(col.findtext("pose"), 6)
                shape = _shape("%s/%s" % (name, col.get("name")), col.find("geometry"), lx + c * cx - s * cy,
                               ly + s * cx + c * cy, lz + cz, 0.0, lyaw + cyaw)
                if shape:
                    parts.append(shape)
        out[name] = ((mx, my, mz, myaw), parts)
    return out


def placed(models: Dict[str, Tuple[Pose4, List[Shape]]], poses: Dict[str, Pose4]) -> List[Shape]:
    """Препятствия в мире: коллизии моделей по их текущим позам (модели без позы — нет в мире)."""
    return [place(s, *poses[n]) for n, (_, parts) in models.items() if n in poses for s in parts]


def with_eye(world_sdf: str, rate_hz: float = 10.0) -> str:
    """Мир с логической камерой twin_eye перед </world>; система логической камеры — в модели, если мир её не грузит."""
    plugin = "" if "logical-camera-system" in world_sdf else EYE_PLUGIN
    i = world_sdf.rindex("</world>")
    return world_sdf[:i] + EYE_SDF % (EYE, rate_hz, plugin) + world_sdf[i:]


def text_string(reply: str) -> str:
    """Строка из ответа `gz service` (protobuf text format: data: "…", управляющие и не-ASCII байты экранированы)."""
    m = re.search(r'data: "(.*)"\s*$', reply, re.S)
    if not m:
        raise ValueError("в ответе gz service нет поля data: %r" % reply[:200])
    return codecs.escape_decode(m.group(1).encode("utf-8"))[0].decode("utf-8")


def world_sdf_now(world: str, timeout_s: float = 5.0) -> str:
    """Текущий мир из Gazebo: сервис generate_world_sdf через `gz service` (раздел — GZ_PARTITION окружения)."""
    reply = subprocess.run(["gz", "service", "-s", "/world/%s/generate_world_sdf" % world, "--reqtype",
                            "gz.msgs.SdfGeneratorConfig", "--reptype", "gz.msgs.StringMsg", "--timeout",
                            str(int(timeout_s * 1000)), "--req", ""], capture_output=True, text=True, check=True,
                           timeout=timeout_s + 10).stdout
    return text_string(reply)


def _corners(a: Shape) -> list:
    c, s = math.cos(a.yaw), math.sin(a.yaw)
    return [(a.cx + c * x - s * y, a.cy + s * x + c * y)
            for x, y in ((-a.sx / 2, -a.sy / 2), (a.sx / 2, -a.sy / 2), (a.sx / 2, a.sy / 2), (-a.sx / 2, a.sy / 2))]


def _point_rect(px: float, py: float, a: Shape) -> float:
    c, s = math.cos(a.yaw), math.sin(a.yaw)
    lx, ly = c * (px - a.cx) + s * (py - a.cy), -s * (px - a.cx) + c * (py - a.cy)
    return math.hypot(max(abs(lx) - a.sx / 2, 0.0), max(abs(ly) - a.sy / 2, 0.0))


def _seg_point(p, q, r) -> float:
    dx, dy = q[0] - p[0], q[1] - p[1]
    t = max(0.0, min(1.0, ((r[0] - p[0]) * dx + (r[1] - p[1]) * dy) / (dx * dx + dy * dy or 1.0)))
    return math.hypot(p[0] + t * dx - r[0], p[1] + t * dy - r[1])


def _overlap(pa: list, pb: list) -> bool:
    """Пересекаются ли выпуклые многоугольники (теорема о разделяющей оси)."""
    for poly in (pa, pb):
        for i in range(len(poly)):
            (x0, y0), (x1, y1) = poly[i], poly[(i + 1) % len(poly)]
            nx, ny = y0 - y1, x1 - x0
            a = [nx * x + ny * y for x, y in pa]
            b = [nx * x + ny * y for x, y in pb]
            if max(a) < min(b) or max(b) < min(a):
                return False
    return True


def distance_2d(a: Shape, b: Shape) -> float:
    """Расстояние между телами в проекции на пол; 0 — пересекаются."""
    if a.r > 0 and b.r > 0:
        return max(0.0, math.hypot(a.cx - b.cx, a.cy - b.cy) - a.r - b.r)
    if a.r > 0 or b.r > 0:
        circ, rect = (a, b) if a.r > 0 else (b, a)
        return max(0.0, _point_rect(circ.cx, circ.cy, rect) - circ.r)
    pa, pb = _corners(a), _corners(b)
    if _overlap(pa, pb):
        return 0.0
    return min(min(_seg_point(p[i], p[(i + 1) % 4], r) for i in range(4) for r in q)
               for p, q in ((pa, pb), (pb, pa)))


def place(part: Shape, x: float, y: float, z: float, yaw: float) -> Shape:
    """Тело из кадра модели (base_link, модель мира) в мир по позе модели (только рыскание)."""
    c, s = math.cos(yaw), math.sin(yaw)
    return part._replace(cx=x + c * part.cx - s * part.cy, cy=y + s * part.cx + c * part.cy, yaw=yaw + part.yaw,
                         z0=z + part.z0, z1=z + part.z1)


def distances(parts: List[Shape], obstacles: List[Shape], x: float, y: float, z: float,
              yaw: float) -> List[Tuple[float, str, str]]:
    """Расстояния от частей робота до препятствий, перекрывающихся по высоте: [(м, препятствие, часть робота)]."""
    out = []
    for p in parts:
        w = place(p, x, y, z, yaw)
        out += [(distance_2d(w, o), o.name, p.name) for o in obstacles if w.z0 < o.z1 and o.z0 < w.z1]
    return out


def clearance(dists: List[Tuple[float, str, str]]) -> Tuple[Optional[float], str, str]:
    """Наименьшее из расстояний: (м, препятствие, часть робота); пар нет — (None, "", "")."""
    return min(dists, key=lambda d: d[0], default=(None, "", ""))


def support_touch(dists: List[Tuple[float, str, str]], eps: float = TOUCH_GEOM_M) -> List[str]:
    """Модели, которых касаются колёса и ролики по геометрии (зазор ≤ eps)."""
    return sorted({o.split("/", 1)[0] for d, o, p in dists if d <= eps and re.search(SUPPORT, p)})


def euler(q) -> Tuple[float, float, float]:
    """Крен, тангаж, рыскание (рад) из кватерниона с полями x, y, z, w."""
    roll = math.atan2(2 * (q.w * q.x + q.y * q.z), 1 - 2 * (q.x * q.x + q.y * q.y))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (q.w * q.y - q.z * q.x))))
    yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
    return roll, pitch, yaw


def q_mul(a, b) -> Quat:
    return Quat(a.w * b.x + a.x * b.w + a.y * b.z - a.z * b.y, a.w * b.y - a.x * b.z + a.y * b.w + a.z * b.x,
                a.w * b.z + a.x * b.y - a.y * b.x + a.z * b.w, a.w * b.w - a.x * b.x - a.y * b.y - a.z * b.z)


def world_pose(cam_pos: Sequence[float], cam_q, pos: Sequence[float], q) -> Pose4:
    """Поза модели в мире (x, y, z, рыскание) из позы относительно логической камеры: камера ∘ модель."""
    v = q_mul(q_mul(cam_q, Quat(pos[0], pos[1], pos[2], 0.0)), Quat(-cam_q.x, -cam_q.y, -cam_q.z, cam_q.w))
    return cam_pos[0] + v.x, cam_pos[1] + v.y, cam_pos[2] + v.z, euler(q_mul(cam_q, q))[2]


def contact_model(entity: str, robot: str) -> Optional[str]:
    """Модель второго тела касания по имени коллизии gz «модель::звено::коллизия»; своё тело — None."""
    model = entity.split("::", 1)[0]
    return None if model == robot else model


def rtf(clock: Sequence[Tuple[float, float]]) -> Optional[float]:
    """Отношение сим. времени к монотонному по парам (монотонное, сим.): от первой до последней."""
    if len(clock) < 2 or clock[-1][0] <= clock[0][0]:
        return None
    return (clock[-1][1] - clock[0][1]) / (clock[-1][0] - clock[0][0])


class Rtf:
    """RTF за скользящее окно: прирост сим. времени к приросту монотонного (от первой пары окна до «сейчас»: если
    сим. время встало, RTF падает)."""

    def __init__(self, window_s: float = RTF_WINDOW_S):
        self.window_s = window_s
        self.samples = collections.deque()

    def add(self, mono: float, sim: float):
        if self.samples and sim < self.samples[-1][1]:
            self.samples.clear()                       # мир начат заново
        self.samples.append((mono, sim))

    def value(self, now: float) -> Optional[float]:
        """None — в окне меньше половины его длины данных (старт, остановка позы)."""
        while self.samples and now - self.samples[0][0] > self.window_s:
            self.samples.popleft()
        if len(self.samples) < 2 or now - self.samples[0][0] < self.window_s / 2:
            return None
        return rtf([self.samples[0], (now, self.samples[-1][1])])


def main():
    from concurrent.futures import ThreadPoolExecutor

    import rclpy
    from rclpy.executors import ExternalShutdownException
    from geometry_msgs.msg import PoseStamped
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from ros_gz_interfaces.msg import Contacts, LogicalCameraImage
    from std_msgs.msg import String

    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    from twin_config import MODEL, MODELS_TOPIC, WORLD_NAME

    def vec(p):
        return p.x, p.y, p.z

    class Truth(Node):
        def __init__(self):
            super().__init__("twin_truth")
            self.skip = "%s|^(%s|%s)$" % (self.declare_parameter("terrain", TERRAIN).value, MODEL, EYE)
            with open(self.declare_parameter("robot_urdf", "").value) as fh:
                self.parts = robot_parts(fh.read())
            with open(self.declare_parameter("world_sdf", "").value) as fh:
                self.models = model_shapes(fh.read(), self.skip)
            self.poses = {n: m[0] for n, m in self.models.items()}
            self.unknown = set()
            self.pool, self.fetch, self.fetch_after = ThreadPoolExecutor(max_workers=1), None, 0.0
            self.pose = None
            self.prev = None
            self.vel = (0.0, 0.0)
            self.touch = {}
            self.rtf = Rtf()
            self.pub = self.create_publisher(String, "/twin/truth", 10)
            self.create_subscription(PoseStamped, "/ground_truth/pose", self.on_pose, qos_profile_sensor_data)
            self.create_subscription(Contacts, "/twin/contacts", self.on_contacts, qos_profile_sensor_data)
            self.create_subscription(LogicalCameraImage, MODELS_TOPIC, self.on_models, qos_profile_sensor_data)
            self.create_timer(1.0 / float(self.declare_parameter("rate_hz", 20.0).value), self.tick)

        def on_pose(self, msg):
            p, q = msg.pose.position, msg.pose.orientation
            t = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
            self.rtf.add(time.monotonic(), t)
            roll, pitch, yaw = euler(q)
            self.pose = (t, p.x, p.y, p.z, roll, pitch, yaw)
            if self.prev is None or t < self.prev[0]:
                self.prev = self.pose
            elif t - self.prev[0] >= 0.05:
                dt = t - self.prev[0]
                dyaw = math.atan2(math.sin(yaw - self.prev[6]), math.cos(yaw - self.prev[6]))
                self.vel = (math.hypot(p.x - self.prev[1], p.y - self.prev[2]) / dt, dyaw / dt)
                self.prev = self.pose

        def on_models(self, msg):
            skip = re.compile(self.skip)
            cam = msg.pose
            self.poses = {m.name: world_pose(vec(cam.position), cam.orientation, vec(m.pose.position),
                                             m.pose.orientation) for m in msg.model if not skip.search(m.name)}
            for gone in set(self.models) - set(self.poses):
                del self.models[gone]
            self.unknown = set(self.poses) - set(self.models)
            if self.unknown and self.fetch is None and time.monotonic() >= self.fetch_after:
                self.fetch = self.pool.submit(world_sdf_now, WORLD_NAME)

        def take_fetch(self):
            """Формы новых моделей из ответа generate_world_sdf."""
            try:
                fresh = model_shapes(self.fetch.result(), self.skip)
                self.models.update({n: fresh[n] for n in self.unknown & set(fresh)})
            except (OSError, subprocess.SubprocessError, ET.ParseError, ValueError) as e:
                self.get_logger().warning("generate_world_sdf: %s" % str(e)[:200])
            self.fetch = None
            self.unknown -= set(self.models)
            self.fetch_after = time.monotonic() + FETCH_RETRY_S if self.unknown else 0.0

        def on_contacts(self, msg):
            now = time.monotonic()
            for c in msg.contacts:
                for entity in (c.collision1.name, c.collision2.name):
                    model = contact_model(entity, MODEL)
                    if model:
                        self.touch[model] = now

        def tick(self):
            if self.fetch is not None and self.fetch.done():
                self.take_fetch()
            if self.pose is None:
                return
            t, x, y, z, roll, pitch, yaw = self.pose
            now = time.monotonic()
            dists = distances(self.parts, placed(self.models, self.poses), x, y, z, yaw)
            self.touch.update((m, now) for m in support_touch(dists))
            d, nearest, part = (None, "", "") if self.unknown else clearance(dists)
            ratio = self.rtf.value(now)
            if ratio is not None and ratio < RTF_OK:
                self.get_logger().warning("RTF %.2f < %g: времена и пути сценариев сейчас не в зачёт" % (ratio, RTF_OK),
                                          throttle_duration_sec=10.0)
            out = {"t": round(t, 3), "x": round(x, 4), "y": round(y, 4), "z": round(z, 4), "roll": round(roll, 4),
                   "pitch": round(pitch, 4), "yaw": round(yaw, 4), "v": round(self.vel[0], 4),
                   "w": round(self.vel[1], 4), "clearance_m": None if d is None else round(d, 4),
                   "nearest": nearest, "part": part, "unknown_models": sorted(self.unknown),
                   "contacts": sorted(m for m, ts in self.touch.items() if now - ts <= CONTACT_WINDOW_S),
                   "rtf": None if ratio is None else round(ratio, 3), "rtf_ok": ratio is not None and ratio >= RTF_OK}
            self.pub.publish(String(data=json.dumps(out, ensure_ascii=False)))

    rclpy.init()
    node = Truth()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.pool.shutdown(wait=False)
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
