"""mock-llm по HTTP: режимы oracle / record / replay, usage и кэш, управление, журнал, CLI-процесс.

Реальных ключей нет: record проверяется на втором экземпляре mock-llm в роли upstream (спецификация §5).
Все серверы — на случайных свободных портах (port=0), процессы останавливаются по PID с таймаутами.
"""
from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.mockllm import RunningServer, ServerConfig  # noqa: E402

PY = sys.executable
TOOLS = [{"type": "function", "function": {"name": "write_property", "description": "Записать свойство.",
                                           "parameters": {"type": "object", "properties": {
                                               "device": {"type": "string", "description": "id"},
                                               "property": {"type": "string", "description": "свойство"},
                                               "value": {"description": "значение"}}}}}]
SYSTEM = {"role": "system", "content": "Ты агент лаборатории. " * 300}   # > 1024 токенов — кэшируемый префикс
CMD = {"input_id": "u1", "text": "включи свет", "expect": {"type": "action", "calls": [
    {"name": "write_property", "args": {"device": "relay1", "property": "on", "value": True}}]}}


def http(method, url, obj=None, headers=None, timeout=10):
    data = None if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def chat(url, messages, headers=None):
    return http("POST", url + "/v1/chat/completions", {"model": "gpt-4o-mini", "messages": messages, "tools": TOOLS},
                headers)


