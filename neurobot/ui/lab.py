"""Bounded EdgeCtx trace laboratory. The HTTP surface accepts content, never paths.

Imports are observations, not evidence of physical causality. Executable jobs use
the project's actual Go agent and isolated local simulators (see lab_runner).
This module itself is Python 3.8 / standard-library only.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

MAX_IMPORT_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 12000
MAX_RUNS = 16
MAX_STORE_BYTES = 32 * 1024 * 1024
MAX_JOBS = 32
MAX_DEPTH = 24
MAX_NODES = 250000
MAX_JOB_SECONDS = 180
MAX_ARTIFACT_BYTES = 24 * 1024 * 1024
MAX_WATCHERS = 4
WATCH_POLL_S = 1.0
ROOT = Path(__file__).resolve().parents[2]

SCENARIOS = [
    {"id": "noise-burst", "label": "Шум и короткий перегрев", "seed": 7,
     "description": "Колебания датчика, короткий опасный эпизод и вопрос пользователя. "
                    "Сравните цену подавления шума и пропущенную реакцию.",
     "duration_s": 180, "unit": "Cel", "source": "seeded synthetic room"},
    {"id": "overheat", "label": "Продолжительный перегрев", "seed": 7,
     "description": "Постепенный нагрев, значимый перегрев и охлаждение; "
                    "одинаковые входы для разных политик пробуждения.",
     "duration_s": 180, "unit": "Cel", "source": "seeded synthetic room"},
    {"id": "leak-recovery", "label": "Протечка и перекрытие воды", "seed": 7,
     "description": "Датчик протечки и виртуальный водяной клапан. "
                    "Действия выполняются только в sim-mcp.",
     "duration_s": 180, "unit": None, "source": "seeded synthetic room"},
]


class LabError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _dump(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise LabError("Данные должны содержать корректный конечный JSON")


def _validate_tree(value):
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise LabError("Слишком сложный JSON: предел %d узлов и глубина %d" % (MAX_NODES, MAX_DEPTH), 413)
        if isinstance(item, dict):
            if any(not isinstance(k, str) for k in item):
                raise LabError("Ключи JSON должны быть строками")
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise LabError("NaN и Infinity в трассе недопустимы")
        elif isinstance(item, int) and item.bit_length() > 1024:
            raise LabError("Число в трассе превышает допустимую разрядность")
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise LabError("В трассе допустимы только JSON-значения")


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _label(value, default):
    if value is None:
        return default
    if not isinstance(value, str) or len(value) > 160 or not value.strip():
        raise LabError("Название должно содержать от 1 до 160 символов")
    return value.strip()


def parse_trace(content=None, events=None):
    """Reject malformed/oversized imports atomically; never silently drop records."""
    if (content is None) == (events is None):
        raise LabError("Укажите ровно одно поле: content (JSONL) или events (массив)")
    if content is not None:
        if not isinstance(content, str):
            raise LabError("content должен быть строкой JSONL")
        try:
            raw = content.encode("utf-8")
        except UnicodeError:
            raise LabError("Некорректная кодировка Unicode")
        if len(raw) > MAX_IMPORT_BYTES:
            raise LabError("Трасса больше 8 MiB", 413)
        rows = []
        def integer(text):
            if len(text) > 310:
                raise ValueError("integer too long")
            return int(text)
        for no, line in enumerate(content.splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line, parse_int=integer, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, RecursionError):
                raise LabError("Строка %d: некорректный JSON" % no)
            rows.append(row)
            if len(rows) > MAX_EVENTS:
                raise LabError("В трассе больше %d событий" % MAX_EVENTS, 413)
    else:
        if not isinstance(events, list):
            raise LabError("events должен быть массивом JSON-объектов")
        if len(events) > MAX_EVENTS:
            raise LabError("В трассе больше %d событий" % MAX_EVENTS, 413)
        _validate_tree(events)
        raw = _dump(events)
        if len(raw) > MAX_IMPORT_BYTES:
            raise LabError("Трасса больше 8 MiB", 413)
        rows = copy.deepcopy(events)
    if not rows:
        raise LabError("Трасса не содержит событий")
    _validate_tree(rows)
    run_ids = set()
    for no, row in enumerate(rows, 1):
        if not isinstance(row, dict) or not isinstance(row.get("type"), str) or not row["type"] or len(row["type"]) > 128:
            raise LabError("Запись %d должна быть объектом с непустым type" % no)
        if row.get("run_id") is not None:
            if not isinstance(row["run_id"], str) or len(row["run_id"]) > 256:
                raise LabError("Запись %d: неверный run_id" % no)
            run_ids.add(row["run_id"])
        for field in ("input_id", "wake_id", "req_id", "call_id"):
            if field in row and row[field] is not None and (not isinstance(row[field], str) or len(row[field]) > 256):
                raise LabError("Запись %d: %s должен быть строкой до 256 символов" % (no, field))
        if "input_ids" in row and (not isinstance(row["input_ids"], list) or
                                   any(not isinstance(key, str) or len(key) > 256 for key in row["input_ids"])):
            raise LabError("Запись %d: input_ids должен быть массивом строк до 256 символов" % no)
    if len(run_ids) > 1:
        raise LabError("Импортируйте один run_id за раз: смешение прогонов делает ссылки неоднозначными")
    return rows, {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                  "format": "edgectx-jsonl" if content is not None else "edgectx-events",
                  "source_run_ids": sorted(run_ids)}


def _metrics(events, kind):
    types = {}
    for event in events:
        types[event["type"]] = types.get(event["type"], 0) + 1
    responses = [e for e in events if e["type"] == "llm_response"]
    valid = [e for e in responses if isinstance(e.get("usage"), dict) and e.get("has_usage", True) is True
             and not e.get("error") and not e.get("maybe_billed")]
    def total(key):
        vals = [_number(e["usage"].get(key)) for e in valid]
        return sum(vals) if vals and all(v is not None and v >= 0 for v in vals) else None
    latencies = [_number(e.get("latency_ms")) for e in responses]
    latencies = [v for v in latencies if v is not None]
    estimated = [_number(e.get("prompt_tokens_est")) for e in events if e["type"] == "llm_request"]
    by_source = {}
    wakes = {e["wake_id"]: e for e in events if e["type"] == "wake" and isinstance(e.get("wake_id"), str)}
    for event in events:
        if event["type"] == "wake":
            reason = str(event.get("reason") or "unknown")
            by_source.setdefault(reason, {"wakes": 0, "calls": 0})["wakes"] += 1
        elif event["type"] == "llm_request":
            key = event.get("wake_id")
            reason = str(wakes.get(key, {}).get("reason") or "unknown") if isinstance(key, str) else "unknown"
            by_source.setdefault(reason, {"wakes": 0, "calls": 0})["calls"] += 1
    return {"events": len(events), "inputs": types.get("input", 0),
            "sensor_inputs": sum(e["type"] == "input" and e.get("kind") == "sensor" for e in events),
            "wakes": types.get("wake", 0), "suppressed": types.get("suppress", 0),
            "llm_calls": types.get("llm_request", 0), "tool_calls": types.get("tool_call", 0),
            "tool_results_ok": sum(e["type"] == "tool_result" and e.get("ok") is True for e in events),
            "errors": sum(e["type"] == "error" or bool(e.get("error")) for e in events),
            "prompt_tokens": total("prompt_tokens"), "completion_tokens": total("completion_tokens"),
            "cached_tokens": total("cached_tokens"), "reasoning_tokens": total("reasoning_tokens"),
            "total_tokens": total("total_tokens"), "usage_calls": len(valid),
            "usage_complete": bool(responses) and len(valid) == len(responses) == types.get("llm_request", 0),
            "maybe_billed_calls": sum(bool(e.get("maybe_billed")) for e in responses),
            "prompt_tokens_est": sum(estimated) if estimated and all(v is not None for v in estimated) else None,
            "latency_ms_mean": sum(latencies) / len(latencies) if latencies else None,
            "cost_rub": None, "by_source": by_source, "scope": "recorded_events",
            "measurement": "local_simulation" if kind == "experiment" else "trace",
            "tokens_basis": "mock_usage_estimate" if kind == "experiment" else "reported_usage",
            "physical_success": None, "energy_j": None}


def normalize(rows, label, provenance, kind="trace", origin="JSONL import", run_id=None, config=None):
    """Only explicit reference IDs form edges. Sequence and proximity form no edges."""
    events, observations, contexts = [], [], []
    indexes = {"input": {}, "wake": {}, "request": {}, "call": {}}
    warnings = []
    starts = [r for r in rows if r["type"] == "run_start"]
    start = starts[0] if starts else {}
    clock = "virtual" if start.get("clock") == "virtual" else "wall"
    clock_known = start.get("clock") in ("virtual", "real", "wall")
    if not starts:
        warnings.append("Нет run_start: часы и конфигурация могут быть неизвестны")
    def remember(group, key, event):
        if not isinstance(key, str) or not key:
            return
        if key in indexes[group]:
            indexes[group][key] = None  # ambiguous reference must never create a false link
            warnings.append("Повторный %s id: %s" % (group, key))
        else:
            indexes[group][key] = event["id"]
    for no, row in enumerate(rows, 1):
        event = copy.deepcopy(row)
        if "id" in event:
            event["record_id"] = event.pop("id")
        event["id"] = "e%06d" % no
        event["ordinal"] = no - 1
        # EdgeCtx real ts_clock is seconds from its epoch/day, not Unix time.
        # Never mix a virtual seconds axis with the wall-clock recording date.
        time_fields = ("ts_clock", "t") if clock == "virtual" else ("ts_wall", "t") if clock_known else ("t", "ts_wall")
        event["t"] = next((value for value in (_number(row.get(key)) for key in time_fields) if value is not None), None)
        if row["type"] == "input":
            if "input_id" not in event and isinstance(row.get("id"), str):
                event["input_id"] = row["id"]
            remember("input", event.get("input_id"), event)
        if row["type"] == "wake":
            remember("wake", event.get("wake_id"), event)
        if row["type"] == "llm_request":
            remember("request", event.get("req_id"), event)
        if row["type"] == "tool_call":
            remember("call", event.get("call_id"), event)
        event["links"] = []
        events.append(event)
        if row["type"] == "input" and row.get("kind") == "sensor" and "value" in row:
            observations.append({"event_id": event["id"], "t": event["t"], "device": row.get("device"),
                                 "property": row.get("property"), "value": copy.deepcopy(row["value"]),
                                 "unit": row.get("unit"), "origin": "agent_input"})
        if row["type"] == "llm_request":
            snap = row.get("context_snapshot")
            available = isinstance(snap, dict) and snap.get("complete") is True and isinstance(snap.get("messages"), list)
            contexts.append({"event_id": event["id"], "req_id": row.get("req_id"), "t": event["t"],
                             "observed_through": len(observations),
                             "prompt": {"available": available,
                                        "snapshot_event_id": event["id"] if isinstance(snap, dict) else None,
                                        "reason": None if available else "Полный контекст этого запроса не записан"}})
    for event in events:
        refs = []
        if event["type"] != "input" and isinstance(event.get("input_id"), str):
            refs.append(("input", event["input_id"]))
        if isinstance(event.get("input_ids"), list):
            refs.extend(("input", key) for key in event["input_ids"] if isinstance(key, str))
        if isinstance(event.get("coalesced"), list):
            refs.extend(("wake", key) for key in event["coalesced"] if isinstance(key, str))
        for group, field, own_type in (("wake", "wake_id", "wake"), ("request", "req_id", "llm_request"),
                                     ("call", "call_id", "tool_call")):
            if event["type"] != own_type and isinstance(event.get(field), str):
                refs.append((group, event[field]))
        # A later or duplicate reference is not a supported dependency.
        for group, key in dict.fromkeys(refs):
            target = indexes[group].get(key)
            if target is not None and target < event["id"]:
                event["links"].append({"id": target, "relation": group})
    times = [event["t"] for event in events if event["t"] is not None]
    completed = (len(starts) == 1 and events[0]["type"] == "run_start" and events[-1]["type"] == "run_end"
                 and sum(e["type"] == "run_end" for e in events) == 1)
    prov = {**copy.deepcopy(provenance), "imported_at": time.time(), "complete_trace": completed,
            "warnings": warnings[:100], "warning_count": len(warnings),
            "limitations": ["Ссылки отражают записанные зависимости, не доказанную физическую причинность",
                            "Observed — входы агента; полное исходное состояние оборудования неизвестно",
                            "Replay интерфейса не является исполняемой контрольной точкой",
                            "Скрытые рассуждения LLM недоступны"]}
    return {"id": run_id or "run-" + uuid.uuid4().hex[:16], "label": label, "kind": kind,
            "origin": origin, "clock": clock, "clock_known": clock_known,
            "t_start": min(times) if times else None, "t_end": max(times) if times else None,
            "status": "completed" if completed else "partial", "events": events,
            "config": copy.deepcopy(config) if config is not None else
                      {key: copy.deepcopy(start[key]) for key in ("policy", "budget", "sched", "model", "clock", "config_hash") if key in start},
            "metrics": _metrics(events, kind), "observations": observations, "contexts": contexts,
            "provenance": prov}


def experiment_spec(body):
    if not isinstance(body, dict):
        raise LabError("Ожидается JSON-объект")
    allowed = {"scenario", "policy", "threshold", "debounce_s", "heartbeat_s", "duration_s", "label", "seed", "history", "window_turns"}
    if set(body) - allowed:
        raise LabError("Неизвестные параметры эксперимента: " + ", ".join(sorted(set(body) - allowed)))
    scenario = body.get("scenario", "noise-burst")
    policy = body.get("policy", "gate")
    if not isinstance(scenario, str) or scenario not in {s["id"] for s in SCENARIOS} or policy not in ("gate", "heartbeat", "hybrid"):
        raise LabError("Неизвестный сценарий или политика")
    out = {"scenario": scenario, "policy": policy}
    for key, default, low, high in (("threshold", 30, 20, 100), ("debounce_s", 2, 0, 10),
                                   ("heartbeat_s", 30, 5, 300), ("duration_s", 180, 60, 600)):
        value = _number(body.get(key, default))
        if value is None or not low <= value <= high:
            raise LabError("%s должен быть числом от %s до %s" % (key, low, high))
        out[key] = value
    seed = body.get("seed", 7)
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= 2147483647:
        raise LabError("seed должен быть целым от 0 до 2147483647")
    out["seed"] = seed
    history = body.get("history", "mask")
    if history not in ("full", "window", "mask", "summary_system"):
        raise LabError("history должен быть full, window, mask или summary_system")
    window_turns = body.get("window_turns", 8)
    if not isinstance(window_turns, int) or isinstance(window_turns, bool) or not 1 <= window_turns <= 32:
        raise LabError("window_turns должен быть целым от 1 до 32")
    out.update(history=history, window_turns=window_turns)
    out["label"] = _label(body.get("label"), "%s · %s" % (scenario, policy))
    return out


def _summary(run):
    return {k: copy.deepcopy(run[k]) for k in ("id", "label", "kind", "origin", "clock", "t_start", "t_end", "metrics", "status", "revision", "stale") if k in run}


class LabStore:
    def __init__(self, demo=False, allow_experiments=True, work_dir=None, python=None, binary=None):
        self.allow_experiments = bool(allow_experiments)
        self._temp = tempfile.TemporaryDirectory(prefix="edgelab-", dir=str(work_dir) if work_dir else None)
        self.work_dir = Path(self._temp.name)
        self.python = str(python or os.environ.get("EDGELAB_PYTHON") or sys.executable)
        self.binary = str(binary) if binary else None
        self._lock = threading.RLock()
        self._runs, self._sizes, self._jobs, self._exports, self._watchers = {}, {}, {}, {}, {}
        self._closed = False
        self._thread = None
        self._proc = None
        self._cancel = threading.Event()
        if demo:
            # Explicitly synthetic import, never presented as an empirical experiment.
            self.add_trace(name="ДЕМО · структура трассы (синтетика)", events=[
                {"type": "run_start", "clock": "virtual", "run_id": "synthetic-example", "ts_clock": 0},
                {"type": "input", "id": "i1", "kind": "sensor", "device": "example", "property": "temperature", "value": 24, "ts_clock": 1},
                {"type": "suppress", "input_id": "i1", "reason": "below_threshold", "ts_clock": 1},
                {"type": "run_end", "ts_clock": 2}], origin="SYNTHETIC DEMONSTRATION, not measured")

    def snapshot(self):
        with self._lock:
            return {"runs": [_summary(r) for r in self._runs.values()], "jobs": copy.deepcopy(list(self._jobs.values())),
                    "watchers": [copy.deepcopy(w["public"]) for w in self._watchers.values()],
                    "scenarios": copy.deepcopy(SCENARIOS),
                    "limits": {"import_bytes": MAX_IMPORT_BYTES, "events": MAX_EVENTS, "runs": MAX_RUNS,
                               "store_bytes": MAX_STORE_BYTES, "jobs": MAX_JOBS, "concurrent_jobs": 1,
                               "experiment_wall_s": MAX_JOB_SECONDS, "allow_experiments": self.allow_experiments,
                               "watchers": MAX_WATCHERS, "watch_poll_s": WATCH_POLL_S}}

    def run(self, run_id):
        with self._lock:
            return copy.deepcopy(self._runs.get(run_id))

    def export(self, run_id):
        """Original JSONL bytes, including original IDs/unknown fields, or None."""
        with self._lock:
            return self._exports.get(run_id)

    def _insert(self, run, source=b"", replace=False):
        size = len(_dump(run)) + len(source) + 2048  # reserve bounded watcher error/status growth
        with self._lock:
            if self._closed:
                raise LabError("Лаборатория остановлена", 503)
            old_size = self._sizes.get(run["id"], 0) if replace else 0
            is_new = run["id"] not in self._runs
            if (is_new and len(self._runs) >= MAX_RUNS) or sum(self._sizes.values()) - old_size + size > MAX_STORE_BYTES:
                raise LabError("Хранилище лаборатории заполнено: предел 16 прогонов / 32 MiB", 413)
            self._runs[run["id"]] = run
            self._sizes[run["id"]] = size
            self._exports[run["id"]] = source
        return _summary(run)

    def watch_trace(self, path, label=None):
        """Trusted startup hook only. No HTTP route accepts a filesystem path."""
        if not isinstance(path, (str, Path)) or not str(path) or "\x00" in str(path):
            raise LabError("Укажите путь к локальной JSONL-трассе")
        location = Path(path).expanduser().absolute()
        name = _label(label, location.name[:160] or "EdgeCtx live")
        with self._lock:
            if self._closed:
                raise LabError("Лаборатория остановлена", 503)
            for existing in self._watchers.values():
                if existing["path"] == location:
                    return copy.deepcopy(existing["public"])
            if len(self._watchers) >= MAX_WATCHERS:
                raise LabError("Можно наблюдать не больше %d файлов трасс" % MAX_WATCHERS, 413)
            watcher_id = "watch-" + uuid.uuid4().hex[:16]
            watcher = {"path": location, "stop": threading.Event(), "identity": None, "signature": None,
                       "prefix_hash": None, "prefix_size": 0, "raw_size": 0,
                       "public": {"id": watcher_id, "label": name, "status": "waiting", "error": None,
                                  "run_id": None, "revision": 0, "last_success": None}}
            self._watchers[watcher_id] = watcher
            thread = threading.Thread(target=self._watch_loop, args=(watcher_id,), name="edgelab-trace-" + watcher_id, daemon=True)
            watcher["thread"] = thread
            thread.start()
            return copy.deepcopy(watcher["public"])

    def _watch_reset(self, watcher, reason):
        public = watcher["public"]
        previous = self._runs.get(public["run_id"])
        if previous is not None:
            if previous["status"] == "live":
                previous["status"] = "partial"
            previous["provenance"]["live_end_reason"] = reason
        public.update(run_id=None, revision=0)
        watcher.update(prefix_hash=None, prefix_size=0, raw_size=0, signature=None)

    def _watch_loop(self, watcher_id):
        watcher = self._watchers[watcher_id]
        while not watcher["stop"].is_set():
            self._poll_trace(watcher_id)
            watcher["stop"].wait(WATCH_POLL_S)

    def _poll_trace(self, watcher_id):
        """One atomic bounded refresh; callable synchronously by tests."""
        watcher = self._watchers[watcher_id]
        public = watcher["public"]
        signature = None
        try:
            # O_NONBLOCK + fstat prevents a replaced FIFO/device from blocking a watcher.
            descriptor = os.open(str(watcher["path"]), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise LabError("Источник live-трассы должен быть обычным файлом")
            except BaseException:
                os.close(descriptor)
                raise
            with os.fdopen(descriptor, "rb") as stream:
                if info.st_size > MAX_IMPORT_BYTES:
                    raise LabError("Live-трасса больше 8 MiB; выполните ротацию файла", 413)
                signature = (info.st_dev, info.st_ino, info.st_size, getattr(info, "st_mtime_ns", info.st_mtime))
                with self._lock:
                    if self._closed or watcher["stop"].is_set():
                        return
                    if signature == watcher["signature"]:
                        if public["run_id"] is not None:
                            public["last_success"] = time.time()
                        return
                    if signature == watcher.get("error_signature"):
                        return
                raw = stream.read(MAX_IMPORT_BYTES + 1)
                if len(raw) > MAX_IMPORT_BYTES:
                    raise LabError("Live-трасса больше 8 MiB; выполните ротацию файла", 413)
            identity = (info.st_dev, info.st_ino)
            with self._lock:
                previous_size = watcher["prefix_size"]
                changed_identity = watcher["identity"] is not None and watcher["identity"] != identity
                rewritten = previous_size and (len(raw) < previous_size or
                            hashlib.sha256(raw[:previous_size]).hexdigest() != watcher["prefix_hash"])
                if changed_identity or len(raw) < watcher["raw_size"] or rewritten:
                    self._watch_reset(watcher, "file_rotated_or_rewritten")
                watcher["identity"] = identity
            # Only an unfinished last line is withheld. A malformed newline-terminated line is an error.
            end = raw.rfind(b"\n") + 1
            complete = raw[:end]
            digest = hashlib.sha256(complete).hexdigest()
            with self._lock:
                if not complete or not complete.strip():
                    public.update(status="waiting", error=None)
                    watcher.update(signature=signature, raw_size=len(raw))
                    return
                if digest == watcher["prefix_hash"]:
                    watcher.update(signature=signature, raw_size=len(raw))
                    previous = self._runs.get(public["run_id"])
                    if previous is not None:
                        previous.pop("stale", None)
                        previous["provenance"].pop("watch_error", None)
                        previous["provenance"]["pending_bytes"] = len(raw) - len(complete)
                        public.update(status="completed" if previous["status"] == "completed" else "watching",
                                      error=None, last_success=time.time())
                    watcher.pop("error_signature", None)
                    return
            try:
                text = complete.decode("utf-8")
            except UnicodeError:
                raise LabError("Live-трасса содержит некорректный UTF-8")
            rows, provenance = parse_trace(content=text)
            if sum(row["type"] == "run_start" for row in rows) > 1:
                raise LabError("В live-файле несколько run_start: выполните ротацию, прогоны не объединяются")
            with self._lock:
                if self._closed or watcher["stop"].is_set():
                    return
                run_id = public["run_id"] or "run-" + uuid.uuid4().hex[:16]
                revision = public["revision"] + 1
            provenance.update(live=True, watcher_id=watcher_id, source_filename=watcher["path"].name,
                              captured_through_bytes=len(complete), pending_bytes=len(raw) - len(complete))
            run = normalize(rows, public["label"], provenance, origin="LIVE FILE · " + watcher["path"].name,
                            run_id=run_id, kind="live")
            run["status"] = "completed" if run["provenance"]["complete_trace"] else "live"
            run["revision"] = revision
            with self._lock:
                self._insert(run, complete, replace=True)
                public.update(status="completed" if run["status"] == "completed" else "watching", error=None,
                              run_id=run_id, revision=revision, last_success=time.time())
                watcher.update(signature=signature, prefix_hash=digest, prefix_size=len(complete), raw_size=len(raw))
                watcher.pop("error_signature", None)
        except FileNotFoundError:
            with self._lock:
                if watcher["identity"] is not None:
                    self._watch_reset(watcher, "file_missing")
                    watcher["identity"] = None
                public.update(status="waiting", error=None)
        except Exception as exc:
            with self._lock:
                public.update(status="error", error=str(exc)[:1000])
                watcher["error_signature"] = signature
                previous = self._runs.get(public["run_id"])
                if previous is not None:
                    previous["provenance"]["watch_error"] = str(exc)[:1000]
                    previous["stale"] = True

    def add_trace(self, name=None, content=None, events=None, origin="JSONL import"):
        label = _label(name, "Импортированная трасса")
        rows, prov = parse_trace(content, events)
        source = content.encode("utf-8") if content is not None else b"\n".join(_dump(r) for r in rows) + b"\n"
        return self._insert(normalize(rows, label, prov, origin=origin), source)

    def start(self, body):
        spec = experiment_spec(body)
        with self._lock:
            if self._closed:
                raise LabError("Лаборатория остановлена", 503)
            if not self.allow_experiments:
                raise LabError("Запуск экспериментов отключён на этом сервере", 403)
            if any(j["status"] in ("queued", "running", "cancelling") for j in self._jobs.values()):
                raise LabError("Уже выполняется эксперимент; дождитесь окончания или отмените его", 409)
            if len(self._jobs) >= MAX_JOBS or len(self._runs) >= MAX_RUNS:
                raise LabError("Достигнут предел заданий или прогонов лаборатории", 413)
            job_id = "job-" + uuid.uuid4().hex[:16]
            job = {"id": job_id, "status": "queued", "spec": spec, "created_at": time.time(),
                   "started_at": None, "finished_at": None, "run_id": None, "error": None,
                   "phase": "queued", "execution": "actual edgectx + local sim-mcp + mock-llm oracle"}
            self._jobs[job_id] = job
            self._cancel = threading.Event()
            self._thread = threading.Thread(target=self._execute, args=(job_id,), name="edgelab-job", daemon=True)
            self._thread.start()
            return copy.deepcopy(job)

    def cancel(self, job_id):
        if not isinstance(job_id, str):
            raise LabError("Укажите id задания")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise LabError("Задание не найдено", 404)
            if job["status"] in ("queued", "running", "cancelling"):
                job["status"] = "cancelling"
                job["phase"] = "cancelling"
                self._cancel.set()
            return copy.deepcopy(job)

    @staticmethod
    def _stop(proc):
        if proc is None:
            return
        # Runner creates a process group; children inherit it. Only our own group is signalled.
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            elif proc.poll() is None:
                proc.terminate()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            elif proc.poll() is None:
                proc.kill()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass

    def _execute(self, job_id):
        proc = None
        outcome = {"status": "failed", "phase": "failed", "error": "Эксперимент не завершён"}
        directory = self.work_dir / job_id
        try:
            directory.mkdir()
            with self._lock:
                job = self._jobs[job_id]
                if self._cancel.is_set():
                    outcome = {"status": "cancelled", "phase": "cancelled", "error": None}
                    return
                job.update(status="running", phase="starting", started_at=time.time())
                spec = copy.deepcopy(job["spec"])
            (directory / "spec.json").write_bytes(_dump(spec))
            args = [self.python, "-m", "neurobot.ui.lab_runner", "--work", str(directory)]
            if self.binary:
                args.extend(["--binary", self.binary])
            env = dict(os.environ)
            # Never inherit a provider credential or upstream selection into the bench process.
            env.pop("ROUTERAI_API_KEY", None)
            env.pop("OPENAI_API_KEY", None)
            env["GOPROXY"], env["GOSUMDB"], env["GOTOOLCHAIN"] = "off", "off", "local"
            with (directory / "runner.log").open("wb") as log:
                proc = subprocess.Popen(args, cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=(os.name == "posix"))
                with self._lock:
                    self._proc = proc
                deadline = time.monotonic() + MAX_JOB_SECONDS
                while proc.poll() is None:
                    if self._cancel.wait(0.1):
                        outcome = {"status": "cancelled", "phase": "cancelled", "error": None}
                        return
                    if time.monotonic() >= deadline:
                        raise LabError("Эксперимент превысил предел %d секунд" % MAX_JOB_SECONDS, 504)
                    # Files are ours, not supplied by an import. Prevent runaway simulator logs.
                    if sum(p.stat().st_size for p in directory.rglob("*") if p.is_file()) > MAX_ARTIFACT_BYTES:
                        raise LabError("Артефакты эксперимента превысили 24 MiB", 413)
                    phase = directory / "phase.json"
                    if phase.exists() and phase.stat().st_size < 4096:
                        try:
                            current = json.loads(phase.read_text()).get("phase")
                            if isinstance(current, str) and len(current) < 128:
                                with self._lock:
                                    self._jobs[job_id]["phase"] = current
                        except (ValueError, OSError):
                            pass
            result_path = directory / "result.json"
            if not result_path.exists():
                tail = (directory / "runner.log").read_bytes()[-1800:].decode("utf-8", "replace")
                raise LabError("Не удалось запустить локальный стенд: " + tail)
            if result_path.stat().st_size > MAX_IMPORT_BYTES:
                raise LabError("Ответ стенда превысил 8 MiB", 413)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if proc.returncode or result.get("error"):
                raise LabError(str(result.get("error") or "Стенд завершился с ошибкой")[:1800])
            rows, prov = parse_trace(events=result.get("events"))
            prov.update(result.get("provenance") or {})
            run = normalize(rows, spec["label"], prov, kind="experiment",
                            origin="LOCAL SIMULATION · actual EdgeCtx + sim-mcp + mock-llm oracle",
                            config=result.get("config"))
            run["evaluation"] = result.get("evaluation")
            run["scenario_truth"] = result.get("scenario_truth")
            run["resources"] = result.get("resources")
            resources = run["resources"] or {}
            run["metrics"].update(agent_rss_sampled_max_bytes=resources.get("rss_sampled_max_bytes"),
                                   agent_cpu_observed_s=resources.get("cpu_observed_s"),
                                   resources_basis="diagnostic_host_agent_pid")
            run["scenario"] = spec
            if not run["provenance"]["complete_trace"]:
                raise LabError("Стенд не записал завершённую трассу run_start → run_end")
            with self._lock:
                if self._cancel.is_set() or self._closed:
                    outcome = {"status": "cancelled", "phase": "cancelled", "error": None}
                    return
                summary = self._insert(run, b"\n".join(_dump(row) for row in rows) + b"\n")
                outcome = {"status": "completed", "phase": "completed", "error": None, "run_id": summary["id"]}
        except Exception as exc:  # lifecycle failures are explicit job outcomes, not HTTP thread failures
            with self._lock:
                cancelled = self._cancel.is_set()
                outcome = {"status": "cancelled" if cancelled else "failed", "phase": "cancelled" if cancelled else "failed",
                           "error": None if cancelled else str(exc)[:2000]}
        finally:
            self._stop(proc)
            with self._lock:
                self._proc = None
                self._jobs[job_id].update(outcome, finished_at=time.time())

    def close(self):
        with self._lock:
            self._closed = True
            self._cancel.set()
            thread = self._thread
            watchers = list(self._watchers.values())
            for watcher in watchers:
                watcher["stop"].set()
        for watcher in watchers:
            watcher["thread"].join(timeout=2)
        if thread is not None:
            thread.join(timeout=8)
        if thread is not None and thread.is_alive():
            self._stop(self._proc)
            thread.join(timeout=2)
        if thread is None or not thread.is_alive():
            self._temp.cleanup()


def mutate(store, path, body):
    try:
        if not isinstance(body, dict):
            raise LabError("Ожидается JSON-объект")
        if path == "/api/lab/import":
            if set(body) - {"name", "content", "events"}:
                raise LabError("Импорт принимает только name, content или events; серверные пути запрещены")
            return 201, {"run": store.add_trace(name=body.get("name"), content=body.get("content"), events=body.get("events"))}
        if path == "/api/lab/experiments":
            return 202, {"job": store.start(body)}
        if path == "/api/lab/cancel":
            if set(body) != {"id"}:
                raise LabError("Для отмены передайте только id задания")
            return 200, {"job": store.cancel(body["id"])}
        raise LabError("Неизвестный маршрут лаборатории", 404)
    except LabError as exc:
        return exc.status, {"error": str(exc)}
