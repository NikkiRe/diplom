# U1. Готовые веб-инструменты для ROS 2: что взять для пульта робота NeuroChip

Исследование 19.10.2026 (агент, часть «tools»). Внешние факты — со ссылкой (раздел 8), даты и версии проверены
19.10.2026 по GitHub API, rosdistro и документации. Метки: [источник] — факт из ссылки; [код проекта] — из нашего
репозитория; [оценка] — вывод без замера; «в проверенных источниках не найдено» — искал и не нашёл.

## 0. Коротко

- **Готового пульта, который подходит роботу как есть, нет.** Мешают три вещи.
  1. Foxy и rosbridge 1.3.1 на роботе. Новые мосты под Foxy не выпускаются: foxglove_bridge Foxy не поддерживал никогда, Vizanti — от Humble.
  2. В rosbridge 1.3.1 один `topics_glob` и на чтение, и на запись. Открыть пульту `/scan` или `/neurobot/safety_state` — значит открыть их на подмену.
  3. Наши темы `/neurobot/*` — это `std_msgs/String` с JSON. Готовые панели рисуют их как строку и не строят по ним графики.
- **Аутентификации нет ни у одного открытого моста.** Её нет у rosbridge (ROS 2), foxglove_bridge, web_video_server, ROSboard и Vizanti; есть только TLS. Значит, вход должен давать наш слой: `127.0.0.1` + `ssh -L` или обратный прокси с TLS и паролем.
- **Опасные «готовые» функции.** У Vizanti есть Node Manager: он запускает и убивает узлы из браузера. У ros-control-center и панелей Publish/Teleop/Parameters в Lichtblick есть запись в любые темы и параметры. PicoClaw WebUI правит конфиг агента. Все они дают новые права, а это нарушает инварианты.
- **Что берём готовым:**
  - Lichtblick (MPL-2.0) на ноутбуке — инженерный «осциллограф» для двойника и разбора записей;
  - roslib 2.1.0 (BSD-2) — клиент rosbridge для своего фронта;
  - foxglove_bridge 3.6.x — только на двойнике (Jazzy, apt), только на чтение;
  - web_video_server — только на двойнике;
  - node_exporter + Prometheus/Grafana — на ноутбуке, по желанию;
  - UX-приёмы Formant, Freedom Pilot, Vizanti и ROSboard.
- **Что пишем сами:**
  - read-only шлюз на роботе: подписки на темы, `neurobot doctor --json`, метрики, выдача SSE/WebSocket на 127.0.0.1;
  - команды только через MCP-сервер, с теми же правами, что у агента, плюс «поставить защёлку»;
  - простая статическая страница в инженерном стиле.

  Снятия защёлки в интерфейсе нет: UI показывает причины и команду `sudo neurobot estop release`.

## 1. Рамки, по которым оценивал

- **Робот** — Jetson Nano 4 ГБ, arm64, Ubuntu 20.04, ROS 2 Foxy (EOL 20.06.2023 [источник: ROS EOL]), Python 3.8.
  Стенд — ВМ sim, Jazzy [код проекта].
- **rosbridge на роботе — 1.3.1:** тег 21.10.2022, в Foxy через apt `1.3.1-1` [rosdistro foxy]. Раздельных
  `topics_pub_glob`/`topics_sub_glob` и действий (actions) нет [код проекта: deploy README; исходник 1.3.1].
- **Память.** Весь стек — 1866,6 МиБ уникальной памяти. Запас с GNOME — 539 МиБ, headless — 1469 МиБ. Сам rosbridge — 111 МиБ PSS
  (Jazzy 2.7.1, ВМ) [код проекта: `neurobot/sim/budget/README.md` §6.1]. Узел rclpy у нас — 33–38 МиБ PSS (исполнитель,
  safety_gate) [там же].
- **Инварианты:**
  - один писатель `/cmd_vel` — safety_gate;
  - защёлку снимает только root-файл;
  - агент не получает новых прав;
  - rosbridge — 127.0.0.1, глобы — список разрешённого.

## 2. Сводная таблица

