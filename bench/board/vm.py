"""Связь с ВМ Lima: команды board.sh и проб (чистые функции), их запуск через limactl shell, окружение отчётов.

Каталог проекта смонтирован в ВМ по тому же пути, поэтому пути к board.sh и probe.py одинаковы на Mac и в ВМ.
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOARD_SH = ROOT / "scripts/board/board.sh"
PROBE = ROOT / "bench/board/probe.py"
RECORD_PREFIX = "board-run: "
# Ответа board.sh run ждём дольше срока юнита: остановка (TimeoutStopSec=10s), запись итога, сам limactl.
STOP_MARGIN_S = 30.0


class VmError(RuntimeError):
    """Команда в ВМ не выполнена или её вывод не разобран; stdout и stderr — что она успела напечатать."""

    def __init__(self, message: str, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.stdout, self.stderr = stdout, stderr


def shell_command(vm: str, *args: str) -> list[str]:
    return ["limactl", "shell", "--workdir", "/", vm, "--", *args]


def board_command(vm: str, *args: str) -> list[str]:
    return shell_command(vm, "sudo", "bash", str(BOARD_SH), *args)


def run_args(name: str, cmd: list[str], *, mem: int, cpu: int, cpus: int | None = None, disk: str | None = None,
             net: str | None = None, workdir: str | None = None, gomemlimit: str | None = None,
             timeout: float | None = None, env: dict[str, str] | None = None) -> list[str]:
    """Аргументы board.sh run в порядке его разбора; None — умолчание board.sh."""
    args = ["run", name, "--mem", str(mem), "--cpu", str(cpu)]
    for flag, value in (("--cpus", cpus), ("--disk", disk), ("--net", net), ("--workdir", workdir),
                        ("--gomemlimit", gomemlimit), ("--timeout", timeout)):
        if value is not None:
            args += [flag, str(value)]
    for key, value in (env or {}).items():
        args += ["--env", f"{key}={value}"]
    return [*args, "--", *cmd]


def probe_command(probe: str, **options) -> list[str]:
    """python3 -B probe.py <проба> --ключ значение …; подчёркивания в именах ключей — дефисы."""
    cmd = ["python3", "-B", str(PROBE), probe]
    for key, value in options.items():
        cmd += [f"--{key.replace('_', '-')}", str(value)]
    return cmd


def parse_record(stderr: str) -> dict | None:
    """Итог юнита — последняя строка «board-run: {…}» в stderr board.sh run; None — такой строки нет."""
    for line in reversed(stderr.splitlines()):
        if line.startswith(RECORD_PREFIX):
            return json.loads(line[len(RECORD_PREFIX):])
    return None


def parse_lines(stdout: str) -> dict[str, dict]:
    """Строки проб {"phase": …} по этапам; у повторяющегося этапа (alloc) остаётся последняя. Прочие строки
    (вывод не-проб, например cpubench) пропускаются."""
    phases = {}
    for line in stdout.splitlines():
        if line.startswith('{"phase"'):
            doc = json.loads(line)
            phases[doc["phase"]] = doc
    return phases


def _text(data: str | bytes | None) -> str:
    """Вывод из TimeoutExpired: успевшее subprocess отдаёт байтами даже при text=True."""
    return data.decode(errors="replace") if isinstance(data, bytes) else data or ""


@dataclass
class RunResult:
    code: int
    stdout: str
    stderr: str
    record: dict
    phases: dict[str, dict]


class Vm:
    def __init__(self, name: str):
        self.name = name

    def _call(self, cmd: list[str], timeout: float, stdin: str | None = None) -> subprocess.CompletedProcess:
        feed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **feed)
        except subprocess.TimeoutExpired as e:
            raise VmError(f"{' '.join(cmd[:8])}…: нет ответа за {timeout:g} с", _text(e.stdout),
                          _text(e.stderr)) from e

    def shell(self, *args: str, timeout: float = 60) -> str:
        done = self._call(shell_command(self.name, *args), timeout)
        if done.returncode:
            raise VmError(f"{' '.join(args)}: код {done.returncode}: {done.stderr.strip()}")
        return done.stdout

    def board(self, *args: str, stdin: str | None = None, timeout: float = 60) -> str:
        done = self._call(board_command(self.name, *args), timeout, stdin)
        if done.returncode:
            raise VmError(f"board.sh {' '.join(args)}: код {done.returncode}: {done.stderr.strip()}")
        return done.stdout

    def tainted(self) -> int:
        return int(self.shell("cat", "/proc/sys/kernel/tainted"))

    def workdir(self) -> str:
        """~/work/board пользователя ВМ — на её собственном диске (io.max применим), для проб диска и cpubench."""
        return self.shell("sh", "-c", 'mkdir -p "$HOME/work/board" && echo "$HOME/work/board"').strip()

    def run(self, name: str, cmd: list[str], timeout: float, **limits) -> RunResult:
        """board.sh run со сроком юнита timeout с (RuntimeMaxSec — соблюдается в самой ВМ); код выхода команды —
        в RunResult.code, итог юнита — в record. Если ответа нет и с запасом STOP_MARGIN_S или запуск прерван на Mac,
        юнит останавливается явно: завершение клиента limactl до процессов в ВМ не доходит."""
        try:
            done = self._call(board_command(self.name, *run_args(name, cmd, timeout=timeout, **limits)),
                              timeout + STOP_MARGIN_S)
        except (VmError, KeyboardInterrupt):
            self._call(shell_command(self.name, "sudo", "systemctl", "stop", f"board-run-{name}.service"),
                       STOP_MARGIN_S)
            raise
        record = parse_record(done.stderr)
        if record is None:
            raise VmError(f"board.sh run {name}: код {done.returncode}, нет строки итога: "
                          f"{done.stderr.strip()[-300:]!r}", done.stdout, done.stderr)
        return RunResult(done.returncode, done.stdout, done.stderr, record, parse_lines(done.stdout))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _host(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"недоступно: {e}"


def environment(vm: Vm, files: list[Path]) -> dict:
    """Окружение отчёта: ВМ (ядро, ресурсы, нагрузка, tainted), Mac (нагрузка и питание — стенд общий и может
    работать от батареи, Lima) и SHA-256 файлов, от которых зависит результат."""
    lines = vm.shell("sh", "-c", "uname -r; nproc; awk '/MemTotal/ {print $2}' /proc/meminfo; cat /proc/loadavg; "
                                 "cat /proc/sys/kernel/tainted; systemctl --version | head -1").splitlines()
    return {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "vm": {"name": vm.name, "kernel": lines[0], "cpus": int(lines[1]), "mem_kib": int(lines[2]),
                   "loadavg": lines[3], "tainted": int(lines[4]), "systemd": lines[5]},
            "host": {"macos": platform.mac_ver()[0], "cpu": _host(["sysctl", "-n", "machdep.cpu.brand_string"]),
                     "loadavg": _host(["sysctl", "-n", "vm.loadavg"]).strip("{ }"),
                     "power": _host(["pmset", "-g", "batt"]).split("\n")[0], "lima": _host(["limactl", "--version"])},
            "sha256": {str(p.relative_to(ROOT)): sha256(p) for p in files}}
