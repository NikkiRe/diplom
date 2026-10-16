"""Цифровой двойник робота NeuroChip: Gazebo Harmonic + ros2_control + граф ROS 2 как на роботе (README двойника).

/cmd_vel (Twist; пишет safety_gate) → twist_mux (cmd_vel 10, cmd_vel_tracker 20, cmd_vel_joy 100; 0,5 с) →
/diff_cont/cmd_vel_unstamped (Twist) → cmd_relay.py → /diff_cont/cmd_vel (TwistStamped) → diff_drive_controller
(gz_ros2_control) → колёса. Звено cmd_relay есть только на двойнике: diff_drive_controller в Jazzy принимает лишь
TwistStamped, а в Foxy на роботе — /diff_cont/cmd_vel_unstamped сам (use_stamped_vel: false).
Датчики: /scan (через sensor_model.py), /camera/color/*, /camera/depth/* (через sensor_model.py), /camera/imu →
imu_filter_madgwick → /imu/data; /odom и /tf — diff_cont, /joint_states — joint_broad, остальное дерево TF —
robot_state_publisher. Истина: /ground_truth/pose, /twin/contacts, /twin/models (логическая камера twin_eye, которую
launch добавляет в копию мира run_dir/world.sdf), /twin/truth (truth.py). rosbridge — только 127.0.0.1, порт
rosbridge_port.

Аргументы (умолчания — twin_config.py): world (lab|step|ramp|путь.sdf), x_pose, y_pose, z_pose, yaw (пусто — поза
спавна мира), cmd_vel_timeout (с; 0 — hold), lidar_hz, color_hz, depth_hz, imu_hz, truth_hz, rosbridge,
rosbridge_port, run_dir, gz_verbose. Каждый запуск — в своём разделе gz-transport (GZ_PARTITION = twin_<каталог
прогона>, run_dir/gz_partition). Если в ВМ остался `gz sim` прошлого двойника (сирота из того же корня прогонов), launch
не стартует. Выход любого узла «железа» (мост, sensor_model, twist_mux, cmd_relay, robot_state_publisher,
madgwick, truth) гасит всю группу процессов двойника (stand_procs.py down). Время симуляции — только у узлов, которым
оно нужно (robot_state_publisher, madgwick, мост); узлы двойника на Python работают по монотонным часам ВМ: /clock идёт
с частотой шага физики (1 кГц), и его разбор в rclpy стоит ≈25 % ядра на узел [замер].
"""
import os
import subprocess
import sys

import xacro
import yaml
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (DeclareLaunchArgument, IncludeLaunchDescription, OpaqueFunction, SetEnvironmentVariable,
                            Shutdown)
from launch.launch_description_sources import AnyLaunchDescriptionSource, PythonLaunchDescriptionSource
from launch.logging import get_logger
from launch_ros.actions import Node

TWIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAND = os.path.join(os.path.dirname(TWIN), 'stand')
sys.path[:0] = [TWIN, STAND]
import twin_config as cfg  # noqa: E402
from stand_procs import brief, gz_sim, snapshot  # noqa: E402
from truth import with_eye  # noqa: E402

TRUE = ('1', 'true', 'yes')
# Ожидания spawner, с: при RTF ≪ 1 (ВМ занята) переключение контроллера по умолчанию (5 с) не успевает [замер].
SPAWN_TIMEOUTS = ('--controller-manager-timeout', '60', '--switch-timeout', '60', '--service-call-timeout', '60')
_DOWN = []      # группу уже гасят: остальные узлы выходят следом, второй раз не гасим


def _hardware_exit(name, run_dir):
    """Узел «железа» завершился: гасим всю группу двойника отдельным процессом вне группы (как стенд)."""
    def on_exit(event, context):
        if context.is_shutdown or _DOWN:
            return None
        _DOWN.append(name)
        if os.getpgrp() != os.getpid():      # не через twin_vm.sh (ros2 launch через setsid — лидер своей группы)
            return [Shutdown(reason='%s завершился' % name)]
        get_logger('twin').error('%s завершился (код %s) — гашу группу двойника %d' % (
            name, event.returncode, os.getpgrp()))
        with open(os.path.join(run_dir, 'twin_down.log'), 'ab') as log:
            subprocess.Popen([sys.executable, os.path.join(STAND, 'stand_procs.py'), 'down', '--pgid',
                              str(os.getpgrp())], start_new_session=True, stdin=subprocess.DEVNULL, stdout=log,
                             stderr=log)
        return None
    return on_exit


