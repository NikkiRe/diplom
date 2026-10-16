# Ключевые источники ВКР (проверенный список)

**Дата проверки:** 06.10.2026. Редакция 2: обновлены записи 7, 9, 15, 18, 20, 21, 45–47, 52, 54–57; добавлен раздел Е — источники из разборов red team, каждый проверен по первоисточнику.
**Оформление:** близко к ГОСТ Р 7.0.100-2018. Правило заголовка по числу авторов:
- 1–3 автора — описание начинается с первого автора;
- 4 автора — с заглавия, все авторы после косой черты;
- 5 и больше — с заглавия, первые три автора и «[et al.]».

Электронные ресурсы оформлены с URL и датой обращения. Для ПЗ этот список нужно довести до полного ГОСТ (тире-разделители, «Текст : электронный» и т. д.) и упорядочить по ходу ссылок в тексте.

**Пометки «Проверка»:**
- **arXiv API** — заглавие, авторы, дата и комментарий сверены по `export.arxiv.org/api/query`;
- **Crossref** — сверены по `api.crossref.org/works/<DOI>`: том, выпуск, страницы, год;
- **Zenodo API**, **GitHub API/код** — аналогично по соответствующим сервисам;
- **заметка X** — проверял предыдущий агент, в этой сессии повторно не открывалось. Перед вставкой в ПЗ такие пункты стоит сверить по первоисточнику.

Все «Проверка: …, 06.10» выполнены в этой сессии.

---

## А. Научные публикации (журналы и конференции)

1. Edge Intelligence: Paving the Last Mile of Artificial Intelligence With Edge Computing / Z. Zhou, X. Chen, E. Li [et al.] // Proceedings of the IEEE. – 2019. – Vol. 107, No. 8. – P. 1738–1762. – DOI 10.1109/JPROC.2019.2918951.
   *Берём:* определение Edge Intelligence и шкалу из шести уровней (от совместного вывода «облако — граница» до «всё на устройстве») — ответ на вопрос «где граничный ИИ» (THESIS_CONCEPT §3). *Проверка:* Crossref, 06.10; содержание шкалы — заметка R9 §1.

2. Wooldridge, M. Intelligent agents: theory and practice / M. Wooldridge, N. R. Jennings // The Knowledge Engineering Review. – 1995. – Vol. 10, No. 2. – P. 115–152. – DOI 10.1017/S0269888900008122.
   *Берём:* «слабое» определение агента (автономность, реактивность, проактивность, социальность) для главы 1. *Проверка:* Crossref, 06.10.

3. Brooks, R. A. A robust layered control system for a mobile robot / R. A. Brooks // IEEE Journal on Robotics and Automation. – 1986. – Vol. 2, No. 1. – P. 14–23. – DOI 10.1109/JRA.1986.1087032.
   *Берём:* слоистость: нижние слои работают без верхних, отсюда слой L0 на плате. *Проверка:* Crossref, 06.10. Вместе с ним цитируется Gat E. On three-layer architectures (1998, глава книги AAAI Press/MIT Press) — по заметке G, DOI нет.

4. Understanding the Long-Term Use of Smart Speaker Assistants / F. Bentley, C. Luvogt, M. Silverman [et al.] // Proceedings of the ACM on Interactive, Mobile, Wearable and Ubiquitous Technologies. – 2018. – Vol. 2, No. 3. – Art. 91. – P. 1–24. – DOI 10.1145/3264901.
   *Берём:*
   - 88 домов, 65 499 команд, медиана 4,1 команды в сутки;
   - кластеры Light / Medium / Super Users (2,61 / 5,55 / 30,44 команды в сутки) — профили нагрузки L / M / H;
   - граница сессии 10 мин.

   *Проверка:* Crossref, 06.10; числа — заметка GAP1 §2.1.

5. Music, Search, and IoT: How People (Really) Use Voice Assistants / T. Ammari, J. Kaye, J. Y. Tsai, F. Bentley // ACM Transactions on Computer-Human Interaction. – 2019. – Vol. 26, No. 3. – Art. 17. – P. 1–28. – DOI 10.1145/3311956.
   *Берём:* интенсивность по активным дням и долю IoT-команд (≈10–17 %), чтобы обосновать «пустые» дни. *Проверка:* Crossref, 06.10; числа — заметка GAP1 §2.1.

6. CASAS: A Smart Home in a Box / D. J. Cook, A. S. Crandall, B. L. Thomas, N. C. Krishnan // Computer. – 2013. – Vol. 46, No. 7. – P. 62–69. – DOI 10.1109/MC.2012.328.
   *Берём:* описание проекта CASAS — источника трасс датчиков (см. В.1). *Проверка:* Crossref, 06.10.

7. Garetto, M. A Unified Approach to the Performance Analysis of Caching Systems / M. Garetto, E. Leonardi, V. Martina // ACM Transactions on Modeling and Performance Evaluation of Computing Systems. – 2016. – Vol. 1, No. 3. – P. 1–28. – DOI 10.1145/2896380.
   *Берём:* модель TTL-кэша с продлением (p_hit = F(T)) — основа анализа keep-warm и расширения X-T1 (в ядро T1 больше не входит). *Проверка:* Crossref, 06.10; препринт arXiv:1307.6702 (arXiv API, 06.10).

8. A comprehensive study on software aging across Android versions and vendors / D. Cotroneo, A. K. Iannillo, R. Natella, R. Pietrantuono // Empirical Software Engineering. – 2020. – Vol. 25, No. 5. – P. 3357–3395. – DOI 10.1007/s10664-020-09838-3.
   *Берём:* рецепт анализа трендов ресурсов в многочасовых прогонах: Манн–Кендалл, Хамед–Рао, наклон Сена. Только как статистику рядов «работы во времени», не как тему надёжности. *Проверка:* Crossref и arXiv:2005.11523, 06.10.

