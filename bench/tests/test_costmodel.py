"""costmodel: прайс из price_table.py, паритет порта cost_model.py с исходником, прогнозы H1 на известных значениях."""
from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.costmodel import (PROFILES, H1Params, PriceBook, Workload, expected_k, h1_breakeven_commands,  # noqa: E402
                             h1_ratio, h1_table, heartbeat_ticks, keep_warm_period, normalize_model_id,
                             prereg_forecasts, simulate)
from bench.costmodel.forecast import background_share_events, daily_cost_bounds  # noqa: E402
from bench.costmodel.history_model import STRATEGIES, TTLS, legacy_report  # noqa: E402

D_ART = ROOT / "research" / "new_topic" / "D_artifacts"


def _load_original() -> ModuleType:
    """Исходник research/.../cost_model.py — исполняется без импорта (не писать __pycache__ в research/)."""
    path = D_ART / "cost_model.py"
    mod = ModuleType("orig_cost_model")
    mod.__file__ = str(path)
    sys.modules[mod.__name__] = mod   # dataclasses ищут модуль класса в sys.modules
    try:
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), mod.__dict__)  # noqa: S102
    finally:
        sys.modules.pop(mod.__name__, None)
    return mod


class PricesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = PriceBook.load()

    def test_normalize(self):
        self.assertEqual(normalize_model_id("gpt-4o-mini"), "openai/gpt-4o-mini")
        self.assertEqual(normalize_model_id("openai/gpt-5-mini@provider=openai&allow_fallbacks=false"),
                         "openai/gpt-5-mini")
        self.assertEqual(normalize_model_id(" openai/gpt-4.1-nano "), "openai/gpt-4.1-nano")

    def test_known_prices_from_snapshot(self):
        # THESIS_CONCEPT §14 (снимок RouterAI 06.10.2026), ₽ за 1M: вход / кэш / выход.
        expected = {"gpt-4o-mini": (16.56, 8.28, 66.25), "gpt-4.1-nano": (11.04, 2.76, 44.16),
                    "gpt-5-nano": (5.52, 0.55, 44.16), "gpt-5-mini": (27.60, 2.76, 220.82)}
        for model, (i, c, o) in expected.items():
            pm = self.book.price(model).per_million()
            self.assertAlmostEqual(pm["input"], i, delta=0.006, msg=model)
            self.assertAlmostEqual(pm["cached"], c, delta=0.006, msg=model)
            self.assertAlmostEqual(pm["output"], o, delta=0.006, msg=model)

    def test_call_cost_formula(self):
        p = self.book.price("gpt-4o-mini")
        got = self.book.call_cost_rub("gpt-4o-mini", 3000, 2048, 150)
        want = (3000 - 2048) * p.input + 2048 * p.cached + 150 * p.output
        self.assertAlmostEqual(got, want, places=12)
        self.assertAlmostEqual(self.book.call_cost_rub("gpt-4o-mini", 1_000_000, 0, 0), p.input * 1e6, places=9)
        with self.assertRaises(ValueError):
            self.book.call_cost_rub("gpt-4o-mini", 10, 11, 0)
        with self.assertRaises(KeyError):
            self.book.price("no-such-model")

    def test_provenance(self):
        prov = self.book.provenance()
        self.assertEqual(prov["snapshot_date"], "2026-10-06")
        self.assertEqual(len(prov["snapshot_sha256"]), 64)


class HistoryPortTest(unittest.TestCase):
    """Порт должен совпадать с исходником D_artifacts/cost_model.py до единицы токена."""

    @classmethod
    def setUpClass(cls):
        cls.orig = _load_original()

    def test_same_totals_as_original(self):
        for R in (3, 10, 40, 200):
            for hours in (8, 16):
                w_orig = self.orig.W(hours=hours, R=R)
                w_port = Workload(hours=hours, R=R)
                for strategy, _ in STRATEGIES:
                    for _, ttl in TTLS:
                        self.assertEqual(simulate(w_port, strategy, ttl).as_tuple(),
                                         tuple(self.orig.run(w_orig, strategy, ttl)), (R, hours, strategy, ttl))

    def test_report_matches_saved_outputs(self):
        for R in (10, 40, 200):
            saved = (D_ART / f"out_R{R}.txt").read_text(encoding="utf-8").splitlines()
            ours = legacy_report(Workload(R=R)).splitlines()
            self.assertEqual(ours, saved[1:], R)   # первая строка — repr класса параметров (W → Workload)

    def test_unknown_strategy(self):
        with self.assertRaises(ValueError):
            simulate(Workload(), "lru", 0)


