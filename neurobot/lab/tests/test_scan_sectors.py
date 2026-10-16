"""scan_sectors.py: сводка LaserScan по секторам 10° (доли NaN, ±inf, значений ниже range_min, ближайшее
расстояние) и блок NaN прямо по курсу, как в скане с робота 02.06 (ANALYSIS RS3: 683 луча, NaN на индексах
168–353). Логика — без ROS, под Python 3.8 и 3.12; без rclpy скрипт завершается с кодом 2 и понятной строкой."""
import math
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
sys.dont_write_bytecode = True

import scan_sectors  # noqa: E402

ANGLE_MIN = -2.0923497677  # urg_node2 на роботе: 683 луча, ±119,9°
INC = 2 * -ANGLE_MIN / 682


def robot_like_scan():
    ranges = [2.0] * 683
    for i in range(168, 354):
        ranges[i] = math.nan
    ranges[10] = 0.007  # код ошибки URG ниже range_min
    ranges[600] = math.inf
    return SimpleNamespace(header=SimpleNamespace(frame_id="laser_frame"), angle_min=ANGLE_MIN, angle_increment=INC,
                           range_min=0.02, range_max=5.6, ranges=ranges)


class SectorsTest(unittest.TestCase):
    def test_front_block_is_all_nan(self):
        msg = robot_like_scan()
        rows = {r[0]: r for r in scan_sectors.sectors(msg.angle_min, msg.angle_increment, msg.ranges, 0.02)}
        for lo in range(-60, 0, 10):
            with self.subTest(sector=lo):
                self.assertEqual(rows[lo][3], 1.0)
                self.assertIsNone(rows[lo][6])
        self.assertEqual(rows[10][3], 0.0)
        self.assertEqual(rows[10][6], 2.0)
        self.assertEqual(sum(r[2] for r in rows.values()), 683)

    def test_codes_below_range_min_and_inf_are_counted_apart(self):
        msg = robot_like_scan()
        rows = {r[0]: r for r in scan_sectors.sectors(msg.angle_min, msg.angle_increment, msg.ranges, 0.02)}
        low = rows[math.floor(math.degrees(ANGLE_MIN + 10 * INC) / 10) * 10]
        inf = rows[math.floor(math.degrees(ANGLE_MIN + 600 * INC) / 10) * 10]
        self.assertGreater(low[5], 0)
        self.assertEqual(low[6], 2.0)  # 0,007 м — не «препятствие вплотную»
        self.assertGreater(inf[4], 0)

    def test_summary_names_block_and_front(self):
        msg = robot_like_scan()
        line = scan_sectors.summary(msg.angle_min, msg.angle_increment, msg.ranges)
        self.assertEqual(line, "блок NaN: -60.8…+4.2° (186 лучей, индексы 168–353), задевает передний сектор ±30°")
        side = [math.nan if 30 <= i < 60 else 1.0 for i in range(683)]
        self.assertIn("не задевает", scan_sectors.summary(ANGLE_MIN, INC, side))
        self.assertEqual(scan_sectors.summary(ANGLE_MIN, INC, [1.0] * 683), "блок NaN: нет")

    def test_report_has_header_rows_and_summary_without_array(self):
        text = scan_sectors.report("/scan", robot_like_scan())
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("тема /scan, кадр laser_frame, лучей 683"))
        self.assertEqual(len(lines), 2 + 24 + 1)  # заголовок, шапка, 24 сектора по 10°, итог
        self.assertTrue(lines[-1].startswith("блок NaN: -60.8"))


class CliTest(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-I", str(LAB / "scan_sectors.py"), *args], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, check=False)

    def test_bad_arguments(self):
        done = self.run_cli("/scan", "soon")
        self.assertEqual(done.returncode, 2)
        self.assertIn("scan_sectors.py", done.stderr)

    def test_without_rclpy(self):
        done = self.run_cli("/scan", "0.1")
        self.assertEqual(done.returncode, 2)
        self.assertIn("нет rclpy", done.stdout)


if __name__ == "__main__":
    unittest.main()
