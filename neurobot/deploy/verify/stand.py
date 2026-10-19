"""Общее для замеров verify/ (k6.py, recovery.py) на роботе или двойнике (root, окружение ROS): фоновый узел rclpy с
записью тем, MCP-сессия как у агента, юниты systemd, квантили. DDS и адреса — из env-файла пакета, как у CLI neurobot
(NEUROBOT_ENV, по умолчанию /etc/neurobot/neurobot.env). Разбор записей — чистые функции без rclpy (тесты:
neurobot/tests/test_deploy_verify.py). Метки — монотонные часы этого процесса при приёме, то есть с доставкой DDS до
него. Логи — в stderr.
"""
from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEPLOY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if DEPLOY not in sys.path:
    sys.path.insert(0, DEPLOY)

from neurobot_deploy.config import Config  # noqa: E402
from neurobot_deploy.probes import McpClient  # noqa: E402

log = logging.getLogger("verify")
Twist3 = Tuple[float, float, float]   # (t, linear.x, angular.z)


def quantiles(values: Sequence[Optional[float]]) -> Dict[str, Optional[float]]:
    """n, p50, p95, max (линейная интерполяция, как у прогонов A0/A1 и наборов двойника); None не в счёт."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return {"n": 0, "p50": None, "p95": None, "max": None}

    def q(p: float) -> float:
        k = (len(v) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(v) - 1)
        return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 4)
    return {"n": len(v), "p50": q(0.5), "p95": q(0.95), "max": round(v[-1], 4)}


def first(rows: Sequence[tuple], t0: float, pred) -> Optional[float]:
    """Момент первой записи (t, …) не раньше t0, для которой pred(запись) истинно, или None."""
    return next((r[0] for r in rows if r[0] >= t0 and pred(r)), None)


def zero(r: Twist3) -> bool:
    return r[1] == 0.0 and r[2] == 0.0


def wrap(a: float) -> float:
    return math.atan2(math.sin(a), math.cos(a))


def systemctl(*args: str, check: bool = True) -> str:
    out = subprocess.run(["systemctl", *args], capture_output=True, text=True)
    if check and out.returncode:
        raise RuntimeError("systemctl {}: {}".format(" ".join(args), out.stderr.strip()))
    return out.stdout.strip()


def unit_props(unit: str, *names: str) -> Dict[str, str]:
    out = systemctl("show", unit, *["-p" + n for n in names])
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def unit_pids(unit: str) -> List[int]:
    """Процессы юнита по его cgroup (cgroup v2), по возрастанию PID."""
    cgroup = unit_props(unit, "ControlGroup").get("ControlGroup", "")
    try:
        with open("/sys/fs/cgroup{}/cgroup.procs".format(cgroup)) as f:
            return sorted(int(p) for p in f.read().split())
    except OSError:
        return []


def cmdline(pid: int) -> str:
    try:
        with open("/proc/{}/cmdline".format(pid), "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


class Mcp:
    """Одна сессия MCP на весь замер, как у PicoClaw: initialize заранее, в замер идёт только tools/call."""

    def __init__(self, url: str, timeout: float):
        self.client = McpClient(url, timeout)
        self.client.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                       "clientInfo": {"name": "neurobot-verify", "version": "1"}})
        self.client.notify("notifications/initialized")

    def call(self, tool: str, args: dict) -> Tuple[float, float, Any]:
        """(момент перед запросом, момент ответа, JSON-ответ инструмента или текст ошибки)."""
        t0 = time.monotonic()
        try:
            res = self.client.rpc("tools/call", {"name": tool, "arguments": args})
            text = "".join(c.get("text", "") for c in (res or {}).get("content", []))
            try:
                out: Any = json.loads(text)
            except ValueError:
                out = text
        except Exception as e:  # noqa: BLE001 — ответ сервера (ProbeError) или обрыв: в замер идёт как есть
            out = "ошибка: {}".format(e)
        return t0, time.monotonic(), out

    def close(self) -> None:
        self.client.close()


class Rec:
    """Фоновый узел rclpy: подписки пишут (t, …) в rec[тема]; публикации Twist — из любого потока."""

    def __init__(self, cfg: Config, name: str):
        os.environ.update(cfg.ros_environ())
        import rclpy
        import rclpy.executors
        import rclpy.qos
        self.rclpy, self.qos = rclpy, rclpy.qos
        self.ctx = rclpy.Context()
        rclpy.init(args=["--ros-args", "--disable-external-lib-logs"], context=self.ctx)
        self.node = rclpy.create_node("neurobot_{}_{}".format(name, os.getpid()), context=self.ctx)
        self.rec: Dict[str, List[tuple]] = {}
        self._pubs: Dict[str, Any] = {}
        self._ex = rclpy.executors.SingleThreadedExecutor(context=self.ctx)
        self._ex.add_node(self.node)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def start(self) -> "Rec":
        self._thread.start()
        return self

    def _spin(self) -> None:
        while not self._stop.is_set():
            self._ex.spin_once(timeout_sec=0.05)

    def _sub(self, msg_type, topic: str, key: str, row, qos=None) -> None:
        rows = self.rec.setdefault(key, [])
        self.node.create_subscription(msg_type, topic, lambda m: rows.append(row(time.monotonic(), m)),
                                      qos or self.qos.qos_profile_sensor_data)

    def twist(self, topic: str) -> None:
        from geometry_msgs.msg import Twist
        self._sub(Twist, topic, topic, lambda t, m: (t, m.linear.x, m.angular.z), 50)

    def json(self, topic: str) -> None:
        from std_msgs.msg import String

        def row(t, m):
            try:
                return t, json.loads(m.data)
            except ValueError:
                return t, {}
        self._sub(String, topic, topic, row, 50)

    def odom(self, topic: str = "/odom") -> None:
        from nav_msgs.msg import Odometry

        def row(t, m):
            q = m.pose.pose.orientation
            return t, m.pose.pose.position.x, m.pose.pose.position.y, math.atan2(
                2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
        self._sub(Odometry, topic, "odom", row)

    def scan(self, topic: str, lo_deg: float, hi_deg: float) -> None:
        """(t, ближайшая достоверная дальность в секторе [lo, hi]°, м или inf): достоверная — конечная и в
        [range_min, range_max] скана."""
        from sensor_msgs.msg import LaserScan
        self._sub(LaserScan, topic, "scan", lambda t, m: (t, nearest(m, lo_deg, hi_deg)))

    def pub(self, topic: str, v: float, w: float) -> None:
        from geometry_msgs.msg import Twist
        if topic not in self._pubs:
            self._pubs[topic] = self.node.create_publisher(Twist, topic, 10)
        msg = Twist()
        msg.linear.x, msg.angular.z = float(v), float(w)
        self._pubs[topic].publish(msg)

    def last(self, key: str) -> Optional[tuple]:
        rows = self.rec.get(key) or []
        return rows[-1] if rows else None

    def since(self, key: str, t0: float) -> List[tuple]:
        return [r for r in list(self.rec.get(key) or []) if r[0] >= t0]

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2.0)
        self._ex.remove_node(self.node)
        self.node.destroy_node()
        self.rclpy.shutdown(context=self.ctx)


def nearest(msg, lo_deg: float, hi_deg: float) -> float:
    """Ближайшая достоверная дальность скана в секторе углов [lo_deg, hi_deg] (кадр лидара), м; нет — inf."""
    best = math.inf
    lo, hi = math.radians(lo_deg), math.radians(hi_deg)
    for i, r in enumerate(msg.ranges):
        a = msg.angle_min + i * msg.angle_increment
        if lo <= a <= hi and math.isfinite(r) and msg.range_min <= r <= msg.range_max:
            best = min(best, r)
    return best


def ahead(rec: Rec, bx: float, by: float) -> Tuple[float, float]:
    """Только двойник: точка (bx, by) кадра базы в мире — по позе робота из истины (/twin/truth)."""
    tr = rec.last("/twin/truth")
    if tr is None:
        raise RuntimeError("нет /twin/truth (истина двойника)")
    r = tr[1]
    return (r["x"] + bx * math.cos(r["yaw"]) - by * math.sin(r["yaw"]),
            r["y"] + bx * math.sin(r["yaw"]) + by * math.cos(r["yaw"]))


def restart_armed(rec: Rec, cfg: Config, unit: str, timeout: float = 30.0) -> bool:
    """Рестарт юнита safety_gate и ожидание взвода: свежее состояние (rec.json на теме состояния), не estop, поворот
    разрешён."""
    t0 = time.monotonic()
    systemctl("restart", unit)
    while time.monotonic() < t0 + timeout:
        r = rec.last(cfg.get("NEUROBOT_SAFETY_STATE_TOPIC"))
        if r and r[0] > t0 and r[1].get("state") != "estop" and ((r[1].get("limits") or {}).get("w_max") or 0) > 0:
            return True
        time.sleep(0.1)
    log.error("safety_gate не взвёлся после рестарта за %.0f с", timeout)
    return False


class World:
    """Только двойник: сервисы мира Gazebo (ros_gz_interfaces, мост twin.launch.py) — создать столб, переставить,
    удалить модель. Ответ ждём, пока узел крутит фоновый поток Rec."""

    def __init__(self, node):
        from ros_gz_interfaces.srv import DeleteEntity, SetEntityPose, SpawnEntity
        self.node = node
        self.cli = {n: node.create_client(t, "/world/default/" + n) for n, t in
                    (("set_pose", SetEntityPose), ("create", SpawnEntity), ("remove", DeleteEntity))}

    def _call(self, name: str, req) -> bool:
        cli = self.cli[name]
        if not cli.wait_for_service(timeout_sec=10.0):
            return False
        fut = cli.call_async(req)
        end = time.monotonic() + 10.0
        while not fut.done() and time.monotonic() < end:
            time.sleep(0.01)
        return bool(fut.done() and fut.result() is not None and fut.result().success)

    def create_pole(self, name: str, x: float, y: float) -> None:
        from ros_gz_interfaces.srv import SpawnEntity
        req = SpawnEntity.Request()
        geo = "<geometry><cylinder><radius>0.025</radius><length>1.0</length></cylinder></geometry>"
        req.entity_factory.name = name
        req.entity_factory.sdf = ("<sdf version='1.9'><model name='{0}'><static>true</static><link name='link'>"
                                  "<collision name='c'>{1}</collision><visual name='v'>{1}</visual></link></model>"
                                  "</sdf>").format(name, geo)
        req.entity_factory.pose.position.x, req.entity_factory.pose.position.y = float(x), float(y)
        req.entity_factory.pose.position.z = 0.5
        if not self._call("create", req):
            raise RuntimeError("create {} не выполнен".format(name))

    def set_pose(self, name: str, x: float, y: float) -> None:
        from ros_gz_interfaces.msg import Entity
        from ros_gz_interfaces.srv import SetEntityPose
        req = SetEntityPose.Request()
        req.entity.name, req.entity.type = name, Entity.MODEL
        req.pose.position.x, req.pose.position.y, req.pose.position.z = float(x), float(y), 0.5
        req.pose.orientation.w = 1.0
        if not self._call("set_pose", req):
            raise RuntimeError("set_pose {} не выполнен".format(name))

    def remove(self, name: str) -> None:
        from ros_gz_interfaces.msg import Entity
        from ros_gz_interfaces.srv import DeleteEntity
        req = DeleteEntity.Request()
        req.entity.name, req.entity.type = name, Entity.MODEL
        self._call("remove", req)