| Инструмент | Что умеет | Лицензия, версия, статус (19.10.2026) | Foxy / arm64 | Транспорт | Ресурсы на роботе | Вход | Вердикт |
|---|---|---|---|---|---|---|---|
| **Foxglove** (приложение) | 3D, графики, изображения, журнал, Teleop/Publish, записи MCAP | **Закрыт** с 11.03.2024: код 1.x остался, `foxglove/studio` в архиве (последний push 18.07.2024). Free: 5 устройств, 10 ГБ, жёсткие лимиты, без remote access; Academic: 10 устройств, 1 ТБ | Клиент на ноутбуке; к Foxy — только через Rosbridge | Foxglove WS или rosbridge | Только мост | **Аккаунт обязателен**; без входа — Enterprise-лицензия standalone | Не брать: закрыт, нужен аккаунт |
| **foxglove_bridge** (ROS 2) | Мост Foxglove WS: темы, сервисы, параметры, граф; можно отключить `clientPublish` и т. п. (`capabilities`) | MIT, **3.6.2** (16.10.2026), живёт в `foxglove-sdk`; Jazzy apt `3.6.1-1` | **Foxy — нет**: поддерживаются humble/jazzy/kilted/rolling; с 0.1.0 «старше Galactic не поддерживается»; нужен `rosx_introspection` | WebSocket (JSON + бинарный CDR) | C++; цифр в проверенных источниках не найдено | Нет; TLS; `address` по умолчанию 0.0.0.0 | Только двойник, режим read-only |
| **Lichtblick** | Форк Foxglove Studio от BMW: панели 3D, Plot, Image, Map, Log, Diagnostics, Table, Gauge, Indicator, StateTransitions, TopicGraph, Teleop, Publish, CallService, Parameters, User Scripts; расширения (`registerMessageConverter`) | **MPL-2.0**, **v1.29.1** (08.09.2026), активен | Клиент: браузер, десктоп, Docker. Источники: Foxglove WS, **Rosbridge**, ROS 1, файлы MCAP, ROS 1/2 bag, ULog | WS (rosbridge: `cbor-raw` + `/rosapi/topics_and_raw_types` — в 1.3.1 есть) | Только мост; статику можно отдавать с ноутбука | Без аккаунта; входа к мосту нет | Брать для разработки и двойника; Publish/Teleop/Parameters не использовать |
| **Webviz** (Cruise) | Просмотр bag ROS 1, rosbridge; от него форкнут Foxglove | Apache-2.0, последний push 17.12.2022; Cruise поддержку не даёт | ROS 2 в проверенных источниках не найден | rosbridge | — | — | Не брать: мёртв, только ROS 1 |
| **rosbridge_suite** | Протокол rosbridge v2: темы, сервисы, (2.x+) действия; `rosapi` | BSD-3; Foxy **1.3.1**, Humble 2.0.8, Jazzy 2.7.1 (apt), upstream **4.2.1** (12.08.2026) | Foxy — да (стоит на роботе) | WS, JSON / CBOR / `cbor-raw`; SSL | 111 МиБ PSS (наш замер, Jazzy) | **Операции authenticate в ROS 2 нет**; только `address`, SSL, глобы | Есть, но агентский экземпляр трогать нельзя; второй для UI на 1.3.1 — только на чтение невозможен |
| **roslibjs** (`roslib`) | JS-клиент rosbridge: Topic, Service, Action, TF, URDF | **BSD-2-Clause**, **2.1.0** (03.03.2026), ESM + TypeScript | Браузер; `ROS2TFClient` требует `tf2_web_republisher` и действия — с 1.3.1 не работает | WS | 0 на роботе | — | Брать, если свой фронт ходит в rosbridge (двойник) |
| **ros2-web-bridge** | Мост на Node.js + rclnodejs | Apache-2.0, **в архиве** (push 12.01.2022); README: «не поддерживается, берите rosbridge_suite» | Сборка вручную, Node на роботе | WS | Node.js | — | Не брать |
| **RWS** (`v-kiniv/rws`) | C++ замена rosbridge, частично совместимая | Apache-2.0, ветки galactic/humble/jazzy | Foxy-ветки нет | WS (rosbridge v2) | По словам автора Vizanti, накладные ≈5× ниже rosbridge | Параметры README: port, rosbridge_compatible, watchdog; **глобов нет** | Не брать: нет списка разрешённого |
| **Vizanti** | Мобильный веб-пульт в стиле RViz 2D: scan, map, image, TF, teleop-джойстик, кнопки, reconfigure, bag, **Node Manager** (запуск и `pkill` узлов) | BSD-3, ветка ros2 активна (23.08.2026) | Humble/Jazzy/Lyrical; **Foxy не заявлен**; в Jazzy — только из исходников | Flask + waitress :5000, свой rosbridge :5001 | Python-сервер + rosbridge (≈111 МиБ по нашему замеру) | Нет | Не брать на робота: новые права (teleop, параметры, Node Manager); взять UX-идеи |
| **ROSboard** | Веб-сервер на роботе: карточки тем, графики uPlot, изображения, scan, **dmesg и процессы** (psutil); удобен с телефона | BSD-3, **v1.3.1**, коммиты 21.08.2026 | ROS 1 и 2 через прослойку `rospy2`, без пакетов дистрибутива; на Foxy не проверено | Свой WS (tornado), изображения с потерями | Один процесс Python + rclpy (≈35–60 МиБ [оценка по нашим узлам]) | **Нет**; слушает все интерфейсы на :8888; глобов нет | Только на чтение по замыслу — образец; как есть не ставить (dmesg, процессы, все темы, 0.0.0.0) |
| **ros-control-center** | Темы, сервисы, параметры: смотреть и **менять** | BSD-3, push 27.02.2023 | README про ROS 1 (`roslaunch`); ROS 2 не описан | rosbridge + roslibjs | Только rosbridge | Нет | Не брать: «суперпользователь» в браузере |
| **ros3djs / ros2djs** | Виджеты 3D/2D для roslibjs (scan, URDF, карта) | BSD-3; 1.1.0 и 0.10.0 — обе 03.05.2022; зависят от `roslib ^1`, `three ^0.89` | Браузер; TF — через `tf2_web_republisher` | rosbridge | 0 | — | Не брать: устарели; радар зазоров проще нарисовать самим (SVG/canvas) |
| **web_video_server** | HTTP-поток изображений: MJPEG/VP8/VP9/H264/PNG, снимки | BSD-3, **3.1.1** (07.05.2026); Humble/Jazzy apt 3.1.0 | **Foxy: в rosdistro нет**, README — «ROS 2 (Humble+)» | HTTP | C++; кодирование нагружает CPU, цифр не найдено | Нет; `address` 0.0.0.0 | Только двойник; на роботе камеру держит perception |
| **Grafana / Prometheus** | Дашборды и временные ряды | Grafana **AGPL-3.0**, v13.2.4 (13.10.2026), минимум 512 МБ ОЗУ и 1 ядро. Prometheus Apache-2.0, v3.15.0, порт 9090 — **тот же, что у rosbridge** | arm64 есть | HTTP pull | На Nano не ставить [оценка по бюджету] | Свой вход | По желанию, на ноутбуке |
| **Экспортёры ROS** | ROSscope (Prometheus + Grafana, 15 метрик ROS 2); greenwave_monitor (частоты и задержки тем → Diagnostics, TUI); node_exporter; jetson_exporter | ROSscope MIT v0.1.0 (06.06.2026, Humble, Python ≥ 3.10, Docker). greenwave Apache-2.0 (Humble+). node_exporter Apache-2.0 v1.12.1, arm64. jetson_exporter MIT v0.3.1 (заявлен Orin). jetson_stats AGPL-3.0 7.2.2 | Под Foxy готового **официального экспортёра ROS 2 в проверенных источниках не найдено** | HTTP `/metrics` | node_exporter — Go-бинарник | — | node_exporter — можно; метрики ROS у нас уже есть |
| **PicoClaw WebUI launcher** | Браузерный конфиг и чат; с v0.2.9 — управление MCP-серверами | MIT; launcher с v0.2.0 (28.02.2026), v0.3.1 (03.07.2026); на роботе 0.2.8 | :18800, `-public` — все интерфейсы | HTTP | Node.js — только для сборки | Пароль (`dashboardauth`) | **Не запускать на роботе:** правит конфиг агента (`exec`, MCP) |
| **Formant** | Облако: наблюдение, телеуправление, вмешательство, агенты ИИ | Закрыт, облачный; «operating layer for physical AI» (сайт, 07.10.2026); цены 2026 в проверенных источниках не найдены | Агент Formant; ROS 2 нативно с агента 1.226 | WebRTC/облако | Агент Formant | Аккаунт | Не брать; UX — взять |
| **Freedom Robotics** | Pilot: телеуправление и вмешательство (2020), Resource Monitor | Статус 2025–2026 в проверенных источниках не найден; 19.10.2026 сайт не открылся (TLS/409) [наблюдение] | — | Облако | — | Аккаунт | Не брать; UX — взять |

