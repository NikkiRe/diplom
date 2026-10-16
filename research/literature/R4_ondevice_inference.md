# R4. Эффективный инференс LLM на устройстве: квантование, speculative decoding, decode, упирающийся в пропускную способность памяти, измерения на мобильных SoC и Apple Silicon

Обзор литературы для бакалаврской ВКР (ИТМО, 09.03.01 «Информатика и вычислительная техника»).
Дата обзора: 2026-10-05. Целевая машина: MacBook, Apple M4 Pro (CPU 8P+4E, GPU 16 ядер, 24 ГБ unified memory, 273 ГБ/с по спецификации Apple).

---

## 0. Как проводилась проверка (методика обзора)

* **Источники метаданных** (первичные, проверялись программно или открытием страницы):
  arXiv API (`export.arxiv.org/api/query?id_list=...`): название, авторы, дата, комментарий авторов о venue, аннотация;
  Crossref API (`api.crossref.org/works/<DOI>`): venue, страницы, DOI для ACM/IEEE/ACL/NeurIPS;
  страницы PMLR (`proceedings.mlr.press/v202`, `v235`): мета-теги `citation_title/author` и страницы;
  `iclr.cc/virtual/2023/poster/10855` (GPTQ/OPTQ); `proceedings.mlsys.org` и `neurips.cc` (через поисковую выдачу с прямыми URL);
  OpenAlex (аннотации ACM-статей, к которым ACM DL отдаёт 403).
  dblp закрыт bot-проверкой, поэтому не использовался. Semantic Scholar не использовался.
* **verified = да** ставилось только после сверки названия, авторов, года и venue хотя бы по одному первичному источнику (arXiv + Crossref/PMLR/ACL Anthology/ACM). Если работа существует только на arXiv, она помечена как **preprint**.
* **Числа** взяты из аннотаций (arXiv/OpenAlex) или из HTML-версий статей на arXiv. Если число снято с графика или пересказано при автоматическом извлечении текста, это отмечено фразой «(по HTML-версии, перед цитированием сверить с PDF)».
* **Собственный пилотный замер** на M4 Pro (раздел 2) сделан, чтобы проверить ключевой тезис обзора на целевом железе. Сырые выводы `llama-bench` лежат в `research/literature/data/`.

### 0.1. Исправления к затравочному списку (что было неточно)

| Затравка | Что на самом деле (проверено) |
|---|---|
| «Large Language Models on Mobile Devices: Measurements, Analysis, and Insights (Xiao et al., 2024)» | Авторы: **Li, Lu, Cai, Ma, Xu**. Venue: **EdgeFM '24** (воркшоп при MobiSys '24), DOI 10.1145/3662006.3662059. Работа «Xiao et al.» — это **другая** статья: *Understanding Large Language Models in Your Pockets: Performance Study on COTS Mobile Devices* (Xiao, Huang, Chen, Tian), вышла в **IEEE TMC 2026** (arXiv 2410.03613). В обзор включены обе. |
| GPTQ (ICLR 2023) | На ICLR 2023 статья опубликована под названием **«OPTQ: Accurate Quantization for Generative Pre-trained Transformers»** (страница iclr.cc/virtual/2023/poster/10855). На arXiv и в коде она называется GPTQ. Ссылаться лучше так: «OPTQ (известна как GPTQ)». |
| AWQ (MLSys 2024) | Подтверждено, Best Paper. Название в трудах MLSys: «AWQ: Activation-aware Weight Quantization for **On-Device** LLM Compression and Acceleration». На arXiv в названии нет слов «On-Device». |
| Medusa (ICML 2024) | Подтверждено: PMLR 235:5209–5235. Ускорение Medusa-2 расходится между версиями: в аннотации PMLR 2.3–2.8×, в arXiv v3 2.3–3.6×. |
| EAGLE / EAGLE-2 / EAGLE-3 | EAGLE: ICML 2024 (PMLR 235:28935–28948). EAGLE-2: EMNLP 2024 (стр. 7421–7432). EAGLE-3: **NeurIPS 2025**. |
| Lookahead decoding (ICML 2024) | Подтверждено: PMLR 235:14060–14079. |
| SpecInfer (ASPLOS 2024) | Подтверждено: ASPLOS '24, Vol. 3, стр. 932–949. |
| PowerInfer (SOSP 2024), PowerInfer-2 | PowerInfer: SOSP '24, стр. 590–606. PowerInfer-2 на 2026-10-05 остаётся **preprint** (рецензируемой публикации не найдено). |
| llm.npu (ASPLOS 2025) | Подтверждено: ASPLOS '25, Vol. 1, стр. 445–462. В трудах название «Fast On-device LLM Inference with NPUs». |
| LLMCad (2023) | Только **preprint** (arXiv 2309.04255). |
| Chen et al. 2023 (speculative sampling) | Только **preprint** (DeepMind, arXiv 2302.01318). |
| LLM Inference Unveiled (Yuan et al., 2024) | Только **preprint** (arXiv 2402.16363). |
| «k-quants/GGUF в llama.cpp» | Рецензируемой статьи **нет**. Первоисточник: PR #1684 «k-quants» (автор ikawrakow, влит 2023-06-05) в llama.cpp. Независимая оценка форматов есть только в препринтах 2026 г. (Kurt; Davidsson & Nugues). |
| «MLX (Apple)» | Это фреймворк, а не статья (MIT, github.com/ml-explore/mlx). Научных статей о самом MLX нет. Есть препринты-измерения (Rajesh et al. 2025; Barrios 2026) и Apple-препринт ReDrafter с реализацией на MLX. |
| HeteroLLM | Опубликовано на **SOSP '25** под названием «Characterizing Mobile SoC for Accelerating Heterogeneous LLM Inference» (система называется HeteroInfer). |

---

## 1. Короткие выводы

1. **Decode при batch = 1 упирается в пропускную способность памяти.** Это подтверждают аналитические модели (Roofline; Pope et al.; Yuan et al.), измерения на телефонах (MELT, Xiao et al., Lu et al.) и на Apple Silicon (Benazir & Lin, POMACS 2025). Верхняя граница скорости: tok/s ≤ BW / (байты весов + байты KV-кэша на токен).
2. **Prefill упирается в вычисления.** Поэтому NPU и матричные блоки ускоряют именно prefill: llm.npu даёт 22.4× на prefill, HeteroInfer до 247.9 tok/s на prefill против 14 tok/s на decode. Фазы требуют разных оптимизаций, и это центральная идея Splitwise (ISCA 2024) и Disaggregated Quantization (2026).
3. **Меньше бит не всегда значит быстрее.** Стоимость деквантования и устройство ядер меняют картину. На M2 Ultra ядро Q6_K работает в 1.22 раза медленнее Q8_0, хотя читает меньше данных, а IQ1_M медленнее Q2_K (Benazir & Lin). Лучше всего оптимизированы «ровные» 4 бита (Lu et al.). Отсюда интерес к LUT-ядрам: T-MAC (EuroSys 2025), bitnet.cpp (ACL 2025).
4. **По качеству безопасны W4A16 и 8 бит, ниже начинается риск.** Для Llama-3.1 INT4 weight-only близок к 8 битам (Kurtic et al., ACL 2025). Для reasoning-моделей W8A8 и W4A16 практически без потерь, более низкие разрядности рискованы (Liu et al., COLM 2025; в исследовании есть Qwen3-8B). Для llama.cpp-форматов Q4_K_M теряет около 0.3 п.п. среднего скора на Llama-3.1-8B, Q3_K_M около 1.4 п.п. (Kurt, препринт 2026).
5. **Ускорение speculative decoding на одном устройстве не гарантировано.** Теоретический выигрыш (Leviathan et al.) предполагает, что верификация K токенов стоит почти столько же, сколько один шаг. На ноутбуке с Apple Silicon лучшая конфигурация дала 1.61×, а 3 из 5 конфигураций **замедлили** генерацию: квантованный Metal-бэкенд выполнял «параллельную» верификацию последовательно (Chordiya, препринт 2026). Draft-модель выбирают по задержке, а не по качеству (Yan et al., NAACL 2025). На Apple Silicon через MLX показано ускорение до 2.3× (ReDrafter, Apple).
6. **Ожидаемые числа для Apple Silicon** подтверждены нашим пилотом: Qwen3-8B Q4_K_M на M4 Pro даёт decode 44.5 tok/s, то есть эффективные ≈208 ГБ/с (≈76% от 273 ГБ/с). Замедление при контексте 4096 токенов **предсказывается** простой моделью «байты/токен» с ошибкой <0.5%. Prefill даёт ≈369 tok/s (≈6 TFLOPS эффективных). Измерение под фоновой GPU-нагрузкой дало разброс ±22 tok/s, поэтому методика замеров критична.
7. **Энергию на Mac без sudo измерять сложно.** `powermetrics` требует root, которого у нас нет. В литературе энергия на Apple Silicon для LLM измерена мало: Hübner et al. использовали powermetrics, Benazir & Lin сосредоточены на задержке и метриках GPU. Это и пробел, и риск для ВКР.

---

## 2. Аналитическая база и пилотный замер на M4 Pro

### 2.1. Формулы, на которых держится вся область

**Decode (batch = 1).** На каждый новый токен нужно прочитать все веса линейных слоёв, `lm_head` и весь KV-кэш:

```
B_tok(ctx) = W_eff + KV(ctx)
KV(ctx)    = 2 · L · n_kv · d_head · s_kv · ctx        (s_kv = 2 байта для f16)
tok/s      ≤ BW_peak / B_tok(ctx)                      (roofline-граница)
tok/s      ≈ η · BW_peak / B_tok(ctx),  η — измеряемая эффективность
```

Арифметическая интенсивность GEMV с b-битными весами: I ≈ 2 FLOP / (b/8 байт) = 16/b FLOP/байт. Для FP16 это 1, для Q4_K (≈4.5 бит/вес) ≈3.6. Это на порядок ниже ridge point любого современного GPU. Yuan et al. на A6000 получают для decode ≈1 OP/байт в проекциях, для prefill ≈1024–1215 OP/байт.

**Prefill** выполняет ≈2·N·T FLOP плюс attention ∝ T². Уже при T в десятки токенов он становится compute-bound.

**Speculative decoding** (Leviathan et al., 2023). При доле принятия α, длине черновика γ и относительной стоимости draft-модели c ожидаемое число токенов за итерацию равно (1−α^{γ+1})/(1−α). Ускорение S = (1−α^{γ+1}) / ((1−α)(γ·c+1)). **Скрытое допущение:** верификация γ+1 токенов стоит столько же, сколько один шаг целевой модели. На устройстве корректнее записывать
S = E[tokens] / (γ·c + t_verify(γ+1)/t_target(1)),
где t_verify(·) нужно **измерять** на конкретном бэкенде и формате весов.

### 2.2. Пилот: Qwen3-8B Q4_K_M, llama.cpp b11146 (Homebrew, ggml 0.25.3), M4 Pro

Параметры модели прочитаны из GGUF: L = 36, n_kv = 8, d_head = 128, d_model = 4096, d_ff = 12288. Тензоры весят 5.022 ГБ: 3.705 ГБ в Q4_K и 1.316 ГБ в Q6_K. За вычетом таблицы эмбеддингов (0.350 ГБ, при decode читается одна строка) W_eff = 4.672 ГБ. KV f16 = 147 456 байт на токен.