9. Hamed, K. H. A modified Mann–Kendall trend test for autocorrelated data / K. H. Hamed, A. R. Rao // Journal of Hydrology. – 1998. – Vol. 204, No. 1–4. – P. 182–196. – DOI 10.1016/S0022-1694(97)00125-X.
   *Берём:* тест тренда при автокорреляции — только разведочно. Основной критерий для PSS(t) — TOST на наклоне Сена: симуляция red team показала, что «тренд незначим» при n = 960 ложно отвергается безобидным ростом. *Проверка:* Crossref, 06.10.

10. Sen, P. K. Estimates of the Regression Coefficient Based on Kendall's Tau / P. K. Sen // Journal of the American Statistical Association. – 1968. – Vol. 63, No. 324. – P. 1379–1389. – DOI 10.1080/01621459.1968.10480934.
    *Берём:* наклон Сена с ДИ (МиБ/ч). *Проверка:* Crossref, 06.10.

11. Schmeiser, B. Batch Size Effects in the Analysis of Simulation Output / B. Schmeiser // Operations Research. – 1982. – Vol. 30, No. 3. – P. 556–568. – DOI 10.1287/opre.30.3.556.
    *Берём:* батч-средние для ДИ по коррелированным рядам (мощность, задержка). *Проверка:* Crossref, 06.10.

12. Plug in the Safety Chip: Enforcing Constraints for LLM-driven Robot Agents / Z. Yang, S. S. Raman, A. Shah, S. Tellex // 2024 IEEE International Conference on Robotics and Automation (ICRA). – 2024. – P. 14435–14442. – DOI 10.1109/ICRA57147.2024.10611447.
    *Берём:* правила из промпта соблюдаются неполностью (54–62 % по заметке GAP3), отсюда детерминированный шлюз приводов. *Проверка:* Crossref, 06.10.

13. IoT-MCP: Bridging LLMs and IoT Systems Through Model Context Protocol / N. Yang, G. Lyu, M. Ma [et al.] // Proceedings of the ACM Workshop on Wireless Network Testbeds, Experimental evaluation & Characterization (WiNTECH '25). – 2025. – P. 73–80. – DOI 10.1145/3737895.3768303.
    *Берём:* MCP-серверы на граничных узлах; 22 типа датчиков; 114 + 1 140 задач; 205 мс; 74 КБ; 12-часовой прогон стабильности (заметка GAP1 §8.9). Ближайший аналог «MCP для датчиков». *Проверка:* Crossref и arXiv:2510.01260 (аннотация), 06.10.

14. Rafique, M. ClawVM: Harness-Managed Virtual Memory for Stateful Tool-Using LLM Agents / M. Rafique, L. Bindschaedler // Proceedings of the Sixth European Workshop on Machine Learning and Systems (EuroMLSys '26). – 2026. – P. 1–12. – DOI 10.1145/3805621.3807648.
    *Берём:* «виртуальная память» для контекста агента — родственная работа к управлению контекстом. *Проверка:* Crossref, 06.10.

15. Accurate Calibration of Power Measurements from Internal Power Sensors on NVIDIA Jetson Devices / N. Shalavi, A. Khoshsirat, M. Stellini [et al.] // 2023 IEEE International Conference on Edge Computing and Communications (EDGE). – 2023. – P. 166–170. – DOI 10.1109/EDGE60047.2023.00034.
    *Берём:* INA3221 на Jetson занижает до 50 %, после линейной калибровки ±3 % (заметка G6 B3) — только для расширения X-nano. *Проверка:* Crossref, 06.10.

16. MASSIVE: A 1M-Example Multilingual Natural Language Understanding Dataset with 51 Typologically-Diverse Languages / J. FitzGerald, C. Hench, C. Peris [et al.] // Proceedings of the 61st Annual Meeting of the Association for Computational Linguistics (Volume 1: Long Papers). – 2023. – P. 4277–4302. – DOI 10.18653/v1/2023.acl-long.235.
    *Берём:* 220 параллельных пар ru/en команд сценария iot — шаблоны набора задач S-room и E0-Q (данные в `GAP4_artifacts/data/`). *Проверка:* Crossref, 06.10.

17. MASSIVE-Agents: A Benchmark for Multilingual Function-Calling in 52 Languages / M. Kulkarni, V. Mazzia, J. Gaspers [et al.] // Findings of the Association for Computational Linguistics: EMNLP 2025. – 2025. – ACL Anthology 2025.findings-emnlp.1099.
    *Берём:* формат BFCL на 52 языках; на русском падает AST-точность (заметка GAP4 §0 п. 6–7). *Проверка:* заметка GAP4 [V].

18. The Berkeley Function Calling Leaderboard (BFCL): From Tool Use to Agentic Evaluation of Large Language Models / S. G. Patil, H. Mao, F. Yan [et al.] // Proceedings of the 42nd International Conference on Machine Learning (ICML 2025). – PMLR, 2025. – Vol. 267. – P. 48371–48392.
    *Берём:* в редакции 2 в ядро не входит: BFCL убран из E0-Q как не относящийся к вопросу исследования. Данные и проверщики остаются в `data/bfcl` и `third_party/gorilla` как инструмент. *Проверка:* заметка R7 A2 (PMLR + OpenReview).

19. AI Agents That Matter / S. Kapoor, B. Stroebl, Z. S. Siegel [et al.] // Transactions on Machine Learning Research. – 2025. – arXiv:2407.01502.
    *Берём:* совместная оценка агентов по точности и стоимости; простые baseline обязательны (B4, B5). *Проверка:* arXiv API, 06.10; площадка TMLR — заметка R7 (OpenReview).

