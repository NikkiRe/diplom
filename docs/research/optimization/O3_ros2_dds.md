# O3. ROS 2 и DDS на роботе (Foxy, Fast DDS): задержка «жест → команда» и CPU

Срез: 20.10.2026. Только исследование: код, конфиги и документы проекта не менялись.

Пометки:
- [есть] / [частично] / [нет] — сделано ли это в проекте;
- [код] — проверено по исходникам (наши или upstream, ветка foxy);
- [замер] — число из наших прогонов на двойнике (ориентир, не результат);
- [оценка] — вывод без замера;
- [гипотеза] — механизм правдоподобен, нужна проверка.

Что сверено:
- `neurobot/safety/node.py`, `neurobot/executor/node.py`, патч `neurobot/perception/predict_live_v2.patch` и `camera_guard.py`,
  `neurobot/ui/node.py` и `fastdds_udp.xml`;
- `neurobot/deploy` (README, `neurobot.env.example`, юниты), `neurobot/sim/twin/launch/twin.launch.py`;
- `experiments/results/neurobot_20261018_acceptfix2/compare.md`, `neurobot_20261016_budget/report.md` и `rmw_delta.txt`;
- `docs/neurobot/PLAN.md` §2–5, §13, `docs/neurobot/research/R5_external.md` §2 (rosbridge уже разобран там — не повторяю).

Upstream — по исходникам веток foxy:
- rclpy 1.0.13;
- rmw_fastrtps 1.3.2 (Fast DDS 2.1.4);
- rosidl_python;
- rosbridge_suite 1.3.1 и, для сравнения, 2.7.1;
- realsense-ros 4.51.1;
- imu_tools 2.0.3;
- ros2_controllers foxy.

Версии взяты из `rosdistro/foxy/distribution.yaml`. Что реально стоит на Jetson лаборатории, покажет `collect_diag.sh`.

## 0. Коротко

1. **DDS — не узкое место K1.** На двойнике:
   - скан → ноль в `/cmd_vel`: 0,7 мс p95;
   - стоп агента через rosbridge → ноль: 2,7 мс p95 [замер, k6_decisions_n30].

   При этом K1 = 0,67 / 0,95 с (p50 / p95). Его задают END_GAP 0,5 с и цикл распознавания. Даже если на Nano доставка
   медленнее в 10 раз, доля ROS/DDS в K1 — не больше 1–3 % [оценка]. **Довести K1 до p95 ≤ 0,5 с правкой DDS нельзя.**
   От ROS-слоя выигрыш другой: CPU, память, джиттер реакции safety_gate и честность замеров.
