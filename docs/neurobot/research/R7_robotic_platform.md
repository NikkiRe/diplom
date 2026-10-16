# R7. Стек robotic_platform лаборатории: MCP-прокси, HTTP-воркеры, PicoClaw 0.2.8 и его журналы

Дата: 14.10.2026. Автор: Claude (субагент R7a-stack).
Объект (только чтение, ничего не менялось): `third_party/neurochip/robotic_platform/robotic_platform/` (далее `rp/`):
`run_yolo_mcp_stack.sh`, `yolo_mcp_server.py`, `yolo_hand_worker.py`, `robot_vision_mcp_proxy.py`, каталог
`.picoclaw/` (конфиг, журналы, сессии, workspace). Дополнительно — презентация «Робот-сканер»
(`third_party/neurochip/robot_scanner_pptx/x/ppt/`) в части, которая касается запуска и движения.
Ссылки вида `gateway.log:N` — номер строки JSONL-журнала `rp/.picoclaw/logs/gateway.log` (498 строк);
`stderr.log:N` — строка `rp/.picoclaw/workspace/mcp/robot_mcp.stderr.log` (2 691 строка).
Исходник PicoClaw **v0.2.8** (тег → коммит `6e1fab80`, совпадает с `build_info.git_commit` в конфиге робота) скачан
в каталог вне проекта; ссылки `pc28:pkg/…`. Для сравнения — v0.3.1 из `bench/baselines/vendor/src/picoclaw`
(`pc31:pkg/…`).

Пометки: **[код]** — по исходнику; **[журнал]** — по журналам и файлам с робота; **[опыт]** — воспроизведено мной
на Mac (повторы указаны); **[ист.]** — внешний источник (§9); **[оценка]** — расчёт без замера;
**[гипотеза]** — проверить у лаборатории. Числа из журналов — факты о том, что происходило у лаборатории в мае–сентябре,
а не замер производительности.

Секреты: `.security.yml` в рабочей копии уже вычищен; в журнале 52 URL long poll VK с параметром `key=` — ниже
везде `<REDACTED>`; идентификаторы пользователей VK и сообщества не приводятся.

---

## 0. Коротко

1. **На роботе сейчас другой агентский стек, чем в присланном ros_mcp_server.** В конфиге PicoClaw 0.2.8 включён один
   MCP-сервер `robot_vision_demo_mcp`: stdio-прокси `robot_vision_mcp_proxy.py` (Python 3.12) → HTTP-воркер
   «Robot Vision Demo Worker» на `127.0.0.1:8766` [код `config.json:591–597`]. **Кода воркера 8766 в архиве нет, и
   ничто в архиве его не запускает**: 29.09 все вызовы агента получили `Connection refused` [журнал
   `sessions/*.jsonl`]. ros_mcp_server и YOLO-стек (8765) — более ранние этапы (02.06) [журнал `gateway.log:4–273`].
2. **Дубли инструментов движения подтверждены опытом.** В FastMCP (mcp 1.2…1.30) остаётся **первая** регистрация:
   `robot_nudge_forward(duration_ms=2000, speed=0.8)`. Её тело передаёт миллисекунды позиционно в
   `robot_nudge(direction, duration_s, speed)`, и воркер получает `duration_s=2000` (без аргументов) или
   `duration_s=300` (так вызвала модель на роботе 29.09) при `speed=0.8` [опыт, mcp 1.30.0; журнал сессии]. Поведение
   воркера неизвестно (кода нет): если он не ограничивает длительность, это 5–33 мин движения на 80 % скорости (N1).
3. **Перезапуски — ручные Ctrl+C шлюза в терминале.** 52 остановки за 4 рабочих дня, 51 — SIGINT (группа процессов
   терминала: у MCP-сервера в stderr 37 трасс `KeyboardInterrupt`), 1 — SIGTERM [журнал]. 02.06: 38 остановок за
   9,2 ч, медианный интервал 5,9 мин. Сервер ros_mcp_server между перезапусками успевал обработать медианно
   **2 вызова** инструментов (85 % — не больше 4) [журнал `stderr.log`] — это согласуется со словами «после 3–4»,
   если речь о командах агенту [гипотеза].
4. **Что предшествует остановкам** [журнал]: ошибки инструментов камеры (M2 `KeyError 'frame_capture'` ×6, M1
   `read_one_message` ×3, таймауты rosbridge 3 с ×9, `Connection refused` ×3); «убегающие» ходы LLM (до 29–32
   итераций, 49 невалидных `robot_nudge` за один ход, 29 вызовов несуществующего `wrap_up`), а в 0.2.8 **нет команды
   `/stop`** — прервать ход можно только остановкой шлюза [код `pc28:pkg/commands/builtin.go:7–21`]; отказы провайдера
   (503, 404, 400 `max_tokens`); «оглохший» VK (3 случая); ручные правки `config.json` и кода сервера.
5. **Признаков падения камеры или воркера в этих журналах нет, но и данных нет**: консоли демо и журналов воркеров
   (`/tmp/yolo_hand_mcp_logs`) в архиве нет. Камера через ROS-узел `realsense2_camera` 02.06 работала: 4 кадра в
   17:33–21:25 [журнал `cam_image/*.json`]. R1 этими журналами не подтверждается и не опровергается.
6. **Механизмы PicoClaw, подтверждённые журналом робота:** PC5 «зомби» после неудачного `initialize` (2 раза, после
   ручной правки кода сервера: `SyntaxError`, `NameError 'true'`); PC8 — 400 `max_tokens` принят за переполнение окна,
   «принудительное сжатие» выбросило 13 и 5 сообщений истории. PC1/PC2 (смерть stdio-сервера, мусор в stdout) за
   4 дня **не наблюдались**: ни одного `connection closed` / `invalid trailing data` [журнал].
7. **Новое для запуска (N7–N9):** YOLO-воркер грузит модель до того, как слушает порт; скрипт проверяет только
   `/health`, поэтому перезапуск во время загрузки порождает второй воркер — он грузит модель целиком и падает на
   `Address already in use`, а журнал первого перезаписан [опыт на подставном воркере, 3/3]. `nohup` без супервизии;
   `torch.hub` с `source="github"` на torch 1.13 без сети не стартует вовсе (офлайн-отката нет до torch 2.0) [код].
8. **Реальная цепочка движения на роботе** (граф ROS 2 на 02.06 17:17, снятый самим агентом): `/cmd_vel` →
   `twist_mux` (входы `/cmd_vel`, `/cmd_vel_joy`, `/cmd_vel_tracker`) → `/diff_cont/cmd_vel_unstamped` →
   `diff_drive_controller` (ros2_control) → STM32; есть `/odom`, неизвестный узел `/topic_watchdog`, два
   `/urg_node2` [журнал `gateway.log:138`; слайд 5]. У `diff_drive_controller` в Foxy `cmd_vel_timeout` по умолчанию
   0,5 с — тишина `/cmd_vel` тормозит робота, если лаборатория не меняла параметр [код ros2_controllers foxy] (N13).
9. **Что это ломает в нашем пакете 1** [опыт с бинарником PicoClaw 0.2.8]: оверлей `deploy/picoclaw/config.overlay.json`
   содержит поля, которых нет в 0.2.8 (`agents.defaults.max_llm_retries`, `tools.load_image`) — шлюз **не стартует**;
   при `log_level: "warn"` из конфига лаборатории строки «Connected to MCP server» / «MCP server initialization
   complete» не печатаются — `neurobot wait-agent` никогда не дождётся готовности; `type: "streamable-http"` в 0.2.8
   не поддержан (только `http`). Подробно — §6.

---

## 1. Как это запускается и работает на роботе

### 1.1. Процессы и порты

