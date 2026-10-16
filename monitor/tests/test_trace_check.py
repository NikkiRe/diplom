"""Проверка трассы edgectx: корректные трассы → 0 нарушений; точечные случаи контракта."""
import json
import unittest

from monitor.catalog import Catalog
from monitor.check_trace import check
from monitor.tests import tracegen
from monitor.trace_check import TraceOptions, wall_seconds


class Builder:
    """Ручная сборка маленькой трассы: записи получают run_id, seq, монотонные ts_wall/ts_clock."""

    def __init__(self, **run_start):
        self.recs = []
        self.t = 25200.0
        self.wall = 1_791_300_000.0
        rs = {"config_hash": "sha256:x", "policy": {"max_steps": 3}, "sched": {"max_in_flight": 1}}
        rs.update(run_start)
        self.add("run_start", **rs)

    def add(self, typ, dt=0.0, dwall_ms=1.0, **fields):
        self.t += dt
        self.wall += dwall_ms / 1000
        self.recs.append({"type": typ, "run_id": "r1", "seq": len(self.recs) + 1, "ts_wall": round(self.wall, 6),
                          "ts_clock": self.t, **fields})
        return self

    def req(self, rid, turn=None, purpose=None, **kw):
        f = dict(req_id=rid, model="m", prompt_tokens_est=100,
                 segments={"prefix": 50, "profile": 10, "history": 10, "state": 10, "turn": 20}, n_tools=3)
        if turn is not None:
            f["turn_id"] = turn
        if purpose is not None:
            f["purpose"] = purpose
        f.update(kw)
        return self.add("llm_request", **f)

    def resp(self, rid, n=0, prompt=100, latency=5.0, **kw):
        f = dict(req_id=rid, latency_ms=latency, usage={"prompt_tokens": prompt, "completion_tokens": 5,
                                                        "cached_tokens": 0},
                 finish_reason="tool_calls" if n else "stop", n_tool_calls=n)
        f.update(kw)
        return self.add("llm_response", dwall_ms=latency + 1, **f)

    def call(self, cid, name="notify_user", server="local", args=None, ok=True, denied=False, **kw):
        self.add("tool_call", call_id=cid, server=server, name=name, args={"text": "x"} if args is None else args, **kw)
        return self.add("tool_result", call_id=cid, ok=ok, denied=denied, ms=1.0)

    def user(self, iid, dt=10.0):
        return self.add("input", dt=dt, kind="user", id=iid)

    def wake(self, reason, iid=None, **kw):
        return self.add("wake", reason=reason, suppressed=False, input_id=iid, **kw)

    def reply(self, iid, latency=10.0):
        return self.add("user_reply", input_id=iid, text="ок", latency_ms=latency)

    def end(self):
        return self.add("run_end")

    def lines(self):
        return [json.dumps(r, ensure_ascii=False) for r in self.recs]


CATALOG = tracegen.CATALOG


def run(b: Builder, tools=CATALOG, **opts):
    return check(b.lines(), None, tools, trace_opts=TraceOptions(**opts) if opts else None)


class CorrectCorpus(unittest.TestCase):
    def test_correct_runs_have_no_findings(self):
        for sc in tracegen.corpus(6):
            r = check(sc.trace_lines(), sc.gate_lines(), sc.tools)
            self.assertEqual(r.by_class(), {}, sc.name)
            self.assertEqual(r.warnings_by_class(), {}, sc.name)
            self.assertEqual(r.n_unsafe, 0, sc.name)

    def test_minimal_turn(self):
        b = Builder().user("u1").wake("user", "u1").req("r1").resp("r1", n=1).call("c1").req("r2").resp("r2")
        r = run(b.reply("u1").end())
        self.assertTrue(r.ok, r.to_dict()["violations"])
        s = r.summary["trace"]
        self.assertEqual(s["llm_calls"], 2)
        self.assertEqual(s["attribution"], "positional")
        self.assertEqual(s["llm_turns_started_by_reason"], {"user": 1})


