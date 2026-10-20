# Журнал решений

Формат: дата — решение — причина — кто (агент/этап).

- 2026-10-06 — Основная тема: среда исполнения ИИ-агента (M1 снимки KV, M2 структурные черновики, M3 бюджет рассуждения) — см. docs/supervisor/vkr_topic_variants.html — выбор после панели судей и red team (вариант ShapeSpec понижен до альтернативы A из-за оспоримой новизны).
- 2026-10-06 — llama.cpp фиксирован на коммите 7fe450e19 (сборка 11146), собран из исходников в third_party/llama.cpp/build — воспроизводимость, совпадение с brew-версией пилотов.
- 2026-10-06 — BFCL: данные v4 и эталонные проверщики из gorilla коммит 6ea5797 (third_party/gorilla), через заглушки модулей без тяжёлых зависимостей — метрики должны совпадать с официальными.
- 2026-10-06 — Загрузка дополнительных моделей отложена пользователем; разработка на Qwen3-8B-Q4_K_M (официальный файл Qwen, sha256 d98cdcbd…).
- 2026-10-06 — Python для разработки: bundled 3.12 + numpy/pandas; статистика без scipy (точный McNemar, bootstrap на numpy).
- 2026-10-06 — [harness] Лимит шагов цикла — семантика BFCL base_handler: force_quit, когда число шагов с вызовами превысило max_steps_per_turn=20 (до 21 обращения к модели за ход), последующие ходы не выполняются — чтобы multi-turn оценки совпадали с официальными — агент стенда.
- 2026-10-06 — [harness] Конвертация схем: помимо §9 (dict→object, float→number, tuple/list→array, any→string, удалить response) — «.»→«_» в именах функций с проверкой underscore_to_dot=True (как FC-модели BFCL), языковая подсказка BFCL в описании, enum, несовместимый с type (integer с ["1","2"], boolean с "dontcare", enum у array; 75 в live_multiple), удаляется со значениями в описание — llama.cpp строит грамматику из enum и игнорирует type; всё пишется в conversion_log, отключается опциями bfcl.* — агент стенда.
- 2026-10-06 — [harness] Поле excluded_function в multi-turn игнорируется, как в коде BFCL (инструменты — все функции involved_classes) — агент стенда.
- 2026-10-06 — [harness] Ведущий system задачи (92 задачи live_*) дописывается к системному промпту сессии; new_messages шага могут содержать assistant-реплики истории датасета (8 задач live_irrelevance) — отступление от «только user/tool» в §9.1, agentrt должен их принимать — агент стенда.
- 2026-10-06 — [harness] Задачи с классом среды, который не импортируется (MathAPI без mpmath: 25/200 multi_turn_base, 8/57 dev), — status skipped_dependency, вне знаменателя точности; сбой инференса — провал в любой категории (включая irrelevance) — агент стенда.
- 2026-10-06 — [harness] multi_turn_miss_func не поддержан (инструменты сессии фиксируются при создании); miss_param и long_context поддержаны — агент стенда.
- 2026-10-06 — [harness] Эталон llama-server: -np 1, -fit off, по умолчанию --cache-ram 0 (RAM-кэш промптов сервера — аналог M1, выключен для паритета E0; включается конфигом), бюджет рассуждения — reasoning_budget_tokens в запросе — агент стенда.
- 2026-10-06 — [harness] Тип шага для M3 — по последнему новому сообщению буквально по §7 (при параллельных вызовах решает последний результат) — агент стенда.
- 2026-10-06 — [harness] TOST для парных долей — по умолчанию score-статистика Танго (1998), вариант Вальда для сравнения; McNemar — точный биномиальный (+ mid-p справочно) — агент стенда.
- 2026-10-06 — [harness] Смоук-прогоны стенда пишутся в harness/smoke/, не в experiments/results (это не результаты протокола); Python-стенд работает на bundled 3.12 без .venv — агент стенда.
- 2026-10-06 — [протокол] experiments/PROTOCOL.md v1 — проект предрегистрации E0–E5: пороги H1–H3 из табл. 4.3 с трёхзначным исходом (подтверждена / опровергнута / не определена), заморозка тегом prereg-Ek (или SHA-256 файлов) до первого прогона на test, без промежуточного анализа test — spec §0.3–0.5 — агент протокола.
- 2026-10-06 — [протокол] Холодный диск в E2: одного F_NOCACHE (spec §5) недостаточно — проба на этой машине: чтение закэшированного файла с F_NOCACHE ≈ 16 ГБ/с, после msync(MS_INVALIDATE) ≈ 6 ГБ/с, а F_NOCACHE-чтение снова кэширует ≈ 94 % страниц; перед каждым холодным замером fsync + msync(MS_SYNC|MS_INVALIDATE) и проверка mincore ≤ 1 % (tools/env_check.sh: ENV_CHECK_EVICT / COLD_FILES / WARM_FILES) — агент протокола.
- 2026-10-06 — [протокол] TOST ±1 п.п. (H1, H2) — на основном наборе test (2557 задач без MathAPI, 2574 с mpmath), а не на multi_turn_base: при n = 126–143 90 % ДИ Tango шире ±1,86 п.п. даже при нуле расхождений; при сокращениях в наборе ≥ 1500 задач — агент протокола.
- 2026-10-06 — [протокол] H2 «не уступают лучшей штатной спекуляции» = «не хуже» с допуском 3 % (нижняя граница 95 % ДИ T_best/T_structural ≥ 0,97, опровержение — верхняя < 0,97); «не выше лучшего штатного метода» из табл. 4.3 трактуется как значимое отставание сильнее допуска; превосходство отчитывается отдельно; лучшая штатная спекуляция выбирается на dev на llama-server (ngram-simple/map-k/map-k4v/mod, draft-simple после загрузки 0.6B), ngram-cache и draft-eagle3/mtp/dflash/dspark исключены — агент протокола.
- 2026-10-06 — [протокол] H1.speed — конъюнкция двух отношений first_step_wall_ms (холодный agentrt / C5 и холодный llama-server / C5, нижняя граница ДИ ≥ 5 у обоих); снимок q8_0 оценивается как «KV q8_0 + снимок» против f16 без снимков (формат состояния llama.cpp повторяет типы KV), размер q8_0/f16 должен быть в [0,45; 0,60] — агент протокола.
- 2026-10-06 — [протокол] Каталоги multi-turn: классы BFCL 9–22 функции, каталоги задач 18–39 функций (2137–4815 токенов префикса, 20 различных в test; «32 инструмента» пилота = GorillaFileSystem + TwitterAPI); синтетическое масштабирование k = 4…96 (k = 128 ≈ 15,8 тыс. токенов не помещается в n_ctx 16384) — агент протокола.
- 2026-10-06 — [протокол] Семантика таймеров agentrt обязана совпадать с llama-server 7fe450e19: prefill_ms включает выборку первого токена, decode_ms — от первого до последнего токена, decode_tokens включает первый; ускорение = отношение сумм затрат по одним задачам (Hoefler & Belli, Rules 3–4); ДИ — парный (в E2 иерархический) перцентильный бутстреп 10 000, seed 20261006 — агент протокола.
- 2026-10-06 — [протокол] Жадное декодирование и в режиме рассуждения (вопреки рекомендации Qwen3 temperature 0,6) ради детерминизма и проверки беспотерьности M2; max_tokens 1024 / 4096 (E4 — 4096 у всех политик), обрыв = неуспех задачи, доля обрывов отчитывается — агент протокола.
- 2026-10-06 — [протокол] Пожелания к стенду и среде из протокола: в harness/server_runner разрешить drafts.mode = ngram-map-k4v (llama-server его поддерживает); drafts.params — расширение spec §3 (параметры n-граммных методов); snapshot.ram_budget_mb = 0 и disk_dir = null отключают уровни M1 (изоляция в E2); agentrt отдаёт extra.raw_text (идентичность L1), отметки конца рассуждения и границы статического префикса (фазы E1), зазоры логитов top1−top2 по флагу трассы (разбор расхождений) — агент протокола.
- 2026-10-06 — [протокол] Машинное время на 8B ≈ 98 ч (с резервом ≈ 118 ч), приоритеты P0 ≈ 40 ч (пилот, ворота E0, ядро E3, ядро E2 с точностью) / P1 / P2 — PROTOCOL §7–8; порты прогонов протокола 8091–8099 (spec §11, прогоны эксклюзивны) — агент протокола.
- 2026-10-06 — [agentrt] C ABI расширен без изменения функций §3: art_session_messages, art_engine_last_error/art_session_last_error, art_version; шаг принимает массив, одно сообщение или {"messages","options":{max_tokens,think,budget,step_type}}; при ошибке step/run возвращают код и {"error":{code,type,message}} в out_json — у step/run нет параметра err, а стенду нужны история и явные указания шага.
- 2026-10-06 — [agentrt] Движок создаётся через common_init_from_params с параметрами как у `llama-server -np 1 -fit off` (n_outputs_max как server_output_limits, сэмплинг-умолчания из GGUF, прогрев); шаблон — kwargs preserve_reasoning=true и enable_thinking, reasoning_format deepseek, как у сервера по умолчанию — паритет E0 (паритет-смоук 10/10, agentrt/PARITY_SMOKE.md).
- 2026-10-06 — [agentrt] Режим черновиков задаётся в движке и переопределяется в сессии (session.drafts.mode); движок с mode "none" резервирует 1 выход на ubatch (как сервер без спекуляции) и запрещает черновики в сессиях, иначе 1+n_max — эталон none и спекуляция сравниваются в одном контексте, а E0 идёт с параметрами сервера.
- 2026-10-06 — [agentrt] drafts.n_max ограничивает длину черновика для всех провайдеров (через dp.n_max common_speculative), прочие параметры ngram-* — умолчания llama.cpp; батчи цели зеркалирует в контекст черновой модели сам движок, а не common_speculative_process — KV черновой модели совпадает с целью при любых сессиях.
- 2026-10-06 — [agentrt] draft-simple реализован, но не проверен на модели: Qwen3-0.6B не докачана, две 8B одновременно держать нельзя; structural — заглушка без черновиков до следующего этапа.
- 2026-10-06 — [agentrt] M1 на этом этапе: интерфейс SnapshotStore, ключ (SHA-256 модели через CommonCrypto, кэш по path+size+mtime), статический префикс и контрольные точки; хранилище — null (enabled()=false, snapshot.enabled=true только отражается в stats.requested, prewarm — no-op, статический префикс не считается) — по заданию этапа.
- 2026-10-06 — [agentrt] M3: ошибка инструмента — текст со слова "Error" (регистр важен) или JSON-объект с полем error (как harness/runner_base.is_error_tool_result) плюс флаг error от callback; fixed с budget 0 = без рассуждения (как в Python-политике); политика по умолчанию never; random_matched — монета splitmix64(seed, номер шага), не последовательность random.Random эталонного раннера.
- 2026-10-06 — [agentrt] Если common_chat_parse отвергает ответ, шаг не падает: content = сырой текст, step.parse_error = причина (сервер отвечает HTTP 500); id вызовов детерминированные call_<сессия>_<шаг>_<i> (у сервера случайные).
- 2026-10-06 — [agentrt] Тайминги: prefill_ms — до готовности логитов последнего токена промпта, decode_ms — от этой точки до конца генерации (включая выборку первого токена), дополнительно render_ms, parse_ms, reasoning_tokens; у сервера граница сдвинута на один токен, поэтому для E0 сравнивать wall-time шага.
- 2026-10-06 — [agentrt] Трасса: кроме событий §8 пишется rollback{checkpoint,messages,kv_tokens}; prefix несёт reason, decode — reasoning_tokens, tool_result — name, session_start — tools и объект reasoning, session_end — total_ms (согласовано с monitor/check_trace.py).
- 2026-10-06 — [agentrt] art_session_run исполняет вызовы и последнего разрешённого шага (stop=max_steps), чтобы история заканчивалась tool-сообщениями, а не вызовами без результатов.
- 2026-10-06 — [agentrt] ASan-тесты идут с ASAN_OPTIONS=detect_container_overflow=0: векторы нашего кода, размер которых меняет неинструментированная libllama-common (ngram-mod), дают ложный container-overflow; LeakSanitizer на macOS недоступен — утечки проверены `leaks --atExit` (0 на полном наборе тестов).
- 2026-10-06 — [agentrt] harness/runner_base.py уже был — agentrt_runner импортирует Runner/Session из него; класс AgentRTRunner, плюс псевдоним AgentrtRunner и create_runner, которые ищет make_runner.
- 2026-10-06 — [monitor] Контракт трассы — фактический формат agentrt (trace.cpp/session.cpp/engine.cpp): конверт event, t_ms, session?, step? (номер шага с 0); step_end.stop ∈ eos|word|limit + finish_reason/n_tool_calls; session_start.reasoning — имя политики или объект {policy, budget, table}; tool_result.ms может быть null; событие rollback; описание и правила — docs/monitor.md §2 — §8 спецификации поля не фиксирует, монитор согласован с производителем трасс — агент монитора.
- 2026-10-06 — [monitor] Тип шага по умолчанию буквально по §7 (решает последний записанный результат прошлого шага, как harness/runner_base.classify_step_type); agentrt/src/reasoning.cpp: classify_step даёт recover при любой ошибке среди результатов — расхождение реализаций, монитор сообщает его как policy.step_type; режим --step-type-rule any проверяет текущее поведение agentrt — агент монитора.
- 2026-10-06 — [monitor] Каждый tool_call обязан получить ровно один tool_result до первой фазы следующего шага; для одношаговых категорий BFCL и force quit (результаты последнего шага не отправляются модели) — флаг --allow-trailing-calls, такие вызовы считаются в summary; превышение 20 шагов в ходе (force quit BFCL = 21 шаг) остаётся нарушением budget.steps — агент монитора.
- 2026-10-06 — [monitor] rollback отменяет все ожидающие вызовы (по трассе не видно, какие остались в истории до контрольной точки) — консервативно, без ложных срабатываний; исключение — вызовы, без результатов которых следующий шаг уже начал вычисления (missing_result) — агент монитора.
- 2026-10-06 — [monitor] Бюджет M3 по трассе: контракт допускает decode.reasoning_tokens (токены внутри блока рассуждения без принудительного окончания; допуск +3 на дописывание UTF-8) и step_start.override; agentrt их пока не пишет — проверка бюджета пропускается (skipped_checks), при записи только имени политики проверяется лишь согласованность решений; просьба к агенту ядра добавить оба поля — агент монитора.
- 2026-10-06 — [monitor] Валидатор аргументов — собственный, семантика JSON Schema 2020-12 (1.0 — integer, --strict-integer для строгого режима; bool не число; enum по равенству JSON); непроверяемые ключевые слова перечисляются в отчёте; схемы в нотации BFCL (dict/float/tuple/any) — catalog.invalid — агент монитора.
- 2026-10-06 — [monitor] Наблюдения на реальных данных: реальный прогон agentrt_cli (Qwen3-8B) с parallel_tool_calls=false дал 2 вызова в одном сообщении (policy.parallel, monitor/tests/fixtures/agentrt_qwen3-8b_parallel_off.jsonl) — сверить с llama-server в E0; 23 эталонных ответа BFCL v4 нарушают собственные схемы (monitor/tests/bfcl_data.py: KNOWN_SCHEMA_VIOLATIONS) — агент монитора.
- 2026-10-06 (после встречи с руководителем) — ТЕМА СОГЛАСОВАНА: «Разработка ИИ-агента для приложений граничного ИИ»: агент на одноплатнике, внешняя LLM (RouterAI, GPT mini), MCP-сервер для контекста оборудования (датчики/камеры/исполнительные устройства), контекст пользователя, метрики работы во времени (часы в день), симуляция ROS/Gazebo, аналоги NanoClaw/PicoClaw. Предыдущие решения о локальной среде исполнения (agentrt, llama.cpp, KV-снимки) ОТМЕНЕНЫ; наработки перемещены в ~/.Trash/diploma-cleanup-2026-10-06.
- 2026-10-06 — Уборка 2: BFCL-стенд (harness кроме stats.py), data/bfcl, third_party/gorilla, tools/env_check.sh, docs/harness.md, промежуточные concept_*.md перемещены в ~/.Trash/diploma-cleanup-2026-10-06/round2_bfcl_harness — к новой постановке не относятся. Оставлены harness/stats.py (+23 теста, проходят) и monitor/ (контракт трасс переписать под нового агента).
- 2026-10-06 — Концепция ВКР: research/new_topic/THESIS_CONCEPT.md (полная, ред. 2 после red team) — рабочий справочник; для студента и руководителя — упрощённое ядро (docs/plan): цель-эффект по требованиям ГИА (сокращение суточных затрат на обращения к LLM против штатных режимов аналогов при сохранении полноты реакции и безопасности команд), 3–4 гипотезы, 3–4 эксперимента. Плата: кафедральный Jetson Nano (если 4 ГБ — основная, встроенный датчик мощности) или Raspberry Pi 5; до платы — эмуляция на Mac.
- 2026-10-06 — Реализация v2 начата по IMPLEMENTATION_SPEC.md (v2): агент edgectx и hw-mcp на Go (статика, linux/arm64), sim-mcp/daygen/mock-llm/реплеер/метрики на Python 3.12 (.venv через uv, mcp 1.30). Установлены: go 1.27.1, uv 0.12.23, mosquitto (brew). Lima VM `sbc` (Ubuntu 24.04 arm64, 4 vCPU, 4 ГБ) — эмуляция платы. CASAS labeled_data.zip (236 МБ, CC BY 4.0) — трассы датчиков.
- 2026-10-06 — [daygen] `t` дня — секунды от полуночи суток (в заголовке `window_s: [25200, 82800]`, `t_origin: "midnight"`), а не от начала окна: так «текущее время» агента и ответы «когда…» совпадают с часами суток; §2 говорит «от начала дня» без уточнения — агент daygen.
- 2026-10-06 — [daygen] Расширения формата §2 (обратно совместимы): заголовок — `format`, `window_s`, `rooms`, `initial_state` (трасса до 07:00 свёрнута в состояние), `initial_user_room`, `situation_rules`, `counts`, `params`, `casas` (DOI, SHA-256); user_cmd — `class` (control/multi/invalid), `action`, `template` (massive:<id>), `room`, `resolve`, `session`, `turn`, `k`, `expect.calls_explicit`, `expect.goal` (проверка по состоянию); question — `class` (past/state/history), `qtype`, `answer.tol/accept/ref`, `expect.calls=[answer]`; situation — `evidence`, `source`, `expect.actions(+_explicit)`; невалидная команда — `expect.type=refuse` — агент daygen.
- 2026-10-06 — [daygen] Устройства — `<тип>_<локация>` (pir_kitchen, light_kitchen, door_front, contact_kitchen, thermo_living_room, boiler, leak_bathroom, valve_water, plug_living_room, coffee_kitchen, vacuum) вместо relay1/pir1 из примера §2; motion/open — целые 0/1, on — boolean, как в примере. Инвентарь — по локациям датчиков дома CASAS + синтетика S-room — агент daygen.
- 2026-10-06 — [daygen] 14 домов — набор GAP1 (12 с одним жителем, 2 с двумя); разбиение 7/7 стратифицировано по числу жителей, seed 20261006: tune hh103 hh105 hh106 hh107 hh114 hh118 hh122, holdout hh101 hh102 hh110 hh121 hh126 hh129 hh130; полных суток 520, пригодных (≥ 50 ON/OPEN в окне) 493; подмножество data/casas/subset воспроизводится байт в байт — агент daygen.
- 2026-10-06 — [daygen] Профили: L/M/M+/H по §12.1 + S69/S200 (стресс T0); сессии ~ Пуассон, реплик в сессии — геометрическое (как gen_day GAP1), не помещающаяся в окно сессия сдвигается, а не обрезается (иначе среднее < профиля); доля вопросов 0,25 и ловушек 0,10 — [допущения], параметры CLI; `k` вызовов LLM на реплику по VisionClaw — агент daygen.
- 2026-10-06 — [daygen] Команды: 171 из 220 пар MASSIVE iot размечены вручную (bench/daygen/massive.py, 49 исключены с причиной: время/расписание, разный смысл ru↔en, нет значения); «выключи свет» без комнаты — комната последнего движения; команда выбирается с предусловием по «идеальному» состоянию (иначе цель выполнена без агента); «голубой» = blue (cyan нет) — агент daygen.
- 2026-10-06 — [daygen] Ситуации из CASAS: door_left_open ≥ 10 мин, door_open_late после 22:00, no_motion_home ≥ 2 ч (тишина не у двери), long_absence ≥ 4 ч (после активности у двери ±10/1 мин; в hh122/hh126 нет контакта двери — прокси pir_entrance); синтетические overheat (котёл > 85 °C) и leak — ровно N/сут; дедлайн 300 с — значения для предрегистрации, трасса на действия агента не реагирует — агент daygen.
- 2026-10-06 — [daygen] Ручные переключения света жителем при смене комнаты (p = 0,3/0,5 в «тёмные часы», 0,02/0,2 днём, не чаще 30 мин на лампу; ≈ 19 событий/сут, source=manual) — [допущение], без них команды «выключи/приглуши» были бы почти неприменимы; ловушки пока только «невалидные» (нет устройства/комнаты), прочие классы §12.8 не генерируются; числовые сообщения CASAS (0–100) в день не попадают — агент daygen.
- 2026-10-06 — [sim-mcp] Explicit-инструменты действий — `<action>_<device>` (brew_coffee_kitchen, start_vacuum, dock_vacuum), как в hwmcp/internal/server (§4 задаёт только read_/set_); list_devices — компактный вид hw-mcp CompactView (`id, loc, kind?, props, actions`) с диапазоном и допустимыми значениями в подписи; результат — только текст с каноническим JSON (structuredContent — флаг `--structured-content`); отказ — isError + `{"ok":false,"denied":true,"reason","detail"}` — агент sim-mcp.
- 2026-10-06 — [sim-mcp] Ресурсы по умолчанию в схеме `hw://` (как hw-mcp) + `hw://state` (всё) и `hw://events` (изменения последнего продвижения часов); `resources/updated` — только подписанным (низкоуровневый Server SDK объявляет subscribe=false — переопределено), `--notify-all` — всем; MCP-сессии не закрываются по простою (T2 с долгими паузами) — агент sim-mcp.
- 2026-10-06 — [sim-mcp] Управление на том же порту: `POST /control/clock {t|to_t}` (назад — 409), `POST /control/input {id}` (явная привязка вызовов к реплике, рекомендуется реплееру), `/control/state`, `/control/report`, `/control/finish`; оценка: команда — цель достигнута вызовом агента в окне до следующей реплики (изменения от трассы не засчитываются), невалидная — refuse без побочных действий, вопрос — последний answer (время «ЧЧ:ММ» или секунды, ±60 с; для state-вопросов эталон — истинное значение в момент ответа), ситуация с действием — совпадающая запись за дедлайн; уведомления пользователю считаются по трассе edgectx — агент sim-mcp.
- 2026-10-06 — [bench/mockllm] Управление сверх §5: POST /control/clock {t} (виртуальное время для TTL эмуляции кэша), /control/expire, /control/clear, /control/reset, GET /control/state (inflight, idle_ms — реплеер ждёт «тишины»), GET /control/journal; заголовки-подсказки X-Bench-Clock|X-Edgectx-Clock, X-Bench-Input-Id|X-Edgectx-Input(s), X-Bench-Wake|X-Edgectx-Wake (точнее сопоставления по тексту реплики); все X-Bench-*/X-Edgectx-* пишутся в журнал (связь с трассой по X-Edgectx-Req-Id) — агент стенда.
- 2026-10-06 — [bench/mockllm] Оракул: «ничего не делать» = пустой content без tool_calls (не NO_ACTION; --noop-text); уведомление о ситуации — инструментом notify|notify_user|send_notification, если он в каталоге, иначе текст «Внимание: <name> [sit:<id>]»; ситуация с expect.actions (daygen) → действия + уведомление одним шагом; несколько активных ситуаций — один ответ; результат {"denied": true} завершает ход без повторов; explicit-каталог — по calls_explicit/actions_explicit daygen, иначе по правилу set_/read_/invoke_<dev>_… — агент стенда.
- 2026-10-06 — [bench/mockllm] Число вызовов на вход — по k из разметки daygen (VisionClaw 1/2/3/5): ход добирается до k шагами чтения состояния перед действием; вход с обязательным инструментом (команда, вопрос через answer) не короче 2 вызовов, поэтому в T0 E[k_eff] = E[max(k,2)] = 3,10, а не 2,89; для сверки T0 с прогнозом — costmodel.expected_k_effective и таблица h1_t0_oracle_k_eff; отключается --ignore-k — агент стенда.
- 2026-10-06 — [bench/mockllm] usage.prompt_tokens — приближение OpenAI Cookbook (константы gpt-4o-mini, вложенные свойства инструментов — рекурсивно) на o200k_base; tiktoken 0.14.0 и pytest 9.1.1 поставлены в .venv (uv), словарь o200k — локальная копия research/new_topic/GAP2_artifacts/.cache/tk (SHA-256 проверяется tiktoken), без сети; калибровка a + b·оценка — параметр --calib (E0-P). cached_tokens — эмуляция префикс-кэша по канонической последовательности system → tools → messages, ≥ 1024, шаг 128, TTL по умолчанию 300 с с продлением при попадании — агент стенда.
- 2026-10-06 — [bench/replay] Шаг часов ≤ 60 с в порядке mock /control/clock → edgectx /v1/clock/advance → sim /control/clock; ситуации регистрируются в t − 1 мс (все ситуации момента t вместе, при равных t ситуации раньше датчиков), ожидание снимается в t + deadline_s; перед репликой POST sim /control/input {id}, в конце POST sim /control/finish → simmcp_report.json; «тишина» после шага — по mock-llm (inflight = 0 и 25 мс без активности); --out — корень, прогон пишется в <out>/<run_id>/; проигрывается [min(начало окна, первое событие), max(конец окна, последнее событие)]; 404 необязательного вызова — пропуск, а не ошибка — агент стенда.
- 2026-10-06 — [bench/metrics] Правила: источник вызова LLM — reason из llm_request или последний wake; пустое пробуждение — не пользовательский wake с вызовами LLM без действия (кроме чтений, answer, refuse, create_rule) и без notify; реакция на ситуацию — notify с situation_id/меткой [sit:id], иначе первое свободное действие/уведомление непользовательского пробуждения в [t, t + deadline_s]; N_unsafe = максимум по доступным источникам (монитор actuator_safety*, исполненные записи шлюза с unsafe=true, tool_result.unsafe), без источников — null, а не 0; наклон PSS (tree.pss_kib, без PSS — RSS с пометкой) после первого часа только для прогонов ≥ 2 ч, TOST ±1 МиБ/ч по 90 % ДИ батч-средних (20 батчей); ₽ — наблюдаемые, «кэша нет», «весь segments.prefix из кэша» — агент стенда.
- 2026-10-06 — [bench/costmodel] Цены — исполнением research/new_topic/F_artifacts/price_table.py (compile/exec, а не importlib: не писать __pycache__ в research/) и его call_cost_rub; порт D_artifacts/cost_model.py без изменения алгоритма (паритет с исходником и out_R10/40/200 — тестом); тики heartbeat — строго внутри окна, ceil(W/P) − 1 (31 для 07–23 / 30 мин, как в расчёте концепции); ρ(M+) = 1,81, ρ(M) = 2,35, ρ(H) = 1,29; ρ = 1,28 при C* ≈ 32 команд/сут — агент стенда.
- 2026-10-06 — [harness/stats] Добавлены (прежние функции и 23 теста не менялись, добавлен 21 тест): sen_slope (совпадает со scipy.stats.theilslopes: Sen 1968 ур. 2.6, свободный член по Коноверу), sen_slope_batch_means, sen_slope_block_bootstrap (кольцевые блоки остатков), tost_slope (решение по (1 − 2α)-ДИ внутри (−δ, δ), p-значения не выдаются), mann_kendall (разведочно, без поправки Хамеда–Рао), batch_means_ci (t-квантиль через неполную бета-функцию, без scipy), block_bootstrap_mean/ratio (Politis & Romano 1992, L = ⌈n^(1/3)⌉) — агент стенда.
- 2026-10-06 — [bench/mockllm, bench/metrics] Пожелания к edgectx для точной атрибуции в T0: передавать в mock-llm X-Edgectx-Input (id входа), X-Edgectx-Wake (причина), X-Edgectx-Clock (виртуальное t запроса) и X-Edgectx-Req-Id; писать reason в llm_request; при вызове инструмента уведомления писать и запись notify с situation_id. Без них работают запасные правила (текст реплики, последний wake, метка [sit:id]) — агент стенда.
- 2026-10-06 — [daygen] Профиль нагрузки задаёт число реплик на окно дня («активный день»), не на 24 ч; для окна-доли суток (T2 14–22, интеграционный день 2 ч) — множитель `--rate-scale`; синтетические ситуации в коротком окне размещаются с пропорционально меньшими полями (окно 07–23 — без изменений), слишком короткое окно — явная ошибка, а не молчаливое уменьшение числа ситуаций — агент daygen.
- 2026-10-06 — [hw-mcp] Порядок проверок шлюза — как в прототипе actuator_gate.py: идемпотентность (повтор cmd_id) первой, затем реестр → схема → TTL → свежесть → блокировки/clamp → частота → подтверждение (в §4 идемпотентность стоит вместе с частотой); повтор исполненной команды не зависит от текущего состояния, паритет решений с прототипом подтверждён эталонными потоками (9 983 + 27 252 операций, tools/gen_golden.py) — агент hw-mcp.
- 2026-10-06 — [hw-mcp] Блокировки задаются данными в declared.json → gate (условия all/any/not над arg/state, eq/ne/lt/le/gt/ge; clamp set/min/max) вместо лямбд прототипа; невычислимое условие = отказ (fail-closed); значение состояния null (датчик не прочитан) = отсутствует → stale_state; ключи состояния "<устройство>.<свойство>" проверяются при старте; период опроса обязан быть меньше max_state_age_s — агент hw-mcp.
- 2026-10-06 — [hw-mcp] Инструменты шлюза: запись свойства P = set_P {"value"}; действия pulse {"seconds"} (логические выходы, таймер в hw-mcp) и renew_lease (устройства с lease_s, без проверок шлюза, как Gate.renew); explicit-каталог = read_<dev>_<prop>, set_<dev>_<prop>, <действие>_<dev> + board_health (без list_devices), generic = 4 общих + board_health — агент hw-mcp.
- 2026-10-06 — [hw-mcp] Сведения оркестратора — в _meta запроса tools/call, не в arguments (LLM их не формирует): edgectx/cmd_id, edgectx/issued_at (часы hw-mcp), edgectx/ttl_s, edgectx/confirmed, edgectx/dry_run; без cmd_id ключ генерирует hw-mcp (повтор = новая команда), без issued_at TTL не действует; повтор cmd_id возвращается как ok/verdict=duplicate (не isError) — агент hw-mcp.
- 2026-10-06 — [hw-mcp] Имена устройств для агента: id из declared.json для объявленных, иначе names[<id sysfs>] или slug("<место>_<драйвер>") (kitchen_bme280), а не short_id "kitchen.bme280" из ctx_formats.py — одно имя и в аргументах generic, и в именах explicit-инструментов (OpenAI: ^[a-zA-Z0-9_-]{1,64}$) — агент hw-mcp.
- 2026-10-06 — [hw-mcp] HTTP по умолчанию на 127.0.0.1:0 (свободный порт): в §10 диапазона для hw-mcp нет; URL печатается в stderr и -ready-file; виртуальные часы двигает POST /control/clock {"t"} (как у sim-mcp), для stdio — отдельный -control-addr; ревизия MCP по умолчанию закреплена 2025-11-25 (-protocol) — агент hw-mcp.
- 2026-10-06 — [hw-mcp] Результат инструмента — одно текстовое содержимое с каноническим JSON без structuredContent (не удваивать трафик MCP); уведомление resources/updated несёт _meta["hw/changed"]; порог уведомления сравнивается с последним сообщённым значением, первый отсчёт — база без уведомления; пороги по умолчанию: темп. 0,2, влажн. 1, давл. 50 Па (параметры эксперимента, фиксировать в предрегистрации) — агент hw-mcp.
- 2026-10-06 — [hw-mcp] Выходы GPIO: состояние = последняя исполненная команда (линия занята hw-mcp монопольно; модель PL061 в QEMU не возвращает уровень выхода), отметка времени обновляется при каждом опросе; при старте выход — в safe_state (иначе false), при завершении — safe_state; входы на Linux — события фронтов uAPI v2 (+debounce_ms), в эмуляции файлами — только опрос — агент hw-mcp.
- 2026-10-06 — [hw-mcp] Валидатор аргументов — порт monitor/argschema.py (паритет 4 144 случая + 46 check_schema), но обход свойств отсортирован (порядок ошибок может отличаться от Python), числа сравниваются как float64; схемы инструментов шлюза с непроверяемыми ключевыми словами отвергаются при старте — агент hw-mcp.
- 2026-10-06 — [hw-mcp] Фикстура sysfs скопирована в hwmcp/testdata/fixture_root (тест сверяет с research/…/fixture_root), эталоны паритета — hwmcp/testdata/golden из прототипов (research/ только читается); запись возможна только в выходы GPIO (IIO out_*, hwmon pwm* не подключены), горячее подключение не отслеживается — агент hw-mcp.
- 2026-10-06 — Пользователь одобрил: (1) «лестницу ограничений» — память 1024→32 МиБ, CPU 400→10%, сеть lan/4g/3g/edge/обрывы (tc netem), диск (io.max) — как эксперимент «карта выживаемости и эффективности» агентов; (2) сценарий с роботом (ROS 2 Jazzy + Gazebo Harmonic + TurtleBot3/Nav2 в Lima VM "sim", доступ через rosbridge и ros-mcp-server) как второй полноценный сценарий рабочего дня. Ключевая метрика — производительность и экономность работы во времени, не надёжность.
- 2026-10-06 — Первая реализация (wf edgectx-implementation) упала на 403 API в конце, но код записан и все тесты зелёные (Go ~17 тыс. строк + 8 тыс. тестов; Python ~15 тыс. строк, 270 тестов). Дальше: ревью/исправления/интеграция (wf edgectx-review-fix-integrate) и робот-симулятор (wf robot-sim-setup) параллельно.
- 2026-10-06 — [robot-sim] VM «sim» (Lima vz, Ubuntu 24.04 arm64, 6 vCPU/8 ГиБ/40 ГиБ, шаблон scripts/sim/sim.lima.yaml): проект смонтирован только для чтения; проброшен только TCP 9090 и только на 127.0.0.1 (rosbridge без аутентификации не публикуется в LAN). Путь host.lima.internal:9090 проверен изнутри «sim» (тот же usernet, что у «sbc»); саму «sbc» не трогали. Если «sbc» порт не увидит — hostIP 0.0.0.0 — агент robot-sim.
- 2026-10-06 — [robot-sim] Стек: ROS 2 Jazzy ros-base + ros-gz 1.0.24 (gz-sim 8.15, Harmonic) + turtlebot3_gazebo 2.3.7/navigation2 2.3.6 + nav2_bringup 1.3.13 + rosbridge_server 2.7.1, `--no-install-recommends` (669 МБ, 346 пакетов). Fast DDS при этом не ставится — RMW закреплён Cyclone DDS (RMW_IMPLEMENTATION), DDS только loopback (ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST, ROS_DOMAIN_ID=30) — агент robot-sim.
- 2026-10-06 — [robot-sim] Cyclone DDS с SocketReceiveBufferSize ≥ 16 МБ (scripts/sim/cyclonedds.xml) и net.core.rmem_max=32 МБ в VM: rosbridge подписывается best effort, и без этого кадры камеры 921 КБ (≈15 UDP-фрагментов) теряются полностью — агент robot-sim.
- 2026-10-06 — [robot-sim] Свой launch (scripts/sim/launch/robot_sim.launch.py) вместо turtlebot3_*.launch.py: только gz-сервер с --headless-rendering (EGL, Mesa llvmpipe); Fuel-модели Ground Plane/Sun заменены встроенными (старт без сети); RTF/шаг физики/частоты сенсоров переписываются в копиях мира и модели; выключенный сенсор удаляется (update_rate=0 в gz-sensors = «каждый кадр рендера»); Nav2 через nav2_bringup без RViz с начальной позой AMCL из позы спавна — агент robot-sim.
- 2026-10-06 — [robot-sim] Камера по умолчанию «ленивая» (camera:=lazy): Image/CameraInfo через parameter_bridge с lazy: true + ленивый image_transport/republish → JPEG q80; рендер только при подписчике. Smoke: RTF 0,99 и 0,85 ядра симуляции против 0,68 и 2,33 у камеры TurtleBot3 «всегда 30 Гц»; цена — первый кадр ≈1 с; режим как в TB3 — camera:=stream (baseline) — агент robot-sim.
- 2026-10-06 — [robot-sim] Истинная поза робота /ground_truth/pose (PosePublisher, 10 Гц, lazy) — для оценки сценария (цель достигнута), агенту не выдаётся; колёсная /odom за 4 м уходит на ~0,5 м — агент robot-sim.
- 2026-10-06 — [robot-sim] Замеры CPU стенда — по времени процессов группы симуляции и CPU процесса Virtualization.framework на хосте; /proc/stat в VM не используется (CONFIG_HZ=1000 + цикл физики 1 кГц: потиковый учёт занижал CPU в 3–4 раза). Все числа robot_smoke_20261006 — smoke при параллельной работе других агентов, не результаты — агент robot-sim.
- 2026-10-06 — [robot-sim] В .venv поставлены websockets 17.2 и cbor2 6.1.5 (uv) — клиент rosbridge scripts/sim/rosbridge_probe.py — агент robot-sim.
- 2026-10-07 — [robot-mcp] ros-mcp-server 3.1.2 (PyPI ros-mcp = тег v3.1.2, коммит 476591a, Apache-2.0) поставлен в venv пользователя: Mac `.venv-rosmcp` (83 пакета, 220 МБ) и VM sbc `~/.venv-rosmcp` (85 пакетов, 260 МБ; uv 0.12.23 в ~/.local/bin, sha256 проверен; системный Python и apt-пакеты sbc не менялись); состав — `uv pip sync` по scripts/sim/rosmcp/requirements{,-sbc}.lock — агент robot-mcp.
- 2026-10-07 — [robot-mcp] На sbc opencv-python заменён на opencv-python-headless 5.0.0.93: opencv-python требует libGL.so.1, которого на безголовой плате нет (ros-mcp падал на `import cv2`); libgl1 в систему sbc не ставили — агент robot-mcp.
- 2026-10-07 — [robot-mcp] Доступ из VM sbc к rosbridge через ws://host.lima.internal:9090 подтверждён (rosapi 100 тем, 32 мс; ros-mcp-server и R-compact работают оттуда); проброс порта «sim» остаётся на 127.0.0.1 — агент robot-mcp.
- 2026-10-07 — [robot-mcp] Baseline R-generic = ros-mcp-server 3.1.2 без правок; его свойства — часть измеряемого baseline, не исправляются: новое WS на вызов и потеря подписки при паузе > 0,5 с (кадр ленивой камеры 4/10), publish_once ≥ 2 с, send_action_goal всегда с feedback (0,9–1,6 МБ на цель) и таймаутом 5 с по умолчанию (ошибка при продолжающемся движении), пример TwistStamped без `twist` → `success`, робот стоит — агент robot-mcp.
- 2026-10-07 — [robot-mcp] Прототип R-compact — Python (scripts/sim/robot_mcp_compact.py: mcp SDK 1.30 + websockets + cbor2), только для замеров и как эталон ответов; 5 инструментов (status, go_to, patrol, stop, look), места — enum в схеме, ответ — один TextContent с каноническим JSON без structuredContent, одно постоянное WS, /scan и кадр в CBOR, Nav2 без feedback, /cmd_vel объявлен заранее; целевая реализация — Go рядом с hw-mcp + события вместо опроса (docs/robot_sim.md §12.7) — агент robot-mcp.
- 2026-10-07 — [robot-mcp] Оценки токенов: o200k Cookbook (bench/mockllm/tokens.py), задачи — «оракул» ReAct без кэша (scripts/sim/rosmcp_analyze.py); vision-токены — по developers.openai.com/api/docs/guides/images-vision (проверено 07.10.2026): патчи 32 px × k (gpt-4.1-mini 1,62; gpt-4.1-nano 2,46; gpt-5-mini 1,2; gpt-5-nano 1,5), плитки 512 px для gpt-4o/4.1/5/4o-mini без увеличения малых кадров — агент robot-mcp.
- 2026-10-07 — [robot-mcp] Кадр для LLM по умолчанию в R-compact — 320×240 JPEG q70 (≈4,6 КБ; 130 vision-токенов gpt-4.1-mini / 96 gpt-5-mini против 486 / 360 у 640×480), `look()` без кадра — сводка по лидару в поле зрения камеры (25 токенов); размер кадра — параметр эксперимента, проверить по качеству в E0-Q — агент robot-mcp.
- 2026-10-07 — [robot-scenario] Файл дня робота (daygen/1, `scenario: "robot"`) содержит только внешние входы: реплики, события стенда, синтетический заряд (`sensor` robot.battery_pct, шаг 5 %), ситуации с дедлайнами; поза/nav/alert/детекции порождает исполнитель (T0-модель или стенд), поэтому один файл годится для любой политики (парный дизайн). Новый вид `world` — только для стенда (старый реплеер пропускает с предупреждением), в expect — `calls_alt` (эквивалент для каталога ros-mcp, список) и `done_within_s`; места — семантические id (dock/door/corridor/window/shelf/desk/pass_ne/pass_sw ↔ dock/east/north/south/north_east/east_south/center_*) — агент robot-scenario.
- 2026-10-07 — [robot-scenario] Время робота — три уровня: T0-model (граф мест, t ≈ a + b·L, откалиброван по Gazebo: 11,1 + 2,0·L с) — для сравнения факторов; T1-gz — пошаговый режим, часы gz ведёт реплеер (run_to_sim_time), простой не симулируется — калибровка и проверка модели; T2 — реальное время. Причина: Nav2 недетерминирован (один участок 17,5–26 с, выброс 58,7 с) и потолок ускорения ≈2,7× (рендер лидара) — агент robot-scenario.
- 2026-10-07 — [robot-sim] start_sim.sh --world-control (launch world_control:=true): ros_gz_bridge отдаёт в ROS /world/default/{control,create,remove,set_pose}; по умолчанию выключено, конфигурации матрицы нагрузки не меняются. Измерено: шаги run_to_sim_time 2,55× с Nav2 и лидаром (15/15 целей), накладные расходы шага ≈3–4 мс, пауза 60 с без последствий, spawn/remove 7/5 мс — агент robot-scenario.
- 2026-10-07 — [robot-scenario] В виртуальных часах (T0/T1) go_to только асинхронный (синхронный блокирует /v1/clock/advance: ход ждёт сим. времени, реплеер ждёт конца хода); на паузе /scan и кадр по запросу не приходят (проверено) → адаптер читает скан из кэша подписки, кадр в T1 — микрошагом мира (тестовый флаг --sim-step, сдвиг сим. времени в журнал, вызовы управления не в трафике платы); таймеры адаптера — по /control/clock — агент robot-scenario.
- 2026-10-07 — [robot-scenario] Робот для edgectx — одно устройство `robot`; классы свойств: контекст (place до места, nav, battery_pct 5 %) — gate ignore, только дельта L1; alert — change/safety; done (итог задачи, начатой LLM) — change/background; итоги задач правил LLM не будят (_meta edgectx/by: rule|llm). Квантование L1 фиксируется в предрегистрации — агент robot-scenario.
- 2026-10-07 — [robot-scenario] Заменитель детектора на стенде — логическая камера Gazebo (идеальный детектор, верхняя граница качества сводки; плагин и тип LogicalCameraImage в стеке есть, не подключались); ONNX-детектор на плате — расширение; заряд публикует драйвер стенда как /battery_state (как у настоящего TB3), на действия агента не реагирует; «человек» — статичный цилиндр — агент robot-scenario.
- 2026-10-07 — [robot-scenario] robot-mcp (Go) размещается в модуле hwmcp (cmd/robot-mcp, internal/robot) и переиспользует canon/clock/trace/argschema и схему диспетчера уведомлений; hub не используется (он под sysfs, значения только float64/bool/nil). Для HR1 добавлен вариант R-raw (R-compact с --payload raw), чтобы разделить вклад размера каталога и формы данных — агент robot-scenario.
- 2026-10-07 — [robot-mcp Go] Адаптер робота R-compact v2 — отдельный Go-модуль robotmcp/ (github.com/local/robotmcp: cmd/robot-mcp, internal/{rosbridge,robot,server,cbor,imgutil,tokens,…}), а не cmd/robot-mcp в hwmcp, как в решении от 07.10 [robot-scenario]: hwmcp параллельно правит другой процесс; canon/clock/trace скопированы по образцу hw-mcp (internal-пакеты чужого модуля не импортируются), argschema не нужен (5 инструментов, своя проверка аргументов) — агент robot-mcp-go.
- 2026-10-07 — [robot-mcp Go] Зависимости: go-sdk v1.8.0 (ревизия 2025-11-25), github.com/coder/websocket v1.8.15 (клиент и фейковый сервер в тестах), gopkg.in/yaml.v3 (места, как в edgectx); CBOR — свой декодер (RFC 8949 + типизированные массивы RFC 8746) вместо fxamacker/cbor; уменьшение кадра — усреднение k×k на плоскостях YCbCr вместо x/image/draw (640→320 q70 ≈4,5 КБ, ≈4 мс CPU на sbc) — агент robot-mcp-go.
- 2026-10-07 — [robot-mcp Go] Умолчания ради трафика (замер со sbc): /scan только по запросу (CBOR; в простое — кэш, снятый после остановки, ≤ 30 с), без подписки на время движения; /amcl_pose — throttle_rate 2000 мс, queue_length 1. Поездка dock→door — 6,2 КБ rosbridge против 37–41 КБ «жадно» (скан 2 с + каждая поза), 13 КБ у прототипа и 926 КБ у ros-mcp; 120 с простоя — 0 байт. Подписка /scan в движении (тревога «препятствие впереди» до прерывания Nav2, T1 на паузе) — флаг -motion-scan-ms, расходится с robot_scenario.md §10.2, где она предполагалась по умолчанию — агент robot-mcp-go.
- 2026-10-07 — [robot-mcp Go] Уведомления resources/updated — только task_end (итог любой задачи) и alert (blocked: Nav2 прервал цель или обрыв rosbridge; stuck: нет позы AMCL 30 с при активной цели или участок > 150 с по часам адаптера; low_battery ≤ 20 %; person — детектор-заглушка); смена места на участках и шаг заряда — молча (флаг -notify-context); в _meta — полное состояние (place, nav, battery_pct, alert, done, seen; ≈44 токена), robot/changed, robot/event, robot/detail; тревоги снимаются без уведомления (stuck — когда поза пошла, blocked — новой задачей, low_battery — выше порога + 5 %) — агент robot-mcp-go.
- 2026-10-07 — [robot-mcp Go] done = {seq, task, place, status, visited?, seen?, view?} меняется только итогом задачи, начатой LLM: не меняется у задач с _meta edgectx/by = rule, отменённых stop или вытесненных новой целью и у go_to с wait=true. Место после подтверждения Nav2 берётся из цели; place_m не сообщается, пока поза AMCL старше остановки (поза обновляется по сдвигу ≥ 0,25 м, robot_sim §12.7 п. 6); «в месте» — ближе place_radius_m = 0,45 м, иначе between — агент robot-mcp-go.
- 2026-10-07 — [robot-mcp Go] Цена асинхронного go_to/patrol, измеренная оракулом: +1 обращение к LLM (≈430 входных токенов) на команду, о прибытии по которой надо сообщить пользователю: 3 запроса и 1 325 токенов против 2 и 795 у синхронного прототипа (тот блокирует ход на 16–90 с, а в виртуальных часах невозможен). Синхронный wait — только флагом -wait-param при -clock real. Предложение для EdgeCtx: уведомление по done — шаблоном гейта без LLM — агент robot-mcp-go.
- 2026-10-07 — [robot-mcp Go] look(place) по robot_scenario.md v2 оставлен по умолчанию (каталог 274 токена Cookbook против 229 у прототипа: сам параметр ≈57 токенов в каждом запросе); -look-place=false даёт 217. Выбор — фактор предрегистрации: «осмотр места» ≈1,5/сут экономит 2 пробуждения, а параметр стоит ≈57 × число запросов/сут — агент robot-mcp-go.
- 2026-10-07 — [robot-mcp Go] Сводки лидара (free_m по секторам, in_view в поле зрения камеры 62,2°) совпадают с прототипом на одном фейковом стенде (тест паритета), кроме ограничения occupied_pct ≤ 100 (у прототипа бывает 102 %); status не отдаёт x/y/курс и nearest (scenario v2): 52–53 токена против 77–96; короткая ошибка неизвестного места — 33 токена против 88 — агент robot-mcp-go.
- 2026-10-07 — [robot-mcp Go] Учёт в адаптере: байты rosbridge на уровне TCP (кадры WS + рукопожатие; на смоуке совпали со счётным прокси robot-probe до байта) и по видам сообщений; оценка токенов ответа без словаря (предразбивка o200k + длина куска; на эталоне 146 текстов +0,9 %, 134/137 коротких в ±15 %) — только журнал/диагностика; числа для ПЗ — tools/analyze.py (точный o200k, Cookbook) — агент robot-mcp-go.
- 2026-10-07 — [robot-mcp Go] Smoke 07.10 (стенд --nav2 --camera lazy, при параллельной нагрузке): PSS на sbc 11,0 МиБ в простое, 13,7–14,0 после сценария, 15,1 после 6 циклов (12 поездок + 6 кадров), против 45,8→50,5 у прототипа и 109→110 у ros-mcp; старт 3 мс; кадр 10/10, p50 684 мс. На паузе мира (стенд --rtf 0 --world-control) /scan и кадр не приходят; -sim-step-iters 100 (multi_step через /world/default/control) даёт status за 0,36 с и кадр за 0,99 с. Участок corridor→dock на sbc дважды шёл 65–77 с (восстановление Nav2, сработал stuck через 30 с) — агент robot-mcp-go.
- 2026-10-07 — [baselines] PicoClaw v0.3.1 (git 2cf030d2, MIT) — бинарники GitHub releases darwin-arm64 и linux-arm64 (статический, 34,5 МБ); SHA-256 архивов совпали с digest GitHub API и picoclaw_0.3.1_checksums.txt (Darwin 30266e4e…, Linux e78ab0de…), install_picoclaw.sh идемпотентен, исходники тега — только для чтения кода (bench/baselines/vendor, не в репозиторий). nanobot 0.3.5 (MIT) — uv venv Python 3.12: Mac .venv-nanobot (3.12.15), sbc ~/.venv-nanobot (база — системный 3.12.3, система не менялась); lock-файлы bench/baselines/requirements (90 пакетов, версии на Mac и sbc одинаковые) — агент baselines.
- 2026-10-07 — [baselines] У аналогов нет виртуальных часов: день проигрывается в реальном времени, сжатом в k раз (bench/baselines/timecomp.py) — периоды систем /k в пределах их конфигов, TTL кэша mock-llm /k (--clock wall), часы sim-mcp — время дня от проигрывателя. Корректны только счётные метрики (вызовы LLM, токены, кэш-токены, байты, число тиков); CPU-с/сутки, задержки, наклон памяти, энергия — нет (только T2); неточности периодов пишутся в manifest.deviations каждого прогона — агент baselines.
- 2026-10-07 — [baselines] PicoClaw heartbeat — целые минуты ≥ 5 (minIntervalMinutes): точно только при k ∈ {1,2,3,5,6}; при других k штатный heartbeat выключается и эмулируется задачей cron every = 1800/k с с тем же промптом «# Heartbeat Check» (новая сессия на тик, без строки Current time, HEARTBEAT_OK уходит в канал) — отклонение от «как поставляется». nanobot: Dream при k = 60 — устаревшее поле dream.cron "*/2 * * * *" (точно), idle-компакция — целые минуты: 1 мин = 60 мин суток вместо 15 (отклонение), опрос local triggers 0,5 с стенных = 30 с суток (не настраивается), maxSessionMessagesPerMinute ×k — агент baselines.
- 2026-10-07 — [baselines] Реплики без человека — штатные каналы: PicoClaw — MQTT-канал (/picoclaw/edge/user/request|response; брокер — mosquitto на Mac, запасной — свой mqttlite), nanobot — WebSocket-канал WebUI (кадр {"content"}, ответ до turn_end; мост nb_bridge рядом со шлюзом). MCP-клиенты обоих берут только инструменты (PicoClaw: NewClient без обработчиков, standalone SSE выключен; nanobot: ресурсы — как инструменты mcp_<srv>_resource_*, без подписок), поэтому события — пушем: PicoClaw — MQTT-клиент sensors, nanobot — local triggers (/trigger sensors + LocalTriggerStore.enqueue = CLI nanobot trigger). Что пушить, решает внешний фильтр с предикатами сигналов гейта EdgeCtx (edgectx_simday.yaml) без debounce/cooldown/коалесценции/правил; --push all|none — границы — агент baselines.
- 2026-10-07 — [baselines] llmtap — прокси агент → mock-llm (mock-llm не правится): nanobot 0.3.5 всегда зовёт провайдера потоком — прокси снимает stream и отдаёт OpenAI SSE; класс вызова (user/heartbeat/event/internal:*) — по маркерам исходников тегов (тест «дрейфа маркеров») или X-Edgectx-*; подсказки оракулу X-Bench-Wake/Purpose (внутренние вызовы → summary, чтобы компакция не «съедала» ожидания ситуаций; оценщик heartbeat nanobot → фон); сводка укорачивается до цели запроса («Target length: about N tokens», seahorse PicoClaw) с правкой usage — иначе фиксированные 400 токенов оракула давали лишний «агрессивный» вызов на каждую сводку (18 внутренних вызовов вместо 9 на дне смоука) — агент baselines.
- 2026-10-07 — [baselines] «Ничего не делать» оракула — по протоколу системы (свой mock-llm на прогон: PicoClaw HEARTBEAT_OK, nanobot «All clear.», EdgeCtx пусто). PicoClaw-best: cron command (проверка помещения без LLM каждые 5 мин суток, вывод в MQTT alerts) требует штатного tools.exec.allow_remote = true; seahorse; Tool Discovery в T0 выключен — скрытые MCP-инструменты вызываемы только после поиска (TTL), оракул так не умеет (флаг --discovery). nanobot: tools.ssrfWhitelist 127/8 (+192.168.5/24 в ВМ), иначе SSRF-фильтр блокирует MCP по локальному адресу; web-инструменты выключены у обоих — агент baselines.
- 2026-10-07 — [baselines] Топология: сервисы стенда (mock-llm, llmtap, sim-mcp, брокер) на Mac, агент — на Mac или в ВМ sbc (host.lima.internal → 127.0.0.1 Mac проверено); проект в ВМ только для чтения — рабочий каталог ~/baselines/<run_id>, после прогона забирается в <run>/agent и удаляется; память в ВМ — sysmon-linux-arm64 (RSS/PSS), на Mac — psutil. Порты: PicoClaw 18120–18126, nanobot 18127–18133, EdgeCtx/тесты 18134–18137. Часы sim-mcp удерживаются перед неотправленной репликой (иначе при сжатии sim-mcp приписывал вызовы хода следующей реплике: 17/18 вместо 18/18) — агент baselines.
- 2026-10-07 — [baselines] Паритет режима времени: drive_edgectx гонит EdgeCtx тем же проигрывателем (clock: real, heartbeat_min, гейт, gap_s сигналов, snapshot/profile периоды /k; for_s правил и TTL шлюза не масштабируются). Рекомендация agent/: режим часов clock: scaled (реальные часы с темпом k) — точный паритет, включая время в промпте и ts_clock — агент baselines.
- 2026-10-07 — [baselines] Смоук (2 ч дня при k = 60, оракул, параллельная нагрузка — ориентиры): на одном дне все системы 22/22 реплики, 18/18 команд; вызовов LLM за окно EdgeCtx 77, PicoClaw-best 91 (shipped 84, из них 17 сводок legacy-контекста), nanobot 90; входных токенов 0,20 / 0,62–0,64 (shipped 0,38) / 0,84–0,90 млн; инструментов в запросе 12 / 22–27 / 54–70; ситуации 2/2, кроме PicoClaw-shipped без пуша (0/2 за дедлайн 300 с); RSS дерева PicoClaw 48–59 МиБ, nanobot 167–188 МиБ, EdgeCtx 28 МиБ (bench/baselines/smoke/20261007/table.md) — агент baselines.
- 2026-10-07 — [monitor] Доведено прерванное исправление по ревью sysmon_monitor (без новых проверок): мутант pulse_overrun не применялся ни к одному синтетическому прогону — в стенд tracegen добавлен импульсный выход relay (pulse {seconds: 0 < s ≤ 3600}, min_interval_s 10, как выходы GPIO hw-mcp; в прототипе GAP3 импульсов нет — устройство добавляет tracegen.demo_setup(), решение пишет pulse_until, конец импульса pulse_end{scheduled} в срок + 1–20 мс ведёт генератор), тот же стенд — в тесте паритета с прототипом; мутант no_lease не берёт команду, которую покрывает аренда, выданная в тот же момент предыдущей командой вентилятору (эквивалентный мутант, проявился после сдвига корпуса). Код монитора не менялся, monitor/examples не перегенерированы. pytest monitor/tests bench/tests harness/tests — 343 passed; mutation_suite --seeds 8 — 89 мутантов, 1813/1813 — агент monitor-fix-finish.
- 2026-10-07 — [hw-mcp] Доведены до согласованного состояния прерванные исправления по ревью (без новых функций). (1) Уведомления асинхронные: hub.OnChange(func(Update)) только ставит изменение в очередь рассыльщика server/dispatch.go (числа сворачиваются, переходы bool сохраняются до 8 на устройство), _meta уведомления = hw/changed + device + state. Зависший клиент stdio больше не останавливает сэмплер, сторожевой таймер и обработчик фронтов. (2) По SIGTERM и концу сессии safe_state ставится сразу (context.AfterFunc → hub.Close, от транспорта не зависит). Закрытие stdio ждётся не дольше 3 с. В run_end добавлен notify_dispatch. (3) Флаг -watchdog-tick убран: cmd запускает hub.RunTimers/RunSampler, которые спят до срока (без аренд и импульсов таймеры не просыпаются, фаза опроса сохраняется). В ready добавлены sample_periods. (4) Исправлена ошибка начатой правки: шаг квантования q считается после решения об уведомлении, иначе первое же изменение откладывалось на Persist отсчётов. (5) Тесты обновлены по смыслу: FailLimit, board_health→Diag, период w1 10 с, _meta.state. Добавлены регрессионные тесты: TestStdioStalledClient (e2e, без dispatcher падает), TestDispatcherNeverBlocks, TestRealTimeLoopsSleepUntilDue, TestQuantizationJitterAndGlitches, TestDriverSamplePeriods, TestRateLimitHandler. Проверено: go vet (darwin и linux/arm64), gofmt, go test -race ./..., статическая сборка linux/arm64. Не сделано (вне объёма): sample_periods в declared.json и флаг периодов драйверов, SessionTimeout HTTP, uAPI v1 GPIO для Jetson, описание board_health (меняется вместе с bench/simmcp/catalog.py) — агент hw-mcp.
- 2026-10-07 — По просьбе пользователя вся работа остановлена (воркфлоу, обе Lima VM выключены), доведены до зелёных тестов два прерванных исправления (hw-mcp: асинхронные уведомления и таймеры «до срока»; monitor: мутант pulse_overrun). Все тесты: Go 46 пакетов ok, Python 394 passed. Собран пакет документов для руководителя: docs/supervisor_pack/ (01 коротко, 02 что изучил, 03 подход, 04 что сделано и первые замеры, 05 план; out/ — PDF/DOCX и общий PDF), проверен 4 агентами (факты против журналов замеров, согласованность, рецензия, язык); завышения убраны (агент на Jetson не запускался; сервера камеры ещё нет; ревью агента прервано; «таймер против фильтра» ещё не измерен; сравнение с PicoClaw — 1,9× после поправки ревью).