20. Cost-of-Pass: An Economic Framework for Evaluating Language Models / M. H. Erol, B. El, M. Suzgun [et al.] // International Conference on Learning Representations (ICLR 2026). – 2026. – arXiv:2504.13359.
    *Берём:* метрику «₽ на успешное действие» для калибровки выбора модели в E0-Q (бывшая H8 снята как гипотеза). *Проверка:* arXiv API, 06.10; площадка — заметка E (PDF: «Published as a conference paper at ICLR 2026»).

21. SimuHome: A Temporal- and Environment-Aware Benchmark for Smart Home LLM Agents / G. Seo, J. Yang, J. Pyo [et al.] // International Conference on Learning Representations (ICLR 2026, Oral). – 2026. – arXiv:2509.24282.
    *Берём:*
    - тиковую модель среды и проверку по финальному состоянию устройств;
    - GPT-4.1-nano: 0–6 % успеха на временных задачах; GPT-4.1-mini: 10–40 % (заметка C).

    *Почему не берём сам SimuHome:* лицензия репозитория holi-lab/SimuHome — CC BY-NC-ND 4.0, производные работы запрещены; нет кэша провайдера и TTL (README, проверено 06.10). Отсюда свой `sim-mcp`.

    *Проверка:* arXiv API (комментарий «Accepted at ICLR 2026 (Oral)»), 06.10.

22. HomeBench: Evaluating LLMs in Smart Homes with Valid and Invalid Instructions Across Single and Multiple Devices / S. Li, Y. Guo, J. Yao [et al.] // Proceedings of the 63rd Annual Meeting of the Association for Computational Linguistics (ACL 2025, Main). – 2025. – arXiv:2505.19628.
    *Берём:* классы задач (валидные / невалидные, одно- и мультиустройственные) для набора S-room. *Проверка:* arXiv API («Accepted by ACL 2025 Main»), 06.10.

23. AIOS: LLM Agent Operating System / K. Mei, X. Zhu, W. Xu [et al.] // Conference on Language Modeling (COLM 2025). – 2025. – arXiv:2403.16971.
    *Берём:* «ОС для агентов» как соседнее направление (глава 1). *Проверка:* arXiv API («Published as a full paper at COLM 2025»), 06.10.

24. Agentic Context Engineering: Evolving Contexts for Self-Improving Language Models / Q. Zhang, C. Hu, S. Upasani [et al.] // International Conference on Learning Representations (ICLR 2026). – 2026. – arXiv:2510.04618.
    *Берём:* соседнюю работу по инженерии контекста. *Проверка:* arXiv API («ICLR 2026»), 06.10.

25. The Complexity Trap: Simple Observation Masking Is as Efficient as LLM Summarization for Agent Context Management / T. Lindenbauer, I. Slinko, L. Felder [et al.] // 4th Deep Learning for Code Workshop (DL4C). – 2025. – arXiv:2508.21433.
    *Берём:* политику истории «маскирование» как кандидата в E3. *Проверка:* arXiv API (v3 — DL4C camera-ready), 06.10.

26. Bajrami, A. How Long Until Your Robot Ignores You? A Safety Benchmark for LLM Orchestrators in Human-Humanoid Collaboration / A. Bajrami, M. Elshamouty, W. Kraus // CBS 2026. – 2026. – arXiv:2609.07288.
    *Берём:* GPT-4o-mini допускает до 13 нарушений за сессию на MCP-архитектуре; управление контекстом (окно против полного) меняет профиль нарушений. Отсюда безопасность в шлюзе, а не в промпте. *Проверка:* arXiv API (аннотация), 06.10.

## Б. Препринты

27. Salman, A. Resource Constraints and Performance in Agentic AI Systems : preprint / A. Salman, M. Halgamuge, T. Susnjak. – 2026. – arXiv:2608.27886. – URL: https://arxiv.org/abs/2608.27886 (дата обращения: 06.10.2026).
    *Берём:* ближайший аналог — OpenClaw против NanoBot как систем целиком. По аннотации: геометрическое среднее отношения пиковой памяти 19,44, времени — 2,98. Медианы пика 2 926,6 / 136,1 МиБ (заметка A S15). Отличие нашей работы: плата, многочасовые прогоны, токены и ₽. *Проверка:* arXiv API, 06.10.

28. Dehigaspitiyage Don, S. Y. Benchmarking Lightweight AI-Agent Frameworks on a 2 GB Raspberry Pi 4: Framework Overhead, Offline Feasibility, and Fleet Density : preprint / S. Y. Dehigaspitiyage Don. – Zenodo, 2026. – DOI 10.5281/zenodo.20593332.
    *Берём:* ZeroClaw, PicoClaw, NanoBot и smolagents на Pi 4 2 ГБ при одной модели. Пиковый RSS: PicoClaw ≈31 МБ, NanoBot ≈130 МБ (заметка AGENT_LANDSCAPE §3.3). Опровергает формулировку «PicoClaw на плате никто не мерил». *Проверка:* Zenodo API (препринт, 08.06.2026, CC-BY-4.0), 06.10.

29. VisionClaw: Always-On AI Agents through Smart Glasses : preprint / X. Liu, D. Lee, E. J. Gonzalez [et al.]. – 2026. – arXiv:2604.03486.
    *Берём:*
    - 10,1 взаимодействия за активный день, максимум 69;
    - 3,2 вызова инструмента на команду и распределение k (заметка GAP1 §2.1);
    - выборка N = 4–5, автобиографическая.

    *Проверка:* arXiv API, 06.10.

30. ROSClaw: An OpenClaw ROS 2 Framework for Agentic Robot Control and Interaction : preprint / I. S. Cardenas, M. A. Arnett, N. C. Yeo [et al.]. – 2026. – arXiv:2603.26997.
    *Берём:* OpenClaw для ROS 2; ход облачной LLM 1,3–2,3 с, P95 ≤ 3,8 с (заметка A S16); манифест «всё в промпте» как baseline R1. *Проверка:* arXiv API, 06.10.

