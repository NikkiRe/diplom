"""neurobot estop engage | release — аварийная защёлка safety_gate (PLAN §13.3, контракт /neurobot/estop).

Команда — std_msgs/String с JSON {"cmd": "engage" | "release", "by": "human", "reason", "id", "t"} в
NEUROBOT_ESTOP_TOPIC; итог — по NEUROBOT_SAFETY_STATE_TOPIC ({state: ok|slow|blocked|estop, reasons[], …}).
- engage — поставить может кто угодно и без вопросов: ждём, что safety_gate перешёл в estop.
- release — только человек: stdin должен быть терминалом, показываем состояние и причины, ждём слово «release».
  Агенту этот путь закрыт тем, что у него нет ни терминала, ни shell (exec выключен — precheck picoclaw), ни темы
  защёлки в MCP (precheck mcp). Узел ROS, опубликовавший release сам, ROS не отличит от человека — проверка «by» и
  своя политика снятия — на стороне safety_gate.
Коды: 0 — состояние подтверждено (или защёлки и так нет), 1 — не подтверждено, отказ или отмена.
"""
from __future__ import annotations

from typing import Callable, NamedTuple, Optional, Tuple

from .config import Config
from .report import emit

WORD = "release"


class Ros(NamedTuple):
    """Доступ к графу (rosgraph.safety_state / send_estop); тесты подставляют свой."""
    state: Callable[[Config], Optional[dict]]
    send: Callable[[Config, dict, Callable[[dict], bool]], Tuple[int, Optional[dict]]]
    message: Callable[[str, str], dict]


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


def release(cfg: Config, ros: Ros, reason: str, is_tty: bool, ask: Callable[[str], str]) -> int:
    if not is_tty:
        emit("estop", "safety", "estop_denied", "снять защёлку может только человек в терминале (stdin — не терминал)")
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
    subs, after = ros.send(cfg, ros.message("release", reason), lambda s: s.get("state") != "estop")
    if not subs:
        emit("estop", "safety", "estop_failed", _no_gate(cfg.get("NEUROBOT_ESTOP_TOPIC")))
        return 1
    if after and after.get("state") != "estop":
        emit("estop", "safety", "estop_released", "защёлка снята: " + describe(after), level="info")
        return 0
    emit("estop", "safety", "estop_failed", "safety_gate не снял защёлку: {}".format(
        describe(after) if after else "нет " + cfg.get("NEUROBOT_SAFETY_STATE_TOPIC")))
    return 1
