#!/usr/bin/env python3
"""(в ВМ sim) Проверка stm32_sim по поведению робота: команда «вперёд», затем тишина на /cmd_vel. JSON в stdout.

Публикует в /cmd_vel линейную скорость --vx с частотой --rate-hz в течение --drive-s, затем молчит --silence-s, в конце
шлёт ноль 0,5 с (робот остаётся стоять). Время — монотонные часы ВМ в момент приёма. Считает по /odom (скорость) и
/ground_truth/pose (путь):
  start_after_s  — от первой команды до первого /odom с |v| > v_move (разгон + мост + квант /odom; ориентир);
  v_cmd_end      — средняя |v| за последние 0,3 с команды;
  stop_after_s   — от последней команды до первого /odom с |v| < v_stop (null — не остановился за тишину);
  coast_m        — путь по истинной позе за тишину;
  v_silence_end  — |v| последнего /odom тишины (колёсная одометрия: у упёршегося в стену робота колёса крутятся);
  pose_end       — истинная поза (x, y) в конце тишины.
Ожидание: hold — едет всю тишину (stop_after_s = null, coast_m ≈ v·silence);
сторож N мс — stop_after_s ≈ N мс + торможение.
  python3 stm32_probe.py [--vx 0.2] [--drive-s 1.5] [--silence-s 2] [--rate-hz 10] [--type Twist|TwistStamped]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time

V_MOVE = 0.05   # м/с — «движение началось» (PLAN, K2)
V_STOP = 0.02   # м/с — «стоит» (шум /odom DiffDrive в покое ≈ 0)


def _first(series, t_from, pred):
    return next((t for t, v in series if t >= t_from and pred(v)), None)


def _pose_at(gt, t):
    """Последняя истинная поза не позже t (x, y); None — поз нет."""
    before = [(x, y) for tt, x, y in gt if tt <= t]
    return before[-1] if before else None


def analyze(odom, gt, t_first, t_last, silence_s):
    """odom — [(t, v)], gt — [(t, x, y)]; t_first/t_last — первая и последняя команда."""
    t_end = t_last + silence_s
    t_start = _first(odom, t_first, lambda v: abs(v) > V_MOVE)
    t_stop = _first([(t, v) for t, v in odom if t <= t_end], t_last, lambda v: abs(v) < V_STOP)
    tail = [abs(v) for t, v in odom if t_last - 0.3 <= t <= t_last]
    silence = [abs(v) for t, v in odom if t_last < t <= t_end]
    p0, p1 = _pose_at(gt, t_last), _pose_at(gt, t_end)
    return {
        "start_after_s": None if t_start is None else round(t_start - t_first, 3),
        "v_cmd_end": round(sum(tail) / len(tail), 3) if tail else None,
        "stop_after_s": None if t_stop is None else round(t_stop - t_last, 3),
        "coast_m": None if p0 is None or p1 is None else round(math.hypot(p1[0] - p0[0], p1[1] - p0[1]), 3),
        "v_silence_end": round(silence[-1], 3) if silence else None,
        "pose_end": None if p1 is None else [round(p1[0], 3), round(p1[1], 3)],
    }


def run(a):
    import rclpy
    from geometry_msgs.msg import PoseStamped, Twist, TwistStamped
    from nav_msgs.msg import Odometry
    from rclpy.qos import qos_profile_sensor_data

    rclpy.init()
    node = rclpy.create_node("stm32_probe")
    odom, gt = [], []
    node.create_subscription(Odometry, "/odom", lambda m: odom.append((time.monotonic(), m.twist.twist.linear.x)),
                             qos_profile_sensor_data)
    node.create_subscription(PoseStamped, "/ground_truth/pose",
                             lambda m: gt.append((time.monotonic(), m.pose.position.x, m.pose.position.y)),
                             qos_profile_sensor_data)
    stamped = a.type == "TwistStamped"
    pub = node.create_publisher(TwistStamped if stamped else Twist, "/cmd_vel", 10)

    def send(vx):
        msg = TwistStamped() if stamped else Twist()
        (msg.twist if stamped else msg).linear.x = vx
        pub.publish(msg)

    def spin_until(t_end, vx=None):
        """Крутить колбэки до t_end; vx не None — публиковать его с частотой rate_hz. Время последней публикации."""
        t_pub, t_next = None, time.monotonic()
        while time.monotonic() < t_end:
            if vx is not None and time.monotonic() >= t_next:
                send(vx)
                t_pub, t_next = time.monotonic(), t_next + 1.0 / a.rate_hz
            rclpy.spin_once(node, timeout_sec=0.002)
        return t_pub

    deadline = time.monotonic() + a.wait_s
    while not (odom and gt and pub.get_subscription_count()) and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.1)
    if not (odom and gt and pub.get_subscription_count()):
        node.destroy_node()
        rclpy.shutdown()
        raise SystemExit("stm32_probe: за %.0f с нет /odom, /ground_truth/pose или подписчика /cmd_vel" % a.wait_s)
    t_first = time.monotonic()
    t_last = spin_until(t_first + a.drive_s, a.vx)
    spin_until(t_last + a.silence_s)
    spin_until(time.monotonic() + 0.5, 0.0)
    node.destroy_node()
    rclpy.shutdown()
    return dict(analyze(odom, gt, t_first, t_last, a.silence_s), vx=a.vx, drive_s=a.drive_s, silence_s=a.silence_s,
                rate_hz=a.rate_hz, type=a.type)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vx", type=float, default=0.2)
    ap.add_argument("--drive-s", type=float, default=1.5)
    ap.add_argument("--silence-s", type=float, default=2.0)
    ap.add_argument("--rate-hz", type=float, default=10.0)
    ap.add_argument("--type", choices=("Twist", "TwistStamped"), default="Twist")
    ap.add_argument("--wait-s", type=float, default=15.0, help="ожидание /odom, истинной позы и подписчика /cmd_vel")
    print(json.dumps(run(ap.parse_args()), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
