# R1. LLM-агенты на граничных (edge/on-device) устройствах и вызов функций малыми моделями — систематический обзор литературы

- Дата составления: 2026-10-05
- Контекст: бакалаврская ВКР, 09.03.01 «Информатика и вычислительная техника» (ИТМО). Тема руководителя: «Разработка ИИ-агентов для приложений граничного искусственного интеллекта (Edge AI)». Узкая формулировка пока не утверждена.
- Целевая платформа экспериментов: MacBook, Apple M4 Pro (8P+4E), 24 ГБ unified memory, Metal, llama.cpp b11146 (brew), без sudo.
- Область заметки: агенты на основе LLM/SLM (малых языковых моделей), которые вызывают функции (function calling / tool use) на устройстве, а также системные механизмы, от которых зависят латентность, память и энергия агентного цикла.

---

## 0. Методика поиска и проверки

**Источники поиска и проверки метаданных.**
- arXiv API (`export.arxiv.org/api/query?id_list=...`): название, полный список авторов, дата первой версии, поля `comment`/`journal_ref` (там часто указан venue).
- OpenReview API v2 (`api2.openreview.net/notes/search`): venue и статус для ICLR/ICML/NeurIPS (поле `venueid`).
- OpenAlex (`api.openalex.org/works`): DOI рецензируемой версии (ACL Anthology `10.18653/...`, ACM `10.1145/...`, IEEE `10.1109/...`, DATE `10.23919/...`, NeurIPS-proceedings `10.52202/...`).
- Страницы PMLR, ACL Anthology, ICML/ICLR virtual: точечная сверка.
- Численные результаты брались из аннотаций и HTML-версий статей на arXiv. Источник каждой цифры указан.
- Карточки моделей на Hugging Face (README) и GitHub API: лицензии и числа, которых нет в статьях (Llama 3.2, FunctionGemma).
- dblp закрыт anti-bot защитой (Anubis), поэтому его не использовал. Semantic Scholar не использовал.

**Критерии включения.** (1) Агенты с tool use / function calling. (2) Малые модели (≲8B) или on-device/edge-развёртывание. (3) Системные механизмы агентного цикла: prefill описаний инструментов, ограниченное декодирование, асинхронные вызовы, кэширование, планирование на SoC. (4) Бенчмарки. Период 2022–2026, основной акцент на 2024–2026.

**Обозначения статуса.**
- **[V]** — проверено: название, авторы и год сверены, рецензируемый venue подтверждён.
- **[V-pre]** — проверено как препринт или технический отчёт: метаданные сверены, рецензируемого venue нет (или не найден).
- **[V-card]** — источник не статья, а официальная карточка модели или документация.

### 0.1. Исправления к «затравочному» списку (важно для библиографии ВКР)

| Работа | Как было в затравке / «по памяти» | Как на самом деле (проверено) |
|---|---|---|
| Hammer (Lin et al.) | arXiv 2024 | **ICLR 2025 (Spotlight)**, но под **изменённым названием**: «Robust Function-Calling for On-Device Language Model via Function Masking». Препринт arXiv 2410.04587 называется «Hammer: Robust Function-Calling for On-Device Language Models via Function Masking» |
| Gorilla (Patil et al.) | 2023 | arXiv 2023 (2305.15334). Рецензируемая публикация: **NeurIPS 2024** (poster) |
| xLAM (Zhang et al.) | тех. отчёт Salesforce | arXiv 2409.03215, затем **NAACL 2025 (long)**, DOI 10.18653/v1/2025.naacl-long.578 |
| APIGen | — | **NeurIPS 2024, Datasets & Benchmarks Track** |
| ToolACE | — | **ICLR 2025 (poster)** |
| TinyAgent | «EMNLP 2024» | **EMNLP 2024 System Demonstrations**, а не main track. DOI 10.18653/v1/2024.emnlp-demo.9 |
| LLM as a System Service on Mobile Devices (Yin et al.) | статья | только **технический отчёт arXiv** (2403.11805). Рецензируемая работа той же группы — другая статья: «Elastic On-Device LLM Service» (MobiCom 2025) |
| Mobile Edge Intelligence for LLMs | обзор | **IEEE Communications Surveys & Tutorials, 2025**, DOI 10.1109/COMST.2025.3527641 |
| On-Device Language Models: A Comprehensive Review (Xu et al.) | обзор | только **arXiv-препринт**. Соавторы (Zhiyuan Li, Wei Chen) — авторы Octopus из Nexa AI, поэтому возможен конфликт интересов при оценке их моделей |
| Octopus v2 / v4 | — | **препринты** Nexa AI. Рецензируемых публикаций не нашёл |
| Small Language Models are the Future of Agentic AI (Belcak et al.) | NVIDIA 2025 | **позиционный препринт** arXiv 2506.02153 (v3) |
| NexusRaven | модель для function calling | **воркшоп NeurIPS 2023** (Instruction Workshop; FMDM). Модель 13B, то есть не on-device. Авторов отдельно не сверял |
| Apple Intelligence Foundation LMs 2024 / 2025 | — | **технические отчёты** arXiv 2407.21075 и 2507.13575 (без рецензии) |
| BFCL | «лидерборд» | рецензируемая статья: **ICML 2025**, PMLR 267:48371–48392 |
| Speculative Actions | — | arXiv 2510.04371, затем **ICLR 2026 (Oral)** под названием «Speculative Actions: A Lossless Framework for Faster AI Agents» |

---

## 1. Карта области

```
                         ┌──────────────── агентный цикл на устройстве ────────────────┐
 запрос ─► [выбор инструментов] ─► [prefill: system + схемы инструментов + история] ─►
        ─► [decode: план / вызов функции (JSON/DAG/код), constrained decoding] ─►
        ─► [исполнение инструмента(ов): последовательно / параллельно / асинхронно] ─►
        ─► [наблюдение → повтор цикла (ReAct) или ответ]
 Ось A (модели/данные):  Octopus, TinyAgent, xLAM/APIGen, Hammer, ToolACE, FunctionGemma, Qwen3…
 Ось B (оценка):         BFCL, API-Bank, τ-bench, When2Call, JSONSchemaBench, MELT…
 Ось C (системы):        Tool-RAG/Less-is-More, Prompt Cache/EPIC/CacheBlend/ReCache, XGrammar,
                         LLMCompiler/AsyncLM/AsyncFC/Conveyor, Speculative actions/PASTE,
                         LLMS/ELMS/Agent.xpu/llm.npu (ОС-уровень и SoC)
```

**Главный вывод карты для ИВТ-диплома.** Ось A (обучение и дообучение моделей) хорошо заселена и требует GPU-кластера. Ось C (системный рантайм агентного цикла на устройстве) заселена в основном серверными работами (vLLM, SGLang, A100/A800). Именно на оси C больше всего пробелов, которые один студент может закрыть на ноутбуке. Ось B даёт готовые метрики и бенчмарки.

---

## 2. Ключевые работы (подробные карточки)

### Группа A. Основы tool use и агентного цикла

#### [A1] ReAct — [V]
- **Библ.:** Yao S., Zhao J., Yu D., Du N., Shafran I., Narasimhan K., Cao Y. *ReAct: Synergizing Reasoning and Acting in Language Models* // ICLR 2023. arXiv:2210.03629. https://arxiv.org/abs/2210.03629
- **Проблема.** Рассуждение (chain-of-thought) и действие (генерация плана или действий) изучались раздельно. CoT галлюцинирует и накапливает ошибки.
- **Метод.** Модель чередует шаги Thought → Action → Observation, действия идут во внешнюю среду или API (например, Wikipedia API).
- **Результаты.** На ALFWorld и WebShop ReAct превосходит imitation/RL-методы на +34% и +10% абсолютного success rate, используя 1–2 in-context примера (из аннотации).
- **Польза для ВКР.** Это канонический последовательный агентный цикл и обязательный baseline для любых ускорений (параллельных, асинхронных, спекулятивных): каждое действие блокирует генерацию.
- **Что заимствовать.** Формат цикла и последовательный ReAct как нижнюю границу по латентности.

#### [A2] Toolformer — [V]
- **Библ.:** Schick T., Dwivedi-Yu J., Dessì R., Raileanu R., Lomeli M., Zettlemoyer L., Cancedda N., Scialom T. *Toolformer: Language Models Can Teach Themselves to Use Tools* // NeurIPS 2023. arXiv:2302.04761. https://arxiv.org/abs/2302.04761
- **Проблема.** LM плохо справляются с арифметикой и фактами, хотя простые инструменты решают это легко.
- **Метод.** Самообучение: модель сама размечает корпус вызовами API (калькулятор, QA, поиск, перевод, календарь) и оставляет только те вызовы, которые снижают перплексию.
- **Результаты.** Существенный прирост zero-shot на ряде задач, часто на уровне гораздо более крупных моделей (из аннотации).
- **Польза.** Исторически первая работа об «инструменте как токенах внутри генерации». Нужна для обзорной главы.
- **Заимствовать.** Концепцию; для экспериментов не нужна.

#### [A3] Gorilla — [V]
- **Библ.:** Patil S.G., Zhang T., Wang X., Gonzalez J.E. *Gorilla: Large Language Model Connected with Massive APIs* // NeurIPS 2024 (poster). arXiv:2305.15334 (2023). https://arxiv.org/abs/2305.15334
- **Проблема.** LLM галлюцинируют аргументы и устаревшие API.
- **Метод.** LLaMA, дообученная на вызовах API, плюс retriever по документации (retrieval-aware training). Бенчмарк APIBench (HuggingFace / TorchHub / TensorHub).
- **Результаты.** Превосходит GPT-4 в написании API-вызовов на APIBench и адаптируется к изменениям документации во время теста (из аннотации).
- **Польза.** Отсюда вырос BFCL [D1], главный бенчмарк области, и AST-метод оценки вызовов.
- **Заимствовать.** Идею retrieval-aware вызова и AST-сравнение вызовов.

#### [A4] ToolLLM / ToolBench — [V]
- **Библ.:** Qin Y., Liang S., Ye Y., Zhu K., Yan L., Lu Y., et al. (19 авт.). *ToolLLM: Facilitating Large Language Models to Master 16000+ Real-world APIs* // ICLR 2024 (spotlight). arXiv:2307.16789. https://arxiv.org/abs/2307.16789
- **Метод.** ToolBench: 16 464 REST API из 49 категорий RapidAPI. DFS-дерево решений (DFSDT), автоматический оценщик ToolEval, нейронный API-retriever.
- **Результаты.** ToolLLaMA близка к ChatGPT и обобщается на APIBench (из аннотации).
- **Польза.** Это масштаб «тысяч инструментов». Для on-device релевантно ограниченно: сотни тысяч токенов документации на устройство не помещаются, и это аргумент за выбор и кэширование инструментов.
- **Заимствовать.** Идею нейронного retriever-а инструментов.

