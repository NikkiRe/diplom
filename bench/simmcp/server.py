"""MCP-сервер sim-mcp (низкоуровневый Server из mcp SDK) + HTTP-управление рядом.

Одно приложение Starlette на одном порту:
  /mcp                MCP streamable HTTP (ревизия 2025-11-25, stateful-сессии, GET-поток для уведомлений);
  POST /control/clock {"t": …} | {"to_t": …}  продвинуть часы; события дня с t ≤ новых часов применяются,
                      подписчикам уходят notifications/resources/updated;
  GET  /control/clock  текущие часы;
  POST /control/input {"id": "u3"}  явно указать текущий вход пользователя (для привязки вызовов);
  GET  /control/state  истинное состояние; GET /control/report — оценка; POST /control/finish — закрыть
                      окна, записать отчёт в журнал (и в --report);
  GET  /healthz.

Ресурсы — как у hw-mcp: <scheme>://inventory (AgentView) и <scheme>://state/<device> (DeviceState);
по флагу extra_resources ещё <scheme>://state (всё) и <scheme>://events (последние изменения) — у hw-mcp их нет.

Уведомления. Подписки хранятся по mcp-session-id отдельно от признака живости сессии; рассылка не
блокирует часы: у каждой сессии своя очередь URI с объединением повторов (размер очереди не больше числа
ресурсов), её разбирает отдельная задача. /control/clock ждёт доставки не дольше notify_wait_s и только
тех сессий, которые успевают; медленная или замороженная сессия отстаёт, но подписки не теряет и получает
объединённые уведомления, когда снова читает поток. Сессиям без открытого GET-потока ничего не
отправляется (SDK всё равно отбросил бы сообщение); заброшенные сессии закрывает SDK по
session_idle_timeout (нет запросов и нет GET-потока дольше N с).
"""
from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.session import ServerSession
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from . import catalog as catalog_mod
from .evaluator import Evaluator
from .world import RECENT_MAX, Change, ClockError, World, fail, is_error, reject

try:   # ключ GET-потока в транспорте SDK (закрытый API; без него считаем, что поток есть)
    from mcp.server.streamable_http import GET_STREAM_KEY
except ImportError:  # pragma: no cover
    GET_STREAM_KEY = None

log = logging.getLogger("simmcp")
VERSION = "1.1"
NOTIFY_WAIT_S = 1.0             # сколько /control/clock ждёт доставки уведомлений успевающим сессиям
SESSION_IDLE_TIMEOUT_S = 120.0  # SDK закрывает сессию без запросов и без GET-потока дольше этого
SESSION_HEADER = "mcp-session-id"
META_CMD_ID = "edgectx/cmd_id"


