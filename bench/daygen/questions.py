"""Вопросы пользователя с правильными ответами из трассы дня (THESIS_CONCEPT §8, §12.8).

Ответ вычисляется только по тому, что агент мог наблюдать с начала окна: события датчиков,
выданные в день (после отбрасывания повторов), начальное состояние и прошлые команды пользователя.
Классы:
  past    — о прошлом по датчикам («когда последний раз открывали дверь?»);
  state   — о текущем состоянии («какая сейчас температура?»); ``answer.ref`` указывает свойство,
            sim-mcp сверяет ответ с истинным состоянием в момент вызова answer;
  history — о прошлых репликах пользователя («какой цвет я просил?») — проверка контекста пользователя.

Поле ``answer``: ``{"value", "unit", "tol"?, "accept"?, "ref"?}``; единицы: ``t`` — секунды от полуночи
(как t дня; sim-mcp принимает и «ЧЧ:ММ[:СС]»), ``count``, ``Cel``, ``%RH``, ``room``, ``color``, ``bool``.
"""
from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import room

if TYPE_CHECKING:
    from .generator import DayBuilder

T_TOL_S = 60.0
TEMP_TOL = 0.15
HUM_TOL = 1.0


@dataclass
class QuestionContext:
    b: DayBuilder
    t: float

    def since(self) -> tuple[str, str]:
        w0 = self.b.w0
        if w0 == 7 * 3600:
            return "с семи утра", "since 7 am"
        hh = f"{w0 // 3600:02d}:{w0 % 3600 // 60:02d}"
        return f"с {hh}", f"since {hh}"

    def log(self, dev: str | None = None, prop: str | None = None) -> list[dict]:
        return [e for e in self.b.sensor_log
                if (dev is None or e["device"] == dev) and (prop is None or e["property"] == prop)]

    def motion_on(self) -> list[dict]:
        return [e for e in self.b.sensor_log if e["property"] == "motion" and e["value"] == 1]

    def room_of(self, dev: str) -> str:
        return self.b.dev[dev]["location"]

    def thermo(self) -> dict:
        return next(d for d in self.b.devices if d["type"] == "climate_sensor")


def room_accept(b: DayBuilder, r: str) -> list[str]:
    rm = room.ALL_ROOMS[r]
    names = {r, rm.ru_nom, rm.ru_loc, rm.en}
    names |= {d["id"] for d in b.devices if d["location"] == r and d["type"] == "motion_sensor"}
    return sorted(names)


def color_accept(c: str) -> list[str]:
    return sorted({c, *room.COLORS[c]})


def _answer_call(ans: dict) -> dict:
    call = {"name": "answer", "args": {"value": ans["value"], "unit": ans["unit"]}}
    return {"type": "answer", "calls": [call], "calls_explicit": [dict(call)]}


QFn = Callable[[QuestionContext, random.Random], "tuple[str, str, str, dict] | None"]


def q_last_door_open(c: QuestionContext, rng: random.Random):
    if "door_front" not in c.b.dev:
        return None
    opens = [e for e in c.log("door_front", "open") if e["value"] == 1]
    if not opens:
        return None
    return ("когда последний раз открывали входную дверь?", "when was the front door last opened?", "past",
            {"value": opens[-1]["t"], "unit": "t", "tol": T_TOL_S})


def q_door_open_count(c: QuestionContext, rng: random.Random):
    if "door_front" not in c.b.dev:
        return None
    n = sum(1 for e in c.log("door_front", "open") if e["value"] == 1)
    ru, en = c.since()
    return (f"сколько раз открывали входную дверь {ru}?", f"how many times has the front door been opened {en}?",
            "past", {"value": n, "unit": "count", "tol": 0})


def q_last_motion_room(c: QuestionContext, rng: random.Random):
    last: dict[str, float] = {}
    for e in c.motion_on():
        last[c.room_of(e["device"])] = e["t"]
    if not last:
        return None
    r = rng.choice(sorted(last))
    rm = room.ALL_ROOMS[r]
    return (f"когда последний раз было движение {rm.ru_loc}?", f"when was motion last detected in the {rm.en}?",
            "past", {"value": last[r], "unit": "t", "tol": T_TOL_S})


