"""Готовность двойника (twin_ready.py) и сводка проверки (twin_summary.py) без ROS: что названо недостающим, какие
критерии выполнены на синтетических прогонах, что сводка пишет всегда."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TWIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TWIN))

import twin_summary as sc  # noqa: E402
import twin_config as cfg  # noqa: E402
import twin_ready as tr  # noqa: E402

LINKS = {(cfg.CMD_VEL, "sub"): ["twist_mux"], (cfg.MUX_OUT, "pub"): ["twist_mux"],
         (cfg.MUX_OUT, "sub"): ["twin_cmd_relay"], (cfg.DIFF_CMD, "sub"): ["diff_cont"]}
ACTIVE = {"diff_cont": "active", "joint_broad": "active"}


class ReadyTest(unittest.TestCase):
    def test_all_present(self):
        self.assertEqual(tr.missing(set(tr.MSG_TOPICS), set(tr.STATIC_FRAMES), ACTIVE, LINKS), [])

    def test_names_what_is_missing(self):
        seen = set(tr.MSG_TOPICS) - {"/scan", "/odom"}
        links = {**LINKS, (cfg.MUX_OUT, "sub"): []}
        lack = tr.missing(seen, {"laser_frame"}, {"diff_cont": "inactive"}, links)
        self.assertEqual(lack, ["/scan: нет сообщений", "/odom: нет сообщений", "/tf_static: нет кадра camera_link",
                                "controller_manager: diff_cont — inactive",
                                "controller_manager: joint_broad — не загружен",
                                "/diff_cont/cmd_vel_unstamped: twin_cmd_relay не подписан"])

    def test_odom_required_and_hold_reported(self):
        """/odom нужен и в hold: начальный ноль контроллеру даёт twin_cmd_relay; режим берётся из controllers.yaml."""
        self.assertEqual(tr.missing(set(tr.MSG_TOPICS) - {"/odom"}, set(tr.STATIC_FRAMES), ACTIVE, LINKS),
                         ["/odom: нет сообщений"])
        with tempfile.TemporaryDirectory() as d:
            for timeout, hold in ((0.0, True), (0.5, False)):
                (Path(d) / "controllers.yaml").write_text("diff_cont:\n  ros__parameters:\n    cmd_vel_timeout: %s\n"
                                                          % timeout)
                self.assertEqual(tr.hold_mode(d), hold)


def passing_runs() -> dict:
    """Прогоны, на которых все критерии выполнены."""
    hz = {t: v[1] for t, v in cfg.SENSORS.items()}
    types = {t: [v[0]] for t, v in cfg.SENSORS.items()}
    reach = {f: [0.0, 0.0, 0.0] for f in cfg.TF_FRAMES}
    reach.update({f: [0.0, 0.0, z] for f, z in sc.FRAME_Z.items()})
    ready = {"ready": True, "missing": [], "hold": False}
    lab_sensors = {
        "windows": {"camera_off": {"rtf": 0.98, "hz": hz}, "camera_on": {"rtf": 0.95, "hz": hz}},
        "types": types, "tf_from_odom": reach,
        "scan": {"n": cfg.SCAN_N, "angle_min": -cfg.SCAN_ANGLE, "angle_max": cfg.SCAN_ANGLE,
                 "range_min": cfg.SCAN_RANGE[0], "range_max": cfg.SCAN_RANGE[1], "frame_id": cfg.SCAN_FRAME,
                 "front30_min_finite_m": 4.03,
                 "blind": {"main": {"first": 168, "last": 352, "holes": [213], "robot_first": 168,
                                    "robot_last": 353, "deg": [-60.8, 3.9]}}},
        "imu": {"tilt_accel_deg": 0.02, "tilt_madgwick_deg": -0.2},
        "depth": {"encoding": "16UC1", "frame_id": "camera_depth_optical_frame",
                  "profile": [[1.2, 50, 0.0, 0.02], [1.3, 1800, 0.07, 0.17], [1.4, 300, 0.15, 0.17],
                              [2.0, 30, 0.0, 0.03]]},
        "truth": {"z": 0.0}}
    step_sensors = {"types": types, "tf_from_odom": reach, "truth": {"z": 0.15},
                    "depth": {"profile": [[1.2, 40, 0.0, 0.02], [1.4, 30, 0.01, 0.03], [1.8, 20, -0.15, -0.13]]}}
    tilt = {"tilt_truth_deg": 12.0, "tilt_accel_deg": 12.1, "tilt_madgwick_deg": 11.5}
    return {
        "xacro": {"check_urdf": "rc=0", "check_urdf_ok": True, "gz_sdf": "rc=0", "gz_sdf_ok": True},
        "lab": {"ready": ready, "sensors": lab_sensors,
                "mux": {"joy_over_cmd": {"values_after_0_6s": [0.0], "truth_moved_m": 0.0},
                        "cmd_alone": {"values_after_0_6s": [0.1], "first_expected_s": 0.5},
                        "tracker_over_cmd": {"values_after_0_6s": [0.05]}},
                "silence": {"writers_before": {t: [] for t in cfg.MUX_INPUTS}, "brake_s": 0.5, "truth_stop_s": 0.77,
                            "contacts": [], "probe_rtf": 0.97},
                "odom": {"pos_err_end_m": 0.0, "yaw_err_end_deg": 0.1},
                "truth": {"set_pose": {"clearance_m": 0.35, "nearest": "person/leg_l",
                                       "drive_contacts": ["person"], "drive_min_clearance_m": 0.0},
                          "create": {"unknown_seen": True, "null_while_unknown": True, "known_after_s": 0.7,
                                     "clearance_m": 0.276, "part": "caster_front/0", "drive_contacts": ["cable"]},
                          "remove": {"gone": True}, "rtf": {"last": 0.99, "last_ok": True}, "probe_rtf": 0.98}},
        "hold": {"ready": dict(ready, hold=True),
                 "silence": {"brake_s": None, "out_zero_s": None, "contacts": ["low_box"], "odom_path_m": 2.0,
                             "odom_vs_truth": {"path_m": 1.16}, "probe_rtf": 0.95},
                 "observer": {"cmd_series": {cfg.CMD_VEL: 1, cfg.MUX_OUT: 1}, "touch_walls": ["low_box/link/c"],
                              "type_mismatch": 0}},
        "ramp": {"ready": ready, "sensors": {"types": types, "tf_from_odom": reach},
                 "ramp": {"standing": dict(tilt, slide_m=0.0), "after_climb": tilt, "probe_rtf": 0.99}},
        "step": {"ready": ready, "sensors": step_sensors,
                 "crash": {"group_gone_after_s": 2.3,
                           "gz_sim_left": [{"cmd": "gz sim /home/u/robot_sim/x/world.sdf"}]}},   # стенд — не наш
    }


class SummaryTest(unittest.TestCase):
    def failed(self, runs):
        return [c["name"] for c in sc.checks(runs) if not c["ok"]]

    def test_all_pass(self):
        self.assertEqual(self.failed(passing_runs()), [])

    def test_failures_are_named(self):
        runs = passing_runs()
        bad = copy.deepcopy(runs)
        bad["lab"]["sensors"]["windows"]["camera_on"]["rtf"] = 0.5
        bad["lab"]["sensors"]["scan"]["blind"]["main"]["first"] = 160
        bad["lab"]["silence"]["brake_s"] = None
        bad["lab"]["silence"]["probe_rtf"] = 0.3
        bad["hold"]["silence"]["brake_s"] = 0.5
        bad["lab"]["sensors"]["scan"]["front30_min_finite_m"] = 1.4
        bad["step"]["sensors"]["depth"]["profile"] = []
        bad["step"]["crash"]["gz_sim_left"].append({"cmd": "gz sim /home/u/twin_runs/y/world.sdf"})
        bad["lab"]["truth"]["set_pose"]["nearest"] = "wall_west/c"            # позу из set_pose истина не видит
        bad["lab"]["truth"]["create"]["drive_contacts"] = []                  # касание ролика не видно
        bad["lab"]["truth"]["create"]["null_while_unknown"] = False
        bad["lab"]["truth"]["rtf"]["last_ok"] = False
        bad["hold"]["ready"]["hold"] = False
        self.assertEqual(self.failed(bad), [
            "lab: RTF, окно camera_on", "lab: слепой сектор (NaN от корпуса D435i) — лучи против скана робота R7b §3.3",
            "lab: лидар low_box не видит — ближайшее конечное ±30°, м",
            "lab: RTF во время проб движения (наименьший)", "lab: тишина /cmd_vel → контроллер тормозит через, с",
            "lab: истина — set_pose человека перед роботом: зазор, м",
            "lab: истина — create: пока формы нет, clearance_m = null",
            "lab: истина — наезд роликом на кабель 4 см (ниже палуб): касание",
            "lab: истина — RTF в /twin/truth (последнее значение, rtf_ok)",
            "hold: /odom есть до первой команды (готовность в hold ждёт /odom)",
            "hold: тишина /cmd_vel — контроллер не тормозит",
            "step: глубина видит пол за краем (z < −0,1), ближний бин x, м",
            "step: глубина видит площадку до края, дальний бин x, м",
            "step: kill -9 sensor_model — gz sim двойника в ВМ не осталось"])

    def test_missing_run_fails_not_skipped(self):
        runs = passing_runs()
        del runs["ramp"]
        names = self.failed(runs)
        self.assertIn("ramp: двойник готов", names)
        self.assertIn("ramp: стоянка на склоне — сползание, м", names)

    def test_observer_events(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "observer.jsonl"
            p.write_text("\n".join(json.dumps(e) for e in (
                {"header": {}}, {"ev": "cmd", "topic": "/cmd_vel"}, {"ev": "cmd", "topic": "/cmd_vel"},
                {"ev": "wall", "touch": True, "wall": "low_box/link/c"}, {"ev": "wall", "touch": False}))
                         + "\nне json\n")
            self.assertEqual(sc.observer_events(p), {"cmd_series": {"/cmd_vel": 2}, "touch_walls": ["low_box/link/c"],
                                                     "type_mismatch": 0})
            self.assertEqual(sc.observer_events(Path(d) / "нет.jsonl"), {})

    def test_cli_writes_summary_and_manifest_on_empty_run(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "command.txt").write_text("check.sh\n")
            rc = subprocess.run([sys.executable, str(TWIN / "twin_summary.py"), d], capture_output=True).returncode
            self.assertEqual(rc, 1)
            summary = json.loads((Path(d) / "summary.json").read_text())
            manifest = json.loads((Path(d) / "manifest.json").read_text())
            self.assertFalse(summary["all_ok"])
            self.assertTrue(manifest["deviations"])
            self.assertIn("neurobot/sim/twin/twin_probe.py", manifest["files_sha256"])
            self.assertNotIn("neurobot/sim/twin/README.md", manifest["files_sha256"])


if __name__ == "__main__":
    unittest.main()
