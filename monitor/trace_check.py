"""Проверка трассы edgectx по контракту IMPLEMENTATION_SPEC §6.

Проверяются: поля записей; рамка прогона (run_start … run_end, единый run_id, seq); монотонность ts_clock;
пары llm_request/llm_response и tool_call/tool_result; «wake перед llm_request»; число шагов хода
(max_steps), запросов в полёте (max_in_flight) и суточные бюджеты; ссылки input_id; ответы пользователю;
аргументы вызовов по схемам каталога.

Атрибуция записей к ходу. Контракт §6 не связывает llm_request с конкретным wake, а при max_in_flight = 2 ходы
перемежаются. Поэтому монитор работает в двух режимах:
  * связанный — если записи несут turn_id (или wake_id) у wake и llm_request и req_id/rule_id у tool_call:
    все проверки хода точные;
  * позиционный — без этих полей. Используются только необходимые условия, которые выполняются у любой
    корректной трассы (проверки «звуковые»: не дают ложных срабатываний, но могут пропустить нарушение).
    Подробности — в README монитора, раздел «Что гарантируется».
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime

from monitor import argschema
from monitor.catalog import Catalog
from monitor.jsonl import Line, loads_strict
from monitor.report import Report

SOURCE = "trace"

INPUT_KINDS = ["user", "sensor", "situation", "tick"]
WAKE_REASONS = ["user", "gate", "heartbeat", "rule", "deferred"]
PRIORITIES = ["user", "safety", "background"]
SEGMENT_KEYS = ["prefix", "profile", "history", "state", "turn"]
LINK_FIELDS = ("turn_id", "wake_id")
# Какие виды входа допустимы для причины пробуждения (проверяется, если у wake есть input_id). deferred — ход, в
# который edgectx слил пробуждения, отложенные до восстановления связи с LLM: входы любых видов.
WAKE_INPUT_KINDS = {"user": {"user"}, "heartbeat": {"tick"}, "gate": {"sensor", "situation"},
                    "rule": set(INPUT_KINDS), "deferred": set(INPUT_KINDS)}

_ID = {"type": ["string", "integer"]}
_OPT_ID = {"type": ["string", "integer", "null"]}
_NNI = {"type": "integer", "minimum": 0}
_NNN = {"type": "number", "minimum": 0}

ENVELOPE = {
    "type": "object",
    "required": ["ts_wall", "ts_clock", "type", "run_id"],
    "properties": {
        "ts_wall": {"type": ["number", "string"]},
        "ts_clock": {"type": "number", "minimum": 0},
        "type": {"type": "string", "minLength": 1},
        "run_id": {"type": "string", "minLength": 1},
        "seq": {"type": "integer", "minimum": 0},
    },
}

FIELDS: dict[str, dict] = {
    "run_start": {"required": ["config_hash", "policy"], "properties": {
        "config_hash": {"type": "string", "minLength": 1},
        "policy": {"type": "object"},
        "budget": {"type": "object", "properties": {
            "daily_calls": _NNI, "daily_input_tokens": _NNI, "degrade_at": {"type": "number", "minimum": 0, "maximum": 1}}},
        "tools": {"type": "array"},
    }},
    "input": {"required": ["kind", "id"], "properties": {"kind": {"enum": INPUT_KINDS}, "id": _ID}},
    "wake": {"required": ["reason", "suppressed"], "properties": {
        "reason": {"enum": WAKE_REASONS}, "suppressed": {"type": "boolean"}, "input_id": _OPT_ID,
        "input_ids": {"type": "array", "items": _ID}, "priority": {"enum": PRIORITIES}, "turn_id": _ID,
        "wake_id": _ID, "coalesced": {"type": "array", "items": _ID}}},
    "suppress": {"required": ["reason", "input_id"], "properties": {
        "reason": {"type": "string", "minLength": 1}, "input_id": _ID, "wake_id": _ID}},
    "llm_request": {"required": ["req_id", "model", "prompt_tokens_est", "segments", "n_tools"], "properties": {
        "req_id": _ID, "model": {"type": "string", "minLength": 1}, "prompt_tokens_est": _NNI,
        "segments": {"type": "object", "required": SEGMENT_KEYS, "properties": {k: _NNI for k in SEGMENT_KEYS}},
        "segments_tokens_est": {"type": "object", "properties": {k: _NNI for k in SEGMENT_KEYS}},
        "n_tools": _NNI, "turn_id": _ID, "wake_id": _ID, "purpose": {"type": "string"}}},
    "llm_response": {"required": ["req_id", "latency_ms", "usage", "finish_reason", "n_tool_calls"], "properties": {
        "req_id": _ID, "latency_ms": _NNN, "finish_reason": {"type": ["string", "null"]}, "n_tool_calls": _NNI,
        "has_usage": {"type": "boolean"}, "error": {"type": ["string", "null"]}, "error_kind": {"type": "string"},
        "maybe_billed": {"type": "boolean"},
        "usage": {"type": "object", "required": ["prompt_tokens", "completion_tokens", "cached_tokens"], "properties": {
            "prompt_tokens": _NNI, "completion_tokens": _NNI, "cached_tokens": {"type": ["integer", "null"], "minimum": 0}}}}},
    "tool_call": {"required": ["call_id", "server", "name", "args"], "properties": {
        "call_id": _ID, "server": {"type": "string"}, "name": {"type": "string", "minLength": 1},
        "args": {"type": ["object", "string"]}, "req_id": _ID, "rule_id": _ID, "turn_id": _ID, "wake_id": _ID,
        "by": {"enum": ["llm", "rule"]}}},
    "tool_result": {"required": ["call_id", "ok", "denied", "ms"], "properties": {
        "call_id": _ID, "ok": {"type": "boolean"}, "denied": {"type": "boolean"}, "ms": {"type": ["number", "null"], "minimum": 0}}},
    "user_reply": {"required": ["input_id", "text", "latency_ms"], "properties": {
        "input_id": _ID, "text": {"type": "string"}, "latency_ms": _NNN, "deferred": {"type": "boolean"}}},
    "notify": {"required": ["text"], "properties": {"situation_id": _OPT_ID, "text": {"type": "string"},
                                                   "input_ids": {"type": "array", "items": _ID}}},
    "rule_fired": {"required": ["rule_id", "input_id"], "properties": {"rule_id": _ID, "input_id": _OPT_ID}},
    "run_end": {},
    # Расширение edgectx (agent/internal/trace): сбой подсистемы, не прервавший прогон. С req_id/call_id
    # закрывает запрос к LLM / вызов инструмента как неуспешный.
    "error": {"required": ["where", "msg"], "properties": {
        "where": {"type": "string"}, "msg": {"type": "string"}, "req_id": _ID, "call_id": _ID}},
    # Расширение edgectx: обрыв связи с LLM и падение MCP-сервера (agent/README, «Плохая сеть и обрывы связи с
    # LLM», «Падение MCP-сервера»). deferred — пробуждение отложено до восстановления связи: исхода входам не даёт,
    # исход — участие в слитом wake (reason deferred) или suppress (deferred_full, shutdown).
    "llm_unavailable": {"required": ["failures", "error_kind", "retry_in_s"], "properties": {
        "failures": {"type": "integer", "minimum": 1}, "error_kind": {"type": "string"}, "retry_in_s": _NNN}},
    "llm_probe": {"required": ["ok", "ms", "status", "bytes"], "properties": {
        "ok": {"type": "boolean"}, "ms": _NNN, "status": _NNI, "bytes": _NNI, "retry_in_s": _NNN}},
    "llm_available": {"required": ["down_s"], "properties": {"down_s": _NNN}},
    "deferred": {"required": ["wake_id", "reason", "priority", "input_ids", "retry", "pending"], "properties": {
        "wake_id": _ID, "reason": {"enum": WAKE_REASONS}, "priority": {"enum": PRIORITIES},
        "input_ids": {"type": "array", "items": _ID}, "retry": {"type": "boolean"}, "pending": _NNI,
        "coalesced": {"type": "array", "items": _ID}}},
    "mcp_down": {"required": ["server", "error"], "properties": {
        "server": {"type": "string", "minLength": 1}, "error": {"type": "string"}}},
    "mcp_up": {"required": ["server", "attempts", "down_s"], "properties": {
        "server": {"type": "string", "minLength": 1}, "attempts": {"type": "integer", "minimum": 1}, "down_s": _NNN}},
}
TYPES = tuple(FIELDS)

# Запросы к LLM вне хода (сводка истории, консолидация профиля) — освобождены от проверки wake/шагов, если
# помечены полем purpose.
DEFAULT_EXEMPT_PURPOSES = frozenset({"summary", "consolidate", "consolidation", "profile"})


@dataclass
class TraceOptions:
    max_steps: int | None = None          # None — из run_start.policy.max_steps
    max_in_flight: int | None = None      # None — из run_start (sched/policy), иначе 1 (умолчание edgectx)
    daily_calls: int | None = None        # None — из run_start.budget
    daily_input_tokens: int | None = None
    degrade_at: float | None = None
    day_s: float = 86400.0                # сутки по ts_clock: floor(ts_clock / day_s), как планировщик edgectx
    strict_integer: bool = False
    allow_types: frozenset = frozenset()
    latency_slack_ms: float = 50.0
    exempt_purposes: frozenset = DEFAULT_EXEMPT_PURPOSES
    # Правдоподобие usage: |prompt_tokens / prompt_tokens_est − 1| > usage_tolerance → предупреждение
    # (оценка edgectx и счёт провайдера расходятся; 0 токенов при непустом запросе — нарушение).
    usage_tolerance: float = 0.5
    segments_tolerance: float = 0.10      # |Σ segments_tokens_est − prompt_tokens_est| > доли (+16 токенов)
    # Записи error одного источника (where): предупреждение, если их ≥ errors_min и ≥ errors_ratio всех записей.
    errors_min: int = 10
    errors_ratio: float = 0.05


@dataclass
class CallInfo:
    """tool_call с результатом — для сверки с журналом шлюза."""
    line: int
    call_id: object
    server: str
    name: str
    args: dict | None
    ts_clock: float
    result_line: int | None = None
    ok: bool | None = None
    denied: bool | None = None
    source: str | None = None     # llm | rule
    ts_wall: float | None = None
    result_ts_wall: float | None = None
    issued_clock: float | None = None   # ts_clock ответа LLM, породившего вызов (возраст решения — по трассе)


@dataclass(frozen=True)
class Reading:
    """Показание датчика, которое видел агент (input kind=sensor с device/property/value)."""
    line: int
    ts_wall: float | None
    ts_clock: float
    key: str                      # "<устройство>.<свойство>" — как ключи состояния в блокировках шлюза
    value: object


@dataclass
class _Wake:
    line: int
    reason: str
    priority: str | None
    used: bool = False

    @property
    def certainly_user(self) -> bool:
        return self.priority == "user" or (self.priority is None and self.reason == "user")

    @property
    def certainly_background(self) -> bool:
        return self.priority == "background" or (self.priority is None and self.reason == "heartbeat")

    @property
    def rank(self) -> int:
        """Порядок обслуживания как в планировщике: пользователь > безопасность > фон."""
        if self.certainly_user:
            return 0
        return 2 if self.certainly_background else 1


@dataclass
class _Turn:
    wake: _Wake
    suppressed: bool
    n_requests: int = 0
    calls: list = field(default_factory=list)   # call_key вызовов, порождённых ответами этого хода
    last_req: str | None = None                 # req_key последнего ответа LLM хода (для вызовов с wake_id)


@dataclass
class _Request:
    line: int
    ts_clock: float
    ts_wall: float | None
    day: int
    turn: _Turn | None
    responded: bool = False
    exempt: bool = False      # служебный запрос вне хода (purpose): не влияет на атрибуцию ходов
    n_tools: int = 0
    prompt_est: int = 0
    resp_clock: float | None = None   # ts_clock успешного ответа
    truncated: bool = False           # ответ обрезан по длине и объявил вызовы
    truncated_reported: bool = False


def _key(v) -> str:
    """Ключ идентификатора: "17" и 17 — разные идентификаторы (JSON-типы различаются)."""
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def wall_seconds(v) -> float | None:
    """ts_wall: число (секунды Unix; > 1e12 — миллисекунды) или строка RFC 3339 → секунды Unix."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        return x / 1000.0 if x > 1e12 else x
    if isinstance(v, str):
        s = v.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if "." in s:  # Go RFC3339Nano: до 9 знаков дробной части — fromisoformat понимает до 6
            head, _, rest = s.partition(".")
            n = 0
            while n < len(rest) and rest[n].isdigit():  # только ведущие цифры: дальше идёт смещение пояса
                n += 1
            s = f"{head}.{rest[:min(n, 6)]}{rest[n:]}"
        try:
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            return None
    return None


