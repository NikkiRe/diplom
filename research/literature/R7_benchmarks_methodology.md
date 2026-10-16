# R7. Бенчмарки function calling / агентов для малых моделей и методология честных системных измерений

- Дата составления: 2026-10-05.
- Контекст: бакалаврская ВКР, ИТМО, P3431, направление 09.03.01 «Информатика и вычислительная техника». Тема руководителя: «Разработка ИИ-агентов для приложений граничного искусственного интеллекта (Edge AI)», узкая формулировка не утверждена. Уклон в ИБ исключён.
- Платформа: MacBook, Apple M4 Pro (8P+4E), 24 ГБ unified memory, macOS 27, llama.cpp build 11146 (brew), sudo нет.
- Как проверялись источники. Метаданные каждой работы (название, авторы, год, venue) сверены по первичной странице: arXiv abs/API, PMLR, ACL Anthology / DOI ACL, ACM DOI через Crossref и OpenAlex, страницы ICLR/NeurIPS/MLSys/IEEE. Числа взяты из PDF самих статей (текст извлекался `pdftotext`, нужные абзацы читались). dblp закрыт анти-бот защитой, поэтому вместо него использовались Crossref и OpenAlex. Статус «preprint» означает, что рецензируемую публикацию я не нашёл.
- Вспомогательные файлы: `research/literature/R7_artifacts/`. Там лежат пробы энергосчётчиков без sudo (C) и скрипт расчёта таблиц статистики.

---

## 0. Главное (TL;DR)

1. **Основной бенчмарк для ВКР — BFCL** (Patil et al., ICML 2025). Данные и код под Apache-2.0, данные в формате JSON Lines (несмотря на расширение `.json`), эталон хранится в `possible_answer/` как список допустимых значений для каждого параметра. Оценка детерминированная, через AST без LLM-судьи, поэтому всё считается офлайн на ноутбуке.
2. **Для моделей 0.5–8B осмысленны однократные (single-turn) AST-категории**: `simple`, `multiple`, `parallel`, `parallel_multiple`, `live_simple`, `live_multiple`. Плюс детекция нерелевантности: `irrelevance`, `live_irrelevance`. Multi-turn и agentic-категории (`miss_func`, `miss_param`, `long_context`, `web_search`, `memory`) у малых моделей лежат у пола: по таблице 1 статьи BFCL это 0–35 %, у моделей ≤3B почти 0. Различающей силы там нет. Категории `live_parallel` (16 примеров), `live_parallel_multiple` (24) и `live_relevance` (18) слишком малы: 95 % ДИ получается ±12–22 п.п.
3. **Режим вызова (FC или prompting) и шаблон чата влияют на результат не меньше, чем размер модели.** Llama-3.1-8B-Instruct набирает 21.0 в режиме FC и 39.6 в режиме Prompt (overall), а детекция нерелевантности в FC даёт всего 4.9 %. Для системной ВКР это довод в пользу собственного детерминированного стека: шаблон, грамматика и парсер должны быть зафиксированы и измерены.
4. **When2Call** (NAACL 2025) хорошо дополняет BFCL на малых моделях. Там 3 652 тестовых примера, решение «вызвать / переспросить / отказаться» оценивается через log-prob, LLM-судья не нужен. Llama 3.1 8B «галлюцинирует» вызов инструмента в 67 % случаев, когда инструментов нет.
5. **τ-bench, τ²-bench, ToolSandbox, ComplexFuncBench, ToolBench/ToolEval требуют LLM-симулятора пользователя или LLM-судьи (обычно GPT-4o), реальных API или контекста 128k.** Для офлайн-эксперимента на M4 Pro с моделями до 8B они полезны как источник идей и метрик, но не как основной бенчмарк. Метрику pass^k из τ-bench стоит позаимствовать.
6. **Методология системных измерений** опирается на работы Georges'07 → Mytkowicz'09 → Kalibera & Jones'13 → Curtsinger & Berger'13 → Hoefler & Belli'15 → MLPerf (Reddi'20). Из них прямо следуют:
   - иерархия повторов (процесс → итерация) и выбор числа повторов по дисперсии на каждом уровне;
   - отказ от «best of N»;
   - непараметрические ДИ, сравнение через ДИ отношения (effect size);
   - рандомизация порядка и условий;
   - прогон не короче 60 с, чтобы DVFS вышел на равновесие;
   - полная документация факторов.
7. **Статистика оценивания LLM.** Парные сравнения по вопросам (McNemar или парный bootstrap; Miller 2024, Dror 2018). Анализ мощности (Card 2020): на 200 примерах парный тест при 10 % дискордантных пар обнаруживает разницу примерно от 6 п.п. Проверка детерминизма: даже при greedy-декодировании результаты зависят от батча и железа (Atil 2024; Yuan, NeurIPS 2025).
8. **Энергию на Apple Silicon можно измерять без sudo тремя путями** (проверено на этой машине, §6):
   - (a) `proc_pid_rusage(RUSAGE_INFO_V6)` даёт per-process энергию CPU (`ri_energy_nj`), инструкции и циклы. Цикл на одном P-ядре: 3.31 Дж за 0.886 с ≈ 3.7 Вт, ≈4.3 ГГц;
   - (b) IOReport «Energy Model», тот же источник, что у `powermetrics` и `macmon`. Канал `GPU Energy` читается без root;
   - (c) телеметрия `AppleSmartBattery` (`PowerTelemetryData.SystemLoad`, `Voltage`, `InstantAmperage`) даёт мощность всей системы, согласованную с V·I.
   Всё это модельные или недокументированные счётчики. Их нужно валидировать: хотя бы один прогон с `powermetrics` от пользователя под sudo плюс проверки на правдоподобие.
9. **Практическая проблема прямо сейчас.** Во время работы над заметкой другой агент запускал `llama-batched-bench` (Qwen3-8B, Metal). GPU был загружен на 92 %, IOReport показывал ≈21–23 Вт на GPU, системная нагрузка от батареи ≈67 Вт. Ноутбук работал **от батареи (25 %)**. Любые замеры, снятые параллельно с другими агентами или от батареи, научно непригодны. Нужны проверка простоя (quiescence check) перед каждым прогоном и строго последовательный запуск бенчмарков.

---

## 1. Карта области: бенчмарки вызова функций и применимость к 0.5–8B на ноутбуке

| Бенчмарк | Что меряет | Размер (тест) | Оценка | Внешние зависимости | Лицензия | Годится для 0.5–8B офлайн? |
|---|---|---|---|---|---|---|
| BFCL v3/v4 | 1 вызов / несколько / параллельные; нерелевантность; multi-turn; agentic | single-turn non-live: 400+200+200+200, irrelevance 240; live: 258/1053/16/24, live_irrelevance 882, live_relevance 18; multi-turn 4×200 | AST-сопоставление; state+response для multi-turn | нет для AST-категорий; exec/REST/web_search требуют сети | Apache-2.0 | **Да, основной** |
| When2Call | вызывать / переспрашивать / отказываться | 3 652 | log-prob над 4 вариантами (+ LLM-судья на подвыборке) | нет | код Apache-2.0, данные CC-BY-4.0 | **Да, дополнение** |
| NESTFUL | вложенные цепочки, выход одного вызова идёт во вход другого | 1 800+ | full-sequence match, win-rate (исполнение) | исполнимые функции локально | Apache-2.0 | Да, «трудная» часть |
| API-Bank | планирование / поиск / вызов API в диалоге | 314 диалогов, 753 вызова | исполнение 73 API | локальный сервер API | MIT (репозиторий DAMO-ConvAI) | Частично, устарел |
| Seal-Tools | много инструментов, вложенные вызовы | 4 076 инструментов | строгий формат + 3 метрики | нет | Apache-2.0 | Да, как доп. набор |
| ToolBench / ToolEval | реальные RapidAPI | 16 464 API | pass/win rate через ChatGPT-судью | RapidAPI, LLM-судья | Apache-2.0 | Нет (нестабильно; см. StableToolBench) |
| τ-bench / τ²-bench | агент + симулятор пользователя + политика домена | retail/airline (+telecom) | сравнение состояния БД; pass^k | LLM-симулятор пользователя | MIT | Скорее нет (нужен сильный симулятор) |
| ToolSandbox | состояние, неявные зависимости, симулятор пользователя | 1 032 сценария | milestones / minefields | GPT-4o-симулятор | Apple custom (не OSI) | Нет |
| ComplexFuncBench | многошаговые вызовы с ограничениями, длинный контекст | 1 000 | ComplexEval (в т.ч. LLM-сопоставление) | 128k контекст, GPT-4o | данные Apache-2.0 (HF) | Нет |
| JSONSchemaBench | структурированный вывод, constrained decoding | 10K схем | coverage / compliance / GCT / TTFT / TPOT | нет | MIT (HF) | **Да, для системного угла** |

---

## 2. Часть A. Бенчмарки function calling и малые модели

### A1. Gorilla / APIBench — предыстория AST-оценки
**Shishir G. Patil, Tianjun Zhang, Xin Wang, Joseph E. Gonzalez.** *Gorilla: Large Language Model Connected with Massive APIs.* Advances in Neural Information Processing Systems 37 (NeurIPS 2024), pp. 126544–126565. DOI 10.52202/079017-4020. arXiv:2305.15334. Проверено: arXiv + Crossref.
- **Проблема.** LLM путаются в аргументах и «галлюцинируют» несуществующие API.
- **Метод.** Набор APIBench: TorchHub (94 вызова), TensorHub (696), HuggingFace (925), всего ≈16 450 пар {инструкция, API}. Дообучение LLaMA с retriever'ом. Корректность и галлюцинации оцениваются **сопоставлением поддеревьев AST**.
- **Польза для ВКР.** Отсюда идёт идея AST-оценки, которую BFCL масштабировал. Это историческое обоснование детерминированной оценки без исполнения.
- **Что взять.** Определение «галлюцинации вызова» как вызова, отсутствующего в каталоге.

### A2. BFCL — Berkeley Function Calling Leaderboard (основной бенчмарк)
**Shishir G. Patil, Huanzhi Mao, Fanjia Yan, Charlie Cheng-Jie Ji, Vishnu Suresh, Ion Stoica, Joseph E. Gonzalez.** *The Berkeley Function Calling Leaderboard (BFCL): From Tool Use to Agentic Evaluation of Large Language Models.* Proceedings of the 42nd ICML, PMLR 267:48371–48392, 2025. https://proceedings.mlr.press/v267/patil25a.html ; OpenReview id 2GmDdhBdDk. Проверено: PMLR + OpenReview + ICML virtual.
- **Проблема.** Не было стандарта оценки вызова функций, охватывающего serial/parallel, разные языки, нерелевантность и многошаговость.
- **Метод.**
  - Экспертные функции (single-turn) и краудсорсинговые «live» запросы: 64 517 реальных запросов, собранных 26.02–01.04.2024, дедуплицированы по ROUGE-L и эмбеддингам.
  - Multi-turn датасеты (base, miss func, miss param, long context) и agentic (web search, memory, SQL; в v4 добавлен format sensitivity).
  - Оценка: **AST-сопоставление** (имя функции, типы, значения из списка допустимых), исполнимые тесты для части данных, **state-based + response-based** проверки для multi-turn.
  - Авторы показывают сильную корреляцию между AST- и exec-оценками.