| Замер (`llama-bench`, параметры по умолчанию, Metal, 8 потоков) | Результат | Интерпретация |
|---|---|---|
| pp512 (prefill), прогон 1 | 368.68 ± 3.41 tok/s | ≈6.0 TFLOPS эффективных (2·N·tok/s без attention) |
| tg128 @ ctx≈0, прогоны 1 / 4 | 44.46 ± 0.42 / 44.50 ± 0.35 tok/s | B_tok ≈ 4.68 ГБ → эффективные ≈208 ГБ/с ≈ 76% от 273 ГБ/с; roofline-граница 58.3 tok/s |
| tg128 @ ctx = 4096 (прогон 4) | 39.56 ± 0.32 tok/s | Модель предсказывает отношение 0.886, измерено 0.889 (ошибка 0.4%); эффективная BW снова ≈209 ГБ/с |
| CPU-only (`-ngl 0`, 8 потоков): pp128 / tg64 | 37.86 ± 0.62 / 22.66 ± 0.73 tok/s | Decode на CPU даёт эффективные ≈106 ГБ/с; prefill на CPU ≈10× медленнее GPU |
| tg128, прогон 2, **с фоновой GPU-нагрузкой** (видео-декодер, WebKit GPU, VPN-туннель) | 31.58 ± 22.38; при ctx = 4096: 16.38 ± 17.80 | Конкуренция за GPU и память разрушает воспроизводимость, нужен протокол замеров |

**Выводы из пилота.**
(а) Простая модель «байты/токен» на целевом железе точна, а η ≈ 0.76 стабилен по глубине контекста. Для сравнения, у Benazir & Lin на M2 Ultra η ≈ 0.85.
(б) При контексте ≈32K KV-кэш Qwen3-8B (≈4.8 ГБ) сравняется с весами. Тогда decode упадёт примерно вдвое (≈22–23 tok/s). Это **прогноз**, его нужно проверить замером. Для агентов с длинной историей инструментов квантование KV станет так же важно, как квантование весов.
(в) Лимит Metal `recommendedMaxWorkingSetSize` = 19 069.67 МБ из 24 ГБ (из лога llama.cpp). Значит, модели Q4 крупнее примерно 30B на GPU не помещаются, и для них нужен стриминг весов (раздел 3.C).
(г) Это один набор прогонов одной модели без контроля температуры. Для ВКР его нужно повторить по протоколу из раздела 6.

---

## 3. Каталог работ

Формат записи: библиографическая запись → статус проверки → проблема → метод → результаты (числа) → польза для ВКР → что заимствовать.

### A. Модели производительности и фазы инференса

#### A1. williams2009roofline
**Williams S., Waterman A., Patterson D.** Roofline: An Insightful Visual Performance Model for Multicore Architectures // *Communications of the ACM*. 2009. Vol. 52, No. 4. P. 65–76. DOI: 10.1145/1498765.1498785.
*Проверено:* Crossref (CACM 52(4):65–76).
* **Проблема.** Нужна простая модель, которая показывает, во что упирается ядро: в вычисления или в память.
* **Метод.** Производительность ≤ min(P_peak, I·BW), где I — арифметическая интенсивность (FLOP/байт). Есть «потолки» для отдельных оптимизаций (SIMD, ILP, prefetch).
* **Результаты.** Классическая работа, по OpenAlex около 2.5 тыс. цитирований. Применена к нескольким многоядерным архитектурам 2009 г.
* **Польза для ВКР.** Теоретическая основа главы «анализ производительности». Позволяет объяснить decode и prefill без эмпирики и ставить измеримые гипотезы.
* **Заимствовать.** Построение roofline для M4 Pro: эмпирически измеренные «крыши» (STREAM-подобный тест BW для CPU и GPU, пиковые FLOPS GEMM на Metal/AMX/SME) и точки ядер llama.cpp на этом графике.

#### A2. pope2023scaling
**Pope R., Douglas S., Chowdhery A., Devlin J., Bradbury J., Levskaya A., Heek J., Xiao K., Agrawal S., Dean J.** Efficiently Scaling Transformer Inference // *Proceedings of Machine Learning and Systems (MLSys)* 5. 2023. arXiv:2211.05102.
*Проверено:* arXiv (авторы и аннотация), страница proceedings.mlsys.org/paper_files/paper/2023/hash/c4be71ab… (через поисковую выдачу).
* **Проблема.** Генеративный инференс крупных моделей с жёсткими ограничениями на задержку.
* **Метод.** Аналитическая модель стоимости (FLOPs, байты памяти, коммуникации) для выбора разбиения модели на TPU v4. Multi-query attention для экономии KV.
* **Результаты.** PaLM 540B: 29 мс/токен на decode при малом batch (с int8-весами), 76% MFU на prefill при большом batch, контекст 2048. MQA позволяет увеличить контекст до 32×.
* **Польза.** Канонический пример того, как «модель стоимости + измерение» дают инженерные решения. Формулы memory time и compute time переносятся на один SoC.
* **Заимствовать.** Разделение латентности на memory time и compute time. Метрики MFU и MBU (model bandwidth utilization) для отчётов.

#### A3. yuan2024unveiled
**Yuan Z., Shang Y., Zhou Y., Dong Z., Zhou Z., Xue C., Wu B., Li Z., et al.** LLM Inference Unveiled: Survey and Roofline Model Insights. arXiv:2402.16363. 2024. **Preprint.**
*Проверено:* arXiv.
* **Проблема.** Нет единой рамки, чтобы сравнивать методы ускорения инференса.
* **Метод.** Обзор методов (квантование, дистилляция, early exit, MoE, системные приёмы), каждый проанализирован через roofline. Инструмент LLM-Viewer (MIT).
* **Результаты.** Пример на Llama-2-7B, A6000, seq = 2048 (по HTML-версии). Prefill: проекции ≈1024 OP/байт, FFN ≈1215 OP/байт (compute-bound), qk/sv matmul ≈114 OP/байт. Decode: все операции ≈1 OP/байт (memory-bound). Квантование весов полезно, пока система memory-bound, и теряет смысл в compute-bound режиме при больших batch.
* **Польза.** Готовая методика пооперационного roofline. Инструмент можно доработать для профиля Apple M4 Pro.
* **Заимствовать.** LLM-Viewer как baseline для аналитического предсказания; таблицы AI по слоям.

#### A4. patel2024splitwise
**Patel P., Choukse E., Zhang C., Shah A., Goiri Í., Maleki S., Bianchini R.** Splitwise: Efficient Generative LLM Inference Using Phase Splitting // *ISCA 2024*. P. 118–132. DOI: 10.1109/ISCA59077.2024.00019.
*Проверено:* Crossref, arXiv 2311.18677.
* **Проблема.** Prefill и decode различаются по латентности, пропускной способности, памяти и мощности. Decode не загружает вычислительные блоки GPU.
* **Метод.** Характеризация фаз на GPU-кластере. Фазы разносятся на разные машины с передачей KV-состояния.
* **Результаты.** 1.4× пропускной способности при стоимости на 20% ниже, либо 2.35× пропускной способности при тех же стоимости и мощности.
* **Польза.** Авторитетное (ISCA) обоснование тезиса «у фаз разная природа». На устройстве аналог — разнести фазы по разным исполнителям: GPU или ANE для prefill, CPU или GPU для decode (ср. Xiao et al.: «GPU prefill + CPU decode»).
* **Заимствовать.** Методика раздельной характеризации фаз (латентность, мощность, утилизация).

### B. Измерительные исследования: мобильные SoC и Apple Silicon

#### B1. laskaridis2024melt
**Laskaridis S., Katevas K., Minto L., Haddadi H.** MELTing Point: Mobile Evaluation of Language Transformers // *MobiCom '24*. 2024. P. 890–907. DOI: 10.1145/3636534.3690668. arXiv:2403.12844.
*Проверено:* Crossref, arXiv.
* **Проблема.** Нет систематического измерения LLM на мобильных устройствах: производительность, энергия, точность, нагрев.
* **Метод.** Инфраструктура MELT для headless-автоматизации запусков на Android, iOS и Jetson. Фреймворки llama.cpp и MLC-LLM. Модели TinyLlama-1.1B, Zephyr-3B, Gemma-2B/7B, Mistral-7B, Llama-2-7B/13B. Квантование 3/4-bit k-quants, GPTQ, AWQ. Энергия на телефонах измерялась прибором Monsoon AAA10F (5 кГц) с обходом батареи, на Jetson — через sysfs. Использовалась тепловизионная камера.
* **Результаты** (по HTML-версии, сверить с PDF). На iPhone 14 Pro генерация в среднем на ≈79% быстрее, чем на Android (MLC). Zephyr-3B 4-bit тратит ≈0.16–0.21 мВт·ч на токен, батареи хватает примерно на 490–590 промптов. Пиковая мощность iPhone 14 Pro ≈18 Вт мгновенная, ≈13.8 Вт устойчивая. Температура после диалога 47.9 °C. 4-bit в среднем на 24.77% быстрее 3-bit. Модели ≤3B в 3-bit «в основном галлюцинируют или повторяют промпт». Главный вывод авторов: инференс в значительной мере memory-bound, а непрерывная работа LLM упирается в энергию и нагрев.
* **Польза.** Эталон **методики** измерений: автоматизация, повторяемость, энергия, температура, качество. Подходящий образец для главы «методика эксперимента».
* **Заимствовать.** Набор метрик: prefill и decode tok/s, загрузка модели, мВт·ч/токен, температура, точность на lm-eval-harness. Структура отчёта. Код brave-experiments/MELT-public (лицензия не распознана GitHub, проверить вручную).

#### B2. li2024edgefm
**Li X., Lu Z., Cai D., Ma X., Xu M.** Large Language Models on Mobile Devices: Measurements, Analysis, and Insights // *EdgeFM '24: Workshop on Edge and Mobile Foundation Models (MobiSys '24)*. 2024. P. 1–6. DOI: 10.1145/3662006.3662059.
*Проверено:* Crossref, аннотация через OpenAlex. Полный текст не открывался (ACM DL отвечает 403).
* **Проблема.** Нет систематического понимания LLM размером до 10B, которые уже реально работают на телефонах.
* **Метод.** 22 модели на 4 мобильных устройствах. Измерялись точность, латентность и память в зависимости от длины входа, устройства и движка.
* **Результаты.** В аннотации конкретных чисел нет. Для цитирования чисел нужен PDF.
* **Польза.** Короткая воркшоп-статья из группы М. Xu (PKU/BUPT), которая затем выпустила SLM-обзор (B4). Полезна как ссылка в related work.
* **Заимствовать.** Схема эксперимента «модель × устройство × движок × длина входа».

