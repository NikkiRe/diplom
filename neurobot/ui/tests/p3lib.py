"""Приёмка П3 пульта на двойнике (docs/ui/PLAN.md §0а, §7): разбор замеров без ROS и браузера.

Сдвиг часов ноутбука и ВМ по /api/meta (запрос с наименьшим RTT), PSS из /proc/PID/smaps_rollup, байты сокетов шлюза
из `ss -tinH`, трафик по сокетам (живые — приращение, новые — всё с открытия), CPU по cgroup в долях ядра ВМ и A57,
наклон памяти МНК, сводка шага и сравнение K1 «с пультом и без». Python 3.8+, только stdlib; тесты — test_ui_p3.py.
Числа — стенд в общей ВМ: ориентир, не результат робота.
"""
from __future__ import annotations

import math
import re
import statistics
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

MIB = 1024.0 * 1024.0
SOCK_RE = re.compile(r"^\s*\d+\s+\d+\s+(\S+)\s+(\S+)")
ACKED_RE = re.compile(r"\bbytes_acked:(\d+)")
SENT_RE = re.compile(r"\bbytes_sent:(\d+)")
RECV_RE = re.compile(r"\bbytes_received:(\d+)")


def clock_offset(samples: Iterable[Tuple[float, float, float]]) -> Tuple[float, float]:
    """(сдвиг, RTT): часы сервера − часы клиента по запросу с наименьшим RTT; samples — (отправлен, t сервера,
    получен) по часам клиента. Ошибка сдвига не больше RTT/2."""
    best = min(samples, key=lambda s: s[2] - s[0])
    return best[1] - (best[0] + best[2]) / 2.0, best[2] - best[0]


def smaps_rollup(text: str) -> Dict[str, int]:
    """Поля /proc/PID/smaps_rollup в КиБ: pss, rss, uss (Private_Clean + Private_Dirty), swap."""
    f: Dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(":") and parts[1].isdigit():
            f[parts[0][:-1]] = int(parts[1])
    return {"pss": f.get("Pss", 0), "rss": f.get("Rss", 0),
            "uss": f.get("Private_Clean", 0) + f.get("Private_Dirty", 0), "swap": f.get("Swap", 0)}


def ss_sockets(text: str) -> Dict[str, Dict[str, int]]:
    """Сокеты из `ss -tinH state established '( sport = :PORT )'`: «local peer» → {acked, sent, received}. Строка
    сокета, затем строка tcp_info с bytes_*; у сокета без данных bytes_acked нет — 0."""
    out: Dict[str, Dict[str, int]] = {}
    key = None
    for line in text.splitlines():
        m = SOCK_RE.match(line)
        if m and ":" in m.group(1) and ":" in m.group(2):
            key = m.group(1) + " " + m.group(2)
            out[key] = {"acked": 0, "sent": 0, "received": 0}
            continue
        if key is not None:
            for name, rx in (("acked", ACKED_RE), ("sent", SENT_RE), ("received", RECV_RE)):
                g = rx.search(line)
                if g:
                    out[key][name] = int(g.group(1))
    return out


def traffic(samples: Sequence[dict], field: str = "acked") -> dict:
    """Байты шлюза в сторону клиентов за окно выборок (у каждой «t» и «socks»): живой сокет — приращение, новый —
    всё с открытия (начало потока, статика страницы). Закрытый между выборками теряет хвост — нижняя оценка.
    steady — только приращения сокетов, живших в обеих выборках: установившийся поток без новых подключений."""
    total = steady = 0
    new = 0
    for a, b in zip(samples, samples[1:]):
        sa, sb = a.get("socks") or {}, b.get("socks") or {}
        for k, v in sb.items():
            if k in sa:
                d = max(0, v[field] - sa[k][field])
                total += d
                steady += d
            else:
                total += v[field]
                new += 1
    span = samples[-1]["t"] - samples[0]["t"] if len(samples) > 1 else 0.0
    return {"bytes": total, "span_s": round(span, 1), "rate_Bps": round(total / span, 1) if span else None,
            "steady_Bps": round(steady / span, 1) if span else None, "new_sockets": new}


def cpu_pct(samples: Sequence[dict]) -> Optional[float]:
    """CPU cgroup шлюза за окно, % одного ядра ВМ: приращение usage_usec / стенное время."""
    if len(samples) < 2:
        return None
    a, b = samples[0], samples[-1]
    span = b["t"] - a["t"]
    return round((b["cpu_usec"] - a["cpu_usec"]) / 1e6 / span * 100.0, 3) if span > 0 else None


