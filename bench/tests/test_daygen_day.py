"""daygen: детерминизм, формат дня, частоты профилей, правильность ответов на вопросы и предикаты ситуаций.

Ответы и ситуации проверяются независимой реализацией — повторным проигрыванием выданного файла дня,
а не повторным вызовом кода генератора.
"""
from __future__ import annotations

import copy
import io
import json
import math
import statistics as st
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bench.daygen import casas, room, situations  # noqa: E402
from bench.daygen.generator import (  # noqa: E402
    K_DIST,
    PROFILES,
    DayBuilder,
    DayConfig,
    dumps,
    generate_day,
    read_day,
    write_day,
)
from bench.simmcp import catalog  # noqa: E402
from bench.simmcp.world import check_value  # noqa: E402

HAVE_SUBSET = casas.subset_exists()


def day_bytes(cfg: DayConfig) -> bytes:
    h, ev = generate_day(cfg)
    buf = io.StringIO()
    buf.write(dumps(h) + "\n")
    for e in ev:
        buf.write(dumps(e) + "\n")
    return buf.getvalue().encode("utf-8")


def synth_days(n: int = 12, seed0: int = 1, **kw) -> list[tuple[dict, list[dict]]]:
    kw.setdefault("profile", "H")
    kw.setdefault("source", "synthetic")
    return [generate_day(DayConfig(seed=seed0 + i, **kw)) for i in range(n)]


class DeterminismTest(unittest.TestCase):
    def test_same_seed_same_bytes_synthetic(self):
        cfg = dict(seed=42, profile="M+", source="synthetic", lang="mix")
        self.assertEqual(day_bytes(DayConfig(**cfg)), day_bytes(DayConfig(**cfg)))

    def test_different_seed_differs(self):
        a = day_bytes(DayConfig(seed=1, source="synthetic"))
        b = day_bytes(DayConfig(seed=2, source="synthetic"))
        self.assertNotEqual(a, b)

    @unittest.skipUnless(HAVE_SUBSET, "нет подмножества CASAS")
    def test_same_seed_same_bytes_casas_and_file_roundtrip(self):
        cfg = dict(seed=7, profile="H", split="holdout")
        b1 = day_bytes(DayConfig(**cfg))
        self.assertEqual(b1, day_bytes(DayConfig(**cfg)))
        with tempfile.TemporaryDirectory() as d:
            h, ev = generate_day(DayConfig(**cfg))
            write_day(Path(d) / "day.jsonl", h, ev)
            self.assertEqual((Path(d) / "day.jsonl").read_bytes(), b1)
            h2, ev2 = read_day(Path(d) / "day.jsonl")
            self.assertEqual((h2, ev2), (json.loads(dumps(h)), json.loads(dumps(ev))))
        self.assertEqual(h["split"], "holdout")

    def test_streams_isolated_trace_does_not_depend_on_commands(self):
        """Трасса датчиков casas+synthetic не зависит от параметров реплик; ручные переключения (manual) —
        зависят: житель переключает свет по «идеальному» состоянию, которое меняют команды (README)."""
        def trace(**kw):
            _, ev = generate_day(DayConfig(seed=5, source="synthetic", **kw))
            return [e for e in ev if e["kind"] == "sensor" and e["source"] != "manual"]
        self.assertEqual(trace(profile="L"), trace(profile="S69", question_share=0.9, trap_share=0.5))


class FormatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.days = synth_days(10, profile="H", lang="mix", trap_share=0.2, synthetic_situations=5)

    def test_header(self):
        for h, _ in self.days:
            for k in ("kind", "seed", "profile", "home", "split", "window", "window_s", "devices",
                      "initial_state", "situation_rules", "params"):
                self.assertIn(k, h)
            self.assertEqual(h["kind"], "header")
            self.assertEqual(h["window"], ["07:00", "23:00"])
            self.assertEqual(h["window_s"], [25200, 82800])
            self.assertEqual(set(h["initial_state"]), {d["id"] for d in h["devices"]})

    def test_events_sorted_in_window_with_unique_ids(self):
        for h, ev in self.days:
            ts = [e["t"] for e in ev]
            self.assertEqual(ts, sorted(ts))
            self.assertTrue(all(h["window_s"][0] <= t < h["window_s"][1] for t in ts))
            for kind, p in (("user_cmd", "u"), ("question", "q"), ("situation", "s")):
                ids = [e["id"] for e in ev if e["kind"] == kind]
                self.assertEqual(ids, [f"{p}{i + 1}" for i in range(len(ids))])

    def test_sensor_events_valid_and_deduplicated(self):
        for h, ev in self.days:
            devs = {d["id"]: d for d in h["devices"]}
            last = copy.deepcopy(h["initial_state"])
            for e in ev:
                if e["kind"] == "user_cmd" and e["class"] != "invalid":   # идеальное исполнение команды
                    for c in e["expect"]["calls"]:
                        if c["name"] == "write_property":
                            last[c["args"]["device"]][c["args"]["property"]] = c["args"]["value"]
                if e["kind"] != "sensor":
                    continue
                spec = devs[e["device"]]["properties"][e["property"]]
                _, reason, _ = check_value(spec, e["value"])
                self.assertIsNone(reason, e)
                self.assertNotEqual(last[e["device"]][e["property"]], e["value"], e)
                last[e["device"]][e["property"]] = e["value"]

    def test_expected_calls_exist_in_both_catalogs(self):
        for h, ev in self.days:
            _, explicit = catalog.build("explicit", h["devices"])
            _, generic = catalog.build("generic", h["devices"])
            for e in ev:
                if e["kind"] == "sensor":
                    continue
                exp = e["expect"]
                for c in exp.get("calls", []) + exp.get("actions", []):
                    self.assertIn(c["name"], generic)
                for c in exp.get("calls_explicit", []) + exp.get("actions_explicit", []):
                    self.assertIn(c["name"], explicit, c)

    def test_languages_and_classes(self):
        langs, classes = set(), set()
        for _, ev in self.days:
            for e in ev:
                if e["kind"] in ("user_cmd", "question"):
                    langs.add(e["lang"])
                    classes.add(e["class"])
                    self.assertIn(e["k"], [k for k, _ in K_DIST])
                    self.assertTrue(e["text"] and "{" not in e["text"])
        self.assertEqual(langs, {"ru", "en"})
        self.assertTrue({"control", "invalid", "past", "state"} <= classes)


class ProfileRateTest(unittest.TestCase):
    """Среднее число реплик в сутки совпадает с профилем Bentley/VisionClaw (1000 seed, ±3 SE)."""

    def test_utterances_per_day(self):
        for prof in ("L", "M", "M+", "H"):
            n = []
            for seed in range(1000):
                b = DayBuilder(DayConfig(seed=seed, profile=prof, source="synthetic"))
                b.w0, b.w1, b.hour_w = 7 * 3600, 23 * 3600, [1 / 24] * 24
                us = b._utterances()
                self.assertTrue(all(b.w0 < u.t < b.w1 for u in us))
                n.append(len(us))
            m, se = st.mean(n), st.stdev(n) / math.sqrt(len(n))
            self.assertLess(abs(m - PROFILES[prof][0]), 3 * se, prof)

    def test_k_distribution(self):
        from random import Random

        from bench.daygen.generator import sample_k
        r = Random(1)
        ks = [sample_k(r) for _ in range(20000)]
        self.assertAlmostEqual(st.mean(ks), sum(k * p for k, p in K_DIST), delta=0.05)


# ---------------------------------------------------------------------------
# Независимая проверка ответов: проигрываем файл дня
# ---------------------------------------------------------------------------


