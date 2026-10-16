"""Разбор прогона бюджета: PSS и CPU по компонентам, сумма против бюджета, лестница MemoryMax → report.json и
report.md.

  python3 -m neurobot.sim.budget.report --run DIR [--main main] [--cpucal cpucal_host.json]
        [--check cpucal_check.json] [--out DIR]

DIR — каталог прогона stack.py: шаги DIR/<метка>/ со step.json, units.json, collect.jsonl, health.jsonl,
sysmon.jsonl. Окно замера шага — от t_measure_start до t_measure_end; пики — за весь шаг, вместе с подъёмом.
Исход шага:
  OOM          — выросло memory.events oom_kill slice (жертвы — по журналу ядра и PID из collect);
  деградация   — без OOM, но хоть одно: компонент не готов, перезапуски, кадры/с perception < 80 % опорных,
                 perception молчит (нет /neurobot/perception_health) > 20 % окон, safety_state < 4 Гц (норма 5),
                 давление памяти PSI some > 5 % времени, мажорные отказы > 50/с, двойник погас;
  норма        — иначе.
Опорные кадры/с — медиана шага --main (если он есть), иначе темп детекции 10 к/с.
Бюджет сравнивается с уникальными страницами slice (uniq.json: общие страницы — один раз на группу, как на плате),
а без них — с суммой PSS (занижена: PSS делит библиотеки и с процессами вне slice).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from typing import Dict, Iterable, List, Optional, Sequence

from .model import budgets

HERE = os.path.dirname(os.path.abspath(__file__))
MIB_KIB = 1024
SAFETY_HZ_MIN, FPS_FRAC, PSI_PCT_MAX, MAJFLT_MAX, SILENT_MAX = 4.0, 0.8, 5.0, 50.0, 0.2
KILLED = re.compile(r"Killed process (\d+) \(([^)]*)\)")


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(path: str) -> List[dict]:
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue                  # недописанная последняя строка
    return out


def counter_delta(values: Sequence[float]) -> float:
    """Прирост накопленного счётчика; падение — перезапуск (группа или процесс созданы заново)."""
    total = 0.0
    for a, b in zip(values, values[1:]):
        total += b - a if b >= a else b
    return total


def med(xs: Iterable[float]) -> Optional[float]:
    xs = list(xs)
    return statistics.median(xs) if xs else None


def r1(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 1)


def unit_pss_kib(u: Optional[dict]) -> int:
    return sum(p.get("pss_kib", 0) for p in (u or {}).get("procs", []))


def unit_rss_kib(u: Optional[dict]) -> int:
    return sum(p.get("rss_kib", 0) for p in (u or {}).get("procs", []))


def step_summary(step_dir: str, ratio: float, cuda_mib: float) -> dict:
    step = read_json(os.path.join(step_dir, "step.json"))
    units = read_json(os.path.join(step_dir, "units.json"))["units"]
    samples = [s for s in load_jsonl(os.path.join(step_dir, "collect.jsonl")) if s.get("type") == "sample"]
    t0, t1 = step["t_measure_start"], step["t_measure_end"]
    win = [s for s in samples if t0 <= s["t_wall"] <= t1]
    span = (win[-1]["t_wall"] - win[0]["t_wall"]) if len(win) > 1 else 0.0
    comps = {}
    for name, meta in units.items():
        pss = [unit_pss_kib(s["units"].get(name)) for s in win if s["units"].get(name)]
        rss = [unit_rss_kib(s["units"].get(name)) for s in win if s["units"].get(name)]
        peak = max((unit_pss_kib(s["units"].get(name)) for s in samples if s["units"].get(name)), default=0)
        usage = [((s["units"].get(name) or {}).get("cg") or {}).get("cpu", {}).get("usage_usec") for s in win]
        usage = [u for u in usage if u is not None]
        maj = [sum(p.get("majflt", 0) for p in (s["units"].get(name) or {}).get("procs", [])) for s in win]
        cpu = counter_delta(usage) / span / 1e4 if span and len(usage) > 1 else None
        last = (win[-1]["units"].get(name) or {}).get("procs", []) if win else []
        comps[name] = {
            "role": meta["role"], "moved": bool(meta.get("moved")),
            "pss_mib": r1(med(pss) / MIB_KIB) if pss else None, "pss_peak_mib": r1(peak / MIB_KIB),
            "rss_mib": r1(med(rss) / MIB_KIB) if rss else None,
            "cpu_host_pct": r1(cpu), "cpu_a57_pct": r1(cpu / ratio) if cpu is not None else None,
            "majflt_per_s": round(counter_delta(maj) / span, 2) if span and len(maj) > 1 else None,
            "procs": [{"comm": p["comm"], "pss_mib": r1(p.get("pss_kib", 0) / MIB_KIB),
                       "anon_mib": r1(p.get("pss_anon_kib", 0) / MIB_KIB),
                       "file_mib": r1(p.get("pss_file_kib", 0) / MIB_KIB), "threads": p.get("threads")}
                      for p in last],
        }
    if "perception" in comps and comps["perception"]["pss_mib"] is not None:
        comps["perception"]["code_pss_mib"] = r1(comps["perception"]["pss_mib"] - cuda_mib)
    totals = [sum(unit_pss_kib(s["units"].get(n)) for n in units) for s in win]
    ballast = []
    for s in win:
        kib = sum(unit_pss_kib(s["units"].get(n)) for n, m in units.items() if m["role"] == "ballast")
        if (s["units"].get("perception") or {}).get("procs"):
            kib += cuda_mib * MIB_KIB             # балласт cuda живёт в процессе perception
        ballast.append(kib)
    sl = [s["slice"] for s in samples if s.get("slice")]
    slw = [s["slice"] for s in win if s.get("slice")]
    out = {
        "label": step["label"], "mem_mib": step["mem_mib"], "quota_pct": step["quota_pct"],
        "window_s": round(span, 1), "samples": len(win), "ready_s": step["ready_s"],
        "components": comps,
        "total_pss_mib": r1(med(totals) / MIB_KIB) if totals else None,
        "total_pss_peak_mib": r1(max((sum(unit_pss_kib(s["units"].get(n)) for n in units) for s in samples),
                                     default=0) / MIB_KIB),
        "ballast_pss_mib": r1(med(ballast) / MIB_KIB) if ballast else None,
        "slice_current_mib": r1(med(x["memory_current"] for x in slw) / MIB_KIB / MIB_KIB) if slw else None,
        # memory.peak slice копится за всю жизнь группы (slice живёт через шаги) — пик шага берём по сэмплам
        "slice_peak_mib": r1(max((x["memory_current"] for x in sl), default=0) / MIB_KIB / MIB_KIB),
        "slice_stat_mib": {k: r1(med(x.get("stat", {}).get(k, 0) for x in slw) / MIB_KIB / MIB_KIB) if slw else None
                           for k in ("anon", "file", "kernel")},
        "oom_kills": int(counter_delta([x["events"].get("oom_kill", 0) for x in sl])) if sl else 0,
        "mem_max_events": int(counter_delta([x["events"].get("max", 0) for x in sl])) if sl else 0,
    }
    if len(slw) > 1 and span:
        psi = [x.get("psi_mem", {}).get("some", {}).get("total", 0) for x in slw]
        out["psi_mem_some_pct"] = round(counter_delta(psi) / span / 1e4, 2)
        cpu = [x["cpu"].get("usage_usec", 0) for x in slw]
        out["slice_cpu_host_pct"] = r1(counter_delta(cpu) / span / 1e4)
        out["slice_cpu_a57_cores"] = round(counter_delta(cpu) / span / 1e6 / ratio, 2)
        per = counter_delta([x["cpu"].get("nr_periods", 0) for x in slw])
        thr = counter_delta([x["cpu"].get("nr_throttled", 0) for x in slw])
        out["throttled_frac"] = round(thr / per, 3) if per else None
        out["throttled_s_per_min"] = round(counter_delta([x["cpu"].get("throttled_usec", 0) for x in slw])
                                           / 1e6 / span * 60, 2)
    loads = [s["sys"].get("load1") for s in win if s.get("sys", {}).get("load1") is not None]
    ticks = [s["sys"]["cpu_ticks"] for s in win if s.get("sys", {}).get("cpu_ticks")]
    out["vm_load1_median"] = r1(med(loads))
    if len(ticks) > 1:
        busy = counter_delta([t["total"] - t["idle"] for t in ticks])
        out["vm_busy_frac"] = round(busy / max(1, counter_delta([t["total"] for t in ticks])), 3)
    out["health"] = health_summary(load_jsonl(os.path.join(step_dir, "health.jsonl")), t0, t1)
    out["sysmon_tree_pss_mib"] = sysmon_pss(load_jsonl(os.path.join(step_dir, "sysmon.jsonl")), t0, t1)
    uniq_path = os.path.join(step_dir, "uniq.json")
    if os.path.exists(uniq_path):
        u = read_json(uniq_path)
        if u.get("slice_unique_kib") is not None:
            out["unique_mib"] = r1(u["slice_unique_kib"] / MIB_KIB)
            out["unique_pss_mib"] = r1(u["slice_pss_kib"] / MIB_KIB)
            out["unique_rss_mib"] = r1(u["slice_rss_kib"] / MIB_KIB)
            for name, kib in u["units_kib"].items():
                if name in comps and kib is not None:
                    comps[name]["unique_mib"] = r1(kib / MIB_KIB)
    status = step.get("status_end", {})
    out["restarts"] = {u: int(s.get("NRestarts") or 0) for u, s in status.items() if int(s.get("NRestarts") or 0)}
    out["not_active_end"] = sorted(u for u, s in status.items() if s.get("ActiveState") != "active")
    out["twin_alive_end"] = step.get("twin_alive_end")
    out["oom_victims"] = oom_victims(step.get("kernel_oom", []), samples, units)
    return out


def health_summary(rows: List[dict], t0: float, t1: float) -> dict:
    w = [r for r in rows if t0 <= r["t_wall"] <= t1]
    dt = sum(r.get("dt", 0) for r in w)

    def n(key: str) -> int:
        return sum(r.get("n", {}).get(key, 0) for r in w)
    alive = [r for r in w if r.get("n", {}).get("health")]   # окна, где perception публиковал здоровье
    states: Dict[str, int] = {}
    for r in alive:
        if r.get("perception"):
            states[r["perception"]] = states.get(r["perception"], 0) + 1
    safety: Dict[str, int] = {}
    for r in w:
        for k, v in r.get("safety", {}).items():
            safety[k] = safety.get(k, 0) + v
    rtf = [r["rtf"] for r in w if r.get("rtf") is not None and r.get("n", {}).get("truth")]
    return {
        "window_s": round(dt, 1),
        "fps_median": med(r["fps"] for r in alive if r.get("fps") is not None),
        "perception_silent_frac": round(1 - len(alive) / len(w), 3) if w else None,
        "perception_states": states,
        "safety_hz": round(n("safety") / dt, 2) if dt else None,
        "safety_states": safety,
        "cmd_req_hz": round(n("cmd_req") / dt, 2) if dt else None,
        "gestures": n("gesture"),
        "cmd_vel_nonzero": sum(r.get("cmd_vel_nonzero", 0) for r in w),
        "rtf_median": med(rtf), "rtf_min": min(rtf) if rtf else None,
    }


def sysmon_pss(rows: List[dict], t0: float, t1: float) -> Optional[float]:
    vals = [r["tree"]["pss_kib"] for r in rows if r.get("type") == "sample" and t0 <= r.get("ts_wall", 0) <= t1
            and "pss_kib" in r.get("tree", {})]
    return r1(med(vals) / MIB_KIB) if vals else None


def oom_victims(lines: List[str], samples: List[dict], units: Dict[str, dict]) -> List[dict]:
    """Жертвы OOM по журналу ядра, сгруппированные по юниту и имени процесса. Юнит — по PID из collect, а для
    процесса, убитого между сэмплами (OOM-петля), — по имени, если оно встречалось только у одного юнита."""
    owner: Dict[int, str] = {}
    comm_units: Dict[str, set] = {}
    for s in samples:
        for name in units:
            for p in (s["units"].get(name) or {}).get("procs", []):
                owner[p["pid"]] = name
                comm_units.setdefault(p["comm"], set()).add(name)
    counts: Dict[tuple, int] = {}
    for line in lines:
        m = KILLED.search(line)
        if m:
            pid, comm = int(m.group(1)), m.group(2)
            by_comm = comm_units.get(comm, set())
            unit = owner.get(pid) or (next(iter(by_comm)) if len(by_comm) == 1 else None)
            counts[(unit, comm)] = counts.get((unit, comm), 0) + 1
    return [{"unit": u, "comm": c, "kills": n} for (u, c), n in sorted(counts.items(), key=lambda kv: -kv[1])]


def classify(s: dict, ref_fps: float) -> dict:
    reasons = []
    not_ready = sorted(k for k, v in (s.get("ready_s") or {}).items() if v is None)
    if not_ready:
        reasons.append("не готов: " + ", ".join(not_ready))
    if s["restarts"]:
        reasons.append("перезапуски: " + ", ".join("%s ×%d" % kv for kv in sorted(s["restarts"].items())))
    h = s["health"]
    if h["fps_median"] is None or h["fps_median"] < FPS_FRAC * ref_fps:
        reasons.append("кадры/с perception %s при опорных %s" % (h["fps_median"], ref_fps))
    if (h.get("perception_silent_frac") or 0) > SILENT_MAX:
        reasons.append("perception молчит %d %% времени" % round(100 * h["perception_silent_frac"]))
    if h["safety_hz"] is None or h["safety_hz"] < SAFETY_HZ_MIN:
        reasons.append("safety_state %s Гц" % h["safety_hz"])
    if (s.get("psi_mem_some_pct") or 0) > PSI_PCT_MAX:
        reasons.append("давление памяти %s %% времени" % s["psi_mem_some_pct"])
    worst = max((c["majflt_per_s"] or 0 for c in s["components"].values()), default=0)
    if worst > MAJFLT_MAX:
        reasons.append("мажорные отказы страниц до %s/с" % worst)
    if s.get("twin_alive_end") is False:
        reasons.append("двойник погас")
    if s["oom_kills"]:
        victims = ", ".join("%s ×%d (%s)" % (v["unit"] or "?", v["kills"], v["comm"])
                            for v in s["oom_victims"]) or "см. журнал ядра"
        return {"outcome": "OOM", "first": victims, "reasons": reasons}
    return {"outcome": "деградация" if reasons else "норма", "first": reasons[0] if reasons else "", "reasons": reasons}


def budget_table(model: dict, total_mib: float) -> Dict[str, Dict[str, dict]]:
    return {b: {c: {"budget_mib": v, "headroom_mib": r1(v - total_mib)} for c, v in cs.items()}
            for b, cs in budgets(model).items()}


def md_report(rep: dict) -> str:
    L = ["# Бюджет Jetson Nano 4 ГБ на стенде — отчёт", "",
         "Все числа — **замер на стенде (ВМ sim, Jazzy, torch CPU), ориентир, не результат**. Итог — только на плате.",
         ""]
    cal = rep.get("cpucal") or {}
    if cal:
        L += ["CPU: 7-zip одного ядра ВМ %s MIPS против %s у A57 → отношение %s, квота %s %% на %s ядрах [замер + ист.]"
              % (cal.get("host_single_median"), cal.get("nano_single_mips"), cal.get("ratio"), cal.get("quota_pct"),
                 cal.get("cores")), ""]
    chk = rep.get("cpucheck") or {}
    if chk:
        L += ["Проверка квоты (7-zip в slice): один поток %s MIPS (×%s к A57), %s потока %s MIPS (×%s к Nano) — "
              "квота держит суммарное время CPU, а не скорость одного ядра." % (
                  chk["slice_mmt1"]["rating_mips"], chk["single_vs_nano"], chk["cpus"],
                  chk["slice_mmt%d" % chk["cpus"]]["rating_mips"], chk["multi_vs_nano"]), ""]
    main = rep.get("main")
    if main:
        L += ["## Шаг «%s»: MemoryMax %d МиБ, квота %d %%, окно %s с" % (main["label"], main["mem_mib"],
                                                                      main["quota_pct"], main["window_s"]), "",
              "| компонент | роль | PSS, МиБ (медиана) | пик PSS | уникальные | RSS | CPU, % ядра ВМ | ≈ % ядра A57 "
              "| мажорн. отказы/с |", "|---|---|---|---|---|---|---|---|---|"]
        for name, c in sorted(main["components"].items(), key=lambda kv: -(kv[1]["pss_mib"] or 0)):
            role = c["role"] + (", перенесён" if c["moved"] else "")
            pss = c["pss_mib"]
            if name == "perception" and c.get("code_pss_mib") is not None:
                pss = "%s (код %s + cuda %s)" % (c["pss_mib"], c["code_pss_mib"], rep["cuda_mib"])
            L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                name, role, pss, c["pss_peak_mib"], c.get("unique_mib", "—"), c["rss_mib"], c["cpu_host_pct"],
                c["cpu_a57_pct"], c["majflt_per_s"]))
        L += ["", "Сумма PSS (медиана сумм): **%s МиБ**, из них балласт %s МиБ; пик суммы %s МиБ; slice: "
              "memory.current %s МиБ (anon %s, file %s, kernel %s), максимум по сэмплам %s МиБ; "
              "sysmon (сверка) %s МиБ." % (
                  main["total_pss_mib"], main["ballast_pss_mib"], main["total_pss_peak_mib"],
                  main["slice_current_mib"], main["slice_stat_mib"]["anon"], main["slice_stat_mib"]["file"],
                  main["slice_stat_mib"]["kernel"], main["slice_peak_mib"], main["sysmon_tree_pss_mib"]), ""]
        if main.get("unique_mib") is not None:
            L += ["Уникальные страницы slice в середине окна (общие — один раз на группу, как на плате): **%s МиБ**; "
                  "в тот же момент сумма PSS %s МиБ, сумма RSS %s МиБ." % (
                      main["unique_mib"], main["unique_pss_mib"], main["unique_rss_mib"]), ""]
        L += ["Бюджет сравнивается с %s." % ("уникальными страницами slice" if rep.get("budget_basis") == "unique"
                                            else "суммой PSS (уникальных страниц нет)"), "",
              "| ветка ОС | бюджет value | запас | бюджет pessimistic | запас |", "|---|---|---|---|---|"]
        for b, cs in rep["budget"].items():
            L.append("| %s | %s | %s | %s | %s |" % (b, cs["value"]["budget_mib"], cs["value"]["headroom_mib"],
                                                    cs["pessimistic"]["budget_mib"], cs["pessimistic"]["headroom_mib"]))
        h = main["health"]
        L += ["", "CPU slice: %s %% ядра ВМ (≈ %s ядра A57) при квоте %s %%; доля периодов с троттлингом %s; "
              "ВМ: load1 %s, занятость %s." % (main.get("slice_cpu_host_pct"), main.get("slice_cpu_a57_cores"),
                                              main["quota_pct"], main.get("throttled_frac"),
                                              main.get("vm_load1_median"), main.get("vm_busy_frac")),
              "Здоровье: perception %s к/с (%s), safety_state %s Гц (%s), жестов %d, ненулевых /cmd_vel %d, "
              "RTF %s (мин. %s). Исход: %s." % (
                  h["fps_median"], h["perception_states"], h["safety_hz"], h["safety_states"], h["gestures"],
                  h["cmd_vel_nonzero"], h["rtf_median"], h["rtf_min"], main["verdict"]["outcome"]), ""]
    if rep.get("steps"):
        L += ["## Шаги", "", "| шаг | MemoryMax | квота | исход | первое | сумма PSS | уникальные "
              "| max memory.current | PSI some, % | CPU slice, ядер A57 | троттлинг | кадры/с | safety, Гц | OOM "
              "| перезапуски |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for s in rep["steps"]:
            v = s["verdict"]
            L.append("| %s | %d | %d | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %d | %s |" % (
                s["label"], s["mem_mib"], s["quota_pct"], v["outcome"], v["first"], s["total_pss_mib"],
                s.get("unique_mib", "—"), s["slice_peak_mib"], s.get("psi_mem_some_pct"),
                s.get("slice_cpu_a57_cores"), s.get("throttled_frac"), s["health"]["fps_median"],
                s["health"]["safety_hz"], s["oom_kills"],
                ", ".join("%s ×%d" % kv for kv in sorted(s["restarts"].items())) or "—"))
        if rep.get("first_break"):
            L += ["", "Первое, что ломается при снижении MemoryMax: **%s**." % rep["first_break"]]
    return "\n".join(L) + "\n"


def build(run: str, main_label: str, model: dict, cpucal: Optional[dict], check: Optional[dict]) -> dict:
    ratio = (cpucal or {}).get("ratio")
    labels = sorted(d for d in os.listdir(run) if os.path.exists(os.path.join(run, d, "step.json")))
    cuda = model["ballast"]["cuda"]["mib"]
    steps = []
    for d in labels:
        st = read_json(os.path.join(run, d, "step.json"))
        steps.append(step_summary(os.path.join(run, d), ratio or st["ratio"], cuda))
    main = next((s for s in steps if s["label"] == main_label), None)
    ref = (main or {}).get("health", {}).get("fps_median") or 10.0
    for s in steps:
        s["verdict"] = classify(s, ref)
    rep = {"run": os.path.abspath(run), "cuda_mib": cuda, "cpucal": cpucal, "cpucheck": check, "main": main,
           "steps": sorted((s for s in steps if s is not main), key=lambda s: (-s["quota_pct"], -s["mem_mib"]))}
    if main and main["total_pss_mib"] is not None:
        rep["budget_basis"] = "unique" if main.get("unique_mib") is not None else "pss"
        rep["budget"] = budget_table(model, main.get("unique_mib") or main["total_pss_mib"])
    ladder = [s for s in rep["steps"] if s["quota_pct"] == (main or {}).get("quota_pct", s["quota_pct"])]
    broken = [s for s in sorted(ladder, key=lambda s: -s["mem_mib"]) if s["verdict"]["outcome"] != "норма"]
    if broken:
        b = broken[0]
        rep["first_break"] = "%d МиБ — %s: %s" % (b["mem_mib"], b["verdict"]["outcome"], b["verdict"]["first"])
    return rep


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--run", required=True, help="каталог прогона (шаги с step.json)")
    p.add_argument("--main", default="main", help="метка основного шага")
    p.add_argument("--cpucal", help="cpucal host JSON")
    p.add_argument("--check", help="cpucal check JSON")
    p.add_argument("--model", default=os.path.join(HERE, "model.json"))
    p.add_argument("--out", help="куда писать report.json и report.md (по умолчанию --run)")
    a = p.parse_args(argv)
    rep = build(a.run, a.main, read_json(a.model), read_json(a.cpucal) if a.cpucal else None,
                read_json(a.check) if a.check else None)
    if not rep["main"] and not rep["steps"]:
        print("report: в %s нет шагов со step.json" % a.run, file=sys.stderr)
        return 2
    out = a.out or a.run
    with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write(md_report(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
