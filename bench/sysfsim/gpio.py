"""Линии GPIO симулятора: файлы (как gpio.Files в hw-mcp) или настоящий модуль ядра gpio-sim.

Общий вид: set(линия, уровень) — вход, который двигает симулятор; get(линия) — выход, который пишет hw-mcp
(реле); chip — имя чипа для declared.json; edges — различает ли потребитель отдельные фронты (тогда
симулятор выдаёт и переходы дребезга); close() — освободить чип. Уровень физический: True — высокий.
"""
from __future__ import annotations

import fcntl
import os
import struct
import time
from pathlib import Path

from .tree import atomic_write

CONFIGFS = Path("/sys/kernel/config/gpio-sim")
PLATFORM = Path("/sys/devices/platform")
DEV = Path("/dev")
# GPIO_V2_GET_LINEINFO_IOCTL = _IOWR(0xB4, 0x05, struct gpio_v2_line_info) из include/uapi/linux/gpio.h:
# структура 256 байт, consumer — байты 32…63, offset (u32) — 64, flags (u64) — 72; GPIO_V2_LINE_FLAG_USED — бит 0.
LINEINFO_SIZE = 256
LINEINFO_IOCTL = 3 << 30 | LINEINFO_SIZE << 16 | 0xB4 << 8 | 0x05
LINE_FLAG_USED = 1


class GpioError(RuntimeError):
    """Чип GPIO недоступен или линия в неверном состоянии."""


class FilesGpio:
    """Линии-файлы <каталог>/<метка>/line<N> с уровнем «0»/«1», как hwmcp/internal/gpio/files.go: метка чипа —
    просто имя каталога. Фронтов нет: hw-mcp такие линии опрашивает."""
    edges = False

    def __init__(self, gpio_dir: Path, label: str):
        self.chip = label
        self.dir = gpio_dir / label
        self.dir.mkdir(parents=True, exist_ok=True)

    def set(self, line: int, level: bool) -> None:
        atomic_write(self.dir / f"line{line}", "1\n" if level else "0\n")

    def get(self, line: int) -> bool:
        """Уровень выхода; файла ещё нет (hw-mcp не запускали) — реле обесточено."""
        path = self.dir / f"line{line}"
        try:
            text = path.read_text().strip()
        except FileNotFoundError:
            return False
        if text not in ("0", "1"):
            raise GpioError(f"{path}: уровень {text!r}")
        return text == "1"

    def close(self) -> None:
        """Файлы линий остаются: следующий прогон их перезапишет."""


class GpioSim:
    """Чип модуля ядра gpio-sim (Documentation/admin-guide/gpio/gpio-sim.rst).

    Устройство configfs sysfsim-<pid> с банком label и именованными линиями. Вход двигается подтяжкой
    sim_gpio<N>/pull: для потребителя uAPI (/dev/gpiochipN) это настоящие фронты, с событиями и подавлением
    дребезга средствами ядра. Выход, который выставил потребитель, читается из sim_gpio<N>/value.
    Метка должна быть уникальной: чужие устройства gpio-sim не читаются (зависшее устройство повесило бы и
    этот запуск), а две одинаковые метки hw-mcp отвергает при поиске чипа.
    """
    edges = True
    RELEASE_WAIT_S = 5.0   # сколько close() ждёт, пока потребители отпустят линии

    def __init__(self, label: str, lines: dict[int, str]):
        if not CONFIGFS.is_dir():
            raise GpioError(f"нет {CONFIGFS}: нужны Linux, модуль gpio-sim (sudo modprobe gpio-sim) и configfs")
        if os.geteuid() != 0:
            raise GpioError("gpio-sim: нужны права root")
        self.num_lines = max(lines) + 1
        self.dev = CONFIGFS / f"sysfsim-{os.getpid()}"
        self.dev.mkdir()
        try:
            bank = self.dev / "bank0"
            bank.mkdir()
            (bank / "label").write_text(label)
            (bank / "num_lines").write_text(str(self.num_lines))
            for n, name in lines.items():
                (bank / f"line{n}").mkdir()
                (bank / f"line{n}" / "name").write_text(name)
            (self.dev / "live").write_text("1")
            self.lines = PLATFORM / (self.dev / "dev_name").read_text().strip() / self.chip
        except BaseException:
            self.close()
            raise

    @property
    def chip(self) -> str:
        """Имя чипа gpiochipN — из configfs при каждом обращении: close() после сбоя в __init__ (уже после
        live=1) не зависит от того, что __init__ успел запомнить."""
        return (self.dev / "bank0/chip_name").read_text().strip()

    def set(self, line: int, level: bool) -> None:
        (self.lines / f"sim_gpio{line}" / "pull").write_text("pull-up" if level else "pull-down")

    def get(self, line: int) -> bool:
        return (self.lines / f"sim_gpio{line}" / "value").read_text().strip() == "1"

    def busy(self) -> dict[int, str]:
        """Занятые потребителем линии банка: номер → имя потребителя (consumer) по GPIO_V2_GET_LINEINFO_IOCTL.
        Проверяются все линии 0…num_lines−1, не только линии платы: запросить можно любую."""
        out = {}
        with open(DEV / self.chip, "rb", buffering=0) as chip:
            for n in range(self.num_lines):
                info = bytearray(LINEINFO_SIZE)
                struct.pack_into("=I", info, 64, n)
                fcntl.ioctl(chip, LINEINFO_IOCTL, info)
                if struct.unpack_from("=Q", info, 72)[0] & LINE_FLAG_USED:
                    out[n] = info[32:64].split(b"\0", 1)[0].decode(errors="replace")
        return out

    def close(self) -> None:
        """Снять чип и удалить устройство configfs.

        Чип с занятыми линиями не снимается: на ядре 6.8 (ВМ sbc) это WARNING в gpiolib, утечка потоков IRQ
        потребителя и затем Oops. Сначала останавливают потребителей (hw-mcp, gpiomon), потом симулятор; если
        линии не освободились за RELEASE_WAIT_S, чип и устройство остаются, а close() сообщает, кто их держит.
        Между последней проверкой и снятием чипа остаётся окно в одну запись configfs: запрос линии именно в
        нём не обнаружится, поэтому порядок остановки обязателен.
        """
        live = self.dev / "live"
        if live.exists() and live.read_text().strip() == "1":
            deadline = time.monotonic() + self.RELEASE_WAIT_S
            while held := self.busy():
                if time.monotonic() >= deadline:
                    who = ", ".join(f"{n} ({consumer})" for n, consumer in held.items())
                    raise GpioError(f"линии {who} заняты: чип {self.chip} не снят; после остановки потребителя "
                                    f"уберите {self.dev} вручную (README, «GPIO»)")
                time.sleep(0.1)
            live.write_text("0")
        bank = self.dev / "bank0"
        if bank.is_dir():
            for line in bank.glob("line*"):
                line.rmdir()
            bank.rmdir()
        self.dev.rmdir()
