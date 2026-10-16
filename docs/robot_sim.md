# Симулятор робота: ROS 2 Jazzy + Gazebo Harmonic + TurtleBot3 (VM «sim»)

Стенд второго сценария «рабочего дня» ВКР — мобильного робота. Агент EdgeCtx работает на одноплатнике
(VM «sbc»). Рассуждение выполняет внешняя LLM, оборудование агент видит через MCP, а робот и мир живут в
отдельной VM «sim». Агент и его MCP-сервер обращаются к роботу только через **rosbridge** (WebSocket, один TCP-порт
9090), поэтому ROS на плате не нужен (F_practical_stack §2.5, [F19], [F20]).

Ключевая метрика ВКР — производительность и экономность работы во времени. Поэтому стенд с самого начала
настроен так, чтобы платить только за используемое: камера рендерится по запросу, обратная связь Nav2 не льётся
в агента, ненужные темы не передаются. Все рычаги ниже измерены (§7, §8).

Все числа в этом документе — **smoke-замеры** (`experiments/results/robot_smoke_20261006/`). Их снимали, пока на
том же Mac работали другие агенты и VM «sbc» (IMPLEMENTATION_SPEC §0 п. 4). Они нужны, чтобы выбрать конфигурацию
и оценить порядок величин. Результатами ВКР они не являются.

## 1. Схема

```
Mac (M4 Pro, 12 ядер, 24 ГБ)
├─ VM "sbc" (4 vCPU, 4 ГБ) — плата: edgectx + MCP-сервер робота ──ws://host.lima.internal:9090──┐
│                                                                                               │
├─ 127.0.0.1:9090  ◄── проброс порта Lima (hostIP 127.0.0.1, guestPort 9090) ◄─────────────────┘
│
└─ VM "sim" (Ubuntu 24.04 arm64, vz, 6 vCPU, 8 ГБ, диск 40 ГБ)
   ├─ gz sim (Harmonic 8.15, сервер без GUI, --headless-rendering: EGL + Mesa llvmpipe)
   │   └─ TurtleBot3 waffle_pi: лидар 360°, камера 640×480, IMU, DiffDrive (одометрия), PosePublisher (истина)
   ├─ ros_gz_bridge (parameter_bridge: clock, odom, tf, imu, scan, joint_states, cmd_vel; камера и истина — lazy)
   ├─ image_transport/republish (raw → JPEG, ленивый)          ├─ robot_state_publisher
   ├─ Nav2 (nav2_bringup, composition, без RViz; карта turtlebot3_world, AMCL с начальной позой)
   └─ rosbridge_server 2.7.1 + rosapi  (0.0.0.0:9090 внутри VM)
   DDS: Cyclone DDS, только loopback (ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST), ROS_DOMAIN_ID=30
```

Путь `host.lima.internal:9090` (шлюз usernet Lima 192.168.5.2 → loopback хоста → проброс в «sim») проверен
изнутри VM «sim»: 100 тем через rosapi (`smoke/reach_host_lima_internal.json`). У «sbc» тот же механизм usernet.
07.10 тот же путь проверен **из самой «sbc»**: TCP и `rosapi/topics` (100 тем) через `host.lima.internal:9090`,
ответ за 32 мс; ros-mcp-server и R-compact работают оттуда (§12). Проброс поэтому оставлен на 127.0.0.1, и
rosbridge (сервер без аутентификации, через него можно управлять роботом) в LAN не публикуется. `hostIP: "0.0.0.0"`
в `scripts/sim/sim.lima.yaml` нужен только для настоящей платы в LAN.

## 2. Что установлено (06.10.2026)

| Компонент | Версия | Примечание |
|---|---|---|
| Ubuntu | 24.04.4 LTS, ядро 6.8.0-106, arm64 | тот же облачный образ, что у «sbc» (взят из кэша Lima, дайджест проверяется) |
| ROS 2 | Jazzy, `ros-jazzy-ros-base` 0.11.0 | 346 пакетов ros-jazzy, `/opt/ros/jazzy` — 735 МБ; на диске VM занято 5,4 ГБ |
| Gazebo | Harmonic, gz-sim 8.15.0 (`ros-jazzy-gz-sim-vendor` 0.0.13), `ros-jazzy-ros-gz` 1.0.24 | |
| TurtleBot3 | `turtlebot3_gazebo` 2.3.7, `turtlebot3_navigation2` 2.3.6, `turtlebot3_simulations` | модель waffle_pi |
| Nav2 | `nav2_bringup` 1.3.13, `navigation2` | |
| rosbridge | `rosbridge_server` 2.7.1 (+ rosapi) | протокол v2, действия `send_action_goal` |
| RMW | `rmw_cyclonedds_cpp` 2.2.4 | Fast DDS не установлен (см. §9) |
| Mesa | 25.2.8, llvmpipe (LLVM 20), OpenGL 4.5 core, EGL surfaceless/device | GPU в VM нет |
| image_transport | 5.1.8 + `image_transport_plugins` | JPEG-сжатие кадров |

Загрузка — 669 МБ архивов (1 316 пакетов) плюс 12,5 МБ обновлений. Установка заняла около 2,5 мин
(`experiments/results/robot_smoke_install/install.log`). RViz пришёл зависимостью `turtlebot3_navigation2`,
но не запускается.

## 3. Создание VM и установка

```sh
scripts/sim/create_vm.sh          # limactl create --name sim scripts/sim/sim.lima.yaml; запуск (идемпотентно)
scripts/sim/provision.sh          # в фоне: install_ros.sh внутри VM, лог experiments/results/robot_smoke_install/install.log
tail -f experiments/results/robot_smoke_install/install.log
```

`scripts/sim/sim.lima.yaml`: vz, 6 vCPU, 8 ГиБ, 40 ГиБ. Проект смонтирован **только для чтения** по тому же пути
(virtiofs): скрипты и launch-файл берутся прямо из репозитория. containerd выключен, видео нет. Проброшен только
TCP 9090, остальные порты гостя (DDS, gz-transport) на хост не пробрасываются.

`scripts/sim/install_ros.sh` (запускается внутри VM, идемпотентен):
1. universe, локаль, `ros2-apt-source` (официальный способ подключить packages.ros.org);
2. `apt-get install --no-install-recommends` ROS-пакетов из §2;
3. Mesa (`libgl1-mesa-dri libegl-mesa0 libegl1 libgles2 mesa-utils`), `xvfb` (запасной путь, не используется),
   `sysstat`, `python3-websockets`;
4. `net.core.rmem_max = 32 МБ` (`/etc/sysctl.d/60-ros-dds.conf`): Cyclone DDS просит буфер 16 МБ, см. §9;
5. выключены `unattended-upgrades`, `apt-daily*.timer` и `lttng-sessiond`, чтобы фоновые службы не шумели в
   замерах;
6. окружение в `~/.bashrc` (интерактивное). Скрипты используют `scripts/sim/vm_env.sh`.

## 4. Запуск и остановка

```sh
scripts/sim/start_sim.sh                       # turtlebot3_world, RTF 1, камера lazy 30 Гц, rosbridge :9090
scripts/sim/start_sim.sh --nav2                # + Nav2 (готовность — lifecycle_manager_navigation active, ~10 с)
scripts/sim/start_sim.sh --world turtlebot3_house --camera-hz 5
scripts/sim/start_sim.sh --rtf 0 --camera off  # ускоренный режим: «как можно быстрее»
scripts/sim/stop_sim.sh                        # SIGINT группе процессов (→ TERM → KILL), затем ros2 daemon stop
scripts/sim/stop_sim.sh --vm                   # + limactl stop sim
```