## 3. Детали по инструментам

### 3.1. Foxglove

- **Открытость.** 11.03.2024 вышел Foxglove 2.0: Studio и платформа данных объединены, открытая редакция Studio закрыта.
  «The 1.x source code remains available on GitHub, but our future development will be closed source» (amacneil,
  Discourse). Открытыми остаются MCAP, SDK (MIT) и foxglove_bridge (MIT).
- **Бесплатное использование.**
  - Free: 5 устройств, 10 ГБ, запросы 1 ч/мес, трафик 100 ГБ/мес. Лимиты жёсткие, remote access не входит.
  - Academic: 10 устройств, 1 ТБ.
  - Число пользователей Free на странице цен не указано; в 2024 было «до 3», по выдаче поиска 2026 — тоже 3.
  - Приложение просит войти в аккаунт: «create a free Foxglove account» — в шагах подключения ROS 2. Без входа работает только standalone по ключу Enterprise.
- **foxglove_bridge.**
  - Код ROS 2 переехал в `foxglove/foxglove-sdk/ros`, версия 3.6.2 от 16.10.2026. Поддерживаемые дистрибутивы — humble, jazzy, kilted, rolling.
  - Старый `ros-foxglove-bridge` — теперь только ROS 1.
  - В README 0.1.0 (ноябрь 2022): «Earlier releases of ROS [than Galactic] will not be supported»; в Foxy rosdistro моста нет.
  - Для Foxy остаётся собирать из исходников. Новый мост зависит от `rosx_introspection` и C++ SDK, собрать его на Foxy — не проверено, шансы низкие [оценка].
