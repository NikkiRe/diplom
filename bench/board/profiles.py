"""Профили «убогой» платы (profiles.json): загрузка, проверка и величины, которые из них следуют.

Файл читают и board.sh (через jq, в ВМ), и Python на Mac. Проверка здесь ловит ошибки до запуска в ВМ: опечатки
в ключах, профили без источника, недопустимые для ядра сочетания (квота меньше 1 мс за период; разброс задержки
без rate — netem тогда переупорядочивает пакеты). Только стандартная библиотека.
"""
from __future__ import annotations

import json
import math
import re
from itertools import pairwise
from pathlib import Path

PROFILES_PATH = Path(__file__).with_name("profiles.json")
NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DISK_LIMITS = ("rbps", "wbps", "riops", "wiops")
DIRECTION_KEYS = {"rate_kbit", "delay_ms", "jitter_ms", "loss_pct", "gemodel"}
GEMODEL_KEYS = ("p_pct", "r_pct", "bad_loss_pct", "good_loss_pct")
BOARD_KEYS = {"basis", "memory", "cpu", "cores", "disk", "net"}
MIN_CFS_QUOTA_MS = 1  # kernel/sched/core.c: min_cfs_quota_period — меньшую квоту ядро отвергает
STEP_FLOOR_S = 0.01   # шаг расписания короче самой смены шага в board.sh (два jq и два tc) смысла не имеет


class ProfileError(ValueError):
    """Ошибка в profiles.json; сообщение начинается с пути к ключу."""


def load(path: Path = PROFILES_PATH) -> dict:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    validate(doc)
    return doc


def _fail(where: str, message: str) -> None:
    raise ProfileError(f"{where}: {message}")


def _object(where: str, obj) -> None:
    if not isinstance(obj, dict):
        _fail(where, "ожидается объект")


def _keys(where: str, obj, required: set[str], optional: set[str] = frozenset()) -> None:
    _object(where, obj)
    missing, extra = required - obj.keys(), obj.keys() - required - optional
    if missing:
        _fail(where, f"нет ключей {sorted(missing)}")
    if extra:
        _fail(where, f"лишние ключи {sorted(extra)}")


def _number(where: str, value, low: float, high: float = math.inf, integer: bool = False) -> None:
    kind = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kind) or not low <= value <= high:
        _fail(where, f"ожидается {'целое' if integer else 'число'} в [{low}, {high}], получено {value!r}")


def _levels(where: str, levels) -> None:
    if not isinstance(levels, list) or not levels:
        _fail(where, "ожидается непустой список")
    for i, level in enumerate(levels):
        _number(f"{where}[{i}]", level, 1, integer=True)
    if any(a <= b for a, b in pairwise(levels)):
        _fail(where, "уровни должны строго убывать")


def _text(where: str, value) -> None:
    if not isinstance(value, str) or not value.strip():
        _fail(where, "нужен непустой текст (источник или «допущение»)")


def _direction(where: str, d) -> None:
    _keys(where, d, set(), DIRECTION_KEYS)
    for key in ("rate_kbit", "delay_ms", "jitter_ms"):
        if key in d:
            _number(f"{where}.{key}", d[key], 0)
    if d.get("jitter_ms"):
        if not d.get("rate_kbit"):
            _fail(where, "jitter_ms без rate_kbit: netem переупорядочит пакеты")
        if d["jitter_ms"] > d.get("delay_ms", 0):
            _fail(where, "jitter_ms больше delay_ms: отрицательные задержки netem обрезает до нуля")
    if "loss_pct" in d and "gemodel" in d:
        _fail(where, "loss_pct и gemodel взаимоисключающие")
    if "loss_pct" in d:
        _number(f"{where}.loss_pct", d["loss_pct"], 0, 100)
    if "gemodel" in d:
        _keys(f"{where}.gemodel", d["gemodel"], set(GEMODEL_KEYS))
        for key in GEMODEL_KEYS:
            _number(f"{where}.gemodel.{key}", d["gemodel"][key], 0, 100)
        if not d["gemodel"]["p_pct"] or not d["gemodel"]["r_pct"]:
            _fail(f"{where}.gemodel", "p_pct и r_pct должны быть больше нуля")


def _static(where: str, name, static: set[str]) -> None:
    if not isinstance(name, str) or name not in static:
        _fail(where, f"нет статического профиля {name!r}")


def _schedule(where: str, schedule, static: set[str], min_step_s: float) -> None:
    """Шаги по кругу (repeat) или один раз, после чего действует профиль after (как в board.sh)."""
    _keys(where, schedule, {"repeat", "steps"}, {"after"})
    if not isinstance(schedule["repeat"], bool):
        _fail(f"{where}.repeat", "ожидается true/false")
    if schedule["repeat"] == ("after" in schedule):
        _fail(where, "after (профиль после последнего шага) — только и обязательно при repeat: false")
    if "after" in schedule:
        _static(f"{where}.after", schedule["after"], static)
    if not isinstance(schedule["steps"], list) or not schedule["steps"]:
        _fail(f"{where}.steps", "ожидается непустой список")
    for i, step in enumerate(schedule["steps"]):
        _keys(f"{where}.steps[{i}]", step, {"profile", "for_s"})
        _static(f"{where}.steps[{i}].profile", step["profile"], static)
        _number(f"{where}.steps[{i}].for_s", step["for_s"], min_step_s)


