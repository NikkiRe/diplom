"""Headless-симуляция TurtleBot3 (Gazebo Harmonic + ROS 2 Jazzy) для сценария «робот» ВКР.

Отличия от turtlebot3_gazebo/turtlebot3_*.launch.py:
  * только gz-сервер (`-s`), без gzclient; рендеринг сенсоров — EGL headless (`--headless-rendering`), Mesa llvmpipe;
  * мир и модель робота копируются в run_dir с правками: real_time_factor, шаг физики, частоты сенсоров
    (выключенный сенсор удаляется из модели), камера по умолчанию «ленивая» (camera:=lazy),
    Fuel-включения (Ground Plane, Sun) заменены встроенными — старт без сети и без обращения к fuel.gazebosim.org;
  * опционально: Nav2 (nav2_bringup/bringup_launch.py без RViz, карта turtlebot3_world, начальная поза AMCL),
    rosbridge_server (WebSocket) + rosapi.

Аргументы: world (turtlebot3_world|turtlebot3_house|empty_world|путь.sdf), rtf, step (max_step_size, с; пусто — как в мире), x_pose, y_pose, yaw,
camera (stream — как в TB3, рендер всегда; lazy — рендер только при подписчике; off), camera_hz (0 — off), jpeg_quality (JPEG /camera/image_raw/compressed в lazy),
lidar_hz (0 — лидар удалён), imu_hz, odom_hz, ground_truth_hz (истинная поза /ground_truth/pose, 0 — нет), nav2, rosbridge, rosbridge_port,
world_control (сервисы /world/<мир>/{control,create,remove,set_pose} в ROS: пауза, multi_step, run_to_sim_time —
pошаговый режим; сценарные модели — препятствие, «человек»), run_dir, gz_verbose.

Для профилей стенда (пусто / false — как в TurtleBot3, поведение по умолчанию не меняется):
lidar_samples, lidar_min_angle, lidar_max_angle (рад), lidar_range_min, lidar_range_max (м) — геометрия лидара;
cmd_vel_type (Twist|TwistStamped) и cmd_vel_topic — тип и имя ROS-темы, которую мост отдаёт в DiffDrive;
scan_topic — имя ROS-темы лидара (ретранслятор стенда публикует /scan сам);
model_sdf — SDF другой модели робота с теми же темами gz, что у TurtleBot3 (cmd_vel, odom, tf, joint_states, imu,
scan; сенсоры imu и gpu_lidar обязательны), robot_urdf — её URDF для robot_state_publisher; сущность в Gazebo
по-прежнему называется $TURTLEBOT3_MODEL;
odom_hz_fix — писать частоту одометрии под именем odom_publish_frequency, которое читает DiffDrive gz-sim 8: модель
TurtleBot3 задаёт odom_publisher_frequency, плагин его не знает, и без исправления /odom идёт с частотой плагина по
умолчанию (50 Гц), а odom_hz ни на что не влияет.
"""
import os
import re

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (DeclareLaunchArgument, IncludeLaunchDescription, OpaqueFunction, SetEnvironmentVariable,
                            TimerAction)
from launch.launch_description_sources import AnyLaunchDescriptionSource, PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

MODEL = os.environ.get('TURTLEBOT3_MODEL', 'waffle_pi')

GROUND = """<model name="ground_plane"><static>true</static><link name="link">
<collision name="collision"><geometry><plane><normal>0 0 1</normal><size>100 100</size></plane></geometry></collision>
<visual name="visual"><geometry><plane><normal>0 0 1</normal><size>100 100</size></plane></geometry>
<material><ambient>0.8 0.8 0.8 1</ambient><diffuse>0.8 0.8 0.8 1</diffuse><specular>0.8 0.8 0.8 1</specular></material>
</visual></link></model>"""
SUN = """<light type="directional" name="sun"><cast_shadows>true</cast_shadows><pose>0 0 10 0 0 0</pose>
<diffuse>0.8 0.8 0.8 1</diffuse><specular>0.2 0.2 0.2 1</specular>
<attenuation><range>1000</range><constant>0.9</constant><linear>0.01</linear><quadratic>0.001</quadratic></attenuation>
<direction>-0.5 0.1 -0.9</direction></light>"""

DEFAULT_POSE = {'turtlebot3_world': (-2.0, -0.5), 'turtlebot3_house': (-2.0, -0.5), 'empty_world': (0.0, 0.0)}


