#!/usr/bin/env python3
"""Таблица цен RouterAI (₽ за 1M токенов) против официальных цен OpenAI ($ за 1M)
и иллюстративная модель стоимости «работы агента во времени».

Источники:
  - routerai_models_2026-10-06.json: снимок GET https://routerai.ru/api/v1/models (без авторизации), 2026-10-06.
    Поле pricing: рубли за 1 токен (pricing_units = token).
  - OFFICIAL_USD: https://developers.openai.com/api/docs/pricing (Standard tier), прочитано 2026-10-06.

Запуск: python3 price_table.py
Иллюстрация стоимости НЕ является измерением: параметры нагрузки заданы вручную ниже.
"""
import json
import pathlib

HERE = pathlib.Path(__file__).parent
MODELS = json.loads((HERE / "routerai_models_2026-10-06.json").read_text())["data"]
BY_ID = {m["id"]: m for m in MODELS}

# $ за 1M токенов: (input, cached input, output), Standard tier, developers.openai.com/api/docs/pricing
OFFICIAL_USD = {
    "openai/gpt-4o-mini": (0.15, 0.075, 0.60),
    "openai/gpt-4.1-nano": (0.10, 0.025, 0.40),
    "openai/gpt-4.1-mini": (0.40, 0.10, 1.60),
    "openai/gpt-5-nano": (0.05, 0.005, 0.40),
    "openai/gpt-5-mini": (0.25, 0.025, 2.00),
    "openai/gpt-5.4-nano": (0.20, 0.02, 1.25),
    "openai/gpt-5.4-mini": (0.75, 0.075, 4.50),
    "openai/gpt-5.6-luna": (0.20, 0.02, 1.20),
    "openai/o4-mini": (1.10, 0.275, 4.40),
}


def rub_per_m(model_id, key):
    v = BY_ID[model_id]["pricing"].get(key)
    return None if v is None else float(v) * 1e6


def table():
    print(f"{'model':22s} {'ctx':>8s} {'in ₽':>8s} {'cache ₽':>8s} {'out ₽':>8s} | {'in $':>6s} {'out $':>6s} | ₽/$ (in)  tools")
    for mid, (ui, uc, uo) in OFFICIAL_USD.items():
        m = BY_ID[mid]
        i, c, o = (rub_per_m(mid, k) for k in ("prompt", "input_cache_read", "completion"))
        tools = "tools" in (m.get("supported_parameters") or [])
        print(f"{mid[7:]:22s} {m['context_length']:>8d} {i:8.2f} {c:8.2f} {o:8.2f} | {ui:6.3f} {uo:6.3f} | {i/ui:7.2f}   {tools}")


def call_cost_rub(mid, prompt_tokens, cached_tokens, output_tokens):
    """Стоимость одного вызова в рублях. output_tokens включает скрытые reasoning-токены (тарифицируются как выход)."""
    i, c, o = (rub_per_m(mid, k) for k in ("prompt", "input_cache_read", "completion"))
    unc = prompt_tokens - cached_tokens
    return (unc * i + cached_tokens * (c if c is not None else i) + output_tokens * o) / 1e6


def illustration():
    # Иллюстративный сценарий (НЕ измерение): системный промпт + каталог инструментов MCP = 2 000 токенов (кэшируемый префикс),
    # контекст пользователя/оборудования + история = 1 000 токенов, ответ 150 видимых токенов.
    # Для reasoning-моделей (gpt-5-*, o4-mini) добавлено 300 скрытых reasoning-токенов при низком reasoning_effort — допущение.
    calls_per_request = 3          # шаги агентного цикла на одну реплику пользователя
    requests_per_hour = 20
    hours_per_day = 8
    calls_day = calls_per_request * requests_per_hour * hours_per_day
    print(f"\nСценарий: {calls_day} вызовов LLM в сутки (= {calls_per_request}×{requests_per_hour}×{hours_per_day})")
    for mid in OFFICIAL_USD:
        reasoning = mid.startswith(("openai/gpt-5", "openai/o4"))
        out = 150 + (300 if reasoning else 0)
        no_cache = call_cost_rub(mid, 3000, 0, out)
        with_cache = call_cost_rub(mid, 3000, 2000, out)
        print(f"{mid[7:]:22s} вызов: {no_cache:.4f} ₽ (без кэша) / {with_cache:.4f} ₽ (кэш 2000) | сутки: {no_cache*calls_day:7.2f} / {with_cache*calls_day:7.2f} ₽ | 30 дней: {with_cache*calls_day*30:8.1f} ₽")


if __name__ == "__main__":
    table()
    illustration()
