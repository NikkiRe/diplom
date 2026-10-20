r"""Единый env-файл neurobot (/etc/neurobot/neurobot.env): разбор по правилам EnvironmentFile= и умолчания.

Тот же файл читают юниты (EnvironmentFile=), install.sh (через render) и наши инструменты. Поддержано
подмножество синтаксиса systemd: «ИМЯ=значение», значение в "…" (экранирование \\ \" \$ \`) или в '…',
комментарии — строки с # или ;. Подстановок $VAR нет ни у systemd, ни здесь.

Приоритет: NEUROBOT_* и PICOCLAW_* из окружения процесса перекрывают файл (так start_test подменяет
значения drop-in-файлами юнитов); ROS_* берутся из файла — проверки обязаны смотреть в тот же DDS-домен,
что и юниты, даже если в терминале выставлен другой.
"""
from __future__ import annotations

import os
import pwd
import re
from typing import Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

ENV_PATH = "/etc/neurobot/neurobot.env"
# Порядок запуска по готовности (PLAN §5.4, §13.3): драйверы (лидар, база с twist_mux) → safety_gate (единственный
# писатель /cmd_vel) → исполнитель (пишет /neurobot/cmd_vel_req) → распознавание → rosbridge → воркеры лаборатории
# (прокси лаборатории ходит в них по HTTP) → MCP-сервер → агент → пульт оператора.
ORDER = ("safety", "executor", "perception", "rosbridge", "mcp", "picoclaw", "ui")
# не в составе по умолчанию: пульт ставится по согласию лаборатории (свой пользователь, порт на 127.0.0.1)
OPTIONAL = ("ui",)
ROS_KEYS = ("ROS_DOMAIN_ID", "ROS_LOCALHOST_ONLY", "ROS_AUTOMATIC_DISCOVERY_RANGE", "RMW_IMPLEMENTATION",
            "CYCLONEDDS_URI", "FASTRTPS_DEFAULT_PROFILES_FILE")
