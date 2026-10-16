"""Подставной pyrealsense2: имена из predict_live.py (по AST их файла), кадры и темп, отказы delay / stall /
gone / freeze / black / slow и их лечение (cure) по команде и по расписанию от якоря; рестарт процесса не
повторяет старые команды, а расписание в прошлом молчит; ошибки обвязки (неверные команды, усечённый файл
команд, журнал probe без каталога, расписание без якоря, якорь чужой загрузки) не выходят из
wait_for_frames/start как отказ камеры; связка с FakeYolo и gesturegen."""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from neurobot.sim.gestures import realsense as rs  # noqa: E402
from neurobot.sim.gestures.clock import boot_id, decode_marker, load_or_create_anchor, mono  # noqa: E402
from neurobot.sim.gestures.fakeyolo import FakeYolo  # noqa: E402
from neurobot.sim.gestures.gesturegen import GenConfig, write_outputs  # noqa: E402
from neurobot.sim.gestures.probe import Probe  # noqa: E402
from neurobot.sim.gestures.timeline import Timeline  # noqa: E402

PREDICT_LIVE = ROOT / "third_party/neurochip/demo/demo-ros2_feature/src/camera/predict_live.py"
SHIM = ROOT / "neurobot/sim/gestures/shim"


class Base(unittest.TestCase):
    fps = 50.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.control = os.path.join(self.tmp.name, "control.jsonl")
        self.probe_path = os.path.join(self.tmp.name, "probe.jsonl")
        self.probe = Probe(self.probe_path)
        self.configure()

    def configure(self, **kw):
        kw.setdefault("fps", self.fps)
        kw.setdefault("reset_s", 0.3)
        kw.setdefault("control_path", self.control)
        rs.configure(probe=self.probe, **kw)

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, **fault):
        with open(self.control, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(fault) + "\n")

    def started(self):
        pipe = rs.pipeline()
        cfg = rs.config()
        cfg.enable_stream(rs.stream.color, 640, 480, rs.format.bgr8, 30)
        pipe.start(cfg)
        return pipe

    def events(self) -> list:
        with open(self.probe_path, encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if '"ev"' in ln]


@unittest.skipUnless(PREDICT_LIVE.exists(), "нет кода лаборатории")
class DemoContractTest(Base):
    def test_names_used_by_demo_exist(self):
        tree = ast.parse(PREDICT_LIVE.read_text(encoding="utf-8"))
        rs_names, methods = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name) and node.value.id == "rs":
                    rs_names.add(node.attr)
                elif isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Name) \
                        and node.value.value.id == "rs":
                    self.assertTrue(hasattr(getattr(rs, node.value.attr), node.attr), (node.value.attr, node.attr))
                else:
                    methods.add(node.attr)
        self.assertEqual(rs_names, {"pipeline", "config", "stream", "format", "camera_info", "option"})
        for name in rs_names:
            self.assertTrue(hasattr(rs, name), name)
        used = {"enable_stream", "start", "get_active_profile", "get_device", "sensors", "get_info", "set_option",
                "wait_for_frames", "get_color_frame", "get_data", "stop"}
        self.assertTrue(used <= methods, used - methods)

    def test_demo_sequence(self):
        """predict_live.py:546–549, :583–585, :108–129, :615–617, :673–677."""
        pipe = rs.pipeline()
        cfg = rs.config()
        cfg.enable_stream(rs.stream.color, 640, 480, rs.format.bgr8, 30)
        pipe.start(cfg)
        dev = pipe.get_active_profile().get_device()
        color = [s for s in dev.sensors if s.get_info(rs.camera_info.name).lower().find("rgb") != -1]
        self.assertEqual(len(color), 1)
        for opt, val in ((rs.option.enable_auto_exposure, 0), (rs.option.enable_auto_white_balance, 0),
                         (rs.option.exposure, 200.0), (rs.option.gain, 16.0), (rs.option.power_line_frequency, 2)):
            color[0].set_option(opt, val)
        self.assertEqual(color[0].get_option(rs.option.exposure), 200.0)
        frame = pipe.wait_for_frames()
        img = np.asanyarray(frame.get_color_frame().get_data())
        self.assertEqual((img.shape, img.dtype), ((480, 640, 3), np.uint8))
        self.assertAlmostEqual(decode_marker(img[:, ::-1]), mono(), delta=0.1)
        pipe.stop()

    def test_shim_shadows_package(self):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT), str(SHIM)]), PYTHONDONTWRITEBYTECODE="1")
        out = subprocess.run([sys.executable, "-c", "import pyrealsense2 as rs; print(rs.__file__, rs.pipeline)"],
                             env=env, capture_output=True, text=True, check=True).stdout
        self.assertIn(str(SHIM / "pyrealsense2"), out)


