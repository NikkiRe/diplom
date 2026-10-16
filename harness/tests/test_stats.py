"""Статистика: проверки на известных значениях и на свойствах, выведенных аналитически."""
from __future__ import annotations

import math
import unittest

import numpy as np

from harness import stats


def _pairs(n11: int, n10: int, n01: int, n00: int) -> tuple[list[int], list[int]]:
    """Парные исходы (a, b) с заданными частотами ячеек (a, b)."""
    a = [1] * n11 + [1] * n10 + [0] * n01 + [0] * n00
    b = [1] * n11 + [0] * n10 + [1] * n01 + [0] * n00
    return a, b


class McNemarTest(unittest.TestCase):
    def test_known_values(self):
        # 0 против 10: p = 2 · 2^-10.
        self.assertAlmostEqual(stats.mcnemar_exact(0, 10)["p"], 2 / 1024, places=15)
        # 3 против 10: p = 2 · (C(13,0)+C(13,1)+C(13,2)+C(13,3)) / 2^13 = 2·378/8192 (R: binom.test(3, 13) → 0.09229).
        self.assertAlmostEqual(stats.mcnemar_exact(3, 10)["p"], 2 * 378 / 8192, places=15)
        self.assertAlmostEqual(stats.mcnemar_exact(3, 10)["p"], 0.0922851562, places=9)
        # mid-p = p − P(X = 3).
        self.assertAlmostEqual(stats.mcnemar_exact(3, 10)["p_mid"], 2 * (378 - 286 / 2) / 8192, places=15)

    def test_symmetry_and_edges(self):
        self.assertEqual(stats.mcnemar_exact(4, 9)["p"], stats.mcnemar_exact(9, 4)["p"])
        self.assertEqual(stats.mcnemar_exact(5, 5)["p"], 1.0)
        self.assertEqual(stats.mcnemar_exact(0, 0)["p"], 1.0)
        with self.assertRaises(ValueError):
            stats.mcnemar_exact(-1, 2)

    def test_from_pairs(self):
        a, b = _pairs(50, 3, 10, 37)
        r = stats.mcnemar_paired(a, b)
        self.assertEqual((r["n"], r["n01"], r["n10"]), (100, 10, 3))
        self.assertEqual(r["p"], stats.mcnemar_exact(10, 3)["p"])

    def test_large_counts_are_exact_and_finite(self):
        r = stats.mcnemar_exact(480, 520)
        self.assertTrue(0 < r["p"] <= 1)
        # Нормальная аппроксимация с поправкой на непрерывность — близко к точному значению.
        z = (abs(480 - 520) - 1) / math.sqrt(1000)
        approx = math.erfc(z / math.sqrt(2))
        self.assertAlmostEqual(r["p"], approx, delta=0.01)


class ClopperPearsonTest(unittest.TestCase):
    def test_known_values(self):
        lo, hi = stats.clopper_pearson(5, 10)
        # R: binom.test(5, 10)$conf.int → 0.1870860 0.8129140
        self.assertAlmostEqual(lo, 0.1870860, places=6)
        self.assertAlmostEqual(hi, 0.8129140, places=6)
        lo, hi = stats.clopper_pearson(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 1 - 0.025 ** 0.1, places=10)  # закрытая форма для k = 0
        lo, hi = stats.clopper_pearson(10, 10)
        self.assertAlmostEqual(lo, 0.025 ** 0.1, places=10)
        self.assertEqual(hi, 1.0)


class HolmTest(unittest.TestCase):
    def test_known_values(self):
        # Сортировка 0.005, 0.01, 0.03, 0.04 → ×4, ×3, ×2, ×1 = 0.02, 0.03, 0.06, 0.04 → монотонизация.
        adjusted = stats.holm([0.01, 0.04, 0.03, 0.005])
        for got, want in zip(adjusted, [0.03, 0.06, 0.06, 0.02]):
            self.assertAlmostEqual(got, want, places=12)

    def test_cap_and_single(self):
        self.assertEqual(stats.holm([0.6, 0.7]), [1.0, 1.0])
        self.assertEqual(stats.holm([0.2]), [0.2])
        self.assertEqual(stats.holm([]), [])


