"""Реплеер дня для режима T0 (спецификация §5.1).

По событиям дня в порядке времени:
  user_cmd / question — (1) продвинуть часы до t; (2) POST mock /control/expect {input_id, expect, text, kind, t}
                        (сразу перед репликой: фоновые ходы окна продвижения не могут перехватить ожидание);
                        (3) POST edgectx /v1/user {id, text} и дождаться ответа;
  situation           — продвинуть часы до t − ε; зарегистрировать ожидание {input_id, expect, situation};
                        продвинуть до t; ничего не подавать (агент должен заметить сам); по дедлайну t + deadline_s
                        ожидание снимается (POST mock /control/expire);
  sensor              — продвинуть часы до t (sim-mcp на продвижение публикует события с t ≤ to_t);
                        режим --sensor-mode post дополнительно шлёт событие в edgectx /v1/event.
Продвижение часов — шагами не длиннее max_step_s, в порядке: mock /control/clock {t} (время для TTL
эмуляции кэша, если агент не шлёт X-Edgectx-Clock-T) → edgectx /v1/clock/advance {to_t} → sim-mcp
/control/clock {t}. edgectx сам дожидается окончания своих ходов в /v1/clock/advance, поэтому фиксированного
ожидания нет; синхронизация с уведомлениями sim-mcp (SSE, асинхронно) — параметр sync:
  inputs  — следующий advance с min_inputs {"sensor": inputs.sensor + notified} (контракт agent/README);
  updates — до следующего advance ждать, пока GET edgectx /v1/metrics mcp.updates не догонит сумму notified
            (точный счётчик принятых уведомлений; лишний HTTP-запрос к агенту на шаг с уведомлениями);
  auto    — inputs, а после 3 промахов min_inputs (вход не засчитан: значение не изменилось, ошибка разбора,
            несколько свойств) — updates до конца прогона; none — без синхронизации.
settle_ms > 0 — дополнительно ждать «тишины» mock-llm (нет запросов в полёте и settle_ms без активности)
после шагов, где сработали таймеры агента или sim-mcp разослал уведомления (агенты без заголовков часов).
Перед репликой — POST sim-mcp /control/input {id} (привязка вызовов к входу). В конце — продвижение до конца
окна, POST sim-mcp /control/finish (отчёт оценки), GET edgectx /v1/metrics, POST edgectx /v1/shutdown и
ожидание run_end в трассе, сбор журналов и manifest.json в <out>/<run_id>/.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from .dayfile import USER_KINDS, Day, DayEvent, load_day
from .jsonhttp import HttpError, JsonClient


@dataclass
class ReplayConfig:
    day: str
    out_root: str
    edgectx: str | None = None
    sim: str | None = None
    mock: str | None = None
    run_id: str | None = None
    max_step_s: float = 60.0
    settle_ms: float = 0.0              # 0 — без ожидания «тишины» mock-llm (edgectx ждёт свои ходы сам)
    settle_timeout_s: float = 30.0
    sync: str = "auto"                  # auto | inputs | updates | none — уведомления sim-mcp → агент
    sync_timeout_s: float = 2.0
    shutdown: bool = True               # POST edgectx /v1/shutdown перед сбором (трасса получает run_end)
    shutdown_timeout_s: float = 30.0
    user_timeout_s: float = 120.0
    http_timeout_s: float = 30.0
    situation_eps_s: float = 0.001
    expire_situations: bool = True
    sensor_mode: str = "sim"            # sim | post | none
    reset_mock: bool = True
    window_end: float | None = None     # переопределить конец проигрывания
    collect: dict[str, str] = field(default_factory=dict)    # метка → путь к файлу (трассы, sysmon, отчёты)
    configs: dict[str, str] = field(default_factory=dict)    # метка → конфиг (в manifest целиком)
    binaries: dict[str, str] = field(default_factory=dict)   # метка → бинарник (в manifest SHA-256)
    dry_run: bool = False
    force: bool = False

    def validate(self) -> None:
        if self.max_step_s <= 0:
            raise ValueError("max_step_s must be positive")
        if self.sensor_mode not in ("sim", "post", "none"):
            raise ValueError("sensor_mode must be sim, post or none")
        if self.sync not in SYNC_MODES:
            raise ValueError(f"sync must be one of {SYNC_MODES}")
        if self.sync_timeout_s <= 0:
            raise ValueError("sync_timeout_s must be positive")
        if not self.dry_run and not self.edgectx:
            raise ValueError("--edgectx is required (or use --dry-run)")


SYNC_MODES = ("auto", "inputs", "updates", "none")
AUTO_FALLBACK_MISSES = 3          # промахов min_inputs до перехода auto → updates


class ReplayError(RuntimeError):
    pass


class Replayer:
    def __init__(self, cfg: ReplayConfig, day: Day | None = None):
        cfg.validate()
        self.cfg = cfg
        self.day = day or load_day(cfg.day)
        self.run_id = cfg.run_id or f"{self.day.path.stem}-{time.strftime('%Y%m%d-%H%M%S')}"
        self.run_dir = Path(cfg.out_root) / self.run_id
        self.cur: float | None = None
        self.errors: list[str] = []
        self.sim_report: dict | None = None
        self._expiries: list[tuple[float, str]] = []
        self._log_f = None
        self.stats = {"advance_steps": 0, "user_inputs": 0, "user_errors": 0, "situations": 0, "expired": 0,
                      "settle_wait_ms": 0.0, "settles": 0, "notified": 0, "timers_fired": 0,
                      "sync": {"mode": cfg.sync, "waits": 0, "ok": 0, "missing": 0, "timeouts": 0, "wait_ms": 0.0,
                               "metrics_polls": 0, "fallback_at_t": None}}
        self._sync_mode = "inputs" if cfg.sync == "auto" else cfg.sync
        self._pending_notified = 0          # уведомления последнего шага sim-mcp, ещё не подтверждённые агентом
        self._sensor_base: int | None = None   # inputs.sensor из последнего ответа /v1/clock/advance
        self._updates_expected: int | None = None
        self._misses = 0
        self.mock_journal_since = 0
        self.edge_metrics = None
        self.shutdown_info: dict = {"requested": False}
        self.completed = False
        self.requested_window: tuple[float, float] | None = None
        t = cfg.http_timeout_s
        self.edge = JsonClient(cfg.edgectx, t, "edgectx") if cfg.edgectx and not cfg.dry_run else None
        self.sim = JsonClient(cfg.sim, t, "sim-mcp") if cfg.sim and not cfg.dry_run else None
        self.mock = JsonClient(cfg.mock, t, "mock-llm") if cfg.mock and not cfg.dry_run else None

    # ------------------------------------------------------------------ журнал реплеера

    def _log(self, action: str, **fields) -> None:
        rec = {"ts_wall": time.time(), "t": self.cur, "action": action, **fields}
        if self._log_f is not None:
            self._log_f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._log_f.flush()

    def _call(self, client: JsonClient | None, method: str, path: str, obj=None, timeout_s: float | None = None,
              required: bool = True):
        if client is None:
            return None
        try:
            status, data, raw, ms = client.request(method, path, obj, timeout_s)
        except HttpError as e:
            msg = str(e)
            self.errors.append(msg)
            self._log("error", error=msg)
            if required:
                raise ReplayError(msg) from e
            return None
        if status == 404 and not required and path != "/v1/user":   # необязательный эндпойнт отсутствует
            self._log("optional_missing", service=client.name, path=path)
            return None
        if status >= 400:
            msg = f"{client.name} {method} {path}: HTTP {status}: {raw[:300].decode('utf-8', 'replace')}"
            self.errors.append(msg)
            self._log("error", error=msg)
            if required:
                raise ReplayError(msg)
            return None
        return status, data, ms

    # ------------------------------------------------------------------ время

    def _settle(self) -> None:
        """Дождаться тишины mock-llm: inflight == 0 и settle_ms без активности после конца шага."""
        if self.cfg.settle_ms <= 0 or self.cfg.dry_run:
            return
        t_step = time.time()
        deadline = time.monotonic() + self.cfg.settle_timeout_s
        start = time.perf_counter()
        while True:
            if self.mock is None:
                time.sleep(self.cfg.settle_ms / 1000.0)
                break
            r = self._call(self.mock, "GET", "/control/state", required=False)
            state = r[1] if r and isinstance(r[1], dict) else {}
            inflight = int(state.get("inflight", 0))
            idle_ms = float(state.get("idle_ms", 1e9))
            quiet_since_step = (time.time() - t_step) * 1000.0
            if inflight == 0 and min(idle_ms, quiet_since_step) >= self.cfg.settle_ms:
                break
            if time.monotonic() > deadline:
                self.errors.append(f"settle timeout at t={self.cur} (inflight={inflight})")
                self._log("settle_timeout", inflight=inflight)
                break
            time.sleep(min(0.005, self.cfg.settle_ms / 4000.0))
        self.stats["settle_wait_ms"] += (time.perf_counter() - start) * 1000.0
        self.stats["settles"] += 1

    # ------------------------------------------------------------------ синхронизация с уведомлениями

    def _edge_updates(self) -> int | None:
        """Число уведомлений MCP, принятых агентом (GET /v1/metrics → mcp.updates); None — счётчика нет."""
        r = self._call(self.edge, "GET", "/v1/metrics", required=False)
        self.stats["sync"]["metrics_polls"] += 1
        data = r[1] if r and isinstance(r[1], dict) else None
        mcp = (data or {}).get("mcp")
        if isinstance(mcp, dict) and isinstance(mcp.get("updates"), (int, float)):
            return int(mcp["updates"])
        return None

    def _switch_to_updates(self) -> None:
        if self._updates_expected is None:
            raise ReplayError("cannot synchronize MCP notifications: edgectx /v1/metrics has no mcp.updates")
        self._sync_mode = "updates"
        self.stats["sync"]["fallback_at_t"] = self.cur
        self._log("sync_fallback", to="updates", expected=self._updates_expected)

    def _wait_updates(self) -> None:
        if self._updates_expected is None:
            raise ReplayError("cannot synchronize MCP notifications: no initial mcp.updates counter")
        target = self._updates_expected
        sync = self.stats["sync"]
        sync["waits"] += 1
        start = time.perf_counter()
        deadline = time.monotonic() + self.cfg.sync_timeout_s
        while True:
            got = self._edge_updates()
            if got is None:
                raise ReplayError("cannot synchronize MCP notifications: edgectx /v1/metrics has no mcp.updates")
            if got >= target:
                sync["ok"] += 1
                break
            if time.monotonic() > deadline:
                sync["timeouts"] += 1
                self._log("sync_timeout", mode="updates", expected=target, got=got)
                sync["wait_ms"] += (time.perf_counter() - start) * 1000.0
                raise ReplayError(f"MCP synchronization timeout: expected mcp.updates >= {target}, got {got}")
            time.sleep(0.001)
        sync["wait_ms"] += (time.perf_counter() - start) * 1000.0

    def _advance_body(self, to_t: float) -> dict:
        body = {"to_t": to_t}
        if self._pending_notified > 0 and not self.cfg.dry_run:
            if self._sync_mode == "inputs":
                if self._sensor_base is None:
                    raise ReplayError("cannot synchronize MCP notifications: clock advance has no inputs.sensor")
                body["min_inputs"] = {"sensor": self._sensor_base + self._pending_notified}
                body["timeout_ms"] = int(self.cfg.sync_timeout_s * 1000)
                self.stats["sync"]["waits"] += 1
            elif self._sync_mode == "updates":
                self._wait_updates()
        return body

    def _after_advance(self, body: dict, r) -> int:
        """Разобрать ответ /v1/clock/advance: промахи min_inputs, база счётчика sensor, сработавшие таймеры."""
        resp = r[1] if r and isinstance(r[1], dict) else {}
        if resp.get("idle") is False:
            raise ReplayError(f"edgectx not idle after clock advance to {body['to_t']}")
        fired = resp.get("timers_fired")
        fired = int(fired) if isinstance(fired, (int, float)) else 0
        if "min_inputs" in body:
            sync = self.stats["sync"]
            sync["wait_ms"] += float(resp.get("waited_ms") or 0.0)
            if resp.get("missing_inputs"):
                sync["missing"] += 1
                self._misses += 1
                self._log("sync_missing", expected=body["min_inputs"], missing=resp["missing_inputs"])
                if self.cfg.sync != "auto":
                    raise ReplayError(f"MCP synchronization failed: missing inputs {resp['missing_inputs']}")
                if self._misses >= AUTO_FALLBACK_MISSES:
                    self._switch_to_updates()
                # Уведомление о неизменившемся свойстве не обязано создавать sensor input. Но сам приём
                # и окончание обработки всё равно подтверждаем; target накоплен с начала прогона.
                self._wait_updates()
                r = self._call(self.edge, "POST", "/v1/clock/advance", {"to_t": body["to_t"]})
                resp = r[1] if r and isinstance(r[1], dict) else {}
                if resp.get("idle") is False:
                    raise ReplayError(f"edgectx not idle after MCP synchronization at {body['to_t']}")
            else:
                sync["ok"] += 1
        self._pending_notified = 0
        inputs = resp.get("inputs")
        if isinstance(inputs, dict):
            self._sensor_base = int(inputs.get("sensor") or 0)
        self.stats["timers_fired"] += fired
        return fired

    def _sync_now(self) -> None:
        """Перед репликой: убедиться, что агент принял уведомления последнего шага (иначе ход пользователя
        соберёт промпт по устаревшему состоянию). inputs — advance на текущее время с min_inputs."""
        if self._pending_notified <= 0 or self.cfg.dry_run or self.edge is None:
            return
        if self._sync_mode in ("inputs", "updates"):
            if self._sync_mode == "inputs" and self._sensor_base is None:
                raise ReplayError("cannot synchronize MCP notifications: clock advance has no inputs.sensor")
            body = self._advance_body(self.cur)
            self._after_advance(body, self._call(self.edge, "POST", "/v1/clock/advance", body))

    def _step(self, to_t: float) -> None:
        self._call(self.mock, "POST", "/control/clock", {"t": to_t})
        body = self._advance_body(to_t)
        fired = self._after_advance(body, self._call(self.edge, "POST", "/v1/clock/advance", body))
        r = self._call(self.sim, "POST", "/control/clock", {"t": to_t})
        sim = r[1] if r and isinstance(r[1], dict) else {}
        notified = sim.get("notified")
        notified = int(notified) if isinstance(notified, (int, float)) else 0
        self._pending_notified = notified
        self.stats["notified"] += notified
        if self._updates_expected is not None:
            self._updates_expected += notified
        self.cur = to_t
        self.stats["advance_steps"] += 1
        if self.cfg.settle_ms > 0 and (fired > 0 or notified > 0):
            self._settle()
        self._log("advance", to_t=to_t, **({"notified": notified} if notified else {}),
                  **({"timers_fired": fired} if fired else {}))

    def advance_to(self, target: float) -> None:
        if self.cur is None:
            self._step(target)
            self._process_expiries()
            return
        while self.cur < target:
            nxt = min(target, self.cur + self.cfg.max_step_s)
            due = [t for t, _ in self._expiries if self.cur < t <= nxt]
            if due:
                nxt = min(due)
            self._step(nxt)
            self._process_expiries()

    def _process_expiries(self) -> None:
        if any(t <= self.cur for t, _ in self._expiries):
            # Датчик ровно на дедлайне ещё может обрабатываться после ответа sim /control/clock.
            self._sync_now()
        keep = []
        for t, input_id in self._expiries:
            if t <= self.cur:
                self._call(self.mock, "POST", "/control/expire", {"input_id": input_id}, required=False)
                self.stats["expired"] += 1
                self._log("expire", input_id=input_id, deadline_t=t)
            else:
                keep.append((t, input_id))
        self._expiries = keep

    # ------------------------------------------------------------------ события

    def _expect(self, payload: dict) -> None:
        self._call(self.mock, "POST", "/control/expect", payload)
        self._log("expect", input_id=payload["input_id"], kind=payload.get("kind"))

    def _user(self, e: DayEvent) -> None:
        raw = e.raw
        if e.kind == "question":
            expect = dict(raw.get("expect") or {})
            expect.setdefault("type", "answer")
            if "answer" in raw:
                expect.setdefault("answer", raw["answer"])
        else:
            expect = dict(raw.get("expect") or {"type": "action", "calls": []})
        payload = {"input_id": e.id, "kind": e.kind, "text": raw.get("text"), "t": e.t, "expect": expect}
        if raw.get("k") is not None:
            payload["k"] = raw["k"]      # вызовов LLM на вход (daygen, VisionClaw) — оракул добирает до k
        self.advance_to(e.t)
        self._sync_now()
        # Ожидание — сразу перед репликой: фоновые пробуждения окна продвижения его не перехватят.
        self._expect(payload)
        # sim-mcp: явно открыть вход, чтобы вызовы агента (answer, действия) привязались к нему
        self._call(self.sim, "POST", "/control/input", {"id": e.id}, required=False)
        self.stats["user_inputs"] += 1
        t0 = time.perf_counter()
        r = self._call(self.edge, "POST", "/v1/user", {"id": e.id, "text": raw.get("text", "")},
                       timeout_s=self.cfg.user_timeout_s, required=False)
        rtt = (time.perf_counter() - t0) * 1000.0
        if r is None and not self.cfg.dry_run:
            self.stats["user_errors"] += 1
        self._log("user", input_id=e.id, kind=e.kind, ok=r is not None or self.cfg.dry_run,
                  status=r[0] if r else None, rtt_ms=rtt, reply=r[1] if r else None)
        if self.cfg.settle_ms > 0:
            self._settle()

    def _situations(self, group: list[DayEvent]) -> None:
        """Ситуации одного момента t: все регистрируются в t − ε, до того как sim-mcp опубликует датчики момента t
        (иначе пробуждение гейта на этих датчиках увидит только часть ситуаций)."""
        t = group[0].t
        if self.cur is not None and t - self.cfg.situation_eps_s > self.cur:
            self.advance_to(t - self.cfg.situation_eps_s)
        for e in group:
            raw = e.raw
            situation = {k: raw[k] for k in ("id", "name", "significant", "deadline_s", "t") if k in raw}
            self._expect({"input_id": e.id, "kind": "situation", "t": e.t,
                          "expect": dict(raw.get("expect") or {"type": "notify_or_action"}), "situation": situation})
            self.stats["situations"] += 1
            if self.cfg.expire_situations and raw.get("deadline_s") is not None:
                self._expiries.append((e.t + float(raw["deadline_s"]), e.id))
        self._expiries.sort()
        self.advance_to(t)

    def _sensor(self, e: DayEvent) -> None:
        self.advance_to(e.t)
        if self.cfg.sensor_mode == "post":
            self._call(self.edge, "POST", "/v1/event", e.raw, required=False)
            if self.cfg.settle_ms > 0:
                self._settle()

    # ------------------------------------------------------------------ прогон

    def run(self) -> Path:
        if self.run_dir.exists() and any(self.run_dir.iterdir()) and not self.cfg.force:
            raise ReplayError(f"run directory exists and is not empty: {self.run_dir} (use --force)")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        start_t, end_t = self.day.span()
        if self.cfg.window_end is not None:
            end_t = self.cfg.window_end
        if not math.isfinite(end_t) or end_t < start_t:
            raise ReplayError("window_end must be finite and not before the start of the day")
        self.requested_window = (start_t, end_t)
        self._log_f = open(self.run_dir / "replay.jsonl", "w", encoding="utf-8")
        try:
            self._log("start", run_id=self.run_id, day=str(self.day.path), span=[start_t, end_t],
                      counts=self.day.counts())
            if self.cfg.reset_mock:
                r = self._call(self.mock, "POST", "/control/reset", {"journal": True})
                seq = (r[1] or {}).get("journal_seq") if r and isinstance(r[1], dict) else None
            else:
                r = self._call(self.mock, "GET", "/control/state", required=False)
                seq = (r[1] or {}).get("journal_seq") if r and isinstance(r[1], dict) else None
            self.mock_journal_since = int(seq) if isinstance(seq, (int, float)) else 0
            if self.cfg.sync in ("auto", "updates") and self.edge is not None:
                base = self._edge_updates()
                self._updates_expected = base
                if base is None and self._sync_mode == "updates":
                    raise ReplayError("sync=updates requires edgectx /v1/metrics mcp.updates")
            self.advance_to(start_t)
            events = self.day.events
            i = 0
            while i < len(events):
                e = events[i]
                if e.t > end_t:
                    break
                if e.kind == "situation":
                    j = i
                    while j < len(events) and events[j].kind == "situation" and events[j].t == e.t:
                        j += 1
                    self._situations(events[i:j])
                    i = j
                    continue
                i += 1
                if e.kind in USER_KINDS:
                    self._user(e)
                elif e.kind == "sensor":
                    self._sensor(e)
                else:
                    self.advance_to(e.t)
            self.advance_to(end_t)
            self._sync_now()
            # sim-mcp закрывает окна оценки и отдаёт отчёт (проверка по истинному состоянию, §12.8)
            r = self._call(self.sim, "POST", "/control/finish", {}, required=False)
            self.sim_report = r[1] if r and isinstance(r[1], dict) else None
            if self.edge is not None:     # до остановки: после /v1/shutdown агент не отвечает
                r = self._call(self.edge, "GET", "/v1/metrics", required=False)
                self.edge_metrics = r[1] if r is not None else None
            self.shutdown_info = self._shutdown()
            self.completed = True
            self._log("end", stats=self.stats, shutdown=self.shutdown_info)
        except Exception as e:
            msg = str(e)
            if msg not in self.errors:
                self.errors.append(msg)
            self._log("aborted", error=msg, stats=self.stats)
            raise
        finally:
            self._log_f.close()
            self._log_f = None
            from .manifest import collect_run  # noqa: PLC0415 — избегаем цикла импорта

            collect_run(self, started, time.time())
        return self.run_dir

    # ------------------------------------------------------------------ остановка агента

    def _edgectx_traces(self) -> list[Path]:
        return [Path(p) for label, p in self.cfg.collect.items()
                if label == "trace" or (label.startswith("edgectx") and "trace" in label)]

    @staticmethod
    def _has_run_end(path: Path) -> bool:
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", "replace").strip().splitlines()
        except OSError:
            return False
        for line in reversed(tail[-5:]):
            try:
                if json.loads(line).get("type") == "run_end":
                    return True
            except (ValueError, AttributeError):
                continue
        return False

    def _shutdown(self) -> dict:
        """POST edgectx /v1/shutdown и ожидание записи run_end в трассе (агент дописывает ходы и закрывает
        трассу): без run_end монитор не принимает трассу (спецификация §9)."""
        if not self.cfg.shutdown or self.edge is None or self.cfg.dry_run:
            return {"requested": False}
        t0 = time.perf_counter()
        r = self._call(self.edge, "POST", "/v1/shutdown", {}, required=False)
        info: dict = {"requested": True, "status": r[0] if r else None}
        if r is None or r[0] >= 300:      # эндпойнта нет (двойник) или ошибка — ждать нечего
            info.update({"run_end": None, "stopped": False, "waited_s": time.perf_counter() - t0})
            return info
        traces = [p for p in self._edgectx_traces() if p.exists()]
        deadline = time.monotonic() + self.cfg.shutdown_timeout_s
        done = False
        while time.monotonic() < deadline:
            if traces:
                done = all(self._has_run_end(p) for p in traces)
            else:          # трассы нет в --collect — ждать, пока агент перестанет отвечать
                try:
                    self.edge.request("GET", "/v1/health", None, 1.0)
                except Exception:  # noqa: BLE001 — соединение закрыто: агент остановился
                    done = True
            if done:
                break
            time.sleep(0.02)
        info.update({"run_end": done if traces else None, "stopped": done, "waited_s": time.perf_counter() - t0,
                     "traces": [str(p) for p in traces]})
        if not done:
            self.errors.append(f"edgectx shutdown: no run_end within {self.cfg.shutdown_timeout_s:g} s")
        self.edge.close()
        return info

    def close(self) -> None:
        for c in (self.edge, self.sim, self.mock):
            if c is not None:
                c.close()
