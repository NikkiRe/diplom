"""Заместитель базы робота в бюджете Jetson (README бюджета, «Состав»): ros2 launch + ros2_control_node с
mock_components/GenericSystem и теми же контроллерами, что у двойника (diff_cont, joint_broad, 50 Гц). Работает в
отдельном домене DDS: колёсами двойника управляет controller_manager внутри Gazebo, а этот — только тратит память и
CPU, как controller_manager на Jetson (железо там — мост UART к STM32).

Аргументы: urdf — URDF двойника с заменённым плагином железа; controllers — controllers.yaml двойника.
"""
import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

BUDGET = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def generate_launch_description():
    urdf, controllers = LaunchConfiguration("urdf"), LaunchConfiguration("controllers")
    return LaunchDescription([
        DeclareLaunchArgument("urdf", description="URDF с mock_components/GenericSystem"),
        DeclareLaunchArgument("controllers", description="controllers.yaml двойника"),
        Node(package="controller_manager", executable="ros2_control_node", parameters=[controllers], output="both"),
        ExecuteProcess(cmd=["python3", os.path.join(BUDGET, "describe.py"), urdf], output="both"),
        Node(package="controller_manager", executable="spawner", output="both",
             arguments=["diff_cont", "joint_broad", "--controller-manager-timeout", "60"]),
    ])
