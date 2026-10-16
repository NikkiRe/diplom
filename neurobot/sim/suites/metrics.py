"""Метрики наборов «датчики работают» и «безопасность» на двойнике — без rclpy (Python ≥ 3.8, тесты на Mac).

Образцы — словари с меткой «t» (монотонные секунды ВМ, время приёма): /twin/truth (истина Gazebo: поза, v, w,
clearance_m, contacts, rtf_ok), /neurobot/safety_state, /cmd_vel. В зачёт истины идут только образцы с rtf_ok:
при RTF ≪ 1 страж успевает больше, чем на роботе, и тормозной путь выходит лучше настоящего (README двойника §2.3).
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

V_STILL = 0.005   # м/с по истине: база стоит
STILL_S = 0.3     # с подряд — встала


def nan_runs(ranges: Sequence[Optional[float]], angle_min: float, increment: float, min_len: int = 1) -> list:
    """Серии неконечных значений (NaN, ±inf, None) скана: [[от°, до°, лучей], …] — не короче min_len лучей."""
    out, start = [], None
    for i, r in enumerate(list(ranges) + [0.0]):
        bad = i < len(ranges) and (r is None or not math.isfinite(r))
        if bad and start is None:
            start = i
        elif not bad and start is not None:
            if i - start >= min_len:
                out.append([round(math.degrees(angle_min + start * increment), 1),
                             round(math.degrees(angle_min + (i - 1) * increment), 1), i - start])
            start = None
    return out


def block_at(runs: Sequence[list], deg: float, max_hole_deg: float) -> Optional[list]:
    """Серия из nan_runs, накрывающая пеленг deg, со склеенными соседями через дыру не шире max_hole_deg
    (у двойника луч 213 проходит сквозь корпус камеры — артефакт gpu_lidar); [от°, до°, лучей в сериях] или None."""
    merged: List[list] = []
    for lo, hi, n in sorted(runs):
        if merged and lo - merged[-1][1] <= max_hole_deg:
            merged[-1] = [merged[-1][0], hi, merged[-1][2] + n]
        else:
            merged.append([lo, hi, n])
    return next((r for r in merged if r[0] <= deg <= r[1]), None)


def rate(stamps: Sequence[float]) -> Optional[float]:
    """Частота по меткам времени, Гц (меньше двух меток — None)."""
    if len(stamps) < 2 or stamps[-1] <= stamps[0]:
        return None
    return (len(stamps) - 1) / (stamps[-1] - stamps[0])


def ok_truth(truth: Iterable[dict], t0: float = -math.inf, t1: float = math.inf) -> List[dict]:
    """Образцы истины в окне [t0, t1] с rtf_ok."""
    return [r for r in truth if t0 <= r["t"] <= t1 and r.get("rtf_ok")]


def min_clearance(truth: Iterable[dict]) -> Optional[float]:
    """Наименьший зазор до препятствий; образцы, где форма модели ещё неизвестна (null), не в счёт."""
    values = [r["clearance_m"] for r in truth if r.get("clearance_m") is not None]
    return min(values) if values else None


def touched(truth: Iterable[dict]) -> List[str]:
    """Модели, которых робот касался (по истине)."""
    return sorted({c for r in truth for c in r.get("contacts", [])})


def path_length(truth: Sequence[dict]) -> float:
    """Путь базы по истине, м."""
    return sum(math.hypot(b["x"] - a["x"], b["y"] - a["y"]) for a, b in zip(truth, truth[1:]))


def stopped_at(truth: Sequence[dict], since: float) -> Optional[dict]:
    """Первый образец после since, с которого база стоит (|v| < V_STILL) не меньше STILL_S; None — не встала."""
    start = None
    for r in truth:
        if r["t"] < since:
            continue
        if abs(r["v"]) < V_STILL and abs(r.get("w", 0.0)) < 10 * V_STILL:
            start = start or r
            if r["t"] - start["t"] >= STILL_S:
                return start
        else:
            start = None
    return None


def braking(truth: Sequence[dict], since: float) -> Dict[str, Optional[float]]:
    """Торможение от события since (решение «ноль», kill, тишина): время до остановки по истине и путь за это время
    (тормозной путь), скорость в момент события."""
    after = [r for r in truth if r["t"] >= since]
    stop = stopped_at(truth, since)
    if not after:
        return {"v0": None, "stop_s": None, "coast_m": None}
    v0 = after[0]["v"]
    if stop is None:
        return {"v0": round(v0, 3), "stop_s": None, "coast_m": None}
    leg = [r for r in after if r["t"] <= stop["t"]]
    return {"v0": round(v0, 3), "stop_s": round(stop["t"] - since, 3), "coast_m": round(path_length(leg), 3)}


def first(samples: Iterable[dict], since: float, pred) -> Optional[dict]:
    """Первый образец не раньше since, для которого pred истинно."""
    return next((s for s in samples if s["t"] >= since and pred(s)), None)


def zero_after(cmds: Sequence[dict], since: float) -> Optional[float]:
    """Момент первого нулевого /cmd_vel не раньше since после ненулевого (решение safety_gate «стоп»)."""
    moving = False
    for c in cmds:
        if c["t"] < since:
            moving = c["v"] != 0.0 or c["w"] != 0.0
            continue
        if c["v"] == 0.0 and c["w"] == 0.0:
            if moving:
                return c["t"]
        else:
            moving = True
    return None


def stop_reason(states: Sequence[dict], t: Optional[float]) -> List[str]:
    """Коды причин safety_state, действующие в момент t (последнее состояние не позже t + 0,25 с)."""
    if t is None:
        return []
    before = [s for s in states if s["t"] <= t + 0.25]
    return sorted({r.split(":", 1)[0] for r in before[-1]["reasons"]}) if before else []


HAZARD_CODES = ("obstacle", "cliff", "approach", "lidar_nodata", "touch")
FALSE_CLEAR_M = 0.5   # м: робот встал по «опасности», а по истине до ближайшего препятствия дальше — ложный стоп


def false_stops(states: Sequence[dict], truth: Sequence[dict]) -> List[dict]:
    """Ложные остановки: safety_state перешёл в blocked с причиной-опасностью (препятствие, обрыв, «нет данных»
    лидара), а по истине зазор до ближайшего препятствия больше FALSE_CLEAR_M. Обрыв (край площадки) истина не видит —
    его стоп ложным не считается."""
    out, prev = [], None
    for s in states:
        blocked = s["state"] == "blocked" and any(r.split(":", 1)[0] in HAZARD_CODES for r in s["reasons"])
        if blocked and prev != "blocked":
            near = [r for r in truth if abs(r["t"] - s["t"]) <= 0.25 and r.get("clearance_m") is not None]
            codes = sorted({r.split(":", 1)[0] for r in s["reasons"]})
            if near and "cliff" not in codes and min(r["clearance_m"] for r in near) > FALSE_CLEAR_M:
                out.append({"t": s["t"], "codes": codes, "clearance_m": min(r["clearance_m"] for r in near)})
        prev = "blocked" if blocked else s["state"]
    return out


def in_frame(x: float, y: float, yaw: float, origin: Tuple[float, float, float]) -> Tuple[float, float, float]:
    """Поза (x, y, yaw) в системе позы origin."""
    ox, oy, oyaw = origin
    c, s = math.cos(oyaw), math.sin(oyaw)
    dx, dy = x - ox, y - oy
    return c * dx + s * dy, -s * dx + c * dy, math.atan2(math.sin(yaw - oyaw), math.cos(yaw - oyaw))


def odom_error(odom: Sequence[dict], truth: Sequence[dict]) -> Dict[str, float]:
    """Одометрия против истины в системе позы старта каждой: наибольшая ошибка положения (м) и курса (°) и в конце."""
    if not odom or not truth:
        return {}
    o0 = (odom[0]["x"], odom[0]["y"], odom[0]["yaw"])
    g0 = (truth[0]["x"], truth[0]["y"], truth[0]["yaw"])
    worst_p = worst_a = 0.0
    end = (0.0, 0.0)
    for o in odom:
        g = min(truth, key=lambda r: abs(r["t"] - o["t"]))
        ox, oy, oa = in_frame(o["x"], o["y"], o["yaw"], o0)
        gx, gy, ga = in_frame(g["x"], g["y"], g["yaw"], g0)
        dp = math.hypot(ox - gx, oy - gy)
        da = abs(math.degrees(math.atan2(math.sin(oa - ga), math.cos(oa - ga))))
        worst_p, worst_a = max(worst_p, dp), max(worst_a, da)
        end = (dp, da)
    return {"max_pos_m": round(worst_p, 4), "max_yaw_deg": round(worst_a, 2), "end_pos_m": round(end[0], 4),
            "end_yaw_deg": round(end[1], 2), "path_m": round(path_length(truth), 3)}


def tilt_deg(ax: float, ay: float, az: float) -> float:
    """Наклон по ускорению покоя, °: угол между g и ближайшей к нему осью кадра IMU (у оптического кадра realsense
    в покое g — вдоль y)."""
    g = math.sqrt(ax * ax + ay * ay + az * az)
    if g == 0.0:
        return math.nan
    return math.degrees(math.acos(min(1.0, max(abs(ax), abs(ay), abs(az)) / g)))