class FramesTest(Base):
    def test_rate_numbers_timestamps(self):
        pipe = self.started()
        t = time.monotonic()
        frames = [pipe.wait_for_frames().get_color_frame() for _ in range(15)]
        rate = 14 / (time.monotonic() - t)
        self.assertAlmostEqual(rate, self.fps, delta=0.2 * self.fps)
        nums = [f.get_frame_number() for f in frames]
        self.assertEqual(nums, list(range(nums[0], nums[0] + 15)))
        ts = [f.get_timestamp() for f in frames]
        self.assertTrue(all(b > a for a, b in zip(ts, ts[1:])))
        self.assertEqual((frames[0].get_width(), frames[0].get_height()), (640, 480))

    def test_slow_consumer_gets_latest_frame(self):
        pipe = self.started()
        pipe.wait_for_frames()
        time.sleep(0.2)
        f = pipe.wait_for_frames().get_color_frame()
        self.assertLess(mono() - f.get_timestamp() / 1000.0, 1.5 / self.fps)

    def test_state_errors(self):
        pipe = rs.pipeline()
        with self.assertRaisesRegex(RuntimeError, "before start"):
            pipe.wait_for_frames()
        with self.assertRaisesRegex(RuntimeError, "before start"):
            pipe.stop()
        with self.assertRaises(RuntimeError):
            pipe.get_active_profile()
        pipe.start()
        with self.assertRaisesRegex(RuntimeError, "before stop"):
            pipe.start()
        bad = rs.config()
        bad.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, 30)
        with self.assertRaisesRegex(RuntimeError, "Couldn't resolve requests"):
            rs.pipeline().start(bad)

    def test_context_lists_device(self):
        devs = rs.context().query_devices()
        self.assertEqual(len(devs), 1)
        self.assertEqual(devs[0].get_info(rs.camera_info.name), "Intel RealSense D435I")


