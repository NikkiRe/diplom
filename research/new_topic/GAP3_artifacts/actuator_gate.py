#!/usr/bin/env python3
"""Пилот локального слоя безопасности команд исполнительным устройствам (заметка GAP3).

Это НЕ компонент агента, а макет для оценки порядка величин: сколько стоит детерминированная проверка
одной команды на хосте и как выглядят классы отказов. Слой стоит между LLM (внешней, недоверенной) и
MCP-инструментами актуаторов и реализует инженерные шаблоны из обзора:

  1. идемпотентность по ключу команды (повтор не приводит к повторному срабатыванию привода);
  2. реестр устройств и инструментов (команда к несуществующему устройству — класс HomeBench «invalid»);
  3. проверка аргументов по JSON Schema (типы, диапазоны) — переиспользуется monitor/argschema.py;
  4. TTL команды: решение LLM устаревает (Gat 1998; Slow Brain 2026; cmd_vel_timeout в ROS 2);
  5. свежесть состояния: блокировки проверяются только по свежим показаниям, неизвестное = опасное;
  6. блокировки (interlocks) — предикаты над состоянием; часть из них не отклоняет, а ограничивает
     команду («пропорциональное соблюдение»: скорость не выше 30 % рядом с человеком, как S2 у
     Bajrami et al. 2026, arXiv:2609.07288);
  7. ограничение частоты команд на привод (минимальный интервал и окно в 60 с) — MCP 2026-07-28 требует
     от серверов «Rate limit tool invocations»;
  8. подтверждение пользователя для отдельных инструментов (confirm у PicoClaw, user_inspection у AgentSpec);
  9. dry-run и commit: план проверяется заранее, а при фиксации проверяется заново (защита от TOCTOU),
     по образцу NETCONF candidate/confirmed-commit (RFC 6241, п. 8.3–8.4);
 10. аренда (lease) и сторожевой таймер для непрерывных приводов: без продления привод уходит в
     безопасное состояние (Spot E-Stop/Lease, twist_mux timeout, systemd WatchdogSec=).

Зависимости: только стандартная библиотека и monitor/argschema.py из этого репозитория.
"""
from __future__ import annotations

import sys
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[3]  # .../diploma-edge-ai
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor import argschema  # noqa: E402

VERDICTS = ("allow", "clamp", "confirm", "duplicate", "reject")
REASONS = (
    "unknown_device", "unknown_tool", "invalid_args", "stale_command", "stale_state",
    "interlock", "rate_limit", "needs_confirm", "duplicate", "commit_expired",
)


@dataclass(frozen=True)
class Command:
    cmd_id: str            # ключ идемпотентности (генерирует оркестратор, не LLM)
    device: str
    tool: str
    args: dict
    issued_at: float       # момент, к которому относится решение LLM (часы хоста, с)
    ttl_s: Optional[float] = None
    confirmed: bool = False


@dataclass
class Decision:
    verdict: str
    reason: Optional[str] = None
    detail: str = ""
    args: Optional[dict] = None   # итоговые аргументы (после clamp)


@dataclass
class Interlock:
    name: str
    applies: Callable[[Command], bool]
    needs: tuple                                          # ключи состояния, от которых зависит проверка
    check: Callable[[dict, dict], Optional[str]]          # (args, state) -> None | причина отказа
    clamp: Optional[Callable[[dict, dict], dict]] = None  # если задано — вместо отказа ограничить аргументы


@dataclass
class Device:
    name: str
    tools: dict                                  # инструмент -> JSON Schema аргументов
    read_only: frozenset = frozenset()           # инструменты чтения (датчики) — без проверок актуации
    min_interval_s: float = 0.0                  # мин. интервал между принятыми командами (реле, компрессор)
    max_per_min: Optional[int] = None            # не больше N принятых команд за скользящие 60 с
    requires_confirm: frozenset = frozenset()    # инструменты, требующие подтверждения пользователя
    lease_s: Optional[float] = None              # непрерывный привод: команда действует, пока продлевается
    safe_state: Optional[dict] = None            # что выставить при истечении аренды / сторожевого таймера


class StateStore:
    """Контекст оборудования: последнее значение и время его получения."""

    def __init__(self) -> None:
        self._v: dict = {}

    def set(self, key: str, value, ts: float) -> None:
        self._v[key] = (value, ts)

    def get(self, key: str):
        return self._v.get(key)


