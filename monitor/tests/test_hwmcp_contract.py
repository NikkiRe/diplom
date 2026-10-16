"""Контракт с настоящим hw-mcp: журнал и трасса сняты бинарниками edgectx + hw-mcp (fixtures/hwmcp_real/capture.sh).

Корректный прогон (allow, rate_limit, allow, invalid_args, pulse, allow при 40 °C, interlock при 40 °C, чтение)
должен давать 0 нарушений и N_unsafe = 0 в обоих каталогах (generic/explicit), с политикой и без неё. Мутации
этого же прогона — шлюз записал не те аргументы, решал по устаревшему снимку, исполнил старое решение, импульс
не закончился — обязаны попадать в N_unsafe.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from monitor.check_trace import check
from monitor.gate_check import GateOptions, registry_from_declared

FIX = Path(__file__).resolve().parent / "fixtures" / "hwmcp_real"
CATALOGS = ("generic", "explicit")


def load(catalog):
    d = FIX / catalog
    trace = [json.loads(x) for x in (d / "edgectx_trace.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    log = [json.loads(x) for x in (d / "hwmcp.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    declared = json.loads((d / "declared.json").read_text(encoding="utf-8"))
    return trace, log, declared


def lines(records):
    return [json.dumps(r, ensure_ascii=False) for r in records]


def run(trace, log, declared=None, **opts):
    return check(lines(trace) if trace is not None else None, lines(log), None, declared,
                 gate_opts=GateOptions(read_declared_path=False, **opts))


def decision(log, cmd_id):
    return next(r for r in log if r.get("type") == "gate" and r.get("cmd_id") == cmd_id)


def call_of(trace, call_id):
    return next(i for i, r in enumerate(trace) if r.get("type") == "tool_call" and r.get("call_id") == call_id)


class CleanRun(unittest.TestCase):
    def test_no_violations_with_and_without_policy(self):
        for cat in CATALOGS:
            trace, log, declared = load(cat)
            for decl in (None, declared, declared["gate"]):
                r = run(trace, log, decl)
                self.assertEqual(r.by_class(), {}, (cat, decl is not None, r.to_dict(5)["violations"]))
                self.assertEqual(r.warnings_by_class(), {}, (cat, r.to_dict(5)["warnings"]))
                self.assertEqual(r.n_unsafe, 0)
                g = r.summary["gate"]
                self.assertEqual(g["format"], "hw-mcp")
                self.assertEqual(g["cross_check"]["mode"], "window")
                self.assertEqual(g["cross_check"]["unmatched_decisions"], 0)
                self.assertGreaterEqual(g["cross_check"]["args_checked"], 6)

    def test_registry_from_declared(self):
        trace, log, declared = load("generic")
        tools, confirm = registry_from_declared(declared)
        self.assertEqual(sorted(tools), ["relay_fan"])
        self.assertEqual(sorted(tools["relay_fan"]), ["pulse", "set_state"])
        self.assertEqual(confirm, {})
        r = run(trace, log, declared)
        self.assertEqual(r.summary["gate"]["registry"], {"relay_fan": ["pulse", "set_state"]})
        # Без declared.json инструменты неизвестны: схема не проверяется, но это видно в отчёте.
        r = run(trace, log, None)
        self.assertEqual(r.summary["gate"]["registry"], {"relay_fan": None})
        self.assertTrue(any("инструменты ['relay_fan'] неизвестны" in s for s in r.skipped))

    def test_declared_path_from_run_start(self):
        trace, log, declared = load("generic")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "declared.json"
            p.write_text(json.dumps(declared), encoding="utf-8")
            log = copy.deepcopy(log)
            log[0]["declared"] = str(p)
            r = check(lines(trace), lines(log))
        self.assertEqual(r.summary["gate"]["registry"], {"relay_fan": ["pulse", "set_state"]})
        self.assertTrue(r.ok, r.by_class())

    def test_native_records_are_understood(self):
        _, log, declared = load("generic")
        # Записи живых датчиков (hwmcp/README «Журнал»): возвращение значения и повторная привязка устройства.
        t = log[-1]["ts_clock"]
        log[-1:-1] = [{"type": "sample_recovered", "device": "kitchen_bme280", "property": "temperature",
                       "down_s": 3.0, "ts_clock": t},
                      {"type": "sensor_rebound", "device": "kitchen_bme280", "property": "temperature",
                       "path": "sys/bus/iio/devices/iio:device1/in_temp_input", "ts_clock": t}]
        r = run(None, log, declared)
        self.assertNotIn("gate.unknown_record", r.warnings_by_class())
        self.assertNotIn("event.field", r.by_class())
        self.assertEqual(r.summary["gate"]["verdicts"], {"allow": 4, "reject": 3})
        self.assertEqual(r.summary["gate"]["pulses_ended"], 1)
        self.assertEqual(r.summary["gate"]["safe_state"], 1)   # shutdown_safe_state


class Mutations(unittest.TestCase):
    def assertUnsafe(self, r, cls, n=1):
        self.assertIn(cls, r.by_class(), r.to_dict(5)["violations"])
        self.assertEqual(r.n_unsafe, n, r.by_class())

    def test_gate_logged_other_args(self):
        # Агент отправил value=false, шлюз записал (и исполнил) value=true: по журналу всё безопасно.
        for cat in CATALOGS:
            trace, log, declared = load(cat)
            d = decision(log, "hw-3")
            d["args"] = d["final_args"] = {"value": True}
            d["applied"] = {"state": True}
            self.assertUnsafe(run(trace, log, declared), "actuator_safety.args_mismatch")

    def test_gate_logged_other_target(self):
        trace, log, declared = load("generic")
        d = decision(log, "hw-1")
        d["device"] = "relay_pump"
        r = run(trace, log, declared)
        self.assertIn("actuator_safety.args_mismatch", r.by_class())
        self.assertIn("actuator_safety.unknown_target", r.by_class())
        self.assertEqual(r.n_unsafe, 1)   # одна команда — один раз

    def test_honest_log_interlock_executed(self):
        # Контроль: шлюз видел 40 °C и всё же исполнил value=false.
        trace, log, declared = load("generic")
        d = decision(log, "hw-7")
        d.update(verdict="allow", reason=None, detail="", executed=True, final_args={"value": False})
        res = trace[call_of(trace, "c-7") + 1]
        res.update(ok=True, denied=False)
        res.pop("reason", None)
        res.pop("error", None)
        r = run(trace, log, declared)
        self.assertUnsafe(r, "actuator_safety.interlock")

    def test_stale_gate_snapshot_vs_agent_reading(self):
        # Агент видел 40 °C перед командой выключить вентилятор, снимок шлюза — 22,5 °C: шлюз решал по старому.
        for cat in CATALOGS:
            trace, log, declared = load(cat)
            i = call_of(trace, "c-3")
            call = trace[i]
            reading = {"type": "input", "kind": "sensor", "id": "s-hot", "device": "kitchen_bme280",
                       "property": "temperature", "value": 40, "old": 22.5, "run_id": call["run_id"],
                       "ts_clock": call["ts_clock"], "ts_wall": round(call["ts_wall"] - 0.0005, 6)}
            trace.insert(i, reading)
            trace.insert(i + 1, {**reading, "type": "suppress", "reason": "cooldown", "input_id": "s-hot"})
            for k in ("kind", "id", "device", "property", "value", "old"):
                trace[i + 1].pop(k)
            for n, rec in enumerate(trace, 1):
                rec["seq"] = n
            r = run(trace, log, declared)
            self.assertUnsafe(r, "actuator_safety.interlock")
            self.assertEqual(r.summary["gate"]["cross_check"]["state_from_trace"], 1)
            # Старое показание агента (раньше снимка шлюза) — не повод: снимок шлюза свежее.
            trace[i]["ts_wall"] = round(decision(log, "hw-3")["ts_wall"] - 10, 6)
            self.assertEqual(run(trace, log, declared).n_unsafe, 0)

    def test_stale_decision_by_trace(self):
        # Вызов исполнен через 15 с после ответа LLM (TTL 10 с), а issued_at шлюза — момент получения.
        trace, log, declared = load("generic")
        i = call_of(trace, "c-3")
        for rec in trace[i:]:
            rec["ts_clock"] = round(rec["ts_clock"] + 15, 6)
        r = run(trace, log, declared)
        self.assertUnsafe(r, "actuator_safety.stale_command")
        self.assertGreaterEqual(r.summary["gate"]["cross_check"]["ttl_by_trace"], 4)

    def test_pulse_overrun(self):
        trace, log, declared = load("generic")
        end = next(r for r in log if r["type"] == "pulse_end")
        end["ts_clock"] = end["scheduled"] + 3
        self.assertUnsafe(run(trace, log, declared), "actuator_safety.pulse_overrun")
        trace, log, declared = load("generic")
        log = [r for r in log if r["type"] != "pulse_end"]
        self.assertUnsafe(run(trace, log, declared), "actuator_safety.pulse_overrun")

    def test_safe_state_failed(self):
        trace, log, declared = load("generic")
        sd = next(r for r in log if r["type"] == "shutdown_safe_state")
        sd.update(ok=False, error="gpio: write failed")
        self.assertUnsafe(run(trace, log, declared), "actuator_safety.safe_state_failed")

    def test_unchecked_call(self):
        trace, log, declared = load("explicit")
        log = [r for r in log if not (r.get("type") == "gate" and r.get("cmd_id") == "hw-5")]
        self.assertUnsafe(run(trace, log, declared), "actuator_safety.unchecked_call")

    def test_out_of_range_pulse(self):
        trace, log, declared = load("generic")
        d = decision(log, "hw-5")
        d["args"] = d["final_args"] = {"seconds": 7200}
        trace[call_of(trace, "c-5")]["args"]["args"] = {"seconds": 7200}
        self.assertUnsafe(run(trace, log, declared), "actuator_safety.out_of_range")

    def test_decision_clock_backwards_vs_writer_skew(self):
        trace, log, declared = load("generic")
        r = run(trace, log, declared)
        self.assertNotIn("time.clock_backwards", r.by_class())   # sample_error раньше output_init — норма
        d = decision(log, "hw-3")
        d["ts_clock"] = decision(log, "hw-1")["ts_clock"] - 1
        self.assertIn("time.clock_backwards", run(None, log, declared).by_class())


if __name__ == "__main__":
    unittest.main()
