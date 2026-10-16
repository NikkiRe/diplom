"""start_test без systemd: цикл ok (готовность, порядок, явный ноль), сценарии поломки (drop-in, занятый порт,
манифест без весов), итоги и квантили. Только stdlib, Python 3.8+."""
from __future__ import annotations

import os
import shutil
import signal
import socket
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))

from neurobot_deploy import start_test  # noqa: E402
from neurobot_deploy.config import Config, parse_env  # noqa: E402
from neurobot_deploy.sd import Entry, Systemd  # noqa: E402

UNITS = ["neurobot-driver@urg.service", "neurobot-safety.service", "neurobot-executor.service",
         "neurobot-perception.service", "neurobot-rosbridge.service"]


class FakeSd(Systemd):
    """Цель запускается мгновенно; юниты становятся active в порядке списка через 1 с каждый."""

    ZERO = "neurobot-zero: safety: zero_sent: /cmd_vel: нули Twist, подписчиков 1"

    def __init__(self, order=UNITS, zero=ZERO):
        super().__init__(run=None)
        self.order, self.zero, self.calls, self.started_at = order, zero, [], None

    def ctl(self, *args):
        self.calls.append(args)
        if args[:3] == ("start", "--no-block", start_test.TARGET):
            self.started_at = start_test.time.monotonic()
        if args[0] == "stop":
            self.started_at = None

    def show(self, units, props=()):
        out = {}
        for u in units:
            if self.started_at is None:
                out[u] = {"ActiveState": "inactive"}
                continue
            idx = self.order.index(u) + 1 if u in self.order else len(self.order) + 1
            out[u] = {"ActiveState": "active", "NRestarts": "0",
                      "ActiveEnterTimestampMonotonic": str(int((self.started_at + 0.01 * idx) * 1e6))}
        return out

    def journal(self, invocation=None, unit=None, lines=200, since=None):
        now = start_test.time.time()
        assert unit == "neurobot-safety.service", unit  # явный ноль шлёт ExecStopPost единственного писателя
        return [Entry(now, 6, "neurobot-run: /usr/bin/python3 -m neurobot.safety", ""), Entry(now, 6, self.zero, "")] \
            if self.zero else []


class StartTestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        manifest = os.path.join(self.tmp, "weights.sha256")
        with open(manifest, "w", encoding="utf-8") as f:
            f.write("# веса\n{}  /m/yolo_hand.pt\n{}  /m/snn.pth\n".format("a" * 64, "b" * 64))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.cfg = Config({"NEUROBOT_DRIVERS": "urg", "NEUROBOT_COMPONENTS": "safety executor perception rosbridge",
                           "NEUROBOT_WEIGHTS": manifest, "NEUROBOT_ROSBRIDGE_PORT": str(self.port)},
                          os.path.join(self.tmp, "n.env"))

    def runner(self, sd, reports, timeout=5.0):
        st = start_test.StartTest(self.cfg, sd, ["doctor"], self.tmp, timeout, 5.0, sleep=lambda s: None,
                                  dropin_root=os.path.join(self.tmp, "run"), work=os.path.join(self.tmp, "work"))
        seen = []

        def fake_doctor():
            seen.append(self.snapshot())
            return reports.pop(0) if len(reports) > 1 else reports[0]
        st.doctor = fake_doctor
        return st, seen

    def snapshot(self) -> dict:
        dropins = sorted(str(p.relative_to(self.tmp)) for p in Path(self.tmp, "run").rglob("*.conf"))
        with socket.socket() as s:
            busy = s.connect_ex(("127.0.0.1", self.port)) == 0
        return {"dropins": dropins, "port_busy": busy}

    def test_ok_cycle_records_readiness_order_and_zero(self):
        sd = FakeSd()
        st, _ = self.runner(sd, [{"ok": False, "root": {"component": "perception", "code": "starting"}},
                                 {"ok": True, "root": None}])
        rec = st.cycle(1, start_test.SCENARIOS["ok"])
        self.assertTrue(rec["passed"], rec)
        self.assertTrue(rec["order_ok"])
        self.assertTrue(rec["zero_on_stop"])
        self.assertEqual(rec["zero_line"], FakeSd.ZERO)
        self.assertEqual(rec["restarts"], 0)
        self.assertEqual(rec["doctor_named"], [])
        self.assertIn(("reset-failed",) + tuple(UNITS), sd.calls)

    def test_wrong_order_or_missing_zero_fails(self):
        st, _ = self.runner(FakeSd(order=list(reversed(UNITS))), [{"ok": True, "root": None}])
        self.assertFalse(st.cycle(1, start_test.SCENARIOS["ok"])["order_ok"])
        st, _ = self.runner(FakeSd(zero=""), [{"ok": True, "root": None}])
        rec = st.cycle(2, start_test.SCENARIOS["ok"])
        self.assertEqual((rec["passed"], rec["why"]), (False, "после stop: нет явного нуля в журнале safety_gate"))
        agent_only = Config({"NEUROBOT_COMPONENTS": "rosbridge mcp"}, self.cfg.path)
        self.assertEqual(start_test.StartTest(agent_only, FakeSd(zero=""), ["doctor"], self.tmp, 1, 1)._zero_after(0),
                         (True, ""))

    def test_undelivered_zero_is_not_counted(self):
        # строка `neurobot zero` есть, но ноль не ушёл: отказ rclpy или нет подписчика (twist_mux не слушал)
        for line in ("neurobot-zero: safety: ros_unavailable: явный ноль не отправлен: rclpy недоступен",
                     "neurobot-zero: safety: zero_undelivered: явный ноль в /cmd_vel не доставлен: подписчиков нет"):
            with self.subTest(line=line):
                st, _ = self.runner(FakeSd(zero=line), [{"ok": True, "root": None}])
                rec = st.cycle(1, start_test.SCENARIOS["ok"])
                self.assertFalse(rec["zero_on_stop"])
                self.assertEqual((rec["passed"], rec["why"]), (False, "после stop: явный ноль не доставлен: " + line))

    def test_unverified_ros_is_not_ok(self):
        # doctor не снял граф ROS — это не «ОК»: цикл не засчитан, причина названа
        st, _ = self.runner(FakeSd(), [{"ok": False, "root": {"component": "ros", "code": "ros_unavailable",
                                                              "text": "датчики … не проверены"}}], timeout=0.05)
        rec = st.cycle(1, start_test.SCENARIOS["ok"])
        self.assertFalse(rec["passed"])
        self.assertIn("ros/ros_unavailable", rec["why"])

    def test_broken_scenarios_use_dropins_and_clean_up(self):
        named = {"safety_down": ("safety", "start_timeout"), "no_camera": ("perception", "camera_missing"),
                 "rosbridge_down": ("rosbridge", "port_timeout")}
        for name, (comp, code) in named.items():
            with self.subTest(scenario=name):
                st, seen = self.runner(FakeSd(), [{"ok": False, "root": {"component": comp, "code": code}}])
                rec = st.cycle(1, start_test.scenario(self.cfg, name))
                self.assertTrue(rec["passed"], rec)
                unit = "neurobot-{}.service".format(comp)
                self.assertEqual(seen[0]["dropins"], ["run/{}.d/{}".format(unit, start_test.DROPIN)])
                self.assertEqual(self.snapshot()["dropins"], [])
                self.assertFalse(Path(self.tmp, "work", unit + ".env").exists())
                self.assertIn(("daemon-reload",), st.sd.calls)

    def test_dropin_env_file_overrides_values(self):
        # вместо rosbridge — sleep с теми же аргументами: проверка глобов перед стартом проходит, порт не открывается
        cmd = "ros2 launch rosbridge_server rosbridge_websocket_launch.xml topics_glob:=\"'[/cmd_vel_agent]'\""
        self.cfg = Config(dict(self.cfg.values, NEUROBOT_ROSBRIDGE_CMD=cmd), self.cfg.path)
        st, _ = self.runner(FakeSd(), [{"ok": False, "root": {"component": "rosbridge", "code": "port_timeout"}}])
        undo = st._sabotage(start_test.scenario(self.cfg, "rosbridge_down"))
        try:
            env = Path(self.tmp, "work", "neurobot-rosbridge.service.env").read_text(encoding="utf-8")
            dropin = Path(self.tmp, "run", "neurobot-rosbridge.service.d", start_test.DROPIN).read_text(
                encoding="utf-8")
        finally:
            undo()
        self.assertEqual(parse_env(env), {"NEUROBOT_ROSBRIDGE_CMD": "sh -c 'exec sleep 3600' " + cmd,
                                          "NEUROBOT_WAIT_S": "5"})
        self.assertIn('topics_glob:=\\"', env)  # кавычки значения экранированы для systemd и bash
        self.assertIn("[Service]\nEnvironmentFile={}/work/neurobot-rosbridge.service.env\n".format(self.tmp), dropin)

    def test_port_busy_holds_port_only_during_start(self):
        st, seen = self.runner(FakeSd(), [{"ok": False, "root": {"component": "rosbridge", "code": "port_busy"}}])
        self.assertTrue(st.cycle(1, start_test.SCENARIOS["port_busy"])["passed"])
        self.assertTrue(seen[0]["port_busy"])
        self.assertFalse(self.snapshot()["port_busy"])

    def test_doctor_subprocess_json_env_and_garbage(self):
        code = "import json, os; print(json.dumps({'ok': True, 'root': None, 'pp': os.environ.get('PYTHONPATH')}))"
        st = start_test.StartTest(self.cfg, FakeSd(), [sys.executable, "-c", code], self.tmp, 1.0, 1.0,
                                  doctor_env=dict(os.environ, PYTHONPATH="/opt/neurobot/lib"))
        self.assertEqual(st.doctor(), {"ok": True, "root": None, "pp": "/opt/neurobot/lib"})
        st.doctor_cmd = [sys.executable, "-c", "import sys; sys.exit('No module named neurobot_deploy')"]
        bad = st.doctor()
        self.assertEqual((bad["ok"], bad["root"]["code"]), (False, "doctor_error"))
        self.assertIn("No module named", bad["root"]["text"])

    def test_scenarios_follow_composition(self):
        self.assertEqual(start_test.applicable(self.cfg), ["safety_down", "no_camera", "no_weights", "port_busy",
                                                           "rosbridge_down"])
        agent = Config({"NEUROBOT_COMPONENTS": "mcp picoclaw", "NEUROBOT_LABWORKERS": "vision"}, self.cfg.path)
        self.assertEqual(start_test.applicable(agent), ["worker_down"])
        sc = start_test.scenario(agent, "worker_down")
        self.assertEqual((sc.expect, sc.env), (("labworker@vision", "labworker_timeout"), {"labworker@vision": {
            "NEUROBOT_LABWORKER_vision_CMD": "sleep 3600", "NEUROBOT_LABWORKER_WAIT_S": "5"}}))

    def test_worker_down_dropin_on_instance(self):
        self.cfg = Config(dict(self.cfg.values, NEUROBOT_LABWORKERS="vision"), self.cfg.path)
        st, seen = self.runner(FakeSd(), [{"ok": False, "root": {"component": "labworker@vision",
                                                                 "code": "labworker_timeout"}}])
        self.assertTrue(st.cycle(1, start_test.scenario(self.cfg, "worker_down"))["passed"])
        self.assertEqual(seen[0]["dropins"], ["run/neurobot-labworker@vision.service.d/" + start_test.DROPIN])

    def test_workers_are_one_readiness_level(self):
        self.cfg = Config(dict(self.cfg.values, NEUROBOT_LABWORKERS="yolo vision"), self.cfg.path)
        workers = ["neurobot-labworker@yolo.service", "neurobot-labworker@vision.service"]
        st, _ = self.runner(FakeSd(order=UNITS + list(reversed(workers))), [{"ok": True, "root": None}])
        self.assertTrue(st.cycle(1, start_test.SCENARIOS["ok"])["order_ok"])  # воркеры между собой — в любом порядке
        st, _ = self.runner(FakeSd(order=UNITS[:4] + workers + UNITS[4:]), [{"ok": True, "root": None}])
        self.assertFalse(st.cycle(2, start_test.SCENARIOS["ok"])["order_ok"])  # воркер раньше rosbridge

    def test_leftovers_of_killed_run_are_cleared(self):
        # start_test, убитый SIGKILL посреди поломки, оставил бы юнит с подменой до перезагрузки
        run, work = Path(self.tmp, "run"), Path(self.tmp, "work")
        mine = run / "neurobot-safety.service.d" / start_test.DROPIN
        other = run / "neurobot-safety.service.d" / "10-lab.conf"
        for path in (mine, other, work / "neurobot-safety.service.env", work / "weights.sha256"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        sd = FakeSd()
        gone = start_test.clear_leftovers(sd, str(run), str(work))
        self.assertEqual(gone, [str(mine), str(work / "neurobot-safety.service.env"), str(work / "weights.sha256")])
        self.assertTrue(other.exists())
        self.assertEqual(sd.calls, [("daemon-reload",)])
        self.assertEqual(start_test.clear_leftovers(sd, str(run), str(work)), [])
        self.assertEqual(sd.calls, [("daemon-reload",)])

    def test_sigterm_ends_run_through_finally(self):
        # systemctl stop или timeout шлют SIGTERM: CLI превращает его в выход, и finally цикла снимает подмену
        from neurobot_deploy import __main__ as cli
        args = types.SimpleNamespace(broken=[])
        with mock.patch.object(os, "geteuid", lambda: 0), mock.patch.object(signal, "signal") as sig, \
                mock.patch.object(start_test, "run", return_value=0) as run:
            self.assertEqual(cli.cmd_start_test(self.cfg, args), 0)
        run.assert_called_once()
        (signum, handler), _ = sig.call_args
        self.assertEqual(signum, signal.SIGTERM)
        with self.assertRaises(SystemExit):
            handler(signal.SIGTERM, None)

    def test_no_weights_manifest(self):
        text = start_test.weights_missing_manifest(self.cfg.get("NEUROBOT_WEIGHTS"))
        self.assertEqual(text, "# веса\n{}  /m/yolo_hand.pt.start-test-missing\n{}  /m/snn.pth\n".format(
            "a" * 64, "b" * 64))

    def test_wrong_component_named_fails_after_timeout(self):
        st, _ = self.runner(FakeSd(), [{"ok": False, "root": {"component": "executor", "code": "crashed",
                                                              "text": "убит сигналом 9"}}], timeout=0.05)
        rec = st.cycle(1, start_test.SCENARIOS["no_camera"])
        self.assertFalse(rec["passed"])
        self.assertEqual(rec["doctor_named"], ["executor/crashed"])
        self.assertIn("executor/crashed: убит сигналом 9", rec["why"])

    def test_summary(self):
        cycles = [{"cycle": i, "scenario": "ok", "passed": True, "why": "", "t_target_s": float(i),
                   "t_doctor_s": i + .5,
                   "order_ok": True, "zero_on_stop": True, "restarts": 0} for i in range(1, 5)]
        cycles.append({"cycle": 5, "scenario": "port_busy", "passed": False, "why": "doctor за 1 с: …"})
        s = start_test.summarize(cycles)
        self.assertEqual(s["ok"]["passed"], 4)
        self.assertEqual(s["ok"]["t_target_s"], {"p50": 2.5, "p95": 3.85, "max": 4.0})
        self.assertEqual(s["port_busy"]["failed"], [{"cycle": 5, "why": "doctor за 1 с: …"}])
        self.assertIsNone(start_test.quantile([], 0.5))


if __name__ == "__main__":
    unittest.main()
