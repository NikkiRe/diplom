"""Сбор артефактов прогона в <out>/<run_id>/ и manifest.json (спецификация §5.1).

Файлы каталога прогона:
    day.jsonl               копия файла дня;
    replay.jsonl            журнал реплеера (шаги часов, ожидания, реплики и их RTT, ошибки);
    mockllm_journal.jsonl   журнал mock-llm этого прогона (GET /control/journal?since=<seq после reset>);
    mockllm_state.json      состояние mock-llm в конце прогона;
    edgectx_metrics.json    GET edgectx /v1/metrics в конце прогона, до /v1/shutdown (если отвечает);
    simmcp_report.json      POST sim-mcp /control/finish — оценка по истинному состоянию (если отвечает);
    <метка>.<расширение>    файлы из --collect (трасса edgectx, трассы hw/sim-mcp, sysmon, отчёт монитора);
    manifest.json           конфиги (целиком), seed и заголовок дня, SHA-256 бинарников и всех файлов, версии.
"""
from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

MANIFEST_VERSION = 1
MAX_INLINE_CONFIG = 256 * 1024


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_info(path: str | Path, inline: bool = False) -> dict:
    p = Path(path)
    if not p.exists():
        return {"path": str(p), "missing": True}
    info = {"path": str(p.resolve()), "sha256": sha256_file(p), "bytes": p.stat().st_size}
    if inline and info["bytes"] <= MAX_INLINE_CONFIG:
        try:
            info["content"] = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            pass
    return info


def _git_commit(root: Path) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def _warnings(rp, remote: dict) -> list[str]:
    """Предупреждения о точности прогона (не ошибки): время mock-llm без заголовка часов при settle_ms = 0,
    промахи синхронизации с уведомлениями sim-mcp."""
    out = []
    mock = remote.get("mock_llm") or {}
    missing = int(mock.get("clock_header_missing") or 0)
    if missing:
        out.append(f"mock-llm: {missing} запрос(ов) edgectx без X-Edgectx-Clock-T — TTL кэша по /control/clock "
                   f"(конец шага)" + ("; включите llm.trace_headers или --settle-ms 25" if not rp.cfg.settle_ms else ""))
    sync = (rp.stats or {}).get("sync") or {}
    if sync.get("missing") or sync.get("timeouts"):
        out.append(f"синхронизация с sim-mcp: промахов min_inputs {sync.get('missing', 0)}, таймаутов "
                   f"{sync.get('timeouts', 0)} (режим {sync.get('mode')}, переход на updates при t="
                   f"{sync.get('fallback_at_t')})")
    sd = getattr(rp, "shutdown_info", None) or {}
    if sd.get("requested") and sd.get("run_end") is False:
        out.append("трасса edgectx без run_end после /v1/shutdown — монитор её не примет")
    return out