def q_last_room(c: QuestionContext, rng: random.Random):
    on = c.motion_on()
    if not on:
        return None
    r = c.room_of(on[-1]["device"])
    return ("в какой комнате последний раз было движение?", "in which room was motion last detected?", "past",
            {"value": r, "unit": "room", "accept": room_accept(c.b, r)})


def q_max_temp(c: QuestionContext, rng: random.Random):
    th = c.thermo()
    vals = [c.b.initial_state[th["id"]]["temperature"]] + [e["value"] for e in c.log(th["id"], "temperature")]
    rm = room.ALL_ROOMS[th["location"]]
    ru, en = c.since()
    return (f"какая была максимальная температура {rm.ru_loc} {ru}?",
            f"what was the highest temperature in the {rm.en} {en}?", "past",
            {"value": max(vals), "unit": "Cel", "tol": TEMP_TOL})


def q_temp_now(c: QuestionContext, rng: random.Random):
    th = c.thermo()
    rm = room.ALL_ROOMS[th["location"]]
    return (f"какая сейчас температура {rm.ru_loc}?", f"what is the temperature in the {rm.en} now?", "state",
            {"value": c.b.state[th["id"]]["temperature"], "unit": "Cel", "tol": TEMP_TOL,
             "ref": {"device": th["id"], "property": "temperature"}})


def q_humidity_now(c: QuestionContext, rng: random.Random):
    th = c.thermo()
    rm = room.ALL_ROOMS[th["location"]]
    return (f"какая сейчас влажность {rm.ru_loc}?", f"what is the humidity in the {rm.en} now?", "state",
            {"value": c.b.state[th["id"]]["humidity"], "unit": "%RH", "tol": HUM_TOL,
             "ref": {"device": th["id"], "property": "humidity"}})


def q_door_open_now(c: QuestionContext, rng: random.Random):
    if "door_front" not in c.b.dev:
        return None
    return ("входная дверь сейчас открыта?", "is the front door open right now?", "state",
            {"value": c.b.state["door_front"]["open"] == 1, "unit": "bool",
             "ref": {"device": "door_front", "property": "open"}})


def q_light_state(c: QuestionContext, rng: random.Random):
    r = rng.choice(c.b.rooms)
    rm = room.ALL_ROOMS[r]
    d = f"light_{r}"
    return (f"свет {rm.ru_loc} сейчас включён?", f"is the {rm.en} light on right now?", "state",
            {"value": bool(c.b.state[d]["on"]), "unit": "bool", "ref": {"device": d, "property": "on"}})


def q_last_color(c: QuestionContext, rng: random.Random):
    cols = [h["params"]["color"] for h in c.b.history if h["action"] in ("light_color", "light_color_all")]
    if not cols:
        return None
    return ("какой цвет света я просил в последний раз?", "what light color did I ask for last time?", "history",
            {"value": cols[-1], "unit": "color", "accept": color_accept(cols[-1])})


def q_coffee_count(c: QuestionContext, rng: random.Random):
    n = sum(1 for h in c.b.history if h["action"] == "coffee_brew")
    if n == 0:
        return None
    ru, en = c.since()
    return (f"сколько раз я просил сварить кофе {ru}?", f"how many times did I ask for coffee {en}?", "history",
            {"value": n, "unit": "count", "tol": 0})


QUESTIONS: dict[str, QFn] = {
    "last_door_open": q_last_door_open,
    "door_open_count": q_door_open_count,
    "last_motion_room": q_last_motion_room,
    "last_room": q_last_room,
    "max_temp": q_max_temp,
    "temp_now": q_temp_now,
    "humidity_now": q_humidity_now,
    "door_open_now": q_door_open_now,
    "light_state": q_light_state,
    "last_color": q_last_color,
    "coffee_count": q_coffee_count,
}


def make_question(c: QuestionContext, rng: random.Random, lang: str) -> dict | None:
    """Случайный доступный тип вопроса (равновероятно среди доступных) или None."""
    # Порядок вызовов фиксирован, и каждый тип получает свой поток — выбор не зависит от доступности других.
    avail = []
    for name, fn in QUESTIONS.items():
        res = fn(c, random.Random(f"{rng.random()}/{name}"))
        if res is not None:
            avail.append((name, res))
    if not avail:
        return None
    name, (ru, en, cls, ans) = avail[rng.randrange(len(avail))]
    return {"kind": "question", "text": ru if lang == "ru" else en, "class": cls, "qtype": name,
            "answer": ans, "expect": _answer_call(ans)}