## 2026-10-07 — сенсорное ядро после передачи HANDOFF

- 2026-10-07 — [объём, пользователь] Работа возобновлена: делать ключевую общую работу с датчиками;
  робот и камера отложены до понимания доступной периферии — прямое указание в чате. Платы и
  ключа RouterAI нет, точная модификация Nano и датчики не установлены. Модель внешней LLM не выбрана;
  nanobot не считать одобренной руководителем заменой NanoClaw.
- 2026-10-07 — [hw-mcp] Подключены уже предусмотренные Hub периоды к `declared.json.sample_periods`
  по устройству/свойству, с проверкой конечного положительного значения, существования ключа
  и свежести интерлоков; источник `declared` виден в ready/diag — частоту чтения можно выбрать
  после получения датчиков, не меняя MCP-контракт.
- 2026-10-07 — [агент] Очередь MCP ограничена 1024 уведомлениями, переполнение явно диагностируется;
  исправлена гонка снимка StateURIs и округление начального/целочисленного показания. Window/mask
  с окном держат в RAM только хвост, summary освобождает покрытую сводкой часть; полный архив
  SQLite сохраняется. `history_turns` считает архив, `history_cached_turns` — кеш.
  `mcp.updates` отдаётся явно с нуля — длительный поток не должен скрыто накапливать историю,
  а реплеер должен иметь точную исходную точку синхронизации.