def _f(ctx, name):
    return LaunchConfiguration(name).perform(ctx)


def _set_rate(sdf: str, sensor_type: str, hz: float) -> str:
    """Поменять <update_rate> у <sensor type=...>; hz <= 0 — удалить сенсор из модели.

    Удаление, а не update_rate=0: в gz-sensors 0 означает «без ограничения» — рендер-сенсор обновляется на каждом
    кадре рендеринга (при лидаре 10 Гц камера с rate 0 давала те же 10 Гц)."""
    pat = re.compile(r'(<sensor[^>]*type="%s"[^>]*>)(.*?)(</sensor>)' % sensor_type, re.S)

    def rep(m):
        if hz <= 0:
            return ''
        body = re.sub(r'<update_rate>[^<]*</update_rate>', '<update_rate>%g</update_rate>' % hz, m.group(2))
        return m.group(1) + body + m.group(3)

    out, n = pat.subn(rep, sdf)
    if n == 0:
        raise RuntimeError('sensor type=%s not found in model' % sensor_type)
    return out


def _set_lidar(sdf: str, samples: str, min_angle: str, max_angle: str, range_min: str, range_max: str) -> str:
    """Геометрия <sensor type="gpu_lidar">: число лучей, углы горизонтальной развёртки, дальности;
    пустое значение — как в модели.

    gz-sensors раскладывает samples лучей от min_angle до max_angle включительно: шаг = (max − min) / (samples − 1)."""
    tags = (('samples', samples), ('min_angle', min_angle), ('max_angle', max_angle), ('min', range_min),
            ('max', range_max))
    if not any(v for _, v in tags):
        return sdf
    m = re.search(r'<sensor[^>]*type="gpu_lidar"[^>]*>.*?</sensor>', sdf, re.S)
    if m is None:
        raise RuntimeError('sensor type=gpu_lidar not found in model')
    body = m.group(0)
    for tag, value in tags:
        if value:
            body, n = re.subn(r'<%s>[^<]*</%s>' % (tag, tag), '<%s>%s</%s>' % (tag, value, tag), body, count=1)
            if n == 0:
                raise RuntimeError('<%s> not found in gpu_lidar' % tag)
    return sdf[:m.start()] + body + sdf[m.end():]


def _bridge_config(src: str, run_dir: str, camera: str, lidar_on: bool, gt_model: str | None = None,
                   control_world: str | None = None, cmd_vel_type: str = '', cmd_vel_topic: str = '',
                   scan_topic: str = '') -> str:
    """Конфиг ros_gz_bridge: как у turtlebot3_gazebo, но темы выключенных сенсоров убраны, а в режиме camera=lazy
    изображение и camera_info идут через parameter_bridge с lazy: true — подписка на стороне Gazebo появляется
    только при подписчике в ROS, и тогда же gz-sim начинает рендерить камеру. cmd_vel_type / cmd_vel_topic (если
    заданы) меняют тип и имя ROS-стороны входа DiffDrive; scan_topic (если задан) — имя ROS-темы лидара."""
    import yaml
    with open(src) as fh:
        entries = yaml.safe_load(fh)
    out = []
    for e in entries:
        t = e['ros_topic_name']
        if t.startswith('camera/') and camera == 'off':
            continue
        if t == 'scan' and not lidar_on:
            continue
        if t == 'scan' and scan_topic:
            e = dict(e, ros_topic_name=scan_topic)
        if t.startswith('camera/') and camera == 'lazy':
            e = dict(e, lazy=True)
        if t == 'cmd_vel':
            if cmd_vel_type:
                e = dict(e, ros_type_name='geometry_msgs/msg/' + cmd_vel_type)
            if cmd_vel_topic:
                e = dict(e, ros_topic_name=cmd_vel_topic)
        out.append(e)
    if camera == 'lazy':
        out.append({'ros_topic_name': 'camera/image_raw', 'gz_topic_name': 'camera/image_raw',
                    'ros_type_name': 'sensor_msgs/msg/Image', 'gz_type_name': 'gz.msgs.Image',
                    'direction': 'GZ_TO_ROS', 'lazy': True})
    if gt_model:
        out.append({'ros_topic_name': 'ground_truth/pose', 'gz_topic_name': '/model/%s/pose' % gt_model,
                    'ros_type_name': 'geometry_msgs/msg/PoseStamped', 'gz_type_name': 'gz.msgs.Pose',
                    'direction': 'GZ_TO_ROS', 'lazy': True})
    if control_world:
        # Управление миром из ROS (и через rosbridge): пауза, multi_step, run_to_sim_time — для пошагового режима
        # (часы стенда ведёт реплеер). Сервис без вызовов ничего не стоит.
        # Сценарные события стенда (препятствие, «человек»): создать / удалить / переставить модель.
        for svc, ros_t, req_t in (('control', 'ControlWorld', 'gz.msgs.WorldControl'),
                                  ('create', 'SpawnEntity', 'gz.msgs.EntityFactory'),
                                  ('remove', 'DeleteEntity', 'gz.msgs.Entity'),
                                  ('set_pose', 'SetEntityPose', 'gz.msgs.Pose')):
            out.append({'service_name': '/world/%s/%s' % (control_world, svc),
                        'ros_type_name': 'ros_gz_interfaces/srv/' + ros_t,
                        'gz_req_type_name': req_t, 'gz_rep_type_name': 'gz.msgs.Boolean'})
    path = os.path.join(run_dir, 'bridge.yaml')
    with open(path, 'w') as fh:
        yaml.safe_dump(out, fh, sort_keys=False)
    return path


