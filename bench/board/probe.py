"""Пробы «убогой» платы: запускаются в ВМ под board.sh run и печатают по строке JSON на этап.

Только стандартная библиотека Python 3.12: в ВМ запускается файлом из смонтированного только для чтения дерева
проекта (python3 -B …/probe.py <проба> …), на Mac импортируется тестами и сервером (bench/board/server.py).
  mem   — выделять память шагами с касанием каждой страницы, пока cgroup не убьёт процесс (или до --max-mib);
  cpu   — N процессов с занятым циклом; доля CPU — по rusage детей;
  disk  — dd с O_DIRECT: последовательные запись/чтение блоками 1 МиБ, затем запросы по 4 КиБ (IOPS);
  net   — до сервера проб на Mac: время TCP connect, RTT TCP-эха, потери UDP вверх и вниз, полезная скорость
          вниз и вверх по TCP (от первого до последнего байта у получателя);
  ping  — серия UDP-эхо с постоянным шагом для обрывов по расписанию.
Каждая строка — {"phase": …, …}; ошибка этапа — поле error, проба продолжает следующий этап.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import select
import socket
import subprocess
import sys
import time
from pathlib import Path

PAGE = 4096
MIB = 1 << 20
CHUNK = 64 * 1024                 # кусок передачи по TCP
MAX_LINE = 64                     # длина командной строки протокола сервера проб
MAX_ECHO = 4096
MAX_TRANSFER = 64 * MIB
DD_LINE = re.compile(r"^(\d+) bytes .* copied, ([0-9.]+) s", re.MULTILINE)
ENV_KEYS = ("GOMEMLIMIT", "GOMAXPROCS")


def emit(phase: str, **fields) -> None:
    print(json.dumps({"phase": phase, **fields}, ensure_ascii=False), flush=True)


# --- память, CPU, диск ----------------------------------------------------------------------------------------------

def mem(max_mib: int, step_mib: int) -> None:
    """Каждый шаг — новый bytearray с записью в каждую страницу: память резидентна, а не только обещана."""
    touch = b"\x01" * (step_mib * MIB // PAGE)
    blocks = []
    for mib in range(step_mib, max_mib + 1, step_mib):
        block = bytearray(step_mib * MIB)
        block[::PAGE] = touch
        blocks.append(block)
        emit("alloc", mib=mib)
    emit("done", mib=len(blocks) * step_mib)


def cpu(workers: int, seconds: float) -> None:
    start = time.monotonic()
    end = start + seconds
    pids = []
    for _ in range(workers):
        pid = os.fork()
        if pid == 0:
            while time.monotonic() < end:
                pass
            os._exit(0)
        pids.append(pid)
    cpu_s = 0.0
    for pid in pids:
        _, _, usage = os.wait4(pid, 0)
        cpu_s += usage.ru_utime + usage.ru_stime
    emit("cpu", workers=workers, wall_s=time.monotonic() - start, cpu_s=cpu_s)


def parse_dd(stderr: str) -> tuple[int, float]:
    """Байты и секунды из итоговой строки dd (LC_ALL=C)."""
    match = DD_LINE.search(stderr)
    if not match:
        raise ValueError(f"нет итога dd: {stderr.strip()!r}")
    return int(match[1]), float(match[2])


def dd_runs(path: Path, seq_mib: int, write_ios: int, read_ios: int) -> list[tuple[str, list[str]]]:
    """Этапы диска: файл создаёт последовательная запись, запросы по 4 КиБ переписывают и читают его начало
    (по одному запросу за раз: соседние запросы не сливаются в один)."""
    return [
        ("seq_write", ["dd", "if=/dev/zero", f"of={path}", "bs=1M", f"count={seq_mib}", "oflag=direct"]),
        ("seq_read", ["dd", f"if={path}", "of=/dev/null", "bs=1M", f"count={seq_mib}", "iflag=direct"]),
        ("iops_write", ["dd", "if=/dev/zero", f"of={path}", "bs=4k", f"count={write_ios}", "oflag=direct",
                        "conv=notrunc"]),
        ("iops_read", ["dd", f"if={path}", "of=/dev/null", "bs=4k", f"count={read_ios}", "iflag=direct"]),
    ]


def disk(directory: Path, seq_mib: int, write_ios: int, read_ios: int) -> None:
    path = directory / f"board-disk-{os.getpid()}.bin"
    env = {**os.environ, "LC_ALL": "C"}
    try:
        for phase, cmd in dd_runs(path, seq_mib, write_ios, read_ios):
            done = subprocess.run(cmd, capture_output=True, text=True, env=env)
            if done.returncode:
                emit(phase, error=done.stderr.strip())
                continue
            try:
                nbytes, seconds = parse_dd(done.stderr)
            except ValueError as e:
                emit(phase, error=str(e))
                continue
            emit(phase, bytes=nbytes, seconds=seconds, block=MIB if phase.startswith("seq") else PAGE)
    finally:
        path.unlink(missing_ok=True)


# --- сеть: клиент протокола сервера проб -----------------------------------------------------------------------------
# TCP: одна строка команды, затем данные. «ECHO n» — эхо кусками по n байт до закрытия; «DOWN n» — сервер шлёт n байт;
# «UP n» — сервер принимает n байт и отвечает JSON {bytes, seconds, total} (от первого до последнего куска); «UDP s» —
# JSON {received} — сколько датаграмм сессии s дошло до сервера. UDP: датаграмма «<сессия> <номер> …», сервер
# отражает её.

def connect_ms(addr: tuple[str, int], timeout: float) -> float | None:
    """Время установления TCP-соединения, мс; None — не соединились за timeout (потери, обрыв)."""
    start = time.perf_counter()
    try:
        with socket.create_connection(addr, timeout=timeout):
            return round((time.perf_counter() - start) * 1000, 3)
    except TimeoutError:
        return None


def _open(addr: tuple[str, int], command: str, timeout: float) -> socket.socket:
    sock = socket.create_connection(addr, timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.sendall(command.encode() + b"\n")
    return sock


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("сервер закрыл соединение")
        data += chunk
    return bytes(data)


def echo_ms(addr: tuple[str, int], count: int, size: int, timeout: float) -> list[float]:
    """RTT запрос–ответ по одному TCP-соединению (TCP_NODELAY)."""
    message = b"e" * size
    rtts = []
    with _open(addr, f"ECHO {size}", timeout) as sock:
        for _ in range(count):
            start = time.perf_counter()
            sock.sendall(message)
            _recv_exact(sock, size)
            rtts.append(round((time.perf_counter() - start) * 1000, 3))
    return rtts


def download(addr: tuple[str, int], size: int, timeout: float) -> dict:
    """Скорость приёма: байты после первого куска за время от первого до последнего куска."""
    buf = bytearray(CHUNK)
    total = first = 0
    t_first = t_last = 0.0
    with _open(addr, f"DOWN {size}", timeout) as sock:
        while total < size:
            n = sock.recv_into(buf)
            if not n:
                raise ConnectionError(f"сервер закрыл соединение после {total} байт")
            t_last = time.perf_counter()
            if not total:
                first, t_first = n, t_last
            total += n
    return {"bytes": total - first, "seconds": t_last - t_first}


def upload(addr: tuple[str, int], size: int, timeout: float) -> dict:
    """Скорость отдачи по замеру сервера (так же: от первого до последнего принятого куска)."""
    payload = memoryview(bytes(CHUNK))
    with _open(addr, f"UP {size}", timeout) as sock:
        left = size
        while left:
            n = min(left, CHUNK)
            sock.sendall(payload[:n])
            left -= n
        sock.shutdown(socket.SHUT_WR)
        reply = sock.makefile("rb").readline(256)
    return json.loads(reply)


def udp_received(addr: tuple[str, int], session: str, timeout: float) -> int:
    with _open(addr, f"UDP {session}", timeout) as sock:
        return json.loads(sock.makefile("rb").readline(256))["received"]


def udp_train(addr: tuple[str, int], session: str, pps: float, seconds: float, size: int,
              grace: float) -> list[tuple[float, float | None]]:
    """Датаграммы с шагом 1/pps в течение seconds, ответы ждутся ещё grace с после последней.
    Итог — [(момент отправки, с эпохи; RTT, мс, или None — ответа нет)] по номерам."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    count = max(1, round(pps * seconds))
    sent: list[float] = []
    sent_mono: list[float] = []
    rtt: list[float | None] = [None] * count
    start = time.monotonic()
    deadline = start + (count - 1) / pps + grace
    with sock:
        while True:
            now = time.monotonic()
            if len(sent) < count and now >= start + len(sent) / pps:
                seq = len(sent)
                head = f"{session} {seq} ".encode()
                sent.append(time.time())
                sent_mono.append(time.monotonic())
                sock.sendto(head.ljust(size, b"."), addr)
                continue
            if now >= deadline:
                break
            wake = start + len(sent) / pps if len(sent) < count else deadline
            readable, _, _ = select.select([sock], [], [], max(0.0, wake - now))
            if not readable:
                continue
            try:
                seq = int(sock.recv(65535).split(b" ", 2)[1])
            except (BlockingIOError, IndexError, ValueError):   # ложная готовность или чужая датаграмма
                continue
            if 0 <= seq < len(sent) and rtt[seq] is None:
                rtt[seq] = (time.monotonic() - sent_mono[seq]) * 1000
    return list(zip(sent, rtt, strict=True))


