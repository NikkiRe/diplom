"""Оценка по истинному состоянию: команды (цель), вопросы (answer), невалидные (refuse), ситуации (действие).

Привязка вызовов к входам дня. «Текущий вход пользователя» — последний user_cmd/question, момент которого
наступил по часам (или явно заданный POST /control/input). Его окно длится до следующего user_cmd/question
или до конца прогона. Ситуации окно пользователя не закрывают: агент мог отвечать на команду, когда
подошла ситуация.

* user_cmd (action): цель ``expect.goal`` выполнена после вызова инструмента внутри окна (moment
  ``satisfied_at``); проверяется после каждой успешной записи/действия, изменения от трассы (ручные
  переключения жителя) успехом агента не считаются. ``pre_satisfied`` — цель уже была выполнена в момент
  реплики (например, агент ранее не выполнил противоположную команду). ``final_ok`` — цель выполнена при
  закрытии окна.
* user_cmd (refuse): в окне вызван refuse и нет успешных записей/действий, кроме ожидаемых действий открытых
  ситуаций.
* question: засчитывается последний answer в окне; если у ответа есть ``ref`` — эталон берётся из истинного
  состояния в момент вызова answer.
* situation: если в ``expect.actions`` есть действия — совпадающая успешная запись в [t; t + deadline_s].
  Уведомления пользователю sim-mcp не видит — их считают метрики по трассе edgectx.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .world import World

UNIT_ALIASES = {
    "t": {"t", "time", "clock", "hh:mm", "hh:mm:ss", "timestamp", "время", "s_since_midnight"},
    "count": {"count", "times", "n", "раз", "number", "pcs", "шт"},
    "Cel": {"cel", "c", "°c", "celsius", "degc", "deg_c", "градусы", "°с"},
    "%RH": {"%rh", "%", "rh", "percent", "pct", "%отн"},
    "room": {"room", "location", "комната", "place"},
    "color": {"color", "colour", "цвет"},
    "bool": {"bool", "boolean", "yes/no", "state", "да/нет"},
}
TRUE_WORDS = {"true", "yes", "да", "1", "on", "open", "opened", "открыта", "открыто", "включен", "включён",
              "включено", "включена"}
FALSE_WORDS = {"false", "no", "нет", "0", "off", "closed", "закрыта", "закрыто", "выключен", "выключено",
               "выключена"}


def norm_text(x: Any) -> str:
    return str(x).strip().lower().replace("ё", "е")


def parse_time_of_day(v: Any) -> float | None:
    """Секунды от полуночи из числа или «ЧЧ:ММ[:СС]» (в т. ч. внутри ISO-строки)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}(?:\.\d+)?))?", str(v))
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3) or 0)
    try:
        return float(str(v))
    except ValueError:
        return None


def to_number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(v))
    return float(m.group(0).replace(",", ".")) if m else None


def to_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    s = norm_text(v)
    if s in TRUE_WORDS:
        return True
    if s in FALSE_WORDS:
        return False
    return None


def match_answer(expected: dict, value: Any, unit: Any, live_value: Any = None) -> dict:
    """Сравнение ответа с эталоном. live_value — истинное значение для вопросов о текущем состоянии."""
    eu = expected["unit"]
    exp_v = live_value if live_value is not None else expected["value"]
    unit_ok = norm_text(unit) in {norm_text(a) for a in UNIT_ALIASES.get(eu, {eu})} | {norm_text(eu)}
    tol = float(expected.get("tol", 0) or 0)
    if eu == "t":
        got = parse_time_of_day(value)
        value_ok = got is not None and abs(got - float(exp_v)) <= tol
    elif eu in ("count", "Cel", "%RH"):
        got = to_number(value)
        value_ok = got is not None and abs(got - float(exp_v)) <= tol + 1e-9
    elif eu == "bool":
        if isinstance(exp_v, int) and not isinstance(exp_v, bool):
            exp_v = bool(exp_v)
        got = to_bool(value)
        value_ok = got is not None and got == exp_v
    else:
        accept = {norm_text(a) for a in expected.get("accept", [exp_v])} | {norm_text(exp_v)}
        value_ok = norm_text(value) in accept
    return {"value_ok": value_ok, "unit_ok": unit_ok, "correct": value_ok and unit_ok, "expected": exp_v}