DEFAULTS = {
    "NEUROBOT_USER": "jetson",
    "NEUROBOT_GROUP": "jetson",
    "NEUROBOT_SUPPLEMENTARY_GROUPS": "",
    "NEUROBOT_PREFIX": "/opt/neurobot",
    "NEUROBOT_COMPONENTS": " ".join(c for c in ORDER if c not in OPTIONAL),
    "NEUROBOT_DRIVERS": "",
    "NEUROBOT_LABWORKERS": "",
    "NEUROBOT_ROS_SETUP": "/opt/ros/foxy/setup.bash",
    "NEUROBOT_ROS_OVERLAY": "",
    # цепочка движения: исполнитель → NEUROBOT_CMD_VEL_REQ (Twist) → safety_gate → NEUROBOT_CMD_VEL → twist_mux
    # (узел NEUROBOT_MUX_NODE) → NEUROBOT_BASE_CMD_TOPIC → diff_drive_controller
    "NEUROBOT_CMD_VEL": "/cmd_vel",
    "NEUROBOT_CMD_VEL_TYPE": "geometry_msgs/msg/Twist",
    "NEUROBOT_CMD_VEL_REQ": "/neurobot/cmd_vel_req",
    "NEUROBOT_AGENT_TOPIC": "/cmd_vel_agent",
    "NEUROBOT_MUX_NODE": "twist_mux",
    "NEUROBOT_BASE_CMD_TOPIC": "/diff_cont/cmd_vel_unstamped",
    # входы twist_mux с приоритетом выше /cmd_vel (articubot_one: tracker 20 > cmd_vel 10): любой их издатель
    # перекрывает явный ноль safety_gate (N13); геймпад (100) — законный перехват человеком, только сообщаем
    "NEUROBOT_OVERRIDE_TOPICS": "/cmd_vel_tracker",
    "NEUROBOT_JOY_TOPICS": "/cmd_vel_joy",
    "NEUROBOT_SCAN_TOPIC": "/scan",
    "NEUROBOT_SCAN_FRAME": "laser_frame",
    # прочие датчики для doctor: «тема:тип[:мин_Гц]» через пробел (/scan проверяется отдельно и строже)
    "NEUROBOT_SENSORS": "",
    "NEUROBOT_HEALTH_TOPIC": "/neurobot/perception_health",
    "NEUROBOT_MOTION_STATE_TOPIC": "/neurobot/motion_state",
    "NEUROBOT_SAFETY_STATE_TOPIC": "/neurobot/safety_state",
    "NEUROBOT_ESTOP_TOPIC": "/neurobot/estop",
    # стоп агента (robot_stop через MCP, решение 1а): safety_gate ставит ноль сразу, без защёлки; тема открыта агенту
    # в rosbridge — она умеет только остановить; "" — нет (robot_stop — штатная плавная остановка)
    "NEUROBOT_STOP_TOPIC": "/neurobot/stop_now",
    # снятие защёлки: release.json root в каталоге root (тот же release_dir у safety_gate); по теме — нельзя
    "NEUROBOT_ESTOP_RELEASE_DIR": "/run/neurobot-estop",
    # копия состояния safety_gate для агента (agent_state_topic): rosbridge 1.3.1 (Foxy) не умеет открыть тему только
    # на чтение, а /neurobot/safety_state читают исполнитель и doctor; пусто — копии нет
    "NEUROBOT_AGENT_STATE_TOPIC": "",
    "NEUROBOT_SAFETY_NODE": "safety_gate",
    "NEUROBOT_EXECUTOR_NODE": "motion_executor",
    # узел predict_live (имя — из кода лаборатории): его писателей проверяет wait-graph после READY
    "NEUROBOT_PERCEPTION_NODE": "velocity_publisher",
    "NEUROBOT_SAFETY_CONFIG": "",
    "NEUROBOT_EXECUTOR_CONFIG": "",
    "NEUROBOT_PERCEPTION_CONFIG": "",
    "NEUROBOT_SAFETY_MODULES": "numpy<2 rclpy",
    # пределы инструментов движения агента (L3, PLAN §13.3): сверяются с конфигом MCP-сервера и патча прокси
    "NEUROBOT_AGENT_MAX_SPEED": "0.15",
    "NEUROBOT_AGENT_MAX_TURN": "0.8",
    "NEUROBOT_AGENT_MAX_DURATION_S": "1.0",
    # модули интерпретатора распознавания (find_spec, без импорта); «numpy<2» — ещё и версия (N5, N6)
    "NEUROBOT_PERCEPTION_MODULES": "numpy<2 torch snntorch pyrealsense2 cv2 rclpy",
    # то же для MCP-сервера: FastMCP требует Python ≥ 3.10 (M13), streamable HTTP — mcp ≥ 1.8
    "NEUROBOT_MCP_MODULES": "python>=3.10 mcp>=1.8",
    "NEUROBOT_CAMERA_USB": "8086:0b3a",
    "NEUROBOT_SYSFS": "/sys",
    "NEUROBOT_WEIGHTS": "/etc/neurobot/weights.sha256",
    "NEUROBOT_ROSBRIDGE_PORT": "9090",
    "NEUROBOT_MCP_URL": "http://127.0.0.1:18110/mcp",
    "NEUROBOT_MCP_CONFIG": "",
    "NEUROBOT_MCP_STATUS_TOOL": "robot_get_status",
    "NEUROBOT_PICOCLAW_PORT": "18790",
    # версия PicoClaw для проверки полей конфига; пусто — «<бинарник> version», затем build_info в config.json
    "NEUROBOT_PICOCLAW_VERSION": "",
    "NEUROBOT_WAIT_S": "30",
    "NEUROBOT_LABWORKER_WAIT_S": "170",
    "NEUROBOT_WRITER_CHECK_S": "0.75",
    "NEUROBOT_PRECHECK_BACKOFF_S": "5",
    # частые рестарты распознавания (backoff.py): паузы 1-го, 2-го, 3-го… рестарта серии (последняя повторяется) и
    # сброс серии, если прошлый запуск был раньше, с
    "NEUROBOT_RESTART_PAUSES_S": "0 5 15 30",
    "NEUROBOT_RESTART_RESET_S": "600",
    "NEUROBOT_WATCHDOG_PERIOD_S": "10",
    # частые пробы сторожа: свежесть тем драйверов (NEUROBOT_DRIVER_<имя>_FRESH) и ping rosbridge
    "NEUROBOT_WATCHDOG_FAST_S": "1",
    "NEUROBOT_WATCHDOG_STALE_S": "2",
    # Чужие процессы (имя программы или файла-аргумента). Камера — один владелец на устройство: демо, просмотрщик
    # и ROS-узел камеры вне наших юнитов (S4, N12; при --camera ros узел камеры в юните драйвера — свой); YOLO-воркер
    # лаборатории вне юнита — второй контекст CUDA (≈1 ГиБ) на Nano с 4 ГБ. Сервер робота и воркеры лаборатории вне
    # наших юнитов — запуск из PicoClaw по stdio или nohup (PC1–PC3, N7, N8).
    "NEUROBOT_FOREIGN_PERCEPTION": "predict_live.py live_stream_yolo.py realsense-viewer realsense2_camera_node "
                                   "yolo_hand_worker.py run_yolo_mcp_stack.sh",
    "NEUROBOT_FOREIGN_MCP": "ros_mcp_server.py start_robot_mcp.sh robot_vision_mcp_proxy.py yolo_mcp_server.py "
                            "run_yolo_mcp_stack.sh yolo_hand_worker.py",
    "NEUROBOT_WATCHDOG_PROBE_TOOL": "",
    # пульт оператора (neurobot/ui, docs/ui/PLAN.md §0а): свой пользователь — агент (NEUROBOT_USER) токен входа не
    # прочитает; узел не neurobot_* (так CLI зовёт свои служебные узлы — проверки писателей их пропускают)
    "NEUROBOT_UI_CMD": "",
    "NEUROBOT_UI_USER": "neurobot-ui",
    "NEUROBOT_UI_NODE": "ui_gateway",
    "NEUROBOT_UI_PORT": "18120",
    "NEUROBOT_UI_MODULES": "rclpy",
    # подпись источника на каждом экране: робот | двойник (числа стенда не должны сойти за числа робота)
    "NEUROBOT_UI_SOURCE": "робот",
    "NEUROBOT_UI_STATE_DIR": "/var/lib/neurobot/ui",
    # бандл «Записи» рядом с живым режимом (переключатель «Живой» / «Запись»); пусто — только живой
    "NEUROBOT_UI_BUNDLE": "",
    # doctor по кнопке пульта не чаще раза в столько секунд; 0 — выключен (во время замеров)
    "NEUROBOT_UI_DOCTOR_S": "60",
    # компоненты не робота (у двойника — Gazebo в driver@twin): в память стека против бюджета не идут
    "NEUROBOT_UI_NOT_ROBOT": "",
}
_LINE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)")
_SENSOR = re.compile(r"(/[\w/]+):(\w+/msg/\w+)(?::([0-9.]+))?")
_INSTANCE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_UNQUOTE = re.compile(r'\\(["\\$`])')


