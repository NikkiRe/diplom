"""Статика пульта и необязательная «Запись»; POST — только настройка читаемых MCP-источников.
Слушает только 127.0.0.1 или ::1.

  python -m neurobot.ui serve --bundle FILE [--port 18120]

Проверки — те же, что у живого шлюза (gateway.py, PLAN §3.3): Host — локальный адрес с любым портом (туннель SSH может
прийти на любой локальный порт), Origin, если есть, — тоже локальный; иначе 403 (DNS rebinding). Файлы vendor/
сверяются с SHA256SUMS при старте: не сошлось — пульт не стартует. Ничего не публикует и ничем не управляет.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

from neurobot.ui import assistant, boards, cam_snapshot, catalog, lab, read_proxy, source_state, stream

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
LOOPBACK = ("127.0.0.1", "::1")
LOCAL_NAMES = ("127.0.0.1", "localhost", "[::1]")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".json": "application/json", ".woff2": "font/woff2",
         ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8"}


def verify_vendor(web: str = WEB) -> List[str]:
    """Расхождения vendor/ с SHA256SUMS: [] — всё сошлось."""
    vendor = os.path.join(web, "vendor")
    sums = os.path.join(vendor, "SHA256SUMS")
    if not os.path.exists(sums):
        return ["нет vendor/SHA256SUMS"]
    bad = []
    with open(sums, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            digest, name = line.split(None, 1)
            name = name.strip().lstrip("*")
            path = os.path.normpath(os.path.join(vendor, name))
            if not os.path.isfile(path):
                bad.append("нет файла %s" % name)
                continue
            with open(path, "rb") as g:
                if hashlib.sha256(g.read()).hexdigest() != digest:
                    bad.append("не та сумма: %s" % name)
    return bad


def importmap_hash(html: str) -> str:
    """CSP-хеш встроенного import map (единственный встроенный скрипт страницы)."""
    m = re.search(r'<script type="importmap">(.*?)</script>', html, re.S)
    if not m:
        return ""
    return "'sha256-%s'" % base64.b64encode(hashlib.sha256(m.group(1).encode("utf-8")).digest()).decode()


def host_ok(value: Optional[str]) -> bool:
    """Host: локальное имя с любым портом."""
    if not value:
        return False
    v = value.strip().lower()
    if v.startswith("["):
        name = v[:v.find("]") + 1] if "]" in v else v
    else:
        name = v.split(":", 1)[0]
    return name in LOCAL_NAMES


def origin_ok(value: Optional[str]) -> bool:
    """Origin: нет (обычный GET) или http://локальное-имя[:порт]."""
    if value is None:
        return True
    parts = urlsplit(value.strip())
    return parts.scheme == "http" and host_ok(parts.netloc)


class Static:
    """Статика пульта (web/) и CSP страницы — общие у «Записи» и живого шлюза."""

    def __init__(self, web: str = WEB):
        self.web = os.path.realpath(web)
        with open(os.path.join(self.web, "index.html"), encoding="utf-8") as f:
            html = f.read()
        self.csp = ("default-src 'none'; script-src 'self' %s; style-src 'self'; img-src 'self' data:; "
                    "font-src 'self'; connect-src 'self'; media-src 'self' data:; base-uri 'none'; form-action 'none'; "
                    "frame-ancestors 'none'" % importmap_hash(html))

    def static(self, path: str) -> Optional[Tuple[bytes, str]]:
        rel = unquote(path).lstrip("/") or "index.html"
        full = os.path.realpath(os.path.join(self.web, rel))
        if not (full == self.web or full.startswith(self.web + os.sep)) or not os.path.isfile(full):
            return None
        ctype = TYPES.get(os.path.splitext(full)[1].lower())
        if ctype is None:
            return None
        with open(full, "rb") as f:
            return f.read(), ctype


class Bundle:
    """Бандл «Записи»: байты как есть и gzip, список прогонов."""

    def __init__(self, path: str):
        doc = stream.read_bundle(path)
        stream.check_bundle(doc)
        self.runs = [{"id": r["id"], "label": r["label"]} for r in doc["runs"]]
        self.raw = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.gz = gzip.compress(self.raw, 6)
        self.name = os.path.basename(path)


