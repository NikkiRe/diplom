"""Browser acceptance for the dynamic equipment/signal explorer, without ROS or real actuators.

Run with the same Playwright Python environment as e2e_design.py. A synthetic MCP HTTP server exposes a device
with an unpredictable id and previously unknown properties. It records every RPC and never executes hardware
actions. Optionally --hw-mcp FILE also starts the native hw-mcp binary against a temporary copy of fixture_root.
Only localhost is used. Screenshots and the JSON acceptance report go to --out.
"""
from __future__ import annotations

import argparse
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from neurobot.ui import server, stream  # noqa: E402
from neurobot.ui.tests.e2e_design import Acceptance, SAMPLE, SIZES, LAYOUT, txt, wait_for  # noqa: E402


class UnknownMCP:
    """Minimal Streamable HTTP MCP peer with real JSON-RPC discovery/state replies and an RPC audit."""

    def __init__(self):
        self.device_id = "unfamiliar_sensor_" + uuid.uuid4().hex[:8]
        self.calls = []
        self.reads = 0
        self.failed = False
        self.server = None
        self.properties = {
            "mystery_flux": {"type": "number", "unit": "μT", "writable": False},
            "digital_zero": {"type": "number", "writable": False},
            "is_ready": {"type": "boolean", "writable": False},
            "missing_value": {"type": "number", "writable": False},
            "vendor.probe/edge": {"type": "number", "writable": False},
            "nested_probe": {"type": "object", "writable": False},
        }

    def descriptor(self):
        return {"id": self.device_id, "name": "Unknown QA instrument", "source": "qa-bus", "kind": "instrument",
                "location": "browser acceptance lab", "properties": self.properties,
                "actions": {"dangerous_action_never_call": {"description": "Capability metadata only",
                            "input": {"type": "object", "properties": {"power": {"type": "number"}}}}}}

    def state(self):
        self.reads += 1
        now = time.time()
        values = {"mystery_flux": round(41.25 + self.reads / 100, 2), "digital_zero": 0, "is_ready": False,
                  "missing_value": None, "vendor.probe/edge": -2.125,
                  "nested_probe": {"gain": 0, "enabled": False, "levels": [None, 3.5]}}
        props = {key: {"value": value, "ts": now, "age_s": 0,
                      **({"unit": self.properties[key]["unit"]} if self.properties[key].get("unit") else {}),
                      **({"error": "qa_sensor_unavailable"} if value is None else {})} for key, value in values.items()}
        return {"device": self.device_id, "now": now, "properties": props}

    def rpc(self, request):
        self.calls.append(copy.deepcopy(request))
        method, params = request.get("method"), request.get("params") or {}
        if method == "initialize":
            return {"protocolVersion": "2025-11-25", "capabilities": {"resources": {}, "tools": {}},
                    "serverInfo": {"name": "qa-unknown-mcp", "version": "1"}}
        if method == "tools/list":
            return {"tools": [{"name": name, "description": name,
                               "inputSchema": {"type": "object", "properties": {}},
                               "annotations": {"readOnlyHint": name in ("list_devices", "read_property")}}
                              for name in ("list_devices", "read_property", "write_property", "dangerous_action_never_call")]}
        if method == "resources/list":
            return {"resources": [{"uri": "hw://inventory", "name": "inventory", "mimeType": "application/json"},
                                  {"uri": "hw://state/" + self.device_id, "name": "state", "mimeType": "application/json"}]}
        if method == "resources/read":
            uri = params.get("uri")
            if uri == "hw://inventory":
                value = {"devices": [self.descriptor()]}
            elif uri == "hw://state/" + self.device_id:
                value = self.state()
            else:
                raise ValueError("unknown resource " + str(uri))
            return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(value)}]}
        if method == "tools/call" and params.get("name") == "list_devices":
            value = {"devices": [self.descriptor()]}
            return {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value}
        if method == "tools/call" and params.get("name") == "read_property":
            value = self.state()["properties"].get(params.get("arguments", {}).get("property"))
            return {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value}
        if method and method.startswith("notifications/"):
            return None
        raise ValueError("unexpected RPC " + str(method))

    @property
    def actuator_calls(self):
        return [call for call in self.calls if call.get("method") == "tools/call"
                and (call.get("params") or {}).get("name") not in ("list_devices", "read_property", "board_health")]

    def __enter__(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def send(self, status, doc=None):
                data = json.dumps(doc).encode() if doc is not None else b""
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                if doc is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "qa-session")
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if fixture.failed:
                    fixture.calls.append(request)
                    return self.send(503, {"error": "qa source deliberately unavailable"})
                try:
                    result = fixture.rpc(request)
                    if "id" not in request:
                        return self.send(202)
                    self.send(200, {"jsonrpc": "2.0", "id": request["id"], "result": result})
                except ValueError as exc:
                    self.send(200, {"jsonrpc": "2.0", "id": request.get("id"),
                                    "error": {"code": -32601, "message": str(exc)}})

            def do_GET(self):
                self.send(405)

            def do_DELETE(self):
                self.send(200)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()


