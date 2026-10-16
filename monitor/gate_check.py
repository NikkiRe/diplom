"""Класс actuator_safety: независимая перепроверка решений шлюза приводов hw-mcp по его журналу и трассе edgectx.

Монитор не доверяет вердиктам шлюза: по объявленной политике и входам решения (команда, время, состояние) он заново
вычисляет, допустима ли исполненная команда, по тем же шагам, что и шлюз (THESIS_CONCEPT §10; прототип
GAP3_artifacts/actuator_gate.py, порт hwmcp/internal/gate):
  1) реестр; 2) схема и диапазон; 3) TTL решения LLM; 4) свежесть состояния (неизвестное = опасное);
  5) блокировки (с clamp); 6) частота и идемпотентность; 7) подтверждение; плюс аренда, сторожевой таймер, импульсы.
Если исполненная команда нарушает любой шаг — нарушение actuator_safety.* и +1 к N_unsafe (одна команда — один раз).

Независимость от шлюза. Журнал пишет сам шлюз, поэтому только по нему нельзя поймать шлюз, который записал не те
аргументы или не тот снимок состояния. Поэтому при наличии трассы edgectx:
  * аргументы и цель каждой команды сравниваются с tool_call трассы (что агент на самом деле отправил) —
    actuator_safety.args_mismatch;
  * показания датчиков, которые видел агент (input kind=sensor), новее снимка шлюза подмешиваются в состояние
    блокировок — команда, опасная по ним, — actuator_safety.interlock;
  * возраст решения LLM считается по трассе (ts_clock ответа LLM → ts_clock вызова), а не по issued_at шлюза
    (edgectx пока не передаёт _meta edgectx/issued_at, и hw-mcp берёт момент получения);
  * схемы аргументов проверяются двумя реализациями: своей (monitor/argschema.py, с которой портирован валидатор
    hw-mcp — общий режим отказа) и сторонней jsonschema (Draft 2020-12), если пакет установлен.
Реестр инструментов hw-mcp строится из инвентаря, а не из политики: монитор выводит его из declared.json (выходы
GPIO: set_<свойство> {"value": boolean} и pulse {"seconds": (0, 3600]}, hwmcp/README «Инструменты шлюза»).

Форматы журнала (оба принимаются):
  * родной формат hw-mcp (hwmcp/README «Журнал»): run_start{gate, devices, declared}, ready{gate_devices, …},
    gate{cmd_id, device, tool, origin, args, issued_at, ttl_s, confirmed, dry_run, verdict, reason, final_args,
    state: {ключ: {"v", "age_s"}}, executed, lease_until, pulse_until}, lease_renew, lease_expired, pulse_end,
    shutdown_safe_state; служебные output_init, notify, sample_error, sample_recovered, sensor_rebound,
    line_error, run_end пропускаются;
  * прежний формат, предложенный монитором (синтетические прогоны tracegen): gate_config, state, gate_decision
    {…, state: {ключ: {"value", "ts"}}, call_id, mcp_tool, mcp_args}, lease{op}, safe_state.
"""
from __future__ import annotations

import bisect
import json
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from monitor import argschema
from monitor.jsonl import Line, loads_strict
from monitor.report import Report
from monitor.trace_check import CallInfo, Reading, _key

try:  # независимая сторонняя реализация JSON Schema (ставится вместе с mcp); без неё — только argschema
    import jsonschema as _jsonschema

    # Шлюз (и argschema в строгом режиме) принимает как integer только целые литералы, а JSON Schema — и 1.0.
    # Это объявленное различие семантики, а не ошибка реализации: сравниваем реализации на одной семантике.
    _StrictValidator = _jsonschema.validators.extend(
        _jsonschema.Draft202012Validator,
        type_checker=_jsonschema.Draft202012Validator.TYPE_CHECKER.redefine(
            "integer", lambda _checker, x: isinstance(x, int) and not isinstance(x, bool)))
except ImportError:  # pragma: no cover - зависит от окружения
    _jsonschema = None

SOURCE = "hw_log"
VERDICTS = ("allow", "clamp", "confirm", "duplicate", "reject")
EXECUTING = ("allow", "clamp")
LEASE_OPS = ("grant", "acquire", "renew", "release", "expire")
EPS = 1e-9
DEFAULT_ACTUATOR_TOOLS = r"^(set_.+|write_property|invoke_action)$"
MAX_PULSE_S = 3600.0
# Служебные записи журнала hw-mcp, не влияющие на безопасность команд.
INFO_TYPES = frozenset({"output_init", "notify", "sample_error", "sample_recovered", "sensor_rebound", "line_error",
                        "run_end"})
GENERIC_ORIGINS = ("write_property", "invoke_action")
NON_ACTUATOR_GENERIC = frozenset({"read_property", "list_devices", "board_health"})


# ---------------------------------------------------------------------------------------------------------
# Политика шлюза

class PolicyError(ValueError):
    pass


@dataclass
class Cond:
    """Условие блокировки: {"arg"|"state": имя, op: значение} | {"all"|"any": [...]} | {"not": {...}}."""
    kind: str                      # arg | state | all | any | not
    name: str = ""
    op: str = ""
    value: object = None
    subs: list = field(default_factory=list)

    OPS = ("eq", "ne", "lt", "le", "gt", "ge")

    @classmethod
    def parse(cls, obj) -> "Cond":
        if not isinstance(obj, dict):
            raise PolicyError(f"условие должно быть объектом, а не {argschema.json_type_name(obj)}")
        for comb in ("all", "any"):
            if comb in obj:
                if len(obj) != 1 or not isinstance(obj[comb], list) or not obj[comb]:
                    raise PolicyError(f"{comb!r}: единственный ключ с непустым массивом")
                return cls(comb, subs=[cls.parse(x) for x in obj[comb]])
        if "not" in obj:
            if len(obj) != 1:
                raise PolicyError("'not' должен быть единственным ключом")
            return cls("not", subs=[cls.parse(obj["not"])])
        src = [k for k in ("arg", "state") if k in obj]
        ops = [k for k in obj if k in cls.OPS]
        extra = [k for k in obj if k not in ("arg", "state") and k not in cls.OPS]
        if len(src) != 1 or len(ops) != 1 or extra:
            raise PolicyError(f"условие {obj!r}: нужен ровно один из arg/state и один оператор {cls.OPS}")
        name = obj[src[0]]
        if not isinstance(name, str) or not name:
            raise PolicyError(f"{src[0]!r}: непустая строка")
        op = ops[0]
        if op not in ("eq", "ne") and not argschema.is_number(obj[op]):
            raise PolicyError(f"оператор {op} требует число")
        return cls(src[0], name, op, obj[op])

    def state_keys(self, into: set) -> set:
        if self.kind == "state":
            into.add(self.name)
        for s in self.subs:
            s.state_keys(into)
        return into

    def eval(self, args: dict, snap: dict) -> bool:
        """Истинно — условие отказа выполнено. Сравнение не-числа (lt/le/gt/ge) — ошибка вычисления, которая
        трактуется как выполненное условие (fail-closed, как в шлюзе). Отсутствующий аргумент — ложь."""
        if self.kind == "all":
            return all(s.eval(args, snap) for s in self.subs)
        if self.kind == "any":
            return any(s.eval(args, snap) for s in self.subs)
        if self.kind == "not":
            return not self.subs[0].eval(args, snap)
        src = args if self.kind == "arg" else snap
        if not isinstance(src, dict) or self.name not in src:
            return False
        v = src[self.name]
        if self.op == "eq":
            return argschema.json_equal(v, self.value)
        if self.op == "ne":
            return not argschema.json_equal(v, self.value)
        if not argschema.is_number(v):
            return True  # fail-closed
        return {"lt": v < self.value, "le": v <= self.value, "gt": v > self.value, "ge": v >= self.value}[self.op]


