"""Explicit read-only adapter for this repository's cam-mcp snapshot contract.

An already connected loopback source is reviewed again in a separate session.
Server name alone or a readOnlyHint alone never enables generic tool execution.
The catalog client's independent list_devices-only allowlist stays unchanged.
"""
from __future__ import annotations

import base64
import binascii
import http.client
import json
import time
from urllib.parse import urlsplit

from . import catalog
from .lab import LabError, _validate_tree

TIMEOUT_S = 3.0
MAX_IMAGE_BYTES = 512 * 1024
MAX_REPLY_BYTES = 768 * 1024
MAX_METADATA_BYTES = 32 * 1024


def _dimensions(data):
    """Read JPEG SOF dimensions without a decoder or allocation of image pixels."""
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise LabError("cam-mcp вернул данные без заголовка JPEG", 502)
    index = 2
    while index < len(data):
        if data[index] != 255:
            raise LabError("Повреждённая структура JPEG", 502)
        while index < len(data) and data[index] == 255:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if marker in (0xD9, 0xDA) or index + 2 > len(data):
            break
        length = int.from_bytes(data[index:index + 2], "big")
        if length < 2 or index + length > len(data):
            break
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if length < 8:
                break
            height = int.from_bytes(data[index + 3:index + 5], "big")
            width = int.from_bytes(data[index + 5:index + 7], "big")
            if not 1 <= width <= 320 or not 1 <= height <= 1280:
                raise LabError("JPEG превысил предел 320 × 1280 px", 502)
            return width, height
        index += length
    raise LabError("В JPEG отсутствует допустимый размер кадра", 502)


def _close(client):
    """Release only our own newly initialized session. Never touch catalog's poller."""
    if not client.session:
        return
    parsed = urlsplit(client.url)
    connection = http.client.HTTPConnection("::1" if parsed.hostname == "::1" else "127.0.0.1",
                                             parsed.port or 80, timeout=0.2)
    try:
        connection.request("DELETE", parsed.path + (("?" + parsed.query) if parsed.query else ""),
                           headers={"Host": parsed.netloc, "Mcp-Session-Id": client.session,
                                    "MCP-Protocol-Version": client.version, "Connection": "close"})
        response = connection.getresponse()
        response.close()
    except (OSError, http.client.HTTPException):
        pass
    finally:
        connection.close()


def _capture(client, width, quality):
    """The sole permitted custom RPC. No caller-supplied method, name or arguments."""
    remaining = client.deadline - time.monotonic()
    if remaining <= 0:
        raise LabError("Истекло время получения кадра", 504)
    parsed = urlsplit(client.url)
    connection = http.client.HTTPConnection("::1" if parsed.hostname == "::1" else "127.0.0.1",
                                             parsed.port or 80, timeout=min(TIMEOUT_S, remaining))
    client.seq += 1
    request_id = client.seq
    request = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
               "params": {"name": "snapshot", "arguments": {"max_width": width, "quality": quality}}}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "Host": parsed.netloc, "MCP-Protocol-Version": client.version, "Connection": "close"}
    if client.session:
        headers["Mcp-Session-Id"] = client.session
    response = None
    try:
        connection.request("POST", parsed.path + (("?" + parsed.query) if parsed.query else ""),
                           json.dumps(request, separators=(",", ":")).encode(), headers)
        transport_socket = connection.sock
        if transport_socket is not None:
            transport_socket.settimeout(max(0.01, client.deadline - time.monotonic()))
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raise LabError("cam-mcp snapshot: HTTP %d; перенаправления запрещены" % response.status, 502)
        kind = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
        if kind not in ("application/json", "text/event-stream"):
            raise LabError("cam-mcp snapshot вернул неподдерживаемый Content-Type", 502)
        size, pending, data = 0, b"", []
        while True:
            if time.monotonic() >= client.deadline:
                raise LabError("Истекло время получения кадра", 504)
            if transport_socket is not None:
                transport_socket.settimeout(max(0.01, client.deadline - time.monotonic()))
            chunk = response.read1(min(65536, MAX_REPLY_BYTES + 1 - size))
            size += len(chunk)
            if size > MAX_REPLY_BYTES:
                raise LabError("Ответ snapshot превысил 768 KiB", 413)
            pending += chunk
            if kind == "text/event-stream":
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    line = line.rstrip(b"\r")
                    if line.startswith(b"data:"):
                        data.append(line[5:].lstrip(b" "))
                    elif not line and data:
                        message = catalog._json(b"\n".join(data))
                        data = []
                        if isinstance(message, dict) and message.get("id") == request_id:
                            return client._result(message, request_id, "snapshot")
            if not chunk:
                if kind == "application/json":
                    return client._result(catalog._json(pending), request_id, "snapshot")
                raise LabError("Поток snapshot завершён без ответа", 502)
    except (OSError, http.client.HTTPException) as exc:
        raise LabError("Не удалось получить кадр cam-mcp: " + (str(exc) or type(exc).__name__), 502)
    finally:
        if response is not None:
            response.close()
        connection.close()