def dumps(obj: Any) -> str:
    """Каноническая сериализация (как в daygen): ключи по порядку, без пробелов, UTF-8 без \\u."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Journal:
    """Журнал JSONL: конверт как у трассы edgectx (ts_wall, ts_clock, type, run_id) + поля события."""

    def __init__(self, path: str | Path | None, run_id: str):
        self.run_id = run_id
        self._f = None
        if path:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._f = open(p, "a", encoding="utf-8", newline="\n")

    def write(self, type_: str, ts_clock: float, **fields: Any) -> None:
        if self._f is None:
            return
        rec = {"ts_wall": round(time.time(), 6), "ts_clock": ts_clock, "type": type_, "run_id": self.run_id,
               **fields}
        self._f.write(dumps(rec) + "\n")
        self._f.flush()

    def close(self) -> None:
        if self._f:
            self._f.close()
            self._f = None


class _SubscribableServer(Server):
    """Низкоуровневый Server объявляет resources.subscribe=false всегда; у нас подписки есть."""

    def get_capabilities(self, notification_options: NotificationOptions,
                         experimental_capabilities: dict[str, dict[str, Any]]) -> types.ServerCapabilities:
        caps = super().get_capabilities(notification_options, experimental_capabilities)
        if caps.resources is not None:
            caps.resources.subscribe = True
        return caps


class _Peer:
    """MCP-сессия клиента: подписки, очередь уведомлений (URI → изменённые свойства), счётчики."""

    def __init__(self, key: str, sid: str | None, session: ServerSession):
        self.key, self.sid, self.session = key, sid, session
        self.subs: set[str] = set()
        self.pending: dict[str, set[str]] = {}
        self.running = False
        self.closed = False
        self.idle = anyio.Event()
        self.idle.set()
        self.sent = self.coalesced = self.failed = self.stalls = 0
        self.last_seen = time.monotonic()


class SimMCP:
    """Эмулятор помещения: мир + оценщик + MCP-сервер + журнал."""

    def __init__(self, header: dict, events: list[dict], catalog: str = "generic", scheme: str = "hw",
                 journal: Journal | None = None, notify_all: bool = False, start_t: float | None = None,
                 report_path: str | Path | None = None, structured: bool = False, rich_labels: bool = False,
                 extra_resources: bool = False, eval_tools: bool = True, inline_state: bool = False,
                 notify_wait_s: float = NOTIFY_WAIT_S):
        self.header = header
        self.world = World(header, events, start_t)
        self.evaluator = Evaluator(self.world, events)
        self.catalog = catalog
        self.tools, self.routes = catalog_mod.build(catalog, header["devices"], eval_tools)
        # валидаторы строятся один раз: jsonschema.validate на каждый вызов заново проверял схему (≈ 95 % времени)
        self._validators = {t["name"]: Draft202012Validator(t["inputSchema"]) for t in self.tools}
        self.scheme = scheme
        self.journal = journal or Journal(None, "none")
        self.notify_all = notify_all
        self.structured = structured     # structuredContent в результатах (у hw-mcp только текст)
        self.rich_labels = rich_labels   # подписи list_devices с диапазоном и enum (у hw-mcp их нет)
        self.extra_resources = extra_resources
        self.inline_state = inline_state  # новые значения в _meta.state уведомления (агенту не нужно читать ресурс)
        self.notify_wait_s = notify_wait_s
        self.report_path = Path(report_path) if report_path else None
        self.peers: dict[str, _Peer] = {}
        # счётчики трафика MCP (для сверки с трассой агента) и рассылки
        self.stats = {"tool_calls": 0, "resource_reads": 0, "resource_read_bytes": 0, "notify_queued": 0,
                      "notify_sent": 0, "notify_coalesced": 0, "notify_failed": 0, "notify_stalls": 0,
                      "sessions_closed": 0}
        self._tg: anyio.abc.TaskGroup | None = None
        self._mgr: StreamableHTTPSessionManager | None = None
        self._call_seq = 0
        self._finished = False
        self.server = self._make_server()
        # события, наступившие к стартовому моменту часов (обычно таких нет)
        self._apply_inputs(self.world.advance(self.world.clock)[1])

    # ------------------------------------------------------------------ URI
    def uri(self, *parts: str) -> str:
        return f"{self.scheme}://" + "/".join(parts)

    def resource_list(self) -> list[types.Resource]:
        res = [types.Resource(uri=AnyUrl(self.uri("inventory")), name="inventory", mimeType="application/json",
                              description="Device model for the agent: properties, units, actions, locations")]
        if self.extra_resources:
            res += [
                types.Resource(uri=AnyUrl(self.uri("state")), name="state", mimeType="application/json",
                               description="Current values of all device properties"),
                types.Resource(uri=AnyUrl(self.uri("events")), name="events", mimeType="application/json",
                               description=f"Latest state changes since the last clock advance "
                                           f"(up to {RECENT_MAX})"),
            ]
        for dev in sorted(self.world.devices):
            res.append(types.Resource(uri=AnyUrl(self.uri("state", dev)), name=f"state-{dev}",
                                      mimeType="application/json",
                                      description=f"Current values of {dev} with their age; subscribe for updates"))
        return res

    def read_uri(self, uri: str) -> dict:
        prefix = f"{self.scheme}://"
        if not uri.startswith(prefix):
            raise ValueError(f"unknown resource {uri}")
        path = uri[len(prefix):]
        if path == "inventory":
            return self.world.inventory()
        if path.startswith("state/") and path[6:] in self.world.devices:
            return self.world.device_state(path[6:])
        if self.extra_resources and path == "state":
            return {"now": self.world.clock, "state": self.world.snapshot()}
        if self.extra_resources and path == "events":
            return {"now": self.world.clock, "events": self.world.recent}
        raise ValueError(f"unknown resource {uri}")

    # ------------------------------------------------------------------ сессии
    def _track(self) -> _Peer | None:
        """Запомнить сессию текущего запроса (по mcp-session-id). Сессия попадает в рассылку после первого своего
        запроса (initialize обрабатывает SDK без наших обработчиков; клиенты затем вызывают list_tools/subscribe)."""
        try:
            ctx = self.server.request_context
        except LookupError:
            return None
        sess = ctx.session
        req = ctx.request
        sid = req.headers.get(SESSION_HEADER) if isinstance(req, Request) else None
        key = sid or f"obj-{id(sess)}"
        peer = self.peers.get(key)
        if peer is None or peer.closed:
            peer = self.peers[key] = _Peer(key, sid, sess)
        elif peer.session is not sess:
            peer.session = sess
        peer.last_seen = time.monotonic()
        if peer.pending and not peer.running:
            self._kick(peer)        # клиент ожил — дослать накопленное
        return peer

    def _transport(self, peer: _Peer) -> Any:
        inst = getattr(self._mgr, "_server_instances", None) if self._mgr is not None else None
        if inst is None or peer.sid is None:
            return None
        return inst.get(peer.sid, False)   # False — сессии в менеджере больше нет

    def _alive(self, peer: _Peer) -> bool:
        if peer.closed:
            return False
        tr = self._transport(peer)
        return tr is not False and not getattr(tr, "is_terminated", False)

    def _has_stream(self, peer: _Peer) -> bool:
        tr = self._transport(peer)
        if tr is None or GET_STREAM_KEY is None:
            return True                     # не можем проверить — считаем, что поток есть
        if tr is False:
            return False
        streams = getattr(tr, "_request_streams", None)
        return True if streams is None else GET_STREAM_KEY in streams

    def _prune(self) -> None:
        """Убрать сессии, закрытые клиентом (DELETE), по простою SDK или из-за ошибки транспорта."""
        for key, peer in list(self.peers.items()):
            if not self._alive(peer):
                peer.closed = True
                peer.pending.clear()
                peer.idle.set()
                del self.peers[key]
                self.stats["sessions_closed"] += 1

    def session_stats(self) -> dict:
        peers = list(self.peers.values())
        out = {"sessions": len(peers), "subscribed": sum(1 for p in peers if p.subs),
               "streaming": sum(1 for p in peers if self._has_stream(p)),
               "pending": sum(len(p.pending) for p in peers), **self.stats}
        inst = getattr(self._mgr, "_server_instances", None) if self._mgr is not None else None
        if inst is not None:
            out["sdk_sessions"] = len(inst)
        return out

    # ------------------------------------------------------------------ MCP
    def _make_server(self) -> Server:
        srv = _SubscribableServer("sim-mcp", version=VERSION,
                                  instructions="Room emulator for EdgeCtx benchmarks: devices, state, answer/refuse.")

        @srv.list_tools()
        async def _list_tools() -> list[types.Tool]:
            self._track()
            return [types.Tool(**t) for t in self.tools]

        @srv.call_tool(validate_input=False)
        async def _call_tool(name: str, arguments: dict) -> types.CallToolResult:
            self._track()
            return await self.call(name, arguments or {}, self._request_meta())

        @srv.list_resources()
        async def _list_resources() -> list[types.Resource]:
            self._track()
            return self.resource_list()

        @srv.read_resource()
        async def _read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
            self._track()
            text = dumps(self.read_uri(str(uri)))
            self.stats["resource_reads"] += 1
            self.stats["resource_read_bytes"] += len(text.encode())
            return [ReadResourceContents(content=text, mime_type="application/json")]

        @srv.subscribe_resource()
        async def _subscribe(uri: AnyUrl) -> None:
            peer = self._track()
            self.read_uri(str(uri))   # проверка, что ресурс существует
            if peer is not None:
                peer.subs.add(str(uri))
            self.journal.write("subscribe", self.world.clock, uri=str(uri), session=peer.key if peer else None)

        @srv.unsubscribe_resource()
        async def _unsubscribe(uri: AnyUrl) -> None:
            peer = self._track()
            if peer is not None:
                peer.subs.discard(str(uri))
                peer.pending.pop(str(uri), None)

        return srv

    def _request_meta(self) -> dict:
        try:
            meta = self.server.request_context.meta
        except LookupError:
            return {}
        return dict(meta.model_extra or {}) if meta is not None else {}

    async def call(self, name: str, args: dict, meta: dict | None = None) -> types.CallToolResult:
        """Выполнить инструмент: проверка аргументов по схеме, операция над миром, журнал, оценка."""
        t0 = time.perf_counter()
        self._call_seq += 1
        self.stats["tool_calls"] += 1
        call_id = f"c{self._call_seq}"
        clock = self.world.clock
        self.journal.write("tool_call", clock, call_id=call_id, name=name, args=args)
        route = self.routes.get(name)
        changes: list[Change] = []
        if route is None:
            result: dict = fail("unknown_tool", f"no tool {name!r}")
            op, nargs = "unknown", dict(args)
        else:
            op, nargs = route.op, catalog_mod.route_args(route, args)
            schema_err = self._validate(name, args)
            if schema_err and op in ("write", "invoke"):     # у hw-mcp: приводы — отказ шлюза, чтения — ошибка
                result = reject("invalid_args", schema_err, nargs.get("device"))
            elif schema_err:
                result = fail("invalid_args", schema_err)
            else:
                result, changes = self._execute(route.op, nargs)
            if op in ("write", "invoke"):
                result["cmd_id"] = str((meta or {}).get(META_CMD_ID) or f"sim-{self._call_seq}")
        err = is_error(result)
        flags = {"ok": not err, "denied": bool(result.get("denied"))}
        input_id = self.evaluator.on_call(op, nargs, flags, clock)
        ms = round((time.perf_counter() - t0) * 1000, 3)
        self.journal.write("tool_result", clock, call_id=call_id, name=name, op=op, ok=flags["ok"],
                           denied=flags["denied"], reason=result.get("reason") or result.get("error"),
                           input_id=input_id, ms=ms)
        if op in ("answer", "refuse"):
            self.journal.write(op, clock, call_id=call_id, input_id=input_id, **nargs)
        if changes:
            self.world.note_recent(changes)
            await self._notify(changes)
        return types.CallToolResult(content=[types.TextContent(type="text", text=dumps(result))],
                                    structuredContent=result if self.structured else None, isError=err)

    def _validate(self, name: str, args: dict) -> str | None:
        e = best_match(self._validators[name].iter_errors(args))
        return None if e is None else e.message

    def _execute(self, op: str, a: dict) -> tuple[dict, list[Change]]:
        w = self.world
        if op == "list":
            return w.list_devices(self.rich_labels), []
        if op == "read":
            return w.read(a["device"], a["property"]), []
        if op == "write":
            return w.write(a["device"], a["property"], a["value"])
        if op == "invoke":
            return w.invoke(a["device"], a["action"], a.get("args"))
        if op == "health":
            return w.board_health(), []
        if op in ("answer", "refuse"):
            return {"ok": True, "recorded": True}, []
        raise ValueError(op)

    # ------------------------------------------------------------------ уведомления
    def _uris_for(self, changes: list[Change]) -> dict[str, set[str]]:
        """URI → изменённые свойства (для _meta["hw/changed"], как у hw-mcp)."""
        out: dict[str, set[str]] = {}
        for c in changes:
            out.setdefault(self.uri("state", c.device), set()).add(c.property)
        if self.extra_resources:
            allp = {f"{c.device}.{c.property}" for c in changes}
            out[self.uri("state")] = allp
            out[self.uri("events")] = allp
        return out

    def _notification(self, uri: str, props: set[str]) -> types.ServerNotification:
        meta: dict[str, Any] = {"hw/changed": sorted(props)}
        if self.inline_state and uri.startswith(self.uri("state", "")):
            dev = uri[len(self.uri("state", "")):]
            if dev in self.world.state:
                meta["device"] = dev
                meta["state"] = {p: self.world.state[dev][p]["value"] for p in sorted(props)
                                 if p in self.world.state[dev]}
        return types.ServerNotification(types.ResourceUpdatedNotification(
            params=types.ResourceUpdatedNotificationParams(uri=AnyUrl(uri), _meta=meta)))

    def _kick(self, peer: _Peer) -> bool:
        """Запустить разбор очереди сессии (если ещё не идёт и есть GET-поток). True — задача запущена сейчас."""
        if peer.running or not peer.pending or self._tg is None or not self._has_stream(peer):
            return False
        peer.running = True
        peer.idle = anyio.Event()
        self._tg.start_soon(self._drain, peer)
        return True

    async def _drain(self, peer: _Peer) -> None:
        try:
            while peer.pending and not peer.closed and self._has_stream(peer):
                uri = next(iter(peer.pending))
                props = peer.pending.pop(uri)   # до отправки: новое изменение того же URI снова попадёт в очередь
                try:
                    await peer.session.send_notification(self._notification(uri, props))
                    peer.sent += 1
                    self.stats["notify_sent"] += 1
                except (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream):
                    peer.closed = True
                    break
                except Exception as e:  # noqa: BLE001 — сбой одной сессии не должен ронять рассылку
                    peer.failed += 1
                    self.stats["notify_failed"] += 1
                    log.warning("уведомление %s не доставлено (%s: %s); подписки сохранены", uri,
                                type(e).__name__, e)
        finally:
            peer.running = False
            peer.idle.set()

    async def _notify(self, changes: list[Change]) -> dict:
        """Поставить resources/updated подписчикам (или всем сессиям при notify_all) и подождать доставки
        не дольше notify_wait_s. Возвращает {"queued", "pending"}."""
        if not self.peers:
            return {"queued": 0, "pending": 0}
        self._prune()
        uris = self._uris_for(changes)
        queued = 0
        fresh: list[_Peer] = []
        for peer in self.peers.values():
            targets = uris if self.notify_all else {u: p for u, p in uris.items() if u in peer.subs}
            if not targets:
                continue
            for u, props in targets.items():
                if u in peer.pending:
                    peer.pending[u] |= props
                    peer.coalesced += 1
                    self.stats["notify_coalesced"] += 1
                else:
                    peer.pending[u] = set(props)
                queued += 1
            if self._kick(peer):
                fresh.append(peer)     # ждём только сессии, которые не отставали до этого шага
        self.stats["notify_queued"] += queued
        if fresh and self.notify_wait_s > 0:
            with anyio.move_on_after(self.notify_wait_s) as scope:
                for peer in fresh:
                    await peer.idle.wait()
            if scope.cancelled_caught:
                for peer in fresh:
                    if peer.running:
                        peer.stalls += 1
                        self.stats["notify_stalls"] += 1
                        log.warning("сессия %s не успевает читать уведомления (в очереди %d); часы не ждут",
                                    peer.key[:12], len(peer.pending))
        return {"queued": queued, "pending": sum(len(p.pending) for p in self.peers.values())}

    # ------------------------------------------------------------------ часы и входы
    def _apply_inputs(self, inputs: list[dict]) -> list[str]:
        ids = []
        for ev in inputs:
            self.evaluator.open(ev, self.world.clock)
            self.journal.write("input", self.world.clock, input_id=ev["id"], kind=ev["kind"], t_event=ev["t"])
            ids.append(ev["id"])
        return ids

    async def advance(self, t: float) -> dict:
        before = self.world.clock
        changes, inputs = self.world.advance(t)
        for c in changes:
            self.journal.write("sensor", c.t, device=c.device, property=c.property, value=c.value, source=c.source)
        opened = self._apply_inputs(inputs)
        nt = await self._notify(changes) if changes else {"queued": 0, "pending": 0}
        self.journal.write("clock", self.world.clock, t_from=before, applied=len(changes), notified=nt["queued"],
                           pending=nt["pending"], sessions=len(self.peers), inputs=opened)
        return {"t": self.world.clock, "applied": len(changes), "notified": nt["queued"], "pending": nt["pending"],
                "inputs_opened": opened, "finished": self.world.finished}

    def report(self) -> dict:
        rep = self.evaluator.report()
        rep["day"] = {k: self.header.get(k) for k in ("home", "date", "split", "profile", "seed")}
        rep["catalog"] = self.catalog
        return rep

    def finish(self) -> dict:
        if not self._finished:
            self.evaluator.finish(self.world.clock)
            self._finished = True
        rep = self.report()
        self.journal.write("report", self.world.clock, summary=rep["summary"], inputs=rep["inputs"])
        if self.report_path:
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            self.report_path.write_text(json.dumps(rep, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                        encoding="utf-8")
        return rep

    def options(self) -> dict:
        return {"catalog": self.catalog, "scheme": self.scheme, "notify_all": self.notify_all,
                "structured": self.structured, "rich_labels": self.rich_labels,
                "extra_resources": self.extra_resources, "inline_state": self.inline_state,
                "notify_wait_s": self.notify_wait_s,
                "eval_tools": [t["name"] for t in self.tools if t["name"] in catalog_mod.EVAL_TOOLS]}


class _MCPEndpoint:
    """ASGI-обёртка менеджера сессий (Route с ASGI-приложением, без редиректа /mcp → /mcp/)."""

    def __init__(self, mgr: StreamableHTTPSessionManager):
        self.mgr = mgr

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.mgr.handle_request(scope, receive, send)


def build_app(sim: SimMCP, session_idle_timeout: float | None = SESSION_IDLE_TIMEOUT_S) -> Starlette:
    mgr = StreamableHTTPSessionManager(app=sim.server, stateless=False, json_response=False,
                                       session_idle_timeout=session_idle_timeout)
    sim._mgr = mgr

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except (json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    async def clock(request: Request) -> JSONResponse:
        if request.method == "GET":
            return JSONResponse({"t": sim.world.clock, "finished": sim.world.finished})
        data = await body(request)
        t = data.get("t", data.get("to_t"))
        if not isinstance(t, (int, float)) or isinstance(t, bool):
            return JSONResponse({"error": "body must be {\"t\": number}"}, status_code=400)
        try:
            return JSONResponse(await sim.advance(float(t)))
        except ClockError as e:
            return JSONResponse({"error": "clock_backwards", "detail": str(e), "t": sim.world.clock}, status_code=409)

    async def set_input(request: Request) -> JSONResponse:
        data = await body(request)
        iid = data.get("id")
        if not isinstance(iid, str) or not sim.evaluator.set_current(iid, sim.world.clock):
            return JSONResponse({"error": "unknown user input id"}, status_code=404)
        sim.journal.write("input_set", sim.world.clock, input_id=iid)
        return JSONResponse({"ok": True, "id": iid})

    async def state(request: Request) -> JSONResponse:
        return JSONResponse({"t": sim.world.clock, "state": sim.world.snapshot()})

    async def report(request: Request) -> JSONResponse:
        return JSONResponse(sim.report())

    async def finish(request: Request) -> JSONResponse:
        return JSONResponse(sim.finish())

    async def health(request: Request) -> JSONResponse:
        h = sim.header
        sim._prune()
        return JSONResponse({"ok": True, "t": sim.world.clock, "catalog": sim.catalog, **sim.session_stats(),
                             "day": {k: h.get(k) for k in ("home", "date", "split", "profile", "seed")}})

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mgr.run(), anyio.create_task_group() as tg:
            sim._tg = tg
            sim.journal.write("run_start", sim.world.clock, **sim.options(), version=VERSION,
                              day={k: sim.header.get(k) for k in ("home", "date", "split", "profile", "seed")},
                              n_tools=len(sim.tools), session_idle_timeout=session_idle_timeout,
                              catalog_bytes=catalog_mod.eval_share_bytes(sim.tools))
            try:
                yield
            finally:
                sim._tg = None
                tg.cancel_scope.cancel()
                sim.finish()
                sim.journal.write("run_end", sim.world.clock, **sim.session_stats())
                sim.journal.close()

    return Starlette(routes=[
        Route("/mcp", endpoint=_MCPEndpoint(mgr), methods=["GET", "POST", "DELETE"]),
        Route("/control/clock", clock, methods=["GET", "POST"]),
        Route("/control/input", set_input, methods=["POST"]),
        Route("/control/state", state, methods=["GET"]),
        Route("/control/report", report, methods=["GET"]),
        Route("/control/finish", finish, methods=["POST"]),
        Route("/healthz", health, methods=["GET"]),
    ], lifespan=lifespan)


def new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