- 2026-10-07 — [стенд] HTTP-ошибки реплик больше не считаются успехом; при обязательном сбое
  сохраняется manifest со статусом aborted. `sync=updates` не сбрасывает накопленную цель при
  таймауте; отсутствие точного счётчика/таймаут прерывает прогон. Перед дедлайном и finish
  проверяется окончание хода. Метрики учитывают фактически проигранное окно и подтверждённый
  результат инструмента — предотвращение завышенной полноты и ложных успешных прогонов.
- 2026-10-07 — [метрики] Сжатое/виртуальное время явно помечает CPU/сутки, наклоны памяти и задержки
  невалидными для сравнения T2; диагностические числа сохраняются. sysmon не добавляет пустой
  финальный sample; end.oom сообщает наблюдение cgroup либо unknown, не причину выхода PID.
  `--cgroup auto` отвергает группу с самим sysmon. Собственные ctxsw учитываются, точные wakeups
  остаются null — границу и ограничения наблюдения нельзя подменять нулями.
- 2026-10-07 — [аналоги, настройка стенда] PicoClaw-best использует legacy-контекст и штатным
  конфигом отключает лишние для сенсорного сценария инструменты; исходники аналога не менялись.
  Умолчание сжатия стенда k=15 вместо 60 уменьшает искажение целоминутной idle-компакции nanobot;
  отклонения таймеров остаются в manifest. Это настройка проверки, а не новый результат сравнения.
