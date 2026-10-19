# neurobot/deploy — запуск, супервизия, аварийная защёлка и агентский путь (этапы Э2, Э2.5)

Пакет поднимает весь стек робота NeuroChip одной командой и объясняет, почему что-то не поднялось. Основание —
`docs/neurobot/PLAN.md` §4, §5, §13, `ANALYSIS.md` (M*, PC*, §5.2) и разбор материалов лаборатории от 14.10
(`research/R7_robotic_platform.md`: N*, `R7_robot_slides.md`: RS*). Инструкция для лаборатории — `RUNBOOK.md`.

Что здесь наше, а что готовое: юниты, CLI `neurobot` (doctor, проверки, защёлка, сторож, start_test), патчи и
конфиги — наша разработка. systemd, journald, logrotate, ROS 2, rosbridge_suite, twist_mux, ros2_control, PicoClaw,
MCP SDK (FastMCP) — готовые компоненты; `ros_mcp_server.py`, `robot_vision_mcp_proxy.py`, `predict_live.py`, воркеры
и мост к STM32 — код лаборатории. Сам safety_gate — `neurobot/safety` (свой README); здесь — его юнит, проверки
вокруг него и CLI защёлки.

## Состав

| Путь | Что |
|---|---|
| `units/` | шаблоны юнитов: `neurobot.target`, `neurobot-driver@.service`, `-safety`, `-executor`, `-perception`, `-rosbridge`, `neurobot-labworker@.service`, `-mcp`, `-picoclaw`, `-watchdog`, `-ui` (пульт оператора, по согласию лаборатории) |
| `neurobot.env.example` | единый env-файл робота (Foxy) → `/etc/neurobot/neurobot.env` |
| `install.sh` | установка / удаление: юниты из env-файла, CLI, исполнитель, safety_gate и пульт (`../executor`, `../safety`, `../ui`; с компонентом `ui` — и пользователь пульта), drop-in journald, logrotate; `--no-journald` — без drop-in journald и без перезапуска journald (стенд), `--no-enable` — юниты поставить, но не включать в автозапуск; `--help` |
| `bin/neurobot` | CLI (Python 3.8, только stdlib; rclpy — лениво) |
| `bin/neurobot-run` | обёртка ExecStart: окружение ROS 2 и команда из переменной env-файла, `exec` без смены PID |
| `launchfiles/supervised.launch.py` | обёртка `ros2 launch` для драйверов и rosbridge: смерть любого узла гасит launch, юнит перезапускает systemd (`install.sh` ставит в `<prefix>/share/launchfiles/`) |
| `neurobot_deploy/` | код CLI: `config` env-файл, `checks` проверки перед стартом, `probes` WebSocket, MCP и HTTP, `picolog` журнал PicoClaw, `picoclaw_cfg` версия, схема и оверлей PicoClaw, `doctor`, `estop` защёлка, `watchdog`, `start_test`, `rosgraph` (rclpy), `render`, `report` коды причин |
| `journald/50-neurobot.conf` | лимиты журнала, журнал на диске |
| `logrotate/neurobot-picoclaw` | ротация `~/.picoclaw/logs/gateway.log` (PicoClaw сам её не делает, N15) |
| `mcp/ros_mcp_server.patch`, `mcp/ros_mcp.json.example` | патч к присланному `ros_mcp_server.py` и его конфиг |
| `mcp/robot_vision_mcp_proxy.patch` | патч к прокси лаборатории `robot_vision_mcp_proxy.py` (дубли инструментов, пределы, HTTP) |
| `picoclaw/config.overlay.json` | оверлей конфига PicoClaw; `picoclaw/schema-<версия>.json` — поля config.json версий 0.2.8 и 0.3.1 |
| `weights.sha256.example` | манифест весов для проверки перед стартом |
| `fakes/` | подставные компоненты, профиль и сценарии стенда для проверки запуска без робота |
| `verify/` | директивы systemd v245 (Ubuntu 20.04), проверка юнитов systemd-analyze 245, генератор схем PicoClaw, тесты обёртки launch против launch 0.10 из Foxy (`foxy_launch.sh`); замеры на двойнике или роботе: `recovery.py` — kill -9 и SIGSTOP узлов внутри юнитов ros2 launch и время восстановления, `k6.py` — K6 «stop через MCP → ноль» и «препятствие → ноль после скана», `agent_truth.py` — ответы агента (`robot_nudge`, `robot_get_status`) против сдвига по `/odom`, `stand.py` — их общая часть (запись тем, MCP-сессия, квантили, сервисы мира двойника) |

## Цепочка движения (контракт двойника, PLAN §13.3)