| Процесс | Чем запущен | Python | Порт / канал | Основание |
|---|---|---|---|---|
| Граф ROS 2 Foxy: `/camera/camera` (realsense2_camera), `/urg_node2` (×2), `/controller_manager` + `/diff_cont` + `/joint_broad`, `/twist_mux`, `/robot_state_publisher`, `/imu_filter_madgwick`, `/image_compressor`, `/topic_watchdog`, `/rosbridge_websocket` + `/rosapi` | два процесса `ros2 launch` (`/launch_ros_104848`, `/launch_ros_11720`) | системный 3.8 (`/opt/ros/foxy/lib/python3.8`) | DDS; rosbridge `:9090` | [журнал `gateway.log:138` — вывод `ros2 node/topic list`, 02.06 17:17]; [слайд 5: REALSENSE_CAMERA, URG_NODE2, ROS2_CONTROL, DIFF_DRIVE, JOY] |
| Демо `predict_live.py` (узел `velocity_publisher`) | вручную | 3.8, torch 1.13 [слайд 4] | `/cmd_vel` | в графе 02.06 17:17 **не было** [журнал] |
| PicoClaw **0.2.8** `gateway` | вручную в терминале, на переднем плане | Go | `localhost:18790`; каналы VK и pico | [код `config.json:481–484, 667–669`]; 51 из 52 остановок — SIGINT [журнал] |
| MCP `robot_vision_demo_mcp` = `robot_vision_mcp_proxy.py` | PicoClaw (stdio) | `~/NeuroChip/yolo_mcp_server/.venv` (3.12 по docstring) | stdin/stdout | [код `config.json:591–597`; `robot_vision_mcp_proxy.py:5`] |
| «Robot Vision Demo Worker» | **не найдено** (кода нет) | по docstring прокси — окружение с torch, cv2, pyrealsense2, rclpy | `127.0.0.1:8766` | [код `robot_vision_mcp_proxy.py:5–8, 18`] |
| MCP `yolo_hand_mcp` = `run_yolo_mcp_stack.sh` → `yolo_mcp_server.py` | PicoClaw (stdio), использовался 02.06 19:55–21:26 | 3.12 (venv или pyenv) | stdin/stdout | [код `run_yolo_mcp_stack.sh:113–135`; журнал `gateway.log:167–273`] |
| `yolo_hand_worker.py` | `nohup … &` из скрипта выше | `/home/jetson/.pyenv/shims/python3` («где работают torch/cv2/CUDA») | `127.0.0.1:8765` | [код `run_yolo_mcp_stack.sh:35, 107`; `yolo_hand_worker.py:47–48`] |
| MCP `robot` → `robot_mcp` = `ros_mcp_server.py` (через `start_robot_mcp.sh`) | PicoClaw (stdio), 22.05–02.06 | venv на uv cpython 3.12.13, затем pyenv 3.12.11 | stdin/stdout; к rosbridge — новое WS на вызов (M3) | [код `workspace/mcp/start_robot_mcp.sh:9–17`; журнал `stderr.log:40, 316`] |
| `picoclaw agent` (CLI) | вручную | — | канал `cli` | [журнал `sessions/sk_v1_cb10…meta.json`: channel `cli`, 29.09 19:08] |

### 1.2. Кто кого запускает и в каком порядке

```
человек ─┬─ ros2 launch … (×2)  → камера, лидар, ros2_control, twist_mux, rosbridge, …   [гипотеза: вручную]
         ├─ (воркер 8766 — кто запускает, неизвестно; 29.09 не был запущен)
         └─ picoclaw gateway (терминал) ─stdio→ robot_vision_mcp_proxy.py ─HTTP→ 127.0.0.1:8766
                                     (02.06) ─stdio→ run_yolo_mcp_stack.sh ─nohup→ yolo_hand_worker.py :8765
                                                                          └exec→ yolo_mcp_server.py ─HTTP→ :8765
                                     (22.05–02.06) ─stdio→ start_robot_mcp.sh ─exec→ ros_mcp_server.py ─WS→ :9090
```

- Порядок нигде не задан. PicoClaw запускает только прокси; прокси не проверяет воркер при старте и отвечает
  «worker is not available» на каждый вызов, пока воркера нет [код `robot_vision_mcp_proxy.py:36–39`,
  `yolo_mcp_server.py:58–63`].
- YOLO-воркер получает кадр не с камеры, а из файла, который сохранил ros_mcp_server
  (`camera_capture_frame_to_file` → `.raw` 921 600 байт + `.json`) [код `yolo_hand_worker.py:157–221`,
  `yolo_mcp_server.py:72–100`]. Цепочка «LLM → кадр через rosbridge → файл → YOLO» — инструмент «посмотреть», а не
  контур жеста.
- Systemd-юнитов, launch-файлов и скриптов автозапуска в архиве нет. Единственная «супервизия» — `nohup` для 8765.

### 1.3. Python-окружения

| Окружение | Где видно | Кто пользуется |
|---|---|---|
| системный 3.8 + `/opt/ros/foxy` | `gateway.log:24` (`/opt/ros/foxy/lib/python3.8/…`) | ROS 2 Foxy, демо [слайд 4: Python 3.8, PyTorch 1.13, snnTorch 1.0.9] |
| uv cpython 3.12.13 | `stderr.log:40` | venv ros_mcp_server до ≈02.06 15:30 |
| pyenv 3.12.11 | `stderr.log:316` | venv ros_mcp_server после 02.06 15:30 |
| `~/NeuroChip/yolo_mcp_server/.venv` | `config.json:593` | прокси 8765/8766 (mcp: в venv ros_mcp_server — 1.27.2, `robot_mcp.stdout.log`) |
| pyenv-шим `python3` | `run_yolo_mcp_stack.sh:35` | YOLO-воркер; версия зависит от `pyenv global`, `.python-version` в текущем каталоге и `PYENV_VERSION` [код pyenv; какая у них — гипотеза] |
| `python3` в `exec` PicoClaw | `gateway.log:24–27, 66–67` | 3.12 без numpy и cv2; агент сам ставил numpy 2.4.6 (cp312) через зеркало pip |

Прокси нельзя запустить под 3.8: `dict[str, Any]` в аннотациях без `from __future__` вычисляется при `def`, а пакет
`mcp` требует Python ≥ 3.10 [код `yolo_mcp_server.py:28`; M13]. `yolo_hand_worker.py` совместим с 3.8 (есть
`from __future__ import annotations`, `ThreadingHTTPServer` с 3.7) [код; синтаксис проверен uv-3.8].

### 1.4. Как менялся стек (по журналам)

| Период | MCP в конфиге | Сервер / версия | Модель LLM |
|---|---|---|---|
| 21.05 | — | первый запуск шлюза, создан шаблон HEARTBEAT.md | — |
| 22.05 16:57–17:04 | `robot` | ros_mcp_server ≈181 строка, uv 3.12.13 | — |
| 02.06 14:04–15:23 | `robot`; в 14:06 вручную добавлен канал VK (синтаксическая ошибка в строке 260) | то же | RouterAI `inclusionai/ling-2.6-flash` |
| 02.06 15:37–17:35 | `robot_mcp` (переименован) | 704→707→741→794 строки, pyenv 3.12.11 | то же |
| 02.06 19:55–21:26 | `robot_mcp` + `yolo_hand_mcp` | + YOLO-стек 8765 | то же |
| 02.06 23:02 – 03.06 12:03 | `robot_vision_demo_mcp` | прокси 8766 | то же; 03.06 11:42 — 503 «Service draining» |
| 29.09 16:54–19:08 | `robot_vision_demo_mcp` | прокси 8766, воркер не запущен | RouterAI 503 «No available providers» → cloud.ru `openai/gpt-4o-mini` |

Номера версий ros_mcp_server — по строке `main()` в трассах (`stderr.log`: 177/181 → 700/704 … 788/792 и 790/794).
Присланная нам версия (704 строки) соответствует середине дня 02.06; на роботе потом была 792/794 строки.

### 1.5. Граф ROS 2 на 02.06 17:17 (что важно для пакета 1)

Источник — ошибка `exec` агента, в текст которой попал вывод `ros2 pkg list; ros2 node list; ros2 topic list`
[журнал `gateway.log:138`]. Это срез на одну минуту четырёхмесячной давности.

