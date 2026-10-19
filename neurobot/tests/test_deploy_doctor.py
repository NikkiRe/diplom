"""neurobot doctor и сторож на подставном systemd: состояния юнитов, причина из журнала, корневой компонент,
прогресс по ROS, соединения; решения сторожа. Только stdlib, Python 3.8+."""
from __future__ import annotations

import contextlib
import io
import json
import math
import sys
import types
import unittest
from pathlib import Path
from typing import Dict, List
from unittest import mock

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))

from neurobot_deploy import __main__ as cli  # noqa: E402
from neurobot_deploy import doctor, watchdog  # noqa: E402
from neurobot_deploy.config import Config  # noqa: E402
from neurobot_deploy.probes import McpProbe, ProbeError  # noqa: E402
from neurobot_deploy.sd import Entry, Systemd, timespan_s  # noqa: E402

NOW = 1000.0          # монотонные часы, с
WALL = 1_700_000_000.0
UNITS = ["neurobot-driver@urg.service", "neurobot-safety.service", "neurobot-executor.service",
         "neurobot-perception.service", "neurobot-rosbridge.service", "neurobot-mcp.service",
         "neurobot-picoclaw.service"]
SAFETY, EXECUTOR, PERCEPTION, ROSBRIDGE, MCP, AGENT = UNITS[1:]


def running(inv: str, watchdog_s: float = 0, wd_age: float = 0.1, since: float = 300.0, restarts: int = 0) -> dict:
    p = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "Result": "success",
         "NRestarts": str(restarts), "InvocationID": inv,
         "WatchdogUSec": "{:g}s".format(watchdog_s) if watchdog_s else "0",
         "ActiveEnterTimestampMonotonic": str(int((NOW - since) * 1e6))}
    if watchdog_s:
        p["WatchdogTimestampMonotonic"] = str(int((NOW - wd_age) * 1e6))
    return p


def failed(inv: str, result: str = "exit-code", status: str = "1", sub: str = "auto-restart",
           active: str = "activating") -> dict:
    return {"LoadState": "loaded", "ActiveState": active, "SubState": sub, "Result": result, "ExecMainStatus": status,
            "NRestarts": "3", "InvocationID": inv, "InactiveExitTimestampMonotonic": str(int((NOW - 2) * 1e6))}


class FakeSd(Systemd):
    def __init__(self, props: Dict[str, dict], journal: Dict[str, List[Entry]] = None, jobs=None):
        super().__init__(run=None)
        self.props, self.journals, self.job_map, self.calls = props, journal or {}, jobs or {}, []

    def show(self, units, props=()):
        return {u: dict(self.props.get(u, {"LoadState": "not-found", "ActiveState": "inactive"}), Id=u) for u in units}

    def jobs(self):
        return dict(self.job_map)

    def journal(self, invocation=None, unit=None, lines=200, since=None):
        return list(self.journals.get(invocation or unit, []))

    def ctl(self, *args):
        self.calls.append(args)


def err(text: str, ts: float = WALL - 5) -> Entry:
    return Entry(ts, 3, text, "")


def healthy() -> Dict[str, dict]:
    return {UNITS[0]: running("d"), SAFETY: running("s", 3), EXECUTOR: running("e", 3),
            PERCEPTION: running("p", 5, wd_age=0.6), ROSBRIDGE: running("r"), MCP: running("m"), AGENT: running("a")}


AGENT_OK = [Entry(WALL - 200, 6, "INF mcp > Connected to MCP server protocol=x server=robot_mcp", "a"),
            Entry(WALL - 200, 6, "INF mcp > MCP server initialization complete", "a")]
SAFE = {"state": "ok", "reasons": [], "min_clearance_m": 1.8, "limits": {"v_max": 0.25, "w_max": 0.8}}
HEALTH = {"fps": 9.8, "frame_age_s": 0.1, "recoveries": 0, "state": "ok"}


def heard(topic: str, pubs, rate: float, **extra) -> dict:
    """Наблюдение темы за OBSERVE_S, как его отдаёт rosgraph.observe."""
    return dict({"topic": topic, "pub": list(pubs), "sub": [], "count": int(rate * doctor.OBSERVE_S),
                 "rate_hz": rate, "age_s": 0.05 if rate else None, "types": []}, **extra)


def ends(topic: str, pubs, subs, types=()) -> dict:
    return {"topic": topic, "pub": list(pubs), "sub": list(subs), "types": list(types)}


def obs(**changes) -> dict:
    """Здоровый граф робота: цепочка исполнитель → safety_gate → twist_mux → diff_cont, датчики свежие; changes
    правят отдельные ключи наблюдения."""
    o = {"safety": heard("/neurobot/safety_state", ["safety_gate"], 5.0, last=dict(SAFE)),
         "motion": heard("/neurobot/motion_state", ["motion_executor"], 2.0),
         "health": heard("/neurobot/perception_health", ["predict_live"], 1.0, last=dict(HEALTH)),
         "scan": heard("/scan", ["urg_node2"], 10.0, angle_min=-2.0923497677, angle_increment=0.0061359,
                       frame_id="laser_frame", bad_frac=[0.0] * 683),
         "sensor:/odom": heard("/odom", ["diff_cont"], 50.0, types=["nav_msgs/msg/Odometry"]),
         "cmd_vel": ends("/cmd_vel", ["safety_gate"], ["twist_mux"], ["geometry_msgs/msg/Twist"]),
         "cmd_vel_req": ends("/neurobot/cmd_vel_req", ["motion_executor"], ["safety_gate"]),
         "base": ends("/diff_cont/cmd_vel_unstamped", ["twist_mux"], ["diff_cont"]),
         "override": {"/cmd_vel_tracker": ends("/cmd_vel_tracker", [], ["twist_mux"])},
         "joy": {"/cmd_vel_joy": ends("/cmd_vel_joy", [], ["twist_mux"])}}
    for key, value in changes.items():
        o[key] = dict(o[key], **value)
    return o


