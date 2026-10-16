#!/usr/bin/env python3
"""Эталон для оценщика токенов robot-mcp (internal/tokens): точный o200k_base (tiktoken, словарь — локальная
копия research/new_topic/GAP2_artifacts/.cache/tk через bench/mockllm/tokens.py) на корпусе текстов, которые
адаптер реально отдаёт в LLM или которые ходят рядом: ответы R-compact и ros-mcp из smoke 07.10, каталоги
инструментов, русские реплики. Пишет robotmcp/testdata/tokens_golden.json; Go-тест сверяет оценку с ним.

  .venv/bin/python robotmcp/tools/gen_token_golden.py [--extra DIR ...]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True          # bench/ только читается: без __pycache__
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bench.mockllm.tokens import load_tokenizer  # noqa: E402

SMOKE = ROOT / "experiments/results/robot_smoke_20261007"
RU = [
    "подъедь к двери", "что в коридоре?", "с 9 до 18 каждый час объезжай коридор, дверь и окно, потом на базу",
    "Готово: робот на месте east, препятствий рядом нет.", "робот: место=дверь 0.1 м, навигация=стоит, заряд=75 %",
    "Мобильный робот TurtleBot3. Места (x,y в м, карта): dock(-2,-0.5), east(1.8,0.5).",
    "Внимание: заряд робота 20 %, возвращаюсь на базу.",
]


def corpus(extra: list[Path]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for d in sorted(SMOKE.glob("rosmcp_*_sbc")) + extra:
        for f in sorted((d / "steps").glob("*.txt")):
            t = f.read_text()
            if t and len(t) < 20_000:
                out.append((f"{d.name}/{f.name}", t))
        tj = d / "tools.json"
        if tj.exists():
            for t in json.loads(tj.read_text()):
                out.append((f"{d.name}/tool:{t['name']}", json.dumps(t, ensure_ascii=False, separators=(",", ":"))))
                out.append((f"{d.name}/desc:{t['name']}", f"{t['name']}:{t.get('description', '')}"))
    out += [(f"ru:{i}", s) for i, s in enumerate(RU)]
    seen, uniq = set(), []
    for k, t in out:
        if t not in seen:
            seen.add(t)
            uniq.append((k, t))
    return uniq


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extra", nargs="*", type=Path, default=[], help="каталоги прогонов robot-probe (steps/, tools.json)")
    ap.add_argument("--out", type=Path, default=ROOT / "robotmcp/testdata/tokens_golden.json")
    a = ap.parse_args()
    tok = load_tokenizer()
    if tok.name != "o200k_base":
        print(f"нет o200k ({tok.name}) — эталон не пишется", file=sys.stderr)
        return 1
    rows = [{"name": k, "text": t, "o200k": tok.count(t)} for k, t in corpus(a.extra)]
    a.out.write_text(json.dumps({"tokenizer": tok.name, "items": rows}, ensure_ascii=False, indent=0) + "\n")
    print(f"{len(rows)} текстов, {sum(r['o200k'] for r in rows)} токенов → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