def goal_holds(world: World, goal: list[dict]) -> bool:
    for g in goal:
        try:
            v = world.value(g["device"], g["property"])
        except KeyError:
            return False
        op, x = g["op"], g["value"]
        if op == "eq":
            ok = v == x and (isinstance(v, bool) == isinstance(x, bool))
        elif op == "lt":
            ok = v is not None and v < x
        elif op == "gt":
            ok = v is not None and v > x
        else:
            raise ValueError(f"неизвестный оператор цели {op}")
        if not ok:
            return False
    return True


def call_matches(expected: dict, op: str, nargs: dict) -> bool:
    """Совпадает ли выполненная операция (generic-вид) с ожидаемым вызовом."""
    a = expected["args"]
    if expected["name"] == "write_property":
        return op == "write" and nargs.get("device") == a["device"] and nargs.get("property") == a["property"] \
            and nargs.get("value") == a["value"]
    if expected["name"] == "invoke_action":
        return op == "invoke" and nargs.get("device") == a["device"] and nargs.get("action") == a["action"]
    return False


@dataclass
class InputState:
    ev: dict
    opened_at: float | None = None
    closed_at: float | None = None
    calls: list[dict] = field(default_factory=list)
    answers: list[dict] = field(default_factory=list)
    refuses: list[dict] = field(default_factory=list)
    satisfied_at: float | None = None
    pre_satisfied: bool | None = None
    final_ok: bool | None = None
    action_at: float | None = None       # для ситуаций

    @property
    def id(self) -> str:
        return self.ev["id"]

    @property
    def kind(self) -> str:
        return self.ev["kind"]


