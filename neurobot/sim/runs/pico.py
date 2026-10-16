"""Агентский путь прогона без LLM-денег (README §2.3): реплика пользователя в канал pico шлюза PicoClaw (WebSocket
`/pico/ws`, как у лаборатории — канал pico включён в их конфиге) и ожидание bench/mockllm (оракул отвечает вызовом
инструмента и «Готово.»). Только stdlib: клиент WebSocket RFC 6455 — текстовые кадры, ping/pong, маска клиента.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import urllib.request
from typing import List, Optional, Tuple

OP_CONT, OP_TEXT, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x8, 0x9, 0xA


def frame(opcode: int, payload: bytes, mask: Optional[bytes] = None) -> bytes:
    """Кадр клиента (FIN, маска обязательна по RFC 6455 §5.3)."""
    mask = mask if mask is not None else os.urandom(4)
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def parse(buf: bytes) -> Optional[Tuple[int, bool, bytes, int]]:
    """Кадр сервера из начала buf: (opcode, fin, payload, длина кадра) или None, если кадр ещё не весь."""
    if len(buf) < 2:
        return None
    fin, opcode = bool(buf[0] & 0x80), buf[0] & 0x0F
    masked, n, pos = bool(buf[1] & 0x80), buf[1] & 0x7F, 2
    if n == 126:
        if len(buf) < 4:
            return None
        n, pos = struct.unpack(">H", buf[2:4])[0], 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n, pos = struct.unpack(">Q", buf[2:10])[0], 10
    key = b""
    if masked:
        key, pos = buf[pos:pos + 4], pos + 4
    if len(buf) < pos + n:
        return None
    data = buf[pos:pos + n]
    if masked:
        data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
    return opcode, fin, data, pos + n


class PicoClient:
    """Одно соединение с каналом pico; send() — реплика и ответы шлюза до финального сообщения."""

    def __init__(self, host: str, port: int, token: str, session: str, timeout: float = 10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET /pico/ws?session_id={} HTTP/1.1\r\nHost: {}:{}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: {}\r\nSec-WebSocket-Version: 13\r\nAuthorization: Bearer {}\r\n\r\n").format(
            session, host, port, key, token)
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("pico: соединение закрыто при рукопожатии")
            head += chunk
        status, _, rest = head.partition(b"\r\n\r\n")
        if b" 101 " not in status.split(b"\r\n", 1)[0]:
            raise ConnectionError("pico: " + status.split(b"\r\n", 1)[0].decode("utf-8", "replace"))
        self.buf = rest

    def _send(self, opcode: int, data: bytes) -> None:
        self.sock.sendall(frame(opcode, data))

    def recv(self, timeout: float) -> Optional[dict]:
        """Следующее сообщение JSON (ping сервера отвечается сам) или None по таймауту."""
        end, parts = time.monotonic() + timeout, b""
        while True:
            got = parse(self.buf)
            if got:
                opcode, fin, data, used = got
                self.buf = self.buf[used:]
                if opcode == OP_PING:
                    self._send(OP_PONG, data)
                elif opcode == OP_CLOSE:
                    raise ConnectionError("pico: сервер закрыл соединение")
                elif opcode in (OP_TEXT, OP_CONT):
                    parts += data
                    if fin:
                        return json.loads(parts.decode("utf-8"))
                continue
            left = end - time.monotonic()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                raise ConnectionError("pico: соединение оборвано")
            self.buf += chunk

    def send(self, text: str, timeout: float = 60.0) -> List[dict]:
        """Реплика; сообщения шлюза до первого message.create без kind (финальный ответ хода) или ошибки."""
        self._send(OP_TEXT, json.dumps({"type": "message.send", "id": "runs-%d" % int(time.time() * 1000),
                                        "payload": {"content": text}}, ensure_ascii=False).encode("utf-8"))
        out, end = [], time.monotonic() + timeout
        while time.monotonic() < end:
            msg = self.recv(end - time.monotonic())
            if msg is None:
                break
            out.append(msg)
            payload = msg.get("payload") or {}
            if msg.get("type") == "error" or (msg.get("type") == "message.create" and not payload.get("kind")):
                break
        return out

    def close(self) -> None:
        try:
            self._send(OP_CLOSE, b"")
        except OSError:
            pass
        self.sock.close()


def final_text(messages: List[dict]) -> Optional[str]:
    """Текст финального ответа хода или None (нет ответа, ошибка канала)."""
    for m in reversed(messages):
        payload = m.get("payload") or {}
        if m.get("type") == "message.create" and not payload.get("kind"):
            return payload.get("content")
    return None


class MockLLM:
    """Управление bench/mockllm (README mockllm, «Эндпойнты»)."""

    def __init__(self, base: str, timeout: float = 5.0):
        self.base, self.timeout = base.rstrip("/"), timeout

    def _call(self, path: str, body: Optional[dict] = None) -> bytes:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json"},
                                     method="GET" if body is None else "POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read()

    def expect(self, input_id: str, text: str, calls: List[dict]) -> None:
        """Ожидание реплики: оракул ответит вызовами calls ({name, args}) и затем финалом."""
        self._call("/control/expect", {"input_id": input_id, "text": text,
                                       "expect": {"type": "action", "calls": calls}})

    def clear(self, input_id: str) -> None:
        self._call("/control/clear", {"input_id": input_id})

    def state(self) -> dict:
        return json.loads(self._call("/control/state"))

    def journal(self, since: int = 0) -> List[dict]:
        text = self._call("/control/journal?since=%d" % since).decode("utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]
