# R5 — внешние источники для задачи «робот NeuroChip: жест → движение, непрерывно, без видимых перезапусков»

Дата: 14.10.2026. Автор: Claude (часть R5 исследования). Ключа LLM не тратили, по ссылкам ИТМО GitLab не ходили.

Пометки: **[F]** — проверено по ссылке (страница открыта или первоисточник/код прочитан); **[F-]** — взято из
поисковой выдачи/вторичного пересказа, страница не открылась (403/503) — использовать осторожно; **[C]** — прочитан
код (сторонний архив в `third_party/` или пакет в `.venv-rosmcp`), строки указаны; **[О]** — моя оценка/гипотеза,
нужен замер; **[НН]** — в проверенных источниках не найдено.

---

## 0. Главное (одним экраном)

1. **«После пары использований надо перезагружать» имеет как минимум три правдоподобных внешних причины**, каждая
   задокументирована у других: (а) RealSense на Jetson Nano периодически не отдаёт кадр → `RuntimeError: Frame didn't
   arrive within 5000` (тред NVIDIA, тот же стек R32.7.1 + D435i) — а в демо `wait_for_frames()` без обработки в
   daemon-потоке, т.е. поток умирает, процесс остаётся «зомби» **[C]**; (б) rosbridge на ROS 2 плохо переносит частые
   переподключения (дедлоки/ошибки исправлялись в 1.3.1, утечка на service call — в 1.3.2, которой на Foxy нет), а
   NeuroChip `ros_mcp_server.py` открывает **новое WebSocket-соединение на каждое сообщение** **[C]**; (в) Jetson Nano
   без вентилятора за 5–10 мин уходит в тепловой троттлинг (~200 МГц, FPS 20→<10) — со стороны это выглядит как «зависло».
2. **LLM не должна стоять в контуре «помахали → поехали».** Даже у самых быстрых «mini» TTFT ≈0,7–0,8 с на 10k входа
   (без рассуждения), у gpt-5-mini medium/high — 11/50 с; индустрия (Helix, GR00T N1, Hi Robot, Gemini Robotics 1.5)
   строит «медленный планировщик (7–10 Гц и реже) + быстрый исполнитель (120–200 Гц)». Для NeuroChip: жест→команда
   движения — локальный рефлекс на плате; LLM — только для редких «умных» запросов и сводок.
3. **Безопасная остановка — штатными средствами ROS 2**: таймаут команды скорости (`twist_mux` timeout, `diff_drive_controller`
   `cmd_vel_timeout`=0,5 с по умолчанию), а не «публикуем 0 пять раз». В демо при препятствии узел просто **перестаёт
   публиковать** /cmd_vel **[C]** — безопасно только если STM32 сам останавливается по таймауту (неизвестно → вопрос лаборатории).
4. **«Невидимые перезапуски»** = быстрый автоперезапуск упавшего/зависшего процесса + сохранение состояния вне процесса:
   `respawn` в launch (ловит только *выход* процесса), systemd `WatchdogSec`+`sd_notify` (ловит *зависание*),
   `FileDescriptorStoreMax` (перезапуск без потери сокетов/состояния), lifecycle-узлы (реконфигурация без пересоздания),
   у realsense-ros — `initial_reset`, `reconnect_timeout`, сервис `hw_reset`.
5. **Платформа робота устарела по всем осям**: Foxy EOL 20.06.2023; JetPack 4 EOL (4.6.6 — последний, 22.11.2024);
   CUDA на Nano ≤10.2 ⇒ PyTorch ≤1.13; INT8 TensorRT на Maxwell не ускоряет; realsense-ros больше не поддерживает Foxy;
   в Nav2 для Foxy нет `collision_monitor` и `velocity_smoother`. При этом `requirements.txt` демо требует
   `torch==2.4.1`, `ultralytics==8.3.190` — на Nano с CUDA 10.2 так не поставить **[C]+[F]** (значит, на роботе стоят
   другие версии — уточнить).

---

## 1. YOLOv5n на Jetson Nano, RealSense D435(i)

### 1.1 Скорость детектора
- **YOLOv5n**: 1,9 M параметров, 4,5 GFLOPs@640, CPU b1 45 мс (AWS p3.2xlarge), позиционируется для mobile/CPU. **[F]**
  https://github.com/ultralytics/yolov5/releases/tag/v6.0
- **Seeed wiki** (YOLOv5 + TensorRT на Jetson, пререквизит JetPack 4.6.1): на Jetson Nano с FP16 «в среднем около 47 мс»
  (~21 FPS) для их обученной модели; вариант/разрешение явно не указаны. **[F]** https://wiki.seeedstudio.com/YOLOv5-Object-Detection-Jetson/
- **Seeed blog 2022** «Run YOLOv5 at 27 FPS on Jetson Nano» (TensorRT); в выдаче — базовая yolov5n на PyTorch ≈16,7 FPS.
  **[F-]** (страница 403) https://www.seeedstudio.com/blog/2022/08/23/faster-inference-with-tensorrt-on-nvidia-jetson-run-yolov5-at-27-fps-on-jetson-nano/
