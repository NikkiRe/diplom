"""Content import, provenance, bounds and actual local EdgeCtx experiment contracts."""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from neurobot.ui import lab


TRACE = [
    {"type": "run_start", "run_id": "r", "ts_clock": 0, "clock": "virtual", "policy": {"wake": "gate"}},
    {"type": "input", "id": "s1", "run_id": "r", "ts_clock": 1, "kind": "sensor",
     "device": "thermometer", "property": "temperature", "value": 33.2, "vendor": {"unknown": [False, None]}},
    {"type": "wake", "wake_id": "w1", "ts_clock": 3, "reason": "gate", "input_ids": ["s1"]},
    {"type": "llm_request", "req_id": "q1", "wake_id": "w1", "ts_clock": 3,
     "segments": {"state": 30}, "prompt_tokens_est": 100},
    {"type": "llm_response", "req_id": "q1", "ts_clock": 3, "has_usage": True, "latency_ms": 4,
     "usage": {"prompt_tokens": 120, "completion_tokens": 10, "cached_tokens": 20, "total_tokens": 130}},
    {"type": "tool_call", "call_id": "c1", "wake_id": "w1", "ts_clock": 3,
     "name": "notify_user", "args": {"text": "Перегрев"}},
    {"type": "tool_result", "call_id": "c1", "ts_clock": 4, "ok": True},
    {"type": "run_end", "ts_clock": 5},
]