- 2026-10-07 — [проверка] Добавлена `python -m bench.sensors`: свежая сборка EdgeCtx, условные
  числовой/логический датчики через sim-mcp, mockllm, gate/heartbeat/hybrid, уникальный каталог
  артефактов и хеши. Успех требует ответов и реакций на обе значимые ситуации. Отдельный тест
  `test_hwmcp_edgectx.py` проверяет настоящий hw-mcp на изменяемой копии sysfs-фикстуры.
  Это проверка интеграции; все числа на mock — ориентир, не результат. Текущий статус и
  незакрытые ограничения: `docs/SENSOR_CORE.md`.
- 2026-10-07 — [сохранность] Перед крупной правкой сделаны архив и SHA-256 исходников в
  `~/Documents/ChatGPT/диплом/backups/sensor-core-20261007-01.before.*`; разработка и проверки
  выполнены в отдельной копии `sensor-core-20261007-01`. VM и реальные API не запускались,
  прежние результаты и компоненты робота/камеры не менялись — проект пока без Git.

## 2026-10-09 … 2026-10-15 — «минимальная плата», обрывы связи, робот NeuroChip

Проверено на Mac и в Lima VM `sbc` (Ubuntu 24.04 arm64; 09.10 — ядро 6.8.0-106, после перезагрузки — 6.8.0-146)
с `bench/mockllm`, стенд NeuroChip — в ВМ `sim`; все числа — ориентир, не результат. На настоящей плате
(Jetson Nano, JetPack 4.6, ядро 4.9), датчиках, камере, роботе и с реальной LLM ничего из ниже перечисленного
не проверялось. Готовое — ядро Linux (cgroup v2, netns, `tc netem`,
ifb, `gpio-sim`, `vivid`, `iio_dummy`), systemd, MCP Go SDK, rosbridge, Nav2, ROS 2, Gazebo, PicoClaw — своим
не считается.