2. **Главный расход CPU в нашем ROS-слое — накладные rclpy на каждый колбэк, а не DDS.**
   - safety_gate на двойнике — ≈ 55,7 % ядра A57 по пересчёту бюджета 16.10 [замер ВМ + пересчёт; калибровка CPU помечена
     ненадёжной].
   - Он получает ≈ 560–590 событий/с. Из них ≈ 400 — две темы IMU по 200 Гц (`/camera/imu` и `/imu/data`).
   - Литература говорит то же: SingleThreadedExecutor в rclpy дорог (rclpy #1389, Bit-Bots 2023 и 2025). EventsExecutor для
     rclpy есть только в Kilted/Rolling и бэкпорте в Jazzy — на Foxy его нет.
3. **Дешёвые правки на Foxy, которые реально дают выигрыш** (подробно — §3):
   - свой SingleThreadedExecutor вместо `rclpy.spin_once(node)`. На Foxy эта функция на каждом вызове добавляет и убирает узел
     из глобального исполнителя [код];
   - меньше событий у safety_gate: профиль глубины 424×240×15, одна тема IMU вместо двух, `/odom` реже;
   - `KEEP_LAST 1` для потоков и команд;
   - сторож свежести камеры по `camera_info`;
   - `publish_tf:=false` у madgwick;
   - `start_parameter_services=False`.
4. **Двойник не отражает DDS-слой робота.**

   | | Двойник | Робот |
   |---|---|---|
   | ROS 2 | Jazzy | Foxy |
   | DDS | Cyclone DDS (без SHM, буфер 16 МБ) | Fast DDS 2.1.4 (SHM по умолчанию, сегмент 512 КиБ — меньше кадра 0,9 МБ) |
   | rosbridge | 2.7.1, spin в отдельном потоке | 1.3.1, `spin_once(0.01)` в цикле tornado каждую 1 мс |
   | Python | 3.12 | 3.8 |

   Задержки и CPU DDS и rosbridge с двойника на робот не переносятся. Нужен прогон в контейнере Foxy — PLAN его уже
   предусматривает (с разрешения).
5. **Найдено расхождение конфигов.** На двойнике madgwick запущен с `publish_tf: False`, а в `neurobot.env.example` для робота
   этого флага нет. По умолчанию в Foxy madgwick шлёт TF `odom → кадр IMU` с частотой IMU, 200 Гц [код]:
   - лишний поток `/tf`;
   - у кадра IMU появляется второй родитель.
6. **Безопасность DDS.** Граф открыт сети лаборатории (RS11). На Foxy с Fast DDS 2.x `ROS_LOCALHOST_ONLY=1` **сохраняет SHM**
   [код rmw_fastrtps foxy]: по скорости локальный граф ничего не теряет. Если нужны RViz и геймпад с ПК — фаервол на
   DDS-порты или SROS2. Цена SROS2: рукопожатия при старте и шифрование. Один источник даёт ×1,55 к средней задержке —
   312 → 485 мкс.

## 1. Граф и QoS сейчас

Частоты робота — по умолчаниям драйверов Foxy: realsense 4.51.1 с `profile 0,0,0`, то есть умолчания устройства; DDC
`publish_rate` 50. Это ещё [допущение] до `collect_diag`. На двойнике — по `twin_config.py`.

| Тема | Кто пишет → кто читает (наши) | Гц робот / двойник | QoS у нас | Вердикт |
|---|---|---|---|---|
| `/camera/color/image_raw` (0,9 МБ) | realsense → perception (`--camera ros`), сторож (raw) | 30 / 15 | sensor_data (BE, depth 5) | профиль не задан; depth 5 — лишний |
| `/camera/depth/image_rect_raw` (0,8 МБ при 848×480) | realsense → safety | 30 / 15 (424×240) | sensor_data | профиль не задан; разбор всех 30, обработка ≤ 10 (`depth_hz`) |
| `/camera/depth/camera_info` | realsense → safety | 30 / 15 | sensor_data | читается на каждом кадре ради смены параметров |
| `/camera/imu` + `/imu/data` | realsense, madgwick → safety | 200 + 200 / 200 + 200 | sensor_data | ≈ 70 % событий safety |
| `/odom` | DDC → safety, executor | 50 / 50 | sensor_data | нужно ≈ 10–20 |
| `/scan` | urg_node2 → safety, executor, perception | 10 / 10 | sensor_data | depth 5 → до 0,5 с старых сканов в очереди |
| `/cmd_vel_gesture` → `/neurobot/cmd_vel_req` → `/cmd_vel` | perception → executor → safety → twist_mux | 10–20 | reliable, volatile, depth 10 | depth 10 — лишний |
| `/neurobot/safety_state`, `/motion_state` | safety, executor → … | 5 / 2 | reliable depth 10, чтение BE | норм. |
| `/neurobot/estop`, `/neurobot/stop_now` | CLI, пульт, rosbridge → safety | события | reliable volatile depth 10; у rosbridge 1.3.1 издатель transient_local, depth 100, lifespan 1 с | норм. — подписчик volatile, повтора старого нет |
| `/tf_static` | rsp, realsense → safety | latched | reliable transient_local depth 100 | норм. (как у tf2) |

Ожидание событий в узлах:
- safety_gate, executor и perception крутят `while …: rclpy.spin_once(node, timeout_sec=0.1)` — глобальный исполнитель;
- пульт (`ui/node.py`) и rosgraph деплоя держат свой `SingleThreadedExecutor`;
- в пульте уже `start_parameter_services=False`.

## 2. Вердикты по принятым решениям

| Решение | Вердикт | Почему |
|---|---|---|
| Каждый компонент — отдельный процесс, без composition | адекватно | В rclpy нет intra-process и zero-copy. realsense с intra-process не поддерживает узлы на rclpy и отключает `/static_tf` (README 4.51.1) — а safety берёт позы камеры из `/tf_static`. Цена — +4,1–5,4 МиБ анонимной памяти на участника Fast DDS [замер, `rmw_delta.txt`]. |
| `std_msgs/String` + JSON для своих тем | адекватно | Нужен rosbridge 1.3.1 и два дистрибутива. Частоты 1–5 Гц, разбор JSON на этом фоне не виден. |
| QoS сенсоров (best effort) на входах датчиков | адекватно | Совместим и с reliable-издателем. По Kronauer 2021 (Foxy, RPi4) RELIABLE даёт до +15 % задержки на крупных сообщениях. Для Wi-Fi ROS 2 docs советуют best effort. |
| Глубина 5–10 у потоков и команд | спорно | Свежесть считается по времени приёма (`time.monotonic()`), а не по `header.stamp`. Отстал колбэк — из очереди придут сканы до 0,5 с давности, и каждый посчитается свежим. Для потоков и команд нужен `KEEP_LAST 1`. |
| Цикл `rclpy.spin_once(node, timeout_sec=0.1)` | спорно | В Foxy это `add_node` → `spin_once` → `remove_node`. Каждое добавление и удаление дёргает guard: ожидание лишний раз просыпается впустую и заново собирает wait set в Python. На каждую пачку событий создаётся новый rcl-таймер для таймаута [код rclpy foxy `__init__.py:168–173`, `executors.py:451–620`]. |
| SingleThreadedExecutor, не MultiThreaded | адекватно | GIL не даёт параллелизма в `take` (rclpy #1025), а один поток проще для safety. |
| Fast DDS на роботе, RMW не менять | адекватно | Kronauer 2021 (Foxy, RPi4): FastRTPS заметно быстрее Connext и Cyclone. В Foxy у Cyclone 0.7 нет SHM (iceoryx — с Galactic), кадры пошли бы через loopback UDP с фрагментацией. |
| Двойник на Cyclone + rosbridge 2.7.1 | спорно для DDS-чисел; адекватно для логики | Счётчики касаний, отказов и вызовов LLM переносятся. Задержки DDS и rosbridge, CPU и память DDS — нет (см. §0 п. 4). |
| Пульт — только UDP (`fastdds_udp.xml`) | адекватно | SHM Fast DDS между разными пользователями не передаёт данных [замер `foxy_smoke.sh`]. Это известное ограничение SHM. |
| `ROS_LOCALHOST_ONLY` выключен (RS11) | спорно, вынужденно | Стоп, защёлку и геймпад, который в twist_mux главнее нас, может прислать любой ПК сети. Решение — за лабораторией. |
| rosbridge: 127.0.0.1, глобы, одно WS, без вызова сервисов в цикле | адекватно [есть] | Разобрано в R5 §2. Добавка в §3 п. 13. |
| madgwick на роботе без `publish_tf:=false` | ошибочно | Умолчание Foxy — `publish_tf=true`, `fixed_frame=odom` [код imu_tools foxy, `imu_filter_ros.cpp:49–53, 390–413`]. Итог — 200 Гц `/tf` и второй родитель у кадра IMU. На двойнике флаг выставлен. |
| Профили камеры в env не заданы | устарело | В README safety уже стоит совет 424×240×15 для глубины, но в `NEUROBOT_DRIVER_camera_CMD` его нет. По умолчанию — профиль устройства, для глубины D435i обычно 848×480×30 [допущение]. |
| Свежесть камеры у сторожа — по `image_raw` (raw) | спорно | Комментарий в env сам называет дешёвый вариант — `camera_info`. |
| safety читает обе IMU-темы по 200 Гц | спорно по CPU | Это решение безопасности: наклон берётся из madgwick, удар — из сырого ускорения. Но в `/imu/data` уже лежит то же сырое ускорение: при `remove_gravity_vector=false` madgwick копирует входное сообщение [код `imu_filter_ros.cpp:448`]. |

## 3. Рекомендации

Эффект оценён на нашей метрике: CPU и RAM на Nano, задержки, непрерывность. Везде, где не сказано иное, применимо к Foxy,
Python 3.8 и Jetson Nano.

### Высокий приоритет

1. **Свой исполнитель вместо `rclpy.spin_once(node)`** — [нет]. Касается safety_gate, executor и perception.
   - Что: создать `SingleThreadedExecutor()` один раз, `add_node` один раз, в цикле — `ex.spin_once(timeout_sec=0.1)`.
     Дешевле всего — `ex.spin()` в отдельном потоке и `ex.shutdown()` по сигналу: тогда нет таймера таймаута на каждую
     пачку. Ноль на выходе сохранить: сначала shutdown исполнителя, потом `halt()` и публикация нуля.
   - Эффект: уходят лишняя сборка wait set и лишний `rcl_wait` на пачку, плюс создание rcl-таймера. Это 5–15 % CPU этих
     узлов [оценка по коду]. На K1 влияния нет.
   - Трудоёмкость: ≈ 10 строк на узел и прогон тестов под 3.8 — 0,5 дня.
   - Источник: [код] rclpy foxy.
2. **Меньше событий у safety_gate** — главный рычаг по CPU.
   - Профили realsense в env [частично]: `depth_module.profile:=424x240x15` — как на двойнике и как советует README safety.
     Цвет — `rgb_camera.profile:=640x480x30` или `x15`, по замеру (п. 8). Названия параметров — для 4.51.1; у 3.x
     параметры `depth_width/height/fps`.
   - Одна тема IMU [нет]. Удар брать из `linear_acceleration` в `/imu/data`: madgwick копирует сырое ускорение.
     Подписку на `/camera/imu` снять: −200 событий/с.
     - Минус: если madgwick умрёт, устареют обе проверки. Это уже `stale_imu` → защитная реакция.
     - Нужно проверить, что у лаборатории `remove_gravity_vector=false`.
     - Решение владельца безопасности; перепрогнать наборы `stop_shock`, `slope_latch`, `ramp`.
     - Альтернатива дороже по риску: без madgwick, наклон по акселерометру (`tilt_from_accel` уже есть), accel 63 или
       250 Гц. Тогда минус ещё один процесс, но меняется фильтрация наклона.
   - `/odom` 50 → 20 Гц [нет]. Это параметр `publish_rate` DDC лаборатории — пункт для LAB_HANDOUT. Минус 60 событий/с
     на два наших узла.
   - Эффект: ≈ 565–585 → ≈ 315–345 событий/с, минус 10–25 % ядра A57 [оценка из пересчёта бюджета 16.10].
     Профиль глубины к тому же убирает разбор ≈ 24 МБ/с в Python и снимает нагрузку с самого realsense.
   - Трудоёмкость: env — минуты; IMU — 1 день с перепрогоном наборов.
   - Источники: [код] madgwick и realsense; Bit-Bots 2025 (TF в rclpy съедает ядро); rclpy #1389.
3. **`-p publish_tf:=false` у madgwick в `neurobot.env.example`** — [нет на роботе; на двойнике есть].
   - Эффект: минус 200 Гц `/tf` (и через Wi-Fi в RViz ПК), корректное дерево TF, на двойнике и роботе одинаковый граф.
   - Трудоёмкость: одна строка. В `collect_diag` добавить `param get /imu_filter_madgwick publish_tf` и
     `topic info -v /tf`.
4. **Проверить кадры больше сегмента SHM на Fast DDS 2.1** — [нет], [гипотеза].
   - Суть: сегмент SHM по умолчанию 512 КиБ, а кадр цвета 0,9 МБ, глубины 848×480 — 0,8 МБ. Документация Fast DDS прямо
     предупреждает: сегмент меньше данных или близкий к ним «poses a high risk of data loss». На двойнике (Cyclone)
     этого не видно.
   - Проверка в контейнере Foxy: свой издатель Image 640×480 с известным узором 30 Гц, подписчик на Python сверяет
     содержимое и частоту. Отдельно — частота кадров perception против частоты realsense.
   - Если подтвердится: XML-профиль только для участника камеры — SHM с `segment_size` порядка 8–16 МБ плюс UDPv4,
     `useBuiltinTransports=false`, формат как в `ui/fastdds_udp.xml`. Сегмент держит **отправитель**, поэтому
     подписчикам менять ничего не надо (док. Fast DDS 2.1).
   - Эффект: без потерь и порчи кадров. Порча кадров тихо бьёт по распознаванию, а значит, по «верной реакции».
   - Трудоёмкость: 0,5–1 день.

### Средний приоритет

5. **`KEEP_LAST 1` для потоков и команд** — [нет].
   - Какие подписки: `/scan`, глубина, цвет; `/cmd_vel_gesture`, `/cmd_vel_agent`, `/neurobot/cmd_vel_req`; издатель
     `/cmd_vel`. IMU оставить 5: для удара нужны все отсчёты. События (`estop`, `stop_now`, `gesture`) оставить 10.
   - Эффект:
     - под нагрузкой обрабатывается последний скан или кадр, а не очередь: до 0,5 с у скана, до 166 мс у кадров 30 к/с.
       Это джиттер K1 и реакции safety;
     - минус (depth−1) × 0,9 МБ буферов на каждую подписку на кадры [оценка].
   - Минус: счётчики окон сканов (`mask_scans`, `fault_scans`) под нагрузкой увидят меньше сканов. Перепрогнать
     набор safety.
   - Трудоёмкость: 0,5 дня с тестами.
   - Источники: Fast DDS «Large data» (KEEP_LAST и best effort для потоков); [код] наш.
6. **Сторож свежести камеры — по `/camera/color/camera_info:2`** — [частично: только в комментарии env].
   - Эффект: минус ≈ 27 МБ/с копий при 30 к/с (≈ 14 при 15) и минус 15–30 колбэков/с в стороже.
   - Трудоёмкость: одна строка.
7. **`start_parameter_services=False` в safety_gate и executor** — [частично: есть в пульте].
   - Эффект: из ≈ 22 сущностей wait set safety уходят 6 сервисов. При ≈ 570 пробуждениях/с это минус 3–7 % ядра A57
     [оценка].
   - Минус: не работает `ros2 param get` для этих узлов. Наш doctor и collect_diag его не используют [код].
     Параметры через `--ros-args -p` работают и без сервисов.
8. **Raw-подписка на кадры с разбором только нужных** — [нет]. Касается perception `--camera ros` и глубины в safety.
   - Суть: в Foxy приём `uint8[]` идёт так [код rosidl_python foxy]: rmw → C-структура, потом `PyBytes_FromStringAndSize`,
     потом `array.frombytes`. Это три копии на кадр плюс объект Python. Делается до колбэка — значит, и для кадров,
     которые `depth_hz` или «последний побеждает» потом выбросят.
   - Что сделать: `raw=True` (есть в Foxy) и `rclpy.serialization.deserialize_message` — только для принятого кадра.
     Ещё дешевле — разбор CDR `sensor_msgs/Image` и `np.frombuffer` по смещению.
   - Эффект: минус ⅔ копий на выброшенных кадрах, меньше удержания GIL в потоке spin. Это 2–5 % ядра A57 на каждую
     подписку при 30 к/с [оценка]. После п. 2 (15 к/с) выигрыш меньше.
   - Трудоёмкость: 1 день с тестами под 3.8.
   - Источники: rclpy #763 и rosidl_python #129 — медленным был путь публикации, его ускорили в Galactic, в Foxy
     бэкпорт закрыт; приёмный путь в Foxy — по коду.
9. **Замеры DDS и rosbridge в контейнере Foxy** — [частично: в PLAN «с разрешения»].
   - Что мерить:
     - задержки по звеньям цепочки (п. 15);
     - CPU safety, executor и perception (`py-spy top`, `pidstat -t`);
     - rosbridge 1.3.1: CPU в простое и задержка стопа агента. `spin_once(0.01)` блокирует цикл tornado до 10 мс
       [код], на двойнике с 2.7.1 этого нет;
     - кадры больше сегмента SHM (п. 4);
     - `ls /dev/shm` после серии `kill -9` (п. 14).
   - Эффект: числа DDS-слоя становятся адекватными роботу.
   - Трудоёмкость: 1–2 дня. Загрузка образа — с разрешения.
10. **Безопасность DDS (RS11)** — [нет].

    | Вариант | Цена | Что с инструментами лаборатории |
    |---|---|---|
    | (а) `ROS_LOCALHOST_ONLY=1` | ноль: SHM сохраняется [код rmw_fastrtps foxy `participant.cpp:158–171`] | RViz и геймпад ПК отваливаются; смотреть через пульт и SSH-туннель |
    | (б) фаервол (nftables) на wlan0: DDS-порты (7400 + 250 × domain …) открыты только IP ПК | CPU ≈ 0 | остаются; трудоёмкость низкая, нужна проверка с RViz |
    | (в) SROS2 | рукопожатия на каждую пару участников при старте [оценка: холодный старт K9 вырастет, мерить]; шифрование | ключи нужны всем участникам, включая ПК |

    - SROS2 работает с deb-пакетами Foxy (sros2 foxy, SROS2_Linux.md).
    - Цену шифрования можно снизить governance: `data_protection_kind` NONE для сенсорных тем, SIGN или ENCRYPT для
      команд.
    - Данные о накладных: Mayoral-Vilches 2025 — средняя задержка 312 → 485 мкс (×1,55), с VPN 1310 мкс. Железо
      в этом разделе не указано.

    Для ВКР — описать угрозу и выбрать вариант вместе с лабораторией.
11. **Фиксировать режим питания при замерах** — [частично: collect_diag снимает `nvpmodel -q`, `jetson_clocks --show`].
    - Что: `nvpmodel -m 0` и `jetson_clocks` на время серий, режим — в манифест прогона.
    - Почему: задержки ROS 2 сильно зависят от частот и состояний простоя CPU. Kronauer 2021: задержка падает с ростом
      частоты публикации. Bit-Bots 2023: отключение idle-состояний влияет сильнее, чем выбор DDS. Плюс троттлинг Nano —
      R9 в ANALYSIS.
    - Эффект: воспроизводимость K1 и CPU.

### Низкий приоритет

12. **rosbridge без rosapi в Э3** — [нет]. robot-mcp на Go rosapi не зовёт [код: в `robotmcp` нет обращений к rosapi,
    кроме testdata]. MCP лаборатории в пакете 1 зовёт `/rosapi/topics`, поэтому только для Э3.
    - Эффект: минус один процесс Python, ≈ 30–50 МиБ [оценка].
    - Как: запуск `rosbridge_websocket` без launch-файла, который поднимает и `rosapi_node`.
13. **Подписки robot-mcp через rosbridge: `queue_length 1`, `throttle_rate`, `cbor-raw`** — [частично: CBOR и throttle
    уже есть].
    - `cbor-raw` в 1.3.1 есть [код `subscribe.py`]: rosbridge отдаёт CDR без конвертации.
    - При 1–5 Гц эффект мал. Смысл есть только для `/scan` во время движения.
    - Публиковать в rosbridge-темы с transient_local нельзя в паре с transient_local-подписчиками: lifespan 1 с означает
      повтор команд секундной давности после рестарта подписчика. Сейчас подписчики volatile — [есть]; это инвариант,
      записать в README.
14. **`fastdds shm clean` после аварийных остановок** — [нет], [гипотеза].
    - Суть: после `kill -9` и SIGABRT от сторожа сегменты SHM в `/dev/shm` (tmpfs, то есть RAM) могут оставаться.
      В Fast DDS 2.1.0 команда есть (`tools/fastdds/shm/clean.py`). Ставит ли её deb-пакет Foxy — проверить
      `ls /opt/ros/foxy/bin/fastdds`.
    - Куда: `ExecStartPre=-…` в юнитах или таймер сторожа.
    - `RemoveIPC=` systemd не годится: все юниты работают от одного пользователя.
15. **Задержки по звеньям на роботе — одним наблюдателем** — [частично: наблюдатель есть на двойнике].
    - Один узел подписан на `/cmd_vel_gesture`, `/neurobot/cmd_vel_req`, `/cmd_vel`, `/diff_cont/cmd_vel_unstamped` и
      пишет монотонное время прихода. Разности — задержка звена, собственная задержка наблюдателя сокращается.
    - Плюс возраст кадра на входе YOLO: `time.time() − header.stamp` (realsense ставит системное время).
    - Это даёт «стадии» для ПЗ без ros2_tracing. На Foxy ros2_tracing не включён в deb, требует сборки rcl и rclcpp и не
      инструментирует rclpy (README ros2_tracing foxy).
16. **Приоритет CPU для цепочки движения** — [нет; есть только у пульта: `CPUWeight=20`, `Nice=10`].
    - Что: safety и executor — `CPUWeight` выше и `Nice=-5`; perception — ниже.
    - RT-политику (SCHED_FIFO) для Python не ставить: зависание займёт ядро.
    - Эффект: меньше джиттер реакции safety при пиках perception. На средних нагрузках выигрыша нет [оценка].
    - Основание: Teper et al. 2024 (RPi 4B, Humble) — приоритетное планирование вместо умолчания убирает замедление
      горячего пути в 2,6 раза; Sobhani et al. 2024 (Jetson AGX Xavier); обзор Casini et al. 2025/26.
17. **SYNCHRONOUS publish mode у наших издателей** — [нет].
    - Как: на Foxy только через XML и `RMW_FASTRTPS_USE_QOS_FROM_XML=1`. Переменной `RMW_FASTRTPS_PUBLICATION_MODE`
      в Foxy нет [код и README rmw_fastrtps foxy; по умолчанию ASYNCHRONOUS].
    - Эффект: доли миллисекунды на звено [оценка].
    - Риски:
      - при `USE_QOS_FROM_XML=1` политику памяти истории тоже надо задать в XML, иначе для неограниченных типов
        (String, Image) — риск [проверить];
      - синхронная запись тормозит колбэк, если есть удалённый reliable-читатель за Wi-Fi.

    Не делать до замера по п. 15.

## 4. Не применимо на Foxy или не нужно

| Что | Почему нет |
|---|---|
| Zero-copy: loaned messages rmw_fastrtps, Fast DDS data sharing, Cyclone + iceoryx, Agnocast (ISORC 2025) | Начиная с Galactic или Humble, только C++; у нас потребители кадров на Python |
| Intra-process и composition для наших узлов | В rclpy нет intra-process. Слить safety и executor в один процесс — выигрыш ≈ одно звено (≤ 1–3 мс) и ≈ 30–60 МиБ, но теряется изоляция safety |
| Intra-process realsense и madgwick в одном контейнере | realsense в этом режиме отключает `/static_tf`, safety останется без поз камеры |
| EventsExecutor (rclpy или rclcpp) | rclpy — Kilted/Rolling и бэкпорт Jazzy (#1435); rclcpp — с Iron/Jazzy; на Foxy только через пересборку ROS |
| MultiThreadedExecutor в Python ради скорости | GIL (rclpy #1025) |
| Перевести робот на Cyclone | В Foxy Cyclone 0.7 без SHM; на RPi4 и Foxy FastRTPS быстрее (Kronauer 2021); лаборатория и её ПК на Fast DDS |
| rosbridge 2.x или RWS на Foxy | 2.x требует rclpy Humble+; RWS — Galactic+, Foxy не заявлен |
| `python -O` ради ускорения сообщений | В сгенерированных классах Foxy проверки стоят под `if __debug__`, но приём крупных массивов идёт по быстрому пути `frombytes`. Выигрыш мал, а `-O` вырезает `assert` и в коде лаборатории [оценка] |

## 5. Что взять в ПЗ

- **Обзор (гл. 1).** Задержка ROS 2 зависит от DDS. Оверхед ROS 2 над DDS — до 50 % на малых сообщениях. На ARM-платах
  при перегрузке латентность растёт скачком (Kronauer 2021). Исполнители и приоритеты — Casini 2025/26, Teper 2024.
  Python-узлы — узкое место по CPU (rclpy #1389, Bit-Bots).
- **Вывод с числами.** Место замыкания контура важнее транспорта. DDS-часть рефлекса на двойнике — ≤ 1 мс на звено, на фоне
  END_GAP 0,5 с. Так ВКР показывает, что рефлекс на борту без LLM и rosbridge (PLAN, принцип 1) выбран правильно.
- **Отличия стенда.** Таблицу «двойник против робота» из §0 п. 4 — в раздел «угрозы валидности».

## 6. Источники

Научные:
1. T. Kronauer, J. Pohlmann, M. Matthé, T. Smejkal, G. Fettweis. Latency Analysis of ROS2 Multi-Node Systems. IEEE MFI
   2021, pp. 1–7. arXiv:2101.02074. https://arxiv.org/abs/2101.02074 — Foxy, i7 и Raspberry Pi 4, FastRTPS 2.0.1,
   Cyclone 0.6, Connext 6.0.1.
2. D. Casini, J.-J. Chen, J. Li, F. Reghenzani, H. Teper. A Survey of Real-Time Support, Analysis, and Advancements in
   ROS 2. arXiv:2601.10722 (v1 12.2025, v2 04.2026). https://arxiv.org/abs/2601.10722
3. H. Teper, O. Bell, M. Günzel, C. Gill, J.-J. Chen. Bridging the Gap between ROS 2 and Classical Real-Time Scheduling
   for Periodic Tasks. arXiv:2408.03696, 2024. https://arxiv.org/abs/2408.03696 — RPi 4B, Humble, Autoware reference
   system: умолчание медленнее в 2,6 раза на горячем пути.
4. H. Sobhani, H. Choi, H. Kim. Timing Analysis and Priority-driven Enhancements of ROS 2 Multi-threaded Executors.
   arXiv:2408.08440, 2024. https://arxiv.org/abs/2408.08440 — Jetson AGX Xavier.
5. D. Casini, T. Blaß, I. Lütkebohle, B. Brandenburg. Response-Time Analysis of ROS 2 Processing Chains under
   Reservation-Based Scheduling. ECRTS 2019. DOI 10.4230/LIPIcs.ECRTS.2019.6 — классика семантики исполнителя.
6. C. Bédard, I. Lütkebohle, M. Dagenais. ros2_tracing: Multipurpose Low-Overhead Framework for Real-Time Tracing of
   ROS 2. IEEE RA-L 7(3), 2022, pp. 6511–6518. arXiv:2201.00393. https://arxiv.org/abs/2201.00393
7. S. Lee, T. Kim, J. Chae, K.-J. Park. Optimizing ROS 2 Communication for Wireless Robotic Systems. arXiv:2508.11366,
   2025. https://arxiv.org/abs/2508.11366 — фрагментация IP и крупные сообщения по Wi-Fi.
8. J. Zhang, X. Yu, S. Ha, J. Peña Queralta, T. Westerlund. Comparison of Middlewares in Edge-to-Edge and Edge-to-Cloud
   Communication for Distributed ROS 2 Systems. J Intell Robot Syst 110, 162 (2024). arXiv:2309.07496.
9. J. Kim, J. M. Smereka, C. Cheung, S. Nepal, M. Grobler. Security and Performance Considerations in ROS 2: A Balancing
   Act. arXiv:1809.09566, 2018.
10. V. Mayoral-Vilches, R. White, G. Caiazza, M. Arguedas. SROS2: Usable Cyber Security Tools for ROS 2. IROS 2022.
    arXiv:2208.02615.
11. V. Mayoral-Vilches. Offensive Robot Cybersecurity (PhD thesis). arXiv:2506.15343, 2025, §5.4.4, рис. 5.4 — DDS
    Security ×1,55 к средней задержке.
12. T. Ishikawa-Aso, S. Kato. ROS 2 Agnocast: Supporting Unsized Message Types for True Zero-Copy Publish/Subscribe IPC.
    ISORC 2025. arXiv:2506.16882 — как пример zero-copy, на Foxy неприменим.

Документация, код, issues:
- rosdistro foxy (версии пакетов): https://raw.githubusercontent.com/ros/rosdistro/master/foxy/distribution.yaml
- rclpy foxy, `__init__.py` (`spin_once`), `executors.py`, `node.py` (`raw`, `start_parameter_services`):
  https://github.com/ros2/rclpy/tree/foxy/rclpy/rclpy
- rclpy #763 (публикация крупных данных в 30–100 раз медленнее rclcpp): https://github.com/ros2/rclpy/issues/763 ;
  rosidl_python #129 (буферный протокол; Galactic, бэкпорт в Foxy #148 закрыт): https://github.com/ros2/rosidl_python/pull/129
- rosidl_python foxy, `_msg_support.c.em` (приём через `frombytes`):
  https://github.com/ros2/rosidl_python/blob/foxy/rosidl_generator_py/resource/_msg_support.c.em
- rclpy #1389 и #1435 — EventsExecutor для rclpy, замер «80 % против 7 % ядра»: https://github.com/ros2/rclpy/issues/1389 ;
  анонс: https://discourse.openrobotics.org/t/faster-rclpy-executor-now-in-rolling/42852
- rclpy #1025 (GIL в `take_message`, профиль rosbridge): https://github.com/ros2/rclpy/issues/1025
- M. Bestmann (Bit-Bots). ROS 2 Latencies for High Frequencies using the EventExecutor, 31.03.2023:
  https://bit-bots.de/?p=8582
- Bit-Bots. Speeding up Python nodes with the Bit-Bots TF Buffer, 20.01.2025:
  https://discourse.openrobotics.org/t/speeding-up-python-nodes-with-the-bit-bots-tf-buffer/41649
- rmw_fastrtps foxy: README (ASYNCHRONOUS, `RMW_FASTRTPS_USE_QOS_FROM_XML`) и `participant.cpp` (localhost_only + SHM):
  https://github.com/ros2/rmw_fastrtps/tree/foxy
- Fast DDS 2.1: SHM-транспорт (включён по умолчанию, сегмент 512 КиБ, сегмент у отправителя):
  https://fast-dds.docs.eprosima.com/en/v2.1.0/fastdds/transport/shared_memory/shared_memory.html ; предупреждение о
  `segment_size`: https://fast-dds.docs.eprosima.com/en/latest/fastdds/transport/shared_memory/shared_memory.html ;
  Large data: https://fast-dds.docs.eprosima.com/en/v2.1.0/fastdds/use_cases/large_data/large_data.html ;
  `shm clean`: https://github.com/eProsima/Fast-DDS/blob/v2.1.0/tools/fastdds/shm/clean.py
- eProsima. ROS2 Foxy & RMW Fast DDS: Improved Intra-process & Inter-process performance (Fast DDS 2.1):
  https://discourse.openrobotics.org/t/ros2-foxy-rmw-fast-dds-improved-intra-process-inter-process-performance/17422
- ROS 2 docs foxy, DDS tuning: https://github.com/ros2/ros2_documentation/blob/foxy/source/How-To-Guides/DDS-tuning.rst
- realsense-ros 4.51.1: README (intra-process, `/static_tf`, профили, QoS) и `rs_launch.py`:
  https://github.com/IntelRealSense/realsense-ros/tree/4.51.1 ; частоты IMU D435i (гироскоп 200/400 Гц, акселерометр
  63/250 Гц): https://realsense.intel.com/autonomous-robotics-intel-realsense-technology-and-brainos/
- imu_tools foxy, `imu_filter_ros.cpp`: https://github.com/CCNYRoboticsLab/imu_tools/blob/foxy/imu_filter_madgwick/src/imu_filter_ros.cpp
- ros2_controllers foxy, diff_drive_controller (`publish_rate` 50, `cmd_vel_timeout` 0,5 с):
  https://github.com/ros-controls/ros2_controllers/tree/foxy/diff_drive_controller
- rosbridge_suite 1.3.1: `rosbridge_websocket.py` (spin в цикле tornado), `subscribers.py`, `publishers.py`,
  `subscribe.py` (cbor-raw): https://github.com/RobotWebTools/rosbridge_suite/tree/1.3.1 ; 2.7.1 (spin в потоке,
  EventsExecutor): https://github.com/RobotWebTools/rosbridge_suite/tree/2.7.1
- ros2_tracing foxy README: https://github.com/ros2/ros2_tracing/tree/foxy
- sros2 foxy, SROS2_Linux.md: https://github.com/ros2/sros2/blob/foxy/SROS2_Linux.md
- RWS (C++-замена rosbridge): https://github.com/v-kiniv/rws

Не нашёл:
- рецензируемой работы с замером rclpy против rclcpp на Jetson Nano или Foxy;
- замера Fast DDS 2.1 SHM с кадрами больше сегмента на ARM.

Поэтому п. 4 и все оценки CPU выше — [оценка] или [гипотеза] до замера в контейнере Foxy или на плате.