def cpu_split(samples: Sequence[dict]) -> Optional[dict]:
    """CPU за окно, % ядра ВМ: сам процесс шлюза (utime + stime, все потоки) и дождавшиеся дочерние (systemctl show
    опросчика юнитов, doctor) — по тикам /proc/PID/stat; None — в выборках тиков нет."""
    w = [s for s in samples if "main_ticks" in s]
    if len(w) < 2 or w[-1]["t"] <= w[0]["t"]:
        return None
    a, b = w[0], w[-1]
    span, hz = b["t"] - a["t"], float(b.get("hz") or 100)
    return {"main": round((b["main_ticks"] - a["main_ticks"]) / hz / span * 100.0, 3),
            "children": round((b["child_ticks"] - a["child_ticks"]) / hz / span * 100.0, 3)}


def to_a57(pct_vm: Optional[float], ratio: float) -> Optional[float]:
    """% ядра ВМ → % ядра A57 при отношении производительности ratio = A57 / ядро ВМ (7-zip, budget/cpucal):
    сколько занял бы шлюз на Nano, если Python и DDS замедляются так же, как 7-zip [допущение]."""
    return None if pct_vm is None or not ratio else round(pct_vm / ratio, 2)


def slope_per_h(points: Sequence[Tuple[float, float]]) -> Optional[float]:
    """Наклон МНК y(t) в единицах y за час; t — секунды."""
    n = len(points)
    if n < 3:
        return None
    mt = sum(t for t, _ in points) / n
    my = sum(y for _, y in points) / n
    var = sum((t - mt) ** 2 for t, _ in points)
    if var <= 0:
        return None
    return round(sum((t - mt) * (y - my) for t, y in points) / var * 3600.0, 3)


def window(samples: Sequence[dict], skip_s: float) -> List[dict]:
    """Выборки после прогрева skip_s от первой."""
    if not samples:
        return []
    t0 = samples[0]["t"]
    return [s for s in samples if s["t"] - t0 >= skip_s]


def mem_stats(samples: Sequence[dict], key: str) -> dict:
    """Память (КиБ в выборках: pss_kib, uss_kib, rss_kib, anon_kib, mem_current) → МиБ: первая, наибольшая,
    последняя, средняя."""
    v = [s[key] / 1024.0 for s in samples if s.get(key) is not None]
    if not v:
        return {"first": None, "max": None, "last": None, "mean": None}
    return {"first": round(v[0], 1), "max": round(max(v), 1), "last": round(v[-1], 1),
            "mean": round(sum(v) / len(v), 1)}


def step_summary(samples: Sequence[dict], ratio: float, skip_s: float = 30.0) -> dict:
    """Сводка шага замера ресурсов: PSS, USS, RSS шлюза, память cgroup (current и anon), CPU (ядро ВМ и A57; сам
    процесс и дочерние, если есть тики), трафик, клиенты SSE."""
    w = window(samples, skip_s)
    pct = cpu_pct(w)
    return {"n": len(w), "span_s": round(w[-1]["t"] - w[0]["t"], 1) if len(w) > 1 else 0.0,
            "pss_mib": mem_stats(w, "pss_kib"), "uss_mib": mem_stats(w, "uss_kib"), "rss_mib": mem_stats(w, "rss_kib"),
            "cgroup_mib": mem_stats(w, "mem_current"), "anon_mib": mem_stats(w, "anon_kib"),
            "cpu_pct_vm": pct, "cpu_pct_a57": to_a57(pct, ratio), "cpu_split_vm": cpu_split(w), "traffic": traffic(w),
            "clients": sorted({s.get("clients") for s in w if s.get("clients") is not None}),
            "restarts": (w[-1].get("nrestarts", 0) - w[0].get("nrestarts", 0)) if w else None,
            "pss_slope_mib_h": slope_per_h([(s["t"] - w[0]["t"], s["pss_kib"] / 1024.0) for s in w
                                            if s.get("pss_kib") is not None])}


def verdict(value: Optional[float], limit: float) -> Optional[bool]:
    return None if value is None else value <= limit


