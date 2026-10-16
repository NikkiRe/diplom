# GAP2_artifacts: инвентаризация оборудования по sysfs и цена представлений контекста

| Файл | Что это |
|---|---|
| `sysfs_probe.py` | Детерминированная инвентаризация (IIO, hwmon, 1-Wire, V4L2, USB, I2C-клиенты, GPIO-чипы, HAT) только чтением sysfs, без опроса шин. Уровни наблюдаемости A/B/C. |
| `declared.json` | Слой «профилей»: то, чего ОС не знает (GPIO-линии, место установки, что за USB-UART). |
| `make_fixture.py` | Синтетическое дерево `/sys` + `/proc/device-tree/hat` по ABI ядра. Значения выдуманы. |
| `ctx_formats.py` | Строит 10 представлений контекста (TD 1.1, MCP explicit/generic, самодельный манифест, текст, SenML…) и считает байты и токены o200k_base. |
| `minijsonschema.py` | Подмножество JSON Schema draft-07 для проверки TD по официальной схеме. |
| `td-schema-v1.1_w3c_2025-03-12.json` | Официальная схема TD 1.1 (https://www.w3.org/2022/wot/td-schema/v1.1, version 1.1-12-March-2025). |
| `td11_spec_examples.json` | 5 полных TD из текста Рекомендации TD 1.1 (позитивные тесты валидатора). |
| `prepare_o200k.py` | Восстанавливает словарь o200k_base из локальной копии (VS Code Copilot) и сверяет SHA-256 с tiktoken. |
| `test_gap2.py` | 18 тестов (валидатор, разбор sysfs, формулы IIO, SenML, детерминизм, порядок токенов). |
| `ctx_formats_output_2026-10-06.txt` | Вывод прогона для k = 1, 5, 20. |
| `reps_k1/` | Сами представления для k = 1 (посмотреть глазами). |

## Воспроизведение (macOS, без загрузок)

```bash
cd research/new_topic/GAP2_artifacts
python3 make_fixture.py fixture_root
python3 sysfs_probe.py --root fixture_root --declared declared.json --out inventory_fixture.json
python3 prepare_o200k.py                       # нужен VS Code с Copilot; иначе только байты
PY=/private/tmp/gia-transcribe-venv/bin/python3 # любой python с tiktoken>=0.14
TIKTOKEN_CACHE_DIR=$PWD/.cache/tk $PY ctx_formats.py inventory_fixture.json --scale 1,5,20 --dump reps_k1
TIKTOKEN_CACHE_DIR=$PWD/.cache/tk $PY -m unittest test_gap2 -v   # 18 OK; без tiktoken 1 тест пропускается
```

`.cache/` (≈7 МБ) можно удалить, он восстанавливается `prepare_o200k.py`.

## На Raspberry Pi / в Linux-ВМ

```bash
python3 sysfs_probe.py --root / --declared declared.json      # реальный /sys
# снять фикстуру с реальной платы для тестов на Mac:
tar --dereference -czf pi_sys.tgz /sys/bus/iio/devices /sys/class/hwmon /sys/bus/w1/devices \
    /sys/class/video4linux /sys/bus/usb/devices /proc/device-tree/hat 2>/dev/null
# или umockdev-record (umockdev, LGPL-2.1) - запись sysfs+uevent для воспроизведения
```

Чего нет и что добавить на Linux: `VIDIOC_QUERYCAP` (`v4l2-ctl --all` или ioctl) для отличия
узла захвата от узла метаданных; `GPIO_V2_GET_LINEINFO_IOCTL` через libgpiod (`gpioinfo`,
python-биндинги `gpiod`) для имён и флагов линий; подписка на hotplug через
`pyudev.Monitor.from_netlink(context)` (источник `udev`, не `kernel`).

Ограничение счёта токенов: считается JSON-поле `tools`/текст ресурса. Как провайдер
вставляет tools в промпт, не публикуется, поэтому абсолютные числа - оценка;
сравнение представлений между собой корректно.
