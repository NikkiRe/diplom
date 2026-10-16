# neurobot/deploy — запуск, супервизия, аварийная защёлка и агентский путь (этапы Э2, Э2.5)

Пакет поднимает весь стек робота NeuroChip одной командой и объясняет, почему что-то не поднялось. Основание —
`docs/neurobot/PLAN.md` §4, §5, §13, `ANALYSIS.md` (M*, PC*, §5.2) и разбор материалов лаборатории от 14.10
(`research/R7_robotic_platform.md`: N*, `R7_robot_slides.md`: RS*). Инструкция для лаборатории — `RUNBOOK.md`.

Что здесь наше, а что готовое: юниты, CLI `neurobot` (doctor, проверки, защёлка, сторож, start_test), патчи и
конфиги — наша разработка. systemd, journald, logrotate, ROS 2, rosbridge_suite, twist_mux, ros2_control, PicoClaw,
MCP SDK (FastMCP) — готовые компоненты; `ros_mcp_server.py`, `robot_vision_mcp_proxy.py`, `predict_live.py`, воркеры
и мост к STM32 — код лаборатории. Сам safety_gate (`neurobot/safety`) пишется отдельно (шаг W2); здесь — его юнит,
проверки вокруг него и CLI защёлки.

## Состав

| Путь | Что |
|---|---|
| `units/` | шаблоны юнитов: `neurobot.target`, `neurobot-driver@.service`, `-safety`, `-executor`, `-perception`, `-rosbridge`, `neurobot-labworker@.service`, `-mcp`, `-picoclaw`, `-watchdog` |
| `neurobot.env.example` | единый env-файл робота (Foxy) → `/etc/neurobot/neurobot.env` |
| `install.sh` | установка / удаление: юниты из env-файла, CLI, исполнитель и safety_gate (`../executor`, `../safety`), drop-in journald, logrotate; `--no-journald` — без drop-in journald и без перезапуска journald (стенд), `--no-enable` — юниты поставить, но не включать в автозапуск; `--help` |
| `bin/neurobot` | CLI (Python 3.8, только stdlib; rclpy — лениво) |
| `bin/neurobot-run` | обёртка ExecStart: окружение ROS 2 и команда из переменной env-файла, `exec` без смены PID |
| `neurobot_deploy/` | код CLI: `config` env-файл, `checks` проверки перед стартом, `probes` WebSocket, MCP и HTTP, `picolog` журнал PicoClaw, `picoclaw_cfg` версия, схема и оверлей PicoClaw, `doctor`, `estop` защёлка, `watchdog`, `start_test`, `rosgraph` (rclpy), `render`, `report` коды причин |
| `journald/50-neurobot.conf` | лимиты журнала, журнал на диске |
| `logrotate/neurobot-picoclaw` | ротация `~/.picoclaw/logs/gateway.log` (PicoClaw сам её не делает, N15) |
| `mcp/ros_mcp_server.patch`, `mcp/ros_mcp.json.example` | патч к присланному `ros_mcp_server.py` и его конфиг |
| `mcp/robot_vision_mcp_proxy.patch` | патч к прокси лаборатории `robot_vision_mcp_proxy.py` (дубли инструментов, пределы, HTTP) |
| `picoclaw/config.overlay.json` | оверлей конфига PicoClaw; `picoclaw/schema-<версия>.json` — поля config.json версий 0.2.8 и 0.3.1 |
| `weights.sha256.example` | манифест весов для проверки перед стартом |
| `fakes/` | подставные компоненты, профиль и сценарии стенда для проверки запуска без робота |
| `verify/` | директивы systemd v245 (Ubuntu 20.04), проверка юнитов systemd-analyze 245, генератор схем PicoClaw |

## Цепочка движения (контракт двойника, PLAN §13.3)

```
жесты: perception (--mode legacy) -> /cmd_vel_gesture ┐
агент: MCP-сервер -> rosbridge -> /cmd_vel_agent ─────┴> исполнитель -> /neurobot/cmd_vel_req (Twist)
  -> safety_gate — ЕДИНСТВЕННЫЙ писатель /cmd_vel -> twist_mux (cmd_vel 10, cmd_vel_tracker 20, cmd_vel_joy 100;
     таймауты 0,5 с) -> /diff_cont/cmd_vel_unstamped -> diff_drive_controller (cmd_vel_timeout 0,5 с) -> STM32
```

Что пакет гарантирует запуском и проверяет:
- safety_gate стартует после драйверов и только если в `/cmd_vel` никто не пишет, входы twist_mux главнее `/cmd_vel`
  никто не публикует (`/cmd_vel_tracker`, N13), в вход базы пишет только twist_mux и у `/scan` один издатель (в графе
  робота 02.06 было два `urg_node2`); предупреждения — twist_mux не слушает `/cmd_vel`, публикует геймпад;
- исполнитель стартует после READY safety_gate и только если тот слушает `/neurobot/cmd_vel_req`, а других
  писателей там нет; `install.sh` не ставит состав «исполнитель без safety_gate»;
- исполнитель v1 и распознавание legacy пишут абсолютное `/cmd_vel` сами, в сторону его уводит только ремап из
  env-файла. Поэтому он проверяется дважды: в строке команды (precheck: у исполнителя
  `-r /cmd_vel:=/neurobot/cmd_vel_req`, у распознавания — ремап в тему вне цепочки; как в rcl, действует первое
  правило для темы, следующие — нет) и в графе после READY (`wait-graph`: узел компонента не пишет в `/cmd_vel`, вход
  базы, входы twist_mux, распознавание — и во вход safety_gate; исполнитель пишет в `/neurobot/cmd_vel_req`). Ремап,
  потерянный не в env-файле (обёртка, launch-файл, свой разбор аргументов), ловит второе: `ExecStartPost` отказывает,
  systemd снимает процесс, и после рестарта safety_gate этот писатель не ведёт робот без него. Чтобы рестарт не
  запускал его снова, `wait-graph` оставляет метку в `StateDirectory` юнита (`/var/lib/neurobot/<компонент>/
  cmd_vel_writer.json`, хеш команды): пока `NEUROBOT_<КОМПОНЕНТ>_CMD` та же, precheck отказывает (`cmd_vel_writer`,
  пауза `NEUROBOT_PRECHECK_BACKOFF_S`); команда изменилась — метка снимается сама, причина была вне команды —
  её удаляет человек (RUNBOOK §4.4);
- явный ноль в `/cmd_vel` после любой остановки safety_gate, в том числе `kill -9` (ExecStopPost) — второй рубеж:
  первым базу тормозит `cmd_vel_timeout` diff_drive_controller (0,5 с по умолчанию в Foxy; на роботе не снят, RS8),
  ноль нужен на случай, если база держит команду;
- агентский путь не пишет скорости мимо исполнителя и не меняет настройки защиты: конфиг MCP-сервера (тема
  `/cmd_vel_agent`, пределы L3), rosbridge только на 127.0.0.1 и со списками разрешённого — `topics_glob` только тема
  агента, `services_glob` только `/rosapi/…`, `params_glob` без параметров; у PicoClaw выключен `exec` (L3,
  PLAN §13.3);
- аварийная защёлка: ставит кто угодно, снимает только человек в терминале (`neurobot estop`); рестарт safety_gate и
  перезагрузка её не снимают (`StateDirectory`, требование к W2 — «Аварийная защёлка»).

Отличия от пакета 1 (там страж v1 сам писал `/cmd_vel`): исполнитель (тот же страж v1) пишет в
`/neurobot/cmd_vel_req` ремапом в env-файле (`-r /cmd_vel:=/neurobot/cmd_vel_req`), ExecStopPost с явным нулём
перенесён в юнит safety_gate, проверка писателей `/cmd_vel` — в его precheck и в `wait-graph` исполнителя и
распознавания. Ремап остаётся обязательным и для исполнителя v2: узлу, который `/cmd_vel` не пишет, он ничего не
меняет. Агентский путь по-прежнему пишет в `/cmd_vel_agent`, вход исполнителя; задачи агента (`/neurobot/task`) —
исполнитель v2, пакет 2.