class FaultTest(Base):
    def test_delay_raises_demo_error_then_recovers(self):
        pipe = self.started()
        pipe.wait_for_frames()
        self.command(kind="delay", duration_s=0.5)
        t = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, r"^Frame didn't arrive within 200$"):
            pipe.wait_for_frames(200)
        ok, frames = pipe.try_wait_for_frames(100)
        self.assertFalse(ok)
        self.assertFalse(frames)
        self.assertFalse(frames.get_color_frame())
        pipe.wait_for_frames(1000)
        self.assertGreaterEqual(time.monotonic() - t, 0.45)
        kinds = [(e["ev"], e.get("kind")) for e in self.events()]
        self.assertIn(("fault_start", "delay"), kinds)
        self.assertIn(("fault_end", "delay"), kinds)
        self.assertEqual(self.probe.get("timeouts"), 2)

    def test_stall_needs_pipeline_restart(self):
        pipe = self.started()
        pipe.wait_for_frames()
        self.command(kind="stall")
        with self.assertRaises(RuntimeError):
            pipe.wait_for_frames(150)
        pipe.stop()
        pipe.start()
        pipe.wait_for_frames(500)

    def test_gone_needs_hardware_reset(self):
        pipe = self.started()
        pipe.wait_for_frames()
        self.command(kind="gone")
        with self.assertRaises(RuntimeError):
            pipe.wait_for_frames(150)
        pipe.stop()
        pipe.start()
        with self.assertRaises(RuntimeError):
            pipe.wait_for_frames(150)
        pipe.get_active_profile().get_device().hardware_reset()
        self.assertEqual(rs.context().query_devices(), [])
        pipe.stop()
        with self.assertRaisesRegex(RuntimeError, "No device connected"):
            pipe.start()
        time.sleep(0.35)
        self.assertEqual(len(rs.context().query_devices()), 1)
        pipe.start()
        pipe.wait_for_frames(500)

    def test_reset_invalidates_running_pipeline(self):
        pipe = self.started()
        pipe.wait_for_frames()
        rs.context().query_devices()[0].hardware_reset()
        time.sleep(0.35)
        with self.assertRaises(RuntimeError):
            pipe.wait_for_frames(150)
        pipe.stop()
        pipe.start()
        pipe.wait_for_frames(500)

    def frozen(self, pipe, n: int = 3) -> set:
        return {(f.get_frame_number(), f.get_timestamp(), f.get_data().tobytes())
                for f in (pipe.wait_for_frames().get_color_frame() for _ in range(n))}

    def test_freeze_repeats_frame_across_pipeline_restart(self):
        pipe = self.started()
        pipe.wait_for_frames()
        self.command(kind="freeze", duration_s=0.5)
        first = self.frozen(pipe)
        self.assertEqual(len(first), 1)
        pipe.stop()
        other = self.started()                          # новый конвейер того же устройства — тот же кадр
        self.assertEqual(self.frozen(other), first)
        time.sleep(0.5)
        self.assertNotEqual(self.frozen(other, 1), first)

    def test_freeze_cured_by_restart_or_reset(self):
        pipe = self.started()
        pipe.wait_for_frames()
        self.command(kind="freeze", cure="restart")
        stuck = self.frozen(pipe)
        self.assertEqual(self.frozen(pipe), stuck)
        pipe.stop()
        pipe.start()
        self.assertNotEqual(self.frozen(pipe, 1), stuck)
        self.command(kind="freeze", cure="reset")
        stuck = self.frozen(pipe)
        pipe.stop()
        pipe.start()
        self.assertEqual(self.frozen(pipe), stuck)          # рестарт конвейера не лечит
        rs.context().query_devices()[0].hardware_reset()
        pipe.stop()
        time.sleep(0.35)
        pipe.start()
        self.assertNotEqual(self.frozen(pipe, 1), stuck)

    def test_black_frames_have_no_marker(self):
        pipe = self.started()
        self.command(kind="black", duration_s=0.3)
        img = pipe.wait_for_frames().get_color_frame().get_data()
        self.assertFalse(img.any())
        self.assertIsNone(decode_marker(img))
        time.sleep(0.3)
        self.assertIsNotNone(decode_marker(pipe.wait_for_frames().get_color_frame().get_data()))

    def test_slow_drops_rate(self):
        pipe = self.started()
        pipe.wait_for_frames()
        self.command(kind="slow", duration_s=1.0, fps=10)
        pipe.wait_for_frames()
        t = time.monotonic()
        for _ in range(3):
            pipe.wait_for_frames()
        self.assertAlmostEqual((time.monotonic() - t) / 3, 0.1, delta=0.03)

    def test_bad_commands_rejected_without_camera_errors(self):
        pipe = self.started()
        bad = ([1, 2], "delay", {"kind": "melt", "duration_s": 1}, {"kind": "delay"},
               {"kind": "delay", "duration_s": [1]}, {"kind": "slow", "duration_s": 1, "fps": {}},
               {"kind": "stall", "cure": "reset"}, {"kind": "black", "cure": "never"})
        with open(self.control, "a", encoding="utf-8") as fh:
            fh.writelines(json.dumps(b) + "\n" for b in bad)
            fh.write("{oops\n")
        with self.assertLogs("fake_pyrealsense2", "ERROR"):
            pipe.wait_for_frames(500)
        errors = [e for e in self.events() if e["ev"] == "control_error"]
        self.assertEqual(len(errors), len(bad) + 1)
        self.assertEqual(rs._state().faults, [])

    def test_truncated_control_file_is_reread(self):
        pipe = self.started()
        self.command(kind="black", duration_s=0.05)
        self.command(kind="black", duration_s=0.05)
        pipe.wait_for_frames()
        with open(self.control, "w", encoding="utf-8") as fh:      # «>» вместо «>>»
            fh.write(json.dumps({"kind": "delay", "duration_s": 5}) + "\n")
        with self.assertLogs("fake_pyrealsense2", "WARNING"):
            ok, _ = pipe.try_wait_for_frames(200)
        self.assertFalse(ok)

    def test_process_restart_does_not_repeat_commands(self):
        self.command(kind="delay", duration_s=0.3)
        pipe = self.started()
        ok, _ = pipe.try_wait_for_frames(100)
        self.assertFalse(ok)                                  # команда действует на прочитавший её процесс
        self.command(kind="delay", duration_s=5)
        with open(self.control, "a", encoding="utf-8") as fh:
            fh.write('{"kind": "black",')                    # строку пишут в момент старта нового процесса
        self.configure()                                      # рестарт процесса: новое устройство, тот же файл
        pipe = self.started()
        with open(self.control, "a", encoding="utf-8") as fh:
            fh.write(' "duration_s": 0.3}\n')
        self.assertFalse(pipe.wait_for_frames(200).get_color_frame().get_data().any())
        self.assertEqual([f.kind for f in rs._state().faults], ["black"])
        self.assertEqual([e["lines"] for e in self.events() if e["ev"] == "control_skipped"], [2])
        self.command(kind="delay", duration_s=0.3)
        ok, _ = pipe.try_wait_for_frames(100)
        self.assertFalse(ok)                                  # новые команды после рестарта действуют

    def test_probe_write_failure_is_not_a_camera_failure(self):
        self.probe = Probe(os.path.join(self.tmp.name, "nope", "probe.jsonl"))
        self.configure()
        with self.assertLogs("neurobot_fakes_probe", "ERROR") as logs:
            pipe = self.started()
            pipe.wait_for_frames()
            pipe.stop()
        self.assertEqual(len(logs.output), 1)
        self.assertIsNone(self.probe.path)

    def test_schedule_relative_to_anchor(self):
        anchor_path = os.path.join(self.tmp.name, "anchor.json")
        anchor = load_or_create_anchor(anchor_path, "x" * 64)
        at = anchor.rel_of(mono()) + 0.2
        self.configure(schedule=[{"at_s": at, "kind": "black", "duration_s": 0.3}], anchor_path=anchor_path)
        pipe = self.started()
        self.assertTrue(pipe.wait_for_frames().get_color_frame().get_data().any())
        time.sleep(0.25)
        self.assertFalse(pipe.wait_for_frames().get_color_frame().get_data().any())
        time.sleep(0.3)
        self.assertTrue(pipe.wait_for_frames().get_color_frame().get_data().any())

    def write_anchor(self, age_s: float, boot: str = None) -> str:
        path = os.path.join(self.tmp.name, "anchor.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"t_wall": time.time() - age_s + (0 if boot is None else 1000), "t_mono": mono() - age_s,
                       "timeline_sha256": "x" * 64, "pid": 1, "boot_id": boot_id() if boot is None else boot}, fh)
        return path

    def test_schedule_in_the_past_after_restart_is_silent(self):
        """Рестарт процесса после последнего отказа расписания законен; старый якорь ловит FakeYolo."""
        self.configure(schedule=[{"at_s": 1, "kind": "delay", "duration_s": 1}], anchor_path=self.write_anchor(100))
        with mock.patch.object(rs.log, "warning") as warning:
            self.started().wait_for_frames(100)
        warning.assert_not_called()
        self.assertEqual([e["ev"] for e in self.events()], ["pipeline_start"])

    def test_schedule_active_at_restart_still_applies(self):
        self.configure(schedule=[{"at_s": 1, "kind": "black", "duration_s": 100}], anchor_path=self.write_anchor(5))
        self.assertFalse(self.started().wait_for_frames(500).get_color_frame().get_data().any())

    def test_schedule_with_anchor_of_other_boot_is_not_applied(self):
        self.configure(schedule=[{"at_s": 0, "kind": "stall"}], anchor_path=self.write_anchor(0, boot="other"))
        with self.assertLogs("fake_pyrealsense2", "ERROR"):
            self.started().wait_for_frames(500)
        self.assertIn("anchor_error", [e["ev"] for e in self.events()])

    def test_schedule_requires_anchor(self):
        with self.assertRaisesRegex(ValueError, "якор"):
            self.configure(schedule=[{"at_s": 1, "kind": "gone"}], anchor_path=None)

    def test_schedule_validation_and_env(self):
        path = os.path.join(self.tmp.name, "faults.json")
        for bad in ([{"kind": "delay", "duration_s": 1}], [{"at_s": 1, "kind": "slow", "duration_s": 1}],
                    {"at_s": 1}, [{"at_s": 1, "kind": "freeze"}], [[1]], [{"at_s": "1", "kind": "gone"}]):
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(bad, fh)
            with self.assertRaises(ValueError, msg=bad):
                rs.load_schedule(path)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump([{"at_s": 3, "kind": "gone"}], fh)
        self.assertEqual(len(rs.load_schedule(path)), 1)
        timeline = os.path.join(self.tmp.name, "t.jsonl")
        with open(timeline, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"header": {"det_fps": 12.5}}) + "\n")
        self.assertEqual(rs._default_fps({rs.ENV_TIMELINE: timeline}), 12.5)
        self.assertEqual(rs._default_fps({rs.ENV_TIMELINE: timeline, rs.ENV_FPS: "9"}), 9.0)
        self.assertEqual(rs._default_fps({}), 10.0)