class Protocol(unittest.TestCase):
    def test_llm_error_closes_request_and_allows_retry(self):
        b = Builder().user("u1").wake("user", "u1").req("r1").add("error", where="llm", msg="timeout", req_id="r1")
        b.req("r2").resp("r2").reply("u1").end()
        r = run(b)
        self.assertTrue(r.ok, r.by_class())
        self.assertEqual(r.summary["trace"]["llm_failed"], 1)

    def test_tool_error_record_closes_call(self):
        b = Builder().user("u1").wake("user", "u1").req("r1").resp("r1", n=1)
        b.add("tool_call", call_id="c1", server="local", name="notify_user", args={"text": "x"})
        b.add("error", where="mcp", msg="connection reset", call_id="c1")
        r = run(b.req("r2").resp("r2").reply("u1").end())
        self.assertTrue(r.ok, r.by_class())

    def test_exempt_purpose_needs_no_wake(self):
        b = Builder().req("s1", purpose="summary").resp("s1").end()
        self.assertTrue(run(b).ok)
        b = Builder().req("s1", purpose="other").resp("s1").end()
        self.assertEqual(run(b).by_class(), {"wake.missing": 1})

    def test_summary_between_steps_does_not_break_turn(self):
        # Служебный запрос посреди хода не «съедает» продолжение хода.
        b = Builder().user("u1").wake("user", "u1").req("r1").resp("r1", n=1).call("c1")
        b.req("s1", purpose="summary").resp("s1").req("r2").resp("r2").reply("u1").end()
        self.assertTrue(run(b).ok, run(b).by_class())

    def test_positional_max_steps(self):
        b = Builder().user("u1").wake("user", "u1")
        for i in range(4):
            b.req(f"r{i}").resp(f"r{i}", n=1).call(f"c{i}")
        r = run(b.reply("u1").end())
        self.assertIn("budget.max_steps", r.by_class())

    def test_linked_mode(self):
        b = Builder().user("u1").wake("user", "u1", turn_id="t1").req("r1", turn="t1").resp("r1", n=2)
        b.call("c1", req_id="r1").call("c2", req_id="r1").req("r2", turn="t1").resp("r2").reply("u1").end()
        r = run(b)
        self.assertTrue(r.ok, r.by_class())
        self.assertEqual(r.summary["trace"]["attribution"], "linked")
        self.assertEqual(r.summary["trace"]["llm_calls_by_wake_reason"], {"user": 2})
        # вызов ссылается на ответ без вызовов
        b = Builder().user("u1").wake("user", "u1", turn_id="t1").req("r1", turn="t1").resp("r1")
        r = run(b.call("c1", req_id="r1").reply("u1").end())
        self.assertIn("tool.count_mismatch", r.by_class())

    def test_rule_sourced_calls(self):
        b = Builder().add("input", kind="sensor", id="s1").add("suppress", reason="rule_handled", input_id="s1")
        b.add("rule_fired", rule_id="rule-1", input_id="s1")
        b.call("c1", server="hw", name="invoke_action", args={"device": "fan", "action": "set_speed", "args": {"pct": 0}})
        self.assertTrue(run(b.end()).ok)
        b = Builder().add("input", kind="sensor", id="s1").add("suppress", reason="rule_handled", input_id="s1")
        b.call("c1", server="hw", name="invoke_action", args={"device": "fan", "action": "set_speed"}, rule_id="nope")
        self.assertIn("tool.unsourced", run(b.end()).by_class())

    def test_concurrent_positional_no_false_positive(self):
        # Два хода в полёте: запросы перемежаются; жадная атрибуция не даёт ложного wake.missing.
        b = Builder(sched={"max_in_flight": 2}).user("u1").wake("user", "u1").add("input", kind="tick", id="h1")
        b.wake("heartbeat", "h1").req("a1").req("b1").resp("a1", n=1).resp("b1", n=1).call("ca").call("cb")
        b.req("a2").resp("a2").req("b2").resp("b2").reply("u1").end()
        r = run(b)
        self.assertTrue(r.ok, r.by_class())
        self.assertEqual(r.summary["trace"]["max_in_flight_observed"], 2)
        self.assertTrue(any("max_steps" in s for s in r.skipped))
        r = run(b, max_in_flight=1)
        self.assertIn("llm.inflight", r.by_class())


