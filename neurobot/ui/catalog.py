"""Read-only MCP equipment discovery for the operator UI. Python 3.8, stdlib only.

An operator adds an explicit loopback Streamable HTTP endpoint. Each source has a
bounded background poller; HTTP GETs read a snapshot and never wait on equipment.
No network scan, tool execution UI, actuator call or implicit write is provided.
Only the known discovery tool ``list_devices`` may be called as a fallback.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import math
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

MAX_SOURCES = 8
MAX_BODY = 4096
MAX_RESPONSE = 2 * 1024 * 1024
MAX_CYCLE_BYTES = 8 * 1024 * 1024
MAX_ITEMS = 512
MAX_PROPERTIES = 8192
MAX_DEVICE_LEAVES = 8192
MAX_SOURCE_LEAVES = 32768
MAX_SNAPSHOT_BYTES = MAX_CYCLE_BYTES
MAX_PAGES = 8
POLL_S = 1.0
DISCOVER_S = 15.0
TIMEOUT_S = 2.0
CYCLE_S = 10.0
PROTOCOL = "2025-11-25"  # same negotiated version as this project's hw-mcp
VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", PROTOCOL, "2026-07-28"}


class CatalogError(ValueError):
    """An explicit, operator-readable discovery failure."""


def endpoint(value: str) -> str:
    """Canonical local URL. localhost is pinned to 127.0.0.1 by Client, without DNS."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise CatalogError("Укажите HTTP-адрес локального MCP-сервера")
    try:
        if len(value.encode("utf-8")) > 2048:
            raise CatalogError("Адрес MCP длиннее 2048 байт")
    except UnicodeError:
        raise CatalogError("Адрес содержит некорректные символы Unicode")
    if any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise CatalogError("В адресе недопустимы пробелы, управляющие символы и обратная косая черта")
    try:
        u = urlsplit(value)
        port = 80 if u.port is None else u.port
        if (u.scheme != "http" or u.hostname not in ("127.0.0.1", "::1", "localhost")
                or u.username is not None or u.password is not None or u.fragment or not 1 <= port <= 65535):
            raise ValueError()
    except ValueError:
        raise CatalogError("Разрешён только http://127.0.0.1, http://localhost или http://[::1], "
                           "без логина и фрагмента")
    host = "[::1]" if u.hostname == "::1" else u.hostname
    authority = host + ((":" + str(port)) if port != 80 else "")
    return urlunsplit(("http", authority, u.path or "/mcp", u.query, ""))


def _json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=lambda _: None)
    except (ValueError, UnicodeError, RecursionError):
        raise CatalogError("MCP-сервер вернул некорректный JSON")


