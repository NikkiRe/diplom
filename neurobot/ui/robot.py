"""Геометрия робота и тормозной путь для пульта — из параметров safety_gate (neurobot/safety), а не своими числами.

Тормозной путь d_stop(v) = v·t_lat + v²/(2·a_brake) + margin считает neurobot.safety.motion.stop_distance с
умолчаниями Params — это [допущение] safety_gate до замера на роботе (README safety, «Параметры»).
"""
from __future__ import annotations

from typing import Optional

from neurobot.safety.config import Params
from neurobot.safety.motion import stop_distance

PARAMS = Params()
LIDAR_HALF_FOV_DEG = 119.9      # URG-04LX: 683 луча ±2,09235 рад (PLAN §13.1); сзади 120° датчика нет
DEPTH_FOV_DEG = 87.0            # D435i, поле глубины по горизонтали (README safety)
ROBOT_MASK_DEG = (-60.8, 4.2)   # маска корпуса камеры, снятая по скану робота 02.06 (README safety, mask_from_deg)
BUDGET_MIB = {"физическая": 4096, "headless": 3336, "lxde": 3056, "gnome": 2406}   # neurobot/sim/budget README §1.2


def d_stop(v: Optional[float]) -> Optional[float]:
    """Тормозной путь от края корпуса при скорости вперёд v, м; назад и стоя — запас margin; None — нет данных."""
    if v is None:
        return None
    return round(stop_distance(PARAMS, float(v)), 4)


def geometry() -> dict:
    """Заголовок потока: корпус, коридор, поля датчиков, пределы и параметры тормозного пути."""
    p = PARAMS
    return {"robot_radius": p.robot_radius, "lateral_margin": p.lateral_margin, "turn_margin": p.turn_margin,
            "t_lat": p.t_lat, "a_brake": p.a_brake, "margin": p.margin, "max_speed": p.max_speed,
            "max_turn": p.max_turn, "blind_speed": p.blind_speed, "reverse_speed": p.reverse_speed,
            "mask_learn_deg": [p.mask_from_deg, p.mask_to_deg], "mask_robot_deg": list(ROBOT_MASK_DEG),
            "lidar_half_fov_deg": LIDAR_HALF_FOV_DEG, "depth_fov_deg": DEPTH_FOV_DEG, "budget_mib": BUDGET_MIB,
            "note": "умолчания safety_gate (neurobot/safety/config.py) — допущение до замера на роботе"}
