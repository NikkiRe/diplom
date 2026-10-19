"""Датчики двойника отдельным launch — как драйверы робота отдельными юнитами neurobot-driver@: role lidar — узел
/urg_node2 (/scan), role camera — узел /camera/camera (цвет, глубина, IMU камеры, как realsense2_camera). Двойник —
twin.launch.py с sensors:=outside (мост отдаёт камеру сырыми темами /twin/*). Под юнитом — через обёртку deploy
(launchfiles/supervised.launch.py), как драйверы робота: смерть узла гасит launch, юнит перезапускает systemd.

Аргументы: role (lidar | camera), no_return (nan | inf — что лидар отдаёт на «нет отражения», sensor_model.py).
"""
import os
import sys

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch_ros.actions import Node

TWIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES = {'lidar': ('urg_node2', ''), 'camera': ('camera', 'camera')}


def _node(ctx):
    a = ctx.launch_configurations
    if a['role'] not in NAMES or a['no_return'] not in ('nan', 'inf'):
        raise RuntimeError('role: lidar | camera, no_return: nan | inf, получено %r, %r' % (a['role'], a['no_return']))
    name, ns = NAMES[a['role']]
    # no_return — логическим параметром: строки «nan» и «inf» launch_ros передал бы числами (YAML)
    return [Node(name=name, namespace=ns, executable=sys.executable, output='log',
                 arguments=[os.path.join(TWIN, 'sensor_model.py')],
                 parameters=[{'role': a['role'], 'no_return_inf': a['no_return'] == 'inf'}])]


def generate_launch_description():
    return LaunchDescription([DeclareLaunchArgument('role', description='lidar | camera'),
                              DeclareLaunchArgument('no_return', default_value='nan', description='nan | inf'),
                              OpaqueFunction(function=_node)])
