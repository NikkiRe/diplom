"""Сводка compare.md (README §9): A0 и прогоны A1 по метрикам PLAN §1, итог по критериям приёмки Э2 и §13.4.

  python3 -m neurobot.sim.runs.compare --a0 DIR --a1 МЕТКА=DIR [--a1 МЕТКА=DIR …] [--also МЕТКА=DIR …]
       [--start МЕТКА=DIR …] [--budget МЕТКА=DIR] [--k6 МЕТКА=FILE …] [--k6-ref ПОДПИСЬ=FILE …]
       [--long МЕТКА=DIR] [--shapeclass DIR] [--runs МЕТКА=DIR …] [--title ТЕКСТ] [--notes FILE] --out compare.md

Столбцы — A0 и прогоны A1 по порядку --a1 (МЕТКА — версия кода, например «A1 16.10»); итог по критериям — по
последнему столбцу. Прочие источники привязаны к столбцу его МЕТКОЙ: --also — другие прогоны того же кода (повторы,
бюджет, длительный, дымовой): «0 касаний» считается по всем прогонам столбца сразу, а не по одному; --start — части
start_test (циклы сводятся вместе тем же summarize, что у пакета); --budget — прогон budget_a1.py; --k6 — замеры
verify/k6.py пакета deploy (stop через MCP, препятствие по скану), каждый файл — серия. Чего у столбца нет — «не
измерено», а 100 циклов start_test не набралось — «мало данных». --k6-ref — серии k6.py для справки (только таблица
K6, без итога): прежние замеры на другой версии кода. Каталоги прогонов — метрики metrics.py (metrics.json рядом или
пересчёт). --runs — прогоны A1 для таблицы по отдельности (повторы, бюджет, длительный, чувствительность к «нет
отражения» лидара и миру). --notes — разбор прогонов человеком (Markdown), идёт в конец как есть. Вместо каталога
можно дать готовый metrics.json (пересчёт новым metrics.py рядом с прежними файлами). Все числа — стенд в общей ВМ:
ориентир, не результат робота.

Честный счёт (20.10, H13–H15): у квантилей — ранговый ДИ, у нулей — верхняя 95 % граница (≈ 3/n; в час — по Пуассону),
видимые перезапуски — честно и по счёту до 20.10, K5 по типам отказа (TTD, TTR после снятия причины, недоступность),
наклон памяти по жизням процессов с TOST; «p95 ≤ порога» выполнено, только если доля превышений ниже 5 % с 95 %
уверенностью (0 превышений — от 59 замеров подряд), иначе — мало данных. Строки, которые показывает пульт («Запись»,
neurobot/ui/replay.py), — прежние: их ячейки те же.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Callable, Dict, List, Optional, Tuple

from neurobot.sim.runs import metrics as M

DEPLOY = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "deploy"))
# T холодного старта на стенде: ⌈1,2 × наибольшее «ОК» doctor во всех циклах до итогового start_test⌉ — 21,1 с в 122
# циклах 16–18.10 → 26 с; зафиксировано до start_test итогового кода (acceptfix2/k9_T.json) [замер, ориентир]
K9_T_S = 26.0
K6_MCP_S, K6_SCAN_S = 0.100, 0.150   # PLAN §1, K6: stop через MCP → ноль; препятствие → ноль после скана (p95)
K1_P95_S = 0.5                       # PLAN §1, K1: стенд, p95 t0 → первая ненулевая /cmd_vel
ZERO_EXCEED_N = 59                   # «p95 ≤ порога» без превышений — от стольких замеров (PROTOCOL §3)


def load_metrics(run_dir: Optional[str]) -> Optional[dict]:
    """Метрики прогона: файл .json как есть, у каталога — его metrics.json или пересчёт."""
    if not run_dir:
        return None
    path = run_dir if run_dir.endswith(".json") else os.path.join(run_dir, "metrics.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return M.summarize(run_dir)


def start_summary(dirs: List[str]) -> Optional[dict]:
    """Циклы start_test из каталогов по порядку (cycles.jsonl) → summary пакета; None — каталогов нет."""
    if not dirs:
        return None
    if DEPLOY not in sys.path:
        sys.path.insert(0, DEPLOY)
    from neurobot_deploy.start_test import summarize
    cycles = []
    for d in dirs:
        cycles += M.load_jsonl(os.path.join(d, "cycles.jsonl"))
    return summarize(cycles)


def num(v, nd: int = 2) -> str:
    """Число по-русски: десятичная запятая."""
    if v is None:
        return "—"
    if isinstance(v, float):
        return (("%." + str(nd) + "f") % v).replace(".", ",")
    return str(v)


def ms(v: Optional[float]) -> str:
    return "—" if v is None else "%s мс" % num(1000.0 * v, 1 if abs(v) < 0.01 else 0)


def lat(d: Optional[dict]) -> str:
    if not d or not d.get("n"):
        return "нет"
    return "%s / %s (n=%d)" % (num(d.get("p50")), num(d.get("p95")), d["n"])


def is_a0(m: dict) -> bool:
    return m.get("system") == "a0"


def reactions(m: dict) -> str:
    r = m.get("reactions") or {}
    return ", ".join("%s %d" % (k, r[k]) for k in ("correct", "wrong", "none", "interrupted", "unobserved") if k in r)


def correct(m: dict) -> Tuple[int, int]:
    """(верных реакций, жестов) по наблюдателю."""
    return (m.get("reactions") or {}).get("correct", 0), (m.get("segments") or {}).get("gestures", 0)


def early(m: dict) -> str:
    d = m.get("early_t0_to_cmd_s") or {}
    return "%s (n=%d)" % (num(d.get("p50")), d["n"]) if d.get("n") else "нет"


def travel(m: dict) -> str:
    t = m.get("travel") or {}
    if not t:
        return "—"
    jumps = t.get("teleports") or {}
    return "%s м; в движении %s с (%s %%)%s" % (
        num(t.get("distance_m"), 1), num(t.get("moving_s"), 0), num(100.0 * (t.get("moving_share") or 0), 0),
        "; переносы не в счёт: %d, %s м" % (jumps["n"], num(jumps.get("m"), 1)) if jumps.get("n") else "")


def safety_share(m: dict) -> str:
    sh = (m.get("travel") or {}).get("safety_share") or {}
    if not sh:
        return "нет safety_gate" if is_a0(m) else "—"
    return " / ".join("%s %s %%" % (k, num(100.0 * sh.get(k, 0.0), 0)) for k in M.SAFETY_STATES)


def dead_end(m: dict) -> str:
    d = m.get("dead_ends")
    if d is None:
        return "—"
    if is_a0(m) or not d["n"]:
        return "нет safety_gate" if is_a0(m) else "0"
    return "%d: %s с, жестов без реакции %d (%s)" % (d["n"], num(d["total_s"], 0), d["gestures"], "; ".join(
        "с %s с: %s" % (num(x["at_s"], 0), ", ".join(x["reasons"])) for x in d["spans"]))


def escape(m: dict) -> str:
    """Эпизоды тупика (touch, вперёд нельзя или к препятствию) и отъезды исполнителя из них (приёмка 18.10)."""
    e = m.get("escapes")
    if is_a0(m):
        return "нет safety_gate"
    if not e or not (e["episodes"] or e["starts"]):
        return "0"
    return "%d, вышел отъездом %d; дольше всего %s с; отъездов %d (%s)" % (
        e["episodes"], e["escaped"], num(e["max_s"], 0), e["starts"],
        ", ".join("%s %d" % kv for kv in sorted(e["outcomes"].items())) or "исходов нет")


def moves(m: dict) -> str:
    mv = m.get("move_requests") or {}
    if not mv.get("n"):
        return "—"
    out = "%d из %d: поехал %d, сообщил о запрете %d; «готово» без движения %d, не подтверждено %d, без ответа %d" % (
        mv["success"], mv["n"], mv.get("moved", 0), mv.get("refused", 0), mv.get("false_ok", 0),
        mv.get("unconfirmed", 0), mv.get("no_answer", 0))
    return out + (" (воркер 8766 — заглушка: робот не двигается)" if is_a0(m) else "")


def unsafe(m: dict) -> str:
    d = m.get("K6_unsafe_no_fresh_cmd") or {}
    silent, ign = d.get("silent") or {}, d.get("ignored_zero") or {}
    out = (m.get("K6_world_outage") or {})
    return "эпизодов %d, %s с (молчание писателя %d, ноль не исполнен %d)%s" % (
        d.get("n", 0), num(d.get("total_s")), silent.get("n", 0), ign.get("n", 0),
        "; из них мир двойника стоял (смерть базы, робот по истине не ехал): %d, %s с" % (
            out["n"], num(out.get("total_s"), 1)) if out.get("n") else "")


def k6_moving(m: dict) -> Optional[tuple]:
    """(эпизодов K6 вне перерывов мира двойника, в перерывах) — K6 по истине: в перерыв робот стоит."""
    d = m.get("K6_unsafe_no_fresh_cmd")
    if d is None:
        return None
    out = (m.get("K6_world_outage") or {}).get("n", 0)
    return d.get("n", 0) - out, out


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
    service = ", ".join(str(k) for k in sorted(a.get("service_calls_by_request") or {}, key=int))
    return "%d из %d; LLM на реплику %s, токенов на вызов %s, инструментов %s; служебных вызовов (сжатие истории " \
           "PicoClaw) %d%s" % (a.get("ok", 0), a.get("requests", 0), num(calls, 0), num(tok, 0),
                               "/".join(str(x) for x in a.get("n_tools_in_catalog") or []) or "—",
                               a.get("service_calls", 0), " — на репликах № " + service if service else "")


def lost(m: dict) -> str:
    k3 = m.get("K3") or {}
    return "%s (заняты своим манёвром %d, на защёлке %d)" % (
        k3.get("deaf_gestures_n"), len(k3.get("busy_gestures") or []), len(k3.get("latched_gestures") or []))


def humans(m: dict) -> str:
    k3 = m.get("K3") or {}
    total, safety = k3.get("operator_interventions") or 0, k3.get("operator_safety") or 0
    return "%d: ремонт %d, безопасность (перенос, снятие защёлки) %d" % (total, total - safety, safety)


# ---- честный счёт (H13–H15): интервалы, границы нулей, видимые перезапуски, K5, наклон памяти

def pct(v: Optional[float], nd: int = 1) -> str:
    return "—" if v is None else "%s %%" % num(100.0 * v, nd)


def ci_pair(c: Optional[list], fmt: Callable[[Optional[float]], str] = num) -> str:
    """[низ; верх], сторона без значения — «—»."""
    if not c:
        return "—"
    return "[%s; %s]" % tuple("—" if x is None else fmt(x) for x in c)


def lat_ci(d: Optional[dict]) -> str:
    """Ранговый 95 % ДИ p50 и p95 (metrics.quantiles_ci): верхней границы p95 при n < 72 нет."""
    ci = (d or {}).get("ci95")
    if not ci:
        return "нет" if not (d or {}).get("n") else "—"
    return "p50 %s; p95 %s" % (ci_pair(ci.get("p50")), ci_pair(ci.get("p95")))


def bound(b: Optional[dict]) -> str:
    """0 из n — «(≤ x %)» (односторонняя 95 %, ≈ 3/n), иначе — «[низ; верх]» Клоппера–Пирсона."""
    if not b:
        return ""
    if b["k"] == 0:
        return "(≤ %s)" % pct(b["upper"])
    return ci_pair(b["ci"], lambda x: pct(x, 0))


def zero_of(k: Optional[int], n: Optional[int], what: str = "") -> str:
    """Счётчик с границей нуля: «0 (≤ 3,7 % из 80 жестов)»; не ноль — как есть."""
    if k is None:
        return "—"
    b = M.zero_bound(k, n) if not k and n else None
    return str(k) if b is None else "0 (≤ %s из %d%s)" % (pct(b["upper"]), n, what)


def zero_rate(k: Optional[int], hours: float) -> str:
    """Счётчик за часы с границей нуля в час (Пуассон): «0 (≤ 5,3 в час за 0,57 ч)»."""
    if k is None:
        return "—"
    b = M.rate_bound(k, hours) if not k else None
    return str(k) if b is None else "0 (≤ %s в час за %s ч)" % (num(b["upper_per_h"], 1), num(hours, 2))


def hours(m: dict) -> float:
    return (m.get("duration_s") or 0.0) / 3600.0


def honest(m: dict) -> dict:
    return (m.get("K3") or {}).get("honest") or {}


def visible_cell(m: dict) -> str:
    """Видимые перезапуски: честно k из n [ДИ] (+ вне расписания) и по счёту до 20.10."""
    h = honest(m)
    if not h:
        return "—"
    extra = h.get("unscheduled")
    out = "%d из %d %s" % (h["visible"], h["faults"], bound(h.get("bound")))
    if extra and extra["n"]:
        out += "; вне расписания %d из %d" % (extra["visible"], extra["n"])
    return out + "; до 20.10 — %d" % h["visible_legacy"]


def unscheduled_cell(m: dict) -> str:
    extra = honest(m).get("unscheduled")
    if extra is None:
        return "нет журнала супервизора" + (" (A0 без супервизии)" if is_a0(m) else "")
    return "%d, видимых %d" % (extra["n"], extra["visible"])


REASONS = (("cut", "оборван манёвр"), ("cut_safety", "оборван защитой по датчику отказа"), ("lost", "потерян жест"),
           ("late", "поздно (> 3 с)"), ("outage", "недоступен дольше паузы до жеста"), ("human", "нужен человек"),
           ("latch", "защёлка снята человеком"), ("not_recovered", "не поднялось само"),
           ("agent", "реплика без ответа"))


def reasons_cell(m: dict) -> str:
    by = honest(m).get("by_reason") or {}
    return ", ".join("%s %d" % (name, by[k]) for k, name in REASONS if by.get(k)) or "—"


def sli_cell(m: dict) -> str:
    """Верная реакция ≤ 3 с в окнах отказов и вне их: k из n [ДИ]."""
    s = honest(m).get("sli_correct_3s")
    if not s:
        return "—"
    return "; ".join("%s %d из %d %s" % (name, s[k]["k"], s[k]["n"], bound(s[k]["bound"]))
                     for k, name in (("in_windows", "в окнах"), ("outside", "вне")) if s[k]["n"]) or "—"


def decision(sen: dict) -> str:
    """Исход TOST K8 (PROTOCOL §1): при lag-1 ≥ 0,2 или неотделённом прогреве — «не определено» при любом положении
    ДИ, весь ДИ за ±δ тогда — справочно."""
    d = sen.get("decision")
    up = (sen.get("ci90") or [0])[0] > 0
    if d == "equivalent":
        return "тренда нет (TOST)"
    if d == "not_equivalent":
        return ("растёт" if up else "убывает") + " быстрее δ" if sen.get("beyond") else "эквивалентность не показана"
    if d == "undetermined":
        why = ["lag-1 ≥ 0,2"] * (sen.get("lag1_ok") is False) + \
            ["прогрев не отделён в %d сегм." % sen["warmup_at_limit"]] * bool(sen.get("warmup_at_limit"))
        return "не определено: %s%s" % (", ".join(why) or "автокорреляция или прогрев",
                                         ("; справочно весь ДИ " + ("выше δ" if up else "ниже −δ"))
                                         if sen.get("beyond") else "")
    return {"short": "мало данных"}.get(d, "—")


def sen_cell(sen: Optional[dict], unit: str = "МиБ/ч") -> str:
    if not sen:
        return "—"
    if sen.get("decision") == "short":
        return "мало данных (%d сегм.)" % sen["segments"]
    return "%s %s %s" % (num(sen["slope"], 1), ci_pair(sen["ci90"], lambda x: num(x, 1)), unit)


def touch_rate(m: dict) -> str:
    """Касания в час: при нуле — верхняя 95 % граница, иначе — точный ДИ Пуассона."""
    k, h = (m.get("touches") or {}).get("episodes", 0), hours(m)
    b = M.rate_bound(k, h)
    if not b:
        return "—"
    if not k:
        return "≤ %s за %s ч" % (num(b["upper_per_h"], 1), num(h, 2))
    return "%s %s за %s ч" % (num(k / h, 1), ci_pair(b["ci_per_h"], lambda x: num(x, 1)), num(h, 2))


def memory_honest(m: dict) -> str:
    """Наклон суммы памяти робота — сумма наклонов компонентов по жизням их процессов, ДИ по Бонферрони не ниже 90 %,
    TOST при δ (metrics.sum_trend, H14)."""
    mem = ((m.get("K8") or {}).get("memory") or {}).get("_total") or {}
    sen = mem.get("sen")
    if not sen:
        return "—"
    if sen.get("decision") == "short":
        return "мало данных"
    return "%s — %s (δ %s; %s ч; сумма %d компонентов, у каждого — по жизням процессов)" % (
        sen_cell(sen), decision(sen), num(sen.get("delta"), 2), num(sen["hours"], 2), sen["components"])


def column(label: str, m: dict, also=(), start=None, budget=None, budget_run=None, k6=()) -> dict:
    """Столбец сводки: прогон и всё, что относится к той же версии кода (README §9)."""
    return {"label": label, "m": m, "also": list(also), "start": start, "budget": budget, "budget_run": budget_run,
            "k6": list(k6)}


def table(head: List[str], rows: List[List[str]]) -> List[str]:
    return ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(r) + " |" for r in rows]


def fault_table(cols: List[dict]) -> List[str]:
    """Отказы всех систем по сегменту расписания (одни жесты — одни сегменты): отказ, которого у системы в
    расписании не было (драйверы — с 18.10), — «не прогонялся»."""
    def key(f):
        return f["fault"], f.get("seg") if f.get("seg") is not None else round(f["at_s"])
    by = [{key(f): f for f in c["m"].get("K5_faults") or []} for c in cols]

    def cell(f):
        if f is None:
            return "не прогонялся"
        if f.get("skipped"):
            return "нет компонента"
        if "unavailable_s" not in f:                  # metrics.json до 20.10
            ttr = "%s с" % num(f["ttr_s"], 1) if f.get("ttr_s") is not None else "не поднялось"
            return ttr + (" — **да**: " + "; ".join(f.get("why") or []) if f.get("visible") else " — нет")
        up = "%s с" % num(f["unavailable_s"], 1) if f["unavailable_s"] is not None else "не поднялось"
        if f.get("ttr_after_s") is not None and f["ttr_after_s"] != f["unavailable_s"]:
            up += " (после снятия причины %s)" % num(f["ttr_after_s"], 1)
        out = up + (" — **да**: " + "; ".join(f.get("why") or []) if f.get("visible") else " — нет")
        if bool(f.get("visible_legacy")) != bool(f.get("visible")):
            out += " (до 20.10: %s)" % ("да" if f.get("visible_legacy") else "нет")
        return out
    keys = sorted({k for b in by for k in b}, key=lambda k: next(b[k]["at_s"] for b in by if k in b))
    rows = []
    for k in keys:
        f = next((b[k] for b in by if k in b and not b[k].get("skipped")), next(b[k] for b in by if k in b))
        rows.append(["%s (%s с)" % (f["fault"], num(f["at_s"], 0)), f.get("path", "—")] + [cell(b.get(k)) for b in by])
    return table(["Отказ (от якоря)", "путь"] + ["%s: недоступность — видим?" % c["label"] for c in cols], rows)


def k6_stats(doc: dict) -> dict:
    """Серия verify/k6.py: квантили и доли в пределе по замерам (mcp: ноль до исполнителя, начало торможения и ноль в
    /cmd_vel; scan: ноль в /cmd_vel после скана с препятствием, касания)."""
    out = {}
    runs = (doc.get("mcp_stop") or {}).get("runs") or []
    if runs:
        def col(key):
            return [r.get(key) for r in runs]
        out["mcp"] = {"n": len(runs), "req": M.quantiles(col("req_zero_s")), "brake": M.quantiles(col("brake_s")),
                      "zero": M.quantiles(col("zero_s")),
                      "brake_ok": sum(1 for v in col("brake_s") if v is not None and v <= K6_MCP_S),
                      "zero_ok": sum(1 for v in col("zero_s") if v is not None and v <= K6_MCP_S),
                      "lost": sum(1 for v in col("req_zero_s") if v is None)}
    runs = (doc.get("scan_stop") or {}).get("runs") or []
    if runs:
        vals = [r.get("zero_after_scan_s") for r in runs]
        out["scan"] = {"n": len(runs), "zero": M.quantiles(vals),
                       "ok": sum(1 for v in vals if v is not None and v <= K6_SCAN_S),
                       "contacts": sum(1 for r in runs if r.get("contacts"))}
    return out


def q3(d: dict, peak: bool = False) -> str:
    return "%s / %s%s" % (ms(d.get("p50")), ms(d.get("p95")), " / " + ms(d.get("max")) if peak else "")


def k6_tables(cols: List[dict], ref: List[tuple] = ()) -> List[str]:
    """Серии verify/k6.py: по столбцам (их итог — в критериях) и для справки (ref: [(подпись, имя, замеры)]); stop
    через MCP и препятствие по скану."""
    mcp, scan = [], []
    series = [(c["label"], name, doc) for c in cols for name, doc in c["k6"]] + list(ref)
    for label, name, doc in series:
        st = k6_stats(doc)
        if "mcp" in st:
            s = st["mcp"]
            mcp.append(["%s: %s" % (label, name), str(s["n"]), q3(s["req"]),
                        "%s; %d из %d" % (q3(s["brake"]), s["brake_ok"], s["n"]),
                        "%s; %d из %d" % (q3(s["zero"]), s["zero_ok"], s["n"]), str(s["lost"])])
        if "scan" in st:
            s = st["scan"]
            scan.append(["%s: %s" % (label, name), str(s["n"]),
                         "%s; %d из %d" % (q3(s["zero"], True), s["ok"], s["n"]), str(s["contacts"])])
    out = []
    if mcp:
        out += ["stop через MCP (робот едет манёвром жеста, агент вызывает robot_stop; метки от вызова):", ""] + table(
            ["Серия", "n", "ноль в /neurobot/cmd_vel_req, p50 / p95", "начало торможения в /cmd_vel, p50 / p95; "
             "≤ 100 мс", "ноль в /cmd_vel, p50 / p95; ≤ 100 мс", "ноль не дошёл до исполнителя"], mcp) + [""]
    if scan:
        out += ["Препятствие по скану (столб Ø 5 см в 2 см от корпуса; задержка < 0 — глубина увидела раньше лидара):",
                ""] + table(["Серия", "n", "ноль в /cmd_vel после скана, p50 / p95 / max; ≤ 150 мс", "касаний"], scan)
    return out


OK_KEY = {("mcp", "zero"): "zero_ok", ("mcp", "brake"): "brake_ok", ("scan", "zero"): "ok"}


def k6_verdict(c: dict, case: str, key: str, limit: float) -> Tuple[Optional[str], Optional[bool]]:
    """Подкритерий K6 «p95 ≤ limit» по всем сериям столбца (PROTOCOL §3): не выполнен, если p95 какой-то серии выше
    предела; выполнен, если в каждой серии доля превышений меньше 5 % с 95 % уверенностью (односторонний верх
    Клоппера–Пирсона; при 0 превышений нужно ≥ 59 замеров); иначе — мало данных."""
    got = [(name, k6_stats(doc).get(case)) for name, doc in c["k6"]]
    got = [(name, s[key], s["n"] - s[OK_KEY[(case, key)]], s["n"]) for name, s in got if s and s[key].get("n")]
    if not got:
        return None, None
    st = M._stats()
    upper = [(1.0 if st is None else st.clopper_pearson(k, n, 0.90)[1]) for _, _, k, n in got]
    text = "; ".join("%s: p95 %s (n=%d), превышений %d — доля ≤ %s" % (name, ms(d["p95"]), d["n"], k, pct(u))
                     for (name, d, k, _), u in zip(got, upper))
    if any(d["p95"] > limit for _, d, _, _ in got):
        return text, False
    return text, True if all(u < 0.05 for u in upper) else None


def agent_errors_after(m: dict, fault: str) -> Optional[int]:
    """Реплики без ответа в окне отказа (от отказа до восстановления или следующего отказа)."""
    rows = [f for f in m.get("K5_faults") or [] if f["fault"] == fault and not f.get("skipped")]
    return sum(len(f.get("agent_failed") or []) for f in rows) if rows else None


def start_counts(start: Optional[dict]) -> tuple:
    """(прошло, всего) холодных стартов (вместе с контрольным после сломанных) и сломанных сценариев start_test."""
    if not start:
        return None, None
    ok = start.get("ok") or {}
    broken = {k: v for k, v in start.items() if k != "ok" and isinstance(v, dict) and "cycles" in v}
    total = sum(v.get("cycles", 0) for v in broken.values())
    return (ok.get("passed"), ok.get("cycles")), (sum(v.get("passed", 0) for v in broken.values()), total) \
        if total else None


def start_tmax(start: Optional[dict]) -> Optional[float]:
    """Наибольшее время до «ОК» doctor в холодных стартах start_test."""
    return ((start or {}).get("ok") or {}).get("t_doctor_s", {}).get("max")


def touches_all(c: dict) -> Tuple[str, int]:
    """Касания по всем прогонам столбца: («N в M прогонах (метка эпизоды, …)» и граница в час за их время, N)."""
    runs = [c["m"]] + c["also"]
    eps = [(m.get("label"), (m.get("touches") or {}).get("episodes", 0)) for m in runs]
    total = sum(n for _, n in eps)
    h = sum(hours(m) for m in runs)
    note = zero_rate(total, h)[1:] if not total and M.rate_bound(0, h) else ""
    if len(runs) == 1:
        return "%d%s" % (total, note), total
    return "%d в %d прогонах (%s)%s" % (total, len(eps), ", ".join("%s %d" % e for e in eps), note), total


def moves_ok(m: dict) -> Optional[tuple]:
    mv = m.get("move_requests") or {}
    return (mv["success"], mv["n"], mv.get("false_ok", 0)) if mv.get("n") else None


def stuck(m: dict) -> Optional[int]:
    """Отказы пути жеста, после которых система сама не поднялась или понадобился человек («глухие состояния»)."""
    rows = [f for f in m.get("K5_faults") or [] if f.get("path") == "жест" and not f.get("skipped")]
    return sum(1 for f in rows if f.get("ttr_s") is None or f.get("operator")) if rows else None


Getter = Callable[[dict], Tuple[Optional[str], Optional[bool]]]


def by_metric(fn: Callable[[dict], object], ok: Callable[[object], bool],
              show: Callable[[object], str] = str) -> Getter:
    def get(c: dict):
        v = fn(c["m"])
        return (None, None) if v is None else (show(v), ok(v))
    return get


def gesture_faults(m: dict) -> int:
    return sum(1 for f in m.get("K5_faults") or [] if f.get("path") == "жест" and not f.get("skipped"))


def n_gestures(m: dict) -> int:
    return (m.get("segments") or {}).get("gestures") or 0


def p95_rule(m: dict, limit: float = 0.5) -> Tuple[Optional[str], Optional[bool]]:
    """K1 «p95 ≤ limit» (PROTOCOL §1): выполнено — ни одного жеста выше предела из ≥ 59 или верх рангового 95 % ДИ p95
    не выше предела; не выполнено — низ ДИ выше предела; иначе — мало данных."""
    d = m.get("K1_t0_to_cmd_s") or {}
    if d.get("p95") is None:
        return None, None
    ci = (d.get("ci95") or {}).get("p95") or [None, None]
    text = "%s %s (n=%d)" % (num(d["p95"]), ci_pair(ci), d["n"])
    if d["max"] <= limit and d["n"] >= ZERO_EXCEED_N:
        return text, True
    if ci[0] is not None and ci[0] > limit:
        return text, False
    return text, True if ci[1] is not None and ci[1] <= limit else None


def p50_ci(m: dict, limit: float = 0.7) -> Tuple[Optional[str], Optional[bool]]:
    """Э2 «p50 ≤ limit» по ранговому 95 % ДИ медианы: выполнено — верх ДИ не выше предела, не выполнено — низ выше,
    иначе — мало данных; без ДИ (metrics.json до 20.10) — по оценке."""
    d = m.get("K1_t0_to_cmd_s") or {}
    if d.get("p50") is None:
        return None, None
    ci = (d.get("ci95") or {}).get("p50")
    if not ci:
        return num(d["p50"]), d["p50"] <= limit
    lo, hi = ci
    ok = True if hi is not None and hi <= limit else False if lo is not None and lo > limit else None
    return "%s %s" % (num(d["p50"]), ci_pair(ci)), ok


def criteria() -> List[Tuple[str, Getter, bool]]:
    """Критерии PLAN (Э2, §1, §13.4): (название, значение и выполнен ли по столбцу, применим ли к A0). У нулевых
    счётчиков — верхняя 95 % граница: на жест или отказ — ≈ 3/n, в час — по Пуассону (PROTOCOL §3)."""
    k3 = lambda m: m.get("K3") or {}  # noqa: E731

    def zero(fn, n_fn=None, what=""):
        def get(c):
            m = c["m"]
            v = fn(m)
            if v is None:
                return None, None
            return (zero_of(v, n_fn(m), what) if n_fn else zero_rate(v, hours(m))), v == 0
        return get

    def visible(c):
        h = honest(c["m"])
        if not h:
            return by_metric(lambda m: k3(m).get("visible_restarts"), lambda v: v == 0)(c)
        return visible_cell(c["m"]), k3(c["m"]).get("visible_restarts") == 0

    def touch(c):
        text, total = touches_all(c)
        return text, total == 0

    def reactions_ci(c):
        k, n = correct(c["m"])
        b = M.zero_bound(k, n) if n else None
        return "%d из %d%s" % (k, n, " " + bound(b) if b and k else ""), n > 0 and k == n

    def k9_cold(c):
        cold, _ = start_counts(c["start"])
        if not cold:
            return None, None
        b = M.zero_bound(cold[0], cold[1]) if cold[1] else None
        note = " (≥ %s, 95 %% CP)" % pct(b["ci"][0]) if b and cold[0] == cold[1] else ""
        return "%s из %s%s" % (cold + (note,)), None if cold[0] == cold[1] < 100 else cold[0] == cold[1] >= 100

    def k9_t(c):
        t = start_tmax(c["start"])
        return (None, None) if t is None else ("max %s с" % num(t, 1), t <= K9_T_S)

    def k9_broken(c):
        _, broken = start_counts(c["start"])
        return (None, None) if not broken else ("%s из %s" % broken, broken[1] > 0 and broken[0] == broken[1])

    def oom(c):
        v = (c["budget"] or {}).get("oom_kill") if c["budget"] else None
        return (None, None) if v is None else (str(v), v == 0)

    def k6_world(c):
        v = k6_moving(c["m"])
        if v is None:
            return None, None
        return zero_rate(v[0], hours(c["m"])) + (" (+%d в перерыв мира)" % v[1] if v[1] else ""), v[0] == 0
    return [
        ("0 «глухих» состояний: путь жеста сам поднялся после каждого отказа",
         zero(stuck, gesture_faults, " отказов пути жеста"), True),
        ("0 потерянных жестов из-за глухоты (детектор не работал)",
         zero(lambda m: k3(m).get("deaf_gestures_n"), n_gestures, " жестов"), True),
        ("0 вмешательств человека", zero(lambda m: k3(m).get("operator_interventions")), True),
        ("0 видимых перезапусков (PLAN §1): честно — из внесённых отказов [95 % ДИ]; до 20.10", visible, True),
        ("0 движений по «не распознано» (K7)",
         zero(lambda m: (m.get("K7") or {}).get("moved_on_unknown"), n_gestures, " жестов"), True),
        ("0 эпизодов «едет без свежей команды > 0,5 с» (K6; в перерыв мира двойника робот стоит)", k6_world, True),
        ("K1 p95 t0 → /cmd_vel без ранних ≤ 0,5 с (стенд) [ориентир]; 95 % ДИ p95",
         lambda c: p95_rule(c["m"], K1_P95_S), True),
        ("p50 t0 → /cmd_vel без ранних ≤ 0,7 с при 10 к/с (Э2) [ориентир]; 95 % ДИ медианы", lambda c: p50_ci(c["m"]),
         True),
        ("0 вызовов LLM на жест (K4)",
         zero(lambda m: (m.get("K4") or {}).get("llm_calls_outside_requests"), n_gestures, " жестов"), True),
        ("PicoClaw по HTTP после kill MCP-сервера — ≤ 1 ошибка",
         by_metric(lambda m: agent_errors_after(m, "kill_mcp"), lambda v: v <= 1), True),
        ("0 касаний (безопасность v2, истина Gazebo), все прогоны столбца", touch, True),
        ("верная реакция на каждый жест (наблюдатель; разбор — ниже) [95 % ДИ]", reactions_ci, True),
        ("реплика «вперёд»: робот поехал или агент сообщил о запрете; 0 «готово» без движения",
         by_metric(moves_ok, lambda v: v[0] == v[1] and v[2] == 0,
                   lambda v: "%d из %d, «готово» без движения %d" % v), True),
        ("stop через MCP → ноль в /cmd_vel ≤ 100 мс (p95, каждая серия), K6",
         lambda c: k6_verdict(c, "mcp", "zero", K6_MCP_S), False),
        ("…справочно: начало торможения в /cmd_vel ≤ 100 мс (p95) — до решения 1а 19.10 стоп агента был плавным",
         lambda c: k6_verdict(c, "mcp", "brake", K6_MCP_S), False),
        ("препятствие → ноль ≤ 150 мс после скана (p95, каждая серия), K6",
         lambda c: k6_verdict(c, "scan", "zero", K6_SCAN_S), False),
        ("start_test: 100 из 100 холодных стартов — рестарт neurobot.target, не перезагрузка ОС (K9)", k9_cold, False),
        ("холодный старт: «ОК» doctor ≤ T = %s с (стенд; на роботе T — по HIL)" % num(K9_T_S, 0), k9_t, False),
        ("doctor называет сломанный компонент во всех сценариях", k9_broken, False),
        ("бюджет 4 ГБ: 0 OOM за прогон", oom, False),
    ]


def verdicts(a0: dict, cols: List[dict]) -> List[str]:
    """Критерии по столбцам: значение A0 и каждого A1, итог — по последнему столбцу. Время — ориентир стенда.
    Подкритерии K6, start_test и бюджет у A0 не мерили: агентский stop у лаборатории идёт мимо любого стража,
    препятствие демо видит только в скане, механизмов старта и бюджета нет."""
    a0c = column("A0", a0)
    rows = []
    for name, get, for_a0 in criteria():
        cells = [get(a0c)[0] if for_a0 else None] + [get(c)[0] for c in cols]
        value, ok = get(cols[-1])
        mark = "не измерено" if value is None else "мало данных" if ok is None else "выполнено" if ok \
            else "**не выполнено**"
        if name.startswith("…справочно") and value is not None:
            mark = "справочно: " + ("в пределе" if ok else "мало данных" if ok is None else "вне предела")
        rows.append([name] + ["—" if v is None else v for v in cells] + [mark])
    return table(["Критерий", "A0"] + [c["label"] for c in cols] + ["Итог: " + cols[-1]["label"]], rows)


def breakdown_table(ms_: List[dict], heads: List[str]) -> List[str]:
    """Разбор верной реакции (metrics.reaction_breakdown) по системам."""
    def g(m, key, *reactions):
        d = ((m.get("reaction_breakdown") or {}).get("by_class") or {}).get(key) or {}
        return sum(d.get(r, 0) for r in reactions) if reactions else sum(d.values())

    def cls_known(m):
        return "unknown" not in ((m.get("reaction_breakdown") or {}).get("by_class") or {})

    def cell(m, fn):
        return fn(m) if cls_known(m) else "класс не публикуется"

    def reasons(m):
        r = ((m.get("reaction_breakdown") or {}).get("safety") or {}).get("reasons") or {}
        return ", ".join("%s %d" % kv for kv in sorted(r.items(), key=lambda kv: -kv[1])) or "—"
    every = ("correct", "wrong", "none", "interrupted", "unobserved")
    rows = [
        ["жестов", *[str((m.get("reaction_breakdown") or {}).get("gestures", 0)) for m in ms_]],
        ["класс perception верный (решающее сообщение)", *[cell(m, lambda m: str(g(m, "ok"))) for m in ms_]],
        ["…манёвр доведён — верная реакция", *[cell(m, lambda m: str(g(m, "ok", "correct"))) for m in ms_]],
        ["…манёвр оборвала / не пустила защита (safety_gate)", *[cell(m, lambda m: "%d / %d" % (
            (m["reaction_breakdown"]["safety"]).get("interrupted", 0),
            (m["reaction_breakdown"]["safety"]).get("none", 0))) for m in ms_]],
        ["…оборван или не начат без защиты, другой рисунок, не видно", *[cell(m, lambda m: str(
            g(m, "ok", *every) - g(m, "ok", "correct") - sum(
                m["reaction_breakdown"]["safety"].get(k, 0) for k in ("interrupted", "none")))) for m in ms_]],
        ["класс неверный / сообщения нет или «не распознано»",
         *[cell(m, lambda m: "%d / %d" % (g(m, "wrong"), g(m, "none"))) for m in ms_]],
        ["верная реакция всего (класс у A0 — по манёвру)", *["%d из %d" % correct(m) for m in ms_]],
        ["причины остановок защитой при верном классе (жестов)", *[cell(m, reasons) for m in ms_]],
    ]
    return table([""] + heads, rows)


def p4(p) -> str:
    return " ".join(num(float(x), 2) for x in p)


def shape_table(groups: dict) -> List[str]:
    rows = [[name, str(g.get("cls")), p4(g["demo"]["p"]), num(g["demo"].get("mute")), p4(g["poly"]["p"])]
            for name, g in sorted(groups.items()) if "demo" in g]
    return table(["Форма", "замысел (класс)", "demo: p(0…3)", "demo: «немые»", "полилиния: p(0…3)"], rows)


def start_table(start: dict) -> List[str]:
    rows = []
    for name, v in sorted(start.items()):
        if not isinstance(v, dict) or "cycles" not in v:
            continue
        t = v.get("t_doctor_s") or {}
        rows.append([name, "%s / %s" % (v.get("passed"), v.get("cycles")),
                     "%s / %s / %s" % (num(t.get("p50"), 1), num(t.get("p95"), 1), num(t.get("max"), 1)),
                     ", ".join(map(str, v.get("failed") or [])) or "—"])
    return table(["Сценарий", "прошло / всего", "время до «ОК» doctor p50 / p95 / max, с", "не прошли"], rows)


def budget_table(b: dict) -> List[str]:
    out = ["MemoryMax %s МиБ, квота %s %% на %s ядра, отношение A57/ВМ %s; OOM за прогон: %s. Квота и отношение — "
           "[допущение] калибровки бюджета 16.10: независимая проверка её вывод о квоте не воспроизвела, калибровка "
           "ядра зависит от нагрузки ВМ, — всё, что про CPU, ненадёжно." % (
               b.get("mem_max_mib"), b.get("quota_pct"), b.get("cpus"), num(b.get("ratio_a57"), 3), b.get("oom_kill")),
           ""]
    rows = [[k, num(v, 1)] for k, v in sorted((b.get("unique_mib") or {}).items())]
    rows.append(["**slice**", "**%s** (PSS %s, RSS %s)" % (
        num(b.get("slice_unique_mib"), 1), num(b.get("slice_pss_mib"), 1), num(b.get("slice_rss_mib"), 1))])
    out += table(["Компонент", "уникальные страницы, МиБ"], rows) + [""]
    margins = (b.get("margin_mib") or {}).items()
    out += table(["Ветка ОС", "запас, МиБ (value / pessimistic / optimistic)"],
                 [[br, "%s / %s / %s" % (num(c.get("value"), 0), num(c.get("pessimistic"), 0),
                                         num(c.get("optimistic"), 0))] for br, c in margins])
    return out


def runs_table(runs: List[tuple]) -> List[str]:
    """Прогоны A1 по отдельности: [(метка, метрики)] — мир, «нет отражения» лидара, верные реакции, оборвала или не
    пустила защита (из них с lidar_nodata), доли состояний safety_gate, тупики защиты, пробег, касания, K1,
    вмешательства человека и видимые перезапуски, K6."""
    rows = []
    for label, m in runs:
        stops = (m.get("stopped_by_safety") or {}).get("by_seg") or {}
        nodata = sum(1 for v in stops.values() if "lidar_nodata" in v.get("reasons", []))
        k3 = m.get("K3") or {}
        k6 = k6_moving(m) or (None, 0)
        ends = m.get("dead_ends") or {}
        rows.append([label, "%s, %s" % (m.get("world", "hall"), {"inf": "+inf"}.get(m.get("no_return"), "NaN")),
                     "%d из %d" % correct(m), "%d (%d)" % (len(stops), nodata), safety_share(m),
                     "%d; %s с" % (ends.get("n", 0), num(ends.get("total_s", 0.0), 0)), travel(m),
                     str((m.get("touches") or {}).get("episodes", 0)), lat(m.get("K1_t0_to_cmd_s")),
                     "%s / %s%s" % (k3.get("operator_interventions"), k3.get("visible_restarts"),
                                    " (до 20.10: %s)" % k3["visible_restarts_legacy"]
                                    if "visible_restarts_legacy" in k3 else ""),
                     "%s%s" % (k6[0], " (+%d в перерыв мира)" % k6[1] if k6[1] else "")])
    return table(["Прогон", "мир, «нет отражения»", "верных реакций", "оборвала или не пустила защита (lidar_nodata)",
                  "ok / slow / blocked / estop", "тупики защиты", "пробег; в движении", "касания", "K1 p50 / p95",
                  "человек / видимые перезапуски", "K6"], rows)


def paused(m: dict) -> str:
    """Остановки ВМ (сон Mac) за прогон — по скачку стенных часов против монотонных."""
    p = m.get("vm_pauses") or []
    return "; ".join("пауза ВМ %s с между %s и %s с таймлайна" % (num(x["wall_jump_s"], 0), num(x["after_s"], 0),
                                                                num(x["before_s"], 0)) for x in p) or "пауз ВМ нет"


def long_table(m: dict) -> List[str]:
    """Длительный прогон: сводка и память по компонентам — МНК (счёт до 20.10, справочно: рестарт в ряду — ступень) и
    наклон по жизням процессов с 90 % ДИ (H14), прогрев (MSER-5), файловые дескрипторы (с 20.10)."""
    mem = (m.get("K8") or {}).get("memory") or {}
    d = (m.get("K8") or {}).get("drift_cmd") or {}
    k3 = m.get("K3") or {}
    legacy = " (до 20.10: %s)" % k3["visible_restarts_legacy"] if "visible_restarts_legacy" in k3 else ""
    out = ["Длительность %s с по монотонным часам ВМ (%s); K1 p50 / p95 %s; дрейф K1: первая треть %s, последняя %s; "
           "касания %s; перезапуски видимые / невидимые %s / %s%s; вмешательства человека %s; рестарты юнитов (сам "
           "systemd): %s." % (
               num(m.get("duration_s"), 0), paused(m), lat(m.get("K1_t0_to_cmd_s")), lat(d.get("first")),
               lat(d.get("last")), touches(m), k3.get("visible_restarts"), k3.get("invisible_restarts"), legacy,
               humans(m), ", ".join("%s %s" % kv for kv in sorted((m.get("restarts") or {}).items()))), ""]

    def warm(sen):
        return "—" if not sen else "%s%s" % (num(sen["warmup_s"], 0), "; на пределе %d" % sen["warmup_at_limit"]
                                              if sen.get("warmup_at_limit") else "")

    def fd(v):
        f = v.get("fd")
        if not f:
            return "не собиралось"
        return "%s → %s (max %s; %s)" % (f["start"], f["end"], f["max"], sen_cell(f.get("trend"), "в час"))
    rows = [[k, "%s → %s" % (num(v.get("pss_mib_start"), 1), num(v.get("pss_mib_end"), 1)),
             num(v.get("slope_mib_h"), 1), sen_cell(v.get("sen")),
             "%s из %s" % ((v.get("sen") or {}).get("segments_used", "—"), (v.get("sen") or {}).get("segments", "—")),
             warm(v.get("sen")), fd(v)] for k, v in sorted(mem.items()) if k != "_total"]
    total = mem.get("_total") or {}
    tail = ["", "Сумма компонентов робота: PSS %s → %s МиБ, МНК %s МиБ/ч (до 20.10, справочно); по жизням процессов — "
            "%s." % (num(total.get("pss_mib_start"), 1), num(total.get("pss_mib_end"), 1),
                     num(total.get("slope_mib_h"), 1), memory_honest(m))] if total else []
    return out + table(["Компонент", "PSS в начале → в конце, МиБ", "МНК, МиБ/ч (до 20.10, справочно)",
                        "наклон по жизням процессов, МиБ/ч [90 % ДИ]", "жизней процессов (взято)",
                        "прогрев отсечён, с (MSER-5)", "fd: в начале → в конце"], rows) + tail


def k3_table(runs: List[tuple]) -> List[str]:
    """Видимые перезапуски по прогонам (H13): внесённых отказов, видимых по счёту до 20.10 и честно (95 % ДИ доли),
    причины, доля отказов на манёвре, рестарты вне расписания, верная реакция ≤ 3 с в окнах отказов и вне их."""
    rows = []
    for label, m in runs:
        h = honest(m)
        if not h:
            continue
        rows.append([label, str(h["faults"]), str(h["visible_legacy"]), "%d %s" % (h["visible"], bound(h["bound"])),
                     reasons_cell(m), "%d из %d" % (h["on_maneuver"], h["faults"]), unscheduled_cell(m), sli_cell(m)])
    return table(["Прогон", "внесено отказов", "видимых до 20.10", "видимых честно [95 % ДИ]",
                  "причины (у отказа их бывает несколько)", "отказ пришёлся на манёвр",
                  "рестарты вне расписания (журнал), видимых", "верная реакция ≤ 3 с: в окнах отказов / вне [95 % ДИ]"],
                 rows)


def k5_table(ms_: List[dict]) -> List[str]:
    """K5 по типам отказа по всем прогонам столбца (metrics.k5_summary): TTD, TTR после снятия причины, недоступность
    роли, первый обслуженный запрос, TTR до 20.10 (от начала отказа) — p50 / p95 / max, без средних."""
    rows_all = [f for m in ms_ for f in m.get("K5_faults") or [] if "unavailable_s" in f]
    by = M.k5_summary(rows_all)
    first = {}
    for f in rows_all:
        first.setdefault(f["fault"], f["at_s"])

    def q(d, full=True):
        if not d.get("n"):
            return "—"
        return " / ".join(num(d[k], 1) for k in (("p50", "p95", "max") if full else ("p50", "max")))
    rows = [[name, str(g["n"]), q(g["ttd_s"], False), q(g["ttr_after_s"]), q(g["unavailable_s"]),
             q(g["first_served_s"], False), q(g["ttr_legacy_s"], False), str(g["not_recovered"])]
            for name, g in sorted(by.items(), key=lambda kv: first[kv[0]])]
    if not rows:
        return []
    return table(["Отказ", "n", "TTD p50 / max, с", "TTR после снятия причины p50 / p95 / max, с",
                  "недоступность p50 / p95 / max, с", "первый обслуженный запрос p50 / max, с",
                  "TTR до 20.10 p50 / max, с", "не поднялось"], rows)


TITLE = ("ВМ sim (Jazzy, Gazebo Harmonic), мир `hall` (README §3), одно расписание жестов и отказов (у A1 18.10 — плюс "
         "отказы драйверов на своих сегментах).")
FAULT_NOTE = (
    "Недоступность — от отказа до «роль снова работает» (perception и отказы камеры — заново по probe: детектор снова "
    "вызывается; остальное — TTR runner.py), в скобках — после снятия причины (SIGSTOP — с момента, когда процесс убил "
    "супервизор, отказ камеры — с конца удержания). Видимость — по PLAN §1 честно: жест задет отказом, если отрезок от "
    "его начала до t0 + 3 с пересекает окно недоступности; оборванный манёвр — видимый перезапуск, в том числе "
    "оборванный защитой по датчику, который выключил сам отказ; «поздно» — в счёт; остановка защитой по препятствию — "
    "нет. «До 20.10» — прежний счёт (окно жеста — пересечение жеста с окном отказа, оборванный манёвр не в счёт).")
REACTION_NOTE = (
    "Верная реакция (наблюдатель) — класс распознан верно **и** манёвр демо исполнен целиком по рисунку команд и "
    "длительностям. У A0 класс демо не публикует: верность видна только по манёвру, и ошибки распознавания у его "
    "конвейера другие. A0 не останавливается защитой — доводит манёвры ценой касаний; у A1 часть манёвров с верным "
    "классом обрывает или не пускает safety_gate (причины — в последней строке). Поэтому «верная реакция» A0 и A1 "
    "сравнивает и распознавание, и цену безопасности. Формы жестов — гипотеза (R7b §4.5), сеть лаборатории — на "
    "синтетике: о точности на настоящей руке эти числа не говорят.")


def build(a0: dict, cols: List[dict], long: Optional[tuple] = None, shapes: Optional[dict] = None,
          title: str = TITLE, runs: Optional[List[tuple]] = None, k6_ref: List[tuple] = ()) -> str:
    """long — (метка, метрики) длительного прогона A1; runs — [(метка, метрики)]; k6_ref — [(подпись, имя, замеры)]."""
    ms_ = [a0] + [c["m"] for c in cols]
    heads = ["A0 — стек лаборатории как на роботе"] + ["%s — пакет 1 + безопасность v2 (`%s`)" % (
        c["label"], c["m"].get("label")) for c in cols]
    k7 = lambda m: m.get("K7") or {}  # noqa: E731
    rtf = lambda m: m.get("rtf_observer") or {}  # noqa: E731
    rows = [
        ("Длительность, жестов / отрицательных", lambda m: "%s с, %d / %d" % (
            num(m["duration_s"], 0), m["segments"]["gestures"], m["segments"]["negatives"]), ""),
        ("K1 t0 → первая ненулевая /cmd_vel, p50 / p95, с (без ранних)", lambda m: lat(m.get("K1_t0_to_cmd_s")),
         "стенд ≤ 0,5 (p95); Э2: p50 ≤ 0,7"),
        ("…95 % ДИ p50 и p95, с (ранговый; верхней границы p95 нет при n < 72)",
         lambda m: lat_ci(m.get("K1_t0_to_cmd_s")), "—"),
        ("…ранние (реакция раньше t0), p50, с", early, "—"),
        ("K2 t0 → начало движения, p50 / p95, с (без ранних)", lambda m: lat(m.get("K2_t0_to_motion_s")),
         "K1 + ≤ 0,3"),
        ("Реакции на жесты (наблюдатель)", reactions, "—"),
        ("Верная реакция на жест (разбор — ниже)", lambda m: "%d из %d" % correct(m), "все"),
        ("…из них оборвала или не пустила защита", lambda m: "—" if is_a0(m) else "%d" % (
            m.get("stopped_by_safety") or {}).get("n", 0), "—"),
        ("K3 глухие жесты (детектор не работал)", lost, "0 глухих"),
        ("K3 вмешательства человека", humans, "0"),
        ("K3 видимые перезапуски (PLAN §1): честно — из внесённых отказов [95 % ДИ]; до 20.10", visible_cell,
         "0 видимых"),
        ("…причины видимости (у отказа их бывает несколько)", reasons_cell, "—"),
        ("…рестарты вне расписания (журнал systemd и сторожа), видимых", unscheduled_cell, "0 видимых"),
        ("K4 вызовы LLM на жест", lambda m: str((m.get("K4") or {}).get("llm_calls_outside_requests")), "0"),
        ("Агент: ответил на реплики", agent, "—"),
        ("Реплики «вперёд»: поехал или агент сообщил о запрете", moves, "все; 0 «готово» без движения"),
        ("Пробег по истине; время в движении", travel, "—"),
        ("safety_gate: доля времени ok / slow / blocked / estop", safety_share, "—"),
        ("Тупики защиты: blocked без движения ≥ 60 с при жестах", dead_end, "0"),
        ("Тупики «поворот заденет (touch), вперёд нельзя или к препятствию»: эпизодов, вышел отъездом", escape,
         "все — без человека"),
        ("K6 «едет без свежей команды > 0,5 с»", unsafe, "0"),
        ("K7 нежелательные действия (в час)", lambda m: "%s (%s)" % (k7(m).get("total"), num(k7(m).get("per_hour"), 1)),
         "0 по «не распознано»"),
        ("Касания (истина Gazebo)", touches, "0"),
        ("…касаний в час: при нуле — верхняя 95 % граница, иначе ДИ (Пуассон)", touch_rate, "—"),
        ("K8 память всех процессов системы (PSS)", memory, "≤ 1 МиБ/ч (8 ч)"),
        ("…наклон суммы робота по жизням процессов, 90 % ДИ; TOST при δ (МНК выше — справочно)", memory_honest,
         "тренда нет при δ = запас / время миссии (8 ч без отказов)"),
        ("RTF (наблюдатель, медиана; доля < 0,95)", lambda m: "%s; %s" % (
            num(rtf(m).get("median"), 3), num(rtf(m).get("share_below"), 3)), "≥ 0,95"),
    ]
    lines = ["# A0 против A1 на цифровом двойнике: сводка", "",
             title + " Все числа — **ориентир стенда, не результат робота** [замер на двойнике]; результат дают "
             "счётчики (касания, глухие жесты, вмешательства человека) и только HIL у лаборатории.", "",
             "## Таблица A0 / A1", ""]
    lines += table(["Метрика (PLAN §1)"] + heads + ["Цель"], [[n] + [fn(m) for m in ms_] + [g] for n, fn, g in rows])
    lines += ["", "## Итог по критериям", ""] + verdicts(a0, cols)
    lines += ["", "## Верная реакция на жест: разбор", ""] + breakdown_table(ms_, ["A0"] + [c["label"] for c in cols])
    lines += ["", REACTION_NOTE]
    lines += ["", "## K5: отказы по расписанию", ""] + fault_table([column("A0", a0)] + cols)
    lines += ["", FAULT_NOTE]
    every = [("A0: %s" % a0.get("label"), a0)] + [("%s: %s" % (c["label"], m.get("label")), m)
                                                  for c in cols for m in [c["m"]] + c["also"]]
    lines += ["", "## K3: видимые перезапуски по прогонам — честный счёт и до 20.10", ""] + k3_table(every)
    for label, group in [("A0", [a0])] + [(c["label"], [c["m"]] + c["also"]) for c in cols]:
        rows = k5_table(group)
        if rows:
            lines += ["", "## K5 по типам отказа: %s (%s)" % (label, ", ".join(m.get("label") for m in group)), ""]
            lines += rows
    if k6_ref or any(c["k6"] for c in cols):
        lines += ["", "## K6: подкритерии (verify/k6.py пакета deploy)", ""] + k6_tables(cols, k6_ref)
    for c in cols:
        if c["start"]:
            lines += ["", "## K9: start_test, %s" % c["label"], ""] + start_table(c["start"])
        if c["budget"]:
            lines += ["", "## Бюджет 4 ГБ: %s целиком в jetson.slice" % c["label"], ""] + budget_table(c["budget"])
        if c["budget_run"]:
            lines += ["", "Тот же прогон по метрикам PLAN §1:", ""] + long_table(c["budget_run"])
    if long:
        lines += ["", "## Длительный прогон, %s (`%s`)" % (long[0], long[1].get("label")), ""] + long_table(long[1])
    if runs:
        lines += ["", "## Прогоны A1 по отдельности", ""] + runs_table(runs)
    if shapes:
        lines += ["", "## «Форма → класс» сетью лаборатории (shapeclass, 50 кодирований)", ""] + shape_table(shapes)
    return "\n".join(lines) + "\n"


def labeled(items: List[str], flag: str) -> List[Tuple[str, str]]:
    out = []
    for item in items:
        label, sep, path = item.partition("=")
        if not sep or not label or not path:
            raise ValueError("%s: нужно МЕТКА=ПУТЬ, получено %r" % (flag, item))
        out.append((label, path))
    return out


def columns(a: argparse.Namespace) -> List[dict]:
    """Столбцы A1 по --a1 и привязанные к ним по МЕТКЕ источники; чужая МЕТКА — ошибка."""
    cols = [column(label, load_metrics(path)) for label, path in labeled(a.a1, "--a1")]
    by: Dict[str, dict] = {c["label"]: c for c in cols}

    def target(label: str, flag: str) -> dict:
        if label not in by:
            raise ValueError("%s: метки %r нет среди --a1 (%s)" % (flag, label, ", ".join(by)))
        return by[label]
    for label, path in labeled(a.also, "--also"):
        target(label, "--also")["also"].append(load_metrics(path))
    starts: Dict[str, List[str]] = {}
    for label, path in labeled(a.start, "--start"):
        target(label, "--start")
        starts.setdefault(label, []).append(path)
    for label, dirs in starts.items():
        by[label]["start"] = start_summary(dirs)
    for label, path in labeled(a.budget, "--budget"):
        c = target(label, "--budget")
        c["budget"] = (M.load_json(os.path.join(path, "run.json")) or {}).get("budget")
        run = load_metrics(path)
        # тот же прогон, уже пересчитанный в столбце (--a1 или --also готовым metrics.json), — из столбца
        c["budget_run"] = next((m for m in [c["m"]] + c["also"] if m.get("label") == run.get("label")), run)
    for label, path in labeled(a.k6, "--k6"):
        target(label, "--k6")["k6"].append((os.path.basename(path).rsplit(".", 1)[0], M.load_json(path) or {}))
    return cols


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m neurobot.sim.runs.compare", description=__doc__.split("\n\n")[0])
    p.add_argument("--a0", required=True, help="каталог прогона A0")
    p.add_argument("--a1", action="append", required=True, metavar="МЕТКА=КАТАЛОГ",
                   help="прогон A1 — столбец сводки; можно несколько, итог — по последнему")
    p.add_argument("--also", action="append", default=[], metavar="МЕТКА=КАТАЛОГ",
                   help="другой прогон кода столбца МЕТКА (для «0 касаний» по всем прогонам)")
    p.add_argument("--start", action="append", default=[], metavar="МЕТКА=КАТАЛОГ",
                   help="часть start_test столбца (cycles.jsonl); можно несколько")
    p.add_argument("--budget", action="append", default=[], metavar="МЕТКА=КАТАЛОГ",
                   help="прогон budget_a1.py столбца (run.json); его метрики — из столбца, если он там уже есть")
    p.add_argument("--k6", action="append", default=[], metavar="МЕТКА=ФАЙЛ",
                   help="серия verify/k6.py столбца (JSON); можно несколько")
    p.add_argument("--k6-ref", action="append", default=[], metavar="ПОДПИСЬ=ФАЙЛ",
                   help="серия verify/k6.py для справки (только таблица K6); можно несколько")
    p.add_argument("--long", default="", metavar="МЕТКА=КАТАЛОГ", help="длительный прогон A1 (МЕТКА — версия кода)")
    p.add_argument("--shapeclass", default="", help="каталог shapeclass (table.json)")
    p.add_argument("--runs", action="append", default=[], metavar="МЕТКА=КАТАЛОГ",
                   help="прогон A1 для таблицы по отдельности (повтор, бюджет, мир, no_return); можно несколько")
    p.add_argument("--title", default=TITLE, help="первая фраза сводки: даты, мир, расписание")
    p.add_argument("--notes", default="", help="разбор прогонов (Markdown) — в конец сводки")
    p.add_argument("--out", required=True, help="compare.md")
    a = p.parse_args(argv)
    try:
        cols = columns(a)
        runs = [(label, load_metrics(path)) for label, path in labeled(a.runs, "--runs")]
        ref = [(label, os.path.basename(path).rsplit(".", 1)[0], M.load_json(path) or {})
               for label, path in labeled(a.k6_ref, "--k6-ref")]
        long = next(((label, load_metrics(path)) for label, path in labeled([a.long], "--long")), None) \
            if a.long else None
    except ValueError as e:
        print("compare: %s" % e, file=sys.stderr)
        return 2
    shapes = (M.load_json(os.path.join(a.shapeclass, "table.json")) or {}).get("groups") if a.shapeclass else None
    text = build(load_metrics(a.a0), cols, long, shapes, a.title, runs, ref)
    if a.notes:
        with open(a.notes, encoding="utf-8") as f:
            text += "\n" + f.read()
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
