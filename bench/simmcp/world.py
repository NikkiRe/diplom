"""Истинное состояние помещения: устройства из заголовка дня, проигрывание событий по часам, приводы.

Мир не знает про MCP: операции возвращают словари-результаты, а изменения состояния — списком
``Change``, по которому сервер рассылает notifications/resources/updated. Время задаёт только
``advance(t)`` (часы агента/реплеера); собственных таймеров реального времени нет, поэтому
ускоренный режим T0 и реальный T2 ведут себя одинаково.

Форматы результатов и ресурсов — как у hw-mcp (hwmcp/internal/server, hub, inventory), чтобы агент и
счёт токенов в T0 совпадали с платой:

* чтение: ``{"device", "property", "value", "unit"?, "age_s"}`` (возраст с точностью 0,1 с);
  ошибка чтения — ``{"error": код, "detail"}``;
* запись/действие: ``{"ok": true, "verdict": "allow", "executed": true, "device", …}``; отказ —
  ``{"denied": true, "verdict": "reject", "reason", "detail", "device"}`` с кодами шлюза hw-mcp
  (unknown_device, unknown_tool, invalid_args, interlock);
* hw://state/<устройство> — ``{"device", "now", "properties": {p: {"value", "unit"?, "ts", "age_s"}}}``;
* hw://inventory — ``{"devices": [{"id", "kind", "location", "properties": {p: {"type", "writable", …}},
  "actions": {a: {"description", "input"}}}]}`` (AgentView); list_devices — CompactView.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Any

RECENT_MAX = 100   # ресурс events: не больше стольких последних изменений


class ClockError(ValueError):
    """Попытка повести часы назад."""


@dataclass(frozen=True)
class Change:
    t: float
    device: str
    property: str
    value: Any
    source: str          # casas:<дом> | synthetic | manual | timer | tool


@dataclass(order=True)
class _Timer:
    t: float
    seq: int
    device: str = field(compare=False)
    values: dict = field(compare=False)
    gen: int = field(compare=False, default=0)


def fail(code: str, detail: str = "") -> dict:
    """Ошибка инструмента чтения (как fail() в hw-mcp): {"error": код, "detail": …}."""
    return {"error": code, "detail": detail}


def reject(reason: str, detail: str, device: str | None = None) -> dict:
    """Отказ привода (как denied() в hw-mcp): {"denied": true, "verdict": "reject", "reason", "detail"}."""
    out: dict[str, Any] = {"denied": True, "verdict": "reject", "reason": reason, "detail": detail}
    if device is not None:
        out["device"] = device
    return out


def is_error(result: dict) -> bool:
    """Результат — ошибка/отказ (isError в MCP)."""
    return bool(result.get("denied")) or "error" in result


def round3(x: float) -> float:
    return round(x, 3)


def round_age(a: float) -> float:
    """Возраст значения для результата чтения — 0,1 с, как roundAge в hw-mcp."""
    return float(int(max(a, 0.0) * 10 + 0.5)) / 10


def prop_sig(spec: dict, rich: bool = False) -> str:
    """Подпись свойства для list_devices.

    По умолчанию — как propSig в hw-mcp: единица («Cel», «%»), «bool» для логических, «,rw» для записываемых
    (у целых/строк без единицы — пусто). ``rich`` — с диапазоном и допустимыми значениями («%,1..100,rw»,
    «amber|blue|…,rw», «0|1»): больше токенов, у hw-mcp такого нет.
    """
    if spec["type"] == "boolean":
        sig = "bool"
    elif rich and "enum" in spec:
        sig = "|".join(str(v) for v in spec["enum"])
    else:
        sig = spec.get("unit") or ""
        if rich:
            sig = sig or {"integer": "int"}.get(spec["type"], spec["type"])
            if "min" in spec or "max" in spec:
                sig += f",{spec.get('min')}..{spec.get('max')}"
    if spec.get("rw"):
        sig += ",rw"
    return sig


def value_schema(spec: dict) -> dict:
    """JSON Schema значения свойства/параметра (для explicit set_* и входа действий)."""
    s: dict[str, Any] = {"type": spec["type"]}
    for k_src, k_dst in (("enum", "enum"), ("min", "minimum"), ("max", "maximum")):
        if k_src in spec:
            s[k_dst] = spec[k_src]
    return s


def action_input(spec: dict) -> dict:
    """JSON Schema аргументов действия (поле input у hw-mcp)."""
    props = {k: value_schema(v) for k, v in sorted(spec.get("params", {}).items())}
    return {"type": "object", "properties": props, "additionalProperties": False}


def action_description(dev: dict, action: str, spec: dict) -> str:
    """Описание действия: из daygen (поле description), иначе имя действия."""
    return spec.get("description") or action.replace("_", " ").capitalize()


def check_value(spec: dict, value: Any) -> tuple[Any, str | None, str]:
    """Проверка значения по схеме свойства/параметра. Возвращает (нормализованное, причина отказа, пояснение).

    Причины — внутренние (invalid_type | invalid_value | out_of_range); наружу (как у шлюза hw-mcp) они
    выдаются кодом invalid_args, а внутренняя причина остаётся в detail.
    """
    typ = spec.get("type")
    if typ == "boolean":
        if not isinstance(value, bool):
            return value, "invalid_type", "expected boolean"
    elif typ == "integer":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return value, "invalid_type", "expected integer"
        if isinstance(value, float):
            if not value.is_integer():
                return value, "invalid_type", "expected integer"
            value = int(value)
    elif typ == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return value, "invalid_type", "expected number"
    elif typ == "string":
        if not isinstance(value, str):
            return value, "invalid_type", "expected string"
    if "enum" in spec and value not in spec["enum"]:
        return value, "invalid_value", f"allowed: {spec['enum']}"
    if "min" in spec and value < spec["min"] or "max" in spec and value > spec["max"]:
        return value, "out_of_range", f"range [{spec.get('min')}; {spec.get('max')}]"
    return value, None, ""


class World:
    def __init__(self, header: dict, events: list[dict], start_t: float | None = None):
        self.header = header
        self.devices: dict[str, dict] = {d["id"]: d for d in header["devices"]}
        self.window = tuple(header["window_s"])
        self.clock = float(self.window[0] if start_t is None else start_t)
        self.state: dict[str, dict[str, dict]] = {}
        for dev, props in header["initial_state"].items():
            self.state[dev] = {p: {"value": v, "t": self.clock} for p, v in props.items()}
        self._events = sorted(events, key=lambda e: e["t"])   # sorted() стабилен: порядок файла сохраняется
        self._next = 0
        self._timers: list[_Timer] = []
        self._seq = 0
        # «поколение» действий устройства: новое действие (start, dock, brew) отменяет таймер предыдущего
        self._gen: dict[str, int] = {}
        # последние изменения (трасса, таймеры, записи агента) с последнего продвижения часов — ресурс events
        self.recent: list[dict] = []

    # ------------------------------------------------------------------ время
    def advance(self, t: float) -> tuple[list[Change], list[dict]]:
        """Продвинуть часы до t: применить события дня и срабатывания таймеров с моментом ≤ t по порядку.

        Возвращает (изменения состояния, «входы» дня — user_cmd/question/situation — с t ≤ новых часов).
        """
        t = float(t)
        if t < self.clock:
            raise ClockError(f"часы назад: {t} < {self.clock}")
        changes: list[Change] = []
        inputs: list[dict] = []
        while True:
            ev_t = self._events[self._next]["t"] if self._next < len(self._events) else math.inf
            tm_t = self._timers[0].t if self._timers else math.inf
            nxt = min(ev_t, tm_t)
            if nxt > t:
                break
            if tm_t <= ev_t:
                tm = heapq.heappop(self._timers)
                self.clock = max(self.clock, tm.t)
                if self._gen.get(tm.device) != tm.gen:
                    continue
                for p, v in tm.values.items():
                    if self._set(tm.device, p, v, tm.t):
                        changes.append(Change(tm.t, tm.device, p, v, "timer"))
                continue
            ev = self._events[self._next]
            self._next += 1
            self.clock = max(self.clock, ev["t"])
            if ev["kind"] == "sensor":
                if ev["device"] in self.devices and self._set(ev["device"], ev["property"], ev["value"], ev["t"]):
                    changes.append(Change(ev["t"], ev["device"], ev["property"], ev["value"], ev.get("source", "")))
            else:
                inputs.append(ev)
        self.clock = t
        self.recent = []
        self.note_recent(changes)
        return changes, inputs

    def note_recent(self, changes: list[Change]) -> None:
        """Дописать изменения в ресурс events (не больше RECENT_MAX последних)."""
        if not changes:
            return
        self.recent.extend({"t": c.t, "device": c.device, "property": c.property, "value": c.value,
                            "source": c.source} for c in changes[-RECENT_MAX:])
        del self.recent[:-RECENT_MAX]

    @property
    def finished(self) -> bool:
        return self._next >= len(self._events)

    # ------------------------------------------------------------------ состояние
    def _set(self, dev: str, prop: str, value: Any, t: float) -> bool:
        cell = self.state.setdefault(dev, {}).setdefault(prop, {"value": None, "t": t})
        if cell["value"] == value and type(cell["value"]) is type(value):
            return False
        cell["value"] = value
        cell["t"] = t
        return True

    def value(self, dev: str, prop: str) -> Any:
        return self.state[dev][prop]["value"]

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {d: {p: c["value"] for p, c in sorted(props.items())} for d, props in sorted(self.state.items())}

    def device_state(self, dev: str) -> dict:
        """Ресурс hw://state/<устройство> в формате hw-mcp (hub.DeviceState / readingView)."""
        specs = self.devices[dev]["properties"]
        props: dict[str, dict] = {}
        for p, c in sorted(self.state[dev].items()):
            v: dict[str, Any] = {"value": c["value"], "ts": c["t"], "age_s": round3(self.clock - c["t"])}
            unit = specs.get(p, {}).get("unit")
            if unit:
                v["unit"] = unit
            props[p] = v
        return {"device": dev, "now": self.clock, "properties": props}

    # ------------------------------------------------------------------ инвентарь
    def inventory(self) -> dict:
        """Ресурс hw://inventory — полное описание для агента (как AgentView в hw-mcp).

        Свойство: type, writable, unit?, а также min/max/enum/resolution, если они есть в схеме daygen
        (edgectx берёт из них диапазон для L0 и точность округления).
        """
        out = []
        for dev_id in sorted(self.devices):
            d = self.devices[dev_id]
            props: dict[str, dict] = {}
            for p, s in sorted(d["properties"].items()):
                pv: dict[str, Any] = {"type": s["type"], "writable": bool(s.get("rw"))}
                for k in ("unit", "min", "max", "enum", "resolution"):
                    if k in s:
                        pv[k] = s[k]
                props[p] = pv
            acts = {a: {"description": action_description(d, a, spec), "input": action_input(spec)}
                    for a, spec in sorted(d.get("actions", {}).items())}
            out.append({"id": dev_id, "kind": d["type"], "location": d["location"], "source": "sim",
                        "properties": props, "actions": acts})
        return {"devices": out}

    def list_devices(self, rich: bool = False) -> dict:
        """Результат list_devices — CompactView hw-mcp: {"id", "loc", "kind"?, "props", "actions"?}.

        ``kind`` — только если тип не читается из id (как в hw-mcp); подпись действия — «параметр:тип».
        """
        out = []
        for dev_id in sorted(self.devices):
            d = self.devices[dev_id]
            item: dict[str, Any] = {"id": dev_id, "loc": d["location"]}
            if d["type"].replace("_", "") not in dev_id.replace("_", ""):
                item["kind"] = d["type"]
            if d["properties"]:
                item["props"] = {p: prop_sig(s, rich) for p, s in sorted(d["properties"].items())}
            if d.get("actions"):
                item["actions"] = {a: ",".join(f"{k}:{v['type']}" for k, v in sorted(spec.get("params", {}).items()))
                                   for a, spec in sorted(d["actions"].items())}
            out.append(item)
        return {"devices": out}

    # ------------------------------------------------------------------ чтение
    def read(self, dev: str, prop: str) -> dict:
        if dev not in self.devices:
            return fail("unknown_device", f"no device {dev!r}; call list_devices")
        spec = self.devices[dev]["properties"].get(prop)
        if spec is None:
            return fail("unknown_property", f"device {dev!r} has no property {prop!r}")
        cell = self.state[dev][prop]
        out = {"device": dev, "property": prop, "value": cell["value"], "age_s": round_age(self.clock - cell["t"])}
        if spec.get("unit"):
            out["unit"] = spec["unit"]
        return out

    # ------------------------------------------------------------------ приводы
    def write(self, dev: str, prop: str, value: Any) -> tuple[dict, list[Change]]:
        if dev not in self.devices:
            return reject("unknown_device", f"no device {dev!r}; call list_devices", dev), []
        spec = self.devices[dev]["properties"].get(prop)
        if spec is None:
            return reject("unknown_tool", f"device {dev!r} has no property {prop!r}", dev), []
        if not spec.get("rw"):
            return reject("unknown_tool", f"property {prop!r} of {dev!r} is read-only", dev), []
        value, reason, detail = check_value(spec, value)
        if reason:
            return reject("invalid_args", f"value: {detail} ({reason})", dev), []
        changed = self._set(dev, prop, value, self.clock)
        res = {"ok": True, "verdict": "allow", "executed": True, "device": dev, "property": prop, "value": value}
        return res, ([Change(self.clock, dev, prop, value, "tool")] if changed else [])

    def invoke(self, dev: str, action: str, args: dict | None) -> tuple[dict, list[Change]]:
        args = {} if args is None else args
        if dev not in self.devices:
            return reject("unknown_device", f"no device {dev!r}; call list_devices", dev), []
        spec = self.devices[dev].get("actions", {}).get(action)
        if spec is None:
            return reject("unknown_tool", f"device {dev!r} has no action {action!r}", dev), []
        if not isinstance(args, dict):
            return reject("invalid_args", "args must be an object", dev), []
        params = spec.get("params", {})
        for k, v in args.items():
            if k not in params:
                return reject("invalid_args", f"unknown parameter {k!r}; parameters: {sorted(params)}", dev), []
            _, reason, detail = check_value(params[k], v)
            if reason:
                return reject("invalid_args", f"{k}: {detail} ({reason})", dev), []
        eff = spec.get("effect", {})
        for p, v in eff.get("busy_if", {}).items():
            if self.state[dev][p]["value"] == v:
                return reject("interlock", f"{dev} is busy ({p}={str(v).lower()})", dev), []
        values = dict(eff.get("set", {}))
        for p, param in eff.get("set_from_params", {}).items():
            if param in args:
                values[p] = args[param]
        changes = [Change(self.clock, dev, p, v, "tool") for p, v in values.items()
                   if self._set(dev, p, v, self.clock)]
        gen = self._gen[dev] = self._gen.get(dev, 0) + 1
        if "duration_s" in eff and eff.get("after"):
            self._seq += 1
            heapq.heappush(self._timers, _Timer(self.clock + eff["duration_s"], self._seq, dev, dict(eff["after"]),
                                                gen))
        res: dict[str, Any] = {"ok": True, "verdict": "allow", "executed": True, "device": dev, "action": action}
        if len(values) == 1:
            (p, v), = values.items()
            res["property"], res["value"] = p, v
        elif values:
            res["applied"] = dict(sorted(values.items()))
        if "duration_s" in eff:
            res["done_in_s"] = eff["duration_s"]
        return res, changes

    def board_health(self) -> dict:
        """Синтетическое здоровье «платы» (L3): детерминированная функция часов, поля — как у hw-mcp
        (hwmcp/internal/hub/health.go); сбоев датчиков в симуляции дня нет."""
        x = self.clock
        return {"now": x, "cpu_temp_c": round(47 + 3 * math.sin(x / 2700), 1),
                "load": {"load1": round(0.15 + 0.1 * (1 + math.sin(x / 600)) / 2, 2)},
                "memory": {"total_mib": 4096, "available_mib": 3100}, "throttled": "0x0",
                "uptime_s": round3(x - self.window[0]), "sensor_faults": {}, "sensor_errors": 0}
