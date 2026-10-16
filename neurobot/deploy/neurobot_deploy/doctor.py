"""neurobot doctor: всё ли поднялось, а если нет — какой компонент и почему (PLAN §5.4, §13.3).

По каждому компоненту в порядке запуска: состояние юнита и READY (Type=notify / ExecStartPost-проба),
прогресс и соединения:
- драйверы и датчики: возраст и издатели /scan (два издателя — драйвер запущен дважды; frame_id; закрытый передний
  сектор лидара, RS3 — предупреждение), темы NEUROBOT_SENSORS (издатель, тип, частота);
- safety_gate: /neurobot/safety_state (жив ли, аварийная защёлка, блок и его причины) и цепочка движения: в
  /cmd_vel пишет только он, входы twist_mux главнее /cmd_vel никто не публикует (N13), в вход базы пишет только
  twist_mux, twist_mux слушает /cmd_vel; геймпад — предупреждение;
- исполнитель: /neurobot/motion_state приходит, в /neurobot/cmd_vel_req пишет только он;
- распознавание: /neurobot/perception_health;
- rosbridge — WebSocket, воркеры лаборатории — HTTP GET, MCP — initialize + tools/list + один вызов состояния,
  PicoClaw — журнал запуска.
Причина отказа — последняя строка «neurobot-…: компонент: код: текст» с приоритетом ≤ 3 в журнале запуска
юнита, иначе — Result юнита и последняя похожая на ошибку строка процесса; у перезапускавшихся юнитов — ещё и
последний отказ из сообщений systemd. Корневая причина — первый по порядку компонент с ошибкой: зависимые от
него обычно падают следом. Компоненты ROS запущены, а граф не снимается (rclpy, DDS) — ошибка ros_unavailable, а не
«ОК»: датчики и безопасность не проверены.

Коды выхода: 0 — всё ОК; 1 — найдена проблема; 2 — сама проверка невозможна (env, systemctl);
3 — система ещё поднимается.
"""
from __future__ import annotations

import json
import math
import re
import time
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from . import checks, picolog, report
from .config import Config
from .estop import describe
from .probes import ProbeError, http_get, probe_mcp, ws_handshake
from .sd import Entry, Systemd, timespan_s

OK, WARN, FAIL, STARTING = "ok", "warn", "fail", "starting"
EXIT = {OK: 0, FAIL: 1, STARTING: 3}
SCAN_MAX_AGE_S = 0.5      # старше — вперёд нельзя (страж v1 и safety_gate: 0,3–0,5 с)
STATE_MIN_HZ = 2.0        # /neurobot/safety_state по контракту 5 Гц
RECENT_FAIL_S = 300.0     # отказ свежее — юнит в цикле рестартов, называем его причину, а не «запускается»
OBSERVE_S = 2.5
FRONT_DEG = 61.0          # сектор препятствий демо (лучи 166–515 URG-04LX); шире сектора остановки
BLIND_MIN_DEG = 10.0      # уже — отдельные выпавшие лучи, а не закрытый сектор
BLIND_FRAC = 0.9          # луч негоден в ≥ 90 % сканов за время наблюдения
ROS_LIVE = ("safety", "executor", "perception")
FAILED_WITH = re.compile(r"Failed with result '([\w-]+)'")
# результат отказа из журнала systemd -> (код, текст), когда своей строки причины у запуска нет
FAILURE = {"timeout": ("start_timeout", "не стал готов за TimeoutStartSec"),
           "watchdog": ("watchdog", "нет WATCHDOG дольше WatchdogSec — процесс завис")}
ERRORISH = re.compile(r"error|exception|traceback|failed|fatal|killed|denied|not found|нет |ошибк", re.I)


class Finding(NamedTuple):
    component: str
    unit: str
    level: str
    code: str
    text: str

    def as_dict(self) -> dict:
        d = self._asdict()
        if self.code:
            d["hint"] = report.hint(self.code)
        return d


class Report(NamedTuple):
    findings: List[Finding]
    root: Optional[Finding]

    @property
    def level(self) -> str:
        return self.root.level if self.root else OK

    @property
    def exit_code(self) -> int:
        return EXIT[self.level]

    def as_dict(self) -> dict:
        return {"ok": self.root is None, "exit": self.exit_code,
                "root": self.root.as_dict() if self.root else None,
                "findings": [f.as_dict() for f in self.findings]}