| Параметр `start_sim.sh` | По умолчанию | Смысл |
|---|---|---|
| `--world` | `turtlebot3_world` | `turtlebot3_world`, `turtlebot3_house`, `empty_world` или путь к .sdf/.world внутри VM |
| `--rtf` | `1.0` | целевой real-time factor; `0` — без ограничения (сколько позволит CPU) |
| `--step` | из мира (0,001 с) | `max_step_size` физики; 0,004 с проверено: езда и повороты корректны (§7) |
| `--nav2` | выкл. | Nav2 с картой `turtlebot3_navigation2/map/map.yaml`, начальная поза AMCL = поза спавна |
| `--camera` | `lazy` | `lazy` — рендер только при подписчике; `stream` — как в TurtleBot3 (всегда); `off` — сенсор удалён |
| `--camera-hz` | `30` | частота камеры (в сим. времени); `0` = `off` |
| `--jpeg-quality` | `80` | качество JPEG `/camera/image_raw/compressed` (режим lazy) |
| `--lidar-hz`, `--imu-hz`, `--odom-hz` | 10, 200, 30 | частоты сенсоров; `0` — сенсор удалён из модели |
| `--x --y --yaw` | (−2,0; −0,5; 0) для world/house | поза спавна |
| `--no-rosbridge`, `--port` | вкл., 9090 | rosbridge; порт должен совпадать с пробросом Lima |
| `--world-control` | выкл. | сервисы `/world/default/{control,create,remove,set_pose}` в ROS через `ros_gz_bridge`: пауза, `run_to_sim_time`, сценарные модели (§13) |
| `-- ключ:=значение` | — | прочие аргументы `launch/robot_sim.launch.py` (`ground_truth_hz`, `nav2_delay`, `map`, `gz_verbose`) |

Готовность проверяется так: идёт `/clock`, с хоста отвечает rosbridge (`rosapi/topics`), при `--nav2`
активен lifecycle Nav2. Без Nav2 стенд готов за 3–4 с, с Nav2 — за 9–13 с.

Внутри VM `scripts/sim/vm_start.sh` запускает `ros2 launch` через `setsid` (своя группа процессов, PID = PGID).
Каталог прогона — `~/robot_sim/<ГГГГММДД_ччммсс>/`, ссылка `~/robot_sim/current`. В нём лежат сгенерированные
`world.sdf`, `models/robot.sdf`, `bridge.yaml`, `nav2_params.yaml`, а также `launch.log`, `ros_log/`, `args.txt`,
`pid` и `started_at`.

### 4.1. Чем `launch/robot_sim.launch.py` отличается от `turtlebot3_gazebo/turtlebot3_*.launch.py`

1. **Только сервер gz** (`-r -s --headless-rendering`). gzclient не запускается.
2. **Fuel-включения Ground Plane и Sun заменены встроенными** моделью и светом. Старт не обращается к
   fuel.gazebosim.org и не зависит от сети.
3. **RTF и шаг физики** записываются в копию мира: `real_time_factor`, `max_step_size` и согласованный
   `real_time_update_rate`.
4. **Сенсоры.** Частоты переписываются в копии модели. Выключенный сенсор **удаляется** из модели: `update_rate=0`
   в gz-sensors означает «без ограничения» (§9).
5. **Ленивая камера (по умолчанию).** Изображение и `camera_info` идут через `parameter_bridge` с `lazy: true`, а
   JPEG — через ленивый `image_transport/republish`. Пока на `/camera/*` никто не подписан, Gazebo камеру не
   рендерит.
6. **Истинная поза робота** — `/ground_truth/pose` (`geometry_msgs/PoseStamped`, 10 Гц, lazy) от
   `gz-sim-pose-publisher-system`. Нужна для оценки сценария (дошёл ли робот), агенту не выдаётся.
7. **Nav2** запускается через `nav2_bringup/bringup_launch.py` без RViz (у `turtlebot3_navigation2` RViz запускается
   всегда). В параметрах `waffle_pi.yaml` включены `set_initial_pose: true` и поза спавна, поэтому публиковать
   `/initialpose` не нужно.
8. **rosbridge_websocket + rosapi** — `rosbridge_websocket_launch.xml` на 0.0.0.0 внутри VM.

## 5. Интерфейс робота (что видит MCP-сервер через rosbridge)

| Тема / действие | Тип | Частота (сим.) | CDR, Б | JSON rosbridge, Б | `cbor-raw`, Б |
|---|---|---|---|---|---|
| `/scan` | sensor_msgs/LaserScan (360 лучей) | 10 Гц | 2 944 | 8 034 | 3 003 |
| `/odom` | nav_msgs/Odometry | 50 Гц¹ | 724 | 747 | 783 |
| `/imu` | sensor_msgs/Imu | 200 Гц | 340 | 756 | 398 |
| `/tf` | tf2_msgs/TFMessage | 60 Гц (80 Гц с Nav2) | 132–135 | 403 | 196 |
| `/joint_states` | sensor_msgs/JointState | 1000 Гц (каждый шаг) | 140 | 295 | 206 |
| `/clock` | rosgraph_msgs/Clock | 1000 Гц | — | 82 | 70 |
| `/camera/image_raw` | sensor_msgs/Image 640×480 RGB | 30 Гц² | 921 664 | 1 238 043 | 921 737 |
| `/camera/image_raw/compressed` | sensor_msgs/CompressedImage, JPEG q80 | 30 Гц² | 13 211 | 16 655–18 348 | 13 291 |
| `/camera/camera_info` | sensor_msgs/CameraInfo | 30 Гц² | 376 | 523 | 449 |
| `/cmd_vel` (вход) | **geometry_msgs/TwistStamped** | — | — | 158 (publish) | — |
| `/ground_truth/pose` | geometry_msgs/PoseStamped | 10 Гц, lazy | — | — | — |
| `/navigate_to_pose` | nav2_msgs/action/NavigateToPose | — | — | цель 272, результат ≈850, feedback ≈500 Б × 100/с (bt_loop_duration по умолчанию 10 мс) | — |

¹ `odom_publisher_frequency=30` у DiffDrive даёт в Harmonic 50 Гц сим. времени. Это поведение плагина, цифра
измерена. ² В режиме lazy — только пока есть подписчик.

Как обращаться к роботу через rosbridge (`scripts/sim/rosbridge_probe.py`, используется в smoke):
- **движение:** `advertise` `/cmd_vel` с типом `geometry_msgs/msg/TwistStamped` (в Jazzy не Twist, [F21]), затем
  `publish` с частотой ≥ 10 Гц;
- **навигация:** `{"op":"send_action_goal","action":"/navigate_to_pose","action_type":"nav2_msgs/action/NavigateToPose","args":{"pose":{"header":{"frame_id":"map"},"pose":{...}}},"feedback":false}`.
  Поле цели называется **`args`**, а не `goal`: с `goal` Nav2 получает пустой кадр и прерывает цель. Результат
  приходит как `action_result` со `status`: 4 — SUCCEEDED, 5 — CANCELED, 6 — ABORTED;
- **кадр по запросу:** `subscribe` `/camera/image_raw/compressed` → первое сообщение → `unsubscribe`;
- **QoS:** rosbridge по умолчанию подписывается best effort. Для больших сообщений это работает только с
  буфером DDS из §9. В `subscribe` можно передать и `"qos":{"reliability":"reliable","depth":1}`.

## 6. Smoke-проверка (`scripts/sim/smoke.sh`)

`experiments/results/robot_smoke_20261006/smoke/` (сводка — `summary.json`, лог — `../smoke.log`):

| Проверка | Результат |
|---|---|
| Старт turtlebot3_world + Nav2 + rosbridge | готов за 13 с |
| Темы `/scan /odom /imu /camera/image_raw(/compressed) /tf /joint_states` | 9,97 / 49,96 / 200,0 / 30,3 / 80,0 / 1000 Гц сим. (RTF 0,65 при подписке на камеру 30 Гц) |
| `/cmd_vel` → `/odom` | 0,2 м/с × 3 с → 0,579 м; 0,5 рад/с × 3 с → 1,503 рад |
| Кадр JPEG по запросу (10 раз, пауза 3 с) | задержка p50 0,98 с (0,66–1,08 с), 16,7 КБ на кадр |
| `navigate_to_pose` (−2,0; −0,5) → (1,8; 0,5) без feedback | SUCCEEDED, 15,5 с; истинная ошибка 0,19 м (допуск xy Nav2 — 0,25 м); принято 1 601 Б (результат ≈850 Б + один `/odom`) |
| `navigate_to_pose` обратно с feedback | SUCCEEDED, 27,2 с; ошибка 0,05 м; **2 714 сообщений feedback, 1,35 МБ** |
| rosbridge через `host.lima.internal:9090` изнутри VM | 100 тем |

