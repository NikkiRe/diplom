"""Стенд робота NeuroChip (профиль neurobot): scripts/sim/launch/robot_sim.launch.py + stm32_sim (+ scan_mask).

/cmd_vel (тип — stm32_cmd_vel_type, на роботе Foxy — Twist) → stm32_sim → /stand/base_cmd_vel (Twist) →
ros_gz_bridge → DiffDrive. Аргументы базового launch (мир, лидар, частоты, rosbridge) проходят насквозь; их значения
для профиля задаёт scripts/sim/profiles/neurobot.sh. Свои аргументы:
  base                    tb3 | neurochip — модель базы (stand_config.BASES); lidar_range_min, если не задан явно, —
                          по базе;
  scan_occluder, scan_codes  true — /scan идёт через scan_mask (NaN окклюдера D435i, коды URG; scan_mask.py);
  stm32_<параметр>        параметры stm32_sim (stand_config.STM32_DEFAULTS, смысл — stm32_sim.py).
Каждый стенд — в своём разделе gz-transport (GZ_PARTITION = neurobot_<имя каталога прогона>, записан в
run_dir/gz_partition): сирота прошлого прогона не может подменить мир. Если в ВМ уже работает чужой `gz sim`, launch
не стартует. stm32_sim и scan_mask — «железо» стенда: выход любого из них гасит всю группу процессов стенда.
"""
import os
import subprocess
import sys

from launch import LaunchDescription
from launch.actions import (DeclareLaunchArgument, ExecuteProcess, IncludeLaunchDescription, OpaqueFunction,
                            SetEnvironmentVariable, Shutdown)
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.logging import get_logger

HERE = os.path.dirname(os.path.abspath(__file__))
STAND = os.path.dirname(HERE)
sys.path.insert(0, STAND)
from stand_config import BASES, DEFAULT_BASE, STM32_DEFAULTS  # noqa: E402
from stand_procs import brief, gz_sim, snapshot  # noqa: E402

BASE = os.path.normpath(os.path.join(STAND, '..', '..', '..', 'scripts', 'sim', 'launch', 'robot_sim.launch.py'))
BASE_CMD_VEL = '/stand/base_cmd_vel'
SCAN_RAW = '/stand/scan_raw'
TRUE = ('1', 'true', 'yes')


def _hardware_exit(name, run_dir):
    """Выход stm32_sim или scan_mask: гасим всю группу стенда, как vm_stop.sh, — отдельным процессом вне группы
    (stand_procs.py down: SIGINT, затем SIGTERM и SIGKILL). Shutdown одного launch сироту не убирает: обёртку ruby
    `gz sim` launch добивает SIGTERM, а её дочерний сервер gz переживает её; бывает, что сервер не отвечает и на
    SIGINT/SIGTERM группе."""
    def on_exit(event, context):
        if context.is_shutdown:
            return None
        if os.getpgrp() != os.getpid():      # не через vm_start.sh (ros2 launch через setsid — лидер своей группы)
            return [Shutdown(reason='%s завершился' % name)]
        get_logger('neurobot_stand').error('%s завершился (код %s) — гашу группу стенда %d' % (
            name, event.returncode, os.getpgrp()))
        with open(os.path.join(run_dir, 'stand_down.log'), 'ab') as log:
            subprocess.Popen([sys.executable, os.path.join(STAND, 'stand_procs.py'), 'down', '--pgid',
                              str(os.getpgrp())], start_new_session=True, stdin=subprocess.DEVNULL, stdout=log,
                             stderr=log)
        return None
    return on_exit


def _stand(ctx):
    cfg = ctx.launch_configurations
    foreign = gz_sim(snapshot(), exclude_pgid=os.getpgrp())
    if foreign:
        raise RuntimeError('в ВМ уже работает gz sim не из этого стенда — сирота прошлого прогона? %s; остановить: '
                           'kill -INT -- -<pgid>' % brief(foreign))
    base_name = cfg['base']
    if base_name not in BASES:
        raise RuntimeError('base должен быть одним из %s, получено %r' % (sorted(BASES), base_name))
    base = BASES[base_name]
    run_dir = cfg.get('run_dir', '/tmp/robot_sim')
    partition = 'neurobot_' + os.path.basename(os.path.normpath(run_dir))
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, 'gz_partition'), 'w') as fh:
        fh.write(partition + '\n')
    occluder, codes = cfg['scan_occluder'].lower() in TRUE, cfg['scan_codes'].lower() in TRUE
    # Вход DiffDrive — внутренняя тема стенда, всегда Twist; /cmd_vel принадлежит stm32_sim.
    base_args = {'cmd_vel_type': 'Twist', 'cmd_vel_topic': BASE_CMD_VEL,
                 'lidar_range_min': cfg.get('lidar_range_min') or base.range_min}
    if base.model_sdf:
        base_args.update(model_sdf=base.model_sdf, robot_urdf=base.robot_urdf)
    if occluder or codes:
        base_args['scan_topic'] = SCAN_RAW
    stm32 = ['python3', os.path.join(STAND, 'stm32_sim.py'), '--ros-args', '-p', 'output_topic:=' + BASE_CMD_VEL]
    for name, _ in STM32_DEFAULTS:
        stm32 += ['-p', '%s:=%s' % (name, cfg['stm32_' + name])]
    actions = [
        SetEnvironmentVariable('GZ_PARTITION', partition),
        IncludeLaunchDescription(PythonLaunchDescriptionSource(BASE), launch_arguments=base_args.items()),
        ExecuteProcess(name='stm32_sim', output='log', cmd=stm32, on_exit=_hardware_exit('stm32_sim', run_dir)),
    ]
    if occluder or codes:
        actions.append(ExecuteProcess(
            name='scan_mask', output='log', on_exit=_hardware_exit('scan_mask', run_dir),
            cmd=['python3', os.path.join(STAND, 'scan_mask.py'), '--ros-args', '-p', 'input_topic:=' + SCAN_RAW,
                 '-p', 'occluder:=%s' % str(occluder).lower(), '-p', 'codes:=%s' % str(codes).lower()]))
    return actions


def generate_launch_description():
    args = [('base', DEFAULT_BASE), ('scan_occluder', 'false'), ('scan_codes', 'false')]
    args += [('stm32_' + n, v) for n, v in STM32_DEFAULTS]
    return LaunchDescription([DeclareLaunchArgument(n, default_value=v) for n, v in args] +
                             [OpaqueFunction(function=_stand)])
