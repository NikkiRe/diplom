"""Обвязка наборов на двойнике (в ВМ sim, root, при поднятом neurobot.target из setup_twin.sh): узел rclpy, который
пишет темы пакета и истину Gazebo, сервисы мира, юниты пакета и пути агента (MCP, WebSocket rosbridge). Окружение DDS
и адреса — из env-файла пакета (/etc/neurobot/neurobot.env), как у CLI neurobot. Логи — в stderr.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import pty
import select
import socket
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

sys.path.insert(0, "/opt/neurobot/lib")

from neurobot_deploy.config import load  # noqa: E402
from neurobot_deploy.probes import McpClient  # noqa: E402

log = logging.getLogger("suites")
CFG = load()
os.environ.update(CFG.ros_environ())

import rclpy  # noqa: E402
from geometry_msgs.msg import Twist  # noqa: E402
from nav_msgs.msg import Odometry  # noqa: E402
from ros_gz_interfaces.msg import Entity  # noqa: E402
from ros_gz_interfaces.srv import DeleteEntity, SetEntityPose, SpawnEntity  # noqa: E402
from std_msgs.msg import String  # noqa: E402

HZ = 20.0
WORLD = "/world/default/"
UNITS = ("neurobot-driver@twin", "neurobot-safety", "neurobot-executor", "neurobot-perception", "neurobot-rosbridge",
         "neurobot-mcp", "neurobot-picoclaw", "neurobot-watchdog")
JSON_TOPICS = {"truth": "/twin/truth", "state": "/neurobot/safety_state", "motion": "/neurobot/motion_state",
               "events": "/neurobot/events", "gesture": "/neurobot/gesture", "health": "/neurobot/perception_health",
               "estop": CFG.get("NEUROBOT_ESTOP_TOPIC")}


def yaw_of(q) -> float:
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


class Twin:
    """Записи (self.rec[имя] — список словарей с «t», монотонные секунды) и действия над двойником и пакетом."""

    def __init__(self, name: str = "neurobot_suites"):
        rclpy.init()
        self.node = rclpy.create_node(name)
        self.rec: Dict[str, List[dict]] = {k: [] for k in list(JSON_TOPICS) + ["cmd", "mux", "odom"]}
        for key, topic in JSON_TOPICS.items():
            self.node.create_subscription(String, topic, self._json(key), 50)
        for key, topic in (("cmd", CFG.get("NEUROBOT_CMD_VEL")), ("mux", CFG.get("NEUROBOT_BASE_CMD_TOPIC"))):
            self.node.create_subscription(Twist, topic, self._twist(key), 50)
        self.node.create_subscription(Odometry, "/odom", self._odom, 50)
        self._pubs: Dict[str, object] = {}
        self._cli = {n: self.node.create_client(t, WORLD + n) for n, t in
                     (("set_pose", SetEntityPose), ("create", SpawnEntity), ("remove", DeleteEntity))}

    def _json(self, key: str):
        def cb(msg):
            try:
                self.rec[key].append(dict(json.loads(msg.data), t=time.monotonic()))
            except ValueError:
                log.warning("%s: не JSON", key)
        return cb

    def _twist(self, key: str):
        return lambda m: self.rec[key].append({"t": time.monotonic(), "v": m.linear.x, "w": m.angular.z})

    def _odom(self, m) -> None:
        p, t = m.pose.pose, m.twist.twist
        self.rec["odom"].append({"t": time.monotonic(), "x": p.position.x, "y": p.position.y,
                                 "yaw": yaw_of(p.orientation), "v": t.linear.x, "w": t.angular.z})

    def close(self) -> None:
        self.node.destroy_node()
        rclpy.try_shutdown()

    # ---- время и движение

    def spin(self, seconds: float, until: Optional[Callable[[], bool]] = None,
             drive: Optional[Dict[str, tuple]] = None) -> bool:
        """Крутить узел seconds или до until(); drive {тема: (v, w)} — публиковать с частотой HZ."""
        end, nxt = time.monotonic() + seconds, 0.0
        while time.monotonic() < end:
            if until and until():
                return True
            now = time.monotonic()
            if drive and now >= nxt:
                for topic, (v, w) in drive.items():
                    msg = Twist()
                    msg.linear.x, msg.angular.z = float(v), float(w)
                    self.pub(topic).publish(msg)
                nxt = now + 1.0 / HZ
            rclpy.spin_once(self.node, timeout_sec=0.005)
        return bool(until and until())

    def pub(self, topic: str):
        if topic not in self._pubs:
            self._pubs[topic] = self.node.create_publisher(Twist, topic, 10)
        return self._pubs[topic]

    def pilot(self, topic: str, cmd) -> threading.Event:
        """Пилот в своём потоке: cmd в topic с частотой HZ, пока не взведено возвращённое событие (главный поток в это
        время может ждать CLI, MCP и systemd — вход исполнителя не устаревает)."""
        stop, pub = threading.Event(), self.pub(topic)
        msg = Twist()
        msg.linear.x, msg.angular.z = float(cmd[0]), float(cmd[1])

        def run():
            while not stop.wait(1.0 / HZ):
                pub.publish(msg)
        threading.Thread(target=run, daemon=True).start()
        return stop

    def unpub(self, topic: str) -> None:
        """Убрать издателя (его наличие видят safety_gate и doctor: /cmd_vel_joy — перехват twist_mux)."""
        if topic in self._pubs:
            self.node.destroy_publisher(self._pubs.pop(topic))

    def last(self, key: str) -> Optional[dict]:
        return self.rec[key][-1] if self.rec[key] else None

    def since(self, key: str, t0: float, t1: float = math.inf) -> List[dict]:
        return [r for r in self.rec[key] if t0 <= r["t"] <= t1]

    # ---- мир Gazebo

    def _call(self, name: str, req) -> bool:
        cli = self._cli[name]
        if not cli.wait_for_service(timeout_sec=10.0):
            raise RuntimeError("нет сервиса " + WORLD + name)
        fut = cli.call_async(req)
        end = time.monotonic() + 10.0
        while not fut.done() and time.monotonic() < end:
            rclpy.spin_once(self.node, timeout_sec=0.01)
        return bool(fut.done() and fut.result().success)

    def set_pose(self, name: str, x: float, y: float, z: float = 0.01, yaw: float = 0.0, pitch: float = 0.0) -> None:
        req = SetEntityPose.Request()
        req.entity.name, req.entity.type = name, Entity.MODEL
        req.pose.position.x, req.pose.position.y, req.pose.position.z = float(x), float(y), float(z)
        cy, sy, cp, sp = math.cos(yaw / 2), math.sin(yaw / 2), math.cos(pitch / 2), math.sin(pitch / 2)
        o = req.pose.orientation
        o.w, o.x, o.y, o.z = cy * cp, -sy * sp, cy * sp, sy * cp
        if not self._call("set_pose", req):
            raise RuntimeError("set_pose {} не выполнен".format(name))

    def create_box(self, name: str, x: float, y: float, size: tuple, yaw: float = 0.0) -> None:
        """Статичный ящик (sx, sy, sz) на полу."""
        sx, sy, sz = size
        geo = "<geometry><box><size>{} {} {}</size></box></geometry>".format(sx, sy, sz)
        self._create(name, x, y, sz / 2, yaw, geo)

    def create_pole(self, name: str, x: float, y: float, radius: float, height: float) -> None:
        geo = "<geometry><cylinder><radius>{}</radius><length>{}</length></cylinder></geometry>".format(radius, height)
        self._create(name, x, y, height / 2, 0.0, geo)

    def _create(self, name: str, x: float, y: float, z: float, yaw: float, geo: str) -> None:
        req = SpawnEntity.Request()
        f = req.entity_factory
        f.name = name
        f.sdf = ("<sdf version='1.9'><model name='{0}'><static>true</static><link name='link'><collision name='c'>{1}"
                 "</collision><visual name='v'>{1}</visual></link></model></sdf>").format(name, geo)
        f.pose.position.x, f.pose.position.y, f.pose.position.z = float(x), float(y), float(z)
        f.pose.orientation.z, f.pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)
        if not self._call("create", req):
            raise RuntimeError("create {} не выполнен".format(name))

    def remove(self, name: str) -> None:
        req = DeleteEntity.Request()
        req.entity.name, req.entity.type = name, Entity.MODEL
        self._call("remove", req)

    def known(self, name: str, timeout: float = 5.0) -> bool:
        """Модель появилась в истине и её форма известна (после create — ≈0,5 с): сначала она в unknown_models."""
        seen = []

        def ok():
            r = self.last("truth")
            if r and r["t"] > t0 and name in r.get("unknown_models", []):
                seen.append(r["t"])
            return bool(seen) and name not in r.get("unknown_models", [])
        t0 = time.monotonic()
        return self.spin(timeout, until=ok)

    # ---- юниты пакета

    @staticmethod
    def systemctl(*args: str, check: bool = True) -> str:
        out = subprocess.run(["systemctl", *args], capture_output=True, text=True)
        if check and out.returncode:
            raise RuntimeError("systemctl {}: {}".format(" ".join(args), out.stderr.strip()))
        return out.stdout.strip()

    def main_pid(self, unit: str) -> int:
        return int(self.systemctl("show", "-p", "MainPID", "--value", unit))

    def kill9(self, unit: str) -> int:
        """kill -9 главного процесса юнита; PID."""
        pid = self.main_pid(unit)
        if pid <= 0:
            raise RuntimeError("{} не запущен".format(unit))
        os.kill(pid, 9)
        return pid

    def active(self, unit: str) -> bool:
        return self.systemctl("is-active", unit, check=False) == "active"

    def wait_active(self, units, timeout: float = 120.0) -> bool:
        return self.spin(timeout, until=lambda: all(self.active(u) for u in units))

    def armed(self, timeout: float = 30.0) -> bool:
        """safety_gate свежего процесса взведён: пределы не все нулевые (вперёд при этом может быть нельзя)."""
        t0 = time.monotonic()

        def ok():
            s = self.last("state")
            return bool(s) and s["t"] > t0 and s["state"] != "estop" and (s["limits"]["w_max"] or 0.0) > 0.0
        return self.spin(timeout, until=ok)

    def relocate(self, x: float, y: float, z: float = 0.01, yaw: float = 0.0, pitch: float = 0.0,
                 arm: bool = True) -> None:
        """Переставить робота при выключенном safety_gate и поднять его заново; arm — дождаться взвода (не взвёлся —
        RuntimeError: защёлка прошлого сценария или датчики). Скачок позы при
        set_pose — «удар» для IMU (защёлка shock), а память глубины прежнего места к новому не относится (одометрия
        телепорт не видит): как будто робот перенесли с выключенной защитой. Ноль в /cmd_vel даёт ExecStopPost."""
        self.systemctl("stop", "neurobot-safety")
        self.set_pose("neurochip", x, y, z, yaw, pitch)
        self.spin(1.5)
        self.systemctl("start", "neurobot-safety")
        if arm and not self.armed():
            raise RuntimeError("safety_gate не взвёлся после переноса: {}".format(
                (self.last("state") or {}).get("reasons")))

    def unit_pid(self, unit: str, pattern: str) -> int:
        """PID процесса юнита, в командной строке которого есть pattern (ВМ общая: чужие двойники не в счёт)."""
        cgroup = self.systemctl("show", "-p", "ControlGroup", "--value", unit)
        with open("/sys/fs/cgroup{}/cgroup.procs".format(cgroup)) as f:
            for pid in f.read().split():
                with open("/proc/{}/cmdline".format(pid), "rb") as c:
                    if pattern.encode() in c.read():
                        return int(pid)
        raise RuntimeError("в {} нет процесса {}".format(unit, pattern))

    @staticmethod
    def cli(*args: str, answer: Optional[str] = None, timeout: float = 60.0) -> dict:
        """CLI neurobot; answer — ответ человека в псевдотерминале (stdin — терминал), иначе stdin не терминал."""
        if answer is None:
            out = subprocess.run(["neurobot", *args], capture_output=True, text=True, timeout=timeout,
                                 stdin=subprocess.DEVNULL)
            return {"rc": out.returncode, "out": out.stdout + out.stderr}
        pid, fd = pty.fork()
        if pid == 0:
            os.execvp("neurobot", ["neurobot", *args])
        text, sent, end = b"", False, time.monotonic() + timeout
        while time.monotonic() < end:
            if not select.select([fd], [], [], 0.2)[0]:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            text += chunk
            if not sent and "Введите".encode() in text:
                os.write(fd, answer.encode() + b"\n")
                sent = True
        _, status = os.waitpid(pid, 0)
        return {"rc": os.waitstatus_to_exitcode(status), "out": text.decode("utf-8", "replace")}

    @staticmethod
    def journal_text(units, since_wall: float) -> str:
        """Журнал юнитов с момента since_wall (секунды эпохи)."""
        cmd = ["journalctl", "--no-pager", "-o", "short-iso-precise", "--since", "@{:.3f}".format(since_wall)]
        for u in units:
            cmd += ["-u", u]
        return subprocess.run(cmd, capture_output=True, text=True).stdout

    def journal(self, units, since_wall: float, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.journal_text(units, since_wall))

    # ---- агентский путь: MCP и WebSocket rosbridge

    @staticmethod
    def _mcp(method: str, params: dict, timeout: float) -> dict:
        c = McpClient(CFG.get("NEUROBOT_MCP_URL"), timeout)
        try:
            c.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                 "clientInfo": {"name": "suites", "version": "1"}})
            c.notify("notifications/initialized")
            return c.rpc(method, params)
        finally:
            c.close()

    def mcp(self, tool: str, args: dict, timeout: float = 20.0) -> dict:
        """Вызов инструмента MCP, как у агента (без LLM)."""
        return self._mcp("tools/call", {"name": tool, "arguments": args}, timeout)

    def mcp_tools(self) -> list:
        """Инструменты, которые MCP-сервер показывает агенту."""
        return self._mcp("tools/list", {}, 10.0)["tools"]

    @staticmethod
    def rosbridge_publish(topic: str, msg_type: str, msg: dict, port: int) -> str:
        """Клиент WebSocket rosbridge (как агент, нашедший адрес): advertise + publish; ответ сервера за 1 с."""
        s = socket.create_connection(("127.0.0.1", port), timeout=3.0)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall(("GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   "Sec-WebSocket-Key: {}\r\nSec-WebSocket-Version: 13\r\n\r\n").format(key).encode())
        if b" 101 " not in s.recv(4096).split(b"\r\n", 1)[0]:
            raise RuntimeError("rosbridge: нет 101 на рукопожатие")
        for op in ({"op": "advertise", "topic": topic, "type": msg_type},
                   {"op": "publish", "topic": topic, "msg": msg}):
            data, mask = json.dumps(op).encode(), os.urandom(4)
            head = bytes([0x81]) + (bytes([0x80 | len(data)]) if len(data) < 126 else
                                    bytes([0x80 | 126]) + len(data).to_bytes(2, "big"))
            s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        s.settimeout(1.0)
        reply = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                reply += chunk
        except socket.timeout:
            pass
        s.close()
        return reply.decode("utf-8", "replace")
