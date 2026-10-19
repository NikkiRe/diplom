"""Обёртка ros2 launch юнитов (neurobot/deploy/launchfiles/supervised.launch.py): смерть любого процесса гасит launch,
выход «одноразового» с кодом 0 — нет, ошибка одноразового — гасит; аргументы командной строки (и обязательные)
доходят до включённого файла.

Нужен пакет launch: в ВМ sim — Jazzy (source /opt/ros/jazzy/setup.bash), для Foxy — исходники launch 0.10 в PYTHONPATH
(README deploy, «Проверка»); без него тесты пропускаются. Запуск — как у ros2 launch (ros2launch.api.
launch_a_launch_file: LaunchService(argv) и IncludeLaunchDescription с аргументами командной строки), а при
NEUROBOT_TEST_ROS2_LAUNCH=1 — ещё и сам `ros2 launch`.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "launchfiles" / "supervised.launch.py"

try:
    import launch  # noqa: F401
except ImportError:
    launch = None
if launch is not None and not hasattr(launch, "LaunchService"):
    launch = None  # каталог launch/ двойника на sys.path (тесты neurobot/sim/twin) — пакет-пространство, не ROS

RUN = textwrap.dedent("""
    import sys
    import launch
    from launch.actions import IncludeLaunchDescription
    from launch.launch_description_sources import AnyLaunchDescriptionSource
    args = sys.argv[2:]
    service = launch.LaunchService(argv=args)
    service.include_launch_description(launch.LaunchDescription([IncludeLaunchDescription(
        AnyLaunchDescriptionSource(sys.argv[1]), launch_arguments=[a.split(":=", 1) for a in args])]))
    sys.exit(service.run())
""")

# обязательный аргумент role без значения по умолчанию: обёртка обязана передать его включённому файлу
CHILD = textwrap.dedent("""
    from launch import LaunchDescription
    from launch.actions import DeclareLaunchArgument, ExecuteProcess
    from launch.substitutions import LaunchConfiguration
    def generate_launch_description():
        return LaunchDescription([DeclareLaunchArgument("role"),
                                  ExecuteProcess(cmd=["{dir}/victim", LaunchConfiguration("role")], name="victim"),
                                  ExecuteProcess(cmd=["{dir}/spawner"], name="spawner")])
""")


def script(path: str, body: str) -> None:
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, 0o755)


def pid_of(name: str, parent: int) -> int:
    """PID дочернего процесса parent, в командной строке которого есть name (pgrep: Linux и macOS)."""
    out = subprocess.run(["pgrep", "-P", str(parent), "-f", name], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else 0


@unittest.skipIf(launch is None, "нужен пакет launch (ВМ: Jazzy; Foxy: исходники launch 0.10 в PYTHONPATH)")
class Supervised(unittest.TestCase):
    def start(self, spawner_exit: int, cli: bool = False):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        script(os.path.join(d.name, "victim"), "trap 'exit 0' INT TERM\nwhile true; do sleep 0.2; done")
        script(os.path.join(d.name, "spawner"), "sleep 0.5\nexit %d" % spawner_exit)
        child = os.path.join(d.name, "child.launch.py")
        with open(child, "w") as f:
            f.write(CHILD.format(dir=d.name))
        args = ["include:=" + child, "oneshot:=spawner", "role:=lidar"]
        argv = (["ros2", "launch", str(WRAPPER)] + args if cli else
                [sys.executable, "-c", RUN, str(WRAPPER)] + args)
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        self.addCleanup(self._kill, proc)
        return proc

    @staticmethod
    def _kill(proc) -> None:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(10)

    def victim(self, proc) -> int:
        end = time.monotonic() + 15.0
        while time.monotonic() < end:
            pid = pid_of("victim", proc.pid)
            if pid:
                return pid
            time.sleep(0.1)
        self.fail("victim не запустился: " + (proc.stdout.read().decode() if proc.poll() is not None else ""))

    def check_kill(self, cli: bool) -> None:
        proc = self.start(0, cli)
        pid = self.victim(proc)
        time.sleep(2.0)
        self.assertIsNone(proc.poll(), "выход одноразового с кодом 0 не должен гасить launch")
        self.assertIn("lidar", subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True,
                                              text=True).stdout)  # аргумент дошёл до включённого файла
        os.kill(pid, signal.SIGKILL)
        rc = proc.wait(20)
        out = proc.stdout.read().decode()
        self.assertIn("гашу launch", out)
        self.assertIn("victim", out)
        self.assertIsNotNone(rc)

    def test_node_death_shuts_launch_down(self):
        self.check_kill(cli=False)

    def test_failed_oneshot_shuts_launch_down(self):
        proc = self.start(3)
        self.victim(proc)
        proc.wait(20)
        self.assertIn("spawner", proc.stdout.read().decode())

    @unittest.skipUnless(os.environ.get("NEUROBOT_TEST_ROS2_LAUNCH") == "1", "только с NEUROBOT_TEST_ROS2_LAUNCH=1")
    def test_ros2_launch_cli(self):
        self.check_kill(cli=True)


if __name__ == "__main__":
    unittest.main()
