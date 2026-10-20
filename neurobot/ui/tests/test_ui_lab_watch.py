"""Live trace file observation: no HTTP paths, no fabricated partial records."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from neurobot.ui import lab


def lines(*rows):
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


START = {"type": "run_start", "run_id": "r1", "clock": "virtual", "ts_clock": 0}
INPUT = {"type": "input", "run_id": "r1", "id": "i1", "kind": "sensor", "ts_clock": 1,
         "device": "sensor", "property": "zero", "value": 0}
END = {"type": "run_end", "run_id": "r1", "ts_clock": 2}


class LiveTraceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "trace.jsonl"
        self.store = lab.LabStore()
        self.addCleanup(self.store.close)
        # Poll synchronously to test exact append boundaries without sleeps/races.
        with mock.patch.object(lab.threading, "Thread"):
            self.watcher = self.store.watch_trace(self.path, "Live EdgeCtx")

    def poll(self):
        self.store._poll_trace(self.watcher["id"])
        return self.store.snapshot()["watchers"][0]

    def test_created_later_partial_final_line_and_stable_revision(self):
        self.assertEqual(self.poll()["status"], "waiting")
        self.assertEqual(self.store.snapshot()["runs"], [])
        self.path.write_text(lines(START) + json.dumps(INPUT)[:20])
        first = self.poll()
        self.assertEqual(first["revision"], 1)
        run = self.store.run(first["run_id"])
        self.assertEqual(run["kind"], "live")
        self.assertEqual(run["status"], "live")
        self.assertEqual(len(run["events"]), 1)
        self.assertEqual(self.poll()["revision"], 1)
        with self.path.open("a") as stream:
            stream.write(json.dumps(INPUT)[20:] + "\n")
        second = self.poll()
        self.assertEqual(second["run_id"], first["run_id"])
        self.assertEqual(second["revision"], 2)
        self.assertEqual(self.store.run(second["run_id"])["observations"][0]["value"], 0)
        with self.path.open("a") as stream:
            stream.write(lines(END))
        third = self.poll()
        self.assertEqual(third["status"], "completed")
        self.assertEqual(third["revision"], 3)
        self.assertEqual(self.store.run(third["run_id"])["status"], "completed")
        self.assertEqual(self.store.export(third["run_id"]), self.path.read_bytes())

    def test_unfinished_only_is_waiting_but_complete_bad_line_is_error(self):
        self.path.write_text('{"type":')
        self.assertEqual(self.poll()["status"], "waiting")
        self.assertEqual(self.store.snapshot()["runs"], [])
        with self.path.open("a") as stream:
            stream.write("bad}\n")
        result = self.poll()
        self.assertEqual(result["status"], "error")
        self.assertIn("JSON", result["error"])
        self.assertEqual(self.store.snapshot()["runs"], [])

    def test_error_keeps_last_valid_data_and_is_not_silently_skipped(self):
        self.path.write_text(lines(START, INPUT))
        before = self.poll()
        original = self.store.export(before["run_id"])
        with self.path.open("a") as stream:
            stream.write("not json\n")
        failed = self.poll()
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["run_id"], before["run_id"])
        self.assertEqual(failed["revision"], before["revision"])
        self.assertEqual(self.store.export(before["run_id"]), original)
        self.assertTrue(self.store.run(before["run_id"])["stale"])

    def test_rotation_and_truncation_create_new_generation(self):
        self.path.write_text(lines(START, INPUT))
        first = self.poll()
        replacement = self.path.with_suffix(".new")
        replacement.write_text(lines({**START, "run_id": "r2"}))
        replacement.replace(self.path)
        second = self.poll()
        self.assertNotEqual(second["run_id"], first["run_id"])
        self.assertEqual(second["revision"], 1)
        self.assertEqual(self.store.run(first["run_id"])["status"], "partial")
        self.path.write_text(lines({**START, "run_id": "r3"}))
        third = self.poll()
        self.assertNotEqual(third["run_id"], second["run_id"])
        self.assertEqual(len(self.store.run(third["run_id"])["events"]), 1)
        self.assertEqual(len(self.store.snapshot()["runs"]), 3)

    def test_missing_rotated_file_waits_without_reusing_old_run(self):
        self.path.write_text(lines(START))
        first = self.poll()
        self.path.unlink()
        missing = self.poll()
        self.assertEqual(missing["status"], "waiting")
        self.assertIsNone(missing["run_id"])
        self.assertEqual(self.store.run(first["run_id"])["status"], "partial")

    def test_resource_caps_are_atomic(self):
        self.path.write_text(lines(START))
        first = self.poll()
        original = self.store.export(first["run_id"])
        with self.path.open("a") as stream:
            stream.write(lines(INPUT))
        with mock.patch.object(lab, "MAX_STORE_BYTES", self.store._sizes[first["run_id"]] + 1):
            failed = self.poll()
        self.assertEqual(failed["status"], "error")
        self.assertEqual(self.store.export(first["run_id"]), original)
        self.assertEqual(len(self.store.run(first["run_id"])["events"]), 1)
        with mock.patch.object(lab, "MAX_IMPORT_BYTES", 5):
            self.assertEqual(self.poll()["status"], "error")

    def test_event_cap_and_multiple_run_start_are_explicit(self):
        self.path.write_text(lines(START, INPUT, END))
        with mock.patch.object(lab, "MAX_EVENTS", 2):
            result = self.poll()
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.store.snapshot()["runs"], [])
        self.path.write_text(lines(START, START))
        result = self.poll()
        self.assertEqual(result["status"], "error")
        self.assertIn("run_start", result["error"])

    def test_watcher_bound_regular_file_and_no_http_paths(self):
        with mock.patch.object(lab, "MAX_WATCHERS", 1):
            with self.assertRaises(lab.LabError):
                self.store.watch_trace(self.path.with_name("other.jsonl"))
        self.path.mkdir()
        self.assertEqual(self.poll()["status"], "error")
        self.assertEqual(lab.mutate(self.store, "/api/lab/import", {"path": str(self.path)})[0], 400)
        self.assertNotIn("path", self.store.snapshot()["watchers"][0])

    def test_actual_background_poll_and_close(self):
        live = lab.LabStore()
        self.addCleanup(live.close)
        watched = live.watch_trace(self.path)
        self.path.write_text(lines(START, INPUT))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = live.snapshot()["watchers"][0]
            if current["revision"]:
                break
            time.sleep(0.02)
        self.assertEqual(current["status"], "watching")
        self.assertEqual(live.run(current["run_id"])["observations"][0]["value"], 0)
        thread = live._watchers[watched["id"]]["thread"]
        live.close()
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
