"""Пульт: раздача «Записи» (server.py) — только 127.0.0.1, проверка Host и Origin (DNS rebinding), только GET/HEAD,
CSP с хешем import map, бандл со сжатием, без выхода за каталог статики, сверка vendor/ с SHA256SUMS; логика фронта
без браузера — Timeline и разбор секторов лидара через node (если он есть). Только stdlib, Python 3.8+."""
from __future__ import annotations

import gzip
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from neurobot.ui import server, stream  # noqa: E402

WEB = ROOT / "neurobot" / "ui" / "web"


def runtime_urls(text):
    """Explicit network dependencies, rather than documentation or URL input examples.

    Browser connections/assets stay same-origin (CSP enforces this too). A local
    MCP URL entered into a form is configuration passed to the Python adapter;
    it is not a frontend import, fetch or asset dependency.
    """
    remote = r"((?:https?:)?//[^\s\"'`<>\)]+)"
    patterns = [
        r"\bfrom\s*[\"'`]" + remote,
        r"\bimport\s*(?:\(\s*)?[\"'`]" + remote,
        r"\b(?:fetch|Request|WebSocket|EventSource|Worker)\s*\(\s*[\"'`]" + remote,
        r"<(?:script|link|img|iframe|audio|video|source|object|embed)\b[^>]*?"
        r"\b(?:src|href|data)\s*=\s*[\"']" + remote,
        r"\burl\(\s*[\"']?" + remote,
        r"@import\s*[\"']" + remote,
        r"\.(?:src|href)\s*=\s*[\"'`]" + remote,
    ]
    found = [m.group(1) for pattern in patterns for m in re.finditer(pattern, text, re.I)]
    for m in re.finditer(r'<script\b[^>]*type=[\"\']importmap[\"\'][^>]*>(.*?)</script>', text, re.S | re.I):
        doc = json.loads(m.group(1))
        maps = [doc.get("imports", {})] + list(doc.get("scopes", {}).values())
        found.extend(value for items in maps for value in items.values()
                     if isinstance(value, str) and re.match(r"(?:https?:)?//", value))
    return found


