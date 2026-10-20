# RUNBOOK: робот NeuroChip под systemd

Коротко: всё поднимает `neurobot.target`, проверяет `neurobot doctor`. Если doctor пишет причину — найдите её код
ниже. Ручные запуски в терминале (`python3 predict_live.py`, `picoclaw gateway`, `nohup … &`) больше не нужны и
мешают: камера, `/cmd_vel`, порты. Подробности — `README.md` рядом.

Как едет робот: жест или команда агента → исполнитель → `/neurobot/cmd_vel_req` → safety_gate (единственный
писатель `/cmd_vel`) → twist_mux → diff_drive_controller → STM32. Агент скоростями не управляет и аварийную защёлку
снять не может.

## 1. Установка

Нужно: Ubuntu 20.04 + ROS 2 Foxy, `python3` (3.8); для MCP-сервера — Python ≥ 3.10 в отдельном окружении (`uv`).

1. Скопировать каталог `neurobot` (в нём `deploy`, `executor`, `safety`) на робот.
2. Env-файл по образцу: `cp deploy/neurobot.env.example my.env`; поправить пользователя, команды драйверов (лидар,
   база с twist_mux, камера `realsense2_camera`, фильтр IMU), интерпретатор и путь демо, пути к весам. Пометки
   «уточнить» — то, чего мы не знаем о роботе. Ремапы `-r /cmd_vel:=…` в командах исполнителя и распознавания не
   убирать: без них precheck откажет (§4.5).
3. Манифест весов: `sudo install -D -m 644 deploy/weights.sha256.example /etc/neurobot/weights.sha256` (если веса
   другие — `sha256sum <файл>` и поправить).
4. MCP-сервер, вариант А: наложить патч (`cd <ros_mcp_server> && patch -p1 --dry-run < …/mcp/ros_mcp_server.patch`,
   затем без `--dry-run`), конфиг — `sudo cp deploy/mcp/ros_mcp.json.example /etc/neurobot/ros_mcp.json`.
   Вариант Б (прокси лаборатории и её воркер) — `README.md` «Агентский путь».
5. `sudo deploy/install.sh --env my.env` — юниты, CLI `neurobot`, исполнитель и safety_gate, лимиты журнала,
   ротация `gateway.log`. Ничего не запускает. `--no-journald` — не ставить лимиты журнала (и не перезапускать
   journald), `--no-enable` — не включать автозапуск при загрузке; `--uninstall` — удалить (§8).
6. PicoClaw: `neurobot picoclaw-config merge` — оверлей по установленной версии (0.2.8 или 0.3.1), копия прежнего
   конфига остаётся рядом с ним. Затем `neurobot picoclaw-config check` — пусто, значит, годится.

Если doctor пишет `not_installed` — шаг 5 не выполнен или компонент выключен в `NEUROBOT_COMPONENTS`.

## 2. Запуск и остановка

```bash
sudo systemctl start neurobot.target      # всё, в порядке готовности
sudo systemctl stop neurobot.target       # всё; safety_gate шлёт явный ноль
sudo systemctl restart neurobot-mcp       # один компонент
sudo systemctl enable neurobot.target     # поднимать при загрузке (install.sh уже включил)
```

Порядок: драйверы (лидар, база, камера, фильтр IMU) → safety_gate → исполнитель → распознавание (прогрев до 2 мин) →
rosbridge → воркеры лаборатории (если есть) → MCP-сервер → PicoClaw; пульт оператора (если в составе) — сразу после
safety_gate. `starting` — ещё поднимается, подождать; дольше
пары минут — журнал юнита. `not_running` — юнит остановлен: запустить цель.

## 3. Проверка: neurobot doctor

```bash
sudo neurobot doctor          # таблица и ИТОГ: «ОК» или компонент, причина, что делать
sudo neurobot doctor --json   # то же для скриптов
```

Код выхода: 0 — ОК (предупреждения не мешают), 1 — проблема, 2 — проверка невозможна, 3 — ещё поднимается. Без
sudo doctor не видит журнал PicoClaw (нужна группа adm или systemd-journal). `env_invalid` — ошибка в
`/etc/neurobot/neurobot.env` (текст скажет, где); `ros_unavailable` — компоненты ROS запущены, а граф ROS doctor не
видит (не найден `NEUROBOT_ROS_SETUP`, другой `ROS_DOMAIN_ID`): датчики, safety_gate и писатели `/cmd_vel` не
проверены, поэтому это ошибка, а не «ОК».

