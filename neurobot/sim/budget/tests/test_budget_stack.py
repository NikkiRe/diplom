"""Состав стека и команды systemd: порядок, роли, супервизия как у deploy, домены, slice, конфиги шага."""
import getpass
import json
import os
import sys

import pytest

from neurobot.sim.budget import stack as S
from neurobot.sim.budget.model import load

ROOT = S.PROJ


@pytest.fixture
def cfg(tmp_path):
    return S.Cfg(user="jetson", base="/w", step_dir=str(tmp_path / "step"), mem_mib=3336, quota_pct=102,
                 ratio=0.25, gestures_dir="/g", twin_run="/t", model=load(), ml_python="/ml/python")


def props(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]


def env(argv):
    return dict(a[len("--setenv="):].split("=", 1) for a in argv if a.startswith("--setenv="))


def test_order_follows_plan_and_roles(cfg):
    names = [s.name for s in S.specs(cfg)]
    assert names == ["base", "camera", "camera_streams", "lidar", "safety", "executor", "perception", "rosbridge",
                     "mcp", "picoclaw"]
    roles = {s.name: s.role for s in S.specs(cfg)}
    assert {n for n, r in roles.items() if r == "proxy"} == {"base", "camera", "lidar"}
    assert roles["camera_streams"] == "ballast"


def test_supervision_like_deploy_units(cfg):
    sp = {s.name: s for s in S.specs(cfg)}
    p = props(S.unit_argv(sp["perception"], cfg))
    for want in ("Type=notify", "NotifyAccess=all", "WatchdogSec=5s", "Restart=always", "RestartSec=1s",
                 "KillSignal=SIGINT", "TimeoutStartSec=120s"):
        assert want in p
    assert "WatchdogSec=3s" in props(S.unit_argv(sp["executor"], cfg))
    assert "RestartSec=0.5s" in props(S.unit_argv(sp["safety"], cfg))
    assert "Type=exec" in props(S.unit_argv(sp["rosbridge"], cfg))
    assert not any(x.startswith("WatchdogSec") for x in props(S.unit_argv(sp["mcp"], cfg)))


def test_unit_in_slice_as_user_with_logs_and_domains(cfg):
    sp = {s.name: s for s in S.specs(cfg)}
    argv = S.unit_argv(sp["perception"], cfg)
    assert argv[:6] == ["systemd-run", "--unit", "jetson-perception.service", "--slice", "jetson.slice", "--uid"]
    assert argv[6] == "jetson" and "--no-block" in argv
    assert "StandardOutput=append:%s/logs/perception.log" % cfg.step_dir in props(argv)
    assert env(argv)["ROS_DOMAIN_ID"] == "86" and env(argv)["ROS_LOG_DIR"].startswith(cfg.step_dir)
    assert env(S.unit_argv(sp["base"], cfg))["ROS_DOMAIN_ID"] == "87"
    tail = argv[argv.index("--") + 1:]
    assert tail[:2] == S.WRAP and tail[2:6] == ["/ml/python", "-m", "neurobot.sim.budget.ballast", "--mib"]
    assert tail[6] == "1024" and "--camera" in tail and tail[tail.index("--camera") + 1] == "ros"
    assert "/cmd_vel:=/cmd_vel_gesture" in tail


def test_perception_env_points_to_step_and_timeline(cfg):
    e = {s.name: s for s in S.specs(cfg)}["perception"].env
    assert e["NEUROBOT_GESTURES_TIMELINE"] == "/g/timeline.jsonl"
    assert e["NEUROBOT_GESTURES_ANCHOR"] == os.path.join(cfg.step_dir, "anchor.json")
    assert e["PYTHONPATH"].split(":") == [ROOT, os.path.join(ROOT, "neurobot", "sim", "gestures", "shim")]


def test_camera_streams_cpu_scaled_to_host(cfg):
    argv = {s.name: s for s in S.specs(cfg)}["camera_streams"].argv
    assert argv[argv.index("--cpu-pct") + 1] == "5.00"         # 20 % ядра A57 × 0,25
    assert argv[argv.index("--mib") + 1] == "30" and "--net" in argv


def test_rosbridge_only_localhost_and_deploy_globs(cfg):
    argv = {s.name: s for s in S.specs(cfg)}["rosbridge"].argv
    assert "address:=127.0.0.1" in argv and "port:=19186" in argv
    assert "topics_glob:='[/cmd_vel_agent]'" in argv and "params_glob:='[none]'" in argv