- **Безопасность моста.**
  - `address` по умолчанию 0.0.0.0.
  - Списки разрешённого: `topic_whitelist`, отдельный `client_topic_whitelist`, `service_whitelist`, `param_whitelist`.
  - В `capabilities` можно убрать `clientPublish`, `services` и `parameters` — тогда мост будет только читать. Это лучше rosbridge 1.3.1, но только на Jazzy, то есть на двойнике.
  - Аутентификации нет, есть TLS. `remote_access` работает через платформу Foxglove по токену устройства — это облако, нам не нужно.

### 3.2. Lichtblick

- **Что это.** Форк Foxglove Studio под MPL-2.0. Copyright BMW AG и Foxglove (2021–2024). Работает в браузере,
  на десктопе (Linux/Windows/macOS) и в Docker (`ghcr.io/lichtblick-suite/lichtblick`). Версия v1.29.1 от 08.09.2026,
  проект активен. Телеметрия выключена в публичных сборках.
- **Источники данных** (код `dataSources/`): Foxglove WebSocket, **Rosbridge** («ROS 1 or ROS 2»), ROS 1 socket,
  локальные MCAP, ROS 1 bag, ROS 2 bag, ULog.
  - Rosbridge-плеер использует `getTopicsAndRawTypes` и `cbor-raw`. В rosbridge 1.3.1 есть и сервис
    `TopicsAndRawTypes.srv`, и `cbor-raw` [исходник тега 1.3.1].
  - Значит, подключиться к Foxy по rosbridge технически можно. На нашем роботе не проверено.
- **Чем полезен нам.**
  - Отладка на двойнике: 3D со сканом, изображения, графики `/odom` и скоростей.
  - Разбор записей прогонов: MCAP или bag.
  - JSON из `std_msgs/String` разворачивается User Scripts (TypeScript в браузере) или расширением с `registerMessageConverter`. После этого Plot и Indicator строят графики по полям `safety_state`.
- **Чем опасен.** Панели Publish, Teleop, CallService и Parameters публикуют, вызывают и меняют параметры от имени
  клиента. Через мост они получают все права, которые ему открыты. Для пульта робота такие панели нельзя
  давать без моста только на чтение.

### 3.3. Webviz

Apache-2.0, последний push 17.12.2022. По README — bag-файлы ROS 1 и rosbridge, коммерческой поддержки Cruise нет.
Поддержка ROS 2 в проверенных источниках не найдена. Для нас — только история: Foxglove начинался как форк Webviz.

### 3.4. rosbridge, roslibjs, ros2-web-bridge, RWS

- **rosbridge_suite.**
  - Версии: Foxy — 1.3.1 (apt, тег 21.10.2022), Jazzy — 2.7.1 (apt), upstream — 4.2.1 (12.08.2026).
  - Протокол v2 поверх WebSocket, кодировки JSON, CBOR, `cbor-raw`, SSL.
  - Аутентификации в ROS 2 нет. В `rosbridge_protocol.py` (1.3.1 и ros2) нет операции authenticate, в дереве нет `rosauth`.
  - В 1.3.1 один `topics_glob` на подписку, объявление и публикацию [код проекта, deploy README «Агентский путь»].
  - **Следствие для UI.** Второй экземпляр rosbridge для пульта, где открыты `/scan` или `/neurobot/safety_state`, открывает их и на запись.
    - `/neurobot/safety_state` читают исполнитель и doctor [код проекта, safety README]. Подделка в этой теме обманет не только пульт.
    - Подмену `/scan` safety_gate ловит (`scan_foreign` → стоп), но это повод для DoS.
  - **Вывод:** на Foxy читать через rosbridge безопасно нельзя. Нужен свой шлюз только на чтение (§5).
