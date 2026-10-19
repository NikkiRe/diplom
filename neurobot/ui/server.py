"""Раздача пульта в режиме «Запись»: статика neurobot/ui/web и бандл. Только 127.0.0.1 (или ::1), только GET и HEAD.

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

from neurobot.ui import stream

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
                    "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
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


class App(Static):
    """Раздача «Записи»: статика и бандл."""

    def __init__(self, bundle_path: str, web: str = WEB):
        super().__init__(web)
        self.bundle = Bundle(bundle_path)

    def meta(self) -> bytes:
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
            if path == "/api/bundle":
                if "gzip" in (self.headers.get("Accept-Encoding") or ""):
                    return self.send(200, app.bundle.gz, "application/json", {"Content-Encoding": "gzip"})
                return self.send(200, app.bundle.raw, "application/json")
            got = app.static(path)
            if got is None:
                return self.deny(404, "нет такого файла")
            return self.send(200, got[0], got[1])

        do_HEAD = do_GET    # noqa: N815

        def do_POST(self):   # noqa: N802 — «Запись» ничем не управляет
            self.deny(405, "режим «Запись»: команд нет")

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST   # noqa: N815
    return Handler


class Server(ThreadingHTTPServer):
    # очередь соединений: браузер открывает ≈25 модулей страницы разом, у socketserver по умолчанию 5 — лишние SYN
    # теряются и страница минутами висит на «Загрузке пульта» [опыт e2e]
    request_queue_size = 128
    daemon_threads = True


def make_server(bundle: str, port: int, host: str = "127.0.0.1", web: str = WEB) -> ThreadingHTTPServer:
    if host not in LOOPBACK:
        raise ValueError("пульт слушает только 127.0.0.1 или ::1, не %r: доступ снаружи — через SSH-туннель" % host)
    app = App(bundle, web)
    cls = Server
    if ":" in host:
        import socket

        class V6(Server):
            address_family = socket.AF_INET6
        cls = V6
    srv = cls((host, port), handler(app))
    srv.daemon_threads = True
    return srv


def serve(bundle: str, port: int, host: str = "127.0.0.1") -> int:
    bad = verify_vendor()
    if bad:
        sys.stderr.write("neurobot-ui: vendor/ не совпадает с SHA256SUMS: %s\n" % "; ".join(bad))
        return 2
    try:
        srv = make_server(bundle, port, host)
    except (OSError, ValueError, stream.FormatError) as e:
        sys.stderr.write("neurobot-ui: %s\n" % e)
        return 2
    url = "http://%s:%d/" % ("127.0.0.1" if host == "127.0.0.1" else "[::1]", srv.server_address[1])
    print("пульт (запись): %s — бандл %s; Ctrl+C — выход" % (url, bundle), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