```
жесты: perception (--mode legacy) -> /cmd_vel_gesture ┐
агент: MCP-сервер -> rosbridge -> /cmd_vel_agent ─────┴> исполнитель -> /neurobot/cmd_vel_req (Twist)
  -> safety_gate — ЕДИНСТВЕННЫЙ писатель /cmd_vel -> twist_mux (cmd_vel 10, cmd_vel_tracker 20, cmd_vel_joy 100;
     таймауты 0,5 с) -> /diff_cont/cmd_vel_unstamped -> diff_drive_controller (cmd_vel_timeout 0,5 с) -> STM32
стоп агента (robot_stop): MCP-сервер -> rosbridge -> /neurobot/stop_now -> safety_gate (ноль сразу, без защёлки)
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
  агента, стоп агента (`/neurobot/stop_now`: умеет только остановить, «Агентский путь») и копия состояния safety_gate
  для агента на Foxy, состояние safety_gate — только на чтение
  (`topics_sub_glob` rosbridge 2.x), `services_glob` только `/rosapi/…`, `params_glob` без параметров; у PicoClaw
  выключен `exec` (L3, PLAN §13.3);
- аварийная защёлка: ставит кто угодно, снимает только человек в терминале через sudo (`sudo neurobot estop
  release`: запрос — файл root, а не команда в теме); рестарт safety_gate и перезагрузка её не снимают
  (`StateDirectory`, требование к W2 — «Аварийная защёлка»);
- смерть узла внутри `ros2 launch` (драйверы, rosbridge) гасит юнит, и systemd его поднимает — обёртка
  `launchfiles/supervised.launch.py`; зависший узел (жив, но молчит) — сторож по свежести тем и ping rosbridge.

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
| 1 | `neurobot-driver@<имя>` (лидар, база, камера `realsense2_camera`, фильтр IMU) | exec | первое сообщение `NEUROBOT_DRIVER_<имя>_TOPIC` | — (сторож: свежесть `NEUROBOT_DRIVER_<имя>_FRESH`) | always / 1 | команда задана; `ros2 launch` — через обёртку (иначе предупреждение `unsupervised_launch`); устройство на месте; камера на USB (`NEUROBOT_DRIVER_<имя>_USB`) | KillSignal=SIGINT; смерть узла — выход launch (обёртка) |
| 2 | `neurobot-safety` | notify | READY после первого нуля | 3 | always / 500ms | команда; JSON-конфиг; модули интерпретатора (`numpy<2`, rclpy); **цепочка движения** (см. выше) | ExecStopPost: явный ноль (`neurobot zero`); TimeoutStopSec=20 — запуск rclpy для нуля на загруженной ВМ занимал до 14 с [замер]; StateDirectory=neurobot/safety — защёлка |
| 3 | `neurobot-executor` | notify | READY после первого нуля, затем `wait-graph executor` | 3 | always / 500ms | команда с ремапом `/cmd_vel:=/neurobot/cmd_vel_req`; JSON-конфиг; нет метки `wait-graph` при той же команде; safety_gate слушает `/neurobot/cmd_vel_req`, других писателей нет | TimeoutStartSec=60 (три запуска rclpy); StateDirectory=neurobot/executor — метка `wait-graph` |
| 4 | `neurobot-perception` | notify | READY после прогрева и первого кадра, затем `wait-graph perception` | 5 (живость цикла) | always / 1 | команда с ремапом `/cmd_vel` вне цепочки; нет метки `wait-graph` при той же команде; камера на USB (и USB 3); веса и sha256; нет чужих владельцев камеры (`predict_live.py`, `realsense2_camera_node` вне `neurobot-driver@` — при `--camera ros`, везде — при `--camera rs`; YOLO-воркер вне юнита); модули (`numpy<2`, torch, pyrealsense2, rclpy) | TimeoutStartSec=120, KillSignal=SIGINT, RestartPreventExitStatus=6 (`--camera rs`: камера занята); StateDirectory=neurobot/perception — метка `wait-graph` |
| 5 | `neurobot-rosbridge` | exec | WebSocket отвечает (`wait-port`) | — (сторож: ping по WebSocket) | always / 1 | порт свободен; `address:=127.0.0.1`; `topics_glob`, `services_glob`, `params_glob` — списки разрешённого (тема агента, стоп агента и копия состояния для агента; `/rosapi/…`; `none`); каждый аргумент один раз; `ros2 launch` — через обёртку | слушает только 127.0.0.1 — при `address:=127.0.0.1` [опыт, Jazzy]; смерть узла `rosbridge_websocket` — выход launch |
| 6 | `neurobot-labworker@<имя>` | exec | GET `NEUROBOT_LABWORKER_<имя>_URL` = 200 (`wait-http`) | — | always / 2 | порт свободен (второй экземпляр не стартует, N7); модули | TimeoutStartSec=180 (загрузка модели) |
| 7 | `neurobot-mcp` | exec | initialize + tools/list (`wait-mcp`) | — | always / 1 | порт свободен; конфиг MCP (HTTP на 127.0.0.1, `/cmd_vel_agent`, пределы L3) или env прокси; нет второго MCP-сервера или воркера вне юнитов; Python ≥ 3.10, `mcp` ≥ 1.8 | без ROS-окружения |
| 8 | `neurobot-picoclaw` | exec | «MCP server initialization complete» в журнале запуска (`wait-agent`, `+` — root) | — | always / 2 | порт шлюза свободен; конфиг PicoClaw по схеме установленной версии | «All MCP servers failed» → отказ и рестарт (PC5) |
| 9 | `neurobot-watchdog` | exec | — | — | always / 5 | — | root; окружение ROS (узел rclpy для свежести тем драйверов); без `After=` — стартует вместе с драйверами: иначе ждал бы весь агентский путь, а зависший драйвер камеры (из-за него не поднимается распознавание) некому было бы перезапустить [опыт, ВМ] |
| 10 | `neurobot-ui` (компонент `ui`, по умолчанию не в составе) | notify | READY после узла и порта, затем `wait-graph ui` | 10 | always / 3, код 2 — без рестарта; 5 запусков за 300 с — сдаётся | команда; темы стопа и защёлки (`NEUROBOT_STOP_TOPIC`, `NEUROBOT_ESTOP_TOPIC`) — те, что в коде пульта; свой пользователь (`NEUROBOT_UI_USER` ≠ `NEUROBOT_USER`, не root); узел не `neurobot_*`; порт свободен и не чужой; `StateDirectory` 0700; источник — робот или двойник; rclpy | только `After=neurobot-safety`; отказ и «сдался» — предупреждение doctor; `StateDirectory=neurobot/ui` (токен, коды входа, сессии, аудит), `MemoryMax=128M` (шлюз и doctor по кнопке в одной cgroup — числа в README пульта [замер, двойник]), `MALLOC_ARENA_MAX=2`, `MALLOC_TRIM_THRESHOLD_=131072`, `Slice=neurobot-ui.slice`, `CPUWeight=20`, `Nice=10`, `NoNewPrivileges`, `ProtectSystem=full` |

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

**Смерть и зависание узла внутри `ros2 launch`.** `ros2 launch` переживает смерть своего узла: юнит active, а узла нет,
и `Restart=always` не срабатывает [замер main_a1: kill -9 узла `rosbridge_websocket` — 9 реплик агента без ответа до
конца прогона]. Поэтому драйверы и rosbridge запускаются через обёртку `launchfiles/supervised.launch.py`:
`ros2 launch <prefix>/share/launchfiles/supervised.launch.py include:=пакет/файл [oneshot:=a,b] [аргументы…]`.
Обёртка включает launch-файл компонента (аргументы командной строки видит и он) и на выход любого процесса отвечает
Shutdown: launch гасит остальные узлы и выходит, systemd поднимает юнит заново (`RestartSec=1`, precheck, проба
готовности). `respawn` у `ExecuteProcess` в Foxy тоже есть (launch 0.10.10), но его пришлось бы прописывать в каждом
launch-файле лаборатории, а перезапуск одного узла не повторяет порядок запуска (spawner контроллеров, параметры);
обёртка ничего в их файлах не меняет. Исключение — одноразовые процессы, которые выходят сами (`oneshot`, по умолчанию `spawner`,
`spawner.py`, `create`): их выход с кодом 0 не гасит launch, с ошибкой — гасит. Своя остановка юнита (SIGINT) — не
повод. Используется только API пакета launch, которое есть в Foxy (launch 0.10: `OnProcessExit` без
`target_action`, `Shutdown`, `IncludeLaunchDescription`, `AnyLaunchDescriptionSource`) [опыт: `verify/foxy_launch.sh` —
launch 0.10.10 ветки foxy, Python 3.8, тест `test_deploy_launch.py`; на Jazzy — тот же тест и юниты двойника; на самом
роботе — не запускалось]. Зависший узел (SIGSTOP, дедлок, оглохший поток USB) launch не видит — его ловит сторож
(ниже): темы драйвера `NEUROBOT_DRIVER_<имя>_FRESH` молчат дольше предела две пробы подряд — `restart` юнита.

Отклонения от PLAN §5.1:
- сторож — долгоживущий сервис, а не `watchdog.timer`: на systemd 245 `LogLevelMax=` не глушит строки самого
  systemd, и таймер раз в 10 с дал бы ≈17 тыс. строк «Starting/Finished» в сутки плюс запуск Python каждые 10 с на
  Nano;
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
| `NEUROBOT_DRIVER_<имя>_FRESH` | тема `TOPIC` с `NEUROBOT_WATCHDOG_STALE_S` | свежесть для сторожа: «/тема:возраст_с» через пробел (камера: `/camera/color/image_raw:2`); тема старше предела две пробы подряд — `restart` драйвера; `none` — не смотреть |
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
| `NEUROBOT_SAFETY_CMD` | — | safety_gate (`python3 -m neurobot.safety`); на Foxy — с `-p agent_state_topic:=` (копия состояния для агента, см. `NEUROBOT_AGENT_STATE_TOPIC`) |
| `NEUROBOT_SAFETY_MODULES` | numpy<2 rclpy | что должно находиться в интерпретаторе safety_gate (синтаксис — как у `NEUROBOT_PERCEPTION_MODULES`) |
| `NEUROBOT_SAFETY_NODE` | safety_gate | имя узла safety_gate (его писатель `/cmd_vel` — свой) |
| `NEUROBOT_EXECUTOR_CMD` | — | исполнитель; в пакете 1 — страж v1; ремап `--ros-args -r /cmd_vel:=/neurobot/cmd_vel_req` обязателен (precheck) |
| `NEUROBOT_EXECUTOR_NODE` | motion_executor | имя узла исполнителя (его писатель `/neurobot/cmd_vel_req` — свой) |
| `NEUROBOT_SAFETY_CONFIG`, `NEUROBOT_EXECUTOR_CONFIG`, `NEUROBOT_PERCEPTION_CONFIG` | — | JSON-конфиги (проверяется только разбор) |
| `NEUROBOT_PERCEPTION_CMD` | — | predict_live v2 `--mode legacy --camera ros`; ремап `/cmd_vel` в тему вне цепочки (`/cmd_vel_gesture`) обязателен (precheck); интерпретатор — абсолютный путь (N6). `--camera rs` (своя камера) — только без драйвера камеры |
| `NEUROBOT_PERCEPTION_NODE` | velocity_publisher | имя узла распознавания (из кода лаборатории): его писателей смотрит `wait-graph` |
| `NEUROBOT_PERCEPTION_MODULES` | numpy<2 torch snntorch pyrealsense2 cv2 rclpy | что должно находиться в интерпретаторе распознавания (в окружении ROS): «модуль», «модуль<версия», «модуль>=версия»; `python>=3.10` — версия самого Python. Ищется `find_spec`, без импорта |
| `NEUROBOT_ROSBRIDGE_CMD` | — | rosbridge через обёртку (`supervised.launch.py include:=rosbridge_server/rosbridge_websocket_launch.xml`): `address:=127.0.0.1`, `topics_glob:="'[/cmd_vel_agent,/neurobot/stop_now]'"` (тема агента и стоп агента; на Foxy — ещё копия состояния для агента), `services_glob:="'[/rosapi/topics]'"` и `params_glob:="'[none]'"` (все четыре обязательны и по одному разу, кавычки YAML у списков — тоже; в списках — только разрешённое, «Агентский путь»); у rosbridge 2.x (Jazzy) — по желанию `topics_pub_glob` (только темы агента) и `topics_sub_glob` (только чтение: `/neurobot/safety_state` для MCP-сервера) |
| `NEUROBOT_ROSBRIDGE_PORT` | 9090 | порт rosbridge |
| `NEUROBOT_MCP_CMD` | — | агентский MCP-сервер (Python ≥ 3.10) |
| `NEUROBOT_MCP_MODULES` | python>=3.10 mcp>=1.8 | то же для MCP-сервера (ros_mcp_server ещё `websocket`) |
| `NEUROBOT_MCP_CONFIG` | — | JSON-конфиг ros_mcp_server (у прокси лаборатории конфига нет — пусто) |
| `NEUROBOT_MCP_URL` | http://127.0.0.1:18110/mcp | адрес MCP (порты робота 18110–18119); его же `merge` пишет в конфиг PicoClaw |
| `NEUROBOT_MCP_STATUS_TOOL` | robot_get_status | инструмент состояния, который один раз вызывает doctor (пусто — не вызывать) |
| `NEUROBOT_AGENT_MAX_SPEED`, `NEUROBOT_AGENT_MAX_TURN`, `NEUROBOT_AGENT_MAX_DURATION_S` | 0.15, 0.8, 1.0 | пределы инструментов движения агента (L3: м/с, рад/с, с) — с ними сверяются конфиг MCP-сервера и env прокси |
| `NEUROBOT_WATCHDOG_PROBE_TOOL` | — | инструмент для пробы сторожа (пусто — только tools/list) |
| `NEUROBOT_WATCHDOG_PERIOD_S` | 10 | период проб MCP и журнала PicoClaw, с |
| `NEUROBOT_WATCHDOG_FAST_S` | 1 | период частых проб сторожа: свежесть тем драйверов и ping rosbridge, с |
| `NEUROBOT_WATCHDOG_STALE_S` | 2 | предельный возраст темы готовности драйвера для сторожа, если своего `FRESH` нет, с [допущение до замера на Nano] |
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
| `NEUROBOT_ESTOP_TOPIC` | /neurobot/estop | команды аварийной защёлки (engage; release по теме safety_gate не принимает) |
| `NEUROBOT_STOP_TOPIC` | /neurobot/stop_now | стоп агента (`robot_stop` через MCP, решение 1а 19.10): safety_gate ставит ноль сразу, без S-кривой и без защёлки, дальше — с нулевого запроса. Тема — в `topics_glob` rosbridge и в `topics.stop_now` конфига MCP (иначе предупреждение precheck mcp: `robot_stop` — штатная плавная остановка, ≈0,5 с); `""` — стопа агента нет |
| `NEUROBOT_ESTOP_RELEASE_DIR` | /run/neurobot-estop | каталог root для запроса снятия защёлки (`sudo neurobot estop release`); тот же, что `release_dir` safety_gate |
| `NEUROBOT_AGENT_STATE_TOPIC` | — | копия `/neurobot/safety_state` для агента (`agent_state_topic` safety_gate): rosbridge 1.3.1 (Foxy) не умеет открыть тему только на чтение, а сам `/neurobot/safety_state` читают исполнитель, doctor и `neurobot estop`; копию читает только MCP-сервер — подменить её агент может лишь себе. На Jazzy не нужна (`topics_sub_glob`) |
| `NEUROBOT_CAMERA_USB` | 8086:0b3a | id камеры на USB (D435i; через запятую) |
| `NEUROBOT_SYSFS` | /sys | корень sysfs (стенд подставляет свой) |
| `NEUROBOT_WEIGHTS` | /etc/neurobot/weights.sha256 | манифест весов в формате sha256sum |
| `NEUROBOT_WAIT_S` | 30 | сколько ждать готовности порта, MCP, агента, темы драйвера, узла компонента в графе (`wait-graph`) |
| `NEUROBOT_WRITER_CHECK_S` | 0.75 | сколько собирать граф DDS при проверке писателей (0 — не проверять); узлы без имени (граф ещё собирается) досматриваются до 2 с сверху; doctor проверяет писателей ещё раз |
| `NEUROBOT_PRECHECK_BACKOFF_S` | 5 | пауза после неудачной проверки перед стартом |
| `NEUROBOT_FOREIGN_PERCEPTION` | predict_live.py live_stream_yolo.py realsense-viewer realsense2_camera_node yolo_hand_worker.py run_yolo_mcp_stack.sh | чужие владельцы камеры и CUDA: имя программы или файла-аргумента вне cgroup юнита (ручной запуск демо, узел камеры вне наших юнитов, YOLO-воркер вне юнита — второй контекст CUDA ≈1 ГиБ, N12). При `--camera ros` узел камеры в `neurobot-driver@` — свой, при `--camera rs` — чужой и там |
| `NEUROBOT_FOREIGN_MCP` | ros_mcp_server.py start_robot_mcp.sh robot_vision_mcp_proxy.py yolo_mcp_server.py run_yolo_mcp_stack.sh yolo_hand_worker.py | то же для MCP-серверов и воркеров (stdio-запуск из PicoClaw, `nohup`); воркеры в юнитах `neurobot-labworker@` не в счёт. Имя процесса воркера 8766 добавить, когда лаборатория пришлёт код |
| `NEUROBOT_ENV` | /etc/neurobot/neurobot.env | путь env-файла для CLI (юниты задают его сами) |
| `NEUROBOT_UI_CMD` | — | пульт оператора: `/usr/bin/python3 -m neurobot.ui live` (аргументы ROS пульт не принимает) |
| `NEUROBOT_UI_USER` | neurobot-ui | пользователь пульта (создаёт install.sh): не `NEUROBOT_USER` и не root — агент не прочитает токен входа |
| `NEUROBOT_UI_NODE` | ui_gateway | имя узла пульта (не `neurobot_*`: так зовутся служебные узлы CLI, их проверки писателей пропускают) |
| `NEUROBOT_UI_PORT` | 18120 | порт пульта на 127.0.0.1 (вне EdgeCtx 18080–18089, MCP 18110–18119, PicoClaw 18790) |
| `NEUROBOT_UI_MODULES` | rclpy | что должно находиться в интерпретаторе пульта |
| `NEUROBOT_UI_SOURCE` | робот | подпись источника на каждом экране: робот \| двойник |
| `NEUROBOT_UI_STATE_DIR` | /var/lib/neurobot/ui | каталог токена, кодов входа и аудита (= `StateDirectory` юнита); его читает `neurobot ui-url` |
| `NEUROBOT_UI_BUNDLE` | — | бандл «Записи» рядом с живым режимом (переключатель в пульте); пусто — только живой |
| `NEUROBOT_UI_DOCTOR_S` | 60 | doctor по кнопке пульта не чаще раза в столько секунд; 0 — выключен (замеры) |
| `NEUROBOT_UI_NOT_ROBOT` | — | компоненты не робота (двойник: `driver@twin`) — в память стека против бюджета не идут |

Прокси лаборатории (вариант Б агентского пути) читает свои ключи из того же файла: `ROBOT_VISION_MCP_PORT` (должен
совпадать с портом `NEUROBOT_MCP_URL`), `ROBOT_VISION_MAX_DURATION_S` (не больше `NEUROBOT_AGENT_MAX_DURATION_S`),
`ROBOT_VISION_MAX_SPEED` (единицы воркера неизвестны — не сравнивается).

## CLI `neurobot`

```
neurobot [--env ФАЙЛ] doctor [--json] [--no-ros]   всё ли поднялось; коды 0 ОК, 1 проблема, 2 проверка невозможна, 3 ещё поднимается
neurobot estop engage|release [--reason ТЕКСТ]     аварийная защёлка safety_gate; release — человеком в терминале, sudo
neurobot precheck <компонент>                      ExecStartPre; safety | executor | perception | rosbridge | mcp | picoclaw | driver@<имя> | labworker@<имя>
neurobot wait-port rosbridge | wait-mcp | wait-agent | wait-topic driver@<имя> | wait-http labworker@<имя>   ExecStartPost-пробы
neurobot wait-graph executor|perception            ExecStartPost: узел в графе и не пишет в /cmd_vel и прочие входы цепочки
neurobot wait-graph ui                             ExecStartPost пульта: узел в графе и пишет только /neurobot/stop_now и /neurobot/estop
neurobot ui-url [--ttl S]                          одноразовая ссылка входа в пульт (root; 120 с)
neurobot zero                                      явный ноль в /cmd_vel (ExecStopPost safety_gate): код 1, если нет подписчика
neurobot watchdog [--once]                         сторож: драйверы и rosbridge (часто), MCP и PicoClaw (период); --once — один проход MCP и PicoClaw
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
  (не стал готов, завис, упал), а не «запускается»; пока какой-нибудь юнит ещё поднимается, молчащий или пропавший
  датчик (`sensor_stale`, `sensor_missing`, `scan_stale`) — «ещё поднимается», а не ошибка: глубину и камеру драйвер
  может отдавать позже (у двойника — только при подписчике), и ошибка датчика заслонила бы настоящую первую причину
  [находка приёмки 17.10: на старте doctor называл `sensor_stale` глубины вместо отказа safety_gate; замер 18.10,
  двойник, start_test: в 21 холодном старте ложных причин 0 (16.10 — в 99 из 101), в 5 сломанных названа только своя
  причина (16.10 — сначала `sensor_stale` во всех 5)]. Датчик, замолчавший при работающем драйвере, пока другой юнит
  ещё поднимается, doctor назовёт, когда тот поднимется или откажет (не позже его TimeoutStartSec);
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
  вызова > 60 с). Строки журнала с ключами (`key=`, `access_token=`, `eyJ…`) показываются с `<REDACTED>`;
