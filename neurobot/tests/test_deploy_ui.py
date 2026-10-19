"""neurobot/deploy и пульт оператора (компонент ui): precheck ui (темы стопа и защёлки в env — те, что в коде пульта),
издатели узла пульта против списка в коде (один список у deploy и у шлюза), wait-graph ui, ссылка входа `neurobot
ui-url` (код пишет root, по ссылкам не идёт; шлюз меняет его на сессию один раз), doctor: отказ пульта —
предупреждение, лишний издатель — ошибка. Только stdlib, Python 3.8+."""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "neurobot" / "deploy"
for p in (str(DEPLOY), str(ROOT), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

from neurobot_deploy import __main__ as cli  # noqa: E402
from neurobot_deploy import checks, doctor, uiurl  # noqa: E402
from neurobot_deploy.config import Config  # noqa: E402
from neurobot_deploy.probes import ProbeError  # noqa: E402
from test_deploy_doctor import AGENT_OK, NOW, WALL, FakeSd, failed, healthy, mcp_ok, obs, running, ws_ok_fn  # noqa: E402

from neurobot.ui import gateway, live  # noqa: E402

UI = "neurobot-ui.service"


def ui_obs(pubs):
    o = obs()
    o["ui"] = {"node": "ui_gateway", "pubs": pubs}
    return o


def ui_cfg(**extra) -> Config:
    values = {"NEUROBOT_COMPONENTS": "safety executor perception rosbridge mcp picoclaw ui",
              "NEUROBOT_DRIVERS": "urg", "NEUROBOT_DRIVER_urg_TOPIC": "/scan",
              "NEUROBOT_SENSORS": "/odom:nav_msgs/msg/Odometry:10",
              "NEUROBOT_UI_CMD": "%s -m neurobot.ui live" % sys.executable, "NEUROBOT_UI_MODULES": "json",
              "NEUROBOT_ROS_SETUP": "/nonexistent/setup.bash"}
    values.update(extra)
    return Config(values, "/etc/neurobot/neurobot.env")


class PublishersTest(unittest.TestCase):
    def test_one_list_for_gateway_and_checks(self):
        self.assertEqual(checks.UI_PUBLISHERS, live.PUBLISHERS)
        self.assertEqual(checks.ROS_INFRA, live.ROS_INFRA)
        self.assertEqual(checks.UI_SOURCES, ("робот", "двойник"))

    def test_ui_publishers(self):
        cfg = ui_cfg()
        self.assertEqual(checks.ui_problems(cfg, ["/neurobot/estop", "/neurobot/stop_now", "/parameter_events"]), [])
        self.assertEqual(checks.ui_problems(cfg, []), [])
        (p,) = checks.ui_problems(cfg, ["/neurobot/stop_now", "/cmd_vel"])
        self.assertEqual((p.code, p.fatal), ("ui_publisher", True))
        self.assertIn("/cmd_vel", p.text)
        (p,) = checks.ui_problems(cfg, ["/neurobot/task"])          # задачи — после исполнителя v2, не в П2
        self.assertEqual(p.code, "ui_publisher")
        (p,) = checks.ui_problems(cfg, None)
        self.assertEqual(p.code, "node_missing")


class PrecheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = os.path.join(self.tmp.name, "proc")
        os.makedirs(os.path.join(self.proc, "net"))
        for name in ("tcp", "tcp6"):
            with open(os.path.join(self.proc, "net", name), "w") as f:
                f.write("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout "
                        "inode\n")

    def tearDown(self):
        self.tmp.cleanup()

    def codes(self, cfg, state=""):
        return [(p.code, p.fatal) for p in checks.precheck("ui", cfg, proc=self.proc, state=state)]

    def test_good(self):
        state = os.path.join(self.tmp.name, "state")
        os.makedirs(state, mode=0o700)
        os.chmod(state, 0o700)
        self.assertEqual(self.codes(ui_cfg(), state), [])

    def test_refusals(self):
        cases = [({"NEUROBOT_UI_CMD": ""}, "env_invalid"),
                 ({"NEUROBOT_UI_NODE": "neurobot_ui"}, "config_invalid"),     # проверки писателей пропустили бы
                 ({"NEUROBOT_UI_NODE": "safety_gate"}, "config_invalid"),
                 ({"NEUROBOT_UI_USER": "jetson"}, "config_invalid"),           # агент прочитал бы токен
                 ({"NEUROBOT_UI_USER": "root"}, "config_invalid"),
                 ({"NEUROBOT_UI_SOURCE": "стенд"}, "config_invalid"),          # подпись — робот или двойник
                 ({"NEUROBOT_UI_PORT": "9090"}, "config_invalid"),             # порт rosbridge
                 ({"NEUROBOT_UI_BUNDLE": "/nope.json.gz"}, "config_invalid"),
                 ({"NEUROBOT_UI_MODULES": "no_such_module_xyz"}, "python_env"),
                 # safety_gate слушает другую тему — СТОП и защёлка пульта ушли бы в пустоту
                 ({"NEUROBOT_STOP_TOPIC": "/robot/stop_now"}, "config_invalid"),
                 ({"NEUROBOT_ESTOP_TOPIC": "/robot/estop"}, "config_invalid"),
                 ({"NEUROBOT_STOP_TOPIC": ""}, "config_invalid")]
        for extra, code in cases:
            with self.subTest(extra=extra):
                self.assertIn((code, True), self.codes(ui_cfg(**extra)))

    def test_open_state_dir_and_ros_args(self):
        state = os.path.join(self.tmp.name, "open")
        os.makedirs(state)
        os.chmod(state, 0o755)
        self.assertIn(("config_invalid", True), self.codes(ui_cfg(), state))
        cfg = ui_cfg(NEUROBOT_UI_CMD="%s -m neurobot.ui live --ros-args -r /neurobot/stop_now:=/cmd_vel"
                     % sys.executable)
        self.assertEqual(self.codes(cfg), [("config_invalid", False)])   # не действуют: rclpy.init(args=[])


class UiUrlTest(unittest.TestCase):
    def test_code_from_cli_is_redeemed_once_by_gateway(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "ui")
            auth = gateway.Auth(state)        # шлюз создаёт каталог, токен и codes/
            cfg = ui_cfg(NEUROBOT_UI_STATE_DIR=state, NEUROBOT_UI_PORT="18120")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    mock.patch.object(os, "fchown") as fchown:
                self.assertEqual(uiurl.run(cfg, 120.0, 0), 0)
            st = os.stat(state)
            self.assertEqual(fchown.call_args[0][1:], (st.st_uid, st.st_gid))   # владелец — пользователь пульта
            url = out.getvalue().strip()
            self.assertTrue(url.startswith("http://127.0.0.1:18120/login?code="))
            code = url.split("code=", 1)[1]
            self.assertNotIn(auth.token, url + err.getvalue())                 # токен в ссылку не попадает
            self.assertIn("-L 18120:127.0.0.1:18120", err.getvalue())
            self.assertNotIn(code, "".join(os.listdir(os.path.join(state, "codes"))))  # в имени файла — хеш кода
            self.assertTrue(auth.redeem(code))
            self.assertFalse(auth.redeem(code))                                # одноразовая
            # просроченная
            uiurl.write_code(state, "old", 10.0, now=1000.0)
            self.assertFalse(auth.redeem("old"))
            self.assertEqual(uiurl.SESSION_H * 3600, gateway.SESSION_TTL_S)
            self.assertIn("ServerAliveInterval=5", err.getvalue())

    def test_root_never_follows_links_in_console_dir(self):
        # взломанный шлюз (владелец каталога) подменяет codes/ ссылкой: root не должен создать файл по ней
        with tempfile.TemporaryDirectory() as tmp:
            state, elsewhere = os.path.join(tmp, "ui"), os.path.join(tmp, "etc")
            gateway.Auth(state)
            os.makedirs(elsewhere)
            codes = os.path.join(state, "codes")
            os.rmdir(codes)
            os.symlink(elsewhere, codes)
            with self.assertRaises(OSError):
                uiurl.write_code(state, "c", 60.0, now=1000.0, chown=True)
            self.assertEqual(os.listdir(elsewhere), [])
            cfg = ui_cfg(NEUROBOT_UI_STATE_DIR=state)
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                self.assertEqual(uiurl.run(cfg, 120.0, 0), 1)
            self.assertIn(": ui: ui_codes_unsafe:", err.getvalue())
            # каталог состояния — ссылка: тоже нет
            os.unlink(codes)
            os.makedirs(codes, mode=0o700)
            link = os.path.join(tmp, "ui-link")
            os.symlink(state, link)
            with self.assertRaises(OSError):
                uiurl.write_code(link, "c", 60.0, now=1000.0)
            # codes/ открыт на запись группе — не наш каталог
            os.chmod(codes, 0o770)
            with self.assertRaises(uiurl.UnsafeDir):
                uiurl.write_code(state, "c", 60.0, now=1000.0)
            os.chmod(codes, 0o700)
            path = uiurl.write_code(state, "c", 60.0, now=1000.0)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):              # тот же код дважды — O_EXCL
                uiurl.write_code(state, "c", 60.0, now=1000.0)

    def test_only_root_and_only_with_ui(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(uiurl.run(ui_cfg(), 120.0, 1000), 1)
            self.assertEqual(uiurl.run(ui_cfg(NEUROBOT_COMPONENTS="safety"), 120.0, 0), 1)
            self.assertEqual(uiurl.run(ui_cfg(NEUROBOT_UI_STATE_DIR="/nonexistent/ui"), 120.0, 0), 1)
        text = err.getvalue()
        for code in ("ui_denied", "not_installed", "not_running"):
            self.assertIn(": ui: %s:" % code, text)


class WaitGraphTest(unittest.TestCase):
    def run_cli(self, pubs):
        err = io.StringIO()
        with mock.patch("neurobot_deploy.rosgraph.node_publishers", return_value=pubs), \
                contextlib.redirect_stderr(err):
            rc = cli.cmd_wait_graph(ui_cfg(), mock.Mock(component="ui"))
        return rc, err.getvalue()

    def test_ready_only_with_listed_publishers(self):
        rc, text = self.run_cli(["/neurobot/estop", "/neurobot/stop_now", "/parameter_events"])
        self.assertEqual(rc, 0)
        self.assertIn("пишет только в /neurobot/estop, /neurobot/stop_now", text)
        rc, text = self.run_cli(["/neurobot/stop_now", "/cmd_vel"])
        self.assertEqual(rc, 1)
        self.assertIn("ui_publisher", text)
        rc, text = self.run_cli(None)
        self.assertEqual(rc, 1)
        self.assertIn("node_missing", text)


class JournalWithoutRightsTest(unittest.TestCase):
    def test_no_journal_access_is_empty_not_failure(self):
        # пульт (и doctor без sudo) — не в группе systemd-journal: journalctl 255 выходит с кодом 1, doctor — дальше
        import types
        from neurobot_deploy.sd import Systemd, SystemdError

        def run(args, **kw):
            return types.SimpleNamespace(returncode=1, stdout="",
                                         stderr="No journal files were opened due to insufficient permissions.")
        self.assertEqual(Systemd(run=run).journal(unit="neurobot-safety.service"), [])

        def broken(args, **kw):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="Failed to parse timestamp")
        with self.assertRaises(SystemdError):
            Systemd(run=broken).journal(unit="neurobot-safety.service")


