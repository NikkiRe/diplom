"""Модель суточных токенов агента при разных политиках истории и TTL кэша провайдера.

Порт research/new_topic/D_artifacts/cost_model.py (заметка D, §9.3) без изменения алгоритма: числа
совпадают с исходником (проверяется тестом test_costmodel_history). Это иллюстративная модель для
прогнозов H4(б) («отношение токенов и ₽/сут между политиками как функция профиля»), не измерение.

Модель. Промпт вызова — список «атомов» (id, токены). Кэш-попадание — длина общего префикса атомов
с предыдущим вызовом, если пауза между вызовами < TTL; меньше MIN_CACHE токенов не кэшируется,
кэш округляется вниз до кратного GRANULARITY (правила кэша OpenAI). Внутри запроса пользователя вызовы
идут подряд (5 с), между запросами — пауза hours·3600 / R.
"""
from __future__ import annotations

from dataclasses import dataclass

MIN_CACHE = 1024   # минимальная длина кэшируемого префикса, токенов (OpenAI prompt caching)
GRANULARITY = 128  # шаг cached_tokens

# Цены исходника за 1 токен: (вход, кэш, выход, запись кэша или None)
LEGACY_PRICES = {
    "gpt-5-mini $": (0.25e-6, 0.025e-6, 2.00e-6, None),
    "gpt-4o-mini $": (0.15e-6, 0.075e-6, 0.60e-6, None),
    "gpt-5-mini RouterAI руб.": (27.6025425e-6, 2.76025425e-6, 220.82034e-6, None),
    # GPT-5.6+: запись кэша 1.25×, чтение 0.1×; приближение — все некэшированные токены вызова
    # длиной ≥ MIN_CACHE считаются записанными.
    "gpt-6-luna $": (0.10e-6, 0.01e-6, 0.50e-6, 0.125e-6),
}


@dataclass
class Workload:
    """Параметры нагрузки (имена полей исходника сохранены)."""
    hours: float = 8      # часов работы в сутки
    R: int = 40           # запросов пользователя в сутки
    k: int = 3            # вызовов LLM на запрос (k − 1 вызов инструмента + ответ)
    S: int = 4000         # system prompt + схемы инструментов MCP (стабильно)
    P: int = 300          # профиль пользователя (меняется раз в 10 запросов)
    D: int = 600          # снимок состояния оборудования (меняется каждый запрос)
    u: int = 60           # реплика пользователя
    o: int = 150          # выход модели на вызов
    r: int = 300          # результат инструмента
    window: int = 5       # окно ходов для оконных стратегий
    summ_every: int = 7   # сводка каждые ~20 сообщений (7 запросов × 3 сообщения)
    summ_len: int = 400


@dataclass(frozen=True)
class TokenTotals:
    uncached: int   # входные токены без кэша (включая вызовы суммаризатора)
    cached: int     # входные токены из кэша
    output: int     # выходные токены
    written: int    # некэшированные токены вызовов длиной ≥ MIN_CACHE (кандидаты на запись кэша)

    @property
    def input_total(self) -> int:
        return self.uncached + self.cached

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.uncached, self.cached, self.output, self.written


STRATEGIES = [
    ("snapshot_head", "полная история, снимок оборудования в начале промпта"),
    ("full", "полная история, снимок в хвосте (стабильный префикс)"),
    ("window_slide", "скользящее окно 5 ходов"),
    ("window_block", "окно, сброс блоком (5..10 ходов)"),
    ("mask_old_results", "маскирование старых результатов инструментов"),
    ("summary_in_system", "сводка в system prompt каждые ~20 сообщений (как PicoClaw)"),
]
TTLS = [("без кэша", 0), ("in_memory ~10 мин", 600), ("30 мин", 1800), ("24h", 86400)]