def replay_check_questions(test: unittest.TestCase, h: dict, ev: list[dict]) -> int:
    state = copy.deepcopy(h["initial_state"])
    devs = {d["id"]: d for d in h["devices"]}
    w0 = h["window_s"][0]
    sensors: list[dict] = []
    cmds: list[dict] = []
    checked = 0
    for e in ev:
        if e["kind"] == "sensor":
            state[e["device"]][e["property"]] = e["value"]
            sensors.append(e)
        elif e["kind"] == "user_cmd":
            if e["class"] != "invalid":
                cmds.append(e)
                for c in e["expect"]["calls"]:   # идеальное исполнение команды
                    if c["name"] == "write_property":
                        a = c["args"]
                        state[a["device"]][a["property"]] = a["value"]
        elif e["kind"] == "question":
            checked += 1
            a, q, text = e["answer"], e["qtype"], e["text"]
            on = [s for s in sensors if s["property"] == "motion" and s["value"] == 1]
            door_opens = [s for s in sensors if s["device"] == "door_front" and s["value"] == 1]
            thermo = next(d for d in h["devices"] if d["type"] == "climate_sensor")["id"]
            if q == "last_door_open":
                test.assertEqual(a["value"], door_opens[-1]["t"])
            elif q == "door_open_count":
                test.assertEqual(a["value"], len(door_opens))
            elif q == "last_motion_room":
                rooms = {devs[s["device"]]["location"] for s in on if s["t"] == a["value"]}
                test.assertTrue(any(room.ALL_ROOMS[r].ru_loc in text or room.ALL_ROOMS[r].en in text
                                    for r in rooms), e)
                r = next(r for r in rooms if room.ALL_ROOMS[r].ru_loc in text or room.ALL_ROOMS[r].en in text)
                test.assertEqual(max(s["t"] for s in on if devs[s["device"]]["location"] == r), a["value"])
            elif q == "last_room":
                test.assertEqual(a["value"], devs[on[-1]["device"]]["location"])
                test.assertIn(a["value"], a["accept"])
            elif q == "max_temp":
                vals = [h["initial_state"][thermo]["temperature"]] + \
                    [s["value"] for s in sensors if s["device"] == thermo and s["property"] == "temperature"]
                test.assertEqual(a["value"], max(vals))
            elif q in ("temp_now", "humidity_now", "door_open_now", "light_state"):
                ref = a["ref"]
                v = state[ref["device"]][ref["property"]]
                test.assertEqual(a["value"], bool(v) if a["unit"] == "bool" else v, e)
                if q == "light_state":
                    r = devs[ref["device"]]["location"]
                    test.assertTrue(room.ALL_ROOMS[r].ru_loc in text or room.ALL_ROOMS[r].en in text)
            elif q == "last_color":
                cols = [c["expect"]["calls"][0]["args"]["value"] for c in cmds
                        if c["action"] in ("light_color", "light_color_all")]
                test.assertEqual(a["value"], cols[-1])
            elif q == "coffee_count":
                test.assertEqual(a["value"], sum(c["action"] == "coffee_brew" for c in cmds))
            else:
                test.fail(f"неизвестный тип вопроса {q}")
            test.assertGreaterEqual(e["t"], w0)
            call = e["expect"]["calls"][0]
            test.assertEqual(call, {"name": "answer", "args": {"value": a["value"], "unit": a["unit"]}})
    return checked


class QuestionAnswersTest(unittest.TestCase):
    def test_answers_match_replayed_trace_synthetic(self):
        n = 0
        for h, ev in synth_days(15, profile="H", question_share=0.6, lang="mix"):
            n += replay_check_questions(self, h, ev)
        self.assertGreater(n, 100)

    @unittest.skipUnless(HAVE_SUBSET, "нет подмножества CASAS")
    def test_answers_match_replayed_trace_casas(self):
        n = 0
        qtypes = set()
        for seed in range(20):
            h, ev = generate_day(DayConfig(seed=seed, profile="H", split="any", question_share=0.5))
            n += replay_check_questions(self, h, ev)
            qtypes |= {e["qtype"] for e in ev if e["kind"] == "question"}
        self.assertGreater(n, 100)
        self.assertGreaterEqual(len(qtypes), 9)


# ---------------------------------------------------------------------------
# Предикаты ситуаций
# ---------------------------------------------------------------------------


def fake_builder(sensor_log: list[dict], *, door: bool = True, initial_open: int = 0,
                 pre_motion_t: float | None = None, overheat_t=(), leak_t=()) -> SimpleNamespace:
    devs = [{"id": "pir_kitchen", "type": "motion_sensor", "location": "kitchen"},
            {"id": "pir_entrance", "type": "motion_sensor", "location": "entrance"},
            {"id": "leak_bathroom", "type": "leak_sensor", "location": "bathroom"},
            {"id": "boiler", "type": "boiler", "location": "entrance"}]
    init = {}
    if door:
        devs.append({"id": "door_front", "type": "door_sensor", "location": "entrance"})
        init["door_front"] = {"open": initial_open}
    cfg = DayConfig(seed=0)
    return SimpleNamespace(cfg=cfg, w0=7 * 3600, w1=23 * 3600, devices=devs, dev={d["id"]: d for d in devs},
                           initial_state=init, sensor_log=sensor_log, pre_motion_t=pre_motion_t, pre_door_t=[],
                           overheat_t=list(overheat_t), leak_t=list(leak_t))


def S(t: float, dev: str, prop: str, value) -> dict:
    return {"t": float(t), "kind": "sensor", "device": dev, "property": prop, "value": value}


def H(hh: float) -> float:
    return hh * 3600