#### [A5] LLMCompiler — [V]
- **Библ.:** Kim S., Moon S., Tabrizi R., Lee N., Mahoney M.W., Keutzer K., Gholami A. *An LLM Compiler for Parallel Function Calling* // ICML 2024. arXiv:2312.04511. https://arxiv.org/abs/2312.04511
- **Проблема.** ReAct вызывает функции последовательно, что даёт высокую латентность и стоимость.
- **Метод.** Аналогия с компилятором. Planner генерирует DAG вызовов с зависимостями (`$1`, `$2`), Task Fetching Unit диспетчеризует готовые задачи, Executor исполняет их параллельно.
- **Результаты.** До **3.7×** ускорения, до **6.7×** экономии стоимости и до **~9%** прироста точности относительно ReAct (из аннотации).
- **Польза для ВКР.** Прямой предок TinyAgent [B3]. Даёт системную постановку агента как DAG-планировщика: это задача ИВТ (зависимости, диспетчеризация, параллелизм). Легаси-проект уже содержит C++-исполнитель DAG (см. §8).
- **Заимствовать.** Формат плана, алгоритм диспетчеризации и baseline. Код открыт (MIT).

#### [A6] CodeAct — [V]
- **Библ.:** Wang X., Chen Y., Yuan L., Zhang Y., Li Y., Peng H., Ji H. *Executable Code Actions Elicit Better LLM Agents* // ICML 2024. arXiv:2402.01030.
- **Суть.** Действия агента записываются как исполняемый Python, а не JSON. До +20% success rate на API-Bank и собственном бенчмарке (17 LLM).
- **Польза.** Альтернатива JSON-вызовам. В on-device варианте эту идею развивает AutoDroid-V2 [B12] (код вместо пошаговых UI-действий).

### Группа B. Малые модели и методы function calling для устройства

#### [B1] Octopus v2 — [V-pre]
- **Библ.:** Chen W., Li Z. *Octopus v2: On-device language model for super agent*. arXiv:2404.01744 (v7), 2024. Препринт (Nexa AI).
- **Проблема.** RAG-подход к function calling на устройстве даёт длинный контекст и высокую латентность.
- **Метод.** Gemma-2B. Каждая функция кодируется **специальным «функциональным токеном»** (`<nexa_0>`…), и выбор функции становится классификацией одного токена. Описания функций «вшиваются» в веса дообучением, поэтому контекст сокращается на 95%.
- **Результаты (HTML-версия статьи).** Точность на 20 Android-API: Octopus v2 **99.524%**, GPT-4 98.571%, Llama-7B+RAG 68.095%. Латентность на вызов: **0.38 с** у Octopus против 1.02 с у GPT-4 и 13.46 с у Llama-7B+RAG. На мобильном устройстве 1.1–1.7 с. LoRA r=16 даёт 99.048% против 99.524% у full FT. 100 примеров на API дают 98.095%.
- **Критика.** Набор функций закрытый и фиксированный: новый инструмент требует дообучения. Оценка ведётся на собственном датасете авторов, сравнений на BFCL нет. Препринт.
- **Польза.** Это крайняя точка спектра «инструменты в весах» (против «инструментов в промпте»). Идея функциональных токенов пригодится как сигнал уверенности для маршрутизации (угол 4).
- **Заимствовать.** Метрику латентности на вызов и идею однотокенного выбора функции.

#### [B2] Octopus v4 и Octo-planner — [V-pre]
- **Библ.:** Chen W., Li Z. *Octopus v4: Graph of language models*. arXiv:2404.19296, 2024. Chen W., Li Z., Guo Z., Shen Y. *Octo-planner: On-device Language Model for Planner-Action Agents*. arXiv:2406.18082, 2024.
- **Суть v4.** Функциональные токены маршрутизируют запрос к «вертикальным» моделям, которые образуют граф моделей. Авторы заявляют MMLU 74.8 при активации моделей <10B.
- **Суть Octo-planner.** Разделение planner (Phi-3 Mini 3.8B, дообученный) и action-агента (Octopus). 97% success в in-domain тесте. Multi-LoRA слияние для нескольких доменов.
- **Польза.** Пример гетерогенного агента из нескольких малых моделей (связь с [F1] Belcak и углом 4). Доказательная база слабая: внутренние тесты, препринты.

#### [B3] TinyAgent — [V] (ключевая работа)
- **Библ.:** Erdogan L.E., Lee N., Jha S., Kim S., Tabrizi R., Moon S., Hooper C., Anumanchipalli G., Keutzer K., Gholami A. *TinyAgent: Function Calling at the Edge* // EMNLP 2024: System Demonstrations. DOI 10.18653/v1/2024.emnlp-demo.9. arXiv:2409.00608. Полный список авторов (10) сверен через arXiv API.
- **Проблема.** Агентные системы с function calling требуют облачных LLM.
- **Метод.** (1) Планирование в формате LLMCompiler (DAG вызовов). (2) Синтетический датасет: **80K train / 1K val / 1K test**, **16 функций** macOS (почта, контакты, SMS, календарь, заметки, напоминания, файлы, Zoom и т.д.), стоимость генерации около $500. (3) LoRA-дообучение моделей 1.1B и 7B. (4) **Tool RAG**: дообученный DeBERTa-v3-small как мульти-лейбл классификатор нужных инструментов. (5) 4-битная квантизация (group size 32). (6) Развёртывание на **MacBook Pro M3 через llama.cpp**, голосовой ввод через Whisper.
- **Результаты (HTML-версия).**
  - Success rate: **TinyAgent-1.1B 80.06%**, **7B 84.95%**, GPT-4-Turbo 79.08%, GPT-3.5 65.04%.
  - Tool RAG: промпт **2762 → 1397 токенов** (~2×), recall 0.998, в среднем 3.97 инструмента на запрос. Точность 1.1B выросла с 78.89% до 80.06%, 7B — с 83.09% до 84.95%.
  - Квантизация 4 бита: ~30% быстрее, модели в 4× меньше (1.1B: 2.2→0.68 ГБ; 7B: 14.5→4.37 ГБ), точность не упала (80.35% / 85.14%).
- **Польза для ВКР.** Самая близкая к ВКР работа: те же платформа (MacBook, Apple Silicon), рантайм (llama.cpp), формат (DAG-план) и открытые код, данные и модели. Её числа дают прямой baseline. Главное наблюдение для системного угла: **даже после Tool RAG промпт около 1.4 тыс. токенов**, и на устройстве prefill доминирует в TTFT (см. §5).
- **Заимствовать.** Датасет (1K test), модели TinyAgent-1.1B/7B, Tool RAG как baseline, метрику success rate по графу вызовов, набор macOS-инструментов. Код под MIT.

#### [B4] MobileLLM — [V]
- **Библ.:** Liu Z., Zhao C., Iandola F., Lai C., Tian Y., Fedorov I., et al. (12 авт.). *MobileLLM: Optimizing Sub-billion Parameter Language Models for On-Device Use Cases* // ICML 2024. arXiv:2402.14905.
- **Метод.** Архитектура для моделей <1B: глубокие и тонкие сети, разделение эмбеддингов, GQA, блочное разделение весов (MobileLLM-LS).
- **Результаты.** +2.7% / +4.3% к SOTA 125M/350M. **API calling** на синтетическом датасете (5000 train / 2500 test, ~8 ходов на диалог): MobileLLM-350M EM_intent **65.3**, EM_structure **48.8** против LLaMA-v2 7B (62.8 / 50.9). Латентность на iPhone 13 (MPS): execute 15.6 мс для 125M.
- **Польза.** Доказательство, что модели ~350M справляются с узкими API-сценариями. Метрики EM_intent / EM_structure (раздельная точность выбора функции и структуры аргументов) полезны для собственной оценки.

#### [B5] APIGen — [V]
- **Библ.:** Liu Z., Hoang T., Zhang J., Zhu M., Lan T., Kokane S., et al. (17 авт.). *APIGen: Automated Pipeline for Generating Verifiable and Diverse Function-Calling Datasets* // NeurIPS 2024 Datasets & Benchmarks. arXiv:2406.18518.
- **Метод.** 3 673 исполняемых API в 21 категории. Трёхступенчатая проверка: формат → реальное исполнение → семантическая проверка.
- **Результаты.** 7B-модель на этих данных выходит на SOTA BFCL (выше ряда GPT-4), **1B-модель выше GPT-3.5-Turbo и Claude-3 Haiku**. Выпущен датасет **xlam-function-calling-60k** (CC-BY-4.0).
- **Польза.** Готовые верифицированные данные и пример конвейера «формат → исполнение → семантика». Этот конвейер можно повторить как валидатор вызовов в рантайме.

#### [B6] xLAM — [V]
- **Библ.:** Zhang J., Lan T., Zhu M., Liu Z., Hoang T., Kokane S., et al. (22 авт.). *xLAM: A Family of Large Action Models to Empower AI Agent Systems* // NAACL 2025 (long). DOI 10.18653/v1/2025.naacl-long.578. arXiv:2409.03215.
- **Результаты (BFCL v2, HTML-версия).** xLAM-8x22b-r 87.31% (1-е место). **xLAM-7b-fc-r 80.18%** (17-е), **xLAM-1b-fc-r 75.43%** (32-е; база DeepSeek-Coder-1.3B, контекст 16k).
- **Польза.** Специализированные FC-модели 1–7B служат сильными baseline для малых моделей.

#### [B7] APIGen-MT / xLAM-2 — [V-pre]
- **Библ.:** Prabhakar A., Liu Z., Zhu M., Zhang J., Awalgaonkar T., Wang S., et al. (15 авт.). *APIGen-MT: Agentic Pipeline for Multi-Turn Data Generation via Simulated Agent-Human Interplay*. arXiv:2504.03601, 2025.
- **Суть.** Многоходовые траектории: «чертёж» задачи с эталонными действиями, затем симуляция диалога человек–агент. Семейство xLAM-2-fc-r 1B–70B превосходит GPT-4o и Claude 3.5 на τ-bench и BFCL, особенно в multi-turn. Открыто 5K траекторий.
- **Польза.** xLAM-2-1b/3b-fc-r — малые многоходовые FC-модели для экспериментов. Лицензия **CC-BY-NC-4.0**: только исследовательское использование.

#### [B8] Hammer — [V]
- **Библ.:** Lin Q., Wen M., Peng Q., Nie G., Liao J., Wang J., et al. (12 авт.). *Robust Function-Calling for On-Device Language Model via Function Masking* // ICLR 2025 (Spotlight). Препринт: *Hammer: Robust Function-Calling for On-Device Language Models via Function Masking*, arXiv:2410.04587.
- **Проблема.** Качество FC-моделей сильно скачет между бенчмарками, потому что модели «цепляются» за конвенции имён функций и параметров.
- **Метод.** **Function masking**: при обучении имена функций и параметров заменяются случайными строками, значения по умолчанию рандомизируются, и модель вынуждена читать описания. Добавлен датасет irrelevance (7 500 примеров, где нужной функции нет и правильный ответ — пустой список). Базы — Qwen2 1.5B/4B/7B.
- **Результаты (HTML-версия).** BFCL overall: Hammer-7B **83.92%** (2-е место после GPT-4 на момент статьи), 4B 76.05%, **1.5B 73.04%**. Средний F1 (функция+аргументы) по API-Bank, Seal-Tools, NexusRaven, Tool-Alpaca: 76.21 / 71.35 / 66.20.
- **Польза.** Явно нацелена на on-device. Метрика irrelevance (отказ от неподходящего вызова) важна для безопасного агента. Hammer2.1-1.5b — кандидат на baseline (CC-BY-NC-4.0).
- **Заимствовать.** Протокол кросс-бенчмарковой оценки и irrelevance-тест.

