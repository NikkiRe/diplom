"""Пульт: числа по-русски (fmt.py) и то же в браузерном fmt.js — одни векторы (fmt.js — через node, если он есть;
в браузере их же сверяет e2e.py). Только stdlib, Python 3.8+."""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from neurobot.ui import fmt  # noqa: E402

WEB = ROOT / "neurobot" / "ui" / "web"
NN = "\u00a0"
VECTORS = [
    ((1.8098, 2), "1,81"), ((0.6846, 2), "0,68"), ((0.9717, 2), "0,97"),
    ((0.125, 2), "0,12"), ((0.375, 2), "0,38"), ((2.5, 0), "2"), ((3.5, 0), "4"),   # ничья — к чётному, как «%.nf»
    ((1.805, 2), "1,80"),                              # 1.805 в double чуть меньше — не ничья
    ((-0.001, 2), "0,00"), ((-0.5, 0), "0"),           # «−0» не пишется
    ((-84.555, 1), "−84,6"), ((99.995, 2), "100,00"),
    ((2406, 0), "2406"), ((12345.678, 1), "12" + NN + "345,7"), ((1234567, 0), "1" + NN + "234" + NN + "567"),
    ((None, 2), "—"), ((float("nan"), 2), "—"), ((float("inf"), 1), "—"), ((True, 0), "—"),
]


def node_num(cases):
    uri = json.dumps((WEB / "lib" / "fmt.js").as_uri())
    code = ("import { num } from %s; const cases = JSON.parse(process.argv[1]);"
            "console.log(JSON.stringify(cases.map(([v, nd]) => num(v, nd))));" % uri)
    out = subprocess.run(["node", "--input-type=module", "-e", code, json.dumps(cases)], capture_output=True,
                         text=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


class FmtTest(unittest.TestCase):
    def test_vectors(self):
        for (v, nd), want in VECTORS:
            self.assertEqual(fmt.num(v, nd), want, (v, nd))

    def test_unit_and_pct(self):
        self.assertEqual(fmt.unit(0.15, 2, "м/с"), "0,15 м/с")
        self.assertEqual(fmt.unit(None, 2, "м/с"), "—")
        self.assertEqual(fmt.pct(0.8419), "84 %")
        self.assertEqual(fmt.pct(None), "—")

    def test_same_as_compare_md_numbers(self):
        # compare.py пишет числа той же точностью: «1,81 / 2,11», «42,4 м», «-84,6» (минус там дефис — его строки
        # пульт показывает как есть)
        from neurobot.sim.runs import compare
        for v, nd in ((1.8098, 2), (42.41, 1), (0.1171, 3), (20.905, 1)):
            self.assertEqual(fmt.num(v, nd), compare.num(v, nd))

    @unittest.skipUnless(shutil.which("node"), "нет node: fmt.js сверяет e2e.py в браузере")
    def test_js_matches_python(self):
        rnd = random.Random(20261019)
        def finite(v):
            return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) < float("inf"))
        cases = [[v, nd] for (v, nd), _ in VECTORS if finite(v)]
        for _ in range(2000):
            nd = rnd.randint(0, 4)
            if rnd.random() < 0.7:
                v = round(rnd.uniform(-20000, 20000), rnd.randint(0, 6))
            else:
                v = rnd.randint(-999, 999) / 8
            cases.append([v, nd])
        got = node_num(cases)
        want = [fmt.num(v, nd) for v, nd in cases]
        bad = [(c, g, w) for c, g, w in zip(cases, got, want) if g != w]
        self.assertEqual(bad, [])

    @unittest.skipUnless(shutil.which("node"), "нет node")
    def test_js_dur_and_age(self):
        uri = json.dumps((WEB / "lib" / "fmt.js").as_uri())
        code = ("import { dur, age } from %s; console.log(JSON.stringify([dur(75, true), dur(-12, true), dur(3725),"
                " age(0.42), age(45), age(600), dur(null)]));" % uri)
        out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), ["+1:15", "−0:12", "1:02:05", "0,4 с", "45 с",
                                                  "10 мин", "—"])


if __name__ == "__main__":
    unittest.main()