def _prepare(ctx):
    tb3 = get_package_share_directory('turtlebot3_gazebo')
    run_dir = _f(ctx, 'run_dir')
    os.makedirs(os.path.join(run_dir, 'models'), exist_ok=True)

    # --- мир ---
    world_arg = _f(ctx, 'world')
    world_src = world_arg if world_arg.endswith(('.sdf', '.world')) else os.path.join(tb3, 'worlds', world_arg + '.world')
    with open(world_src) as fh:
        world = fh.read()
    world = re.sub(r'<include>\s*<uri>https://fuel\.gazebosim\.org[^<]*Ground Plane</uri>\s*</include>', GROUND, world)
    world = re.sub(r'<include>\s*<uri>https://fuel\.gazebosim\.org[^<]*Sun</uri>\s*</include>', SUN, world)
    if 'fuel.gazebosim.org' in world:
        raise RuntimeError('world still references Fuel: ' + world_src)
    rtf = float(_f(ctx, 'rtf'))
    world = re.sub(r'<real_time_factor>[^<]*</real_time_factor>', '<real_time_factor>%g</real_time_factor>' % rtf, world)
    # gz-sim: период шага = max_step_size / real_time_factor; real_time_update_rate держим согласованным.
    step_arg = _f(ctx, 'step')
    if step_arg:
        world = re.sub(r'<max_step_size>[^<]*</max_step_size>', '<max_step_size>%g</max_step_size>' % float(step_arg),
                       world)
    step = float(re.search(r'<max_step_size>([^<]*)</max_step_size>', world).group(1))
    upd = 0.0 if rtf <= 0 else rtf / step
    world = re.sub(r'<real_time_update_rate>[^<]*</real_time_update_rate>',
                   '<real_time_update_rate>%g</real_time_update_rate>' % upd, world)
    world_name = re.search(r'<world name="([^"]+)"', world).group(1)
    world_out = os.path.join(run_dir, 'world.sdf')
    with open(world_out, 'w') as fh:
        fh.write(world)

    # --- модель робота ---
    mdir = 'turtlebot3_' + MODEL
    with open(_f(ctx, 'model_sdf') or os.path.join(tb3, 'models', mdir, 'model.sdf')) as fh:
        model = fh.read()
    model = _set_rate(model, 'imu', float(_f(ctx, 'imu_hz')))
    model = _set_rate(model, 'gpu_lidar', float(_f(ctx, 'lidar_hz')))
    if float(_f(ctx, 'lidar_hz')) > 0:
        model = _set_lidar(model, *(_f(ctx, n) for n in ('lidar_samples', 'lidar_min_angle', 'lidar_max_angle',
                                                          'lidar_range_min', 'lidar_range_max')))
    cam_hz = float(_f(ctx, 'camera_hz'))
    camera = _f(ctx, 'camera')  # stream | lazy | off
    if camera not in ('stream', 'lazy', 'off'):
        raise RuntimeError('camera must be stream|lazy|off, got %r' % camera)
    if cam_hz <= 0 or 'type="camera"' not in model:
        camera = 'off'
    if 'type="camera"' in model:
        model = _set_rate(model, 'camera', cam_hz if camera != 'off' else 0)
    lidar_on = float(_f(ctx, 'lidar_hz')) > 0
    # Истинная поза робота (для оценки сценария, не для агента): PosePublisher → /model/<имя>/pose,
    # в ROS — /ground_truth/pose через lazy-мост (без подписчика ничего не передаётся).
    gt_hz = float(_f(ctx, 'ground_truth_hz'))
    if gt_hz > 0:
        model = model.replace('</model>', '''  <plugin filename="gz-sim-pose-publisher-system" name="gz::sim::systems::PosePublisher">
      <publish_link_pose>false</publish_link_pose><publish_collision_pose>false</publish_collision_pose>
      <publish_visual_pose>false</publish_visual_pose><publish_sensor_pose>false</publish_sensor_pose>
      <publish_model_pose>true</publish_model_pose><publish_nested_model_pose>false</publish_nested_model_pose>
      <use_pose_vector_msg>false</use_pose_vector_msg><update_frequency>%g</update_frequency>
    </plugin>
  </model>''' % gt_hz, 1) if model.count('</model>') == 1 else model
    odom_tag = 'odom_publish_frequency' if _f(ctx, 'odom_hz_fix').lower() in ('1', 'true', 'yes') \
        else 'odom_publisher_frequency'
    model = re.sub(r'<odom_publisher_frequency>[^<]*</odom_publisher_frequency>',
                   '<%s>%g</%s>' % (odom_tag, float(_f(ctx, 'odom_hz')), odom_tag), model)
    cmd_vel_type = _f(ctx, 'cmd_vel_type')
    if cmd_vel_type not in ('', 'Twist', 'TwistStamped'):
        raise RuntimeError('cmd_vel_type must be Twist|TwistStamped, got %r' % cmd_vel_type)
    model_out = os.path.join(run_dir, 'models', 'robot.sdf')
    with open(model_out, 'w') as fh:
        fh.write(model)

    base = os.path.basename(world_src).rsplit('.', 1)[0]
    dx, dy = DEFAULT_POSE.get(base, (0.0, 0.0))
    x = _f(ctx, 'x_pose') or str(dx)
    y = _f(ctx, 'y_pose') or str(dy)
    yaw = _f(ctx, 'yaw')

    verbose = _f(ctx, 'gz_verbose')
    gz_args = '-r -s --headless-rendering -v%s %s' % (verbose, world_out)
    ros_gz_sim = get_package_share_directory('ros_gz_sim')
    with open(_f(ctx, 'robot_urdf') or os.path.join(tb3, 'urdf', 'turtlebot3_%s.urdf' % MODEL)) as fh:
        robot_desc = fh.read()

    actions = [
        SetEnvironmentVariable('GZ_SIM_RESOURCE_PATH',
                               os.path.join(tb3, 'models') + ':' + os.environ.get('GZ_SIM_RESOURCE_PATH', '')),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(os.path.join(ros_gz_sim, 'launch', 'gz_sim.launch.py')),
            launch_arguments={'gz_args': gz_args, 'on_exit_shutdown': 'true'}.items()),
        Node(package='robot_state_publisher', executable='robot_state_publisher', name='robot_state_publisher',
             output='log', parameters=[{'use_sim_time': True, 'robot_description': robot_desc}]),
        Node(package='ros_gz_sim', executable='create', output='log',
             arguments=['-world', world_name, '-name', MODEL, '-file', model_out,
                        '-x', x, '-y', y, '-z', '0.01', '-Y', yaw]),
        Node(package='ros_gz_bridge', executable='parameter_bridge', name='gz_bridge', output='log',
             arguments=['--ros-args', '-p',
                        'config_file:=' + _bridge_config(os.path.join(tb3, 'params', mdir + '_bridge.yaml'),
                                                         run_dir, camera, lidar_on, MODEL if gt_hz > 0 else None,
                                                         world_name if _f(ctx, 'world_control').lower() in
                                                         ('1', 'true', 'yes') else None,
                                                         cmd_vel_type, _f(ctx, 'cmd_vel_topic'),
                                                         _f(ctx, 'scan_topic')),
                        '-p', 'use_sim_time:=true']),
    ]
    if camera == 'lazy':
        # JPEG по запросу: republish в Jazzy ленивый — подписывается на raw, только пока есть подписчик на
        # /camera/image_raw/compressed; цепочка «подписчик → republish → lazy-мост → рендер в gz» включается целиком.
        q = int(_f(ctx, 'jpeg_quality'))
        actions.append(Node(package='image_transport', executable='republish', name='camera_compressor', output='log',
                            parameters=[{'in_transport': 'raw', 'out_transport': 'compressed', 'use_sim_time': True,
                                         'out.compressed.jpeg_quality': q, 'out.compressed.format': 'jpeg'}],
                            remappings=[('in', '/camera/image_raw'),
                                        ('out/compressed', '/camera/image_raw/compressed')]))
    if camera == 'stream':
        # Как в turtlebot3_gazebo: image_bridge (image_transport: raw + compressed/zstd/theora), рендер постоянно.
        actions.append(Node(package='ros_gz_image', executable='image_bridge', name='image_bridge', output='log',
                            arguments=['/camera/image_raw'], parameters=[{'use_sim_time': True}]))

    if _f(ctx, 'nav2').lower() in ('1', 'true', 'yes'):
        nav2_params = _nav2_params(run_dir, float(x), float(y), float(yaw))
        nav = IncludeLaunchDescription(
            PythonLaunchDescriptionSource(os.path.join(get_package_share_directory('nav2_bringup'),
                                                       'launch', 'bringup_launch.py')),
            launch_arguments={'map': _f(ctx, 'map'), 'use_sim_time': 'True', 'params_file': nav2_params,
                              'autostart': 'True', 'use_composition': 'True'}.items())
        actions.append(TimerAction(period=float(_f(ctx, 'nav2_delay')), actions=[nav]))

    if _f(ctx, 'rosbridge').lower() in ('1', 'true', 'yes'):
        actions.append(IncludeLaunchDescription(
            AnyLaunchDescriptionSource(os.path.join(get_package_share_directory('rosbridge_server'),
                                                    'launch', 'rosbridge_websocket_launch.xml')),
            launch_arguments={'port': _f(ctx, 'rosbridge_port'), 'address': '0.0.0.0'}.items()))
    return actions


