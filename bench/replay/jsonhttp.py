"""Минимальный JSON-клиент поверх http.client: одно keep-alive соединение на сервис, таймауты на всё,
одна попытка переподключения при обрыве. Только stdlib — реплеер не тянет зависимостей."""
from __future__ import annotations

import http.client
import json
import socket
import time
from urllib.parse import urlparse


class HttpError(RuntimeError):
    pass


class JsonClient:
    def __init__(self, base_url: str, timeout_s: float = 30.0, name: str = ""):
        u = urlparse(base_url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"bad URL {base_url!r}")
        self.base_url = base_url.rstrip("/")
        self.name = name or u.hostname
        self._scheme, self._host, self._port = u.scheme, u.hostname, u.port
        self._prefix = u.path.rstrip("/")
        self.timeout_s = timeout_s
        self._conn: http.client.HTTPConnection | None = None
        self.calls = 0

    def _connect(self, timeout: float) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        return cls(self._host, self._port, timeout=timeout)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def request(self, method: str, path: str, obj=None, timeout_s: float | None = None,
                headers: dict | None = None) -> tuple[int, object, bytes, float]:
        """(статус, JSON или None, сырое тело, мс). Сетевые ошибки → HttpError."""
        timeout = self.timeout_s if timeout_s is None else timeout_s
        body = None if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json", **(headers or {})}
        if body is not None:
            headers["Content-Type"] = "application/json"
        last_exc: Exception | None = None
        for attempt in range(2):
            if self._conn is None:
                self._conn = self._connect(timeout)
            self._conn.timeout = timeout
            if self._conn.sock is not None:
                self._conn.sock.settimeout(timeout)
            t0 = time.perf_counter()
            try:
                self._conn.request(method, self._prefix + path, body=body, headers=headers)
                resp = self._conn.getresponse()
                raw = resp.read()
                ms = (time.perf_counter() - t0) * 1000.0
                self.calls += 1
                try:
                    data = json.loads(raw.decode("utf-8")) if raw else None
                except (UnicodeDecodeError, ValueError):
                    data = None
                if resp.getheader("Connection", "").lower() == "close" or resp.status >= 400:
                    self.close()   # после ошибки состояние соединения у сервера неизвестно — переподключиться
                return resp.status, data, raw, ms
            except (socket.timeout, TimeoutError) as e:
                self.close()
                raise HttpError(f"{self.name} {method} {path}: timeout after {timeout:g} s") from e
            except (ConnectionError, http.client.HTTPException, OSError) as e:
                self.close()
                last_exc = e
                if attempt == 0 and isinstance(e, (http.client.RemoteDisconnected, BrokenPipeError,
                                                   ConnectionResetError)):
                    continue   # сервер закрыл keep-alive соединение — переподключиться один раз
                break
        raise HttpError(f"{self.name} {method} {path}: {last_exc}") from last_exc

    def post(self, path: str, obj=None, timeout_s: float | None = None) -> tuple[int, object, bytes, float]:
        return self.request("POST", path, {} if obj is None else obj, timeout_s)

    def get(self, path: str, timeout_s: float | None = None) -> tuple[int, object, bytes, float]:
        return self.request("GET", path, None, timeout_s)
