#!/usr/bin/env python3
"""Монитор трасс edgectx и журнала шлюза hw-mcp (IMPLEMENTATION_SPEC §6, класс actuator_safety).

CLI:
    python monitor/check_trace.py --trace run/edgectx_trace.jsonl --hw-log run/hwmcp_gate.jsonl \\
        --tools run/tools.json [--gate-config declared.json] [--out run/monitor.json]
Коды выхода: 0 — нарушений нет; 1 — есть нарушения; 2 — ошибка запуска (нет файла, неверный каталог/политика).

Библиотека:
    from monitor.check_trace import check
    report = check(trace="t.jsonl", hw_log="g.jsonl", tools="tools.json")
    report.ok, report.n_unsafe, report.by_class(), report.to_dict()

Монитор не импортирует код edgectx и hw-mcp: правила реализованы заново по спецификации, поэтому ошибка
агента или шлюза не может «согласованно» повториться в проверщике.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable

if __package__ in (None, ""):  # запуск как скрипта: python monitor/check_trace.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.catalog import Catalog  # noqa: E402
from monitor.gate_check import DEFAULT_ACTUATOR_TOOLS, GateChecker, GateOptions  # noqa: E402
from monitor.jsonl import loads_strict, read_lines  # noqa: E402
from monitor.report import Report  # noqa: E402
from monitor.trace_check import TraceChecker, TraceOptions  # noqa: E402

LinesSource = str | Path | Iterable[str | bytes]


def _catalog(tools) -> Catalog | None:
    if tools is None or isinstance(tools, Catalog):
        return tools
    if isinstance(tools, (str, Path)):
        return Catalog.from_file(tools)
    return Catalog.from_obj(tools)


def _declared(gate_config):
    if gate_config is None or isinstance(gate_config, dict):
        return gate_config
    with open(gate_config, encoding="utf-8") as f:
        return loads_strict(f.read())


def check(trace: LinesSource | None = None, hw_log: LinesSource | None = None, tools=None, gate_config=None,
          trace_opts: TraceOptions | None = None, gate_opts: GateOptions | None = None) -> Report:
    """Проверить трассу edgectx и/или журнал шлюза. trace/hw_log — путь или последовательность строк JSONL;
    tools — путь, объект каталога или Catalog; gate_config — путь или объект политики шлюза."""
    report = Report()
    report.inputs = {k: (str(v) if isinstance(v, (str, Path)) else ("<lines>" if v is not None else None))
                     for k, v in (("trace", trace), ("hw_log", hw_log), ("tools", tools if not isinstance(tools, (dict, list, Catalog)) else "<object>"),
                                  ("gate_config", gate_config if not isinstance(gate_config, dict) else "<object>"))}
    calls = readings = None
    if trace is not None:
        tc = TraceChecker(report, _catalog(tools), trace_opts)
        calls = tc.check(read_lines(trace))
        readings = tc.readings
        report.summary["trace"] = tc.summary
    if hw_log is not None:
        gc = GateChecker(report, _declared(gate_config), gate_opts)
        report.summary["gate"] = gc.check(read_lines(hw_log), calls, readings)
    elif trace is not None:
        report.skip("actuator_safety.*: нет журнала шлюза (--hw-log)")
    report.summary["n_unsafe"] = report.n_unsafe
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Проверка трассы edgectx (§6) и журнала шлюза hw-mcp (actuator_safety)")
    ap.add_argument("--trace", help="трасса edgectx (JSONL)")
    ap.add_argument("--hw-log", help="журнал шлюза hw-mcp (JSONL)")
    ap.add_argument("--tools", help="каталог инструментов (JSON): список MCP/OpenAI или {servers: {...}}")
    ap.add_argument("--gate-config", help="declared.json hw-mcp (политика gate и выходы gpio → реестр) или раздел gate")
    ap.add_argument("--out", help="файл отчёта JSON (по умолчанию stdout)")
    ap.add_argument("--max-steps", type=int, help="запросов к LLM на ход (по умолчанию run_start.policy.max_steps)")
    ap.add_argument("--max-in-flight", type=int, help="запросов в полёте (по умолчанию из run_start, иначе 1)")
    ap.add_argument("--daily-calls", type=int, help="суточный лимит вызовов (по умолчанию run_start.budget)")
    ap.add_argument("--daily-input-tokens", type=int, help="суточный лимит входных токенов")
    ap.add_argument("--degrade-at", type=float, help="доля лимита, с которой фон отключается")
    ap.add_argument("--day-s", type=float, default=86400.0, help="длина суток по ts_clock, с")
    ap.add_argument("--strict-integer", action="store_true", help="integer — только целые литералы (1.0 — ошибка)")
    ap.add_argument("--allow-type", action="append", default=[], help="разрешить дополнительный тип записи трассы")
    ap.add_argument("--hw-server", action="append", default=None,
                    help="имя MCP-сервера приводов в tool_call.server (по умолчанию hw, hw-mcp, hwmcp)")
    ap.add_argument("--actuator-tools", default=DEFAULT_ACTUATOR_TOOLS, help="регулярное выражение имён команд приводам")
    ap.add_argument("--watchdog-tolerance-s", type=float, help="допуск сторожевого тика аренды и конца импульса, с")
    ap.add_argument("--match-slack-s", type=float, default=1.0,
                    help="окно сопоставления tool_call трассы и решения шлюза по ts_wall, с")
    ap.add_argument("--no-declared-path", action="store_true",
                    help="не читать declared.json по пути из run_start журнала hw-mcp (реестр — только --gate-config)")
    ap.add_argument("--usage-tolerance", type=float, default=0.5,
                    help="допуск |prompt_tokens / prompt_tokens_est − 1| для предупреждения llm.usage_mismatch")
    ap.add_argument("--max-findings", type=int, default=1000, help="сколько находок каждого вида писать в отчёт")
    ap.add_argument("--quiet", action="store_true", help="не печатать сводку в stderr")
    args = ap.parse_args(argv)
    if not args.trace and not args.hw_log:
        ap.error("нужен --trace и/или --hw-log")

    topts = TraceOptions(max_steps=args.max_steps, max_in_flight=args.max_in_flight, daily_calls=args.daily_calls,
                         daily_input_tokens=args.daily_input_tokens, degrade_at=args.degrade_at, day_s=args.day_s,
                         strict_integer=args.strict_integer, allow_types=frozenset(args.allow_type),
                         usage_tolerance=args.usage_tolerance)
    gopts = GateOptions(watchdog_tolerance_s=args.watchdog_tolerance_s, actuator_tools=args.actuator_tools,
                        match_slack_s=args.match_slack_s, read_declared_path=not args.no_declared_path)
    if args.hw_server:
        gopts.hw_servers = frozenset(args.hw_server)
    try:
        report = check(args.trace, args.hw_log, args.tools, args.gate_config, topts, gopts)
    except (OSError, ValueError) as e:
        print(f"check_trace: {e}", file=sys.stderr)
        return 2
    out = json.dumps(report.to_dict(args.max_findings), ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(out + "\n", encoding="utf-8")
    else:
        print(out)
    if not args.quiet:
        status = "OK" if report.ok else "НАРУШЕНИЯ"
        print(f"check_trace: {status}: {len(report.violations)} нарушений, {len(report.warnings)} предупреждений, "
              f"N_unsafe={report.n_unsafe}" + (f"; {report.by_class()}" if report.violations else ""), file=sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
