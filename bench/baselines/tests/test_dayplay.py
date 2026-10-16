"""Сквозной прогон проигрывателя дня с поддельным агентом: настоящие mock-llm (oracle), sim-mcp и llmtap,
сжатое время, ожидания оракулу, пуш, сводка. Порты 18134–18136 (тестовый диапазон внутри 18120–18139)."""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from bench.baselines import timecomp
from bench.baselines.dayplay import Adapter, DayPlayer, PlayConfig, port_free

ROOT = Path(__file__).resolve().parents[3]
BASE = 18134
TOOLS = [{"type": "function", "function": {"name": n, "description": n, "parameters": {
    "type": "object", "properties": {p: {"type": "string"} for p in props}}}}
    for n, props in (("list_devices", []), ("read_property", ["device", "property"]),
                     ("write_property", ["device", "property", "value"]),
                     ("invoke_action", ["device", "action", "args"]), ("answer", ["value", "unit"]),
                     ("refuse", ["reason"]))]


class FakeAdapter(Adapter):
    """Агент-заглушка: цикл «LLM → инструменты (фиктивный результат) → LLM» через llmtap, heartbeat-поток."""

    system = "fake"
    rules = "generic"
    noop_text = "NOOP"

    def plan(self):
        p = timecomp.Plan("fake", self.cfg.k)
        p.values["heartbeat_wall_s"] = self.cfg.heartbeat_min * 60 / self.cfg.k
        return p

    def prepare(self, ep, plan, day):
        self.ep, self.hb_s = ep, plan.values["heartbeat_wall_s"]
        self.stop_ev = threading.Event()
        self.calls = 0
        return {"fake": True}

    def _chat(self, text):
        msgs = [{"role": "system", "content": "fake agent"}, {"role": "user", "content": text}]
        for _ in range(6):
            req = urllib.request.Request(self.ep.llm_base_url + "/chat/completions", method="POST",
                                         data=json.dumps({"model": "m", "messages": msgs, "tools": TOOLS}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                resp = json.loads(r.read())
            self.calls += 1
            m = resp["choices"][0]["message"]
            if not m.get("tool_calls"):
                return m.get("content") or ""
            msgs.append(m)
            msgs += [{"role": "tool", "tool_call_id": tc["id"], "content": '{"ok":true}'} for tc in m["tool_calls"]]
        return ""

    def start(self):
        def hb():
            while not self.stop_ev.wait(self.hb_s):
                self._chat("heartbeat: плановая проверка")
        threading.Thread(target=hb, daemon=True).start()

    def wait_ready(self, timeout):
        pass

    def root_pids(self):
        return [os.getpid()]

    def send_user(self, input_id, text, timeout):
        return {"ok": True, "reply": self._chat(text)}

    def push_event(self, text):
        threading.Thread(target=self._chat, args=(text,), daemon=True).start()
        return {"ok": True}

    def stop(self):
        self.stop_ev.set()
        return {"calls": self.calls}


@pytest.fixture(scope="module")
def day(tmp_path_factory):
    p = tmp_path_factory.mktemp("day") / "day.jsonl"
    subprocess.run([sys.executable, "-m", "bench.daygen", "gen", "--seed", "3", "--source", "synthetic",
                    "--profile", "S200", "--window", "07:00-07:30", "--rate-scale", "0.3", "--out", str(p)],
                   cwd=ROOT, check=True, capture_output=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    return p


@pytest.mark.skipif(not all(port_free(BASE + i) for i in (0, 1, 2)), reason="test ports 18134-18136 busy")
def test_fake_agent_day(day, tmp_path):
    cfg = PlayConfig(day=str(day), k=300, out=str(tmp_path), run_id="fake1", port_base=BASE, push="all",
                     heartbeat_min=10, warmup_s=0.2, drain_s=2, sample_interval_s=0.2, mcp=())
    t0 = time.time()
    run_dir = DayPlayer(cfg, FakeAdapter).run()
    wall = time.time() - t0
    assert wall < 60
    man = json.loads((run_dir / "manifest.json").read_text())
    s = json.loads((run_dir / "summary.json").read_text())
    counts = man["day"]["header"]["counts"]
    users = counts.get("user_cmd", 0) + counts.get("question", 0)
    assert users >= 1
    assert s["user"]["sent"] == users and s["user"]["ok"] == users
    by = s["llm"]["day_by_source"]
    assert by["user"]["calls"] >= 2 * users                     # k_eff ≥ 2 у входа с инструментом
    assert s["push"]["sent"] == man["stats"]["pushes"] == counts["sensor"]       # push=all
    assert by.get("event", {}).get("calls", 0) >= 1
    assert by.get("heartbeat", {}).get("calls", 0) >= 1          # 30 мин дня / 10 мин
    assert s["llm"]["unmatched_mock"] == 0
    assert s["llm"]["by_phase"]["day"]["prompt_tokens"] > 0
    assert s["sim_eval"]["commands"]["n"] == counts.get("user_cmd", 0)
    assert man["timecomp"]["mock"]["values"]["cache_ttl_s"] == pytest.approx(1.0)
    for f in ("play.jsonl", "llmtap.jsonl", "mockllm_journal.jsonl", "simmcp_report.json", "procmon.jsonl",
              "summary.md", "llm_calls.jsonl", "day.jsonl"):
        assert (run_dir / f).exists(), f
    # сервисы остановлены, порты свободны
    time.sleep(0.5)
    assert all(port_free(BASE + i) for i in (0, 1, 2))
