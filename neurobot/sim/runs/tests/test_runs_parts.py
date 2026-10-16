"""Части прогона без ROS: заглушка воркера 8766, узел отказов камеры (решение по кадру), журнал probe, исходы
инструментов по сессиям PicoClaw, конфиги PicoClaw A0/A1, правки env-файла A1, обёртка балласта, демо A0, выбор
жеста start_test."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from neurobot.sim.gestures.clock import boot_id, mono  # noqa: E402
from neurobot.sim.runs import a0_demo, relay, runner, stacks, start_gesture, worker8766  # noqa: E402
from neurobot.sim.runs.budget_a1 import ballast_cmd  # noqa: E402

LAB_CFG = ROOT / stacks.LAB_RP / ".picoclaw" / "config.json"
TWIN_ENV = ROOT / "neurobot" / "sim" / "suites" / "twin.env"
SUITES_PC = ROOT / "neurobot" / "sim" / "suites" / "picoclaw.json"


class WorkerTest(unittest.TestCase):
    def test_answers_proxy_api_records_requests_and_does_not_move(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "w.jsonl")
            srv = ThreadingHTTPServer(("127.0.0.1", 0), worker8766.handler(worker8766.Journal(log)))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = "http://127.0.0.1:%d" % srv.server_address[1]
            with urllib.request.urlopen(base + "/state") as r:
                self.assertEqual(json.loads(r.read())["stub"], True)
            body = json.dumps({"direction": "forward", "duration_s": 300, "speed": 0.8}).encode()
            req = urllib.request.Request(base + "/motion/nudge", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as r:
                out = json.loads(r.read())
            self.assertEqual((out["moved"], out["received"]["duration_s"]), (False, 300))
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(base + "/nope")
            srv.shutdown()
            rows = [json.loads(line) for line in open(log)]
            self.assertEqual([r["path"] for r in rows], ["/state", "/motion/nudge", "/nope"])
            self.assertEqual(rows[1]["body"]["duration_s"], 300)


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.anchor = os.path.join(self.d.name, "anchor.json")
        self.log = []
        faults = [{"at_s": 10.0, "kind": "delay", "duration_s": 2.0}, {"at_s": 20.0, "kind": "freeze",
                                                                        "duration_s": 2.0},
                  {"at_s": 30.0, "kind": "black", "duration_s": 2.0}, {"at_s": 40.0, "kind": "slow",
                                                                       "duration_s": 5.0, "fps": 2.0}]
        self.r = relay.Relay(relay.Faults(faults, self.anchor), self.log.append)

    def tearDown(self):
        self.d.cleanup()

    def write_anchor(self, t_mono: float) -> None:
        t_wall = time.time() - (mono() - t_mono)
        with open(self.anchor, "w") as f:
            json.dump({"t_wall": t_wall, "t_mono": t_mono, "timeline_sha256": "x", "pid": 1, "boot_id": boot_id()}, f)

    def test_passes_frames_until_anchor_then_applies_schedule(self):
        black = lambda m: "black"  # noqa: E731
        self.assertEqual(self.r.on_frame("f0", 100.0, black), "f0")
        now = mono()
        self.write_anchor(now - 100.0)            # якорь: rel = m − (now − 100)
        m0 = now - 100.0
        self.assertEqual(self.r.on_frame("f1", m0 + 5.0, black), "f1")
        self.assertIsNone(self.r.on_frame("f2", m0 + 10.5, black))
        self.assertEqual(self.r.on_frame("f3", m0 + 12.5, black), "f3")
        self.assertEqual(self.r.on_frame("f4", m0 + 20.5, black), "f3")
        self.assertEqual(self.r.on_frame("f5", m0 + 30.5, black), "black")
        self.assertEqual(self.r.on_frame("f6", m0 + 40.1, black), "f6")
        self.assertIsNone(self.r.on_frame("f7", m0 + 40.2, black))
        self.assertEqual(self.r.on_frame("f8", m0 + 40.7, black), "f8")
        kinds = [(e["ev"], e["kind"]) for e in self.log]
        self.assertEqual(kinds[:4], [("fault_start", "delay"), ("fault_end", "delay"), ("fault_start", "freeze"),
                                     ("fault_end", "freeze")])


class ProbeTailTest(unittest.TestCase):
    def test_progress_after_moment_and_last_progress(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            for m, pid, calls in ((1, 10, 5), (2, 10, 15), (3, 10, 15), (4, 10, 15), (5, 11, 0), (6, 11, 9)):
                f.write(json.dumps({"m": m, "pid": pid, "det_calls": calls}) + "\n")
            f.write('{"m": 7, "pid"')                 # недописанная строка — дочитается потом
        try:
            p = runner.ProbeTail(f.name)
            p.poll()
            self.assertEqual(p.progress_since(0.5), 2)
            self.assertEqual(p.progress_since(2.5), 6)     # новый процесс, его вторая строка
            self.assertIsNone(p.progress_since(6.0))
            self.assertEqual(p.last_progress(), 6)
            with open(f.name, "a") as g:
                g.write(': 11, "det_calls": 20}\n')
            p.poll()
            self.assertEqual(p.last_progress(), 7)
        finally:
            os.remove(f.name)


class SessionsTest(unittest.TestCase):
    def test_tool_outcomes_of_one_turn(self):
        rows = [{"role": "user", "content": "старое"},
                {"role": "tool", "content": "{}", "tool_call_id": "a"},
                {"role": "user", "content": "[runs main a0 #1] Какое сейчас состояние робота?"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "mcp_x_s"}}]},
                {"role": "tool", "content": "MCP tool execution failed: connection closed: EOF",
                 "tool_call_id": "c1"},
                {"role": "assistant", "content": "Готово."}]
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "sk_v1_x.jsonl"), "w") as f:
                f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
            got = runner.tool_outcomes(d, "[runs main a0 #1]")
        self.assertEqual([(g["name"], g["ok"]) for g in got], [("mcp_x_s", False)])

    def test_tool_ok(self):
        self.assertTrue(runner.tool_ok('{"ok": true, "stub": true}'))
        self.assertFalse(runner.tool_ok('{"ok": false, "error": "worker is not available"}'))
        self.assertFalse(runner.tool_ok('{"error": "x"}'))
        self.assertTrue(runner.tool_ok('{"connected": true, "rosbridge": "ok"}'))
        self.assertFalse(runner.tool_ok("Error executing tool robot_nudge: [Errno 111] Connection refused"))


class ConfigTest(unittest.TestCase):
    def test_lab_picoclaw_changes_only_llm_vk_paths_and_keeps_their_defaults(self):
        lab = json.loads(LAB_CFG.read_text(encoding="utf-8"))
        cfg = stacks.lab_picoclaw(lab, home="/h", api_base="http://m/v1", proxy_python="/py", proxy="/p.py")
        name = cfg["agents"]["defaults"]["model_name"]
        model = [m for m in cfg["model_list"] if m["model_name"] == name][0]
        self.assertEqual(model["api_base"], "http://m/v1")
        self.assertFalse(cfg["channel_list"]["vk"]["enabled"])
        self.assertTrue(cfg["channel_list"]["pico"]["enabled"])
        srv = cfg["tools"]["mcp"]["servers"]["robot_vision_demo_mcp"]
        self.assertEqual((srv["command"], srv["args"], srv["type"]), ("/py", ["/p.py"], "stdio"))
        self.assertEqual(cfg["gateway"]["log_level"], "warn")
        self.assertEqual(cfg["agents"]["defaults"]["max_tool_iterations"], 50)
        self.assertEqual(cfg["agents"]["defaults"]["workspace"], "/h/workspace")
        self.assertEqual(lab["channel_list"]["vk"]["enabled"], True)          # исходник не тронут

    def test_a1_picoclaw_gets_pico_channel_and_mockllm(self):
        cfg = stacks.pico_config(json.loads(SUITES_PC.read_text()), port=18793, api_base="http://m/v1")
        self.assertEqual(cfg["channel_list"]["pico"], {"type": "pico", "enabled": True})
        self.assertEqual(cfg["model_list"][0]["api_base"], "http://m/v1")
        self.assertEqual(cfg["gateway"]["port"], 18793)

    def test_security_yml(self):
        text = stacks.security_yml("abc", "cloudrugpt")
        self.assertIn("pico:\n    settings:\n      token: abc\n", text)
        self.assertIn("cloudrugpt:0:\n    api_keys:\n      - mock-not-a-key", text)
        self.assertNotIn("model_list", stacks.security_yml("abc"))

    def test_env_edit_is_idempotent_and_touches_only_run_settings(self):
        text = TWIN_ENV.read_text(encoding="utf-8")
        once = stacks.A1.env_edit(text, "/w/hall.sdf", "/g/probe.jsonl")
        self.assertEqual(stacks.A1.env_edit(once, "/w/hall.sdf", "/g/probe.jsonl"), once)
        get = lambda t, k: [l for l in t.splitlines() if l.startswith(k + "=")][0]  # noqa: E731
        self.assertIn("world:=/w/hall.sdf color_hz:=10\"", get(once, "NEUROBOT_DRIVER_twin_CMD"))
        self.assertIn("--ros-args -r /camera/color/image_raw:=%s -r /cmd_vel:=/cmd_vel_gesture"
                      % stacks.CAMERA_RELAY_TOPIC, get(once, "NEUROBOT_PERCEPTION_CMD"))
        self.assertNotIn("robot_vision_mcp_proxy.py", get(once, "NEUROBOT_FOREIGN_MCP"))
        self.assertEqual(get(once, "NEUROBOT_FAKES_PROBE"), "NEUROBOT_FAKES_PROBE=/g/probe.jsonl")
        changed = {l.split("=", 1)[0] for l in set(once.splitlines()) ^ set(text.splitlines())}
        self.assertEqual(changed, {"NEUROBOT_DRIVER_twin_CMD", "NEUROBOT_PERCEPTION_CMD", "NEUROBOT_FOREIGN_MCP",
                                   "NEUROBOT_FAKES_PROBE"})

    def test_ballast_wraps_perception_once(self):
        cmd = "/v/bin/python /s/predict_live.py --camera ros --ros-args -r /cmd_vel:=/cmd_vel_gesture"
        out = ballast_cmd(cmd, 1024)
        self.assertEqual(out, "/v/bin/python -m neurobot.sim.budget.ballast --mib 1024 --run /s/predict_live.py -- "
                              "--camera ros --ros-args -r /cmd_vel:=/cmd_vel_gesture")
        self.assertEqual(ballast_cmd(out, 1024), out)


class DemoTest(unittest.TestCase):
    def test_patch_torch_hides_compile_forces_weights_only_and_fakes_hub(self):
        calls = []
        fake = types.SimpleNamespace(load=lambda *a, **k: calls.append(k) or "state", compile=lambda m: m,
                                     hub=types.SimpleNamespace(load=None))
        a0_demo.patch_torch(fake)
        self.assertFalse(hasattr(fake, "compile"))
        self.assertEqual(fake.load("w.pth", map_location="cpu"), "state")
        self.assertEqual(calls, [{"map_location": "cpu", "weights_only": True}])
        self.assertIs(fake.hub.load, a0_demo.hub_load)


class StartGestureTest(unittest.TestCase):
    def test_pick_next_gesture_ahead(self):
        segs = [{"seg": 0, "kind": "gesture", "t_start": 4.0}, {"seg": 1, "kind": "negative", "t_start": 9.0},
                {"seg": 2, "kind": "gesture", "t_start": 12.0}]
        self.assertEqual(start_gesture.pick(segs, 3.0)["seg"], 0)
        self.assertEqual(start_gesture.pick(segs, 3.9)["seg"], 2)
        self.assertIsNone(start_gesture.pick(segs, 12.0))


if __name__ == "__main__":
    unittest.main()
