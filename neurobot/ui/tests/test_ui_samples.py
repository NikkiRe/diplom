"""Пульт: демо-бандл для публичного репозитория (samples/demo_hall.json.gz) — по формату, ≤ 2 МБ, A0 и A1 в зале, числа
совпадают с compare.md (строки ниже переписаны из experiments/results/neurobot_20261018_a1v2/compare.md; если каталог
есть локально — сверяются и с самим файлом), итог потока — с этими же числами; без секретов, путей машины, журналов и
ответов инструментов. Только stdlib, Python 3.8+."""
from __future__ import annotations

import gzip
import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from neurobot.ui import stream  # noqa: E402

SAMPLE = ROOT / "neurobot" / "ui" / "samples" / "demo_hall.json.gz"
COMPARE_MD = ROOT / "experiments" / "results" / "neurobot_20261018_a1v2" / "compare.md"
# compare.md, «Таблица A0 / A1»: столбцы «A0 — стек лаборатории как на роботе» и «A1 18.10 … (`a1v2`)»
EXPECTED = {
    "K1 t0 → первая ненулевая /cmd_vel, p50 / p95, с (без ранних)": ("1,81 / 2,11 (n=69)", "0,68 / 0,97 (n=57)"),
    "K2 t0 → начало движения, p50 / p95, с (без ранних)": ("1,84 / 2,14 (n=69)", "0,77 / 1,09 (n=57)"),
    "Верная реакция на жест (разбор — ниже)": ("28 из 80", "37 из 80"),
    "K4 вызовы LLM на жест": ("0", "0"),
    "Реплики «вперёд»: поехал или агент сообщил о запрете": (
        "0 из 3: поехал 0, сообщил о запрете 0; «готово» без движения 2, не подтверждено 0, без ответа 1 "
        "(воркер 8766 — заглушка: робот не двигается)",
        "3 из 3: поехал 3, сообщил о запрете 0; «готово» без движения 0, не подтверждено 0, без ответа 0"),
    "Пробег по истине; время в движении": ("42,4 м; в движении 599 с (29 %); переносы не в счёт: 2, 8,5 м",
                                           "22,6 м; в движении 494 с (24 %)"),
    "safety_gate: доля времени ok / slow / blocked / estop": ("нет safety_gate",
                                                              "ok 0 % / slow 84 % / blocked 13 % / estop 3 %"),
    "K7 нежелательные действия (в час)": ("12 (20,9)", "5 (8,7)"),
    "Касания (истина Gazebo)": ("23 (75,7 с; low_box, person, table, wall_east), мин. зазор 0,000 м",
                                "0 (0 с; —), мин. зазор 0,117 м"),
    "K8 память всех процессов системы (PSS)": ("520 → 481 МиБ, наклон -84,6 МиБ/ч (0,53 ч)",
                                               "621 → 631 МиБ, наклон 23,1 МиБ/ч (0,53 ч)"),
}


class SampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = stream.read_bundle(str(SAMPLE))
        cls.runs = {r["id"]: r for r in cls.doc["runs"]}

    def test_format_and_size(self):
        self.assertLessEqual(os.path.getsize(SAMPLE), 2 * 1024 * 1024)
        self.assertEqual(stream.check_bundle(self.doc), ["a0", "a1v2"])
        for r in self.doc["runs"]:
            self.assertEqual((r["source"], r["origin"], r["world"]), ("запись", "двойник", "hall"))
        self.assertEqual(self.runs["a0"]["system"], "a0")
        self.assertEqual(self.runs["a1v2"]["system"], "a1")

    def test_cells_equal_compare_md(self):
        rows = [r["name"] for r in self.doc["compare_rows"]]
        self.assertEqual(rows, list(EXPECTED))
        a0, a1 = self.runs["a0"]["summary"]["compare"], self.runs["a1v2"]["summary"]["compare"]
        for i, name in enumerate(rows):
            self.assertEqual((a0[i], a1[i]), EXPECTED[name], name)

    @unittest.skipUnless(COMPARE_MD.exists(), "compare.md — только локально (experiments/results)")
    def test_expected_is_compare_md(self):
        from test_ui_replay import column, table
        tab = table(COMPARE_MD)
        ca0, ca1 = column(tab, "A0"), column(tab, "A1 18.10")
        for name, (a0, a1) in EXPECTED.items():
            self.assertEqual((tab[name][ca0], tab[name][ca1]), (a0, a1), name)

    def test_stream_end_has_same_numbers(self):
        for rid, k1, touches, dist in (("a0", (69, 1.8098, 2.1148), 23, 42.41),
                                       ("a1v2", (57, 0.6846, 0.9717), 0, 22.61)):
            st = [m for m in self.runs[rid]["stream"] if m["k"] == "stats"][-1]
            self.assertEqual((st["k1"]["n"], st["k1"]["p50"], st["k1"]["p95"]), k1, rid)
            self.assertEqual((st["touches"], st["distance_m"], st["llm"]["gesture"]), (touches, dist, 0), rid)
        share = [m for m in self.runs["a1v2"]["stream"] if m["k"] == "stats"][-1]["safety_share"]
        self.assertEqual(share, {"ok": 0.0, "slow": 0.8419, "blocked": 0.1268, "estop": 0.0313})

    def test_no_secrets_paths_or_logs(self):
        raw = gzip.decompress(SAMPLE.read_bytes()).decode("utf-8")
        for pat in (r"Bearer", r"\bsk-", r"api[_-]?key", r"password", r"Authorization", r"/Users/", r"/home/",
                    r"/var/tmp", r"\"content\"", r"\"text\":\s*\"\[runs", r"\"tools\""):
            self.assertIsNone(re.search(pat, raw, re.I), pat)
        for r in self.doc["runs"]:
            self.assertFalse(os.path.isabs(r["dir"]), r["dir"])


if __name__ == "__main__":
    unittest.main()