def _nav2_params(run_dir, x, y, yaw):
    """Параметры turtlebot3_navigation2/param/<model>.yaml + начальная поза AMCL = поза спавна."""
    src = os.path.join(get_package_share_directory('turtlebot3_navigation2'), 'param', MODEL + '.yaml')
    with open(src) as fh:
        p = fh.read()
    p = re.sub(r'(?m)^(\s*)set_initial_pose: false', r'\1set_initial_pose: true', p)
    p = re.sub(r'(initial_pose:\s*\n\s*x:)\s*[-\d.]+(\s*\n\s*y:)\s*[-\d.]+(\s*\n\s*z:\s*[-\d.]+\s*\n\s*yaw:)\s*[-\d.]+',
               r'\g<1> %.4f\g<2> %.4f\g<3> %.4f' % (x, y, yaw), p)
    out = os.path.join(run_dir, 'nav2_params.yaml')
    with open(out, 'w') as fh:
        fh.write(p)
    return out


def generate_launch_description():
    nav_map = os.path.join(get_package_share_directory('turtlebot3_navigation2'), 'map', 'map.yaml')
    args = [
        ('world', 'turtlebot3_world'), ('rtf', '1.0'), ('step', ''), ('x_pose', ''), ('y_pose', ''), ('yaw', '0.0'),
        ('camera', 'lazy'), ('camera_hz', '30'), ('jpeg_quality', '80'), ('ground_truth_hz', '10'), ('lidar_hz', '10'), ('imu_hz', '200'), ('odom_hz', '30'),
        ('nav2', 'false'), ('map', nav_map), ('nav2_delay', '5.0'),
        ('rosbridge', 'true'), ('rosbridge_port', '9090'), ('world_control', 'false'),
        ('run_dir', '/tmp/robot_sim'), ('gz_verbose', '2'),
        ('lidar_samples', ''), ('lidar_min_angle', ''), ('lidar_max_angle', ''), ('lidar_range_min', ''),
        ('lidar_range_max', ''), ('cmd_vel_type', ''), ('cmd_vel_topic', ''), ('odom_hz_fix', 'false'),
        ('scan_topic', ''), ('model_sdf', ''), ('robot_urdf', ''),
    ]
    return LaunchDescription([DeclareLaunchArgument(n, default_value=v) for n, v in args] +
                             [OpaqueFunction(function=_prepare)])