class H1Test(unittest.TestCase):
    def test_expected_k_and_ticks(self):
        self.assertAlmostEqual(expected_k(), 2.89, places=12)
        self.assertEqual(heartbeat_ticks(16 * 3600, 1800), 31)   # окно 07–23, 30 мин (§6 концепции)
        self.assertEqual(heartbeat_ticks(8 * 3600, 1800), 15)    # окно T2 14–22
        self.assertEqual(heartbeat_ticks(100, 30), 3)            # 30, 60, 90
        self.assertEqual(heartbeat_ticks(90, 30), 2)             # 30, 60 (тик в конце окна не считается)
        self.assertAlmostEqual(keep_warm_period(300), 270.0)
        with self.assertRaises(ValueError):
            expected_k({1: 0.5, 2: 0.4})

    def test_effective_k_for_tool_requiring_inputs(self):
        from bench.costmodel import expected_k_effective
        self.assertAlmostEqual(expected_k_effective(), 0.21 * 2 + 0.27 * 2 + 0.23 * 3 + 0.29 * 5, places=12)
        self.assertAlmostEqual(expected_k_effective(min_calls=1), expected_k(), places=12)
        self.assertAlmostEqual(prereg_forecasts()["h1_t0_oracle_k_eff"]["params"]["expected_k"], 3.10, places=12)

    def test_concept_reference_values(self):
        # (31 + 10,1·2,89) / (4 + 10,1·2,89) = 1,81; M → 2,35; H (30,44) → ≈1,29 (план ВКР, гипотеза H1).
        self.assertAlmostEqual(h1_ratio(10.1, 31, 4, 2.89), (31 + 29.189) / (4 + 29.189), places=12)
        self.assertEqual(round(h1_ratio(10.1, 31, 4, 2.89), 2), 1.81)
        self.assertEqual(round(h1_ratio(5.55, 31, 4, 2.89), 2), 2.35)
        self.assertEqual(round(h1_ratio(30.44, 31, 4, 2.89), 2), 1.29)
        rows = {r["profile"]: r for r in h1_table()["rows"]}
        self.assertEqual(round(rows["M+"]["ratio_hb_to_gate"], 2), 1.81)
        self.assertAlmostEqual(rows["L"]["background_share_heartbeat"], 31 / (31 + 2.61 * 2.89))

    def test_ratio_monotone_in_commands(self):
        ratios = [h1_ratio(c, 31, 4, 2.89) for c in sorted(PROFILES.values())]
        self.assertTrue(all(a > b for a, b in zip(ratios, ratios[1:])))
        self.assertTrue(all(r > 1 for r in ratios))
        self.assertEqual(h1_ratio(0, 0, 0, 2.89), math.inf)

    def test_breakeven_inverts_ratio(self):
        for target in (1.1, 1.28, 1.45, 2.0):
            c = h1_breakeven_commands(target, 31, 4, 2.89)
            self.assertAlmostEqual(h1_ratio(c, 31, 4, 2.89), target, places=10)
        self.assertEqual(h1_breakeven_commands(1.5, 4, 4, 2.89), 0.0)   # ρ(0) = 1 < 1,5
        with self.assertRaises(ValueError):
            h1_breakeven_commands(1.0, 31, 4, 2.89)

    def test_t2_window_and_sensitivity(self):
        t = h1_table(H1Params(window_s=8 * 3600, gate_wakes=2.0))
        self.assertEqual(t["params"]["heartbeat_ticks"], 15)
        self.assertAlmostEqual(background_share_events(10.1, 45.0), 45 / (45 + 10.1 * 2.89))

    def test_cost_bounds(self):
        book = PriceBook.load()
        b = daily_cost_bounds(10, 3000, 2000, 150, "gpt-4o-mini", book)
        self.assertEqual(b["cached_tokens_per_call"], 1920)          # 2000 → кратно 128
        self.assertAlmostEqual(b["rub_no_cache"], 10 * book.call_cost_rub("gpt-4o-mini", 3000, 0, 150))
        self.assertLess(b["rub_full_prefix_cache"], b["rub_no_cache"])
        small = daily_cost_bounds(10, 3000, 1000, 150, "gpt-4o-mini", book)   # префикс < 1024 не кэшируется
        self.assertEqual(small["rub_full_prefix_cache"], small["rub_no_cache"])

    def test_prereg_is_json_serializable(self):
        res = prereg_forecasts()
        text = json.dumps(res, ensure_ascii=False)
        self.assertIn("h1_t0_window", res)
        self.assertEqual(res["h1_t0_window"]["params"]["heartbeat_ticks"], 31)
        self.assertEqual(res["h1_t2_window"]["params"]["heartbeat_ticks"], 15)
        self.assertGreater(len(text), 1000)
        first = res["h4b_history_ratio_ttl600"][0]
        self.assertAlmostEqual(first["full"]["ratio_to_base"], 1.0)


if __name__ == "__main__":
    unittest.main()
