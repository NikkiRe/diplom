"""python -m bench.board verify: пробы в ВМ по каждой оси и отчёт «задано vs измерено» (verify.json, verify.md).

Ожидания и допуски заданы здесь до прогона (README, «Проверка»), а не подбираются по результату. Расхождение —
находка для отчёта, а не повод менять допуск. Пробы — bench/board/probe.py, сервер сети — bench/board/server.py.
"""
from __future__ import annotations

import json
import math
import os
import statistics
from itertools import cycle
from pathlib import Path

from . import profiles
from .probe import MIB
from .server import ProbeServer
from .vm import BOARD_SH, RunResult, Vm, VmError, environment, probe_command

AXES = ("memory", "cpu", "disk", "net")
DEFAULT_SPECS = ("memory=64,16", "cpu=200/4,50/1,5/1", "disk=nvme,sd-good,sd-awful", "net=all")
PROBE_HOST = "host.lima.internal"
# Кадры на проводе, байт: SYN/SYN-ACK с опциями MSS, SACK, TS, WS — 14 Ethernet + 20 IP + 40 TCP;
# данные TCP — 14 + 20 + 32 (TCP с отметками времени); UDP — 14 + 20 + 8.
SYN_FRAME, TCP_OVERHEAD, UDP_OVERHEAD = 74, 66, 42
ECHO_BYTES, UDP_BYTES = 32, 64
Z99 = 2.576                      # двусторонние 99 % нормального распределения
DISK_TOLERANCE = 0.15            # доля от лимита io.max
CPU_TOLERANCE = (1.0, 0.10)      # п. п. одного ядра или доля ожидаемого — что больше
RTT_TOLERANCE = (3.0, 0.10)      # мс или доля ожидаемого — что больше; плюс 3 σ разброса медианы
RATE_SLACK = 1.05                # полезная скорость не выше предела netem с этим запасом
RATE_FLOOR = 0.5                 # и не ниже этой доли того, что успевает взять TCP с медленным стартом (без потерь)
TCP_IW_BYTES = 10 * 1448         # начальное окно TCP — 10 сегментов (RFC 6928)
UDP_PPS, LOSS_PPS = 200, 2000    # частота UDP-серии; у профилей с потерями — выше, чтобы потери было видно
CPU_SECONDS = 3.0
PING_SECONDS, PING_INTERVAL_MS = 20.0, 100.0
UNCONSTRAINED = {"mem": 256, "cpu": 400}   # ресурсы проб сети и диска: ограничивается только проверяемая ось


class SelectionError(ValueError):
    """Неверный выбор уровней в --profiles."""


# --- выбор уровней -------------------------------------------------------------------------------------------------

def parse_selection(specs: list[str], doc: dict) -> dict:
    """Спецификации «ось=уровень,…» поверх DEFAULT_SPECS: memory=64,16; cpu=50/1 (квота/ядра, ядра по умолчанию 1);
    disk=sd-good; net=all или имена; ось=none — пропустить; board=<плата> — уровни пресета платы по всем осям."""
    sel: dict = {}
    for spec in (*DEFAULT_SPECS, *specs):
        axis, _, value = spec.partition("=")
        if axis == "board":
            board = doc["boards"].get(value)
            if board is None:
                raise SelectionError(f"нет платы {value!r}; есть: {', '.join(doc['boards'])}")
            sel.update(memory=[board["memory"]], cpu=[(board["cpu"], board["cores"])], disk=[board["disk"]],
                       net=[board["net"]])
            continue
        if axis not in AXES or not value:
            raise SelectionError(f"{spec!r}: ожидается ось=уровни, ось — {', '.join(AXES)} или board")
        sel[axis] = [] if value == "none" else [_level(axis, item, doc) for item in value.split(",")]
        if axis == "net" and "all" in value.split(","):
            sel[axis] = list(doc["net"]["profiles"])
    return sel