## 4. Что делать по причине

### 4.1. Камера

`camera_missing`: камеры нет на USB (у драйвера камеры или у распознавания). `lsusb | grep 8086`; переткнуть кабель,
проверить питание. `camera_usb2` (предупреждение): камера на USB 2 — переткнуть в синий порт USB 3, иначе будут
«Frame didn't arrive». `camera_busy`: распознавание со своей камерой (`--camera rs`) вышло с кодом 6 — камеру держит
другой процесс (кто — в строке журнала распознавания); systemd его не перезапускает, чтобы модель не грузилась по
кругу. Остановить владельца, затем `sudo systemctl restart neurobot-perception`. На роботе с драйвером камеры
распознавание должно быть с `--camera ros`.

### 4.2. Веса

`weights_missing`: нет файла из `/etc/neurobot/weights.sha256` — вернуть. `weights_hash`: файл другой — вернуть
исходный или, если замена намеренная, пересчитать `sha256sum` и обновить манифест.

### 4.3. Занятый порт

`port_busy`: порт rosbridge (9090), MCP (18110), PicoClaw (18790) или воркера (8765, 8766) уже слушает чужой процесс —
doctor назовёт pid. `sudo ss -ltnp`, остановить его (обычно — ручной запуск в терминале, screen или `nohup`).

### 4.4. Ручной запуск и чужие писатели скорости

`foreign_process`: вне сервисов запущены `predict_live.py`, `realsense-viewer`, узел камеры `realsense2_camera`,
YOLO-воркер (`run_yolo_mcp_stack.sh`, `yolo_hand_worker.py`) или второй MCP-сервер — остановить. Камерой владеет один
процесс: узел `realsense2_camera` в юните `neurobot-driver@camera`, а распознавание читает его тему (`--camera ros`).
Если причина называет юнит `neurobot-driver@camera`, распознавание запущено со своей камерой (`--camera rs`) — нужен
`--camera ros`. MCP-сервер робота в юните PicoClaw значит, что PicoClaw ещё настроен на stdio (§4.5).

Цепочка скорости (`ros2 topic info -v <тема>` покажет узлы):
- `cmd_vel_writer`: в `/cmd_vel` пишет кто-то кроме safety_gate (или в `/neurobot/cmd_vel_req` — кроме
  исполнителя) — ручной запуск демо или скрипт агента; пока он есть, safety_gate (исполнитель) не стартует. Если это
  сам исполнитель или распознавание (`neurobot-wait: executor|perception: cmd_vel_writer`), у них не сработал ремап
  `/cmd_vel`: после готовности их проверяет `wait-graph`, systemd снимает процесс и остаётся метка — пока команда в
  env-файле та же, компонент не запустится (`neurobot-precheck: …: cmd_vel_writer: метка wait-graph …`). Вернуть
  ремап в команде (`NEUROBOT_EXECUTOR_CMD`, `NEUROBOT_PERCEPTION_CMD`; правило `-r /cmd_vel:=…` — одно: действует
  первое) — метка снимется сама. Если команда была верна, а ремап терялся в обёртке или launch-файле, — исправить их и
  удалить метку: `sudo rm /var/lib/neurobot/executor/cmd_vel_writer.json` (у распознавания — `…/perception/…`);
- `node_missing`: после старта исполнителя или распознавания их узла нет в графе — писателей не проверить, юнит
  перезапускается; имя узла (`NEUROBOT_EXECUTOR_NODE`, `NEUROBOT_PERCEPTION_NODE`) или `ROS_DOMAIN_ID` не те;
- `cmd_vel_override`: есть издатель `/cmd_vel_tracker` — twist_mux ставит его выше `/cmd_vel`, и явный ноль
  safety_gate не доходит до колёс; остановить его;
- `cmd_vel_bypass`: в вход базы `/diff_cont/cmd_vel_unstamped` пишет не twist_mux — мимо защиты; остановить узел;
- `twist_mux_missing` (предупреждение): twist_mux не слушает `/cmd_vel` — проверить launch базы
  (`NEUROBOT_DRIVER_base_CMD`): команды safety_gate не доходят до колёс;
