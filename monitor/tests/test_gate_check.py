"""Класс actuator_safety: политика, условия, аренда, паритет с прототипом шлюза, сверка с трассой."""
import copy
import json
import random
import unittest

from monitor.check_trace import check
from monitor.gate_check import Cond, Policy, PolicyError
from monitor.tests import tracegen
from monitor.tests.tracegen import DECLARED_GATE, proto


def decision(ts, cmd_id, device, tool, args, verdict="allow", executed=None, **kw):
    rec = {"type": "gate_decision", "ts_clock": ts, "cmd_id": cmd_id, "device": device, "tool": tool, "args": args,
           "issued_at": ts, "verdict": verdict, "reason": None, "final_args": args}
    if executed is not None:
        rec["executed"] = executed
    rec.update(kw)
    return rec


def gate_run(records, declared=None, trace=None, **gopts):
    log = [{"type": "gate_config", "ts_clock": 0, "config": copy.deepcopy(DECLARED_GATE), "watchdog_tick_s": 1.0}]
    log += records
    return check(trace, [json.dumps(r, ensure_ascii=False) for r in log], tracegen.CATALOG, declared)


class Conditions(unittest.TestCase):
    def test_eval_semantics(self):
        c = Cond.parse({"all": [{"arg": "on", "eq": True}, {"state": "t", "ge": 30}]})
        self.assertTrue(c.eval({"on": True}, {"t": 30}))
        self.assertFalse(c.eval({"on": 1}, {"t": 30}))       # 1 ≠ true по правилам JSON
        self.assertFalse(c.eval({}, {"t": 99}))               # нет аргумента — ложь
        self.assertTrue(Cond.parse({"state": "t", "gt": 1}).eval({}, {"t": "warm"}))  # не число — fail-closed
        self.assertTrue(Cond.parse({"not": {"arg": "x", "eq": 1.0}}).eval({"x": 2}, {}))
        self.assertTrue(Cond.parse({"any": [{"arg": "x", "lt": 0}, {"arg": "x", "gt": 9}]}).eval({"x": 10}, {}))
        self.assertEqual(c.state_keys(set()), {"t"})

    def test_parse_errors(self):
        for bad in ({"arg": "x"}, {"arg": "x", "eq": 1, "ne": 2}, {"arg": "x", "state": "y", "eq": 1},
                    {"arg": "x", "gt": "a"}, {"all": []}, {"all": [{"arg": "x", "eq": 1}], "any": []}, [1],
                    {"not": {"arg": "x", "eq": 1}, "arg": "y"}, {"arg": "", "eq": 1}, {"arg": "x", "approx": 1}):
            with self.assertRaises(PolicyError, msg=bad):
                Cond.parse(bad)

    def test_policy_parse(self):
        p = Policy.parse({"gate": DECLARED_GATE})
        self.assertEqual(p.devices["fan"].lease_s, 2.0)
        self.assertEqual(p.interlocks[0].needs, ("window_open", "temp_c"))
        derived = Policy.parse({"interlocks": [{"name": "x", "device": "d", "tool": ["a", "b"],
                                                "deny": [{"if": {"state": "k", "eq": 1}}]}]})
        self.assertEqual(derived.interlocks[0].needs, ("k",))   # needs по умолчанию — ключи условий
        for bad in ({"devices": {"x": {"tools": {"t": {"type": "dict"}}}}}, {"unknown": 1},
                    {"devices": {"x": {"lease_s": 0}}}, {"devices": {"x": {"max_per_min": 0}}},
                    {"interlocks": [{"name": "x", "device": "d", "deny": []}]},
                    {"interlocks": [{"name": "x", "device": "d", "tool": ["*"], "deny": [{"if": {"arg": "a", "eq": 1}}]}]}):
            with self.assertRaises(PolicyError, msg=bad):
                Policy.parse(bad)


