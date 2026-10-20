# Паспорта устройств и распознавание MCP

`scripts/edge_probe.py` — переносимый read-only сборщик на Python 3.8 и стандартной библиотеке.
Он работает с любой доступной Linux-системой, включая SBC с неизвестной проекту моделью. Список
поддерживаемых брендов не нужен: модель читается из Device Tree или DMI. Полнота зависит от того,
что экспортируют ядро, драйверы и права текущего пользователя.

## Паспорт на устройстве

Скачайте `/api/boards/probe.py`, перенесите один файл на нужную плату и выполните там:

```bash
python3 edge_probe.py > board.json
```

Импортируйте `board.json` в EdgeLab. Сборщик не требует пакетов, sudo, SSH или сетевых запросов.
Он не перебирает I²C-адреса, не открывает serial/SPI для обмена, не переключает GPIO, не получает
кадры и не запускает обнаруженные MCP-команды. Запуск на удалённом устройстве выполняет оператор;
браузер не получает SSH-учётные данные. Для MCP удалённой платы используйте отдельно настроенный
оператором локальный туннель и явно укажите loopback endpoint.

Например, если MCP на плате уже слушает `127.0.0.1:18090/mcp`, на своём компьютере выполните
`ssh -N -L 18130:127.0.0.1:18090 user@board`, а в «Подключениях» проверьте
`http://127.0.0.1:18130/mcp`. Замените `user@board` и удалённый порт на свои; локальный порт должен
быть свободен. Туннель поддерживает SSH, интерфейс не получает пароль/ключ и не запускает команду.
JSON-паспорт можно перенести обычным `scp user@board:board.json ./board.json`, затем импортировать.

`--root /path/to/fixture` предназначен для проверки сохранённого дерева Linux без реальной платы.
Символьные ссылки, выходящие из этого дерева, не читаются. Результат помечен
`collector.mode: filesystem_fixture`. Это не доказательство наличия физической платы.

Формат отчёта — `edge-board/1`:

```json
{
  "format": "edge-board/1",
  "collected_at": 1792520000,
  "collector": {"name": "edge-probe", "version": "1", "platform": "Linux", "mode": "local", "passive": true},
  "identity": {"kind": "linux_device", "model": null, "manufacturer": null, "source": null, "evidence": []},
  "system": {"os": {}, "architecture": "aarch64", "cpu": {}, "memory": {}, "uptime_s": null},
  "storage": [], "interfaces": [],
  "buses": {"usb": [], "i2c": [], "spi": [], "serial": [], "gpio": [], "iio": [], "hwmon": [], "camera": []},
  "services": [], "mcp_candidates": [], "coverage": [], "limits": {}, "redacted": true
}
```

Приоритет идентификации: `/sys/firmware/devicetree/base/model`, `/proc/device-tree/model`, затем
`/sys/class/dmi/id/product_name` / `board_name`. Производитель читается из Device Tree manufacturer
или DMI vendor; исходный `compatible` и его `vendor_id` сохраняются отдельно. Заполнители DMI вроде
`To Be Filled By O.E.M.` не считаются моделью. Имя хоста, CPU Hardware/SoC и модель платы не смешиваются.
`identity.evidence` содержит `{field,value,source}`; неизвестное значение остаётся `null`.

`kind: linux_device` не утверждает форм-фактор SBC: Device Tree также бывает у ARM-хостов и ВМ.
На macOS формируется `kind: host`, модель/CPU/память читаются через ограниченные sysctl-запросы,
ОС/архитектура — через Python platform. Linux-интерфейсы на Mac помечаются `unsupported`; Mac не
выдаётся за SBC. На других ОС формируется ограниченный паспорт хоста с явным отсутствием покрытия.

Linux-сбор включает:

- `/etc/os-release`, kernel release, `/proc/cpuinfo`, `/proc/meminfo`, uptime;
- блочные устройства, размеры из экспортированного числа 512-байтовых секторов, ro/removable;
- сетевые интерфейсы, состояние/MTU/скорость, без IP/MAC;
- USB vendor/product и interface drivers; перечисленные ядром I²C-устройства и адаптеры;
- SPI-устройства, serial/GPIO device nodes, GPIO-контроллеры из старого и нового sysfs;
- IIO и hwmon каналы в исходных единицах/строках ядра без выдуманного перевода единиц;
- video4linux узлы/названия камер; кадры не запрашиваются;
- установленные systemd-файлы и имена MCP-бинарников в известных системных каталогах.

Наличие узла или unit-файла не доказывает, что устройство подключено, драйвер исправен, служба
запущена или интерфейс разрешено использовать для управления. `accessible` отражает проверку прав
на узел, а не выполненную аппаратную операцию. Один физический узел может иметь несколько
экспортированных интерфейсов; суммы элементов разных категорий не являются числом физических плат.

`mcp_candidates` — пассивные свидетельства, а не результаты проверки сервера. Только явно записанные
loopback HTTP URL извлекаются из unit-файлов. Полный ExecStart, environment, ключи и пароли не
экспортируются. Пустой `url` означает, что endpoint не установлен; stdio/неизвестный транспорт
не превращается в выдуманный HTTP-порт. Службы systemd не запускаются и их состояние не меняется.

Coverage содержит `{id,label,status,count,sources,errors,limited}`. Возможны `ok`, `partial`,
`unavailable`, `permission_denied`, `unsupported`, `limited`. Пустая доступная категория отличается
от отсутствующего/недоступного sysfs. По умолчанию серийные номера, MAC, IP и credentials не собираются;
`redaction` описывает это явно. Неизвестные поля импортированного JSON сохраняются как предоставлены.