@dataclass
class Gate:
    devices: dict
    interlocks: list
    state: StateStore
    default_ttl_s: float = 10.0
    max_state_age_s: float = 5.0
    commit_window_s: float = 5.0
    idem_capacity: int = 4096
    _accepted: dict = field(default_factory=dict)        # device -> deque[ts] принятых команд
    _seen: OrderedDict = field(default_factory=OrderedDict)  # cmd_id -> Decision (LRU)
    _leases: dict = field(default_factory=dict)          # device -> время истечения аренды
    safe_events: deque = field(default_factory=lambda: deque(maxlen=1024))  # (ts, device, safe_state); полный журнал — во внешнюю трассу
    safe_count: int = 0
    _by_target: dict = field(default_factory=dict)       # (device, tool) -> [Interlock], кэш

    # --- основная проверка ------------------------------------------------------------------------------
    def check(self, cmd: Command, now: float, *, dry_run: bool = False) -> Decision:
        prev = self._seen.get(cmd.cmd_id)
        if prev is not None:
            return Decision("duplicate", "duplicate", "команда с этим ключом уже исполнена", prev.args)

        dev = self.devices.get(cmd.device)
        if dev is None:
            return Decision("reject", "unknown_device", cmd.device)
        schema = dev.tools.get(cmd.tool)
        if schema is None:
            return Decision("reject", "unknown_tool", f"{cmd.device}.{cmd.tool}")
        errs = argschema.validate(cmd.args, schema, strict_integer=True)
        if errs:
            return Decision("reject", "invalid_args", "; ".join(str(e) for e in errs[:3]))
        if cmd.tool in dev.read_only:
            return Decision("allow", args=cmd.args)

        ttl = cmd.ttl_s if cmd.ttl_s is not None else self.default_ttl_s
        if now - cmd.issued_at > ttl:
            return Decision("reject", "stale_command", f"возраст {now - cmd.issued_at:.1f} с > TTL {ttl} с")

        args = cmd.args
        clamped = False
        for il in self._interlocks_for(cmd):
            snap = {}
            for key in il.needs:
                rec = self.state.get(key)
                if rec is None or now - rec[1] > self.max_state_age_s:
                    return Decision("reject", "stale_state", f"{il.name}: нет свежего значения {key}")
                snap[key] = rec[0]
            why = il.check(args, snap)
            if why is not None:
                if il.clamp is None:
                    return Decision("reject", "interlock", f"{il.name}: {why}")
                args = il.clamp(args, snap)
                clamped = True

        dq = self._accepted.get(cmd.device)
        if dq:
            if dev.min_interval_s and now - dq[-1] < dev.min_interval_s:
                return Decision("reject", "rate_limit", f"интервал {now - dq[-1]:.1f} с < {dev.min_interval_s} с")
            if dev.max_per_min is not None:
                while dq and now - dq[0] >= 60.0:
                    dq.popleft()
                if len(dq) >= dev.max_per_min:
                    return Decision("reject", "rate_limit", f"{len(dq)} команд за 60 с")

        if cmd.tool in dev.requires_confirm and not cmd.confirmed:
            return Decision("confirm", "needs_confirm", f"{cmd.device}.{cmd.tool} требует подтверждения")

        dec = Decision("clamp" if clamped else "allow", args=args)
        if not dry_run:
            self._record(cmd, dev, dec, now)
        return dec

    def _interlocks_for(self, cmd: Command):
        key = (cmd.device, cmd.tool)
        lst = self._by_target.get(key)
        if lst is None:
            lst = [il for il in self.interlocks if il.applies(cmd)]
            self._by_target[key] = lst
        return lst

    def _record(self, cmd: Command, dev: Device, dec: Decision, now: float) -> None:
        # История ограничена: хранится не больше max_per_min отметок (или одна — для min_interval).
        # Неограниченная deque здесь — утечка памяти, видимая только в долгом прогоне (наклон RSS).
        dq = self._accepted.get(cmd.device)
        if dq is None:
            dq = self._accepted[cmd.device] = deque(maxlen=dev.max_per_min or 1)
        dq.append(now)
        self._seen[cmd.cmd_id] = dec
        if len(self._seen) > self.idem_capacity:
            self._seen.popitem(last=False)
        if dev.lease_s is not None:
            self._leases[cmd.device] = now + dev.lease_s

    # --- dry-run / commit ----------------------------------------------------------------------------------
    def dry_run(self, cmd: Command, now: float):
        """Проверка без исполнения. Возвращает (решение, токен для commit или None)."""
        dec = self.check(cmd, now, dry_run=True)
        token = (cmd, now + self.commit_window_s) if dec.verdict in ("allow", "clamp") else None
        return dec, token

    def commit(self, token, now: float) -> Decision:
        cmd, expires = token
        if now > expires:
            return Decision("reject", "commit_expired", "окно фиксации истекло")
        return self.check(cmd, now)  # повторная проверка: состояние могло измениться (TOCTOU)

    # --- аренда и сторожевой таймер ----------------------------------------------------------------------
    def renew(self, device: str, now: float) -> bool:
        dev = self.devices.get(device)
        if dev is None or dev.lease_s is None or device not in self._leases:
            return False
        self._leases[device] = now + dev.lease_s
        return True

    def tick(self, now: float) -> list:
        """Вызывается периодически (и при каждом событии). Истёкшая аренда -> безопасное состояние."""
        fired = []
        for name, exp in list(self._leases.items()):
            if now >= exp:
                del self._leases[name]
                ev = (now, name, self.devices[name].safe_state)
                self.safe_events.append(ev)
                self.safe_count += 1
                fired.append(ev)
        return fired


