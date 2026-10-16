"""Сквозной T0-прогон без Go-агента: mock-llm (oracle) + FakeAgent + FakeSim → реплеер → метрики.

День построен так, что исход известен заранее:
- u1 (команда) → 2 вызова LLM (вызов инструмента + финал);
- s1 возникает в момент открытия двери (датчик гейта) → пробуждение гейта → notify(s1) + финал = 2 вызова,
  реакция через 0 с;
- q1 (вопрос) → answer(26000, t) + финал = 2 вызова, ответ верен;
- heartbeat каждые 30 мин от 07:00: тики 07:30, 08:00, 08:30, 09:00 → 4 пустых вызова (ситуаций нет);
- s2 (без датчика, дедлайн 5 мин) истекает до ближайшего heartbeat → пропущена.
Итого 10 вызовов: user 4, gate 2, heartbeat 4; полнота 1/2.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.metrics import compute_metrics  # noqa: E402
from bench.mockllm import RunningServer, ServerConfig  # noqa: E402
from bench.replay import ReplayConfig, Replayer  # noqa: E402
from bench.replay.fakes import FakeAgent, FakeServer, FakeSim  # noqa: E402

DAY = [
    {"kind": "header", "seed": 1, "profile": "M+", "home": "hh101", "split": "tune", "window": ["07:00", "09:00"],
     "devices": ["relay1", "door1", "pir1"]},
    {"t": 25300, "kind": "sensor", "device": "pir1", "property": "motion", "value": 1},
    {"t": 25500, "kind": "user_cmd", "id": "u1", "text": "включи свет в лаборатории", "lang": "ru",
     "expect": {"type": "action", "calls": [{"name": "write_property",
                                             "args": {"device": "relay1", "property": "on", "value": True}}]}},
    {"t": 26000, "kind": "sensor", "device": "door1", "property": "open", "value": 1},
    {"t": 26000, "kind": "situation", "id": "s1", "name": "door_open", "significant": True, "deadline_s": 300,
     "expect": {"type": "notify_or_action"}},
    {"t": 27000, "kind": "question", "id": "q1", "text": "когда открывали дверь?", "answer": {"value": 26000, "unit": "t"}},
    {"t": 30000, "kind": "situation", "id": "s2", "name": "no_motion", "significant": True, "deadline_s": 300},
]


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        day = d / "day.jsonl"
        day.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in DAY) + "\n", encoding="utf-8")
        trace = d / "edgectx_trace.jsonl"
        with RunningServer(ServerConfig(port=0, cache_ttl_s=300)) as mock:
            agent = FakeAgent(mock.url, str(trace), heartbeat_s=1800)
            with FakeServer.agent(agent) as ag:
                with FakeServer.sim(FakeSim(str(day), ag.url)) as sim:
                    rp = Replayer(ReplayConfig(day=str(day), out_root=str(d / "results"), edgectx=ag.url,
                                               sim=sim.url, mock=mock.url, run_id="e2e", settle_ms=5,
                                               collect={"edgectx_trace": str(trace)}))
                    cls.run_dir = rp.run()
                    rp.close()
            cls.mock_journal = list(mock.app.journal)
        cls.rp = rp
        cls.m = compute_metrics(cls.run_dir)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_replay_clean(self):
        self.assertEqual(self.rp.errors, [])
        self.assertEqual(self.rp.stats["user_inputs"], 2)
        self.assertEqual(self.rp.stats["expired"], 2)

    def test_shutdown_before_collect_gives_run_end(self):
        # ревью: трасса копировалась без остановки агента — без run_end монитор её не принимает
        last = json.loads((self.run_dir / "edgectx_trace.jsonl").read_text().splitlines()[-1])
        self.assertEqual(last["type"], "run_end")
        m = json.loads((self.run_dir / "manifest.json").read_text())
        self.assertEqual((m["edgectx_shutdown"]["requested"], m["edgectx_shutdown"]["run_end"]), (True, True))
        self.assertTrue((self.run_dir / "edgectx_metrics.json").exists())

    def test_second_run_on_same_mock_is_identical(self):
        # ревью: /control/reset не сбрасывал журнал, id вызовов и ГСЧ — второй прогон удваивал трафик
        d = Path(self.tmp.name)
        day = d / "day.jsonl"
        journals = []
        with RunningServer(ServerConfig(port=0, cache_ttl_s=300, latency="uniform:0:2", seed=7)) as mock:
            for run in ("a", "b"):
                trace = d / f"trace_{run}.jsonl"
                agent = FakeAgent(mock.url, str(trace), heartbeat_s=1800, run_id=f"aa-{run}")
                with FakeServer.agent(agent) as ag, FakeServer.sim(FakeSim(str(day), ag.url)) as sim:
                    rp = Replayer(ReplayConfig(day=str(day), out_root=str(d / "aa"), edgectx=ag.url, sim=sim.url,
                                               mock=mock.url, run_id=run, collect={"edgectx_trace": str(trace)}))
                    run_dir = rp.run()
                    rp.close()
                recs = [json.loads(x) for x in (run_dir / "mockllm_journal.jsonl").read_text().splitlines()]
                journals.append([r for r in recs if r.get("type") == "llm"])
                self.assertEqual(compute_metrics(run_dir)["traffic"]["requests"], 10)
        a, b = journals
        self.assertEqual(len(a), len(b))
        key = lambda r: (r["bytes"]["request_body"], r["usage"], r["latency_ms"]["injected"], r["req_id"])  # noqa: E731
        self.assertEqual([key(r) for r in a], [key(r) for r in b])

    def test_calls_by_source(self):
        llm = self.m["llm"]
        self.assertEqual(llm["calls"], 10)
        self.assertEqual(llm["by_source"], {"gate": 2, "heartbeat": 4, "user": 4})
        self.assertEqual(llm["empty_wakes"]["count"], 4)
        decisions = [r["oracle"]["decision"] for r in self.mock_journal if r.get("type") == "llm"]
        self.assertEqual(decisions.count("noop"), 4)
        self.assertEqual(decisions.count("situation"), 1)

    def test_tokens_match_mock_journal(self):
        llm = [r for r in self.mock_journal if r.get("type") == "llm"]
        tok = self.m["tokens"]
        self.assertEqual(tok["prompt"], sum(r["usage"]["prompt_tokens"] for r in llm))
        self.assertEqual(tok["cached"], sum(r["usage"]["cached_tokens"] for r in llm))
        # продолжения ходов (через секунды) попадают в кэш; heartbeat через 30 мин при TTL 5 мин — нет
        hb = tok["by_source"]["heartbeat"]
        self.assertEqual(hb["cached"], 0)
        self.assertGreater(tok["by_source"]["user"]["cached"], 0)
        self.assertEqual(self.m["traffic"]["requests"], 10)
        self.assertLessEqual(self.m["cost_rub"]["observed"], self.m["cost_rub"]["no_cache"])

    def test_situations_and_answers(self):
        s = self.m["situations"]
        self.assertEqual((s["significant"], s["reacted_in_time"], s["missed"]), (2, 1, ["s2"]))
        s1 = next(x for x in s["details"] if x["id"] == "s1")
        self.assertEqual((s1["latency_s"], s1["in_time"]), (0.0, True))
        q = self.m["questions"]
        self.assertEqual((q["total"], q["correct"]), (1, 1))
        self.assertEqual(self.m["commands"], {"total": 1, "replied": 1})
        self.assertIsNone(self.m["safety"]["n_unsafe"])

    def test_metrics_cli_on_run(self):
        r = subprocess.run([sys.executable, "-m", "bench.metrics", str(self.run_dir), "--json"], cwd=ROOT,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["llm"]["calls"], 10)


if __name__ == "__main__":
    unittest.main()