def validate(doc: dict) -> None:
    _keys("profiles", doc, {"format", "memory", "cpu", "tasks_max", "disk", "net", "boards"})
    if doc["format"] != "board-profiles/1":
        _fail("format", f"неизвестный формат {doc['format']!r}")

    memory, cpu = doc["memory"], doc["cpu"]
    _keys("memory", memory, {"levels_mib", "gomemlimit_pct"})
    _levels("memory.levels_mib", memory["levels_mib"])
    _number("memory.gomemlimit_pct", memory["gomemlimit_pct"], 1, 100, integer=True)
    _keys("cpu", cpu, {"quota_pct", "cores", "period_ms"})
    _levels("cpu.quota_pct", cpu["quota_pct"])
    _levels("cpu.cores", cpu["cores"])
    _number("cpu.period_ms", cpu["period_ms"], 1, 1000, integer=True)
    if min(cpu["quota_pct"]) * cpu["period_ms"] / 100 < MIN_CFS_QUOTA_MS:
        _fail("cpu.period_ms", "квота наименьшего уровня за период меньше 1 мс — ядро её не примет")
    _number("tasks_max", doc["tasks_max"], 16, integer=True)

    _object("disk", doc["disk"])
    for name, disk in doc["disk"].items():
        where = f"disk.{name}"
        if not NAME.match(name):
            _fail(where, "имя: строчные латинские буквы, цифры, дефис")
        _keys(where, disk, {"source"}, set(DISK_LIMITS))
        _text(f"{where}.source", disk["source"])
        for key in DISK_LIMITS:
            if key in disk:
                _number(f"{where}.{key}", disk[key], 1, integer=True)

    net = doc["net"]
    _keys("net", net, {"queue", "schedule_min_step_s", "profiles"})
    queue = net["queue"]
    _keys("net.queue", queue, {"ms", "min_packets", "max_packets", "source"})
    _number("net.queue.ms", queue["ms"], 1)
    _number("net.queue.min_packets", queue["min_packets"], 2, integer=True)
    _number("net.queue.max_packets", queue["max_packets"], queue["min_packets"], integer=True)
    _text("net.queue.source", queue["source"])
    _number("net.schedule_min_step_s", net["schedule_min_step_s"], STEP_FLOOR_S)
    _object("net.profiles", net["profiles"])
    static = {name for name, p in net["profiles"].items() if isinstance(p, dict) and "schedule" not in p}
    for name, profile in net["profiles"].items():
        where = f"net.profiles.{name}"
        if not NAME.match(name):
            _fail(where, "имя: строчные латинские буквы, цифры, дефис")
        if isinstance(profile, dict) and "schedule" in profile:
            _keys(where, profile, {"source", "schedule"})
            _schedule(f"{where}.schedule", profile["schedule"], static, net["schedule_min_step_s"])
        else:
            _keys(where, profile, {"source", "up", "down"})
            _direction(f"{where}.up", profile["up"])
            _direction(f"{where}.down", profile["down"])
        _text(f"{where}.source", profile["source"])

    _object("boards", doc["boards"])
    for name, board in doc["boards"].items():
        where = f"boards.{name}"
        if not NAME.match(name):
            _fail(where, "имя: строчные латинские буквы, цифры, дефис")
        _keys(where, board, BOARD_KEYS)
        _text(f"{where}.basis", board["basis"])
        for key, kind, allowed in (("memory", int, memory["levels_mib"]), ("cpu", int, cpu["quota_pct"]),
                                   ("cores", int, cpu["cores"]), ("disk", str, doc["disk"]),
                                   ("net", str, net["profiles"])):
            value = board[key]
            if isinstance(value, bool) or not isinstance(value, kind) or value not in allowed:
                _fail(f"{where}.{key}", f"{value!r} нет среди уровней оси")


def is_schedule(profile: dict) -> bool:
    return "schedule" in profile


def direction_loss(d: dict) -> float:
    """Средняя доля потерь направления; у gemodel — по стационарному распределению цепи Гилберта–Эллиотта."""
    if "gemodel" in d:
        g = {k: v / 100 for k, v in d["gemodel"].items()}
        bad = g["p_pct"] / (g["p_pct"] + g["r_pct"])
        return (1 - bad) * g["good_loss_pct"] + bad * g["bad_loss_pct"]
    return d.get("loss_pct", 0) / 100


def round_trip_loss(profile: dict) -> float:
    """Доля потерянных эхо-пакетов: пакет должен пройти вверх и ответ — вниз."""
    return 1 - (1 - direction_loss(profile["up"])) * (1 - direction_loss(profile["down"]))


def added_rtt_ms(profile: dict) -> float:
    """Задержка, которую netem добавляет к RTT (среднее равномерного разброса равно нулю)."""
    return profile["up"].get("delay_ms", 0) + profile["down"].get("delay_ms", 0)