#### B3. xiao2026pockets
**Xiao J., Huang Q., Chen X., Tian C.** Understanding Large Language Models in Your Pockets: Performance Study on COTS Mobile Devices // *IEEE Transactions on Mobile Computing*. 2026. Vol. 25, No. 8. P. 13077–13094. DOI: 10.1109/TMC.2026.3674827. arXiv:2410.03613.
*Проверено:* Crossref, arXiv (v5).
* **Проблема.** Как LLM ведут себя на серийных телефонах с точки зрения пользователя (скорость, задержка) и разработчика (ресурсы, ОС, батарея, старт).
* **Метод.** 7 устройств от 4 вендоров (Qualcomm, MediaTek, HiSilicon, Apple M1 iPad). llama.cpp на CPU, MLC на GPU. Изучались потоки и кластеры ядер, DVFS, батарея, холодный старт, конкуренция с другими задачами.
* **Результаты** (по HTML v5, часть чисел снята с графиков; сверить с PDF). Оптимальное число потоков равно числу больших ядер, добавление E-ядер обычно ухудшает скорость. GPU ускоряет prefill, но почти не помогает decode. Утилизация ALU на prefill: Mali-G720 <3%, Adreno 750 ≈20%, Apple M1 до 92%. Расход батареи на Llama-2-7B: CPU ≈0.10–0.14 мА·ч/токен, CPU с инструкциями sdot/smmla ≈0.038, GPU ≈0.013. На Snapdragon 8 Gen3 частота prime-ядра падает примерно вдвое к 9-му последовательному прогону. При параллельном YOLO на 8 потоках decode падает более чем на 90%.
* **Польза.** Прямо показывает эффекты, которые видны и на нашем Mac: конкуренция за ресурсы, выбор потоков, DVFS и нагрев. Это фактическая база для «агента-контроллера» (раздел 5, угол A4).
* **Заимствовать.** Сценарии с фоновыми задачами; метрики холодного и тёплого старта; анализ потоков P/E.

#### B4. lu2024slm
**Lu Z., Li X., Cai D., Yi R., Liu F., Zhang X., Lane N. D., Xu M.** Small Language Models: Survey, Measurements, and Insights. arXiv:2409.15790. 2024. **Preprint.**
*Проверено:* arXiv.
* **Проблема.** Малые модели (100M–5B) почти не изучены как класс, особенно их стоимость на устройстве.
* **Метод.** Обзор 70 SLM, оценка качества и замеры на Jetson Orin NX 16GB и смартфонах в llama.cpp (50 токенов промпта и 50 токенов генерации).
* **Результаты** (по HTML-версии). Matrix-vector операции занимают более 80% времени. 4-bit сокращает латентность decode до 75%. Q4_K_M даёт стабильное ускорение prefill около 50%. «Нерегулярные» 3/5/6 бит хуже оптимизированы. Размер словаря сильно влияет на память: у Bloom-560M (250K токенов) compute buffer в 3.5 раза больше, чем у OpenELM-1.1B. При максимальных контекстах KV занимает 83–90% памяти. GQA уменьшает KV примерно в 3.9 раза.
* **Польза.** Даёт обоснование выбора draft-модели и модели для агента среди малых моделей. Вывод «KV доминирует на длинном контексте» совпадает с нашим прогнозом.
* **Заимствовать.** Пооперационную разбивку латентности; вывод о «неровных» разрядностях как гипотезу для проверки на Metal.

#### B5. benazir2025apple
**Benazir A., Lin F. X.** Benchmarking and Characterization of Large Language Model Inference on Apple Silicon // *Proceedings of the ACM on Measurement and Analysis of Computing Systems (POMACS)*. 2025. Vol. 9, No. 3. 26 p. DOI: 10.1145/3771563. Видимо, препринт той же работы (совпадают авторы, стенды и выводы): arXiv:2508.08531 «Profiling Large Language Model Inference on Apple Silicon: A Quantization Perspective».
*Проверено:* Crossref (POMACS 9(3), дек. 2025), аннотация через OpenAlex, HTML препринта.
* **Проблема.** Систематической характеристики Apple Silicon для инференса LLM нет. Распространён миф, что «меньше бит — всегда быстрее».
* **Метод.** 5 стендов: M2 Ultra, M2 Max, **M4 Pro (48 ГБ, 273 ГБ/с)**, RTX A6000 и 2×A6000. Модели 8B–405B, 14 схем квантования llama.cpp (Q2_K…Q8_0, IQ1_M…IQ4_NL). Профилирование низкоуровневых метрик (ALU, BW, буферы, кэш) в Xcode Instruments.
* **Результаты** (по HTML препринта). На M2 Ultra используется около 85% пиковой BW. Ядро Q6_K в 1.22 раза медленнее Q8_0, хотя данных меньше (6.56 против 8 бит/вес): виновато деквантование. Llama-70B в Q2_K (2.625 bpw) быстрее, чем в IQ1_M (1.75 bpw). Утилизация ALU: Q2_K 76.8%, IQ1_M 35.8%. Для сверхкрупных моделей M2 Ultra по стоимости и токенам выигрывает у 1×A6000 (до 3.67× токенов при 3.5× лучшей стоимости). Ровно на нашем классе железа (M4 Pro).
* **Польза.** **Ближайшая работа к ВКР.** Задаёт планку методики (Instruments, метрики GPU), показывает пробелы: мало про энергию, нет модели-предиктора, нет speculative decoding, нет CPU/SME.
* **Заимствовать.** Набор форматов и моделей; профилирование через Instruments/Metal counters; гипотезу «стоимость деквантования × формат» для собственного cost-model.

#### B6. hubner2025apple
**Hübner P., Hu A., Peng I., Markidis S.** Apple vs. Oranges: Evaluating the Apple Silicon M-Series SoCs for HPC Performance and Efficiency // *IPDPSW 2025*. P. 45–54. DOI: 10.1109/IPDPSW66978.2025.00013. arXiv:2502.05317.
*Проверено:* Crossref, arXiv.
* **Проблема.** Каковы реальные пики FP32 и BW у M1–M4 и их энергоэффективность.
* **Метод.** Собственные бенчмарки на Metal Shading Language и Objective-C++ (GPU, AMX), мощность через `powermetrics`.
* **Результаты.** Базовые чипы M1–M4 дают до ≈100 ГБ/с BW. M4 GPU выдаёт до 2.9 TFLOPS FP32. Мощность от нескольких Вт до 10–20 Вт. Все чипы превышают 200 GFLOPS/Вт.
* **Польза.** Образец микробенчмарков на Metal для «крыш» roofline. Подтверждает, что измеренная BW ниже паспортной.
* **Заимствовать.** Дизайн STREAM-подобных Metal-ядер. Предупреждение: `powermetrics` требует sudo, у нас его нет.

#### B7. remke2024hellosme
**Remke S., Breuer A.** Hello SME! Generating Fast Matrix Multiplication Kernels Using the Scalable Matrix Extension // *SC24-W: Workshops of SC24*. 2024. P. 1443–1454. DOI: 10.1109/SCW63240.2024.00185. arXiv:2409.18779.
*Проверено:* Crossref, arXiv. Дополнительно на нашем M4 Pro подтверждено: `hw.optional.arm.FEAT_SME = 1`, `FEAT_SME2 = 1`, `SME_I8I32 = 1`, `SME_F16F32 = 1`.
* **Проблема.** M4 — первый чип с Arm SME. Его реальные характеристики неизвестны.
* **Метод.** Микробенчмарки пропускной способности FP и int на SME. JIT-генератор малых GEMM.
* **Результаты.** SME на M4 ориентирован на FP32: более 2.3 TFLOPS FP32. Загрузку и выгрузку матричных регистров нужно делать в два шага. JIT-ядра почти везде быстрее вендорного BLAS.
* **Польза.** Открывает **низкоуровневый угол** (A3): CPU-ядра prefill и decode для квантованных LLM на SME/I8MM. Это системное ПО в чистом виде.
* **Заимствовать.** Методику микробенчмарков SME. Сравнение с Accelerate. Идею JIT-генерации.

#### B8. rajesh2025applesilicon
**Rajesh V., Jodhpurkar O., Anbuselvan P., Singh M., Jallepali A., Godbole S., Sharma P. K., Shrivastava H.** Production-Grade Local LLM Inference on Apple Silicon: A Comparative Study of MLX, MLC-LLM, Ollama, llama.cpp, and PyTorch MPS. arXiv:2511.05502. 2025. **Preprint.**
*Проверено:* arXiv.
* **Результаты.** M2 Ultra 192 ГБ, Qwen-2.5, промпты до 100K токенов. MLX даёт максимальный устойчивый throughput генерации. MLC-LLM даёт меньший TTFT на средних промптах. llama.cpp эффективен для одного потока. Ollama отстаёт. PyTorch MPS ограничен памятью.
* **Польза и заимствование.** Обоснование выбора бэкендов (llama.cpp и MLX) и метрик (TTFT, p50/p95, длинный контекст). Скрипты открыты.

#### B9. qiu2026ggufpred
**Qiu X., Xu C., Su B., Chen Z., Xu R., Dai S.** GGUF-Metadata Prediction of Single-Sequence llama.cpp Throughput Across Three Systems. arXiv:2609.14864. 2026. **Preprint** (подана на ICASSP 2027).
*Проверено:* arXiv.
* **Проблема.** Можно ли предсказать tok/s по метаданным GGUF.
* **Метод.** Предикторы в форме roofline с коэффициентами под каждый формат квантования. 318 замеров, 53 конфигурации, **2× Apple M4 Max** и RTX 5080.
* **Результаты.** Decode-модель на «активных параметрах» даёт MAPE 13.1 / 14.4 / 36.1% против 49–55% у модели на полных параметрах. Prefill-baseline даёт MAPE 18.7 / 22.2 / **108.2%**. Эффективности «не универсальны».
* **Польза.** **Прямой baseline для угла A1**: опубликованная точность, которую нужно превзойти на M4 Pro, особенно для prefill и с учётом KV (у нас ошибка 0.4% на одной точке).
* **Заимствовать.** Постановку задачи и протокол leave-one-host-out. Метрику MAPE.

#### B10. husom2025sustainable
**Husom E. J., Goknil A., Astekin M., Shar L. K., Kåsen A., Sen S., Mithassel B. A., Soylu A.** Sustainable LLM Inference for Edge AI: Evaluating Quantized LLMs for Energy Efficiency, Output Accuracy, and Inference Latency. arXiv:2504.03360. 2025. **Preprint.**
*Проверено:* arXiv.
* **Результаты.** 28 квантованных моделей Ollama на Raspberry Pi 4 (4 ГБ). Энергия измерялась аппаратным измерителем. Задачи: CommonsenseQA, BBH, TruthfulQA, GSM8K, HumanEval.
* **Польза.** Образец совместного отчёта «энергия × точность × латентность». Числа для Pi в ВКР не переносятся.

### C. Системы инференса на устройстве

#### C1. alizadeh2024flash
**Alizadeh K., Mirzadeh S. I., Belenko D., Khatamifard S. K., Cho M., Del Mundo C. C., Rastegari M., Farajtabar M.** LLM in a flash: Efficient Large Language Model Inference with Limited Memory // *ACL 2024 (Long Papers)*. P. 12562–12584. DOI: 10.18653/v1/2024.acl-long.678. arXiv:2312.11514.
*Проверено:* Crossref, arXiv.
* **Проблема.** Модель больше DRAM устройства.
* **Метод.** Веса хранятся во flash и подгружаются по требованию. Cost-model flash-памяти: больше последовательные чанки, меньше объём. «Windowing» переиспользует нейроны последних k токенов. «Row-column bundling» объединяет строки и столбцы FFN. Активированные нейроны предсказываются за счёт ReLU-разреженности.
* **Результаты.** Модели до 2× больше DRAM. По сравнению с наивной загрузкой ускорение 4–5× на CPU и 20–25× на GPU. По HTML-версии: OPT-6.7B на M1 Max, I/O на токен 2196 мс (наивно) против ≈105 мс I/O + ≈58 мс накладных. Разреженность FFN 90–97%. Последовательное чтение flash на M1 Max выше 6 ГБ/с. Bundling поднимает throughput чтения с 1.25 до 2.25 ГБ/с.
* **Польза.** Решает ровно наше ограничение: 19 ГБ Metal working set из 24 ГБ. Это Apple-работа с cost-model I/O.
* **Заимствовать.** Модель стоимости «объём × размер чанка». Измерение SSD M4 Pro для mmap-стриминга в llama.cpp.