def cfg() -> Config:
    return Config({"NEUROBOT_DRIVERS": "urg", "NEUROBOT_DRIVER_urg_TOPIC": "/scan",
                   "NEUROBOT_SENSORS": "/odom:nav_msgs/msg/Odometry:10"}, "/etc/neurobot/neurobot.env")


def mcp_ok(url, timeout, status_tool=""):
    return McpProbe(["robot_get_status", "robot_stop", "robot_nudge"],
                    json.dumps({"connected": True}) if status_tool else None, 0.05)


class DoctorTest(unittest.TestCase):
    def diagnose(self, props, journal=None, jobs=None, observation=None, probe=mcp_ok, ws_ok=True):
        journal = dict({"a": AGENT_OK}, **(journal or {}))
        return doctor.diagnose(cfg(), FakeSd(props, journal, jobs), observe=lambda c, s: observation or obs(),
                               probe=probe, ws=ws_ok_fn if ws_ok else _refuse, now_mono=NOW, now_real=WALL)

    def test_all_ok(self):
        rep = self.diagnose(healthy())
        self.assertEqual((rep.exit_code, rep.root), (0, None))
        self.assertEqual([f.component for f in rep.findings], ["driver@urg", "sensor:/odom", "safety", "executor",
                                                               "perception", "rosbridge", "mcp", "picoclaw"])
        text = doctor.format_text(rep)
        self.assertIn("ИТОГ: ОК", text)
        self.assertIn("READY, прогресс 0.6 с назад (WatchdogSec 5 с)", text)
        self.assertIn("/scan 10.0 Гц", text)
        self.assertIn("/odom 50.0 Гц", text)
        self.assertIn("состояние ok; зазор 1.8 м; в /cmd_vel пишет только он", text)
        self.assertIn("/neurobot/motion_state 2.0 Гц; в /neurobot/cmd_vel_req пишет только он", text)
        self.assertIn("камера ok: 9.8 к/с", text)
        self.assertIn("MCP подключён (robot_mcp)", text)
        self.assertEqual(json.loads(json.dumps(rep.as_dict()))["ok"], True)

    def test_without_ros_only_units_and_connections(self):
        rep = doctor.diagnose(cfg(), FakeSd(healthy(), {"a": AGENT_OK}), observe=None, probe=mcp_ok, ws=ws_ok_fn,
                              now_mono=NOW, now_real=WALL)
        self.assertEqual(rep.exit_code, 0)
        self.assertNotIn("sensor:/odom", [f.component for f in rep.findings])

    def test_precheck_reason_names_component_and_runbook(self):
        props = healthy()
        props[PERCEPTION] = failed("p2")
        line = ("neurobot-precheck: perception: camera_missing: камера 8086:0b3a не найдена на USB "
                "(/sys/bus/usb/devices)")
        rep = self.diagnose(props, {"p2": [Entry(WALL - 3, 6, "neurobot-run: x", "p2"), err(line)]})
        self.assertEqual((rep.exit_code, rep.root.component, rep.root.code), (1, "perception", "camera_missing"))
        d = rep.as_dict()["root"]
        self.assertIn("RUNBOOK.md §4.1", d["hint"])
        self.assertIn("рестартов 3", d["text"])

    def test_reason_found_while_precheck_backs_off(self):
        props = healthy()
        props[PERCEPTION] = failed("p3", result="success", sub="start-pre")
        line = "neurobot-precheck: perception: weights_missing: нет файла весов /m/a.pt"
        rep = self.diagnose(props, {"p3": [err(line)]})
        self.assertEqual((rep.root.component, rep.root.code), ("perception", "weights_missing"))

    def test_first_failed_component_is_root_cause(self):
        props = healthy()
        props[ROSBRIDGE] = failed("r2", active="failed", sub="failed")
        props[AGENT] = running("a2")
        rep = self.diagnose(props, {
            "r2": [err("neurobot-wait: rosbridge: port_timeout: порт 9090 не ответил WebSocket за 30 с: refused")],
            "a2": [Entry(WALL - 100, 3, "ERR mcp > All MCP servers failed to connect", "a2")]})
        self.assertEqual((rep.root.component, rep.root.code), ("rosbridge", "port_timeout"))
        self.assertEqual([f.code for f in rep.findings if f.level == doctor.FAIL], ["port_timeout", "agent_zombie"])

    def test_result_without_our_line(self):
        props = healthy()
        props[EXECUTOR] = failed("e2", result="watchdog", status="6")
        props[MCP] = failed("m2", result="exit-code", status="1")
        rep = self.diagnose(props)
        self.assertEqual((rep.root.component, rep.root.code), ("executor", "watchdog"))
        mcp = next(f for f in rep.findings if f.component == "mcp")
        self.assertEqual((mcp.code, mcp.text), ("crashed", "завершился: exit-code, код 1, рестартов 3"))

    def test_crash_reason_quotes_last_error_line(self):
        props = healthy()
        props[EXECUTOR] = failed("e3")
        journal = {"e3": [Entry(WALL - 3, 6, "Traceback (most recent call last):", "e3"),
                          Entry(WALL - 3, 6, "rclpy._rclpy_pybind11.RCLError: failed to initialize logging: "
                                             "Failed to create log directory '/home/neurobot/.ros/log': 13", "e3"),
                          Entry(WALL - 2, 6, "neurobot-executor.service: Failed with result 'exit-code'.", "e3",
                                False)]}
        root = self.diagnose(props, journal).root
        self.assertEqual(root.code, "crashed")
        self.assertIn("; в журнале: rclpy._rclpy_pybind11.RCLError: failed to initialize logging", root.text)
        self.assertLessEqual(len(root.text.split("в журнале: ")[1]), 160)

    def test_restart_loop_names_reason_of_failed_run(self):
        props = healthy()
        props[ROSBRIDGE] = failed("r5", result="success", sub="start-post")  # новый запуск ждёт порт
        unit_log = [Entry(WALL - 3, 6, "neurobot-rosbridge.service: Failed with result 'exit-code'.", "r4", False)]
        reason = err("neurobot-wait: rosbridge: port_timeout: порт 9090 не ответил WebSocket за 5 с: refused")
        rep = self.diagnose(props, {ROSBRIDGE: unit_log, "r4": [reason._replace(invocation="r4")], "r5": []})
        self.assertEqual((rep.root.component, rep.root.code), ("rosbridge", "port_timeout"))
        self.assertIn("перезапускается, рестартов 3 (последний отказ: exit-code 3 с назад)", rep.root.text)
        old = [e._replace(ts=WALL - 400) for e in unit_log]  # давний отказ: юнит просто запускается
        rep = self.diagnose(props, {ROSBRIDGE: old, "r4": [reason], "r5": []})
        self.assertEqual((rep.root.component, rep.root.code), ("rosbridge", "starting"))

    def test_restart_loop_without_reason_line_is_not_starting(self):
        props = healthy()
        props[SAFETY] = failed("s5", result="success", sub="start")  # сервис не дошёл до READY и запускается снова
        unit_log = [Entry(WALL - 20, 6, "neurobot-safety.service: Failed with result 'timeout'.", "s4", False)]
        rep = self.diagnose(props, {SAFETY: unit_log, "s4": [], "s5": []})
        self.assertEqual((rep.exit_code, rep.root.component, rep.root.code), (1, "safety", "start_timeout"))
        self.assertIn("не стал готов за TimeoutStartSec; перезапускается, рестартов 3", rep.root.text)
        unit_log = [Entry(WALL - 20, 6, "neurobot-safety.service: Failed with result 'exit-code'.", "s4", False)]
        rep = self.diagnose(props, {SAFETY: unit_log, "s4": [], "s5": []})
        self.assertEqual((rep.root.code, rep.root.text.split(";")[0]), ("crashed", "упал (exit-code)"))

    def test_killed_unit_does_not_quote_our_info_lines(self):
        props = healthy()
        props[MCP] = failed("m4", result="signal", status="9")
        ready = "neurobot-wait: mcp: ready: http://127.0.0.1:18110/mcp: robot_stop"
        journal = {"m4": [Entry(WALL - 10, 6, "neurobot-run: /usr/bin/python3 x.py", "m4"),
                          Entry(WALL - 9, 6, ready, "m4")]}
        root = self.diagnose(props, journal).root
        self.assertEqual((root.code, root.text), ("crashed", "убит сигналом 9, рестартов 3"))

    def test_restarted_unit_shows_last_failure_from_journal(self):
        props = healthy()
        props[EXECUTOR] = running("e", 3, restarts=2)
        unit_log = [Entry(WALL - 40, 6, "neurobot-executor.service: Failed with result 'signal'.", "", False),
                    Entry(WALL - 12, 4, "neurobot-executor.service: Watchdog timeout (limit 3s)!", "", False),
                    Entry(WALL - 12, 6, "neurobot-executor.service: Failed with result 'watchdog'.", "", False),
                    Entry(WALL - 11, 6, "Failed with result 'нет' — строка процесса", "")]
        rep = self.diagnose(props, {EXECUTOR: unit_log})
        self.assertEqual(rep.exit_code, 0)
        self.assertIn("рестартов 2 (последний отказ: watchdog 12 с назад)",
                      next(f.text for f in rep.findings if f.component == "executor"))

    def test_stale_watchdog_on_active_unit(self):
        props = healthy()
        props[EXECUTOR] = running("e", 3, wd_age=4.2)
        self.assertEqual(self.diagnose(props).root.code, "watchdog")

    def test_starting_and_waiting_jobs(self):
        props = healthy()
        props[PERCEPTION] = failed("p", result="success", sub="start")
        for u in UNITS[4:]:
            props[u] = {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "Result": "success"}
        rep = self.diagnose(props, jobs={u: "start" for u in UNITS[4:]})
        self.assertEqual((rep.exit_code, rep.root.component), (3, "perception"))
        self.assertIn("ждёт запуска", next(f.text for f in rep.findings if f.component == "rosbridge"))
        self.assertIn("ИТОГ: ещё поднимается — perception", doctor.format_text(rep))

    def test_not_installed_and_not_running(self):
        props = healthy()
        del props[AGENT]
        props[MCP] = {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "Result": "success"}
        rep = self.diagnose(props)
        self.assertEqual([(f.component, f.code) for f in rep.findings if f.level == doctor.FAIL],
                         [("mcp", "not_running"), ("picoclaw", "not_installed")])

    def test_ros_progress_problems(self):
        estop = dict(SAFE, state="estop", reasons=["estop: human"])
        cases = [(obs(scan={"age_s": None, "count": 0}), "driver@urg", "scan_stale"),
                 (obs(scan={"age_s": 0.8}), "driver@urg", "scan_stale"),
                 (obs(scan={"pub": ["urg_node2", "urg_node2"]}), "driver@urg", "scan_publishers"),
                 (obs(**{"sensor:/odom": {"pub": []}}), "sensor:/odom", "sensor_missing"),
                 (obs(**{"sensor:/odom": {"types": ["geometry_msgs/msg/Twist"]}}), "sensor:/odom", "config_invalid"),
                 (obs(**{"sensor:/odom": {"count": 0, "rate_hz": 0.0}}), "sensor:/odom", "sensor_stale"),
                 (obs(**{"sensor:/odom": {"rate_hz": 4.0}}), "sensor:/odom", "sensor_stale"),
                 (obs(safety={"last": None}), "safety", "safety_silent"),
                 (obs(safety={"rate_hz": 1.0}), "safety", "safety_silent"),
                 (obs(safety={"last": {"state": "bad_json"}}), "safety", "safety_silent"),
                 (obs(safety={"last": estop}), "safety", "estop_engaged"),
                 (obs(cmd_vel={"types": ["geometry_msgs/msg/TwistStamped"]}), "safety", "config_invalid"),
                 (obs(cmd_vel={"pub": ["safety_gate", "predict_live"]}), "safety", "cmd_vel_writer"),
                 (obs(base={"pub": ["twist_mux", "teleop_node"]}), "safety", "cmd_vel_bypass"),
                 (obs(motion={"count": 0, "rate_hz": 0.0}), "executor", "executor_silent"),
                 (obs(cmd_vel_req={"sub": []}), "executor", "gate_missing"),
                 (obs(cmd_vel_req={"pub": ["motion_executor", "teleop_node"]}), "executor", "cmd_vel_writer"),
                 (obs(health={"last": dict(HEALTH, state="recovering", fps=0.0)}), "perception", "perception_stalled"),
                 (obs(health={"last": {}}), "perception", "perception_silent")]
        for observation, comp, code in cases:
            with self.subTest(code=code, comp=comp):
                rep = self.diagnose(healthy(), observation=observation)
                self.assertEqual((rep.exit_code, rep.root.component, rep.root.code), (1, comp, code))
        rep = self.diagnose(healthy(), observation=obs(safety={"last": estop}))
        self.assertIn("причины: estop: human", rep.root.text)
        self.assertIn("sudo neurobot estop release (RUNBOOK.md §4.14)", rep.as_dict()["root"]["hint"])
        rep = self.diagnose(healthy(), observation=obs(health={"last": dict(HEALTH, state="starting")}))
        self.assertEqual((rep.exit_code, rep.root.component), (3, "perception"))

    def test_silent_sensor_while_starting_is_not_the_root(self):
        # приёмка: на старте молчит глубина (у двойника — нет подписчика), а doctor называл первой sensor_stale вместо
        # настоящей причины; пока юнит поднимается, датчик — «ещё поднимается»
        props = healthy()
        props[SAFETY] = failed("s", result="success", sub="start")
        late = obs(**{"sensor:/odom": {"count": 0, "rate_hz": 0.0}}, scan={"age_s": None, "count": 0})
        rep = self.diagnose(props, observation=late)
        self.assertEqual((rep.exit_code, rep.root.component), (3, "driver@urg"))
        self.assertIn("ждём датчик, система ещё поднимается", rep.root.text)
        odom = next(f for f in rep.findings if f.component == "sensor:/odom")
        self.assertEqual((odom.level, odom.code), (doctor.STARTING, "starting"))
        props[SAFETY] = failed("s", result="timeout", sub="auto-restart")  # safety_gate не стал готов — он и причина
        journal = {"s": [Entry(WALL - 3, 3, "neurobot-wait: safety: start_timeout: нет READY за 30 с", "s")]}
        props[ROSBRIDGE] = failed("r", result="success", sub="start")
        rep = self.diagnose(props, journal=journal, observation=late)
        self.assertEqual((rep.exit_code, rep.root.component), (1, "safety"))
        rep = self.diagnose(healthy(), observation=late)  # всё поднято — молчащий датчик и есть ошибка
        self.assertEqual((rep.root.component, rep.root.code), ("driver@urg", "scan_stale"))

    def test_sensor_is_root_cause_before_safety_block(self):
        blocked = dict(SAFE, state="blocked", reasons=["scan_stale"])
        rep = self.diagnose(healthy(), observation=obs(scan={"age_s": None, "count": 0}, safety={"last": blocked}))
        self.assertEqual((rep.root.component, rep.root.code), ("driver@urg", "scan_stale"))
        rep = self.diagnose(healthy(), observation=obs(safety={"last": blocked}))
        self.assertEqual(rep.exit_code, 0)  # блок — работа safety_gate, а не отказ: предупреждение с причинами
        f = next(f for f in rep.findings if f.component == "safety")
        self.assertEqual((f.level, f.code), (doctor.WARN, "safety_blocked"))
        self.assertIn("причины: scan_stale", f.text)

    def test_scan_without_driver_unit_is_own_line(self):
        c = Config({}, "/e/n.env")
        props = {u: p for u, p in healthy().items() if "driver" not in u}
        rep = doctor.diagnose(c, FakeSd(props, {"a": AGENT_OK}), observe=lambda *_: obs(scan={"age_s": 0.9}),
                              probe=mcp_ok, ws=ws_ok_fn, now_mono=NOW, now_real=WALL)
        self.assertEqual((rep.findings[0].component, rep.root.code), ("scan", "scan_stale"))

    def test_ros_unavailable_is_failure_not_ok(self):
        # компоненты ROS живы, а граф не снять: датчики, защёлка и писатели /cmd_vel не проверены — не «ОК» (start_test
        # иначе засчитал бы цикл без проверки безопасности)
        def broken(*_):
            raise RuntimeError("rclpy недоступен")
        rep = doctor.diagnose(cfg(), FakeSd(healthy(), {"a": AGENT_OK}), observe=broken, probe=mcp_ok,
                              ws=ws_ok_fn, now_mono=NOW, now_real=WALL)
        self.assertEqual((rep.exit_code, rep.root.component, rep.root.code), (1, "ros", "ros_unavailable"))
        self.assertEqual([f.component for f in rep.findings][:2], ["driver@urg", "ros"])  # после драйверов
        self.assertIn("писатели /cmd_vel не проверены: rclpy недоступен", rep.root.text)
        props = {u: p for u, p in healthy().items() if u in (MCP, AGENT)}  # без компонентов ROS граф не нужен
        c = Config({"NEUROBOT_COMPONENTS": "mcp picoclaw"}, "/e/n.env")
        rep = doctor.diagnose(c, FakeSd(props, {"a": AGENT_OK}), observe=broken, probe=mcp_ok, ws=ws_ok_fn,
                              now_mono=NOW, now_real=WALL)
        self.assertEqual(rep.exit_code, 0)

    def test_busy_camera_exit_code(self):
        props = healthy()
        props[PERCEPTION] = failed("p6", status="6", sub="failed", active="failed")
        line = Entry(WALL - 3, 6, "конвейер камеры не запустился: камера занята другим процессом (держат: pid 40)",
                     "p6")
        rep = self.diagnose(props, {"p6": [line]})
        self.assertEqual((rep.root.component, rep.root.code), ("perception", "camera_busy"))
        self.assertIn("RestartPreventExitStatus=6", rep.root.text)
        self.assertIn("в журнале: конвейер камеры не запустился", rep.root.text)
        props[PERCEPTION] = failed("p7", status="5", sub="failed", active="failed")
        self.assertEqual(self.diagnose(props).root.code, "crashed")

    def test_connections(self):
        rep = self.diagnose(healthy(), ws_ok=False)
        self.assertEqual((rep.root.component, rep.root.code), ("rosbridge", "rosbridge_down"))

        def no_bridge(url, timeout, status_tool=""):
            return McpProbe(["robot_get_status"], json.dumps({"connected": False, "error": "refused"}), 0.1)
        self.assertEqual(self.diagnose(healthy(), probe=no_bridge).root.code, "mcp_no_rosbridge")

        def down(*_):
            raise ProbeError("connection refused")
        self.assertEqual(self.diagnose(healthy(), probe=down).root.code, "mcp_down")

    def test_lab_worker(self):
        c = Config({"NEUROBOT_LABWORKERS": "vision", "NEUROBOT_LABWORKER_vision_URL": "http://127.0.0.1:8766/state"},
                   "/e/n.env")
        props = {u: p for u, p in healthy().items() if "driver" not in u}
        props["neurobot-labworker@vision.service"] = running("w")

        def run(get):
            return doctor.diagnose(c, FakeSd(props, {"a": AGENT_OK}), observe=lambda *_: obs(), probe=mcp_ok,
                                   ws=ws_ok_fn, get=get, now_mono=NOW, now_real=WALL)
        rep = run(lambda url, timeout: (200, "{}"))
        self.assertEqual(rep.exit_code, 0)
        self.assertIn("http://127.0.0.1:8766/state отвечает 200", doctor.format_text(rep))
        rep = run(lambda url, timeout: (503, '{"state": "loading"}'))
        self.assertEqual((rep.root.component, rep.root.code), ("labworker@vision", "labworker_down"))
        self.assertIn("HTTP 503", rep.root.text)

        def refused(url, timeout):
            raise ProbeError(url + ": Connection refused")
        rep = run(refused)
        self.assertIn("не отвечает: http://127.0.0.1:8766/state: Connection refused", rep.root.text)
        self.assertIn("RUNBOOK.md §4.13", rep.as_dict()["root"]["hint"])

    def test_mcp_tool_units_and_worker_state(self):
        def mixed(url, timeout, status_tool=""):  # прокси лаборатории без патча: остались первые регистрации
            return McpProbe(["robot_nudge_forward", "robot_nudge"], None, 0.1,
                            {"robot_nudge_forward": ["duration_ms", "speed"], "robot_nudge": ["direction", "duration_s",
                                                                                              "speed"]})
        root = self.diagnose(healthy(), probe=mixed).root
        self.assertEqual(root.code, "mcp_schema")
        self.assertIn("robot_nudge_forward — duration_ms, robot_nudge — duration_s", root.text)

        def worker_down(url, timeout, status_tool=""):
            return McpProbe(["robot_demo_get_status"], json.dumps({"ok": False, "error": "robot vision worker is not "
                                                                   "available: Connection refused"}), 0.1)
        self.assertEqual(self.diagnose(healthy(), probe=worker_down).root.code, "mcp_worker_down")

    def test_twist_mux_inputs_above_safety_gate(self):
        tracker = {"/cmd_vel_tracker": ends("/cmd_vel_tracker", ["tracker_node"], ["twist_mux"])}
        rep = self.diagnose(healthy(), observation=obs(override=tracker))
        self.assertEqual((rep.root.component, rep.root.code), ("safety", "cmd_vel_override"))
        self.assertIn("/cmd_vel_tracker публикует tracker_node", rep.root.text)
        joy = {"/cmd_vel_joy": ends("/cmd_vel_joy", ["teleop_twist_joy_node"], ["twist_mux"])}
        rep = self.diagnose(healthy(), observation=obs(joy=joy))
        self.assertEqual(rep.exit_code, 0)
        self.assertEqual((rep.findings[-1].component, rep.findings[-1].level, rep.findings[-1].code),
                         ("twist_mux", doctor.WARN, "cmd_vel_joy"))
        self.assertIn("ИТОГ: ОК (предупреждений: 1)", doctor.format_text(rep))
        rep = self.diagnose(healthy(), observation=obs(cmd_vel={"sub": ["diff_cont"]}))
        f = next(f for f in rep.findings if f.component == "safety")
        self.assertEqual((rep.exit_code, f.level, f.code), (0, doctor.WARN, "twist_mux_missing"))

    def test_scan_frame_and_blind_sector(self):
        rep = self.diagnose(healthy(), observation=obs(scan={"frame_id": "base_scan"}))
        f = next(f for f in rep.findings if f.component == "driver@urg")
        self.assertEqual((rep.exit_code, f.level, f.code), (0, doctor.WARN, "scan_frame"))
        blind = [0.0] * 168 + [1.0] * 186 + [0.0] * 329  # скан робота 02.06: NaN в лучах 168–353 (RS3)
        rep = self.diagnose(healthy(), observation=obs(scan={"bad_frac": blind}))
        self.assertEqual(rep.exit_code, 0)
        f = next(f for f in rep.findings if f.component == "driver@urg")
        self.assertEqual((f.level, f.code), (doctor.WARN, "scan_blind"))
        self.assertIn("сектор -61…+4°", f.text)

    def test_agent_log_states(self):
        hung = AGENT_OK + [Entry(WALL - 75, 6,
                                 "INF tool > Tool execution started args={} tool=mcp_robot_mcp_robot_nudge",
                                 "a")]
        self.assertEqual(self.diagnose(healthy(), {"a": hung}).root.code, "agent_hung")
        self.assertEqual(self.diagnose(healthy(), {"a": AGENT_OK[1:]}).root.code, "agent_timeout")
        rep = self.diagnose(healthy(), {"a": []})
        self.assertEqual((rep.exit_code, rep.findings[-1].level), (0, doctor.WARN))
        deaf = AGENT_OK + [Entry(WALL - 30, 3, 'ERR vk > Long poll failed error="Post \\"https://lp.vk.com/x?key=s3cr3t'
                                               '&ts=1\\": context deadline exceeded"', "a")]
        root = self.diagnose(healthy(), {"a": deaf}).root
        self.assertEqual(root.code, "agent_channel_lost")
        self.assertNotIn("s3cr3t", root.text)