- **Ключевые результаты** (таблица 1 статьи, срез начала 2025 г.). Overall: GPT-4o-2024-11-20 (Prompt) 66.4. Малые модели (overall / AST simple / irrelevance / multi-turn base):

  | Модель (режим) | Overall | AST Simple | Irrelevance | MT Base |
  |---|---:|---:|---:|---:|
  | ToolACE-2-8B (FC) | 56.6 | 75.3 | 90.1 | 48.5 |
  | Hammer2.1-7b (FC) | 48.6 | 78.1 | 78.6 | 35.5 |
  | Qwen2.5-7B-Instruct (FC) | 44.7 | 71.8 | 69.1 | 13.5 |
  | Llama-3.1-8B-Instruct (Prompt) | 39.6 | 72.8 | 48.8 | 13.0 |
  | Llama-3.1-8B-Instruct (FC) | 21.0 | 55.8 | 4.9 | 5.0 |
  | Hammer2.1-3b (FC) | 45.0 | 81.4 | 81.9 | 27.5 |
  | Qwen2.5-3B-Instruct (FC) | 38.7 | 73.3 | 64.3 | 8.5 |
  | Qwen2.5-1.5B-Instruct (FC) | 36.6 | 72.4 | 62.7 | 4.0 |
  | Hammer2.1-0.5b (FC) | 34.0 | 68.0 | 73.9 | 4.0 |
  | Qwen2.5-0.5B-Instruct (FC) | 27.1 | 61.2 | 46.2 | 1.0 |
  | Llama-3.2-1B-Instruct (Prompt) | 15.4 | 29.2 | 59.7 | 0.0 |

  В режиме prompting ошибок декодирования примерно втрое больше, чем в FC: 412.93 против 182.5 из 4 251 записи (среднее по моделям). Зато FC-модели чаще ошибаются с числом вызовов в категории multiple.
- **Данные и лицензия (проверено вручную).**
  - HF `gorilla-llm/Berkeley-Function-Calling-Leaderboard`, `license: apache-2.0`. Репозиторий `ShishirPatil/gorilla` тоже Apache-2.0. Пакет `pip install bfcl-eval`; есть одноимённый посторонний пакет `bfcl`, его ставить не надо.
  - Формат: по одному JSON-объекту на строку. Вход: `{"id", "question": [[{"role","content"}]], "function": [JSON-schema-подобные описания]}`. Эталон: `possible_answer/*.json` вида `{"id":"simple_0","ground_truth":[{"calculate_triangle_area":{"base":[10],"height":[5],"unit":["units",""]}}]}`. Пустая строка `""` в списке значит, что необязательный параметр можно опустить.
  - Фактические размеры v3 на HF (посчитаны построчно): simple 400, multiple 200, parallel 200, parallel_multiple 200, irrelevance 240, live_simple 258, live_multiple 1053, live_parallel 16, live_parallel_multiple 24, live_irrelevance 882, live_relevance 18, multi_turn_base 200.
  - В карточке HF для live указаны 1037/875/41 (V2). Версии расходятся, поэтому **фиксируйте коммит данных и версию `bfcl-eval`**.
  - Для локальной модели в README есть бэкенды vLLM/SGLang (на Mac не подходят) и OpenAI-совместимый endpoint. Значит, можно подключить `llama-server`. Шаблон чата и FC-парсер нужно проверить отдельно.
- **Польза для ВКР.** Готовый, лицензионно чистый, детерминированно оцениваемый корпус с опубликованными baseline для моделей 0.5–8B.
- **Что взять.**
  - AST-чекер как независимый оракул корректности.
  - Категории для малых моделей (§7).
  - Опубликованные числа как внешние baseline. Сравнивать с ними можно только при совпадении режима и промпта, иначе только качественно.

### A3. API-Bank
**Minghao Li, Yingxiu Zhao, Bowen Yu, Feifan Song, Hangyu Li, Haiyang Yu, Zhoujun Li, Fei Huang, Yongbin Li.** *API-Bank: A Comprehensive Benchmark for Tool-Augmented LLMs.* EMNLP 2023 (main). DOI 10.18653/v1/2023.emnlp-main.187. arXiv:2304.08244. Проверено: arXiv + OpenAlex/ACL DOI.
- **Метод.** Исполнимая система из 73 API. 314 размеченных диалогов с 753 вызовами; уровни: call / retrieve+call / plan+retrieve+call. Обучающий набор: 1 888 диалогов, 2 138 API, 1 000 доменов. Модель Lynx (на базе Alpaca).
- **Результаты.** Lynx превосходит Alpaca по использованию инструментов более чем на 26 пунктов и приближается к GPT-3.5. GPT-4 сильнее всех в планировании.
- **Польза и что взять.** Таксономия уровней и классификация ошибок (неверное имя API, аргументы, «ложный» вызов). Как основной бенчмарк устарел: модели 2023 г., малый тест.

### A4. ToolLLM / ToolBench / ToolEval (+ StableToolBench)
**Yujia Qin, Shihao Liang, Yining Ye, …, Zhiyuan Liu, Maosong Sun** (19 авторов). *ToolLLM: Facilitating Large Language Models to Master 16000+ Real-world APIs.* ICLR 2024 (spotlight). arXiv:2307.16789; OpenReview dHng200Jjr. Проверено: arXiv + ICLR virtual.
- **Метод.** 16 464 REST API из 49 категорий RapidAPI. Инструкции и траектории сгенерированы ChatGPT. Поиск решения через DFSDT (дерево решений с поиском в глубину). ToolEval считает pass rate и win rate с ChatGPT в роли судьи. Модель ToolLLaMA с нейро-retriever'ом API.
- **Результат.** ToolLLaMA сопоставима с ChatGPT и обобщается на APIBench.
- **Ограничение (важно для методологии).** Реальные RapidAPI нестабильны, а судья является LLM. Отдельная работа **StableToolBench** (Guo et al., Findings of ACL 2024, DOI 10.18653/v1/2024.findings-acl.664, arXiv:2403.07714; проверено по arXiv и OpenAlex) вводит кэш и симулятор API ради стабильности оценки. BFCL тоже ссылается на высокую дисперсию RapidAPI.
- **Польза.** Отрицательный пример для ВКР: оценка через внешние API и LLM-судью невоспроизводима на ноутбуке. Идея Tool retriever пригодится в угле 2.

### A5. τ-bench и τ²-bench
**Shunyu Yao, Noah Shinn, Pedram Razavi, Karthik Narasimhan.** *τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains.* ICLR 2025. arXiv:2406.12045. Проверено: arXiv + proceedings.iclr.cc. Код MIT (`sierra-research/tau-bench`).
- **Метод.** Динамический диалог агента и симулированного LLM пользователя в доменах retail и airline. У агента есть API и документ политики. Корректность определяется сравнением **итогового состояния БД** с целевым. Новая метрика **pass^k**: вероятность успеха во всех k испытаниях.
- **Результаты.** GPT-4o решает <50 % задач; pass^8 <25 % в retail.
- **Что взять.** **pass^k** для оценки надёжности агента при стохастическом декодировании на edge. Оценку по итоговому состоянию тоже стоит заимствовать: она не чувствительна к перефразированию.

**Victor Barres, Honghua Dong, Soham Ray, Xujie Si, Karthik Narasimhan.** *τ²-Bench: Evaluating Conversational Agents in a Dual-Control Environment.* arXiv:2506.07982, 2025 (**preprint**). Код MIT.
- Домен telecom моделируется как Dec-POMDP, где и пользователь, и агент действуют инструментами. Есть композиционный генератор задач. Результат: заметное падение, когда агент переходит от режима «без пользователя» к dual-control.
- **Вывод для ВКР.** Оба бенчмарка требуют сильного симулятора пользователя (облачная LLM). Это противоречит офлайн-постановке. Кроме того, ABC-чеклист (C8) нашёл в τ-bench дефект: пустые ответы засчитывались как успех.

### A6. When2Call
**Hayley Ross, Ameya Sunil Mahabaleshwarkar, Yoshi Suhara.** *When2Call: When (not) to Call Tools.* NAACL 2025 (long). DOI 10.18653/v1/2025.naacl-long.174. arXiv:2504.18851. Проверено: arXiv + ACL DOI. Код Apache-2.0, данные CC-BY-4.0 (HF `nvidia/When2Call`).
- **Проблема.** Бенчмарки проверяют правильность вызова, но не решение «вызывать ли вообще».
- **Метод.** Множественный выбор из вызова инструмента, уточняющего вопроса, ответа «не могу» и прямого ответа. Основная оценка через **log-probability** вариантов, LLM-судья только на подвыборке из 300. Тест: 3 652 примера (1 295 / 1 062 / 1 295 по типам правильного ответа). Метрики: macro-F1, length-normalized accuracy, **tool hallucination rate** (вызов при отсутствии инструментов).
- **Результаты** (таблица 3):

  | Модель | F1 | Tool hallucination |
  |---|---:|---:|
  | Llama 3.2 3B | 17.9 | 52 % |
  | Llama 3.1 8B | 16.6 | 67 % |
  | Qwen 2.5 3B | 29.8 | 23 % |
  | Qwen 2.5 7B | 32.0 | 21 % |
  | xLAM 7B FC-R | 31.5 | 24 % |
  | MNM 8B + When2Call-RPO | 52.4 | 1.2 % |

  Качество не растёт монотонно с размером: у Qwen 2.5 3B/7B/72B F1 почти одинаковый.
- **Польза.** Оценка через log-prob дешёвая и детерминированная, на llama.cpp её можно считать через `llama_get_logits_ith`. Для edge-агента ложный вызов стоит дорого: это лишний prefill, действие и энергия.