def net(a: argparse.Namespace) -> None:
    tcp, udp = (a.host, a.tcp_port), (a.host, a.udp_port)
    stages = [
        ("connect", lambda: {"ms": [connect_ms(tcp, a.timeout) for _ in range(a.connects)]}),
        ("echo", lambda: {"bytes": a.echo_bytes, "ms": echo_ms(tcp, a.echo, a.echo_bytes, a.timeout)}),
        ("udp", lambda: _udp_loss(tcp, udp, a)),
        ("down", lambda: download(tcp, a.down_bytes, a.timeout)),
        ("up", lambda: upload(tcp, a.up_bytes, a.timeout)),
    ]
    skip = {"echo": not a.echo, "udp": not a.udp_pps, "down": not a.down_bytes, "up": not a.up_bytes}
    for phase, stage in stages:
        if skip.get(phase):
            continue
        try:
            emit(phase, **stage())
        except (OSError, ValueError) as e:
            emit(phase, error=f"{type(e).__name__}: {e}")


def _udp_loss(tcp: tuple[str, int], udp: tuple[str, int], a: argparse.Namespace) -> dict:
    """Серия UDP и счётчик сервера: потери вверх — не дошли до сервера, вниз — дошли, но ответа нет.
    Без связи (обрыв) счётчик не спросить — server_received = null, есть только доля без ответа."""
    session = secrets.token_hex(6)
    series = udp_train(udp, session, a.udp_pps, a.udp_seconds, a.udp_bytes, a.grace)
    rtts = [round(r, 3) for _, r in series if r is not None]
    doc = {"sent": len(series), "echoed": len(rtts), "server_received": None, "bytes": a.udp_bytes, "ms": rtts}
    try:
        doc["server_received"] = udp_received(tcp, session, a.timeout)
    except (OSError, ValueError) as e:
        doc["server_error"] = f"{type(e).__name__}: {e}"
    return doc


