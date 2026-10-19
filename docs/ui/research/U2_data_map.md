# U2. Карта данных и действий для интерфейса оператора

19.10.2026. Часть «data» исследования интерфейса: что система уже отдаёт, в каком виде и как часто, что из этого
показывать, кто вправе действовать — и как сделать каждое действие оператора, не нарушив инварианты безопасности.
Метки: [код] — прочитано в коде или README проекта (путь указан), [оценка] — расчёт, [допущение] — до проверки на
роботе, [ист.] — внешний источник (§8).

## 0. Коротко

1. Почти всё нужное интерфейсу уже есть в графе ROS как JSON в `std_msgs/String`: `safety_state` (5 Гц),
   `motion_state` (2 Гц), `perception_health` (1 Гц), `gesture`, `events`. Это готовый «приборный щиток» —
   сырые датчики браузеру почти не нужны [код].
2. Читать граф должен **свой узел rclpy на роботе** (бэкенд UI), а не rosbridge агента. Там на Foxy (1.3.1)
   любая тема из `topics_glob` открыта и на запись: если расширить список под UI, клиент сможет подменить `/scan`
   или `/odom` [код: `neurobot/deploy/README.md`, «Агентский путь»].
3. Чего нет: живого замера задержки «жест → движение» на роботе, истории метрик, HTTP-обёртки doctor, превью
   камеры, входа телеуправления в исполнителе, `/neurobot/task` и выключателя рефлекса (это исполнитель v2,
   пакет 2), API метрик LLM у PicoClaw.
4. Действия: **стоп** — через `/neurobot/stop_now`; **поставить защёлку** — через `/neurobot/estop engage`. Обе
   темы умеют только останавливать. **Снимать защёлку из веба нельзя** — только `sudo neurobot estop release` в
   терминале у робота; интерфейс показывает причины и команду. **Телеуправление** — через новый вход исполнителя,
   а не `/cmd_vel_joy`. **Рестарт** — через sudoers со списком точных команд.
5. У агента прав не прибавляется: бэкенд UI — отдельный пользователь и отдельный процесс; агенту ни интерфейс, ни
   его порт не открываются. `exec`, `web` и `web_fetch` у PicoClaw выключены [код:
   `neurobot/deploy/picoclaw/config.overlay.json`].

## 1. Контекст: что работает сейчас

- На роботе агент — **PicoClaw** (0.2.8 у лаборатории, 0.3.1 на стенде) с **ros_mcp_server + наш патч**
  (вариант А): инструменты `robot_get_status`, `robot_stop`, `robot_nudge` [код: `neurobot/deploy/mcp/`].
- EdgeCtx к роботу пока не подключён (этап Э3). Его HTTP-канал живёт в сценарии «датчики» [код:
  `docs/OVERVIEW.md` §4–5].
- Исполнитель — **v1 (страж)**. Входы — `/cmd_vel_gesture` и `/cmd_vel_agent`, выход — `/neurobot/cmd_vel_req`.
  `/neurobot/task`, рефлекса как переключателя и `task/reflex/last_gesture` в `motion_state` нет: это v2 [код:
  `neurobot/executor/README.md`, «Расхождения с PLAN §2.3»].
- `/cmd_vel` пишет только safety_gate. Дальше `twist_mux` (`cmd_vel` 10, `cmd_vel_tracker` 20, `cmd_vel_joy` 100),
  потом база [код: `neurobot/safety/README.md`].

## 2. Карта данных

Частоты: «робот» — из контракта и кода; «двойник» — из конфигурации Gazebo
(`neurobot/sim/twin/twin_config.py`: лидар 10 Гц, цвет и глубина 15, IMU 200; `/odom` 50 Гц —
`twin/config/controllers.yaml`). Частоты на самом роботе не снимались [допущение].

### 2.1. Состояние пакета neurobot (главный источник для UI)

