"""Сводка прогона аналога: вызовы LLM и токены по источникам, байты, ответы на реплики, память/CPU.

Источники: llmtap.jsonl (класс запроса, байты на проводе агента), mockllm_journal.jsonl (usage, кэш, оценка
сегментов, решение оракула; связь — id ответа chatcmpl-mock-N ↔ req_id mock-N), play.jsonl (реплики, пуши),
procmon.jsonl / sysmon.jsonl (память, CPU), simmcp_report.json (оценка по истинному состоянию), manifest.json.

Фазы по стенному времени: startup (до начала дня — старт агента, первый heartbeat PicoClaw через 1 с),
day (окно дня), after (после конца окна, до остановки). Суточные величины — только фаза day.

    python -m bench.baselines.summary bench/baselines/runs/<run_id>
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from .procmon import load_rows, summarize_rows
from .timecomp import metric_validity


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _num_id(s: str | None) -> int | None:
    m = re.search(r"(\d+)$", s or "")
    return int(m.group(1)) if m else None


def _pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    v = sorted(vals)
    i = min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))
    return v[i]


def summarize(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    man = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    k = float(man.get("k") or 1.0)
    validity = metric_validity(k)
    wall0 = man.get("wall0")
    wall_end = man.get("wall_end") or man.get("wall_end_planned")
    t0, t_end = man.get("t0"), man.get("t_end")
    tap = [r for r in _jsonl(run_dir / "llmtap.jsonl") if "cls" in r]
    mock = {_num_id(r.get("req_id")): r for r in _jsonl(run_dir / "mockllm_journal.jsonl") if r.get("type") == "llm"}

    def phase(ts: float) -> str:
        if wall0 is None or ts < wall0:
            return "startup"
        if wall_end is not None and ts >= wall_end:
            return "after"
        return "day"

    def zero() -> dict:
        return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "bytes_up": 0,
                "bytes_down": 0, "errors": 0, "tools_in_request_max": 0}

    adjusted = [r["usage_adjusted"] for r in tap if r.get("usage_adjusted")]
    by_phase: dict[str, dict] = defaultdict(zero)
    by_cls: dict[str, dict] = defaultdict(zero)
    decisions: dict[str, int] = defaultdict(int)
    segments: dict[str, int] = defaultdict(int)
    unmatched = 0
    rows = []
    for r in tap:
        ph = phase(r["ts_wall"])
        m = mock.get(_num_id(r.get("mock_id")))
        # ответ, укороченный llmtap до цели сводки, — usage из llmtap; иначе — из журнала mock-llm
        usage = (r.get("usage") if r.get("usage_adjusted") else None) or (m or {}).get("usage") or r.get("usage") or {}
        cached = usage.get("cached_tokens")
        if cached is None:
            cached = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
        b = r.get("bytes") or {}
        for agg in (by_phase[ph],) + ((by_cls[r["cls"]],) if ph == "day" else ()):
            agg["calls"] += 1
            agg["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            agg["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            agg["cached_tokens"] += int(cached or 0)
            agg["bytes_up"] += int(b.get("request_total") or 0)
            agg["bytes_down"] += int(b.get("response_total") or 0)
            agg["errors"] += int(r.get("status") != 200)
            agg["tools_in_request_max"] = max(agg["tools_in_request_max"], int(r.get("n_tools") or 0))
        if m is None:
            unmatched += 1
        if ph == "day" and m is not None:
            decisions[(m.get("oracle") or {}).get("decision", "?")] += 1
            for sk, sv in ((m.get("estimate") or {}).get("segments") or {}).items():
                segments[sk] += int(sv or 0)
        rows.append({"seq": r.get("seq"), "phase": ph, "cls": r["cls"],
                     "t_day": round(t0 + (r["ts_wall"] - wall0) * k, 2) if wall0 is not None else None,
                     "n_tools": r.get("n_tools"), "n_messages": r.get("n_messages"),
                     "prompt_tokens": usage.get("prompt_tokens"), "cached_tokens": cached,
                     "decision": ((m or {}).get("oracle") or {}).get("decision"),
                     "tool_calls": r.get("tool_calls"), "status": r.get("status")})

    play = _jsonl(run_dir / "play.jsonl")
    users = [p for p in play if p.get("type") == "user"]
    pushes = [p for p in play if p.get("type") == "push"]
    rtt = [u["rtt_wall_ms"] for u in users if u.get("ok")]
    lag = [u["lag_day_s"] for u in users]

    proc_rows = load_rows(run_dir / "procmon.jsonl") or load_rows(run_dir / "sysmon.jsonl")
    res_all = summarize_rows(proc_rows)
    res_day = summarize_rows(proc_rows, wall0, wall_end)

    sim = {}
    p = run_dir / "simmcp_report.json"
    if p.exists():
        try:
            rep = json.loads(p.read_text(encoding="utf-8")) or {}
            sim = rep.get("summary") or {}
        except ValueError:
            sim = {}

    day_h = (t_end - t0) / 3600.0 if (t0 is not None and t_end is not None) else None
    d = by_phase.get("day", zero())
    per_hour = {key: round(d[key] / day_h, 3) for key in ("calls", "prompt_tokens", "completion_tokens",
                                                            "cached_tokens", "bytes_up", "bytes_down")} if day_h else {}
    out = {
        "run_id": man.get("run_id"), "system": man.get("system"), "profile": man.get("profile"), "k": k,
        "host": (man.get("host") or {}).get("executor"),
        "metric_validity": validity,
        "day": {"span": [t0, t_end], "hours": round(day_h, 3) if day_h else None,
                "home": (man.get("day") or {}).get("header", {}).get("home"),
                "seed": (man.get("day") or {}).get("header", {}).get("seed"),
                "profile": (man.get("day") or {}).get("header", {}).get("profile"),
                "counts": (man.get("day") or {}).get("header", {}).get("counts")},
        "llm": {"by_phase": dict(by_phase), "day_by_source": dict(by_cls), "day_per_hour": per_hour,
                "oracle_decisions": dict(decisions), "prompt_segments_day": dict(segments),
                "tap_records": len(tap), "unmatched_mock": unmatched,
                "summaries_fitted": {"n": len(adjusted),
                                     "completion_tokens_removed": sum(a["completion_before"] - a["completion_after"]
                                                                      for a in adjusted)}},
        "user": {"sent": len(users), "ok": sum(1 for u in users if u.get("ok")),
                 "latency_valid_for_t2": validity["latency_valid_for_t2"],
                 "rtt_wall_ms_p50": _pct(rtt, 0.5), "rtt_wall_ms_p95": _pct(rtt, 0.95),
                 "lag_day_s_p50": _pct(lag, 0.5), "lag_day_s_max": max(lag) if lag else None},
        "push": {"mode": (man.get("push") or {}).get("mode"), "sent": len(pushes),
                 "errors": sum(1 for p in pushes if not p.get("ok"))},
        "resources": {"day": res_day, "all": res_all,
                      "validity": validity, "note": validity["note"]},
        "sim_eval": {key: sim.get(key) for key in ("commands", "questions", "situations_with_actions")
                     if key in sim},
        "deviations": man.get("deviations") or [],
        "errors": man.get("errors") or [],
        "stats": man.get("stats") or {},
    }
    (run_dir / "llm_calls.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                             encoding="utf-8")
    return out


def _fmt_kib(v: Any) -> str:
    return "—" if v is None else f"{v / 1024:.1f} МиБ"


def write_summary(run_dir: Path, s: dict) -> None:
    run_dir = Path(run_dir)
    (run_dir / "summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    llm = s["llm"]
    d = llm["by_phase"].get("day") or {}
    lines = [f"# {s['system']} ({s['profile']}) — {s['run_id']}", "",
             f"Хост: {s['host']}; сжатие k = {s['k']:g}; день {s['day']['home']} seed {s['day']['seed']} "
             f"профиль {s['day']['profile']}, {s['day']['hours']} ч дня. **Ориентиры, не результаты.**", "",
             "| фаза / источник | вызовы LLM | prompt | cached | completion | байт ↑ | байт ↓ | tools max |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, agg in list(llm["by_phase"].items()) + [(f"day: {c}", a) for c, a in llm["day_by_source"].items()]:
        lines.append(f"| {name} | {agg['calls']} | {agg['prompt_tokens']} | {agg['cached_tokens']} | "
                     f"{agg['completion_tokens']} | {agg['bytes_up']} | {agg['bytes_down']} | "
                     f"{agg['tools_in_request_max']} |")
    r = s["resources"]["day"]
    lines += ["", f"Окно дня: {d.get('calls', 0)} вызовов, {d.get('prompt_tokens', 0)} входных токенов "
                  f"({llm['day_per_hour'].get('calls', 0)} вызовов/ч дня).",
              f"Решения оракула: {llm['oracle_decisions']}.",
              f"Реплики: {s['user']['ok']}/{s['user']['sent']} с ответом; RTT p50 {s['user']['rtt_wall_ms_p50']} мс "
              f"стенных; опоздание подачи p50 {s['user']['lag_day_s_p50']} с дня.",
              f"Пуш ({s['push']['mode']}): {s['push']['sent']} событий, ошибок {s['push']['errors']}.",
              f"RSS дерева: медиана {_fmt_kib((r.get('rss_kib') or {}).get('median'))}, пик "
              f"{_fmt_kib((r.get('rss_kib') or {}).get('max'))}; корень (оркестратор): медиана "
              f"{_fmt_kib((r.get('root_rss_kib') or {}).get('median'))}, пик "
              f"{_fmt_kib((r.get('root_rss_kib') or {}).get('max'))}; сэмплов {r.get('samples')}.",
              f"Оценка sim-mcp: {json.dumps(s['sim_eval'], ensure_ascii=False)}"]
    if not (s.get("metric_validity") or {}).get("latency_valid_for_t2", True):
        lines += ["", "**CPU за сутки, задержки и наклон памяти невалидны для T2; числа — диагностика.**"]
    if s["deviations"]:
        lines += ["", "Отклонения сжатия времени:"] + [f"- {x}" for x in s["deviations"]]
    if s["errors"]:
        lines += ["", "Ошибки:"] + [f"- {x}" for x in s["errors"]]
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    for rd in argv:
        s = summarize(Path(rd))
        write_summary(Path(rd), s)
        print((Path(rd) / "summary.md").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