### hw-mcp, cam-mcp, robot-mcp, sysfsim (09.10)

- 2026-10-09 — [hw-mcp] GPIO uAPI v1 рядом с v2 (`golang.org/x/sys/unix`, без cgo); `-gpio-uapi auto`
  для каждого чипа пробует `GPIO_V2_GET_LINEINFO_IOCTL`, при `EINVAL`/`ENOTTY` берёт v1; дребезг в v1
  подавляет сам hw-mcp — у Jetson Nano ядро 4.9, а v2 есть только с 5.10 (v1 — с 4.8, docs.kernel.org).
  Проверено на gpio-sim ядра 6.8 в ВМ для v1 и v2 (`experiments/results/hwmcp_vmchecks_20261009_a`,
  `…_20261014_a`); на ядре 4.9 — нет. Закрывает пункт «uAPI v1 для Jetson» записи 07.10.
- 2026-10-09 — [hw-mcp] `-gpio-driver auto|cdev|files` и смешанный режим (sysfs из каталога симулятора,
  линии — настоящий `/dev/gpiochipN`); `chip` в `declared.json` — `gpiochipN` или метка чипа, метка должна
  быть ровно у одного чипа, иначе ошибка запуска — номер узла меняется с набором драйверов, метка нет;
  смешанный режим даёт проверить приводы и фронты на gpio-sim при эмулированных датчиках.