#### C2. song2024powerinfer
**Song Y., Mi Z., Xie H., Chen H.** PowerInfer: Fast Large Language Model Serving with a Consumer-grade GPU // *SOSP '24*. P. 590–606. DOI: 10.1145/3694715.3695964. arXiv:2312.12456.
*Проверено:* Crossref, arXiv.
* **Метод.** Активации нейронов распределены по степенному закону. «Горячие» нейроны хранятся на GPU, «холодные» считаются на CPU. Используются адаптивные предикторы и разреженные операторы.
* **Результаты.** До 11.69× быстрее llama.cpp на RTX 4090. OPT-30B достигает 82% скорости генерации A100.
* **Польза.** Пример системной работы уровня SOSP поверх llama.cpp-подобного движка. На unified memory идея «hot/cold» превращается в «DRAM/SSD» (как в C1 и C3). Код MIT (Tiiny-AI/PowerInfer).

#### C3. xue2024powerinfer2
**Xue Z., Song Y., Mi Z., Zheng X., Xia Y., Chen H.** PowerInfer-2: Fast Large Language Model Inference on a Smartphone. arXiv:2406.06282. 2024. **Preprint.**
*Проверено:* arXiv.
* **Метод.** Матричные операции разбиваются на кластеры нейронов. Плотные кластеры считаются на NPU, разреженные на CPU. Конвейер I/O и вычислений с сегментированным кэшем нейронов.
* **Результаты.** До 27.8× быстрее SOTA-фреймворков. Модель на 47B на смартфоне выдаёт 11.68 tok/s.
* **Польза.** Шаблон «I/O-compute pipeline + кэш» для модели больше памяти (угол A5).

#### C4. xu2025llmnpu
**Xu D., Zhang H., Yang L., Liu R., Huang G., Xu M., Liu X.** Fast On-device LLM Inference with NPUs // *ASPLOS '25*, Vol. 1. P. 445–462. DOI: 10.1145/3669940.3707239. arXiv:2407.05858.
*Проверено:* Crossref, arXiv.
* **Проблема.** На мобильных задачах (например, понимание UI) узкое место — prefill.
* **Метод.** Prefill выносится на NPU. Промпт режется на чанки фиксированного размера. Выбросы активаций уходят на CPU/GPU. Блоки планируются out-of-order между NPU и CPU/GPU.
* **Результаты.** В среднем prefill в 22.4× быстрее и энергия в 30.7× меньше, end-to-end до 32.8×. Впервые получено более 1000 tok/s prefill для модели масштаба 1B.
* **Польза.** Доказывает тезис «prefill → ускоритель». Для Apple аналог — ANE через Core ML (закрытый стек, высокий риск).
* **Заимствовать.** Чанкинг промпта под статические формы; метрики энергии prefill. Код mllm (MIT).

#### C5. chen2025heteroinfer
**Chen L., Feng D., Feng E., Wang Y., Zhao R., Xia Y., Xu P., Chen H.** Characterizing Mobile SoC for Accelerating Heterogeneous LLM Inference // *SOSP '25*. P. 359–374. DOI: 10.1145/3731569.3764808. arXiv:2501.14794.
*Проверено:* Crossref, arXiv.
* **Метод.** Характеризация GPU и NPU Snapdragon 8 Gen 3. Параллелизм GPU+NPU. Быстрая синхронизация через unified memory.
* **Результаты** (по HTML-версии). Один процессор на decode достигает только 40–45 ГБ/с из 68 ГБ/с пика. GPU+NPU в соотношении 75/25 дают ≈60 ГБ/с. Синхронизация GPU–NPU стоит ≈400 мкс. Порядок и форма матриц меняют скорость NPU до 6×. Llama-8B: prefill 247.9 tok/s, decode 14.01 tok/s (llama.cpp: 4.4 и 5.5). End-to-end 1.34–6.02× против GPU-only и NPU-only. Энергия на 55% меньше GPU-only.
* **Польза.** Методика «характеризация → механизм» на уровне SOSP. Открытый вопрос для Apple: насыщает ли один GPU шину M4 Pro? Наш пилот: GPU ≈208 ГБ/с, CPU ≈106 ГБ/с. Даёт ли совместная работа больше — неизвестно, это измеримая гипотеза.
* **Заимствовать.** Эксперимент «BW одного исполнителя против BW совместно работающих исполнителей».

#### C6. wei2025tmac
**Wei J., Cao S., Cao T., Ma L., Wang L., Zhang Y., Yang M.** T-MAC: CPU Renaissance via Table Lookup for Low-Bit LLM Deployment on Edge // *EuroSys '25*. P. 278–292. DOI: 10.1145/3689031.3696099. arXiv:2407.00088.
*Проверено:* Crossref, arXiv.
* **Проблема.** mpGEMM (низкобитные веса × FP-активации) без нативной поддержки требует деквантования.
* **Метод.** Битовая декомпозиция весов и табличный поиск (LUT) вместо умножений. Ядра линейно масштабируются по числу бит.
* **Результаты.** До 4× больше throughput и на 70% меньше энергии по сравнению с llama.cpp. BitNet-b1.58-3B на **M2-Ultra** выдаёт 30 tok/s на одном ядре и 71 tok/s на 8 ядрах. Raspberry Pi 5 даёт 11 tok/s.
* **Польза.** **Главный baseline для низкоуровневого угла A3.** Есть измерения на Apple M2 и открытый код (MIT).
* **Заимствовать.** Ядра LUT-mpGEMV; методику сравнения с llama.cpp по tok/s и энергии.

#### C7. wang2025bitnetcpp
**Wang J., Zhou H., Song T., Cao S., Xia Y., Cao T., Wei J., Ma S., et al.** Bitnet.cpp: Efficient Edge Inference for Ternary LLMs // *ACL 2025 (Long Papers)*. P. 9305–9322. DOI: 10.18653/v1/2025.acl-long.457. arXiv:2502.11880.
*Проверено:* Crossref, arXiv.
* **Метод.** Тернарный LUT (TL) и формат I2_S для инференса без потерь при весах менее 2 бит. Элементный LUT (ELUT) для low-bit.
* **Результаты.** До 6.25× быстрее full-precision и до 2.32× быстрее low-bit baseline.
* **Польза.** Второй открытый (MIT) baseline для CPU-ядер, совместимый с ggml.

#### C8. benazir2026npumoe
**Benazir A., Lin F. X.** Efficient Mixture-of-Experts LLM Inference with Apple Silicon NPUs. arXiv:2604.18788. 2026. **Preprint.**
*Проверено:* arXiv.
* **Метод.** NPUMoE: плотная статическая часть MoE считается на ANE, динамическая (top-k, scatter/gather) остаётся на CPU/GPU. Статические уровни ёмкости экспертов, групповое исполнение, резидентность графов.
* **Результаты.** На Apple M-series латентность ниже в 1.32–5.55 раза, энергоэффективность выше в 1.81–7.37 раза, CPU-циклов меньше в 1.78–5.54 раза.
* **Польза.** Единственная найденная работа про ANE для LLM-prefill на Mac. Показывает, что ANE-угол реалистичен, но требует Core ML и статических форм.

#### C9. gunter2024afm
**Gunter T., Wang Z., Wang C., Pang R., Narayanan A., Zhang A., Zhang B., Chen C., et al.** Apple Intelligence Foundation Language Models. arXiv:2407.21075. 2024. **Технический отчёт (preprint).** Дополнение: Apple ML Research blog «Introducing Apple's On-Device and Server Foundation Models» (июнь 2024). Продолжение: arXiv:2507.13575 (2025).
*Проверено:* arXiv; страница machinelearning.apple.com.
* **Результаты** (из блога Apple). Модель около 3B на iPhone 15 Pro: TTFT ≈0.6 мс на токен промпта, генерация ≈30 tok/s. Смешанное 2- и 4-битное квантование со средним 3.7 bpw (возможно 3.5 bpw без заметной потери). Talaria — инструмент анализа латентности и мощности. Для генерации используется «token speculation». В отчёте 2025 г.: 2-bit QAT и KV-cache sharing.
* **Польза.** Промышленный ориентир того, что считается «приемлемым» на устройстве Apple: смешанная разрядность по слоям и спекуляция.

### D. Квантование

#### D1. dettmers2022int8
**Dettmers T., Lewis M., Belkada Y., Zettlemoyer L.** LLM.int8(): 8-bit Matrix Multiplication for Transformers at Scale // *NeurIPS 35*. 2022. P. 30318–30332. arXiv:2208.07339.
*Проверено:* Crossref (NeurIPS 35), arXiv.
* **Метод.** Векторное (vector-wise) квантование. Выбросы (emergent outlier features) обрабатываются в FP16 через mixed-precision decomposition, более 99.9% значений умножаются в int8.
* **Результаты.** Память на инференс сокращается вдвое без деградации до 175B.
* **Польза.** Первоисточник «проблемы выбросов» в активациях. Объясняет, почему W8A8 сложнее weight-only.

#### D2. xiao2023smoothquant
**Xiao G., Lin J., Seznec M., Wu H., Demouth J., Han S.** SmoothQuant: Accurate and Efficient Post-Training Quantization for Large Language Models // *ICML 2023*, PMLR 202:38087–38099. arXiv:2211.10438.
*Проверено:* страница PMLR, arXiv.
* **Метод.** Эквивалентное преобразование переносит «трудность» квантования из активаций в веса для W8A8.
* **Результаты.** До 1.56× ускорения и 2× меньше памяти при пренебрежимой потере качества. Модель 530B обслуживается на одном узле.
* **Польза.** W8A8 полезен для **prefill** (compute-bound) и почти бесполезен для decode. Хорошая иллюстрация роли фазы.

#### D3. frantar2023gptq
**Frantar E., Ashkboos S., Hoefler T., Alistarh D.** OPTQ: Accurate Quantization for Generative Pre-trained Transformers // *ICLR 2023*. На arXiv: «GPTQ: Accurate Post-Training Quantization for Generative Pre-trained Transformers», arXiv:2210.17323.
*Проверено:* страница iclr.cc (poster 10855), arXiv.
* **Метод.** One-shot квантование весов с использованием приближённой информации второго порядка (послойная OBQ-подобная схема).
* **Результаты.** 175B квантуются примерно за 4 GPU-часа до 3–4 бит. Ускорение около 3.25× на A100 и 4.5× на A6000 против FP16.
* **Польза.** Стандартный baseline weight-only PTQ. Идеи используются и в importance-matrix (imatrix) квантовании llama.cpp. Код Apache-2.0.

