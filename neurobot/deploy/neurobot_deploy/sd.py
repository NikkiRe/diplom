"""systemd и журнал: systemctl show / list-jobs / restart, journalctl -o json, sd_notify.

Работает на systemd 245 (Ubuntu 20.04): свойства NRestarts, InvocationID, WatchdogTimestampMonotonic есть
с v232–v235; journalctl --invocation появился только в v257, поэтому журнал запуска юнита выбирается
полями _SYSTEMD_INVOCATION_ID (вывод процессов) и INVOCATION_ID (сообщения самого systemd о юните).
Все вызовы — через runner (subprocess.run по умолчанию), тесты подменяют его.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from typing import Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence

UNIT_PROPS = ("Id", "LoadState", "ActiveState", "SubState", "Result", "NRestarts", "ExecMainStatus",
              "ExecMainCode", "StatusText", "Type", "WatchdogUSec", "WatchdogTimestampMonotonic",
              "ActiveEnterTimestampMonotonic", "InactiveExitTimestampMonotonic", "InvocationID",
              "UnitFileState", "MainPID")


_SPAN = re.compile(r"(\d+(?:\.\d+)?)\s*(us|ms|s|min|h|d)?")
_SPAN_S = {"": 1e-6, "us": 1e-6, "ms": 1e-3, "s": 1.0, "min": 60.0, "h": 3600.0, "d": 86400.0}


class SystemdError(RuntimeError):
    pass


def timespan_s(text: str) -> float:
    """Промежуток из systemctl show («3s», «500ms», «1min 30s», голое число — мкс) в секундах."""
    text = (text or "").strip()
    if text in ("", "infinity"):
        return 0.0
    return sum(float(n) * _SPAN_S[u] for n, u in _SPAN.findall(text))


class Entry(NamedTuple):
    ts: float          # стенное время, с
    priority: int
    message: str
    invocation: str
    process: bool = True   # вывод процесса юнита (иначе — сообщение самого systemd о юните)


def _message(value) -> str:
    if isinstance(value, list):  # не-UTF-8 журнал отдаёт массивом байтов
        return bytes(value).decode("utf-8", errors="replace")
    return value or ""


class Systemd:
    def __init__(self, run: Callable = subprocess.run):
        self._run = run

    def _out(self, args: Sequence[str], check: bool = True) -> str:
        # без NOTIFY_SOCKET: systemctl и journalctl systemd ≥ 254 сами шлют в него EXIT_STATUS, и из сторожа
        # (Type=notify, NotifyAccess=main) это было бы предупреждение systemd на каждый вызов [опыт, ВМ, systemd 255]
        env = {k: v for k, v in os.environ.items() if k != "NOTIFY_SOCKET"}
        try:
            r = self._run(list(args), capture_output=True, text=True, timeout=30, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise SystemdError("{}: {}".format(args[0], e))
        if check and r.returncode != 0:
            raise SystemdError("{}: код {}: {}".format(" ".join(args), r.returncode, (r.stderr or "").strip()))
        return r.stdout

    def show(self, units: Sequence[str], props: Sequence[str] = UNIT_PROPS) -> Dict[str, Dict[str, str]]:
        """{юнит: {свойство: значение}}; блоки systemctl show разделены пустой строкой, юнит — по Id."""
        if not units:
            return {}
        text = self._out(["systemctl", "show", "--no-pager", "-p", ",".join(props)] + list(units))
        out = {}
        for block in text.split("\n\n"):
            props_ = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            if props_.get("Id"):
                out[props_["Id"]] = props_
        return out

    def jobs(self) -> Dict[str, str]:
        """{юнит: тип задания} для юнитов с ожидающим или идущим заданием."""
        out = {}
        for line in self._out(["systemctl", "list-jobs", "--no-legend", "--no-pager", "--plain"]).splitlines():
            parts = line.split()
            if len(parts) >= 3:
                out[parts[1]] = parts[2]
        return out

    def journal(self, invocation: Optional[str] = None, unit: Optional[str] = None, lines: int = 200,
                since: Optional[str] = None) -> List[Entry]:
        args = ["journalctl", "--no-pager", "-o", "json", "-n", str(lines)]
        if since:
            args.append("--since=" + since)
        if unit:
            args += ["-u", unit]
        if invocation:
            args += ["_SYSTEMD_INVOCATION_ID=" + invocation, "+", "INVOCATION_ID=" + invocation]
        try:
            text = self._out(args)
        except SystemdError as e:
            # не root и не в группе adm/systemd-journal: journalctl systemd ≥ 25x выходит с кодом 1 «insufficient
            # permissions» (раньше — код 0 и пусто); журнал недоступен — это не отказ проверки (doctor предупреждает)
            if "insufficient permissions" in str(e):
                return []
            raise
        out = []
        for line in text.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            out.append(Entry(int(e.get("__REALTIME_TIMESTAMP", 0)) / 1e6, int(e.get("PRIORITY", 6)),
                             _message(e.get("MESSAGE")),
                             e.get("_SYSTEMD_INVOCATION_ID") or e.get("INVOCATION_ID") or "",
                             bool(e.get("_SYSTEMD_INVOCATION_ID"))))
        return out

    def ctl(self, *args: str) -> None:
        self._out(["systemctl"] + list(args))

    def version(self) -> int:
        first = self._out(["systemctl", "--version"]).split()
        return int(first[1]) if len(first) > 1 and first[1].isdigit() else 0


def notify(state: str, environ: Mapping[str, str] = os.environ) -> bool:
    """sd_notify(3) без libsystemd — датаграмма в $NOTIFY_SOCKET (как neurobot/executor/sd_notify.py; пакет deploy
    ставится отдельно и берёт только stdlib). Без NOTIFY_SOCKET (запуск не из юнита) — ничего; ошибка отправки не
    роняет процесс: молчание заметит сам systemd."""
    addr = environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]  # абстрактное пространство имён
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(state.encode())
    except OSError:
        return False
    return True