class OracleHttpTest(unittest.TestCase):
    def test_flow_usage_and_journal(self):
        with tempfile.TemporaryDirectory() as d:
            jpath = Path(d) / "j.jsonl"
            with RunningServer(ServerConfig(port=0, journal_path=str(jpath))) as s:
                self.assertEqual(http("POST", s.url + "/control/expect", CMD)[0], 200)
                st, r = chat(s.url, [SYSTEM, {"role": "user", "content": "Включи свет"}])
                self.assertEqual(st, 200)
                self.assertEqual(r["object"], "chat.completion")
                msg = r["choices"][0]["message"]
                self.assertEqual(r["choices"][0]["finish_reason"], "tool_calls")
                u = r["usage"]
                self.assertEqual(u["total_tokens"], u["prompt_tokens"] + u["completion_tokens"])
                self.assertEqual(u["prompt_tokens_details"]["cached_tokens"], 0)
                self.assertGreater(u["prompt_tokens"], 1024)
                call = msg["tool_calls"][0]
                st, r2 = chat(s.url, [SYSTEM, {"role": "user", "content": "Включи свет"}, msg,
                                      {"role": "tool", "tool_call_id": call["id"], "content": '{"ok":true}'}])
                self.assertEqual(r2["choices"][0]["message"]["content"], "Готово.")
                cached = r2["usage"]["prompt_tokens_details"]["cached_tokens"]
                self.assertGreaterEqual(cached, 1024)
                self.assertEqual(cached % 128, 0)
                st, journal = http("GET", s.url + "/control/journal")
            lines = [json.loads(x) for x in jpath.read_text().splitlines()]
            llm = [x for x in lines if x["type"] == "llm"]
            self.assertEqual(len(llm), 2)
            self.assertEqual(llm[0]["oracle"]["decision"], "user_start")
            self.assertEqual(llm[1]["usage"]["cached_tokens"], cached)
            b = llm[0]["bytes"]
            self.assertGreater(b["request_total"], b["request_body"])
            self.assertGreater(b["response_total"], b["response_body"])
            self.assertEqual(set(llm[0]["estimate"]["segments"]), {"tools", "system", "history", "turn", "priming"})
            self.assertIn(b"\"type\":\"control\"", journal if isinstance(journal, bytes) else b"")

    def test_virtual_clock_ttl(self):
        with RunningServer(ServerConfig(port=0, cache_ttl_s=300)) as s:
            msgs = [SYSTEM, {"role": "user", "content": "[heartbeat]"}]
            hdr = lambda t: {"X-Bench-Clock": str(t)}  # noqa: E731
            self.assertEqual(chat(s.url, msgs, hdr(1000))[1]["usage"]["prompt_tokens_details"]["cached_tokens"], 0)
            self.assertGreater(chat(s.url, msgs, hdr(1200))[1]["usage"]["prompt_tokens_details"]["cached_tokens"], 0)
            self.assertEqual(chat(s.url, msgs, hdr(1800))[1]["usage"]["prompt_tokens_details"]["cached_tokens"], 0)
            # то же через /control/clock (так время задаёт реплеер)
            http("POST", s.url + "/control/clock", {"t": 1900})
            self.assertGreater(chat(s.url, msgs)[1]["usage"]["prompt_tokens_details"]["cached_tokens"], 0)
            http("POST", s.url + "/control/clock", {"t": 3000})
            self.assertEqual(chat(s.url, msgs)[1]["usage"]["prompt_tokens_details"]["cached_tokens"], 0)
            self.assertEqual(http("GET", s.url + "/control/state")[1]["virtual_t"], 3000)

    def test_header_expect_base64_and_errors(self):
        with RunningServer(ServerConfig(port=0)) as s:
            raw = base64.b64encode(json.dumps(CMD).encode()).decode()
            st, r = chat(s.url, [{"role": "user", "content": "что угодно"}], {"X-Bench-Expect": raw})
            self.assertEqual(r["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "write_property")
            st, r = chat(s.url, [{"role": "user", "content": "x"}], {"X-Bench-Expect": "%%%"})
            self.assertEqual((st, r["error"]["code"]), (400, "bad_expect_header"))
            st, r = http("POST", s.url + "/v1/chat/completions", {"model": "m", "stream": True,
                                                                  "messages": [{"role": "user", "content": "x"}]})
            self.assertEqual((st, r["error"]["code"]), (400, "stream_unsupported"))
            st, r = http("POST", s.url + "/v1/chat/completions", {"model": "m", "messages": []})
            self.assertEqual(st, 400)
            self.assertEqual(http("POST", s.url + "/control/expect", {"expect": {}})[0], 400)
            self.assertEqual(http("POST", s.url + "/control/nope", {})[0], 404)
            self.assertEqual(http("GET", s.url + "/v1/models")[1]["data"][0]["id"], "mock-oracle")
            self.assertTrue(http("GET", s.url + "/healthz")[1]["ok"])

    def test_control_clear_expire_reset(self):
        with RunningServer(ServerConfig(port=0)) as s:
            http("POST", s.url + "/control/expect", {"input_id": "s1", "kind": "situation",
                                                     "expect": {"type": "notify_or_action"}})
            self.assertTrue(http("POST", s.url + "/control/expire", {"input_id": "s1"})[1]["expired"])
            st, r = chat(s.url, [{"role": "user", "content": "[heartbeat]"}])
            self.assertEqual(r["choices"][0]["message"]["content"], "")
            http("POST", s.url + "/control/expect", CMD)
            self.assertEqual(http("POST", s.url + "/control/clear", {})[1]["cleared"], 2)
            http("POST", s.url + "/control/reset", {"journal": True})
            state = http("GET", s.url + "/control/state")[1]
            # в очищенном журнале остаётся только запись о самом reset
            self.assertEqual((state["journal_records"], state["oracle"]["counts"]), (1, {}))

    def test_edgectx_hint_headers_are_used_and_journaled(self):
        with RunningServer(ServerConfig(port=0)) as s:
            http("POST", s.url + "/control/expect", {**CMD, "text": "не совпадает"})
            hdr = {"X-Edgectx-Input": "u1", "X-Edgectx-Wake": "user", "X-Edgectx-Req-Id": "r-42",
                   "X-Edgectx-Clock": "25200.5"}
            st, r = chat(s.url, [{"role": "user", "content": "Включи свет"}], hdr)
            self.assertEqual(r["choices"][0]["finish_reason"], "tool_calls")
            rec = s.app.journal[-1]
            self.assertEqual(rec["headers"]["x-edgectx-req-id"], "r-42")
            self.assertEqual(rec["t_clock"], 25200.5)

    def test_edgectx_turn_go_header_names(self):
        """Контракт: имена заголовков, которые реально шлёт edgectx (agent/internal/agent/turn.go), понимает
        mock-llm; по ним берутся время запроса (TTL кэша), id входа, причина пробуждения и назначение."""
        from bench.mockllm.server import CLOCK_HEADERS, INPUT_HEADERS, PURPOSE_HEADERS, WAKE_HEADERS
        turn_go = ROOT / "agent" / "internal" / "agent" / "turn.go"
        if not turn_go.exists():
            self.skipTest("нет agent/internal/agent/turn.go")
        import re
        names = {n.lower() for n in re.findall(r'"(X-Edgectx-[A-Za-z-]+)"', turn_go.read_text())}
        self.assertIn("x-edgectx-clock-t", names)
        for want, table in (("x-edgectx-clock-t", CLOCK_HEADERS), ("x-edgectx-input-ids", INPUT_HEADERS),
                            ("x-edgectx-wake-reason", WAKE_HEADERS), ("x-edgectx-purpose", PURPOSE_HEADERS)):
            self.assertIn(want, names)
            self.assertIn(want, table)
        with RunningServer(ServerConfig(port=0, cache_ttl_s=300)) as s:
            http("POST", s.url + "/control/clock", {"t": 30000})      # конец шага реплеера — не время запроса
            http("POST", s.url + "/control/expect", {**CMD, "text": "не совпадает"})
            hdr = {"X-Edgectx-Run-Id": "run1", "X-Edgectx-Req-Id": "r-1", "X-Edgectx-Wake-Id": "w1",
                   "X-Edgectx-Wake-Reason": "user", "X-Edgectx-Input-Ids": "u1", "X-Edgectx-Purpose": "turn",
                   "X-Edgectx-Step": "0", "X-Edgectx-Clock-T": "29944.2"}
            st, r = chat(s.url, [SYSTEM, {"role": "user", "content": "Включи свет"}], hdr)
            self.assertEqual(r["choices"][0]["finish_reason"], "tool_calls")        # по id, а не по тексту
            rec = s.app.journal[-1]
            self.assertEqual((rec["t_clock"], rec["clock_source"]), (29944.2, "header"))
            # фоновое пробуждение с текстом реплики — не ход пользователя
            http("POST", s.url + "/control/expect", {**CMD, "input_id": "u2"})
            st, r = chat(s.url, [{"role": "user", "content": "включи свет"}],
                         {**hdr, "X-Edgectx-Wake-Reason": "heartbeat", "X-Edgectx-Input-Ids": "hb-3"})
            self.assertEqual(s.app.journal[-1]["oracle"]["decision"], "noop")
            # сводка с текстом ожидаемой реплики — ответ-сводка, ожидание u2 цело
            st, r = chat(s.url, [{"role": "system", "content": "Сожми историю"},
                                 {"role": "user", "content": "Новые ходы:\nВход: включи свет"}],
                         {**hdr, "X-Edgectx-Purpose": "summary", "X-Edgectx-Input-Ids": "u1"})
            self.assertEqual(s.app.journal[-1]["oracle"]["decision"], "summary")
            self.assertGreaterEqual(r["usage"]["completion_tokens"], 399)
            pending = [e["input_id"] for e in http("GET", s.url + "/control/state")[1]["oracle"]["expectations"]
                       if e["status"] == "pending"]
            self.assertEqual(pending, ["u2"])
            # запрос edgectx без заголовка часов — предупреждение и счётчик
            hdr2 = {k: v for k, v in hdr.items() if k != "X-Edgectx-Clock-T"}
            chat(s.url, [{"role": "user", "content": "x"}], {**hdr2, "X-Edgectx-Wake-Reason": "heartbeat"})
            rec = s.app.journal[-1]
            self.assertEqual((rec["clock_source"], rec["t_clock"]), ("control", 30000.0))
            self.assertIn("X-Edgectx-Clock-T", rec["warning"])
            st = http("GET", s.url + "/control/state")[1]
            self.assertEqual(st["clock_header_missing"], 1)
            self.assertEqual(st["clock_sources"], {"header": 3, "control": 1})

    def test_reset_makes_runs_reproducible(self):
        """Два прогона на одном экземпляре: после reset {journal: true} те же id вызовов и задержки, журнал —
        только свой (since = journal_seq)."""
        def one_run(url):
            st, r = http("POST", url + "/control/reset", {"journal": True})
            since = r["journal_seq"]
            http("POST", url + "/control/expect", CMD)
            st, r1 = chat(url, [SYSTEM, {"role": "user", "content": "включи свет"}])
            call = r1["choices"][0]["message"]["tool_calls"][0]
            chat(url, [SYSTEM, {"role": "user", "content": "включи свет"}, r1["choices"][0]["message"],
                       {"role": "tool", "tool_call_id": call["id"], "content": "{}"}])
            st, raw = http("GET", url + f"/control/journal?since={since}")
            recs = [json.loads(x) for x in raw.decode().splitlines()] if isinstance(raw, bytes) else []
            llm = [x for x in recs if x["type"] == "llm"]
            return call["id"], [x["latency_ms"]["injected"] for x in llm], [x["req_id"] for x in llm], len(llm), since

        with RunningServer(ServerConfig(port=0, latency="uniform:0:5", seed=7)) as s:
            a, b = one_run(s.url), one_run(s.url)
        self.assertEqual(a[:4], b[:4])          # id вызовов, задержки, id запросов, число записей
        self.assertEqual(a[3], 2)
        self.assertGreater(b[4], a[4])          # номера журнала монотонны

    def test_latency_injection(self):
        with RunningServer(ServerConfig(port=0, latency="fixed:120")) as s:
            t0 = time.perf_counter()
            chat(s.url, [{"role": "user", "content": "x"}])
            self.assertGreaterEqual(time.perf_counter() - t0, 0.11)
            self.assertEqual(s.app.journal[-1]["latency_ms"]["injected"], 120.0)


class RecordReplayTest(unittest.TestCase):
    def test_record_through_second_mock_then_replay(self):
        with tempfile.TemporaryDirectory() as d:
            rec = Path(d) / "rec.jsonl"
            conv = [SYSTEM, {"role": "user", "content": "Включи свет"}]
            with RunningServer(ServerConfig(port=0)) as upstream:
                http("POST", upstream.url + "/control/expect", CMD)
                cfg = ServerConfig(mode="record", port=0, upstream=upstream.url + "/v1", recording_path=str(rec))
                with RunningServer(cfg) as proxy:
                    st, r1 = chat(proxy.url, conv)
                    st2, r_err = http("POST", proxy.url + "/v1/chat/completions",
                                      {"model": "m", "messages": [{"role": "user", "content": "x"}], "stream": True})
                    self.assertEqual(st2, 400)   # отказ mock до upstream, без записи
                    rec_journal = next(x for x in proxy.app.journal if x["type"] == "llm" and x["status"] == 200)
            self.assertEqual(st, 200)
            self.assertEqual(r1["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "write_property")
            self.assertEqual(rec_journal["usage"]["prompt_tokens"], r1["usage"]["prompt_tokens"])
            self.assertIn("estimate", rec_journal)            # своя оценка рядом с «оплаченной» — для калибровки
            self.assertEqual(len(rec.read_text().splitlines()), 1)
            with RunningServer(ServerConfig(mode="replay", port=0, recording_path=str(rec))) as rp:
                st, r2 = chat(rp.url, conv)
                self.assertEqual((st, r2), (200, r1))         # детерминированно, байт в байт по JSON
                st, miss = chat(rp.url, [{"role": "user", "content": "другое"}])
                self.assertEqual((st, miss["error"]["code"]), (404, "replay_miss"))
            cfg = ServerConfig(mode="replay", port=0, recording_path=str(rec), replay_miss="oracle")
            with RunningServer(cfg) as rp:
                st, r3 = chat(rp.url, [{"role": "user", "content": "другое"}])
                self.assertEqual((st, r3["choices"][0]["message"]["content"]), (200, ""))
                self.assertFalse(rp.app.journal[-1]["replay"]["hit"])

    def test_record_upstream_down(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = ServerConfig(mode="record", port=0, upstream="http://127.0.0.1:9/v1",
                               recording_path=str(Path(d) / "r.jsonl"), upstream_timeout_s=2)
            with RunningServer(cfg) as proxy:
                st, r = chat(proxy.url, [{"role": "user", "content": "x"}])
                self.assertEqual((st, r["error"]["code"]), (502, "upstream_unavailable"))

    def test_config_validation(self):
        for bad in (ServerConfig(mode="record"), ServerConfig(mode="replay", recording_path="/nonexistent"),
                    ServerConfig(mode="nope"), ServerConfig(tls_cert="x")):
            with self.assertRaises(ValueError):
                bad.validate()


class CliProcessTest(unittest.TestCase):
    def test_start_listen_and_sigterm(self):
        with tempfile.TemporaryDirectory() as d:
            journal = Path(d) / "j.jsonl"
            proc = subprocess.Popen([PY, "-m", "bench.mockllm", "--port", "0", "--journal", str(journal)],
                                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                line = proc.stdout.readline()
                info = json.loads(line)
                self.assertEqual((info["event"], info["mode"], info["pid"]), ("listening", "oracle", proc.pid))
                url = f"http://127.0.0.1:{info['port']}"
                st, r = chat(url, [{"role": "user", "content": "[heartbeat]"}])
                self.assertEqual(st, 200)
            finally:
                os.kill(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(len(journal.read_text().splitlines()), 1)

    def test_bad_args_exit_code(self):
        r = subprocess.run([PY, "-m", "bench.mockllm", "--mode", "replay", "--port", "0"], cwd=ROOT,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("recording", r.stderr)


if __name__ == "__main__":
    unittest.main()
