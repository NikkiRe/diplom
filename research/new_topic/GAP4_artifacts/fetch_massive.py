#!/usr/bin/env python3
"""Скачивает параллельные русско-английские тексты для замера токенизации (GAP4).

Источники (через HF datasets-server, без ключа, 2026-10-06):
  - mteb/amazon_massive_intent, конфиги en и ru, split test (MASSIVE, FitzGerald et al., ACL 2023, CC BY 4.0).
    2974 параллельных высказывания-команды ассистенту, одинаковые id в обоих языках; 220 из них — сценарий iot.
  - wmt/wmt19, конфиг ru-en, split validation (newstest2018), первые 1000 пар предложений (новости).

Запуск: python3 fetch_massive.py   (кладёт JSON в ./data/)
"""
import json
import pathlib
import time
import urllib.request

HERE = pathlib.Path(__file__).parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)
API = "https://datasets-server.huggingface.co/rows?dataset={ds}&config={cfg}&split={split}&offset={off}&length=100"


def get(url):
    for a in range(6):
        try:
            return json.load(urllib.request.urlopen(url, timeout=90))
        except Exception as e:  # сетевые таймауты datasets-server
            print("retry", e)
            time.sleep(5 + 5 * a)
    raise SystemExit("failed: " + url)


def rows(ds, cfg, split, limit=None):
    out, off = [], 0
    while True:
        d = get(API.format(ds=ds, cfg=cfg, split=split, off=off))
        out += [r["row"] for r in d["rows"]]
        off += 100
        if off >= d["num_rows_total"] or (limit and off >= limit):
            return out


if __name__ == "__main__":
    for lang in ("en", "ru"):
        r = rows("mteb/amazon_massive_intent", lang, "test")
        (DATA / f"massive_test_{lang}.json").write_text(json.dumps(r, ensure_ascii=False))
        print("massive", lang, len(r))
    w = [r["translation"] for r in rows("wmt/wmt19", "ru-en", "validation", limit=1000)]
    (DATA / "wmt19_ruen_val_first1000.json").write_text(json.dumps(w, ensure_ascii=False))
    print("wmt19", len(w))
    # MASSIVE-Agents (Kulkarni et al., Findings EMNLP 2025, CC BY 4.0), формат BFCL, сплит 10k.
    # input.jsonl ~8 МБ на язык (в каждом примере все 55 функций на английском, ~9,8 тыс. токенов o200k),
    # поэтому в репозитории храним только golden и вопросы; для прогона в harness/ скачать input.jsonl заново.
    base = "https://huggingface.co/datasets/AmazonScience/massive-agents/resolve/main/massive-10k-converted-all-langs-fixed"
    out = DATA / "massive_agents"
    out.mkdir(exist_ok=True)
    for loc in ("ru-RU", "en-US"):
        urllib.request.urlretrieve(f"{base}/{loc}/golden.jsonl", out / f"{loc}_10k_golden.jsonl")
        # urllib.request.urlretrieve(f"{base}/{loc}/input.jsonl", out / f"{loc}_10k_input.jsonl")  # для harness
        print("massive-agents", loc)
