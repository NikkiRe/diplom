"""Надбавка за запись в кэш и цена рассуждения (O1 R7, §4): умолчания прежние, надбавка — по флагу."""
import pytest

from bench.costmodel import PriceBook, cache_write_tokens, reasoning_table, reasoning_turn_cost


@pytest.fixture(scope="module")
def book():
    return PriceBook.load()


def test_default_formula_unchanged(book):
    # Без флага — прежняя формула price_table.call_cost_rub и для моделей с платной записью.
    p = book.price("gpt-6-luna")
    want = (3000 - 2048) * p.input + 2048 * p.cached + 150 * p.output
    assert book.call_cost_rub("gpt-6-luna", 3000, 2048, 150) == pytest.approx(want, rel=1e-12)


def test_write_premium_is_125_percent_for_openai(book):
    for model in ("gpt-6-luna", "gpt-5.6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-5.6-terra"):
        p = book.price(model)
        assert p.cache_write == pytest.approx(1.25 * p.input, rel=1e-6), model
        base = book.call_cost_rub(model, 3000, 2048, 150)
        got = book.call_cost_rub(model, 3000, 2048, 150, cache_write=True)
        assert got - base == pytest.approx((3000 - 2048) * 0.25 * p.input, rel=1e-6), model


def test_no_premium_without_write_price_or_short_prompt(book):
    # gpt-5-mini — без поля записи; промпт короче 1024 — кэша, а значит и записи, нет.
    assert book.price("gpt-5-mini").cache_write is None
    for model, prompt, cached in (("gpt-5-mini", 3000, 0), ("gpt-6-luna", 1000, 0)):
        assert book.call_cost_rub(model, prompt, cached, 50, cache_write=True) == \
            book.call_cost_rub(model, prompt, cached, 50)
    assert cache_write_tokens(1023, 0) == 0 and cache_write_tokens(1024, 512) == 512


def test_premium_only_for_automatic_cache_families(book):
    # Anthropic пишет в кэш только по cache_control, у Google поле — цена хранения: надбавки нет.
    for model in ("anthropic/claude-haiku-4.5", "google/gemini-2.5-flash"):
        assert book.price(model).cache_write is None
        assert book.call_cost_rub(model, 3000, 0, 10, cache_write=True) == book.call_cost_rub(model, 3000, 0, 10)


def test_per_million_has_write_column(book):
    pm = book.price("gpt-6-luna").per_million()
    assert pm["cache_write"] == pytest.approx(13.80, abs=0.01)
    assert book.price("gpt-4o-mini").per_million()["cache_write"] is None


def test_reasoning_turn_matches_o1(book):
    # O1 §4 (прайс 20.10): gpt-5-mini 0,077 → 0,249 ₽ за ход при R 0 → 400, доля выхода 74 %, −69 %.
    # Здесь прайс снимка 06.10 (вход 27,60 против 26,93 ₽/1M) — те же доли, рубли выше на ≈2–3 %.
    r0 = reasoning_turn_cost(book, "gpt-5-mini", 0)
    r400 = reasoning_turn_cost(book, "gpt-5-mini", 400)
    assert r400["output_share"] == pytest.approx(0.74, abs=0.01)
    assert 1 - r0["rub_turn"] / r400["rub_turn"] == pytest.approx(0.69, abs=0.01)
    assert r0["rub_turn"] == pytest.approx(0.077, rel=0.04) and r400["rub_turn"] == pytest.approx(0.249, rel=0.04)
    luna = reasoning_turn_cost(book, "gpt-6-luna", 0)
    assert luna["cache_write"] and luna["rub_turn"] > reasoning_turn_cost(book, "gpt-6-luna", 0, False)["rub_turn"]
    rows = reasoning_table(book)["rows"]
    assert {(r["model"], r["reasoning_tokens"]) for r in rows} >= {("openai/gpt-5-mini", 400), ("openai/gpt-6-luna", 0)}