#### D4. lin2024awq
**Lin J., Tang J., Tang H., Yang S., Chen W.-M., Wang W.-C., Xiao G., Dang X., Gan C., Han S.** AWQ: Activation-aware Weight Quantization for On-Device LLM Compression and Acceleration // *MLSys 2024* (Best Paper). arXiv:2306.00978.
*Проверено:* proceedings.mlsys.org (через выдачу), arXiv.
* **Метод.** Защита около 1% «важных» каналов весов через масштабирование по статистике активаций, без обратного распространения. TinyChat — движок для 4-bit на edge.
* **Результаты.** Более 3× быстрее HF FP16 на десктопных и мобильных GPU. Llama-2-70B запускается на мобильном GPU (Jetson Orin).
* **Польза.** Ключевая on-device PTQ-работа. Наша модель называется «Qwen3 8B AWQ-compatible», то есть в экосистеме это используется.

#### D5. li2024evalquant
**Li S., Ning X., Wang L., Liu T., Shi X., Yan S., Dai G., Yang H., Wang Y.** Evaluating Quantized Large Language Models // *ICML 2024*, PMLR 235:28480–28524. arXiv:2402.18158.
*Проверено:* страница PMLR, arXiv.
* **Метод.** PTQ весов, активаций и KV для 11 семейств (125M–180B), 5 типов задач (базовые, emergent, trustworthiness, диалог, длинный контекст).
* **Польза.** Методологическая опора для «оценки качества после квантования». Код qllm-eval (MIT).

#### D6. kurtic2025bf16
**Kurtic E., Marques A. N., Pandit S., Kurtz M., Alistarh D.** «Give Me BF16 or Give Me Death»? Accuracy-Performance Trade-Offs in LLM Quantization // *ACL 2025 (Long Papers)*. P. 26872–26886. DOI: 10.18653/v1/2025.acl-long.1304. arXiv:2411.02355.
*Проверено:* Crossref, arXiv.
* **Результаты.** Более 500 000 оценок на семействе Llama-3.1. FP8 (W8A8-FP) практически без потерь. Хорошо настроенный INT8 теряет 1–3%. **INT4 weight-only (W4A16) конкурентен с 8 битами.** Для синхронного (однопоточного) режима W4A16 наиболее выгоден по стоимости.
* **Польза.** Обосновывает выбор 4-bit weight-only для edge (single-stream = memory-bound).

#### D7. liu2025quantreasoning
**Liu R., Sun Y., Zhang M., Bai H., Yu X., Yu T., Yuan C., Hou L.** Quantization Hurts Reasoning? An Empirical Study on Quantized Reasoning Models // *COLM 2025* (venue по комментарию авторов на arXiv). arXiv:2504.04823.
*Проверено:* arXiv. Venue по комментарию авторов, на сайте COLM не проверялось.
* **Результаты.** DeepSeek-R1-Distill (1.5B–70B), QwQ-32B, **Qwen3-8B**. W8A8 и W4A16 практически без потерь, низкие разрядности дают значимый риск. Важны размер модели, её происхождение и сложность задачи. Длина рассуждений у квантованных моделей **не растёт**.
* **Польза.** Прямо касается нашей модели (Qwen3-8B) и агентных задач, требующих рассуждений.

#### D8. kurt2026llamacpp
**Kurt U.** Which Quantization Should I Use? A Unified Evaluation of llama.cpp Quantization on Llama-3.1-8B-Instruct. arXiv:2601.14277. 2026. **Preprint.**
*Проверено:* arXiv, HTML-версия.
* **Результаты** (по HTML-версии). WikiText-2 PPL: F16 7.32, Q8_0 7.33, Q6_K 7.35, Q5_K_M 7.40, Q4_K_M 7.56, Q4_0 7.74, Q3_K_M 7.96. Средний скор (GSM8K, HellaSwag, IFEval, MMLU, TruthfulQA): F16 69.47, Q8_0 −0.06, Q5_K_M −0.11, Q6_K −0.24, **Q4_K_M −0.32**, Q3_K_M −1.40, Q4_0 −1.49 п.п. Размер Q4_K_M 4685 МиБ, в 3.3 раза меньше F16. **Замечание:** CPU-throughput на Xeon немонотонен (decode Q4_0 4.36 tok/s против 6.85 у Q5_K_M), что говорит о слабой методике замеров скорости.
* **Польза.** Единственная системная оценка k-quants на одной модели. Пример того, как **не надо** мерить скорость: аргумент в пользу нашего протокола.

#### D9. Дополнительные работы по квантованию (кратко, все проверены)
* **ashkboos2024quarot.** Ashkboos S., Mohtashami A., Croci M. L., Li B., Cameron P., Jaggi M., Alistarh D., Hoefler T., Hensman J. QuaRot: Outlier-Free 4-Bit Inference in Rotated LLMs // *NeurIPS 37*, 2024, pp. 100213–100240. arXiv:2404.00456. Вращения убирают выбросы, всё в 4 битах (W4A4KV4). LLaMa2-70B: потеря ≤0.47 PPL, сохраняется 99% zero-shot.
* **liu2025spinquant.** Liu Z., Zhao C., Fedorov I., et al. SpinQuant: LLM Quantization with Learned Rotations // *ICLR 2025* (по комментарию arXiv), arXiv:2405.16406. Вращения обучаются. Для W4A4KV4 на LLaMA-2 7B разрыв с FP 2.9 п.; на LLaMA-3 8B разрыв на 45.1% меньше, чем у QuaRot.
* **liu2024kivi.** Liu Z., Yuan J., Jin H., et al. KIVI: A Tuning-Free Asymmetric 2bit Quantization for KV Cache // *ICML 2024* (PMLR 235, страница liu24bz проверена). Ключи квантуются по каналам, значения по токенам. Пиковая память в 2.6 раза меньше, throughput выше в 2.35–3.47 раза. **Прямо связано с нашим прогнозом про KV на 32K.**
* **panferov2026dq.** Panferov A., Kleinegger M., Priyadarshi S., Blankevoort T., Alistarh D. Disaggregated Quantization: Specializing LLM Prefill and Decode. arXiv:2609.26333, 2026, **preprint**. Отдельные форматы и веса для prefill и decode. Offloaded disaggregated prefill с SSD ускоряет TTFT в 1.78 раза при промпте 8K на модели 27B **в llama.cpp**. Свежее подтверждение тезиса о разных режимах фаз.
* **davidsson2026kquant.** Davidsson R. B., Nugues P. K-Quantization and its Impact on Output Performance. arXiv:2605.19645, 2026, **preprint**. 8 моделей, Q2_K…Q8_0, MMLU-Pro/CRUXEval/MuSR. Модели 7–9B — оптимальный баланс, Q2_K иногда приемлем, иногда катастрофичен.
* **liu2024mobilellm.** Liu Z., Zhao C., Iandola F., et al. MobileLLM: Optimizing Sub-billion Parameter Language Models for On-Device Use Cases // *ICML 2024*, PMLR 235:32431–32454. Глубокие и узкие модели, общие эмбеддинги, GQA. +2.7 / +4.3 п.п. на 125M / 350M. Кандидаты в draft-модели.

### E. Speculative decoding и параллельное декодирование

#### E1. leviathan2023spec
**Leviathan Y., Kalman M., Matias Y.** Fast Inference from Transformers via Speculative Decoding // *ICML 2023* (Oral), PMLR 202:19274–19286. arXiv:2211.17192.
*Проверено:* страница PMLR, arXiv.
* **Метод.** Малая модель предлагает γ токенов, большая проверяет их за один проход. Модифицированное rejection sampling сохраняет распределение целевой модели.
* **Результаты.** T5-XXL ускоряется в 2–3 раза с идентичным выходом.
* **Польза.** Формула ожидаемого ускорения через α, γ и c (раздел 2.1) — ядро аналитической части угла A2.

#### E2. chen2023specsampling
**Chen C., Borgeaud S., Irving G., Lespiau J.-B., Sifre L., Jumper J.** Accelerating Large Language Model Decoding with Speculative Sampling. arXiv:2302.01318. 2023. **Preprint.**
*Проверено:* arXiv.
* **Результаты.** Chinchilla 70B: ускорение 2–2.5× в распределённой конфигурации. Ключевое наблюдение: параллельная оценка короткого продолжения стоит примерно как один токен. **Именно это допущение нужно проверить на M4 Pro.**

#### E3. miao2024specinfer
**Miao X., Oliaro G., Zhang Z., Cheng X., Wang Z., Zhang Z., Wong R. Y. Y., Zhu A., et al.** SpecInfer: Accelerating Large Language Model Serving with Tree-based Speculative Inference and Verification // *ASPLOS '24*, Vol. 3. P. 932–949. DOI: 10.1145/3620666.3651335. arXiv:2305.09781.
*Проверено:* Crossref, arXiv.
* **Метод.** Дерево токенов и tree-based parallel decoding (древесная маска внимания).
* **Результаты.** 1.5–2.8× для распределённого инференса, **2.6–3.5× для offloading-инференса**.
* **Польза.** Чем дороже шаг целевой модели (offload, медленная память), тем выгоднее спекуляция. Это прямо применимо к модели больше памяти на Mac.

#### E4. cai2024medusa
**Cai T., Li Y., Geng Z., Peng H., Lee J. D., Chen D., Dao T.** Medusa: Simple LLM Inference Acceleration Framework with Multiple Decoding Heads // *ICML 2024*, PMLR 235:5209–5235. arXiv:2401.10774.
*Проверено:* страница PMLR, arXiv.
* **Метод.** Дополнительные «головы» предсказывают будущие токены, проверка идёт деревом. Medusa-1 обучается на замороженной модели (без потерь), Medusa-2 обучается совместно с ней.
* **Результаты.** Medusa-1 даёт более 2.2×, Medusa-2 даёт 2.3–2.8× (PMLR) или 2.3–3.6× (arXiv v3).
* **Польза.** Draft без отдельной модели, то есть без дополнительной памяти. Минус для ВКР: нужно обучение голов, а поддержки в llama.cpp/Metal нет.

#### E5. li2024eagle / li2024eagle2 / li2025eagle3
* **Li Y., Wei F., Zhang C., Zhang H.** EAGLE: Speculative Sampling Requires Rethinking Feature Uncertainty // *ICML 2024*, PMLR 235:28935–28948. arXiv:2401.15077. Авторегрессия на уровне признаков предпоследнего слоя. LLaMA2-Chat 70B ускоряется в 2.7–3.5 раза.
* **Li Y., Wei F., Zhang C., Zhang H.** EAGLE-2: Faster Inference of Language Models with Dynamic Draft Trees // *EMNLP 2024*. P. 7421–7432. DOI: 10.18653/v1/2024.emnlp-main.422. Динамическое дерево по уверенности draft-модели: 3.05–4.26×, на 20–40% быстрее EAGLE.
* **Li Y., Wei F., Zhang C., Zhang H.** EAGLE-3: Scaling up Inference Acceleration of Large Language Models via Training-Time Test // *NeurIPS 2025*. arXiv:2503.01840. Предсказание токенов вместо признаков, слияние признаков нескольких слоёв, training-time test. До 6.5×, примерно 1.4× к EAGLE-2.
*Проверено:* PMLR, Crossref, arXiv, страница NeurIPS 2025 (через выдачу).
* **Польза.** Текущий SOTA, сильный baseline в любой related work. Практический минус: EAGLE-головы надо обучать под каждую модель, а в Metal/llama.cpp поддержка ограничена. Это нужно проверить отдельно. Код: лицензия по GitHub API не распознана (NOASSERTION), проверить LICENSE вручную.

