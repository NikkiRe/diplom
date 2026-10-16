#!/usr/bin/env python3
"""Сводка живого смоука robot-mcp (Go, R-compact v2) против Python-прототипа R-compact и ros-mcp-server 3.1.2.

  .venv/bin/python robotmcp/tools/analyze.py [--run experiments/results/robotmcp_smoke_20261007]
        [--baseline experiments/results/robot_smoke_20261007]

Токены — точный o200k_base через bench/mockllm/tokens.py (каталог — Cookbook count_tools, как у mock-llm и
scripts/sim/rosmcp_analyze.py), vision — topic_tokens.vision_tokens (правила OpenAI на 07.10.2026), ₽ — замороженный
прайс bench/costmodel. Задачи — «оракулом» тем же simulate(), что в rosmcp_analyze.py (один запрос LLM на вызов +
короткий ответ); у асинхронных go_to/patrol добавляется пробуждение по уведомлению task_end (system + событие →
короткое уведомление пользователю). bench/ и scripts/sim/ только читаются (без __pycache__).
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "sim"))
from bench.costmodel.prices import default_pricebook  # noqa: E402
from bench.mockllm.tokens import account_request, count_completion, count_tools, load_tokenizer  # noqa: E402
from rosmcp_analyze import MODELS, SYSTEM, load, md_table, oa_tools, simulate, step_tokens  # noqa: E402

EVENT_REPLY = "Робот доехал до двери."


def wake_on_event(state: dict, tools: list, tok, pb) -> dict:
    """Пробуждение по уведомлению task_end: system + каталог + событие (дельта L1) → короткое уведомление."""
    ev = "Событие робота: " + json.dumps({"done": state.get("done"), "place": state.get("place"),
                                          "nav": state.get("nav")}, ensure_ascii=False, separators=(",", ":"))
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": ev}]
    inp = account_request({"messages": msgs, "tools": tools}, tok).prompt_tokens
    out = count_completion({"role": "assistant", "content": EVENT_REPLY}, tok)
    return {"input_tokens": inp, "output_tokens": out, "event_tokens": tok.count(ev)}


def rub(inp: float, out: float, vision: dict, pb) -> dict:
    res = {}
    for m, mid in MODELS.items():
        p = pb.price(mid)
        res[m] = round((inp + vision.get(m, 0)) * p.input + out * p.output, 4)
    return res


def proc(r: dict) -> dict:
    i, e = r.get("proc_idle") or {}, r.get("proc_end") or {}
    return {"pss_idle_kib": i.get("pss_kib"), "pss_end_kib": e.get("pss_kib"), "rss_idle_kib": i.get("rss_kib"),
            "rss_end_kib": e.get("rss_kib"), "threads": e.get("threads"), "cpu_s": e.get("cpu_s"),
            "init_ms": r.get("init_ms")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", type=Path, default=ROOT / "experiments/results/robotmcp_smoke_20261007")
    ap.add_argument("--baseline", type=Path, default=ROOT / "experiments/results/robot_smoke_20261007")
    a = ap.parse_args()
    tok = load_tokenizer("o200k_base")
    pb = default_pricebook()
    runs = {d.name: load(d) for d in sorted(a.run.iterdir()) if (d / "probe.json").exists() and (d / "tools.json").exists()}
    base = {k: load(a.baseline / k) for k in ("rosmcp_generic_sbc", "rosmcp_compact_sbc", "rosmcp_camera_compact_sbc",
                                              "rosmcp_camera_generic_sbc")}
    go = runs["compact_sbc"]
    tools = {"robot-mcp": oa_tools(go["_tools"]), "prototype": oa_tools(base["rosmcp_compact_sbc"]["_tools"]),
             "ros-mcp": oa_tools(base["rosmcp_generic_sbc"]["_tools"])}
    rep: dict = {"tokenizer": tok.name, "price": pb.provenance(), "runs": sorted(runs)}

    # --- каталог
    cat = []
    for name, src in (("robot-mcp", go), ("prototype", base["rosmcp_compact_sbc"]), ("ros-mcp", base["rosmcp_generic_sbc"])):
        t = oa_tools(src["_tools"])
        cat.append({"server": name, "n_tools": src["n_tools"], "tools_list_bytes": src["tools_list_bytes"],
                    "cookbook": count_tools(t, tok), "json": count_tools(t, tok, "json")})
    import subprocess
    binp = ROOT / "robotmcp/.build/robot-mcp"
    if binp.exists():   # вариант каталога без look(place) — флаг -look-place=false
        for flag, label in (("-look-place=true", "robot-mcp (сборка сейчас)"), ("-look-place=false", "robot-mcp -look-place=false")):
            tl = json.loads(subprocess.run([str(binp), "-print-tools", flag], capture_output=True, text=True,
                                           check=True).stdout)["tools"]
            t = oa_tools(tl)
            cat.append({"server": label, "n_tools": len(tl),
                        "tools_list_bytes": len(json.dumps({"tools": tl}, separators=(",", ":"))),
                        "cookbook": count_tools(t, tok), "json": count_tools(t, tok, "json")})
    rep["catalog"] = cat

    # --- процесс на плате
    pr = [{"server": "robot-mcp (Go)", **proc(go)}, {"server": "robot-mcp, повтор", **proc(runs["compact_sbc_r2"])},
          {"server": "robot-mcp, цикл 6×(2 поездки + кадр)", **proc(runs["soak_sbc"])},
          {"server": "прототип R-compact (Python)", **proc(base["rosmcp_compact_sbc"])},
          {"server": "ros-mcp-server 3.1.2", **proc(base["rosmcp_generic_sbc"])}]
    rep["process"] = pr
    soak = [s.get("server_pss_kib") for s in runs["soak_sbc"]["steps"] if s["step"].endswith("status_dock")]
    rep["soak_pss_kib"] = soak

    # --- вызовы: текст, токены, трафик
    def rows_of(r: dict) -> list[dict]:
        out = []
        for s in r["steps"]:
            if s.get("tool") == "resources/read":
                continue
            t = step_tokens(s, tok)
            out.append({"step": s["step"], "tool": s["tool"], "ms": s["ms"], "text_bytes": s.get("text_bytes"),
                        "tokens": t["text_tokens"], "vision_4.1mini": t["vision"].get("gpt-4.1-mini"),
                        "rb_down": s["rb_down"], "rb_up": s["rb_up"], "event_ms": s.get("event_ms"),
                        "event_bytes": s.get("event_bytes"), "moved_m": s.get("moved_m"),
                        "err_m": s.get("err_to_target_m"), "cpu_ms": s.get("server_cpu_ms"),
                        "stop_ms": s.get("stop_ms"), "coast_m": s.get("coast_m"), "head": s.get("text_head", "")[:200]})
        return out
    rep["calls"] = {k: rows_of(r) for k, r in runs.items()}

    # --- трафик на поездку
    def step(r, name):
        return next((s for s in r["steps"] if s["step"] == name), None)
    trips = []
    for label, r, name in (("robot-mcp, Mac", runs["compact_mac"], "go_door"),
                           ("robot-mcp, sbc", go, "go_door"),
                           ("robot-mcp, sbc, повтор", runs["compact_sbc_r2"], "go_door"),
                           ("robot-mcp «жадный» (скан 2 с + каждая поза), sbc", runs["eager_sbc"], "go_door"),
                           ("robot-mcp «жадный», Mac", runs["compact_mac_eager"], "go_door"),
                           ("прототип R-compact, sbc", base["rosmcp_compact_sbc"], "go_east"),
                           ("ros-mcp send_action_goal, sbc", base["rosmcp_generic_sbc"], "nav_east")):
        s = step(r, name)
        if s:
            b = s["rb_up"] + s["rb_down"]
            trips.append({"variant": label, "bytes": b, "moved_m": s.get("moved_m"),
                          "bytes_per_m": round(b / s["moved_m"]) if s.get("moved_m") else None,
                          "s": round((s.get("event_ms") or s["ms"]) / 1000, 1), "err_m": s.get("err_to_target_m")})
    for label, r, name in (("robot-mcp patrol corridor→dock, sbc", go, "patrol"),
                           ("robot-mcp patrol, sbc, повтор", runs["compact_sbc_r2"], "patrol"),
                           ("robot-mcp patrol, Mac", runs["compact_mac"], "patrol"),
                           ("прототип patrol north→dock, sbc", base["rosmcp_compact_sbc"], "patrol")):
        s = step(r, name)
        if s:
            b = s["rb_up"] + s["rb_down"]
            trips.append({"variant": label, "bytes": b, "moved_m": s.get("moved_m"),
                          "bytes_per_m": None, "s": round((s.get("event_ms") or s["ms"]) / 1000, 1),
                          "err_m": s.get("err_to_target_m")})
    rep["trips"] = trips
    soak_trips = [s["rb_up"] + s["rb_down"] for s in runs["soak_sbc"]["steps"] if s["step"].endswith(("go_door", "go_dock"))]
    rep["soak_trip_bytes"] = {"n": len(soak_trips), "median": st.median(soak_trips), "min": min(soak_trips),
                              "max": max(soak_trips)}
    idle = runs["idle_sbc"]["steps"][0]
    rep["idle"] = {"seconds": idle.get("post_wait_s"), "bytes": (idle.get("rb_up_after_wait") or 0) + (idle.get("rb_down_after_wait") or 0)}

    # --- камера
    def cam(r):
        fr = [s for s in r["steps"] if s["step"].startswith("frame_")]
        ok = [s for s in fr if s.get("images")]
        ims = [s["images"][0] for s in ok]
        return {"n": len(fr), "ok": len(ok), "ms_p50": round(st.median([s["ms"] for s in ok])) if ok else None,
                "ms_range": [round(min(s["ms"] for s in ok)), round(max(s["ms"] for s in ok))] if ok else None,
                "image_bytes_p50": round(st.median([i["bytes"] for i in ims])) if ims else None,
                "wh": f"{ims[0]['w']}x{ims[0]['h']}" if ims else None,
                "rb_down_p50": round(st.median([s["rb_down"] for s in fr])),
                "text_tokens_p50": st.median([tok.count((r["_dir"] / "steps" / s["text_file"]).read_text()) for s in fr]),
                "cpu_ms_total": sum(s.get("server_cpu_ms") or 0 for s in fr)}
    rep["camera"] = {"robot-mcp": cam(runs["camera_sbc"]), "prototype": cam(base["rosmcp_camera_compact_sbc"]),
                     "ros-mcp": cam(base["rosmcp_camera_generic_sbc"])}

    # --- уведомления
    notes = [n for k in ("compact_sbc", "compact_sbc_r2", "soak_sbc") for n in runs[k]["notifications"]]
    rep["notifications"] = {"n": len(notes), "bytes_p50": st.median(n["bytes"] for n in notes),
                            "state_tokens_p50": st.median(tok.count(json.dumps(n["state"], ensure_ascii=False,
                                                                               separators=(",", ":"), sort_keys=True))
                                                          for n in notes),
                            "events": sorted({n["event"] for n in notes})}

    # --- задачи оракулом
    by_go = {s["step"]: s for s in go["steps"]}
    by_p = {s["step"]: s for s in base["rosmcp_compact_sbc"]["steps"]}
    by_g = {s["step"]: s for s in base["rosmcp_generic_sbc"]["steps"]}
    cam_ok = next((s for s in base["rosmcp_camera_generic_sbc"]["steps"] if s.get("images")), None)
    if cam_ok:
        by_g["camera_jpeg_ok"] = cam_ok
    tasks_def = [
        ("«Где ты?»", ["status"], ["status"], ["amcl_pose"], False),
        ("«Что впереди?»", ["status"], ["status"], ["scan"], False),
        ("«Посмотри камерой»", ["look_image"], ["look_image"], ["camera_jpeg_ok"], False),
        ("«Поезжай к двери»", ["go_door"], ["go_east"], ["nav_east"], True),
        ("«Обход: коридор, база»", ["patrol"], ["patrol"], ["nav_east", "nav_east"], True),
        ("«Стоп»", ["go_window_stop"], ["stop"], ["cmd_vel_stop"], False),
    ]
    tasks = []
    for title, gs, ps, rs, async_ in tasks_def:
        row = {"task": title}
        for var, names, by in (("robot-mcp", gs, by_go), ("prototype", ps, by_p), ("ros-mcp", rs, by_g)):
            steps = [dict(by[n]) for n in names if n in by]
            if len(steps) != len(names):
                continue
            if var == "robot-mcp" and title == "«Стоп»":   # шаг stop внутри go_window_stop
                s0 = steps[0]
                tx = next((json.loads(l) for l in (go["_dir"] / "trace.jsonl").read_text().splitlines()
                           if '"name":"stop"' in l and '"type":"tool_call"' in l), {})
                steps = [{"tool": "stop", "args": {}, "_text": s0.get("stop_text", ""), "rb_up": tx.get("rb_tcp_tx", 0),
                          "rb_down": tx.get("rb_tcp_rx", 0), "ms": s0.get("stop_ms") or 0, "images": []}]
            res = simulate(steps, tools[var], tok, pb)
            res["rb_bytes"] = int(sum(s["rb_up"] + s["rb_down"] for s in steps))
            if var == "robot-mcp" and async_:
                w = wake_on_event(by_go[gs[0]].get("event_state") or {}, tools[var], tok, pb)
                res["wake"] = w
                res["llm_calls"] += 1
                res["input_tokens"] += w["input_tokens"]
                res["output_tokens"] += w["output_tokens"]
                res["rub"] = rub(res["input_tokens"], res["output_tokens"], res["vision_tokens"], pb)
            row[var] = res
        tasks.append(row)
    rep["tasks"] = tasks

    out = a.run / "analysis"
    out.mkdir(exist_ok=True)
    (out / "analysis.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str))

    # --- summary.md
    md = ["# robot-mcp (Go) против прототипа R-compact и ros-mcp-server — smoke 07.10.2026", "",
          "Smoke при параллельной работе других агентов: порядок величин, не результаты (IMPLEMENTATION_SPEC §0 п. 4).",
          f"Токены — {tok.name} (Cookbook), ₽ — {pb.provenance() if isinstance(pb.provenance(), str) else 'прайс bench/costmodel'}.", "",
          "## Каталог", ""]
    md.append(md_table([[c["server"], c["n_tools"], c["tools_list_bytes"], c["cookbook"], c["json"]] for c in cat],
                       ["сервер", "инстр.", "tools/list, Б", "каталог, ток. (Cookbook)", "ток. (JSON)"]))
    md += ["", "## Процесс на «плате» (VM sbc, linux/arm64)", ""]
    md.append(md_table([[p["server"], p["pss_idle_kib"], p["pss_end_kib"], p["rss_end_kib"], p["threads"], p["cpu_s"],
                         p["init_ms"]] for p in pr],
                       ["сервер", "PSS простой, КиБ", "PSS конец, КиБ", "RSS конец, КиБ", "потоки", "CPU за сценарий, с",
                        "старт до initialize, мс"]))
    md += ["", f"PSS по циклам (6 × поездка туда-обратно + кадр), КиБ: {soak}", "",
           "## Вызовы robot-mcp с «платы» (compact_sbc)", ""]
    md.append(md_table([[c["step"], c["tool"], c["ms"], c["event_ms"], c["text_bytes"], c["tokens"], c["vision_4.1mini"],
                         c["rb_down"], c["rb_up"], c["moved_m"], c["err_m"], c["head"]] for c in rep["calls"]["compact_sbc"]],
                       ["шаг", "инструмент", "мс вызова", "мс до task_end", "текст, Б", "ток.", "vision 4.1-mini",
                        "rb↓, Б", "rb↑, Б", "сдвиг, м", "до цели, м", "ответ"]))
    md += ["", "## Трафик rosbridge на поездку", ""]
    md.append(md_table([[t["variant"], t["bytes"], t["moved_m"], t["bytes_per_m"], t["s"], t["err_m"]] for t in trips],
                       ["вариант", "байт", "путь по истине, м", "байт/м", "с", "до цели, м"]))
    md += ["", f"Цикл (12 поездок door↔dock): байт на поездку {rep['soak_trip_bytes']}. "
               f"Простой {rep['idle']['seconds']} с: {rep['idle']['bytes']} байт rosbridge.", "",
           "## Кадр по запросу (10 попыток, пауза 6 с, ленивая камера)", ""]
    md.append(md_table([[k, v["n"], v["ok"], v["ms_p50"], v["ms_range"], v["wh"], v["image_bytes_p50"], v["rb_down_p50"],
                         v["text_tokens_p50"], v["cpu_ms_total"]] for k, v in rep["camera"].items()],
                       ["сервер", "попыток", "кадров", "мс p50", "мс мин–макс", "кадр", "JPEG, Б", "rb↓ p50, Б",
                        "текст, ток.", "CPU сервера за 10, мс"]))
    nt = rep["notifications"]
    md += ["", f"Уведомления resources/updated: {nt['n']} шт. ({', '.join(nt['events'])}), {nt['bytes_p50']} Б "
               f"(JSON-RPC params, медиана), состояние в _meta — {nt['state_tokens_p50']} ток.", "",
           "## Задачи оракулом (ReAct без кэша; у асинхронных — +1 пробуждение по task_end)", ""]
    rows = []
    for t in tasks:
        for var in ("robot-mcp", "prototype", "ros-mcp"):
            s = t.get(var)
            if s:
                rows.append([t["task"], var, s["llm_calls"], s["input_tokens"], s["vision_tokens"].get("gpt-4.1-mini"),
                             s["output_tokens"], s["rub"]["gpt-4.1-mini"], s["rub"]["gpt-5-mini"], s["rb_bytes"]])
    md.append(md_table(rows, ["задача", "сервер", "запросов LLM", "вход, ток.", "vision 4.1-mini", "выход, ток.",
                              "₽ 4.1-mini", "₽ 5-mini", "rosbridge, Б"]))
    pz = {k: runs[k] for k in ("paused_nostep", "paused_simstep") if k in runs}
    if pz:
        md += ["", "## Мир на паузе (стенд --rtf 0 --world-control, пауза перед чтениями)", ""]
        md.append(md_table([[k, s["step"], s["ms"], s["rb_down"], (s.get("text_head") or "")[:120]]
                            for k, r in pz.items() for s in r["steps"]], ["режим", "шаг", "мс", "rb↓, Б", "ответ"]))
    (out / "summary.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
