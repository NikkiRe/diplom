"""neurobot estop engage | release — аварийная защёлка safety_gate (PLAN §13.3, контракт /neurobot/estop).

engage — std_msgs/String с JSON {"cmd": "engage", "by": "human", "reason", "id", "t"} в NEUROBOT_ESTOP_TOPIC; итог —
по NEUROBOT_SAFETY_STATE_TOPIC ({state: ok|slow|blocked|estop, reasons[], estop: {engaged, since, reasons}, …}).
Поставить может кто угодно и без вопросов.
release — только человек и только root: stdin — терминал, показываем состояние и причины, ждём слово «release», затем
пишем запрос снятия release.json в каталог NEUROBOT_ESTOP_RELEASE_DIR (/run/neurobot-estop: владелец root, на запись
только ему) — его читает safety_gate (neurobot/safety/latch.read_release). В запросе — защёлка, которую видел
человек (её since и причины): если за это время встала новая причина, safety_gate не снимет. По теме защёлку не
снять: поле by самозаявлено, а узел ROS или клиент rosbridge подписался бы «human». Агенту путь закрыт: он не root,
у него нет ни терминала, ни shell (exec выключен — отказ precheck picoclaw).
Коды: 0 — состояние подтверждено (или защёлки и так нет), 1 — не подтверждено, отказ или отмена.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import uuid
from typing import Callable, NamedTuple, Optional, Tuple

from .config import Config
from .report import emit

WORD = "release"
RELEASE_FILE = "release.json"
RELEASE_WAIT_S = 4.0  # safety_gate смотрит каталог дважды в секунду, плюс доставка состояния


class Ros(NamedTuple):
    """Доступ к графу (rosgraph.safety_state / send_estop / wait_safety_state); тесты подставляют свой."""
    state: Callable[[Config], Optional[dict]]
    send: Callable[[Config, dict, Callable[[dict], bool]], Tuple[int, Optional[dict]]]
    message: Callable[[str, str], dict]
    wait: Callable[[Config, Callable[[dict], bool], float], Optional[dict]]


class ReleaseError(Exception):
    pass


def describe(state: dict) -> str:
    reasons = state.get("reasons") or []
    parts = ["состояние {}".format(state.get("state"))]
    if reasons:
        parts.append("причины: " + ", ".join(str(r) for r in reasons))
    if state.get("min_clearance_m") is not None:
        parts.append("зазор {} м".format(state.get("min_clearance_m")))
    return "; ".join(parts)


def _no_gate(topic: str) -> str:
    return ("никто не слушает {}: safety_gate не запущен (sudo neurobot doctor); остановить робот — кнопкой или "
            "sudo systemctl stop neurobot-safety: при остановке он шлёт ноль в /cmd_vel".format(topic))


def write_release(directory: str, state: dict, reason: str, owner: int = 0) -> str:
    """release.json для safety_gate: каталог (создаётся) и файл — владельца owner (root), на запись только ему;
    атомарно (временный файл, fsync, rename). Путь файла; ReleaseError — каталог чужой или открыт на запись."""
    try:
        os.makedirs(directory, mode=0o755, exist_ok=True)
        st = os.lstat(directory)
    except OSError as e:
        raise ReleaseError("каталог {}: {}".format(directory, e.strerror or e))
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != owner or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ReleaseError("{} — не каталог root с правами 755: safety_gate такому запросу не поверит".format(
            directory))
    latch = state.get("estop") or {}
    doc = {"id": uuid.uuid4().hex, "t": round(time.time(), 3), "since": latch.get("since"),
           "reasons": list(latch.get("reasons") or []), "reason": reason}
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".release-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
            f.flush()
            os.fchmod(f.fileno(), 0o644)
            os.fsync(f.fileno())
        path = os.path.join(directory, RELEASE_FILE)
        os.replace(tmp, path)
    except OSError as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise ReleaseError("запрос снятия не записан: {}".format(e.strerror or e))
    return path


def engage(cfg: Config, ros: Ros, reason: str) -> int:
    subs, state = ros.send(cfg, ros.message("engage", reason), lambda s: s.get("state") == "estop")
    if not subs:
        emit("estop", "safety", "estop_failed", _no_gate(cfg.get("NEUROBOT_ESTOP_TOPIC")))
        return 1
    if state and state.get("state") == "estop":
        emit("estop", "safety", "estop_engaged", "защёлка стоит: " + describe(state), level="warning")
        return 0
    emit("estop", "safety", "estop_failed", "safety_gate не подтвердил защёлку: {}".format(
        describe(state) if state else "нет " + cfg.get("NEUROBOT_SAFETY_STATE_TOPIC")))
    return 1


def release(cfg: Config, ros: Ros, reason: str, is_tty: bool, ask: Callable[[str], str], euid: int,
            write: Callable[[str, dict, str], str] = write_release) -> int:
    if not is_tty:
        emit("estop", "safety", "estop_denied", "снять защёлку может только человек в терминале (stdin — не терминал)")
        return 1
    if euid != 0:
        emit("estop", "safety", "estop_denied", "снять защёлку может только root: sudo neurobot estop release (запрос "
             "снятия — файл в {}, писать туда может только root)".format(cfg.get("NEUROBOT_ESTOP_RELEASE_DIR")))
        return 1
    state = ros.state(cfg)
    if state is None:
        emit("estop", "safety", "estop_failed", "нет {}: safety_gate не запущен — снимать нечего".format(
            cfg.get("NEUROBOT_SAFETY_STATE_TOPIC")))
        return 1
    if state.get("state") != "estop":
        emit("estop", "safety", "estop_absent", "защёлки нет: " + describe(state), level="info")
        return 0
    answer = ask("Аварийная защёлка: {}.\nПосле снятия робот сможет поехать. Вокруг свободно, причина устранена? "
                 "Введите {}: ".format(describe(state), WORD))
    if answer.strip() != WORD:
        emit("estop", "safety", "estop_cancelled", "отменено: защёлка остаётся", level="warning")
        return 1
    try:
        path = write(cfg.get("NEUROBOT_ESTOP_RELEASE_DIR"), state, reason)
    except ReleaseError as e:
        emit("estop", "safety", "estop_failed", str(e))
        return 1
    try:
        after = ros.wait(cfg, lambda s: s.get("state") != "estop", RELEASE_WAIT_S)
    finally:
        if os.path.exists(path):
            os.remove(path)
    if after and after.get("state") != "estop":
        emit("estop", "safety", "estop_released", "защёлка снята: " + describe(after), level="info")
        return 0
    emit("estop", "safety", "estop_failed", "safety_gate не снял защёлку за {:g} с: {} (причина отказа — в журнале "
         "neurobot-safety)".format(RELEASE_WAIT_S, describe(after) if after else "нет " +
                                   cfg.get("NEUROBOT_SAFETY_STATE_TOPIC")))
    return 1