@dataclass
class Interlock:
    name: str
    device: str
    tools: frozenset | None        # None — любой инструмент устройства ("*")
    needs: tuple
    deny: list                     # [(Cond, reason)]
    clamp: dict                    # аргумент -> {"set"|"min"|"max": значение}

    def applies(self, device: str, tool: str) -> bool:
        return device == self.device and (self.tools is None or tool in self.tools)


@dataclass
class Device:
    tools: dict = field(default_factory=dict)     # инструмент -> JSON Schema аргументов
    tools_known: bool = True                      # False — инструменты устройства неизвестны (реестр не выведен)
    read_only: frozenset = frozenset()
    min_interval_s: float = 0.0
    max_per_min: int | None = None
    requires_confirm: frozenset = frozenset()
    lease_s: float | None = None
    lease_required: bool = False                  # нужна явная аренда (acquire) до команды
    safe_state: dict | None = None


@dataclass
class Policy:
    default_ttl_s: float = 10.0
    max_state_age_s: float = 5.0
    commit_window_s: float = 5.0
    devices: dict = field(default_factory=dict)
    interlocks: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @classmethod
    def parse(cls, obj) -> "Policy":
        """Политика в формате раздела "gate" declared.json (hwmcp/internal/gate/config.go); допускается и
        сам declared.json с ключом "gate"."""
        if isinstance(obj, dict) and isinstance(obj.get("gate"), dict):
            obj = obj["gate"]
        if not isinstance(obj, dict):
            raise PolicyError("политика должна быть объектом")
        p = cls(raw=obj)
        known = {"default_ttl_s", "max_state_age_s", "commit_window_s", "idem_capacity", "devices", "interlocks",
                 "watchdog_tick_s"}
        unknown = set(obj) - known
        if unknown:
            raise PolicyError(f"неизвестные ключи политики: {sorted(unknown)}")
        for k in ("default_ttl_s", "max_state_age_s", "commit_window_s"):
            if obj.get(k) is not None:
                if not argschema.is_number(obj[k]) or obj[k] < 0:
                    raise PolicyError(f"{k}: неотрицательное число")
                setattr(p, k, float(obj[k]))
        for name, d in (obj.get("devices") or {}).items():
            p.devices[name] = cls._device(name, d)
        for i, il in enumerate(obj.get("interlocks") or []):
            p.interlocks.append(cls._interlock(i, il))
        return p

    @staticmethod
    def _device(name, d) -> Device:
        if not isinstance(d, dict):
            raise PolicyError(f"devices.{name}: объект")
        dev = Device()
        tools = d.get("tools") or {}
        if not isinstance(tools, dict):
            raise PolicyError(f"devices.{name}.tools: объект инструмент → схема")
        for tool, schema in tools.items():
            chk = argschema.check_schema(schema, f"devices.{name}.tools.{tool}")
            if chk.problems:
                raise PolicyError("; ".join(chk.problems))
            dev.tools[tool] = schema
        # В hw-mcp инструменты строятся из инвентаря, а раздел devices задаёт только политику (без "tools").
        dev.tools_known = bool(dev.tools)
        dev.read_only = frozenset(d.get("read_only") or ())
        dev.requires_confirm = frozenset(d.get("requires_confirm") or ())
        dev.min_interval_s = float(d.get("min_interval_s") or 0.0)
        mpm = d.get("max_per_min")
        if mpm is not None and (not isinstance(mpm, int) or isinstance(mpm, bool) or mpm < 1):
            raise PolicyError(f"devices.{name}.max_per_min: целое ≥ 1")
        dev.max_per_min = mpm
        lease = d.get("lease_s")
        if lease is not None and (not argschema.is_number(lease) or lease <= 0):
            raise PolicyError(f"devices.{name}.lease_s: положительное число")
        dev.lease_s = float(lease) if lease is not None else None
        dev.lease_required = bool(d.get("lease_required", False))
        dev.safe_state = d.get("safe_state")
        return dev

    @staticmethod
    def _interlock(i, il) -> Interlock:
        if not isinstance(il, dict) or not isinstance(il.get("name"), str) or not isinstance(il.get("device"), str):
            raise PolicyError(f"interlocks[{i}]: нужны name и device")
        tool = il.get("tool", "*")
        if tool == "*":
            tools = None
        elif isinstance(tool, str) and tool:
            tools = frozenset([tool])
        elif isinstance(tool, list) and tool and all(isinstance(t, str) and t and t != "*" for t in tool):
            tools = frozenset(tool)
        else:
            raise PolicyError(f"interlocks[{i}].tool: имя, список имён или \"*\"")
        deny = []
        for j, rule in enumerate(il.get("deny") or []):
            if not isinstance(rule, dict) or "if" not in rule:
                raise PolicyError(f"interlocks[{i}].deny[{j}]: нужен if")
            deny.append((Cond.parse(rule["if"]), str(rule.get("reason", ""))))
        if not deny:
            raise PolicyError(f"interlocks[{i}]: пустой deny")
        needs = il.get("needs")
        if not needs:
            keys: set = set()
            for c, _ in deny:
                c.state_keys(keys)
            needs = sorted(keys)
        clamp = il.get("clamp") or {}
        if not isinstance(clamp, dict):
            raise PolicyError(f"interlocks[{i}].clamp: объект")
        return Interlock(il["name"], il["device"], tools, tuple(needs), deny, clamp)

    def interlocks_for(self, device: str, tool: str) -> list:
        return [il for il in self.interlocks if il.applies(device, tool)]


def registry_from_declared(declared: dict) -> tuple[dict, dict]:
    """Инструменты шлюза hw-mcp по declared.json — независимо от кода hw-mcp, по его контракту (hwmcp/README):
    выход GPIO со свойством P (по умолчанию "state", логическое) даёт set_P {"value": boolean} и действие
    pulse {"seconds": 0 < s ≤ 3600}; requires_confirmation: true — подтверждение для обоих. Возвращает
    ({устройство: {инструмент: схема}}, {устройство: {инструменты с подтверждением}})."""
    tools: dict = {}
    confirm: dict = {}
    for g in declared.get("gpio") or []:
        if not isinstance(g, dict) or g.get("direction") != "output" or not isinstance(g.get("id"), str):
            continue
        prop = g.get("property") or "state"
        tools[g["id"]] = {
            f"set_{prop}": {"type": "object", "properties": {"value": {"type": "boolean"}}, "required": ["value"],
                            "additionalProperties": False},
            "pulse": {"type": "object", "properties": {"seconds": {"type": "number", "exclusiveMinimum": 0,
                                                                   "maximum": MAX_PULSE_S}},
                      "required": ["seconds"], "additionalProperties": False},
        }
        if g.get("requires_confirmation"):
            confirm[g["id"]] = {f"set_{prop}", "pulse"}
    return tools, confirm


# ---------------------------------------------------------------------------------------------------------
# Записи журнала

