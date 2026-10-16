"""Самопроверка дня: «идеальный агент» исполняет эталонные вызовы дня прямо в sim-mcp (без HTTP).

Для согласованного дня оценщик sim-mcp должен дать 100 % по командам, вопросам и ситуациям с действиями,
а ни одна эталонная команда не должна быть выполненной заранее (pre_satisfied). Это проверка daygen
(предусловия, ответы, таймеры устройств) и sim-mcp (проигрывание, валидация, оценка) друг против друга.

  python -m bench.simmcp.selfcheck day.jsonl [--catalog explicit]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from bench.daygen.generator import read_day

from .server import SimMCP


async def oracle_replay(header: dict, events: list[dict], catalog: str = "generic") -> dict:
    sim = SimMCP(header, events, catalog=catalog)
    key_calls = "calls" if catalog == "generic" else "calls_explicit"
    key_actions = "actions" if catalog == "generic" else "actions_explicit"
    denied = []
    for ev in sorted(events, key=lambda e: e["t"]):
        if ev["kind"] == "sensor":
            continue
        await sim.advance(ev["t"])
        if ev["kind"] != "situation":
            sim.evaluator.set_current(ev["id"], sim.world.clock)   # как POST /control/input у реплеера
        calls = ev["expect"].get(key_actions if ev["kind"] == "situation" else key_calls, [])
        for c in calls:
            res = await sim.call(c["name"], c["args"])
            if res.isError:
                denied.append({"input": ev["id"], "call": c, "result": json.loads(res.content[0].text)})
    await sim.advance(header["window_s"][1])
    rep = sim.finish()
    rep["oracle_denied"] = denied
    rep["pre_satisfied"] = [r["id"] for r in rep["inputs"] if r.get("pre_satisfied")]
    return rep


def is_consistent(rep: dict) -> bool:
    s = rep["summary"]
    full = all(v["rate"] in (None, 1.0) for v in (s["commands"], s["questions"], s["situations_with_actions"]))
    return full and not rep["oracle_denied"] and not rep["pre_satisfied"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.simmcp.selfcheck")
    ap.add_argument("days", nargs="+")
    ap.add_argument("--catalog", default="generic", choices=["generic", "explicit"])
    a = ap.parse_args(argv)
    bad = 0
    for path in a.days:
        header, events = read_day(path)
        rep = asyncio.run(oracle_replay(header, events, a.catalog))
        ok = is_consistent(rep)
        bad += not ok
        print(f"{'OK ' if ok else 'FAIL'} {path}: {json.dumps(rep['summary'], ensure_ascii=False)}")
        if not ok:
            print(json.dumps({"denied": rep["oracle_denied"], "pre_satisfied": rep["pre_satisfied"],
                              "failed": [r for r in rep["inputs"] if r.get("ok") is False]},
                             ensure_ascii=False, indent=1), file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
