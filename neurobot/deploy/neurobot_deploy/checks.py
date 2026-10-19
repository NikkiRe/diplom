"""Проверки перед стартом (ExecStartPre) и их сборка по компонентам (PLAN §5.4 «Проверки перед стартом»).

Каждая проверка возвращает список Problem; fatal=False — предупреждение (старт не блокирует). Корни /sys и
/proc — параметры: тесты и стенд подставляют свои деревья. Проверки только читают; веса хешируются как байты,
не распаковываются (pickle не загружаем); модули интерпретатора ищутся find_spec, без импорта.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from . import picoclaw_cfg
from .config import Config, EnvError

LOOPBACK = ("127.0.0.1", "localhost", "::1")
SUPERVISED = "supervised.launch.py"  # обёртка ros2 launch deploy: смерть узла гасит launch, юнит перезапускается
# Списки тем rosbridge 2.x (Jazzy) раздельно для публикации и подписки: объединяются с topics_glob; в 1.3.1 их нет.
ROSBRIDGE_GLOBS_2X = ("topics_pub_glob", "topics_sub_glob")
# Ключи секции server патча ros_mcp_server (уходят в FastMCP(**server)).
MCP_SERVER_KEYS = ("host", "port", "streamable_http_path", "json_response", "stateless_http", "log_level")
# Встроенные инструменты PicoClaw, лишние роботу (PC7), и те из них, что в v0.3.1 выключены по умолчанию
# (pkg/config/defaults.go). exec и cron с командами — отказ, а не предупреждение: это shell для LLM на роботе.
PICOCLAW_TOOLS_OFF = ("append_file", "edit_file", "write_file", "list_dir", "read_file", "load_image",
                      "send_file", "send_tts", "spawn", "spawn_status", "subagent", "cron", "skills",
                      "find_skills", "install_skill", "web", "web_fetch", "serial", "spi", "i2c")
PICOCLAW_DEFAULT_OFF = ("send_tts", "spawn_status", "spi", "serial", "i2c")
# Уровни журнала PicoClaw, на которых видны строки подключения MCP и вызовов инструментов (wait-agent, сторож).
PICOCLAW_LOG_OK = ("info", "debug")
# Метка wait-graph в StateDirectory исполнителя и распознавания: узел писал мимо safety_gate (check_writer_mark).
WRITER_MARK = "cmd_vel_writer.json"
_PYTHON = re.compile(r"python[0-9.]*")
_REQ = re.compile(r"([A-Za-z_][\w.]*)(?:(<|>=)([0-9][0-9.]*))?")
# Выполняется целевым интерпретатором (Python 3.8+): find_spec без импорта; версия — только у модулей с условием.
_PROBE = """
import importlib.util, json, sys
out = {"python": "%d.%d.%d" % sys.version_info[:3]}
for arg in sys.argv[1:]:
    name, need = arg.split(":")
    try:
        found = importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        found = False
    version = ""
    if found and need == "v":
        try:
            from importlib import metadata
            version = metadata.version(name)
        except Exception:
            try:
                version = str(getattr(__import__(name), "__version__", ""))
            except Exception:
                version = ""
    out[name] = version if found else None
print(json.dumps(out))
"""


class Problem(NamedTuple):
    code: str
    text: str
    fatal: bool = True


class GraphUnavailable(RuntimeError):
    """Граф ROS 2 не снять (нет rclpy, DDS): проверка писателей скорости невозможна — это отказ, а не пропуск."""


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


# --- окружение и файлы ------------------------------------------------------------------------------------------

def check_env(cfg: Config, keys: Sequence[str]) -> List[Problem]:
    return [Problem("env_invalid", "{} не задана в {}".format(k, cfg.path)) for k in keys if not cfg.get(k)]


def check_json(path: str, what: str) -> List[Problem]:
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as f:
            json.load(f)
    except OSError as e:
        return [Problem("config_invalid", "{} {}: {}".format(what, path, e.strerror or e))]
    except ValueError as e:
        return [Problem("config_invalid", "{} {}: не JSON: {}".format(what, path, e))]
    return []


def check_device(path: str) -> List[Problem]:
    if path and not os.path.exists(path):
        return [Problem("device_missing", "нет устройства {}".format(path))]
    return []


# --- интерпретатор Python компонента ------------------------------------------------------------------------------

_HINTS = {"numpy": "cv_bridge и пакеты ROS 2 Foxy собраны с NumPy 1.x (N5)", "python": "FastMCP — Python ≥ 3.10 (M13)",
          "mcp": "streamable HTTP — с mcp 1.8"}


def _vt(text: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text or "")[:3])


def interpreter(command: str) -> str:
    """Интерпретатор Python из команды компонента («env A=1 /usr/bin/python3 x.py …»); "" — команда не Python."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return ""
    prog = next((a for a in argv if a != "env" and "=" not in a), "")
    return prog if _PYTHON.fullmatch(os.path.basename(prog)) else ""


def _probe_python(python: str, names: Sequence[str], cfg: Config) -> Tuple[Optional[Dict[str, Optional[str]]], str]:
    """({модуль: версия | "" (есть, версия не нужна) | None (нет)}, "") в окружении ROS 2 из env-файла
    или (None, почему интерпретатор не запустился)."""
    args = [python, "-c", _PROBE] + list(names)
    setup = cfg.get("NEUROBOT_ROS_SETUP")
    if os.path.isfile(setup):
        args = ["bash", "-c", 'set +u; source "$1" >/dev/null 2>&1; if [ -n "$2" ]; then source "$2" >/dev/null 2>&1; '
                'fi; shift 2; exec "$@"', "bash", setup, cfg.get("NEUROBOT_ROS_OVERLAY")] + args
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30, cwd="/")
    except (OSError, subprocess.SubprocessError) as e:
        return None, "{} не запускается: {}".format(python, e)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]), ""
    except (ValueError, IndexError):
        return None, "{} не запускается (код {}): {}".format(python, r.returncode, r.stderr.strip()[-200:])


def check_python(command: str, key: str, cfg: Config) -> List[Problem]:
    """Интерпретатор команды компонента: модули и версии из key («numpy<2 torch rclpy», «python>=3.10 mcp>=1.8»),
    абсолютный путь не через шим pyenv (N6). Команда не Python (ros2 launch …) — не проверяется."""
    python = interpreter(command)
    if not python:
        return []
    reqs = []
    for token in cfg.get(key).split():
        m = _REQ.fullmatch(token)
        if not m:
            raise EnvError("{}: ожидалось «модуль», «модуль<версия» или «модуль>=версия», получено {!r}".format(
                key, token))
        reqs.append(m.groups())
    out = []
    if not os.path.isabs(python) or "/.pyenv/shims/" in python:
        out.append(Problem("python_env", "{}: не абсолютный путь или шим pyenv — какой Python запустится, зависит от "
                           "окружения (N6)".format(python), fatal=False))
    found, error = _probe_python(python, ["{}:{}".format(n, "v" if op else "") for n, op, _ in reqs
                                          if n != "python"], cfg)
    if found is None:
        return out + [Problem("python_env", error)]
    for name, op, want in reqs:
        got = found.get(name)
        hint = " — " + _HINTS[name] if name in _HINTS else ""
        if got is None:
            out.append(Problem("python_env", "{}: нет модуля {}{}".format(python, name, hint)))
        elif op and not got:
            out.append(Problem("python_env", "{}: версия {} не определена, нужна {}{}".format(
                python, name, op, want), fatal=False))
        elif op and (_vt(got) < _vt(want)) != (op == "<"):
            out.append(Problem("python_env", "{}: {} {}, нужна {}{}{}".format(python, name, got, op, want, hint)))
    return out