def _review(client):
    reply = client.rpc("initialize", {"protocolVersion": catalog.PROTOCOL, "capabilities": {},
                                       "clientInfo": {"name": "edgelab-camera-readonly", "version": "1.0"}})
    if not isinstance(reply.get("serverInfo"), dict) or reply["serverInfo"].get("name") != "cam-mcp":
        raise LabError("Кадры доступны только через проверенный адаптер проекта cam-mcp", 403)
    version = reply.get("protocolVersion")
    if not isinstance(version, str) or version not in catalog.VERSIONS:
        raise LabError("cam-mcp выбрал неподдерживаемую версию MCP", 502)
    client.version = version
    client.rpc("notifications/initialized", notify=True)
    resources = client.listing("resources/list", "resources")
    if not any(resource.get("uri") == "cam://inventory" for resource in resources):
        raise LabError("Источник не соответствует cam-mcp: нет cam://inventory", 403)
    inventories = client.contents("cam://inventory")
    if len(inventories) != 1 or not isinstance(inventories[0], dict):
        raise LabError("Некорректный инвентарь cam-mcp", 502)
    inventory = inventories[0]
    _validate_tree(inventory)
    if len(json.dumps(inventory, ensure_ascii=False).encode()) > MAX_METADATA_BYTES:
        raise LabError("Инвентарь камеры превысил 32 KiB", 413)
    devices, camera = inventory.get("devices"), inventory.get("camera")
    if (not isinstance(devices, list) or not devices or len(devices) > 8 or
            any(not isinstance(device, dict) or device.get("kind") != "camera" for device in devices) or
            not isinstance(camera, dict) or camera.get("source") not in ("synthetic", "dir", "v4l2")):
        raise LabError("Источник не соответствует модели камеры проекта", 403)
    tools = [tool for tool in client.listing("tools/list", "tools") if tool.get("name") == "snapshot"]
    if len(tools) != 1:
        raise LabError("В cam-mcp отсутствует однозначный инструмент snapshot", 403)
    tool = tools[0]
    annotations, schema = tool.get("annotations"), tool.get("inputSchema")
    if (not isinstance(annotations, dict) or annotations.get("readOnlyHint") is not True or
            annotations.get("destructiveHint") is True or not isinstance(schema, dict) or
            schema.get("type") != "object" or schema.get("additionalProperties") is not False):
        raise LabError("snapshot не соответствует проверенной схеме read-only адаптера", 403)
    props = schema.get("properties")
    if not isinstance(props, dict) or set(props) != {"max_width", "quality"}:
        raise LabError("Недопустимые аргументы snapshot", 403)
    chosen = []
    for name, minimum, cap, upper in (("max_width", 64, 320, 1280), ("quality", 20, 60, 95)):
        item = props[name]
        maximum = item.get("maximum") if isinstance(item, dict) else None
        if (not isinstance(item, dict) or item.get("type") != "integer" or item.get("minimum") != minimum or
                not isinstance(maximum, (int, float)) or isinstance(maximum, bool) or not minimum <= maximum <= upper):
            raise LabError("Границы snapshot не соответствуют cam-mcp", 403)
        chosen.append(int(min(cap, maximum)))
    return inventory, chosen


def snapshot(registry, source_id):
    if not isinstance(source_id, str):
        raise LabError("Укажите source_id подключённой камеры")
    with registry.lock:
        record = registry.sources.get(source_id)
        if record is None or registry.closed:
            raise LabError("Подключённый источник камеры не найден", 404)
        url = catalog.endpoint(record["public"]["url"])
        state = record.setdefault("camera_snapshot", {"busy": False, "last": 0.0})
        if state["busy"]:
            raise LabError("Кадр этой камеры уже запрашивается", 409)
        if time.monotonic() - state["last"] < 1:
            raise LabError("Запрашивайте кадр не чаще одного раза в секунду", 429)
        state["busy"], state["last"] = True, time.monotonic()
    client = catalog.Client(url, timeout=TIMEOUT_S)
    client.deadline = time.monotonic() + TIMEOUT_S
    try:
        inventory, (width, quality) = _review(client)
        result = _capture(client, width, quality)
        if result.get("isError"):
            raise LabError("cam-mcp не выдал кадр: " + str(result.get("content", ""))[:300], 502)
        content = result.get("content")
        if not isinstance(content, list) or len(content) > 16:
            raise LabError("Некорректный ответ snapshot", 502)
        images = [item for item in content if isinstance(item, dict) and item.get("type") == "image"]
        if len(images) != 1 or images[0].get("mimeType") != "image/jpeg" or not isinstance(images[0].get("data"), str):
            raise LabError("snapshot должен вернуть один JPEG ImageContent", 502)
        encoded = images[0]["data"]
        if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise LabError("Кадр превысил 512 KiB", 413)
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise LabError("Некорректный base64 кадра", 502)
        if len(raw) > MAX_IMAGE_BYTES:
            raise LabError("Кадр превысил 512 KiB", 413)
        actual_width, actual_height = _dimensions(raw)
        if actual_width > width:
            raise LabError("cam-mcp превысил запрошенную ширину кадра", 502)
        metadata = {}
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                if len(item["text"].encode()) > MAX_METADATA_BYTES:
                    raise LabError("Метаданные кадра превысили 32 KiB", 413)
                parsed = catalog._json(item["text"].encode())
                _validate_tree(parsed)
                if not isinstance(parsed, dict):
                    raise LabError("Метаданные кадра должны быть JSON-объектом", 502)
                metadata.update(parsed)
        metadata.update(source_id=source_id, camera=inventory["camera"], devices=inventory["devices"],
                        decoded_bytes=len(raw), actual_width=actual_width, actual_height=actual_height)
        if len(json.dumps(metadata, ensure_ascii=False).encode()) > MAX_METADATA_BYTES:
            raise LabError("Метаданные кадра превысили 32 KiB", 413)
        return {"mimeType": "image/jpeg", "data": encoded, "metadata": metadata, "observed_at": time.time(),
                "origin": "cam-mcp · " + inventory["camera"]["source"], "source": inventory["camera"]["source"]}
    except catalog.CatalogError as exc:
        raise LabError(str(exc), 502)
    finally:
        _close(client)
        with registry.lock:
            state["busy"] = False
