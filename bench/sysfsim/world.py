"""Мир симулятора: физика, сигналы входов, отказы и показания датчиков без ввода-вывода.

Состояние — чистая функция (сценарий, seed, входы, t). Входы — то, что мир не моделирует сам: реле
(его пишет hw-mcp) и, по желанию, нагрузка CPU «платы». advance(t) возвращает записи Record в порядке
времени; исполнитель (runner.py) пишет по ним атрибуты sysfs, линии GPIO и журнал истины.

Датчик обновляется в моменты k·period_s, шум зависит только от (seed, устройство, свойство, k), поэтому
содержимое атрибутов на момент t не зависит от того, как часто двигали часы: при большом скачке
промежуточные моменты (и снимки истины) просто пропускаются, и число записей за шаг ограничено
сценарием, а не длиной скачка. Отказ stuck держит значение, измеренное в момент его начала.
"""
from __future__ import annotations

import hashlib
import math
import random
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass

from .physics import EPS, STEP_S, Edge, Physics, pir_edges, poisson, reed_edges
from .scenario import OPS, VALUE_FAULTS, Scenario
from .sensors import BY_NAME, INPUTS, MODELS, SENSORS, Device, props

SNAPSHOT_S = 10.0    # период записей truth — истинного состояния среды
# порядок записей одного момента: сначала сценарий (отказ действует на показание того же момента)
ORDER = {"event": 0, "fault": 0, "question": 1, "situation": 1, "gpio": 2, "render": 3, "truth": 4}


class ClockError(ValueError):
    """Часы назад."""


@dataclass(frozen=True)
class Record:
    """Запись мира в момент t сценария: event | fault | link | question | situation | gpio | render | truth."""
    t: float
    type: str
    data: dict


@dataclass(frozen=True)
class Fault:
    id: str              # f<номер в сценарии>; у случайных — f<номер>.<номер случая>
    kind: str
    device: str
    prop: str | None     # свойство для spike, drift, noise_burst
    t0: float
    t1: float
    params: dict


def expand_faults(sc: Scenario, seed: int) -> list[Fault]:
    """Отказы сценария; у заданных частотой rate_per_h — пуассоновский поток моментов на [0, duration_s)."""
    out = []
    for i, f in enumerate(sc.faults):
        prop = f.get("property", props(BY_NAME[f["device"]])[0]) if f["kind"] in VALUE_FAULTS else None
        params = {k: v for k, v in f.items()
                  if k not in ("kind", "device", "property", "t", "rate_per_h", "duration_s", "note")}
        if "t" in f:
            starts = {f"f{i}": f["t"]}
        else:
            moments = poisson(0.0, sc.duration_s, f["rate_per_h"] / 3600, random.Random(f"{seed}:fault:{i}"))
            starts = {f"f{i}.{j}": round(t, 3) for j, t in enumerate(moments)}
        out += [Fault(fid, f["kind"], f["device"], prop, t0, t0 + f["duration_s"], params)
                for fid, t0 in starts.items()]
    return sorted(out, key=lambda f: f.t0)


def _uniforms(seed: int, key: tuple) -> tuple[float, float]:
    """Две равномерные величины на [0, 1), зависящие только от (seed, key)."""
    h = hashlib.blake2b(repr((seed, *key)).encode(), digest_size=16).digest()
    return (int.from_bytes(h[:8]) >> 11) * 2.0**-53, (int.from_bytes(h[8:]) >> 11) * 2.0**-53


def gauss(seed: int, *key) -> float:
    """N(0, 1) от (seed, key) по Боксу — Мюллеру: шум не зависит от порядка и числа вызовов."""
    u1, u2 = _uniforms(seed, key)
    return math.sqrt(-2 * math.log1p(-u1)) * math.cos(2 * math.pi * u2)


def _strip(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in ("t", "note")}


class Signal:
    """Переходы цифрового входа и курсор выдачи."""

    def __init__(self, device: Device, edges: list[Edge]):
        self.device, self.edges = device, edges
        self.times = [e.t for e in edges]
        self.pos = 0

    def level_at(self, t: float) -> bool:
        i = bisect_right(self.times, t)
        return self.edges[i - 1].level if i else False

    def take(self, now: float) -> list[Edge]:
        end = bisect_right(self.times, now, lo=self.pos)
        out, self.pos = self.edges[self.pos:end], end
        return out

    def next_t(self, bounce: bool) -> float:
        for i in range(self.pos, len(self.edges)):
            if bounce or not self.edges[i].bounce:
                return self.edges[i].t
        return math.inf


