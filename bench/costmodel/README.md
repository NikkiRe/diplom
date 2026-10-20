# costmodel — прогнозы затрат и ₽ по замороженному прайсу

Три части:

1. **Прайс** (`prices.py`) — обёртка над `research/new_topic/F_artifacts/price_table.py` (снимок
   `GET routerai.ru/api/v1/models` от 06.10.2026, ₽ за токен). Стоимость вызова считается функцией самого
   `price_table.call_cost_rub`: `(prompt − cached)·вход + cached·кэш + output·выход`. Файл исполняется без
   импорта, чтобы не писать `__pycache__` в `research/`. Метка снимка (путь, SHA-256, дата) попадает в
   `metrics.json` и в прогнозы.
   Надбавка за запись в кэш — по флагу `call_cost_rub(..., cache_write=True)`: у моделей OpenAI с полем
   `input_cache_write` (GPT-5.6+, GPT-6: 1,25× входа) некэшированные токены промпта от 1024 оплачиваются по
   цене записи (`cache_write_tokens`). По умолчанию флаг выключен, и прежние прогнозы и `metrics.json` не
   меняются. У Anthropic запись в кэш бывает только по явному `cache_control` (агент его не шлёт), у Google
   `input_cache_write` — цена хранения, поэтому для них надбавки нет.
2. **Модель политик истории** (`history_model.py`) — порт `research/new_topic/D_artifacts/cost_model.py`
   без изменения алгоритма: числа совпадают с исходником до токена (тест сверяет с исходником и с
   сохранёнными `out_R10/40/200.txt`). Нужна для H4(б): отношение токенов между политиками как функция числа
   запросов в сутки и TTL кэша.
3. **Прогнозы для предрегистрации** (`forecast.py`), в том числе цена рассуждения: `reasoning_table` —
   ход из двух вызовов (O1 §4) по моделям и числу скрытых reasoning-токенов R ∈ {0, 100, 400} на вызов, с
   надбавкой за запись. R = 400 — допущение концепции, реальные R даст проба E0-P.

Это прогнозы (воспроизведение), а не результаты: измерения T0/T2 сверяются с ними, отклонение отчитывается.

## Гипотеза H1 («когда будить»)

Вызовы LLM за окно: heartbeat — `N_hb + C·E[k]`, гейт — `N_gate + C·E[k]`, отношение

    ρ(C) = (N_hb + C·E[k]) / (N_gate + C·E[k]),   C* (ρ = r) = (N_hb − r·N_gate) / (E[k]·(r − 1)),

где C — команд в сутки (Bentley et al., 2018: L 2,61, M 5,55, M+ 10,1, H 30,44; S69/S200 — стресс), E[k] = 2,89
(VisionClaw: 1/2/3/5 вызовов с вероятностями 0,21/0,27/0,23/0,29), N_hb — тиков строго внутри окна (07–23 и
30 мин → 31; 14–22 → 15), N_gate ≈ 4. Получается ρ(M+) = 1,81, ρ(M) = 2,35, ρ(H) = 1,29, как в концепции и плане;
порог ρ = 1,28 (ширина ДИ E5-T2 при s = 0,2) достигается при C* ≈ 32 команд/сут.

Для сверки с T0: оракул mock-llm делает на вход с инструментом не меньше двух вызовов, поэтому
`expected_k_effective()` = E[max(k, 2)] = 3,10, и в прогнозах есть таблица `h1_t0_oracle_k_eff`.

## Запуск

```bash
.venv/bin/python -m bench.costmodel h1 [--period-min 30] [--gate-wakes 4] [--window 07:00-23:00] [--json]
.venv/bin/python -m bench.costmodel history --R 10          # таблица как у D_artifacts/cost_model.py
.venv/bin/python -m bench.costmodel prices [--model gpt-4o-mini]
.venv/bin/python -m bench.costmodel prereg --out prereg_forecasts.json
.venv/bin/python -m bench.costmodel reasoning [--no-cache-write] [--json]    # ₽ хода по моделям и R
```

`reasoning` по снимку 06.10.2026: gpt-5-mini — 0,079 ₽ за ход при R = 0, 0,123 при R = 100, 0,255 при R = 400
(доля выхода 74 %); переход с R = 400 на R = 0 — −69 %. gpt-6-luna с `none` — 0,036 ₽ с надбавкой за запись и
0,030 без неё. Это расчёт, не замер.

`prereg` собирает в один JSON: H1 для окон T0 и T2 (+ с E[k_eff]), чувствительность ρ к числу пробуждений
гейта и периоду heartbeat/keep-warm (0,9·TTL при TTL 300/600 с), долю фоновых вызовов у «каждое событие в LLM»
по уровням CASAS (активность 45, дверь 55, смена комнаты 390 в сутки), отношения токенов политик истории (TTL
600 с и без кэша), границы ₽ «кэша нет / весь префикс из кэша» для среднего вызова (иллюстративно до калибровки
E0-P), источники и метку прайса.

Библиотека:

```python
from bench.costmodel import PriceBook, h1_table, h1_ratio, prereg_forecasts
PriceBook.load().call_cost_rub("gpt-4o-mini", prompt_tokens=3000, cached_tokens=2048, output_tokens=150)
```

## Тесты

```bash
.venv/bin/python -m pytest bench/tests/test_costmodel.py bench/costmodel/tests -q
```
Цены снимка (§14 концепции), формула вызова, паритет порта с исходником (все политики × TTL × R × часы) и с
сохранёнными выводами, H1 на опорных значениях концепции, обращение точки безубыточности, границы ₽, JSON
предрегистрации. `bench/costmodel/tests/test_cache_write.py` — прежняя формула без флага, надбавка 1,25× у
моделей OpenAI с полем записи, её отсутствие у gpt-5-mini, у промпта короче 1024 и у Anthropic и Google, цена
хода по R против O1 §4.
