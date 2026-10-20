"""CLI neurobot: doctor, проверки перед стартом, ожидание готовности, явный ноль, сторож, start_test.

    neurobot doctor [--json] [--no-ros]          всё ли поднялось (коды: 0 ОК, 1 проблема, 2 ошибка, 3 запуск)
    neurobot estop engage|release [--reason Т]   аварийная защёлка safety_gate; release — человеком, в терминале, root
    neurobot precheck <компонент>                ExecStartPre: safety | executor | perception | rosbridge | mcp |
                                                 picoclaw | driver@<имя> | labworker@<имя>
    neurobot wait-port rosbridge                 ExecStartPost: WebSocket на NEUROBOT_ROSBRIDGE_PORT
    neurobot wait-mcp                            ExecStartPost: MCP отвечает на initialize + tools/list
    neurobot wait-agent                          ExecStartPost (+, root): PicoClaw подключил MCP (журнал запуска)
    neurobot wait-topic driver@<имя>             ExecStartPost: первое сообщение NEUROBOT_DRIVER_<имя>_TOPIC
    neurobot wait-http labworker@<имя>           ExecStartPost: GET NEUROBOT_LABWORKER_<имя>_URL отвечает 200
    neurobot wait-graph executor|perception|ui   ExecStartPost: узел компонента в графе и не пишет в /cmd_vel
                                                 (ui — пишет только в стоп и защёлку)
    neurobot ui-url [--ttl S]                    ссылка входа в пульт оператора (root; одноразовая)
    neurobot zero                                ExecStopPost safety_gate: явный ноль в /cmd_vel
    neurobot watchdog [--once]                   сторож: драйверы и rosbridge (часто), MCP и PicoClaw (период)
    neurobot start-test --cycles N --out КАТАЛОГ холодные старты и «сломанные» старты (root)
    neurobot picoclaw-config check|merge         конфиг PicoClaw: проверка / наложение оверлея
    neurobot render --out КАТАЛОГ                юниты из шаблонов (install.sh); печатает, что включать
    neurobot env-get ИМЯ…                        значения env-файла (install.sh)
Общее: --env ФАЙЛ (иначе $NEUROBOT_ENV или /etc/neurobot/neurobot.env). Причины — в stderr строкой
«neurobot-<инструмент>: <компонент>: <код>: <текст>»; stdout — только для вывода doctor/render/env-get.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from typing import Optional

from . import backoff, checks, doctor, estop, picoclaw_cfg, render, start_test, uiurl, watchdog
from .config import Config, EnvError, load
from .picolog import analyze
from .probes import ProbeError, http_get, probe_mcp, ws_handshake
from .report import emit, hint
from .sd import Systemd, SystemdError

DEPLOY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROS_COMMANDS = ("doctor", "estop", "zero", "wait-topic", "wait-graph", "precheck", "watchdog")
GRAPH_PRECHECK = ("safety", "executor")


def ensure_ros(cfg: Config, argv: list) -> None:
    """Если rclpy не импортируется, перезапустить себя под окружением ROS 2 из env-файла (один раз)."""
    try:
        import rclpy  # noqa: F401
        return
    except ImportError:
        pass
    setup = cfg.get("NEUROBOT_ROS_SETUP")
    if os.environ.get("NEUROBOT_ROS_SOURCED") or not os.path.isfile(setup):
        return
    env = dict(os.environ, NEUROBOT_ROS_SOURCED="1",
               PYTHONPATH=os.pathsep.join(p for p in (DEPLOY_DIR, os.environ.get("PYTHONPATH", "")) if p))
    script = 'set +u; source "$1" >/dev/null; if [ -n "$2" ]; then source "$2" >/dev/null; fi; shift 2; exec "$@"'
    os.execvpe("bash", ["bash", "-c", script, "bash", setup, cfg.get("NEUROBOT_ROS_OVERLAY"),
                        sys.executable, "-m", "neurobot_deploy"] + argv, env)


def _state_dir() -> str:
    """StateDirectory= юнита ($STATE_DIRECTORY от systemd; "" — запуск не из юнита)."""
    return os.environ.get("STATE_DIRECTORY", "").split(":")[0]


def _wait(fn, timeout: float, period: float = 0.5):
    """Повторять fn() до успеха (не None) или до timeout; вернуть (результат, последняя ошибка)."""
    end, err = time.monotonic() + timeout, ""
    while True:
        try:
            res = fn()
            if res is not None:
                return res, ""
        except (ProbeError, SystemdError) as e:
            err = str(e)
        if time.monotonic() >= end:
            return None, err
        time.sleep(period)


def cmd_doctor(cfg: Config, args) -> int:
    observe = None
    if not args.no_ros:
        from .rosgraph import observe
    rep = doctor.diagnose(cfg, Systemd(), observe=observe)
    print(json.dumps(rep.as_dict(), ensure_ascii=False) if args.json else doctor.format_text(rep))
    return rep.exit_code


def cmd_estop(cfg: Config, args) -> int:
    from . import rosgraph
    ros = estop.Ros(rosgraph.safety_state, rosgraph.send_estop, rosgraph.estop_message, rosgraph.wait_safety_state)
    try:
        if args.action == "engage":
            return estop.engage(cfg, ros, args.reason)

        def ask(prompt: str) -> str:
            sys.stderr.write(prompt)
            sys.stderr.flush()
            return sys.stdin.readline()
        return estop.release(cfg, ros, args.reason, sys.stdin.isatty(), ask, os.geteuid())
    except (rosgraph.RosUnavailable, RuntimeError) as e:
        emit("estop", "safety", "ros_unavailable", str(e))
        return 1


def restart_pause(cfg: Config, component: str, sd: Optional[Systemd] = None, clock=time.monotonic,
                  sleep=time.sleep) -> float:
    """Пауза перед частым автоматическим рестартом (backoff.py). Только из юнита (INVOCATION_ID, StateDirectory):
    precheck из терминала не ждёт."""
    unit, state = backoff.COMPONENTS.get(component), _state_dir()
    if not unit or not state or not os.environ.get("INVOCATION_ID"):
        return 0.0
    pauses, reset_s = cfg.nums("NEUROBOT_RESTART_PAUSES_S"), cfg.num("NEUROBOT_RESTART_RESET_S")
    try:
        n = int((sd or Systemd()).show([unit], ("Id", "NRestarts")).get(unit, {}).get("NRestarts") or 0)
    except (SystemdError, ValueError):
        n = 0  # не узнали — как ручной старт: без паузы
    path = os.path.join(state, backoff.STATE)
    pause, streak, new = backoff.plan(n, backoff.read_state(path), backoff.boot_id(), clock(), pauses, reset_s)
    try:
        backoff.write_state(path, new)
    except OSError as e:
        emit("precheck", component, "restart_loop", "метку запуска не записать ({}): пауза не нарастёт".format(e),
             level="warning")
    if pause > 0:
        emit("precheck", component, "restart_loop", "{}-й рестарт подряд, прошлый запуск — меньше {:g} с назад: "
             "пауза {:g} с".format(streak, reset_s, pause), level="warning")
        sleep(pause)
    return pause


def cmd_precheck(cfg: Config, args) -> int:
    restart_pause(cfg, args.component)
    graph = None
    if args.component in GRAPH_PRECHECK:
        from . import rosgraph

        def graph(c, topics):
            try:
                return rosgraph.graph(c, topics)
            except (rosgraph.RosUnavailable, RuntimeError) as e:  # RuntimeError — и RCLError инициализации rclpy
                raise checks.GraphUnavailable(str(e))
    problems = checks.precheck(args.component, cfg, graph=graph, state=_state_dir())
    for p in problems:
        emit("precheck", args.component, p.code, p.text, level="err" if p.fatal else "warning")
    if any(p.fatal for p in problems):
        time.sleep(cfg.num("NEUROBOT_PRECHECK_BACKOFF_S"))  # RestartSteps нет до v254: пауза между попытками
        return 1
    return 0


def cmd_wait_port(cfg: Config, args) -> int:
    port = cfg.port("NEUROBOT_ROSBRIDGE_PORT")
    t0 = time.monotonic()
    res, err = _wait(lambda: ws_handshake("127.0.0.1", port, timeout=2.0) or True, cfg.num("NEUROBOT_WAIT_S"))
    if res is None:
        emit("wait", args.component, "port_timeout", "порт {} не ответил WebSocket за {:g} с: {}".format(
            port, cfg.num("NEUROBOT_WAIT_S"), err))
        return 1
    emit("wait", args.component, "ready", "WebSocket :{} за {:.1f} с".format(port, time.monotonic() - t0), "info")
    return 0


def cmd_wait_mcp(cfg: Config, args) -> int:
    url = cfg.get("NEUROBOT_MCP_URL")
    res, err = _wait(lambda: probe_mcp(url, 3.0), cfg.num("NEUROBOT_WAIT_S"))
    if res is None:
        emit("wait", "mcp", "mcp_timeout", "{} не ответил за {:g} с: {}".format(url, cfg.num("NEUROBOT_WAIT_S"), err))
        return 1
    emit("wait", "mcp", "ready", "{}: {}".format(url, ", ".join(res.tools)), "info")
    return 0


def cmd_wait_agent(cfg: Config, args) -> int:
    inv = os.environ.get("INVOCATION_ID", "")
    if not inv:
        emit("wait", "picoclaw", "env_invalid", "нет INVOCATION_ID: wait-agent запускается из ExecStartPost юнита")
        return 1
    sd = Systemd()

    def state():
        log = analyze(sd.journal(invocation=inv, lines=500))
        if log.fatal and log.fatal.code == "agent_zombie":
            return log
        return log if log.init_done and log.connected else None
    log, err = _wait(state, cfg.num("NEUROBOT_WAIT_S"))
    if log is None:
        emit("wait", "picoclaw", "agent_timeout", "PicoClaw не сообщил о подключении MCP за {:g} с{}".format(
            cfg.num("NEUROBOT_WAIT_S"), ": " + err if err else ""))
        return 1
    if log.fatal:
        emit("wait", "picoclaw", "agent_zombie", log.fatal.line[-200:])
        return 1
    emit("wait", "picoclaw", "ready", "MCP: {}".format(", ".join(log.connected)), "info")
    return 0


def cmd_wait_topic(cfg: Config, args) -> int:
    from .rosgraph import RosUnavailable, wait_topic
    name = args.component.split("@", 1)[-1]
    topic = cfg.driver(name, "TOPIC")
    if not topic:
        return 0
    try:
        got = wait_topic(cfg, topic, cfg.num("NEUROBOT_WAIT_S"))
    except (RosUnavailable, RuntimeError) as e:  # RuntimeError — и RCLError инициализации rclpy
        emit("wait", args.component, "ros_unavailable", str(e))
        return 1
    if got is None:
        emit("wait", args.component, "topic_timeout", "нет сообщений {} за {:g} с".format(
            topic, cfg.num("NEUROBOT_WAIT_S")))
        return 1
    emit("wait", args.component, "ready", "{}: первое сообщение за {:.1f} с".format(topic, got), "info")
    return 0


def cmd_wait_http(cfg: Config, args) -> int:
    name = args.component.split("@", 1)[-1]
    cfg.labworker_url(name)  # адрес проверен: иначе EnvError с понятной причиной
    url, limit = cfg.labworker(name, "URL"), cfg.num("NEUROBOT_LABWORKER_WAIT_S")
    t0 = time.monotonic()

    def ready():
        code, body = http_get(url, 3.0)
        if code != 200:  # воркер может держать порт и отвечать 503 «загружаю модель»
            raise ProbeError("HTTP {}: {}".format(code, body[:120]))
        return code
    res, err = _wait(ready, limit, period=1.0)
    if res is None:
        emit("wait", args.component, "labworker_timeout", "{} не ответил 200 за {:g} с: {}".format(url, limit, err))
        return 1
    emit("wait", args.component, "ready", "{}: 200 за {:.1f} с".format(url, time.monotonic() - t0), "info")
    return 0


def cmd_wait_graph_ui(cfg: Config, args) -> int:
    """Пульт после READY: его узел в графе и пишет только в стоп и защёлку (checks.UI_PUBLISHERS)."""
    from .rosgraph import RosUnavailable, node_publishers
    name = cfg.get("NEUROBOT_UI_NODE")
    try:
        pubs = node_publishers(cfg, name, cfg.num("NEUROBOT_WAIT_S"))
    except (RosUnavailable, RuntimeError) as e:
        emit("wait", "ui", "ros_unavailable", str(e))
        return 1
    problems = checks.ui_problems(cfg, pubs)
    for p in problems:
        emit("wait", "ui", p.code, p.text)
    if problems:
        return 1
    emit("wait", "ui", "ready", "узел {} пишет только в {}".format(
        name, ", ".join(t for t in pubs if t not in checks.ROS_INFRA) or "никуда"), "info")
    return 0


def cmd_wait_graph(cfg: Config, args) -> int:
    if args.component == "ui":
        return cmd_wait_graph_ui(cfg, args)
    from .rosgraph import RosUnavailable, wait_node
    name = cfg.get("NEUROBOT_{}_NODE".format(args.component.upper()))
    try:
        graph = wait_node(cfg, name, checks.own_topics(cfg), cfg.num("NEUROBOT_WAIT_S"))
    except (RosUnavailable, RuntimeError) as e:
        emit("wait", args.component, "ros_unavailable", str(e))
        return 1
    if graph is None:
        emit("wait", args.component, "node_missing", "узла {} нет в графе ROS за {:g} с: писателей скорости не "
             "проверить (NEUROBOT_{}_NODE, ROS_DOMAIN_ID)".format(name, cfg.num("NEUROBOT_WAIT_S"),
                                                                  args.component.upper()))
        return 1
    problems = checks.own_problems(args.component, cfg, graph)
    for p in problems:
        emit("wait", args.component, p.code, p.text)
    state = _state_dir()
    if state and any(p.code == "cmd_vel_writer" for p in problems):
        # без метки после рестарта узел снова писал бы мимо safety_gate до этой же проверки (check_writer_mark)
        try:
            checks.mark_writer(args.component, cfg, state, problems, time.time())
        except OSError as e:
            emit("wait", args.component, "cmd_vel_writer", "метку для precheck не записать ({}): после рестарта узел "
                 "снова будет писать мимо safety_gate до проверки графа".format(e), level="warning")
    if problems:
        return 1
    req = cfg.get("NEUROBOT_CMD_VEL_REQ")
    executor = args.component == "executor"
    emit("wait", args.component, "ready", "узел {} не пишет в {}{}".format(
        name, ", ".join(t for t in checks.own_topics(cfg) if not (executor and t == req)),
        "; пишет в " + req if executor else ""), "info")
    return 0


def cmd_zero(cfg: Config, args) -> int:
    from .rosgraph import RosUnavailable, publish_zero
    topic = cfg.get("NEUROBOT_CMD_VEL")
    try:
        subs = publish_zero(cfg)
    except (RosUnavailable, RuntimeError, ValueError) as e:  # RuntimeError — и RCLError инициализации rclpy
        emit("zero", "safety", "ros_unavailable", "явный ноль не отправлен: {}".format(e))
        return 1
    if not subs:
        emit("zero", "safety", "zero_undelivered", "явный ноль в {} не доставлен: подписчиков нет ({} не "
             "слушает)".format(topic, cfg.get("NEUROBOT_MUX_NODE") or "база"))
        return 1
    emit("zero", "safety", "zero_sent", "{}: нули {}, подписчиков {}".format(
        topic, cfg.get("NEUROBOT_CMD_VEL_TYPE").rsplit("/", 1)[-1], subs), level="info")
    return 0


def cmd_watchdog(cfg: Config, args) -> int:
    sd = Systemd()
    if args.once:
        watchdog.run_once(cfg, sd)
        return 0
    topics = sorted({t for ts in watchdog.driver_watch(cfg).values() for t, _ in ts})
    watch = None
    if topics:
        from .rosgraph import RosUnavailable, TopicWatch
        try:
            watch = TopicWatch(cfg, topics)
        except (RosUnavailable, RuntimeError) as e:  # RuntimeError — и RCLError инициализации rclpy
            emit("watchdog", "watchdog", "ros_unavailable", "свежесть тем драйверов не смотрю: {}".format(e),
                 level="warning")
    watchdog.loop(cfg, sd, cfg.num("NEUROBOT_WATCHDOG_PERIOD_S"), cfg.num("NEUROBOT_WATCHDOG_FAST_S"), watch)
    return 0


def cmd_start_test(cfg: Config, args) -> int:
    if os.geteuid() != 0:
        print("start-test: нужен root (systemctl start/stop, drop-in в /run)", file=sys.stderr)
        return 2
    can = start_test.applicable(cfg)
    if args.broken is None:
        args.broken = can
    unknown = [b for b in args.broken if b not in can]
    if unknown:
        print("start-test: сценарии {} не подходят к составу (можно: {})".format(unknown, ", ".join(can)),
              file=sys.stderr)
        return 2
    doctor_cmd = [sys.executable, "-m", "neurobot_deploy", "--env", cfg.path, "doctor"]
    # SIGTERM (systemctl stop, timeout) — как Ctrl+C: finally цикла снимает подмену юнита
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in (DEPLOY_DIR, os.environ.get("PYTHONPATH", "")) if p))
    return start_test.run(cfg, Systemd(), args, doctor_cmd, env)


def cmd_picoclaw_config(cfg: Config, args) -> int:
    path = args.config or cfg.picoclaw_config()
    if args.action == "merge":
        overlay = picoclaw_cfg.load_overlay(args.overlay or picoclaw_cfg.OVERLAY)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            emit("picoclaw-config", "picoclaw", "config_invalid", "{}: {}".format(path, e))
            return 1
        version, source = picoclaw_cfg.detect_version(cfg.get("NEUROBOT_PICOCLAW_VERSION"),
                                                      cfg.get("NEUROBOT_PICOCLAW_CMD"), data)
        schema, exact = picoclaw_cfg.load_schema(version)
        overlay, dropped = picoclaw_cfg.prune(overlay, schema)
        for entry in ((overlay.get("tools") or {}).get("mcp") or {}).get("servers", {}).values():
            if "type" in entry:  # наш сервер: адрес — из env-файла
                entry["url"] = cfg.get("NEUROBOT_MCP_URL")
        backup = picoclaw_cfg.apply(path, overlay, args.request_timeout)
        print("{}: оверлей наложен для PicoClaw {} ({}{}){}; копия — {}".format(
            path, version or "неизвестной версии", source or "версия не определена",
            "" if exact else ", схемы нет — только общие поля известных версий",
            "; нет в этой версии, пропущено: " + ", ".join(dropped) if dropped else "", backup), file=sys.stderr)
    problems = checks.check_picoclaw_config(Config(dict(cfg.values, PICOCLAW_CONFIG=path), cfg.path))
    for p in problems:
        emit("precheck", "picoclaw", p.code, p.text, level="err" if p.fatal else "warning")
    return 1 if any(p.fatal for p in problems) else 0


def cmd_render(cfg: Config, args) -> int:
    units = render.render(cfg, args.units or os.path.join(DEPLOY_DIR, "units"))
    os.makedirs(args.out, exist_ok=True)
    for name, text in units.items():
        with open(os.path.join(args.out, name), "w", encoding="utf-8") as f:
            f.write(text)
    print("\n".join(render.enable_list(cfg)))
    return 0


def cmd_ui_url(cfg: Config, args) -> int:
    return uiurl.run(cfg, args.ttl, os.geteuid())


def cmd_env_get(cfg: Config, args) -> int:
    for key in args.keys:
        print(cfg.get(key))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="neurobot", description="Запуск и проверка neurobot (PLAN §5)")
    p.add_argument("--env", help="env-файл (по умолчанию $NEUROBOT_ENV или /etc/neurobot/neurobot.env)")
    sub = p.add_subparsers(dest="cmd")
    sub.required = True
    s = sub.add_parser("doctor", help="всё ли поднялось")
    s.add_argument("--json", action="store_true", help="машиночитаемый вывод (start_test)")
    s.add_argument("--no-ros", action="store_true", help="без проверки тем ROS (датчики, safety_gate, исполнитель, "
                                                          "камера)")
    s = sub.add_parser("estop", help="аварийная защёлка safety_gate")
    s.add_argument("action", choices=("engage", "release"))
    s.add_argument("--reason", default="", help="причина (уходит в команду защёлки)")
    for name in ("precheck", "wait-port", "wait-topic", "wait-http"):
        sub.add_parser(name).add_argument("component")
    sub.add_parser("wait-graph").add_argument("component", choices=("executor", "perception", "ui"))
    s = sub.add_parser("ui-url", help="одноразовая ссылка входа в пульт оператора (root)")
    s.add_argument("--ttl", type=float, default=uiurl.TTL_S, help="сколько секунд ссылка действует (по умолчанию "
                                                                    "%(default)g)")
    for name in ("wait-mcp", "wait-agent", "zero"):
        sub.add_parser(name)
    s = sub.add_parser("watchdog", help="сторож: свежесть тем драйверов и ping rosbridge (NEUROBOT_WATCHDOG_FAST_S), "
                                         "MCP и PicoClaw (NEUROBOT_WATCHDOG_PERIOD_S)")
    s.add_argument("--once", action="store_true", help="один проход MCP и PicoClaw (действия — строками причин в "
                                                       "stderr); частым пробам нужна история — только в цикле")
    s = sub.add_parser("start-test", help="холодные и «сломанные» старты (root)")
    s.add_argument("--cycles", type=int, default=100, help="холодных стартов (по умолчанию 100, K9)")
    s.add_argument("--broken", type=lambda v: [x for x in v.split(",") if x],
                   help="сценарии поломки через запятую ({}; по умолчанию — все, что подходят к составу); '' — без "
                        "них".format(",".join(start_test.BROKEN)))
    s.add_argument("--ready-timeout", type=float, default=180.0, help="ждать «ОК»/причину doctor, с")
    s.add_argument("--stop-timeout", type=float, default=60.0)
    s.add_argument("--gesture-cmd", default="", help="команда «один жест»: код 0 — робот отреагировал")
    s.add_argument("--deviation", action="append", help="отклонение стенда для manifest.json (можно несколько)")
    s.add_argument("--out", required=True, help="каталог итогов (cycles.jsonl, summary.json, manifest.json)")
    s = sub.add_parser("picoclaw-config", help="конфиг PicoClaw: check | merge")
    s.add_argument("action", choices=("check", "merge"))
    s.add_argument("--config", help="config.json (по умолчанию $PICOCLAW_CONFIG, $PICOCLAW_HOME или ~пользователя)")
    s.add_argument("--overlay", help="оверлей (по умолчанию picoclaw/config.overlay.json пакета; поля, которых нет "
                                     "в установленной версии PicoClaw, отбрасываются)")
    s.add_argument("--request-timeout", type=int, default=20, help="model_list[].request_timeout, с")
    s = sub.add_parser("render", help="юниты из шаблонов")
    s.add_argument("--out", required=True)
    s.add_argument("--units", help="каталог шаблонов (по умолчанию units/ пакета)")
    s = sub.add_parser("env-get")
    s.add_argument("keys", nargs="+")
    return p


HANDLERS = {"doctor": cmd_doctor, "estop": cmd_estop, "precheck": cmd_precheck, "wait-port": cmd_wait_port,
            "wait-mcp": cmd_wait_mcp, "wait-agent": cmd_wait_agent, "wait-topic": cmd_wait_topic,
            "wait-http": cmd_wait_http, "wait-graph": cmd_wait_graph, "zero": cmd_zero, "watchdog": cmd_watchdog,
            "start-test": cmd_start_test, "ui-url": cmd_ui_url,
            "picoclaw-config": cmd_picoclaw_config, "render": cmd_render, "env-get": cmd_env_get}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    try:
        cfg = load(args.env)
        if args.cmd in ROS_COMMANDS and not getattr(args, "no_ros", False) and (
                args.cmd != "precheck" or args.component in GRAPH_PRECHECK):
            ensure_ros(cfg, argv)
        return HANDLERS[args.cmd](cfg, args)
    except EnvError as e:
        print("neurobot: {}\nЧто делать: {}".format(e, hint("env_invalid")), file=sys.stderr)
        return 2
    except SystemdError as e:
        print("neurobot: {}".format(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