class DoctorUiTest(unittest.TestCase):
    def diagnose(self, ui_props, observation=None, get=None):
        props = dict(healthy(), **{UI: ui_props})
        return doctor.diagnose(ui_cfg(), FakeSd(props, {"a": AGENT_OK}), observe=lambda c, s: observation or ui_obs(
            ["/neurobot/estop", "/neurobot/stop_now", "/parameter_events"]),
            probe=mcp_ok, ws=ws_ok_fn, get=get or (lambda url, t: (200, '{"ok": true}')), now_mono=NOW,
            now_real=WALL)

    def test_ok_line_last(self):
        rep = self.diagnose(running("u", 10))
        self.assertEqual((rep.exit_code, rep.root), (0, None))
        f = rep.findings[-1]
        self.assertEqual((f.component, f.level), ("ui", doctor.OK))
        self.assertIn("пишет только в /neurobot/stop_now, /neurobot/estop", f.text)

    def test_failed_or_starting_console_is_only_a_warning(self):
        for props in (failed("u2"), dict(failed("u3", result="success", sub="start"), NRestarts="0"),
                      {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "Result": "success"}):
            with self.subTest(props=props.get("SubState")):
                rep = self.diagnose(props)
                self.assertEqual((rep.exit_code, rep.root), (0, None))      # робот работает и без пульта
                f = rep.findings[-1]
                self.assertEqual((f.component, f.level), ("ui", doctor.WARN))
                self.assertIn("пульт (роботу не нужен)", f.text)

    def test_not_answering(self):
        def refuse(url, t):
            raise ProbeError("connection refused")
        f = self.diagnose(running("u", 10), get=refuse).findings[-1]
        self.assertEqual((f.level, f.code), (doctor.WARN, "ui_down"))

    def test_extra_publisher_is_error(self):
        o = ui_obs(["/neurobot/stop_now", "/cmd_vel"])
        rep = self.diagnose(running("u", 10), observation=o)
        self.assertEqual((rep.exit_code, rep.root.component, rep.root.code), (1, "ui", "ui_publisher"))


if __name__ == "__main__":
    unittest.main()
