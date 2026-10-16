"""Отбор событий датчиков для режима «пуш» у аналогов (MQTT-канал PicoClaw, local triggers nanobot).

У аналогов нет событийного гейта: что попало в канал, то и будит LLM. Внешний «мост» (в жизни — правило
Home Assistant/Node-RED, публикующее в MQTT) решает, что отправлять. Режимы:

* `signals` (по умолчанию) — те же предикаты значимости, что у сигналов гейта EdgeCtx
  (`policy.gate.signals` в bench/simmcp/edgectx_simday.yaml): change / threshold (above/below с гистерезисом,
  будит только пересечение вверх для above и вниз для below) / delta / episode («конец эпизода» после gap_s
  тишины) / ignore; не описанные свойства — `policy.gate.default`. **Без** debounce, cooldown, коалесценции,
  самоэха и правил — это механизмы EdgeCtx, их вклад и меряется;
* `all` — каждое событие датчика (граница B0-events);
* `none` — пуша нет (режим «опрос»: только heartbeat).

Текст события одинаков для обеих систем (format_event) и начинается с маркера llmtap.EVENT_MARKER.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .llmtap import EVENT_MARKER

MODES = ("signals", "all", "none")
DEFAULT_GATE_CONFIG = Path(__file__).resolve().parents[1] / "simmcp" / "edgectx_simday.yaml"


@dataclass
class Signal:
    device: str = "*"
    property: str = "*"
    kind: str = "change"          # change | threshold | delta | episode | ignore
    above: float | None = None
    below: float | None = None
    hysteresis: float = 0.0
    delta: float = 0.0
    gap_s: float = 0.0
    priority: str = "background"

    def matches(self, device: str, prop: str) -> bool:
        return fnmatch.fnmatchcase(device, self.device or "*") and fnmatch.fnmatchcase(prop, self.property or "*")

    @classmethod
    def from_dict(cls, d: dict) -> "Signal":
        return cls(device=str(d.get("device") or "*"), property=str(d.get("property") or "*"),
                   kind=str(d.get("kind") or "change"),
                   above=None if d.get("above") is None else float(d["above"]),
                   below=None if d.get("below") is None else float(d["below"]),
                   hysteresis=float(d.get("hysteresis") or 0.0), delta=float(d.get("delta") or 0.0),
                   gap_s=float(d.get("gap_s") or 0.0), priority=str(d.get("priority") or "background"))


@dataclass
class PushEvent:
    t: float
    device: str
    property: str
    value: Any
    prev: Any
    reason: str                   # change | threshold | delta | episode_end | all
    detail: str = ""
    source: str = ""

    def as_dict(self) -> dict:
        return {"t": self.t, "device": self.device, "property": self.property, "value": self.value,
                "prev": self.prev, "reason": self.reason, "detail": self.detail, "source": self.source}


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    return None


@dataclass
class PushFilter:
    mode: str = "signals"
    signals: list[Signal] = field(default_factory=list)
    default: str = "ignore"
    last: dict[tuple[str, str], Any] = field(default_factory=dict)          # последнее значение
    sig_value: dict[tuple[str, str], float] = field(default_factory=dict)   # для delta
    armed: dict[tuple[str, str], bool] = field(default_factory=dict)        # для threshold
    episodes: dict[tuple[str, str], tuple[float, Signal]] = field(default_factory=dict)  # открытые эпизоды
    seen: int = 0
    pushed: int = 0

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"push mode must be one of {MODES}")

    @classmethod
    def from_gate_config(cls, path: str | Path | None = None, mode: str = "signals",
                         initial_state: dict | None = None) -> "PushFilter":
        import yaml  # noqa: PLC0415 — PyYAML есть в .venv стенда

        p = Path(path) if path else DEFAULT_GATE_CONFIG
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        gate = ((cfg.get("policy") or {}).get("gate")) or {}
        f = cls(mode=mode, signals=[Signal.from_dict(s) for s in gate.get("signals") or []],
                default=str(gate.get("default") or "ignore"))
        f.set_initial(initial_state or {})
        return f

    def set_initial(self, initial_state: dict) -> None:
        """Начальное состояние дня (header.initial_state) — база для change/threshold, без пуша."""
        for dev, props in (initial_state or {}).items():
            for prop, v in (props or {}).items():
                key = (dev, prop)
                self.last[key] = v
                n = _num(v)
                if n is not None:
                    self.sig_value[key] = n
                    sig = self.signal_for(dev, prop)
                    if sig is not None and sig.kind == "threshold":
                        self.armed[key] = self._should_arm(sig, n, True)

    def signal_for(self, device: str, prop: str) -> Signal | None:
        for s in self.signals:
            if s.matches(device, prop):
                return s
        if self.default == "change":
            return Signal(device=device, property=prop, kind="change")
        return None

    @staticmethod
    def _should_arm(sig: Signal, v: float, current: bool) -> bool:
        if sig.above is not None:
            if v > sig.above:
                return False
            if v < sig.above - sig.hysteresis:
                return True
            return current
        if sig.below is not None:
            if v < sig.below:
                return False
            if v > sig.below + sig.hysteresis:
                return True
            return current
        return current

    def on_sensor(self, t: float, device: str, prop: str, value: Any, source: str = "") -> list[PushEvent]:
        self.seen += 1
        key = (device, prop)
        had = key in self.last
        prev = self.last.get(key)
        self.last[key] = value
        out: list[PushEvent] = []
        if self.mode == "none":
            return out
        if self.mode == "all":
            out.append(PushEvent(t, device, prop, value, prev, "all", source=source))
            self.pushed += len(out)
            return out
        sig = self.signal_for(device, prop)
        if sig is None or sig.kind == "ignore":
            return out
        n = _num(value)
        if sig.kind == "change":
            if not had or prev == value:         # первый отсчёт — база (как у hw-mcp); повтор — не изменение
                return out
            out.append(PushEvent(t, device, prop, value, prev, "change", source=source))
        elif sig.kind == "threshold" and n is not None:
            armed = self.armed.get(key, True)
            fired = False
            if armed and sig.above is not None and n > sig.above:
                fired, detail = True, f"> {sig.above:g}"
            elif armed and sig.below is not None and n < sig.below:
                fired, detail = True, f"< {sig.below:g}"
            self.armed[key] = self._should_arm(sig, n, armed and not fired)
            if fired:
                out.append(PushEvent(t, device, prop, value, prev, "threshold", detail, source))
        elif sig.kind == "delta" and n is not None:
            base = self.sig_value.get(key)
            if base is None or abs(n - base) >= sig.delta:
                self.sig_value[key] = n
                if base is not None:
                    out.append(PushEvent(t, device, prop, value, prev, "delta", f"Δ ≥ {sig.delta:g}", source))
        elif sig.kind == "episode":
            self.episodes[key] = (t, sig)          # эпизод открыт/продлён; конец — в tick()
        self.pushed += len(out)
        return out

    def tick(self, t: float) -> list[PushEvent]:
        """Закрыть эпизоды, в которых gap_s секунд не было событий (момент конца — last + gap_s)."""
        out: list[PushEvent] = []
        for key, (last_t, sig) in sorted(self.episodes.items(), key=lambda kv: kv[1][0]):
            if t - last_t >= sig.gap_s:
                end_t = last_t + sig.gap_s
                out.append(PushEvent(end_t, key[0], key[1], self.last.get(key), None, "episode_end",
                                     f"{sig.gap_s:g} с тишины"))
                del self.episodes[key]
        self.pushed += len(out)
        return out

    def next_deadline(self) -> float | None:
        """Ближайший момент закрытия эпизода (чтобы планировщик успел вызвать tick)."""
        if not self.episodes:
            return None
        return min(last_t + sig.gap_s for last_t, sig in self.episodes.values())


def hhmmss(t: float) -> str:
    s = int(t) % 86400
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def format_event(ev: PushEvent) -> str:
    """Короткий текст события (одинаковый для PicoClaw и nanobot)."""
    head = f"{EVENT_MARKER} {hhmmss(ev.t)} {ev.device}.{ev.property}"
    if ev.reason == "episode_end":
        return f"{head}: конец эпизода ({ev.detail}), последнее значение {_fmt(ev.value)}"
    tail = f" ({ev.detail})" if ev.detail else ""
    prev = "?" if ev.prev is None else _fmt(ev.prev)
    return f"{head}: {prev} → {_fmt(ev.value)}{tail}"
