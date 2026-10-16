#!/usr/bin/env python3
"""(в ВМ sim) Готовность стенда neurobot: ждёт темы и связи, а не время. JSON в stdout; код 0 — готов, 1 — нет.

Стенд готов, когда:
  /clock, /scan, /odom, /ground_truth/pose — пришло хотя бы по одному сообщению (истинная поза идёт через
  ленивый мост — подписка этого скрипта её и включает);
  /cmd_vel — подписан stm32_sim;
  /stand/base_cmd_vel — публикует stm32_sim, подписан мост gz_bridge (вход DiffDrive).
Чего не хватает — в "missing" по-русски (для start_sim.sh и диагностики). Вместе с готовностью отдаются геометрия
и сводка первого /scan, тип и участники /cmd_vel и список тем — по ним check.sh проверяет профиль. Сводка скана:
n_nan — лучей NaN; n_below_min — конечных ниже range_min (коды ошибок URG); min_valid — ближайшее в
[range_min, range_max]; front_min — то же прямо по курсу, |угол| ≤ 10°.
  python3 stand_ready.py [--timeout 30]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time

MSG_TOPICS = ("/clock", "/scan", "/odom", "/ground_truth/pose")
CMD_VEL = "/cmd_vel"
BASE_CMD_VEL = "/stand/base_cmd_vel"
FRONT_RAD = math.radians(10)


def missing(seen, cmd_vel_subs, base_pubs, base_subs):
    """Чего не хватает до готовности. seen — темы, по которым пришло сообщение; *_subs/*_pubs — имена узлов."""
    out = ["%s: нет сообщений" % t for t in MSG_TOPICS if t not in seen]
    if "stm32_sim" not in cmd_vel_subs:
        out.append("%s: не подписан stm32_sim" % CMD_VEL)
    if "stm32_sim" not in base_pubs:
        out.append("%s: не публикует stm32_sim" % BASE_CMD_VEL)
    if "gz_bridge" not in base_subs:
        out.append("%s: не подписан мост gz_bridge" % BASE_CMD_VEL)
    return out


def scan_geometry(msg):
    rays = [(msg.angle_min + i * msg.angle_increment, r) for i, r in enumerate(msg.ranges)]
    valid = [(a, r) for a, r in rays if msg.range_min <= r <= msg.range_max]
    return {"n": len(msg.ranges), "angle_min": msg.angle_min, "angle_max": msg.angle_max,
            "angle_increment": msg.angle_increment, "range_min": msg.range_min, "range_max": msg.range_max,
            "frame_id": msg.header.frame_id,
            "n_nan": sum(1 for _, r in rays if math.isnan(r)),
            "n_below_min": sum(1 for _, r in rays if math.isfinite(r) and r < msg.range_min),
            "min_valid": min((r for _, r in valid), default=None),
            "front_min": min((r for a, r in valid if abs(a) <= FRONT_RAD), default=None)}


def run(timeout_s):
    import rclpy
    from geometry_msgs.msg import PoseStamped
    from nav_msgs.msg import Odometry
    from rclpy.qos import qos_profile_sensor_data
    from rosgraph_msgs.msg import Clock
    from sensor_msgs.msg import LaserScan

    rclpy.init()
    node = rclpy.create_node("stand_ready")
    seen = {}

    def keep(topic):
        def cb(msg):
            seen.setdefault(topic, msg)
        return cb

    # best effort совместим и с reliable-, и с best-effort-издателями.
    for topic, mtype in zip(MSG_TOPICS, (Clock, LaserScan, Odometry, PoseStamped)):
        node.create_subscription(mtype, topic, keep(topic), qos_profile_sensor_data)

    def names(infos):
        return sorted(i.node_name for i in infos)

    t0 = time.monotonic()
    while True:
        rclpy.spin_once(node, timeout_sec=0.1)
        cmd_subs = node.get_subscriptions_info_by_topic(CMD_VEL)
        lack = missing(seen, names(cmd_subs), names(node.get_publishers_info_by_topic(BASE_CMD_VEL)),
                       names(node.get_subscriptions_info_by_topic(BASE_CMD_VEL)))
        if not lack or time.monotonic() - t0 > timeout_s:
            break
    out = {"ready": not lack, "waited_s": round(time.monotonic() - t0, 2), "missing": lack,
           "cmd_vel": {"types": sorted({i.topic_type for i in cmd_subs}), "subscribers": names(cmd_subs),
                       "publishers": names(node.get_publishers_info_by_topic(CMD_VEL))},
           "scan": scan_geometry(seen["/scan"]) if "/scan" in seen else None,
           "topics": sorted(t for t, _ in node.get_topic_names_and_types())}
    node.destroy_node()
    rclpy.shutdown()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--timeout", type=float, default=30.0, help="сколько ждать, с")
    out = run(ap.parse_args().timeout)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
