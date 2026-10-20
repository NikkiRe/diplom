"""Isolated, cancellable local EdgeLab bench. No provider or physical MCP endpoint.

The actual Go EdgeCtx, bench.simmcp, bench.mockllm oracle and bench.replay execute
the experiment. Mock tokens are explicitly estimates; no real LLM is contacted.
Invoked only with server-generated paths/specs by LabStore, never import paths.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from .lab import ROOT, experiment_spec


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_hash():
    digest = hashlib.sha256()
    for path in sorted((ROOT / "agent").rglob("*.go")) + [ROOT / "agent/go.mod", ROOT / "agent/go.sum"]:
        if path.is_file():
            digest.update(str(path.relative_to(ROOT)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def bench_hash():
    digest = hashlib.sha256()
    for path in sorted((ROOT / "bench").rglob("*.py")):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def make_day(spec):
    """Seeded stimulus independent of policy: paired runs receive identical inputs."""
    rng = random.Random(spec["seed"])
    duration = spec["duration_s"]
    devices = [
        {"id": "thermo_lab", "type": "climate_sensor", "location": "lab", "title": {"ru": "Датчик лаборатории"},
         "properties": {"temperature": {"type": "number", "unit": "Cel", "resolution": 0.1, "rw": False}}, "actions": {}},
        {"id": "boiler", "type": "boiler", "location": "lab", "title": {"ru": "Виртуальный котёл"},
         "properties": {"temperature": {"type": "number", "unit": "Cel", "resolution": 0.1, "rw": False},
                        "heating": {"type": "boolean", "rw": True}}, "actions": {}},
        {"id": "leak_lab", "type": "leak_sensor", "location": "lab", "title": {"ru": "Датчик протечки"},
         "properties": {"wet": {"type": "integer", "enum": [0, 1], "rw": False}}, "actions": {}},
        {"id": "valve_water", "type": "valve", "location": "lab", "title": {"ru": "Виртуальный водяной клапан"},
         "properties": {"open": {"type": "boolean", "rw": True}}, "actions": {}},
    ]
    header = {"kind": "header", "seed": spec["seed"], "source": "edgelab_seeded_synthetic",
              "profile": "EdgeLab", "home": "virtual_lab", "split": "development", "dev_run": True,
              "scenario": spec["scenario"], "window_s": [0, duration], "devices": devices,
              "initial_state": {"thermo_lab": {"temperature": 22.0}, "boiler": {"temperature": 60.0, "heating": True},
                                "leak_lab": {"wet": 0}, "valve_water": {"open": True}}}
    events = []
    def sensor(t, device, prop, value):
        events.append({"kind": "sensor", "t": round(t, 3), "device": device, "property": prop,
                       "value": value, "source": "seeded synthetic stimulus"})
    for index in range(1, 18):
        sensor(duration * index / 55, "thermo_lab", "temperature", round(22 + rng.uniform(-0.35, 0.35), 1))
    onset = round(duration * 0.36, 3)
    deadline = min(12.0, duration * 0.1)
    actions = []
    if spec["scenario"] == "leak-recovery":
        sensor(onset, "leak_lab", "wet", 1)
        sensor(duration * 0.72, "leak_lab", "wet", 0)
        actions = [{"name": "write_property", "args": {"device": "valve_water", "property": "open", "value": False}}]
        name = "water_leak"
    elif spec["scenario"] == "overheat":
        sensor(onset, "boiler", "temperature", 92.0)
        sensor(duration * 0.70, "boiler", "temperature", 67.0)
        actions = [{"name": "write_property", "args": {"device": "boiler", "property": "heating", "value": False}}]
        deadline = duration * 0.25
        name = "boiler_overheat"
    else:
        sensor(onset, "thermo_lab", "temperature", 34.5)
        sensor(onset + duration * 0.08, "thermo_lab", "temperature", 23.0)
        name = "brief_room_overheat"
    events.append({"kind": "situation", "t": onset, "id": "s-heat" if "overheat" in name else "s-leak",
                   "name": name, "significant": True, "deadline_s": deadline,
                   "expect": {"type": "notify_or_action", "actions": actions}})
    sensor(duration * 0.78, "thermo_lab", "temperature", 23.0)
    events.append({"kind": "question", "t": round(duration * 0.85, 3), "id": "q-temperature",
                   "text": "Какая сейчас температура в лаборатории?", "lang": "ru",
                   "answer": {"value": 23.0, "unit": "Cel", "tol": 0.1,
                              "ref": {"device": "thermo_lab", "property": "temperature"}}})
    events.sort(key=lambda e: (e["t"], 0 if e["kind"] == "situation" else 1))
    return header, events


def make_config(spec, work, edge_port, sim_port, mock_port):
    # JSON is valid YAML. No optional YAML dependency needed in the UI process.
    return {
        "clock": {"mode": "virtual", "epoch": "2026-10-06T00:00:00Z", "start_t": 0, "settle_timeout_s": 15},
        "listen": "127.0.0.1:%d" % edge_port,
        "llm": {"base_url": "http://127.0.0.1:%d/v1" % mock_port, "model": "mock-oracle",
                "api_key_env": "EDGELAB_NO_API_KEY", "timeout_s": 8, "retries": 0, "trace_headers": True},
        "mcp": [{"name": "sim", "transport": "http", "url": "http://127.0.0.1:%d/mcp" % sim_port,
                 "inventory_uri": "hw://inventory", "subscribe": ["*/state/*"]}],
        "policy": {"wake": spec["policy"], "heartbeat_min": spec["heartbeat_s"] / 60.0,
                   "heartbeat_task": "Проверь помещение. При значимом событии выполни действие или уведомление; иначе NO_ACTION.",
                   "max_steps": 4, "gate": {"debounce_s": spec["debounce_s"], "cooldown_s": 15,
                       "coalesce_s": 1, "self_echo_s": 10, "rules_enabled": True, "default": "ignore",
                       "signals": [
                           {"device": "thermo_lab", "property": "temperature", "kind": "threshold",
                            "above": spec["threshold"], "hysteresis": 0.5},
                           {"device": "boiler", "property": "temperature", "kind": "threshold", "above": 85,
                            "hysteresis": 1, "priority": "safety"},
                           {"device": "leak_lab", "property": "wet", "kind": "change", "priority": "safety"}]},
                   "history": spec["history"], "window_turns": spec["window_turns"],
                   "mask_keep_turns": min(2, spec["window_turns"]), "history_batch_turns": min(3, spec["window_turns"]),
                   "history_background": True, "hwctx": "delta_tail", "catalog": "generic",
                   "local_tools": ["notify_user", "recent_events"], "noop_reply": "NO_ACTION", "recent_events_k": 10},
        "budget": {"daily_calls": 200, "daily_input_tokens": 500000, "degrade_at": 0.9},
        "sched": {"max_in_flight": 1, "queue_max": 32, "deferred_max": 16},
        "trace": str(work / "edgectx_trace.jsonl"), "trace_flush_s": 0,
        "trace_context": True, "trace_context_max_bytes": 65536,
        "userctx": {"db": str(work / "context.db"), "reset": True,
                    "profile": [{"id": "comfort", "text": "Комфортная температура пользователя 22 °C; сообщай о перегреве и протечке."}]},
    }


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait(url, process, log, timeout=15):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        if process.poll() is not None:
            break
        time.sleep(0.05)
    tail = log.read_bytes()[-1600:].decode("utf-8", "replace") if log.exists() else ""
    raise RuntimeError("Локальный процесс не запустился: " + tail)


def _stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


class AgentSampler:
    """Observed host process counters, never scaled to simulated time or a board."""

    def __init__(self, pid, interval=0.2):
        self.pid, self.interval = pid, interval
        self.samples = []
        self._done = threading.Event()
        self._start = time.monotonic()
        self._thread = threading.Thread(target=self._loop, name="edgelab-agent-sampler", daemon=True)
        self.source = "/proc/<agent_pid>/stat" if Path("/proc/%d/stat" % pid).exists() else "ps agent_pid"

    def sample(self):
        try:
            if self.source.startswith("/proc"):
                # Comm can contain spaces and ')'; fields after the last ')' begin at stat field 3.
                fields = Path("/proc/%d/stat" % self.pid).read_text().rsplit(")", 1)[1].split()
                cpu = (int(fields[11]) + int(fields[12])) / float(os.sysconf("SC_CLK_TCK"))
                rss = int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
            else:
                result = subprocess.run(["ps", "-p", str(self.pid), "-o", "pid=,rss=,time="],
                                        capture_output=True, text=True, timeout=1)
                fields = result.stdout.split()
                if result.returncode or len(fields) != 3 or int(fields[0]) != self.pid:
                    return
                rss = int(fields[1]) * 1024
                clock = fields[2]
                days = 0
                if "-" in clock:
                    day, clock = clock.split("-", 1)
                    days = int(day)
                cpu = days * 86400
                multiplier = 1
                for part in reversed(clock.split(":")):
                    cpu += float(part) * multiplier
                    multiplier *= 60
            self.samples.append({"wall_s": time.monotonic() - self._start, "ts_wall": time.time(),
                                 "cpu_s": cpu, "rss_bytes": rss})
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            pass  # unavailable counters remain absent, never zero-filled

    def _loop(self):
        while not self._done.is_set():
            self.sample()
            self._done.wait(self.interval)

    def start(self):
        self._thread.start()

    def stop(self):
        self._done.set()
        self._thread.join(timeout=2)
        samples = self.samples
        return {"target": "agent_pid_only", "pid": self.pid, "source": self.source,
                "host": {"system": platform.system(), "machine": platform.machine()},
                "interval_s": self.interval, "samples": samples,
                "rss_sampled_max_bytes": max(s["rss_bytes"] for s in samples) if samples else None,
                "cpu_observed_s": max(0, samples[-1]["cpu_s"] - samples[0]["cpu_s"]) if len(samples) >= 2 else None,
                "observed_wall_s": samples[-1]["wall_s"] - samples[0]["wall_s"] if len(samples) >= 2 else None,
                "diagnostic_only": True,
                "note": "Процесс агента на текущем хосте; выборочные RSS/CPU в виртуальном T0. "
                        "Не пик всей системы, не потребление за виртуальные сутки и не замер целевой платы."}
def _binary(work, explicit=None):
    if explicit:
        path = Path(explicit).resolve()
        if not path.is_file() or not os.access(str(path), os.X_OK):
            raise RuntimeError("Настроенный бинарник EdgeCtx не найден или не исполняемый")
        return path
    target = work.parent / "edgectx-local"
    fingerprint = source_hash()
    marker = target.with_suffix(".sha256")
    if target.exists() and marker.exists() and marker.read_text() == fingerprint:
        return target
    go = shutil.which("go")
    if go is None:
        raise RuntimeError("Нужен локальный Go или явно настроенный бинарник EdgeCtx; установка автоматически не выполняется")
    env = {**os.environ, "CGO_ENABLED": "0", "GOPROXY": "off", "GOSUMDB": "off", "GOTOOLCHAIN": "local"}
    env["GOCACHE"] = str(work.parent / "go-cache")
    build_log = work / "build.log"
    with build_log.open("wb") as log:
        process = subprocess.Popen([go, "build", "-trimpath", "-o", str(target), "./cmd/edgectx"],
                                   cwd=str(ROOT / "agent"), env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            status = process.wait(timeout=100)
        finally:
            _stop(process)
    if status:
        raise RuntimeError("Локальная сборка EdgeCtx не удалась (сеть отключена): " + build_log.read_text(errors="replace")[-1600:])
    marker.write_text(fingerprint)
    return target


def execute(work, binary=None):
    if sys.version_info < (3, 10):
        raise RuntimeError("Для bench.sim-mcp нужен Python ≥3.10 с зависимостями проекта; настройте EDGELAB_PYTHON")
    try:
        from bench.mockllm import RunningServer, ServerConfig
        from bench.replay import ReplayConfig, Replayer
        import mcp  # noqa: F401 — fail before building/starting anything if dependency is absent
        import uvicorn  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("Не установлены зависимости локального стенда; используйте Python из .venv проекта: " + str(exc))
    spec = experiment_spec(json.loads((work / "spec.json").read_text(encoding="utf-8")))
    def phase(value):
        tmp = work / "phase.tmp"
        _write(tmp, {"phase": value})
        tmp.replace(work / "phase.json")
    phase("building_agent")
    edge_binary = _binary(work, binary)
    header, events = make_day(spec)
    day = work / "day.jsonl"
    day.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [header] + events) + "\n", encoding="utf-8")
    phase("starting_simulator")
    ports = set()
    while len(ports) < 2:
        ports.add(_port())
    edge_port, sim_port = sorted(ports)
    sim_log, edge_log = work / "sim.log", work / "edge.log"
    procs, files = [], []
    sampler = None
    started = time.monotonic()
    try:
        for path in (sim_log, edge_log):
            files.append(path.open("wb"))
        sim = subprocess.Popen([sys.executable, "-m", "bench.simmcp", "--day", str(day), "--port", str(sim_port),
                                "--journal", str(work / "simmcp_journal.jsonl"), "--run-id", "edgelab"],
                               cwd=str(ROOT), stdout=files[0], stderr=subprocess.STDOUT)
        procs.append(sim)
        sim_url, edge_url = "http://127.0.0.1:%d" % sim_port, "http://127.0.0.1:%d" % edge_port
        _wait(sim_url + "/healthz", sim, sim_log)
        # Explicit heuristic avoids tiktoken downloading a missing dictionary. Every token/cost is an estimate.
        with RunningServer(ServerConfig(port=0, mode="oracle", tokenizer="heuristic", latency="0", seed=spec["seed"],
                                        clock="virtual", model_name="mock-oracle", noop_text="NO_ACTION")) as mock:
            config = make_config(spec, work, edge_port, sim_port, mock.port)
            config_path = work / "edgectx.yaml"
            _write(config_path, config)
            phase("starting_agent")
            env = {**os.environ, "EDGELAB_NO_API_KEY": "", "GOMEMLIMIT": "160MiB", "GOMAXPROCS": "2"}
            agent = subprocess.Popen([str(edge_binary), "-config", str(config_path), "-run-id", "edgelab-%d" % spec["seed"]],
                                     cwd=str(work), env=env, stdout=files[1], stderr=subprocess.STDOUT)
            procs.append(agent)
            _wait(edge_url + "/v1/health", agent, edge_log)
            sampler = AgentSampler(agent.pid)
            sampler.start()
            phase("executing_scenario")
            replayer = Replayer(ReplayConfig(day=str(day), out_root=str(work / "results"), run_id="run",
                                             edgectx=edge_url, sim=sim_url, mock=mock.url, max_step_s=5,
                                             sync="auto", sync_timeout_s=2, http_timeout_s=12, user_timeout_s=15,
                                             shutdown_timeout_s=10, collect={"edgectx_trace": str(work / "edgectx_trace.jsonl"),
                                                                                 "simmcp_journal": str(work / "simmcp_journal.jsonl")},
                                             configs={"edgectx": str(config_path)}, binaries={"edgectx": str(edge_binary)}))
            try:
                run_dir = replayer.run()
                if replayer.errors:
                    raise RuntimeError("Стенд завершился с ошибками: " + "; ".join(replayer.errors))
                state_status, final_state, _, _ = replayer.sim.get("/control/state", timeout_s=2)
                if state_status != 200 or not isinstance(final_state, dict):
                    raise RuntimeError("sim-mcp не подтвердил конечное состояние виртуального мира")
            finally:
                replayer.close()
        phase("collecting_evidence")
        trace = work / "edgectx_trace.jsonl"
        records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line.strip()]
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("status") != "completed" or not manifest.get("edgectx_shutdown", {}).get("run_end"):
            raise RuntimeError("Replay не подтвердил завершение EdgeCtx")
        # Reuse the thesis evaluator rather than inferring success from a nearby sensor event.
        from bench.metrics import compute_metrics
        measured = compute_metrics(run_dir)
        evaluation = {key: measured.get(key) for key in ("situations", "questions", "commands", "safety", "metric_validity")}
        resources = sampler.stop() if sampler is not None else None
        return {"events": records, "config": config, "evaluation": evaluation,
                "resources": resources,
                "scenario_truth": {"header": header, "events": events, "final_state": final_state,
                                   "source": "seeded synthetic sim-mcp input; final_state read from sim-mcp"},
                "provenance": {"execution": "actual_go_agent_local_oracle", "dev_run": True,
                               "network": "loopback_only", "external_llm": False, "physical_equipment": False,
                               "seed": spec["seed"], "day_sha256": _hash(day), "agent_sha256": _hash(edge_binary),
                               "agent_source_sha256": source_hash(), "raw_trace_sha256": _hash(trace),
                               "bench_source_sha256": bench_hash(), "runner_sha256": _hash(__file__),
                               "tokenizer": "heuristic", "tokens_are_estimates": True, "real_money_spent": 0,
                               "wall_s": time.monotonic() - started,
                               "manifest": {key: manifest.get(key) for key in ("status", "completed", "mode", "dev_run", "versions", "files", "day", "edgectx_shutdown", "errors", "warnings")}}}
    finally:
        if sampler is not None:
            sampler.stop()
        for proc in reversed(procs):
            _stop(proc)
        for stream in files:
            stream.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", required=True)
    parser.add_argument("--binary")
    args = parser.parse_args(argv)
    work = Path(args.work).resolve()
    def terminate(signum, frame):
        raise KeyboardInterrupt("EdgeLab cancelled")
    signal.signal(signal.SIGTERM, terminate)
    try:
        result = execute(work, args.binary)
    except BaseException as exc:
        _write(work / "result.json", {"error": str(exc) or type(exc).__name__})
        return 1
    _write(work / "result.json", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
