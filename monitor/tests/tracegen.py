"""Генератор корректных синтетических прогонов: трасса edgectx (§6) + журнал шлюза + каталог + политика.

Модель агента повторяет то, что известно о edgectx (agent/internal/sched, configs/example.yaml): входы
(команды пользователя, датчики, тики heartbeat, ситуации), гейт (подавление или wake), планировщик с
суточным бюджетом (user обслуживается всегда, фон отключается с degrade_at, всё, кроме user, — при
исчерпании), ход до max_steps запросов к LLM, вызовы инструментов, правила без LLM, служебные запросы
(сводка истории) с purpose.

Решения шлюза принимает ПРОТОТИП research/new_topic/GAP3_artifacts/actuator_gate.py (его demo_setup с
блокировками на Python-функциях), а монитор перепроверяет их по декларативной политике DECLARED_GATE —
две независимые реализации одних правил. Корректный прогон должен давать 0 нарушений и 0 предупреждений.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_GATE_PATH = ROOT / "research" / "new_topic" / "GAP3_artifacts" / "actuator_gate.py"


def _load_prototype():
    spec = importlib.util.spec_from_file_location("gap3_actuator_gate", _GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


proto = _load_prototype()

_BOOL = {"type": "boolean"}


def _obj(props: dict, required=None) -> dict:
    s = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        s["required"] = required
    return s


# Импульс выхода GPIO hw-mcp (hwmcp/README «Инструменты шлюза»): pulse {"seconds": 0 < s ≤ 3600}; шлюз пишет
# pulse_until в решение, а таймер выхода — pulse_end{scheduled} в срок. В прототипе импульсов нет: устройство
# relay добавляет demo_setup() ниже, длительность и конец импульса ведёт генератор (_Sim.flush_gate).
PULSE_SCHEMA = _obj({"seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600}}, ["seconds"])

# Декларативная политика, эквивалентная demo_setup() (формат раздела "gate" declared.json hw-mcp).
DECLARED_GATE = {
    "default_ttl_s": 10, "max_state_age_s": 5, "commit_window_s": 5,
    "devices": {
        "heater": {"tools": {"set_power": _obj({"on": _BOOL}, ["on"])}, "min_interval_s": 30, "safe_state": {"on": False}},
        "pump": {"tools": {"set_power": _obj({"on": _BOOL}, ["on"])}, "min_interval_s": 10, "safe_state": {"on": False}},
        "valve": {"tools": {"set_angle": _obj({"deg": {"type": "integer", "minimum": 0, "maximum": 90}}, ["deg"])},
                  "max_per_min": 20, "safe_state": {"deg": 0}},
        "fan": {"tools": {"set_speed": _obj({"pct": {"type": "integer", "minimum": 0, "maximum": 100}}, ["pct"])},
                "lease_s": 2.0, "safe_state": {"pct": 0}},
        "door": {"tools": {"lock": {"type": "object", "additionalProperties": False},
                           "unlock": {"type": "object", "additionalProperties": False}},
                 "requires_confirm": ["unlock"]},
        "led": {"tools": {"set_color": _obj({c: {"type": "integer", "minimum": 0, "maximum": 255} for c in "rgb"},
                                            ["r", "g", "b"])}},
        "thermo": {"tools": {"read": {"type": "object", "additionalProperties": False}}, "read_only": ["read"]},
        "camera": {"tools": {"capture": _obj({"w": {"type": "integer", "minimum": 160, "maximum": 1920}})},
                   "read_only": ["capture"]},
        "relay": {"tools": {"pulse": PULSE_SCHEMA}, "min_interval_s": 10, "safe_state": {"on": False}},
    },
    "interlocks": [
        {"name": "heater_window", "device": "heater", "tool": "set_power", "needs": ["window_open", "temp_c"],
         "deny": [{"if": {"all": [{"arg": "on", "eq": True}, {"state": "window_open", "eq": True}]}, "reason": "окно открыто"},
                  {"if": {"all": [{"arg": "on", "eq": True}, {"state": "temp_c", "ge": 30.0}]}, "reason": "температура >= 30 °C"}]},
        {"name": "pump_dry_run", "device": "pump", "tool": "set_power", "needs": ["water_level_pct"],
         "deny": [{"if": {"all": [{"arg": "on", "eq": True}, {"state": "water_level_pct", "lt": 10.0}]},
                   "reason": "сухой ход: уровень воды < 10 %"}]},
        {"name": "fan_human_speed", "device": "fan", "tool": "set_speed", "needs": ["human_near"],
         "deny": [{"if": {"all": [{"state": "human_near", "eq": True}, {"arg": "pct", "gt": 30}]},
                   "reason": "человек рядом, скорость > 30 %"}],
         "clamp": {"pct": {"set": 30}}},
    ],
}
NEEDS = {("heater", "set_power"): ["window_open", "temp_c"], ("pump", "set_power"): ["water_level_pct"],
         ("fan", "set_speed"): ["human_near"]}
DEVICES = sorted(DECLARED_GATE["devices"])


def demo_setup():
    """Стенд прототипа (proto.demo_setup) плюс импульсный выход relay — как у DECLARED_GATE."""
    devices, interlocks = proto.demo_setup()
    devices["relay"] = proto.Device("relay", {"pulse": copy.deepcopy(PULSE_SCHEMA)}, min_interval_s=10.0,
                                    safe_state={"on": False})
    return devices, interlocks

CATALOG = {"servers": {
    "hw": [
        {"name": "invoke_action", "inputSchema": _obj({
            "device": {"type": "string", "enum": DEVICES}, "action": {"type": "string", "minLength": 1},
            "args": {"type": "object"}}, ["device", "action"])},
        {"name": "read_property", "inputSchema": _obj({
            "device": {"type": "string", "enum": ["room", "tank", "hall"]},
            "property": {"type": "string", "enum": ["temperature", "window_open", "water_level", "motion"]}},
            ["device", "property"])},
        {"name": "list_devices", "inputSchema": _obj({})},
        {"name": "board_health", "inputSchema": _obj({})},
    ],
    "local": [
        {"name": "notify_user", "inputSchema": _obj({"text": {"type": "string", "minLength": 1, "maxLength": 500},
                                                     "level": {"enum": ["info", "warning", "alarm"]}}, ["text"])},
        {"name": "create_rule", "inputSchema": _obj({"name": {"type": "string"}, "when": {"type": "string"},
                                                     "then": {"type": "string"}}, ["when", "then"])},
        {"name": "recent_events", "inputSchema": _obj({"k": {"type": "integer", "minimum": 1, "maximum": 50}}, ["k"])},
    ],
}}


@dataclass
class Scenario:
    name: str
    trace: list[dict]
    gate: list[dict]
    tools: dict = field(default_factory=lambda: copy.deepcopy(CATALOG))
    declared: dict | None = None
    linked: bool = False
    concurrent: bool = False

    def trace_lines(self) -> list[str]:
        return to_lines(self.trace)

    def gate_lines(self) -> list[str]:
        return to_lines(self.gate)


def to_lines(records) -> list[str]:
    out = []
    for r in records:
        out.append(r if isinstance(r, str) else json.dumps(r, ensure_ascii=False, sort_keys=True))
    return out


# ---------------------------------------------------------------------------------------------------------

class _Sim:
    """Один прогон агента на виртуальных часах."""

    def __init__(self, seed: int, linked: bool, concurrent: bool, hours: float, tight_budget: bool):
        self.rng = random.Random(seed)
        self.linked = linked
        self.concurrent = concurrent
        # Стиль связей связанного режима: как edgectx (wake_id; вызовы LLM — по wake_id хода, input_ids)
        # или явный (turn_id; вызовы — по req_id ответа).
        self.link_field = "wake_id" if seed % 2 == 0 else "turn_id"
        self.call_link = "wake_id" if seed % 4 < 2 else "req_id"
        self.run_id = f"synthetic-{seed}"
        self.max_steps = self.rng.choice([3, 4, 6])
        self.max_in_flight = 2 if concurrent else 1
        self.t0 = 25200.0                       # 07:00
        self.t_end = self.t0 + hours * 3600
        self.wall = 1_791_300_000.0 + seed
        self.seq = 0
        self.trace: list[dict] = []
        self.gate_log: list[dict] = []
        self.ids = {"in": 0, "req": 0, "call": 0, "turn": 0, "cmd": 0}
        if tight_budget:
            self.budget = {"daily_calls": self.rng.randint(25, 45), "daily_input_tokens": self.rng.randint(40_000, 90_000),
                           "degrade_at": 0.8}
        else:
            self.budget = {"daily_calls": 500, "daily_input_tokens": 2_000_000, "degrade_at": 0.8}
        self.calls_today = 0
        self.tokens_today = 0
        devices, interlocks = demo_setup()
        self.store = proto.StateStore()
        self.gate = proto.Gate(devices, interlocks, self.store)
        self.state = {"window_open": False, "temp_c": 22.0, "water_level_pct": 60.0, "human_near": False}
        self.state_ts = {k: -1e9 for k in self.state}
        self.renew_queue: list[float] = []      # запланированные продления аренды вентилятора
        self.pulse_ends: dict[str, tuple[float, float]] = {}   # устройство -> (срок импульса, момент pulse_end)
        self.recent_cmds: list[dict] = []       # для повторов cmd_id (идемпотентность)
        self.now = self.t0
        self.req_turn: dict[str, str] = {}

    # --- запись ------------------------------------------------------------------------------------
    def nid(self, kind, prefix):
        self.ids[kind] += 1
        return f"{prefix}{self.ids[kind]}"

    def emit(self, typ, advance_ms: float = None, **fields):
        self.wall += (advance_ms if advance_ms is not None else self.rng.uniform(0.05, 3.0)) / 1000
        self.seq += 1
        rec = {"type": typ, "run_id": self.run_id, "seq": self.seq, "ts_wall": round(self.wall, 6),
               "ts_clock": round(self.now, 6), **fields}
        self.trace.append(rec)
        return rec

    def glog(self, typ, t=None, **fields):
        self.gate_log.append({"type": typ, "ts_clock": round(self.now if t is None else t, 6),
                              "ts_wall": round(self.wall, 6), **fields})

    def flush_gate(self, upto):
        """Продления аренды, сторожевой тик (раз в 0,5 с) и концы импульсов до момента upto — в порядке времени."""
        while True:
            lease = self.gate._leases.get("fan")
            nxt = None                                # следующее событие аренды: (время, вид)
            if lease is None:
                self.renew_queue.clear()
            else:
                tick = (int(lease / 0.5) + 1) * 0.5   # первый тик после истечения
                renew = self.renew_queue[0] if self.renew_queue else None
                nxt = (renew, "renew") if renew is not None and renew < lease and renew < tick else (tick, "tick")
            if self.pulse_ends:
                dev, (scheduled, t_end) = min(self.pulse_ends.items(), key=lambda kv: kv[1][1])
                if t_end <= upto and (nxt is None or t_end <= nxt[0]):
                    del self.pulse_ends[dev]
                    self.glog("pulse_end", t_end, device=dev, scheduled=scheduled, ok=True)
                    continue
            if nxt is None or nxt[0] > upto:
                return
            if nxt[1] == "renew":
                self.renew_queue.pop(0)
                self.gate.renew("fan", nxt[0])
                self.glog("lease", nxt[0], device="fan", op="renew", ok=True,
                          expires_at=round(self.gate._leases["fan"], 6))
                continue
            self.renew_queue.clear()
            for ev in self.gate.tick(nxt[0]):
                self.glog("safe_state", nxt[0], device=ev[1], state=ev[2], cause="lease_expired")

    # --- инструменты --------------------------------------------------------------------------------
    def pick_call(self):
        """Вызов, который «предлагает модель»: команды приводам (иногда недопустимые), чтения, локальные."""
        r = self.rng.random()
        if r < 0.55:
            dev = self.rng.choice(["heater", "pump", "valve", "fan", "door", "led", "thermo", "camera", "fan", "valve",
                                   "relay"])
            action, args = {
                "heater": ("set_power", {"on": self.rng.random() < 0.7}),
                "pump": ("set_power", {"on": self.rng.random() < 0.6}),
                "valve": ("set_angle", {"deg": self.rng.choice([0, 15, 45, 90, 120, 45.0])}),
                "fan": ("set_speed", {"pct": self.rng.choice([0, 20, 30, 60, 100])}),
                "door": (self.rng.choice(["lock", "unlock"]), {}),
                "led": ("set_color", {"r": self.rng.randint(0, 300), "g": 10, "b": 20}),
                "thermo": ("read", {}),
                "camera": ("capture", {"w": self.rng.choice([320, 640, 4000])}),
                "relay": ("pulse", {"seconds": self.rng.choice([0.5, 1, 2, 5, 2.5, 0, 4000])}),
            }[dev]
            if self.rng.random() < 0.03:
                action = "explode"  # нет в реестре: шлюз откажет unknown_tool
            return "hw", "invoke_action", {"device": dev, "action": action, "args": args}
        if r < 0.7:
            return "hw", "read_property", {"device": self.rng.choice(["room", "tank"]),
                                           "property": self.rng.choice(["temperature", "water_level"])}
        if r < 0.75:
            return "hw", "board_health", {}
        if r < 0.9:
            return "local", "notify_user", {"text": "Проверьте помещение", "level": self.rng.choice(["info", "warning"])}
        if r < 0.95:
            return "local", "recent_events", {"k": self.rng.randint(1, 20)}
        return "local", "create_rule", {"when": "door.open after 22:00", "then": "notify"}

    def refresh_state(self, keys):
        """Фоновый сэмплер hw-mcp: обычно свежие значения есть, иногда сэмплер отстал (stale_state)."""
        if self.rng.random() < 0.15:
            return
        for k in keys:
            ts = round(self.now - self.rng.uniform(0, 3.0), 6)
            if ts <= self.state_ts[k]:
                continue
            self.state_ts[k] = ts
            self.store.set(k, self.state[k], ts)
            self.glog("state", key=k, value=self.state[k], ts=ts)

    def gate_decide(self, call_id, cmd_id, device, action, args, issued_at, source):
        """Решение прототипа шлюза + запись в журнал. Возвращает True, если команда исполнена."""
        self.flush_gate(self.now)
        if (device, action) in NEEDS:
            self.refresh_state(NEEDS[(device, action)])
        confirmed = action == "unlock" and self.rng.random() < 0.5
        cmd = proto.Command(cmd_id, device, action, args, issued_at, None, confirmed)
        dec = self.gate.check(cmd, self.now)
        executed = dec.verdict in ("allow", "clamp")
        snap = {}
        for k in NEEDS.get((device, action), []):
            got = self.store.get(k)
            if got is not None:
                snap[k] = {"value": got[0], "ts": got[1]}
        lease_until = None
        dev = self.gate.devices.get(device)
        if executed and dev is not None and dev.lease_s is not None and action not in dev.read_only:
            lease_until = round(self.gate._leases[device], 6)
        pulse_until = None
        if executed and dev is not None and action not in dev.read_only:
            # как hw-mcp (hub.applyLocked): исполненная команда отменяет незавершённый импульс выхода
            self.pulse_ends.pop(device, None)
            if action == "pulse":
                pulse_until = round(self.now + dec.args["seconds"], 6)
                # конец импульса пишет таймер выхода в срок, с задержкой в миллисекунды
                self.pulse_ends[device] = (pulse_until, round(pulse_until + self.rng.uniform(0.001, 0.02), 6))
        self.glog("gate_decision", cmd_id=cmd_id, call_id=call_id, mcp_tool="invoke_action",
                  mcp_args={"device": device, "action": action, "args": args}, device=device, tool=action,
                  args=args, issued_at=issued_at, ttl_s=None, confirmed=confirmed, verdict=dec.verdict,
                  reason=dec.reason, final_args=dec.args, executed=executed, dry_run=False, state=snap,
                  lease_until=lease_until, pulse_until=pulse_until, source=source)
        if executed:
            self.recent_cmds.append({"cmd_id": cmd_id, "args": {"device": device, "action": action, "args": args}})
            if lease_until is not None:
                # новая команда сбрасывает аренду; клиент продлит её 0–2 раза до истечения
                self.renew_queue.clear()
                t = self.now
                for _ in range(self.rng.randint(0, 2)):
                    t = round(t + self.rng.uniform(0.5, 1.5), 6)
                    self.renew_queue.append(t)
        return executed

    def do_call(self, server, name, args, *, req_id=None, rule_id=None, issued_at=None):
        call_id = self.nid("call", "call-")
        cmd_id = None
        if server == "hw" and name == "invoke_action":
            if self.recent_cmds and source_is_llm(rule_id) and self.rng.random() < 0.05:
                prev = self.rng.choice(self.recent_cmds)   # модель повторила команду, оркестратор дал тот же ключ
                cmd_id, args = prev["cmd_id"], copy.deepcopy(prev["args"])
            else:
                cmd_id = self.nid("cmd", "cmd-")
        extra = {}
        if self.linked:
            if req_id is not None:
                if self.call_link == "req_id":
                    extra["req_id"] = req_id
                else:
                    extra.update({self.link_field: self.req_turn[req_id], "by": "llm"})
            if rule_id is not None:
                extra.update(rule_id=rule_id, by="rule")
        recs = [("tool_call", dict(call_id=call_id, server=server, name=name, args=args, **extra))]
        ok, denied = True, False
        if cmd_id is not None:
            executed = self.gate_decide(call_id, cmd_id, args["device"], args["action"], args.get("args", {}),
                                        issued_at if issued_at is not None else self.now,
                                        "rule" if rule_id else "llm")
            ok, denied = executed, not executed
        recs.append(("tool_result", dict(call_id=call_id, ok=ok, denied=denied, ms=round(self.rng.uniform(1, 40), 3))))
        return recs

    # --- ход ---------------------------------------------------------------------------------------
    def turn_records(self, reason, input_id):
        """Записи одного хода (без wake): список (тип, поля, задержка_мс)."""
        turn_id = self.nid("turn", "t") if self.linked else None
        steps = self.rng.randint(1, self.max_steps)
        out = []
        used = 0
        for step in range(steps):
            if used >= self.max_steps:
                break
            final = step == steps - 1 or used == self.max_steps - 1
            # иногда запрос к LLM падает (таймаут) и повторяется тем же ходом
            if used < self.max_steps - 1 and not final and self.rng.random() < 0.05:
                rid = self.nid("req", "r")
                out.append(("llm_request", self.request_fields(rid, turn_id), 1))
                out.append(("error", dict(where="llm", msg="timeout", req_id=rid), 30000))
                used += 1
                final = final or used == self.max_steps - 1
            rid = self.nid("req", "r")
            req = self.request_fields(rid, turn_id)
            out.append(("llm_request", req, 1))
            used += 1
            calls = [] if final else [self.pick_call() for _ in range(self.rng.choice([1, 1, 2, 3]))]
            latency = round(self.rng.uniform(300, 2500), 3)
            pt = int(req["prompt_tokens_est"] * self.rng.uniform(0.9, 1.15))
            usage = {"prompt_tokens": pt, "completion_tokens": self.rng.randint(5, 300),
                     "cached_tokens": self.rng.choice([0, None, min(pt, 1024 * self.rng.randint(0, 3))])}
            out.append(("llm_response", dict(req_id=rid, latency_ms=latency, usage=usage,
                                             finish_reason="tool_calls" if calls else "stop",
                                             n_tool_calls=len(calls)), latency + 1))
            for server, name, args in calls:
                out.append(("call", dict(server=server, name=name, args=args, req_id=rid), 1))
        if reason == "user":
            out.append(("user_reply", dict(input_id=input_id, text="Готово."), 2))
        elif reason in ("gate", "rule") and self.rng.random() < 0.4:
            out.append(("notify", dict(text="Внимание: событие в помещении", situation_id=None), 2))
        return turn_id, out

    def request_fields(self, rid, turn_id):
        seg = {"prefix": self.rng.randint(800, 1500), "profile": self.rng.randint(20, 120),
               "history": self.rng.randint(0, 1500), "state": self.rng.randint(20, 300), "turn": self.rng.randint(10, 200)}
        f = dict(req_id=rid, model="openai/gpt-5-mini", prompt_tokens_est=sum(seg.values()) + 12, segments=seg,
                 n_tools=7)
        if turn_id is not None:
            f[self.link_field] = turn_id
            self.req_turn[rid] = turn_id
        return f

    def play_turn_records(self, recs, input_ids):
        """Записать подготовленные записи хода (возможно перемешанные с другим ходом)."""
        for typ, fields, delay in recs:
            if typ == "call":
                f = dict(fields)
                rid = f.pop("req_id")
                for t2, f2 in self.do_call(f["server"], f["name"], f["args"], req_id=rid,
                                           issued_at=round(self.now - self.rng.choice([0, 0, 0.5, 1.0, 12.0]), 6)):
                    self.emit(t2, **f2)
                continue
            if typ == "user_reply":
                lat = (self.wall - input_ids[fields["input_id"]]) * 1000 + delay
                fields = dict(fields, latency_ms=round(max(lat, 0), 3))
            if typ == "llm_response":
                self.emit(typ, advance_ms=delay, **fields)
                self.calls_today += 1
                self.tokens_today += fields["usage"]["prompt_tokens"]
                continue
            self.emit(typ, advance_ms=delay, **fields)

    def admit(self, reason):
        """Планировщик edgectx: user — всегда; при исчерпании — только user; с degrade_at — без фона."""
        if reason == "user":
            return True
        f = max(self.calls_today / self.budget["daily_calls"], self.tokens_today / self.budget["daily_input_tokens"])
        if f >= 1:
            return False
        if reason == "heartbeat" and f >= self.budget["degrade_at"]:
            return False
        return True

    # --- день --------------------------------------------------------------------------------------
    def events(self):
        evs = []
        t = self.t0 + self.rng.uniform(30, 300)
        while t < self.t_end:
            evs.append((t, "user"))
            t += self.rng.expovariate(1 / 600)
        t = self.t0 + 60
        while t < self.t_end:
            evs.append((t, "sensor"))
            t += self.rng.uniform(60, 240)
        t = self.t0 + 1800
        while t < self.t_end:
            evs.append((t, "tick"))
            t += 1800
        for _ in range(self.rng.randint(0, 2)):
            evs.append((self.rng.uniform(self.t0, self.t_end), "situation"))
        for _ in range(self.rng.randint(0, 2)):
            evs.append((self.rng.uniform(self.t0, self.t_end), "summary"))
        evs.sort()
        return evs

    def run(self) -> Scenario:
        policy = {"wake": "hybrid", "heartbeat_min": 30, "max_steps": self.max_steps, "history": "mask",
                  "hwctx": "delta_tail", "catalog": "generic"}
        self.emit("run_start", config_hash="sha256:" + "ab" * 16, policy=policy, budget=self.budget,
                  sched={"max_in_flight": self.max_in_flight, "queue_max": 64})
        self.glog("gate_config", t=self.t0, config=copy.deepcopy(DECLARED_GATE), watchdog_tick_s=1.0)
        evs = self.events()
        input_walls = {}
        pending_user = None   # в режиме конкуренции: ход пользователя, который перемешается со следующим ходом
        for i, (t, kind) in enumerate(evs):
            self.next_event_t = evs[i + 1][0] if i + 1 < len(evs) else self.t_end
            self.flush_gate(t)
            self.now = round(t, 6)
            if kind == "summary":
                rid = self.nid("req", "r")
                f = self.request_fields(rid, None)
                f["purpose"] = "summary"
                self.emit("llm_request", **f)
                self.emit("llm_response", advance_ms=800, req_id=rid, latency_ms=700.0,
                          usage={"prompt_tokens": f["prompt_tokens_est"], "completion_tokens": 120, "cached_tokens": 0},
                          finish_reason="stop", n_tool_calls=0)
                self.calls_today += 1
                self.tokens_today += f["prompt_tokens_est"]
                continue
            iid = self.nid("in", {"user": "u", "sensor": "s", "tick": "hb", "situation": "sit"}[kind])
            self.emit("input", kind=kind, id=iid)
            input_walls[iid] = self.wall
            if kind == "sensor":
                key = self.rng.choice(list(self.state))
                self.state[key] = {"window_open": not self.state["window_open"],
                                   "temp_c": round(self.state["temp_c"] + self.rng.uniform(-3, 4), 1),
                                   "water_level_pct": round(self.rng.uniform(0, 100), 1),
                                   "human_near": not self.state["human_near"]}[key]
                self.state_ts[key] = self.now
                self.store.set(key, self.state[key], self.now)
                self.glog("state", key=key, value=self.state[key], ts=self.now)
                r = self.rng.random()
                if r < 0.55:
                    self.emit("suppress", reason=self.rng.choice(["debounce", "cooldown", "below_threshold"]), input_id=iid)
                    continue
                if r < 0.75:
                    rule_id = self.rng.choice(["rule-fan-off", "rule-led"])
                    # как edgectx (agent/internal/gate): вход, обработанный правилом, получает исход rule_handled
                    self.emit("suppress", reason="rule_handled", input_id=iid)
                    self.emit("rule_fired", rule_id=rule_id, input_id=iid)
                    args = ({"device": "fan", "action": "set_speed", "args": {"pct": 0}} if rule_id == "rule-fan-off"
                            else {"device": "led", "action": "set_color", "args": {"r": 0, "g": 0, "b": 255}})
                    for t2, f2 in self.do_call("hw", "invoke_action", args, rule_id=rule_id):
                        self.emit(t2, **f2)
                    continue
                reason = "gate"
            else:
                reason = {"user": "user", "tick": "heartbeat", "situation": "gate"}[kind]
            if not self.admit(reason):
                why = "budget_exhausted" if reason != "heartbeat" else "budget_degraded"
                if self.linked:  # как edgectx: отклонённое пробуждение + suppress по каждому входу
                    wid = self.nid("turn", "w")
                    self.emit("wake", reason=reason, suppressed=True, suppress_reason=why, input_ids=[iid],
                              priority="background" if reason == "heartbeat" else "safety", **{self.link_field: wid})
                    self.emit("suppress", reason=why, input_id=iid, wake_id=wid)
                else:
                    self.emit("suppress", reason=why, input_id=iid)
                continue
            turn_id, recs = self.turn_records(reason, iid)
            wake = dict(reason=reason, suppressed=False)
            if self.linked:
                wake["input_ids"] = [iid]
                wake["priority"] = {"user": "user", "heartbeat": "background"}.get(reason, "safety")
            else:
                wake["input_id"] = iid
            if turn_id is not None:
                wake[self.link_field] = turn_id
            if reason == "gate" and not self.linked and self.rng.random() < 0.5:
                wake["priority"] = "safety"
            if self.concurrent and pending_user is None and reason == "user" and self.rng.random() < 0.5:
                self.emit("wake", **wake)
                pending_user = recs        # отложить: перемешается со следующим ходом
                continue
            self.emit("wake", **wake)
            if pending_user is not None:
                recs = self.interleave(pending_user, recs)
                pending_user = None
            self.play_turn_records(recs, input_walls)
        if pending_user is not None:
            self.play_turn_records(pending_user, input_walls)
        self.now = round(self.t_end, 6)
        self.flush_gate(self.t_end + 10)
        self.emit("run_end")
        return Scenario(self.run_id, self.trace, self.gate_log, linked=self.linked, concurrent=self.concurrent)

    def interleave(self, a, b):
        """Слить записи двух ходов, сохраняя порядок внутри каждого (не больше двух запросов в полёте)."""
        out, i, j = [], 0, 0
        while i < len(a) or j < len(b):
            if j >= len(b) or (i < len(a) and self.rng.random() < 0.5):
                out.append(a[i])
                i += 1
            else:
                out.append(b[j])
                j += 1
        return out


def source_is_llm(rule_id) -> bool:
    return rule_id is None


def generate(seed: int, linked: bool = False, concurrent: bool = False, hours: float = 2.0,
             tight_budget: bool | None = None) -> Scenario:
    rng = random.Random(seed * 7919 + 1)
    if tight_budget is None:
        tight_budget = rng.random() < 0.4
    sc = _Sim(seed, linked, concurrent, hours, tight_budget).run()
    sc.name = f"seed{seed}{'-linked' if linked else ''}{'-concurrent' if concurrent else ''}"
    return sc


def corpus(n: int = 12) -> list[Scenario]:
    """Корпус корректных прогонов: позиционный и связанный режимы, последовательный и с двумя ходами в полёте."""
    out = []
    for seed in range(n):
        out.append(generate(seed, linked=False))
        out.append(generate(seed, linked=True))
        if seed % 3 == 0:
            out.append(generate(seed, linked=True, concurrent=True))
            out.append(generate(seed, linked=False, concurrent=True))
    return out


def write_examples(out_dir: str | Path, seed: int = 3) -> list[Path]:
    """Файлы примеров для README и тестов CLI: корректный прогон и испорченный несколькими мутантами."""
    from monitor.tests.mutants import MUTANTS, clone  # импорт здесь: mutants импортирует этот модуль

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sc = generate(seed, linked=True, tight_budget=True)
    bad = clone(sc)
    rng = random.Random(seed)
    for name in ("drop_user_reply", "arg_bounds", "exec_out_of_range", "watchdog_missed", "unchecked_call"):
        m = next(m for m in MUTANTS if m.name == name)
        bad = m.apply(bad, rng) or bad
    files = {
        "trace.jsonl": "\n".join(sc.trace_lines()) + "\n",
        "hwmcp_gate.jsonl": "\n".join(sc.gate_lines()) + "\n",
        "trace_bad.jsonl": "\n".join(bad.trace_lines()) + "\n",
        "hwmcp_gate_bad.jsonl": "\n".join(bad.gate_lines()) + "\n",
        "tools.json": json.dumps(CATALOG, ensure_ascii=False, indent=1) + "\n",
        "declared_gate.json": json.dumps({"gate": DECLARED_GATE}, ensure_ascii=False, indent=1) + "\n",
    }
    paths = []
    for name, text in files.items():
        p = out / name
        p.write_text(text, encoding="utf-8")
        paths.append(p)
    return paths


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Синтетические прогоны для монитора")
    ap.add_argument("--examples", default=str(ROOT / "monitor" / "examples"), help="каталог файлов примеров")
    ap.add_argument("--seed", type=int, default=3)
    a = ap.parse_args()
    for p in write_examples(a.examples, a.seed):
        print(p)