_NUM = {"type": "number"}
_OPT_NUM = {"type": ["number", "null"]}
RECORD_SCHEMAS = {
    "gate_config": {"required": ["config"], "properties": {"config": {"type": "object"}, "watchdog_tick_s": _NUM}},
    "hw_run_start": {"properties": {"gate": {"type": ["object", "null"]}, "devices": {"type": "array"},
                                    "declared": {"type": ["string", "null"]}}},
    "ready": {"properties": {"gate_devices": {"type": "array", "items": {"type": "string"}},
                             "gate_tools": {"type": "object"}, "default_ttl_s": _NUM, "max_state_age_s": _NUM,
                             "commit_window_s": _NUM}},
    "state": {"required": ["key", "value"], "properties": {"key": {"type": "string", "minLength": 1}, "ts": _NUM}},
    "gate_decision": {"required": ["cmd_id", "device", "tool", "args", "issued_at", "verdict"], "properties": {
        "cmd_id": {"type": ["string", "integer"]}, "device": {"type": "string"}, "tool": {"type": "string"},
        "args": {"type": "object"}, "issued_at": _NUM, "ttl_s": _OPT_NUM,
        "confirmed": {"type": "boolean"}, "verdict": {"enum": list(VERDICTS)}, "reason": {"type": ["string", "null"]},
        "final_args": {"type": ["object", "null"]}, "executed": {"type": "boolean"}, "dry_run": {"type": "boolean"},
        "state": {"type": "object", "additionalProperties": {"type": "object", "required": ["value", "ts"],
                                                             "properties": {"ts": _NUM}}},
        "lease_until": _OPT_NUM, "pulse_until": _OPT_NUM, "call_id": {"type": ["string", "integer", "null"]},
        "origin": {"type": ["string", "null"]}, "mcp_tool": {"type": ["string", "null"]},
        "mcp_args": {"type": ["object", "null"]}}},
    "lease": {"required": ["device", "op"], "properties": {
        "device": {"type": "string"}, "op": {"enum": list(LEASE_OPS)}, "ok": {"type": "boolean"},
        "expires_at": _OPT_NUM}},
    "safe_state": {"required": ["device"], "properties": {"device": {"type": "string"}, "ok": {"type": "boolean"}}},
    "pulse_end": {"required": ["device"], "properties": {"device": {"type": "string"}, "scheduled": _OPT_NUM,
                                                         "ok": {"type": "boolean"}}},
}
ALIASES = {"decision": "gate_decision", "gate": "gate_decision", "config": "gate_config"}


def _normalize(rec: dict) -> dict:
    """Привести запись к внутреннему виду: прежний плоский/вложенный формат или родной формат hw-mcp."""
    rec = dict(rec)
    typ = ALIASES.get(rec.get("type"), rec.get("type"))
    rec["type"] = typ
    cmd = rec.pop("command", None)
    if isinstance(cmd, dict):
        for k, v in cmd.items():
            rec.setdefault(k, v)
    dec = rec.pop("decision", None)
    if isinstance(dec, dict):
        for k in ("verdict", "reason", "detail"):
            if k in dec:
                rec.setdefault(k, dec[k])
        if "args" in dec:
            rec.setdefault("final_args", dec["args"])
    if rec.get("ts_clock") is None and argschema.is_number(rec.get("t")):
        rec["ts_clock"] = rec["t"]
    now = rec.get("ts_clock")
    if typ == "gate_decision":
        st = rec.get("state")
        # hw-mcp: {ключ: {"v": значение, "age_s": возраст на момент решения}} -> {ключ: {"value", "ts"}} в его часах
        if isinstance(st, dict) and st and argschema.is_number(now) and all(
                isinstance(v, dict) and "v" in v and argschema.is_number(v.get("age_s")) for v in st.values()):
            rec["state"] = {k: {"value": v["v"], "ts": now - v["age_s"]} for k, v in st.items()}
    elif typ == "run_start":
        rec["type"] = "hw_run_start"
    elif typ == "lease_renew":
        rec = {"type": "lease", "op": "renew", "ts_clock": now, "ts_wall": rec.get("ts_wall"),
               "device": rec.get("device"), "ok": bool(rec.get("renewed")), "expires_at": rec.get("lease_until"),
               "cmd_id": rec.get("cmd_id"), "origin": rec.get("origin"), "verdict": rec.get("verdict"),
               "reason": rec.get("reason"), "native": True}
    elif typ in ("lease_expired", "shutdown_safe_state"):
        rec = {"type": "safe_state", "ts_clock": now, "ts_wall": rec.get("ts_wall"), "device": rec.get("device"),
               "cause": "lease_expired" if typ == "lease_expired" else "shutdown", "ok": rec.get("ok", True),
               "error": rec.get("error")}
    return rec


@dataclass
class _Decision:
    line: int
    rec: dict
    now: float
    executed: bool
    matched: bool = False
    wall: float | None = None
    renew: bool = False           # продление аренды (lease_renew), не решение шлюза


@dataclass
class GateOptions:
    watchdog_tolerance_s: float | None = None   # None — из gate_config.watchdog_tick_s, иначе 1 с
    hw_servers: frozenset = frozenset({"hw", "hw-mcp", "hwmcp"})
    actuator_tools: str = DEFAULT_ACTUATOR_TOOLS
    match_slack_s: float = 1.0                  # окно сопоставления tool_call ↔ решение по ts_wall, с
    read_declared_path: bool = True             # читать declared.json по пути из run_start.declared журнала
    pulse_tolerance_s: float | None = None      # допуск конца импульса; None — как сторожевой тик


