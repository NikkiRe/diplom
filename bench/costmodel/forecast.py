"""Аналитические прогнозы для предрегистрации (THESIS_CONCEPT §6, §12.6; план ВКР, гипотеза H1).

H1 («когда будить»). Вызовы LLM за окно дня:
    heartbeat:  N_hb   + C·E[k]
    гейт:       N_gate + C·E[k]
где C — команд пользователя в сутки (профиль Bentley et al., 2018, табл. 6), E[k] — вызовов LLM на команду
(VisionClaw §6.2: 1/2/3/5 с вероятностями 0,21/0,27/0,23/0,29, E[k] = 2,89), N_hb — тиков heartbeat строго
внутри окна (07–23, период 30 мин → 31 тик), N_gate — пробуждений гейта (≈4 в сутки). Отношение
    ρ(C) = (N_hb + C·E[k]) / (N_gate + C·E[k])
даёт ориентиры концепции: ρ(M+) = (31 + 29,2)/(4 + 29,2) = 1,81, ρ(M) = 2,35, ρ(H) ≈ 1,29. Выигрыш падает
с ростом C; «точка безубыточности» по командам для порога r: C* = (N_hb − r·N_gate) / (E[k]·(r − 1)).

Это прогноз (воспроизведение), а не результат: T0/T2 сверяются с ним, отклонение отчитывается.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from .history_model import STRATEGIES, Workload, history_ratio_table
from .prices import PriceBook

# Команд пользователя в сутки по профилям (Bentley et al., 2018, табл. 6); S — стресс, только T0.
PROFILES: dict[str, float] = {"L": 2.61, "M": 5.55, "M+": 10.1, "H": 30.44, "S69": 69.0, "S200": 200.0}
# Вызовов LLM на команду (VisionClaw §6.2).
K_DIST: dict[int, float] = {1: 0.21, 2: 0.27, 3: 0.23, 5: 0.29}
# События CASAS в сутки по уровням гейта (GAP1 §2.3): для B0-events и доли фона.
CASAS_EVENTS_PER_DAY: dict[str, float] = {"activity": 45.0, "door": 55.0, "room_change": 390.0}

WINDOW_T0 = (7 * 3600, 23 * 3600)    # активное окно T0 (07–23)
WINDOW_T2 = (14 * 3600, 22 * 3600)   # окно T2 (14–22)
HEARTBEAT_PERIOD_S = 1800            # 30 мин — умолчание OpenClaw/PicoClaw
GATE_WAKES_PER_DAY = 4.0             # ориентир концепции (§6, расчёт red team)


def expected_k(dist: dict[int, float] = K_DIST) -> float:
    total = sum(dist.values())
    if not math.isclose(total, 1.0, abs_tol=1e-9):
        raise ValueError(f"probabilities sum to {total}, not 1")
    return sum(k * p for k, p in dist.items())


def expected_k_effective(dist: dict[int, float] = K_DIST, min_calls: int = 2) -> float:
    """E[max(k, min_calls)]: если каждый вход требует инструмента (команда → действие, вопрос → answer), ход
    не короче двух вызовов (вызов инструмента + финал). Так считает оракул T0: 0,21·2 + 0,27·2 + 0,23·3 + 0,29·5
    = 3,10 вместо 2,89 — прогноз для сверки с T0 берётся с этим E[k]."""
    expected_k(dist)   # проверка суммы вероятностей
    return sum(max(k, min_calls) * p for k, p in dist.items())


def heartbeat_ticks(window_s: float, period_s: float) -> int:
    """Тиков heartbeat строго внутри окна: первый — через период после начала, последний — до конца окна.

    16 ч / 30 мин → 31 (как в расчёте концепции); 8 ч / 30 мин → 15.
    """
    if period_s <= 0 or window_s <= 0:
        raise ValueError("window and period must be positive")
    return max(0, math.ceil(window_s / period_s) - 1)


def keep_warm_period(ttl_s: float, margin: float = 0.9) -> float:
    """Период keep-warm «чуть меньше TTL» (решение 8 концепции): margin·TTL."""
    if not 0 < margin < 1:
        raise ValueError("margin must be in (0, 1)")
    return ttl_s * margin


def h1_ratio(commands_per_day: float, hb_ticks: float, gate_wakes: float, ek: float) -> float:
    """ρ = (N_hb + C·E[k]) / (N_gate + C·E[k]) — во сколько раз heartbeat делает больше вызовов, чем гейт."""
    user = commands_per_day * ek
    den = gate_wakes + user
    if den <= 0:
        return math.inf
    return (hb_ticks + user) / den


def h1_breakeven_commands(target_ratio: float, hb_ticks: float, gate_wakes: float, ek: float) -> float:
    """Команд в сутки, при которых ρ падает до target_ratio (> 1); inf, если ρ не достигает порога и при C = 0
    уже ниже него — тогда 0."""
    if target_ratio <= 1:
        raise ValueError("target_ratio must be > 1")
    if gate_wakes > 0 and hb_ticks / gate_wakes <= target_ratio:
        return 0.0
    if gate_wakes == 0 and hb_ticks == 0:
        return 0.0
    return (hb_ticks - target_ratio * gate_wakes) / (ek * (target_ratio - 1.0))


@dataclass
class H1Params:
    window_s: float = WINDOW_T0[1] - WINDOW_T0[0]
    heartbeat_period_s: float = HEARTBEAT_PERIOD_S
    gate_wakes: float = GATE_WAKES_PER_DAY
    k_dist: dict = field(default_factory=lambda: dict(K_DIST))
    profiles: dict = field(default_factory=lambda: dict(PROFILES))


def h1_table(p: H1Params | None = None) -> dict:
    """Таблица H1 по профилям + точки безубыточности по командам для порогов 1,1 / 1,28 / 1,45
    (1,28 и 1,45 — ширина парного 95 % ДИ E5-T2 при s = 0,2 / 0,3, §12.6)."""
    p = p or H1Params()
    ek = expected_k(p.k_dist)
    n_hb = heartbeat_ticks(p.window_s, p.heartbeat_period_s)
    rows = []
    for name, c in p.profiles.items():
        user = c * ek
        rows.append({
            "profile": name, "commands_per_day": c, "user_calls": user,
            "calls_heartbeat": n_hb + user, "calls_gate": p.gate_wakes + user,
            "ratio_hb_to_gate": h1_ratio(c, n_hb, p.gate_wakes, ek),
            "background_share_heartbeat": n_hb / (n_hb + user) if n_hb + user else 0.0,
            "background_share_gate": p.gate_wakes / (p.gate_wakes + user) if p.gate_wakes + user else 0.0,
        })
    breakeven = {str(r): h1_breakeven_commands(r, n_hb, p.gate_wakes, ek) for r in (1.1, 1.28, 1.45)}
    return {"params": {**asdict(p), "expected_k": ek, "heartbeat_ticks": n_hb}, "rows": rows,
            "breakeven_commands_per_day": breakeven,
            "breakeven_gate_wakes_per_day": n_hb,
            "formula": "ratio = (N_hb + C*E[k]) / (N_gate + C*E[k]); C* = (N_hb - r*N_gate) / (E[k]*(r-1))"}


def h1_sensitivity(gate_wakes: list[float], periods_s: list[float], commands_per_day: float,
                   window_s: float = WINDOW_T0[1] - WINDOW_T0[0], k_dist: dict = K_DIST) -> list[dict]:
    """ρ на сетке «пробуждений гейта × период heartbeat» при фиксированном профиле (для H2/E4)."""
    ek = expected_k(k_dist)
    out = []
    for period in periods_s:
        n_hb = heartbeat_ticks(window_s, period)
        for g in gate_wakes:
            out.append({"heartbeat_period_s": period, "heartbeat_ticks": n_hb, "gate_wakes": g,
                        "commands_per_day": commands_per_day, "ratio_hb_to_gate": h1_ratio(commands_per_day, n_hb, g, ek)})
    return out


def _k_eff_dist(dist: dict[int, float], min_calls: int = 2) -> dict[int, float]:
    """Распределение max(k, min_calls)."""
    out: dict[int, float] = {}
    for k, p in dist.items():
        out[max(k, min_calls)] = out.get(max(k, min_calls), 0.0) + p
    return out


def background_share_events(commands_per_day: float, events_per_day: float, k_dist: dict = K_DIST) -> float:
    """Q1: доля фоновых вызовов у B0-events (каждое событие уровня гейта — вызов LLM)."""
    user = commands_per_day * expected_k(k_dist)
    return events_per_day / (events_per_day + user) if events_per_day + user else 0.0


def daily_cost_bounds(calls: float, prompt_tokens: float, prefix_tokens: float, output_tokens: float,
                      model: str, book: PriceBook, min_cache: int = 1024, granularity: int = 128) -> dict:
    """₽ за окно в границах «кэша нет» и «весь стабильный префикс из кэша в каждом вызове» (§12.2).

    prompt_tokens / output_tokens — средние на вызов; prefix_tokens — стабильный префикс (system + tools +
    инвентарь). Кэшируется префикс ≥ min_cache токенов, округлённый вниз до granularity.
    """
    cached = 0 if prefix_tokens < min_cache else (int(prefix_tokens) // granularity) * granularity
    cached = min(cached, prompt_tokens)
    no_cache = calls * book.call_cost_rub(model, prompt_tokens, 0, output_tokens)
    full_cache = calls * book.call_cost_rub(model, prompt_tokens, cached, output_tokens)
    return {"model": model, "calls": calls, "prompt_tokens_per_call": prompt_tokens, "prefix_tokens": prefix_tokens,
            "cached_tokens_per_call": cached, "output_tokens_per_call": output_tokens,
            "rub_no_cache": no_cache, "rub_full_prefix_cache": full_cache}


# Ход из двух вызовов для оценки цены рассуждения (O1 §4): вызов 1 — холодный (вход, кэш, видимый выход),
# вызов 2 — продолжение хода с префиксом из кэша. R — скрытые reasoning-токены на вызов (тарифицируются как выход).
REASONING_TURN = ((2000, 0, 40), (2100, 1920, 20))
# Модели и уровни R: у reasoning-моделей R ∈ {0, 100, 400} (400 — допущение концепции §14, не замер; реальное R
# даст проба E0-P), у моделей без рассуждения и у GPT-6 Luna с effort none — только R = 0.
REASONING_MODELS: dict[str, tuple[int, ...]] = {
    "openai/gpt-5-mini": (0, 100, 400), "openai/gpt-5-nano": (0, 100, 400),
    "openai/gpt-4.1-nano": (0,), "openai/gpt-4o-mini": (0,), "openai/gpt-6-luna": (0,),
}


def reasoning_turn_cost(book: PriceBook, model: str, reasoning: int, cache_write: bool = True,
                        turn: tuple = REASONING_TURN) -> dict:
    """₽ хода REASONING_TURN при R reasoning-токенах на вызов и доля выхода (видимого и скрытого) в цене."""
    p = book.price(model)
    total = out = 0.0
    for prompt, cached, visible in turn:
        total += book.call_cost_rub(model, prompt, cached, visible + reasoning, cache_write=cache_write)
        out += (visible + reasoning) * p.output
    return {"model": p.model, "reasoning_tokens": reasoning, "rub_turn": total,
            "output_share": out / total if total else None, "cache_write": cache_write and p.cache_write is not None}


def reasoning_table(book: PriceBook | None = None, models: dict[str, tuple[int, ...]] | None = None,
                    cache_write: bool = True) -> dict:
    """Цена хода по моделям и R (расчёт по замороженному прайсу, не замер): основание выбора reasoning_effort."""
    book = book or PriceBook.load()
    rows = [reasoning_turn_cost(book, m, r, cache_write) for m, rs in (models or REASONING_MODELS).items() for r in rs]
    return {"turn": [dict(zip(("prompt", "cached", "visible_output"), c)) for c in REASONING_TURN], "rows": rows,
            "note": "расчёт; R — допущение до пробы E0-P", "prices": book.provenance()}


@dataclass
class CallShape:
    """Средний вызов LLM (иллюстративно до калибровки E0-P): стабильный префикс и полный промпт, токенов."""
    prefix_tokens: float = 2000.0
    prompt_tokens: float = 3000.0
    output_tokens: float = 150.0


def prereg_forecasts(book: PriceBook | None = None, model: str = "openai/gpt-4o-mini",
                     h1: H1Params | None = None, shape: CallShape | None = None,
                     history_rates: tuple = (3, 6, 10, 30, 69, 200)) -> dict:
    """Все прогнозы costmodel для предрегистрации одним JSON-объектом."""
    book = book or PriceBook.load()
    h1p = h1 or H1Params()
    shape = shape or CallShape()
    h1t = h1_table(h1p)
    window_t2 = WINDOW_T2[1] - WINDOW_T2[0]
    h1_t2 = h1_table(H1Params(window_s=window_t2, heartbeat_period_s=h1p.heartbeat_period_s,
                              gate_wakes=h1p.gate_wakes * window_t2 / h1p.window_s,
                              k_dist=h1p.k_dist, profiles=h1p.profiles))
    ek = expected_k(h1p.k_dist)
    q1 = []
    for name, c in h1p.profiles.items():
        row = {"profile": name, "commands_per_day": c}
        for level, ev in CASAS_EVENTS_PER_DAY.items():
            row[f"background_share_events_{level}"] = background_share_events(c, ev, h1p.k_dist)
        q1.append(row)
    costs = []
    for r in h1t["rows"]:
        for policy, calls in (("heartbeat", r["calls_heartbeat"]), ("gate", r["calls_gate"])):
            costs.append({"profile": r["profile"], "policy": policy,
                          **daily_cost_bounds(calls, shape.prompt_tokens, shape.prefix_tokens, shape.output_tokens,
                                              model, book)})
    keep_warm = h1_sensitivity([2, 4, 8, 16], [HEARTBEAT_PERIOD_S, keep_warm_period(300), keep_warm_period(600)],
                               PROFILES["M+"], h1p.window_s, h1p.k_dist)
    return {
        "kind": "costmodel prereg forecasts",
        "status": "forecast (воспроизведение), не результат",
        "h1_t0_window": h1t,
        "h1_t2_window": h1_t2,
        "h1_sensitivity_mplus": keep_warm,
        "q1_background_share_events": q1,
        "h4b_history_ratio_ttl600": history_ratio_table(list(history_rates), ttl=600),
        "h4b_history_ratio_no_cache": history_ratio_table(list(history_rates), ttl=0),
        "h4b_strategies": dict(STRATEGIES),
        "h4b_workload": Workload().__dict__,
        "rub_bounds": {"call_shape": asdict(shape), "rows": costs, "note": "иллюстративно до калибровки E0-P"},
        "expected_k": ek,
        "expected_k_effective_t0": expected_k_effective(h1p.k_dist),
        "h1_t0_oracle_k_eff": h1_table(H1Params(window_s=h1p.window_s, heartbeat_period_s=h1p.heartbeat_period_s,
                                                gate_wakes=h1p.gate_wakes,
                                                k_dist=_k_eff_dist(h1p.k_dist),
                                                profiles=h1p.profiles)),
        "prices": book.provenance(),
        "sources": {
            "profiles": "Bentley et al. 2018, IMWUT 2(3):91, табл. 6",
            "k_dist": "VisionClaw §6.2",
            "casas_events": "GAP1 §2.3 (CASAS, 14 домов, 497 суток)",
            "history_model": "port of research/new_topic/D_artifacts/cost_model.py",
        },
    }
