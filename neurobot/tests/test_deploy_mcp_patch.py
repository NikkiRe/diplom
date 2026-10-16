"""Патчи neurobot к MCP-серверам лаборатории (≤ 20 добавленных строк, накладываются на присланные версии).

ros_mcp_server: пропатченный сервер отвечает по streamable HTTP, пишет в /cmd_vel_agent, ничего не печатает в stdout,
автостоп срабатывает и при исключении в цикле толчка (finally), и при SIGTERM (atexit); против подставного rosbridge.
robot_vision_mcp_proxy: без патча — дубли инструментов движения (FastMCP оставляет первую регистрацию, мс уходят
воркеру как секунды, N1), doctor видит это по tools/list; с патчем — дублей нет, длительность и скорость ограничены,
HTTP на 127.0.0.1, стоп воркеру при SIGTERM; против подставного воркера 8766.
Серверы лаборатории запускаются копией во временном каталоге. Нужны Python ≥ 3.10, mcp, websocket-client,
websockets; иначе тест пропускается."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "neurobot" / "deploy"
LAB = ROOT / "third_party/neurochip/ros_mcp_server/ros_mcp_server-master/ros_mcp_server.py"
PATCH = DEPLOY / "mcp/ros_mcp_server.patch"
LAB_PROXY = ROOT / "third_party/neurochip/robotic_platform/robotic_platform/robot_vision_mcp_proxy.py"
PROXY_PATCH = DEPLOY / "mcp/robot_vision_mcp_proxy.patch"
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))

from neurobot_deploy import doctor  # noqa: E402
from neurobot_deploy.config import Config  # noqa: E402
from neurobot_deploy.probes import McpClient, ProbeError, probe_mcp  # noqa: E402

try:
    import mcp  # noqa: F401
    import websocket  # noqa: F401
    from websockets.asyncio.server import serve
    DEPS = sys.version_info >= (3, 10)
except ImportError:
    DEPS = False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeRosbridge:
    """Принимает WebSocket rosbridge v2 и пишет (номер соединения, сообщение); соединения из reject — HTTP 500."""

    def __init__(self):
        self.messages, self.conns, self.reject = [], 0, set()
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        async def handler(ws):
            async for raw in ws:
                self.messages.append((ws.neurobot_no, json.loads(raw)))

        def process_request(conn, request):
            self.conns += 1
            conn.neurobot_no = self.conns
            if self.conns in self.reject:
                return conn.respond(HTTPStatus.INTERNAL_SERVER_ERROR, "reject\n")
            return None

        async def main():
            self.server = await serve(handler, "127.0.0.1", 0, process_request=process_request)
            self.port = self.server.sockets[0].getsockname()[1]
            ready.set()
            await self.server.serve_forever()

        self.thread = threading.Thread(target=lambda: self._run(main()), daemon=True)
        self.thread.start()
        ready.wait(5)

    def _run(self, coro):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(coro)
        except asyncio.CancelledError:
            pass

    def close(self):
        self.loop.call_soon_threadsafe(self.server.close)
        self.thread.join(5)

    def twists(self, after_conn: int = 0) -> list:
        return [(n, m["msg"]) for n, m in self.messages
                if m.get("op") == "publish" and m.get("topic") == "/cmd_vel_agent" and n > after_conn]


def is_zero(t: dict) -> bool:
    return not any(t["linear"].values()) and not any(t["angular"].values())


@unittest.skipUnless(DEPS, "нужны Python ≥ 3.10, mcp, websocket-client, websockets")
class PatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        shutil.copy(LAB, self.tmp)
        r = subprocess.run(["patch", "-p1", "--quiet", "-i", str(PATCH)], cwd=self.tmp, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.bridge = FakeRosbridge()
        self.addCleanup(self.bridge.close)
        self.port = free_port()
        cfg = {"server": {"host": "127.0.0.1", "port": self.port},
               "rosbridge": {"url": "ws://127.0.0.1:{}".format(self.bridge.port), "timeout_s": 3.0},
               "topics": {"cmd_vel": {"name": "/cmd_vel_agent", "type": "geometry_msgs/msg/Twist", "access": "write"}}}
        with open(os.path.join(self.tmp, "cfg.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        self.proc = subprocess.Popen([sys.executable, "ros_mcp_server.py", "--config", "cfg.json"], cwd=self.tmp,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.addCleanup(self._kill)
        self.url = "http://127.0.0.1:{}/mcp".format(self.port)
        deadline = time.monotonic() + 20
        while True:
            try:
                self.tools = probe_mcp(self.url, 2.0).tools
                break
            except ProbeError:
                if time.monotonic() > deadline or self.proc.poll() is not None:
                    self.fail("сервер не поднялся: " + self._kill()[1].decode(errors="replace")[-2000:])
                time.sleep(0.2)

    def _kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        return self.proc.communicate(timeout=10)

    def call(self, name: str, args: dict) -> dict:
        c = McpClient(self.url, 20.0)
        try:
            c.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                 "clientInfo": {"name": "test", "version": "1"}})
            c.notify("notifications/initialized")
            return c.rpc("tools/call", {"name": name, "arguments": args})
        finally:
            c.close()

    def test_patch_is_small(self):
        lines = PATCH.read_text(encoding="utf-8").splitlines()
        added = [ln for ln in lines if ln.startswith("+") and not ln.startswith("+++")]
        self.assertLessEqual(len(added), 20)
        self.assertEqual(self.tools, ["robot_get_status", "robot_stop", "robot_nudge"])

    def test_nudge_goes_to_agent_topic_then_zero(self):
        res = self.call("robot_nudge", {"direction": "forward", "duration_ms": 200, "speed": 0.05})
        self.assertFalse(res.get("isError"), res)
        twists = [t for _, t in self.bridge.twists()]
        moving = [t for t in twists if not is_zero(t)]
        self.assertTrue(moving and all(t["linear"]["x"] == 0.05 for t in moving))
        self.assertEqual([is_zero(t) for t in twists[-5:]], [True] * 5)
        self.assertFalse(any(m.get("topic") == "/cmd_vel" for _, m in self.bridge.messages))

    def test_auto_stop_when_publish_fails_mid_nudge(self):
        before = self.bridge.conns
        self.bridge.reject = {before + 2}  # второе соединение толчка отвергнуто — исключение в цикле
        res = self.call("robot_nudge", {"direction": "turn_left", "duration_ms": 500, "speed": 0.3})
        self.assertTrue(res.get("isError"), res)
        after = [t for n, t in self.bridge.twists(after_conn=before + 2)]
        self.assertEqual([is_zero(t) for t in after], [True] * 5)

    def test_sigterm_publishes_zero_and_stdout_stays_clean(self):
        before = self.bridge.conns
        self.proc.send_signal(signal.SIGTERM)
        out, err = self.proc.communicate(timeout=15)
        self.assertEqual(self.proc.returncode, 0, err.decode(errors="replace")[-1500:])
        self.assertEqual(out, b"")
        self.assertEqual([is_zero(t) for _, t in self.bridge.twists(after_conn=before)], [True] * 5)


# Запуск прокси лаборатории из копии: адрес воркера подменяется до старта; «main» — как в юните (после патча),
# иначе — тот же модуль без патча, но по HTTP (у него только stdio), чтобы посмотреть его инструменты.
PROXY_RUNNER = """
import sys
sys.path.insert(0, sys.argv[1])
import robot_vision_mcp_proxy as m
m.WORKER_URL = sys.argv[2]
if sys.argv[3] == "main":
    m.main()
