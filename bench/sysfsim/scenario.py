"""Сценарий симулятора: формат sysfsim/1, проверка при загрузке, встроенные сценарии (scenarios/*.json).

Поля сценария и их смысл — README.md, «Формат сценария». Неизвестный ключ, не тот тип, ссылка на
несуществующие зону, устройство или свойство — ScenarioError с путём до поля, например
«dense.json.events[3].zone: неизвестное значение 'kitchn'; допустимо: kitchen, bathroom, boiler_pipe, board».
Отказы со случайными моментами (rate_per_h) разворачивает по seed world.expand_faults.
"""
from __future__ import annotations

import json
import math
import operator
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from .physics import OUTDOOR, ZONES, clock_s
from .sensors import BY_NAME, DEVICES, MIN_PERIOD_S, MODELS, PARAMS, props

FORMAT = "sysfsim/1"
BUILTIN_DIR = Path(__file__).resolve().parent / "scenarios"


class ScenarioError(ValueError):
    """Ошибка сценария: путь до поля и что не так."""


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    duration_s: float
    start_s: float                  # время суток в момент t = 0, с от полуночи
    outdoor: dict
    zones: dict[str, dict]          # physics.ZONES + переопределения сценария
    devices: dict[str, dict]        # sensors.PARAMS + переопределения сценария
    events: tuple[dict, ...]
    faults: tuple[dict, ...]
    questions: tuple[dict, ...]
    situations: tuple[dict, ...]
    sha256: str                     # SHA-256 канонического JSON документа


Check = Callable[[Any, str], None]


def _fail(path: str, message: str) -> None:
    raise ScenarioError(f"{path}: {message}")


def number(lo: float = -math.inf, hi: float = math.inf, *, positive: bool = False) -> Check:
    def check(v: Any, path: str) -> None:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            _fail(path, "ожидается число")
        if positive and v <= 0:
            _fail(path, "ожидается число > 0")
        if not lo <= v <= hi:
            _fail(path, f"ожидается число в [{lo}; {hi}]")
    return check


ANY, POS, NONNEG, FRAC = number(), number(positive=True), number(0), number(0, 1)


def integer(lo: int, hi: int) -> Check:
    def check(v: Any, path: str) -> None:
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            _fail(path, f"ожидается целое в [{lo}; {hi}]")
    return check


def boolean(v: Any, path: str) -> None:
    if not isinstance(v, bool):
        _fail(path, "ожидается true или false")


def text(v: Any, path: str) -> None:
    if not isinstance(v, str) or not v.strip():
        _fail(path, "ожидается непустая строка")


def one_of(*options: Any) -> Check:
    def check(v: Any, path: str) -> None:
        if v not in options:
            _fail(path, f"неизвестное значение {v!r}; допустимо: {', '.join(map(str, options))}")
    return check


def clock(v: Any, path: str) -> None:
    text(v, path)
    h, _, m = v.partition(":")
    if not (len(v) == 5 and h.isdigit() and m.isdigit() and int(h) < 24 and int(m) < 60):
        _fail(path, "ожидается время «ЧЧ:ММ»")


def later(v: Any, path: str) -> None:
    """Значение проверяется отдельно, с учётом контекста."""


def device(*models: str) -> Check:
    """Имя устройства платы (любого или одной из моделей)."""
    return one_of(*(d.name for d in DEVICES if not models or d.model in models))


OUTDOOR_CHECKS = {"mean_c": number(-60, 60), "amplitude_c": number(0, 30), "coldest": clock, "rh": number(0, 100),
                  "pressure_pa": number(30000, 110000), "pressure_amp_pa": NONNEG, "pressure_period_s": POS}
ZONE_CHECKS = {"base_c": number(-40, 120), "outdoor_k": FRAC, "tau_s": POS, "fan_tau_s": NONNEG,
               "rh": number(0, 100), "rh_tau_s": POS, "load_rise_c": number(0, 100)}
PARAM_CHECKS = {"period_s": POS, "fail_rate": FRAC, "v_dry": ANY, "v_wet": ANY, "tau_wet_s": POS, "tau_dry_s": POS,
                "hold_s": POS, "block_s": NONNEG, "retrigger": boolean, "rate_hz": POS, "false_per_h": NONNEG,
                "bounce_ms": number(0, 100), "bounce_pulses": integer(0, 50)}