- **roslib 2.1.0** (npm, 03.03.2026).
  - Лицензия BSD-2-Clause, ESM, TypeScript, исходники в монорепо `packages/roslib`. Классы: Topic, Service, Action, TFClient, ROS2TFClient, URDF.
  - `ROS2TFClient` работает через действие `tf2_web_republisher`, а действий в 1.3.1 нет.
  - Свой радар рисуем в кадре лидара — TF для этого не нужен [оценка].
- **ros2-web-bridge** — в архиве, Apache-2.0. В README сказано: «This package is not actively maintained. Please use the rosbridge_suite».
- **RWS** — C++, «partially compatible with Rosbridge v2». Ветки galactic, humble и jazzy, Foxy нет. Списков разрешённого в README нет.
  Инвариант «глобы — список разрешённого» он не держит.

### 3.5. Vizanti

- BSD-3, ветка `ros2` активна: последний коммит 23.08.2026, Diagnostics + rosout widget.
- Совместимость: Humble 22.04, Jazzy 24.04, Lyrical 26.04. Foxy не заявлен. В rosdistro Jazzy Vizanti есть только как исходники.
- Состав: сервер Flask + waitress на :5000, свой rosbridge на :5001. Рекомендуется C++ бэкенд RWS. Настройки виджетов хранятся в localStorage.
- Виджеты: scan, map, image, TF, teleop, кнопка, reconfigure, bag, **Node Manager**. Node Manager — «Launch and kill nodes…
  some pkill shenanigans under ROS 2». Это запуск процессов из браузера, то есть новые права в чистом виде.
- **UX взять:**
  - интерфейс под телефон;
  - у каждого виджета «Socket throttle (ms)» — свой предел частоты;
  - при пропаже heartbeat rosbridge страница перезагружается сама;
  - экспорт и импорт раскладки.

### 3.6. ROSboard (найден дополнительно)

- BSD-3, v1.3.1, коммиты 21.08.2026. Это узел-веб-сервер на роботе (tornado). Работает с ROS 1 и 2 через свою
  прослойку `rospy2`, без пакетов дистрибутива. Поэтому, по словам автора, «should theoretically work immediately
  when new ROS distros are released». На Foxy не проверено.
- **Только на чтение по замыслу.** Автор пишет: «I don't want the browser to have "superuser" access to the ROS
  system». Изображения сжимаются с потерями до отправки. Вход по паролю — только в планах.
- **Минусы для нас:**
  - `listen(port)` без адреса — все интерфейсы, :8888;
  - любая тема по запросу клиента;
  - отдаёт dmesg и список процессов.

  Как есть не ставить, но это лучший образец для нашего шлюза: один процесс, только чтение, прореживание.

### 3.7. ros-control-center, ros3djs и ros2djs

- **ros-control-center:** BSD-3, последний push 27.02.2023. README про ROS 1. Умеет публиковать, вызывать сервисы и
  менять параметры — для нашего робота это противопоказано.
- **ros3djs 1.1.0 и ros2djs 0.10.0:** обе версии от 03.05.2022, BSD-3, зависят от `roslib ^1.0.0`, three 0.89 и
  EaselJS. Брать их ради одного радара скана тяжело. Полярный график на canvas/SVG проще [оценка].

### 3.8. web_video_server

- BSD-3, 3.1.1 от 07.05.2026. Форматы MJPEG, VP8, VP9, H264, PNG; есть снимки. README: «ROS 2 (Humble+)», в rosdistro
  Foxy пакета нет. `address` по умолчанию 0.0.0.0, аутентификации нет.
- **На роботе камеру держит perception** (`--camera rs`). При `--camera ros` — драйвер [код проекта, deploy README].
  - При `rs` ROS-темы изображения нет, и web_video_server нечего показывать.
  - Кодировать второй видеопоток на Nano рядом с YOLO — минус CPU у распознавания [оценка].
- **Лучше:** редкий JPEG-снимок по запросу от perception или «кадр с рамкой жеста» после события. Это уже предусмотрено как `look(image)` в robot-mcp [код проекта, PLAN §2.6].

### 3.9. Grafana, Prometheus и экспортёры

- **Grafana:** v13.2.4, AGPL-3.0. Минимум по документации — 512 МБ ОЗУ и 1 ядро. На Nano не ставить: это около 35 % headless-запаса
  [оценка по §6.1 бюджета].