Остальные ручные проверки:
- `navigate_to_pose` при камере stream (RTF 0,66) — 23,2 с, ошибка по AMCL 0,25 м;
- `gz model -p` против AMCL — расхождение 0,07 м. Колёсная одометрия за 4 м уходит на ~0,5 м: это проскальзывание
  в DiffDrive, поэтому успех цели проверяется по `/ground_truth/pose`, а не по `/odom`;
- шаг физики 4 мс: 0,584 м и 1,508 рад на тех же командах. Физика остаётся корректной, `/joint_states` идёт с
  частотой 250 Гц.

## 7. Нагрузка стенда (`scripts/sim/load_matrix.sh`, 20 с замера после 10 с прогрева)

`experiments/results/robot_smoke_20261006/load/` (JSON по конфигурациям и `summary.md`). Колонки:
- «CPU sim» — сумма utime+stime процессов группы симуляции (`/proc/<pid>/stat`) за окно, в ядрах;
- «хост» — CPU-время процесса `com.apple.Virtualization.VirtualMachine` этой VM за то же окно (ядра M4 Pro).
  Включает гостевое ядро и virtio, то есть это реальная цена стенда для Mac;
- «PSS sim» — Σ PSS процессов симуляции (`smaps_rollup`). RSS процесса VM на хосте — 2,2–2,4 ГиБ во всех
  конфигурациях (память гостя, включая его page cache).
- Замерщик (`topic_rates.py`, сырые подписки rclpy) стоит 0,1–0,2 ядра и в «CPU sim» не входит.

| Конфигурация | RTF | CPU sim, ядер | из них gz | Nav2 | PSS sim, МиБ | CPU хоста, ядер | /scan, Гц ст./сим. |
|---|---|---|---|---|---|---|---|
| A: камера stream 30 Гц (как в TurtleBot3) | **0,676** | 2,33 | 2,17 | – | 793 | 2,74 | 6,8 / 10,1 |
| B: stream 5 Гц | 0,969 | 1,27 | 1,05 | – | 794 | 1,82 | 9,7 / 10,0 |
| **C: lazy 30 Гц, никто не смотрит (по умолчанию)** | **0,994** | **0,85** | 0,64 | – | 762 | **1,40** | 10,0 / 10,0 |
| D: lazy 30 Гц, подписчик на JPEG весь замер | 0,687 | 2,22 | 2,06 | – | 803 | 2,61 | 6,9 / 10,1 |
| E: lazy 5 Гц, подписчик на JPEG | 0,972 | 1,20 | 1,00 | – | 799 | 1,73 | 9,8 / 10,0 |
| F: камера удалена | 0,993 | 0,83 | 0,65 | – | 694 | 1,38 | 10,0 / 10,0 |
| G: камера и лидар удалены (рендеринга нет) | 1,000 | 0,52 | 0,31 | – | 328 | 1,15 | – |
| H: lazy, RTF 0 (без ограничения) | **2,90** | 1,99 | 1,61 | – | 762 | 2,78 | 29,0 / 10,0 |
| I: без камеры и лидара, RTF 0 | **6,66** | 2,17 | 1,37 | – | 328 | 4,02 | – |
| J: lazy, RTF 0, шаг 4 мс | 3,06 | 1,39 | 1,26 | – | 762 | 1,71 | 30,6 / 10,0 |
| K: stream 30 Гц + Nav2 | 0,677 | 2,61 | 2,12 | 0,33 | 921 | 3,03 | 6,8 / 10,0 |
| **L: lazy + Nav2 (рабочая конфигурация сценария)** | **0,996** | **1,34** | 0,66 | 0,47 | 887 | **1,96** | 10,0 / 10,0 |
| M: turtlebot3_house, lazy | 0,984 | 1,41 | 1,18 | – | 941 | 2,12 | 9,8 / 10,0 |
| N: house, lazy 5 Гц, подписчик на JPEG | 0,937 | 1,83 | 1,61 | – | 985 | 2,49 | 9,4 / 10,0 |
| O: empty_world, lazy | 0,994 | 0,90 | 0,68 | – | 740 | 1,46 | 10,0 / 10,0 |

Выводы для стенда:
1. **Узкое место — программный рендеринг камеры** (llvmpipe), а не физика. В TurtleBot3 по умолчанию камера
   30 Гц рендерится всегда. Из-за этого RTF падает до 0,68, а лидар и IMU отстают по стенному времени.
2. **Ленивая камера бесплатна в простое.** C ≈ F: RTF 0,99, CPU симуляции 0,85 ядра против 2,33, на хосте
   1,40 ядра против 2,74. Это −63 % и −49 %. Цена — задержка первого кадра около 1 с (§6). Пока агент смотрит,
   нагрузка как у stream (D ≈ A), поэтому для частого просмотра камеры нужно 5 Гц (E).
3. Nav2 добавляет около 0,5 ядра и 125 МиБ (L против C). Рабочая конфигурация «Nav2 + ленивая камера» стоит
   1,34 ядра в VM и около 2 ядер хоста. На хосте остаётся запас под «sbc» и стенд.
4. **Ускоренный режим.** RTF 0 даёт около 2,9× с лидаром и 6,7× без рендеринга. Шаг 4 мс почти не помогает
   (3,06×): ограничивает рендер лидара, около 29 кадров/с по стенному времени. Ускорение применимо к прогонам с
   mock-LLM (T0), если агент живёт по симуляционному времени. Для T2 нужен RTF 1.
5. Дом (`turtlebot3_house`) дороже пустого мира на 0,5 ядра: больше геометрии в кадре лидара.
6. `/proc/stat` в VM для CPU не годится. При `CONFIG_HZ=1000` и цикле физики 1 кГц потиковый учёт занижает
   нагрузку: в G «вся VM» — 0,14 ядра при 0,52 ядра у процессов. Поэтому в отчётах берутся время процессов
   (учёт по runtime планировщика) и CPU процесса VM на хосте.

## 8. Трафик rosbridge и рычаги экономии для агента

С хоста, `rosbridge_probe.py rates` по одной подписке на тему
(`experiments/results/robot_smoke_20261006/rosbridge/`):

| Поток | JSON | CBOR | cbor-raw | PNG | с `throttle_rate` 1000 мс |
|---|---|---|---|---|---|
| `/scan` (10 Гц) | 80,0 КБ/с | 31,3 | 29,9 | 60,5 | 8,0 КБ/с (cbor-raw 3,0) |
| `/odom` (50 Гц) | 37,2 КБ/с | 49,1 | 39,0 | 37,4 | 0,75 КБ/с |
| `/imu` (200 Гц) | 150 КБ/с | 117 | 79,2 | 187 | 0,76 КБ/с |
| `/joint_states` (1 кГц) | 291 КБ/с | 202 | 204 | 512 | — |
| `/clock` (1 кГц) | 80 КБ/с | 55 | 69 | 249 | — |
| камера raw (≈20 кадров/с при RTF 0,68) | 25,2 МБ/с, 1,24 МБ/кадр | 19,2 МБ/с | 19,1 МБ/с | — | — |
| камера JPEG q80 | 381 КБ/с, 18,3 КБ/кадр | 278 | 278, 13,3 КБ/кадр | 417 | 18,3 КБ/с |
| feedback `navigate_to_pose` | ≈50 КБ/с (100 сообщ./с, ≈500 Б) | | | | не регулируется |

Отсюда правила для MCP-сервера робота и для агента. Они же — проверяемые факторы оптимизации в сценарии:
1. **Кадр — по событию, JPEG.** 18 КБ вместо 1,24 МБ, в 67 раз меньше. Ленивая камера при этом не нагружает
   стенд между кадрами.