class BootstrapTest(unittest.TestCase):
    def test_degenerate_ratios(self):
        x = np.array([10.0, 12.0, 9.0, 15.0, 11.0])
        r = stats.paired_bootstrap_ratio(x, x, n_boot=500)
        self.assertEqual((r["estimate"], r["ci_low"], r["ci_high"]), (1.0, 1.0, 1.0))
        r = stats.paired_bootstrap_ratio(x, 2 * x, stat="mean", n_boot=500)
        self.assertAlmostEqual(r["estimate"], 2.0)
        self.assertAlmostEqual(r["ci_low"], 2.0)
        self.assertAlmostEqual(r["ci_high"], 2.0)

    def test_deterministic_with_seed(self):
        rng = np.random.default_rng(1)
        x = rng.lognormal(0, 0.5, 40)
        y = x * rng.lognormal(-0.2, 0.1, 40)
        r1 = stats.paired_bootstrap_ratio(x, y, n_boot=2000, seed=7)
        r2 = stats.paired_bootstrap_ratio(x, y, n_boot=2000, seed=7)
        self.assertEqual(r1, r2)
        self.assertLess(r1["ci_low"], r1["estimate"])
        self.assertLess(r1["estimate"], r1["ci_high"])

    def test_pairing_narrows_interval(self):
        # При сильной корреляции пар парный ДИ должен быть уже, чем при перемешанных парах.
        rng = np.random.default_rng(2)
        x = rng.lognormal(0, 1.0, 60)
        y = 0.8 * x * rng.lognormal(0, 0.05, 60)
        paired = stats.paired_bootstrap_ratio(x, y, stat="mean", n_boot=3000)
        shuffled = stats.paired_bootstrap_ratio(x, rng.permutation(y), stat="mean", n_boot=3000)
        self.assertLess(paired["ci_high"] - paired["ci_low"], 0.5 * (shuffled["ci_high"] - shuffled["ci_low"]))

    def test_coverage(self):
        # Отношение средних известно (0.8): доля ДИ, накрывших его, близка к номиналу 95%.
        rng = np.random.default_rng(3)
        hits = 0
        trials = 200
        for i in range(trials):
            x = rng.gamma(4.0, 1.0, 60)
            y = 0.8 * rng.gamma(4.0, 1.0, 60) * 0.5 + 0.8 * x * 0.5
            r = stats.paired_bootstrap_ratio(x, y, stat="mean", n_boot=1000, seed=i)
            hits += r["ci_low"] <= 0.8 <= r["ci_high"]
        self.assertGreater(hits / trials, 0.88)
        self.assertLess(hits / trials, 0.99)

    def test_input_validation(self):
        with self.assertRaises(ValueError):
            stats.paired_bootstrap_ratio([1, 2], [1])
        with self.assertRaises(ValueError):
            stats.paired_bootstrap_ratio([1, 2], [1, float("nan")])
        with self.assertRaises(ValueError):
            stats.paired_bootstrap_ratio([1, 2], [1, 2], stat="mode")