class WithGesturesTest(Base):
    """Подставная камера + FakeYolo + таймлайн gesturegen: на каждом кадре — бокс таймлайна в момент захвата."""
    fps = 20.0

    def test_boxes_follow_timeline(self):
        out = os.path.join(self.tmp.name, "g")
        timeline_path, _ = write_outputs(GenConfig(seed=2, gestures=1, negatives=0, det_fps=self.fps, lead_in_s=0.3,
                                                   dur_s=(0.8, 0.8), p_miss=0.0, fit=True), out)
        timeline = Timeline.load(timeline_path)
        model = FakeYolo(timeline, os.path.join(self.tmp.name, "anchor.json"), tensor="numpy", probe=self.probe)
        model.conf = 0.35
        pipe = self.started()
        seen, expected = 0, 0
        for _ in range(int(1.6 * self.fps)):
            img = np.asanyarray(pipe.wait_for_frames().get_color_frame().get_data())[:, ::-1]
            boxes = model(img, size=320).xyxy[0]
            box = timeline.box_at(model.anchor.rel_of(decode_marker(img)))
            expected += box is not None
            seen += len(boxes)
            if box is not None:
                np.testing.assert_allclose(boxes[0][:4], box[:4], rtol=1e-5)
        self.assertEqual(seen, expected)
        self.assertGreaterEqual(seen, int(0.8 * self.fps) - 1)
        self.assertEqual(self.probe.get("det_calls"), int(1.6 * self.fps))