## Backend

`neurobot.ui.boards.BoardRegistry(state_dir=None)` хранит до 16 паспортов / 8 MiB. С `state_dir`
реестр сохраняется в `boards.json` атомарной заменой, mode 0600; повреждённый файл не затирается.
Один паспорт ограничен 1 MiB. Импорт принимает содержимое, а не серверные пути. SHA-256 отражает
содержимое отчёта, а не аутентификацию физического устройства. Новый снимок удалённой платы имеет
свой ID; одинаковые отчёты дедуплицируются по содержимому. Локальный паспорт имеет стабильный
`board-local` и обновляется при повторном probe.

Методы: `snapshot()`, `get(id)`, `local()`, `import_report(body)`, `forget(id)`, `inspect_mcp(body)`.
`mutate(registry,path,body)` возвращает `(HTTP code, JSON object)`.

- `GET /api/boards` → `{format:'edge-boards/1',boards:[entry],local_id,updated_at,probe,limits}`.
- Entry: `{id,label,origin:'local'|'import',report,sha256,imported_at}`.
- `POST /api/boards/local {}` → 201 `{board:entry}`; выполняется только фиксированный probe.
- `POST /api/boards/import {report:object,label?}` или `{content:JSONstring,label?}` → 201 `{board:entry}`.
- `POST /api/boards/forget {id}` → 200 `{forgotten:id}`; исходный файл устройства не удаляется.
- `GET /api/boards/probe.py` отдаёт фиксированный standalone-скрипт для переноса на плату.

Сбор ограничен 768 чтениями / 2 MiB данных, 64 KiB на файл, 128 результатов категории,
4096 просмотренными именами каталога и 1 MiB JSON-выхода. Ограничение фиксируется в coverage;
превышение общего размера не даёт тихо усечённый JSON. Плановый сбор занимает до 6 с; backend
дополнительно ограничивает дочернюю группу процессов 9 с и допускает один local probe одновременно.
Это ограниченный разовый паспорт, а не постоянно работающий монитор устройств.

## MCP helper

`POST /api/boards/mcp/inspect {url}` проверяет **только явно указанный** endpoint. Разрешён HTTP на
литеральных loopback `127.0.0.1`, `localhost` (закреплён за 127.0.0.1), `[::1]`. DNS, прокси,
перенаправления и сканирование портов/сети не используются. До двух проверок одновременно; общий
MCP deadline 6 с, ограничение результата 1 MiB. Сессия отдельная и закрывается после диагностики.

Последовательность: `initialize`, `notifications/initialized`, затем `tools/list` и `resources/list`
для объявленных capabilities. `tools/call` не выполняется даже для `list_devices`, значения ресурсов
не читаются, actuator/readOnly аннотации не дают разрешения запускать инструменты.

Результат содержит `url,status,transport,server,protocol,capabilities,tools,resources,coverage,issues,
read_only,checked_at,fingerprint,initialized,connected`. `connected:false` означает, что инспекция
сама не добавляет источник в атлас. Для этого оператор отдельно нажимает подключение каталога.

Статусы: `valid` — initialize и объявленные каталоги прочитаны; `partial` — часть описания нарушена
или недоступна; `auth_required` — 401/403; `not_mcp` — HTML, неправильный endpoint или не-MCP ответ;
`unreachable` — сетевой сбой/таймаут; `invalid` — неверная MCP-спецификация/версия; `stdio` — введена
команда/путь вместо HTTP URL. Stdio-команда не выполняется, её работоспособность не подтверждается.
`issues` содержит `{code,message,severity}`; fingerprint фиксирует имена инструментов и схемы URI,
а также `tools_called:0`, `resources_read:0`. Credentials не запрашиваются и не сохраняются.

## Сохранение подключённых источников

`neurobot.ui.source_state.SourceState(state_dir=None)` сохраняет только ранее явно добавленные
`{url,label}` в `mcp-sources.json`, формат `edge-mcp-sources/1`. `load()` проверяет до восьми
loopback URL через тот же `catalog.endpoint`, возвращает список и не выполняет RPC. `save(snapshot)`
принимает снимок каталога, исключает device data, статусы и MCP metadata, записывает приватный файл
0600 через атомарную замену и возвращает `None` либо строку ошибки. Последняя ошибка доступна в
`error`. С `state_dir=None` сохранение выключено. Каталог внутри Git не принимается.

Конфигурация ограничена 64 KiB. Повреждённый, слишком большой или подменённый symlink файл не
перезаписывается; ошибка не удаляет текущие источники каталога. Повторное подключение после
перезапуска выполняет сервер отдельно, только для успешно разобранных сохранённых URL.

## Проверка

```bash
python3 -m unittest neurobot.ui.tests.test_ui_boards -v

BOARD_TEST_HW_BIN=/absolute/path/to/hw-mcp \
python3 -m unittest neurobot.ui.tests.test_ui_boards -v
```

Проверяются произвольная ARM-плата со всеми категориями, DT/DMI fallback, неизвестная модель,
permission/limit coverage, автономность копии скрипта, настоящий локальный паспорт, atomic persistence,
HTML/auth/stdio/неизвестная версия MCP, SSRF и отсутствие выполнения инструментов. Вторая команда
дополнительно проверяет настоящий нативный hw-mcp на копии fixture-root и его trace без tool_call.