class PairedDifferenceTest(unittest.TestCase):
    def test_wald_known_values(self):
        # n = 100, b решила 15 задач, которые не решила a, и провалила 5 решённых a:
        # diff = 0.10, SE = sqrt(20 − 10²/100)/100 = sqrt(19)/100.
        a, b = _pairs(60, 5, 15, 20)
        r = stats.paired_diff_ci(a, b, method="wald")
        se = math.sqrt(19) / 100
        z = 1.959963984540054
        self.assertAlmostEqual(r["diff"], 0.10)
        self.assertAlmostEqual(r["ci_low"], 0.10 - z * se, places=12)
        self.assertAlmostEqual(r["ci_high"], 0.10 + z * se, places=12)

    def test_tango_reduces_to_mcnemar_at_zero(self):
        for x, y, n in [(15, 5, 100), (3, 9, 40), (0, 4, 30), (7, 7, 50)]:
            expected = (x - y) / math.sqrt(x + y)
            self.assertAlmostEqual(stats.tango_z(x, y, n, 0.0), expected, places=12)

    def test_tango_rmle_matches_numerical_maximum(self):
        for x, y, n, delta in [(15, 5, 100, 0.05), (15, 5, 100, -0.1), (0, 7, 50, 0.1), (7, 0, 50, -0.05),
                               (3, 3, 30, 0.2), (20, 1, 25, -0.3)]:
            q = stats.tango_rmle(x, y, n, delta)
            lo, hi = max(0.0, -delta), (1 - delta) / 2
            grid = np.linspace(lo, hi, 400001)[1:-1]
            m = n - x - y
            with np.errstate(divide="ignore", invalid="ignore"):
                loglik = ((x * np.log(grid + delta)) if x else 0) + ((y * np.log(grid)) if y else 0) \
                    + ((m * np.log(1 - 2 * grid - delta)) if m else 0)
            self.assertAlmostEqual(q, grid[np.argmax(loglik)], delta=2 * (hi - lo) / 400000)

    def test_tango_monotone_and_ci_inversion(self):
        a, b = _pairs(60, 5, 15, 20)
        zs = [stats.tango_z(15, 5, 100, d) for d in np.linspace(-0.9, 0.9, 181)]
        self.assertTrue(all(z1 > z2 for z1, z2 in zip(zs, zs[1:])))
        r = stats.paired_diff_ci(a, b, method="tango")
        z = 1.959963984540054
        self.assertAlmostEqual(stats.tango_z(15, 5, 100, r["ci_low"]), z, places=6)
        self.assertAlmostEqual(stats.tango_z(15, 5, 100, r["ci_high"]), -z, places=6)
        self.assertLess(r["ci_low"], r["diff"])
        self.assertLess(r["diff"], r["ci_high"])
        # Перестановка конфигураций отражает интервал.
        r_swapped = stats.paired_diff_ci(b, a, method="tango")
        self.assertAlmostEqual(r_swapped["ci_low"], -r["ci_high"], places=9)
        self.assertAlmostEqual(r_swapped["ci_high"], -r["ci_low"], places=9)

    def test_tango_no_discordant_pairs(self):
        a, b = _pairs(70, 0, 0, 30)
        r = stats.paired_diff_ci(a, b, method="tango")
        self.assertEqual(r["diff"], 0.0)
        self.assertLess(r["ci_low"], 0.0)
        self.assertGreater(r["ci_high"], 0.0)
        self.assertLess(r["ci_high"], 0.1)


class TostTest(unittest.TestCase):
    def test_identical_large_sample_is_equivalent(self):
        a, b = _pairs(300, 4, 5, 191)
        for method in ("tango", "wald"):
            r = stats.tost_paired(a, b, delta=0.05, method=method)
            self.assertTrue(r["equivalent"], (method, r))

    def test_real_difference_is_not_equivalent(self):
        a, b = _pairs(60, 2, 12, 26)  # diff = +0.10
        for method in ("tango", "wald"):
            r = stats.tost_paired(a, b, delta=0.05, method=method)
            self.assertFalse(r["equivalent"], method)
            self.assertAlmostEqual(r["diff"], 0.10)

    def test_tost_decision_matches_ci_inclusion(self):
        # Двойственность: p < α ⇔ (1−2α)-ДИ лежит внутри (−δ, δ).
        rng = np.random.default_rng(5)
        for _ in range(60):
            n10, n01 = rng.integers(0, 15, size=2)
            a, b = _pairs(int(rng.integers(20, 200)), int(n10), int(n01), int(rng.integers(0, 60)))
            for method in ("tango", "wald"):
                r = stats.tost_paired(a, b, delta=0.08, method=method)
                inside = -0.08 < r["ci_low"] and r["ci_high"] < 0.08
                self.assertEqual(r["equivalent"], inside, (method, r))

    def test_wald_known_value(self):
        a, b = _pairs(60, 5, 15, 20)  # diff = 0.10, SE = sqrt(19)/100
        r = stats.tost_paired(a, b, delta=0.15, method="wald")
        se = math.sqrt(19) / 100
        p_upper = 0.5 * math.erfc(-((0.10 - 0.15) / se) / math.sqrt(2))
        p_lower = 1 - 0.5 * math.erfc(-((0.10 + 0.15) / se) / math.sqrt(2))
        self.assertAlmostEqual(r["p_upper"], p_upper, places=12)
        self.assertAlmostEqual(r["p_lower"], p_lower, places=12)
        self.assertAlmostEqual(r["p"], max(p_upper, p_lower), places=12)


