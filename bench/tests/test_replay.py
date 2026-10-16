"""Реплеер: разбор файла дня, порядок управляющих вызовов (§5.1), шаги часов, ситуации и дедлайны, manifest."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.replay import ReplayConfig, Replayer, ReplayError, load_day, parse_clock  # noqa: E402
from bench.replay.fakes import FakeServer  # noqa: E402
from bench.replay.manifest import sha256_file  # noqa: E402

HEADER = {"kind": "header", "seed": 42, "profile": "M+", "home": "hh101", "split": "tune",
          "window": ["07:00", "07:10"], "devices": ["relay1", "door1"]}
EVENTS = [
    {"t": 25210, "kind": "sensor", "device": "pir1", "property": "motion", "value": 1},
    {"t": 25300, "kind": "user_cmd", "id": "u1", "text": "включи свет", "lang": "ru", "k": 3,
     "expect": {"type": "action", "calls": [{"name": "write_property",
                                             "args": {"device": "relay1", "property": "on", "value": True}}]}},
    {"t": 25400, "kind": "sensor", "device": "door1", "property": "open", "value": 1},   # строка раньше ситуации
    {"t": 25400, "kind": "situation", "id": "s1", "name": "door_open", "significant": True, "deadline_s": 100,
     "expect": {"type": "notify_or_action"}},
    {"t": 25400, "kind": "situation", "id": "s1b", "name": "door_open_late", "deadline_s": 100},
    {"t": 25700, "kind": "question", "id": "q1", "text": "когда открывали дверь?", "answer": {"value": 25400, "unit": "t"}},
]


def write_day(path: Path, header=HEADER, events=EVENTS) -> Path:
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [header, *events]) + "\n", encoding="utf-8")
    return path


class Recorder:
    """Три HTTP-двойника, пишущие все управляющие вызовы в общий журнал (порядок важен)."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self._lock = threading.Lock()

    def _rec(self, who, path, reply):
        def f(body):
            with self._lock:
                self.calls.append((who, path, body))
            return reply(body) if callable(reply) else reply
        return f

    def servers(self):
        mock = FakeServer({("POST", p): self._rec("mock", p, {"ok": True}) for p in
                           ("/control/reset", "/control/clock", "/control/expect", "/control/expire")}
                          | {("GET", "/control/state"): lambda b: {"inflight": 0, "idle_ms": 1e9, "version": "rec"},
                             ("GET", "/control/journal"): lambda b: {"note": "journal"}})
        edge = FakeServer({("POST", "/v1/clock/advance"): self._rec("edge", "/v1/clock/advance",
                                                                     lambda b: {"t": b["to_t"]}),
                           ("POST", "/v1/user"): self._rec("edge", "/v1/user", {"reply": "ok"}),
                           ("POST", "/v1/event"): self._rec("edge", "/v1/event", {"ok": True}),
                           ("GET", "/v1/metrics"): lambda b: {"calls": 0}})
        sim = FakeServer({("POST", "/control/clock"): self._rec("sim", "/control/clock", {"ok": True}),
                          ("POST", "/control/input"): self._rec("sim", "/control/input", {"ok": True}),
                          ("POST", "/control/finish"): self._rec("sim", "/control/finish",
                                                                 {"summary": {"commands": {"n": 1, "ok": 1}}})})
        return mock, edge, sim

    def index(self, who, path, pred=lambda b: True):
        for i, (w, p, b) in enumerate(self.calls):
            if w == who and p == path and pred(b):
                return i
        raise AssertionError(f"no call {who} {path}")


