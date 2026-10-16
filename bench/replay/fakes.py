"""Тестовые двойники edgectx и sim-mcp для проверки реплеера, mock-llm и метрик без Go-агента.

FakeAgent — крошечный агент с HTTP-API edgectx (§3): /v1/user, /v1/event, /v1/clock/advance, /v1/metrics,
/v1/shutdown. Политика: heartbeat с периодом heartbeat_s (виртуальные часы) и гейт «будить на истинное
значение устройства из gate_devices»; полная история диалога; generic-каталог (4 инструмента) +
answer/refuse/notify. Трасса — формат edgectx (agent/README): wake_id в wake/llm_request/tool_call/notify,
input_ids, purpose, segments (символы) и segments_tokens_est; заголовки X-Edgectx-* как в turn.go.
Это НЕ модель edgectx: числа с ним — не результаты.

FakeSim — двойник sim-mcp: POST /control/clock {t} публикует события датчиков дня с t ≤ to_t, отправляя их
в агент как POST /v1/event (вместо MCP notifications/resources/updated).

CLI (смоук-стенд):
    python -m bench.replay.fakes agent --llm http://127.0.0.1:18100 --trace /tmp/trace.jsonl --port 18080
    python -m bench.replay.fakes sim --day day.jsonl --agent http://127.0.0.1:18080 --port 18090
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .dayfile import load_day
from .jsonhttp import JsonClient

GENERIC_TOOLS = [
    {"type": "function", "function": {
        "name": "list_devices", "description": "Список устройств и их свойств.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "read_property", "description": "Прочитать свойство устройства.",
        "parameters": {"type": "object", "properties": {
            "device": {"type": "string", "description": "id устройства"},
            "property": {"type": "string", "description": "имя свойства"}}, "required": ["device", "property"]}}},
    {"type": "function", "function": {
        "name": "write_property", "description": "Записать свойство устройства (через шлюз приводов).",
        "parameters": {"type": "object", "properties": {
            "device": {"type": "string", "description": "id устройства"},
            "property": {"type": "string", "description": "имя свойства"},
            "value": {"description": "новое значение"}}, "required": ["device", "property", "value"]}}},
    {"type": "function", "function": {
        "name": "invoke_action", "description": "Вызвать действие устройства.",
        "parameters": {"type": "object", "properties": {
            "device": {"type": "string", "description": "id устройства"},
            "action": {"type": "string", "description": "имя действия"},
            "args": {"type": "object", "description": "аргументы"}}, "required": ["device", "action"]}}},
    {"type": "function", "function": {
        "name": "answer", "description": "Ответ на вопрос пользователя.",
        "parameters": {"type": "object", "properties": {
            "value": {"description": "значение ответа"},
            "unit": {"type": "string", "description": "единица"}}, "required": ["value"]}}},
    {"type": "function", "function": {
        "name": "refuse", "description": "Отказ от невыполнимой команды.",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string", "description": "код причины"}}, "required": ["reason"]}}},
    {"type": "function", "function": {
        "name": "notify", "description": "Уведомить пользователя.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "текст уведомления"},
            "situation_id": {"type": "string", "description": "id ситуации, если известен"}}, "required": ["text"]}}},
]
ACTUATOR_TOOLS = {"write_property", "invoke_action"}
SYSTEM_PROMPT = ("Ты агент одноплатного компьютера в лаборатории. Отвечай кратко. Используй инструменты устройств. "
                 "Безопасность приводов проверяет шлюз. ") * 40   # ≈1,3 тыс. токенов стабильного префикса
SIT_TAG = re.compile(r"\[sit:([^\]]+)\]")


class FakeAgent:
    def __init__(self, llm_url: str, trace_path: str, model: str = "gpt-4o-mini", heartbeat_s: float | None = 1800.0,
                 gate_devices: tuple[str, ...] = ("door1", "leak1", "temp_alarm"), max_steps: int = 5,
                 run_id: str = "fake-run", tools: list | None = None):
        self.llm = JsonClient(llm_url, 60.0, "llm")
        self.model = model
        self.heartbeat_s = heartbeat_s
        self.gate_devices = set(gate_devices)
        self.max_steps = max_steps
        self.run_id = run_id
        self.tools = tools or GENERIC_TOOLS
        self.t: float | None = None
        self.next_hb: float | None = None
        self.state: dict[str, dict] = {}
        self.history: list[dict] = []
        self.counters = {"llm_calls": 0, "wakes": 0, "user_turns": 0, "notifies": 0}
        self.inputs: dict[str, int] = {}
        self._lock = threading.RLock()
        self._req = 0
        self._call = 0
        self._wake = 0
        self._wake_id: str | None = None
        self._f = open(trace_path, "w", encoding="utf-8")
        self.trace("run_start", config_hash="fake", policy={"wake": "hybrid", "heartbeat_s": heartbeat_s,
                                                            "history": "full", "catalog": "generic"})

    # ---------------------------------------------------------------- трасса

    def trace(self, typ: str, **fields) -> None:
        rec = {"ts_wall": time.time(), "ts_clock": self.t, "type": typ, "run_id": self.run_id, **fields}
        self._f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._f.flush()

    def close(self) -> None:
        with self._lock:
            if not self._f.closed:
                self.trace("run_end")
                self._f.close()
            self.llm.close()

    # ---------------------------------------------------------------- API edgectx

    def _input(self, kind: str, input_id: str) -> None:
        self.inputs[kind] = self.inputs.get(kind, 0) + 1
        self.trace("input", kind=kind, id=input_id)

    def user(self, input_id: str, text: str) -> str:
        with self._lock:
            self.counters["user_turns"] += 1
            self._input("user", input_id)
            return self._turn("user", input_id, text)

    def event(self, ev: dict) -> None:
        with self._lock:
            dev, val = ev.get("device"), ev.get("value")
            self.state.setdefault(str(dev), {})[str(ev.get("property"))] = val
            iid = f"{dev}.{ev.get('property')}@{ev.get('t')}"
            self._input("sensor", iid)
            if dev in self.gate_devices and val:
                self._turn("gate", iid, f"[событие] {dev}.{ev.get('property')} = {val}")

    def advance(self, to_t: float) -> dict:
        with self._lock:
            fired = 0
            if self.t is None:
                self.t = to_t
                if self.heartbeat_s:
                    self.next_hb = to_t + self.heartbeat_s
            else:
                while self.next_hb is not None and self.next_hb <= to_t:
                    self.t = self.next_hb
                    hb = f"hb@{self.t:g}"
                    self._input("tick", hb)
                    self._turn("heartbeat", hb, "[heartbeat] проверь состояние помещения")
                    self.next_hb += self.heartbeat_s
                    fired += 1
                self.t = max(self.t, to_t)
            return {"now_t": self.t, "timers_fired": fired, "inputs": dict(self.inputs), "idle": True}

    def metrics(self) -> dict:
        with self._lock:
            return {"t": self.t, **self.counters, "inputs_total": dict(self.inputs)}

    # ---------------------------------------------------------------- цикл

    def _turn(self, reason: str, input_id: str | None, text: str) -> str:
        t_start = time.perf_counter()
        self.counters["wakes"] += 1
        self._wake += 1
        wake_id = self._wake_id = f"w{self._wake}"
        self.trace("wake", wake_id=wake_id, reason=reason, suppressed=False,
                   input_ids=[input_id] if input_id else [])
        system = {"role": "system", "content": SYSTEM_PROMPT}
        turn = [{"role": "user", "content": text}]
        final = ""
        for step in range(self.max_steps):
            messages = [system, *self.history, *turn]
            self._req += 1
            req_id = f"r{self._req}"
            chars = {"prefix": len(SYSTEM_PROMPT), "profile": 0,
                     "history": sum(len(json.dumps(m, ensure_ascii=False)) for m in self.history),
                     "state": 0, "turn": sum(len(json.dumps(m, ensure_ascii=False)) for m in turn)}
            seg = {k: v // 4 for k, v in chars.items()}     # как в edgectx: segments — символы, *_tokens_est — оценка
            self.trace("llm_request", req_id=req_id, wake_id=wake_id, step=step, purpose="turn", model=self.model,
                       prompt_tokens_est=sum(seg.values()), segments=chars, segments_tokens_est=seg,
                       n_tools=len(self.tools))
            headers = {"X-Edgectx-Run-Id": self.run_id, "X-Edgectx-Req-Id": req_id, "X-Edgectx-Wake-Id": wake_id,
                       "X-Edgectx-Wake-Reason": reason, "X-Edgectx-Input-Ids": input_id or "",
                       "X-Edgectx-Purpose": "turn", "X-Edgectx-Step": str(step),
                       "X-Edgectx-Clock-T": f"{self.t:g}"}
            status, resp, _, ms = self.llm.request("POST", "/v1/chat/completions",
                                                   {"model": self.model, "messages": messages, "tools": self.tools},
                                                   headers=headers)
            self.counters["llm_calls"] += 1
            if status != 200 or not isinstance(resp, dict):
                self.trace("llm_response", req_id=req_id, latency_ms=ms, usage={}, finish_reason="error",
                           n_tool_calls=0)
                break
            usage = resp.get("usage") or {}
            choice = resp["choices"][0]
            msg = choice["message"]
            calls = msg.get("tool_calls") or []
            self.trace("llm_response", req_id=req_id, latency_ms=ms,
                       usage={"prompt_tokens": usage.get("prompt_tokens"),
                              "completion_tokens": usage.get("completion_tokens"),
                              "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)},
                       finish_reason=choice.get("finish_reason"), n_tool_calls=len(calls))
            turn.append({k: v for k, v in msg.items() if v is not None})
            if not calls:
                final = msg.get("content") or ""
                break
            for c in calls:
                turn.append(self._execute(c, input_id, wake_id))
        if reason == "user":
            self.trace("user_reply", input_id=input_id, wake_id=wake_id, text=final,
                       latency_ms=(time.perf_counter() - t_start) * 1000.0)
            self.history.extend(turn)
        elif final:
            m = SIT_TAG.search(final)
            self.counters["notifies"] += 1
            self.trace("notify", situation_id=m.group(1) if m else None, text=final, source="llm_reply",
                       wake_id=wake_id)
        return final

    def _execute(self, call: dict, input_id: str | None, wake_id: str | None = None) -> dict:
        self._call += 1
        fn = call.get("function") or {}
        name = fn.get("name", "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        server = "hw" if name in ACTUATOR_TOOLS or name in ("read_property", "list_devices") else "sim"
        self.trace("tool_call", call_id=call.get("id"), server=server, name=name, args=args, wake_id=wake_id,
                   by="llm")
        t0 = time.perf_counter()
        ok, denied, result = True, False, {"ok": True}
        if name == "write_property":
            self.state.setdefault(str(args.get("device")), {})[str(args.get("property"))] = args.get("value")
        elif name == "read_property":
            result = {"value": self.state.get(str(args.get("device")), {}).get(str(args.get("property")))}
        elif name == "list_devices":
            result = {"devices": sorted(self.state)}
        elif name == "notify":
            self.counters["notifies"] += 1
            self.trace("notify", situation_id=args.get("situation_id"), text=args.get("text", ""),
                       source="llm_tool", wake_id=wake_id)
        elif name not in ("answer", "refuse", "invoke_action"):
            ok, result = False, {"error": f"unknown tool {name}"}
        self.trace("tool_result", call_id=call.get("id"), ok=ok, denied=denied, ms=(time.perf_counter() - t0) * 1000)
        return {"role": "tool", "tool_call_id": call.get("id"), "content": json.dumps(result, ensure_ascii=False)}


class FakeSim:
    """Публикует события датчиков дня в агент по мере продвижения часов."""

    def __init__(self, day_path: str, agent_url: str | None):
        self.events = [e.raw for e in load_day(day_path).by_kind("sensor")]
        self.agent = JsonClient(agent_url, 60.0, "agent") if agent_url else None
        self.t: float | None = None
        self._i = 0
        self.published = 0
        self._lock = threading.Lock()

    def clock(self, t: float) -> int:
        with self._lock:
            self.t = t
            n = 0
            while self._i < len(self.events) and float(self.events[self._i]["t"]) <= t:
                if self.agent is not None:
                    self.agent.post("/v1/event", self.events[self._i])
                self._i += 1
                n += 1
            self.published += n
            return n


def _handler_for(routes: dict):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a) -> None:
            pass

        def _send(self, status: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method: str) -> None:
            # тело читается до маршрутизации: иначе на keep-alive соединении остаток тела станет «запросом»
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            fn = routes.get((method, self.path.split("?", 1)[0]))
            if fn is None:
                self._send(404, {"error": f"no route {method} {self.path}"})
                return
            body = json.loads(raw or b"{}")
            try:
                self._send(200, fn(body))
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": repr(e)})

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

    return H


class FakeServer:
    """HTTP-обвязка двойника в фоновом потоке: with FakeServer.agent(...) as s: s.url"""

    def __init__(self, routes: dict, port: int = 0, host: str = "127.0.0.1", on_close=None):
        self.srv = ThreadingHTTPServer((host, port), _handler_for(routes))
        self.srv.daemon_threads = True
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._on_close = on_close

    @classmethod
    def agent(cls, agent: FakeAgent, port: int = 0) -> "FakeServer":
        routes = {
            ("POST", "/v1/user"): lambda b: {"reply": agent.user(str(b.get("id")), str(b.get("text", "")))},
            ("POST", "/v1/event"): lambda b: (agent.event(b), {"ok": True})[1],
            ("POST", "/v1/clock/advance"): lambda b: agent.advance(float(b["to_t"])),
            ("GET", "/v1/metrics"): lambda b: agent.metrics(),
            ("POST", "/v1/shutdown"): lambda b: (agent.close(), {"ok": True})[1],
        }
        s = cls(routes, port, on_close=agent.close)
        s.obj = agent
        return s

    @classmethod
    def sim(cls, sim: FakeSim, port: int = 0) -> "FakeServer":
        routes = {("POST", "/control/clock"): lambda b: {"t": float(b["t"]), "published": sim.clock(float(b["t"]))},
                  ("POST", "/control/input"): lambda b: {"ok": True, "id": b.get("id")},
                  ("POST", "/control/finish"): lambda b: {"summary": {"published": sim.published}, "inputs": []}}
        s = cls(routes, port)
        s.obj = sim
        return s

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.srv.server_address[1]}"

    def __enter__(self) -> "FakeServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)
        if self._on_close:
            self._on_close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m bench.replay.fakes", description="двойники edgectx и sim-mcp")
    sub = p.add_subparsers(dest="what", required=True)
    a = sub.add_parser("agent")
    a.add_argument("--llm", required=True)
    a.add_argument("--trace", required=True)
    a.add_argument("--port", type=int, default=18080)
    a.add_argument("--heartbeat-s", type=float, default=1800.0)
    s = sub.add_parser("sim")
    s.add_argument("--day", required=True)
    s.add_argument("--agent", required=True)
    s.add_argument("--port", type=int, default=18090)
    args = p.parse_args(argv)
    server = (FakeServer.agent(FakeAgent(args.llm, args.trace, heartbeat_s=args.heartbeat_s), args.port)
              if args.what == "agent" else FakeServer.sim(FakeSim(args.day, args.agent), args.port))
    print(json.dumps({"event": "listening", "what": args.what, "url": server.url}), flush=True)
    with server:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
