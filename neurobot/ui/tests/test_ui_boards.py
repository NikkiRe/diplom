"""Passive portable probe, persisted passports and explicit MCP recognition."""
from __future__ import annotations

import importlib.util
import json
import math
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from neurobot.ui import boards
from neurobot.ui.lab import LabError

spec = importlib.util.spec_from_file_location("edge_probe", boards.PROBE)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def write(root, path, value):
    target = Path(root) / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(value if isinstance(value, bytes) else str(value).encode())


def fixture(root):
    fields = {
        "/sys/firmware/devicetree/base/model": b"Acme Experimental ARM Board\x00",
        "/sys/firmware/devicetree/base/manufacturer": b"Acme Devices\x00",
        "/sys/firmware/devicetree/base/compatible": b"acme,edge-42\x00vendor,soc-123\x00",
        "/sys/class/dmi/id/product_name": "wrong lower-priority DMI model",
        "/etc/hostname": "raspberrypi-jetson-secret",  # must never become model evidence
        "/etc/os-release": 'ID=debian\nNAME="Debian GNU/Linux"\nVERSION_ID="12"\nPRETTY_NAME="Debian 12"\n',
        "/proc/cpuinfo": "processor : 0\nmodel name : Example CPU\nHardware : Example SoC\nprocessor : 1\n",
        "/proc/meminfo": "MemTotal: 2097152 kB\nMemAvailable: 1048576 kB\n",
        "/proc/uptime": "123.5 200\n", "/proc/sys/kernel/osrelease": "6.6.1-fixture",
        "/sys/class/block/mmcblk0/size": "8192", "/sys/class/block/mmcblk0/removable": "0",
        "/sys/class/block/mmcblk0/ro": "0", "/sys/class/block/mmcblk0/device/model": "SD card",
        "/sys/class/net/eth0/operstate": "up", "/sys/class/net/eth0/mtu": "1500",
        "/sys/class/net/eth0/speed": "1000", "/sys/class/net/eth0/address": "aa:bb:cc:dd:ee:ff",
        "/sys/bus/usb/devices/1-1/idVendor": "046d", "/sys/bus/usb/devices/1-1/idProduct": "0825",
        "/sys/bus/usb/devices/1-1/manufacturer": "Logitech", "/sys/bus/usb/devices/1-1/product": "Webcam",
        "/sys/bus/usb/devices/1-1/serial": "private-device-serial",
        "/sys/class/i2c-adapter/i2c-1/name": "SoC I2C controller",
        "/sys/bus/i2c/devices/1-0076/name": "bme280",
        "/sys/bus/spi/devices/spi0.0/modalias": "spidev",
        "/dev/ttyAMA0": "", "/dev/spidev0.0": "", "/dev/gpiochip0": "",
        "/sys/class/gpio/gpiochip0/label": "pinctrl", "/sys/class/gpio/gpiochip0/ngpio": "32",
        "/sys/bus/iio/devices/iio:device0/name": "adc",
        "/sys/bus/iio/devices/iio:device0/in_voltage0_raw": "1500",
        "/sys/bus/iio/devices/iio:device0/in_voltage_scale": "0.001",
        "/sys/class/hwmon/hwmon0/name": "cpu_thermal",
        "/sys/class/hwmon/hwmon0/temp1_input": "42000",
        "/sys/class/video4linux/video0/name": "USB Video",
        "/dev/video0": "",
        "/etc/systemd/system/hw-mcp.service": '[Service]\nExecStart=/usr/local/bin/hw-mcp -transport http -addr 127.0.0.1:19000\nEnvironment="KEY=not-exported-secret"\n# endpoint http://127.0.0.1:19000/mcp\n',
        "/usr/local/bin/cam-mcp": "binary fixture",
    }
    for path, value in fields.items():
        write(root, path, value)