class DayFileTest(unittest.TestCase):
    def test_parse_clock(self):
        self.assertEqual(parse_clock("07:00"), 25200)
        self.assertEqual(parse_clock("23:59:30"), 86370)
        self.assertEqual(parse_clock(25200), 25200.0)
        with self.assertRaises(ValueError):
            parse_clock(None)

    def test_load_sort_and_span(self):
        with tempfile.TemporaryDirectory() as d:
            day = load_day(write_day(Path(d) / "day.jsonl"))
        self.assertEqual(day.window, (25200.0, 25800.0))
        self.assertEqual(day.counts(), {"sensor": 2, "user_cmd": 1, "situation": 2, "question": 1})
        same_t = [e.kind for e in day.events if e.t == 25400]
        self.assertEqual(same_t, ["situation", "situation", "sensor"])   # ситуации раньше датчика того же момента
        self.assertEqual(day.span(), (25200.0, 25800.0))
        self.assertEqual(len(day.sha256), 64)

    def test_window_s_preferred(self):
        with tempfile.TemporaryDirectory() as d:
            day = load_day(write_day(Path(d) / "day.jsonl", header={**HEADER, "window_s": [25200, 26400]}))
        self.assertEqual(day.window, (25200.0, 26400.0))

    def test_span_covers_events_outside_window(self):
        with tempfile.TemporaryDirectory() as d:
            ev = EVENTS + [{"t": 84000, "kind": "situation", "id": "night", "deadline_s": 60}]
            day = load_day(write_day(Path(d) / "day.jsonl", events=ev))
        self.assertEqual(day.span(), (25200.0, 84000.0))

    def test_errors(self):
        bad = [
            [HEADER, {"t": 1, "kind": "user_cmd", "text": "x"}],                                   # без id
            [HEADER, {"t": 1, "kind": "question", "id": "a"}, {"t": 2, "kind": "user_cmd", "id": "a"}],   # дубль
            [HEADER, HEADER],
            [{**HEADER, "window": ["10:00", "09:00"]}],
            [HEADER, {"kind": "sensor"}],
        ]
        with tempfile.TemporaryDirectory() as d:
            for i, rows in enumerate(bad):
                p = Path(d) / f"bad{i}.jsonl"
                p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
                with self.assertRaises(ValueError, msg=i):
                    load_day(p)
            p = Path(d) / "unknown.jsonl"
            write_day(p, events=[{"t": 25300, "kind": "camera", "id": "c1"}])
            self.assertTrue(load_day(p).warnings)


class ProtocolOrderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.day_path = write_day(d / "day.jsonl")
        (d / "trace.jsonl").write_text('{"type":"run_start"}\n')
        (d / "edgectx.yaml").write_text("clock: virtual\n")
        rec = Recorder()
        mock, edge, sim = rec.servers()
        with mock, edge, sim:
            cfg = ReplayConfig(day=str(cls.day_path), out_root=str(d / "results"), edgectx=edge.url, sim=sim.url,
                               mock=mock.url, run_id="r1", max_step_s=60, settle_ms=1,
                               collect={"edgectx_trace": str(d / "trace.jsonl"), "sysmon": str(d / "missing.jsonl")},
                               configs={"edgectx": str(d / "edgectx.yaml")},
                               binaries={"day": str(cls.day_path)})
            rp = Replayer(cfg)
            cls.run_dir = rp.run()
            rp.close()
        cls.rec, cls.rp = rec, rp

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_step_order_and_size(self):
        steps = [(w, b) for w, p, b in self.rec.calls if p in ("/control/clock", "/v1/clock/advance")]
        # каждый шаг — тройка: mock clock → edgectx advance → sim clock с одним и тем же временем
        self.assertEqual(len(steps) % 3, 0)
        prev = None
        for i in range(0, len(steps), 3):
            (w1, b1), (w2, b2), (w3, b3) = steps[i:i + 3]
            self.assertEqual((w1, w2, w3), ("mock", "edge", "sim"))
            self.assertEqual(b1["t"], b2["to_t"])
            self.assertEqual(b2["to_t"], b3["t"])
            if prev is not None:
                self.assertGreater(b2["to_t"], prev)
                self.assertLessEqual(b2["to_t"] - prev, 60 + 1e-9)
            prev = b2["to_t"]
        self.assertEqual(steps[0][1]["t"], 25200.0)
        self.assertEqual(prev, 25800.0)
        self.assertEqual(self.rec.calls[0][:2], ("mock", "/control/reset"))

    def test_user_expect_after_advance_right_before_post(self):
        # Ожидание реплики — после продвижения часов до t и сразу перед /v1/user: фоновые пробуждения окна
        # продвижения не могут его перехватить (ревью: ожидание регистрировалось до advance_to).
        i_exp = self.rec.index("mock", "/control/expect", lambda b: b["input_id"] == "u1")
        i_adv = self.rec.index("sim", "/control/clock", lambda b: b["t"] == 25300)
        i_user = self.rec.index("edge", "/v1/user", lambda b: b["id"] == "u1")
        self.assertLess(i_adv, i_exp)
        self.assertLess(i_exp, i_user)
        between = [(w, p) for w, p, _ in self.rec.calls[i_exp + 1:i_user]]
        self.assertEqual(between, [("sim", "/control/input")])
        body = self.rec.calls[i_exp][2]
        self.assertEqual((body["text"], body["kind"], body["expect"]["type"], body["k"]),
                         ("включи свет", "user_cmd", "action", 3))
        self.assertEqual(self.rec.calls[i_user][2], {"id": "u1", "text": "включи свет"})

    def test_sim_input_opened_before_user_and_finish_at_end(self):
        i_in = self.rec.index("sim", "/control/input", lambda b: b["id"] == "u1")
        i_user = self.rec.index("edge", "/v1/user", lambda b: b["id"] == "u1")
        self.assertLess(i_in, i_user)
        i_fin = self.rec.index("sim", "/control/finish")
        last_clock = max(i for i, (w, p, _) in enumerate(self.rec.calls) if w == "sim" and p == "/control/clock")
        self.assertGreater(i_fin, last_clock)
        rep = json.loads((self.run_dir / "simmcp_report.json").read_text())
        self.assertEqual(rep["summary"]["commands"], {"n": 1, "ok": 1})

    def test_question_expect_carries_answer(self):
        i = self.rec.index("mock", "/control/expect", lambda b: b["input_id"] == "q1")
        self.assertEqual(self.rec.calls[i][2]["expect"], {"type": "answer", "answer": {"value": 25400, "unit": "t"}})

    def test_situation_registered_just_before_its_time_and_expired_at_deadline(self):
        i_eps = self.rec.index("sim", "/control/clock", lambda b: abs(b["t"] - (25400 - 0.001)) < 1e-9)
        i_exp = self.rec.index("mock", "/control/expect", lambda b: b["input_id"] == "s1")
        i_exp_b = self.rec.index("mock", "/control/expect", lambda b: b["input_id"] == "s1b")
        i_t = self.rec.index("sim", "/control/clock", lambda b: b["t"] == 25400)
        self.assertLess(i_eps, i_exp)
        self.assertLess(i_exp, i_t)
        self.assertLess(i_exp_b, i_t)       # обе ситуации момента t — до публикации датчиков t
        body = self.rec.calls[i_exp][2]
        self.assertEqual(body["situation"], {"id": "s1", "name": "door_open", "significant": True,
                                             "deadline_s": 100, "t": 25400})
        i_dead = self.rec.index("sim", "/control/clock", lambda b: b["t"] == 25500)   # шаг ровно на дедлайн
        i_expire = self.rec.index("mock", "/control/expire", lambda b: b["input_id"] == "s1")
        self.assertLess(i_dead, i_expire)
        self.assertNotIn("/v1/event", [p for _, p, _ in self.rec.calls])   # sensor_mode=sim: события не постятся

    def test_manifest_and_files(self):
        m = json.loads((self.run_dir / "manifest.json").read_text())
        self.assertEqual(m["run_id"], "r1")
        self.assertTrue(m["dev_run"])
        self.assertEqual(m["day"]["sha256"], sha256_file(self.day_path))
        self.assertEqual((m["day"]["seed"], m["day"]["profile"]), (42, "M+"))
        self.assertEqual(m["configs"]["edgectx"]["content"], "clock: virtual\n")
        self.assertEqual(m["binaries"]["day"]["sha256"], sha256_file(self.day_path))
        self.assertTrue(m["collected"]["sysmon"]["missing"])
        self.assertTrue(any("sysmon" in e for e in m["errors"]))
        for name in ("day.jsonl", "replay.jsonl", "edgectx_trace.jsonl", "mockllm_journal.jsonl",
                     "edgectx_metrics.json", "mockllm_state.json", "simmcp_report.json"):
            self.assertIn(name, m["files"])
            self.assertEqual(m["files"][name]["sha256"], sha256_file(self.run_dir / name))
        self.assertEqual(m["replay_stats"]["user_inputs"], 2)
        self.assertEqual(m["replay_stats"]["situations"], 2)
        self.assertEqual(m["replay_stats"]["expired"], 2)
        log = [json.loads(x) for x in (self.run_dir / "replay.jsonl").read_text().splitlines()]
        self.assertEqual((log[0]["action"], log[-1]["action"]), ("start", "end"))
        users = [x for x in log if x["action"] == "user"]
        self.assertTrue(all(u["ok"] and u["rtt_ms"] >= 0 for u in users))

    def test_existing_run_dir_refused(self):
        cfg = ReplayConfig(day=str(self.day_path), out_root=str(self.run_dir.parent), run_id="r1", dry_run=True)
        with self.assertRaises(ReplayError):
            Replayer(cfg).run()