#### [B9] ToolACE — [V]
- **Библ.:** Liu W., Huang X., Zeng X., Hao X., Yu S., Li D., et al. (27 авт.). *ToolACE: Winning the Points of LLM Function Calling* // ICLR 2025 (poster). arXiv:2409.00920.
- **Метод.** Самоэволюционный синтез пула из **26 507 API** в 390 доменах, мультиагентная генерация диалогов, двухслойная проверка (правила + модель). Обучение LLaMA-3.1-8B-Instruct с LoRA r=16.
- **Результаты (BFCL v3, HTML).** ToolACE-8B overall **59.22** (GPT-4-turbo 59.49, GPT-4o 59.29); non-live AST 89.27; relevance 85.37; irrelevance 83.81. xLAM-7b-fc-r 51.45. API-Bank Call 75.94.
- **Польза.** Наглядно показывает, что **BFCL v3 overall (~59)** и **BFCL v2 overall (~80–88)** несопоставимы: при сравнении нельзя смешивать версии.

#### [B10] Small LLMs Are Weak Tool Learners (α-UMi) — [V]
- **Библ.:** Shen W., Li C., Chen H., Yan M., Quan X., Chen H., et al. (8 авт.). *Small LLMs Are Weak Tool Learners: A Multi-LLM Agent* // EMNLP 2024 (main). DOI 10.18653/v1/2024.emnlp-main.929. arXiv:2401.07324.
- **Метод.** Агент разбивается на planner, caller и summarizer, каждую роль выполняет своя малая LLM. Двухэтапное обучение: общее, затем по ролям.
- **Результаты.** Мульти-LLM схема превосходит одну LLM на нескольких tool-use бенчмарках (из аннотации).
- **Польза.** Научное обоснование декомпозиции агента на малые специализированные модели (угол 4). Системный вопрос, который статья не изучает: стоимость резидентности нескольких моделей и переключения между ними на устройстве.

#### [B11] DroidCall — [V]
- **Библ.:** Xie W., Zhang L., Wang S., Yi R., Xu M. *DroidCall: A Dataset for LLM-powered Android Intent Invocation* // Findings of EMNLP 2025, pp. 9116–9134. arXiv:2412.00402.
- **Суть.** 10K примеров вызова 24 Android-интентов как функций. Qwen2.5-3B и Gemma2-2B после дообучения приближаются к GPT-4o или превосходят его. Есть демо-приложение. Код под MIT.
- **Польза.** Пример «системного API ОС как набора функций»: интенты на Android, аналог на macOS — AppleScript/Shortcuts. Это хорошая предметная область для собственного агента.

#### [B12] AutoDroid (MobiCom 2024) и AutoDroid-V2 (MobiSys 2025) — [V]
- **Библ.:** Wen H., Li Y., Liu G., Zhao S., Yu T., Li T.J.-J., et al. (10 авт.). *AutoDroid: LLM-powered Task Automation in Android* // MobiCom 2024. arXiv:2308.15272. Wen H., Tian S., Pavlov B., Du W., Li Y., Chang G., et al. (11 авт.). *AutoDroid-V2: Boosting SLM-based GUI Agents via Code Generation* // MobiSys 2025. DOI 10.1145/3711875.3729134. arXiv:2412.18116.
- **AutoDroid.** UI-представление с учётом функциональности, «память» из автоматического исследования приложений, оптимизация запросов. Точность действий 90.9%, успех задач 71.3% (+36.4% и +39.7% к GPT-4-baseline). 158 задач.
- **AutoDroid-V2 (HTML).** UI-автоматизация переформулирована как генерация скрипта малой моделью. Успех 54.4% на DroidTask против 43.9% у AutoDroid. **Латентность −93.1%** (46.3 с против 669.2 с на Snapdragon 8 Gen 2). Входных токенов в 43.5× меньше, выходных — в 5.8×. Тестировалось также на **MacBook Pro M2 Pro**.
- **Польза.** Главный системный тезис: **число токенов, а значит prefill и decode, является главной статьёй затрат on-device агента**. Переход к «плану-программе» сокращает число LLM-вызовов. Подкрепляет углы 1 и 3.

#### [B13] AgentFlux (decoupled fine-tuning) — [V-pre]
- **Библ.:** Kadekodi R., Jin Z., Kamahori K., Gu Y., Khatiri S., Bayindirli N.H., et al. (8 авт.). *AgentFlux: Decoupled Fine-Tuning & Inference for On-Device Agentic Systems*. arXiv:2510.00229, 2025.
- **Суть.** Вызов разделён на **выбор инструмента** и **генерацию аргументов**, под каждую подзадачу свой LoRA-адаптер, который динамически подгружается при инференсе. Иерархическая оркестрация сужает набор инструментов. На MCP-Bench Qwen2.5-7B получает **+46%** к точности базовой модели.
- **Польза.** Системный элемент (динамическая загрузка адаптеров) и декомпозиция выбор/аргументы. Это сигнал для маршрутизации и раннего выхода (угол 4).

#### [B14] SimpleTool (параллельное декодирование вызова) — [V-pre]
- **Библ.:** Shi X., Wan J., Dong L., Jiang W., Liu Y., Huang Z. *SimpleTool: Parallel Decoding for Real-Time LLM Function Calling*. arXiv:2603.00030, 2026.
- **Суть.** Вызов функции содержит много низкоэнтропийных токенов (разделители, имена параметров), а аргументы слабо зависят друг от друга. Специальные токены сжимают такие последовательности в **4–6×** и позволяют генерировать имя и аргументы параллельно. E2E ускорение **3–6× (до 9.6×)**, накладные расходы параллелизации +8.2%. Модели Qwen 0.5B–14B. На Mobile Actions ST-Qwen-0.5B обходит FunctionGemma. P50 **61.2 мс** при 4B на потребительском GPU.
- **Польза.** Свежая работа, которая атакует decode-фазу вызова. Комплементарна углу 1 (prefill). Требует дообучения.

#### [B15] FunctionGemma и его расширение — [V-card] + [V-pre]
- **Источники.** Карточка `google/functiongemma-270m-it` (Hugging Face, лицензия Gemma, проверено 2026-10-05). Rezagholizadeh A., Samiee S. *Extending FunctionGemma for Practical On-Device Mobile Function Calling*. arXiv:2609.25373, 2026 (по комментарию авторов принято на EMNLP 2026; не перепроверял).
- **Числа из карточки (0-shot).** BFCL Simple 61.6, Multiple 63.5, Parallel 39.0, Parallel-Multiple 29.5, Live Simple 36.2, Live Multiple 25.7, Relevance 61.1, Irrelevance 73.7. «Mobile Actions»: 58% у базовой модели, **85%** после дообучения по рецепту Google.
- **Extending.** Датасет MobileActionsExtended (~9 500 диалогов, 15 категорий управления устройством). E2E-точность выросла с 29.3% до **76.5%**. Совместная модель даёт 82.3% на MobileActionsGoogle против 90.3% у специалиста Google.
- **Польза.** Самая маленькая (270M) специализированная FC-модель. Сильный кандидат для первого уровня каскада (угол 4) и для изучения ограничений constrained decoding на очень малых моделях (угол 2).

#### [B16] RL для tool use: ToolRL (NeurIPS 2025) и Nemotron-Research-Tool-N1 (ICLR 2026) — [V]
- **Библ.:** Qian C., Acikgoz E.C., He Q., Wang H., Chen X., Hakkani-Tür D., et al. *ToolRL: Reward is All Tool Learning Needs* // NeurIPS 2025. arXiv:2504.13958. Zhang S., Dong Y., Zhang J., Kautz J., Catanzaro B., Tao A., et al. *Nemotron-Research-Tool-N1: Exploring Tool-Using Language Models with Reinforced Reasoning* // ICLR 2026 (poster). arXiv:2505.00024.
- **Суть.** GRPO с наградами за формат и корректность вызова. ToolRL даёт +17% к базе и +15% к SFT. Tool-N1-7B/14B превосходят GPT-4o. Чистое RL не уступает связке SFT→RL.
- **Польза.** Обучающий тренд 2025–2026. Для ВКР на ноутбуке это скорее фон: RL-дообучение даже 1B-модели на 24 ГБ — рискованное направление вне ИВТ-фокуса.

### Группа C. Базовые малые модели и их FC-способности (первичные источники — тех. отчёты)

#### [C1] Apple Intelligence Foundation Language Models (2024) — [V-pre]
- **Библ.:** Gunter T., Wang Z., Wang C., Pang R., Narayanan A., Zhang A., et al. (156 авт.). *Apple Intelligence Foundation Language Models*. arXiv:2407.21075, 2024.
- **Факты (HTML).** AFM-on-device около **3B** параметров (2.58B без эмбеддингов). Квантизация «в среднем <4 бит на вес», в продакшене **3.7 bpw**, часть слоёв 2-битная. «Accuracy-recovery» LoRA-адаптеры r=16 (~10 млрд токенов на восстановление). Динамически подгружаемые LoRA-адаптеры задач (десятки МБ). BFCL приведён только для AFM-server.
- **Польза.** Промышленная архитектура on-device LLM-сервиса: одна базовая модель плюс сменные адаптеры. Пример системного решения на целевой платформе.

#### [C2] Apple Intelligence Foundation Language Models: Tech Report 2025 — [V-pre]
- **Библ.:** Li E., Larsen A.B.L., Zhang C., Zhou X., Qin J., Yap D.A., et al. (398 авт.). arXiv:2507.13575, 2025.
- **Факты (HTML).** On-device модель ~3B. **KV-cache sharing**: блок 2 (37.5% слоёв) не имеет K/V-проекций и использует KV блока 1, что даёт **−37.5% памяти KV** и примерно −37.5% TTFT. **2-битный QAT** (MMLU 67.8 → 64.4). Swift-фреймворк *Foundation Models*: **guided generation** (`@Generable`, constrained decoding) и **constrained tool calling**, то есть структурная корректность вызова гарантирована, включая графы параллельных и последовательных вызовов. Есть speculative decoding и LoRA (r=32).
- **Польза.** Индустриальное подтверждение, что **constrained decoding вызовов и экономия KV/prefill — ключевые системные механизмы on-device агента** (углы 1–2). Готовый фреймворк на Mac можно использовать как внешний референс, но веса и рантайм закрыты.

#### [C3] Qwen3 Technical Report — [V-pre]
- **Библ.:** Yang A., Li A., Yang B., Zhang B., Hui B., Zheng B., et al. (60 авт.). *Qwen3 Technical Report*. arXiv:2505.09388, 2025. Модели под Apache-2.0.
- **BFCL v3 (таблицы 17–20, HTML).** thinking / non-thinking:
  - Qwen3-0.6B: **46.4 / 44.1**
  - Qwen3-1.7B: **56.6 / 52.2**
  - Qwen3-4B: **65.9 / 57.6**
  - Qwen3-8B: **68.1 / 60.2**
  - Baselines non-thinking в тех же таблицах: Qwen2.5-1.5B-Instruct 47.8, Qwen2.5-3B-Instruct 50.4, **Gemma-3-1B-IT 16.3**, **Phi-4-mini 31.3**.
- **Польза.** Единственный найденный первичный источник с BFCL v3 для всей «лестницы» 0.6B→8B в одной методике. Удобен для экспериментов: одна архитектура, разные размеры, режим thinking как ручка компромисса латентность/точность. Локальная Qwen3-8B-Q4_K_M уже есть.

