"""Дерево sysfs симулятора: корень по фикстуре hw-mcp и атомарные изменения атрибутов и ссылок.

Корень — копия hwmcp/testdata/fixture_root (её значения синтетические), чтобы инвентарь hw-mcp совпадал
с лабораторным сценарием; отличия под модели датчиков перечислены ниже. Атрибут меняется только целиком:
временный файл в том же каталоге и rename(2) — читатель видит старое или новое значение, но не
недописанное. Ошибку чтения (аналог -EIO из show() драйвера) обычный файл дать не может, поэтому атрибут
атомарно подменяется символьной ссылкой на собственный каталог: open() проходит, read() возвращает EISDIR.
Сбой w1_therm выглядит иначе — пустое чтение, это обычный пустой файл.
Отключение устройства — снятие его ссылки шины (sys/bus/…/devices), как при горячем отключении.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .sensors import MODELS, SENSORS, Device

FIXTURE = Path(__file__).resolve().parents[2] / "hwmcp/testdata/fixture_root"
ADC = "sys/devices/platform/soc/1f00074000.i2c/i2c-1/1-0048"
# Отличия от фикстуры: АЦП — ADS1115 (драйвер ti-ads1015 не даёт атрибута смещения), у SoC — ссылка класса
# thermal; неизменные атрибуты моделей — sensors.MODELS[…].static.
DROP = {f"{ADC}/iio:device2/in_voltage0_offset"}
NAMES = {f"{ADC}/name": "ads1115", f"{ADC}/iio:device2/name": "ads1115"}
CLASS_LINKS = {"sys/class/thermal/thermal_zone0": "sys/devices/virtual/thermal/thermal_zone0"}
W1_BUS = "sys/bus/w1/devices/"
W1_COUNT = "sys/devices/w1_bus_master1/w1_master_slave_count"


def _tmp(path: Path) -> Path:
    tmp = path.with_name(f".{path.name}.sysfsim")
    tmp.unlink(missing_ok=True)   # остаток прерванной записи
    return tmp


def atomic_write(path: Path, text: str) -> None:
    """Записать файл целиком: временный файл рядом и rename."""
    tmp = _tmp(path)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def break_attr(path: Path) -> None:
    """Сделать атрибут нечитаемым: атомарно заменить его ссылкой на свой каталог (read() → EISDIR)."""
    tmp = _tmp(path)
    os.symlink(".", tmp)
    os.replace(tmp, path)


def link_path(root: Path, link: str, target: str) -> None:
    """Относительная символьная ссылка root/link → root/target, как в настоящем /sys."""
    path = root / link
    path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(os.path.relpath(root / target, path.parent), path)


def init_root(root: Path) -> None:
    """Создать корень sysfs по фикстуре hw-mcp; каталог не должен существовать или должен быть пуст."""
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"{root}: каталог не пуст")
    shutil.copytree(FIXTURE, root, symlinks=True, dirs_exist_ok=True,
                    ignore=lambda d, names: {n for n in names if os.path.relpath(Path(d, n), FIXTURE) in DROP})
    for rel, text in NAMES.items():
        atomic_write(root / rel, text + "\n")
    for link, target in CLASS_LINKS.items():
        link_path(root, link, target)
    for d in SENSORS:
        for rel, text in MODELS[d.model].static.items():
            atomic_write(root / d.dir / rel, text + "\n")


class Tree:
    """Корень sysfs, который ведёт симулятор: атрибуты датчиков и ссылки шин."""

    def __init__(self, root: Path):
        missing = [d.dir for d in SENSORS if not (root / d.dir).is_dir()]
        if missing:
            raise FileNotFoundError(f"{root}: нет {missing[0]} — сначала python -m bench.sysfsim init --root {root}")
        self.root = root
        self._written: dict[tuple[str, str], str | None] = {}   # (устройство, атрибут) → текст или None
        for d in SENSORS:
            if d.link:   # после прерванного прогона устройство могло остаться отключённым
                self.plug(d, True)

    def write(self, dev: Device, attrs: dict[str, str | None]) -> int:
        """Записать изменившиеся атрибуты устройства (None — ошибка чтения); вернуть число записей."""
        n = 0
        for rel, text in attrs.items():
            key = dev.name, rel
            if key in self._written and self._written[key] == text:
                continue
            path = self.root / dev.dir / rel
            if text is None:
                break_attr(path)
            else:   # атрибут sysfs заканчивается переводом строки; пустое чтение — 0 байт
                atomic_write(path, f"{text}\n" if text else "")
            self._written[key] = text
            n += 1
        return n

    def plug(self, dev: Device, present: bool) -> None:
        """Вернуть или снять ссылку шины устройства; у 1-Wire — и w1_master_slave_count."""
        path = self.root / dev.link
        if present and not path.is_symlink():
            link_path(self.root, dev.link, dev.dir)
        elif not present and path.is_symlink():
            path.unlink()
        if dev.link.startswith(W1_BUS):
            count = sum((self.root / d.link).is_symlink() for d in SENSORS if d.link.startswith(W1_BUS))
            atomic_write(self.root / W1_COUNT, f"{count}\n")