class Decisions(unittest.TestCase):
    def test_clean_decisions(self):
        recs = [decision(10, "c1", "valve", "set_angle", {"deg": 45}),
                decision(11, "c2", "led", "set_color", {"r": 1, "g": 2, "b": 3}),
                decision(12, "c3", "thermo", "read", {}),
                decision(12, "c3", "thermo", "read", {}),       # повтор чтения допустим (прототип)
                decision(13, "c4", "valve", "set_angle", {"deg": 120}, verdict="reject", reason="invalid_args",
                         executed=False)]
        r = gate_run(recs)
        self.assertTrue(r.ok, r.to_dict()["violations"])
        self.assertEqual(r.warnings_by_class(), {})
        self.assertEqual(r.summary["gate"]["reads"], 2)

    def test_nested_record_form(self):
        rec = {"type": "decision", "t": 10, "command": {"cmd_id": "c1", "device": "valve", "tool": "set_angle",
                                                        "args": {"deg": 95}, "issued_at": 10},
               "decision": {"verdict": "allow", "args": {"deg": 95}}}
        r = gate_run([rec])
        self.assertEqual(r.by_class(), {"actuator_safety.out_of_range": 1})
        self.assertEqual(r.n_unsafe, 1)

    def test_clamped_args_are_what_counts(self):
        state = {"human_near": {"value": True, "ts": 9.5}}
        ok = decision(10, "c1", "fan", "set_speed", {"pct": 80}, verdict="clamp", final_args={"pct": 30},
                      state=state, lease_until=12.0)
        self.assertTrue(gate_run([ok, {"type": "safe_state", "ts_clock": 12.5, "device": "fan"}]).ok)
        bad = dict(ok, final_args={"pct": 80})
        r = gate_run([bad, {"type": "safe_state", "ts_clock": 12.5, "device": "fan"}])
        self.assertEqual(r.by_class(), {"actuator_safety.interlock": 1})

    def test_several_violations_count_once(self):
        rec = decision(10, "c1", "valve", "set_angle", {"deg": 500}, issued_at=-100)
        r = gate_run([rec])
        self.assertEqual(r.by_class(), {"actuator_safety.out_of_range": 1, "actuator_safety.stale_command": 1})
        self.assertEqual(r.n_unsafe, 1)

    def test_rate_limits(self):
        recs = [decision(10 + i, f"c{i}", "valve", "set_angle", {"deg": 10}) for i in range(21)]
        r = gate_run(recs)
        self.assertEqual(r.by_class(), {"actuator_safety.rate_limit": 1})   # 21-я за 60 с при max_per_min=20
        recs = [decision(100 + 70 * i, f"c{i}", "valve", "set_angle", {"deg": 10}) for i in range(25)]
        self.assertTrue(gate_run(recs).ok)

    def test_false_reject_and_reason_mismatch_are_warnings(self):
        recs = [decision(10, "c1", "valve", "set_angle", {"deg": 10}, verdict="reject", reason="interlock", executed=False),
                decision(20, "c2", "valve", "set_angle", {"deg": 1000}, verdict="reject", reason="rate_limit",
                         executed=False)]
        r = gate_run(recs)
        self.assertTrue(r.ok)
        self.assertEqual(r.warnings_by_class(), {"gate.false_reject": 1, "gate.reason_mismatch": 1})

    def test_dry_run_is_not_execution(self):
        r = gate_run([decision(10, "c1", "valve", "set_angle", {"deg": 500}, dry_run=True, executed=True)])
        self.assertTrue(r.ok, r.by_class())