- **NVIDIA forum**: YOLOv5s на Nano 4 ГБ — PyTorch ≈120 мс/кадр; TensorRT ≈140 мс при ошибочном входе 640×640, ≈70 мс
  после пересборки движка под 640×480. **[F]** https://forums.developer.nvidia.com/t/yolov5-tensorrt-results-seems-weird-on-jetson-nano-4gb/198282
- **arXiv 2608.14727** (Hossen et al., 08.2026): YOLOv5n как TensorRT FP16 на Jetson Nano; FP32 YOLO **не влез в память**
  Nano; конвейер 13,45 FPS против 9,86 FPS последовательного цикла (×1,36), **~91 % выигрыша — от перекрытия декодирования
  кадра с инференсом**. **[F]** https://arxiv.org/abs/2608.14727 — аргумент за разнесение захвата и инференса по потокам.
- Прочее (низкое доверие): Medium (Makhalov) — yolov5n самая быстрая на Nano, «чуть меньше 50 мс» (FP16, 640) **[F-]**;
  GitHub imnuman/jetson-object-detection — «Jetson Nano 4GB: 32 FPS» без методики **[F-]**.
- **Точных цифр YOLOv5n на Nano для 320 и 640 отдельно, PyTorch FP16 vs TRT FP16, по одной методике — [НН].** Нужно
  мерить самим: `trtexec --onnx=... --fp16` на 320 и 640, `nvpmodel -m 0` + `jetson_clocks`, отдельно pre/post-обработка.
- **INT8 на Nano**: GPU Maxwell (sm_53) без нативного INT8; NVIDIA советует FP16/FP32; TensorRT предупреждает, что INT8
  «performance will be negatively affected». **[F-]** https://forums.developer.nvidia.com/t/why-jetson-nano-not-support-int8/84060 ,
  https://forums.developer.nvidia.com/t/is-int8-ptq-even-possible-on-jetson-nano/218184
- **Ultralytics**: JetPack 4 (Nano) — только Docker-образ `ultralytics/ultralytics:latest-jetson-jetpack4`; бенчмарков
  оригинального Nano в гайде нет. **[F]** https://docs.ultralytics.com/guides/nvidia-jetson
- В демо: `torch.hub.load("ultralytics/yolov5", "custom", ...)` в цикле `while True: try/except: continue` (без паузы),
  `.half()` на CUDA, `size=320` **[C]** `third_party/neurochip/demo/demo-ros2_feature/src/camera/predict_live.py:510-525,561-562,625`.

### 1.2 Программный стек Nano
- JetPack 4.6.6 — **последний** JetPack 4 (EOL-объявление 22.11.2024), рекомендован для Nano; JetPack 5 Nano не поддерживает;
  базовая ФС — Ubuntu 18.04. **[F]** https://forums.developer.nvidia.com/t/announcing-end-of-life-for-nvidia-jetpack-4-with-the-release-of-jetpack-4-6-6/314302
- Ubuntu 20.04 на Nano — только неофициальные образы (Qengineering: PyTorch 1.13.0, TorchVision 0.14.0, TensorRT 8.0.1.6+cuda10.2;
  «PyTorch 2.0 requires CUDA 11 … cannot be installed on a Jetson Nano»). **[F]** https://github.com/Qengineering/Jetson-Nano-Ubuntu-20-image
- Официальные колёса PyTorch от NVIDIA для JetPack 4 — только Python 3.6, иначе сборка из исходников. **[F-]**
  https://forums.developer.nvidia.com/t/need-pytorch-tool-base-on-python-3-8-version-on-jetpack-4-6-4-with-cuda-10-2/288018
- **Память**: в тесте NVIDIA первый `.cuda()` на Nano (JP 4.5.1, PyTorch 1.8) поднял занятую RAM 1052→2386 МБ из 3964
  («around 1GiB»): грузятся CUDA/cuDNN. **[F]** https://forums.developer.nvidia.com/t/calling-cuda-consumes-all-the-ram-memory/183240
  ⇒ на 4 ГБ Nano YOLO(PyTorch)+SNN+ROS 2+rosbridge+агент — впритык **[О]**; мерить `tegrastats`/`jtop`.
- **Тепло**: hand-pose демо на Nano через 5–10 мин — CPU ~200 МГц, ~95 °C, камера с >20 до <10 FPS; NVIDIA: программный
  троттлинг, «add fan». **[F]** https://forums.developer.nvidia.com/t/jetson-nano-thermal-management/183506

### 1.3 RealSense на Jetson
- Гайд librealsense для Jetson: «рекомендуется нативный драйвер ядра, особенно в production»; RSUSB (libuvc) — без патча
  ядра, но «ценой ограничений производительности и функциональности (например, multi-cam)», без метаданных; Nano — только
  через внешний community-гайд. **[F]** https://raw.githubusercontent.com/IntelRealSense/librealsense/master/doc/installation_jetson.md