def _safe(value):
    """JSON-safe values, including non-finite vendor readings, without invented numbers."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe(v) for v in value]
    return value


class Client:
    """Small, explicitly read-only MCP Streamable HTTP client, including SSE replies."""

    def __init__(self, url: str, timeout: float = TIMEOUT_S):
        self.url = endpoint(url)
        self.timeout = timeout
        self.session = None
        self.version = PROTOCOL
        self.info: dict = {}
        self.tools: List[dict] = []
        self.resources: List[dict] = []
        self.descriptors: List[tuple] = []
        self.reads: List[str] = []
        self.discovery_at = 0.0
        self.fallback = False
        self.seq = 0
        self.deadline = 0.0
        self.cycle_bytes = 0

    def reset(self) -> None:
        self.session = None
        self.info = {}
        self.discovery_at = 0.0

    def rpc(self, method: str, params: Optional[dict] = None, notify: bool = False):
        # Keep this whitelist independent of what an untrusted server advertises.
        if method not in {"initialize", "notifications/initialized", "tools/list", "resources/list", "resources/read",
                          "tools/call"} or (method == "tools/call" and (params or {}).get("name") != "list_devices"):
            raise CatalogError("Пульт не вызывает команды оборудования")
        left = self.deadline - time.monotonic() if self.deadline else self.timeout
        if left <= 0 or self.cycle_bytes >= MAX_CYCLE_BYTES:
            raise CatalogError("Лимит времени или объёма обновления каталога исчерпан")
        u = urlsplit(self.url)
        # Do not consult DNS, proxies or redirects, including for localhost.
        host = "::1" if u.hostname == "::1" else "127.0.0.1"
        conn = http.client.HTTPConnection(host, u.port or 80, timeout=min(self.timeout, left))
        self.seq += 1
        doc = {"jsonrpc": "2.0", "method": method}
        if not notify:
            doc["id"] = self.seq
        if params is not None:
            doc["params"] = params
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Host": u.netloc, "MCP-Protocol-Version": self.version, "Connection": "close"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        response = None
        try:
            conn.request("POST", u.path + (("?" + u.query) if u.query else ""),
                         json.dumps(doc, separators=(",", ":")).encode("utf-8"), headers)
            response = conn.getresponse()
            if 300 <= response.status < 400:
                raise CatalogError("MCP-сервер перенаправляет запрос: переходы запрещены")
            if not 200 <= response.status < 300:
                raise CatalogError("MCP %s: HTTP %d" % (method, response.status))
            if method == "initialize":
                sid = response.getheader("Mcp-Session-Id")
                if sid and (len(sid) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in sid)):
                    raise CatalogError("MCP-сервер выдал недопустимый идентификатор сессии")
                self.session = sid
            if notify:
                return None
            kind = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if kind not in ("application/json", "text/event-stream"):
                raise CatalogError("MCP-сервер вернул неподдерживаемый Content-Type: %s" % (kind or "не указан"))
            size = 0
            pending = b""
            data = []
            call_deadline = min(self.deadline or float("inf"), time.monotonic() + self.timeout)
            while True:
                if time.monotonic() > call_deadline:
                    raise CatalogError("MCP %s: истекло время ответа" % method)
                chunk = response.read1(min(65536, MAX_RESPONSE + 1 - size))
                size += len(chunk)
                self.cycle_bytes += len(chunk)
                if size > MAX_RESPONSE or self.cycle_bytes > MAX_CYCLE_BYTES:
                    raise CatalogError("Ответ MCP превышает допустимый объём")
                pending += chunk
                if kind == "text/event-stream":
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        line = line.rstrip(b"\r")
                        if line.startswith(b"data:"):
                            data.append(line[5:].lstrip(b" "))
                        elif not line and data:
                            message = _json(b"\n".join(data))
                            data = []
                            if isinstance(message, dict) and message.get("id") == doc["id"]:
                                return self._result(message, doc["id"], method)
                if not chunk:
                    if kind == "application/json":
                        return self._result(_json(pending), doc["id"], method)
                    raise CatalogError("Поток MCP завершён без ответа на %s" % method)
        except (OSError, http.client.HTTPException) as e:
            raise CatalogError("MCP %s: %s" % (method, str(e) or type(e).__name__))
        finally:
            if response is not None:
                response.close()
            conn.close()

    @staticmethod
    def _result(message, request_id, method):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or message.get("id") != request_id:
            raise CatalogError("Некорректный ответ MCP на %s" % method)
        if "error" in message:
            err = message["error"]
            text = err.get("message", "ошибка протокола") if isinstance(err, dict) else str(err)
            raise CatalogError("MCP %s: %s" % (method, str(text)[:300]))
        result = message.get("result")
        if not isinstance(result, dict):
            raise CatalogError("В ответе MCP %s нет объекта result" % method)
        return result

    def listing(self, method: str, key: str) -> List[dict]:
        result = []
        cursor = None
        seen = set()
        for _ in range(MAX_PAGES):
            reply = self.rpc(method, {"cursor": cursor} if cursor else {})
            rows = reply.get(key, [])
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise CatalogError("MCP %s: поле %s должно быть списком объектов" % (method, key))
            result.extend(rows)
            if len(result) > MAX_ITEMS:
                raise CatalogError("В каталоге больше %d записей; данные не обрезаны молча" % MAX_ITEMS)
            cursor = reply.get("nextCursor")
            if not cursor:
                return result
            if not isinstance(cursor, str) or len(cursor) > 2048 or cursor in seen:
                raise CatalogError("MCP-сервер повторяет или искажает курсор каталога")
            seen.add(cursor)
        raise CatalogError("Каталог MCP превышает лимит страниц")

    def contents(self, uri: str) -> list:
        result = self.rpc("resources/read", {"uri": uri})
        return payloads(result, "contents")

    def collect(self, source_id: str) -> tuple:
        self.deadline = time.monotonic() + CYCLE_S
        self.cycle_bytes = 0
        warnings = []
        discovered = False
        if not self.info:
            reply = self.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                            "clientInfo": {"name": "neurochip-catalog", "version": "1.0"}})
            self.version = reply.get("protocolVersion", "")
            if self.version not in VERSIONS:
                raise CatalogError("MCP-сервер выбрал неподдерживаемую версию протокола")
            if not isinstance(reply.get("serverInfo", {}), dict) or not isinstance(reply.get("capabilities", {}), dict):
                raise CatalogError("MCP initialize: serverInfo и capabilities должны быть объектами")
            self.info = reply
            self.rpc("notifications/initialized", notify=True)
        if time.monotonic() - self.discovery_at >= DISCOVER_S or not self.discovery_at:
            self.descriptors, self.reads = [], []
            self.fallback = False
            discovered = True
            try:
                self.tools = self.listing("tools/list", "tools")
            except CatalogError as e:
                self.tools = []
                warnings.append(str(e))
            try:
                self.resources = self.listing("resources/list", "resources")
            except CatalogError as e:
                self.resources = []
                warnings.append(str(e))
            for r in self.resources:
                uri = r.get("uri")
                if not isinstance(uri, str) or len(uri) > 2048:
                    continue
                if uri in {"hw://inventory/text", "hw://probe", "hw://td", "hw://diag"}:
                    continue
                if "json" not in str(r.get("mimeType", "")).lower() and not uri.startswith("hw://"):
                    continue
                if any(word in uri.lower() for word in ("inventory", "catalog", "devices")):
                    try:
                        self.descriptors.extend((uri, x) for x in self.contents(uri))
                    except CatalogError as e:
                        warnings.append(str(e))
                else:
                    self.reads.append(uri)
            if not self.descriptors:
                tool = next((t for t in self.tools if t.get("name") == "list_devices"), None)
                if tool and (tool.get("annotations") or {}).get("readOnlyHint") is not False:
                    schema = tool.get("inputSchema") or {}
                    args = {"all": True} if "all" in (schema.get("properties") or {}) else {}
                    self.fallback = True
                    try:
                        reply = self.rpc("tools/call", {"name": "list_devices", "arguments": args})
                        self.descriptors.extend(("list_devices", x) for x in payloads(reply, "content"))
                    except CatalogError as e:
                        warnings.append(str(e))
            self.discovery_at = time.monotonic()
        if self.fallback and not discovered:
            tool = next((t for t in self.tools if t.get("name") == "list_devices"), {})
            schema = tool.get("inputSchema") or {}
            args = {"all": True} if "all" in (schema.get("properties") or {}) else {}
            reply = self.rpc("tools/call", {"name": "list_devices", "arguments": args})
            self.descriptors = [("list_devices", x) for x in payloads(reply, "content")]
        documents = list(self.descriptors)
        successful_reads = 0
        for uri in self.reads:
            try:
                documents.extend((uri, x) for x in self.contents(uri))
                successful_reads += 1
            except CatalogError as e:
                warnings.append("%s: %s" % (uri, e))
        if self.reads and not successful_reads:
            raise CatalogError("Не удалось прочесть состояние: " + "; ".join(warnings))
        devices = normalize(source_id, documents)
        if not devices and not self.descriptors and not self.reads:
            raise CatalogError("Не найден читаемый каталог устройств. " + "; ".join(warnings))
        document_metadata = []
        for uri, doc in documents:
            if isinstance(doc, dict) and any(k in doc for k in ("devices", "inventory", "things")):
                extra = {k: v for k, v in doc.items() if k not in ("devices", "inventory", "things")}
                if extra:
                    document_metadata.append({"uri": uri, "metadata": extra})
        metadata = {"server_info": self.info.get("serverInfo", {}), "capabilities": self.info.get("capabilities", {}),
                    "version": self.version, "tools": self.tools, "resources": self.resources,
                    "metadata": {"documents": document_metadata}}
        return devices, _safe(metadata), warnings


def payloads(result: dict, key: str) -> list:
    if result.get("isError"):
        raise CatalogError("MCP-сервер отказал в чтении каталога устройств")
    if isinstance(result.get("structuredContent"), (dict, list)):
        return [result["structuredContent"]]
    items = result.get(key)
    if not isinstance(items, list):
        raise CatalogError("В ответе MCP отсутствует %s" % key)
    out = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            value = _json(item["text"].encode("utf-8"))
            out.append(value)
    if not out:
        raise CatalogError("Ресурс MCP не содержит JSON с описанием или значениями")
    return out


def _rows(value) -> list:
    if isinstance(value, list):
        return [(str(i), x) for i, x in enumerate(value)]
    return list(value.items()) if isinstance(value, dict) else []


PROPERTY_GROUPS = ("props", "properties", "signals", "channels", "measurements", "values")
DESCRIPTOR_KEYS = {
    "id", "device", "name", "title", "label", "kind", "type", "source", "source_id", "location", "loc",
    "description", "observability", "actions", "notes", "@context", "$schema", "forms", "security",
    "securityDefinitions", "base", "links", "readable", "writable", "readOnly", "writeOnly", "unit", "units",
    "ts", "t", "timestamp", "now", "updated_at", "status", "error", "protocolVersion", "capabilities",
}


def _properties(doc: dict) -> Dict[str, dict]:
    out = {}
    has_groups = any(isinstance(doc.get(group), (dict, list)) for group in PROPERTY_GROUPS)
    if not has_groups:
        # Flat JSON resources need no pre-agreed sensor schema. Literal field
        # names and nested values stay intact; the frontend may expand vectors.
        for name, value in doc.items():
            if name not in DESCRIPTOR_KEYS:
                prop = {"value": value}
                if name == "value":
                    prop.update({k: doc[k] for k in ("unit", "units", "type", "ts", "timestamp", "status", "error")
                                 if k in doc})
                out[name] = prop
    for group in PROPERTY_GROUPS:
        for key, row in _rows(doc.get(group)):
            if isinstance(row, dict):
                name = str(row.get("name") or row.get("id") or row.get("key") or key)
                value = dict(row)
            elif isinstance(row, str) and group == "props":
                sig = row.split(",")
                name = str(key)
                value = {"unit": "" if sig[0] == "bool" else sig[0],
                         "type": "boolean" if sig[0] == "bool" else "number",
                         "writable": "rw" in sig, "raw_signature": row}
            else:
                name, value = str(key), {"value": row}
            out[name] = dict(out.get(name, {}), **value)
    return out


def _devices(doc, uri: str) -> list:
    fallback = uri or "device"
    if isinstance(doc, list):
        if doc and all(isinstance(x, dict) and ("id" in x or "device" in x
                       or any(group in x for group in PROPERTY_GROUPS)) for x in doc):
            return [(str(i), x) for i, x in enumerate(doc)]
        return [(fallback, {"value": doc})]
    if not isinstance(doc, dict):
        return [(fallback, {"value": doc})]
    for key in ("devices", "inventory", "things"):
        if isinstance(doc.get(key), (dict, list)):
            return [(str(k), v) for k, v in _rows(doc[key]) if isinstance(v, dict)]
    return [(str(doc.get("device") or doc.get("id") or fallback), doc)]


def normalize(source_id: str, documents: list) -> List[dict]:
    """Maps device/property dictionaries or arrays to one complete, lossless catalog.

    Schema metadata, unknown signals, arbitrary values and action descriptors remain
    in raw; writable describes the source capability, never permission in this UI.
    """
    merged = {}
    property_count = 0
    for uri, doc in documents:
        for fallback, row in _devices(doc, uri):
            local_id = str(row.get("device") or row.get("id") or fallback)
            is_state = "state" in uri.lower() or "device" in row and "now" in row
            if local_id not in merged and len(merged) >= MAX_ITEMS:
                raise CatalogError("Каталог превышает лимит %d устройств на источник" % MAX_ITEMS)
            entry = merged.setdefault(local_id, {"descriptor": {}, "state": {}, "properties": {}, "resources": []})
            entry["state" if is_state else "descriptor"].update(row)
            entry["resources"].append(uri)
            for name, prop in _properties(row).items():
                if name not in entry["properties"]:
                    property_count += 1
                    if property_count > MAX_PROPERTIES:
                        raise CatalogError("Каталог превышает лимит %d свойств на источник" % MAX_PROPERTIES)
                entry["properties"][name] = dict(entry["properties"].get(name, {}), **prop)
    result = []
    source_leaves = 0
    for local_id, entry in merged.items():
        device_leaves = 0
        desc = entry["descriptor"] or entry["state"]
        properties = []
        for name, row in entry["properties"].items():
            raw_value = row.get("value", row.get("reading", row.get("current", row.get("data"))))
            remaining = min(MAX_DEVICE_LEAVES - device_leaves, MAX_SOURCE_LEAVES - source_leaves)
            leaves = value_leaves(raw_value, remaining)
            device_leaves += leaves
            source_leaves += leaves
            val = _safe(raw_value)
            typ = row.get("type")
            if not typ:
                typ = ("boolean" if isinstance(val, bool) else "number" if isinstance(val, (int, float)) else
                       "string" if isinstance(val, str) else "array" if isinstance(val, list) else
                       "object" if isinstance(val, dict) else "unknown")
            if not isinstance(typ, str):
                typ = " | ".join(str(t) for t in typ) if isinstance(typ, list) else "unknown"
            status = "error" if row.get("error") else row.get("status") or ("ok" if val is not None else "unknown")
            prop = {"name": name, "value": _safe(val), "unit": row.get("unit") or row.get("units") or "",
                    "type": typ, "readable": row.get("readable") is not False and row.get("writeOnly") is not True,
                    "writable": row.get("writable") is True, "status": str(status), "raw": _safe(row)}
            for key in ("ts", "timestamp", "t"):
                if row.get(key) is not None:
                    prop["ts"] = _safe(row[key])
                    break
            properties.append(prop)
        actions = []
        for key, action in _rows(desc.get("actions")):
            actions.append(dict(action, name=action.get("name") or key) if isinstance(action, dict)
                           else {"name": str(key), "description": action})
        result.append({"id": source_id + "/" + local_id, "local_id": local_id, "source": source_id,
                       "name": str(desc.get("title") or desc.get("label") or desc.get("name")
                                   or local_id.rsplit("/", 1)[-1]),
                       "kind": str(desc.get("kind") or desc.get("type") or desc.get("source") or "device"),
                       "location": desc.get("location") or desc.get("loc") or "", "properties": properties,
                       "actions": _safe(actions), "raw": _safe(entry)})
    return result


def value_leaves(value, limit: int) -> int:
    """Bound flattened UI work, including booleans, null and empty terminal containers."""
    pending, count = [value], 0
    while pending:
        item = pending.pop()
        children = list(item.values()) if isinstance(item, dict) else item if isinstance(item, list) else None
        if children:
            pending.extend(children)
        else:
            count += 1
            if count > limit:
                raise CatalogError("Значения превышают лимит %d элементов на устройство или %d на источник"
                                   % (MAX_DEVICE_LEAVES, MAX_SOURCE_LEAVES))
    return count


def within_snapshot_limit(doc: dict, limit: int) -> bool:
    """Incremental encoding avoids allocating an oversized response just to reject it."""
    size = 0
    try:
        for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(doc):
            size += len(chunk.encode("utf-8"))
            if size > limit:
                return False
    except (ValueError, TypeError, RecursionError):
        return False
    return True


class Registry:
    """At most eight independent sources. All network operations stay outside its lock."""

    def __init__(self, poll_s: float = POLL_S, client_factory=Client):
        self.poll_s = max(POLL_S, poll_s)
        self.factory = client_factory
        self.lock = threading.RLock()
        self.sources: Dict[str, dict] = {}
        self.updated_at = time.time()
        self.closed = False

    def snapshot(self) -> dict:
        with self.lock:
            records = list(self.sources.values())
            return copy.deepcopy({"sources": [r["public"] for r in records],
                                  "devices": [d for r in records for d in r["devices"]],
                                  "updated_at": self.updated_at})

    def connect(self, url: str, label: str = "") -> str:
        url = endpoint(url)
        if not isinstance(label, str) or len(label) > 120:
            raise CatalogError("Название источника должно быть строкой до 120 символов")
        try:
            label.encode("utf-8")
        except UnicodeError:
            raise CatalogError("Название источника содержит некорректные символы Unicode")
        sid = "mcp-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
        with self.lock:
            if self.closed:
                raise CatalogError("Каталог завершает работу")
            if sid in self.sources:
                return sid
            if len(self.sources) >= MAX_SOURCES:
                raise CatalogError("Одновременно можно подключить не больше %d источников" % MAX_SOURCES)
            record = {"public": {"id": sid, "label": label or url, "url": url, "status": "connecting",
                                  "error": None, "protocol": "MCP / Streamable HTTP"},
                      "devices": [], "stop": threading.Event(), "label": label}
            self.sources[sid] = record
            self.updated_at = time.time()
        thread = threading.Thread(target=self._worker, args=(sid, record), daemon=True, name="ui-catalog-" + sid)
        record["thread"] = thread
        thread.start()
        return sid

    def disconnect(self, sid: str) -> None:
        if not isinstance(sid, str):
            raise CatalogError("Укажите идентификатор источника")
        with self.lock:
            record = self.sources.pop(sid, None)
            if record is None:
                raise CatalogError("Источник не найден")
            record["stop"].set()
            self.updated_at = time.time()

    def close(self) -> None:
        with self.lock:
            self.closed = True
            for record in self.sources.values():
                record["stop"].set()

    def _worker(self, sid: str, record: dict) -> None:
        client = self.factory(record["public"]["url"])
        while not record["stop"].is_set():
            began = time.monotonic()
            try:
                devices, metadata, errors = client.collect(sid)
                update = {"status": "degraded" if errors else "ready", "error": "; ".join(errors) or None,
                          "last_success": time.time(), **metadata}
                name = metadata.get("server_info", {}).get("title") or metadata.get("server_info", {}).get("name")
                if name and not record["label"]:
                    update["label"] = str(name)
            except (CatalogError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as e:
                devices = None
                update = {"status": "error", "error": str(e)[:1000] or "Ошибка MCP-каталога"}
                client.reset()
            with self.lock:
                if self.sources.get(sid) is not record:
                    return
                if devices is not None:
                    proposed = dict(record["public"], **update)
                    snapshot = {"sources": [proposed if r is record else r["public"] for r in self.sources.values()],
                                "devices": [d for r in self.sources.values()
                                            for d in (devices if r is record else r["devices"])],
                                "updated_at": time.time()}
                    # Reserve worst-case growth of 8 x 8192 statuses to stale, errors and URL-only source records.
                    allowance = MAX_SNAPSHOT_BYTES - min(512 * 1024, MAX_SNAPSHOT_BYTES // 8)
                    if not within_snapshot_limit(snapshot, allowance):
                        devices = None
                        update = {"status": "error", "error": "Каталог превышает общий лимит объёма %d КиБ; "
                                  "прежние значения устарели" % (MAX_SNAPSHOT_BYTES // 1024)}
                record["public"].update(update, updated_at=time.time())
                if devices is not None:
                    record["devices"] = devices
                else:
                    for device in record["devices"]:
                        for prop in device["properties"]:
                            prop["status"] = "stale"
                self.updated_at = time.time()
            record["stop"].wait(max(self.poll_s, self.poll_s - (time.monotonic() - began)))


def mutate(registry: Registry, path: str, body: dict) -> tuple:
    """HTTP-neutral configuration routes shared by replay and authenticated live gateway."""
    try:
        if path == "/api/catalog/sources":
            sid = registry.connect(body.get("url"), body.get("label", ""))
            return 200, dict(registry.snapshot(), ok=True, source_id=sid)
        if path == "/api/catalog/disconnect":
            registry.disconnect(body.get("id"))
            return 200, dict(registry.snapshot(), ok=True)
    except CatalogError as e:
        return 400, {"ok": False, "error": str(e)}
    return 404, {"ok": False, "error": "Неизвестный адрес каталога"}
