"""Подсчёт токенов запроса Chat Completions для usage оракула и эмуляции кэша провайдера.

Токенизатор — o200k_base (GPT-4o/4.1/5 и их mini/nano) через tiktoken. Словарь берётся без сети, если
есть локальная копия: переменная TIKTOKEN_CACHE_DIR, иначе кэш, проверенный по официальному SHA-256 в
research/new_topic/GAP2_artifacts/.cache/tk (prepare_o200k.py); иначе tiktoken скачает словарь сам.
Без tiktoken — эвристика (≈4 символа на токен), и это помечается в журнале (tokenizer = "heuristic").

Оценка prompt_tokens — приближение OpenAI Cookbook («How to count tokens with tiktoken», константы
gpt-4o/gpt-4o-mini): 3 токена на сообщение + токены значений полей, +1 за name, +3 на затравку ответа;
инструменты — num_tokens_for_tools (func_init 7, prop_init 3, prop_key 3, enum_init −3, enum_item 3,
func_end 12), вложенные объекты обходятся рекурсивно. Провайдер оплачивает свой отрендеренный формат,
поэтому оценка калибруется линейно (решение 6 концепции: a + b·оценка по 20–30 запросам E0-P).

Для эмуляции кэша строится последовательность токенов запроса в порядке рендеринга: system/developer в
начале → инструменты (JSON в исходном порядке ключей тела, без сортировки) → остальные сообщения. Общий
префикс считается по ней.
"""
from __future__ import annotations

import json
import os
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_TIKTOKEN_CACHE = REPO_ROOT / "research" / "new_topic" / "GAP2_artifacts" / ".cache" / "tk"

# Константы OpenAI Cookbook для gpt-4o / gpt-4o-mini.
TOKENS_PER_MESSAGE = 3
TOKENS_PER_NAME = 1
REPLY_PRIMING = 3
FUNC_INIT, PROP_INIT, PROP_KEY, ENUM_INIT, ENUM_ITEM, FUNC_END = 7, 3, 3, -3, 3, 12
TOOL_CALL_OVERHEAD = 3     # на каждый вызов в ответе/истории (имя + аргументы + разметка) — допущение
IMAGE_PART_TOKENS = 85     # картинка detail=low у gpt-4o (OpenAI vision guide); грубо, вне ядра


class Tokenizer:
    """Обёртка: encode(text) → список int, count(text) → int."""

    name = "abstract"

    def encode(self, text: str) -> list[int]:  # pragma: no cover - интерфейс
        raise NotImplementedError

    def count(self, text: str) -> int:
        return len(self.encode(text)) if text else 0

    def truncate(self, text: str, n: int) -> str:  # pragma: no cover - интерфейс
        """Начало текста длиной не больше n токенов."""
        raise NotImplementedError


class TiktokenTokenizer(Tokenizer):
    def __init__(self, encoding):
        self._enc = encoding
        self.name = encoding.name

    def encode(self, text: str) -> list[int]:
        # disallowed_special=() — специальные токены в тексте пользователя считаются обычным текстом.
        return self._enc.encode(text, disallowed_special=()) if text else []

    def truncate(self, text: str, n: int) -> str:
        ids = self.encode(text)
        if len(ids) <= n:
            return text
        out = self._enc.decode(ids[:max(0, n)])
        while out and self.count(out) > n:      # стык BPE после декодирования может дать +1 токен
            out = out[:-1]
        return out


class HeuristicTokenizer(Tokenizer):
    """Запасной вариант без tiktoken: слова режутся на куски до 4 символов, знаки — по одному.

    Детерминирован (crc32 кусков), годится для эмуляции префикс-кэша; счёт токенов — оценка.
    """

    name = "heuristic"
    _pattern = re.compile(r"\w{1,4}|[^\w\s]|\s+", re.UNICODE)

    def encode(self, text: str) -> list[int]:
        return [zlib.crc32(m.encode("utf-8")) & 0x7FFFFFFF for m in self._pattern.findall(text or "")]

    def truncate(self, text: str, n: int) -> str:
        return "".join(self._pattern.findall(text or "")[:max(0, n)])


_TOKENIZER_CACHE: dict[str, Tokenizer] = {}