- **Тот же симптом, что у лаборатории**: Jetson Nano + D435i, L4T R32.7.1 — `RuntimeError: Frame didn't arrive within 5000`
  при depth+color (depth-only работает); ответ NVIDIA про поддержку форматов в драйверном слое; решения в треде нет. **[F]**
  https://forums.developer.nvidia.com/t/jetson-nano-with-d435i-runtimeerror-frame-didnt-arrive-within-5000/328056
- RealSense support (MartyG): `hardware_reset()` ≈ отключить/включить USB, занимает 2–3 с, после — пауза; если камера
  видна — закрыть и заново запустить pipeline; если нет — сбросить USB-порт. **[F-]**
  https://support.realsenseai.com/hc/en-us/community/posts/38442968708499/comments/38466525657491
- Частые советы (низкое доверие, разрозненно): проверить, что камера на USB 3 (бывает USB 2.1), короткий кабель/питаемый хаб,
  15 FPS вместо 30, отключить USB autosuspend, на Jetson закрепить версию SDK (сообщения о проблемах 2.56.x). **[F-]**
- **API pyrealsense2** (исходник обёртки): `wait_for_frames(timeout_ms=5000)` блокирует и бросает исключение по таймауту;
  если приложение держит кадры слишком долго, устройству не хватает ресурсов на новые; есть `try_wait_for_frames(timeout_ms)`
  → `(ok, frames)` и `poll_for_frames()`. **[F]** https://github.com/IntelRealSense/librealsense/blob/master/wrappers/python/pyrs_pipeline.cpp
- **realsense-ros**: параметры `initial_reset` («device was not closed properly … needs to reset»), `reconnect_timeout`,
  `wait_for_device_timeout`; сервис `hw_reset`; узел может быть lifecycle (`on_activate→startDevice`, `on_deactivate→stopDevice`);
  **Foxy wrapper больше не поддерживает** (EOL). **[F]** https://github.com/IntelRealSense/realsense-ros (README, ветка ros2-development)
- Накладные расходы pyrealsense2 на Nano (CPU %, мс/кадр) — **[НН]**.

---

## 2. rosbridge_suite: частые advertise/unadvertise и новое соединение на каждое сообщение

### 2.1 Версии
- Foxy: rosbridge_suite **1.3.1** (последний релиз для Foxy); Humble 2.0.8; Jazzy **2.7.1** (= наш стенд sim); Kilted 3.3.1;
  Rolling 4.2.1. **[F]** https://raw.githubusercontent.com/ros/rosdistro/master/foxy/distribution.yaml ,
  …/jazzy/distribution.yaml , https://index.ros.org/p/rosbridge_server/
- ROS 2 Foxy EOL — **20.06.2023**. **[F]** https://raw.githubusercontent.com/ros2/ros2_documentation/rolling/source/Releases.rst

### 2.2 Что важно из кода и changelog
- **Один ROS-publisher на топик на всех клиентов**; QoS публикатора rosbridge: `TRANSIENT_LOCAL`, depth=queue_size (100),
  для не-latched — `lifespan = 1 s` («to prevent late-joining subscribers from missing messages»). После `unadvertise`/обрыва
  клиента publisher уничтожается **через `unregister_timeout` = 10 с**, если клиентов не осталось (параметр запуска, по умолчанию 10.0).
  Одинаково в 1.3.1 и 2.7.1. **[F]** https://github.com/RobotWebTools/rosbridge_suite/blob/1.3.1/rosbridge_library/src/rosbridge_library/internal/publishers.py ,
  …/blob/2.7.1/…/publishers.py , launch: …/blob/1.3.1/rosbridge_server/launch/rosbridge_websocket_launch.xml
- Протокол: `publish` в нерекламированный топик **сам делает advertise**; `latch`/`queue_size` устарели (вместо них `qos`, но
  QoS-профили в advertise добавлены только в 4.2.0, 27.05.2026 — т.е. не в Jazzy 2.7.1); `subscribe`: `throttle_rate` (мс),
  `queue_length`, `compression`: none/png/cbor/cbor-raw; фрагментация. **[F]** https://github.com/RobotWebTools/rosbridge_suite/blob/ros2/ROSBRIDGE_PROTOCOL.md