2. **`navigate_to_pose` без `feedback`.** На одну цель уходит 1,6 КБ вместо 1,35 МБ (в 840 раз меньше).
   Прогресс читается по запросу (`/ground_truth/pose` для оценки, `/amcl_pose` или `tf` для агента) с
   `throttle_rate`.
3. **Подписки — только с `throttle_rate`** и только на нужные темы. Высокочастотные `/joint_states`, `/clock` и
   `/imu` агенту не нужны: это 0,5 МБ/с в JSON.
4. **CBOR** окупается на числовых массивах (`/scan` в 2,7 раза меньше), на мелких сообщениях — нет (`/odom` в
   CBOR больше JSON). Сжатие PNG вредно везде, кроме `/scan`.

## 9. Проблемы и решения при развёртывании

| Симптом | Причина | Решение |
|---|---|---|
| `AMENT_TRACE_SETUP_FILES: unbound variable` | `setup.bash` ROS несовместим с `set -u` | `vm_env.sh` временно снимает `set -u` |
| `malformed launch argument 'step:='` | `ros2 launch` не принимает пустые значения | `start_sim.sh` передаёт необязательные аргументы, только если они заданы |
| AMCL не загружается: `initial_pose.x … double … integer` | `-2` вместо `-2.0` в YAML | поза пишется с `%.4f`; правка `set_initial_pose` — регуляркой по началу строки (иначе задевала `always_reset_initial_pose`) |
| `navigate_to_pose` сразу ABORTED, `frame_id ''` | rosbridge 2.x берёт цель из поля `args` | `rosbridge_probe.py` шлёт `args` |
| Fast DDS отсутствует, работает Cyclone | `--no-install-recommends` не тянет `rmw_fastrtps_cpp` | Cyclone закреплён явно (`RMW_IMPLEMENTATION` в `vm_env.sh`): он легче, а для DDS внутри одной VM разницы нет |
| rosbridge отдаёт 0–6 кадров камеры, потом тишина | rosbridge подписывается **best effort**; кадр 921 КБ — это около 15 UDP-фрагментов по loopback, а буфер сокета 208 КБ | `scripts/sim/cyclonedds.xml` (`SocketReceiveBufferSize min=16MB`) + `net.core.rmem_max=32MB`: 20 кадров/с в JSON |
| `--camera-hz 0` не выключал камеру | в gz-sensors `update_rate=0` — «каждый кадр рендера» (камера шла 10 Гц вслед за лидаром) | выключенный сенсор удаляется из модели |
| «Ленивая» камера рендерилась без зрителей | подписка на `/camera/camera_info` тоже включает рендер | в замерах простоя `/camera/*` не подписывается (`load_v2_camera_info_subscribed/` — ошибочный прогон, сохранён) |
| `vm_stop` доходил до KILL | короткое ожидание: Nav2 под нагрузкой гасит lifecycle-узлы до 10–20 с | INT ждёт 30 с, TERM — 10 с; PGID = PID лидера (`setsid`) |

## 10. Файлы

| Путь | Что |
|---|---|
| `scripts/sim/sim.lima.yaml` | шаблон Lima VM «sim» |
| `scripts/sim/create_vm.sh`, `provision.sh`, `install_ros.sh` | создание VM, установка стека (лог) |
| `scripts/sim/lib.sh` | `to` (таймаут без coreutils), `vm` (команда в VM с таймаутом) |
| `scripts/sim/start_sim.sh`, `stop_sim.sh` | запуск и остановка с хоста |
| `scripts/sim/vm_env.sh`, `vm_start.sh`, `vm_stop.sh`, `cyclonedds.xml` | окружение и процессы внутри VM |
| `scripts/sim/launch/robot_sim.launch.py` | headless-launch: мир/модель/мост/Nav2/rosbridge |
| `scripts/sim/rosbridge_probe.py` | клиент rosbridge без ROS: topics, rates, snapshot, drive, nav (JSON) |
| `scripts/sim/topic_rates.py` | (в VM) частоты тем по стенному и сим. времени, RTF, без десериализации |
| `scripts/sim/vm_load.py`, `measure_load.sh`, `load_matrix.sh`, `summarize_load.py` | замер нагрузки, матрица конфигураций |
| `scripts/sim/smoke.sh` | сквозная проверка (§6) |
| `experiments/results/robot_smoke_20261006/` | `smoke/`, `load/`, `rosbridge/`, архивы `load_v1_*`, `load_v2_*` (до исправлений) |
| `scripts/sim/install_rosmcp.sh`, `rosmcp/requirements*.lock` | ros-mcp-server в venv пользователя на Mac и в «sbc» (§12.1) |
| `scripts/sim/rosmcp_probe.py` | MCP-клиент замеров: задержка, ответ MCP, трафик rosbridge через счётный прокси, PSS/CPU сервера (§12.2) |
| `scripts/sim/robot_mcp_compact.py` | прототип тонкого MCP-адаптера робота R-compact (§12.6–12.7) |
| `scripts/sim/topic_tokens.py`, `rosmcp_analyze.py` | токены одного сообщения каждой темы; токены/₽/трафик по задачам (§12.5–12.6) |
| `scripts/sim/rosmcp_smoke.sh` | весь smoke §12 с «платы» с перезапуском симуляции |
| `experiments/results/robot_smoke_20261007/` | прогоны §12 (`rosmcp_*`, `topic_tokens/`, `rosmcp_analysis_sbc/`) и §13 (`lockstep/`) |
| `scripts/sim/lockstep_probe.py` | пошаговый режим через rosbridge: free / step / tour / hold / reads (§13) |
| `scripts/sim/world_events.py` | сценарные события стенда: spawn / remove / move модели, blocked (ящик в точке цели) (§13) |
| `scripts/sim/summarize_lockstep.py` | сводка §13 → `lockstep/summary.{json,md}` |

## 11. Ограничения и следующие шаги

- **Карта есть только для `turtlebot3_world`.** Для `turtlebot3_house` Nav2 нужна карта: построить её
  slam_toolbox (пакет установлен с nav2_bringup) или растеризовать геометрию мира. Без этого дом годится только
  для движения по `cmd_vel` и кадров.
- ~~ros-mcp-server не поставлен~~ — поставлен и измерен 07.10 (§12). Осталось: R-compact на Go с событиями
  (§12.7), детектор для `look`, асинхронный `go_to`.
- ~~Синхронный режим с T0 не реализован~~ — пошаговый режим через `/world/default/control` проверен и измерен
  07.10 (§13). Осталось встроить его в реплеер (драйвер стенда, `research/new_topic/robot_scenario.md` §10, §12).
- Все числа здесь — smoke при параллельной работе других агентов. Для главы 3 матрицу (§7) нужно повторить на
  свободной машине, с повторами.

## 12. Доступ агента к роботу через MCP (07.10.2026)

Две реализации MCP-сервера робота на стороне «платы» (VM «sbc»), обе ходят в rosbridge по
`ws://host.lima.internal:9090`:
- **R-generic** — готовый ros-mcp-server ([F20]) без правок: baseline;
- **R-compact** — свой тонкий адаптер (прототип на Python для замеров, `scripts/sim/robot_mcp_compact.py`).

Результаты лежат в `experiments/results/robot_smoke_20261007/`, сводная таблица — в `rosmcp_analysis_sbc/summary.md`.
Все числа — smoke при параллельной работе других агентов (IMPLEMENTATION_SPEC §0 п. 4), это порядок величин, а
не результаты ВКР.

### 12.1. Установка ros-mcp-server

