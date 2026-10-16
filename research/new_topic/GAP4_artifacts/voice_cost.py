#!/usr/bin/env python3
"""GAP4. Вклад облачных ASR/TTS RouterAI в ₽/сутки и трафик голосового канала (иллюстративный расчёт, НЕ измерение).

Цены: снимок каталога RouterAI F_artifacts/routerai_models_2026-10-06.json (GET https://routerai.ru/api/v1/models, без ключа).
  ASR: поле pricing.seconds — ₽ за секунду входного аудио (docs: /api/v1/audio/transcriptions, «оплата за секунду входного аудио»,
       эндпоинт нестриминговый, лимит файла 25 МБ).
  TTS: поле pricing.prompt — ₽ за единицу входного текста; docs RouterAI: «большинство TTS-моделей тарифицируются посимвольно».
       У Fish Audio в первоисточнике единица — UTF-8 байт ($15 / M UTF-8 bytes, docs.fish.audio), а кириллица = 2 байта на букву.
       Поэтому для TTS считаются две гипотезы: «за символ» и «за байт UTF-8». Какая верна у RouterAI — проверить по usage.cost с ключом.

Нагрузка (параметры, источник):
  - длительность голосовой команды 4,0 с: Speech-MASSIVE ru-RU — 2033 высказывания / 2,25 ч (validation) и 2974 / 3,44 ч (test)
    → 3,98 и 4,16 с (HF dataset card FBK-MT/Speech-MASSIVE);
  - длина ответа 111 символов: медиана русского предложения WMT19 newstest2018 (data/wmt19_ruen_val_first1000.json);
  - байт UTF-8 на символ русского текста 1,83–1,85 (замер на MASSIVE ru и WMT19 ru, token_ru_en.py);
  - скорость речи ≥ 8,7 симв/с: 36,4 симв. на высказывание MASSIVE ru test / 4,16 с (оценка снизу: в записях есть паузы).
  - Opus: «sweet spot» 16–20 кбит/с для широкополосной речи (RFC 6716, §2.1.1); PCM 16 кГц × 16 бит = 32 000 Б/с;
    base64 в JSON (+33 %) — вариант input_audio у RouterAI.
Запуск: python3 voice_cost.py
"""
import json
import math
import pathlib

HERE = pathlib.Path(__file__).parent
CAT = {m["id"]: m for m in json.loads((HERE.parent / "F_artifacts" / "routerai_models_2026-10-06.json").read_text())["data"]}
USD = 110.41  # ₽/$ по F (price_table.py)

CMD_S = 4.0
ANSWER_CHARS = 111
BYTES_PER_CHAR_RU = 1.84
SPEECH_CPS = 8.7

# ru: 'yes' = русский явно указан в первоисточнике; '?' = не проверено
ASR = {
    "openai/whisper-large-v3-turbo": "yes (Whisper, 99 яз.)",
    "qwen/qwen3-asr-0.6b": "yes (модельная карта Qwen3-ASR)",
    "qwen/qwen3-asr-1.7b": "yes (модельная карта Qwen3-ASR)",
    "nvidia/nemotron-3.5-asr-streaming-multilingual-0.6b": "?",
    "openai/whisper-large-v3": "yes (Whisper)",
    "mistralai/voxtral-mini-3b-2507": "?",
    "nvidia/parakeet-tdt-0.6b-v3": "yes (модельная карта: ru, FLEURS WER 5,51%)",
    "microsoft/mai-transcribe-2": "? (60 языков по описанию)",
    "x-ai/grok-stt-1.0": "?",
    "qwen/qwen3-asr-flash-2026-02-10": "yes (описание RouterAI)",
    "deepgram/nova-3": "?",
    "openai/gpt-transcribe": "?",
    "openai/whisper-1": "yes (Whisper)",
    "fish-audio/transcribe-1": "?",
    "google/chirp-3": "?",
}
TTS = {
    "fish-audio/s1": "yes (docs.fish.audio: Russian)",
    "fish-audio/s2-pro": "? (80+ языков)",
    "microsoft/mai-voice-2-flash": "? (15 языков)",
    "qwen/qwen-audio-3.0-tts-flash": "?",
    "x-ai/grok-voice-tts-1.0": "? (20+ языков)",
    "minimax/speech-2.8-turbo": "?",
    "hexgrad/kokoro-82m": "НЕТ (8 языков без русского, описание RouterAI)",
}