class BlindSectorTest(unittest.TestCase):
    def test_urg_and_turtlebot_layouts(self):
        urg = {"angle_min": -2.0923497677, "angle_increment": 0.0061359,
               "bad_frac": [0.0] * 168 + [0.95] * 186 + [0.0] * 329}
        self.assertEqual(doctor.blind_sectors(urg), [(-60.8, 4.2)])
        urg["bad_frac"] = [0.0] * 168 + [0.5] * 186 + [0.0] * 329  # мигающие лучи — не закрытый сектор
        self.assertEqual(doctor.blind_sectors(urg), [])
        urg["bad_frac"] = [1.0] * 60 + [0.0] * 623  # сзади слева (за пределами ±61°) — не наше дело
        self.assertEqual(doctor.blind_sectors(urg), [])
        tb3 = {"angle_min": 0.0, "angle_increment": math.radians(1.0), "bad_frac": [1.0] * 8 + [0.0] * 344 + [1.0] * 8}
        self.assertEqual(doctor.blind_sectors(tb3), [(-8.0, 7.0)])  # через 0/360 — один сектор
        self.assertEqual(doctor.blind_sectors({"bad_frac": [], "angle_min": 0.0, "angle_increment": 0.0}), [])


def ws_ok_fn(*a, **k):
    return None