| Источник | Тип и формат | Частота, размер | Что показать | Кто вправе |
|---|---|---|---|---|
| `/neurobot/safety_state` | String JSON `{boot, seq, state: ok\|slow\|blocked\|estop, reasons[], warnings[], min_clearance_m, front_m, limits{v_max,v_rev,w_max}, req[v,w], out[v,w], cut, tilt_deg, lidar_mask_deg, estop{engaged,since,reasons}, counters{invalid,cut,estop_engaged,estop_released,release_denied,stop_now,ticks,published}}` [код: `neurobot/safety/node.py:_state`] | 5 Гц и при смене состояния, ≈0,7 КБ [оценка] | Большая плашка состояния (на роботе норма — `slow` с `lidar_blind`, `ok` не бывает). Причины кодами с расшифровкой, зазор, путь вперёд, пределы, наклон, «запрос → выход». Защёлка: с какого момента и почему | читать — все; пишет только safety_gate |
| `/neurobot/motion_state` | String JSON `{boot, seq, moving, by: agent\|gesture\|escape\|none, blocked_front, front: clear\|obstacle\|blind\|stale, cut, linear, angular, scan_age_s, front_min_m, front_gap_deg, safety, counters{cut_forward,stale,preempted,safety_cancel,escape,invalid,ticks,published}}` [код: `neurobot/executor/README.md`] | 2 Гц и при изменении, ≈1 КБ (внутри копия safety) [оценка] | Кто ведёт робот (жест, агент, отъезд), едет ли, срезан ли ход вперёд, счётчики отмен | читать — все |
| `/neurobot/perception_health` | String JSON `{fps, frame_age_s, recoveries, state: starting\|recovering\|paused\|ok, dark}` [код: `predict_live.py:publish_health`] | 1 Гц, ≈0,1 КБ | Кадры/с, возраст кадра, восстановления камеры, «пауза в движении», тёмный кадр | читать — все |
| `/neurobot/gesture` | String JSON `{boot, seq, cls, name, conf, spikes, margin, side, n_boxes, t_end, t_rec}`; `t_end`, `t_rec` — стенные часы робота [код: `predict_live.py`] | на жест, ≈0,25 КБ | Лента жестов: класс, манёвр или «не распознано», уверенность, отрыв. Основа для замера задержки (§4, п. 3) | читать — все |
| `/neurobot/events` | String JSON `{type: task_end, task: gesture\|agent\|escape, status: estop\|blocked\|safety_lost\|done\|preempted, reasons, boot, seq}` и `{type: escape, status: start, dist_m, reasons}` [код: `executor/README.md`] | на событие | Журнал событий: отмены, отъезд из тупика | читать — все |
| `/neurobot/estop` | String JSON `{cmd: engage\|release, by, reason, id, t}`; release по теме safety_gate не принимает [код] | на команду | Кто и почему ставил защёлку (строка журнала) | engage — кто угодно; release — только root-файлом (§5) |
| `/neurobot/stop_now` | String JSON `{id, by, reason, t}`; копии с одним `id` — один стоп [код] | на стоп | Стопы агента и оператора | писать — агент (через rosbridge) и бэкенд UI; умеет только остановить |
| `/neurobot/agent/safety_state` | копия `safety_state` для MCP-сервера (Foxy) [код] | как оригинал | не показывать — дубль | читает только MCP-сервер |
| `/neurobot/task` | по PLAN §2.3: `{seq, op: drive\|turn\|maneuver\|stop\|reflex, args, by: llm\|rule\|user, preempt}` | — | — | **в коде не найдено**: исполнитель v2, пакет 2 |

Счётчики в `safety_state` и `motion_state` живут в пределах одного процесса: после рестарта меняется `boot`, и
отсчёт начинается с нуля. История в UI должна склеивать ряды по `boot` [код].

### 2.2. Цепочка скоростей (инженерная схема «кто что просил и что дошло»)

| Источник | Тип | Частота | Что показать | Кто пишет |
|---|---|---|---|---|
| `/cmd_vel_gesture` | Twist | 10 Гц, в покое нули [код] | вход «жест» | perception (legacy, ремап) |
| `/cmd_vel_agent` | Twist | по вызову `robot_nudge` и `robot_stop` | вход «агент» | MCP-сервер через rosbridge |
| `/neurobot/cmd_vel_req` | Twist | 20 Гц в движении, 10 Гц нули | запрос исполнителя | только исполнитель (проверяют doctor и `wait-graph`) |
| `/cmd_vel` | Twist (`NEUROBOT_CMD_VEL_TYPE`) | 20 / 10 Гц | выход safety_gate | **только safety_gate** |
| `/diff_cont/cmd_vel_unstamped` | Twist | как вход twist_mux | дошло ли до базы. Расхождение с `/cmd_vel` — перехват (`override_active`) | только twist_mux |
| `/cmd_vel_joy`, `/cmd_vel_tracker` | Twist | — | предупреждение: есть издатель главнее safety_gate | геймпад, трекер лаборатории |