def _level(axis: str, item: str, doc: dict):
    if axis == "net" and item == "all":
        return item
    allowed = {"memory": doc["memory"]["levels_mib"], "disk": doc["disk"], "net": doc["net"]["profiles"]}
    if axis == "cpu":
        quota, _, cores = item.partition("/")
        try:
            level = (int(quota), int(cores or 1))
        except ValueError:
            raise SelectionError(f"cpu={item!r}: ожидается квота[/ядра]") from None
        if level[0] not in doc["cpu"]["quota_pct"] or level[1] not in doc["cpu"]["cores"]:
            raise SelectionError(f"cpu={item!r}: квоты {doc['cpu']['quota_pct']}, ядра {doc['cpu']['cores']}")
        if level[0] > 100 * level[1]:
            raise SelectionError(f"cpu={item!r}: квота больше, чем дают ядра ({100 * level[1]} %)")
        return level
    level = int(item) if axis == "memory" and item.isdigit() else item
    if level not in allowed[axis]:
        raise SelectionError(f"{axis}={item!r}: нет такого уровня; есть: {', '.join(map(str, allowed[axis]))}")
    return level


# --- строки отчёта -------------------------------------------------------------------------------------------------

def row(axis: str, level: str, metric: str, unit: str, set_value, expected, measured, ok: bool | None,
        note: str = "") -> dict:
    """Строка «задано / ожидание / измерено»; ok=None — справочная (ожидания нет)."""
    return {"axis": axis, "level": level, "metric": metric, "unit": unit, "set": set_value, "expected": expected,
            "measured": measured, "ok": ok, "note": note}


def count_cpus(text: str | None) -> int:
    """Число CPU в списке cpuset («0-2,4» → 4)."""
    if not text:
        return 0
    ranges = (part.partition("-") for part in text.split(","))
    return sum(int(hi or lo) - int(lo) + 1 for lo, _, hi in ranges)


def _start_checks(axis: str, level: str, res: RunResult, env: dict[str, str]) -> list[dict]:
    """Запуск пробы: обычный пользователь и переменные рантайма Go, выставленные board.sh."""
    start = res.phases.get("start", {})
    rows = [row(axis, level, "uid процесса", "", "≠ 0", "≠ 0", start.get("uid"), start.get("uid", 0) != 0)]
    for key, value in env.items():
        got = start.get("env", {}).get(key)
        rows.append(row(axis, level, key, "", value, value, got, got == value))
    return rows