def ping(a: argparse.Namespace) -> None:
    series = udp_train((a.host, a.udp_port), secrets.token_hex(6), 1000 / a.interval_ms, a.seconds, a.udp_bytes,
                       a.grace)
    t0 = series[0][0]
    emit("ping", t0=t0, interval_ms=a.interval_ms,
         series=[[round((t - t0) * 1000, 1), None if r is None else round(r, 2)] for t, r in series])


# --- запуск ---------------------------------------------------------------------------------------------------------

def _number(kind, low, high=math.inf, open_low=False):
    """Тип аргумента: число kind в [low, high] (или (low, high] при open_low)."""
    def parse(text: str):
        value = kind(text)
        if not (low < value if open_low else low <= value) or value > high:
            raise argparse.ArgumentTypeError(f"ожидается число в {'(' if open_low else '['}{low}, {high}]")
        return value
    return parse


POSITIVE = _number(float, 0, open_low=True)
PORT = _number(int, 1, 65535)


def _network_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", required=True)
    p.add_argument("--udp-port", type=PORT, required=True)
    p.add_argument("--udp-bytes", type=_number(int, 16, 1400), default=64, help="размер датаграммы")
    p.add_argument("--grace", type=POSITIVE, default=1.0, help="ожидание ответов после серии UDP, с")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="probe.py", description="Пробы платы bench/board (запуск под board.sh run).")
    sub = ap.add_subparsers(dest="probe", required=True)
    p = sub.add_parser("mem")
    p.add_argument("--max-mib", type=_number(int, 1), required=True, help="предел выделения, если не убили раньше")
    p.add_argument("--step-mib", type=_number(int, 1), default=1)
    p = sub.add_parser("cpu")
    p.add_argument("--workers", type=_number(int, 1), required=True)
    p.add_argument("--seconds", type=POSITIVE, required=True)
    p = sub.add_parser("disk")
    p.add_argument("--dir", type=Path, required=True, help="каталог на проверяемом диске")
    p.add_argument("--seq-mib", type=_number(int, 1), required=True)
    p.add_argument("--write-ios", type=_number(int, 1), required=True, help="число запросов записи по 4 КиБ")
    p.add_argument("--read-ios", type=_number(int, 1), required=True,
                   help="число запросов чтения по 4 КиБ, не больше 256 на МиБ файла")
    p = sub.add_parser("net")
    _network_args(p)
    p.add_argument("--tcp-port", type=PORT, required=True)
    p.add_argument("--timeout", type=POSITIVE, default=10.0, help="предел одной операции TCP, с")
    p.add_argument("--connects", type=_number(int, 1), default=3)
    p.add_argument("--echo", type=_number(int, 0), default=10, help="число эхо-запросов (0 — без этапа)")
    p.add_argument("--echo-bytes", type=_number(int, 1, MAX_ECHO), default=32)
    p.add_argument("--udp-pps", type=_number(float, 0), default=50.0, help="датаграмм в секунду (0 — без этапа)")
    p.add_argument("--udp-seconds", type=POSITIVE, default=2.0)
    p.add_argument("--down-bytes", type=_number(int, 0, MAX_TRANSFER), default=0, help="объём приёма (0 — без этапа)")
    p.add_argument("--up-bytes", type=_number(int, 0, MAX_TRANSFER), default=0, help="объём отдачи (0 — без этапа)")
    p = sub.add_parser("ping")
    _network_args(p)
    p.add_argument("--seconds", type=POSITIVE, required=True)
    p.add_argument("--interval-ms", type=POSITIVE, default=100.0)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    emit("start", probe=a.probe, uid=os.getuid(), pid=os.getpid(), env={k: os.environ.get(k) for k in ENV_KEYS})
    if a.probe == "mem":
        mem(a.max_mib, a.step_mib)
    elif a.probe == "cpu":
        cpu(a.workers, a.seconds)
    elif a.probe == "disk":
        disk(a.dir, a.seq_mib, a.write_ios, a.read_ios)
    elif a.probe == "net":
        net(a)
    else:
        ping(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