class SituationPredicateTest(unittest.TestCase):
    def names(self, b) -> list[tuple[str, float]]:
        return [(s["name"], s["t"]) for s in situations.detect(b)]

    def test_door_left_open(self):
        log = [S(H(9), "pir_kitchen", "motion", 1),
               S(H(10), "door_front", "open", 1), S(H(10) + 900, "door_front", "open", 0),   # 15 мин — да
               S(H(11), "door_front", "open", 1), S(H(11) + 300, "door_front", "open", 0),   # 5 мин — нет
               S(H(11.2), "pir_kitchen", "motion", 1)]
        got = [x for x in self.names(fake_builder(log, pre_motion_t=H(6.9))) if x[0] == "door_left_open"]
        self.assertEqual(got, [("door_left_open", H(10) + 600)])

    def test_door_open_late(self):
        log = [S(H(21.9), "door_front", "open", 1), S(H(22) + 300, "door_front", "open", 0),   # открыта на 22:00
               S(H(22.5), "door_front", "open", 1), S(H(22.5) + 20, "door_front", "open", 0)]  # открыли в 22:30
        got = [x for x in self.names(fake_builder(log)) if x[0] == "door_open_late"]
        self.assertEqual(got, [("door_open_late", H(22)), ("door_open_late", H(22.5))])

    def test_door_open_at_window_start_counts_from_w0(self):
        log = [S(H(7) + 1200, "door_front", "open", 0)]
        got = [x for x in self.names(fake_builder(log, initial_open=1)) if x[0] == "door_left_open"]
        self.assertEqual(got, [("door_left_open", H(7) + 600)])

    def test_silence_home_vs_away(self):
        log = [S(H(9), "pir_kitchen", "motion", 1),                 # тишина 3 ч дома -> no_motion_home в 11:00
               S(H(12), "pir_kitchen", "motion", 1),
               S(H(12) + 30, "door_front", "open", 1), S(H(12) + 45, "door_front", "open", 0),  # уход
               S(H(17), "pir_entrance", "motion", 1),               # тишина 5 ч после двери -> long_absence 16:00
               S(H(17.5), "pir_kitchen", "motion", 1), S(H(19), "pir_kitchen", "motion", 1),
               S(H(21), "pir_kitchen", "motion", 1), S(H(22.5), "pir_kitchen", "motion", 1)]
        got = [x for x in self.names(fake_builder(log, pre_motion_t=H(8.5))) if x[0] in
               ("no_motion_home", "long_absence")]
        self.assertEqual(got, [("no_motion_home", H(11)), ("long_absence", H(16))])

    def test_silence_needs_full_threshold_inside_awake_hours(self):
        log = [S(H(9), "pir_kitchen", "motion", 1), S(H(21.5), "pir_kitchen", "motion", 1)]
        got = [x for x in self.names(fake_builder(log, pre_motion_t=H(6))) if x[0] == "no_motion_home"]
        # тишина 06:00–09:00 началась до окна (утренний сон) — не ситуация; 09:00–21:30 — одна ситуация в 11:00
        self.assertEqual(got, [("no_motion_home", H(11))])

    def test_sleep_is_not_no_motion_home(self):
        """Утренний сон (последнее движение до 07:00) и отход ко сну (тишина не закончится до 21:00) — не ситуации."""
        log = [S(H(9.5), "pir_kitchen", "motion", 1), S(H(19.5), "pir_kitchen", "motion", 1)]
        got = [x for x in self.names(fake_builder(log, pre_motion_t=H(5.5))) if x[0] == "no_motion_home"]
        self.assertEqual(got, [("no_motion_home", H(11.5))])         # только дневная тишина 09:30–19:30
        b = fake_builder(log, pre_motion_t=H(5.5))
        b.cfg.awake = ("05:00", "23:00")                              # «часы бодрствования» — параметр правила
        got = [x for x in self.names(b) if x[0] == "no_motion_home"]
        self.assertEqual(got, [("no_motion_home", H(7.5)), ("no_motion_home", H(11.5)),
                               ("no_motion_home", H(21.5))])

    def test_door_proxy_without_contact(self):
        log = [S(H(10), "pir_entrance", "motion", 1), S(H(15), "pir_kitchen", "motion", 1),
               S(H(16.5), "pir_kitchen", "motion", 1), S(H(18), "pir_kitchen", "motion", 1),
               S(H(19.5), "pir_kitchen", "motion", 1), S(H(21), "pir_kitchen", "motion", 1),
               S(H(22.5), "pir_kitchen", "motion", 1)]
        got = [x for x in self.names(fake_builder(log, door=False, pre_motion_t=H(9.9)))
               if x[0] in ("no_motion_home", "long_absence")]
        self.assertEqual(got, [("long_absence", H(14))])

    def test_synthetic_situations_have_actions(self):
        b = fake_builder([], overheat_t=[H(10)], leak_t=[H(12)])
        sits = {s["name"]: s for s in situations.detect(b)}
        self.assertEqual(sits["overheat"]["expect"]["actions"][0]["args"],
                         {"device": "boiler", "property": "heating", "value": False})
        self.assertEqual(sits["leak"]["expect"]["actions_explicit"][0]["name"], "set_valve_water_open")


