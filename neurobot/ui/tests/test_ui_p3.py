"""Пульт, приёмка П3 (p3lib.py): разбор замеров без ROS и браузера — сдвиг часов по запросу с наименьшим RTT,
smaps_rollup, сокеты `ss -tinH`, трафик (живые сокеты — приращение, новые — с открытия), CPU cgroup в долях ядра ВМ и
A57, наклон МНК, сводка шага, сравнение K1 с пультом и без. Только stdlib, Python 3.8+."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import p3lib  # noqa: E402

SMAPS = """55d0c0000000-ffffd4a6f000 ---p 00000000 00:00 0                          [rollup]
Rss:               70360 kB
Pss:               46019 kB
Pss_Anon:          44100 kB
Shared_Clean:      26000 kB
Private_Clean:        12 kB
Private_Dirty:     44280 kB
Swap:                  0 kB
"""

SS = """0      0      127.0.0.1:18120 127.0.0.1:51234
\t cubic wscale:7,7 rto:204 rtt:0.05/0.025 bytes_sent:1500 bytes_acked:1400 bytes_received:420 segs_out:9
0      0      127.0.0.1:18120 127.0.0.1:51240
\t cubic wscale:7,7 rto:204 rtt:0.05/0.025 bytes_received:300 segs_out:1
"""


class P3Test(unittest.TestCase):
    def test_clock_offset_min_rtt(self):
        # сервер впереди на 2,0 с; второй запрос — самый быстрый, по нему и сдвиг
        off, rtt = p3lib.clock_offset([(10.0, 12.03, 10.02), (11.0, 13.0005, 11.001), (12.0, 14.2, 12.4)])
        self.assertAlmostEqual(off, 2.0, places=6)
        self.assertAlmostEqual(rtt, 0.001, places=6)

    def test_smaps_rollup(self):
        self.assertEqual(p3lib.smaps_rollup(SMAPS), {"pss": 46019, "rss": 70360, "uss": 44292, "swap": 0})
        self.assertEqual(p3lib.smaps_rollup(""), {"pss": 0, "rss": 0, "uss": 0, "swap": 0})

    def test_ss_sockets(self):
        s = p3lib.ss_sockets(SS)
        self.assertEqual(s["127.0.0.1:18120 127.0.0.1:51234"], {"acked": 1400, "sent": 1500, "received": 420})
        self.assertEqual(s["127.0.0.1:18120 127.0.0.1:51240"], {"acked": 0, "sent": 0, "received": 300})
        self.assertEqual(p3lib.ss_sockets(""), {})

    def test_traffic_alive_and_new(self):
        a = {"t": 0.0, "socks": {"x": {"acked": 1000}}}
        b = {"t": 10.0, "socks": {"x": {"acked": 6000}, "y": {"acked": 3000}}}        # y открыт между выборками
        c = {"t": 20.0, "socks": {"y": {"acked": 4000}}}                             # x закрыт: хвост потерян
        tr = p3lib.traffic([a, b, c])
        self.assertEqual((tr["bytes"], tr["new_sockets"], tr["rate_Bps"], tr["steady_Bps"]), (9000, 1, 450.0, 300.0))
        self.assertEqual(p3lib.traffic([a])["rate_Bps"], None)

    def test_cpu_and_a57(self):
        s = [{"t": 0.0, "cpu_usec": 0}, {"t": 100.0, "cpu_usec": 1_500_000}]
        self.assertEqual(p3lib.cpu_pct(s), 1.5)
        self.assertEqual(p3lib.to_a57(1.5, 0.265), 5.66)       # A57 ≈ в 3,8 раза медленнее ядра ВМ
        self.assertIsNone(p3lib.to_a57(None, 0.265))
        self.assertIsNone(p3lib.cpu_pct(s[:1]))

    def test_slope_per_hour(self):
        pts = [(t, 40.0 + t / 3600.0 * 2.0) for t in range(0, 1800, 30)]          # 2 МиБ в час
        self.assertAlmostEqual(p3lib.slope_per_h(pts), 2.0, places=3)
        self.assertIsNone(p3lib.slope_per_h(pts[:2]))
        self.assertIsNone(p3lib.slope_per_h([(5.0, 1.0)] * 4))

    def test_step_summary_skips_warmup(self):
        smp = [{"t": float(t), "pss_kib": 40960 + (10240 if t < 30 else 0), "uss_kib": 40000, "rss_kib": 70000,
                "mem_current": 45000, "cpu_usec": t * 10_000, "clients": 1, "nrestarts": 2,
                "socks": {"s": {"acked": t * 5000}}} for t in range(0, 335, 5)]
        s = p3lib.step_summary(smp, 0.25, skip_s=30.0)
        self.assertEqual(s["pss_mib"]["max"], 40.0)              # прогрев (50 МиБ) не в счёт
        self.assertEqual((s["cpu_pct_vm"], s["cpu_pct_a57"]), (1.0, 4.0))
        self.assertEqual(s["traffic"]["rate_Bps"], 5000.0)
        self.assertEqual((s["clients"], s["restarts"], s["pss_slope_mib_h"]), ([1], 0, 0.0))
        self.assertEqual(s["cgroup_mib"]["max"], 43.9)           # mem_current — КиБ, как остальная память
        self.assertIsNone(s["cpu_split_vm"])

    def test_cpu_split_main_and_children(self):
        smp = [{"t": 0.0, "main_ticks": 100, "child_ticks": 10, "hz": 100},
               {"t": 100.0, "main_ticks": 250, "child_ticks": 20, "hz": 100}]
        self.assertEqual(p3lib.cpu_split(smp), {"main": 1.5, "children": 0.1})
        self.assertIsNone(p3lib.cpu_split([{"t": 0.0}, {"t": 5.0}]))

    def test_compare_k1_within_spread(self):
        def run(label, ui, p50, p95, ci):
            return {"label": label, "ui": ui, "k1": {"n": 30, "p50": p50, "p95": p95}, "k1_p50_ci": ci}
        same = [run("off1", "off", 0.56, 0.70, [0.52, 0.6]), run("on1", "on", 0.57, 0.71, [0.53, 0.61]),
                run("on2", "on", 0.55, 0.69, [0.51, 0.59]), run("off2", "off", 0.58, 0.72, [0.54, 0.62])]
        c = p3lib.compare_k1(same)
        self.assertEqual((c["delta_p50_s"], c["spread_p50_s"], c["ci_p50_overlap"], c["within_spread"]),
                         (-0.01, 0.02, True, True))
        worse = [run("off1", "off", 0.56, 0.70, [0.52, 0.6]), run("on1", "on", 0.70, 0.90, [0.66, 0.75]),
                 run("on2", "on", 0.71, 0.92, [0.67, 0.76]), run("off2", "off", 0.57, 0.71, [0.53, 0.61])]
        self.assertFalse(p3lib.compare_k1(worse)["within_spread"])
        self.assertNotIn("within_spread", p3lib.compare_k1(same[:1]))

    def test_median_ci_rank(self):
        lo, hi = p3lib.median_ci(list(range(1, 33)))           # n = 32: X(10) … X(23) (Bin(32, 1/2), 95 %)
        self.assertEqual((lo, hi), (10, 23))
        self.assertIsNone(p3lib.median_ci([1.0, 2.0]))

    def test_paired_k1_same_gestures(self):
        base = {s: 0.7 + 0.01 * (s % 5) for s in range(32)}
        noise = {s: (0.004 if s % 2 else -0.004) for s in range(32)}
        runs = {"off1": dict(base), "off2": {s: v + noise[s] for s, v in base.items()},
                "on1": {s: v - noise[s] for s, v in base.items()}, "on2": dict(base)}
        del runs["on2"][31]                                     # жест не во всех прогонах — не в счёт
        p = p3lib.paired_k1(runs, ["on1", "on2"], ["off1", "off2"])
        self.assertEqual(p["segs"], 31)
        self.assertTrue(p["no_effect_seen"])
        self.assertLessEqual(abs(p["median_ms"]), 4.0)
        slow = {k: ({s: v + 0.05 for s, v in r.items()} if k.startswith("on") else r) for k, r in runs.items()}
        q = p3lib.paired_k1(slow, ["on1", "on2"], ["off1", "off2"])
        self.assertFalse(q["no_effect_seen"])
        self.assertGreater(q["median_ms"], 40.0)
        self.assertEqual(p3lib.paired_k1({"a": {1: 0.1}, "b": {1: 0.1}}, ["a"], ["b"]), {"segs": 1})


if __name__ == "__main__":
    unittest.main()
