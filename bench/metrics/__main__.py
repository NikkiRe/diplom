"""CLI метрик прогона.

    python -m bench.metrics experiments/results/<run_id>               # metrics.json в каталог прогона + таблица
    python -m bench.metrics RUN --out /tmp/m.json --model gpt-4o-mini --trace other_trace.jsonl
    python -m bench.metrics RUN --json                                 # JSON в stdout вместо таблицы
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compute import MetricsConfig, compute_metrics, summary_table


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m bench.metrics", description="метрики прогона «работы во времени»")
    p.add_argument("run_dir", help="каталог прогона (bench/replay)")
    p.add_argument("--out", help="куда записать metrics.json (по умолчанию <run_dir>/metrics.json)")
    p.add_argument("--json", action="store_true", help="печатать JSON вместо таблицы")
    p.add_argument("--model", help="модель для ₽ (по умолчанию — из трассы/журнала)")
    p.add_argument("--price-table", help="путь к price_table.py (по умолчанию research/new_topic/F_artifacts)")
    for key in ("trace", "journal", "sysmon", "day", "replay", "manifest"):
        p.add_argument(f"--{key}", help=f"явный путь: {key}")
    p.add_argument("--monitor", action="append", help="отчёт монитора JSON (повторяемый)")
    p.add_argument("--gate", action="append", help="трасса шлюза hw-mcp/sim-mcp JSONL (повторяемый)")
    p.add_argument("--answer-abs-tol", type=float, default=1e-6)
    p.add_argument("--slope-delta", type=float, default=1.0, help="граница TOST на наклоне, МиБ/ч")
    p.add_argument("--first-hour-s", type=float, default=3600.0)
    p.add_argument("--block-bootstrap", action="store_true", help="добавить ДИ наклона блочным бутстрепом (медленно)")
    a = p.parse_args(argv)
    run_dir = Path(a.run_dir)
    if not run_dir.is_dir():
        print(f"metrics: not a directory: {run_dir}", file=sys.stderr)
        return 2
    cfg = MetricsConfig(model=a.model, price_table=a.price_table, answer_abs_tol=a.answer_abs_tol,
                        slope_delta_mib_h=a.slope_delta, first_hour_s=a.first_hour_s,
                        block_bootstrap=a.block_bootstrap)
    overrides = {k: getattr(a, k) for k in ("trace", "journal", "sysmon", "day", "replay", "manifest", "monitor",
                                            "gate")}
    m = compute_metrics(run_dir, cfg, overrides)
    out = Path(a.out) if a.out else run_dir / "metrics.json"
    out.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if a.json:
        json.dump(m, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print(summary_table(m))
        print(f"\nmetrics.json → {out}")
        for w in m["warnings"]:
            print(f"предупреждение: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
