"""Сторож (PLAN §5.2; R3 §7.1) без хода LLM: драйверы и rosbridge — каждую секунду, агентский контур — раз в период.

Часто (NEUROBOT_WATCHDOG_FAST_S, по умолчанию 1 с) — то, что «живо, но не работает», и чего не видят ни systemd, ни
обёртка launch (deploy/launchfiles/supervised.launch.py гасит юнит, только когда процесс умер):
  1. Свежесть тем драйверов: NEUROBOT_DRIVER_<имя>_FRESH — «/тема:возраст_с» через пробел; пусто — тема готовности
     NEUROBOT_DRIVER_<имя>_TOPIC с NEUROBOT_WATCHDOG_STALE_S; none — драйвер не смотреть. Подписка raw в одном узле
     rclpy (кадры камеры не разбираются). Две пробы подряд с устаревшей темой — restart драйвера (зависший узел:
     SIGSTOP, дедлок, оглохший поток USB). Если молчат темы всех драйверов сразу (дольше наименьшего из пределов),
     никого не трогаем: так выглядит беда DDS или самого сторожа, а не отказ одного драйвера.
  2. rosbridge: постоянное соединение WebSocket и ping, pong за PING_S; две неудачи подряд — restart rosbridge.
     Соединение одно, а не рукопожатие на каждую пробу: rosbridge пишет в журнал каждое подключение клиента.
Раз в NEUROBOT_WATCHDOG_PERIOD_S (по умолчанию 10 с):
  3. MCP: initialize + tools/list с таймаутом 3 с, две попытки подряд; обе неудачны — restart MCP-сервера.
     По умолчанию инструмент не вызывается: robot_get_status лаборатории дёргает /rosapi/topics, а вызовы
     сервисов rosbridge 1.3.1 в цикле текут (M11, #774). Для robot-mcp пакета 2 — NEUROBOT_WATCHDOG_PROBE_TOOL=status.
  4. PicoClaw, журнал текущего запуска (picolog):
     - вызов MCP-инструмента без завершения > 60 с — restart MCP-сервера: зависший вызов сразу получает
       ошибку, следующие проходят (опыт R3 «http hang + сторож»);
     - любой вызов без завершения > 90 с, «зомби» (PC5), обрыв MCP без восстановления (PC1/PC2) или оглохший
       канал VK (N10) — restart PicoClaw, но только когда MCP-сервер отвечает и строке больше 20 с (ход успевает
       ответить). Для этого PicoClaw должен писать журнал на уровне info (N15; проверяет picoclaw-config check).
Юнит, вошедший в active недавно, не трогаем: рестарт уже был (агентский контур — GRACE_S, драйвер и rosbridge —
FAST_GRACE_S: их active и так значит «первое сообщение пришло» / «порт ответил»). Рестарты в цикле — без ожидания
(--no-block), чтобы сторож не стоял, пока юнит поднимается (--once ждёт). Сторож — долгоживущий сервис, а не таймер: на
systemd 245 таймер раз в 10 с даёт две строки systemd в журнале на каждый запуск (≈17 тыс. в сутки) и стоит запуска
Python каждые 10 с.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import picolog
from .config import Config, EnvError
from .probes import ProbeError, WsPing, probe_mcp
from .report import emit
from .sd import Systemd, SystemdError

PROBE_S = 3.0
PING_S = 1.0
GRACE_S = 15.0
FAST_GRACE_S = 5.0
FAILS = 2
HANG_MCP_S = 60.0
HANG_AGENT_S = 90.0
FATAL_AGE_S = 20.0
MCP_UNIT, AGENT_UNIT = "neurobot-mcp.service", "neurobot-picoclaw.service"
ROSBRIDGE_UNIT = "neurobot-rosbridge.service"


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


def driver_watch(cfg: Config) -> Dict[str, List[Tuple[str, float]]]:
    """{юнит драйвера: [(тема, предельный возраст, с)]} по NEUROBOT_DRIVER_<имя>_FRESH (README deploy, env-файл)."""
    out = {}
    stale = cfg.num("NEUROBOT_WATCHDOG_STALE_S")
    for name in cfg.drivers():
        spec = cfg.driver(name, "FRESH").strip()
        if spec == "none":
            continue
        if not spec:
            topic = cfg.driver(name, "TOPIC")
            spec = "{}:{}".format(topic, stale) if topic else ""
        topics = []
        for token in spec.split():
            topic, sep, age = token.rpartition(":")
            try:
                limit = float(age)
            except ValueError:
                limit = 0.0
            if not sep or not topic.startswith("/") or not limit > 0:
                raise EnvError("NEUROBOT_DRIVER_{}_FRESH: ожидалось «/тема:возраст_с» через пробел или none, получено "
                               "{!r}".format(name, token))
            topics.append((topic, limit))
        if topics:
            out["neurobot-driver@{}.service".format(name)] = topics
    return out


def sensor_actions(watch: Dict[str, List[Tuple[str, float]]], ages: Dict[str, float],
                   props: Dict[str, Dict[str, str]], now_mono: float, fails: Dict[str, int]) -> List[Action]:
    """Рестарты драйверов по свежести их тем. ages — секунды без сообщений по темам (TopicWatch.ages); fails —
    неудачи подряд по юнитам (меняется на месте). Молчат все темы всех драйверов дольше наименьшего предела — общая
    беда (DDS, сам сторож, пауза ВМ; у двойника все датчики идут из Gazebo, и рестарт базы глушит и лидар), никого не
    трогаем: иначе разные пределы (/odom 3 с, /scan 2 с) дали бы лишний рестарт драйвера с меньшим пределом."""
    stale = {u: [t for t, limit in topics if ages.get(t, float("inf")) > limit] for u, topics in watch.items()}
    floor = min((limit for topics in watch.values() for _, limit in topics), default=0.0)
    if len(watch) > 1 and all(ages.get(t, float("inf")) > floor for topics in watch.values() for t, _ in topics):
        fails.clear()
        return []
    out = []
    for unit, topics in sorted(stale.items()):
        p = props.get(unit, {})
        if not topics or not _running(p) or _since_active(p, now_mono) <= FAST_GRACE_S:
            fails.pop(unit, None)
            continue
        fails[unit] = fails.get(unit, 0) + 1
        if fails[unit] >= FAILS:
            fails.pop(unit)
            out.append(Action(unit, "sensor_stale", "темы молчат {} проб подряд: {}".format(FAILS, ", ".join(
                "{} ({:.1f} с)".format(t, ages.get(t, float("inf"))) for t in topics))))
    return out


def rosbridge_action(ping: Callable[[], None], props: Dict[str, str], now_mono: float,
                     fails: Dict[str, int]) -> List[Action]:
    """Рестарт rosbridge после FAILS неудачных ping подряд (нет соединения, нет pong)."""
    if not _running(props) or _since_active(props, now_mono) <= FAST_GRACE_S:
        fails.pop(ROSBRIDGE_UNIT, None)
        return []
    try:
        ping()
    except ProbeError as e:
        fails[ROSBRIDGE_UNIT] = fails.get(ROSBRIDGE_UNIT, 0) + 1
        if fails[ROSBRIDGE_UNIT] >= FAILS:
            fails.pop(ROSBRIDGE_UNIT)
            return [Action(ROSBRIDGE_UNIT, "rosbridge_down", "WebSocket не ответил {} раза подряд: {}".format(
                FAILS, e))]
        return []
    fails.pop(ROSBRIDGE_UNIT, None)
    return []


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


def act(sd: Systemd, actions: Sequence[Action], block: bool = True) -> None:
    for a in actions:
        component = a.unit[len("neurobot-"):-len(".service")]
        emit("watchdog", component, a.code, "restart {}: {}".format(a.unit, a.text), level="warning")
        try:
            sd.ctl("restart", a.unit) if block else sd.ctl("restart", "--no-block", a.unit)
        except SystemdError as e:
            emit("watchdog", component, a.code, "restart {} не удался: {}".format(a.unit, e))


def run_once(cfg: Config, sd: Systemd, probe: Callable = probe_mcp, block: bool = True) -> List[Action]:
    actions = decide(cfg, sd, probe)
    act(sd, actions, block)
    return actions


class Fast:
    """Частые пробы (драйверы, rosbridge) со счётом неудач подряд. topics — rosgraph.TopicWatch или подстановка
    теста (ages(), spin(с)); None — свежесть не смотрим (ROS недоступен)."""

    def __init__(self, cfg: Config, sd: Systemd, topics=None, ping: Optional[WsPing] = None):
        self.cfg, self.sd, self.topics, self.ping = cfg, sd, topics, ping
        self.watch = driver_watch(cfg) if topics is not None else {}
        self.fails: Dict[str, int] = {}

    def step(self, now_mono: Optional[float] = None) -> List[Action]:
        now_mono = time.monotonic() if now_mono is None else now_mono
        units = list(self.watch) + ([ROSBRIDGE_UNIT] if self.ping else [])
        props = self.sd.show(units)
        actions = sensor_actions(self.watch, self.topics.ages(), props, now_mono, self.fails) if self.watch else []
        if self.ping:
            actions += rosbridge_action(lambda: self.ping.ping(PING_S), props.get(ROSBRIDGE_UNIT, {}), now_mono,
                                        self.fails)
        act(self.sd, actions, block=False)
        return actions

    def wait(self, seconds: float) -> None:
        if self.topics is not None:
            self.topics.spin(seconds)
        elif seconds > 0:
            time.sleep(seconds)


def loop(cfg: Config, sd: Systemd, period: float, fast: float, topics=None) -> None:
    ping = WsPing("127.0.0.1", cfg.port("NEUROBOT_ROSBRIDGE_PORT")) if "rosbridge" in cfg.components() else None
    f = Fast(cfg, sd, topics, ping)
    next_slow = 0.0
    while True:
        t0 = time.monotonic()
        try:
            f.step(t0)
            if t0 >= next_slow:
                next_slow = t0 + period
                run_once(cfg, sd, block=False)  # частые пробы не ждут, пока поднимается MCP или PicoClaw
        except SystemdError as e:
            emit("watchdog", "watchdog", "systemd_error", str(e))
        f.wait(fast - (time.monotonic() - t0))
