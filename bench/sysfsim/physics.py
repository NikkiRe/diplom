"""Физика среды симулятора: температура и влажность зон, давление, протечка, сигналы PIR и геркона.

Тепловая модель зоны — первого порядка (одна ёмкость):
    dT/dt = (T_eq(t) + ΣΔT − T)/τ + f·(T_out(t) − T)/τ_fan,   T_eq = base + k_out·(T_out − mean_out),
где ΣΔT — источники тепла сценария (и нагрузка CPU у зоны платы), f — вентилятор: вход от реле, которое
пишет hw-mcp (замкнутый контур). Влажность ведётся парциальным давлением пара e (гПа):
    de/dt = (e_base + Σe − e)/τ_rh + f·(e_out − e)/τ_fan,   RH = 100·e/e_s(T),
e_s — формула Магнуса (WMO-No. 8). Источник задаёт установившийся прирост (rise_c, rh_rise), который
дал бы при бесконечном действии. Интегрирование — точная экспонента на шаге STEP_S с входами на начало
шага, поэтому состояние на момент t не зависит от того, как часто его спрашивают.
"""
from __future__ import annotations

import bisect
import math
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

from .sensors import DEVICES

if TYPE_CHECKING:
    from .scenario import Scenario

STEP_S = 1.0
EPS = 1e-9
DAY_S = 86400.0

# Умолчания — допущения о порядке величин для жилого помещения (README, «Физика»).
ZONES = {
    "kitchen": {"base_c": 22.0, "outdoor_k": 0.1, "tau_s": 600.0, "fan_tau_s": 300.0,
                "rh": 45.0, "rh_tau_s": 900.0, "load_rise_c": 0.0},
    "bathroom": {"base_c": 23.0, "outdoor_k": 0.1, "tau_s": 900.0, "fan_tau_s": 0.0,
                 "rh": 50.0, "rh_tau_s": 600.0, "load_rise_c": 0.0},
    "boiler_pipe": {"base_c": 24.0, "outdoor_k": 0.0, "tau_s": 300.0, "fan_tau_s": 0.0,
                    "rh": 40.0, "rh_tau_s": 900.0, "load_rise_c": 0.0},
    "board": {"base_c": 45.0, "outdoor_k": 0.0, "tau_s": 60.0, "fan_tau_s": 0.0,
              "rh": 40.0, "rh_tau_s": 900.0, "load_rise_c": 25.0},
}
OUTDOOR = {"mean_c": 8.0, "amplitude_c": 4.0, "coldest": "05:00", "rh": 80.0,
           "pressure_pa": 101325.0, "pressure_amp_pa": 150.0, "pressure_period_s": 2 * DAY_S}


def clock_s(text: str) -> float:
    """«ЧЧ:ММ» → секунды от полуночи."""
    h, m = text.split(":")
    return int(h) * 3600.0 + int(m) * 60.0


def e_sat(t_c: float) -> float:
    """Давление насыщенного пара над водой, гПа (формула Магнуса, коэффициенты WMO-No. 8)."""
    return 6.112 * math.exp(17.62 * t_c / (243.12 + t_c))


def relax(x: float, target: float, rate: float, dt: float) -> float:
    """Решение dx/dt = rate·(target − x) за время dt при постоянных target и rate."""
    return target + (x - target) * math.exp(-rate * dt)


class Steps:
    """Кусочно-постоянная функция времени: значение ступени с последним началом ≤ t (до первой — 0)."""

    def __init__(self) -> None:
        self.ts: list[float] = []
        self.vs: list[float] = []

    @classmethod
    def total(cls, pulses: Iterable[tuple[float, float, float]]) -> Steps:
        """Сумма прямоугольных импульсов (начало, конец, величина)."""
        steps, level = cls(), 0.0
        for t, dv in sorted((t, dv) for t0, t1, v in pulses for t, dv in ((t0, v), (t1, -v))):
            level = round(level + dv, 9)   # сумма +v и −v — ровно ноль, а не 1e-16
            steps.set(t, level)
        return steps

    def set(self, t: float, v: float) -> None:
        """Ступень v с момента t (не раньше последней)."""
        if self.ts and t < self.ts[-1]:
            raise ValueError(f"ступень в прошлом: {t} < {self.ts[-1]}")
        if self.ts and t == self.ts[-1]:
            self.vs[-1] = v
        else:
            self.ts.append(t)
            self.vs.append(v)

    def at(self, t: float) -> float:
        i = bisect.bisect_right(self.ts, t)
        return self.vs[i - 1] if i else 0.0

    def forget(self, before: float) -> None:
        """Отбросить ступени, не влияющие на значения с момента before: вход извне не копится без предела."""
        i = bisect.bisect_right(self.ts, before) - 1
        if i > 0:
            del self.ts[:i], self.vs[:i]