def orphan_gz(procs, run_root, pgid):
    """Серверы `gz sim` прошлых двойников: мир из run_root, группа — не наша. Стенд и чужие миры — не сироты двойника:
    у них свой раздел gz-transport."""
    return [p for p in gz_sim(procs, exclude_pgid=pgid) if run_root + '/' in ' '.join(p.argv)]


def bridge_config(world_name):
    """Мост gz ↔ ROS: темы робота (R7a §1.5, realsense2_camera), истина и сервисы мира. Камера ленивая: без
    подписчика в ROS мост не подписан в gz, и Gazebo её не рендерит."""
    def t(ros, gz, ros_type, gz_type, lazy=False):
        e = {'ros_topic_name': ros, 'gz_topic_name': gz, 'ros_type_name': ros_type, 'gz_type_name': gz_type,
             'direction': 'GZ_TO_ROS'}
        if lazy:
            e['lazy'] = True
        return e
    img, info = 'sensor_msgs/msg/Image', 'sensor_msgs/msg/CameraInfo'
    out = [
        t('/clock', '/clock', 'rosgraph_msgs/msg/Clock', 'gz.msgs.Clock'),
        t(cfg.SCAN_RAW, '/twin/scan', 'sensor_msgs/msg/LaserScan', 'gz.msgs.LaserScan'),
        t('/camera/imu', '/twin/imu', 'sensor_msgs/msg/Imu', 'gz.msgs.IMU'),
        t('/camera/color/image_raw', '/twin/color/image', img, 'gz.msgs.Image', lazy=True),
        t('/camera/color/camera_info', '/twin/color/camera_info', info, 'gz.msgs.CameraInfo', lazy=True),
        t(cfg.DEPTH_RAW, '/twin/depth/image', img, 'gz.msgs.Image', lazy=True),
        t('/camera/depth/camera_info', '/twin/depth/camera_info', info, 'gz.msgs.CameraInfo', lazy=True),
        t('/ground_truth/pose', '/model/%s/pose' % cfg.MODEL, 'geometry_msgs/msg/PoseStamped', 'gz.msgs.Pose'),
        t('/twin/contacts', '/twin/contacts', 'ros_gz_interfaces/msg/Contacts', 'gz.msgs.Contacts'),
        t(cfg.MODELS_TOPIC, '/twin/models', 'ros_gz_interfaces/msg/LogicalCameraImage', 'gz.msgs.LogicalCameraImage'),
    ]
    for svc, ros_t, req_t in (('control', 'ControlWorld', 'gz.msgs.WorldControl'),
                              ('create', 'SpawnEntity', 'gz.msgs.EntityFactory'),
                              ('remove', 'DeleteEntity', 'gz.msgs.Entity'),
                              ('set_pose', 'SetEntityPose', 'gz.msgs.Pose')):
        out.append({'service_name': '/world/%s/%s' % (world_name, svc),
                    'ros_type_name': 'ros_gz_interfaces/srv/' + ros_t, 'gz_req_type_name': req_t,
                    'gz_rep_type_name': 'gz.msgs.Boolean'})
    return out


def controllers_config(src, cmd_vel_timeout):
    """Параметры controller_manager из config/controllers.yaml с подставленным cmd_vel_timeout (с, ≥ 0)."""
    if not cmd_vel_timeout >= 0:
        raise RuntimeError('cmd_vel_timeout должен быть ≥ 0 с (0 — hold), получено %r' % cmd_vel_timeout)
    with open(src) as fh:
        params = yaml.safe_load(fh)
    params['diff_cont']['ros__parameters']['cmd_vel_timeout'] = float(cmd_vel_timeout)
    return params


