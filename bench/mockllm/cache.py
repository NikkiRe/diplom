"""Эмуляция префиксного кэша провайдера (правила OpenAI prompt caching).

- Кэш-попадание — точное совпадение префикса с промптом одного из недавних запросов.
- Кэшируются промпты от min_tokens (1024) токенов; cached_tokens округляется вниз до кратного granularity (128).
- Запись живёт ttl_s секунд без обращений; попадание продлевает жизнь (in-memory retention ≈ 5–10 мин,
  поэтому TTL — параметр; 0 — кэш выключен).
- Время — то, что передаёт сервер: виртуальные часы T0 (X-Bench-Clock или /control/clock) либо стенные.

Последовательности хранятся как bytes (int32 little-endian): длина общего префикса ищется бинарным поиском
по срезам (сравнение срезов bytes — memcmp), без numpy.
"""
from __future__ import annotations

import threading
from array import array
from dataclasses import dataclass


def _pack(seq: list[int]) -> bytes:
    return array("i", seq).tobytes()


def common_prefix_len(a: bytes, b: bytes, item: int = 4) -> int:
    """Длина общего префикса двух упакованных последовательностей, в элементах."""
    n = min(len(a), len(b)) // item
    if a[: n * item] == b[: n * item]:
        return n
    lo, hi = 0, n - 1  # инвариант: префикс длины lo совпадает, длины hi + 1 — нет
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if a[: mid * item] == b[: mid * item]:
            lo = mid
        else:
            hi = mid - 1
    return lo


@dataclass
class _Entry:
    data: bytes
    n_tokens: int
    last_used: float


@dataclass(frozen=True)
class CacheResult:
    cached_tokens: int     # сколько входных токенов провайдер посчитал бы из кэша
    common_prefix: int     # длина общего префикса с лучшей записью (до порога и округления)
    entries: int           # записей в кэше после операции


class PrefixCache:
    def __init__(self, ttl_s: float = 300.0, min_tokens: int = 1024, granularity: int = 128,
                 max_entries: int = 256):
        if granularity <= 0 or min_tokens < 0:
            raise ValueError("bad cache parameters")
        self.ttl_s = float(ttl_s)
        self.min_tokens = int(min_tokens)
        self.granularity = int(granularity)
        self.max_entries = int(max_entries)
        self._entries: list[_Entry] = []
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.ttl_s > 0

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    def _quantize(self, common: int) -> int:
        if common < self.min_tokens:
            return 0
        return (common // self.granularity) * self.granularity

    def lookup_and_store(self, seq: list[int], now: float, prompt_tokens: int | None = None) -> CacheResult:
        """Найти лучший живой префикс, продлить его, записать текущий промпт. Возвращает cached_tokens
        (не больше prompt_tokens, если он задан — оценка промпта и последовательность различаются).

        Право на кэш — по длине промпта провайдера: при prompt_tokens < min_tokens кэша нет, и усечённое до
        prompt_tokens значение тоже не бывает меньше min_tokens (правило OpenAI: минимум 1024)."""
        if not self.enabled:
            return CacheResult(0, 0, 0)
        data = _pack(seq)
        with self._lock:
            self._entries = [e for e in self._entries if now - e.last_used <= self.ttl_s]
            best, best_entry = 0, None
            for e in self._entries:
                c = common_prefix_len(e.data, data)
                if c > best:
                    best, best_entry = c, e
            cached = self._quantize(best)
            if prompt_tokens is not None:
                cached = min(cached, (prompt_tokens // self.granularity) * self.granularity)
                if prompt_tokens < self.min_tokens or cached < self.min_tokens:
                    cached = 0
            if best_entry is not None and cached > 0:
                best_entry.last_used = now          # продлевает жизнь только настоящее попадание
            if len(seq) >= self.min_tokens and (prompt_tokens is None or prompt_tokens >= self.min_tokens):
                # Новая запись поглощает записи, которые целиком являются её префиксом.
                self._entries = [e for e in self._entries
                                 if not (e.n_tokens <= len(seq) and data[: len(e.data)] == e.data)]
                self._entries.append(_Entry(data, len(seq), now))
                if len(self._entries) > self.max_entries:
                    self._entries.sort(key=lambda e: e.last_used)
                    self._entries = self._entries[-self.max_entries:]
            return CacheResult(cached, best, len(self._entries))
