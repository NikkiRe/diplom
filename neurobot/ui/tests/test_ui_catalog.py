"""Read-only catalog against a real local fake MCP HTTP endpoint; no ROS or external network."""
from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from neurobot.ui import catalog, gateway, live, server, stream


class FakeMCP:
    def __init__(self):
        self.calls = []
        self.sessions = 0
        self.session = ""
        self.sse = False
        self.status = 200
        self.malformed = False
        self.wrong_id = False
        self.oversized = False
        self.resources = True
        self.value = 23.5
        self.expire = False
        self.delay = 0.0
        self.ready = threading.Event()
        self.inventory = {"devices": [
            {"id": "climate", "kind": "bme280", "location": "laboratory", "description": "Real descriptor",
             "properties": {"temperature": {"type": "number", "unit": "Cel", "writable": False},
                            "mystery": {"type": "array", "vendor": {"calibration": "untouched"}}},
             "actions": {"reboot": {"description": "must never be invoked", "input": {"type": "object"}}}},
            {"id": "relay", "props": {"state": "bool,rw"}, "notes": ["capability is not UI permission"]}]}
        self.state = {"device": "climate", "now": 100.0, "properties": {
            "temperature": {"value": self.value, "ts": 99.0, "age_s": 1.0},
            "mystery": {"value": [1, 2, 3], "ts": 99.0, "quality": "vendor-quality"},
            "brand_new_channel": {"value": {"x": 4}, "unit": "widgets", "ts": 99.0}}}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                method = body["method"]
                time.sleep(fake.delay)
                fake.calls.append(body)
                if fake.status != 200 or (fake.expire and method != "initialize"):
                    self.send_response(fake.status if fake.status != 200 else 404)
                    self.send_header("Location", "http://192.168.1.1/forbidden")
                    self.end_headers()
                    return
                if method == "initialize":
                    fake.sessions += 1
                    fake.session = "fake-session-%d" % fake.sessions
                    result = {"protocolVersion": catalog.PROTOCOL, "serverInfo": {"name": "Lab sensors"},
                              "capabilities": {"tools": {}, "resources": {}}}
                elif self.headers.get("Mcp-Session-Id") != fake.session:
                    self.send_response(404)
                    self.end_headers()
                    return
                elif method == "notifications/initialized":
                    self.send_response(202)
                    self.end_headers()
                    return
                elif method == "tools/list":
                    result = {"tools": [
                        {"name": "list_devices", "annotations": {"readOnlyHint": True},
                         "inputSchema": {"properties": {"all": {"type": "boolean"}}}},
                        {"name": "reboot", "annotations": {"readOnlyHint": False}},
                        {"name": "read_anything", "annotations": {"readOnlyHint": True}}]}
                elif method == "resources/list":
                    result = {"resources": [
                        {"uri": "hw://inventory", "mimeType": "application/json"},
                        {"uri": "hw://state/climate", "mimeType": "application/json"}]} if fake.resources else {}
                elif method == "resources/read":
                    uri = body["params"]["uri"]
                    doc = fake.inventory if uri == "hw://inventory" else fake.state
                    result = {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(doc)}]}
                elif method == "tools/call" and body["params"]["name"] == "list_devices":
                    result = {"content": [{"type": "text", "text": json.dumps(fake.inventory)}]}
                else:
                    result = {}
                reply = {"jsonrpc": "2.0", "id": -123 if fake.wrong_id else body.get("id"), "result": result}
                raw = b"{broken" if fake.malformed else json.dumps(reply).encode()
                if fake.oversized:
                    raw = b" " * (catalog.MAX_RESPONSE + 10)
                if fake.sse:
                    raw = (b': heartbeat\n\ndata: {"jsonrpc":"2.0","method":"notifications/message"}\n\n'
                           + b"data: " + raw + b"\n\n")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if fake.sse else "application/json")
                self.send_header("Mcp-Session-Id", fake.session)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                fake.ready.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d/mcp" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def wait_for(check, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        got = check()
        if got:
            return got
        time.sleep(0.01)
    raise AssertionError("catalog background worker did not reach expected state")


