# U3. Архитектура и безопасность интерфейса оператора

19.10.2026. Часть «arch» исследования UI. Соседние заметки: `U2_data_map.md` (карта данных и действий).
Метки: [код] — из кода и README проекта; [замер] — стенд или двойник, ориентир; [ист.] — внешний источник (§9);
[оценка]; [допущение]. На Jetson ничего из этого не проверялось.

## 0. Коротко

- **Реализуемо и адекватно.** Рекомендация — **(C) гибрид**:
  - **ядро — своё лёгкое веб-приложение.** Бэкенд — один узел rclpy на Jetson: Python 3.8, только stdlib и rclpy.
    Связь с браузером — HTTP, SSE и MJPEG. Слушает только `127.0.0.1`, доступ — через SSH-туннель с токеном;
  - **Lichtblick** — для глубокой отладки, сначала только офлайн, по записям прогонов. На Jetson он не нагружает ничего.
- **(A) «только готовое»** на Foxy не годится как основной интерфейс:
  - foxglove_bridge Foxy не поддерживает [ист. 1, 2];
  - остаётся второй rosbridge 1.3.x. В нём один список тем и на чтение, и на запись, и нет аутентификации [ист. 5; код]:
    что UI видит, то он может и подменить, в том числе входы safety_gate.
- **Инварианты целы по построению:**
  - бэкенд пишет только в `/neurobot/stop_now` и `/neurobot/estop` (только engage), а после исполнителя v2 — ещё в `/neurobot/task`;
  - защёлку из UI снять нельзя — только `sudo neurobot estop release` у робота;
  - глобы rosbridge агента не меняются;
  - агент к UI доступа не имеет: токен в root-файле, проверка Host и Origin.
- **Трудоёмкость:**
  - MVP — чтение, STOP, баннер защёлки, метрики агента и ресурсов: **≈5–6 дней**;
  - полный (C) — с превью камеры, задачами после исполнителя v2, Lichtblick и наборами на двойнике: **≈10–13 дней** (§7).

## 1. Исходные условия проекта

| Что | Как сейчас | Где |
|---|---|---|
| Писатель `/cmd_vel` | только safety_gate. Чужой писатель — защёлка `foreign_writer`. precheck и `wait-graph` ловят писателей при старте | [код] `neurobot/safety/README.md` п. 8; `deploy/README.md` «Цепочка движения» |
| Обход L2 | `/cmd_vel_joy` и `/cmd_vel_tracker` стоят в twist_mux выше safety_gate (приёмка 17.10, P4) | [код] safety README, deploy |
| Защёлка | engage — кто угодно, командой в `/neurobot/estop`. release — только файл root `release.json` в `/run/neurobot-estop` от `sudo neurobot estop release` с TTY. Команду release в теме safety_gate не принимает. Граница: «у кого есть sudo» | [код] deploy «Аварийная защёлка» |
| Стоп агента | `/neurobot/stop_now` `{id, by, reason, t}`: ноль сразу и без защёлки, взвод снят. Ноль за 1,9 / 2,7 мс (p50 / p95). Первый стоп после старта издателя — 52–55 мс: издатель создаётся лениво | [замер] PLAN §13.6; deploy «Ограничения» |
| rosbridge агента | `127.0.0.1:9090`. `topics_glob` = `[/cmd_vel_agent,/neurobot/stop_now]` и копия состояния на Foxy; `services_glob` = `[/rosapi/topics]`; `params_glob` = `[none]`. В 1.3.1 один глоб на чтение и запись | [код] deploy «Агентский путь» |
| Пользователь компонентов | все от `jetson`, включая PicoClaw (без `exec` и `web`) | [код] env `NEUROBOT_USER` |
| Порты робота | MCP 18110–18119, EdgeCtx 127.0.0.1:18080, шлюз PicoClaw 18790 (канал `pico` — WebSocket с токеном) | [код] env, agent README, R3 |
| Память | весь стек в cgroup — 1866,6 МиБ уникальных. Запас с GNOME — 539 МиБ, headless — 1469 МиБ. rosbridge — 111 PSS / 137 МиБ уникальных; узел rclpy (исполнитель, safety_gate) — 57–63 МиБ уникальных | [замер] OVERVIEW §4; `sim/budget/README.md` §6.1 |
| Сеть DDS | у лаборатории RViz и геймпад на ПК по DDS/UDP; изоляция localhost выключена | R7 RS11 |
| Метрики агента | EdgeCtx: `GET /v1/metrics`, `/v1/health`, трасса JSONL. PicoClaw: журнал. Токены PicoClaw — через mockllm (`/control/state`, `/control/journal`; режим `--upstream` — учётный прокси перед RouterAI) | [код] agent README, `bench/mockllm/README.md` |

