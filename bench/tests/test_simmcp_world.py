"""sim-mcp без транспорта: мир (проигрывание, приводы, таймеры), каталоги, оценщик, самопроверка дня."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bench.daygen import casas  # noqa: E402
from bench.daygen.generator import DayConfig, generate_day  # noqa: E402
from bench.simmcp import catalog  # noqa: E402
from bench.simmcp.evaluator import match_answer, parse_time_of_day  # noqa: E402
from bench.simmcp.selfcheck import is_consistent, oracle_replay  # noqa: E402
from bench.simmcp.server import SimMCP  # noqa: E402
from bench.simmcp.world import ClockError, World  # noqa: E402

DAY = generate_day(DayConfig(seed=11, profile="H", source="synthetic", trap_share=0.2, synthetic_situations=2))


def run(coro):
    return asyncio.run(coro)


class WorldTest(unittest.TestCase):
    def setUp(self):
        self.h, self.ev = DAY
        self.w = World(self.h, self.ev)

    def test_initial_state_and_clock(self):
        self.assertEqual(self.w.clock, self.h["window_s"][0])
        self.assertEqual(self.w.snapshot(), {d: dict(sorted(p.items())) for d, p in
                                             sorted(self.h["initial_state"].items())})

    def test_advance_applies_events_in_order(self):
        sensors = [e for e in self.ev if e["kind"] == "sensor"]
        t = sensors[50]["t"]
        changes, inputs = self.w.advance(t)
        self.assertEqual(len(changes), sum(1 for e in sensors if e["t"] <= t))
        last = {}
        for e in sensors:
            if e["t"] <= t:
                last[(e["device"], e["property"])] = e["value"]
        for (d, p), v in last.items():
            self.assertEqual(self.w.value(d, p), v)
        self.assertTrue(all(i["t"] <= t for i in inputs))
        with self.assertRaises(ClockError):
            self.w.advance(t - 1)

    def test_list_devices_compact_like_hw_mcp(self):
        devs = {d["id"]: d for d in self.w.list_devices()["devices"]}
        self.assertEqual(set(devs), {d["id"] for d in self.h["devices"]})
        # подписи как propSig у hw-mcp: единица / bool, «,rw» для записываемых; без диапазонов и перечней
        self.assertEqual(devs["light_kitchen"], {
            "id": "light_kitchen", "loc": "kitchen", "props": {"brightness": "%,rw", "on": "bool,rw", "color": ",rw"}})
        self.assertEqual(devs["vacuum"]["actions"], {"dock": "", "start": "room:string"})
        self.assertEqual(devs["pir_kitchen"]["props"], {"motion": ""})
        self.assertEqual(devs["thermo_living_room"]["kind"], "climate_sensor")
        self.assertEqual(devs["thermo_living_room"]["props"]["temperature"], "Cel")
        rich = {d["id"]: d for d in self.w.list_devices(rich=True)["devices"]}
        self.assertEqual(rich["light_kitchen"]["props"], {
            "brightness": "%,1..100,rw", "on": "bool,rw",
            "color": "amber|blue|green|orange|pink|purple|red|warm|white|yellow,rw"})
        self.assertEqual(rich["pir_kitchen"]["props"], {"motion": "0|1"})

    def test_state_resource_like_hw_mcp(self):
        """hw://state/<dev> — формат hub.DeviceState hw-mcp: device, now, properties{value, unit?, ts, age_s}."""
        sensors = [e for e in self.ev if e["kind"] == "sensor" and e["device"].startswith("thermo")]
        e = sensors[3]
        self.w.advance(e["t"] + 10)
        st = self.w.device_state(e["device"])
        self.assertEqual(set(st), {"device", "now", "properties"})
        self.assertEqual(st["now"], e["t"] + 10)
        cell = st["properties"][e["property"]]
        self.assertEqual(set(cell) - {"unit"}, {"value", "ts", "age_s"})
        self.assertEqual(cell["unit"], {"temperature": "Cel", "humidity": "%RH", "pressure": "Pa"}[e["property"]])
        last = [x for x in sensors if x["property"] == e["property"] and x["t"] <= e["t"] + 10][-1]
        self.assertEqual((cell["value"], cell["ts"], cell["age_s"]), (last["value"], last["t"], round(e["t"] + 10 - last["t"], 3)))
        self.assertNotIn("unit", self.w.device_state("light_kitchen")["properties"]["on"])

    def test_inventory_like_hw_mcp_agent_view(self):
        inv = {d["id"]: d for d in self.w.inventory()["devices"]}
        self.assertEqual(set(inv), {d["id"] for d in self.h["devices"]})
        lk = inv["light_kitchen"]
        self.assertEqual((lk["kind"], lk["location"]), ("light", "kitchen"))
        self.assertEqual(lk["properties"]["on"], {"type": "boolean", "writable": True})
        self.assertEqual(lk["properties"]["brightness"],
                         {"type": "integer", "writable": True, "unit": "%", "min": 1, "max": 100})
        self.assertEqual(inv["pir_kitchen"]["properties"]["motion"], {"type": "integer", "writable": False, "enum": [0, 1]})
        self.assertEqual(inv["thermo_living_room"]["properties"]["temperature"]["resolution"], 0.1)
        start = inv["vacuum"]["actions"]["start"]
        self.assertTrue(start["description"])
        self.assertEqual(start["input"]["type"], "object")
        self.assertEqual(start["input"]["properties"]["room"]["type"], "string")
        # значения состояния в инвентарь не попадают (иначе агент принял бы подписи за значения)
        for d in inv.values():
            for p in d["properties"].values():
                self.assertNotIn("value", p)

    def test_read_result_like_hw_mcp(self):
        r = self.w.read("thermo_living_room", "temperature")
        self.assertEqual(set(r), {"device", "property", "value", "unit", "age_s"})
        self.w.advance(self.w.clock + 12.34)
        self.assertEqual(self.w.read("light_kitchen", "on")["age_s"], 12.3)       # 0,1 с, как roundAge
        self.assertEqual(self.w.read("nope", "on"), {"error": "unknown_device",
                                                     "detail": "no device 'nope'; call list_devices"})
        self.assertEqual(self.w.read("light_kitchen", "nope")["error"], "unknown_property")

    def test_write_validation(self):
        w = self.w
        cases = [   # (аргументы, код шлюза hw-mcp, фрагмент пояснения)
            (("nope", "on", True), "unknown_device", "no device"),
            (("light_kitchen", "nope", True), "unknown_tool", "has no property"),
            (("pir_kitchen", "motion", 1), "unknown_tool", "read-only"),
            (("light_kitchen", "on", "yes"), "invalid_args", "expected boolean"),
            (("light_kitchen", "brightness", 0), "invalid_args", "out_of_range"),
            (("light_kitchen", "brightness", 55.5), "invalid_args", "expected integer"),
            (("light_kitchen", "color", "cyan"), "invalid_args", "invalid_value"),
        ]
        for args, reason, detail in cases:
            res, ch = w.write(*args)
            self.assertEqual((res["denied"], res["verdict"], res["reason"]), (True, "reject", reason), args)
            self.assertIn(detail, res["detail"])
            self.assertNotIn("ok", res)
            self.assertEqual(ch, [])
        res, ch = w.write("light_kitchen", "brightness", 40.0)   # целое во float допускается
        self.assertEqual(res, {"ok": True, "verdict": "allow", "executed": True, "device": "light_kitchen",
                               "property": "brightness", "value": 40})
        self.assertEqual(w.value("light_kitchen", "brightness"), 40)
        self.assertEqual(len(ch), 1)
        res, ch = w.write("light_kitchen", "brightness", 40)      # то же значение: успех без изменения
        self.assertTrue(res["ok"])
        self.assertEqual(ch, [])

    def test_actions_timers_and_busy(self):
        w = self.w
        t0 = w.clock
        res, ch = w.invoke("coffee_kitchen", "brew", {})
        self.assertEqual(res, {"ok": True, "verdict": "allow", "executed": True, "device": "coffee_kitchen",
                               "action": "brew", "property": "brewing", "value": True, "done_in_s": 300})
        self.assertTrue(w.value("coffee_kitchen", "brewing"))
        res, _ = w.invoke("coffee_kitchen", "brew", {})
        self.assertEqual((res["reason"], res["detail"]), ("interlock", "coffee_kitchen is busy (brewing=true)"))
        changes, _ = w.advance(t0 + 299)
        self.assertTrue(w.value("coffee_kitchen", "brewing"))
        changes, _ = w.advance(t0 + 300)
        self.assertFalse(w.value("coffee_kitchen", "brewing"))
        self.assertIn(("coffee_kitchen", "brewing", False, "timer"),
                      [(c.device, c.property, c.value, c.source) for c in changes])
        res, _ = w.invoke("vacuum", "start", {"room": "garage"})
        self.assertEqual(res["reason"], "invalid_args")
        self.assertIn("invalid_value", res["detail"])
        res, _ = w.invoke("vacuum", "start", {"floor": 2})
        self.assertEqual(res["reason"], "invalid_args")
        self.assertEqual(w.invoke("vacuum", "fly", {})[0]["reason"], "unknown_tool")
        res, _ = w.invoke("vacuum", "dock", {})
        self.assertEqual(res["applied"], {"cleaning": False, "zone": "dock"})

    def test_recent_includes_tool_changes(self):
        """Ресурс events (флаг extra_resources) отражает и записи агента, а не только трассу."""
        w = self.w
        w.advance(w.clock + 60)
        w.note_recent(w.write("plug_living_room", "on", True)[1])
        self.assertEqual(w.recent[-1]["source"], "tool")
        self.assertEqual((w.recent[-1]["device"], w.recent[-1]["value"]), ("plug_living_room", True))

    def test_new_action_cancels_old_timer(self):
        w = self.w
        t0 = w.clock
        w.invoke("vacuum", "start", {"room": "kitchen"})
        self.assertEqual(w.value("vacuum", "zone"), "kitchen")
        w.advance(t0 + 600)
        w.invoke("vacuum", "dock", {})
        w.advance(t0 + 900)
        w.invoke("vacuum", "start", {})
        w.advance(t0 + 1800)          # таймер первого запуска не должен остановить второй
        self.assertTrue(w.value("vacuum", "cleaning"))
        w.advance(t0 + 900 + 1800)
        self.assertFalse(w.value("vacuum", "cleaning"))
        self.assertEqual(w.value("vacuum", "zone"), "dock")