- 2026-10-09 — [hw-mcp] Живые датчики: показание `null` с причиной `absent` / `unreadable` / `not_a_number` /
  `stuck`, `sensor_faults` и `sensor_errors` в `board_health`, история — `hw://diag`; на плате (`-root /`)
  дескрипторы атрибутов держатся открытыми — в ВМ на `iio_dummy` номер пропавшего устройства сразу занимало
  другое, и hw-mcp читал чужие значения (нашёл `tools/vm_hotplug_check.sh`). Модель устройств не меняется,
  `list_changed` нет — стабильный префикс L0; `stuck_after_s` по умолчанию выключен (параметр
  предрегистрации); журнал сбоя — не чаще раза в 60 с на свойство (допущение).
- 2026-10-09 — [hw-mcp] Свой учёт брошенных HTTP-сессий (`-session-timeout`, 1 ч — допущение) вместо
  `SessionTimeout` SDK — SDK считает активностью только POST и закрыл бы сессию агента, который ночью лишь
  слушает поток SSE; сессия закрывается, только если у неё нет ни запросов, ни открытого GET. С
  PicoClaw/nanobot не проверялось. Закрывает пункт «SessionTimeout HTTP» записи 07.10.
- 2026-10-09 — [cam-mcp] Детектор движения на плате (вычитание фона, без ML), кадры в LLM не уходят: агенту —
  уведомление о начале и конце эпизода (≈450 байт), маленький JPEG — по запросу — кадр 640×480 стоит
  ≈490 входных токенов gpt-4.1-mini в каждом запросе. Контракт ресурсов как у hw-mcp: агент подключает
  камеру без правок своего кода (`bench/tests/test_cammcp_edgectx.py`, смоук
  `experiments/results/agent_20261014_twoservers`). Источники кадров: `synthetic` по умолчанию (камеры на
  кафедре может не быть; сцена с известной истиной для тестов), `dir`, `v4l2` без cgo. Проверено:
  синтетика, `vivid` в ВМ (`cammcp_vm_smoke_20261009_{synthetic,v4l2}_r2`); настоящая USB-камера — нет.
- 2026-10-09 — [cam-mcp] По ревью: фон хранит оценку истинной яркости (Q16.16, может быть выше 255) —
  простое ограничение прогноза до 255 давало ложную тревогу на выключении сильной лампы; эпизод собирается
  только из окна K из N (кольцо 32 кадров); отказ источника — как у датчиков hw-mcp (3 ошибки подряд →
  `motion: null` + `camera_lost`, первый удачный кадр → `camera_back` и переучивание фона); захват кадра вне
  блокировки детектора; свежесть кадра V4L2 — по номеру кадра драйвера. Открыто: V4L2 не переоткрывается
  после отключения USB, камера стримит на `-active-fps` и в покое.
- 2026-10-09 — [robot-mcp] Все отмены целей — сервисом `/navigate_to_pose/_action/cancel_goal` «отменить все»
  (`CancelAllGoals`) при stop, таймауте участка, завершении и первым сообщением каждого подключения, с
  ожиданием ответа ≤ `-cancel-timeout` (1 с — допущение) — rosbridge 2.7.1 не отменяет цели отключившегося
  клиента: робот доезжал бы до цели, о которой агент уже не знает. Подтверждение — `result: true` и
  `return_code` 0; без ответа соединение рвётся, и инструментам оно открывается только после подтверждённой
  отмены — rosbridge вызывает сервисы в отдельных потоках (`call_services_in_new_thread=true`), поздняя
  отмена сняла бы следующую цель; вариант «ждать отмену в полёте» отклонён: на молчащей связи `go_to`
  блокировался бы минутами. `-cancel-timeout` должен быть меньше 5 с (таймаут сервиса rosbridge) — код
  этого не проверяет. Проверено на fakebridge и в ВМ `sbc`; на Gazebo/Nav2 — нет.
- 2026-10-09 — [robot-mcp] Устаревшее состояние помечается, а не выдаётся за текущее: `place_age_s` без связи
  с rosbridge, `displaced` при сдвиге робота извне в простое (окно `-pose-throttle-ms` + 1 с по стенным
  часам — в T1 виртуальные прыгают; пороги 0,1 м / 0,1 рад — допущение), `lidar_age_s` и
  `"lidar":"no data"`; цель задачи LLM, отменённая не адаптером, даёт `done.status: canceled`. Новых
  свойств L1 нет — контракт гейта EdgeCtx не меняется. Пинга WebSocket нет — он стоит трафика в простое:
  «молчащую» связь без RST заметит только TCP (≈2,75 мин в простое, ≈15 мин с неподтверждёнными данными) или
  отмена без ответа, зависший rosbridge при живом TCP не замечается (не проверялось).
- 2026-10-09 — [sysfsim] Симулятор «живого sysfs» (`bench/sysfsim`) — только стандартная библиотека Python
  3.12 — запускается системным Python ВМ из смонтированного только для чтения дерева проекта. Атрибут
  меняется целиком (временный файл + `rename(2)`) — читатель видит старое или новое значение, не
  недописанное (тест с параллельным читателем). Линии GPIO: `files` (как `gpio.Files` hw-mcp) или модуль
  ядра `gpio-sim` (настоящие фронты от ядра). Дребезг геркона ≤ 0,5 мс — даташиты Standex-Meder, число
  возвратов — допущение. Своё — модели датчиков, физика, отказы, сценарии, журнал истины.
- 2026-10-09 — [sysfsim] Сигнал остановки не прерывает запуск, шаг мира и уборку: обработчик пуст, номер
  сигнала идёт в канал (`signal.set_wakeup_fd`), прогон ждёт канал в `select` — исключение из обработчика
  оставляло окна при запуске и уборке (на gpio-sim — риск снять занятый чип).

### Агент EdgeCtx: плохая сеть, падение MCP, память и SD (09–14.10)

- 2026-10-09 — [edgectx] Связь с LLM: раздельные `connect_timeout_s` 10 (допущение: при RTO 1 с успевают SYN
  и три повтора) и `timeout_s` 60; повтор внутри обращения — только сетевая ошибка до ответа, 429, 5xx;
  умолчания по `openai-python` (2 повтора, 0,5 с, предел 8 с; проверено 09.10.2026); таймаут попытки и
  оборванный ответ не повторяются и не откладываются (`maybe_billed: true`) — запрос мог быть уже обработан и
  оплачен.
- 2026-10-09 — [edgectx] Предохранитель LLM (`llm.breaker`: размыкание после 1 сбоя, проба `GET /models` без
  токенов по часам агента, пауза 30 → 300 с — допущения). Пока модели нет, правила, гейт и MCP работают;
  пробуждения откладываются (`deferred`) в очередь `sched.deferred_max` 32 (предел именно пробуждений), при
  восстановлении — один слитый ход с причиной `deferred` (`wake.coalesced`) — без шквала запросов после
  обрыва; вопрос пользователя без связи сразу получает HTTP 202 `deferred` — канал синхронный, держать
  запрос часами бессмысленно, а повтор на каждый вопрос занимал бы слот LLM таймаутом. 14.10 по ревью:
  удачная проба только полуоткрывает предохранитель, счётчик обнуляет лишь ответ модели — `/models`
  агрегатора отвечает и при мёртвой модели (в тесте 18 пробных ходов за час вместо 157).
- 2026-10-09 — [edgectx] Падение MCP-сервера: конец сессии = `mcp_down`; переподключение по настенным часам
  (`reconnect_s` 1 → `reconnect_max_s` 60, допущение), пауза не сбрасывается после короткой сессии; для
  SSE повторы SDK выключены (`MaxRetries` −1) — серверы не хранят события, повторы лишь откладывали бы
  обнаружение; после `mcp_up` — перечитать инвентарь, подписку и состояние, при неудаче повтор
  (`mcp_resync`); «нет связи» снимается с каждого устройства только свежим показанием — иначе модель
  получила бы старое значение как свежее.
- 2026-10-09 — [edgectx] `delta_tail`: дельта L1 считается показанной модели только после её ответа
  (`Commit`) — при ошибке LLM изменения терялись. `run_start` — всегда первая запись трассы: правила
  взводятся и события MCP доставляются после неё. Закрывает «Открытые программные ограничения»
  `docs/SENSOR_CORE.md` из записи 07.10 (переподключение MCP, `delta_tail` при ошибке LLM, порядок `run_start`).
- 2026-10-09 — [edgectx] Память и SD-карта: SQLite `journal_mode=wal` + `synchronous=normal`, кэш 256 КиБ
  (вне WAL разрешён только `full`) — по документации SQLite при сбое питания база остаётся согласованной,
  теряются лишь последние фиксации; буфер трассы со сбросом раз в 2 с; `coalesce_max` 32; журнал L2 —
  кольцо без выделений памяти; шаблоны ресурсов состояния раскрываются только по устройствам своего сервера
  (при двух серверах подписки сыпали ошибками). Лимит памяти — только снаружи (`GOMEMLIMIT`), фактические
  `gomemlimit`/`gogc`/`gomaxprocs` пишутся в `run_start.runtime`. На SD-карте не проверено.
- 2026-10-14 — [edgectx] Проверено в ВМ (`experiments/results/agent_20261014_vmlink`, повтор
  `agent_20261009_vmlink2`, исход совпал; ориентир): netem loss 100 % → таймаут с `maybe_billed`, без
  повтора → `llm_unavailable`, перегрев и вопрос отложены, вопрос сразу получил 202; после восстановления —
  один слитый ход, `user_reply` с `deferred: true`; остановка и запуск hw-mcp → `mcp_down` → `mcp_up`,
  значение, записанное без связи, прочитано заново. Многочасовые обрывы, реальный RouterAI (размер
  `/models`, `Retry-After`) — не проверены.

### Эмуляция «минимальной платы» (14.10)

- 2026-10-14 — [board] Эмуляция платы — `bench/board` + `scripts/board/board.sh` (своё — обвязка, профили,
  `verify`/`calibrate`; готовое — cgroup v2, systemd, netns/veth, `tc netem`, ifb, nftables). Оси: память
  1024…16 МиБ, CPU 400…5 %, ядра 4/2/1, диск `nvme`/`sd-good`/`sd-slow`/`sd-awful` (`io.max`), сеть — 12
  профилей (lan … gprs, lossy, satellite, offline, flaky с расписанием). Пресеты: `jetson-nano-4g` (стеку
  1024 МиБ из 4 ГБ, общих с GPU, — допущение), `jetson-nano-2g`, `pi-zero-2w`, `potato`, `abyss`; CPU
  пресетов — допущение до `calibrate` на плате.
- 2026-10-14 — [board] Юнит — служба `systemd-run --wait`, а не scope — только у службы есть
  `NetworkNamespacePath=`, `User=`, `RuntimeMaxSec=`, `ExecStopPost=` со счётчиками cgroup до её удаления и
  итог systemd (`oom-kill`, `timeout`); пустая scope удаляется сразу. Срок — `--timeout` → `RuntimeMaxSec` в
  самой ВМ, `Vm.run` сам останавливает юнит при таймауте и Ctrl+C — `limactl shell` без tty не передаёт
  сигналы в ВМ. `OOMPolicy=stop` — первое OOM-убийство = ступень не пройдена.
- 2026-10-14 — [board] `GOMEMLIMIT` = 45 % `MemoryMax`, задаётся снаружи — Go GC guide советует 5–10 % на
  память вне рантайма, остаток делится на два Go-процесса стека (агент и hw-mcp); `GOMAXPROCS` = число ядер
  платы — Go ≥ 1.25 урезает его по квоте cgroup, а на плате квоты нет; период квоты 20 мс — при 5 % это 1 мс
  каждые 20 мс, ближе к равномерно медленному процессору.
- 2026-10-14 — [board] Сеть — свой netns + veth, netem в обе стороны по пакету; «вниз» — на ifb внутри
  netns (`tc mirred` с входа `bn-<ns>`), а не на `bh-<ns>` — на eth0 ВМ работает GRO: 506 из 600 пакетов
  были склейками до 14 200 Б, и потеря, разброс и место в очереди доставались склейке целиком; ifb с
  `numifbs=0`, корневой netns не меняется. Очередь netem 1 с — допущение по Jiang et al., IMC 2012.
  Расписание с `repeat: false` требует `after`; `--disk` — только на блочном устройстве (иначе systemd лишь
  пишет в журнал и запускает юнит без `io.max`; `board.sh` проверяет это заранее и выходит с кодом 2).
- 2026-10-14 — [board] Проверено `verify` (`experiments/results/board_verify_20261014_f`, ориентир): 132
  строки — 101 совпадает, 2 расходятся (RTT эха wifi-weak 40,2 мс при заданных 30; скорость 4g вниз 9 %
  предела — стек Lima usernet переполняет очередь), 29 справочных; память 64/16 МиБ — OOM, квоты
  200/50/5 % → 200/50/4,98 %. Расхождения не подгонялись под стенд. RTT точен только при занятых всех
  vCPU; скорость CPU при малой квоте менялась с питанием Mac — причина не установлена. На плате (cpubench на
  ядре 4.9) не проверено.
- 2026-10-14 — [hw-mcp] Пропавшее устройство IIO/hwmon ищется заново через `-rescan-period` (10 с), дальше с
  удвоением до 300 с (допущение), период сбрасывается, когда пропавших нет — снятый насовсем датчик иначе
  искался бы каждые 10 с всё время работы; повторная инвентаризация не читает значений; одиночные сбои
  чтения (< 3 подряд) не прерывают окно `stuck`; вход без IRQ переходит на опрос.