def verify_situations_on_trace(test: unittest.TestCase, h: dict, ev: list[dict]) -> int:
    """Каждая ситуация дня подтверждается выданной трассой; door_left_open — полный набор."""
    rules = h["situation_rules"]
    w0, w1 = h["window_s"]
    sensors = [e for e in ev if e["kind"] == "sensor"]
    sits = [e for e in ev if e["kind"] == "situation"]
    on = [e["t"] for e in sensors if e["property"] == "motion" and e["value"] == 1]
    for s in sits:
        t, ev_ = s["t"], s["evidence"]
        test.assertTrue(s["significant"])
        test.assertEqual(s["deadline_s"], rules["deadline_s"])
        if s["name"] == "door_left_open":
            o = ev_["open_since"]
            test.assertAlmostEqual(t, o + rules["door_left_open"]["min_open_s"], places=3)
            test.assertFalse(any(o < e["t"] <= t and e["device"] == "door_front" and e["value"] == 0
                                 for e in sensors))
        elif s["name"] in ("no_motion_home", "long_absence"):
            a = ev_["last_motion_t"]
            test.assertAlmostEqual(t - a, rules[s["name"]]["min_silence_s"], places=3)
            if s["name"] == "no_motion_home":                          # не сон: тишина в часах бодрствования
                aw0, aw1 = (int(x[:2]) * 3600 + int(x[3:]) * 60 for x in rules["no_motion_home"]["awake"])
                test.assertTrue(aw0 <= a and t <= aw1, s)
            test.assertFalse(any(a < x <= t for x in on), s)
            if a >= w0:
                test.assertIn(a, on)
        elif s["name"] == "overheat":
            prev = [e["value"] for e in sensors if e["device"] == "boiler" and e["property"] == "temperature"
                    and e["t"] < t]
            cur = [e["value"] for e in sensors if e["device"] == "boiler" and e["property"] == "temperature"
                   and e["t"] == t]
            test.assertTrue(cur and cur[0] >= rules["overheat"]["threshold"])
            test.assertTrue(not prev or prev[-1] < rules["overheat"]["threshold"])
        elif s["name"] == "leak":
            test.assertTrue(any(e["t"] == t and e["device"] == rules["leak"]["device"] and e["value"] == 1
                                for e in sensors))
        elif s["name"] == "door_open_late":
            pass
        else:
            test.fail(s["name"])
    # полнота door_left_open: каждая открытая ≥ N мин дверь дала ситуацию
    if rules["door_left_open"]["enabled"]:
        n = rules["door_left_open"]["min_open_s"]
        expected, since = [], (w0 if h["initial_state"]["door_front"]["open"] == 1 else None)
        for e in [x for x in sensors if x["device"] == "door_front"] + [{"t": w1, "value": 0}]:
            if e["value"] == 1:
                since = e["t"]
            elif since is not None:
                if min(e["t"], w1) - since >= n and since + n < w1:
                    expected.append(round(since + n, 3))
                since = None
        test.assertEqual(sorted(s["t"] for s in sits if s["name"] == "door_left_open"), expected)
    n_synth = sum(s["source"] == "synthetic" for s in sits)
    test.assertEqual(n_synth, rules["synthetic_per_day"])
    return len(sits)


class SituationTraceTest(unittest.TestCase):
    def test_synthetic_days(self):
        for k in (0, 2, 5):
            for h, ev in synth_days(6, profile="L", synthetic_situations=k):
                verify_situations_on_trace(self, h, ev)

    @unittest.skipUnless(HAVE_SUBSET, "нет подмножества CASAS")
    def test_casas_days(self):
        names = set()
        for seed in range(40):
            h, ev = generate_day(DayConfig(seed=seed, profile="L", split="any"))
            verify_situations_on_trace(self, h, ev)
            names |= {e["name"] for e in ev if e["kind"] == "situation"}
        self.assertTrue({"overheat", "leak", "no_motion_home"} <= names)


