"""(ВМ sim, root) Стек «как на Jetson» в cgroup jetson.slice: подъём, перенос узлов двойника, замер, остановка.

  sudo python3 -m neurobot.sim.budget.stack run --out DIR --mem 3336[,3072,…] --quota PCT --ratio R \
       [--seconds 600] [--warmup 30] [--cpus 4] [--labels main,…] [--keep-twin]
        для каждого MemoryMax — шаг DIR/<метка>: наблюдатели (collect, health, sysmon), стек в slice, ожидание
        готовности, warmup + seconds замера, снимок юнитов, остановка; двойник поднимается один раз на все шаги
        (если он упал на шаге, следующий шаг поднимает его заново)
  sudo python3 -m neurobot.sim.budget.stack down [--twin-root DIR]   остановить юниты бюджета, снять настройки
        slice; с --twin-root — и двойник этого корня
  sudo python3 -m neurobot.sim.budget.stack status                   состояние юнитов бюджета (JSON)

Юниты — transient-службы systemd jetson-<компонент>.service в jetson.slice от имени SUDO_USER (обычный пользователь,
root запрещён), с супервизией как у юнитов neurobot/deploy (Type, WatchdogSec, Restart, RestartSec, KillSignal).
Узлы двойника, которые на роботе работают на Jetson (twist_mux, robot_state_publisher, imu_filter_madgwick),
переносятся в slice scope-юнитами jetson-<узел>.scope. Наблюдатели — budget-*.service в budget-obs.slice, вне
лимитов. В середине окна замера шаг снимает уникальные страницы slice (collect.unique_snapshot) → uniq.json.
Ошибки аргументов — код 2, строки журнала — в stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .collect import unique_snapshot

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
TWIN = os.path.join(PROJ, "neurobot", "sim", "twin")
SIM = os.path.join(PROJ, "scripts", "sim")
SLICE, OBS_SLICE = "jetson.slice", "budget-obs.slice"
CG_ROOT = "/sys/fs/cgroup"
WRAP = ["/bin/bash", os.path.join(HERE, "run_ros.sh")]
SNN = os.path.join(PROJ, "third_party", "neurochip", "demo", "demo-ros2_feature", "models", "snn_emnist_small.pth")
SYSMON = os.path.join(PROJ, "sysmon", "bin", "sysmon-linux-arm64")
# узлы двойника, которые на роботе работают на Jetson: имя → признак в командной строке
TWIN_NODES = {"twist_mux": "/twist_mux/twist_mux", "robot_state_publisher": "/robot_state_publisher",
              "imu_filter_madgwick": "imu_filter_madgwick_node"}
TWIN_HW = "<plugin>gz_ros2_control/GazeboSimSystem</plugin>"
MOCK_HW = '<plugin>mock_components/GenericSystem</plugin><param name="calculate_dynamics">true</param>'


def log(msg: str) -> None:
    print("[budget] " + msg, file=sys.stderr, flush=True)


@dataclass
class Cfg:
    user: str
    base: str                      # рабочий каталог в ВМ: инструменты и venv (README, «Подготовка»)
    step_dir: str
    mem_mib: int
    quota_pct: int
    ratio: float                   # ядро A57 / ядро ВМ по 7-zip (cpucal)
    cpus: int = 4
    period_ms: int = 20
    tasks_max: int = 2048
    domain: int = 86
    proxy_domain: int = 87
    rosbridge_port: int = 19186
    mcp_port: int = 18186
    picoclaw_port: int = 18796
    gestures_dir: str = ""
    twin_run: str = ""
    model: dict = field(default_factory=dict)
    ml_python: str = ""

    @property
    def mcp_python(self) -> str:
        return os.path.join(self.base, "venv-mcp", "bin", "python")

    @property
    def mcp_script(self) -> str:
        return os.path.join(self.base, "mcp", "ros_mcp_server.py")

    @property
    def picoclaw(self) -> str:
        return os.path.join(self.base, "tools", "picoclaw028", "picoclaw")


@dataclass
class Spec:
    name: str
    argv: List[str]
    role: str                      # measured | proxy | ballast
    proxy: bool = False            # домен DDS заместителей
    notify: bool = False
    watchdog_s: float = 0.0
    restart_s: float = 1.0
    kill: str = "SIGTERM"
    start_s: int = 60
    stop_s: int = 10
    env: Dict[str, str] = field(default_factory=dict)
    ready: Tuple = ()              # ("notify",) | ("port", N) | ("log", текст)


def specs(c: Cfg) -> List[Spec]:
    """Состав стека в порядке подъёма (PLAN §5.4: драйверы, safety_gate, исполнитель, perception, rosbridge, MCP,
    агент). Супервизия — как у юнитов neurobot/deploy/units."""
    py_env = {"PYTHONPATH": PROJ, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    cam = c.model["ballast"]["camera_streams"]
    cuda = c.model["ballast"]["cuda"]
    d = c.step_dir
    return [
        Spec("base", WRAP + ["ros2", "launch", os.path.join(HERE, "launch", "base_proxy.launch.py"),
                             "urdf:=" + os.path.join(d, "robot_mock.urdf"),
                             "controllers:=" + os.path.join(d, "controllers.yaml")],
             "proxy", proxy=True, kill="SIGINT"),
        Spec("camera", WRAP + ["ros2", "launch", "realsense2_camera", "rs_launch.py", "enable_gyro:=true",
                               "enable_accel:=true", "unite_imu_method:=2"], "proxy", proxy=True, kill="SIGINT"),
        Spec("camera_streams", ["python3", "-m", "neurobot.sim.budget.ballast", "--mib", str(cam["mib"]), "--net",
                                "--cpu-pct", "%.2f" % (cam["cpu_a57_pct"] * c.ratio)], "ballast", env=py_env),
        Spec("lidar", WRAP + ["ros2", "launch", os.path.join(HERE, "launch", "lidar_proxy.launch.py")],
             "proxy", proxy=True, kill="SIGINT"),
        Spec("safety", WRAP + ["python3", "-m", "neurobot.safety"], "measured", notify=True, watchdog_s=3,
             restart_s=0.5, start_s=30, stop_s=20, ready=("notify",),
             env=dict(py_env, STATE_DIRECTORY=os.path.join(d, "safety_state"))),
        Spec("executor", WRAP + ["python3", "-m", "neurobot.executor", "--ros-args", "-p", "cmd_vel_type:=Twist"],
             "measured", notify=True, watchdog_s=3, restart_s=0.5, stop_s=5, ready=("notify",), env=py_env),
        Spec("perception", WRAP + [c.ml_python, "-m", "neurobot.sim.budget.ballast", "--mib", str(cuda["mib"]),
                                   "--run", os.path.join(PROJ, "neurobot", "perception", "predict_live.py"), "--",
                                   "--camera", "ros", "--yolo", "fake", "--snn-weights", SNN,
                                   "--ros-args", "-r", "/cmd_vel:=/cmd_vel_gesture"],
             "measured", notify=True, watchdog_s=5, kill="SIGINT", start_s=120, stop_s=15, ready=("notify",),
             env=dict(py_env, PYTHONPATH=PROJ + ":" + os.path.join(PROJ, "neurobot", "sim", "gestures", "shim"),
                      NEUROBOT_GESTURES_TIMELINE=os.path.join(c.gestures_dir, "timeline.jsonl"),
                      NEUROBOT_GESTURES_ANCHOR=os.path.join(d, "anchor.json"),
                      NEUROBOT_FAKES_PROBE=os.path.join(d, "probe.jsonl"))),
        Spec("rosbridge", WRAP + ["ros2", "launch", "rosbridge_server", "rosbridge_websocket_launch.xml",
                                  "address:=127.0.0.1", "port:=%d" % c.rosbridge_port,
                                  "topics_glob:='[/cmd_vel_agent]'", "services_glob:='[/rosapi/topics]'",
                                  "params_glob:='[none]'"],
             "measured", kill="SIGINT", ready=("port", c.rosbridge_port)),
        Spec("mcp", [c.mcp_python, c.mcp_script, "--config", os.path.join(d, "mcp.json")], "measured",
             env={"PYTHONUNBUFFERED": "1"}, ready=("port", c.mcp_port)),
        Spec("picoclaw", [c.picoclaw, "--no-color", "gateway"], "measured", restart_s=2, stop_s=15,
             env={"PICOCLAW_HOME": os.path.join(d, "picoclaw"), "GOMAXPROCS": str(c.cpus)},
             ready=("log", "MCP server initialization complete")),
    ]


def unit(name: str, kind: str = "service") -> str:
    return "jetson-%s.%s" % (name, kind)


def unit_argv(s: Spec, c: Cfg) -> List[str]:
    """systemd-run для компонента: служба в jetson.slice от пользователя, журнал — в файл шага."""
    logf = os.path.join(c.step_dir, "logs", s.name + ".log")
    props = ["Type=" + ("notify" if s.notify else "exec"), "Restart=always", "RestartSec=%gs" % s.restart_s,
             "KillSignal=" + s.kill, "TimeoutStartSec=%ds" % s.start_s, "TimeoutStopSec=%ds" % s.stop_s,
             "StandardOutput=append:" + logf, "StandardError=append:" + logf, "StartLimitIntervalSec=0"]
    if s.notify:
        props.append("NotifyAccess=all")
    if s.watchdog_s:
        props.append("WatchdogSec=%gs" % s.watchdog_s)
    env = dict(s.env, ROS_DOMAIN_ID=str(c.proxy_domain if s.proxy else c.domain),
               ROS_LOG_DIR=os.path.join(c.step_dir, "ros_log"))
    argv = ["systemd-run", "--unit", unit(s.name), "--slice", SLICE, "--uid", c.user, "--no-block", "--quiet",
            "--expand-environment=no", "--working-directory", c.step_dir]
    for p in props:
        argv += ["-p", p]
    for k in sorted(env):
        argv.append("--setenv=%s=%s" % (k, env[k]))
    return argv + ["--"] + s.argv


def slice_props(c: Cfg) -> List[str]:
    return ["MemoryMax=%dM" % c.mem_mib, "MemorySwapMax=0", "CPUQuota=%d%%" % c.quota_pct,
            "CPUQuotaPeriodSec=%dms" % c.period_ms, "AllowedCPUs=0-%d" % (c.cpus - 1), "TasksMax=%d" % c.tasks_max]


def adopt_argv(name: str, pid: int) -> List[str]:
    """Scope jetson-<узел>.scope в jetson.slice для уже идущего процесса (StartTransientUnit с PIDs)."""
    return ["busctl", "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager", "StartTransientUnit", "ssa(sv)a(sa(sv))", unit(name, "scope"),
            "fail", "2", "PIDs", "au", "1", str(pid), "Slice", "s", SLICE, "0"]


def mock_urdf(text: str) -> str:
    """URDF двойника с железом mock_components вместо Gazebo."""
    if text.count(TWIN_HW) != 1:
        raise ValueError("в URDF двойника нет ровно одного плагина %s" % TWIN_HW)
    return text.replace(TWIN_HW, MOCK_HW)


def mcp_config(example: dict, c: Cfg) -> dict:
    """ros_mcp.json.example deploy с портами шага."""
    cfg = json.loads(json.dumps(example))
    cfg["server"] = {"host": "127.0.0.1", "port": c.mcp_port}
    cfg["rosbridge"]["url"] = "ws://127.0.0.1:%d" % c.rosbridge_port
    return cfg


def picoclaw_config(fake: dict, c: Cfg) -> dict:
    """Конфиг стенда deploy (fakes/picoclaw.json, поля 0.2.8) с путями и портами шага; LLM не вызывается."""
    cfg = json.loads(json.dumps(fake))
    cfg["agents"]["defaults"]["workspace"] = os.path.join(c.step_dir, "picoclaw", "workspace")
    cfg["gateway"].update(host="127.0.0.1", port=c.picoclaw_port)
    cfg["tools"]["mcp"]["servers"]["robot_mcp"]["url"] = "http://127.0.0.1:%d/mcp" % c.mcp_port
    return cfg


def twin_pids(procs: Sequence[Tuple[int, int, str]], pgid: int) -> Dict[str, int]:
    """PID узлов двойника из TWIN_NODES в его группе процессов: (pid, pgid, cmdline)."""
    out = {}
    for pid, pg, cmd in procs:
        if pg != pgid:
            continue
        for name, mark in TWIN_NODES.items():
            if mark in cmd and name not in out:
                out[name] = pid
    return out


def proc_table(root: str = "/proc") -> List[Tuple[int, int, str]]:
    out = []
    for d in os.listdir(root):
        if not d.isdigit():
            continue
        try:
            with open(os.path.join(root, d, "stat")) as f:
                pgid = int(f.read().rpartition(")")[2].split()[2])
            with open(os.path.join(root, d, "cmdline"), "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="replace")
        except (OSError, IndexError, ValueError):
            continue
        out.append((int(d), pgid, cmd))
    return out


# --- действия (root) --------------------------------------------------------------------------------------------

def sh(argv: Sequence[str], check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    r = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError("%s: код %d: %s" % (" ".join(argv[:3]), r.returncode, (r.stderr or r.stdout).strip()))
    return r


def as_user(c: Cfg, script: str, timeout: float = 300) -> subprocess.CompletedProcess:
    return sh(["sudo", "-u", c.user, "-H", "bash", "-c", script], check=False, timeout=timeout)


def show(units: Sequence[str]) -> Dict[str, Dict[str, str]]:
    keys = "Id,LoadState,ActiveState,SubState,Result,NRestarts,MainPID,ExecMainStatus,ExecMainCode,ControlGroup"
    r = sh(["systemctl", "show", "-p", keys] + list(units), check=False)
    out: Dict[str, Dict[str, str]] = {}
    for block in r.stdout.strip().split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if props.get("Id"):
            out[props["Id"]] = props
    return out


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def append_event(c: Cfg, **ev) -> None:
    ev["t_wall"] = time.time()
    with open(os.path.join(c.step_dir, "events.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def write_units(c: Cfg, extra: Dict[str, dict]) -> None:
    units = {s.name: {"unit": unit(s.name), "role": s.role, "cgroup": os.path.join(CG_ROOT, SLICE, unit(s.name))}
             for s in specs(c)}
    units.update(extra)
    tmp = os.path.join(c.step_dir, "units.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"slice": os.path.join(CG_ROOT, SLICE), "units": units}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(c.step_dir, "units.json"))


def prepare_step(c: Cfg) -> None:
    for sub in ("logs", "ros_log", "safety_state", os.path.join("picoclaw", "workspace")):
        os.makedirs(os.path.join(c.step_dir, sub), exist_ok=True)
    with open(os.path.join(c.twin_run, "robot.urdf"), encoding="utf-8") as f:
        urdf = mock_urdf(f.read())
    with open(os.path.join(c.step_dir, "robot_mock.urdf"), "w", encoding="utf-8") as f:
        f.write(urdf)
    shutil.copy(os.path.join(c.twin_run, "controllers.yaml"), os.path.join(c.step_dir, "controllers.yaml"))
    with open(os.path.join(PROJ, "neurobot", "deploy", "mcp", "ros_mcp.json.example"), encoding="utf-8") as f:
        mcp = mcp_config(json.load(f), c)
    with open(os.path.join(c.step_dir, "mcp.json"), "w", encoding="utf-8") as f:
        json.dump(mcp, f, ensure_ascii=False, indent=1)
    with open(os.path.join(PROJ, "neurobot", "deploy", "fakes", "picoclaw.json"), encoding="utf-8") as f:
        pc = picoclaw_config(json.load(f), c)
    with open(os.path.join(c.step_dir, "picoclaw", "config.json"), "w", encoding="utf-8") as f:
        json.dump(pc, f, ensure_ascii=False, indent=1)
    sh(["chown", "-R", c.user, c.step_dir])


def slice_set(c: Cfg) -> None:
    sh(["systemctl", "set-property", "--runtime", SLICE] + slice_props(c))


def adopt_twin(c: Cfg, pgid: int) -> Dict[str, dict]:
    """Перенести узлы двойника в slice; уже перенесённые не трогать."""
    found = twin_pids(proc_table(), pgid)
    missing = sorted(set(TWIN_NODES) - set(found))
    if missing:
        raise RuntimeError("в группе двойника %d нет узлов %s" % (pgid, ", ".join(missing)))
    out = {}
    for name, pid in sorted(found.items()):
        cg = os.path.join(CG_ROOT, SLICE, unit(name, "scope"))
        with open("/proc/%d/cgroup" % pid) as f:
            here = f.read().strip().partition("::")[2]
        if here != "/%s/%s" % (SLICE, unit(name, "scope")):
            sh(adopt_argv(name, pid))
            append_event(c, ev="adopt", name=name, pid=pid, was=here)
        out[name] = {"unit": unit(name, "scope"), "role": "measured", "cgroup": cg, "moved": True}
    return out


def wait_ready(c: Cfg, s: Spec, deadline: float) -> bool:
    u = unit(s.name)
    while time.monotonic() < deadline:
        st = show([u]).get(u, {})
        if s.ready[0] == "notify" and st.get("ActiveState") == "active":
            return True
        if s.ready[0] == "port" and st.get("ActiveState") == "active" and port_open(s.ready[1]):
            return True
        if s.ready[0] == "log":
            try:
                with open(os.path.join(c.step_dir, "logs", s.name + ".log"), errors="replace") as f:
                    if s.ready[1] in f.read():
                        return True
            except OSError:
                pass
        time.sleep(0.5)
    return False


def up(c: Cfg, pgid: int) -> Dict[str, Optional[float]]:
    """Подъём шага: файлы, slice, перенос узлов двойника, компоненты по порядку; время готовности каждого, с."""
    prepare_step(c)
    slice_set(c)
    write_units(c, adopt_twin(c, pgid))
    for p in (c.rosbridge_port, c.mcp_port, c.picoclaw_port):
        if port_open(p):
            raise RuntimeError("порт %d уже занят" % p)
    ready: Dict[str, Optional[float]] = {}
    for s in specs(c):
        t0 = time.monotonic()
        sh(unit_argv(s, c))
        append_event(c, ev="start", name=s.name)
        if s.ready:
            ok = wait_ready(c, s, t0 + s.start_s)
            ready[s.name] = round(time.monotonic() - t0, 2) if ok else None
            append_event(c, ev="ready" if ok else "not_ready", name=s.name, after_s=ready[s.name])
            if not ok:
                log("%s не готов за %d с — шаг продолжается (замер покажет причину)" % (s.name, s.start_s))
    return ready


def stack_units() -> List[str]:
    r = sh(["systemctl", "list-units", "--all", "--plain", "--no-legend", "jetson-*", "budget-*"], check=False)
    return [line.split()[0] for line in r.stdout.splitlines() if line.strip()]


def stop_services(prefix: str) -> None:
    units = [u for u in stack_units() if u.startswith(prefix) and u.endswith(".service")]
    if units:
        sh(["systemctl", "stop"] + units, check=False, timeout=120)
        sh(["systemctl", "reset-failed"] + units, check=False)


def observers_up(c: Cfg, interval: float) -> None:
    d = c.step_dir
    obs = {
        "collect": ["python3", "-m", "neurobot.sim.budget.collect", "--units", os.path.join(d, "units.json"),
                    "--out", os.path.join(d, "collect.jsonl"), "--interval", "%g" % interval],
        "health": WRAP + ["python3", "-m", "neurobot.sim.budget.health", "--out", os.path.join(d, "health.jsonl")],
        "sysmon": [SYSMON, "--cgroup", os.path.join(CG_ROOT, SLICE), "--interval", "%gs" % interval,
                   "--system", "--out", os.path.join(d, "sysmon.jsonl")],
    }
    for name, argv in obs.items():
        sh(["systemd-run", "--unit", "budget-%s.service" % name, "--slice", OBS_SLICE, "--uid", c.user,
            "--quiet", "--no-block", "--working-directory", d, "-p", "KillSignal=SIGINT",
            "-p", "StandardOutput=append:" + os.path.join(d, "logs", "budget-%s.log" % name),
            "-p", "StandardError=append:" + os.path.join(d, "logs", "budget-%s.log" % name),
            "--setenv=PYTHONPATH=" + PROJ, "--setenv=PYTHONDONTWRITEBYTECODE=1",
            "--setenv=ROS_DOMAIN_ID=%d" % c.domain, "--"] + argv)


# --- двойник (от пользователя) ----------------------------------------------------------------------------------

def twin_up(c: Cfg, root: str, world: str, color_hz: int) -> Tuple[str, int]:
    script = ("TWIN_RUN_ROOT=%s TWIN_DOMAIN=%d bash %s/twin_vm.sh start world:=%s color_hz:=%d rosbridge:=false"
              % (root, c.domain, TWIN, world, color_hz))
    r = as_user(c, script, timeout=60)
    if r.returncode:
        raise RuntimeError("twin_vm.sh start: %s" % (r.stderr or r.stdout).strip())
    run = os.path.realpath(os.path.join(root, "current"))
    with open(os.path.join(run, "pid")) as f:
        pgid = int(f.read())
    deadline = time.monotonic() + 60          # launch пишет controllers.yaml не сразу, а twin_ready его читает
    while not os.path.exists(os.path.join(run, "controllers.yaml")):
        if not twin_alive(pgid) or time.monotonic() > deadline:
            twin_down(c, run)
            raise RuntimeError("двойник не поднялся: %s/launch.log" % run)
        time.sleep(0.5)
    r = as_user(c, "source %s/vm_env.sh; export ROS_DOMAIN_ID=%d; python3 %s/twin_ready.py --timeout 150 "
                   "--run-dir %s" % (SIM, c.domain, TWIN, run), timeout=200)
    if r.returncode:
        twin_down(c, run)
        raise RuntimeError("двойник не готов: %s" % (r.stdout + r.stderr).strip()[-600:])
    return run, pgid


def running_twin(root: str) -> Tuple[str, Optional[int]]:
    """Двойник этого корня, оставленный прошлым run --keep-twin: (каталог, PGID) или ("", None)."""
    run = os.path.realpath(os.path.join(root, "current"))
    try:
        with open(os.path.join(run, "pid")) as f:
            pgid = int(f.read())
    except (OSError, ValueError):
        return "", None
    return (run, pgid) if twin_alive(pgid) else ("", None)


def twin_alive(pgid: int) -> bool:
    return any(pg == pgid for _, pg, _ in proc_table())


def twin_down(c: Cfg, run: str) -> None:
    as_user(c, "bash %s/twin_vm.sh stop %s" % (TWIN, run), timeout=60)


# --- прогон -----------------------------------------------------------------------------------------------------

def gestures(c: Cfg, out: str, seed: int) -> str:
    d = os.path.join(out, "gestures")
    if not os.path.exists(os.path.join(d, "timeline.jsonl")):
        os.makedirs(d, exist_ok=True)
        sh(["chown", c.user, d])
        r = as_user(c, "cd %s && PYTHONPATH=%s python3 -m neurobot.sim.gestures.gesturegen gen --seed %d "
                       "--det-fps 10 --out-dir %s" % (PROJ, PROJ, seed, d), timeout=120)
        if r.returncode:
            raise RuntimeError("gesturegen: %s" % r.stderr.strip())
    return d


def kernel_oom(since: float) -> List[str]:
    r = sh(["journalctl", "-k", "--no-pager", "-o", "short-unix", "--since", "@%d" % int(since)], check=False)
    return [line for line in r.stdout.splitlines() if re.search(r"Out of memory|oom-kill|Killed process", line)]


def run_step(c: Cfg, label: str, pgid: int, seconds: float, warmup: float, interval: float) -> dict:
    os.makedirs(os.path.join(c.step_dir, "logs"), exist_ok=True)
    sh(["chown", "-R", c.user, c.step_dir])
    t0 = time.time()
    write_units(c, {})
    observers_up(c, interval)
    ready = up(c, pgid)
    t_meas = time.time() + warmup
    log("%s: MemoryMax %d МиБ, готовность %s; замер %g с после прогрева %g с" % (
        label, c.mem_mib, json.dumps(ready), seconds, warmup))
    time.sleep(warmup + seconds / 2)
    with open(os.path.join(c.step_dir, "units.json"), encoding="utf-8") as f:
        uniq = unique_snapshot(json.load(f))
    with open(os.path.join(c.step_dir, "uniq.json"), "w", encoding="utf-8") as f:
        json.dump(uniq, f, ensure_ascii=False, indent=1)
    time.sleep(max(0.0, t_meas + seconds - time.time()))
    t_end = time.time()
    names = [u for u in stack_units() if u.startswith("jetson-")]
    status = show(names)
    stop_services("jetson-")
    stop_services("budget-")
    step = {"label": label, "mem_mib": c.mem_mib, "quota_pct": c.quota_pct, "cpus": c.cpus,
            "period_ms": c.period_ms, "ratio": c.ratio, "domain": c.domain, "proxy_domain": c.proxy_domain,
            "t_start": t0, "t_measure_start": t_meas, "t_measure_end": t_end, "ready_s": ready,
            "status_end": status, "twin_alive_end": twin_alive(pgid), "twin_run": c.twin_run,
            "kernel_oom": kernel_oom(t0), "slice_props": slice_props(c)}
    with open(os.path.join(c.step_dir, "step.json"), "w", encoding="utf-8") as f:
        json.dump(step, f, ensure_ascii=False, indent=1)
    sh(["chown", "-R", c.user, c.step_dir])
    return step


def cmd_run(a: argparse.Namespace, user: str) -> int:
    try:
        mems = [int(x) for x in a.mem.split(",")]
    except ValueError:
        mems = []
    labels = a.labels.split(",") if a.labels else ["mem%d" % m for m in mems]
    if not mems or len(labels) != len(mems) or any(m < 256 for m in mems) or a.quota < 5 or not 0 < a.ratio <= 1:
        log("--mem: МиБ ≥ 256 через запятую, --labels той же длины, --quota ≥ 5, 0 < --ratio ≤ 1")
        return 2
    os.makedirs(a.out, exist_ok=True)
    sh(["chown", user, a.out])
    with open(os.path.join(HERE, "model.json"), encoding="utf-8") as f:
        model = json.load(f)
    base = Cfg(user=user, base=a.base, step_dir="", mem_mib=0, quota_pct=a.quota, ratio=a.ratio, cpus=a.cpus,
               domain=a.domain, proxy_domain=a.proxy_domain, model=model,
               ml_python=a.ml_python or os.path.join(pwd.getpwnam(user).pw_dir, "nb", "venv", "bin", "python"))
    base.gestures_dir = gestures(base, a.out, a.seed)
    twin_root = os.path.join(a.out, "twin_runs")
    os.makedirs(twin_root, exist_ok=True)
    sh(["chown", user, twin_root])
    run, pgid = running_twin(twin_root)
    if pgid is None:
        run, pgid = twin_up(base, twin_root, a.world, a.color_hz)
    log("двойник: %s, PGID %d, домен %d" % (run, pgid, a.domain))
    try:
        for mem, label in zip(mems, labels):
            if not twin_alive(pgid):
                log("двойник погас на прошлом шаге — поднимаю заново")
                run, pgid = twin_up(base, twin_root, a.world, a.color_hz)
            c = Cfg(**{**base.__dict__, "mem_mib": mem, "step_dir": os.path.join(a.out, label), "twin_run": run})
            run_step(c, label, pgid, a.seconds, a.warmup, a.interval)
    finally:
        stop_services("jetson-")
        stop_services("budget-")
        if not a.keep_twin:
            twin_down(base, run)
            sh(["systemctl", "revert", SLICE], check=False)
    return 0


def cmd_down(a: argparse.Namespace, user: str) -> int:
    stop_services("jetson-")
    stop_services("budget-")
    if a.twin_root:
        c = Cfg(user=user, base="", step_dir="", mem_mib=0, quota_pct=0, ratio=1)
        twin_down(c, os.path.realpath(os.path.join(a.twin_root, "current")))
    deadline = time.monotonic() + 15          # scope перенесённых узлов исчезает, когда выходят их процессы
    left = [u for u in stack_units() if u.startswith("jetson-")]
    while left and a.twin_root and time.monotonic() < deadline:
        time.sleep(0.5)
        left = [u for u in stack_units() if u.startswith("jetson-")]
    if left:
        log("в slice остались %s (узлы двойника) — настройки slice не сняты" % ", ".join(left))
    else:
        sh(["systemctl", "revert", SLICE], check=False)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="шаги замера")
    r.add_argument("--out", required=True, help="каталог прогона в ВМ")
    r.add_argument("--mem", required=True, help="MemoryMax шагов, МиБ, через запятую")
    r.add_argument("--labels", default="", help="имена шагов через запятую (по умолчанию mem<МиБ>)")
    r.add_argument("--quota", type=int, required=True, help="CPUQuota slice, %% одного ядра ВМ (cpucal)")
    r.add_argument("--ratio", type=float, required=True, help="ядро A57 / ядро ВМ по 7-zip (cpucal)")
    r.add_argument("--cpus", type=int, default=4, help="AllowedCPUs=0-(N-1)")
    r.add_argument("--seconds", type=float, default=600, help="замер шага после прогрева, с")
    r.add_argument("--warmup", type=float, default=30, help="прогрев после готовности, с")
    r.add_argument("--interval", type=float, default=5, help="период collect и sysmon, с")
    r.add_argument("--domain", type=int, default=86, help="ROS_DOMAIN_ID двойника и стека")
    r.add_argument("--proxy-domain", type=int, default=87, help="ROS_DOMAIN_ID заместителей драйверов")
    r.add_argument("--world", default="lab", help="мир двойника")
    r.add_argument("--color-hz", type=int, default=10, help="кадры/с камеры цвета двойника (= темп детекции)")
    r.add_argument("--seed", type=int, default=20261016, help="зерно gesturegen")
    r.add_argument("--base", default="", help="рабочий каталог инструментов (по умолчанию ~/budget_20261016)")
    r.add_argument("--ml-python", default="", help="Python с torch для perception (по умолчанию ~/nb/venv)")
    r.add_argument("--keep-twin", action="store_true", help="не гасить двойник и slice в конце")
    d = sub.add_parser("down", help="остановить юниты бюджета")
    d.add_argument("--twin-root", default="", help="корень прогонов двойника, который тоже погасить")
    sub.add_parser("status", help="состояние юнитов (JSON)")
    a = p.parse_args(argv)
    if os.geteuid() != 0:
        log("нужен root: sudo python3 -m neurobot.sim.budget.stack …")
        return 2
    user = os.environ.get("SUDO_USER", "")
    if not user or user == "root":
        log("нужен обычный пользователь: запуск через sudo")
        return 2
    if a.cmd == "status":
        print(json.dumps(show(stack_units()), ensure_ascii=False, indent=1))
        return 0
    if a.cmd == "down":
        return cmd_down(a, user)
    a.base = a.base or os.path.join(pwd.getpwnam(user).pw_dir, "budget_20261016")
    return cmd_run(a, user)


if __name__ == "__main__":
    sys.exit(main())