### A7. NESTFUL
**Kinjal Basu, Ibrahim Abdelaziz, Kiran Kate, Mayank Agarwal, Maxwell Crouse, Yara Rizk, Kelsey Bradford, Asim Munawar, Sadhana Kumaravel, Saurabh Goyal, Xin Wang, Luis A. Lastras, Pavan Kapanipathi.** *NESTFUL: A Benchmark for Evaluating LLMs on Nested Sequences of API Calls.* EMNLP 2025 (main). DOI 10.18653/v1/2025.emnlp-main.1702. arXiv:2409.03797. Проверено: arXiv + OpenAlex (EMNLP DOI). Apache-2.0.
- **Метод.** Более 1 800 вложенных последовательностей: выход одного вызова передаётся во вход следующего, все функции исполнимы.
- **Результаты.** Лучшая модель (GPT-4o) даёт full sequence match 28 % и win-rate 60 %.
- **Что взять.** Метрики partial/full sequence match. Набор задач на зависимости по данным подходит для угла с DAG-планированием вызовов (связь с LLMCompiler, см. R2).

### A8. ToolSandbox
**Jiarui Lu, Thomas Holleis, Yizhe Zhang, Bernhard Aumayer, Feng Nan, Felix Bai, Shuang Ma, Shen Ma, Mengyu Li, Guoli Yin, Zirui Wang, Ruoming Pang.** *ToolSandbox: A Stateful, Conversational, Interactive Evaluation Benchmark for LLM Tool Use Capabilities.* Findings of NAACL 2025. DOI 10.18653/v1/2025.findings-naacl.65. arXiv:2408.04682. Проверено: arXiv + OpenAlex. Лицензия: собственная лицензия Apple (не OSI).
- **Метод.** Инструменты с состоянием и неявными зависимостями (например, нельзя отправить SMS при выключенной сотовой связи). Симулятор пользователя на GPT-4o. Оценка по **milestones / minefields** (DAG ключевых событий) по произвольной траектории. 1 032 сценария.
- **Результаты** (средняя similarity): GPT-4o 73.0, Claude-3-Opus 69.2, Hermes-2-Pro-Mistral-7B 31.4, Mistral-7B-Instruct-v0.3 29.8, Gorilla-OpenFunctions-v2 25.6.
- **Польза.** Идея milestones/minefields подходит для собственного мини-набора задач «edge-ассистента». Сам бенчмарк требует облачного симулятора, а лицензия ограничена.

### A9. Seal-Tools
**Mengsong Wu, Tong Zhu, Han Han, Chuanyuan Tan, Xiang Zhang, Wenliang Chen.** *Seal-Tools: Self-instruct Tool Learning Dataset for Agent Tuning and Detailed Benchmark.* NLPCC 2024, LNCS (Springer). DOI 10.1007/978-981-97-9434-8_29. arXiv:2405.08355. Проверено: arXiv + OpenAlex. Apache-2.0.
- **Метод.** 4 076 сгенерированных (self-instruct) инструментов, сложные экземпляры с несколькими и вложенными вызовами. Строгий контроль формата и три метрики: формат, выбор инструмента, параметры.
- **Польза.** Доп. набор с большим каталогом инструментов. Подходит для эксперимента «сколько инструментов влезает в контекст и сколько стоит prefill каталога».

### A10. ComplexFuncBench
**Lucen Zhong, Zhengxiao Du, Xiaohan Zhang, Haiyi Hu, Jie Tang.** *ComplexFuncBench: Exploring Multi-Step and Constrained Function Calling under Long-Context Scenario.* arXiv:2501.10132, 2025 (**preprint**, рецензируемую публикацию не нашёл). Данные Apache-2.0 (HF `zai-org/ComplexFuncBench`).
- **Метод.** 1 000 размеченных примеров, 43 реальных API Booking.com в пяти сценариях. Многошаговые вызовы с ограничениями и длинными параметрами, контекст до 128k. Оценка ComplexEval: правила + эмбеддинги + LLM-сопоставление на GPT-4o. Метрики: Success Rate, Call Accuracy.
- **Результаты.** Лучшие закрытые модели: 61.0 / 60.5 / 57.1 % success. Qwen2.5-72B: 40.1 %. Llama 3.1 заметно хуже.
- **Вывод.** Для ноутбука с моделями ≤8B не подходит: нужен контекст 128k, судья GPT-4o и реальные API.

### A11. TinyAgent (прямой аналог edge-агента на MacBook)
**Lutfi Eren Erdogan, Nicholas Lee, Siddharth Jha, Sehoon Kim, Ryan Tabrizi, Suhong Moon, Coleman Hooper, Gopala Anumanchipalli, Kurt Keutzer, Amir Gholami.** *TinyAgent: Function Calling at the Edge.* EMNLP 2024: System Demonstrations. DOI 10.18653/v1/2024.emnlp-demo.9. arXiv:2409.00608. Проверено: arXiv + OpenAlex. Код MIT.
- **Метод.** Планировщик в стиле LLMCompiler (DAG вызовов) для локального Siri-подобного ассистента на MacBook. Дообучение TinyAgent-1.1B и 7B. **Tool RAG**: классификатор DeBERTa-v3-small отбирает ≈3.97 инструмента на запрос. INT4-квантование.
- **Результаты.**
  - Success rate: 1.1B 80.06 %, 7B 84.95 %, GPT-4-Turbo 79.08 %.
  - Tool RAG сокращает промпт с 2 762 до 1 397 токенов и повышает точность.
  - Квантование 4 бита: латентность 1.1B 3.9 → 2.9 с, 7B 19.5 → 13.1 с при неизменной точности (80.35 / 85.14 %).
- **Польза.** Сильный baseline и доказательство, что prefill каталога доминирует в стоимости. Методологически слабое место: латентность дана одним числом без ДИ, условий и энергии. Это ниша для ВКР.

### A12. Hammer
**Qiqiang Lin, Muning Wen, Qiuying Peng, Guanyu Nie, Junwei Liao, Jun Wang, Xiaoyun Mo, Jiamu Zhou, Cheng Cheng, Yin Zhao, Jun Wang, Weinan Zhang.** *Hammer: Robust Function-Calling for On-Device Language Models via Function Masking.* ICLR 2025 (spotlight). arXiv:2410.04587; OpenReview yVQcr4qjD6. Проверено: arXiv + ICLR virtual. Код Apache-2.0; веса Hammer2.1 под **CC-BY-NC-4.0**; датасет нерелевантности `MadeAgents/xlam-irrelevance-7.5k` под CC-BY-4.0.
- **Проблема.** Качество FC-моделей сильно скачет между бенчмарками из-за ориентации на имена функций и параметров.
- **Метод.** «Маскирование» имён (модель учится по описаниям) и +7 500 примеров на нерелевантность поверх xLAM-60k.
- **Результаты.** Модели Hammer 0.5–7B стоят на верху BFCL в своём классе (см. таблицу A2).
- **Польза.** Готовые веса малых FC-моделей для экспериментов. Некоммерческая лицензия для ВКР допустима. Результаты подтверждают, что чувствительность к именам является источником систематической ошибки бенчмарка.

### A13. Less-is-More (DATE 2025) — выбор инструментов на edge с замером мощности
**Varatheepan Paramanayakam, Andreas Karatzas, Iraklis Anagnostopoulos, Dimitrios Stamoulis.** *Less is More: Optimizing Function Calling for LLM Execution on Edge Devices.* DATE 2025. DOI 10.23919/DATE64628.2025.10992798. arXiv:2411.15399. Проверено: arXiv + OpenAlex.
- **Метод.** Динамический отбор инструментов без дообучения. LLM сама описывает «идеальные» инструменты, recommender ищет ближайшие. Опыты: Llama3.1-8B Q4_K_M на Jetson AGX Orin, бенчмарки BFCL и GeoEngine (46 инструментов на запрос).
- **Результаты.** Время выполнения снижается до 70 %, мощность до 40 %. Пример: при 46 инструментах модель выбирает неверный, при 19 справляется.
- **Польза.** Это прямой baseline для угла 2. Edge-работа, которая меряет и точность, и время, и мощность. Её статистика (одиночные замеры) слабая, это можно улучшить.
- Родственная работа: CarbonCall (arXiv:2504.20348, preprint) тех же авторов.

### A14. Small Models, Big Tasks (EASE 2025)
**Ishan Kavathekar, Raghav Donakanti, Ponnurangam Kumaraguru, Karthik Vaidhyanathan.** *Small Models, Big Tasks: An Exploratory Empirical Study on Small Language Models for Function Calling.* EASE 2025 (AI Models and Data Evaluation track; venue по комментарию arXiv). arXiv:2504.19277. Проверено: arXiv.
- **Метод.** SLM на датасете xLAM function calling 60k в режимах zero-shot, few-shot и fine-tune, включая prompt injection. Edge-опыты: GGUF Q4_K_M на Snapdragon-устройстве, латентность и память.
- **Результаты (качественно).** Fine-tune лучше всего. **Главный провал SLM — несоблюдение формата вывода.** На edge латентность заметно выше.
- **Польза.** Подтверждает, что ограниченное декодирование (грамматика) является системным рычагом качества для малых моделей (угол 3).

### A15. JSONSchemaBench — системная стоимость структурированного вывода
**Saibo Geng, Hudson Cooper, Michał Moskal, Samuel Jenkins, Julian Berman, Nathan Ranchin, Robert West, Eric Horvitz, Harsha Nori.** *JSONSchemaBench: A Rigorous Benchmark of Structured Outputs for Language Models.* arXiv:2501.10868, 2025 (**preprint**). Данные MIT (HF `epfl-dlab/JSONSchemaBench`).
- **Метод.** 10K реальных JSON Schema и официальный JSON Schema Test Suite. Метрики: эффективность (Grammar Compilation Time, TTFT, TPOT), declared/empirical coverage, compliance, качество. Фреймворки: Guidance, Outlines, llama.cpp, XGrammar, OpenAI, Gemini.
- **Результаты** (медианы, Llama-3.1-8B на бэкенде llama.cpp):
  - **TPOT llama.cpp-грамматики ≈27–30 мс против ≈15–16 мс без ограничений**, то есть замедление около 2×;
  - Guidance даёт 6–9 мс за счёт fast-forward вынужденных токенов;
  - Outlines тратит на компиляцию 3.5–8 с.
- **Польза.** Прямо мотивирует low-level угол 3: накладные расходы грамматики в llama.cpp и ускорение jump-forward для схем вызовов функций. Методологический приём: эффективность считается только на схемах, которые покрывают все движки, иначе возникает coverage bias. Подробнее про constrained decoding см. R3.

---

## 3. Часть B. Методология системных измерений (классика + ML-бенчмарки)

### B1. Georges, Buytaert, Eeckhout — статистически строгая оценка производительности
**Andy Georges, Dries Buytaert, Lieven Eeckhout.** *Statistically Rigorous Java Performance Evaluation.* OOPSLA 2007, pp. 57–76. DOI 10.1145/1297027.1297033. Проверено: Crossref + OpenAlex + PDF.
- **Проблема.** Распространённые практики (best of N, среднее без ДИ, одна VM-инвокация) ведут к неверным выводам.
- **Метод.**
  - ДИ для среднего: t-распределение при n<30.
  - ANOVA + Tukey HSD для сравнения нескольких альтернатив.
  - Протокол для start-up: много инвокаций VM по одной итерации.
  - Протокол для steady-state: много инвокаций, в каждой итерации после разогрева. Разогрев считается законченным, когда CoV окна из k итераций ниже порога 0.01–0.02.
  - Инструмент JavaStats подбирает число измерений под заданную ширину ДИ.
