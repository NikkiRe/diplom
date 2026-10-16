"""llmtap: проброс, SSE, классификация источника вызова, подсказки оракулу, укорачивание сводки."""
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bench.baselines.llmtap import EVENT_MARKER, RULES, TapServer, classify, fit_summary, sse_from_completion


class Upstream:
    """Поддельный mock-llm: отвечает заданным chat.completion и запоминает заголовки/тела."""

    def __init__(self, message: dict, finish="stop"):
        self.message, self.finish = message, finish
        self.calls = []
        up = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                up.calls.append((dict(self.headers), body))
                n = len(up.calls)
                resp = {"id": f"chatcmpl-mock-{n:06d}", "object": "chat.completion", "created": 1, "model": "m",
                        "choices": [{"index": 0, "message": up.message, "finish_reason": up.finish}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 400, "total_tokens": 500,
                                  "prompt_tokens_details": {"cached_tokens": 0}}}
                data = json.dumps(resp, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def post(port, body, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", "/v1/chat/completions", body=json.dumps(body).encode(),
              headers={"Content-Type": "application/json", **(headers or {})})
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()


def first_record(s, timeout=5.0):
    """Первая запись tap. Её добавляют после отправки ответа (в ней байты ответа), поэтому клиент получает ответ
    чуть раньше, чем она появляется."""
    deadline = time.monotonic() + timeout
    while not s.tap.records:
        assert time.monotonic() < deadline, "нет записи tap"
        time.sleep(0.01)
    return s.tap.records[0]


TOOLS = [{"type": "function", "function": {"name": "mcp_sim_write_property", "parameters": {"type": "object"}}}]


def test_passthrough_and_record(tmp_path):
    up = Upstream({"role": "assistant", "content": "Готово."})
    try:
        with TapServer(up.url, RULES["picoclaw"], journal=str(tmp_path / "tap.jsonl")) as s:
            body = {"model": "m", "messages": [{"role": "system", "content": "sys"},
                                               {"role": "user", "content": "включи свет"}], "tools": TOOLS}
            st, h, data = post(s.port, body, {"X-Custom": "1"})
            assert st == 200 and json.loads(data)["choices"][0]["message"]["content"] == "Готово."
            hdrs, fwd = up.calls[0]
            assert fwd == body                                   # тело без изменений
            assert hdrs["X-Bench-Wake"] == "user" and hdrs["X-Bench-Purpose"] == "turn" and hdrs["X-Custom"] == "1"
            rec = first_record(s)
            assert rec["cls"] == "user" and rec["mock_id"] == "chatcmpl-mock-000001" and rec["n_tools"] == 1
            assert rec["bytes"]["request_body"] == len(json.dumps(body).encode())
        lines = (tmp_path / "tap.jsonl").read_text().splitlines()
        assert len(lines) == 1 and json.loads(lines[0])["seq"] == 1
    finally:
        up.close()


def test_stream_converted_to_sse():
    msg = {"role": "assistant", "content": None,
           "tool_calls": [{"id": "call_1", "type": "function",
                           "function": {"name": "mcp_sim_write_property", "arguments": '{"device":"x"}'}}]}
    up = Upstream(msg, finish="tool_calls")
    try:
        with TapServer(up.url, RULES["nanobot"]) as s:
            body = {"model": "m", "stream": True, "stream_options": {"include_usage": True},
                    "messages": [{"role": "user", "content": "hi"}], "tools": TOOLS}
            st, h, data = post(s.port, body)
            assert st == 200 and h["Content-Type"].startswith("text/event-stream")
            _, fwd = up.calls[0]
            assert "stream" not in fwd and "stream_options" not in fwd
            events = [e[len("data: "):] for e in data.decode().split("\n\n") if e]
            assert events[-1] == "[DONE]"
            chunks = [json.loads(e) for e in events[:-1]]
            assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
            tc = [c for c in chunks if c["choices"] and c["choices"][0]["delta"].get("tool_calls")]
            assert tc[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "mcp_sim_write_property"
            assert any(c["choices"] and c["choices"][0]["finish_reason"] == "tool_calls" for c in chunks)
            assert chunks[-1]["choices"] == [] and chunks[-1]["usage"]["prompt_tokens"] == 100
            assert first_record(s)["stream"] is True
    finally:
        up.close()


def test_sse_without_usage():
    out = sse_from_completion({"id": "x", "choices": [{"message": {"content": "a"}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 1}}, include_usage=False).decode()
    assert '"usage"' not in out and out.endswith("data: [DONE]\n\n")


def B(user, system="sys", tools=True, extra=None):
    msgs = [{"role": "system", "content": system}] + (extra or []) + [{"role": "user", "content": user}]
    return {"messages": msgs, "tools": TOOLS if tools else []}


@pytest.mark.parametrize("body,cls", [
    (B("# Heartbeat Check\n\nCurrent time: x"), "heartbeat"),
    (B(f"{EVENT_MARKER} 07:00:00 door_front.open: 0 → 1"), "event"),
    (B("You summarize a SEGMENT …\nAggressive summary policy:\n- Keep", tools=False), "internal:summary_retry"),
    (B("You summarize a SEGMENT …\nNormal summary policy:", tools=False), "internal:summary"),
    (B("сделай кофе"), "user"),
])
def test_classify_picoclaw(body, cls):
    assert classify(body, RULES["picoclaw"])[0] == cls


@pytest.mark.parametrize("body,cls", [
    (B("[Your response …] You are executing periodic heartbeat tasks. Read…"), "heartbeat"),
    (B("## Original task\n… You are executing periodic heartbeat tasks …",
       system="You are a notification gate for a background agent."), "internal:heartbeat_eval"),
    (B("You are running Dream. Consolidate the conversation"), "internal:dream"),
    (B("Create a compact replacement checkpoint for this session.", tools=False), "internal:compact"),
    (B("Please provide your response to the user based on the conversation above.", tools=False,
       extra=[{"role": "user", "content": f"{EVENT_MARKER} 07:29:02 leak_bathroom.wet: 0 → 1"},
              {"role": "assistant", "content": ""}]), "event"),
    (B("выключи свет"), "user"),
])
def test_classify_nanobot(body, cls):
    assert classify(body, RULES["nanobot"])[0] == cls


def test_internal_gets_summary_purpose_and_eval_gets_turn():
    up = Upstream({"role": "assistant", "content": "x"})
    try:
        with TapServer(up.url, RULES["nanobot"]) as s:
            post(s.port, B("Create a compact replacement checkpoint", tools=False))
            post(s.port, B("## Original task", system="You are a notification gate for a background agent."))
            h1, h2 = up.calls[0][0], up.calls[1][0]
            assert h1["X-Bench-Purpose"] == "summary" and "X-Bench-Wake" not in h1
            assert h2["X-Bench-Purpose"] == "turn" and h2["X-Bench-Wake"] == "heartbeat"
    finally:
        up.close()


def test_fit_summary_truncates_to_target():
    text = "слово " * 600
    resp = {"choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 400, "total_tokens": 410}}
    body = B("You summarize …\n- Target length: about 50 tokens or less.\n<conversation_segment>", tools=False)
    adj = fit_summary(body, resp)
    assert adj["target"] == 50 and adj["completion_after"] <= 50
    assert len(resp["choices"][0]["message"]["content"]) <= int(2.25 * 50)
    assert resp["usage"]["completion_tokens"] == adj["completion_after"]
    assert fit_summary(B("no target", tools=False), resp) is None


def test_fit_summary_through_tap():
    up = Upstream({"role": "assistant", "content": "факт " * 500})
    try:
        with TapServer(up.url, RULES["picoclaw"]) as s:
            st, _, data = post(s.port, B("Normal summary policy:\n- Target length: about 80 tokens or less.",
                                         tools=False))
            resp = json.loads(data)
            assert resp["usage"]["completion_tokens"] <= 80
            assert first_record(s)["usage_adjusted"]["completion_before"] == 400
        with TapServer(up.url, RULES["picoclaw"], fit_summary=False) as s2:
            _, _, data = post(s2.port, B("Normal summary policy:\n- Target length: about 80 tokens", tools=False))
            assert json.loads(data)["usage"]["completion_tokens"] == 400
    finally:
        up.close()


def test_edgectx_headers_override_text_rules():
    from bench.baselines.llmtap import header_class

    assert header_class({"X-Edgectx-Wake-Reason": "gate"}) == "event"
    assert header_class({"X-Edgectx-Wake-Reason": "heartbeat"}) == "heartbeat"
    assert header_class({"X-Edgectx-Purpose": "summary", "X-Edgectx-Wake-Reason": "user"}) == "internal:summary"
    assert header_class({"Content-Type": "application/json"}) is None
    up = Upstream({"role": "assistant", "content": "ok"})
    try:
        with TapServer(up.url, RULES["generic"], hints=False) as s:
            post(s.port, B("сделай кофе"), {"X-Edgectx-Wake-Reason": "rule"})
            assert first_record(s)["cls"] == "rule"
            assert "X-Bench-Wake" not in up.calls[0][0]          # без подсказок: у EdgeCtx свои заголовки
            assert up.calls[0][0]["X-Edgectx-Wake-Reason"] == "rule"
    finally:
        up.close()