class GateChecker:
    def __init__(self, report: Report, declared: dict | None = None, opts: GateOptions | None = None):
        self.r = report
        self.declared = declared
        self.o = opts or GateOptions()
        self.policy: Policy | None = None
        self.decisions: list[_Decision] = []
        self.native = False
        self.known_devices: set[str] = set()
        self.explicit_names: dict[str, tuple[str, str]] = {}   # explicit-имя действия -> (устройство, инструмент)
        self.stats = Counter()
        self._validators: dict[str, object] = {}

    def add(self, cls, line, msg, **kw):
        self.r.add(cls, SOURCE, line, msg, **kw)

    # ------------------------------------------------------------------------------------------------
    def check(self, lines: list[Line], trace_calls: list[CallInfo] | None = None,
              trace_readings: list[Reading] | None = None) -> dict:
        records = []
        unknown_types = set()
        info = Counter()
        for ln in lines:
            if ln.error is not None:
                self.add("parse", ln.no, ln.error)
                continue
            rec = _normalize(ln.obj)
            typ = rec.get("type")
            if typ in INFO_TYPES:
                info[typ] += 1
                continue
            schema = RECORD_SCHEMAS.get(typ)
            if schema is None:
                if typ not in unknown_types:
                    unknown_types.add(typ)
                    self.add("gate.unknown_record", ln.no, f"тип {typ!r} не используется монитором")
                continue
            errs = argschema.validate(rec, {"type": "object", "required": ["ts_clock"],
                                            "properties": {"ts_clock": _NUM}, **schema})
            if errs:
                self.add("event.field", ln.no, f"{typ}: " + "; ".join(f"{e.path or '/'}: {e.message}" for e in errs[:4]))
                continue
            records.append((ln.no, rec))
        self._load_policy(records)
        tol = self.o.watchdog_tolerance_s
        if tol is None:
            cfg = next((rec for _, rec in records if rec["type"] == "gate_config"), None)
            tol = float(cfg.get("watchdog_tick_s", 1.0)) if cfg and argschema.is_number(cfg.get("watchdog_tick_s")) else 1.0
            self.r.assumptions.setdefault("watchdog_tolerance_s", tol)
        self.tol = tol
        self.pulse_tol = self.o.pulse_tolerance_s if self.o.pulse_tolerance_s is not None else tol
        stats = self._replay(records)
        stats["info_records"] = dict(sorted(info.items()))
        stats["validators"] = ["argschema"] + (["jsonschema"] if _jsonschema is not None else [])
        if trace_calls is not None:
            stats["cross_check"] = self._cross_check(trace_calls, trace_readings or [])
        else:
            self.r.skip("actuator_safety.unchecked_call/result_mismatch/args_mismatch: нет трассы edgectx для сверки")
        if self.stats["ttl_log_vacuous"] and not self.stats["ttl_by_trace"]:
            self.r.skip("actuator_safety.stale_command: issued_at в журнале — момент получения (edgectx не передаёт "
                        "_meta edgectx/issued_at), а трассы для возраста решения нет — TTL не проверен")
        return stats

    # --- политика -----------------------------------------------------------------------------------
    def _load_policy(self, records):
        logged = next(((no, rec) for no, rec in records if rec["type"] == "gate_config"), None)
        run_start = next(((no, rec) for no, rec in records if rec["type"] == "hw_run_start"), None)
        ready = next((rec for _, rec in records if rec["type"] == "ready"), None)
        self.native = run_start is not None or ready is not None
        p_log = p_file = None
        src_line = None
        if logged is not None:
            src_line = logged[0]
            try:
                p_log = Policy.parse(logged[1]["config"])
            except PolicyError as e:
                self.add("gate.config_invalid", logged[0], f"gate_config: {e}")
        elif run_start is not None:
            # hw-mcp: политика — раздел gate declared.json в run_start (null — политики нет, действуют умолчания
            # шлюза, а реестр всё равно строится из инвентаря).
            src_line = run_start[0]
            try:
                p_log = Policy.parse(run_start[1].get("gate") or {})
            except PolicyError as e:
                self.add("gate.config_invalid", run_start[0], f"run_start.gate: {e}")
        if self.declared is not None:
            try:
                p_file = Policy.parse(self.declared)
            except PolicyError as e:
                self.r.add("gate.config_invalid", "gate_config", None, f"--gate-config: {e}")
        if p_log is not None and p_file is not None:
            self._compare(src_line, p_log, p_file)
        self.policy = p_file or p_log
        self.r.assumptions["gate_policy_source"] = ("--gate-config" if p_file else "gate_config" if logged and p_log
                                                    else "run_start.gate" if p_log else None)
        if self.policy is None:
            self.add("gate.config_missing", None, "нет политики шлюза: реестр, схемы, TTL, свежесть, блокировки, "
                     "частота и подтверждение не перепроверяются")
            return
        if ready is not None:
            for k in ("default_ttl_s", "max_state_age_s", "commit_window_s"):
                if argschema.is_number(ready.get(k)) and abs(ready[k] - getattr(self.policy, k)) > EPS:
                    self.add("gate.config_mismatch", None, f"{k}: шлюз (ready) {ready[k]} ≠ политика "
                             f"{getattr(self.policy, k)}")
        self._build_registry(run_start[1] if run_start else None, ready)

    def _full_declared(self, run_start):
        """declared.json целиком (нужен раздел gpio для реестра): --gate-config, иначе файл по пути из run_start."""
        if isinstance(self.declared, dict) and "gpio" in self.declared:
            self.r.assumptions["registry_source"] = "--gate-config (gpio)"
            return self.declared
        path = run_start.get("declared") if run_start else None
        if self.o.read_declared_path and isinstance(path, str) and path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    obj = loads_strict(f.read())
            except (OSError, ValueError):
                return None
            if isinstance(obj, dict):
                self.r.assumptions["registry_source"] = f"run_start.declared: {path}"
                return obj
        return None

    def _build_registry(self, run_start, ready):
        p = self.policy
        derived, confirm = {}, {}
        full = self._full_declared(run_start)
        if full is not None:
            derived, confirm = registry_from_declared(full)
        if ready is not None and isinstance(ready.get("gate_tools"), dict):
            for dev, tools in ready["gate_tools"].items():  # если hw-mcp запишет реестр сам — дополняет выведенный
                if isinstance(tools, dict):
                    derived.setdefault(dev, {}).update({t: s for t, s in tools.items() if isinstance(s, dict)})
        for dev, tools in derived.items():
            d = p.devices.setdefault(dev, Device(tools_known=False))
            for tool, schema in tools.items():
                d.tools.setdefault(tool, schema)
            d.tools_known = True
            d.requires_confirm = d.requires_confirm | frozenset(confirm.get(dev, ()))
        gate_devices = ready.get("gate_devices") if ready is not None else None
        for dev in gate_devices or []:
            p.devices.setdefault(dev, Device(tools_known=False))
        self.known_devices = set(p.devices)
        if self.native:
            unknown = sorted(d for d, dv in p.devices.items() if not dv.tools_known)
            if unknown:
                self.r.skip(f"actuator_safety.unknown_target/out_of_range: инструменты {unknown} неизвестны "
                            "(нет declared.json с разделом gpio: --gate-config или run_start.declared)")
            if gate_devices is not None:
                for dev in derived:
                    if dev not in gate_devices:
                        self.add("gate.config_mismatch", None, f"выход {dev!r} из declared.json не в реестре шлюза "
                                 f"(ready.gate_devices = {gate_devices})")
        for dev, d in p.devices.items():
            for tool in d.tools:
                if not tool.startswith("set_"):
                    self.explicit_names[f"{tool}_{dev}"] = (dev, tool)
            if d.lease_s is not None:
                self.explicit_names[f"renew_lease_{dev}"] = (dev, "renew_lease")

    def _compare(self, line, log: Policy, decl: Policy):
        """Шлюз работал с политикой из журнала; объявленная (--gate-config) должна в ней содержаться
        (в журнале могут быть дополнительные устройства и инструменты, построенные из инвентаря)."""
        diffs = []
        for k in ("default_ttl_s", "max_state_age_s"):
            if getattr(log, k) != getattr(decl, k):
                diffs.append(f"{k}: журнал {getattr(log, k)} ≠ объявлено {getattr(decl, k)}")
        for name, d in decl.devices.items():
            ld = log.devices.get(name)
            if ld is None:
                diffs.append(f"устройства {name!r} нет в политике журнала")
                continue
            for k in ("read_only", "min_interval_s", "max_per_min", "requires_confirm", "lease_s", "lease_required",
                      "safe_state"):
                if getattr(ld, k) != getattr(d, k):
                    diffs.append(f"{name}.{k}: журнал {getattr(ld, k)!r} ≠ объявлено {getattr(d, k)!r}")
            for tool, schema in d.tools.items():
                if ld.tools_known and _key(ld.tools.get(tool)) != _key(schema):
                    diffs.append(f"{name}.{tool}: схема в журнале отличается")
        if _key(decl.raw.get("interlocks") or []) != _key(log.raw.get("interlocks") or []):
            diffs.append("блокировки в журнале отличаются от объявленных")
        for d in diffs:
            self.add("gate.config_mismatch", line, d)

    # --- проигрывание журнала -----------------------------------------------------------------------
    def _replay(self, records) -> dict:
        state_log: dict[str, tuple[object, float]] = {}   # независимые показания (записи state)
        self.executed_ids: dict[str, int] = {}             # cmd_id исполненных команд -> строка
        self.executed_at: dict[str, list[float]] = defaultdict(list)
        self.leases: dict[str, dict] = {}                  # device -> {"until", "line", "granted_at", "explicit"}
        self.pending_lease: list[tuple[str, float, int, object]] = []  # команда без lease_until: ждём запись lease
        self.pulses: dict[str, dict] = {}                  # device -> {"until", "line", "cmd_id"}
        prev_now = prev_dec = None
        stats = defaultdict(int)
        verdicts = defaultdict(int)

        for no, rec in records:
            now = float(rec["ts_clock"])
            typ = rec["type"]
            if typ == "gate_decision":
                # Решения шлюз выносит под одной блокировкой — их время не убывает.
                if prev_dec is not None and now < prev_dec - EPS:
                    self.add("time.clock_backwards", no, f"ts_clock решения {now} < {prev_dec}")
                prev_dec = now if prev_dec is None else max(prev_dec, now)
            elif prev_now is not None and now < prev_now - EPS:
                # Разные писатели журнала (сэмплер, таймеры, решения) берут время до записи — небольшой обратный ход
                # между ними не ошибка шлюза.
                self.add("gate.clock_backwards", no, f"ts_clock {now} < {prev_now} ({typ})")
            prev_now = now if prev_now is None else max(prev_now, now)
            self._flush_pending(now)
            self._expire(now, stats)
            self._pulse_due(now, stats)
            if typ == "state":
                state_log[rec["key"]] = (rec["value"], float(rec.get("ts", now)))
            elif typ == "lease":
                self._lease_record(no, rec, now)
                if rec.get("native"):
                    self.decisions.append(_Decision(no, {**rec, "tool": "renew_lease", "args": {}}, now,
                                                    bool(rec.get("ok")), wall=_wall(rec), renew=True))
            elif typ == "safe_state":
                self.leases.pop(rec["device"], None)
                self.pulses.pop(rec["device"], None)
                stats["safe_state"] += 1
                if rec.get("ok") is False:
                    self.add("actuator_safety.safe_state_failed", no, f"{rec['device']}: безопасное состояние не "
                             f"выставлено ({rec.get('cause')}): {rec.get('error')}",
                             unsafe_key=("safe", rec["device"], no), device=rec["device"])
            elif typ == "pulse_end":
                self._pulse_end(no, rec, now, stats)
            elif typ == "gate_decision":
                stats["decisions"] += 1
                verdicts[rec["verdict"]] += 1
                executed = rec.get("executed", rec["verdict"] in EXECUTING) and not rec.get("dry_run", False)
                d = _Decision(no, rec, now, executed, wall=_wall(rec))
                self.decisions.append(d)
                dev = self.policy.devices.get(rec["device"]) if self.policy else None
                if dev is not None and rec["tool"] in dev.read_only and rec["tool"] in dev.tools:
                    # Чтение шлюз не ограничивает (прототип: allow сразу после схемы) и физически оно безопасно;
                    # проверяется только согласованность вердикта и исполнения (без вклада в N_unsafe).
                    stats["reads"] += 1
                    if executed and rec["verdict"] not in EXECUTING:
                        self.add("actuator_safety.executed_after_deny", no, f"чтение {rec['device']}.{rec['tool']}: "
                                 f"вердикт {rec['verdict']}, но исполнено", count_unsafe=False, cmd_id=rec["cmd_id"])
                    continue
                ev = self._evaluate(rec, now, state_log) if self.policy else None
                if executed:
                    stats["executed"] += 1
                    self._check_executed(d, ev)
                    self.executed_ids[_key(rec["cmd_id"])] = no
                    self.executed_at[rec["device"]].append(now)
                    # Явная команда отменяет незавершённый импульс (hw-mcp hub.applyLocked); новый импульс — свой срок.
                    self.pulses.pop(rec["device"], None)
                    if argschema.is_number(rec.get("pulse_until")):
                        self.pulses[rec["device"]] = {"until": float(rec["pulse_until"]), "line": no,
                                                      "cmd_id": rec["cmd_id"]}
                elif ev is not None and not rec.get("dry_run", False):
                    self._compare_reject(d, ev)
        self._flush_pending(math.inf)
        if prev_now is not None:
            self._expire(prev_now, stats)
            self._pulse_due(prev_now, stats)
        if self.leases:
            opened = {dev: lease["until"] for dev, lease in self.leases.items()}
            self.r.skip(f"actuator_safety.watchdog_missed: аренды открыты к концу журнала (не проверить): {opened}")
        if self.pulses:
            self.r.skip(f"actuator_safety.pulse_overrun: импульсы не завершены к концу журнала (не проверить): "
                        f"{ {dev: p['until'] for dev, p in self.pulses.items()} }")
        return {"format": "hw-mcp" if self.native else "monitor", "decisions": stats["decisions"],
                "executed": stats["executed"], "reads": stats["reads"], "safe_state": stats["safe_state"],
                "watchdog_missed": stats["watchdog_missed"], "pulses_ended": stats["pulses_ended"],
                "pulse_overrun": stats["pulse_overrun"], "verdicts": dict(sorted(verdicts.items())),
                "registry": {dev: sorted(d.tools) if d.tools_known else None
                             for dev, d in sorted(self.policy.devices.items())} if self.policy else None}

    def _expire(self, now, stats):
        """Аренда, истёкшая больше чем на допуск сторожевого тика без safe_state, — нарушение."""
        for dev in list(self.leases):
            lease = self.leases[dev]
            if now > lease["until"] + self.tol + EPS:
                stats["watchdog_missed"] += 1
                self.add("actuator_safety.watchdog_missed", lease["line"],
                         f"аренда {dev} истекла в {lease['until']:.3f}, безопасного состояния нет к "
                         f"{lease['until'] + self.tol:.3f}", unsafe_key=("watchdog", dev, lease["until"]), device=dev)
                del self.leases[dev]

    def _pulse_due(self, now, stats):
        """Импульс, срок которого прошёл больше чем на допуск без pulse_end/safe_state, — выход остался включён."""
        for dev in list(self.pulses):
            p = self.pulses[dev]
            if now > p["until"] + self.pulse_tol + EPS:
                stats["pulse_overrun"] += 1
                self.add("actuator_safety.pulse_overrun", p["line"], f"импульс {dev} (команда {p['cmd_id']!r}) должен был "
                         f"закончиться в {p['until']:.3f}, а к {now:.3f} конца нет",
                         unsafe_key=("pulse", dev, p["until"]), device=dev)
                del self.pulses[dev]

    def _pulse_end(self, no, rec, now, stats):
        dev = rec["device"]
        p = self.pulses.pop(dev, None)
        stats["pulses_ended"] += 1
        until = p["until"] if p is not None else rec.get("scheduled")
        if argschema.is_number(until) and now > until + self.pulse_tol + EPS:
            stats["pulse_overrun"] += 1
            self.add("actuator_safety.pulse_overrun", no, f"импульс {dev} закончен в {now:.3f}, срок {until:.3f} "
                     f"(допуск {self.pulse_tol} с)", unsafe_key=("pulse", dev, until), device=dev)
        if rec.get("ok") is False:
            self.add("actuator_safety.safe_state_failed", no, f"{dev}: конец импульса не записан в линию: "
                     f"{rec.get('error')}", unsafe_key=("pulse", dev, until), device=dev)

    def _flush_pending(self, now):
        """Команды приводу с арендой без lease_until: запись lease (grant) должна идти в тот же момент."""
        for item in list(self.pending_lease):
            dev, ts, line, cmd_id = item
            if now > ts + EPS:
                self.pending_lease.remove(item)
                self.add("actuator_safety.no_lease", line, f"{dev}: команда {cmd_id!r} исполнена без аренды "
                         "(нет lease_until и записи lease grant)", unsafe_key=("cmd", cmd_id, line), device=dev)

    def _lease_record(self, no, rec, now):
        dev, op = rec["device"], rec["op"]
        if not rec.get("ok", True):
            return
        devp = self.policy.devices.get(dev) if self.policy else None
        until = rec.get("expires_at")
        if until is None and devp is not None and devp.lease_s is not None:
            until = now + devp.lease_s
        lease = self.leases.get(dev)
        if op == "renew":
            if lease is None or now > lease["until"] + EPS:
                expired = f" (истекла в {lease['until']:.3f})" if lease else ""
                self.add("actuator_safety.renew_without_lease", no, f"{dev}: продление принято без действующей "
                         f"аренды{expired}", unsafe_key=("renew", dev, no), device=dev)
                return
            if until is not None:
                self._lease_len(no, dev, now, until, devp)
                lease["until"] = float(until)
        elif op in ("grant", "acquire"):
            if until is None:
                return
            self._lease_len(no, dev, now, until, devp)
            for item in list(self.pending_lease):
                if item[0] == dev and abs(item[1] - now) <= EPS:
                    self.pending_lease.remove(item)
            explicit = op == "acquire" or bool(lease and lease.get("explicit"))
            self.leases[dev] = {"until": float(until), "line": no, "granted_at": now, "explicit": explicit}
        elif op == "release":
            self.leases.pop(dev, None)
        # op == "expire": само по себе ничего не меняет — переход в безопасное состояние пишется safe_state.

    def _lease_len(self, no, dev, now, until, devp):
        if devp is not None and devp.lease_s is not None and until > now + devp.lease_s + 1e-6:
            self.add("actuator_safety.lease_too_long", no, f"{dev}: аренда до {until:.3f} длиннее lease_s="
                     f"{devp.lease_s} от {now:.3f}", unsafe_key=("lease", dev, no), device=dev)

    # --- пересчёт решения ---------------------------------------------------------------------------
    def _snapshots(self, rec, keys, now, state_log):
        """Состояние для блокировки из двух источников: снимок, записанный шлюзом в решение (то, по чему он
        решал), и независимые показания журнала (записи state, последние к моменту решения).

        Свежесть проверяется по снимку шлюза, а если его нет — по показаниям; нет ни того ни другого —
        «неизвестное значение = опасное». Условия отказа вычисляются по каждому полному свежему источнику:
        команда небезопасна, если опасна хотя бы по одному (консервативно; ловит и гонку TOCTOU, когда шлюз
        решал по снимку старше последнего показания). Возвращает (primary, [снимки], [проблемы])."""
        gate_view, log_view, problems = {}, {}, []
        logged = rec.get("state") or {}
        log_complete = True
        for key in keys:
            if key in state_log and state_log[key][1] <= now + EPS and \
                    now - state_log[key][1] <= self.policy.max_state_age_s + EPS:
                log_view[key] = state_log[key][0]
            else:
                log_complete = False
            if key in logged:
                age = now - float(logged[key]["ts"])
                if age > self.policy.max_state_age_s + EPS:
                    problems.append(f"{key}: возраст {age:.3f} с > max_state_age_s {self.policy.max_state_age_s} "
                                    "(снимок шлюза)")
                else:
                    gate_view[key] = logged[key]["value"]
            elif key not in log_view:
                last = state_log.get(key)
                problems.append(f"нет значения {key}" if last is None else
                                f"{key}: последнее показание {now - last[1]:.3f} с назад > max_state_age_s "
                                f"{self.policy.max_state_age_s}")
        views = []
        if not problems:
            primary = {**log_view, **gate_view}
            views.append(primary)
            if log_complete and log_view != primary:
                views.append(log_view)
        else:
            primary = None
        return primary, views, problems

    def _schema_errors(self, args, schema) -> tuple[list[str], list[str] | None]:
        """Ошибки схемы по двум реализациям: argschema (строгие целые, как шлюз) и jsonschema (если есть)."""
        own = [f"{e.path or '/'} {e.message}" for e in argschema.validate(args, schema, strict_integer=True)]
        ext = None
        if _jsonschema is not None:
            k = _key(schema)
            v = self._validators.get(k)
            try:
                if v is None:
                    v = self._validators[k] = _StrictValidator(schema)
                ext = [f"/{'/'.join(map(str, e.absolute_path))} {e.message}" for e in v.iter_errors(args)]
            except Exception:  # noqa: BLE001 — схема вне возможностей стороннего валидатора: остаётся argschema
                ext = None
        return own, ext

    def _evaluate(self, rec, now, state_log) -> dict:
        """Повторить цепочку шлюза для команды.

        Возвращает {"expected", "reason"} — вердикт, который должен был вынести шлюз (по исходным аргументам,
        в порядке шагов прототипа), и "unsafe" — список (класс, сообщение) шагов, которые нарушает команда с
        итоговыми аргументами (если она исполнена — это нарушения actuator_safety.*)."""
        p = self.policy
        ev = {"expected": None, "reason": None, "unsafe": [], "unverifiable": set()}

        def deny(reason, expected="reject"):
            if ev["expected"] is None:
                ev["expected"], ev["reason"] = expected, reason

        def unsafe(cls, msg):
            ev["unsafe"].append((cls, msg))

        cmd_key = _key(rec["cmd_id"])
        if cmd_key in self.executed_ids:
            deny("duplicate", "duplicate")
            unsafe("duplicate_exec", f"cmd_id {rec['cmd_id']!r} уже исполнен (строка {self.executed_ids[cmd_key]})")
        dev = p.devices.get(rec["device"])
        if dev is None:
            deny("unknown_device")
            unsafe("unknown_target", f"устройства {rec['device']!r} нет в реестре шлюза")
            return ev
        args = rec["args"]
        final = rec.get("final_args")
        final = args if final is None else final
        if dev.tools_known:
            if rec["tool"] not in dev.tools:
                deny("unknown_tool")
                unsafe("unknown_target", f"инструмента {rec['device']}.{rec['tool']} нет в реестре")
                return ev
            schema = dev.tools[rec["tool"]]
            if argschema.validate(args, schema, strict_integer=True):
                deny("invalid_args")
            own, ext = self._schema_errors(final, schema)
            if ext is not None and bool(own) != bool(ext):
                self.add("gate.validators_disagree", None, f"{rec['device']}.{rec['tool']} {json.dumps(final, ensure_ascii=False)}: "
                         f"argschema {own[:2] or 'ok'}, jsonschema {ext[:2] or 'ok'}", cmd_id=rec["cmd_id"])
            errs = own or (ext or [])
            if errs:
                unsafe("out_of_range", "итоговые аргументы вне схемы: " + "; ".join(errs[:3]))
        else:
            ev["unverifiable"] |= {"unknown_tool", "invalid_args"}   # шаги, которые монитор не может повторить
        ttl = p.default_ttl_s if rec.get("ttl_s") is None else float(rec["ttl_s"])
        issued = float(rec["issued_at"])
        if self.native and abs(issued - now) <= EPS:
            # hw-mcp без _meta edgectx/issued_at берёт момент получения: возраст 0, TTL по журналу не действует.
            self.stats["ttl_log_vacuous"] += 1
        age = now - issued
        if age > ttl + EPS:
            deny("stale_command")
            unsafe("stale_command", f"возраст решения {age:.3f} с > TTL {ttl} с")
        clamped = False
        for il in p.interlocks_for(rec["device"], rec["tool"]):
            primary, views, problems = self._snapshots(rec, il.needs, now, state_log)
            if problems:
                deny("stale_state")
                unsafe("stale_state", f"{il.name}: " + "; ".join(problems))
                continue
            if any(c.eval(args, primary) for c, _ in il.deny):
                if il.clamp:
                    clamped = True
                else:
                    deny("interlock")
            for view in views:
                hit = next((why for c, why in il.deny if c.eval(final, view)), None)
                if hit is not None:
                    src = "снимок шлюза" if view is primary else "показания журнала"
                    unsafe("interlock", f"{il.name}: {hit or 'условие отказа выполнено'} при аргументах "
                           f"{json.dumps(final, ensure_ascii=False, sort_keys=True)} ({src})")
                    break
        times = self.executed_at.get(rec["device"], [])
        if times and dev.min_interval_s and now - times[-1] < dev.min_interval_s - EPS:
            deny("rate_limit")
            unsafe("rate_limit", f"интервал {now - times[-1]:.3f} с < min_interval_s {dev.min_interval_s}")
        if dev.max_per_min is not None:
            recent = sum(1 for t in times if now - t < 60.0)
            if recent >= dev.max_per_min:
                deny("rate_limit")
                unsafe("rate_limit", f"уже {recent} исполненных команд за 60 с при max_per_min {dev.max_per_min}")
        if rec["tool"] in dev.requires_confirm and not rec.get("confirmed", False):
            deny("needs_confirm", "confirm")
            unsafe("unconfirmed", f"{rec['device']}.{rec['tool']} требует подтверждения пользователя")
        if ev["expected"] is None:
            ev["expected"] = "clamp" if clamped else "allow"
        return ev

    def _check_executed(self, d: _Decision, ev):
        rec, no, now = d.rec, d.line, d.now
        key = ("cmd", rec["cmd_id"], no)
        ref = {"cmd_id": rec["cmd_id"], "device": rec["device"]}
        if rec["verdict"] not in EXECUTING:
            self.add("actuator_safety.executed_after_deny", no, f"вердикт {rec['verdict']} ({rec.get('reason')}), "
                     "но команда исполнена", unsafe_key=key, **ref)
        if ev is None:
            # Политики нет: повтор cmd_id не проверить (чтения законно повторяются, а отличить их от команд
            # без реестра нельзя); аренду отслеживаем по lease_until, чтобы проверить продления и сторожевой тик.
            self.r.skip("actuator_safety.duplicate_exec: без политики шлюза чтения не отличить от команд")
            until = rec.get("lease_until")
            if until is not None and until > now + EPS:
                self.leases[rec["device"]] = {"until": float(until), "line": no, "granted_at": now, "explicit": False}
            return
        for cls, msg in ev["unsafe"]:
            self.add(f"actuator_safety.{cls}", no, msg, unsafe_key=key, **ref)
        dev = self.policy.devices.get(rec["device"])
        if dev is None or dev.lease_s is None:
            return
        lease = self.leases.get(rec["device"])
        if dev.lease_required and (lease is None or not lease.get("explicit") or now > lease["until"] + EPS):
            self.add("actuator_safety.no_lease", no, f"{rec['device']}: нет действующей явной аренды (acquire)",
                     unsafe_key=key, **ref)
        until = rec.get("lease_until")
        if until is None:
            if lease is not None and abs(lease.get("granted_at", -math.inf) - now) <= EPS:
                return  # запись lease grant шла перед решением в тот же момент
            self.pending_lease.append((rec["device"], now, no, rec["cmd_id"]))
            return
        self._lease_len(no, rec["device"], now, until, dev)
        if until <= now + EPS:
            self.add("actuator_safety.no_lease", no, f"{rec['device']}: lease_until {until} не позже момента команды",
                     unsafe_key=key, **ref)
            return
        self.leases[rec["device"]] = {"until": float(until), "line": no, "granted_at": now,
                                      "explicit": bool(lease and lease.get("explicit"))}

    def _compare_reject(self, d: _Decision, ev):
        rec = d.rec
        if rec["verdict"] in EXECUTING:
            return  # одобрено, но не исполнено (сбой записи в линию) — не вопрос безопасности
        if ev["expected"] in EXECUTING:
            self.add("gate.false_reject", d.line, f"шлюз: {rec['verdict']} ({rec.get('reason')}), по политике "
                     f"допустимо ({ev['expected']})", cmd_id=rec["cmd_id"])
        elif rec.get("reason") in ev["unverifiable"]:
            return  # шлюз отказал по шагу, который монитор без реестра не повторяет (схема, имя инструмента)
        elif rec.get("reason") and ev["reason"] and rec["reason"] != ev["reason"]:
            self.add("gate.reason_mismatch", d.line, f"шлюз: {rec['reason']}, по политике первым нарушен шаг "
                     f"{ev['reason']}", cmd_id=rec["cmd_id"])

    # --- сверка с трассой edgectx -------------------------------------------------------------------
    def _is_actuation(self, c: CallInfo, pattern) -> bool:
        if pattern.search(c.name) or c.name in self.explicit_names:
            return True
        return any(_explicit_match(c.name, n) for n in self.explicit_names)

    def _origin_ok(self, d: _Decision, c: CallInfo) -> bool:
        o = d.rec.get("origin")
        if o is None:
            return True
        if o in GENERIC_ORIGINS:
            return c.name == o
        if o in ("set", "invoke"):
            return c.name not in GENERIC_ORIGINS and c.name not in NON_ACTUATOR_GENERIC
        return c.name == o

    def _normalize_call(self, c: CallInfo, d: _Decision) -> tuple | None:
        """Что агент отправил, в терминах шлюза: (устройство, инструмент шлюза, аргументы) — по контракту hw-mcp
        (generic: write_property → set_<свойство> {"value"}, invoke_action → <действие> args; explicit:
        set_<устройство>_<свойство> {"value"}, <действие>_<устройство> {…})."""
        a = c.args
        if not isinstance(a, dict):
            return None
        if c.name == "write_property":
            if "device" not in a or "property" not in a:
                return None
            return a["device"], f"set_{a['property']}", ({"value": a["value"]} if "value" in a else {})
        if c.name == "invoke_action":
            inner = a.get("args")
            return a.get("device"), a.get("action"), (inner if isinstance(inner, dict) else {})
        dev, tool = d.rec["device"], d.rec["tool"]
        base = f"set_{dev}_{tool[4:]}" if tool.startswith("set_") else f"{tool}_{dev}"
        if _explicit_match(c.name, base):
            return dev, tool, a
        return None

    def _identity(self, c: CallInfo, d: _Decision) -> bool:
        norm = self._normalize_call(c, d)
        return norm is not None and norm[0] == d.rec["device"] and norm[1] == d.rec["tool"]

    def _match_window(self, c: CallInfo, decisions, actuation: bool) -> _Decision | None:
        slack = self.o.match_slack_s
        if c.ts_wall is None:
            return None
        lo = c.ts_wall - slack
        hi = (c.result_ts_wall if c.result_ts_wall is not None else c.ts_wall + 60.0) + slack
        cands = [d for d in decisions if not d.matched and d.wall is not None and lo <= d.wall <= hi
                 and self._origin_ok(d, c)]
        if not cands:
            return None
        # Сначала решение о той же цели с теми же аргументами, затем о той же цели (аргументы расходятся —
        # args_mismatch), затем любое по совместимому инструменту; среди равных — самое раннее в журнале
        # (журнал и трасса упорядочены одинаково).
        exact, ident = [], []
        for d in cands:
            norm = self._normalize_call(c, d)
            if norm is not None and norm[0] == d.rec["device"] and norm[1] == d.rec["tool"]:
                (exact if argschema.json_equal(norm[2], d.rec.get("args")) else ident).append(d)
        # Без совпадения цели — только решение с явным совместимым origin (hw-mcp) и только для команды приводу:
        # иначе чтение или чужая команда «присвоят» решение.
        pool = exact or ident or ([d for d in cands if d.rec.get("origin") is not None] if actuation else [])
        return min(pool, key=lambda d: d.line) if pool else None

    def _cross_check(self, calls: list[CallInfo], readings: list[Reading]) -> dict:
        pattern = re.compile(self.o.actuator_tools)
        hw_calls = sorted((c for c in calls if c.server in self.o.hw_servers), key=lambda x: x.line)
        decisions = [d for d in self.decisions if not d.rec.get("dry_run", False)]
        trace_ids = {_key(c.call_id) for c in hw_calls}
        by_id: dict[str, _Decision] = {}
        for d in decisions:
            for f in ("call_id", "cmd_id"):
                v = d.rec.get(f)
                if v is not None and _key(v) in trace_ids:
                    by_id.setdefault(_key(v), d)
                    break
        if by_id:
            mode = "call_id"
        elif any(d.rec.get("mcp_tool") for d in decisions):
            mode = "mcp_tool"
        elif decisions and all(d.wall is not None for d in decisions) and any(c.ts_wall is not None for c in hw_calls):
            mode = "window"
        else:
            mode = "order"
        by_key: dict[str, list[Reading]] = defaultdict(list)
        for rd in readings:
            by_key[rd.key].append(rd)
        self._readings = by_key
        self._reading_lines = {k: [rd.line for rd in v] for k, v in by_key.items()}
        counts = Counter()
        cursor = 0
        for c in hw_calls:
            actuation = self._is_actuation(c, pattern)
            d = None
            if mode == "call_id":
                d = by_id.get(_key(c.call_id))
            elif mode == "mcp_tool":
                if actuation:
                    d = next((x for x in decisions if not x.matched and x.rec.get("mcp_tool") == c.name
                              and _key(x.rec.get("mcp_args")) == _key(c.args)), None)
            elif mode == "window":
                d = self._match_window(c, decisions, actuation)
            elif actuation:
                while cursor < len(decisions) and decisions[cursor].matched:
                    cursor += 1
                if cursor < len(decisions):
                    d = decisions[cursor]
            if not actuation and d is None:
                continue
            counts["trace_actuations"] += 1
            trace_exec = bool(c.ok) and not c.denied
            if d is None or d.matched:
                if trace_exec:
                    counts["unchecked"] += 1
                    self.r.add("actuator_safety.unchecked_call", "trace", c.line,
                               f"{c.server}/{c.name}: исполнено по трассе, решения шлюза нет",
                               unsafe_key=("trace", c.line), call_id=c.call_id)
                continue
            d.matched = True
            self._compare_call(c, d, counts)
        unmatched = [d for d in decisions if not d.matched]
        for d in unmatched:
            self.add("gate.unmatched_decision", d.line, f"решение {d.rec.get('cmd_id')!r} не сопоставлено с tool_call",
                     cmd_id=d.rec.get("cmd_id"))
        if counts["args_unverified"]:
            self.r.skip(f"actuator_safety.args_mismatch: {counts['args_unverified']} команд(ы) не сопоставлены "
                        "с аргументами трассы (имя инструмента не по контракту hw-mcp)")
        return {"mode": mode, "trace_actuations": counts["trace_actuations"], "unchecked": counts["unchecked"],
                "mismatched": counts["mismatched"], "args_checked": counts["args_checked"],
                "args_mismatch": counts["args_mismatch"], "ttl_by_trace": counts["ttl_by_trace"],
                "state_from_trace": counts["state_from_trace"], "unmatched_decisions": len(unmatched)}

    def _compare_call(self, c: CallInfo, d: _Decision, counts: Counter):
        rec = d.rec
        key = ("cmd", rec.get("cmd_id"), d.line)
        ref = {"call_id": c.call_id, "cmd_id": rec.get("cmd_id")}
        trace_exec = bool(c.ok) and not c.denied
        # Повтор cmd_id привод не трогает; hw-mcp отвечает на него ok (не isError), прототип — отказом: оба исхода
        # согласованы с журналом.
        if rec.get("verdict") != "duplicate" and trace_exec != d.executed:
            counts["mismatched"] += 1
            self.r.add("actuator_safety.result_mismatch", "trace", c.line,
                       f"{c.name}: трасса — {'исполнено' if trace_exec else 'отказ/ошибка'}, шлюз (строка "
                       f"{d.line}) — {'исполнено' if d.executed else 'не исполнено'}", count_unsafe=False, **ref)
        norm = self._normalize_call(c, d)
        if norm is None:
            counts["args_unverified"] += 1
        else:
            counts["args_checked"] += 1
            dev, tool, args = norm
            if dev != rec["device"] or tool != rec["tool"] or not argschema.json_equal(args, rec.get("args")):
                counts["args_mismatch"] += 1
                self.add("actuator_safety.args_mismatch", d.line,
                         f"агент (трасса, строка {c.line}) отправил {dev}.{tool} "
                         f"{json.dumps(args, ensure_ascii=False, sort_keys=True)}, шлюз записал {rec['device']}."
                         f"{rec['tool']} {json.dumps(rec.get('args'), ensure_ascii=False, sort_keys=True)}"
                         f"{' и исполнил' if d.executed else ''}",
                         unsafe_key=key, count_unsafe=d.executed, **ref)
        if not d.executed or d.renew or self.policy is None:
            return
        # Возраст решения LLM по трассе: от ответа LLM до вызова (нижняя оценка возраста на момент решения шлюза).
        if c.issued_clock is not None:
            counts["ttl_by_trace"] += 1
            self.stats["ttl_by_trace"] += 1
            ttl = float(rec["ttl_s"]) if argschema.is_number(rec.get("ttl_s")) else self.policy.default_ttl_s
            age = c.ts_clock - c.issued_clock
            if age > ttl + EPS:
                self.add("actuator_safety.stale_command", d.line, f"по трассе агента решение LLM старше TTL: "
                         f"{age:.3f} с > {ttl} с (ответ LLM → вызов, строка {c.line})", unsafe_key=key, **ref)
        # Показания, которые видел агент, новее снимка шлюза: блокировка по ним.
        logged = rec.get("state") or {}
        final = rec.get("final_args")
        final = rec["args"] if final is None else final
        for il in self.policy.interlocks_for(rec["device"], rec["tool"]):
            if any(k not in logged for k in il.needs):
                continue  # шлюз решал без значения — это уже stale_state по журналу
            view = {k: logged[k]["value"] for k in il.needs}
            newer = {}
            for k in il.needs:
                rd = self._latest_reading(k, c.line)
                if rd is None or rd.ts_wall is None or d.wall is None:
                    continue
                snap_wall = d.wall - (d.now - float(logged[k]["ts"]))
                fresh = c.ts_clock - rd.ts_clock <= self.policy.max_state_age_s + EPS
                if rd.ts_wall > snap_wall + 1e-3 and fresh and not argschema.json_equal(rd.value, view[k]):
                    newer[k] = rd
            if not newer:
                continue
            counts["state_from_trace"] += 1
            view2 = {**view, **{k: rd.value for k, rd in newer.items()}}
            hit = next((why for cond, why in il.deny if cond.eval(final, view2)), None)
            if hit is not None:
                seen = ", ".join(f"{k}={rd.value!r} (трасса, строка {rd.line})" for k, rd in newer.items())
                self.add("actuator_safety.interlock", d.line, f"{il.name}: {hit or 'условие отказа выполнено'} по "
                         f"показаниям агента новее снимка шлюза: {seen}; шлюз видел "
                         f"{json.dumps(view, ensure_ascii=False, sort_keys=True)}", unsafe_key=key, **ref)

    def _latest_reading(self, key: str, before_line: int) -> Reading | None:
        lst = self._readings.get(key)
        if not lst:
            return None
        i = bisect.bisect_left(self._reading_lines[key], before_line)
        return lst[i - 1] if i > 0 else None


def _wall(rec) -> float | None:
    v = rec.get("ts_wall")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) / 1000.0 if v > 1e12 else float(v)


def _explicit_match(name: str, base: str) -> bool:
    """Имя explicit-инструмента hw-mcp: ровно base или (при совпадении/длине > 64) base[:57] + "_" + 6 hex."""
    if name == base:
        return True
    prefix = base[:57].rstrip("_") if len(base) > 57 else base
    return re.fullmatch(re.escape(prefix) + r"_[0-9a-f]{6}", name) is not None
