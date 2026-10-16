"""Иллюстративная модель суточной стоимости токенов агента (одноплатник + внешняя LLM).

СОБСТВЕННАЯ ОЦЕНКА для заметки ../D_context_memory.md, не результат измерений.
Нужна, чтобы показать порядок величин и какие решения по контексту реально влияют на счёт.

Цены (за 1M токенов), сняты 06.10.2026:
  OpenAI Standard (developers.openai.com/api/docs/pricing):
    gpt-5-mini 0.25 / cached 0.025 / out 2.00 $;  gpt-4o-mini 0.15 / 0.075 / 0.60 $
  RouterAI (https://routerai.ru/api/v1/models, поля prompt / input_cache_read / completion, руб./токен):
    gpt-5-mini 27.60 / 2.76 / 220.82 руб.;  gpt-4o-mini 16.56 / 8.28 / 66.25 руб.
Правила кэша OpenAI (developers.openai.com/api/docs/guides/prompt-caching): точное совпадение префикса,
минимум 1024 токена, cached_tokens округляется вниз до кратного 128; in_memory-кэш живёт ~5–10 мин
бездействия, 24h-retention — до суток (для моделей gpt-5.x / gpt-4.1), у GPT-5.6+ — не менее 30 мин.

Модель: промпт вызова — список «атомов» (id, токены). Кэш-попадание = длина общего префикса атомов
с предыдущим вызовом, если пауза между вызовами < TTL. Внутри одного запроса вызовы идут подряд
(секунды), между запросами пауза = часы_работы * 3600 / R.
"""
from dataclasses import dataclass, field

MIN_CACHE, GRAN = 1024, 128

PRICES = {  # (input, cached, output, write) за 1 токен; write=None -> запись кэша не тарифицируется
    "gpt-5-mini $": (0.25e-6, 0.025e-6, 2.00e-6, None),
    "gpt-4o-mini $": (0.15e-6, 0.075e-6, 0.60e-6, None),
    "gpt-5-mini RouterAI руб.": (27.6025425e-6, 2.76025425e-6, 220.82034e-6, None),
    # GPT-5.6+: запись кэша 1.25x, чтение 0.1x, TTL >= 30 мин. Приближение: все некэшированные
    # входные токены вызова длиной >= MIN_CACHE считаются записанными (implicit-режим).
    "gpt-6-luna $": (0.10e-6, 0.01e-6, 0.50e-6, 0.125e-6),
}


@dataclass
class W:
    hours: float = 8      # часов работы в сутки
    R: int = 40           # запросов пользователя в сутки
    k: int = 3            # вызовов LLM на запрос (k-1 вызов инструмента + ответ)
    S: int = 4000         # system prompt + схемы инструментов MCP (стабильно)
    P: int = 300          # профиль пользователя (меняется раз в 10 запросов)
    D: int = 600          # снимок состояния оборудования (меняется каждый запрос)
    u: int = 60           # реплика пользователя
    o: int = 150          # выход модели на вызов
    r: int = 300          # результат инструмента (датчик / MCP)
    window: int = 5       # окно ходов для оконных стратегий
    summ_every: int = 7   # суммаризация каждые ~20 сообщений (7 запросов * 3 сообщения)
    summ_len: int = 400


def common_prefix(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += x[1]
    return n


def run(w: W, strategy: str, ttl: float):
    gap = w.hours * 3600 / w.R
    prev, prev_t, t = None, -1e18, 0.0
    unc = cac = out = 0
    wr = 0               # некэшированные токены вызовов длиной >= MIN_CACHE (кандидаты на запись)
    hist = []            # список ходов; ход = список атомов
    summary = None
    prof_v = 0
    for q in range(w.R):
        if q and q % 10 == 0:
            prof_v += 1
        snap = ("snap", w.D, q)              # новая версия снимка каждый запрос
        prof = ("prof", w.P, prof_v)
        head = [("sys", w.S)]
        turn = [("u", w.u, q)]
        for c in range(w.k):
            t = q * gap + c * 5.0
            if strategy == "snapshot_head":
                atoms = head + [snap, prof] + [a for h in hist for a in h] + turn
            elif strategy == "summary_in_system":
                atoms = head + ([summary] if summary else []) + [prof] + \
                        [a for h in hist for a in h] + [snap] + turn
            else:
                atoms = head + [prof] + [a for h in hist for a in h] + [snap] + turn
            atoms = [tuple(a) for a in atoms]
            total = sum(a[1] for a in atoms)
            hit = 0
            if prev is not None and t - prev_t < ttl:
                hit = common_prefix(prev, atoms)
                hit = 0 if hit < MIN_CACHE else (hit // GRAN) * GRAN
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
        # ход завершён
        if strategy in ("snapshot_head", "full"):
            hist.append(turn)
        elif strategy == "window_slide":
            hist = (hist + [turn])[-w.window:]
        elif strategy == "window_block":     # сбрасываем окно блоком, реже ломая префикс
            hist = hist + [turn]
            if len(hist) >= 2 * w.window:
                hist = hist[-w.window:]
        elif strategy == "mask_old_results":  # старые результаты инструментов -> заглушка
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
    return unc, cac, out, wr


def cost(tok, p):
    unc, cac, out, wr = tok
    if p[3] is None:
        return unc * p[0] + cac * p[1] + out * p[2]
    return (unc - wr) * p[0] + wr * p[3] + cac * p[1] + out * p[2]


STRATS = [
    ("snapshot_head", "полная история, снимок оборудования в начале промпта"),
    ("full", "полная история, снимок в хвосте (стабильный префикс)"),
    ("window_slide", "скользящее окно 5 ходов"),
    ("window_block", "окно, сброс блоком (5..10 ходов)"),
    ("mask_old_results", "маскирование старых результатов инструментов"),
    ("summary_in_system", "сводка в system prompt каждые ~20 сообщений (как PicoClaw)"),
]
TTLS = [("без кэша", 0), ("in_memory ~10 мин", 600), ("30 мин", 1800), ("24h", 86400)]

if __name__ == "__main__":
    import sys
    w = W()
    if len(sys.argv) > 1:
        w.R = int(sys.argv[1])
    print(w)
    print("Пауза между запросами: %.0f с" % (w.hours * 3600 / w.R))
    for ttl_name, ttl in TTLS:
        print(f"\n== TTL: {ttl_name}")
        print(f"{'стратегия':<20}{'вход, тыс.':>11}{'кэш %':>7}{'gpt-5-mini $':>14}{'gpt-4o-mini $':>15}{'RouterAI руб.':>15}{'gpt-6-luna $':>14}")
        for s, desc in STRATS:
            tk = run(w, s, ttl)
            tin = tk[0] + tk[1]
            print(f"{s:<20}{tin/1e3:>11.1f}{100*tk[1]/tin:>6.1f}%"
                  f"{cost(tk, PRICES['gpt-5-mini $']):>14.4f}{cost(tk, PRICES['gpt-4o-mini $']):>15.4f}"
                  f"{cost(tk, PRICES['gpt-5-mini RouterAI руб.']):>15.2f}"
                  f"{cost(tk, PRICES['gpt-6-luna $']):>14.4f}")