31. Don't Break the Cache: An Evaluation of Prompt Caching for Long-Horizon Agentic Tasks : preprint / E. Lumer, F. Nizar, A. Jangiti [et al.]. – 2026. – arXiv:2601.06007.
    *Берём:* фиксированный порядок сегментов промпта ради кэша. *Проверка:* arXiv API, 06.10.

32. What Limits Agentic Systems Efficiency? : preprint / S. Bian, M. Yan, A. Jayarajan [et al.]. – 2025. – arXiv:2510.16276.
    *Берём:* нестационарность задержки API (до 69,21×) — стратификация по часу и ABAB. *Проверка:* arXiv API, 06.10.

33. Tschisgale, P. Daily and Weekly Periodicity in Large Language Model Performance and Its Implications for Research : preprint / P. Tschisgale, P. Wulff. – 2026. – arXiv:2602.15889.
    *Берём:* покрытие будней и выходных, разных часов. *Проверка:* arXiv API, 06.10.

34. Oliveira, W. Throughput per Megabyte: A Pilot Benchmark of Language-Stack Efficiency for Self-Hosted HTTP Services on a Raspberry Pi 5 : preprint / W. Oliveira. – 2026. – arXiv:2609.11932.
    *Берём:* пол RSS языковых стеков на Pi 5 (Rust 7,36 МБ; Go — «практичный вариант»), довод при выборе Go. *Проверка:* arXiv API (аннотация), 06.10.

35. She, J. AgentRM: An OS-Inspired Resource Manager for LLM Agent Systems : preprint / J. She. – 2026. – arXiv:2603.13110.
    *Берём:* соседнее направление (глава 1). *Проверка:* arXiv API, 06.10.

36. ContextPipe: Database-Inspired Context Assembly for Long-Horizon Agents : preprint / P. Xu, Z. Zhang, Y. Sun [et al.]. – 2026. – arXiv:2609.00749.
    *Берём:* сборку контекста как конвейер — соседняя работа. *Проверка:* arXiv API, 06.10.

37. Weinberger, S. Token Reduction Is Not Cost Reduction : preprint / S. Weinberger, A. Hozez. – 2026. – arXiv:2607.12161.
    *Берём:* экономия токенов не равна экономии денег, поэтому метрика — ₽ с учётом кэша, а не токены. *Проверка:* arXiv API, 06.10.

38. Local-Splitter: A Measurement Study of Seven Tactics for Reducing Cloud LLM Token Usage on Coding-Agent Workloads : preprint / J. Owusu Agyemang, J. J. Kponyo, E. Amponsah [et al.]. – 2026. – arXiv:2604.12301.
    *Берём:* каталог приёмов экономии на кодовых агентах — отличие от разреженного дня устройства. *Проверка:* arXiv API, 06.10.

39. Kwartler, T. Harness Tokenomics: A Router for the Enterprise Agentic Control Plane : preprint / T. Kwartler, A. Aqrawi, A. Abbasi. – 2026. – arXiv:2609.28919.
    *Берём:* маршрутизацию моделей с учётом кэша — соседняя работа. *Проверка:* arXiv API, 06.10.

40. Help or Hurdle? Rethinking Model Context Protocol-Augmented Large Language Models : preprint / W. Song, H. Zhong, Z. Ding [et al.]. – 2025. – arXiv:2508.12566.
    *Берём:* подключение MCP не всегда помогает модели — мотивация сравнивать представления контекста оборудования (E2). *Проверка:* arXiv API, 06.10.

41. Simeon, Q. Octopus Protocol: One-Shot Hardware Discovery and Control for AI Agents via Infrastructure-as-Prompts : preprint / Q. Simeon, J. M. Wei, Y. Fan. – 2026. – arXiv:2605.09055.
    *Берём:* альтернативный подход к обнаружению оборудования; 11-часовой прогон (заметка GAP1 §8.9). *Проверка:* arXiv API, 06.10.

42. Nguyen, T. An Evaluation of LLMs Inference on Popular Single-board Computers : preprint / T. Nguyen, T. Nguyen. – 2025. – arXiv:2511.07425.
    *Берём:* на Pi 5 надёжно работают модели до 1,5B при 5–15 ток/с; пик 10 Вт — аргумент «почему не локальная LLM» (заметка E §E9). *Проверка:* arXiv API, 06.10.

## В. Данные, спецификации, документация

43. Cook, D. CASAS Smart Home dataset – free living, motion, door, activity labels : dataset / D. Cook. – Zenodo, 2025. – DOI 10.5281/zenodo.15708568. – Лицензия CC-BY-4.0.
    *Берём:* 189 домов, 2007–2024, PIR и дверные датчики. По 14 домам (497 суток) рассчитаны частоты событий для гейта: «активность» ≈45/сут, «дверь» ≈55, «смена комнаты» ≈390 (заметка GAP1 §2.3, скрипты `GAP1_artifacts/casas_*.py`). *Проверка:* Zenodo API, 06.10.

44. Model Context Protocol. Specification, revision 2026-07-28. Key Changes. – URL: https://modelcontextprotocol.io/specification/2026-07-28/changelog (дата обращения: 06.10.2026).
    *Берём:*
    - удаление `initialize` и `server/discover`;
    - `subscriptions/listen`;
    - обязательные `ttlMs` и `cacheScope`;
    - рекомендация детерминированного `tools/list` ради кэша промптов;
    - устаревание Sampling и HTTP+SSE.

    *Проверка:* changelog, 06.10.