- 2026-10-14 — [стенд, gpio-sim] Порядок уборки любого скрипта с gpio-sim: завершить потребителей линий и
  дождаться выхода → проверить по `/sys/kernel/debug/gpio`, что линии не запрошены → только потом `live=0`
  и `rmdir`; `tainted` — до и после — на ядре 6.8 снятие чипа с запрошенными линиями портит ядро (09.10:
  предупреждения `gpiod_free` в `lineevent_release`, затем Oops; по журналу ядра — процессы `gpiomon` (его
  использует проверка sysfsim) и `python3`, не hw-mcp; ВМ перезагружена, ядро 6.8.0-146). Образец —
  `hwmcp/tools/vm_gpiosim_check.sh`; sysfsim чип с занятыми линиями не снимает (код 1).

### Интеграция «минимальной платы» (15.10)

- 2026-10-15 — [edgectx] Предел цепочки дебаунса — 2·`debounce_s` (`debounceChainMax`, допущение): значения,
  сменяющие друг друга дольше, — изменение, подтверждается текущее — живой смоук: при опросе hw-mcp раз в
  секунду и квантовании 0,2 °C дебаунс 2 с перезапускался каждым новым значением, и перегрев с 35 с разбудил
  модель лишь около 64 с (вентилятор в 65,0 с); после правки вентилятор включился в 50,0 с (run2) и 48,8 с
  (run3) (ориентир). Дребезг контакта —
  доли миллисекунды (герконы Standex-Meder ≤ 0,5 мс, `bench/sysfsim/README.md`). Остаток задержки — в
  основном окно коалесценции 10 с (фактор W, не менялся). Тест `TestDebounceChainBounded`.
- 2026-10-15 — [стенд] Смоук минимальной платы (`experiments/results/minboard_smoke_20261014_a`): sysfsim с
  gpio-sim (метка `minboard-<pid>`) — вне юнита платы, в роли железа и ядра; агент и hw-mcp (stdio,
  `-gpio-driver cdev`, чип по метке) — в одном юните `board.sh run` (64 МиБ, CPU 25 %, 1 ядро, сеть 3g);
  hw-mcp — от обычного пользователя, узел `gpiochipN` отдаётся через `chown` — аналог группы `gpio` на
  плате. run3 (итоговые бинарники, ориентир): вопрос — 2 обращения к LLM, 1,41 с; перегрев → вентилятор
  через шлюз (`allow`); монитор — 0 нарушений, `n_unsafe` 0; `memory.peak` 33,5 из 64 МиБ, OOM 0. По
  одному прогону на версию; диск, память < 64 МиБ, CPU < 25 % в связке не проверялись.
- 2026-10-15 — [monitor] `trace_check` принимает расширения трассы edgectx: типы `llm_unavailable`,
  `llm_probe`, `llm_available`, `deferred`, `mcp_down`, `mcp_up`, причину wake `deferred` (входы любого вида),
  поля `maybe_billed`, `error_kind`, `user_reply.deferred`, `wake.coalesced`; запись `deferred` исхода входу
  не даёт, потерянный отложенный вход — `input.no_outcome`; `gate_check` считает `sample_recovered` и
  `sensor_rebound` служебными — трассы с обрывом (`agent_20261009_vmlink2`, `agent_20261014_vmlink`) давали
  по 4 нарушения, теперь 0; добавлены тесты `LinkOutage`. `bench/metrics` и оракул mockllm эпизоды
  `deferred` пока не разбирают (формат ответа на несколько слитых вопросов не решён) — нужно к лестнице с
  обрывами, таких прогонов пока нет.
- 2026-10-15 — [sim-mcp] `board_health` с теми же полями, что у hw-mcp (`sensor_faults: {}`,
  `sensor_errors: 0`) — паритет токенов каталога.
- 2026-10-15 — [стенд] Сквозные тесты нескольких компонентов — в `bench/tests`: тест камеры перенесён из
  `cammcp/tools` в `bench/tests/test_cammcp_edgectx.py` и дополнен (вход → gate → wake → `llm_request`,
  монитор), а не продублирован.
- 2026-10-15 — [hw-mcp] Удалён мёртвый код: кольцо `safeEvents` (его не читали, а на каждом истечении аренды
  оно копировалось), `SafeEvents()`, `HasLocation()`.

### Робот NeuroChip (14–15.10) — в работе

Статус: анализ (`docs/neurobot/ANALYSIS.md`), план (`docs/neurobot/PLAN.md`) и разбор новых материалов
лаборатории (`docs/neurobot/research/R7_*`) готовы; стенд (`neurobot/sim/stand`), генератор жестов
(`neurobot/sim/gestures`) и материалы для лаборатории (`neurobot/lab`) построены; пакет 1 (страж `/cmd_vel`,
патч predict_live v2, развёртывание) ещё не принят. Всё — стенд; итоговые цифры даёт только HIL на роботе.

- 2026-10-14 — [neurobot] LLM не стоит в контуре «жест → движение»: рефлекс замыкается на роботе в графе ROS 2
  (DDS, localhost) без LLM, MCP и rosbridge; агент (PicoClaw лаборатории или EdgeCtx) — сверху, через MCP по
  HTTP, командами уровня задачи; на жест — 0 вызовов LLM, сырые скорости агенту недоступны — LLM в контуре —
  это ≥ 2 вызова и секунды на событие по построению, деньги и риск «уговорённой» модели (RoboPAIR); в демо
  лаборатории рефлекс и так работает без LLM (ANALYSIS §0). Двухуровневая схема известна (Helix, GR00T N1,
  SayCan) — своей не называем.
- 2026-10-14 — [neurobot] У `/cmd_vel` один писатель с явным нулём (при старте, выходе, препятствии, отмене,
  по deadman; вход старше 0,4 с — ноль): страж в пакете 1, исполнитель задач в пакете 2, в безопасности v2
  (15.10) — отдельный `safety_gate` — у лаборатории два писателя (демо шлёт нули 10 Гц, MCP — толчки: рывки,
  ANALYSIS S4), а молчание вместо нуля при отказе небезопасно.
- 2026-10-14 — [neurobot] twist_mux не используется как средство остановки — по коду foxy-devel он только
  пересылает пришедшее и ноль по таймауту или lock не публикует (проверено по исходникам 14.10.2026; поправка
  к R2/R5, где это взято из комментария yaml). На роботе он есть (`/cmd_vel` → twist_mux →
  `diff_drive_controller`, R7) и на стенде воспроизводится как часть их графа; издатели `/cmd_vel_joy` и
  `/cmd_vel_tracker` выше нас по приоритету и перекрывают наш ноль — по плану их наличие перед стартом даёт
  предупреждение или отказ.
- 2026-10-14 — [neurobot] PicoClaw не патчим — в ВКР это аналог; апстрим закрывает исправления надёжности без
  слияния. Его отказы (ANALYSIS PC1–PC9: упавший stdio-сервер не поднимается, `/reload`, нет таймаута
  вызова) лечим конфигом (MCP по streamable HTTP, `request_timeout` 20, `max_llm_retries` 1, лишние
  инструменты выключены) и внешним супервизором. На роботе по журналам PicoClaw 0.2.8, а не 0.3.1 (R7).
- 2026-10-14 — [neurobot] Присланный `ros_mcp_server` — снимок, не эталон: со слов лаборатории версия может
  быть устаревшей (актуальная запрошена, ANALYSIS §5 п. 3), а на роботе по журналам работает другой MCP
  (`robot_vision_demo_mcp`, R7). На его детали не завязываемся: наш robot-mcp работает с темами ROS, имена —
  из конфига, инструменты лаборатории — совместимые псевдонимы; правка их сервера в пакете 1 — не больше
  20 строк — беда архитектурная: LLM внутри цикла движения.
- 2026-10-14 — [neurobot] Исходники лаборатории (`third_party/neurochip/`) не правим: наши изменения — копии с
  патчами в `neurobot/` (perception — копия их `predict_live.py` + патч; `neurobot/deploy/mcp/*.patch`); в рабочей
  копии вычищены секреты (ниже). На этапе анализа веса (архивы PyTorch, то есть pickle) разобраны статически
  (`zipfile` + `pickletools`, без загрузки). Позже их код запускался — только на стенде и в тестах, на синтетике
  и с подставными процессами: класс SNN и предобработка из их `predict_live.py` с весами SNN — только через
  `torch.load(weights_only=True)`, в ВМ `sim` (`neurobot_20261014_gestures_fix`, perception v2 —
  `neurobot_20261014_onboard_final`); их `ros_mcp_server.py` и `robot_vision_mcp_proxy.py` — копиями во
  временном каталоге, с патчем и без (`neurobot/tests/test_deploy_mcp_patch.py`; интеграция в ВМ `sim` —
  `neurobot_20261014_deploy/vm`). `yolo_hand.pt` не загружался (на стенде — подставной `FakeYolo`): только в
  изолированной офлайн-ВМ и с разрешения — загрузка pickle исполняет код стороннего источника. Видео с лицами —
  только с разрешения, хранятся локально.
- 2026-10-14 — [neurobot, секреты] В присланном архиве robotic_platform были секреты (токен VK, ключи API):
  `.security.yml` рабочей копии вычищен, оригиналы — в Корзине; в документах (R7) параметры `key=` URL long
  poll VK заменены на `<REDACTED>`, идентификаторы VK не приводятся; значения нигде не цитируются. Открыто
  (подсчёт 15.10 без вывода значений): журнал `.picoclaw/logs/gateway.log` рабочей копии ещё содержит 52
  невычищенных параметра `key=` long poll VK — вычистить, оригинал — в Корзину. Ключ RouterAI не тратим,
  везде `bench/mockllm`.
- 2026-10-15 — [neurobot, план] По запросу студента 15.10 в план добавлен этап Э2.5 (PLAN §13), прогоны A0/A1
  пакета 1 переносятся с TB3 на него: цифровой двойник робота в Gazebo (геометрия по фото — оценка; лидар
  как URG-04LX, RGB-D как D435, IMU, одометрия; ros2_control + `diff_drive_controller` + twist_mux, как на
  роботе); бюджет Jetson Nano 4 ГБ — всё, что на роботе работает на Jetson, в отдельном cgroup ВМ `sim`
  (память — 4 ГБ минус ОС и драйверы, CPU — квота по соотношению Cortex-A57 и хоста; допущения до замера на
  Nano); безопасность v2 слоями — железо (аварийная кнопка), пределы контроллера, `safety_gate` (тормозной
  путь от края корпуса, «нет данных» лидара в секторе остановки — запрет вперёд, глубина — низкие
  препятствия и обрыв, IMU — наклон и удар, пробуксовка, геозабор, блокировка старта, защёлка estop снимает
  только человек), пределы инструментов агента — «чтобы при попытке ничего физически не пострадало»: лидар
  на ≈0,67 м не видит низких препятствий и, вероятно, слеп спереди-справа из-за корпуса камеры (RS1, RS3),
  а остановка при тишине `/cmd_vel` не подтверждена (вероятно, `cmd_vel_timeout` 0,5 с контроллера).
  Реализация идёт в `neurobot/`, не принята.

## 2026-10-16 … 2026-10-19 — робот NeuroChip: прогоны W3, приёмка, доработки

Всё — на цифровом двойнике в ВМ `sim` (ROS 2 Jazzy, Gazebo Harmonic, Python 3.12) с `bench/mockllm`; код для робота
(Foxy, Python 3.8) проверен под 3.8 только тестами логики и `py_compile`. Числа — ориентир стенда, не результат
робота; на роботе не запускалось ничего. Готовое — ROS 2, Gazebo, gz_ros2_control, twist_mux, rosbridge, PicoClaw,
MCP SDK — своим не считается. Итог — `experiments/results/neurobot_20261018_acceptfix2/compare.md` (локально, в
репозиторий не входит), статус по критериям — `docs/neurobot/PLAN.md` §13.5. Ход: прогоны 16.10, приёмка 17.10
(принято частично), доработки 18.10, повторная приёмка 18.10, доводка 19.10 (независимо не перепринята).

- 2026-10-16 — [neurobot, план] Длительный прогон A1 на двойнике — 1 ч вместо 2 ч (решение студента: время и
  лимит); 8-часовые прогоны для ВКР — отдельно, по протоколу Э4 (PLAN §13.4). Цена: K8 (наклон PSS за 8 ч) на стенде
  не измерен — за 1 ч на итоговом коде наклон суммы компонентов пакета без драйверов двойника 7,1 МиБ/ч, по
  процессам — до 2,8 МиБ/ч (сторож); это не K8 (≤ 1 МиБ/ч на процесс за 8 ч).
- 2026-10-16 — [neurobot, safety] Значения скана — по REP 117: `+inf` и дальше `range_max` — «свободно в пределах
  дальности»; `NaN`, `−inf`, ниже `range_min` (коды ошибок URG) и направления без лучей — «неизвестно». Неизвестный
  луч запрещает вперёд, только если его участок в коридоре движения выходит за корпус дальше `max_gap_m` (3 см) и
  свежая глубина не видит этот участок свободным; иначе — предупреждение `lidar_unknown` — раньше `−inf` считалось
  препятствием вплотную, обрезанный скан запрещал всё, а проёмы давали ложные стопы на старте (в 8 позах мира lab —
  5 → 1; `neurobot_20261016_safety/safety.md` §2). Что urg_node2 на роботе отдаёт на «нет отражения» (NaN или +inf),
  неизвестно: скан 02.06 снят через rosbridge с `null` вместо обоих — просьба `neurobot/lab/LAB_HANDOUT.md` п. 7;
  двойник умеет оба (`--no-return`). Уточнено 18.10 (`nodata_unseen`) и 19.10 (`lidar_fault`) — ниже.