- пульт оператора (если в составе): `/healthz` на 127.0.0.1 и издатели его узла по графу — только стоп и защёлка
  (`ui_publisher` — ошибка); не отвечает или запускается — предупреждение `ui_down`: робот работает и без пульта.

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

Сторож (`neurobot-watchdog`, root, с окружением ROS) — то, что «живо, но не работает», чего не видят systemd и обёртка
launch. Раз в `NEUROBOT_WATCHDOG_FAST_S` (1 с):
- свежесть тем драйверов (`NEUROBOT_DRIVER_<имя>_FRESH`) в одном постоянном узле rclpy, подписка raw (кадры камеры
  не разбираются): тема старше предела две пробы подряд — `systemctl restart --no-block` драйвера (SIGSTOP узла, дедлок,
  оглохший поток USB); если молчат темы всех драйверов сразу, никого не трогает — так выглядит беда DDS или самого
  сторожа, а не отказ одного драйвера; драйвер моложе 5 с не трогает (его active и так значит «первое сообщение»);
- rosbridge: одно постоянное соединение WebSocket и ping, pong за 1 с; две неудачи подряд (нет соединения, нет pong) —
  `restart --no-block` rosbridge. Соединение одно, а не рукопожатие на каждую пробу: rosbridge пишет в журнал каждое
  подключение клиента. На ping самого сервера (`websocket_ping_interval` rosbridge) сторож отвечает pong, иначе
  сервер закрыл бы соединение по своему таймауту.

