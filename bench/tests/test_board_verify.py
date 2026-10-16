"""bench.board без ВМ: выбор уровней, планы проб, ожидания и вердикты verify на синтетических итогах, обрывы
по расписанию, отчёт; команды limactl/board.sh и разбор их вывода; calibrate и CLI."""
from __future__ import annotations

import contextlib
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.board import __main__ as cli  # noqa: E402
from bench.board import calibrate, profiles, verify, vm  # noqa: E402
from bench.board.verify import SelectionError  # noqa: E402
from bench.board.vm import RunResult, VmError  # noqa: E402

DOC = profiles.load()
NET = DOC["net"]["profiles"]
MIB = 1 << 20
START = {"phase": "start", "uid": 501, "env": {"GOMEMLIMIT": None, "GOMAXPROCS": None}}


def result(record: dict | None = None, **phases) -> RunResult:
    rec = {"reason": "exit", "exit_code": 0, "memory": {}, "cpu": {"max": None, "cpus": None, "stat": {}},
           "io": {"max": {}, "stat": {}}} | (record or {})
    return RunResult(0, "", "", rec, {name: {"phase": name, **doc} for name, doc in phases.items()})


def verdicts(rows: list[dict]) -> dict[str, bool | None]:
    return {r["metric"]: r["ok"] for r in rows}


class SelectionTest(unittest.TestCase):
    def test_defaults(self):
        sel = verify.parse_selection([], DOC)
        self.assertEqual(sel["memory"], [64, 16])
        self.assertEqual(sel["cpu"], [(200, 4), (50, 1), (5, 1)])
        self.assertEqual(sel["disk"], ["nvme", "sd-good", "sd-awful"])
        self.assertEqual(sel["net"], list(NET))

    def test_overrides_none_and_board(self):
        sel = verify.parse_selection(["cpu=25", "net=4g,flaky", "disk=none"], DOC)
        self.assertEqual((sel["cpu"], sel["net"], sel["disk"]), ([(25, 1)], ["4g", "flaky"], []))
        sel = verify.parse_selection(["board=pi-zero-2w", "net=none"], DOC)
        self.assertEqual(sel, {"memory": [256], "cpu": [(25, 4)], "disk": ["sd-slow"], "net": []})

    def test_errors(self):
        for spec in ("memory=100", "cpu=50/3", "cpu=400/1", "cpu=x", "disk=ssd", "net=5g", "gpu=1", "memory=",
                     "board=pi5"):
            with self.assertRaises(SelectionError, msg=spec):
                verify.parse_selection([spec], DOC)


