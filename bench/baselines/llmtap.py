"""llmtap — прозрачный прокси «агент → mock-llm» для аналогов.

Зачем:
1. **SSE.** nanobot 0.3.5 всегда вызывает провайдера потоком (`chat_stream_with_retry`, `stream: true`), а
   mock-llm поток не поддерживает (400 stream_unsupported). Прокси отправляет в mock-llm тот же запрос без
   `stream`/`stream_options` и отдаёт агенту ответ в формате OpenAI SSE (chat.completion.chunk + usage +
   [DONE]). На токены это не влияет: mock-llm считает их по messages/tools.
2. **Журнал вызовов с источником.** У аналогов нет заголовков X-Edgectx-*: прокси по тексту последнего
   сообщения user определяет, что разбудило LLM — реплика пользователя, heartbeat, событие (пуш), внутренний
   вызов (сводка/компакция/Dream/оценщик heartbeat) — и пишет журнал с байтами «как на проводе» агента.
3. **Подсказки оракулу** (по умолчанию включены): `X-Bench-Wake` (user | heartbeat | gate) и
   `X-Bench-Purpose` (turn | summary). Внутренние вызовы помечаются как summary — оракул отвечает сводкой
   фиксированной длины и не трогает ожидания реплик и ситуаций (иначе вызов компакции мог бы «съесть»
   реакцию на ситуацию). Тело запроса агента не меняется (кроме снятия stream для mock-llm).
4. **Длина сводки «как просили».** Оракул отвечает на сводку текстом фиксированной длины (--summary-tokens,
   400 — summ_len модели затрат). Если запрос сводки задаёт цель («Target length: about N tokens», seahorse
   PicoClaw), идеальная модель её соблюдает: прокси укорачивает ответ до N токенов o200k и до 2,25·N символов
   (оценщик PicoClaw — 2,5 символа на токен) и правит usage.completion_tokens; иначе seahorse на каждую
   сводку делал бы второй, «агрессивный» вызов — артефакт оракула, а не свойство системы. Исходные и
   новые значения — в журнале (usage_adjusted); отключается --no-fit-summary.

    python -m bench.baselines.llmtap --upstream http://127.0.0.1:18120 --port 18129 --journal tap.jsonl \
        --rules picoclaw
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

EVENT_MARKER = "[датчик]"     # префикс текста события, который подают драйверы (pushfilter.format_event)


@dataclass
class Rule:
    cls: str                    # user | heartbeat | event | internal:<вид>
    pattern: str                # регулярное выражение по тексту последнего сообщения user (re.S)
    tools: bool | None = None   # требовать наличие (True) / отсутствие (False) tools; None — всё равно
    system: bool = False        # искать по system, а не по user

    def match(self, user_text: str, system_text: str, has_tools: bool) -> bool:
        if self.tools is not None and self.tools != has_tools:
            return False
        return re.search(self.pattern, system_text if self.system else user_text, re.S) is not None


# Правила по системам. Маркеры — из исходников на тегах v0.3.1 / v0.3.5 (см. README, раздел «Классификация»).
RULES: dict[str, list[Rule]] = {
    "picoclaw": [
        Rule("event", re.escape(EVENT_MARKER)),
        Rule("heartbeat", r"^# Heartbeat Check"),                    # pkg/heartbeat/service.go buildPrompt
        Rule("internal:summary_retry", r"Aggressive summary policy", tools=False),   # seahorse, уровень 2
        Rule("internal:summary", r"(?i)summar", tools=False),         # legacy-сводка истории / seahorse
        Rule("internal:other", r".", tools=False),
    ],
    "nanobot": [
        Rule("event", re.escape(EVENT_MARKER)),
        Rule("internal:heartbeat_eval", r"^\s*You are a notification gate", system=True),  # templates/agent/evaluator.md
        Rule("heartbeat", r"You are executing periodic heartbeat tasks"),   # cli/gateway_runtime.py
        Rule("internal:dream", r"You are running Dream"),                    # templates/agent/dream.md
        Rule("internal:dream", r"You are running Dream", system=True),
        Rule("internal:compact", r"Create a compact replacement checkpoint"),   # consolidator_archive.md
        Rule("internal:compact", r"Create a compact replacement checkpoint", system=True),
        Rule("internal:other", r".", tools=False),
    ],
    "generic": [
        Rule("event", re.escape(EVENT_MARKER)),
        Rule("heartbeat", r"(?i)heartbeat"),
        Rule("internal:other", r".", tools=False),
    ],
}

# EdgeCtx сам сообщает причину пробуждения и назначение запроса (agent/internal/agent/turn.go)
EDGECTX_WAKE_CLASS = {"user": "user", "gate": "event", "heartbeat": "heartbeat", "rule": "rule"}


def header_class(headers: dict) -> str | None:
    h = {k.lower(): v for k, v in headers.items()}
    if (h.get("x-edgectx-purpose") or "").lower() == "summary":
        return "internal:summary"
    wake = (h.get("x-edgectx-wake-reason") or h.get("x-edgectx-wake") or "").lower()
    return EDGECTX_WAKE_CLASS.get(wake) or (f"other:{wake}" if wake else None)


WAKE_BY_CLASS = {"user": "user", "heartbeat": "heartbeat", "event": "gate",
                 # оценщик heartbeat nanobot: ответ оракула «ничего не делать» (короткий, без вызова инструмента →
                 # nanobot по умолчанию не уведомляет) ближе к идеальной модели, чем сводка на 400 токенов
                 "internal:heartbeat_eval": "heartbeat"}
TURN_CLASSES = {"internal:heartbeat_eval"}     # внутренние, но не сводки


def content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if isinstance(p.get("text"), str):
                    parts.append(p["text"])
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return str(content)


# Служебные «подталкивания» системы внутри хода (не новый вход): класс берётся по предыдущему user.
# nanobot: после пустого финального ответа — повторный вызов без инструментов (agent/runner.py).
NUDGES = (re.compile(r"^\s*Please provide your response to the user based on the conversation above"),)


def classify(body: dict, rules: list[Rule]) -> tuple[str, str]:
    """(класс, фрагмент текста последнего user) для запроса chat/completions."""
    msgs = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
    user_text = ""
    nudged = False
    for m in reversed(msgs):
        if m.get("role") == "user":
            user_text = content_text(m.get("content"))
            if any(n.search(user_text) for n in NUDGES):
                nudged = True
                continue
            break
    if nudged:
        body = dict(body, tools=body.get("tools") or [{"nudge": True}])   # не считать «внутренним» из-за tools=[]
    system_text = "\n".join(content_text(m.get("content")) for m in msgs if m.get("role") in ("system", "developer"))
    has_tools = bool(body.get("tools"))
    for r in rules:
        if r.match(user_text, system_text, has_tools):
            return r.cls, user_text
    return "user", user_text


def sse_from_completion(resp: dict, include_usage: bool) -> bytes:
    """Ответ chat.completion → поток SSE chat.completion.chunk (как у OpenAI)."""
    base = {"id": resp.get("id"), "object": "chat.completion.chunk", "created": resp.get("created", int(time.time())),
            "model": resp.get("model"), "system_fingerprint": resp.get("system_fingerprint")}
    if resp.get("service_tier"):
        base["service_tier"] = resp["service_tier"]
    out: list[bytes] = []

    def emit(choices: list, **extra) -> None:
        obj = dict(base, choices=choices, **extra)
        out.append(b"data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n")

    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    idx = choice.get("index", 0)
    emit([{"index": idx, "delta": {"role": "assistant", "content": ""}, "logprobs": None, "finish_reason": None}])
    if msg.get("content"):
        emit([{"index": idx, "delta": {"content": msg["content"]}, "logprobs": None, "finish_reason": None}])
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        emit([{"index": idx, "delta": {"tool_calls": [{"index": i, "id": tc.get("id"), "type": tc.get("type", "function"),
                                                        "function": {"name": fn.get("name"),
                                                                     "arguments": fn.get("arguments", "")}}]},
               "logprobs": None, "finish_reason": None}])
    emit([{"index": idx, "delta": {}, "logprobs": None, "finish_reason": choice.get("finish_reason") or "stop"}])
    if include_usage and resp.get("usage") is not None:
        emit([], usage=resp["usage"])
    out.append(b"data: [DONE]\n\n")
    return b"".join(out)


TARGET_RE = re.compile(r"Target length:\s*about\s*(\d+)\s*tokens", re.I)
_TOK = None


def _tokenizer():
    """o200k из bench.mockllm.tokens (тот же счёт, что у mock-llm); без него — эвристика ~4 символа."""
    global _TOK
    if _TOK is None:
        try:
            from bench.mockllm.tokens import load_tokenizer  # noqa: PLC0415

            _TOK = load_tokenizer()
        except Exception:  # noqa: BLE001
            _TOK = False
    return _TOK or None


def fit_summary(body: dict, resp: dict) -> dict | None:
    """Укоротить ответ-сводку до цели запроса. Возвращает {before, after, target} или None."""
    m = None
    for msg in reversed(body.get("messages") or []):
        m = TARGET_RE.search(content_text((msg or {}).get("content")))
        if m:
            break
    if not m:
        return None
    target = int(m.group(1))
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    tok = _tokenizer()
    count = (lambda t: tok.count(t)) if tok else (lambda t: (len(t) + 3) // 4)
    max_chars = int(2.25 * target)
    before = count(text)
    if before <= target and len(text) <= max_chars:
        return None
    new = text[:max_chars]
    while new and count(new) > target:
        new = new[: int(len(new) * 0.9)]
    usage = resp.get("usage") or {}
    old_ct = int(usage.get("completion_tokens") or before)
    reasoning = int(((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0)
    after = count(new) + reasoning
    msg["content"] = new
    usage["completion_tokens"] = after
    usage["total_tokens"] = int(usage.get("prompt_tokens") or 0) + after
    return {"target": target, "completion_before": old_ct, "completion_after": after,
            "chars_before": len(text), "chars_after": len(new)}


class Tap:
    def __init__(self, upstream: str, rules: list[Rule], journal_path: str | None = None, hints: bool = True,
                 timeout_s: float = 300.0, fit_summary: bool = True):
        u = urlsplit(upstream.rstrip("/"))
        self.up_host, self.up_port = u.hostname or "127.0.0.1", u.port or (443 if u.scheme == "https" else 80)
        self.up_prefix = u.path or ""
        self.rules = rules
        self.hints = hints
        self.fit_summary = fit_summary
        self.timeout_s = timeout_s
        self._local = threading.local()
        self._lock = threading.Lock()
        self.seq = 0
        self.records: list[dict] = []
        self.counts: dict[str, int] = {}
        self.inflight = 0
        self.last_activity = time.time()
        self._jf = open(journal_path, "a", encoding="utf-8") if journal_path else None

    def conn(self) -> http.client.HTTPConnection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = http.client.HTTPConnection(self.up_host, self.up_port, timeout=self.timeout_s)
            self._local.conn = c
        return c

    def upstream(self, method: str, path: str, body: bytes | None, headers: dict) -> tuple[int, dict, bytes]:
        for attempt in (1, 2):
            c = self.conn()
            try:
                c.request(method, self.up_prefix + path, body=body, headers=headers)
                r = c.getresponse()
                data = r.read()
                return r.status, {k.lower(): v for k, v in r.getheaders()}, data
            except (http.client.HTTPException, OSError):
                c.close()
                self._local.conn = None
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")

    def record(self, rec: dict) -> dict:
        with self._lock:
            self.seq += 1
            rec["seq"] = self.seq
            self.records.append(rec)
            self.counts[rec["cls"]] = self.counts.get(rec["cls"], 0) + 1
            if self._jf is not None:
                self._jf.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
                self._jf.flush()
        return rec

    def state(self) -> dict:
        with self._lock:
            return {"seq": self.seq, "counts": dict(self.counts), "inflight": self.inflight,
                    "idle_ms": round((time.time() - self.last_activity) * 1000.0, 1)}

    def close(self) -> None:
        if self._jf is not None:
            self._jf.close()
            self._jf = None


def make_handler(tap: Tap):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "llmtap/1"

        def log_message(self, fmt, *args):     # тихо
            pass

        def _send(self, status: int, body: bytes, ctype: str = "application/json") -> int:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if ctype.startswith("text/event-stream"):
                self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            # оценка байт ответа «на проводе»: строка статуса + заголовки + тело
            return len(body) + 17 + len(self.server_version) + 120

        def _req_total(self, body_len: int) -> int:
            head = len(self.raw_requestline) + sum(len(k) + len(v) + 4 for k, v in self.headers.items()) + 2
            return head + body_len

        def do_GET(self):
            if self.path.startswith("/tap/state"):
                self._send(200, json.dumps(tap.state()).encode())
                return
            if self.path.startswith("/tap/journal"):
                since = 0
                m = re.search(r"since=(\d+)", self.path)
                if m:
                    since = int(m.group(1))
                with tap._lock:
                    rows = [r for r in tap.records if r["seq"] > since]
                data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
                self._send(200, data, "application/x-ndjson")
                return
            try:
                status, headers, data = tap.upstream("GET", self.path, None, {})
            except OSError as e:
                self._send(502, json.dumps({"error": {"message": f"upstream: {e}"}}).encode())
                return
            self._send(status, data, headers.get("content-type", "application/json"))

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if not self.path.rstrip("/").endswith("chat/completions"):
                status, headers, data = tap.upstream("POST", self.path, raw,
                                                     {"Content-Type": self.headers.get("Content-Type", "application/json")})
                self._send(status, data, headers.get("content-type", "application/json"))
                return
            t0 = time.time()
            with tap._lock:
                tap.inflight += 1
                tap.last_activity = t0
            try:
                self._chat(raw, t0)
            finally:
                with tap._lock:
                    tap.inflight -= 1
                    tap.last_activity = time.time()

        def _chat(self, raw: bytes, t0: float) -> None:
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._send(400, b'{"error":{"message":"invalid JSON"}}')
                return
            stream = bool(body.get("stream"))
            include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
            cls, text = classify(body, tap.rules)
            hcls = header_class(dict(self.headers.items()))
            if hcls is not None:          # агент сам назвал причину (EdgeCtx) — точнее текстовых правил
                cls = hcls
            headers = {"Content-Type": "application/json"}
            for k, v in self.headers.items():
                lk = k.lower()
                if lk == "authorization" or lk.startswith("x-"):
                    headers[k] = v
            if tap.hints:
                summary = cls.startswith("internal") and cls not in TURN_CLASSES
                headers.setdefault("X-Bench-Purpose", "summary" if summary else "turn")
                if cls in WAKE_BY_CLASS:
                    headers.setdefault("X-Bench-Wake", WAKE_BY_CLASS[cls])
            if stream:
                fwd = {k: v for k, v in body.items() if k not in ("stream", "stream_options")}
                up_body = json.dumps(fwd, ensure_ascii=False).encode("utf-8")
            else:
                up_body = raw
            try:
                status, _, data = tap.upstream("POST", "/v1/chat/completions", up_body, headers)
            except OSError as e:
                self._send(502, json.dumps({"error": {"message": f"upstream: {e}"}}).encode())
                tap.record({"ts_wall": t0, "cls": cls, "status": 502, "error": str(e), "stream": stream})
                return
            resp: dict | None = None
            adjusted = None
            if status == 200:
                try:
                    resp = json.loads(data.decode("utf-8"))
                except ValueError:
                    resp = None
                if resp is not None and tap.fit_summary and cls.startswith("internal"):
                    adjusted = fit_summary(body, resp)
                    if adjusted is not None:
                        data = json.dumps(resp, ensure_ascii=False).encode("utf-8")
            if stream and status == 200 and resp is not None:
                out = sse_from_completion(resp, include_usage)
                sent = self._send(200, out, "text/event-stream; charset=utf-8")
            else:
                out = data
                sent = self._send(status, data)
            msg = ((resp or {}).get("choices") or [{}])[0].get("message") or {}
            msgs = body.get("messages") or []
            tap.record({
                "ts_wall": t0, "ts_end": time.time(), "cls": cls, "status": status, "stream": stream,
                "model": body.get("model"), "n_messages": len(msgs), "n_tools": len(body.get("tools") or []),
                "last_role": (msgs[-1] or {}).get("role") if msgs else None,
                "user_snippet": text[:200], "mock_id": (resp or {}).get("id"),
                "usage": (resp or {}).get("usage"), "finish_reason": ((resp or {}).get("choices") or [{}])[0].get("finish_reason"),
                "tool_calls": [((tc.get("function") or {}).get("name")) for tc in (msg.get("tool_calls") or [])],
                "content": (msg.get("content") or "")[:200],
                "bytes": {"request_body": len(raw), "request_total": self._req_total(len(raw)),
                          "response_body": len(out), "response_total": sent,
                          "upstream_request_body": len(up_body), "upstream_response_body": len(data)},
                "latency_ms": round((time.time() - t0) * 1000.0, 3),
                **({"usage_adjusted": adjusted} if adjusted else {}),
            })

    return Handler


class TapServer:
    """llmtap в фоновом потоке: with TapServer(upstream, rules) as s: s.port."""

    def __init__(self, upstream: str, rules: list[Rule], host: str = "127.0.0.1", port: int = 0,
                 journal: str | None = None, hints: bool = True, fit_summary: bool = True):
        self.tap = Tap(upstream, rules, journal, hints, fit_summary=fit_summary)
        self.httpd = ThreadingHTTPServer((host, port), make_handler(self.tap))
        self.httpd.daemon_threads = True
        self.host, self.port = host, self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="llmtap", daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    def __enter__(self) -> "TapServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> "TapServer":
        return self.__enter__()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tap.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.baselines.llmtap")
    ap.add_argument("--upstream", required=True, help="mock-llm, напр. http://127.0.0.1:18120")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--journal")
    ap.add_argument("--rules", default="generic", choices=sorted(RULES))
    ap.add_argument("--no-hints", action="store_true")
    ap.add_argument("--no-fit-summary", action="store_true")
    a = ap.parse_args(argv)
    s = TapServer(a.upstream, RULES[a.rules], a.host, a.port, a.journal, not a.no_hints, not a.no_fit_summary)
    print(json.dumps({"event": "listening", "host": s.host, "port": s.port}), flush=True)
    try:
        s.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        s.httpd.server_close()
        s.tap.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