class SplitAndScheduleTest(unittest.TestCase):
    """tune и holdout при одинаковых seed — независимая нагрузка; (дом, сутки) — по расписанию без повторов."""

    @staticmethod
    def load(ev: list[dict]) -> tuple:
        us = [e for e in ev if e["kind"] in ("user_cmd", "question")]
        return tuple(e["k"] for e in us), tuple(e["kind"] for e in us)

    def test_split_salts_user_load_synthetic(self):
        same = sum(self.load(generate_day(DayConfig(seed=s, profile="M+", source="synthetic", split="tune"))[1]) ==
                   self.load(generate_day(DayConfig(seed=s, profile="M+", source="synthetic", split="holdout"))[1])
                   for s in range(1, 21))
        self.assertLessEqual(same, 2)      # до соли split — 20 из 20 (ревью: 30 из 30 на CASAS)

    @unittest.skipUnless(HAVE_SUBSET, "нет подмножества CASAS")
    def test_split_salts_user_load_casas(self):
        same = 0
        for s in range(1, 16):
            a = generate_day(DayConfig(seed=s, profile="M+", split="tune"))
            b = generate_day(DayConfig(seed=s, profile="M+", split="holdout"))
            self.assertEqual((a[0]["split"], b[0]["split"]), ("tune", "holdout"))
            same += self.load(a[1]) == self.load(b[1])
        self.assertLessEqual(same, 2)

    @unittest.skipUnless(HAVE_SUBSET, "нет подмножества CASAS")
    def test_schedule_without_replacement_and_balanced(self):
        from collections import Counter

        from bench.daygen.generator import schedule_pair
        idx = casas.load_index(casas.env_subset_dir())
        for split, n in (("tune", 175), ("holdout", 63), ("any", 126)):
            pairs = [schedule_pair(idx, split, s) for s in range(1, n + 1)]
            self.assertEqual(len(set(pairs)), n, split)                       # без повторов
            homes = Counter(h for h, _ in pairs)
            # поровну: невыровненный по кругам диапазон — разброс не больше 2 (неполные первый и последний круги)
            self.assertLessEqual(max(homes.values()) - min(homes.values()), 2, (split, homes))
            nh = len(homes)
            aligned = Counter(schedule_pair(idx, split, s)[0] for s in range(nh * 5))
            self.assertEqual(set(aligned.values()), {5}, split)              # полные круги — строго поровну
            for h, d in pairs:
                m = idx["homes"][h]
                self.assertIn(d, m["eligible_days"])
                if split != "any":
                    self.assertEqual(m["split"], split)
        self.assertEqual(generate_day(DayConfig(seed=5, profile="L", split="tune"))[0]["home"],
                         schedule_pair(idx, "tune", 5)[0])

    def test_split_required_for_casas(self):
        with self.assertRaises(ValueError):
            DayBuilder(DayConfig(seed=1, profile="L"))                         # CASAS без split и home
        DayBuilder(DayConfig(seed=1, profile="L", source="synthetic"))        # синтетике split не нужен
        with self.assertRaises(ValueError):
            DayBuilder(DayConfig(seed=1, profile="L", split="validation"))


class ShortWindowTest(unittest.TestCase):
    """Все события лежат в [window_s[0]; window_s[1]) и при окне короче сессии (ревью: 189/200 дней S200 07:00–07:30)."""

    def test_events_inside_short_windows(self):
        truncated = 0
        for prof, win, n in (("S200", ("07:00", "07:30"), 60), ("H", ("07:00", "08:00"), 60), ("S69", ("07:00", "07:05"), 20)):
            for seed in range(n):
                h, ev = generate_day(DayConfig(seed=seed, profile=prof, source="synthetic", window=win,
                                               synthetic_situations=0))
                w0, w1 = h["window_s"]
                bad = [e for e in ev if not w0 <= e["t"] < w1]
                self.assertEqual(bad, [], (prof, win, seed))
                us = [e["t"] for e in ev if e["kind"] in ("user_cmd", "question")]
                self.assertTrue(all(t <= max(w1 - 60, w0 + 1) or w1 - w0 <= 120 for t in us))
                truncated += h["counts"]["utterances_truncated"]
        self.assertGreater(truncated, 0)

    def test_full_window_not_truncated(self):
        for seed in range(30):
            h, _ = generate_day(DayConfig(seed=seed, profile="S200", source="synthetic"))
            self.assertEqual(h["counts"]["utterances_truncated"], 0)


if __name__ == "__main__":
    unittest.main()