- `cmd_vel_joy` (предупреждение): геймпад главнее safety_gate — во время демо его не трогать.

### 4.5. Конфиги

`config_invalid`: текст называет файл и ключ. Частые:
- PicoClaw не стартует с полем, которого нет в его версии (0.2.8 не знает `max_llm_retries`, `load_image`) — снова
  `neurobot picoclaw-config merge` (он подгоняет оверлей под версию);
- `gateway.log_level` не `info` — без этого не видно ни подключения MCP, ни зависших вызовов;
- в PicoClaw включён сервер робота по stdio (`robot_vision_demo_mcp`, `ros_mcp_server`) — второй набор инструментов
  движения; оставить один HTTP-сервер (`merge` выключает известные);
- у PicoClaw включены `exec` или `cron` с командами — shell для LLM на роботе (ставил пакеты, мог бы снять защёлку);
  `merge` их выключает;
- в конфиге MCP `topics.cmd_vel.name` не `/cmd_vel_agent`, пределы `motion.*` шире пределов агента, открыта запись в
  другие темы; не совпадают порты с env-файлом;
- в команде исполнителя нет `--ros-args -r /cmd_vel:=/neurobot/cmd_vel_req`, у распознавания — ремапа `/cmd_vel` в
  `/cmd_vel_gesture`: без них они пишут `/cmd_vel` сами, мимо safety_gate;
- rosbridge без `address:=127.0.0.1` (тогда он слушает сеть лаборатории), без `topics_glob`, `services_glob` или
  `params_glob` (пустой список `[]` для rosbridge на Foxy — «без ограничений»), с лишним в них (в `topics_glob` — только
  тема агента, в `services_glob` — только `/rosapi/…`, в `params_glob` — только `none`) или с аргументом, заданным
  дважды (`ros2 launch` берёт последний); глоб без кавычек YAML — rosbridge не стартует (образец —
  `neurobot.env.example`).

`unsupervised_launch` (предупреждение): драйвер или rosbridge запущен `ros2 launch …` без обёртки
`supervised.launch.py`. Если узел внутри такого launch умрёт, юнит останется active без узла, и systemd его не
поднимет (так rosbridge после смерти узла не поднимался до конца прогона на двойнике). Переписать команду в env-файле:
`ros2 launch /opt/neurobot/share/launchfiles/supervised.launch.py include:=пакет/файл.launch.py` и прежние аргументы
(образец — `neurobot.env.example`).

### 4.6. Драйверы и датчики

`device_missing`: нет `/dev/ttyACM0` (или что задано) — кабель, права (группа dialout). `topic_timeout`: драйвер
запустился, но тема молчит — `journalctl -u neurobot-driver@lidar -n 50`. `scan_stale`: нет свежего `/scan` —
safety_gate не пустит робота вперёд (повороты можно). `scan_publishers`: `/scan` публикуют два узла — лидар запущен
дважды (в графе 02.06 было два `/urg_node2`); оставить один. `scan_frame` (предупреждение): `frame_id` скана не
`laser_frame` — положение лидара в TF не найдётся; поправить драйвер или `NEUROBOT_SCAN_FRAME`. `scan_blind`
(предупреждение): в переднем секторе лидар стабильно не видит участок — вероятно, его закрывает камера D435i, стоящая
перед лидаром почти в плоскости скана, либо там пусто дальше 5,6 м. Проверка — лист картона в 0,5 м прямо перед
роботом и `ros2 topic echo /scan`; если закрыт — поднять лидар на проставке или опустить камеру.
`sensor_missing`, `sensor_stale`: датчик из `NEUROBOT_SENSORS` (`/odom`, `/tf`, `/joint_states`, …) не публикуется,
молчит или медленнее порога — `journalctl -u neurobot-driver@<имя> -n 50`. Пока какой-нибудь юнит ещё поднимается,
doctor пишет о молчащем датчике «ещё поднимается» — подождать. Зависший драйвер (процесс жив, тема молчит) сторож
перезапускает сам: тема старше предела две пробы подряд — строка `sensor_stale` в `journalctl -u neurobot-watchdog`.
Если молчат все датчики сразу, сторож никого не трогает (так выглядит беда DDS) — `sudo systemctl restart
neurobot.target`. Зависший `twist_mux` в launch базы сторож ловит по его статусу в `/diagnostics` и перезапускает
базу (строка `sensor_stale … /diagnostics@twist_mux`). Робот не реагирует на жесты, а doctor «ОК», в `journalctl -u
neurobot-safety` идут команды и такой строки нет — значит, twist_mux лаборатории не публикует `/diagnostics` (§9):
`sudo systemctl restart neurobot-driver@base`.