class EndpointTest(unittest.TestCase):
    def test_only_literal_loopback_http(self):
        for url in ("https://127.0.0.1/mcp", "http://192.168.1.1/mcp", "http://169.254.169.254/",
                    "http://0.0.0.0/mcp", "http://127.0.0.2/mcp", "http://127.1/mcp", "http://2130706433/",
                    "http://localhost.evil/mcp", "http://localhost./mcp", "http://user@localhost/mcp",
                    "http://[::ffff:127.0.0.1]/mcp", "file:///etc/passwd", "http://localhost:0/mcp",
                    "http://localhost:65536/mcp", "http://localhost/mcp#other", "http://localhost/\r\nX:yes"):
            with self.subTest(url=url), self.assertRaises(catalog.CatalogError):
                catalog.endpoint(url)
        self.assertEqual(catalog.endpoint("http://localhost:8000"), "http://localhost:8000/mcp")
        self.assertEqual(catalog.endpoint("http://[::1]:8000/mcp"), "http://[::1]:8000/mcp")

    def test_registry_limit_and_shutdown(self):
        class EmptyClient:
            def __init__(self, url):
                pass

            def collect(self, sid):
                return [], {}, []

        registry = catalog.Registry(client_factory=EmptyClient)
        self.addCleanup(registry.close)
        for port in range(41000, 41000 + catalog.MAX_SOURCES):
            registry.connect("http://127.0.0.1:%d/mcp" % port)
        with self.assertRaises(catalog.CatalogError):
            registry.connect("http://127.0.0.1:42000/mcp")
        self.assertEqual(len(registry.snapshot()["sources"]), catalog.MAX_SOURCES)
        registry.close()
        self.assertTrue(all(r["stop"].is_set() for r in registry.sources.values()))

    def test_network_cannot_be_reached_for_invalid_url(self):
        with mock.patch("http.client.HTTPConnection") as http_mock:
            for url in ("http://example.com/mcp", "http://127.0.0.1@evil.example/mcp"):
                with self.assertRaises(catalog.CatalogError):
                    catalog.Client(url)
            http_mock.assert_not_called()


class ConnectorTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMCP()

    def tearDown(self):
        self.fake.close()

    def test_real_hw_shape_complete_properties_and_actions_are_metadata(self):
        devices, metadata, errors = catalog.Client(self.fake.url).collect("test")
        self.assertEqual(errors, [])
        self.assertEqual(metadata["server_info"]["name"], "Lab sensors")
        self.assertEqual(len(devices), 2)
        climate = devices[0]
        props = {p["name"]: p for p in climate["properties"]}
        self.assertEqual(props["temperature"]["value"], 23.5)
        self.assertEqual(props["temperature"]["unit"], "Cel")
        self.assertEqual(props["temperature"]["ts"], 99.0)
        self.assertFalse(props["temperature"]["writable"])
        self.assertEqual(props["mystery"]["value"], [1, 2, 3])
        self.assertEqual(props["mystery"]["raw"]["vendor"]["calibration"], "untouched")
        self.assertEqual(props["brand_new_channel"]["value"], {"x": 4})
        self.assertEqual(climate["actions"][0]["name"], "reboot")
        relay = devices[1]["properties"][0]
        self.assertEqual((relay["name"], relay["type"], relay["writable"], relay["value"]),
                         ("state", "boolean", True, None))
        self.assertFalse(any(c["method"] == "tools/call" for c in self.fake.calls))

    def test_fallback_only_list_devices_including_passive_devices(self):
        self.fake.resources = False
        devices, _, errors = catalog.Client(self.fake.url).collect("test")
        self.assertEqual(errors, [])
        self.assertEqual(len(devices), 2)
        calls = [c["params"] for c in self.fake.calls if c["method"] == "tools/call"]
        self.assertEqual(calls, [{"name": "list_devices", "arguments": {"all": True}}])
        with self.assertRaises(catalog.CatalogError):
            catalog.Client(self.fake.url).rpc("tools/call", {"name": "read_anything"})

    def test_sse_rpc_and_localhost_without_dns_or_proxies(self):
        self.fake.sse = True
        url = self.fake.url.replace("127.0.0.1", "localhost")
        real = http.client.HTTPConnection
        with mock.patch("http.client.HTTPConnection", wraps=real) as http_mock:
            devices, _, errors = catalog.Client(url).collect("test")
        self.assertEqual(errors, [])
        self.assertTrue(devices)
        self.assertTrue(all(c.args[0] == "127.0.0.1" for c in http_mock.call_args_list))

    def test_explicit_http_json_and_response_size_failures(self):
        for key, expected in (("status", "HTTP 503"), ("malformed", "JSON"),
                              ("wrong_id", "Некорректный"), ("oversized", "объём")):
            with self.subTest(key=key):
                setattr(self.fake, key, 503 if key == "status" else True)
                with self.assertRaisesRegex(catalog.CatalogError, expected):
                    catalog.Client(self.fake.url).collect("test")
                setattr(self.fake, key, 200 if key == "status" else False)
        self.fake.status = 302
        with mock.patch("http.client.HTTPConnection", wraps=http.client.HTTPConnection) as http_mock:
            with self.assertRaisesRegex(catalog.CatalogError, "переходы запрещены"):
                catalog.Client(self.fake.url).collect("test")
            self.assertEqual(http_mock.call_count, 1)

    def test_request_timeout_is_explicit_and_bounded(self):
        self.fake.delay = 0.2
        started = time.monotonic()
        with self.assertRaises(catalog.CatalogError):
            catalog.Client(self.fake.url, timeout=0.03).collect("test")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_inventory_wide_metadata_is_preserved_once_at_source(self):
        calibration = "x" * 100000
        self.fake.inventory = {"devices": [{"id": "device-%d" % i, "properties": {}} for i in range(20)],
                               "vendor_calibration": calibration}
        devices, metadata, _ = catalog.Client(self.fake.url).collect("test")
        documents = metadata["metadata"]["documents"]
        self.assertEqual(documents, [{"uri": "hw://inventory", "metadata": {"vendor_calibration": calibration}}])
        encoded = json.dumps({"devices": devices, "sources": [metadata]})
        self.assertEqual(encoded.count(calibration), 1)
        self.assertLess(len(encoded), 200000)
        self.assertTrue(all("resource_metadata" not in d["raw"] for d in devices))

    def test_generic_signal_arrays_and_null_values_are_preserved(self):
        devices = catalog.normalize("s", [("sensors://inventory", {"devices": [
            {"id": "spectrometer", "signals": [{"name": "wavelength", "value": 488, "unit": "nm", "vendor": 9},
                                               {"name": "unknown", "value": None, "type": "custom-kind"}]}]})])
        self.assertEqual([p["name"] for p in devices[0]["properties"]], ["wavelength", "unknown"])
        self.assertEqual(devices[0]["properties"][1]["status"], "unknown")
        self.assertEqual(devices[0]["properties"][1]["type"], "custom-kind")
        self.assertEqual(devices[0]["properties"][0]["raw"]["vendor"], 9)

    def test_schema_free_json_resources_expose_literal_fields_and_primitives(self):
        documents = [
            ("sensor://current", {"vibrationHz": 50, "mystery": {"enabled": False}, "key.with/slash": 0}),
            ("sensor://scalar", 21),
            ("sensor://boolean", False),
            ("sensor://vector", [1, 2, 3]),
            ("sensor://descriptor", {"id": "metadata-only", "kind": "camera", "description": "Descriptor only",
                                     "actions": {"snapshot": {"description": "Do not invoke"}}, "notes": ["vendor"]}),
            ("sensor://explicit-empty", {"id": "passive-usb", "properties": {}, "vendor_id": 123}),
        ]
        documents.append(("sensor://inventory", {"devices": [{"id": "metadata-wrapper", "properties": {}}],
                                                   "camera": {"width": 640, "unknown": False}}))
        devices = {d["local_id"]: d for d in catalog.normalize("s", documents)}
        signals = {p["name"]: p for p in devices["sensor://current"]["properties"]}
        self.assertEqual(set(signals), {"vibrationHz", "mystery", "key.with/slash"})
        self.assertEqual(signals["mystery"]["value"], {"enabled": False})
        self.assertEqual(signals["vibrationHz"]["unit"], "")
        self.assertEqual(signals["key.with/slash"]["value"], 0)
        self.assertEqual(devices["sensor://scalar"]["properties"][0]["value"], 21)
        self.assertEqual(devices["sensor://boolean"]["properties"][0]["value"], False)
        self.assertEqual(devices["sensor://vector"]["properties"][0]["value"], [1, 2, 3])
        self.assertEqual(devices["metadata-only"]["properties"], [])
        self.assertEqual(devices["metadata-only"]["raw"]["descriptor"]["notes"], ["vendor"])
        self.assertEqual(devices["passive-usb"]["properties"], [])
        self.assertEqual(devices["passive-usb"]["raw"]["descriptor"]["vendor_id"], 123)
        self.assertEqual(devices["metadata-wrapper"]["raw"]["resources"], ["sensor://inventory"])
        self.assertNotIn("resource_metadata", devices["metadata-wrapper"]["raw"])
        isolated = catalog.normalize("s", [("sensor://north/current", {"v": 1}),
                                             ("sensor://south/current", {"v": 2})])
        self.assertEqual(len({d["id"] for d in isolated}), 2)
        reply = {"contents": [{"text": "21"}, {"text": "false"}, {"text": "null"}]}
        self.assertEqual(catalog.payloads(reply, "contents"), [21, False, None])

    def test_background_cache_error_staleness_and_automatic_reconnect(self):
        registry = catalog.Registry()
        self.addCleanup(registry.close)
        sid = registry.connect(self.fake.url)
        wait_for(lambda: registry.snapshot()["sources"][0]["status"] == "ready")
        before = len(self.fake.calls)
        for _ in range(20):
            registry.snapshot()
        self.assertEqual(len(self.fake.calls), before, "GET polling must not call equipment")
        self.fake.expire = True
        wait_for(lambda: registry.snapshot()["sources"][0]["status"] == "error")
        self.assertTrue(all(p["status"] == "stale" for d in registry.snapshot()["devices"] for p in d["properties"]))
        self.fake.expire = False
        wait_for(lambda: registry.snapshot()["sources"][0]["status"] == "ready")
        self.assertGreaterEqual(self.fake.sessions, 2)
        self.assertEqual(registry.connect(self.fake.url), sid)
        self.assertEqual(len(registry.snapshot()["sources"]), 1)
        registry.disconnect(sid)
        self.assertEqual(registry.snapshot()["sources"], [])
        self.assertEqual(registry.snapshot()["devices"], [])