45. modelcontextprotocol/go-sdk : Release v1.7.0 (28.07.2026), v1.8.0 (14.09.2026). – URL: https://github.com/modelcontextprotocol/go-sdk/releases (дата обращения: 06.10.2026).
    *Берём:* полная поддержка 2026-07-28 и обратная совместимость с 2025-11-25 — наши серверы обслужат и PicoClaw (go-sdk 1.6.1), и nanobot (`mcp<2.0`). v1.8.0 исправлял «deadlocks and teardown hangs found by users running the new protocol at scale» (#1166 — deadlock при `subscriptionsListen`, #1193 — 404 на `subscriptions/listen` в stateless-режиме). Поэтому измерения ведутся на ревизии 2025-11-25. *Проверка:* GitHub API (тексты релизов), 06.10.

46. RouterAI. Документация (`llms.txt`, `llms-full.txt`), OpenAPI (`/api/openapi.json`), каталог моделей `GET /api/v1/models` и `/models/<id>/endpoints` ; Пользовательское соглашение (оферта). – URL: https://routerai.ru/ (дата обращения: 06.10.2026).
    *Берём:*
    - цены в ₽ за токен, включая чтение кэша; объект `provider` и синтаксис `@provider=…&allow_fallbacks=false`; командные аккаунты; `service_tier`: `flex` «примерно вдвое дешевле», задаётся на каждый запрос;
    - о кэше: в `llms-full.txt` (333 КБ) нет ни одного упоминания кэша; в OpenAPI usage описан полями Responses (`input_tokens_details.cached_tokens`), у `/generation` — `input_cached_tokens`; в `supported_parameters` endpoint'ов gpt-4.1-nano, gpt-5-nano и gpt-5-mini нет `prompt_cache_key` и `prompt_cache_retention`, у gpt-5-* нет `temperature`. Отсюда проба E0-P в первый день с ключом;
    - оферта: п. 4.2.1 — условия меняются в одностороннем порядке; п. 8.3 — сервис «как есть». Отсюда ₽ по замороженному прайсу.

    *Проверка:* цены — живой каталог, 06.10 (совпадают со снимком `F_artifacts/routerai_models_2026-10-06.json`); документация, OpenAPI и endpoint'ы — снимки `redteam_artifacts/routerai_*_2026-10-06.json` и `llms-full.txt`, 06.10; оферта — страница соглашения, 06.10.

47. OpenAI. Prompt caching ; Images and vision ; Pricing : документация API. – URL: https://developers.openai.com/api/docs/guides/prompt-caching ; https://developers.openai.com/api/docs/guides/images-vision ; https://developers.openai.com/api/docs/pricing (дата обращения: 06.10.2026).
    *Берём:*
    - «Keep the prefix stable… place those [dynamic content] at the end» — кэш-дружественная раскладка известна из документации;
    - `in_memory`: «around 5 to 10 minutes of inactivity, up to one hour»; `24h` поддерживают gpt-5.x, gpt-5, gpt-4.1; для моделей с обоими режимами организации без ZDR по умолчанию получают `24h`. Входят ли сюда mini/nano-варианты — явно не сказано;
    - запись в кэш 1,25× — только GPT-5.6 и новее; кэш не разделяется между организациями; минимальная длина кэшируемого префикса до GPT-5.6 «varies by request settings»; для GPT-5.6+ `prompt_cache_key` не нужен;
    - токены изображений gpt-4o-mini: base 2 833, tile 5 667 (кадр 640×480: 2 833 в low и 14 167 в high) — прогноз H7.

    *Проверка:* обе страницы открыты и процитированы 06.10 (в редакции 1 было по заметкам F и GAP1).

48. W3C. Web of Things (WoT) Thing Description 1.1 : W3C Recommendation, 05.12.2023. – URL: https://www.w3.org/TR/wot-thing-description11/ (дата обращения: 06.10.2026).
    *Берём:* формат внутренней модели устройства в `hw-mcp`; проверка по официальной схеме (`GAP2_artifacts/td-schema-v1.1_w3c_2025-03-12.json`). *Проверка:* заметка GAP2.

49. OpenTelemetry. Semantic conventions for generative AI (статус Development) : [репозиторий]. – URL: https://github.com/open-telemetry/semantic-conventions-genai (дата обращения: 06.10.2026).
    *Берём:* имена полей трассы (`gen_ai.usage.*` и др.). *Проверка:* заметка E G1.

50. NVIDIA. JetPack SDK 4.6.6 (финальный выпуск JetPack 4 / L4T R32.7.6) ; Jetson Linux R32.7.6 Developer Guide: Power Management for Jetson Nano and TX1. – URL: https://developer.nvidia.com/jetpack-sdk-466 ; https://docs.nvidia.com/jetson/archives/l4t-archived/l4t-3276/Tegra%20Linux%20Driver%20Package%20Development%20Guide/power_management_nano.html (дата обращения: 06.10.2026).
    *Берём:* стек Nano (Ubuntu 18.04, ядро 4.9, glibc 2.27); INA3221 VDD_IN/GPU/CPU; режимы 10 и 5 Вт; у 2 ГБ-версии датчика нет. *Проверка:* заметка L4 §2–4.

51. Raspberry Pi Ltd. Raspberry Pi documentation: config.txt (`kernel_2712`, страницы 16 КиБ); frequency management and thermal control (троттлинг с 80 °C). – URL: https://www.raspberrypi.com/documentation/computers/ (дата обращения: 06.10.2026). Точные адреса разделов — уточнить при оформлении.
    *Берём:* размер страницы как фактор RSS; обязательность Active Cooler. *Проверка:* заметки GAP1 §8.4, `concept_systems_depth.md` §5.

52. Texas Instruments. INA226 High-Side or Low-Side Measurement, Bi-Directional Current and Power Monitor with I²C Compatible Interface : datasheet SBOS547C. – URL: https://www.ti.com/product/INA226 (дата обращения: 06.10.2026).
    *Берём:* 0–36 В, 16 бит, ошибка усиления ≤ 0,1 %, смещение ≤ 10 мкВ (страница продукта — проверено 06.10); время преобразования 140 мкс – 8,244 мс, усреднение 1–1024 (заметка E §E7, даташит). В редакции 2 — только для расширения X-energy (событийно-синхронное измерение); в ядре энергия меряется USB-ваттметром с порогом обнаружения.

53. The Linux Kernel documentation: ABI sysfs-bus-iio; GPIO character device; Control Group v2 (cgroup-v2.rst); PSI (psi.rst); /proc (smaps_rollup). – URL: https://docs.kernel.org/ (дата обращения: 06.10.2026).
    *Берём:* инвентарь и единицы измерения из ABI; границу измерения через cgroup v2; PSS. *Проверка:* заметки GAP2 §2, `concept_systems_depth.md`.

## Г. Программные проекты (аналоги, baseline, инструменты)

Звёзды, язык, лицензия, релизы и коммиты — GitHub API, 06.10.2026.

54. PicoClaw / Sipeed : [репозиторий]. – Go, MIT, ★30 012. – URL: https://github.com/sipeed/picoclaw (дата обращения: 06.10.2026).
    *Берём:* baseline B1 в двух видах — как поставляется (E1) и в лучшей штатной настройке PicoClaw-best (E1, E5-T2). Релиз v0.3.1 от 03.07.2026, тег указывает на коммит `2cf030d2`. По коду тега проверено 06.10:
    - Go 1.25.11, `modelcontextprotocol/go-sdk v1.6.1`;
    - `pkg/mcp/manager.go`: `mcp.NewClient(…, nil)` без обработчиков уведомлений; в коде вне тестов нет `ReadResource`/`ListResources` — MCP-клиент работает только с инструментами;
    - `pkg/heartbeat/service.go`: интервал 30 мин, `minIntervalMinutes = 5`; при HEARTBEAT.md без задач ход пропускается;
    - `docs/reference/tools_configuration.md`, Tool Discovery: MCP-инструменты скрыты, LLM получает поиск BM25 или regex (иначе «increase API costs»);
    - `docs/reference/cron.md`: `deliver: true` — сообщение «without agent processing»; режим `command` — shell-команда без хода LLM;
    - `pkg/seahorse` (ContextManager на SQLite FTS5: компакция, поиск) и `cmd/membench`;
    - каналы `pkg/channels/maixcam` (`person_detected` → сообщение агенту, строка 147), `mqtt`, `vk`;
    - `pkg/providers/openai_compat/provider.go`: `prompt_cache_key` только для `api.openai.com` и `*.openai.azure.com`; `pkg/providers/common/common.go`: `http.DefaultTransport`;
    - README: «Runs on $10 hardware with <10MB RAM», «Recent builds may use 10-20MB RAM».

    *Уточнение:* коммит `bbf6893c` из заметок GAP4 и `concept_systems_depth.md` — это main от 19.08.2026, а не v0.3.1.

55. nanobot / HKUDS : [репозиторий]. – Python, MIT, ★48 827. – URL: https://github.com/HKUDS/nanobot (дата обращения: 06.10.2026).
    *Берём:* baseline B2 (только E1). v0.3.5 от 15.09.2026, коммит `1bb712d3`; `requires-python >=3.11`; `mcp>=1.26.0,<2.0.0`. По коду тега проверено 06.10:
    - `nanobot/agent/tools/mcp.py`: ресурсы читаются (`read_resource`) как read-only инструменты, подписок (`resources/updated`) нет;
    - `docs/automations.md`: local triggers — локальный скрипт будит агента;
    - `nanobot/config/schema.py`: `DreamConfig.enabled = True`, `interval_h = 2` — консолидация памяти раз в 2 ч по умолчанию.

56. NanoClaw / nanocoai : [репозиторий]. – TypeScript, MIT, ★30 881. – URL: https://github.com/nanocoai/nanoclaw (дата обращения: 06.10.2026).
    *Берём:* приём script gates — `docs/scheduled-tasks.md`: `{"wakeAgent": false}` «completes the run without calling the model»; повторяющаяся задача без гейта, которая сработала бы больше четырёх раз за 24 ч, отклоняется (проверено 06.10). Не baseline: нужны Node ≥ 22 и Docker (заметка L4 §0 п. 3).

57. OpenClaw : [репозиторий и документация]. – TypeScript, MIT, ★391 505. – URL: https://github.com/openclaw/openclaw (дата обращения: 06.10.2026). Документация на коммите `c78c2638` (06.10.2026): `docs/reference/prompt-caching.md`, `docs/concepts/session-pruning.md`, `docs/gateway/config-agents/heartbeat-compaction-and-streaming.md` ; Issue #61690. – URL: https://github.com/openclaw/openclaw/issues/61690.
    *Берём* (проверено 06.10):
    - системный промпт делится на «stable prefix» и «volatile suffix»; «Bundled MCP tool catalogs are sorted deterministically»;
    - «Heartbeat keep-warm» (пример `every: "55m"`, «Keep heartbeat below your TTL»);
    - `contextPruning.mode: "cache-ttl"`: обрезка старых результатов инструментов после TTL с заглушкой «[Old tool result content cleared]»;
    - живая регрессия кэша: hit rate 0,891 для MCP-style transcript; для gpt-5.6 маршрутизация «session-sticky rather than key-driven»;
    - `isolatedSession`: «Reduces per-heartbeat token cost from ~100K to ~2-5K tokens»; `every: 0m` выключает регулярный heartbeat, event-driven wakes остаются;
    - Issue #61690: 2026.4.2, ~27 000 входных токенов на тик; **закрыто 06.04.2026 как completed**.

    Всё это — «что не ново» (THESIS_CONCEPT §5.2) и источник решений 4 и 8.

58. robotmcp/ros-mcp-server : [репозиторий]. – Python, Apache-2.0, ★1 486. – URL: https://github.com/robotmcp/ros-mcp-server (дата обращения: 06.10.2026).
    *Берём:* MCP-мост к ROS через rosbridge для сценария S-robot (31 инструмент, 6 ресурсов — заметка B H1).

59. ROS 2 Jazzy ; Gazebo Harmonic ; TurtleBot3 ; rosbridge_suite : [программное обеспечение]. – URL: https://docs.ros.org/en/jazzy/ ; https://gazebosim.org/ (дата обращения: 06.10.2026).
    *Берём:* симулятор для S-robot; наличие arm64-пакетов проверено (`F_artifacts/ros_apt_arm64_check_2026-10-06.txt`).

## Е. Добавлено в редакции 2 (разборы red team; каждый пункт проверен 06.10 по первоисточнику)

### Е.1. Публикации и препринты

60. TokenPilot: Cache-Efficient Context Management for LLM Agents / B. Xu, Z. Xue, D. Chen [et al.] // Findings of the Association for Computational Linguistics: EMNLP 2026 (принято). – 2026. – arXiv:2606.17016.
    *Берём:* стабилизация префикса и консервативное вытеснение по «batch-turn schedule» — прецедент политики «окно со сбросом блоком»; стоимость −56…−87 % на PinchBench и Claw-Eval. *Проверка:* arXiv API (комментарий «EMNLP 2026 Findings»).

61. Budget-Aware Tool Use Enables Effective Agent Scaling / T. Liu, Z. Wang, J. Miao [et al.] // Conference on Language Modeling (COLM 2026, принято). – 2026. – arXiv:2511.17006.
    *Берём:* Budget Tracker — непрерывная подача бюджета в контекст агента; «бюджет в контексте» — известный приём. *Проверка:* arXiv API («Accepted to COLM 2026»).

62. HearthNet: Edge Multi-Agent Orchestration for Smart Homes / Z. Zhan, K. Li, Y. Zhang, H. Haddadi // Proceedings of the ACM Conference on AI and Agentic Systems (CAIS '26). – 2026. – P. 1123–1127. – DOI 10.1145/3786335.3813188.
    *Берём:* ближайший аналог: постоянные агенты на домашнем хабе, «hosted LLM APIs for inference», MQTT, общее состояние в Git, root-issued actuation leases; устаревшие команды отклонены 5/5, p95 проверки аренды < 0,01 мс. Прецедент шлюза приводов. *Проверка:* arXiv API (2604.09618), HTML-версия, Crossref.

63. Proactive Rejection and Grounded Execution: A Dual-Stage Intent Analysis Paradigm for Safe and Efficient AIoT Smart Homes / X. Jin, Z. Ni, Z. Sheng, V. C. M. Leung : preprint. – 2026. – arXiv:2603.16207.
    *Берём:* DS-IA — проверка состояния дома и детерминированный каскадный верификатор до исполнения; EM 58,56 % на HomeBench и SAGE. Прецедент шлюза. *Проверка:* arXiv API.

64. The Harness Effect: How Orchestration Design Sets the Token Economics of Enterprise Agentic AI : preprint / M. Sayed Ali, A. Novik, A. Boddupally [et al.]. – 2026. – arXiv:2607.06906.
    *Берём:* контролируемая замена обвязки при фиксированных моделях: −41 % стоимости задачи ($0,21 → $0,12), −38 % токенов; «cache-shape discipline». Сравнение обвязок при фиксированной модели уже опубликовано. *Проверка:* arXiv API.

65. WildClawBench: A Benchmark for Real-World, Long-Horizon Agent Evaluation : preprint / S. Ding, X. Dai, L. Xing [et al.]. – 2026. – arXiv:2605.10912.
    *Берём:* «switching harness alone shifts a single model by up to 18 points». *Проверка:* arXiv API.

66. Claw-SWE-Bench: A Benchmark for Evaluating OpenClaw-style Agent Harnesses on Coding Tasks : preprint / M. Zheng, K. Han, B. Li [et al.]. – 2026. – arXiv:2606.12344.
    *Берём:* при фиксированной модели успех и потребление ресурсов обвязок «vary substantially». *Проверка:* arXiv API.

67. STAGE-Claw: Automated State-based Agent Benchmarking for Realistic Scenarios : preprint / S. Liang, B. Yu, P. Wang [et al.]. – 2026. – arXiv:2606.10394.
    *Берём:* оценка по конечному состоянию системы, а не по тексту — опора протокола качества. *Проверка:* arXiv API.

68. Kadaboina, R. K. Jagarin: A Three-Layer Architecture for Hibernating Personal Duty Agents on Mobile : preprint / R. K. Kadaboina. – 2026. – arXiv:2603.05069.
    *Берём:* движок на устройстве решает, будить ли «спящего» агента — прецедент гейта пробуждения. *Проверка:* arXiv API.

69. Leveraging LLMs for Efficient and Personalized Smart Home Automation : preprint / C. Yu, C. Choi, S. Lee [et al.]. – 2026. – arXiv:2601.04680.
    *Берём:* IoTGPT — повторное использование выученных подзадач сокращает число вызовов LLM. *Проверка:* arXiv API.

70. Yang, D. Device Context Protocol: A Compact, Safety-First Architecture for LLM-Driven Control of Constrained Devices : preprint / D. Yang. – 2026. – arXiv:2605.26159.
    *Берём:* проверки диапазона и типа, dry-run, мост, отклоняющий выдуманные вызовы, — прецедент шлюза приводов. *Проверка:* arXiv API.

71. AgentOpt v0.1 Technical Report: Client-Side Optimization for LLM-Based Agent : preprint / W. Hua, S. Karne, Q. Xie [et al.]. – 2026. – arXiv:2604.06296.
    *Берём:* при равной точности разрыв стоимости между комбинациями моделей 13–32× — выбор модели известный рычаг (бывшая H8). *Проверка:* arXiv API.

72. Non-Determinism of "Deterministic" LLM Settings : preprint / B. Atil, S. Aykent, A. Chittams [et al.]. – 2024. – arXiv:2408.04667.
    *Берём:* колебания точности до 15 % между прогонами в «детерминированных» настройках — A/A-пол и отчёт о недетерминизме. *Проверка:* arXiv API.

### Е.2. Документация и программные проекты

73. Ultralytics Docs. Raspberry Pi : руководство и бенчмарки. – URL: https://docs.ultralytics.com/guides/raspberry-pi/ (дата обращения: 06.10.2026).
    *Берём:* YOLO26n на Raspberry Pi 5, imgsz 640, FP32: NCNN 67,03 мс на изображение (PyTorch 299,09; ONNX 125,99), без пред- и постобработки, Ultralytics 8.4.108 — детектор E6.

74. kornia/bubbaloop : [репозиторий]. – Rust, Apache-2.0, ★28. – URL: https://github.com/kornia/bubbaloop (дата обращения: 06.10.2026).
    *Берём:* ближайший аналог по замыслу — один бинарник 13 МБ для камер, датчиков и роботов на Jetson и Raspberry Pi, MCP-сервер. ROADMAP: Tier 1 без LLM, Tier 2 через `schedule_task`, adaptive heartbeat 5–60 с, «resource summary in every agent turn», «Live world state… with stale-entry warnings», human-in-the-loop proposals. README: «~$5-10/day» против «~$0.05/day» — без описанного замера. *Проверка:* README и ROADMAP, GitHub API.

75. macc-n/wot-mcp : [репозиторий]. – TypeScript, GPL-3.0, ★10. – URL: https://github.com/macc-n/wot-mcp (дата обращения: 06.10.2026).
    *Берём:* WoT → MCP с `--tool-strategy explicit|generic`; generic = `list_devices`, `read_property`, `write_property`, `invoke_action` — наше условие R4 существует как опция. *Проверка:* README.

76. allenporter/home-assistant-datasets : [репозиторий]. – Apache-2.0, ★254. – URL: https://github.com/allenporter/home-assistant-datasets (дата обращения: 06.10.2026).
    *Берём:* синтетические дома и проверка выходов моделей по эталону — внешний якорь качества (THESIS_CONCEPT §12.8). *Проверка:* README, GitHub API.

77. Home Assistant core: pull requests #129058 «Handle sentence triggers and local intents before pipeline agent» (влит 14.11.2024); #168457 «Add ability to filter GetLiveContext tool» (влит 29.04.2026); #170014 «Remove area from LLM system prompt and provide in a tool» (открыт). – URL: https://github.com/home-assistant/core/pulls (дата обращения: 06.10.2026).
    *Берём:* `prefer_local_intents` — LLM как fallback (прецедент локальных правил); фильтр `GetLiveContext` ради токенов; area «breaks the prompt cache» → вынесена в инструмент, проверено на eval-датасете HA (Gemma4: 86,3 % → 87,0 % good). *Проверка:* GitHub API (статусы, даты, тексты PR).

78. lhzn-io/jetson-telemetry-mcp (Python, MIT, ★1) ; rhel-lightspeed/linux-mcp-server (Python, Apache-2.0, ★313) : [репозитории]. – URL: https://github.com/lhzn-io/jetson-telemetry-mcp ; https://github.com/rhel-lightspeed/linux-mcp-server (дата обращения: 06.10.2026).
    *Берём:* ресурсы платы через MCP уже есть: температура, шины питания и частоты Jetson; сведения о Linux-системе только для чтения. *Проверка:* GitHub API (описания).

### Е.3. Протокол поиска (основа приложения Д ПЗ)

- arXiv API, 06.10.2026: «all:OpenClaw» — 227 записей; «OpenClaw AND (cost OR token OR heartbeat)» — 37.
- GitHub (поиск red team): «sysfs mcp server», «hwmon mcp», «thing description sysfs» — 0 результатов.
- Повтор с теми же запросами — перед 15.01.2027 и перед сдачей; формулировки новизны — «по протоколу поиска от <дата>».

## Д. Собственные расчёты и артефакты проекта (не литература; ссылки в тексте ПЗ — как на приложения)

- `research/new_topic/final_artifacts/budget_mvp.py` и `budget_mvp_output_2026-10-06.txt` — бюджет API MVP-ядра, мощность (не меньшая эффективность с Холмом), ДИ T2 по блокам, машинное время платы. Прежний `budget_final.py` — для редакции 1.
- `research/new_topic/redteam_artifacts/` — расчёты red team (`redteam_calc.py`, `mk_sim.py`, `boot5.py`), снимки OpenAPI и endpoint'ов RouterAI от 06.10.2026, версия концепции до разбора.
- `research/new_topic/GAP1_artifacts/` — модель нагрузки, модель смещения ускоренного времени, `ttl_probe.py`, частоты CASAS.
- `research/new_topic/GAP2_artifacts/` — инвентарь из sysfs, фикстура, TD 1.1, цена представлений контекста (k = 1/5/20).
- `research/new_topic/GAP3_artifacts/` — шлюз приводов и его бенчмарк (p99 6,8 мкс на M4 Pro).
- `research/new_topic/GAP4_artifacts/` — токены ru/en (o200k) и оценщик PicoClaw.
- `research/new_topic/concept_systems_artifacts/tls_probe*` — замер TLS к routerai.ru: полное рукопожатие 4 987 Б, возобновлённое 890 Б, билет 64 800 с.
- `research/consultation-gia-1/` — требования ГИА ИТМО (сроки, объём, структура). Не литература.