- **Движение:** темы `/cmd_vel`, `/cmd_vel_joy`, `/cmd_vel_tracker`, `/diff_cont/cmd_vel_unstamped`; узлы `/twist_mux`,
  `/diff_cont`, `/joint_broad`, `/controller_manager`. Имена `diff_cont`, `joint_broad`, `cmd_vel_tracker`,
  `cmd_vel_joy` и пакет `serial_motor_demo` совпадают с учебным проектом Articulated Robotics `articubot_one`, где
  `twist_mux`: `cmd_vel` — приоритет 10, `cmd_vel_tracker` — 20, `cmd_vel_joy` — 100, таймауты 0,5 с;
  `use_stamped_vel: false`; `cmd_vel_timeout` не задан (значит, 0,5 с) [ист. articubot_one; совпадение имён —
  гипотеза о происхождении].
- **Датчики:** `/scan` (urg_node2, `laser_frame`), `/odom`, `/tf`, `/joint_states`, `/camera/color/image_raw`
  (+ compressed), `/camera/imu`, `/imu/data`.
- **Прочее:** `/rosbridge_websocket`, `/rosapi` (ros_mcp_server ходит в `/rosapi/topics`, M11), `/topic_watchdog`
  (назначение неизвестно), `/urg_node2` встречается дважды.
- Узла демо `velocity_publisher` в списке нет: в этот момент работал агентский путь, а не демо.

Пакеты на роботе: `rosbridge_server`, `realsense2_camera`, `urg_node2`, `diff_drive_controller`, `twist_mux`,
`nav2_*`, `slam_toolbox`, `robot_localization`, `rtabmap_slam`, `gazebo_ros2_control`, `turtlebot3_navigation2`
[журнал `gateway.log:138`]. Nav2 установлен, но в графе не запущен.

Лидар (`workspace/lidar_scan/…_meta.json`, 02.06 21:25): 683 луча, ±119,9°, шаг 0,3516°, 0,02–5,6 м, 10 Гц — совпадает
с профилем стенда (PLAN §6) [журнал].

---

## 2. Почему стек может не подняться

| # | Что | Механизм | Основание | id |
|---|---|---|---|---|
| 1 | Воркер 8766 не запущен | его никто не запускает; PicoClaw стартует только прокси; прокси отвечает «not available» на каждый вызов, и LLM сообщает «сервис недоступен» | [код `config.json:591–597`]; [журнал: обе сессии 29.09 — `Connection refused`] | N8 |
| 2 | «Воркер ещё грузит модель, а прокси уже отвечает» | `yolo_hand_worker.py` грузит модель при импорте (`:111`) и только потом слушает порт (`:459`); прокси возвращает тот же ответ «not available», что и при упавшем воркере (`yolo_mcp_server.py:58–63`) | [код] | N7 |
| 3 | Двойной воркер | перезапуск PicoClaw во время загрузки: `curl /health` не отвечает → второй `nohup` → вторая загрузка модели (на Nano — второй CUDA-контекст, ≈1 ГиБ [ист. ANALYSIS L5]) → `Address already in use`; `>` перезаписывает журнал первого | [код `run_yolo_mcp_stack.sh:67–69, 98–111`]; [опыт 3/3: первый воркер переживает SIGINT группы, второй падает после «загрузки»] | N7 |
| 4 | Упавший воркер никто не поднимает | `nohup` без супервизии; журнал в `/tmp/yolo_hand_mcp_logs` | [код `:41–42, 105–107`] | N8 |
| 5 | Без интернета воркер не стартует | `torch.hub.load("ultralytics/yolov5", source="github")`: в torch 1.13 `_parse_repo_info` делает `urlopen("https://github.com/…/tree/main/")` без таймаута и без отката на кэш; откат появился в torch 2.0. Нет сети — исключение при импорте, воркер завершается; медленная сеть — висит до привязки порта | [код `yolo_hand_worker.py:53–54, 90–96`]; [код torch v1.13.1 `hub.py:134`, v2.0.0 `hub.py:149–158`]; torch 1.13 на роботе [слайд 4] | N9 |
| 6 | Жёсткие пути `/home/jetson` | рабочий каталог, команды MCP, `MODEL_PATH`, `start_robot_mcp.sh`, шим pyenv; при другом пользователе или раскладке — `die` в скрипте или EOF на `initialize` | [код `config.json:11, 593–595`; `yolo_hand_worker.py:51`; `run_yolo_mcp_stack.sh:22 vs :30` — в комментарии `yolo_hand_mcp_proxy.py`, по умолчанию `yolo_mcp_server.py`] | N9 |
| 7 | Другие веса | воркер берёт `demo/src/camera/best.pt`, демо — `demo/models/yolo_hand.pt` | [код `yolo_hand_worker.py:51`; `predict_live.py:525`] | N9 |
| 8 | Два (и больше) Python | шим `python3` выбирает версию по окружению; `exec` агента — 3.12 без numpy; ROS — 3.8 | [код; журнал `gateway.log:24, 66–67`] | N6 |
| 9 | Агент сам меняет окружение | `pip install numpy` из `exec` (2 раза по 60 с до таймаута), затем `NumPy 2.4.6` против `cv_bridge` Foxy, собранного с NumPy 1.x | [журнал `gateway.log:25, 27, 66`] | N5 |
| 10 | Ручные правки `config.json` | строгий разбор 0.2.8: синтаксическая ошибка или неизвестное поле — шлюз не стартует (4 раза) | [журнал `gateway.log:7–8, 458–463`; `gateway_panic.log:1–3, 10–12`]; [код `pc28:pkg/config/diagnostics.go:15–27`] | N11 |
| 11 | Ручные правки кода MCP-сервера | `SyntaxError` в строке 36, `NameError: name 'true'` в строке 30 (JSON вставлен в Python) → `calling "initialize": EOF` ×2 → PicoClaw «зомби» до перезапуска | [журнал `stderr.log:1090, 1095` ↔ `gateway.log:74–79`, 02.06 16:48:33 и 16:50:00] | PC5 |
| 12 | Камера занята другим процессом | на роботе камерой владеет ROS-узел `realsense2_camera`; демо открывает D435i напрямую через pyrealsense2; librealsense допускает одного владельца устройства — кто стартовал первым, тот и работает | [журнал `gateway.log:138`, `cam_image/*.json`]; [код `predict_live.py:18, 546–583`]; [ист. форум Intel]; конфликт — [гипотеза] | N12 |
| 13 | rosbridge не запущен | `camera_capture_frame_to_file: [Errno 111] Connection refused` 02.06 20:01 и 21:09 | [журнал `gateway.log:170, 174, 249`] | — |
| 14 | Медленный импорт MCP-сервера | SIGINT приходил во время `from mcp.server.fastmcp import FastMCP` (импорт pydantic) — запуск Python-сервера на Nano занимает секунды; пока `initialize` не завершён, агентный цикл не обслуживает никого (`pc28:pkg/agent/agent.go:134`) | [журнал `stderr.log:100–142, 450–483`]; длительность — [гипотеза] | — |
| 15 | Комментарий скрипта про «таймаут подключения MCP» | в 0.2.8 таймаута `initialize` нет: подключение идёт на контексте шлюза (`pc28:pkg/mcp/manager.go:284–440`); неблокирующий запуск полезен, но по другой причине — чтобы не держать агентный цикл | [код] | — |

---

## 3. «Надо перезагружать примерно после 3–4 жестов»: что подтверждают журналы

### 3.1. Что есть в журналах и чего нет

- `gateway.log` пишется на уровне `warn` (`config.json:484`): видны только предупреждения и ошибки. Старты, подключения
  MCP, начало и конец вызовов не видны. Старт шлюза виден косвенно — по строкам «No channels enabled», «not running
  (PID …)», «SECURITY: Channel allows EVERYONE».
- Каждая остановка шлюза оставляет пару строк: `Failed to close server connection … signal: interrupt` и
  `Failed to close MCP manager` [код `pc28:pkg/mcp/manager.go:656`, `pc28:pkg/agent/agent.go:273–281`]. Ребёнок
  получил SIGINT вместе со шлюзом: значит, Ctrl+C в терминале, где шлюз запущен на переднем плане. Это же видно со
  стороны сервера — 37 трасс `KeyboardInterrupt` в `stderr.log`.
- Строка `Long poll failed … context canceled` в 49 из 52 случаев появляется за ≤ 1 с до закрытия MCP: это отмена
  контекста при остановке шлюза, а не сбой сети [журнал]. Три `context deadline exceeded` — настоящие сбои (N10).
