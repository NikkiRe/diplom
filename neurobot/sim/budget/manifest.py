"""(ВМ sim) manifest.json прогона бюджета: версии, хеши кода и бинарников, конфиги шагов, отклонения от платы.

  python3 -m neurobot.sim.budget.manifest --run DIR [--base ~/budget_20261016]

Хеши кода снимаются в момент записи: компоненты (perception, executor, safety, двойник) другие агенты правят
параллельно, поэтому манифест пишется сразу после прогона.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
CODE = ["neurobot/sim/budget/*.py", "neurobot/sim/budget/*.sh", "neurobot/sim/budget/*.json",
        "neurobot/sim/budget/launch/*.py", "neurobot/perception/*.py", "neurobot/executor/*.py",
        "neurobot/safety/*.py", "neurobot/sim/twin/*.py", "neurobot/sim/twin/launch/*.py",
        "neurobot/sim/twin/config/*.yaml", "neurobot/sim/twin/urdf/*", "neurobot/sim/gestures/*.py",
        "neurobot/deploy/mcp/ros_mcp_server.patch", "neurobot/deploy/mcp/ros_mcp.json.example",
        "neurobot/deploy/fakes/picoclaw.json", "scripts/sim/vm_env.sh", "scripts/sim/cyclonedds.xml",
        "sysmon/bin/sysmon-linux-arm64",
        "third_party/neurochip/demo/demo-ros2_feature/models/snn_emnist_small.pth"]
ROS_PKGS = ["ros-jazzy-rclpy", "ros-jazzy-rmw-cyclonedds-cpp", "ros-jazzy-rosbridge-server",
            "ros-jazzy-realsense2-camera", "ros-jazzy-librealsense2", "ros-jazzy-urg-node",
            "ros-jazzy-controller-manager", "ros-jazzy-diff-drive-controller", "ros-jazzy-twist-mux",
            "ros-jazzy-imu-filter-madgwick", "ros-jazzy-robot-state-publisher", "ros-jazzy-gz-ros2-control",
            "ros-jazzy-ros-gz-bridge"]
DEVIATIONS = [
    "ROS 2 Jazzy, Python 3.12 и Cyclone DDS вместо Foxy, Python 3.8 и Fast DDS (умолчание Foxy) — память процессов "
    "ROS и Python на плате будет другой",
    "perception: torch 2.4.1 CPU вместо 1.13 + CUDA; YOLO заменён FakeYolo — CPU предобработки и NMS YOLO не учтён; "
    "память CUDA — балласт 1024 МиБ в процессе perception [ист.: форум NVIDIA, «around 1GiB»]",
    "realsense2_camera 4.58 и urg_node 1.1.2 (Jazzy; urg_node2 в Jazzy нет) работают без устройств в отдельном домене "
    "DDS; потоки камеры — балласт 30 МиБ и 20 % ядра A57 [оценка]",
    "controller_manager — заместитель с mock_components в отдельном домене; на плате железо — мост UART к STM32",
    "twist_mux, robot_state_publisher, imu_filter_madgwick — процессы двойника, перенесённые в slice: страницы, "
    "выделенные до переноса, остаются на счету прежней группы (cgroup v2 не переносит заряд), MemoryMax их не "
    "ограничивает; PSS учтён полностью",
    "камера цвета двойника 10 к/с (= темп детекции), на роботе realsense-ros, вероятно, отдаёт 30: разбор лишних "
    "кадров в rclpy не учтён; глубина двойника 424×240 15 к/с — профиль робота не снят",
    "CPU: квота = 4 × (7-zip одного A57 / одного ядра ВМ); суммарное время CPU как у четырёх A57, но одиночный поток "
    "в ВМ в ≈ 3,9 раза быстрее A57 (cpucal_check); соотношение для Python и DDS — [допущение] до cpubench на плате",
    "своп выключен (MemorySwapMax=0): zram JetPack не моделируется, бюджет без его кредита",
    "ВМ общая с другими агентами: CPU-секунды и RTF зависят от их нагрузки (load1 и занятость ВМ — в report)",
    "PicoClaw 0.2.8 без каналов и без ходов LLM, ros_mcp_server без вызовов инструментов — агентский путь в простое",
]


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def out(argv: List[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return "ошибка: %s" % e


def code_hashes() -> Dict[str, str]:
    res = {}
    for pattern in CODE:
        for path in sorted(glob.glob(os.path.join(PROJ, pattern))):
            if os.path.isfile(path):
                res[os.path.relpath(path, PROJ)] = sha256(path)
    return res


def steps(run: str) -> List[dict]:
    res = []
    for path in sorted(glob.glob(os.path.join(run, "*", "step.json"))):
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        res.append({k: s.get(k) for k in ("label", "mem_mib", "quota_pct", "cpus", "period_ms", "ratio", "domain",
                                          "proxy_domain", "slice_props", "ready_s", "twin_run")})
    return res


def build(run: str, base: str) -> dict:
    tools = {"7za": os.path.join(base, "tools", "p7zip16", "usr", "lib", "p7zip", "7za"),
             "picoclaw": os.path.join(base, "tools", "picoclaw028", "picoclaw"),
             "ros_mcp_server.py (с патчем)": os.path.join(base, "mcp", "ros_mcp_server.py")}
    ml = os.path.expanduser("~/nb/venv/bin/python")
    return {
        "format": "neurobot-budget-manifest/1",
        "written": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "label": "стенд (ВМ sim): ориентир, не результат; балласт и заместители помечены в report и README",
        "vm": {"kernel": platform.release(), "machine": platform.machine(), "ncpu": os.cpu_count(),
               "systemd": (out(["systemctl", "--version"]).splitlines() or [""])[0],
               "python": sys.version.split()[0], "os": out(["lsb_release", "-ds"])},
        "versions": {
            "ros": dict(line.split(" ", 1) for line in out(["dpkg-query", "-W", "-f", "${Package} ${Version}\\n"]
                                                           + ROS_PKGS).splitlines() if " " in line),
            "torch (perception)": out([ml, "-c", "import torch; print(torch.__version__)"]),
            "numpy (perception)": out([ml, "-c", "import numpy; print(numpy.__version__)"]),
            "mcp": out([os.path.join(base, "venv-mcp", "bin", "python"), "-c",
                        "import importlib.metadata as m; print(m.version('mcp'), m.version('websocket-client'))"]),
            "picoclaw": "0.2.8 (релиз sipeed/picoclaw, picoclaw_Linux_arm64.tar.gz)",
            "p7zip": "16.02+dfsg-8 (Debian bullseye, p7zip-full)",
        },
        "tools_sha256": {k: sha256(v) for k, v in tools.items() if os.path.exists(v)},
        "code_sha256": code_hashes(),
        "steps": steps(run),
        "deviations": DEVIATIONS,
    }


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--run", required=True)
    p.add_argument("--base", default=os.path.expanduser("~/budget_20261016"))
    a = p.parse_args(argv)
    if not os.path.isdir(a.run):
        print("manifest: нет каталога %s" % a.run, file=sys.stderr)
        return 2
    with open(os.path.join(a.run, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(build(a.run, a.base), f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
