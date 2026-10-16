"""Сторож агентского контура: MCP-сервер и PicoClaw (PLAN §5.2; R3 §7.1). Без хода LLM.

Раз в период (по умолчанию 10 с):
  1. MCP: initialize + tools/list с таймаутом 3 с, две попытки подряд; обе неудачны — restart MCP-сервера.
     По умолчанию инструмент не вызывается: robot_get_status лаборатории дёргает /rosapi/topics, а вызовы
     сервисов rosbridge 1.3.1 в цикле текут (M11, #774). Для robot-mcp пакета 2 — NEUROBOT_WATCHDOG_PROBE_TOOL=status.
  2. PicoClaw, журнал текущего запуска (picolog):
     - вызов MCP-инструмента без завершения > 60 с — restart MCP-сервера: зависший вызов сразу получает
       ошибку, следующие проходят (опыт R3 «http hang + сторож»);
     - любой вызов без завершения > 90 с, «зомби» (PC5), обрыв MCP без восстановления (PC1/PC2) или оглохший
       канал VK (N10) — restart PicoClaw, но только когда MCP-сервер отвечает и строке больше 20 с (ход успевает
       ответить). Для этого PicoClaw должен писать журнал на уровне info (N15; проверяет picoclaw-config check).
Юнит, вошедший в active меньше GRACE_S назад, не трогаем: рестарт уже был. Сторож — долгоживущий сервис, а не
таймер: на systemd 245 таймер раз в 10 с даёт две строки systemd в журнале на каждый запуск (≈17 тыс. в сутки)
и стоит запуска Python каждые 10 с.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, NamedTuple, Optional

from . import picolog
from .config import Config
from .probes import ProbeError, probe_mcp
from .report import emit
from .sd import Systemd, SystemdError

PROBE_S = 3.0
GRACE_S = 15.0
HANG_MCP_S = 60.0
HANG_AGENT_S = 90.0
FATAL_AGE_S = 20.0
MCP_UNIT, AGENT_UNIT = "neurobot-mcp.service", "neurobot-picoclaw.service"


class Action(NamedTuple):
    unit: str
    code: str
    text: str


def _running(props: Dict[str, str]) -> bool:
    return props.get("ActiveState") == "active" and props.get("SubState") == "running"


def _since_active(props: Dict[str, str], now_mono: float) -> float:
    try:
        return now_mono - int(props.get("ActiveEnterTimestampMonotonic") or 0) / 1e6
    except ValueError:
        return 0.0


def decide(cfg: Config, sd: Systemd, probe: Callable = probe_mcp, now_mono: Optional[float] = None,
           now_real: Optional[float] = None) -> List[Action]:
    now_mono = time.monotonic() if now_mono is None else now_mono
    now_real = time.time() if now_real is None else now_real
    comps = cfg.components()
    units = [u for c, u in (("mcp", MCP_UNIT), ("picoclaw", AGENT_UNIT)) if c in comps]
    props = sd.show(units)
    mcp, agent = props.get(MCP_UNIT, {}), props.get(AGENT_UNIT, {})
    actions = []
    mcp_ok = "mcp" not in comps
    if "mcp" in comps and _running(mcp) and _since_active(mcp, now_mono) > GRACE_S:
        errors = []
        for _ in range(2):
            try:
                probe(cfg.get("NEUROBOT_MCP_URL"), PROBE_S, cfg.get("NEUROBOT_WATCHDOG_PROBE_TOOL"))
                break
            except ProbeError as e:
                errors.append(str(e))
        mcp_ok = len(errors) < 2
        if not mcp_ok:
            actions.append(Action(MCP_UNIT, "mcp_down", "MCP не ответил дважды по {:g} с: {}".format(
                PROBE_S, errors[-1])))
    if "picoclaw" not in comps or not _running(agent) or _since_active(agent, now_mono) <= GRACE_S:
        return actions
    log = picolog.analyze(sd.journal(invocation=agent.get("InvocationID", ""), lines=3000))
    hung_mcp = [(t, now_real - ts) for t, ts in log.pending if t.startswith("mcp_") and now_real - ts > HANG_MCP_S]
    if hung_mcp and mcp_ok and "mcp" in comps and _since_active(mcp, now_mono) > HANG_MCP_S:
        actions.append(Action(MCP_UNIT, "agent_hung", "вызов {} без завершения {:.0f} с".format(*hung_mcp[0])))
    age = log.pending_age(now_real)
    if mcp_ok and age > HANG_AGENT_S:
        actions.append(Action(AGENT_UNIT, "agent_hung", "вызов {} без завершения {:.0f} с".format(
            log.pending[0][0], age)))
    elif mcp_ok and log.fatal and now_real - log.fatal.ts > FATAL_AGE_S:
        actions.append(Action(AGENT_UNIT, log.fatal.code, "в журнале: " + log.fatal.line[-160:]))
    return actions


def run_once(cfg: Config, sd: Systemd, probe: Callable = probe_mcp) -> List[Action]:
    actions = decide(cfg, sd, probe)
    for a in actions:
        component = a.unit[len("neurobot-"):-len(".service")]
        emit("watchdog", component, a.code, "restart {}: {}".format(a.unit, a.text), level="warning")
        try:
            sd.ctl("restart", a.unit)
        except SystemdError as e:
            emit("watchdog", component, a.code, "restart {} не удался: {}".format(a.unit, e))
    return actions


def loop(cfg: Config, sd: Systemd, period: float, sleep: Callable[[float], None] = time.sleep) -> None:
    while True:
        try:
            run_once(cfg, sd)
        except SystemdError as e:
            emit("watchdog", "watchdog", "systemd_error", str(e))
        sleep(period)