def _common_prefix_tokens(a: list[tuple], b: list[tuple]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += x[1]
    return n


def simulate(w: Workload, strategy: str, ttl: float) -> TokenTotals:
    """Суточные токены для политики истории strategy и TTL кэша ttl (с)."""
    if strategy not in dict(STRATEGIES):
        raise ValueError(f"unknown strategy {strategy!r}")
    gap = w.hours * 3600 / w.R
    prev, prev_t, t = None, -1e18, 0.0
    unc = cac = out = wr = 0
    hist: list[list[tuple]] = []   # ходы; ход — список атомов
    summary = None
    prof_v = 0
    for q in range(w.R):
        if q and q % 10 == 0:
            prof_v += 1
        snap = ("snap", w.D, q)          # снимок меняется каждый запрос
        prof = ("prof", w.P, prof_v)
        head = [("sys", w.S)]
        turn = [("u", w.u, q)]
        for c in range(w.k):
            t = q * gap + c * 5.0
            if strategy == "snapshot_head":
                atoms = head + [snap, prof] + [a for h in hist for a in h] + turn
            elif strategy == "summary_in_system":
                atoms = head + ([summary] if summary else []) + [prof] + [a for h in hist for a in h] + [snap] + turn
            else:
                atoms = head + [prof] + [a for h in hist for a in h] + [snap] + turn
            atoms = [tuple(a) for a in atoms]
            total = sum(a[1] for a in atoms)
            hit = 0
            if prev is not None and t - prev_t < ttl:
                hit = _common_prefix_tokens(prev, atoms)
                hit = 0 if hit < MIN_CACHE else (hit // GRANULARITY) * GRANULARITY
            cac += hit
            unc += total - hit
            if total >= MIN_CACHE:
                wr += total - hit
            out += w.o
            prev, prev_t = atoms, t
            if c < w.k - 1:
                turn = turn + [("call", w.o, q, c), ("res", w.r, q, c)]
            else:
                turn = turn + [("ans", w.o, q)]
        # ход завершён — обновить историю по политике
        if strategy in ("snapshot_head", "full"):
            hist.append(turn)
        elif strategy == "window_slide":
            hist = (hist + [turn])[-w.window:]
        elif strategy == "window_block":      # окно сбрасывается блоком — префикс ломается реже
            hist = hist + [turn]
            if len(hist) >= 2 * w.window:
                hist = hist[-w.window:]
        elif strategy == "mask_old_results":  # старые результаты инструментов → заглушка в 10 токенов
            hist.append(turn)
            hist = [[(("res_masked", 10) + a[2:]) if (a[0] == "res" and i < len(hist) - w.window) else a
                     for a in h] for i, h in enumerate(hist)]
        elif strategy == "summary_in_system":
            hist.append(turn)
            if (q + 1) % w.summ_every == 0:
                tok = sum(a[1] for h in hist for a in h)
                unc += tok + (summary[1] if summary else 0)   # отдельный вызов суммаризатора
                out += w.summ_len
                summary, hist = ("summary", w.summ_len, q), []
    return TokenTotals(unc, cac, out, wr)


def cost(tok: TokenTotals, price: tuple) -> float:
    """Стоимость по ценам (вход, кэш, выход, запись|None) за 1 токен."""
    unc, cac, out, wr = tok.as_tuple()
    if price[3] is None:
        return unc * price[0] + cac * price[1] + out * price[2]
    return (unc - wr) * price[0] + wr * price[3] + cac * price[1] + out * price[2]


def legacy_report(w: Workload) -> str:
    """Таблица в формате исходного скрипта (python cost_model.py R) — для сверки с out_R*.txt."""
    lines = [f"Пауза между запросами: {w.hours * 3600 / w.R:.0f} с"]
    for ttl_name, ttl in TTLS:
        lines.append(f"\n== TTL: {ttl_name}")
        lines.append(f"{'стратегия':<20}{'вход, тыс.':>11}{'кэш %':>7}{'gpt-5-mini $':>14}{'gpt-4o-mini $':>15}"
                     f"{'RouterAI руб.':>15}{'gpt-6-luna $':>14}")
        for s, _desc in STRATEGIES:
            tk = simulate(w, s, ttl)
            tin = tk.input_total
            lines.append(f"{s:<20}{tin / 1e3:>11.1f}{100 * tk.cached / tin:>6.1f}%"
                         f"{cost(tk, LEGACY_PRICES['gpt-5-mini $']):>14.4f}"
                         f"{cost(tk, LEGACY_PRICES['gpt-4o-mini $']):>15.4f}"
                         f"{cost(tk, LEGACY_PRICES['gpt-5-mini RouterAI руб.']):>15.2f}"
                         f"{cost(tk, LEGACY_PRICES['gpt-6-luna $']):>14.4f}")
    return "\n".join(lines)


def history_ratio_table(rates: list[float], ttl: float, base: str = "full", w: Workload | None = None) -> list[dict]:
    """H4(б): отношение входных токенов политики к базовой (полная история) как функция числа запросов/сут."""
    w0 = w or Workload()
    rows = []
    for R in rates:
        wk = Workload(**{**w0.__dict__, "R": int(R)})
        ref = simulate(wk, base, ttl)
        row = {"requests_per_day": int(R), "ttl_s": ttl, "base": base}
        for s, _ in STRATEGIES:
            tk = simulate(wk, s, ttl)
            row[s] = {"input_tokens": tk.input_total, "cached_share": tk.cached / tk.input_total if tk.input_total else 0.0,
                      "ratio_to_base": tk.input_total / ref.input_total}
        rows.append(row)
    return rows
