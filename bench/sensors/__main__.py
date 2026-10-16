"""Свежий Go-агент + sim-mcp + mockllm, только чтение условных датчиков."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

import yaml

from bench.metrics.compute import compute_metrics
from bench.mockllm import RunningServer, ServerConfig
from bench.replay import ReplayConfig, Replayer
from monitor.check_trace import check

ROOT = Path(__file__).resolve().parents[2]
MODES = ("gate", "heartbeat", "hybrid")


def save(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sensor_day(path: Path) -> None:
    """Имена и пороги условные: пример контракта, без выбора периферии."""
    header = {
        "kind": "header", "format": "sensor-smoke.v1", "source": "synthetic",
        "generator": {"name": "bench.sensors", "version": 1},
        "profile": "sensor-smoke", "seed": 0, "split": "tune", "home": "fixture",
        "date": "2026-10-07", "window_s": [0, 1800], "t_origin": "midnight",
        "devices": [
            {"id": "sensor_numeric", "type": "sensor", "location": "test",
             "properties": {"value": {"type": "number", "resolution": 0.1}}},
            {"id": "sensor_binary", "type": "sensor", "location": "test",
             "properties": {"active": {"type": "boolean"}}},
        ],
        "initial_state": {"sensor_numeric": {"value": 20.0}, "sensor_binary": {"active": False}},
    }
    events = [
        {"kind": "sensor", "t": t, "device": "sensor_numeric", "property": "value",
         "value": 20.0 + (t // 10 % 3) * 0.1, "source": "synthetic"}
        for t in range(10, 300, 10)
    ]
    for t, ident, device, prop, value, name in (
        (310, "s1", "sensor_numeric", "value", 35.0, "numeric_threshold"),
        (910, "s2", "sensor_binary", "active", True, "binary_change"),
    ):
        events += [
            {"kind": "situation", "t": t, "id": ident, "name": name, "significant": True,
             "deadline_s": 120, "expect": {"type": "notify"}, "evidence": {"device": device}},
            {"kind": "sensor", "t": t, "device": device, "property": prop,
             "value": value, "source": "synthetic"},
        ]
    events += [
        {"kind": "sensor", "t": 450, "device": "sensor_numeric", "property": "value", "value": 20.0},
        {"kind": "sensor", "t": 1200, "device": "sensor_binary", "property": "active", "value": False},
    ]
    for t, ident in ((100, "q1"), (700, "q2")):
        value = 20.1 if t == 100 else 20.0
        events.append({"kind": "question", "t": t, "id": ident,
                       "text": "Каково текущее значение sensor_numeric?", "k": 2,
                       "answer": {"value": value, "tol": 0.01,
                                  "ref": {"device": "sensor_numeric", "property": "value"}}})
    events.sort(key=lambda e: (e["t"], e["kind"] != "situation"))
    with path.open("x", encoding="utf-8") as stream:
        for rec in [header, *events]:
            stream.write(json.dumps(rec, ensure_ascii=False) + "\n")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_http(url: str, proc: subprocess.Popen, log: Path) -> None:
    deadline = time.monotonic() + 30
    while True:
        try:
            with urllib.request.urlopen(url, timeout=1) as reply:
                json.load(reply)
            return
        except OSError:
            if proc.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError(f"{url}: {log.read_text(errors='replace')[-2000:]}") from None
            time.sleep(0.05)


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)


def validate_run(metrics: dict, raw: dict, monitor_ok: bool, replay_errors: list[str]) -> list[str]:
    errors = list(replay_errors)
    if not monitor_ok:
        errors.append("monitor rejected trace")
    if raw.get("hw", {}).get("devices") != 2 or raw.get("mcp", {}).get("updates", 0) <= 0:
        errors.append("sensor discovery or updates missing")
    if any(raw.get("errors", {}).values()):
        errors.append("agent reported errors")
    questions, situations = metrics["questions"], metrics["situations"]
    if questions.get("correct") != 2 or questions.get("total") != 2:
        errors.append("expected two correct sensor answers")
    if situations.get("significant") != 2 or situations.get("reacted_in_time") != 2:
        errors.append("expected two sensor alerts within deadlines")
    return errors


def run_mode(group: Path, binary: Path, mode: str) -> dict:
    work = group / (mode + "-services")
    work.mkdir()
    sim_port, edge_port = free_port(), free_port()
    while edge_port == sim_port:
        edge_port = free_port()
    sim_url, edge_url = f"http://127.0.0.1:{sim_port}", f"http://127.0.0.1:{edge_port}"
    procs, logs = [], []
    try:
        sim_log = work / "sim.log"
        logs.append(sim_log.open("w"))
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "bench.simmcp", "--day", str(group / "day.jsonl"),
             "--port", str(sim_port), "--journal", str(work / "sim.jsonl"), "--run-id", mode],
            cwd=ROOT, stdout=logs[-1], stderr=subprocess.STDOUT))
        wait_http(sim_url + "/healthz", procs[-1], sim_log)
        with RunningServer(ServerConfig(port=0)) as mock:
            cfg = yaml.safe_load((ROOT / "bench/simmcp/edgectx_simday.yaml").read_text())
            cfg["clock"].update(start_t=0, epoch="2026-10-07T00:00:00+03:00")
            cfg["llm"].update(base_url=mock.url + "/v1", api_key_env="", model="mock-oracle")
            cfg["mcp"][0]["url"] = sim_url + "/mcp"
            cfg["policy"].update(wake=mode, heartbeat_min=1, history="window", window_turns=8,
                                  local_tools=["notify_user", "recent_events"], profile_consolidate_s=0)
            cfg["policy"]["gate"].update(
                debounce_s=2, cooldown_s=60, coalesce_s=5, rules_enabled=False,
                signals=[{"device": "sensor_numeric", "property": "value", "kind": "threshold",
                          "above": 30, "hysteresis": 1},
                         {"device": "sensor_binary", "property": "active", "kind": "change"}])
            cfg["userctx"].update(db=str(work / "agent.db"), profile=[])
            cfg_path, trace, edge_log = work / "agent.yaml", work / "agent.jsonl", work / "agent.log"
            cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
            logs.append(edge_log.open("w"))
            procs.append(subprocess.Popen(
                [str(binary), "-config", str(cfg_path), "-listen", f"127.0.0.1:{edge_port}",
                 "-trace", str(trace), "-run-id", mode], cwd=work, stdout=logs[-1], stderr=subprocess.STDOUT))
            wait_http(edge_url + "/v1/health", procs[-1], edge_log)
            replay = Replayer(ReplayConfig(
                day=str(group / "day.jsonl"), out_root=str(group), run_id=mode,
                edgectx=edge_url, sim=sim_url, mock=mock.url, sync="updates", sync_timeout_s=10,
                collect={"edgectx_trace": str(trace), "simmcp_trace": str(work / "sim.jsonl")},
                configs={"edgectx": str(cfg_path)}, binaries={"edgectx": str(binary)}))
            try:
                run_dir = replay.run()
            finally:
                replay.close()
            monitor = check(trace=run_dir / "edgectx_trace.jsonl")
            save(run_dir / "monitor.json", monitor.to_dict())
            metrics = compute_metrics(run_dir)
            save(run_dir / "metrics.json", metrics)
            raw = json.loads((run_dir / "edgectx_metrics.json").read_text())
            questions = metrics["questions"]
            errors = validate_run(metrics, raw, monitor.ok, replay.errors)
            result = {"ok": not errors, "dev_run": True, "mode": mode,
                      "note": "Ориентир, не результат: виртуальные часы и mockllm; ресурсов платы не измеряли.",
                      "llm_calls": metrics["llm"]["calls"], "questions": questions,
                      "situations": metrics["situations"], "errors": errors,
                      "monitor_ok": monitor.ok, "agent_errors": raw.get("errors", {})}
            save(run_dir / "check.json", result)
            return result
    finally:
        for proc in reversed(procs):
            stop(proc)
        for log in logs:
            log.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка датчиков без платы и ключа; только mockllm")
    parser.add_argument("--out", type=Path, default=ROOT / "bench/sensors/smoke")
    parser.add_argument("--mode", choices=["all", *MODES], default="all")
    args = parser.parse_args()
    go = shutil.which("go")
    if not go:
        parser.error("нужен Go для свежей сборки agent/")
    group = args.out.resolve() / (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    group.mkdir(parents=True, exist_ok=False)
    print(f"Артефакты: {group}", flush=True)
    binary = group / "edgectx"
    try:
        subprocess.run([go, "build", "-trimpath", "-o", str(binary), "./cmd/edgectx"],
                       cwd=ROOT / "agent", env={**os.environ, "CGO_ENABLED": "0"}, check=True, timeout=300)
        sensor_day(group / "day.jsonl")
        results = [run_mode(group, binary, mode) for mode in (MODES if args.mode == "all" else [args.mode])]
        hashes = {str(p.relative_to(group)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in group.rglob("*") if p.is_file()}
        save(group / "check.json", {"ok": all(r["ok"] for r in results), "dev_run": True,
                                    "results": results, "sha256": hashes})
        for result in results:
            print(f"{result['mode']}: {'OK' if result['ok'] else 'FAIL'}, LLM={result['llm_calls']}")
        print("Ориентир, не результат: mockllm, виртуальное время, без платы.")
        return 0 if all(r["ok"] for r in results) else 1
    except Exception as exc:
        save(group / "failure.json", {"ok": False, "dev_run": True, "error": str(exc)})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
