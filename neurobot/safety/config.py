"""Параметры safety_gate: умолчания и проверка (PLAN §13.3; статус каждого значения — README, «Параметры»).

Типы проверяются здесь, а не rclpy: на Foxy declare_parameter не сверяет тип переопределения, и строка «false»
была бы истинной, а строка вместо числа роняла бы узел TypeError вместо кода 2.
"""
from __future__ import annotations

import math
from typing import Mapping, NamedTuple, Tuple


class ConfigError(Exception):
    """Неверный параметр: узел выходит с кодом 2."""


class Params(NamedTuple):
    # выход и такт
    cmd_vel_type: str = "Twist"          # Twist (робот, twist_mux Foxy) | TwistStamped
    base_frame: str = "base_link"        # кадр базы: TF датчиков, header.frame_id у TwistStamped
    rate_hz: float = 20.0                # тик: решение, /cmd_vel, WATCHDOG
    state_hz: float = 5.0                # /neurobot/safety_state без изменений
    # корпус [оценка по фото, R7b §2.3]
    robot_radius: float = 0.195          # м, круг палуб Ø0,39
    lateral_margin: float = 0.05         # м, запас коридора вбок
    turn_margin: float = 0.03            # м, точка ближе radius + turn_margin — поворот на месте заденет
    # тормозной путь от края корпуса: v·t_lat + v²/(2·a_brake) + margin [допущение до замера на роботе]
    t_lat: float = 0.25                  # с, период скана + обработка + доставка + реакция контроллера
    a_brake: float = 0.8                 # м/с², замедление базы (L1 двойника — 1,0)
    margin: float = 0.05                 # м, запас до препятствия после остановки
    min_speed: float = 0.03              # м/с; разрешённая скорость ниже — блок вперёд (не ползти к препятствию)
    release_hyst: float = 0.05           # м, гистерезис снятия блока вперёд
    release_s: float = 0.3               # с, столько путь должен оставаться свободным для снятия блока
    # пределы
    max_speed: float = 0.25              # м/с вперёд
    max_turn: float = 0.8                # рад/с
    max_accel: float = 0.5               # м/с², разгон (торможение не ограничено: его держит L1)
    max_jerk: float = 2.0                # м/с³, рывок при разгоне (мачта)
    max_turn_accel: float = 2.0          # рад/с², разгон поворота
    reverse_speed: float = 0.05          # м/с назад; 0 — задний ход запрещён (сзади датчиков нет, S7)
    reverse_dist: float = 0.2            # м назад за один эпизод заднего хода
    blind_speed: float = 0.15            # м/с вперёд, когда перед в слепом секторе лидара видит только глубина
    max_motion_s: float = 30.0           # с непрерывного движения, затем стоп до нулевого запроса
    geofence_m: float = 3.0              # м от точки старта по одометрии; 0 — без геозабора
    # свежесть входов
    req_timeout: float = 0.5             # с; запрос исполнителя старше — ноль
    scan_timeout: float = 0.4            # с; скан старше — стоп
    odom_timeout: float = 0.5            # с
    depth_timeout: float = 0.5           # с
    imu_timeout: float = 0.5             # с
    depth_stale: str = "block"           # глубина устарела: block — вперёд нельзя, slow — не быстрее blind_speed
    # лидар
    mask_scans: int = 20                 # сканов на старте для маски слепых лучей (корпус камеры, RS3)
    mask_from_deg: float = -65.0         # сектор корпуса камеры, пеленг в базе (°): маска учится только в нём
    mask_to_deg: float = 8.0
    mask_ratio: float = 0.8              # доля сканов «нет данных» у луча, чтобы он попал в маску
    mask_dilate: int = 2                 # лучей расширения маски с каждой стороны (мерцающий край корпуса)
    max_gap_m: float = 0.03              # м: провал «нет данных» уже этого на дальности остановки — терпим
    # глубина
    depth_cols: int = 80                 # ширина уменьшенной карты глубины, пикселей
    depth_hz: float = 10.0               # не чаще стольких кадров глубины в секунду
    depth_max_m: float = 2.5             # м от камеры по полу: дальше шум глубины велик
    depth_min_valid: float = 0.5         # доля пикселей с глубиной в каждой полосе коридора; меньше — кадр не принят
    obstacle_min_h: float = 0.02         # м над полом: выше — препятствие
    obstacle_max_h: float = 0.75         # м: выше — над роботом (верх URG ≈0,69–0,70)
    drop_min: float = 0.04               # м ниже пола — обрыв
    min_pixels: int = 2                  # пикселей опасности в клетке за кадр, чтобы ей поверить
    cell_m: float = 0.05                 # м, клетка памяти глубины
    memory_m: float = 3.0                # м: клетки дальше от робота забываются
    # IMU
    tilt_max_deg: float = 10.0           # наклон больше — защёлка
    shock_mps2: float = 6.0              # всплеск ускорения больше — защёлка
    # упор и пробуксовка
    stall_s: float = 1.0                 # с: команда есть, а база стоит — защёлка
    stall_cmd: float = 0.03              # м/с: команда вперёд-назад, которую считаем движением
    stall_turn_cmd: float = 0.2          # рад/с: то же для поворота
    stall_odom: float = 0.01             # м/с по одометрии: медленнее — стоит
    stall_turn_odom: float = 0.05        # рад/с по одометрии
    # граф
    override_topics: Tuple[str, ...] = ("/cmd_vel_tracker", "/cmd_vel_joy")  # входы twist_mux главнее /cmd_vel
    override_policy: str = "warn"        # их издатели: warn — предупреждение, block — стоять, estop — защёлка
    base_cmd_topic: str = "/diff_cont/cmd_vel_unstamped"  # выход twist_mux: дошла ли команда до базы; "" — нет
    estop_file: str = ""                 # файл защёлки; "" — $STATE_DIRECTORY/estop.json (юнит), иначе только память