class AAFloorTest(unittest.TestCase):
    def test_binary_floor(self):
        a, b = _pairs(80, 2, 3, 15)
        r = stats.aa_floor_binary(a, b)
        self.assertEqual((r["n"], r["discordant"]), (100, 5))
        self.assertAlmostEqual(r["rate"], 0.05)
        self.assertEqual((r["ci_low"], r["ci_high"]), stats.clopper_pearson(5, 100))
        self.assertEqual(r["mcnemar"]["p"], stats.mcnemar_exact(3, 2)["p"])

    def test_ratio_floor(self):
        rng = np.random.default_rng(4)
        x = rng.lognormal(0, 0.3, 80)
        noise = rng.lognormal(0, 1.0, 80)
        floors = []
        for sigma in (0.02, 0.10):  # A/A-разброс времени по задачам ~2% и ~10%
            y = x * noise ** sigma
            r = stats.aa_floor_ratio(x, y, n_boot=2000)
            self.assertGreater(r["floor"], 0.0)
            self.assertLess(r["floor"], 0.1)
            self.assertLess(r["abs_log_ratio_p50"], r["abs_log_ratio_p95"])
            self.assertAlmostEqual(r["abs_log_ratio_p50"], float(np.median(np.abs(np.log(noise)))) * sigma, places=9)
            floors.append(r["floor"])
        self.assertLess(floors[0], floors[1])



# ---------------------------------------------------------------------------
# Временные ряды: наклон Сена, TOST на наклоне, блочный бутстреп (добавлено для ВКР EdgeCtx)
# ---------------------------------------------------------------------------

try:  # scipy — только эталон в тестах; сам stats.py от scipy не зависит
    import scipy.stats as _sps
except ImportError:  # pragma: no cover
    _sps = None


class StudentTTest(unittest.TestCase):
    def test_known_quantiles(self):
        # Табличные значения: t_{0.975}(1) = 12.7062047, t_{0.975}(10) = 2.2281389, t_{0.95}(19) = 1.7291328.
        self.assertAlmostEqual(stats.t_quantile(0.975, 1), 12.706204736174698, places=8)
        self.assertAlmostEqual(stats.t_quantile(0.975, 10), 2.2281388519649385, places=9)
        self.assertAlmostEqual(stats.t_quantile(0.95, 19), 1.7291328115213678, places=9)
        self.assertEqual(stats.t_quantile(0.5, 7), 0.0)
        self.assertAlmostEqual(stats.t_quantile(0.025, 10), -stats.t_quantile(0.975, 10), places=12)
        # df = 1 — распределение Коши: F(t) = 1/2 + atan(t)/π.
        for t in (-3.0, -0.5, 0.2, 4.0):
            self.assertAlmostEqual(stats.t_cdf(t, 1), 0.5 + math.atan(t) / math.pi, places=12)

    @unittest.skipIf(_sps is None, "scipy недоступен")
    def test_matches_scipy(self):
        for df in (2, 3, 5, 9, 29, 200):
            for p in (0.6, 0.9, 0.95, 0.975, 0.995):
                self.assertAlmostEqual(stats.t_quantile(p, df), float(_sps.t.ppf(p, df)), places=8)


