"""Команды пользователя: шаблон MASSIVE → устройство помещения → ожидаемые вызовы и цель.

Команда выбирается с учётом «идеального» состояния дома в момент реплики: выключают включённый свет,
приглушают горящий, варят кофе, когда кофемашина свободна, и т. п. Иначе цель была бы выполнена
без действия агента и проверка по состоянию ничего бы не показывала.

Поле ``expect`` (контракт для mock-llm oracle, sim-mcp и метрик)::

    {"type": "action",
     "calls":          [{"name": "write_property", "args": {"device", "property", "value"}}, ...],  # generic
     "calls_explicit": [{"name": "set_<dev>_<prop>", "args": {"value": ...}}, ...],               # explicit
     "goal":           [{"device", "property", "op": "eq|lt|gt", "value"}, ...]}
    {"type": "refuse", "reason": "no_such_device|no_such_room", "calls": [{"name": "refuse", ...}], ...}

``calls`` — минимальная эталонная реакция; засчитывается любая, после которой выполнена ``goal``
(sim-mcp проверяет по истинному состоянию).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import room

if TYPE_CHECKING:
    from .generator import DayBuilder

DIM_STEP, UP_STEP, MIN_BRIGHTNESS = 30, 30, 10


@dataclass
class CommandContext:
    b: DayBuilder
    t: float

    @property
    def state(self) -> dict[str, dict]:
        return self.b.state

    def light(self, room_id: str) -> str | None:
        d = f"light_{room_id}"
        return d if d in self.b.dev else None

    def device_of(self, dev_type: str) -> str:
        d = room.find_one(self.b.devices, dev_type)
        if d is None:
            raise KeyError(dev_type)
        return d["id"]

    def context_room(self) -> str:
        """Комната пользователя: где последним было движение (иначе гостиная или первая комната)."""
        r = self.b.user_room
        if r in self.b.rooms:
            return r
        return "living_room" if "living_room" in self.b.rooms else self.b.rooms[0]


@dataclass
class Plan:
    calls: list[dict]
    goal: list[dict]
    effects: list[tuple[str, str, Any]]
    timers: list[tuple[float, str, dict]]
    room: str | None


def write(dev: str, prop: str, value: Any) -> dict:
    return {"name": "write_property", "args": {"device": dev, "property": prop, "value": value}}


def invoke(dev: str, action: str, args: dict | None = None) -> dict:
    return {"name": "invoke_action", "args": {"device": dev, "action": action, "args": args or {}}}


def goal(dev: str, prop: str, op: str, value: Any) -> dict:
    return {"device": dev, "property": prop, "op": op, "value": value}


def to_explicit(call: dict) -> dict:
    """Вызов generic-каталога → эквивалент explicit-каталога sim-mcp/hw-mcp (действие — <action>_<device>)."""
    a = call["args"]
    if call["name"] == "write_property":
        return {"name": f"set_{a['device']}_{a['property']}", "args": {"value": a["value"]}}
    if call["name"] == "read_property":
        return {"name": f"read_{a['device']}_{a['property']}", "args": {}}
    if call["name"] == "invoke_action":
        return {"name": f"{a['action']}_{a['device']}", "args": dict(a.get("args") or {})}
    return {"name": call["name"], "args": dict(a)}


# ---------------------------------------------------------------------------
# Планы по действиям: None — команда сейчас неприменима (предусловие не выполнено)
# ---------------------------------------------------------------------------


def _light_plan(ctx: CommandContext, action: str, r: str, params: dict) -> Plan | None:
    d = ctx.light(r)
    if d is None:
        return None
    s = ctx.state[d]
    on, b, color = s["on"], s["brightness"], s["color"]
    if action == "light_on":
        if on:
            return None
        return Plan([write(d, "on", True)], [goal(d, "on", "eq", True)], [(d, "on", True)], [], r)
    if action == "light_off":
        if not on:
            return None
        return Plan([write(d, "on", False)], [goal(d, "on", "eq", False)], [(d, "on", False)], [], r)
    if action == "light_up":
        if not on:   # «ярче» при выключенном свете — включить
            return Plan([write(d, "on", True)], [goal(d, "on", "eq", True)], [(d, "on", True)], [], r)
        if b >= 100:
            return None
        nb = min(100, b + UP_STEP)
        return Plan([write(d, "brightness", nb)], [goal(d, "on", "eq", True), goal(d, "brightness", "gt", b)],
                    [(d, "brightness", nb)], [], r)
    if action == "light_dim":
        if not on or b <= MIN_BRIGHTNESS:
            return None
        nb = max(MIN_BRIGHTNESS, b - DIM_STEP)
        return Plan([write(d, "brightness", nb)], [goal(d, "on", "eq", True), goal(d, "brightness", "lt", b)],
                    [(d, "brightness", nb)], [], r)
    if action == "light_color":
        x = params["color"]
        if not on or color == x:
            return None
        return Plan([write(d, "color", x)], [goal(d, "color", "eq", x)], [(d, "color", x)], [], r)
    if action == "light_brightness":
        v = params["brightness"]
        if on and b == v:
            return None
        calls = ([] if on else [write(d, "on", True)]) + [write(d, "brightness", v)]
        return Plan(calls, [goal(d, "on", "eq", True), goal(d, "brightness", "eq", v)],
                    [(d, "on", True), (d, "brightness", v)], [], r)
    raise ValueError(action)


def _all_lights(ctx: CommandContext) -> list[str]:
    return [d["id"] for d in room.lights(ctx.b.devices)]


def _device_plan(ctx: CommandContext, action: str, r: str | None, params: dict) -> Plan | None:
    st = ctx.state
    if action in ("light_on_all", "light_off_all"):
        want = action == "light_on_all"
        todo = [d for d in _all_lights(ctx) if st[d]["on"] != want]
        if not todo:
            return None
        return Plan([write(d, "on", want) for d in todo], [goal(d, "on", "eq", want) for d in _all_lights(ctx)],
                    [(d, "on", want) for d in todo], [], None)
    if action == "light_color_all":
        x = params["color"]
        todo = [d for d in _all_lights(ctx) if st[d]["color"] != x]
        if not todo:
            return None
        return Plan([write(d, "color", x) for d in todo], [goal(d, "color", "eq", x) for d in _all_lights(ctx)],
                    [(d, "color", x) for d in todo], [], None)
    if action == "all_off":
        plug, vac = ctx.device_of("plug"), ctx.device_of("vacuum")
        calls, effects = [], []
        for d in _all_lights(ctx):
            if st[d]["on"]:
                calls.append(write(d, "on", False))
                effects.append((d, "on", False))
        if st[plug]["on"]:
            calls.append(write(plug, "on", False))
            effects.append((plug, "on", False))
        g = [goal(d, "on", "eq", False) for d in _all_lights(ctx)] + [goal(plug, "on", "eq", False)]
        if st[vac]["cleaning"]:
            calls.append(invoke(vac, "dock"))
            effects += [(vac, "cleaning", False), (vac, "zone", "dock")]
            g.append(goal(vac, "cleaning", "eq", False))
        if not calls:
            return None
        return Plan(calls, g, effects, [], None)
    if action in ("plug_on", "plug_off"):
        plug = ctx.device_of("plug")
        want = action == "plug_on"
        if st[plug]["on"] == want:
            return None
        return Plan([write(plug, "on", want)], [goal(plug, "on", "eq", want)], [(plug, "on", want)], [], None)
    if action == "coffee_brew":
        cm = ctx.device_of("coffee_maker")
        spec = ctx.b.dev[cm]["actions"]["brew"]["effect"]
        if st[cm]["brewing"]:
            return None
        return Plan([invoke(cm, "brew")], [goal(cm, "brewing", "eq", True)], [(cm, "brewing", True)],
                    [(ctx.t + spec["duration_s"], cm, dict(spec["after"]))], None)
    if action == "vacuum_start":
        vac = ctx.device_of("vacuum")
        spec = ctx.b.dev[vac]["actions"]["start"]["effect"]
        if st[vac]["cleaning"]:
            return None
        zone = r or "all"
        g = [goal(vac, "cleaning", "eq", True)] + ([goal(vac, "zone", "eq", r)] if r else [])
        return Plan([invoke(vac, "start", {"room": r} if r else {})], g,
                    [(vac, "cleaning", True), (vac, "zone", zone)],
                    [(ctx.t + spec["duration_s"], vac, dict(spec["after"]))], r)
    raise ValueError(action)


def plan_for(ctx: CommandContext, tpl: dict, r: str | None) -> Plan | None:
    action = tpl["action"]
    if action.startswith("light_") and not action.endswith("_all"):
        return _light_plan(ctx, action, r, tpl["params"]) if r else None
    return _device_plan(ctx, action, r, tpl["params"])


def render(tpl: dict, lang: str, r: str | None) -> str:
    if tpl["room_mode"] == "slot":
        rm = room.ALL_ROOMS[r]
        if lang == "ru":
            return tpl["ru_tpl"].format(loc=rm.ru_loc, acc=rm.ru_acc)
        return tpl["en_tpl"].format(room=rm.en)
    return tpl[lang]


def _valid_attempt(ctx: CommandContext, tpl: dict, rng: random.Random) -> tuple[Plan, str | None] | None:
    mode = tpl["room_mode"]
    if mode == "slot":
        rooms = list(ctx.b.rooms)
        rng.shuffle(rooms)
        for r in rooms:
            p = plan_for(ctx, tpl, r)
            if p is not None:
                return p, r
        return None
    if mode == "context":
        r = ctx.context_room()
        p = plan_for(ctx, tpl, r)
        return (p, r) if p else None
    if mode == "fixed":
        r = tpl["params"].get("room")
        if r not in ctx.b.rooms:
            return None
        p = plan_for(ctx, tpl, r)
        return (p, r) if p else None
    p = plan_for(ctx, tpl, None)
    return (p, None) if p else None


def _make_valid(ctx: CommandContext, rng: random.Random, lang: str) -> dict | None:
    valid = [t for t in ctx.b.templates if t["class"] != "invalid"]
    found = None
    for _ in range(60):
        tpl = rng.choice(valid)
        res = _valid_attempt(ctx, tpl, rng)
        if res:
            found = (tpl, *res)
            break
    if found is None:   # редкий случай: перебор всех шаблонов в случайном порядке
        order = valid[:]
        rng.shuffle(order)
        for tpl in order:
            res = _valid_attempt(ctx, tpl, rng)
            if res:
                found = (tpl, *res)
                break
    if found is None:
        return None
    tpl, plan, r = found
    for dev, prop, val in plan.effects:
        ctx.state[dev][prop] = val
    ctx.b.history.append({"t": ctx.t, "action": tpl["action"], "params": tpl["params"], "room": r})
    return {
        "kind": "user_cmd", "text": render(tpl, lang, r), "class": tpl["class"], "action": tpl["action"],
        "template": f"massive:{tpl['id']}", "room": r, "resolve": tpl["room_mode"],
        "expect": {"type": "action", "calls": plan.calls, "calls_explicit": [to_explicit(c) for c in plan.calls],
                   "goal": plan.goal},
        "_timers": plan.timers,
    }


def _make_invalid(ctx: CommandContext, rng: random.Random, lang: str) -> dict:
    """Ловушка «невалидная команда»: устройства или комнаты нет в доме — ожидается refuse."""
    tpls = ctx.b.templates
    inherent = [t for t in tpls if t["action"] == "invalid"
                or (t["room_mode"] == "fixed" and t["params"].get("room") not in ctx.b.rooms)]
    slot = [t for t in tpls if t["room_mode"] == "slot"]
    absent = sorted(set(room.ALL_ROOMS) - set(ctx.b.rooms))
    if rng.random() < 0.5 and inherent:
        tpl = rng.choice(inherent)
        r = tpl["params"].get("room")
        target = tpl["params"].get("target") or (f"light_{r}" if r else None)
    else:
        tpl = rng.choice(slot)
        r = rng.choice(absent)
        target = "vacuum" if tpl["action"] == "vacuum_start" else f"light_{r}"
    reason = "no_such_room" if target == "vacuum" else "no_such_device"
    call = {"name": "refuse", "args": {"reason": reason}}
    ctx.b.history.append({"t": ctx.t, "action": "invalid", "params": tpl["params"], "room": r})
    return {
        "kind": "user_cmd", "text": render(tpl, lang, r), "class": "invalid", "action": tpl["action"],
        "template": f"massive:{tpl['id']}", "room": r, "resolve": tpl["room_mode"], "target": target,
        "expect": {"type": "refuse", "reason": reason, "calls": [call], "calls_explicit": [dict(call)]},
    }


def make_command(ctx: CommandContext, rng: random.Random, lang: str, trap: bool) -> dict | None:
    if trap:
        return _make_invalid(ctx, rng, lang)
    return _make_valid(ctx, rng, lang)