Раз в `NEUROBOT_WATCHDOG_PERIOD_S` (10 с):
1. MCP не ответил на две пробы по 3 с — `systemctl restart neurobot-mcp`;
2. вызов MCP-инструмента в PicoClaw без завершения > 60 с — restart MCP-сервера: зависший вызов получает
   ошибку, следующие проходят (R3 «http hang + сторож»);
3. любой вызов > 90 с, «зомби», обрыв MCP без восстановления или «Long poll failed» не от остановки шлюза (канал VK
   после любой ошибки long poll больше не слушает, N10) — restart PicoClaw, только когда MCP отвечает и строке
   больше 20 с; юниты моложе 15 с не трогаются. LLM сторож не вызывает.

Рестарты в цикле — `--no-block`: частые пробы драйверов не ждут, пока поднимается MCP-сервер или PicoClaw
(`--once` ждёт).

## Аварийная защёлка (PLAN §13.3)

- **engage** — `neurobot estop engage` публикует в `NEUROBOT_ESTOP_TOPIC` `std_msgs/String` с JSON
  `{"cmd": "engage", "by": "human", "reason": …, "id": <uuid>, "t": <время>}` (QoS reliable + transient_local, одна
  команда трижды с одним `id`) и ждёт `state` = `estop` в `/neurobot/safety_state`. Без вопросов, кто угодно; никто не
  слушает тему — отказ с подсказкой «кнопка или `systemctl stop neurobot-safety`» (при остановке safety_gate шлёт
  ноль);
- **release** — только человек и только root: `sudo neurobot estop release`. stdin обязан быть терминалом, команда
  показывает причины защёлки и ждёт слово `release`, затем пишет запрос снятия `release.json` в каталог
  `NEUROBOT_ESTOP_RELEASE_DIR` (`/run/neurobot-estop`: владелец root, права 755, файл 644, атомарно) — в нём защёлка,
  которую видел человек (`estop.since` и `estop.reasons` из `/neurobot/safety_state`), и время записи — и ждёт
  `state` ≠ `estop`; запрос удаляет сам. safety_gate смотрит каталог дважды в секунду и снимает защёлку, только если
  каталог и файл — root и не доступны на запись группе и прочим, запросу не больше 10 с, защёлка та же (since и
  причины не изменились) и причина устранена (наклон, чужой писатель, база стоит).

Команде `release` в теме safety_gate не верит: поле `by` самозаявлено, и узел ROS (или клиент rosbridge, если тему
защёлки открыть ему по ошибке) подписался бы «human» — ROS не знает, кто опубликовал сообщение [находка приёмки 17.10:
прежняя проверка `by=human` пускала такой узел]. Канал root закрывает это без SROS2: агенту (PicoClaw без `exec`,
MCP-сервер и rosbridge работают не от root) записать файл root нечем, у него нет ни терминала, ни shell, ни темы
защёлки (конфиг MCP-сервера и `topics_glob`), ни сервисов и параметров (`services_glob`, `params_glob`). Граница: кто
может `sudo` на роботе, может и снять — это и есть «человек у робота». SROS2 (подпись участников DDS) закрыл бы и
саму тему, но на Foxy и Nano это ключи и политика на каждый узел — пакет 2.