Для схемы на экране хватит `req`/`out` из `safety_state` и `linear`/`angular` из `motion_state`. Подписка на
сами Twist нужна только для замера задержки и для индикатора «база получила».

### 2.3. Датчики

| Источник | Тип | Частота и объём | Что показать | Как брать |
|---|---|---|---|---|
| `/scan` | LaserScan, URG-04LX: 683 луча, ±2,09 рад, 0,02–4 м (у двойника) | 10 Гц, ≈2,7 КБ дальностей на скан ≈ 30 КБ/с [оценка] | Вид сверху: точки лидара, слепой сектор (`lidar_mask_deg`), коридор корпуса | бэкенд прореживает до 1° и 2–5 Гц, ≈0,5 КБ на кадр [оценка]; точки — в `base_link` по `/tf_static` (тот же разбор TF без tf2, что `neurobot/safety/geometry.py`) |
| `/odom` | Odometry | двойник 50 Гц, ≈0,7 КБ (ковариации) [оценка]; робот — не мерено | Скорость, след, расстояние от точки старта (геозабор 3 м) | бэкенд: 5 Гц `{x, y, yaw, v, w}` |
| `/imu/data`, `/camera/imu` | Imu | двойник 200 Гц | Наклон уже есть в `safety_state.tilt_deg`, удар — в причинах защёлки | **не подписываться**: на 200 Гц это зря потраченный CPU Python |
| `/camera/color/image_raw` | Image rgb8 640×480 | 15 к/с ≈ 14 МБ/с [оценка, `neurobot.env.example`] | Превью | **не брать сырой поток в Python.** Варианты — §4, п. 6 |
| `/camera/color/image_raw/compressed` | CompressedImage | если стоят плагины image_transport | Превью | `compressed_image_transport` для Foxy есть (2.3.3, BSD) [ист. 3]; публикует ли его realsense2_camera на роботе — [допущение, проверить `ros2 topic list`]. Кодирует JPEG CPU узла камеры |
| `/camera/depth/image_rect_raw` | Image 16UC1 | 15 к/с (у двойника 424×240) ≈ 3 МБ/с [оценка] | — | не транслировать. Итог глубины уже в `safety_state` (`obstacle`, `cliff`, `front_m`) |
| `/tf`, `/tf_static` | TFMessage | `/tf` — с частотой одометрии; static — один раз | Позы датчиков на схеме робота | static — один раз при старте бэкенда |
| `/joint_states` | JointState | 10 Гц и выше (порог doctor) | не нужно | — |
| только двойник: `/ground_truth/pose`, `/twin/truth` | — | — | Режим «стенд»: истинная поза и касания поверх одометрии | только ВМ sim |

### 2.4. Эксплуатация (systemd, doctor, журналы)

| Источник | Тип и формат | Частота, цена | Что показать | Кто вправе |
|---|---|---|---|---|
| `neurobot doctor --json` | `{ok, exit: 0\|1\|3, root: Finding\|null, findings: [{component, unit, level: ok\|warn\|fail\|starting, code, text, hint}]}`; код 2 — проверка невозможна [код: `doctor.py:Report.as_dict`] | по запросу. Один прогон — rclpy + наблюдение графа 2,5 с, подписка на кадры камеры; на загруженной ВМ запуск rclpy занимал до 14 с [код, замер ВМ]; на Nano — не мерено | Таблица компонентов в порядке запуска и корневая причина с подсказкой «что делать» | запуск — инженер; нужен доступ к журналу (группа `systemd-journal`/`adm`), **без root** |
| `systemctl show neurobot-*` | `ActiveState`, `SubState`, `Result`, `NRestarts`, `ExecMainStartTimestamp`, `InvocationID` | дёшево, раз в 2–5 с | Светофор юнитов, число рестартов, аптайм | читать — без root |
| journald юнитов | `journalctl -u 'neurobot-*' -o json`; причины — строки `neurobot-<инструмент>: <компонент>: <код>: <текст>` с приоритетом syslog [код: `deploy/README.md`, «CLI»] | поток (`-f`) | Хвост журнала компонента, фильтр по уровню; лента «сторож перезапустил X» (`neurobot-watchdog`) | группа `systemd-journal`. **Маскировать ключи** тем же выражением, что `picolog._SECRET`; в журнале PicoClaw — реплики людей |
| `neurobot start-test` | `cycles.jsonl`, `summary.json`, `manifest.json` [код] | разовый прогон, root, ломает компоненты по очереди | Только просмотр итогов прошлых прогонов | запуск из UI — **нет** (root, роботу нужна пауза) |
| юниты | `neurobot.target`; `neurobot-driver@{lidar,base,camera,imu}`, `-safety`, `-executor`, `-perception`, `-rosbridge`, `-labworker@*`, `-mcp`, `-picoclaw`, `-watchdog` [код: `deploy/units/`] | — | Список компонентов в порядке готовности | рестарт — §5 |