def collect_run(rp, started: float, finished: float) -> dict:
    """rp — Replayer после прогона. Возвращает manifest (и пишет manifest.json)."""
    run_dir: Path = rp.run_dir
    shutil.copyfile(rp.day.path, run_dir / "day.jsonl")
    remote: dict = {}
    if rp.mock is not None:
        try:   # тело — NDJSON, а не JSON: берём сырые байты; только записи этого прогона (seq > since)
            since = int(getattr(rp, "mock_journal_since", 0) or 0)
            status, _, raw, _ = rp.mock.get(f"/control/journal?since={since}", timeout_s=rp.cfg.http_timeout_s)
            if status == 200:
                (run_dir / "mockllm_journal.jsonl").write_bytes(raw)
            else:
                rp.errors.append(f"mock journal: HTTP {status}")
        except Exception as e:  # noqa: BLE001 — сбор не должен ронять прогон
            rp.errors.append(f"mock journal: {e}")
        r = rp._call(rp.mock, "GET", "/control/state", required=False)
        if r is not None and isinstance(r[1], dict):
            remote["mock_llm"] = r[1]
            (run_dir / "mockllm_state.json").write_text(json.dumps(r[1], ensure_ascii=False, indent=2) + "\n",
                                                        encoding="utf-8")
    if getattr(rp, "sim_report", None):
        (run_dir / "simmcp_report.json").write_text(json.dumps(rp.sim_report, ensure_ascii=False, indent=1) + "\n",
                                                    encoding="utf-8")
    edge_metrics = getattr(rp, "edge_metrics", None)
    if edge_metrics is None and rp.edge is not None and not (getattr(rp, "shutdown_info", None) or {}).get("requested"):
        r = rp._call(rp.edge, "GET", "/v1/metrics", required=False)
        edge_metrics = r[1] if r is not None else None
    if edge_metrics is not None:
        remote["edgectx_metrics"] = edge_metrics
        (run_dir / "edgectx_metrics.json").write_text(json.dumps(edge_metrics, ensure_ascii=False, indent=2) + "\n",
                                                      encoding="utf-8")
    collected = {}
    for label, src in rp.cfg.collect.items():
        p = Path(src)
        if not p.exists():
            rp.errors.append(f"collect {label}: not found: {src}")
            collected[label] = {"source": str(src), "missing": True}
            continue
        dst = run_dir / (label + "".join(p.suffixes[-1:]))
        shutil.copyfile(p, dst)
        collected[label] = {"source": str(p.resolve()), "file": dst.name}
    root = Path(__file__).resolve().parents[2]
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "run_id": rp.run_id,
        "status": ("completed_with_errors" if rp.errors else "completed") if rp.completed else "aborted",
        "completed": rp.completed,
        "mode": "T0",
        "dev_run": True,   # прогоны при разработке — не результаты (спецификация §0.4); снять в протоколе
        "created": _iso(started),
        "finished": _iso(finished),
        "duration_wall_s": finished - started,
        "requested_window_s": list(rp.requested_window) if rp.requested_window else None,
        "played_window_s": [rp.requested_window[0], rp.cur] if rp.requested_window and rp.cur is not None else None,
        "day": {"source": str(rp.day.path.resolve()), "sha256": rp.day.sha256, "header": rp.day.header,
                "window_s": list(rp.day.window) if rp.day.window else None, "span_s": list(rp.day.span()),
                "counts": rp.day.counts(), "warnings": rp.day.warnings,
                "seed": rp.day.header.get("seed"), "profile": rp.day.header.get("profile"),
                "home": rp.day.header.get("home"), "split": rp.day.header.get("split")},
        "endpoints": {"edgectx": rp.cfg.edgectx, "sim": rp.cfg.sim, "mock": rp.cfg.mock},
        "replay_params": {k: getattr(rp.cfg, k) for k in (
            "max_step_s", "settle_ms", "settle_timeout_s", "sync", "sync_timeout_s", "shutdown", "shutdown_timeout_s",
            "user_timeout_s", "http_timeout_s", "situation_eps_s", "expire_situations", "sensor_mode", "reset_mock",
            "window_end", "dry_run")},
        "replay_stats": rp.stats,
        "edgectx_shutdown": getattr(rp, "shutdown_info", None),
        "mock_journal_since": getattr(rp, "mock_journal_since", 0),
        "configs": {k: _file_info(v, inline=True) for k, v in rp.cfg.configs.items()},
        "binaries": {k: _file_info(v) for k, v in rp.cfg.binaries.items()},
        "collected": collected,
        "versions": {"python": sys.version.split()[0], "platform": platform.platform(),
                     "machine": platform.machine(), "replayer": "bench.replay 1.0",
                     "mock_llm": (remote.get("mock_llm") or {}).get("version"),
                     "git_commit": _git_commit(root)},
        "remote_state": remote,
        "errors": rp.errors,
        "warnings": _warnings(rp, remote),
    }
    manifest["files"] = {p.name: {"sha256": sha256_file(p), "bytes": p.stat().st_size}
                         for p in sorted(run_dir.iterdir()) if p.is_file() and p.name != "manifest.json"}
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest
