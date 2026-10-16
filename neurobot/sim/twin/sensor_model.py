#!/usr/bin/env python3
"""(в ВМ sim) Выход драйверов робота поверх идеальных датчиков Gazebo — узел twin_sensor_model.

/twin/scan_raw (gpu_lidar) → /scan, как у urg_node2: луч без отражения (дальше range_max) и луч, упёршийся ближе
  self_hit_m, — NaN. URG на роботе отдаёт корпус D435i в 48–99 мм перед своей осью кодом 0, а urg_node2 превращает
  его в NaN (скан 02.06: 186 лучей от −60,8 до +4,2°, R7b §3.3). Сектор не задаётся маской: его даёт корпус D435i в
  модели; без него в /scan были бы отражения в 3–13 см (с шумом gpu_lidar). Порог 0,15 м — 0,099 м до дальнего угла
  корпуса плюс 5σ шума; ближе 0,15 м к оси лидара в плоскости скана снаружи робота ничего быть не может (голова
  шириной 0,12 м).
/twin/depth_raw (32FC1, м) → /camera/depth/image_rect_raw, как у realsense2_camera: 16UC1 в мм, «нет данных» — 0.
  Шум — нормальный с σ(z) = z²·subpixel / (f·B) (модель RMS-ошибки Intel для стерео: subpixel 0,08, база D435
  B = 50 мм, f — по ширине кадра и полю зрения) — [оценка]; провалы, «летящие» пиксели и зависимость от текстуры
  не моделируются. Глубину узел берёт, только пока на выходе есть подписчик (сверка раз в секунду): без него мост
  ленивый и Gazebo камеру глубины не рендерит.
Параметры: self_hit_m (0,15), depth_hfov (1,5184 рад — как у датчика в модели), depth_subpixel (0,08; 0 — без шума),
depth_baseline_m (0,05), seed (0). Издатели reliable: их принимают и reliable-, и best-effort-подписчики.
"""
from __future__ import annotations

import math
import sys

import numpy as np

SELF_HIT_M = 0.15
DEPTH_HFOV = 1.5184
SUBPIXEL = 0.08
BASELINE_M = 0.05


def urg_ranges(ranges, range_max: float, self_hit_m: float) -> list:
    """Дальности как у urg_node2: нет отражения (не число или дальше range_max) и попадание в свой корпус — NaN."""
    nan = float("nan")
    return [r if math.isfinite(r) and self_hit_m <= r <= range_max else nan for r in ranges]


def depth_sigma(z, width: int, hfov: float, subpixel: float, baseline_m: float):
    """RMS-ошибка глубины стереокамеры на расстоянии z, м: z²·subpixel / (f·B), f = (width/2) / tg(hfov/2)."""
    f = width / 2.0 / math.tan(hfov / 2.0)
    return np.square(z) * subpixel / (f * baseline_m)


def depth_mm(depth_m, near: float, far: float, hfov: float, subpixel: float, baseline_m: float, rng):
    """32FC1 в метрах → uint16 в мм как у realsense2_camera; вне [near, far] и не числа — 0."""
    z = np.asarray(depth_m, dtype=np.float64)
    valid = np.isfinite(z) & (z >= near) & (z <= far)
    z = np.where(valid, z, 0.0)
    if subpixel > 0:
        z = z + rng.standard_normal(z.shape) * depth_sigma(z, z.shape[-1], hfov, subpixel, baseline_m)
    mm = np.rint(np.clip(z, 0.0, 65.535) * 1000.0)
    return np.where(valid, mm, 0).astype(np.uint16)


def main():
    import rclpy
    from rclpy.executors import ExternalShutdownException
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import Image, LaserScan

    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    from twin_config import DEPTH_RAW, SCAN_RAW

    class SensorModel(Node):
        def __init__(self):
            super().__init__("twin_sensor_model")
            self.self_hit = float(self.declare_parameter("self_hit_m", SELF_HIT_M).value)
            self.hfov = float(self.declare_parameter("depth_hfov", DEPTH_HFOV).value)
            self.subpixel = float(self.declare_parameter("depth_subpixel", SUBPIXEL).value)
            self.baseline = float(self.declare_parameter("depth_baseline_m", BASELINE_M).value)
            self.rng = np.random.default_rng(int(self.declare_parameter("seed", 0).value))
            self.near, self.far = 0.105, 10.0
            self.scan_pub = self.create_publisher(LaserScan, "/scan", 10)
            self.create_subscription(LaserScan, SCAN_RAW, self.on_scan, qos_profile_sensor_data)
            self.depth_pub = self.create_publisher(Image, "/camera/depth/image_rect_raw", 10)
            self.depth_sub = None
            self.create_timer(1.0, self.lazy_depth)

        def on_scan(self, msg):
            msg.ranges = urg_ranges(msg.ranges, msg.range_max, self.self_hit)
            self.scan_pub.publish(msg)

        def lazy_depth(self):
            wanted = self.depth_pub.get_subscription_count() > 0
            if wanted and self.depth_sub is None:
                self.depth_sub = self.create_subscription(Image, DEPTH_RAW, self.on_depth, qos_profile_sensor_data)
            elif not wanted and self.depth_sub is not None:
                self.destroy_subscription(self.depth_sub)
                self.depth_sub = None

        def on_depth(self, msg):
            if msg.encoding != "32FC1":
                self.get_logger().error("глубина: ждал 32FC1, пришло %s — кадр пропущен" % msg.encoding,
                                        throttle_duration_sec=5.0)
                return
            z = np.frombuffer(bytes(msg.data), dtype=np.float32).reshape(msg.height, msg.width)
            out = Image(header=msg.header, height=msg.height, width=msg.width, encoding="16UC1", is_bigendian=0,
                        step=msg.width * 2)
            out.data = depth_mm(z, self.near, self.far, self.hfov, self.subpixel, self.baseline,
                                self.rng).tobytes()
            self.depth_pub.publish(out)

    rclpy.init()
    node = SensorModel()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
