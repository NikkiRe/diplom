"""fakes/scenarios.py без systemd: kill -9 главного процесса юнита — только при живом MainPID (у неактивного юнита он 0,
а kill(0, SIGKILL) убил бы группу процессов самого сценария); сценарий, который нельзя провести, — ok false с причиной,
следующие идут; подмены прерванного прогона снимаются до первого сценария, SIGTERM — выход через finally. Только stdlib,
Python 3.8+."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def load_scenarios():
    """Модуль сценариев с профилем стенда (он читает env-файл при импорте)."""
    with mock.patch.dict(os.environ, {"NEUROBOT_ENV": str(DEPLOY / "fakes/fake.env")}):
        spec = importlib.util.spec_from_file_location("nb_scenarios", str(DEPLOY / "fakes/scenarios.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class Kill9Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sc = load_scenarios()

    def test_kills_only_live_main_pid(self):
        for pid in ("0", "", "[not set]"):
            with self.subTest(pid=pid), mock.patch.object(self.sc, "prop", lambda u, n: pid), \
                    mock.patch.object(os, "kill") as kill:
                with self.assertRaisesRegex(self.sc.ScenarioError, "нет главного процесса"):
                    self.sc.kill9("neurobot-safety.service")
                kill.assert_not_called()
        with mock.patch.object(self.sc, "prop", lambda u, n: "4242"), mock.patch.object(os, "kill") as kill:
            self.sc.kill9("neurobot-safety.service")
        kill.assert_called_once_with(4242, signal.SIGKILL)

    def test_scenario_that_cannot_run_fails_and_the_rest_go_on(self):
        out = io.StringIO()
        with mock.patch.object(self.sc, "prop", lambda u, n: "0"), mock.patch.object(os, "kill") as kill, \
                mock.patch.object(os, "geteuid", lambda: 0), mock.patch.object(self.sc, "clear"), \
                mock.patch.object(signal, "signal"), \
                mock.patch.dict(self.sc.SCENARIOS, {"verify": lambda: {"ok": True}}), \
                mock.patch.object(sys, "argv", ["scenarios.py", "kill", "verify"]), contextlib.redirect_stdout(out):
            code = self.sc.main()
        kill.assert_not_called()
        records = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(code, 1)
        self.assertEqual([(r["scenario"], r["ok"]) for r in records], [("kill", False), ("verify", True)])
        self.assertIn("MainPID", records[0]["error"])


class LeftoversTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sc = load_scenarios()

    def test_cleared_before_first_scenario_and_sigterm_exits(self):
        # прогон, убитый на середине, оставил бы подмену (--hang-after у safety_gate) следующему
        order = []
        with mock.patch.object(self.sc, "clear", lambda: order.append("clear")), \
                mock.patch.object(os, "geteuid", lambda: 0), mock.patch.object(signal, "signal") as sig, \
                mock.patch.dict(self.sc.SCENARIOS, {"verify": lambda: order.append("verify") or {"ok": True}}), \
                mock.patch.object(sys, "argv", ["scenarios.py", "verify"]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.sc.main(), 0)
        self.assertEqual(order, ["clear", "verify"])
        (signum, handler), _ = sig.call_args
        self.assertEqual(signum, signal.SIGTERM)
        with self.assertRaises(SystemExit):  # finally сценария снимет его подмену
            handler(signal.SIGTERM, None)

    def test_clear_removes_dropins_and_stray_publisher(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        unit = self.sc.SAFETY
        dropin = os.path.join(tmp, "{}.d", "60-neurobot-scenario.conf")
        os.makedirs(os.path.dirname(dropin.format(unit)))
        for path in (dropin.format(unit), os.path.join(tmp, unit + ".env")):
            Path(path).write_text("x", encoding="utf-8")
        with mock.patch.object(self.sc, "DROPIN", dropin), mock.patch.object(self.sc, "WORK", tmp), \
                mock.patch.object(self.sc, "sh") as sh, mock.patch.object(self.sc.sd, "ctl") as ctl:
            self.sc.clear()
        self.assertFalse(os.path.exists(dropin.format(unit)) or os.path.exists(os.path.join(tmp, unit + ".env")))
        sh.assert_called_once_with("systemctl", "stop", self.sc.TRACKER, check=False)
        ctl.assert_called_once_with("daemon-reload")


if __name__ == "__main__":
    unittest.main()