Защёлка — состояние safety_gate, а не команда в теме: издатель CLI живёт около 2 с, и `transient_local` отдаёт
команду поздним подписчикам, только пока издатель жив. Поэтому safety_gate хранит её сам: юнит даёт ему
`StateDirectory=neurobot/safety` (`$STATE_DIRECTORY` = `/var/lib/neurobot/safety`, есть в systemd 245; переживает
рестарт, перезагрузку и `install.sh --uninstall`). Иначе любой рестарт (зависание тика на 3 с, исключение, OOM) снял бы
защёлку без человека — а защёлки по наклону и пробуксовке (PLAN §13.3) стоят как раз тогда, когда процесс под
нагрузкой.

Что нужно от safety_gate (W2), чтобы юнит и doctor работали:
- имя узла `NEUROBOT_SAFETY_NODE`; READY после первого явного нуля в `/cmd_vel` и WATCHDOG от тика (sd_notify через
  `$NOTIFY_SOCKET`); подписка на `NEUROBOT_CMD_VEL_REQ`, `NEUROBOT_ESTOP_TOPIC` (формат выше) и `NEUROBOT_STOP_TOPIC`
  (стоп агента: `{"id", "by", "reason", "t"}`, ноль сразу, без защёлки; копии с одним `id` — один стоп);
  `/neurobot/safety_state` 5 Гц и при изменении;
- защёлка и её причины атомарно (временный файл, fsync, rename) пишутся в `$STATE_DIRECTORY/estop.json` до того, как
  safety_gate подтвердит engage (`state` = `estop`), и читаются до READY; файл есть, но не читается — старт с
  защёлкой; снимает её только запрос root в `release_dir`; не удалось записать — защёлка остаётся в памяти, строка в
  журнале; в `/neurobot/safety_state` — `estop: {engaged, since, reasons}` (их CLI переносит в запрос снятия);
- чужой писатель `/cmd_vel` (или входов twist_mux главнее него) во время работы — защёлка: precheck ловит его только
  при старте, `wait-graph` — только у исполнителя и распознавания;
- если защёлка станет сервисом (контракт это допускает), его имя не должно попадать под `services_glob` rosbridge
  (precheck откажет, если попадает `NEUROBOT_ESTOP_TOPIC`).

Подставной safety_gate (`fakes/fake_component.py safety`) реализует ровно это (снятие — тем же файлом root);
сценарий `estop` стенда проверяет защёлку после `kill -9`.

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
- `topics_glob:="'[/cmd_vel_agent,/neurobot/stop_now]'"` — только тема агента и стоп агента (ниже). Раздельных
  списков подписки и публикации в rosbridge 1.3.1 (Foxy, робот) нет (тот же `topics_glob` проверяют и publish, и
  subscribe), поэтому тема из списка открыта клиенту и на запись: добавленные в список датчики safety_gate (`/scan`, глубина, IMU) или `/neurobot/*` (состояние, которое читают
  doctor и `neurobot estop`) можно было бы подменить. В `/cmd_vel`, `/neurobot/cmd_vel_req` и `/neurobot/estop`
  rosbridge с этим списком отвечает «No match found for topic, cancelling publish» [опыт, 15.10 — каталог
  `neurobot_20261015_deploy`]. В rosbridge 2.x (Jazzy, стенд) есть и раздельные `topics_pub_glob`, `topics_sub_glob`:
  они объединяются с `topics_glob` [по исходнику 2.7.1, `rosbridge_websocket`; опыт: с `topics_pub_glob:="'[*]'"` 20 из
  20 публикаций дошли до `/cmd_vel`], поэтому `topics_pub_glob` precheck держит тем же списком разрешённого, а
  `topics_sub_glob` (только подписка) пропускает любым; оба необязательны. Своего глоба для действий (actions) в
  launch-файле 2.x нет, и клиенту они открыты; в 1.3.1 действий нет вовсе, а в графе двойника action-серверов нет
  [опыт, 16.10: `ros2 action list` пуст];
- стоп агента `/neurobot/stop_now` (`NEUROBOT_STOP_TOPIC`, решение 1а 19.10) — по нему safety_gate ставит ноль
  сразу, без S-кривой и без защёлки, а дальше ждёт нулевого запроса. Это единственный вход safety_gate, открытый агенту,
  и он умеет только остановить: не трогает пределы, не снимает защёлку и не взводит; подделка — это стоп, а
  остановить робот агент может и нулями в `/cmd_vel_agent`. Без темы в списке `robot_stop` — штатная плавная остановка
  (≈0,5 с): precheck rosbridge её пропускает, precheck mcp предупреждает, если её нет в `topics_glob`;
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
пакета 2), rosbridge 1.3.1 их не даст: расширять список значит открыть их и на запись (раздельных списков нет — по
исходнику тега 1.3.1, `rosbridge_websocket.py`: один `topics_glob` для Subscribe, Advertise и Publish), а safety_gate
не должен опираться на темы, открытые агенту (требование к W2). Состояние safety_gate MCP-серверу нужно
(`robot_get_status`, отказ `robot_nudge`), поэтому:
- **Jazzy (rosbridge 2.x, двойник):** `topics_sub_glob:="'[/neurobot/safety_state]'"` — только подписка, публикация
  остаётся `[/cmd_vel_agent,/neurobot/stop_now]` (2.x объединяет `topics_sub_glob` с `topics_glob` только для подписки);
- **Foxy (rosbridge 1.3.1, робот):** safety_gate публикует копию в `agent_state_topic`
  (`/neurobot/agent/safety_state`, `NEUROBOT_AGENT_STATE_TOPIC`), и в `topics_glob` идёт она. Её никто, кроме
  MCP-сервера, не читает: опубликовав подделку, агент обманет только себя, а не исполнитель, doctor или `neurobot
  estop`. `/neurobot/safety_state` в `topics_glob` precheck не пустит.

Длительность толчка в секундах ограничена сверху и в прокси (патч), поэтому ошибка N1 «миллисекунды как секунды»
безопасна.

### Патч к ros_mcp_server (`mcp/ros_mcp_server.patch`, 136 добавленных строк)

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
5. **Состояние safety_gate в `robot_get_status`** (приёмка 17.10): тема `topics.safety_state` конфига (только чтение;
   на роботе — копия для агента, на двойнике — `/neurobot/safety_state`) читается своим соединением WebSocket
   (подписка, поток-читатель), в ответе — `safety: {state, reasons, estop, limits, req, out}`; `estop` — защёлка
   стоит (поле `estop.engaged` или `state` = `estop`); `motion_allowed` — ещё и «состояние ok или slow». Нет
   состояния за 1 с — `safety.state` = `unknown`, `motion_allowed` = false.
6. **Итог `robot_nudge` по выходу safety_gate, а не по факту публикации**: до толчка — защёлка (`status: estop`) или
   предел нужной оси равен нулю (`status: blocked`, причины safety_gate) — толчка нет. После — по состояниям
   safety_gate за время толчка и автостопа (в каждом — его вход `req` и выход `out`): выход по нужной оси хоть раз
   ненулевой со знаком толчка — `ok: true`; запрос до него дошёл (или он в блоке), а выход — ноль —
   `ok: false, status: blocked|estop, reasons`; состояний нет или в них не видно ни запроса, ни выхода —
   `ok: false, status: unconfirmed` (safety_gate не запущен, публикация потерялась). Раньше инструмент отвечал
   `ok: true`, когда safety_gate толчок не пропустил, и агент сообщал «проехал». Состояние идёт 5 Гц, поэтому толчок
   короче ≈0,25 с может дать `unconfirmed` и при движении — агент тогда не утверждает, что проехал. Сдвиг по `/odom`
   не используется: на Foxy открыть `/odom` агенту значит открыть его и на запись, а `/odom` читает safety_gate
   (геозабор, упор, память глубины). Конфиг без `topics.safety_state` — предупреждение precheck mcp.
