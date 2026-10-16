"""Чистая логика инструментов стенда без ROS: готовность и сводка скана (stand_ready), разбор пробы stm32
(stm32_probe), маска скана (scan_mask), процессы стенда (stand_procs), критерии сводки (summarize_check)."""
from __future__ import annotations

import io
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace

STAND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAND))

import scan_mask  # noqa: E402
import stand_procs  # noqa: E402
import stand_ready  # noqa: E402
import stm32_probe  # noqa: E402
import summarize_check  # noqa: E402
from stand_config import BASES  # noqa: E402

ALL_TOPICS = {t: object() for t in stand_ready.MSG_TOPICS}
# Аргументы launch из профиля (PROFILE_ARGS=(ключ:=значение …)) — те же числа, что у стенда, без копий в тестах.
PROFILE_ARGS = dict(re.findall(r"(\w+):=([^\s)]+)", (STAND.parents[2] / "scripts/sim/profiles/neurobot.sh").read_text()))
ROBOT = dict(n=683, angle_min=-2.0923497677, inc=4.1846995354 / 682)   # скан с робота 02.06


class ReadyTest(unittest.TestCase):
    def test_ready(self):
        self.assertEqual(stand_ready.missing(ALL_TOPICS, ["stm32_sim"], ["stm32_sim"], ["gz_bridge"]), [])

    def test_names_what_is_missing(self):
        seen = {t: m for t, m in ALL_TOPICS.items() if t != "/odom"}
        lack = stand_ready.missing(seen, [], ["stm32_sim"], [])
        self.assertEqual(lack, ["/odom: нет сообщений", "/cmd_vel: не подписан stm32_sim",
                                "/stand/base_cmd_vel: не подписан мост gz_bridge"])

    def test_foreign_publisher_is_not_stm32(self):
        lack = stand_ready.missing(ALL_TOPICS, ["stm32_sim"], ["executor"], ["gz_bridge"])
        self.assertEqual(lack, ["/stand/base_cmd_vel: не публикует stm32_sim"])

    def test_scan_summary(self):
        inc = math.radians(5)
        ranges = [0.5, math.nan, 0.006, 2.0, 1.2, math.inf, 3.0]        # углы −15…+15° с шагом 5°
        msg = SimpleNamespace(ranges=ranges, angle_min=-math.radians(15), angle_max=math.radians(15),
                              angle_increment=inc, range_min=0.02, range_max=5.6,
                              header=SimpleNamespace(frame_id="laser_frame"))
        s = stand_ready.scan_geometry(msg)
        self.assertEqual((s["n"], s["n_nan"], s["n_below_min"], s["min_valid"], s["frame_id"]),
                         (7, 1, 1, 0.5, "laser_frame"))
        self.assertEqual(s["front_min"], 1.2)       # ±10°: NaN, код 0,006, 2,0, 1,2, inf — годны 2,0 и 1,2


class ScanMaskTest(unittest.TestCase):
    def test_robot_rays(self):
        p = dict(scan_mask.plan(ROBOT["n"], ROBOT["angle_min"], ROBOT["inc"], True, True))
        nan = sorted(i for i, v in p.items() if v != v)
        codes = sorted(i for i, v in p.items() if v == v)
        self.assertEqual((nan[0], nan[-1], len(nan)), (168, 353, 186))   # блок NaN скана робота
        self.assertEqual((codes[0], codes[-1], len(codes)), (0, 58, 59))
        self.assertTrue(all(v == scan_mask.CODE_M for i, v in p.items() if i in codes))

    def test_flags(self):
        args = (ROBOT["n"], ROBOT["angle_min"], ROBOT["inc"])
        self.assertEqual(scan_mask.plan(*args, False, False), [])
        self.assertEqual(len(scan_mask.plan(*args, True, False)), 186)
        self.assertEqual(len(scan_mask.plan(*args, False, True)), 59)

    def test_by_angle_on_other_geometry(self):
        n, lo = 360, -math.pi                       # 1° на луч: −60,8…+4,2° → лучи −60…+4
        p = scan_mask.plan(n, lo, 2 * math.pi / n, True, False)
        self.assertEqual((p[0][0] - 180, p[-1][0] - 180, len(p)), (-60, 4, 65))