def load_tokenizer(prefer: str = "o200k_base") -> Tokenizer:
    """o200k_base через tiktoken (локальный словарь, если есть) или эвристика."""
    if prefer in _TOKENIZER_CACHE:
        return _TOKENIZER_CACHE[prefer]
    tok: Tokenizer
    if prefer == "heuristic":
        tok = HeuristicTokenizer()
    else:
        try:
            if "TIKTOKEN_CACHE_DIR" not in os.environ and LOCAL_TIKTOKEN_CACHE.is_dir():
                os.environ["TIKTOKEN_CACHE_DIR"] = str(LOCAL_TIKTOKEN_CACHE)
            import tiktoken  # noqa: PLC0415 — необязательная зависимость

            tok = TiktokenTokenizer(tiktoken.get_encoding(prefer))
        except Exception:  # noqa: BLE001 — нет пакета, нет сети, битый кэш: работаем на эвристике
            tok = HeuristicTokenizer()
    _TOKENIZER_CACHE[prefer] = tok
    return tok


def canonical_json(obj: Any) -> str:
    """Каноническая сериализация (спецификация §0.5): ключи отсортированы, без пробелов, UTF-8 без \\u."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def body_json(obj: Any) -> str:
    """Сериализация в исходном порядке ключей тела запроса (json.loads сохраняет порядок): так эмуляция кэша
    видит нестабильный порядок ключей схем, на котором настоящий провайдер дал бы промах."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def fit_tokens(text: str, n: int, tok: "Tokenizer", filler: str = " Других событий нет.") -> str:
    """Текст ровно ≈n токенов: длинный обрезается, короткий дополняется повторами filler (для детерминированной
    сводки оракула заданной длины)."""
    if n <= 0:
        return ""
    if tok.count(text) < n:
        need = n - tok.count(text)
        per = max(1, tok.count(filler))
        text = text + filler * (need // per + 2)
    return tok.truncate(text, n)


def message_text(content: Any) -> tuple[str, int]:
    """Текст содержимого сообщения и число частей-картинок (content: str | list[part] | None)."""
    if content is None:
        return "", 0
    if isinstance(content, str):
        return content, 0
    if isinstance(content, list):
        texts, images = [], 0
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in ("text", "input_text"):
                    texts.append(str(part.get("text", "")))
                elif part.get("type") in ("image_url", "input_image"):
                    images += 1
                else:
                    texts.append(canonical_json(part))
            else:
                texts.append(str(part))
        return "".join(texts), images
    return canonical_json(content), 0


def _tool_calls_text(tool_calls: list) -> list[str]:
    out = []
    for tc in tool_calls or []:
        fn = (tc or {}).get("function") or {}
        out.append(f"{fn.get('name', '')}({fn.get('arguments', '')})")
    return out


def count_message(msg: dict, tok: Tokenizer) -> int:
    """Токены одного сообщения по Cookbook (+ вызовы инструментов и картинки)."""
    n = TOKENS_PER_MESSAGE
    for key, value in msg.items():
        if key == "content":
            text, images = message_text(value)
            n += tok.count(text) + images * IMAGE_PART_TOKENS
        elif key == "tool_calls":
            for s in _tool_calls_text(value):
                n += tok.count(s) + TOOL_CALL_OVERHEAD
        elif key in ("role", "name", "tool_call_id"):
            n += tok.count(str(value))
            if key == "name":
                n += TOKENS_PER_NAME
        # прочие поля (refusal, audio и т.п.) провайдер в промпт не рендерит
    return n


def _strip_dot(s: str) -> str:
    return s[:-1] if s.endswith(".") else s


def _count_properties(props: dict, tok: Tokenizer) -> int:
    n = PROP_INIT
    for key, p in props.items():
        p = p if isinstance(p, dict) else {}
        n += PROP_KEY
        if "enum" in p and isinstance(p["enum"], list):
            n += ENUM_INIT
            for item in p["enum"]:
                n += ENUM_ITEM + tok.count(str(item))
        ptype = p.get("type", "")
        if isinstance(ptype, list):
            ptype = "|".join(map(str, ptype))
        n += tok.count(f"{key}:{ptype}:{_strip_dot(str(p.get('description', '')))}")
        nested = p.get("properties")
        if isinstance(nested, dict) and nested:      # расширение Cookbook: вложенный объект
            n += _count_properties(nested, tok)
        items = p.get("items")
        if isinstance(items, dict) and isinstance(items.get("properties"), dict) and items["properties"]:
            n += _count_properties(items["properties"], tok)
    return n


def count_tools(tools: list, tok: Tokenizer, method: str = "cookbook") -> int:
    """Токены описаний инструментов. method: "cookbook" (по умолчанию) или "json" (o200k канонического JSON)."""
    if not tools:
        return 0
    if method == "json":
        return sum(tok.count(canonical_json(t)) for t in tools)
    if method != "cookbook":
        raise ValueError("method must be 'cookbook' or 'json'")
    n = 0
    for t in tools:
        fn = (t or {}).get("function") or t or {}
        n += FUNC_INIT
        n += tok.count(f"{fn.get('name', '')}:{_strip_dot(str(fn.get('description', '')))}")
        props = ((fn.get("parameters") or {}).get("properties")) or {}
        if props:
            n += _count_properties(props, tok)
    return n + FUNC_END


@dataclass
class PromptAccount:
    """Учёт промпта одного запроса."""
    prompt_tokens: int                  # оценка после калибровки
    raw_estimate: int                   # оценка до калибровки
    segments: dict[str, int]            # tools / system / history / turn (+ priming)
    sequence: list[int] = field(repr=False, default_factory=list)  # каноническая последовательность для кэша
    tokenizer: str = ""


def split_segments(messages: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """(system-голова, история, ход). Голова — подряд идущие system/developer в начале; ход — от последнего
    сообщения пользователя до конца (при продолжении после tool — вместе с вызовами и результатами)."""
    i = 0
    while i < len(messages) and messages[i].get("role") in ("system", "developer"):
        i += 1
    head, rest = messages[:i], messages[i:]
    last_user = max((k for k, m in enumerate(rest) if m.get("role") == "user"), default=None)
    if last_user is None:
        cut = max(len(rest) - 1, 0)
    else:
        cut = last_user
    return head, rest[:cut], rest[cut:]


def _message_sequence(msg: dict, tok: Tokenizer) -> list[int]:
    text, _ = message_text(msg.get("content"))
    parts = [f"<|{msg.get('role', '')}|>"]
    if msg.get("name"):
        parts.append(f"[{msg['name']}]")
    if msg.get("tool_call_id"):
        parts.append(f"[{msg['tool_call_id']}]")
    parts.append(text)
    parts.extend(_tool_calls_text(msg.get("tool_calls") or []))
    return tok.encode("\n".join(parts) + "\n")


def account_request(body: dict, tok: Tokenizer, tool_method: str = "cookbook",
                    calib: tuple[float, float] = (0.0, 1.0)) -> PromptAccount:
    """Оценка prompt_tokens по сегментам и каноническая последовательность токенов запроса."""
    messages = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
    tools = body.get("tools") or []
    head, history, turn = split_segments(messages)
    seg = {
        "tools": count_tools(tools, tok, tool_method),
        "system": sum(count_message(m, tok) for m in head),
        "history": sum(count_message(m, tok) for m in history),
        "turn": sum(count_message(m, tok) for m in turn),
        "priming": REPLY_PRIMING,
    }
    raw = sum(seg.values())
    a, b = calib
    calibrated = max(0, int(round(a + b * raw)))
    seq: list[int] = []
    for m in head:
        seq.extend(_message_sequence(m, tok))
    for t in tools:
        seq.extend(tok.encode(body_json(t)))
    for m in history + turn:
        seq.extend(_message_sequence(m, tok))
    return PromptAccount(calibrated, raw, seg, seq, tok.name)


def count_completion(message: dict, tok: Tokenizer) -> int:
    """Выходные токены ответа: текст + вызовы инструментов (без служебных 3 токенов сообщения)."""
    text, _ = message_text(message.get("content"))
    n = tok.count(text)
    for s in _tool_calls_text(message.get("tool_calls") or []):
        n += tok.count(s) + TOOL_CALL_OVERHEAD
    return max(n, 1)