### 4.7. rosbridge

`port_timeout`: rosbridge не открыл порт за 30 с — `journalctl -u neurobot-rosbridge -n 50`. `rosbridge_down`:
процесс жив, но WebSocket не отвечает — сторож перезапустит его сам после двух неудачных проб (ping раз в секунду);
вручную — `sudo systemctl restart neurobot-rosbridge`. Жесты от rosbridge не зависят.

### 4.8. MCP-сервер

`mcp_timeout`: сервер не ответил при старте — `journalctl -u neurobot-mcp -n 50`. `mcp_down`: не отвечает в работе —
сторож перезапустит сам. `mcp_no_rosbridge`: сервер жив, но не достаёт rosbridge — см. §4.7. `mcp_worker_down`:
сервер жив, но его воркер не отвечает — см. §4.13. `mcp_schema`: у инструментов движения разные единицы
длительности — это прокси лаборатории без патча: дубли `robot_nudge_forward` и др., где миллисекунды уходят воркеру
как секунды (2000 «мс» = 2000 с); наложить `mcp/robot_vision_mcp_proxy.patch`.

Что агент отвечает о движении (ros_mcp_server с патчем): `robot_get_status` — поле `safety` (`state`, `reasons`,
`estop`) и `motion_allowed` только при `ok`/`slow`; `robot_nudge` — `ok: true`, только если safety_gate толчок
пропустил, иначе `status: estop` (защёлка, §4.14), `blocked` (с причинами safety_gate: препятствие, нет данных
лидара, наклон…) или `unconfirmed` (safety_gate толчка не видел: он не запущен или запрос не дошёл — `sudo neurobot
doctor`). Ответ «готово» при стоящем роботе — повод проверить, наложен ли патч и есть ли в конфиге MCP-сервера
`topics.safety_state` (предупреждение precheck mcp).

### 4.9. PicoClaw

`agent_zombie`: PicoClaw стартовал, когда MCP-сервер лежал, и замолчал при «зелёном» `/health` — сторож перезапустит
его, когда MCP поднимется. `agent_mcp_lost`: в журнале «connection closed» или «invalid trailing data» без
восстановления — то же. `agent_channel_lost`: канал VK оглох после ошибки long poll (PicoClaw его сам не
переподключает) — сторож перезапустит PicoClaw. `agent_hung`: вызов инструмента висит > 60 с — сторож перезапустит
MCP-сервер, через 90 с — PicoClaw. `agent_timeout`: PicoClaw не сообщил о подключении MCP — проверить `tools.mcp` и
`gateway.log_level` в его `config.json` (§4.5). Вручную: `sudo systemctl restart neurobot-picoclaw`.

### 4.10. safety_gate и исполнитель

`safety_silent`: safety_gate жив, но не публикует `/neurobot/safety_state` (5 Гц) —
`journalctl -u neurobot-safety -n 50`; при «тип в графе …» поправить `NEUROBOT_CMD_VEL_TYPE`. `safety_blocked`
(предупреждение): safety_gate запрещает движение — причины в тексте (препятствие, нет свежих датчиков, наклон и т. п.);
устранить причину, это не отказ. **Робот стоит `blocked` с причинами `obstacle` и `touch`** (или `slow` с `touch` и
одной из `approach`, `obstacle`, `cliff`: вперёд нельзя или только к препятствию или краю, поворот на месте заденет —
подъехал к стене под углом или к ножке стула) — тупик: ни один жест не ведёт назад, поэтому исполнитель через 3 с сам
отъезжает назад по своему следу, до 0,1 м (в журнале исполнителя — `escape: start` и `task_end escape: done`). Не вышло
— робот поставлен руками или развернулся вплотную (следа нет), в причинах `reverse_limit` (назад за самую дальнюю точку
пути уже 0,2 м) или касание сбоку — скажите агенту «отъедь назад» (`robot_nudge backward`), посмотрев, что сзади
свободно, или отведите робота рукой. `lidar_fault`: лидар не видит ничего дальше корпуса (весь скан «нет данных» или
«свободно») — отказ лидара или подменный `/scan`: `neurobot doctor`, `journalctl -u neurobot-driver@lidar -n 50`; в
помещении шире 11 м — ложный стоп (README safety, «Что НЕ защищено»). `gate_missing`: исполнитель не стартует, потому
что safety_gate не слушает `/neurobot/cmd_vel_req` — смотреть строку `safety` выше. `executor_silent`: исполнитель жив,
но не публикует `/neurobot/motion_state` — `journalctl -u neurobot-executor -n 50`. `zero_undelivered` (в журнале
safety_gate после остановки): явный ноль некому было принять — twist_mux не слушал `/cmd_vel`; базу тогда остановил
только её `cmd_vel_timeout`, проверить launch базы.

