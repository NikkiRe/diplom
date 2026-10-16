"""Сжатие времени дня в k раз для систем без виртуальных часов (PicoClaw, nanobot).

У EdgeCtx есть виртуальные часы (T0: день за секунды, LLM — ноль виртуального времени). У аналогов их нет:
все таймеры — стенные (Go time.Ticker, asyncio/croniter). Поэтому день проигрывается в реальном времени,
сжатом в k раз: событие дня с моментом t подаётся в стенной момент wall0 + (t − t0)/k, а все периоды систем,
выраженные в стенном времени, делятся на k (heartbeat, cron, Dream, idle-компакция, TTL кэша mock-llm).

Период можно разделить на k только в пределах того, что позволяет конфиг системы (целые минуты, минимум
5 мин у heartbeat PicoClaw, целые часы у Dream nanobot и т. д.). Каждое значение здесь возвращается с флагом
exact и фактическим периодом в сутках, а неточности попадают в manifest прогона (deviations).

Какие метрики при сжатии корректны — METRIC_VALIDITY (и README). Коротко: счётные (вызовы LLM, токены,
кэш-токены при TTL/k, байты, число тиков/пробуждений) — да; CPU-с/сутки, энергия, абсолютные задержки,
наклон памяти во времени — нет.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

# --------------------------------------------------------------------------- шкала времени


class TimeMap:
    """Соответствие «время дня t (с от полуночи) ↔ стенное время» при сжатии в k раз."""

    def __init__(self, k: float, t0: float, wall0: float | None = None):
        if k <= 0:
            raise ValueError("k must be > 0")
        self.k = float(k)
        self.t0 = float(t0)
        self.wall0 = time.time() if wall0 is None else float(wall0)

    def wall(self, t: float) -> float:
        """Стенной момент (Unix, с), когда наступает момент дня t."""
        return self.wall0 + (float(t) - self.t0) / self.k

    def day(self, wall: float | None = None) -> float:
        """Момент дня (с от полуночи) для стенного момента wall (по умолчанию — сейчас)."""
        w = time.time() if wall is None else float(wall)
        return self.t0 + (w - self.wall0) * self.k

    def day_s(self, wall_s: float) -> float:
        """Длительность в секундах дня для стенной длительности."""
        return float(wall_s) * self.k

    def sleep_until(self, t: float, stop: threading.Event | None = None) -> float:
        """Ждать наступления момента дня t. Возвращает опоздание в стенных секундах (≥ 0)."""
        target = self.wall(t)
        while True:
            now = time.time()
            rest = target - now
            if rest <= 0:
                return now - target
            if stop is not None:
                if stop.wait(min(rest, 0.5)):
                    return max(0.0, time.time() - target)
            else:
                time.sleep(min(rest, 0.5))


# --------------------------------------------------------------------------- масштабирование периодов


@dataclass
class Scaled:
    """Один период системы после деления на k."""

    system: str
    name: str                    # параметр конфига
    day_s: float                 # целевой период в сутках, с
    config_value: Any            # что записывается в конфиг
    config_unit: str             # единица конфига (min, s, ms, cron, …)
    wall_s: float                # фактический стенной период, с
    effective_day_s: float       # фактический период в сутках = wall_s · k, с
    exact: bool
    note: str = ""

    @property
    def rel_error(self) -> float:
        if self.day_s == 0:
            return 0.0
        return self.effective_day_s / self.day_s - 1.0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["rel_error"] = round(self.rel_error, 6)
        return d


def _is_int(x: float, eps: float = 1e-9) -> bool:
    return abs(x - round(x)) < eps


def scale_period(system: str, name: str, day_s: float, k: float, *, unit_s: float, unit: str,
                 min_units: float = 0.0, allow_zero: bool = False, note: str = "") -> Scaled:
    """Период day_s (с) в сутках → целое число единиц конфига unit_s (с) в стенном времени.

    Округление к ближайшему представимому значению, не меньше min_units (и не ноль, если 0 у системы
    означает «выключено» — allow_zero=False)."""
    want_units = day_s / k / unit_s
    units = round(want_units)
    lo = max(min_units, 0 if allow_zero else 1)
    if units < lo:
        units = lo
    exact = _is_int(want_units) and want_units >= lo
    wall = units * unit_s
    return Scaled(system, name, day_s, int(units), unit, wall, wall * k, exact, note)


def divisors_of_60() -> list[int]:
    return [d for d in range(1, 61) if 60 % d == 0]


# --------------------------------------------------------------------------- планы для систем


@dataclass
class Plan:
    """Параметры одной системы при сжатии k: значения для конфига и список неточностей."""

    system: str
    k: float
    values: dict[str, Any] = field(default_factory=dict)
    scaled: list[Scaled] = field(default_factory=list)
    deviations: list[str] = field(default_factory=list)

    def add(self, s: Scaled, key: str | None = None) -> Scaled:
        self.scaled.append(s)
        self.values[key or s.name] = s.config_value
        if not s.exact:
            self.deviations.append(
                f"{s.system}.{s.name}: {s.day_s:g} с/сут → {s.config_value} {s.config_unit} = "
                f"{s.effective_day_s:g} с/сут (ошибка {s.rel_error:+.1%}){'; ' + s.note if s.note else ''}")
        return s

    def as_dict(self) -> dict:
        return {"system": self.system, "k": self.k, "values": self.values,
                "scaled": [s.as_dict() for s in self.scaled], "deviations": list(self.deviations),
                "exact": not self.deviations}


def picoclaw_plan(k: float, *, heartbeat_min: float = 30.0, cron_check_min: float | None = 5.0,
                  hb_mode: str = "auto") -> Plan:
    """PicoClaw v0.3.1.

    heartbeat: `heartbeat.interval` — целые минуты, минимум 5 (pkg/heartbeat/service.go: minIntervalMinutes),
    первый тик через 1 с после старта, затем time.Ticker. Если 30/k — целое ≥ 5 (k ∈ {1, 2, 3, 5, 6}),
    heartbeat штатный (`native`). Иначе — эмуляция задачей cron `every` (секунды) с тем же текстом
    промпта heartbeat (`cron`): штатный heartbeat выключается. Это отклонение от «как поставляется»:
    задача cron идёт в новой сессии `agent:cron-<id>-<uuid>` (у heartbeat — сессия heartbeat с NoHistory) —
    обе без истории, но у cron нет строки «Current time» и ответ публикуется в канал, а не подавляется по
    HEARTBEAT_OK. В manifest — deviations.

    cron command (PicoClaw-best): `every_seconds` — целые секунды ≥ 1.
    """
    p = Plan("picoclaw", k)
    hb_day = heartbeat_min * 60.0
    native_units = heartbeat_min / k
    native_ok = _is_int(native_units) and round(native_units) >= 5
    mode = hb_mode
    if mode == "auto":
        mode = "native" if native_ok else "cron"
    if heartbeat_min <= 0:
        mode = "off"
    p.values["heartbeat_mode"] = mode
    if mode == "native":
        s = scale_period("picoclaw", "heartbeat.interval", hb_day, k, unit_s=60, unit="min", min_units=5,
                         note="минимум 5 мин (minIntervalMinutes)")
        p.add(s, "heartbeat_interval_min")
    elif mode == "cron":
        s = scale_period("picoclaw", "heartbeat(cron every)", hb_day, k, unit_s=1, unit="s", min_units=1)
        p.add(s, "heartbeat_every_s")
        p.deviations.append(
            "picoclaw.heartbeat: штатный heartbeat (целые минуты, ≥ 5) не делится на k="
            f"{k:g}; эмуляция задачей cron every={s.config_value} с с промптом heartbeat (новая сессия на тик, "
            "без строки Current time; ответ HEARTBEAT_OK публикуется в канал, а не подавляется)")
    if cron_check_min:
        s = scale_period("picoclaw", "cron.check(every_seconds)", cron_check_min * 60.0, k, unit_s=1, unit="s",
                         min_units=1)
        p.add(s, "cron_check_every_s")
    return p


def nanobot_plan(k: float, *, heartbeat_min: float = 30.0, dream_h: float | None = 2.0,
                 idle_compact_min: float = 15.0, idle_check_s: float = 60.0,
                 session_msgs_per_min: int = 6) -> Plan:
    """nanobot 0.3.5.

    heartbeat: `gateway.heartbeat.intervalS` — целые секунды (системная задача cron every_ms).
    Dream: `agents.defaults.dream.intervalH` — целые часы ≥ 1; для k > 2 — устаревшее поле `dream.cron`
    ("*/m * * * *", m — делитель 60), иначе ближайший делитель 60 (отклонение).
    Idle-компакция сессий: `idleCompactAfterMinutes` — целые минуты (0 — выкл.): 15/k точно только при
    k ∈ {1, 3, 5, 15}; иначе ближайшее ≥ 1 (отклонение). `idleCompactCheckIntervalSeconds` — целые секунды.
    `tools.maxSessionMessagesPerMinute` — лимит на стенную минуту: умножается на k, чтобы лимит в сутках
    не ужесточался.
    Не настраивается: опрос очереди local triggers — 0,5 с стенных (pkg triggers/local_runner.py), т. е.
    до 0,5·k с суток задержки доставки события (k = 60 → до 30 с суток).
    """
    p = Plan("nanobot", k)
    if heartbeat_min > 0:
        p.add(scale_period("nanobot", "gateway.heartbeat.intervalS", heartbeat_min * 60.0, k, unit_s=1, unit="s",
                           min_units=1), "heartbeat_interval_s")
    if dream_h:
        want_min = dream_h * 60.0 / k
        if _is_int(dream_h / k) and dream_h / k >= 1:
            s = Scaled("nanobot", "agents.defaults.dream.intervalH", dream_h * 3600.0, int(round(dream_h / k)), "h",
                       round(dream_h / k) * 3600.0, round(dream_h / k) * 3600.0 * k, True)
            p.add(s, "dream_interval_h")
            p.values["dream_cron"] = None
        else:
            # "*/m * * * *" срабатывает в минуты 0, m, 2m… каждого часа: ceil(60/m) раз в час; средний период
            # 60/ceil(60/m) мин. Берётся m с ближайшей к цели частотой; точно — если цель делит 60.
            want_rate = 60.0 / want_min
            m = min(range(1, 61), key=lambda d: (abs(math.ceil(60 / d) - want_rate), d))
            eff_min = 60.0 / math.ceil(60 / m)
            exact = _is_int(want_min) and int(round(want_min)) in divisors_of_60()
            s = Scaled("nanobot", "agents.defaults.dream.cron", dream_h * 3600.0, f"*/{m} * * * *", "cron",
                       eff_min * 60.0, eff_min * 60.0 * k, exact,
                       "intervalH — целые часы; устаревшее поле cron (минуты от начала часа)")
            p.add(s, "dream_cron")
            p.values["dream_interval_h"] = None
    s = scale_period("nanobot", "agents.defaults.idleCompactAfterMinutes", idle_compact_min * 60.0, k, unit_s=60,
                     unit="min", min_units=1, note="целые минуты; 0 выключило бы компакцию")
    p.add(s, "idle_compact_min")
    p.add(scale_period("nanobot", "agents.defaults.idleCompactCheckIntervalSeconds", idle_check_s, k, unit_s=1,
                       unit="s", min_units=1), "idle_check_s")
    p.values["max_session_messages_per_minute"] = int(math.ceil(session_msgs_per_min * k))
    p.values["trigger_poll_day_s"] = 0.5 * k
    if 0.5 * k > 5:
        p.deviations.append(f"nanobot.local_triggers: опрос очереди 0,5 с стенных = {0.5 * k:g} с суток задержки "
                            "доставки события (не настраивается)")
    return p


def mock_plan(k: float, *, cache_ttl_s: float = 300.0, latency: str = "0") -> Plan:
    """mock-llm: TTL эмуляции префикс-кэша в стенных секундах (--clock wall) и инжектируемая задержка /k."""
    p = Plan("mock-llm", k)
    ttl = cache_ttl_s / k
    s = Scaled("mock-llm", "--cache-ttl", cache_ttl_s, round(ttl, 6), "s", ttl, ttl * k, True)
    p.add(s, "cache_ttl_s")
    p.values["latency"] = scale_latency(latency, k)
    return p


def scale_latency(spec: str, k: float) -> str:
    """'fixed:800' → 'fixed:13.33…' при k = 60 и т. п.: задержка LLM в сутках сохраняется в долях дня."""
    spec = (spec or "0").strip()
    if spec in ("0", ""):
        return "0"
    kind, _, rest = spec.partition(":")
    parts = [x for x in rest.split(":") if x]
    if kind == "fixed" and len(parts) == 1:
        return f"fixed:{float(parts[0]) / k:.6g}"
    if kind == "uniform" and len(parts) == 2:
        return f"uniform:{float(parts[0]) / k:.6g}:{float(parts[1]) / k:.6g}"
    if kind == "lognormal" and len(parts) == 2:          # медиана масштабируется, σ лог-шкалы — нет
        return f"lognormal:{float(parts[0]) / k:.6g}:{parts[1]}"
    if kind == "normal" and len(parts) == 2:
        return f"normal:{float(parts[0]) / k:.6g}:{float(parts[1]) / k:.6g}"
    raise ValueError(f"cannot scale latency spec {spec!r}")


# Параметры edgectx (agent/internal/config/config.go), выраженные в секундах/минутах стенного времени при
# clock: real. Все — float, поэтому деление на k точное.
EDGECTX_TIME_KEYS: dict[tuple[str, ...], str] = {
    ("policy", "heartbeat_min"): "min",
    ("policy", "snapshot_period_s"): "s",
    ("policy", "profile_consolidate_s"): "s",
    ("policy", "gate", "debounce_s"): "s",
    ("policy", "gate", "cooldown_s"): "s",
    ("policy", "gate", "coalesce_s"): "s",
    ("policy", "gate", "self_echo_s"): "s",
}
EDGECTX_SIGNAL_KEYS = ("gap_s", "debounce_s", "cooldown_s")


def edgectx_compressed_config(cfg: dict, k: float) -> tuple[dict, Plan]:
    """Конфиг edgectx для паритетного прогона в реальном сжатом времени (а не на виртуальных часах).

    clock: real; все периоды политики /k (heartbeat_min, snapshot_period_s, profile_consolidate_s, гейт:
    debounce/cooldown/coalesce/self_echo, у сигналов gap_s/debounce_s/cooldown_s). Суточный бюджет
    (budget.daily_*) считается по Clock — при реальных часах «сутки» = 24 ч стенных, поэтому лимиты
    остаются на прогон. Правила, созданные LLM (for_s), и TTL решений шлюза hw-mcp (edgectx/ttl_s) в конфиге
    не задаются и при сжатии не масштабируются — это отклонения (в Plan.deviations).
    Точнее — режим часов «scaled» в самом edgectx (Real с темпом k) — см. README, раздел «Паритет».
    """
    import copy

    out = copy.deepcopy(cfg)
    p = Plan("edgectx", k)
    clock = out.get("clock")
    if isinstance(clock, str) or clock is None:
        clock = {"mode": clock or "real"}
    clock = dict(clock)
    clock["mode"] = "real"
    clock.pop("start_t", None)
    out["clock"] = clock
    for path, unit in EDGECTX_TIME_KEYS.items():
        node = out
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if not isinstance(node, dict) or path[-1] not in node or node[path[-1]] in (None, 0):
            continue
        v = float(node[path[-1]])
        day_s = v * 60.0 if unit == "min" else v
        node[path[-1]] = v / k
        p.scaled.append(Scaled("edgectx", ".".join(path), day_s, v / k, unit, day_s / k, day_s, True))
        p.values[".".join(path)] = v / k
    signals = (((out.get("policy") or {}).get("gate") or {}).get("signals")) or []
    for i, sig in enumerate(signals):
        for key in EDGECTX_SIGNAL_KEYS:
            if isinstance(sig, dict) and sig.get(key) not in (None, 0):
                sig[key] = float(sig[key]) / k
                p.values[f"policy.gate.signals[{i}].{key}"] = sig[key]
    p.deviations.append("edgectx: for_s правил LLM и TTL решений шлюза (edgectx/ttl_s) не масштабируются")
    p.deviations.append("edgectx: время в промпте и ts_clock — стенные (clock: real), а не время дня")
    return out, p


# --------------------------------------------------------------------------- допустимость метрик


METRIC_VALIDITY: list[dict[str, str]] = [
    {"metric": "вызовы LLM за день (всего и по источникам)", "valid": "да",
     "why": "счётная величина; периоды фона делятся на k, события подаются в те же моменты дня"},
    {"metric": "входные/выходные токены", "valid": "да",
     "why": "зависят от содержимого запросов, а не от темпа; оракул детерминирован"},
    {"metric": "кэш-токены (cached_tokens)", "valid": "да, при TTL/k",
     "why": "mock-llm с --clock wall и --cache-ttl = TTL/k; погрешность — время обработки запросов, "
            "растянутое в k раз в шкале дня"},
    {"metric": "байты LLM / MCP / канала", "valid": "да",
     "why": "счётные; keep-alive MQTT/WebSocket (пинги по стенным часам) дают в k раз меньше служебного трафика"},
    {"metric": "число heartbeat/Dream/cron за день", "valid": "да, если период exact",
     "why": "иначе — фактический период из manifest (deviations)"},
    {"metric": "RSS/PSS: уровень и пик", "valid": "ориентир",
     "why": "рост, зависящий от числа событий, сохраняется; рост и сборка мусора по стенным часам — сжаты"},
    {"metric": "наклон памяти, МиБ/ч", "valid": "нет", "why": "ось времени сжата неоднородно"},
    {"metric": "CPU-с за сутки, пробуждения/с, энергия", "valid": "нет",
     "why": "фоновые затраты (тики рантайма, опросы, keep-alive) сжимаются в k раз, затраты на событие — нет"},
    {"metric": "задержки «реплика → ответ», «событие → действие»", "valid": "нет (в сутках)",
     "why": "стенная обработка 50 мс = 3 с суток при k = 60; стенные значения пишутся, но это не T2"},
    {"metric": "качество ответов живой модели", "valid": "нет",
     "why": "в промптах аналогов — стенное время, а не время дня; для живой модели только T2 (реальное время)"},
]


def metric_validity(k: float = 1.0, *, virtual_t0: bool = False) -> dict:
    """Машинные признаки допустимости переноса временных метрик в T2 (остальные условия — в протоколе)."""
    accelerated = virtual_t0 or not math.isclose(float(k), 1.0)
    return {
        "time_mode": "virtual_t0" if virtual_t0 else "compressed" if accelerated else "real_time",
        "k": None if virtual_t0 else float(k),
        "counts": "within_protocol",
        "memory_level": "indicative" if accelerated else "observed",
        "cpu_daily_valid_for_t2": not accelerated,
        "latency_valid_for_t2": not accelerated,
        "memory_slope_valid_for_t2": not accelerated,
        "note": ("Сжатое/виртуальное время: CPU за сутки, задержки и наклон памяти невалидны для T2; "
                 "числа сохранены только для диагностики. RSS/PSS — ориентир, не результат."
                 if accelerated else "Время реальное; остальные условия валидности проверяются протоколом."),
    }


def exact_k(system: str, ks: range | list[int] = range(1, 121), **kw) -> list[int]:
    """При каких k все периоды системы делятся точно."""
    out = []
    for k in ks:
        if system == "picoclaw":
            plan = picoclaw_plan(k, hb_mode="native", **kw)
            ok = not plan.deviations and plan.values.get("heartbeat_mode") == "native"
        elif system == "nanobot":
            ok = not [d for d in nanobot_plan(k, **kw).deviations if "local_triggers" not in d]
        else:
            raise ValueError(system)
        if ok:
            out.append(k)
    return out


if __name__ == "__main__":     # pragma: no cover — справка для README
    import json
    import sys

    kk = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    print(json.dumps({"picoclaw": picoclaw_plan(kk).as_dict(), "nanobot": nanobot_plan(kk).as_dict(),
                      "mock": mock_plan(kk).as_dict(),
                      "exact_k": {"picoclaw_native_heartbeat": exact_k("picoclaw"),
                                  "nanobot": exact_k("nanobot")}}, ensure_ascii=False, indent=1))
