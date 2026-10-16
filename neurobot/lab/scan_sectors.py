#!/usr/bin/env python3
"""Сводка одного сообщения LaserScan по секторам для collect_diag.sh — без выгрузки массива. Python 3.8+.

  scan_sectors.py [ТЕМА [ТАЙМАУТ_С]]   (по умолчанию /scan 8) — в окружении ROS 2: ждёт одно сообщение
                                        (QoS sensor_data) и печатает сводку. Код 0 — сводка есть; 2 — неверные
                                        аргументы или нет rclpy; 3 — сообщение не пришло.

По каждому сектору 10° (угол от оси X лидара, «+» — влево): число лучей, доля NaN, доля ±inf, доля значений
ниже range_min (у URG это коды ошибок, 0,006–0,019 м) и минимум допустимых расстояний. Последняя строка —
самый длинный сплошной блок NaN: так видно, закрывает ли что-то лидару сектор прямо по курсу (ANALYSIS RS3).
"""
import math
import sys
import time

SECTOR_DEG = 10
FRONT_DEG = 30  # передний сектор стража: ±30°


def ray_deg(angle_min, increment, i):
    return math.degrees(angle_min + i * increment)


def sectors(angle_min, increment, ranges, range_min):
    """Строки (от, до, лучей, доля NaN, доля ±inf, доля < range_min, ближайшее допустимое или None) по секторам."""
    bins = {}
    for i, r in enumerate(ranges):
        lo = math.floor(ray_deg(angle_min, increment, i) / SECTOR_DEG) * SECTOR_DEG
        b = bins.setdefault(lo, [0, 0, 0, 0, None])
        b[0] += 1
        if math.isnan(r):
            b[1] += 1
        elif math.isinf(r):
            b[2] += 1
        elif r < range_min:
            b[3] += 1
        elif b[4] is None or r < b[4]:
            b[4] = r
    return [(lo, lo + SECTOR_DEG, n, nan / n, inf / n, low / n, near)
            for lo, (n, nan, inf, low, near) in sorted(bins.items())]


def nan_block(ranges):
    """Самый длинный сплошной блок NaN: (первый, последний индекс) или None."""
    best, start = None, None
    for i, r in enumerate(list(ranges) + [0.0]):
        if math.isnan(r):
            start = i if start is None else start
        elif start is not None:
            if best is None or i - start > best[1] - best[0] + 1:
                best = (start, i - 1)
            start = None
    return best


def summary(angle_min, increment, ranges):
    """Строка итога для отчёта: где блок NaN и задевает ли он передний сектор."""
    block = nan_block(ranges)
    if block is None:
        return "блок NaN: нет"
    a, b = (ray_deg(angle_min, increment, i) for i in block)
    lo, hi = min(a, b), max(a, b)
    front = "задевает" if lo <= FRONT_DEG and hi >= -FRONT_DEG else "не задевает"
    return (f"блок NaN: {lo:+.1f}…{hi:+.1f}° ({block[1] - block[0] + 1} лучей, индексы {block[0]}–{block[1]}), "
            f"{front} передний сектор ±{FRONT_DEG}°")


def report(topic, msg):
    ranges = list(msg.ranges)
    n = len(ranges)
    last = ray_deg(msg.angle_min, msg.angle_increment, n - 1) if n else math.degrees(msg.angle_min)
    lines = [f"тема {topic}, кадр {msg.header.frame_id}, лучей {n}, углы {math.degrees(msg.angle_min):+.1f}…"
             f"{last:+.1f}°, шаг {math.degrees(msg.angle_increment):.4f}°, range_min {msg.range_min:.3f} м, "
             f"range_max {msg.range_max:.3f} м",
             "сектор, °      лучей   NaN   inf  <min  ближе всего, м"]
    for lo, hi, cnt, nan, inf, low, near in sectors(msg.angle_min, msg.angle_increment, ranges, msg.range_min):
        dist = "—" if near is None else f"{near:.3f}"
        lines.append(f"{lo:+5d}…{hi:+5d}  {cnt:6d}  {nan:4.2f}  {inf:4.2f}  {low:4.2f}  {dist}")
    lines.append(summary(msg.angle_min, msg.angle_increment, ranges))
    return "\n".join(lines)


def wait_one(topic, timeout_s):
    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import LaserScan

    rclpy.init()
    node = rclpy.create_node("_neurobot_diag_scan")  # «_» — скрытый узел, в node list не виден
    got = []
    node.create_subscription(LaserScan, topic, got.append, qos_profile_sensor_data)
    deadline = time.monotonic() + timeout_s
    try:
        while not got and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.2)
    finally:
        node.destroy_node()
        rclpy.shutdown()
    return got[0] if got else None


def main(argv):
    if len(argv) > 2 or (len(argv) == 2 and not argv[1].replace(".", "", 1).isdigit()):
        sys.stderr.write(__doc__)
        return 2
    topic = argv[0] if argv else "/scan"
    timeout_s = float(argv[1]) if len(argv) == 2 else 8.0
    try:
        msg = wait_one(topic, timeout_s)
    except ImportError as e:
        print(f"нет rclpy или sensor_msgs: {e}")
        return 2
    if msg is None:
        print(f"сообщение {topic} не пришло за {timeout_s:g} с")
        return 3
    print(report(topic, msg))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
