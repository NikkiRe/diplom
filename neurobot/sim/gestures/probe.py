"""Счётчики подставных модулей внутри процесса perception и их журнал (JSONL, раз в секунду).

Нужен, чтобы видеть «глухой, но живой» процесс без помощи самого процесса: у демо как есть нет темы
здоровья (A0, R1). Пока поток журнала пишет строки, процесс жив; если det_calls перестал расти —
детектор не вызывается, кадры не обрабатываются.

Строка журнала: {t, m, pid, frames, det_calls, timeouts, marker_miss, faults: [вид, …]}; событие —
{t, m, pid, ev, …} (начало/конец отказа камеры, hardware_reset, рестарт конвейера, команда отказа и пропуск
старых команд при старте процесса, якорь старше таймлайна). Файл задаёт NEUROBOT_FAKES_PROBE; без него счётчики
есть, а журнала нет. Запись дозаписью (O_APPEND) короткими строками — после рестарта процесса новые строки идут в
тот же файл с новым pid.
Журнал — обвязка стенда: ошибка записи (нет каталога, кончилось место) не должна выглядеть отказом камеры,
поэтому она пишется в stderr один раз, а журнал выключается.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from .clock import mono

ENV_PATH = "NEUROBOT_FAKES_PROBE"
COUNTERS = ("frames", "det_calls", "timeouts", "marker_miss")

log = logging.getLogger("neurobot_fakes_probe")


class Probe:
    def __init__(self, path=None, period_s: float = 1.0):
        self.path = path
        self.period_s = period_s
        self._lock = threading.Lock()
        self._counts = {k: 0 for k in COUNTERS}
        self._faults = ()
        self._thread = None

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counts[name] += n

    def get(self, name: str) -> int:
        with self._lock:
            return self._counts[name]

    def set_faults(self, kinds) -> None:
        with self._lock:
            self._faults = tuple(sorted(kinds))

    def event(self, ev: str, **fields) -> None:
        self._write(dict(fields, ev=ev))

    def start(self) -> None:
        """Запустить поток журнала (идемпотентно); без файла — ничего не делать."""
        with self._lock:
            if self.path is None or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="fakes-probe", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while self.path is not None:
            with self._lock:
                rec = dict(self._counts, faults=list(self._faults))
            self._write(rec)
            time.sleep(self.period_s)

    def _write(self, rec: dict) -> None:
        path = self.path
        if path is None:
            return
        rec.update(t=round(time.time(), 6), m=round(mono(), 6), pid=os.getpid())
        line = (json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        except OSError as e:
            self.path = None
            log.error("журнал подставных модулей %s выключен: %s", path, e)


_PROBE = None
_PROBE_LOCK = threading.Lock()


def probe() -> Probe:
    """Общий для процесса экземпляр; файл берётся из окружения при первом обращении."""
    global _PROBE
    with _PROBE_LOCK:
        if _PROBE is None:
            _PROBE = Probe(os.environ.get(ENV_PATH) or None)
            _PROBE.start()
        return _PROBE
