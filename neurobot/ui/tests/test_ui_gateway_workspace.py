"""Authenticated workspace APIs on a robot gateway preserve its command boundary."""
import http.client
import json
import tempfile
import threading
import time
import unittest

from neurobot.ui import gateway, live
from neurobot.ui.tests.test_ui_boards import minimal_report
from neurobot.ui.tests.test_ui_gateway import StubNode, slow_state


class GatewayWorkspaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.hub = live.Hub()
        cls.lv = live.Live(cls.hub)
        cls.lv.on_safety(slow_state(), time.time())
        cls.node = StubNode(cls.lv)
        cls.auth = gateway.Auth(cls.temp.name)
        cls.gw = gateway.Gateway(cls.hub, cls.lv, cls.node, cls.auth, "робот", "workspace-test")
        cls.server = gateway.make_server(cls.gw, 0)
        cls.port = cls.server.server_address[1]
        cls.sid = cls.auth.new_session()
        cls.cookie = cls.auth.cookie_name + "=" + cls.sid
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stopping.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.gw.assistant.close()  # explicitly safe after both server lifecycle hooks
        cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def request(self, path, method="GET", body=None, auth=True, csrf=True, origin=True, declared_length=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        host = "127.0.0.1:%d" % self.port
        headers = {"Host": host, "Content-Type": "application/json"}
        if auth:
            headers["Cookie"] = self.cookie
        if csrf:
            headers[gateway.CSRF_HEADER] = self.auth.csrf_for(self.sid)
        if origin:
            headers["Origin"] = "http://" + host
        if declared_length is not None:
            headers["Content-Length"] = str(declared_length)
        raw = json.dumps(body).encode() if body is not None else None
        connection.request(method, path, raw, headers)
        response = connection.getresponse()
        data = response.read()
        content_type = response.getheader("Content-Type", "")
        status = response.status
        connection.close()
        return status, json.loads(data) if content_type.startswith("application/json") else data

    def test_workspace_get_and_script_are_authenticated(self):
        for path in ("/api/boards", "/api/boards/probe.py", "/api/assistant"):
            self.assertEqual(self.request(path, auth=False)[0], 401)
        code, script = self.request("/api/boards/probe.py")
        self.assertEqual(code, 200)
        self.assertIn(b"edge-board/1", script)
        code, report = self.request("/api/boards")
        self.assertEqual((code, report["format"]), (200, "edge-boards/1"))

    def test_board_import_get_and_forget(self):
        report = minimal_report()
        report["extension"] = "x" * 8000  # above robot command limit, below board import limit
        code, result = self.request("/api/boards/import", "POST", {"report": report, "label": "Gateway imported board"})
        self.assertEqual(code, 201, result)
        identity = result["board"]["id"]
        code, fetched = self.request("/api/boards/" + identity)
        self.assertEqual((code, fetched["board"]["report"]), (200, report))
        self.assertEqual(self.request("/api/boards/forget", "POST", {"id": identity})[0], 200)
        self.assertEqual(self.request("/api/boards/" + identity)[0], 404)

    def test_sessions_export_and_runtime_disabled(self):
        code, snapshot = self.request("/api/assistant")
        self.assertEqual(code, 200)
        self.assertFalse(snapshot["state"]["runtime_enabled"])
        self.assertEqual(self.request("/api/assistant/agent/start", "POST", {})[0], 403)
        self.assertEqual(self.request("/api/assistant/agent/connect", "POST", {"url": "http://127.0.0.1:1"})[0], 403)
        code, created = self.request("/api/assistant/sessions", "POST", {"title": "Gateway journal"})
        self.assertEqual(code, 201, created)
        identity = created["session"]["id"]
        self.assertEqual(self.request("/api/assistant/sessions/" + identity)[0], 200)
        code, exported = self.request("/api/assistant/export/" + identity)
        self.assertEqual((code, exported["format"]), (200, "edgelab-chat/1"))
        self.assertEqual(self.node.sent, [])

    def test_mutations_require_origin_session_and_csrf(self):
        for path, body in (("/api/boards/import", {"report": minimal_report()}),
                           ("/api/assistant/sessions", {"title": "no permission"})):
            self.assertEqual(self.request(path, "POST", body, origin=False)[0], 403)
            self.assertEqual(self.request(path, "POST", body, auth=False)[0], 401)
            self.assertEqual(self.request(path, "POST", body, csrf=False)[0], 403)
        self.assertEqual(self.request("/api/boards/local", "POST", {"path": "/etc"})[0], 400)

    def test_route_body_limits_and_stdio_diagnostic(self):
        self.assertEqual(self.request("/api/assistant/sessions", "POST", {}, declared_length=128 * 1024 + 1)[0], 413)
        self.assertEqual(self.request("/api/boards/import", "POST", {}, declared_length=1024 * 1024 + 1)[0], 413)
        code, result = self.request("/api/boards/mcp/inspect", "POST", {"url": "python3 -m example_mcp"})
        self.assertEqual((code, result["status"]), (200, "stdio"))
        self.assertEqual(self.node.sent, [])


if __name__ == "__main__":
    unittest.main()
