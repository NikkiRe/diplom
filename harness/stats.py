"""Статистика для сравнения конфигураций на парных данных (одни и те же задачи BFCL).

Без scipy: нормальное распределение — statistics.NormalDist, биномиальные вероятности —
точной целочисленной арифметикой (math.comb) или через lgamma.

Обозначения для бинарных исходов: a — базовая конфигурация, b — сравниваемая; для каждой
задачи 1 = решена. n01 — число задач, где a не решила, а b решила; n10 — наоборот.
Разность долей diff = p_b − p_a = (n01 − n10) / n.

Функции:
  paired_bootstrap_ratio  — отношение медиан/средних (b / a) и перцентильный ДИ парного бутстрепа;
  mcnemar_exact           — точный двусторонний тест Мак-Немара (биномиальный, p = 1/2), и mid-p;
  paired_diff_ci          — ДИ для разности парных долей: Вальд или score-интервал Танго (1998);
  tost_paired             — TOST эквивалентности парных долей в границах ±δ (Танго или Вальд);
  clopper_pearson         — точный ДИ для доли;
  aa_floor_binary/ratio   — «пол» A/A: насколько расходятся два прогона одной и той же конфигурации;
  holm                    — поправка Холма на множественные сравнения.
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Sequence

import numpy as np

_NORMAL = NormalDist()


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------

def _paired_bool(a: Sequence, b: Sequence) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray(a, dtype=bool)
    y = np.asarray(b, dtype=bool)
    if x.shape != y.shape or x.ndim != 1:
        raise ValueError("paired outcomes must be 1-D sequences of equal length")
    if x.size == 0:
        raise ValueError("no pairs")
    return x, y


def discordant_counts(a: Sequence, b: Sequence) -> tuple[int, int, int]:
    """(n, n01, n10): n01 — a=0, b=1; n10 — a=1, b=0."""
    x, y = _paired_bool(a, b)
    return int(x.size), int(np.sum(~x & y)), int(np.sum(x & ~y))


# ---------------------------------------------------------------------------
# Парный бутстреп отношения
# ---------------------------------------------------------------------------

def paired_bootstrap_ratio(baseline: Sequence[float], treatment: Sequence[float], stat: str = "median",
                           n_boot: int = 10000, conf: float = 0.95, seed: int = 0, chunk: int = 1000) -> dict:
    """Отношение stat(treatment) / stat(baseline) и перцентильный ДИ парного бутстрепа.

    Пары (baseline[i], treatment[i]) ресэмплируются вместе — сохраняется корреляция между
    конфигурациями на одной задаче. stat: "median" или "mean".
    """
    x = np.asarray(baseline, dtype=float)
    y = np.asarray(treatment, dtype=float)
    if x.shape != y.shape or x.ndim != 1 or x.size == 0:
        raise ValueError("baseline and treatment must be non-empty 1-D sequences of equal length")
    if not (np.all(np.isfinite(x)) and np.all(np.isfinite(y))):
        raise ValueError("non-finite values")
    funcs = {"median": np.median, "mean": np.mean}
    if stat not in funcs:
        raise ValueError(f"stat must be one of {sorted(funcs)}")
    f = funcs[stat]
    n = x.size
    rng = np.random.default_rng(seed)
    parts = []
    for start in range(0, n_boot, chunk):
        m = min(chunk, n_boot - start)
        idx = rng.integers(0, n, size=(m, n))
        with np.errstate(divide="ignore", invalid="ignore"):
            parts.append(f(y[idx], axis=1) / f(x[idx], axis=1))
    ratios = np.concatenate(parts)
    alpha = 1.0 - conf
    lo, hi = np.quantile(ratios, [alpha / 2, 1 - alpha / 2])
    with np.errstate(divide="ignore", invalid="ignore"):
        estimate = float(f(y) / f(x))
    return {"estimate": estimate, "ci_low": float(lo), "ci_high": float(hi), "conf": conf, "stat": stat,
            "n": int(n), "n_boot": int(n_boot), "seed": seed, "method": "paired percentile bootstrap"}


# ---------------------------------------------------------------------------
# Биномиальные вероятности и McNemar
# ---------------------------------------------------------------------------

def _binom_half_cdf(k: int, n: int) -> float:
    """P(X ≤ k), X ~ Bin(n, 1/2) — точно, через целые числа."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return sum(math.comb(n, i) for i in range(k + 1)) / (1 << n)


def mcnemar_exact(n01: int, n10: int) -> dict:
    """Точный двусторонний тест Мак-Немара: при H0 n01 ~ Bin(n01 + n10, 1/2).

    p = min(1, 2·P(X ≤ min(n01, n10))). mid-p (Fagerland et al., 2013) — вычитается половина
    вероятности наблюдённого значения; менее консервативен, приводится справочно.
    """
    if n01 < 0 or n10 < 0:
        raise ValueError("counts must be non-negative")
    m = n01 + n10
    if m == 0:
        return {"n_discordant": 0, "n01": n01, "n10": n10, "p": 1.0, "p_mid": 1.0}
    k = min(n01, n10)
    p = min(1.0, 2.0 * _binom_half_cdf(k, m))
    pmf_k = math.comb(m, k) / (1 << m)
    p_mid = min(1.0, 2.0 * (_binom_half_cdf(k, m) - 0.5 * pmf_k))
    return {"n_discordant": m, "n01": n01, "n10": n10, "p": p, "p_mid": p_mid}


def mcnemar_paired(a: Sequence, b: Sequence) -> dict:
    n, n01, n10 = discordant_counts(a, b)
    out = mcnemar_exact(n01, n10)
    out["n"] = n
    return out


