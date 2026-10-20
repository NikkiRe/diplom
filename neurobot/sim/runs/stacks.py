"""(ВМ sim, root) Две системы прогона на двойнике (README §2): A0 — стек лаборатории как на роботе, A1 — пакет 1 +
безопасность v2 (neurobot.target). Общий интерфейс для runner.py: подготовка, подъём, остановка, процесс по роли
(для отказов), «роль снова работает» (для TTR), действия человека-оператора.

Роли отказов (schedule.FAULT_CYCLE): perception, rosbridge, mcp, agent, executor, safety; драйверы
(schedule.DRIVER_CYCLE): lidar — узел /urg_node2, camera — узел /camera/camera (юниты neurobot-driver@lidar и @camera,
как на роботе), base — сервер Gazebo юнита neurobot-driver@twin (в нём gz_ros2_control: controller_manager и
diff_drive_controller; на роботе на этом месте ros2_control с мостом STM32). У A0 нет исполнителя и safety_gate
(/cmd_vel пишет демо) — такие отказы пропускаются с пометкой; драйверы A0 — внутри launch двойника, их отказы на нём
не прогонялись. Пути ВМ — BASE; код — снимок SRC.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import time
from typing import Dict, List, Optional, Sequence

BASE = "/var/tmp/nb-runs"
SRC = os.path.join(BASE, "src")
TW = "/var/lib/neurobot-twin"
ENV_FILE = "/etc/neurobot/neurobot.env"
WALL_REGEX = "wall|table|chair|person|box"
FOOTPRINT_R = 0.195
LAB_DEMO = "third_party/neurochip/demo/demo-ros2_feature/src/camera/predict_live.py"
LAB_SNN = "third_party/neurochip/demo/demo-ros2_feature/models/snn_emnist_small.pth"
LAB_RP = "third_party/neurochip/robotic_platform/robotic_platform"
PICO_TOKEN_FILE = "pico_token"
CAMERA_RELAY_TOPIC = "/runs/camera/color/image_raw"
# узлы внутри ros2 launch — регулярные выражения по командной строке (re.search). Сервер rosbridge (у самого launch в
# строке — rosbridge_websocket_launch.xml): отказ «упал узел»
ROSBRIDGE_NODE = r"lib/rosbridge_server/rosbridge_websocket"
# узел датчиков двойника в launch драйвера (sensor.launch.py): отказ «упал или завис узел»
SENSOR_NODE = r"sensor_model\.py"
# сервер Gazebo базы двойника; его обёртка «/bin/sh -c ruby … gz sim» — не он
BASE_NODE = r"^gz sim "
# миры прогона: файл (путь в снимке) и поза спавна — та, куда оператор переносит застрявшего робота
WORLDS = {"hall": ("neurobot/sim/runs/worlds/hall.sdf", (0.0, 0.0, 0.01, 0.0)),
          "lab": ("neurobot/sim/twin/worlds/lab.sdf", (-1.5, 0.0, 0.01, 0.0)),
          # отложенный набор (PROTOCOL §4): зеркало hall, только для итогового прогона
          "hall_m": ("neurobot/sim/runs/worlds/hall_m.sdf", (0.0, 0.0, 0.01, 0.0))}


def log(msg: str) -> None:
    print("[runs] " + msg, file=sys.stderr, flush=True)


def sh(argv: Sequence[str], check: bool = True, timeout: float = 120.0, **kw) -> subprocess.CompletedProcess:
    r = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, **kw)
    if check and r.returncode:
        raise RuntimeError("%s: код %d: %s" % (" ".join(argv[:4]), r.returncode, (r.stderr or r.stdout).strip()[-400:]))
    return r


def unit_prop(unit: str, prop: str) -> str:
    return sh(["systemctl", "show", "-p", prop, "--value", unit], check=False).stdout.strip()


def unit_active(unit: str) -> bool:
    return sh(["systemctl", "is-active", unit], check=False).stdout.strip() == "active"


def cgroup_pids(unit: str) -> List[int]:
    cg = unit_prop(unit, "ControlGroup")
    if not cg:
        return []
    try:
        with open("/sys/fs/cgroup%s/cgroup.procs" % cg) as f:
            return [int(p) for p in f.read().split()]
    except OSError:
        return []


def cmdline(pid: int) -> str:
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def find_pid(unit: str, pattern: str) -> int:
    """PID процесса юнита, командная строка которого подходит под pattern (re.search); 0 — нет такого."""
    for pid in sorted(cgroup_pids(unit)):
        if re.search(pattern, cmdline(pid)):
            return pid
    return 0


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def wait(pred, timeout: float, step: float = 0.5) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def sudo_user() -> str:
    user = os.environ.get("SUDO_USER", "")
    if not user or user == "root":
        raise RuntimeError("нужен запуск через sudo от обычного пользователя (SUDO_USER)")
    return user


def deploy_lib():
    """Модули CLI deploy из снимка: пробы WebSocket и MCP, разбор env-файла."""
    path = os.path.join(SRC, "neurobot", "deploy")
    if path not in sys.path:
        sys.path.insert(0, path)
    from neurobot_deploy import config, probes
    return config, probes


def pico_config(base: dict, *, port: int, api_base: str, workspace: Optional[str] = None) -> dict:
    """Конфиг PicoClaw с каналом pico (WebSocket /pico/ws шлюза, как у лаборатории) и LLM — bench/mockllm."""
    cfg = json.loads(json.dumps(base))
    if workspace:
        cfg["agents"]["defaults"]["workspace"] = workspace
    cfg.setdefault("gateway", {})["port"] = port
    name = cfg["agents"]["defaults"]["model_name"]
    for m in cfg["model_list"]:
        if m["model_name"] == name:
            m["api_base"] = api_base
    pico = cfg.setdefault("channel_list", {}).setdefault("pico", {"type": "pico"})
    pico["enabled"] = True
    return cfg


def lab_picoclaw(lab: dict, *, home: str, api_base: str, proxy_python: str, proxy: str) -> dict:
    """Конфиг PicoClaw 0.2.8 с робота (R7a §1.1) с минимальными правками стенда: LLM — mockllm вместо cloud.ru, VK
    выключен (нет сети и ключа), рабочий каталог и пути прокси — ВМ. Остальное как у них: log_level warn,
    max_tool_iterations 50, exec и cron включены, MCP — их stdio-прокси robot_vision_demo_mcp."""
    cfg = pico_config(lab, port=lab["gateway"]["port"], api_base=api_base, workspace=os.path.join(home, "workspace"))
    cfg["channel_list"]["vk"]["enabled"] = False
    srv = cfg["tools"]["mcp"]["servers"]["robot_vision_demo_mcp"]
    srv["command"], srv["args"] = proxy_python, [proxy]
    return cfg


def security_yml(token: str, model_key: str = "") -> str:
    """.security.yml PicoClaw 0.2.8: токен канала pico (тестовый, этого прогона) и ключ LLM-заглушки."""
    out = "channel_list:\n  pico:\n    settings:\n      token: %s\n" % token
    if model_key:
        out += "model_list:\n  %s:0:\n    api_keys:\n      - mock-not-a-key\n" % model_key
    return out


class Stack:
    """Общее у A0 и A1. Наследник задаёт роли, подъём и проверки."""

    name = ""
    domain = 0
    gateway_port = 0
    probe_path = ""
    anchor_path = ""
    agent_tools: Dict[str, List[dict]] = {}
    observer_extra: List[str] = []

    def __init__(self, run_dir: str, sched: str, mockllm: str):
        self.run_dir, self.sched, self.mockllm = run_dir, sched, mockllm
        os.makedirs(os.path.join(run_dir, "logs"), exist_ok=True)

    @property
    def token(self) -> str:
        with open(os.path.join(self.run_dir, PICO_TOKEN_FILE)) as f:
            return f.read().strip()

    def new_token(self) -> str:
        token = os.urandom(16).hex()
        path = os.path.join(self.run_dir, PICO_TOKEN_FILE)
        with open(path, "w") as f:
            f.write(token + "\n")
        os.chmod(path, 0o600)
        return token

    def ros_env(self) -> Dict[str, str]:
        return {"ROS_DOMAIN_ID": str(self.domain), "ROS_AUTOMATIC_DISCOVERY_RANGE": "LOCALHOST",
                "RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp",
                "CYCLONEDDS_URI": "file://%s/scripts/sim/cyclonedds.xml" % SRC,
                "ROS_LOG_DIR": os.path.join(self.run_dir, "logs", "ros")}

    def pid(self, role: str) -> int:
        raise NotImplementedError

    def has(self, role: str) -> bool:
        return True

    def skip_reason(self, role: str) -> str:
        return "у системы нет компонента"

    def up(self, role: str) -> bool:
        raise NotImplementedError

    def units(self) -> Dict[str, str]:
        """{компонент: юнит} для сэмплера памяти (neurobot/sim/budget/collect.py)."""
        raise NotImplementedError

    def restarts(self) -> Dict[str, int]:
        return {k: int(unit_prop(u, "NRestarts") or 0) for k, u in self.units().items()}

    def write_units_json(self) -> str:
        """units.json сэмплера памяти: компоненты системы; драйвер двойника (Gazebo) — тоже, для сверки нагрузки."""
        doc = {"slice": "/sys/fs/cgroup/system.slice",
               "units": {k: {"unit": u, "role": "measured",
                             "cgroup": "/sys/fs/cgroup" + (unit_prop(u, "ControlGroup") or "/system.slice/" + u)}
                         for k, u in self.units().items()}}
        path = os.path.join(self.run_dir, "units.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        return path

    def session_dir(self) -> str:
        raise NotImplementedError


class A0(Stack):
    """Стек лаборатории как на роботе (A0): демо predict_live.py как есть пишет /cmd_vel прямо в twist_mux двойника;
    rosbridge, PicoClaw 0.2.8 с их конфигом, их stdio-прокси и заглушка воркера 8766 — каждый процесс без супервизии
    (на роботе — терминалы и nohup, R7a N16): упавшее поднимает только человек (оператор runner.py)."""

    name = "a0"
    domain = 61
    rosbridge_port = 9161
    worker_port = 8766
    gateway_port = 18790
    agent_tools = {"status": [{"name": "robot_demo_get_status", "args": {}}],
                   # так модель вызвала инструмент на роботе 29.09 (R7a §0, N1): первая регистрация дубля
                   "move": [{"name": "robot_nudge_forward", "args": {"duration_ms": 300, "speed": 0.8}}]}
    UNITS = {"twin": "a0-twin", "rosbridge": "a0-rosbridge", "worker": "a0-worker", "demo": "a0-demo",
             "picoclaw": "a0-picoclaw"}
    ROLE = {"perception": ("demo", r"a0_demo\.py"), "rosbridge": ("rosbridge", ROSBRIDGE_NODE),
            "mcp": ("picoclaw", r"robot_vision_mcp_proxy\.py"), "agent": ("picoclaw", "picoclaw")}

    def __init__(self, run_dir: str, sched: str, mockllm: str, ml_python: str, picoclaw: str, mcp_venv: str):
        super().__init__(run_dir, sched, mockllm)
        self.user = sudo_user()
        self.ml_python, self.picoclaw_bin, self.mcp_venv = ml_python, picoclaw, mcp_venv
        self.anchor_path = os.path.join(run_dir, "anchor.json")
        self.probe_path = os.path.join(run_dir, "probe.jsonl")
        self.twin_dir = os.path.join(run_dir, "twin_a0")
        self.home = os.path.join(run_dir, "picoclaw")
        self.observer_extra = ["--extra-cmd", "/diff_cont/cmd_vel_unstamped", "--probe", self.probe_path]

    def units(self) -> Dict[str, str]:
        return {k: self.UNITS[k] for k in ("rosbridge", "worker", "demo", "picoclaw")}

    def has(self, role: str) -> bool:
        return role in self.ROLE

    def skip_reason(self, role: str) -> str:
        if role in ("lidar", "camera", "base"):
            return "драйверы A0 — внутри launch двойника (как одним launch у лаборатории): отказ драйвера не прогонялся"
        return "у системы нет компонента"

    def session_dir(self) -> str:
        return os.path.join(self.home, "workspace", "sessions")

    # ---- подготовка

    def prepare(self) -> None:
        """Остатки прошлого прогона остановлены; каталоги прогона, ссылка /home/jetson (пути демо), копии прокси и
        конфига PicoClaw лаборатории."""
        self.stop()
        jet = os.path.join(BASE, "jetson")
        models = os.path.join(jet, "NeuroChip", "demo", "models")
        os.makedirs(models, exist_ok=True)
        shutil.copy(os.path.join(SRC, LAB_SNN), models)
        if os.path.islink("/home/jetson") and os.readlink("/home/jetson") != jet:
            os.remove("/home/jetson")
        if not os.path.lexists("/home/jetson"):
            os.symlink(jet, "/home/jetson")
        if not os.path.samefile("/home/jetson", jet):
            raise RuntimeError("/home/jetson есть и это не ссылка стенда — не трогаю")
        os.makedirs(os.path.join(self.home, "workspace"), exist_ok=True)
        lab_dir = os.path.join(self.run_dir, "lab")
        os.makedirs(lab_dir, exist_ok=True)
        proxy = os.path.join(lab_dir, "robot_vision_mcp_proxy.py")
        shutil.copy(os.path.join(SRC, LAB_RP, "robot_vision_mcp_proxy.py"), proxy)
        with open(os.path.join(SRC, LAB_RP, ".picoclaw", "config.json"), encoding="utf-8") as f:
            lab = json.load(f)
        cfg = lab_picoclaw(lab, home=self.home, api_base=self.mockllm + "/v1",
                           proxy_python=os.path.join(self.mcp_venv, "bin", "python3"), proxy=proxy)
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        with open(os.path.join(self.home, ".security.yml"), "w", encoding="utf-8") as f:
            f.write(security_yml(self.new_token(), cfg["agents"]["defaults"]["model_name"]))
        for p in (self.anchor_path, self.probe_path):
            if os.path.exists(p):
                os.remove(p)
        sh(["chown", "-R", self.user, self.run_dir, jet])

    # ---- процессы

    def _run(self, key: str, argv: List[str], env: Dict[str, str], kill: str = "SIGTERM") -> None:
        """Процесс как transient-юнит без рестарта: на роботе его запускают руками, поднимает его только человек."""
        unit = self.UNITS[key]
        sh(["systemctl", "reset-failed", unit], check=False)
        logf = os.path.join(self.run_dir, "logs", key + ".log")
        cmd = ["systemd-run", "--unit", unit, "--uid", self.user, "--collect", "--quiet", "--no-block",
               "--working-directory", self.run_dir, "-p", "Restart=no", "-p", "KillSignal=" + kill,
               "-p", "TimeoutStopSec=20", "-p", "StandardOutput=append:" + logf, "-p", "StandardError=append:" + logf]
        for k, v in sorted(dict(env, PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1",
                                HOME=pwd.getpwnam(self.user).pw_dir).items()):
            cmd.append("--setenv=%s=%s" % (k, v))
        sh(cmd + ["--"] + argv)

    def _ros(self, argv: List[str]) -> List[str]:
        return ["/bin/bash", os.path.join(SRC, "neurobot", "sim", "budget", "run_ros.sh")] + argv

    def start_twin(self) -> None:
        os.makedirs(self.twin_dir, exist_ok=True)
        sh(["chown", self.user, self.twin_dir])
        world = os.path.join(SRC, "neurobot", "sim", "runs", "worlds", "hall.sdf")
        self._run("twin", self._ros(["ros2", "launch", os.path.join(SRC, "neurobot/sim/twin/launch/twin.launch.py"),
                                     "run_dir:=" + self.twin_dir, "world:=" + world, "rosbridge:=false"]),
                  self.ros_env(), kill="SIGINT")
        # twin_ready читает controllers.yaml прогона — его пишет launch
        if not wait(lambda: os.path.exists(os.path.join(self.twin_dir, "controllers.yaml")), 60):
            raise RuntimeError("двойник A0: launch не записал controllers.yaml за 60 с")
        ready = sh(["sudo", "-u", self.user, "env"] + ["%s=%s" % kv for kv in self.ros_env().items()] +
                   self._ros(["python3", os.path.join(SRC, "neurobot/sim/twin/twin_ready.py"), "--timeout", "150",
                              "--run-dir", self.twin_dir]), check=False, timeout=200)
        if ready.returncode:
            raise RuntimeError("двойник A0 не готов: %s %s" % (ready.stdout[-600:], ready.stderr[-400:]))

    def start_demo(self) -> None:
        env = dict(self.ros_env(), PYTHONPATH="%s:%s/neurobot/sim/gestures/shim" % (SRC, SRC),
                   NEUROBOT_GESTURES_TIMELINE=os.path.join(self.sched, "timeline.jsonl"),
                   NEUROBOT_GESTURES_ANCHOR=self.anchor_path, NEUROBOT_FAKES_PROBE=self.probe_path,
                   NEUROBOT_FAKE_RS_FAULTS=os.path.join(self.sched, "faults.json"))
        self._run("demo", ["xvfb-run", "-a", "-s", "-screen 0 1280x720x24"] +
                  self._ros([self.ml_python, os.path.join(SRC, "neurobot/sim/runs/a0_demo.py"),
                             "--demo", os.path.join(SRC, LAB_DEMO)]), env, kill="SIGINT")

    def start_rosbridge(self) -> None:
        # как ros2 launch на роботе; address:=127.0.0.1 — правило стенда (наружу ВМ не открываем)
        self._run("rosbridge", self._ros(["ros2", "launch", "rosbridge_server", "rosbridge_websocket_launch.xml",
                                          "address:=127.0.0.1", "port:=%d" % self.rosbridge_port]),
                  self.ros_env(), kill="SIGINT")

    def start_worker(self) -> None:
        self._run("worker", ["python3", os.path.join(SRC, "neurobot/sim/runs/worker8766.py"), "--port",
                             str(self.worker_port), "--log", os.path.join(self.run_dir, "worker8766.jsonl")], {})

    def start_picoclaw(self) -> None:
        self._run("picoclaw", [self.picoclaw_bin, "--no-color", "gateway"], {"PICOCLAW_HOME": self.home})

    def start(self) -> None:
        self.start_twin()
        self.start_rosbridge()
        self.start_worker()
        if not wait(lambda: port_open(self.worker_port) and port_open(self.rosbridge_port), 60):
            raise RuntimeError("A0: rosbridge или заглушка 8766 не слушают порт")
        self.start_demo()
        if not wait(lambda: os.path.exists(self.anchor_path), 180):
            raise RuntimeError("A0: демо не вызвало детектор за 180 с (якоря нет)")
        self.start_picoclaw()
        if not wait(lambda: port_open(self.gateway_port), 60):
            raise RuntimeError("A0: шлюз PicoClaw не слушает порт")

    def stop(self) -> None:
        for key in ("picoclaw", "demo", "worker", "rosbridge", "twin"):
            sh(["systemctl", "stop", self.UNITS[key]], check=False, timeout=60)
            sh(["systemctl", "reset-failed", self.UNITS[key]], check=False)

    def pid(self, role: str) -> int:
        key, pattern = self.ROLE[role]
        if role == "agent":
            pid = int(unit_prop(self.UNITS[key], "MainPID") or 0)
            return pid if pid > 0 else 0
        return find_pid(self.UNITS[key], pattern)

    def up(self, role: str) -> bool:
        if role == "rosbridge":
            return port_open(self.rosbridge_port)
        if role in ("mcp", "agent"):
            return self.pid(role) > 0 and port_open(self.gateway_port)
        return self.pid(role) > 0

    def restart(self, key: str) -> None:
        """Человек перезапускает процесс: остановить остаток юнита (xvfb-run, ros2 launch) и запустить снова."""
        sh(["systemctl", "stop", self.UNITS[key]], check=False, timeout=60)
        {"demo": self.start_demo, "picoclaw": self.start_picoclaw, "rosbridge": self.start_rosbridge}[key]()


class A1(Stack):
    """Пакет 1 + безопасность v2 (A1): neurobot.target из neurobot/deploy на двойнике (профиль
    neurobot/sim/suites/twin.env), мир hall, кадры камеры — через узел отказов relay.py."""

    name = "a1"
    domain = 23
    gateway_port = 18793
    agent_tools = {"status": [{"name": "robot_get_status", "args": {}}],
                   "move": [{"name": "robot_nudge", "args": {"direction": "forward", "duration_ms": 1000,
                                                             "speed": 0.15}}]}
    UNITS = {"perception": "neurobot-perception", "rosbridge": "neurobot-rosbridge", "mcp": "neurobot-mcp",
             "agent": "neurobot-picoclaw", "executor": "neurobot-executor", "safety": "neurobot-safety",
             "lidar": "neurobot-driver@lidar", "camera": "neurobot-driver@camera", "base": "neurobot-driver@twin"}
    DRIVERS = ("lidar", "camera", "base")
    RELAY = "runs-camrelay"

    def __init__(self, run_dir: str, sched: str, mockllm: str, world: str = "hall", no_return: str = "nan"):
        super().__init__(run_dir, sched, mockllm)
        self.world, self.no_return = world, no_return
        self.anchor_path = os.path.join(TW, "gestures", "anchor.json")
        self.probe_path = os.path.join(TW, "gestures", "probe.jsonl")
        self.observer_extra = ["--extra-cmd", "/neurobot/cmd_vel_req", "--extra-cmd", "/diff_cont/cmd_vel_unstamped",
                               "--probe", self.probe_path]

    def units(self) -> Dict[str, str]:
        """Юниты пакета; драйверы двойника (Gazebo и база, узлы лидара и камеры) — с суффиксом _driver: на роботе там
        urg_node2, realsense2_camera и мост STM32, их память и рестарты к пакету не относятся."""
        out = {k: u for k, u in self.UNITS.items() if k not in self.DRIVERS}
        out.update(watchdog="neurobot-watchdog", twin_driver=self.UNITS["base"], lidar_driver=self.UNITS["lidar"],
                   camera_driver=self.UNITS["camera"])
        return out

    def session_dir(self) -> str:
        return os.path.join(TW, "picoclaw", "workspace", "sessions")

    @property
    def token(self) -> str:
        """Токен канала pico — из .security.yml PicoClaw пакета (его пишет install)."""
        with open(os.path.join(TW, "picoclaw", ".security.yml"), encoding="utf-8") as f:
            m = re.search(r"^\s*token:\s*(\S+)\s*$", f.read(), re.M)
        if not m:
            raise RuntimeError("в .security.yml PicoClaw пакета нет токена pico")
        return m.group(1)

    # ---- подготовка

    @staticmethod
    def installed() -> bool:
        return os.path.exists(ENV_FILE)

    @staticmethod
    def env_edit(text: str, world: str, spawn: tuple, probe: str, no_return: str = "nan") -> str:
        """Правки env-файла пакета для прогона (README §2.2), повторное применение ничего не меняет: мир (hall, lab),
        поза спавна (x, y, z, курс; мир путём к .sdf launch двойника ставит в начало координат, а в мире lab там низкий
        ящик) и цвет 10 к/с у драйвера двойника, «нет отражения» у лидара (no_return), кадры распознавания — с узла
        отказов, журнал подставных модулей (det_calls FakeYolo); прокси лаборатории убран из чужих MCP — в той же ВМ
        он работает у A0 (другой домен DDS и шлюз)."""
        def sub(key: str, fn) -> None:
            nonlocal text
            m = re.search(r'^%s="(.*)"$' % key, text, re.M)
            if not m:
                raise ValueError("в env-файле нет %s" % key)
            text = text[:m.start(1)] + fn(m.group(1)) + text[m.end(1):]
        remap = "-r /camera/color/image_raw:=%s" % CAMERA_RELAY_TOPIC
        pose = " ".join("%s:=%g" % kv for kv in zip(("x_pose", "y_pose", "z_pose", "yaw"), spawn))
        sub("NEUROBOT_DRIVER_twin_CMD", lambda v: re.sub(r"world:=\S+", "world:=%s %s color_hz:=10" % (world, pose),
                                                         re.sub(r" (color_hz|x_pose|y_pose|z_pose|yaw):=\S+", "", v)))
        sub("NEUROBOT_DRIVER_lidar_CMD", lambda v: re.sub(r"no_return:=\S+", "no_return:=" + no_return, v))
        sub("NEUROBOT_PERCEPTION_CMD",
            lambda v: v if remap in v else v.replace("--ros-args", "--ros-args " + remap, 1))
        sub("NEUROBOT_FOREIGN_MCP", lambda v: " ".join(w for w in v.split() if w != "robot_vision_mcp_proxy.py"))
        text = re.sub(r"^NEUROBOT_FAKES_PROBE=.*\n", "", text, flags=re.M)
        return text.rstrip("\n") + "\nNEUROBOT_FAKES_PROBE=%s\n" % probe

    def apply_env(self) -> None:
        world, spawn = WORLDS[self.world]
        with open(ENV_FILE, encoding="utf-8") as f:
            text = f.read()
        new = self.env_edit(text, os.path.join(TW, "src", world), spawn, self.probe_path, self.no_return)
        if new != text:
            with open(ENV_FILE, "w", encoding="utf-8") as f:
                f.write(new)

    def install(self) -> None:
        """setup_twin.sh снимка, затем правки прогона: env-файл, конфиг PicoClaw с каналом pico и mockllm."""
        if self.installed():
            raise RuntimeError("пакет уже установлен (%s): сначала teardown" % ENV_FILE)
        sh(["bash", os.path.join(SRC, "neurobot/sim/suites/setup_twin.sh"), "--domain", str(self.domain)],
           timeout=900)
        self.apply_env()
        with open(os.path.join(SRC, "neurobot/sim/suites/picoclaw.json"), encoding="utf-8") as f:
            cfg = pico_config(json.load(f), port=self.gateway_port, api_base=self.mockllm + "/v1")
        home = os.path.join(TW, "picoclaw")
        with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        with open(os.path.join(home, ".security.yml"), "w", encoding="utf-8") as f:
            f.write(security_yml(self.new_token()))
        sh(["chown", "-R", "neurobot:neurobot", home])
        sh(["chmod", "600", os.path.join(home, "config.json"), os.path.join(home, ".security.yml")])
        sh(["neurobot", "picoclaw-config", "merge"])
        sh(["systemctl", "daemon-reload"])

    @staticmethod
    def teardown() -> None:
        sh(["systemctl", "stop", A1.RELAY], check=False)
        sh(["bash", os.path.join(SRC, "neurobot/sim/suites/setup_twin.sh"), "--teardown"], check=False, timeout=600)

    def point_llm(self) -> None:
        """Конфиг PicoClaw пакета — на mockllm этого прогона (адрес мог смениться после install), с merge оверлея."""
        path = os.path.join(TW, "picoclaw", "config.json")
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        new = pico_config(cfg, port=self.gateway_port, api_base=self.mockllm + "/v1")
        if new == cfg:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(new, f, ensure_ascii=False, indent=2)
        sh(["chown", "neurobot:neurobot", path])
        sh(["chmod", "600", path])
        sh(["neurobot", "picoclaw-config", "merge"])

    def prepare(self) -> None:
        """Пакет остановлен; PicoClaw — на mockllm прогона (если задан); таймлайн прогона на место таймлайна пакета,
        новый якорь и журнал probe; узел отказов камеры."""
        self.stop()
        self.apply_env()
        if self.mockllm:
            self.point_llm()
        gdir = os.path.join(TW, "gestures")
        for name in ("timeline.jsonl", "truth.jsonl"):
            shutil.copy(os.path.join(self.sched, name), os.path.join(gdir, name))
        for p in (self.anchor_path, self.probe_path):
            if os.path.exists(p):
                os.remove(p)
        sh(["chown", "-R", "neurobot:neurobot", gdir])
        sh(["systemctl", "stop", self.RELAY], check=False)
        sh(["systemctl", "reset-failed", self.RELAY], check=False)
        logf = os.path.join(self.run_dir, "logs", "relay.log")
        cmd = ["systemd-run", "--unit", self.RELAY, "--collect", "--quiet", "-p", "Restart=always",
               "-p", "StandardOutput=append:" + logf, "-p", "StandardError=append:" + logf]
        for k, v in sorted(dict(self.ros_env(), PYTHONPATH=SRC, PYTHONDONTWRITEBYTECODE="1").items()):
            cmd.append("--setenv=%s=%s" % (k, v))
        sh(cmd + ["--", "/bin/bash", os.path.join(SRC, "neurobot/sim/budget/run_ros.sh"), "python3",
                  os.path.join(SRC, "neurobot/sim/runs/relay.py"), "--faults", os.path.join(self.sched, "faults.json"),
                  "--anchor", self.anchor_path, "--out-topic", CAMERA_RELAY_TOPIC,
                  "--log", os.path.join(self.run_dir, "relay.jsonl")])

    def start(self) -> None:
        sh(["systemctl", "reset-failed"], check=False)
        sh(["systemctl", "start", "--no-block", "neurobot.target"])
        if not wait(lambda: sh(["neurobot", "doctor"], check=False, timeout=120).returncode == 0, 240, step=3.0):
            raise RuntimeError("A1: doctor не дал «ОК»: %s" % sh(["neurobot", "doctor"], check=False).stdout[-800:])

    def stop(self) -> None:
        """Цель и все юниты пакета (юнит, который ещё поднимался, остановка цели может не задеть), узел отказов."""
        sh(["systemctl", "stop", "neurobot.target"], check=False, timeout=180)
        sh(["systemctl", "stop"] + sorted(self.units().values()) + [self.RELAY], check=False, timeout=180)

    def pid(self, role: str) -> int:
        unit = self.UNITS[role]
        if role == "rosbridge":
            return find_pid(unit, ROSBRIDGE_NODE)
        if role in ("lidar", "camera"):
            return find_pid(unit, SENSOR_NODE)
        if role == "base":
            return find_pid(unit, BASE_NODE)
        pid = int(unit_prop(unit, "MainPID") or 0)
        return pid if pid > 0 else 0

    def up(self, role: str) -> bool:
        config, probes = deploy_lib()
        if not unit_active(self.UNITS[role]):
            return False
        cfg = config.load(ENV_FILE)
        try:
            if role == "rosbridge":
                probes.ws_handshake("127.0.0.1", int(cfg.get("NEUROBOT_ROSBRIDGE_PORT")), 1.0)
            elif role == "mcp":
                probes.probe_mcp(cfg.get("NEUROBOT_MCP_URL"), 3.0)
        except probes.ProbeError:
            return False
        if role == "agent":
            return port_open(self.gateway_port)
        return True

    @staticmethod
    def estop_release() -> dict:
        """Человек снимает защёлку в терминале (`neurobot estop release`, ответ «release» в псевдотерминале)."""
        import pty
        import select
        pid, fd = pty.fork()
        if pid == 0:
            os.execvp("neurobot", ["neurobot", "estop", "release", "--reason", "оператор прогона"])
        text, sent, end = b"", False, time.monotonic() + 60
        while time.monotonic() < end:
            if not select.select([fd], [], [], 0.2)[0]:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            text += chunk
            if not sent and "Введите".encode() in text:
                os.write(fd, b"release\n")
                sent = True
        _, status = os.waitpid(pid, 0)
        return {"rc": os.waitstatus_to_exitcode(status), "out": text.decode("utf-8", "replace")[-300:]}

