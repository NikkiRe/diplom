"""Мелкие части узла predict_live v2 без torch и rclpy: sd_notify, сектор лидара, ящик «последняя побеждает»,
разбор /neurobot/motion_state исполнителя и /neurobot/safety_state safety_gate.

Notifier и front_scan — копии neurobot/executor/sd_notify.py и guard.front_scan: патч для ветки
лаборатории должен быть самодостаточным (кладётся в src/camera/ без пакета neurobot). Совпадение поведения
проверяют общие тесты (neurobot/tests/test_perception_util.py).
"""
from __future__ import annotations

import functools
import json
import logging
import math
import os
import socket
import threading
import time
from typing import Callable, Mapping, NamedTuple, Optional, Sequence

log = logging.getLogger(__name__)


class Notifier:
    """sd_notify(3) без libsystemd: датаграммы в $NOTIFY_SOCKET (READY, WATCHDOG, STOPPING).

    Без NOTIFY_SOCKET ничего не шлётся. WATCHDOG — только при WATCHDOG_USEC (и нашем WATCHDOG_PID, если
    задан), не чаще четверти периода. Ошибка отправки пишется в журнал и процесс не роняет.
    """

    def __init__(self, environ: Mapping[str, str] = os.environ, clock: Callable[[], float] = time.monotonic):
        addr = environ.get("NOTIFY_SOCKET") or None
        if addr is not None and addr.startswith("@"):
            addr = "\0" + addr[1:]
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


class Front(NamedTuple):
    nearest: float  # м; ближайшая достоверная дальность в секторе, inf — ничего
    gap_deg: float  # самый широкий сплошной провал в секторе, градусы


def _wrap(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


@functools.lru_cache(maxsize=8)
def _sector(angle_min: float, angle_increment: float, n: int, half_width: float) -> tuple:
    angles = sorted((_wrap(angle_min + i * angle_increment), i) for i in range(n))
    return tuple(i for a, i in angles if abs(a) <= half_width)


def front_scan(ranges: Sequence[float], angle_min: float, angle_increment: float, half_width: float,
               range_min: float, range_max: float) -> Front:
    """Сектор ±half_width (рад) вокруг оси x робота: ближайшая достоверная дальность и самый широкий провал (R8).

    Сектор — по углам из angle_min/angle_increment, углы приводятся к (−π, π]. −inf (REP 117) — это 0; +inf и
    значения дальше range_max — «ничего нет»; NaN и значения ниже range_min (коды ошибок URG) — провал.
    """
    nearest = math.inf
    run = longest = 0
    for i in _sector(angle_min, angle_increment, len(ranges), half_width):
        r = ranges[i]
        if r == -math.inf:
            run, nearest = 0, 0.0
        elif math.isnan(r) or r < range_min:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
            if r <= range_max and r < nearest:
                nearest = r
    return Front(nearest, longest * abs(math.degrees(angle_increment)))


class LatestSlot:
    """Ящик на одно значение: новое вытесняет непрочитанное (R7 — вместо Queue(3) + put_nowait)."""

    def __init__(self):
        self._cv = threading.Condition()
        self._value = None

    def put(self, value) -> bool:
        """Положить значение; True — вытеснено непрочитанное."""
        with self._cv:
            replaced = self._value is not None
            self._value = value
            self._cv.notify()
        return replaced

    def take(self, timeout: float):
        """Забрать значение или None, если за timeout ничего не пришло."""
        with self._cv:
            if self._value is None:
                self._cv.wait(timeout)
            value, self._value = self._value, None
        return value


class MotionState(NamedTuple):
    moving: bool         # исполнитель (страж) публикует ненулевую команду
    front_blocked: bool  # вперёд нельзя: страж v1 срезал жест (cut при by=gesture) или blocked_front (PLAN §2.3)


def parse_motion_state(data: str) -> MotionState:
    """Строка JSON /neurobot/motion_state; ValueError — не разобрана, нет moving или moving, cut, blocked_front не
    true/false (строка "false" истинна, поэтому типы проверяются, а не приводятся). Поля стража v1 (cut, by) и схемы
    PLAN §2.3 (blocked_front) — оба варианта, недостающее — false."""
    try:
        doc = json.loads(data)
        moving, by = doc["moving"], doc.get("by")
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise ValueError("motion_state не разобран: {}".format(e)) from e
    bad = {n: doc[n] for n in ("moving", "cut", "blocked_front") if n in doc and not isinstance(doc[n], bool)}
    if bad:
        raise ValueError("motion_state: {} — true/false, а не {!r}".format(", ".join(bad), list(bad.values())))
    return MotionState(moving, (doc.get("cut", False) and by == "gesture") or doc.get("blocked_front", False))


SAFETY_STATES = ("ok", "slow", "blocked", "estop")


class SafetyState(NamedTuple):
    state: str       # SAFETY_STATES
    reasons: tuple   # причины блокировки или защёлки — только для журнала


def parse_safety_state(data: str) -> SafetyState:
    """Строка JSON /neurobot/safety_state ({state, reasons[], min_clearance_m, limits}, контракт этапа); ValueError —
    не разобрана или state не из SAFETY_STATES. reasons нужны только журналу: не список — пусто, элементы — str()."""
    try:
        doc = json.loads(data)
        state, reasons = doc["state"], doc.get("reasons")
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise ValueError("safety_state не разобран: {}".format(e)) from e
    if not (isinstance(state, str) and state in SAFETY_STATES):
        raise ValueError("safety_state: state {!r} не из {}".format(state, "|".join(SAFETY_STATES)))
    return SafetyState(state, tuple(str(r) for r in reasons) if isinstance(reasons, list) else ())