def _f(props: Dict[str, str], key: str) -> float:
    try:
        return float(props.get(key) or 0)
    except ValueError:
        return 0.0


def _reason(entries: List[Entry]) -> Optional[report.Reason]:
    for e in reversed(entries):
        r = report.parse(e.message) if e.priority <= 3 else None
        if r:
            return r
    return None


def _last_error(entries: List[Entry]) -> str:
    """Последняя строка процесса, похожая на ошибку (иначе — просто последняя строка процесса); наши строки
    «neurobot-…: компонент: код: текст» и эхо команды neurobot-run не в счёт: ошибочные из первых уже разобрал
    _reason, прочие — не ошибки."""
    lines = [e.message.strip() for e in entries if e.process and e.message.strip() and not report.parse(e.message)
             and not e.message.startswith("neurobot-run: ")]
    errors = [ln for ln in lines if ERRORISH.search(ln)]
    last = (errors or lines or [""])[-1]
    return last if len(last) <= 160 else last[:157] + "…"


def _last_failure(sd: Systemd, unit: str) -> Optional[Tuple[str, Entry]]:
    """Последний отказ юнита по сообщениям systemd («Failed with result 'watchdog'»): Result после рестарта
    сбрасывается, а журнал помнит. (результат, запись — её invocation указывает на отказавший запуск)."""
    for e in reversed(sd.journal(unit=unit, lines=100)):
        m = FAILED_WITH.search(e.message) if not e.process else None
        if m:
            return m.group(1), e
    return None


def _from_result(component: str, props: Dict[str, str]) -> Finding:
    result, status = props.get("Result", ""), props.get("ExecMainStatus", "")
    if result in FAILURE:
        return Finding("", "", FAIL, *FAILURE[result])
    if component == "perception" and result == "exit-code" and status == "6":
        return Finding("", "", FAIL, "camera_busy", "камера занята другим процессом (код 6, --camera rs); юнит сам не "
                       "перезапускается (RestartPreventExitStatus=6)")
    if result in ("signal", "core-dump"):
        return Finding("", "", FAIL, "crashed", "убит сигналом {}".format(status))
    return Finding("", "", FAIL, "crashed", "завершился: {}, код {}".format(result or "?", status or "?"))


def unit_state(component: str, unit: str, props: Dict[str, str], jobs: Dict[str, str], sd: Systemd,
               now_mono: float, now_real: float) -> Finding:
    def mk(level, code, text):
        return Finding(component, unit, level, code, text)

    if props.get("LoadState") != "loaded":
        return mk(FAIL, "not_installed", "юнит {} не установлен ({})".format(unit, props.get("LoadState", "?")))
    active, sub = props.get("ActiveState", ""), props.get("SubState", "")
    restarts = int(_f(props, "NRestarts"))
    tail, failure = "", None
    if restarts:
        failure = _last_failure(sd, unit)
        tail = ", рестартов {}{}".format(restarts, " (последний отказ: {} {:.0f} с назад)".format(
            failure[0], max(0.0, now_real - failure[1].ts)) if failure else "")
    if active == "active":
        wd = timespan_s(props.get("WatchdogUSec", ""))
        ping = _f(props, "WatchdogTimestampMonotonic") / 1e6
        if wd and ping:
            age = max(0.0, now_mono - ping)
            if age > wd:
                return mk(FAIL, "watchdog", "нет WATCHDOG {:.1f} с (WatchdogSec {:g} с){}".format(age, wd, tail))
            return mk(OK, "", "READY, прогресс {:.1f} с назад (WatchdogSec {:g} с){}".format(age, wd, tail))
        return mk(OK, "", "работает{}".format(tail))
    if active in ("activating", "failed") or (active == "inactive" and props.get("Result") not in ("", "success")):
        entries = sd.journal(invocation=props["InvocationID"], lines=200) if props.get("InvocationID") else []
        r = _reason(entries)
        if r:
            return mk(FAIL, r.code, r.text + tail)
        if failure and now_real - failure[1].ts < RECENT_FAIL_S:
            # перезапуск по кругу: причина — в журнале отказавшего запуска (ожидание порта, проба готовности), иначе —
            # результат отказа (не стал готов, завис, упал): «запускается» здесь скрыло бы цикл рестартов
            r = _reason(sd.journal(invocation=failure[1].invocation, lines=200))
            if r:
                return mk(FAIL, r.code, "{}; перезапускается{}".format(r.text, tail))
            if active == "activating":
                code, text = FAILURE.get(failure[0], ("crashed", "упал ({})".format(failure[0])))
                return mk(FAIL, code, "{}; перезапускается{}".format(text, tail))
        if active == "activating" and sub != "auto-restart":
            since = now_mono - _f(props, "InactiveExitTimestampMonotonic") / 1e6
            return mk(STARTING, "starting", "запускается ({}, {:.0f} с){}".format(sub, since, tail))
        f = _from_result(component, props)
        last = _last_error(entries)
        return mk(FAIL, f.code, f.text + tail + ("; в журнале: " + last if last else ""))
    if unit in jobs:
        return mk(STARTING, "starting", "ждёт запуска (задание {}: после предыдущих компонентов)".format(jobs[unit]))
    return mk(FAIL, "not_running", "не запущен ({}/{})".format(active, sub))