### 4.11. Распознавание

`perception_silent`: нет `/neurobot/perception_health` — `journalctl -u neurobot-perception -n 50`.
`perception_stalled`: камера восстанавливается (перезапуск конвейера → hardware_reset → выход); дольше минуты — §4.1.

### 4.12. Окружение Python

`python_env`: в интерпретаторе компонента нет нужного модуля или версия не та. Частое: `numpy` ≥ 2 в окружении ROS
(ROS 2 Foxy собран с NumPy 1.x; агент ставил numpy 2 сам через `exec`) — `pip install 'numpy<2'` в это окружение;
MCP-серверу нужен Python ≥ 3.10 и `mcp` ≥ 1.8. Предупреждение про шим pyenv или относительный путь: укажите
абсолютный путь интерпретатора — какой Python запустит шим, зависит от окружения.

### 4.13. Воркеры лаборатории

`labworker_timeout`: воркер не ответил 200 на пробу за 170 с (грузит модель дольше или упал) —
`journalctl -u neurobot-labworker@<имя> -n 50`; частое: torch.hub лезет в GitHub без сети. `labworker_down`: воркер не
отвечает в работе — `sudo systemctl restart neurobot-labworker@<имя>`. Второй экземпляр не стартует (`port_busy`).

### 4.14. Аварийная защёлка

```bash
sudo neurobot estop engage --reason "что случилось"   # поставить: может кто угодно
sudo neurobot estop release                            # снять: только человек, в терминале, через sudo, слово release
```

`estop_engaged`: защёлка стоит — робот не поедет, пока человек её не снимет. Она переживает рестарт safety_gate и
перезагрузку (хранится в `/var/lib/neurobot/safety`). Устранить причину (doctor покажет её), убедиться, что вокруг
свободно, затем `release`. Снятие — файл root в `/run/neurobot-estop`: без sudo его не записать, а команде «release» в
теме `/neurobot/estop` safety_gate не верит (кто её прислал, ROS не знает). Если за время между «посмотрел» и
«release» встала новая причина, safety_gate не снимет — посмотреть ещё раз. `estop_denied`: снимать пытались не из
терминала (скрипт, агент) или без sudo — так нельзя. `estop_failed`: safety_gate не ответил — `sudo neurobot doctor`;
остановить робот физически — кнопкой или `sudo systemctl stop neurobot-safety` (при остановке он шлёт ноль).

### 4.15. Пульт оператора

Пульт (компонент `ui`, юнит `neurobot-ui`) — окно в робот из браузера ноутбука: наблюдение, «СТОП движения» и
«Поставить защёлку». Снять защёлку из пульта нельзя — только §4.14 у робота. Робот без пульта работает: отказ пульта
doctor показывает предупреждением.

```bash
sudo neurobot ui-url                               # одноразовая ссылка входа (действует 2 мин), вход — на 12 ч
ssh -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -L 18120:127.0.0.1:18120 jetson@<робот>   # с ноутбука
```

`ServerAlive*` — чтобы уснувший ноутбук или оборванный Wi-Fi закрывали туннель за ≈15 с, а не через минуты
ретрансляций TCP; на стороне робота то же даёт `ClientAliveInterval 5` / `ClientAliveCountMax 3` в sshd (по согласию
лаборатории). Потоков у шлюза не больше 6: новый вытесняет самый старый.