#### E6. fu2024lookahead
**Fu Y., Bailis P., Stoica I., Zhang H.** Break the Sequential Dependency of LLM Inference Using Lookahead Decoding // *ICML 2024*, PMLR 235:14060–14079. arXiv:2402.02057.
*Проверено:* страница PMLR, arXiv.
* **Метод.** Точное параллельное декодирование без draft-модели (Jacobi-итерации и пул n-грамм): log(FLOPs) на шаг обменивается на меньшее число шагов.
* **Результаты.** До 1.8× на MT-bench, до 4× на нескольких GPU в автодополнении кода.
* **Польза.** Метод без модели и без лишней памяти, это хорошо для 24 ГБ. Использует «лишние» FLOPs memory-bound decode: при I ≈ 3.6 FLOP/байт у GPU есть запас вычислений. Код Apache-2.0.

#### E7. he2024rest и hu2025samdecoding: методы без модели (retrieval / n-gram)
* **He Z., Zhong Z., Cai T., Lee J. D., He D.** REST: Retrieval-Based Speculative Decoding // *NAACL 2024*. P. 1582–1595. DOI: 10.18653/v1/2024.naacl-long.88. Draft из хранилища n-грамм, 1.62–2.36× на 7B/13B.
* **Hu Y., Wang K., Zhang X., Zhang F., Li C., Chen H., Zhang J.** SAM Decoding: Speculative Decoding via Suffix Automaton // *ACL 2025*. DOI: 10.18653/v1/2025.acl-long.595. arXiv:2411.10666. Суффиксный автомат, O(1) на шаг. На 18% быстрее других retrieval-методов, вместе с EAGLE-2 даёт ещё +3.3–11.1%.
* **Prompt lookup decoding** (A. Saxena, 2023; github.com/apoorvumang/prompt-lookup-decoding). **Не статья**, а приём: черновик берётся из n-грамм самого промпта. Интегрирован в HF transformers и vLLM, в llama.cpp есть аналоги (`llama-lookup`). У репозитория нет файла лицензии.
*Проверено:* Crossref и OpenAlex для REST и SAM, GitHub для PLD.
* **Польза для ВКР.** **Агентные нагрузки** (вызовы инструментов, JSON, правка кода, цитирование контекста) содержат много копирования из контекста. Гипотеза для проверки: методы без модели дают на них устойчивое ускорение без дополнительной памяти. SAM — алгоритмически красивая структура данных (суффиксный автомат), её можно реализовать на C++.

#### E8. xu2023llmcad
**Xu D., Yin W., Jin X., Zhang Y., Wei S., Xu M., Liu X.** LLMCad: Fast and Scalable On-device Large Language Model Inference. arXiv:2309.04255. 2023. **Preprint.**
*Проверено:* arXiv.
* **Метод.** Малая модель в памяти строит дерево токенов, большая проверяет его. Самонастраивающийся fallback. Конвейер «вычисления — I/O» (спекуляция идёт во время подгрузки весов большой модели).
* **Результаты.** Генерация до 9.3× быстрее существующих движков.
* **Польза.** Первая on-device спекуляция, объединённая с ограничением памяти. Близкий аналог угла A2+A5.

#### E9. svirschevski2024specexec
**Svirschevski R., May A., Chen Z., Chen B., Jia Z., Ryabinin M.** SpecExec: Massively Parallel Speculative Decoding For Interactive LLM Inference on Consumer Devices // *NeurIPS 37*. 2024. P. 16342–16368. DOI: 10.52202/079017-0522. arXiv:2406.02532.
*Проверено:* Crossref, arXiv.
* **Метод.** При offloading целевая модель обрабатывает сотни токенов за то же время, что один. Строится большое дерево-«кэш» из наиболее вероятных продолжений.
* **Результаты.** До 20 токенов на итерацию. Модели 50B+ на потребительских GPU с RAM-offload: 4–6 tok/s (4-bit) и 2–3 tok/s (16-bit).
* **Польза.** Теоретически самый близкий к «потребительскому устройству» сценарий. На unified memory стоит проверить, где на кривой t_verify(K) находится Apple GPU.

#### E10. yan2025decodingsd
**Yan M., Agarwal S., Venkataraman S.** Decoding Speculative Decoding // *NAACL 2025 (Long Papers)*. P. 6460–6473. DOI: 10.18653/v1/2025.naacl-long.328. arXiv:2402.01528.
*Проверено:* Crossref, arXiv.
* **Результаты.** Более 350 экспериментов (LLaMA-65B, OPT-66B). Ускорение определяется в первую очередь **латентностью draft-модели**, а её качество как LM слабо коррелирует с выигрышем. Hardware-efficient draft-модели дают на 111% больше throughput.
* **Польза.** Обоснование выбора draft-модели по замерам на M4 Pro, а не по качеству (например, Qwen3-0.6B против 1.7B как draft для 8B).

#### E11. xia2024specsurvey
**Xia H., Yang Z., Dong Q., Wang P., Li Y., Ge T., Liu T., Li W., Sui Z.** Unlocking Efficiency in Large Language Model Inference: A Comprehensive Survey of Speculative Decoding // *Findings of ACL 2024*. P. 7655–7671. DOI: 10.18653/v1/2024.findings-acl.456. arXiv:2401.07851.
*Проверено:* Crossref, arXiv.
* **Польза.** Таксономия (выбор drafter-а, стратегия верификации) и бенчмарк **Spec-Bench** (Apache-2.0) для сопоставимых сравнений.

#### E12. cheng2024redrafter
**Cheng Y., Zhang A., Zhang X., Wang C., Wang Y.** Recurrent Drafter for Fast Speculative Decoding in Large Language Models. arXiv:2403.09919. 2024. **Preprint (Apple).**
*Проверено:* arXiv.
* **Метод.** RNN-драфтер на скрытых состояниях LLM, динамическое древесное внимание над beam search, дистилляция.
* **Результаты.** До 2.8× на H100 (PyTorch) и **до 2.3× на Metal GPU Apple Silicon (реализация на MLX)**.
* **Польза.** Единственная найденная работа с опубликованным ускорением спекуляции именно на Apple Silicon от вендора. Это верхний ориентир для угла A2.

#### E13. chordiya2026lossless
**Chordiya P.** Lossless but Not Free: An Empirical Anatomy of Speculative Decoding on Consumer Hardware. arXiv:2607.17283. 2026. **Preprint** (один автор, не рецензирован, использовать осторожно).
*Проверено:* arXiv.
* **Метод.** Собственная реализация (CUDA/MPS/CPU), 5 конфигураций draft/target на ноутбуке Apple Silicon. Эквивалентность распределений проверена тестом χ² (≈9200 токенов, p = 0.976) и точным совпадением greedy-последовательностей.
* **Результаты.** Лучшая конфигурация даёт **1.61×** при K = 6. Доля принятия падает с 69.7% (K = 1) до 37.8% в оптимуме. **3 из 5 конфигураций замедлились**: либо draft не быстрее малой целевой модели, либо квантованный Metal-бэкенд выполнял «параллельную» верификацию последовательно.
* **Польза.** Явно формулирует ключевую системную проблему угла A2 и даёт протокол проверки корректности (χ² и greedy-identity), который стоит перенять. Код открыт (лицензии нет).

#### E14. bhendawade2025mirrorsd (кратко)
**Bhendawade N., Nishu K., Kundu A., Bartels C., Cho M., Belousova I.** Mirror Speculative Decoding: Breaking the Serial Barrier in LLM Inference. arXiv:2510.13161. 2025. **Preprint (Apple).** Draft и target параллельно на гетерогенных ускорителях (GPU + NPU) плюс speculative streaming. 2.8–5.8× на серверных моделях 14–66B, +30% к EAGLE-3. Показывает направление «спекуляция × гетерогенность», пока для серверного масштаба.

### F. Фреймворки Apple (не статьи, но обязательны)

* **MLX** (ml-explore/mlx, MIT) и **mlx-lm** (MIT). Массивы в unified memory без копий, ленивые вычисления, Metal-ядра. В mlx-lm есть спекулятивная генерация с draft-моделью. Измерения: Rajesh et al. 2025 (B8). Barrios 2026, **preprint**, arXiv:2601.19139 (vllm-mlx): на M4 Max throughput на 21–87% выше llama.cpp для моделей Qwen3-0.6B…Nemotron-30B, до 525 tok/s, непрерывный батчинг даёт 4.3× суммарного throughput при 16 запросах. Ajayi & Odunayo 2025 (arXiv:2510.18921, non-archival) сравнивают только BERT-подобные модели, для LLM непригодно.
* **Core ML / ANE.** Официальный путь на Neural Engine: статические формы, ограниченный набор операторов. Справочная реализация: apple-aiml-research/ml-ane-transformers (лицензия не распознана GitHub API). Научные данные об ANE для LLM: C8 (NPUMoE) и C9 (AFM).

---

## 4. Сводка ключевых чисел

| Работа | Устройство | Модель / формат | Prefill | Decode | Энергия / память |
|---|---|---|---|---|---|
| **Наш пилот** | M4 Pro, Metal | Qwen3-8B Q4_K_M | 368.7 tok/s (pp512) | 44.5 tok/s; 39.6 при ctx 4096 | ≈208 ГБ/с эффективных (76% от 273) |
| Наш пилот | M4 Pro, CPU 8 потоков | то же | 37.9 tok/s | 22.7 tok/s | ≈106 ГБ/с эффективных |
| Benazir & Lin 2025 | M2 Ultra / M2 Max / M4 Pro | 8B–405B, 14 форматов | — | Q2_K быстрее IQ1_M; Q6_K в 1.22× медленнее Q8_0 (ядро) | ≈85% пиковой BW на M2 Ultra |
| Hübner et al. 2025 | M1–M4 (базовые) | микробенчмарки | 2.9 TFLOPS FP32 (M4 GPU) | ≤100 ГБ/с | >200 GFLOPS/Вт; 10–20 Вт |
| Remke & Breuer 2024 | M4 SME | GEMM | >2.3 TFLOPS FP32 (SME) | — | — |
| MELT 2024 | iPhone 14 Pro, S23 и др. | Zephyr-3B 4-bit и др. | — | iPhone на ≈79% быстрее Android (MLC) | 0.16–0.21 мВт·ч/токен; 13.8 Вт устойчиво |
| Xiao et al. 2026 | 7 SoC | Llama-2-7B 4-bit | GPU в 3–4× быстрее CPU (M1) | GPU почти без выигрыша | GPU 0.013 мА·ч/токен против CPU 0.10–0.14 |
| HeteroInfer 2025 | Snapdragon 8 Gen 3 | Llama-8B | 247.9 tok/s | 14.0 tok/s | BW: один исполнитель 40–45, вместе ≈60 из 68 ГБ/с; −55% энергии |
| llm.npu 2025 | смартфоны с NPU | ~1B–2B | >1000 tok/s (22.4×) | — | в 30.7× меньше энергии |
| PowerInfer-2 | смартфон | 47B (разреж.) | — | 11.68 tok/s | модель больше RAM |
| T-MAC 2025 | M2 Ultra CPU | BitNet-b1.58-3B | — | 30 (1 ядро) / 71 (8 ядер) tok/s | −70% энергии против llama.cpp |
| LLM in a flash 2024 | M1 Max | OPT-6.7B | — | I/O на токен 2196 → ≈163 мс | модель в 2× больше DRAM |
| AFM (Apple) 2024 | iPhone 15 Pro | ~3B, 3.7 bpw | 0.6 мс/токен промпта | 30 tok/s | — |
| ReDrafter 2024 | Apple Silicon (MLX) | — | — | до 2.3× (спекуляция) | — |
| Chordiya 2026 | Apple Silicon ноутбук | 5 пар draft/target | — | лучшая 1.61×; 3/5 медленнее | — |
| Kurt 2026 | Xeon CPU | Llama-3.1-8B | Q4_K_M: 87.7 tok/s | Q4_K_M: 5.1 tok/s | Q4_K_M = 4.69 ГиБ; −0.32 п.п. качества |

