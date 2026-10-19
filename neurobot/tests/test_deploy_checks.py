"""neurobot/deploy: проверки перед стартом на подставных /sys и /proc, конфиги MCP и PicoClaw, оверлей PicoClaw.
Только stdlib, Python 3.8+."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))

from neurobot_deploy import __main__ as cli  # noqa: E402
from neurobot_deploy import checks, picoclaw_cfg  # noqa: E402
from neurobot_deploy.config import DEFAULTS, Config, EnvError  # noqa: E402


def put(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def codes(problems) -> list:
    return [(p.code, p.fatal) for p in problems]


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def p(self, *parts) -> str:
        return os.path.join(self.tmp, *parts)


class CameraTest(Tmp):
    def usb(self, name: str, vid: str, pid: str, speed: str):
        for f, v in (("idVendor", vid), ("idProduct", pid), ("speed", speed)):
            put(self.p("sys/bus/usb/devices", name, f), v + "\n")

    def test_found_on_usb3(self):
        self.usb("1-1", "0bda", "5411", "480")
        self.usb("2-1", "8086", "0b3a", "5000")
        self.assertEqual(checks.check_camera(self.p("sys"), "8086:0b3a"), [])

    def test_usb2_is_warning_and_list_of_ids(self):
        self.usb("1-2", "8086", "0b07", "480")
        self.assertEqual(codes(checks.check_camera(self.p("sys"), "8086:0b3a, 8086:0B07")), [("camera_usb2", False)])

    def test_missing(self):
        self.usb("1-1", "0bda", "5411", "480")
        (p,) = checks.check_camera(self.p("sys"), "8086:0b3a")
        self.assertEqual((p.code, p.fatal), ("camera_missing", True))
        self.assertIn("8086:0b3a", p.text)
        self.assertEqual(codes(checks.check_camera(self.p("нет"), "8086:0b3a")), [("camera_missing", True)])


class WeightsTest(Tmp):
    def manifest(self, entries) -> str:
        return put(self.p("weights.sha256"), "# комментарий\n" + "".join("{}  {}\n".format(h, f) for h, f in entries))

    def test_ok_missing_and_wrong_hash(self):
        good = put(self.p("m/a.pt"), "a")
        bad = put(self.p("m/b.pth"), "b")
        sha = hashlib.sha256(b"a").hexdigest()
        self.assertEqual(checks.check_weights(self.manifest([(sha, good)])), [])
        problems = checks.check_weights(self.manifest([(sha, good), (sha, bad), (sha, self.p("m/c.pt"))]))
        self.assertEqual(codes(problems), [("weights_hash", True), ("weights_missing", True)])
        self.assertIn(self.p("m/c.pt"), problems[1].text)

    def test_bad_manifest(self):
        self.assertEqual(codes(checks.check_weights(self.p("нет.sha256"))), [("config_invalid", True)])
        self.assertEqual(codes(checks.check_weights(put(self.p("e"), "# пусто\n"))), [("config_invalid", True)])
        self.assertEqual(codes(checks.check_weights(put(self.p("x"), "abc  /m/a.pt\n"))), [("config_invalid", True)])


TCP_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def tcp_line(port: int, state: str, inode: int) -> str:
    return "   0: 0100007F:{:04X} 00000000:0000 {} 00000000:00000000 00:00000000 00000000  1000        0 {} 1\n".format(
        port, state, inode)


class ProcTest(Tmp):
    def proc(self, pid: int, cmdline: str, cgroup: str, comm: str = "python3", fds=None):
        base = self.p("proc", str(pid))
        put(os.path.join(base, "cmdline"), cmdline.replace(" ", "\0"))
        put(os.path.join(base, "cgroup"), "0::" + cgroup + "\n")
        put(os.path.join(base, "comm"), comm + "\n")
        os.makedirs(os.path.join(base, "fd"), exist_ok=True)
        for i, target in enumerate(fds or []):
            os.symlink(target, os.path.join(base, "fd", str(i)))

    def test_port_busy_names_owner(self):
        put(self.p("proc/net/tcp"), TCP_HEADER + tcp_line(9090, "0A", 4242) + tcp_line(18110, "01", 7))
        put(self.p("proc/net/tcp6"), TCP_HEADER)
        self.proc(700, "python3 -m rosbridge_server", "/system.slice/neurobot-rosbridge.service", "rosbridge",
                  fds=["socket:[4242]"])
        (p,) = checks.check_port_free(9090, self.p("proc"))
        self.assertEqual(p.code, "port_busy")
        self.assertIn("pid 700 (rosbridge, neurobot-rosbridge.service)", p.text)
        self.assertEqual(checks.check_port_free(18110, self.p("proc")), [])  # ESTABLISHED — не занят

    def test_foreign_process_outside_our_cgroup(self):
        self.proc(10, "/usr/bin/python3 /opt/neurobot/bin/neurobot precheck perception",
                  "/system.slice/neurobot-perception.service")
        self.proc(11, "python3 src/camera/predict_live.py", "/user.slice/user-1000.slice/session-3.scope")
        self.proc(12, "python3 predict_live.py --mode legacy", "/system.slice/neurobot-perception.service")
        self.proc(13, "vim notes.txt", "/user.slice/session-3.scope")
        self.proc(14, "python3 neurobot start-test --deviation рядом_шёл_realsense-viewer_и_predict_live.py",
                  "/user.slice/session-3.scope")
        problems = checks.check_foreign(DEFAULTS["NEUROBOT_FOREIGN_PERCEPTION"].split(), self.p("proc"), self_pid=10)
        self.assertEqual(len(problems), 1)
        self.assertIn("pid 11, вне сервисов (session-3.scope)", problems[0].text)

    def test_foreign_mcp_spawned_by_picoclaw(self):
        self.proc(20, "python3 ros_mcp_server.py --config c.json", "/system.slice/neurobot-picoclaw.service")
        (p,) = checks.check_foreign(DEFAULTS["NEUROBOT_FOREIGN_MCP"].split(), self.p("proc"), self_pid=1)
        self.assertIn("юнит neurobot-picoclaw.service", p.text)

    def test_lab_workers_nohup_vs_supervised(self):
        self.proc(30, "/home/jetson/.pyenv/shims/python3 /home/jetson/NeuroChip/yolo_hand_worker.py",
                  "/user.slice/user-1000.slice/session-2.scope")
        self.proc(31, "/usr/bin/python3 /home/jetson/NeuroChip/yolo_hand_worker.py",
                  "/system.slice/system-neurobot\\x2dlabworker.slice/neurobot-labworker@yolo.service")
        self.proc(32, "/opt/ros/foxy/lib/realsense2_camera/realsense2_camera_node --ros-args",
                  "/user.slice/session-2.scope")
        mcp = checks.check_foreign(DEFAULTS["NEUROBOT_FOREIGN_MCP"].split(), self.p("proc"), self_pid=1)
        self.assertEqual([p.text.split(":")[0] for p in mcp], ["уже запущен yolo_hand_worker.py"])
        self.assertIn("pid 30", mcp[0].text)
        # камера и CUDA: узел камеры лаборатории и YOLO-воркер вне юнита — второй владелец D435i и контекст CUDA
        cam = checks.check_foreign(DEFAULTS["NEUROBOT_FOREIGN_PERCEPTION"].split(), self.p("proc"), self_pid=1)
        self.assertEqual([p.text.split(",")[0] for p in cam], ["уже запущен yolo_hand_worker.py: pid 30",
                                                               "уже запущен realsense2_camera_node: pid 32"])

    def test_camera_node_of_driver_unit_by_camera_mode(self):
        # безопасность v2: камерой владеет realsense2_camera в neurobot-driver@camera, распознавание читает его тему
        put(self.p("proc/1/cmdline"), "init")
        self.proc(40, "/opt/ros/foxy/lib/realsense2_camera/realsense2_camera_node --ros-args -r __node:=camera",
                  "/system.slice/system-neurobot\\x2ddriver.slice/neurobot-driver@camera.service")
        base = {"NEUROBOT_SYSFS": self.p("нет"), "NEUROBOT_WEIGHTS": self.p("нет.sha256")}

        def foreign(cmd: str) -> list:
            cfg = Config(dict(base, NEUROBOT_PERCEPTION_CMD=cmd), self.p("e"))
            return [p.text for p in checks.precheck("perception", cfg, proc=self.p("proc"))
                    if p.code == "foreign_process"]
        tail = " --mode legacy --ros-args -r /cmd_vel:=/cmd_vel_gesture"
        self.assertEqual(foreign("python3 predict_live.py --camera ros" + tail), [])
        self.assertEqual(foreign("python3 predict_live.py --camera=ros" + tail), [])
        (text,) = foreign("python3 predict_live.py" + tail)  # своя камера (rs) — второй владелец D435i
        self.assertIn("realsense2_camera_node: pid 40, юнит neurobot-driver@camera.service", text)
        # узел камеры вне наших юнитов — чужой и при --camera ros
        self.proc(41, "realsense2_camera_node", "/user.slice/session-2.scope")
        (text,) = foreign("python3 predict_live.py --camera ros" + tail)
        self.assertIn("pid 41", text)


def mcp_cfg(tmp: str, data: dict, **env) -> Config:
    """Конфиг MCP рядом с env робота: копия состояния safety_gate для агента — как в neurobot.env.example."""
    path = put(os.path.join(tmp, "ros_mcp.json"), json.dumps(data))
    return Config(dict({"NEUROBOT_MCP_CONFIG": path, "NEUROBOT_AGENT_STATE_TOPIC": "/neurobot/agent/safety_state"},
                       **env), os.path.join(tmp, "n.env"))


class McpConfigTest(Tmp):
    def example(self) -> dict:
        return json.loads((DEPLOY / "mcp/ros_mcp.json.example").read_text(encoding="utf-8"))

    def test_example_passes(self):
        self.assertEqual(checks.check_mcp_config(mcp_cfg(self.tmp, self.example())), [])

    def test_agent_must_not_write_cmd_vel(self):
        data = self.example()
        data["topics"]["cmd_vel"]["name"] = "/cmd_vel"
        (p,) = checks.check_mcp_config(mcp_cfg(self.tmp, data))
        self.assertEqual(p.code, "config_invalid")
        self.assertIn("второй писатель", p.text)

    def test_server_rosbridge_and_tools(self):
        data = self.example()
        data["server"] = {"host": "0.0.0.0", "port": 18111, "debug": True}
        data["rosbridge"]["url"] = "ws://127.0.0.1:9091"
        data["enabled_tools"]["camera_capture_frame_to_file"] = True
        got = checks.check_mcp_config(mcp_cfg(self.tmp, data))
        self.assertEqual([p.fatal for p in got], [True, True, True, True, False])
        self.assertIn("server.debug", got[0].text)
        self.assertIn("M1", got[4].text)

    def test_agent_cannot_reach_gate_input_estop_or_other_topics(self):
        for topic, why in (("/neurobot/cmd_vel_req", "мимо исполнителя"), ("/neurobot/estop", "снять аварийную"),
                           ("/cmd_vel_gesture", "исполнитель слушает /cmd_vel_agent")):
            data = self.example()
            data["topics"]["cmd_vel"]["name"] = topic
            with self.subTest(topic=topic):
                (p,) = checks.check_mcp_config(mcp_cfg(self.tmp, data))
                self.assertIn(why, p.text)
        data = self.example()
        data["topics"]["camera"] = {"name": "/neurobot/estop", "type": "std_msgs/msg/String", "access": "write"}
        data["safety"]["reject_unknown_topics"] = False
        got = checks.check_mcp_config(mcp_cfg(self.tmp, data))
        self.assertEqual([p.fatal for p in got], [True, True])
        self.assertIn("открыта запись в /neurobot/estop", got[0].text)
        self.assertIn("reject_unknown_topics=false", got[1].text)

    def test_motion_limits_not_wider_than_agent_limits(self):
        data = self.example()
        data["motion"].update(max_linear_speed=0.3, max_angular_speed=0.8, max_duration_ms=3000)
        got = checks.check_mcp_config(mcp_cfg(self.tmp, data))
        self.assertEqual([p.text.split(":")[1].split("=")[0].strip() for p in got],
                         ["motion.max_linear_speed", "motion.max_duration_ms"])
        self.assertIn("шире предела агента 1 с (L3)", got[1].text)
        loose = mcp_cfg(self.tmp, data, NEUROBOT_AGENT_MAX_SPEED="0.5", NEUROBOT_AGENT_MAX_DURATION_S="5")
        self.assertEqual(checks.check_mcp_config(loose), [])

    def test_safety_state_for_status_and_nudge(self):
        # приёмка: robot_get_status и robot_nudge видят состояние safety_gate только через topics.safety_state
        data = self.example()
        del data["topics"]["safety_state"]
        (p,) = checks.check_mcp_config(mcp_cfg(self.tmp, data))
        self.assertEqual((p.code, p.fatal), ("config_invalid", False))
        self.assertIn("robot_nudge ответит ok и там, где safety_gate не пустил", p.text)
        data["topics"]["safety_state"] = {"name": "/scan", "type": "std_msgs/msg/String", "access": "read"}
        (p,) = checks.check_mcp_config(mcp_cfg(self.tmp, data))
        self.assertIn("/neurobot/safety_state и /neurobot/agent/safety_state", p.text)
        data["topics"]["safety_state"] = {"name": "/neurobot/safety_state", "type": "std_msgs/msg/String",
                                          "access": "read_write"}
        (p,) = checks.check_mcp_config(mcp_cfg(self.tmp, data))
        self.assertEqual((p.fatal, "открыта запись в /neurobot/safety_state" in p.text), (True, True))

    def test_not_json(self):
        cfg = Config({"NEUROBOT_MCP_CONFIG": put(self.p("c.json"), "{")}, self.p("n.env"))
        self.assertEqual(codes(checks.check_mcp_config(cfg)), [("config_invalid", True)])

    def test_server_without_config_file(self):  # прокси лаборатории: настройки — из окружения
        self.assertEqual(checks.check_mcp_config(Config({"NEUROBOT_MCP_CONFIG": ""}, self.p("n.env"))), [])


LAB_PICOCLAW = DEPLOY.parents[1] / "third_party/neurochip/robotic_platform/robotic_platform/.picoclaw/config.json"


def overlay() -> dict:
    return json.loads((DEPLOY / "picoclaw/config.overlay.json").read_text(encoding="utf-8"))


class PicoClawConfigTest(Tmp):
    BASE = {"version": 3,
            "agents": {"defaults": {"workspace": "/w", "model_name": "m", "max_tokens": 32768}},
            "model_list": [{"model_name": "m", "model": "gpt-5-mini", "api_keys": ["секрет"]}],
            "channel_list": {"telegram": {"enabled": True}},
            "gateway": {"host": "localhost", "port": 18790, "hot_reload": True},
            "tools": {"mcp": {"enabled": False, "servers": {
                "robot_mcp": {"enabled": True, "command": "python3", "args": ["/r/ros_mcp_server.py"]}}}}}

    def cfg(self, data: dict, version: str = "0.3.1") -> Config:
        return Config({"PICOCLAW_CONFIG": put(self.p("config.json"), json.dumps(data)),
                       "NEUROBOT_PICOCLAW_VERSION": version}, self.p("n.env"))

    def for_version(self, data: dict, version: str) -> dict:
        ov, _ = picoclaw_cfg.prune(overlay(), picoclaw_cfg.load_schema(version)[0])
        return picoclaw_cfg.merged(data, ov, 20)

    def test_lab_stdio_config_is_rejected_with_reasons(self):
        problems = checks.check_picoclaw_config(self.cfg(self.BASE))
        fatal = [p.text for p in problems if p.fatal]
        self.assertTrue(any("tools.mcp.enabled=false" in t for t in fatal))
        self.assertTrue(any("по stdio" in t and "второй набор инструментов движения" in t for t in fatal))
        self.assertTrue(any("нет включённого MCP-сервера type=http" in t for t in fatal))
        self.assertTrue(any("gateway.log_level=warn" in t and "N15" in t for t in fatal))
        # shell для LLM на роботе — отказ: пишет /cmd_vel мимо safety_gate, снимает защёлку, ставит numpy 2 (N5)
        self.assertTrue(any("tools.exec включён" in t and "защёлку" in t for t in fatal))
        self.assertTrue(any("tools.cron с командами" in t for t in fatal))
        warn = " ".join(p.text for p in problems if not p.fatal)
        for word in ("hot_reload", "max_tokens", "max_parallel_turns", "max_llm_retries", "max_tool_iterations",
                     "request_timeout", "web_fetch"):
            self.assertIn(word, warn)

    def test_overlay_fixes_everything_and_keeps_lab_settings(self):
        out = self.for_version(self.BASE, "0.3.1")
        self.assertEqual(checks.check_picoclaw_config(self.cfg(out)), [])
        self.assertEqual(out["tools"]["mcp"]["servers"],
                         {"robot_mcp": {"enabled": True, "type": "http", "url": "http://127.0.0.1:18110/mcp"}})
        self.assertEqual(out["gateway"]["log_level"], "info")
        self.assertEqual(out["model_list"][0]["api_keys"], ["секрет"])
        self.assertEqual(out["model_list"][0]["request_timeout"], 20)
        self.assertEqual(out["channel_list"], self.BASE["channel_list"])
        self.assertEqual(out["agents"]["defaults"]["workspace"], "/w")
        self.assertEqual(self.BASE["gateway"]["hot_reload"], True)  # исходный словарь не тронут

    def test_unknown_fields_of_installed_version_are_fatal(self):
        out = picoclaw_cfg.merged(self.BASE, overlay(), 20)  # оверлей 0.3.1 как есть — на 0.2.8 шлюз не стартует
        (p,) = [p for p in checks.check_picoclaw_config(self.cfg(out, "0.2.8")) if p.fatal]
        self.assertIn("agents.defaults.max_llm_retries, tools.load_image", p.text)
        self.assertIn("PicoClaw 0.2.8 (NEUROBOT_PICOCLAW_VERSION)", p.text)

    def test_prune_by_version_and_unknown_version(self):
        _, dropped = picoclaw_cfg.prune(overlay(), picoclaw_cfg.load_schema("0.2.8")[0])
        self.assertEqual(dropped, ["agents.defaults.max_llm_retries", "tools.load_image"])
        self.assertEqual(picoclaw_cfg.prune(overlay(), picoclaw_cfg.load_schema("0.3.1")[0])[1], [])
        schema, exact = picoclaw_cfg.load_schema("0.2.9")  # схемы нет — только общие поля 0.2.8 и 0.3.1
        self.assertFalse(exact)
        self.assertEqual(picoclaw_cfg.prune(overlay(), schema)[1], dropped)
        out = self.for_version(self.BASE, "0.2.9")
        problems = checks.check_picoclaw_config(self.cfg(out, "0.2.9"))
        self.assertEqual(codes(problems), [("config_invalid", False)])
        self.assertIn("схемы полей для PicoClaw 0.2.9 нет", problems[0].text)

    def test_streamable_http_needs_0_3_1(self):
        out = self.for_version(self.BASE, "0.2.8")
        out["tools"]["mcp"]["servers"]["robot_mcp"]["type"] = "streamable-http"
        (p,) = checks.check_picoclaw_config(self.cfg(out, "0.2.8"))
        self.assertIn("streamable-http в PicoClaw 0.2.8 не поддержан", p.text)
        self.assertEqual(checks.check_picoclaw_config(self.cfg(out, "0.3.1"))[0].code, "config_invalid")
        self.assertTrue(all(not p.fatal for p in checks.check_picoclaw_config(self.cfg(out, "0.3.1"))))

    @unittest.skipUnless(LAB_PICOCLAW.is_file(), "нет конфига PicoClaw лаборатории (third_party)")
    def test_lab_robot_config_0_2_8(self):
        lab = json.loads(LAB_PICOCLAW.read_text(encoding="utf-8"))
        self.assertEqual(lab["build_info"]["version"], "0.2.8")
        cfg = Config({"PICOCLAW_CONFIG": put(self.p("config.json"), json.dumps(lab))}, self.p("n.env"))
        fatal = [p.text for p in checks.check_picoclaw_config(cfg) if p.fatal]
        # уровень журнала, прокси робота по stdio, нет HTTP-сервера, exec и cron с командами у LLM
        self.assertEqual(len(fatal), 5, fatal)
        self.assertTrue(any("robot_vision_demo_mcp по stdio" in t and "второй набор" in t for t in fatal))
        out = self.for_version(lab, "0.2.8")
        self.assertEqual(out["tools"]["mcp"]["servers"]["robot_vision_demo_mcp"]["enabled"], False)
        self.assertEqual(out["tools"]["mcp"]["servers"]["robot_vision_demo_mcp"]["command"],
                         lab["tools"]["mcp"]["servers"]["robot_vision_demo_mcp"]["command"])
        self.assertNotIn("robot", out["tools"]["mcp"]["servers"])  # отключаются только существующие записи
        cfg = Config({"PICOCLAW_CONFIG": put(self.p("config.json"), json.dumps(out))}, self.p("n.env"))
        problems = checks.check_picoclaw_config(cfg)
        self.assertEqual([p.text for p in problems if p.fatal], [])
        self.assertEqual(picoclaw_cfg.unknown_fields(out, picoclaw_cfg.load_schema("0.2.8")[0]), [])

    def test_other_stdio_server_is_only_warning(self):
        out = self.for_version(self.BASE, "0.3.1")
        out["tools"]["mcp"]["servers"]["notes"] = {"enabled": True, "command": "notes-mcp"}
        self.assertEqual(codes(checks.check_picoclaw_config(self.cfg(out))), [("config_invalid", False)])

    def test_apply_writes_atomically_with_backup(self):
        path = put(self.p("config.json"), json.dumps(self.BASE))
        os.chmod(path, 0o600)
        backup = picoclaw_cfg.apply(path, overlay(), 20)
        self.assertEqual(json.loads(Path(backup).read_text(encoding="utf-8")), self.BASE)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8"))["gateway"]["hot_reload"], False)
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_cli_merge_by_installed_version(self):
        binary = put(self.p("bin/picoclaw"), "#!/bin/sh\necho 'picoclaw 0.2.8 (git: 6e1fab80)'\n")
        os.chmod(binary, 0o755)
        config = put(self.p("home/config.json"), json.dumps(self.BASE))
        env = put(self.p("n.env"), 'NEUROBOT_PICOCLAW_CMD="{} --no-color gateway"\nPICOCLAW_HOME={}\n'
                  'NEUROBOT_MCP_URL=http://127.0.0.1:18119/mcp\n'.format(binary, self.p("home")))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["--env", env, "picoclaw-config", "merge"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("PicoClaw 0.2.8 ({} version); нет в этой версии, пропущено: agents.defaults.max_llm_retries, "
                      "tools.load_image".format(binary), err.getvalue())
        out = json.loads(Path(config).read_text(encoding="utf-8"))
        self.assertEqual(out["tools"]["mcp"]["servers"]["robot_mcp"]["url"], "http://127.0.0.1:18119/mcp")
        self.assertNotIn("max_llm_retries", out["agents"]["defaults"])
        self.assertEqual(len(list(Path(self.p("home")).glob("config.json.neurobot-*"))), 1)

    def test_version_from_binary_and_build_info(self):
        binary = put(self.p("bin/picoclaw"), "#!/bin/sh\nprintf '\\033[1m logo 1.2\\n\\360\\237\\246\\236 "
                                             "picoclaw 0.2.8 (git: 6e1fab80)\\n  Build: 2026-04-30\\n'\n")
        os.chmod(binary, 0o755)
        self.assertEqual(picoclaw_cfg.detect_version("", binary + " --no-color gateway", {}),
                         ("0.2.8", binary + " version"))
        self.assertEqual(picoclaw_cfg.detect_version("", self.p("нет") + " gateway",
                                                     {"build_info": {"version": "0.3.1"}}),
                         ("0.3.1", "build_info.version в config.json"))
        self.assertEqual(picoclaw_cfg.detect_version("", "", {}), ("", ""))


class PythonEnvTest(Tmp):
    def wrapper(self, site: str) -> str:
        """Интерпретатор-обёртка: python3 с подставным site (numpy 2.4.6 как dist-info)."""
        put(os.path.join(site, "numpy", "__init__.py"), "__version__ = '2.4.6'\n")
        put(os.path.join(site, "numpy-2.4.6.dist-info", "METADATA"),
            "Metadata-Version: 2.1\nName: numpy\nVersion: 2.4.6\n")
        path = put(self.p("bin/python3"), '#!/bin/sh\nPYTHONPATH="{}:$PYTHONPATH" exec "{}" "$@"\n'.format(
            site, sys.executable))
        os.chmod(path, 0o755)
        return path

    def check(self, cmd: str, reqs: str, **env) -> list:
        return checks.check_python(cmd, "MODS", Config(dict({"MODS": reqs, "NEUROBOT_ROS_SETUP": self.p("нет")}, **env),
                                                       self.p("n.env")))

    def test_modules_and_versions(self):
        py = self.wrapper(self.p("site"))
        problems = self.check(py + " /x/predict_live.py --mode legacy", "numpy<2 json no_such_module python>=3.8")
        self.assertEqual(codes(problems), [("python_env", True), ("python_env", True)])
        self.assertIn("numpy 2.4.6, нужна <2 — cv_bridge и пакеты ROS 2 Foxy собраны с NumPy 1.x (N5)",
                      problems[0].text)
        self.assertIn("нет модуля no_such_module", problems[1].text)
        self.assertEqual(self.check(py + " x.py", "numpy>=2 json python>=3.8"), [])
        (p,) = self.check(py + " x.py", "python>=99")
        self.assertIn("нужна >=99 — FastMCP — Python ≥ 3.10 (M13)", p.text)

    def test_ros_setup_is_sourced(self):
        py = self.wrapper(self.p("site"))
        put(self.p("ros/rclpy_x/__init__.py"), "")
        setup = put(self.p("setup.bash"), 'export PYTHONPATH="{}:$PYTHONPATH"\n'.format(self.p("ros")))
        self.assertEqual(self.check(py, "rclpy_x", NEUROBOT_ROS_SETUP=setup), [])

    def test_shim_relative_and_not_python(self):
        problems = self.check("/home/u/.pyenv/shims/python3 x.py", "json")
        self.assertEqual(codes(problems), [("python_env", False), ("python_env", True)])
        self.assertIn("шим pyenv", problems[0].text)
        self.assertEqual(self.check("ros2 launch urg_node2 urg_node2.launch.py", "json"), [])
        self.assertEqual(self.check("env A=1 {} -m x".format(sys.executable), "json"), [])
        with self.assertRaisesRegex(EnvError, "MODS"):
            self.check(sys.executable, "numpy~=1")


def chain(**topics) -> dict:
    """Граф цепочки движения робота в порядке: всё на месте, кроме переданного."""
    g = {"/cmd_vel": {"pub": ["safety_gate"], "sub": ["twist_mux"]},
         "/diff_cont/cmd_vel_unstamped": {"pub": ["twist_mux"], "sub": ["diff_cont"]},
         "/scan": {"pub": ["urg_node2"], "sub": ["safety_gate"]},
         "/cmd_vel_tracker": {"pub": [], "sub": ["twist_mux"]}, "/cmd_vel_joy": {"pub": [], "sub": ["twist_mux"]}}
    g.update({"/" + k.replace("__", "/"): v for k, v in topics.items()})
    return g


class GraphTest(unittest.TestCase):
    cfg = Config({}, "/e/n.env")

    def check(self, graph) -> list:
        return codes(checks.graph_problems("safety", self.cfg, graph))

    def test_robot_chain_is_clean(self):
        self.assertEqual(self.check(chain()), [])

    def test_our_helpers_and_dead_gate_are_not_foreign(self):
        # neurobot zero / doctor и издатель убитого kill -9 safety_gate (висит в DDS до конца аренды)
        g = chain(cmd_vel={"pub": ["safety_gate", "safety_gate", "neurobot_zero_77"], "sub": ["twist_mux"]})
        self.assertEqual(self.check(g), [])

    def test_writers_against_the_chain(self):
        cases = [(chain(cmd_vel={"pub": ["safety_gate", "predict_live"], "sub": ["twist_mux"]}),
                  [("cmd_vel_writer", True)]),
                 (chain(cmd_vel_tracker={"pub": ["tracker_node"]}), [("cmd_vel_override", True)]),
                 (chain(diff_cont__cmd_vel_unstamped={"pub": ["twist_mux", "teleop"]}), [("cmd_vel_bypass", True)]),
                 (chain(cmd_vel={"pub": ["safety_gate"], "sub": ["diff_cont"]}), [("twist_mux_missing", False)]),
                 (chain(cmd_vel_joy={"pub": ["joy_teleop"]}), [("cmd_vel_joy", False)]),
                 (chain(scan={"pub": ["urg_node2", "urg_node2"]}), [("scan_publishers", True)]),
                 (chain(scan={"pub": []}), [("scan_stale", False)])]
        for graph, want in cases:
            with self.subTest(want=want):
                self.assertEqual(self.check(graph), want)

    def test_stand_without_twist_mux(self):
        cfg = Config({"NEUROBOT_MUX_NODE": "", "NEUROBOT_BASE_CMD_TOPIC": ""}, "/e/n.env")
        g = chain(cmd_vel={"pub": ["safety_gate"], "sub": ["ros_gz_bridge"]})
        self.assertEqual(checks.graph_problems("safety", cfg, g), [])
        self.assertEqual(checks.graph_topics("safety", cfg), ["/cmd_vel", "/scan", "/cmd_vel_tracker", "/cmd_vel_joy"])


class AgentPathTest(Tmp):
    def rosbridge(self, cmd: str, **env) -> list:
        return checks.check_rosbridge(Config(dict({"NEUROBOT_ROSBRIDGE_CMD": cmd}, **env), self.p("n.env")))

    def test_example_rosbridge_lets_only_agent_topic(self):
        from neurobot_deploy.config import parse_env
        example = parse_env((DEPLOY / "neurobot.env.example").read_text(encoding="utf-8"))
        self.assertEqual(checks.check_rosbridge(Config(example, self.p("n.env"))), [])
        # копия состояния для агента в topics_glob — только когда она объявлена (NEUROBOT_AGENT_STATE_TOPIC)
        (p,) = checks.check_rosbridge(Config(dict(example, NEUROBOT_AGENT_STATE_TOPIC=""), self.p("n.env")))
        self.assertIn("лишнее /neurobot/agent/safety_state", p.text)

    BASE = "ros2 launch rosbridge_server rosbridge_websocket_launch.xml address:=127.0.0.1 "
    TOPICS = "topics_glob:=\"'[/cmd_vel_agent]'\""
    SERVICES = " services_glob:=\"'[/rosapi/topics]'\""
    PARAMS = " params_glob:=\"'[none]'\""

    def test_address(self):
        # без address rosbridge слушает все интерфейсы: тема агента и /rosapi/* открыты сети лаборатории
        globs = self.TOPICS + self.SERVICES + self.PARAMS
        self.assertEqual(self.rosbridge(self.BASE + globs), [])
        self.assertEqual(self.rosbridge(self.BASE.replace("127.0.0.1", "localhost") + globs), [])
        (p,) = self.rosbridge(self.BASE.replace(" address:=127.0.0.1", "") + globs)
        self.assertEqual((p.code, p.fatal), ("config_invalid", True))
        self.assertIn("без address:=127.0.0.1", p.text)
        for value in ("0.0.0.0", "", "192.168.1.5"):
            with self.subTest(address=value):
                (p,) = self.rosbridge(self.BASE.replace("127.0.0.1", value) + globs)
                self.assertIn("address:={} — rosbridge открыт сети".format(value), p.text)

    def test_repeated_argument_is_refused(self):
        # [опыт, Jazzy]: при повторе ros2 launch берёт последнее значение — дописанный в конец «[*]» открыл бы всё
        head = self.BASE + self.TOPICS + self.SERVICES + self.PARAMS
        for tail in (" topics_glob:=\"'[*]'\"", " params_glob:=\"'[*]'\"", " services_glob:=\"'[/rosapi/topics]'\"",
                     " address:=0.0.0.0"):
            arg = tail.split(":=")[0].strip()
            with self.subTest(arg=arg):
                (p,) = self.rosbridge(head + tail)
                self.assertEqual((p.code, p.fatal), ("config_invalid", True))
                self.assertIn("{} задан 2 раза — ros2 launch возьмёт последнее значение".format(arg), p.text)

    def test_topics_glob_allows_only_agent_topic(self):
        base, rest = self.BASE, self.SERVICES + self.PARAMS
        self.assertEqual(self.rosbridge(""), [])
        self.assertEqual(self.rosbridge(base + "topics_glob:=\"'[ /cmd_vel_agent , /cmd_vel_agent ]'\"" + rest), [])
        # пустой список в rosbridge 1.3.1 — «без ограничений»
        for value in ("", "topics_glob:=\"'[]'\"", "topics_glob:=\"'[ ]'\""):
            with self.subTest(value=value):
                (p,) = self.rosbridge(base + value + rest)
                self.assertEqual((p.code, p.fatal), ("config_invalid", True))
                self.assertIn("без topics_glob", p.text)
                self.assertIn("/cmd_vel, /neurobot/cmd_vel_req и /neurobot/estop", p.text)
        # любая тема сверх темы агента — отказ: в rosbridge тема из списка открыта и на запись
        cases = {"/cmd_vel*": "открывает агенту /cmd_vel, /cmd_vel_joy, /cmd_vel_tracker",
                 "/neurobot/*": "/neurobot/cmd_vel_req, /neurobot/estop, /neurobot/motion_state",
                 "/neurobot/safety_state": "открывает агенту /neurobot/safety_state",  # подмена состояния для doctor
                 "/scan": "открывает агенту /scan",  # подмена датчика safety_gate
                 "/odom": "лишнее /odom", "/cmd_vel_agen?": "лишнее /cmd_vel_agen?"}
        for glob, want in cases.items():
            with self.subTest(glob=glob):
                (p,) = self.rosbridge(base + "topics_glob:=\"'[/cmd_vel_agent, {}]'\"".format(glob) + rest)
                self.assertIn("разрешено только тема агента /cmd_vel_agent", p.text)
                self.assertIn(want, p.text)
        (p,) = self.rosbridge(base + "topics_glob:=\"'[/camera/depth/image_rect_raw]'\"" + rest,
                              NEUROBOT_SENSORS="/camera/depth/image_rect_raw:sensor_msgs/msg/Image:5")
        self.assertIn("открывает агенту /camera/depth/image_rect_raw", p.text)
        (p,) = self.rosbridge(base + "topics_glob:=[/cmd_vel_agent]" + rest)  # без кавычек YAML не стартует
        self.assertIn("нужен список в кавычках YAML", p.text)

    def test_pub_and_sub_globs_of_rosbridge_2x(self):
        # rosbridge 2.x объединяет topics_pub_glob и topics_sub_glob с topics_glob: публикация — тот же список
        # разрешённого, подписка движение не открывает; в 1.3.1 (Foxy) этих аргументов нет
        head, rest = self.BASE + self.TOPICS, self.SERVICES + self.PARAMS
        for tail in ("", " topics_pub_glob:=\"'[/cmd_vel_agent]'\"", " topics_pub_glob:=\"'[]'\"",
                     " topics_sub_glob:=\"'[/neurobot/*, /scan]'\"", " topics_sub_glob:="):
            with self.subTest(tail=tail):
                self.assertEqual(self.rosbridge(head + rest + tail), [])
        (p,) = self.rosbridge(head + rest + " topics_pub_glob:=\"'[*]'\"")
        self.assertEqual((p.code, p.fatal), ("config_invalid", True))
        self.assertIn("topics_pub_glob '[*]': лишнее * — разрешено только тема агента /cmd_vel_agent", p.text)
        self.assertIn("/neurobot/estop", p.text)
        (p,) = self.rosbridge(head + rest + " topics_sub_glob:=[/scan]")
        self.assertIn("нужен список в кавычках YAML", p.text)
        (p,) = self.rosbridge(head + rest + " topics_pub_glob:=\"'[/cmd_vel_agent]'\" topics_pub_glob:=\"'[*]'\"")
        self.assertIn("topics_pub_glob задан 2 раза", p.text)

    def test_services_glob_allows_only_rosapi(self):
        # [опыт, rosbridge 2.7.1]: без services_glob клиент поменял max_speed safety_gate через set_parameters
        head, params = self.BASE + self.TOPICS, self.PARAMS
        for value in ("", " services_glob:=\"'[]'\""):
            with self.subTest(value=value):
                (p,) = self.rosbridge(head + value + params)
                self.assertEqual((p.code, p.fatal), ("config_invalid", True))
                self.assertIn("без services_glob", p.text)
                self.assertIn("set_parameters у safety_gate", p.text)
        # /rosapi/* rosbridge пропускает сам при любом services_glob — его ограничивает params_glob
        self.assertEqual(self.rosbridge(head + " services_glob:=\"'[/rosapi/*, /rosapi/nodes]'\"" + params), [])
        cases = {"[*]": "/<любой узел>/set_parameters", "[/safety_gate/*]": "/safety_gate/set_parameters",
                 "[/diff_cont/set_param*]": "/diff_cont/set_parameters",
                 "[/controller_manager/*]": "/controller_manager/switch_controller",
                 "[/neurobot/estop]": "/neurobot/estop", "[/twist_mux/set_parameters]": "/twist_mux/set_parameters",
                 # драйверы датчиков safety_gate: выключить глубину, сузить скан
                 "[/rosapi/topics, /urg_node2/set_parameters]": "лишнее /urg_node2/set_parameters",
                 "[/camera/camera/*]": "лишнее /camera/camera/*", "[/ros*]": "лишнее /ros*"}
        for glob, name in cases.items():
            with self.subTest(glob=glob):
                (p,) = self.rosbridge(head + " services_glob:=\"'{}'\"".format(glob) + params)
                self.assertIn("разрешено только /rosapi/", p.text)
                self.assertIn(name, p.text)
        (p,) = self.rosbridge(head + " services_glob:=[/rosapi/topics]" + params)
        self.assertIn("services_glob:=[/rosapi/topics] — нужен список в кавычках YAML", p.text)

    def test_backslash_in_any_glob_is_refused(self):
        # [опыт W1c, rosbridge 2.7.1]: в строке YAML в двойных кавычках «\x2c» — запятая; precheck видел один элемент
        # «/rosapi/topics\x2c/safety_gate/set_parameters», а rosbridge открыл set_parameters safety_gate
        head = self.BASE + self.TOPICS + self.SERVICES + self.PARAMS
        sneaky = {"services_glob": "'\"[/rosapi/topics\\x2c/safety_gate/set_parameters]\"'",
                  "topics_glob": "'\"[/cmd_vel_agent\\u002c/cmd_vel]\"'",
                  "params_glob": "\"'[none\\x2c*]'\""}
        for arg, value in sneaky.items():
            with self.subTest(arg=arg):
                cmd = head.replace(dict(services_glob=self.SERVICES, topics_glob=self.TOPICS,
                                        params_glob=self.PARAMS)[arg].strip(), "{}:={}".format(arg, value))
                problems = self.rosbridge(cmd)
                self.assertTrue(problems)
                self.assertTrue(all((p.code, p.fatal) == ("config_invalid", True) for p in problems))
                self.assertIn("{}:=".format(arg), problems[0].text)
                self.assertIn("обратная косая черта", problems[0].text)
        # любой *_glob, и необязательный (topics_sub_glob rosbridge 2.x) — тоже
        (p,) = self.rosbridge(head + " topics_sub_glob:=\"'[/scan\\x2c/odom]'\"")
        self.assertIn("topics_sub_glob", p.text)
        self.assertEqual(self.rosbridge(head + " topics_sub_glob:=\"'[/scan, /odom]'\""), [])

    def test_params_glob_allows_none_only(self):
        # [опыт, rosbridge 2.7.1]: с services_glob, но без params_glob /rosapi/set_param поменял max_speed safety_gate
        head = self.BASE + self.TOPICS + self.SERVICES
        self.assertEqual(self.rosbridge(head + self.PARAMS), [])
        for value in ("", " params_glob:=\"'[]'\""):
            with self.subTest(value=value):
                (p,) = self.rosbridge(head + value)
                self.assertIn("без params_glob", p.text)
                self.assertIn("/rosapi/set_param для любого параметра любого узла", p.text)
        for glob in ("*", "max_*", "speed_scale", "/safety_gate:max_speed", "cmd_vel_time?ut"):
            with self.subTest(glob=glob):
                (p,) = self.rosbridge(head + " params_glob:=\"'[none, {}]'\"".format(glob))
                self.assertIn("лишнее {} — разрешено только none".format(glob), p.text)

    def test_proxy_env(self):
        def proxy(**env):
            return codes(checks.check_proxy_env(Config(env, self.p("n.env"))))
        self.assertEqual(proxy(), [])
        self.assertEqual(proxy(ROBOT_VISION_MCP_PORT="18110", ROBOT_VISION_MAX_DURATION_S="0.5"), [])
        self.assertEqual(proxy(ROBOT_VISION_MCP_PORT="18111"), [("config_invalid", True)])
        self.assertEqual(proxy(ROBOT_VISION_MAX_DURATION_S="2000"), [("config_invalid", True)])  # мс вместо с (N1)
        self.assertEqual(proxy(ROBOT_VISION_MAX_DURATION_S="полсекунды"), [("config_invalid", True)])


class PrecheckTest(Tmp):
    def test_dispatch_and_env(self):
        cfg = Config({"NEUROBOT_DRIVER_urg_CMD": "x", "NEUROBOT_DRIVER_urg_DEVICE": self.p("ttyACM0")}, self.p("e"))
        self.assertEqual(codes(checks.precheck("driver@urg", cfg)), [("device_missing", True)])
        self.assertEqual(codes(checks.precheck("driver@lidar", cfg)), [("env_invalid", True)])
        self.assertEqual(codes(checks.precheck("camera", cfg)), [("env_invalid", True)])
        bad_port = Config({"NEUROBOT_ROSBRIDGE_CMD": "x", "NEUROBOT_ROSBRIDGE_PORT": "порт"}, self.p("e"))
        self.assertEqual(codes(checks.precheck("rosbridge", bad_port)), [("env_invalid", True)])

    def test_graph_only_after_static_checks(self):
        calls = []

        def graph(cfg, topics):
            calls.append(topics)
            return {"/cmd_vel": {"pub": ["velocity_publisher"], "sub": ["twist_mux"]}, "/scan": {"pub": ["urg_node2"]}}
        cfg = Config({"NEUROBOT_SAFETY_CMD": "x"}, self.p("e"))
        self.assertEqual(codes(checks.precheck("safety", cfg, graph=graph)), [("cmd_vel_writer", True)])
        self.assertEqual(calls, [["/cmd_vel", "/diff_cont/cmd_vel_unstamped", "/scan", "/cmd_vel_tracker",
                                  "/cmd_vel_joy"]])
        self.assertEqual(codes(checks.precheck("safety", Config({}, self.p("e")), graph=graph)),
                         [("env_invalid", True)])
        self.assertEqual(len(calls), 1)  # без команды граф не снимаем

        def broken(cfg, topics):
            raise checks.GraphUnavailable("rclpy недоступен")
        self.assertEqual(codes(checks.precheck("executor", Config({"NEUROBOT_EXECUTOR_CMD": EXECUTOR}, self.p("e")),
                                               graph=broken)), [("ros_unavailable", True)])
        self.assertEqual(codes(checks.precheck("executor", Config({"NEUROBOT_EXECUTOR_CMD": "x"}, self.p("e")),
                                               graph=broken)), [("config_invalid", True)])  # без ремапа граф не нужен

    def test_executor_starts_only_behind_safety_gate(self):
        cfg = Config({"NEUROBOT_EXECUTOR_CMD": EXECUTOR}, self.p("e"))
        req = "/neurobot/cmd_vel_req"
        gate_listens = {req: {"pub": [], "sub": ["safety_gate"]}}
        self.assertEqual(checks.precheck("executor", cfg, graph=lambda c, t: gate_listens), [])
        (p,) = checks.precheck("executor", cfg, graph=lambda c, t: {req: {"pub": [], "sub": []}})
        self.assertEqual(p.code, "gate_missing")
        got = checks.precheck("executor", cfg, graph=lambda c, t: {req: {"pub": ["motion_executor", "teleop"],
                                                                         "sub": ["safety_gate"]}})
        self.assertEqual(codes(got), [("cmd_vel_writer", True)])
        self.assertIn("teleop", got[0].text)

    def test_labworker(self):
        put(self.p("proc/net/tcp"), TCP_HEADER + tcp_line(8766, "0A", 77))
        put(self.p("proc/net/tcp6"), TCP_HEADER)
        cfg = Config({"NEUROBOT_LABWORKER_vision_CMD": "/usr/bin/worker", "NEUROBOT_LABWORKER_vision_URL":
                      "http://127.0.0.1:8766/state"}, self.p("e"))
        (p,) = checks.precheck("labworker@vision", cfg, proc=self.p("proc"))
        self.assertEqual(p.code, "port_busy")  # второй воркер не стартует поверх живого (N7)
        self.assertIn("порт 8766", p.text)
        self.assertEqual(codes(checks.precheck("labworker@yolo", cfg, proc=self.p("proc"))),
                         [("env_invalid", True), ("env_invalid", True)])
        bad = Config({"NEUROBOT_LABWORKER_vision_CMD": "x", "NEUROBOT_LABWORKER_vision_URL": "8766"}, self.p("e"))
        self.assertEqual(codes(checks.precheck("labworker@vision", bad, proc=self.p("proc"))), [("env_invalid", True)])

    def test_perception_collects_all_problems(self):
        put(self.p("proc/1/cmdline"), "init")
        cfg = Config({"NEUROBOT_PERCEPTION_CMD": "x", "NEUROBOT_SYSFS": self.p("sys"),
                      "NEUROBOT_WEIGHTS": self.p("w.sha256")}, self.p("e"))
        got = checks.precheck("perception", cfg, proc=self.p("proc"))
        self.assertEqual(codes(got), [("config_invalid", True), ("camera_missing", True), ("config_invalid", True)])
        self.assertIn("нет ремапа /cmd_vel", got[0].text)

    def test_unsupervised_ros2_launch_is_a_warning(self):
        # приёмка: ros2 launch без обёртки переживает смерть узла (rosbridge в main_a1) — юнит active без узла
        wrapped = ("ros2 launch /opt/neurobot/share/launchfiles/supervised.launch.py "
                   "include:=urg_node2/urg_node2.launch.py")
        for cmd, want in (("ros2 launch urg_node2 urg_node2.launch.py", [("unsupervised_launch", False)]),
                          (wrapped, []), ("ros2 run urg_node2 urg_node2_node", []), ("/usr/bin/python3 x.py", [])):
            with self.subTest(cmd=cmd):
                cfg = Config({"NEUROBOT_DRIVER_lidar_CMD": cmd}, self.p("e"))
                self.assertEqual(codes(checks.precheck("driver@lidar", cfg)), want)
        cfg = Config({"NEUROBOT_ROSBRIDGE_CMD": AgentPathTest.BASE + AgentPathTest.TOPICS + AgentPathTest.SERVICES
                      + AgentPathTest.PARAMS, "NEUROBOT_ROSBRIDGE_PORT": "9"}, self.p("e"))
        self.assertIn(("unsupervised_launch", False), codes(checks.precheck("rosbridge", cfg)))

    def test_camera_driver_checks_usb(self):
        cfg = Config({"NEUROBOT_DRIVER_camera_CMD": "ros2 launch /opt/neurobot/share/launchfiles/supervised.launch.py "
                                                    "include:=realsense2_camera/rs_launch.py",
                      "NEUROBOT_DRIVER_camera_USB": "8086:0b3a", "NEUROBOT_SYSFS": self.p("sys")}, self.p("e"))
        self.assertEqual(codes(checks.precheck("driver@camera", cfg)), [("camera_missing", True)])
        put(self.p("sys/bus/usb/devices/2-1/idVendor"), "8086")
        put(self.p("sys/bus/usb/devices/2-1/idProduct"), "0b3a")
        self.assertEqual(checks.precheck("driver@camera", cfg), [])


EXECUTOR = "/usr/bin/python3 -m neurobot.executor --ros-args -p cmd_vel_type:=Twist -r /cmd_vel:=/neurobot/cmd_vel_req"


class RemapTest(unittest.TestCase):
    """Исполнитель v1 и распознавание legacy пишут «/cmd_vel» сами: в сторону его уводит только ремап в команде."""

    def check(self, component: str, cmd: str) -> list:
        return checks.check_remap(component, Config({"NEUROBOT_{}_CMD".format(component.upper()): cmd}, "/e/n.env"))

    def test_parse_rules(self):
        cmd = ("python3 x.py --mode legacy -r /a:=/b --ros-args -p v:=1 -r cmd_vel:=/q --remap /s:=t "
               "-r other:/cmd_vel:=/z -r motion_executor:/odom:=/o -r __node:=n -- -r /c:=/d --ros-args -r /e:=/f")
        self.assertEqual(checks.remaps(cmd, "motion_executor"), {"/cmd_vel": "/q", "/s": "/t", "/odom": "/o",
                                                                 "/e": "/f"})
        self.assertEqual(checks.remaps("python3 'x", "n"), {})
        # как в rcl: действует первое подходящее правило [опыт, rclpy Jazzy]
        self.assertEqual(checks.remaps("x --ros-args -r /cmd_vel:=/cmd_vel -r /cmd_vel:=/neurobot/cmd_vel_req", "n"),
                         {"/cmd_vel": "/cmd_vel"})
        self.assertEqual(checks.remaps("x --ros-args -r cmd_vel:=/a -r n:/cmd_vel:=/b -r other:/odom:=/c -r /odom:=/d",
                                       "n"), {"/cmd_vel": "/a", "/odom": "/d"})

    def test_executor(self):
        self.assertEqual(self.check("executor", EXECUTOR), [])
        self.assertEqual(self.check("executor", "python3 -m neurobot.executor --ros-args "
                                                "-r motion_executor:/cmd_vel:=/neurobot/cmd_vel_req"), [])
        for cmd, why in (("/usr/bin/python3 -m neurobot.executor", "нет ремапа /cmd_vel"),
                         ("python3 -m neurobot.executor --ros-args -r /cmd_vel:=/cmd_vel_x",
                          "ремап /cmd_vel в /cmd_vel_x"),
                         ("python3 -m neurobot.executor -r /cmd_vel:=/neurobot/cmd_vel_req", "нет ремапа"),
                         ("python3 -m neurobot.executor --ros-args -r other:/cmd_vel:=/neurobot/cmd_vel_req",
                          "нет ремапа"),
                         # второе правило rcl уже не применит: узел останется на /cmd_vel
                         ("python3 -m neurobot.executor --ros-args -r /cmd_vel:=/cmd_vel -r "
                          "/cmd_vel:=/neurobot/cmd_vel_req", "ремап /cmd_vel в /cmd_vel")):
            with self.subTest(cmd=cmd):
                (p,) = self.check("executor", cmd)
                self.assertEqual((p.code, p.fatal), ("config_invalid", True))
                self.assertIn(why, p.text)
                self.assertIn("-r /cmd_vel:=/neurobot/cmd_vel_req", p.text)

    def test_perception(self):
        ok = "python3 predict_live.py --camera ros --ros-args -r /cmd_vel:=/cmd_vel_gesture"
        self.assertEqual(self.check("perception", ok), [])
        for dst in ("", " -r /cmd_vel:=/cmd_vel", " -r /cmd_vel:=/neurobot/cmd_vel_req", " -r /cmd_vel:=/cmd_vel_joy",
                    " -r /cmd_vel:=/diff_cont/cmd_vel_unstamped"):
            with self.subTest(dst=dst):
                (p,) = self.check("perception", "python3 predict_live.py --ros-args" + dst)
                self.assertIn("-r /cmd_vel:=/cmd_vel_gesture", p.text)

    def test_camera_mode(self):
        def mode(cmd):
            return checks.camera_mode(Config({"NEUROBOT_PERCEPTION_CMD": cmd}, "/e/n.env"))
        self.assertEqual([mode("p.py"), mode("p.py --camera ros"), mode("p.py --camera=ros"),
                          mode("p.py --ros-args -p --camera ros"), mode("p.py --camera")],
                         ["rs", "ros", "ros", "rs", "rs"])


class OwnGraphTest(unittest.TestCase):
    """wait-graph: после READY узел исполнителя или распознавания не пишет в цепочку движения мимо safety_gate."""
    cfg = Config({}, "/e/n.env")

    def test_topics(self):
        self.assertEqual(checks.own_topics(self.cfg), [
            "/cmd_vel", "/diff_cont/cmd_vel_unstamped", "/cmd_vel_tracker", "/cmd_vel_joy", "/neurobot/cmd_vel_req"])

    def test_executor(self):
        good = {"/cmd_vel": {"pub": ["safety_gate"]}, "/neurobot/cmd_vel_req": {"pub": ["motion_executor"]}}
        self.assertEqual(checks.own_problems("executor", self.cfg, good), [])
        # потерянный ремап: одна причина — писатель мимо safety_gate (doctor берёт последнюю строку отказа)
        lost = {"/cmd_vel": {"pub": ["safety_gate", "motion_executor"]}, "/neurobot/cmd_vel_req": {"pub": []}}
        (p,) = checks.own_problems("executor", self.cfg, lost)
        self.assertEqual((p.code, p.fatal), ("cmd_vel_writer", True))
        self.assertIn("узел motion_executor пишет в /cmd_vel — мимо safety_gate", p.text)
        silent = {"/cmd_vel": {"pub": ["safety_gate"]}, "/neurobot/cmd_vel_req": {"pub": []}}
        (p,) = checks.own_problems("executor", self.cfg, silent)
        self.assertEqual(p.code, "config_invalid")
        self.assertIn("не пишет в /neurobot/cmd_vel_req", p.text)

    def test_perception(self):
        good = {"/cmd_vel": {"pub": ["safety_gate"]}, "/neurobot/cmd_vel_req": {"pub": ["motion_executor"]}}
        self.assertEqual(checks.own_problems("perception", self.cfg, good), [])
        for topic in ("/cmd_vel", "/neurobot/cmd_vel_req", "/cmd_vel_tracker"):
            with self.subTest(topic=topic):
                (p,) = checks.own_problems("perception", self.cfg, {topic: {"pub": ["velocity_publisher"]}})
                self.assertEqual(p.code, "cmd_vel_writer")
                self.assertIn("velocity_publisher пишет в " + topic, p.text)


class WriterMarkTest(Tmp):
    """Метка wait-graph: с той же командой precheck не запускает узел, который писал мимо safety_gate."""
    LOST = [checks.Problem("cmd_vel_writer", "узел motion_executor пишет в /cmd_vel — мимо safety_gate (S4)")]

    def cfg(self, cmd: str = EXECUTOR) -> Config:
        return Config({"NEUROBOT_EXECUTOR_CMD": cmd}, self.p("e"))

    def test_same_command_is_refused_until_changed(self):
        self.assertEqual(checks.check_writer_mark("executor", self.cfg(), self.tmp), [])
        self.assertEqual(checks.check_writer_mark("executor", self.cfg(), ""), [])  # не из юнита — метки нет
        path = checks.mark_writer("executor", self.cfg(), self.tmp, self.LOST, 1760540000.0)
        self.assertEqual(os.listdir(self.tmp), ["cmd_vel_writer.json"])
        for _ in range(2):  # метка не расходуется: каждый рестарт с той же командой — отказ
            (p,) = checks.check_writer_mark("executor", self.cfg(), self.tmp)
            self.assertEqual((p.code, p.fatal), ("cmd_vel_writer", True))
            self.assertIn("пишет в /cmd_vel — мимо safety_gate", p.text)
            self.assertIn("NEUROBOT_EXECUTOR_CMD с тех пор не менялась", p.text)
            self.assertIn("sudo rm " + path, p.text)
        fixed = self.cfg(EXECUTOR + " -p use_sim_time:=false")
        self.assertEqual(checks.check_writer_mark("executor", fixed, self.tmp), [])  # команду исправили — снята
        self.assertFalse(os.path.exists(path))

    def test_unreadable_mark_is_refused(self):
        put(self.p("cmd_vel_writer.json"), "{обрыв")
        (p,) = checks.check_writer_mark("perception", Config({}, self.p("e")), self.tmp)
        self.assertEqual(p.code, "cmd_vel_writer")
        self.assertIn("время неизвестно", p.text)

    def test_precheck_refuses_before_graph(self):
        calls = []

        def graph(cfg, topics):
            calls.append(topics)
            return {"/neurobot/cmd_vel_req": {"pub": [], "sub": ["safety_gate"]}}
        self.assertEqual(checks.precheck("executor", self.cfg(), graph=graph, state=self.tmp), [])
        checks.mark_writer("executor", self.cfg(), self.tmp, self.LOST, 1760540000.0)
        self.assertEqual(codes(checks.precheck("executor", self.cfg(), graph=graph, state=self.tmp)),
                         [("cmd_vel_writer", True)])
        self.assertEqual(len(calls), 1)  # с меткой граф не снимаем
        perception = Config({"NEUROBOT_PERCEPTION_CMD": "python3 p.py --ros-args -r /cmd_vel:=/cmd_vel_gesture",
                             "NEUROBOT_SYSFS": self.p("sys"), "NEUROBOT_WEIGHTS": self.p("w.sha256")}, self.p("e"))
        os.makedirs(self.p("perception"))
        checks.mark_writer("perception", perception, self.p("perception"), self.LOST, 1760540000.0)
        got = checks.precheck("perception", perception, proc=self.p("proc"), state=self.p("perception"))
        self.assertEqual(codes(got)[0], ("cmd_vel_writer", True))


if __name__ == "__main__":
    unittest.main()