else:
    m.mcp.settings.host, m.mcp.settings.port = "127.0.0.1", int(sys.argv[4])
    m.mcp.run(transport="streamable-http")
"""


class FakeWorker:
    """Подставной «Robot Vision Demo Worker»: пишет (метод, путь, тело) и отвечает {"ok": true}."""

    def __init__(self):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, body):
                outer.requests.append((self.command, self.path, body))
                data = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._reply(None)

            def do_POST(self):
                self._reply(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:{}".format(self.httpd.server_address[1])
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def posts(self, path: str) -> list:
        return [b for m, p, b in self.requests if m == "POST" and p == path]


@unittest.skipUnless(DEPS, "нужны Python ≥ 3.10, mcp, websocket-client, websockets")
class ProxyPatchTest(unittest.TestCase):
    def start(self, patched: bool):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        shutil.copy(LAB_PROXY, tmp)
        if patched:
            r = subprocess.run(["patch", "-p1", "--quiet", "-i", str(PROXY_PATCH)], cwd=tmp, capture_output=True,
                               text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.worker = FakeWorker()
        self.addCleanup(self.worker.close)
        port = free_port()
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", ROBOT_VISION_MCP_PORT=str(port))
        self.proc = subprocess.Popen([sys.executable, "-c", PROXY_RUNNER, tmp, self.worker.url,
                                      "main" if patched else "plain", str(port)], cwd=tmp,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.addCleanup(self._kill)
        self.url = "http://127.0.0.1:{}/mcp".format(port)
        deadline = time.monotonic() + 20
        while True:
            try:
                return probe_mcp(self.url, 2.0)
            except ProbeError:
                if time.monotonic() > deadline or self.proc.poll() is not None:
                    self.fail("прокси не поднялся: " + self._kill()[1].decode(errors="replace")[-2000:])
                time.sleep(0.2)

    def _kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        return self.proc.communicate(timeout=10)

    def call(self, name: str, args: dict) -> dict:
        c = McpClient(self.url, 20.0)
        try:
            c.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                 "clientInfo": {"name": "test", "version": "1"}})
            c.notify("notifications/initialized")
            return c.rpc("tools/call", {"name": name, "arguments": args})
        finally:
            c.close()

    def doctor_mcp(self) -> doctor.Finding:
        return doctor._check_mcp(Config({"NEUROBOT_MCP_URL": self.url, "NEUROBOT_MCP_STATUS_TOOL": ""}, "/e/n.env"))

    def test_lab_proxy_duplicates_send_ms_as_seconds(self):
        res = self.start(patched=False)
        self.assertEqual(res.params["robot_nudge_forward"], ["duration_ms", "speed"])  # первая регистрация (N1)
        self.call("robot_nudge_forward", {})
        self.assertEqual(self.worker.posts("/motion/nudge"),
                         [{"direction": "forward", "duration_s": 2000, "speed": 0.8}])
        f = self.doctor_mcp()
        self.assertEqual((f.level, f.code), (doctor.FAIL, "mcp_schema"))
        self.assertIn("Tool already exists", self._kill()[1].decode(errors="replace"))

    def test_patched_proxy(self):
        lines = PROXY_PATCH.read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len([ln for ln in lines if ln.startswith("+") and not ln.startswith("+++")]), 20)
        res = self.start(patched=True)
        self.assertEqual(len(res.tools), len(set(res.tools)))
        self.assertFalse([t for t, p in res.params.items() if "duration_ms" in p])
        self.assertEqual(self.doctor_mcp().level, doctor.OK)
        self.call("robot_nudge_forward", {})
        self.call("robot_nudge", {"direction": "turn_left", "duration_s": 300, "speed": 0.8})
        # NaN и ±inf до воркера не доходят: FastMCP отклоняет их при проверке аргументов (mcp 1.30)
        for bad in (float("nan"), float("inf"), -float("inf")):
            self.assertTrue(self.call("robot_nudge", {"direction": "forward", "duration_s": bad})["isError"])
        self.assertEqual(self.worker.posts("/motion/nudge"),
                         [{"direction": "forward", "duration_s": 0.3, "speed": 0.25},
                          {"direction": "turn_left", "duration_s": 0.5, "speed": 0.25}])
        self.proc.send_signal(signal.SIGTERM)
        _, err = self.proc.communicate(timeout=15)
        self.assertEqual(self.proc.returncode, 0, err.decode(errors="replace")[-1500:])
        self.assertEqual(self.worker.posts("/motion/stop"), [{}])
        self.assertNotIn("Tool already exists", err.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
