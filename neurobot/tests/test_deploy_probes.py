"""neurobot/deploy: разбор журнала PicoClaw (строки 0.2.8 и 0.3.1 одинаковы), WebSocket-проба rosbridge (рукопожатие и
постоянное соединение с ping сторожа), MCP-проба против подставного сервера (JSON, SSE, зависание), HTTP-проба
воркера. Только stdlib, Python 3.8+."""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))
sys.path.insert(0, str(DEPLOY / "fakes"))

import fake_component  # noqa: E402
from neurobot_deploy import __main__ as cli  # noqa: E402
from neurobot_deploy import picolog  # noqa: E402
from neurobot_deploy.probes import ProbeError, WsPing, http_get, probe_mcp, ws_handshake  # noqa: E402
from neurobot_deploy.sd import Entry  # noqa: E402

CALLER = "../../../runner/work/picoclaw/picoclaw/pkg"


def line(ts: float, text: str) -> Entry:
    return Entry(ts, 6, text, "inv")


START = '03:14:08 INF tool {}/tools/registry.go:258 > Tool execution started args={{"tool=x":1}} tool={}'
DONE = "03:14:08 INF tool {}/tools/registry.go:353 > Tool execution completed duration_ms=4 result_length=117 tool={}"
FAILED = ("03:14:09 ERR tool {}/tools/registry.go:340 > Tool execution failed duration=3 error=MCP tool execution "
          "failed: failed to call tool: connection closed: calling \"tools/call\": client is closing: EOF tool={}")


class PicologTest(unittest.TestCase):
    def test_healthy_start(self):
        log = picolog.analyze([
            line(1, "03:13:35 INF mcp {}/mcp/manager.go:461 > Connected to MCP server protocol=2025-11-25 "
                    "server=robot_mcp serverName=ros_mcp_server serverVersion=1.30.0".format(CALLER)),
            line(2, "03:13:35 INF mcp {}/mcp/manager.go:281 > MCP server initialization complete".format(CALLER)),
            line(3, START.format(CALLER, "mcp_robot_mcp_robot_nudge")),
            line(4, DONE.format(CALLER, "mcp_robot_mcp_robot_nudge")),
        ])
        self.assertEqual((log.connected, log.init_done, log.fatal, log.pending), (["robot_mcp"], True, None, []))

    def test_zombie_is_final(self):
        log = picolog.analyze([line(1, "ERR mcp > All MCP servers failed to connect error=…"),
                               line(2, START.format(CALLER, "mcp_ros_mcp_robot_get_status")),
                               line(3, DONE.format(CALLER, "mcp_ros_mcp_robot_get_status"))])
        self.assertEqual(log.fatal.code, "agent_zombie")

    def test_closed_connection_until_next_success(self):
        entries = [line(1, START.format(CALLER, "mcp_ros_mcp_robot_nudge")),
                   line(2, FAILED.format(CALLER, "mcp_ros_mcp_robot_nudge"))]
        log = picolog.analyze(entries)
        self.assertEqual((log.fatal.code, log.fatal.ts, log.pending), ("agent_mcp_lost", 2, []))
        entries += [line(5, START.format(CALLER, "mcp_ros_mcp_robot_stop")),
                    line(6, DONE.format(CALLER, "mcp_ros_mcp_robot_stop"))]
        self.assertIsNone(picolog.analyze(entries).fatal)
        noise = [line(1, "WRN mcp > read error: invalid trailing data at the end of stream")]
        self.assertEqual(picolog.analyze(noise).fatal.code, "agent_mcp_lost")

    def test_vk_long_poll_deaf_but_not_on_shutdown(self):
        url = r'Post \"https://lp.vk.com/whp1?act=a_check&key={}&ts=7&wait=25\"'
        deaf = 'ERR vk > Long poll failed error="{}: context deadline exceeded"'.format(url.format("0123456789abcdef"))
        stop = 'ERR vk > Long poll failed error="{}: context canceled"'.format(url.format("eyJhbGciOi.eyJzdWIi.c2ln"))
        self.assertIsNone(picolog.analyze([line(1, stop)]).fatal)
        log = picolog.analyze([line(1, deaf), line(2, START.format(CALLER, "mcp_robot_mcp_robot_stop")),
                               line(3, DONE.format(CALLER, "mcp_robot_mcp_robot_stop"))])
        self.assertEqual(log.fatal.code, "agent_channel_lost")  # успешный вызов MCP канал не лечит
        self.assertNotIn("0123456789abcdef", log.fatal.line)
        self.assertIn("key=<REDACTED>&ts=7", log.fatal.line)
        self.assertEqual(picolog.redact("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOjF9.sig x"), "token <REDACTED> x")

    def test_pending_calls_and_reloads(self):
        log = picolog.analyze([
            line(10, START.format(CALLER, "mcp_ros_mcp_robot_nudge")),
            line(11, START.format(CALLER, "message")),
            line(12, DONE.format(CALLER, "message")),
            line(13, "INF > Manual reload triggered via /reload endpoint"),
            line(14, "INF > 🔄 Config file changed, reloading..."),
            line(15, DONE.format(CALLER, "mcp_unknown_tool")),  # завершение без начала в окне журнала
        ])
        self.assertEqual(log.pending, [("mcp_ros_mcp_robot_nudge", 10)])
        self.assertEqual(log.pending_age(100), 90)
        self.assertEqual(log.reloads, 2)