@dataclass
class Zone:
    p: dict           # параметры зоны (ZONES + сценарий)
    heat: Steps       # Σ rise_c действующих источников тепла
    moisture: Steps   # Σ rh_rise действующих источников влаги
    fan: Steps        # вентилятор: 0/1 (вход)
    e_base: float     # давление пара без источников, гПа
    e_unit: float     # гПа на 1 %RH прироста при base_c
    temp: float = 0.0
    vapor: float = 0.0


@dataclass
class Leak:
    """Датчик протечки на входе АЦП: смачивание w ∈ [0, 1] растёт с τ_wet, пока есть вода, и сохнет с τ_dry."""
    p: dict
    water: Steps
    wet: float = 0.0

    def target(self, t: float) -> float:
        return 1.0 if self.water.at(t) > 0 else 0.0

    def rate(self, target: float) -> float:
        return 1 / (self.p["tau_wet_s"] if target > self.wet else self.p["tau_dry_s"])

    def voltage(self, wet: float) -> float:
        return self.p["v_dry"] + (self.p["v_wet"] - self.p["v_dry"]) * wet


class Physics:
    """Состояние среды на шаге n (момент n·STEP_S) и его значения внутри шага."""

    def __init__(self, sc: Scenario, *, external_load: bool = False):
        self.outdoor = sc.outdoor
        self._phase_s = sc.start_s - clock_s(sc.outdoor["coldest"])
        events = sc.events

        def pulses(kind: str, key: str | None, **match: str) -> Steps:
            return Steps.total((e["t"], e["t"] + e["duration_s"], e[key] if key else 1.0) for e in events
                               if e["kind"] == kind and all(e[k] == v for k, v in match.items()))

        # нагрузка CPU платы: из сценария или извне (cgroup «платы», set_load)
        self.load = Steps() if external_load else pulses("load", "level")
        self.zones: dict[str, Zone] = {}
        for name, p in sc.zones.items():
            e_ref = e_sat(p["base_c"])
            self.zones[name] = Zone(p, pulses("heat", "rise_c", zone=name), pulses("moisture", "rh_rise", zone=name),
                                    Steps(), e_base=p["rh"] / 100 * e_ref, e_unit=e_ref / 100)
        self.leaks = {d.name: Leak(sc.devices[d.name], pulses("leak", None, device=d.name))
                      for d in DEVICES if d.model == "ads1115"}
        # до начала сценария — равновесие без источников: события начинаются не раньше t = 0
        self.n = 0
        for z in self.zones.values():
            z.temp, _, z.vapor, _ = self._targets(z, -STEP_S)
        for leak in self.leaks.values():
            leak.wet = leak.target(-STEP_S)

    def outdoor_c(self, t: float) -> float:
        o = self.outdoor
        return o["mean_c"] - o["amplitude_c"] * math.cos(2 * math.pi * (t + self._phase_s) / DAY_S)

    def pressure(self, t: float) -> float:
        o = self.outdoor
        return o["pressure_pa"] + o["pressure_amp_pa"] * math.sin(2 * math.pi * t / o["pressure_period_s"])

    def advance(self, t: float, on_step: Callable[[float], None] | None = None) -> None:
        """Шагами STEP_S до последней границы шага ≤ t; on_step(граница) — после каждого шага."""
        while (self.n + 1) * STEP_S <= t + EPS:
            t0 = self.n * STEP_S
            for z in self.zones.values():
                temp, a, vapor, b = self._targets(z, t0)
                z.temp, z.vapor = relax(z.temp, temp, a, STEP_S), relax(z.vapor, vapor, b, STEP_S)
            for leak in self.leaks.values():
                target = leak.target(t0)
                leak.wet = relax(leak.wet, target, leak.rate(target), STEP_S)
            self.n += 1
            if on_step:
                on_step(self.n * STEP_S)

    def zone_at(self, name: str, t: float) -> tuple[float, float]:
        """Температура (°C) и относительная влажность (%) зоны в момент t текущего шага."""
        z, t0 = self.zones[name], self.n * STEP_S
        temp, a, vapor, b = self._targets(z, t0)
        temp, vapor = relax(z.temp, temp, a, t - t0), relax(z.vapor, vapor, b, t - t0)
        return temp, min(100.0, 100 * vapor / e_sat(temp))

    def leak_voltage(self, name: str, t: float) -> float:
        """Напряжение датчика протечки (В) в момент t текущего шага."""
        leak, t0 = self.leaks[name], self.n * STEP_S
        target = leak.target(t0)
        return leak.voltage(relax(leak.wet, target, leak.rate(target), t - t0))

    def _targets(self, z: Zone, t: float) -> tuple[float, float, float, float]:
        """Цели и скорости релаксации температуры и давления пара зоны при входах на момент t."""
        p, out_c = z.p, self.outdoor_c(t)
        fan = z.fan.at(t) / p["fan_tau_s"] if p["fan_tau_s"] else 0.0
        eq = (p["base_c"] + p["outdoor_k"] * (out_c - self.outdoor["mean_c"]) + z.heat.at(t)
              + p["load_rise_c"] * self.load.at(t))
        a, b = 1 / p["tau_s"] + fan, 1 / p["rh_tau_s"] + fan
        e_out = self.outdoor["rh"] / 100 * e_sat(out_c)
        return ((eq / p["tau_s"] + fan * out_c) / a, a,
                ((z.e_base + z.e_unit * z.moisture.at(t)) / p["rh_tau_s"] + fan * e_out) / b, b)