class Budget(unittest.TestCase):
    def build(self, first_reason, second_reason, priority=None):
        b = Builder(budget={"daily_calls": 2, "daily_input_tokens": 10 ** 6, "degrade_at": 0.5})
        b.add("input", kind="tick", id="h1").wake("heartbeat", "h1").req("r1").resp("r1").req("r2", purpose="summary")
        b.resp("r2")
        kind = {"user": "user", "gate": "sensor", "heartbeat": "tick"}[second_reason]
        b.add("input", dt=60, kind=kind, id="x2")
        w = {"priority": priority} if priority else {}
        b.wake(second_reason, "x2", **w).req("r3").resp("r3")
        if second_reason == "user":
            b.reply("x2")
        return b.end()

    def test_user_served_after_exhaustion(self):
        self.assertTrue(run(self.build("heartbeat", "user")).ok)

    def test_background_after_exhaustion(self):
        self.assertIn("budget.daily_calls", run(self.build("heartbeat", "gate")).by_class())
        self.assertIn("budget.daily_calls", run(self.build("heartbeat", "heartbeat")).by_class())

    def test_degraded_only_for_certain_background(self):
        b = Builder(budget={"daily_calls": 4, "daily_input_tokens": 10 ** 6, "degrade_at": 0.5})
        b.add("input", kind="tick", id="h1").wake("heartbeat", "h1").req("r1").resp("r1").req("r2", purpose="summary")
        b.resp("r2").add("input", dt=60, kind="sensor", id="s2").wake("gate", "s2").req("r3").resp("r3")
        self.assertTrue(run(b.end()).ok)   # gate без priority: может быть safety — не проверяется
        b = Builder(budget={"daily_calls": 4, "daily_input_tokens": 10 ** 6, "degrade_at": 0.5})
        b.add("input", kind="tick", id="h1").wake("heartbeat", "h1").req("r1").resp("r1").req("r2", purpose="summary")
        b.resp("r2").add("input", dt=60, kind="sensor", id="s2").wake("gate", "s2", priority="background")
        b.req("r3").resp("r3")
        self.assertIn("budget.degraded", run(b.end()).by_class())

    def test_day_boundary_resets_budget(self):
        b = Builder(budget={"daily_calls": 1, "daily_input_tokens": 10 ** 6})
        b.add("input", kind="tick", id="h1").wake("heartbeat", "h1").req("r1").resp("r1")
        b.add("input", dt=86400, kind="tick", id="h2").wake("heartbeat", "h2").req("r2").resp("r2")
        r = run(b.end())
        self.assertTrue(r.ok, r.by_class())
        self.assertEqual(set(r.summary["trace"]["per_day"]), {"0", "1"})

    def test_cli_overrides_run_start(self):
        b = Builder().add("input", kind="tick", id="h1").wake("heartbeat", "h1").req("r1").resp("r1")
        b.add("input", dt=60, kind="tick", id="h2").wake("heartbeat", "h2").req("r2").resp("r2").end()
        self.assertTrue(run(b).ok)
        self.assertIn("budget.daily_calls", run(b, daily_calls=1).by_class())


class Warnings(unittest.TestCase):
    def test_wall_backwards_is_warning(self):
        b = Builder().user("u1").wake("user", "u1").req("r1").resp("r1").reply("u1").end()
        b.recs[3]["ts_wall"] -= 10
        r = run(b)
        self.assertTrue(r.ok)
        self.assertIn("time.wall_backwards", r.warnings_by_class())

    def test_latency_inconsistent(self):
        b = Builder().user("u1").wake("user", "u1").req("r1").resp("r1", latency=5000.0)
        b.recs[-1]["ts_wall"] = b.recs[-2]["ts_wall"] + 0.001
        r = run(b.reply("u1").end())
        self.assertIn("time.latency_inconsistent", r.warnings_by_class())

    def test_user_suppressed(self):
        # Вход пользователя подавлен (очередь переполнена): ответа не требуется, но это подозрительно.
        b = Builder().user("u1").add("suppress", reason="queue_full", input_id="u1")
        r = run(b.end())
        self.assertTrue(r.ok, r.by_class())
        self.assertIn("wake.user_suppressed", r.warnings_by_class())
        # Подавлен и всё же разбудил LLM — два исхода одного входа.
        b = Builder().user("u1").add("suppress", reason="duplicate", input_id="u1").wake("user", "u1")
        r = run(b.req("r1").resp("r1").reply("u1").end())
        self.assertIn("input.multi_outcome", r.by_class())