| Что | Значение |
|---|---|
| Пакет | PyPI `ros-mcp` **3.1.2** (23.09.2026) = тег `v3.1.2` репозитория robotmcp/ros-mcp-server (коммит `476591a`). Лицензия **Apache-2.0** (файл LICENSE и метаданные PyPI). Python ≥ 3.10 |
| Внутри | fastmcp 2.14.7, mcp 1.30.0, opencv-python 5.0.0.93, numpy 2.5.3, pillow 12.3.0, websocket-client 1.9.2. Сервер представляется как `ros-mcp-server`, в поле версии отдаёт версию fastmcp (2.14.7). Согласованная ревизия MCP — 2025-11-25 |
| Mac | `.venv-rosmcp` (uv, CPython 3.12.15): 83 пакета, 220 МБ |
| «Плата» (VM sbc) | `~/.venv-rosmcp` (системный python3 3.12.3): 85 пакетов, 260 МБ. uv 0.12.23 лежит в `~/.local/bin` (sha256 проверен), системный Python и apt-пакеты sbc не менялись. Свободно на диске sbc 16 ГБ из 19 |
| Состав | закреплён в `scripts/sim/rosmcp/requirements.lock` (macOS) и `requirements-sbc.lock` (Linux aarch64), ставится через `uv pip sync` (`scripts/sim/install_rosmcp.sh mac\|sbc`, лог `install_rosmcp_*.log`). Для прототипа R-compact добавлен cbor2 6.1.5 |

На «плате» есть подвох: **ros-mcp не стартует на безголовой системе**. Пакет `opencv-python` требует `libGL.so.1`,
а в облачном образе Ubuntu его нет, и сервер падает при `import cv2`. Решение — `opencv-python-headless` той же
версии (тот же модуль `cv2`), так системные пакеты платы не трогаются. На Mac ставится обычный opencv-python.

Доступ из sbc к rosbridge: TCP и `rosapi/topics` (100 тем) через `host.lima.internal:9090` работают, ответ за
32 мс. Проброс порта остаётся на 127.0.0.1, `hostIP: 0.0.0.0` не нужен (§1).

### 12.2. Как проверялось

`scripts/sim/rosmcp_probe.py` — MCP-клиент на python mcp SDK 1.30. На «плате» он запускается тем же venv, что и
сервер.
1. Сервер стартует по stdio через обёртку, которая пишет PID. По PID снимаются PSS, RSS и CPU-время из `/proc`.
2. Между сервером и rosbridge ставится TCP-прокси со счётчиком. Он считает байты в обе стороны (кадры WS и
   HTTP-рукопожатие) и число новых WebSocket-соединений на каждый вызов.
3. Для каждого вызова пишутся задержка, размер ответа MCP (result JSON-RPC), текст для LLM и картинки.
4. Истинная поза (`/ground_truth/pose`) читается до и после движения отдельным соединением мимо прокси.

Перед сценариями с движением симуляция перезапускается (`scripts/sim/rosmcp_smoke.sh`), поэтому робот каждый раз
стартует на dock. Токены и ₽ считает `scripts/sim/rosmcp_analyze.py`: o200k по Cookbook, как mock-llm
(`bench/mockllm/tokens.py`), и замороженный прайс (`bench/costmodel`).

Проверено с «платы» (sbc → host.lima.internal:9090):
- каталог: **31 инструмент, 6 ресурсов, 7 промптов**;
- `connect_to_robot` (ping + порт + определение версии ROS);
- `get_topics` (100 тем);
- `subscribe_once` для `/odom`, `/scan`, `/amcl_pose` и камеры;
- `publish_once` и `publish_for_durations` для `/cmd_vel` — робот едет;
- действие Nav2 `send_action_goal /navigate_to_pose` — **поддерживается** и выполнено (SUCCEEDED, 0,07 м до цели
  по истинной позе);
- `get_action_status`, `get_actions`, `get_action_details`, `subscribe_for_duration`, ресурс
  `ros-mcp://ros-metadata/all`.

### 12.3. ros-mcp-server: цена вызовов (с «платы», `rosmcp_generic_sbc/`)

| Вызов | мс | ответ MCP, Б (текст) | токены текста | rosbridge ↓ / ↑, Б | новых WS | примечание |
|---|---|---|---|---|---|---|
| `connect_to_robot` | 10 | 782 (338) | 96 | 346 / 302 | 2 | ping, проверка порта, rosapi |
| `get_topics` | 9 | 11 870 (5 694) | **1 454** | 6 341 / 329 | 1 | 100 тем + типы |
| `get_topic_type /cmd_vel` | 9 | 204 (60) | 16 | 365 / 366 | 1 | |
| `get_message_details TwistStamped` | 39 | 1 310 (577) | 146 | 2 626 / 424 | 1 | |
| `subscribe_once /odom` | 37 | 1 573 (716) | 468 | 949 / 354 | 1 | позиция в кадре `odom`, а не `map` |
| `subscribe_once /scan` | 73 | 16 088 (7 990) | **4 433** | 8 223 / 358 | 1 | 360 дальностей, `inf` → `null` |
| `subscribe_once /amcl_pose` | 7 | 830 (358) | 235 | 596 / 388 | 1 | latched: приходит и в простое |
| `subscribe_once` камеры (ленивая) | 236–348 / **5 060** | 16 330 / 180 | 82 текст + кадр 640×480 | 17 770 / 412 | 1 / **11** | удалось **4 из 10**; кадр пережат в JPEG ≈12 КБ (§12.4 п. 1) |
| `subscribe_once /camera/image_raw` | 715 (Mac) | 16 400 | кадр 640×480 | **3 714 359** / 378 | 1 | пришло 3 сырых кадра по 1,24 МБ |
| `publish_once /cmd_vel` | **2 015** | 272 (95) | 19 | 591 / 800 | 3 | робот едет, пока не придёт ноль (0,56 м) |
| `publish_once` по примеру из описания | 2 014 | 272 (95) | 19 | 591 / 794 | 3 | `success: true`, **робот стоит** (§12.4 п. 3) |
| `publish_for_durations` 10 Гц, 2 + 0,5 с | 2 511 | 386 (146) | 42 | 197 / 2 480 | 1 | 25 публикаций, проехал 0,38 м |
| `get_actions` | 20 | 802 (344) | 95 | 666 / 345 | 1 | 17 действий (Nav2 и др.) |
| `get_action_details /navigate_to_pose` | 122 | 1 144 (503) | 114 | **27 956** / 1 052 | 1 | |
| `send_action_goal` (timeout 120) | 17 860 | 470 (185) | 58 | **925 450** / 506 | 1 | feedback ≈100/с; CPU сервера 0,27 с; в других прогонах 30,4 с / 1,58 МБ и 18,4 с / 0,95 МБ |
| `send_action_goal` (timeout по умолчанию) | 5 006 | 1 402 (627) | 205 | 261 499 / 508 | 1 | «Action timed out», но **робот доехал** (0,008 м) |
| `get_action_status` | 9 | 452 (177) | 59 | 399 / 426 | 1 | |
| `subscribe_for_duration /scan` 2 с | 2 048 | 185 289 (184 071) | **108 858** | 169 506 / 358 | 1 | 21 скан |
| ресурс `ros-mcp://ros-metadata/all` | 5 030 | 43 522 (38 468) | **10 131** | 20 247 / 1 327 | 4 | |

Процесс на «плате»:
- **PSS 112 МиБ** (RSS 122 МиБ), 15–17 потоков;
- от запуска до ответа на `initialize` — 431 мс (самый первый запуск — 974 мс);
- за весь сценарий — 0,8 с CPU.

Каталог в каждом запросе к LLM — **2 221 токен** (Cookbook; 3 009 по JSON; `tools/list` весит 17,6 КБ). Самые
дорогие инструменты:
- `publish_for_durations` — 233 токена;
- `subscribe_once` — 222;
- `subscribe_for_duration` — 218;
- `call_service` — 143.

### 12.4. Особенности ros-mcp-server 3.1.2, влияющие на производительность

Всё ниже измерено (§12.3) и оставлено как есть: это свойства baseline.
1. **Новое WebSocket-соединение на каждый вызов.** Обёртка `with ws_manager:` закрывает его в конце вызова.
   `WebSocketManager.receive` при таймауте **закрывает соединение**, а следующий `receive` открывает новое, уже
   без подписки. `subscribe_once` ждёт кусками по 0,5 с. Поэтому если первое сообщение приходит позже 0,5 с, вызов
   кончается таймаутом через 5 с и 11 переподключениями. У ленивой камеры первый кадр приходит через 0,4–0,8 с:
   удалось 4 попытки из 10, и только когда камера ещё не «уснула» после прошлой попытки. С камерой в режиме
   `stream` это обходится, но стенд тогда тратит 2,3 ядра вместо 0,85 (§7).