def compare_k1(runs: Sequence[dict]) -> dict:
    """K1 с пультом и без: p50 и p95 по прогонам; разброс — размах тех же величин между прогонами одного условия;
    различие условий в пределах разброса, если |Δ средних по условию| не больше наибольшего внутреннего размаха и
    ранговые ДИ медиан условий пересекаются."""
    by: Dict[str, List[dict]] = {}
    for r in runs:
        by.setdefault(r["ui"], []).append(r)
    out: dict = {"conditions": {}}
    for cond, rs in by.items():
        p50 = [r["k1"]["p50"] for r in rs if r["k1"].get("p50") is not None]
        p95 = [r["k1"]["p95"] for r in rs if r["k1"].get("p95") is not None]
        out["conditions"][cond] = {
            "runs": [r["label"] for r in rs], "n": [r["k1"]["n"] for r in rs], "p50": p50, "p95": p95,
            "p50_mean": round(sum(p50) / len(p50), 4) if p50 else None,
            "p95_mean": round(sum(p95) / len(p95), 4) if p95 else None,
            "p50_range": round(max(p50) - min(p50), 4) if len(p50) > 1 else None,
            "p95_range": round(max(p95) - min(p95), 4) if len(p95) > 1 else None,
            "p50_ci": [r.get("k1_p50_ci") for r in rs]}
    c = out["conditions"]
    if "on" in c and "off" in c and c["on"]["p50_mean"] is not None and c["off"]["p50_mean"] is not None:
        d50 = round(c["on"]["p50_mean"] - c["off"]["p50_mean"], 4)
        d95 = round(c["on"]["p95_mean"] - c["off"]["p95_mean"], 4) if c["on"]["p95_mean"] is not None and \
            c["off"]["p95_mean"] is not None else None
        spread50 = max(x for x in (c["on"]["p50_range"], c["off"]["p50_range"], 0.0) if x is not None)
        spread95 = max(x for x in (c["on"]["p95_range"], c["off"]["p95_range"], 0.0) if x is not None)
        cis = [ci for cond in ("on", "off") for ci in c[cond]["p50_ci"] if ci]
        overlap = bool(cis) and max(ci[0] for ci in cis) <= min(ci[1] for ci in cis)
        out.update(delta_p50_s=d50, delta_p95_s=d95, spread_p50_s=spread50, spread_p95_s=spread95,
                   ci_p50_overlap=overlap,
                   within_spread=abs(d50) <= spread50 and (d95 is None or abs(d95) <= spread95) and overlap)
    return out


def median_ci(values: Sequence[float], conf: float = 0.95) -> Optional[Tuple[float, float]]:
    """Ранговый ДИ медианы (порядковые статистики по Bin(n, 1/2), как quantile_ci разбора жестов): None — выборка
    мала."""
    v = sorted(values)
    n = len(v)
    if n < 6:
        return None
    alpha = (1.0 - conf) / 2
    cdf, acc = [], 0.0
    for i in range(n + 1):
        acc += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) - n * math.log(2.0))
        cdf.append(acc)
    lo = max((k for k in range(1, n + 1) if cdf[k - 1] <= alpha), default=None)
    hi = min((k for k in range(1, n + 1) if cdf[k - 1] >= 1.0 - alpha), default=None)
    return None if lo is None or hi is None else (v[lo - 1], v[hi - 1])


def paired_k1(per_run: Dict[str, Dict[int, float]], on: Sequence[str], off: Sequence[str]) -> dict:
    """K1 по одним и тем же жестам (одно расписание — номер сегмента): для каждого жеста среднее по прогонам с пультом
    минус среднее без него, мс. Влияние пульта не видно, если ранговый ДИ медианы разности содержит 0; масштаб шума —
    разности прогонов одного условия (on1 − on2, off1 − off2) по тем же жестам. Жест в счёт, только если он есть во
    всех прогонах (не ранний, RTF ≥ 0,95)."""
    segs = sorted(set.intersection(*[set(per_run[r]) for r in list(on) + list(off)]))
    if len(segs) < 6:
        return {"segs": len(segs)}

    def mean_of(runs, s):
        return sum(per_run[r][s] for r in runs) / len(runs)
    d = [1000.0 * (mean_of(on, s) - mean_of(off, s)) for s in segs]
    within = [1000.0 * (per_run[a][s] - per_run[b][s]) for a, b in (tuple(on[:2]), tuple(off[:2])) if a != b
              for s in segs] if len(on) > 1 and len(off) > 1 else []
    ci = median_ci(d)
    return {"segs": len(segs), "median_ms": round(statistics.median(d), 1), "mean_ms": round(statistics.mean(d), 1),
            "sd_ms": round(statistics.stdev(d), 1),
            "ci95_median_ms": [round(ci[0], 1), round(ci[1], 1)] if ci else None,
            "positive": sum(1 for x in d if x > 0),
            "within_sd_ms": round(statistics.stdev(within), 1) if len(within) > 2 else None,
            "no_effect_seen": bool(ci) and ci[0] <= 0.0 <= ci[1]}