## Порядок запуска и супервизия (PLAN §5.1, §5.4)

Порядок — по готовности, а не по `sleep`: каждый следующий юнит `After=` всех предыдущих уровней, а «предыдущий
готов» значит READY (`Type=notify`) или успешную пробу `ExecStartPost`.

| # | Юнит | Type | Готовность | WatchdogSec | Restart / RestartSec | ExecStartPre (`neurobot precheck …`) | Прочее |
|---|---|---|---|---|---|---|---|
| 1 | `neurobot-driver@<имя>` (лидар, база, камера `realsense2_camera`, фильтр IMU) | exec | первое сообщение `NEUROBOT_DRIVER_<имя>_TOPIC` | — | always / 1 | команда задана; устройство на месте; камера на USB (`NEUROBOT_DRIVER_<имя>_USB`) | KillSignal=SIGINT |
| 2 | `neurobot-safety` | notify | READY после первого нуля | 3 | always / 500ms | команда; JSON-конфиг; модули интерпретатора (`numpy<2`, rclpy); **цепочка движения** (см. выше) | ExecStopPost: явный ноль (`neurobot zero`); TimeoutStopSec=20 — запуск rclpy для нуля на загруженной ВМ занимал до 14 с [замер]; StateDirectory=neurobot/safety — защёлка |
| 3 | `neurobot-executor` | notify | READY после первого нуля, затем `wait-graph executor` | 3 | always / 500ms | команда с ремапом `/cmd_vel:=/neurobot/cmd_vel_req`; JSON-конфиг; нет метки `wait-graph` при той же команде; safety_gate слушает `/neurobot/cmd_vel_req`, других писателей нет | TimeoutStartSec=60 (три запуска rclpy); StateDirectory=neurobot/executor — метка `wait-graph` |
| 4 | `neurobot-perception` | notify | READY после прогрева и первого кадра, затем `wait-graph perception` | 5 (живость цикла) | always / 1 | команда с ремапом `/cmd_vel` вне цепочки; нет метки `wait-graph` при той же команде; камера на USB (и USB 3); веса и sha256; нет чужих владельцев камеры (`predict_live.py`, `realsense2_camera_node` вне `neurobot-driver@` — при `--camera ros`, везде — при `--camera rs`; YOLO-воркер вне юнита); модули (`numpy<2`, torch, pyrealsense2, rclpy) | TimeoutStartSec=120, KillSignal=SIGINT, RestartPreventExitStatus=6 (`--camera rs`: камера занята); StateDirectory=neurobot/perception — метка `wait-graph` |
| 5 | `neurobot-rosbridge` | exec | WebSocket отвечает (`wait-port`) | — | always / 1 | порт свободен; `address:=127.0.0.1`; `topics_glob`, `services_glob`, `params_glob` — списки разрешённого (тема агента; `/rosapi/…`; `none`); каждый аргумент один раз | слушает только 127.0.0.1 — при `address:=127.0.0.1` [опыт, Jazzy] |
| 6 | `neurobot-labworker@<имя>` | exec | GET `NEUROBOT_LABWORKER_<имя>_URL` = 200 (`wait-http`) | — | always / 2 | порт свободен (второй экземпляр не стартует, N7); модули | TimeoutStartSec=180 (загрузка модели) |
| 7 | `neurobot-mcp` | exec | initialize + tools/list (`wait-mcp`) | — | always / 1 | порт свободен; конфиг MCP (HTTP на 127.0.0.1, `/cmd_vel_agent`, пределы L3) или env прокси; нет второго MCP-сервера или воркера вне юнитов; Python ≥ 3.10, `mcp` ≥ 1.8 | без ROS-окружения |
| 8 | `neurobot-picoclaw` | exec | «MCP server initialization complete» в журнале запуска (`wait-agent`, `+` — root) | — | always / 2 | порт шлюза свободен; конфиг PicoClaw по схеме установленной версии | «All MCP servers failed» → отказ и рестарт (PC5) |
| 9 | `neurobot-watchdog` | exec | — | — | always / 5 | — | root |

Общее для всех: `PartOf=neurobot.target` (stop/restart цели — для всех), `StartLimitIntervalSec=0` (непрерывный
режим: не сдаваться), `LogRateLimitIntervalSec/Burst` (шумный компонент не глушит журнал остальных),
`EnvironmentFile=/etc/neurobot/neurobot.env`. После `Restart=` проверка перед стартом при отказе ждёт
`NEUROBOT_PRECHECK_BACKOFF_S` — замена `RestartSteps=`, которого нет в systemd 245. Зависимости — только `After=`, без
`Requires=`: при отказе зависимости юнит с `Requires=` не запускается вовсе, и `Restart=` его не поднимет; с `After=`
он стартует, ждёт по пробе или отказывает в precheck и сам поправится, когда поднимется зависимость (doctor при этом
называет первый в порядке отказавший компонент). `After=` на отсутствующий в составе юнит ничего не ломает.

Совместимость с Ubuntu 20.04 (systemd 245): все директивы есть в v245 (тест по `verify/systemd245-directives.txt`,
список извлечён из `man/*.xml` тега v245; в ВМ — `verify/systemd245_verify.sh`: systemd-analyze из пакета focal);
`RestartSteps`/`RestartMaxDelaySec` (v254), `ExitType`, `OpenFile`, `journalctl --invocation` (v257) не используются;
журнал запуска выбирается полями `_SYSTEMD_INVOCATION_ID` и `INVOCATION_ID`. systemd 245 включает сторож юнита только
после READY — прогрев perception ограничен лишь `TimeoutStartSec`.

Отклонения от PLAN §5.1:
- сторож — долгоживущий сервис с периодом 10 с, а не `watchdog.timer`: на systemd 245 `LogLevelMax=` не глушит
  строки самого systemd, и таймер раз в 10 с дал бы ≈17 тыс. строк «Starting/Finished» в сутки плюс запуск Python
  каждые 10 с на Nano;
