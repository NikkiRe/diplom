"""Канал pico и mockllm (pico.py): кадры WebSocket RFC 6455, клиент против подставного шлюза (Bearer, ping, ответ
хода), финальный ответ, управление оракулом mockllm."""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from neurobot.sim.runs import pico  # noqa: E402

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def server_frame(opcode: int, payload: bytes) -> bytes:
    n = len(payload)
    head = bytes([0x80 | opcode])
    head += bytes([n]) if n < 126 else bytes([126]) + n.to_bytes(2, "big")
    return head + payload


class FrameTest(unittest.TestCase):
    def test_client_frame_is_masked_and_parses_back_for_all_lengths(self):
        for n in (0, 125, 126, 70000):
            data = bytes(i % 251 for i in range(n))
            raw = pico.frame(pico.OP_TEXT, data, mask=b"\x01\x02\x03\x04")
            self.assertTrue(raw[1] & 0x80)
            opcode, fin, payload, used = pico.parse(raw)
            self.assertEqual((opcode, fin, payload, used), (pico.OP_TEXT, True, data, len(raw)))

    def test_incomplete_frame_waits(self):
        raw = server_frame(pico.OP_TEXT, b"x" * 300)
        self.assertIsNone(pico.parse(raw[:3]))
        self.assertIsNone(pico.parse(raw[:-1]))

    def test_final_text_skips_tool_feedback(self):
        msgs = [{"type": "message.create", "payload": {"kind": "tool_calls", "content": ""}},
                {"type": "message.create", "payload": {"content": "Готово."}}]
        self.assertEqual(pico.final_text(msgs), "Готово.")
        self.assertIsNone(pico.final_text(msgs[:1]))


class FakeGateway(threading.Thread):
    """Шлюз с каналом pico: проверяет Bearer, шлёт ping, отвечает на message.send ходом из двух сообщений."""

    def __init__(self, token: str):
        super().__init__(daemon=True)
        self.token, self.sock = token, socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.got, self.pong = [], threading.Event()

    def run(self):
        conn, _ = self.sock.accept()
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(4096)
        text = head.decode()
        if "Authorization: Bearer %s" % self.token not in text:
            conn.sendall(b"HTTP/1.1 401 Unauthorized\r\n\r\n")
            conn.close()
            return
        key = [ln.split(": ", 1)[1] for ln in text.split("\r\n") if ln.startswith("Sec-WebSocket-Key")][0]
        acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Accept: %s\r\n\r\n" % acc).encode() + server_frame(pico.OP_PING, b"p"))
        buf = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            got = pico.parse(buf)
            while got:
                opcode, _, data, used = got
                buf = buf[used:]
                if opcode == pico.OP_PONG:
                    self.pong.set()
                elif opcode == pico.OP_TEXT:
                    msg = json.loads(data)
                    self.got.append(msg)
                    for out in ({"type": "message.create", "payload": {"kind": "tool_calls", "content": ""}},
                                {"type": "message.create", "payload": {"content": "Готово."}}):
                        conn.sendall(server_frame(pico.OP_TEXT, json.dumps(out).encode()))
                elif opcode == pico.OP_CLOSE:
                    conn.close()
                    return
                got = pico.parse(buf)


class ClientTest(unittest.TestCase):
    def test_turn_over_pico_channel(self):
        gw = FakeGateway("tok")
        gw.start()
        c = pico.PicoClient("127.0.0.1", gw.port, "tok", "s1")
        msgs = c.send("[runs] привет", timeout=5.0)
        c.close()
        self.assertEqual(pico.final_text(msgs), "Готово.")
        self.assertEqual(gw.got[0]["type"], "message.send")
        self.assertEqual(gw.got[0]["payload"]["content"], "[runs] привет")
        self.assertTrue(gw.pong.wait(2.0))

    def test_wrong_token_is_refused(self):
        gw = FakeGateway("tok")
        gw.start()
        with self.assertRaises(ConnectionError):
            pico.PicoClient("127.0.0.1", gw.port, "bad", "s1")


class MockLLMTest(unittest.TestCase):
    def test_expect_clear_state_journal(self):
        seen = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                seen.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self._ok(b'{"ok": true}')

            def do_GET(self):  # noqa: N802
                self._ok(b'{"journal_seq": 3}' if self.path == "/control/state" else b'{"seq": 4}\n\n{"seq": 5}\n')

            def _ok(self, body):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        m = pico.MockLLM("http://127.0.0.1:%d/" % srv.server_address[1])
        m.expect("x-1", "текст", [{"name": "robot_stop", "args": {}}])
        m.clear("x-1")
        self.assertEqual(m.state()["journal_seq"], 3)
        self.assertEqual([r["seq"] for r in m.journal(3)], [4, 5])
        srv.shutdown()
        self.assertEqual(seen[0], ("/control/expect", {"input_id": "x-1", "text": "текст", "expect": {
            "type": "action", "calls": [{"name": "robot_stop", "args": {}}]}}))
        self.assertEqual(seen[1], ("/control/clear", {"input_id": "x-1"}))


if __name__ == "__main__":
    unittest.main()