CHOICES = {
    "cmd_vel_type": ("Twist", "TwistStamped"),
    "depth_stale": ("block", "slow"),
    "override_policy": ("warn", "block", "estop"),
}
POSITIVE = ("rate_hz", "state_hz", "robot_radius", "a_brake", "min_speed", "release_s", "max_speed", "max_turn",
            "max_accel", "max_turn_accel", "blind_speed", "max_motion_s", "req_timeout", "scan_timeout", "odom_timeout",
            "depth_timeout", "imu_timeout", "mask_scans", "mask_ratio", "depth_cols", "depth_hz", "depth_max_m",
            "depth_min_valid", "obstacle_min_h", "obstacle_max_h", "drop_min", "min_pixels", "cell_m", "memory_m",
            "tilt_max_deg", "shock_mps2", "stall_s", "stall_cmd", "stall_turn_cmd", "stall_odom", "stall_turn_odom")
SIGNED = ("mask_from_deg", "mask_to_deg")  # пеленги: знак — сторона


def _typed(name: str, value, default):
    """Значение того же типа, что умолчание; целое годится для дробного (кроме bool)."""
    if isinstance(default, bool):
        ok = isinstance(value, bool)
    elif isinstance(default, int):
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif isinstance(default, float):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        value = float(value) if ok else value
    elif isinstance(default, str):
        ok = isinstance(value, str)
    else:
        ok = isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value)
        value = tuple(value) if ok else value
    if not ok:
        raise ConfigError("{}: ожидается {}, получено {!r}".format(name, type(default).__name__, value))
    return value


def make(values: Mapping) -> Params:
    """Params из словаря (ROS-параметры); ConfigError — неизвестное имя, тип, диапазон или выбор."""
    unknown = set(values) - set(Params._fields)
    if unknown:
        raise ConfigError("неизвестные параметры: {}".format(", ".join(sorted(unknown))))
    defaults = Params()
    p = defaults._replace(**{n: _typed(n, v, getattr(defaults, n)) for n, v in values.items()})
    for name, choices in CHOICES.items():
        if getattr(p, name) not in choices:
            raise ConfigError("{}: ожидается {}, получено {!r}".format(name, " | ".join(choices), getattr(p, name)))
    for name in POSITIVE:
        if not getattr(p, name) > 0:
            raise ConfigError("{} должен быть > 0, получено {!r}".format(name, getattr(p, name)))
    for name in Params._fields:
        value = getattr(p, name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0 and name not in SIGNED:
            raise ConfigError("{} должен быть ≥ 0, получено {!r}".format(name, value))
    for name in ("mask_ratio", "depth_min_valid"):
        if getattr(p, name) > 1:
            raise ConfigError("{} — доля, ≤ 1, получено {!r}".format(name, getattr(p, name)))
    if not -180.0 <= p.mask_from_deg < p.mask_to_deg <= 180.0:
        raise ConfigError("сектор корпуса: ожидается −180 ≤ mask_from_deg < mask_to_deg ≤ 180, получено {!r}…{!r}"
                          .format(p.mask_from_deg, p.mask_to_deg))
    if p.obstacle_min_h >= p.obstacle_max_h:
        raise ConfigError("obstacle_min_h должен быть меньше obstacle_max_h")
    if p.reverse_speed > p.max_speed or p.blind_speed > p.max_speed:
        raise ConfigError("reverse_speed и blind_speed не больше max_speed")
    return p