2. **`publish_once` занимает ≥ 2 с.** После `advertise` и после `publish` сервер ждёт ответа rosbridge по 1 с.
   При успехе rosbridge ничего не присылает, поэтому каждое ожидание кончается таймаутом и переподключением
   (3 соединения на вызов). DiffDrive в Gazebo держит последнюю скорость, поэтому «стоп» доходит на ≈1 с позже и
   робот проезжает лишние 0,15 м.
3. **Пример в описании `publish_once` неверен для Jazzy.** Там `msg={'linear': {'x': 1.0}}` с типом
   `TwistStamped`, а нужно `{'twist': {'linear': …}}`. Вызов по образцу возвращает `success: true`, но робот не
   двигается (0,0 м по истинной позе). LLM, повторившая пример, получит ложное подтверждение.
4. **`send_action_goal` всегда просит feedback** (`"feedback": True` в коде). Nav2 шлёт его ≈100 раз/с, поэтому на
   цель уходит 0,93–1,58 МБ трафика rosbridge и 0,27 с CPU сервера на разбор. Результат для LLM при этом весит
   185 Б. Таймаут по умолчанию — 5 с, а цель занимает 15–30 с. Вызов возвращает ошибку «timed out» (с
   `last_feedback`, 205 токенов), хотя робот продолжает ехать и доезжает. Модель видит неудачу там, где её нет:
   это лишние шаги LLM (повтор цели, опрос статуса).
5. **Ответ дублируется:** текст и `structuredContent` с тем же JSON, поэтому ответ MCP примерно вдвое больше
   текста. На токены LLM это не влияет, если клиент берёт только одно из двух, но трафик stdio/HTTP и разбор на
   плате удваиваются.
6. **Тяжёлые зависимости.** Процесс ros-mcp на плате занимает 112 МиБ PSS: fastmcp со своим стеком, плюс
   opencv и numpy, которые нужны только для пересжатия кадров. Прототип R-compact на том же Python (mcp SDK,
   websockets, cbor2) занимает 47–52 МиБ.

### 12.5. Сколько стоит одно сообщение каждой темы в LLM (`topic_tokens/topic_tokens.json`)

Сообщение берётся так, как его отдаёт `subscribe_once`: `{"msg": …}`, компактный JSON rosbridge. Токены — o200k.
₽ — однократная подача на вход gpt-4.1-mini (4,42·10⁻⁵ ₽/токен) без кэша.

| Тема | Б | токены | с округлением float до 3 знаков | ₽ (4.1-mini) |
|---|---|---|---|---|
| `/clock` | 49 | 18 | 18 | 0,0008 |
| `/joint_states` | 249 | 90 | 68 | 0,004 |
| `/tf` | 289 | 109 | 90 | 0,005 |
| `/ground_truth/pose` | 293 | 116 | 79 | 0,005 |
| `/camera/camera_info` | 476 | 247 | 231 | 0,011 |
| `/amcl_pose` | 486 | 273 | 228 | 0,012 |
| `/imu` | 730 | 332 | 234 | 0,015 |
| `/odom` | 685 | 450 | 431 | 0,020 |
| feedback `navigate_to_pose` (одно, ≈100/с) | ≈500 | ≈180 | — | 0,008 |
| **`/scan`** | 8 006 | **4 427** | 2 857 | **0,196** |
| `/camera/image_raw/compressed` текстом (base64) | 16 938 | **10 597** | — | 0,47 |
| `/camera/image_raw` текстом (base64) | 1 228 972 | **774 754** | — | 34,2 (больше контекста gpt-5-mini) |
| `/map` (384×384) | 435 521 | **295 029** | — | 13,0 |

Если кадр отдать картинкой (ImageContent, как делает ros-mcp), счёт идёт по правилам OpenAI
(developers.openai.com/api/docs/guides/images-vision, проверено 07.10.2026). У моделей с патчами 32 px токены
равны `ceil(w/32)·ceil(h/32)·k`. У моделей с плитками 512 px — база плюс плитки, малые кадры не увеличиваются.

| Кадр | gpt-4.1-mini (k 1,62) | gpt-5-mini (k 1,2) | gpt-4.1-nano (2,46) | gpt-5-nano (1,5) | gpt-4o / 4.1 high (low) | gpt-4o-mini high (low) |
|---|---|---|---|---|---|---|
| 640×480 | 486 | 360 | 738 | 450 | 425 (85) | 14 167 (2 833) |
| 320×240 | 130 | 96 | 197 | 120 | 255 (85) | 8 500 (2 833) |

Выводы:
- **сырой `/scan` в LLM — самая дорогая регулярная телеметрия.** 4,4 тыс. токенов и 0,2 ₽ за одно сообщение, а за
  2 с подписки — 109 тыс. токенов. Сводка лидара (§12.7) стоит около 25 токенов, это в 170 раз меньше;
- кадр можно отдавать только картинкой и только уменьшенным: 320×240 обходится в 3,7 раза дешевле 640×480. В
  виде base64-текста кадр стоит 10,6 тыс. токенов (JPEG) или 775 тыс. (raw);
- у gpt-4o-mini картинки дорогие по токенам (база 2 833), так задумано тарифом. Для этого сценария она не
  подходит;
- `/map`, `/joint_states`, `/clock`, `/imu`, `/tf` и feedback навигации агенту не нужны вовсе.

### 12.6. R-compact: прототип и сравнение на задачах

`scripts/sim/robot_mcp_compact.py` (mcp SDK FastMCP, websockets, cbor2) реализует 5 инструментов уровня задачи;
их контракт описан в §12.7. Каталог весит **229 токенов**, против 2 221 у ros-mcp-server, то есть в 9,7 раза
меньше. Устройство адаптера:
- одно постоянное WS-соединение на всю сессию;
- `/amcl_pose` — постоянная latched-подписка, сообщения приходят только при движении;
- `/scan` и кадр запрашиваются по требованию в CBOR;
- Nav2 вызывается без feedback, `/cmd_vel` объявляется один раз при подключении;
- ответ — один TextContent с каноническим JSON.

Процесс на «плате»: PSS 47→52 МиБ, 3 потока, старт 222 мс, 0,26 с CPU за сценарий.

| Вызов R-compact (с «платы», `rosmcp_compact_sbc/`) | мс | текст, Б | токены | rosbridge ↓, Б |
|---|---|---|---|---|
| `status()` | 28–110 | 177–236 | 77–96 | 3 139–3 730 (в JSON без CBOR — 8 765) |
| `look()` — сводка по лидару в поле зрения камеры | 102 | 80 | 25 | 3 138 |
| `look(image=true)` — + JPEG 320×240 q70 ≈4,6 КБ | p50 673 (404–771), **10 из 10** | 124 | 45 + 130 vision | ≈16 000 |
| `go_to("east")` | 16 026 | 69 | 27 | **12 702** (против 925 450) |
| `patrol(["north","dock"])` | 40 835 | 115 | 39 | 36 960 |
| `stop()` | **1,8** | 28 | 10 | 94 ↑ |
| `go_to("kitchen")` (нет такого места) | 1,7 | 321 | 88 | 0 — отказ по enum схемы, до rosbridge не доходит |

Задачи «оракулом», как в T0 (`rosmcp_analysis_sbc/summary.md`). Идеальная модель делает минимальную
последовательность вызовов. Каждый вызов — отдельный запрос к LLM (ReAct), в конце идёт короткий ответ. В
промпте одинаковые system и таблица мест, каталог — свой у каждого варианта. Ответы инструментов взяты из
прогона, ₽ — без кэша.

