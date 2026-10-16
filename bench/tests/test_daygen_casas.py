"""daygen: адаптер CASAS (разбор строк, разбиение домов, подмножество) и шаблоны MASSIVE."""
from __future__ import annotations

import gzip
import json
import string
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bench.daygen import casas, massive, room  # noqa: E402

SUBSET = casas.DEFAULT_SUBSET
HAVE_SUBSET = casas.subset_exists(SUBSET)
HAVE_ZIP = casas.DEFAULT_ZIP.exists()


class ParseLineTest(unittest.TestCase):
    def test_motion_with_activity_begin(self):
        r = casas.parse_line('2012-07-20,10:38:54.512364,OutsideDoor,ON,Step_Out="begin"\n')
        self.assertEqual(r, casas.Record("2012-07-20", 38334.512, "OutsideDoor", "ON", "Step_Out:begin"))
        self.assertTrue(r.is_motion)

    def test_end_and_plain_labels(self):
        self.assertEqual(casas.parse_line("2012-07-20,10:50:54.9,OutsideDoor,OFF,Step_Out=\"end\"").activity,
                         "Step_Out:end")
        # метка «внутри активности» не сохраняется
        self.assertEqual(casas.parse_line("2012-07-20,11:10:01.75,Bathroom,ON,Toilet").activity, "")

    def test_carriage_return_and_short_fraction(self):
        r = casas.parse_line("2011-06-15,10:43:05.0317,Kitchen,OPEN\r\n")
        self.assertEqual((r.value, r.t), ("OPEN", 38585.032))
        self.assertTrue(r.is_contact)

    def test_numeric_value_kept_as_int(self):
        r = casas.parse_line("2011-06-15,22:43:53.30917,Bedroom,42")
        self.assertEqual(r.value, 42)
        self.assertFalse(r.is_motion or r.is_contact)

    def test_garbage_rejected(self):
        for line in ["", ":1", "2012-07-20,10:00:00,Kitchen", "2012-07-20,xx,Kitchen,ON",
                     "2012-07-20,10:00:00,,ON", "2012-07-20,10:00:00,Kitchen,ONf"]:
            self.assertIsNone(casas.parse_line(line), line)


class SplitTest(unittest.TestCase):
    def test_split_is_7_7_stratified_and_deterministic(self):
        s1 = casas.split_homes(casas.HOMES, 20261006)
        s2 = casas.split_homes(dict(reversed(list(casas.HOMES.items()))), 20261006)
        self.assertEqual(s1, s2)   # не зависит от порядка словаря
        self.assertEqual(sum(v == "tune" for v in s1.values()), 7)
        pair = [h for h, n in casas.HOMES.items() if n == 2]
        self.assertEqual({s1[h] for h in pair}, {"tune", "holdout"})

    def test_other_seed_changes_split(self):
        splits = {tuple(sorted(casas.split_homes(casas.HOMES, s).items())) for s in range(10)}
        self.assertGreater(len(splits), 1)


