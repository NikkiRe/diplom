"""predict_live v2: лесенка восстановления камеры (camera_guard.py, R1, R2, N12) на подставных модулях и кадры из
темы ROS (TopicCamera, --camera ros).

FakeRs — минимальный pyrealsense2 на поддельных часах (детерминированно, без ожиданий). Второй класс тестов
гоняет ту же лесенку против подставного pyrealsense2 стенда (neurobot/sim/gestures/realsense.py) в реальном
времени с короткими ступенями — проверка совместимости с контрактом; без этого модуля тест пропускается.
Кандидаты во владельцы камеры (holders) — на поддельном каталоге /proc. TopicCamera — на поддельных сообщениях
sensor_msgs/Image (поля header.stamp, encoding, width, height, step, data), без rclpy.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
PERCEPTION = ROOT / "neurobot" / "perception"
for p in (str(ROOT), str(PERCEPTION)):
    if p not in sys.path:
        sys.path.insert(0, p)

from camera_guard import CameraBusy, CameraFailed, CameraGuard, TopicCamera, holders  # noqa: E402

BUSY = "xioctl(VIDIOC_S_FMT) failed, errno=16 Last Error: Device or resource busy"


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += dt


class FakeRs:
    """Устройство с одним отказом: fault ∈ {None, stall, error, frozen, black}; heal — что его снимает
    (restart — новый start конвейера, reset — hardware_reset, None — ничего). frozen — тот же кадр с тем же
    номером; black — живая камера в темноте: чёрные кадры с растущим номером. absent_until — устройства нет до
    этого момента: start() и query_devices() как у отключённой камеры. busy — камерой владеет другой процесс;
    start_errors — ошибки следующих стартов конвейера по одной."""

    class stream:
        color = "color"

    class format:
        bgr8 = "bgr8"

    def __init__(self, clock: Clock, fps: float = 8.0):
        self.clock = clock
        self.fps = fps
        self.fault = None
        self.heal = None
        self.absent_until = 0.0
        self.busy = False
        self.start_errors = []
        self.attempts = self.starts = self.stops = self.resets = self.waits = 0
        self.n = 0
        self.last = None

    def present(self) -> bool:
        return self.clock() >= self.absent_until

    def pipeline(self):
        return _Pipe(self)

    def config(self):
        return _Cfg()

    def context(self):
        return _Ctx(self)

    def _healed(self, by: str) -> None:
        if self.heal == by:
            self.fault = None


class _Cfg:
    def enable_stream(self, *args):
        self.args = args


class _Ctx:
    def __init__(self, rs: FakeRs):
        self.rs = rs

    def query_devices(self):
        return [_Dev(self.rs)] if self.rs.present() else []


class _Dev:
    def __init__(self, rs: FakeRs):
        self.rs = rs

    def hardware_reset(self):
        self.rs.resets += 1
        self.rs._healed("reset")


class _Frame:
    def __init__(self, data, number):
        self.data = data
        self.number = number

    def __bool__(self):
        return True

    def get_data(self):
        return self.data

    def get_frame_number(self):
        return self.number


class _Frames:
    def __init__(self, data, number):
        self.color = _Frame(data, number)

    def get_color_frame(self):
        return self.color


class _Pipe:
    def __init__(self, rs: FakeRs):
        self.rs = rs

    def start(self, cfg):
        self.rs.attempts += 1
        if not self.rs.present():
            raise RuntimeError("No device connected")
        if self.rs.busy:
            raise RuntimeError(BUSY)
        if self.rs.start_errors:
            raise RuntimeError(self.rs.start_errors.pop(0))
        self.rs.starts += 1
        self.rs._healed("restart")
        return "profile"

    def stop(self):
        self.rs.stops += 1

    def try_wait_for_frames(self, timeout_ms):
        rs = self.rs
        rs.waits += 1
        if rs.fault == "error":
            raise RuntimeError("Frame didn't arrive within 5000")
        if rs.fault == "stall" or not rs.present():
            rs.clock.sleep(timeout_ms / 1000)
            return False, None
        rs.clock.sleep(1 / rs.fps)
        if rs.fault == "frozen" and rs.last is not None:
            return True, _Frames(rs.last.copy(), rs.n)
        rs.n += 1
        rs.last = np.full((48, 64, 3), 0 if rs.fault == "black" else 64, np.uint8)
        return True, _Frames(rs.last.copy(), rs.n)


def make(**kw):
    clock = Clock()
    rs = FakeRs(clock)
    guard = CameraGuard(rs, 64, 48, 30, clock=clock, sleep=clock.sleep, **kw)
    return clock, rs, guard


def read_for(guard: CameraGuard, clock: Clock, seconds: float):
    """Читать кадры seconds секунд поддельного времени; число хороших кадров."""
    end = clock() + seconds
    good = 0
    while clock() < end:
        good += guard.read() is not None
    return good


class Ladder(unittest.TestCase):
    def test_frames_flow(self):
        clock, rs, guard = make()
        guard.open(reset=False)
        self.assertEqual(read_for(guard, clock, 1.0), 8)
        self.assertEqual((guard.frames, guard.level, guard.recoveries, rs.resets), (8, 0, 0, 0))
        self.assertAlmostEqual(guard.frame_age(), 0.0)

    def test_open_does_not_reset_working_camera(self):
        clock, rs, guard = make(reset_pause_s=3.0)
        guard.open()
        self.assertEqual((rs.resets, rs.starts), (0, 1))
        self.assertEqual(clock(), 100.0)

    def test_open_resets_after_failed_start(self):
        clock, rs, guard = make(reset_pause_s=3.0)
        rs.start_errors = ["failed to set power state"]  # камера осталась в плохом состоянии (R2)
        with self.assertLogs("camera_guard", "ERROR"):
            guard.open()
        self.assertEqual((rs.attempts, rs.resets, rs.starts), (2, 1, 1))
        self.assertEqual(clock(), 103.0)
        self.assertIsNotNone(guard.read())

    def test_open_without_reset_leaves_failed_start_to_ladder(self):
        clock, rs, guard = make()
        rs.start_errors = ["failed to set power state"]
        with self.assertLogs("camera_guard", "ERROR"):
            guard.open(reset=False)
        self.assertEqual((rs.resets, guard.pipe), (0, None))
        while guard.read() is None:
            pass
        self.assertEqual((rs.starts, rs.resets, guard.recoveries), (1, 0, 1))

    def test_stall_healed_by_pipeline_restart(self):
        clock, rs, guard = make(step_s=3.0)
        guard.open(reset=False)
        read_for(guard, clock, 1.0)
        rs.fault, rs.heal = "stall", "restart"
        t_fail = clock()
        while guard.read() is None:
            pass
        self.assertEqual((rs.starts, rs.stops, rs.resets), (2, 1, 0))
        self.assertEqual((guard.level, guard.recoveries), (0, 1))
        self.assertLess(clock() - t_fail, 4.5)  # без кадров 3 с → рестарт → первый кадр

    def test_hung_device_healed_by_hardware_reset(self):
        clock, rs, guard = make(step_s=3.0, reset_pause_s=3.0)
        guard.open(reset=False)
        rs.fault, rs.heal = "stall", "reset"
        t_fail = clock()
        while guard.read() is None:
            pass
        self.assertEqual((rs.starts, rs.resets, guard.recoveries), (3, 1, 1))
        self.assertLess(clock() - t_fail, 10.5)  # 3 с → рестарт, ещё 3 с → reset + 3 с паузы → кадр

    def test_exhausted_ladder_raises(self):
        clock, rs, guard = make(step_s=3.0, reset_pause_s=3.0)
        guard.open(reset=False)
        rs.fault = "stall"
        t_fail = clock()
        with self.assertRaises(CameraFailed):
            while True:
                guard.read()
        self.assertEqual((rs.starts, rs.resets), (3, 1))
        self.assertAlmostEqual(clock() - t_fail, 12.0, delta=1.01)

    def test_driver_exceptions_count_as_no_frame_without_busy_loop(self):
        clock, rs, guard = make(step_s=3.0)
        guard.open(reset=False)
        rs.fault, rs.heal = "error", "restart"
        with self.assertLogs("camera_guard", "WARNING") as logs:
            while guard.read() is None:
                pass
        self.assertEqual(guard.recoveries, 1)
        self.assertLessEqual(rs.waits, 3.0 / 0.1 + 3)  # между исключениями пауза 0,1 с
        self.assertEqual(sum("Frame didn't arrive" in m for m in logs.output), 1)  # первая ошибка серии

    def test_frozen_stream_is_failure(self):
        clock, rs, guard = make(step_s=3.0)
        guard.open(reset=False)
        read_for(guard, clock, 0.5)
        rs.fault, rs.heal = "frozen", "restart"
        frames = guard.frames
        t_fail = clock()
        while guard.recoveries == 0:
            guard.read()
        self.assertEqual(guard.frames, frames + 1)  # застывшие кадры не считаются, только кадр восстановления
        self.assertEqual(rs.starts, 2)
        self.assertAlmostEqual(clock() - t_fail, 3.25, delta=0.2)

    def test_dark_scene_is_good_frames_without_ladder(self):
        clock, rs, guard = make(step_s=3.0)
        guard.open(reset=False)
        read_for(guard, clock, 0.5)
        self.assertFalse(guard.dark)
        rs.fault = "black"  # темно или крышка объектива: номер кадра растёт — камера жива
        with self.assertLogs("camera_guard", "WARNING") as logs:
            self.assertEqual(read_for(guard, clock, 10.0), 80)
        self.assertEqual(len(logs.output), 1)  # одна строка на вход в «темно», без лесенки
        self.assertTrue(guard.dark)
        self.assertEqual((guard.level, guard.recoveries, rs.starts, rs.resets), (0, 0, 1, 0))
        rs.fault = None
        with self.assertLogs("camera_guard", "INFO") as logs:
            read_for(guard, clock, 0.2)
        self.assertFalse(guard.dark)
        self.assertIn("снова светлый", logs.output[0])

    def test_missing_device_at_start_is_retried_by_ladder(self):
        clock, rs, guard = make(step_s=3.0, reset_pause_s=3.0)
        rs.absent_until = clock() + 5.0
        with self.assertLogs("camera_guard", "ERROR"):
            guard.open(reset=False)
        self.assertIsNone(guard.pipe)
        while guard.read() is None:
            pass
        self.assertEqual((rs.starts, guard.recoveries), (1, 1))

    def test_busy_camera_is_never_reset(self):
        clock, rs, guard = make(step_s=3.0)
        rs.busy = True  # камерой владеет другой процесс, на роботе — realsense2_camera (N12)
        with self.assertLogs("camera_guard", "ERROR") as logs:
            guard.open()
            self.assertIsNone(guard.pipe)
            t_open = clock()
            with self.assertRaises(CameraBusy):
                while True:
                    guard.read()
        self.assertIn("занята другим процессом", logs.output[0])
        self.assertEqual((rs.attempts, rs.resets), (2, 0))  # старт и рестарт конвейера, hardware_reset — ни разу
        self.assertAlmostEqual(clock() - t_open, 6.0, delta=1.01)

    def test_camera_taken_during_ladder_is_not_reset(self):
        clock, rs, guard = make(step_s=3.0)
        guard.open(reset=False)
        read_for(guard, clock, 0.5)
        rs.fault, rs.busy = "stall", True  # пока конвейер перезапускался, камеру занял другой процесс
        with self.assertLogs("camera_guard", "ERROR"), self.assertRaises(CameraBusy):
            while True:
                guard.read()
        self.assertEqual((rs.stops, rs.resets), (1, 0))
        self.assertTrue(issubclass(CameraBusy, CameraFailed))

    def test_configure_failure_keeps_stream(self):
        clock, rs, _ = make()

        def configure(profile):
            raise AssertionError("RGB sensor not found")

        guard = CameraGuard(rs, 64, 48, 30, configure=configure, clock=clock, sleep=clock.sleep)
        with self.assertLogs("camera_guard", "ERROR"):
            guard.open(reset=False)
        self.assertIsNotNone(guard.read())

    def test_close_stops_pipeline(self):
        clock, rs, guard = make()
        guard.open(reset=False)
        guard.close()
        self.assertEqual(rs.stops, 1)
        self.assertIsNone(guard.pipe)

    def test_invalid_params(self):
        for kw in ({"timeout_ms": 0}, {"step_s": 0}, {"reset_pause_s": -1}):
            with self.assertRaises(ValueError):
                CameraGuard(FakeRs(Clock()), **kw)


class Holders(unittest.TestCase):
    def test_processes_with_camera_nodes_open(self):
        with tempfile.TemporaryDirectory() as proc:
            def process(pid, comm, *targets):
                fd = os.path.join(proc, pid, "fd")
                os.makedirs(fd)
                for i, target in enumerate(targets):
                    os.symlink(target, os.path.join(fd, str(i)))
                if comm is not None:
                    with open(os.path.join(proc, pid, "comm"), "w") as fh:
                        fh.write(comm + "\n")

            process("10", "realsense2_camer", "/dev/null", "/dev/video2")
            process("11", "urg_node2", "/dev/ttyACM0")
            process("12", None, "/dev/bus/usb/001/004")  # comm не прочитать
            process("99", "python3", "/dev/video0")       # свой процесс
            os.makedirs(os.path.join(proc, "13"))          # fd не читается (чужой пользователь)
            os.symlink("99", os.path.join(proc, "self"))
            self.assertEqual(holders(proc, own_pid=99), ["? (12)", "realsense2_camer (10)"])
        self.assertEqual(holders(os.path.join(proc, "нет")), [])


def image_msg(img: np.ndarray, stamp: float, encoding: str = "rgb8", pad: int = 0) -> SimpleNamespace:
    """Поддельный sensor_msgs/Image: img — H×W×3 в порядке encoding, pad — байт выравнивания в конце строки."""
    h, w = img.shape[:2]
    rows = np.zeros((h, 3 * w + pad), np.uint8)
    rows[:, :3 * w] = img.reshape(h, 3 * w)
    sec = int(stamp)
    return SimpleNamespace(header=SimpleNamespace(stamp=SimpleNamespace(sec=sec, nanosec=int((stamp - sec) * 1e9))),
                           encoding=encoding, width=w, height=h, step=3 * w + pad, data=rows.tobytes())


class Topic(unittest.TestCase):
    """--camera ros: камерой владеет другой узел — без кадров только recovering и строка журнала, ни перезапуска,
    ни сброса, ни выхода (их у TopicCamera просто нет)."""

    def setUp(self):
        self.clock = Clock()
        self.cam = TopicCamera(64, 48, timeout_ms=1, step_s=3.0, clock=self.clock)
        self.stamp = 1000.0
        self.gray = np.full((48, 64, 3), 64, np.uint8)

    def send(self, img=None, same_stamp: bool = False, **kw):
        if not same_stamp:
            self.stamp += 0.1
        self.cam.on_image(image_msg(self.gray if img is None else img, self.stamp, **kw))

    def idle(self, seconds: float) -> None:
        """Сообщений нет seconds секунд поддельного времени (чтения по 0,1 с)."""
        for _ in range(int(round(seconds * 10))):
            self.clock.t += 0.1
            self.assertIsNone(self.cam.read())

    def test_rgb8_and_bgr8_become_bgr_with_row_padding(self):
        rgb = np.zeros((48, 64, 3), np.uint8)
        rgb[..., 0] = 200  # красный
        self.send(rgb, encoding="rgb8", pad=8)
        bgr = self.cam.read()
        self.assertEqual(bgr.shape, (48, 64, 3))
        self.assertTrue(bgr.flags.c_contiguous)  # cv2.flip и рамки в --show
        self.assertEqual(bgr[0, 0].tolist(), [0, 0, 200])
        self.send(bgr, encoding="bgr8")
        self.assertEqual(self.cam.read()[5, 5].tolist(), [0, 0, 200])
        self.assertEqual((self.cam.frames, self.cam.level, self.cam.dark), (2, 0, False))

    def test_latest_message_wins(self):
        self.send()
        light = np.full((48, 64, 3), 90, np.uint8)
        self.send(light)  # читатель не успел — берётся последнее сообщение
        self.assertEqual(self.cam.read()[0, 0, 0], 90)
        self.assertIsNone(self.cam.read())
        self.assertEqual(self.cam.frames, 1)

    def test_no_frames_is_recovering_without_exit(self):
        self.send()
        self.cam.read()
        with self.assertLogs("camera_guard", "WARNING") as logs:
            self.idle(2.9)
            self.assertEqual(self.cam.level, 0)
            self.idle(60.0)  # издатель молчит минуту: только ждём
        self.assertEqual(len(logs.output), 1)
        self.assertIn("устройство не трогаем", logs.output[0])
        self.assertEqual((self.cam.level, self.cam.recoveries), (1, 0))
        self.assertAlmostEqual(self.cam.frame_age(), 62.9, delta=0.01)
        self.send()
        with self.assertLogs("camera_guard", "WARNING") as logs:
            self.assertIsNotNone(self.cam.read())
        self.assertIn("снова идут", logs.output[0])
        self.assertEqual((self.cam.level, self.cam.recoveries, self.cam.frames), (0, 1, 2))

    def test_same_stamp_is_not_a_frame(self):
        self.send()
        self.cam.read()
        with self.assertLogs("camera_guard", "WARNING"):
            for _ in range(40):  # издатель застыл: шлёт тот же кадр 4 с
                self.clock.t += 0.1
                self.send(same_stamp=True)
                self.assertIsNone(self.cam.read())
        self.assertEqual((self.cam.frames, self.cam.level), (1, 1))
        self.send()
        self.assertIsNotNone(self.cam.read())
        self.assertEqual(self.cam.recoveries, 1)

    def test_dark_frames_are_good_frames(self):
        with self.assertLogs("camera_guard", "WARNING") as logs:
            for _ in range(50):
                self.clock.t += 0.1
                self.send(np.zeros((48, 64, 3), np.uint8))
                self.assertIsNotNone(self.cam.read())
        self.assertEqual(len(logs.output), 1)
        self.assertIn("кадр тёмный", logs.output[0])
        self.assertEqual((self.cam.dark, self.cam.level, self.cam.frames), (True, 0, 50))
        self.send()
        with self.assertLogs("camera_guard", "INFO"):
            self.cam.read()
        self.assertFalse(self.cam.dark)

    def test_unsuitable_messages_logged_once_per_kind(self):
        with self.assertLogs("camera_guard", "ERROR") as logs:
            for _ in range(3):
                self.send(encoding="yuv422")
                self.assertIsNone(self.cam.read())
            self.send(np.zeros((24, 32, 3), np.uint8))
            self.assertIsNone(self.cam.read())
            short = image_msg(self.gray, self.stamp + 0.05)
            short.data = short.data[:100]
            self.cam.on_image(short)
            self.assertIsNone(self.cam.read())
        self.assertEqual(len(logs.output), 3, logs.output)
        self.assertIn("кодировка yuv422", logs.output[0])
        self.assertIn("размер 32×24 (нужен 64×48", logs.output[1])
        self.send()
        self.assertIsNotNone(self.cam.read())

    def test_invalid_params(self):
        for kw in ({"timeout_ms": 0}, {"step_s": 0}):
            with self.assertRaises(ValueError):
                TopicCamera(**kw)


try:
    from neurobot.sim.gestures import realsense as stand_rs
except ImportError:
    stand_rs = None


@unittest.skipIf(stand_rs is None, "нет подставного pyrealsense2 стенда (neurobot/sim/gestures)")
class StandFake(unittest.TestCase):
    """Отказы подставного pyrealsense2 стенда (команды в файл управления) → лесенка восстанавливает поток
    без выхода процесса. Реальное время ≈ 9 с: ступень 0,4 с, отказы по 1 с."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.control = os.path.join(self.dir.name, "control.jsonl")
        open(self.control, "w").close()
        stand_rs.configure(fps=20, reset_s=0.3, control_path=self.control)
        self.guard = CameraGuard(stand_rs, 640, 480, 30, timeout_ms=200, step_s=0.4, reset_pause_s=0.5)
        self.guard.open(reset=False)
        self.assertIsNotNone(self.guard.read())

    def tearDown(self):
        self.guard.close()
        self.dir.cleanup()

    def survive(self, seconds: float, **fault) -> int:
        """Вбросить отказ (если задан) и читать кадры seconds секунд; число восстановлений. В конце поток должен
        идти."""
        if fault:
            with open(self.control, "a") as fh:
                fh.write(json.dumps(fault) + "\n")
        before = self.guard.recoveries
        t_end = time.monotonic() + seconds
        while time.monotonic() < t_end:
            self.guard.read()
        self.assertEqual(self.guard.level, 0, fault)
        self.assertLess(self.guard.frame_age(), 0.3, fault)
        return self.guard.recoveries - before

    def test_each_fault_recovers_in_process(self):
        self.assertGreaterEqual(self.survive(1.2, kind="stall"), 1)  # снимается рестартом конвейера
        for kind in ("freeze", "delay"):
            self.assertGreaterEqual(self.survive(1.8, kind=kind, duration_s=1.0), 1, kind)
        with self.assertLogs("camera_guard", "INFO") as logs:
            self.assertGreaterEqual(self.survive(2.5, kind="gone"), 1)  # снимается только hardware_reset
        self.assertTrue(any("hardware_reset: устройств 1" in m for m in logs.output), logs.output)

    def test_dark_scene_is_not_a_fault(self):
        with self.assertLogs("camera_guard", "WARNING"):
            self.assertEqual(self.survive(0.6, kind="black", duration_s=1.0), 0)
        self.assertTrue(self.guard.dark)
        self.assertEqual(self.survive(1.0), 0)
        self.assertFalse(self.guard.dark)


if __name__ == "__main__":
    unittest.main()
