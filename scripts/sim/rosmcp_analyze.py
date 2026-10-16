#!/usr/bin/env python3
"""Разбор прогонов rosmcp_probe.py: токены каталога и ответов, ₽, трафик rosbridge, задержки; сравнение
R-generic (ros-mcp-server) и R-compact (robot_mcp_compact.py) на одних и тех же задачах.

  .venv/bin/python scripts/sim/rosmcp_analyze.py --generic DIR_G --compact DIR_C [--camera-generic DIR]
        [--camera-compact DIR] --out DIR

Токены — o200k_base через bench/mockllm/tokens.py (как mock-llm: Cookbook-оценка запроса, инструменты —
num_tokens_for_tools); ₽ — bench/costmodel/prices.py (замороженный прайс RouterAI), без кэша.
Задачи считаются «оракулом» (как T0): минимальная последовательность вызовов, которую сделала бы идеальная
модель; каждый вызов инструмента — отдельный запрос к LLM (ReAct), результат вызова остаётся в истории хода,
в конце — короткий ответ пользователю. Результаты вызовов — фактические ответы серверов из прогона.
Кадр камеры в Chat Completions идёт отдельной частью-картинкой: его vision-токены считаются по правилам OpenAI
(topic_tokens.vision_tokens) и добавляются к входу.
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench.costmodel.prices import default_pricebook  # noqa: E402
from bench.mockllm.tokens import account_request, count_completion, count_tools, load_tokenizer  # noqa: E402
from topic_tokens import vision_tokens  # noqa: E402

MODELS = {"gpt-4.1-mini": "openai/gpt-4.1-mini", "gpt-5-mini": "openai/gpt-5-mini"}
VISION_KEY = {"gpt-4.1-mini": "gpt-4.1-mini patch", "gpt-5-mini": "gpt-5-mini patch"}
SYSTEM = ("Ты агент мобильного робота TurtleBot3 на одноплатном компьютере. Выполняй команды пользователя "
          "инструментами, отвечай кратко по-русски. Места на карте (x, y в м, кадр map): dock(-2,-0.5), "
          "east(1.8,0.5), east_south(1.8,-0.5), north(0,1.8), south(0.5,-1.7), north_east(1.6,1.6), "
          "center_ne(0.55,0.55), center_sw(-0.55,-0.55).")
FINAL_REPLY = "Готово: робот на месте east, препятствий рядом нет."   # ≈ типичный короткий ответ

# Задача → шаги прогона (минимальная последовательность «идеальной» модели).
TASKS = [
    ("where", "«Где ты?»", ["amcl_pose"], ["status"]),
    ("ahead", "«Что впереди, свободно ли?»", ["scan"], ["status"]),
    ("go", "«Поезжай к east»", ["nav_east"], ["go_east"]),
    ("look", "«Посмотри камерой»", ["camera_jpeg"], ["look_image"]),
    ("look_ok", "«Посмотри камерой» (кадр пришёл)", ["camera_jpeg_ok"], ["look_image"]),
    ("patrol", "«Обход: north, затем dock»", ["nav_east", "nav_east"], ["patrol"]),
    ("stop", "«Стоп»", ["cmd_vel_stop"], ["stop"]),
]
DISCOVERY = ["get_topics", "get_topic_type_cmd_vel", "msg_details_twiststamped", "get_actions", "action_details_nav"]


def load(d: Path) -> dict:
    r = json.loads((d / "probe.json").read_text())
    r["_dir"] = d
    r["_tools"] = json.loads((d / "tools.json").read_text())
    for s in r["steps"]:
        tf = s.get("text_file")
        s["_text"] = (d / "steps" / tf).read_text() if tf else ""
    return r


def oa_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t.get("inputSchema", {})}} for t in tools]


def step_tokens(s: dict, tok) -> dict:
    out = {"text_tokens": tok.count(s.get("_text", "")), "vision": {}}
    for im in s.get("images") or []:
        if im.get("w"):
            v = vision_tokens(im["w"], im["h"])
            for m, k in VISION_KEY.items():
                out["vision"][m] = out["vision"].get(m, 0) + v[k]
    return out


def describe_run(r: dict, tok) -> dict:
    tools = oa_tools(r["_tools"])
    per_tool = sorted(((t["function"]["name"], count_tools([t], tok) - 12) for t in tools), key=lambda x: -x[1])
    rows = []
    for s in r["steps"]:
        t = step_tokens(s, tok)
        rows.append({
            "step": s["step"], "tool": s["tool"], "ms": s["ms"], "mcp_bytes": s.get("mcp_bytes"),
            "text_bytes": s.get("text_bytes"), "structured_bytes": s.get("structured_bytes"),
            "text_tokens": t["text_tokens"], "vision": t["vision"],
            "image": [f"{i['w']}x{i['h']} {i['bytes']} B" for i in s.get("images") or []],
            "rb_bytes": s["rb_up"] + s["rb_down"], "rb_up": s["rb_up"], "rb_down": s["rb_down"],
            "rb_conns": s["rb_conns"], "server_cpu_ms": s.get("server_cpu_ms"), "moved_m": s.get("moved_m"),
            "err_to_target_m": s.get("err_to_target_m"), "is_error": s.get("is_error"),
            "head": s.get("text_head", "")[:160]})
    return {"dir": str(r["_dir"].relative_to(ROOT)) if r["_dir"].is_relative_to(ROOT) else str(r["_dir"]),
            "host": r["host"], "server": r.get("server_info", {}).get("name"), "protocol": r.get("protocol"),
            "n_tools": r["n_tools"], "tools_list_bytes": r["tools_list_bytes"],
            "catalog_tokens": count_tools(tools, tok), "catalog_json_tokens": count_tools(tools, tok, "json"),
            "top_tools": per_tool[:6], "n_resources": r.get("n_resources"), "n_prompts": r.get("n_prompts"),
            "resources_list_bytes": r.get("resources_list_bytes"), "prompts_list_bytes": r.get("prompts_list_bytes"),
            "init_ms": r.get("init_ms"), "proc_idle": r.get("proc_idle"), "proc_end": r.get("proc_end"),
            "rb_total": r.get("rb_total"), "steps": rows}


def simulate(task_steps: list[dict], tools: list[dict], tok, pb) -> dict:
    """ReAct-оракул: запрос LLM на каждый вызов + финальный ответ; результаты накапливаются в истории хода."""
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "команда пользователя"}]
    inp = out = 0
    vision = {m: 0 for m in MODELS}
    rb = ms = 0.0
    for i, s in enumerate(task_steps):
        body = {"messages": msgs, "tools": tools}
        inp += account_request(body, tok).prompt_tokens
        call = {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"c{i}", "type": "function",
            "function": {"name": s["tool"], "arguments": json.dumps(s["args"], ensure_ascii=False,
                                                                      separators=(",", ":"))}}]}
        out += count_completion(call, tok)
        msgs = msgs + [call, {"role": "tool", "tool_call_id": f"c{i}", "content": s.get("_text", "")}]
        st_ = step_tokens(s, tok)
        for m in MODELS:   # картинка из ответа остаётся в истории: входит во все последующие запросы хода
            vision[m] += st_["vision"].get(m, 0) * (len(task_steps) - i)
        rb += s["rb_up"] + s["rb_down"]
        ms += s["ms"]
    inp += account_request({"messages": msgs, "tools": tools}, tok).prompt_tokens
    out += count_completion({"role": "assistant", "content": FINAL_REPLY}, tok)
    rub = {}
    for m, mid in MODELS.items():
        p = pb.price(mid)
        rub[m] = round((inp + vision[m]) * p.input + out * p.output, 4)
    return {"llm_calls": len(task_steps) + 1, "input_tokens": inp, "vision_tokens": vision, "output_tokens": out,
            "rub": rub, "rb_bytes": int(rb), "tool_ms": round(ms)}


def camera_stats(r: dict) -> dict:
    fr = [s for s in r["steps"] if s["step"].startswith("frame_")]
    ok = [s for s in fr if s.get("images")]
    return {"n": len(fr), "ok": len(ok),
            "ms_ok_p50": round(st.median([s["ms"] for s in ok])) if ok else None,
            "ms_ok_range": [round(min(s["ms"] for s in ok)), round(max(s["ms"] for s in ok))] if ok else None,
            "ms_fail": round(st.median([s["ms"] for s in fr if not s.get("images")])) if len(ok) < len(fr) else None,
            "image_bytes_p50": round(st.median([s["images"][0]["bytes"] for s in ok])) if ok else None,
            "rb_down_p50": round(st.median([s["rb_down"] for s in fr])) if fr else None,
            "conns_p50": st.median([s["rb_conns"] for s in fr]) if fr else None}


def md_table(rows: list[list], head: list[str]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--generic", required=True)
    ap.add_argument("--compact", required=True)
    ap.add_argument("--camera-generic")
    ap.add_argument("--camera-compact")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    tok = load_tokenizer("o200k_base")
    pb = default_pricebook()
    g, c = load(Path(a.generic)), load(Path(a.compact))
    dg, dc = describe_run(g, tok), describe_run(c, tok)
    by = {"generic": {s["step"]: s for s in g["steps"]}, "compact": {s["step"]: s for s in c["steps"]}}
    tools = {"generic": oa_tools(g["_tools"]), "compact": oa_tools(c["_tools"])}
    cam_g = load(Path(a.camera_generic)) if a.camera_generic else None
    if cam_g:   # удачная попытка subscribe_once на камеру (в основном прогоне кадр мог не прийти)
        ok = [s for s in cam_g["steps"] if s.get("images")]
        if ok:
            by["generic"]["camera_jpeg_ok"] = ok[0]
    tasks = []
    for key, title, gs, cs in TASKS:
        row = {"task": key, "title": title}
        for var, names in (("generic", gs), ("compact", cs)):
            steps = [by[var][n] for n in names if n in by[var]]
            row[var] = simulate(steps, tools[var], tok, pb) if len(steps) == len(names) else None
            row[var + "_steps"] = names
        tasks.append(row)
    disc = [by["generic"][n] for n in DISCOVERY if n in by["generic"]]
    discovery = simulate(disc, tools["generic"], tok, pb)
    rep = {"tokenizer": tok.name, "price": pb.provenance(), "generic": dg, "compact": dc, "tasks": tasks,
           "generic_discovery": discovery}
    if cam_g:
        rep["camera_generic"] = camera_stats(cam_g)
    if a.camera_compact:
        rep["camera_compact"] = camera_stats(load(Path(a.camera_compact)))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "analysis.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str))

    md = [f"# R-generic (ros-mcp-server) против R-compact — smoke {Path(a.generic).name} / {Path(a.compact).name}", ""]
    md.append(md_table([[d["server"], d["host"], d["n_tools"], d["tools_list_bytes"], d["catalog_tokens"],
                         d["n_resources"], d["n_prompts"], d["init_ms"],
                         (d["proc_idle"] or {}).get("pss_kib") or (d["proc_idle"] or {}).get("rss_kib"),
                         (d["proc_end"] or {}).get("pss_kib") or (d["proc_end"] or {}).get("rss_kib")]
                        for d in (dg, dc)],
                       ["сервер", "хост", "инстр.", "tools/list, Б", "каталог, ток.", "ресурсы", "промпты",
                        "старт, мс", "PSS простой, КиБ", "PSS конец, КиБ"]))
    for name, d in (("R-generic", dg), ("R-compact", dc)):
        md += ["", f"## {name}: вызовы ({d['dir']})", ""]
        md.append(md_table([[s["step"], s["tool"], s["ms"], s["mcp_bytes"], s["text_bytes"], s["text_tokens"],
                             ", ".join(s["image"]), s["vision"].get("gpt-4.1-mini"), s["rb_down"], s["rb_up"],
                             s["rb_conns"], s["server_cpu_ms"], s["moved_m"], s["err_to_target_m"]]
                            for s in d["steps"]],
                           ["шаг", "инструмент", "мс", "MCP, Б", "текст, Б", "текст, ток.", "кадр",
                            "vision 4.1-mini", "rb↓, Б", "rb↑, Б", "WS", "CPU сервера, мс", "сдвиг, м",
                            "до цели, м"]))
    md += ["", "## Задачи (оракул: минимальная последовательность, ReAct, без кэша)", ""]
    rows = []
    for t in tasks:
        for var in ("generic", "compact"):
            s = t[var]
            if not s:
                continue
            rows.append([t["title"], var, "+".join(t[var + "_steps"]), s["llm_calls"], s["input_tokens"],
                         s["vision_tokens"]["gpt-4.1-mini"], s["output_tokens"], s["rub"]["gpt-4.1-mini"],
                         s["rub"]["gpt-5-mini"], s["rb_bytes"], s["tool_ms"]])
    md.append(md_table(rows, ["задача", "вариант", "вызовы", "LLM", "вход, ток.", "vision (4.1-mini)",
                              "выход, ток.", "₽ 4.1-mini", "₽ 5-mini", "rosbridge, Б", "инструменты, мс"]))
    md += ["", f"Обнаружение для R-generic (get_topics, get_topic_type, get_message_details, get_actions, "
               f"get_action_details — если модель не знает интерфейс робота): {discovery['llm_calls']} запросов LLM, "
               f"{discovery['input_tokens']} входных токенов, ₽ {discovery['rub']}.", ""]
    for k in ("camera_generic", "camera_compact"):
        if k in rep:
            md.append(f"- {k}: {json.dumps(rep[k], ensure_ascii=False)}")
    (out / "summary.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
