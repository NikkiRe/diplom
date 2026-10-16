"""Интеграция реплеера с настоящим sim-mcp (bench/simmcp): день daygen, процесс sim-mcp на свободном порту,
mock-llm в процессе теста, edgectx — записывающий двойник. Проверяется контракт /control/clock, /control/input,
/control/finish (спецификация §5.1). Если компонент sim-mcp/daygen недоступен — тест пропускается."""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.mockllm import RunningServer, ServerConfig  # noqa: E402
from bench.replay import ReplayConfig, Replayer  # noqa: E402
from bench.replay.fakes import FakeServer  # noqa: E402

try:   # чужие компоненты могут быть в работе — тогда тест пропускается
    HAVE_SIM = all(importlib.util.find_spec(m) is not None for m in ("bench.daygen", "bench.simmcp"))
except (ImportError, ValueError):
    HAVE_SIM = False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(HAVE_SIM, "bench.simmcp / bench.daygen недоступны")
class RealSimMcpTest(unittest.TestCase):
    def test_replay_against_real_simmcp(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            day = d / "day.jsonl"
            r = subprocess.run([sys.executable, "-m", "bench.daygen", "gen", "--seed", "11", "--profile", "H",
                                "--source", "synthetic", "--window", "07:00-08:30", "--out", str(day)],
                               cwd=ROOT, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                self.skipTest(f"daygen не сгенерировал день: {r.stderr[-300:]}")
            port = free_port()
            sim = subprocess.Popen([sys.executable, "-m", "bench.simmcp", "--day", str(day), "--port", str(port),
                                    "--journal", str(d / "sim.jsonl")], cwd=ROOT,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            try:
                url = f"http://127.0.0.1:{port}"
                deadline = time.monotonic() + 30
                while True:
                    try:
                        with urllib.request.urlopen(url + "/healthz", timeout=2):
                            break
                    except OSError:
                        if sim.poll() is not None or time.monotonic() > deadline:
                            self.skipTest(f"sim-mcp не запустился: {sim.stderr.read()[-300:] if sim.stderr else ''}")
                        time.sleep(0.1)
                users = []
                edge = FakeServer({("POST", "/v1/clock/advance"): lambda b: {"t": b["to_t"]},
                                   ("POST", "/v1/user"): lambda b: (users.append(b["id"]), {"reply": "ok"})[1],
                                   ("GET", "/v1/metrics"): lambda b: {}})
                with RunningServer(ServerConfig(port=0)) as mock, edge:
                    rp = Replayer(ReplayConfig(day=str(day), out_root=str(d / "results"), edgectx=edge.url, sim=url,
                                               mock=mock.url, run_id="sim", settle_ms=0, max_step_s=300,
                                               collect={"simmcp_journal": str(d / "sim.jsonl")}))
                    run_dir = rp.run()
                    rp.close()
                self.assertEqual(rp.errors, [])
                with urllib.request.urlopen(url + "/control/clock", timeout=5) as resp:
                    clock = json.loads(resp.read())
                self.assertAlmostEqual(float(clock.get("t", clock.get("clock", -1))), 8.5 * 3600, places=3)
                rep = json.loads((run_dir / "simmcp_report.json").read_text())
                self.assertIn("summary", rep)
                n_user = sum(1 for line in day.read_text().splitlines()[1:]
                             if json.loads(line)["kind"] in ("user_cmd", "question"))
                self.assertEqual(len(users), n_user)
                # каждая реплика открыта в sim-mcp (его inputs_total включает ещё и ситуации)
                summ = rep["summary"]
                self.assertEqual(summ["commands"]["n"] + summ["questions"]["n"], n_user)
            finally:
                os.kill(sim.pid, signal.SIGTERM)
                try:
                    sim.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    sim.kill()
                    sim.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