class CatalogTest(unittest.TestCase):
    def test_generic_and_explicit(self):
        h, _ = DAY
        tools, routes = catalog.build("generic", h["devices"])
        self.assertEqual([t["name"] for t in tools], ["list_devices", "read_property", "write_property",
                                                      "invoke_action", "board_health", "answer", "refuse"])
        tools, routes = catalog.build("explicit", h["devices"])
        names = [t["name"] for t in tools]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("set_light_kitchen_brightness", names)
        self.assertIn("start_vacuum", names)
        self.assertIn("brew_coffee_kitchen", names)
        self.assertNotIn("set_pir_kitchen_motion", names)        # только для записываемых свойств
        n_props = sum(len(d["properties"]) for d in h["devices"])
        n_rw = sum(1 for d in h["devices"] for p in d["properties"].values() if p.get("rw"))
        n_act = sum(len(d.get("actions", {})) for d in h["devices"])
        self.assertEqual(len(names), n_props + n_rw + n_act + 3)
        spec = next(t for t in tools if t["name"] == "set_light_kitchen_brightness")["inputSchema"]
        self.assertEqual(spec["properties"]["value"], {"type": "integer", "minimum": 1, "maximum": 100})
        self.assertEqual(routes["set_light_kitchen_brightness"], catalog.Route("write", "light_kitchen", "brightness"))
        self.assertTrue(all(len(n) <= 64 for n in names))
        desc = {t["name"]: t["description"] for t in tools}
        self.assertEqual(desc["read_thermo_living_room_temperature"], "Read temperature (Cel) of thermo_living_room")
        self.assertEqual(desc["set_boiler_heating"], "Set heating (boolean) of boiler @entrance")

    def test_generic_matches_hw_mcp(self):
        """Описания и схемы пяти общих инструментов — дословно как в hwmcp/internal/server (addGenericTools):
        токены каталога T0 и платы совпадают (кроме answer/refuse)."""
        tools = {t["name"]: t for t in catalog.generic_tools(eval_tools=False)[0]}
        self.assertEqual(tools["write_property"]["description"],
                         "Write a writable device property (checked by the safety gate)")
        self.assertEqual(tools["write_property"]["inputSchema"], {
            "type": "object", "additionalProperties": False, "required": ["device", "property", "value"],
            "properties": {"device": {"type": "string"}, "property": {"type": "string"}, "value": {}}})
        self.assertEqual(tools["list_devices"]["description"],
                         "List connected devices: properties (unit; rw = writable) and actions with arguments")
        self.assertEqual(set(tools), {"list_devices", "read_property", "write_property", "invoke_action",
                                      "board_health"})

    def test_eval_tools_switch_and_share(self):
        h, _ = DAY
        for cat in ("generic", "explicit"):
            with_eval, r1 = catalog.build(cat, h["devices"])
            without, r0 = catalog.build(cat, h["devices"], eval_tools=False)
            self.assertEqual(len(with_eval) - len(without), 2)
            self.assertNotIn("answer", r0)
            share = catalog.eval_share(with_eval)
            self.assertEqual(share["eval_tools"], ["answer", "refuse"])
            self.assertGreater(share["bytes_eval"], 0)
            if "tokens_eval" in share:
                self.assertLess(share["tokens_eval"], 120)            # было 176 (ревью), теперь ≈ 108
                self.assertLess(share["tokens_eval"], share["tokens_total"])