«СТОП движения» в пульте — **не аварийная кнопка**: при обрыве Wi-Fi или туннеля она не сработает. Пульт защищает
только свой HTTP (127.0.0.1, туннель, токен); граф ROS робота открыт сети лаборатории (`ROS_LOCALHOST_ONLY` выключен):
стоп и защёлку в ROS может прислать любой ПК сети — это ограничение робота, не пульта.
`ui_down`: пульт не отвечает — `journalctl -u neurobot-ui`, `sudo systemctl restart neurobot-ui`. `ui_publisher`:
узел пульта пишет не только стоп и защёлку (чужая версия кода, ремап) — `sudo systemctl stop neurobot-ui` и
разобраться; `wait-graph ui` такой пульт и не запустит. `ui_denied`: ссылку входа выдаёт только root (`sudo`).
`ui_codes_unsafe`: `codes/` в каталоге пульта — ссылка или чужой каталог (так выглядит взломанный шлюз): ссылок не
выдавать, `sudo systemctl stop neurobot-ui` и разобраться. Ссылка «недействительна» — она одноразовая или прошло
2 мин: выдать новую. Вход действует 12 ч (срок держит шлюз). Выйти с одного ноутбука — удалить его файл в
`/var/lib/neurobot/ui/sessions/` (отпечаток и время входа — строка `login` в `audit.jsonl`), со всех —
`sudo rm /var/lib/neurobot/ui/sessions/*`; токен менять не нужно. Пульт сдался после 5 неудачных запусков за 5 мин
(`start-limit-hit`, doctor — предупреждение): устранить причину из журнала и `sudo systemctl reset-failed
neurobot-ui && sudo systemctl start neurobot-ui`.

## 5. Перезапуски, зависания, журналы

systemd перезапускает упавший компонент сам (`Restart=always`); doctor показывает число рестартов и последний отказ.
- `watchdog`: процесс завис (safety_gate и исполнитель — нет тика 3 с, распознавание — цикл стоит 5 с);
  systemd послал SIGABRT и перезапустил; стек Python — в журнале. После рестарта движение не возобновляется.
- `crashed`: процесс завершился с ошибкой — `journalctl -u <юнит> -n 50`.
- `start_timeout`: не стал готов за `TimeoutStartSec` (safety_gate — 30 с, исполнитель — 60 с, распознавание — 120 с).
- `restart_loop` (предупреждение в журнале распознавания): распознавание падает раз за разом, и перед каждым
  следующим стартом пауза растёт — 0, 5, 15, затем по 30 с, пока оно не проработает 10 мин. Причина — выше в `journalctl -u
  neurobot-perception`; нехватка памяти — `dmesg | grep -i oom`. Ручной `sudo systemctl restart neurobot-perception`
  стартует сразу. При нехватке памяти ядро сначала убивает пульт, затем rosbridge, MCP-сервер и PicoClaw, потом
  распознавание; safety_gate и исполнитель — последними.
- Сторож сам под надзором systemd: завис больше 30 с — systemd перезапускает его (`watchdog` в журнале сторожа).
- `systemd_error`: сторож не смог вызвать systemctl/journalctl — `journalctl -u neurobot-watchdog`.

```bash
journalctl -u neurobot-perception -b -n 100       # журнал компонента с последней загрузки
journalctl -u 'neurobot-*' --since -10min         # всё за 10 минут
journalctl -u neurobot-watchdog                   # что перезапускал сторож и почему
```

Журнал хранится на диске (≤ 200 МБ, 14 дней) и переживает перезагрузку; `gateway.log` PicoClaw ротируется раз в
неделю или при 20 МБ.

## 6. «Надо перезагружать»: как найти причину

