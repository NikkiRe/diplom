"""Метрики PLAN §1 (K1–K9) одного прогона runner.py (README §5). Без ROS: читает каталог прогона.

  python3 -m neurobot.sim.runs.metrics RUN_DIR [--out metrics.json]

Источники: run.json (события, TTR, вмешательства, агент), truth.jsonl расписания (сегменты), наблюдатель жестов
(summary.json, gestures.jsonl — задержки, реакции, K6, K7, глухота), truth.jsonl прогона (касания, зазор, RTF),
probe.jsonl (вызовы детектора), collect.jsonl (память), mockllm.jsonl (вызовы LLM). Все числа — стенд: ориентир.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

DEAF_SHARE = 0.5           # жест «глухой», если детектор отработал меньше половины кадров сегмента
LOST_REACTIONS = ("none", "interrupted")
CONTACT_GAP_S = 1.0        # касания с перерывом короче — один эпизод (дребезг контакта у препятствия)
REACTION_WINDOW_S = 12.0   # окно манёвра после t0 (самый длинный манёвр демо — think, 11 с)
GESTURE_PATH = ("camera", "perception", "executor", "safety")
AGENT_PATH = ("rosbridge", "mcp", "agent")
MIB = 1024.0
NOT_ROBOT = ("twin_driver",)  # драйвер двойника — Gazebo и мосты, на роботе их нет: в сумму не входит


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

def det_rate(probe: Sequence[dict], rel0: float, rel1: float, t_mono0: float) -> Optional[int]:
    """Приращение det_calls за [rel0, rel1] секунд от якоря (сумма по процессам; рестарт процесса не даёт
    отрицательного прироста); None — строк journal в окне нет."""
    a, b = t_mono0 + rel0, t_mono0 + rel1
    rows = [r for r in probe if "det_calls" in r and a <= r["m"] <= b]
    if not rows:
        return None
    total, last = 0, {}
    for r in rows:
        pid = r.get("pid")
        if pid in last:
            total += max(0, r["det_calls"] - last[pid])
        last[pid] = r["det_calls"]
    return total


def deaf_gestures(segs: Sequence[dict], probe: Sequence[dict], t_mono0: float, fps: float) -> List[int]:
    """Сегменты-жесты, во время которых детектор отработал меньше DEAF_SHARE кадров (процесс умер, завис, поток
    камеры умер, кадров нет) — жест потерян из-за глухоты, а не из-за распознавания."""
    out = []
    for s in segs:
        if s["kind"] != "gesture":
            continue
        span = s["t_leave"] - s["t_start"]
        got = det_rate(probe, s["t_start"], s["t_leave"], t_mono0)
        if got is None or got < DEAF_SHARE * fps * span:
            out.append(s["seg"])
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


def fault_rows(run: dict, segs: Sequence[dict], per: Dict[int, dict], deaf: Sequence[int]) -> List[dict]:
    """Строка на отказ: TTR, вмешательства человека, видимость перезапуска по PLAN §1. Для отказов пути жеста
    (камера, perception, исполнитель, safety_gate) — потерянные жесты: глухие или без реакции от отказа до
    восстановления; для агентского пути (rosbridge, MCP, PicoClaw) — реплики пользователя без ответа."""
    t0 = run["anchor"]["t_mono"]
    faults = [e for e in run["events"] if e.get("kind") == "fault"]
    ops = [e for e in run["events"] if e.get("kind") == "operator"]
    end_rel = run["events"][-1]["m"] - t0 if run["events"] else 0.0
    out = []
    for i, f in enumerate(faults):
        start = f["m"] - t0
        nxt = faults[i + 1]["m"] - t0 if i + 1 < len(faults) else end_rel
        ttr = f.get("ttr_s")
        stop = start + ttr if ttr is not None else nxt
        row = {"fault": f["fault"], "at_s": round(start, 1), "ttr_s": ttr, "skipped": f.get("skipped")}
        if f.get("skipped"):
            out.append(row)
            continue
        role = fault_role(f["fault"])
        humans = [o for o in ops if start <= o["m"] - t0 <= min(stop, nxt) + 1.0]
        why = []
        if humans:
            why.append("понадобился человек")
        if ttr is None:
            why.append("не восстановилось само")
        if role in GESTURE_PATH:
            hit = [s for s in segs if s["kind"] == "gesture" and s["t_leave"] >= start and s["t_start"] <= stop]
            lost = [s["seg"] for s in hit
                    if s["seg"] in deaf or per.get(s["seg"], {}).get("reaction") in LOST_REACTIONS]
            later = [s for s in segs if s["kind"] == "gesture" and s["t_start"] > start]
            gap = later[0]["t_start"] - start if later else None
            if lost:
                why.append("потеряно жестов: %d" % len(lost))
            if ttr is not None and gap is not None and ttr > gap:
                why.append("недоступна дольше паузы до жеста (%.1f > %.1f с)" % (ttr, gap))
            row.update(lost_gestures=lost, gap_to_next_gesture_s=None if gap is None else round(gap, 1))
        else:
            asked = [a for a in run.get("agent", []) if start <= a["m"] - t0 <= min(stop, nxt)]
            failed = [a["n"] for a in asked if not a.get("ok")]
            if failed:
                why.append("реплик без ответа: %d" % len(failed))
            row.update(agent_failed=failed)
        row.update(path="жест" if role in GESTURE_PATH else "агент",
                   operator=[o["action"] + ":" + str(o.get("what") or "") for o in humans], visible=bool(why), why=why)
        out.append(row)
    return out


# ---- агент и LLM

def agent_summary(run: dict, llm: Sequence[dict]) -> dict:
    """Реплики пользователя: успешные (финальный ответ и все инструменты без ошибки), задержка; вызовы LLM по
    input_id оракула; вызовы LLM без реплики (на жест, пробуждения) — K4."""
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
    return {"requests": len(reqs), "ok": sum(1 for r in reqs if r.get("ok")), "failed": failed,
            "latency_s": quantiles([r["latency_s"] for r in reqs if r.get("ok")]),
            "llm_calls": len(mine), "llm_calls_per_request": quantiles(list(per_req.values())),
            "prompt_tokens_per_call": quantiles(prompt), "n_tools_in_catalog": sorted({r.get("n_tools") for r in mine})}


def llm_outside_requests(run: dict, llm: Sequence[dict], model: str) -> int:
    """Вызовы LLM этой системы (по имени модели в журнале) без реплики пользователя — на жест их быть не должно."""
    lo, hi = run["t_start"], run["t_end"]
    return sum(1 for r in llm if r.get("type") == "llm" and r.get("model") == model and lo <= r["ts_wall"] <= hi
               and not (r.get("oracle") or {}).get("input_ids"))


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


def drift(per: Sequence[dict], key: str = "cmd") -> dict:
    """Дрейф задержки t0 → /cmd_vel: p50 и p95 первой и последней трети жестов прогона."""
    vals = [p["lat"][key] for p in sorted(per, key=lambda p: p["seg"])
            if p.get("kind") == "gesture" and key in (p.get("lat") or {})]
    third = len(vals) // 3
    if third < 3:
        return {"n": len(vals)}
    return {"n": len(vals), "first": quantiles(vals[:third]), "last": quantiles(vals[-third:])}


# ---- сборка

MODELS = {"a0": "openai/gpt-4o-mini", "a1": "gpt-4o-mini"}


def summarize(run_dir: str, truth_path: Optional[str] = None) -> dict:
    run = load_json(os.path.join(run_dir, "run.json")) or {}
    if "anchor" not in run:
        return {"run_dir": run_dir, "error": run.get("error", "нет run.json или прогон не дошёл до якоря")}
    truth_path = truth_path or os.path.join(run_dir, "truth_schedule.jsonl")
    t_end_rel = run["events"][-1]["m"] - run["anchor"]["t_mono"]
    segs = [s for s in segments(truth_path) if s["t_leave"] <= t_end_rel]   # дошедшие до конца записи
    obs = load_json(os.path.join(run_dir, "summary.json")) or {}
    per = {p["seg"]: p for p in load_jsonl(os.path.join(run_dir, "gestures.jsonl")) if "seg" in p}
    probe = load_jsonl(os.path.join(run_dir, "probe.jsonl"))
    t_mono0 = run["anchor"]["t_mono"]
    fps = 10.0
    deaf = deaf_gestures(segs, probe, t_mono0, fps)
    llm = load_jsonl(os.path.join(run_dir, "mockllm.jsonl"))
    events = run["events"]
    ops = [e for e in events if e.get("kind") == "operator"]
    lat = obs.get("latency_s", {})
    gestures = [s for s in segs if s["kind"] == "gesture"]
    faults = fault_rows(run, segs, per, deaf)
    return {
        "system": run["system"], "label": run["label"], "profile": run["plan"]["profile"],
        "duration_s": round(run["t_end"] - run["t_start"], 1),
        "segments": {"gestures": len(gestures), "negatives": len(segs) - len(gestures)},
        "K1_t0_to_cmd_s": lat.get("cmd"), "K2_t0_to_motion_s": lat.get("motion"),
        "latency_excluded_rtf": lat.get("excluded_rtf"),
        "reactions": obs.get("reactions"), "late": obs.get("late"), "visible_failures_observer":
            obs.get("visible_failures"), "recognition": obs.get("recognition"),
        "K3": {"deaf_gestures": deaf, "deaf_gestures_n": len(deaf), "operator_interventions": len(ops),
               "operator": [{"at_s": round(o["m"] - t_mono0, 1), "action": o["action"], "what": o.get("what"),
                             "why": o.get("why") or o.get("reasons")} for o in ops],
               "visible_restarts": sum(1 for f in faults if f.get("visible")),
               "invisible_restarts": sum(1 for f in faults if f.get("visible") is False),
               "deaf_probe": (obs.get("deaf") or {}).get("probe"),
               "deaf_probe_standing": (obs.get("deaf") or {}).get("probe_standing"),
               "deaf_health": (obs.get("deaf") or {}).get("health")},
        "K4": {"llm_calls_outside_requests": llm_outside_requests(run, llm, MODELS[run["system"]]),
               "agent": agent_summary(run, llm)},
        "K5_faults": faults,
        "stopped_by_safety": safety_stops(per, segs, load_jsonl(os.path.join(run_dir, "safety.jsonl")), t_mono0),
        "K6_unsafe_no_fresh_cmd": obs.get("unsafe_no_fresh_cmd"),
        "K7": obs.get("k7"), "false_actions": obs.get("false_actions"),
        "touches": contacts(load_jsonl(os.path.join(run_dir, "truth.jsonl"))),
        "K8": {"memory": memory(load_jsonl(os.path.join(run_dir, "collect.jsonl"))), "drift_cmd": drift(list(per.values()))},
        "restarts": run.get("restarts"),
        "rtf_observer": obs.get("rtf"),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m neurobot.sim.runs.metrics", description=__doc__.split("\n\n")[0])
    p.add_argument("run_dir", help="каталог прогона (runner.py)")
    p.add_argument("--truth", default="", help="истина расписания (по умолчанию RUN_DIR/truth_schedule.jsonl)")
    p.add_argument("--out", default="", help="файл JSON (по умолчанию stdout)")
    a = p.parse_args(argv)
    doc = summarize(a.run_dir, a.truth or None)
    text = json.dumps(doc, ensure_ascii=False, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 1 if "error" in doc else 0


if __name__ == "__main__":
    sys.exit(main())
