"""Честная заглушка «Robot Vision Demo Worker» :8766 для A0 (README §2.1). Кода воркера лаборатории нет
(R7a N8): заглушка отвечает на API, которым пользуется их прокси `robot_vision_mcp_proxy.py`, и записывает каждый
запрос (что прокси передал воркеру — например, duration_s = 300 из дубля robot_nudge_forward, N1). Робота она не
двигает: что делает настоящий воркер с /motion/nudge, неизвестно, и выдумывать это мы не стали.

  python3 worker8766.py --log FILE.jsonl [--port 8766]

Ответ — JSON {ok: true, stub: true, …}; неизвестный путь — 404 {ok: false}. Журнал — строка JSONL на запрос:
{t, method, path, body}. Логи — в stderr.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GET_PATHS = ("/state", "/vision/detections", "/vision/hand", "/lidar/status", "/motion/state")
POST_PATHS = ("/vision/wait_hand", "/vision/wait_hand_appeared", "/vision/wait_object", "/motion/nudge",
              "/motion/stop")


def answer(method: str, path: str, body: dict) -> tuple:
    """(код HTTP, ответ) заглушки на запрос прокси."""
    known = GET_PATHS if method == "GET" else POST_PATHS
    if path not in known:
        return 404, {"ok": False, "stub": True, "error": "unknown path %s" % path}
    out = {"ok": True, "stub": True, "path": path}
    if path == "/motion/nudge":
        out.update(moved=False, received=body, note="stub worker: motion is not simulated")
    return 200, out


class Journal:
    def __init__(self, path: str):
        self.path, self.lock = path, threading.Lock()

    def write(self, rec: dict) -> None:
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with self.lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line)


def handler(journal: Journal):
    class H(BaseHTTPRequestHandler):
        def _reply(self, method: str) -> None:
            body = {}
            size = int(self.headers.get("Content-Length") or 0)
            if size:
                try:
                    body = json.loads(self.rfile.read(size).decode("utf-8"))
                except ValueError:
                    body = {"_raw": "not json"}
            code, out = answer(method, self.path, body)
            journal.write({"t": time.time(), "method": method, "path": self.path, "body": body, "code": code})
            data = json.dumps(out).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802 (имя из http.server)
            self._reply("GET")

        def do_POST(self):  # noqa: N802
            self._reply("POST")

        def log_message(self, fmt, *args):
            sys.stderr.write("worker8766: " + fmt % args + "\n")
    return H


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="заглушка воркера 8766 лаборатории (A0)")
    p.add_argument("--log", required=True, help="журнал запросов JSONL")
    p.add_argument("--port", type=int, default=8766, help="порт на 127.0.0.1 (прокси лаборатории зашил 8766)")
    a = p.parse_args(argv)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), handler(Journal(a.log)))
    print("worker8766: заглушка на 127.0.0.1:%d" % a.port, file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
