"""Умолчания двойника NeuroChip в одном месте: их читают launch, готовность, проверки, сводка и тесты.

Имена тем — как на роботе (R7a §1.5; realsense2_camera; urg_node2), внутренние темы двойника — /twin/*.
"""
from __future__ import annotations

import math
import os

TWIN = os.path.dirname(os.path.abspath(__file__))
MODEL = "neurochip"
WORLD_NAME = "default"
TRUTH_HZ = 20.0

# Миры: файл и поза спавна (x, y, z, yaw) — README §5.
WORLDS = {
    "lab": ("worlds/lab.sdf", (-1.5, 0.0, 0.01, 0.0)),
    "step": ("worlds/step.sdf", (-1.0, 0.0, 0.16, 0.0)),
    "ramp": ("worlds/ramp.sdf", (-1.5, 0.0, 0.01, 0.0)),
}

# Пандус мира ramp: начало (x, м) и уклон (°) — worlds/ramp.sdf.
RAMP_START_X = -0.8
RAMP_DEG = 12.0

# Проба истины (twin_probe.py truth, мир lab): модель, её поза в мире и поза перед роботом, куда её ставит set_pose
# (x, y); «кабель», который создаёт create: имя, центр (x, y) и размер (м) — ниже нижней палубы (0,05 м).
TRUTH_PERSON = ("person", (1.2, -0.9), (-0.9, 0.0))
TRUTH_CABLE = ("cable", (-0.95, 0.0), (0.2, 0.2, 0.04))

# Аргументы twin.launch.py и их умолчания; частоты камеры — README §3 (RTF).
LAUNCH_ARGS = (
    ("world", "lab"), ("x_pose", ""), ("y_pose", ""), ("z_pose", ""), ("yaw", ""),
    ("cmd_vel_timeout", "0.5"), ("lidar_hz", "10"), ("color_hz", "15"), ("depth_hz", "15"), ("imu_hz", "200"),
    ("truth_hz", "100"), ("rosbridge", "true"), ("rosbridge_port", "9090"), ("run_dir", "/tmp/twin"),
    ("gz_verbose", "2"), ("sensors", "inside"),
)

# Цепочка движения (как на роботе, R7a N13) и внутренние темы двойника.
CMD_VEL = "/cmd_vel"
MUX_INPUTS = {"/cmd_vel": 10, "/cmd_vel_tracker": 20, "/cmd_vel_joy": 100}
MUX_OUT = "/diff_cont/cmd_vel_unstamped"
DIFF_CMD = "/diff_cont/cmd_vel"           # только двойник: вход diff_drive_controller Jazzy (TwistStamped)
DIFF_OUT = "/diff_cont/cmd_vel_out"       # что контроллер отдаёт колёсам (publish_limited_velocity)
SCAN_RAW = "/twin/scan_raw"
DEPTH_RAW = "/twin/depth_raw"
# sensors:=outside — камера от моста сырыми темами, их отдаёт узел камеры (sensor.launch.py role camera):
# выход → (вход, тип)
CAMERA_RAW = {"/camera/color/image_raw": ("/twin/color/image_raw", "Image"),
              "/camera/color/camera_info": ("/twin/color/camera_info", "CameraInfo"),
              "/camera/depth/camera_info": ("/twin/depth/camera_info", "CameraInfo"),
              "/camera/imu": ("/twin/imu", "Imu")}
MODELS_TOPIC = "/twin/models"             # логическая камера twin_eye: имена и позы моделей мира → truth.py
CONTROLLERS = ("diff_cont", "joint_broad")

# Темы робота: тип, частота (Гц) и допуск по частоте. Камера ленивая — частота считается при подписчике.
SENSORS = {
    "/scan": ("sensor_msgs/msg/LaserScan", 10.0, 1.0),
    "/odom": ("nav_msgs/msg/Odometry", 50.0, 5.0),
    "/joint_states": ("sensor_msgs/msg/JointState", 50.0, 5.0),
    "/camera/imu": ("sensor_msgs/msg/Imu", 200.0, 20.0),
    "/imu/data": ("sensor_msgs/msg/Imu", 200.0, 20.0),
    "/camera/color/image_raw": ("sensor_msgs/msg/Image", 15.0, 2.0),
    "/camera/color/camera_info": ("sensor_msgs/msg/CameraInfo", 15.0, 2.0),
    "/camera/depth/image_rect_raw": ("sensor_msgs/msg/Image", 15.0, 2.0),
    "/camera/depth/camera_info": ("sensor_msgs/msg/CameraInfo", 15.0, 2.0),
    "/ground_truth/pose": ("geometry_msgs/msg/PoseStamped", 100.0, 10.0),
    "/twin/truth": ("std_msgs/msg/String", TRUTH_HZ, 2.0),
}
CAMERA_TOPICS = ("/camera/color/image_raw", "/camera/color/camera_info", "/camera/depth/image_rect_raw",
                 "/camera/depth/camera_info")
# Кадры TF: корень — odom (diff_cont), остальное — robot_state_publisher.
TF_FRAMES = ("odom", "base_link", "base_footprint", "left_wheel", "right_wheel", "laser_frame", "camera_link",
             "camera_color_optical_frame", "camera_depth_optical_frame", "camera_imu_optical_frame")

# Лидар — как в скане с робота 02.06 (R7b RS15) и слепой сектор оттуда же (R7b §3.3).
SCAN_N = 683
SCAN_ANGLE = 2.0923497677
SCAN_RANGE = (0.02, 5.6)
SCAN_FRAME = "laser_frame"
ROBOT_BLIND_DEG = (-60.8, 4.2)
ROBOT_BLIND_N = 186


def world(arg: str):
    """Файл мира и поза спавна: имя из WORLDS или путь к .sdf (поза — начало координат)."""
    if arg in WORLDS:
        rel, spawn = WORLDS[arg]
        return os.path.join(TWIN, rel), spawn
    if arg.endswith(".sdf") and os.path.isfile(arg):
        return arg, (0.0, 0.0, 0.01, 0.0)
    raise RuntimeError("world: %s или путь к .sdf, получено %r" % ("|".join(sorted(WORLDS)), arg))


def scan_angle(i: int) -> float:
    """Угол луча i, градусы (gz-sensors: шаг = 2·SCAN_ANGLE / (SCAN_N − 1))."""
    return math.degrees(-SCAN_ANGLE + i * 2 * SCAN_ANGLE / (SCAN_N - 1))