### 2.5. Агентский контур

| Источник | Тип и формат | Частота | Что показать | Кто вправе |
|---|---|---|---|---|
| MCP ros_mcp_server (патч) `127.0.0.1:18110/mcp`, streamable HTTP | `robot_get_status` → `{connected, rosbridge, motion_allowed, safety{state,reasons,estop,limits,req,out}, topics…}`; `robot_stop(reason)`; `robot_nudge(direction, duration_ms ≤ 500, speed ≤ 0,08 / 0,4)` [код: `mcp/ros_mcp.json.example`] | по вызову агента | Каталог инструментов и их пределы; лента вызовов агента | **бэкенду UI его не опрашивать**: `robot_get_status` каждый раз зовёт `/rosapi/topics`, а вызовы сервисов rosbridge 1.3.1 в цикле текут (M11). Живость — из doctor (`tools/list`) |
| Журнал PicoClaw (journald юнита) | строки zerolog: `Tool execution started … tool=mcp_robot_mcp_robot_nudge args={…}`, `completed/failed`, «MCP server initialization complete», «All MCP servers failed» [код: `picolog.py`] | поток | Вызовы инструментов с длительностью, «зомби», висящий вызов | группа журнала; маскировать ключи |
| PicoClaw, канал `pico` | WebSocket `GET /pico/ws?session_id=…`, токен в `Authorization: Bearer`; сообщения `message.send` / `message.create` [код: `neurobot/sim/runs/pico.py`; исходник PicoClaw — ист. 4] | на реплику | Чат с агентом | оператор. **Токен — только в бэкенде** |
| PicoClaw, канал `mqtt` (0.3.1) | `/picoclaw/<agent>/user/request` `{text}` → `…/response` [код: `bench/baselines/drive_picoclaw.py`] | на реплику | Чат (запасной путь) | оператор; в 0.2.8 робота — **в проверенных источниках не найдено** |
| EdgeCtx `POST /v1/user` | `{id?, text}` → `{input_id, wake_id, text, latency_ms, llm_calls, tool_calls, ok}`; 202 `deferred` — модель недоступна, ответ придёт в трассу; 504 — таймаут [код: `agent/internal/channel/http.go`] | на реплику, синхронно | Чат и под каждым ответом — вызовы LLM, задержка | оператор |
| EdgeCtx `GET /v1/health` | `{ok, run_id, clock, now_t, idle, active, queued, inputs, servers[{…, down}], budget, wake, …}` | опрос 2–5 с | Живость агента, MCP-серверы, очередь | читать — все |
| EdgeCtx `GET /v1/metrics` | накопительные счётчики: входы, пробуждения, подавления, LLM (запросы, токены, байты, ошибки по классам), `llm_link{state, failures, deferred}`, `mcp{updates, down, up}`, бюджет, планировщик, гейт, `runtime{heap…}`, `uptime_s` [код: `agent/internal/agent/metrics.go`] | опрос 10–30 с | Главная метрика ВКР: вызовы, токены, ₽ в час. Скорость — разностью снимков | читать — все |
| EdgeCtx `POST /v1/event`, `/v1/clock/advance`, `/v1/shutdown` | — | — | — | **в браузер не пробрасывать**: подделка событий датчиков будит LLM (деньги) и обманывает гейт; `shutdown` без авторизации гасит агента. У HTTP-канала нет ни авторизации, ни CORS [код] |
| Трасса EdgeCtx JSONL | `run_start`, `input`, `wake`, `suppress`, `llm_request`, `llm_response{usage, latency_ms, maybe_billed}`, `tool_call`, `tool_result`, `user_reply`, `notify`, `rule_fired`, `mcp_down/up`, `llm_unavailable/available`, `run_end` [код: `agent/README.md` §«Трасса»] | на событие, сброс ≤ 2 с | Лента «почему агент проснулся или промолчал», токены на ход, ₽ | читать — инженер (в трассе реплики людей) |
| Журнал LLM-прокси | mockllm journal или `bench/baselines/llmtap.py` — байты и источник вызова | на вызов | Токены и ₽ для **PicoClaw**: своего API метрик у него нет | на роботе прокси не стоит (§4, п. 10) |

