"""sd_notify(3) без libsystemd: датаграммы в $NOTIFY_SOCKET (READY, WATCHDOG, STOPPING).

Без NOTIFY_SOCKET (запуск не из systemd) ничего не шлётся. WATCHDOG шлётся, только если systemd задал
WATCHDOG_USEC (и WATCHDOG_PID, если задан, — наш), не чаще четверти периода. Ошибка отправки не роняет
процесс: она пишется в журнал, а systemd сам заметит молчание.
"""
from __future__ import annotations

import logging
import os
import socket
import time
from typing import Callable, Mapping, Optional

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, environ: Mapping[str, str] = os.environ, clock: Callable[[], float] = time.monotonic):
        addr = environ.get("NOTIFY_SOCKET") or None
        if addr is not None and addr.startswith("@"):
            addr = "\0" + addr[1:]  # абстрактное пространство имён
        self._addr = addr
        pid = environ.get("WATCHDOG_PID")
        usec = int(environ.get("WATCHDOG_USEC") or 0)
        self.watchdog_s = usec / 1e6 if not pid or int(pid) == os.getpid() else 0.0
        self._clock = clock
        self._last_ping: Optional[float] = None

    def send(self, state: str) -> bool:
        if self._addr is None:
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.connect(self._addr)
                s.sendall(state.encode())
        except OSError as e:
            log.warning("sd_notify %s: %s", state, e)
            return False
        return True

    def ready(self) -> bool:
        return self.send("READY=1")

    def stopping(self) -> bool:
        return self.send("STOPPING=1")

    def watchdog(self) -> bool:
        if not self.watchdog_s:
            return False
        now = self._clock()
        if self._last_ping is not None and now - self._last_ping < self.watchdog_s / 4:
            return False
        self._last_ping = now
        return self.send("WATCHDOG=1")