class AxisChecksTest(unittest.TestCase):
    def test_memory_oom_kill(self):
        rec = {"reason": "oom-kill", "exit_code": 137,
               "memory": {"max": 16 * MIB, "swap_max": 0, "peak": 16 * MIB, "events": {"oom_kill": 1}}}
        start = START | {"env": {"GOMEMLIMIT": str(16 * MIB * 45 // 100), "GOMAXPROCS": "4"}}
        rows = verify.memory_checks(16, result(rec, start=start, alloc={"mib": 7}), 45)
        self.assertEqual(verdicts(rows), {"memory.max": True, "memory.swap.max": True, "итог пробы": True,
                                          "memory.peak": True, "выделено пробой": None, "uid процесса": True,
                                          "GOMEMLIMIT": True})

    def test_memory_limit_not_enforced(self):
        rec = {"memory": {"max": "max", "swap_max": "max", "peak": 200 * MIB, "events": {}}}
        rows = verify.memory_checks(16, result(rec, start=START | {"uid": 0}, done={"mib": 96}), 45)
        self.assertEqual([r["metric"] for r in rows if r["ok"] is False],
                         ["memory.max", "memory.swap.max", "итог пробы", "memory.peak", "uid процесса", "GOMEMLIMIT"])

    def test_cpu_share_and_throttling(self):
        rec = {"cpu": {"max": "1000 20000", "cpus": "0", "stat": {"nr_throttled": 140, "nr_periods": 150}}}
        start = START | {"env": {"GOMEMLIMIT": None, "GOMAXPROCS": "1"}}
        rows = verify.cpu_checks(5, 1, result(rec, start=start, cpu={"cpu_s": 0.157, "wall_s": 3.0}), 20)
        self.assertTrue(all(r["ok"] for r in rows), rows)
        # вдвое больше квоты — расхождение; троттлинга нет — тоже
        rec["cpu"]["stat"]["nr_throttled"] = 0
        rows = verify.cpu_checks(5, 1, result(rec, start=start, cpu={"cpu_s": 0.3, "wall_s": 3.0}), 20)
        self.assertEqual(verdicts(rows)["доля CPU"], False)
        self.assertEqual(verdicts(rows)["троттлинг (nr_throttled)"], False)

    def test_cpu_without_quota_pressure_has_no_throttling_row(self):
        rec = {"cpu": {"max": "80000 20000", "cpus": "0-3", "stat": {}}}
        rows = verify.cpu_checks(400, 4, result(rec, start=START, cpu={"cpu_s": 11.9, "wall_s": 3.0}), 20)
        self.assertNotIn("троттлинг (nr_throttled)", verdicts(rows))
        self.assertTrue(verdicts(rows)["доля CPU"])

    def test_count_cpus(self):
        self.assertEqual([verify.count_cpus(t) for t in ("0", "0-3", "0-1,3", "", None)], [1, 4, 3, 0, 0])

    def test_disk_plan(self):
        self.assertEqual(verify.disk_plan(DOC["disk"]["sd-awful"]), {"seq_mib": 11, "write_ios": 40, "read_ios": 600})
        self.assertEqual(verify.disk_plan(DOC["disk"]["nvme"]), {"seq_mib": 64, "write_ios": 4000, "read_ios": 4000})
        # чтение по 4 КиБ не выходит за файл последовательной записи
        self.assertEqual(verify.disk_plan({"wbps": 1, "riops": 4000})["read_ios"], 4 * 256)

    def test_disk_checks(self):
        disk = DOC["disk"]["sd-awful"]
        rec = {"io": {"max": {"253:0": {"rbps": 12500000, "wbps": 4000000, "riops": 300, "wiops": 20}}, "stat": {}}}
        res = result(rec, seq_write={"bytes": 11 * MIB, "seconds": 2.9, "block": MIB},
                     seq_read={"bytes": 11 * MIB, "seconds": 0.5, "block": MIB},
                     iops_write={"bytes": 40 * 4096, "seconds": 2.0, "block": 4096},
                     iops_read={"error": "dd: boom"})
        got = verdicts(verify.disk_checks("sd-awful", disk, res))
        # 11 МиБ за 0,5 с ≈ 23 МБ/с при лимите 12,5 — расхождение
        self.assertEqual(got, {"seq_write": True, "seq_read": False, "iops_write": True, "iops_read": False,
                               "io.max": True})
        got = verdicts(verify.disk_checks("nvme", DOC["disk"]["nvme"],
                                          result(seq_write={"bytes": MIB, "seconds": 0.01, "block": MIB})))
        self.assertIsNone(got["seq_write"])
        self.assertNotIn("io.max", got)


class NetMathTest(unittest.TestCase):
    def test_expected_rtt_adds_delay_and_serialization(self):
        # gprs: 1000 мс задержки + 74 байта вверх на 24 кбит/с и вниз на 36 кбит/с
        self.assertAlmostEqual(verify.expected_rtt_ms(0.25, NET["gprs"], 74), 0.25 + 1000 + 74 * 8 / 24 + 74 * 8 / 36)
        self.assertEqual(verify.expected_rtt_ms(0.25, NET["offline"], 74), 0.25)

    def test_tolerances(self):
        self.assertAlmostEqual(verify.jitter_sd_ms(NET["wifi"]), math.sqrt(8 / 3))
        self.assertEqual(verify.rtt_tolerance(10, 0, 5), 3.0)
        self.assertAlmostEqual(verify.rtt_tolerance(100, 2, 4), 10 + 3)
        self.assertEqual(verify.loss_inflation(NET["wifi-weak"]["up"]), 1.0)
        # Гилберт–Эллиотт p=1 %, r=30 %, 1−h=70 %: пачки увеличивают дисперсию примерно вчетверо
        self.assertAlmostEqual(verify.loss_inflation(NET["lossy"]["up"]), 4.09, places=2)
        self.assertAlmostEqual(verify.loss_tolerance(NET["wifi-weak"]["up"], 1000),
                               verify.Z99 * math.sqrt(0.01 * 0.99 / 1000) + 0.001)
        self.assertEqual(verify.loss_tolerance(NET["4g"]["up"], 400), 1 / 400)

    def test_net_plan(self):
        plan = verify.net_plan(NET["gprs"], 0.2)
        self.assertEqual(plan["echo"], 3)
        self.assertEqual((plan["down_bytes"], plan["up_bytes"]), (16 * 1024, 16 * 1024))
        self.assertLess(plan["udp_pps"] * (64 + 42) * 8, 0.3 * 24000)   # UDP — меньше трети самой медленной скорости
        lossy = verify.net_plan(NET["lossy"], 0.2)
        self.assertEqual((lossy["udp_pps"], lossy["udp_seconds"]), (verify.LOSS_PPS, 5))
        self.assertEqual(verify.net_plan(NET["4g"], 0.2)["udp_pps"], verify.UDP_PPS)
        self.assertEqual(verify.net_plan(NET["lan"], 0.2)["down_bytes"], 8 * MIB)
        self.assertEqual(verify.net_plan(NET["offline"], 0.2), {"connects": 1, "echo": 0, "udp_pps": 10,
                                                                 "udp_seconds": 1, "timeout": 2, "grace": 1})

    def test_lossy_sample_tells_bursts_from_no_loss(self):
        # план lossy даёт столько UDP, что допуск уже ожидаемых потерь: полное отсутствие потерь — расхождение
        plan = verify.net_plan(NET["lossy"], 0.2)
        n = round(plan["udp_pps"] * plan["udp_seconds"])
        self.assertLess(verify.loss_tolerance(NET["lossy"]["up"], n), profiles.direction_loss(NET["lossy"]["up"]) / 2)
        rows = verify._loss_rows("lossy", NET["lossy"]["up"], NET["lossy"]["down"],
                                 {"sent": n, "server_received": n, "echoed": n})
        self.assertEqual([r["ok"] for r in rows], [False, False])
        lost = round(n * profiles.direction_loss(NET["lossy"]["up"]))
        rows = verify._loss_rows("lossy", NET["lossy"]["up"], NET["lossy"]["down"],
                                 {"sent": n, "server_received": n - lost, "echoed": n - 2 * lost})
        self.assertEqual([r["ok"] for r in rows], [True, True])

    def test_slow_start_share(self):
        # satellite вниз: BDP 750 КБ ≈ 52 начальных окна → ≈ 5,7 RTT по 0,6 с сверх 2,5 с передачи на пределе
        self.assertAlmostEqual(verify.slow_start_share(3_125_000, 10_000, 600), 0.42, places=2)
        self.assertEqual(verify.slow_start_share(16_384, 36, 1000), 1.0)   # gprs: BDP меньше начального окна


class NetChecksTest(unittest.TestCase):
    BASE = {"connect_ms": 0.25, "echo_ms": 0.15}
    COUNTERS = {"up": {"packets": 990, "drops": 10}, "down": {"packets": 0, "drops": 0}}

    def run_4g(self, connect: list, echo: list, udp: dict, down: dict) -> dict:
        res = result(connect={"ms": connect}, echo={"ms": echo}, udp=udp, down=down,
                     up={"bytes": 3_000_000, "seconds": 2.2})
        return verdicts(verify.net_checks("4g", NET["4g"], self.BASE, res, self.COUNTERS))

    def test_matching_profile(self):
        got = self.run_4g([70.5, 71.0, 70.9], [70.4] * 20,
                          {"sent": 400, "server_received": 400, "echoed": 400}, {"bytes": 3_000_000, "seconds": 2.2})
        self.assertEqual(got, {"RTT TCP connect": True, "RTT TCP-эха": True, "потери вверх (UDP)": True,
                               "потери вниз (UDP)": True, "скорость вниз (TCP)": True, "скорость вверх (TCP)": True,
                               "netem up: отброшено": None})

    def test_mismatches(self):
        got = self.run_4g([70.5, None, 70.9], [90.0] * 20,
                          {"sent": 400, "server_received": 390, "echoed": 380}, {"bytes": 3_000_000, "seconds": 1.0})
        # пропуск соединения без потерь в профиле, лишние 20 мс, потери, скорость выше предела
        self.assertEqual([k for k, v in got.items() if v is False],
                         ["RTT TCP connect", "RTT TCP-эха", "потери вверх (UDP)", "потери вниз (UDP)",
                          "скорость вниз (TCP)"])

    def test_rate_lower_bound(self):
        up = {"bytes": 3_000_000, "seconds": 2.2}
        # 4g ограничен в 8 раз сильнее заданного (кбит вместо кбайт) — расхождение, а не «не выше предела»
        res = result(down={"bytes": 3_000_000, "seconds": 16.0}, up=up)
        rows = verdicts(verify.net_checks("4g", NET["4g"], self.BASE, res, {}))
        self.assertEqual((rows["скорость вниз (TCP)"], rows["скорость вверх (TCP)"]), (False, True))
        # satellite: 45 % предела — столько и позволяет медленный старт при RTT 600 мс
        res = result(down={"bytes": 3_100_000, "seconds": 5.5}, up={"bytes": 620_000, "seconds": 4.1})
        rows = verdicts(verify.net_checks("satellite", NET["satellite"], self.BASE, res, {}))
        self.assertEqual((rows["скорость вниз (TCP)"], rows["скорость вверх (TCP)"]), (True, True))
        # с потерями нижней границы нет
        res = result(down={"bytes": 3_000_000, "seconds": 16.0}, up=up)
        rows = verdicts(verify.net_checks("lossy", NET["lossy"], self.BASE, res, {}))
        self.assertTrue(rows["скорость вниз (TCP)"])

    def test_phase_errors(self):
        res = result(connect={"error": "TimeoutError"}, udp={"sent": 10, "echoed": 0, "server_received": None,
                                                             "server_error": "TimeoutError: timed out"})
        rows = verify.net_checks("4g", NET["4g"], self.BASE, res, {})
        self.assertTrue(all(r["ok"] is False for r in rows))
        self.assertIn("TimeoutError: timed out", next(r["note"] for r in rows if r["metric"] == "потери UDP"))

    def test_lossy_tolerates_lost_connects(self):
        res = result(connect={"ms": [70.6, None, 71.0]}, echo={"ms": [70.5] * 20})
        rows = verdicts(verify.net_checks("lossy", NET["lossy"], self.BASE, res, {}))
        self.assertTrue(rows["RTT TCP connect"])

    def test_offline(self):
        res = result(connect={"ms": [None]}, udp={"sent": 10, "echoed": 0, "server_received": None})
        self.assertEqual(verdicts(verify.net_checks("offline", NET["offline"], self.BASE, res, {})),
                         {"TCP connect": True, "UDP: доля без ответа": True})
        res = result(connect={"ms": [0.3]}, udp={"sent": 10, "echoed": 10, "server_received": 10})
        self.assertEqual(set(verdicts(verify.net_checks("offline", NET["offline"], self.BASE, res, {})).values()),
                         {False})

    def test_counters_delta(self):
        before = {"up": {"packets": 10, "drops": 1, "bytes": 5}, "down": {"packets": 20, "drops": 0}}
        after = {"up": {"packets": 15, "drops": 3, "bytes": 9}, "down": {"packets": 26, "drops": 0}}
        self.assertEqual(verify.netem_counters(before, after),
                         {"up": {"packets": 5, "drops": 2}, "down": {"packets": 6, "drops": 0}})


class FlakyTest(unittest.TestCase):
    SCHEDULE = NET["flaky"]["schedule"]   # 10 с wifi, 5 с обрыва, по кругу

    def test_outages(self):
        series = [[t * 100.0, None if 3 <= t < 7 or t == 9 else 1.0] for t in range(12)]
        self.assertEqual(verify.outages(series, 100), [(0.3, 0.7)])   # одиночная потеря — не обрыв
        series[-2:] = [[1000.0, None], [1100.0, None]]   # с одиночной потерей перед ними — обрыв до конца серии
        self.assertEqual(verify.outages(series, 100), [(0.3, 0.7), (0.9, 1.2)])

    def test_schedule_outages(self):
        self.assertEqual(verify.schedule_outages(self.SCHEDULE, NET, 0.5, 20), [(9.5, 14.5)])
        self.assertEqual(verify.schedule_outages(self.SCHEDULE, NET, 12, 20), [(0.0, 3.0), (13.0, 18.0)])
        # неповторяющееся: после последнего шага — профиль after до конца пробы; смежные окна сливаются
        once = {"repeat": False, "steps": self.SCHEDULE["steps"], "after": "wifi"}
        self.assertEqual(verify.schedule_outages(once, NET, 0, 40), [(10.0, 15.0)])
        self.assertEqual(verify.schedule_outages(once | {"after": "offline"}, NET, 0, 40), [(10.0, 40.0)])

    def test_flaky_checks(self):
        series = [[t * 100.0, None if 96 <= t < 146 else 2.0] for t in range(200)]
        ping = {"t0": 1000.4, "interval_ms": 100.0, "series": series}
        (row,) = verify.flaky_checks("flaky", self.SCHEDULE, NET, ping, 1000.0)
        self.assertTrue(row["ok"], row)
        ping["series"] = [[t * 100.0, 2.0] for t in range(200)]
        (row,) = verify.flaky_checks("flaky", self.SCHEDULE, NET, ping, 1000.0)
        self.assertFalse(row["ok"])
        self.assertFalse(verify.flaky_checks("flaky", self.SCHEDULE, NET, None, 0)[0]["ok"])


class ReportTest(unittest.TestCase):
    def test_render_and_fmt(self):
        self.assertEqual([verify.fmt(v) for v in (None, 1234.5, 12.34, 1.234, "a|b", {"x": 1.5})],
                         ["—", "1234", "12.3", "1.23", "a/b", "x 1.50"])
        report = {"env": {"utc": "t", "vm": {"name": "sbc", "kernel": "k", "cpus": 4, "mem_kib": 4096 * 1024,
                                              "loadavg": "0", "tainted": 0, "tainted_after": 0, "systemd": "s"},
                          "host": {"macos": "27", "cpu": "M4", "lima": "l", "loadavg": "3.5 3.4 3.2",
                                   "power": "AC"}},
                  "summary": {"rows": 2, "ok": 1, "failed": 1, "info": 0},
                  "rows": [verify.row("cpu", "5 %", "доля CPU", "% ядра", 5, 5, 5.1, True),
                           verify.row("net", "4g", "RTT", "мс", 70, 70.3, 79.0, False, "медиана 3")]}
        text = verify.render(report)
        self.assertIn("Ориентир, не результат", text)
        self.assertIn("loadavg 3.5 3.4 3.2", text)
        self.assertIn("| 5 % | доля CPU | 5 | 5 | 5.10 % ядра | совпадает |  |", text)
        self.assertIn("| 4g | RTT | 70 | 70.3 | 79.0 мс | **расходится** | медиана 3 |", text)
        self.assertNotIn("## Память", text)

    def test_verify_refuses_non_empty_out_and_tainted_kernel(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "verify.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "не пуст"):
                verify.verify(mock.Mock(), DOC, {}, Path(tmp))
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(verify, "environment", return_value={"vm": {"tainted": 512}}):
            with self.assertRaisesRegex(VmError, "tainted=512"):
                verify.verify(mock.Mock(), DOC, {}, Path(tmp) / "new")
            self.assertFalse((Path(tmp) / "new").exists())


class VmCommandsTest(unittest.TestCase):
    def test_board_and_run_commands(self):
        self.assertEqual(vm.board_command("sbc", "net-up", "x", "4g")[:8],
                         ["limactl", "shell", "--workdir", "/", "sbc", "--", "sudo", "bash"])
        args = vm.run_args("t", ["python3", "-c", "print(1)"], mem=64, cpu=50, cpus=1, net="v1", timeout=30,
                           env={"A": "b c"})
        self.assertEqual(args, ["run", "t", "--mem", "64", "--cpu", "50", "--cpus", "1", "--net", "v1",
                                "--timeout", "30", "--env", "A=b c", "--", "python3", "-c", "print(1)"])
        self.assertEqual(vm.probe_command("cpu", workers=2, seconds=3.0)[-5:],
                         ["cpu", "--workers", "2", "--seconds", "3.0"])

    def test_parse_record_and_lines(self):
        stderr = 'warn\nboard-run: {"reason": "x"}\nboard-run: {"reason": "exit"}\n'
        self.assertEqual(vm.parse_record(stderr), {"reason": "exit"})
        self.assertIsNone(vm.parse_record("board: run: нет каталога\n"))
        stdout = '{"phase": "alloc", "mib": 1}\nshell noise\n{"phase": "alloc", "mib": 2}\n{"ops": 1}\n'
        self.assertEqual(vm.parse_lines(stdout), {"alloc": {"phase": "alloc", "mib": 2}})

    def test_vm_run_parses_board_output(self):
        done = mock.Mock(returncode=137, stdout='{"phase": "start", "uid": 501}\n',
                         stderr='board-run: {"reason": "oom-kill", "exit_code": 137}\n')
        with mock.patch.object(vm.subprocess, "run", return_value=done) as run:
            res = vm.Vm("sbc").run("t", ["true"], 10, mem=16, cpu=100)
        self.assertEqual((res.code, res.record["reason"], res.phases["start"]["uid"]), (137, "oom-kill", 501))
        cmd, kwargs = run.call_args.args[0], run.call_args.kwargs
        self.assertEqual(cmd[cmd.index("--timeout") + 1], "10")         # срок юнита — в самой ВМ
        self.assertEqual(kwargs["timeout"], 10 + vm.STOP_MARGIN_S)     # limactl ждёт с запасом

    def test_vm_run_without_record_keeps_output(self):
        done = mock.Mock(returncode=2, stdout='{"phase": "start"}\n', stderr="board: run: нет каталога /x\n")
        with mock.patch.object(vm.subprocess, "run", return_value=done), \
                self.assertRaisesRegex(VmError, "нет строки итога") as ctx:
            vm.Vm("sbc").run("t", ["true"], 10, mem=16, cpu=100)
        self.assertEqual((ctx.exception.stdout, ctx.exception.stderr), (done.stdout, done.stderr))

    def test_vm_run_stops_unit_on_timeout_and_interrupt(self):
        stop = ["sudo", "systemctl", "stop", "board-run-t.service"]
        stopped = mock.Mock(returncode=0, stdout="", stderr="")
        expired = vm.subprocess.TimeoutExpired("limactl", 40, output=b'{"phase": "start"}\n')
        with mock.patch.object(vm.subprocess, "run", side_effect=[expired, stopped]) as run, \
                self.assertRaisesRegex(VmError, "нет ответа за 40 с") as ctx:
            vm.Vm("sbc").run("t", ["sleep", "60"], 10, mem=16, cpu=100)
        self.assertEqual(run.call_args.args[0][-4:], stop)
        self.assertEqual(ctx.exception.stdout, '{"phase": "start"}\n')   # успевшее — текстом
        with mock.patch.object(vm.subprocess, "run", side_effect=[KeyboardInterrupt, stopped]) as run, \
                self.assertRaises(KeyboardInterrupt):
            vm.Vm("sbc").run("t", ["sleep", "60"], 10, mem=16, cpu=100)
        self.assertEqual(run.call_args.args[0][-4:], stop)

    def test_verifier_saves_raw_output_of_a_failed_probe(self):
        machine = mock.Mock()
        machine.run.side_effect = VmError("нет строки итога", "out-text", "err-text")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "raw").mkdir()
            with self.assertRaises(VmError):
                verify.Verifier(machine, DOC, Path(tmp)).run("mem-16", ["true"], 10, mem=16, cpu=100)
            self.assertEqual((Path(tmp) / "raw/mem-16.out").read_text(), "out-text")
            self.assertEqual((Path(tmp) / "raw/mem-16.err").read_text(), "err-text")


class CalibrateTest(unittest.TestCase):
    RUNS = {100: [3100.0, 3000.0, 2900.0], 50: [1000.0, 1200.0, 900.0]}
    TABLE = [calibrate.summarize(q, 1, 1, [{"ops_per_s": o} for o in ops]) for q, ops in RUNS.items()]
    ENV = {"utc": "t", "vm": {"name": "sbc", "kernel": "k", "loadavg": "0", "tainted": 0},
           "host": {"cpu": "M4", "loadavg": "3.5", "power": "AC"},
           "cpubench": {"sha256": "ab" * 32, "go": "go1.27", "goarch": "arm64", "document_sha256": "cd" * 32}}
    REPORT = {"format": "board-calibrate/1", "env": ENV, "seconds": 5.0, "repeats": 3, "table": TABLE}

    def test_summarize(self):
        row = self.TABLE[1]
        self.assertEqual((row["ops_per_s"], row["min"], row["max"], row["per_pct"]), (1000.0, 900.0, 1200.0, 20.0))
        self.assertEqual(len(row["runs"]), 3)

    def test_quota_for_interpolates_between_measurements(self):
        self.assertAlmostEqual(calibrate.quota_for(2000, self.TABLE), 75.0)
        self.assertEqual(calibrate.quota_for(1000, self.TABLE), 50)
        # за пределами таблицы не экстраполируем: скорость при малой квоте нестабильна между прогонами
        self.assertIsNone(calibrate.quota_for(300, self.TABLE))
        self.assertIsNone(calibrate.quota_for(3001, self.TABLE))

    def test_parse_and_render(self):
        self.assertEqual(calibrate.parse_cpubench('noise\n{"ops_per_s": 5}\n\n'), {"ops_per_s": 5})
        with self.assertRaises(VmError):
            calibrate.parse_cpubench("\n")
        text = calibrate.render(self.REPORT, 2000)
        self.assertIn("Ориентир, не результат", text)
        self.assertIn("loadavg 3.5", text)
        self.assertIn("go1.27 arm64", text)
        self.assertIn("| 100 | 1 | 1 | 3000 | 2900–3100 | 30.0 |", text)
        self.assertIn("CPUQuota ≈ 75.0 %", text)
        self.assertIn("вне диапазона таблицы", calibrate.render(self.REPORT, 100))
        self.assertIn("Ориентир, не результат", calibrate.render(self.REPORT, None))


class CliTest(unittest.TestCase):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, err.getvalue()

    def test_calibrate_rejects_quota_below_kernel_minimum(self):
        code, err = self.run_cli("calibrate", "--quotas", "100,4")
        self.assertEqual(code, 2)
        self.assertIn("квоты от 5 %", err)
        code, err = self.run_cli("calibrate", "--quotas", "200")
        self.assertIn("до 100 %", err)

    def test_verify_selection_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, err = self.run_cli("verify", "--profiles", "memory=7", "--out", tmp)
        self.assertEqual(code, 2)
        self.assertIn("memory='7'", err)

    def test_quota_list_parsing(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.parse_args(["calibrate", "--quotas", "a,b"])
        self.assertEqual(cli.parse_args(["calibrate", "--quotas", "50,5"]).quotas, [50, 5])

    def test_calibrate_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(calibrate, "calibrate", return_value=dict(CalibrateTest.REPORT)) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            code, _ = self.run_cli("calibrate", "--quotas", "100,50", "--target", "2000", "--out", tmp)
            doc = json.loads((Path(tmp) / "calibrate.json").read_text())
            self.assertIn("CPUQuota ≈ 75.0 %", (Path(tmp) / "calibrate.md").read_text())
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[1:], ([100, 50], 1, 1, 5.0, 3))   # квоты, ядра, горутины, с, повторы
        self.assertAlmostEqual(doc["quota_for_target"], 75.0)
        self.assertEqual(doc["env"]["cpubench"]["go"], "go1.27")

    def test_interrupt_is_reported_not_traced(self):
        with mock.patch.object(cli, "_verify", side_effect=KeyboardInterrupt):
            code, err = self.run_cli("verify", "--out", "/nonexistent")
        self.assertEqual((code, err), (130, "board: прервано\n"))


if __name__ == "__main__":
    unittest.main()