- **Prometheus:** v3.15.0, Apache-2.0. Порт по умолчанию 9090 совпадает с rosbridge.
- **Где запускать.** Prometheus и Grafana — на ноутбуке. На роботе — только экспортёр:
  - node_exporter v1.12.1, официальный бинарник arm64, :9100;
  - или маленький `/metrics` в нашем шлюзе.
- **Экспортёры ROS:**
  - ROSscope — MIT, v0.1.0 от 06.06.2026, Humble, Python ≥ 3.10, Docker: под Foxy и Python 3.8 не подходит;
  - greenwave_monitor (NVIDIA) — Apache-2.0, Humble+, считает частоты и задержки тем в Diagnostics, дашборд в терминале;
  - официального экспортёра ROS 2 → Prometheus в проверенных источниках не найдено;
  - jetson_exporter заявлен для Orin, jetson_stats — AGPL-3.0.
- **У нас уже есть** [код проекта]:
  - `sysmon` — PSS, CPU и IO процессов и cgroup в JSONL;
  - EdgeCtx `GET /v1/metrics` — LLM-вызовы, токены, байты, состояние связи, MCP;
  - трасса JSONL.

  Для ВКР этого достаточно. Grafana — только для красоты на защите.

### 3.10. PicoClaw WebUI (найден попутно)

- Launcher — браузерная настройка и чат на :18800, с `-public` — все интерфейсы. Появился в v0.2.0 (28.02.2026), в v0.2.9 добавлено
  «MCP server management in Web UI». Вход по паролю есть.
- **Риск.** Через WebUI можно включить `exec`, добавить MCP-сервер или сменить ключ, то есть дать агенту новые права в обход
  `picoclaw-config check` и precheck. На роботе стоит 0.2.8 [код проекта, R7].
- **Рекомендация:** launcher на роботе не запускать. doctor может проверять, что порт 18800 не слушается [предложение].

### 3.11. Formant и Freedom Robotics — что взять в UX

- **Formant** (документация от 11.11.2025):
  - пульт оператора шлёт heartbeat «every 50ms from each connection»;
  - на потерю связи робот должен реагировать своим кодом на стороне устройства;
  - сеансы «наблюдать» и «управлять» различаются;
  - телеуправление — клавиатура, мышь, геймпад, кнопки, клик по изображению.

  У нас deadman уже на роботе — входы старше 0,4–0,5 с дают ноль [код проекта]. Значит, переносим не механизм, а
  разделение режимов и явный индикатор связи.
- **Freedom Pilot** (2020):
  - «stopping the robot» — отдельное действие;
  - живой просмотр тем;
  - оповещения, чтобы человек зашёл;
  - Resource Monitor.

  Статус компании в 2025–2026 в проверенных источниках не найден.
- **Общие приёмы для нашего пульта:**
  1. Большая кнопка «Стоп» всегда видна. Она вызывает `robot_stop`, это стоп без защёлки (решение 1а).
  2. Отдельная кнопка «Аварийная защёлка» — `engage`.
  3. Режим «Наблюдение» по умолчанию. «Управление» включается явно и выключается по таймауту.
  4. Индикатор связи и возраст данных у каждой карточки. Устаревшее — серым, а не последним значением.
  5. Лента событий: жест, `task_end`, `alert`, смены `safety_state`, `doctor`.
  6. Причины стопа — словами из `reasons`.

## 4. Инварианты и готовые инструменты

| Инвариант | Что ломают готовые инструменты | Как держать |
|---|---|---|
| Один писатель `/cmd_vel` — safety_gate | Teleop в Vizanti, Lichtblick и Foxglove по умолчанию пишет Twist в `/cmd_vel` | В пульте нет джойстика и Twist; движение — только задачами MCP (`robot_nudge` и др.) через исполнитель → safety_gate |
| Защёлку снимает только человек через root-файл | Кнопка «снять» в вебе стала бы новым каналом снятия | В UI только «поставить» и показ причин; снятие — текст команды `sudo neurobot estop release` |
| Агент не получает новых прав | PicoClaw WebUI, Node Manager в Vizanti, Parameters и CallService | Шлюз без записи в ROS; команды только через тот же MCP-сервер, что у агента, или вообще без команд |
| rosbridge минимален, глобы — список разрешённого | Готовые пульты хотят свой rosbridge «на всё» (Vizanti :5001, ros-control-center) | Агентский rosbridge не трогаем; пульт к нему не подключается; чтение — свой шлюз |
| Нет аутентификации у мостов | rosbridge, foxglove_bridge, web_video_server, ROSboard слушают 0.0.0.0 по умолчанию | Всё на 127.0.0.1; доступ — `ssh -L`, то есть аккаунт на роботе. Вариант — обратный прокси с TLS и паролем |

