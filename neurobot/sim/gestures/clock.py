"""Общая шкала времени источника жестов: якорь таймлайна и метка времени кадра.

Якорь — JSON-файл {t_wall, t_mono, timeline_sha256, pid, boot_id}: момент, который соответствует t = 0
таймлайна gesturegen. Его создаёт первый вызов FakeYolo (процесс perception уже обрабатывает кадры), а все
следующие процессы (рестарт perception) читают тот же файл: «мир» с жестами идёт дальше, жесты во время
простоя теряются, как и на роботе. Создание атомарное (O_EXCL), поэтому два процесса не получат разных якорей.

t_mono — CLOCK_MONOTONIC (mono(): общий для всех процессов одной загрузки ОС), по нему ищется бокс; t_wall —
time.time() в тот же момент, по нему наблюдатель переводит истину в стенное время. Часы — ВМ sim. Якорь
живого прогона должен быть из текущей загрузки ОС: boot_id ядра Linux, а где его нет (macOS) — смещение
t_wall − t_mono, оно постоянно в пределах загрузки. Офлайн-разбору «сейчас» не нужно — он загрузку не сверяет.

Метка кадра — время захвата (CLOCK_MONOTONIC, float64) в строке 0 кадра: 4 байта сигнатуры и 8 байт
времени в столбцах 0..11 и зеркально в столбцах W−1..W−12, одинаково во всех каналах. Поэтому метка
переживает cv2.flip(img, 1) из predict_live.py и перестановку BGR↔RGB, а замороженный кадр несёт старое
время — FakeYolo отдаёт для него старый бокс, как настоящий детектор на застывшей картинке.
"""
from __future__ import annotations

import json
import os
import struct
import time
from dataclasses import asdict, dataclass

ENV_TIMELINE = "NEUROBOT_GESTURES_TIMELINE"
ENV_ANCHOR = "NEUROBOT_GESTURES_ANCHOR"
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
BOOT_TOL_S = 60.0       # допуск сверки загрузки по смещению t_wall − t_mono (ОС без boot_id), с
MAGIC = b"NBG1"
_MARK_LEN = len(MAGIC) + 8


def mono() -> float:
    """CLOCK_MONOTONIC явно: time.monotonic() в Python 3.8 на macOS отсчитывается от старта процесса,
    а якорь и метки кадров сравниваются между процессами."""
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def boot_id() -> str:
    """Идентификатор загрузки ОС (Linux); на других ОС — пустая строка."""
    try:
        with open(BOOT_ID_PATH, encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        return ""


@dataclass(frozen=True)
class Anchor:
    t_wall: float
    t_mono: float
    timeline_sha256: str
    pid: int
    boot_id: str = ""

    def mono_of(self, rel: float) -> float:
        return self.t_mono + rel

    def rel_of(self, mono: float) -> float:
        return mono - self.t_mono

    def same_boot(self) -> bool:
        """Якорь создан в текущей загрузке ОС: по boot_id, а без него — по смещению стенных и монотонных часов."""
        current = boot_id()
        if self.boot_id and current:
            return self.boot_id == current
        return abs((time.time() - mono()) - (self.t_wall - self.t_mono)) <= BOOT_TOL_S


def read_anchor(path: str, live: bool = True) -> Anchor:
    """live — якорь идущего прогона: созданный в другой загрузке ОС — ошибка (нужен новый файл)."""
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    a = Anchor(t_wall=float(d["t_wall"]), t_mono=float(d["t_mono"]), timeline_sha256=str(d["timeline_sha256"]),
               pid=int(d["pid"]), boot_id=str(d.get("boot_id", "")))
    if live and not a.same_boot():
        raise RuntimeError("якорь %s создан в другой загрузке ОС — нужен новый файл на прогон" % path)
    return a


def load_or_create_anchor(path: str, timeline_sha256: str) -> Anchor:
    """Прочитать якорь или создать его «сейчас». Якорь другого таймлайна или другой загрузки ОС — ошибка
    (файл от прошлого прогона)."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        a = _read_complete(path)
    else:
        a = Anchor(t_wall=time.time(), t_mono=mono(), timeline_sha256=timeline_sha256, pid=os.getpid(),
                   boot_id=boot_id())
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(a), fh)
            fh.flush()
            os.fsync(fh.fileno())
    if a.timeline_sha256 != timeline_sha256:
        raise RuntimeError("якорь %s относится к другому таймлайну (sha256 %s…, ожидался %s…)"
                           % (path, a.timeline_sha256[:12], timeline_sha256[:12]))
    return a


def _read_complete(path: str, wait_s: float = 2.0) -> Anchor:
    """Файл мог быть создан другим процессом и ещё не дописан: ждать полный JSON до wait_s."""
    deadline = mono() + wait_s
    while True:
        try:
            return read_anchor(path)
        except (ValueError, KeyError):
            if mono() > deadline:
                raise
            time.sleep(0.01)


def encode_marker(img, t_mono: float) -> None:
    """Записать метку времени захвата в строку 0 кадра (H×W×C, uint8) на месте."""
    row = img[0]
    data = MAGIC + struct.pack("<d", t_mono)
    w = row.shape[0]
    for i, b in enumerate(data):
        row[i] = b
        row[w - 1 - i] = b


def decode_marker(img):
    """Время захвата из метки или None, если метки нет (чужой, изменённый или чёрный кадр)."""
    if getattr(img, "ndim", 0) < 2 or img.shape[1] < 2 * _MARK_LEN:
        return None
    row = img[0]
    first = row[:_MARK_LEN] if row.ndim == 1 else row[:_MARK_LEN, 0]
    data = bytes(int(v) for v in first)
    if data[:len(MAGIC)] != MAGIC:
        return None
    return struct.unpack("<d", data[len(MAGIC):])[0]
