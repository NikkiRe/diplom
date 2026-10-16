"""Запись и воспроизведение пар «нормализованный запрос → ответ» (режимы record и replay).

Ключ записи — SHA-256 канонического JSON тела запроса (ключи отсортированы, без пробелов, UTF-8),
из которого удалены поля, не влияющие на ответ модели (IGNORED_FIELDS; набор можно расширить).
Файл записи — JSONL: {"key", "status", "response", "request", "ts_wall", "latency_ms", "seq"}.

Если один и тот же запрос записан несколько раз (недетерминизм API, A/A), при воспроизведении ответы
выдаются по порядку записи; после последнего повторяется последний.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from .tokens import canonical_json

# Поля, которые не меняют ответ модели или различаются между прогонами одной конфигурации.
IGNORED_FIELDS = ("user", "metadata", "store", "prompt_cache_key", "prompt_cache_retention", "service_tier",
                  "stream_options")


def normalize_request(body: dict, ignore: tuple[str, ...] | list[str] = IGNORED_FIELDS) -> dict:
    return {k: v for k, v in body.items() if k not in set(ignore)}


def request_key(body: dict, ignore: tuple[str, ...] | list[str] = IGNORED_FIELDS) -> str:
    return hashlib.sha256(canonical_json(normalize_request(body, ignore)).encode("utf-8")).hexdigest()


class RecordStore:
    def __init__(self, path: str | Path | None, ignore: tuple[str, ...] | list[str] = IGNORED_FIELDS):
        self.path = Path(path) if path else None
        self.ignore = tuple(ignore)
        self._by_key: dict[str, list[dict]] = {}
        self._cursor: dict[str, int] = {}
        self._lock = threading.Lock()
        self._seq = 0
        if self.path and self.path.exists():
            self._load()

    def _load(self) -> None:
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                self._by_key.setdefault(rec["key"], []).append(rec)
                self._seq = max(self._seq, int(rec.get("seq", 0)))

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_key.values())

    def key(self, body: dict) -> str:
        return request_key(body, self.ignore)

    def add(self, body: dict, status: int, response: Any, ts_wall: float, latency_ms: float) -> dict:
        key = self.key(body)
        with self._lock:
            self._seq += 1
            rec = {"key": key, "seq": self._seq, "status": status, "response": response,
                   "request": normalize_request(body, self.ignore), "ts_wall": ts_wall, "latency_ms": latency_ms}
            self._by_key.setdefault(key, []).append(rec)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            return rec

    def lookup(self, body: dict) -> dict | None:
        key = self.key(body)
        with self._lock:
            recs = self._by_key.get(key)
            if not recs:
                return None
            i = self._cursor.get(key, 0)
            self._cursor[key] = i + 1
            return recs[min(i, len(recs) - 1)]

    def rewind(self) -> None:
        with self._lock:
            self._cursor.clear()