EVENTS: dict[str, dict[str, Check]] = {
    "heat": {"zone": one_of(*ZONES), "rise_c": number(-50, 100), "duration_s": POS},
    "moisture": {"zone": one_of(*ZONES), "rh_rise": number(0, 100), "duration_s": POS},
    "load": {"level": FRAC, "duration_s": POS},
    "motion": {"device": device("pir"), "duration_s": POS},
    "door": {"device": device("reed"), "duration_s": POS},
    "leak": {"device": device("ads1115"), "duration_s": POS},
}
EVENT_OPTIONAL: dict[str, dict[str, Check]] = {"motion": {"rate_hz": POS}}

# вид отказа → (модели, к которым он применим; его параметры)
FAULTS: dict[str, tuple[tuple[str, ...], dict[str, Check]]] = {
    "disconnect": (("bme280", "dht11", "ds18b20", "ads1115"), {}),
    "read_error": (tuple(MODELS), {}),
    "por85": (("ds18b20",), {}),
    "stuck": (tuple(MODELS), {}),
    "spike": (tuple(MODELS), {"amplitude": ANY}),
    "drift": (tuple(MODELS), {"per_h": ANY}),
    "noise_burst": (tuple(MODELS), {"sigma": POS}),
    "dht_fail_rate": (("dht11",), {"p": FRAC}),
    "bounce": (("reed",), {"ms": number(0, 100, positive=True), "pulses": integer(1, 50)}),
}
VALUE_FAULTS = ("spike", "drift", "noise_burst")   # действуют на одно свойство
OPS = {"ge": operator.ge, "gt": operator.gt, "le": operator.le, "lt": operator.lt, "eq": operator.eq}


def _object(obj: Any, path: str, required: dict[str, Check], optional: dict[str, Check] | None = None) -> dict:
    """Объект с обязательными и необязательными ключами, без лишних; значения проверены."""
    optional = optional or {}
    if not isinstance(obj, dict):
        _fail(path, "ожидается объект")
    if unknown := sorted(obj.keys() - required.keys() - optional.keys()):
        _fail(f"{path}.{unknown[0]}", "неизвестный ключ")
    for key, check in (required | optional).items():
        if key in obj:
            check(obj[key], f"{path}.{key}")
        elif key in required:
            _fail(f"{path}.{key}", "обязательное поле")
    return obj


def _kind(obj: Any, path: str, kinds: dict) -> str:
    if not isinstance(obj, dict):
        _fail(path, "ожидается объект")
    one_of(*kinds)(obj.get("kind"), f"{path}.kind")
    return obj["kind"]


def _property(obj: dict, path: str) -> None:
    own = props(BY_NAME[obj["device"]])
    if obj["property"] not in own:
        _fail(f"{path}.property", f"у {obj['device']} нет свойства {obj['property']!r}; есть: {', '.join(own)}")


def _target(v: Any, path: str) -> None:
    _object(v, path, {"device": device(), "property": text})
    _property(v, path)


def _action(v: Any, path: str) -> None:
    _object(v, path, {"device": device("relay"), "property": one_of("state"), "value": boolean})


def _when(v: Any, path: str) -> None:
    _object(v, path, {"device": device(), "property": text}, {"after": NONNEG} | dict.fromkeys(OPS, later))
    _property(v, path)
    ops = [op for op in OPS if op in v]
    if len(ops) != 1:
        _fail(path, f"нужно ровно одно сравнение из {', '.join(OPS)}")
    logic = BY_NAME[v["device"]].model not in MODELS
    if logic and ops != ["eq"]:
        _fail(f"{path}.{ops[0]}", "у логического свойства допустимо только eq")
    (boolean if logic else ANY)(v[ops[0]], f"{path}.{ops[0]}")


def _before_end(obj: dict, path: str, duration: float) -> None:
    if obj["t"] >= duration:
        _fail(f"{path}.t", f"момент {obj['t']} не раньше конца сценария ({duration} с)")


def _event(e: Any, path: str, duration: float) -> dict:
    kind = _kind(e, path, EVENTS)
    _object(e, path, {"t": NONNEG, "kind": text} | EVENTS[kind], {"note": text} | EVENT_OPTIONAL.get(kind, {}))
    _before_end(e, path, duration)
    return e


def _fault(f: Any, path: str, duration: float) -> dict:
    kind = _kind(f, path, FAULTS)
    models, params = FAULTS[kind]
    _object(f, path, {"kind": text, "device": device(*models), "duration_s": POS} | params,
            {"t": NONNEG, "rate_per_h": POS, "property": text, "note": text})
    if ("t" in f) == ("rate_per_h" in f):
        _fail(path, "нужно ровно одно из t (момент) и rate_per_h (случайные моменты)")
    if "t" in f:
        _before_end(f, path, duration)
    own = props(BY_NAME[f["device"]])
    if kind not in VALUE_FAULTS:
        if "property" in f:
            _fail(f"{path}.property", f"отказ {kind} действует на всё устройство")
    elif "property" in f:
        _property(f, path)
    elif len(own) > 1:
        _fail(f"{path}.property", f"обязательное поле: у {f['device']} свойства {', '.join(own)}")
    return f