class ProcsTest(unittest.TestCase):
    def test_gz_sim_detection(self):
        yes = [("/bin/sh", "-c", "ruby /opt/ros/jazzy/opt/gz_tools_vendor/bin/gz sim -r -s w.sdf"),
               ("gz sim -r -s --headless-rendering -v2 w.sdf",), ("ruby", "/opt/x/bin/gz", "sim", "-s")]
        no = [("bash", "-lc", 'pgrep -af "gz sim"'), ("python3", "gz sim"), ("gz", "topic", "-l"), ()]
        self.assertEqual([stand_procs.is_gz_sim(a) for a in yes], [True] * 3)
        self.assertEqual([stand_procs.is_gz_sim(a) for a in no], [False] * 4)

    def test_foreign_gz_and_group(self):
        P = stand_procs.Proc
        procs = [P(10, 10, ("ros2", "launch")), P(11, 10, ("gz sim -s w.sdf",)), P(20, 20, ("gz sim -s old.sdf",)),
                 P(30, 30, ("bash",))]
        self.assertEqual([p.pid for p in stand_procs.gz_sim(procs, exclude_pgid=10)], [20])
        self.assertEqual([p.pid for p in stand_procs.group(procs, 10)], [10, 11])

    def test_down_escalates_to_kill(self):
        stubborn = "import signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); " \
                   "signal.signal(signal.SIGTERM, signal.SIG_IGN); print(flush=True); time.sleep(60)"
        for code, expect in (("import time; print(flush=True); time.sleep(60)", "SIGINT"), (stubborn, "SIGKILL")):
            p = subprocess.Popen([sys.executable, "-c", code], start_new_session=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL)
            p.stdout.readline()                     # обработчики сигналов уже стоят
            reaper = threading.Thread(target=p.wait)  # как init в ВМ: зомби лидера не держит группу
            reaper.start()
            with redirect_stderr(io.StringIO()):
                self.assertEqual(stand_procs.down(p.pid, 0.3, 0.3), expect)
            reaper.join()
            p.stdout.close()
        self.assertEqual(stand_procs.down(p.pid, 0.3, 0.3), "-")   # группы уже нет

    @unittest.skipUnless(os.path.isdir("/proc/self"), "нужен /proc (ВМ)")
    def test_crash_kills_named_victim(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp, "victim_node.py")
            script.write_text("import time\nprint(flush=True)\ntime.sleep(60)\n")
            p = subprocess.Popen([sys.executable, str(script)], start_new_session=True, stdout=subprocess.PIPE)
            p.stdout.readline()
            Path(tmp, "pid").write_text(str(p.pid))
            reaper = threading.Thread(target=p.wait)
            reaper.start()
            with self.assertRaises(SystemExit):
                stand_procs.crash(tmp, 1.0)                       # по умолчанию ищет stm32_sim.py
            out = stand_procs.crash(tmp, 5.0, victim="victim_node.py")
            reaper.join()
            p.stdout.close()
            self.assertEqual(out["killed"], [p.pid])
            self.assertIsNotNone(out["group_gone_after_s"])
            self.assertEqual(out["group_left"], [])

    def test_snapshot_reads_pgid_after_comm(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp, "42")
            d.mkdir()
            (d / "stat").write_bytes(b"42 (gz sim) a) S 1 4242 4242 0 -1")
            (d / "cmdline").write_bytes(b"gz sim -r -s w.sdf\0")
            Path(tmp, "self").mkdir()
            self.assertEqual(stand_procs.snapshot(tmp), [stand_procs.Proc(42, 4242, ("gz sim -r -s w.sdf",))])


def series(t0, t1, dt, f):
    n = int(round((t1 - t0) / dt))
    return [(t0 + i * dt, f(t0 + i * dt)) for i in range(n + 1)]


class ProbeAnalyzeTest(unittest.TestCase):
    """Команда 0,2 м/с с t=1 до t=2,5; тишина 2 с; /odom 50 Гц, истинная поза 100 Гц."""

    def run_case(self, v_of_t):
        odom = series(0.0, 5.0, 0.02, v_of_t)
        x = 0.0
        gt = []
        for t, v in series(0.0, 5.0, 0.01, v_of_t):
            x += v * 0.01
            gt.append((t, x, 0.0))
        return stm32_probe.analyze(odom, gt, 1.0, 2.5, 2.0)

    def test_hold(self):
        r = self.run_case(lambda t: 0.2 if t >= 1.06 else 0.0)
        self.assertEqual(r["stop_after_s"], None)
        self.assertAlmostEqual(r["start_after_s"], 0.06, places=2)
        self.assertEqual(r["v_cmd_end"], 0.2)
        self.assertAlmostEqual(r["coast_m"], 0.4, places=2)
        self.assertEqual(r["v_silence_end"], 0.2)
        self.assertAlmostEqual(r["pose_end"][0], 0.2 * (4.5 - 1.06), places=2)

    def test_watchdog(self):
        r = self.run_case(lambda t: 0.2 if 1.06 <= t < 2.84 else 0.0)
        self.assertAlmostEqual(r["stop_after_s"], 0.34, places=2)
        self.assertAlmostEqual(r["coast_m"], 0.068, places=2)
        self.assertEqual(r["v_silence_end"], 0.0)

    def test_never_moved(self):
        r = self.run_case(lambda t: 0.0)
        self.assertIsNone(r["start_after_s"])
        self.assertEqual(r["stop_after_s"], 0.0)   # /odom в момент последней команды уже «стоит»

    def test_no_data(self):
        r = stm32_probe.analyze([], [], 1.0, 2.5, 2.0)
        self.assertEqual(r, {"start_after_s": None, "v_cmd_end": None, "stop_after_s": None, "coast_m": None,
                             "v_silence_end": None, "pose_end": None})


