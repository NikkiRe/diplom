"""daygen — генератор «рабочего дня» для стенда EdgeCtx (IMPLEMENTATION_SPEC §2).

День — JSONL: первая строка — заголовок (kind=header), далее события по возрастанию t
(секунды от полуночи суток, виртуальные часы): sensor, user_cmd, question, situation.
Генерация детерминирована по seed. CLI: ``python -m bench.daygen --help``.
"""
from .generator import PROFILES, DayConfig, generate_day, write_day

__all__ = ["PROFILES", "DayConfig", "generate_day", "write_day"]
