"""Восстанавливает словарь o200k_base (токенизатор GPT-4o/4.1/5 и их mini/nano)
из локальной копии без скачивания и проверяет его по официальному SHA-256.

Источник: бинарный файл из расширения Copilot в VS Code (формат: varint-длина
+ байты токена, ранг = порядковый номер). Канонический текстовый формат
tiktoken: base64(token) + " " + rank + "\n". Эталонный хеш взят из
tiktoken_ext/openai_public.py (tiktoken 0.14.0):
446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d.
Если хеш совпал, токенизация в ctx_formats.py точная, а не оценка.

Запуск: python3 prepare_o200k.py [путь_к_бинарному_o200k] -> .cache/o200k_base.tiktoken
"""
from __future__ import annotations

import base64
import hashlib
import os
import sys

SRC = "/Applications/Visual Studio Code.app/Contents/Resources/app/extensions/copilot/dist/o200k_base.tiktoken"
EXPECTED = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "o200k_base.tiktoken")


def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else SRC
    b = open(src, "rb").read()
    toks, i = [], 0
    while i < len(b):
        n = shift = 0
        while True:
            c = b[i]
            i += 1
            n |= (c & 0x7F) << shift
            if c < 0x80:
                break
            shift += 7
        toks.append(b[i:i + n])
        i += n
    txt = "".join(f"{base64.b64encode(t).decode()} {r}\n" for r, t in enumerate(toks))
    h = hashlib.sha256(txt.encode()).hexdigest()
    print(f"tokens={len(toks)} sha256={h} match={h == EXPECTED}")
    if h != EXPECTED:
        sys.exit("hash mismatch: не использовать")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "w").write(txt)
    print("written", OUT)


if __name__ == "__main__":
    main()