#### [C4] Gemma 3 Technical Report — [V-pre]
- **Библ.:** Gemma Team, Kamath A., Ferret J., Pathak S., Vieillard N., Merhej R., et al. *Gemma 3 Technical Report*. arXiv:2503.19786, 2025.
- **Факты.** 1B–27B. Увеличено отношение локального внимания к глобальному с коротким окном, чтобы **сократить KV-кэш** на длинном контексте. Контекст ≥128K. Gemma3-4B-IT сравнима с Gemma2-27B-IT.
- **Польза.** База для FunctionGemma-270M [B15]. При этом сама Gemma-3-1B слаба в FC: 16.3 BFCL v3 по [C3].

#### [C5] Llama 3 Herd и карточка Llama 3.2 — [V-pre] + [V-card]
- **Библ.:** Grattafiori A., Dubey A., Jauhri A., et al. (561 авт.). *The Llama 3 Herd of Models*. arXiv:2407.21783, 2024. Карточка `meta-llama/Llama-3.2-3B-Instruct` (лицензия Llama 3.2 Community).
- **Числа из карточки (BFCL V2, acc, bf16).** Llama-3.2-1B **25.7**, Llama-3.2-3B **67.0**. Nexus: 13.5 / 34.3.
- **Польза.** Широко поддерживаемый в llama.cpp нативный формат tool calling. Резкий провал 1B против 3B показывает порог размера.

#### [C6] Прочие малые базовые модели (кратко) — [V-pre]
- **Phi-4-Mini** (Microsoft: Abouelenin A., Ashfaq A., Atkinson A., Awadalla H., et al.). arXiv:2503.01743, 2025. 3.8B, словарь 200K, GQA. По [C3] BFCL v3 31.3.
- **SmolLM2** (Ben Allal L., Lozhkov A., Bakouch E., et al.). arXiv:2502.02737, 2025. 1.7B, ~11T токенов.
- **LFM2** (Amini A., Banaszak A., Benoit H., et al., 33 авт.). arXiv:2511.23404, 2025. Гибрид из коротких свёрток и малого числа GQA-блоков, найден hardware-in-the-loop поиском архитектуры под edge. **До 2× быстрее prefill/decode на CPU**, 350M–8.3B, пакеты для llama.cpp/ExecuTorch. Есть вариант LFM2-1.2B-Tool (лицензия «other», LFM Open License).
- **Польза.** Кандидаты для расширения набора моделей. LFM2 интересна системно: неклассическая архитектура с малым KV-кэшем меняет баланс prefill/decode (см. «LinearKV» ниже как тренд).

### Группа D. Бенчмарки и эмпирика

#### [D1] BFCL — [V]
- **Библ.:** Patil S.G., Mao H., Yan F., Ji C.C.-J., Suresh V., Stoica I., Gonzalez J.E. *The Berkeley Function Calling Leaderboard (BFCL): From Tool Use to Agentic Evaluation of Large Language Models* // ICML 2025, PMLR 267:48371–48392. https://proceedings.mlr.press/v267/patil25a.html
- **Суть.** Оценка последовательных и параллельных вызовов, нескольких языков, многоходовых stateful-сценариев и **воздержания** (irrelevance). Масштабируемая **AST-проверка** вызова без исполнения. Категории: simple / multiple / parallel / parallel-multiple / relevance / irrelevance, live и non-live, multi-turn.
- **Вывод авторов.** Одиночные вызовы SOTA-модели делают хорошо, а память, динамические решения и длинный горизонт остаются нерешёнными.
- **Польза.** **Основной бенчмарк точности для ВКР.** Харнесс открыт (Apache-2.0) и умеет оценивать локальные модели через OpenAI-совместимый endpoint, например llama-server.
- **Внимание.** Версии v1/v2/v3/v4 по «overall» несопоставимы. Сравнивать можно только внутри одной версии и только по одинаковым категориям.

#### [D2] API-Bank — [V]
- **Библ.:** Li M., Zhao Y., Yu B., Song F., Li H., Yu H., et al. (9 авт.). *API-Bank: A Comprehensive Benchmark for Tool-Augmented LLMs* // EMNLP 2023. arXiv:2304.08244.
- **Суть.** 73 исполняемых API, 314 диалогов, 753 вызова. Уровни Call / Retrieve+Call / Plan.
- **Польза.** Дополнительный исполняемый бенчмарк. Используется в Hammer и ToolACE.

#### [D3] τ-bench — [V]
- **Библ.:** Yao S., Shinn N., Razavi P., Narasimhan K. *τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains* // ICLR 2025. arXiv:2406.12045.
- **Суть.** Диалог агента с симулированным пользователем, домены retail и airline. Проверка по конечному состоянию БД. Метрика надёжности **pass^k**. У gpt-4o меньше 50% успеха, pass^8 ниже 25% в retail.
- **Польза.** Метрика pass^k (воспроизводимость) полезна для агента на устройстве. Сам τ-bench требует LLM-симулятора пользователя, поэтому для малых локальных моделей тяжёл. Использовать опционально.

#### [D4] When2Call — [V]
- **Библ.:** Ross H., Mahabaleshwarkar A.S., Suhara Y. *When2Call: When (not) to Call Tools* // NAACL 2025 (long). DOI 10.18653/v1/2025.naacl-long.174. arXiv:2504.18851.
- **Суть.** Решение «вызывать / уточнить / отказаться / ответить самому» оформлено как multiple-choice: 3 652 тест-примера плюс 300 для LLM-judge. Даже SOTA-модели оставляют большой запас. Preference-обучение даёт больше, чем SFT. Лицензия Apache-2.0.
- **Польза.** Дешёвая (multiple-choice, через logprob) метрика «дисциплины вызовов». Подходит для проверки, не ломают ли системные оптимизации (кэш, грамматика) решение «не вызывать».

#### [D5] Small Models, Big Tasks — [V]
- **Библ.:** Kavathekar I., Donakanti R., Kumaraguru P., Vaidhyanathan K. *Small Models, Big Tasks: An Exploratory Empirical Study on Small Language Models for Function Calling* // EASE 2025 (AI Models and Data Evaluation track). DOI 10.1145/3756681.3757001. arXiv:2504.19277.
- **Суть.** Zero-shot, few-shot и fine-tuning SLM для FC, тест на edge-устройстве (латентность, память). Главный вывод: **SLM систематически плохо держат формат вывода**. Prompt-injection снижает качество незначительно.
- **Польза.** Эмпирическое обоснование для constrained decoding (угол 2).

#### [D6] TinyLLM: SLM для агентных задач на edge — [V-pre]
- **Библ.:** Haque M.A., Rahman F., Gupta K.D., Shujaee K., George R. *TinyLLM: Evaluation and Optimization of Small Language Models for Agentic Tasks on Edge Devices*. arXiv:2511.22138, 2025.
- **Суть.** BFCL (simple … relevance, live/non-live, multi-turn) для TinyAgent, TinyLlama, Qwen, xLAM. SFT / PEFT / RL / DPO и гибриды. Модели 1–3B значительно лучше <1B: до **65.74% overall** и **55.62% multi-turn** при гибридной оптимизации.
- **Польза.** Свежая сводка по SLM на BFCL. Уровень венью низкий, поэтому использовать как вторичный источник.

#### [D7] Beyond Fluent Generation: CPU reliability benchmark (MCP-style) — [V-pre]
- **Библ.:** (авторская строка arXiv: «Abrar Shahriar Qurat-Ul-Ain Mastoi»). *Beyond Fluent Generation: A CPU Reliability Benchmark for MCP-Style Tool Calling in Sub-2B Small Language Models for Edge Deployment*. arXiv:2609.07370, 2026.
- **Числа (аннотация).** 100 промптов, 5 моделей <2B на CPU. Qwen2.5-1.5B: 75% (greedy) / 79% (sampling). Qwen2.5-0.5B: 72% / **32%** при сэмплировании. Phi-1.5: 0%. **Только 5 из 1000 сырых ответов парсятся как JSON напрямую**, всё остальное держится на парсере-восстановителе. Qwen2.5-1.5B требует 7 960 MiB и 30.8 с в среднем на CPU.
- **Польза.** Яркая иллюстрация «формат — главная беда SLM». Работа слабая (маленькая выборка, один автор), цитировать только как иллюстрацию.

#### [D8] MELTing point — [V]
- **Библ.:** Laskaridis S., Katevas K., Minto L., Haddadi H. *MELTing Point: Mobile Evaluation of Language Transformers* // MobiCom 2024. DOI 10.1145/3636534.3690668. arXiv:2403.12844.
- **Суть.** Инфраструктура MELT для headless-бенчмаркинга LLM на Android, iOS и Jetson: латентность, память, энергия, термика. Выводы: инференс LLM **memory-bound**; квантизация снижает память ценой точности; непрерывная работа ограничена энергией и нагревом.
- **Польза.** Методология измерений на устройстве (угол 5).

#### [D9] JSONSchemaBench — [V-pre]
- **Библ.:** Geng S., Cooper H., Moskal M., Jenkins S., Berman J., Ranchin N., et al. (9 авт.). *JSONSchemaBench: A Rigorous Benchmark of Structured Outputs for Language Models*. arXiv:2501.10868, 2025.
- **Суть.** 10K реальных JSON-схем. Сравнение Guidance, Outlines, **llama.cpp**, XGrammar, OpenAI, Gemini по эффективности, покрытию и качеству.
- **Польза.** Готовая методика и набор схем для угла 2. В репозитории есть MaskBench для времени маски.

#### [D10] Constrained decoding в малых LLM: структурная против семантической корректности — [V-pre]
- **Библ.:** Chavan A. *Constrained Decoding Eliminates Structural Failures in Small LLMs but Reveals a Scale-Dependent Semantic Gap*. arXiv:2609.23742, 2026.
- **Числа (аннотация).** Пять моделей 0.6–4B, 14 задач, режимы native / Outlines / XGrammar. CD поднимает валидность по схеме с **78.6–92.9% до 100%**, но семантические ошибки (например, многошаговый FC) CD не исправляет.
- **Польза.** Прямо формулирует гипотезу для угла 2: двухосевая метрика «структура × семантика». Работа одного автора, малая; использовать с оговоркой.

#### [D11] Let Me Speak Freely? — [V]
- **Библ.:** Tam Z.R., Wu C.-K., Tsai Y.-L., Lin C.-Y., Lee H., Chen Y.-N. *Let Me Speak Freely? A Study on the Impact of Format Restrictions on Performance of Large Language Models* // EMNLP 2024 Industry Track. DOI 10.18653/v1/2024.emnlp-industry.91. arXiv:2408.02442.
- **Суть.** Жёсткие форматные ограничения (JSON/XML) **ухудшают рассуждение**, и чем строже формат, тем сильнее деградация.
- **Польза.** Контраргумент для угла 2. Надо проверить, верно ли это для FC малых моделей и для «ленивой» грамматики, которая включается только после триггер-тега вызова.

### Группа E. Системные механизмы агентного цикла (edge и серверные предшественники)

