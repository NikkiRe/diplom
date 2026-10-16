"""CLI sim-mcp.

  python -m bench.simmcp --day day.jsonl [--port 18090] [--catalog generic|explicit]
                         [--journal sim.jsonl] [--report report.json] [--run-id R] [--notify-all] …
  python -m bench.simmcp --day day.jsonl --print-costs      каталог и ресурсы: байты, токены, доля answer/refuse

MCP: http://<host>:<port>/mcp ; управление: /control/clock, /control/input, /control/state,
/control/report, /control/finish ; /healthz.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from bench.daygen.generator import read_day

from .server import NOTIFY_WAIT_S, SESSION_IDLE_TIMEOUT_S, Journal, SimMCP, build_app, dumps, new_run_id


def costs(sim: SimMCP) -> dict:
    """Что sim-mcp добавляет в промпт и трафик: каталог (токены, доля answer/refuse), list_devices, ресурсы."""
    from . import catalog as catalog_mod
    out = {"catalog": sim.catalog, "n_tools": len(sim.tools), "tools": catalog_mod.eval_share(sim.tools)}
    try:
        from bench.mockllm.tokens import load_tokenizer
        tok = load_tokenizer()
        count = tok.count
    except Exception:  # noqa: BLE001
        count = None

    def size(obj) -> dict:
        text = dumps(obj)
        d = {"bytes": len(text.encode())}
        if count:
            d["tokens"] = count(text)
        return d

    out["list_devices"] = size(sim.world.list_devices(sim.rich_labels))
    out["inventory"] = size(sim.world.inventory())
    states = [sim.world.device_state(d) for d in sorted(sim.world.devices)]
    out["state_resource_mean_bytes"] = round(sum(size(s)["bytes"] for s in states) / max(len(states), 1), 1)
    out["read_result_example"] = asyncio.run(_first_read(sim))
    return out


async def _first_read(sim: SimMCP) -> dict:
    dev = sorted(sim.world.devices)[0]
    prop = sorted(sim.world.devices[dev]["properties"])[0]
    name = "read_property" if sim.catalog == "generic" else f"read_{dev}_{prop}"
    args = {"device": dev, "property": prop} if sim.catalog == "generic" else {}
    res = await sim.call(name, args)
    return json.loads(res.content[0].text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.simmcp", description="sim-mcp: эмулятор помещения (MCP)")
    ap.add_argument("--day", required=True, help="файл дня daygen (JSONL)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18090, help="порт (диапазон sim-mcp 18090–18099)")
    ap.add_argument("--catalog", default="generic", choices=["generic", "explicit"])
    ap.add_argument("--scheme", default="hw", help="схема URI ресурсов (hw://… как у hw-mcp)")
    ap.add_argument("--journal", help="журнал JSONL (дописывается)")
    ap.add_argument("--report", help="куда записать отчёт оценки при завершении (JSON)")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--notify-all", action="store_true",
                    help="слать resources/updated всем сессиям, а не только подписанным")
    ap.add_argument("--structured-content", action="store_true",
                    help="дублировать результат в structuredContent (по умолчанию только текст, как hw-mcp)")
    ap.add_argument("--rich-labels", action="store_true",
                    help="подписи list_devices с диапазоном и допустимыми значениями (у hw-mcp их нет; +≈30 %% токенов)")
    ap.add_argument("--extra-resources", action="store_true",
                    help="ещё ресурсы hw://state (всё) и hw://events (у hw-mcp их нет)")
    ap.add_argument("--no-eval-tools", action="store_true",
                    help="без инструментов оценки answer/refuse (прогоны затрат; вопросы и ловушки тогда не оцениваются)")
    ap.add_argument("--inline-state", action="store_true",
                    help="класть новые значения в _meta.state уведомления (агенту не нужно читать ресурс; у hw-mcp нет)")
    ap.add_argument("--notify-wait", type=float, default=NOTIFY_WAIT_S,
                    help="сколько /control/clock ждёт доставки уведомлений успевающим сессиям, с")
    ap.add_argument("--start-t", type=float, default=None, help="начальные часы (по умолчанию начало окна)")
    ap.add_argument("--session-idle-timeout", type=float, default=SESSION_IDLE_TIMEOUT_S,
                    help="закрывать MCP-сессию без запросов и без GET-потока дольше N с (0 — не закрывать)")
    ap.add_argument("--print-costs", action="store_true", help="напечатать размеры каталога и ресурсов и выйти")
    ap.add_argument("--log-level", default="warning")
    a = ap.parse_args(argv)
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        print("предупреждение: sim-mcp без аутентификации слушает не localhost", file=sys.stderr)
    logging.basicConfig(level=a.log_level.upper(), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    header, events = read_day(a.day)
    run_id = a.run_id or new_run_id()
    sim = SimMCP(header, events, catalog=a.catalog, scheme=a.scheme,
                 journal=Journal(None if a.print_costs else a.journal, run_id),
                 notify_all=a.notify_all, start_t=a.start_t, report_path=None if a.print_costs else a.report,
                 structured=a.structured_content, rich_labels=a.rich_labels, extra_resources=a.extra_resources,
                 eval_tools=not a.no_eval_tools, inline_state=a.inline_state, notify_wait_s=a.notify_wait)
    if a.print_costs:
        print(json.dumps(costs(sim), ensure_ascii=False, indent=1))
        return 0
    import uvicorn
    app = build_app(sim, session_idle_timeout=a.session_idle_timeout or None)
    print(f"sim-mcp {header.get('home')} {header.get('date')} catalog={a.catalog} tools={len(sim.tools)} "
          f"run_id={run_id}: http://{a.host}:{a.port}/mcp", file=sys.stderr, flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level=a.log_level, lifespan="on", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