def minimal_report():
    return {"format": "edge-board/1", "collected_at": 1000, "identity": {"kind": "unknown", "model": None},
            "system": {"os": {}, "architecture": None}, "buses": {}, "storage": [], "interfaces": [],
            "services": [], "mcp_candidates": [], "coverage": []}


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fixture(self.tmp.name)

    def test_arbitrary_linux_board_all_buses_and_source_evidence(self):
        report = probe.collect(self.tmp.name)
        self.assertEqual(report["format"], "edge-board/1")
        self.assertEqual(report["identity"]["model"], "Acme Experimental ARM Board")
        self.assertEqual(report["identity"]["manufacturer"], "Acme Devices")
        self.assertEqual(report["identity"]["vendor_id"], "acme")
        self.assertEqual(report["identity"]["kind"], "linux_device")
        self.assertIn("devicetree", report["identity"]["source"])
        self.assertEqual(report["system"]["cpu"]["logical_cores"], 2)
        self.assertEqual(report["system"]["memory"]["total_bytes"], 2097152 * 1024)
        self.assertEqual(report["storage"][0]["size_bytes"], 8192 * 512)
        for key in ("usb", "i2c", "spi", "serial", "gpio", "iio", "hwmon", "camera"):
            self.assertTrue(report["buses"][key], key)
        self.assertEqual(report["buses"]["hwmon"][0]["channels"][0]["value"], "42000")
        self.assertTrue(any(row.get("url") == "http://127.0.0.1:19000/mcp" for row in report["mcp_candidates"]))
        self.assertTrue(all(row["status"] != "verified" for row in report["mcp_candidates"]))

    def test_no_serial_mac_credentials_or_hostname_guesses(self):
        report = probe.collect(self.tmp.name)
        raw = json.dumps(report)
        for secret in ("private-device-serial", "aa:bb:cc:dd:ee:ff", "not-exported-secret", "raspberrypi-jetson-secret"):
            self.assertNotIn(secret, raw)
        self.assertTrue(report["redacted"])
        with tempfile.TemporaryDirectory() as empty:
            write(empty, "/etc/hostname", "jetson-nano")
            unknown = probe.collect(empty)
        self.assertIsNone(unknown["identity"]["model"])
        self.assertIsNone(unknown["identity"]["manufacturer"])

    def test_fallback_proc_dt_dmi_and_missing_are_honest(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, "/proc/device-tree/model", b"Other ARM board\x00")
            report = probe.collect(root)
            self.assertEqual(report["identity"]["model"], "Other ARM board")
            self.assertEqual(report["identity"]["source"], "/proc/device-tree/model")
        with tempfile.TemporaryDirectory() as root:
            write(root, "/sys/class/dmi/id/product_name", "x86 development PC")
            write(root, "/sys/class/dmi/id/sys_vendor", "Example Vendor")
            report = probe.collect(root)
            self.assertEqual(report["identity"]["model"], "x86 development PC")
            self.assertEqual(report["identity"]["manufacturer"], "Example Vendor")
            self.assertEqual(report["buses"]["i2c"], [])
            self.assertEqual(next(x for x in report["coverage"] if x["id"] == "i2c")["status"], "unavailable")

    def test_permission_and_limits_explicit_without_driver_commands(self):
        collector = probe.Probe(self.tmp.name)
        original = os.open
        def protected(path, flags, *args, **kwargs):
            if str(path).endswith("/proc/meminfo"):
                raise PermissionError()
            return original(path, flags, *args, **kwargs)
        with mock.patch.object(probe.os, "open", side_effect=protected), mock.patch.object(probe.subprocess, "run") as command:
            report = collector.collect()
            command.assert_not_called()
        system = next(row for row in report["coverage"] if row["id"] == "system")
        self.assertEqual(system["status"], "partial")
        self.assertTrue(any(error["code"] == "permission_denied" for error in system["errors"]))
        self.assertIsNone(report["system"]["memory"]["total_bytes"])
        with mock.patch.object(probe, "MAX_FILES", 2):
            limited = probe.collect(self.tmp.name)
        self.assertTrue(any(row["status"] == "limited" for row in limited["coverage"]))

    def test_symlink_escape_is_not_read_from_fixture(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "proc"
            target.symlink_to("/proc")
            report = probe.collect(root)
            self.assertIsNone(report["system"]["memory"]["total_bytes"])

    def test_portable_standalone_cli_has_no_repo_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "edge_probe.py"
            script.write_bytes(boards.PROBE.read_bytes())
            result = subprocess.run([__import__("sys").executable, str(script), "--root", self.tmp.name],
                                    cwd=directory, capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["identity"]["model"], "Acme Experimental ARM Board")

    def test_mac_is_host_even_without_sysctl_access(self):
        with mock.patch.object(probe.Probe, "command", return_value=None):
            report = probe.collect(self.tmp.name, system="Darwin")
        self.assertEqual(report["identity"]["kind"], "host")
        self.assertIsNone(report["identity"]["model"])
        self.assertEqual(report["system"]["os"]["name"], "macOS")
        self.assertEqual(report["buses"]["i2c"], [])


class RegistryTest(unittest.TestCase):
    def test_persistence_import_raw_unknown_fields_and_forget(self):
        report = minimal_report()
        report["vendor_extension"] = {"mystery": [False, None, "retained"]}
        with tempfile.TemporaryDirectory() as directory:
            registry = boards.BoardRegistry(directory)
            entry = registry.import_report({"content": json.dumps(report), "label": "My board"})["board"]
            restored = boards.BoardRegistry(directory)
            self.assertEqual(restored.get(entry["id"])["report"], report)
            self.assertEqual(restored.get(entry["id"])["sha256"], entry["sha256"])
            self.assertEqual(os.stat(Path(directory) / "boards.json").st_mode & 0o777, 0o600)
            self.assertEqual(restored.forget(entry["id"]), {"forgotten": entry["id"]})
            self.assertEqual(boards.BoardRegistry(directory).snapshot()["boards"], [])

    def test_invalid_import_and_storage_caps_are_atomic(self):
        registry = boards.BoardRegistry()
        bad = ({"path": "/etc/passwd"}, {}, {"report": minimal_report(), "content": "{}"},
               {"content": "not-json"}, {"report": {"format": "other"}}, {"report": minimal_report(), "label": ""})
        for body in bad:
            self.assertGreaterEqual(boards.mutate(registry, "/api/boards/import", body)[0], 400)
        invalid = minimal_report()
        invalid["unknown"] = math.inf
        self.assertEqual(boards.mutate(registry, "/api/boards/import", {"report": invalid})[0], 400)
        with mock.patch.object(boards, "MAX_REPORT_BYTES", 20):
            self.assertEqual(boards.mutate(registry, "/api/boards/import", {"report": minimal_report()})[0], 413)
        with mock.patch.object(boards, "MAX_BOARDS", 1):
            registry.import_report({"report": minimal_report()})
            second = minimal_report()
            second["collected_at"] = 2000
            self.assertEqual(boards.mutate(registry, "/api/boards/import", {"report": second})[0], 413)
        self.assertEqual(len(registry.snapshot()["boards"]), 1)

    def test_copy_isolation_and_persistence_failure_keep_previous(self):
        registry = boards.BoardRegistry()
        entry = registry.import_report({"report": minimal_report()})["board"]
        entry["report"]["identity"]["model"] = "tampered"
        self.assertIsNone(registry.get(entry["id"])["report"]["identity"]["model"])
        with tempfile.TemporaryDirectory() as directory:
            registry = boards.BoardRegistry(directory)
            with mock.patch.object(boards.os, "replace", side_effect=PermissionError("denied")):
                self.assertEqual(boards.mutate(registry, "/api/boards/import", {"report": minimal_report()})[0], 503)
            self.assertEqual(registry.snapshot()["boards"], [])

    def test_corrupt_persistence_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boards.json"
            path.write_text("not json")
            with self.assertRaises(LabError):
                boards.BoardRegistry(directory)
            self.assertEqual(path.read_text(), "not json")

    def test_local_actual_host_probe_and_no_paths(self):
        registry = boards.BoardRegistry()
        result = registry.local()["board"]
        self.assertEqual(result["id"], "board-local")
        self.assertEqual(result["origin"], "local")
        self.assertEqual(result["report"]["collector"]["platform"], platform.system())
        if platform.system() == "Darwin":
            self.assertEqual(result["report"]["identity"]["kind"], "host")
        self.assertEqual(boards.mutate(registry, "/api/boards/local", {"path": "/etc"})[0], 400)
        self.assertEqual(boards.mutate(registry, "/api/boards/forget", {"id": "missing"})[0], 404)


class FakeMCP:
    def __init__(self):
        self.calls, self.deleted = [], []
        self.status = 200
        self.html = False
        self.bad_tools = False
        self.version = "2025-11-25"
        fake = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_DELETE(self):  # noqa: N802
                fake.deleted.append(self.headers.get("Mcp-Session-Id"))
                self.send_response(200)
                self.end_headers()
            def do_POST(self):  # noqa: N802
                call = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append(call)
                method = call["method"]
                if fake.status != 200 or fake.html:
                    self.send_response(fake.status)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html>not MCP</html>")
                    return
                if method == "notifications/initialized":
                    self.send_response(202)
                    self.end_headers()
                    return
                if method == "initialize":
                    result = {"serverInfo": {"name": "Arbitrary Vendor MCP", "version": "1.2"}, "protocolVersion": fake.version,
                              "capabilities": {"tools": {}, "resources": {}}}
                elif method == "tools/list":
                    if fake.bad_tools:
                        result = {"tools": "invalid"}
                    else:
                        result = {"tools": [{"name": "dangerous_relay_write", "inputSchema": {"type": "object"}}]}
                elif method == "resources/list":
                    result = {"resources": [{"uri": "custom://state", "mimeType": "application/json"}]}
                else:
                    raise AssertionError("unexpected action " + method)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "inspection-only")
                self.end_headers()
                self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": call["id"], "result": result}).encode())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d/mcp" % self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class MCPInspectionTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMCP()
        self.addCleanup(self.fake.close)
        self.registry = boards.BoardRegistry()

    def test_generic_valid_mcp_fingerprint_no_tool_execution(self):
        result = self.registry.inspect_mcp({"url": self.fake.url})
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["server"]["name"], "Arbitrary Vendor MCP")
        self.assertEqual(result["fingerprint"]["resource_schemes"], ["custom"])
        self.assertEqual(result["fingerprint"]["tools_called"], 0)
        self.assertEqual([c["method"] for c in self.fake.calls], ["initialize", "notifications/initialized", "tools/list", "resources/list"])
        self.assertEqual(self.fake.deleted, ["inspection-only"])

    def test_auth_html_wrong_endpoint_and_bad_spec_distinguished(self):
        for status, expected in ((401, "auth_required"), (403, "auth_required"), (404, "not_mcp"), (405, "not_mcp")):
            self.fake.status = status
            self.assertEqual(self.registry.inspect_mcp({"url": self.fake.url})["status"], expected)
        self.fake.status = 200
        self.fake.html = True
        self.assertEqual(self.registry.inspect_mcp({"url": self.fake.url})["issues"][0]["code"], "html_not_mcp")
        self.fake.html = False
        self.fake.bad_tools = True
        result = self.registry.inspect_mcp({"url": self.fake.url})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["resources"][0]["uri"], "custom://state")
        self.assertFalse(any(c["method"] == "tools/call" for c in self.fake.calls))

    def test_unsupported_revision_and_stdio_do_not_execute(self):
        self.fake.version = "1900-01-01"
        result = self.registry.inspect_mcp({"url": self.fake.url})
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(len(self.fake.calls), 1)
        with mock.patch.object(boards.subprocess, "Popen") as process:
            result = self.registry.inspect_mcp({"url": "python3 -m vendor_mcp"})
            self.assertEqual(result["status"], "stdio")
            process.assert_not_called()

    def test_ssrf_invalid_url_and_arbitrary_commands_rejected(self):
        for url in ("http://192.168.1.2/mcp", "https://127.0.0.1/mcp", "http://localhost.evil/mcp", "http://user:pass@127.0.0.1/mcp"):
            self.assertEqual(boards.mutate(self.registry, "/api/boards/mcp/inspect", {"url": url})[0], 400)
        self.assertEqual(boards.mutate(self.registry, "/api/boards/mcp/inspect", {"url": self.fake.url, "command": "anything"})[0], 400)
        self.assertEqual(self.fake.calls, [])


