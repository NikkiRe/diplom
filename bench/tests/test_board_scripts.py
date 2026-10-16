"""scripts/board без ВМ: аргументы netem (netem.jq), итог юнита run (record.jq) — тем же jq, что вызывает board.sh;
синтаксис и справка board.sh. Нужен jq (в macOS 15+ — /usr/bin/jq); без него тесты jq пропускаются."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.board import profiles  # noqa: E402

SCRIPTS = ROOT / "scripts/board"
JQ = shutil.which("jq")
CGROUP = {  # сырые файлы, как их снимает record в ExecStopPost
    "memory.peak": "16777216\n", "memory.max": "16777216\n", "memory.swap.max": "0\n",
    "memory.events": "low 0\nhigh 0\nmax 35\noom 1\noom_kill 1\noom_group_kill 0\n",
    "cpu.max": "1000 20000\n", "cpu.stat": "usage_usec 16370\nnr_periods 3\nnr_throttled 2\nthrottled_usec 9000\n",
    "cpuset.cpus.effective": "0-1\n", "io.max": "253:0 rbps=12500000 wbps=max riops=300 wiops=20\n",
    "io.stat": "253:0 rbytes=36864 wbytes=0 rios=9 wios=0 dbytes=0 dios=0\n", "pids.peak": "2\n",
}


def netem(profile: str, direction: str) -> str:
    return subprocess.run([JQ, "-r", "--arg", "p", profile, "--arg", "d", direction, "-f", SCRIPTS / "netem.jq",
                           profiles.PROFILES_PATH], capture_output=True, text=True, check=True).stdout.strip()


def record(files: dict[str, str], signum: str = "") -> dict:
    """Итог по сырым файлам — те же аргументы jq, что собирает record_json в board.sh."""
    with tempfile.TemporaryDirectory() as tmp:
        args = []
        for name, text in files.items():
            (Path(tmp) / name).write_text(text)
            args += ["--rawfile", name, str(Path(tmp) / name)]
        done = subprocess.run([JQ, "-c", "-n", *args, "--arg", "unit", "board-run-t.service", "--arg", "wall_s",
                               "1.500000", "--arg", "signum", signum, "-f", SCRIPTS / "record.jq"],
                              capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


@unittest.skipUnless(JQ, "нет jq")
class NetemArgsTest(unittest.TestCase):
    def test_static_profiles(self):
        self.assertEqual(netem("4g", "up"), "delay 35ms rate 12000kbit limit 1026")
        self.assertEqual(netem("wifi", "down"), "delay 3ms 2ms rate 30000kbit limit 2490")
        self.assertEqual(netem("wifi-weak", "up"), "delay 15ms 10ms rate 2000kbit loss random 1% limit 170")
        self.assertEqual(netem("lossy", "down"),
                         "delay 35ms rate 12000kbit loss gemodel 1% 30% 70% 0% limit 1026")

    def test_queue_limits_and_unlimited_rate(self):
        self.assertEqual(netem("gprs", "up"), "delay 500ms rate 24kbit limit 64")         # не меньше min_packets
        self.assertEqual(netem("lan", "down"), "delay 0ms rate 1000000kbit limit 10000")  # не больше max_packets
        # без rate — явный 0kbit: tc qdisc replace иначе оставил бы прежнюю скорость
        self.assertEqual(netem("offline", "up"), "delay 0ms rate 0kbit loss random 100% limit 1000")

    def test_missing_or_schedule_profile_fails(self):
        # пустые аргументы дали бы netem без ограничений — jq падает явно
        for name in ("nope", "flaky", ""):
            with self.assertRaises(subprocess.CalledProcessError, msg=name):
                netem(name, "up")

    def test_every_static_profile_both_directions(self):
        for name, profile in profiles.load()["net"]["profiles"].items():
            if profiles.is_schedule(profile):
                continue
            for direction in ("up", "down"):
                args = netem(name, direction).split()
                self.assertEqual(args[0], "delay", name)
                self.assertIn("rate", args)
                self.assertEqual(args[-2], "limit")


@unittest.skipUnless(JQ, "нет jq")
class RecordTest(unittest.TestCase):
    def test_oom_kill(self):
        doc = record(CGROUP | {"exit": "oom-kill\nkilled\nKILL\n"}, signum="9")
        self.assertEqual((doc["reason"], doc["exit_code"], doc["signal"]), ("oom-kill", 137, "SIGKILL"))
        self.assertEqual(doc["service_result"], "oom-kill")
        self.assertEqual(doc["memory"], {"max": 16777216, "swap_max": 0, "peak": 16777216,
                                         "events": {"low": 0, "high": 0, "max": 35, "oom": 1, "oom_kill": 1,
                                                    "oom_group_kill": 0}})
        self.assertEqual(doc["cpu"]["stat"]["nr_throttled"], 2)
        self.assertEqual(doc["cpu"]["max"], "1000 20000")
        self.assertEqual(doc["io"]["max"], {"253:0": {"rbps": 12500000, "wbps": "max", "riops": 300, "wiops": 20}})
        self.assertEqual(doc["io"]["stat"]["253:0"]["rios"], 9)
        self.assertEqual((doc["pids_peak"], doc["wall_s"], doc["unit"]), (2, 1.5, "board-run-t.service"))

    def test_exit_codes_and_signals(self):
        calm = CGROUP | {"memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n"}
        doc = record(calm | {"exit": "exit-code\nexited\n3\n"})
        self.assertEqual((doc["reason"], doc["exit_code"], doc["signal"]), ("exit", 3, None))
        doc = record(calm | {"exit": "success\nkilled\nTERM\n"}, signum="15")
        self.assertEqual((doc["reason"], doc["exit_code"], doc["signal"]), ("signal", 143, "SIGTERM"))

    def test_timeout(self):
        calm = CGROUP | {"memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n"}
        doc = record(calm | {"exit": "timeout\nkilled\nTERM\n"}, signum="15")
        self.assertEqual((doc["reason"], doc["exit_code"], doc["signal"]), ("timeout", 143, "SIGTERM"))
        # команда сама вышла по SIGTERM с 0 — код всё равно ненулевой, как у timeout(1)
        doc = record(calm | {"exit": "timeout\nexited\n0\n"})
        self.assertEqual((doc["reason"], doc["exit_code"]), ("timeout", 124))

    def test_oom_of_a_child_is_not_success(self):
        # главный процесс вышел с 0 после OOM-убийства потомка — код всё равно ненулевой
        doc = record(CGROUP | {"exit": "oom-kill\nexited\n0\n"})
        self.assertEqual((doc["reason"], doc["exit_code"]), ("oom-kill", 137))

    def test_missing_files_are_null(self):
        doc = record({"exit": "\n\n\n"})
        self.assertEqual((doc["reason"], doc["exit_code"]), ("not-started", 125))
        self.assertIsNone(doc["memory"]["peak"])
        self.assertIsNone(doc["io"]["max"])


class BoardShTest(unittest.TestCase):
    def test_syntax(self):
        subprocess.run(["bash", "-n", SCRIPTS / "board.sh"], check=True)

    @unittest.skipUnless(shutil.which("shellcheck"), "нет shellcheck")
    def test_shellcheck(self):
        subprocess.run(["shellcheck", "-s", "bash", SCRIPTS / "board.sh"], check=True)

    def test_usage_and_root_required(self):
        done = subprocess.run(["bash", SCRIPTS / "board.sh"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)
        for command in ("net-up", "net-set", "net-schedule", '"after"', "net-stats", "net-down", "run <имя>",
                        "--timeout", "cleanup"):
            self.assertIn(command, done.stdout)
        if os.geteuid() != 0:
            done = subprocess.run(["bash", SCRIPTS / "board.sh", "cleanup"], capture_output=True, text=True)
            self.assertEqual(done.returncode, 2)
            self.assertIn("нужен root", done.stderr)


if __name__ == "__main__":
    unittest.main()