class SenSlopeTest(unittest.TestCase):
    def test_hand_computed(self):
        # Попарные наклоны: шесть единиц, 10, 13, 19, 37 → медиана 1; свободный член median(y) − 1·median(t) = 0.
        r = stats.sen_slope([0, 1, 2, 3, 4], [0, 1, 2, 3, 40])
        self.assertEqual(r["slope"], 1.0)
        self.assertEqual(r["intercept"], 0.0)
        self.assertEqual(r["n_slopes"], 10)
        self.assertLessEqual(r["ci_low"], 1.0)
        self.assertGreaterEqual(r["ci_high"], 1.0)

    def test_exact_line_and_robustness(self):
        t = np.arange(50, dtype=float)
        y = 3.0 - 0.25 * t
        r = stats.sen_slope(t, y)
        self.assertAlmostEqual(r["slope"], -0.25, places=12)
        self.assertAlmostEqual(r["intercept"], 3.0, places=12)
        self.assertAlmostEqual(r["ci_low"], -0.25, places=12)
        self.assertAlmostEqual(r["ci_high"], -0.25, places=12)
        y2 = y.copy()
        y2[[3, 17, 40]] += [500.0, -800.0, 1e6]  # выбросы почти не сдвигают оценку Сена
        self.assertAlmostEqual(stats.sen_slope(t, y2)["slope"], -0.25, places=12)

    @unittest.skipIf(_sps is None, "scipy недоступен")
    def test_matches_scipy_theilslopes_with_ties(self):
        rng = np.random.default_rng(11)
        for n, conf in [(6, 0.95), (25, 0.9), (80, 0.95), (301, 0.99)]:
            t = np.round(rng.uniform(0, 8, n), 1)          # связки в t
            y = np.round(0.4 * t + rng.normal(0, 1.5, n))  # связки в y
            mine = stats.sen_slope(t, y, conf=conf)
            ref = _sps.theilslopes(y, t, conf)
            self.assertEqual(mine["slope"], float(ref.slope))
            self.assertAlmostEqual(mine["intercept"], float(ref.intercept), places=12)
            self.assertEqual(mine["ci_low"], float(ref.low_slope))
            self.assertEqual(mine["ci_high"], float(ref.high_slope))

    def test_validation(self):
        with self.assertRaises(ValueError):
            stats.sen_slope([1, 1, 1], [1, 2, 3])
        with self.assertRaises(ValueError):
            stats.sen_slope([1, 2], [1])
        with self.assertRaises(ValueError):
            stats.sen_slope([1, 2, 3], [1, float("nan"), 3])

    def test_batch_means(self):
        # Точная прямая: батч-средние лежат на ней же, наклон и ДИ совпадают с истинным.
        t = np.arange(1000) * 30.0 / 3600.0
        r = stats.sen_slope_batch_means(t, 100.0 + 0.5 * t, n_batches=20)
        self.assertAlmostEqual(r["slope"], 0.5, places=10)
        self.assertAlmostEqual(r["ci_low"], 0.5, places=10)
        self.assertEqual((r["n_batches"], r["batch_size"]), (20, 50))
        with self.assertRaises(ValueError):
            stats.sen_slope_batch_means(t[:10], t[:10], n_batches=20)

    def test_block_bootstrap_ci_wider_than_iid_on_autocorrelated_series(self):
        # AR(1) с φ = 0.95 вокруг нулевого тренда: независимый ДИ Сена слишком узок, блочный — шире.
        rng = np.random.default_rng(12)
        n = 400
        e = np.zeros(n)
        for k in range(1, n):
            e[k] = 0.95 * e[k - 1] + rng.normal(0, 0.1)
        t = np.arange(n) * 30.0 / 3600.0
        iid = stats.sen_slope(t, 80.0 + e)
        bb = stats.sen_slope_block_bootstrap(t, 80.0 + e, n_boot=300, block_len=40, seed=1)
        self.assertEqual(bb["slope"], iid["slope"])
        self.assertGreater(bb["ci_high"] - bb["ci_low"], 1.5 * (iid["ci_high"] - iid["ci_low"]))
        again = stats.sen_slope_block_bootstrap(t, 80.0 + e, n_boot=300, block_len=40, seed=1)
        self.assertEqual(bb, again)