def _twin(ctx):
    a = dict(ctx.launch_configurations)
    run_dir = a['run_dir']
    orphans = orphan_gz(snapshot(), os.path.dirname(os.path.normpath(run_dir)), os.getpgrp())
    if orphans:
        raise RuntimeError('в ВМ остался gz sim прошлого двойника: %s; остановить: twin_vm.sh stop <его run_dir> или '
                           'kill -INT -- -<pgid>' % brief(orphans))
    os.makedirs(run_dir, exist_ok=True)
    world_src, spawn = cfg.world(a['world'])
    pose = [a[k] or str(v) for k, v in zip(('x_pose', 'y_pose', 'z_pose', 'yaw'), spawn)]
    partition = 'twin_' + os.path.basename(os.path.normpath(run_dir))

    def write(name, text):
        path = os.path.join(run_dir, name)
        with open(path, 'w') as fh:
            fh.write(text)
        return path

    write('gz_partition', partition + '\n')
    with open(world_src) as fh:
        world_out = write('world.sdf', with_eye(fh.read()))
    controllers = write('controllers.yaml', yaml.safe_dump(
        controllers_config(os.path.join(TWIN, 'config', 'controllers.yaml'), float(a['cmd_vel_timeout'])),
        sort_keys=False))
    urdf = xacro.process_file(os.path.join(TWIN, 'urdf', 'neurochip.urdf.xacro'), mappings={
        'controllers': controllers, **{k: a[k] for k in ('lidar_hz', 'color_hz', 'depth_hz', 'imu_hz', 'truth_hz')}
    }).toxml()
    urdf_path = write('robot.urdf', urdf)
    bridge = write('bridge.yaml', yaml.safe_dump(bridge_config(cfg.WORLD_NAME), sort_keys=False))

    sim = {'use_sim_time': True}

    def node(name, **kw):
        return Node(name=name, output='log', on_exit=_hardware_exit(name, run_dir), **kw)

    ros_gz_sim = get_package_share_directory('ros_gz_sim')
    actions = [
        SetEnvironmentVariable('GZ_PARTITION', partition),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(os.path.join(ros_gz_sim, 'launch', 'gz_sim.launch.py')),
            launch_arguments={'gz_args': '-r -s --headless-rendering -v%s %s' % (a['gz_verbose'], world_out),
                              'on_exit_shutdown': 'true'}.items()),
        node('robot_state_publisher', package='robot_state_publisher', executable='robot_state_publisher',
             parameters=[sim, {'robot_description': urdf}]),
        Node(package='ros_gz_sim', executable='create', output='log',
             arguments=['-world', cfg.WORLD_NAME, '-name', cfg.MODEL, '-file', urdf_path,
                        '-x', pose[0], '-y', pose[1], '-z', pose[2], '-Y', pose[3]]),
        node('gz_bridge', package='ros_gz_bridge', executable='parameter_bridge',
             parameters=[sim, {'config_file': bridge}]),
        Node(package='controller_manager', executable='spawner', output='log',
             arguments=['joint_broad', *SPAWN_TIMEOUTS]),
        Node(package='controller_manager', executable='spawner', output='log',
             arguments=['diff_cont', *SPAWN_TIMEOUTS, '--controller-ros-args', '-r /diff_cont/odom:=/odom']),
        node('twist_mux', package='twist_mux', executable='twist_mux',
             parameters=[os.path.join(TWIN, 'config', 'twist_mux.yaml')],
             remappings=[('cmd_vel_out', cfg.MUX_OUT)]),
        node('twin_cmd_relay', executable=sys.executable, arguments=[os.path.join(TWIN, 'cmd_relay.py')]),
        node('imu_filter_madgwick', package='imu_filter_madgwick', executable='imu_filter_madgwick_node',
             parameters=[sim, {'use_mag': False, 'publish_tf': False, 'world_frame': 'enu'}],
             remappings=[('imu/data_raw', '/camera/imu'), ('imu/data', '/imu/data')]),
        node('twin_sensor_model', executable=sys.executable, arguments=[os.path.join(TWIN, 'sensor_model.py')]),
        node('twin_truth', executable=sys.executable, arguments=[os.path.join(TWIN, 'truth.py')],
             parameters=[{'world_sdf': world_out, 'robot_urdf': urdf_path, 'rate_hz': cfg.TRUTH_HZ}]),
    ]
    if a['rosbridge'].lower() in TRUE:
        actions.append(IncludeLaunchDescription(
            AnyLaunchDescriptionSource(os.path.join(get_package_share_directory('rosbridge_server'), 'launch',
                                                    'rosbridge_websocket_launch.xml')),
            launch_arguments={'port': a['rosbridge_port'], 'address': '127.0.0.1'}.items()))
    return actions


def generate_launch_description():
    return LaunchDescription([DeclareLaunchArgument(n, default_value=v) for n, v in cfg.LAUNCH_ARGS] +
                             [OpaqueFunction(function=_twin)])
