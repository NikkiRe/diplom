"""Исполнитель: переносит записи мира в дерево sysfs и линии GPIO, читает реле и нагрузку CPU, ведёт журнал
истины и держит часы — реальные (ожидание до ближайшего срока мира) или виртуальные (HTTP POST /clock). Часы
ждут и канал остановки stop: дескриптор, который становится читаемым по сигналу остановки (__main__)."""
from __future__ import annotations

import json
import math
import os
import resource
import select
import sys
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import IO, Any

from .sensors import BY_NAME, RELAYS
from .tree import Tree
from .world import ClockError, Record, World

MAX_BODY = 1 << 16   # байт тела POST /clock


def _quota_cpus(path: Path) -> float:
    """CPU по квоте cpu.max («квота период» или «max период»); файла нет (контроллер cpu выключен) — без предела."""
    if not path.exists():
        return math.inf
    quota, period = path.read_text().split()
    return math.inf if quota == "max" else int(quota) / int(period)


def _cpuset_cpus(path: Path) -> int:
    """Число CPU в cpuset.cpus.effective («0-2,4»); файла нет (контроллер cpuset выключен) — все CPU машины."""
    text = path.read_text().strip() if path.exists() else ""
    if not text:
        return os.cpu_count() or 1
    ranges = (part.partition("-") for part in text.split(","))
    return sum(int(hi or lo) - int(lo) + 1 for lo, _, hi in ranges)


class CgroupLoad:
    """Загрузка CPU cgroup v2 «платы»: Δusage_usec / (Δt × доступные CPU) за окно не короче WINDOW_S.
    Доступные CPU — меньшее из квоты cpu.max и числа CPU cpuset.cpus.effective этой cgroup (bench/board задаёт
    оба: CPUQuota и AllowedCPUs)."""
    WINDOW_S = 1.0

    def __init__(self, path: Path):
        self.stat = path / "cpu.stat"
        self.cpus = min(_quota_cpus(path / "cpu.max"), _cpuset_cpus(path / "cpuset.cpus.effective"))
        self.last = (self._usage_s(), time.monotonic())

    def _usage_s(self) -> float:
        for line in self.stat.read_text().splitlines():
            key, value = line.split()
            if key == "usage_usec":
                return int(value) / 1e6
        raise ValueError(f"{self.stat}: нет usage_usec")

    def sample(self) -> float | None:
        """Загрузка 0…1 с точностью 0,01 за прошедшее окно; None — окно ещё не прошло."""
        mono = time.monotonic()
        if mono - self.last[1] < self.WINDOW_S:
            return None
        usage = self._usage_s()
        level = (usage - self.last[0]) / ((mono - self.last[1]) * self.cpus)
        self.last = (usage, mono)
        return round(min(max(level, 0.0), 1.0), 2)