| Задача | R-generic: вызовы → вход, ток. → ₽ (4.1-mini / 5-mini) | R-compact | Вход generic / compact, раз | rosbridge generic / compact |
|---|---|---|---|---|
| «Где ты?» | `subscribe_once /amcl_pose` → 5 012 → 0,23 / 0,15 | `status` → 841 → 0,041 / 0,028 | 6,0 | 1,0 КБ / 4,3 КБ |
| «Что впереди?» | `subscribe_once /scan` → 9 201 → 0,41 / 0,26 | `status` → 841 → 0,041 / 0,028 | 10,9 | 8,6 КБ / 4,3 КБ |
| «Поезжай к east» | `send_action_goal` → 4 881 → 0,23 / 0,16 | `go_to` → 795 → 0,039 / 0,027 | 6,1 | **926 КБ / 13 КБ** |
| «Посмотри» (кадр пришёл) | `subscribe_once` камеры → 4 796 + 486 vision → 0,24 / 0,15 | `look(image)` → 812 + 130 → 0,046 / 0,030 | 5,6 | 18 КБ / 16–22 КБ |
| «Посмотри» (ленивая камера, 6 из 10) | таймаут 5 с, кадра нет → 0,22 / 0,14 | — | — | — |
| «Обход north → dock» | 2 × `send_action_goal`, 3 запроса LLM → 7 545 → 0,36 / 0,25 | `patrol`, 2 запроса → 810 → 0,041 / 0,028 | 9,3 | 1,85 МБ / 37 КБ |
| «Стоп» | `publish_once` нулевой скорости, 2 с → 4 798 → 0,22 / 0,14 | `stop`, 2 мс → 774 → 0,038 / 0,026 | 6,2 | 1,4 КБ / 0,1 КБ |

Если модель не знает интерфейс робота, для R-generic добавляется обнаружение: `get_topics`, `get_topic_type`,
`get_message_details`, `get_actions`, `get_action_details`. Это ещё 6 запросов LLM, 22,6 тыс. входных токенов и
≈1,0 ₽ (gpt-4.1-mini).

Главный вклад в разницу даёт каталог: 2 221 токен на каждый запрос против 229. Следом идут сырые сообщения в
ответах (`/scan` — 4,4 тыс.) и лишние запросы LLM, когда исполнение пошагово ведёт сама модель (обход). Для профиля
«рабочего дня» с десятками команд это разница в разы по ₽ и токенам. Трафик плата ↔ стенд падает в 50–70 раз на
каждой навигационной цели (926 КБ против 13 КБ, обход — 1,85 МБ против 37 КБ).

Оговорки:
- это оракул, качество живой модели здесь не измеряется;
- R-generic не кэширует обнаружение и не знает мест, поэтому таблица мест в обоих вариантах передаётся в system;
- кэш префикса провайдера выровнял бы цену каталога, но только если каталог стабилен и паузы короче TTL кэша
  (THESIS_CONCEPT H3(в)).

### 12.7. Что нужно агенту: контракт тонкого MCP-адаптера робота (вход для реализации на Go)

Принцип: **в LLM уходит решение-уровень, а не сигнал-уровень.** Всё, что можно посчитать на плате, считается
на плате. Сводки — канонический JSON с округлением до точности датчика: метры до 0,01, градусы целые.

| Инструмент | Схема | Ответ (пример из прогона) | ток. | Как считается на плате | rosbridge за вызов |
|---|---|---|---|---|---|
| `status()` | — | `{"free_m":{"back":0.58,"front":1.0,"left":1.49,"right":0.56},"nav":"idle","nearest":{"deg":-130,"m":0.53},"pose":{"heading_deg":-18,"near":"dock","near_m":0.04,"x":-1.96,"y":-0.49}}` | 77–96 | поза — последняя `/amcl_pose` (событийная latched-подписка) → ближайшее место; лидар — один `/scan` в CBOR → минимум по секторам ±30° / 60–120° / −120…−60° / ≥150° и ближайшее препятствие | 3,1 КБ |
| `go_to(place)` | `place`: enum мест из конфигурации | `{"err_m":0.28,"ok":true,"place":"east","s":16.0,"status":"succeeded"}` | 27 | `navigate_to_pose` без feedback, `action_result` ждётся на плате | 12,7 КБ на 3,7 м (события AMCL) |
| `patrol(places)` | массив enum | `{"legs":[{"place":"north","s":19.4,"status":"succeeded"},…],"ok":true}` | 39 | последовательность целей исполняется локально (идея ROS-LLM [F24]) | ≈5 КБ на метр |
| `stop()` | — | `{"cancelled":"east","ok":true}` | 10 | `cancel_action_goal` + нулевой TwistStamped (топик объявлен заранее) | 94 Б |
| `look(image=false)` | `image`: bool | `{"detector":"none","in_view":{"nearest_m":1.01,"occupied_pct":71,"side":"right"}}` | 25 | лидар в поле зрения камеры (62°) | 3,1 КБ |
| `look(image=true)` | | + JPEG 320×240 q70 ≈4,6 КБ | 45 + 96–130 | один кадр ленивой камеры (CBOR), уменьшение на плате | ≈16 КБ |

Чего в прототипе нет, но адаптеру нужно (по данным §12.3–12.5):
1. **События вместо опроса**, то есть уведомления MCP `resources/updated` для гейта edgectx, как у hw-mcp.
   Нужны такие события:
   - цель завершена (`succeeded`/`aborted`);
   - «застрял»: при активной цели нет продвижения N с;
   - препятствие ближе X м по ходу движения;
   - «низкий заряд» (синтетический);
   - прибытие в место.

   Тогда при долгих действиях LLM не будит ни опрос, ни heartbeat. Сюда же — асинхронный
   `go_to(place, wait=false)`: вызов возвращается сразу, результат приходит событием.
2. **Сводка детектора для камеры.** `{"objects":[{"label":"person","side":"left","m":1.2}]}` — это 20–40 токенов
   вместо 96–486 vision-токенов и без передачи кадра. Детектор работает на плате (малая ONNX-модель) или на
   стороне стенда как «умная камера». Сейчас его место занимает `"detector":"none"` и сводка по лидару.
3. **Прогресс навигации по запросу** — из позы AMCL и расстояния до цели по прямой, в `status().nav`:
   `"to east 8s, 1.2 m left"`. Feedback Nav2 для этого не нужен: rosbridge его не прореживает.
4. **Контекст оборудования L0** в стабильном префиксе:
   - места с координатами;
   - возможности;
   - пределы: скорость до 0,3 м/с, допуск цели 0,25 м;
   - цена инструментов: `go_to` ≈ 4,3–5,5 с на метр (smoke), `look` ≈ 0,7 с и 4,6 КБ, `status` ≈ 0,1 с.

   Это структура, а не свободный текст `robot_specifications` из ros-mcp.
5. **Короткие ошибки.** Сейчас отказ по enum — это текст pydantic на 88 токенов, хватит примерно 15:
   `{"ok":false,"error":"unknown place","known":[…]}`.
6. **Поза после прибытия.** `/amcl_pose` обновляется только каждые 0,25 м или 0,2 рад, поэтому `err_m` после
   остановки бывает устаревшим на величину до 0,25 м (0,28 против 0,19 по истине). Нужно брать свежую позу (tf
   `map→base_footprint`) или убрать `err_m`.
7. **Не отдавать в LLM:**
   - сырые `/scan`, `/odom`, `/amcl_pose`, `/imu`, `/tf`, `/joint_states`, `/clock`, `/map`;
   - кадры base64-текстом;
   - feedback Nav2;
   - интроспекцию ROS (`get_topics`, `get_message_details`): её место — конфигурация адаптера, а не каталог LLM;
   - прямой `cmd_vel` (шлюз приводов, принцип 6 спецификации).
8. **Реализация — Go**, статический бинарник рядом с hw-mcp (THESIS_CONCEPT, решение 2):
   - те же 5 инструментов и события;
   - постоянный WebSocket-клиент rosbridge, CBOR для числовых массивов и кадров;
   - канонический JSON без `structuredContent`, `_meta` оркестратора как в hw-mcp;
   - уменьшение JPEG стандартной `image/jpeg`.

   Python-прототип (47–52 МиБ) служит только эталоном для паритета ответов.

### 12.8. Воспроизведение

