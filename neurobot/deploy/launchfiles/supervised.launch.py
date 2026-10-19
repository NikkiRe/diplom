"""Обёртка ros2 launch для юнитов neurobot (драйверы, rosbridge): запускает launch-файл компонента и гасит весь launch,
когда любой его процесс завершился. Тогда выходит и юнит, а systemd поднимает его снова (Restart=always). Без обёртки
ros2 launch переживает смерть своего узла (kill -9, падение драйвера): юнит active, а узла нет — так rosbridge после
kill -9 узла rosbridge_websocket не поднялся до конца прогона main_a1 [замер].

    ros2 launch <prefix>/share/launchfiles/supervised.launch.py include:=ПАКЕТ/ФАЙЛ|/путь [oneshot:=a,b] [имя:=знач…]

include — что запустить: «пакет/файл» (файл ищется в share пакета, как у «ros2 launch пакет файл») или путь к
.py/.xml/.yaml. Прочие аргументы командной строки передаются включённому файлу его аргументами (launch_arguments):
обязательный аргумент без значения по умолчанию иначе не прошёл бы проверку IncludeLaunchDescription. oneshot —
имена исполняемых, которые завершаются сами, сделав своё (spawner контроллеров, create двойника): их выход с кодом 0
launch не гасит, выход с ошибкой — гасит. Своя остановка (SIGINT юниту) — не повод: выходы при ней не считаются.

Только API launch, которое есть и в Foxy (launch 0.10): IncludeLaunchDescription, AnyLaunchDescriptionSource,
RegisterEventHandler, OnProcessExit без target_action (любой процесс), Shutdown, OpaqueFunction. В Foxy launch по
Shutdown выходит с кодом 0 — systemd с Restart=always перезапускает и так.
"""
import os

from launch import LaunchDescription
from launch.actions import (DeclareLaunchArgument, IncludeLaunchDescription, OpaqueFunction, RegisterEventHandler,
                            Shutdown)
from launch.event_handlers import OnProcessExit
from launch.launch_description_sources import AnyLaunchDescriptionSource
from launch.logging import get_logger

ONESHOT = "spawner,spawner.py,create"


def resolve(spec: str) -> str:
    """Путь launch-файла: «/путь» как есть, «пакет/файл» — в share пакета."""
    if os.path.isabs(spec):
        if not os.path.isfile(spec):
            raise RuntimeError("include: нет файла {}".format(spec))
        return spec
    package, sep, name = spec.partition("/")
    if not sep or not package or not name:
        raise RuntimeError("include: ожидалось ПАКЕТ/ФАЙЛ или абсолютный путь, получено {!r}".format(spec))
    from ros2launch.api import get_share_file_path_from_package
    return get_share_file_path_from_package(package_name=package, file_name=name)


def program(cmd) -> str:
    """Имя исполняемого процесса: первый аргумент, а у «python3 скрипт» — скрипт."""
    names = [os.path.basename(str(c)) for c in cmd or []]
    if len(names) > 1 and names[0].startswith("python"):
        return names[1]
    return names[0] if names else "?"


def on_exit(oneshot):
    def handle(event, context):
        name = program(event.cmd)
        if context.is_shutdown or (event.returncode == 0 and name in oneshot):
            return None
        get_logger("supervised").error("процесс {} ({}) завершился с кодом {} — гашу launch: юнит перезапустит "
                                       "systemd".format(event.process_name, name, event.returncode))
        return [Shutdown(reason="{} завершился ({})".format(name, event.returncode))]
    return handle


def _setup(context):
    cfg = context.launch_configurations
    path = resolve(cfg["include"])
    oneshot = {n.strip() for n in cfg.get("oneshot", ONESHOT).split(",") if n.strip()}
    args = [(k, v) for k, v in sorted(cfg.items()) if k not in ("include", "oneshot")]
    return [RegisterEventHandler(OnProcessExit(on_exit=on_exit(oneshot))),
            IncludeLaunchDescription(AnyLaunchDescriptionSource(path), launch_arguments=args)]


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("include", description="ПАКЕТ/ФАЙЛ или абсолютный путь launch-файла компонента"),
        DeclareLaunchArgument("oneshot", default_value=ONESHOT,
                              description="исполняемые, чей выход с кодом 0 launch не гасит (через запятую)"),
        OpaqueFunction(function=_setup),
    ])