#### [E1] Less is More: выбор инструментов на edge — [V]
- **Библ.:** Paramanayakam V., Karatzas A., Anagnostopoulos I., Stamoulis D. *Less is More: Optimizing Function Calling for LLM Execution on Edge Devices* // DATE 2025. DOI 10.23919/DATE64628.2025.10992798. arXiv:2411.15399.
- **Метод.** Без дообучения. «Recommender»-LLM описывает идеальный инструмент, не видя реальных API. Описание эмбеддится (MPNet) и ищется через k-NN (FAISS, k=3/5) по уровням: инструменты → кластеры → все.
- **Установка.** **Jetson AGX Orin, Ollama**, квантования q4_0/q4_1/q4_K_M/q8_0, модели Hermes2-Pro-8B, Llama3.1-8B, Mistral, Phi3, Qwen2-1.5B/7B. BFCL-подвыборка (230 запросов, 51 функция) и GeoEngine.
- **Результаты.** Время выполнения **−70…−80%**, мощность **−27…−45%** при росте success rate (например, Hermes2-Pro около 71%).
- **Польза.** Прямой edge-baseline для угла 1: сокращение набора инструментов. Метрики (время, мощность, success) совпадают с нужными. Слабое место — каждый новый набор инструментов ломает префиксный кэш, и этим пробелом занимается угол 1.

#### [E2] CarbonCall — [V-pre]
- **Библ.:** те же авторы, что [E1]. *CarbonCall: Sustainability-Aware Function Calling for Large Language Models on Edge Devices*. arXiv:2504.20348, 2025.
- **Суть.** Динамический выбор инструментов, переключение вариантов квантованной модели под порог мощности и прогноз углеродной интенсивности (Jetson AGX Orin). Выбросы −52%, мощность −30%, время −30%.
- **Польза.** Пример адаптивного контроллера «модель-вариант под бюджет», то есть зрелой версии того, что пытался сделать легаси-агент (часть B), только для LLM.

#### [E3] LLM as a System Service (LLMS) — [V-pre]
- **Библ.:** Yin W., Xu M., Li Y., Liu X. *LLM as a System Service on Mobile Devices*. arXiv:2403.11805, 2024 (технический отчёт).
- **Метод.** LLM как stateful системный сервис ОС с персистентным KV-кэшем на контекст приложения. Покусочное (chunk) сжатие KV с учётом толерантности к сжатию, конвейер «IO + пересчёт» при подкачке, вытеснение LCTRU.
- **Установка и результаты (HTML).** Jetson Orin NX/TX2, смартфон Mi14. Llama2-7B и OPT-6.7B, 4-битные веса и 8-битный KV. Переключение контекстов ускорено **до 2 порядков**, до 20× (в среднем 9.7×) против vLLM-подобного chunk-INT8. При ограничении 10 мс поддерживается в 2–3× больше активных контекстов.
- **Польза для ИВТ.** Это задача уровня ОС: управление памятью KV между агентами и приложениями под бюджет. Прямо перекликается с планировщиком памяти из легаси (жизненные циклы и вытеснение) и с углом 1 (кэш KV схем инструментов как ресурс ОС).

#### [E4] Elastic On-Device LLM Service (ELMS) — [V]
- **Библ.:** Yin W., Yi R., Xu D., Huang G., Xu M., Liu X. *Elastic On-Device LLM Service* // MobiCom 2025. DOI 10.1145/3680207.3765259. arXiv:2409.09071.
- **Суть.** Эластичность и по модели (переупорядочивание нейронов), и по промпту (dual-head TLM для сжатия промпта) под разные требования к латентности приложений. До +14.83% абсолютной точности, переключение <1% TTFT.
- **Польза.** Рецензируемое продолжение LLMS, показывает уровень системных работ MobiCom по on-device LLM.

#### [E5] Agent.xpu — [V-pre]
- **Библ.:** Wei X., Zhang J., Li H., Chen J., Guan H., Qu R., et al. (9 авт.). *Agent.xpu: Efficient Scheduling of Agentic LLM Workloads on Heterogeneous SoC*. arXiv:2506.24045, 2025.
- **Метод.** Персональный агент порождает **реактивные** (пользовательские) и **проактивные** (фоновые) LLM-потоки. Гетерогенный граф исполнения с аффинностью операторов к NPU/iGPU, разнесение prefill/decode по ускорителям, вытеснение с «slack-aware piggybacking».
- **Установка и результаты (HTML).** Intel Core Ultra 5 125H (Arc iGPU + NPU), Llama-3.1-8B и 3.2-3B (W8A16). Baselines: llama.cpp, OpenVINO, serial NPU-iGPU. Пропускная способность проактивных задач 1.2–4.9×, **латентность реактивных −91…−97%**, энергия −26.8%.
- **Польза.** Наиболее «ИВТ-шная» постановка агента на SoC: планирование, приоритеты, вытеснение. На Apple Silicon её не исследовали, хотя есть CPU/GPU/ANE и общая память. Источник идей для угла 4 и для расширения углов 1 и 3 (приоритетная очередь запросов).

#### [E6] llm.npu — [V]
- **Библ.:** Xu D., Zhang H., Yang L., Liu R., Huang G., Xu M., Liu X. *Fast On-device LLM Inference with NPUs* // ASPLOS 2025. DOI 10.1145/3669940.3707239. arXiv:2407.05858.
- **Суть.** Prefill длинных промптов (например, понимание UI) — узкое место on-device агентов. Решение: выгрузка на NPU с чанкингом промпта, выделением выбросов на CPU/GPU и out-of-order планированием блоков. Prefill в **22.4×** быстрее, энергия **−30.7×**, до 32.8× E2E. Впервые >1000 ток/с prefill для модели ~1B.
- **Польза.** Независимое подтверждение тезиса «**prefill — бутылочное горлышко агентов на устройстве**». На Mac ANE из llama.cpp недоступен, поэтому эта работа здесь мотивационная, а не методическая.

#### [E7] Prompt Cache — [V]
- **Библ.:** Gim I., Chen G., Lee S., Sarda N., Khandelwal A., Zhong L. *Prompt Cache: Modular Attention Reuse for Low-Latency Inference* // MLSys 2024. arXiv:2311.04934.
- **Метод.** Схема «промпт-модулей» (системные сообщения, шаблоны, документы). Attention-состояния модулей предвычисляются, позиции резервируются схемой, модули переиспользуются в произвольных промптах.
- **Результаты.** TTFT сокращается в **8× на GPU** и в **60× на CPU** при сохранении точности (из аннотации).
- **Польза.** Прямой прототип для угла 1: описание инструмента рассматривается как промпт-модуль. Особенно актуально, потому что на CPU и на слабом edge-ускорителе выигрыш больше.

#### [E8] EPIC (ICML 2025) и CacheBlend (EuroSys 2025) — position-independent caching — [V]
- **Библ.:** Hu J., Huang W., Wang W., et al. (10 авт.). *EPIC: Efficient Position-Independent Caching for Serving Large Language Models* // ICML 2025. arXiv:2410.15332. Yao J., Li H., Liu Y., Ray S., Cheng Y., Zhang Q., et al. (9 авт.). *CacheBlend: Fast Large Language Model Serving for RAG with Cached Knowledge Fusion* // EuroSys 2025. DOI 10.1145/3689031.3696098 (расширение — ACM TOCS 2026). arXiv:2405.16444.
- **Суть.** Чанки контекста (RAG) встречаются после разных префиксов, поэтому их KV нельзя переиспользовать «как есть»: теряется cross-attention. CacheBlend пересчитывает около 15% выбранных токенов. EPIC (AttnLink и KVSplit) использует статическую разреженность внимания и пересчитывает минимум.
- **Польза.** **Training-free** методы, которые можно перенести с RAG-чанков на **схемы инструментов** и с серверного GPU на устройство (угол 1).

#### [E9] ReCache — [V-pre] (самая близкая работа к углу 1)
- **Библ.:** Fang Y., Wei S., Hu H., Shen X. *ReCache: Efficient KV Cache Reuse and Compression for Tool-Augmented LLM Agents*. arXiv:2608.19662, 2026.
- **Суть.** Схемы инструментов и навыков повторяются между запросами **в разных комбинациях и порядках**, поэтому обычный префиксный кэш их не переиспользует. Решение: «resource-wise attention» без взаимодействия между ресурсами и с локальными позициями (KV-блоки инвариантны к композиции), маршрутизация видимости по слоям и KV-головам, прунинг полей схем.
- **Результаты.** Inv-F1 82.3% против 82.4% у плотного варианта. **TTFT в 3.655× быстрее**, память KV **−92.43%**, внимание в 1.423× быстрее.
- **Ограничения (HTML, проверено).** **Требует full fine-tuning** (Qwen3-4B на A800; вспомогательно Qwen3-1.7B на RTX 5000). Авторы пишут, что адаптация к frozen-моделям — future work. **On-device оценки нет.**
- **Польза.** Подтверждает актуальность проблемы и **очерчивает пробел** для угла 1: training-free вариант для замороженных SLM на устройстве с ограниченной памятью.

#### [E10] SGLang — [V]
- **Библ.:** Zheng L., Yin L., Xie Z., Sun C., Huang J., Yu C.H., et al. (12 авт.). *SGLang: Efficient Execution of Structured Language Model Programs* // NeurIPS 2024. arXiv:2312.07104.
- **Суть.** RadixAttention (префиксное дерево KV для переиспользования) и сжатые FSM для структурного вывода. До 6.4× пропускной способности на агентных задачах, JSON и прочих.
- **Польза.** Эталон «точного» переиспользования префиксов (radix-tree). Baseline-идея для угла 1: канонический порядок инструментов плюс префиксное дерево.

#### [E11] XGrammar — [V]
- **Библ.:** Dong Y., Ruan C.F., Cai Y., Lai R., Xu Z., Zhao Y., Chen T. *XGrammar: Flexible and Efficient Structured Generation Engine for Large Language Models* // MLSys 2025. arXiv:2411.15100.
- **Метод.** Словарь делится на контекстно-независимые токены (их маски предвычисляются) и контекстно-зависимые (проверяются в рантайме). Для Llama-3.1 и JSON-грамматики зависимых **<1% (1134 из 128K)**, а расширение контекста сокращает их до около 120. Персистентный стек, перекрытие маски с GPU-вычислениями.
- **Результаты (HTML).** **<40 мкс/токен** для JSON Schema и JSON-CFG, до **100×** быстрее существующих решений (Outlines, **llama.cpp b3998**, lm-format-enforcer). TPOT-оверхед почти нулевой. Интегрирован в SGLang, MLC-LLM и WebLLM. Код под Apache-2.0.
- **Польза.** Главный системный референс для угла 2. Замеры против llama.cpp сделаны на старой версии (b3998), и повторить их на b11146 и Apple Silicon — честная задача.

#### [E12] XGrammar-2 — [V-pre]
- **Библ.:** Li L., Dong Y., Wang G., Xu Z., Jiang A., Chen T. *XGrammar-2: Dynamic and Efficient Structured Generation Engine for Agentic LLMs*. arXiv:2601.04426, 2026 (комментарий: ACM CAIS 2026).
- **Суть.** Агентные структуры **динамические**: tool calling включается по тегу, и набор схем меняется от запроса к запросу. TagDispatch и Cross-Grammar Cache (переиспользование подструктур между грамматиками), Earley-маски, JIT. Компиляция в >6× быстрее.
- **Польза.** Близкая аналогия к углу 1 («кэш между запросами с разными наборами инструментов»), только на уровне грамматики, а не KV. Можно объединить оба кэша в одном рантайме.

