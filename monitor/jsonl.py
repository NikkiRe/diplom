"""Строгое чтение JSONL: каждая непустая строка — JSON-объект.

Отличия от ``json.loads`` по умолчанию, важные для монитора:
  * повтор ключа в объекте — ошибка (по RFC 8259 поведение не определено, а парсеры берут разное значение);
  * NaN, Infinity, -Infinity — ошибка (это не JSON, хотя Python их принимает);
  * числа, не помещающиеся в double (1e400), — ошибка, а не inf;
  * строка не в UTF-8 — ошибка этой строки, остальные читаются.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class StrictJSONError(ValueError):
    pass


def _no_duplicates(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise StrictJSONError(f"повтор ключа {key!r}")
        obj[key] = value
    return obj


def _no_constants(name):
    raise StrictJSONError(f"{name} — не JSON")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise StrictJSONError(f"число {text} не помещается в double")
    return value


def loads_strict(text: str):
    """json.loads с отказом от повторов ключей, NaN/Infinity и переполнения double."""
    return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants,
                      parse_float=_finite_float)


@dataclass(frozen=True)
class Line:
    """Строка файла: номер (с 1) и разобранный объект либо текст ошибки."""
    no: int
    obj: dict | None
    error: str | None = None


def read_lines(source: str | Path | Iterable[str | bytes]) -> list[Line]:
    """Прочитать JSONL из файла или из последовательности строк (для тестов)."""
    if isinstance(source, (str, Path)):
        with open(source, "rb") as f:
            raw_lines = f.read().split(b"\n")
    else:
        raw_lines = list(source)
    out: list[Line] = []
    for no, raw in enumerate(raw_lines, 1):
        if isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as e:
                out.append(Line(no, None, f"не UTF-8: {e}"))
                continue
        else:
            text = raw
        if not text.strip():
            continue
        try:
            obj = loads_strict(text)
        except (ValueError, RecursionError) as e:
            out.append(Line(no, None, f"не JSON: {e}"))
            continue
        if not isinstance(obj, dict):
            out.append(Line(no, None, f"ожидается JSON-объект, получено {type(obj).__name__}"))
            continue
        out.append(Line(no, obj))
    return out