## 5. Вывод: что брать готовым, что писать самим

**Брать готовым:**

| Что | Где | Зачем |
|---|---|---|
| Lichtblick v1.29.x (MPL-2.0) | Ноутбук или Docker | Отладка на двойнике и разбор записей: 3D, скан, графики. Свои поля JSON — через User Scripts |
| foxglove_bridge 3.6.x (MIT) | Только двойник, Jazzy apt | Источник для Lichtblick: `address:=127.0.0.1`, `capabilities` без `clientPublish`, `services`, `parameters`, белый список тем |
| roslib 2.1.0 (BSD-2) | Свой фронт, если ходить в rosbridge на двойнике | Готовый клиент протокола |
| web_video_server 3.1.x | Только двойник | Видео камеры двойника |
| node_exporter 1.12.1 + Prometheus/Grafana | Экспортёр на роботе; остальное на ноутбуке; по желанию | Красивые графики ресурсов; для ВКР достаточно `sysmon` |
| UX-приёмы Formant, Freedom Pilot, Vizanti, ROSboard | — | §3.11, §3.5, §3.6 |

**Писать самим** (оценка объёма — [оценка]):

1. **Шлюз на роботе, только чтение** — `neurobot-ui`, Python 3.8, rclpy, один процесс; ≈35–60 МиБ по аналогии с нашими узлами 33–38 МиБ PSS.
   - **Подписки:**
     - `/neurobot/safety_state` (5 Гц);
     - `motion_state`, `perception_health`, `gesture`, `events`;
     - `/scan`, прорежённый до 2–5 Гц и до сводки по секторам;
     - `/odom`.
   - **Опросы:** `neurobot doctor --json` по таймеру и по кнопке. Без root и без группы `adm`/`systemd-journal` doctor
     предупреждает и не ставит диагноз PicoClaw [код проекта, deploy README]. Пользователя шлюза надо добавить в группу журнала — это решение студента.
   - **Метрики:** EdgeCtx `/v1/metrics` и `/v1/health`, когда агент — EdgeCtx; `sysmon` JSONL.
   - **Выдача:** SSE или WebSocket JSON на `127.0.0.1`. Шлюз не создаёт издателей ROS, кроме одного — `/neurobot/estop` с `engage`.
     Вариант без издателей — вызывать `neurobot estop engage`.
2. **Команды** — через MCP-сервер робота как MCP-клиент:
   - сейчас: `robot_get_status`, `robot_stop`, `robot_nudge` патченого `ros_mcp_server`;
   - в Э3: `status`, `drive`, `turn`, `maneuver`, `stop`, `reflex` robot-mcp.

   Пределы L3 — те же. Права пульта не шире агентских.
3. **Чат с агентом** — `POST /v1/user` EdgeCtx: ответ с `latency_ms`, `llm_calls`, `tool_calls` — готовые числа для метрики ВКР.
   Для PicoClaw — его канал, не WebUI.
4. **Фронт** — одна статическая страница: без сборки или Vite; uPlot (MIT) для временных рядов — его же использует ROSboard. Состав:
   - радар зазоров (`min_clearance_m`, `front_m`, сектор скана, слепой сектор);
   - состояние safety: ok, slow, blocked, estop, `reasons` словами;
   - цепочка `req → out`;
   - лента событий;
   - последний жест;
   - память и CPU;
   - LLM: вызовы, токены, ₽, задержки.
5. **Декодер JSON в `std_msgs/String`.** Готовые панели его не дают.

**Не брать:**
- Foxglove — закрыт, нужен аккаунт;
- Webviz и ros2-web-bridge — мертвы;
- ros-control-center, Vizanti и ROSboard на роботе как есть — новые права или нет аутентификации;
- RWS — нет глобов;
- Grafana и Prometheus на Nano — память;
- PicoClaw WebUI на роботе.

## 6. Что не проверено

- Lichtblick → rosbridge 1.3.1 на Foxy: список тем, `cbor-raw`, `/rosapi/*` при наших `services_glob`. Проверить в контейнере Foxy; загрузка образа — с разрешения.
- Сборка foxglove_bridge на Foxy — не пробовал. По зависимостям шансы низкие [оценка].
- ROSboard на Foxy и Python 3.8 — только заявление автора.
- Память и CPU шлюза на Nano — оценка по аналогии, нужен замер `sysmon`.
- roslib 2.x против rosbridge 1.3.1 — совместимость протокола не проверена.
- Цены Formant 2026 и статус Freedom Robotics — в проверенных источниках не найдены.