```sh
scripts/sim/install_rosmcp.sh mac            # .venv-rosmcp на Mac
scripts/sim/install_rosmcp.sh sbc            # ~/.venv-rosmcp в VM sbc (uv в ~/.local/bin, системный Python не трогается)
scripts/sim/rosmcp_smoke.sh experiments/results/robot_smoke_20261007            # всё с «платы»: generic, compact, cbor, camera, tokens
scripts/sim/rosmcp_smoke.sh OUT --only compact,camera_compact                    # часть; --where mac — всё на Mac
.venv/bin/python scripts/sim/topic_tokens.py --out OUT/topic_tokens              # токены одного сообщения каждой темы
.venv/bin/python scripts/sim/rosmcp_analyze.py --generic OUT/rosmcp_generic_sbc --compact OUT/rosmcp_compact_sbc \
    --camera-generic OUT/rosmcp_camera_generic_sbc --camera-compact OUT/rosmcp_camera_compact_sbc --out OUT/rosmcp_analysis_sbc
```

Файлы в `experiments/results/robot_smoke_20261007/`:

| Каталог | Что внутри |
|---|---|
| `rosmcp_generic_sbc/`, `rosmcp_compact_sbc/` | основные прогоны: `probe.json`, `tools.json`, `steps/` (тексты и кадры каждого ответа), `server_stderr.log` |
| `rosmcp_compact_json_sbc/` | R-compact без CBOR |
| `rosmcp_camera_{generic,compact}_sbc/` | 10 кадров по запросу |
| `rosmcp_generic_mac/` | первый прогон на Mac: единственный, где `subscribe_once /camera/image_raw` удался |
| `topic_tokens/` | токены одного сообщения каждой темы |
| `rosmcp_analysis_sbc/` | `analysis.json`, `summary.md` |

## 13. Пошаговый режим и сценарные события (07.10.2026)

Нужны для ускоренных прогонов рабочего дня робота (T1-gz, `research/new_topic/robot_scenario.md` §10). Часы
симуляции ведёт клиент: мир стоит на паузе, пока агент думает, и продвигается до нужного сим. времени вызовом
`run_to_sim_time`. Результаты — `experiments/results/robot_smoke_20261007/lockstep/` (`summary.md`). Smoke при
параллельной работе других агентов, не результаты ВКР.

**Как устроено.**
- `start_sim.sh --world-control` (launch `world_control:=true`) добавляет в `bridge.yaml` четыре сервиса
  `ros_gz_bridge`:
  - `/world/default/control` (`ros_gz_interfaces/srv/ControlWorld` ↔ `gz.msgs.WorldControl`);
  - `/world/default/create` (`SpawnEntity` ↔ `gz.msgs.EntityFactory`);
  - `/world/default/remove` (`DeleteEntity` ↔ `gz.msgs.Entity`);
  - `/world/default/set_pose` (`SetEntityPose` ↔ `gz.msgs.Pose`).

  Все они доступны через rosbridge `call_service`. По умолчанию выключено: конфигурации §7 не меняются.
- Клиент (`scripts/sim/lockstep_probe.py`) держит одно WS-соединение и подписан на `/clock` с `throttle_rate` 20 мс
  и `queue_length` 1. rosbridge отдаёт последнее значение после интервала, поэтому момент остановки виден.
  Шаг — `{"world_control": {"run_to_sim_time": {sec, nanosec}}}`, затем ожидание `/clock` ≥ цели.

**Результаты** (стенд `--nav2 --rtf 0 --world-control`, лидар 10 Гц, камера ленивая):

| Проверка | Результат |
|---|---|
| Свободный ход RTF 0 (эталон) | 2,69× |
| Шаги dt 0,05 / 0,1 / 0,5 / 1 / 5 с (по 15 сим. с) | 2,20× / 2,44× / 2,65× / 2,66× / 2,68×; накладные расходы ≈3–4 мс на шаг (вызов сервиса + доставка `/clock`) |
| Обход dock → east → north → center_sw → south → dock шагами 0,5 с, 3 повтора | **15 из 15** целей; 2,52–2,55×; 76–96 сим. с за обход |
| Тот же обход при свободном ходе RTF 0 / при RTF 1 (2 повтора) | 5/5, 2,58× / 10/10 |
| Время участка, все 30 участков | медианы 11,5–20,8 с; одинаковый участок — от 17,5 до 26 с; выброс 58,7 с при RTF 1 (восстановление Nav2); модель t ≈ 11,1 + 2,0·L с (L — путь по истине, n = 29, СКО остатка 4,0 с) |
| Ошибка по истинной позе | медиана 0,07 м, максимум 0,25 м (допуск 0,25 м) |
| Пауза 60 с стенного времени | часы стоят; следующая цель — SUCCEEDED за 15,5 сим. с (bond-таймеры Nav2 не срабатывают) |
| Ящик 0,4 м в точке цели east (`world_events.py blocked`) | spawn 7,4 мс; цель ABORTED через 27,5 сим. с, робот встал в 0,56 м от цели; remove 5,0 мс |
| Чтение по запросу на паузе (`lockstep_probe.py reads`) | `/scan` и JPEG — **нет сообщения 4 с**; после шага 0,1 с приходят; `/amcl_pose` (latched) — 3,5 мс |

Выводы для сценария:
1. **Пошаговый режим практически бесплатен.** Ускорение ограничено рендером лидара (≈2,7×), а не управлением.
   При шаге ≥ 0,5 с теряется меньше 2 %.
2. **Nav2 работает в пошаговом режиме** и переживает долгие паузы. Время участков в пошаговом режиме не
   отличается от реального времени сильнее, чем повторы между собой. Но и там разброс большой: Nav2 считает
   асинхронно, и его вычисления не привязаны к шагам мира. Детерминизма это не даёт, поэтому факторы сравниваются
   в T0-модели, а Gazebo калибрует её.
3. **На паузе датчики по запросу не приходят.** MCP-адаптер робота в виртуальных часах:
   - берёт скан из кэша подписки;
   - для кадра делает микрошаг мира (тестовый режим);
   - `go_to` у него асинхронный, иначе ход агента и шаг часов блокируют друг друга.
4. **При RTF 1 и нагрузке хоста** часть участков шла с фактическим RTF 0,47–0,82
   (`tour_free_rtf1_r1.json`). Для T2 нужна проверка RTF по журналу прогона.
5. При RTF 0 `collision_monitor` дважды отбросил скан старше 0,2 с сим. времени и на миг остановил робота.
   Это артефакт ускорения, который отличает его от реального времени.

Воспроизведение:

```sh
scripts/sim/start_sim.sh --nav2 --rtf 0 --world-control
OUT=experiments/results/robot_smoke_20261007/lockstep
.venv/bin/python scripts/sim/lockstep_probe.py --out $OUT/free.json free --seconds 15
.venv/bin/python scripts/sim/lockstep_probe.py --out $OUT/step.json step --dt 0.05 0.1 0.5 1 5 --total 15
.venv/bin/python scripts/sim/lockstep_probe.py --out $OUT/tour_lockstep_dt0.5.json tour --dt 0.5
.venv/bin/python scripts/sim/lockstep_probe.py --out $OUT/tour_free_rtf0.json tour --free
.venv/bin/python scripts/sim/lockstep_probe.py --out $OUT/hold60_r2.json hold --seconds 60 --places north
.venv/bin/python scripts/sim/lockstep_probe.py --out $OUT/paused_reads.json reads
.venv/bin/python scripts/sim/world_events.py --out $OUT/blocked_east.json blocked --place east
scripts/sim/stop_sim.sh && scripts/sim/start_sim.sh --nav2 --rtf 1 --world-control
.venv/bin/python scripts/sim/lockstep_probe.py --out $OUT/tour_free_rtf1_r1.json tour --free
.venv/bin/python scripts/sim/summarize_lockstep.py $OUT
```

`hold60_r1_clockcheck_bug.json` — первый прогон паузы. В нём «часы стоят» проверялось сразу после команды паузы,
до того как пришли последние прореженные `/clock`. Ошибка проверки исправлена (ожидание 0,5 с), цель после
паузы в том прогоне тоже выполнена.