#### [E13] DOMINO — [V]
- **Библ.:** Beurer-Kellner L., Fischer M., Vechev M. *Guiding LLMs The Right Way: Fast, Non-Invasive Constrained Generation* // ICML 2024. arXiv:2403.06988.
- **Суть.** Некорректное выравнивание ограничений с субтокенами **ухудшает точность**. DOMINO выравнивает ограничения с субтокенами, использует предвычисление и спекуляцию: оверхед почти нулевой, иногда ~2× ускорение.
- **Польза.** Теоретическое обоснование, почему «наивная» грамматика может портить вызовы. Это гипотеза, которую стоит проверить в угле 2.

#### [E14] AsyncLM — [V-pre]
- **Библ.:** Gim I., Lee S., Zhong L. *Asynchronous LLM Function Calling*. arXiv:2412.07017, 2024.
- **Метод.** Вызовы функций не блокируют генерацию. Когда функция завершается, executor инжектирует в поток токенов **прерывание** ([CALL]/[INTR]/[TRAP]… в «Context Markup Language»). Есть критические секции. Модель дообучается под семантику прерываний.
- **Результаты (HTML).** На BFCL E2E латентность в **1.6–5.4×** ниже синхронной. Llama-3.2-1B/3B на **RTX 4090**, GPT-4o через промптинг.
- **Польза.** Аналогия с прерываниями ОС естественна для ИВТ (угол 3). Edge не исследован.

#### [E15] AsyncFC — [V-pre]
- **Библ.:** Feng G., Mao H., Dutta P., Gonzalez J.E. *Concurrency without Model Changes: Future-based Asynchronous Function Calling for LLMs*. arXiv:2605.15077, 2026 (на OpenReview значится как подача на ICLR 2027).
- **Суть.** Чисто исполнительный слой: **futures** для незавершённых результатов, перекрытие decode и исполнения, параллелизм между функциями, без дообучения и без изменения протокола. Авторы показывают, что LLM умеют рассуждать над символическими futures.
- **Польза.** Training-free вариант AsyncLM. Хорошо ложится на C++-исполнитель (угол 3).

#### [E16] Conveyor — [V-pre]
- **Библ.:** Xu Y., Kong X., Chen T., Zhuo D. *Conveyor: Efficient Tool-aware LLM Serving with Tool Partial Execution*. arXiv:2406.00059, 2024.
- **Суть.** Инструмент запускается **частично**, пока LLM ещё декодирует его аргументы. Нужен интерфейс для разработчика инструмента. Латентность до −38.8%.
- **Польза.** Идея потокового старта инструмента для угла 3.

#### [E17] Спекуляция действий и инструментов — [V] / [V-pre]
- **Speculative Actions.** Ye N., Ahuja A., Liargkovas G., Lu Y., Kaffes K., Peng T. ICLR 2026 (Oral), arXiv:2510.04371. Быстрая модель предсказывает следующее действие: до 55% точности предсказания и до 20% снижения латентности. Есть анализ «ширины спекуляции против стоимости».
- **Speculative Interaction Agents.** Hooper C., Kang M., Moon S., Lee N., Wen E., Wawrzynek J., et al. (Berkeley, та же группа, что TinyAgent и LLMCompiler). arXiv:2605.13360, 2026. Асинхронный ввод-вывод и спекулятивные вызовы: 1.3–1.7× для облачных моделей и **1.6–2.2× для Qwen2.5-3B и Llama-3.2-3B** (edge-масштаб, с дообучением).
- **PASTE.** Sui Y., Zhao H., Ma R., He Z., Wang H., Li J., et al. *Parallelizing Tool Execution and LLM Generation for Low-Latency Agent Serving*. arXiv:2603.18897, 2026 (ранний заголовок: «Act While Thinking…»). Предсказание вызовов по повторяющимся паттернам: −43.5% времени задачи.
- **Optimizing Agentic LM Inference via Speculative Tool Calls.** Nichols D., Singhania P., Jekel C., Bhatele A., Menon H. arXiv:2512.15834, 2025. Серверный vLLM, предложен «tool cache» API.
- **Польза.** Тренд 2025–2026: агентный цикл ускоряют методами из микроархитектуры (спекуляция, out-of-order). На устройстве с одним GPU и общей памятью спекуляция конкурирует с основной генерацией за ресурсы, и этот вопрос не изучен (угол 3).

#### [E18] lm-Meter — [V-pre]
- **Библ.:** Wang H., Tu X., Ke H., Chai H., Chen D., Han K., et al. *lm-Meter: Unveiling Runtime Inference Latency for On-Device Language Models* // ACM/IEEE SEC 2025 (по комментарию arXiv). arXiv:2510.06126.
- **Суть.** Онлайн-профайлер фаз (embedding / prefill / decode / softmax / sampling) и ядер на мобильных платформах. Оверхед 2.58% в prefill и 0.99% в decode при Powersave.
- **Польза.** Методика фазового профилирования для угла 5. Код под MIT, но для Android.

### Группа F. Позиционные работы и обзоры

#### [F1] Small Language Models are the Future of Agentic AI — [V-pre]
- **Библ.:** Belcak P., Heinrich G., Diao S., Fu Y., Dong X., Muralidharan S., Lin Y.C., Molchanov P. (NVIDIA). arXiv:2506.02153 (v3), 2025.
- **Тезисы (HTML).** V1: SLM достаточно мощны. V2: операционно лучше подходят агентам. V3: экономичнее (обслуживание 7B в **10–30×** дешевле, чем 70–175B). Аргументы A1–A7: узкая функциональность агентов, нужда в строгом формате, естественная гетерогенность, данные из логов. Алгоритм конверсии LLM→SLM S1–S6: сбор вызовов → очистка → кластеризация задач → выбор SLM → PEFT → итерации. Оценка заменимых вызовов: MetaGPT ~60%, Open Operator ~40%, Cradle ~70%. Определение SLM: модель, которая помещается на потребительское устройство и даёт практичную латентность для одного пользователя.
- **Польза.** Удобная «рамка» для введения ВКР: почему агент на устройстве и почему малая модель. Это позиционная статья, а не эмпирическое доказательство.

#### [F2] On-Device Language Models: A Comprehensive Review — [V-pre]
- **Библ.:** Xu J., Li Z., Chen W., Wang Q., Gao X., Cai Q., et al. (7 авт.). arXiv:2409.00088, 2024.
- **Польза.** Обзор архитектур, сжатия, аппаратного ускорения и edge-cloud. Есть awesome-список. Конфликт интересов (Nexa AI) учитывать.

#### [F3] Mobile Edge Intelligence for LLMs: A Contemporary Survey — [V]
- **Библ.:** Qu G., Chen Q., Wei W., Lin Z., Chen X., Huang K. // IEEE Communications Surveys & Tutorials, 2025. DOI 10.1109/COMST.2025.3527641. arXiv:2407.18921.
- **Польза.** Сетевой взгляд: кэширование и доставка моделей, обучение и инференс на edge-серверах. Для ноутбучной ВКР в основном справочный.

#### [F4] Small Language Models: Survey, Measurements, and Insights — [V-pre]
- **Библ.:** Lu Z., Li X., Cai D., Yi R., Liu F., Zhang X., et al. (8 авт.). arXiv:2409.15790, 2024.
- **Суть.** 70 SLM (100M–5B), бенчмарк латентности и памяти на устройствах.
- **Польза.** Справочные данные о затратах SLM.

#### [F5] Tool Learning with LLMs: A Survey — [V]
- **Библ.:** Qu C., Dai S., Wei X., Cai H., Wang S., Yin D., et al. (8 авт.) // Frontiers of Computer Science, 2025. DOI 10.1007/s11704-024-40678-2. arXiv:2405.17935.
- **Польза.** Таксономия tool learning (planning → selection → calling → response) для обзорной главы.

---

## 3. Дополнительные проверенные работы (кратко, для обзорной главы)

| Работа | Статус и venue | Чем полезна |
|---|---|---|
| Granite-Function Calling Model (Abdelaziz I., Basu K., Agarwal M., et al.), arXiv:2407.00121 | [V] EMNLP 2024 Industry, DOI 10.18653/v1/2024.emnlp-industry.85 | Многозадачная декомпозиция FC на 7 подзадач (вложенные вызовы, цепочки, параллельные, детекция имени, пар параметр–значение…). Готовая таксономия ошибок |
| ToolGen (Wang R., Han X., Ji L., et al.), arXiv:2410.03439 | [V] ICLR 2025 (по комментарию arXiv) | Каждый инструмент как токен (47K инструментов), retrieval и вызов как генерация. Развитие идеи Octopus |
| NexusRaven, NeurIPS 2023 workshops (Instruction; FMDM) | [~] venue проверен на OpenReview, авторы не сверены | Ранняя открытая FC-модель 13B, не on-device |
| PowerInfer-2 (Xue Z., Song Y., Mi Z., Zheng X., Xia Y., Chen H.), arXiv:2406.06282 | [V-pre] | 47B на смартфоне (11.68 ток/с) за счёт кластеров нейронов NPU/CPU и I/O-конвейера. Смежная область (инференс) |
| LLM in a flash (Alizadeh K., Mirzadeh I., et al., Apple), arXiv:2312.11514 | [V] ACL 2024 (по комментарию arXiv) | Инференс моделей больше DRAM с флеш-памяти. Смежная область |
| Speculative Tool Calls (Nichols et al.), arXiv:2512.15834 | [V-pre] | Серверная спекуляция вызовов и «tool cache» endpoint |
| What Limits Agentic Systems Efficiency? (Bian S., Yan M., Jayarajan A., Pekhimenko G., Venkataraman S.), arXiv:2510.16276 | [V-pre] | Декомпозиция E2E агента: до 53.7% латентности приходится на среду (web). SpecCache. Методика декомпозиции латентности |
| Don't Break the Cache (Lumer E., et al.), arXiv:2601.06007 | [V-pre] | Изменение набора инструментов инвалидирует префиксный кэш у облачных провайдеров. Мотивация угла 1 |
| Learning Agent Execution for KV-Cache Management in Agentic Serving (Zhang R., Kim C., Feng S., Du K., Liu Y., et al.), arXiv:2608.14624 | [V-pre] | «Agent anchor» (system + инструменты + навыки) как переиспользуемый префикс. Серверная постановка |
| Are We There Yet? (Yan X., Ding Y.), arXiv:2504.00002 | [V-pre] | Измерения на мобильных: <4B моделей запускаются, ответ с осмысленным выводом >30 с против <10 с в облаке |
| When Correct Isn't Usable (Galeone C., Park M., Ettorre G., Ligorio D.), arXiv:2605.02363 | [V-pre] | Constrained decoding даёт **3.6–8.2× латентности** в их установке (7–9B) и иногда портит точность. Контрпример к «нулевому оверхеду» |
| Characterizing Energy Footprint of SLM on Edges (Islam M.R., Deng B., et al.), arXiv:2511.11624 | [V-pre], IEEE MASS 2025 (по комментарию) | Энергетика SLM на edge-устройствах |

---

## 4. Сводка по малым моделям для function calling (только первичные числа)

> **Предупреждение.** Числа ниже нельзя напрямую сравнивать между строками с разными версиями BFCL и разными метриками. Это ориентиры для выбора моделей, а не таблица для ВКР. В ВКР все модели надо прогнать самостоятельно одним харнессом и в одной квантизации.