## 7. Числа для сравнения на роботе [код проекта]

| Компонент | PSS, МиБ | Замер |
|---|---|---|
| rosbridge (`ros2 launch` + websocket + rosapi) | 111 | Jazzy 2.7.1, ВМ, §6.1 бюджета |
| узел rclpy (исполнитель / safety_gate) | 33 / 38 | там же |
| ros_mcp_server | 42 | там же |
| запас headless / GNOME | 1469 / 539 | `runs/budget_v5b` |

## 8. Источники (проверено 19.10.2026)

- Foxglove 2.0, закрытие Studio, 11.03.2024: https://discourse.openrobotics.org/t/foxglove-2-0-integrated-ui-new-pricing-and-open-source-changes/36583
- Цены и лимиты Foxglove: https://docs.foxglove.dev/docs/pricing ; https://www.foxglove.dev/pricing
- Foxglove standalone (без входа, Enterprise): https://docs.foxglove.dev/docs/standalone-license
- Foxglove + ROS 2 (bridge, Rosbridge, аккаунт): https://docs.foxglove.dev/docs/connecting-to-data/frameworks/ros2
- foxglove_bridge ROS 2, 3.6.2, MIT, дистрибутивы и параметры: https://github.com/foxglove/foxglove-sdk/tree/main/ros ; https://github.com/foxglove/foxglove-sdk/blob/main/ros/src/foxglove_bridge/README.md
- foxglove_bridge 0.1.0, «earlier releases… not supported»: https://github.com/foxglove/ros-foxglove-bridge/blob/0.1.0/README.md
- Архив `foxglove/studio`: https://github.com/foxglove/studio
- Lichtblick (MPL-2.0, v1.29.1, источники данных, панели): https://github.com/lichtblick-suite/lichtblick ; https://lichtblick-suite.github.io/docs/
- Webviz: https://github.com/cruise-automation/webviz
- rosbridge_suite (1.3.1, 4.2.1, протокол): https://github.com/RobotWebTools/rosbridge_suite ; https://github.com/RobotWebTools/rosbridge_suite/blob/1.3.1/rosbridge_server/launch/rosbridge_websocket_launch.xml
- rosdistro (Foxy, Humble, Jazzy): https://github.com/ros/rosdistro/blob/master/foxy/distribution.yaml ; …/jazzy/distribution.yaml
- Foxy EOL: https://docs.ros.org/en/lyrical/Releases/End-of-Life.html (Foxy EOL 20.06.2023); https://www.therobotreport.com/foxy-fitzroy-ros2-releases-reaches-end-of-life/
- roslibjs 2.1.0: https://github.com/RobotWebTools/roslibjs ; https://www.npmjs.com/package/roslib
- ros2-web-bridge (архив): https://github.com/RobotWebTools/ros2-web-bridge
- RWS: https://github.com/v-kiniv/rws
- Vizanti: https://github.com/MoffKalast/vizanti/tree/ros2 ; https://github.com/MoffKalast/vizanti_docs (FAQ, Node Manager)
- ROSboard: https://github.com/dheera/rosboard
- ros-control-center: https://github.com/pantor/ros-control-center
- ros3djs, ros2djs: https://github.com/RobotWebTools/ros3djs ; https://github.com/RobotWebTools/ros2djs
- web_video_server 3.1.1: https://github.com/RobotWebTools/web_video_server
- Grafana (требования): https://grafana.com/docs/grafana/latest/setup-grafana/installation/ ; https://github.com/grafana/grafana
- Prometheus, node_exporter: https://github.com/prometheus/prometheus ; https://github.com/prometheus/node_exporter
- ROSscope: https://discourse.openrobotics.org/t/rosscope-open-source-observability-platform-for-ros-2-fleets-v0-1-0/55338 ; https://github.com/jawad-glitch/ROSscope
- greenwave_monitor: https://github.com/NVIDIA-ISAAC-ROS/greenwave_monitor
- jetson_exporter, jetson_stats: https://github.com/GDWR/jetson_exporter ; https://github.com/rbonghi/jetson_stats
- PicoClaw (WebUI launcher): https://github.com/sipeed/picoclaw
- Formant: https://docs.formant.io/docs/teleoperation ; https://docs.formant.io/docs/handle-disconnections-safely ; https://docs.formant.io/docs/ros-2-adapter ; https://formant.ai/
- Freedom Robotics: https://techcrunch.com/2020/06/17/freedom-launches-pilot-to-simplify-teleoperation-on-third-party-robots ; https://www.therobotreport.com/freedom-pilot-resource-monitor-enable-rapid-robot-deployment/
