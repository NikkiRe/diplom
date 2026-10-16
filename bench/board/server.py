"""Сервер проб сети на Mac: TCP и UDP на 127.0.0.1, из ВМ — через host.lima.internal (прокси Lima).

Протокол — в bench/board/probe.py (клиент). Порты выбирает система; сервер живёт, пока идёт verify.
Число учитываемых UDP-сессий ограничено MAX_SESSIONS: датаграммы сверх предела отражаются, но не считаются.
"""
from __future__ import annotations

import json
import socket
import socketserver
import threading
import time

from .probe import CHUNK, MAX_ECHO, MAX_LINE, MAX_TRANSFER

MAX_SESSIONS = 4096


class _TcpHandler(socketserver.StreamRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def handle(self) -> None:
        command, _, arg = self.rfile.readline(MAX_LINE).decode("ascii", "replace").strip().partition(" ")
        if command == "UDP":
            self._reply({"received": self.server.owner.received(arg)})
            return
        if not arg.isdigit():
            return
        size = int(arg)
        if command == "ECHO" and 0 < size <= MAX_ECHO:
            while len(data := self.rfile.read(size)) == size:
                self.wfile.write(data)
        elif command == "DOWN" and size <= MAX_TRANSFER:
            payload = memoryview(bytes(CHUNK))
            while size:
                n = min(size, CHUNK)
                self.wfile.write(payload[:n])
                size -= n
        elif command == "UP" and size <= MAX_TRANSFER:
            self._reply(self._receive(size))

    def _receive(self, size: int) -> dict:
        total = first = 0
        t_first = t_last = 0.0
        while total < size and (chunk := self.rfile.read1(CHUNK)):
            t_last = time.perf_counter()
            if not total:
                first, t_first = len(chunk), t_last
            total += len(chunk)
        return {"bytes": total - first, "seconds": t_last - t_first, "total": total}

    def _reply(self, doc: dict) -> None:
        self.wfile.write(json.dumps(doc).encode() + b"\n")


class _UdpHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data, sock = self.request
        self.server.owner.count(data.split(b" ", 1)[0].decode("ascii", "replace"))
        sock.sendto(data, self.client_address)


class _Tcp(socketserver.ThreadingTCPServer):
    daemon_threads = True   # соединение, брошенное пробой, не держит выход из verify


class ProbeServer:
    """with ProbeServer() as s: s.tcp_port, s.udp_port — сервер в фоновых потоках до выхода из блока."""

    def __init__(self, host: str = "127.0.0.1"):
        self._tcp = _Tcp((host, 0), _TcpHandler)
        self._udp = socketserver.UDPServer((host, 0), _UdpHandler)
        self._tcp.owner = self._udp.owner = self
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()
        self._threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (self._tcp, self._udp)]

    @property
    def tcp_port(self) -> int:
        return self._tcp.server_address[1]

    @property
    def udp_port(self) -> int:
        return self._udp.server_address[1]

    def count(self, session: str) -> None:
        with self._lock:
            if session in self._counts or len(self._counts) < MAX_SESSIONS:
                self._counts[session] = self._counts.get(session, 0) + 1

    def received(self, session: str) -> int:
        with self._lock:
            return self._counts.get(session, 0)

    def __enter__(self) -> ProbeServer:
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *exc) -> None:
        for server in (self._tcp, self._udp):
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join()