| Симптом | Что посмотреть | Вероятная причина |
|---|---|---|
| Агент: «ошибка / недоступен / перезапустите» | `neurobot doctor`; `journalctl -u neurobot-picoclaw \| grep -E 'connection closed\|EOF\|invalid trailing\|reload'`; `dmesg \| grep -i oom` | обрыв MCP; воркер не запущен (`labworker_down`) |
| Агент молчит во всех чатах | `journalctl -u neurobot-picoclaw \| grep -E 'Tool execution (started\|completed)\|All MCP servers failed\|Long poll failed'` | зависший вызов, «зомби», оглохший VK |
| Агент «бегает по кругу» десятками вызовов | журнал PicoClaw: `Tool not found`, отклонённые аргументы | разные схемы инструментов, старые имена в сессиях (§7) |
| На жесты не реагирует, процесс жив | `neurobot doctor` (кадры/с, `perception_stalled`, `safety_blocked`, `estop_engaged`); `journalctl -u neurobot-perception \| grep -E "Frame didn't arrive\|Exception"` | камера отвалилась; safety_gate запрещает движение |
| Стоит у стены или стула, жесты отменяются | `neurobot doctor`: `safety_blocked` с `obstacle` и `touch` (или в `safety_state` — `slow` с `touch` и `approach`, `obstacle` или `cliff`); `journalctl -u neurobot-executor \| grep escape` | тупик: исполнитель отъедет назад сам; если в причинах `reverse_limit` — скажите агенту отъехать назад (§4.10) |
| После перезапуска камера не находится или старт висит | `dmesg \| tail`, doctor `camera_missing`, `foreign_process` (realsense2_camera), `start_timeout` | камера занята или не сброшена |
| Робот сам выключается или перезагружается после жестов | `journalctl --list-boots`, `uptime`, `dmesg \| grep -iE 'over-current\|usb .*disconnect'`; напряжение батареи под нагрузкой | просадка питания при пуске моторов (отсечка 11,1 В, общая шина 5 В) |
| Разворот вместо команды | журнал распознавания: «не распознано» / класс 0 | ошибка распознавания |
| «Сначала работало, потом хуже» | `tegrastats`, кадры/с в doctor | перегрев, троттлинг |
| Дёргается при командах агента | doctor `cmd_vel_writer`, `cmd_vel_override`, `cmd_vel_bypass`, `cmd_vel_joy` | второй писатель скорости |

Если причина не нашлась — собрать журналы скриптом Э0 (`collect_diag.sh`) и прислать.

## 7. Правила эксплуатации

- Шлюз PicoClaw не запускать в терминале и не останавливать Ctrl+C: он работает в юните, перезапуск —
  `sudo systemctl restart neurobot-picoclaw`.
- Правка `config.json` PicoClaw: правка → `neurobot picoclaw-config check` → `systemctl restart`. `/reload` и
  `hot_reload` не использовать.
- Имя MCP-сервера в PicoClaw (`robot_mcp`) не менять: от него зависят имена инструментов в сохранённых сессиях. После
  переименования или смены схемы инструментов — `/clear` в чате или удалить `~/.picoclaw/workspace/sessions/*`.
- `exec` у агента выключен: он больше не может ставить пакеты и «чинить» окружение. numpy в окружении ROS не обновлять.
- Аварийную защёлку снимает только человек, который видит робота (§4.14).
- Геймпад во время демо не трогать — он главнее safety_gate. Высоту мачты не менять: от неё зависят поле зрения
  камеры и затенение лидара.
- Пока не проверено на роботе, что база останавливается сама при тишине `/cmd_vel` (`cmd_vel_timeout` у
  diff_drive_controller), — работать только под присмотром.

## 8. Откат

```bash
sudo systemctl stop neurobot.target
sudo ./install.sh --uninstall          # юниты, drop-in журнала и logrotate — в /var/backups/neurobot/<время>/
cp ~/.picoclaw/config.json.neurobot-<время> ~/.picoclaw/config.json   # прежний конфиг PicoClaw
```

Конфиги `/etc/neurobot`, защёлка и метки `wait-graph` в `/var/lib/neurobot` остаются. Вернуть прежнюю версию
юнитов — скопировать их из `/var/backups/neurobot/<время>/etc/systemd/system/` обратно и `sudo systemctl
daemon-reload`. Патчи MCP-серверов снимаются `patch -R -p1 < <патч>`.

## 9. Чек-лист HIL (на роботе, вместе с лабораторией)

То, что на двойнике не проверить: у него другие ядро, cgroup, DDS, камера и тормоза. Порядок — сверху вниз; где нужно
решение лаборатории, это сказано. Колёса — сняты или робот на подставке, пока не сказано иное. Основание —
`docs/research/optimization/REVIEW.md` (§2.1, §4).