class NativeHardware:
    def __init__(self, binary, temp, out):
        self.binary, self.temp, self.out = binary, Path(temp), Path(out)
        self.child = None

    def __enter__(self):
        fixture = self.temp / "hardware-root"
        self.fixture = fixture
        # Git does not retain empty sysfs target directories; dangling fixture links are intentional.
        shutil.copytree(ROOT / "hwmcp/testdata/fixture_root", fixture, symlinks=True)
        ready = self.temp / "hw-mcp.url"
        self.log = (self.out / "hw-mcp.stderr.log").open("w")
        self.child = subprocess.Popen([self.binary, "-root", str(fixture), "-declared",
            str(ROOT / "hwmcp/configs/declared.lab.json"), "-transport", "http", "-ready-file", str(ready)],
            stdout=subprocess.DEVNULL, stderr=self.log)
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline and self.child.poll() is None:
            time.sleep(0.1)
        if not ready.exists():
            self.__exit__()
            raise RuntimeError("native hw-mcp did not start; see hw-mcp.stderr.log")
        self.url = ready.read_text().strip()
        return self

    def __exit__(self, *_):
        if self.child:
            self.child.terminate()
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=5)
        self.log.close()


def instrument_bundle(original):
    """Inject novel nested names into permitted schema maps; stream's top-level schema remains valid."""
    doc = copy.deepcopy(original)
    for run in doc["runs"]:
        for message in run["stream"]:
            if message["k"] == "safety":
                message.setdefault("limits", {}).update({"qa_zero": 0, "qa_null": None})
            if message["k"] == "mem":
                message.setdefault("units", {})["qa.vendor/novel_component"] = 12.5
    stream.check_bundle(doc)
    return doc


def flatten_fields(value, path=()):
    """Independent expected JSON leaves, preserving null/false/zero and literal dots in map keys."""
    if isinstance(value, dict) and value:
        return [leaf for key, child in value.items() for leaf in flatten_fields(child, path + (key,))]
    if isinstance(value, list) and value:
        return [leaf for index, child in enumerate(value) for leaf in flatten_fields(child, path + (index,))]
    return [(path, value)]


def field_name(path):
    result = ""
    for token in path:
        result += f"[{token}]" if isinstance(token, int) else ("." if result else "") + token
    return result