def scan(base="tb3", masked=False):
    exp = summarize_check.expected_scan(summarize_check.effective(dict(
        PROFILE_ARGS, base=base, scan_occluder=str(masked).lower(), scan_codes=str(masked).lower())))
    front = summarize_check.WALL_X - BASES[base].lidar_x
    return dict(exp, min_valid=0.86 if base == "tb3" else 1.51, front_min=front - 0.01)


def ready(base="tb3", masked=False, cmd_type="Twist"):
    return {"ready": True, "missing": [], "topics": ["/clock", "/cmd_vel", "/scan"], "scan": scan(base, masked),
            "cmd_vel": {"types": ["geometry_msgs/msg/" + cmd_type], "subscribers": ["stm32_sim"], "publishers": []}}


def load():
    return {"vm": {"rates": {"rtf": 0.99, "topics": {"/scan": {"hz_sim": 10.0}, "/odom": {"hz_sim": 50.0},
                                                    "/ground_truth/pose": {"hz_sim": 100.0}}}}}


def good_runs():
    neuro = dict(PROFILE_ARGS, base="neurochip", scan_occluder="true", scan_codes="true")
    runs = {
        "hold": {"args": dict(PROFILE_ARGS, stm32_timeout_ms="0"), "ready": ready(), "load": load(),
                 "probe": {"v_cmd_end": 0.2, "stop_after_s": None, "coast_m": 0.4, "silence_s": 2.0},
                 "probe_wall": {"v_silence_end": 0.5, "pose_end": [1.432, 0.0]}},
        "timeout300": {"args": dict(PROFILE_ARGS, stm32_timeout_ms="300"), "ready": ready(),
                       "probe": {"v_cmd_end": 0.2, "stop_after_s": 0.366, "silence_s": 2.0}},
        "stamped500": {"args": dict(PROFILE_ARGS, stm32_timeout_ms="500", stm32_cmd_vel_type="TwistStamped"),
                       "ready": ready(cmd_type="TwistStamped"),
                       "probe": {"v_cmd_end": 0.2, "stop_after_s": 0.566, "silence_s": 2.0}},
        "odom20_limits": {"args": dict(PROFILE_ARGS, odom_hz="20"), "ready": ready(),
                          "rates": {"topics": {"/odom": {"hz_sim": 20.0}}},
                          "probe": {"v_cmd_end": 0.15, "stop_after_s": None, "silence_s": 2.0},
                          "crash": {"group_gone_after_s": 2.1, "gz_sim_left": []}},
        "neurochip": {"args": neuro, "ready": ready("neurochip", True), "load": load(),
                      "probe_wall": {"pose_end": [1.305, 0.0]}},
    }
    for name, x in summarize_check.NEG_CONTACT_X.items():
        runs[name] = {"args": dict(PROFILE_ARGS, base="neurochip"),
                      "ready": dict(ready("neurochip"), scan=dict(scan("neurochip"), front_min=1.5)),
                      "probe_obstacle": {"pose_end": [x + 0.004, 0.0]}}
    runs["neg_table"]["crash"] = {"group_gone_after_s": 15.2, "gz_sim_left": [], "hung_gz": [11, 12]}
    return runs


def failed(runs, starts=()):
    return [c["name"] for c in summarize_check.checks(runs, list(starts)) if not c["ok"]]


