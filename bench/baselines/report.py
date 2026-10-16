"""Сводная таблица по нескольким прогонам (summary.json в подкаталогах).

    python -m bench.baselines.report bench/baselines/smoke/20261007 [--out table.md]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _mib(v) -> str:
    return "—" if v is None else f"{v / 1024:.1f}"


def rows(root: Path) -> list[dict]:
    out = []
    for p in sorted(root.glob("*/summary.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        d = s["llm"]["by_phase"].get("day") or {}
        src = s["llm"]["day_by_source"]
        r = (s["resources"].get("day") or {})
        out.append({
            "run": p.parent.name, "system": s["system"], "profile": s["profile"], "host": s["host"], "k": s["k"],
            "hours": s["day"]["hours"], "calls": d.get("calls", 0),
            "by_source": {k: v["calls"] for k, v in sorted(src.items())},
            "prompt": d.get("prompt_tokens", 0), "cached": d.get("cached_tokens", 0),
            "completion": d.get("completion_tokens", 0), "bytes_up": d.get("bytes_up", 0),
            "tools_max": d.get("tools_in_request_max", 0),
            "rss_med": (r.get("rss_kib") or {}).get("median"), "rss_max": (r.get("rss_kib") or {}).get("max"),
            "root_med": (r.get("root_rss_kib") or {}).get("median"), "pss_med": (r.get("pss_kib") or {}).get("median"),
            "user_ok": f"{s['user']['ok']}/{s['user']['sent']}", "push": s["push"]["sent"],
            "cmd_rate": ((s.get("sim_eval") or {}).get("commands") or {}).get("rate"),
            "deviations": len(s.get("deviations") or []), "errors": len(s.get("errors") or []),
        })
    return out


def table(rs: list[dict]) -> str:
    head = ("| прогон | система | профиль | хост | вызовы LLM | по источникам | prompt | cached | completion | "
            "байт ↑ LLM | tools | RSS дерева мед./пик, МиБ | RSS корня мед., МиБ | PSS мед., МиБ | реплики | пуш | "
            "команды ok | откл. |")
    sep = "|" + "---|" * 18
    lines = [head, sep]
    for r in rs:
        src = ", ".join(f"{k} {v}" for k, v in r["by_source"].items())
        lines.append(f"| {r['run']} | {r['system']} | {r['profile']} | {r['host']} | {r['calls']} | {src} | "
                     f"{r['prompt']} | {r['cached']} | {r['completion']} | {r['bytes_up']} | {r['tools_max']} | "
                     f"{_mib(r['rss_med'])} / {_mib(r['rss_max'])} | {_mib(r['root_med'])} | {_mib(r['pss_med'])} | "
                     f"{r['user_ok']} | {r['push']} | {r['cmd_rate']} | {r['deviations']} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.baselines.report")
    ap.add_argument("root")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    rs = rows(Path(a.root))
    t = table(rs)
    if a.out:
        Path(a.out).write_text(t, encoding="utf-8")
        Path(a.out).with_suffix(".json").write_text(json.dumps(rs, ensure_ascii=False, indent=1), encoding="utf-8")
    print(t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