class Edge(NamedTuple):
    t: float
    level: bool
    bounce: bool   # переход дребезга после основного фронта


def union(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """Объединение пересекающихся интервалов."""
    out: list[tuple[float, float]] = []
    for t0, t1 in sorted(intervals):
        if out and t0 <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], t1))
        else:
            out.append((t0, t1))
    return out


def poisson(t0: float, t1: float, rate: float, rng: random.Random) -> list[float]:
    """Моменты пуассоновского потока интенсивности rate (1/с) на (t0, t1)."""
    out, t = [], t0
    while (t := t + rng.expovariate(rate)) < t1:
        out.append(t)
    return out


def pir_edges(p: dict, motions: Iterable[tuple[float, float, float]], duration: float,
              rng: random.Random) -> list[Edge]:
    """Выход PIR HC-SR501.

    Стимулы: движение в интервале motions (начало, конец, rate_hz) — вход в зону обзора в начале и далее
    пуассоновский поток rate_hz; ложные — поток false_per_h на всём сценарии. Режим H (retrigger) продлевает
    импульс до стимул + hold_s, режим L — нет. После спада выход блокирован block_s: стимулы не действуют.
    """
    stimuli = [s for t0, t1, rate in motions for s in (t0, *poisson(t0, t1, rate, rng))]
    if p["false_per_h"] > 0:
        stimuli += poisson(0.0, duration, p["false_per_h"] / 3600, rng)
    edges: list[Edge] = []
    end = 0.0
    for s in sorted(stimuli):
        if edges and edges[-1].level:
            if s < end:
                if p["retrigger"]:
                    end = s + p["hold_s"]
                continue
            edges.append(Edge(end, False, False))
        if edges and s < edges[-1].t + p["block_s"]:
            continue
        edges.append(Edge(s, True, False))
        end = s + p["hold_s"]
    if edges and edges[-1].level:
        edges.append(Edge(end, False, False))
    return edges


def reed_edges(p: dict, opened: Iterable[tuple[float, float]], bounce: list[tuple[float, float, float, int]],
               rng: random.Random) -> list[Edge]:
    """Геркон двери (уровень 1 — дверь открыта). За каждым фронтом — bounce_pulses возвратов в пределах
    bounce_ms; в окнах bounce (начало, конец, ms, pulses) — их параметры. Возвраты, не успевшие до следующего
    фронта, отбрасываются парами: переходы упорядочены, и к следующему фронту линия в уровне своего фронта."""
    fronts = [(t, level) for t0, t1 in union(opened) for t, level in ((t0, True), (t1, False))]
    edges: list[Edge] = []
    for i, (t, level) in enumerate(fronts):
        t_next = fronts[i + 1][0] if i + 1 < len(fronts) else math.inf
        ms, pulses = next(((ms, n) for b0, b1, ms, n in bounce if b0 <= t < b1),
                          (p["bounce_ms"], p["bounce_pulses"]))
        returns = [t + dt for dt in sorted(rng.uniform(0, ms / 1000) for _ in range(2 * pulses)) if t + dt < t_next]
        if len(returns) % 2:   # возврат — пара переходов: уход с уровня фронта и обратно
            returns.pop()
        edges.append(Edge(t, level, False))
        edges += [Edge(tr, level if j % 2 else not level, True) for j, tr in enumerate(returns)]
    return edges