- **Результаты.** Распространённые методики дают **до 16 % «вводящих в заблуждение»** сравнений и **>3 % прямо неверных** (сравнения с порогом θ=1–3 %). Пример «best of 30» против «mean ± 95 % ДИ» для сборщиков мусора меняет вывод.
- **Для ВКР.** Прямой перенос на LLM-инференс. «Start-up» соответствует холодной загрузке модели, первому вызову и прогреву Metal-шейдеров. «Steady-state» соответствует серии запросов к загруженной модели. Запрет на «best of N».

### B2. Mytkowicz, Diwan, Hauswirth, Sweeney — смещение измерения
**Todd Mytkowicz, Amer Diwan, Matthias Hauswirth, Peter F. Sweeney.** *Producing Wrong Data Without Doing Anything Obviously Wrong!* ASPLOS 2009, pp. 265–276. DOI 10.1145/1508244.1508275. Проверено: Crossref + PDF.
- **Проблема.** Measurement bias: безобидный фактор установки систематически меняет вывод.
- **Метод.** Факторы: размер окружения UNIX (сдвигает стек) и порядок линковки. Лечение: **рандомизация установки** и каузальный анализ.
- **Результаты.**
  - Изменение размера неиспользуемой переменной окружения меняет время «часто примерно на 33 %, однажды почти на 300 %».
  - Ускорение O3 относительно O2 для perlbench в зависимости от порядка линковки лежит между 0.92 и 1.10: от 7 % замедления до 8 % ускорения.
  - Ни одна из 133 статей ASPLOS/PACT/PLDI/CGO не учла это смещение должным образом.
- **Для ВКР.**
  - Рандомизировать порядок конфигураций и путь/окружение запуска.
  - Пересобирать бинарники llama.cpp с разным порядком при сравнении своих C++-оптимизаций.
  - Не доверять разнице в единицы процентов без такой рандомизации.

### B3. Kalibera & Jones — строгий бенчмаркинг за разумное время
**Tomas Kalibera, Richard Jones.** *Rigorous Benchmarking in Reasonable Time.* ISMM 2013, pp. 63–74. DOI 10.1145/2464157.2464160. Проверено: Crossref + PDF (Kent Academic Repository).
- **Проблема.** Из 122 статей 2011 г. (PLDI, ASPLOS, ISMM, TOPLAS, TACO) 90 измеряют время, и **71 из них не приводят никакой меры вариативности**. 65 из 90 сообщают ускорения.
- **Метод.**
  - Иерархическая модель случайных эффектов: сборка → запуск (execution) → итерация.
  - Независимость итераций проверяется визуально: run-sequence, lag-plot, ACF.
  - Оптимальное число повторов на каждом уровне выводится из доли дисперсии этого уровня и стоимости повтора.
  - Результат сообщается как **ДИ размера эффекта, то есть отношения времён**. Используется интервал Филлера или bootstrap: «A быстрее B на 5.5 % ± 2.5 %».
- **Для ВКР.** Это готовый рецепт планирования опыта. Пилот оценивает дисперсию между запусками `llama-server`/процесса и между запросами внутри запуска, по ним выбирается число повторов. Итоговые сравнения даются как отношение с ДИ.

### B4. Curtsinger & Berger — STABILIZER
**Charlie Curtsinger, Emery D. Berger.** *STABILIZER: Statistically Sound Performance Evaluation.* ASPLOS 2013, pp. 219–228. DOI 10.1145/2451116.2451141. Проверено: Crossref + PDF.
- **Метод.** Во время исполнения многократно рандомизируется размещение кода, стека и кучи. Один бинарник перестаёт быть «одной выборкой из пространства раскладок», а распределение времени становится близким к нормальному (проверено тестом Шапиро–Уилка).
- **Результаты.** Медианные накладные расходы 6.7 % (<7 %). На SPEC CPU2006 с LLVM эффект −O3 относительно −O2 **неотличим от шума**, а −O2 относительно −O1 значим. Смена порядка линковки ухудшает производительность до 57 %.
- **Для ВКР.** Не переносить инструмент (он под LLVM 3.x и x86), а перенять принцип: результат собственной C++-оптимизации (ядро, планировщик, парсер) проверять на нескольких раскладках или сборках.

### B5. Hoefler & Belli — 12 правил научного бенчмаркинга
**Torsten Hoefler, Roberto Belli.** *Scientific Benchmarking of Parallel Computing Systems: Twelve Ways to Tell the Masses When Reporting Performance Results.* SC 2015, pp. 1–12. DOI 10.1145/2807591.2807644. Проверено: Crossref + PDF.
- **Проблема.** Стратифицированная выборка из 120 статей трёх ведущих HPC-конференций: только 15 из 95 упоминают хоть какую-то меру разброса, и лишь 2 из 95 приводят ДИ.
- **Правила, которые прямо используются в ВКР:**
  - R3: арифметическое среднее только для затрат, гармоническое для скоростей.
  - R4: не усреднять отношения; если всё же нужно, то геометрическим средним.
  - R5: сообщать, детерминированы ли данные, а для недетерминированных давать ДИ.
  - R6: не предполагать нормальность без проверки (Q-Q plot), иначе брать непараметрические ДИ медианы по рангам.
  - R7: сравнивать статистически, через непересекающиеся ДИ, ANOVA или Kruskal–Wallis.
  - R8: для хвостов использовать перцентили и квантильную регрессию.
  - R9: документировать все факторы и полную установку.
  - R11: показывать верхние границы производительности (roofline/bandwidth bound).
  - R12: рисовать распределения.
  - Библиотека LibSciBench.
- **Для ВКР.** Чек-лист раздела «Методика эксперимента». R11 особенно полезно: decode на M4 Pro ограничен пропускной способностью памяти, поэтому измеренные ток/с нужно сравнить с потолком «пропускная способность / размер весов» (см. R4).

### B6. MLPerf Inference
**Vijay Janapa Reddi, Christine Cheng, David Kanter, Peter Mattson, Guenther Schmuelling, Carole-Jean Wu, et al.** (47 авторов). *MLPerf Inference Benchmark.* ISCA 2020, pp. 446–459. DOI 10.1109/ISCA45697.2020.00045. arXiv:1911.02549. Проверено: arXiv + OpenAlex.
- **Метод.**
  - Четыре сценария: single-stream, multi-stream, server, offline.
  - Генератор нагрузки **LoadGen**, отдельный режим точности.
  - Метрика single-stream: **90-й перцентиль латентности**.
  - Статистически обоснованное число запросов: NumQueries = Φ⁻¹(1−(1−conf)/2)² · p(1−p)/margin², где margin = (1−p)/20 и conf = 99 %.
- **Результаты и правила.**
  - p99 требует 262 742 запроса (округляется до 270K), p97 требует 90K. Single-stream: 1 024 запроса.
  - **Минимум 60 с на прогон**, чтобы измерить равновесное поведение управления питанием и DVFS.
  - В server берётся минимум из 5 прогонов.
  - Первый раунд: более 600 измерений от 14 организаций.
- **Для ВКР.**
  - Терминология сценариев: агент на ноутбуке — это single-stream.
  - Формула объёма выборки: я пересчитал, для p90 при тех же условиях нужно ≈23 900 запросов (скрипт в `R7_artifacts`). Отсюда вывод: для p90 латентности агентного запроса на ноутбуке разумнее bootstrap-ДИ квантиля, чем требования MLPerf.
  - Правило минимальной длительности 60 с.

### B7. MLPerf Tiny
**Colby Banbury, Vijay Janapa Reddi, Peter Torelli, Jeremy Holleman, Nat Jeffries, Csaba Kiraly, Pietro Montino, David Kanter, Pete Warden, Danilo Pau, Urmish Thakker, et al.** *MLPerf Tiny Benchmark.* NeurIPS 2021 Datasets and Benchmarks Track. arXiv:2106.07597. Проверено: arXiv + neurips.cc + proceedings D&B.
- **Метод.** Четыре задачи (keyword spotting, visual wake words, image classification, anomaly detection), измерения точности, латентности и **энергии**, эталонные реализации. Код Apache-2.0 (`mlcommons/tiny`).
- **Для ВКР.** Пример триады «качество / латентность / энергия» как обязательного набора метрик edge-бенчмарка. Энергия там мерится внешней аппаратурой; на ноутбуке её заменяют программные счётчики (§6), и это ограничение надо честно указать.

### B8. MLPerf Mobile
**Vijay Janapa Reddi, David Kanter, Peter Mattson, Jared Duke, Thai Nguyen, Ramesh Chukka, et al.** *MLPerf Mobile Inference Benchmark: An Industry-Standard Open-Source Machine Learning Benchmark for On-Device AI.* Proceedings of MLSys 2022 (на arXiv:2012.02328 под названием «…Why Mobile AI Benchmarking Is Hard and What to Do About It»). Проверено: arXiv + proceedings.mlsys.org. Код Apache-2.0.
- **Метод.** Приложение для Android/iOS, четыре модели (3 CV + 1 NLP). Правила учитывают слои SDK и вендорских делегатов.
- **Результат.** За 6 месяцев offline throughput вырос в 3 раза, латентность упала до 12 раз. Вклад стека ПО сопоставим с вкладом «железа».
- **Для ВКР.** Аргумент для системной ВКР: результат on-device определяется связкой «железо + фреймворк + настройки». Поэтому версии и флаги llama.cpp являются факторами эксперимента.

### B9. MLPerf Client (документация, не статья)
MLCommons, MLPerf Client v2.0, https://mlcommons.org/benchmarks/client/ ; код `mlcommons/mlperf_client` (Apache-2.0). Проверено: страница MLCommons + GitHub.
- Метрики LLM: **TTFT** и **TPS** (скорость после первого токена). Модели Llama 3.1 8B Instruct и Phi 4 Mini; экспериментальные: Qwen3 8B и др. Задачи: content generation, creative writing, summarization, code analysis и др. Поддерживает macOS через llama.cpp (Metal) и MLX.
- **Для ВКР.** Индустриальный эталон «клиентского» LLM-бенчмарка на той же платформе. Можно сопоставить свои TTFT/TPS при той же модели (Qwen3-8B). Статьи нет, поэтому цитируется как документация.

