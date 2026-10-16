"""Чтение файла дня (спецификация §2): JSONL, первая строка — заголовок {"kind": "header", ...}.

Время событий t — секунды от начала суток (виртуальные часы). Окно заголовка допускается в виде
["07:00", "23:00"], ["07:00:00", "23:00:00"] или [25200, 82800] (секунды).
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

USER_KINDS = ("user_cmd", "question")
KNOWN_KINDS = ("user_cmd", "question", "sensor", "situation")
# Порядок при равных t: ситуация регистрируется раньше, чем sim-mcp опубликует датчик того же момента, —
# иначе пробуждение гейта на этом датчике не увидит ситуацию (она ещё не зарегистрирована в оракуле).
KIND_ORDER = {"situation": 0}


def parse_clock(v) -> float:
    """'07:00' | '07:00:30' | число секунд → секунды от полуночи."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        parts = v.strip().split(":")
        if 2 <= len(parts) <= 3:
            h, m = int(parts[0]), int(parts[1])
            s = float(parts[2]) if len(parts) == 3 else 0.0
            return h * 3600 + m * 60 + s
        return float(v)
    raise ValueError(f"cannot parse clock value {v!r}")


def header_window(header: dict) -> tuple[float, float] | None:
    """Окно дня из заголовка: window_s [с, с] (daygen) или window ["07:00", "23:00"]."""
    for key in ("window_s", "window"):
        w = header.get(key)
        if isinstance(w, (list, tuple)) and len(w) == 2:
            return parse_clock(w[0]), parse_clock(w[1])
    return None


@dataclass
class DayEvent:
    t: float
    kind: str
    raw: dict
    line: int

    @property
    def id(self) -> str | None:
        v = self.raw.get("id")
        return None if v is None else str(v)


@dataclass
class Day:
    path: Path
    header: dict
    events: list[DayEvent]
    sha256: str
    window: tuple[float, float] | None = None
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.events:
            out[e.kind] = out.get(e.kind, 0) + 1
        return out

    def span(self) -> tuple[float, float]:
        """Отрезок проигрывания: от min(начало окна, первое событие) до max(конец окна, последнее событие)."""
        ts = [e.t for e in self.events]
        lo = min([*ts, self.window[0]] if self.window else ts or [0.0])
        hi = max([*ts, self.window[1]] if self.window else ts or [0.0])
        return float(lo), float(hi)

    def by_kind(self, kind: str) -> list[DayEvent]:
        return [e for e in self.events if e.kind == kind]


def load_day(path: str | Path) -> Day:
    p = Path(path)
    data = p.read_bytes()
    header: dict = {}
    events: list[DayEvent] = []
    warnings: list[str] = []
    for no, line in enumerate(data.decode("utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError as e:
            raise ValueError(f"{p}:{no}: not JSON: {e}") from e
        if not isinstance(obj, dict):
            raise ValueError(f"{p}:{no}: not a JSON object")
        if obj.get("kind") == "header":
            if header:
                raise ValueError(f"{p}:{no}: second header")
            header = obj
            continue
        if "t" not in obj or "kind" not in obj:
            raise ValueError(f"{p}:{no}: event without t/kind")
        kind = str(obj["kind"])
        if kind not in KNOWN_KINDS:
            warnings.append(f"line {no}: unknown kind {kind!r} (будет только продвинуто время)")
        events.append(DayEvent(float(obj["t"]), kind, obj, no))
    events.sort(key=lambda e: (e.t, KIND_ORDER.get(e.kind, 1), e.line))   # устойчиво при равных t
    window = header_window(header)
    if window is not None and window[1] <= window[0]:
        raise ValueError(f"{p}: window end <= start: {header.get('window_s') or header.get('window')}")
    ids = [e.id for e in events if e.kind in (*USER_KINDS, "situation") and e.id is not None]
    dup = sorted(i for i, c in Counter(ids).items() if c > 1)
    if dup:
        raise ValueError(f"{p}: duplicate input ids: {dup[:10]}")
    for e in events:
        if e.kind in (*USER_KINDS, "situation") and e.id is None:
            raise ValueError(f"{p}:{e.line}: {e.kind} without id")
    return Day(p, header, events, hashlib.sha256(data).hexdigest(), window, warnings)
