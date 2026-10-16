"""neurobot/deploy: env-файл (правила EnvironmentFile=, приоритеты) и отрисовка юнитов. Только stdlib, Python 3.8+."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))

from neurobot_deploy import checks, render  # noqa: E402
from neurobot_deploy.config import Config, EnvError, load, parse_env, quote  # noqa: E402


def write_env(tmp: str, text: str) -> str:
    path = os.path.join(tmp, "neurobot.env")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class ParseEnvTest(unittest.TestCase):
    def test_quotes_comments_and_escapes(self):
        env = parse_env('# комментарий\n; тоже\n\nA=1\nB="два слова"\nC=\'$X "q"\'\nD="a\\"b\\\\c\\$d"\n'
                        'E=  пробелы по краям  \nF=\n')
        self.assertEqual(env, {"A": "1", "B": "два слова", "C": '$X "q"', "D": 'a"b\\c$d', "E": "пробелы по краям",
                               "F": ""})

    def test_trailing_comment_is_part_of_value(self):
        # как у systemd: комментарий — только целой строкой
        self.assertEqual(parse_env("A=1 # не комментарий")["A"], "1 # не комментарий")

    def test_bad_line_names_file_and_line(self):
        with self.assertRaisesRegex(EnvError, r"env:2: ожидалось"):
            parse_env("A=1\nexport B=2\n")

    def test_quote_round_trip_matches_bash(self):
        # drop-in start_test и сценариев стенда: команда с кавычками YAML, $ и обратной чертой — то же значение у
        # parse_env (правила EnvironmentFile=) и у bash. С самим systemd сверено отдельно [опыт, ВМ: systemd 255,
        # systemd-run и EnvironmentFile= — байт в байт; 245 не проверялся]
        value = "sh -c 'exec sleep 3600' ros2 launch x topics_glob:=\"'[/a]'\" $HOME `id` a\\b"
        line = "K=" + quote(value)
        self.assertEqual(parse_env(line), {"K": value})
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(["bash", "-c", 'source "$1"; printf %s "$K"', "bash", write_env(tmp, line + "\n")],
                               capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        self.assertEqual(r.stdout, value)


class LoadTest(unittest.TestCase):
    def test_process_env_overrides_neurobot_keys_but_not_ros(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_env(tmp, "NEUROBOT_CAMERA_USB=8086:0b3a\nROS_DOMAIN_ID=7\n")
            cfg = load(path, {"NEUROBOT_CAMERA_USB": "ffff:ffff", "ROS_DOMAIN_ID": "99", "PICOCLAW_HOME": "/p"})
            self.assertEqual(cfg.get("NEUROBOT_CAMERA_USB"), "ffff:ffff")
            self.assertEqual(cfg.ros_environ(), {"ROS_DOMAIN_ID": "7"})
            self.assertEqual(cfg.picoclaw_config(), "/p/config.json")

    def test_path_from_neurobot_env_and_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_env(tmp, "NEUROBOT_DRIVERS=urg\n")
            self.assertEqual(load(None, {"NEUROBOT_ENV": path}).drivers(), ["urg"])
            with self.assertRaisesRegex(EnvError, "env-файл"):
                load(os.path.join(tmp, "нет.env"), {})

    def test_defaults_and_plan_order(self):
        cfg = Config({"NEUROBOT_DRIVERS": "urg base", "NEUROBOT_COMPONENTS": "picoclaw executor mcp safety"},
                     "/x/n.env")
        self.assertEqual(cfg.plan(), [
            ("driver@urg", "neurobot-driver@urg.service"), ("driver@base", "neurobot-driver@base.service"),
            ("safety", "neurobot-safety.service"), ("executor", "neurobot-executor.service"),
            ("mcp", "neurobot-mcp.service"), ("picoclaw", "neurobot-picoclaw.service")])
        self.assertEqual(cfg.port("NEUROBOT_ROSBRIDGE_PORT"), 9090)
        self.assertEqual(cfg.mcp_endpoint(), ("127.0.0.1", 18110, "/mcp"))

    def test_labworkers_start_between_rosbridge_and_mcp(self):
        cfg = Config({"NEUROBOT_LABWORKERS": "yolo vision", "NEUROBOT_LABWORKER_yolo_URL": "http://127.0.0.1:8765/health"},
                     "/x/n.env")
        self.assertEqual([c for c, _ in cfg.plan()], ["safety", "executor", "perception", "rosbridge", "labworker@yolo",
                                                      "labworker@vision", "mcp", "picoclaw"])
        self.assertEqual(cfg.labworker_url("yolo"), ("127.0.0.1", 8765, "/health"))
        only_agent = Config({"NEUROBOT_LABWORKERS": "vision", "NEUROBOT_COMPONENTS": "mcp"}, "/x/n.env")
        self.assertEqual([u for _, u in only_agent.plan()],
                         ["neurobot-labworker@vision.service", "neurobot-mcp.service"])

    def test_sensors(self):
        cfg = Config({"NEUROBOT_SENSORS": "/odom:nav_msgs/msg/Odometry:10 /tf:tf2_msgs/msg/TFMessage"}, "/x/n.env")
        self.assertEqual(cfg.sensors(), [("/odom", "nav_msgs/msg/Odometry", 10.0),
                                         ("/tf", "tf2_msgs/msg/TFMessage", 0.0)])
        self.assertEqual(Config({}, "/x/n.env").sensors(), [])

    def test_invalid_values_are_explained(self):
        cases = (({"NEUROBOT_COMPONENTS": "executor camera"}, lambda c: c.components(), "неизвестные"),
                 ({"NEUROBOT_SENSORS": "/odom:Odometry"}, lambda c: c.sensors(), "NEUROBOT_SENSORS"),
                 ({"NEUROBOT_DRIVERS": "Urg-1"}, lambda c: c.drivers(), "NEUROBOT_DRIVERS: имя экземпляра"),
                 ({"NEUROBOT_LABWORKERS": "yolo-8765"}, lambda c: c.labworkers(), "NEUROBOT_LABWORKERS"),
                 ({"NEUROBOT_LABWORKER_yolo_URL": "127.0.0.1:8765"}, lambda c: c.labworker_url("yolo"),
                  "NEUROBOT_LABWORKER_yolo_URL"),
                 ({"NEUROBOT_ROSBRIDGE_PORT": "70000"}, lambda c: c.port("NEUROBOT_ROSBRIDGE_PORT"), "порт"),
                 ({"NEUROBOT_MCP_URL": "https://h/mcp"}, lambda c: c.mcp_endpoint(), "NEUROBOT_MCP_URL"),
                 ({"NEUROBOT_WAIT_S": "долго"}, lambda c: c.num("NEUROBOT_WAIT_S"), "число"))
        for values, call, pattern in cases:
            with self.subTest(values=values), self.assertRaisesRegex(EnvError, pattern):
                call(Config(values, "/x/n.env"))


class RenderTest(unittest.TestCase):
    def render_with(self, text: str, files: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            units = os.path.join(tmp, "units")
            os.mkdir(units)
            for name, body in files.items():
                with open(os.path.join(units, name), "w", encoding="utf-8") as f:
                    f.write(body)
            return render.render(load(write_env(tmp, text), {}), units)

    def test_placeholders_and_driver_line(self):
        out = self.render_with("NEUROBOT_USER=robot\nNEUROBOT_DRIVERS=\"urg bridge\"\nNEUROBOT_PREFIX=/opt/nb/\n"
                               "NEUROBOT_SUPPLEMENTARY_GROUPS=\"dialout  video\"\n",
                               {"x.service": "[Unit]\n@AFTER_DRIVERS@\n[Service]\nUser=@USER@\n"
                                             "SupplementaryGroups=@SUPPLEMENTARY_GROUPS@\nExecStart=@PREFIX@/bin/x\n"
                                             "EnvironmentFile=@ETC@/neurobot.env\n"})["x.service"]
        self.assertIn("After=neurobot-driver@urg.service neurobot-driver@bridge.service\n", out)
        self.assertIn("User=robot\nSupplementaryGroups=dialout video\nExecStart=/opt/nb/bin/x\n", out)
        self.assertNotIn("@", out.replace("driver@", ""))

    def test_no_drivers_removes_after_line(self):
        out = self.render_with("NEUROBOT_DRIVERS=\n", {"x.service": "[Unit]\n@AFTER_DRIVERS@\nPartOf=a\n"})
        self.assertEqual(out["x.service"], "[Unit]\nPartOf=a\n")

    def test_labworkers_line_and_picoclaw_home(self):
        out = self.render_with("NEUROBOT_LABWORKERS=\"yolo vision\"\nPICOCLAW_HOME=/home/j/.picoclaw\n",
                               {"x.service": "[Unit]\n@AFTER_LABWORKERS@\n",
                                "rot": "@PICOCLAW_HOME@/logs/gateway.log\n"})
        self.assertEqual(out["x.service"],
                         "[Unit]\nAfter=neurobot-labworker@yolo.service neurobot-labworker@vision.service\n")
        self.assertEqual(out["rot"], "/home/j/.picoclaw/logs/gateway.log\n")

    def test_camera_has_one_owner(self):
        env = "NEUROBOT_LABWORKERS=vision\nNEUROBOT_LABWORKER_vision_CAMERA=yes\n"
        with self.assertRaisesRegex(EnvError, "один владелец"):
            self.render_with(env, {"x.service": ""})
        self.render_with(env + "NEUROBOT_COMPONENTS=\"safety executor rosbridge mcp picoclaw\"\n", {"x.service": ""})

    def test_executor_needs_safety_gate(self):
        with self.assertRaisesRegex(EnvError, "исполнитель без safety"):
            self.render_with("NEUROBOT_COMPONENTS=\"executor perception\"\n", {"x.service": ""})
        self.render_with("NEUROBOT_COMPONENTS=\"safety perception\"\n", {"x.service": ""})

    def test_unknown_placeholder_and_bad_values_fail(self):
        with self.assertRaisesRegex(EnvError, "не подставлены"):
            self.render_with("", {"x.service": "User=@WHO@\n"})
        for env, pattern in (("NEUROBOT_USER=\"two words\"\n", "NEUROBOT_USER"),
                             ("NEUROBOT_PREFIX=opt/nb\n", "NEUROBOT_PREFIX"),
                             ("NEUROBOT_SUPPLEMENTARY_GROUPS=\"ok Bad!\"\n", "SUPPLEMENTARY")):
            with self.assertRaisesRegex(EnvError, pattern):
                self.render_with(env, {"x.service": ""})

    def test_enable_list(self):
        cfg = Config({"NEUROBOT_DRIVERS": "urg", "NEUROBOT_COMPONENTS": "safety executor perception"}, "/e/n.env")
        self.assertEqual(render.enable_list(cfg), ["neurobot.target", "neurobot-driver@urg.service",
                                                   "neurobot-safety.service", "neurobot-executor.service",
                                                   "neurobot-perception.service"])
        cfg = Config({"NEUROBOT_COMPONENTS": "safety executor mcp", "NEUROBOT_LABWORKERS": "vision"}, "/e/n.env")
        self.assertEqual(render.enable_list(cfg)[-3:], ["neurobot-labworker@vision.service", "neurobot-mcp.service",
                                                        "neurobot-watchdog.service"])


def env_file(name: str) -> Config:
    return Config(parse_env((DEPLOY / name).read_text(encoding="utf-8")), "/etc/neurobot/neurobot.env")


class RobotProfileTest(unittest.TestCase):
    """Профиль робота (neurobot.env.example) против контракта тем и безопасности v2 (R7a, PLAN §13.3)."""
    cfg = env_file("neurobot.env.example")

    def test_topics_as_on_robot(self):
        want = {"NEUROBOT_CMD_VEL": "/cmd_vel", "NEUROBOT_CMD_VEL_TYPE": "geometry_msgs/msg/Twist",
                "NEUROBOT_CMD_VEL_REQ": "/neurobot/cmd_vel_req", "NEUROBOT_MUX_NODE": "twist_mux",
                "NEUROBOT_BASE_CMD_TOPIC": "/diff_cont/cmd_vel_unstamped",
                "NEUROBOT_OVERRIDE_TOPICS": "/cmd_vel_tracker", "NEUROBOT_JOY_TOPICS": "/cmd_vel_joy",
                "NEUROBOT_SCAN_TOPIC": "/scan", "NEUROBOT_SCAN_FRAME": "laser_frame",
                "NEUROBOT_SAFETY_STATE_TOPIC": "/neurobot/safety_state", "NEUROBOT_ESTOP_TOPIC": "/neurobot/estop"}
        self.assertEqual({k: self.cfg.get(k) for k in want}, want)
        self.assertEqual({t: typ for t, typ, _ in self.cfg.sensors()}, {
            "/odom": "nav_msgs/msg/Odometry", "/tf": "tf2_msgs/msg/TFMessage",
            "/joint_states": "sensor_msgs/msg/JointState", "/camera/color/image_raw": "sensor_msgs/msg/Image",
            "/camera/depth/image_rect_raw": "sensor_msgs/msg/Image", "/camera/imu": "sensor_msgs/msg/Imu",
            "/imu/data": "sensor_msgs/msg/Imu"})

    def test_camera_is_a_driver_and_perception_reads_its_topic(self):
        # глубина и IMU D435i — датчики safety_gate, их публикует realsense2_camera; распознавание — --camera ros
        topics = {self.cfg.driver(d, "TOPIC"): d for d in self.cfg.drivers()}
        self.assertEqual(set(topics), {"/scan", "/odom", "/camera/color/image_raw", "/imu/data"})
        self.assertIn("realsense2_camera", self.cfg.driver(topics["/camera/color/image_raw"], "CMD"))
        self.assertEqual(self.cfg.driver(topics["/camera/color/image_raw"], "USB"), self.cfg.get("NEUROBOT_CAMERA_USB"))
        self.assertEqual(checks.camera_mode(self.cfg), "ros")
        self.assertIn("realsense2_camera_node", self.cfg.get("NEUROBOT_FOREIGN_PERCEPTION").split())

    def test_writers_are_remapped_on_robot_and_stand(self):
        for name in ("neurobot.env.example", "fakes/fake.env"):
            cfg = env_file(name)
            with self.subTest(env=name):
                self.assertEqual(checks.check_remap("executor", cfg) + checks.check_remap("perception", cfg), [])
                self.assertEqual(checks.check_rosbridge(cfg), [])


if __name__ == "__main__":
    unittest.main()