### B10. MELT — измерение LLM на мобильных и edge-устройствах
**Stefanos Laskaridis, Kleomenis Katevas, Lorenzo Minto, Hamed Haddadi.** *MELTing Point: Mobile Evaluation of Language Transformers.* MobiCom 2024. DOI 10.1145/3636534.3690668. arXiv:2403.12844. Проверено: arXiv + OpenAlex.
- **Метод.** Инфраструктура MELT для headless-запуска на Android, iOS и Jetson через llama.cpp и MLC-LLM.
  - Энергия: **внешний Monsoon power monitor, 5 кГц**.
  - Тепло: тепловизор FLIR.
  - Контроль условий: фиксированная яркость, отключённые адаптивные функции и зарядка, вычитание энергии Wi-Fi-событий; nvpmodel на Jetson.
- **Результаты.** LLM-инференс в основном упирается в память. Квантование делает запуск возможным ценой заметной потери точности. Тепло и энергия мешают непрерывной работе.
- **Для ВКР.** Образец протокола контроля условий. Ограничение ВКР: внешнего монитора нет, его заменяют программные счётчики с валидацией.

### B11. Etalon — метрики LLM-инференса
**Amey Agrawal, Anmol Agarwal, Nitin Kedia, Jayashree Mohan, Souvik Kundu, Nipun Kwatra, Ramachandran Ramjee, Alexey Tumanov.** *Etalon: Holistic Performance Evaluation Framework for LLM Inference Systems.* arXiv:2407.07000 (ранее Metron), 2024 (**preprint**). Код Apache-2.0.
- **Суть.** TTFT, TBT, TPOT и нормализованная латентность скрывают «рывки» генерации. Предложены fluidity-index (доля токенов, уложившихся в дедлайны) и fluid token generation rate.
- **Для ВКР.** Для интерактивного агента важна не только средняя скорость, но и распределение межтокенных интервалов. Логировать нужно временные метки каждого токена, а не только итог.

### B12. ML.ENERGY Benchmark
**Jae-Won Chung, Jeff J. Ma, Ruofan Wu, Jiachen Liu, Oh Jun Kweon, Yuxuan Xia, Zhiyu Wu, Mosharaf Chowdhury.** *The ML.ENERGY Benchmark: Toward Automated Inference Energy Measurement and Optimization.* NeurIPS 2025 Datasets & Benchmarks (spotlight; по комментарию arXiv). arXiv:2505.06371. Проверено: arXiv. Код Apache-2.0.
- **Суть.** Четыре принципа энергобенчмаркинга. 40 архитектур на 6 задачах. Автоматические рекомендации дают экономию энергии, иногда больше 40 %, без изменения вычислений.
- **Для ВКР.** Метрики «энергия на запрос / на токен / на успешный ответ», энергия как первоклассная метрика рядом с точностью и латентностью.

### B13. Измерения на Apple M-series
**Paul Hübner, Andong Hu, Ivy Peng, Stefano Markidis.** *Apple vs. Oranges: Evaluating the Apple Silicon M-Series SoCs for HPC Performance and Efficiency.* IPDPSW 2025, pp. 45–54. DOI 10.1109/IPDPSW66978.2025.00013. arXiv:2502.05317. Проверено: arXiv + Crossref.
- Бенчмарки на Metal и Objective-C++ для M1–M4, мощность через `powermetrics`. Результаты: до ~100 ГБ/с пропускной способности памяти (в их тестах), до 2.9 TFLOPS FP32 на M4, мощность от единиц до 10–20 Вт, более 200 GFLOPS/Вт.
- **Для ВКР.** Рецензируемая работа, где `powermetrics` используется как инструмент энергоизмерений на M-series. Даёт порядок величин для проверок на правдоподобие.

Дополнительно (метаданные проверены по arXiv, все **preprint**):
- **Feng, Xu, Wang, Lin.** *Profiling Apple Silicon Performance for ML Training.* arXiv:2501.14925 (2025). Разрыв с NVIDIA объясняется системными факторами: page faults, мощность, время запуска ядер.
- **Corrochano López, García Sánchez.** *Evaluation of Domain-Specific Architectures for General-Purpose Applications in Apple Silicon.* arXiv:2511.13450 (2025). На M4 Pro ANE даёт до 3.8 TFLOPS GEMM против 4.7 у GPU, мощность GEMM 5.2 Вт на ANE против 24 Вт на GPU.
- **Kannan et al.** *GreenBench…* arXiv:2608.28667 (2026; по комментарию принят на IEEE ICCUBEA 2026). Мерит LLM-инференс на M4 Pro через `powermetrics` и сообщает **0.47 Вт CPU+GPU package power** при устойчивом инференсе. По моей оценке это неправдоподобно: Hübner'25 (10–20 Вт под нагрузкой), Corrochano'25 (24 Вт GPU в GEMM) и мой собственный замер (§6: GPU Energy ≈21–23 Вт во время инференса 8B) дают на порядки больше. Их установку я не перепроверял. Использовать **только как пример того, зачем нужны проверки на правдоподобие**, а не как источник чисел.

---

## 4. Часть C. Статистика оценивания LLM и агентов

### C1. Dror et al. — выбор теста значимости в NLP
**Rotem Dror, Gili Baumer, Segev Shlomov, Roi Reichart.** *The Hitchhiker's Guide to Testing Statistical Significance in Natural Language Processing.* ACL 2018 (long), pp. 1383–1392. DOI 10.18653/v1/P18-1128. Проверено: ACL Anthology.
- Обзор статей ACL/TACL 2017 показал, что значимость часто игнорируют или проверяют неверно. Авторы дают протокол выбора теста: параметрический или непараметрический, парный (bootstrap, permutation, McNemar) в зависимости от метрики и распределения.
- **Для ВКР.** Точность на BFCL — бинарный исход на пример при одном наборе примеров для двух систем. Подходят **точный McNemar** или **парный bootstrap / перестановочный тест**.

### C2. Card et al. — мощность экспериментов
**Dallas Card, Peter Henderson, Urvashi Khandelwal, Robin Jia, Kyle Mahowald, Dan Jurafsky.** *With Little Power Comes Great Responsibility.* EMNLP 2020. arXiv:2010.06595. Проверено: arXiv (комментарий «To appear at EMNLP 2020»).
- Многие сравнения в NLP недостаточно мощны. На малых тестах GLUE большинство сравнений с SOTA бессильны. В MT на 2 000 предложениях мощность для разницы в 1 BLEU около 75 %.
- **Для ВКР.** Перед экспериментом нужно посчитать минимально обнаружимую разность (MDE) для выбранных категорий BFCL (§7).

### C3. Miller — «error bars» для LLM-evals
**Evan Miller.** *Adding Error Bars to Evals: A Statistical Approach to Language Model Evaluations.* arXiv:2411.00640, 2024 (**preprint**, Anthropic). Проверено: arXiv + PDF.
- **Рекомендации:**
  1. стандартная ошибка через ЦПТ;
  2. кластерные стандартные ошибки для связанных групп вопросов;
  3. снижение дисперсии через K повторных ответов на вопрос или через вероятности следующего токена; температуру ради снижения дисперсии не менять;
  4. **парные разности по вопросам** при сравнении двух моделей. При корреляции 0.5 это даёт −1/3 дисперсии, формула SE_{A−B} = √(SE_A² + SE_B² − 2·SE_A·SE_B·ρ);
  5. анализ мощности.
- **Для ВКР.** Готовый рецепт отчёта по точности. Live-категории BFCL кластеризованы по источнику и функциям, поэтому нужна кластерная SE.

### C4. Madaan et al. — дисперсия бенчмарков
**Lovish Madaan, Aaditya K. Singh, Rylan Schaeffer, Andrew Poulton, Sanmi Koyejo, Pontus Stenetorp, Sharan Narang, Dieuwke Hupkes.** *Quantifying Variance in Evaluation Benchmarks.* arXiv:2406.10229, 2024 (**preprint**). Проверено: arXiv.
- Работа измеряет seed-дисперсию и монотонность и сравнивает дискретные метрики с непрерывными (log-prob). Для моделей около 7B формат задачи заметно влияет на дисперсию. Методы IRT дисперсию почти не снижают.
- **Для ВКР.** Непрерывные метрики (лог-вероятность правильного вызова, как в When2Call) снижают шум по сравнению с бинарной точностью.

### C5. Atil et al. — недетерминизм «детерминированных» настроек
**Berk Atil, Sarp Aykent, Alexa Chittams, Lisheng Fu, Rebecca J. Passonneau, Evan Radcliffe, Guru Rajan Rajagopal, Adam Sloan, Tomasz Tudrej, Ferhan Ture, Zhe Wu, Lixinyu Xu, Breck Baldwin.** *Non-Determinism of "Deterministic" LLM Settings.* arXiv:2408.04667, 2024 (**preprint**). Проверено: arXiv. Код Apache-2.0.
- Пять LLM, восемь задач, по 10 прогонов. Колебания точности до 15 %, разрыв между лучшим и худшим до 70 %. Метрики **TARr@N / TARa@N** (total agreement rate по сырому выводу и по распарсенному ответу).
- **Для ВКР.** На локальном llama.cpp нужно проверить детерминизм: N повторов при temp=0 и фиксированном seed. TARa@N показывать в отчёте.

### C6. Yuan et al. — численный источник недетерминизма
**Jiayi Yuan, Hao Li, Xinheng Ding, Wenya Xie, Yu-Jhe Li, Wentian Zhao, Kun Wan, Jing Shi, Xia Hu, Zirui Liu.** *Understanding and Mitigating Numerical Sources of Nondeterminism in LLM Inference* (ранее «Give Me FP32 or Give Me Death? …»). NeurIPS 2025 (oral). arXiv:2506.09501. Проверено: arXiv + neurips.cc.
- Причина — неассоциативность плавающей арифметики. При bf16 и greedy DeepSeek-R1-Distill-Qwen-7B даёт **до 9 % разброса точности и до 9 000 токенов разницы в длине ответа** в зависимости от размера батча, числа и типа GPU. Решение LayerCast: веса в 16 бит, вычисления в FP32.
- **Для ВКР.** В llama.cpp на Metal размер батча и ubatch, flash-attention и число потоков являются факторами, которые могут менять вывод. Сравнение «оптимизированный/базовый стек» обязано проверять **совпадение вывода** (bitwise или по распарсенным вызовам), а не только скорость. Это системная, а не ML-задача.