class SyncWithNotificationsTest(unittest.TestCase):
    """Ревью: уведомления sim-mcp асинхронны — следующий шаг часов ждёт, пока агент их примет (min_inputs по
    контракту agent/README; после 3 промахов — mcp.updates из /v1/metrics); тишина mock-llm не ждётся."""

    def run_day(self, agent_counts_sensors: bool, sync: str = "auto"):
        calls = []
        state = {"notified": 0, "sensor": 0}

        def advance(b):
            calls.append(("edge", "/v1/clock/advance", b))
            if agent_counts_sensors:
                state["sensor"] = state["notified"]
            resp = {"now_t": b["to_t"], "timers_fired": 0, "inputs": {"sensor": state["sensor"]}, "idle": True}
            need = (b.get("min_inputs") or {}).get("sensor")
            if need is not None and state["sensor"] < need:
                resp["missing_inputs"] = {"sensor": need - state["sensor"]}
            return resp

        def sim_clock(b):
            calls.append(("sim", "/control/clock", b))
            n = sum(1 for e in EVENTS if e["kind"] == "sensor" and calls_t["prev"] < e["t"] <= b["t"])
            calls_t["prev"] = b["t"]
            state["notified"] += n
            return {"t": b["t"], "applied": n, "notified": n}

        def metrics(b):
            calls.append(("edge", "/v1/metrics", b))
            return {"mcp": {"updates": state["notified"]}, "inputs_total": {"sensor": state["sensor"]}}

        calls_t = {"prev": -1.0}
        edge = FakeServer({("POST", "/v1/clock/advance"): advance, ("POST", "/v1/user"): lambda b: {"text": "ok"},
                           ("GET", "/v1/metrics"): metrics})
        sim = FakeServer({("POST", "/control/clock"): sim_clock, ("POST", "/control/input"): lambda b: {"ok": True},
                          ("POST", "/control/finish"): lambda b: {"summary": {}}})
        with tempfile.TemporaryDirectory() as d, edge, sim:
            day = write_day(Path(d) / "day.jsonl")
            rp = Replayer(ReplayConfig(day=str(day), out_root=d, run_id="s", edgectx=edge.url, sim=sim.url,
                                       sync=sync, sync_timeout_s=0.2))
            run_dir = rp.run()
            rp.close()
            manifest = json.loads((run_dir / "manifest.json").read_text())
        return calls, rp, manifest

    def test_min_inputs_follow_notified(self):
        calls, rp, m = self.run_day(agent_counts_sensors=True)
        adv = [b for w, p, b in calls if p == "/v1/clock/advance"]
        with_min = [b for b in adv if "min_inputs" in b]
        # два датчика дня → два шага с ожиданием: base (inputs.sensor из прошлого ответа) + notified
        self.assertEqual([b["min_inputs"]["sensor"] for b in with_min], [1, 2])
        self.assertTrue(all(b["timeout_ms"] == 200 for b in with_min))
        sync = rp.stats["sync"]
        self.assertEqual((sync["waits"], sync["ok"], sync["missing"], sync["metrics_polls"]), (2, 2, 0, 1))
        self.assertEqual(rp.stats["settles"], 0)                  # settle_ms = 0 по умолчанию
        self.assertEqual(m["warnings"], [])

    def test_auto_falls_back_to_updates_after_misses(self):
        # агент принимает уведомления, но не считает их входами sensor (как в текущей связке edgectx+sim-mcp)
        calls, rp, m = self.run_day(agent_counts_sensors=False)
        sync = rp.stats["sync"]
        self.assertEqual(sync["missing"], 2)                      # в дне только два датчика — до 3 промахов
        self.assertIsNone(sync["fallback_at_t"])
        self.assertTrue(any("промахов min_inputs 2" in w for w in m["warnings"]))
        calls, rp, m = self.run_day(agent_counts_sensors=False, sync="updates")
        polls = [p for w, p, b in calls if p == "/v1/metrics"]
        self.assertGreaterEqual(len(polls), 3)                    # база + два ожидания (+ метрики в конце)
        self.assertFalse(any("min_inputs" in b for w, p, b in calls if p == "/v1/clock/advance"))
        self.assertEqual((rp.stats["sync"]["ok"], rp.stats["sync"]["timeouts"]), (2, 0))

    def test_auto_switch_after_three_misses(self):
        from bench.replay import replayer as rmod
        old = rmod.AUTO_FALLBACK_MISSES
        rmod.AUTO_FALLBACK_MISSES = 1
        try:
            calls, rp, m = self.run_day(agent_counts_sensors=False)
        finally:
            rmod.AUTO_FALLBACK_MISSES = old
        self.assertIsNotNone(rp.stats["sync"]["fallback_at_t"])
        adv = [b for w, p, b in calls if p == "/v1/clock/advance"]
        self.assertEqual(sum("min_inputs" in b for b in adv), 1)  # после первого промаха — только updates
        self.assertGreater(rp.stats["sync"]["metrics_polls"], 0)