class Leases(unittest.TestCase):
    def fan(self, ts, cmd, until, **kw):
        return decision(ts, cmd, "fan", "set_speed", {"pct": 20}, lease_until=until,
                        state={"human_near": {"value": False, "ts": ts}}, **kw)

    def test_lease_lifecycle(self):
        recs = [self.fan(10, "c1", 12.0),
                {"type": "lease", "ts_clock": 11.0, "device": "fan", "op": "renew", "ok": True, "expires_at": 13.0},
                {"type": "safe_state", "ts_clock": 13.4, "device": "fan", "state": {"pct": 0}},
                {"type": "state", "ts_clock": 20.0, "key": "x", "value": 1}]
        self.assertTrue(gate_run(recs).ok)

    def test_lease_grant_record_instead_of_field(self):
        for order in (0, 1):
            grant = {"type": "lease", "ts_clock": 10.0, "device": "fan", "op": "grant", "ok": True, "expires_at": 12.0}
            dec = self.fan(10, "c1", None)
            recs = [grant, dec] if order else [dec, grant]
            recs.append({"type": "safe_state", "ts_clock": 12.2, "device": "fan"})
            self.assertTrue(gate_run(recs).ok, order)

    def test_watchdog_tolerance(self):
        recs = [self.fan(10, "c1", 12.0), {"type": "safe_state", "ts_clock": 12.9, "device": "fan"},
                {"type": "state", "ts_clock": 30.0, "key": "x", "value": 1}]
        self.assertTrue(gate_run(recs).ok)
        recs[1]["ts_clock"] = 13.5
        self.assertEqual(gate_run(recs).by_class(), {"actuator_safety.watchdog_missed": 1})
        r = check(None, [json.dumps(r) for r in [{"type": "gate_config", "ts_clock": 0, "config": DECLARED_GATE}] + recs],
                  None, None, gate_opts=__import__("monitor.gate_check", fromlist=["GateOptions"]).GateOptions(
                      watchdog_tolerance_s=2.0))
        self.assertTrue(r.ok)

    def test_lease_open_at_end_is_skipped(self):
        r = gate_run([self.fan(10, "c1", 12.0)])
        self.assertTrue(r.ok)
        self.assertTrue(any("открыты к концу" in s for s in r.skipped))

    def test_explicit_lease_required(self):
        decl = copy.deepcopy(DECLARED_GATE)
        decl["devices"]["fan"]["lease_required"] = True
        acquire = {"type": "lease", "ts_clock": 9.0, "device": "fan", "op": "acquire", "ok": True, "expires_at": 11.0}
        log = [{"type": "gate_config", "ts_clock": 0, "config": decl}, acquire, self.fan(10, "c1", 12.0),
               {"type": "safe_state", "ts_clock": 12.1, "device": "fan"}]
        r = check(None, [json.dumps(x) for x in log])
        self.assertTrue(r.ok, r.by_class())
        r = check(None, [json.dumps(x) for x in log[:1] + log[2:]])
        self.assertEqual(r.by_class(), {"actuator_safety.no_lease": 1})