---

## 5. Открытые проблемы (что не закрыто литературой на 2026-10)

1. **Нет стандартизированной и воспроизводимой методики замеров на Apple Silicon.** В работах разные версии движков, нет контроля фоновой нагрузки и температуры. Kurt 2026 публикует немонотонные CPU-данные. Наш пилот показал разброс ±22 tok/s при фоновом GPU-использовании.
2. **Предсказательные модели производительности слабые для prefill и для «неровных» форматов.** Qiu et al. 2026: MAPE prefill до 108%, а «эффективности не универсальны». В моделях нет явных членов для стоимости деквантования по формату (Benazir & Lin) и для KV-трафика.
3. **Стоимость верификации в speculative decoding на квантованных Metal-ядрах.** Кривая t_verify(K) и порог переключения GEMV→GEMM в ggml-metal не охарактеризованы. Отсюда отрицательные результаты Chordiya 2026.
4. **Адаптивная спекуляция под нагрузку и тип задачи** (выбор метода draft-модель / n-gram / SAM и длины K онлайн) на одном устройстве с общей памятью. Работ мало, и они ориентированы на серверы.
5. **Совместное использование CPU + GPU + ANE на Apple.** На Snapdragon один исполнитель не насыщает шину (HeteroInfer). На M4 Pro это не измерено. ANE доступен только через Core ML (NPUMoE 2026 — единственная работа).
6. **Качество квантованных малых моделей на агентных задачах** (вызов инструментов, строгий JSON, многошаговое планирование) в форматах k-quants/IQ почти не изучено. Есть данные только по стандартным бенчмаркам и reasoning.
7. **Энергия на ноутбуке без root.** Методики измерения Дж/токен на Mac без `powermetrics` не валидированы. Утилита macmon (MIT, «no sudo») заявляет чтение мощности без root, но её точность для LLM-нагрузок не проверена против внешнего ваттметра.
8. **KV-кэш на длинном контексте у агентов.** Квантование KV (KIVI) и вытеснение/сжатие на Metal почти не измерены для single-stream. По нашей модели при ≈32K токенов KV-трафик равен трафику весов.
9. **Модели больше лимита Metal working set (≈19 ГБ из 24).** Стриминг весов и экспертов с SSD на Mac (идеи LLM in a flash, PowerInfer-2, SpecExec) в открытых движках реализован ограниченно (mmap).

---

## 6. Углы для ВКР (реализуемы одним студентом на M4 Pro / 24 ГБ за 3–5 месяцев)

Общие принципы. Каждый угол даёт **системный и низкоуровневый артефакт** (C/C++, Metal, NEON/SME, рантайм), **сильные baseline** (llama.cpp, MLX, опубликованные числа), **измеримые метрики** и **честную проверку гипотез** (допустим и отрицательный результат, как NO_ADAPTIVE_ADVANTAGE_ESTABLISHED в legacy-работе). Тема руководителя «ИИ-агенты для Edge AI» закрывается так: агентная нагрузка задаёт сценарии и профиль запросов, а системная часть оптимизирует её исполнение на устройстве.

### Угол A1 (фундамент). Калиброванная модель производительности и протокол измерений инференса LLM на Apple Silicon
* **Идея.** Измерить эмпирические «крыши» M4 Pro. Для BW: STREAM-подобные ядра на C++/NEON для CPU и на Metal для GPU, одиночно и одновременно. Для FLOPS: GEMM на Metal, Accelerate/AMX, SME. Построить пооперационную roofline-модель decode и prefill для форматов GGUF (члены: W_eff, KV(ctx), стоимость деквантования для формата, порог GEMV/GEMM) и проверить её на ≥40 конфигурациях: модели 0.6B–14B, Q2_K…Q8_0 и IQ, контекст 0–32K, CPU/GPU, llama.cpp и MLX.
* **Осуществимость на M4 Pro.** Высокая. Пилот уже показал ошибку 0.4% на одной точке. Нужны только llama-bench, mlx-lm и собственные микробенчмарки (C++/Metal, CMake). Модели 0.6B–14B в Q4 помещаются в лимит Metal.
* **Baseline.** Наивная граница size/BW; предиктор Qiu et al. 2026 (MAPE decode 13–36%, prefill 19–108%); LLM-Viewer (Yuan et al.); числа Benazir & Lin для M4 Pro.
* **Метрики.** MAPE предсказания tok/s (цель: decode <10%, prefill <20%); доля пиковой BW (MBU); стабильность (CV по 5+ прогонам); Дж/токен через macmon с проверкой внешним USB-C ваттметром.
* **Риски.** Новизна «измерительная»: нужно оформить как проверку гипотез (H1: η не зависит от контекста; H2: член деквантования объясняет немонотонность форматов; H3: CPU+GPU вместе не увеличивают BW). Шум от фоновых процессов требует протокола: фиксированная температура, `taskpolicy`, закрытые приложения, прогрев, порядок прогонов с перемешиванием. Версии движков быстро меняются, поэтому нужно зафиксировать коммиты.

### Угол A2 (основной кандидат). Спекулятивное декодирование на устройстве с unified memory: стоимость верификации и адаптивный выбор метода и длины черновика для агентных нагрузок
* **Идея.** (1) Измерить t_verify(K) для Q4_K/Q8_0 на Metal и CPU и найти, где нарушается допущение Leviathan/Chen. (2) Реализовать на C++ поверх llama.cpp API контроллер, который онлайн оценивает α по окну, выбирает K и метод (draft-модель Qwen3-0.6B/1.7B, prompt-lookup или SAM-автомат) по обобщённой формуле S из раздела 2.1. (3) Нагрузки: чат (подмножество MT-Bench / Spec-Bench), правка кода, агентные трассы вызовов инструментов с JSON.
* **Осуществимость.** Высокая и средняя. Qwen3-8B и Qwen3-0.6B вместе помещаются в память. В llama.cpp уже есть `llama-speculative` и `llama-lookup`, поэтому не нужно писать всё с нуля. Суффиксный автомат — 300–500 строк C++.
* **Baseline.** Авторегрессия; llama.cpp speculative с фиксированным K; prompt-lookup; mlx-lm с draft-моделью; опубликованные ориентиры ReDrafter (до 2.3× на Apple) и Chordiya (1.61×).
* **Метрики.** Ускорение tok/s (p50/p95), α, среднее число принятых токенов, Дж/токен, дополнительная память. **Корректность:** точное совпадение greedy-выхода и χ²-тест распределений при sampling (протокол Chordiya).
* **Риски.** Выигрыш может оказаться ≤1.3× или отрицательным. Это всё равно результат, если объяснён через t_verify(K). EAGLE/Medusa требуют обучения голов и, вероятно, недоступны на Metal, поэтому их нужно честно указать как «вне рамок» или сравнить по опубликованным числам. Конкуренция с бурно развивающейся реализацией в llama.cpp: вклад должен быть в анализе и контроллере, а не в «ещё одной реализации».

### Угол A3 (самый low-level). Ядра low-bit mpGEMV/mpGEMM для CPU Apple M4: NEON-dotprod/I8MM против SME2 против LUT (T-MAC)
* **Идея.** Написать на C++ (intrinsics или asm) ядра для Q4/Q2/тернарных весов: (а) деквантование + I8MM, (б) LUT-подход T-MAC, (в) outer-product на SME/SME2 (`SME_I8I32`, `SME_F16F32` подтверждены на машине). Сравнить с ядрами ggml-cpu (включая опциональную интеграцию KleidiAI, есть в ggml) и с T-MAC. Встроить лучшее ядро в ggml и измерить end-to-end tok/s.
* **Осуществимость.** Средняя. Требует уверенного владения SIMD, документации Arm SME и аккуратных микробенчмарков (опора — Remke & Breuer). Объём кода умеренный.
* **Baseline.** ggml-cpu (NEON, I8MM, сборка apple_m4), T-MAC (MIT), bitnet.cpp (MIT), Accelerate.
* **Метрики.** ГБ/с (доля от CPU-BW ≈106 ГБ/с из пилота), GFLOPS, tok/s decode и prefill на CPU, энергия, точность (побитовое совпадение с референсом).
* **Риски.** CPU-decode на Mac всё равно уступает GPU (22.7 против 44.5 tok/s). Ценность обосновывается сценариями «GPU занят» (A4) или энергией. Streaming-mode SME имеет накладные расходы на вход и выход. Работа может свестись к повторению KleidiAI.

### Угол A4 (связка с темой «агенты»). Ресурсно-осведомлённый агент-контроллер локального LLM-ассистента
* **Идея.** Агент (не LLM, а детерминированный контроллер с измеряемой обратной связью — продолжение legacy-ветки B, но на реальной LLM-нагрузке) выбирает для каждого запроса конфигурацию: формат весов (Q4_K_M/Q8_0), исполнитель (GPU/CPU), спекуляция вкл./выкл., K, квантование и обрезка KV. Выбор делается по cost-model из A1 при SLO на латентность и энергию в условиях **конкуренции** (видео-декодер, второй процесс, нагрев). Наш пилот показал, что фоновая нагрузка роняет decode с 44.5 до 16–31 tok/s с огромным разбросом.
* **Осуществимость.** Средняя. Основа (A1) нужна в любом случае. Контроллер пишется на C++ или Python поверх llama-server и llama.cpp API.
* **Baseline.** Лучшая статическая конфигурация; простые правила; оракул (ретроспективно лучший выбор); при необходимости простой бандит (UCB) как «обучаемый» baseline.
* **Метрики.** Доля нарушений SLO, p95 латентности, Дж/запрос, потеря качества (лёгкий eval), накладные расходы контроллера.
* **Риски.** Повторить NO_ADAPTIVE_ADVANTAGE: если статическая конфигурация почти всегда лучше, адаптивности нечего выигрывать. Сценарии конкуренции нужно спроектировать заранее и честно, без подгонки. Есть риск размытия темы: агент должен быть инженерным контроллером с формальной постановкой.

