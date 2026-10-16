"""Значимые ситуации — предикаты над трассой дня, заданные до прогонов (THESIS_CONCEPT §12.1).

(а) Из данных CASAS — их частота есть свойство данных, а не выбор экспериментатора:
  door_left_open  — входная дверь открыта дольше door_open_min мин; момент — open + N мин;
  door_open_late  — дверь открыли (или она открыта) после night_start; момент — открытие / night_start;
  no_motion_home  — нет движения дольше no_motion_h ч, тишина началась не у двери (житель дома) и целиком
                    лежит в «часах бодрствования» awake (по умолчанию 07:00–21:00): тишина, начавшаяся до
                    окна (утренний сон) или вечером (отход ко сну), ситуацией не считается;
  long_absence    — нет движения дольше absence_h ч после активности у двери (житель ушёл).
  «Активность у двери» — события door_front в окне [начало тишины − 10 мин; + 1 мин]; если контакта двери
  в доме нет (hh122, hh126), — срабатывания pir_entrance.
(б) Синтетические редкие — ровно synthetic_situations в сутки:
  overheat — температура котла пересекла порог (действие: выключить нагрев);
  leak     — датчик протечки сработал (действие: перекрыть кран).

Ситуация: ``{"t", "kind": "situation", "name", "significant": true, "deadline_s", "expect": {"type":
"notify_or_action", "actions"?: [...]}, "evidence": {...}}``. Агенту она не подаётся — он должен заметить её
по датчикам; дедлайн нужен метрике полноты.
"""
from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

from . import synth
from .commands import to_explicit, write

if TYPE_CHECKING:
    from .generator import DayBuilder

AWAY_BEFORE_S, AWAY_AFTER_S = 600.0, 60.0


def _hhmm_s(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 3600 + int(m) * 60


def _door_proxy(b: DayBuilder) -> str:
    if "door_front" in b.dev:
        return "door_front"
    return "pir_entrance" if "pir_entrance" in b.dev else ""


def rules(b: DayBuilder) -> dict:
    cfg = b.cfg
    leak = next(d["id"] for d in b.devices if d["type"] == "leak_sensor")
    return {
        "door_left_open": {"device": "door_front", "min_open_s": cfg.door_open_min * 60,
                           "enabled": "door_front" in b.dev},
        "door_open_late": {"device": "door_front", "after": cfg.night_start, "enabled": "door_front" in b.dev},
        "no_motion_home": {"min_silence_s": cfg.no_motion_h * 3600, "door_proxy": _door_proxy(b),
                           "away_window_s": [-AWAY_BEFORE_S, AWAY_AFTER_S], "awake": list(cfg.awake)},
        "long_absence": {"min_silence_s": cfg.absence_h * 3600, "door_proxy": _door_proxy(b),
                         "away_window_s": [-AWAY_BEFORE_S, AWAY_AFTER_S]},
        "overheat": {"device": "boiler", "property": "temperature", "threshold": synth.BOILER_THRESHOLD_C},
        "leak": {"device": leak, "property": "wet", "value": 1},
        "deadline_s": cfg.deadline_s,
        "synthetic_per_day": cfg.synthetic_situations,
    }


def _sit(b: DayBuilder, t: float, name: str, evidence: dict, source: str,
         actions: list[dict] | None = None) -> dict:
    expect: dict = {"type": "notify_or_action"}
    if actions:
        expect["actions"] = actions
        expect["actions_explicit"] = [to_explicit(a) for a in actions]
    return {"t": round(t, 3), "kind": "situation", "name": name, "significant": True,
            "deadline_s": b.cfg.deadline_s, "expect": expect, "evidence": evidence, "source": source}


def detect(b: DayBuilder) -> list[dict]:
    cfg, w0, w1 = b.cfg, b.w0, b.w1
    out: list[dict] = []
    src = "casas" if cfg.source == "casas" else "synthetic-home"
    # --- дверь ---
    if "door_front" in b.dev:
        n_open = cfg.door_open_min * 60
        late = sum(int(x) * m for x, m in zip(cfg.night_start.split(":"), (3600, 60), strict=True))
        open_since = w0 if b.initial_state["door_front"]["open"] == 1 else None
        late_done_for: float | None = None
        events = [e for e in b.sensor_log if e["device"] == "door_front"]
        # дверь, открытая до night_start и всё ещё открытая в night_start
        for e in events + [{"t": w1, "value": None}]:
            t = e["t"]
            if open_since is not None and open_since < late <= t and late < w1 and late_done_for != open_since:
                out.append(_sit(b, late, "door_open_late", {"device": "door_front", "open_since": open_since}, src))
                late_done_for = open_since
            if e["value"] == 1 and open_since is None:
                open_since = t
                if late <= t < w1:
                    out.append(_sit(b, t, "door_open_late", {"device": "door_front", "open_since": t}, src))
                    late_done_for = t
            elif e["value"] in (0, None) and open_since is not None:
                end = min(t, w1)
                if end - open_since >= n_open and open_since + n_open < w1:
                    out.append(_sit(b, open_since + n_open, "door_left_open",
                                    {"device": "door_front", "open_since": open_since, "min_open_s": n_open}, src))
                if e["value"] == 0:
                    open_since = None
    # --- тишина: нет движения дома / долгое отсутствие ---
    proxy = _door_proxy(b)
    on_times = ([b.pre_motion_t] if b.pre_motion_t is not None else []) + \
        [e["t"] for e in b.sensor_log if e["property"] == "motion" and e["value"] == 1]
    door_times = list(b.pre_door_t) + [e["t"] for e in b.sensor_log if e["device"] == proxy and
                                       (proxy == "door_front" or e["value"] == 1)]
    awake0, awake1 = (_hhmm_s(x) for x in cfg.awake)
    for a, nxt in itertools.pairwise([*on_times, float("inf")]):
        away = any(a - AWAY_BEFORE_S <= x <= a + AWAY_AFTER_S for x in door_times)
        name, thr = ("long_absence", cfg.absence_h * 3600) if away else ("no_motion_home", cfg.no_motion_h * 3600)
        if name == "no_motion_home" and not (awake0 <= a and a + thr <= awake1):
            continue                     # сон: тишина началась до часов бодрствования или не закончится в них
        fire = a + thr
        if w0 <= fire < w1 and fire < nxt:
            out.append(_sit(b, fire, name, {"last_motion_t": a, "min_silence_s": thr, "door_proxy": proxy}, src))
    # --- синтетические ---
    for t in b.overheat_t:
        if w0 <= t < w1:
            out.append(_sit(b, t, "overheat",
                            {"device": "boiler", "property": "temperature", "threshold": synth.BOILER_THRESHOLD_C},
                            "synthetic", [write("boiler", "heating", False)]))
    leak = next(d["id"] for d in b.devices if d["type"] == "leak_sensor")
    for t in b.leak_t:
        if w0 <= t < w1:
            out.append(_sit(b, t, "leak", {"device": leak, "property": "wet"}, "synthetic",
                            [write("valve_water", "open", False)]))
    out.sort(key=lambda s: (s["t"], s["name"]))
    return out
