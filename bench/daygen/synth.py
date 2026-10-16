"""Синтетика: «дом без CASAS» для тестов и климат/котёл/протечка для любого дня.

Всё здесь — [допущения] модели нагрузки, а не данные: параметры подобраны правдоподобными и
зафиксированы, чтобы трасса воспроизводилась по seed. Реальные события датчиков движения и дверей
берутся из CASAS (casas.py); синтетический дом нужен, когда архива нет, и для быстрых тестов.
"""
from __future__ import annotations

import itertools
import math
import random

from .casas import Record

# Средние по 14 домам доли начал активностей CASAS по часам, % (research/new_topic/GAP1_artifacts/
# casas_rates_stdout.txt, «Часовой профиль act»). Используются в синтетическом режиме вместо профиля дома.
AGG_ACT_HOUR_PCT = [1.3, 1.2, 0.8, 0.8, 0.9, 1.0, 2.5, 5.8, 8.0, 7.6, 5.4, 5.6,
                    6.0, 5.4, 4.6, 4.9, 5.3, 5.6, 5.6, 5.0, 4.9, 5.1, 4.6, 2.2]

SYNTH_LOCATIONS = {"Bedroom": ["motion"], "Bathroom": ["motion"], "Kitchen": ["motion"],
                   "LivingRoom": ["motion"], "OutsideDoor": ["contact", "motion"]}

SAMPLE_PERIOD_S = 300.0          # период опроса климат-датчика и котла [допущение; снимок в агенте — 60 с]
BOILER_THRESHOLD_C = 85.0        # порог перегрева
LEAK_DURATION_S = 1800.0         # протечка держится 30 мин (трасса не реагирует на действия агента)


def _round(x: float, res: float) -> float:
    """Округление до точности датчика (детерминированно, без -0.0)."""
    v = round(round(x / res) * res, 6)
    return 0.0 if v == 0 else v


# ---------------------------------------------------------------------------
# Синтетический дом (замена трассы CASAS)
# ---------------------------------------------------------------------------


def synthetic_casas_day(rng: random.Random, date: str = "synthetic") -> list[Record]:
    """Сутки одного жителя: распорядок дня → всплески ON/OFF в комнатах, выходы через дверь.

    Активность в комнате — пуассоновские срабатывания (в среднем раз в 60 с), OFF через 2–10 с.
    Сон и дневной отдых — без движения. Метки активностей (begin/end) пишутся, как в CASAS.
    """
    out: list[Record] = []

    def burst(t0: float, t1: float, loc: str, mean_gap: float = 60.0) -> None:
        t = t0 + rng.expovariate(1 / 20)
        while t < t1:
            out.append(Record(date, round(t, 3), loc, "ON"))
            out.append(Record(date, round(min(t + rng.uniform(2, 10), t1), 3), loc, "OFF"))
            t += rng.expovariate(1 / mean_gap) + 10

    def label(t0: float, t1: float, loc: str, name: str) -> None:
        out.append(Record(date, round(t0, 3), loc, "ON", f"{name}:begin"))
        out.append(Record(date, round(t1, 3), loc, "OFF", f"{name}:end"))

    def door(t: float) -> float:
        out.append(Record(date, round(t, 3), "OutsideDoor", "ON"))
        out.append(Record(date, round(t + 2, 3), "OutsideDoor", "OPEN"))
        close = t + 2 + rng.uniform(8, 25)
        out.append(Record(date, round(close, 3), "OutsideDoor", "CLOSE"))
        out.append(Record(date, round(close + 3, 3), "OutsideDoor", "OFF"))
        return close + 5

    t = 0.0
    wake = min(max(rng.gauss(6.75 * 3600, 1800), 5.5 * 3600), 8.5 * 3600)
    # ночь: редкие движения в спальне
    burst(t, wake, "Bedroom", mean_gap=3600)
    t = wake
    plan = [("Bathroom", "Personal_Hygiene", 10, 20), ("Kitchen", "Cook_Breakfast", 15, 30),
            ("Kitchen", "Eat_Breakfast", 10, 25), ("LivingRoom", "Relax", 40, 120)]
    if rng.random() < 0.6:
        plan.append(("OUT", "Leave_Home", 60, 180))
    plan += [("Kitchen", "Cook_Lunch", 15, 35), ("Kitchen", "Eat_Lunch", 15, 30),
             ("Bedroom", "NAP", 30, 90), ("LivingRoom", "Watch_TV", 60, 150),
             ("Bathroom", "Toilet", 3, 8), ("Kitchen", "Cook_Dinner", 20, 45),
             ("Kitchen", "Eat_Dinner", 15, 30), ("LivingRoom", "Watch_TV", 60, 120),
             ("Bathroom", "Personal_Hygiene", 10, 20)]
    for loc, name, lo, hi in plan:
        dur = rng.uniform(lo, hi) * 60
        if loc == "OUT":
            t = door(t)
            back = t + dur
            out.append(Record(date, round(back, 3), "OutsideDoor", "OPEN", "Enter_Home:begin"))
            out.append(Record(date, round(back + 15, 3), "OutsideDoor", "CLOSE", "Enter_Home:end"))
            t = back + 20
            continue
        if name == "NAP":
            label(t, t + dur, "Bedroom", "Sleep")
            t += dur
            continue
        label(t, t + dur, loc, name)
        burst(t + 1, t + dur - 1, loc)
        t += dur + rng.uniform(10, 60)
    # вечер до сна (около 23:00 ± 30 мин) и ночь
    bed = max(t + 600, min(rng.gauss(23 * 3600, 1800), 23.9 * 3600))
    burst(t, bed, "LivingRoom")
    label(bed, min(bed + 3600, 86399), "Bedroom", "Sleep")
    burst(bed, 86399, "Bedroom", mean_gap=3600)
    out = [r for r in out if 0 <= r.t < 86400]
    out.sort(key=lambda r: (r.t, r.sensor, str(r.value)))
    return out