class Runner:
    """Один прогон: мир, дерево, линии GPIO, журнал истины (JSONL; t — время сценария, mono — монотонное)."""

    def __init__(self, world: World, tree: Tree, gpio: Any, truth: IO[str] | None, load: CgroupLoad | None = None):
        self.world, self.tree, self.gpio, self.truth, self.load = world, tree, gpio, truth, load
        self.stats: Counter[str] = Counter()
        self._readings: dict[tuple[str, str], float | None] = {}
        self._actuators: dict[str, bool] = {}
        self._load: float | None = None
        self._mono0 = time.monotonic()

    def log(self, kind: str, t: float, mono: float, data: dict) -> None:
        if self.truth:
            self.truth.write(json.dumps({"type": kind, "t": round(t, 6), "mono": round(mono, 6)} | data,
                                        ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            self.stats["truth"] += 1

    def start(self, meta: dict) -> None:
        """Запись run_start и состояние на момент 0 вместе с входами."""
        self.log("run_start", 0.0, time.monotonic(), meta)
        self.sense()
        self.step(0.0)

    def sense(self) -> None:
        """Прочитать входы мира — реле (его пишет hw-mcp) и загрузку cgroup — и применить с текущего момента мира.
        Виртуальные часы читают их перед шагом (hw-mcp стоял в этом моменте), реальные — после шага (вход
        замечен при пробуждении и действует с него)."""
        mono = time.monotonic()
        for dev in RELAYS:
            on = self.gpio.get(dev.line)
            self.world.set_actuator(dev.name, on)
            if self._actuators.get(dev.name) != on:
                self._actuators[dev.name] = on
                self.log("actuator", self.world.t, mono, {"device": dev.name, "value": on})
        if self.load and (level := self.load.sample()) is not None and level != self._load:
            self._load = level
            self.world.set_load(level)
            self.log("load", self.world.t, mono, {"value": level})

    def step(self, now: float) -> None:
        """Продвинуть мир до now и применить его записи."""
        mono = time.monotonic()
        for rec in self.world.advance(now):
            self._apply(rec, mono)
        self.stats["steps"] += 1
        if self.truth:
            self.truth.flush()

    def close(self) -> None:
        """Запись run_end: счётчики, собственные CPU и пиковый RSS симулятора."""
        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss_mib = usage.ru_maxrss / (2**20 if sys.platform == "darwin" else 2**10)   # байты на macOS, КиБ в Linux
        self.log("run_end", self.world.t, time.monotonic(), {
            "stats": dict(self.stats), "cpu_s": round(usage.ru_utime + usage.ru_stime, 3),
            "max_rss_mib": round(rss_mib, 1), "wall_s": round(time.monotonic() - self._mono0, 3)})
        if self.truth:
            self.truth.flush()

    def _apply(self, rec: Record, mono: float) -> None:
        d = rec.data
        if rec.type == "render":
            self.stats["writes"] += self.tree.write(BY_NAME[d["device"]], d["attrs"])
            for prop, reading in d["readings"].items():
                key = d["device"], prop
                if key not in self._readings or self._readings[key] != reading:
                    self._readings[key] = reading
                    self.log("value", rec.t, mono, {"device": d["device"], "property": prop,
                                                    "value": d["values"][prop], "reading": reading})
        elif rec.type == "link":
            self.tree.plug(BY_NAME[d["device"]], d["present"])
            self.log("device", rec.t, mono, d)
        elif rec.type == "gpio":
            if d["bounce"] and not self.gpio.edges:
                return   # дребезг различим только фронтами (gpio-sim); в файлах — один итоговый уровень
            self.gpio.set(d["line"], d["value"])
            self.stats["gpio"] += 1
            self.log("gpio", rec.t, mono, d)
        else:
            self.log(rec.type, rec.t, mono, d)


def wait(stop: int, timeout: float | None = None) -> bool:
    """Ждать сигнала остановки не дольше timeout с (None — без предела); True — сигнал пришёл."""
    return bool(select.select([stop], [], [], timeout)[0])


def run_real(runner: Runner, speed: float, end: float, stop: int) -> None:
    """Реальные часы до конца прогона или сигнала остановки: t = speed × (монотонное время от старта); между
    сроками мира — ожидание сигнала, без опроса."""
    start = time.monotonic()
    while (now := min((time.monotonic() - start) * speed, end)) < end:
        runner.step(now)
        runner.sense()
        due = min(runner.world.next_due(runner.gpio.edges), end)
        if wait(stop, max(0.0, due / speed - (time.monotonic() - start))):
            return
    runner.step(end)


class ClockServer(HTTPServer):
    """Виртуальные часы: POST /clock {"t": …} двигает мир до t (не дальше end) и отвечает после записи
    состояния; GET /healthz — текущий момент. Запросы обрабатываются по одному, как и шаги мира. Сбой шага
    мира запоминается в failure и останавливает прогон (serve_virtual); обрыв соединения клиентом — нет."""
    timeout = 0   # handle_request не ждёт соединения: serve_virtual зовёт его, когда select уже увидел запрос

    def __init__(self, addr: tuple[str, int], runner: Runner, end: float):
        super().__init__(addr, ClockHandler)
        self.runner, self.end = runner, end
        self.failure: Exception | None = None

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Ошибка соединения с клиентом (обрыв, сброс) — одна строка в stderr вместо трассировки."""
        print(f"sysfsim: клиент часов {client_address[0]}:{client_address[1]}: {sys.exc_info()[1]}", file=sys.stderr)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


class ClockHandler(BaseHTTPRequestHandler):
    server: ClockServer
    timeout = 10   # молчащий клиент задерживает остальных не дольше, с

    def do_GET(self) -> None:
        if self.path != "/healthz":
            return self._reply(404, {"error": f"нет пути {self.path}"})
        self._reply(200, {"ok": True, "now": self.server.runner.world.t, "clock": "virtual"})

    def do_POST(self) -> None:
        if self.path != "/clock":
            return self._reply(404, {"error": f"нет пути {self.path}"})
        length = self.headers.get("Content-Length", "")
        try:
            if not length.isdigit() or int(length) > MAX_BODY:
                raise ValueError(length)
            t = json.loads(self.rfile.read(int(length)))["t"]
            if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t):
                raise ValueError(t)
        except (ValueError, KeyError, TypeError):
            return self._reply(400, {"error": f'тело — {{"t": <секунды>}}, Content-Length 0…{MAX_BODY}'})
        server, runner = self.server, self.server.runner
        if t > server.end:
            return self._reply(409, {"error": f"t {t} за концом прогона {server.end}", "now": runner.world.t})
        try:
            runner.sense()
            runner.step(float(t))
        except ClockError as e:
            return self._reply(409, {"error": str(e), "now": runner.world.t})
        except Exception as e:   # сбой шага (файл, линия GPIO) передаётся в serve_virtual
            server.failure = e
            return self._reply(500, {"error": str(e)})
        self._reply(200, {"ok": True, "now": runner.world.t})

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        """Без журнала запросов в stderr: часы двигают тысячи раз за прогон."""


def serve_virtual(server: ClockServer, stop: int) -> None:
    """Обслуживать часы, пока мир не дойдёт до конца прогона или не придёт сигнал остановки (он замечается
    между запросами); сбой шага мира — исключение."""
    while server.runner.world.t < server.end:
        if stop in select.select([server, stop], [], [])[0]:
            return
        server.handle_request()
        if server.failure:
            raise server.failure