class EnvError(ValueError):
    """Env-файл или значение в нём не годятся; текст — для человека."""


def parse_env(text: str, name: str = "env") -> Dict[str, str]:
    out = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        m = _LINE.fullmatch(line)
        if not m:
            raise EnvError("{}:{}: ожидалось ИМЯ=значение, получено {!r}".format(name, n, raw))
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = _UNQUOTE.sub(r"\1", value[1:-1])
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        out[key] = value
    return out


class Config:
    def __init__(self, values: Mapping[str, str], path: str):
        self.values = dict(values)
        self.path = path

    def get(self, key: str) -> str:
        return self.values.get(key, DEFAULTS.get(key, ""))

    def num(self, key: str) -> float:
        try:
            return float(self.get(key))
        except ValueError:
            raise EnvError("{}: ожидалось число, получено {!r}".format(key, self.get(key)))

    def nums(self, key: str) -> List[float]:
        """Числа ≥ 0 через пробел."""
        try:
            out = [float(x) for x in self.get(key).split()]
        except ValueError:
            out = [-1.0]
        if any(not v >= 0 for v in out):
            raise EnvError("{}: ожидались числа ≥ 0 через пробел, получено {!r}".format(key, self.get(key)))
        return out

    def port(self, key: str) -> int:
        v = self.num(key)
        if v != int(v) or not 0 < v < 65536:
            raise EnvError("{}: ожидался порт 1–65535, получено {!r}".format(key, self.get(key)))
        return int(v)

    def components(self) -> List[str]:
        comps = self.get("NEUROBOT_COMPONENTS").split()
        bad = [c for c in comps if c not in ORDER]
        if bad:
            raise EnvError("NEUROBOT_COMPONENTS: неизвестные {} (допустимы: {})".format(bad, " ".join(ORDER)))
        return [c for c in ORDER if c in comps]

    def instances(self, kind: str) -> List[str]:
        """Имена экземпляров шаблона: kind — DRIVER или LABWORKER (NEUROBOT_DRIVERS, NEUROBOT_LABWORKERS)."""
        key = "NEUROBOT_{}S".format(kind)
        names = self.get(key).split()
        bad = [d for d in names if not _INSTANCE.fullmatch(d)]
        if bad:
            raise EnvError("{}: имя экземпляра — [a-z][a-z0-9_]* (до 32 знаков), получено {}".format(key, bad))
        return names

    def sensors(self) -> List[Tuple[str, str, float]]:
        """[(тема, тип, мин. частота Гц; 0 — только наличие)] из NEUROBOT_SENSORS («/odom:nav_msgs/msg/Odometry:20»)."""
        out = []
        for token in self.get("NEUROBOT_SENSORS").split():
            m = _SENSOR.fullmatch(token)
            if not m:
                raise EnvError("NEUROBOT_SENSORS: ожидалось «/тема:пакет/msg/Тип[:мин_Гц]», получено {!r}".format(
                    token))
            out.append((m.group(1), m.group(2), float(m.group(3) or 0)))
        return out

    def drivers(self) -> List[str]:
        return self.instances("DRIVER")

    def labworkers(self) -> List[str]:
        return self.instances("LABWORKER")

    def driver(self, name: str, field: str) -> str:
        return self.get("NEUROBOT_DRIVER_{}_{}".format(name, field))

    def labworker(self, name: str, field: str) -> str:
        return self.get("NEUROBOT_LABWORKER_{}_{}".format(name, field))

    def labworker_url(self, name: str) -> Tuple[str, int, str]:
        """(хост, порт, путь) пробы готовности воркера NEUROBOT_LABWORKER_<имя>_URL."""
        return _http_url("NEUROBOT_LABWORKER_{}_URL".format(name), self.labworker(name, "URL"))

    def plan(self) -> List[Tuple[str, str]]:
        """(компонент, юнит) в порядке запуска: драйверы, safety_gate, исполнитель, распознавание, rosbridge,
        воркеры, MCP, агент."""
        head = [("driver@" + d, "neurobot-driver@{}.service".format(d)) for d in self.drivers()]
        workers = [("labworker@" + w, "neurobot-labworker@{}.service".format(w)) for w in self.labworkers()]
        comps = [(c, "neurobot-{}.service".format(c)) for c in self.components()]
        split = next((i for i, (c, _) in enumerate(comps) if c in ("mcp", "picoclaw")), len(comps))
        return head + comps[:split] + workers + comps[split:]

    def mcp_endpoint(self) -> Tuple[str, int, str]:
        return _http_url("NEUROBOT_MCP_URL", self.get("NEUROBOT_MCP_URL"))

    def picoclaw_home(self) -> str:
        """Каталог PicoClaw (журналы, сессии): PICOCLAW_HOME или ~/.picoclaw пользователя компонентов."""
        if self.get("PICOCLAW_HOME"):
            return self.get("PICOCLAW_HOME")
        try:
            return os.path.join(pwd.getpwnam(self.get("NEUROBOT_USER")).pw_dir, ".picoclaw")
        except KeyError:
            return os.path.expanduser("~/.picoclaw")

    def picoclaw_config(self) -> str:
        return self.get("PICOCLAW_CONFIG") or os.path.join(self.picoclaw_home(), "config.json")

    def ros_environ(self) -> Dict[str, str]:
        return {k: self.values[k] for k in ROS_KEYS if k in self.values}


def quote(value: str) -> str:
    """Значение для env-файла в "…": systemd и bash прочитают его одинаково (parse_env — обратное)."""
    return '"{}"'.format(re.sub(r'(["\\$`])', r"\\\1", value))


def _http_url(key: str, value: str) -> Tuple[str, int, str]:
    u = urlsplit(value)
    try:
        port = u.port
    except ValueError:
        port = None
    if u.scheme != "http" or not u.hostname or not port:
        raise EnvError("{}: ожидалось http://хост:порт/путь, получено {!r}".format(key, value))
    return u.hostname, port, u.path or "/"


def load(path: Optional[str] = None, environ: Mapping[str, str] = os.environ) -> Config:
    path = path or environ.get("NEUROBOT_ENV") or ENV_PATH
    try:
        with open(path, encoding="utf-8") as f:
            values = parse_env(f.read(), path)
    except OSError as e:
        raise EnvError("env-файл {}: {}".format(path, e.strerror or e))
    for key, value in environ.items():
        if key.startswith(("NEUROBOT_", "PICOCLAW_")) and key != "NEUROBOT_ENV":
            values[key] = value
    return Config(values, path)