class World:
    """Помещения, датчики и входы платы по сценарию; время двигает advance, входы — set_actuator и set_load."""

    def __init__(self, sc: Scenario, seed: int, *, external_load: bool = False):
        self.sc, self.seed = sc, seed
        self.physics = Physics(sc, external_load=external_load)
        self.faults = expand_faults(sc, seed)
        self._faults_of = {d.name: [f for f in self.faults if f.device == d.name] for d in SENSORS + INPUTS}
        self.signals = {d.name: Signal(d, self._edges(d)) for d in INPUTS}
        self._timeline = sorted(self._scenario_records(), key=lambda r: (r.t, ORDER[r.type]))
        self._pos = 0
        self._instant = {d.name: -1 for d in SENSORS}     # последний выданный момент k обновления
        self._plugged = {d.name: True for d in SENSORS}
        self._frozen: dict[str, dict[str, float]] = {}    # id отказа stuck → значения в момент его начала
        self._when = [s for s in sc.situations if "when" in s]
        self._snapshots = 0
        self._started = False
        self.t = 0.0

    # ------------------------------------------------------------------ входы
    def set_actuator(self, name: str, on: bool) -> None:
        """Состояние реле с текущего момента: его видит физика зоны вентилятора."""
        fan = self.physics.zones[BY_NAME[name].zone].fan
        if (fan.at(self.t) > 0) != on:
            fan.set(self.t, 1.0 if on else 0.0)
            fan.forget(self.physics.n * STEP_S)

    def set_load(self, level: float) -> None:
        """Нагрузка CPU платы (0…1) с текущего момента (вместо событий load сценария)."""
        load = self.physics.load
        if load.at(self.t) != level:
            load.set(self.t, level)
            load.forget(self.physics.n * STEP_S)

    # ------------------------------------------------------------------ время
    def advance(self, now: float) -> list[Record]:
        """Продвинуть мир до момента now; записи — в порядке времени."""
        if now < self.t:
            raise ClockError(f"часы назад: {now} < {self.t}")
        out = [] if self._started else self._start()
        for rec in self._due(now):
            out += self._step_to(rec.t)
            out += self._resolve(rec)
        out += self._step_to(now)
        self.t = now
        return out

    def next_due(self, bounce: bool) -> float:
        """Ближайший момент изменения (bounce — учитывать переходы дребезга)."""
        due = [self._timeline[self._pos].t if self._pos < len(self._timeline) else math.inf,
               self._snapshots * SNAPSHOT_S]
        due += [(self._instant[d.name] + 1) * self.sc.devices[d.name]["period_s"] for d in SENSORS]
        due += [s.next_t(bounce) for s in self.signals.values()]
        return min(due)

    def truth(self, name: str, prop: str, t: float) -> float | bool:
        """Истинное значение свойства устройства в момент t текущего шага физики."""
        dev = BY_NAME[name]
        if dev.name in self.signals:
            return self.signals[name].level_at(t)
        if dev.model == "relay":
            return self.physics.zones[dev.zone].fan.at(t) > 0
        return round(self._true(dev, t)[prop], 6)

    def _start(self) -> list[Record]:
        self._started = True
        out = self._check_when(0.0)
        for sig in self.signals.values():
            sig.take(0.0)
            out.append(Record(0.0, "gpio", self._gpio(sig.device, sig.level_at(0.0), False) | {"initial": True}))
        return out

    def _due(self, now: float) -> list[Record]:
        """Что наступило к моменту now: события сценария, фронты входов, обновления датчиков, снимки истины."""
        recs = []
        while self._pos < len(self._timeline) and self._timeline[self._pos].t <= now:
            recs.append(self._timeline[self._pos])
            self._pos += 1
        for sig in self.signals.values():
            recs += [Record(e.t, "gpio", self._gpio(sig.device, e.level, e.bounce)) for e in sig.take(now)]
        for d in SENSORS:
            period = self.sc.devices[d.name]["period_s"]
            k = math.floor(now / period + EPS)
            if k > self._instant[d.name]:
                self._instant[d.name] = k
                recs.append(Record(k * period, "render", {"device": d.name}))
        if (k := math.floor(now / SNAPSHOT_S + EPS)) >= self._snapshots:
            recs.append(Record(k * SNAPSHOT_S, "truth", {}))
            self._snapshots = k + 1
        return sorted(recs, key=lambda r: (r.t, ORDER[r.type]))

    def _step_to(self, t: float) -> list[Record]:
        fired: list[Record] = []
        self.physics.advance(t, (lambda tn: fired.extend(self._check_when(tn))) if self._when else None)
        return fired

    def _check_when(self, t: float) -> list[Record]:
        """Ситуации с условием when, ставшим истинным в момент t (граница шага физики)."""
        fired = []
        for s in self._when:
            w = s["when"]
            op = next(op for op in OPS if op in w)
            if t >= w.get("after", 0) and OPS[op](self.truth(w["device"], w["property"], t), w[op]):
                fired.append(s)
        for s in fired:
            self._when.remove(s)
        return [Record(t, "situation", _strip(s)) for s in fired]

    def _resolve(self, rec: Record) -> list[Record]:
        if rec.type == "render":
            dev = BY_NAME[rec.data["device"]]
            return [self._render(dev, rec.t)] if self._plugged[dev.name] else []
        if rec.type == "question":
            e = rec.data["expect"]
            return [Record(rec.t, "question", rec.data | {"value": self.truth(e["device"], e["property"], rec.t)})]
        if rec.type == "truth":
            return [Record(rec.t, "truth", self._snapshot(rec.t))]
        if rec.type == "fault":
            return [rec, *self._fault(rec)]
        return [rec]

    def _fault(self, rec: Record) -> list[Record]:
        """Последствия границы отказа: значение stuck, исчезновение и возвращение устройства."""
        d, t = rec.data, rec.t
        dev = BY_NAME[d["device"]]
        if d["kind"] == "stuck" and d["phase"] == "start":
            others = [f for f in self._active(dev, t) if f.kind != "stuck"]
            self._frozen[d["id"]] = self._measure(dev, t, self._true(dev, t), others)
        if d["kind"] != "disconnect":
            return []
        present = not any(f.kind == "disconnect" for f in self._active(dev, t))
        if present == self._plugged[dev.name]:
            return []
        self._plugged[dev.name] = present
        link = Record(t, "link", {"device": dev.name, "present": present})
        # перед возвращением ссылки в атрибутах уже должно быть текущее показание
        return [self._render(dev, t), link] if present else [link]

    # ------------------------------------------------------------------ датчики
    def _active(self, dev: Device, t: float) -> list:
        return [f for f in self._faults_of[dev.name] if f.t0 <= t < f.t1]

    def _index(self, dev: Device, t: float) -> int:
        return math.floor(t / self.sc.devices[dev.name]["period_s"] + EPS)

    def _true(self, dev: Device, t: float) -> dict[str, float]:
        """Истинные значения свойств датчика (единицы SenML)."""
        if dev.model == "ads1115":
            return {"voltage0": self.physics.leak_voltage(dev.name, t)}
        temp, rh = self.physics.zone_at(dev.zone, t)
        if dev.model == "bme280":
            return {"temperature": temp, "humidity": rh, "pressure": self.physics.pressure(t)}
        if dev.model == "dht11":
            return {"temperature": temp, "humidity": rh}
        return {MODELS[dev.model].props[0]: temp}

    def _measure(self, dev: Device, t: float, values: dict[str, float], active: list) -> dict[str, float]:
        """Измеренные значения: истина + шум датчика + отказы spike, drift, noise_burst, por85."""
        model, k = MODELS[dev.model], self._index(dev, t)
        out = {}
        for p in model.props:
            mine = [f for f in active if f.prop == p]
            sigma = math.hypot(model.noise[p], *(f.params["sigma"] for f in mine if f.kind == "noise_burst"))
            out[p] = (values[p] + sigma * gauss(self.seed, dev.name, p, k)
                      + sum(f.params["amplitude"] for f in mine if f.kind == "spike")
                      + sum(f.params["per_h"] * (t - f.t0) / 3600 for f in mine if f.kind == "drift"))
        if any(f.kind == "por85" for f in active):
            out["temperature"] = 85.0   # значение регистра DS18B20 после сброса питания
        return out

    def _read_fails(self, dev: Device, t: float, active: list) -> bool:
        """Ошибка чтения: отказ read_error или сбой обмена DHT11 с вероятностью fail_rate (dht_fail_rate)."""
        if any(f.kind == "read_error" for f in active):
            return True
        if dev.model != "dht11":
            return False
        p = max([self.sc.devices[dev.name]["fail_rate"], *(f.params["p"] for f in active if f.kind == "dht_fail_rate")])
        return _uniforms(self.seed, (dev.name, "fail", self._index(dev, t)))[0] < p

    def _render(self, dev: Device, t: float) -> Record:
        """Атрибуты датчика в момент t ({атрибут: текст или None — ошибка чтения}), показания и истина."""
        model, active = MODELS[dev.model], self._active(dev, t)
        values = self._true(dev, t)
        stuck = next((f for f in active if f.kind == "stuck"), None)
        attrs: dict[str, str | None] = model.encode(
            self._frozen[stuck.id] if stuck else self._measure(dev, t, values, active))
        if self._read_fails(dev, t, active):
            attrs, readings = dict.fromkeys(attrs, model.unreadable), dict.fromkeys(model.props)
        else:
            readings = model.read(attrs)
        return Record(t, "render", {"device": dev.name, "attrs": attrs, "readings": readings,
                                    "values": {p: round(v, 6) for p, v in values.items()}})

    def _edges(self, d: Device) -> list[Edge]:
        p, rng = self.sc.devices[d.name], random.Random(f"{self.seed}:{d.name}")
        mine = [e for e in self.sc.events if e.get("device") == d.name]
        if d.model == "pir":
            motions = [(e["t"], e["t"] + e["duration_s"], e.get("rate_hz", p["rate_hz"])) for e in mine]
            return pir_edges(p, motions, self.sc.duration_s, rng)
        bounce = [(f.t0, f.t1, f.params["ms"], f.params["pulses"]) for f in self._faults_of[d.name]
                  if f.kind == "bounce"]
        return reed_edges(p, [(e["t"], e["t"] + e["duration_s"]) for e in mine], bounce, rng)

    @staticmethod
    def _gpio(dev: Device, level: bool, bounce: bool) -> dict:
        return {"device": dev.name, "line": dev.line, "value": level, "bounce": bounce}

    def _snapshot(self, t: float) -> dict:
        """Истинное состояние среды для журнала (record truth)."""
        ph = self.physics
        zones = {}
        for name in ph.zones:
            temp, rh = ph.zone_at(name, t)
            zones[name] = {"temp_c": round(temp, 3), "rh": round(rh, 2)}
        return {"zones": zones, "outdoor_c": round(ph.outdoor_c(t), 3), "pressure_pa": round(ph.pressure(t), 1),
                "leak_v": {n: round(ph.leak_voltage(n, t), 4) for n in ph.leaks}, "load": ph.load.at(t)}

    def _scenario_records(self) -> list[Record]:
        recs = []
        for e in self.sc.events:
            data, end = {k: v for k, v in e.items() if k not in ("t", "duration_s", "note")}, e["t"] + e["duration_s"]
            recs += [Record(e["t"], "event", data | {"phase": "start", "until": end}),
                     Record(end, "event", data | {"phase": "end"})]
        for f in self.faults:
            prop = {"property": f.prop} if f.prop else {}
            data = {"id": f.id, "kind": f.kind, "device": f.device} | prop | f.params
            recs += [Record(f.t0, "fault", data | {"phase": "start", "until": f.t1}),
                     Record(f.t1, "fault", data | {"phase": "end"})]
        recs += [Record(q["t"], "question", _strip(q)) for q in self.sc.questions]
        recs += [Record(s["t"], "situation", _strip(s)) for s in self.sc.situations if "t" in s]
        return recs


