"""CLI costmodel.

    python -m bench.costmodel h1 [--period-min 30] [--gate-wakes 4] [--window 07:00-23:00] [--json]
    python -m bench.costmodel history [--R 10] [--hours 8]        # таблица как у D_artifacts/cost_model.py
    python -m bench.costmodel prices [--model gpt-4o-mini]
    python -m bench.costmodel prereg [--out prereg_forecasts.json] [--model openai/gpt-4o-mini]
"""
from __future__ import annotations

import argparse
import json
import sys

from .forecast import H1Params, h1_table, prereg_forecasts
from .history_model import Workload, legacy_report
from .prices import PriceBook


def _hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 3600 + int(m) * 60


def _window(s: str) -> float:
    a, b = s.split("-")
    return _hhmm(b) - _hhmm(a)


def _cmd_h1(a: argparse.Namespace) -> int:
    res = h1_table(H1Params(window_s=_window(a.window), heartbeat_period_s=a.period_min * 60,
                            gate_wakes=a.gate_wakes))
    if a.json:
        json.dump(res, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0
    p = res["params"]
    print(f"H1: окно {a.window}, heartbeat {a.period_min:g} мин → N_hb = {p['heartbeat_ticks']}, "
          f"N_gate = {p['gate_wakes']:g}, E[k] = {p['expected_k']:.2f}")
    print(f"{'профиль':<8}{'команд/сут':>11}{'вызовы hb':>11}{'вызовы гейт':>13}{'ρ hb/гейт':>11}{'фон hb':>9}{'фон гейт':>10}")
    for r in res["rows"]:
        print(f"{r['profile']:<8}{r['commands_per_day']:>11.2f}{r['calls_heartbeat']:>11.1f}{r['calls_gate']:>13.1f}"
              f"{r['ratio_hb_to_gate']:>11.2f}{100 * r['background_share_heartbeat']:>8.0f}%"
              f"{100 * r['background_share_gate']:>9.0f}%")
    print("Безубыточность (команд/сут, при которых ρ падает до порога): " +
          ", ".join(f"ρ={k}: {v:.1f}" for k, v in res["breakeven_commands_per_day"].items()))
    return 0


def _cmd_history(a: argparse.Namespace) -> int:
    w = Workload(hours=a.hours, R=a.R)
    print(w)
    print(legacy_report(w))
    return 0


def _cmd_prices(a: argparse.Namespace) -> int:
    book = PriceBook.load(a.price_table)
    models = [a.model] if a.model else book.models()
    print(f"{'модель':<28}{'вход ₽/1M':>11}{'кэш ₽/1M':>10}{'выход ₽/1M':>12}")
    for m in models:
        pm = book.price(m).per_million()
        cached = "—" if pm["cached"] is None else f"{pm['cached']:.2f}"
        print(f"{book.price(m).model:<28}{pm['input']:>11.2f}{cached:>10}{pm['output']:>12.2f}")
    print(json.dumps(book.provenance(), ensure_ascii=False))
    return 0


def _cmd_prereg(a: argparse.Namespace) -> int:
    res = prereg_forecasts(PriceBook.load(a.price_table), model=a.model)
    text = json.dumps(res, ensure_ascii=False, indent=2) + "\n"
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"written {a.out}")
    else:
        sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m bench.costmodel", description="прогнозы затрат и ₽ по прайсу")
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("h1", help="отношение вызовов heartbeat/гейт по профилям (H1)")
    h.add_argument("--period-min", type=float, default=30.0)
    h.add_argument("--gate-wakes", type=float, default=4.0)
    h.add_argument("--window", default="07:00-23:00")
    h.add_argument("--json", action="store_true")
    hi = sub.add_parser("history", help="порт D_artifacts/cost_model.py: политики истории × TTL")
    hi.add_argument("--R", type=int, default=40, help="запросов пользователя в сутки")
    hi.add_argument("--hours", type=float, default=8)
    pr = sub.add_parser("prices", help="замороженный прайс RouterAI")
    pr.add_argument("--model")
    pr.add_argument("--price-table")
    pg = sub.add_parser("prereg", help="все прогнозы для предрегистрации (JSON)")
    pg.add_argument("--out")
    pg.add_argument("--model", default="openai/gpt-4o-mini")
    pg.add_argument("--price-table")
    a = p.parse_args(argv)
    return {"h1": _cmd_h1, "history": _cmd_history, "prices": _cmd_prices, "prereg": _cmd_prereg}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