### 2.6. Ресурсы и метрики прогонов

| Источник | Тип и формат | Частота | Что показать | Замечание |
|---|---|---|---|---|
| sysmon JSONL (`sysmon.v1`) | `header` / `sample{tree{rss,pss,cpu_s,io,…}, procs[], cgroup, system, net, thermal_c{…}}` / `end` [код: `sysmon/README.md`] | 1–30 с | Память (PSS) и CPU по компонентам, наклон памяти, температура | На Nano по умолчанию cgroup v1 — только режим `--pid` [код: `sysmon/README.md:155`]. Юнита sysmon в deploy **нет** |
| GPU, частоты, питание Jetson | `tegrastats` | 1 с по умолчанию | Загрузка GPU (`GR3D_FREQ`), питание (`POM_5V_IN`), троттлинг | в sysmon нет; RUNBOOK §6 советует `tegrastats` при «сначала работало, потом хуже». Источники только форумные (2019–2020) [ист. 5] |
| Итоги прогонов стенда | `metrics.json` (`bench/metrics`), K1–K9 (`neurobot/sim/runs/metrics.py`), `compare.md`, `summary.json` наблюдателя | разово | Страница «прогоны»: K1 p50/p95, касания, TTR, память | `experiments/results/` в репозиторий не входит — данные только локально |
| Журнал шлюза hw-mcp (`*gate*.jsonl`) | решения шлюза приводов | на команду | для сценария «датчики», не для робота | — |

## 3. Как бэкенду брать данные (принципы)

1. **Один долгоживущий узел rclpy** (Python 3.8, Foxy, только stdlib + rclpy). Подписки — только на темы §2.1 и
   2.2, плюс `/scan` и `/odom`. QoS: датчики — `qos_profile_sensor_data`, состояние neurobot — по умолчанию
   (исполнитель читает `safety_state` как best effort — совместимо). Домен и `ROS_LOCALHOST_ONLY` берутся из
   `/etc/neurobot/neurobot.env`, как у CLI.
2. **rosbridge агента не трогать и не расширять** — ни `topics_glob`, ни второй экземпляр на Foxy: в 1.3.1 нет
   раздельных списков на чтение и запись [код: `deploy/README.md`]. Второй rosbridge «для UI» с `/scan` или `/odom`
   в списке дал бы браузеру запись в датчики safety_gate. Подмену `/scan` safety_gate ловит (`scan_foreign`),
   подмену `/odom`, IMU и глубины — нет [код: `safety/README.md`, «Что НЕ защищено»].
3. **Прореживать на роботе, в браузер — агрегаты.** Скан — 1°, 2–5 Гц; одометрия — 5 Гц; состояние — как есть.
   В браузер ≈ 5–15 КБ/с без видео [оценка].
4. **Часы.** Все задержки считает бэкенд по стенным часам робота: `t_end` и `t_rec` жеста — `time.time()` того же
   хоста. Часы браузера в расчёт не идут.
5. **Склейка по `boot`.** Рестарт узла обнуляет `seq` и счётчики. Для «за сутки» бэкенд копит разности.
6. **Не нагружать бюджет 4 ГБ.** Запас на роботе с GNOME — 539 МиБ, headless — 1469 МиБ [код: `PLAN.md` §13.5].
   Бэкенд UI — в той же `jetson.slice`; его PSS и CPU мерить sysmon, как любой компонент.

## 4. Чего не хватает

