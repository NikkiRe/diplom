"""Умолчания стенда neurobot в одном месте: их читают launch стенда, сводка check.sh и тесты.

Базы (stand.sh up --base):
  tb3       — TurtleBot3 waffle_pi (готовая модель ROBOTIS), по умолчанию и для регрессии;
  neurochip — своя простая модель базы NeuroChip (models/neurochip_base.sdf) по оценкам с фото, R7b §2.3.
Геометрия лидара (683 луча, ±2,0923497677 рад, до 5,6 м) у обеих баз — по скану с робота (профиль neurobot.sh).
"""
from __future__ import annotations

import os
from typing import NamedTuple

STAND = os.path.dirname(os.path.abspath(__file__))


class Base(NamedTuple):
    model_sdf: str      # пусто — модель TurtleBot3 из turtlebot3_gazebo
    robot_urdf: str     # для robot_state_publisher; пусто — URDF TurtleBot3
    range_min: str      # ближняя граница лидара, м (строка — аргумент launch)
    lidar_x: float      # ось лидара от центра базы по x, м
    front_m: float      # передний край базы (коллизия) от центра, м
    scan_frame: str     # frame_id /scan


BASES = {
    # Лучи gpu_lidar начинаются в центре корпуса LDS TurtleBot3 (меш ≈0,05 м): при 0,02 все лучи упираются в него.
    "tb3": Base("", "", "0.06", -0.064, 0.0685, "base_scan"),
    "neurochip": Base(os.path.join(STAND, "models", "neurochip_base.sdf"),
                      os.path.join(STAND, "models", "neurochip_base.urdf"), "0.02", -0.015, 0.195, "laser_frame"),
}
DEFAULT_BASE = "tb3"

# Параметры stm32_sim по умолчанию на стенде (аргументы launch stm32_<имя>; смысл — stm32_sim.py).
# delay_ms 20 — R7b §5.5: кадр UART + 1–2 периода ПИД [гипотеза до HIL].
STM32_DEFAULTS = (("cmd_vel_type", "Twist"), ("timeout_ms", "0"), ("delay_ms", "20"), ("max_lin", "0.0"),
                  ("max_ang", "0.0"), ("max_lin_acc", "0.0"), ("max_ang_acc", "0.0"), ("rate_hz", "50.0"))
