"""Реплеер дня T0 (спецификация §5.1): подача дня в edgectx, sim-mcp и mock-llm по виртуальным часам,
сбор артефактов прогона и manifest.json.

    from bench.replay import ReplayConfig, Replayer
    run_dir = Replayer(ReplayConfig(day="day.jsonl", out_root="experiments/results", edgectx=..., sim=..., mock=...)).run()

CLI: python -m bench.replay --help; двойники агента и sim-mcp: python -m bench.replay.fakes --help
"""
from .dayfile import Day, DayEvent, load_day, parse_clock
from .replayer import ReplayConfig, Replayer, ReplayError

__all__ = ["Day", "DayEvent", "load_day", "parse_clock", "ReplayConfig", "Replayer", "ReplayError"]