# --- камера и веса ------------------------------------------------------------------------------------------------

def usb_devices(sysfs: str) -> List[Tuple[str, str]]:
    """[(vid:pid, скорость Мбит/с)] из <sysfs>/bus/usb/devices."""
    base = os.path.join(sysfs, "bus", "usb", "devices")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    out = []
    for name in names:
        d = os.path.join(base, name)
        vid, pid = _read(os.path.join(d, "idVendor")), _read(os.path.join(d, "idProduct"))
        if vid and pid:
            out.append(("{}:{}".format(vid, pid).lower(), _read(os.path.join(d, "speed"))))
    return out


def check_camera(sysfs: str, ids: str) -> List[Problem]:
    want = [i.strip().lower() for i in ids.split(",") if i.strip()]
    found = [(i, speed) for i, speed in usb_devices(sysfs) if i in want]
    if not found:
        return [Problem("camera_missing", "камера {} не найдена на USB ({}/bus/usb/devices)".format(
            " или ".join(want), sysfs.rstrip("/")))]
    i, speed = found[0]
    if speed and speed.isdigit() and int(speed) < 5000:
        return [Problem("camera_usb2", "камера {} на USB {} Мбит/с: нужен USB 3, на USB 2 кадры теряются "
                        "(«Frame didn't arrive», R1)".format(i, speed), fatal=False)]
    return []


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check_weights(manifest: str) -> List[Problem]:
    """Манифест в формате sha256sum: «<sha256>  <путь>» (или «<sha256> *<путь>»)."""
    try:
        with open(manifest, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except OSError as e:
        return [Problem("config_invalid", "манифест весов {}: {}".format(manifest, e.strerror or e))]
    if not lines:
        return [Problem("config_invalid", "манифест весов {} пуст".format(manifest))]
    out = []
    for ln in lines:
        want, _, path = ln.partition(" ")
        path = path.strip().lstrip("*")
        if len(want) != 64 or not path:
            out.append(Problem("config_invalid", "манифест весов {}: строка {!r}".format(manifest, ln)))
        elif not os.path.isfile(path):
            out.append(Problem("weights_missing", "нет файла весов {}".format(path)))
        else:
            got = sha256_file(path)
            if got != want.lower():
                out.append(Problem("weights_hash", "{}: sha256 {}…, ожидался {}…".format(path, got[:12], want[:12])))
    return out


# --- порты и процессы ---------------------------------------------------------------------------------------------

def _listen_inodes(proc: str, port: int) -> List[str]:
    out = []
    for name in ("tcp", "tcp6"):
        for line in _read(os.path.join(proc, "net", name)).splitlines()[1:]:
            f = line.split()
            if len(f) > 9 and f[3] == "0A" and int(f[1].rsplit(":", 1)[1], 16) == port:
                out.append(f[9])
    return out


def _pids(proc: str) -> List[int]:
    try:
        return sorted(int(n) for n in os.listdir(proc) if n.isdigit())
    except OSError:
        return []


def _unit_of(proc: str, pid: int) -> str:
    """Юнит systemd процесса по /proc/<pid>/cgroup (последний компонент *.service или *.scope)."""
    for line in _read(os.path.join(proc, str(pid), "cgroup")).splitlines():
        for part in reversed(line.rsplit(":", 1)[-1].split("/")):
            if part.endswith((".service", ".scope")):
                return part
    return ""


def _owner(proc: str, inodes: Sequence[str]) -> str:
    targets = {"socket:[{}]".format(i) for i in inodes}
    for pid in _pids(proc):
        fd_dir = os.path.join(proc, str(pid), "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(os.path.join(fd_dir, fd)) in targets:
                    unit = _unit_of(proc, pid)
                    return "pid {} ({}{})".format(pid, _read(os.path.join(proc, str(pid), "comm")),
                                                  ", " + unit if unit else "")
            except OSError:
                continue
    return "владелец не виден (нужен root)"


def check_port_free(port: int, proc: str = "/proc") -> List[Problem]:
    inodes = _listen_inodes(proc, port)
    if inodes:
        return [Problem("port_busy", "порт {} уже слушает {}".format(port, _owner(proc, inodes)))]
    return []


def check_foreign(patterns: Sequence[str], proc: str = "/proc", self_pid: Optional[int] = None,
                  supervised: Sequence[str] = ("neurobot-labworker@",)) -> List[Problem]:
    """Процессы вне нашего cgroup, у которых программа или аргумент-файл называется как один из patterns
    (`python3 src/camera/predict_live.py`, `realsense-viewer`) — ручной запуск в терминале или чужой юнит.
    Сравнивается имя файла целиком, а не подстрока: текст в аргументах (`--note "… predict_live.py …"`) не в счёт.
    Процессы в юнитах с префиксами supervised (воркеры лаборатории neurobot-labworker@, для распознавания с
    --camera ros — и узел камеры в neurobot-driver@) под супервизией, они не чужие."""
    self_pid = os.getpid() if self_pid is None else self_pid
    mine = _unit_of(proc, self_pid)
    out = []
    for pid in _pids(proc):
        if pid == self_pid:
            continue
        argv = [a for a in _read(os.path.join(proc, str(pid), "cmdline")).split("\0") if a]
        hit = next((p for p in patterns if any(os.path.basename(a) == p for a in argv)), None)
        if hit is None:
            continue
        cmd = " ".join(argv)
        unit = _unit_of(proc, pid)
        if (mine and unit == mine) or unit.startswith(tuple(supervised)):
            continue
        where = "юнит " + unit if unit.endswith(".service") else "вне сервисов ({})".format(unit or "?")
        out.append(Problem("foreign_process", "уже запущен {}: pid {}, {}: {}".format(hit, pid, where, cmd[:120])))
    return out


# --- граф ROS 2: цепочка движения и издатели /scan ----------------------------------------------------------------
# Граф — {тема: {"pub": [узел на каждого издателя], "sub": [узел на каждого подписчика]}}; его снимает rosgraph
# (precheck) или doctor, правила общие.

def _ours(name: str, own: str) -> bool:
    """Свой узел: сам компонент или наш вспомогательный (neurobot zero, doctor, precheck — neurobot_<роль>_<pid>).
    Издатель убитого kill -9 компонента висит в DDS до конца аренды — по имени он свой и рестарт не блокирует."""
    return name == own or name.startswith("neurobot_")


def _names(graph: Dict[str, Dict[str, List[str]]], topic: str, side: str = "pub") -> List[str]:
    return list((graph.get(topic) or {}).get(side) or [])


def graph_topics(component: str, cfg: Config) -> List[str]:
    """Темы, которые precheck компонента смотрит в графе ROS 2."""
    if component == "safety":
        return [t for t in [cfg.get("NEUROBOT_CMD_VEL"), cfg.get("NEUROBOT_BASE_CMD_TOPIC"),
                            cfg.get("NEUROBOT_SCAN_TOPIC")] + cfg.get("NEUROBOT_OVERRIDE_TOPICS").split()
                + cfg.get("NEUROBOT_JOY_TOPICS").split() if t]
    if component == "executor":
        return [cfg.get("NEUROBOT_CMD_VEL_REQ")]
    return []


def writer_problems(cfg: Config, graph: Dict[str, Dict[str, List[str]]]) -> List[Problem]:
    """Писатели скорости против цепочки (исполнитель → safety_gate → twist_mux → база): в /cmd_vel пишет только
    safety_gate; входы twist_mux главнее /cmd_vel перекрывают его явный ноль (N13); в вход базы пишет только
    twist_mux; twist_mux слушает /cmd_vel (иначе нули safety_gate не доходят до базы); геймпад — предупреждение."""
    topic, own = cfg.get("NEUROBOT_CMD_VEL"), cfg.get("NEUROBOT_SAFETY_NODE")
    out = [Problem("cmd_vel_writer", "в {} уже пишет узел {} — второй писатель мимо safety_gate (S4)".format(topic, n))
           for n in sorted(set(_names(graph, topic))) if not _ours(n, own)]
    for t in cfg.get("NEUROBOT_OVERRIDE_TOPICS").split():
        names = sorted(set(n for n in _names(graph, t) if not n.startswith("neurobot_")))
        if names:
            out.append(Problem("cmd_vel_override", "{} публикует {}: twist_mux ставит его выше {} — явный ноль "
                               "safety_gate не дойдёт до колёс (N13)".format(t, ", ".join(names), topic)))
    mux, base = cfg.get("NEUROBOT_MUX_NODE"), cfg.get("NEUROBOT_BASE_CMD_TOPIC")
    if mux and base:
        names = sorted(set(n for n in _names(graph, base) if n != mux and not n.startswith("neurobot_")))
        if names:
            out.append(Problem("cmd_vel_bypass", "в {} пишет {} — мимо twist_mux и safety_gate".format(
                base, ", ".join(names))))
    subs = sorted(set(_names(graph, topic, "sub")))
    if mux and mux not in subs:
        out.append(Problem("twist_mux_missing", "{} не слушает {}: нули и скорости safety_gate не доходят до базы "
                           "(подписчики: {})".format(mux, topic, ", ".join(subs) or "нет"), fatal=False))
    for t in cfg.get("NEUROBOT_JOY_TOPICS").split():
        names = sorted(set(_names(graph, t)))
        if names:
            out.append(Problem("cmd_vel_joy", "{} публикует {}: геймпад главнее safety_gate".format(
                t, ", ".join(names)), fatal=False))
    return out


def request_problems(cfg: Config, graph: Dict[str, Dict[str, List[str]]]) -> List[Problem]:
    """Вход safety_gate /neurobot/cmd_vel_req: пишет только исполнитель; safety_gate его слушает — без него
    исполнитель не нужен (скорости уходили бы в никуда, а при неверном ремапе — мимо защиты)."""
    topic, own = cfg.get("NEUROBOT_CMD_VEL_REQ"), cfg.get("NEUROBOT_EXECUTOR_NODE")
    out = [Problem("cmd_vel_writer", "в {} уже пишет узел {} — мимо исполнителя".format(topic, n))
           for n in sorted(set(_names(graph, topic))) if not _ours(n, own)]
    gate = cfg.get("NEUROBOT_SAFETY_NODE")
    if "safety" in cfg.components() and gate not in _names(graph, topic, "sub"):
        out.append(Problem("gate_missing", "{} не слушает {}: без safety_gate исполнитель не запускается".format(
            gate, topic)))
    return out


def graph_problems(component: str, cfg: Config, graph: Dict[str, Dict[str, List[str]]]) -> List[Problem]:
    """Граф против цепочки движения для precheck: safety_gate — писатели скорости и один издатель /scan (в графе
    робота было два urg_node2); исполнитель — его вход /neurobot/cmd_vel_req."""
    if component == "executor":
        return request_problems(cfg, graph)
    out = writer_problems(cfg, graph)
    scan = cfg.get("NEUROBOT_SCAN_TOPIC")
    pubs = _names(graph, scan)
    if len(pubs) > 1:
        out.append(Problem("scan_publishers", "{} публикуют {} издателя ({}): драйвер лидара запущен дважды".format(
            scan, len(pubs), ", ".join(sorted(set(pubs))))))
    elif not pubs:
        out.append(Problem("scan_stale", "у {} нет издателя: вперёд safety_gate не пустит".format(scan), fatal=False))
    return out


def _motion_inputs(cfg: Config) -> List[str]:
    """Входы цепочки движения, в которые исполнитель и распознавание сами не пишут: /cmd_vel (только safety_gate),
    вход базы (только twist_mux), входы twist_mux главнее /cmd_vel и геймпад."""
    return [t for t in [cfg.get("NEUROBOT_CMD_VEL"), cfg.get("NEUROBOT_BASE_CMD_TOPIC")]
            + cfg.get("NEUROBOT_OVERRIDE_TOPICS").split() + cfg.get("NEUROBOT_JOY_TOPICS").split() if t]


def remaps(command: str, node: str) -> Dict[str, str]:
    """Ремапы тем из «--ros-args -r откуда:=куда» команды (и «--remap»): {откуда: куда}. Как в rcl, действует первое
    подходящее правило, следующие для той же темы — нет [опыт, rclpy Jazzy: «-r /cmd_vel:=/cmd_vel -r
    /cmd_vel:=/neurobot/cmd_vel_req» — издатель остался на /cmd_vel]. Правило для другого узла («узел:откуда:=куда») не
    в счёт; относительное имя — от корня (узлы компонентов — в корневом пространстве имён)."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return {}
    out, ros = {}, False
    for i, a in enumerate(argv):
        if a in ("--ros-args", "--"):
            ros = a == "--ros-args"
        elif ros and a in ("-r", "--remap") and i + 1 < len(argv):
            left, sep, dst = argv[i + 1].partition(":=")
            who, _, src = left.rpartition(":")
            if sep and src and dst and not src.startswith(("_", "~")) and who in ("", node):
                out.setdefault("/" + src.lstrip("/"), "/" + dst.lstrip("/"))
    return out


def check_remap(component: str, cfg: Config) -> List[Problem]:
    """Исполнитель v1 (страж) и распознавание в режиме legacy пишут абсолютное «/cmd_vel» сами, в сторону его уводит
    только ремап в команде env-файла: без него это второй писатель /cmd_vel с приоритетом 10 в twist_mux мимо
    safety_gate (S4). Узлу, который /cmd_vel не пишет (исполнитель v2, распознавание gesture), этот ремап ничего не
    меняет — поэтому он нужен всегда. Исполнитель уходит во вход safety_gate, распознавание — во вход жестов
    исполнителя (любая тема вне цепочки движения)."""
    upper = component.upper()
    key, topic, req = "NEUROBOT_{}_CMD".format(upper), cfg.get("NEUROBOT_CMD_VEL"), cfg.get("NEUROBOT_CMD_VEL_REQ")
    dst = remaps(cfg.get(key), cfg.get("NEUROBOT_{}_NODE".format(upper))).get(topic)
    if component == "executor" and dst != req:
        return [Problem("config_invalid", "{}: {} — нужен «--ros-args -r {}:={}»: страж v1 пишет {} сам, без ремапа "
                        "это второй писатель мимо safety_gate (S4)".format(
                            key, "ремап {} в {}".format(topic, dst) if dst else "нет ремапа " + topic, topic, req,
                            topic))]
    if component == "perception" and (dst is None or dst in _motion_inputs(cfg) + [req]):
        return [Problem("config_invalid", "{}: {} — нужен «--ros-args -r {}:=/cmd_vel_gesture» (вход жестов "
                        "исполнителя): DriveController legacy пишет {} сам, иначе это писатель мимо исполнителя и "
                        "safety_gate (S4)".format(
                            key, "ремап {} в {}".format(topic, dst) if dst else "нет ремапа " + topic, topic, topic))]
    return []


def own_topics(cfg: Config) -> List[str]:
    """Темы, которые wait-graph смотрит после READY исполнителя или распознавания."""
    return _motion_inputs(cfg) + [cfg.get("NEUROBOT_CMD_VEL_REQ")]


def own_problems(component: str, cfg: Config, graph: Dict[str, Dict[str, List[str]]]) -> List[Problem]:
    """Граф после READY исполнителя или распознавания (ExecStartPost wait-graph): их узел не пишет в /cmd_vel и прочие
    входы цепочки движения (распознавание — и во вход safety_gate). Ремап мог потеряться и при верной строке команды
    (обёртка, launch-файл, свой разбор аргументов), а после рестарта safety_gate такой писатель ведёт робот без него.
    Исполнитель обязан писать во вход safety_gate — иначе жесты и агент никуда не доходят; если же он пишет мимо
    safety_gate, это следствие той же причины, и строка одна: doctor берёт последнюю строку отказа запуска."""
    upper = component.upper()
    node, key, req = cfg.get("NEUROBOT_{}_NODE".format(upper)), "NEUROBOT_{}_CMD".format(upper), cfg.get(
        "NEUROBOT_CMD_VEL_REQ")
    out = [Problem("cmd_vel_writer", "узел {} пишет в {} — мимо safety_gate (S4): ремап {} не сработал ({})".format(
        node, t, cfg.get("NEUROBOT_CMD_VEL"), key)) for t in own_topics(cfg)
        if node in _names(graph, t) and not (component == "executor" and t == req)]
    if component == "executor" and not out and node not in _names(graph, req):
        out.append(Problem("config_invalid", "узел {} не пишет в {}: жесты и команды агента не дойдут до safety_gate "
                           "({})".format(node, req, key)))
    return out


def _cmd_sha256(component: str, cfg: Config) -> str:
    return hashlib.sha256(cfg.get("NEUROBOT_{}_CMD".format(component.upper())).encode("utf-8")).hexdigest()


def mark_writer(component: str, cfg: Config, state: str, problems: Sequence[Problem], now: float) -> str:
    """wait-graph застал узел компонента писателем цепочки движения: метка в state (StateDirectory юнита) с хешем
    команды и причинами — по ней precheck не запускает компонент снова с той же командой (check_writer_mark).
    Запись атомарная; путь метки."""
    path = os.path.join(state, WRITER_MARK)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"cmd_sha256": _cmd_sha256(component, cfg), "t": round(now, 3),
                   "reasons": [p.text for p in problems if p.code == "cmd_vel_writer"]}, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    return path


def check_writer_mark(component: str, cfg: Config, state: str) -> List[Problem]:
    """Метка wait-graph (mark_writer) при той же команде — отказ: иначе после каждого рестарта узел снова пишет мимо
    safety_gate, пока wait-graph его не поймает [опыт, ВМ: 37 % времени, по 1,2–1,5 с из каждых ≈3,6 с]. Команда
    изменилась — метка снимается, старт разрешён (ремап так и не работает — wait-graph поставит её снова). Причину вне
    команды (обёртка, launch-файл) после исправления снимает человек, удалив метку. Метка не читается или не
    удаляется — тоже отказ."""
    path = os.path.join(state, WRITER_MARK) if state else ""
    if not path or not os.path.exists(path):
        return []
    key = "NEUROBOT_{}_CMD".format(component.upper())
    try:
        with open(path, encoding="utf-8") as f:
            mark = json.load(f)
    except (OSError, ValueError):
        mark = {}
    mark = mark if isinstance(mark, dict) else {}
    if mark.get("cmd_sha256") and mark["cmd_sha256"] != _cmd_sha256(component, cfg):
        try:
            os.remove(path)
            return []
        except OSError:
            pass
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mark["t"])) if isinstance(
        mark.get("t"), (int, float)) else "время неизвестно"
    return [Problem("cmd_vel_writer", "метка wait-graph ({}): {}; {} с тех пор не менялась — не запускаю, иначе узел "
                    "снова пишет мимо safety_gate до проверки графа. Исправить ремап в команде; если причина вне "
                    "команды (обёртка, launch-файл) и устранена — sudo rm {}".format(
                        when, "; ".join(mark.get("reasons") or ["узел писал в цепочку движения мимо safety_gate"]),
                        key, path))]


def camera_mode(cfg: Config) -> str:
    """Камера распознавания по его команде: «ros» — кадры темы realsense2_camera, «rs» — своя (по умолчанию)."""
    try:
        argv = shlex.split(cfg.get("NEUROBOT_PERCEPTION_CMD"))
    except ValueError:
        return "rs"
    for i, a in enumerate(argv):
        if a == "--ros-args":
            break
        if a.startswith("--camera="):
            return a.split("=", 1)[1]
        if a == "--camera" and i + 1 < len(argv):
            return argv[i + 1]
    return "rs"


# --- конфиги MCP и PicoClaw ---------------------------------------------------------------------------------------

def _load_json(path: str, what: str) -> Tuple[Optional[dict], List[Problem]]:
    problems = check_json(path, what)
    if problems or not path:
        return None, problems
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return None, [Problem("config_invalid", "{} {}: ожидался объект JSON".format(what, path))]
    return data, []


def _int(value, default: int) -> int:
    """Число из JSON; 0 и отсутствие — умолчание PicoClaw (omitempty), мусор — -1 (не пройдёт проверку)."""
    if value in (None, 0, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _get(data: dict, *keys, default=None):
    for k in keys:
        data = data.get(k) if isinstance(data, dict) else None
    return default if data is None else data


def _over(value, limit: float) -> bool:
    try:
        return float(value) > limit
    except (TypeError, ValueError):
        return True


def check_proxy_env(cfg: Config) -> List[Problem]:
    """Прокси лаборатории с патчем neurobot берёт настройки из env-файла (ROBOT_VISION_*): порт — тот, что в
    NEUROBOT_MCP_URL; длительность толчка в секундах ограничена пределом агента (L3), поэтому миллисекунды, принятые за
    секунды (N1), не увезут робота. Единицы speed у воркера 8766 неизвестны — скорость здесь не сравнивается."""
    out = []
    port = cfg.get("ROBOT_VISION_MCP_PORT")
    if port and port != str(cfg.mcp_endpoint()[1]):
        out.append(Problem("config_invalid", "ROBOT_VISION_MCP_PORT={} не совпадает с портом NEUROBOT_MCP_URL "
                           "{}".format(port, cfg.get("NEUROBOT_MCP_URL"))))
    value = cfg.get("ROBOT_VISION_MAX_DURATION_S")
    limit = cfg.num("NEUROBOT_AGENT_MAX_DURATION_S")
    if value and _over(value, limit):
        out.append(Problem("config_invalid", "ROBOT_VISION_MAX_DURATION_S={} больше предела агента "
                           "NEUROBOT_AGENT_MAX_DURATION_S={:g} с (L3, N1)".format(value, limit)))
    return out


def _launch_args(command: str) -> Dict[str, List[str]]:
    """Аргументы «имя:=значение» команды ros2 launch: {имя: [значения по порядку]}. Повтор не теряется: ros2 launch
    берёт последнее значение [опыт, Jazzy], поэтому проверка смотрит все."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return {}
    out: Dict[str, List[str]] = {}
    for a in argv:
        name, sep, value = a.partition(":=")
        if sep:
            out.setdefault(name, []).append(value)
    return out


def check_supervised(key: str, cfg: Config) -> List[Problem]:
    """ros2 launch без обёртки deploy (launchfiles/supervised.launch.py) переживает смерть своего узла: юнит active, а
    узла нет, и systemd его не поднимет [замер main_a1: rosbridge после kill -9 узла — до конца прогона].
    Предупреждение: у «ros2 run» и одного процесса такой беды нет."""
    try:
        argv = shlex.split(cfg.get(key))
    except ValueError:
        return []
    if argv[:2] != ["ros2", "launch"] or (len(argv) > 2 and argv[2].endswith(SUPERVISED)):
        return []
    return [Problem("unsupervised_launch", "{}: «{}» без обёртки {} — если узел внутри launch умрёт, юнит останется "
                    "active без узла; запускать через ros2 launch {}/share/launchfiles/{} include:=…".format(
                        key, " ".join(argv[:4]), SUPERVISED, cfg.get("NEUROBOT_PREFIX"), SUPERVISED), fatal=False)]


def check_rosbridge(cfg: Config) -> List[Problem]:
    """Команда rosbridge — второй рубеж L3 для любого клиента WebSocket (MCP-сервер ограничивает себя сам,
    check_mcp_config). Аргументы ros2 launch (rosbridge 1.3.1 и 2.x), каждый ровно один раз:
    - address:=127.0.0.1 — по умолчанию адрес пуст, и rosbridge слушает все интерфейсы, то есть сеть лаборатории;
    - topics_glob:="'[/cmd_vel_agent]'" — только тема агента (и копия состояния safety_gate для агента
      NEUROBOT_AGENT_STATE_TOPIC, если задана): тема из списка открыта клиенту и на запись (раздельных списков
      подписки и публикации в rosbridge 1.3.1 нет), а без глоба открыты все — /cmd_vel, вход safety_gate, защёлка,
      датчики safety_gate и состояние, которое читают исполнитель, doctor и neurobot estop. Копию читает только
      MCP-сервер: подменить её агент может лишь себе. В rosbridge 2.x (Jazzy) есть и раздельные topics_pub_glob и
      topics_sub_glob, они объединяются с topics_glob: topics_pub_glob — тот же список разрешённого, topics_sub_glob
      (только подписка, там — /neurobot/safety_state) — любой; оба необязательны, в 1.3.1 их нет;
    - services_glob:="'[/rosapi/topics]'" — только /rosapi/…: их rosbridge при любом services_glob пропускает сам, а
      без глоба открыт любой сервис (set_parameters safety_gate, контроллера базы, twist_mux, драйверов датчиков,
      /controller_manager/*);
    - params_glob:="'[none]'" — ни одного параметра: /rosapi/set_param и delete_param меняют параметры любого узла.
    Глобы — списки разрешённого: любой другой элемент (тема, сервис, маска) — отказ. Пустой список «[]» в rosbridge
    1.3.1 — «без ограничений»; список без кавычек YAML ros2 launch передаст массивом строк, и rosbridge не стартует.
    Обратная косая черта в значении любого *_glob — отказ: launch_ros разбирает значение как YAML, и в строке в
    двойных кавычках «\x2c» станет запятой — лишний элемент, которого разбор здесь не видит [опыт W1c, Jazzy]."""
    command = cfg.get("NEUROBOT_ROSBRIDGE_CMD")
    if not command:
        return []
    gate, agent, estop, req = (cfg.get("NEUROBOT_SAFETY_NODE"), cfg.get("NEUROBOT_AGENT_TOPIC"),
                               cfg.get("NEUROBOT_ESTOP_TOPIC"), cfg.get("NEUROBOT_CMD_VEL_REQ"))
    base = cfg.get("NEUROBOT_BASE_CMD_TOPIC").strip("/").split("/")
    nodes = [gate, cfg.get("NEUROBOT_EXECUTOR_NODE"), cfg.get("NEUROBOT_MUX_NODE"), base[0] if len(base) > 1 else "",
             "controller_manager", "<любой узел>"]
    # имена, на которых видно, что открывает лишний элемент списка (сам отказ — по списку разрешённого)
    topics = _motion_inputs(cfg) + [req, estop, cfg.get("NEUROBOT_SAFETY_STATE_TOPIC"),
                                    cfg.get("NEUROBOT_MOTION_STATE_TOPIC"), cfg.get("NEUROBOT_HEALTH_TOPIC"),
                                    cfg.get("NEUROBOT_SCAN_TOPIC")] + [t for t, _, _ in cfg.sensors()]
    services = (["/{}/{}".format(n, s) for n in nodes if n for s in ("set_parameters", "set_parameters_atomically")]
                + ["/controller_manager/switch_controller", "/controller_manager/unload_controller", estop])
    mirror = cfg.get("NEUROBOT_AGENT_STATE_TOPIC")
    agent_topics = {agent, mirror} - {""}
    # аргумент: (разрешён ли элемент, что разрешено, что открыто без глоба, имена для текста, пример значения)
    topic_rule = (lambda g: g in agent_topics, "только тема агента " + agent + (
                      " и копия состояния для агента " + mirror if mirror else ""),
                  "запись в любые темы, в том числе {}, {} и {}".format(cfg.get("NEUROBOT_CMD_VEL"), req, estop),
                  topics, agent)
    rules = {"topics_glob": topic_rule, "topics_pub_glob": topic_rule,
             "topics_sub_glob": (lambda g: True, "", "", [], agent),
             "services_glob": (lambda g: g.startswith("/rosapi/"), "только /rosapi/… (их rosbridge пропускает сам)",
                               "вызов любых сервисов: set_parameters у {}, контроллера базы и twist_mux, "
                               "/controller_manager/*".format(gate), services, "/rosapi/topics"),
             "params_glob": (lambda g: g == "none", "только none — ни одного параметра",
                             "/rosapi/set_param для любого параметра любого узла (пороги {}, cmd_vel_timeout "
                             "контроллера базы)".format(gate), [], "none")}
    given = _launch_args(command)
    out = [Problem("config_invalid", "NEUROBOT_ROSBRIDGE_CMD: {} задан {} раза — ros2 launch возьмёт последнее "
                   "значение ({}); оставить один".format(arg, len(given[arg]), given[arg][-1]))
           for arg in ("address",) + tuple(rules) if len(given.get(arg, [])) > 1]
    out += [Problem("config_invalid", "{}:={} — обратная косая черта в глобе: launch_ros разбирает значение как YAML, "
                    "и экранирование («\\x2c» — запятая) добавит элемент мимо списка разрешённого (L3)".format(
                        arg, value))
            for arg, values in given.items() if arg.endswith("_glob") for value in values if "\\" in value]
    address = given.get("address")
    if not address:
        out.append(Problem("config_invalid", "NEUROBOT_ROSBRIDGE_CMD без address:=127.0.0.1: по умолчанию адрес пуст, "
                           "и rosbridge слушает все интерфейсы — любой клиент из сети лаборатории пишет в {} и "
                           "зовёт /rosapi/* (L3)".format(agent)))
    elif len(address) == 1 and address[0] not in LOOPBACK:
        out.append(Problem("config_invalid", "address:={} — rosbridge открыт сети; нужен address:=127.0.0.1 "
                           "(L3)".format(address[0])))
    for arg, (allowed, what_ok, what_open, names, example) in rules.items():
        values = given.get(arg, [])
        if len(values) > 1 or (arg in ROSBRIDGE_GLOBS_2X and not values):
            continue
        text = values[0].strip() if values else ""
        quoted = len(text) > 2 and text[0] == text[-1] and text[0] in "'\"" and text[1] == "[" and text[-2] == "]"
        globs = [g for g in (g.strip().strip("'\"") for g in text[2:-2].split(",")) if g] if quoted else []
        if not globs and (quoted or text.strip("'\" ") in ("", "[]")):
            if arg not in ROSBRIDGE_GLOBS_2X:  # пустой topics_pub_glob/topics_sub_glob в 2.x — это topics_glob
                out.append(Problem("config_invalid", "NEUROBOT_ROSBRIDGE_CMD без {} (пустой список для rosbridge "
                                   "1.3.1 — «без ограничений»): любому клиенту rosbridge открыт {} (L3)".format(
                                       arg, what_open)))
            continue
        if not quoted:
            out.append(Problem("config_invalid", "{}:={} — нужен список в кавычках YAML, например {}:=\"'[{}]'\": "
                               "иначе ros2 launch передаст массив строк, и rosbridge не стартует".format(
                                   arg, text, arg, example)))
            continue
        extra = [g for g in globs if not allowed(g)]
        if extra:
            opened = sorted({n for n in names for g in extra if fnmatch.fnmatchcase(n, g)})
            out.append(Problem("config_invalid", "{} {}: лишнее {} — разрешено {}{} (L3)".format(
                arg, text, ", ".join(extra), what_ok, "; открывает агенту " + ", ".join(opened) if opened else "")))
    return out


def check_mcp_config(cfg: Config) -> List[Problem]:
    """Конфиг ros_mcp_server лаборатории с патчем neurobot: HTTP только на 127.0.0.1, агент пишет только во вход
    исполнителя (не в /cmd_vel, не в /neurobot/cmd_vel_req, не в защёлку), пределы движения не шире L3."""
    path = cfg.get("NEUROBOT_MCP_CONFIG")
    data, problems = _load_json(path, "конфиг MCP")
    if data is None:
        return problems
    _, port, url_path = cfg.mcp_endpoint()
    server = data.get("server") or {}
    bad = sorted(set(server) - set(MCP_SERVER_KEYS))
    if bad:
        problems.append(Problem("config_invalid", "{}: server.{} — неизвестные ключи (допустимы: {})".format(
            path, ", ".join(bad), ", ".join(MCP_SERVER_KEYS))))
    if server.get("host", "127.0.0.1") not in LOOPBACK:
        problems.append(Problem("config_invalid", "{}: server.host={!r} — MCP-сервер должен слушать только "
                                "127.0.0.1".format(path, server.get("host"))))
    if _int(server.get("port"), 18110) != port or server.get("streamable_http_path", "/mcp") != url_path:
        problems.append(Problem("config_invalid", "{}: server {}:{}{} не совпадает с NEUROBOT_MCP_URL {}".format(
            path, server.get("host", "127.0.0.1"), server.get("port", 18110),
            server.get("streamable_http_path", "/mcp"), cfg.get("NEUROBOT_MCP_URL"))))
    agent = cfg.get("NEUROBOT_AGENT_TOPIC")
    guarded = {cfg.get("NEUROBOT_CMD_VEL"): "мимо исполнителя и safety_gate (второй писатель, S4)",
               cfg.get("NEUROBOT_CMD_VEL_REQ"): "мимо исполнителя",
               cfg.get("NEUROBOT_ESTOP_TOPIC"): "— агент мог бы снять аварийную защёлку"}
    topics = data.get("topics") or {}
    topic = (topics.get("cmd_vel") or {}).get("name", "/cmd_vel")
    if topic in guarded:
        problems.append(Problem("config_invalid", "{}: topics.cmd_vel.name={} — агент писал бы {}; нужно {}".format(
            path, topic, guarded[topic], agent)))
    elif topic != agent:
        problems.append(Problem("config_invalid", "{}: topics.cmd_vel.name={}, а исполнитель слушает {}".format(
            path, topic, agent)))
    writable = sorted(str(t.get("name")) for k, t in topics.items() if k != "cmd_vel" and isinstance(t, dict)
                      and t.get("access", "read") in ("write", "read_write"))
    if writable:
        problems.append(Problem("config_invalid", "{}: агенту открыта запись в {} — пишет он только {}".format(
            path, ", ".join(writable), agent)))
    state = (topics.get("safety_state") or {}).get("name")
    known = [t for t in (cfg.get("NEUROBOT_SAFETY_STATE_TOPIC"), cfg.get("NEUROBOT_AGENT_STATE_TOPIC")) if t]
    if not state:
        problems.append(Problem("config_invalid", "{}: нет topics.safety_state — robot_get_status не скажет агенту о "
                                "защёлке и блоке, robot_nudge ответит ok и там, где safety_gate не пустил".format(path),
                                fatal=False))
    elif state not in known:
        problems.append(Problem("config_invalid", "{}: topics.safety_state.name={} — состояние safety_gate идёт в "
                                "{}".format(path, state, " и ".join(known)), fatal=False))
    if not (data.get("safety") or {}).get("reject_unknown_topics", True):
        problems.append(Problem("config_invalid", "{}: safety.reject_unknown_topics=false — агент сможет писать в "
                                "любые темы, в том числе /cmd_vel и {}".format(path, cfg.get("NEUROBOT_ESTOP_TOPIC"))))
    motion = data.get("motion") or {}
    limits = (("max_linear_speed", 0.08, cfg.num("NEUROBOT_AGENT_MAX_SPEED"), "м/с", 1.0),
              ("max_angular_speed", 0.4, cfg.num("NEUROBOT_AGENT_MAX_TURN"), "рад/с", 1.0),
              ("max_duration_ms", 500, cfg.num("NEUROBOT_AGENT_MAX_DURATION_S"), "с", 1000.0))
    for key, default, limit, unit, scale in limits:
        value = motion.get(key, default)
        if _over(value, limit * scale):
            problems.append(Problem("config_invalid", "{}: motion.{}={} шире предела агента {:g} {} (L3)".format(
                path, key, value, limit, unit)))
    rb = urlsplit((data.get("rosbridge") or {}).get("url", "ws://127.0.0.1:9090"))
    if rb.port != cfg.port("NEUROBOT_ROSBRIDGE_PORT"):
        problems.append(Problem("config_invalid", "{}: rosbridge.url порт {} ≠ NEUROBOT_ROSBRIDGE_PORT {}".format(
            path, rb.port, cfg.get("NEUROBOT_ROSBRIDGE_PORT"))))
    tools = data.get("enabled_tools") or {}
    for name in ("camera_capture_frame_to_file", "lidar_capture_scan_to_file"):
        if tools.get(name):
            problems.append(Problem("config_invalid", "{}: {} включён, но в присланной версии не работает "
                                    "(read_one_message нет, M1)".format(path, name), fatal=False))
    return problems


def check_picoclaw_config(cfg: Config) -> List[Problem]:
    """config.json PicoClaw: поля — по схеме установленной версии (иначе шлюз не стартует, N11); журнал на info
    (иначе готовность и зависания не видны, N15); MCP только по HTTP к нашему серверу; сервер робота по stdio (имя из
    оверлея или скрипт из NEUROBOT_FOREIGN_MCP) — отказ: второй набор инструментов движения; shell для LLM (exec,
    cron с командами) — отказ; остальное — предупреждения (PLAN §4)."""
    path = cfg.picoclaw_config()
    data, problems = _load_json(path, "конфиг PicoClaw")
    if data is None:
        return problems
    version, source = picoclaw_cfg.detect_version(cfg.get("NEUROBOT_PICOCLAW_VERSION"),
                                                  cfg.get("NEUROBOT_PICOCLAW_CMD"), data)
    schema, exact = picoclaw_cfg.load_schema(version)
    if exact:
        unknown = picoclaw_cfg.unknown_fields(data, schema)
        if unknown:
            problems.append(Problem("config_invalid", "{}: PicoClaw {} ({}) не стартует с неизвестными полями: {} "
                                    "(N11)".format(path, version, source, ", ".join(unknown))))
    else:
        problems.append(Problem("config_invalid", "{}: схемы полей для PicoClaw {} нет (есть: {}) — неизвестные поля "
                                "не проверены".format(path, version or "неизвестной версии",
                                                      ", ".join(picoclaw_cfg.known_versions())), fatal=False))
    level = str(_get(data, "gateway", "log_level", default="") or "warn").lower()
    if level not in PICOCLAW_LOG_OK:
        problems.append(Problem("config_invalid", "{}: gateway.log_level={} — строк «Connected to MCP server» и "
                                "вызовов инструментов в журнале нет: готовность не дождаться, сторож слеп; нужно "
                                "info (N15)".format(path, level)))
    _, port, url_path = cfg.mcp_endpoint()
    if not _get(data, "tools", "mcp", "enabled", default=False):
        problems.append(Problem("config_invalid", "{}: tools.mcp.enabled=false — агент без робота".format(path)))
    servers = {k: v for k, v in (_get(data, "tools", "mcp", "servers", default={}) or {}).items()
               if isinstance(v, dict) and v.get("enabled")}
    robot_scripts = cfg.get("NEUROBOT_FOREIGN_MCP").split()
    robot_names = picoclaw_cfg.lab_servers(picoclaw_cfg.load_overlay())
    ours = []
    for name, s in sorted(servers.items()):
        kind = s.get("type") or ("stdio" if s.get("command") else "sse")
        u = urlsplit(s.get("url", ""))
        if kind in ("http", "streamable-http") and u.hostname in LOOPBACK and (u.port, u.path or "/") == (
                port, url_path):
            ours.append(name)
            if kind == "streamable-http" and _vt(version) < (0, 3, 1):
                problems.append(Problem("config_invalid", "{}: MCP-сервер {}: type streamable-http в PicoClaw {} не "
                                        "поддержан — нужен http (N11)".format(path, name, version or "?")))
        elif kind == "stdio":
            argv = [s.get("command", "")] + list(s.get("args") or [])
            robot = name in robot_names or any(os.path.basename(a) in robot_scripts for a in argv)
            problems.append(Problem("config_invalid", "{}: MCP-сервер {} по stdio: PicoClaw не поднимает его после "
                                    "падения, рвёт по первой строке в stdout и убивает при /reload (PC1–PC3){}".format(
                                        path, name, "; это сервер робота — второй набор инструментов движения и "
                                        "второй писатель скоростей" if robot else ""), fatal=robot))
    if not ours:
        problems.append(Problem("config_invalid", "{}: нет включённого MCP-сервера type=http с url={}".format(
            path, cfg.get("NEUROBOT_MCP_URL"))))
    warn = []
    if _get(data, "gateway", "hot_reload", default=False):
        warn.append("gateway.hot_reload=true (перезагрузка конфига убивает stdio-серверы, PC3)")
    defaults = _get(data, "agents", "defaults", default={}) or {}
    known = (_get(schema, "agents", "defaults", default={}) or {}) if schema else {}
    if not 0 < _int(defaults.get("max_tokens"), 32768) <= 2048:
        warn.append("agents.defaults.max_tokens={} (рекомендуется 1024, PC8)".format(defaults.get("max_tokens")))
    if _int(defaults.get("max_parallel_turns"), 1) < 2:
        warn.append("agents.defaults.max_parallel_turns<2: зависший ход глушит все чаты (PC4)")
    if "max_llm_retries" in known and _int(defaults.get("max_llm_retries"), 2) > 1:
        warn.append("agents.defaults.max_llm_retries>1 (рекомендуется 1, PC8)")
    if not 0 < _int(defaults.get("max_tool_iterations"), 50) <= 8:
        warn.append("agents.defaults.max_tool_iterations={} (рекомендуется 6–8, N4)".format(
            defaults.get("max_tool_iterations")))
    models = data.get("model_list") or []
    if any(not 0 < _int((m or {}).get("request_timeout"), 120) <= 30 for m in models):
        warn.append("model_list[].request_timeout не задан или > 30 с (рекомендуется 20, PC8)")
    if _get(data, "tools", "exec", "enabled", default=True):
        problems.append(Problem("config_invalid", "{}: tools.exec включён — у LLM shell на роботе: пишет /cmd_vel "
                                "мимо исполнителя, снимает аварийную защёлку, ставит пакеты (S4, N5)".format(path)))
    cron_shell = (_get(data, "tools", "cron", "enabled", default=True)
                  and _get(data, "tools", "cron", "allow_command", default=True))
    if cron_shell:
        problems.append(Problem("config_invalid", "{}: tools.cron с командами (allow_command) — тот же shell для LLM "
                                "по расписанию".format(path)))
    tools = (_get(schema, "tools", default={}) or {}) if schema else {}
    on = [t for t in PICOCLAW_TOOLS_OFF if (not tools or t in tools) and not (t == "cron" and cron_shell)
          and _get(data, "tools", t, "enabled", default=t not in PICOCLAW_DEFAULT_OFF)]
    if on:
        warn.append("включены лишние встроенные инструменты: {} (PC7)".format(", ".join(on)))
    problems += [Problem("config_invalid", "{}: {}".format(path, w), fatal=False) for w in warn]
    return problems


# --- сборка по компонентам ----------------------------------------------------------------------------------------

def precheck(component: str, cfg: Config, proc: str = "/proc",
             graph: Optional[Callable[[Config, List[str]], Dict[str, Dict[str, List[str]]]]] = None,
             state: str = "") -> List[Problem]:
    """Проверки компонента. graph(cfg, темы) → {тема: {"pub": [...], "sub": [...]}} — граф ROS 2 (rclpy),
    подставляется вызывающим; смотрится только после статических проверок (без команды и конфига незачем поднимать
    rclpy); не снялся (GraphUnavailable) — отказ ros_unavailable. state — StateDirectory юнита: там метка wait-graph
    исполнителя и распознавания (check_writer_mark)."""
    try:
        if component.startswith("driver@"):
            name = component.split("@", 1)[1]
            usb = cfg.driver(name, "USB")
            return (check_env(cfg, ["NEUROBOT_DRIVER_{}_CMD".format(name)])
                    + check_supervised("NEUROBOT_DRIVER_{}_CMD".format(name), cfg)
                    + check_device(cfg.driver(name, "DEVICE"))
                    + (check_camera(cfg.get("NEUROBOT_SYSFS"), usb) if usb else []))
        if component.startswith("labworker@"):
            name = component.split("@", 1)[1]
            out = check_env(cfg, ["NEUROBOT_LABWORKER_{}_{}".format(name, f) for f in ("CMD", "URL")])
            modules = "NEUROBOT_LABWORKER_{}_MODULES".format(name)
            return out or (check_port_free(cfg.labworker_url(name)[1], proc)
                           + check_python(cfg.labworker(name, "CMD"), modules, cfg))
        if component in ("safety", "executor"):
            what = {"safety": "safety_gate", "executor": "исполнителя"}[component]
            key = component.upper()
            out = (check_env(cfg, ["NEUROBOT_{}_CMD".format(key)])
                   + check_json(cfg.get("NEUROBOT_{}_CONFIG".format(key)), "конфиг " + what))
            if not out:
                out += (check_python(cfg.get("NEUROBOT_SAFETY_CMD"), "NEUROBOT_SAFETY_MODULES", cfg)
                        if component == "safety" else check_remap(component, cfg)
                        + check_writer_mark(component, cfg, state))
            if graph and not any(p.fatal for p in out):
                try:
                    out += graph_problems(component, cfg, graph(cfg, graph_topics(component, cfg)))
                except GraphUnavailable as e:
                    out.append(Problem("ros_unavailable", str(e)))
            return out
        if component == "perception":
            # при --camera ros устройством владеет узел камеры в юните драйвера, распознавание читает его тему
            supervised = ("neurobot-labworker@",) + (("neurobot-driver@",) if camera_mode(cfg) == "ros" else ())
            out = check_env(cfg, ["NEUROBOT_PERCEPTION_CMD"])
            if not out:
                out += check_remap(component, cfg) + check_writer_mark(component, cfg, state)
            return (out + check_json(cfg.get("NEUROBOT_PERCEPTION_CONFIG"), "конфиг распознавания")
                    + check_camera(cfg.get("NEUROBOT_SYSFS"), cfg.get("NEUROBOT_CAMERA_USB"))
                    + check_weights(cfg.get("NEUROBOT_WEIGHTS"))
                    + check_foreign(cfg.get("NEUROBOT_FOREIGN_PERCEPTION").split(), proc, supervised=supervised)
                    + check_python(cfg.get("NEUROBOT_PERCEPTION_CMD"), "NEUROBOT_PERCEPTION_MODULES", cfg))
        if component == "rosbridge":
            return (check_env(cfg, ["NEUROBOT_ROSBRIDGE_CMD"]) + check_rosbridge(cfg)
                    + check_supervised("NEUROBOT_ROSBRIDGE_CMD", cfg)
                    + check_port_free(cfg.port("NEUROBOT_ROSBRIDGE_PORT"), proc))
        if component == "mcp":
            return (check_env(cfg, ["NEUROBOT_MCP_CMD"]) + check_port_free(cfg.mcp_endpoint()[1], proc)
                    + check_mcp_config(cfg) + check_proxy_env(cfg)
                    + check_foreign(cfg.get("NEUROBOT_FOREIGN_MCP").split(), proc)
                    + check_python(cfg.get("NEUROBOT_MCP_CMD"), "NEUROBOT_MCP_MODULES", cfg))
        if component == "picoclaw":
            return (check_env(cfg, ["NEUROBOT_PICOCLAW_CMD"])
                    + check_port_free(cfg.port("NEUROBOT_PICOCLAW_PORT"), proc) + check_picoclaw_config(cfg))
    except EnvError as e:
        return [Problem("env_invalid", str(e))]
    return [Problem("env_invalid", "неизвестный компонент {!r}".format(component))]