def _check_rosbridge(cfg: Config, ws: Callable = ws_handshake) -> Finding:
    port = cfg.port("NEUROBOT_ROSBRIDGE_PORT")
    try:
        ws("127.0.0.1", port, timeout=3.0)
    except ProbeError as e:
        return Finding("rosbridge", "", FAIL, "rosbridge_down", "WebSocket не отвечает: {}".format(e))
    return Finding("rosbridge", "", OK, "", "WebSocket 127.0.0.1:{} отвечает".format(port))


def _check_mcp(cfg: Config, probe: Callable = probe_mcp) -> Finding:
    try:
        res = probe(cfg.get("NEUROBOT_MCP_URL"), 5.0, cfg.get("NEUROBOT_MCP_STATUS_TOOL"))
    except ProbeError as e:
        return Finding("mcp", "", FAIL, "mcp_down", "MCP не отвечает: {}".format(e))
    text = "MCP за {:.2f} с, инструменты: {}".format(res.elapsed_s, ", ".join(res.tools))
    in_ms = sorted(t for t, p in res.params.items() if "duration_ms" in p)
    in_s = sorted(t for t, p in res.params.items() if "duration_s" in p)
    if in_ms and in_s:
        return Finding("mcp", "", FAIL, "mcp_schema", "длительность движения в разных единицах: {} — duration_ms, "
                       "{} — duration_s (у прокси лаборатории так выглядят дубли: мс уходят воркеру как секунды, "
                       "N1, N2)".format(", ".join(in_ms), ", ".join(in_s)))
    if res.status is not None:
        try:
            status = json.loads(res.status)
        except ValueError:
            status = {}
        if status.get("connected") is False:
            return Finding("mcp", "", FAIL, "mcp_no_rosbridge", "{}; состояние: {}".format(
                text, status.get("error", res.status[:120])))
        if status.get("ok") is False:
            return Finding("mcp", "", FAIL, "mcp_worker_down", "{}; состояние: {}".format(
                text, str(status.get("error", res.status))[:160]))
    return Finding("mcp", "", OK, "", text)


def _check_labworker(cfg: Config, component: str, get: Callable = http_get) -> Finding:
    url = cfg.labworker(component.split("@", 1)[1], "URL")
    try:
        code, body = get(url, 3.0)
    except ProbeError as e:
        return Finding(component, "", FAIL, "labworker_down", "не отвечает: {}".format(e))
    if code != 200:
        return Finding(component, "", FAIL, "labworker_down", "{}: HTTP {}: {}".format(url, code, body[:120]))
    return Finding(component, "", OK, "", "{} отвечает 200".format(url))


def blind_sectors(scan: dict) -> List[Tuple[float, float]]:
    """Сплошные участки переднего сектора (±FRONT_DEG), где луч почти всегда негоден: закрыт корпусом или камерой
    (RS3) — либо там пусто дальше дальности лидара. Градусы: 0 — вперёд, плюс — влево; сканы ±120° и 0…360°."""
    frac, a0, da = scan.get("bad_frac"), scan.get("angle_min"), scan.get("angle_increment")
    if not frac or not da:
        return []
    rays = sorted((d, f) for d, f in ((math.degrees(math.atan2(math.sin(a0 + i * da), math.cos(a0 + i * da))), f)
                                      for i, f in enumerate(frac)) if abs(d) <= FRONT_DEG)
    step = abs(math.degrees(da)) * 1.5
    runs, cur = [], []
    for d, f in rays:
        if f >= BLIND_FRAC and cur and d - cur[-1] <= step:
            cur.append(d)
            continue
        runs.append(cur)
        cur = [d] if f >= BLIND_FRAC else []
    runs.append(cur)
    return [(round(r[0], 1), round(r[-1], 1)) for r in runs if r and r[-1] - r[0] >= BLIND_MIN_DEG]