def default_state_dir() -> str:
    """User state lives outside source control; constructors stay ephemeral in tests."""
    return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "edgelab")


def workspace_get(app, path):
    """Shared read API, called after the caller's origin/auth checks."""
    doc = None
    extra = {}
    try:
        if path == "/api/boards/probe.py":
            probe = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(WEB))), "scripts", "edge_probe.py")
            with open(probe, "rb") as source:
                return 200, source.read(), "text/x-python; charset=utf-8", {"Content-Disposition": 'attachment; filename="edge_probe.py"'}
        if path == "/api/boards":
            doc = app.boards.snapshot()
        elif path.startswith("/api/boards/"):
            board = app.boards.get(unquote(path[len("/api/boards/"):]))
            if board is None:
                return 404, b'{"error":"Board not found"}', "application/json", {}
            doc = {"board": board}
        elif path == "/api/assistant":
            doc = app.assistant.snapshot()
        elif path.startswith("/api/assistant/sessions/"):
            doc = app.assistant.session(unquote(path[len("/api/assistant/sessions/"):]))
        elif path.startswith("/api/assistant/export/"):
            doc = app.assistant.export(unquote(path[len("/api/assistant/export/"):]))
            extra = {"Content-Disposition": 'attachment; filename="edgelab-chat.json"'}
        else:
            return None
        return 200, json.dumps(doc, ensure_ascii=False).encode("utf-8"), "application/json", extra
    except assistant.AssistantError as exc:
        return exc.status, json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"), "application/json", {}


class App(Static):
    """Раздача «Записи»: статика и бандл."""

    def __init__(self, bundle_path: Optional[str], web: str = WEB, state_dir=None, assistant_binary=None):
        super().__init__(web)
        self.bundle = Bundle(bundle_path) if bundle_path else None
        self.catalog = catalog.Registry()
        self.lab = lab.LabStore()
        self.boards = boards.BoardRegistry(state_dir)
        self.assistant = assistant.AssistantStore(os.path.join(state_dir, "assistant") if state_dir else None,
                                                 binary=assistant_binary)
        self.read_proxy = read_proxy.ReadProxy(self.catalog, self.boards)
        self._proxy_lock = threading.Lock()
        self._proxy_url = None
        self.assistant.trace_callback = lambda path, label: self.lab.watch_trace(path, label)
        self.source_state = source_state.SourceState(state_dir)
        for source in self.source_state.load():
            self.catalog.connect(source["url"], source["label"])

    def catalog_snapshot(self):
        return {**self.catalog.snapshot(), "persistence": {"durable": self.source_state.path is not None,
                                                         "error": self.source_state.error}}

    def ensure_proxy(self):
        with self._proxy_lock:
            if self._proxy_url is None:
                self._proxy_url = self.read_proxy.start()
            self.assistant.configure_mcp(self._proxy_url)

    def meta(self) -> bytes:
        if self.bundle is None:
            return json.dumps({"mode": "catalog", "modes": ["catalog"], "source": "MCP", "format": "edge-atlas/1"},
                              ensure_ascii=False).encode("utf-8")
        return json.dumps({"mode": "replay", "modes": ["replay"], "source": "запись", "format": stream.FORMAT,
                           "bundle": self.bundle.name, "runs": self.bundle.runs}, ensure_ascii=False).encode("utf-8")


def handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "neurobot-ui"
        sys_version = ""

        def log_message(self, fmt, *args):   # noqa: A003 — тихо: только ответы 4xx и 5xx
            if len(args) >= 2 and str(args[1])[:1] in ("4", "5"):
                sys.stderr.write("neurobot-ui: %s %s\n" % (args[1], args[0]))

        def send(self, code: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", app.csp)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def deny(self, code: int, text: str) -> None:
            self.send(code, text.encode("utf-8"), "text/plain; charset=utf-8")

        def do_GET(self):   # noqa: N802
            if not host_ok(self.headers.get("Host")) or not origin_ok(self.headers.get("Origin")):
                return self.deny(403, "запрещено: пульт открывается только с локального адреса")
            path = urlsplit(self.path).path
            if path == "/api/meta":
                return self.send(200, app.meta(), "application/json")
            if path == "/api/catalog":
                raw = json.dumps(app.catalog_snapshot(), ensure_ascii=False).encode("utf-8")
                return self.send(200, raw, "application/json")
            workspace = workspace_get(app, path)
            if workspace is not None:
                return self.send(*workspace)
            if path.startswith("/api/lab/export/"):
                raw = app.lab.export(unquote(path[len("/api/lab/export/"):]))
                if raw is None:
                    return self.deny(404, "Прогон не найден")
                return self.send(200, raw, "application/x-ndjson; charset=utf-8",
                                 {"Content-Disposition": 'attachment; filename="edgelab-trace.jsonl"'})
            if path == "/api/lab" or path.startswith("/api/lab/runs/"):
                doc = app.lab.snapshot() if path == "/api/lab" else app.lab.run(unquote(path[len("/api/lab/runs/"):]))
                return self.send(200 if doc is not None else 404,
                                 json.dumps(doc if doc is not None else {"error": "Прогон не найден"}, ensure_ascii=False).encode("utf-8"), "application/json")
            if path == "/api/bundle" and app.bundle is not None:
                if "gzip" in (self.headers.get("Accept-Encoding") or ""):
                    return self.send(200, app.bundle.gz, "application/json", {"Content-Encoding": "gzip"})
                return self.send(200, app.bundle.raw, "application/json")
            got = app.static(path)
            if got is None:
                return self.deny(404, "нет такого файла")
            return self.send(200, got[0], got[1])

        do_HEAD = do_GET    # noqa: N815

        def do_POST(self):   # noqa: N802 — configuration only; no equipment commands
            path = urlsplit(self.path).path
            lab_paths = ("/api/lab/import", "/api/lab/experiments", "/api/lab/cancel")
            workspace_path = path.startswith("/api/boards/") or path.startswith("/api/assistant/")
            if not workspace_path and path not in ("/api/catalog/sources", "/api/catalog/disconnect", "/api/catalog/frame") + lab_paths:
                return self.deny(405, "пульт не отправляет команды оборудования")
            host = (self.headers.get("Host") or "").lower()
            if not host_ok(host) or (self.headers.get("Origin") or "").lower() != "http://" + host:
                return self.deny(403, "источники подключаются только со страницы пульта")
            if (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() != "application/json":
                return self.deny(415, "ожидается application/json")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            body_limit = 8 * 1024 * 1024 if path in lab_paths else (1024 * 1024 if path.startswith("/api/boards/") else 128 * 1024 if workspace_path else catalog.MAX_BODY)
            if not 0 <= length <= body_limit:
                self.close_connection = True
                return self.deny(413, "слишком большой запрос")
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeError):
                body = None
            if not isinstance(body, dict):
                return self.send(400, b'{"ok":false,"error":"JSON object required"}', "application/json")
            if path.startswith("/api/boards/"):
                code, doc = boards.mutate(app.boards, path, body)
            elif path.startswith("/api/assistant/"):
                try:
                    if path == "/api/assistant/agent/start":
                        app.ensure_proxy()
                    code, doc = assistant.mutate(app.assistant, path, body)
                except OSError:
                    code, doc = 503, {"error": "Не удалось открыть локальный MCP-канал наблюдений"}
            elif path == "/api/catalog/frame":
                try:
                    code, doc = 200, cam_snapshot.snapshot(app.catalog, body.get("source_id"))
                except lab.LabError as exc:
                    code, doc = exc.status, {"error": str(exc)}
            elif path in lab_paths:
                code, doc = lab.mutate(app.lab, path, body)
            else:
                # Serialize configuration and its persistence as one operation.
                # An older concurrent snapshot must not overwrite newer sources.
                with app.source_state.lock:
                    code, doc = catalog.mutate(app.catalog, path, body)
                    if code < 300:
                        error = app.source_state.save(app.catalog.snapshot())
                        doc["persistence"] = {"durable": app.source_state.path is not None, "error": error}
            return self.send(code, json.dumps(doc, ensure_ascii=False).encode("utf-8"), "application/json")

        def refuse(self):
            self.deny(405, "метод не поддерживается")

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = refuse   # noqa: N815
    return Handler


class Server(ThreadingHTTPServer):
    # очередь соединений: браузер открывает ≈25 модулей страницы разом, у socketserver по умолчанию 5 — лишние SYN
    # теряются и страница минутами висит на «Загрузке пульта» [опыт e2e]
    request_queue_size = 128
    daemon_threads = True

    def shutdown(self):
        if hasattr(self, "assistant"):
            self.assistant.close()
        if hasattr(self, "read_proxy"):
            self.read_proxy.close()
        if hasattr(self, "catalog"):
            self.catalog.close()
        if hasattr(self, "lab"):
            self.lab.close()
        super().shutdown()

    def server_close(self):
        if hasattr(self, "assistant"):
            self.assistant.close()
        if hasattr(self, "read_proxy"):
            self.read_proxy.close()
        if hasattr(self, "catalog"):
            self.catalog.close()
        if hasattr(self, "lab"):
            self.lab.close()
        super().server_close()


def make_server(bundle: Optional[str], port: int, host: str = "127.0.0.1", web: str = WEB,
                state_dir=None, assistant_binary=None) -> ThreadingHTTPServer:
    if host not in LOOPBACK:
        raise ValueError("пульт слушает только 127.0.0.1 или ::1, не %r: доступ снаружи — через SSH-туннель" % host)
    app = App(bundle, web, state_dir=state_dir, assistant_binary=assistant_binary)
    cls = Server
    if ":" in host:
        import socket

        class V6(Server):
            address_family = socket.AF_INET6
        cls = V6
    try:
        srv = cls((host, port), handler(app))
    except BaseException:
        app.assistant.close()
        app.read_proxy.close()
        app.catalog.close()
        app.lab.close()
        raise
    srv.daemon_threads = True
    srv.catalog = app.catalog
    srv.lab = app.lab
    srv.boards = app.boards
    srv.assistant = app.assistant
    srv.read_proxy = app.read_proxy
    srv.source_state = app.source_state
    return srv


def serve(bundle: Optional[str], port: int, host: str = "127.0.0.1", sources: Optional[List[str]] = None,
          traces: Optional[List[str]] = None, state_dir=None, assistant_binary=None) -> int:
    bad = verify_vendor()
    if bad:
        sys.stderr.write("neurobot-ui: vendor/ не совпадает с SHA256SUMS: %s\n" % "; ".join(bad))
        return 2
    srv = None
    try:
        initial = [catalog.endpoint(url) for url in (sources or [])]
        if len(set(initial)) > catalog.MAX_SOURCES:
            raise ValueError("не больше %d MCP-источников" % catalog.MAX_SOURCES)
        srv = make_server(bundle, port, host, state_dir=state_dir, assistant_binary=assistant_binary)
        for url in initial:
            srv.catalog.connect(url)
        if initial:
            srv.source_state.save(srv.catalog.snapshot())
        for trace in (traces or []):
            srv.lab.watch_trace(trace)
    except (OSError, ValueError, stream.FormatError, lab.LabError, assistant.AssistantError) as e:
        if srv is not None:
            srv.server_close()
        sys.stderr.write("neurobot-ui: %s\n" % e)
        return 2
    url = "http://%s:%d/" % ("127.0.0.1" if host == "127.0.0.1" else "[::1]", srv.server_address[1])
    if bundle is None:
        print("EdgeLab: %s — лаборатория агента и MCP-оборудование; Ctrl+C — выход" % url, flush=True)
    else:
        print("пульт (запись): %s — бандл %s; Ctrl+C — выход" % (url, bundle), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