- Журналов демо, воркеров 8765/8766, rosbridge и `dmesg` нет. Поэтому про камеру и распознавание жестов эти журналы
  ничего не говорят.

### 3.2. Хронология

| Дата, время | Состояние | Ключевые события [журнал] | Остановок |
|---|---|---|---|
| 22.05 16:57–17:04 | `robot`, сервер ≈181 строка | ≥ 7 подключений (`ListTools`), ≥ 5 вызовов, ошибок нет | 1 |
| 02.06 14:04–15:23 | `robot`; канал VK добавлен руками | 14:06 синтаксис `config.json`; 15:21–15:23 агент запускает `ros/nudge_cmd_vel.py`, получает `No module named 'numpy'` и ставит numpy через pip (60 с × 2 до таймаута) | 5 |
| 02.06 15:37–16:45 | переименован в `robot_mcp`, 704–707 строк | 15:37 `Tool not found: mcp_robot_robot_get_status` (старое имя из истории); 16:17–16:43 `KeyError 'frame_capture'` ×6 (**M2**); 16:43 NumPy 2.4.6 против `cv_bridge` | 7 |
| 02.06 16:48–16:50 | правки кода сервера | `initialize: EOF` ×2 — «зомби» (**PC5**) | (рестарт без закрытия MCP) |
| 02.06 16:54–17:35 | 741→794 строки | 16:59 `read_one_message` ×3 (**M1**); 17:07–17:20 таймауты захвата 3,0–3,1 с ×4 и `robot_nudge` 3,0 с; 13 блокировок `exec` за ход; 17:17 агент снимает граф ROS 2 (§1.5); 17:33 кадр сохранён | 9 |
| 02.06 19:55–21:26 | + `yolo_hand_mcp` | 20:01 и 21:09 rosbridge `Connection refused`; 20:01 три задачи cron от агента; кадры 20:07, 20:55, 21:25; ходы прерваны на 29-й, 32-й и 29-й итерациях (20:23, 20:56, 21:10) | 13 |
| 02.06 23:02–23:28 | `robot_vision_demo_mcp` | 75 отклонённых вызовов `robot_nudge` (пустой `direction`, лишний `duration_ms`), из них 49 за один ход | 4 |
| 03.06 11:36–12:03 | то же | 11:42 RouterAI 503 «Service draining»; 11:56–11:58 подагенты упёрлись в глубину 3; 38 вызовов несуществующих `wrap_up`/`end`/`done` за 70 с; cron с командой запрещён из VK ×7 | 2 |
| 29.09 16:54–19:08 | то же | 16:54 RouterAI 503 «No available providers for model»; 18:19 cloud.ru 404 (имя модели без `openai/`); 18:21–18:31 400 `max_tokens` ×3 → «сжатие» выбросило 13 и 5 сообщений (**PC8**); 18:25–18:26 шлюз не стартует ×3 (`model_list[0].max_tokes` / `max_tokens`); 18:31–19:08 воркер 8766 `Connection refused` | 11 |

Итого 52 остановки шлюза (51 SIGINT + 1 SIGTERM) и 5 «грязных» выходов (pid-файл остался от прошлого запуска)
[журнал].

### 3.3. Сколько успевали сделать между перезапусками

Сервер ros_mcp_server за 33 «жизни» (версии 704–794 строк) получил от 0 до 10 вызовов инструментов: медиана **2**,
среднее 2,4; в 85 % жизней — не больше 4, в 9 — ни одного [журнал `stderr.log`, счёт `CallToolRequest` между
трассами `KeyboardInterrupt`]. Часть перезапусков — разработка (правки конфига и кода), а не отказ; разделить их по
журналу уровня `warn` нельзя.

### 3.4. Поправки к сводке главного агента

| В сводке | По журналу [журнал + мой подсчёт] |
|---|---|
| 54× `Tool not found: mcp_robot_robot_get_status` | 54 «Tool not found» всего; старое имя — 1 раз. Остальное — выдуманные моделью инструменты: `wrap_up` 29, `think` 10, `end` 6, `tmux` 4, `done` 3, `save_file` 1 |
| 101× `exec: property action not in enum` | 101 отклонённый вызов всего: `robot_nudge` — пустой `direction` 73, лишний `duration_ms` 19, `duration` 1; `exec` — пустой `action` 7, `background` не строка 1 |
| 129× `Command blocked by safety guard` | 129 — все «Tool execution failed». Из них `exec`: вне рабочего каталога 55, «опасный шаблон» 14, прочие ошибки 19; инструменты камеры 20; `robot_nudge` 1; `cron` 10; `list_dir`/`read_file` 8; подагенты 2 |
| 65× `Failed to close server connection` — перезапуски | 65 закрытых серверов при 52 остановках шлюза (в период 19:55–21:26 серверов было два) |
| 52× VK `Long poll failed` | 49 — артефакт остановки (`context canceled`), 3 — настоящий сбой (`context deadline exceeded`) |
| 9× `LLM call failed … context canceled` | `context canceled` — 3 (Ctrl+C посреди хода); 503 — 2; 404 — 1; 400 `max_tokens` — 3 |
| 4× `Gateway startup failed` из-за синтаксиса | синтаксис — 1; неизвестное поле — 3 |
| PicoClaw: heartbeat включён | при шаблонном HEARTBEAT.md без задач ход LLM не запускается [код `pc28:pkg/heartbeat/service.go:166, 236, 288–300`]; `heartbeat.log` — одна строка от 21.05. Если добавить задачу, каждый старт шлюза даст ход LLM через 1 с (`:134`) |

### 3.5. Связь с ANALYSIS

| id | Что говорят материалы R7 |
|---|---|
| R1 | данных нет (консоль демо не прислана); камера через ROS-узел 02.06 отдавала кадры |
| R3 | тот же `torch.hub` с GitHub в воркере, но без цикла повторов: воркер просто завершается (N9) |
| R9 | `gc.collect()` + `torch.cuda.empty_cache()` после каждого кадра и `INFERENCE_SIZE = 256` «для снижения давления памяти» [код `yolo_hand_worker.py:64–76, 328–336`] — косвенный признак, что лаборатория боролась с памятью на Nano [гипотеза] |
| S1/S2, вопрос №1 | в цепочке есть `diff_drive_controller` (Foxy, `cmd_vel_timeout` 0,5 с по умолчанию) и `twist_mux` (таймаут 0,5 с в articubot_one): тишина `/cmd_vel` тормозит робота через ≈0,5 с, если параметры не меняли (N13) |
| S4 | писателей `/cmd_vel` стало больше: демо, ros_mcp_server, воркер 8766 (вероятно), скрипты агента `workspace/ros/*.sh|.py` и `send_cmd.py` (публикуют `Twist` в `/cmd_vel` напрямую через `exec`) [код]; плюс `/cmd_vel_tracker` с приоритетом выше `/cmd_vel` |
| S5 | порог 25 см подтверждён слайдом 11 («до препятствия < 25 см»); 0,35 м в README устарело |
| M1, M2 | подтверждены на роботе (02.06 16:17–16:59) |
| M3/M10/M12 | захват сырого кадра 640×480×3 через rosbridge: от метки кадра до записи файла 2,3–3,0 с (4 кадра), скан лидара — 9,6 с; таймаут сервера 3 с → 9 таймаутов длительностью 3,0–3,5 с; кадры по 0,9 МБ остаются в workspace [журнал `cam_image/*.json`, `lidar_scan/*_meta.json`, `gateway.log:99–101, 148–149, 183, 206, 213, 224`] |
| M5 | повторяется в прокси: синхронные инструменты FastMCP, `vision_wait_*` блокируют до `timeout_s` + HTTP-таймаут 35 с [код `robot_vision_mcp_proxy.py:19, 69–93`] |
| M13 | решено у лаборатории отдельным venv 3.12 (uv, затем pyenv) [журнал] |
| PC1, PC2 | за 4 дня не наблюдались (0 `connection closed`, 0 `invalid trailing data`); прокси не печатают в stdout; stderr прокси PicoClaw выбрасывает |
| PC3 | не видно (уровень `warn`); `hot_reload` выключен |
| PC4 | не видно; у прокси есть HTTP-таймауты 15 и 35 с — зависание ограничено ими |
| PC5 | подтверждён (2 раза); в 0.2.8 «зомби» бывает и с HTTP-сервером, если он не отвечает при старте шлюза [опыт, случай B, §8] |
| PC8 | подтверждён: 400 `max_tokens` → «Context window error detected» ×6 → принудительное сжатие (13 и 5 сообщений) [журнал `gateway.log:450–472`]; в 0.2.8 `max_tokens: 0` превращается в 8192 [код `pc28:pkg/agent/instance.go:149–152`] |
| PC10 | аналог для VK (N10) |
| G6 | слайд 12: жесты «Полный оборот», «Вперёд», «Идти направо», «Идти налево»; слайд 9: обучение на EMNIST с латинскими буквами — расходится с гипотезой ANALYSIS §5.1 о цифрах 0–3 (разбирает другой агент) |