class AnswerMatchTest(unittest.TestCase):
    def test_time_formats(self):
        exp = {"value": 13 * 3600 + 5 * 60 + 30, "unit": "t", "tol": 60}
        for v in (47130, 47130.0, "13:05", "13:06:20", "2012-07-25T13:05:31", "в 13:05"):
            self.assertTrue(match_answer(exp, v, "t")["correct"], v)
        self.assertFalse(match_answer(exp, "13:10", "t")["correct"])
        self.assertTrue(match_answer(exp, "13:05", "time")["unit_ok"])
        self.assertIsNone(parse_time_of_day("вчера"))

    def test_numbers_bools_strings(self):
        self.assertTrue(match_answer({"value": 22.4, "unit": "Cel", "tol": 0.15}, "22.5 °C", "°C")["correct"])
        self.assertFalse(match_answer({"value": 22.4, "unit": "Cel", "tol": 0.15}, 22.6, "Cel")["correct"])
        self.assertTrue(match_answer({"value": 3, "unit": "count", "tol": 0}, "3", "раз")["correct"])
        self.assertTrue(match_answer({"value": True, "unit": "bool"}, "да", "bool")["correct"])
        self.assertTrue(match_answer({"value": False, "unit": "bool"}, "закрыта", "boolean")["correct"])
        self.assertTrue(match_answer({"value": 1, "unit": "bool"}, True, "bool")["correct"])  # open=1 из состояния
        room = {"value": "kitchen", "unit": "room", "accept": ["kitchen", "кухня", "на кухне", "pir_kitchen"]}
        self.assertTrue(match_answer(room, "Кухня", "комната")["correct"])
        self.assertFalse(match_answer(room, "bedroom", "room")["correct"])
        self.assertFalse(match_answer(room, "kitchen", "Cel")["correct"])   # единица не та
        live = match_answer({"value": 20.0, "unit": "Cel", "tol": 0.15}, 21.0, "Cel", live_value=21.0)
        self.assertTrue(live["correct"])


