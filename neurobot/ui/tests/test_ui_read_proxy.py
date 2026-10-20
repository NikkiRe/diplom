import copy
import json
import unittest

from neurobot.ui import catalog, read_proxy


class Registry:
    def __init__(self):
        self.doc = {"sources": [{"id": "s1", "status": "ready"}], "devices": [
            {"id": "s1/test", "name": "actual observed shape", "source": "s1", "properties": [
                {"name": "zero", "value": 0, "status": "ok", "ts": 100},
                {"name": "off", "value": False, "status": "ok", "ts": 100},
                {"name": "stale", "value": 55, "status": "stale", "ts": 20}]}]}
    def snapshot(self):
        return copy.deepcopy(self.doc)


class ReadProxyTest(unittest.TestCase):
    def test_imported_summary_uses_byte_limits_and_unknown_bus_is_not_zero(self):
        text = "板" * 1000
        report = {"identity": {k: text for k in ("kind", "model", "manufacturer", "source", "vendor_id")},
                  "system": {"architecture": text, "os": {k: text for k in ("pretty_name", "kernel", "id", "version_id")},
                             "cpu": {"model": text, "hardware": text}},
                  "buses": {"usb": 1, "gpio": [], "custom": "unknown"}, "coverage": []}
        summary = read_proxy.board_summary({"report": report, "sha256": "a" * 64})
        self.assertLess(len(read_proxy._bytes(summary)), 2048)
        self.assertIsNone(summary["bus_counts"]["usb"])
        self.assertEqual(summary["bus_counts"]["gpio"], 0)
        self.assertEqual(summary["sha256"], "a" * 64)
        self.assertTrue(summary["summary_limited"])

    def test_all_collected_channels_remain_addressable_and_raw_passport_is_on_demand(self):
        registry = Registry()
        registry.doc["devices"][0]["properties"].extend({"name": "channel-" + str(i), "value": i, "status": "ok"} for i in range(140))
        registry.doc["devices"].extend({"id": "device-" + str(i), "name": str(i), "source": "s1", "properties": []} for i in range(520))
        report = {"identity": {"model": "Arbitrary Board", "custom_bulk": "x" * 100000},
                  "system": {"architecture": "arm64", "cpu": {"model": "Real CPU"}, "unknown": "x" * 100000},
                  "buses": {}, "coverage": [], "collected_at": 100}
        class Boards:
            def snapshot(self):
                return {"boards": [{"id": "board-test", "label": "Arbitrary Board", "report": report}]}
        proxy = read_proxy.ReadProxy(registry, Boards())
        self.assertEqual(len(proxy.devices()), 522)
        exact = proxy.dispatch("tools/call", {"name": "read_property", "arguments": {"device": "s1/test", "property": "channel-139"}})
        self.assertEqual(json.loads(exact["content"][0]["text"])["value"], 139)
        summary = proxy.devices()[-1]["properties"]["summary"]["value"]
        self.assertLess(len(read_proxy._bytes(summary)), 2048)
        self.assertEqual(summary["identity"]["model"], "Arbitrary Board")
        inventory = proxy.inventory(proxy.devices())
        board = next(d for d in inventory["devices"] if d["id"] == "board:board-test")
        self.assertEqual(board["properties"]["summary"]["value"]["system"]["cpu"]["model"], "Real CPU")
        self.assertEqual(board["properties"]["passport"]["status"], "omitted_from_automatic_context")
        self.assertEqual(len(proxy.dispatch("resources/list", {})["resources"]), 1)
        original = proxy.dispatch("tools/call", {"name": "read_property", "arguments": {"device": "board:board-test", "property": "passport"}})
        self.assertEqual(json.loads(original["content"][0]["text"])["value"], report)

    def test_automatic_context_bounded_and_exact_value_remains_readable(self):
        registry = Registry()
        original = {"samples": list(range(5000)), "unit": "raw"}
        registry.doc["devices"][0]["properties"].append({"name": "wave", "value": original, "status": "ok", "ts": 101})
        registry.doc["devices"].extend({"id": "large-" + str(i), "name": "Device", "source": "s1",
            "properties": [{"name": "p" + str(j), "value": "x" * 3000, "status": "ok"} for j in range(50)]} for i in range(40))
        proxy = read_proxy.ReadProxy(registry)
        inventory = proxy.inventory(proxy.devices())
        self.assertLess(len(read_proxy._bytes(inventory)), read_proxy.MAX_INVENTORY)
        self.assertGreater(inventory["automatic_context"]["omitted_values"], 0)
        self.assertGreater(inventory["automatic_context"]["omitted_devices"], 0)
        properties = inventory["devices"][0]["properties"]
        self.assertEqual(properties["zero"]["value"], 0)
        self.assertIs(properties["off"]["value"], False)
        self.assertEqual(properties["wave"]["status"], "omitted_from_automatic_context")
        exact = proxy.dispatch("tools/call", {"name": "read_property", "arguments": {"device": "s1/test", "property": "wave"}})
        self.assertEqual(json.loads(exact["content"][0]["text"])["value"], original)
        page = proxy.dispatch("tools/call", {"name": "list_devices", "arguments": {"offset": 1, "limit": 2}})
        listing = json.loads(page["content"][0]["text"])
        self.assertEqual(len(listing["devices"]), 2)
        self.assertEqual(listing["next_offset"], 3)
        self.assertNotIn("value", listing["devices"][0]["properties"]["p0"])

    def test_mcp_readonly_projection_preserves_truth(self):
        registry = Registry()
        proxy = read_proxy.ReadProxy(registry)
        url = proxy.start()
        self.addCleanup(proxy.close)
        client = catalog.Client(url)
        devices, metadata, warnings = client.collect("read-source")
        self.assertEqual(metadata["server_info"]["name"], "edgelab-observations")
        self.assertFalse(warnings, warnings)
        self.assertEqual(len(devices), 1)
        values = {p["name"]: p["value"] for p in devices[0]["properties"]}
        self.assertEqual(values["zero"], 0)
        self.assertIs(values["off"], False)
        self.assertIsNone(values["stale"])
        good = proxy.dispatch("tools/call", {"name": "read_property", "arguments": {"device": "s1/test", "property": "off"}})
        self.assertFalse(good["isError"])
        self.assertIs(json.loads(good["content"][0]["text"])["value"], False)
        bad = proxy.dispatch("tools/call", {"name": "read_property", "arguments": {"device": "s1/test", "property": "stale"}})
        self.assertTrue(bad["isError"])
        for name in ("write_property", "look", "snapshot", "set_detector", "stop", "arbitrary"):
            with self.assertRaises(ValueError):
                proxy.dispatch("tools/call", {"name": name, "arguments": {}})
        names = [t["name"] for t in proxy.dispatch("tools/list", {})["tools"]]
        self.assertEqual(names, ["list_devices", "read_property"])
        registry.doc["sources"][0]["status"] = "error"
        failed = proxy.dispatch("tools/call", {"name": "read_property", "arguments": {"device": "s1/test", "property": "zero"}})
        self.assertTrue(failed["isError"])
        self.assertIsNone(json.loads(failed["content"][0]["text"])["value"])