7. **Постоянный издатель** (M3, K6): соединение WebSocket с `advertise` — одно на тему на всё время работы сервера
   (поток-читатель замечает обрыв, следующая публикация открывает новое; при ошибке — одна повторная попытка), а не
   новое на каждую публикацию. Прежде издатель rosbridge создавался заново почти на каждый вызов (rosbridge держит
   его 10 с после отключения клиента), DDS искал подписчиков заново, и первые нули `robot_stop` терялись: в 9 из 30
   стопов первый ноль дошёл до исполнителя через 0,15–0,36 с вместо 0,05 с, а в одном из 6 пробных стопов не дошёл
   ни один из пяти, и робот доехал манёвр жеста [замер, двойник, «Замеры на двойнике 18.10»]. Теперь у rosbridge
   один издатель `/cmd_vel_agent`, пока жив MCP-сервер: ноль доходит за 2–3 мс, 30 из 30.
8. **Стоп агента — стоп по безопасности** (решение 1а 19.10): инструмент `robot_stop` перед каждым из пяти нулей в
   `/cmd_vel_agent` публикует в тему `topics.stop_now` конфига (`/neurobot/stop_now`, `std_msgs/msg/String`, запись)
   `{"id", "by": "agent", "reason", "t"}` — один `id` на вызов, safety_gate исполняет его один раз: ноль в `/cmd_vel`
   сразу, без S-кривой и без защёлки. Автостоп в конце `robot_nudge` и ноль при выходе сервера — общая функция
   `send_stop` без темы стопа: это штатный конец манёвра, он остаётся плавным (мачта). Тема стопа недоступна
   (соединение отвергнуто) — строка в stderr, нули идут всё равно. Описание инструмента для агента не менялось.

Камера и лидар в примере конфига выключены (M1, `read_one_message` нет). Остальные дефекты (синхронные инструменты:
`robot_stop` ждёт конца идущего `robot_nudge`, толчки ≤ 0,044 м — M4–M8) патч не лечит: это пакет 2 (robot-mcp).
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

## Пульт оператора (компонент `ui`)

Живой шлюз пульта — `neurobot/ui` (README там же): узел rclpy и HTTP на 127.0.0.1, вход — SSH-туннель и одноразовая
ссылка `sudo neurobot ui-url`. Что держит инварианты, кроме кода пульта:
- **у `/cmd_vel` один писатель.** Узел пульта пишет только `/neurobot/stop_now` и `/neurobot/estop {engage}`: после
  READY это сверяет `wait-graph ui` (лишняя тема — отказ, systemd снимает процесс), во время работы — doctor
  (`ui_publisher`), а писателя `/cmd_vel` поймали бы ещё precheck safety_gate и его защёлка `foreign_writer`: имя узла
  пульта не `neurobot_*`, проверки писателей его видят;
- **агент новых прав не получает.** rosbridge агента, его глобы, MCP и PicoClaw пульт не трогает; пульт — отдельный
  пользователь, токен входа — в его `StateDirectory` 0700; ссылку входа выдаёт только root (граница — та же, что у
  снятия защёлки);
- **снять защёлку из пульта нельзя:** маршрута нет, команда в коде одна — engage; карточка показывает причины и
  `sudo neurobot estop release`;
- **робот без пульта работает:** `After=neurobot-safety` без `Requires=`, отказ — предупреждение doctor, свой
  `MemoryMax` и `Slice=`; рестарт мягче, чем у цепочки движения (через 3 с; код 2 — без рестарта; 5 неудачных запусков
  за 5 мин — сдаётся), `CPUWeight=20` и `Nice=10`: постоянный отказ пульта не съедает ядро у perception;
- **пульт пишет туда, где слушает safety_gate:** `precheck ui` отказывает, если `NEUROBOT_STOP_TOPIC` или
  `NEUROBOT_ESTOP_TOPIC` — не те темы, что в коде пульта (иначе СТОП и защёлка ушли бы в тему без safety_gate, а
  проверки издателей остались бы зелёными); подтверждение стопа — счётчик safety_gate, а не «есть подписчик»;
- **DDS между пользователями:** пульт и граф работают от разных пользователей; у Fast DDS (по умолчанию в Foxy и
  Jazzy) общая память между ними данных не пропускает — обнаружение есть, данных нет [замер, ВМ, Jazzy:
  `neurobot/ui/tests/foxy_smoke.sh`]; поэтому шлюз берёт профиль «только UDP» (`neurobot/ui/fastdds_udp.xml`), если
  env-файл своего `FASTRTPS_DEFAULT_PROFILES_FILE` не задал. Свой профиль лаборатории с общей памятью — пульт данных
  не увидит.

Граф ROS робота открыт сети лаборатории (`ROS_LOCALHOST_ONLY` выключен, RS11): стоп и защёлку любой ПК сети может
прислать и без пульта. Это ограничение робота, а не пульта; закрывается `ROS_LOCALHOST_ONLY=1` или SROS2 — вопрос к
лаборатории.

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
rosbridge (с подставным `safety_state`) и воркера; нужны `mcp`, `websocket-client`, `websockets` в `.venv`.
Обёртка launch против launch из Foxy (0.10.10, Python 3.8; без ROS 2 Foxy целиком):
`neurobot/deploy/verify/foxy_launch.sh <каталог исходников>`; на Jazzy (ВМ) — `test_deploy_launch.py` с
`NEUROBOT_TEST_ROS2_LAUNCH=1` (и сам `ros2 launch`). `test_deploy_verify.py` — разбор записей замеров `verify/`
(задержки, квантили, сектор скана, выбор узла); сами замеры — на двойнике:

```bash
# в ВМ sim, root, пакет на двойнике поднят (neurobot/sim/suites/setup_twin.sh, systemctl start neurobot.target)
source /opt/ros/jazzy/setup.bash; V=/var/lib/neurobot-twin/src/neurobot/deploy/verify
python3 $V/recovery.py --out rec.json --runs 5 --mcp                      # драйверы env-файла и rosbridge; KILL и STOP
python3 $V/recovery.py --out base.json --runs 3 --signals KILL driver@twin:twist_mux
python3 $V/k6.py --out k6.json --runs 30 --heading 90                       # mcp и scan; мир lab, курс — на север
python3 $V/agent_truth.py --out truth.json --runs 5                         # free, blocked, estop, nogate
```