| # | Что | Как | Критерий / что решается |
|---|---|---|---|
| 1 | Факты платы (H11) | `bash collect_diag.sh --sudo` при запущенном стеке | `system/cgroup*.txt`: cgroup v1 или v2, доходят ли `MemoryMax` пульта и `CPUWeight` до файлов cgroup; THP, zram, swappiness, governor, цель загрузки (рабочий стол); `python/`: грузится ли TensorRT в Python 3.8 распознавания; `system/nvmap.txt` против `processes/memory.txt`: видна ли память CUDA в RSS распознавания (от этого зависит запас `OOMScoreAdjust`); `ros/`: версия realsense-ros по именам параметров, `publish_tf` madgwick, издатели `/diagnostics` (есть ли twist_mux) |
| 2 | Профили камеры и IMU (H5) | env-файл из `neurobot.env.example`; `ros2 param dump /camera/camera --print`, `ros2 topic hz` цвета, глубины и `/camera/imu` | профили приняты: глубина 848×480 15 к/с, цвет 640×480 15 к/с, IMU 200 Гц; иначе — имена параметров по версии из п. 1 |
| 3 | Сторож камеры по `camera_info` (H5) | `sudo systemctl stop neurobot-perception`, `ros2 topic hz /camera/color/camera_info` 30 с | `camera_info` идёт без подписчиков кадров; иначе вернуть `FRESH="/camera/color/image_raw:2"` |
| 4 | Разрешение глубины (H5, решение §2.5 REVIEW) | A/B: 424×240 15 к/с против 848×480 15 к/с с прореживанием, пресеты Default и High Accuracy; ровный пол, 1,2–2,5 м | шум высоты пола < 2 см (порог safety_gate), покрытие, CPU safety_gate; победитель — в env-файл |
| 5 | Поза камеры (H8) | 15 мин записи `/camera/depth/*`, `/tf_static`, `/imu/data`: стоя и на ходу | гистограмма высоты пола по дальности; по ней допуски калибровки на старте (±3°, ±3 см), затем код (МНК-плоскость пола 1,2–2,5 м) |
| 6 | Торможение (H9) | по 5 стопов с 0,10 и 0,15 м/с (запись `/odom`, `/cmd_vel`, `/camera/imu`); `ros2 param get /diff_cont linear.x.has_acceleration_limits` (и `max_acceleration`, `min_acceleration`) | худшие (не средние) `t_lat` и `a_brake` — в `d_stop` safety_gate; та же запись — порог удара `shock_mps2`; лимитов нет и лаборатория их не задаст — ограниченное замедление в L2 |
| 7 | Петля OOM и жертвы (H4) | балласт памяти в распознавании (budget, `ballast.py`) до OOM; `journalctl -u neurobot-perception`, `dmesg`; `cat /proc/<pid>/oom_score_adj` главных процессов юнитов | паузы 0, 5, 15, 30 с в строках `restart_loop`; тик safety_gate 20 Гц не проседает; первыми убиты пульт и агентский путь, safety_gate и исполнитель живы |
| 8 | Надзор (H16) | SIGSTOP узла `twist_mux` (`kill -STOP`); SIGSTOP процесса сторожа | база перезапущена ≤ 10 с (строка `/diagnostics@twist_mux`); сторож — новый запуск ≈ 36 с, стек в журнале |
| 9 | YOLO на TensorRT FP16 (H12) | если п. 1 показал TensorRT для 3.8: `trtexec --fp16` на самом Nano, вход 256×320 (h × w, как у AutoShape), и попытка с `--tacticSources=-CUDNN` | цикл распознавания и PSS до и после; IoU рамок ≥ 0,95 на ≥ 200 кадрах записей лаборатории |
| 10 | Геймпад под защёлкой (H17, **решение лаборатории** — это их yaml twist_mux) | если лаборатория согласна: в yaml twist_mux — тема блокировки (lock) с приоритетом 255, safety_gate публикует в неё `true` на время защёлки (правка safety_gate — после решения) | под защёлкой геймпад не двигает робот (приёмка 17.10, P4: 0,20 м мимо L2); цена — под защёлкой не отвести робот геймпадом, сначала снять защёлку |