### 3.6. Вывод

Журналы подтверждают, что **агентский путь** у лаборатории требовал перезапуска после немногих команд, и называют
причины: ошибки MCP-сервера (M1, M2), таймауты rosbridge, неработающий воркер, «убегающие» ходы LLM без `/stop`,
отказы и неверные параметры провайдера LLM, «оглохший» VK, «зомби» после правок. Перезапуск в каждом случае —
Ctrl+C шлюза в терминале. Про **жестовый путь** (демо) журналы молчат: «3–4 жеста» может означать и команды
агенту, и жесты демо. Для второго нужны консоль демо и `dmesg` (Э0, `collect_diag.sh`).

---

## 4. Новые проблемы

| id | Что | Крит. | Основание | Что видит пользователь | Исправление |
|---|---|---|---|---|---|
| N1 | Дубли `robot_nudge_forward/backward`, `robot_turn_left/right`: остаётся первая регистрация (`duration_ms=2000`, `speed=0.8`), её тело передаёт мс позиционно в `duration_s` | крит. | [код `robot_vision_mcp_proxy.py:113–134, 145–179`; mcp `tool_manager.py:67–71` одинаково в 1.2.0, 1.9.0, 1.20.0, 1.27.2, 1.30.0]; [опыт, mcp 1.30.0: `{}` → воркеру `duration_s=2000, speed=0.8`; `{"duration_ms":300}` → `duration_s=300`; аргумент `duration_s` молча игнорируется]; [журнал: 29.09 19:08 модель вызвала `robot_nudge_forward {"duration_ms":300,"speed":0.8}`] | при воркере без ограничения длительности — минуты движения на 80 % скорости; прокси через 35 с вернёт ошибку, а робот может ехать дальше [гипотеза: код воркера неизвестен] | один набор инструментов; секунды везде; ограничения длительности и скорости в прокси **и** воркере; тест «нет дублей» (предупреждение FastMCP `Tool already exists` → провал) и проверка `tools/list` в doctor |
| N2 | Разнобой схем движения: `robot_nudge` — `duration_s`, обёртки — `duration_ms`, у старого ros_mcp_server `robot_nudge` был с `duration_ms` | сущ. | [журнал: `direction` пустой ×73, лишний `duration_ms` ×19, до 49 подряд в одном ходе]; [код `pc28:pkg/tools/validate.go:33–40` — лишние поля отклоняются, если нет `additionalProperties`]; старая схема — R2 §2 | агент «дёргается» десятками ошибочных вызовов, потом человек останавливает шлюз | одна схема; `max_tool_iterations` 6–8; после смены схемы — очистить сессии |
| N3 | Переименование MCP-сервера меняет имена инструментов (`mcp_<сервер>_<инструмент>`), а сессии на диске хранят старые | мелк. | [код `pc28:pkg/tools/integration/mcp_tool.go:131`]; [журнал `gateway.log:31`] | `Tool not found`, модель путается | имя сервера в конфиге не менять; при смене — `/clear` или удалить `workspace/sessions/*` (RUNBOOK) |
| N4 | «Убегающие» ходы LLM, а в 0.2.8 нет `/stop` | сущ. | [журнал: ходы на 29–32 итерациях прерваны Ctrl+C; `wrap_up`×29 за 70 с; подагенты на пределе глубины]; [код `pc28:pkg/commands/builtin.go:7–21` — команды `stop` нет, `HardAbort` никто не вызывает; `max_tool_iterations` 50 в `config.json:17`] | бот «завис», единственный выход — убить шлюз | `max_tool_iterations` 6–8; выключить `spawn`, `subagent`, `skills`, `cron`, `exec`; на 0.3.1 есть `/stop` |
| N5 | `exec` агента: петли блокировок песочницы и изменение окружения | сущ. | [журнал: 55 «path outside working dir», 14 «dangerous pattern»; `pip install numpy` (cp312) → NumPy 2.4.6 против `cv_bridge` Foxy; попытки `apt`, `docker`, `rosnode`/`rostopic` (ROS 1)]; [код `pc28:pkg/tools/shell.go:270–280, 1060–1115`; `config.json:548–554` — `allow_remote: true`, `exec` доступен из VK] | следующий запуск ломается «сам», причину не найти | `tools.exec.enabled: false`; `cron.allow_command: false`; doctor проверяет `numpy<2` и импорты в интерпретаторах ROS и torch, `pip check` |
| N6 | Несколько интерпретаторов и шимы pyenv | сущ. | [код `run_yolo_mcp_stack.sh:35, 116–124`]; [журнал `gateway.log:24`, `stderr.log:40, 316`] | «вчера работало» — сегодня шим выбрал другой Python | абсолютные пути интерпретаторов в юнитах, `PYENV_VERSION` не наследовать, `ExecStartPre` — проверка импортов |
| N7 | YOLO-стек: гонка загрузки и двойной запуск | сущ. | [код `yolo_hand_worker.py:111 vs :459`; `run_yolo_mcp_stack.sh:67–69, 98–111`; `yolo_mcp_server.py:58–63`]; [опыт 3/3, §8] | «недоступен» во время загрузки; после перезапуска — двойная загрузка модели и потеря журнала | воркер — systemd-юнит (один экземпляр), сначала порт и `/health` со статусом «loading» (503), готовность — `ExecStartPost`-проба, `TimeoutStartSec` под Nano |
| N8 | Воркеры без супервизии; 8766 никто не запускает; кода 8766 нет | крит. для запуска | [код `run_yolo_mcp_stack.sh:105–107`; `config.json:591–597`]; [журнал: все вызовы 29.09 — `Connection refused`] | агент отвечает «сервис недоступен» с первого сообщения | юниты с `Restart=always` и пробой готовности; PicoClaw `After=`/`Requires=` на них; doctor называет «воркер 8766 не слушает порт»; запросить код 8766 |
| N9 | `torch.hub` с GitHub на torch 1.13 без офлайн-отката; веса воркера ≠ веса демо; пути `/home/jetson` | сущ. | [код `yolo_hand_worker.py:51–58, 90–96`; torch v1.13.1 `hub.py:134`; `predict_live.py:525`] | без сети воркер не стартует; распознавание отличается от демо | `source="local"` с yolov5 на закреплённом коммите (как R3 в perception v2), `YOLOv5_AUTOINSTALL=false`, sha256 весов, пути — из env-файла |
| N10 | VK «глохнет» навсегда после одной ошибки long poll | сущ. | [код `pc28:pkg/channels/vk/vk.go:94–99` (то же в 0.3.1) + vksdk v3.3.1 `longpoll-bot/longpoll.go:118` (дедлайн 25+10 с), `:256–258` (любая ошибка завершает цикл)]; [журнал: 3× `context deadline exceeded` (02.06 16:12:50, 20:21:30; 29.09 18:22:23), после каждого — остановка шлюза через 20 с–2 мин] | бот не отвечает в VK, процесс жив | сторож: «Long poll failed» без `context canceled` → рестарт PicoClaw; лучше канал с переподключением (MQTT) |
| N11 | Строгая проверка конфига 0.2.8: неизвестное поле или синтаксис — шлюз не стартует | крит. для нашего deploy | [код `pc28:pkg/config/diagnostics.go:15–27`]; [журнал ×4]; [опыт: наш оверлей → «unknown field(s): agents.defaults.max_llm_retries, tools.load_image», шлюз не стартует; `type: "streamable-http"` → «unsupported transport type», «зомби»] | после установки нашего пакета PicoClaw не поднимается | оверлей по версии PicoClaw; проверка неизвестных полей по списку полей установленной версии; `type: "http"` |
| N12 | Камерой владеет ROS-узел `realsense2_camera`; второй владелец (демо через pyrealsense2, воркер 8766) — конфликт, зависящий от порядка запуска | сущ. | [журнал `gateway.log:138`; `cam_image/*.json`]; [слайд 5]; [код `predict_live.py:546–583`]; [ист. форум Intel]; сам конфликт — [гипотеза] | после перезапуска камера «не находится» или «занята» | один владелец: в `neurobot.target` — perception v2, узел камеры лаборатории выключен (`Conflicts=`); или perception читает тему ROS (опция); precheck по процессу `realsense2_camera_node` |
| N13 | Реальная цепочка движения: `/cmd_vel` → `twist_mux` → `/diff_cont/cmd_vel_unstamped` → `diff_drive_controller` → STM32; `/odom` есть; `/cmd_vel_tracker` выше `/cmd_vel` по приоритету; два `/urg_node2`; `/topic_watchdog` | сущ. | [журнал `gateway.log:138`]; [слайд 5]; [код ros2_controllers foxy `diff_drive_controller.cpp:166–173`, `.hpp:136` (`cmd_vel_timeout_{500}`), `:164` (`use_stamped_vel_ = true`)]; приоритеты — [ист. articubot_one] | при смерти единственного писателя робот, вероятно, тормозит через ≈0,5 с; любой издатель `/cmd_vel_tracker` перекрывает наш явный ноль | Э0: снять параметры (§7); precheck — нет издателей `/cmd_vel_tracker`; стенд — режим `stm32_sim` 500 мс как «вероятный реальный» |
| N14 | Лидар: значения ниже `range_min` (коды ошибок URG 0,006–0,019 м) и `null` после rosbridge | мелк. | [журнал `lidar_scan/…json`: 64 из 683 лучей < 0,02 м (сектор −120…−100° и единичные, 1 — в секторе демо, индекс 167), 289 `null`; в метаданных «closest_distance_m: 0.006»] | сводка агенту «ближайшее препятствие 6 мм»; потребитель без фильтра видит «стену вплотную» | фильтр `[range_min, range_max]` у всех потребителей (страж уже фильтрует, `executor/guard.py:52–60`); на стенде — подмешивать такие значения |
| N15 | Журналы: уровень `warn`, stderr stdio-серверов выбрасывается, без ротации | сущ. для диагностики | [код `config.json:484`; `pc28:pkg/mcp/manager.go:371–412` (нет `cmd.Stderr`); `pc28:pkg/logger/logger.go:184` (`O_APPEND`)]; [опыт: при `warn` нет строк подключения MCP, §8] | ни человек, ни сторож не видят старт, подключение и зависание; предупреждения FastMCP о дублях не видны | `gateway.log_level: "info"` + logrotate; MCP-серверы и воркеры — под systemd (journald) |
| N16 | Ручная эксплуатация: шлюз в терминале, остановка Ctrl+C | сущ. | [журнал: 51 SIGINT, 37 `KeyboardInterrupt` у сервера, 5 «грязных» выходов] | «перезагружаем вручную» | юниты, `Restart=always`, сторож; RUNBOOK: «не запускать шлюз в терминале» |
| N17 | `ThreadingHTTPServer` и одна CUDA-модель на все потоки; `gc.collect()` + `empty_cache()` на каждый кадр | мелк. | [код `yolo_hand_worker.py:328–336, 349–350, 459`] | при параллельных `/detect` — гонка в CUDA [гипотеза]; лишние мс на кадр [оценка] | один поток инференса или блокировка |
| N18 | Наш `lab/redact.py` маскирует ключ long poll VK только потому, что он в формате JWT | мелк. | [опыт: 52 настоящих ключа (194 символа, `eyJ…`) замаскированы правилом JWT; ключ другого формата в `…&key=<40 hex>&…` проходит без маски] | при другом формате ключа он попадёт в пакет `collect_diag` | правило для параметров URL `key=`, `access_token=` |