### C7. Kapoor et al. — «AI Agents That Matter»
**Sayash Kapoor, Benedikt Stroebl, Zachary S. Siegel, Nitya Nadgir, Arvind Narayanan.** *AI Agents That Matter.* Transactions on Machine Learning Research (TMLR), 05/2025. arXiv:2407.01502. Проверено: arXiv + OpenReview (TMLR).
- **Тезисы.** Оценивать только точность нельзя. Нужно совместно оптимизировать **стоимость и точность** (Парето-фронт). Простые baseline часто не хуже сложных агентов. Потребности разработчиков моделей и разработчиков приложений смешаны. Отложенные выборки недостаточны. Воспроизводимость низкая.
- **Для ВКР.** Главный тезис для edge-агента: отчёт в виде Парето-фронта «точность против латентности/энергии», обязательные простые baseline (один вызов без планировщика, без RAG и т. п.).

### C8. Zhu et al. — Agentic Benchmark Checklist (ABC)
**Yuxuan Zhu, Tengjun Jin, Yada Pruksachatkun, Andy Zhang, Shu Liu, Sasha Cui, Sayash Kapoor, Shayne Longpre, Kevin Meng, Rebecca Weiss, Fazl Barez, Rahul Gupta, Jwala Dhamala, Jacob Merizian, Mario Giulianelli, Harry Coppock, Cozmin Ududec, Jasjeet Sekhon, Jacob Steinhardt, Antony Kellermann, Sarah Schwettmann, Matei Zaharia, Ion Stoica, Percy Liang, Daniel Kang.** *Establishing Best Practices for Building Rigorous Agentic Benchmarks.* NeurIPS 2025 Datasets and Benchmarks Track, pp. 184435–184475. DOI 10.52202/085713-5547. arXiv:2507.02825. Проверено: arXiv + Crossref + neurips.cc.
- Дефекты постановки задач и наград ведут к ошибке оценки до 100 % в относительных величинах (например, SWE-bench Verified и τ-bench). Чек-лист ABC: task validity, outcome validity, reporting. Из 10 бенчмарков 7 нарушают task validity, 7 нарушают outcome validity, у всех 10 есть проблемы с отчётностью.
- **Для ВКР.** Если ВКР строит свой мини-набор задач edge-агента, его нужно прогнать по ABC: в частности, проверять, что пустой или тривиальный ответ не засчитывается.

### C9. Классические статистические ссылки (проверены по Crossref)
- **Wilson E. B.** Probable Inference, the Law of Succession, and Statistical Inference. JASA 22 (1927): 209–212. Используется для ДИ доли (точности).
- **McNemar Q.** Note on the Sampling Error of the Difference Between Correlated Proportions or Percentages. Psychometrika 12 (1947): 153–157. Парный тест для двух моделей на одних примерах.
- **Efron B.** Bootstrap Methods: Another Look at the Jackknife. Annals of Statistics 7 (1979). Bootstrap-ДИ медиан, квантилей и отношений.

---

## 5. Сводка: что заимствовать в протокол ВКР (из B и C)

1. **Факторы и уровни** (Hoefler R9): модель (0.5–8B), квантование (Q8_0/Q4_K_M/…), бэкенд (Metal/CPU), режим FC/prompt/грамматика, размер каталога инструментов, коммит llama.cpp, флаги (`-fa`, `-ub`, `-t`), состояние системы (AC, яркость, температура).
2. **Иерархия повторов** (Kalibera). Уровень 1 — запуск процесса с загрузкой модели: ≥5–10 независимых запусков на конфигурацию. Уровень 2 — запросы внутри запуска: прогон ≥60 с (правило MLPerf) после разогрева. Конец разогрева определяется по CoV окна (Georges) или по run-sequence plot (Kalibera). Число повторов уточняется по дисперсиям из пилота.
3. **Рандомизация** (Mytkowicz). Порядок конфигураций чередуется случайно (interleaving, ABBA), а не блоками «сначала все A». Это защищает от дрейфа температуры и фоновых задач.
4. **Проверка простоя (quiescence) перед прогоном:**
   - GPU utilization из `ioreg` (`Device Utilization %`) около 0;
   - нет чужих тяжёлых процессов (`ps`);
   - питание от сети (`pmset -g batt`), Low Power Mode выключен;
   - `pmset -g therm` без предупреждений.
   Все значения логируются в метаданные прогона.
5. **Отчёт по латентности.** Медиана и p90 с bootstrap-ДИ (10 000 ресэмплов, по запускам, кластерно). Сравнение двух стеков: **отношение медиан с bootstrap-ДИ** (Kalibera). Статистический тест: Mann–Whitney или Kruskal–Wallis (Hoefler R6/R7). Отдельно TTFT, prefill tok/s, decode tok/s, межтокенные интервалы (Etalon).
6. **Отчёт по точности.** Доля с ДИ Уилсона по категориям BFCL. Сравнение двух систем: **точный McNemar** и парный bootstrap по примерам (Miller, Dror). При многих сравнениях поправка Холма. Кластерная SE для live-категорий.
7. **Детерминизм.** TARa@N (Atil) на N=3–5 повторах при temp=0. Проверка совпадения распарсенных вызовов между базовым и оптимизированным стеком (Yuan).
8. **Энергия.** Джоули на запрос, на токен и **на правильный вызов**. Источники: per-process CPU-энергия, IOReport GPU/CPU, системная мощность из батарейной телеметрии. Обязательные проверки: энергия покоя (idle baseline), согласие источников между собой, порядок величин по сравнению с литературой.
9. **Парето-отчёт** (Kapoor). Точность против латентности или энергии, с простыми baseline.
10. **Артефакт воспроизводимости.** Скрипт, который собирает все факторы (`sysctl`, `sw_vers`, коммит, флаги, SHA модели) в JSON рядом с результатами.

---

## 6. Энергия на Apple Silicon без sudo: что реально доступно (проверено 2026-10-05 на этой машине)

| Источник | Что даёт | Права | Проверено здесь | Ограничения |
|---|---|---|---|---|
| `powermetrics` | CPU/GPU/ANE мощность, частоты, residency | **root** | нет (нет sudo) | агент запустить не может; пользователь может один раз для калибровки |
| asitop (MIT) | TUI поверх `powermetrics` | root | — | то же |
| macmon (MIT, `vladkens/macmon`) | CPU/GPU/ANE/RAM/System power, температуры, частоты; режим `pipe` с JSON, интервал `-i` | **без sudo** (по README; тот же источник, что у `powermetrics`) | не установлен | недокументированный API; нужна валидация |
| **IOReport, группа «Energy Model»** (`libIOReport`, приватный API) | счётчики энергии по каналам: `GPU Energy` (nJ), `CPU Energy`, `PACC*/EACC*` (mJ) и др. | **без sudo** | **да**: подписка работает; `GPU Energy` за 2 с в простое ≈1.6 Дж (≈0.8 Вт), под чужим 8B-инференсом ≈41–47 Дж (≈21–23 Вт) | в моей наивной схеме дельта по каналам CPU дала 0; macmon, видимо, читает их иначе, нужно разобраться. Частота обновления и точность неизвестны |
| **`proc_pid_rusage(RUSAGE_INFO_V6)`** (`<libproc.h>`, `<sys/resource.h>`) | per-process `ri_energy_nj`, `ri_penergy_nj` (P-ядра), `ri_instructions`, `ri_cycles`, `ri_pinstructions` | **без sudo** (свой процесс) | **да**: цикл на одном P-ядре 0.886 с → 3.31 Дж (≈3.7 Вт), 3.80·10⁹ циклов (≈4.3 ГГц), 2.80·10⁹ инструкций | только CPU, без GPU/ANE/DRAM. Энергия **модельная** (оценка ядра ОС). Поля времени (`ri_user_time`) на Apple Silicon идут в тиках `mach_absolute_time`, timebase 125/3 нс: известная ловушка |
| `ioreg -rn AppleSmartBattery` | `Voltage`, `InstantAmperage` (int64 в дополнительном коде), `PowerTelemetryData.SystemLoad` (мВт), `SystemPowerIn` (от адаптера) | **без sudo** | **да**: 10.781 В × 6.294 А = 67.9 Вт против `SystemLoad` = 67 324 мВт, сходится | мощность всей системы (экран, SSD, Wi-Fi); обновляется примерно раз в секунду (по наблюдению, нужно уточнить); семантика полей недокументирована |
| GPU utilization через `ioreg -r -c IOAccelerator` | `Device/Renderer/Tiler Utilization %` | без sudo | **да** (92 % во время чужого `llama-batched-bench`) | только загрузка, не энергия; годится для проверки простоя |

**Валидация энергосчётчиков** (сама по себе может быть главой ВКР):
1. Синтетические нагрузки известного профиля: 1/4/8 P-ядер, Metal GEMM, простой. Проверяется линейность и аддитивность каналов.
2. Сверка с `powermetrics`: пользователь один раз запускает его с `sudo` параллельно с нашими счётчиками.
3. Сверка суммы «CPU+GPU+DRAM» из IOReport с `SystemLoad` минус idle.
4. Длительный прогон от батареи: интеграл мощности против падения заряда в мВт·ч (`AppleRawCurrentCapacity`/`CurrentCapacity`) как грубая независимая проверка.

Пробы: `R7_artifacts/rusage_energy_probe.c`, `R7_artifacts/ioreport_energy_probe.c`.

---

## 7. Какие категории BFCL осмысленны для 0.5–8B и сколько нужно примеров

**Рекомендованный «edge-набор» BFCL (офлайн, AST):**
- `simple` (400), `multiple` (200), `parallel` (200), `parallel_multiple` (200). Малые модели набирают 50–95 %, различия между моделями и стеками видны.
- `live_simple` (258), `live_multiple` (1 053). Реальные запросы, большая выборка, у `live_multiple` узкий ДИ.
- `irrelevance` (240), `live_irrelevance` (882). Ложные вызовы для edge-агента особенно важны (энергия и действия). Разброс между моделями огромный: 4.9–90 %.
- Дополнительно When2Call (3 652) для решения «вызывать ли вообще», через log-prob.

**Не рекомендуется как основа:**
- `live_parallel` (16), `live_parallel_multiple` (24), `live_relevance` (18): ДИ ±12–22 п.п., сравнения бессмысленны.
- multi-turn (`miss_func`, `miss_param`, `long_context`) и agentic (`web_search`, `memory`): у моделей ≤3B результаты почти 0, у 7–8B 5–35 % (эффект пола). `web_search` требует сети и потому невоспроизводим. `multi_turn_base` допустим только для 7–8B как вторичная метрика.
- exec-категории и REST требуют внешних API. Java/JavaScript по желанию.

**Сколько примеров нужно** (расчёт в `R7_artifacts/stats_tables.py`):

| n примеров | полуширина 95 % ДИ Уилсона при p=0.8 | парная MDE (мощность 0.8, α=0.05) при 10 % дискордантных пар |
|---:|---:|---:|
| 200 | ±5.5 п.п. | ≈6.2 п.п. |
| 400 | ±3.9 п.п. | ≈4.4 п.п. |
| 1 000 | ±2.5 п.п. | ≈2.8 п.п. |
| 2 000 | — | ≈2.0 п.п. |
| 3 652 | ±1.3 п.п. | ≈1.5 п.п. |