def summarize(sc: Scenario, seed: int) -> dict:
    """Проиграть сценарий без ввода-вывода (реле выключено) шагами 1 с и свести: отказы, моменты ситуаций,
    число фронтов входов и диапазоны показаний."""
    world, ranges, edges = World(sc, seed), {}, Counter()
    situations: dict[str, float | None] = dict.fromkeys(s["id"] for s in sc.situations)
    t = 0.0
    while True:
        for r in world.advance(t):
            if r.type == "render":
                for p, v in r.data["readings"].items():
                    if v is not None:
                        key = f"{r.data['device']}.{p}"
                        lo, hi = ranges.get(key, (v, v))
                        ranges[key] = (min(lo, v), max(hi, v))
            elif r.type == "situation":
                situations[r.data["id"]] = r.t
            elif r.type == "gpio" and not r.data["bounce"] and not r.data.get("initial"):
                edges[r.data["device"]] += 1
        if t >= sc.duration_s:
            break
        t = min(t + 1, sc.duration_s)
    return {"scenario": sc.name, "sha256": sc.sha256, "seed": seed, "duration_s": sc.duration_s,
            "events": len(sc.events), "questions": len(sc.questions),
            "faults": [{"id": f.id, "kind": f.kind, "device": f.device, "t0": f.t0, "t1": f.t1} for f in world.faults],
            "situations": situations, "gpio_edges": dict(edges),
            "ranges": {k: list(v) for k, v in sorted(ranges.items())}}