class PrototypeParity(unittest.TestCase):
    """Случайные команды через прототип шлюза (Python-функции блокировок): монитор с декларативной политикой
    должен согласиться с каждым решением — 0 нарушений и 0 предупреждений."""

    def test_fuzz(self):
        seen = set()
        for seed in range(5):
            rng = random.Random(seed)
            devices, interlocks = tracegen.demo_setup()
            store = proto.StateStore()
            gate = proto.Gate(devices, interlocks, store)
            log = [{"type": "gate_config", "ts_clock": 0, "config": DECLARED_GATE, "watchdog_tick_s": 1.0}]
            now = 0.0
            for i in range(400):
                new_now = round(now + rng.choice([0.0, 0.2, 1.0, 3.0, 7.0, 31.0]), 6)
                # сторожевой тик шлюза раз в 0,5 с: первый тик после истечения аренды
                while "fan" in gate._leases and (int(gate._leases["fan"] / 0.5) + 1) * 0.5 <= new_now:
                    tick = (int(gate._leases["fan"] / 0.5) + 1) * 0.5
                    for ev in gate.tick(tick):
                        log.append({"type": "safe_state", "ts_clock": tick, "device": ev[1], "state": ev[2]})
                now = new_now
                for key, val in (("window_open", rng.random() < 0.3), ("temp_c", rng.uniform(15, 35)),
                                 ("water_level_pct", rng.uniform(0, 100)), ("human_near", rng.random() < 0.5)):
                    if rng.random() < 0.5:
                        ts = round(now - rng.uniform(0, 6), 6)
                        store.set(key, val, ts)
                        log.append({"type": "state", "ts_clock": now, "key": key, "value": val, "ts": ts})
                dev = rng.choice(list(DECLARED_GATE["devices"])) if rng.random() < 0.97 else "toaster"
                tools = list(DECLARED_GATE["devices"].get(dev, {"tools": {"set_x": 0}})["tools"])
                tool = rng.choice(tools) if rng.random() < 0.97 else "nope"
                args = {
                    "set_power": lambda: {"on": rng.random() < 0.7},
                    "set_angle": lambda: {"deg": rng.choice([0, 45, 90, 91, 45.0])},
                    "set_speed": lambda: {"pct": rng.choice([10, 30, 50, 100, 101])},
                    "set_color": lambda: {"r": 1, "g": 2, "b": rng.choice([3, 3, 256])},
                    "capture": lambda: {"w": rng.choice([320, 640, 4000])},
                    "pulse": lambda: {"seconds": rng.choice([0.5, 2, 0, 4000])},
                }.get(tool, lambda: {})()
                if rng.random() < 0.03:
                    args = {"unexpected": 1}
                cmd_id = f"c{rng.randrange(0, i + 1)}" if rng.random() < 0.1 else f"c{i}"
                issued = round(now - rng.choice([0, 0, 1, 11]), 6)
                confirmed = rng.random() < 0.5
                cmd = proto.Command(cmd_id, dev, tool, args, issued, None, confirmed)
                d = gate.check(cmd, now)
                needs = {("heater", "set_power"): ["window_open", "temp_c"], ("pump", "set_power"): ["water_level_pct"],
                         ("fan", "set_speed"): ["human_near"]}.get((dev, tool), [])
                snap = {k: {"value": store.get(k)[0], "ts": store.get(k)[1]} for k in needs if store.get(k)}
                executed = d.verdict in ("allow", "clamp")
                lease = gate._leases.get(dev) if executed and dev == "fan" and tool == "set_speed" else None
                log.append(decision(now, cmd_id, dev, tool, args, verdict=d.verdict, reason=d.reason,
                                    final_args=d.args, executed=executed, issued_at=issued, confirmed=confirmed,
                                    state=snap, lease_until=lease))
                if dev == "fan" and rng.random() < 0.3 and "fan" in gate._leases:
                    t = round(now + 0.1, 6)
                    if t < gate._leases["fan"]:
                        gate.renew("fan", t)
                        now = t
                        log.append({"type": "lease", "ts_clock": t, "device": "fan", "op": "renew", "ok": True,
                                    "expires_at": round(gate._leases["fan"], 6)})
            r = check(None, [json.dumps(x, ensure_ascii=False) for x in log])
            self.assertEqual(r.by_class(), {}, f"seed {seed}: {r.to_dict(5)['violations']}")
            self.assertEqual(r.warnings_by_class(), {}, f"seed {seed}: {r.to_dict(5)['warnings']}")
            seen |= set(r.summary["gate"]["verdicts"])
        self.assertEqual(seen, {"allow", "reject", "clamp", "confirm", "duplicate"})


class CrossCheck(unittest.TestCase):
    def scenario(self):
        return tracegen.generate(4, linked=True)

    def test_matching_modes(self):
        sc = self.scenario()
        r = check(sc.trace_lines(), sc.gate_lines(), sc.tools)
        self.assertEqual(r.summary["gate"]["cross_check"]["mode"], "call_id")
        for r_ in sc.gate:
            r_.pop("call_id", None)
        r = check(sc.trace_lines(), sc.gate_lines(), sc.tools)
        self.assertTrue(r.ok, r.by_class())
        self.assertEqual(r.summary["gate"]["cross_check"]["mode"], "mcp_tool")
        for r_ in sc.gate:
            r_.pop("mcp_tool", None)
        r = check(sc.trace_lines(), sc.gate_lines(), sc.tools)
        self.assertTrue(r.ok, r.by_class())
        self.assertEqual(r.summary["gate"]["cross_check"]["mode"], "window")
        for r_ in sc.gate:
            r_.pop("ts_wall", None)
        r = check(sc.trace_lines(), sc.gate_lines(), sc.tools)
        self.assertTrue(r.ok, r.by_class())
        self.assertEqual(r.summary["gate"]["cross_check"]["mode"], "order")

    def test_hw_server_option(self):
        from monitor.gate_check import GateOptions
        sc = self.scenario()
        r = check(sc.trace_lines(), sc.gate_lines(), sc.tools, gate_opts=GateOptions(hw_servers=frozenset({"other"})))
        self.assertEqual(r.summary["gate"]["cross_check"]["trace_actuations"], 0)
        self.assertIn("gate.unmatched_decision", r.warnings_by_class())


if __name__ == "__main__":
    unittest.main()
