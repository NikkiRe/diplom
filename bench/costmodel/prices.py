"""Замороженный прайс RouterAI (₽ за токен) и стоимость вызова.

Единственный источник цен — research/new_topic/F_artifacts/price_table.py: он читает снимок
GET https://routerai.ru/api/v1/models от 06.10.2026 (routerai_models_2026-10-06.json, поле pricing,
₽ за 1 токен). Модуль загружается по пути (каталог research/ только читается), стоимость вызова
считается его же функцией call_cost_rub — формула не дублируется.

Метка снимка (путь, SHA-256 JSON, дата) кладётся в metrics.json и в прогнозы предрегистрации,
чтобы ₽ всегда можно было пересчитать по тому же прайсу (решение 10 концепции).

Запись в кэш. У моделей OpenAI с полем input_cache_write (GPT-5.6+, GPT-6) кэш автоматический, а запись в него
стоит 1,25× входа (документация OpenAI «Prompt caching», 20.10.2026). call_cost_rub(..., cache_write=True)
добавляет надбавку: записанными считаются некэшированные токены промпта от CACHE_MIN_TOKENS (короче — кэша нет).
По умолчанию надбавка выключена: прежние прогнозы и metrics.json не меняются. У Anthropic запись бывает только
по явному cache_control (агент его не шлёт), у Google поле input_cache_write — цена хранения, а не надбавка,
поэтому для них надбавки нет.
"""
from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRICE_TABLE = REPO_ROOT / "research" / "new_topic" / "F_artifacts" / "price_table.py"
PRICE_SNAPSHOT_DATE = "2026-10-06"
PRICE_TABLE_ENV = "EDGE_PRICE_TABLE"
CACHE_MIN_TOKENS = 1024                     # кэшируются промпты от 1024 токенов (OpenAI)
WRITE_PREMIUM_PREFIXES = ("openai/",)       # семейства с автоматическим кэшем и платной записью


@dataclass(frozen=True)
class Price:
    """Цены модели, ₽ за 1 токен. cached = None — провайдер не публикует цену чтения кэша; cache_write = None —
    запись в кэш не дороже входа."""
    model: str
    input: float
    cached: float | None
    output: float
    cache_write: float | None = None

    def per_million(self) -> dict:
        def m(v: float | None) -> float | None:
            return None if v is None else v * 1e6
        return {"input": m(self.input), "cached": m(self.cached), "output": m(self.output),
                "cache_write": m(self.cache_write)}


def cache_write_tokens(prompt_tokens: float, cached_tokens: float) -> float:
    """Токены, записанные в кэш вызовом: некэшированная часть промпта от CACHE_MIN_TOKENS (оценка O1 §4)."""
    return prompt_tokens - cached_tokens if prompt_tokens >= CACHE_MIN_TOKENS else 0.0


class UnknownModel(KeyError):
    pass


def normalize_model_id(name: str) -> str:
    """Имя модели из конфига/запроса → id каталога RouterAI.

    "gpt-4o-mini" → "openai/gpt-4o-mini"; синтаксис закрепления endpoint'а
    "<model>@provider=openai&allow_fallbacks=false" (решение 3 концепции) отбрасывается.
    """
    mid = name.strip()
    if "@" in mid:
        mid = mid.split("@", 1)[0]
    if "/" not in mid:
        mid = "openai/" + mid
    return mid


def _load_module(path: Path) -> ModuleType:
    """Исполнить price_table.py в отдельном модуле. Не через importlib: тот пишет __pycache__ рядом с
    исходником, а каталог research/ должен оставаться неизменным."""
    if not path.exists():
        raise ImportError(f"price table not found: {path}")
    module = ModuleType("edge_price_table")
    module.__file__ = str(path)
    code = compile(path.read_text(encoding="utf-8"), str(path), "exec")
    sys.modules[module.__name__] = module   # на случай dataclasses/pickle внутри исполняемого файла
    try:
        exec(code, module.__dict__)  # noqa: S102 — доверенный файл проекта (замороженный прайс)
    finally:
        sys.modules.pop(module.__name__, None)
    return module


class PriceBook:
    """Обёртка над price_table.py: цены по id модели и стоимость вызова в ₽."""

    def __init__(self, module: ModuleType, path: Path):
        self._m = module
        self.path = path
        snapshot = path.parent / f"routerai_models_{PRICE_SNAPSHOT_DATE}.json"
        self.snapshot_path = snapshot if snapshot.exists() else None
        self.snapshot_sha256 = (hashlib.sha256(snapshot.read_bytes()).hexdigest()
                                if self.snapshot_path else None)

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "PriceBook":
        p = Path(path or os.environ.get(PRICE_TABLE_ENV) or DEFAULT_PRICE_TABLE)
        return cls(_load_module(p), p)

    def models(self) -> list[str]:
        return sorted(self._m.BY_ID)

    def has(self, model: str) -> bool:
        return normalize_model_id(model) in self._m.BY_ID

    def price(self, model: str) -> Price:
        mid = normalize_model_id(model)
        if mid not in self._m.BY_ID:
            raise UnknownModel(mid)
        per_m = {k: self._m.rub_per_m(mid, k) for k in ("prompt", "input_cache_read", "completion")}
        write = self._m.BY_ID[mid]["pricing"].get("input_cache_write") if mid.startswith(WRITE_PREMIUM_PREFIXES) \
            else None
        return Price(mid, per_m["prompt"] / 1e6,
                     None if per_m["input_cache_read"] is None else per_m["input_cache_read"] / 1e6,
                     per_m["completion"] / 1e6, None if write is None else float(write))

    def call_cost_rub(self, model: str, prompt_tokens: float, cached_tokens: float, output_tokens: float,
                      cache_write: bool = False) -> float:
        """Стоимость вызова, ₽: (prompt − cached)·вход + cached·кэш + output·выход (функция price_table.py).

        output_tokens включает скрытые reasoning-токены — они тарифицируются как выход. cache_write=True добавляет
        надбавку за запись в кэш: cache_write_tokens · (запись − вход), если у модели она есть.
        """
        mid = normalize_model_id(model)
        if mid not in self._m.BY_ID:
            raise UnknownModel(mid)
        if cached_tokens > prompt_tokens:
            raise ValueError("cached_tokens > prompt_tokens")
        cost = float(self._m.call_cost_rub(mid, prompt_tokens, cached_tokens, output_tokens))
        if cache_write:
            p = self.price(mid)
            if p.cache_write is not None:
                cost += cache_write_tokens(prompt_tokens, cached_tokens) * (p.cache_write - p.input)
        return cost

    def provenance(self) -> dict:
        return {"price_table": str(self.path), "snapshot": str(self.snapshot_path) if self.snapshot_path else None,
                "snapshot_sha256": self.snapshot_sha256, "snapshot_date": PRICE_SNAPSHOT_DATE,
                "units": "RUB per token (RouterAI pricing, endpoint openai, tariff default)"}


@lru_cache(maxsize=4)
def default_pricebook(path: str | None = None) -> PriceBook:
    return PriceBook.load(path)