@unittest.skipUnless(os.environ.get("BOARD_TEST_HW_BIN"), "set BOARD_TEST_HW_BIN for actual native hw-mcp")
class NativeMCPTest(unittest.TestCase):
    def test_actual_project_hardware_mcp_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready, trace = root / "ready", root / "trace.jsonl"
            fixture_root = root / "fixture"
            shutil.copytree(boards.ROOT / "hwmcp/testdata/fixture_root", fixture_root, symlinks=True)
            with (root / "log").open("wb") as log:
                proc = subprocess.Popen([os.environ["BOARD_TEST_HW_BIN"], "-transport", "http", "-addr", "127.0.0.1:0",
                                         "-root", str(fixture_root), "-ready-file", str(ready), "-trace", str(trace)],
                                        stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(ready.exists(), (root / "log").read_text())
                    result = boards.inspect_endpoint({"url": ready.read_text().strip()})
                    self.assertEqual(result["status"], "valid", result)
                    self.assertTrue(result["tools"])
                    self.assertIn("hw", result["fingerprint"]["resource_schemes"])
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                    try:
                        proc.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
            records = [json.loads(line) for line in trace.read_text().splitlines()]
            self.assertFalse(any(r["type"] == "tool_call" for r in records))


if __name__ == "__main__":
    unittest.main()
