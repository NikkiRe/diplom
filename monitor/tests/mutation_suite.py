"""Мутационное тестирование монитора: каждый мутант применяется к каждому прогону корпуса, к которому он
применим; обнаружение засчитывается, только если среди нарушений есть один из ожидаемых классов.

    .venv/bin/python -m monitor.tests.mutation_suite            # таблица markdown
    .venv/bin/python -m monitor.tests.mutation_suite --json     # то же в JSON
    .venv/bin/python -m monitor.tests.mutation_suite --seeds 20 # больший корпус
"""
from __future__ import annotations

import argparse
import json
import random
import zlib
from collections import Counter
from dataclasses import dataclass, field

from monitor.check_trace import check
from monitor.tests import tracegen
from monitor.tests.mutants import MUTANTS, Mutant, clone


@dataclass
class Stats:
    mutant: Mutant
    applied: int = 0
    detected: int = 0
    extra: Counter = field(default_factory=Counter)
    missed: list = field(default_factory=list)


def run_one(sc) -> dict:
    return check(sc.trace_lines(), sc.gate_lines(), sc.tools, sc.declared).by_class()


def run(corpus=None, mutants=None) -> list[Stats]:
    corpus = tracegen.corpus(8) if corpus is None else corpus
    out = []
    for m in mutants or MUTANTS:
        st = Stats(m)
        for sc in corpus:
            rng = random.Random(zlib.crc32(f"{m.name}/{sc.name}".encode()))
            mutated = m.apply(clone(sc), rng)
            if mutated is None:
                continue
            st.applied += 1
            classes = run_one(mutated)
            if any(e in classes for e in m.expected):
                st.detected += 1
            else:
                st.missed.append((sc.name, classes))
            st.extra.update(c for c in classes if c not in m.expected)
        out.append(st)
    return out


def table(stats: list[Stats]) -> str:
    lines = ["| # | мутант | цель | ожидаемый класс | применён | обнаружен | попутно |",
             "|---|---|---|---|---|---|---|"]
    for i, st in enumerate(stats, 1):
        extra = ", ".join(f"{c}×{n}" for c, n in st.extra.most_common(3))
        lines.append(f"| {i} | `{st.mutant.name}` — {st.mutant.description} | {st.mutant.target} | "
                     f"`{' / '.join(st.mutant.expected)}` | {st.applied} | {st.detected} | {extra} |")
    total_a = sum(s.applied for s in stats)
    total_d = sum(s.detected for s in stats)
    lines.append(f"| | **итого: {len(stats)} мутантов** | | | {total_a} | {total_d} | |")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--seeds", type=int, default=8)
    args = ap.parse_args(argv)
    stats = run(tracegen.corpus(args.seeds))
    if args.json:
        print(json.dumps([{"mutant": s.mutant.name, "expected": s.mutant.expected, "applied": s.applied,
                           "detected": s.detected, "extra": dict(s.extra), "missed": s.missed[:3]} for s in stats],
                         ensure_ascii=False, indent=1))
    else:
        print(table(stats))
    bad = [s for s in stats if s.applied == 0 or s.detected < s.applied]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