class LargeSeriesTest(unittest.TestCase):
    """Ревью: Сен и Манн–Кендалл строили все пары (np.triu_indices) — O(n²) памяти, день по 1 с не считался."""

    @unittest.skipIf(_sps is None, "scipy недоступен")
    def test_blockwise_selection_is_exact(self):
        rng = np.random.default_rng(21)
        old = stats.DENSE_PAIRS
        stats.DENSE_PAIRS = 10                      # принудительно — путь точного выбора блоками
        try:
            for n, conf in [(40, 0.95), (300, 0.9), (1600, 0.95)]:
                t = np.round(rng.uniform(0, 8, n), 1)
                y = np.round(0.4 * t + rng.normal(0, 1.5, n))
                mine = stats.sen_slope(t, y, conf=conf)
                ref = _sps.theilslopes(y, t, conf)
                self.assertIn("blockwise", mine["method"])
                self.assertEqual((mine["slope"], mine["ci_low"], mine["ci_high"]),
                                 (float(ref.slope), float(ref.low_slope), float(ref.high_slope)))
            # плато памяти: масса равных наклонов на границе интервала выбора
            t = np.arange(3000) / 3600.0
            y = np.repeat(np.arange(30) * 0.1, 100)
            blk = stats.sen_slope(t, y)
            stats.DENSE_PAIRS = 10 ** 8             # тот же ряд плотным путём
            dense = stats.sen_slope(t, y)
        finally:
            stats.DENSE_PAIRS = old
        self.assertEqual((blk["slope"], blk["ci_low"], blk["ci_high"]),
                         (dense["slope"], dense["ci_low"], dense["ci_high"]))

    def test_memory_bounded_and_guard(self):
        import tracemalloc
        rng = np.random.default_rng(22)
        n = 12000                                   # 7,2·10⁷ пар: прежде ≈ 1,7 ГиБ пикового RSS
        t = np.arange(n) / 3600.0
        y = 50 + 0.01 * t + rng.normal(0, 0.3, n)
        tracemalloc.start()
        r = stats.sen_slope(t, y)
        stats.mann_kendall(t, y)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        self.assertLess(peak, 200 * 2 ** 20)
        self.assertLess(r["ci_low"], r["slope"])
        with self.assertRaises(ValueError):
            stats.sen_slope(np.arange(20001.0), np.arange(20001.0))
        with self.assertRaises(ValueError):
            stats.sen_slope_block_bootstrap(np.arange(4000.0), np.arange(4000.0))

    def test_knight_mann_kendall_equals_pairwise(self):
        rng = np.random.default_rng(23)
        for n in (2, 3, 10, 57, 400, 1001):
            x = np.round(rng.uniform(0, 5, n))      # связки по обеим переменным
            y = np.round(rng.normal(0, 2, n))
            i, j = np.triu_indices(n, k=1)
            want = float(np.sum(np.sign(x[j] - x[i]) * np.sign(y[j] - y[i])))
            self.assertEqual(stats.mann_kendall(x, y)["s"], want, n)