| Модель | Размер | Число | Источник | Лицензия весов |
|---|---|---|---|---|
| FunctionGemma-270M-it | 0.27B | BFCL Simple 61.6 / Multiple 63.5 / Parallel 39.0 / Irrelevance 73.7 (0-shot) | карточка HF | Gemma ToU |
| Qwen3-0.6B | 0.6B | BFCL v3: 46.4 (think) / 44.1 (no-think) | [C3] | Apache-2.0 |
| Qwen3-1.7B | 1.7B | BFCL v3: 56.6 / 52.2 | [C3] | Apache-2.0 |
| Qwen3-4B | 4B | BFCL v3: 65.9 / 57.6 | [C3] | Apache-2.0 |
| Qwen3-8B (есть локально, Q4_K_M) | 8B | BFCL v3: 68.1 / 60.2 | [C3] | Apache-2.0 |
| Qwen2.5-1.5B / 3B-Instruct | 1.5 / 3B | BFCL v3: 47.8 / 50.4 | [C3] | Apache-2.0 / «other» (Qwen Research) у 3B |
| Gemma-3-1B-IT | 1B | BFCL v3: 16.3 | [C3] | Gemma ToU |
| Phi-4-mini | 3.8B | BFCL v3: 31.3 | [C3] | MIT |
| Llama-3.2-1B / 3B-Instruct | 1 / 3B | BFCL v2: 25.7 / 67.0 | карточка HF | Llama 3.2 Community |
| xLAM-1b-fc-r / 7b-fc-r | 1.35 / 6.9B | BFCL v2: 75.43 / 80.18 | [B6] | CC-BY-NC-4.0 |
| Hammer-1.5B / 7B | 1.5 / 7B | BFCL (версия на 10.2024): 73.04 / 83.92 | [B8] | CC-BY-NC-4.0 (Hammer2.1-1.5b) |
| ToolACE-8B | 8B | BFCL v3 overall 59.22 | [B9] | см. карточку |
| TinyAgent-1.1B / 7B | 1.1 / 7B | 80.06 / 84.95% success на TinyAgent-test (не BFCL) | [B3] | см. репозиторий (MIT код) |

**Чтение таблицы.** (1) Порог «полезности» для общего FC лежит около 1.5–4B. Ниже нужна специализация (FunctionGemma, xLAM-1b, Hammer-1.5B) или узкий домен (TinyAgent, Octopus). (2) Режим thinking у Qwen3 даёт +2…+8 п.п. BFCL ценой многих токенов decode. Это готовая «ручка» латентность/точность для системных экспериментов.

---

## 5. Контрольный замер на целевой машине (M4 Pro, 2026-10-05)

`llama-bench` (llama.cpp build 11146, backend BLAS+Metal, 8 потоков, 3 повтора), модель `Qwen3-8B-Q4_K_M.gguf` (4.68 GiB):

| тест | ток/с |
|---|---|
| pp512 (prefill) | 367.89 ± 2.77 |
| pp2048 (prefill) | 354.34 ± 0.79 |
| tg128 (decode) | 44.68 ± 0.37 |

**Оценка (расчёт, не end-to-end замер).** Промпт TinyAgent со всеми 16 инструментами (2762 токена [B3]) даёт около **7.8 с prefill**. После Tool RAG (1397 токенов) около **3.9 с**. Генерация плана в ~100 токенов занимает около 2.2 с. Значит, для 8B-модели на этой машине **prefill схем инструментов составляет 60–80% латентности одного шага агента**. Для 0.6–1.7B prefill в разы быстрее, но доля сохраняется. Это количественное обоснование угла 1. Повторный замер нужно вписать в методику ВКР (фиксированный commit llama.cpp, питание от сети, без фоновых задач, n≥5 повторов, медиана и p95).

---

## 6. Синтез: что установлено, тренды, открытые проблемы

**Что установлено.**
1. Малые модели (0.27–4B) после специализации достигают или превосходят GPT-4-класс на **узких** наборах функций: Octopus, TinyAgent, DroidCall, FunctionGemma-Mobile Actions. На **общих** бенчмарках (BFCL v3, multi-turn) сохраняется заметный разрыв.
2. Главные режимы отказа SLM: **формат** (лечится constrained decoding), **выбор инструмента из большого набора** (лечится Tool RAG, иерархическим выбором, функциональными токенами), **решение «не вызывать»** (irrelevance, When2Call) и **многоходовость**.
3. На устройстве латентность агентного шага определяется **prefill длинного промпта со схемами инструментов** (TinyAgent, AutoDroid-V2, llm.npu, наш замер) и **последовательностью цикла** ReAct (LLMCompiler, AsyncLM).
4. Индустрия (Apple 2025) встроила в ОС guided generation, constrained tool calling, KV-sharing и адаптеры. Значит, системные механизмы агентного рантайма — это признанная инженерная проблема, а не академическая экзотика.