- Changelog rosbridge_library (ветка ros2): **1.3.1 (21.10.2022)** «Improve robustness for multiple client connections (#803)»;
  в PR #803 — дедлок сервера при быстром подключении/отключении клиентов, `RuntimeError: dictionary changed size during iteration`,
  спам `WebSocketClosedError` (симптом из связанного issue: «Multiple webpage refresh crashes Rosbridge server»). **[F]**
  https://github.com/RobotWebTools/rosbridge_suite/pull/803
- **1.3.2 (27.09.2023)** «fix memory leak on service call (#774)», «option to call services in a separate thread» — **после EOL
  Foxy, в Foxy не попало**. В обсуждении PR #774 корень утечки отнесён к rclpy (ros2/rclpy#1034) — т.е. и в новых версиях
  проверять замером. **[F]** https://github.com/RobotWebTools/rosbridge_suite/pull/774 ,
  https://raw.githubusercontent.com/RobotWebTools/rosbridge_suite/ros2/rosbridge_library/CHANGELOG.rst
- 2.2.0 (26.02.2025): таймаут для `call_service`, исправлен бесконечный цикл в QueueMessageHandler, монотонные часы; подписчики
  по умолчанию BestEffort. 4.1.0: гонка при уничтожении подписки. rosbridge_server: снижение CPU в простое (#1040). **[F]** (тот же changelog
  + https://raw.githubusercontent.com/RobotWebTools/rosbridge_suite/ros2/rosbridge_server/CHANGELOG.rst)
- **Отзыв практиков** (README альтернативы RWS, C++): Python-rosbridge на ROS 2 — «severe memory leaks (~1 GB of memory per
  day), high CPU usage», «sometimes crashes if multiple clients reconnect rapidly»; RPi 4B: rosbridge 57,4 % CPU / 59 МБ против
  RWS 12,8 % / 18 МБ («not a benchmark»). **[F]** (анекдотично) https://github.com/v-kiniv/rws
- Специального issue «первое сообщение после advertise теряется» в rosbridge_suite — **[НН]**. Механизм, однако, следует из
  QoS выше **[О]**: после >10 с простоя publisher пересоздаётся, DDS-сопоставление с подписчиком асинхронно; VOLATILE-подписчик
  (типичный драйвер /cmd_vel) не получает «историю», значит сообщения, опубликованные до сопоставления, для него теряются.
  NeuroChip-сервер ждёт после advertise всего 50 мс. **Проверить на стенде sim (rosbridge 2.7.1) — замером доли потерь первых команд.**

### 2.3 Рекомендации из источников
- Одно постоянное соединение, advertise один раз, публикация в него; для потоков — `throttle_rate`/`queue_length`, для
  бинарных/крупных — `cbor`/`cbor-raw`; для управления движением rosbridge вообще не обязателен (узел rclpy/rclcpp на роботе). **[F]+[О]**
- Наш Go-адаптер robot-mcp уже держит одно соединение: stop 0,5 мс (2–4 см) против 2 с (15 см) у ros-mcp-server (HANDOFF §7 —
  ориентир, не результат).

---

## 3. Непрерывная работа ROS 2 и «невидимые перезапуски»

- **launch respawn**: `ExecuteProcess`/`Node(respawn=True, respawn_delay=...)` перезапускает процесс, если он **завершился**
  (и launch не в shutdown); есть и XML-атрибуты `respawn`, `respawn_delay` (Foxy). Зависший, но живой процесс respawn не ловит. **[F]**
  https://github.com/ros2/launch/blob/foxy/launch/launch/actions/execute_process.py
- **systemd** (`systemd.service(5)`): `WatchdogSec=` — сервис обязан регулярно слать `sd_notify("WATCHDOG=1")`, иначе
  failed + SIGABRT; при `Restart=on-failure|on-watchdog|on-abnormal|always` — автоперезапуск; `RestartSec` по умолчанию 100 мс;
  `FileDescriptorStoreMax=` (с v219) — «services that can restart after an explicit request or a crash without losing state»,
  состояние — в `RuntimeDirectory=` или memfd; `RestartSteps=`/`RestartMaxDelaySec=` — только с systemd v254 (на Ubuntu 20.04
  версия ниже — проверить `systemctl --version`). **[F]** https://github.com/systemd/systemd/blob/main/man/systemd.service.xml
- **Lifecycle-узлы** (design.ros2.org): состояния Unconfigured/Inactive/Active/Finalized, переходы configure/activate/deactivate/
  cleanup, ErrorProcessing → Unconfigured при успешном восстановлении; «allows greater control over the state of ROS system». **[F]**
  https://design.ros2.org/articles/node_lifecycle.html
- **Nav2 lifecycle_manager** (bond): `bond_timeout` 4,0 с, `attempt_respawn_reconnection` true, `bond_respawn_max_duration` 10 с;
  при падении сервера менеджер ради безопасности опускает все узлы и пытается переподключиться к перезапущенным (через respawn). **[F-]**
  https://docs.nav2.org/configuration/packages/configuring-lifecycle.html
- **Остановка по таймауту команд**:
  - `diff_drive_controller` (ros2_control): `cmd_vel_timeout` — «команда устаревает» через N с, по умолчанию **0,5 с**,
    «Automatic stop after command time-out»; в Jazzy вход `TwistStamped`. **[F]** https://control.ros.org/jazzy/doc/ros2_controllers/diff_drive_controller/doc/userdoc.html
  - `twist_mux`: каждый вход — `topic`, `timeout` («start discarding old messages, and use 0.0 speed instead»), `priority` 0–255;
    `locks` (std_msgs/Bool, с таймаутом «publisher alive»). Релизы: Foxy 4.0.1, Jazzy 4.5.0. **[F]**
    https://github.com/ros-teleop/twist_mux/blob/rolling/config/twist_mux_topics.yaml , …/twist_mux_locks.yaml , rosdistro.
  - Nav2 `collision_monitor` (stop/slowdown/limit/approach по сырым датчикам, ниже Nav2, фильтр cmd_vel): **есть в Humble/Jazzy,
    нет в Foxy** (Nav2 0.4.7 без `nav2_collision_monitor` и `nav2_velocity_smoother`). **[F]** https://docs.nav2.org/tutorials/docs/using_collision_monitor.html + rosdistro foxy/humble/jazzy.
- Готовых публикаций именно про «невидимые для пользователя перезапуски» робота (быстрый перезапуск + восстановление состояния) — **[НН]**;
  это инженерная комбинация пунктов выше **[О]**.

---

## 4. LLM-агенты и роботы в реальном времени

### 4.1 Двухуровневые архитектуры
- **SayCan** (Ahn et al., 04.2022, arXiv 2204.01691): LLM предлагает шаги, value-функции навыков проверяют выполнимость; исполняют
  заранее обученные навыки. **[F]**
- **Code as Policies** (Liang et al., 09.2022, arXiv 2209.07753): LLM пишет код политики, вызывающий API восприятия и управления. **[F]**
- **RT-H** (Belkhale et al., 03.2024, arXiv 2403.01823): задача → «language motions» («move arm forward») → действия. **[F]**
- **Hi Robot** (Shi et al., 02.2025, ICML 2025, arXiv 2502.19417): высокоуровневая VLM рассуждает над инструкциями и обратной
  связью, низкоуровневая VLA-политика исполняет. **[F]**
- **Figure Helix** (20.02.2025): S2 — VLM 7B, **7–9 Гц**; S1 — 80M, **200 Гц**; S2 асинхронно обновляет латентный вектор, S1 берёт
  свежее наблюдение + последний вектор. **[F]** https://www.figure.ai/news/helix
- **NVIDIA GR00T N1** (03.2025, arXiv 2503.14734): System 2 VLM **10 Гц** на L40; System 1 (диффузионный трансформер) **120 Гц**;
  16 действий за 63,9 мс. **[F]** https://arxiv.org/html/2503.14734v2
- **Gemini Robotics 1.5** (09.2025, arXiv 2510.03342): ER-модель — оркестратор (планирует, вызывает инструменты), VLA — исполнитель. **[F-]**
- Обзоры: планирование LLM/VLM добавляет задержку порядка секунд; облачная выгрузка даёт сетевые задержки, которые трудно
  ограничить для safety-critical управления (arXiv 2312.10807); SayCan/Inner Monologue — как высокоуровневые планировщики, а не
  высокочастотные контроллеры (arXiv 2608.20379). **[F-]**
- **Безопасность**: RoboPAIR (Robey et al., 10.2024, arXiv 2410.13691) — джейлбрейк LLM-управляемых роботов, часто 100 % успеха,
  включая Clearpath Jackal с планировщиком GPT-4o и Unitree Go2. **[F]** ⇒ команды LLM к приводам только через шлюз с жёсткими лимитами.

### 4.2 Задержки «mini»-моделей (Artificial Analysis, API OpenAI, нагрузка 10k входных токенов; дата замера на странице не указана)
| Модель | TTFT, с | Скорость вывода, ток/с |
|---|---|---|
| gpt-4o-mini | 0,82 | 116,4 |
| gpt-4.1-mini | 0,68 | 154,1 |
| gpt-5-mini (minimal) | 0,75 | 150,2 |
| gpt-5-mini (medium) | 11,22 | 153,6 |
| gpt-5-mini (high) | 49,51 | 152,0 |
**[F]** https://artificialanalysis.ai/models/gpt-4o-mini , …/gpt-4-1-mini , …/gpt-5-mini-minimal , …/gpt-5-mini-medium , …/gpt-5-mini
- OpenAI, гайд по задержке: 7 принципов, в т.ч. «Make fewer requests» (каждый запрос — round-trip), «Don't default to an LLM»;
  «генерация токенов почти всегда самый долгий шаг». **[F]** https://developers.openai.com/api/docs/guides/latency-optimization
- **[О]** Один шаг агента с вызовом инструмента ≈ TTFT + ~50 ток. вывода ≈ 1,0–1,1 с у gpt-4.1-mini; типичный цикл «LLM → tool → LLM»
  ≥ 2 вызовов ⇒ ≥ 2 с + сеть до RouterAI (задержки RouterAI — **[НН]**). Это на порядок больше окна реакции «помахал — поехал».

---

## 5. MCP для ROS/роботов (2025–2026)

- **robotmcp/ros-mcp-server** (Apache-2.0, через rosbridge, ROS 1/2): README обещает «monitor robot state in real time», про
  лимиты скорости/allowlist/e-stop — ничего. **[F]** https://github.com/robotmcp/ros-mcp-server . По коду v3.1.2 (наш `.venv-rosmcp`):
  каждый инструмент — `with ws_manager:` (соединение закрывается на выходе), публикация — advertise→publish→unadvertise. **[C]**
  `.venv-rosmcp/lib/python3.12/site-packages/ros_mcp/utils/websocket.py:425-433`, `ros_mcp/tools/topics.py:661-715,771-811`.
  NeuroChip-сервер повторяет тот же шаблон ещё грубее (новое соединение на **каждое** сообщение, `sleep(0.05)`). **[C]**
- **Нативные (без rosbridge)**: amazing-ros2-mcp (rclpy; dry-run, ограничение скорости, блок-лист топиков; 06.2026) **[F]**
  https://discourse.openrobotics.org/t/amazing-ros-2-mcp-native-ros-2-mcp-server-for-ai-assisted-robotics-agents/55270 ;
  kakimochi/ros2-mcp-server (узел ROS 2, только /cmd_vel) **[F-]**.
- **Научное**: ROSA (NASA JPL, arXiv 2410.06472) — «parameter validation and constraint enforcement» **[F]**; ROSBag MCP Server
  (arXiv 2511.03497) — анализ bag-файлов, большой разброс качества tool-calling у 8 моделей **[F-]**.
- **События в MCP**: `resources/subscribe` + `notifications/resources/updated` (стабильная часть спецификации) **[F]**
  https://modelcontextprotocol.io/specification/2025-11-25/server/resources ; **tasks** (с 2025-11-25, experimental): долгие
  операции как машина состояний, `tasks/get`/`tasks/result`/`tasks/cancel`, необязательные `notifications/tasks/status` (полагаться
  на них нельзя). **[F]** https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks
- Как известные MCP-серверы решают **реальное время** — никак (нет заявлений/замеров) **[НН]**; события — в основном через
  подписку «на время» (`subscribe_for_duration`) внутри вызова инструмента **[C]**; безопасность — у части нативных: clamp/dry-run.
- **PicoClaw**: «early rapid development … Do not deploy to production before v1.0»; нативный MCP; «Recent builds may use 10-20MB RAM».
  **[F]** https://github.com/sipeed/picoclaw . Известного issue «после N запросов нужна перезагрузка» — **[НН]**.

---

## 6. Air-writing и SNN (для обзора ВКР)

1. **Alam et al., Sensors 2020, 20(2):376** — «Trajectory-Based Air-Writing Recognition Using Deep Neural Network and Depth Sensor»:
   Intel RealSense SR300, траектория кончика пальца, LSTM 99,17 % / CNN 99,06 % на своём наборе RTD, 99,32 % на 6DMG; начало/конец
   письма — **кнопками** (автоматической сегментации нет). **[F]** https://pmc.ncbi.nlm.nih.gov/articles/PMC7013930/
   → прямой аналог пайплайна NeuroChip (траектория → изображение/последовательность → классификатор); проблема сегментации жеста открыта
   (в демо — пауза руки 1,5 с **[C]** `predict_live.py:607-611`).
2. **Amir et al., CVPR 2017** — «A Low Power, Fully Event-Based Gesture Recognition System»: DVS + IBM TrueNorth, 11 жестов, 96,5 %,
   задержка распознавания начала жеста 105 мс, <200 мВт. **[F-]** https://openaccess.thecvf.com/content_cvpr_2017/html/Amir_A_Low_Power_CVPR_2017_paper.html
3. **Ceolini et al., Front. Neurosci. 2020, 14:637** — жесты по EMG + DVS на Loihi и ODIN+MorphIC: инференс на 20–40 % медленнее
   GPU, но EDP в 30–600× лучше. **[F-]** https://doi.org/10.3389/fnins.2020.00637
4. **Pei et al., Nature 572 (2019)** — гибридный ANN/SNN-чип Tianjic; беспилотный велосипед, несколько сетей (CNN, SNN, …) одновременно
   на одном чипе. **[F-]** https://doi.org/10.1038/s41586-019-1424-8 — ближайший аналог «гибридного нейропроцессора» NeuroChip.
5. **Eshraghian et al., Proc. IEEE 111(9):1016–1054, 2023** — обучение SNN приёмами глубокого обучения; snnTorch (используется в демо). **[F-]**
   https://arxiv.org/abs/2109.12894

---

## 7. Что из этого следует для робота NeuroChip (связка с кодом)

| # | Наблюдение в коде | Внешнее подтверждение механизма |
|---|---|---|
| a | `predict_live.py:615` `self.pipe.wait_for_frames()` (таймаут 5 с, исключение) в daemon-потоке `worker_thread` (`:595-597`); `rclpy.spin` продолжает жить | тред NVIDIA 328056 (тот же стек), pyrs_pipeline.cpp |
| b | `ros_mcp_server.py:161-193` новое WS-соединение + advertise + `sleep(0.05)` на каждое сообщение; `robot_nudge` шлёт так каждые ≥50 мс (`:381-385`), `robot_stop` — 5 раз (`:331-334`); `call_service` — тоже новое соединение (`:120-159`) | rosbridge PR #803 (дедлоки при частых подключениях), RWS README, publishers.py (unregister 10 с, TRANSIENT_LOCAL+lifespan 1 с) |
| c | `predict_live.py:473-486` при препятствии `send_twist_msg` **ничего не публикует** (а не ноль); порог 0,25 м в коде против 0,35 м в README | twist_mux/diff_drive: стоп по таймауту — штатная практика; есть ли таймаут в прошивке STM32 — неизвестно |
| d | Движение — блокирующие сценарии с `time.sleep(2..7)` (`:401-452`); во время движения жесты игнорируются; конец жеста = 1,5 с без руки | дуальные архитектуры: быстрый слой должен быть прерываемым |
| e | `load_yolo_model` — `torch.hub.load(...)` в бесконечном цикле без паузы (`:510-520`) | torch.hub тянет код репозитория; без сети/кэша — вечный busy-loop при старте **[О]** |
| f | `requirements.txt`: torch 2.4.1, ultralytics 8.3.190; README: PyTorch 1.13, CUDA 10.2 | Qengineering: PyTorch 2.x требует CUDA 11 — на Nano невозможно |
| g | Два независимых источника /cmd_vel: таймер демо (10 Гц) и MCP-сервер через rosbridge | twist_mux с приоритетами — стандартное решение |
</content>
</invoke>

---

## 8. Рекомендации (конкретно, по приоритету)

1. **Рефлекс локально, LLM — вне контура.** Жест (YOLO+SNN) → команда → `/cmd_vel` целиком на плате, без LLM и без MCP в
   пути; агент (PicoClaw/EdgeCtx) получает **событие** «распознан жест N, выполняю сценарий S» и может отменить/изменить
   (по образцу Helix/GR00T: медленный слой асинхронно, быстрый не ждёт). Цель задержки «конец жеста → начало движения» задать
   числом (например, ≤300 мс без учёта паузы-терминатора) и мерить.
2. **Одно постоянное соединение с ROS** вместо connect-per-message: в MCP-сервере — один WS-клиент (advertise один раз при
   старте, переподключение с backoff), либо (лучше для движения) нативный узел rclpy/rclcpp. Это уже сделано в нашем robot-mcp
   (Go) — переиспользовать подход. Проверить на стенде sim (rosbridge 2.7.1) потерю первых сообщений после простоя >10 с.
3. **Мультиплексор и стоп по таймауту**: `twist_mux` (есть для Foxy 4.0.1): вход «жесты» (приоритет выше), вход «агент/MCP»
   (ниже), lock «e_stop/препятствие» (255), timeout 0,3–0,5 с → ноль. При препятствии публиковать **ноль**, а не молчать.
   Узнать, есть ли таймаут в прошивке STM32; если нет — добавить (сторожевой таймер ~0,5 с).
4. **Камера**: `try_wait_for_frames(timeout)` + счётчик неудач → `pipe.stop()/start()` → при повторе `hardware_reset()` +
   пауза 2–3 с → пересоздание pipeline; лучше — вынести камеру в отдельный процесс/узел (realsense-ros с `initial_reset:=true`,
   `reconnect_timeout`), тогда её перезапуск не трогает детектор и агент. Захват и инференс — в разных потоках (arXiv 2608.14727:
   основной выигрыш именно от перекрытия).
5. **Сторожа процессов**: каждый узел — под systemd (`Type=notify`, `WatchdogSec=2..5`, `Restart=on-failure`, `RestartSec=0.5`)
   или `respawn=True` в launch + внутренний heartbeat рабочего цикла (если цикл камеры не тикал >N с — `os._exit(1)`, чтобы
   respawn/systemd подняли процесс). Состояние (последний жест, режим, сценарий) — во внешнем хранилище (файл в tmpfs/SQLite),
   чтобы перезапуск был «невидим». Daemon-потоки с необработанными исключениями — запрещены.
6. **Детектор**: экспорт YOLOv5n в TensorRT FP16 (INT8 на Nano бессмысленен), вход 320; замерить `trtexec` 320/640; держать
   вентилятор/режим питания, логировать `tegrastats` (троттлинг маскируется под «зависание»). Памяти 4 ГБ мало для PyTorch+CUDA
   (+~1 ГиБ на старте CUDA) — TensorRT-движок вместо PyTorch-модели снижает и задержку, и RAM (проверить замером).
7. **Метрики «во времени» (ключевая метрика ВКР)**: задержка жест→движение (p50/p95), FPS камеры, число авто-перезапусков и
   их длительность (MTTR), RSS rosbridge/агента/детектора по часам, температура/частоты, вызовы LLM/токены (= 0 на рефлекс).
   Многочасовой прогон в Gazebo (TurtleBot3 + синтетический «жест» как событие) до выхода на железо.
8. **Платформа**: на время ВКР — стенд Jazzy (rosbridge 2.7.1, twist_mux 4.5.0, Nav2 collision_monitor); для робота — либо
   оставить Foxy и учесть ограничения (rosbridge 1.3.1, нет collision_monitor), либо контейнер с Humble; решение за лабораторией.

## 9. Вопросы лаборатории

1. Точная модель платы (Nano 4 ГБ/2 ГБ? или Orin Nano), образ ОС (официальный JetPack 4.6.x или неофициальная Ubuntu 20.04),
   реальные версии torch/torchvision/ultralytics/librealsense/rosbridge (`pip freeze`, `dpkg -l | grep ros-foxy`).
2. Что именно пишет система «надо перезагружать» — текст ошибки, кто пишет (PicoClaw, MCP-сервер, rosbridge, демо)? Есть логи/журналы?
   Что перезагружают — процесс, rosbridge, всю плату? Через сколько использований/минут?
3. Как запускаются процессы (launch, systemd, руками в терминалах)? Есть ли вентилятор, какой `nvpmodel`?
4. Прошивка STM32: есть ли остановка по таймауту отсутствия команд? Частота/формат UART-кадров?
5. Кто публикует в /cmd_vel одновременно — демо (`VelocityPublisher`) и MCP/PicoClaw? Как они должны сосуществовать (жест важнее агента?)
6. Камера: USB 3? Кабель/хаб? Какие потоки включены (только color 640×480@30 или ещё depth)? Бывает ли `Frame didn't arrive`?
7. Какая роль у PicoClaw в целевом сценарии: только диалог/сводки, или он должен управлять движением? Какая модель (gpt-4o-mini/4.1-mini/5-mini)?
8. Есть ли планы перенести SNN на «гибридный нейропроцессор» (какой интерфейс/SDK — «NeuroChip SDK/MCP Server» из readme)?
9. Можно ли получить запись (rosbag) реальных сессий с жестами — для воспроизведения на стенде?

## 10. Источники (все открывались 14.10.2026, кроме помеченных [F-])
- Jetson/YOLO: github.com/ultralytics/yolov5/releases/tag/v6.0; wiki.seeedstudio.com/YOLOv5-Object-Detection-Jetson/;
  forums.developer.nvidia.com/t/…/198282; arxiv.org/abs/2608.14727; docs.ultralytics.com/guides/nvidia-jetson;
  forums.developer.nvidia.com/t/…/314302 (JetPack 4 EOL); github.com/Qengineering/Jetson-Nano-Ubuntu-20-image;
  forums.developer.nvidia.com/t/…/183240 (RAM .cuda()); …/183506 (троттлинг); [F-] …/84060, …/218184 (INT8); [F-] Seeed blog 2022.
- RealSense: raw…/librealsense/master/doc/installation_jetson.md; forums.developer.nvidia.com/t/…/328056;
  github.com/IntelRealSense/librealsense/blob/master/wrappers/python/pyrs_pipeline.cpp; github.com/IntelRealSense/realsense-ros;
  [F-] support.realsenseai.com/…/38466525657491.
- rosbridge: rosdistro foxy/jazzy distribution.yaml; index.ros.org/p/rosbridge_server; ROSBRIDGE_PROTOCOL.md; publishers.py @1.3.1/@2.7.1;
  rosbridge_websocket_launch.xml @1.3.1; CHANGELOG.rst (library, server); PR #774, #803; github.com/v-kiniv/rws; ros2_documentation Releases.rst.
- ROS 2 устойчивость: ros2/launch execute_process.py (foxy); systemd.service.xml; design.ros2.org/articles/node_lifecycle.html;
  control.ros.org (diff_drive_controller, jazzy); ros-teleop/twist_mux config; docs.nav2.org (collision monitor, lifecycle [F-]).
- LLM/роботы: arXiv 2204.01691, 2209.07753, 2403.01823, 2502.19417, 2503.14734, 2410.13691; figure.ai/news/helix;
  artificialanalysis.ai (gpt-4o-mini, gpt-4-1-mini, gpt-5-mini*); developers.openai.com/api/docs/guides/latency-optimization;
  [F-] arXiv 2510.03342, 2312.10807, 2608.20379.
- MCP: github.com/robotmcp/ros-mcp-server; modelcontextprotocol.io spec 2025-11-25 (resources, tasks); discourse.openrobotics.org/t/…/55270;
  arXiv 2410.06472; [F-] arXiv 2511.03497; github.com/sipeed/picoclaw.
- Air-writing/SNN: pmc.ncbi.nlm.nih.gov/articles/PMC7013930; [F-] CVPR 2017 Amir; Front. Neurosci. 2020 Ceolini; Nature 2019 Pei; Proc. IEEE 2023 Eshraghian.