def memory_checks(level_mib: int, res: RunResult, gomemlimit_pct: int) -> list[dict]:
    level, rec, limit = f"{level_mib} МиБ", res.record, level_mib * MIB
    mem = rec["memory"]
    peak = mem["peak"] or 0
    alloc = res.phases.get("alloc", {}).get("mib", 0)
    return [
        row("memory", level, "memory.max", "МиБ", level_mib, level_mib,
            mem["max"] / MIB if isinstance(mem["max"], int) else mem["max"], mem["max"] == limit),
        row("memory", level, "memory.swap.max", "Б", 0, 0, mem["swap_max"], mem["swap_max"] == 0),
        row("memory", level, "итог пробы", "", "рост до гибели", "oom-kill", rec["reason"], rec["reason"] == "oom-kill",
            f"код {rec['exit_code']}, memory.events oom_kill={mem['events'].get('oom_kill')}"),
        row("memory", level, "memory.peak", "МиБ", level_mib, "90–100 % лимита", peak / MIB,
            0.9 * limit <= peak <= limit),
        row("memory", level, "выделено пробой", "МиБ", None, None, alloc, None,
            "до гибели; остальное — сам интерпретатор Python и страничный кэш его файлов"),
        *_start_checks("memory", level, res, {"GOMEMLIMIT": str(limit * gomemlimit_pct // 100)}),
    ]


def cpu_checks(quota: int, cores: int, res: RunResult, period_ms: int) -> list[dict]:
    level, rec = f"{quota} % / {cores} яд.", res.record
    stat, probe = rec["cpu"]["stat"] or {}, res.phases.get("cpu")
    expected = min(quota, 100 * cores)
    share = 100 * probe["cpu_s"] / probe["wall_s"] if probe else None
    tolerance = max(CPU_TOLERANCE[0], CPU_TOLERANCE[1] * expected)
    throttled = stat.get("nr_throttled", 0)
    quota_us = round(quota * period_ms * 10)
    rows = [
        row("cpu", level, "cpu.max", "", f"{quota} % за {period_ms} мс", f"{quota_us} {period_ms * 1000}",
            rec["cpu"]["max"], rec["cpu"]["max"] == f"{quota_us} {period_ms * 1000}"),
        row("cpu", level, "CPU в cpuset", "", cores, cores, count_cpus(rec["cpu"]["cpus"]),
            count_cpus(rec["cpu"]["cpus"]) == cores, rec["cpu"]["cpus"] or ""),
        row("cpu", level, "доля CPU", "% ядра", quota, expected, share,
            share is not None and abs(share - expected) <= tolerance,
            f"процессов с занятым циклом: {cores}, {CPU_SECONDS:g} с; допуск ±{tolerance:.1f} п. п."),
    ]
    if quota < 100 * cores:
        rows.append(row("cpu", level, "троттлинг (nr_throttled)", "", "> 0", "> 0", throttled, throttled > 0,
                        f"из {stat.get('nr_periods')} периодов; throttled {stat.get('throttled_usec', 0) / 1e6:.2f} с"))
    rows += _start_checks("cpu", level, res, {"GOMAXPROCS": str(cores)})
    return rows


def disk_plan(disk: dict) -> dict:
    """Объёмы на ≈3 с последовательной записи и ≈2 с запросов каждого вида при лимите; без лимита — потолок."""
    seq_mib = min(64, max(4, round(disk["wbps"] * 3 / MIB))) if "wbps" in disk else 64
    write_ios = min(4000, max(20, disk["wiops"] * 2)) if "wiops" in disk else 4000
    read_ios = min(4000, max(20, disk["riops"] * 2)) if "riops" in disk else 4000
    return {"seq_mib": seq_mib, "write_ios": write_ios, "read_ios": min(read_ios, seq_mib * MIB // 4096)}


def disk_checks(name: str, disk: dict, res: RunResult) -> list[dict]:
    rows = []
    for phase, key, unit in (("seq_write", "wbps", "МБ/с"), ("seq_read", "rbps", "МБ/с"),
                             ("iops_write", "wiops", "IOPS"), ("iops_read", "riops", "IOPS")):
        got = res.phases.get(phase, {})
        limit = disk.get(key)
        if "error" in got or not got.get("seconds"):
            rows.append(row("disk", name, phase, unit, limit, limit, None, False, got.get("error", "нет замера")))
            continue
        measured = got["bytes"] / got["seconds"]
        measured = measured / 1e6 if unit == "МБ/с" else measured / got["block"]
        expected = limit / 1e6 if limit and unit == "МБ/с" else limit
        rows.append(row("disk", name, phase, unit, expected, expected, measured,
                        None if limit is None else abs(measured / expected - 1) <= DISK_TOLERANCE,
                        f"запросов по {got['block'] // 1024} КиБ: {got['bytes'] // got['block']}, O_DIRECT"
                        + ("" if limit else "; без лимита — скорость диска ВМ")))
    io_max = res.record["io"]["max"] or {}
    wanted = {key: disk[key] for key in ("rbps", "wbps", "riops", "wiops") if key in disk}
    if wanted:
        device, got = next(iter(io_max.items()), ("—", {}))
        rows.append(row("disk", name, "io.max", "", wanted, wanted, {device: got},
                        {k: got.get(k) for k in wanted} == wanted, "устройство рабочего каталога (весь диск)"))
    return rows


def ser_ms(frame_bytes: int, rate_kbit: float | None) -> float:
    """Время передачи кадра на скорости netem, мс."""
    return frame_bytes * 8 / rate_kbit if rate_kbit else 0.0


def jitter_sd_ms(profile: dict) -> float:
    """σ суммарного равномерного разброса задержки ±j в двух направлениях."""
    return math.sqrt((profile["up"].get("jitter_ms", 0) ** 2 + profile["down"].get("jitter_ms", 0) ** 2) / 3)


def rtt_tolerance(expected: float, sd: float, n: int) -> float:
    return max(RTT_TOLERANCE[0], RTT_TOLERANCE[1] * expected) + 3 * sd / math.sqrt(max(n, 1))


def loss_inflation(d: dict) -> float:
    """Во сколько раз дисперсия доли потерь больше биномиальной: у Гилберта–Эллиотта потери идут пачками.
    Ковариация индикаторов потерь через k пакетов — (b−g)²·π(1−π)·λ^k, λ = 1 − p − r, π = p/(p+r)."""
    if "gemodel" not in d:
        return 1.0
    g = {k: v / 100 for k, v in d["gemodel"].items()}
    pi = g["p_pct"] / (g["p_pct"] + g["r_pct"])
    lam = 1 - g["p_pct"] - g["r_pct"]
    mean = profiles.direction_loss(d)
    var = mean * (1 - mean)
    if not var:
        return 1.0
    cov = (g["bad_loss_pct"] - g["good_loss_pct"]) ** 2 * pi * (1 - pi)
    return 1 + 2 * cov / var * lam / (1 - lam)


def loss_tolerance(d: dict, n: int) -> float:
    """Допуск доли потерь по n пакетам: 99 % нормального приближения с поправкой на пачки плюс один пакет."""
    p = profiles.direction_loss(d)
    return Z99 * math.sqrt(loss_inflation(d) * p * (1 - p) / n) + 1 / n if n else 1.0


def net_plan(profile: dict, base_rtt_ms: float) -> dict:
    """Параметры пробы net под профиль: ≈1,5 с эха, UDP не больше четверти самой медленной скорости (у профилей
    с потерями — до LOSS_PPS: при UDP_PPS допуск потерь lossy шире самих потерь), передачи по ≈2,5 с."""
    if profiles.round_trip_loss(profile) >= 1:
        return {"connects": 1, "echo": 0, "udp_pps": 10, "udp_seconds": 1, "timeout": 2, "grace": 1}
    rtt = (base_rtt_ms + profiles.added_rtt_ms(profile) + profile["up"].get("jitter_ms", 0)
           + profile["down"].get("jitter_ms", 0))
    rates = [d["rate_kbit"] for d in (profile["up"], profile["down"]) if d.get("rate_kbit")]
    lossy = any(profiles.direction_loss(profile[d]) for d in ("up", "down"))
    cap = LOSS_PPS if lossy else UDP_PPS
    pps = min(cap, 0.25 * min(rates) * 1000 / ((UDP_BYTES + UDP_OVERHEAD) * 8)) if rates else cap

    def volume(d: dict) -> int:
        return min(8 * MIB, max(16 * 1024, round(d["rate_kbit"] * 125 * 2.5))) if d.get("rate_kbit") else 8 * MIB

    return {"connects": 3, "echo": min(20, max(3, int(1500 / rtt))), "echo_bytes": ECHO_BYTES,
            "udp_pps": round(pps, 2), "udp_seconds": 5 if lossy else 2, "udp_bytes": UDP_BYTES,
            "down_bytes": volume(profile["down"]), "up_bytes": volume(profile["up"]),
            "timeout": round(5 + 4 * rtt / 1000, 2), "grace": round(1 + 2 * rtt / 1000, 2)}


def _answered(got: dict) -> list[float]:
    """Замеры этапа без пропусков (None — нет ответа за таймаут)."""
    return [ms for ms in got.get("ms") or [] if ms is not None]


def base_rtt(res: RunResult) -> dict:
    """База стенда без netns и netem: медианы connect и эха, мс; без них ожиданий профилей нет."""
    connect, echo = _answered(res.phases.get("connect", {})), _answered(res.phases.get("echo", {}))
    if not connect or not echo:
        raise VmError(f"база сети не измерена: {res.stdout.strip()[-300:]} {res.stderr.strip()[-300:]}")
    return {"connect_ms": statistics.median(connect), "echo_ms": statistics.median(echo)}


def expected_rtt_ms(base_ms: float, profile: dict, frame: int) -> float:
    """База стенда + задержки netem в обе стороны + передача кадра запроса вверх и ответа вниз на скоростях профиля."""
    return (base_ms + profiles.added_rtt_ms(profile) + ser_ms(frame, profile["up"].get("rate_kbit"))
            + ser_ms(frame, profile["down"].get("rate_kbit")))


def _rtt_row(name: str, metric: str, got: dict, base_ms: float, frame: int, profile: dict) -> dict:
    """RTT по медиане ответивших; пропуски (нет ответа за таймаут) допустимы только у профиля с потерями."""
    added, expected = profiles.added_rtt_ms(profile), expected_rtt_ms(base_ms, profile, frame)
    ms = _answered(got)
    if "error" in got or not ms:
        return row("net", name, metric, "мс", added, expected, None, False, got.get("error", "нет ответа"))
    missed = len(got["ms"]) - len(ms)
    tol = rtt_tolerance(expected, jitter_sd_ms(profile), len(ms))
    median = statistics.median(ms)
    return row("net", name, metric, "мс", added, expected, median,
               abs(median - expected) <= tol and (profiles.round_trip_loss(profile) > 0 or not missed),
               f"медиана {len(ms)}; база {base_ms:.2f} мс; допуск ±{tol:.1f} мс"
               + (f"; без ответа за таймаут — {missed}" if missed else ""))


def slow_start_share(nbytes: int, rate_kbit: float, rtt_ms: float) -> float:
    """Доля предела, которую TCP в среднем берёт за передачу nbytes: медленный старт удваивает окно каждый RTT
    от начального TCP_IW_BYTES до произведения скорости на RTT — это log2(BDP / IW) RTT сверх времени передачи
    на пределе (RFC 5681)."""
    t_ms, bdp = nbytes * 8 / rate_kbit, rate_kbit * rtt_ms / 8
    rounds = math.log2(bdp / TCP_IW_BYTES) if bdp > TCP_IW_BYTES else 0.0
    return t_ms / (t_ms + rounds * rtt_ms)


def _rate_row(name: str, metric: str, got: dict, limit: float | None, rtt_ms: float, lossy: bool) -> dict:
    """Полезная скорость TCP не выше предела netem (с запасом RATE_SLACK), а без потерь и не ниже RATE_FLOOR от доли,
    которую успевает взять медленный старт: слишком жёсткое ограничение тоже расхождение. С потерями нижней
    границы нет — скорость определяют повторы TCP."""
    if "error" in got or not got.get("seconds"):
        return row("net", name, metric, "кбит/с", limit, limit, None, False, got.get("error", "нет замера"))
    measured = got["bytes"] * 8 / got["seconds"] / 1000
    if not limit:
        return row("net", name, metric, "кбит/с", None, None, measured, None,
                   f"{got['bytes'] // 1024} КиБ; без предела netem — потолок стенда")
    note = f"{got['bytes'] // 1024} КиБ; {measured / limit:.0%} предела"
    if lossy:
        return row("net", name, metric, "кбит/с", limit, f"≤ {limit}", measured, measured <= RATE_SLACK * limit,
                   f"{note}; с потерями нижней границы нет")
    share = slow_start_share(got["bytes"], limit, rtt_ms)
    low = RATE_FLOOR * share * limit
    return row("net", name, metric, "кбит/с", limit, f"{low:.0f}–{limit}", measured,
               low <= measured <= RATE_SLACK * limit, f"{note}; медленный старт позволяет ≈ {share:.0%}")


def net_checks(name: str, profile: dict, base: dict, res: RunResult, counters: dict) -> list[dict]:
    phases = res.phases
    connect, udp = phases.get("connect", {}), phases.get("udp", {})
    if profiles.round_trip_loss(profile) >= 1:
        return [row("net", name, "TCP connect", "", "обрыв", "нет соединения",
                    f"соединений {len(_answered(connect))} из {len(connect.get('ms') or [])}",
                    "error" not in connect and bool(connect.get("ms")) and not _answered(connect)),
                row("net", name, "UDP: доля без ответа", "%", 100, 100,
                    100 * (1 - udp["echoed"] / udp["sent"]) if udp.get("sent") else None,
                    udp.get("sent", 0) > 0 and udp.get("echoed") == 0)]
    rows = [_rtt_row(name, "RTT TCP connect", connect, base["connect_ms"], SYN_FRAME, profile),
            _rtt_row(name, "RTT TCP-эха", phases.get("echo", {}), base["echo_ms"], ECHO_BYTES + TCP_OVERHEAD,
                     profile)]
    sd, echo = jitter_sd_ms(profile), _answered(phases.get("echo", {}))
    if sd and len(echo) > 1:
        rows.append(row("net", name, "разброс RTT эха (σ)", "мс", None, sd, statistics.pstdev(echo), None,
                        "равномерный ±jitter в каждую сторону" + ("; с потерями — вместе с повторами TCP"
                                                                  if profiles.round_trip_loss(profile) else "")))
    rows += _loss_rows(name, profile["up"], profile["down"], udp)
    rtt, lossy = base["echo_ms"] + profiles.added_rtt_ms(profile), profiles.round_trip_loss(profile) > 0
    rows += [_rate_row(name, "скорость вниз (TCP)", phases.get("down", {}), profile["down"].get("rate_kbit"), rtt,
                       lossy),
             _rate_row(name, "скорость вверх (TCP)", phases.get("up", {}), profile["up"].get("rate_kbit"), rtt,
                       lossy)]
    for direction, c in counters.items():
        if c["packets"] + c["drops"]:
            rows.append(row("net", name, f"netem {direction}: отброшено", "%", None, None,
                            100 * c["drops"] / (c["packets"] + c["drops"]), None,
                            f"{c['drops']} из {c['packets'] + c['drops']} пакетов за пробу: потери профиля "
                            "и переполнение очереди limit"))
    return rows


def _loss_rows(name: str, up: dict, down: dict, udp: dict) -> list[dict]:
    if "error" in udp or not udp.get("sent") or udp.get("server_received") is None:
        return [row("net", name, "потери UDP", "%", None, None, None, False,
                    udp.get("error") or udp.get("server_error") or "нет замера")]
    rows = []
    passed = udp["server_received"]
    for d, metric, lost, n in ((up, "потери вверх (UDP)", udp["sent"] - passed, udp["sent"]),
                               (down, "потери вниз (UDP)", passed - udp["echoed"], passed)):
        expected = profiles.direction_loss(d)
        measured = lost / n if n else None
        tol = loss_tolerance(d, n)
        rows.append(row("net", name, metric, "%", 100 * expected, 100 * expected,
                        None if measured is None else 100 * measured,
                        measured is not None and abs(measured - expected) <= tol,
                        f"{lost} из {n}; допуск ±{100 * tol:.2f} п. п."))
    return rows


def netem_counters(before: dict, after: dict) -> dict:
    """Приращение счётчиков netem (пакеты, отброшенные) по направлениям между двумя net-stats."""
    return {d: {k: after[d][k] - before[d][k] for k in ("packets", "drops")} for d in ("up", "down")}


def outages(series: list, interval_ms: float, min_lost: int = 2) -> list[tuple[float, float]]:
    """Обрывы в серии [мс от начала, RTT|None]: подряд не меньше min_lost без ответа; конец — первый ответ после
    обрыва (или конец серии). Секунды."""
    found, start, lost = [], None, 0
    for t_ms, rtt in series:
        if rtt is None:
            start, lost = (t_ms if start is None else start), lost + 1
            continue
        if start is not None and lost >= min_lost:
            found.append((start / 1000, t_ms / 1000))
        start, lost = None, 0
    if start is not None and lost >= min_lost:
        found.append((start / 1000, (series[-1][0] + interval_ms) / 1000))
    return found


def schedule_outages(schedule: dict, net_profiles: dict, offset_s: float, duration_s: float) -> list[tuple]:
    """Окна обрыва (профиль с потерей 100 %) по расписанию в секундах пробы: offset_s — начало пробы от начала
    расписания; окна обрезаются по [0, duration_s], смежные сливаются. Как в board.sh: неповторяющееся расписание
    после последнего шага оставляет профиль after."""
    steps = cycle(schedule["steps"]) if schedule["repeat"] else [
        *schedule["steps"], {"profile": schedule["after"], "for_s": math.inf}]
    windows, t = [], 0.0
    for step in steps:
        if t >= offset_s + duration_s:
            break
        a, b = max(t - offset_s, 0.0), min(t + step["for_s"] - offset_s, duration_s)
        if profiles.round_trip_loss(net_profiles[step["profile"]]) >= 1 and b > 0:
            if windows and windows[-1][1] == a:
                a = windows.pop()[0]
            windows.append((a, b))
        t += step["for_s"]
    return windows


def flaky_checks(name: str, schedule: dict, net_profiles: dict, ping: dict | None,
                 schedule_start_s: float) -> list[dict]:
    if not ping:
        return [row("net", name, "окна обрыва", "", None, None, None, False, "проба ping не дала итога")]
    duration = (ping["series"][-1][0] + ping["interval_ms"]) / 1000
    expected = schedule_outages(schedule, net_profiles, ping["t0"] - schedule_start_s, duration)
    found = outages(ping["series"], ping["interval_ms"])
    tol = ping["interval_ms"] / 1000 + 0.1
    ok = len(found) == len(expected) and all(abs(fa - ea) <= tol and abs(fb - eb) <= tol
                                             for (fa, fb), (ea, eb) in zip(found, expected, strict=True))

    def show(windows):
        return "; ".join(f"{a:.2f}–{b:.2f} с" for a, b in windows) or "нет"

    answered = sum(r is not None for _, r in ping["series"])
    return [row("net", name, "окна обрыва", "", show(expected), show(expected), show(found), ok,
                f"UDP-эхо каждые {ping['interval_ms']:g} мс; допуск границ ±{tol:.2f} с; "
                f"ответов {answered} из {len(ping['series'])}")]


# --- прогон --------------------------------------------------------------------------------------------------------

def sources() -> list[Path]:
    """Файлы, от которых зависят пробы, ожидания и допуски: их SHA-256 — в отчёте."""
    return [BOARD_SH, BOARD_SH.with_name("netem.jq"), BOARD_SH.with_name("record.jq"),
            *sorted(Path(__file__).parent.glob("*.py")), profiles.PROFILES_PATH]


class Verifier:
    def __init__(self, vm: Vm, doc: dict, out: Path):
        self.vm, self.doc, self.out = vm, doc, out
        self.tag = f"v{os.getpid() % 100000}"
        self.rows: list[dict] = []

    def run(self, name: str, cmd: list[str], timeout: float, **limits) -> RunResult:
        """Проба под board.sh run; её stdout и stderr — в raw/, в том числе если итога нет."""
        try:
            res = self.vm.run(f"{self.tag}-{name}", cmd, timeout, **limits)
        except VmError as e:
            self._raw(name, e.stdout, e.stderr)
            raise
        self._raw(name, res.stdout, res.stderr)
        return res

    def _raw(self, name: str, stdout: str, stderr: str) -> None:
        (self.out / "raw" / f"{name}.out").write_text(stdout, encoding="utf-8")
        (self.out / "raw" / f"{name}.err").write_text(stderr, encoding="utf-8")

    def memory(self, levels: list[int]) -> None:
        for level in levels:
            res = self.run(f"mem-{level}", probe_command("mem", max_mib=2 * level + 64), 120, mem=level, cpu=400)
            self.rows += memory_checks(level, res, self.doc["memory"]["gomemlimit_pct"])

    def cpu(self, levels: list[tuple[int, int]]) -> None:
        for quota, cores in levels:
            res = self.run(f"cpu-{quota}-{cores}", probe_command("cpu", workers=cores, seconds=CPU_SECONDS), 60,
                           mem=128, cpu=quota, cpus=cores)
            self.rows += cpu_checks(quota, cores, res, self.doc["cpu"]["period_ms"])

    def disk(self, names: list[str]) -> None:
        workdir = self.vm.workdir()
        for name in names:
            disk = self.doc["disk"][name]
            res = self.run(f"disk-{name}", probe_command("disk", dir=workdir, **disk_plan(disk)), 300,
                           disk=name, workdir=workdir, **UNCONSTRAINED)
            self.rows += disk_checks(name, disk, res)

    def net(self, names: list[str]) -> None:
        net_profiles = self.doc["net"]["profiles"]
        with ProbeServer() as server:
            ports = {"host": PROBE_HOST, "tcp_port": server.tcp_port, "udp_port": server.udp_port}
            lan_like = {"up": {"rate_kbit": 1_000_000}, "down": {"rate_kbit": 1_000_000}}
            res = self.run("net-base", probe_command("net", **ports, **net_plan(lan_like, 1.0)), 120, **UNCONSTRAINED)
            base = base_rtt(res)
            self.rows += [row("net", "база (без netns)", "RTT TCP connect / эха", "мс", None, None,
                              f"{base['connect_ms']:.2f} / {base['echo_ms']:.2f}", None,
                              "ВМ → прокси Lima → 127.0.0.1 Mac; прибавляется к ожиданиям профилей")]
            ns = self.tag
            try:
                for name in names:
                    profile = net_profiles[name]
                    if profiles.is_schedule(profile):
                        self._flaky(ns, name, profile, ports)
                        continue
                    self.vm.board("net-up", ns, name)
                    before = json.loads(self.vm.board("net-stats", ns))
                    res = self.run(f"net-{name}", probe_command("net", **ports, **net_plan(profile, base["echo_ms"])),
                                   300, net=ns, **UNCONSTRAINED)
                    after = json.loads(self.vm.board("net-stats", ns))
                    self.rows += net_checks(name, profile, base, res, netem_counters(before, after))
            finally:
                self.vm.board("net-down", ns)

    def _flaky(self, ns: str, name: str, profile: dict, ports: dict) -> None:
        self.vm.board("net-up", ns, name)
        start = json.loads(self.vm.board("net-stats", ns))["schedule_start_us"]
        cmd = probe_command("ping", host=ports["host"], udp_port=ports["udp_port"], seconds=PING_SECONDS,
                            interval_ms=PING_INTERVAL_MS)
        res = self.run(f"net-{name}", cmd, PING_SECONDS + 60, net=ns, **UNCONSTRAINED)
        self.rows += flaky_checks(name, profile["schedule"], self.doc["net"]["profiles"], res.phases.get("ping"),
                                  start / 1e6)


def verify(vm: Vm, doc: dict, selection: dict, out: Path) -> int:
    """Код 0 — всё совпало, 1 — есть расхождения (отчёт записан). VmError — прогон невозможен или сорван,
    ValueError — каталог отчёта не пуст."""
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"{out} не пуст — отчёты не перезаписываются")
    env = environment(vm, sources())
    if env["vm"]["tainted"]:
        raise VmError(f"ядро ВМ помечено tainted={env['vm']['tainted']} — проверка не начата")
    (out / "raw").mkdir(parents=True, exist_ok=True)
    v = Verifier(vm, doc, out)
    for axis in AXES:
        if selection.get(axis):
            getattr(v, axis)(selection[axis])
    tainted = vm.tainted()
    env["vm"]["tainted_after"] = tainted
    summary = {"rows": len(v.rows), "ok": sum(r["ok"] is True for r in v.rows),
               "failed": sum(r["ok"] is False for r in v.rows), "info": sum(r["ok"] is None for r in v.rows)}
    report = {"format": "board-verify/1", "env": env, "selection": selection, "summary": summary, "rows": v.rows}
    (out / "verify.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / "verify.md").write_text(render(report), encoding="utf-8")
    if tainted:
        raise VmError(f"ядро ВМ стало tainted={tainted} во время проверки — отчёт {out} записан, ВМ проверить")
    return 1 if summary["failed"] else 0


# --- отчёт ---------------------------------------------------------------------------------------------------------

TITLES = {"memory": "Память: MemoryMax, MemorySwapMax=0, GOMEMLIMIT",
          "cpu": "CPU: CPUQuota, AllowedCPUs, GOMAXPROCS",
          "disk": "Диск: io.max (IO*BandwidthMax, IO*IOPSMax), dd с O_DIRECT",
          "net": "Сеть: netns + veth + netem в обе стороны"}
VERDICT = {True: "совпадает", False: "**расходится**", None: "справочно"}


def fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.0f}" if abs(value) >= 100 else f"{value:.1f}" if abs(value) >= 10 else f"{value:.2f}"
    if isinstance(value, dict):
        return ", ".join(f"{k} {fmt(v)}" for k, v in value.items())
    return str(value).replace("|", "/")


def render(report: dict) -> str:
    env, s = report["env"], report["summary"]
    vm, host = env["vm"], env["host"]
    lines = ["# Эмуляция платы bench/board: задано vs измерено", "",
             f"Проверка {env['utc']}. **Ориентир, не результат**: ВМ Lima `{vm['name']}` на Mac, один прогон; "
             "ВМ общая с другими задачами, фоновая нагрузка не контролировалась.", "",
             f"- ВМ: ядро {vm['kernel']}, {vm['cpus']} vCPU, {vm['mem_kib'] // 1024} МиБ, {vm['systemd']}; "
             f"loadavg до проверки {vm['loadavg']}; tainted до/после {vm['tainted']}/{vm.get('tainted_after')}",
             f"- Mac: macOS {host['macos']}, {host['cpu']}, {host['lima']}; loadavg {host['loadavg']}; "
             f"питание: {host['power']}",
             f"- Итог: {s['rows']} строк — совпадает {s['ok']}, расходится {s['failed']}, справочно {s['info']}", ""]
    for axis in AXES:
        rows = [r for r in report["rows"] if r["axis"] == axis]
        if not rows:
            continue
        lines += [f"## {TITLES[axis]}", "", "| уровень | метрика | задано | ожидание | измерено | вывод | примечание |",
                  "|---|---|---|---|---|---|---|"]
        for r in rows:
            unit = f" {r['unit']}" if r["unit"] and r["measured"] is not None else ""
            lines.append(f"| {r['level']} | {r['metric']} | {fmt(r['set'])} | {fmt(r['expected'])} | "
                         f"{fmt(r['measured'])}{unit} | {VERDICT[r['ok']]} | {fmt(r['note']) if r['note'] else ''} |")
        lines.append("")
    return "\n".join(lines)