class Evaluator:
    def __init__(self, world: World, events: list[dict]):
        self.world = world
        self.inputs: dict[str, InputState] = {}
        for e in events:
            if e["kind"] in ("user_cmd", "question", "situation"):
                self.inputs[e["id"]] = InputState(e)
        self.current: InputState | None = None
        self.unattributed: list[dict] = []

    # ------------------------------------------------------------------ входы
    def open(self, ev: dict, clock: float) -> None:
        st = self.inputs.get(ev["id"])
        if st is None or st.opened_at is not None:
            return
        st.opened_at = clock
        if st.kind == "situation":
            return
        self._close_current(clock)
        self.current = st
        if st.kind == "user_cmd" and st.ev["expect"]["type"] == "action":
            st.pre_satisfied = goal_holds(self.world, st.ev["expect"]["goal"])

    def set_current(self, input_id: str, clock: float) -> bool:
        st = self.inputs.get(input_id)
        if st is None or st.kind == "situation":
            return False
        if st.opened_at is None:
            self.open(st.ev, clock)
        elif self.current is not st:
            self._close_current(clock)
            self.current = st
            st.closed_at = None
        return True

    def _close_current(self, clock: float) -> None:
        cur = self.current
        if cur is None:
            return
        cur.closed_at = clock
        if cur.kind == "user_cmd" and cur.ev["expect"]["type"] == "action":
            cur.final_ok = goal_holds(self.world, cur.ev["expect"]["goal"])
        self.current = None

    # ------------------------------------------------------------------ вызовы
    def on_call(self, op: str, nargs: dict, result: dict, clock: float) -> str | None:
        """Учесть вызов инструмента. Возвращает id входа, к которому он отнесён."""
        rec = {"t": clock, "op": op, "args": nargs, "ok": bool(result.get("ok")),
               "denied": bool(result.get("denied"))}
        cur = self.current
        if cur is None:
            self.unattributed.append(rec)
        else:
            cur.calls.append(rec)
            if op == "answer" and rec["ok"]:          # ответ с неверными аргументами не засчитывается
                if cur.kind == "question":
                    rec["live"] = self.live_value(cur)   # эталон для вопросов о текущем состоянии
                cur.answers.append(rec)
            elif op == "refuse" and rec["ok"]:
                cur.refuses.append(rec)
        if op in ("write", "invoke") and rec["ok"]:
            if cur is not None and cur.kind == "user_cmd" and cur.ev["expect"]["type"] == "action" \
                    and cur.satisfied_at is None and goal_holds(self.world, cur.ev["expect"]["goal"]):
                cur.satisfied_at = clock
            for st in self.inputs.values():
                if st.kind != "situation" or st.opened_at is None or st.action_at is not None:
                    continue
                if not st.ev["t"] <= clock <= st.ev["t"] + st.ev["deadline_s"]:
                    continue
                if any(call_matches(a, op, nargs) for a in st.ev["expect"].get("actions", [])):
                    st.action_at = clock
        return cur.id if cur else None

    def live_value(self, st: InputState) -> Any:
        ref = st.ev.get("answer", {}).get("ref")
        if not ref:
            return None
        try:
            return self.world.value(ref["device"], ref["property"])
        except KeyError:
            return None

    # ------------------------------------------------------------------ отчёт
    def _situation_action(self, op: str, nargs: dict, t: float) -> bool:
        for st in self.inputs.values():
            if st.kind == "situation" and st.ev["t"] <= t <= st.ev["t"] + st.ev["deadline_s"] and \
                    any(call_matches(a, op, nargs) for a in st.ev["expect"].get("actions", [])):
                return True
        return False

    def result(self, st: InputState) -> dict:
        ev = st.ev
        out: dict[str, Any] = {"id": st.id, "kind": st.kind, "t": ev["t"], "opened": st.opened_at is not None,
                               "n_calls": len(st.calls)}
        if st.kind == "user_cmd":
            out["class"] = ev.get("class")
            out["action"] = ev.get("action")
            if ev["expect"]["type"] == "action":
                out.update({"ok": st.satisfied_at is not None, "satisfied_at": st.satisfied_at,
                            "pre_satisfied": st.pre_satisfied, "final_ok": st.final_ok})
            else:
                bad = [c for c in st.calls if c["op"] in ("write", "invoke") and c["ok"]
                       and not self._situation_action(c["op"], c["args"], c["t"])]
                out.update({"ok": bool(st.refuses) and not bad, "refused": bool(st.refuses),
                            "side_effects": len(bad)})
        elif st.kind == "question":
            out["class"] = ev.get("class")
            out["qtype"] = ev.get("qtype")
            if st.answers:
                last = st.answers[-1]
                m = match_answer(ev["answer"], last["args"].get("value"), last["args"].get("unit"),
                                 last.get("live"))
                out.update({"ok": m["correct"], "answered": True, "value": last["args"].get("value"),
                            "unit": last["args"].get("unit"), **m})
            else:
                out.update({"ok": False, "answered": False})
        else:
            out["name"] = ev["name"]
            if ev["expect"].get("actions"):
                out.update({"ok": st.action_at is not None, "action_at": st.action_at,
                            "latency_s": None if st.action_at is None else round(st.action_at - ev["t"], 3)})
            else:
                out["ok"] = None   # проверяется по уведомлениям в трассе edgectx
        return out

    def report(self) -> dict:
        rows = [self.result(st) for st in sorted(self.inputs.values(), key=lambda s: (s.ev["t"], s.id))]

        def rate(xs: list[dict]) -> dict:
            n = len(xs)
            ok = sum(1 for x in xs if x.get("ok"))
            return {"n": n, "ok": ok, "rate": (ok / n) if n else None}

        opened = [r for r in rows if r["opened"]]
        cmds = [r for r in opened if r["kind"] == "user_cmd"]
        qs = [r for r in opened if r["kind"] == "question"]
        sits = [r for r in opened if r["kind"] == "situation" and r["ok"] is not None]
        by_class: dict[str, list[dict]] = {}
        for r in cmds + qs:
            by_class.setdefault(f"{r['kind']}:{r.get('class')}", []).append(r)
        return {
            "t": self.world.clock,
            "summary": {
                "commands": rate(cmds), "questions": rate(qs), "situations_with_actions": rate(sits),
                "by_class": {k: rate(v) for k, v in sorted(by_class.items())},
                "inputs_total": len(rows), "inputs_opened": len(opened),
                "unattributed_calls": len(self.unattributed),
            },
            "inputs": rows,
        }

    def finish(self, clock: float) -> None:
        self._close_current(clock)