@unittest.skipUnless(HAVE_SUBSET, "нет data/casas/subset (python -m bench.daygen casas-extract)")
class SubsetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = casas.load_index(SUBSET)

    def test_index_matches_files(self):
        self.assertEqual(set(self.index["homes"]), set(casas.HOMES))
        self.assertEqual(self.index["split_seed"], casas.SPLIT_SEED)
        self.assertEqual(casas.split_homes(casas.HOMES), {h: m["split"] for h, m in self.index["homes"].items()})
        for h, m in self.index["homes"].items():
            self.assertEqual(casas.sha256_file(SUBSET / m["file"]), m["sha256"], h)
            self.assertTrue(set(m["eligible_days"]) <= set(m["days"]))
            self.assertAlmostEqual(sum(m["hour_weights_act"]), 1.0, places=4)
        self.assertTrue((SUBSET / "PROVENANCE.txt").read_text(encoding="utf-8").count("CC BY 4.0"))

    def test_subset_header_and_order(self):
        m = self.index["homes"]["hh121"]
        with gzip.open(SUBSET / m["file"], "rt", encoding="utf-8") as f:
            self.assertEqual(f.readline().strip(), ",".join(casas.SUBSET_HEADER))
        recs = casas.load_day(SUBSET, "hh121", m["days"][0], self.index)
        self.assertEqual([r.t for r in recs], sorted(r.t for r in recs))
        self.assertTrue(all(r.date == m["days"][0] for r in recs))
        self.assertTrue(all(isinstance(r.value, int) or r.value in ("ON", "OFF", "OPEN", "CLOSE") for r in recs))

    def test_inventory_from_every_home(self):
        for h, m in self.index["homes"].items():
            devs, rooms = room.build_inventory({k: set(v) for k, v in m["locations"].items()})
            ids = [d["id"] for d in devs]
            self.assertEqual(ids, sorted(set(ids)), h)
            self.assertTrue({f"light_{r}" for r in rooms} <= set(ids))
            has_door = "contact" in m["locations"].get("OutsideDoor", [])
            self.assertEqual("door_front" in ids, has_door, h)


@unittest.skipUnless(HAVE_ZIP and HAVE_SUBSET, "нужны labeled_data.zip и подмножество")
class ExtractReproducibleTest(unittest.TestCase):
    def test_extract_one_home_byte_identical(self):
        ok, msg = casas.check_zip(casas.DEFAULT_ZIP)
        self.assertTrue(ok, msg)
        with tempfile.TemporaryDirectory() as d:
            idx = casas.extract_subset(casas.DEFAULT_ZIP, Path(d), homes={"hh121": 2})
            committed = casas.load_index(SUBSET)["homes"]["hh121"]
            self.assertEqual(idx["homes"]["hh121"]["sha256"], committed["sha256"])
            self.assertEqual(idx["homes"]["hh121"]["days"], committed["days"])


class MassiveTemplatesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tpls = massive.load_templates()

    def test_coverage_and_fields(self):
        doc = json.loads((massive.DEFAULT_OUT / massive.TEMPLATES_FILE).read_text(encoding="utf-8"))
        self.assertEqual(doc["n_source_pairs"], 220)
        self.assertEqual(len(self.tpls) + len(doc["excluded"]), 220)
        for t in self.tpls:
            self.assertIn(t["action"], massive.ACTIONS)
            self.assertTrue(t["ru"] and t["en"])
            self.assertIn(t["room_mode"], ("slot", "context", "fixed", "none"))
        colors = {t["params"]["color"] for t in self.tpls if "color" in t["params"]}
        self.assertTrue(colors <= set(room.COLORS))

    def test_slot_templates_render_for_every_room(self):
        fields = {"loc", "acc", "room"}
        for t in (x for x in self.tpls if x["room_mode"] == "slot"):
            for key in ("ru_tpl", "en_tpl"):
                names = {f for _, f, _, _ in string.Formatter().parse(t[key]) if f}
                self.assertTrue(names <= fields, t["id"])
            for r in room.ALL_ROOMS.values():
                ru = t["ru_tpl"].format(loc=r.ru_loc, acc=r.ru_acc)
                en = t["en_tpl"].format(room=r.en)
                self.assertNotIn("{", ru + en)
                self.assertTrue(r.ru_loc in ru or r.ru_acc in ru)
                self.assertIn(r.en, en)

    @unittest.skipUnless((massive.DEFAULT_SRC / "massive_test_ru.json").exists(), "нет исходника MASSIVE")
    def test_build_reproduces_committed_file(self):
        with tempfile.TemporaryDirectory() as d:
            massive.build(massive.DEFAULT_SRC, Path(d))
            for name in (massive.TEMPLATES_FILE, "iot_ru_en.jsonl"):
                self.assertEqual((Path(d) / name).read_bytes(), (massive.DEFAULT_OUT / name).read_bytes(), name)


if __name__ == "__main__":
    unittest.main()
