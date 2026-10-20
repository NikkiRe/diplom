"""Local read-only MCP projection for the conversational EdgeCtx agent.

Only data already obtained by the catalog is published. No tool is forwarded to
an equipment server. Names and writable hints never grant actuator permission.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

MAX_BODY = 16384
MAX_REPLY = 4 * 1024 * 1024
MAX_INVENTORY = 128 * 1024
MAX_AUTOMATIC_VALUES = 32 * 1024
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def _bytes(doc):
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def board_summary(board):
    """Selected bounded facts; never a second copy of untrusted raw sysfs."""
    report = board.get("report") or {}
    def fields(value, names):
        value = value if isinstance(value, dict) else {}
        return {name: (value[name].encode("utf-8")[:96].decode("utf-8", "ignore") if isinstance(value.get(name), str) else value.get(name)
                       if isinstance(value.get(name), (int, float, bool)) else None) for name in names}
    system = report.get("system") or {}
    coverage = report.get("coverage") or []
    result = {"identity": fields(report.get("identity"), ("kind", "model", "manufacturer", "source", "vendor_id")),
        "system": {"architecture": fields(system, ("architecture",))["architecture"],
            "os": fields(system.get("os"), ("pretty_name", "kernel", "id", "version", "version_id")),
            "cpu": fields(system.get("cpu"), ("model", "hardware", "logical_cores")),
            "memory": fields(system.get("memory"), ("total_bytes", "available_bytes"))},
        "bus_counts": {k: len(report["buses"][k]) if isinstance((report.get("buses") or {}).get(k), list) else None
                       for k in ("usb", "i2c", "spi", "serial", "gpio", "iio", "hwmon", "camera")},
        "storage_count": len(report.get("storage") or []), "interface_count": len(report.get("interfaces") or []),
        "coverage": [fields(row, ("id", "status", "count")) for row in coverage[:12] if isinstance(row, dict)],
        "collected_at": fields(report, ("collected_at",))["collected_at"], "snapshot": True,
        "projection": "selected_fields", "summary_limited": True, "text_limit_bytes": 96,
        "full_report_available": True, "sha256": board.get("sha256")}
    while result["coverage"] and len(_bytes(result)) > 1800:
        result["coverage"].pop()
    result["coverage_omitted"] = len(coverage) - len(result["coverage"])
    if len(_bytes(result)) > 2048:
        def shorten(value):
            if isinstance(value, str):
                return value.encode("utf-8")[:48].decode("utf-8", "ignore")
            if isinstance(value, dict):
                return {key: (item if key == "sha256" else shorten(item)) for key, item in value.items()}
            if isinstance(value, list):
                return [shorten(item) for item in value]
            return value
        result = shorten(result)
        result["text_limit_bytes"] = 48
    return result


class ReadProxy:
    def __init__(self, catalog, boards=None):
        self.catalog, self.boards = catalog, boards
        self.server = None
        self.url = None

    def devices(self):
        snapshot = self.catalog.snapshot()
        sources = {s["id"]: s for s in snapshot.get("sources", [])}
        devices = []
        for device in snapshot.get("devices", []):
            source = sources.get(device.get("source"), {})
            if source.get("server_info", {}).get("name") == "edgelab-observations":
                continue  # avoid a catalog/projection feedback loop
            properties = {}
            for prop in device.get("properties", []):
                if prop.get("readable") is False:
                    continue
                status = prop.get("status", "unknown")
                if source.get("status") not in ("ready", "degraded"):
                    status = "stale"
                value = prop.get("value") if status in ("ok", "live", "recorded") else None
                properties[prop["name"]] = {"value": value, "ts": prop.get("ts"), "unit": prop.get("unit"),
                    "type": prop.get("type", "unknown"), "writable": False, "status": status,
                    "error": None if status in ("ok", "live", "recorded") else status}
            devices.append({"id": device["id"], "name": device.get("name", device["id"]),
                "location": device.get("location", ""), "source": source.get("label", "MCP catalog"),
                "properties": properties, "actions": {}, "description": "Read-only observations from EdgeLab catalog"})
        if self.boards is not None:
            for board in self.boards.snapshot().get("boards", [])[:16]:
                report = board.get("report") or {}
                if not report:
                    continue
                devices.append({"id": "board:" + board["id"], "name": board.get("label", "Board passport"),
                    "properties": {"passport": {"value": report, "type": "object", "writable": False,
                        "ts": report.get("collected_at"), "status": "snapshot"},
                        "summary": {"value": board_summary(board),
                            "type": "object", "writable": False, "ts": report.get("collected_at"), "status": "snapshot"}}, "actions": {},
                    "description": "Collected board passport, not live telemetry; origin=" + board.get("origin", "unknown")})
        return devices

    def inventory(self, devices):
        """Bound automatic prompt input; exact observations remain on demand."""
        output, used, values_used, omitted_values = [], 2048, 0, 0
        for device in sorted(devices, key=lambda item: not item["id"].startswith("board:")):
            view = {k: v for k, v in device.items() if k != "properties"}
            view["properties"] = {}
            observation_meta = {}
            for name, prop in device["properties"].items():
                value_bytes = len(_bytes(prop.get("value")))
                selected = dict(prop)
                if value_bytes > 2048 or values_used + value_bytes > MAX_AUTOMATIC_VALUES:
                    selected.update(value=None, status="omitted_from_automatic_context", error="Read this property explicitly for its original value",
                                    original_value_bytes=value_bytes)
                    omitted_values += 1
                else:
                    values_used += value_bytes
                view["properties"][name] = selected
                observation_meta[name] = {"status": selected.get("status"), "source_ts": prop.get("ts")}
            view["description"] = (view.get("description", "") +
                ". Source observation metadata (original source clocks; EdgeCtx cache age is not measurement time): " +
                _bytes(observation_meta).decode("utf-8"))
            size = len(_bytes(view))
            if used + size > MAX_INVENTORY:
                continue
            used += size
            output.append(view)
        omitted_devices = len(devices) - len(output)
        if omitted_devices or omitted_values:
            output.append({"id": "workspace:coverage", "name": "Automatic context coverage", "properties": {}, "actions": {},
                "description": "%d devices and %d property values omitted from automatic context by byte limits. Use list_devices with offset/limit and read_property for original observations; omitted is not a measured null." % (omitted_devices, omitted_values)})
        return {"devices": output, "read_only": True, "origin": "EdgeLab received observations",
                "automatic_context": {"byte_limit": MAX_INVENTORY, "value_byte_limit": MAX_AUTOMATIC_VALUES,
                                      "omitted_devices": omitted_devices, "omitted_values": omitted_values}}

    def dispatch(self, method, params):
        if method == "initialize":
            requested = params.get("protocolVersion")
            return {"protocolVersion": requested if requested in VERSIONS else VERSIONS[0],
                "serverInfo": {"name": "edgelab-observations", "version": "1", "title": "Workspace observations (read only)"},
                "capabilities": {"resources": {}, "tools": {}},
                "instructions": "Observations only. Never infer current values from missing/stale properties. Board passports are snapshots."}
        if method in ("ping", "notifications/initialized", "notifications/cancelled"):
            return {}
        if method == "tools/list":
            return {"tools": [
                {"name": "list_devices", "description": "Page observed device/property metadata; use read_property for original values. No actuator commands.",
                 "inputSchema": {"type": "object", "properties": {"offset": {"type": "integer", "minimum": 0},
                     "limit": {"type": "integer", "minimum": 1, "maximum": 50}}}, "annotations": {"readOnlyHint": True}},
                {"name": "read_property", "description": "Read a published observation. Status/time are authoritative; a passport is not live telemetry.",
                 "inputSchema": {"type": "object", "properties": {"device": {"type": "string"}, "property": {"type": "string"}},
                                 "required": ["device", "property"]}, "annotations": {"readOnlyHint": True}}]}
        devices = self.devices()
        if method == "resources/list":
            # Exposing full per-device state resources makes an MCP host eagerly
            # reread all large values and defeats the automatic context budget.
            return {"resources": [{"uri": "hw://inventory", "name": "Bounded observed inventory", "mimeType": "application/json"}]}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "resources/read":
            uri = params.get("uri", "")
            if uri == "hw://inventory":
                doc = self.inventory(devices)
            elif isinstance(uri, str) and uri.startswith("hw://state/"):
                device_id = unquote(uri[len("hw://state/"):])
                device = next((d for d in self.inventory(devices)["devices"] if d["id"] == device_id), None)
                if device is None:
                    raise ValueError("Device absent from automatic context; use list_devices/read_property")
                doc = {"device": device["id"], "properties": device["properties"], "description": device.get("description")}
            else:
                raise ValueError("Unknown read-only resource")
            return {"contents": [{"uri": uri, "mimeType": "application/json", "text": _bytes(doc).decode("utf-8")}]}
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
            if name == "list_devices":
                offset, limit = args.get("offset", 0), args.get("limit", 20)
                if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
                    raise ValueError("offset >= 0, limit 1..50")
                page = [{**{k: v for k, v in device.items() if k != "properties"},
                         "properties": {name: {k: v for k, v in prop.items() if k != "value"} for name, prop in device["properties"].items()}}
                        for device in devices[offset:offset + limit]]
                doc = {"devices": page, "total": len(devices), "offset": offset,
                       "next_offset": offset + limit if offset + limit < len(devices) else None, "read_only": True,
                       "values": "Use read_property for the original published observation"}
            elif name == "read_property":
                device = next((d for d in devices if d["id"] == args.get("device")), None)
                prop = device and device["properties"].get(args.get("property"))
                if prop is None:
                    return {"isError": True, "content": [{"type": "text", "text": '{"error":"Unknown observed property"}'}]}
                doc = {"device": device["id"], "property": args.get("property"), **prop, "observed": True}
            else:
                raise ValueError("Only list_devices and read_property are available; equipment actions are never forwarded")
            return {"content": [{"type": "text", "text": _bytes(doc).decode("utf-8")}], "isError": bool(doc.get("error"))}
        raise ValueError("Unsupported read-only MCP method")

    def start(self):
        if self.server is not None:
            return self.url
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def send(self, code, doc=None):
                raw = _bytes(doc) if doc is not None else b""
                if len(raw) > MAX_REPLY:
                    code, raw = 413, b'{"error":"Observation projection exceeds 4 MiB"}'
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def allowed(self):
                name = urlsplit("http://" + (self.headers.get("Host") or "")).hostname
                return name in ("127.0.0.1", "localhost") and not self.headers.get("Origin")

            def do_POST(self):  # noqa: N802
                if not self.allowed() or urlsplit(self.path).path != "/mcp":
                    return self.send(403, {"error": "Local agent transport only"})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if not 0 < length <= MAX_BODY:
                        self.close_connection = True
                        return self.send(413, {"error": "MCP request is too large"})
                    doc = json.loads(self.rfile.read(length))
                    if not isinstance(doc, dict) or doc.get("jsonrpc") != "2.0" or not isinstance(doc.get("method"), str):
                        raise ValueError("JSON-RPC object required")
                    params = doc.get("params") or {}
                    if not isinstance(params, dict):
                        raise ValueError("params must be an object")
                    result = owner.dispatch(doc["method"], params)
                    if "id" not in doc:
                        return self.send(202)
                    return self.send(200, {"jsonrpc": "2.0", "id": doc["id"], "result": result})
                except (ValueError, TypeError, UnicodeError) as exc:
                    return self.send(200, {"jsonrpc": "2.0", "id": doc.get("id") if isinstance(locals().get("doc"), dict) else None,
                        "error": {"code": -32602, "message": str(exc)[:300]}})

            def do_GET(self):  # noqa: N802
                self.send(405)

            do_DELETE = do_GET
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:%d/mcp" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, name="edgelab-read-proxy", daemon=True)
        self.thread.start()
        return self.url

    def close(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
            self.server = None