def test_slice_props(cfg):
    assert S.slice_props(cfg) == ["MemoryMax=3336M", "MemorySwapMax=0", "CPUQuota=102%", "CPUQuotaPeriodSec=20ms",
                                  "AllowedCPUs=0-3", "TasksMax=2048"]


def test_adopt_argv():
    a = S.adopt_argv("twist_mux", 4321)
    assert a[a.index("StartTransientUnit") + 2] == "jetson-twist_mux.scope"
    assert a[-8:] == ["PIDs", "au", "1", "4321", "Slice", "s", "jetson.slice", "0"]


def test_mock_urdf():
    text = "<robot><ros2_control>%s</ros2_control></robot>" % S.TWIN_HW
    assert "mock_components/GenericSystem" in S.mock_urdf(text) and S.TWIN_HW not in S.mock_urdf(text)
    for bad in ("<robot/>", text.replace("</robot>", S.TWIN_HW + "</robot>")):
        with pytest.raises(ValueError):
            S.mock_urdf(bad)


def test_mcp_config_ports_and_limits_kept(cfg):
    with open(os.path.join(ROOT, "neurobot", "deploy", "mcp", "ros_mcp.json.example"), encoding="utf-8") as f:
        ex = json.load(f)
    c = S.mcp_config(ex, cfg)
    assert c["server"] == {"host": "127.0.0.1", "port": 18186} and c["rosbridge"]["url"] == "ws://127.0.0.1:19186"
    assert c["motion"] == ex["motion"] and c["topics"]["cmd_vel"]["name"] == "/cmd_vel_agent"
    assert ex["server"]["port"] == 18110                         # пример не изменён


def test_picoclaw_config_fits_028_schema(cfg):
    sys.path.insert(0, os.path.join(ROOT, "neurobot", "deploy"))
    from neurobot_deploy.picoclaw_cfg import load_schema, unknown_fields
    with open(os.path.join(ROOT, "neurobot", "deploy", "fakes", "picoclaw.json"), encoding="utf-8") as f:
        c = S.picoclaw_config(json.load(f), cfg)
    schema, _ = load_schema("0.2.8")
    assert unknown_fields(c, schema) == []
    assert c["gateway"]["host"] == "127.0.0.1" and c["gateway"]["log_level"] == "info"
    assert c["tools"]["mcp"]["servers"]["robot_mcp"] == {"enabled": True, "type": "http",
                                                         "url": "http://127.0.0.1:18186/mcp"}


def test_twin_pids_only_in_group():
    procs = [(10, 5, "/opt/ros/jazzy/lib/twist_mux/twist_mux --ros-args"),
             (11, 5, "/opt/ros/jazzy/lib/robot_state_publisher/robot_state_publisher --ros-args"),
             (12, 5, "/opt/ros/jazzy/lib/imu_filter_madgwick/imu_filter_madgwick_node --ros-args"),
             (13, 9, "/opt/ros/jazzy/lib/twist_mux/twist_mux"),          # чужой двойник
             (14, 5, "python3 cmd_relay.py")]
    assert S.twin_pids(procs, 5) == {"twist_mux": 10, "robot_state_publisher": 11, "imu_filter_madgwick": 12}
    assert S.twin_pids(procs, 9) == {"twist_mux": 13}


def test_requires_root(monkeypatch):
    monkeypatch.setattr(S.os, "geteuid", lambda: 1000)
    assert S.main(["status"]) == 2


@pytest.mark.parametrize("args", [["--mem", "3336,x"], ["--mem", "128"], ["--mem", "3336,2048", "--labels", "a"],
                                  ["--mem", "3336", "--quota", "2"], ["--mem", "3336", "--ratio", "1.5"]])
def test_run_rejects_bad_arguments_before_acting(monkeypatch, tmp_path, args):
    monkeypatch.setattr(S.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", getpass.getuser())
    monkeypatch.setattr(S, "sh", lambda *a, **k: pytest.fail("ничего не запускать при неверных аргументах"))
    argv = ["run", "--out", str(tmp_path / "run"), "--quota", "106", "--ratio", "0.26"] + args
    assert S.main(argv) == 2 and not (tmp_path / "run").exists()