`recovery.py`: `--runs N` (5) замеров на случай и сигнал, `--signals KILL,STOP`, `--settle S` (10) — юнит active
столько перед замером, `--timeout S` (60), `--mcp` — у rosbridge ждать и ответа `robot_get_status`; случаи —
`driver@<имя>[:шаблон]`, `rosbridge[:шаблон]` (шаблон — подстрока командной строки узла; по умолчанию у драйвера —
первый узел по PID, у rosbridge — `rosbridge_websocket`). `k6.py`: `--runs N` (30), `--cases mcp,scan`, `--obstacle
twin|wait` (столб сервисом мира двойника или препятствие ставит человек — HIL), `--heading ГРАД`, `--drive-topic`
(`/cmd_vel_gesture`), `--pause-unit` (`neurobot-perception`: он пишет в этот вход, пока стоит — пишет сценарий),
`--reset-unit` (`neurobot-safety`: рестарт после каждого замера scan — память глубины держит убранный вплотную столб),
`--speed` (0,15) и `--scan-speed` (0,08) м/с, `--drive-s` (1,5), `--near` (0,35 м) и `--sector` (5,25 градусов) —
где в скане искать препятствие; `--cases ui` — СТОП пульта (компонент `ui`, `docs/ui/PLAN.md` §7 U2): робот едет так же,
как в mcp, стоп — `POST /api/stop` шлюза с CSRF и `issued_at`, как у кнопки; `--ui-trigger local` — запрос с этого
хоста (вход по `neurobot ui-url`, root; `--ui-url`, по умолчанию `127.0.0.1:NEUROBOT_UI_PORT`), `stdin` — запрос
делает ноутбук через SSH-туннель (`neurobot/ui/tests/accept_p3.py stop`: строка готовности в stdout, моменты запроса
и ответа — строкой в stdin по часам хоста); метки — стоп в `/neurobot/stop_now` (копии с одним id), сообщение собрано
шлюзом, ноль в `/neurobot/cmd_vel_req` и `/cmd_vel`, «поехал после нуля» (сценарий ещё 2 с просит ехать).
`agent_truth.py`: `--runs N` (5) вызовов на условие, `--conditions` (`free` —
свободно, толчки вперёд и назад; `blocked` — столб вплотную, только двойник; `estop` — защёлка, снимает сценарий как
человек: `sudo neurobot estop release` в псевдотерминале; `nogate` — safety_gate остановлен), `--moved` (0,01 м) —
порог сдвига по `/odom`, `--pause-unit`, `--gate-unit` (`neurobot-safety`). Итог всех трёх — JSON (существующий файл
не перезаписывается), журнал — stderr.

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

## Замеры на двойнике 18.10 (приёмка 17.10)

Двойник в ВМ sim (мир lab, профиль `neurobot/sim/suites/twin.env`, снимок проекта 18.10), инструменты `verify/`;
артефакты, версии, хеши и отклонения — `experiments/results/neurobot_20261018_deploy_fix/` (`manifest.json`). Всё —
**ориентир стенда, не результат робота** [замер, двойник]; ВМ общая: в часть окон шли прогоны другого компонента со
своим Gazebo (отклонения в манифесте).

**Смерть и зависание узла внутри юнита ros2 launch** (`recovery.py`, n = 5 на случай; время от сигнала до «вернулся»:
тема драйвера снова идёт после нового запуска юнита, у rosbridge — WebSocket отвечает и `robot_get_status` по MCP
видит rosbridge), p50 / max, с:

| Узел (юнит) | kill -9 → обёртка гасит launch, systemd поднимает | SIGSTOP → сторож (свежесть темы / ping) |
|---|---|---|
| `/urg_node2` (`driver@lidar`) | 1,78 / 1,79, 5 из 5 | 4,77 / 4,88, 5 из 5 |
| `/camera/camera` (`driver@camera`) | 1,67 / 1,77, 5 из 5 | 4,77 / 4,88, 5 из 5 |
| `rosbridge_websocket` (`rosbridge`; до ответа MCP) | 2,81 / 2,81 (2,88 / 3,07), 5 из 5 | 4,46 / 4,66, 5 из 5 |
| сервер Gazebo с ros2_control (база двойника, `driver@twin`) | 8,80 / 8,82, 5 из 5 | не лечится: молчат все датчики сразу, 0 из 3 (по правилу «общая беда»; после SIGCONT пошло само) |
| `twist_mux` (в launch базы двойника; n = 3 и 1) | 7,34 / 21,26, 3 из 3 (Gazebo гас 16 с в одном) | не видит: своей темы свежести нет, 0 из 1 |

Человек не понадобился ни разу; кто поднял — в журнале: «процесс … завершился с кодом -9 — гашу launch» у обёртки,
`sensor_stale` / `rosbridge_down` у сторожа. SIGSTOP узла — две пробы сторожа по 1 с после того, как тема старше
2 с, плюс рестарт юнита: ≈4,5–5 с. На двойнике смерть узла базы — это рестарт Gazebo, то есть мир заново: робот
«переносится» на точку спавна, IMU камеры даёт всплеск, и safety_gate ставит защёлку `shock`/`tilt` — дальше нужен
человек (видимый перезапуск). На роботе рестарт launch базы не трогает IMU камеры и не двигает робот, поэтому
защёлки быть не должно — [допущение до HIL].

**K6 «stop через MCP → ноль ≤ 100 мс (p95)»** (`k6.py --cases mcp`, n = 30 на вариант, робот едет манёвром жеста
0,15 м/с, агент вызывает `robot_stop`; от момента перед HTTP-запросом):

| Метка, с | патч до постоянного издателя: p50 / p95 / max, в пределе | с постоянным издателем (п. 7 патча) |
|---|---|---|
| ноль в `/cmd_vel_agent` (дошёл через rosbridge) | 0,053 / 0,210 / 0,356, 21 из 30 | 0,002 / 0,003 / 0,053, 30 из 30 |
| ноль в `/neurobot/cmd_vel_req` (исполнитель) | 0,053 / 0,211 / 0,356, 21 из 30 | 0,002 / 0,003 / 0,053, 30 из 30 |
| начало торможения в `/cmd_vel` (safety_gate) | 0,080 / 0,252 / 0,356, 21 из 30 | 0,030 / 0,061 / 0,097, 30 из 30 |
| ноль в `/cmd_vel` | 0,576 / 0,756 / 0,829, 0 из 30 | 0,506 / 0,545 / 0,559, 0 из 30 |
| ответ инструмента | 0,508 / 0,510 / 0,511 | 0,253 / 0,254 / 0,303 |

Вывод: команда стопа доходит до исполнителя за 2–3 мс (первый вызов после старта сервера — 53 мс: новое соединение
ждёт 50 мс после `advertise`, как у лаборатории), safety_gate начинает тормозить в пределах 100 мс во всех 30.
Ноль в `/cmd_vel` по букве K6 не выполнен ни в одном: штатный стоп safety_gate делает плавно (S-кривая
`stop_decel` 0,5 м/с², рывок 2 м/с³ — находка 3 приёмки, `neurobot/safety`), с 0,15 м/с это ≈0,5 с. 19.10 студент
решил (1а): stop агента — стоп по безопасности, ноль сразу; замер — «Замеры на двойнике 19.10» ниже. После стопа робот не поехал ни разу (сценарий ещё 1,5 с просил ехать: исполнитель гасит
манёвр).

**K6 «препятствие → ноль ≤ 150 мс после скана»** (`k6.py --cases scan`, n = 30 × 2 серии, 0,08 м/с, столб Ø 5 см в
2 см от корпуса на пеленге +13°, вне слепого сектора; от приёма первого скана со столбом ближе 0,35 м в секторе
5…25° до первой `/cmd_vel` с нулевой линейной): серия 1 — p50 0,0003 с, p95 0,386 с, max 0,400 с, в пределе 27 из
30; серия 2 (с самопроверкой записи: наибольший промежуток приёма `/odom` узлом сценария ≤ 0,041 с) — p50 0,0001,
p95 0,0006, max 0,0007 с, 30 из 30. Первым останавливал лидар в 22 и 15 замерах (ноль — через 0,2–0,7 мс после
скана: safety_gate публикует решение из колбэка скана), глубина — в 8 и 15 (ноль раньше скана лидара). Три выброса
0,38–0,40 с серии 1 (остановка лидаром) пришлись на окно, когда в ВМ шли прогоны другого компонента; без самопроверки
записи в той серии нельзя отличить задержку safety_gate от остановки узла сценария — во второй серии их нет.
Касаний столба 0 из 60. Ограничение R7b: на роботе это верно только вне слепого сектора лидара и для того, что
пересекает плоскость скана (0,67 м).