def _refuse(*a, **k):
    raise ProbeError("refused")


class TimespanTest(unittest.TestCase):
    def test_systemctl_show_formats(self):
        cases = {"3s": 3.0, "500ms": 0.5, "1min 30s": 90.0, "5000000": 5.0, "infinity": 0.0, "": 0.0}
        for text, want in cases.items():
            self.assertAlmostEqual(timespan_s(text), want)


class WatchdogTest(unittest.TestCase):
    def decide(self, props, journal=None, probe=mcp_ok):
        sd = FakeSd(props, journal or {"a": AGENT_OK})
        return [(a.unit, a.code) for a in watchdog.decide(cfg(), sd, probe, now_mono=NOW, now_real=WALL)]

    def test_quiet_when_healthy(self):
        self.assertEqual(self.decide(healthy()), [])

    def test_mcp_needs_two_failed_probes(self):
        attempts = []

        def flaky(url, timeout, tool=""):
            attempts.append(timeout)
            if len(attempts) == 1:
                raise ProbeError("нет ответа за отведённое время")
            return mcp_ok(url, timeout)
        self.assertEqual(self.decide(healthy(), probe=flaky), [])
        self.assertEqual(attempts, [3.0, 3.0])

        def dead(*_a, **_k):
            raise ProbeError("нет ответа за отведённое время")
        self.assertEqual(self.decide(healthy(), probe=dead), [("neurobot-mcp.service", "mcp_down")])

    def test_hung_tool_call_restarts_mcp_then_agent(self):
        def started(age):
            return AGENT_OK + [Entry(WALL - age, 6, "Tool execution started args={} tool=mcp_robot_mcp_robot_nudge",
                                     "a")]
        self.assertEqual(self.decide(healthy(), {"a": started(30)}), [])
        self.assertEqual(self.decide(healthy(), {"a": started(70)}), [("neurobot-mcp.service", "agent_hung")])
        props = healthy()
        props["neurobot-mcp.service"] = running("m", since=40)  # MCP уже перезапущен недавно
        self.assertEqual(self.decide(props, {"a": started(95)}), [("neurobot-picoclaw.service", "agent_hung")])

    def test_zombie_restart_waits_for_mcp_and_age(self):
        zombie = [Entry(WALL - 25, 3, "ERR mcp > All MCP servers failed to connect", "a")]
        self.assertEqual(self.decide(healthy(), {"a": zombie}), [("neurobot-picoclaw.service", "agent_zombie")])
        fresh = [Entry(WALL - 5, 3, "ERR mcp > All MCP servers failed to connect", "a")]
        self.assertEqual(self.decide(healthy(), {"a": fresh}), [])

        def dead(*_a, **_k):
            raise ProbeError("refused")
        self.assertEqual(self.decide(healthy(), {"a": zombie}, probe=dead), [("neurobot-mcp.service", "mcp_down")])

    def test_deaf_channel_restarts_agent(self):
        deaf = AGENT_OK + [Entry(WALL - 25, 3, 'ERR vk > Long poll failed error="context deadline exceeded"', "a")]
        self.assertEqual(self.decide(healthy(), {"a": deaf}), [("neurobot-picoclaw.service", "agent_channel_lost")])
        stop = AGENT_OK + [Entry(WALL - 25, 3, 'ERR vk > Long poll failed error="context canceled"', "a")]
        self.assertEqual(self.decide(healthy(), {"a": stop}), [])

    def test_grace_after_start(self):
        props = healthy()
        props["neurobot-mcp.service"] = running("m", since=5)
        props["neurobot-picoclaw.service"] = running("a", since=5)

        def dead(*_a, **_k):
            raise ProbeError("refused")
        zombie = [Entry(WALL - 25, 3, "All MCP servers failed to connect", "a")]
        self.assertEqual(self.decide(props, {"a": zombie}, probe=dead), [])

    def test_driver_watch_from_env(self):
        c = Config({"NEUROBOT_DRIVERS": "lidar camera base imu", "NEUROBOT_DRIVER_lidar_TOPIC": "/scan",
                    "NEUROBOT_DRIVER_camera_FRESH": "/camera/color/image_raw:2.5 /camera/imu:1",
                    "NEUROBOT_DRIVER_base_TOPIC": "/odom", "NEUROBOT_DRIVER_base_FRESH": "none"}, "/e/n.env")
        self.assertEqual(watchdog.driver_watch(c), {
            "neurobot-driver@lidar.service": [("/scan", 2.0)],
            "neurobot-driver@camera.service": [("/camera/color/image_raw", 2.5), ("/camera/imu", 1.0)]})
        for bad in ("scan:1", "/scan", "/scan:0", "/scan:x"):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                watchdog.driver_watch(Config({"NEUROBOT_DRIVERS": "lidar", "NEUROBOT_DRIVER_lidar_FRESH": bad}, "e"))

    def test_stale_driver_restarted_after_two_probes(self):
        watch = {"neurobot-driver@lidar.service": [("/scan", 1.5)],
                 "neurobot-driver@camera.service": [("/camera/color/image_raw", 2.0)]}
        props = {u: running(u) for u in watch}
        fails: Dict[str, int] = {}
        ages = {"/scan": 0.1, "/camera/color/image_raw": 5.0}
        self.assertEqual(watchdog.sensor_actions(watch, ages, props, NOW, fails), [])
        got = watchdog.sensor_actions(watch, ages, props, NOW, fails)
        self.assertEqual([(a.unit, a.code) for a in got], [("neurobot-driver@camera.service", "sensor_stale")])
        self.assertIn("/camera/color/image_raw (5.0 с)", got[0].text)
        self.assertEqual(fails, {})
        ages["/camera/color/image_raw"] = 0.2  # одна неудача между свежими — не рестарт
        for a in (5.0, 0.2, 5.0):
            ages["/camera/color/image_raw"] = a
            self.assertEqual(watchdog.sensor_actions(watch, ages, props, NOW, fails), [])
        props["neurobot-driver@camera.service"] = running("c", since=3)  # только что поднялся — ждём
        self.assertEqual(watchdog.sensor_actions(watch, ages, props, NOW, fails), [])
        self.assertEqual(watchdog.sensor_actions(watch, ages, props, NOW, fails), [])

    def test_everything_silent_restarts_nobody(self):
        # молчат темы всех драйверов сразу — беда DDS, сторожа или пауза ВМ, а не отказ драйвера
        watch = {"neurobot-driver@lidar.service": [("/scan", 1.5)], "neurobot-driver@base.service": [("/odom", 1.5)]}
        props = {u: running(u) for u in watch}
        fails: Dict[str, int] = {}
        for _ in range(3):
            self.assertEqual(watchdog.sensor_actions(watch, {"/scan": 9.0, "/odom": 9.0}, props, NOW, fails), [])
        # пределы разные: /odom ещё в своём (3 с), /scan — уже нет, но молчат оба дольше наименьшего — тоже общая беда
        # (двойник: рестарт Gazebo глушит и лидар) [замер 18.10: без этого — лишний рестарт драйвера лидара]
        watch["neurobot-driver@base.service"] = [("/odom", 3.0)]
        for _ in range(3):
            self.assertEqual(watchdog.sensor_actions(watch, {"/scan": 2.5, "/odom": 2.5}, props, NOW, fails), [])
        ages = {"/scan": 2.5, "/odom": 0.1}  # молчит только лидар — его и перезапускаем
        self.assertEqual(watchdog.sensor_actions(watch, ages, props, NOW, fails), [])
        self.assertEqual([a.unit for a in watchdog.sensor_actions(watch, ages, props, NOW, fails)],
                         ["neurobot-driver@lidar.service"])

    def test_rosbridge_ping_needs_two_failures(self):
        props = running("r")
        fails: Dict[str, int] = {}
        results = iter([ProbeError("нет pong за 1 с"), None, ProbeError("x"), ProbeError("соединение оборвано")])

        def ping():
            e = next(results)
            if e:
                raise e
        got = [watchdog.rosbridge_action(ping, props, NOW, fails) for _ in range(4)]
        self.assertEqual([len(g) for g in got], [0, 0, 0, 1])
        self.assertEqual((got[3][0].unit, got[3][0].code), ("neurobot-rosbridge.service", "rosbridge_down"))
        self.assertEqual(watchdog.rosbridge_action(ping, running("r", since=2), NOW, fails), [])  # грейс после старта

    def test_fast_step_restarts_without_blocking(self):
        c = Config({"NEUROBOT_DRIVERS": "lidar urg", "NEUROBOT_DRIVER_lidar_TOPIC": "/scan",
                    "NEUROBOT_DRIVER_urg_TOPIC": "/scan2"}, "/e/n.env")
        units = ["neurobot-driver@lidar.service", "neurobot-driver@urg.service", watchdog.ROSBRIDGE_UNIT]
        sd = FakeSd({u: running(u) for u in units})
        topics = types.SimpleNamespace(ages=lambda: {"/scan": 9.0, "/scan2": 0.1}, spin=lambda s: None)

        class Dead:
            def ping(self, timeout):
                raise ProbeError("refused")
        f = watchdog.Fast(c, sd, topics, Dead())
        f.step(NOW)
        self.assertEqual(sd.calls, [])
        f.step(NOW)
        self.assertEqual(sorted(sd.calls), [("restart", "--no-block", "neurobot-driver@lidar.service"),
                                            ("restart", "--no-block", "neurobot-rosbridge.service")])

    def test_loop_slow_pass_does_not_block(self):
        # в цикле рестарт агентского контура — --no-block: частые пробы драйверов не стоят, пока юнит поднимается
        sd = FakeSd(healthy(), {"a": [Entry(WALL - 30, 3, "All MCP servers failed to connect", "a")]})
        run_once = watchdog.run_once

        class Stop(Exception):
            pass
        with mock.patch.object(watchdog, "time", types.SimpleNamespace(time=lambda: WALL, monotonic=lambda: NOW)), \
                mock.patch.object(watchdog, "run_once", lambda c, s, block=True: run_once(c, s, mcp_ok, block)), \
                mock.patch.object(watchdog, "WsPing", lambda *a: types.SimpleNamespace(ping=lambda t: None)), \
                mock.patch.object(watchdog.Fast, "wait", side_effect=Stop), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(Stop):
                watchdog.loop(cfg(), sd, 10.0, 1.0)
        self.assertEqual(sd.calls, [("restart", "--no-block", "neurobot-picoclaw.service")])

    def test_once_restarts_and_reports_to_stderr(self):
        # neurobot watchdog --once: действие — строка причины в stderr; stdout у CLI — только doctor, render, env-get
        sd = FakeSd(healthy(), {"a": [Entry(WALL - 30, 3, "All MCP servers failed to connect", "a")]})
        run_once = watchdog.run_once
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(watchdog, "time", types.SimpleNamespace(time=lambda: WALL, monotonic=lambda: NOW)), \
                mock.patch.object(cli, "Systemd", lambda: sd), \
                mock.patch.object(watchdog, "run_once", lambda c, s: run_once(c, s, probe=mcp_ok)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cmd_watchdog(cfg(), types.SimpleNamespace(once=True))
        self.assertEqual((code, out.getvalue()), (0, ""))
        self.assertIn("neurobot-watchdog: picoclaw: agent_zombie: restart neurobot-picoclaw.service", err.getvalue())
        self.assertEqual(sd.calls, [("restart", "neurobot-picoclaw.service")])


if __name__ == "__main__":
    unittest.main()
