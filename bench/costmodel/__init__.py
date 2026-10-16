"""costmodel — прогнозы суточных затрат агента и ₽ по замороженному прайсу RouterAI.

    from bench.costmodel import PriceBook, h1_table, prereg_forecasts
    book = PriceBook.load()                       # research/new_topic/F_artifacts/price_table.py
    book.call_cost_rub("gpt-4o-mini", 3000, 2048, 150)
    h1_table()["rows"]                            # отношение вызовов heartbeat/гейт по профилям L…S200

CLI: python -m bench.costmodel {h1,history,prices,prereg} --help
"""
from .forecast import (CASAS_EVENTS_PER_DAY, GATE_WAKES_PER_DAY, HEARTBEAT_PERIOD_S, K_DIST, PROFILES, CallShape,
                       H1Params, background_share_events, daily_cost_bounds, expected_k, expected_k_effective,
                       h1_breakeven_commands,
                       h1_ratio, h1_sensitivity, h1_table, heartbeat_ticks, keep_warm_period, prereg_forecasts)
from .history_model import STRATEGIES, TTLS, TokenTotals, Workload, simulate
from .prices import Price, PriceBook, UnknownModel, default_pricebook, normalize_model_id

__all__ = [
    "CASAS_EVENTS_PER_DAY", "GATE_WAKES_PER_DAY", "HEARTBEAT_PERIOD_S", "K_DIST", "PROFILES", "CallShape", "H1Params",
    "background_share_events", "daily_cost_bounds", "expected_k", "expected_k_effective", "h1_breakeven_commands", "h1_ratio",
    "h1_sensitivity", "h1_table", "heartbeat_ticks", "keep_warm_period", "prereg_forecasts", "STRATEGIES", "TTLS",
    "TokenTotals", "Workload", "simulate", "Price", "PriceBook", "UnknownModel", "default_pricebook",
    "normalize_model_id",
]
