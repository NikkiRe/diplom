"""mock-llm: OpenAI-совместимый сервер Chat Completions для стенда (спецификация §5).

Режимы:
    oracle  — ответы оракула по разметке дня (T0, 0 ₽); usage — оценка o200k + эмуляция кэша провайдера;
    record  — прокси на --upstream с записью пар «нормализованный запрос → ответ» в --recording;
    replay  — ответы из --recording по хешу нормализованного запроса (промах → ошибка или оракул).

Эндпойнты:
    POST /v1/chat/completions      (и /chat/completions)   — без потока (stream=true → 400);
    GET  /v1/models, GET /healthz;
    POST /control/expect {input_id, expect, situation?, text?, kind?, t?}  — ожидание для оракула;
    POST /control/clock {t}        — виртуальное время (для TTL кэша) от реплеера;
    POST /control/clear {input_id?}, POST /control/expire {input_id};
    POST /control/reset {journal?}  — полный сброс между прогонами: кэш, ожидания, id вызовов, ГСЧ задержки,
                                     счётчик запросов, часы; journal=true — очистить журнал в памяти (номера
                                     seq журнала не сбрасываются); ответ {ok, journal_seq};
    GET  /control/state            — счётчики, inflight, простой, journal_seq, источники часов;
    GET  /control/journal[?since=N] — журнал запросов (NDJSON), записи с seq > N.
Заголовки запроса (первый найденный; имена edgectx — agent/internal/agent/turn.go):
    X-Bench-Expect                                   JSON или base64(JSON) ожидания — вместо /control/expect;
    X-Bench-Clock | X-Edgectx-Clock-T | X-Edgectx-Clock      виртуальное время запроса, с (TTL кэша);
    X-Bench-Input-Id | X-Edgectx-Input-Ids | X-Edgectx-Input(-Id|s)  id входа(ов) через запятую;
    X-Bench-Wake | X-Edgectx-Wake-Reason | X-Edgectx-Wake    причина пробуждения (user|gate|heartbeat|rule);
    X-Bench-Purpose | X-Edgectx-Purpose              turn | summary (сводка — отдельный ответ оракула).
Все X-Bench-*/X-Edgectx-* пишутся в журнал; clock_source записи — header | control | wall, а запрос с
заголовками X-Edgectx-* без заголовка часов помечается предупреждением (счётчик в /control/state).

Журнал (--journal, JSONL, строка на запрос): токены по сегментам, usage, cached_tokens, байты запроса и
ответа (тело и с заголовками), задержка, решение оракула. По нему bench/metrics считает трафик.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .cache import PrefixCache
from .latency import LatencyModel
from .oracle import Oracle
from .recording import IGNORED_FIELDS, RecordStore
from .tokens import account_request, count_completion, load_tokenizer

VERSION = "mock-llm 1.0"
MODES = ("oracle", "record", "replay")
# Заголовки-подсказки от агента/реплеера (первый найденный). X-Edgectx-* пишет edgectx при llm.trace_headers:
# X-Edgectx-Clock-T, X-Edgectx-Input-Ids, X-Edgectx-Wake-Reason, X-Edgectx-Purpose (agent/internal/agent/turn.go);
# прежние имена (x-edgectx-clock, -input, -wake) оставлены как алиасы.
CLOCK_HEADERS = ("x-bench-clock", "x-edgectx-clock-t", "x-edgectx-clock")
INPUT_HEADERS = ("x-bench-input-id", "x-edgectx-input-ids", "x-edgectx-input", "x-edgectx-input-id",
                 "x-edgectx-inputs")
WAKE_HEADERS = ("x-bench-wake", "x-edgectx-wake-reason", "x-edgectx-wake")
PURPOSE_HEADERS = ("x-bench-purpose", "x-edgectx-purpose")


def _first_header(headers: dict, names: tuple[str, ...]) -> str | None:
    for n in names:
        if headers.get(n):
            return headers[n]
    return None


@dataclass
class ServerConfig:
    mode: str = "oracle"
    host: str = "127.0.0.1"
    port: int = 18100
    model_name: str = "mock-oracle"
    journal_path: str | None = None
    recording_path: str | None = None
    upstream: str | None = None              # базовый URL, напр. https://routerai.ru/api/v1
    upstream_key_env: str | None = None      # имя переменной окружения с ключом (в разработке не задаётся)
    upstream_timeout_s: float = 120.0
    cache_ttl_s: float = 300.0
    cache_min_tokens: int = 1024
    cache_granularity: int = 128
    latency: str = "0"
    seed: int = 0
    noop_text: str = ""
    parallel_calls: bool = True
    honor_k: bool = True                     # k вызовов LLM на вход из разметки дня (шаги чтения перед действием)
    replay_miss: str = "error"               # error | oracle
    tokenizer: str = "o200k_base"
    tool_tokens: str = "cookbook"            # cookbook | json
    calib: tuple[float, float] = (0.0, 1.0)  # prompt_tokens = a + b·оценка
    reasoning_tokens: int = 0                # скрытые reasoning-токены на вызов (gpt-5-*: ≈400 по допущению §14)
    summary_tokens: int = 400                # длина сводки оракула (summ_len, D_artifacts/cost_model.py)
    pad_profile: str = "targeted"            # добор до k: targeted | legacy (до ревью: list_devices ×N)
    clock: str = "auto"                      # auto | wall | virtual
    tls_cert: str | None = None
    tls_key: str | None = None
    hash_ignore: tuple[str, ...] = IGNORED_FIELDS
    verbose: bool = False

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if self.mode == "record" and not self.upstream:
            raise ValueError("record mode needs --upstream")
        if self.mode in ("record", "replay") and not self.recording_path:
            raise ValueError(f"{self.mode} mode needs --recording")
        if self.mode == "replay" and not os.path.exists(self.recording_path or ""):
            raise ValueError(f"recording not found: {self.recording_path}")
        if self.replay_miss not in ("error", "oracle"):
            raise ValueError("replay_miss must be 'error' or 'oracle'")
        if self.clock not in ("auto", "wall", "virtual"):
            raise ValueError("clock must be auto, wall or virtual")
        if bool(self.tls_cert) != bool(self.tls_key):
            raise ValueError("TLS needs both --tls-cert and --tls-key")
        if self.summary_tokens < 0:
            raise ValueError("summary_tokens must be >= 0")


@dataclass
class _Stats:
    requests: int = 0
    inflight: int = 0
    last_activity: float = field(default_factory=time.time)
    clock_sources: dict = field(default_factory=dict)   # header | control | wall → число запросов
    clock_header_missing: int = 0                       # запросы с X-Edgectx-* без заголовка часов


class MockLLM:
    """Состояние сервера (не зависит от HTTP — удобно тестировать напрямую)."""

    def __init__(self, cfg: ServerConfig):
        cfg.validate()
        self.cfg = cfg
        self.tok = load_tokenizer(cfg.tokenizer)
        self.cache = PrefixCache(cfg.cache_ttl_s, cfg.cache_min_tokens, cfg.cache_granularity)
        self.oracle = Oracle(noop_text=cfg.noop_text, parallel_calls=cfg.parallel_calls, honor_k=cfg.honor_k,
                             summary_tokens=cfg.summary_tokens, tokenizer=self.tok, pad_profile=cfg.pad_profile)
        self.latency = LatencyModel(cfg.latency, cfg.seed)
        self.store = RecordStore(cfg.recording_path, cfg.hash_ignore) if cfg.mode in ("record", "replay") else None
        self.virtual_t: float | None = None
        self.stats = _Stats()
        self.journal: list[dict] = []
        self.journal_seq = 0                     # монотонный номер записи журнала (не сбрасывается reset)
        self._lock = threading.Lock()
        self._journal_lock = threading.Lock()
        if cfg.journal_path:
            os.makedirs(os.path.dirname(os.path.abspath(cfg.journal_path)), exist_ok=True)

    # ---------------------------------------------------------------- время и журнал

    def now(self, header_clock: str | None) -> tuple[float, float | None, str]:
        """(время для TTL кэша, виртуальное время или None, источник: header | control | wall)."""
        if self.cfg.clock != "wall":
            if header_clock:
                try:
                    t = float(header_clock)
                    return t, t, "header"
                except ValueError:
                    pass
            if self.virtual_t is not None:
                return self.virtual_t, self.virtual_t, "control"
        return time.monotonic(), None, "wall"

    def log(self, rec: dict) -> None:
        with self._journal_lock:
            self.journal_seq += 1
            rec = {"seq": self.journal_seq, **rec}
            self.journal.append(rec)
            if self.cfg.journal_path:
                with open(self.cfg.journal_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")

    def begin(self) -> int:
        with self._lock:
            self.stats.requests += 1
            self.stats.inflight += 1
            self.stats.last_activity = time.time()
            return self.stats.requests

    def end(self) -> None:
        with self._lock:
            self.stats.inflight -= 1
            self.stats.last_activity = time.time()

    def state(self) -> dict:
        with self._lock:
            s = {"version": VERSION, "mode": self.cfg.mode, "model": self.cfg.model_name,
                 "requests": self.stats.requests, "inflight": self.stats.inflight,
                 "idle_ms": max(0.0, (time.time() - self.stats.last_activity) * 1000.0),
                 "virtual_t": self.virtual_t, "tokenizer": self.tok.name,
                 "cache": {"ttl_s": self.cache.ttl_s, "min_tokens": self.cache.min_tokens,
                           "granularity": self.cache.granularity},
                 "journal_records": len(self.journal), "journal_seq": self.journal_seq,
                 "clock_sources": dict(self.stats.clock_sources),
                 "clock_header_missing": self.stats.clock_header_missing,
                 "summary_tokens": self.cfg.summary_tokens, "pad_profile": self.cfg.pad_profile}
        s["oracle"] = self.oracle.state()
        if self.store is not None:
            s["recording"] = {"path": self.cfg.recording_path, "records": len(self.store)}
        return s

    def reset(self, journal: bool = False) -> int:
        """Сброс между прогонами: всё, от чего зависят ответы и usage (кэш, ожидания и id вызовов оракула,
        ГСЧ задержки, счётчик запросов → id ответов, часы, источники часов). journal=true — очистить журнал
        в памяти; номер seq журнала монотонен. Возвращает journal_seq на момент сброса."""
        self.cache.reset()
        self.oracle.reset()
        self.virtual_t = None
        self.latency = LatencyModel(self.cfg.latency, self.cfg.seed)
        with self._lock:
            self.stats.requests = 0
            self.stats.clock_sources = {}
            self.stats.clock_header_missing = 0
        if self.store is not None:
            self.store.rewind()
        with self._journal_lock:
            if journal:
                self.journal.clear()
            return self.journal_seq

    # ---------------------------------------------------------------- чат

    def chat(self, body: dict, headers: dict, req_seq: int) -> tuple[int, dict, dict]:
        """Ответ на /v1/chat/completions: (HTTP-статус, тело, поля для журнала)."""
        if body.get("stream"):
            return 400, _error("stream=true is not supported by mock-llm (agent must not stream)",
                               "invalid_request_error", "stream_unsupported"), {}
        if not isinstance(body.get("messages"), list) or not body["messages"]:
            return 400, _error("messages must be a non-empty list", "invalid_request_error", "bad_messages"), {}
        if self.cfg.mode == "record":
            return self._record(body, headers)
        if self.cfg.mode == "replay":
            rec = self.store.lookup(body)
            if rec is not None:
                return int(rec["status"]), rec["response"], {"replay": {"key": rec["key"], "hit": True,
                                                                        "seq": rec.get("seq")}}
            if self.cfg.replay_miss == "error":
                return 404, _error("request not found in recording", "replay_miss", "replay_miss"), \
                    {"replay": {"key": self.store.key(body), "hit": False}}
            status, resp, extra = self._oracle(body, headers, req_seq)
            extra["replay"] = {"key": self.store.key(body), "hit": False, "fallback": "oracle"}
            return status, resp, extra
        return self._oracle(body, headers, req_seq)

    def _oracle(self, body: dict, headers: dict, req_seq: int) -> tuple[int, dict, dict]:
        header_expect = None
        raw = headers.get("x-bench-expect")
        if raw:
            try:
                header_expect = _decode_header_json(raw)
            except ValueError as e:
                return 400, _error(f"bad X-Bench-Expect: {e}", "invalid_request_error", "bad_expect_header"), {}
        acc = account_request(body, self.tok, self.cfg.tool_tokens, self.cfg.calib)
        now, vt, clock_source = self.now(_first_header(headers, CLOCK_HEADERS))
        from_edgectx = any(h.startswith("x-edgectx-") for h in headers)
        with self._lock:
            self.stats.clock_sources[clock_source] = self.stats.clock_sources.get(clock_source, 0) + 1
            if from_edgectx and clock_source != "header":
                self.stats.clock_header_missing += 1
        cache = self.cache.lookup_and_store(acc.sequence, now, acc.prompt_tokens)
        try:
            decision = self.oracle.respond(body, header_expect, _first_header(headers, INPUT_HEADERS),
                                           _first_header(headers, WAKE_HEADERS),
                                           _first_header(headers, PURPOSE_HEADERS))
        except ValueError as e:
            return 400, _error(str(e), "invalid_request_error", "bad_expectation"), {}
        completion = count_completion(decision.message, self.tok) + self.cfg.reasoning_tokens
        usage = {
            "prompt_tokens": acc.prompt_tokens,
            "completion_tokens": completion,
            "total_tokens": acc.prompt_tokens + completion,
            "prompt_tokens_details": {"cached_tokens": cache.cached_tokens, "audio_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": self.cfg.reasoning_tokens, "audio_tokens": 0,
                                          "accepted_prediction_tokens": 0, "rejected_prediction_tokens": 0},
        }
        resp = {
            "id": f"chatcmpl-mock-{req_seq:06d}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model") or self.cfg.model_name,
            "choices": [{"index": 0, "message": decision.message, "finish_reason": decision.finish_reason,
                         "logprobs": None}],
            "usage": usage,
            "service_tier": "default",
            "system_fingerprint": "mock-llm-oracle",
        }
        extra = {"t_clock": vt, "clock_source": clock_source, "oracle": decision.journal(),
                 "estimate": {"prompt_tokens_raw": acc.raw_estimate, "segments": acc.segments,
                              "sequence_tokens": len(acc.sequence), "common_prefix": cache.common_prefix,
                              "tokenizer": acc.tokenizer}}
        if from_edgectx and clock_source != "header":
            extra["warning"] = ("edgectx request without X-Edgectx-Clock-T: cache TTL uses "
                                + ("/control/clock (end of replay step)" if clock_source == "control" else "wall clock"))
        return 200, resp, extra

    def _record(self, body: dict, headers: dict) -> tuple[int, dict, dict]:
        url = self.cfg.upstream.rstrip("/") + "/chat/completions"
        out_headers = {"Content-Type": "application/json", "Accept": "application/json"}
        key = os.environ.get(self.cfg.upstream_key_env) if self.cfg.upstream_key_env else None
        if key:
            out_headers["Authorization"] = f"Bearer {key}"
        elif headers.get("authorization"):
            out_headers["Authorization"] = headers["authorization"]
        for h, v in headers.items():   # подсказки агента пробрасываются (цепочка mock → mock в тестах)
            if h.startswith(("x-bench-", "x-edgectx-")):
                out_headers[h] = v
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(url, data=data, headers=out_headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.cfg.upstream_timeout_s) as r:  # noqa: S310
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return 502, _error(f"upstream unavailable: {e}", "upstream_error", "upstream_unavailable"), {}
        up_ms = (time.perf_counter() - t0) * 1000.0
        try:
            resp = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            resp = {"raw": raw.decode("utf-8", "replace")}
        rec = self.store.add(body, status, resp, time.time(), up_ms)
        acc = account_request(body, self.tok, self.cfg.tool_tokens, self.cfg.calib)
        extra = {"record": {"key": rec["key"], "upstream_status": status, "upstream_ms": up_ms},
                 "estimate": {"prompt_tokens": acc.prompt_tokens, "prompt_tokens_raw": acc.raw_estimate,
                              "segments": acc.segments, "tokenizer": acc.tokenizer}}
        return status, resp, extra


def _error(message: str, typ: str, code: str) -> dict:
    return {"error": {"message": message, "type": typ, "code": code}}


def _decode_header_json(raw: str) -> dict:
    raw = raw.strip()
    try:
        obj = json.loads(raw)
    except ValueError:
        try:
            obj = json.loads(base64.b64decode(raw, validate=True).decode("utf-8"))
        except (ValueError, binascii.Error, UnicodeDecodeError) as e:
            raise ValueError("neither JSON nor base64(JSON)") from e
    if not isinstance(obj, dict):
        raise ValueError("must be a JSON object")
    return obj


def _summarize_response(resp: dict) -> dict:
    out: dict = {}
    try:
        choice = resp["choices"][0]
        msg = choice.get("message") or {}
        calls = msg.get("tool_calls") or []
        out["finish_reason"] = choice.get("finish_reason")
        out["n_tool_calls"] = len(calls)
        out["tool_names"] = [((c.get("function") or {}).get("name")) for c in calls]
    except (KeyError, IndexError, TypeError, AttributeError):
        pass
    usage = resp.get("usage") if isinstance(resp, dict) else None
    if isinstance(usage, dict):
        details = usage.get("prompt_tokens_details") or {}
        cdet = usage.get("completion_tokens_details") or {}
        out["usage"] = {"prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                        "cached_tokens": details.get("cached_tokens", 0),
                        "reasoning_tokens": cdet.get("reasoning_tokens", 0)}
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive: LLM-клиент агента держит одно соединение
    server_version = "mock-llm/1.0"
    app: MockLLM  # задаётся в make_server

    # ------------------------------------------------------------ транспорт

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401 — тихий режим по умолчанию
        if self.app.cfg.verbose:
            sys.stderr.write("mock-llm: " + (fmt % args) + "\n")

    def _request_header_bytes(self) -> int:
        n = len(self.raw_requestline or b"")
        for k, v in self.headers.items():
            n += len(k.encode("latin-1", "replace")) + len(str(v).encode("latin-1", "replace")) + 4
        return n + 2

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _send(self, status: int, payload, content_type: str = "application/json") -> int:
        body = payload if isinstance(payload, bytes) else \
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = [("Content-Type", f"{content_type}; charset=utf-8"), ("Content-Length", str(len(body)))]
        self.send_response(status)
        hdr_bytes = len(f"HTTP/1.1 {status} {self.responses.get(status, ('',))[0]}\r\n".encode())
        for k, v in headers:
            self.send_header(k, v)
            hdr_bytes += len(k) + len(v) + 4
        # BaseHTTPRequestHandler сам добавляет Server и Date
        hdr_bytes += len(f"Server: {self.version_string()}\r\n") + len(f"Date: {self.date_time_string()}\r\n") + 2
        self.end_headers()
        self.wfile.write(body)
        return hdr_bytes + len(body)

    def _json_body(self, raw: bytes) -> dict | None:
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return obj if isinstance(obj, dict) else None

    # ------------------------------------------------------------ маршрутизация

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path in ("/healthz", "/health"):
            self._send(200, {"ok": True, "version": VERSION, "mode": self.app.cfg.mode})
        elif url.path in ("/v1/models", "/models"):
            self._send(200, {"object": "list", "data": [{"id": self.app.cfg.model_name, "object": "model",
                                                         "owned_by": "mock-llm"}]})
        elif url.path == "/control/state":
            self._send(200, self.app.state())
        elif url.path == "/control/journal":
            since = int((parse_qs(url.query).get("since") or ["0"])[0])
            with self.app._journal_lock:
                lines = [json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in self.app.journal
                         if r["seq"] > since]
            self._send(200, ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"), "application/x-ndjson")
        else:
            self._send(404, _error(f"no route GET {url.path}", "not_found", "not_found"))

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        raw = self._read_body()
        body = self._json_body(raw)
        if body is None:
            self._send(400, _error("body must be a JSON object", "invalid_request_error", "bad_json"))
            return
        if url.path in ("/v1/chat/completions", "/chat/completions"):
            self._chat(body, raw)
        elif url.path.startswith("/control/"):
            self._control(url.path[len("/control/"):], body)
        else:
            self._send(404, _error(f"no route POST {url.path}", "not_found", "not_found"))

    def _control(self, action: str, body: dict) -> None:
        app = self.app
        try:
            if action == "expect":
                exp = app.oracle.register(body)
                result = {"ok": True, "input_id": exp.input_id, "kind": exp.kind, "n_steps": len(exp.steps)}
            elif action == "clock":
                t = float(body["t"])
                app.virtual_t = t
                result = {"ok": True, "t": t}
            elif action == "clear":
                result = {"ok": True, "cleared": app.oracle.clear(body.get("input_id"))}
            elif action == "expire":
                result = {"ok": True, "expired": app.oracle.expire(str(body["input_id"]))}
            elif action == "reset":
                result = {"ok": True, "journal_seq": app.reset(journal=bool(body.get("journal", False)))}
            else:
                self._send(404, _error(f"unknown control action {action!r}", "not_found", "not_found"))
                return
        except (KeyError, TypeError, ValueError) as e:
            self._send(400, _error(f"bad control request: {e}", "invalid_request_error", "bad_control"))
            return
        self._send(200, result)
        app.log({"type": "control", "ts_wall": time.time(), "action": action,
                 "input_id": body.get("input_id"), "t": body.get("t")})

    def _chat(self, body: dict, raw: bytes) -> None:
        app = self.app
        seq = app.begin()
        t0 = time.perf_counter()
        try:
            headers = {k.lower(): v for k, v in self.headers.items()}
            try:
                status, resp, extra = app.chat(body, headers, seq)
            except Exception as e:  # noqa: BLE001 — ответить 500, а не уронить соединение агента
                status, resp, extra = 500, _error(f"mock-llm internal error: {e!r}", "server_error", "internal"), {}
            injected = app.latency.sample_ms()
            if injected > 0:
                time.sleep(injected / 1000.0)
            sent = self._send(status, resp)
            total_ms = (time.perf_counter() - t0) * 1000.0
            resp_body = len(json.dumps(resp, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            rec = {"type": "llm", "req_id": f"mock-{seq:06d}", "ts_wall": time.time(), "mode": app.cfg.mode,
                   "status": status, "model": body.get("model"), "n_messages": len(body.get("messages") or []),
                   "n_tools": len(body.get("tools") or []),
                   "bytes": {"request_body": len(raw), "request_total": len(raw) + self._request_header_bytes(),
                             "response_body": resp_body, "response_total": sent},
                   "latency_ms": {"total": total_ms, "injected": injected}}
            hints = {k: v for k, v in headers.items() if k.startswith(("x-bench-", "x-edgectx-"))}
            if hints:      # X-Edgectx-Req-Id и т.п. — связь записи журнала с llm_request трассы
                rec["headers"] = hints
            rec.update(_summarize_response(resp) if isinstance(resp, dict) else {})
            rec.update(extra)
            app.log(rec)
        finally:
            app.end()


def make_server(cfg: ServerConfig) -> tuple[ThreadingHTTPServer, MockLLM]:
    app = MockLLM(cfg)
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer((cfg.host, cfg.port), handler)
    srv.daemon_threads = True
    if cfg.tls_cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cfg.tls_cert, cfg.tls_key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    return srv, app


class RunningServer:
    """Сервер в фоновом потоке — для тестов и встраивания: with RunningServer(cfg) as s: s.url"""

    def __init__(self, cfg: ServerConfig):
        self.srv, self.app = make_server(cfg)
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def port(self) -> int:
        return self.srv.server_address[1]

    @property
    def url(self) -> str:
        scheme = "https" if self.app.cfg.tls_cert else "http"
        return f"{scheme}://{self.app.cfg.host}:{self.port}"

    def __enter__(self) -> "RunningServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)
