"""Метрики PLAN §1 (K1–K9) одного прогона runner.py (README §5). Без ROS: читает каталог прогона.

  python3 -m neurobot.sim.runs.metrics RUN_DIR [--observer DIR] [--out metrics.json]

Источники: run.json (события, TTR, вмешательства, агент), truth.jsonl расписания (сегменты), наблюдатель жестов
(summary.json, gestures.jsonl — задержки, реакции, K6, K7, глухота), truth.jsonl прогона (касания, зазор, RTF, пробег,
время в движении), safety.jsonl (доли состояний safety_gate, защёлки), probe.jsonl (вызовы детектора), collect.jsonl
(память), mockllm.jsonl (вызовы LLM). Все числа — стенд: ориентир.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Optional, Sequence, Tuple

DEAF_SHARE = 0.5           # жест «глухой», если детектор отработал меньше половины кадров сегмента
MOVING_V, MOVING_W = 0.02, 0.05   # робот едет (истина Gazebo), м/с и рад/с
SERVICE_S = 5.0            # служебные вызовы LLM хода (сжатие истории PicoClaw) — до стольких с после ответа
PAUSE_S = 5.0              # стенные часы ушли вперёд монотонных больше — ВМ стояла (сон Mac)
LOST_REACTIONS = ("none", "interrupted")
CONTACT_GAP_S = 1.0        # касания с перерывом короче — один эпизод (дребезг контакта у препятствия)
REACTION_WINDOW_S = 12.0   # окно манёвра после t0 (самый длинный манёвр демо — think, 11 с)
# путь жеста: камера (кадры), драйверы (без /scan и глубины safety_gate не пустит, без базы робот не едет),
# perception, исполнитель, safety_gate; остальное — агентский путь
GESTURE_PATH = ("camera", "perception", "executor", "safety", "lidar", "base")
UNIT_ROLES = {"neurobot-perception": ("camera", "perception"), "neurobot-rosbridge": ("rosbridge",),
              "neurobot-mcp": ("mcp",), "neurobot-picoclaw": ("agent",), "neurobot-executor": ("executor",),
              "neurobot-safety": ("safety",), "neurobot-driver@lidar": ("lidar",),
              "neurobot-driver@camera": ("camera",), "neurobot-driver@twin": ("base",)}
SAFETY_ACTIONS = ("relocate", "estop_release")
SAFETY_STATES = ("ok", "slow", "blocked", "estop")
MOVE_MIN_M = 0.01          # реплика «вперёд» выполнена: робот сдвинулся по истине хотя бы на столько
MOVE_TAIL_S = 2.0          # после ответа агента робот ещё может доехать толчок
TELEPORT_M_S, TELEPORT_EPS_M = 1.0, 0.05   # скачок позы быстрее — перенос оператором или мир двойника заново, не пробег
LATCH_AFTER_S = 3.0        # защёлка safety_gate не позже стольких с после восстановления роли — следствие отказа
LATCH_TOUCH_S = 2.0        # …если рядом (± столько с) нет касания по истине: иначе её причина — касание
DEAD_END_S = 60.0          # тупик защиты: blocked без движения по истине хотя бы столько, и жесты не помогают
MIB = 1024.0
# драйверы двойника — Gazebo и мосты, узлы лидара и камеры на Python; на роботе там urg_node2, realsense2_camera и мост
# STM32: в сумму памяти пакета не входят
NOT_ROBOT = ("twin_driver", "lidar_driver", "camera_driver")


def load_jsonl(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def load_json(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def segments(truth_path: str) -> List[dict]:
    """Сегменты истины gesturegen (без заголовка)."""
    return [r for r in load_jsonl(truth_path) if "seg" in r]


def quantiles(values: Sequence[float]) -> dict:
    v = sorted(x for x in values if x is not None)
    if not v:
        return {"n": 0}

    def q(p: float) -> float:
        k = (len(v) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(v) - 1)
        return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 4)
    return {"n": len(v), "p50": q(0.5), "p95": q(0.95), "max": round(v[-1], 4)}


# ---- детектор по журналу probe

def det_rate(probe: Sequence[dict], rel0: float, rel1: float, t_mono0: float) -> Optional[float]:
    """Вызовы детектора за [rel0, rel1] с от якоря: приращения det_calls между соседними строками одного процесса,
    поделённые по доле перекрытия с окном (строки probe — раз в секунду: окно жеста короче 2 с иначе ловит одну строку
    и ноль вызовов); рестарт процесса не даёт отрицательного прироста. None — ни одна пара строк окно не перекрывает
    (процесс не пишет probe: умер или стоит)."""
    a, b = t_mono0 + rel0, t_mono0 + rel1
    total, seen, last = 0.0, False, {}
    for r in probe:
        if "det_calls" not in r:
            continue
        prev = last.get(r.get("pid"))
        last[r.get("pid")] = r
        if prev is None or r["m"] <= prev["m"]:
            continue
        lo, hi = max(a, prev["m"]), min(b, r["m"])
        if hi > lo:
            seen = True
            total += max(0, r["det_calls"] - prev["det_calls"]) * (hi - lo) / (r["m"] - prev["m"])
    return round(total, 1) if seen else None


def share_in(rows: Sequence[dict], a: float, b: float, pred) -> float:
    """Доля строк с моментом m в [a, b], для которых pred истинно (0 — строк нет)."""
    got = [pred(r) for r in rows if a <= r["m"] <= b]
    return sum(got) / len(got) if got else 0.0


def moving(r: dict) -> bool:
    return abs(r.get("v") or 0.0) > MOVING_V or abs(r.get("w") or 0.0) > MOVING_W


def deaf_gestures(segs: Sequence[dict], probe: Sequence[dict], t_mono0: float, fps: float,
                  truth: Sequence[dict] = (), safety: Sequence[dict] = (),
                  reacted: Sequence[int] = ()) -> Dict[str, List[int]]:
    """Сегменты-жесты без реакции (reacted — те, на которые робот ответил манёвром: распознал раньше конца окна и
    по замыслу перестал смотреть), во время которых детектор отработал меньше DEAF_SHARE кадров. Причина по окну жеста:
    latched — стоит аварийная защёлка safety_gate (детекция на паузе, снимает человек); busy — робот больше половины
    окна едет своим манёвром (жесты принимаются только на стоящем роботе, PLAN; perception v2 детекцию на манёвре
    ставит на паузу — L6); deaf — остальное: процесс умер, завис, поток камеры умер, кадров нет — жест потерян из-за
    глухоты, а не из-за распознавания."""
    out: Dict[str, List[int]] = {"deaf": [], "busy": [], "latched": []}
    for s in segs:
        if s["kind"] != "gesture" or s["seg"] in reacted:
            continue
        span = s["t_leave"] - s["t_start"]
        got = det_rate(probe, s["t_start"], s["t_leave"], t_mono0)
        if got is not None and got >= DEAF_SHARE * fps * span:
            continue
        a, b = t_mono0 + s["t_start"], t_mono0 + s["t_leave"]
        if any(a <= r["m"] <= b and r.get("state") == "estop" for r in safety):
            out["latched"].append(s["seg"])
        elif share_in(truth, a, b, moving) > DEAF_SHARE:
            out["busy"].append(s["seg"])
        else:
            out["deaf"].append(s["seg"])
    return out


# ---- касания и RTF по истине

def contacts(truth: Sequence[dict]) -> dict:
    """Эпизоды касания (касания с перерывами короче CONTACT_GAP_S — один эпизод), их длительность и модели;
    наименьший зазор; доля rtf_ok."""
    eps, cur, last, models = [], None, None, {}
    for r in truth:
        c = r.get("contacts") or []
        if c:
            if cur is None or r["m"] - last > CONTACT_GAP_S:
                if cur is not None:
                    eps.append(last - cur)
                cur = r["m"]
                for name in c:
                    models[name] = models.get(name, 0) + 1
            last = r["m"]
    if cur is not None:
        eps.append(last - cur)
    clear = [r["clearance_m"] for r in truth if r.get("clearance_m") is not None]
    rtf = [r for r in truth if r.get("rtf") is not None]
    return {"episodes": len(eps), "total_s": round(sum(eps), 2), "max_s": round(max(eps), 2) if eps else 0.0,
            "models": models, "min_clearance_m": round(min(clear), 4) if clear else None,
            "rtf_ok_share": round(sum(1 for r in rtf if r.get("rtf_ok")) / len(rtf), 4) if rtf else None,
            "rtf_median": quantiles([r["rtf"] for r in rtf]).get("p50")}


# ---- отказы, восстановление, видимость перезапуска

def fault_role(fault: str) -> str:
    """camera_delay → camera, kill_perception → perception, stop_perception → perception."""
    return fault.split("_", 1)[1] if fault.startswith(("kill_", "stop_")) else fault.split("_", 1)[0]


def op_roles(o: dict) -> set:
    """Роли, которые чинит действие оператора: перезапуск демо — путь камеры, PicoClaw — агент и его MCP (прокси A0 —
    дочерний процесс шлюза), юниты пакета — по UNIT_ROLES. Перенос робота и снятие защёлки — не ремонт отказа, а
    безопасность (касание, удар): ни одной роли."""
    if o.get("action") == "restart":
        return {"demo": {"camera", "perception"}, "picoclaw": {"agent", "mcp"}}.get(o.get("what"), {o.get("what")})
    units = o.get("units") or ([o["what"]] if o.get("what") else [])
    return {r for u in units for r in UNIT_ROLES.get(u.replace(".service", ""), ())}


def latches(safety: Sequence[dict], ops: Sequence[dict]) -> List[dict]:
    """Защёлки safety_gate по safety.jsonl: [{m0, m1 (None — до конца записи), reasons, released}]; released —
    её снял оператор (событие estop_release не раньше начала и не позже 5 с после конца: CLI ждёт новое состояние)."""
    out: List[dict] = []
    cur: Optional[dict] = None
    for r in safety:
        if r.get("state") == "estop" and cur is None:
            cur = {"m0": r["m"], "m1": None, "reasons": [str(x).split(":")[0] for x in r.get("reasons") or []]}
            out.append(cur)
        elif r.get("state") != "estop" and cur is not None:
            cur["m1"], cur = r["m"], None
    for c in out:
        hi = float("inf") if c["m1"] is None else c["m1"] + 5.0
        c["released"] = any(o.get("action") == "estop_release" and c["m0"] <= o["m"] <= hi for o in ops)
    return out


def fault_rows(run: dict, segs: Sequence[dict], per: Dict[int, dict], deaf: Sequence[int],
               excluded: Sequence[int] = (), safety: Sequence[dict] = (), truth: Sequence[dict] = ()) -> List[dict]:
    """Строка на отказ: TTR, вмешательства человека, видимость перезапуска по PLAN §1. Окно отказа — до
    восстановления, у невосстановившегося — до конца прогона. Человек засчитывается отказу, если в окне он чинил его
    роль (op_roles) или снимал защёлку safety_gate, вставшую в окне (до LATCH_AFTER_S после восстановления) без
    касания по истине рядом (± LATCH_TOUCH_S): такая защёлка — следствие отказа (на двойнике смерть базы — мир заново и
    удар IMU), робот стоит до человека. Путь жеста (камера, драйверы, perception, исполнитель, safety_gate): потерянные
    жесты окна — глухие или без реакции, кроме excluded (робот был занят манёвром или его остановила защита — не из-за
    отказа). Агентский путь (rosbridge, MCP, PicoClaw): реплики без ответа, каждая — последнему отказу пути, в окно
    которого она попала."""
    t0 = run["anchor"]["t_mono"]
    faults = [e for e in run["events"] if e.get("kind") == "fault"]
    ops = [e for e in run["events"] if e.get("kind") == "operator"]
    end_rel = run["events"][-1]["m"] - t0 if run["events"] else 0.0
    touched = [r["m"] for r in truth if r.get("contacts")]
    caused = [c for c in latches(safety, ops) if (c["released"] or c["m1"] is None)
              and not any(abs(m - c["m0"]) <= LATCH_TOUCH_S for m in touched)]

    def window(f: dict) -> Tuple[float, float]:
        start = f["m"] - t0
        return start, start + f["ttr_s"] if f.get("ttr_s") is not None else end_rel
    agent_faults = [(i, window(f)) for i, f in enumerate(faults)
                    if not f.get("skipped") and fault_role(f["fault"]) not in GESTURE_PATH]
    blamed: Dict[int, List[int]] = {}
    for a in run.get("agent", []):
        if a.get("ok"):
            continue
        hit = [i for i, (lo, hi) in agent_faults if lo <= a["m"] - t0 <= hi]
        if hit:
            blamed.setdefault(hit[-1], []).append(a["n"])
    out = []
    for i, f in enumerate(faults):
        ttr = f.get("ttr_s")
        row = {"fault": f["fault"], "at_s": round(f["m"] - t0, 1), "seg": f.get("seg"), "ttr_s": ttr,
               "skipped": f.get("skipped")}
        if f.get("skipped"):
            out.append(row)
            continue
        start, stop = window(f)
        role = fault_role(f["fault"])
        humans = [o for o in ops if start <= o["m"] - t0 <= stop + 1.0 and role in op_roles(o)]
        latched = [c for c in caused if start <= c["m0"] - t0 <= stop + LATCH_AFTER_S]
        why = []
        if humans:
            why.append("понадобился человек")
        if latched:
            why.append("защёлка safety_gate (%s): %s" % (", ".join(sorted({x for c in latched for x in c["reasons"]}))
                                                         or "—", "снял человек" if all(c["released"] for c in latched)
                                                         else "стоит до конца прогона"))
        if ttr is None:
            why.append("не восстановилось само")
        if role in GESTURE_PATH:
            hit = [s for s in segs if s["kind"] == "gesture" and s["t_leave"] >= start and s["t_start"] <= stop]
            lost = [s["seg"] for s in hit if s["seg"] not in excluded and (
                s["seg"] in deaf or per.get(s["seg"], {}).get("reaction") in LOST_REACTIONS)]
            later = [s for s in segs if s["kind"] == "gesture" and s["t_start"] > start]
            gap = later[0]["t_start"] - start if later else None
            if lost:
                why.append("потеряно жестов: %d" % len(lost))
            if ttr is not None and gap is not None and ttr > gap:
                why.append("недоступна дольше паузы до жеста (%.1f > %.1f с)" % (ttr, gap))
            row.update(lost_gestures=lost, gap_to_next_gesture_s=None if gap is None else round(gap, 1))
        else:
            failed = blamed.get(i, [])
            if failed:
                why.append("реплик без ответа: %d" % len(failed))
            row.update(agent_failed=failed)
        row.update(path="жест" if role in GESTURE_PATH else "агент",
                   operator=[o["action"] + ":" + str(o.get("what") or "") for o in humans] +
                   ["estop_release:" for c in latched if c["released"]],
                   latch=[{"at_s": round(c["m0"] - t0, 1), "reasons": c["reasons"], "released": c["released"]}
                          for c in latched], visible=bool(why), why=why)
        out.append(row)
    return out


# ---- агент и LLM

def request_windows(run: dict) -> List[Tuple[float, float, int]]:
    """Окна реплик пользователя по стенным часам: от начала реплики до ответа + SERVICE_S (не дальше следующей)."""
    reqs = sorted(run.get("agent", []), key=lambda r: r["t"])
    out = []
    for i, r in enumerate(reqs):
        hi = r["t"] + (r.get("latency_s") or 0.0) + SERVICE_S
        if i + 1 < len(reqs):
            hi = min(hi, reqs[i + 1]["t"])
        out.append((r["t"], hi, r["n"]))
    return out


def service_calls(run: dict, llm: Sequence[dict], model: str) -> Dict[int, int]:
    """Вызовы LLM этой системы (по имени модели) без ожидания оракула внутри окна реплики — служебные вызовы её хода
    (сжатие истории сессии PicoClaw): {номер реплики: вызовов}."""
    out: Dict[int, int] = {}
    wins = request_windows(run)
    for r in llm:
        if r.get("type") != "llm" or r.get("model") != model or (r.get("oracle") or {}).get("input_ids"):
            continue
        n = next((n for lo, hi, n in wins if lo <= float(r["ts_wall"]) <= hi), None)
        if n is not None:
            out[n] = out.get(n, 0) + 1
    return out


def agent_summary(run: dict, llm: Sequence[dict], model: str) -> dict:
    """Реплики пользователя: успешные (финальный ответ и все инструменты без ошибки), задержка; вызовы LLM по
    input_id оракула и служебные вызовы хода (service_calls)."""
    prefix = "%s-%s-" % (run["label"], run["system"])
    mine = [r for r in llm if r.get("type") == "llm" and any(
        i.startswith(prefix) for i in (r.get("oracle") or {}).get("input_ids", []))]
    reqs = run.get("agent", [])
    per_req = {}
    for r in mine:
        for i in r["oracle"]["input_ids"]:
            if i.startswith(prefix):
                per_req[i] = per_req.get(i, 0) + 1
    failed = [{"n": r["n"], "request": r["request"], "error": r.get("error"),
               "tools": [t["content"][:120] for t in r.get("tools", []) if not t["ok"]]}
              for r in reqs if not r.get("ok")]
    prompt = [r["usage"]["prompt_tokens"] for r in mine if r.get("usage")]
    service = service_calls(run, llm, model)
    return {"requests": len(reqs), "ok": sum(1 for r in reqs if r.get("ok")), "failed": failed,
            "latency_s": quantiles([r["latency_s"] for r in reqs if r.get("ok")]),
            "llm_calls": len(mine), "llm_calls_per_request": quantiles(list(per_req.values())),
            "service_calls": sum(service.values()), "service_calls_by_request": service,
            "prompt_tokens_per_call": quantiles(prompt), "n_tools_in_catalog": sorted({r.get("n_tools") for r in mine})}


def llm_outside_requests(run: dict, llm: Sequence[dict], model: str) -> int:
    """Вызовы LLM этой системы (по имени модели) вне окон реплик пользователя — на жест их быть не должно (K4)."""
    lo, hi = run["t_start"], run["t_end"]
    wins = request_windows(run)
    return sum(1 for r in llm if r.get("type") == "llm" and r.get("model") == model
               and lo <= float(r["ts_wall"]) <= hi and not (r.get("oracle") or {}).get("input_ids")
               and not any(a <= float(r["ts_wall"]) <= b for a, b, _ in wins))


# ---- память по сэмплеру

def memory(collect: Sequence[dict], skip_s: float = 120.0) -> dict:
    """PSS по компонентам (сумма процессов юнита), МиБ: медиана в начале и в конце окна и наклон МиБ/ч
    (МНК по сэмплам после skip_s от первого сэмпла — подъём не в счёт); _total — компоненты робота (без NOT_ROBOT).
    Перезапуск процесса виден как ступень."""
    samples = [s for s in collect if s.get("type") == "sample"]
    if len(samples) < 4:
        return {}
    t_first = samples[0]["t_mono"]
    samples = [s for s in samples if s["t_mono"] - t_first >= skip_s] or samples
    series: Dict[str, List[Tuple[float, float]]] = {}
    for s in samples:
        total = 0.0
        for name, u in s["units"].items():
            pss = sum(p.get("pss_kib", 0) for p in u.get("procs", [])) / MIB
            series.setdefault(name, []).append((s["t_mono"], pss))
            if name not in NOT_ROBOT:
                total += pss
        series.setdefault("_total", []).append((s["t_mono"], total))
    out = {}
    for name, pts in series.items():
        k = max(1, len(pts) // 10)
        first = sorted(v for _, v in pts[:k])[k // 2]
        last = sorted(v for _, v in pts[-k:])[k // 2]
        out[name] = {"pss_mib_start": round(first, 1), "pss_mib_end": round(last, 1),
                     "slope_mib_h": slope(pts), "hours": round((pts[-1][0] - pts[0][0]) / 3600, 3)}
    return out


def slope(pts: Sequence[Tuple[float, float]]) -> Optional[float]:
    if len(pts) < 2:
        return None
    n = len(pts)
    mx = sum(t for t, _ in pts) / n
    my = sum(v for _, v in pts) / n
    den = sum((t - mx) ** 2 for t, _ in pts)
    if den == 0:
        return None
    return round(sum((t - mx) * (v - my) for t, v in pts) / den * 3600, 3)


def latency(per: Sequence[dict], key: str, rtf_min: float = 0.95) -> dict:
    """Задержка t0 → key (cmd — первая ненулевая /cmd_vel, motion — начало движения) по жестам наблюдателя: без
    ранних (реакция раньше t0 — perception разрезал длинный жест и поехал по первой части: «задержки» нет) — K1/K2;
    ранние — отдельно. Жесты при RTF < rtf_min не входят (как у наблюдателя)."""
    vals = [p for p in per if p.get("kind") == "gesture" and key in (p.get("lat") or {})
            and (p.get("rtf") is None or p["rtf"] >= rtf_min)]
    early = [p["lat"][key] for p in vals if p.get("early")]
    return {"on_time": quantiles([p["lat"][key] for p in vals if not p.get("early")]), "early": quantiles(early)}


def travel(truth: Sequence[dict], safety: Sequence[dict], gap_s: float = 2.0) -> dict:
    """Пробег и время в движении по истине: перерывы записи дольше gap_s (пауза ВМ, мир двойника поднимается заново)
    не в счёт, скачок позы быстрее TELEPORT_M_S — перенос робота оператором или новый мир — не пробег (teleports);
    доли времени в состояниях safety_gate (строка safety.jsonl действует до следующей)."""
    dist = moving_s = jumps = 0.0
    n_jumps = 0
    for a, b in zip(truth, truth[1:]):
        dt = b["m"] - a["m"]
        if dt > gap_s or a.get("x") is None or b.get("x") is None:
            continue
        step = ((b["x"] - a["x"]) ** 2 + (b["y"] - a["y"]) ** 2) ** 0.5
        if step > TELEPORT_M_S * dt + TELEPORT_EPS_M:
            n_jumps += 1
            jumps += step
            continue
        dist += step
        if moving(a):
            moving_s += dt
    spans: Dict[str, float] = dict.fromkeys(SAFETY_STATES, 0.0)
    for a, b in zip(safety, safety[1:]):
        dt = b["m"] - a["m"]
        if a.get("state") in spans and dt <= gap_s:
            spans[a["state"]] += dt
    total = sum(spans.values())
    span = truth[-1]["m"] - truth[0]["m"] if len(truth) > 1 else 0.0
    return {"distance_m": round(dist, 2), "moving_s": round(moving_s, 1),
            "moving_share": round(moving_s / span, 4) if span else None,
            "teleports": {"n": n_jumps, "m": round(jumps, 2)},
            "safety_share": {k: round(v / total, 4) for k, v in spans.items()} if total else {}}


def k6_outage(run_dir: str, truth: Sequence[dict], gap_s: float = 2.0) -> dict:
    """Эпизоды K6 наблюдателя (движение без свежей команды, кроме торможения после нуля), пришедшиеся на перерыв
    истины двойника дольше gap_s: мир не шёл (смерть базы — Gazebo заново), трекер движения держит последнее «едет»,
    а робот по истине стоит. Интервалы — тем же разбором наблюдателя (neurobot.sim.gestures.analyze) по events.jsonl
    прогона. {n, total_s, episodes: [{a_s, b_s, cause}]} от якоря; None — нет записи наблюдателя или якоря."""
    events, anchor_path = os.path.join(run_dir, "events.jsonl"), os.path.join(run_dir, "anchor.json")
    if not (os.path.exists(events) and os.path.exists(anchor_path)):
        return None
    from neurobot.sim.gestures import analyze
    from neurobot.sim.gestures.clock import read_anchor
    anchor = read_anchor(anchor_path, live=False)
    sessions, _ = analyze.select_sessions(analyze.read_events(events), anchor)
    cfg = analyze.load_config()
    unsafe = [u for evs in sessions if evs for u in analyze._session(evs, cfg)["unsafe"]
              if u["cause"] != "braking_after_zero"]
    gaps = [(a["m"], b["m"]) for a, b in zip(truth, truth[1:]) if b["m"] - a["m"] > gap_s]
    hit = [u for u in unsafe if any(lo < u["b"] and u["a"] < hi for lo, hi in gaps)]
    return {"n": len(hit), "total_s": round(sum(u["b"] - u["a"] for u in hit), 2),
            "episodes": [{"a_s": round(u["a"] - anchor.t_mono, 1), "b_s": round(u["b"] - anchor.t_mono, 1),
                          "cause": u["cause"]} for u in hit]}


def move_outcome(a: dict, moved: bool) -> str:
    """Исход реплики «вперёд»: moved — робот поехал; refused — не поехал, агент сообщил об отказе safety_gate с
    причиной (blocked, estop); false_ok — не поехал, а агент ответил «готово» (инструмент ok: true) — неправда о
    роботе; unconfirmed — инструмент честно не смог подтвердить толчок (safety_gate его не видел); no_answer — сбой
    агентского пути или ошибка инструмента."""
    if moved:
        return "moved"
    if a.get("ok") and a.get("refused"):
        return "refused"
    if a.get("ok"):
        return "false_ok"
    if any(re.search(r'"status":\s*"unconfirmed"', t.get("content", "")) for t in a.get("tools") or []):
        return "unconfirmed"
    return "no_answer"


MOVE_OUTCOMES = ("moved", "refused", "false_ok", "unconfirmed", "no_answer")


def move_requests(run: dict, truth: Sequence[dict]) -> dict:
    """Реплики «вперёд» (move_outcome): выполнена, если робот поехал (сдвиг по истине ≥ MOVE_MIN_M за ход и
    MOVE_TAIL_S после) или агент сообщил о запрете safety_gate с причиной; ok: true без движения — не успех."""
    rows = []
    for a in run.get("agent", []):
        if a.get("request") != "move":
            continue
        hi = a["m"] + (a.get("latency_s") or 0.0) + MOVE_TAIL_S
        pts = [r for r in truth if a["m"] <= r["m"] <= hi and r.get("x") is not None]
        shift = max((((r["x"] - pts[0]["x"]) ** 2 + (r["y"] - pts[0]["y"]) ** 2) ** 0.5 for r in pts), default=0.0)
        outcome = move_outcome(a, shift >= MOVE_MIN_M)
        rows.append({"n": a["n"], "moved_m": round(shift, 3), "outcome": outcome, "refused": a.get("refused") or [],
                     "success": outcome in ("moved", "refused")})
    out = {k: sum(1 for r in rows if r["outcome"] == k) for k in MOVE_OUTCOMES}
    out.update(n=len(rows), success=sum(r["success"] for r in rows), rows=rows)
    return out


def safety_stops(per: Dict[int, dict], segs: Sequence[dict], safety: Sequence[dict], t_mono0: float) -> dict:
    """Реакции «оборвана» и «нет» (A1), в окне манёвра которых safety_gate был в blocked или estop: остановила защита
    (препятствие, слепой сектор, геозабор), а не отказ — по причинам safety_state."""
    out: Dict[str, dict] = {}
    for s in segs:
        p = per.get(s["seg"], {})
        if s["kind"] != "gesture" or p.get("reaction") not in LOST_REACTIONS:
            continue
        a, b = t_mono0 + s["t0"], t_mono0 + s["t0"] + REACTION_WINDOW_S
        hits = [r for r in safety if a <= r["m"] <= b and r.get("state") in ("blocked", "estop")]
        if hits:
            reasons = sorted({str(x).split(":")[0] for r in hits for x in (r.get("reasons") or [])})
            out[str(s["seg"])] = {"reaction": p["reaction"], "reasons": reasons}
    return {"n": len(out), "by_seg": out}


def dead_ends(safety: Sequence[dict], truth: Sequence[dict], segs: Sequence[dict], t_mono0: float,
              min_s: float = DEAD_END_S) -> dict:
    """Тупики защиты: safety_gate держит blocked без перерыва не меньше min_s, робот по истине всё это время стоит, и
    в это время были жесты — ни один не вывел его (например, у ножки стула запрещены и путь вперёд, и поворот на
    месте). Выйти робот может только с помощью человека (или нового мира двойника после смерти базы); оператор прогона
    тупики не чинит — они видны здесь. {n, total_s, gestures, spans: [{at_s, s, reasons, gestures}]}."""
    spans: List[list] = []
    for a, b in zip(safety, safety[1:]):
        if a.get("state") != "blocked":
            continue
        reasons = {str(x).split(":")[0] for x in a.get("reasons") or []}
        if spans and spans[-1][1] == a["m"]:
            spans[-1][1] = b["m"]
            spans[-1][2] |= reasons
        else:
            spans.append([a["m"], b["m"], reasons])
    out = []
    for lo, hi, reasons in spans:
        if hi - lo < min_s or any(lo <= r["m"] <= hi and moving(r) for r in truth):
            continue
        hit = [s["seg"] for s in segs if s["kind"] == "gesture" and lo <= t_mono0 + s["t0"] <= hi]
        if hit:
            out.append({"at_s": round(lo - t_mono0, 1), "s": round(hi - lo, 1), "reasons": sorted(reasons),
                        "gestures": hit})
    return {"n": len(out), "total_s": round(sum(x["s"] for x in out), 1),
            "gestures": sum(len(x["gestures"]) for x in out), "spans": out}


def escapes(executor: Sequence[dict], safety: Sequence[dict], t_mono0: float) -> dict:
    """Тупики safety_gate и выход из них (повторная приёмка 18.10): эпизоды по safety.jsonl, где поворот на месте
    заденет (touch), а вперёд нельзя (blocked) или только подползти к препятствию (approach, obstacle, cliff), — как их
    видит исполнитель (guard.dead_end); отъезды исполнителя назад — по его событиям (executor.jsonl: escape start,
    task_end escape). Эпизод вышел отъездом, если отъезд начался в нём. reverse_limit — назад уже нельзя, выхода нет.
    {episodes, escaped, max_s, total_s, starts, outcomes: {status: n}, spans: [{at_s, s, escaped, reasons}]}."""
    spans: List[list] = []
    for a, b in zip(safety, safety[1:]):
        reasons = {str(x).split(":")[0] for x in a.get("reasons") or []}
        near = a.get("state") == "blocked" or (a.get("state") == "slow" and reasons & {"approach", "obstacle", "cliff"})
        if "touch" not in reasons or not near:
            continue
        if spans and spans[-1][1] == a["m"]:
            spans[-1][1] = b["m"]
            spans[-1][2] |= reasons
        else:
            spans.append([a["m"], b["m"], reasons])
    starts = [e["m"] for e in executor if e.get("type") == "escape" and e.get("status") == "start"]
    outcomes: Dict[str, int] = {}
    for e in executor:
        if e.get("type") == "task_end" and e.get("task") == "escape":
            outcomes[e["status"]] = outcomes.get(e["status"], 0) + 1
    out = [{"at_s": round(lo - t_mono0, 1), "s": round(hi - lo, 1), "escaped": any(lo <= t <= hi for t in starts),
            "reasons": sorted(reasons)} for lo, hi, reasons in spans]
    return {"episodes": len(out), "escaped": sum(1 for x in out if x["escaped"]),
            "max_s": max((x["s"] for x in out), default=0.0), "total_s": round(sum(x["s"] for x in out), 1),
            "starts": len(starts), "outcomes": outcomes, "spans": out}


def reaction_breakdown(per: Dict[int, dict], segs: Sequence[dict], stopped: dict) -> dict:
    """Разбор «верной реакции» (наблюдатель: класс распознан верно и манёвр демо исполнен целиком) по классу решающего
    сообщения perception: ok — верный, wrong — другой класс, none — сообщения нет или «не распознано» (−1), unknown —
    система класс не публикует (демо A0: судить можно только по манёвру). В каждой группе — реакции; safety — из
    оборванных и не начатых при верном классе сколько остановил safety_gate (stopped_by_safety) и по каким причинам."""
    published = any(p.get("rec") for p in per.values())
    groups: Dict[str, Dict[str, int]] = {}
    cut = {"interrupted": 0, "none": 0}
    reasons: Dict[str, int] = {}
    gestures = [s for s in segs if s["kind"] == "gesture"]
    for s in gestures:
        p = per.get(s["seg"], {})
        rec = p.get("rec") or {}
        key = "unknown" if not published else "none" if rec.get("cls") in (None, -1) else \
            "ok" if rec["cls"] == p.get("cls") else "wrong"
        reaction = p.get("reaction") or "unobserved"
        g = groups.setdefault(key, {})
        g[reaction] = g.get(reaction, 0) + 1
        hit = (stopped.get("by_seg") or {}).get(str(s["seg"]))
        if key == "ok" and hit and reaction in cut:
            cut[reaction] += 1
            for r in hit["reasons"]:
                reasons[r] = reasons.get(r, 0) + 1
    return {"gestures": len(gestures), "by_class": groups, "safety": dict(cut, reasons=reasons)}


def drift(per: Sequence[dict], key: str = "cmd") -> dict:
    """Дрейф задержки t0 → /cmd_vel: p50 и p95 первой и последней трети жестов прогона."""
    vals = [p["lat"][key] for p in sorted(per, key=lambda p: p["seg"])
            if p.get("kind") == "gesture" and key in (p.get("lat") or {})]
    third = len(vals) // 3
    if third < 3:
        return {"n": len(vals)}
    return {"n": len(vals), "first": quantiles(vals[:third]), "last": quantiles(vals[-third:])}


def pauses(events: Sequence[dict], t_mono0: float) -> List[dict]:
    """Остановки ВМ (сон Mac): между соседними событиями стенные часы ушли вперёд монотонных больше PAUSE_S. Монотонные
    часы ВМ на паузе стоят, поэтому таймлайн и метрики по ним паузы не видят; после неё у узлов ROS скачок стенного
    времени."""
    out = []
    for a, b in zip(events, events[1:]):
        jump = (b["t"] - a["t"]) - (b["m"] - a["m"])
        if jump > PAUSE_S:
            out.append({"after_s": round(a["m"] - t_mono0, 1), "before_s": round(b["m"] - t_mono0, 1),
                        "wall_jump_s": round(jump, 1)})
    return out


# ---- сборка

MODELS = {"a0": "openai/gpt-4o-mini", "a1": "gpt-4o-mini"}


def summarize(run_dir: str, truth_path: Optional[str] = None, observer_dir: Optional[str] = None) -> dict:
    """observer_dir — каталог с summary.json и gestures.jsonl разбора наблюдателя, сделанного заново
    (python -m neurobot.sim.gestures.analyze по events.jsonl прогона); по умолчанию — разбор из прогона."""
    run = load_json(os.path.join(run_dir, "run.json")) or {}
    if "anchor" not in run:
        return {"run_dir": run_dir, "error": run.get("error", "нет run.json или прогон не дошёл до якоря")}
    truth_path = truth_path or os.path.join(run_dir, "truth_schedule.jsonl")
    t_end_rel = run["events"][-1]["m"] - run["anchor"]["t_mono"]
    segs = [s for s in segments(truth_path) if s["t_leave"] <= t_end_rel]   # дошедшие до конца записи
    obs_dir = observer_dir or run_dir
    obs = load_json(os.path.join(obs_dir, "summary.json")) or {}
    per = {p["seg"]: p for p in load_jsonl(os.path.join(obs_dir, "gestures.jsonl")) if "seg" in p}
    probe = load_jsonl(os.path.join(run_dir, "probe.jsonl"))
    truth = load_jsonl(os.path.join(run_dir, "truth.jsonl"))
    safety = load_jsonl(os.path.join(run_dir, "safety.jsonl"))
    t_mono0 = run["anchor"]["t_mono"]
    fps = 10.0
    reacted = {n for n, p in per.items() if p.get("reaction") in ("correct", "wrong", "interrupted")}
    lost = deaf_gestures(segs, probe, t_mono0, fps, truth, safety, reacted)
    deaf = lost["deaf"]
    stopped = safety_stops(per, segs, safety, t_mono0)
    llm = load_jsonl(os.path.join(run_dir, "mockllm.jsonl"))
    events = run["events"]
    ops = [e for e in events if e.get("kind") == "operator"]
    lat = obs.get("latency_s", {})
    gestures = [s for s in segs if s["kind"] == "gesture"]
    faults = fault_rows(run, segs, per, deaf, set(lost["busy"]) | {int(k) for k in stopped["by_seg"]}, safety, truth)
    model = MODELS[run["system"]]
    return {
        "system": run["system"], "label": run["label"], "profile": run["plan"]["profile"],
        "world": os.path.basename(run.get("world") or "hall.sdf").rsplit(".", 1)[0],
        "no_return": run.get("no_return", "nan"),
        "duration_s": round(events[-1]["m"] - events[0]["m"], 1),
        "wall_duration_s": round(run["t_end"] - run["t_start"], 1),
        "vm_pauses": pauses(events, t_mono0),
        "segments": {"gestures": len(gestures), "negatives": len(segs) - len(gestures)},
        "K1_t0_to_cmd_s": latency(list(per.values()), "cmd")["on_time"],
        "K2_t0_to_motion_s": latency(list(per.values()), "motion")["on_time"],
        "early_t0_to_cmd_s": latency(list(per.values()), "cmd")["early"],
        "K1_observer_all_s": lat.get("cmd"), "latency_excluded_rtf": lat.get("excluded_rtf"),
        "reactions": obs.get("reactions"), "late": obs.get("late"), "visible_failures_observer":
            obs.get("visible_failures"), "recognition": obs.get("recognition"),
        "K3": {"deaf_gestures": deaf, "deaf_gestures_n": len(deaf), "busy_gestures": lost["busy"],
               "latched_gestures": lost["latched"], "operator_interventions": len(ops),
               "operator_safety": sum(1 for o in ops if o["action"] in SAFETY_ACTIONS),
               "operator": [{"at_s": round(o["m"] - t_mono0, 1), "action": o["action"], "what": o.get("what"),
                             "why": o.get("why") or o.get("reasons")} for o in ops],
               "visible_restarts": sum(1 for f in faults if f.get("visible")),
               "invisible_restarts": sum(1 for f in faults if f.get("visible") is False),
               "deaf_probe": (obs.get("deaf") or {}).get("probe"),
               "deaf_probe_standing": (obs.get("deaf") or {}).get("probe_standing"),
               "deaf_health": (obs.get("deaf") or {}).get("health")},
        "K4": {"llm_calls_outside_requests": llm_outside_requests(run, llm, model),
               "agent": agent_summary(run, llm, model)},
        "K5_faults": faults,
        "stopped_by_safety": stopped,
        "reaction_breakdown": reaction_breakdown(per, segs, stopped),
        "dead_ends": dead_ends(safety, truth, segs, t_mono0),
        "escapes": escapes(load_jsonl(os.path.join(run_dir, "executor.jsonl")), safety, t_mono0),
        "K6_unsafe_no_fresh_cmd": obs.get("unsafe_no_fresh_cmd"),
        "K6_world_outage": k6_outage(run_dir, truth),
        "K7": obs.get("k7"), "false_actions": obs.get("false_actions"),
        "touches": contacts(truth),
        "travel": travel(truth, safety),
        "move_requests": move_requests(run, truth),
        "K8": {"memory": memory(load_jsonl(os.path.join(run_dir, "collect.jsonl"))),
               "drift_cmd": drift(list(per.values()))},
        "restarts": run.get("restarts"),
        "rtf_observer": obs.get("rtf"),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m neurobot.sim.runs.metrics", description=__doc__.split("\n\n")[0])
    p.add_argument("run_dir", help="каталог прогона (runner.py)")
    p.add_argument("--truth", default="", help="истина расписания (по умолчанию RUN_DIR/truth_schedule.jsonl)")
    p.add_argument("--observer", default="", help="каталог разбора наблюдателя заново (summary.json, gestures.jsonl); "
                   "по умолчанию — RUN_DIR")
    p.add_argument("--out", default="", help="файл JSON (по умолчанию stdout)")
    a = p.parse_args(argv)
    doc = summarize(a.run_dir, a.truth or None, a.observer or None)
    text = json.dumps(doc, ensure_ascii=False, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 1 if "error" in doc else 0


if __name__ == "__main__":
    sys.exit(main())