# --- демонстрационный стенд: «умная комната / лаборатория» на одноплатнике ---------------------------------
def demo_setup():
    b = {"type": "boolean"}
    devices = {
        "heater": Device("heater", {"set_power": {"type": "object", "properties": {"on": b},
                                                  "required": ["on"], "additionalProperties": False}},
                         min_interval_s=30.0, safe_state={"on": False}),
        "pump": Device("pump", {"set_power": {"type": "object", "properties": {"on": b},
                                              "required": ["on"], "additionalProperties": False}},
                       min_interval_s=10.0, safe_state={"on": False}),
        "valve": Device("valve", {"set_angle": {"type": "object", "properties": {
                            "deg": {"type": "integer", "minimum": 0, "maximum": 90}},
                            "required": ["deg"], "additionalProperties": False}},
                        max_per_min=20, safe_state={"deg": 0}),
        "fan": Device("fan", {"set_speed": {"type": "object", "properties": {
                          "pct": {"type": "integer", "minimum": 0, "maximum": 100}},
                          "required": ["pct"], "additionalProperties": False}},
                      lease_s=2.0, safe_state={"pct": 0}),
        "door": Device("door", {"lock": {"type": "object", "additionalProperties": False},
                                "unlock": {"type": "object", "additionalProperties": False}},
                       requires_confirm=frozenset({"unlock"})),
        "led": Device("led", {"set_color": {"type": "object", "properties": {
                          c: {"type": "integer", "minimum": 0, "maximum": 255} for c in "rgb"},
                          "required": ["r", "g", "b"], "additionalProperties": False}}),
        "thermo": Device("thermo", {"read": {"type": "object", "additionalProperties": False}},
                         read_only=frozenset({"read"})),
        "camera": Device("camera", {"capture": {"type": "object", "properties": {
                             "w": {"type": "integer", "minimum": 160, "maximum": 1920}},
                             "additionalProperties": False}}, read_only=frozenset({"capture"})),
    }

    def heater_check(args, s):
        if not args["on"]:
            return None
        if s["window_open"]:
            return "окно открыто"
        if s["temp_c"] >= 30.0:
            return "температура >= 30 °C"
        return None

    def pump_check(args, s):
        return "сухой ход: уровень воды < 10 %" if args["on"] and s["water_level_pct"] < 10.0 else None

    def fan_check(args, s):
        return "человек рядом, скорость > 30 %" if s["human_near"] and args["pct"] > 30 else None

    interlocks = [
        Interlock("heater_window", lambda c: c.device == "heater" and c.tool == "set_power",
                  ("window_open", "temp_c"), heater_check),
        Interlock("pump_dry_run", lambda c: c.device == "pump" and c.tool == "set_power",
                  ("water_level_pct",), pump_check),
        Interlock("fan_human_speed", lambda c: c.device == "fan" and c.tool == "set_speed",
                  ("human_near",), fan_check, clamp=lambda a, s: {**a, "pct": 30}),
    ]
    return devices, interlocks