def _check_agent(sd: Systemd, props: Dict[str, str], now_real: float) -> Finding:
    entries = sd.journal(invocation=props.get("InvocationID", ""), lines=3000)
    if not entries:
        return Finding("picoclaw", "", WARN, "", "журнал PicoClaw не читается: запустите doctor через sudo "
                       "или из группы adm/systemd-journal")
    log = picolog.analyze(entries)
    age = log.pending_age(now_real)
    if log.fatal:
        return Finding("picoclaw", "", FAIL, log.fatal.code, "в журнале: {}".format(log.fatal.line[-160:]))
    if age > 60:
        return Finding("picoclaw", "", FAIL, "agent_hung", "вызов {} без завершения {:.0f} с".format(
            log.pending[0][0], age))
    if not log.connected:
        return Finding("picoclaw", "", FAIL, "agent_timeout", "в журнале запуска нет «Connected to MCP server»")
    extra = ", перезагрузок конфига {}".format(log.reloads) if log.reloads else ""
    return Finding("picoclaw", "", OK, "", "MCP подключён ({}){}".format(", ".join(log.connected), extra))


def _graph(obs: Dict[str, dict]) -> Dict[str, Dict[str, List[str]]]:
    """Конечные точки цепочки движения из наблюдения — в форме графа checks."""
    out = {o["topic"]: o for o in (obs.get("cmd_vel"), obs.get("cmd_vel_req"), obs.get("base")) if o}
    for key in ("override", "joy"):
        out.update(obs.get(key) or {})
    return out


def _check_safety(cfg: Config, obs: Dict[str, dict]) -> Finding:
    def mk(level, code, text):
        return Finding("safety", "", level, code, text)

    o = obs["cmd_vel"]
    if o["types"] and cfg.get("NEUROBOT_CMD_VEL_TYPE") not in o["types"]:
        return mk(FAIL, "config_invalid", "{}: тип в графе {}, а NEUROBOT_CMD_VEL_TYPE={}".format(
            o["topic"], ", ".join(o["types"]), cfg.get("NEUROBOT_CMD_VEL_TYPE")))
    problems = [p for p in checks.writer_problems(cfg, _graph(obs)) if p.code != "cmd_vel_joy"]
    fatal = [p for p in problems if p.fatal]
    if fatal:
        return mk(FAIL, fatal[0].code, "; ".join(p.text for p in fatal))
    s = obs["safety"]
    state = s.get("last")
    if not state:
        return mk(FAIL, "safety_silent", "нет {} за {:g} с".format(s["topic"], OBSERVE_S))
    if state.get("state") not in ("ok", "slow", "blocked", "estop"):
        return mk(FAIL, "safety_silent", "{}: непонятное состояние {}".format(s["topic"], json.dumps(state)[:120]))
    if s["rate_hz"] < STATE_MIN_HZ:
        return mk(FAIL, "safety_silent", "{} {:.1f} Гц (ожидается 5, не меньше {:g})".format(
            s["topic"], s["rate_hz"], STATE_MIN_HZ))
    if state["state"] == "estop":
        return mk(FAIL, "estop_engaged", "аварийная защёлка: " + describe(state))
    warn = [(p.code, p.text) for p in problems]
    if state["state"] == "blocked":
        warn.append(("safety_blocked", "движение запрещено: " + describe(state)))
    if warn:
        return mk(WARN, warn[0][0], "; ".join(t for _, t in warn))
    return mk(OK, "", "{}; в {} пишет только он".format(describe(state), o["topic"]))


