"""Заместитель драйвера лидара в бюджете Jetson: ros2 launch + urg_node (на роботе — urg_node2, в Jazzy его пакета
нет) с последовательным портом URG-04LX. Устройства в ВМ нет: узел живёт и дважды в секунду пробует открыть порт,
данных не публикует. Работает в отдельном домене DDS, чтобы не стать вторым издателем /scan рядом с двойником.
Готовый launch пакета urg_node (urg_node_launch.py) в Jazzy не запускается: в нём устаревший аргумент node_executable.
"""
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([
        Node(package="urg_node", executable="urg_node_driver", output="both",
             parameters=[{"serial_port": "/dev/ttyACM0", "serial_baud": 115200, "laser_frame_id": "laser_frame",
                          "angle_min": -2.0923497677, "angle_max": 2.0923497677}]),
    ])