class EquipmentAcceptance(Acceptance):
    def device_button(self, page, device_id):
        return page.locator("[data-testid=equipment-device][data-device-id=" + json.dumps(device_id) + "]")

    def select_device(self, page, device_id):
        button = self.device_button(page, device_id)
        button.wait_for(timeout=15000)
        button.click()
        page.locator("[data-testid=device-detail]").wait_for()
        if not wait_for(page, lambda: button.get_attribute("aria-pressed") == "true", 5):
            raise RuntimeError("Device selection did not settle: " + device_id)
        # Raw/field mode and search are independent user controls, including reselection of the same device.
        page.locator(".eq-inspector-tabs button").first.click()
        page.locator("[data-testid=signal-search]").wait_for()
        page.locator("[data-testid=signal-search]").fill("")

    def signal_row(self, page, name):
        return page.locator("[data-testid=signal-row][data-path=" + json.dumps(name) + "]")

    def choose_field(self, page, name):
        page.locator("[data-testid=signal-search]").fill(name)
        row = self.signal_row(page, name)
        row.wait_for()
        row.locator(".eq-field-select").click()
        if not wait_for(page, lambda: txt(page, "[data-testid=selected-signal]") == name, 5):
            raise RuntimeError("Signal selection did not settle: " + name)
        return row

    def catalog(self, context, base):
        response = context.request.get(base + "/api/catalog")
        if response.status != 200:
            raise RuntimeError(f"catalog returned HTTP {response.status}")
        return response.json()

    def catalog_source(self, page, context, base, source_id, statuses=("ready",), timeout=20):
        def ready():
            source = next((source for source in self.catalog(context, base).get("sources", [])
                           if source.get("id") == source_id), None)
            return source if source and source.get("status") in statuses else None
        return wait_for(page, ready, timeout)

    def source_id(self, context, base, url):
        return next((source["id"] for source in self.catalog(context, base).get("sources", [])
                     if source.get("url") == url), None)

    def scene_matrix(self, browser, base, context=None, mode="replay"):
        for theme in ("dark", "light"):
            for width, height in SIZES:
                ctx = context or browser.new_context(viewport={"width": width, "height": height},
                    color_scheme=theme, reduced_motion="reduce", locale="ru-RU")
                page = ctx.new_page()
                self.observe(page)
                page.set_viewport_size({"width": width, "height": height})
                page.emulate_media(color_scheme=theme, reduced_motion="reduce")
                self.phase = f"equipment/{mode}/{theme}/{width}x{height}"
                try:
                    page.goto(base + f"/?theme={theme}&t=1095#/equipment")
                    self.ready(page)
                    page.locator("main[data-screen=equipment]").wait_for()
                    layout = page.evaluate(LAYOUT)
                    name = f"equipment_{mode}_{theme}_{width}x{height}"
                    self.check(name + "_width", layout["width"] <= width + 1, layout)
                    self.check(name + "_theme", page.locator("html").get_attribute("data-theme") == theme)
                    self.check(name + "_nav", page.locator("nav a[href='#/equipment']").get_attribute("aria-current") == "page")
                    self.screenshot(page, name)
                    self.report["pages"].append({"mode": mode, "theme": theme, "size": [width, height],
                        "screen": "equipment", "layout": layout, "screenshot": name + ".png" if self.shots else None})
                finally:
                    page.close()
                    if context is None:
                        ctx.close()

    def replay_fields(self, browser, base, bundle):
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
        page = context.new_page()
        self.observe(page)
        run = next(run for run in bundle["runs"] if run["id"] == "a1v2")
        at = run["anchor_t"] + 1095
        try:
            page.goto(base + "/?run=a1v2&t=1095&theme=dark")
            self.ready(page)
            self.check("equipment_is_default", page.locator("main").get_attribute("data-screen") == "equipment")
            page.keyboard.press("1")
            page.keyboard.press("4")
            page.locator("main[data-screen=equipment]").wait_for()
            self.check("equipment_keyboard_four", page.locator("nav a[href='#/equipment']").get_attribute("aria-current") == "page")

            for kind in ("safety", "health", "mem", "units"):
                message = next(m for m in reversed(run["stream"]) if m["k"] == kind and m["t"] <= at)
                expected = [(path, value) for path, value in flatten_fields(message) if path[0] not in ("k", "t")]
                self.select_device(page, "stream:" + kind)
                while page.locator(".eq-load-more").count():
                    page.locator(".eq-load-more").click()
                paths = page.locator("[data-testid=signal-row]").evaluate_all("rows => rows.map(row => row.dataset.path)")
                self.check("all_" + kind + "_fields_browsable", set(paths) == {field_name(path) for path, _ in expected},
                           {"expected": len(expected), "shown": len(paths), "paths": paths})
                for path, value in expected:
                    name = field_name(path)
                    row = self.choose_field(page, name)
                    shown = txt(page, "[data-testid=selected-value]").removesuffix("+").strip()
                    if value is None:
                        valid = "—" in shown and "null" in row.inner_text()
                    elif isinstance(value, bool):
                        valid = str(value).lower() in shown
                    elif isinstance(value, (int, float)):
                        raw_shown = row.locator("b").evaluate("b => b.firstChild.textContent")
                        normalized = raw_shown.replace("\u00a0", "").replace("\u202f", "").replace(",", ".").replace("−", "-")
                        try:
                            valid = abs(float(normalized) - value) <= 0.00006
                        except ValueError:
                            valid = False
                    elif isinstance(value, list):
                        valid = "[]" in shown
                    elif isinstance(value, dict):
                        valid = "{}" in shown
                    else:
                        valid = str(value) in shown
                    self.check(kind + "_field_" + name, valid, {"expected": value, "shown": shown})
                page.locator("[data-testid=raw-tab]").click()
                self.check(kind + "_raw_json_exact", json.loads(page.locator("[data-testid=device-raw]").inner_text()) == message)

            self.select_device(page, "stream:safety")
            self.choose_field(page, "min_clearance_m")
            self.check("signal_selection_changes_scope", txt(page, "[data-testid=selected-signal]") == "min_clearance_m")
            self.check("signal_trace_has_recorded_points", page.locator("[data-testid=signal-trace] .eq-trace-line").count() == 1)
            for name in ("min_clearance_m", "limits.qa_zero", "limits.qa_null", "state"):
                self.choose_field(page, name)
                page.locator("[data-testid=pin-selected]").click()
            self.check("four_signals_pinned", page.locator("[data-testid=pinned-signal]").count() == 4)
            self.choose_field(page, "level")
            page.locator("[data-testid=pin-selected]").click()
            self.check("pin_limit_four", page.locator("[data-testid=pinned-signal]").count() == 4 and
                       "до четырёх" in txt(page, "[data-testid=signal-pins]"))
            self.screenshot(page, "equipment_four_pins")
            page.reload()
            self.ready(page)
            self.check("pins_persist_reload", page.locator("[data-testid=pinned-signal]").count() == 4)
            self.select_device(page, "stream:scan")
            self.check("replay_no_fabricated_scan", "Скан не входит" in txt(page, "[data-testid=device-detail]") and
                       not page.locator("[data-testid=signal-row]").count())
            page.locator("[data-testid=device-search]").fill("qa.vendor/novel_component")
            self.check("device_search_unknown_field", self.device_button(page, "stream:mem").count() == 1)
            page.locator("[data-testid=device-search]").fill("")
            animations = page.locator("[data-testid=equipment]").evaluate("e => e.getAnimations({subtree:true}).filter(a => a.playState === 'running').length")
            self.check("reduced_motion_scene_static", animations == 0, animations)
        finally:
            context.close()

    def add_source(self, page, context, base, url, label):
        page.locator("[data-testid=add-source]").click()
        dialog = page.locator("[data-testid=source-dialog]")
        dialog.wait_for()
        page.locator("[data-testid=source-url]").fill(url)
        dialog.get_by_placeholder("Например, датчики лаборатории").fill(label)
        self.screenshot(page, "equipment_add_source_dialog")
        page.locator("[data-testid=source-submit]").click()
        dialog.wait_for(state="hidden")
        source_id = wait_for(page, lambda: self.source_id(context, base, url), 10)
        self.check("source_registered_" + label, source_id is not None)
        source = self.catalog_source(page, context, base, source_id)
        self.check("source_ready_" + label, source is not None, source)
        return source_id

    def mcp_discovery(self, page, context, base, fixture):
        source_id = self.add_source(page, context, base, fixture.url, "Unknown QA instrument")
        device_id = source_id + "/" + fixture.device_id
        self.select_device(page, device_id)
        catalog = self.catalog(context, base)
        descriptor = next(device for device in catalog["devices"] if device["id"] == device_id)
        self.check("unknown_mcp_device_discovered", descriptor["source"] == source_id and
                   {p["name"] for p in descriptor["properties"]} == set(fixture.properties), descriptor)
        expected_names = {"mystery_flux", "digital_zero", "is_ready", "missing_value", "vendor.probe/edge",
                          "nested_probe.gain", "nested_probe.enabled", "nested_probe.levels[0]", "nested_probe.levels[1]"}
        paths = set(page.locator("[data-testid=signal-row]").evaluate_all("rows => rows.map(row => row.dataset.path)"))
        self.check("unknown_nested_mcp_properties_browsable", expected_names == paths, sorted(paths))
        for name, expected in (("digital_zero", "0"), ("is_ready", "false"), ("missing_value", "—"),
                               ("nested_probe.gain", "0"), ("nested_probe.enabled", "false"),
                               ("nested_probe.levels[0]", "—"), ("vendor.probe/edge", "-2,125")):
            self.choose_field(page, name)
            got = txt(page, "[data-testid=selected-value]")
            self.check("unknown_mcp_value_" + name, expected in got.replace("−", "-"), got)
        self.choose_field(page, "mystery_flux")
        before = txt(page, "[data-testid=selected-value]")
        self.check("unknown_mcp_value_updates", wait_for(page, lambda: txt(page, "[data-testid=selected-value]") != before, 12), before)
        self.check("unknown_mcp_trace_updates", wait_for(page, lambda:
            page.locator("[data-testid=signal-trace] .eq-trace-line").count() == 1, 12))
        self.check("mcp_capabilities_descriptive", "dangerous_action_never_call" in txt(page, "[data-testid=device-capabilities]")
                   and not page.get_by_role("button", name="dangerous_action_never_call", exact=True).count())
        page.locator("[data-testid=raw-tab]").click()
        raw = txt(page, "[data-testid=device-raw]")
        self.check("unknown_mcp_raw_preserved", "nested_probe" in raw and "vendor.probe/edge" in raw)
        self.screenshot(page, "equipment_unknown_mcp")
        fixture.failed = True
        source = self.catalog_source(page, context, base, source_id, statuses=("error", "degraded"), timeout=20)
        self.check("source_outage_explicit", source is not None and bool(source.get("error")), source)
        self.check("source_outage_visible", wait_for(page, lambda: page.locator("[data-testid=source-error]").count(), 10))
        self.check("source_outage_has_no_live_trace_head", wait_for(page, lambda:
            page.locator("[data-testid=signal-trace] .eq-trace-head").count() == 0, 5))
        self.screenshot(page, "equipment_source_failure")
        fixture.failed = False
        self.check("source_recovers", self.catalog_source(page, context, base, source_id, timeout=20) is not None)
        self.check("source_outage_keeps_explicit_trace_gap", wait_for(page, lambda:
            page.locator("[data-testid=signal-trace] .eq-trace-line").count() == 1 and
            page.locator("[data-testid=signal-trace] .eq-trace-line").get_attribute("d").count("M") >= 2, 8))
        self.check("no_arbitrary_actuator_rpc", not fixture.actuator_calls, fixture.actuator_calls)
        self.report["mcp_rpc_methods"] = [{"method": call.get("method"), "params": call.get("params")} for call in fixture.calls]

    def native_discovery(self, page, context, base, native):
        source_id = self.add_source(page, context, base, native.url, "Native hw-mcp fixture")
        devices = [device for device in self.catalog(context, base)["devices"] if device["source"] == source_id]
        self.check("real_hw_inventory_discovered", len(devices) >= 5, [device["id"] for device in devices])
        local_ids = {device.get("local_id") for device in devices}
        self.check("real_hw_passive_devices_discovered", {"webcam_c270", "uvc_camera_046d_0825", "usb_serial",
                   "gpiochip0", "esp32_bridge"} <= local_ids, sorted(local_ids))
        self.report["native_inventory"] = {"devices": len(devices), "properties": sum(len(d["properties"]) for d in devices),
            "rows": [{"id": d["local_id"], "properties": len(d["properties"])} for d in devices]}
        bme = next((device for device in devices if device["id"].endswith("/kitchen_bme280")), None)
        self.check("real_hw_bme280_present", bme is not None)
        if bme:
            self.select_device(page, bme["id"])
            selected_node = page.locator("[data-testid=" + json.dumps("map-node-" + bme["id"]) + "]")
            self.check("real_hw_selected_node_stays_on_map", selected_node.count() == 1 and
                       selected_node.get_attribute("aria-pressed") == "true")
            self.choose_field(page, "temperature")
            self.check("real_hw_temperature_value", "22,51" in txt(page, "[data-testid=selected-value]"))
            # Change only our temporary synthetic sysfs reading, then follow hw-mcp → catalog → browser.
            reading = native.fixture / "sys/devices/platform/soc/1f00074000.i2c/i2c-1/1-0076/iio:device0/in_temp_input"
            reading.write_text("24510\n")
            self.check("real_hw_temperature_updates", wait_for(page, lambda:
                "24,51" in txt(page, "[data-testid=selected-value]"), 10))
            self.screenshot(page, "equipment_native_hw_mcp")

    def live_scan_fields(self, page):
        self.select_device(page, "stream:scan")
        self.check("live_scan_arrived", wait_for(page, lambda:
            page.locator("[data-testid=signal-row]").count() > 0, 10))
        page.locator("[data-testid=raw-tab]").click()
        raw = json.loads(page.locator("[data-testid=device-raw]").inner_text())
        expected = {field_name(path) for path, _ in flatten_fields(raw) if path[0] not in ("k", "t")}
        page.locator(".eq-inspector-tabs button").first.click()
        while page.locator(".eq-load-more").count():
            page.locator(".eq-load-more").click()
        paths = set(page.locator("[data-testid=signal-row]").evaluate_all("rows => rows.map(row => row.dataset.path)"))
        self.check("live_full_scan_array_browsable", len(expected) >= 200 and paths == expected,
                   {"expected": len(expected), "rendered": len(paths)})
        self.choose_field(page, "r[200]")
        self.check("live_high_index_array_search", txt(page, "[data-testid=selected-signal]") == "r[200]")
        self.check("live_selected_scan_trace_accumulates", wait_for(page, lambda:
            page.locator("[data-testid=signal-trace] .eq-trace-line").count() == 1, 5))
        self.select_device(page, "stream:health")
        self.choose_field(page, "dark")
        self.check("live_boolean_false_preserved", "false" in txt(page, "[data-testid=selected-value]"))
        self.select_device(page, "stream:cmd:req")
        self.check("live_req_topic_distinct", "Запрос движения" in txt(page, "[data-testid=device-detail]"))
        self.select_device(page, "stream:cmd:out")
        self.check("live_out_topic_distinct", "Выход на привод" in txt(page, "[data-testid=device-detail]"))

    def live_equipment(self, browser, bundle_path, temporary, native_binary=None):
        state = Path(temporary) / "live-state"
        stderr = (self.out / "fake_live.stderr.log").open("w")
        child = subprocess.Popen([sys.executable, str(ROOT / "neurobot/ui/tests/fake_live.py"), "--port", "0",
            "--state", str(state), "--bundle", str(bundle_path)], stdout=subprocess.PIPE, stderr=stderr, text=True)
        login_lines = queue.Queue()
        threading.Thread(target=lambda: login_lines.put(child.stdout.readline()), daemon=True).start()
        context = None
        try:
            login = login_lines.get(timeout=15).strip()
            if not login.startswith("http://127.0.0.1:"):
                raise RuntimeError("Synthetic live gateway did not start")
            base = login.split("/login?", 1)[0]
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce", locale="ru-RU")
            page = context.new_page()
            self.observe(page)
            page.goto(login)
            self.ready(page)
            page.locator("main[data-screen=equipment]").wait_for()
            self.check("live_equipment_default", "ЖИВОЙ" in txt(page, "[data-testid=source]"))
            self.attempt("live_scan_fields", lambda: self.live_scan_fields(page))
            with UnknownMCP() as fixture:
                self.attempt("unknown_mcp_discovery", lambda: self.mcp_discovery(page, context, base, fixture))
                if native_binary:
                    with NativeHardware(native_binary, temporary, self.out) as native:
                        self.attempt("native_hw_mcp_discovery", lambda: self.native_discovery(page, context, base, native))
                        # Matrix includes cards from both the unknown device and native inventory.
                        page.close()
                        self.attempt("live_equipment_matrix", lambda: self.scene_matrix(browser, base, context=context, mode="live"))
                else:
                    page.close()
                    self.attempt("live_equipment_matrix", lambda: self.scene_matrix(browser, base, context=context, mode="live"))
                self.check("no_actuator_calls_after_all_views", not fixture.actuator_calls, fixture.actuator_calls)
        finally:
            if context:
                context.close()
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            stderr.close()

    def standalone_catalog(self, browser):
        standalone = server.make_server(None, 0)
        threading.Thread(target=standalone.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{standalone.server_address[1]}"
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
        page = context.new_page()
        self.observe(page)
        try:
            page.goto(base)
            self.ready(page)
            self.check("standalone_source_mcp", "MCP" in txt(page, "[data-testid=source]"))
            self.check("standalone_default_equipment", page.locator("main").get_attribute("data-screen") == "equipment")
            self.check("standalone_empty_has_no_invented_robot", page.locator("[data-testid=equipment-device]").count() == 0)
            self.check("standalone_no_robot_controls", not page.locator("[data-testid=stop]").count()
                       and not page.locator("#runsel").count() and page.locator("nav a").count() == 1)
            self.screenshot(page, "equipment_standalone_empty")
            with UnknownMCP() as fixture:
                self.mcp_discovery(page, context, base, fixture)
                page.close()
                self.scene_matrix(browser, base, context=context, mode="catalog")
                self.check("standalone_no_actuator_rpc", not fixture.actuator_calls, fixture.actuator_calls)
        finally:
            context.close()
            standalone.shutdown()
            standalone.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--bundle", default=str(SAMPLE))
    parser.add_argument("--hw-mcp", help="Optional native hw-mcp binary; uses a copied repository fixture, never /sys")
    parser.add_argument("--no-shots", action="store_true")
    parser.add_argument("--replay-only", action="store_true")
    args = parser.parse_args(argv)
    from playwright.sync_api import sync_playwright
    qa = EquipmentAcceptance(args.out, shots=not args.no_shots)
    started = time.monotonic()
    replay = None
    try:
        with tempfile.TemporaryDirectory(prefix="equipment-e2e-") as temporary, sync_playwright() as pw:
            bundle = instrument_bundle(stream.read_bundle(args.bundle))
            path = Path(temporary) / "equipment-recording.json.gz"
            stream.write_bundle(str(path), bundle)
            replay = server.make_server(str(path), 0)
            threading.Thread(target=replay.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{replay.server_address[1]}"
            browser = pw.chromium.launch()
            qa.attempt("equipment_replay_matrix", lambda: qa.scene_matrix(browser, base))
            qa.attempt("equipment_replay_fields", lambda: qa.replay_fields(browser, base, bundle))
            if not args.replay_only:
                qa.attempt("equipment_live", lambda: qa.live_equipment(browser, path, temporary, args.hw_mcp))
                qa.attempt("equipment_standalone_catalog", lambda: qa.standalone_catalog(browser))
            browser.close()
    except Exception as exc:
        qa.check("equipment_session_completed", False, str(exc))
    finally:
        if replay:
            replay.shutdown()
            replay.server_close()
        qa.phase = "browser_events"
        # A component unmount cancels a pending catalog fetch intentionally.
        events = [event for event in qa.report["browser_events"] if not (event["kind"] == "requestfailed" and
                  "/api/catalog" in event["detail"] and "ERR_ABORTED" in event["detail"])]
        qa.report["browser_events"] = events
        qa.check("equipment_browser_clean", not events, events)
        qa.report["seconds"] = round(time.monotonic() - started, 2)
        qa.report["ok"] = not qa.report["errors"]
        report = qa.out / "e2e_equipment.json"
        report.write_text(json.dumps(qa.report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"equipment e2e: {len(qa.report['pages'])} pages, {len(qa.report['checks'])} checks, "
              f"{len(qa.report['errors'])} errors, {qa.report['seconds']} s; {report}")
        for error in qa.report["errors"]:
            print("  " + str(error))
    return 0 if qa.report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())


# Additional browser assertions use the equipment screen's stable acceptance identifiers.