**Вывод.** Для системных изменений, которые не должны менять точность (кэш, грамматика, планировщик), нужен **тест эквивалентности или неухудшения**, а не только «нет значимой разницы». Например: точность не хуже базовой на 2 п.п. по объединённому набору из ≥2 000 примеров.

**Повторы для точности.** При greedy-декодировании и подтверждённом детерминизме (TARa@N≈1) хватает одного прохода на пример. Если детерминизма нет, берётся K=3–5 проходов, и оценка идёт по средним на пример (Miller).

**Повторы для латентности и энергии.** ≥5–10 запусков процесса × ≥60 с запросов после разогрева на конфигурацию. Окончательное число определяется пилотом по Kalibera.

---

## 8. Углы для ВКР (реалистично для одного студента, M4 Pro / 24 ГБ, несколько месяцев)

### Угол 1. «EdgeFC-Bench»: воспроизводимый C/C++-харнесс измерения качества и стоимости вызова функций малыми LLM на Apple Silicon
- **Идея.** Тонкий C++-раннер поверх C API llama.cpp, без Python в горячем пути. Он прогоняет edge-набор BFCL и When2Call и для каждого запроса логирует:
  - число prefill/decode токенов, TTFT, межтокенные интервалы;
  - энергию по трём источникам из §6, пиковую память;
  - распарсенный вызов.
  Корректность проверяется независимо, официальным AST-чекером `bfcl-eval` как оракулом. Матрица факторов: модели 0.5–8B × квантование × FC/prompt/грамматика × размер каталога. Результат — Парето-фронты «точность против Дж/запрос и p90 латентности» со статистикой из §5.
- **Системная часть.** C API llama.cpp, счётчики `proc_pid_rusage`/IOReport/SMC-батарея, протокол measurement hygiene, валидация энергосчётчиков.
- **Реализуемость на M4 Pro.** Высокая. Модели до 8B Q4 влезают с запасом. Edge-набор примерно 3 500 + 3 652 примеров; на 1.5B — минуты на конфигурацию, на 8B — до часа.
- **Baselines.** Опубликованные числа BFCL (таблица 1) для тех же моделей и режима; `llama-server` с `--jinja` как «стек по умолчанию»; `llama-bench` для потолков tok/s; TinyAgent и Less-is-More как edge-ориентиры.
- **Метрики.** AST-точность с ДИ Уилсона, McNemar против baseline, TARa@N, TTFT/p90 с bootstrap-ДИ, Дж/запрос и Дж/правильный вызов, токены промпта.
- **Риски.**
  - «Это просто бенчмаркинг»: снимается научной частью (валидация энергии без root, декомпозиция дисперсии, найденные эффекты шаблона и режима).
  - Несовпадение с цифрами лидерборда из-за промптов.
  - Недокументированные API могут измениться в следующей macOS.

### Угол 2. Снижение стоимости каталога инструментов: KV-кэш статического префикса + выбор инструментов в llama.cpp
- **Идея.** Prefill каталога инструментов доминирует в TTFT: по ENVIRONMENT.md около 1 500 токенов дают ≈4.3 с на Qwen3-8B Q4. Сравниваются три подхода:
  - (a) сохранение и восстановление KV-состояния префикса «system + каталог» через `llama_state_seq_*`;
  - (b) Tool RAG / Less-is-More: в промпт идут только k релевантных инструментов;
  - (c) их комбинация: кэш по подмножествам или упорядоченный каталог с переиспользуемым префиксом.
- **Системная часть.** Управление KV-кэшем, сериализация состояния, учёт памяти, порядок инструментов ради максимального общего префикса.
- **Baselines.** Без кэша и без отбора; `cache_prompt` в `llama-server`; Less-is-More (DATE'25); Tool RAG из TinyAgent (EMNLP'24 Demo).
- **Метрики.** TTFT, Дж/запрос, точность на `multiple`/`live_multiple`/`irrelevance` (тест неухудшения), recall отбора инструментов, объём KV-состояний.
- **Риски.** Prefix caching известен (см. R2), поэтому новизна должна лежать в edge-специфике и честном измерении. Отбор инструментов может ухудшить irrelevance-поведение. Нужен аккуратный тест эквивалентности.