| # | Пробел | Зачем UI | Как закрыть | Трогает безопасность? |
|---|---|---|---|---|
| 1 | Бэкенд UI (узел rclpy + HTTP/WebSocket) | весь интерфейс | новый юнит `neurobot-ui`, свой пользователь, после `neurobot-safety` по `After=` | нет, если пишет только темы §5 |
| 2 | HTTP-обёртка doctor | кнопка «диагностика» и плашка «ОК / причина» | бэкенд запускает `neurobot doctor --json` подпроцессом: по кнопке и раз в 60 с, не больше одного одновременно. В кэше — время снимка, в ответе — его возраст. Пользователь UI — в группе `systemd-journal`, не root | нет |
| 3 | Живой поток «жест → движение» | ключевая метрика робота на экране | бэкенд на каждый жест с манёвром (`name` ∉ {`unknown`, `none`}), пока робот стоял, ждёт первого ненулевого `/neurobot/cmd_vel_req`, `/cmd_vel`, `/diff_cont/cmd_vel_unstamped` и \|v\| > 0,05 по `/odom`. Задержки от `t_end` — это метки PLAN §3. Логику записи и разбора можно взять из `neurobot/sim/gestures/observer.py` (там она завязана на истину стенда). Окно — 3 с; не дождался — «нет движения» с причиной из `events` | нет (только чтение). Сдвиг: обработка колбэка в Python, ≈1 мс [оценка] |
| 4 | История метрик | графики за час или сутки: вызовы, токены, ₽, PSS, задержки | кольцевой буфер в бэкенде: снимок раз в 10–30 с. Писать на диск пачкой раз в 60 с (ресурс флеш-памяти) в `StateDirectory`. Или поставить sysmon юнитом (`--pid`, 30 с) и читать его JSONL | нет |
| 5 | GPU и питание Jetson | троттлинг, просадки | разбор `tegrastats` (1 Гц) в бэкенде | нет |
| 6 | Превью камеры | видеть, что видит робот | лучший вариант — в патче perception публиковать раз в 0,5 с JPEG 320×240 с рамкой YOLO и следом руки (`/neurobot/preview`, CompressedImage) и только при подписчике. Кадр уже декодирован, cv2 есть. Иначе `compressed` от узла камеры (CPU) | нет; это правка perception, по умолчанию выключено |
| 7 | Вход телеуправления в исполнителе | «держи кнопку — едет» | `/neurobot/cmd_vel_ui` (Twist) с тем же deadman 0,4 с, приоритет `ui > agent > gesture`, `by: user` в `motion_state`. precheck и `wait-graph`: писатель — только узел UI | да: изменение исполнителя; safety_gate не меняется |
| 8 | `/neurobot/task` и рефлекс вкл/выкл | «проехать N м», «рефлекс выключен» | исполнитель v2 (пакет 2, PLAN §2.4). До него — только параметр и тема рефлекса в v1 (§5) | да: изменение исполнителя |
| 9 | Карта опасностей safety_gate | вид сверху: клетки глубины, коридор, маска | сейчас наружу идёт только сводка (`min_clearance_m`, `front_m`, `lidar_mask_deg`). Можно отдельную тему отладки 1 Гц, по умолчанию выключенную | правка safety_gate — только с повторной приёмкой; для UI не обязательно |
| 10 | Метрики LLM для PicoClaw | вызовы, токены, ₽ агента лаборатории | прокси в духе `llmtap` между PicoClaw и LLM, его журнал читает бэкенд. Или счёт по журналу шлюза (там нет `usage` — [допущение]) | нет |
| 11 | Аудит действий оператора | кто нажал стоп, поставил защёлку, перезапустил | JSONL в `StateDirectory` бэкенда и строка в journald | нет |
| 12 | Зависший `twist_mux` doctor не видит | — | индикатор «`/cmd_vel` идёт, а вход базы молчит» из §2.2 (известный пробел, пакет 2) | нет |

## 5. Действия оператора и инварианты

Инварианты: у `/cmd_vel` один писатель — safety_gate; защёлку снимает только человек (root-файл); агент новых прав
не получает; rosbridge открыт агенту минимально.