- проба MCP у сторожа — `initialize` + `tools/list`, а не `status`: `robot_get_status` лаборатории каждый раз
  вызывает `/rosapi/topics`, а вызовы сервисов rosbridge 1.3.1 в цикле текут (M11, #774). `tools/list` ловит и
  смерть, и зависание (синхронный инструмент FastMCP занимает цикл событий, M5). Для robot-mcp пакета 2 —
  `NEUROBOT_WATCHDOG_PROBE_TOOL=status`;
- юниты воркеров лаборатории (`neurobot-labworker@`) — по R7 (N7, N8): в плане их не было;
- safety_gate (`neurobot-safety`) — по PLAN §13.3: в §5.1 его ещё не было.

## env-файл

Один файл для юнитов (`EnvironmentFile=`), `install.sh` и CLI. Синтаксис — systemd: `ИМЯ=значение`, значение с
пробелами — в `"…"` (внутри `\"` — кавычка), комментарий — только целой строкой, подстановок `$VAR` нет. Команды
компонентов разбирает `neurobot-run` по правилам bash (кавычки работают). `User=`, группы и пути systemd не
раскрывает из окружения — их подставляет `install.sh` при установке (после правки — переустановить).

| Ключ | По умолчанию | Смысл |
|---|---|---|
| `NEUROBOT_USER`, `NEUROBOT_GROUP` | jetson | от кого работают компоненты (подставляется в юниты) |
| `NEUROBOT_SUPPLEMENTARY_GROUPS` | — | доп. группы: `dialout` (последовательные порты), `video plugdev` (камера); без входа в сеанс `uaccess` не действует |
| `NEUROBOT_PREFIX` | /opt/neurobot | куда ставятся `bin/`, `lib/`, `share/`, `src/` (исполнитель, safety_gate) |
| `NEUROBOT_COMPONENTS` | safety executor perception rosbridge mcp picoclaw | что включать (порядок всегда как в таблице выше); `executor` без `safety` не ставится |
| `NEUROBOT_DRIVERS` | — | имена экземпляров `neurobot-driver@<имя>` |
| `NEUROBOT_DRIVER_<имя>_CMD` | — | команда драйвера |
| `NEUROBOT_DRIVER_<имя>_DEVICE` | — | устройство для проверки перед стартом (`/dev/ttyACM0`) |
| `NEUROBOT_DRIVER_<имя>_TOPIC` | — | тема готовности драйвера (ждём первое сообщение) |
| `NEUROBOT_DRIVER_<имя>_USB` | — | id устройства на USB для проверки перед стартом (камера: `8086:0b3a`): нет — `camera_missing` у драйвера, а не тишина темы через `NEUROBOT_WAIT_S` |
| `NEUROBOT_LABWORKERS` | — | имена экземпляров `neurobot-labworker@<имя>` (HTTP-воркеры лаборатории) |
| `NEUROBOT_LABWORKER_<имя>_CMD` | — | команда воркера |
| `NEUROBOT_LABWORKER_<имя>_URL` | — | проба готовности: GET отвечает 200 (`/health` у YOLO-воркера, `/state` у 8766) |
| `NEUROBOT_LABWORKER_<имя>_MODULES` | — | модули интерпретатора воркера (синтаксис — как у `NEUROBOT_PERCEPTION_MODULES`) |
| `NEUROBOT_LABWORKER_<имя>_CAMERA` | — | `yes` — воркер сам открывает камеру: тогда без `perception` (у камеры один владелец, N12; проверяет install.sh) |
| `NEUROBOT_LABWORKER_WAIT_S` | 170 | сколько ждать готовности воркера (загрузка модели на Nano) |
| `NEUROBOT_ROS_SETUP` | /opt/ros/foxy/setup.bash | окружение ROS 2 |
| `NEUROBOT_ROS_OVERLAY` | — | setup.bash рабочего пространства (база лаборатории, пакет neurobot) |
| `ROS_DOMAIN_ID`, `ROS_LOCALHOST_ONLY` (Foxy), `ROS_AUTOMATIC_DISCOVERY_RANGE` (Jazzy), `RMW_IMPLEMENTATION` | — | DDS; CLI берёт их из файла, а не из терминала. Изоляция localhost по умолчанию выключена: RViz и геймпад лаборатории — на ПК (RS11) |
| `NEUROBOT_SAFETY_CMD` | — | safety_gate (`python3 -m neurobot.safety`, код — W2) |
| `NEUROBOT_SAFETY_MODULES` | numpy<2 rclpy | что должно находиться в интерпретаторе safety_gate (синтаксис — как у `NEUROBOT_PERCEPTION_MODULES`) |
| `NEUROBOT_SAFETY_NODE` | safety_gate | имя узла safety_gate (его писатель `/cmd_vel` — свой) |
| `NEUROBOT_EXECUTOR_CMD` | — | исполнитель; в пакете 1 — страж v1; ремап `--ros-args -r /cmd_vel:=/neurobot/cmd_vel_req` обязателен (precheck) |
| `NEUROBOT_EXECUTOR_NODE` | motion_executor | имя узла исполнителя (его писатель `/neurobot/cmd_vel_req` — свой) |
| `NEUROBOT_SAFETY_CONFIG`, `NEUROBOT_EXECUTOR_CONFIG`, `NEUROBOT_PERCEPTION_CONFIG` | — | JSON-конфиги (проверяется только разбор) |
| `NEUROBOT_PERCEPTION_CMD` | — | predict_live v2 `--mode legacy --camera ros`; ремап `/cmd_vel` в тему вне цепочки (`/cmd_vel_gesture`) обязателен (precheck); интерпретатор — абсолютный путь (N6). `--camera rs` (своя камера) — только без драйвера камеры |
| `NEUROBOT_PERCEPTION_NODE` | velocity_publisher | имя узла распознавания (из кода лаборатории): его писателей смотрит `wait-graph` |
| `NEUROBOT_PERCEPTION_MODULES` | numpy<2 torch snntorch pyrealsense2 cv2 rclpy | что должно находиться в интерпретаторе распознавания (в окружении ROS): «модуль», «модуль<версия», «модуль>=версия»; `python>=3.10` — версия самого Python. Ищется `find_spec`, без импорта |
| `NEUROBOT_ROSBRIDGE_CMD` | — | rosbridge: `address:=127.0.0.1`, `topics_glob:="'[/cmd_vel_agent]'"`, `services_glob:="'[/rosapi/topics]'"` и `params_glob:="'[none]'"` (все четыре обязательны и по одному разу, кавычки YAML у списков — тоже; в списках — только разрешённое, «Агентский путь»); у rosbridge 2.x (Jazzy) — по желанию `topics_pub_glob` (только тема агента) и `topics_sub_glob` |
| `NEUROBOT_ROSBRIDGE_PORT` | 9090 | порт rosbridge |
| `NEUROBOT_MCP_CMD` | — | агентский MCP-сервер (Python ≥ 3.10) |
| `NEUROBOT_MCP_MODULES` | python>=3.10 mcp>=1.8 | то же для MCP-сервера (ros_mcp_server ещё `websocket`) |
| `NEUROBOT_MCP_CONFIG` | — | JSON-конфиг ros_mcp_server (у прокси лаборатории конфига нет — пусто) |
| `NEUROBOT_MCP_URL` | http://127.0.0.1:18110/mcp | адрес MCP (порты робота 18110–18119); его же `merge` пишет в конфиг PicoClaw |
| `NEUROBOT_MCP_STATUS_TOOL` | robot_get_status | инструмент состояния, который один раз вызывает doctor (пусто — не вызывать) |
| `NEUROBOT_AGENT_MAX_SPEED`, `NEUROBOT_AGENT_MAX_TURN`, `NEUROBOT_AGENT_MAX_DURATION_S` | 0.15, 0.8, 1.0 | пределы инструментов движения агента (L3: м/с, рад/с, с) — с ними сверяются конфиг MCP-сервера и env прокси |
| `NEUROBOT_WATCHDOG_PROBE_TOOL` | — | инструмент для пробы сторожа (пусто — только tools/list) |
| `NEUROBOT_WATCHDOG_PERIOD_S` | 10 | период сторожа, с |
| `NEUROBOT_PICOCLAW_CMD` | — | `picoclaw --no-color gateway` |
| `NEUROBOT_PICOCLAW_PORT` | 18790 | порт шлюза PicoClaw (проверка «свободен») |
| `NEUROBOT_PICOCLAW_VERSION` | — | версия PicoClaw для проверки полей конфига; пусто — `<бинарник> version`, затем `build_info.version` |
| `PICOCLAW_HOME`, `PICOCLAW_CONFIG` | ~пользователя/.picoclaw | где лежит `config.json` PicoClaw (их же читает сам PicoClaw) |
| `NEUROBOT_CMD_VEL`, `NEUROBOT_CMD_VEL_TYPE` | /cmd_vel, geometry_msgs/msg/Twist | выход safety_gate (вход twist_mux) и его тип |
| `NEUROBOT_CMD_VEL_REQ` | /neurobot/cmd_vel_req | выход исполнителя, вход safety_gate |
| `NEUROBOT_AGENT_TOPIC` | /cmd_vel_agent | куда пишет агентский путь (вход исполнителя; сверяется с конфигом MCP и `topics_glob`) |
| `NEUROBOT_MUX_NODE` | twist_mux | узел twist_mux (пусто — стенд без него: `/cmd_vel` прямо в базу) |
| `NEUROBOT_BASE_CMD_TOPIC` | /diff_cont/cmd_vel_unstamped | вход базы (diff_drive_controller): пишет только twist_mux |
| `NEUROBOT_OVERRIDE_TOPICS` | /cmd_vel_tracker | входы twist_mux главнее `/cmd_vel`: любой их издатель перекрывает ноль safety_gate — отказ precheck и ошибка doctor (N13). Перенести тему в `NEUROBOT_JOY_TOPICS` — только предупреждать |
| `NEUROBOT_JOY_TOPICS` | /cmd_vel_joy | геймпад: законный перехват человеком — только предупреждение |
| `NEUROBOT_SCAN_TOPIC`, `NEUROBOT_SCAN_FRAME` | /scan, laser_frame | лидар и его frame_id (как у urg_node2 на роботе) |
| `NEUROBOT_SENSORS` | — | прочие датчики для doctor: «/тема:пакет/msg/Тип[:мин_Гц]» через пробел (`/odom:nav_msgs/msg/Odometry:10`); на роботе — `/odom`, `/tf`, `/joint_states`, `/camera/color/image_raw`, `/camera/depth/image_rect_raw`, `/camera/imu`, `/imu/data` |
| `NEUROBOT_HEALTH_TOPIC` | /neurobot/perception_health | здоровье распознавания |
| `NEUROBOT_MOTION_STATE_TOPIC` | /neurobot/motion_state | состояние исполнителя |
| `NEUROBOT_SAFETY_STATE_TOPIC` | /neurobot/safety_state | состояние safety_gate (`{state: ok\|slow\|blocked\|estop, reasons, min_clearance_m, limits}`, 5 Гц) |
| `NEUROBOT_ESTOP_TOPIC` | /neurobot/estop | команды аварийной защёлки |
| `NEUROBOT_CAMERA_USB` | 8086:0b3a | id камеры на USB (D435i; через запятую) |
| `NEUROBOT_SYSFS` | /sys | корень sysfs (стенд подставляет свой) |
| `NEUROBOT_WEIGHTS` | /etc/neurobot/weights.sha256 | манифест весов в формате sha256sum |
| `NEUROBOT_WAIT_S` | 30 | сколько ждать готовности порта, MCP, агента, темы драйвера, узла компонента в графе (`wait-graph`) |
| `NEUROBOT_WRITER_CHECK_S` | 0.75 | сколько собирать граф DDS при проверке писателей (0 — не проверять); узлы без имени (граф ещё собирается) досматриваются до 2 с сверху; doctor проверяет писателей ещё раз |
| `NEUROBOT_PRECHECK_BACKOFF_S` | 5 | пауза после неудачной проверки перед стартом |
| `NEUROBOT_FOREIGN_PERCEPTION` | predict_live.py live_stream_yolo.py realsense-viewer realsense2_camera_node yolo_hand_worker.py run_yolo_mcp_stack.sh | чужие владельцы камеры и CUDA: имя программы или файла-аргумента вне cgroup юнита (ручной запуск демо, узел камеры вне наших юнитов, YOLO-воркер вне юнита — второй контекст CUDA ≈1 ГиБ, N12). При `--camera ros` узел камеры в `neurobot-driver@` — свой, при `--camera rs` — чужой и там |
| `NEUROBOT_FOREIGN_MCP` | ros_mcp_server.py start_robot_mcp.sh robot_vision_mcp_proxy.py yolo_mcp_server.py run_yolo_mcp_stack.sh yolo_hand_worker.py | то же для MCP-серверов и воркеров (stdio-запуск из PicoClaw, `nohup`); воркеры в юнитах `neurobot-labworker@` не в счёт. Имя процесса воркера 8766 добавить, когда лаборатория пришлёт код |
| `NEUROBOT_ENV` | /etc/neurobot/neurobot.env | путь env-файла для CLI (юниты задают его сами) |

Прокси лаборатории (вариант Б агентского пути) читает свои ключи из того же файла: `ROBOT_VISION_MCP_PORT` (должен
совпадать с портом `NEUROBOT_MCP_URL`), `ROBOT_VISION_MAX_DURATION_S` (не больше `NEUROBOT_AGENT_MAX_DURATION_S`),
`ROBOT_VISION_MAX_SPEED` (единицы воркера неизвестны — не сравнивается).

## CLI `neurobot`

```
neurobot [--env ФАЙЛ] doctor [--json] [--no-ros]   всё ли поднялось; коды 0 ОК, 1 проблема, 2 проверка невозможна, 3 ещё поднимается
neurobot estop engage|release [--reason ТЕКСТ]     аварийная защёлка safety_gate; release — человеком в терминале
neurobot precheck <компонент>                      ExecStartPre; safety | executor | perception | rosbridge | mcp | picoclaw | driver@<имя> | labworker@<имя>
neurobot wait-port rosbridge | wait-mcp | wait-agent | wait-topic driver@<имя> | wait-http labworker@<имя>   ExecStartPost-пробы
neurobot wait-graph executor|perception            ExecStartPost: узел в графе и не пишет в /cmd_vel и прочие входы цепочки
neurobot zero                                      явный ноль в /cmd_vel (ExecStopPost safety_gate): код 1, если нет подписчика
neurobot watchdog [--once]                         сторож MCP и PicoClaw; --once — один проход (действия — в stderr)
neurobot start-test --cycles N --out КАТАЛОГ [--broken a,b] [--ready-timeout 180] [--stop-timeout 60] [--gesture-cmd КОМАНДА] [--deviation ТЕКСТ]
neurobot picoclaw-config check | merge [--config F] [--overlay F] [--request-timeout 20]
neurobot render --out КАТАЛОГ [--units КАТАЛОГ], neurobot env-get ИМЯ…   для install.sh
```

Причины отказа пишутся в stderr одной строкой `neurobot-<инструмент>: <компонент>: <код>: <текст>`; в журнале —
с приоритетом syslog (`<3>` ошибка, `<4>` предупреждение). doctor берёт последнюю такую строку с приоритетом ≤ 3
из журнала текущего запуска юнита, иначе — `Result` юнита; корневая причина — первый по порядку запуска
компонент с ошибкой. Коды и что делать — `neurobot_deploy/report.py` и `RUNBOOK.md` §4–5. stdout у CLI — только
для вывода doctor/render/env-get; stdout stdio-процессов не трогаем (PC2).

doctor проверяет:
- состояние юнитов и READY; юнит, который недавно отказал и снова запускается, — ошибка с результатом отказа
  (не стал готов, завис, упал), а не «запускается»;
- датчики (сразу после драйверов: без них safety_gate не пустит робота, и причина — в них): возраст `/scan` (≤ 0,5 с),
  число его издателей (два `urg_node2` — ошибка), `frame_id` (`NEUROBOT_SCAN_FRAME`, иначе предупреждение),
  закрытый передний сектор лидара (участок ±61° шире 10°, где луч негоден в ≥ 90 % сканов за 2,5 с, —
  предупреждение, RS3); темы `NEUROBOT_SENSORS` — издатель, тип, частота;
- safety_gate: `/neurobot/safety_state` (≥ 2 Гц при контракте 5 Гц; `estop` — ошибка с причинами, `blocked` —
  предупреждение с причинами), тип `/cmd_vel`, цепочка движения (как в precheck); геймпад — отдельная строка
  `twist_mux` с предупреждением;
- исполнитель: `/neurobot/motion_state` приходит, в `/neurobot/cmd_vel_req` пишет только он, safety_gate его слушает;
- распознавание: `/neurobot/perception_health` (`state` ok/paused; `starting` — ещё поднимается); вышло с кодом 6
  (`--camera rs`, камеру держит другой процесс) — `camera_busy`;
- соединения: WebSocket rosbridge, воркеры лаборатории (GET → 200), MCP (`initialize` + `tools/list` + один вызов
  состояния: видит ли сервер rosbridge или воркер; инструменты движения в разных единицах длительности — дубли
  прокси, N1, N2), PicoClaw (журнал запуска: «Connected to MCP server», нет «зомби», обрыва MCP, оглохшего канала,
  вызова > 60 с). Строки журнала с ключами (`key=`, `access_token=`, `eyJ…`) показываются с `<REDACTED>`.

Граф DDS собирается со временем, и имя только что запущенного узла приходит позже его конечных точек
(`_NODE_NAME_UNKNOWN_`): precheck и doctor досматривают такой снимок до 2 с, иначе свой узел сошёл бы за чужого
писателя [опыт, ВМ: precheck safety_gate сразу после старта базы назвал twist_mux безымянным писателем].

Без доступа к журналу (не root и не в группе adm/systemd-journal) doctor предупреждает и не ставит диагноз
PicoClaw. Если компоненты ROS запущены, а граф не снимается (не найден `NEUROBOT_ROS_SETUP`, сбой DDS), — ошибка
`ros_unavailable` сразу после драйверов: датчики, защёлка и писатели `/cmd_vel` не проверены, и «ОК» было бы
непроверенным (start_test засчитывал бы такой цикл). `--no-ros` — осознанный отказ от этих проверок. Журнал rcl в
файл CLI не пишет (`--disable-external-lib-logs`, есть и в Foxy), поэтому doctor, `estop` и start_test работают и без
`HOME` и `ROS_LOG_DIR` — из юнита, `systemd-run` или cron [опыт, ВМ: без флага doctor из `systemd-run` давал
`ros_unavailable` «Failed to get logging directory»].

Сторож (`neurobot-watchdog`), раз в `NEUROBOT_WATCHDOG_PERIOD_S`:
1. MCP не ответил на две пробы по 3 с — `systemctl restart neurobot-mcp`;
2. вызов MCP-инструмента в PicoClaw без завершения > 60 с — restart MCP-сервера: зависший вызов получает
   ошибку, следующие проходят (R3 «http hang + сторож»);
3. любой вызов > 90 с, «зомби», обрыв MCP без восстановления или «Long poll failed» не от остановки шлюза (канал VK
   после любой ошибки long poll больше не слушает, N10) — restart PicoClaw, только когда MCP отвечает и строке
   больше 20 с; юниты моложе 15 с не трогаются. LLM сторож не вызывает.

## Аварийная защёлка (PLAN §13.3)

`neurobot estop engage|release` публикует в `NEUROBOT_ESTOP_TOPIC` `std_msgs/String` с JSON
`{"cmd": "engage"|"release", "by": "human", "reason": …, "id": <uuid>, "t": <время>}` (QoS reliable +
transient_local, одна команда трижды с одним `id`) и ждёт подтверждения в `/neurobot/safety_state`
(`state` = `estop` после engage, ≠ `estop` после release):
- **engage** — без вопросов, кто угодно; никто не слушает тему — отказ с подсказкой «кнопка или
  `systemctl stop neurobot-safety`» (при остановке safety_gate шлёт ноль);
- **release** — только человек: stdin обязан быть терминалом, команда показывает причины защёлки и ждёт слово
  `release`. Агенту этот путь закрыт тем, что у него нет ни терминала, ни shell (`exec` выключен — отказ precheck
  picoclaw), ни темы защёлки (конфиг MCP-сервера и `topics_glob` rosbridge), ни сервисов и параметров
  (`services_glob`, `params_glob`). Узел ROS, опубликовавший release сам, ROS не отличит от человека — политика
  снятия (по `by`, отдельный вход и т. п.) — на стороне safety_gate (W2).

Защёлка — состояние safety_gate, а не команда в теме: издатель CLI живёт около 2 с, и `transient_local` отдаёт
команду поздним подписчикам, только пока издатель жив. Поэтому safety_gate хранит её сам: юнит даёт ему
`StateDirectory=neurobot/safety` (`$STATE_DIRECTORY` = `/var/lib/neurobot/safety`, есть в systemd 245; переживает
рестарт, перезагрузку и `install.sh --uninstall`). Иначе любой рестарт (зависание тика на 3 с, исключение, OOM) снял бы
защёлку без человека — а защёлки по наклону и пробуксовке (PLAN §13.3) стоят как раз тогда, когда процесс под
нагрузкой.

Что нужно от safety_gate (W2), чтобы юнит и doctor работали:
- имя узла `NEUROBOT_SAFETY_NODE`; READY после первого явного нуля в `/cmd_vel` и WATCHDOG от тика (sd_notify через
  `$NOTIFY_SOCKET`); подписка на `NEUROBOT_CMD_VEL_REQ` и `NEUROBOT_ESTOP_TOPIC` (формат выше);
  `/neurobot/safety_state` 5 Гц и при изменении;
- защёлка и её причины атомарно (временный файл, fsync, rename) пишутся в `$STATE_DIRECTORY/estop.json` до того, как
  safety_gate подтвердит engage (`state` = `estop`), и читаются до READY; файл есть, но не читается — старт с
  защёлкой; снимает её только release; не удалось записать — защёлка остаётся в памяти, строка в журнале;
- чужой писатель `/cmd_vel` (или входов twist_mux главнее него) во время работы — защёлка: precheck ловит его только
  при старте, `wait-graph` — только у исполнителя и распознавания;
- если защёлка станет сервисом (контракт это допускает), его имя не должно попадать под `services_glob` rosbridge
  (precheck откажет, если попадает `NEUROBOT_ESTOP_TOPIC`).

Подставной safety_gate (`fakes/fake_component.py safety`) реализует ровно это; сценарий `estop` стенда проверяет
защёлку после `kill -9`.

## Агентский путь

**Что на роботе на самом деле (R7).** В конфиге PicoClaw **0.2.8** робота включён один stdio-сервер
`robot_vision_demo_mcp` — прокси `robot_vision_mcp_proxy.py` к воркеру 8766, **кода которого в архиве нет**
(29.09 все вызовы — `Connection refused`). ros_mcp_server стоял там до 02.06, а присланная версия (704 строки) может
быть устаревшей (на роботе позже — 792 строки). Поэтому два варианта, выбор — в env-файле:
- **А (по умолчанию):** ros_mcp_server с патчем — единственный агентский путь, который мы можем направить в
  `/cmd_vel_agent` (тема берётся из его конфига), т. е. через исполнитель и safety_gate;
- **Б:** прокси лаборатории с патчем + её воркер под `neurobot-labworker@`. Куда воркер 8766 пишет скорость и
  открывает ли камеру — неизвестно до получения кода: если пишет `/cmd_vel` напрямую, safety_gate не стартует и
  doctor называет `cmd_vel_writer` (второй писатель мимо safety_gate), если открывает камеру — `CAMERA=yes` и без
  распознавания.

**Пределы L3.** Сырые скорости агенту недоступны: конфиг ros_mcp_server (`motion.max_linear_speed`,
`max_angular_speed`, `max_duration_ms`) не шире `NEUROBOT_AGENT_MAX_*`, запись открыта только в `/cmd_vel_agent`,
`safety.reject_unknown_topics` не выключен (precheck mcp). Второй рубеж — команда rosbridge, без него любой клиент
WebSocket (в том числе LLM, нашедший адрес) обходит MCP-сервер. precheck rosbridge требует четыре аргумента
`ros2 launch`, каждый ровно один раз (необязательные списки 2.x — тоже не больше раза): при повторе `ros2 launch` берёт
последнее значение, и дописанный в конец глоб открыл бы всё [опыт, Jazzy: из `topics_glob:="'[/cmd_vel_agent]'"
topics_glob:="'[*]'"` rosbridge получил `[*]`, и 20 из 20 публикаций чужого клиента дошли до `/cmd_vel`]. Глобы —
списки разрешённого: любой другой элемент (тема, сервис, маска) — отказ. Обратная косая черта в значении любого
`*_glob` — тоже отказ: launch_ros разбирает значение как YAML, и в строке в двойных кавычках `\x2c` становится
запятой — лишний элемент, которого precheck не видит [опыт проверки W1c, rosbridge 2.7.1: из
`services_glob:='"[/rosapi/topics\x2c/safety_gate/set_parameters]"'` rosbridge получил два элемента, и клиент поменял
`max_speed` safety_gate; тест `test_backslash_in_any_glob_is_refused`].
- `address:=127.0.0.1` — по умолчанию адрес пуст, и rosbridge слушает все интерфейсы: тема агента и `/rosapi/*`
  открыты сети лаборатории. Без него порт слушал `0.0.0.0` и `[::]`, с ним — только `127.0.0.1` [опыт, rosbridge
  2.7.1 Jazzy, `ss -ltn`; опыты этого абзаца — `experiments/results/neurobot_20261015_deploy_fix2/vm/
  rosbridge_address.*`];
- `topics_glob:="'[/cmd_vel_agent]'"` — только тема агента. Раздельных списков подписки и публикации в rosbridge 1.3.1
  (Foxy, робот) нет (тот же `topics_glob` проверяют и publish, и subscribe), поэтому тема из списка открыта клиенту и на
  запись: добавленные в список датчики safety_gate (`/scan`, глубина, IMU) или `/neurobot/*` (состояние, которое читают
  doctor и `neurobot estop`) можно было бы подменить. В `/cmd_vel`, `/neurobot/cmd_vel_req` и `/neurobot/estop`
  rosbridge с этим списком отвечает «No match found for topic, cancelling publish» [опыт, 15.10 — каталог
  `neurobot_20261015_deploy`]. В rosbridge 2.x (Jazzy, стенд) есть и раздельные `topics_pub_glob`, `topics_sub_glob`:
  они объединяются с `topics_glob` [по исходнику 2.7.1, `rosbridge_websocket`; опыт: с `topics_pub_glob:="'[*]'"` 20 из
  20 публикаций дошли до `/cmd_vel`], поэтому `topics_pub_glob` precheck держит тем же списком разрешённого, а
  `topics_sub_glob` (только подписка) пропускает любым; оба необязательны. Своего глоба для действий (actions) в
  launch-файле 2.x нет, и клиенту они открыты; в 1.3.1 действий нет вовсе, а в графе двойника action-серверов нет
  [опыт, 16.10: `ros2 action list` пуст];
- `services_glob:="'[/rosapi/topics]'"` — только `/rosapi/…`: их rosbridge при любом `services_glob` добавляет к
  списку сам (его зовёт `robot_get_status`), а прочие закрыты — `set_parameters` у safety_gate (пороги, пределы),
  diff_cont (`cmd_vel_timeout`, пределы скорости), twist_mux (приоритеты), драйверов датчиков (`/urg_node2/*`,
  `/camera/camera/*`: сузить скан, выключить глубину) и `/controller_manager/*`: «No match found for service»;
- `params_glob:="'[none]'"` — ни одного параметра, поэтому нужен и он: `/rosapi/set_param` меняет параметр любого
  узла, и ограничивает его только `params_glob`.
[опыт: rosbridge 2.7.1 Jazzy, ВМ, `experiments/results/neurobot_20261015_deploy_fix/vm/rosbridge_services.*`: без
`services_glob` клиент поменял `max_speed` узла safety_gate через `set_parameters`; с одним `services_glob` — через
`/rosapi/set_param`; с `params_glob` — ни так, ни так, а `/rosapi/topics` отвечает. В 1.3.1 Foxy все аргументы есть в
launch и `/rosapi/*` добавляется так же — по исходнику, на Foxy не проверено.]
Пустой список `[]` в rosbridge 1.3.1 — «без ограничений» (по исходнику; в 2.7.1 для сервисов — «только `/rosapi/*`»),
поэтому precheck требует непустые. Без кавычек YAML `ros2 launch` передаёт список массивом строк, и rosbridge падает на
старте (`InvalidParameterType`, [опыт]) — это тоже ловит precheck rosbridge.

Агентскому пути, которому нужно читать темы (датчики в ros_mcp_server, состояние `/neurobot/*` для robot-mcp
пакета 2), rosbridge 1.3.1 их не даст: расширять список значит открыть их и на запись. Читать их надо не через этот
rosbridge (например, своим узлом rclpy в MCP-сервере), а safety_gate не должен опираться на темы, открытые агенту
(требование к W2). Длительность толчка в секундах ограничена сверху и в прокси (патч), поэтому ошибка N1
«миллисекунды как секунды» безопасна.

### Патч к ros_mcp_server (`mcp/ros_mcp_server.patch`, 17 добавленных строк)

Наложен и проверен на присланной версии (`third_party/…/ros_mcp_server.py`, 704 строки). **Лаборатория говорит,
что версия может быть устаревшей**, поэтому патч описан и по смыслу — чтобы перенести на актуальную:

1. **Транспорт streamable HTTP вместо stdio** (PC1–PC3): секция `server` в `DEFAULT_CONFIG`
   (`{"host": "127.0.0.1", "port": 18110}`), `FastMCP("ros_mcp_server", **cfg["server"])`,
   `mcp.run(transport="streamable-http")`. Нужен `mcp` ≥ 1.8 (транспорт появился там). После перезапуска сервера
   PicoClaw переподключается сам (404 «session not found»; в 0.2.8 — тоже, PR #2664).
2. **Логи только в stderr** (PC2): `sys.stdout = sys.stderr` первой строкой `main()` — туда же уходят печать
   библиотек и журнал доступа uvicorn.
3. **Автостоп в finally/atexit/SIGTERM** (M6, S8): цикл толчка в `robot_nudge` обёрнут `try/finally` с
   `robot_stop`; `atexit.register(robot_stop, "exit")` в конце `make_server`; `signal.signal(SIGTERM, sys.exit)`
   — uvicorn после своей остановки пробрасывает SIGTERM прежнему обработчику, `sys.exit` запускает atexit.
4. **Тема агента — через конфиг**: правка кода не нужна, `topics.cmd_vel.name` = `/cmd_vel_agent`
   (`mcp/ros_mcp.json.example`). Исполнитель слушает эту тему; прямо в `/cmd_vel` агент не пишет (S4), это
   проверяет `neurobot precheck mcp`.

Камера и лидар в примере конфига выключены (M1, `read_one_message` нет). Остальные дефекты (новое соединение на
каждую публикацию, синхронные инструменты, толчки ≤ 0,044 м — M3–M8) патч не лечит: это пакет 2 (robot-mcp). M3
видно и на стенде: первый толчок после старта сервера не дошёл до исполнителя целиком в 1 из 3 прогонов и дошёл
частично (3 команды из ≈7) ещё в 1 — издатель rosbridge создаётся заново и не успевает найти подписчика, а инструмент
всё равно отвечает успехом [опыт, стенд «интеграция», ВМ под нагрузкой; робот при этом стоит].
Сервер требует Python ≥ 3.10 (M13): на роботе — отдельное окружение, например
`uv venv --python 3.10 /opt/ros-mcp/.venv && uv pip install --python /opt/ros-mcp/.venv/bin/python 'mcp>=1.8' websocket-client`.

### Патч к прокси лаборатории (`mcp/robot_vision_mcp_proxy.patch`, 13 добавленных строк)

По смыслу (переносится на любую версию прокси):
1. **Убраны первые четыре дубля** `robot_nudge_forward/backward`, `robot_turn_left/right(duration_ms=2000,
   speed=0.8)`. FastMCP при повторном имени оставляет **первую** регистрацию (mcp 1.2–1.30), а её тело передаёт
   миллисекунды позиционно в `duration_s`: без аргументов воркер получал `duration_s=2000` при `speed=0.8` (N1;
   воспроизведено тестом на подставном воркере). Остаются функции с `duration_s` — одна схема на все инструменты (N2).
2. **Пределы и в прокси**: `robot_nudge` ограничивает `duration_s` ≤ `ROBOT_VISION_MAX_DURATION_S` (0,5 с) и
   `speed` ≤ `ROBOT_VISION_MAX_SPEED` (0,25 — значение по умолчанию у самой лаборатории; единицы `speed` у воркера
   неизвестны — заглушки до ответа). Ограничение в самом воркере — за лабораторией.
3. **Streamable HTTP на 127.0.0.1** вместо stdio, порт `ROBOT_VISION_MCP_PORT` (18110).
4. **Стоп при выходе**: `atexit.register(robot_stop)` и SIGTERM → `sys.exit` — воркер получает `POST /motion/stop`.

Окружение прокси — Python ≥ 3.10 с `mcp` (у лаборатории — `~/NeuroChip/yolo_mcp_server/.venv`). Применить: `cd
<каталог прокси> && patch -p1 < <neurobot/deploy>/mcp/robot_vision_mcp_proxy.patch` (сначала `--dry-run`).

### Конфиг PicoClaw (`picoclaw/config.overlay.json`)

На роботе — **0.2.8**, на стенде — 0.3.1. Обе версии не стартуют, если в `config.json` есть поле, которого нет в их
структуре конфига (у 0.2.8 нет `max_llm_retries` и `tools.load_image`, N11). Поэтому `neurobot picoclaw-config merge`
определяет версию (`NEUROBOT_PICOCLAW_VERSION` → `picoclaw version` → `build_info.version` конфига), отбрасывает поля
оверлея, которых в ней нет (по `picoclaw/schema-<версия>.json`; для версии без схемы — только общие поля известных
версий), ставит адрес нашего сервера из `NEUROBOT_MCP_URL`, сохраняет копию `config.json.neurobot-<время>` и пишет
атомарно с прежними правами и владельцем; ключи (`.security.yml`) и каналы не трогает. Схемы сняты с исходников
обеих версий `verify/picoclaw_schema.go` (тем же обходом структуры, что делает PicoClaw).

| Ключ | Значение | Зачем |
|---|---|---|
| `tools.mcp.servers.robot_mcp` | `{"type": "http", "url": NEUROBOT_MCP_URL}` | HTTP вместо stdio (PC1–PC3); `type` `http`, а не `streamable-http` — второго 0.2.8 не знает; имя `robot_mcp` — как у лаборатории для ros_mcp_server с 02.06 (инструменты `mcp_robot_mcp_*`, N3) |
| `tools.mcp.servers.{robot, robot_vision_demo_mcp, yolo_hand_mcp}.enabled` | false | stdio-серверы лаборатории (если есть в конфиге): иначе два набора инструментов движения |
| `tools.mcp.discovery.enabled` | false | инструменты робота видны сразу |
| `gateway.log_level` | info | на `warn` (по умолчанию и у лаборатории) нет строк подключения MCP и вызовов — `wait-agent` не дождётся готовности, сторож слеп (N15) |
| `gateway.hot_reload` | false | PC3; после правки конфига — `systemctl restart neurobot-picoclaw`, не `/reload` |
| `agents.defaults.max_tokens` | 1024 | PC8 (у лаборатории 0 → 8192; в журнале — ошибки 400 `max_tokens`) |
| `model_list[].request_timeout` | 20 (с) | ставится каждой модели командой merge; PC8 |
| `agents.defaults.max_llm_retries` | 1 | PC8; только 0.3.1 (в 0.2.8 повторов 2, жёстко в коде) |
| `agents.defaults.max_tool_iterations` | 8 | у лаборатории 50: ходы на 29–32 итерации без `/stop` в 0.2.8 (N4) |
| `agents.defaults.max_parallel_turns` | 2 | зависший ход не глушит остальные чаты (PC4) |
| `tools.<имя>.enabled` | false для append_file, edit_file, write_file, list_dir, read_file, load_image (0.3.1), send_file, send_tts, spawn, spawn_status, subagent, exec, cron, skills, find_skills, install_skill, web, web_fetch, serial, spi, i2c | PC7; `exec` и `cron` — shell для LLM на роботе (агент ставил numpy 2 сам, N5; мог бы писать `/cmd_vel` и снимать защёлку) |

`check` — та же проверка, что в ExecStartPre. Отказ: неизвестные поля для установленной версии, `log_level` не
`info`/`debug`, нет HTTP-сервера `NEUROBOT_MCP_URL`, включённый stdio-сервер робота (по именам из оверлея или
скриптам `NEUROBOT_FOREIGN_MCP`), `streamable-http` на версии до 0.3.1, включённые `exec` или `cron` с командами.
Прочее — предупреждения.

Проверено на настоящих бинарниках linux-arm64 в ВМ (`experiments/results/neurobot_20261015_deploy/vm/
picoclaw_versions.*`): конфиг робота (0.2.8) — 5 отказов и 5 предупреждений; оверлей без подгонки под версию —
0.2.8 не стартует (`unknown field(s): agents.defaults.max_llm_retries, tools.load_image`); после `merge` `check`
пуст, и оба шлюза подключают MCP по HTTP («Connected to MCP server … server=robot_mcp», «MCP server initialization
complete»); без MCP-сервера оба пишут «All MCP servers failed to connect» — это «зомби», которого узнают `wait-agent`
и сторож.

## Проверка

На Mac (Python проекта и 3.8, как на роботе):

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider neurobot/tests/test_deploy_*.py
PY38=$(uv python find 3.8)
PYTHONDONTWRITEBYTECODE=1 $PY38 -m unittest discover -s neurobot/tests -p 'test_deploy_*.py'   # mcp_patch — пропуск
$PY38 -c "import glob; [compile(open(f, encoding='utf-8').read(), f, 'exec') for f in glob.glob('neurobot/deploy/**/*.py', recursive=True) + ['neurobot/deploy/bin/neurobot']]"
uvx --from shellcheck-py shellcheck -x neurobot/deploy/install.sh neurobot/deploy/bin/neurobot-run neurobot/deploy/fakes/setup_stand.sh neurobot/deploy/verify/systemd245_verify.sh
uvx ruff@0.17.0 check --no-cache --line-length 120 --select E,F,W neurobot/deploy neurobot/tests/test_deploy_*.py
```

`test_deploy_mcp_patch.py` запускает пропатченные серверы лаборатории (копии во временном каталоге) против подставных
rosbridge и воркера; нужны `mcp`, `websocket-client`, `websockets` в `.venv`.

На стенде (ВМ sim, Jazzy) — подставные компоненты (`fakes/fake_component.py`: safety_gate с защёлкой, исполнитель,
база с узлом twist_mux и датчиками, драйвер `/scan`, распознавание, rosbridge, воркер с «загрузкой модели», MCP,
строки журнала PicoClaw) в домене 77 или `--domain N` (ВМ общая: чужой издатель `/scan` или `/cmd_vel` в том же домене
precheck справедливо не пустит), те же юниты и doctor:

```bash
limactl shell --workdir / sim -- sudo bash "$PWD/neurobot/deploy/fakes/setup_stand.sh" --domain 41   # и --picoclaw <linux-arm64>
limactl shell --workdir / sim -- sudo neurobot start-test --cycles 100 --out /var/tmp/nb-start-test
limactl shell --workdir / sim -- sudo python3 /opt/neurobot/lib/fakes/scenarios.py --out /var/tmp/nb-scenarios.jsonl
limactl shell --workdir / sim -- sudo bash "$PWD/neurobot/deploy/verify/systemd245_verify.sh"
limactl shell --workdir / sim -- sudo bash "$PWD/neurobot/deploy/fakes/setup_stand.sh" --teardown
```

`scenarios.py` — то, чего нет в start_test: `systemd-analyze verify`, порядок, kill -9 и зависание safety_gate,
защёлка (engage, kill -9 safety_gate — защёлка на месте, отказ release без терминала, release в терминале), потерянный
ремап (исполнитель без ремапа — отказ precheck; ремап в команде есть, но узел его не применил — `wait-graph` снимает
исполнитель и распознавание, doctor называет `cmd_vel_writer`, метка не даёт precheck запустить их снова: процесс
стартует один раз; команду вернули — поднимаются сами), лишний издатель `/cmd_vel_tracker`, падение зависимости,
зависание MCP и вызова, kill -9 воркера во время «загрузки», оглохший канал. kill -9 — только живого главного
процесса юнита: у неактивного `MainPID=0`, и сценарий не проходит с причиной, а не убивает свою группу процессов.
Подставные исполнитель и распознавание, как настоящие, пишут `/cmd_vel` сами, и в сторону его уводит ремап из
env-файла стенда. С `--picoclaw` агентом работает настоящий PicoClaw (конфиг — `fakes/picoclaw.json` + `merge`).

Флаги стенда:
- `setup_stand.sh`: `--domain N` — домен DDS вместо 77 из `fake.env`; `--picoclaw БИНАРНИК` — агентом работает
  настоящий PicoClaw linux-arm64; `--teardown` — снять стенд (`install.sh --uninstall`, `/etc/neurobot`, `/var/lib/neurobot`);
- `scenarios.py [--out ФАЙЛ.jsonl] [сценарий …]` — по умолчанию все сценарии по порядку (список — в шапке файла);
- `fake_component.py <роль> [--ros-args …]` — роли safety, executor, perception, driver, base, publish, rosbridge,
  worker, mcp, picoclaw (что делает каждая — в шапке файла). `--warmup S` — прогрев распознавания до READY и
  «загрузка модели» воркера (по умолчанию 2 с); `--topic T` — тема драйвера или лишнего писателя (`/scan`); `--node N` —
  узел лишнего писателя (`cmd_vel_tracker_pub`); `--name N` — экземпляр воркера; `--tool-every S` — вызов инструмента
  PicoClaw раз в S с; `--channel-fail-after S` — «Long poll failed» через S с. Отказы: `--hang-after S` — перестать
  слать WATCHDOG и отвечать; `--hang-tools-after S` — MCP виснет только на `tools/call`; `--exit-after S` — выход с
  кодом 1; `--ignore-remap` — узел не применяет ремапы (потерянный ремап).

Стенд «интеграция» (настоящие исполнитель, rosbridge с `topics_glob`, ros_mcp_server с патчем и PicoClaw 0.2.8;
агентский путь до `/neurobot/cmd_vel_req` без LLM) и итоги прогонов — `experiments/results/neurobot_20261015_deploy/`;
прогоны после исправлений ревью W1 (ремап и `wait-graph`, `services_glob` и `params_glob`, защёлка в `StateDirectory`,
строгий счёт явного нуля) — `experiments/results/neurobot_20261015_deploy_fix/` (`manifest.json`, `README.md`); после
проверки W1 (повтор аргументов и адрес rosbridge, списки разрешённого и `topics_pub_glob` 2.x, метка `wait-graph`,
первое правило ремапа, CLI без `HOME`, подмены прерванного прогона) —
`experiments/results/neurobot_20261015_deploy_fix2/`; тесты, юниты (systemd 255 и 245) и PicoClaw 0.2.8 и 0.3.1 на
итоговом коде — `experiments/results/neurobot_20261015_deploy_fix3/`.

## start_test

`neurobot start-test` (root) — N холодных стартов «stop → reset-failed → start neurobot.target → doctor до «ОК» →
жест (по `--gesture-cmd`) → stop → явный ноль в журнале safety_gate», затем сценарии «сломанного старта» и ещё один
холодный старт. «ОК» doctor без снятого графа ROS не бывает (`ros_unavailable` — ошибка). Явный ноль засчитывается
только по коду `zero_sent` строки `neurobot zero` — нули ушли хотя бы одному подписчику; отказ (`ros_unavailable`) и
ноль без подписчиков (`zero_undelivered`) — провал цикла, строка пишется в `zero_line`. Прежний счётчик (до 15.10,
`experiments/results/neurobot_20261015_deploy/`) засчитывал любую строку `neurobot-zero:` и не отличал
доставленный ноль от недоставленного. Поломки обратимые и роботу не вредят: значения env-файла подменяются drop-in'ом в
`/run/systemd/system/<юнит>.d/` (исчезает при перезагрузке), занятый порт держит сам start_test. Подмену снимает конец
цикла, и при Ctrl+C, и при SIGTERM (`systemctl stop`, `timeout`); подмены прогона, убитого SIGKILL, start_test убирает
перед первым циклом (`scenarios.py` — так же) [опыт, ВМ: убитый посреди сценария `hang` прогон оставил safety_gate с
`--hang-after 3`, и следующий прогон шёл с ним]. По умолчанию — сценарии тех компонентов, что есть в составе.

| Сценарий | Как ломаем | doctor обязан назвать |
|---|---|---|
| `safety_down` | вместо safety_gate `sleep 3600`: READY нет, через 30 с отказ и рестарт; исполнитель без него не стартует | safety / `start_timeout` |
| `no_camera` | `NEUROBOT_CAMERA_USB=ffff:ffff` (кабель не выдёргиваем) | perception / `camera_missing` |
| `no_weights` | манифест, где первый файл весов — несуществующий путь | perception / `weights_missing` |
| `port_busy` | start_test слушает порт rosbridge | rosbridge / `port_busy` |
| `rosbridge_down` | вместо rosbridge `sh -c 'exec sleep 3600'` с теми же аргументами (проверка глобов проходит), ожидание порта 5 с | rosbridge / `port_timeout` |
| `worker_down` | вместо первого воркера лаборатории `sleep 3600`, ожидание 5 с | labworker@<имя> / `labworker_timeout` |

Значения подменяются в env-файле drop-in'а с экранированием (`config.quote`): systemd и bash читают их одинаково
[тест — против bash; с systemd 255 сверено опытом в ВМ (`systemd-run`, `EnvironmentFile=`), 245 не проверялся].
Итоги — `cycles.jsonl` (на цикл: прошёл ли, время до active цели по systemd и до «ОК» doctor, порядок готовности,
рестарты, явный ноль, что называл doctor), `summary.json` (p50/p95/max), `manifest.json` (версии, sha256 файлов
пакета и env-файла, параметры, отклонения). Время на стенде — ориентир; результат — счётчики пройденных циклов.

## Ограничения

- safety_gate ещё нет (W2): на стенде его роль играет подставной процесс, и всё, что касается решений
  безопасности (тормозной путь, глубина, IMU, слепой сектор), здесь не проверяется — только юнит, порядок, ноль при
  остановке, цепочка движения и защёлка как протокол.
- Проверено на Jazzy + systemd 255 (ВМ) и статически против директив systemd 245; на Foxy (rclpy, rosbridge 1.3.1,
  launch_ros) и на Jetson не запускалось — до HIL это [допущение].
- На цифровом двойнике пакет целиком поднимает профиль `neurobot/sim/suites/twin.env` (драйвер — `twin.launch.py`
  вместо драйверов лаборатории, свой домен DDS и порты; отличия от робота — README `neurobot/sim/suites` §2); итоги —
  `experiments/results/neurobot_20261016_safety/`. Имена тем датчиков и цепочки движения у двойника и в
  `neurobot.env.example` одни и те же.
- Профиль робота под безопасность v2: камерой владеет `realsense2_camera` (драйвер `camera`), распознавание читает
  его тему (`--camera ros`), глубина и IMU — датчики safety_gate, все четыре темы камеры и IMU — в `NEUROBOT_SENSORS`.
  Параметры launch камеры и фильтра IMU — по realsense-ros 4.x для Foxy [допущение: версия на роботе не известна,
  уточнить]. Подписка doctor на изображения 640×480 на Nano стоит CPU [оценка, не мерили]; пороги частот — с запасом
  вниз.
- `wait-graph` ловит писателя `/cmd_vel` у исполнителя и распознавания только после READY: в первом запуске с
  потерянным ремапом узел пишет мимо safety_gate до проверки графа — ≈1,2–1,5 с [замер, ВМ]. Дальше метка не даёт
  запустить его с той же командой (без неё цикл рестартов возобновлял обход в 37 % времени [опыт W1, ВМ]; с ней —
  один раз, `experiments/results/neurobot_20261015_deploy_fix2/vm/remap_loop.*`). Метку снимает смена команды или
  человек; ремап, потерянный после смены команды, снова даст одно окно.