def rub(model, key):
    return float(CAT[model]["pricing"].get(key, 0) or 0)


def main():
    print("## ASR: ₽ за команду 4 с (округление вверх до секунды) и ₽/сутки")
    print(f"{'модель':52s} {'₽/с':>9s} {'₽/ч аудио':>9s} {'₽/команда':>9s} {'50 ком/сут':>10s} {'200 ком/сут':>11s} {'поток 8 ч/сут':>13s}  ru")
    for m, ru in sorted(ASR.items(), key=lambda kv: rub(kv[0], "seconds")):
        ps = rub(m, "seconds")
        per_cmd = math.ceil(CMD_S) * ps
        print(f"{m:52s} {ps:9.6f} {ps*3600:9.2f} {per_cmd:9.4f} {50*per_cmd:10.3f} {200*per_cmd:11.3f} {8*3600*ps:13.1f}  {ru}")
    print("  gpt-4o-mini-transcribe / gpt-4o-transcribe / gemini-3.5-transcribe тарифицируются по аудио-токенам — в таблицу не включены.")

    print(f"\n## TTS: ответ {ANSWER_CHARS} символов (≈{ANSWER_CHARS*BYTES_PER_CHAR_RU:.0f} байт UTF-8, ≈{ANSWER_CHARS/SPEECH_CPS:.1f} с речи)")
    print(f"{'модель':34s} {'₽/ед.':>10s} {'$/M ед.':>8s} {'₽/ответ (симв)':>14s} {'₽/ответ (байт)':>14s} {'200 отв/сут симв':>16s} {'байт':>8s}  ru")
    for m, ru in sorted(TTS.items(), key=lambda kv: rub(kv[0], "prompt")):
        p = rub(m, "prompt")
        a, b = ANSWER_CHARS * p, ANSWER_CHARS * BYTES_PER_CHAR_RU * p
        print(f"{m:34s} {p:10.7f} {p/USD*1e6:8.2f} {a:14.4f} {b:14.4f} {200*a:16.2f} {200*b:8.2f}  {ru}")

    print("\n## Трафик голосового цикла на одну команду (без HTTP/TLS-накладных)")
    pcm = 32000 * CMD_S
    opus_lo, opus_hi = 16000 / 8 * CMD_S, 20000 / 8 * CMD_S
    print(f"  вверх, PCM16 16 кГц: {pcm/1024:.0f} КиБ; в JSON base64: {pcm*4/3/1024:.0f} КиБ; Opus 16–20 кбит/с: {opus_lo/1024:.1f}–{opus_hi/1024:.1f} КиБ")
    dur = ANSWER_CHARS / SPEECH_CPS
    print(f"  вниз, TTS PCM 24 кГц 16 бит (формат Gemini TTS в docs RouterAI), {dur:.1f} с: {48000*dur/1024:.0f} КиБ; "
          f"Opus 20 кбит/с: {20000/8*dur/1024:.1f} КиБ")
    for n in (50, 200):
        print(f"  {n} команд/сут: PCM вверх+вниз ≈ {(pcm + 48000*dur)*n/1024/1024:.1f} МиБ/сут; Opus ≈ {(opus_hi + 20000/8*dur)*n/1024/1024:.2f} МиБ/сут")
    print(f"  поток без wake word, 8 ч/сут: PCM {32000*8*3600/1024/1024:.0f} МиБ/сут; Opus 16 кбит/с {16000/8*8*3600/1024/1024:.0f} МиБ/сут")


if __name__ == "__main__":
    main()