def _binom_logpmf(i: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 0.0 if i == 0 else -math.inf
    if p >= 1.0:
        return 0.0 if i == n else -math.inf
    return (math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
            + i * math.log(p) + (n - i) * math.log1p(-p))


def _binom_cdf(k: int, n: int, p: float) -> float:
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return min(1.0, sum(math.exp(_binom_logpmf(i, n, p)) for i in range(k + 1)))


def _bisect(f, lo: float, hi: float, iters: int = 200) -> float:
    """Корень монотонной f на [lo, hi] (знаки на концах различаются)."""
    flo = f(lo)
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        fm = f(mid)
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi = mid
    return 0.5 * (lo + hi)


def clopper_pearson(k: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    """Точный (Клоппер–Пирсон) двусторонний ДИ для доли k/n."""
    if not 0 <= k <= n or n == 0:
        raise ValueError("need 0 <= k <= n, n > 0")
    alpha = 1.0 - conf
    lo = 0.0 if k == 0 else _bisect(lambda p: (1.0 - _binom_cdf(k - 1, n, p)) - alpha / 2, 0.0, 1.0)
    hi = 1.0 if k == n else _bisect(lambda p: _binom_cdf(k, n, p) - alpha / 2, 0.0, 1.0)
    return lo, hi


# ---------------------------------------------------------------------------
# Разность парных долей: Вальд и score-статистика Танго
# ---------------------------------------------------------------------------

def _xlogy(x: float, y: float) -> float:
    if x == 0:
        return 0.0
    return x * math.log(y) if y > 0 else -math.inf


def tango_rmle(x: int, y: int, n: int, delta: float) -> float:
    """Ограниченная ОМП q = P(first=0, second=1) при условии P(1,0) − P(0,1) = delta.

    x = #(first=1, second=0), y = #(first=0, second=1). Максимизируется
    x·ln(q+Δ) + y·ln q + (n−x−y)·ln(1−2q−Δ) на q ∈ [max(0, −Δ), (1−Δ)/2]. Стационарные точки —
    корни 2n·q² + (−x − y + (2n − x + y)Δ)·q − yΔ(1−Δ) = 0 (Tango, 1998); дополнительно
    проверяются концы отрезка — так корректно обрабатываются x = 0 или y = 0.
    """
    m = n - x - y
    lo, hi = max(0.0, -delta), (1.0 - delta) / 2.0

    def loglik(q: float) -> float:
        return _xlogy(x, q + delta) + _xlogy(y, q) + _xlogy(m, 1.0 - 2.0 * q - delta)

    A = 2.0 * n
    B = -x - y + (2.0 * n - x + y) * delta
    C = -y * delta * (1.0 - delta)
    candidates = [lo, hi]
    disc = B * B - 4.0 * A * C
    if disc >= 0:
        root = math.sqrt(disc)
        candidates += [(-B + root) / (2 * A), (-B - root) / (2 * A)]
    feasible = [q for q in candidates if lo - 1e-15 <= q <= hi + 1e-15]
    return max(feasible, key=loglik)


def tango_z(x: int, y: int, n: int, delta: float) -> float:
    """Score-статистика Танго для H0: P(first) − P(second) = delta (убывает по delta).

    Z = (x − y − nΔ) / sqrt(n·(2q̃ + Δ(1−Δ))); при Δ = 0 совпадает со статистикой Мак-Немара.
    """
    q = tango_rmle(x, y, n, delta)
    var = n * (2.0 * q + delta * (1.0 - delta))
    num = x - y - n * delta
    if var <= 1e-300:
        return 0.0 if abs(num) < 1e-12 else math.copysign(math.inf, num)
    return num / math.sqrt(var)


def _wald(x: int, y: int, n: int) -> tuple[float, float]:
    diff = (x - y) / n
    var = (x + y) - (x - y) ** 2 / n
    return diff, math.sqrt(max(var, 0.0)) / n


def paired_diff_ci(a: Sequence, b: Sequence, conf: float = 0.95, method: str = "tango") -> dict:
    """ДИ для diff = p_b − p_a по парным бинарным исходам."""
    n, n01, n10 = discordant_counts(a, b)
    # «first» — b, «second» — a: x = #(b=1, a=0) = n01, y = #(b=0, a=1) = n10.
    x, y = n01, n10
    diff = (x - y) / n
    z = _NORMAL.inv_cdf(1 - (1 - conf) / 2)
    if method == "wald":
        _, se = _wald(x, y, n)
        lo, hi = max(-1.0, diff - z * se), min(1.0, diff + z * se)
    elif method == "tango":
        eps = 1e-12
        f_lo = lambda d: tango_z(x, y, n, d) - z  # noqa: E731
        f_hi = lambda d: tango_z(x, y, n, d) + z  # noqa: E731
        lo = -1.0 if f_lo(-1.0 + eps) <= 0 else _bisect(f_lo, -1.0 + eps, diff)
        hi = 1.0 if f_hi(1.0 - eps) >= 0 else _bisect(f_hi, diff, 1.0 - eps)
    else:
        raise ValueError("method must be 'tango' or 'wald'")
    return {"diff": diff, "ci_low": lo, "ci_high": hi, "conf": conf, "method": method, "n": n,
            "n01": n01, "n10": n10}


def tost_paired(a: Sequence, b: Sequence, delta: float, alpha: float = 0.05, method: str = "tango") -> dict:
    """TOST эквивалентности парных долей: H0: |p_b − p_a| ≥ δ против H1: |p_b − p_a| < δ.

    Два односторонних теста уровня α: H01: diff ≤ −δ и H02: diff ≥ δ; p = max(p1, p2).
    Эквивалентность принимается при p < α, что равносильно попаданию (1 − 2α)-ДИ в (−δ, δ).
    method="tango" — score-тест (Tango, 1998; Liu et al., 2002), "wald" — z-тест с выборочной SE.
    """
    if not 0 < delta < 1:
        raise ValueError("delta must be in (0, 1)")
    n, n01, n10 = discordant_counts(a, b)
    x, y = n01, n10
    diff = (x - y) / n
    if method == "tango":
        p1 = 1.0 - _NORMAL.cdf(tango_z(x, y, n, -delta))
        p2 = _NORMAL.cdf(tango_z(x, y, n, delta))
    elif method == "wald":
        _, se = _wald(x, y, n)
        if se == 0:
            p1 = 0.0 if diff > -delta else 1.0
            p2 = 0.0 if diff < delta else 1.0
        else:
            p1 = 1.0 - _NORMAL.cdf((diff + delta) / se)
            p2 = _NORMAL.cdf((diff - delta) / se)
    else:
        raise ValueError("method must be 'tango' or 'wald'")
    p = max(p1, p2)
    ci = paired_diff_ci(a, b, conf=1 - 2 * alpha, method=method)
    return {"diff": diff, "delta": delta, "alpha": alpha, "method": method, "p_lower": p1, "p_upper": p2,
            "p": p, "equivalent": p < alpha, "ci_low": ci["ci_low"], "ci_high": ci["ci_high"],
            "ci_conf": 1 - 2 * alpha, "n": n, "n01": n01, "n10": n10}


# ---------------------------------------------------------------------------
# A/A-пол
# ---------------------------------------------------------------------------

def aa_floor_binary(run1: Sequence, run2: Sequence, conf: float = 0.95) -> dict:
    """Расхождение исходов двух прогонов ОДНОЙ конфигурации (A/A).

    Доля задач, где исход поменялся, — шумовой пол: если у сравнения A/B доля несовпадающих
    исходов в пределах этого пола, различие не отличимо от недетерминизма движка.
    """
    n, n01, n10 = discordant_counts(run1, run2)
    k = n01 + n10
    lo, hi = clopper_pearson(k, n, conf)
    return {"n": n, "discordant": k, "rate": k / n, "ci_low": lo, "ci_high": hi, "conf": conf,
            "mcnemar": mcnemar_exact(n01, n10)}


def aa_floor_ratio(run1: Sequence[float], run2: Sequence[float], stat: str = "median", n_boot: int = 10000,
                   conf: float = 0.95, seed: int = 0) -> dict:
    """Шумовой пол для отношения времён: ДИ отношения run2/run1 для двух прогонов одной конфигурации.

    floor = max(1 − ci_low, ci_high − 1): эффекты ускорения меньше этой величины неотличимы от
    разброса A/A. Дополнительно — квантили |ln(run2_i / run1_i)| по задачам.
    """
    boot = paired_bootstrap_ratio(run1, run2, stat=stat, n_boot=n_boot, conf=conf, seed=seed)
    x = np.asarray(run1, dtype=float)
    y = np.asarray(run2, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        logr = np.abs(np.log(y / x))
    logr = logr[np.isfinite(logr)]
    q = np.quantile(logr, [0.5, 0.9, 0.95]) if logr.size else [math.nan] * 3
    return {**boot, "floor": max(1.0 - boot["ci_low"], boot["ci_high"] - 1.0),
            "abs_log_ratio_p50": float(q[0]), "abs_log_ratio_p90": float(q[1]), "abs_log_ratio_p95": float(q[2])}


# ---------------------------------------------------------------------------
# Множественные сравнения
# ---------------------------------------------------------------------------

def holm(pvalues: Sequence[float]) -> list[float]:
    """Скорректированные p-значения Холма (step-down), в исходном порядке."""
    p = [float(v) for v in pvalues]
    m = len(p)
    order = sorted(range(m), key=lambda i: p[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p[i]))
        adjusted[i] = running
    return adjusted


# ===========================================================================
# Временные ряды «работы во времени» (добавлено для ВКР EdgeCtx, THESIS_CONCEPT §12.6)
#
#   sen_slope                 — наклон Тейла–Сена и его ДИ (Sen, 1968, ур. 2.6; как scipy.stats.theilslopes);
#                               больше 10⁶ пар — точный выбор блоками (память O(n)), n > 20 000 — ValueError;
#   sen_slope_batch_means     — тот же наклон по батч-средним (10–30 батчей) с проверкой lag-1 остатков
#                               батч-средних (≥ 0,2 → батчи вдвое длиннее, иначе lag1_ok = False), τ и n_eff;
#   sen_slope_block_bootstrap — ДИ наклона блочным бутстрепом остатков (кольцевые блоки, L по Politis–White);
#   tost_slope                — эквивалентность «тренда нет»: (1 − 2α)-ДИ наклона внутри (−δ, δ); исход
#                               equivalent / not_equivalent / undetermined (ДИ батч-средних с lag1_ok = False);
#   mann_kendall              — тест Манна–Кендалла (только разведочно, см. §12.6: «тренд незначим» ≠ «тренда нет»),
#                               S за O(n log n) по Найту;
#   batch_means_ci            — ДИ среднего коррелированного ряда по батч-средним (Стьюдент, B − 1 степеней свободы);
#   block_bootstrap_mean      — ДИ среднего кольцевым блочным бутстрепом (Politis & Romano, 1992), L по
#                               Politis & White (2004/2009);
#   block_bootstrap_ratio     — ДИ отношения средних двух выровненных рядов тем же бутстрепом;
#   politis_white_block_length, integrated_autocorr_time — выбор длины блока и время автокорреляции;
#   t_quantile / t_cdf        — распределение Стьюдента без scipy (неполная бета-функция, цепная дробь).
# ===========================================================================

def _betacf(a: float, b: float, x: float, max_iter: int = 300, eps: float = 3e-16) -> float:
    """Цепная дробь неполной бета-функции (модифицированный метод Ленца, Numerical Recipes §6.4)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betainc_reg(a: float, b: float, x: float) -> float:
    """Регуляризованная неполная бета-функция I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                 + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(log_front) * _betacf(a, b, x) / a
    return 1.0 - math.exp(log_front) * _betacf(b, a, 1.0 - x) / b


def t_cdf(t: float, df: float) -> float:
    """Функция распределения Стьюдента с df степенями свободы."""
    if df <= 0:
        raise ValueError("df must be positive")
    if math.isinf(t):
        return 1.0 if t > 0 else 0.0
    tail = 0.5 * _betainc_reg(df / 2.0, 0.5, df / (df + t * t))
    return 1.0 - tail if t > 0 else tail


def t_quantile(p: float, df: float) -> float:
    """Квантиль распределения Стьюдента (обращение t_cdf бисекцией; точность ~1e-12)."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    if p == 0.5:
        return 0.0
    if p < 0.5:
        return -t_quantile(1.0 - p, df)
    hi = 1.0
    while t_cdf(hi, df) < p:
        hi *= 2.0
        if hi > 1e12:
            return math.inf
    lo = 0.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo <= 1e-13 * max(1.0, hi):
            break
    return 0.5 * (lo + hi)


def _series(t: Sequence[float], y: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray(t, dtype=float)
    v = np.asarray(y, dtype=float)
    if x.shape != v.shape or x.ndim != 1:
        raise ValueError("t and y must be 1-D sequences of equal length")
    if x.size < 2:
        raise ValueError("need at least 2 points")
    if not (np.all(np.isfinite(x)) and np.all(np.isfinite(v))):
        raise ValueError("non-finite values")
    return x, v


def _tie_sizes(v: np.ndarray) -> np.ndarray:
    """Размеры групп совпадающих значений (только группы ≥ 2)."""
    _, counts = np.unique(v, return_counts=True)
    return counts[counts > 1].astype(float)


def _kendall_var(n: int, x: np.ndarray, y: np.ndarray) -> float:
    """Дисперсия статистики Кендалла S при H0 с поправкой на связки в обеих переменных (Sen, 1968, ур. 2.6)."""
    tx, ty = _tie_sizes(x), _tie_sizes(y)
    return (n * (n - 1) * (2 * n + 5)
            - float(np.sum(tx * (tx - 1) * (2 * tx + 5)))
            - float(np.sum(ty * (ty - 1) * (2 * ty + 5)))) / 18.0


# Пределы для O(n²)-расчётов по всем парам. До DENSE_PAIRS пар — все наклоны в памяти (≈ 50 байт на пару),
# дальше — точный выбор порядковых статистик блоками за один проход (память O(n + блок)); больше SEN_MAX_N
# точек — ValueError: такой ряд надо проредить или свести к батч-средним (время O(n²) всё равно).
DENSE_PAIRS = 1_000_000
SEN_MAX_N = 20_000
BOOT_MAX_N = 3_000
_CHUNK_PAIRS = 1_000_000


def _pairwise_slopes(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Наклоны (y_j − y_i)/(x_j − x_i) по всем парам i < j с x_i ≠ x_j (вся матрица в памяти)."""
    i, j = np.triu_indices(x.size, k=1)
    dx = x[j] - x[i]
    keep = dx != 0
    return (y[j][keep] - y[i][keep]) / dx[keep]


def _slope_blocks(x: np.ndarray, y: np.ndarray, chunk_pairs: int = _CHUNK_PAIRS):
    """Наклоны всех пар i < j с x_i ≠ x_j блоками строк (не больше ≈chunk_pairs пар на блок)."""
    n = x.size
    i0 = 0
    while i0 < n - 1:
        rows = max(1, chunk_pairs // max(1, n - i0 - 1))
        i1 = min(n - 1, i0 + rows)
        cols = np.arange(i0 + 1, n)
        dx = x[None, cols] - x[i0:i1, None]
        dy = y[None, cols] - y[i0:i1, None]
        keep = (cols[None, :] > np.arange(i0, i1)[:, None]) & (dx != 0)
        yield dy[keep] / dx[keep]
        i0 = i1


def _count_pairs(x: np.ndarray) -> int:
    """Число пар i < j с x_i ≠ x_j."""
    n = x.size
    return int(n * (n - 1) // 2 - np.sum(_tie_sizes(x) * (_tie_sizes(x) - 1) / 2))


def _select_slopes(x: np.ndarray, y: np.ndarray, ranks: Sequence[int], nt: int, seed: int = 0,
                   collect_limit: int = 20_000_000) -> list[float]:
    """Точные порядковые статистики (0-базовые ранги) попарных наклонов без хранения всех пар.

    Интервал вокруг каждого ранга берётся по квантилям случайной выборки пар (m до 10⁶, запас 6σ),
    затем за проход блоками считаются наклоны ниже, равные концам и строго внутри интервала (только
    последние хранятся). Ранг вне интервала (вероятность ≪ 10⁻⁶) — интервал расширяется, проход повторяется.
    Связки на концах интервала (плато ряда памяти — много нулевых наклонов) учитываются счётчиками.
    """
    rng = np.random.default_rng(seed)
    n = x.size
    m = int(min(nt, 1_000_000))
    i = rng.integers(0, n, size=int(1.2 * m) + 16)
    j = rng.integers(0, n, size=int(1.2 * m) + 16)
    ok = (i != j) & (x[i] != x[j])
    i, j = i[ok][:m], j[ok][:m]
    sample = np.sort((y[j] - y[i]) / (x[j] - x[i]))
    m = sample.size
    out: dict[int, float] = {}
    widen = 6.0
    pending = sorted(set(int(r) for r in ranks))
    for _ in range(4):
        brackets = []
        for r in pending:
            q = (r + 0.5) / nt
            d = widen * math.sqrt(max(q * (1 - q), 1.0 / m) / m) + 2.0 / m
            klo, khi = int(math.floor((q - d) * m)), int(math.ceil((q + d) * m))
            lo = float(sample[klo]) if klo >= 0 else -math.inf
            hi = float(sample[khi]) if khi < m else math.inf
            brackets.append([r, lo, hi, 0, 0, 0, []])      # ранг, lo, hi, ниже, =lo, =hi, внутри
        collected = 0
        for blk in _slope_blocks(x, y):
            for b in brackets:
                _, lo, hi = b[0], b[1], b[2]
                b[3] += int(np.count_nonzero(blk < lo))
                b[4] += int(np.count_nonzero(blk == lo))
                if hi != lo:
                    b[5] += int(np.count_nonzero(blk == hi))
                    inside = blk[(blk > lo) & (blk < hi)]
                    if inside.size:
                        b[6].append(inside)
                        collected += inside.size
            if collected > collect_limit:
                raise MemoryError("Sen slope selection: interval too wide; thin the series")
        retry = []
        for r, lo, hi, below, eq_lo, eq_hi, parts in brackets:
            inside = np.concatenate(parts) if parts else np.empty(0)
            k = r - below
            if k < 0:
                retry.append(r)
            elif k < eq_lo:
                out[r] = lo
            elif k < eq_lo + inside.size:
                kk = k - eq_lo
                out[r] = float(np.partition(inside, kk)[kk])
            elif hi != lo and k < eq_lo + inside.size + eq_hi:
                out[r] = hi
            else:
                retry.append(r)
        if not retry:
            break
        pending, widen = retry, widen * 4
    if any(r not in out for r in ranks):
        raise RuntimeError("Sen slope selection did not converge")
    return [out[int(r)] for r in ranks]


def sen_slope(t: Sequence[float], y: Sequence[float], conf: float = 0.95, max_n: int = SEN_MAX_N) -> dict:
    """Наклон Тейла–Сена: медиана попарных наклонов; ДИ по Сену (1968, ур. 2.6).

    ДИ — порядковые статистики попарных наклонов с номерами (N ∓ z·σ_S)/2, где N — число пар с
    разными t, σ_S² — дисперсия статистики Кендалла с поправкой на связки (нормальная аппроксимация).
    Реализация совпадает со scipy.stats.theilslopes (те же индексы и округление), свободный член —
    по Коноверу: median(y) − b·median(t) (method="separate" в scipy).

    Память: до DENSE_PAIRS пар — все наклоны сразу; больше — точный выбор блоками (O(n) памяти, тот же
    результат); n > max_n — ValueError (O(n²) времени: проредите ряд или возьмите батч-средние).

    Предполагает независимые остатки; для ряда с автокорреляцией (PSS с шагом 30 с) ДИ будет узким —
    используйте sen_slope_batch_means или sen_slope_block_bootstrap.
    """
    if not 0.0 < conf < 1.0:
        raise ValueError("conf must be in (0, 1)")
    x, v = _series(t, y)
    if max_n and x.size > max_n:
        raise ValueError(f"sen_slope: n = {x.size} > max_n = {max_n} (O(n^2) pairs); thin the series "
                         f"(batch means / binning) first")
    dense = x.size * (x.size - 1) // 2 <= DENSE_PAIRS
    slopes = _pairwise_slopes(x, v) if dense else None
    nt = slopes.size if dense else _count_pairs(x)
    if nt == 0:
        raise ValueError("all t are equal")
    z = _NORMAL.inv_cdf(1.0 - (1.0 - conf) / 2.0)
    var = _kendall_var(x.size, x, v)
    mid = [(nt - 1) // 2, nt // 2]
    if var >= 0:
        sigma = math.sqrt(var)
        # Индексы как в scipy (z там отрицательный): Ru = round((N + z·σ)/2), Rl = round((N − z·σ)/2) − 1.
        r_up = min(int(np.round((nt + z * sigma) / 2.0)), nt - 1)
        r_lo = max(int(np.round((nt - z * sigma) / 2.0)) - 1, 0)
    else:
        r_lo = r_up = None
    ranks = mid + ([r_lo, r_up] if r_lo is not None else [])
    if dense:
        part = np.partition(slopes, sorted(set(ranks)))
        vals = [float(part[r]) for r in ranks]
    else:
        vals = _select_slopes(x, v, ranks, nt)
    slope = 0.5 * (vals[0] + vals[1])            # медиана (как np.median)
    if r_lo is not None:
        lo, hi = vals[2], vals[3]
    else:
        lo = hi = math.nan
    intercept = float(np.median(v) - slope * np.median(x))
    return {"slope": slope, "intercept": intercept, "ci_low": lo, "ci_high": hi, "conf": conf,
            "n": int(x.size), "n_slopes": int(nt), "method": "sen1968" + ("" if dense else " (blockwise selection)")}


def _contiguous_batches(n: int, n_batches: int) -> list[np.ndarray]:
    """Разбиение индексов 0..n−1 на n_batches смежных батчей равной длины; остаток отбрасывается в начале ряда
    (начало ряда — самое «нестационарное», его и так отделяют, §12.2)."""
    if n_batches < 2:
        raise ValueError("need at least 2 batches")
    size = n // n_batches
    if size < 1:
        raise ValueError("fewer points than batches")
    start = n - size * n_batches
    return [np.arange(start + k * size, start + (k + 1) * size) for k in range(n_batches)]


def _lag1(e: np.ndarray, scale: float = 1.0) -> float:
    """Автокорреляция первого порядка (0, если ряд постоянный с точностью до округления относительно scale)."""
    d = e - e.mean()
    den = float(np.dot(d, d))
    tiny = (1e-9 * max(1.0, abs(scale))) ** 2 * d.size
    return float(np.dot(d[:-1], d[1:]) / den) if den > tiny and d.size > 2 else 0.0


def sen_slope_batch_means(t: Sequence[float], y: Sequence[float], n_batches: int = 20,
                          conf: float = 0.95, max_lag1: float = 0.2, min_batches: int = 10) -> dict:
    """Наклон Сена по батч-средним: ряд режется на смежные батчи, наклон и ДИ (Sen, 1968) — по точкам
    (среднее t, среднее y) батчей. Батчи гасят автокорреляцию соседних отсчётов; 10–30 батчей (§12.6).

    Проверка: lag-1 автокорреляция остатков батч-средних от прямой Сена. Если она ≥ max_lag1 (время
    корреляции сравнимо с длиной батча — ДИ недопокрывает, TOST антиконсервативен), число батчей
    уменьшается вдвое (батчи длиннее), но не ниже min_batches; если и тогда ≥ max_lag1 — lag1_ok = False
    (вывод по ДИ «не определён»)."""
    x, v = _series(t, y)
    nb = int(n_batches)
    while True:
        batches = _contiguous_batches(x.size, nb)
        bt = np.array([x[b].mean() for b in batches])
        by = np.array([v[b].mean() for b in batches])
        r = sen_slope(bt, by, conf=conf)
        lag1 = _lag1(by - (r["intercept"] + r["slope"] * bt), float(np.max(np.abs(by))))
        if lag1 < max_lag1 or nb // 2 < min_batches:
            break
        nb //= 2
    tau = integrated_autocorr_time(v - (r["intercept"] + r["slope"] * x))
    r.update({"method": "sen1968 on batch means", "n": int(x.size), "n_batches": nb,
              "n_batches_requested": int(n_batches), "batch_size": int(batches[0].size), "batch_lag1": lag1,
              "max_lag1": max_lag1, "lag1_ok": bool(lag1 < max_lag1), "tau_int": tau, "n_eff": x.size / tau})
    return r


def default_block_length(n: int) -> int:
    """Длина блока по правилу ~n^(1/3) (Hall, Horowitz & Jing, 1995) — без учёта данных (справочно)."""
    return max(1, int(math.ceil(n ** (1.0 / 3.0))))


def _autocov(x: np.ndarray, max_lag: int) -> np.ndarray:
    """Выборочная автоковариация (делитель n) лагов 0..max_lag через БПФ."""
    d = x - x.mean()
    n = d.size
    f = np.fft.rfft(d, 2 * n)
    return np.fft.irfft(f * np.conj(f))[: max_lag + 1] / n


def politis_white_block_length(x: Sequence[float], circular: bool = True) -> int:
    """Длина блока по данным: Politis & White (2004) с поправкой Patton, Politis & White (2009).

    Значимые лаги — до первого m, после которого K_N = max(5, ⌈√log10 n⌉) автокорреляций подряд меньше
    2·√(log10 n / n); окно M = 2m с плоской вершиной (λ = 1 при |t| ≤ ½, 2(1 − |t|) при ½ < |t| ≤ 1);
    b = (2Ĝ²/D̂)^(1/3)·n^(1/3), Ĝ = Σλ(k/M)|k|γ̂(k), D̂ = (4/3)·ĝ(0)² для кольцевого бутстрепа (2·ĝ(0)² —
    для стационарного); ограничение b ≤ min(3√n, n/3). Монте-Карло (AR(1), n = 600, 95 % ДИ среднего):
    φ = 0,8 — покрытие 0,915 против 0,855 у L = ⌈n^(1/3)⌉; φ = 0,95 — 0,82 против 0,61."""
    v = np.asarray(x, dtype=float)
    n = v.size
    if n < 4 or not np.all(np.isfinite(v)):
        return 1
    kn = max(5, int(math.ceil(math.sqrt(math.log10(n)))))
    m_max = int(math.ceil(math.sqrt(n))) + kn
    b_max = int(math.ceil(min(3.0 * math.sqrt(n), n / 3.0)))
    acov = _autocov(v, min(n - 1, m_max + kn))
    if acov[0] <= 0:
        return 1
    rho = acov / acov[0]
    thr = 2.0 * math.sqrt(math.log10(n) / n)
    m_hat = next((m for m in range(1, m_max + 1) if np.all(np.abs(rho[m:m + kn]) < thr)), m_max)
    big_m = max(1, min(2 * m_hat, m_max, acov.size - 1))
    k = np.arange(-big_m, big_m + 1)
    u = np.abs(k) / big_m
    lam = np.where(u <= 0.5, 1.0, np.where(u <= 1.0, 2.0 * (1.0 - u), 0.0))
    g = acov[np.abs(k)]
    big_g = float(np.sum(lam * np.abs(k) * g))
    g0 = float(np.sum(lam * g))
    d = (4.0 / 3.0 if circular else 2.0) * g0 * g0
    if d <= 0 or big_g == 0:
        return 1
    b = (2.0 * big_g * big_g / d) ** (1.0 / 3.0) * n ** (1.0 / 3.0)
    return int(max(1, min(math.ceil(b), b_max)))


def integrated_autocorr_time(x: Sequence[float], c: float = 5.0) -> float:
    """Интегральное время автокорреляции τ = 1 + 2Σρ(k) с автоматическим окном Сокала (M ≥ c·τ(M)).
    n/τ — эффективный размер выборки. На коротком ряду (n/τ ≲ 20) τ занижается — только диагностика."""
    v = np.asarray(x, dtype=float)
    n = v.size
    if n < 4:
        return 1.0
    acov = _autocov(v, n - 1)
    if acov[0] <= 0:
        return 1.0
    rho = acov / acov[0]
    csum = 1.0 + 2.0 * np.cumsum(rho[1:])
    ms = np.arange(1, n)
    ok = ms >= c * csum
    tau = float(csum[np.argmax(ok)]) if ok.any() else float(csum[-1])
    return max(tau, 1.0)


def _circular_block_indices(n: int, block_len: int, n_boot: int, rng: np.random.Generator) -> np.ndarray:
    """Индексы n_boot реплик кольцевого блочного бутстрепа (Politis & Romano, 1992): ⌈n/L⌉ блоков длины L
    со случайным началом, склейка, обрезка до n."""
    if not 1 <= block_len <= n:
        raise ValueError("block_len must be in [1, n]")
    n_blocks = -(-n // block_len)
    starts = rng.integers(0, n, size=(n_boot, n_blocks))
    idx = (starts[:, :, None] + np.arange(block_len)[None, None, :]) % n
    return idx.reshape(n_boot, n_blocks * block_len)[:, :n]


def sen_slope_block_bootstrap(t: Sequence[float], y: Sequence[float], conf: float = 0.95, n_boot: int = 1000,
                              block_len: int | None = None, seed: int = 0) -> dict:
    """Наклон Сена с перцентильным ДИ блочного бутстрепа остатков.

    Остатки e = y − (a + b·t) ресэмплируются кольцевыми блоками длины L (сохраняется автокорреляция),
    дизайн t фиксирован: y* = a + b·t + e*, по каждой реплике — наклон Сена. L по умолчанию — по данным
    (politis_white_block_length остатков). Стоимость O(n_boot·n²): для n ≈ 1000 и n_boot = 1000 — секунды;
    n > BOOT_MAX_N — ValueError (проредите ряд).
    """
    x, v = _series(t, y)
    if x.size > BOOT_MAX_N:
        raise ValueError(f"sen_slope_block_bootstrap: n = {x.size} > {BOOT_MAX_N} (O(n_boot·n²)); thin the series")
    base = sen_slope(x, v, conf=conf)
    b, a = base["slope"], base["intercept"]
    resid = v - (a + b * x)
    n = x.size
    L = politis_white_block_length(resid) if block_len is None else int(block_len)
    rng = np.random.default_rng(seed)
    i, j = np.triu_indices(n, k=1)
    dx = x[j] - x[i]
    keep = dx != 0
    i, j, dx = i[keep], j[keep], dx[keep]
    boot = np.empty(n_boot)
    for r, idx in enumerate(_circular_block_indices(n, L, n_boot, rng)):
        e = resid[idx]
        boot[r] = b + float(np.median((e[j] - e[i]) / dx))
    alpha = 1.0 - conf
    lo, hi = np.quantile(boot, [alpha / 2, 1 - alpha / 2])
    return {"slope": b, "intercept": a, "ci_low": float(lo), "ci_high": float(hi), "conf": conf, "n": int(n),
            "n_boot": int(n_boot), "block_len": L, "seed": seed,
            "method": "sen slope, circular block bootstrap of residuals"}


def tost_slope(t: Sequence[float], y: Sequence[float], delta: float, alpha: float = 0.05, method: str = "batch_means",
               **kwargs) -> dict:
    """TOST «тренда нет»: H0 |β| ≥ δ против H1 |β| < δ для наклона Сена β.

    Решение — через двойственность: эквивалентность принимается, если (1 − 2α)-ДИ наклона лежит строго
    внутри (−δ, δ) (при α = 0,05 — 90 % ДИ, как в H5(а): граница ±1 МиБ/ч). ДИ строится выбранным методом:
    "sen" (независимые остатки), "batch_means" (по умолчанию), "block_bootstrap"; kwargs передаются ему.
    """
    if delta <= 0:
        raise ValueError("delta must be positive")
    if not 0.0 < alpha < 0.5:
        raise ValueError("alpha must be in (0, 0.5)")
    conf = 1.0 - 2.0 * alpha
    funcs = {"sen": sen_slope, "batch_means": sen_slope_batch_means, "block_bootstrap": sen_slope_block_bootstrap}
    if method not in funcs:
        raise ValueError(f"method must be one of {sorted(funcs)}")
    ci = funcs[method](t, y, conf=conf, **kwargs)
    lo, hi = ci["ci_low"], ci["ci_high"]
    inside = bool(math.isfinite(lo) and math.isfinite(hi) and -delta < lo and hi < delta)
    reliable = ci.get("lag1_ok", True)
    # Трёхзначный исход: ДИ батч-средних с сильной остаточной автокорреляцией недопокрывает — эквивалентность
    # по нему не принимается («не определено»); выход ДИ за ±δ — «не эквивалентно» в любом случае.
    decision = ("equivalent" if inside and reliable else "undetermined" if inside else "not_equivalent")
    return {"slope": ci["slope"], "delta": delta, "alpha": alpha, "ci_low": lo, "ci_high": hi, "ci_conf": conf,
            "equivalent": decision == "equivalent", "decision": decision, "reliable": bool(reliable),
            "method": method, "ci": ci}


def _inversions(a: np.ndarray) -> int:
    """Число пар i < j с a_i > a_j (сортировка слиянием снизу вверх, векторно по уровням; O(n log² n))."""
    n = a.size
    if n < 2:
        return 0
    _, cur = np.unique(a, return_inverse=True)     # ранги 0..n−1 (связки — равные ранги)
    cur = cur.astype(np.int64)
    idx = np.arange(n)
    base = np.int64(n + 1)
    inv = 0
    w = 1
    while w < n:
        pair = idx // (2 * w)
        left = (idx % (2 * w)) < w
        key = pair * base + cur
        lk, rk, rp = key[left], key[~left], pair[~left]
        # левые элементы той же пары, большие правого: [searchsorted(rk, 'right'), конец пары)
        inv += int(np.sum(np.searchsorted(lk, (rp + 1) * base) - np.searchsorted(lk, rk, side="right")))
        cur = np.sort(key) - pair * base            # слияние: внутри пары по возрастанию
        w *= 2
    return inv


def _kendall_s(x: np.ndarray, y: np.ndarray) -> float:
    """S = Σ_{i<j} sign(x_j − x_i)·sign(y_j − y_i) за O(n log n) (Knight, 1966): S = C − D, где D — инверсии
    y после сортировки по (x, y), C = N0 − Nx − Ny + Nxy − D."""
    n = x.size
    order = np.lexsort((y, x))
    xs, ys = x[order], y[order]
    n0 = n * (n - 1) // 2
    tx = _tie_sizes(xs)
    ty = _tie_sizes(ys)
    _, joint = np.unique(np.stack([xs, ys], axis=1), axis=0, return_counts=True)
    joint = joint[joint > 1].astype(float)
    nx, ny, nxy = (float(np.sum(c * (c - 1) / 2)) for c in (tx, ty, joint))
    d = _inversions(ys)
    c = n0 - nx - ny + nxy - d
    return float(c - d)


def mann_kendall(t: Sequence[float], y: Sequence[float]) -> dict:
    """Тест Манна–Кендалла на монотонный тренд (разведочно, §12.6).

    S = Σ_{i<j} sign(t_j − t_i)·sign(y_j − y_i) — за O(n log n) по Найту (1966), без матрицы пар;
    Var(S) — с поправкой на связки (как в sen_slope); Z — с поправкой на непрерывность; p — двусторонний
    (нормальная аппроксимация). Автокорреляцию не учитывает (поправка Хамеда–Рао не реализована): на рядах с
    шагом 30 с p-значения занижены.
    """
    x, v = _series(t, y)
    s = _kendall_s(x, v)
    var = _kendall_var(x.size, x, v)
    if var <= 0:
        z = 0.0
    elif s > 0:
        z = (s - 1.0) / math.sqrt(var)
    elif s < 0:
        z = (s + 1.0) / math.sqrt(var)
    else:
        z = 0.0
    p = 2.0 * (1.0 - _NORMAL.cdf(abs(z)))
    n_pairs = x.size * (x.size - 1) / 2.0
    return {"s": s, "var_s": var, "z": z, "p": p, "tau": s / n_pairs, "n": int(x.size)}


def batch_means_ci(x: Sequence[float], n_batches: int = 20, conf: float = 0.95) -> dict:
    """ДИ среднего коррелированного ряда методом батч-средних: mean ± t_{B−1}·s_B/√B."""
    v = np.asarray(x, dtype=float)
    if v.ndim != 1 or not np.all(np.isfinite(v)):
        raise ValueError("x must be a finite 1-D sequence")
    batches = _contiguous_batches(v.size, n_batches)
    means = np.array([v[b].mean() for b in batches])
    est = float(means.mean())
    s = float(means.std(ddof=1))
    q = t_quantile(1.0 - (1.0 - conf) / 2.0, n_batches - 1)
    half = q * s / math.sqrt(n_batches)
    return {"estimate": est, "ci_low": est - half, "ci_high": est + half, "conf": conf, "n": int(v.size),
            "n_batches": n_batches, "batch_size": int(batches[0].size), "batch_sd": s, "t_quantile": q,
            "method": "batch means (Student t)"}


def block_bootstrap_mean(x: Sequence[float], conf: float = 0.95, n_boot: int = 10000, block_len: int | None = None,
                         seed: int = 0) -> dict:
    """Перцентильный ДИ среднего ряда кольцевым блочным бутстрепом (Politis & Romano, 1992).

    block_len по умолчанию — по данным (politis_white_block_length; прежнее правило ⌈n^(1/3)⌉ недопокрывает
    при сильной автокорреляции). Частные случаи: L = 1 — обычный бутстреп; L = n — каждая реплика
    есть циклический сдвиг ряда, поэтому интервал вырождается в выборочное среднее.
    """
    v = np.asarray(x, dtype=float)
    if v.ndim != 1 or v.size == 0 or not np.all(np.isfinite(v)):
        raise ValueError("x must be a non-empty finite 1-D sequence")
    n = v.size
    L = politis_white_block_length(v) if block_len is None else int(block_len)
    rng = np.random.default_rng(seed)
    means = v[_circular_block_indices(n, L, n_boot, rng)].mean(axis=1)
    alpha = 1.0 - conf
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return {"estimate": float(v.mean()), "ci_low": float(lo), "ci_high": float(hi), "conf": conf, "n": int(n),
            "n_boot": int(n_boot), "block_len": L, "seed": seed, "method": "circular block bootstrap"}


def block_bootstrap_ratio(numerator: Sequence[float], denominator: Sequence[float], conf: float = 0.95,
                          n_boot: int = 10000, block_len: int | None = None, seed: int = 0) -> dict:
    """Отношение средних mean(numerator)/mean(denominator) двух выровненных по времени рядов
    (например, вызовы LLM в час у двух политик на одной трассе) и перцентильный ДИ кольцевого блочного
    бутстрепа: одни и те же блоки индексов для обоих рядов — сохраняются и автокорреляция, и парность."""
    a = np.asarray(numerator, dtype=float)
    b = np.asarray(denominator, dtype=float)
    if a.shape != b.shape or a.ndim != 1 or a.size == 0:
        raise ValueError("numerator and denominator must be non-empty 1-D sequences of equal length")
    if not (np.all(np.isfinite(a)) and np.all(np.isfinite(b))):
        raise ValueError("non-finite values")
    n = a.size
    L = max(politis_white_block_length(a), politis_white_block_length(b)) if block_len is None else int(block_len)
    rng = np.random.default_rng(seed)
    idx = _circular_block_indices(n, L, n_boot, rng)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratios = a[idx].mean(axis=1) / b[idx].mean(axis=1)
        estimate = float(a.mean() / b.mean())
    alpha = 1.0 - conf
    lo, hi = np.quantile(ratios, [alpha / 2, 1 - alpha / 2])
    return {"estimate": estimate, "ci_low": float(lo), "ci_high": float(hi), "conf": conf, "n": int(n),
            "n_boot": int(n_boot), "block_len": L, "seed": seed, "method": "circular block bootstrap of ratio of means"}