def _check_executor(cfg: Config, obs: Dict[str, dict]) -> Finding:
    def mk(level, code, text):
        return Finding("executor", "", level, code, text)

    problems = checks.request_problems(cfg, _graph(obs))
    if problems:
        return mk(FAIL, problems[0].code, "; ".join(p.text for p in problems))
    m = obs["motion"]
    if not m["count"]:
        return mk(FAIL, "executor_silent", "нет {} за {:g} с".format(m["topic"], OBSERVE_S))
    return mk(OK, "", "{} {:.1f} Гц; в {} пишет только он".format(m["topic"], m["rate_hz"],
                                                                     cfg.get("NEUROBOT_CMD_VEL_REQ")))


def _check_perception(obs: Dict[str, dict]) -> Finding:
    def mk(level, code, text):
        return Finding("perception", "", level, code, text)

    o = obs["health"]
    h = o.get("last")
    if not h:
        return mk(FAIL, "perception_silent", "нет {} за {:g} с".format(o["topic"], OBSERVE_S))
    text = "камера {}: {} к/с, кадр {} с назад, восстановлений {}".format(
        h.get("state"), h.get("fps"), h.get("frame_age_s"), h.get("recoveries"))
    if h.get("state") == "starting":
        return mk(STARTING, "starting", text)
    if h.get("state") not in ("ok", "paused"):
        return mk(FAIL, "perception_stalled", text)
    return mk(OK, "", text)


def _check_scan(component: str, cfg: Config, obs: Dict[str, dict]) -> Finding:
    def mk(level, code, text):
        return Finding(component, "", level, code, text)

    o = obs["scan"]
    if o["age_s"] is None or o["age_s"] > SCAN_MAX_AGE_S:
        age = "нет сообщений" if o["age_s"] is None else "последнее {:.2f} с назад".format(o["age_s"])
        return mk(FAIL, "scan_stale", "{}: {} — вперёд safety_gate не пустит".format(o["topic"], age))
    if len(o["pub"]) > 1:
        return mk(FAIL, "scan_publishers", "{} публикуют {} издателя ({}): драйвер лидара запущен дважды".format(
            o["topic"], len(o["pub"]), ", ".join(sorted(set(o["pub"])))))
    frame = cfg.get("NEUROBOT_SCAN_FRAME")
    if frame and o.get("frame_id") not in (None, frame):
        return mk(WARN, "scan_frame", "{}: frame_id {!r}, а ожидается {!r} (NEUROBOT_SCAN_FRAME): положение лидара "
                  "в TF не найдётся".format(o["topic"], o.get("frame_id"), frame))
    blind = blind_sectors(o)
    if blind:
        sectors = ", ".join("сектор {:+.0f}…{:+.0f}°".format(a, b) for a, b in blind)
        return mk(WARN, "scan_blind", "{}: лидар не видит {} (негодные значения в ≥ {:.0%} сканов) — закрыт камерой "
                  "или корпусом, либо там пусто дальше дальности лидара (RS3)".format(o["topic"], sectors, BLIND_FRAC))
    return mk(OK, "", "{} {:.1f} Гц".format(o["topic"], o["rate_hz"]))


def _check_sensor(topic: str, type_name: str, min_hz: float, obs: Dict[str, dict]) -> Finding:
    def mk(level, code, text):
        return Finding("sensor:" + topic, "", level, code, text)

    o = obs["sensor:" + topic]
    if not o["pub"]:
        return mk(FAIL, "sensor_missing", "у {} нет издателя".format(topic))
    if o["types"] and type_name not in o["types"]:
        return mk(FAIL, "config_invalid", "{}: тип в графе {}, а в NEUROBOT_SENSORS {}".format(
            topic, ", ".join(o["types"]), type_name))
    if not o["count"]:
        return mk(FAIL, "sensor_stale", "{}: издатели есть ({}), сообщений за {:g} с нет".format(
            topic, ", ".join(sorted(set(o["pub"]))), OBSERVE_S))
    if o["rate_hz"] < min_hz:
        return mk(FAIL, "sensor_stale", "{} {:.1f} Гц, ожидается ≥ {:g}".format(topic, o["rate_hz"], min_hz))
    return mk(OK, "", "{} {:.1f} Гц".format(topic, o["rate_hz"]))


def _merge(unit_f: Finding, live: Finding) -> Finding:
    if live.level == OK:
        return unit_f._replace(text="{}; {}".format(unit_f.text, live.text))
    return Finding(unit_f.component, unit_f.unit, live.level, live.code, live.text)