---

## 5. PicoClaw 0.2.8 (на роботе) против 0.3.1 (наш стенд)

| Место | 0.2.8 | 0.3.1 | Основание |
|---|---|---|---|
| Упавший stdio-сервер | не поднимается | так же | `pc28:pkg/mcp/manager.go:506–528, 565–573` ≡ `pc31:…:539–561, 598–606` |
| HTTP: переподключение по 404 «session not found» | есть (PR #2664 слит до тега) | есть | `pc28:…manager.go:508–525` |
| Мусор в stdout stdio-сервера | рвёт соединение | так же | `pc28:pkg/mcp/isolated_command_transport.go:114–138` (код идентичен) |
| stderr stdio-сервера | выбрасывается | так же | нет `cmd.Stderr` в `pc28:…manager.go:371–412` |
| Типы транспорта | `stdio`, `sse`, `http` | + `streamable-http` | `pc28:…manager.go:318–418`; `pc31:pkg/config/mcp_transport.go:21–32`; [опыт: «unsupported transport type: streamable-http»] |
| `/reload` и `hot_reload` убивают stdio | да: контекст 30 с + `defer cancel` | так же | `pc28:pkg/gateway/gateway.go:540–543` → `pc28:pkg/agent/agent.go:388–405` → `exec.CommandContext` (`manager.go:371`) |
| Таймаут вызова MCP | нет | нет | `pc28:pkg/config/config.go:938–959` (нет поля) |
| Таймаут `initialize` | нет (контекст шлюза) | нет | `pc28:…manager.go:284–440` |
| «Зомби» при неудаче MCP на старте | да; и для HTTP-сервера, который не отвечает | да | `pc28:pkg/agent/agent.go:134–136`, `agent_mcp.go:99–121, 245`, `gateway.go:248`; [журнал]; [опыт] |
| `/stop` | **нет** | есть | `pc28:pkg/commands/builtin.go:7–21`; `pc31:pkg/commands/cmd_stop.go` |
| Повторы LLM | 2, жёстко в коде; при таймауте — паузы 5 и 10 с; поля `max_llm_retries` **нет** | `max_llm_retries`, `llm_retry_backoff_secs` | `pc28:pkg/agent/pipeline_llm.go:187, 246–247`; `pc28:pkg/config/*.go` (нет поля) |
| `max_parallel_turns` | есть (по умолчанию 1) | есть | `pc28:pkg/config/config.go:273`; `pc28:pkg/agent/agent_init.go:53–56` |
| `model_list[].request_timeout` | есть | есть | `pc28:pkg/config/config.go:558` |
| `max_tokens` | 0 → 8192; по умолчанию 32 768 | так же | `pc28:pkg/agent/instance.go:149–152`; `pc28:pkg/config/defaults.go:31` |
| 400 `max_tokens` = «переполнение окна» | да → принудительное сжатие истории | так же | `pc28:pkg/agent/pipeline_llm.go:235–245, 275–300`; [журнал] |
| Неизвестные поля конфига | шлюз не стартует | так же (но набор полей другой) | `pc28:pkg/config/diagnostics.go:15–27`; [опыт] |
| `tools.load_image` | **нет** | есть | [опыт: «unknown field(s): … tools.load_image»] |
| VK long poll | одна ошибка — канал мёртв | так же | `pc28:pkg/channels/vk/vk.go:94–99` ≡ `pc31` (diff пуст) |
| Heartbeat | минимум 5 мин; первый через 1 с после старта; без задач — без LLM | так же | `pc28:pkg/heartbeat/service.go:27, 134, 166, 236` |
| Проверка аргументов инструментов | лишние поля и значения вне `enum` отклоняются | так же | `pc28:pkg/tools/validate.go` ≡ `pc31` (diff пуст) |
| Сессии | JSONL на диске, переживают перезапуск; имена инструментов с префиксом сервера | так же | `sessions/*.jsonl`; `pc28:pkg/tools/integration/mcp_tool.go:131` |
| `/health`, `/ready` | 200 / 503 в моих опытах без каналов (подробнее не разбирал) | 200 / 200 (R3) | [опыт] |

Вывод: для пакета 1 важны три отличия 0.2.8 — нет `/stop`, нет `max_llm_retries` (и `load_image`), нет
`streamable-http`. Остальные механизмы PC1–PC5, PC8 и N10 у версий общие, поэтому выводы R3 переносятся на 0.2.8.

---

## 6. Что это меняет в пакете 1 (Э0–Э2) и в A0/A1

### 6.1. deploy

1. **Оверлей PicoClaw — по версии.** Для 0.2.8 убрать `agents.defaults.max_llm_retries` и `tools.load_image`;
   `type` только `"http"`; добавить `"gateway": {"log_level": "info"}`. `neurobot picoclaw-config check`
   должен ловить неизвестные поля по списку полей установленной версии (версия — из `build_info.version` в
   `config.json` или `picoclaw version`); сейчас `check` про это молчит, а README пишет «сверены с v0.3.1» [опыт, §8].
2. **Готовность PicoClaw.** `wait-agent` ищет INFO-строки «Connected to MCP server» и «MCP server initialization
   complete». При `log_level: "warn"` (как у лаборатории) их нет вовсе → юнит не станет готовым и будет
   перезапускаться бесконечно (`StartLimitIntervalSec=0`). Оверлей должен ставить `info`, а `check` — считать
   `warn`/`error` фатальными. Сторожу (`picolog`) `info` нужен и для PC4 («Tool execution started» без завершения).
   Добавить logrotate для `~/.picoclaw/logs/gateway.log` (`copytruncate`).
3. **Сервер MCP лаборатории.** Оверлей добавляет `ros_mcp` по HTTP, но оставляет включённым stdio-сервер
   `robot_vision_demo_mcp`; `check` считает его фатальным только при `ros_mcp_server` в команде. Итог — два набора
   инструментов движения и два писателя `/cmd_vel`. Нужно: в оверлее `robot_vision_demo_mcp.enabled: false` (или
   перевести прокси на HTTP), а фатальным считать любой включённый stdio-сервер с инструментами движения.
   Имя сервера не менять без очистки сессий (N3).
4. **Цель правки Э2 «≤ 20 строк».** Сейчас патч нацелен на `ros_mcp_server.py`, которого в конфиге робота с 02.06
   нет. Предлагаю добавить второй маленький патч к `robot_vision_mcp_proxy.py`: убрать первую четвёрку дублей,
   ограничить `duration_s` и `speed`, `mcp.run(transport="streamable-http")` на 127.0.0.1. Без кода воркера 8766
   двигательную часть их стека мы не контролируем — запросить код (§7).
5. **Вместо `nohup`.** Если лаборатория хочет свои воркеры в составе цели: юнит на каждый (`neurobot-labworker@8765`,
   `@8766`), `Type=exec`, `ExecStartPost` — проба `GET /health` (для 8766 — `/state`) до 200 с
   `TimeoutStartSec`≈180 с под загрузку модели на Nano, `Restart=always`, один экземпляр по определению. Прокси и
   PicoClaw — `After=`/`Requires=` на воркер. Если воркер 8766 сам пишет `/cmd_vel` и открывает камеру, в
   `neurobot.target` он конфликтует с нашими perception и стражем → `Conflicts=` и запуск либо нашего пути, либо их.
6. **precheck и doctor.**
   - `NEUROBOT_FOREIGN_PERCEPTION`: добавить `realsense2_camera_node`, `yolo_hand_worker.py`, `run_yolo_mcp_stack.sh`
     и имя процесса воркера 8766 (после ответа лаборатории) (N12).
   - Издатели `/cmd_vel_tracker`: нет никого, кроме разрешённых (N13); `/scan` — один издатель (два `urg_node2`).
   - Интерпретатор perception (`NEUROBOT_PERCEPTION_CMD` указывает на `~/NeuroChip/demo/.venv/bin/python`, такого
     venv на роботе может не быть): импорт `torch`, `pyrealsense2`, `rclpy`, `numpy<2` — с понятной причиной в
     doctor (N5, N6).
   - Сторож: «Long poll failed» с ошибкой, отличной от `context canceled`, → рестарт PicoClaw (N10); «All MCP
     servers failed to connect» → рестарт (уже есть).
   - doctor для MCP: в `tools/list` нет инструментов движения с `duration_ms` и нет дублей (N1, N2).
7. **RUNBOOK:** не запускать шлюз в терминале; не править `config.json` и код сервера на работающей системе — правка →
   `picoclaw-config check` → `systemctl restart`; после переименования сервера — очистить сессии; `exec` выключен
   (агент больше не может «чинить» окружение).

### 6.2. onboard (perception v2, страж)

- Страж уже фильтрует лидар по `[range_min, range_max]` — для URG это обязательно (N14). Порог 25 см подтверждён
  слайдом 11.
- Выход стража `/cmd_vel` (`Twist`) на роботе — вход `twist_mux` с низшим приоритетом (10 в articubot_one). Явный ноль
  стража не перекрывает джойстик (так и нужно), но перекрывается `/cmd_vel_tracker` (20) → только через precheck.
- `/odom` на роботе есть (от `diff_cont`) — v2 исполнителя (Э3) может опираться на одометрию и `odom_stall`.
- Камера: perception v2 открывает D435i через pyrealsense2, а на роботе камерой обычно владеет `realsense2_camera`
  (N12). Для Э2 — precheck и `Conflicts=`; опция Э3 — режим чтения кадра из темы ROS.

### 6.3. lab (Э0)

- `collect_diag.sh`: добавить разделы из §7 (воркеры, порты 8765/8766, pyenv, параметры `diff_cont` и `twist_mux`,
  `/topic_watchdog`, издатели `/cmd_vel*`), журналы `/tmp/yolo_hand_mcp_logs/*.log` и
  `~/.picoclaw/workspace/mcp/*.log`; сессии — только число и размер, без содержимого.
- `redact.py`: правило для параметров URL `key=` / `access_token=` (N18).
- `LAB_HANDOUT.md`: вопросы из §7; «меры без кода» дополнить: `log_level: "info"`, `exec` выключить,
  `max_tool_iterations` 8, не запускать шлюз в терминале, не обновлять numpy в окружении ROS.

### 6.4. stand и gestures

- `stm32_sim`: режим `timeout_ms=500` — «вероятное поведение робота» (`diff_drive_controller` по умолчанию), `0` —
  худший случай; оба в отчёте отдельно. Опционально — `twist_mux` перед `stm32_sim` с приоритетами 10/20/100 и
  таймаутами 0,5 с, чтобы проверить, что явный ноль стража проходит, а `/cmd_vel_tracker` — нет.
- Лидар стенда: подмешивать значения < `range_min` (0,006–0,019 м) в задний левый сектор и NaN — для проверки
  фильтров стража и robot-mcp.
- gestures: материалы R7a на генератор не влияют (классы — по слайдам 9 и 12, разбирает другой агент).

### 6.5. A0 и A1

- **A0 («как у лаборатории»)** должен воспроизводить их текущий агентский стек, а не 0.3.1 + ros_mcp_server по stdio:
  PicoClaw **0.2.8** (бинарник linux-arm64 ≈20 МБ из релиза; для ВМ sbc — с разрешения), их конфиг (`max_tokens` 0,
  `max_tool_iterations` 50, `exec` и `cron` включены, `log_level` warn), прокси `robot_vision_mcp_proxy.py` как есть
  (с дублями) и подставной воркер 8766 по API прокси (`/state`, `/vision/*`, `/lidar/status`, `/motion/*`), который
  записывает полученные `duration_s`/`speed` и **ограничивает** движение на стенде (иначе дубль увезёт модель
  робота). Отклонения — в `manifest.deviations`.
- **A1** — наш пакет с оверлеем, совместимым с 0.2.8 (п. 6.1.1–6.1.2). Сравнивать на одном расписании отказов;
  добавить отказы, найденные в журналах: «воркер не запущен», «перезапуск во время загрузки воркера», «VK long poll
  deadline», «400 `max_tokens`» (mockllm возвращает такую ошибку).

---

## 7. Что спросить у лаборатории (дополнение к ANALYSIS §5 и LAB_HANDOUT)

1. Код и способ запуска **Robot Vision Demo Worker (8766)**: кто его запускает, каким Python, пишет ли он `/cmd_vel`
   (какой темой и типом), открывает ли камеру сам или читает `/camera/color/image_raw`, ограничивает ли
   `duration_s` и `speed` в `/motion/nudge`, в каких единицах `speed` («normalized and scaled»).
2. Актуальная версия `ros_mcp_server.py` (792–794 строки) и используется ли он сейчас.
3. `ros2 param get /diff_cont cmd_vel_timeout`, `ros2 param get /diff_cont use_stamped_vel`,
   `ros2 param dump /twist_mux`, `ros2 node info /topic_watchdog`, `ros2 topic info -v /cmd_vel /cmd_vel_tracker
   /cmd_vel_joy /scan`; кто публикует `/cmd_vel_tracker`; почему два `/urg_node2`; какие launch-файлы запускаются
   и кем. Это прямой ответ на вопрос №1 (ANALYSIS §5).
4. Какие веса правильные: `demo/models/yolo_hand.pt` или `demo/src/camera/best.pt` (sha256 обоих).
5. `pyenv versions`, `pyenv global`, `which -a python3`, `python3 -c "import numpy, torch; print(numpy.__version__,
   torch.__version__)"` в окружении воркера; не обновлялся ли numpy после 02.06.
6. Работает ли демо одновременно с `realsense2_camera` или их запускают по очереди.
7. Что именно перезапускают после «3–4 жестов»: шлюз PicoClaw (журналы говорят — его), демо или весь Jetson.

---

## 8. Как воспроизвести

Скрипты — во временном каталоге сессии `/private/tmp/claude-501/…/scratchpad/r7/` (не в проекте). Если он удалён —
всё восстанавливается по описанию. Код лаборатории не менялся; веса не загружались.

| Опыт | Что делает | Итог | Повторы |
|---|---|---|---|
| `exp/dup_probe.py <путь к robot_vision_mcp_proxy.py>` | загружает прокси по пути (`importlib`, `python -I`), подменяет `WORKER_URL` на подставной HTTP-воркер, перечисляет инструменты, вызывает дубли через `FastMCP.call_tool` | 4 предупреждения `Tool already exists`; 14 инструментов; у обёрток схема `duration_ms`/`speed` 2000/0.8; `{}` → `/motion/nudge {"duration_s": 2000, "speed": 0.8}`; `{"duration_ms":300,"speed":0.8}` → `duration_s: 300`; `{"duration_s":0.3}` → снова 2000 | детерминированно; mcp 1.30.0, pydantic 2.13.5 |
| исходник `tool_manager.py` mcp v1.2.0, v1.9.0, v1.20.0, v1.27.2 (raw.githubusercontent.com) | `add_tool` при совпадении имени возвращает существующий инструмент | во всех версиях «первый выигрывает» | — |
| `exp/stack_rerun.py` | запускает **их** `run_yolo_mcp_stack.sh` с переменными окружения: подставной воркер (5 с «загрузки», затем порт 18765), подставной прокси; SIGINT группе процессов через 1 с, повторный запуск | 1 живой воркер (первый пережил SIGINT: фоновый процесс неинтерактивного bash игнорирует SIGINT), второй «загрузился» и упал `Address already in use`; в журнале осталась только запись второго | 3/3 |
| бинарник PicoClaw v0.2.8 Darwin arm64 (`gh release download`, sha256 `e8104449…15d4` совпал с `picoclaw_0.2.8_checksums.txt`); `exp/pc028_cfg.py` | конфиг лаборатории (каналы, heartbeat и сеть выключены) + наш оверлей; запуск `picoclaw gateway` с временным `PICOCLAW_HOME` | A: «unknown field(s): agents.defaults.max_llm_retries, tools.load_image» → Gateway startup failed; B (без них): шлюз стартует, `/health` 200, при недоступном HTTP-сервере — «All MCP servers failed to connect» (зомби); C (`streamable-http`): «unsupported transport type» | по 1 |
| `exp/tiny_http_mcp.py` (FastMCP streamable HTTP, свой) + 0.2.8 | уровень журнала `warn` против `info` | `warn`: 0 строк «Connected to MCP server» / «MCP server initialization complete» в консоли и в `gateway.log`; `info`: есть | по 1 |
| `parse.py`, `stats.py`, `timeline.py` | разбор `gateway.log` (JSONL) на копии с вырезанными `key=`/`ts=` и длинными токенами | частоты §3.4, хронология §3.2 | — |
| подсчёт по `stderr.log` | вызовы `CallToolRequest` между трассами `KeyboardInterrupt`, версия по строке `main()`, интерпретатор по путям | §3.3 | — |
| `redact.py` на строках long poll | значения ключей не выводятся; проверяется только, остались ли они в выводе | 0 из 52 остались (правило JWT); синтетический `key=<40 hex>` — не замаскирован | — |

Исходник PicoClaw v0.2.8: `codeload.github.com/sipeed/picoclaw/tar.gz/refs/tags/v0.2.8`
(sha256 архива `1e75f68d…054418`, коммит `6e1fab80`). vksdk v3.3.1 — `proxy.golang.org`
(sha256 zip `8b889a7c…98a1`). torch `hub.py` — теги v1.13.1 и v2.0.0; `diff_drive_controller` — ветка `foxy`
репозитория ros2_controllers.

Артефактов в `experiments/results/` не создавал: задача R7a — только заметки. Для отчёта Э2 эти опыты стоит
повторить как тесты пакета (дубли — в тестах патча прокси; 0.2.8 — в тестах `picoclaw_cfg`).

---

## 9. Источники

- Код и данные лаборатории: `rp/` (файлы перечислены выше); презентация «Робот-сканер»: слайды 4 (стек), 5
  (компоненты, `embeddings/Microsoft_Visio_Drawing1.vsdx`: REALSENSE_CAMERA, URG_NODE2, ROS2_CONTROL, DIFF_DRIVE,
  JOY, STM32 UART), 9, 11 (`media/image42.png`, `image43.png`: порог 25 см, ≥ 6 кадров, 1,5 с), 12.
- PicoClaw v0.2.8: https://github.com/sipeed/picoclaw/releases/tag/v0.2.8 ; PR #2664 (переподключение HTTP):
  https://github.com/sipeed/picoclaw/pull/2664
- MCP Python SDK, `tool_manager.py`: https://github.com/modelcontextprotocol/python-sdk/blob/v1.27.2/src/mcp/server/fastmcp/tools/tool_manager.py
- PyTorch hub: https://github.com/pytorch/pytorch/blob/v1.13.1/torch/hub.py , https://github.com/pytorch/pytorch/blob/v2.0.0/torch/hub.py
- vksdk long poll: https://github.com/SevereCloud/vksdk/blob/v3.3.1/longpoll-bot/longpoll.go
- ros2_controllers foxy, diff_drive_controller: https://github.com/ros-controls/ros2_controllers/blob/foxy/diff_drive_controller/src/diff_drive_controller.cpp ,
  https://github.com/ros-controls/ros2_controllers/blob/foxy/diff_drive_controller/include/diff_drive_controller/diff_drive_controller.hpp
- articubot_one (twist_mux и контроллеры): https://github.com/joshnewans/articubot_one/blob/main/config/twist_mux.yaml ,
  https://github.com/joshnewans/articubot_one/blob/main/config/my_controllers.yaml
- Один процесс на камеру RealSense: https://forums.intel.com/s/question/0D50P0000490TsR/how-to-access-same-device-from-multiple-processes
- Внутренние: `docs/neurobot/ANALYSIS.md`, `PLAN.md`, `research/R1–R6`; пакет 1: `neurobot/deploy/`
  (`picoclaw/config.overlay.json`, `neurobot_deploy/checks.py:275–315`, `__main__.py:119–140`, `picolog.py:69`,
  `units/neurobot-picoclaw.service`, `neurobot.env.example:35–38`), `neurobot/lab/redact.py`,
  `neurobot/executor/guard.py:48–60`, `neurobot/sim/stand/stm32_sim.py`.