class TraceChecker:
    def __init__(self, report: Report, catalog: Catalog | None, opts: TraceOptions | None = None):
        self.r = report
        self.catalog = catalog
        self.o = opts or TraceOptions()
        self.calls: dict[str, CallInfo] = {}
        self.readings: list[Reading] = []
        self.summary: dict = {}

    # ------------------------------------------------------------------------------------------------
    def add(self, cls, line, msg, **ref):
        self.r.add(cls, SOURCE, line, msg, **ref)

    def check(self, lines: list[Line]) -> list[CallInfo]:
        records: list[tuple[int, dict]] = []
        for ln in lines:
            if ln.error is not None:
                self.add("parse", ln.no, ln.error)
            else:
                records.append((ln.no, ln.obj))
        valid = self._check_fields(records)
        self._check_frame(records)
        self._semantics(valid)
        return list(self.calls.values())

    # --- 1. поля ------------------------------------------------------------------------------------
    def _check_fields(self, records):
        valid = []
        for no, rec in records:
            errs = argschema.validate(rec, ENVELOPE)
            typ = rec.get("type")
            spec = FIELDS.get(typ) if isinstance(typ, str) else None
            if isinstance(typ, str) and spec is None:
                if typ not in self.o.allow_types:
                    self.add("event.unknown_type", no, f"неизвестный тип записи {typ!r}")
                continue
            if spec is not None:
                errs += argschema.validate(rec, {"type": "object", **spec})
            if errs:
                self.add("event.field", no, f"{typ}: " + "; ".join(f"{e.path or '/'}: {e.message}" for e in errs[:4]))
                continue
            valid.append((no, rec))
        return valid

    # --- 2. рамка, порядок, время -------------------------------------------------------------------
    def _check_frame(self, records):
        if not records:
            self.add("frame.no_run_start", None, "трасса пуста")
            return
        first_no, first = records[0]
        if first.get("type") != "run_start":
            self.add("frame.no_run_start", first_no, f"первая запись — {first.get('type')!r}, а не run_start")
        run_id = first.get("run_id")
        started = False
        ended_at = None
        after_end = 0
        prev_seq = prev_clock = prev_wall = None
        for no, rec in records:
            typ = rec.get("type")
            if typ == "run_start":
                if started:
                    self.add("frame.duplicate_run_start", no, "повторный run_start")
                started = True
            if ended_at is not None:
                after_end += 1
                if after_end == 1:
                    self.add("frame.after_run_end", no, f"запись {typ!r} после run_end (строка {ended_at})")
            if typ == "run_end" and ended_at is None:
                ended_at = no
            if isinstance(run_id, str) and rec.get("run_id") != run_id:
                self.add("frame.run_id", no, f"run_id {rec.get('run_id')!r} ≠ {run_id!r}")
            seq = rec.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool):
                if prev_seq is not None and seq != prev_seq + 1:
                    self.add("order.seq", no, f"seq {seq} после {prev_seq}")
                prev_seq = seq
            clock = rec.get("ts_clock")
            if argschema.is_number(clock) and math.isfinite(clock):
                if prev_clock is not None and clock < prev_clock:
                    self.add("time.clock_backwards", no, f"ts_clock {clock} < {prev_clock}")
                prev_clock = clock if prev_clock is None else max(prev_clock, clock)
            wall = wall_seconds(rec.get("ts_wall"))
            if wall is not None:
                if prev_wall is not None and wall < prev_wall:
                    self.add("time.wall_backwards", no, f"ts_wall на {prev_wall - wall:.6f} с раньше предыдущей")
                prev_wall = wall if prev_wall is None else max(prev_wall, wall)
        if after_end > 1:
            self.r.summary.setdefault("trace", {})["records_after_run_end"] = after_end
        if ended_at is None:
            self.add("frame.no_run_end", records[-1][0], "нет run_end")

    # --- 3. семантика -------------------------------------------------------------------------------
    def _settings(self, run_start: dict | None):
        rs = run_start or {}
        policy = rs.get("policy") if isinstance(rs.get("policy"), dict) else {}
        sched = rs.get("sched") if isinstance(rs.get("sched"), dict) else {}
        budget = rs.get("budget") if isinstance(rs.get("budget"), dict) else (
            policy.get("budget") if isinstance(policy.get("budget"), dict) else {})

        def pick(opt, *cands, default=None, name=""):
            if opt is not None:
                return opt
            for c in cands:
                if argschema.is_number(c):
                    return c
            if default is not None:
                self.r.assumptions[name] = default
            return default

        self.max_steps = pick(self.o.max_steps, policy.get("max_steps"), name="max_steps")
        self.max_in_flight = pick(self.o.max_in_flight, sched.get("max_in_flight"), policy.get("max_in_flight"),
                                  rs.get("max_in_flight"), default=1, name="max_in_flight")
        self.daily_calls = pick(self.o.daily_calls, budget.get("daily_calls"))
        self.daily_tokens = pick(self.o.daily_input_tokens, budget.get("daily_input_tokens"))
        self.degrade_at = pick(self.o.degrade_at, budget.get("degrade_at"))
        if self.max_steps is None:
            self.r.skip("budget.max_steps: max_steps не задан (ни run_start.policy.max_steps, ни --max-steps)")
        if not self.daily_calls and not self.daily_tokens:
            self.r.skip("budget.daily_*: суточные лимиты не заданы или равны 0 (без ограничения)")

    def _semantics(self, records):
        run_start = next((rec for _, rec in records if rec["type"] == "run_start"), None)
        self._settings(run_start)
        if self.catalog is None and run_start and isinstance(run_start.get("tools"), list):
            self.catalog = Catalog.from_obj(run_start["tools"])
        if self.catalog is None:
            self.r.skip("args.*: нет каталога инструментов (--tools или run_start.tools)")
        else:
            for p in self.catalog.problems:
                self.r.add("catalog.invalid", "tools", None, p)
            if self.catalog.unchecked:
                self.r.summary["catalog_unchecked_keywords"] = sorted(self.catalog.unchecked)

        sequential = self.max_in_flight == 1
        linked_mode = Counter()
        inputs: dict[str, tuple[int, str, float | None]] = {}
        replies: Counter = Counter()
        user_inputs: dict[str, int] = {}
        credits: list[_Wake] = []           # wake, ещё не начавшие ход (позиционный режим)
        turns: dict[str, _Turn] = {}
        requests: dict[str, _Request] = {}
        early_responses: dict[str, int] = {}
        outstanding: set[str] = set()
        expected_calls: dict[str, int] = {}  # req_key -> n_tool_calls (связанный режим)
        got_calls: Counter = Counter()
        early_results: dict[str, int] = {}
        rule_ids: set[str] = set()
        suppressed_users: set[str] = set()  # вход пользователя явно отклонён — ответа не требуется
        day_calls: Counter = Counter()       # нижняя оценка учтённых вызовов: запрос и ответ в одних сутках
        day_tokens: Counter = Counter()
        llm_call_credits = 0                 # объявлено ответами LLM, ещё не вызвано (позиционный режим)
        chain_len = 0
        last_continuable = False
        continuable_credits = 0
        rules_seen = wakes_seen = 0
        max_outstanding = 0
        stats = Counter()
        tokens = Counter()
        wakes_by_reason = Counter()
        turn_starts_by_reason = Counter()   # начатые ходы по причине wake (в позиционном режиме — оценка)
        calls_by_reason = Counter()         # все запросы по причине wake (только связанный режим)
        suppress_by_reason = Counter()
        tool_by_server = Counter()
        purposes = Counter()
        woke: Counter = Counter()            # вход -> число допущенных пробуждений с ним
        suppressed_n: Counter = Counter()    # вход -> число записей suppress
        admitted_wakes: dict[str, int] = {}  # связанный режим: ход допущенного wake -> строка
        turns_with_request: set[str] = set()
        errors_by_where = Counter()
        usage_off: list[tuple[int, float]] = []    # (строка, prompt_tokens / оценка) вне допуска
        usage_checked = 0
        segments_off: list[int] = []
        no_usage_lines: list[int] = []
        last_calls_req: str | None = None    # позиционная атрибуция: последний ответ с вызовами
        ended = False
        day_of = lambda t: int(math.floor(t / self.o.day_s))  # noqa: E731

        def ref_input(no, iid, what, rule_id=None):
            """Вход, на который ссылается запись. Синтетический id «rule:<rule_id>» (правило сработало по уже
            известному состоянию, edgectx gate.AddRule) допустим, если это правило уже срабатывало (или
            это сама запись rule_fired этого правила); у него нет вида — возвращается None без нарушения."""
            if iid is None:
                return None
            got = inputs.get(_key(iid))
            if got is None:
                if isinstance(iid, str) and iid.startswith("rule:"):
                    rid = iid[len("rule:"):]
                    if (rule_id is not None and str(rule_id) == rid) or _key(rid) in rule_ids:
                        return None
                self.add("ref.dangling", no, f"{what}: входа {iid!r} нет до этой записи", input_id=iid)
            return got

        day_recent: dict[int, deque] = defaultdict(deque)   # последние ответы суток: (вызов, токены)

        def lower_bound(day):
            """Нижняя оценка учтённого планировщиком к моменту допуска хода. При max_in_flight = k до
            k − 1 последних ответов других исполнителей могли быть записаны, но ещё не учтены (Charge
            вызывается после записи ответа) — их не считаем, чтобы не было ложных срабатываний."""
            calls, toks = day_calls[day], day_tokens[day]
            for c, t in list(day_recent[day])[-(self.max_in_flight - 1):] if self.max_in_flight > 1 else []:
                calls -= c
                toks -= t
            return calls, toks

        def budget_check(no, rec, w: _Wake):
            """Ход допущен (wake без suppressed): лимиты проверяются по нижней оценке израсходованного."""
            if not self.daily_calls and not self.daily_tokens:
                return
            if w.certainly_user or (w.priority is None and w.reason == "user"):
                return  # ход пользователя обслуживается всегда
            if w.priority == "user":
                return
            day = day_of(rec["ts_clock"])
            calls, toks = lower_bound(day)
            fc = calls / self.daily_calls if self.daily_calls else 0.0
            ft = toks / self.daily_tokens if self.daily_tokens else 0.0
            ref = {"wake_id": rec.get("wake_id", rec.get("turn_id"))}
            if fc >= 1:
                self.add("budget.daily_calls", no, f"фоновый ход ({w.reason}) допущен при {calls} учтённых вызовах "
                         f"за сутки {day} (лимит {self.daily_calls})", **ref)
            if ft >= 1:
                self.add("budget.daily_input_tokens", no, f"фоновый ход ({w.reason}) допущен при {toks} входных "
                         f"токенах за сутки {day} (лимит {self.daily_tokens})", **ref)
            if w.certainly_background and self.degrade_at and max(fc, ft) < 1 and max(fc, ft) >= self.degrade_at:
                self.add("budget.degraded", no, f"фоновый ход ({w.reason}) допущен при израсходованной доле "
                         f"{max(fc, ft):.3f} ≥ degrade_at {self.degrade_at}", **ref)

        def take_credit() -> tuple[_Wake | None, list[_Wake]]:
            """Позиционный режим: ход начат — израсходовать один wake (в порядке приоритета планировщика)."""
            alive = [w for w in credits if not w.used]
            if not alive:
                return None, []
            w = min(alive, key=lambda x: (x.rank, x.line))
            w.used = True
            return w, alive

        for no, rec in records:
            typ = rec["type"]
            stats[typ] += 1
            if typ == "error":
                errors_by_where[rec["where"]] += 1
            if typ == "run_end":
                ended = True
            if typ == "input":
                k = _key(rec["id"])
                if k in inputs:
                    self.add("input.duplicate_id", no, f"повтор id входа {rec['id']!r}", input_id=rec["id"])
                inputs[k] = (no, rec["kind"], wall_seconds(rec["ts_wall"]))
                if rec["kind"] == "user":
                    user_inputs.setdefault(k, no)
                dev, prop = rec.get("device"), rec.get("property")
                if rec["kind"] == "sensor" and isinstance(dev, str) and isinstance(prop, str) and "value" in rec:
                    self.readings.append(Reading(no, wall_seconds(rec["ts_wall"]), rec["ts_clock"], f"{dev}.{prop}",
                                                 rec["value"]))

            elif typ == "wake":
                ids = list(rec.get("input_ids") or [])
                if rec.get("input_id") is not None:
                    ids.append(rec["input_id"])
                seen_here = set()
                for iid in ids:
                    got = ref_input(no, iid, "wake")
                    if got is not None and got[1] not in WAKE_INPUT_KINDS[rec["reason"]]:
                        self.add("wake.reason_mismatch", no, f"wake reason={rec['reason']} для входа вида {got[1]}",
                                 input_id=iid)
                    k = _key(iid)
                    if got is None or rec["suppressed"] or k in seen_here:
                        continue
                    seen_here.add(k)
                    woke[k] += 1
                    if woke[k] == 2:
                        self.add("wake.duplicate_input", no, f"вход {iid!r} уже будил LLM (строка повторного wake)",
                                 input_id=iid)
                    if suppressed_n[k] and woke[k] == 1:
                        self.add("input.multi_outcome", no, f"вход {iid!r} был подавлен, а теперь будит LLM",
                                 input_id=iid)
                w = _Wake(no, rec["reason"], rec.get("priority"))
                link = next((rec[f] for f in LINK_FIELDS if f in rec), None)
                if rec["suppressed"]:
                    # Отклонённое пробуждение (edgectx: планировщик отказал по бюджету/очереди) — ход не начат.
                    wakes_by_reason["suppressed"] += 1
                else:
                    wakes_by_reason[rec["reason"]] += 1
                    wakes_seen += 1
                    credits.append(w)
                    budget_check(no, rec, w)
                    if link is not None:
                        admitted_wakes.setdefault(_key(link), no)
                if link is not None:
                    turns[_key(link)] = _Turn(w, rec["suppressed"])

            elif typ == "suppress":
                suppress_by_reason[rec["reason"]] += 1
                got = ref_input(no, rec["input_id"], "suppress")
                if got is not None:
                    k = _key(rec["input_id"])
                    suppressed_n[k] += 1
                    if suppressed_n[k] > 1 or woke[k]:
                        why = "подавлен повторно" if suppressed_n[k] > 1 else "уже будил LLM"
                        self.add("input.multi_outcome", no, f"вход {rec['input_id']!r} {why} ({rec['reason']})",
                                 input_id=rec["input_id"])
                if got is not None and got[1] == "user":
                    suppressed_users.add(_key(rec["input_id"]))
                    self.add("wake.user_suppressed", no, f"вход пользователя {rec['input_id']!r} подавлен ({rec['reason']})",
                             input_id=rec["input_id"])

            elif typ == "llm_request":
                k = _key(rec["req_id"])
                if k in requests:
                    self.add("llm.duplicate_req_id", no, f"req_id {rec['req_id']!r} уже был (строка {requests[k].line})",
                             req_id=rec["req_id"])
                    continue
                if len(outstanding) >= self.max_in_flight:
                    self.add("llm.inflight", no, f"в полёте уже {len(outstanding)} запрос(а) при max_in_flight="
                             f"{self.max_in_flight}", req_id=rec["req_id"])
                req = _Request(no, rec["ts_clock"], wall_seconds(rec["ts_wall"]), day_of(rec["ts_clock"]), None,
                               n_tools=rec["n_tools"], prompt_est=rec["prompt_tokens_est"])
                requests[k] = req
                seg = rec.get("segments_tokens_est")
                if isinstance(seg, dict) and seg:
                    total = sum(v for v in seg.values() if argschema.is_number(v))
                    est = rec["prompt_tokens_est"]
                    if abs(total - est) > self.o.segments_tolerance * est + 16:
                        segments_off.append(no)
                if k in early_responses:
                    self.add("llm.response_before_request", early_responses.pop(k),
                             f"llm_response для {rec['req_id']!r} записан раньше запроса (строка {no})", req_id=rec["req_id"])
                    req.responded = True
                else:
                    outstanding.add(k)
                max_outstanding = max(max_outstanding, len(outstanding))
                purpose = rec.get("purpose")
                if purpose is not None:
                    purposes[purpose] += 1
                if purpose in self.o.exempt_purposes:
                    req.exempt = True
                    continue
                link = next((rec[f] for f in LINK_FIELDS if f in rec), None)
                if link is not None:
                    linked_mode["linked"] += 1
                    turns_with_request.add(_key(link))
                    turn = turns.get(_key(link))
                    if turn is None:
                        self.add("wake.missing", no, f"llm_request ссылается на неизвестный ход {link!r}", req_id=rec["req_id"])
                        continue
                    if turn.suppressed:
                        self.add("wake.suppressed_flag", no, f"запрос в отклонённом ходе {link!r}", req_id=rec["req_id"])
                    req.turn = turn
                    turn.n_requests += 1
                    calls_by_reason[turn.wake.reason] += 1
                    if turn.n_requests == 1:
                        turn_starts_by_reason[turn.wake.reason] += 1
                        turn.wake.used = True
                    if self.max_steps is not None and turn.n_requests > self.max_steps:
                        self.add("budget.max_steps", no, f"ход {link!r}: {turn.n_requests}-й запрос при max_steps="
                                 f"{self.max_steps}", req_id=rec["req_id"])
                    for ck in turn.calls:
                        c = self.calls[ck]
                        if c.result_line is None:
                            self.add("tool.result_late", c.line, f"нет результата вызова {c.call_id!r} к следующему "
                                     f"запросу хода (строка {no})", call_id=c.call_id)
                    turn.calls = []
                    continue
                linked_mode["positional"] += 1
                if sequential:
                    if last_continuable and (self.max_steps is None or chain_len < self.max_steps):
                        chain_len += 1
                    else:
                        w, alive = take_credit()
                        if w is None:
                            if last_continuable and self.max_steps is not None and chain_len >= self.max_steps:
                                self.add("budget.max_steps", no, f"{chain_len + 1}-й запрос подряд при max_steps="
                                         f"{self.max_steps} и нет нового wake", req_id=rec["req_id"])
                            else:
                                self.add("wake.missing", no, "llm_request без предшествующего wake", req_id=rec["req_id"])
                        else:
                            turn_starts_by_reason[w.reason] += 1
                        chain_len = 1
                else:
                    if continuable_credits > 0:
                        continuable_credits -= 1
                    else:
                        w, alive = take_credit()
                        if w is None:
                            self.add("wake.missing", no, "llm_request без предшествующего wake", req_id=rec["req_id"])
                        else:
                            turn_starts_by_reason[w.reason] += 1
                last_continuable = False

            elif typ in ("llm_response", "error") and (typ == "llm_response" or "req_id" in rec):
                k = _key(rec["req_id"])
                req = requests.get(k)
                if req is None:
                    if typ == "llm_response":
                        if k in early_responses:
                            self.add("llm.duplicate_response", no, f"второй ответ на {rec['req_id']!r}", req_id=rec["req_id"])
                        early_responses[k] = no
                    continue
                if req.responded:
                    self.add("llm.duplicate_response", no, f"второй ответ на {rec['req_id']!r}", req_id=rec["req_id"])
                    continue
                req.responded = True
                outstanding.discard(k)
                # Сбой обращения: запись error с req_id или (edgectx, agent/turn.go) llm_response с полем error /
                # finish_reason=error и нулевым usage. В токены и суточный учёт не входит.
                if typ == "error" or rec.get("error") or rec.get("finish_reason") == "error":
                    stats["llm_failed"] += 1
                    if not req.exempt:
                        last_continuable = True      # после сбоя допустим повторный запрос того же хода
                        continuable_credits += 1
                    continue
                usage = rec["usage"]
                pt, ct, cached = usage["prompt_tokens"], usage["completion_tokens"], usage.get("cached_tokens")
                tokens["prompt"] += pt
                tokens["completion"] += ct
                tokens["cached"] += cached or 0
                if cached is not None and cached > pt:
                    self.add("usage.inconsistent", no, f"cached_tokens {cached} > prompt_tokens {pt}", req_id=rec["req_id"])
                n = rec["n_tool_calls"]
                if rec.get("has_usage") is False:
                    no_usage_lines.append(no)
                else:
                    est = req.prompt_est
                    if pt == 0 and est > 0:
                        self.add("usage.implausible", no, f"prompt_tokens = 0 при оценке запроса {est}", req_id=rec["req_id"])
                    elif est > 0:
                        usage_checked += 1
                        if abs(pt / est - 1) > self.o.usage_tolerance:
                            usage_off.append((no, pt / est))
                    if ct == 0 and n > 0:
                        self.add("usage.implausible", no, f"completion_tokens = 0 при {n} вызов(ах)", req_id=rec["req_id"])
                if req.day == day_of(rec["ts_clock"]):
                    day_calls[req.day] += 1
                    day_tokens[req.day] += pt
                    day_recent[req.day].append((1, pt))
                    if len(day_recent[req.day]) > 4:
                        day_recent[req.day].popleft()
                if rec["finish_reason"] == "tool_calls" and n == 0:
                    self.add("llm.finish_mismatch", no, "finish_reason=tool_calls при n_tool_calls=0", req_id=rec["req_id"])
                req.resp_clock = rec["ts_clock"]
                if n > 0 and (req.exempt or req.n_tools == 0):
                    # Провайдер вернул вызовы на запрос без инструментов (сводка истории): агент их не исполняет —
                    # это сбой провайдера/оракула, а не агента. Вызовов не ждём.
                    self.add("llm.tool_calls_without_tools", no, f"ответ на запрос {rec['req_id']!r} без инструментов "
                             f"(n_tools={req.n_tools}{', служебный' if req.exempt else ''}) объявил {n} вызов(ов)",
                             req_id=rec["req_id"])
                    n = 0
                if n > 0 and rec["finish_reason"] == "length":
                    req.truncated = True
                wall = wall_seconds(rec["ts_wall"])
                if wall is not None and req.ts_wall is not None and \
                        rec["latency_ms"] > (wall - req.ts_wall) * 1000 + self.o.latency_slack_ms:
                    self.add("time.latency_inconsistent", no, f"latency_ms {rec['latency_ms']} > "
                             f"{(wall - req.ts_wall) * 1000:.1f} мс между записями", req_id=rec["req_id"])
                expected_calls[k] = n
                llm_call_credits += n
                if n > 0:
                    last_calls_req = k
                if req.turn is not None:
                    req.turn.last_req = k
                if not req.exempt:
                    last_continuable = n > 0
                    if n > 0:
                        continuable_credits += 1

            elif typ == "tool_call":
                k = _key(rec["call_id"])
                if k in self.calls:
                    self.add("tool.duplicate_call_id", no, f"call_id {rec['call_id']!r} уже был (строка {self.calls[k].line})",
                             call_id=rec["call_id"])
                    continue
                tool_by_server[rec["server"]] += 1
                args = self._check_args(no, rec)
                info = CallInfo(no, rec["call_id"], rec["server"], rec["name"], args, rec["ts_clock"],
                                ts_wall=wall_seconds(rec["ts_wall"]))
                self.calls[k] = info
                source_req: str | None = None
                link = next((rec[f] for f in LINK_FIELDS if f in rec), None)
                rk_link = None
                if "req_id" not in rec and "rule_id" not in rec and rec.get("by") != "rule" and link is not None:
                    turn = turns.get(_key(link))
                    if turn is None or turn.last_req is None:
                        self.add("tool.unsourced", no, f"tool_call хода {link!r}, у которого ещё нет ответа LLM",
                                 call_id=rec["call_id"])
                    else:
                        rk_link = turn.last_req  # вызов относится к последнему ответу своего хода
                if rk_link is not None or "req_id" in rec:
                    rk = rk_link if rk_link is not None else _key(rec["req_id"])
                    req = requests.get(rk)
                    if req is None or rk not in expected_calls:
                        self.add("tool.unsourced", no, f"tool_call ссылается на req_id {rec.get('req_id')!r} без ответа LLM",
                                 call_id=rec["call_id"])
                    else:
                        got_calls[rk] += 1
                        info.source = "llm"
                        source_req = rk
                        llm_call_credits = max(0, llm_call_credits - 1)
                        if got_calls[rk] > expected_calls[rk]:
                            self.add("tool.count_mismatch", no, f"ответ {json.loads(rk)!r} объявил {expected_calls[rk]} "
                                     f"вызов(ов), а их больше", req_id=json.loads(rk), call_id=rec["call_id"])
                        if req.turn is not None:
                            req.turn.calls.append(k)
                elif link is not None and "rule_id" not in rec and rec.get("by") != "rule":
                    pass  # ход известен, но ответа с вызовами нет — уже отмечено tool.unsourced
                elif "rule_id" in rec:
                    info.source = "rule"
                    if _key(rec["rule_id"]) not in rule_ids:
                        self.add("tool.unsourced", no, f"tool_call ссылается на правило {rec['rule_id']!r}, "
                                 "которое не срабатывало", call_id=rec["call_id"])
                elif llm_call_credits > 0 and rec.get("by") != "rule":
                    llm_call_credits -= 1
                    info.source = "llm"
                    source_req = last_calls_req
                elif rules_seen > 0 and rec.get("by") != "llm":
                    info.source = "rule"
                else:
                    self.add("tool.unsourced", no, "tool_call без ответа LLM с вызовами и без сработавшего правила",
                             call_id=rec["call_id"])
                src = requests.get(source_req) if source_req is not None else None
                if src is not None:
                    info.issued_clock = src.resp_clock
                    if src.truncated and not src.truncated_reported:
                        src.truncated_reported = True
                        self.add("llm.truncated_calls", no, f"исполняется вызов из ответа {json.loads(source_req)!r}, "
                                 "обрезанного по длине (finish_reason=length)", call_id=rec["call_id"])
                if k in early_results:
                    self.add("tool.result_before_call", early_results.pop(k),
                             f"результат {rec['call_id']!r} записан раньше вызова (строка {no})", call_id=rec["call_id"])
                    info.result_line = -1

            elif typ == "tool_result" or (typ == "error" and "call_id" in rec):
                k = _key(rec["call_id"])
                info = self.calls.get(k)
                if info is None:
                    if typ == "tool_result":
                        if k in early_results:
                            self.add("tool.duplicate_result", no, f"второй результат {rec['call_id']!r}", call_id=rec["call_id"])
                        early_results[k] = no
                    continue
                if info.result_line is not None:
                    self.add("tool.duplicate_result", no, f"второй результат {rec['call_id']!r}", call_id=rec["call_id"])
                    continue
                info.result_line = no
                info.result_ts_wall = wall_seconds(rec["ts_wall"])
                if typ == "tool_result":
                    info.ok, info.denied = rec["ok"], rec["denied"]
                    if rec["ok"] and rec["denied"]:
                        self.add("tool.ok_and_denied", no, f"результат {rec['call_id']!r}: ok=true и denied=true",
                                 call_id=rec["call_id"])
                else:
                    info.ok, info.denied = False, False

            elif typ == "user_reply":
                k = _key(rec["input_id"])
                got = ref_input(no, rec["input_id"], "user_reply")
                if got is None:
                    continue
                if got[1] != "user":
                    self.add("user.reply_to_non_user", no, f"ответ на вход вида {got[1]}", input_id=rec["input_id"])
                replies[k] += 1
                if replies[k] > 1:
                    self.add("user.duplicate_reply", no, f"второй ответ на {rec['input_id']!r}", input_id=rec["input_id"])
                wall = wall_seconds(rec["ts_wall"])
                if wall is not None and got[2] is not None and \
                        rec["latency_ms"] > (wall - got[2]) * 1000 + self.o.latency_slack_ms:
                    self.add("time.latency_inconsistent", no, f"latency_ms {rec['latency_ms']} > "
                             f"{(wall - got[2]) * 1000:.1f} мс от входа", input_id=rec["input_id"])

            elif typ == "deferred":
                for iid in rec["input_ids"]:
                    ref_input(no, iid, "deferred")

            elif typ == "rule_fired":
                rules_seen += 1
                rule_ids.add(_key(rec["rule_id"]))
                ref_input(no, rec.get("input_id"), "rule_fired", rule_id=rec["rule_id"])

            elif typ == "notify":
                if wakes_seen == 0 and rules_seen == 0:
                    self.add("notify.unsourced", no, "notify без предшествующего wake или rule_fired")

        # --- конец трассы
        for k in sorted(outstanding, key=lambda x: requests[x].line):
            self.add("llm.missing_response", requests[k].line, "нет llm_response до конца трассы",
                     req_id=json.loads(k))
        for k, line in early_responses.items():
            self.add("llm.orphan_response", line, f"нет llm_request с req_id {json.loads(k)!r}", req_id=json.loads(k))
        for info in self.calls.values():
            if info.result_line is None:
                self.add("tool.missing_result", info.line, f"нет tool_result для {info.call_id!r}", call_id=info.call_id)
        for k, line in early_results.items():
            self.add("tool.orphan_result", line, f"нет tool_call с call_id {json.loads(k)!r}", call_id=json.loads(k))
        for k, line in user_inputs.items():
            if replies[k] == 0 and k not in suppressed_users:
                self.add("user.unanswered", line, f"нет user_reply на вход {json.loads(k)!r}", input_id=json.loads(k))
        # Исход каждого входа (инвариант edgectx, agent/tests/tracecheck_test.go): ровно один suppress или участие
        # ровно в одном допущенном wake. Потерянный вход — потерянная реакция; лишний wake — лишний вызов LLM.
        outcomes = Counter()
        if ended:
            for k, (line, kind, _) in inputs.items():
                if woke[k] == 0 and suppressed_n[k] == 0:
                    outcomes["none"] += 1
                    self.add("input.no_outcome", line, f"вход {json.loads(k)!r} ({kind}) не подавлен и не будил LLM",
                             input_id=json.loads(k))
                else:
                    outcomes["woke" if woke[k] else "suppressed"] += 1
            for k, line in admitted_wakes.items():
                if k not in turns_with_request:
                    self.add("wake.no_request", line, f"допущенное пробуждение {json.loads(k)!r} без llm_request",
                             wake_id=json.loads(k))
        else:
            self.r.skip("input.no_outcome / wake.no_request: трасса не закончена run_end — исходы не окончательны")
        if no_usage_lines:
            self.add("llm.no_usage", no_usage_lines[0], f"{len(no_usage_lines)} ответ(ов) без usage (has_usage=false): "
                     "токены и ₽ прогона занижены")
        if usage_off:
            ratios = sorted(r for _, r in usage_off)
            self.add("llm.usage_mismatch", usage_off[0][0], f"{len(usage_off)} из {usage_checked} ответов: "
                     f"prompt_tokens/оценка вне 1 ± {self.o.usage_tolerance} (медиана {ratios[len(ratios) // 2]:.3f})")
        if segments_off:
            self.add("llm.segments_mismatch", segments_off[0], f"{len(segments_off)} запрос(ов): Σ segments_tokens_est "
                     f"отличается от prompt_tokens_est больше чем на {self.o.segments_tolerance:.0%}")
        n_records = max(1, sum(stats.values()))
        for where_, n in sorted(errors_by_where.items()):
            if n >= self.o.errors_min and n >= self.o.errors_ratio * n_records:
                self.add("errors.frequent", None, f"{n} записей error where={where_!r} ({n / n_records:.1%} трассы): "
                         "подсистема работает вслепую, а прогон выглядит успешным")
        linked_calls = sum(got_calls.values())
        if linked_calls:  # вызовы несут req_id — сверяется каждый ответ
            for rk, n in expected_calls.items():
                if got_calls[rk] < n:
                    self.add("tool.count_mismatch", requests[rk].line, f"ответ объявил {n} вызов(ов), получено "
                             f"{got_calls[rk]}", req_id=json.loads(rk))
        elif llm_call_credits > 0:
            self.add("tool.count_mismatch", None, f"ответы LLM объявили на {llm_call_credits} вызов(ов) больше, "
                     "чем записано tool_call")
        if self.max_steps is not None and sequential is False and linked_mode["positional"]:
            self.r.skip("budget.max_steps (позиционный режим): при max_in_flight > 1 ходы перемежаются — "
                        "проверяется только в связанном режиме (turn_id)")

        mode = "linked" if linked_mode["linked"] and not linked_mode["positional"] else (
            "positional" if not linked_mode["linked"] else "mixed")
        self.summary = {
            "records": dict(sorted(stats.items())),
            "attribution": mode,
            "llm_calls": stats["llm_request"],
            "llm_calls_by_wake_reason": dict(sorted(calls_by_reason.items())) if mode == "linked" else None,
            "llm_turns_started_by_reason": dict(sorted(turn_starts_by_reason.items())),
            "llm_failed": stats["llm_failed"],
            "llm_no_usage": len(no_usage_lines),
            "usage_ratio_median": (lambda xs: xs[len(xs) // 2] if xs else None)(sorted(
                r for _, r in usage_off)) if usage_off else None,
            "errors_by_where": dict(sorted(errors_by_where.items())),
            "inputs_by_outcome": dict(sorted(outcomes.items())),
            "llm_purposes": dict(sorted(purposes.items())),
            "tokens": {"prompt": tokens["prompt"], "completion": tokens["completion"], "cached": tokens["cached"]},
            "per_day": {str(d): {"llm_calls_completed": day_calls[d], "prompt_tokens": day_tokens[d]}
                        for d in sorted(day_calls)},
            "max_in_flight_observed": max_outstanding,
            "wakes_by_reason": dict(sorted(wakes_by_reason.items())),
            "suppressed_by_reason": dict(sorted(suppress_by_reason.items())),
            "tool_calls_by_server": dict(sorted(tool_by_server.items())),
            "tool_denied": sum(1 for c in self.calls.values() if c.denied),
            "user_inputs": len(user_inputs),
            "user_replies": sum(replies.values()),
            "user_suppressed": len(suppressed_users),
            "rules_fired": rules_seen,
            "notifies": stats["notify"],
            "settings": {"max_steps": self.max_steps, "max_in_flight": self.max_in_flight,
                         "daily_calls": self.daily_calls, "daily_input_tokens": self.daily_tokens,
                         "degrade_at": self.degrade_at},
        }
        return

    # --- аргументы ----------------------------------------------------------------------------------
    def _check_args(self, no, rec) -> dict | None:
        raw = rec["args"]
        cid = rec["call_id"]
        if isinstance(raw, str):
            try:
                args = loads_strict(raw)
            except ValueError as e:
                cls = "args.duplicate_key" if "повтор ключа" in str(e) else "args.invalid_json"
                self.add(cls, no, f"{rec['name']}: {e}", call_id=cid)
                return None
        else:
            args = raw
        if not isinstance(args, dict):
            self.add("args.not_object", no, f"{rec['name']}: args — {argschema.json_type_name(args)}", call_id=cid)
            return None
        if self.catalog is None:
            return args
        schema, why = self.catalog.lookup(rec["server"], rec["name"])
        if schema is None:
            self.add("args.unknown_tool", no, why, call_id=cid, tool=rec["name"])
            return args
        for e in argschema.validate(args, schema, strict_integer=self.o.strict_integer):
            self.add(f"args.{e.kind}", no, f"{rec['name']}{e.path}: {e.message}", call_id=cid, tool=rec["name"])
        return args
