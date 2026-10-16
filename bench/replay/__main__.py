"""CLI реплеера дня (T0).

    python -m bench.replay --day data/days/m_plus_hh101.jsonl \
        --edgectx http://127.0.0.1:18080 --sim http://127.0.0.1:18090 --mock http://127.0.0.1:18100 \
        --out experiments/results [--run-id ID] \
        --collect edgectx_trace=/tmp/edgectx_trace.jsonl --collect sysmon=/tmp/sysmon.jsonl \
        --config edgectx=agent/configs/t0.yaml --binary edgectx=agent/bin/edgectx

Код выхода: 0 — прогон завершён (ошибки отдельных реплик — в manifest.errors), 1 — прогон прерван,
2 — ошибка аргументов.
"""
from __future__ import annotations

import argparse
import json
import sys

from .replayer import ReplayConfig, Replayer, ReplayError


def _pairs(items: list[str] | None, opt: str) -> dict[str, str]:
    out = {}
    for it in items or []:
        if "=" not in it:
            raise ValueError(f"{opt} expects label=path, got {it!r}")
        k, v = it.split("=", 1)
        out[k] = v
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m bench.replay", description="проигрывание дня в edgectx/sim-mcp/mock-llm")
    p.add_argument("--day", required=True, help="файл дня JSONL (спецификация §2)")
    p.add_argument("--edgectx", help="URL edgectx, напр. http://127.0.0.1:18080")
    p.add_argument("--sim", help="URL sim-mcp (управление часами), напр. http://127.0.0.1:18090")
    p.add_argument("--mock", help="URL mock-llm, напр. http://127.0.0.1:18100")
    p.add_argument("--out", required=True, help="корень результатов; прогон пишется в <out>/<run_id>/")
    p.add_argument("--run-id")
    p.add_argument("--max-step", type=float, default=60.0, help="макс. шаг виртуальных часов, с")
    p.add_argument("--settle-ms", type=float, default=0.0,
                   help="ждать N мс тишины mock-llm после шагов с таймерами/уведомлениями (0 — не ждать: edgectx "
                        "дожидается своих ходов в /v1/clock/advance, время запроса — X-Edgectx-Clock-T)")
    p.add_argument("--settle-timeout", type=float, default=30.0)
    p.add_argument("--sync", choices=("auto", "inputs", "updates", "none"), default="auto",
                   help="синхронизация с уведомлениями sim-mcp: inputs — min_inputs в /v1/clock/advance; updates — "
                        "ждать mcp.updates в /v1/metrics; auto — inputs, после 3 промахов updates")
    p.add_argument("--sync-timeout", type=float, default=2.0, help="предел ожидания синхронизации, с")
    p.add_argument("--no-shutdown", action="store_true",
                   help="не вызывать POST edgectx /v1/shutdown в конце (трасса останется без run_end)")
    p.add_argument("--shutdown-timeout", type=float, default=30.0, help="ожидание run_end в трассе, с")
    p.add_argument("--user-timeout", type=float, default=120.0, help="таймаут ответа edgectx на реплику, с")
    p.add_argument("--http-timeout", type=float, default=30.0)
    p.add_argument("--sensor-mode", choices=("sim", "post", "none"), default="sim",
                   help="sim — события публикует sim-mcp по часам; post — ещё и POST edgectx /v1/event")
    p.add_argument("--no-expire", action="store_true", help="не снимать ожидание ситуации по дедлайну")
    p.add_argument("--no-reset-mock", action="store_true")
    p.add_argument("--window-end", type=float, help="конец проигрывания, с от полуночи (по умолчанию — конец окна)")
    p.add_argument("--collect", action="append", metavar="LABEL=PATH", help="скопировать файл в каталог прогона")
    p.add_argument("--config", action="append", metavar="LABEL=PATH", help="конфиг в manifest (целиком)")
    p.add_argument("--binary", action="append", metavar="LABEL=PATH", help="бинарник в manifest (SHA-256)")
    p.add_argument("--dry-run", action="store_true", help="без сети: только план шагов и manifest")
    p.add_argument("--force", action="store_true", help="разрешить непустой каталог прогона")
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        cfg = ReplayConfig(day=a.day, out_root=a.out, edgectx=a.edgectx, sim=a.sim, mock=a.mock, run_id=a.run_id,
                           max_step_s=a.max_step, settle_ms=a.settle_ms, settle_timeout_s=a.settle_timeout,
                           sync=a.sync, sync_timeout_s=a.sync_timeout, shutdown=not a.no_shutdown,
                           shutdown_timeout_s=a.shutdown_timeout,
                           user_timeout_s=a.user_timeout, http_timeout_s=a.http_timeout, sensor_mode=a.sensor_mode,
                           expire_situations=not a.no_expire, reset_mock=not a.no_reset_mock,
                           window_end=a.window_end, collect=_pairs(a.collect, "--collect"),
                           configs=_pairs(a.config, "--config"), binaries=_pairs(a.binary, "--binary"),
                           dry_run=a.dry_run, force=a.force)
        rp = Replayer(cfg)
    except (ValueError, OSError) as e:
        print(f"replay: {e}", file=sys.stderr)
        return 2
    try:
        run_dir = rp.run()
    except ReplayError as e:
        print(f"replay: aborted: {e}", file=sys.stderr)
        return 1
    finally:
        rp.close()
    print(json.dumps({"run_dir": str(run_dir), "stats": rp.stats, "errors": len(rp.errors)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
