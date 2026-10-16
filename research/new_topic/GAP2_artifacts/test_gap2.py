"""Тесты артефактов GAP2. Запуск: python3 -m unittest test_gap2 -v"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest

import ctx_formats as cf
import make_fixture
import sysfs_probe as sp
from minijsonschema import Validator

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = json.load(open(os.path.join(HERE, "td-schema-v1.1_w3c_2025-03-12.json")))
EXAMPLES = json.load(open(os.path.join(HERE, "td11_spec_examples.json")))


def senml_resolve(pack: list[dict]) -> list[dict]:
    """Разрешение пакета SenML по RFC 8428 §4.6 (bn + n, bt + t)."""
    out, bn, bt = [], "", 0.0
    for r in pack:
        bn = r.get("bn", bn)
        bt = r.get("bt", bt)
        rr = {"n": bn + r.get("n", ""), "t": bt + r.get("t", 0.0)}
        for k in ("u", "v", "vb", "vs"):
            if k in r:
                rr[k] = r[k]
        out.append(rr)
    return out


class TestValidator(unittest.TestCase):
    def setUp(self):
        self.v = Validator(SCHEMA)

    def test_spec_examples_pass(self):
        self.assertGreaterEqual(len(EXAMPLES), 5)
        for td in EXAMPLES:
            self.assertEqual(self.v.errors(td), [], td["title"])

    def test_negatives_fail(self):
        base = EXAMPLES[0]
        for mutate in (lambda t: t.pop("title"),
                       lambda t: t.pop("securityDefinitions"),
                       lambda t: t["properties"]["status"].pop("forms"),
                       lambda t: t["properties"]["status"].__setitem__("type", "float"),
                       lambda t: t.__setitem__("security", 7),
                       lambda t: t.__setitem__("@context", "https://example.org/ctx")):
            t = copy.deepcopy(base)
            mutate(t)
            self.assertNotEqual(self.v.errors(t), [])


class TestProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.root = os.path.join(cls.tmp, "root")
        make_fixture.build(cls.root)
        cls.decl = json.load(open(os.path.join(HERE, "declared.json")))
        cls.inv = sp.probe(cls.root, cls.decl)
        cls.by = {d["id"]: d for d in cls.inv}

    def ch(self, dev, name):
        return next(c for c in self.by[dev]["channels"] if c["name"] == name)

    def test_iio_units_converted(self):
        self.assertAlmostEqual(self.ch("iio:bme280@i2c-1/0x76", "temperature")["value"], 22.51)
        self.assertEqual(self.ch("iio:bme280@i2c-1/0x76", "temperature")["unit"], "Cel")
        self.assertAlmostEqual(self.ch("iio:bme280@i2c-1/0x76", "humidity")["value"], 41.23)
        self.assertAlmostEqual(self.ch("iio:bme280@i2c-1/0x76", "pressure")["value"], 101325.0)
        self.assertEqual(self.ch("iio:bme280@i2c-1/0x76", "pressure")["unit"], "Pa")

    def test_iio_raw_offset_scale(self):
        c = self.ch("iio:ads1015@i2c-1/0x48", "voltage0")
        self.assertEqual(c["how"], "(raw+offset)*scale")
        self.assertAlmostEqual(c["value"], (1650 - 25) * 2.0 / 1000)

    def test_type_wide_scale(self):
        d = os.path.join(self.tmp, "iiox")
        os.makedirs(d)
        make_fixture.w(os.path.join(d, "in_voltage1_raw"), "100\n")
        make_fixture.w(os.path.join(d, "in_voltage_scale"), "0.5\n")
        v, how = sp.iio_channel_value(d, "in_voltage1", "voltage")
        self.assertEqual((v, how), (50.0, "(raw+offset)*scale"))

    def test_raw_without_scale_is_not_guessed(self):
        d = os.path.join(self.tmp, "iioy")
        os.makedirs(d)
        make_fixture.w(os.path.join(d, "in_temp_raw"), "123\n")
        self.assertEqual(sp.iio_channel_value(d, "in_temp", "temp"), (None, "raw-without-scale"))

    def test_w1_and_hwmon(self):
        self.assertAlmostEqual(self.ch("w1:28-0316a2795cff", "temperature")["value"], 21.437)
        self.assertEqual(self.by["w1:28-0316a2795cff"]["name"], "DS18B20")
        self.assertIn("750 ms", self.by["w1:28-0316a2795cff"]["notes"][0])
        self.assertAlmostEqual(self.ch("hwmon:cpu_thermal", "temperature1")["value"], 48.3)

    def test_observability_levels(self):
        self.assertEqual(self.by["iio:bme280@i2c-1/0x76"]["observability"], "A")
        self.assertEqual(self.by["usb:1a86:7523@1-2"]["observability"], "B")
        self.assertEqual(self.by["i2c:i2c-1/0x40"]["observability"], "C")
        self.assertEqual(self.by["gpio:relay_fan"]["observability"], "C")

    def test_i2c_claimed_not_duplicated_and_roothub_hidden(self):
        i2c = [d["id"] for d in self.inv if d["source"] == "i2c"]
        self.assertEqual(i2c, ["i2c:i2c-1/0x40"])
        self.assertFalse(any(d["id"].startswith("usb:1d6b") for d in self.inv))

    def test_v4l2_grouped(self):
        v = [d for d in self.inv if d["source"] == "v4l2"]
        self.assertEqual(len(v), 1)
        self.assertEqual(sorted(n["dev"] for n in v[0]["nodes"]), ["video0", "video1"])

    def test_hat_strings_nul_stripped(self):
        h = [d for d in self.inv if d["source"] == "hat"][0]
        self.assertEqual(h["name"], "Special Sensor Board")

    def test_declared_locations_applied(self):
        self.assertEqual(self.by["iio:bme280@i2c-1/0x76"].get("location"), "kitchen")


class TestFormats(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = os.path.join(tempfile.mkdtemp(), "root")
        make_fixture.build(root)
        inv = sp.probe(root, json.load(open(os.path.join(HERE, "declared.json"))))
        cls.devs = cf.devices_with_channels(inv)

    def test_generated_tds_valid(self):
        v = Validator(SCHEMA)
        for k in (1, 5):
            for d in cf.scale_inventory(self.devs, k):
                self.assertEqual(v.errors(cf.td_full(d)), [], d["id"])

    def test_agent_view_has_no_protocol_details(self):
        td = cf.td_agent(cf.td_full(self.devs[0]))
        s = json.dumps(td)
        for k in ("forms", "@context", "securityDefinitions", "href"):
            self.assertNotIn(k, s)

    def test_senml_resolves_to_inventory(self):
        res = senml_resolve(cf.senml(self.devs))
        self.assertTrue(all(r["n"].startswith("urn:dev:edge:") for r in res))
        self.assertTrue(all(r["t"] == cf.TS for r in res))
        got = {r["n"]: r.get("v", r.get("vb")) for r in res}
        self.assertAlmostEqual(got["urn:dev:edge:kitchen.bme280:temperature"], 22.51)
        self.assertEqual(len(res), sum(len(d["channels"]) for d in self.devs))

    def test_deterministic(self):
        a, _ = cf.build_all(self.devs)
        b, _ = cf.build_all(copy.deepcopy(self.devs))
        self.assertEqual(a, b)

    def test_explicit_tool_count(self):
        tds = [cf.td_full(d) for d in self.devs]
        n_props = sum(len(t["properties"]) for t in tds)
        n_rw = sum(1 for t in tds for p in t["properties"].values() if not p["readOnly"])
        self.assertEqual(len(cf.mcp_explicit(tds)), n_props + n_rw)
        self.assertEqual(len(cf.mcp_generic_tools()), 4)

    def test_token_order_if_tokenizer(self):
        count, _ = cf.get_counter()
        if count is None:
            self.skipTest("нет tiktoken/o200k")
        reps, _ = cf.build_all(self.devs)
        t = {k: count(v) for k, v in reps.items()}
        self.assertGreater(t["L0.td_full"], t["L0.td_agent"])
        self.assertGreater(t["L0.td_agent"], t["L0.text"])
        self.assertGreater(t["L1.senml"], t["L1.custom_json"])


if __name__ == "__main__":
    unittest.main()