class DryRunAndCliTest(unittest.TestCase):
    def test_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            day = write_day(Path(d) / "day.jsonl")
            rp = Replayer(ReplayConfig(day=str(day), out_root=d, run_id="dry", dry_run=True, max_step_s=120))
            run_dir = rp.run()
            m = json.loads((run_dir / "manifest.json").read_text())
            self.assertTrue(m["replay_params"]["dry_run"])
            adv = [json.loads(x)["to_t"] for x in (run_dir / "replay.jsonl").read_text().splitlines()
                   if json.loads(x)["action"] == "advance"]
            self.assertEqual(adv[0], 25200.0)
            self.assertEqual(adv[-1], 25800.0)
            self.assertTrue(all(b - a <= 120 for a, b in zip(adv, adv[1:])))

    def test_sensor_post_mode(self):
        rec = Recorder()
        mock, edge, sim = rec.servers()
        with tempfile.TemporaryDirectory() as d, mock, edge, sim:
            day = write_day(Path(d) / "day.jsonl")
            rp = Replayer(ReplayConfig(day=str(day), out_root=d, run_id="p", edgectx=edge.url, mock=mock.url,
                                       sensor_mode="post", settle_ms=0))
            rp.run()
            rp.close()
        posted = [b for _, p, b in rec.calls if p == "/v1/event"]
        self.assertEqual([(b["device"], b["t"]) for b in posted], [("pir1", 25210), ("door1", 25400)])

    def test_unreachable_edgectx_aborts(self):
        with tempfile.TemporaryDirectory() as d:
            day = write_day(Path(d) / "day.jsonl")
            rp = Replayer(ReplayConfig(day=str(day), out_root=d, run_id="x", edgectx="http://127.0.0.1:9",
                                       settle_ms=0, http_timeout_s=2))
            with self.assertRaises(ReplayError):
                rp.run()
            manifest = json.loads((rp.run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "aborted")
            self.assertFalse(manifest["completed"])
            rp.close()

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            day = write_day(Path(d) / "day.jsonl")
            r = subprocess.run([sys.executable, "-m", "bench.replay", "--day", str(day), "--out", d, "--run-id", "c",
                                "--dry-run"], cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)["run_dir"], str(Path(d) / "c"))
            r = subprocess.run([sys.executable, "-m", "bench.replay", "--day", str(day), "--out", d],
                               cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2)
            self.assertIn("--edgectx", r.stderr)