def tiny_bundle(path):
    run = {"id": "r1", "label": "прогон", "system": "a1", "source": "запись", "t_start": 0.0, "t_end": 1.0,
           "summary": {}, "stream": [{"k": "hb", "t": 0.5}]}
    doc = {"format": stream.BUNDLE_FORMAT, "stream_format": stream.FORMAT, "geometry": {}, "alarms": {},
           "compare_rows": [], "runs": [run]}
    stream.write_bundle(path, doc)


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.bundle = os.path.join(cls.tmp.name, "b.json.gz")
        tiny_bundle(cls.bundle)
        cls.srv = server.make_server(cls.bundle, 0)
        cls.port = cls.srv.server_address[1]
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def req(self, path, method="GET", headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": "127.0.0.1:%d" % self.port}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def test_listens_on_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        for host in ("0.0.0.0", "192.168.1.5", ""):
            with self.assertRaisesRegex(ValueError, "127.0.0.1"):
                server.make_server(self.bundle, 0, host=host)

    def test_index_with_csp(self):
        r, data = self.req("/")
        self.assertEqual(r.status, 200)
        self.assertIn("text/html", r.getheader("Content-Type"))
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("'sha256-", csp)                     # import map — единственный встроенный скрипт
        self.assertNotIn("unsafe-inline", csp)
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertIn(b'type="importmap"', data)

    def test_bundle_and_meta(self):
        r, data = self.req("/api/bundle", headers={"Accept-Encoding": "gzip"})
        self.assertEqual((r.status, r.getheader("Content-Encoding")), (200, "gzip"))
        self.assertEqual(json.loads(gzip.decompress(data))["runs"][0]["id"], "r1")
        r, data = self.req("/api/bundle")
        self.assertEqual(json.loads(data)["format"], stream.BUNDLE_FORMAT)
        r, data = self.req("/api/meta")
        meta = json.loads(data)
        self.assertEqual((meta["mode"], meta["source"], meta["runs"][0]["id"]), ("replay", "запись", "r1"))

    def test_static_types(self):
        for path, ctype in (("/app.js", "text/javascript"), ("/theme.css", "text/css"),
                            ("/vendor/preact/preact.module.js", "text/javascript"),
                            ("/vendor/fonts/JetBrainsMono-Regular.woff2", "font/woff2")):
            r, _ = self.req(path)
            self.assertEqual(r.status, 200, path)
            self.assertIn(ctype, r.getheader("Content-Type"), path)

    def test_host_and_origin(self):
        r, _ = self.req("/", headers={"Host": "evil.example:18120"})
        self.assertEqual(r.status, 403)
        r, _ = self.req("/", headers={"Host": "localhost:9999"})        # туннель на другой локальный порт — можно
        self.assertEqual(r.status, 200)
        r, _ = self.req("/api/bundle", headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status, 403)
        r, _ = self.req("/api/meta", headers={"Origin": "http://127.0.0.1:18120"})
        self.assertEqual(r.status, 200)
        r, _ = self.req("/", headers={"Origin": "https://127.0.0.1"})   # другая схема — не наш источник
        self.assertEqual(r.status, 403)

    def test_no_commands_and_no_escape(self):
        for m in ("POST", "PUT", "DELETE"):
            r, _ = self.req("/api/stop", method=m, body=b"{}")
            self.assertEqual(r.status, 405, m)
        for path in ("/../server.py", "/%2e%2e/server.py", "/vendor/../../server.py", "/vendor/SHA256SUMS",
                     "/nope.js", "/vendor/preact/preact.module.js.map"):
            r, _ = self.req(path)
            self.assertEqual(r.status, 404, path)
        r, data = self.req("/", method="HEAD")
        self.assertEqual((r.status, data), (200, b""))

    def test_host_parsing(self):
        self.assertTrue(server.host_ok("127.0.0.1:18120"))
        self.assertTrue(server.host_ok("[::1]:18120"))
        self.assertTrue(server.host_ok("LOCALHOST"))
        self.assertFalse(server.host_ok("127.0.0.1.evil.example"))
        self.assertFalse(server.host_ok(None))
        self.assertTrue(server.origin_ok(None))
        self.assertFalse(server.origin_ok("null"))


class VendorTest(unittest.TestCase):
    def test_vendor_matches_sums(self):
        self.assertEqual(server.verify_vendor(), [])
        lic = (WEB / "vendor" / "LICENSES").read_text(encoding="utf-8")
        for name in ("preact 10.29.8", "htm 3.1.1", "uplot 1.6.32", "SIL Open Font License"):
            self.assertIn(name, lic)

    def test_tampered_vendor_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = os.path.join(tmp, "web")
            shutil.copytree(str(WEB), web)
            with open(os.path.join(web, "vendor", "htm", "htm.module.js"), "a") as f:
                f.write("//")
            self.assertEqual(server.verify_vendor(web), ["не та сумма: ./htm/htm.module.js"])

    def test_no_cdn_at_runtime(self):
        for p in [WEB / "index.html"] + list(WEB.rglob("*.css")) + list(WEB.rglob("*.js")):
            if "vendor" in p.parts:
                continue
            text = p.read_text(encoding="utf-8")
            self.assertEqual(runtime_urls(text), [], str(p))

    def test_runtime_asset_filter_allows_local_mcp_examples(self):
        example = ('<input placeholder="http://127.0.0.1:18130/mcp">'
                   'const example="http://localhost:18130/mcp"; fetch("api/catalog");')
        self.assertEqual(runtime_urls(example), [])
        for text in ("import {h} from 'https://cdn.example/preact.js'",
                     "import('https://cdn.example/module.js')",
                     "fetch('https://outside.example/data')",
                     "fetch('http://localhost:18130/mcp')",  # direct cross-origin fetch is still forbidden
                     '<script src="//cdn.example/a.js"></script>',
                     '<link rel="stylesheet" href="https://cdn.example/a.css">',
                     'body{background:url(https://cdn.example/a.png)}',
                     '@import "https://cdn.example/a.css";',
                     '<script type="importmap">{"imports":{"x":"https://cdn.example/a.js"}}</script>'):
            with self.subTest(text=text):
                self.assertTrue(runtime_urls(text))


NODE = shutil.which("node")


@unittest.skipUnless(NODE, "нет node: логику фронта проверяет e2e.py в браузере")
class FrontLogicTest(unittest.TestCase):
    def node(self, code):
        out = subprocess.run([NODE, "--input-type=module", "-e", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_timeline(self):
        uri = json.dumps((WEB / "lib" / "timeline.js").as_uri())
        got = self.node("""import { Timeline } from %s;
const tl = new Timeline([
  {k: 'safety', t: 1, state: 'slow'}, {k: 'safety', t: 2, state: 'blocked'}, {k: 'safety', t: 3, state: 'slow'},
  {k: 'gesture', t: 1.5, id: 'g', name: 'come'}, {k: 'gesture', t: 2.5, id: 'g', outcome: 'done'},
  {k: 'alarm', t: 2, key: 'obstacle', code: 'obstacle', prio: 2, on: true, text: 'x', n: 1},
  {k: 'alarm', t: 2.8, key: 'obstacle', code: 'obstacle', prio: 2, on: false, text: 'x', n: 1}]);
const g = tl.gestures(2.6)[0];
console.log(JSON.stringify([tl.at('safety', 2.5).state, tl.at('safety', 0.5), tl.range('safety', 1, 3).length,
  tl.gestures(2)[0].outcome || null, g.name, g.outcome, g.first, tl.alarms(2.5)[0].on, tl.alarms(3)[0].on,
  tl.alarms(3)[0].since]));""" % uri)
        self.assertEqual(got, ["blocked", None, 2, None, "come", "done", 1.5, True, False, 2])

    def test_lidar_sectors_from_reasons(self):
        uri = json.dumps((WEB / "lib" / "lidar.js").as_uri())
        got = self.node("""import { sectors } from %s;
console.log(JSON.stringify(sectors([
  'lidar_blind: перед в слепом секторе лидара — только глубина, ≤ 0.15 м/с',
  'lidar_unseen: нет данных лидара 36,6…43,9° у корпуса, вне поля глубины — как слепой сектор, ≤ 0,15 м/с',
  'lidar_nodata: нет данных лидара перед роботом, -4.2…9.1° — участок в коридоре за корпусом'])));""" % uri)
        self.assertEqual(got, [{"code": "lidar_unseen", "a": 36.6, "b": 43.9},
                               {"code": "lidar_nodata", "a": -4.2, "b": 9.1}])


if __name__ == "__main__":
    unittest.main()
