"""Trace laboratory HTTP integration: explicit content, same-origin writes and no robot commands."""
import http.client
import json
import threading
import unittest

from neurobot.ui import server


class LabHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = server.make_server(None, 0)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def request(self, path, method="GET", body=None, origin=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        host = "127.0.0.1:%d" % self.port
        headers = {"Host": host, "Content-Type": "application/json"}
        if origin:
            headers["Origin"] = "http://" + host
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, json.loads(data) if res.getheader("Content-Type", "").startswith("application/json") else data

    def test_import_linked_trace(self):
        rows = [{"type": "run_start", "clock": "virtual", "ts_clock": 0},
                {"type": "input", "id": "i-0", "kind": "sensor", "device": "custom", "property": "zero", "value": 0, "ts_clock": 1},
                {"type": "wake", "wake_id": "w-0", "input_ids": ["i-0"], "ts_clock": 1},
                {"type": "run_end", "ts_clock": 2}]
        code, out = self.request("/api/lab/import", "POST", {"name": "HTTP test", "events": rows})
        self.assertEqual(code, 201, out)
        code, run = self.request("/api/lab/runs/" + out["run"]["id"])
        self.assertEqual(code, 200)
        self.assertEqual(run["events"][1]["value"], 0)
        self.assertEqual(run["events"][2]["links"][0]["id"], run["events"][1]["id"])
        self.assertEqual(len(run["provenance"]["sha256"]), 64)
        self.assertIsNone(run["metrics"]["total_tokens"])
        self.assertEqual(self.request("/api/lab/runs/missing")[0], 404)

    def test_writes_require_origin(self):
        self.assertEqual(self.request("/api/lab/import", "POST", {"events": []}, origin=False)[0], 403)
        self.assertEqual(self.request("/api/lab/import", "POST", {"path": "/etc/passwd"})[0], 400)
        self.assertEqual(self.request("/api/stop", "POST", {})[0], 405)