### Угол A5 (амбициозный, опциональный). Модель больше лимита Metal: стриминг весов и экспертов с SSD для MoE на Mac
* **Идея.** Для MoE-моделей класса 30B-A3B, которые на грани лимита ≈19 ГБ, реализовать кэш экспертов в DRAM с подгрузкой с SSD, предсказателем и конвейером I/O–вычисления (идеи C1, C3, E3, E9). Скомбинировать со спекуляцией: верификация дешевле, когда шаг целевой модели дорогой.
* **Риски.** Высокая сложность. Нужно точно измерить SSD M4 Pro и поведение mmap/page cache macOS. Результат сильно зависит от модели. Рекомендуется только как расширение A1 или A2.

**Рекомендация.** Взять **A1 как обязательный фундамент** (1–1.5 месяца, даёт главу «модель производительности и методика») плюс **A2 как основной вклад**. Если руководитель требует явной «агентности», вместо A2 взять **A4**: оба опираются на cost-model из A1. A3 подходит, если студент хочет максимально low-level-работу и готов принять риск, что CPU-ядра не превзойдут GPU-путь по скорости.

---

## 7. Протокол измерений (чек-лист для любого угла)

1. Фиксировать: коммит llama.cpp (сейчас b11146 / 7fe450e19, ggml 0.25.3 из Homebrew), версию MLX, macOS, хэши GGUF-файлов.
2. Перед замером: питание от сети, закрыть приложения с GPU-нагрузкой (браузер с видео, VPN-туннель отмечен как активный процесс), прогрев 1–2 прогона, пауза для охлаждения между сериями, лог температуры и мощности (macmon).
3. Не меньше 5 повторов, перемешанный порядок конфигураций, медиана и межквартильный размах. Отчёт по CV. Прогоны с CV >5% отбрасывать или повторять.
4. Раздельно мерить pp (prefill) и tg (decode), tg при нескольких глубинах контекста (`-d`), TTFT и end-to-end для реальных промптов.
5. Качество: lm-evaluation-harness (MIT) на фиксированном подмножестве + PPL (`llama-perplexity`) + агентный eval (точность вызова инструментов и валидность JSON).
6. Энергия: macmon (без sudo) и проверка внешним USB-C ваттметром на 2–3 точках. Указывать Дж/токен отдельно для prefill и decode.
7. Для спекуляции: проверка эквивалентности (greedy-identity и χ²) по Chordiya 2026; логирование α по позиции в черновике.

---

## 8. Инструменты, код и бенчмарки (лицензии по GitHub API на 2026-10-05)

| Инструмент | URL | Лицензия | Для чего |
|---|---|---|---|
| llama.cpp / ggml | github.com/ggml-org/llama.cpp | MIT | Основной движок: `llama-bench`, `llama-batched-bench`, `llama-speculative`, `llama-lookup`, `llama-perplexity`, GGUF; Metal, CPU (NEON/I8MM, опционально KleidiAI) |
| MLX / mlx-lm | github.com/ml-explore/mlx, …/mlx-lm | MIT / MIT | Второй бэкенд для сравнения; спекуляция с draft-моделью |
| LLM-Viewer | github.com/hahnyuan/LLM-Viewer | MIT | Аналитический roofline-baseline (Yuan et al.) |
| T-MAC | github.com/microsoft/T-MAC | MIT | LUT-ядра, baseline для A3 |
| bitnet.cpp | github.com/microsoft/BitNet | MIT | Тернарные ядра TL/I2_S |
| PowerInfer | github.com/Tiiny-AI/PowerInfer (бывш. SJTU-IPADS) | MIT | Hot/cold-нейроны, идеи для A5 |
| mllm | github.com/UbiquitousLearning/mllm | MIT | Движок llm.npu (мобильные NPU) |
| EAGLE (1/2/3) | github.com/SafeAILab/EAGLE | не распознана (NOASSERTION), проверить вручную | SOTA-спекуляция (CUDA) |
| Medusa | github.com/FasterDecoding/Medusa | Apache-2.0 | Головы-драфтеры |
| LookaheadDecoding | github.com/hao-ai-lab/LookaheadDecoding | Apache-2.0 | Спекуляция без draft-модели |
| Spec-Bench | github.com/hemingkx/Spec-Bench | Apache-2.0 | Стандартный бенчмарк спекуляции |
| prompt-lookup-decoding | github.com/apoorvumang/prompt-lookup-decoding | нет файла лицензии | Идея PLD (код не копировать) |
| speculative_decoding_engine (Chordiya) | github.com/ParamChordiya/speculative_decoding_engine | нет лицензии | Протокол проверки эквивалентности (только как идея) |
| llm-awq | github.com/mit-han-lab/llm-awq | MIT | AWQ / TinyChat |
| GPTQ | github.com/IST-DASLab/gptq | Apache-2.0 | GPTQ/OPTQ |
| qllm-eval | github.com/thu-nics/qllm-eval | MIT | Оценка квантованных LLM (Li et al. ICML'24) |
| lm-evaluation-harness | github.com/EleutherAI/lm-evaluation-harness | MIT | Оценка качества |
| MELT | github.com/brave-experiments/MELT-public | не распознана (NOASSERTION) | Образец инфраструктуры замеров |
| macmon | github.com/vladkens/macmon | MIT | Мощность, температура и частоты Apple Silicon **без sudo** (установка `brew install macmon`; на машине пока не установлен) |
| asitop | github.com/tlkh/asitop | MIT | Монитор Apple Silicon (работает через powermetrics, требует sudo, нам не подходит) |
| ml-ane-transformers | github.com/apple-aiml-research/ml-ane-transformers | не распознана | Transformer под ANE (Core ML) |
| Xcode Instruments / Metal System Trace (`xctrace`) | developer.apple.com | проприетарная, бесплатная | Профилирование GPU-ядер, счётчики Metal (как у Benazir & Lin) |
| k-quants (первоисточник формата) | github.com/ggml-org/llama.cpp/pull/1684 | MIT (часть llama.cpp) | Описание форматов Q2_K…Q6_K |

Модели для экспериментов (открытые веса): Qwen3-0.6B / 1.7B / 4B / 8B (8B уже лежит локально в Q4_K_M), Llama-3.2-1B/3B, Llama-3.1-8B (для сравнения с Kurt 2026 и Kurtic 2025), Gemma-3-1B/4B.

---

## 9. Список литературы (ключи → запись)

Проверенные рецензируемые:
1. williams2009roofline — CACM 52(4):65–76, 2009. DOI 10.1145/1498765.1498785
2. pope2023scaling — MLSys 5, 2023. arXiv:2211.05102
3. patel2024splitwise — ISCA 2024, pp. 118–132. DOI 10.1109/ISCA59077.2024.00019
4. laskaridis2024melt — MobiCom '24, pp. 890–907. DOI 10.1145/3636534.3690668
5. li2024edgefm — EdgeFM '24 (MobiSys WS), pp. 1–6. DOI 10.1145/3662006.3662059
6. xiao2026pockets — IEEE TMC 25(8):13077–13094, 2026. DOI 10.1109/TMC.2026.3674827
7. benazir2025apple — POMACS 9(3), 2025. DOI 10.1145/3771563
8. hubner2025apple — IPDPSW 2025, pp. 45–54. DOI 10.1109/IPDPSW66978.2025.00013
9. remke2024hellosme — SC24-W, pp. 1443–1454. DOI 10.1109/SCW63240.2024.00185
10. alizadeh2024flash — ACL 2024, pp. 12562–12584. DOI 10.18653/v1/2024.acl-long.678
11. song2024powerinfer — SOSP '24, pp. 590–606. DOI 10.1145/3694715.3695964
12. xu2025llmnpu — ASPLOS '25, pp. 445–462. DOI 10.1145/3669940.3707239
13. chen2025heteroinfer — SOSP '25, pp. 359–374. DOI 10.1145/3731569.3764808
14. wei2025tmac — EuroSys '25, pp. 278–292. DOI 10.1145/3689031.3696099
15. wang2025bitnetcpp — ACL 2025, pp. 9305–9322. DOI 10.18653/v1/2025.acl-long.457
16. dettmers2022int8 — NeurIPS 35, pp. 30318–30332, 2022
17. xiao2023smoothquant — ICML 2023, PMLR 202:38087–38099
18. frantar2023gptq (OPTQ) — ICLR 2023
19. lin2024awq — MLSys 2024 (Best Paper)
20. li2024evalquant — ICML 2024, PMLR 235:28480–28524
21. kurtic2025bf16 — ACL 2025, pp. 26872–26886. DOI 10.18653/v1/2025.acl-long.1304
22. liu2025quantreasoning — COLM 2025 (по arXiv-комментарию)
23. ashkboos2024quarot — NeurIPS 37, pp. 100213–100240
24. liu2025spinquant — ICLR 2025 (по arXiv-комментарию)
25. liu2024kivi — ICML 2024 (PMLR 235)
26. liu2024mobilellm — ICML 2024, PMLR 235:32431–32454
27. leviathan2023spec — ICML 2023, PMLR 202:19274–19286
28. miao2024specinfer — ASPLOS '24 Vol. 3, pp. 932–949. DOI 10.1145/3620666.3651335
29. cai2024medusa — ICML 2024, PMLR 235:5209–5235
30. li2024eagle — ICML 2024, PMLR 235:28935–28948
31. li2024eagle2 — EMNLP 2024, pp. 7421–7432. DOI 10.18653/v1/2024.emnlp-main.422
32. li2025eagle3 — NeurIPS 2025. arXiv:2503.01840
33. fu2024lookahead — ICML 2024, PMLR 235:14060–14079
34. he2024rest — NAACL 2024, pp. 1582–1595. DOI 10.18653/v1/2024.naacl-long.88
35. hu2025samdecoding — ACL 2025. DOI 10.18653/v1/2025.acl-long.595
36. svirschevski2024specexec — NeurIPS 37, pp. 16342–16368. DOI 10.52202/079017-0522
37. yan2025decodingsd — NAACL 2025, pp. 6460–6473. DOI 10.18653/v1/2025.naacl-long.328
38. xia2024specsurvey — Findings ACL 2024, pp. 7655–7671. DOI 10.18653/v1/2024.findings-acl.456

Проверенные препринты и технические отчёты:
39. yuan2024unveiled — arXiv:2402.16363
40. lu2024slm — arXiv:2409.15790
41. xue2024powerinfer2 — arXiv:2406.06282
42. xu2023llmcad — arXiv:2309.04255
43. chen2023specsampling — arXiv:2302.01318
44. cheng2024redrafter — arXiv:2403.09919 (Apple)
45. bhendawade2025mirrorsd — arXiv:2510.13161 (Apple)
46. gunter2024afm — arXiv:2407.21075 (Apple)
47. rajesh2025applesilicon — arXiv:2511.05502
48. qiu2026ggufpred — arXiv:2609.14864
49. chordiya2026lossless — arXiv:2607.17283
50. kurt2026llamacpp — arXiv:2601.14277
51. davidsson2026kquant — arXiv:2605.19645
52. panferov2026dq — arXiv:2609.26333
53. benazir2026npumoe — arXiv:2604.18788
54. husom2025sustainable — arXiv:2504.03360
55. barrios2026vllmmlx — arXiv:2601.19139

Нерецензируемые первоисточники форматов и инструментов: llama.cpp PR #1684 (k-quants, 2023); блог Apple ML Research «Introducing Apple's On-Device and Server Foundation Models» (2024); пресс-релиз Apple об M4 Pro (273 ГБ/с; newsroom, окт. 2024).
