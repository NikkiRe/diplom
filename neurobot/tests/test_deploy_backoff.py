"""neurobot/deploy: пауза перед частыми автоматическими рестартами распознавания (H4, backoff.py и precheck).
Только stdlib, Python 3.8+."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))

from neurobot_deploy import __main__ as cli  # noqa: E402
from neurobot_deploy import backoff  # noqa: E402
from neurobot_deploy.config import Config, EnvError  # noqa: E402
from neurobot_deploy.sd import SystemdError  # noqa: E402

PAUSES = [0.0, 5.0, 15.0, 30.0]
UNIT = "neurobot-perception.service"


class PlanTest(unittest.TestCase):
    def test_series_grows_and_repeats_last(self):
        state, got, t = None, [], 0.0
        for n in range(7):  # ручной старт, затем рестарты по кругу: процесс живёт 2 с, RestartSec 1
            pause, streak, state = backoff.plan(n, state, "b", t, PAUSES, 600.0)
            got.append((pause, streak))
            t += pause + 3.0
        self.assertEqual(got, [(0.0, 0), (0.0, 1), (5.0, 2), (15.0, 3), (30.0, 4), (30.0, 5), (30.0, 6)])

    def test_reset_after_long_run_reboot_and_manual_start(self):
        prev = {"boot": "b", "t": 100.0, "streak": 4}
        self.assertEqual(backoff.plan(9, prev, "b", 100.0 + 599.0, PAUSES, 600.0)[:2], (30.0, 5))
        self.assertEqual(backoff.plan(9, prev, "b", 100.0 + 600.0, PAUSES, 600.0)[:2], (0.0, 0))  # ≈10 мин работы
        self.assertEqual(backoff.plan(9, prev, "другая загрузка", 101.0, PAUSES, 600.0)[:2], (0.0, 0))
        self.assertEqual(backoff.plan(0, prev, "b", 101.0, PAUSES, 600.0)[:2], (0.0, 0))  # человек, сторож
        self.assertEqual(backoff.plan(9, prev, "b", 50.0, PAUSES, 600.0)[:2], (0.0, 0))  # часы назад — не серия

    def test_bad_state_and_new_state(self):
        for prev in (None, {}, {"boot": "b"}, {"boot": "b", "t": "x"}, {"boot": "b", "t": 1.0, "streak": "x"}):
            with self.subTest(prev=prev):
                self.assertEqual(backoff.plan(3, prev, "b", 2.0, PAUSES, 600.0)[:2], (0.0, 0))
        pause, streak, state = backoff.plan(3, {"boot": "b", "t": 1.0, "streak": 1}, "b", 2.0, PAUSES, 600.0)
        self.assertEqual(state, {"boot": "b", "t": 2.0 + 5.0, "streak": 2})  # время старта — после паузы
        self.assertEqual(backoff.plan(3, {"boot": "b", "t": 1.0, "streak": 1}, "b", 2.0, [], 600.0)[0], 0.0)


class FakeSd:
    def __init__(self, n=0, error=False):
        self.n, self.error, self.calls = n, error, []

    def show(self, units, props=()):
        self.calls.append((tuple(units), tuple(props)))
        if self.error:
            raise SystemdError("systemctl: нет связи с systemd")
        return {u: {"Id": u, "NRestarts": str(self.n)} for u in units}


class PrecheckPauseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config({}, "/etc/neurobot/neurobot.env")
        self.slept = []
        self.env = {"STATE_DIRECTORY": self.tmp.name, "INVOCATION_ID": "inv"}

    def pause(self, sd, now, component="perception", env=None):
        err = io.StringIO()
        with mock.patch.dict(os.environ, self.env if env is None else env, clear=False), \
                mock.patch.object(backoff, "boot_id", lambda: "b"), contextlib.redirect_stderr(err):
            got = cli.restart_pause(self.cfg, component, sd, clock=lambda: now, sleep=self.slept.append)
        return got, err.getvalue()

    def test_loop_from_unit(self):
        got = [self.pause(FakeSd(n), t)[0] for n, t in ((0, 10.0), (1, 13.0), (2, 16.0), (3, 24.0), (4, 42.0))]
        self.assertEqual(got, [0.0, 0.0, 5.0, 15.0, 30.0])
        self.assertEqual(self.slept, [5.0, 15.0, 30.0])
        _, err = self.pause(FakeSd(5), 75.0)
        self.assertIn("neurobot-precheck: perception: restart_loop: 5-й рестарт подряд", err)
        self.assertIn("пауза 30 с", err)
        with open(os.path.join(self.tmp.name, backoff.STATE), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"boot": "b", "t": 105.0, "streak": 5})

    def test_only_perception_and_only_from_unit(self):
        sd = FakeSd(7)
        self.assertEqual(self.pause(sd, 1.0, component="safety"), (0.0, ""))  # цепочка движения — без паузы
        self.assertEqual(self.pause(sd, 1.0, env={"INVOCATION_ID": "", "STATE_DIRECTORY": self.tmp.name}), (0.0, ""))
        self.assertEqual(sd.calls, [])
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_systemd_unreachable_means_no_pause(self):
        self.pause(FakeSd(0), 1.0)
        self.pause(FakeSd(1), 2.0)
        self.assertEqual(self.pause(FakeSd(error=True), 3.0)[0], 0.0)  # как ручной старт: серия обнулена
        self.assertEqual(self.slept, [])

    def test_unwritable_state_is_a_warning(self):
        os.chmod(self.tmp.name, 0o500)
        self.addCleanup(os.chmod, self.tmp.name, 0o700)
        if os.access(self.tmp.name, os.W_OK):
            self.skipTest("root пишет и в каталог без прав")
        got, err = self.pause(FakeSd(1), 1.0)
        self.assertEqual(got, 0.0)
        self.assertIn("restart_loop: метку запуска не записать", err)

    def test_env_values(self):
        self.assertEqual(self.cfg.nums("NEUROBOT_RESTART_PAUSES_S"), PAUSES)
        self.assertEqual(self.cfg.num("NEUROBOT_RESTART_RESET_S"), 600.0)
        for bad in ("5 x", "-1 5"):
            with self.subTest(bad=bad), self.assertRaisesRegex(EnvError, "NEUROBOT_RESTART_PAUSES_S"):
                Config({"NEUROBOT_RESTART_PAUSES_S": bad}, "e").nums("NEUROBOT_RESTART_PAUSES_S")


if __name__ == "__main__":
    unittest.main()