try:
    sys.path.insert(0, str(ROOT / "neurobot" / "perception"))
    from camera_guard import CameraFailed, CameraGuard
except ImportError:
    CameraGuard = None


@unittest.skipIf(CameraGuard is None, "нет лесенки perception v2 (neurobot/perception/camera_guard.py)")
class CameraGuardTest(Base):
    """Лесенка perception v2 поверх подставной камеры: застывший кадр не «лечится» рестартом конвейера —
    лесенка доходит до hardware_reset и CameraFailed, ложных восстановлений нет."""

    def test_long_freeze_exhausts_ladder(self):
        guard = CameraGuard(rs, 640, 480, 30, timeout_ms=100, step_s=0.2, reset_pause_s=0.35)
        guard.open(reset=False)
        self.assertIsNotNone(guard.read())
        self.command(kind="freeze", duration_s=10)
        deadline = time.monotonic() + 5.0
        with self.assertLogs("camera_guard", "WARNING"), self.assertRaises(CameraFailed):
            while time.monotonic() < deadline:
                guard.read()
        self.assertEqual((guard.level, guard.recoveries, len(rs._state().resets)), (2, 0, 1))


class LazyEnvTest(unittest.TestCase):
    def test_state_from_environment(self):
        env = {rs.ENV_FPS: "7", rs.ENV_RESET_S: "1.5", rs.ENV_CONTROL: "", rs.ENV_FAULTS: ""}
        with mock.patch.dict(os.environ, env), mock.patch.object(rs, "_STATE", None):
            st = rs._state()
            self.assertEqual((st.fps, st.reset_s, st.control_path, st.schedule), (7.0, 1.5, None, []))

    def test_commands_written_before_process_start_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            control = os.path.join(d, "control.jsonl")
            with open(control, "w", encoding="utf-8") as fh:
                fh.write('{"kind": "gone"}\n')
            env = {rs.ENV_FPS: "50", rs.ENV_CONTROL: control, rs.ENV_FAULTS: ""}
            with mock.patch.dict(os.environ, env), mock.patch.object(rs, "_STATE", None):
                pipe = rs.pipeline()
                pipe.start()
                pipe.wait_for_frames(200)
                pipe.stop()
                self.assertEqual(rs._state().faults, [])

    def test_schedule_without_anchor_fails_at_start(self):
        with tempfile.TemporaryDirectory() as d:
            faults = os.path.join(d, "faults.json")
            with open(faults, "w", encoding="utf-8") as fh:
                json.dump([{"at_s": 3, "kind": "gone"}], fh)
            env = {rs.ENV_FPS: "7", rs.ENV_FAULTS: faults, rs.ENV_ANCHOR: ""}
            with mock.patch.dict(os.environ, env), mock.patch.object(rs, "_STATE", None):
                with self.assertRaisesRegex(ValueError, rs.ENV_ANCHOR):
                    rs.pipeline().start()


if __name__ == "__main__":
    unittest.main()