| Действие | Как сделать | Кто вправе | Подтверждение | Почему инвариант цел | Есть сейчас? |
|---|---|---|---|---|---|
| **Стоп** | бэкенд публикует в `/neurobot/stop_now` `{"id", "by": "ui:<имя>", "reason", "t"}` пять раз с одним `id`, как MCP-сервер, и ноль во вход телеуправления. Итог — `safety_state.out` = 0 | любой вошедший оператор | нет (одна кнопка, всегда видна) | вход safety_gate умеет только остановить: пределы и защёлку не трогает, взвод только снимает. Ноль в `/cmd_vel` по замеру — 1,9 / 2,7 мс p50 / p95 [код, двойник] | да, тема и контракт есть |
| **Поставить защёлку** | `/neurobot/estop` `{"cmd": "engage", "by": "ui:<имя>", "reason", "id", "t"}` трижды с одним `id`, как CLI. Ждать `state` = `estop` (≤ 0,3 с по тесту) | любой оператор | нет | engage по контракту — «кто угодно»; release по теме safety_gate не принимает. Подпись на кнопке: **«не аварийная кнопка»** — при обрыве Wi-Fi она не сработает, а кнопки L0 на роботе нет | да |
| **Снять защёлку** | **из веба — нет.** UI показывает `estop.since` и `reasons` и даёт скопировать команду `sudo neurobot estop release`. Человек у робота вводит её в терминале и подтверждает словом `release`. UI видит `state` ≠ `estop`, затем `unarmed` | человек с sudo у робота | в терминале (TTY, слово `release`) | канал root не меняется: бэкенд не root, `/run/neurobot-estop` (root, 755) ему недоступен, sudoers на `neurobot estop` не даётся. RUNBOOK §7: «снимает только человек, который видит робота» | да (CLI) |
| **Задача «проехать N м»** | после исполнителя v2: `/neurobot/task {op: drive, args: {distance_m ∈ [−0,2; 1], speed ≤ 0,15}, by: user, seq}`. Исполнитель ведёт по `/odom` и пишет запрос в safety_gate | оператор | показать N и предупреждение о заднем ходе | бэкенд не становится регулятором: замкнутый контур по одометрии — только в исполнителе. Пределы — в исполнителе и в safety_gate | **нет** (пакет 2). Временного пути не делать: `robot_nudge` даёт ≤ 4 см за вызов |
| **Рефлекс вкл/выкл** | v2: `/neurobot/task {op: reflex, args: {enabled}}`. До v2 — параметр исполнителя v1 `gesture_enabled` и тема `/neurobot/reflex` (String JSON `{enabled, by, id}`), которой нет в `topics_glob` rosbridge. Выключен — вход `/cmd_vel_gesture` не считается заявкой; состояние — в `motion_state` | оператор | нет | выключение только убирает источник движения. Включённое состояние по умолчанию — из конфига, после рестарта — как в конфиге. `systemctl stop neurobot-perception` вместо этого не годится: doctor покажет ошибку, и освобождается камера и GPU | **нет** |
| **Перезапустить компонент** | sudoers с точными командами для пользователя UI, например `neurobot-ui ALL=(root) NOPASSWD: /bin/systemctl restart --no-block neurobot-perception.service`, и так же для executor, mcp, picoclaw, rosbridge, `labworker@<имя>`, `driver@lidar`, `driver@camera`, `driver@imu`. Перед рестартом — стоп (`stop_now`). `stop`/`disable` не даются | инженер | диалог с именем компонента и последствием (perception прогревается до 2 мин) | не агентское действие. Агент до UI не дотягивается: PicoClaw без `exec`/`web`. Рестарт safety_gate безопасен (ноль в `ExecStopPost`, защёлка в `StateDirectory`, после старта `unarmed`), но давать его UI — решение студента. `driver@base` на двойнике = мир заново и защёлка `shock` — в UI не давать. polkit на Ubuntu 20.04 — 0.105 с `.pkla` без JS-правил, по имени юнита не фильтрует [ист. 1, 2] | **нет** |
| **Чат с агентом** | PicoClaw: бэкенд — клиент канала `pico` (`/pico/ws`, Bearer-токен из конфига, свой `session_id` на оператора). EdgeCtx: прокси только `POST /v1/user` | оператор | счётчик сообщений и «≈ вызовов LLM». При реальной LLM — предел N реплик в минуту | инструменты агента те же, прав не прибавилось. Токен PicoClaw в браузер не уходит, хотя канал умеет `Sec-WebSocket-Protocol: token.<…>` [ист. 4]. `/v1/event`, `/v1/shutdown`, `/v1/clock/advance` не пробрасываются | частично: каналы есть, клиента UI нет |
| **Телеуправление** | кнопки и стрелки «держи — едет». Браузер шлёт по WebSocket «держу» 10 Гц, бэкенд публикует `/neurobot/cmd_vel_ui` 20 Гц, пока «держу» не старше 0,3 с. Отпустил кнопку или закрылось соединение — ноль сразу. Пределы бэкенда не шире L3: 0,15 м/с, 0,8 рад/с, назад 0,05 м/с | оператор, видящий робота | одно активное управление на робот (захват, отпускание) | идёт через исполнитель и safety_gate: тот урезает, блокирует, плавно тормозит. **Не через `/cmd_vel_joy`** — он выше safety_gate в twist_mux, мимо L2 (приёмка 17.10, P4: 0,20 м). Не в `/cmd_vel`, не в `/neurobot/cmd_vel_req` — второй писатель, doctor и защёлка `foreign_writer` | **нет** (нужен п. 7 §4). Временно можно писать в `/cmd_vel_agent` при выключенном агенте, но это делёж входа с агентом — не рекомендую |
| **Диагностика** | doctor через обёртку (§4, п. 2) | инженер | нет | только чтение | частично |