class LabImportTest(unittest.TestCase):
    def setUp(self):
        self.store = lab.LabStore(allow_experiments=False)
        self.addCleanup(self.store.close)

    def add(self, events=None, **kwargs):
        summary = self.store.add_trace(events=TRACE if events is None else events, **kwargs)
        return self.store.run(summary["id"])

    def test_explicit_links_and_original_ids(self):
        run = self.add()
        events = run["events"]
        self.assertEqual(events[1]["id"], "e000002")
        self.assertEqual(events[1]["record_id"], "s1")
        self.assertEqual(events[1]["input_id"], "s1")
        self.assertEqual(events[1]["vendor"], {"unknown": [False, None]})
        self.assertEqual(events[2]["links"], [{"id": "e000002", "relation": "input"}])
        self.assertEqual(events[3]["links"], [{"id": "e000003", "relation": "wake"}])
        self.assertEqual(events[4]["links"], [{"id": "e000004", "relation": "request"}])
        self.assertEqual(events[6]["links"], [{"id": "e000006", "relation": "call"}])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["clock"], "virtual")

    def test_no_temporal_causal_link_or_invented_prompt(self):
        run = self.add()
        self.assertEqual(run["events"][1]["links"], [])
        self.assertFalse(run["contexts"][0]["prompt"]["available"])
        self.assertEqual(run["contexts"][0]["observed_through"], 1)
        self.assertEqual(run["observations"][0]["origin"], "agent_input")
        self.assertEqual(run["observations"][0]["value"], 33.2)
        self.assertNotIn("current_state", run)

    def test_recorded_context_is_available_without_reconstruction(self):
        events = json.loads(json.dumps(TRACE))
        events[3]["context_snapshot"] = {"version": 1, "complete": True,
                                          "messages": [{"role": "user", "content": "recorded value"}],
                                          "tools": [], "sha256": "recorded-digest", "bytes": 57}
        run = self.add(events)
        self.assertTrue(run["contexts"][0]["prompt"]["available"])
        self.assertEqual(run["contexts"][0]["prompt"]["snapshot_event_id"], run["events"][3]["id"])
        self.assertEqual(run["events"][3]["context_snapshot"], events[3]["context_snapshot"])
        events[3]["context_snapshot"]["complete"] = False
        self.assertFalse(self.add(events)["contexts"][0]["prompt"]["available"])

    def test_observations_are_not_repeated_per_request(self):
        events = TRACE[:4] + [{"type": "llm_request", "req_id": "q2", "wake_id": "w1", "ts_clock": 4}]
        run = self.add(events)
        self.assertEqual(len(run["observations"]), 1)
        self.assertEqual([c["observed_through"] for c in run["contexts"]], [1, 1])
        self.assertNotIn("observed", run["contexts"][0])

    def test_partial_unknown_times_stay_unknown(self):
        run = self.add([{"type": "custom", "data": {"temperature": None}}])
        self.assertEqual(run["status"], "partial")
        self.assertIsNone(run["t_start"])
        self.assertIsNone(run["events"][0]["t"])
        self.assertFalse(run["clock_known"])
        self.assertIsNone(run["metrics"]["prompt_tokens"])
        self.assertIsNone(run["metrics"]["cost_rub"])

    def test_real_and_virtual_clock_domains_never_mix(self):
        wall = 1792520000.0
        real = self.add([{"type": "run_start", "clock": "real", "ts_clock": 38000, "ts_wall": wall},
                         {"type": "input", "ts_clock": 38001, "ts_wall": wall + 1},
                         {"type": "custom", "ts_clock": 38002}])
        self.assertEqual(real["events"][0]["t"], wall)
        self.assertEqual(real["events"][1]["t"], wall + 1)
        self.assertIsNone(real["events"][2]["t"])
        self.assertEqual(real["t_start"], wall)
        virtual = self.add([{"type": "run_start", "clock": "virtual", "ts_clock": 0, "ts_wall": wall},
                            {"type": "custom", "ts_wall": wall + 1},
                            {"type": "custom", "t": 3, "ts_wall": wall + 2}])
        self.assertEqual(virtual["events"][0]["t"], 0)
        self.assertIsNone(virtual["events"][1]["t"])
        self.assertEqual(virtual["events"][2]["t"], 3)
        unknown = self.add([{"type": "custom", "ts_clock": 5}, {"type": "custom", "t": 9, "ts_wall": wall}])
        self.assertFalse(unknown["clock_known"])
        self.assertIsNone(unknown["events"][0]["t"])
        self.assertEqual(unknown["events"][1]["t"], 9)

    def test_reported_tokens_and_unknown_billing(self):
        run = self.add()
        self.assertEqual(run["metrics"]["prompt_tokens"], 120)
        self.assertEqual(run["metrics"]["total_tokens"], 130)
        self.assertIsNone(run["metrics"]["reasoning_tokens"])
        self.assertTrue(run["metrics"]["usage_complete"])
        self.assertEqual(run["metrics"]["by_source"]["gate"]["calls"], 1)
        events = TRACE + [{"type": "llm_request", "req_id": "q2"},
                          {"type": "llm_response", "req_id": "q2", "error": "timeout", "maybe_billed": True,
                           "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}}]
        metrics = self.add(events)["metrics"]
        self.assertEqual(metrics["prompt_tokens"], 120)  # known partial usage only
        self.assertFalse(metrics["usage_complete"])
        self.assertEqual(metrics["maybe_billed_calls"], 1)

    def test_duplicate_or_future_id_never_creates_link(self):
        events = [{"type": "wake", "wake_id": "w", "input_ids": ["future"]},
                  {"type": "input", "id": "future"}, {"type": "input", "id": "duplicate"},
                  {"type": "input", "id": "duplicate"},
                  {"type": "suppress", "input_id": "duplicate", "reason": "ignore"}]
        run = self.add(events)
        self.assertEqual(run["events"][0]["links"], [])
        self.assertEqual(run["events"][-1]["links"], [])
        self.assertGreater(run["provenance"]["warning_count"], 0)

    def test_jsonl_hash_and_export_are_exact(self):
        content = ' {"type":"input", "id":"original", "text":"Юникод", "new":{"x":false}}\n\n'
        summary = self.store.add_trace(content=content, name="Trace")
        run = self.store.run(summary["id"])
        self.assertEqual(run["provenance"]["sha256"], hashlib.sha256(content.encode()).hexdigest())
        self.assertEqual(self.store.export(summary["id"]), content.encode())
        events_export = self.store.export(self.add()["id"])
        self.assertEqual([json.loads(line) for line in events_export.splitlines()], TRACE)
        self.assertIsNone(self.store.export("missing"))

    def test_copy_isolation(self):
        run = self.add()
        run["events"][1]["value"] = 999
        self.assertEqual(self.store.run(run["id"])["events"][1]["value"], 33.2)
        snapshot = self.store.snapshot()
        snapshot["runs"][0]["metrics"]["llm_calls"] = 999
        self.assertEqual(self.store.snapshot()["runs"][0]["metrics"]["llm_calls"], 1)

    def test_rejects_bad_or_ambiguous_import_atomically(self):
        bodies = [
            {}, {"events": []}, {"content": "not-json"}, {"content": "[]"},
            {"events": [{"type": "input"}], "content": "{}"},
            {"events": [{"type": "input", "run_id": "a"}, {"type": "input", "run_id": "b"}]},
            {"events": [{"type": "wake", "wake_id": []}]},
            {"events": [{"type": "wake", "input_ids": [1]}]},
            {"events": [{"type": "input", "value": math.nan}]},
            {"content": '{"type":"input","value":1e999}'},
            {"content": '{"type":"input","value":' + '9' * 2000 + '}'},
            {"path": "/etc/passwd"}, {"events": [{"type": "input"}], "name": ""},
        ]
        for body in bodies:
            status, doc = lab.mutate(self.store, "/api/lab/import", body)
            self.assertGreaterEqual(status, 400, body)
            self.assertIn("error", doc)
        self.assertEqual(self.store.snapshot()["runs"], [])

    def test_explicit_limits_no_truncation(self):
        with mock.patch.object(lab, "MAX_EVENTS", 2):
            status, doc = lab.mutate(self.store, "/api/lab/import", {"events": TRACE[:3]})
            self.assertEqual(status, 413)
        with mock.patch.object(lab, "MAX_IMPORT_BYTES", 20):
            self.assertEqual(lab.mutate(self.store, "/api/lab/import", {"content": '{"type":"custom","x":1}'})[0], 413)
        with mock.patch.object(lab, "MAX_DEPTH", 2):
            self.assertEqual(lab.mutate(self.store, "/api/lab/import", {"events": [{"type": "custom", "nested": {"a": {"b": 1}}}]})[0], 413)
        with mock.patch.object(lab, "MAX_RUNS", 1):
            self.add()
            self.assertEqual(lab.mutate(self.store, "/api/lab/import", {"events": TRACE})[0], 413)
        self.assertEqual(len(self.store.snapshot()["runs"]), 1)

    def test_store_limit_includes_original_export_bytes(self):
        rows, prov = lab.parse_trace(events=TRACE)
        normalized = lab.normalize(rows, "test", prov)
        normalized_bytes = len(lab._dump(normalized))
        with mock.patch.object(lab, "MAX_STORE_BYTES", normalized_bytes + 1):
            self.assertEqual(lab.mutate(self.store, "/api/lab/import", {"name": "test", "events": TRACE})[0], 413)
        self.assertEqual(self.store.snapshot()["runs"], [])

    def test_synthetic_demo_explicit_and_opt_in(self):
        self.assertEqual(self.store.snapshot()["runs"], [])
        demo = lab.LabStore(demo=True)
        self.addCleanup(demo.close)
        summary = demo.snapshot()["runs"][0]
        self.assertIn("SYNTHETIC", summary["origin"])
        self.assertEqual(summary["kind"], "trace")

    def test_disabled_experiments_and_unknown_routes(self):
        self.assertEqual(lab.mutate(self.store, "/api/lab/experiments", {})[0], 403)
        self.assertEqual(lab.mutate(self.store, "/api/lab/import", [])[0], 400)
        self.assertEqual(lab.mutate(self.store, "/api/lab/unknown", {})[0], 404)
        self.assertEqual(lab.mutate(self.store, "/api/lab/cancel", {"id": "absent"})[0], 404)
        self.assertEqual(lab.mutate(self.store, "/api/lab/cancel", {"id": "x", "path": "/tmp"})[0], 400)


class LabJobsTest(unittest.TestCase):
    def test_validation_rejects_remote_configuration_and_unbounded_work(self):
        for body in ({"base_url": "https://example.com"}, {"duration_s": 601}, {"heartbeat_s": 0},
                     {"threshold": "30"}, {"debounce_s": True}, {"seed": -1}, {"policy": "other"},
                     {"scenario": "unknown"}, {"scenario": {}}, {"threshold": float("inf")}):
            with self.assertRaises(lab.LabError):
                lab.experiment_spec(body)

    def test_history_and_integer_window_config(self):
        from neurobot.ui.lab_runner import make_config, make_day
        for window in (True, False, 0, 33, 2.0, "8", None):
            with self.assertRaises(lab.LabError):
                lab.experiment_spec({"window_turns": window})
        with self.assertRaises(lab.LabError):
            lab.experiment_spec({"history": "unknown"})
        store = lab.LabStore()
        self.addCleanup(store.close)
        default = lab.experiment_spec({})
        self.assertEqual((default["history"], default["window_turns"]), ("mask", 8))
        for history in ("full", "window", "mask", "summary_system"):
            spec = lab.experiment_spec({"history": history, "window_turns": 1})
            config = make_config(spec, store.work_dir, 19000, 19001, 19002)
            self.assertEqual(config["policy"]["history"], history)
            self.assertEqual(config["policy"]["window_turns"], 1)
            self.assertEqual(config["policy"]["history_batch_turns"], 1)
            self.assertEqual(config["policy"]["mask_keep_turns"], 1)
            self.assertEqual(make_day(spec), make_day(default))

    def test_queued_cancel_and_concurrency(self):
        store = lab.LabStore()
        self.addCleanup(store.close)
        with mock.patch.object(lab.threading, "Thread") as thread:
            thread.return_value.is_alive.return_value = False
            job = store.start({})
            self.assertEqual(job["status"], "queued")
            self.assertEqual(lab.mutate(store, "/api/lab/experiments", {})[0], 409)
            self.assertEqual(store.cancel(job["id"])["status"], "cancelling")
            with mock.patch.object(lab.subprocess, "Popen") as popen:
                store._execute(job["id"])
                popen.assert_not_called()
            self.assertEqual(store.snapshot()["jobs"][0]["status"], "cancelled")
            self.assertEqual(store.snapshot()["runs"], [])

    def test_missing_interpreter_is_explicit_failed_job(self):
        store = lab.LabStore(python="/nonexistent/edgelab-python")
        self.addCleanup(store.close)
        store.start({})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = store.snapshot()["jobs"][0]
            if job["status"] == "failed":
                break
            time.sleep(0.01)
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["error"])
        self.assertEqual(store.snapshot()["runs"], [])

    def test_closed_store_rejects_import_and_jobs(self):
        store = lab.LabStore()
        store.close()
        self.assertEqual(lab.mutate(store, "/api/lab/import", {"events": TRACE})[0], 503)
        self.assertEqual(lab.mutate(store, "/api/lab/experiments", {})[0], 503)

    def test_seeded_stimulus_same_across_policies(self):
        from neurobot.ui.lab_runner import make_day
        first = make_day(lab.experiment_spec({"policy": "gate"}))
        second = make_day(lab.experiment_spec({"policy": "heartbeat"}))
        self.assertEqual(first, second)
        self.assertNotEqual(first, make_day(lab.experiment_spec({"seed": 9})))

    @unittest.skipUnless(os.name == "posix", "process group cancellation is POSIX-specific")
    def test_running_cancel_and_shutdown_reap_owned_process_group(self):
        store = lab.LabStore()
        self.addCleanup(store.close)
        real_popen = subprocess.Popen
        owned = []
        script = ("import subprocess,sys,time; child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
                  "print(child.pid,flush=True); time.sleep(60)")
        def launch(_args, **kwargs):
            process = real_popen([sys.executable, "-u", "-c", script], **kwargs)
            owned.append(process)
            return process
        with mock.patch.object(lab.subprocess, "Popen", side_effect=launch):
            job = store.start({})
            log = store.work_dir / job["id"] / "runner.log"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if log.exists() and log.read_text().strip():
                    break
                time.sleep(0.01)
            self.assertTrue(log.exists())
            child_pid = int(log.read_text().strip())
            store.cancel(job["id"])
            store.close()
        self.assertIsNotNone(owned[0].poll())
        self.assertEqual(store.snapshot()["jobs"][0]["status"], "cancelled")
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            self.skipTest("sandbox forbids inspecting child process state")
        # On Linux a reparented zombie can briefly exist until PID 1 reaps it;
        # it is not a running leaked simulator. Check state rather than kill(pid,0).
        try:
            check = subprocess.run(["ps", "-p", str(child_pid), "-o", "stat="], capture_output=True, text=True)
        except PermissionError:
            self.skipTest("sandbox forbids ps for child process state")
        self.assertTrue(check.returncode != 0 or not check.stdout.strip() or check.stdout.strip().startswith("Z"), check.stdout)


@unittest.skipUnless(os.environ.get("EDGELAB_TEST_BIN"), "set EDGELAB_TEST_BIN for actual Go+sim-mcp integration")
class ActualEdgeLabTest(unittest.TestCase):
    def setUp(self):
        self.store = lab.LabStore(binary=os.environ["EDGELAB_TEST_BIN"], python=os.environ.get("EDGELAB_PYTHON"))
        self.addCleanup(self.store.close)

    def execute(self, **spec):
        job = self.store.start(spec)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            current = next(j for j in self.store.snapshot()["jobs"] if j["id"] == job["id"])
            if current["status"] in ("completed", "failed", "cancelled"):
                self.assertEqual(current["status"], "completed", current)
                return self.store.run(current["run_id"])
            time.sleep(0.05)
        self.fail("local experiment timed out")

    def test_real_paired_policy_experiment_and_context_evidence(self):
        gate = self.execute(scenario="noise-burst", policy="gate")
        heartbeat = self.execute(scenario="noise-burst", policy="heartbeat")
        self.assertEqual(gate["provenance"]["day_sha256"], heartbeat["provenance"]["day_sha256"])
        self.assertEqual(gate["provenance"]["execution"], "actual_go_agent_local_oracle")
        self.assertFalse(gate["provenance"]["external_llm"])
        self.assertEqual(gate["provenance"]["real_money_spent"], 0)
        self.assertLess(gate["metrics"]["llm_calls"], heartbeat["metrics"]["llm_calls"])
        self.assertEqual(gate["evaluation"]["situations"]["reacted_in_time"], 1)
        self.assertEqual(heartbeat["evaluation"]["situations"]["reacted_in_time"], 0)
        self.assertTrue(all(c["prompt"]["available"] for c in gate["contexts"]))
        self.assertEqual(gate["scenario_truth"]["header"]["initial_state"]["thermo_lab"]["temperature"], 22)
        for run in (gate, heartbeat):
            records = [json.loads(line) for line in self.store.export(run["id"]).splitlines()]
            self.assertEqual(records[0]["type"], "run_start")
            self.assertEqual(records[-1]["type"], "run_end")
            self.assertEqual(len(run["provenance"]["agent_sha256"]), 64)
            self.assertIsNone(run["metrics"]["cost_rub"])
            self.assertEqual(run["metrics"]["tokens_basis"], "mock_usage_estimate")
            resource = run["resources"]
            self.assertEqual(resource["target"], "agent_pid_only")
            self.assertTrue(resource["diagnostic_only"])
            self.assertEqual(resource["host"]["system"], __import__("platform").system())
            if resource["samples"]:
                self.assertGreater(resource["rss_sampled_max_bytes"], 0)
                self.assertTrue(all(s["cpu_s"] >= 0 for s in resource["samples"]))
            with self.assertRaises(ProcessLookupError):
                os.kill(resource["pid"], 0)

    def test_virtual_actuator_is_confirmed_only_by_actual_tool_result(self):
        run = self.execute(scenario="leak-recovery", policy="gate")
        writes = [e for e in run["events"] if e["type"] == "tool_call" and e.get("args", {}).get("device") == "valve_water"]
        self.assertTrue(writes)
        call_ids = {event["call_id"] for event in writes}
        results = [e for e in run["events"] if e["type"] == "tool_result" and e.get("call_id") in call_ids]
        self.assertTrue(all(e["ok"] for e in results))
        self.assertEqual(run["evaluation"]["situations"]["reacted_in_time"], 1)
        self.assertFalse(run["provenance"]["physical_equipment"])
        self.assertFalse(run["scenario_truth"]["final_state"]["state"]["valve_water"]["open"])

    def test_actual_threshold_changes_quality_and_overheat_executes(self):
        high_threshold = self.execute(scenario="noise-burst", policy="gate", threshold=40)
        self.assertEqual(high_threshold["evaluation"]["situations"]["reacted_in_time"], 0)
        self.assertEqual(high_threshold["metrics"]["llm_calls"], 2)  # only the user's question
        overheat = self.execute(scenario="overheat", policy="hybrid")
        self.assertEqual(overheat["evaluation"]["situations"]["reacted_in_time"], 1)
        self.assertTrue(any(e["type"] == "tool_call" and e.get("args", {}).get("device") == "boiler"
                            for e in overheat["events"]))


if __name__ == "__main__":
    unittest.main()
