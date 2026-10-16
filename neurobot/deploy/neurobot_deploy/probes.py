"""Сетевые пробы без зависимостей: WebSocket-рукопожатие rosbridge, MCP по streamable HTTP, HTTP GET воркера.

Проба MCP — initialize → notifications/initialized → tools/list (по желанию ещё один tools/call) → DELETE
сессии. tools/list не трогает rosbridge (в rosbridge 1.3.1 вызовы сервисов в цикле текут, #774), но висит,
если сервер занят синхронным инструментом (у FastMCP лаборатории так и есть, M5) — значит, ловит и
смерть, и зависание. Сессия закрывается: иначе сервер копит по сессии на каждую пробу.
Ответ сервера — JSON или SSE (FastMCP по умолчанию отвечает потоком text/event-stream).
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import time
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import urlsplit

PROTOCOL = "2025-03-26"


class ProbeError(Exception):
    pass


def ws_handshake(host: str, port: int, timeout: float, path: str = "/") -> None:
    """Открыть WebSocket и закрыть: rosbridge жив, если ответил 101."""
    key = base64.b64encode(os.urandom(16)).decode()
    req = ("GET {} HTTP/1.1\r\nHost: {}:{}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           "Sec-WebSocket-Key: {}\r\nSec-WebSocket-Version: 13\r\n\r\n").format(path, host, port, key)
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(req.encode())
            head = b""
            while b"\r\n" not in head:
                chunk = s.recv(256)
                if not chunk:
                    break
                head += chunk
    except OSError as e:
        raise ProbeError("{}:{}: {}".format(host, port, e.strerror or e))
    status = head.split(b"\r\n", 1)[0].decode("latin-1")
    if " 101 " not in status + " ":
        raise ProbeError("{}:{}: ответ не WebSocket: {!r}".format(host, port, status[:80]))


def _sse_messages(body: str) -> List[Dict[str, Any]]:
    out = []
    for event in body.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.split("\n") if line.startswith("data:"))
        if data:
            out.append(json.loads(data))
    return out


class McpClient:
    """Минимальный клиент MCP streamable HTTP; каждый запрос укладывается в общий срок deadline."""

    def __init__(self, url: str, timeout: float):
        u = urlsplit(url)
        self.host, self.port, self.path = u.hostname or "127.0.0.1", u.port or 80, u.path or "/"
        self.deadline = time.monotonic() + timeout
        self.session: Optional[str] = None
        self.protocol = PROTOCOL
        self._id = 0

    def _request(self, method: str, body: Optional[dict]) -> Tuple[int, Dict[str, str], str]:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise ProbeError("нет ответа за отведённое время")
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                   "MCP-Protocol-Version": self.protocol}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        conn = http.client.HTTPConnection(self.host, self.port, timeout=left)
        try:
            conn.request(method, self.path, body=json.dumps(body) if body is not None else None, headers=headers)
            resp = conn.getresponse()
            text = resp.read().decode("utf-8", errors="replace")
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, text
        except socket.timeout:
            raise ProbeError("нет ответа за отведённое время")
        except (OSError, http.client.HTTPException) as e:
            raise ProbeError("{}:{}: {}".format(self.host, self.port, getattr(e, "strerror", None) or e))
        finally:
            conn.close()

    def rpc(self, method: str, params: Optional[dict] = None) -> Any:
        self._id += 1
        status, headers, text = self._request("POST", {"jsonrpc": "2.0", "id": self._id, "method": method,
                                                       "params": params or {}})
        if status != 200:
            raise ProbeError("{}: HTTP {}: {}".format(method, status, text[:120]))
        if "mcp-session-id" in headers:
            self.session = headers["mcp-session-id"]
        try:
            msgs = _sse_messages(text) if "text/event-stream" in headers.get("content-type", "") else [json.loads(text)]
        except ValueError:
            raise ProbeError("{}: ответ не JSON: {!r}".format(method, text[:120]))
        for m in msgs:
            if m.get("id") == self._id:
                if "error" in m:
                    raise ProbeError("{}: {}".format(method, m["error"].get("message", m["error"])))
                return m.get("result")
        raise ProbeError("{}: в ответе нет результата".format(method))

    def notify(self, method: str) -> None:
        status, _, text = self._request("POST", {"jsonrpc": "2.0", "method": method})
        if status not in (200, 202, 204):
            raise ProbeError("{}: HTTP {}: {}".format(method, status, text[:120]))

    def close(self) -> None:
        if self.session:
            try:
                self._request("DELETE", None)
            except ProbeError:
                pass
            self.session = None


def http_get(url: str, timeout: float) -> Tuple[int, str]:
    """GET url → (код, начало тела): проба готовности воркера лаборатории (/health, /state)."""
    u = urlsplit(url)
    conn = http.client.HTTPConnection(u.hostname or "127.0.0.1", u.port or 80, timeout=timeout)
    try:
        conn.request("GET", u.path or "/")
        resp = conn.getresponse()
        return resp.status, resp.read(400).decode("utf-8", errors="replace")
    except socket.timeout:
        raise ProbeError("{}: нет ответа за {:g} с".format(url, timeout))
    except (OSError, http.client.HTTPException) as e:
        raise ProbeError("{}: {}".format(url, getattr(e, "strerror", None) or e))
    finally:
        conn.close()


class McpProbe(NamedTuple):
    tools: List[str]
    status: Optional[str]   # текст ответа инструмента состояния, если его вызывали
    elapsed_s: float
    params: Dict[str, List[str]] = {}   # инструмент -> имена аргументов (inputSchema.properties)


def probe_mcp(url: str, timeout: float, status_tool: str = "") -> McpProbe:
    t0 = time.monotonic()
    c = McpClient(url, timeout)
    try:
        init = c.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                    "clientInfo": {"name": "neurobot-probe", "version": "1"}})
        c.protocol = (init or {}).get("protocolVersion", PROTOCOL)
        c.notify("notifications/initialized")
        listed = (c.rpc("tools/list") or {}).get("tools", [])
        tools = [t.get("name", "") for t in listed]
        params = {t.get("name", ""): sorted((t.get("inputSchema") or {}).get("properties") or {}) for t in listed}
        status = None
        if status_tool:
            if status_tool not in tools:
                raise ProbeError("нет инструмента {} (есть: {})".format(status_tool, ", ".join(tools)))
            res = c.rpc("tools/call", {"name": status_tool, "arguments": {}}) or {}
            status = "".join(x.get("text", "") for x in res.get("content", []) if x.get("type") == "text")
            if res.get("isError"):
                raise ProbeError("{}: {}".format(status_tool, status[:160]))
        return McpProbe(tools, status, time.monotonic() - t0, params)
    finally:
        c.close()