class SelfCheckTest(unittest.TestCase):
    """Идеальный агент (эталонные вызовы дня) получает 100 %; ничего не делающий — 0 %."""

    def test_oracle_consistent_synthetic(self):
        for seed in range(6):
            h, ev = generate_day(DayConfig(seed=seed, profile="H", source="synthetic", lang="mix",
                                           trap_share=0.2, question_share=0.4, synthetic_situations=5))
            for cat in ("generic", "explicit"):
                rep = run(oracle_replay(h, ev, cat))
                self.assertTrue(is_consistent(rep), (seed, cat, rep["summary"], rep["oracle_denied"],
                                                     rep["pre_satisfied"]))
                self.assertEqual(rep["summary"]["inputs_opened"], rep["summary"]["inputs_total"])

    @unittest.skipUnless(casas.subset_exists(), "нет подмножества CASAS")
    def test_oracle_consistent_casas(self):
        for seed in range(10):
            h, ev = generate_day(DayConfig(seed=seed, profile="S69", split="holdout" if seed % 2 else "tune"))
            rep = run(oracle_replay(h, ev))
            self.assertTrue(is_consistent(rep), (seed, rep["summary"]))

    def test_idle_agent_scores_zero(self):
        h, ev = DAY

        async def idle():
            sim = SimMCP(h, ev)
            await sim.advance(h["window_s"][1])
            return sim.finish()
        rep = run(idle())
        s = rep["summary"]
        self.assertEqual(s["commands"]["ok"], 0)
        self.assertEqual(s["questions"]["ok"], 0)
        self.assertEqual(s["situations_with_actions"]["ok"], 0)

    def test_wrong_answer_and_side_effect_detected(self):
        h, ev = generate_day(DayConfig(seed=3, profile="S69", source="synthetic", trap_share=0.5,
                                       question_share=0.3))
        q = next(e for e in ev if e["kind"] == "question" and e["answer"]["unit"] == "count")
        inv = next(e for e in ev if e["kind"] == "user_cmd" and e["class"] == "invalid")

        async def agent():
            sim = SimMCP(h, ev)
            for e in sorted([q, inv], key=lambda x: x["t"]):
                await sim.advance(e["t"])
                if e is q:
                    await sim.call("answer", {"value": q["answer"]["value"] + 1, "unit": "count"})
                else:
                    await sim.call("refuse", {"reason": "no_such_device"})
                    await sim.call("write_property", {"device": "plug_living_room", "property": "on", "value": True})
            return sim.finish()
        rep = run(agent())
        rows = {r["id"]: r for r in rep["inputs"]}
        self.assertFalse(rows[q["id"]]["ok"])
        self.assertTrue(rows[q["id"]]["answered"])
        self.assertFalse(rows[inv["id"]]["ok"])
        self.assertEqual(rows[inv["id"]]["side_effects"], 1)


if __name__ == "__main__":
    unittest.main()