### Роли

- **Наблюдатель** — всё на чтение: состояние, датчики в агрегатах, метрики, история.
- **Оператор** — стоп, поставить защёлку, телеуправление, задачи и рефлекс (после v2), чат.
- **Инженер** — рестарт компонентов, doctor, полный журнал, трассы.
- **Человек у робота (sudo)** — снятие защёлки, только в терминале.
- **Агент (LLM)** — к UI доступа нет; его права не меняются (MCP, `topics_glob`).

Где слушать: по умолчанию `127.0.0.1`, доступ — через SSH-туннель. Порт в сети лаборатории — только с
авторизацией на действия. Порт — вне занятых диапазонов (EdgeCtx 18080–18089, MCP робота 18110–18119, PicoClaw
18790) [код].

## 6. Бюджет трафика и ресурсов [оценка]

| Поток в браузер | Объём |
|---|---|
| `safety_state` 5 Гц + `motion_state` 2 Гц + health 1 Гц | ≈ 6 КБ/с |
| Скан 1°, 5 Гц + одометрия 5 Гц | ≈ 3 КБ/с |
| Превью JPEG 320×240, 2 к/с | ≈ 20–40 КБ/с, только при открытой вкладке |
| Метрики и `systemctl show` раз в 5–30 с | < 1 КБ/с |

Самое дорогое на Jetson — не браузер, а разбор сообщений в Python. Поэтому: без IMU 200 Гц, без сырых кадров и
глубины. Подписка на `/odom` 50 Гц стоит дешевле, чем кажется (≈0,7 КБ) [оценка]. Память бэкенда — мерить; на
Nano не мерено.

## 7. Проверить на роботе

- частоты `/odom`, `/imu/data`, `/camera/imu`, есть ли `…/compressed` (`ros2 topic hz`, `ros2 topic list`);
- время одного `neurobot doctor --json` на Nano;
- PSS и CPU бэкенда UI в `jetson.slice`;
- есть ли канал `pico` в конфиге PicoClaw 0.2.8 робота (по `neurobot/sim/runs/pico.py` — включён у лаборатории) и
  MQTT в 0.2.8;
- `tegrastats` на образе робота (JetPack 4.x).

## 8. Внешние источники

1. polkit в Ubuntu 20.04: пакет `policykit-1` 0.105-26ubuntu1 (релиз), 0.105-26ubuntu1.3 (обновления, 21.02.2022)
   — https://launchpad.net/ubuntu/focal/+source/policykit-1. Лицензия в проверенных источниках не указана.
2. Формат правил polkit на focal — `.pkla` (INI-файлы локального авторитета); JS-правила `.rules` на странице не
   упоминаются — https://manpages.ubuntu.com/manpages/focal/man8/pklocalauthority.8.html. Вывод «по имени юнита
   не фильтрует» — [оценка по документации]: `.pkla` сопоставляет только id действия.
3. `compressed_image_transport`: для Foxy — 2.3.3, Humble — 2.5.5, Jazzy — 4.0.7; лицензия BSD —
   https://index.ros.org/p/compressed_image_transport/.
4. PicoClaw, канал `pico` (`pkg/channels/pico/pico.go`): токен обязателен; принимается из `Authorization: Bearer`,
   подпротокола `token.<значение>` или параметра `token` (если разрешён). Источник — локальная копия исходника
   `bench/baselines/vendor/src/picoclaw`, коммит 2cf030d2 от 30.06.2026 (v0.3.1 по
   `bench/baselines/drive_picoclaw.py`), лицензия MIT; https://github.com/sipeed/picoclaw. Для 0.2.8 исходник не
   проверялся.
5. tegrastats: `--interval` в мс, по умолчанию 1 с; `POM_5V_IN` — общее входное питание платы; `GR3D_FREQ` —
   загрузка GPU — форумы NVIDIA 2019–2020
   (https://forums.developer.nvidia.com/t/power-consumption-monitoring/73608,
   https://forums.developer.nvidia.com/t/how-can-i-monitor-gpu-status-in-nano/72597). Свежих (2025–2026)
   источников по Nano в проверенных источниках не найдено.
