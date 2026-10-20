"""Reviewed camera adapter: only explicit local cam-mcp can receive snapshot."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from neurobot.ui import cam_snapshot, catalog
from neurobot.ui.lab import LabError


def registry(url):
    return SimpleNamespace(lock=threading.RLock(), closed=False,
                           sources={"cam": {"public": {"url": url}}})


class FakeCamera:
    def __init__(self):
        self.calls = []
        self.deleted = []
        self.name = "cam-mcp"
        self.readonly = True
        self.resource = "cam://inventory"
        self.schema_extra = False
        self.mime = "image/jpeg"
        self.oversized = False
        self.sse = False
        self.redirect = False
        self.error = False
        self.maximum = 320
        # Small SOF/EOI fixture for bounded header inspection. The native test
        # below exercises the actual Go JPEG encoder and snapshot ImageContent.
        self.image = b"\xff\xd8\xff\xc0\x00\x08\x08\x00\x78\x00\xa0\x01\xff\xd9"
        fake = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *_args):
                pass

            def do_DELETE(self):  # noqa: N802
                fake.deleted.append(self.headers.get("Mcp-Session-Id"))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):  # noqa: N802
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append(request)
                method = request["method"]
                if fake.redirect:
                    self.send_response(302)
                    self.send_header("Location", "https://example.com/forbidden")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if method == "notifications/initialized":
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if method == "initialize":
                    result = {"serverInfo": {"name": fake.name}, "capabilities": {"tools": {}, "resources": {}},
                              "protocolVersion": catalog.PROTOCOL}
                elif method == "resources/list":
                    result = {"resources": [{"uri": fake.resource, "mimeType": "application/json"}]}
                elif method == "resources/read":
                    inventory = {"devices": [{"id": "cam1", "kind": "camera", "properties": {}}],
                                 "camera": {"source": "synthetic", "w": 320, "h": 240, "vendor": "retained"}}
                    result = {"contents": [{"uri": fake.resource, "mimeType": "application/json", "text": json.dumps(inventory)}]}
                elif method == "tools/list":
                    props = {"max_width": {"type": "integer", "minimum": 64, "maximum": fake.maximum},
                             "quality": {"type": "integer", "minimum": 20, "maximum": 60}}
                    if fake.schema_extra:
                        props["engage"] = {"type": "boolean"}
                    result = {"tools": [{"name": "snapshot", "annotations": {"readOnlyHint": fake.readonly},
                                         "inputSchema": {"type": "object", "properties": props, "additionalProperties": False}},
                                        {"name": "robot.Look", "annotations": {"readOnlyHint": True}}]}
                elif method == "tools/call":
                    data = base64.b64encode(fake.image).decode()
                    if fake.oversized:
                        data = base64.b64encode(b"x" * (cam_snapshot.MAX_IMAGE_BYTES + 1)).decode()
                    result = {"isError": fake.error, "content": [
                        {"type": "text", "text": json.dumps({"w": 160, "h": 120, "age_s": 0.1, "vendor": {"x": False}})},
                        {"type": "image", "mimeType": fake.mime, "data": data}]}
                else:
                    raise AssertionError("unexpected method " + method)
                message = {"jsonrpc": "2.0", "id": request["id"], "result": result}
                raw = json.dumps(message).encode()
                if fake.sse:
                    raw = b"event: message\ndata: " + raw + b"\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if fake.sse else "application/json")
                self.send_header("Mcp-Session-Id", "camera-test-session")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d/mcp" % self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class CameraSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.camera = FakeCamera()
        self.addCleanup(self.camera.close)

    def tool_calls(self):
        return [r["params"] for r in self.camera.calls if r["method"] == "tools/call"]

    def test_only_reviewed_snapshot_and_private_session_cleanup(self):
        result = cam_snapshot.snapshot(registry(self.camera.url), "cam")
        self.assertEqual(result["mimeType"], "image/jpeg")
        self.assertEqual(base64.b64decode(result["data"]), self.camera.image)
        self.assertEqual(result["metadata"]["actual_width"], 160)
        self.assertEqual(result["metadata"]["vendor"], {"x": False})
        self.assertEqual(result["source"], "synthetic")
        self.assertIn("synthetic", result["origin"])
        self.assertEqual(self.tool_calls(), [{"name": "snapshot", "arguments": {"max_width": 320, "quality": 60}}])
        self.assertEqual(self.camera.deleted, ["camera-test-session"])

    def test_sse_and_lower_server_width_cap(self):
        self.camera.sse = True
        self.camera.maximum = 160
        result = cam_snapshot.snapshot(registry(self.camera.url), "cam")
        self.assertEqual(result["metadata"]["actual_width"], 160)
        self.assertEqual(self.tool_calls()[0]["arguments"]["max_width"], 160)

    def test_unknown_server_or_schema_or_resource_never_gets_action(self):
        for attribute, value in (("name", "neurobot-mcp"), ("readonly", False),
                                 ("resource", "hw://inventory"), ("schema_extra", True)):
            original = getattr(self.camera, attribute)
            setattr(self.camera, attribute, value)
            with self.assertRaises(LabError) as error:
                cam_snapshot.snapshot(registry(self.camera.url), "cam")
            self.assertEqual(error.exception.status, 403)
            self.assertEqual(self.tool_calls(), [])
            setattr(self.camera, attribute, original)

    def test_no_arbitrary_url_source_or_redirect(self):
        with self.assertRaises(LabError) as error:
            cam_snapshot.snapshot(registry(self.camera.url), "http://example.com")
        self.assertEqual(error.exception.status, 404)
        with self.assertRaises(catalog.CatalogError):
            cam_snapshot.snapshot(registry("http://example.com/mcp"), "cam")
        self.assertEqual(self.camera.calls, [])
        self.camera.redirect = True
        with self.assertRaises(LabError):
            cam_snapshot.snapshot(registry(self.camera.url), "cam")
        self.assertEqual(self.tool_calls(), [])

    def test_oversize_nonjpeg_and_device_error_are_explicit(self):
        for attribute, value in (("oversized", True), ("mime", "image/svg+xml"), ("error", True), ("image", b"not jpeg")):
            original = getattr(self.camera, attribute)
            setattr(self.camera, attribute, value)
            with self.assertRaises(LabError):
                cam_snapshot.snapshot(registry(self.camera.url), "cam")
            setattr(self.camera, attribute, original)
        self.assertTrue(all(call["name"] == "snapshot" for call in self.tool_calls()))

    def test_rate_and_busy_bound(self):
        source = registry(self.camera.url)
        cam_snapshot.snapshot(source, "cam")
        with self.assertRaises(LabError) as error:
            cam_snapshot.snapshot(source, "cam")
        self.assertEqual(error.exception.status, 429)
        source.sources["cam"]["camera_snapshot"]["busy"] = True
        with self.assertRaises(LabError) as error:
            cam_snapshot.snapshot(source, "cam")
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(len(self.tool_calls()), 1)


@unittest.skipUnless(os.environ.get("EDGELAB_TEST_CAM_BIN"), "set EDGELAB_TEST_CAM_BIN for actual native cam-mcp")
class NativeCameraTest(unittest.TestCase):
    def test_actual_project_camera_jpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready, trace = root / "ready", root / "trace.jsonl"
            with (root / "server.log").open("wb") as log:
                proc = subprocess.Popen([os.environ["EDGELAB_TEST_CAM_BIN"], "-transport", "http", "-source", "synthetic",
                                         "-clock", "real", "-ready-file", str(ready), "-trace", str(trace)],
                                        stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < deadline and proc.poll() is None:
                        time.sleep(0.02)
                    self.assertTrue(ready.exists(), (root / "server.log").read_text())
                    source = registry(ready.read_text().strip())
                    result = cam_snapshot.snapshot(source, "cam")
                    self.assertEqual(result["source"], "synthetic")
                    self.assertGreater(len(base64.b64decode(result["data"])), 100)
                    self.assertLessEqual(result["metadata"]["actual_width"], 320)
                    self.assertEqual(result["metadata"]["decoded_bytes"], len(base64.b64decode(result["data"])))
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                    try:
                        proc.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
            records = [json.loads(line) for line in trace.read_text().splitlines()]
            names = [r.get("name") for r in records if r["type"] == "tool_call"]
            self.assertEqual(names, ["snapshot"])


if __name__ == "__main__":
    unittest.main()