def synthetic_hour_weights() -> list[float]:
    s = sum(AGG_ACT_HOUR_PCT)
    return [round(x / s, 6) for x in AGG_ACT_HOUR_PCT]


# ---------------------------------------------------------------------------
# Климат, котёл, протечка
# ---------------------------------------------------------------------------


def climate_series(rng: random.Random, t0: float, t1: float) -> list[tuple[float, dict[str, float]]]:
    """Отсчёты BME280 каждые SAMPLE_PERIOD_S в [t0; t1): суточная синусоида + AR(1)-шум (до округления)."""
    base = rng.uniform(20.5, 23.0)
    amp = rng.uniform(0.8, 1.8)
    hum0 = rng.uniform(38, 52)
    pres = rng.uniform(99500, 102500)
    noise = 0.0
    out = []
    t = t0
    while t < t1:
        noise = 0.8 * noise + rng.gauss(0, 0.08)
        temp = base + amp * math.sin(2 * math.pi * (t / 3600 - 10) / 24) + noise
        hum = hum0 - 2.0 * (temp - base) + rng.gauss(0, 0.3)
        pres += rng.gauss(0, 15)
        out.append((t, {"temperature": temp, "humidity": hum, "pressure": pres}))
        t += SAMPLE_PERIOD_S
    return out


def boiler_series(rng: random.Random, t0: float, t1: float,
                  overheats: list[float]) -> tuple[list[tuple[float, float]], list[float]]:
    """Температура подачи котла: циклы нагрева 90 мин около 60 °C; эпизоды перегрева в моменты overheats.

    Эпизод: рост до ≈92 °C за 10 мин (отсчёт раз в 60 с), плато 20 мин, спад 15 мин.
    Возвращает (отсчёты, моменты первого пересечения порога BOILER_THRESHOLD_C).
    """
    phase = rng.uniform(0, 5400)
    peak = rng.uniform(91, 94)

    def normal(t: float) -> float:
        return 60 + 4 * math.sin(2 * math.pi * (t + phase) / 5400)

    times = []
    t = t0
    while t < t1:
        times.append(t)
        t += SAMPLE_PERIOD_S
    episodes = []
    for s in overheats:
        dense = [s + 60 * i for i in range(46)]   # 45 мин с отсчётом раз в минуту
        times += dense
        episodes.append(s)
    times = sorted({round(x, 3) for x in times if t0 <= x < t1})
    out = []
    crossings = []
    for t in times:
        v = normal(t) + rng.gauss(0, 0.15)
        for s in episodes:
            dt = t - s
            if 0 <= dt <= 600:
                v = normal(s) + (peak - normal(s)) * dt / 600
            elif 600 < dt <= 1800:
                v = peak + rng.gauss(0, 0.3)
            elif 1800 < dt <= 2700:
                v = peak + (normal(t) - peak) * (dt - 1800) / 900
        out.append((t, v))
    for s in episodes:
        first = next((t for t, v in out if t >= s and _round(v, 0.1) >= BOILER_THRESHOLD_C), None)
        if first is not None:
            crossings.append(first)
    return out, crossings


def pick_episode_times(rng: random.Random, n: int, t0: float, t1: float, margin: float = 1800.0,
                       tail: float = 3600.0, min_gap: float = 3600.0) -> list[float]:
    """n моментов начала эпизодов в [t0 + margin; t1 − margin − tail] с попарным расстоянием ≥ min_gap.

    В коротком окне (интеграционный день на 2 ч) поля и зазор уменьшаются пропорционально: margin ≤ окно/8,
    tail ≤ окно/4, min_gap ≤ длина диапазона/n. Для окна 07–23 это ровно 30 мин, 1 ч и 1 ч.
    """
    if n <= 0:
        return []
    span = t1 - t0
    margin, tail = min(margin, span / 8), min(tail, span / 4)
    lo, hi = t0 + margin, t1 - margin - tail
    if hi <= lo:
        raise ValueError(f"окно {span:.0f} с слишком короткое для {n} синтетических ситуаций")
    min_gap = min(min_gap, (hi - lo) / n)
    for _ in range(1000):
        ts = sorted(round(rng.uniform(lo, hi), 1) for _ in range(n))
        if all(b - a >= min_gap for a, b in itertools.pairwise(ts)):
            return ts
    # слишком тесно: равномерная сетка
    step = (hi - lo) / n
    return [round(lo + step * (i + 0.5), 1) for i in range(n)]


def quantize_series(series: list[tuple[float, float]], res: float) -> list[tuple[float, float]]:
    """Отсчёты -> события «значение изменилось» после округления до res."""
    out = []
    last = None
    for t, v in series:
        q = _round(v, res)
        if q != last:
            out.append((t, q))
            last = q
    return out