- 2026-10-16 — [neurobot, deploy] Обратная косая черта в значении любого `*_glob` rosbridge — отказ precheck
  (`config_invalid`) — launch_ros разбирает значение как YAML, и в строке в двойных кавычках `\x2c` становится
  запятой: лишний элемент мимо списка разрешённого (проверка W1c, rosbridge 2.7.1: так клиент WebSocket поменял
  `max_speed` safety_gate). Тест `test_backslash_in_any_glob_is_refused`, опыт в ВМ —
  `neurobot_20261016_safety/vm/glob_backslash.txt`.
- 2026-10-18 — [neurobot, deploy] Смерть и зависание узла внутри `ros2 launch`: драйверы и rosbridge запускаются
  через обёртку `neurobot/deploy/launchfiles/supervised.launch.py` — на выход любого процесса, кроме одноразовых
  (`spawner` и т. п.), launch гасит остальные узлы и выходит, systemd поднимает юнит заново по порядку готовности;
  зависание ловит сторож — свежесть тем драйверов и ping WebSocket rosbridge, две неудачные пробы — `restart` юнита
  — `ros2 launch` переживает смерть своего узла, и `Restart=always` не срабатывал (16.10, `main_a1`: после kill -9
  rosbridge агент молчал до конца прогона; с человеком по doctor — 98–126 с). `respawn` не взят: его пришлось бы
  прописывать в каждом launch-файле лаборатории, а перезапуск одного узла не повторяет порядок запуска. API — только
  то, что есть в Foxy (тест против launch 0.10.10, `verify/foxy_launch.sh`). Двойник (`neurobot_20261018_deploy_fix`,
  n = 5 на случай): kill -9 узла лидара, камеры, rosbridge — снова работает через 1,7–2,8 с, SIGSTOP — через
  4,5–4,9 с, 30 из 30 без человека. Не лечится: зависание всех датчиков сразу и SIGSTOP `twist_mux` (README deploy,
  «Ограничения»). На роботе не запускалось.
- 2026-10-18 — [neurobot, MCP] Агент говорит о движении правду: `robot_get_status` отдаёт состояние safety_gate
  (`state`, `reasons`, `estop`, `limits`), `motion_allowed` — только при `ok` или `slow`; `robot_nudge` возвращает
  `ok`, `blocked` с причинами, `estop` или `unconfirmed` — по выходу safety_gate, а не `ok: true` после публикации —
  приёмка 17.10: «готово» при стоящем роботе в 1–3 из 3 реплик «вперёд» и под защёлкой (P5). Ноль стопа идёт через
  одно постоянное соединение с `advertise` на тему: до него ноль доходил до исполнителя за 0,21 с (p95), 9 из 30 позже
  100 мс, после — за 3 мс. Правка — в патче к `ros_mcp_server` лаборатории (патч не публикуется); на Foxy MCP-сервер
  читает состояние из отдельной копии `safety_state` — rosbridge 1.3.1 не умеет открыть тему только на чтение. Двойник:
  0 неправдивых ответов из 20 (`neurobot_20261018_a1v2/k6/agent_truth_n5.json`); на реплику «вперёд» робот поехал
  3 из 3 в каждом 34-минутном и часовом прогоне A1 18–19.10, «готово» без движения — 0.
- 2026-10-18 — [neurobot, safety] Перенастройка safety_gate по приёмке 17.10 (в зале A1 стоял в `blocked` 73–80 %
  времени, касание ножки стула при повороте на месте, ложная защёлка `shock` от своей остановки):
  (1) `nodata_unseen=slow` — участок «нет данных» лидара, который камера глубины не видит (у корпуса слева-спереди),
  — как слепой сектор корпуса камеры справа: вперёд не быстрее 0,15 м/с; строгий режим — `nodata_unseen:=block` —
  главная причина перестраховки была ложной: стены зала дальше 5,6 м давали NaN на участке вне поля камеры. Это
  отступление от буквы PLAN §13.3 — принято студентом 19.10 (запись 2а ниже);
  (2) `depth_err_m` 0,04 м — клетка глубины считается ближе на ошибку у тонкой ножки, и для поворота, и в пути вперёд
  (у правила 17.10 в повторе инцидента запас был 1 мм);
  (3) штатная остановка плавная (S-кривая `stop_decel` 0,5 м/с², рывок 2 м/с³), по причине безопасности — сразу, в
  том числе если опасность появилась в тот же тик, что и нулевой запрос — резкая остановка своего манёвра однажды
  (прогон `long` 16.10, 2829 с) дала на мачтовом `/camera/imu` 6,1 м/с² при пороге 6 и ложную защёлку `shock`;
  калибровка 18.10 этого не воспроизвела: резкий стоп — 3,1–3,2 м/с², плавный — 2,2 м/с², ложных защёлок 0 в обоих
  (`neurobot_20261018_safety_tune/safety.md` §3.3);
  (4) чужой издатель `/scan` во время работы — стоп; второй с именем драйвера — предупреждение и стоп через 30 с
  (след убитого драйвера в графе DDS);
  (5) защёлку снимает только файл root в `/run/neurobot-estop` (`sudo neurobot estop release`); команде `release` в
  теме — отказ — поле `by` самозаявлено: при коде 16.10 защёлку снимал любой узел ROS с `by: human` (P5). Граница L2 —
  root на роботе; подписей DDS (SROS2) нет.
  Проверено (`neurobot_20261018_safety_tune/safety.md`): первые 900 с расписания в зале — `blocked` 65 % → 0 %,
  пробег 4,3 → 14,0 м (A0 — 13,9 м), верных реакций 10 → 25 из 37, касаний 0, но к предметам в этом окне робот не
  подъезжал (зазор ≥ 0,59 м) — остановку проверяют наборы. Цена (3): K6 «stop через MCP → ноль ≤ 100 мс» по букве не
  выполнялся — ноль через ≈0,5 с; решение студента 19.10 — запись 1а ниже.
- 2026-10-18 — [neurobot, прогоны] Сводка A0/A1 (`neurobot/sim/runs/compare.py`) после приёмки 17.10: «0 касаний» —
  по всем прогонам столбца, а не по выбранному; рядом с безопасностью — пробег, время в движении и доли состояний
  safety_gate; верная реакция на жест с разбором (распознавание или защита); K1 без ранних жестов, ранние — отдельной
  строкой; реплика «вперёд» засчитывается, только если робот поехал или агент сообщил о запрете — заголовок 16.10
  «0 касаний — выполнено» скрыл касание стула в `main_a1`, а 0 касаний во многом объяснялись тем, что A1 почти не
  ехал; пробег A0 пересчитан без переносов робота оператором (42,4 м, а не 50,9).
- 2026-10-18 — [neurobot, прогоны] T для K9 — ⌈1,2 × наибольшее время до «ОК» doctor во всех циклах до итогового
  замера⌉ = 26 с, зафиксировано до итоговых циклов (`neurobot_20261018_acceptfix2/k9_T.json`, 23:34) — 20 с от 16.10
  было взято по максимуму 19,9 с без запаса, и цикл 18.10 (21,1 с) его превысил. Итог: максимум 20,55 с в 101 цикле.
  Холодный старт — рестарт `neurobot.target`, не перезагрузка ОС; на роботе T — по HIL.
- 2026-10-19 — [neurobot, safety, executor] По повторной приёмке 18.10:
  (1) `lidar_fault` — скан, где попаданий дальше корпуса меньше 10, пустой; три пустых подряд — полный стоп своей
  причиной; при двух издателях с именем драйвера и пустых сканах — стоп без окна 30 с — правило `nodata_unseen=slow`
  пропускало скан целиком из NaN как `slow` (S3b: 1,49 м за 10 с; после правки — 0,085 м);
  (2) выход из тупика — если поворот на месте заденет, а вперёд нельзя или только к препятствию, исполнитель через
  3 с без заявок жеста и агента сам отъезжает назад на 0,05 м/с по своему следу из `/odom`, не дальше 0,1 м — в A1
  18.10 робот 221 с стоял у ножки стула, 9 жестов без реакции. Сзади датчиков нет, поэтому назад — только туда, где
  корпус только что был; поправку поворота не ослабляли: без неё — касание ножки.
  На итоговом коде за 2,7 ч прогонов — 5 тупиков, все кончились отъездом за 3,5–4,6 с, касаний 0
  (`neurobot_20261018_acceptfix2/notes.md` §1–2); правило отъезда дважды уточнялось по прогонам.
- 2026-10-19 — [neurobot, safety, MCP] **Решение студента 1а:** `robot_stop` агента через MCP — стоп по безопасности,
  мгновенный: ноль в `/cmd_vel` ≤ 100 мс (p95), без S-кривой; аварийная защёлка не ставится — разовый стоп текущего
  движения, после него робот принимает новые команды как обычно. Критерий K6 (PLAN §1) не меняется — 1а его
  выполняет; вариант (б) «торможение ≤ 100 мс, ноль ≤ 0,6 с» отвергнут. Сделано: патч `ros_mcp_server` (вне git)
  вместе с нулями в `/cmd_vel_agent` шлёт `/neurobot/stop_now` (`{id, by, reason, t}`, один `id` на вызов), тема — в
  `topics_glob` rosbridge и в precheck; safety_gate по ней сразу ставит ноль и снимает взвод — движение снова только с
  нулевого запроса, так что прежний манёвр не продолжается и без нуля агента; исполнитель не менялся; автостоп в
  конце толчка и конец манёвра жеста — штатные, плавные. Отдельная тема, а не флаг: у Twist нет поля под флаг, своё
  сообщение — сборка пакета на Foxy, стоп одним переходом не зависит от исполнителя; тема умеет только остановить.
  Двойник (`experiments/results/neurobot_20261019_decisions/`, мир hall, как серия 18.10): ноль в `/cmd_vel` через
  1,9 / 2,7 мс (p50 / p95, n = 30; было 514 / 545 мс), 30 из 30 ≤ 100 мс, поехал после стопа 0 из 30; скан —
  0,3 / 0,7 мс, не хуже 18.10. Цена — удар по мачте на каждом стопе агента: `/camera/imu` 3,17 / 3,72 м/с² (p50 /
  p95, n = 10; наибольший 4,14) при пороге `shock` 6, ложных защёлок 0 из 10; однажды (16.10) резкий стоп дал 6,1 м/с²
  и ложную защёлку — на роботе порог по записи `/camera/imu`. Ориентир стенда, не результат робота.
- 2026-10-19 — [neurobot, safety] **Решение студента 2а:** `nodata_unseen=slow` — осознанное отступление от буквы
  PLAN §13.3 («NaN и „нет данных“ лидара в секторе остановки — запрет вперёд»), записано в §13.3: «нет данных» вне
  поля камеры глубины (у корпуса слева-спереди, дальше ≈25° от курса) — не быстрее 0,15 м/с, как слепой сектор
  корпуса камеры; полный отказ лидара — стоп `lidar_fault`. Цена записана: предмет, не отражающий лазер (стекло,
  чёрный бархат), слева-спереди вне поля камеры — робот едет к нему 0,15 м/с. Строгое правило стоило в зале A1 16.10
  `blocked` 73–80 % времени и 19 верных реакций из 80 (с правилом 18.10 — 2–13 % и 37–44 из 80); оно остаётся
  параметром `nodata_unseen:=block`. Код не менялся (правило — с 18.10).

## Открытые предложения (не решения)

Предложения, ждущие согласия, без даты решения; решением каждое станет отдельной датированной записью выше —
после согласия студента и руководителя.

- [объём] Сценарий «мобильный робот» в ВКР — робот NeuroChip на стенде neurobot и HIL у лаборатории; он заменяет
  «день робота TB3 + Nav2» (`research/new_topic/robot_scenario.md` §12–13), а не добавляется; профиль Nav2 в
  robot-mcp становится второстепенным. Меняет одобренный 06.10 сценарий робота; эксперименты ВКР по роботу
  (Э4) — только после согласования. Источник — `docs/neurobot/PLAN.md` §9, §12.

- 2026-10-20 — [ui, Edge Atlas] По прямому запросу студента интерфейс расширен с пульта NeuroChip до самостоятельной среды наблюдения оборудования. Ветка `codex/neurochip-interface` создана от опубликованного снимка `codex/snapshot-2026-10-20` (`d23bd76`). Новый экран определяет устройства и вложенные поля из потока и MCP-каталога; есть карта наблюдаемых сигналов, осциллограммы, поиск, raw JSON, экспорт и четыре закрепления. Добавлен `python -m neurobot.ui catalog` без ROS/записи и локальный демонстратор sysfsim → hw-mcp → UI. Внешние адреса — через явный localhost/SSH-туннель, без сетевого сканирования. Чтение ресурсов и известный `list_devices`; автоматических действий с приводами нет. Две роботные команды и их подтверждения сохранены. Состояния источника и измерений разделены, ошибка не освежает прошлые показания, пропуски дают разрыв; память и объёмы каталога ограничены явными ошибками. Обоснование дизайна и команды — `docs/ui/ATLAS.md`. Это инструмент демонстрации, а не новая научная гипотеза; показания симулятора не являются результатами на плате.
