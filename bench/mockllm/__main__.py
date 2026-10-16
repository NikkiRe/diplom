"""CLI mock-llm.

    python -m bench.mockllm --mode oracle --port 18100 --journal run/mockllm_journal.jsonl
    python -m bench.mockllm --mode record --upstream http://127.0.0.1:18101/v1 --recording rec.jsonl --port 18100
    python -m bench.mockllm --mode replay --recording rec.jsonl --port 18100

При старте печатает в stdout одну строку JSON {"event": "listening", "host", "port", "mode", "pid"}
(--port 0 — случайный свободный порт; --port-file — записать порт в файл). SIGTERM/SIGINT — штатная остановка.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading

from .recording import IGNORED_FIELDS
from .server import MODES, ServerConfig, make_server


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m bench.mockllm",
                                description="OpenAI-совместимый mock LLM: oracle / record / replay")
    p.add_argument("--mode", choices=MODES, default="oracle")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18100, help="0 — случайный свободный (диапазон стенда 18100–18109)")
    p.add_argument("--port-file", help="записать фактический порт в файл")
    p.add_argument("--model-name", default="mock-oracle", help="id модели в /v1/models")
    p.add_argument("--journal", help="журнал запросов JSONL")
    p.add_argument("--recording", help="файл записи (record/replay)")
    p.add_argument("--upstream", help="базовый URL настоящего API для record (…/v1)")
    p.add_argument("--upstream-key-env", help="переменная окружения с ключом upstream (иначе Authorization клиента)")
    p.add_argument("--upstream-timeout", type=float, default=120.0)
    p.add_argument("--replay-miss", choices=("error", "oracle"), default="error")
    p.add_argument("--hash-ignore", default=",".join(IGNORED_FIELDS),
                   help="поля тела, исключаемые из хеша записи (через запятую)")
    p.add_argument("--cache-ttl", type=float, default=300.0, help="TTL префикс-кэша, с (0 — выключен)")
    p.add_argument("--cache-min-tokens", type=int, default=1024)
    p.add_argument("--cache-granularity", type=int, default=128)
    p.add_argument("--clock", choices=("auto", "wall", "virtual"), default="auto",
                   help="время для TTL: X-Bench-Clock / /control/clock (virtual) или стенные часы")
    p.add_argument("--latency", default="0", help="мс: 0 | fixed:250 | uniform:100:300 | lognormal:800:0.5 | normal:500:100")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--noop-text", default="", help="ответ «ничего не делать» на фоновое пробуждение")
    p.add_argument("--sequential-calls", action="store_true",
                   help="оракул выдаёт вызовы команды по одному на шаг (по умолчанию — все сразу)")
    p.add_argument("--ignore-k", action="store_true",
                   help="не добирать число вызовов на вход до k из разметки дня (VisionClaw 1/2/3/5)")
    p.add_argument("--tokenizer", default="o200k_base", help="o200k_base | heuristic")
    p.add_argument("--tool-tokens", choices=("cookbook", "json"), default="cookbook")
    p.add_argument("--calib", default="0,1", help="калибровка prompt_tokens = a + b·оценка: 'a,b'")
    p.add_argument("--reasoning-tokens", type=int, default=0, help="скрытые reasoning-токены на вызов")
    p.add_argument("--summary-tokens", type=int, default=400,
                   help="длина сводки оракула на запрос purpose=summary, токенов (summ_len модели D_artifacts)")
    p.add_argument("--pad-profile", choices=("targeted", "legacy"), default="targeted",
                   help="добор до k: targeted — чтения целей/ref, list_devices ≤ 1 раза; legacy — как до ревью")
    p.add_argument("--tls-cert")
    p.add_argument("--tls-key")
    p.add_argument("--verbose", action="store_true")
    return p


def config_from_args(a: argparse.Namespace) -> ServerConfig:
    ca, cb = (float(x) for x in a.calib.split(","))
    return ServerConfig(
        mode=a.mode, host=a.host, port=a.port, model_name=a.model_name, journal_path=a.journal,
        recording_path=a.recording, upstream=a.upstream, upstream_key_env=a.upstream_key_env,
        upstream_timeout_s=a.upstream_timeout, cache_ttl_s=a.cache_ttl, cache_min_tokens=a.cache_min_tokens,
        cache_granularity=a.cache_granularity, latency=a.latency, seed=a.seed, noop_text=a.noop_text,
        parallel_calls=not a.sequential_calls, honor_k=not a.ignore_k, replay_miss=a.replay_miss,
        tokenizer=a.tokenizer,
        tool_tokens=a.tool_tokens, calib=(ca, cb), reasoning_tokens=a.reasoning_tokens, clock=a.clock,
        summary_tokens=a.summary_tokens, pad_profile=a.pad_profile,
        tls_cert=a.tls_cert, tls_key=a.tls_key,
        hash_ignore=tuple(x for x in a.hash_ignore.split(",") if x), verbose=a.verbose)


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        cfg = config_from_args(a)
        srv, app = make_server(cfg)
    except (ValueError, OSError) as e:
        print(f"mock-llm: {e}", file=sys.stderr)
        return 2
    host, port = srv.server_address[:2]
    if a.port_file:
        with open(a.port_file, "w") as f:
            f.write(str(port))
    print(json.dumps({"event": "listening", "host": host, "port": port, "mode": cfg.mode, "pid": os.getpid(),
                      "tokenizer": app.tok.name}), flush=True)

    def stop(_signum, _frame):
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        srv.serve_forever(poll_interval=0.1)
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