### Угол 3. Цена и ускорение грамматически ограниченного декодирования вызовов функций в llama.cpp
- **Идея.** По JSONSchemaBench грамматика в llama.cpp почти удваивает TPOT (≈28–30 против ≈15–16 мс на 8B). При этом главный провал SLM — формат вывода (EASE'25, ошибки декодирования в BFCL). Предлагается:
  - компиляция JSON Schema инструментов в GBNF с кэшем;
  - **jump-forward / fast-forward вынужденных токенов** (ключи, скобки, имена функций), когда автомат допускает одну последовательность;
  - измерение влияния на точность и на стоимость.
- **Системная часть.** Автоматы и маски токенов в C++, интеграция с сэмплером llama.cpp (`llama_sampler_init_grammar`). Подробнее в R3.
- **Baselines.** Генерация без ограничений + парсер; встроенная грамматика llama.cpp; llguidance/XGrammar, если доступны в сборке.
- **Метрики.** Доля валидных вызовов, AST-точность, TPOT/TTFT, время компиляции грамматики, Дж/вызов.
- **Риски.** В апстриме llama.cpp уже есть часть механизмов (llguidance), поэтому нужно чётко отделить свой вклад. Fast-forward меняет токенизацию, и это может влиять на качество.

### Угол 4. Валидация программных энергосчётчиков Apple Silicon для edge-AI-нагрузок без root (самостоятельная работа или методологическая глава угла 1)
- **Идея.** Систематически сравнить `ri_energy_nj`, IOReport «Energy Model» и `AppleSmartBattery` `SystemLoad` с `powermetrics` (разовый запуск пользователем) на синтетических и LLM-нагрузках: точность, задержка, разрешение, аддитивность. Выпустить C-библиотеку «энергия на участок кода».
- **Baselines.** `powermetrics`, macmon, разряд батареи.
- **Метрики.** Ошибка относительно эталона (MAPE), корреляция, разрешение по времени, накладные расходы чтения.
- **Риски.** Нет внешнего ваттметра, а `powermetrics` сам модельный. Приватные API могут меняться. Связь с «ИИ-агентами» прямая только в составе угла 1.

### Угол 5 (дополнительный). Детерминизм и воспроизводимость локальных агентных прогонов
- **Идея.** Измерить, как `-ub`/`-b`, flash-attention, число потоков и бэкенд (Metal/CPU) меняют вывод и точность BFCL при greedy-декодировании (по аналогии с Yuan, NeurIPS'25 и Atil'24). Предложить «детерминированный профиль» запуска.
- **Метрики.** TARr/TARa@N, расхождение логитов, точность.
- **Риски.** Эффект может оказаться малым на одной машине. Лучше как раздел угла 1, а не отдельная тема.

**Рекомендация.** Угол 1 как каркас, угол 2 или угол 3 как основной вклад «оптимизации», угол 4 как методологическая глава. Это хорошо ложится на ИВТ и системное ПО: C/C++, ОС-счётчики, KV-кэш и автоматы, строгий эксперимент.

---

## 9. Открытые инструменты, код и данные

| Название | URL | Лицензия | Использование |
|---|---|---|---|
| BFCL (`bfcl-eval`, данные HF) | https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard ; https://huggingface.co/datasets/gorilla-llm/Berkeley-Function-Calling-Leaderboard | Apache-2.0 | основной бенчмарк и AST-оракул |
| When2Call | https://github.com/NVIDIA/When2Call ; HF `nvidia/When2Call` | код Apache-2.0; данные CC-BY-4.0 | решение «вызывать ли», log-prob |
| NESTFUL | https://github.com/IBM/NESTFUL ; HF `ibm-research/nestful` | Apache-2.0 | вложенные цепочки |
| Seal-Tools | https://github.com/fairyshine/Seal-Tools | Apache-2.0 | большой каталог инструментов |
| τ-bench / τ²-bench | https://github.com/sierra-research/tau-bench ; https://github.com/sierra-research/tau2-bench | MIT | метрика pass^k, идеи |
| ToolSandbox | https://github.com/apple/ToolSandbox | собственная лицензия Apple | идеи milestones/minefields |
| ToolBench / StableToolBench | https://github.com/OpenBMB/ToolBench | Apache-2.0 | справочно |
| ComplexFuncBench | https://github.com/zai-org/ComplexFuncBench ; HF `zai-org/ComplexFuncBench` | данные Apache-2.0 (HF) | справочно |
| xLAM function-calling-60k | HF `Salesforce/xlam-function-calling-60k` | CC-BY-4.0 (gated) | обучение / отбор инструментов |
| Hammer (код, модели) | https://github.com/MadeAgents/Hammer | код Apache-2.0; веса Hammer2.1 CC-BY-NC-4.0 | малые FC-модели |
| TinyAgent | https://github.com/SqueezeAILab/TinyAgent | MIT | Tool RAG, baseline |
| JSONSchemaBench | https://github.com/guidance-ai/jsonschemabench ; HF `epfl-dlab/JSONSchemaBench` | данные MIT | бенчмарк constrained decoding |
| llama.cpp | https://github.com/ggml-org/llama.cpp | MIT | движок; `llama-bench` (`-r`, `--delay`, `-o json`), `llama-server` (`--json-schema`, `--grammar`) |
| MLPerf Inference / Tiny / Mobile / Client | https://github.com/mlcommons/inference ; …/tiny ; …/mobile_app_open ; …/mlperf_client | Apache-2.0 | правила, LoadGen, терминология |
| ML.ENERGY Benchmark | https://github.com/ml-energy/benchmark | Apache-2.0 | методика энергобенчмаркинга |
| Etalon | https://github.com/project-etalon/etalon | Apache-2.0 | метрики межтокенных интервалов |
| llm-stability (Atil et al.) | https://github.com/breckbaldwin/llm-stability | Apache-2.0 | TARr/TARa@N |
| macmon | https://github.com/vladkens/macmon | MIT | энергия CPU/GPU/ANE без sudo |
| asitop / mactop | https://github.com/tlkh/asitop ; https://github.com/context-labs/mactop | MIT | мониторинг (root) |
| Пробы R7 | `research/literature/R7_artifacts/` | — | `proc_pid_rusage` и IOReport без sudo, статистические таблицы |

---

## 10. Открытые проблемы (честно)

1. **Нет эталона энергии.** Без внешнего ваттметра все источники модельные. Нужна хотя бы сверка с `powermetrics` от пользователя и проверки на правдоподобие. Каналы CPU в IOReport у меня пока дают 0, причина не выяснена.
2. **Шаблоны чата и FC-режим.** Числа BFCL для локальных GGUF-моделей зависят от шаблона (`--jinja`) и парсера `llama-server`. Сравнимость с лидербордом не гарантирована, нужна собственная калибровка на 1–2 моделях.
3. **Версионирование BFCL.** Данные v3/v4 различаются, размеры категорий в карточке HF и в файлах расходятся. Нужно фиксировать коммит.
4. **Эффект пола на multi-turn.** У малых моделей нет хорошего многошагового бенчмарка, который работает офлайн и детерминированно. Это ниша для небольшого собственного набора, построенного по чек-листу ABC.
5. **Недетерминизм Metal.** Не известно, детерминирован ли llama.cpp на Metal при разных `-ub`/`-b`/`-fa`. Это надо измерить до любых сравнений точности.
6. **Общая машина.** Другие агенты одновременно гоняют бенчмарки (замечен `llama-batched-bench`), ноутбук работает от батареи. Любые числа, собранные так, нельзя использовать. Нужны очередь или эксклюзивный доступ к машине и запись метаданных простоя.
7. **Апстрим llama.cpp быстро меняется** (prefix cache, llguidance, шаблоны). Для углов 2 и 3 нужно заранее проверить, что ещё не сделано, и собирать из фиксированного коммита.

---

## 11. Список литературы (проверенные записи)

1. Patil S. G., Zhang T., Wang X., Gonzalez J. E. Gorilla: Large Language Model Connected with Massive APIs // NeurIPS 37, 2024. P. 126544–126565. DOI 10.52202/079017-4020. arXiv:2305.15334.
2. Patil S. G., Mao H., Yan F., Ji C. C.-J., Suresh V., Stoica I., Gonzalez J. E. The Berkeley Function Calling Leaderboard (BFCL): From Tool Use to Agentic Evaluation of Large Language Models // ICML 2025, PMLR 267. P. 48371–48392.
3. Li M., Zhao Y., Yu B., Song F., Li H., Yu H., Li Z., Huang F., Li Y. API-Bank: A Comprehensive Benchmark for Tool-Augmented LLMs // EMNLP 2023. DOI 10.18653/v1/2023.emnlp-main.187.
4. Qin Y., Liang S., Ye Y., et al. ToolLLM: Facilitating Large Language Models to Master 16000+ Real-world APIs // ICLR 2024. arXiv:2307.16789.
5. Guo Z., Cheng S., Wang H., Liang S., Qin Y., Li P., Liu Z., Sun M., Liu Y. StableToolBench: Towards Stable Large-Scale Benchmarking on Tool Learning of Large Language Models // Findings of ACL 2024. DOI 10.18653/v1/2024.findings-acl.664.
6. Yao S., Shinn N., Razavi P., Narasimhan K. τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains // ICLR 2025. arXiv:2406.12045.
7. Barres V., Dong H., Ray S., Si X., Narasimhan K. τ²-Bench: Evaluating Conversational Agents in a Dual-Control Environment // arXiv:2506.07982, 2025 (preprint).
8. Ross H., Mahabaleshwarkar A. S., Suhara Y. When2Call: When (not) to Call Tools // NAACL 2025. DOI 10.18653/v1/2025.naacl-long.174.
9. Basu K., Abdelaziz I., Kate K., et al. NESTFUL: A Benchmark for Evaluating LLMs on Nested Sequences of API Calls // EMNLP 2025. DOI 10.18653/v1/2025.emnlp-main.1702.
10. Lu J., Holleis T., Zhang Y., et al. ToolSandbox: A Stateful, Conversational, Interactive Evaluation Benchmark for LLM Tool Use Capabilities // Findings of NAACL 2025. DOI 10.18653/v1/2025.findings-naacl.65.
11. Wu M., Zhu T., Han H., Tan C., Zhang X., Chen W. Seal-Tools: Self-instruct Tool Learning Dataset for Agent Tuning and Detailed Benchmark // NLPCC 2024, LNCS. DOI 10.1007/978-981-97-9434-8_29.
12. Zhong L., Du Z., Zhang X., Hu H., Tang J. ComplexFuncBench: Exploring Multi-Step and Constrained Function Calling under Long-Context Scenario // arXiv:2501.10132, 2025 (preprint).
13. Erdogan L. E., Lee N., Jha S., et al. TinyAgent: Function Calling at the Edge // EMNLP 2024 System Demonstrations. DOI 10.18653/v1/2024.emnlp-demo.9.
14. Lin Q., Wen M., Peng Q., et al. Hammer: Robust Function-Calling for On-Device Language Models via Function Masking // ICLR 2025. arXiv:2410.04587.
15. Paramanayakam V., Karatzas A., Anagnostopoulos I., Stamoulis D. Less is More: Optimizing Function Calling for LLM Execution on Edge Devices // DATE 2025. DOI 10.23919/DATE64628.2025.10992798.
16. Kavathekar I., Donakanti R., Kumaraguru P., Vaidhyanathan K. Small Models, Big Tasks: An Exploratory Empirical Study on Small Language Models for Function Calling // EASE 2025. arXiv:2504.19277.
17. Geng S., Cooper H., Moskal M., et al. JSONSchemaBench: A Rigorous Benchmark of Structured Outputs for Language Models // arXiv:2501.10868, 2025 (preprint).
18. Georges A., Buytaert D., Eeckhout L. Statistically Rigorous Java Performance Evaluation // OOPSLA 2007. P. 57–76. DOI 10.1145/1297027.1297033.
19. Mytkowicz T., Diwan A., Hauswirth M., Sweeney P. F. Producing Wrong Data Without Doing Anything Obviously Wrong! // ASPLOS 2009. P. 265–276. DOI 10.1145/1508244.1508275.
20. Kalibera T., Jones R. Rigorous Benchmarking in Reasonable Time // ISMM 2013. P. 63–74. DOI 10.1145/2464157.2464160.
21. Curtsinger C., Berger E. D. STABILIZER: Statistically Sound Performance Evaluation // ASPLOS 2013. P. 219–228. DOI 10.1145/2451116.2451141.
22. Hoefler T., Belli R. Scientific Benchmarking of Parallel Computing Systems: Twelve Ways to Tell the Masses When Reporting Performance Results // SC 2015. DOI 10.1145/2807591.2807644.
23. Reddi V. J., Cheng C., Kanter D., et al. MLPerf Inference Benchmark // ISCA 2020. P. 446–459. DOI 10.1109/ISCA45697.2020.00045.
24. Banbury C., Reddi V. J., Torelli P., et al. MLPerf Tiny Benchmark // NeurIPS 2021 Datasets and Benchmarks Track. arXiv:2106.07597.
25. Reddi V. J., Kanter D., Mattson P., et al. MLPerf Mobile Inference Benchmark: An Industry-Standard Open-Source Machine Learning Benchmark for On-Device AI // MLSys 2022. arXiv:2012.02328.
26. Laskaridis S., Katevas K., Minto L., Haddadi H. MELTing Point: Mobile Evaluation of Language Transformers // MobiCom 2024. DOI 10.1145/3636534.3690668.
27. Agrawal A., Agarwal A., Kedia N., et al. Etalon: Holistic Performance Evaluation Framework for LLM Inference Systems // arXiv:2407.07000, 2024 (preprint).
28. Chung J.-W., Ma J. J., Wu R., et al. The ML.ENERGY Benchmark: Toward Automated Inference Energy Measurement and Optimization // NeurIPS 2025 D&B. arXiv:2505.06371.
29. Hübner P., Hu A., Peng I., Markidis S. Apple vs. Oranges: Evaluating the Apple Silicon M-Series SoCs for HPC Performance and Efficiency // IPDPSW 2025. P. 45–54. DOI 10.1109/IPDPSW66978.2025.00013.
30. Dror R., Baumer G., Shlomov S., Reichart R. The Hitchhiker's Guide to Testing Statistical Significance in Natural Language Processing // ACL 2018. P. 1383–1392. DOI 10.18653/v1/P18-1128.
31. Card D., Henderson P., Khandelwal U., Jia R., Mahowald K., Jurafsky D. With Little Power Comes Great Responsibility // EMNLP 2020. arXiv:2010.06595.
32. Miller E. Adding Error Bars to Evals: A Statistical Approach to Language Model Evaluations // arXiv:2411.00640, 2024 (preprint).
33. Madaan L., Singh A. K., Schaeffer R., et al. Quantifying Variance in Evaluation Benchmarks // arXiv:2406.10229, 2024 (preprint).
34. Atil B., Aykent S., Chittams A., et al. Non-Determinism of "Deterministic" LLM Settings // arXiv:2408.04667, 2024 (preprint).
35. Yuan J., Li H., Ding X., et al. Understanding and Mitigating Numerical Sources of Nondeterminism in LLM Inference // NeurIPS 2025. arXiv:2506.09501.
36. Kapoor S., Stroebl B., Siegel Z. S., Nadgir N., Narayanan A. AI Agents That Matter // TMLR, 2025. arXiv:2407.01502.
37. Zhu Y., Jin T., Pruksachatkun Y., et al. Establishing Best Practices for Building Rigorous Agentic Benchmarks // NeurIPS 38, 2025 (D&B). P. 184435–184475. DOI 10.52202/085713-5547.
38. Wilson E. B. Probable Inference, the Law of Succession, and Statistical Inference // JASA. 1927. Vol. 22. P. 209–212.
39. McNemar Q. Note on the Sampling Error of the Difference Between Correlated Proportions or Percentages // Psychometrika. 1947. Vol. 12. P. 153–157.
40. Efron B. Bootstrap Methods: Another Look at the Jackknife // Annals of Statistics. 1979. Vol. 7.

Справочно (preprint, метаданные проверены): Feng et al. arXiv:2501.14925; Corrochano López & García Sánchez arXiv:2511.13450; Kannan et al. (GreenBench) arXiv:2608.28667; Paramanayakam et al. (CarbonCall) arXiv:2504.20348. Документация: MLPerf Client v2.0 (MLCommons).

---

## 12. Связь с наследием прошлого агента (кратко)

Работа `legacy/edge-sentinel` (MLP на UCI digits, детерминированный «агент»-контроллер выбора linear/MLP/INT8, C ABI-библиотека, планировщик памяти на двух игрушечных графах) **не пересекается** с областью бенчмарков вызова функций и LLM-агентов. В части B у неё нет корректной статистики производительности: нет иерархии повторов, ДИ и контроля условий. Перенести стоит только инженерные привычки: независимый проверяющий (trace-checker соответствует AST-оракулу BFCL), фиксацию SHA артефактов и тесты. Содержательная база ВКР по этой теме должна строиться заново, вокруг BFCL, When2Call, llama.cpp и протокола §5.
