"""Числа пульта по-русски (U4 §7.1): запятая в дробях, «—» вместо отсутствующего, минус «−», неразрывный пробел в
разрядах (с 5 цифр; в моноширинном шрифте узкого пробела нет — у JetBrains Mono нет и знака U+202F). Зеркало —
web/lib/fmt.js; общие векторы проверяет тест на обеих сторонах."""
from __future__ import annotations

import math
import re
from typing import Optional

DASH = "—"
MINUS = "−"
GROUP = "\u00a0"    # неразрывный пробел: число не рвётся переносом
_POINT = re.compile(r"(?<=\d)\.(?=\d)")


def _group(digits: str) -> str:
    if len(digits) < 5:
        return digits
    head = len(digits) % 3 or 3
    parts = [digits[:head]] + [digits[i:i + 3] for i in range(head, len(digits), 3)]
    return GROUP.join(parts)


def num(v: Optional[float], nd: int = 2) -> str:
    """Число с nd знаками после запятой; None, NaN, ±inf — «—». −0,00 пишется как 0,00."""
    if v is None or isinstance(v, bool):
        return DASH
    try:
        x = float(v)
    except (TypeError, ValueError):
        return DASH
    if not math.isfinite(x):
        return DASH
    text = ("%." + str(max(0, nd)) + "f") % abs(x)
    whole, _, frac = text.partition(".")
    neg = x < 0 and any(c not in "0." for c in text)
    return (MINUS if neg else "") + _group(whole) + ("," + frac if frac else "")


def unit(v: Optional[float], nd: int, u: str) -> str:
    """Число и единица через неразрывный пробел; отсутствующее — «—» без единицы."""
    s = num(v, nd)
    return s if s == DASH else s + " " + u


def pct(share: Optional[float], nd: int = 0) -> str:
    """Доля 0…1 в процентах."""
    return DASH if share is None else unit(100.0 * share, nd, "%")


def reason(text: str) -> str:
    """«код: текст» причины safety_gate по-русски: точка в дробях текста — запятая («путь 0.06 м» → «путь 0,06 м»);
    код не трогается. Делает шлюз (сборщик записи), браузер показывает как есть."""
    code, sep, rest = str(text).partition(":")
    return code + sep + _POINT.sub(",", rest) if sep else _POINT.sub(",", code)
