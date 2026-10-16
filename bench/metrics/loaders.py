"""Чтение артефактов прогона: трасса edgectx (§6), журнал mock-llm, sysmon.v1, файл дня, отчёты монитора
и трассы шлюза. Файлы ищутся в каталоге прогона по шаблонам имён (их пишет bench/replay), любой путь можно
задать явно."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PATTERNS = {
    "trace": ["edgectx_trace*.jsonl", "edgectx*.jsonl", "trace*.jsonl"],
    "journal": ["mockllm_journal*.jsonl", "mock*journal*.jsonl"],
    "sysmon": ["sysmon*.jsonl"],
    "day": ["day.jsonl", "day*.jsonl"],
    "replay": ["replay.jsonl"],
    "manifest": ["manifest.json"],
    "monitor": ["monitor*.json"],
    "sim_report": ["simmcp_report*.json", "sim_report*.json"],
    "gate": ["hwmcp*.jsonl", "hw_mcp*.jsonl", "simmcp*.jsonl", "sim_mcp*.jsonl", "*gate*.jsonl"],
}
MULTI = {"monitor", "gate"}
_FRACTION = re.compile(r"\.(\d+)")   # дробная часть секунд (только цифры сразу после точки)


@dataclass
class RunFiles:
    run_dir: Path
    paths: dict[str, Path | list[Path] | None] = field(default_factory=dict)

    def get(self, key: str):
        return self.paths.get(key)


def discover(run_dir: str | Path, overrides: dict | None = None) -> RunFiles:
    d = Path(run_dir)
    out: dict = {}
    for key, pats in PATTERNS.items():
        found: list[Path] = []
        for pat in pats:
            for p in sorted(d.glob(pat)):
                if p.is_file() and p not in found and (key != "trace" or "mockllm" not in p.name):
                    found.append(p)
        out[key] = found if key in MULTI else (found[0] if found else None)
    for key, val in (overrides or {}).items():
        if val is None or val == []:
            continue
        out[key] = [Path(v) for v in val] if key in MULTI else Path(val)
    return RunFiles(d, out)


def read_jsonl(path: str | Path | None, warnings: list[str] | None = None) -> list[dict]:
    if path is None:
        return []
    recs = []
    with open(path, encoding="utf-8") as f:
        for no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                if warnings is not None:
                    warnings.append(f"{Path(path).name}:{no}: not JSON, skipped")
                continue
            if isinstance(obj, dict):
                recs.append(obj)
    return recs


def read_json(path: str | Path | None) -> dict | None:
    if path is None:
        return None
    with open(path, encoding="utf-8") as f:
        obj = json.load(f)
    return obj if isinstance(obj, dict) else None


def epoch_seconds(v) -> float | None:
    """Время стенных часов: число (с или мс Unix) или строка ISO 8601/RFC 3339 → секунды Unix."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        return x / 1000.0 if x > 1e12 else x
    if isinstance(v, str):
        s = v.strip()
        try:
            return float(s)
        except ValueError:
            pass
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        # RFC 3339 с наносекундами (Go time.RFC3339Nano) — обрезать дробную часть до микросекунд
        m = _FRACTION.search(s)
        if m:
            s = s[:m.start()] + "." + m.group(1)[:6] + s[m.end():]
        try:
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            return None
    return None


def clock_seconds(v) -> float | None:
    """ts_clock трассы → секунды от полуночи (шкала t файла дня).

    Виртуальные часы T0 пишут секунды дня числом. Реальные часы (T2) — время Unix или RFC 3339; тогда берутся
    секунды от местной полуночи того же дня."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and float(v) < 1e7:
        return float(v)
    ep = epoch_seconds(v)
    if ep is None:
        return None
    dt = datetime.fromtimestamp(ep)
    return dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6