class AutocorrelationTest(unittest.TestCase):
    """Ревью (minor): ДИ батч-средних недопокрывает при длинной автокорреляции — проверка lag-1, L по данным."""

    def test_batch_lag1_halves_batches_and_flags(self):
        rng = np.random.default_rng(31)
        n = 1800
        t = np.arange(n) * 30 / 3600
        e = np.zeros(n)
        z = rng.normal(0, 0.3 * math.sqrt(1 - 0.995 ** 2), n)
        for i in range(1, n):
            e[i] = 0.995 * e[i - 1] + z[i]
        r = stats.sen_slope_batch_means(t, 50 + 0.2 * t + e, n_batches=20)
        self.assertIn(r["n_batches"], (10, 20))
        self.assertGreater(r["tau_int"], 10)
        flat = stats.sen_slope_batch_means(t, 50 + 0.2 * t + rng.normal(0, 0.3, n), n_batches=20)
        self.assertEqual((flat["n_batches"], flat["lag1_ok"]), (20, True))
        # постоянные остатки: lag-1 = 0, а не NaN
        exact = stats.sen_slope_batch_means(t, 1 + 0.5 * t, n_batches=20)
        self.assertEqual((exact["batch_lag1"], exact["lag1_ok"]), (0.0, True))

    def test_tost_undetermined_when_batch_means_autocorrelated(self):
        t = np.arange(400) / 60.0
        y = 10 + 0.01 * np.sin(t / 2.0)              # медленная волна: соседние батч-средние сильно связаны
        r = stats.tost_slope(t, y, delta=1.0, n_batches=20)
        self.assertEqual((r["decision"], r["equivalent"], r["reliable"]), ("undetermined", False, False))
        self.assertLess(r["ci_high"], 1.0)

    def test_politis_white_block_length(self):
        rng = np.random.default_rng(32)
        self.assertLessEqual(stats.politis_white_block_length(rng.normal(size=600)), 6)
        e = np.zeros(600)
        z = rng.normal(size=600)
        for i in range(1, 600):
            e[i] = 0.8 * e[i - 1] + z[i]
        self.assertGreater(stats.politis_white_block_length(e), stats.default_block_length(600))
        self.assertEqual(stats.politis_white_block_length(np.ones(50)), 1)


class TostSlopeTest(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(13)
        self.t = np.arange(960) * 30.0 / 3600.0  # 8 ч с шагом 30 с, в часах
        self.noise = rng.normal(0, 0.3, self.t.size)

    def test_flat_series_is_equivalent(self):
        for method in ("sen", "batch_means", "block_bootstrap"):
            kw = {"n_boot": 200} if method == "block_bootstrap" else {}
            r = stats.tost_slope(self.t, 60.0 + self.noise, delta=1.0, method=method, **kw)
            self.assertTrue(r["equivalent"], (method, r["ci_low"], r["ci_high"]))
            self.assertAlmostEqual(r["ci_conf"], 0.90)

    def test_real_trend_is_not_equivalent(self):
        r = stats.tost_slope(self.t, 60.0 + 2.0 * self.t + self.noise, delta=1.0)
        self.assertFalse(r["equivalent"])
        self.assertAlmostEqual(r["slope"], 2.0, delta=0.2)

    def test_decision_matches_ci_inclusion(self):
        for beta in (-1.2, -0.95, -0.5, 0.0, 0.7, 0.99, 1.3):
            r = stats.tost_slope(self.t, 60.0 + beta * self.t + self.noise, delta=1.0, method="sen")
            ci = stats.sen_slope(self.t, 60.0 + beta * self.t + self.noise, conf=0.90)
            self.assertEqual(r["equivalent"], -1.0 < ci["ci_low"] and ci["ci_high"] < 1.0, beta)

    def test_validation(self):
        with self.assertRaises(ValueError):
            stats.tost_slope(self.t, self.noise, delta=0.0)
        with self.assertRaises(ValueError):
            stats.tost_slope(self.t, self.noise, delta=1.0, method="ols")


class MannKendallTest(unittest.TestCase):
    def test_known_value(self):
        # Строго возрастающий ряд из 10 точек: S = 45, Var = 10·9·25/18 = 125, Z = 44/√125.
        r = stats.mann_kendall(np.arange(10), np.arange(10) ** 2)
        self.assertEqual(r["s"], 45.0)
        self.assertAlmostEqual(r["var_s"], 125.0)
        self.assertAlmostEqual(r["z"], 44 / math.sqrt(125), places=12)
        self.assertAlmostEqual(r["tau"], 1.0)
        self.assertAlmostEqual(r["p"], math.erfc(44 / math.sqrt(125) / math.sqrt(2)), places=12)

    def test_ties_reduce_variance(self):
        r = stats.mann_kendall([1, 2, 3, 4, 5, 6], [1, 1, 2, 2, 3, 3])
        # Связки y: три группы по 2 → Var = (6·5·17 − 3·2·1·9)/18 = (510 − 54)/18.
        self.assertAlmostEqual(r["var_s"], (510 - 54) / 18)
        self.assertEqual(r["s"], 12.0)


class BatchMeansTest(unittest.TestCase):
    def test_known_value(self):
        # 4 батча по 2 точки: средние 1.5, 3.5, 5.5, 7.5 → mean 4.5, sd = √(20/3), t_{0.975}(3) = 3.18244630528.
        r = stats.batch_means_ci(np.arange(1, 9, dtype=float), n_batches=4)
        sd = math.sqrt(20 / 3)
        half = 3.182446305284263 * sd / 2
        self.assertAlmostEqual(r["estimate"], 4.5)
        self.assertAlmostEqual(r["ci_low"], 4.5 - half, places=8)
        self.assertAlmostEqual(r["ci_high"], 4.5 + half, places=8)

    def test_remainder_dropped_at_start(self):
        r = stats.batch_means_ci(np.r_[1000.0, np.ones(20)], n_batches=10)
        self.assertEqual(r["estimate"], 1.0)  # первый (лишний) отсчёт отброшен


class BlockBootstrapTest(unittest.TestCase):
    def test_full_length_block_is_degenerate(self):
        # L = n: каждая реплика — циклический сдвиг, среднее не меняется.
        x = np.array([3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0])
        r = stats.block_bootstrap_mean(x, block_len=x.size, n_boot=500)
        self.assertAlmostEqual(r["estimate"], x.mean())
        self.assertAlmostEqual(r["ci_low"], x.mean(), places=12)
        self.assertAlmostEqual(r["ci_high"], x.mean(), places=12)

    def test_constant_and_proportional_series(self):
        r = stats.block_bootstrap_mean(np.full(30, 7.0), n_boot=200)
        self.assertEqual((r["ci_low"], r["ci_high"]), (7.0, 7.0))
        x = np.random.default_rng(14).gamma(2.0, 1.0, 100)
        rr = stats.block_bootstrap_ratio(2.5 * x, x, n_boot=500)
        self.assertAlmostEqual(rr["estimate"], 2.5)
        self.assertAlmostEqual(rr["ci_low"], 2.5)
        self.assertAlmostEqual(rr["ci_high"], 2.5)

    def test_default_block_length(self):
        self.assertEqual(stats.default_block_length(1000), 10)
        self.assertEqual(stats.default_block_length(960), 10)
        self.assertEqual(stats.default_block_length(1), 1)

    def test_block_bootstrap_covers_mean_of_ar1_better_than_iid(self):
        # AR(1), φ = 0.8, истинное среднее 0: обычный бутстреп (L = 1) недопокрывает, блочный — ближе к 95 %.
        rng = np.random.default_rng(15)
        trials, n = 120, 300
        hits_iid = hits_block = 0
        for k in range(trials):
            e = np.zeros(n)
            noise = rng.normal(0, 1, n)
            for i in range(1, n):
                e[i] = 0.8 * e[i - 1] + noise[i]
            iid = stats.block_bootstrap_mean(e, block_len=1, n_boot=400, seed=k)
            blk = stats.block_bootstrap_mean(e, block_len=25, n_boot=400, seed=k)
            hits_iid += iid["ci_low"] <= 0.0 <= iid["ci_high"]
            hits_block += blk["ci_low"] <= 0.0 <= blk["ci_high"]
        self.assertLess(hits_iid / trials, 0.75)
        self.assertGreater(hits_block / trials, 0.80)

    def test_validation(self):
        with self.assertRaises(ValueError):
            stats.block_bootstrap_mean([1.0, 2.0], block_len=3)
        with self.assertRaises(ValueError):
            stats.block_bootstrap_ratio([1.0, 2.0], [1.0])


if __name__ == "__main__":
    unittest.main()