def diagnose(cfg: Config, sd: Systemd, observe: Optional[Callable[[Config, float], Dict[str, dict]]] = None,
             probe: Callable = probe_mcp, ws: Callable = ws_handshake, get: Callable = http_get,
             now_mono: Optional[float] = None, now_real: Optional[float] = None) -> Report:
    now_mono = time.monotonic() if now_mono is None else now_mono
    now_real = time.time() if now_real is None else now_real
    plan = cfg.plan()
    props = sd.show([u for _, u in plan])
    jobs = sd.jobs()
    states = [unit_state(c, u, props.get(u, {}), jobs, sd, now_mono, now_real) for c, u in plan]
    obs, obs_error = None, ""
    if observe and any(s.level == OK and (s.component in ROS_LIVE or s.component.startswith("driver@"))
                       for s in states):
        try:
            obs = observe(cfg, OBSERVE_S)
        except Exception as e:  # noqa: BLE001 — любая беда rclpy/DDS не должна ронять doctor
            obs_error = str(e)
    scan_owner = next((c for c, _ in plan if c.startswith("driver@")
                       and cfg.driver(c.split("@", 1)[1], "TOPIC") == cfg.get("NEUROBOT_SCAN_TOPIC")), None)
    live_ros = {"safety": lambda: _check_safety(cfg, obs), "executor": lambda: _check_executor(cfg, obs),
                "perception": lambda: _check_perception(obs)}
    findings: List[Finding] = []
    for s in states:
        live = None
        if s.level == OK:
            if s.component == "rosbridge":
                live = _check_rosbridge(cfg, ws)
            elif s.component.startswith("labworker@"):
                live = _check_labworker(cfg, s.component, get)
            elif s.component == "mcp":
                live = _check_mcp(cfg, probe)
            elif s.component == "picoclaw":
                live = _check_agent(sd, props.get(s.unit, {}), now_real)
            elif obs is not None and s.component in live_ros:
                live = live_ros[s.component]()
            elif obs is not None and s.component == scan_owner:
                live = _check_scan(s.component, cfg, obs)
        findings.append(_merge(s, live) if live else s)
    at = sum(1 for c, _ in plan if c.startswith("driver@"))
    if obs is not None:
        # датчики — после драйверов, до safety_gate: без них он не пустит робота, и причина — в них
        sensors = [] if scan_owner else [_check_scan("scan", cfg, obs)]
        sensors += [_check_sensor(t, typ, hz, obs) for t, typ, hz in cfg.sensors()]
        findings[at:at] = sensors
        joy = ["{} ({})".format(t, ", ".join(sorted(set(e["pub"])))) for t, e in sorted(obs["joy"].items())
               if e["pub"]]
        if joy:
            findings.append(Finding("twist_mux", "", WARN, "cmd_vel_joy", "геймпад главнее safety_gate: " +
                                    "; ".join(joy)))
    if obs_error:
        # компоненты ROS живы, а граф не снять: датчики, safety_gate, защёлка и писатели /cmd_vel не проверены —
        # «ОК» здесь был бы непроверенным
        findings.insert(at, Finding("ros", "", FAIL, "ros_unavailable", "датчики, safety_gate и писатели /cmd_vel "
                                    "не проверены: " + obs_error))
    root = next((f for f in findings if f.level == FAIL), None) or next(
        (f for f in findings if f.level == STARTING), None)
    return Report(findings, root)


LABEL = {OK: "ОК", WARN: "ВНИМАНИЕ", FAIL: "ОШИБКА", STARTING: "ЗАПУСК"}


def format_text(rep: Report) -> str:
    width = max([len(f.component) for f in rep.findings] + [10])
    lines = ["{:<{w}}  {:<8}  {}".format(f.component, LABEL[f.level], f.text, w=width) for f in rep.findings]
    warnings = sum(f.level == WARN for f in rep.findings)
    if rep.root is None:
        lines.append("ИТОГ: ОК" + (" (предупреждений: {})".format(warnings) if warnings else ""))
    elif rep.root.level == STARTING:
        lines.append("ИТОГ: ещё поднимается — {}: {}".format(rep.root.component, rep.root.text))
    else:
        lines.append("ИТОГ: {} — {}".format(rep.root.component, rep.root.text))
        lines.append("Что делать: " + report.hint(rep.root.code))
    return "\n".join(lines)
