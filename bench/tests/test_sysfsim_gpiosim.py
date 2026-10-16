"""Бэкенд gpiosim на настоящем модуле ядра gpio-sim: только Linux под root (ВМ sbc).

Вне Linux или без root тест пропускается. Сквозная проверка с потребителями uAPI (libgpiod, hw-mcp) —
bench/sysfsim/vm_check.sh. Только стандартная библиотека: запускается и системным Python ВМ.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.sysfsim.gpio import CONFIGFS, GpioError, GpioSim  # noqa: E402

ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
# GPIO_V2_GET_LINE_IOCTL = _IOWR(0xB4, 0x07, struct gpio_v2_line_request) из include/uapi/linux/gpio.h: 592 байта,
# offsets[0] — 0, consumer — 256…287, config.flags (u64) — 288, num_lines — 560, fd (s32) — 588.
LINE_REQUEST_SIZE = 592
LINE_REQUEST_IOCTL = 3 << 30 | LINE_REQUEST_SIZE << 16 | 0xB4 << 8 | 0x07
LINE_FLAG_INPUT = 1 << 2


def request_input(chip: str, line: int, consumer: str) -> int:
    """Занять линию входом без событий фронтов, как потребитель uAPI; вернуть дескриптор запроса."""
    req = bytearray(LINE_REQUEST_SIZE)
    struct.pack_into("=I", req, 0, line)
    req[256:256 + len(consumer)] = consumer.encode()
    struct.pack_into("=Q", req, 288, LINE_FLAG_INPUT)
    struct.pack_into("=I", req, 560, 1)
    with open(Path("/dev", chip), "rb", buffering=0) as f:
        fcntl.ioctl(f, LINE_REQUEST_IOCTL, req)
    return struct.unpack_from("=i", req, 588)[0]


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0 and CONFIGFS.is_dir(),
                     "нужны Linux, root и модуль gpio-sim")
class GpioSimTest(unittest.TestCase):
    def test_chip_lines_and_cleanup(self):
        label = f"sysfsim-test-{os.getpid()}"
        sim = GpioSim(label, {17: "pir_hall", 22: "door_entry", 27: "relay_fan"})
        try:
            self.assertTrue(Path("/dev", sim.chip).exists())
            self.assertEqual((sim.dev / "bank0/label").read_text().strip(), label)
            for level in (True, False, True):
                sim.set(22, level)
                self.assertEqual((sim.lines / "sim_gpio22/value").read_text().strip(), str(int(level)))
            self.assertFalse(sim.get(27))   # выход без потребителя — подтяжка по умолчанию, вниз
            self.assertEqual(sim.busy(), {})
            fds = [request_input(sim.chip, 22, "sysfsim-test"), request_input(sim.chip, 5, "other")]
            try:   # линия 5 — не линия платы, но в банке 0…27 её тоже можно занять
                self.assertEqual(sim.busy(), {5: "other", 22: "sysfsim-test"})
                sim.RELEASE_WAIT_S = 0.3
                with self.assertRaisesRegex(GpioError, r"линии 5 \(other\), 22 \(sysfsim-test\) заняты"):
                    sim.close()   # занятый чип не снимается
                self.assertEqual((sim.dev / "live").read_text().strip(), "1")
            finally:
                for fd in fds:
                    os.close(fd)
        finally:
            sim.close()
        self.assertFalse(sim.dev.exists())
        self.assertFalse(sim.lines.exists())

    def test_failure_after_live_removes_the_device(self):
        """Сбой после live=1 до чтения имени чипа (подменённое чтение dev_name): чип снят, устройства нет."""
        real_read = Path.read_text

        def read_text(path: Path, *args, **kw) -> str:
            if path.name == "dev_name":
                raise OSError("сбой чтения")
            return real_read(path, *args, **kw)
        with mock.patch.object(Path, "read_text", read_text), self.assertRaisesRegex(OSError, "сбой чтения"):
            GpioSim(f"sysfsim-test-{os.getpid()}", {17: "pir_hall", 22: "door_entry", 27: "relay_fan"})
        self.assertFalse((CONFIGFS / f"sysfsim-{os.getpid()}").exists())

    def start_run(self, duration: int) -> tuple[subprocess.Popen, Path, Path]:
        """run --gpio gpiosim в фоне на новом корне; вернуть процесс, его устройство configfs и журнал истины, когда
        чип создан (ready-file). В конце теста живому процессу — SIGTERM: симулятор снимет чип сам."""
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="sysfsim-gpiosim-")))
        root, truth, ready = tmp / "root", tmp / "truth.jsonl", tmp / "ready.json"
        subprocess.run([sys.executable, "-m", "bench.sysfsim", "init", "--root", str(root)], cwd=ROOT, env=ENV,
                       check=True)
        proc = subprocess.Popen([sys.executable, "-m", "bench.sysfsim", "run", "--root", str(root), "--gpio", "gpiosim",
                                 "--gpio-label", f"sysfsim-test-{os.getpid()}", "--duration", str(duration),
                                 "--truth", str(truth), "--ready-file", str(ready)],
                                cwd=ROOT, env=ENV, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

        def stop():
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
            proc.wait(15)
            proc.stderr.close()
        self.addCleanup(stop)
        deadline = time.monotonic() + 15
        while not ready.exists():
            self.assertIsNone(proc.poll())
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        return proc, CONFIGFS / f"sysfsim-{proc.pid}", truth

    def test_run_holds_chip_until_signal(self):
        """run --gpio gpiosim по концу сценария не выходит и держит чип; SIGTERM — run_end, чип снят, код 0."""
        proc, dev, truth = self.start_run(2)
        self.assertIn("сценарий окончен", proc.stderr.readline())
        time.sleep(0.5)
        self.assertIsNone(proc.poll())
        self.assertEqual((dev / "live").read_text().strip(), "1")
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(15), 0)
        self.assertFalse(dev.exists())
        end = json.loads(truth.read_text().splitlines()[-1])
        self.assertEqual((end["type"], end["t"]), ("run_end", 2.0))

    def test_signal_before_end_removes_the_chip(self):
        """SIGTERM до конца сценария: run_end, чип снят, код 0 — без ожидания после конца и сообщения о нём."""
        proc, dev, truth = self.start_run(900)
        self.assertTrue(dev.exists())
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(15), 0)
        self.assertEqual(proc.stderr.read(), "")
        self.assertFalse(dev.exists())
        end = json.loads(truth.read_text().splitlines()[-1])
        self.assertEqual(end["type"], "run_end")
        self.assertLess(end["t"], 900)


if __name__ == "__main__":
    unittest.main()
