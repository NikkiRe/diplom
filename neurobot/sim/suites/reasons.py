"""Разбор прогона A1 по журналу safety_state: какие причины держат blocked и slow и сколько это стоит (приёмка 17.10:
«защита перестраховывается»). Без ROS, Python ≥ 3.8.

  python3 neurobot/sim/suites/reasons.py [--window S] [--json ФАЙЛ] [--md ФАЙЛ] МЕТКА=КАТАЛОГ …

КАТАЛОГ — каталог прогона runner.py (vm/ артефактов): safety.jsonl (строка действует до следующей), truth.jsonl
(истина Gazebo), anchor.json (t = 0 таймлайна), а для разбора жестов — events.jsonl наблюдателя и truth_schedule.jsonl
(расписание). По каждому прогону, в окне [0, S] от якоря (без --window — весь прогон; перерывы записи дольше GAP_S —
пауза ВМ — не в счёт):
- доли времени состояний ok / slow / blocked / estop;
- по каждой причине (код до «:») — время, когда она стояла в blocked (вместе с estop) и в slow; «одна» — время, когда в
  blocked стояла только она; «цена» — жесты, манёвр которых оборван или не начат (реакция interrupted или none), пока
  в окне манёвра (REACTION_WINDOW_S после t0) safety_gate держал blocked или estop с этой причиной. Причины бывают
  одновременно: их суммы больше общего времени;
- пробег и время в движении по истине, касания (sim/runs/metrics.py), реакции на жесты — разбор наблюдателя заново
  нынешним neurobot/sim/gestures/analyze.py (один и тот же разбор для прогонов «до» и «после»), если есть events.jsonl,
  anchor.json и truth_schedule.jsonl; иначе — gestures.jsonl прогона как есть (поле reactions_source).
Прогон без строк safety.jsonl (A0: safety_gate нет) — только пробег, касания и реакции, поле safety — null.
Числа — стенд: ориентир, не результат робота.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import sys
from typing import Dict, List, Optional, Sequence

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from neurobot.sim.runs.metrics import (LOST_REACTIONS, REACTION_WINDOW_S, contacts, load_json,  # noqa: E402
                                       load_jsonl, travel)

GAP_S = 2.0        # перерыв записи дольше — пауза ВМ (сон Mac), не в счёт
STATES = ("ok", "slow", "blocked", "estop")
REACTIONS = ("correct", "wrong", "interrupted", "none", "unobserved")


def code(reason: str) -> str:
    return reason.split(":", 1)[0]


def spans(safety: Sequence[dict], end: float):
    """(строка, начало, конец) — строка safety.jsonl действует до следующей; перерывы дольше GAP_S — не в счёт."""
    for a, b in zip(safety, list(safety[1:]) + [None]):
        hi = end if b is None else b["m"]
        if 0.0 < hi - a["m"] <= GAP_S:
            yield a, a["m"], hi


def reason_table(safety: Sequence[dict], end: float) -> dict:
    """Доли состояний и время по причинам: {причина: {blocked, slow, sole}} (blocked — вместе с estop)."""
    states: Dict[str, float] = dict.fromkeys(STATES, 0.0)
    by: Dict[str, Dict[str, float]] = collections.defaultdict(lambda: dict.fromkeys(("blocked", "slow", "sole"), 0.0))
    for row, a, b in spans(safety, end):
        st = row.get("state")
        if st not in states:
            continue
        dt = b - a
        states[st] += dt
        codes = sorted({code(r) for r in row.get("reasons") or []})
        for c in codes:
            by[c]["slow" if st == "slow" else "blocked"] += dt
        if st in ("blocked", "estop") and len(codes) == 1:
            by[codes[0]]["sole"] += dt
    total = sum(states.values())
    rows = sorted(by.items(), key=lambda kv: -(kv[1]["blocked"] + kv[1]["slow"]))
    return {"total_s": round(total, 1), "share": {s: round(states[s] / total, 4) if total else None for s in STATES},
            "reasons": {c: {k: round(v, 1) for k, v in d.items()} for c, d in rows}}


def gestures(run_dir: str):
    """(разбор по сегментам, источник): заново нынешним наблюдателем, если есть его входы, иначе gestures.jsonl."""
    files = [os.path.join(run_dir, f) for f in ("events.jsonl", "truth_schedule.jsonl", "anchor.json")]
    if all(os.path.exists(f) for f in files):
        from neurobot.sim.gestures.analyze import run as analyze
        return analyze(*files)[0], "analyze.py (заново)"
    return load_jsonl(os.path.join(run_dir, "gestures.jsonl")), "gestures.jsonl прогона"


def lost_by_reason(per: Sequence[dict], safety: Sequence[dict], wall_to_mono: float) -> Dict[str, int]:
    """Жесты interrupted/none по причинам blocked/estop в окне их манёвра (одна причина — один жест не больше раза)."""
    out: Dict[str, int] = collections.Counter()
    for p in per:
        if p.get("reaction") not in LOST_REACTIONS:
            continue
        a = p["t0"] + wall_to_mono
        hit = {code(r) for s in safety
               if a <= s["m"] <= a + REACTION_WINDOW_S and s.get("state") in ("blocked", "estop")
               for r in s.get("reasons") or []}
        out.update(hit)
    return dict(out)


def summarize(run_dir: str, seconds: Optional[float] = None) -> dict:
    anchor = load_json(os.path.join(run_dir, "anchor.json"))
    if not anchor:
        raise ValueError("{}: нужен anchor.json".format(run_dir))
    m0 = anchor["t_mono"]
    hi = m0 + seconds if seconds is not None else math.inf
    safety = [r for r in load_jsonl(os.path.join(run_dir, "safety.jsonl")) if m0 <= r["m"] <= hi]
    truth = [r for r in load_jsonl(os.path.join(run_dir, "truth.jsonl")) if m0 <= r["m"] <= hi]
    if not safety and not truth:
        raise ValueError("{}: в окне нет строк safety.jsonl и truth.jsonl".format(run_dir))
    per, source = gestures(run_dir)
    per = [p for p in per if p.get("kind") == "gesture" and p.get("t0") is not None
           and p["t0"] <= anchor["t_wall"] + (seconds if seconds is not None else math.inf)]
    table = reason_table(safety, min(hi, safety[-1]["m"] + 1.0)) if safety else None
    for c, n in lost_by_reason(per, safety, m0 - anchor["t_wall"]).items():
        table["reasons"].setdefault(c, {"blocked": 0.0, "slow": 0.0, "sole": 0.0})["lost_gestures"] = n
    got = collections.Counter(p.get("reaction") or "none" for p in per)
    return {"window_s": seconds, "safety": table,
            "travel": {k: v for k, v in travel(truth, safety).items() if k != "safety_share"} if truth else {},
            "touches": {k: v for k, v in contacts(truth).items()
                        if k in ("episodes", "total_s", "models", "min_clearance_m")} if truth else {},
            "gestures": len(per), "reactions": {r: got.get(r, 0) for r in REACTIONS}, "reactions_source": source}


def _pct(x: Optional[float]) -> str:
    return "—" if x is None else "{:.0f} %".format(100 * x)


def markdown(results: Dict[str, dict]) -> str:
    """Итоги прогонов и причины: время в blocked / slow, с; в скобках — потерянные из-за неё жесты."""
    labels = list(results)
    out: List[str] = ["| | " + " | ".join(labels) + " |", "|---|" + "---|" * len(labels)]

    def row(name, fn):
        out.append("| {} | {} |".format(name, " | ".join(fn(results[k]) for k in labels)))
    row("окно, с", lambda d: "{:.0f}".format(d["safety"]["total_s"]) if d["safety"] else "—")
    row("ok / slow / blocked / estop", lambda d: " / ".join(_pct(d["safety"]["share"][s]) for s in STATES)
        if d["safety"] else "нет safety_gate")
    row("пробег по истине; в движении", lambda d: "{} м; {}".format(
        d["travel"].get("distance_m"), _pct(d["travel"].get("moving_share"))) if d["travel"] else "—")
    row("касания (эпизоды, истина Gazebo)", lambda d: "{} {}".format(
        d["touches"].get("episodes"), "({})".format(", ".join(sorted(d["touches"]["models"])))
        if d["touches"].get("models") else "").strip() if d["touches"] else "—")
    row("мин. зазор по истине, м", lambda d: str(d["touches"].get("min_clearance_m")) if d["touches"] else "—")
    row("верные реакции / жестов", lambda d: "{} / {}".format(d["reactions"]["correct"], d["gestures"]))
    row("оборваны / неверные / нет реакции", lambda d: "{} / {} / {}".format(
        d["reactions"]["interrupted"], d["reactions"]["wrong"], d["reactions"]["none"]))
    codes: List[str] = []
    for d in results.values():
        codes.extend(c for c in (d["safety"] or {}).get("reasons", ()) if c not in codes)
    out += ["", "Причины: время в blocked (с estop) / в slow, с; в скобках — жестов оборвано или не начато при ней", "",
            "| причина | " + " | ".join(labels) + " |", "|---|" + "---|" * len(labels)]
    for c in codes:
        cells = []
        for k in labels:
            r = (results[k]["safety"] or {}).get("reasons", {}).get(c)
            cells.append("—" if not r else "{:.0f} / {:.0f} ({})".format(r["blocked"], r["slow"],
                                                                           r.get("lost_gestures", 0)))
        out.append("| `{}` | {} |".format(c, " | ".join(cells)))
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--window", type=float, help="окно от якоря таймлайна, с (без него — весь прогон)")
    ap.add_argument("--json", help="итог JSON")
    ap.add_argument("--md", help="таблицы Markdown")
    ap.add_argument("runs", nargs="+", metavar="МЕТКА=КАТАЛОГ")
    a = ap.parse_args(argv)
    if a.window is not None and not a.window > 0:
        ap.error("--window: число секунд > 0")
    results = {}
    for item in a.runs:
        label, sep, path = item.partition("=")
        if not sep or not label or not os.path.isdir(path):
            ap.error("{!r}: МЕТКА=КАТАЛОГ прогона".format(item))
        try:
            results[label] = summarize(path, a.window)
        except (OSError, ValueError, KeyError) as e:
            print("reasons: {}: {}".format(label, e), file=sys.stderr)
            return 1
    text = json.dumps(results, ensure_ascii=False, indent=1)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    if a.md:
        with open(a.md, "w", encoding="utf-8") as f:
            f.write(markdown(results))
    if not a.json and not a.md:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