class Server:
    """HTTP-сервер в потоке на свободном порту."""

    def __init__(self, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:{}/mcp".format(self.httpd.server_address[1])
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Faults:
    def __init__(self, hung=False):
        self._hung = hung

    def hung(self):
        return self._hung

    def tools_hung(self):
        return False


def fake_mcp(hung=False):
    return type("H", (fake_component.McpHandler,), {"faults": Faults(hung)})


class SseHandler(BaseHTTPRequestHandler):
    """Ответы как у FastMCP по умолчанию: text/event-stream, сессия в заголовке, DELETE закрывает сессию."""
    deleted = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "id" not in req:
            self.send_response(202)
            self.end_headers()
            return
        result = {"initialize": {"protocolVersion": "2025-06-18"},
                  "tools/list": {"tools": [{"name": "robot_get_status"}]},
                  "tools/call": {"content": [{"type": "text", "text": json.dumps({"connected": False,
                                                                                  "error": "refused"})}]}}
        body = "event: message\r\ndata: {}\r\n\r\n".format(json.dumps({"jsonrpc": "2.0", "id": req["id"],
                                                                      "result": result[req["method"]]}))
        assert self.headers["Accept"] == "application/json, text/event-stream"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("mcp-session-id", "s1")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_DELETE(self):
        SseHandler.deleted.append(self.headers.get("Mcp-Session-Id"))
        self.send_response(200)
        self.end_headers()


class McpProbeTest(unittest.TestCase):
    def test_json_server_with_status_call(self):
        srv = Server(fake_mcp())
        try:
            res = probe_mcp(srv.url, 3.0, "robot_get_status")
        finally:
            srv.close()
        self.assertEqual(res.tools, ["robot_get_status", "robot_stop", "robot_nudge"])
        self.assertTrue(json.loads(res.status)["connected"])
        self.assertEqual(res.params["robot_nudge"], ["direction", "duration_ms", "speed"])

    def test_sse_server_and_session_close(self):
        SseHandler.deleted = []
        srv = Server(SseHandler)
        try:
            res = probe_mcp(srv.url, 3.0, "robot_get_status")
        finally:
            srv.close()
        self.assertEqual(json.loads(res.status)["connected"], False)
        self.assertEqual(SseHandler.deleted, ["s1"])

    def test_missing_tool_is_error(self):
        srv = Server(fake_mcp())
        try:
            with self.assertRaisesRegex(ProbeError, "нет инструмента status"):
                probe_mcp(srv.url, 3.0, "status")
        finally:
            srv.close()

    def test_hung_server_times_out(self):
        srv = Server(fake_mcp(hung=True))
        t0 = time.monotonic()
        try:
            with self.assertRaisesRegex(ProbeError, "нет ответа"):
                probe_mcp(srv.url, 0.5)
        finally:
            srv.close()
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_refused(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with self.assertRaises(ProbeError):
            probe_mcp("http://127.0.0.1:{}/mcp".format(port), 1.0)


class HttpGetTest(unittest.TestCase):
    def test_codes_and_refused(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                code = 200 if self.path == "/state" else 503
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b'{"state": "loading"}' if code == 503 else b"{}")
        srv = Server(H)
        base = srv.url.rsplit("/", 1)[0]
        try:
            self.assertEqual(http_get(base + "/state", 2.0), (200, "{}"))
            self.assertEqual(http_get(base + "/health", 2.0), (503, '{"state": "loading"}'))
        finally:
            srv.close()
        with self.assertRaisesRegex(ProbeError, "/health"):
            http_get(base + "/health", 1.0)


class WaitHttpTest(unittest.TestCase):
    """neurobot wait-http labworker@<имя>: ждёт 200, пока воркер отвечает 503 «загружаю модель»."""

    def run_cli(self, url: str, wait_s: str):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.path.join(tmp, "n.env")
            with open(env, "w", encoding="utf-8") as f:
                f.write("NEUROBOT_LABWORKER_vision_URL={}\nNEUROBOT_LABWORKER_WAIT_S={}\n".format(url, wait_s))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli.main(["--env", env, "wait-http", "labworker@vision"])
        return code, err.getvalue()

    def test_loading_then_ready_and_timeout(self):
        calls = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                calls.append(self.path)
                self.send_response(200 if len(calls) > 2 else 503)
                self.end_headers()
                self.wfile.write(b'{"state": "loading"}')
        srv = Server(H)
        base = srv.url.rsplit("/", 1)[0]
        try:
            code, err = self.run_cli(base + "/health", "10")
        finally:
            srv.close()
        self.assertEqual((code, len(calls)), (0, 3), err)
        self.assertIn("neurobot-wait: labworker@vision: ready: {}/health: 200".format(base), err)
        code, err = self.run_cli(base + "/health", "1")
        self.assertEqual(code, 1)
        self.assertIn("labworker@vision: labworker_timeout:", err)


class WsTest(unittest.TestCase):
    def serve(self, reply: bytes) -> int:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)

        def run():
            conn, _ = srv.accept()
            with conn:
                data = conn.recv(4096)
                assert b"Upgrade: websocket" in data and b"Sec-WebSocket-Key" in data
                conn.sendall(reply)
        threading.Thread(target=run, daemon=True).start()
        return srv.getsockname()[1]

    def test_101_ok_and_other_status_fails(self):
        ws_handshake("127.0.0.1", self.serve(b"HTTP/1.1 101 Switching Protocols\r\n\r\n"), timeout=2.0)
        with self.assertRaisesRegex(ProbeError, "не WebSocket"):
            ws_handshake("127.0.0.1", self.serve(b"HTTP/1.1 404 Not Found\r\n\r\n"), timeout=2.0)

    def test_closed_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with self.assertRaises(ProbeError):
            ws_handshake("127.0.0.1", port, timeout=1.0)


class WsPingTest(unittest.TestCase):
    """Подставной rosbridge: рукопожатие, затем pong на каждый ping (mode: pong — отвечает, silent — молчит как
    SIGSTOP, server_ping — сначала сам шлёт ping и отвечает, только получив на него pong); счёт подключений — сторож
    не переподключается на каждую пробу."""

    def setUp(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.addCleanup(self.srv.close)
        self.mode, self.accepted, self.conns = "pong", 0, []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            self.accepted += 1
            self.conns.append(conn)
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    @staticmethod
    def _exact(conn, n):
        out = b""
        while len(out) < n:
            chunk = conn.recv(n - len(out))
            if not chunk:
                raise OSError("closed")
            out += chunk
        return out

    def _client(self, conn):
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                head += conn.recv(1024)
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            while True:
                b0, b1 = self._exact(conn, 2)
                mask = self._exact(conn, 4)
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(self._exact(conn, b1 & 0x7F)))
                op = b0 & 0x0F
                if op == 0x9 and self.mode == "server_ping":
                    pending = data
                    conn.sendall(bytes([0x89, 3]) + b"srv")
                elif op == 0xA and self.mode == "server_ping" and data == b"srv":
                    conn.sendall(bytes([0x8A, len(pending)]) + pending)
                elif op == 0x9 and self.mode == "pong":
                    conn.sendall(bytes([0x8A, len(data)]) + data)
        except OSError:
            pass

    def test_pong_on_one_connection_timeout_and_reconnect(self):
        ws = WsPing("127.0.0.1", self.srv.getsockname()[1])
        self.addCleanup(ws.close)
        for _ in range(3):
            ws.ping(1.0)
        self.assertEqual(self.accepted, 1)
        self.mode = "silent"
        t0 = time.monotonic()
        with self.assertRaisesRegex(ProbeError, "нет pong"):
            ws.ping(0.5)
        self.assertLess(time.monotonic() - t0, 1.5)
        self.mode = "pong"
        ws.ping(1.0)  # после неудачи — новое соединение
        self.assertEqual(self.accepted, 2)
        self.mode = "server_ping"
        ws.ping(1.0)  # на ping сервера — pong, иначе сервер не ответил бы на наш
        self.assertEqual(self.accepted, 2)
        self.conns[-1].close()
        with self.assertRaises(ProbeError):
            ws.ping(1.0)  # сервер оборвал соединение (умер)


if __name__ == "__main__":
    unittest.main()