class NormalizeLimitsTest(unittest.TestCase):
    @staticmethod
    def normalize(devices):
        return catalog.normalize("s", [("sensor://inventory", {"devices": devices})])

    def test_device_limit_boundary_and_error(self):
        rows = [{"id": str(i), "properties": {}} for i in range(catalog.MAX_ITEMS + 1)]
        self.assertEqual(len(self.normalize(rows[:-1])), catalog.MAX_ITEMS)
        with self.assertRaisesRegex(catalog.CatalogError, "устройств"):
            self.normalize(rows)

    def test_property_limit_boundary_and_error(self):
        half = catalog.MAX_PROPERTIES // 2
        rows = [{"id": str(i), "properties": {str(k): {"value": k} for k in range(half)}} for i in range(2)]
        self.assertEqual(sum(len(d["properties"]) for d in self.normalize(rows)), catalog.MAX_PROPERTIES)
        rows[1]["properties"]["overflow"] = {"value": None}
        with self.assertRaisesRegex(catalog.CatalogError, "свойств"):
            self.normalize(rows)

    def test_value_leaf_limits_include_null_boolean_and_empty_containers(self):
        values = [None, False, {}] * (catalog.MAX_DEVICE_LEAVES // 3)
        values += [0] * (catalog.MAX_DEVICE_LEAVES - len(values))
        rows = [{"id": "vector", "properties": {"samples": {"value": values}}}]
        self.assertEqual(len(self.normalize(rows)[0]["properties"][0]["value"]), catalog.MAX_DEVICE_LEAVES)
        rows[0]["properties"]["samples"]["value"] = values + [None]
        with self.assertRaisesRegex(catalog.CatalogError, "элементов"):
            self.normalize(rows)
        rows = [{"id": str(i), "properties": {"samples": {"value": [0] * catalog.MAX_DEVICE_LEAVES}}}
                for i in range(catalog.MAX_SOURCE_LEAVES // catalog.MAX_DEVICE_LEAVES)]
        self.assertEqual(len(self.normalize(rows)), 4)
        rows.append({"id": "overflow", "properties": {"one": {"value": True}}})
        with self.assertRaisesRegex(catalog.CatalogError, "элементов"):
            self.normalize(rows)

    def test_global_snapshot_budget_retains_old_values_as_stale(self):
        large = threading.Event()

        class BudgetClient:
            def __init__(self, url):
                pass

            def collect(self, sid):
                device = {"id": sid + "/d", "properties": [{"name": "temperature", "value": 21, "status": "ok"}]}
                metadata = {"metadata": {"documents": [{"metadata": "x" * 4000}]}} if large.is_set() else {}
                return [device], metadata, []

        with mock.patch.object(catalog, "MAX_SNAPSHOT_BYTES", 2000):
            registry = catalog.Registry(client_factory=BudgetClient)
            try:
                registry.connect("http://127.0.0.1:41000/mcp")
                wait_for(lambda: registry.snapshot()["devices"])
                large.set()
                wait_for(lambda: registry.snapshot()["sources"][0]["status"] == "error")
                snapshot = registry.snapshot()
                self.assertIn("объёма", snapshot["sources"][0]["error"])
                self.assertEqual(snapshot["devices"][0]["properties"][0]["value"], 21)
                self.assertEqual(snapshot["devices"][0]["properties"][0]["status"], "stale")
                self.assertLess(len(json.dumps(snapshot, ensure_ascii=False).encode()), 2000)
            finally:
                registry.close()


def tiny_bundle(path):
    stream.write_bundle(path, {"format": stream.BUNDLE_FORMAT, "stream_format": stream.FORMAT,
                              "geometry": {}, "alarms": {}, "compare_rows": [], "runs": [
                                  {"id": "r", "label": "run", "system": "a1", "source": "запись",
                                   "t_start": 0.0, "t_end": 1.0, "summary": {}, "stream": []}]})


class CatalogHTTPTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fake = FakeMCP()
        self.addCleanup(self.fake.close)
        path = os.path.join(self.temp.name, "bundle.json.gz")
        tiny_bundle(path)
        self.replay = server.make_server(path, 0)
        threading.Thread(target=self.replay.serve_forever, daemon=True).start()
        self.addCleanup(self.replay.server_close)
        self.addCleanup(self.replay.shutdown)

    def request(self, srv, method, path, body=None, headers=None):
        host = "127.0.0.1:%d" % srv.server_address[1]
        h = {"Host": host, "Origin": "http://" + host, "Content-Type": "application/json"}
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=3)
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        response = c.getresponse()
        raw = response.read()
        c.close()
        return response.status, json.loads(raw) if response.getheader("Content-Type") == "application/json" else raw

    def test_standalone_without_replay_or_robot(self):
        srv = server.make_server(None, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        code, meta = self.request(srv, "GET", "/api/meta")
        self.assertEqual(code, 200)
        self.assertEqual(meta, {"mode": "catalog", "modes": ["catalog"], "source": "MCP", "format": "edge-atlas/1"})
        self.assertEqual(self.request(srv, "GET", "/api/bundle")[0], 404)
        self.assertEqual(self.request(srv, "GET", "/api/catalog")[1]["sources"], [])
        from neurobot.ui import __main__ as cli
        with mock.patch.object(server, "serve", return_value=0) as launch:
            self.assertEqual(cli.main(["catalog", "--port", "18126", "--source", self.fake.url]), 0)
            launch.assert_called_once_with(None, 18126, "127.0.0.1", sources=[self.fake.url], traces=[],
                                           state_dir=server.default_state_dir(), assistant_binary=None)

    def test_replay_source_configuration_and_same_origin_guard(self):
        code, doc = self.request(self.replay, "GET", "/api/catalog")
        self.assertEqual((code, doc["devices"], doc["sources"]), (200, [], []))
        for origin in ("http://evil.example", "http://localhost:9999", "null", ""):
            code, _ = self.request(self.replay, "POST", "/api/catalog/sources", {"url": self.fake.url},
                                   {"Origin": origin})
            self.assertEqual(code, 403)
        code, doc = self.request(self.replay, "POST", "/api/catalog/sources", {"url": self.fake.url})
        self.assertEqual(code, 200)
        sid = doc["source_id"]
        wait_for(lambda: self.request(self.replay, "GET", "/api/catalog")[1]["devices"])
        code, doc = self.request(self.replay, "POST", "/api/catalog/disconnect", {"id": sid})
        self.assertEqual((code, doc["devices"]), (200, []))
        self.assertEqual(self.request(self.replay, "POST", "/api/stop", {})[0], 405)
        self.assertEqual(self.request(self.replay, "PUT", "/api/catalog/sources", {})[0], 405)

    def test_live_catalog_requires_existing_auth_and_csrf(self):
        auth = gateway.Auth(os.path.join(self.temp.name, "auth"))
        hub = live.Hub()
        actions = type("Actions", (), {"name": "ui_gateway"})()
        gw = gateway.Gateway(hub, live.Live(hub), actions, auth, "двойник", "test")
        srv = gateway.make_server(gw, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.assertEqual(self.request(srv, "GET", "/api/catalog")[0], 401)
        sid = auth.new_session()
        cookie = auth.set_cookie(sid).split(";", 1)[0]
        self.assertEqual(self.request(srv, "POST", "/api/catalog/sources", {"url": self.fake.url},
                                      {"Cookie": cookie})[0], 403)
        headers = {"Cookie": cookie, "X-Neurobot-CSRF": auth.csrf_for(sid)}
        code, doc = self.request(srv, "POST", "/api/catalog/sources", {"url": self.fake.url}, headers)
        self.assertEqual(code, 200)
        self.assertTrue(doc["ok"])
        self.assertEqual(self.request(srv, "GET", "/api/catalog", headers=headers)[0], 200)


if __name__ == "__main__":
    unittest.main()
