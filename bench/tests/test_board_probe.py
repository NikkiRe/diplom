"""Пробы bench/board/probe.py и сервер проб bench/board/server.py без ВМ: разбор dd, память и CPU на Mac,
протокол сети на 127.0.0.1 (эхо, объёмы, счётчик UDP), обрыв — порт без ответа."""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.board import probe, server  # noqa: E402
from bench.board.server import ProbeServer  # noqa: E402

ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
DD_STDERR = "16+0 records in\n16+0 records out\n16777216 bytes (17 MB, 16 MiB) copied, 4.0012 s, 4.2 MB/s\n"


def run_probe(*args: str) -> list[dict]:
    """Проба отдельным процессом, как в ВМ: python3 -B probe.py …"""
    done = subprocess.run([sys.executable, "-B", str(ROOT / "bench/board/probe.py"), *args], capture_output=True,
                          text=True, env=ENV, timeout=60, check=True)
    return [json.loads(line) for line in done.stdout.splitlines()]


def capture(fn, *args) -> list[dict]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn(*args)
    return [json.loads(line) for line in out.getvalue().splitlines()]


class LocalProbesTest(unittest.TestCase):
    def test_parse_dd(self):
        self.assertEqual(probe.parse_dd(DD_STDERR), (16777216, 4.0012))
        with self.assertRaises(ValueError):
            probe.parse_dd("dd: failed to open 'x': Permission denied\n")

    def test_dd_runs_use_direct_io(self):
        runs = dict(probe.dd_runs(Path("/w/f.bin"), 8, 40, 600))
        self.assertEqual(list(runs), ["seq_write", "seq_read", "iops_write", "iops_read"])
        self.assertIn("oflag=direct", runs["seq_write"])
        self.assertIn("iflag=direct", runs["seq_read"])
        self.assertEqual([a for a in runs["iops_write"] if a.startswith(("bs=", "count=", "conv="))],
                         ["bs=4k", "count=40", "conv=notrunc"])
        self.assertIn("count=600", runs["iops_read"])

    def test_disk_reports_each_phase_and_removes_file(self):
        results = [subprocess.CompletedProcess([], 0, "", DD_STDERR), subprocess.CompletedProcess([], 1, "", "boom"),
                   subprocess.CompletedProcess([], 0, "", DD_STDERR), subprocess.CompletedProcess([], 0, "", "?")]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(probe.subprocess, "run", side_effect=results):
            path = Path(tmp) / f"board-disk-{os.getpid()}.bin"
            path.write_bytes(b"x")
            lines = capture(probe.disk, Path(tmp), 16, 40, 600)
            self.assertFalse(path.exists())
        self.assertEqual([line["phase"] for line in lines], ["seq_write", "seq_read", "iops_write", "iops_read"])
        self.assertEqual((lines[0]["bytes"], lines[0]["block"]), (16777216, probe.MIB))
        self.assertEqual(lines[1]["error"], "boom")
        self.assertEqual(lines[2]["block"], probe.PAGE)
        self.assertIn("нет итога dd", lines[3]["error"])

    def test_mem_allocates_until_limit(self):
        lines = run_probe("mem", "--max-mib", "3")
        self.assertEqual(lines[0]["phase"], "start")
        self.assertEqual(lines[0]["uid"], os.getuid())
        self.assertEqual([line["mib"] for line in lines[1:]], [1, 2, 3, 3])
        self.assertEqual(lines[-1]["phase"], "done")

    def test_cpu_counts_children(self):
        (line,) = capture(probe.cpu, 2, 0.2)
        self.assertEqual(line["workers"], 2)
        self.assertGreaterEqual(line["wall_s"], 0.2)
        self.assertGreater(line["cpu_s"], 0.1)

    def test_argument_bounds(self):
        for args in (["net", "--host", "h", "--tcp-port", "1", "--udp-port", "1", "--udp-bytes", "8"],
                     ["net", "--host", "h", "--tcp-port", "1", "--udp-port", "70000"],
                     ["net", "--host", "h", "--tcp-port", "1", "--udp-port", "1", "--down-bytes", "-1"],
                     ["cpu", "--workers", "0", "--seconds", "1"], ["mem", "--max-mib", "0"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                probe.parse_args(args)


class NetProbeTest(unittest.TestCase):
    def test_all_phases_against_server(self):
        with ProbeServer() as srv:
            lines = run_probe("net", "--host", "127.0.0.1", "--tcp-port", str(srv.tcp_port), "--udp-port",
                              str(srv.udp_port), "--echo", "4", "--udp-pps", "200", "--udp-seconds", "0.1",
                              "--grace", "0.3", "--down-bytes", "300000", "--up-bytes", "200000")
        phases = {line["phase"]: line for line in lines}
        self.assertEqual(list(phases), ["start", "connect", "echo", "udp", "down", "up"])
        self.assertEqual(len(phases["connect"]["ms"]), 3)
        self.assertEqual(len(phases["echo"]["ms"]), 4)
        self.assertEqual(phases["udp"]["sent"], 20)
        self.assertEqual(phases["udp"]["server_received"], 20)
        self.assertEqual(phases["udp"]["echoed"], 20)
        # скорость считается без первого куска: байт меньше объёма, но не больше
        self.assertTrue(0 < phases["down"]["bytes"] < 300000)
        self.assertTrue(0 < phases["up"]["bytes"] < 200000)
        self.assertEqual(phases["up"]["total"], 200000)

    def test_skipped_phases(self):
        with ProbeServer() as srv:
            lines = run_probe("net", "--host", "127.0.0.1", "--tcp-port", str(srv.tcp_port), "--udp-port",
                              str(srv.udp_port), "--connects", "1", "--echo", "0", "--udp-pps", "0")
        self.assertEqual([line["phase"] for line in lines], ["start", "connect"])

    def test_phase_error_does_not_stop_probe(self):
        with ProbeServer() as srv:
            closed = socket.socket()
            closed.bind(("127.0.0.1", 0))
            port = closed.getsockname()[1]
            closed.close()   # порт без слушателя — отказ в соединении
            lines = run_probe("net", "--host", "127.0.0.1", "--tcp-port", str(port), "--udp-port", str(srv.udp_port),
                              "--echo", "1", "--udp-pps", "100", "--udp-seconds", "0.05", "--grace", "0.2")
        phases = {line["phase"]: line for line in lines}
        self.assertIn("ConnectionRefusedError", phases["connect"]["error"])
        self.assertIn("ConnectionRefusedError", phases["echo"]["error"])
        self.assertEqual(phases["udp"]["echoed"], 5)            # UDP отражён, счётчик спросить не у кого
        self.assertIsNone(phases["udp"]["server_received"])
        self.assertIn("server_error", phases["udp"])

    def test_connect_timeout_is_a_gap(self):
        with mock.patch.object(probe.socket, "create_connection", side_effect=TimeoutError):
            self.assertIsNone(probe.connect_ms(("192.0.2.1", 9), 0.1))

    def test_udp_train_without_answers(self):
        silent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        silent.bind(("127.0.0.1", 0))
        with silent:
            series = probe.udp_train(silent.getsockname(), "s", 100, 0.05, 64, 0.1)
        self.assertEqual(len(series), 5)
        self.assertTrue(all(rtt is None for _, rtt in series))
        self.assertEqual(series, sorted(series))

    def test_ping_series(self):
        with ProbeServer() as srv:
            lines = run_probe("ping", "--host", "127.0.0.1", "--udp-port", str(srv.udp_port), "--seconds", "0.3",
                              "--interval-ms", "100", "--grace", "0.2")
        ping = lines[-1]
        self.assertEqual(ping["phase"], "ping")
        self.assertEqual(ping["series"][0][0], 0.0)
        self.assertEqual(len(ping["series"]), 3)
        self.assertTrue(all(rtt is not None for _, rtt in ping["series"]))


class ServerTest(unittest.TestCase):
    def test_unknown_or_oversized_command_closes(self):
        with ProbeServer() as srv:
            for command in (b"HELLO 1\n", b"DOWN 999999999999\n", b"ECHO 0\n", b"ECHO x\n"):
                with socket.create_connection(("127.0.0.1", srv.tcp_port), timeout=5) as sock:
                    sock.sendall(command)
                    self.assertEqual(sock.recv(16), b"", command)

    def test_session_count_is_bounded(self):
        with ProbeServer() as srv, mock.patch.object(server, "MAX_SESSIONS", 2):
            for session in ("a", "b", "c", "a"):
                srv.count(session)
            self.assertEqual((srv.received("a"), srv.received("b"), srv.received("c")), (2, 1, 0))


if __name__ == "__main__":
    unittest.main()