class SummaryChecksTest(unittest.TestCase):
    def test_profile_args_parsed(self):
        self.assertEqual((PROFILE_ARGS["lidar_samples"], PROFILE_ARGS["lidar_min_angle"]), ("683", "-2.0923497677"))

    def test_all_ok(self):
        self.assertEqual(failed(good_runs()), [])

    def test_expected_scan_by_base_and_masks(self):
        tb3, neuro = scan(), scan("neurochip", True)
        self.assertEqual((tb3["range_min"], tb3["frame_id"], tb3["n_nan"], tb3["n_below_min"]),
                         (0.06, "base_scan", 0, 0))
        self.assertEqual((neuro["range_min"], neuro["frame_id"], neuro["n_nan"], neuro["n_below_min"]),
                         (0.02, "laser_frame", 186, 59))

    def test_explicit_range_min_wins(self):
        a = summarize_check.effective(dict(PROFILE_ARGS, base="neurochip", lidar_range_min="0.05"))
        self.assertEqual(summarize_check.expected_scan(a)["range_min"], 0.05)

    def test_low_rtf_and_lost_rate(self):
        runs = good_runs()
        runs["hold"]["load"]["vm"]["rates"]["rtf"] = 0.9
        del runs["neurochip"]["load"]["vm"]["rates"]["topics"]["/odom"]
        self.assertEqual(failed(runs), ["hold: RTF, окно замера", "neurochip: /odom, Гц сим."])

    def test_camera_topic_and_wrong_type(self):
        runs = good_runs()
        runs["hold"]["ready"] = dict(ready(cmd_type="TwistStamped"), topics=["/camera/image_raw"])
        self.assertEqual(failed(runs), ["hold: /cmd_vel тип", "hold: камера Gazebo выключена — темы /camera*"])

    def test_stamped_type_expected_from_args(self):
        runs = good_runs()
        runs["stamped500"]["ready"] = ready()
        self.assertEqual(failed(runs), ["stamped500: /cmd_vel тип"])

    def test_hold_that_stopped_and_slow_watchdogs(self):
        runs = good_runs()
        runs["hold"]["probe"] = {"v_cmd_end": 0.2, "stop_after_s": 0.3, "coast_m": 0.06, "silence_s": 2.0}
        runs["timeout300"]["probe"]["stop_after_s"] = 0.9
        runs["stamped500"]["probe"]["stop_after_s"] = 0.33        # 500 мс не дождался — режим не тот
        self.assertEqual(failed(runs), [
            "hold: stm32 hold, тишина — не остановился", "hold: stm32 hold, путь за тишину, м",
            "timeout300: сторож 300 мс, задержка 20 мс — остановка после последней команды, с",
            "stamped500: сторож 500 мс, задержка 20 мс — остановка после последней команды, с"])

    def test_missing_runs_fail(self):
        names = failed({})
        for name in ("hold: стенд готов", "neurochip: стенд готов", "odom20_limits: --odom-hz 20 действует, /odom Гц "
                     "сим.", "odom20_limits: kill -9 stm32_sim — gz sim в ВМ не осталось", "neg_box: стенд готов"):
            self.assertIn(name, names)

    def test_robot_passed_through_wall(self):
        runs = good_runs()
        runs["hold"]["probe_wall"]["pose_end"] = [2.1, 0.0]
        runs["neurochip"]["probe_wall"]["pose_end"] = [1.432, 0.0]      # край TB3, а не круга Ø0,39
        self.assertEqual(failed(runs), ["hold: hold, 0,5 м/с и тишина — база упёрлась в стену, x, м",
                                        "neurochip: hold, 0,5 м/с и тишина — база упёрлась в стену, x, м"])

    def test_self_hits_masks_and_ignored_odom_hz(self):
        runs = good_runs()
        runs["hold"]["ready"]["scan"]["min_valid"] = 0.02
        runs["neurochip"]["ready"]["scan"]["n_nan"] = 0                # окклюдер не включился
        runs["odom20_limits"]["rates"]["topics"]["/odom"]["hz_sim"] = 50.0
        self.assertEqual(failed(runs), ["hold: /scan ближайшее, м",
                                        "odom20_limits: --odom-hz 20 действует, /odom Гц сим.",
                                        "neurochip: /scan n_nan"])

    def test_orphan_after_crash(self):
        runs = good_runs()
        runs["odom20_limits"]["crash"] = {"group_gone_after_s": None, "gz_sim_left": [{"pid": 7}]}
        runs["neg_table"]["crash"]["group_gone_after_s"] = 31.0     # сервер gz пережил SIGTERM слишком долго
        self.assertEqual(failed(runs), [
            "odom20_limits: kill -9 stm32_sim — группа стенда погасла, с",
            "odom20_limits: kill -9 stm32_sim — gz sim в ВМ не осталось",
            "neg_table: сервер gz завис (SIGSTOP), kill -9 stm32_sim — группа стенда погасла, с"])

    def test_negative_seen_or_not_reached(self):
        runs = good_runs()
        runs["neg_box"]["ready"]["scan"]["front_min"] = 0.66
        runs["neg_table"]["probe_obstacle"]["pose_end"] = [1.4, -1.0]
        self.assertEqual(failed(runs), ["neg_box: лидар не видит препятствие по курсу, front_min, м",
                                        "neg_table: база въехала в препятствие, x, м"])

    def test_starts(self):
        ok, bad = {"ready": {"ready": True}}, {"ready": None}
        self.assertEqual(failed(good_runs(), [ok, ok]), [])
        self.assertEqual(failed(good_runs(), [ok, bad]), ["холодные подъёмы up → ready → down: готовы"])

    def test_args_txt_last_wins(self):
        self.assertEqual(summarize_check.parse_args_txt("a:=1\nworld:=/x.sdf\na:=2\n"), {"a": "2", "world": "/x.sdf"})


if __name__ == "__main__":
    unittest.main()