def _question(q: Any, path: str, duration: float) -> dict:
    _object(q, path, {"id": text, "t": NONNEG, "text": text, "expect": _target}, {"note": text})
    _before_end(q, path, duration)
    return q


def _situation(s: Any, path: str, duration: float) -> dict:
    _object(s, path, {"id": text, "kind": text, "deadline_s": POS, "expect": one_of("notify", "actuate")},
            {"t": NONNEG, "when": _when, "action": _action, "note": text})
    if ("t" in s) == ("when" in s):
        _fail(path, "нужно ровно одно из t (момент) и when (условие на истинное значение)")
    if "t" in s:
        _before_end(s, path, duration)
    if (s["expect"] == "actuate") != ("action" in s):
        _fail(path, "action задаётся тогда и только тогда, когда expect = actuate")
    return s


def _mapping(doc: dict, key: str, path: str) -> dict:
    value = doc.get(key, {})
    if not isinstance(value, dict):
        _fail(f"{path}.{key}", "ожидается объект")
    return value


def _items(doc: dict, key: str, path: str, parse_item: Callable[[Any, str, float], dict]) -> tuple[dict, ...]:
    value = doc.get(key, [])
    if not isinstance(value, list):
        _fail(f"{path}.{key}", "ожидается список")
    return tuple(parse_item(item, f"{path}.{key}[{i}]", doc["duration_s"]) for i, item in enumerate(value))


def parse(doc: Any, source: str = "сценарий") -> Scenario:
    """Проверить документ сценария и дополнить его умолчаниями зон, улицы и устройств."""
    _object(doc, source, {"format": one_of(FORMAT), "name": text, "duration_s": POS, "start": clock},
            dict.fromkeys(("outdoor", "zones", "devices", "events", "faults", "questions", "situations"), later)
            | {"description": text})
    outdoor = OUTDOOR | _object(_mapping(doc, "outdoor", source), f"{source}.outdoor", {}, OUTDOOR_CHECKS)
    zones = {name: dict(p) for name, p in ZONES.items()}
    for name, over in _mapping(doc, "zones", source).items():
        one_of(*ZONES)(name, f"{source}.zones")
        zones[name] |= _object(over, f"{source}.zones.{name}", {}, ZONE_CHECKS)
    devices = {d.name: dict(PARAMS[d.model]) for d in DEVICES}
    for name, over in _mapping(doc, "devices", source).items():
        device()(name, f"{source}.devices")
        model, path = BY_NAME[name].model, f"{source}.devices.{name}"
        devices[name] |= _object(over, path, {}, {k: PARAM_CHECKS[k] for k in PARAMS[model]})
        if devices[name].get("period_s", math.inf) < MIN_PERIOD_S.get(model, 0):
            _fail(f"{path}.period_s", f"{model} обновляется не чаще раза в {MIN_PERIOD_S[model]} с")
    questions = _items(doc, "questions", source, _question)
    situations = _items(doc, "situations", source, _situation)
    ids = [x["id"] for x in questions + situations]
    if repeated := sorted({i for i in ids if ids.count(i) > 1}):
        _fail(source, f"повторяются id вопросов и ситуаций: {', '.join(repeated)}")
    canonical = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return Scenario(
        name=doc["name"], description=doc.get("description", ""), duration_s=float(doc["duration_s"]),
        start_s=clock_s(doc["start"]), outdoor=outdoor, zones=zones, devices=devices,
        events=_items(doc, "events", source, _event), faults=_items(doc, "faults", source, _fault),
        questions=questions, situations=situations, sha256=sha256(canonical.encode()).hexdigest())


def builtin() -> list[str]:
    """Имена встроенных сценариев."""
    return sorted(p.stem for p in BUILTIN_DIR.glob("*.json"))


def load(ref: str) -> Scenario:
    """Встроенный сценарий по имени (dense, day) или файл JSON по пути."""
    path = BUILTIN_DIR / f"{ref}.json" if ref in builtin() else Path(ref)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ScenarioError(f"{ref}: {e.strerror or e}") from e
    except ValueError as e:
        raise ScenarioError(f"{path.name}: не JSON ({e})") from e
    return parse(doc, path.name)