## 2. Внешние факты (проверено 19.10.2026)

| Компонент | Версия, дата | Лицензия | Что важно для нас |
|---|---|---|---|
| foxglove_bridge | ROS 1-ветка 0.8.5 (2025-05-15). Для ROS 2 — в `foxglove-sdk`: humble, jazzy, kilted, rolling | MIT | **Foxy нет**: «более ранние выпуски ROS не будут поддерживаться» [ист. 1, 2, 4]. Списки раздельные: `topic_whitelist` и `client_topic_whitelist`, а также `service_whitelist`, `param_whitelist`, `capabilities`. По умолчанию — `0.0.0.0:8765`, всё открыто, публикация клиентом включена. Аутентификации нет, только TLS [ист. 1, 3]. Новая версия умеет удалённый доступ через облако Foxglove по токену устройства [ист. 3] |
| rosbridge_suite | последняя — 4.2.1 (2026-08-12). Для Foxy — 1.3.2 (2023-09-27), на роботе 1.3.1 (2022-10-21) | BSD-3 | аутентификация из ROS 2 удалена (#586) [ист. 5]. Раздельные `topics_pub_glob` и `topics_sub_glob` в 1.3.x отсутствуют [код; ист. 5] |
| Межверсионный DDS (Foxy ↔ Humble или Jazzy) | — | — | официально не поддерживается; у одного пользователя с одинаковым RMW работало [ист. 6] |
| Lichtblick | v1.29.1 (2026-09-08) | MPL-2.0 (BMW, форк Foxglove Studio) | источники данных: Foxglove WebSocket, Rosbridge, ROS 2 `.db3`, MCAP, ROS 1. В `.db3` нет определений сообщений — читаются только известные типы; наши String, LaserScan, Odometry, Imu к ним относятся. Есть панели Publish и Teleop. Desktop, web или Docker `ghcr.io/lichtblick-suite/lichtblick` [ист. 7] |
| Foxglove (приложение) | 2.x | проприетарное с 2.0 | открытую редакцию Studio прекратили [ист. 8] — поэтому Lichtblick |
| web_video_server | 3.1.1 | BSD-3 | README: ROS 2 Humble и новее. Для Foxy в проверенных источниках не найдено. Аутентификации нет [ист. 9] |
| roslib (JS) | 2.1.0 (2026-03-03) | BSD-2 | нужен только при работе через rosbridge |
| Фронтенд | preact 10.29.8 (2026-08-01); uPlot 1.6.32 (2025-03-14); htmx 2.0.11 | MIT, MIT, 0BSD | собирается на ноутбуке. На робот уходит только статика |
| Python-зависимости бэкенда | aiohttp 3.14.5 требует Python ≥ 3.10; websockets — последняя для 3.8 версия 13.1 (2024-09-21) | — | на Python 3.8 — **только stdlib** (`ThreadingHTTPServer` есть с 3.7) [ист. 10] |
| rclpy Foxy | ветка foxy | Apache-2.0 | есть `Publisher.get_subscription_count()`, `Node.count_subscribers()` и `get_publishers_info_by_topic()` (проверено по исходнику) [ист. 11] |
| jetson-stats (jtop) | 7.2.2 (2026-10-01) | **AGPL-3.0** | в бэкенд не встраивать; температуру и частоты читать из sysfs |
| MCP Streamable HTTP (2025-11-25) | — | — | сервер обязан проверять `Origin` против DNS rebinding, должен слушать localhost и аутентифицировать клиентов [ист. 12] — те же меры берём для своего бэкенда |
| Защёлка по ISO 13850 (вторичные источники) | — | — | снятие — только ручным действием там, где команда подана, и не с вышестоящего пульта [ист. 13, 14]. Аварийное устройство — с фиксацией и прямым размыканием [ист. 15]. **Кнопка в браузере не аварийная** |
| Jupyter Server | — | BSD | образец «токен при старте → ссылка → cookie» [ист. 16] |
| Playwright (Python) | 1.63.0 (2026-09-15) | Apache-2.0 | e2e на Mac, Python ≥ 3.10 — не на роботе |
| Lima | 2.1.1 (локально) | Apache-2.0 | у ВМ sim есть `~/.lima/sim/ssh.config` — туннель как к роботу |

## 3. Сравнение вариантов

| Критерий | (A) Lichtblick + мост | (B) своё приложение | (C) гибрид (B + Lichtblick) |
|---|---|---|---|
| Где бэкенд | своего нет. Мост на Jetson: foxglove_bridge — нельзя (нет Foxy), значит второй rosbridge 1.3.x. Вариант «мост на ноутбуке» — Docker Foxy и DDS по Wi-Fi: межверсионно не поддерживается, хрупко | на Jetson: нужен DDS localhost. Бэкенд на ноутбуке потянул бы DDS по Wi-Fi и ROS на ноутбуке | как B. Lichtblick — на ноутбуке |
| Транспорт | WebSocket rosbridge v2 (JSON или CBOR) | браузер ↔ бэкенд: HTTP GET/POST, **SSE** (поток состояния) и MJPEG (превью). Бэкенд ↔ ROS: прямой DDS. Бэкенд ↔ EdgeCtx: HTTP 127.0.0.1:18080 | B; Lichtblick — офлайн `.db3` и MCAP. Онлайн — по требованию: rosbridge №2, вне `neurobot.target` |
| Права | ролей нет. Тема в глобе открыта и на запись. Publish и Teleop Lichtblick пишут в любую тему из глоба | роли в бэкенде (§4.3). Издатели узла — жёсткий список из 2–3 тем | B. Онлайн-мост только с «безопасными для подмены» темами |
| Аутентификация в сети лаборатории | в rosbridge нет. Только 127.0.0.1 и SSH-туннель | 127.0.0.1 + SSH-туннель + токен (root-файл) → cookie; проверка Host и Origin | B; онлайн-мост — 127.0.0.1 и SSH |
| Память Jetson | +111–137 МиБ (rosbridge №2) [замер аналога] | ≈60–70 МиБ (один узел rclpy) [оценка по замеру 57–63] | как B; мост №2 — только на время отладки |
| CPU Jetson | Python rosbridge: JSON или CBOR каждой темы на клиента. `/scan` 10 Гц, состояния — терпимо [оценка] | разбор 4–6 малых тем в Python и SSE 2–5 Гц: единицы процентов ядра [оценка, мерить] | как B |
| Видео | только сжатые темы: сырое 640×480×15 ≈ 13,8 МБ/с ≈ 110 Мбит/с — не для Wi-Fi [расчёт] | без видео; или превью JPEG 320×240, 2–5 к/с ≈ 20–100 КБ/с из патча perception, только при зрителе [оценка] | как B |
| Не второй писатель `/cmd_vel` | `/cmd_vel` можно не класть в глоб. Но Teleop в `/cmd_vel_joy` при широком глобе — обход L2 | список издателей в коде, проверка графа (`wait-graph ui`), защёлка `foreign_writer` как страховка | B |
| Снятие защёлки | нет (правильно) | нет. Баннер с причинами и командой. Этап 2 (по желанию) — физическая кнопка у робота (§4.4) | B |
| Что видит агент | rosbridge №2 на 127.0.0.1 агенту формально доступен. Защита — только то, что у PicoClaw выключены exec и web | ничего нового: UI не MCP-сервер и не в конфигах агентов, у API токен | B |
| Foxy, Python 3.8, Go | rosbridge 1.3.x есть в Foxy. Lichtblick — на ноутбуке | rclpy Foxy + stdlib 3.8 — как safety_gate и CLI. Go-компоненты (EdgeCtx, sysmon, robot-mcp) — только через их HTTP и JSONL | B |
| Метрики ВКР (вызовы, токены, задержки) | нет: Lichtblick не читает HTTP EdgeCtx и mockllm | есть: EdgeCtx `/v1/metrics`, трасса, mockllm, sysmon, «жест → команда» по `/neurobot/gesture` и `/cmd_vel` | B; Lichtblick — графики тем по записям |
| Трудоёмкость | 1,5–3 дня | 9–12 дней | 10–13 дней (MVP 5–6) |

**Почему A опасен именно на Foxy.** Чтобы UI видел лидар, одометрию и состояние, их надо внести в `topics_glob`.
Но в 1.3.x это открывает их и на запись:
- подмену `/scan` safety_gate поймает (`scan_foreign` — стоп);
- подмену `/odom`, IMU и глубины — нет (safety README, «Что НЕ защищено»);
- подменённый `/neurobot/safety_state` обманет исполнитель, doctor и `neurobot estop`.

Если эти темы из глоба исключить, UI ослепнет. Требование W2 прямо запрещает safety_gate опираться на открытые
мосту темы [код].

**Почему бэкенд не на Go**, хотя агент и sysmon — Go:
- статический Go без cgo не говорит с DDS. rclgo требует cgo и ROS, а это ломает «статический бинарник»;
- без DDS нужен rosbridge №2, а это +111–137 МиБ и те же глобы;
- пара «Go-HTTP + рядом узел rclpy» — два процесса и свой протокол между ними.

Одного узла rclpy хватает.

## 4. Рекомендуемая архитектура (C)

### 4.1. Схема

```
 НОУТБУК ОПЕРАТОРА                                   JETSON (Foxy, Python 3.8)
 браузер ──HTTP/SSE/MJPEG──► ssh -L 18120:127.0.0.1:18120 ──► [neurobot-ui] узел rclpy + ThreadingHTTPServer
   ▲ статика (preact+uPlot,                                   127.0.0.1:18120, пользователь neurobot-ui
   │ собрана на ноутбуке)                                     ├─ DDS sub (только чтение): /neurobot/safety_state,
 Lichtblick (офлайн .db3/MCAP;                                │   motion_state, perception_health, gesture, events,
 онлайн — только отладка, §4.7)                               │   /cmd_vel (для задержки), /scan (→ 1°, 5 Гц), /odom (5 Гц),
                                                              │   /neurobot/preview/compressed (если есть зритель)
                                                              ├─ DDS pub (список в коде): /neurobot/stop_now,
                                                              │   /neurobot/estop {cmd: engage}; v2: /neurobot/task {by: user}
                                                              ├─ HTTP-клиент: EdgeCtx 127.0.0.1:18080 /v1/metrics, /v1/health;
                                                              │   mockllm /control/state (стенд и учётный прокси)
                                                              ├─ файлы: трасса EdgeCtx (tail), sysmon JSONL, cgroup юнитов
                                                              └─ по кнопке: `neurobot doctor --json` (не по таймеру)
 НЕ ТРОГАЕТ: rosbridge :9090 и его глобы, MCP :18110 (не опрашивает), /cmd_vel*, /cmd_vel_joy, датчики на запись,
             параметры и сервисы узлов, /run/neurobot-estop
```

- Юнит `neurobot-ui`: `After=neurobot-safety`, без `Requires=`, `Restart=always`. Робот без UI работает, поэтому
  doctor считает отказ UI предупреждением, а не ошибкой.
- Для `ExecStartPost` нужен `neurobot wait-graph ui`: узел в графе, его издатели входят в список.
- Издатели создаются **при старте** и живут всё время работы. Иначе первый STOP займёт ≈50 мс, а не 2–3 мс
  [замер аналога в MCP].

### 4.2. API (минимум)

| Метод | Что | Роль |
|---|---|---|
| `GET /` и статика | интерфейс | вошедший |
| `GET /api/stream` (SSE) | состояние 2–5 Гц, события, heartbeat 1 с, `retry`; одно соединение на вкладку | наблюдатель |
| `GET /api/preview.mjpg` | превью, только пока открыто; бэкенд подписан на превью только при зрителе | наблюдатель |
| `POST /api/stop` | `stop_now` ×5 с одним `id`, `by: "ui:<роль>"` | **все роли** |
| `POST /api/estop` | `{cmd: engage}` ×3 с одним `id`; подтверждения нет | **все роли** |
| `POST /api/task` (после исполнителя v2) | `/neurobot/task {op, args, by: "user", seq}`; пределы — из env L3 (`NEUROBOT_AGENT_MAX_*`), исполнитель проверяет ещё раз | оператор |
| `POST /api/doctor` | один запуск `neurobot doctor --json`, не чаще раза в 10 с | инженер |
| `POST /api/chat` (по желанию) | EdgeCtx `POST /v1/user` или канал `pico` PicoClaw. Счётчик вызовов LLM и токенов в ответе | оператор |
| release защёлки, параметры, сервисы, `/cmd_vel*` | **нет такого кода**; маршрут → 404 | — |

STOP работает и без SSE: это отдельный POST. При потере heartbeat дольше 2 с UI закрывает экран плашкой
«нет связи с роботом»: показанное больше не правда.

### 4.3. Модель прав

| Кто | Видит | Может | Не может |
|---|---|---|---|
| Наблюдатель (`neurobot ui-url --view`; токен читает группа `neurobot-ui`) | всё на чтение | STOP, поставить защёлку — направление «безопасно» | задачи, чат, doctor |
| Оператор (`sudo neurobot ui-url`) | то же | + задачи (v2), рефлекс (v2), чат | снять защёлку; любые параметры |
| Инженер (то же, что оператор + флаг) | + doctor, журнал (с вычисткой ключей, как в doctor) | + рестарт по списку sudoers — **решение студента** (U2 §5) | рестарт `driver@base`, `stop`/`disable` юнитов |
| Человек у робота (sudo, TTY) | — | **снять защёлку**: `sudo neurobot estop release` | — |
| Агент (LLM) | ничего нового: результат стопа виден в `safety_state` через свой MCP | ничего нового | достучаться до API: токен; Host/Origin; UI не в конфиге MCP |

**Аутентификация.**
1. Бэкенд слушает только `127.0.0.1`. Из сети лаборатории порт не виден. Вход — SSH-ключи лаборатории:
   `ssh -L 18120:127.0.0.1:18120 jetson@robot`. Шифрование даёт SSH, сертификаты не нужны.
2. Токен (128 бит) пишется при старте в `/run/neurobot-ui/` с правами 0600 для оператора и 0640 для группы
   наблюдателя. `sudo neurobot ui-url` печатает ссылку `http://127.0.0.1:18120/?t=…` — образец Jupyter [ист. 16].
   Дальше токен живёт в cookie `HttpOnly; SameSite=Strict`.
3. Каждый запрос проверяется так:
   - `Host` ∈ {`127.0.0.1:18120`, `localhost:18120`}, `Origin` — тот же или пустой у GET, иначе 403. Это защита от DNS
     rebinding и CSRF, как требует спецификация MCP [ист. 12];
   - POST — только с cookie.
   - Токен в журнал не пишется.
4. Почему localhost недостаточно. Агент живёт на том же хосте и от того же пользователя `jetson`. Отделяет его
   токен: читать его может только root или группа, которой у `jetson` нет. Это та же граница, что у `release.json`.
5. Отвергнуто для этапа 1:
   - порт в сети лаборатории с TLS и паролем через reverse proxy: новый открытый порт, сертификаты, +20–40 МиБ [оценка];
   - удалённый доступ через облако Foxglove: внешний сервис.

### 4.4. Защёлка через интерфейс

- **Этап 1 (рекомендация).** Из UI защёлку снять нельзя. Красный баннер показывает:
  - `estop.since` и `reasons`;
  - что устранить;
  - команду `sudo neurobot estop release` с кнопкой «копировать».

  После снятия UI показывает `unarmed`: «движение — только с нулевого запроса». Основания:
  - контракт проекта (граница root);
  - ISO 13850 (по вторичным источникам): снятие — ручным действием там, где подана команда [ист. 13, 14];
  - UI по Wi-Fi не доказывает, что человек видит робота.
- **Этап 2 (по желанию, нужна лаборатория).** Физическое присутствие — кнопка «Сброс» на GPIO Jetson.
  - Root-сервис `neurobot-estop-button` при удержании ≥ 2 с пишет тот же `release.json` с текущими `since` и `reasons`.
  - safety_gate проверяет всё как сейчас: запросу ≤ 10 с, защёлка та же, причина устранена.
  - UI только подсказывает «держите кнопку на роботе» и сам снять ничего не может.
  - GPIO на ядре 4.9 — uAPI v1, он уже есть в hw-mcp [код]. Трудоёмкость ≈1,5–2 дня плюс железо.
- **Отвергнуто:**
  - «подтвердить в UI» или «ввести код из UI»: присутствия не доказывает;
  - токен роли «оператор» на снятие: «у кого ссылка» стало бы равно «у кого sudo», без прямой видимости робота;
  - sudoers на `neurobot estop release` для пользователя UI: та же дыра.
- **Подпись на кнопке STOP — «не аварийная кнопка».** При обрыве Wi-Fi она не сработает. Аварийной остановкой
  остаётся L0 — кнопка у лаборатории, которой пока нет [код; ист. 15].

### 4.5. Как не стать вторым писателем `/cmd_vel`

1. **В коде.** Издатели создаются из константного списка: `/neurobot/stop_now`, `/neurobot/estop`, а после
   исполнителя v2 — `/neurobot/task`. Тест падает, если узел объявит другую тему. Прямо запрещены `/cmd_vel`,
   `/neurobot/cmd_vel_req`, `/cmd_vel_joy` и `/cmd_vel_tracker` (те, что выше safety_gate в twist_mux),
   `/diff_cont/*` и все датчики.
2. **В графе.** `wait-graph ui` после READY и строка doctor. safety_gate по-прежнему защёлкивается на чужого писателя
   `/cmd_vel` — отказ безопасный.
3. **Движение — только задачами через исполнитель.** Бэкенд сам скорость не регулирует.
   - Телеуправление «держи — едет» — этап 2 и только во вход исполнителя (U2 §5).
   - Транспорт — POST-heartbeat 10 Гц по keep-alive. Бэкенд держит вход, пока heartbeat моложе 0,3 с. WebSocket на
     stdlib 3.8 писать вручную (websockets для 3.8 застыл на 13.1).
   - Захват управления — одним оператором.
4. **В MVP движения из UI нет вовсе** — только STOP и защёлка.

### 4.6. Что видит агент

- Агенту не добавляется ни прав, ни поверхности:
  - глобы rosbridge `:9090` не меняются;
  - UI не регистрируется MCP-сервером в PicoClaw или EdgeCtx;
  - API закрыт токеном.
- Агент видит только последствия: стоп с `by: ui:*` в состоянии safety_gate, которое и так читает `robot_get_status`.
- По желанию можно сообщать EdgeCtx `POST /v1/event {kind: situation, name: operator_stop}`. Правило гейта —
  «уведомить без LLM», чтобы стоп оператора не стоил токенов. Права агента от этого не растут.
- UI **не опрашивает** MCP лаборатории (`robot_get_status`):
  - каждый вызов идёт в `/rosapi/topics`, а вызовы сервисов rosbridge 1.3.1 в цикле текут (M11);
  - синхронные инструменты FastMCP занимают цикл событий агента (M5) [код].
- С robot-mcp пакета 2 можно подписаться на `robot://state/robot` только на чтение — панель «глазами агента»
  (что и сколько токенов агент видит).

### 4.7. Lichtblick: где он уместен

- **Офлайн, на ноутбуке.** На Jetson ничего не добавляет.
  - Запись прогона: `ros2 bag record` нужных тем на двойнике или роботе, `.db3`.
  - Lichtblick читает известные типы. Неизвестные — через `mcap convert` [ист. 7].
  - Раскладка: 3D со сканом и TF, Plot `safety_state.out` против `req`, Raw `reasons`.
- **Онлайн — только отладка инженером.** Юнит `neurobot-debugbridge`:
  - не в `neurobot.target`, запуск руками, остановка после сеанса;
  - rosbridge 1.3.x на `127.0.0.1:9091`, доступ через SSH;
  - `topics_glob` — только темы, подмена которых ничего не решает: `/neurobot/ui/*` — копии от бэкенда — и
    `/neurobot/agent/safety_state`;
  - `services_glob` — `[/rosapi/topics]`, `params_glob` — `[none]`;
  - precheck — по тем же правилам, что у rosbridge агента.

  Если нужны сырые `/scan` или `/odom` — только офлайн по записи.

### 4.8. Нагрузка на Jetson 4 ГБ [оценка; мерить на двойнике, затем на Nano]

| Поток | Jetson | Wi-Fi до браузера |
|---|---|---|
| Узел UI (rclpy + HTTP) | ≈60–70 МиБ уникальных (аналоги — 57–63 [замер]). Это ≈12 % запаса GNOME (539) или ≈5 % headless (1469) | — |
| Состояния (5 + 2 + 1 Гц), события | разбор String JSON, < 1 % ядра | ≈6 КБ/с |
| `/scan` → бины 1°, 5 Гц; `/odom` → 5 Гц | подписка 10 Гц и 683 float, единицы процентов ядра | ≈3–8 КБ/с |
| `/cmd_vel` 10–20 Гц — для задержки «жест → команда», как `observer.py` | мало | — |
| Превью 320×240 JPEG 2–5 к/с из perception (патч, только при зрителе) | кодирование — единицы мс на кадр на A57 [оценка, не мерено] | ≈20–100 КБ/с |
| Сырые кадры, глубина, IMU 200 Гц | **не подписываться** (≈13,8 МБ/с копий в Python) | — |
| doctor | только по кнопке: снимок графа rclpy стоит CPU | — |
| Запись на SD | только журнал действий оператора (JSONL, ротация) | — |

## 5. Совместимость

- **Бэкенд.** Python 3.8, только stdlib и rclpy Foxy:
  - `http.server.ThreadingHTTPServer`, `json`, `secrets`, `hmac.compare_digest`;
  - без f-строк с `=` и без синтаксиса 3.9+;
  - `ruff --target-version py38`, тесты под uv 3.8 и `py_compile` 3.8 — как у `neurobot/deploy`.

  На стенде тот же код идёт на Jazzy (Python 3.12). Тип `/cmd_vel` берётся из `NEUROBOT_CMD_VEL_TYPE`.
- **Фронтенд** — статика, собирается на Mac (esbuild). На роботе не нужны ни Node, ни интернет.
- **Go-компоненты** не меняются. UI — их клиент:
  - EdgeCtx — HTTP `127.0.0.1:18080`;
  - sysmon — JSONL;
  - robot-mcp — MCP, только чтение, пакет 2.
- **PicoClaw 0.2.8.** Чат идёт через канал `pico` с Bearer-токеном, который хранится в бэкенде и в браузер не уходит
  (U2 §5). Токены PicoClaw считает mockllm: на стенде — заглушка, на роботе — `--upstream`.
- **systemd 245.** Директивы юнита — как у остальных; проверка — `verify/systemd245-directives.txt`.

## 6. Проверка на двойнике (ВМ sim, без железа)

Стенд: `neurobot/sim/suites/setup_twin.sh` и `systemctl start neurobot.target`, затем юнит `neurobot-ui`. Браузер
на Mac ходит через **тот же туннель, что к роботу**:
`ssh -F ~/.lima/sim/ssh.config -L 18120:127.0.0.1:18120 lima-sim`. Сеть Wi-Fi имитируют профили `bench/board`
(wifi 6 мс, wifi-weak 30 мс и 1 %, offline): `tc netem` только на порт туннеля. E2E — Playwright 1.63 (Python) на Mac.

| # | Критерий | Как | Цель (стенд) |
|---|---|---|---|
| U1 | UI — не писатель | граф: издатели узла ⊆ списка; `foreign_writer` = 0; precheck и doctor «ОК» | 100 % проверок |
| U2 | STOP из UI → ноль в `/cmd_vel` | расширить `verify/k6.py` случаем `ui`: POST через туннель, n = 30, профили lan, wifi, wifi-weak | p95 ≤ 100 мс на lan; поехал после стопа 0 из 30; на wifi-weak записать, как есть |
| U3 | Защёлка | engage из UI → `estop` ≤ 0,3 с. Перебор API и тел: снятия нет; `release_denied` не растёт (UI release не шлёт вовсе). `sudo neurobot estop release` в псевдо-TTY → UI показывает `unarmed` ≤ 1 с | 100 % |
| U4 | Доступ | без токена — 401; чужой `Host`/`Origin` — 403; роль «наблюдатель» → `/api/task` 403, STOP 200; токена нет в журнале | 100 % |
| U5 | Агент без новых прав | diff глобов rosbridge до и после — пусто; precheck rosbridge и mcp — «ОК»; из-под `jetson` без токена — 401; адреса UI нет в конфигах агентов | 100 % |
| U6 | Ресурсы | `neurobot/sim/budget`: UI в `jetson.slice`, PSS и уникальные; CPU при 0, 1 и 3 клиентах, превью вкл. и выкл.; трафик — счётчики бэкенда | цели [допущение]: ≤ 80 МиБ, ≤ 5 % ядра без видео, ≤ 150 КБ/с с превью; OOM 0 |
| U7 | UI не мешает рефлексу | K1 «жест → команда» (observer, n ≥ 30): без UI против 1 и 3 открытых клиентов | разница в пределах разброса прогонов |
| U8 | Отказы | kill -9 UI → робот не замечает, systemd поднимает ≤ 3 с; netem offline → плашка «нет связи» ≤ 2 с; kill rosbridge, MCP или агента → UI показывает ≤ 3 с | 100 % |
| U9 | Час работы | расписание `long` с открытым UI (Playwright headless) | наклон PSS UI, 0 ошибок SSE, переподключения посчитаны |
| U10 | Foxy и 3.8 | логика под uv 3.8, `py_compile` 3.8; только API rclpy, которые есть в ветке foxy (§2) | на роботе — [допущение до HIL] |

Все числа — ориентир стенда, не результат робота (правило проекта №1).

## 7. Трудоёмкость, дни

| Работа | MVP | Полный (C) |
|---|---|---|
| Контракт: API, роли, темы; правки README и RUNBOOK | 0,5 | 1 |
| Бэкенд: подписки, SSE, токен и Host/Origin, список издателей, EdgeCtx и mockllm, cgroup и sysmon | 1,5 | 2,5 |
| Фронтенд в инженерном стиле: строка состояния, полярный скан со слепым сектором, цепочка `req → out`, таймлайн uPlot, панель агента, STOP, баннер защёлки | 1,5 | 3 |
| Деплой: юнит, `precheck ui`, `wait-graph ui`, `neurobot ui-url`, env, install.sh | 0,5 | 1 |
| Превью в патче perception (только при зрителе) | — | 0,5–1 |
| Задачи и рефлекс через `/neurobot/task` (после исполнителя v2) | — | 0,5 |
| Тесты: логика, процессы в ВМ, наборы U1–U10 на двойнике | 1–1,5 | 2,5 |
| Lichtblick: раскладка, запись прогонов, по желанию debugbridge | — | 0,5–1 |
| **Итого** | **≈5–6** | **≈10–13** |

Кнопка «Сброс» на GPIO (§4.4) — отдельно, ≈1,5–2 дня. Телеуправление — отдельно, ≈1,5 дня плюс правка исполнителя
(U2 §4).

## 8. Риски и вопросы

- **Риски:**
  - браузерный STOP примут за аварийную кнопку — нужны подпись и RUNBOOK;
  - SSE через туннель на плохом Wi-Fi рвётся — нужны `retry` и плашка «нет связи»;
  - если `sudo` на роботе у всех, роль «оператор» ничем не отличается от наблюдателя;
  - память: с GNOME «через часы» запас тает (budget §6.1) — UI первым кандидатом на отключение;
  - превью трогает код лаборатории (патч perception).
- **Вопросы лаборатории:**
  - есть ли SSH-доступ с ключами;
  - кто имеет `sudo`;
  - стоят ли `compressed_image_transport` и realsense-ros (U2 §7);
  - можно ли ставить кнопку на GPIO;
  - headless или GNOME;
  - нужен ли доступ с планшета без SSH (тогда — reverse proxy с TLS, отдельное решение).
- **В проверенных источниках не найдено:**
  - работает ли web_video_server на Foxy;
  - пишет ли PicoClaw 0.2.8 usage токенов в журнал. Поэтому токены считает учётный прокси mockllm.

## 9. Источники

1. foxglove/ros-foxglove-bridge: README (параметры, MIT) и CHANGELOG 0.8.5 (2025-05-15) — https://github.com/foxglove/ros-foxglove-bridge ; https://raw.githubusercontent.com/foxglove/ros-foxglove-bridge/main/CHANGELOG.rst
2. foxglove_bridge README (Humble): «earlier releases of ROS will not be supported» — https://docs.ros.org/en/ros2_packages/humble/api/foxglove_bridge/__README.html ; foxglove-sdk/ros (humble, jazzy, kilted, rolling) — https://github.com/foxglove/foxglove-sdk/tree/main/ros
3. Foxglove docs, ROS Foxglove bridge (remote_access, device token, 2025–2026) — https://docs.foxglove.dev/docs/visualization/ros-foxglove-bridge
4. ROS Index, foxglove_bridge (дистрибутивы в индексе: Humble, Jazzy и новее; Foxy нет) — https://index.ros.org/p/foxglove_bridge/
5. RobotWebTools/rosbridge_suite: теги и `rosbridge_server/CHANGELOG.rst` (4.2.1 — 2026-08-12, 1.3.2 — 2023-09-27, #586 «Remove authentication features»), BSD-3 — https://github.com/RobotWebTools/rosbridge_suite
6. ROS Discourse, «ROS Cross-Distribution Communication» — https://discourse.openrobotics.org/t/ros-cross-distribution-communication/27335
7. lichtblick-suite/lichtblick: релизы (v1.29.1 — 2026-09-08), LICENSE (MPL-2.0), `dataSources/*`, `rosdb3/RosDb3IterableSource.ts` — https://github.com/lichtblick-suite/lichtblick
8. «Foxglove 2.0 — integrated UI, new pricing, and open source changes» — https://discourse.openrobotics.org/t/foxglove-2-0-integrated-ui-new-pricing-and-open-source-changes/36583
9. RobotWebTools/web_video_server, ветка ros2, 3.1.1, BSD-3 — https://github.com/RobotWebTools/web_video_server
10. PyPI: aiohttp 3.14.5 (Python ≥ 3.10), websockets (13.1 — последняя для 3.8) — https://pypi.org/project/aiohttp/ ; https://pypi.org/project/websockets/
11. ros2/rclpy, ветка foxy: `node.py`, `publisher.py` — https://github.com/ros2/rclpy/tree/foxy
12. MCP Specification 2025-11-25, Transports, Security Warning — https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
13. Pilz FAQ (по DIN EN ISO 13850): снятие — на том устройстве, где подана команда, и не с пульта — https://www.pilz.com/en-INT/support/faq/standards/articles/180045
14. Machinery Safety 101, «ISO 13850 Application…» (2026-05-18) — https://machinerysafety101.com/2026/05/18/iso-13850-emergency-stop-requirements/
15. ABB, Training Guide No. 2 (ISO 13850: фиксация команды) — https://library.e.abb.com/public/e32f367d17642f29852575d20068205e/1SXU150186X0201.pdf ; Banner (прямое размыкание, ISO 13850 / IEC 60204-1) — https://www.automation.com/article/banner-releases-surface-flush-mount-e-stop
16. Jupyter Server, Security (токен при старте, ссылка, cookie) — https://jupyter-server.readthedocs.io/en/latest/operators/security.html
17. npm registry: preact 10.29.8, uplot 1.6.32, htmx.org 2.0.11, roslib 2.1.0; PyPI: jetson-stats 7.2.2 (AGPL-3.0), playwright 1.63.0 — https://registry.npmjs.org ; https://pypi.org
18. Lima, Port forwarding — https://lima-vm.io/docs/config/port/
