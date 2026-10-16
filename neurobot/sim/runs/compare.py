"""Сводка compare.md (README §9): таблица A0/A1 по метрикам PLAN §1 и итог по критериям приёмки Э2 и §13.4.

  python3 -m neurobot.sim.runs.compare --a0 DIR --a1 DIR [--start DIR] [--budget DIR] [--long DIR]
       [--shapeclass DIR] --out compare.md

Каталоги — результаты прогонов (runner.py, budget_a1.py, start_test пакета, shapeclass); метрики считаются
metrics.py, если рядом нет metrics.json. Все числа — стенд в общей ВМ: ориентир, не результат робота.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from neurobot.sim.runs import metrics as M


def load_metrics(run_dir: Optional[str]) -> Optional[dict]:
    if not run_dir:
        return None
    path = os.path.join(run_dir, "metrics.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return M.summarize(run_dir)


def num(v, nd: int = 2) -> str:
    """Число по-русски: десятичная запятая."""
    if v is None:
        return "—"
    if isinstance(v, float):
        return (("%." + str(nd) + "f") % v).replace(".", ",")
    return str(v)


def lat(d: Optional[dict]) -> str:
    if not d or not d.get("n"):
        return "нет"
    return "%s / %s (n=%d)" % (num(d.get("p50")), num(d.get("p95")), d["n"])


def reactions(m: dict) -> str:
    r = m.get("reactions") or {}
    return ", ".join("%s %d" % (k, r[k]) for k in ("correct", "wrong", "none", "interrupted", "unobserved") if k in r)


def k6(m: dict) -> str:
    d = m.get("K6_unsafe_no_fresh_cmd") or {}
    silent, ign = d.get("silent") or {}, d.get("ignored_zero") or {}
    return "%d эпизодов, %s с (молчание писателя %d, ноль не исполнен %d)" % (
        d.get("n", 0), num(d.get("total_s")), silent.get("n", 0), ign.get("n", 0))


def touches(m: dict) -> str:
    t = m.get("touches") or {}
    models = ", ".join("%s" % k for k in sorted(t.get("models") or {}))
    return "%d (%s с; %s), мин. зазор %s м" % (t.get("episodes", 0), num(t.get("total_s"), 1), models or "—",
                                              num(t.get("min_clearance_m"), 3))


def memory(m: dict) -> str:
    mem = ((m.get("K8") or {}).get("memory") or {}).get("_total") or {}
    if not mem:
        return "—"
    return "%s → %s МиБ, наклон %s МиБ/ч (%s ч)" % (num(mem.get("pss_mib_start"), 0), num(mem.get("pss_mib_end"), 0),
                                                  num(mem.get("slope_mib_h"), 1), num(mem.get("hours"), 2))


def agent(m: dict) -> str:
    a = (m.get("K4") or {}).get("agent") or {}
    calls = (a.get("llm_calls_per_request") or {}).get("p50")
    tok = (a.get("prompt_tokens_per_call") or {}).get("p50")
    return "%d из %d; LLM на реплику %s, токенов на вызов %s, инструментов %s" % (
        a.get("ok", 0), a.get("requests", 0), num(calls, 0), num(tok, 0),
        "/".join(str(x) for x in a.get("n_tools_in_catalog") or []) or "—")


def row(name: str, a0: str, a1: str, goal: str = "") -> str:
    return "| %s | %s | %s | %s |" % (name, a0, a1, goal)


def fault_table(a0: dict, a1: dict) -> List[str]:
    out = ["| Отказ | путь | A0: TTR, с | A0: видим? | A1: TTR, с | A1: видим? |", "|---|---|---|---|---|---|"]
    rows1 = {f["fault"] + "#%d" % i: f for i, f in enumerate(a1.get("K5_faults") or [])}
    for i, f0 in enumerate(a0.get("K5_faults") or []):
        f1 = rows1.get(f0["fault"] + "#%d" % i, {})

        def cell(f):
            if f.get("skipped"):
                return "нет компонента", "—"
            return num(f.get("ttr_s"), 1) if f.get("ttr_s") is not None else "не поднялось", (
                "да: " + "; ".join(f.get("why") or []) if f.get("visible") else "нет")
        t0, v0 = cell(f0)
        t1, v1 = cell(f1)
        out.append("| %s (%s с) | %s | %s | %s | %s | %s |" % (f0["fault"], num(f0["at_s"], 0), f0.get("path", "—"),
                                                             t0, v0, t1, v1))
    return out


def agent_errors_after(m: dict, fault: str) -> Optional[int]:
    """Реплики без ответа в окне отказа (от отказа до восстановления или следующего отказа)."""
    rows = [f for f in m.get("K5_faults") or [] if f["fault"] == fault and not f.get("skipped")]
    return sum(len(f.get("agent_failed") or []) for f in rows) if rows else None


def start_counts(start: Optional[dict]) -> tuple:
    """(прошло, всего) холодных стартов и сломанных сценариев start_test."""
    if not start:
        return None, None
    ok = start.get("ok") or {}
    broken = {k: v for k, v in start.items() if k != "ok" and isinstance(v, dict) and "cycles" in v}
    return (ok.get("passed"), ok.get("cycles")), (sum(v.get("passed", 0) for v in broken.values()),
                                                 sum(v.get("cycles", 0) for v in broken.values()))


def verdicts(a0: dict, a1: dict, start: Optional[dict], budget: Optional[dict]) -> List[str]:
    """Критерии PLAN (Э2, §1, §13.4): значение A0, A1 и вердикт по A1. Время — ориентир стенда."""
    k1 = lambda m: (m.get("K1_t0_to_cmd_s") or {}).get("p50")  # noqa: E731
    deaf = lambda m: (m.get("K3") or {}).get("deaf_gestures_n")  # noqa: E731
    vis = lambda m: (m.get("K3") or {}).get("visible_restarts")  # noqa: E731
    k6n = lambda m: (m.get("K6_unsafe_no_fresh_cmd") or {}).get("n")  # noqa: E731
    unk = lambda m: (m.get("K7") or {}).get("moved_on_unknown")  # noqa: E731
    tch = lambda m: (m.get("touches") or {}).get("episodes")  # noqa: E731
    llm = lambda m: (m.get("K4") or {}).get("llm_calls_outside_requests")  # noqa: E731
    hum = lambda m: (m.get("K3") or {}).get("operator_interventions")  # noqa: E731
    cold, broken = start_counts(start)

    def stuck(m: dict) -> Optional[int]:
        """Отказы пути жеста, после которых система сама не поднялась или понадобился человек («глухие
        состояния»)."""
        rows = [f for f in m.get("K5_faults") or [] if f.get("path") == "жест" and not f.get("skipped")]
        return sum(1 for f in rows if f.get("ttr_s") is None or f.get("operator")) if rows else None

    def mark(value, ok) -> str:
        return "нет данных" if value is None else ("выполнено" if ok(value) else "**не выполнено**")
    rows = [
        ("0 «глухих» состояний: путь жеста сам поднялся после каждого отказа", stuck(a0), stuck(a1),
         mark(stuck(a1), lambda v: v == 0)),
        ("0 потерянных жестов из-за глухоты (детектор не работал)", deaf(a0), deaf(a1),
         mark(deaf(a1), lambda v: v == 0)),
        ("0 вмешательств человека", hum(a0), hum(a1), mark(hum(a1), lambda v: v == 0)),
        ("0 видимых перезапусков (PLAN §1)", vis(a0), vis(a1), mark(vis(a1), lambda v: v == 0)),
        ("0 движений по «не распознано» (K7)", unk(a0), unk(a1), mark(unk(a1), lambda v: v == 0)),
        ("0 эпизодов «едет без свежей команды > 0,5 с» (K6)", k6n(a0), k6n(a1), mark(k6n(a1), lambda v: v == 0)),
        ("p50 t0 → /cmd_vel ≤ 0,7 с при 10 к/с (Э2) [ориентир]", num(k1(a0)), num(k1(a1)),
         mark(k1(a1), lambda v: v <= 0.7)),
        ("0 вызовов LLM на жест (K4)", llm(a0), llm(a1), mark(llm(a1), lambda v: v == 0)),
        ("PicoClaw по HTTP после kill MCP-сервера — ≤ 1 ошибка", agent_errors_after(a0, "kill_mcp"),
         agent_errors_after(a1, "kill_mcp"), mark(agent_errors_after(a1, "kill_mcp"), lambda v: v <= 1)),
        ("0 касаний (безопасность v2, истина Gazebo)", tch(a0), tch(a1), mark(tch(a1), lambda v: v == 0)),
        ("start_test: 100 из 100 холодных стартов (K9)", "нет механизма", cold and "%s из %s" % cold,
         mark(cold, lambda v: v[0] == v[1] == 100)),
        ("doctor называет сломанный компонент во всех сценариях", "нет doctor", broken and "%s из %s" % broken,
         mark(broken, lambda v: v[1] > 0 and v[0] == v[1])),
        ("бюджет 4 ГБ: 0 OOM за прогон", "—", budget and budget.get("oom_kill"),
         mark(budget and budget.get("oom_kill"), lambda v: v == 0)),
    ]
    out = ["| Критерий | A0 | A1 | Итог A1 |", "|---|---|---|---|"]
    return out + ["| %s | %s | %s | %s |" % (c, "—" if a is None else a, "—" if b is None else b, v)
                  for c, a, b, v in rows]


def p4(p) -> str:
    return " ".join(num(float(x), 2) for x in p)


def shape_table(groups: dict) -> List[str]:
    out = ["| Форма | замысел (класс) | demo: p(0…3) | demo: «немые» | полилиния: p(0…3) |", "|---|---|---|---|---|"]
    for name, g in sorted(groups.items()):
        if "demo" not in g:
            continue
        out.append("| %s | %s | %s | %s | %s |" % (name, g.get("cls"), p4(g["demo"]["p"]), num(g["demo"].get("mute")),
                                                 p4(g["poly"]["p"])))
    return out


def start_table(start: dict) -> List[str]:
    out = ["| Сценарий | прошло / всего | время до «ОК» doctor p50 / p95, с | не прошли |", "|---|---|---|---|"]
    for name, v in sorted(start.items()):
        if not isinstance(v, dict) or "cycles" not in v:
            continue
        t = v.get("t_doctor_s") or {}
        out.append("| %s | %s / %s | %s / %s | %s |" % (name, v.get("passed"), v.get("cycles"), num(t.get("p50"), 1),
                                                       num(t.get("p95"), 1), ", ".join(map(str, v.get("failed") or []))
                                                       or "—"))
    return out


def budget_table(b: dict) -> List[str]:
    out = ["MemoryMax %s МиБ, квота %s %% на %s ядра, отношение A57/ВМ %s; OOM за прогон: %s." % (
        b.get("mem_max_mib"), b.get("quota_pct"), b.get("cpus"), num(b.get("ratio_a57"), 3), b.get("oom_kill")), "",
        "| Компонент | уникальные страницы, МиБ |", "|---|---|"]
    out += ["| %s | %s |" % (k, num(v, 1)) for k, v in sorted((b.get("unique_mib") or {}).items())]
    out += ["| **slice** | **%s** (PSS %s, RSS %s) |" % (num(b.get("slice_unique_mib"), 1), num(b.get("slice_pss_mib"), 1),
                                                          num(b.get("slice_rss_mib"), 1)),
            "", "| Ветка ОС | запас, МиБ (value / pessimistic / optimistic) |", "|---|---|"]
    out += ["| %s | %s / %s / %s |" % (br, num(c.get("value"), 0), num(c.get("pessimistic"), 0),
                                      num(c.get("optimistic"), 0)) for br, c in (b.get("margin_mib") or {}).items()]
    return out


def long_table(m: dict) -> List[str]:
    mem = (m.get("K8") or {}).get("memory") or {}
    d = (m.get("K8") or {}).get("drift_cmd") or {}
    k3 = m.get("K3") or {}
    out = ["Длительность %s с; K1 p50 / p95 %s; дрейф K1: первая треть %s, последняя %s; касания %s; перезапуски "
           "видимые / невидимые %s / %s; вмешательств человека %s; рестарты юнитов: %s." % (
               num(m.get("duration_s"), 0), lat(m.get("K1_t0_to_cmd_s")), lat(d.get("first")), lat(d.get("last")),
               touches(m), k3.get("visible_restarts"), k3.get("invisible_restarts"), k3.get("operator_interventions"),
               ", ".join("%s %s" % kv for kv in sorted((m.get("restarts") or {}).items()))), "",
           "| Компонент | PSS в начале → в конце, МиБ | наклон, МиБ/ч |", "|---|---|---|"]
    out += ["| %s | %s → %s | %s |" % (k, num(v.get("pss_mib_start"), 1), num(v.get("pss_mib_end"), 1),
                                      num(v.get("slope_mib_h"), 1)) for k, v in sorted(mem.items())]
    return out


def build(a0: dict, a1: dict, start: Optional[dict], budget: Optional[dict], long: Optional[dict],
          shapes: Optional[dict]) -> str:
    k3 = lambda m: m.get("K3") or {}  # noqa: E731
    lines = ["# A0 против A1 на цифровом двойнике: сводка", "",
             "Прогоны 16.10.2026, ВМ sim (Jazzy, Gazebo Harmonic), мир `hall` (README §3), одно расписание жестов и "
             "отказов. Все числа — **ориентир стенда, не результат робота** [замер на двойнике]; результат дают "
             "счётчики (касания, глухие жесты, вмешательства человека) и только HIL у лаборатории.", "",
             "## Таблица A0 / A1", "",
             "| Метрика (PLAN §1) | A0 — стек лаборатории как на роботе | A1 — пакет 1 + безопасность v2 | Цель |",
             "|---|---|---|---|",
             row("Длительность, жестов / отрицательных", "%s с, %d / %d" % (
                 num(a0["duration_s"], 0), a0["segments"]["gestures"], a0["segments"]["negatives"]),
                 "%s с, %d / %d" % (num(a1["duration_s"], 0), a1["segments"]["gestures"],
                                   a1["segments"]["negatives"])),
             row("K1 t0 → первая ненулевая /cmd_vel, p50 / p95, с", lat(a0.get("K1_t0_to_cmd_s")),
                 lat(a1.get("K1_t0_to_cmd_s")), "стенд ≤ 0,5 (p95); Э2: p50 ≤ 0,7"),
             row("K2 t0 → начало движения, p50 / p95, с", lat(a0.get("K2_t0_to_motion_s")),
                 lat(a1.get("K2_t0_to_motion_s")), "K1 + ≤ 0,3"),
             row("Реакции на жесты (наблюдатель)", reactions(a0), reactions(a1), "—"),
             row("…из них оборвала или не пустила защита", "—", "%d" % (a1.get("stopped_by_safety") or {}).get("n", 0),
                 "—"),
             row("K3 глухие жесты (детектор не работал)", str(k3(a0).get("deaf_gestures_n")),
                 str(k3(a1).get("deaf_gestures_n")), "0"),
             row("K3 вмешательства человека", str(k3(a0).get("operator_interventions")),
                 str(k3(a1).get("operator_interventions")), "0"),
             row("K3 перезапуски: видимые / невидимые", "%s / %s" % (k3(a0).get("visible_restarts"),
                                                                 k3(a0).get("invisible_restarts")),
                 "%s / %s" % (k3(a1).get("visible_restarts"), k3(a1).get("invisible_restarts")), "0 видимых"),
             row("K4 вызовы LLM на жест", str((a0.get("K4") or {}).get("llm_calls_outside_requests")),
                 str((a1.get("K4") or {}).get("llm_calls_outside_requests")), "0"),
             row("Агент: ответил на реплики", agent(a0), agent(a1), "—"),
             row("K6 «едет без свежей команды > 0,5 с»", k6(a0), k6(a1), "0"),
             row("K7 нежелательные действия (в час)", "%s (%s)" % ((a0.get("K7") or {}).get("total"),
                                                                 num((a0.get("K7") or {}).get("per_hour"), 1)),
                 "%s (%s)" % ((a1.get("K7") or {}).get("total"), num((a1.get("K7") or {}).get("per_hour"), 1)),
                 "0 по «не распознано»"),
             row("Касания (истина Gazebo)", touches(a0), touches(a1), "0"),
             row("K8 память всех процессов системы (PSS)", memory(a0), memory(a1), "≤ 1 МиБ/ч (8 ч)"),
             row("RTF (наблюдатель, медиана; доля < 0,95)", "%s; %s" % (
                 num((a0.get("rtf_observer") or {}).get("median"), 3),
                 num((a0.get("rtf_observer") or {}).get("share_below"), 3)),
                 "%s; %s" % (num((a1.get("rtf_observer") or {}).get("median"), 3),
                             num((a1.get("rtf_observer") or {}).get("share_below"), 3)), "≥ 0,95"),
             "", "## Итог по критериям", ""] + verdicts(a0, a1, start, budget) + [
             "", "## K5: отказы по расписанию", ""] + fault_table(a0, a1)
    if start:
        lines += ["", "## K9: start_test (A1)", ""] + start_table(start)
    if budget:
        lines += ["", "## Бюджет 4 ГБ: A1 целиком в jetson.slice", ""] + budget_table(budget)
    if long:
        lines += ["", "## Длительный прогон A1", ""] + long_table(long)
    if shapes:
        lines += ["", "## «Форма → класс» сетью лаборатории (shapeclass, 50 кодирований)", ""] + shape_table(shapes)
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m neurobot.sim.runs.compare", description=__doc__.split("\n\n")[0])
    p.add_argument("--a0", required=True, help="каталог прогона A0")
    p.add_argument("--a1", required=True, help="каталог прогона A1")
    p.add_argument("--start", default="", help="каталог start_test (summary.json)")
    p.add_argument("--budget", default="", help="каталог budget_a1.py (run.json)")
    p.add_argument("--long", default="", help="каталог длительного прогона A1")
    p.add_argument("--shapeclass", default="", help="каталог shapeclass (table.json)")
    p.add_argument("--out", required=True, help="compare.md")
    a = p.parse_args(argv)
    start = M.load_json(os.path.join(a.start, "summary.json")) if a.start else None
    budget = (M.load_json(os.path.join(a.budget, "run.json")) or {}).get("budget") if a.budget else None
    shapes = (M.load_json(os.path.join(a.shapeclass, "table.json")) or {}).get("groups") if a.shapeclass else None
    text = build(load_metrics(a.a0), load_metrics(a.a1), start, budget, load_metrics(a.long or None), shapes)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
