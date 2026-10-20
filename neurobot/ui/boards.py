"""Versioned passive board passports and explicit read-only MCP diagnostics.

Remote reports are imported as content. Only a trusted local probe script runs;
no request body supplies a command, filesystem path, credentials or SSH target.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import catalog
from .lab import LabError, _validate_tree

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "scripts/edge_probe.py"
MAX_REPORT_BYTES = 1024 * 1024
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_BOARDS = 16
MAX_DIAGNOSTIC_BYTES = 1024 * 1024
PROBE_TIMEOUT_S = 9
MCP_TIMEOUT_S = 6


def _encode(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise LabError("Паспорт должен содержать корректный конечный JSON")


def _loads(content):
    def integer(value):
        if len(value) > 310:
            raise ValueError("Число превышает допустимую разрядность")
        return int(value)
    try:
        return json.loads(content, parse_int=integer)
    except (ValueError, UnicodeError, RecursionError):
        raise LabError("Некорректный JSON паспорта")


def _report(value):
    if not isinstance(value, dict) or value.get("format") != "edge-board/1":
        raise LabError("Ожидается паспорт формата edge-board/1; выполните edge_probe.py на устройстве")
    _validate_tree(value)
    for key in ("identity", "system", "buses"):
        if not isinstance(value.get(key), dict):
            raise LabError("Поле %s паспорта должно быть объектом" % key)
    for key in ("storage", "interfaces", "services", "mcp_candidates", "coverage"):
        if not isinstance(value.get(key), list):
            raise LabError("Поле %s паспорта должно быть массивом" % key)
    raw = _encode(value)
    if len(raw) > MAX_REPORT_BYTES:
        raise LabError("Паспорт больше 1 MiB", 413)
    return copy.deepcopy(value), raw


def _label(value, report):
    if value is None:
        identity = report.get("identity") or {}
        model = identity.get("model")
        return model[:160] if isinstance(model, str) and model else "Неизвестное устройство"
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise LabError("Название должно содержать от 1 до 160 символов")
    return value.strip()


def _close_session(client):
    if not client.session:
        return
    parsed = urlsplit(client.url)
    connection = http.client.HTTPConnection("::1" if parsed.hostname == "::1" else "127.0.0.1", parsed.port or 80, timeout=0.2)
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


def inspect_endpoint(body):
    if not isinstance(body, dict) or set(body) - {"url"}:
        raise LabError("Для диагностики передайте только url локального MCP endpoint")
    value = body.get("url")
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise LabError("Укажите адрес MCP endpoint")
    diagnostic = {"url": value, "status": "invalid", "transport": "unknown", "server": None,
                  "protocol": None, "capabilities": {}, "tools": [], "resources": [], "coverage": [],
                  "issues": [], "read_only": True, "checked_at": time.time(), "connected": False, "initialized": False}
    def issue(code, message, severity="error"):
        diagnostic["issues"].append({"code": code, "message": str(message)[:1000], "severity": severity})
    if value.startswith(("stdio:", "python ", "python3 ", "node ", "npx ", "uvx ", "/")) and not value.startswith("//"):
        diagnostic.update(status="stdio", transport="stdio")
        issue("stdio_requires_bridge", "Это stdio-команда или путь, а не HTTP endpoint. Запустите явно настроенный MCP HTTP bridge; команда здесь не выполнялась.", "info")
        return diagnostic
    url = catalog.endpoint(value)  # literal loopback only; no DNS, proxy, redirect or scan
    diagnostic["url"] = url
    client = catalog.Client(url, timeout=2)
    client.deadline = time.monotonic() + MCP_TIMEOUT_S
    initialized = False
    try:
        reply = client.rpc("initialize", {"protocolVersion": catalog.PROTOCOL, "capabilities": {},
                                          "clientInfo": {"name": "edgelab-board-inspector", "version": "1"}})
        initialized = True
        diagnostic["initialized"] = True
        diagnostic["transport"] = "streamable_http"
        info, capabilities, version = reply.get("serverInfo"), reply.get("capabilities"), reply.get("protocolVersion")
        if not isinstance(info, dict) or not isinstance(info.get("name"), str) or not info["name"]:
            issue("invalid_server_info", "initialize не содержит корректный serverInfo.name")
            return diagnostic
        diagnostic["server"] = info
        diagnostic["protocol"] = version
        if not isinstance(capabilities, dict):
            issue("invalid_capabilities", "initialize.capabilities должен быть объектом")
            return diagnostic
        diagnostic["capabilities"] = capabilities
        if not isinstance(version, str) or version not in catalog.VERSIONS:
            issue("unsupported_protocol", "Сервер выбрал неизвестную или неподдерживаемую версию MCP: " + str(version))
            return diagnostic
        client.version = version
        client.rpc("notifications/initialized", notify=True)
        diagnostic.update(status="valid")  # inspection does not connect the source to the catalog
        for capability, method, key in (("tools", "tools/list", "tools"), ("resources", "resources/list", "resources")):
            coverage = {"id": capability, "label": capability, "status": "unsupported", "count": 0,
                        "sources": [method], "errors": []}
            diagnostic["coverage"].append(coverage)
            if capability not in capabilities:
                continue
            try:
                rows = client.listing(method, key)
                diagnostic[key] = rows
                coverage.update(status="ok", count=len(rows))
                if key == "tools":
                    for tool in rows:
                        if not isinstance(tool.get("name"), str) or not isinstance(tool.get("inputSchema"), dict):
                            issue("invalid_tool_descriptor", "Обнаружен инструмент без name/inputSchema", "warning")
                            diagnostic["status"] = "partial"
                else:
                    if any(not isinstance(resource.get("uri"), str) for resource in rows):
                        issue("invalid_resource_descriptor", "Обнаружен ресурс без URI", "warning")
                        diagnostic["status"] = "partial"
            except catalog.CatalogError as exc:
                diagnostic["status"] = "partial"
                coverage.update(status="unavailable", errors=[str(exc)])
                issue(capability + "_list_failed", str(exc), "warning")
        diagnostic["fingerprint"] = {"server_name": info["name"], "protocol": version,
                                      "tool_names": [tool.get("name") for tool in diagnostic["tools"]],
                                      "resource_schemes": sorted({str(resource.get("uri", "")).split(":", 1)[0] for resource in diagnostic["resources"]}),
                                      "tools_called": 0, "resources_read": 0}
        _validate_tree(diagnostic)
        if len(_encode(diagnostic)) > MAX_DIAGNOSTIC_BYTES:
            raise LabError("Диагностика MCP превысила 1 MiB; данные не обрезаны", 413)
    except catalog.CatalogError as exc:
        message = str(exc)
        if re.search(r"HTTP (401|403)\b", message):
            diagnostic["status"] = "auth_required"
            issue("authentication_required", message + ". Учётные данные здесь не запрашиваются и не сохраняются.")
        elif "text/html" in message:
            diagnostic["status"] = "not_mcp"
            issue("html_not_mcp", "URL вернул HTML-страницу вместо MCP JSON-RPC: проверьте путь endpoint (часто /mcp).")
        elif re.search(r"HTTP (404|405)\b", message):
            diagnostic["status"] = "not_mcp"
            issue("wrong_endpoint", message + ". Проверьте транспорт и точный путь MCP endpoint.")
        elif "Content-Type" in message or "JSON" in message or "ответ MCP" in message:
            diagnostic["status"] = "not_mcp" if not initialized else "partial"
            issue("invalid_protocol_response", message)
        else:
            diagnostic["status"] = "unreachable" if not initialized else "partial"
            issue("connection_failed", message)
    finally:
        _close_session(client)
    return diagnostic


class BoardRegistry:
    def __init__(self, state_dir=None):
        self.lock = threading.RLock()
        self._probe_lock = threading.Lock()
        self._inspect_slots = threading.BoundedSemaphore(2)
        self.boards = {}
        self.local_id = None
        self.updated_at = time.time()
        self.state_path = Path(state_dir) / "boards.json" if state_dir is not None else None
        if self.state_path is not None:
            self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.state_path.exists():
                if self.state_path.stat().st_size > MAX_STATE_BYTES:
                    raise LabError("Сохранённый реестр плат больше 8 MiB", 413)
                try:
                    state = _loads(self.state_path.read_text(encoding="utf-8"))
                    if not isinstance(state, dict) or state.get("format") != "edge-boards-state/1" or not isinstance(state.get("boards"), list):
                        raise ValueError("unknown state format")
                    if len(state["boards"]) > MAX_BOARDS:
                        raise ValueError("too many boards")
                    for entry in state["boards"]:
                        _report(entry["report"])
                        if not re.fullmatch(r"board-[a-zA-Z0-9-]{1,64}", entry["id"]):
                            raise ValueError("invalid board id")
                        if entry["id"] in self.boards:
                            raise ValueError("duplicate board id")
                        self.boards[entry["id"]] = entry
                    self.local_id = state.get("local_id") if state.get("local_id") in self.boards else None
                except (ValueError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
                    raise LabError("Не удалось прочитать реестр плат; исходный файл сохранён: " + str(exc))

    def snapshot(self):
        with self.lock:
            return {"format": "edge-boards/1", "boards": copy.deepcopy(list(self.boards.values())),
                    "local_id": self.local_id, "updated_at": self.updated_at,
                    "probe": {"command": "python3 edge_probe.py > board.json", "download": "/api/boards/probe.py",
                              "remote_execution": False},
                    "limits": {"boards": MAX_BOARDS, "report_bytes": MAX_REPORT_BYTES, "state_bytes": MAX_STATE_BYTES,
                               "probe_timeout_s": PROBE_TIMEOUT_S, "mcp_timeout_s": MCP_TIMEOUT_S}}

    def get(self, board_id):
        with self.lock:
            return copy.deepcopy(self.boards.get(board_id))

    def _commit(self, boards, local_id):
        state = {"format": "edge-boards-state/1", "boards": list(boards.values()), "local_id": local_id}
        raw = _encode(state)
        if len(boards) > MAX_BOARDS or len(raw) > MAX_STATE_BYTES:
            raise LabError("Реестр заполнен: предел 16 паспортов / 8 MiB", 413)
        if self.state_path is not None:
            name = None
            try:
                with tempfile.NamedTemporaryFile(dir=str(self.state_path.parent), prefix=".boards-", delete=False) as stream:
                    name = stream.name
                    os.chmod(name, 0o600)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, str(self.state_path))
            except OSError as exc:
                raise LabError("Не удалось сохранить паспорт: " + str(exc), 503)
            finally:
                if name and os.path.exists(name):
                    os.unlink(name)
        self.boards, self.local_id, self.updated_at = boards, local_id, time.time()

    def import_report(self, body):
        if not isinstance(body, dict) or set(body) - {"report", "content", "label"}:
            raise LabError("Импорт принимает report или content и необязательный label; серверные пути запрещены")
        if (body.get("report") is None) == (body.get("content") is None):
            raise LabError("Укажите ровно одно поле: report или content")
        value = body.get("report")
        if body.get("content") is not None:
            content = body["content"]
            if not isinstance(content, str):
                raise LabError("content должен быть строкой JSON")
            try:
                content_size = len(content.encode("utf-8"))
            except UnicodeError:
                raise LabError("Некорректный Unicode паспорта")
            if content_size > MAX_REPORT_BYTES:
                raise LabError("Паспорт больше 1 MiB", 413)
            value = _loads(content)
        report, raw = _report(value)
        digest = hashlib.sha256(raw).hexdigest()
        entry = {"id": "board-" + digest[:16], "label": _label(body.get("label"), report), "origin": "import",
                 "report": report, "sha256": digest, "imported_at": time.time()}
        with self.lock:
            proposed = dict(self.boards)
            proposed[entry["id"]] = entry
            self._commit(proposed, self.local_id)
        return {"board": copy.deepcopy(entry)}

    def local(self):
        if not self._probe_lock.acquire(blocking=False):
            raise LabError("Локальный probe уже выполняется", 409)
        try:
            return self._local_impl()
        finally:
            self._probe_lock.release()

    def _local_impl(self):
        # The fixed standalone script bounds its output. An outer deadline also
        # protects HTTP threads from a stalled kernel/sysfs read or subprocess.
        try:
            proc = subprocess.Popen([sys.executable, str(PROBE)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=(os.name == "posix"))
        except OSError as exc:
            raise LabError("Не удалось запустить локальный probe: " + str(exc), 503)
        try:
            raw, error = proc.communicate(timeout=PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            proc.communicate(timeout=2)
            raise LabError("Локальный probe превысил 9 секунд", 504)
        if proc.returncode:
            raise LabError("Локальный probe не завершился: " + error[-1000:].decode("utf-8", "replace"), 503)
        if len(raw) > MAX_REPORT_BYTES:
            raise LabError("Локальный паспорт больше 1 MiB", 413)
        try:
            report, encoded = _report(json.loads(raw.decode("utf-8")))
        except (ValueError, UnicodeError):
            raise LabError("Локальный probe вернул некорректный паспорт", 502)
        entry = {"id": "board-local", "label": _label(None, report), "origin": "local", "report": report,
                 "sha256": hashlib.sha256(encoded).hexdigest(), "imported_at": time.time()}
        with self.lock:
            proposed = dict(self.boards)
            proposed[entry["id"]] = entry
            self._commit(proposed, entry["id"])
        return {"board": copy.deepcopy(entry)}

    def forget(self, board_id):
        if not isinstance(board_id, str):
            raise LabError("Укажите id паспорта")
        with self.lock:
            if board_id not in self.boards:
                raise LabError("Паспорт не найден", 404)
            proposed = dict(self.boards)
            del proposed[board_id]
            self._commit(proposed, None if self.local_id == board_id else self.local_id)
        return {"forgotten": board_id}

    def inspect_mcp(self, body):
        if not self._inspect_slots.acquire(blocking=False):
            raise LabError("Одновременно выполняется не больше двух проверок MCP", 429)
        try:
            return inspect_endpoint(body)
        finally:
            self._inspect_slots.release()

    def close(self):
        pass  # no worker threads or persistent open descriptors


def mutate(registry, path, body):
    try:
        if not isinstance(body, dict):
            raise LabError("Ожидается JSON-объект")
        if path == "/api/boards/import":
            return 201, registry.import_report(body)
        if path == "/api/boards/local":
            if body:
                raise LabError("Локальный probe не принимает пути или команды")
            return 201, registry.local()
        if path == "/api/boards/forget":
            if set(body) != {"id"}:
                raise LabError("Передайте только id паспорта")
            return 200, registry.forget(body["id"])
        if path == "/api/boards/mcp/inspect":
            return 200, registry.inspect_mcp(body)
        raise LabError("Неизвестный маршрут паспортов", 404)
    except (LabError, catalog.CatalogError) as exc:
        return getattr(exc, "status", 400), {"error": str(exc)}
