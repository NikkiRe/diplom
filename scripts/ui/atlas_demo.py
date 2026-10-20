#!/usr/bin/env python3
"""Run Edge Atlas with the project's live sysfs simulator and hw-mcp. No hardware, ROS or LLM.

  python3 scripts/ui/atlas_demo.py [--port 18127] [--hw-mcp /path/to/hw-mcp]

Requires Python 3.12 (sysfsim); Go is needed only if the hw-mcp binary has not been built.
All runtime files are kept in a private temporary directory and removed on exit.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from neurobot.ui import server  # noqa: E402


def seed_lab(store):
    """Actual paired experiments; every subprocess is owned by LabStore.

    HTTP is already available while these run. A failed dependency stays visible
    as a failed job, rather than being replaced with made-up demo measurements.
    """
    from neurobot.ui.lab import LabError
    for policy in ("heartbeat", "gate"):
        try:
            job = store.start({"scenario": "noise-burst", "policy": policy,
                               "duration_s": 180, "seed": 7,
                               "label": "Шум и перегрев · " + policy})
        except LabError as exc:
            print("EdgeLab: %s" % exc, flush=True)
            return
        while True:
            current = next((j for j in store.snapshot()["jobs"] if j["id"] == job["id"]), None)
            if not current or current["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(.2)
        if not current or current["status"] != "completed":
            print("EdgeLab: эксперимент %s — %s" % (policy, current.get("error") if current else "остановлен"), flush=True)
            return
        print("EdgeLab: %s завершён · реальный EdgeCtx + mock-LLM" % policy, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=18127)
    parser.add_argument("--mcp-port", type=int, help="стабильный порт hw-mcp; по умолчанию порт Атласа + 1")
    parser.add_argument("--hw-mcp", type=Path)
    parser.add_argument("--lab", action="store_true", help="EdgeLab: запустить два настоящих парных эксперимента")
    parser.add_argument("--lab-python", type=Path, help="Python с зависимостями bench для экспериментов")
    parser.add_argument("--agent-binary", type=Path, help="готовый бинарник EdgeCtx; без флага — локальная сборка Go")
    parser.add_argument("--cam-mcp", type=Path, help="готовый cam-mcp для синтетической камеры EdgeLab")
    parser.add_argument("--cam-port", type=int, help="порт синтетической камеры; по умолчанию порт интерфейса + 2")
    args = parser.parse_args()
    mcp_port = args.mcp_port if args.mcp_port is not None else args.port + 1
    cam_port = args.cam_port if args.cam_port is not None else args.port + 2
    if not (1 <= args.port <= 65535 and 1 <= mcp_port <= 65535) or args.port == mcp_port:
        parser.error("Нужны два разных порта в диапазоне 1–65535")
    if args.lab and (not 1 <= cam_port <= 65535 or cam_port in (args.port, mcp_port)):
        parser.error("Порт камеры должен отличаться от портов интерфейса и hw-mcp")
    if sys.version_info < (3, 12):
        parser.error("Симулятор требует Python 3.12; сам каталог работает с Python 3.8")
    children = []
    srv = None
    def stop(_signum, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, stop)
    with tempfile.TemporaryDirectory(prefix="edge-atlas-demo-") as folder:
        state = Path(folder)
        try:
            binary = args.hw_mcp or ROOT / "hwmcp/.build/hw-mcp"
            if not binary.is_file():
                if not shutil.which("go"):
                    raise RuntimeError("Соберите hw-mcp: cd hwmcp && make build; либо передайте --hw-mcp")
                binary = state / "hw-mcp"
                print("Сборка hw-mcp…", flush=True)
                subprocess.run(["go", "build", "-trimpath", "-o", str(binary), "./cmd/hw-mcp"],
                               cwd=ROOT / "hwmcp", check=True)
            subprocess.run([sys.executable, "-m", "bench.sysfsim", "init", "--root", str(state / "sysfs")],
                           cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
            sim_log = (state / "sim.log").open("w")
            hw_log = (state / "hw-mcp.log").open("w")
            children.append(subprocess.Popen([sys.executable, "-m", "bench.sysfsim", "run", "--root",
                str(state / "sysfs"), "--scenario", "day", "--ready-file", str(state / "sim.ready.json")],
                cwd=ROOT, stdout=sim_log, stderr=sim_log))
            wait_until = time.monotonic() + 15
            while not (state / "sim.ready.json").exists() and time.monotonic() < wait_until:
                if children[0].poll() is not None:
                    raise RuntimeError((state / "sim.log").read_text()[-1500:])
                time.sleep(.1)
            if not (state / "sim.ready.json").exists():
                raise RuntimeError("Симулятор не сообщил о готовности")
            children.append(subprocess.Popen([str(binary.resolve()), "-root", str(state / "sysfs"), "-declared",
                str(ROOT / "bench/sysfsim/configs/declared.sim.json"), "-transport", "http", "-addr", "127.0.0.1:%d" % mcp_port, "-ready-file",
                str(state / "hw.url")], cwd=ROOT, stdout=hw_log, stderr=hw_log))
            wait_until = time.monotonic() + 15
            while not (state / "hw.url").exists() and time.monotonic() < wait_until:
                if children[-1].poll() is not None:
                    raise RuntimeError((state / "hw-mcp.log").read_text()[-1500:])
                time.sleep(.1)
            if not (state / "hw.url").exists():
                raise RuntimeError("hw-mcp не сообщил о готовности")
            if args.lab:
                camera_binary = args.cam_mcp or ROOT / "cammcp/.build/cam-mcp"
                if not camera_binary.is_file():
                    if not shutil.which("go"):
                        raise RuntimeError("Соберите cam-mcp или передайте --cam-mcp")
                    camera_binary = state / "cam-mcp"
                    subprocess.run(["go", "build", "-trimpath", "-o", str(camera_binary), "./cmd/cam-mcp"],
                                   cwd=ROOT / "cammcp", check=True)
                cam_log = (state / "cam-mcp.log").open("w")
                children.append(subprocess.Popen([str(camera_binary.resolve()), "-source", "synthetic", "-transport", "http",
                    "-addr", "127.0.0.1:%d" % cam_port, "-ready-file", str(state / "cam.url")],
                    cwd=ROOT, stdout=cam_log, stderr=cam_log))
                wait_until = time.monotonic() + 15
                while not (state / "cam.url").exists() and time.monotonic() < wait_until:
                    if children[-1].poll() is not None:
                        raise RuntimeError((state / "cam-mcp.log").read_text()[-1500:])
                    time.sleep(.1)
                if not (state / "cam.url").exists():
                    raise RuntimeError("cam-mcp не сообщил о готовности")
            srv = server.make_server(None, args.port)
            if args.lab_python:
                # Keep the venv executable path: resolving its symlink selects
                # the base interpreter and loses the environment's packages.
                srv.lab.python = str(args.lab_python.absolute())
            elif (ROOT / ".venv/bin/python").is_file():
                srv.lab.python = str(ROOT / ".venv/bin/python")
            if args.agent_binary:
                srv.lab.binary = str(args.agent_binary.resolve())
            srv.catalog.connect((state / "hw.url").read_text().strip(), "Симулятор датчиков · hw-mcp")
            if args.lab:
                srv.catalog.connect((state / "cam.url").read_text().strip(), "Синтетическая камера · cam-mcp")
                threading.Thread(target=seed_lab, args=(srv.lab,), daemon=True).start()
            print("EdgeLab · СИМУЛЯТОР SYSFS · без физического оборудования", flush=True)
            print("http://127.0.0.1:%d/?demo=1#/%s" % (srv.server_address[1], "lab" if args.lab else "equipment"), flush=True)
            print("Ctrl+C — остановить атлас, hw-mcp и симулятор", flush=True)
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as exc:
            print("atlas-demo: %s" % exc, file=sys.stderr)
            return 1
        finally:
            if srv:
                srv.server_close()
            # Stop the consumer before removing the simulator's devices.
            for child in reversed(children):
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
