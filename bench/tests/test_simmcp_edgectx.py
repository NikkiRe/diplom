"""Сквозная проверка «настоящий edgectx + настоящий sim-mcp + mock-llm (oracle) + реплеер» на дне daygen.

Ловит то, чего не видят тесты с двойниками: совместимость ресурсов sim-mcp с разбором edgectx
(hw://inventory → L0, hw://state/<dev> → L1/L2, уведомления → гейт). До исправления формата ресурсов
агент отвергал каждое обновление состояния (errors.mcp_state = mcp.updates, hw.log_events = 0), а в промпт
попадали подписи list_devices вместо значений.

edgectx собирается из agent/ (нужен go) или берётся из EDGECTX_BIN. Порты — свободные эфемерные.
Конфигурация агента — bench/simmcp/edgectx_simday.yaml (сигналы гейта под имена устройств daygen).
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.daygen.generator import DayConfig, generate_day, write_day  # noqa: E402
from bench.mockllm import RunningServer, ServerConfig  # noqa: E402
from bench.replay import ReplayConfig, Replayer  # noqa: E402

CONFIG = ROOT / "bench" / "simmcp" / "edgectx_simday.yaml"
GO = shutil.which("go")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get_json(url: str, timeout: float = 5) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def wait_http(url: str, proc: subprocess.Popen, log: Path, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            get_json(url, 2)
            return
        except OSError:
            if proc.poll() is not None or time.monotonic() > deadline:
                raise AssertionError(f"{url} не поднялся: {log.read_text(errors='replace')[-2000:]}") from None
            time.sleep(0.1)


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        os.kill(proc.pid, signal.SIGTERM)
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)


def edgectx_binary(tmp: Path) -> Path:
    if os.environ.get("EDGECTX_BIN"):
        return Path(os.environ["EDGECTX_BIN"])
    out = tmp / "edgectx"
    r = subprocess.run([GO, "build", "-trimpath", "-o", str(out), "./cmd/edgectx"], cwd=ROOT / "agent",
                       env={**os.environ, "CGO_ENABLED": "0"}, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise AssertionError(f"сборка edgectx не удалась: {r.stderr[-2000:]}")
    return out


@unittest.skipUnless(GO or os.environ.get("EDGECTX_BIN"), "нет go и EDGECTX_BIN — edgectx не собрать")
class EdgectxWithSimMcpTest(unittest.TestCase):
    SIM_ARGS: tuple[str, ...] = ()

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        d = cls.tmp = Path(cls._tmp.name)
        binary = edgectx_binary(d)
        # день: синтетический дом, 07:00–09:00, нагрузка H × 0,15 (≈ 4–5 реплик), перегрев и протечка
        cls.h, cls.ev = generate_day(DayConfig(seed=4, profile="H", source="synthetic", split="tune",
                                               window=("07:00", "09:00"), rate_scale=0.15, question_share=0.4))
        cls.day = d / "day.jsonl"
        write_day(cls.day, cls.h, cls.ev)
        cls.procs: list[subprocess.Popen] = []
        cls.bodies: list[dict] = []
        sim_port, edge_port = free_port(), free_port()
        cls.sim_url, cls.edge_url = f"http://127.0.0.1:{sim_port}", f"http://127.0.0.1:{edge_port}"
        cls.sim_journal = d / "sim.jsonl"
        sim_log = d / "sim.log"
        cls.procs.append(subprocess.Popen(
            [sys.executable, "-m", "bench.simmcp", "--day", str(cls.day), "--port", str(sim_port),
             "--journal", str(cls.sim_journal), "--run-id", "e2e", *cls.SIM_ARGS],
            cwd=ROOT, stdout=open(sim_log, "w"), stderr=subprocess.STDOUT))
        try:
            wait_http(cls.sim_url + "/healthz", cls.procs[-1], sim_log)
            cls.mock = RunningServer(ServerConfig(port=0)).__enter__()
            orig_chat = cls.mock.app.chat

            def chat(body, headers, seq):          # перехват промптов агента
                cls.bodies.append(body)
                return orig_chat(body, headers, seq)
            cls.mock.app.chat = chat
            cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
            cfg["llm"]["base_url"] = cls.mock.url + "/v1"
            cfg["mcp"][0]["url"] = cls.sim_url + "/mcp"
            cfg["userctx"]["db"] = str(d / "edgectx.db")
            cfg_path = d / "edgectx.yaml"
            cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
            cls.trace = d / "edgectx_trace.jsonl"
            edge_log = d / "edgectx.log"
            cls.procs.append(subprocess.Popen(
                [str(binary), "-config", str(cfg_path), "-listen", f"127.0.0.1:{edge_port}", "-trace",
                 str(cls.trace), "-run-id", "e2e"], cwd=d, stdout=open(edge_log, "w"), stderr=subprocess.STDOUT))
            wait_http(cls.edge_url + "/v1/health", cls.procs[-1], edge_log)
            rp = Replayer(ReplayConfig(day=str(cls.day), out_root=str(d / "results"), edgectx=cls.edge_url,
                                       sim=cls.sim_url, mock=cls.mock.url, run_id="e2e", settle_ms=25,
                                       collect={"edgectx_trace": str(cls.trace)}))
            cls.run_dir = rp.run()
            rp.close()
            cls.replay_errors = rp.errors
            # реплеер снимает /v1/metrics до POST /v1/shutdown (после него агент не отвечает)
            cls.metrics = json.loads((cls.run_dir / "edgectx_metrics.json").read_text(encoding="utf-8"))
            cls.sim_health = get_json(cls.sim_url + "/healthz")
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        for p in reversed(getattr(cls, "procs", [])):
            stop(p)
        if getattr(cls, "mock", None) is not None:
            cls.mock.__exit__(None, None, None)
            cls.mock = None
        cls._tmp.cleanup()

    def trace_records(self) -> list[dict]:
        return [json.loads(x) for x in self.trace.read_text(encoding="utf-8").splitlines() if x.strip()]

    def test_replay_clean(self):
        self.assertEqual(self.replay_errors, [])

    def test_state_updates_parsed(self):
        """Каждое уведомление sim-mcp разобрано агентом: ни одной ошибки mcp_state, журнал L2 не пуст."""
        m = self.metrics
        updates = m.get("mcp", {}).get("updates", 0)
        n_sensor = sum(1 for e in self.ev if e["kind"] == "sensor")
        self.assertGreater(updates, 0.5 * n_sensor, m.get("mcp"))   # объединение в sim-mcp может сократить число
        self.assertEqual(m.get("errors", {}).get("mcp_state", 0), 0, m.get("errors"))
        self.assertEqual(m.get("errors", {}).get("mcp_read", 0), 0, m.get("errors"))
        self.assertGreater(m["hw"]["log_events"], 0.5 * n_sensor, m["hw"])
        self.assertEqual(m["hw"]["devices"], len(self.h["devices"]))
        recs = self.trace_records()
        self.assertGreater(sum(1 for r in recs if r["type"] == "input" and r.get("kind") == "sensor"), 0)
        self.assertFalse([r for r in recs if r["type"] == "error" and "состояние" in json.dumps(r, ensure_ascii=False)])

    def test_prompt_has_values_not_signatures(self):
        """L0 — нормальная проекция инвентаря, в промпте нет подписей list_devices вместо значений."""
        self.assertTrue(self.bodies, "агент ни разу не обратился к LLM")
        text = json.dumps([b["messages"] for b in self.bodies], ensure_ascii=False)
        self.assertIn("brightness [%, rw, 1..100]", text)
        # до исправления в секции состояния были подписи: temperature="Cel", heating="bool,rw", open="0|1"
        for bad in ('=\\"Cel\\"', '=\\"bool,rw\\"', '=\\"0|1\\"', '%,1..100,rw'):
            self.assertNotIn(bad, text)

    def test_gate_wakes_on_sensor_situations(self):
        """Перегрев и протечка (синтетика daygen) будят гейт агента — сигналы конфигурации совпадают с именами."""
        recs = self.trace_records()
        gate_wakes = [r for r in recs if r["type"] == "wake" and r.get("reason") == "gate"]
        self.assertTrue(gate_wakes, "гейт не проснулся ни разу")

    def test_sim_report(self):
        rep = json.loads((self.run_dir / "simmcp_report.json").read_text(encoding="utf-8"))
        s = rep["summary"]
        self.assertEqual(s["inputs_opened"], s["inputs_total"])
        self.assertGreater(s["commands"]["n"], 0)
        self.assertEqual(s["commands"]["rate"], 1.0, [r for r in rep["inputs"] if r["kind"] == "user_cmd"])
        self.assertEqual(s["situations_with_actions"]["rate"], 1.0,
                         [r for r in rep["inputs"] if r["kind"] == "situation"])
        journal = [json.loads(x) for x in self.sim_journal.read_text(encoding="utf-8").splitlines()]
        subs = {r["uri"] for r in journal if r["type"] == "subscribe"}
        self.assertEqual(subs, {f"hw://state/{d['id']}" for d in self.h["devices"]})   # фильтр */state/*
        self.assertEqual(max(r.get("sessions", 0) for r in journal if r["type"] == "clock"), 1)
        self.assertEqual(self.sim_health["notify_failed"], 0)
        self.assertEqual(self.sim_health["sessions"], 0)       # агент остановлен реплеером — сессия закрыта


class EdgectxWithSimMcpInlineStateTest(EdgectxWithSimMcpTest):
    """--inline-state: новые значения в _meta.state уведомления — агент не читает ресурс на каждое изменение."""

    SIM_ARGS = ("--inline-state",)

    def test_no_resource_read_per_update(self):
        updates = self.metrics["mcp"]["updates"]
        reads = self.sim_health["resource_reads"]
        self.assertLess(reads, 0.2 * updates, (reads, updates))   # только начальное чтение инвентаря и состояния


if __name__ == "__main__":
    unittest.main()