class LinkOutage(unittest.TestCase):
    """Обрыв связи с LLM и падение MCP-сервера (agent/README, «Плохая сеть…»): записи как у edgectx."""

    def outage(self) -> Builder:
        # Вопрос сорвался на первом обращении и отложен повторно; перегрев отложен до первого обращения;
        # после удачной пробы оба уходят одним ходом deferred, вопрос получает ответ с deferred: true.
        b = Builder().user("u1").wake("user", "u1", wake_id="w1", priority="user").req("r1", wake_id="w1")
        b.resp("r1", prompt=0, finish_reason="error", error="llm: network", error_kind="network", attempts=3)
        b.add("llm_unavailable", failures=1, error_kind="network", error="llm: network", retry_in_s=31.2)
        b.add("deferred", wake_id="w1", reason="user", priority="user", input_ids=["u1"], retry=True, pending=1)
        b.add("input", dt=5, kind="sensor", id="s1", device="kitchen", property="temperature", value=35)
        b.add("deferred", wake_id="w2", reason="gate", priority="safety", input_ids=["s1"], retry=False, pending=2)
        b.add("llm_probe", dt=30, ok=False, ms=2003.4, status=0, bytes=0, error="dial tcp: i/o timeout",
              error_kind="network", retry_in_s=58.1)
        b.add("llm_probe", dt=60, ok=True, ms=12.5, status=200, bytes=86)
        b.add("wake", reason="deferred", suppressed=False, wake_id="w3", priority="user", input_ids=["s1"],
              coalesced=["w1", "w2"])
        b.req("r2", wake_id="w3").resp("r2").add("llm_available", down_s=95.0)
        b.add("user_reply", input_id="u1", text="ок", latency_ms=95000.0, deferred=True, dwall_ms=95000.0)
        b.add("mcp_down", dt=10, server="hw", error="session closed")
        return b.add("mcp_up", dt=1, server="hw", attempts=2, down_s=1.0)

    def test_outage_trace_is_accepted(self):
        r = run(self.outage().end())
        self.assertTrue(r.ok, r.to_dict(5)["violations"])
        self.assertEqual(r.warnings_by_class(), {})
        s = r.summary["trace"]
        self.assertEqual(s["wakes_by_reason"], {"deferred": 1, "user": 1})
        self.assertEqual(s["inputs_by_outcome"], {"woke": 2})
        self.assertEqual(s["llm_failed"], 1)

    def test_deferred_input_needs_outcome(self):
        # Отложенный вход так и не попал в слитый ход и не был сброшен (suppress) — потерян.
        b = self.outage()
        b.add("input", kind="sensor", id="s2", device="kitchen", property="temperature", value=36)
        b.add("deferred", wake_id="w4", reason="gate", priority="safety", input_ids=["s2"], retry=False, pending=1)
        self.assertEqual(run(b.end()).by_class(), {"input.no_outcome": 1})
        b = self.outage()
        b.add("deferred", wake_id="w4", reason="gate", priority="safety", input_ids=["s9"], retry=False, pending=1)
        self.assertEqual(run(b.end()).by_class(), {"ref.dangling": 1})

    def test_new_records_are_validated(self):
        b = self.outage().add("deferred", wake_id="w4", reason="later", priority="safety", input_ids=[], pending=0)
        b.add("mcp_up", server="hw", attempts=0, down_s=1.0)
        self.assertEqual(run(b.end()).by_class(), {"event.field": 2})


class FieldsAndCatalog(unittest.TestCase):
    def test_iso_wall_time_and_ms(self):
        self.assertAlmostEqual(wall_seconds("2026-10-06T07:00:00.123456789Z"), 1791270000.123456, places=5)
        self.assertAlmostEqual(wall_seconds("2026-10-06T10:00:00.5+03:00"), 1791270000.5, places=5)
        self.assertAlmostEqual(wall_seconds("2026-10-06T07:00:00Z"), 1791270000.0, places=5)
        self.assertEqual(wall_seconds(1791270000123), 1791270000.123)
        self.assertIsNone(wall_seconds(True))

    def test_unknown_type_allowed_by_option(self):
        b = Builder().add("telemetry", x=1).end()
        self.assertIn("event.unknown_type", run(b).by_class())
        self.assertTrue(run(b, allow_types=frozenset({"telemetry"})).ok)

    def test_catalog_formats(self):
        schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
        for cat in ([{"name": "notify_user", "inputSchema": schema}],
                    {"tools": [{"name": "notify_user", "inputSchema": schema}]},
                    [{"type": "function", "function": {"name": "notify_user", "parameters": schema}}],
                    {"servers": {"local": {"tools": [{"name": "notify_user", "parameters": schema}]}}}):
            b = Builder().user("u1").wake("user", "u1").req("r1").resp("r1", n=1).call("c1", args={})
            r = run(b.req("r2").resp("r2").reply("u1").end(), tools=cat)
            self.assertEqual(r.by_class(), {"args.required": 1}, cat)
        bad = Catalog.from_obj([{"name": "a", "parameters": {"type": "dict"}}, {"name": "a"}])
        self.assertEqual(len(bad.problems), 2)

    def test_run_start_tools_used_when_no_catalog(self):
        b = Builder(tools=[{"name": "notify_user", "inputSchema": {"type": "object", "required": ["text"]}}])
        b.user("u1").wake("user", "u1").req("r1").resp("r1", n=1).call("c1", args={}).req("r2").resp("r2")
        r = run(b.reply("u1").end(), tools=None)
        self.assertEqual(r.by_class(), {"args.required": 1})
        r = run(Builder().end(), tools=None)
        self.assertTrue(any(s.startswith("args.*") for s in r.skipped))

    def test_string_args_parsed(self):
        b = Builder().user("u1").wake("user", "u1").req("r1").resp("r1", n=1)
        b.call("c1", args=json.dumps({"text": "x", "level": "info"})).req("r2").resp("r2").reply("u1").end()
        self.assertTrue(run(b).ok)

    def test_strict_integer(self):
        b = Builder().user("u1").wake("user", "u1").req("r1").resp("r1", n=1)
        b.call("c1", name="recent_events", args={"k": 5.0}).req("r2").resp("r2").reply("u1").end()
        self.assertTrue(run(b).ok)
        self.assertEqual(run(b, strict_integer=True).by_class(), {"args.type": 1})


if __name__ == "__main__":
    unittest.main()