**Тренды 2025–2026.** RL-дообучение tool use (ToolRL, Tool-N1). Многоходовые синтетические данные (APIGen-MT). Специализированные FC-модели ≤300M (FunctionGemma). Структурно-осознанное декодирование (SimpleTool, XGrammar-2). Асинхронность и спекуляция в агентном цикле (AsyncFC, Speculative Actions, PASTE, Speculative Interaction Agents). Переиспользование KV для схем инструментов (ReCache, «Don't Break the Cache»). Агентные рантаймы на гетерогенных SoC (Agent.xpu).

**Открытые проблемы (пробелы, релевантные ИВТ).**
- **P1.** Training-free переиспользование KV схем инструментов **в произвольных подмножествах и порядках** для **замороженных** SLM **на устройстве** с ограниченной памятью. ReCache требует дообучения и GPU A800. EPIC и CacheBlend сделаны для RAG на серверах.
- **P2.** **Стоимость и эффект constrained decoding на edge для малых моделей.** Замеры XGrammar против llama.cpp устарели (b3998). Есть противоречивые данные о точности: Let Me Speak Freely и When Correct Isn't Usable против Chavan 2026 и XGrammar-2.
- **P3.** Асинхронные и спекулятивные агентные циклы оценены на RTX 4090, A100 и облачных API. На **одном ускорителе с unified memory** их выгода и конкуренция за ресурсы не изучены.
- **P4.** **Нет стандарта системной оценки on-device агента**: FC-бенчмарки меряют точность, а системные работы меряют токены в секунду. Совместные метрики (TTFT шага агента, J на задачу, память, pass^k) почти никто не публикует.
- **P5.** Влияние **квантования весов и KV** на точность tool calling систематически не изучено. Документация llama.cpp прямо предупреждает, что `-ctk q4_0` «может существенно ухудшить» tool calling.
- **P6.** Многоагентная или многоприложенческая работа на устройстве: планирование реактивных и фоновых LLM-потоков на Apple Silicon (аналог Agent.xpu) не исследовано.
- **P7.** Измерение энергии на macOS без root не имеет устоявшейся методики. Инструменты на IOReport (macmon) требуют валидации.

---

## 7. Углы ВКР (реализуемо одним студентом на M4 Pro / 24 ГБ за несколько месяцев)

Общая инфраструктура для всех углов: llama.cpp (C/C++ API и llama-server), харнесс BFCL (Apache-2.0) с локальным endpoint, тест TinyAgent (1K), When2Call (MC). Модели — лестница Qwen3 0.6B/1.7B/4B/8B (Apache-2.0), плюс Llama-3.2-1B/3B, FunctionGemma-270M, xLAM-2-1b/3b (NC, только исследование). Все прогоны идут в одной квантизации (Q4_K_M и Q8_0), фиксируются commit, seed и температура 0.

### Угол 1 (рекомендуется как ядро). «Training-free модульное кэширование KV-состояний схем инструментов для on-device агента»
- **Идея.** Описание каждого инструмента — это переиспользуемый модуль промпта. Его KV-блок предвычисляется один раз и хранится в кэше рантайма. Под запрос (после Tool RAG) собирается промпт из блоков без повторного prefill. Сравниваются три стратегии сборки:
  1. **Точная.** Канонический порядок инструментов плюс префиксное дерево (как RadixAttention), без потери точности, но с ограниченной долей попаданий.
  2. **Независимые блоки.** RoPE-сдвиг позиций (как Prompt Cache и наивный EPIC), без cross-attention между инструментами.
  3. **Блоки с выборочным пересчётом k% токенов** (CacheBlend / EPIC-AttnLink).
  
  Плюс менеджер памяти кэша под бюджет: LRU против частотной политики против политики по стоимости пересчёта. Это прямой наследник «планировщика памяти по lifetimes» из легаси.
- **Системная (low-level) часть.** C++ поверх llama.cpp C API: сохранение и восстановление KV последовательностей (`llama_state_seq_*`), копирование и сдвиг позиций в KV (`llama_memory_seq_cp/rm/add`; функции присутствуют в `llama.h` установленного b11146, семантику для RoPE-сдвига проверить экспериментально), учёт границ токенизации шаблона чата, размещение кэша в unified memory, измерение TTFT по фазам.
- **Научная новизна (честно).** Не «изобретение кэша», а **первое исследование training-free PIC для схем инструментов на frozen SLM на устройстве**. Включает компромисс «точность против TTFT против памяти» по размерам моделей 0.6–8B и по стратегиям сборки. ReCache ([E9]) это явно оставляет на будущее.
- **Baselines.** B0 полный промпт без кэша. B1 префиксный кэш llama-server (`cache_prompt`). B2 Tool RAG ([B3], [E1]) без кэша. B3 Tool RAG + префиксный кэш. Верхняя граница — oracle-набор инструментов. Опционально — Octopus-подобные функциональные токены как «инструменты в весах», если хватит времени на LoRA.
- **Метрики.** TTFT p50/p95, латентность шага и задачи, доля попаданий в кэш, память KV (МБ) и пиковый RSS. Точность: BFCL v3/v4 non-live (simple, multiple, parallel, parallel-multiple), irrelevance; success rate на TinyAgent-test; When2Call. Энергия на запрос (опционально, см. угол 5).
- **Осуществимость на M4 Pro.** Высокая. Модели до 8B Q4 помещаются с запасом, 24 ГБ хватает на кэш сотен инструментов (KV 8B Q4 около 0.1–0.15 МБ/токен в f16; уточнить замером). Всё реализуемо без GPU-кластера, обучение не требуется.
- **Риски.** (1) Независимые блоки могут заметно снижать точность. Это и есть исследуемый вопрос, а «точная» стратегия даёт безопасный результат в любом случае. (2) API llama.cpp часто меняется, поэтому нужно зафиксировать commit или форк. (3) Для моделей 0.6–1.7B абсолютный выигрыш TTFT меньше: оценивать Pareto и относительную долю. (4) Шаблоны чата (Qwen3 `<tools>`…) требуют аккуратной сборки модулей на границах токенов. (5) Свежий конкурент (ReCache) обязывает чётко позиционироваться: training-free, edge, frozen.

### Угол 2. «Constrained decoding вызовов функций на edge: стоимость маски, динамические наборы инструментов и влияние на точность малых моделей»
- **Идея.** Систематически измерить и оптимизировать ограниченную генерацию tool-call на Apple Silicon. Изучаемые оси:
  - (а) оверхед вычисления маски на токен относительно decode малых моделей, у которых decode быстрый и оверхед заметнее;
  - (б) «ленивые» грамматики по триггер-тегу;
  - (в) **кэш скомпилированных грамматик между запросами** с меняющимися наборами инструментов (аналог XGrammar-2 Cross-Grammar Cache в llama.cpp);
  - (г) двухосевая точность «структура × семантика» ([D10]).
- **Системная часть.** C++: интеграция XGrammar (Apache-2.0, C++ ядро) как альтернативного sampler-а в llama.cpp или собственный кэш масок для схем инструментов. Сравнение с GBNF llama.cpp и llguidance (опция `LLAMA_LLGUIDANCE`; заявлено около 50 мкс/токен для 128K-словаря). Микробенчмарки ядра маскирования.
- **Baselines.** Без ограничений плюс парсер-восстановитель. GBNF llama.cpp. llguidance. XGrammar. Опционально Outlines (Python).
- **Метрики.** мкс/токен маски (p50/p99), время компиляции грамматики, TPOT-оверхед %, доля валидных вызовов, BFCL AST accuracy, irrelevance, When2Call, влияние на режим thinking Qwen3.
- **Осуществимость.** Высокая, без обучения.
- **Риски.** XGrammar уже показывает около 40 мкс/токен, поэтому новизна должна быть в **edge-эмпирике и динамических наборах инструментов**, а не в новом алгоритме. Есть риск скатиться в «сравнительный бенчмарк». Этот угол хорошо сочетается с углом 1 как вторая глава («кэш грамматик + кэш KV = tool-aware рантайм»).

### Угол 3. «Асинхронный потоковый исполнитель планов вызовов функций для локального агента»
- **Идея.** Модель генерирует DAG-план (формат LLMCompiler, как у TinyAgent). Исполнитель **разбирает поток токенов инкрементально** и запускает вызов, как только его аргументы и зависимости готовы (идеи Conveyor и AsyncFC-futures). Исполнение инструментов перекрывается с decode, при ошибке план отменяется или перестраивается.
- **Системная часть.** C++-исполнитель DAG (переиспользовать `native_execution_lab` из легаси), lock-free очередь готовых задач, futures, отмена. Реальные macOS-инструменты (Spotlight `mdfind`, Calendar/Reminders через `osascript`, файловые операции), а также синтетические инструменты с заданным распределением латентности.
- **Baselines.** Последовательный ReAct. Оригинальный LLMCompiler (Python, MIT). `parallel_tool_calls` в llama-server. Синхронный DAG без стриминга.
- **Метрики.** E2E латентность задачи, доля критического пути, загрузка CPU и GPU, точность на TinyAgent-test и BFCL parallel(-multiple), число лишних или отменённых вызовов.
- **Осуществимость.** Средне-высокая. Модели TinyAgent-1.1B/7B и формат уже открыты.
- **Риски.** **Если локальные инструменты выполняются за миллисекунды, выигрыш будет мал.** Сначала нужно честно измерить латентности реальных macOS-инструментов: AppleScript-вызовы, по опыту, порядка сотен мс, но это надо проверить. Иначе результат легко «подогнать» синтетикой. Риск — повторить LLMCompiler и AsyncLM без нового вклада. Новизна в edge-условиях (один GPU, unified memory) и в стриминговом старте.

### Угол 4 (дополнительный, рискованный). «Каскад малых моделей с маршрутизацией по уверенности под бюджет латентности и памяти»
- **Идея.** Первый уровень — FunctionGemma-270M или Qwen3-0.6B. Эскалация на Qwen3-4B/8B происходит по сигналу уверенности: logprob имени функции (функциональные токены Octopus), согласованность выбора, валидность по схеме. Учитывается разделение «выбор инструмента / аргументы» (AgentFlux, α-UMi). Системная часть: одновременная резидентность моделей в 24 ГБ, общий кэш схем (связь с углом 1), приоритеты запросов (идеи Agent.xpu).
- **Baselines.** Каждая модель отдельно. Oracle-маршрутизация. Случайная маршрутизация с той же долей эскалаций.
- **Метрики.** Pareto «точность против латентности/энергии», доля эскалаций, калибровка (ECE) сигнала уверенности.
- **Риски.** **Это зрелая версия легаси-эксперимента (B), который закончился NO_ADAPTIVE_ADVANTAGE_ESTABLISHED.** Риск повторить отрицательный результат высок, потому что 8B декодирует около 45 ток/с, а основная цена в prefill. Уклон в ML сильнее, чем в системы. Брать только как дополнительную главу.

### Угол 5 (поддерживающий, обязательная методика). «Фазовое профилирование агентного шага на Apple Silicon»
- **Идея.** Инструмент, который разбивает латентность и энергию шага агента на фазы: выбор инструментов / prefill схем / decode / маска грамматики / исполнение инструмента / повтор. Аналоги — lm-Meter ([E18]) и MELT ([D8]), но для агентов и macOS. Энергия без sudo через IOReport (например, macmon, MIT). Точность такого метода нужно валидировать, например по разряду батареи (`ioreg AppleSmartBattery`) на длинных прогонах.
- **Самостоятельная ВКР?** Нет, новизна мала. Но без него углы 1–4 не будут убедительны.
- **Риск.** Валидность энергетических измерений без powermetrics.

**Рекомендуемая композиция ВКР:** угол 1 как ядро, угол 2 или 3 как вторая глава, угол 5 как методика. Пример узкой формулировки в рамках темы руководителя: *«Разработка tool-aware среды исполнения ИИ-агента с вызовом функций для граничного устройства на базе малых языковых моделей (на примере Apple Silicon)»* или *«Методы снижения задержки агентного цикла вызова функций малыми языковыми моделями на граничных устройствах»*.

---

## 8. Связь с наследием `legacy/edge-sentinel` (критически, в разрезе этой области)

- **Не соответствует современному понятию «ИИ-агент».** В литературе 2023–2026 (ReAct, Toolformer, TinyAgent, BFCL) агент — это LLM/SLM, которая планирует и вызывает инструменты в цикле. Детерминированный контроллер выбора между linear, MLP и INT8 на UCI digits к этому определению не относится. Для темы «ИИ-агенты для Edge AI» рецензент сочтёт такое ядро нерелевантным.
- **Что можно переиспользовать:**
  - C++-исполнитель DAG (`native_execution_lab`): основа угла 3.
  - Идея планировщика памяти по жизненным циклам (`tensor_arena_lab`): переносится на управление бюджетом KV-кэша схем в угле 1 и на вытеснение контекстов, как в LLMS ([E3]).
  - Независимый trace-checker из части (A): верификатор трасс выполнения агентного плана (порядок, зависимости, отсутствие лишних вызовов).
  - Практика честных отрицательных результатов и много тестов: перенести в методику.
- **Что не стоит тащить:** MLP на 8×8 digits как предмет исследования, а также web UI как самоцель.

---

## 9. Открытые инструменты, код, бенчмарки и данные

| Ресурс | URL | Лицензия | Применение |
|---|---|---|---|
| llama.cpp (llama-server, llama-bench, C API, GBNF, `--jinja` tool calling, опция llguidance) | https://github.com/ggml-org/llama.cpp | MIT | основной рантайм и место для системных модификаций |
| mlx-lm | https://github.com/ml-explore/mlx-lm | MIT | второй рантайм на Apple Silicon для перекрёстной проверки |
| BFCL (gorilla repo) | https://github.com/ShishirPatil/gorilla | Apache-2.0 | основной бенчмарк точности FC, поддержка локальных endpoint |
| TinyAgent (данные, модели, Mac-инструменты) | https://github.com/SqueezeAILab/TinyAgent | MIT (код) | edge-baseline, тест 1K, macOS-инструменты |
| LLMCompiler | https://github.com/SqueezeAILab/LLMCompiler | MIT | формат DAG-плана, baseline параллельных вызовов |
| XGrammar | https://github.com/mlc-ai/xgrammar | Apache-2.0 | движок constrained decoding (угол 2) |
| llguidance | https://github.com/guidance-ai/llguidance | MIT | движок constrained decoding, встраивается в llama.cpp |
| Outlines | https://github.com/dottxt-ai/outlines | Apache-2.0 | baseline constrained decoding (Python) |
| JSONSchemaBench / MaskBench | https://github.com/guidance-ai/jsonschemabench | лицензия в репозитории не указана (GitHub API: none), уточнить | схемы и методика замера масок |
| When2Call | https://github.com/NVIDIA/When2Call | Apache-2.0 | метрика «когда не вызывать» |
| τ-bench | https://github.com/sierra-research/tau-bench | MIT | многоходовая надёжность (pass^k), опционально |
| DroidCall | https://github.com/UbiquitousLearning/DroidCall | MIT | данные «системное API ОС как функции» |
| Hammer | https://github.com/MadeAgents/Hammer | Apache-2.0 (код); веса Hammer2.1-1.5b CC-BY-NC-4.0 | function masking, irrelevance-данные |
| xLAM / APIGen | https://github.com/SalesforceAIResearch/xLAM ; датасет `Salesforce/xlam-function-calling-60k` | Apache-2.0 (код); датасет CC-BY-4.0; веса xLAM-2 CC-BY-NC-4.0 | данные и baseline-модели |
| Qwen3 0.6B–8B | https://huggingface.co/Qwen | Apache-2.0 | основная лестница моделей |
| Llama-3.2-1B/3B-Instruct | https://huggingface.co/meta-llama | Llama 3.2 Community License | нативный tool-формат в llama.cpp |
| FunctionGemma-270M-it | https://huggingface.co/google/functiongemma-270m-it | Gemma Terms of Use | сверхмалая FC-модель |
| LFM2 (вкл. LFM2-1.2B-Tool) | https://huggingface.co/LiquidAI | LFM Open License («other») | edge-оптимизированная архитектура |
| macmon | https://github.com/vladkens/macmon (есть в Homebrew) | MIT | мощность CPU/GPU/ANE без sudo (IOReport); требует валидации |
| lm-Meter | https://github.com/amai-gsu/LM-Meter | MIT | референс методики фазового профилирования (Android) |
| AutoDroid-V2 | https://github.com/MobileLLM/AutoDroid-V2 | лицензия не указана | референс «план-программа» |
| ReCache | https://github.com/EIT-NLP/ReCache | не проверял | ближайший конкурент угла 1 |

---

## 10. Следующие шаги

1. Обсудить с руководителем узкую формулировку: ядро — угол 1, вторая глава — угол 2 или 3.
2. Неделя 1–2. Поднять llama-server и харнесс BFCL с Qwen3-0.6B/1.7B/4B/8B (Q4_K_M). Воспроизвести порядок чисел из [C3] на своих прогонах. Снять фазовую разбивку TTFT для промптов с 4/8/16/32 инструментами.
3. Неделя 2–3. Воспроизвести TinyAgent на M4 Pro (1.1B, тест 1K) как edge-baseline. Измерить реальные латентности macOS-инструментов: это решает, жизнеспособен ли угол 3.
4. Далее — прототип кэша блоков KV («точная» стратегия, затем «независимые блоки», затем выборочный пересчёт) и полный экспериментальный план.

*Все статьи, отмеченные [V]/[V-pre], проверены через arXiv API (название, авторы, дата), а venue — через OpenReview, OpenAlex DOI, PMLR или ACL Anthology в день составления заметки. Числа взяты из аннотаций и HTML-версий статей. Перед защитой стоит повторно сверить venue у препринтов 2026 года, поскольку статусы могут измениться.*