class CompletionRegressionTest(unittest.TestCase):
    def test_failed_user_http_is_not_a_success(self):
        def fail(_):
            raise RuntimeError("user handler failed")

        edge = FakeServer({("POST", "/v1/clock/advance"): lambda b: {"idle": True},
                           ("POST", "/v1/user"): fail})
        with tempfile.TemporaryDirectory() as d, edge:
            day = write_day(Path(d) / "day.jsonl", header={"kind": "header", "window_s": [0, 1]},
                            events=[{"kind": "user_cmd", "id": "u", "t": 1, "text": "x"}])
            rp = Replayer(ReplayConfig(day=str(day), out_root=d, run_id="user-fail", edgectx=edge.url,
                                       shutdown=False, sync="none"))
            try:
                rd = rp.run()
            finally:
                rp.close()
            users = [json.loads(x) for x in (rd / "replay.jsonl").read_text().splitlines()
                     if json.loads(x)["action"] == "user"]
            self.assertFalse(users[0]["ok"])
            self.assertEqual(rp.stats["user_errors"], 1)
            self.assertEqual(json.loads((rd / "manifest.json").read_text())["status"], "completed_with_errors")

    def test_updates_barrier_finishes_work_before_deadline_expiry_and_finish(self):
        state = {"updates": 0, "active": False, "prev": -1, "flushed": []}

        def advance(b):
            if state["active"]:
                state["flushed"].append(b["to_t"])
            state["active"] = False
            return {"now_t": b["to_t"], "idle": True}

        def sim_clock(b):
            n = sum(state["prev"] < t <= b["t"] for t in (1, 2, 3))
            state["prev"] = b["t"]
            state["updates"] += n
            state["active"] = state["active"] or bool(n)
            return {"notified": n}

        def closed_window(_):
            self.assertFalse(state["active"], "processing must finish before closing the oracle/evaluation window")
            return {"ok": True}

        edge = FakeServer({("POST", "/v1/clock/advance"): advance,
                           ("GET", "/v1/metrics"): lambda b: {"mcp": {"updates": state["updates"]}}})
        sim = FakeServer({("POST", "/control/clock"): sim_clock, ("POST", "/control/finish"): closed_window})
        mock = FakeServer({("POST", "/control/reset"): lambda b: {}, ("POST", "/control/clock"): lambda b: {},
                           ("POST", "/control/expect"): lambda b: {}, ("POST", "/control/expire"): closed_window})
        with tempfile.TemporaryDirectory() as d, edge, sim, mock:
            day = write_day(Path(d) / "day.jsonl", header={"kind": "header", "window_s": [0, 3]},
                            events=[{"kind": "situation", "id": "s", "t": 1, "deadline_s": 1}])
            rp = Replayer(ReplayConfig(day=str(day), out_root=d, run_id="barrier", edgectx=edge.url,
                                       sim=sim.url, mock=mock.url, sync="updates", shutdown=False, max_step_s=1))
            try:
                rd = rp.run()
            finally:
                rp.close()
            self.assertEqual(state["flushed"], [2, 2, 3])  # prior step, deadline itself, final step
            self.assertTrue(json.loads((rd / "manifest.json").read_text())["completed"])

    def test_updates_timeout_aborts_without_losing_target_and_collects_manifest(self):
        finished = []
        edge = FakeServer({("POST", "/v1/clock/advance"): lambda b: {"idle": True},
                           ("GET", "/v1/metrics"): lambda b: {"mcp": {"updates": 0}}})
        sim = FakeServer({("POST", "/control/clock"): lambda b: {"notified": int(b["t"] == 1)},
                          ("POST", "/control/finish"): lambda b: finished.append(True) or {}})
        with tempfile.TemporaryDirectory() as d, edge, sim:
            day = write_day(Path(d) / "day.jsonl", header={"kind": "header", "window_s": [0, 1]}, events=[])
            rp = Replayer(ReplayConfig(day=str(day), out_root=d, run_id="timeout", edgectx=edge.url,
                                       sim=sim.url, sync="updates", sync_timeout_s=0.01, shutdown=False))
            try:
                with self.assertRaisesRegex(ReplayError, "synchronization timeout"):
                    rp.run()
            finally:
                rp.close()
            self.assertEqual(rp._updates_expected, 1)
            self.assertEqual(rp._pending_notified, 1)
            self.assertEqual(finished, [])
            manifest = json.loads((rp.run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "aborted")
            self.assertEqual(manifest["played_window_s"], [0, 1])

    def test_non_idle_advance_aborts_and_is_not_completed(self):
        edge = FakeServer({("POST", "/v1/clock/advance"): lambda b: {"idle": False}})
        with tempfile.TemporaryDirectory() as d, edge:
            day = write_day(Path(d) / "day.jsonl")
            rp = Replayer(ReplayConfig(day=str(day), out_root=d, run_id="busy", edgectx=edge.url,
                                       sync="none", shutdown=False))
            try:
                with self.assertRaisesRegex(ReplayError, "not idle"):
                    rp.run()
            finally:
                rp.close()
            self.assertFalse(json.loads((rp.run_dir / "manifest.json").read_text())["completed"])


if __name__ == "__main__":
    unittest.main()
