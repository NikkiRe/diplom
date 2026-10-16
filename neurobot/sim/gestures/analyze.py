"""Разбор записи наблюдателя (record.py) против истины gesturegen — без ROS.

По каждому сегменту истины: t0 → t_rec → t_pub → t_cmd → t_motion и реакция:
  correct     — команды /cmd_vel сложились в ожидаемый манёвр (таблица рефлекса и манёвров ниже), а для
                класса без действия («none» в таблице рефлекса) — движения не было;
  wrong       — другой манёвр, нераспознанный рисунок команд или движение там, где надо стоять;
  interrupted — начало ожидаемого манёвра, оборванное (видимый перезапуск по PLAN §1);
  none        — реакции не было в окне match_window_s;
  unobserved  — реакция видна не целиком: окно сегмента или эпизод движения выходит за сессию записи, или начала
                реакции не видно (перерыв наблюдателя или соединение подписок после t0).
Эпизод движения — подряд идущие ненулевые серии /cmd_vel с разрывами ≤ episode_gap_s; он относится к
последнему сегменту, начавшемуся до него, если начался не позже t0 + match_window_s, иначе это
«самопроизвольное» действие. Эпизод на отрицательном сегменте — ложное действие. Сообщения
/neurobot/gesture относятся к сегментам так же; распознавание сегмента (rec, матрица ошибок, метки rec и pub) —
по решающему сообщению: последнему до начала эпизода движения, а без движения — последнему в сегменте
(perception может разрезать длинный жест на несколько сообщений).

«Едет без свежей команды» (K6) — робот едет, а последняя ненулевая команда старше stale_s. Такой интервал
делится по явному нулю писателя: нуля не было — writer silent (писатель замолчал: S1 у демо, отказ самого
стража); ноль был, и база встала не позже brake_allow_s после него — торможение (не нарушение); ноль был, а
база едет дольше — база игнорирует ноль. «Последняя ненулевая → остановка» считается только по настоящей остановке
базы; молчание писателя, на котором база ехала до конца сессии записи, — отдельный счётчик «не встала до конца
записи».

Сессия записи — один запуск наблюдателя (рестарт с --append начинает новую): от заголовка до события stop. Между
сессиями наблюдения нет, поэтому всё, что тянется через её границу, обрывается границей, а не переносится в
следующую сессию: состояния «едет», «касается», fps = 0 закрываются концом сессии, эпизод у границы — «не виден
целиком» (unobserved), перерыв не входит ни в «сообщений нет», ни во время записи (K7 в час). Первые connect_s
сессии подписки соединяются и команд до неё не видно: эпизод с первой команды темы за это время (эпизод без начала)
мог начаться до сессии, а K6 до первой команды писателя не судится. «Сообщений нет» (health, probe) судится с начала
сессии + connect_s, но не раньше первого сообщения темы в прогоне (прогрев perception — не глухота): тишина от этого
момента до первого сообщения сессии входит, а сессия без сообщений — целиком. Эпизод без начала до t0 сегмента —
продолжение чужого манёвра, не действие: реакция сегмента — его первое действие, а продолжение судится (как
unobserved), только если других эпизодов у сегмента нет. Начало реакции видно, если это эпизод с началом и его сессия
началась не позже t0; иначе реакция могла начаться в перерыве наблюдателя (сессия после t0) или пока соединялись
подписки (эпизод без начала после t0), и сегмент — unobserved без меток и решающего сообщения. Метка дополнительной
темы (lat_extra) — по тому же правилу для её эпизодов в окне сегмента.

Все сопоставления — по CLOCK_MONOTONIC ВМ sim (поле m событий и t_mono якоря); метки t_rec/t_end из
сообщений perception (time.time()) переводятся по смещению t − m события приёма. Задержки и точность
стенда — ориентир, счётчики (ложные действия, эпизоды, глухие интервалы, касания) — результат прогона.

  python -m neurobot.sim.gestures.analyze --events events.jsonl --truth truth.jsonl --anchor anchor.json \\
      [--probe probe.jsonl] [--out summary.json] [--per-gesture gestures.jsonl]
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
from dataclasses import asdict, dataclass, field, fields

import numpy as np

from harness.stats import block_bootstrap_mean

from .clock import read_anchor
from .timeline import dumps, read_jsonl

LABEL = "стенд: задержки и точность — ориентир; счётчики — результат прогона"
# Манёвры демо (predict_live.py:398–454): буква — знак команды (F/B — linear.x, L/R — angular.z), секунды.
MANEUVERS = {
    "turn180": [["R", 7.0]],
    "come": [["F", 3.0]],
    "back": [["B", 3.0]],
    "go_left": [["L", 3.0], ["F", 2.0], ["R", 3.0]],
    "go_right": [["R", 3.0], ["F", 2.0], ["L", 3.0]],
    "think": [["R", 2.0], ["F", 2.0], ["L", 5.0], ["F", 2.0]],
}
# Класс (и сторона) → манёвр, как в predict_live.py:647–656; "none" (или null) — класс без действия.
REFLEX = {"0": "turn180", "1": "come", "2:left": "go_left", "2:right": "go_right", "3": "think"}
STAND = "none"
NOT_DEAF_STATES = ("paused", "moving")
MSG_SKEW_S = 0.1   # сообщение жеста и первая команда могут прийти к наблюдателю в обратном порядке
K6_CAUSES = ("silent", "ignored_zero", "braking_after_zero")


def _opt(default, help_: str):
    return field(default=default, metadata={"help": help_})


@dataclass
class AnalyzeConfig:
    cmd_topic: str = _opt("/cmd_vel", "тема единственного писателя скоростей")
    episode_gap_s: float = _opt(0.6, "разрыв между сериями команд внутри одного эпизода движения, с")
    min_phase_s: float = _opt(0.25, "фаза манёвра короче — переходный тик, с")
    match_window_s: float = _opt(10.0, "реакция относится к сегменту, если началась не позже t0 + окно, с")
    sla_s: float = _opt(3.0, "реакция позже t0 + sla — видимый сбой (PLAN §1), с")
    stale_s: float = _opt(0.5, "команда свежая столько после последней ненулевой (K6), с")
    brake_allow_s: float = _opt(0.6, "после явного нуля база встаёт не позже, иначе игнорирует ноль, с")
    connect_s: float = _opt(2.0, "начало сессии записи, пока соединяются подписки: эпизод с первой команды темы мог "
                                 "начаться до сессии, K6 до первой команды писателя и «сообщений нет» не судятся, с")
    health_gap_s: float = _opt(3.0, "тишина perception_health или probe дольше — сообщений нет, с")
    deaf_min_s: float = _opt(2.0, "fps = 0 короче — не глухота, с")
    rtf_min: float = _opt(0.95, "жесты в окне с RTF ниже не входят в задержки")
    dur_tol_abs: float = _opt(0.4, "допуск длительности фазы манёвра, с")
    dur_tol_rel: float = _opt(0.15, "допуск длительности фазы манёвра, доля")
    maneuvers: dict = field(default_factory=lambda: dict(MANEUVERS))
    reflex: dict = field(default_factory=lambda: dict(REFLEX))


FLAGS = tuple(f for f in fields(AnalyzeConfig) if "help" in f.metadata)


# ---------------------------------------------------------------------------
# Интервалы
# ---------------------------------------------------------------------------

def union(iv) -> list:
    out = []
    for a, b in sorted(iv):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def subtract(a, b) -> list:
    out = []
    cuts = union(b)
    for s, e in union(a):
        cur = s
        for cs, ce in cuts:
            if ce <= cur or cs >= e:
                continue
            if cs > cur:
                out.append((cur, cs))
            cur = max(cur, ce)
        if cur < e:
            out.append((cur, e))
    return out


def iv_stats(iv) -> dict:
    d = [b - a for a, b in iv]
    return {"n": len(d), "total_s": round(sum(d), 3), "max_s": round(max(d), 3) if d else 0.0}


def _covers(iv, m: float) -> bool:
    return any(a < m < b for a, b in iv)


def _intervals(events: list, on, end_m: float) -> list:
    """Интервалы, когда on(событие) истинно, по событиям-переходам (время m); незакрытый — до end_m."""
    out, start = [], None
    for e in sorted(events, key=lambda e: e["m"]):
        if on(e) and start is None:
            start = e["m"]
        elif not on(e) and start is not None:
            out.append((start, e["m"]))
            start = None
    if start is not None:
        out.append((start, max(end_m, start)))
    return out


# ---------------------------------------------------------------------------
# Эпизоды движения и манёвры
# ---------------------------------------------------------------------------

def letter(x: float, z: float) -> str:
    if x:
        return "F" if x > 0 else "B"
    if z:
        return "L" if z > 0 else "R"
    return ""


def episodes(runs: list, gap_s: float) -> list:
    """Группы ненулевых серий команд: [{start, end, runs}]."""
    out = []
    for r in sorted((r for r in runs if letter(r["x"], r["z"])), key=lambda r: r["m0"]):
        if out and r["m0"] - out[-1]["end"] <= gap_s:
            out[-1]["runs"].append(r)
            out[-1]["end"] = max(out[-1]["end"], r["m1"])
        else:
            out.append({"start": r["m0"], "end": r["m1"], "runs": [r]})
    return out


def phases(ep: dict, min_phase_s: float) -> list:
    """[(буква, длительность)]: длительность — от начала фазы до начала следующей; короткая фаза
    (переходный тик демо, где x и th меняются не одновременно) отдаёт своё время предыдущей, соседние
    одинаковые фазы сливаются."""
    raw = []
    for r in ep["runs"]:
        L = letter(r["x"], r["z"])
        if raw and raw[-1][0] == L:
            raw[-1][2] = r["m1"]
            raw[-1][3] += r["n"]
        else:
            raw.append([L, r["m0"], r["m1"], r["n"]])
    out = []
    for i, (L, s, e, n) in enumerate(raw):
        if i + 1 < len(raw):
            dur = raw[i + 1][1] - s
        else:
            dur = e - s + ((e - s) / (n - 1) if n > 1 else 0.0)
        if dur < min_phase_s:
            if out:
                out[-1][1] += dur
            continue
        if out and out[-1][0] == L:
            out[-1][1] += dur
        else:
            out.append([L, dur])
    return [(L, round(d, 3)) for L, d in out]


def _dur_ok(obs: float, exp, cfg: AnalyzeConfig) -> bool:
    return exp is None or abs(obs - exp) <= max(cfg.dur_tol_abs, cfg.dur_tol_rel * exp)


def match(ph: list, steps: list, cfg: AnalyzeConfig):
    """"full" — рисунок команд совпал с манёвром; "prefix" — его оборванное начало; None — не он."""
    n = len(ph)
    if n == 0 or n > len(steps):
        return None
    for i, (L, d) in enumerate(ph):
        if L != steps[i][0] or (i < n - 1 and not _dur_ok(d, steps[i][1], cfg)):
            return None
    exp = steps[n - 1][1]
    if n == len(steps) and _dur_ok(ph[-1][1], exp, cfg):
        return "full"
    if exp is None or ph[-1][1] <= exp + max(cfg.dur_tol_abs, cfg.dur_tol_rel * exp):
        return "prefix"
    return None


def judge(ph: list, expected, cfg: AnalyzeConfig, requested=None):
    """(реакция, манёвр) для эпизода при ожидаемом манёвре expected (None — надо стоять). requested — манёвр,
    которого просило сообщение perception: им называется рисунок, подходящий к нескольким манёврам (оборванное
    начало); без него — все подходящие через «|»."""
    res = {name: match(ph, steps, cfg) for name, steps in sorted(cfg.maneuvers.items())}
    full = [n for n, r in res.items() if r == "full"]
    prefix = [n for n, r in res.items() if r == "prefix"]
    if expected in full:
        return "correct", expected
    if full:
        return "wrong", requested if requested in full else full[0]
    if expected in prefix:
        return "interrupted", expected
    if prefix:
        return "wrong", "partial:" + (requested if requested in prefix else "|".join(prefix))
    return "wrong", "unknown:" + "".join(L for L, _ in ph)


def expected_maneuver(reflex: dict, seg: dict):
    """Манёвр по таблице рефлекса для сегмента-жеста или None (класс без действия); класса нет — ValueError."""
    for key in ("%d:%s" % (seg["cls"], seg["side"]), str(seg["cls"])):
        if key in reflex:
            name = reflex[key]
            return None if name in (None, STAND) else name
    raise ValueError("в таблице рефлекса нет класса %d (сторона %s)" % (seg["cls"], seg["side"]))


def requested_maneuver(reflex: dict, msg: dict):
    """Манёвр, которого по таблице рефлекса просит сообщение perception, или None («не распознано», класс без
    действия или класса нет в таблице)."""
    cls = msg.get("cls")
    if not isinstance(cls, int) or cls < 0:
        return None
    try:
        return expected_maneuver(reflex, {"cls": cls, "side": msg.get("side")})
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Статистика задержек (ориентир)
# ---------------------------------------------------------------------------

def quantile_ci(sorted_v, q: float, conf: float = 0.95):
    """Ранговый ДИ квантиля q (Conover, Practical Nonparametric Statistics, 1999, §3.2): порядковые
    статистики X(l) ≤ ξ_q ≤ X(u) с вероятностью ≥ conf при независимых наблюдениях (жесты разделены паузами);
    None — выборка мала для такого квантиля."""
    n = len(sorted_v)
    alpha = (1.0 - conf) / 2
    cdf, acc = [], 0.0
    for i in range(n + 1):                        # функция распределения Bin(n, q)
        acc += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                        + i * math.log(q) + (n - i) * math.log1p(-q))
        cdf.append(acc)
    lo = max((k for k in range(1, n + 1) if cdf[k - 1] <= alpha), default=None)
    hi = min((k for k in range(1, n + 1) if cdf[k - 1] >= 1.0 - alpha), default=None)
    if lo is None or hi is None:
        return None
    return [round(float(sorted_v[lo - 1]), 4), round(float(sorted_v[hi - 1]), 4)]


def latency_stats(values: list) -> dict:
    """values — в порядке времени (для блочного бутстрепа среднего). ДИ — при n ≥ 20."""
    if not values:
        return {"n": 0}
    v = np.asarray(values, dtype=float)
    p50, p95, p99 = (float(x) for x in np.quantile(v, [0.5, 0.95, 0.99]))
    out = {"n": int(v.size), "p50": round(p50, 4), "p95": round(p95, 4), "p99": round(p99, 4),
           "mean": round(float(v.mean()), 4), "max": round(float(v.max()), 4)}
    if v.size >= 20:
        mean_ci = block_bootstrap_mean(v, n_boot=2000)
        s = np.sort(v)
        out["ci95"] = {"mean": [round(mean_ci["ci_low"], 4), round(mean_ci["ci_high"], 4)],
                       "p50": quantile_ci(s, 0.5), "p95": quantile_ci(s, 0.95),
                       "method": "среднее — кольцевой блочный бутстреп (harness/stats.py); квантили — ранговый ДИ"}
    return out


# ---------------------------------------------------------------------------
# Разбор
# ---------------------------------------------------------------------------

def _assign(times: list, segs: list, window_s: float) -> list:
    """Индекс сегмента для каждого момента: последний начавшийся, если окно от его t0 не истекло; иначе None."""
    starts = [s["_m_start"] for s in segs]
    out = []
    for m in times:
        k = bisect.bisect_right(starts, m) - 1
        out.append(k if k >= 0 and m - segs[k]["_m0"] <= window_s else None)
    return out


def _group(items: list, idx: list) -> dict:
    out = {}
    for it, k in zip(items, idx):
        if k is not None:
            out.setdefault(k, []).append(it)
    return out


def _first_between(sorted_times: list, lo: float, hi: float):
    i = bisect.bisect_left(sorted_times, lo)
    return sorted_times[i] if i < len(sorted_times) and sorted_times[i] <= hi else None


def _first_action(eps: list, m0: float) -> tuple:
    """(первое действие сегмента или None, видно ли его начало) по эпизодам одной темы в окне сегмента (по времени).
    Эпизод без начала (cut_start: первая команда темы в первые connect_s сессии) до t0 + MSG_SKEW_S — продолжение
    чужого манёвра, не действие. После t0 он может быть и реакцией, начавшейся, пока подписки соединялись, — начала не
    видно. Не видно начала и у эпизода из сессии, начавшейся после t0: реакция могла начаться в перерыве наблюдателя,
    а видимый эпизод — оказаться повтором."""
    first = next((e for e in eps if not e["cut_start"] or e["start"] > m0 + MSG_SKEW_S), None)
    return first, first is not None and not first["cut_start"] and first["span"][0] <= m0


def _rtf_at(rtf: list, m: float):
    vals = [e["rtf"] for e in rtf if e["m"] - e["wall_s"] <= m <= e["m"]]
    return min(vals) if vals else None


def no_fresh_cmd(moving: list, runs: list, cfg: AnalyzeConfig, open_end=None) -> list:
    """Движение без свежей ненулевой команды писателя в одной сессии записи: [{a, b, cause, last_cmd, stop, to_end}],
    cause — silent (нуля после последней ненулевой не было), braking_after_zero (ноль был, база встала за
    brake_allow_s) или ignored_zero (ноль был, база едет дольше); ноль, пришедший позже stale_s, делит интервал на
    silent и остальное. open_end — конец сессии, если последний интервал движения закрыт им (база ещё ехала), иначе
    None. Как кончился интервал движения без свежей команды: stop — момент, когда база встала; to_end — конец сессии,
    если база ехала до него; оба None — пришла свежая команда."""
    nonzero = [r for r in runs if letter(r["x"], r["z"])]
    fresh = [(r["m0"], r["m1"] + cfg.stale_s) for r in nonzero]
    last_ends = sorted(r["m1"] for r in nonzero)
    zeros = sorted(r["m0"] for r in runs if not letter(r["x"], r["z"]))
    ends = {b for _, b in union(moving)}
    out = []
    for a, b in subtract(moving, fresh):
        i = bisect.bisect_right(last_ends, a) - 1
        to_end = b if b == open_end else None
        item = {"last_cmd": last_ends[i] if i >= 0 else None, "stop": b if b in ends and to_end is None else None,
                "to_end": to_end}
        z = _first_between(zeros, -math.inf if item["last_cmd"] is None else item["last_cmd"], b)
        if z is None or z >= b:
            out.append(dict(item, a=a, b=b, cause="silent"))
            continue
        if z > a:
            out.append(dict(item, a=a, b=z, cause="silent"))
            a = z
        out.append(dict(item, a=a, b=b, cause="braking_after_zero" if b <= z + cfg.brake_allow_s else "ignored_zero"))
    return out


def _by_cause(found: list) -> dict:
    out = {c: iv_stats([(u["a"], u["b"]) for u in found if u["cause"] == c]) for c in K6_CAUSES}
    out["not_stopped_by_end"] = iv_stats([(u["a"], u["to_end"]) for u in found
                                          if u["cause"] == "silent" and u["to_end"] is not None])
    return out


def _health(health: list, end_m: float) -> dict:
    msgs = sorted((e for e in health if "msg" in e), key=lambda e: e["m"])
    deaf = _intervals(msgs, lambda e: e["msg"].get("fps") == 0 and e["msg"].get("state") not in NOT_DEAF_STATES,
                      msgs[-1]["m"] if msgs else end_m)
    return {"m": [e["m"] for e in msgs], "bad": sum(1 for e in health if "msg" not in e), "deaf": deaf}


def _missing(times: list, listened: list, gap_s: float) -> list:
    """«Сообщений нет» дольше gap_s: times — моменты сообщений по сессиям записи, listened — [(с какого момента сессии
    сообщения уже должны идти, конец сессии)]. Тишина — между сообщениями, от начала суда до первого сообщения и от
    последнего до конца сессии, а в сессии без сообщений — весь суд. Суд начинается не раньше первого сообщения темы в
    прогоне: до него тема ещё не звучала (прогрев), и тишина — не глухота. Прогон без единого сообщения — темы нет, и
    тишины нет."""
    heard = [m for ms in times for m in ms]
    if not heard:
        return []
    first = min(heard)
    out = []
    for ms, (lo, end) in zip(times, listened):
        lo = max(lo, first)
        pts = sorted(ms)
        if not pts or pts[0] > lo:
            pts.insert(0, lo)
        pts.append(end)
        out += [(a, b) for a, b in zip(pts, pts[1:]) if b - a > gap_s]
    return out


def _probe(groups: list, listened: list, cfg: AnalyzeConfig) -> dict:
    """groups — строки журнала по сессиям записи (listened — как у _missing): перерыв наблюдателя — не тишина
    журнала."""
    stalled, times, pids, faults, events, timeouts = [], [], set(), {}, {}, 0
    for lines in groups:
        counters = sorted((p for p in lines if "ev" not in p), key=lambda p: p["m"])
        by_pid = {}
        for p in counters:
            by_pid.setdefault(p["pid"], []).append(p)
        for seq in by_pid.values():
            stalled += [(a["m"], b["m"]) for a, b in zip(seq, seq[1:]) if b["det_calls"] == a["det_calls"]]
        times.append([p["m"] for p in counters])
        pids.update(by_pid)
        timeouts = max([timeouts] + [p["timeouts"] for p in counters])
        for p in lines:
            if p.get("ev") == "fault_start":
                faults[p["kind"]] = faults.get(p["kind"], 0) + 1
            elif "ev" in p and p["ev"] not in ("fault_end", "pipeline_start", "pipeline_stop"):
                events[p["ev"]] = events.get(p["ev"], 0) + 1
    return {"deaf": [iv for iv in union(stalled) if iv[1] - iv[0] >= cfg.deaf_min_s],
            "missing": _missing(times, listened, cfg.health_gap_s), "pids": len(pids), "fault_starts": faults,
            "events": events, "timeouts": timeouts}


def _span(events: list) -> tuple:
    """(начало, конец) сессии записи — первое (start — момент заголовка) и последнее (обычно stop) событие."""
    return (min(e.get("m0", e.get("m", math.inf)) for e in events),
            max(e.get("m1", e.get("m", -math.inf)) for e in events))


def _session(events: list, cfg: AnalyzeConfig) -> dict:
    """Что видно в одной сессии записи. Трекеры record.py каждую сессию начинают с «стоит» и «не касается», а
    незакрытое к концу сессии закрывается её концом: дальше наблюдения нет.
    Эпизод движения: cut_start — начался с первой команды темы в сессии не позже connect_s от начала сессии (мог
    начаться до неё), cut_end — сессия кончилась раньше, чем через episode_gap_s после его последней команды (мог
    продолжиться). K6 судится с первой команды писателя в сессии, но не позже connect_s от её начала."""
    start, end = _span(events)
    by = {}
    for e in events:
        by.setdefault(e["ev"], []).append(e)
    by_topic = {}
    for r in by.get("cmd", []):
        by_topic.setdefault(r["topic"], []).append(r)
    eps = {}
    for topic, runs in by_topic.items():
        first = min(r["m0"] for r in runs)
        eps[topic] = [dict(ep, span=(start, end), cut_end=ep["end"] + cfg.episode_gap_s > end,
                           cut_start=ep["start"] == first and ep["start"] - start <= cfg.connect_s)
                      for ep in episodes(runs, cfg.episode_gap_s)]
    my_runs = by_topic.get(cfg.cmd_topic, [])
    motion = sorted(by.get("motion", []), key=lambda e: e["m"])
    moving = {src: _intervals([e for e in motion if e["src"] == src], lambda e: e["moving"], end)
              for src in sorted({e["src"] for e in motion})}
    moving_any = union(iv for v in moving.values() for iv in v)
    # Последнее событие источника в сессии — «едет»: его интервал закрыт концом сессии, а не остановкой базы.
    open_end = {e["src"]: end if e["moving"] else None for e in motion}
    blind = [(start, min([start + cfg.connect_s] + [r["m0"] for r in my_runs]))]
    judged = subtract(moving_any, blind)
    any_open = any(v is not None for v in open_end.values())
    return {"span": (start, end), "eps": eps, "moving_any": moving_any,
            "unsafe": no_fresh_cmd(judged, my_runs, cfg, end if any_open else None),
            "unsafe_src": {src: no_fresh_cmd(subtract(ivs, blind), my_runs, cfg, open_end[src])
                           for src, ivs in moving.items()},
            "touch": _intervals(by.get("wall", []), lambda e: e["touch"], end),
            "health": _health(by.get("health", []), end),
            "moving_without_cmd": bool(judged) and not my_runs}


def analyze(sessions: list, segments: list, anchor, probe_lines=None, cfg: AnalyzeConfig = None):
    """(записи по сегментам, сводка). sessions — события по сессиям записи одного прогона (запускам наблюдателя);
    начало сессии — её событие start (момент заголовка, select_sessions), а без него — первое событие. Строки probe
    вне сессий записи не учитываются."""
    cfg = cfg or AnalyzeConfig()
    sessions = sorted((evs for evs in sessions if evs), key=lambda evs: _span(evs)[0])
    if not sessions:
        raise ValueError("в записи наблюдателя нет событий")
    parts = [_session(evs, cfg) for evs in sessions]
    spans = [p["span"] for p in parts]
    # Тишина perception в сессии — с connect_s от её начала (подписки соединяются); прогрев отсекает _missing.
    listened = [(a + cfg.connect_s, b) for a, b in spans]
    by = {}
    for evs in sessions:
        for e in evs:
            by.setdefault(e["ev"], []).append(e)

    runs = by.get("cmd", [])
    eps = sorted((ep for p in parts for ep in p["eps"].get(cfg.cmd_topic, [])), key=lambda ep: ep["start"])
    moving_any = union(iv for p in parts for iv in p["moving_any"])
    motion_starts = [a for a, _ in moving_any]
    rtf = by.get("rtf", [])

    segs = sorted((dict(s, _m_start=anchor.mono_of(s["t_start"]), _m0=anchor.mono_of(s["t0"])) for s in segments),
                  key=lambda s: s["_m_start"])
    ep_seg = _assign([ep["start"] for ep in eps], segs, cfg.match_window_s)
    msgs, seen = [], set()
    for e in sorted(by.get("gesture", []), key=lambda e: e["m"]):
        key = (e.get("msg", {}).get("boot"), e.get("msg", {}).get("seq"))
        if "msg" not in e or (key[1] is not None and key in seen):
            continue
        seen.add(key)
        msgs.append(e)
    msg_seg = _assign([e["m"] for e in msgs], segs, cfg.match_window_s)
    eps_by_seg = _group(eps, ep_seg)
    msgs_by_seg = _group(msgs, msg_seg)
    # Эпизоды дополнительных тем по сегментам — по тому же правилу, что эпизоды писателя.
    extra = {}
    for topic in sorted({r["topic"] for r in runs} - {cfg.cmd_topic}):
        t_eps = sorted((ep for p in parts for ep in p["eps"].get(topic, [])), key=lambda ep: ep["start"])
        extra[topic] = _group(t_eps, _assign([ep["start"] for ep in t_eps], segs, cfg.match_window_s))

    per = []
    counts = {"correct": 0, "wrong": 0, "interrupted": 0, "none": 0, "unobserved": 0}
    lat = {k: [] for k in ("rec", "pub", "cmd", "motion", "cmd_to_motion")}
    excluded_rtf = 0
    confusion = {}
    false_actions, false_recog = {}, 0
    late = early = moved_on_unknown = moved_when_ignored = extra_actions = 0
    for k, s in enumerate(segs):
        m0 = s["_m0"]
        # Окно сегмента — от его начала до конца окна реакции или начала следующего; видно целиком — в одной сессии.
        hi = min([m0 + cfg.match_window_s] + [n["_m_start"] for n in segs[k + 1:k + 2]])
        whole = any(a <= s["_m_start"] and hi <= b for a, b in spans)
        my_eps = eps_by_seg.get(k, [])
        my_msgs = msgs_by_seg.get(k, [])
        # Реакция — первое действие сегмента (_first_action). Продолжение чужого манёвра — не действие: оно судится,
        # только если других эпизодов у сегмента нет (и тогда это unobserved).
        started = [e for e in my_eps if not e["cut_start"]]
        ep = started[0] if started else my_eps[0] if my_eps else None
        first, seen = _first_action(my_eps, m0)
        hidden = first is not None and not seen
        # Решающее сообщение perception: последнее до начала движения (по нему робот поехал), а без движения —
        # последнее сегмента. Длинный жест perception может разрезать на несколько сообщений. Начала реакции не видно —
        # не видно и решающего сообщения: оно могло прийти в перерыве или до реакции, которую наблюдатель не застал.
        before = [e for e in my_msgs if ep is not None and e["m"] <= ep["start"] + MSG_SKEW_S]
        rec = None if hidden else before[-1] if before else my_msgs[-1] if my_msgs else None
        out = {"seg": s["seg"], "kind": s["kind"], "cls": s["cls"], "name": s["name"], "side": s["side"],
               "shape": s.get("shape"), "t_start": round(anchor.t_wall + s["t_start"], 6),
               "t0": round(anchor.t_wall + s["t0"], 6), "rtf": _rtf_at(rtf, m0)}
        marks = {}
        if my_msgs:
            out["n_msgs"] = len(my_msgs)
        if rec is not None:
            msg = rec["msg"]
            offset = rec["t"] - rec["m"]
            out["rec"] = {k2: msg.get(k2) for k2 in ("cls", "name", "side", "conf", "n_boxes", "margin", "spikes")}
            marks["pub"] = rec["m"]
            if isinstance(msg.get("t_rec"), (int, float)):
                marks["rec"] = msg["t_rec"] - offset
        if ep is not None:
            out["phases"] = phases(ep, cfg.min_phase_s)
            if ep["cut_start"] or ep["cut_end"]:
                out["cut"] = "start" if ep["cut_start"] else "end"
            if seen:
                marks["cmd"] = ep["start"]
                if _covers(moving_any, ep["start"]):
                    out["moving_at_cmd"] = True
                else:
                    tm = _first_between(motion_starts, ep["start"], min(ep["end"] + 2.0, ep["span"][1]))
                    if tm is not None:
                        marks["motion"] = tm
                if before and rec["msg"].get("cls") == -1:
                    out["moved_on_unknown"] = True
                    moved_on_unknown += 1
        # Метка дополнительной темы — её первое действие в окне сегмента, если его начало видно, как у реакции.
        for topic, t_by_seg in extra.items():
            t_first, t_seen = _first_action(t_by_seg.get(k, []), m0)
            if t_seen:
                out.setdefault("lat_extra", {})[topic] = round(t_first["start"] - m0, 4)
        out["lat"] = {k2: round(v - m0, 4) for k2, v in marks.items()}
        if "cmd" in marks and "motion" in marks:
            out["lat"]["cmd_to_motion"] = round(marks["motion"] - marks["cmd"], 4)
        out["actions"] = len(started)
        extra_actions += max(0, len(started) - 1)

        if s["kind"] == "gesture":
            expected = expected_maneuver(cfg.reflex, s)
            out["expected"] = expected
            if ep is None:
                reaction = ("none" if expected else "correct") if whole else "unobserved"
                maneuver = None
            elif not seen or (ep["cut_end"] and expected and match(out["phases"], cfg.maneuvers[expected], cfg)):
                # Начала реакции не видно или начало ожидаемого манёвра оборвал конец сессии: исход неизвестен.
                reaction, maneuver = "unobserved", None
            else:
                reaction, maneuver = judge(out["phases"], expected, cfg,
                                           rec and requested_maneuver(cfg.reflex, rec["msg"]))
                if expected is None:
                    out["moved_when_ignored"] = True
                    moved_when_ignored += 1
            out["reaction"], out["maneuver"] = reaction, maneuver
            counts[reaction] += 1
            out["late"] = "cmd" in out["lat"] and out["lat"]["cmd"] > cfg.sla_s
            out["early"] = "cmd" in out["lat"] and out["lat"]["cmd"] < 0
            late += out["late"]
            early += out["early"]
            row = confusion.setdefault(str(s["cls"]), {})
            rc = str(rec["msg"].get("cls")) if rec is not None else "none" if whole and not hidden else "unobserved"
            row[rc] = row.get(rc, 0) + 1
            if out["rtf"] is not None and out["rtf"] < cfg.rtf_min:
                excluded_rtf += 1
            else:
                for k2, v in out["lat"].items():
                    lat[k2].append(v)
        else:
            if started:
                false_actions[s["name"]] = false_actions.get(s["name"], 0) + 1
                out["false_action"] = True
            if any(e["msg"].get("cls") not in (-1, None) for e in my_msgs):
                false_recog += 1
        per.append(out)

    spontaneous = sum(1 for ep, j in zip(eps, ep_seg) if j is None and not ep["cut_start"])
    unwanted = spontaneous + sum(1 for p in per if p.get("false_action") or p.get("moved_on_unknown")
                                 or p.get("moved_when_ignored"))
    observed = sum(b - a for a, b in spans)
    hours = observed / 3600.0

    unsafe = [u for p in parts for u in p["unsafe"]]
    unsafe_src = {}
    for p in parts:
        for src, found in p["unsafe_src"].items():
            unsafe_src.setdefault(src, []).extend(found)
    k6 = dict(iv_stats([(u["a"], u["b"]) for u in unsafe if u["cause"] != "braking_after_zero"]),
              stop_after_last_cmd_s=latency_stats([u["stop"] - u["last_cmd"] for u in unsafe if u["cause"] == "silent"
                                                   and u["last_cmd"] is not None and u["stop"] is not None]),
              by_src={src: _by_cause(found) for src, found in sorted(unsafe_src.items())},
              **_by_cause(unsafe))
    health_deaf = [iv for p in parts for iv in p["health"]["deaf"] if iv[1] - iv[0] >= cfg.deaf_min_s]
    walls = by.get("wall", [])
    dists = [e["d"] for e in by.get("pose", []) + walls if e.get("d") is not None]
    rtf_vals = sorted(e["rtf"] for e in rtf)
    n_g = sum(1 for s in segs if s["kind"] == "gesture")
    cmd_msgs = {}
    for r in runs:
        cmd_msgs[r["topic"]] = cmd_msgs.get(r["topic"], 0) + r["n"]

    summary = {
        "label": LABEL,
        "duration_s": round(observed, 3),
        "observer_gaps": iv_stats([(a[1], b[0]) for a, b in zip(spans, spans[1:])]),
        "segments": {"gestures": n_g, "negatives": len(segs) - n_g},
        "reactions": counts,
        "late": late,
        "early": early,
        "visible_failures": sum(1 for p in per if p["kind"] == "gesture"
                                and (p["reaction"] not in ("correct", "unobserved") or p["late"])),
        "recognition": {"msgs": len(msgs), "bad_msgs": sum(1 for e in by.get("gesture", []) if "msg" not in e),
                        "unmatched": sum(1 for j in msg_seg if j is None), "confusion": confusion},
        "moved_on_unknown": moved_on_unknown,
        "false_actions": {"total": sum(false_actions.values()), "by_kind": false_actions},
        "false_recognitions": false_recog,
        "extra_actions": extra_actions,
        "spontaneous_actions": spontaneous,
        "k7": {"moved_on_unknown": moved_on_unknown, "moved_when_ignored": moved_when_ignored,
               "false_actions": sum(false_actions.values()), "spontaneous_actions": spontaneous, "total": unwanted,
               "hours": round(hours, 4), "per_hour": round(unwanted / hours, 3) if hours > 0 else None},
        "latency_s": dict({k: latency_stats(v) for k, v in lat.items()}, excluded_rtf=excluded_rtf),
        "unsafe_no_fresh_cmd": k6,
        "deaf": {"health": iv_stats(health_deaf),
                 "health_missing": iv_stats(_missing([p["health"]["m"] for p in parts], listened, cfg.health_gap_s)),
                 "health_msgs": sum(len(p["health"]["m"]) for p in parts),
                 "health_bad_msgs": sum(p["health"]["bad"] for p in parts)},
        "wall": dict(iv_stats([iv for p in parts for iv in p["touch"]]), min_d=round(min(dists), 4) if dists else None),
        "rtf": {"windows": len(rtf_vals), "min": rtf_vals[0] if rtf_vals else None,
                "median": rtf_vals[len(rtf_vals) // 2] if rtf_vals else None,
                "share_below": round(sum(1 for v in rtf_vals if v < cfg.rtf_min) / len(rtf_vals), 4)
                if rtf_vals else None},
        "cmd": {"msgs": cmd_msgs, "type_mismatch": sorted({e["topic"] for e in by.get("cmd_type_mismatch", [])}),
                "moving_without_cmd": any(p["moving_without_cmd"] for p in parts)},
        "config": asdict(cfg),
    }
    if probe_lines is not None:
        pr = _probe([[p for p in probe_lines if a <= p["m"] <= b] for a, b in spans], listened, cfg)
        summary["deaf"]["probe"] = iv_stats(pr["deaf"])
        summary["deaf"]["probe_standing"] = iv_stats([iv for iv in subtract(pr["deaf"], moving_any)
                                                      if iv[1] - iv[0] >= cfg.deaf_min_s])
        summary["deaf"]["probe_missing"] = iv_stats(pr["missing"])
        summary["probe"] = {"pids": pr["pids"], "fault_starts": pr["fault_starts"], "events": pr["events"],
                            "timeouts": pr["timeouts"]}
    return per, summary


# ---------------------------------------------------------------------------
# Файлы и CLI
# ---------------------------------------------------------------------------

def read_events(path: str) -> list:
    """Сессии записи [(заголовок, события)]: каждый запуск наблюдателя пишет заголовок. Недописанная последняя
    строка (наблюдатель убит) пропускается с предупреждением."""
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    sessions = []
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            if i >= len(lines) - 2:
                print("analyze: недописанная последняя строка %s пропущена" % path, file=sys.stderr)
                continue
            raise
        if "header" in d:
            sessions.append((d["header"], []))
        elif sessions:
            sessions[-1][1].append(d)
        else:
            sessions.append(({}, [d]))
    return sessions


def _foreign(header: dict, events: list, anchor):
    """Почему сессия записи не относится к прогону этого якоря, или None."""
    if header.get("boot_id") and anchor.boot_id and header["boot_id"] != anchor.boot_id:
        return "boot"
    if header.get("timeline_sha256") and header["timeline_sha256"] != anchor.timeline_sha256:
        return "timeline"
    if not any(e.get("m1", e.get("m", -math.inf)) >= anchor.t_mono for e in events):
        return "before_anchor"
    return None


def select_sessions(sessions: list, anchor):
    """(события по сессиям этого прогона, сводка отброшенного): чужая загрузка ОС, чужой таймлайн или сессия,
    целиком закончившаяся до якоря (прошлый прогон в том же файле). Сессия этого прогона начинается событием start
    в момент заголовка: наблюдатель пишет его до подписок, а первое событие приходит, когда они соединятся."""
    kept, dropped, n_dropped = [], {}, 0
    for header, evs in sessions:
        why = _foreign(header, evs, anchor)
        if why is None:
            kept.append(([{"ev": "start", "t": header.get("t"), "m": header["m"]}] if "m" in header else []) + evs)
        else:
            dropped[why] = dropped.get(why, 0) + 1
            n_dropped += len(evs)
    if dropped:
        print("analyze: отброшены сессии записи не этого прогона: %s, событий %d" % (dumps(dropped), n_dropped),
              file=sys.stderr)
    return kept, {"sessions": len(sessions), "dropped_sessions": dropped, "dropped_events": n_dropped}


def read_probe(path: str) -> list:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def _step_ok(st) -> bool:
    return (isinstance(st, list) and len(st) == 2 and st[0] in ("F", "B", "L", "R")
            and (st[1] is None or (isinstance(st[1], (int, float)) and st[1] > 0)))


def load_config(path=None, **overrides) -> AnalyzeConfig:
    """Конфиг разбора; path — JSON {maneuvers, reflex} (таблицы проверяются)."""
    cfg = AnalyzeConfig(**overrides)
    if path:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        cfg.maneuvers = d.get("maneuvers", cfg.maneuvers)
        cfg.reflex = d.get("reflex", cfg.reflex)
    if not isinstance(cfg.maneuvers, dict) or not isinstance(cfg.reflex, dict):
        raise ValueError("maneuvers и reflex — JSON-объекты")
    for name, steps in cfg.maneuvers.items():
        if not isinstance(steps, list) or not steps or not all(_step_ok(st) for st in steps):
            raise ValueError("манёвр %s: шаги [[F|B|L|R, секунды|null], …]" % name)
    bad = {k: v for k, v in cfg.reflex.items() if v not in (None, STAND) and v not in cfg.maneuvers}
    if bad:
        raise ValueError("рефлекс ссылается на неизвестные манёвры: %s" % dumps(bad))
    return cfg


def run(events_path: str, truth_path: str, anchor_path: str, probe_path=None, cfg: AnalyzeConfig = None):
    """Разбор файлов прогона: истина и якорь должны быть от одного таймлайна, сессии записи — от этого прогона."""
    header, segments = read_jsonl(truth_path)
    anchor = read_anchor(anchor_path, live=False)
    if header.get("timeline_sha256") != anchor.timeline_sha256:
        raise ValueError("истина %s и якорь %s — от разных таймлайнов (sha256 %s и %s)"
                         % (truth_path, anchor_path, str(header.get("timeline_sha256"))[:12],
                            anchor.timeline_sha256[:12]))
    sessions, info = select_sessions(read_events(events_path), anchor)
    per, summary = analyze(sessions, segments, anchor, read_probe(probe_path) if probe_path else None, cfg)
    summary["input"] = info
    return per, summary


def write_results(per: list, summary: dict, out_path=None, per_path=None) -> None:
    if per_path:
        with open(per_path, "w", encoding="utf-8") as fh:
            for p in per:
                fh.write(dumps(p) + "\n")
    text = json.dumps(summary, ensure_ascii=False, indent=1, sort_keys=True)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)


def add_config_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--maneuvers",
                   help="JSON {maneuvers: {имя: [[буква, с|null], …]}, reflex: {\"класс[:сторона]\": имя|\"none\"}}")
    for f in FLAGS:
        p.add_argument("--" + f.name.replace("_", "-"), dest=f.name, type=type(f.default), default=f.default,
                       help=f.metadata["help"])


def config_from_args(a: argparse.Namespace) -> AnalyzeConfig:
    return load_config(a.maneuvers, **{f.name: getattr(a, f.name) for f in FLAGS})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m neurobot.sim.gestures.analyze", description=__doc__.split("\n")[0])
    p.add_argument("--events", required=True, help="запись наблюдателя events.jsonl")
    p.add_argument("--truth", required=True, help="truth.jsonl gesturegen")
    p.add_argument("--anchor", required=True, help="якорь таймлайна этого прогона")
    p.add_argument("--probe", help="журнал подставных модулей (probe.py) — глухота процесса без темы здоровья")
    p.add_argument("--out", help="сводка JSON (по умолчанию — stdout)")
    p.add_argument("--per-gesture", help="JSONL по сегментам")
    add_config_args(p)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        per, summary = run(a.events, a.truth, a.anchor, a.probe, config_from_args(a))
        write_results(per, summary, a.out, a.per_gesture)
    except (OSError, ValueError) as e:
        print("analyze: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