**Что агент говорит о движении** (`agent_truth.py`, по 5 вызовов `robot_nudge` 1 с / 0,15 м/с, сдвиг по `/odom`):
свободно — `ok` и робот поехал 5 из 5; столб вплотную — `blocked` с причиной safety_gate («obstacle: глубина…»),
робот стоит 5 из 5; защёлка — `estop` 5 из 5, `robot_get_status`: `safety.estop` true, `motion_allowed` false;
safety_gate остановлен — `unconfirmed`, `safety.state` `unknown` 5 из 5. Ложных ответов 0 из 20 (в прогонах 16.10 —
`ok: true` при стоящем роботе в 1–3 из 3 реплик «вперёд» и в P5 под защёлкой). Блок в этих замерах ловится до толчка (предел
оси 0); отказ после толчка (safety_gate срезал на ходу) проверен только тестом с подставным rosbridge. Снятие
защёлки сценарием — `sudo neurobot estop release` в псевдотерминале, через файл root (`/run/neurobot-estop`).

**doctor на старте** (`neurobot start-test --cycles 20`, 26 циклов): холодных стартов 21 из 21, ложных причин до
«ОК» 0 (doctor_named пуст), время до «ОК» p50 17,1 с, max 21,1 с; сломанные `safety_down`, `no_camera`, `no_weights`,
`port_busy`, `rosbridge_down` — doctor назвал только свою причину, 5 из 5. «Холодный старт» — рестарт
`neurobot.target`, а не перезагрузка ОС.

## Замеры на двойнике 19.10 (решение 1а)

Пакет из рабочего дерева на двойнике (`neurobot/sim/suites/setup_twin.sh`), артефакты, версии и хеши —
`experiments/results/neurobot_20261019_decisions/` (`manifest.json`) [замер, двойник, ориентир].

**K6 «stop через MCP → ноль ≤ 100 мс (p95)»** с `/neurobot/stop_now` (`k6.py --runs 30 --cases mcp,scan --heading 90`,
мир hall — как серия 18.10 `a1v2/k6`; робот едет манёвром жеста 0,15 м/с, агент вызывает `robot_stop`):

| Метка от момента перед HTTP-запросом, с | p50 / p95 / max, в пределе 0,1 с |
|---|---|
| стоп агента в `/neurobot/stop_now` (дошёл через rosbridge) | 0,0016 / 0,0020 / 0,052, 30 из 30 |
| ноль в `/cmd_vel_agent` | 0,0015 / 0,0024 / 0,104, 29 из 30 |
| ноль в `/neurobot/cmd_vel_req` (исполнитель) | 0,0017 / 0,0027 / 0,053, 30 из 30 |
| ноль в `/cmd_vel` (safety_gate) | 0,0019 / 0,0027 / 0,053, 30 из 30 (18.10: 0,506–0,559, 0 из 30) |
| ответ инструмента | 0,252 / 0,253 / 0,355 |

Наибольшие значения — первый вызов после старта MCP-сервера: соединения тем стопа и агента открываются в нём и ждут
по 50 мс после `advertise`; ноль исполнителя в нём пришёл раньше нуля агента — safety_gate после стопа ждал нулевого
запроса, исполнитель по его `blocked` (`unarmed`) погасил манёвр жеста. После стопа робот не поехал ни разу (сценарий
ещё 1,5 с просил ехать). Препятствие по скану не ухудшилось: ноль через 0,3 / 0,7 / 0,8 мс после скана со столбом,
30 из 30, касаний 0. Проверки агентского пути (`check_rosbridge`, `check_mcp_config` на установленном env-файле) —
без замечаний.

## Ограничения

- Первый `robot_stop` после старта MCP-сервера открывает тему стопа агента лениво (advertise и пауза 50 мс): на двойнике ноль через 52–55 мс против 2–3 мс у следующих. На Jetson это может подойти к пределу K6 (100 мс), особенно сразу после перезапуска сервера сторожем. Предложение: делать advertise `/neurobot/stop_now` при старте сервера (правка в локальном патче к ros_mcp_server) — не сделано.
- Стенд подставных компонентов (`fakes/`) проверяет только запуск: юнит, порядок, ноль при остановке, цепочку
  движения и защёлку как протокол; решения безопасности (тормозной путь, глубина, IMU, слепой сектор) — настоящий
  safety_gate на двойнике (`neurobot/sim/suites`).
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
- Обёртка launch проверена против launch 0.10.10 из Foxy (Python 3.8, `verify/foxy_launch.sh`) и на Jazzy (юниты
  двойника: kill -9 узлов лидара, камеры и rosbridge — тема снова идёт через 1,7–2,8 с, база двойника — 7–9 с, до
  21 с, когда Gazebo долго гаснет [замер, «Замеры на двойнике 18.10»]); на роботе с его urg_node2, realsense2_camera
  и launch базы — нет. Launch базы лаборатории может запускать ещё одноразовые процессы,
  кроме spawner: их надо дописать в `oneshot:=`, иначе их нормальный выход погасит базу (в журнале — «процесс …
  завершился с кодом 0 — гашу launch»).
- Сторож подписан на кадры цвета raw: ≈14 МБ/с копирования на Nano при 640×480×15 [оценка, не мерили]; дешевле —
  `FRESH="/camera/color/camera_info:2"`. Пороги свежести (2 с, две пробы по 1 с) — [допущение]: SIGSTOP узла лидара,
  камеры или rosbridge — снова работает через 4,5–4,9 с, 15 из 15 [замер, двойник]. Вне внесённых отказов за ≈1 ч
  замеров 18.10 сторож никого не перезапускал; при рестарте Gazebo двойника (из него идут все датчики) прежнее правило
  «молчат все» не срабатывало из-за разных пределов (/odom 3 с, /scan 2 с) и давало лишний рестарт драйвера лидара —
  3 раза из 5 [замер]; теперь «все молчат» — дольше наименьшего предела (тест; повтор на двойнике — в артефактах).
- Что сторож не лечит: зависание всех датчиков сразу (на двойнике — SIGSTOP сервера Gazebo: 3 из 3 без рестарта,
  после SIGCONT всё продолжилось само; на роботе так выглядит беда DDS) и зависший узел, у которого нет своей темы
  свежести, — `twist_mux` (SIGSTOP: 1 из 1 без рестарта; база стоит по `cmd_vel_timeout`, жесты не исполняются, doctor
  этого не видит). Нужна проба «вход `/cmd_vel` идёт, а `/diff_cont/cmd_vel_unstamped` — нет» — пакет 2; до него —
  `sudo systemctl restart neurobot-driver@base` человеком (RUNBOOK §4.6). Смерть `twist_mux` (kill -9) ловит обёртка:
  3 из 3.
- На двойнике смерть узла базы — рестарт Gazebo: мир заново, робот на точке спавна, всплеск IMU и защёлка `shock` /
  `tilt` safety_gate — снимает человек. В замерах 18.10 её снимал сценарий как оператор стенда (`sudo neurobot estop
  release` в псевдотерминале).
- `After=` упорядочивает и рестарт: юнит, который поднимается заново (`Restart=`), ждёт окончания долгого задания start
  у юнитов, после которых он стоит [опыт, ВМ: рестарт rosbridge ждал ≈115 с, пока perception без кадров висел в своём
  start до TimeoutStartSec]. В штатном отказе одного компонента это не мешает; при двух сразу время восстановления
  второго — до TimeoutStartSec первого.
- `wait-graph` ловит писателя `/cmd_vel` у исполнителя и распознавания только после READY: в первом запуске с
  потерянным ремапом узел пишет мимо safety_gate до проверки графа — ≈1,2–1,5 с [замер, ВМ]. Дальше метка не даёт
  запустить его с той же командой (без неё цикл рестартов возобновлял обход в 37 % времени [опыт W1, ВМ]; с ней —
  один раз, `experiments/results/neurobot_20261015_deploy_fix2/vm/remap_loop.*`). Метку снимает смена команды или
  человек; ремап, потерянный после смены команды, снова даст одно окно.
