#!/usr/bin/env python3
"""(в ВМ sim) Готовность двойника: ждёт темы и связи, а не время. JSON в stdout; код 0 — готов, 1 — нет.

Двойник готов, когда:
  /clock, /scan, /odom, /joint_states, /camera/imu, /imu/data, /ground_truth/pose, /twin/truth — пришло хотя бы по
  одному сообщению; /tf_static — есть кадры laser_frame и camera_link;
  controller_manager: diff_cont и joint_broad — active;
  цепочка движения связана: /cmd_vel читает twist_mux, /diff_cont/cmd_vel_unstamped пишет twist_mux и читает
  twin_cmd_relay, /diff_cont/cmd_vel читает diff_cont.
/odom нужен и в режиме hold (cmd_vel_timeout 0): начальный ноль контроллеру даёт twin_cmd_relay, как у контроллера
Foxy на роботе. Камера (ленивая) готовности не требует. Чего не хватает — в "missing" по-русски; вместе с готовностью
отдаются режим hold (из controllers.yaml прогона), участники входов twist_mux (чужие писатели) и список тем.
  python3 twin_ready.py [--timeout 60] [--run-dir ~/twin_runs/current]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from twin_config import CMD_VEL, CONTROLLERS, DIFF_CMD, MUX_INPUTS, MUX_OUT  # noqa: E402

MSG_TOPICS = ("/clock", "/scan", "/odom", "/joint_states", "/camera/imu", "/imu/data", "/ground_truth/pose",
              "/twin/truth")
STATIC_FRAMES = ("laser_frame", "camera_link")


def hold_mode(run_dir: str) -> bool:
    """cmd_vel_timeout прогона равен 0 (hold): из controllers.yaml, который launch пишет в run_dir."""
    with open(os.path.join(run_dir, "controllers.yaml")) as fh:
        return float(yaml.safe_load(fh)["diff_cont"]["ros__parameters"]["cmd_vel_timeout"]) == 0


def missing(seen, static_frames, controllers, links):
    """Чего не хватает до готовности. seen — темы с сообщениями; static_frames — кадры из /tf_static;
    controllers — {имя: состояние}; links — {(тема, 'sub'|'pub'): [узлы]}."""
    out = ["%s: нет сообщений" % t for t in MSG_TOPICS if t not in seen]
    out += ["/tf_static: нет кадра %s" % f for f in STATIC_FRAMES if f not in static_frames]
    out += ["controller_manager: %s — %s" % (c, controllers.get(c, "не загружен")) for c in CONTROLLERS
            if controllers.get(c) != "active"]
    for topic, kind, node in ((CMD_VEL, "sub", "twist_mux"), (MUX_OUT, "pub", "twist_mux"),
                              (MUX_OUT, "sub", "twin_cmd_relay"), (DIFF_CMD, "sub", "diff_cont")):
        if node not in links.get((topic, kind), []):
            out.append("%s: %s не %s" % (topic, node, "подписан" if kind == "sub" else "публикует"))
    return out


def run(timeout_s, hold):
    import rclpy
    from controller_manager_msgs.srv import ListControllers
    from geometry_msgs.msg import PoseStamped
    from nav_msgs.msg import Odometry
    from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
    from rosgraph_msgs.msg import Clock
    from sensor_msgs.msg import Imu, JointState, LaserScan
    from std_msgs.msg import String
    from tf2_msgs.msg import TFMessage

    rclpy.init()
    node = rclpy.create_node("twin_ready")
    seen, static = set(), set()

    def keep(topic):
        return lambda _msg: seen.add(topic)

    types = (Clock, LaserScan, Odometry, JointState, Imu, Imu, PoseStamped, String)
    for topic, mtype in zip(MSG_TOPICS, types):
        node.create_subscription(mtype, topic, keep(topic), qos_profile_sensor_data)
    node.create_subscription(TFMessage, "/tf_static", lambda m: static.update(t.child_frame_id for t in m.transforms),
                             QoSProfile(depth=100, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    lister = node.create_client(ListControllers, "/controller_manager/list_controllers")
    controllers, future = {}, None

    def names(infos):
        return sorted(i.node_name for i in infos)

    def links():
        out = {}
        for topic in (CMD_VEL, MUX_OUT, DIFF_CMD) + tuple(MUX_INPUTS):
            out[(topic, "sub")] = names(node.get_subscriptions_info_by_topic(topic))
            out[(topic, "pub")] = names(node.get_publishers_info_by_topic(topic))
        return out

    t0 = time.monotonic()
    while True:
        rclpy.spin_once(node, timeout_sec=0.1)
        if future is None and lister.service_is_ready():
            future = lister.call_async(ListControllers.Request())
        elif future is not None and future.done():
            controllers = {c.name: c.state for c in future.result().controller}
            future = None
        graph = links()
        lack = missing(seen, static, controllers, graph)
        if not lack or time.monotonic() - t0 > timeout_s:
            break
    out = {"ready": not lack, "waited_s": round(time.monotonic() - t0, 2), "missing": lack, "hold": hold,
           "controllers": controllers,
           "cmd_vel": {t: {"publishers": graph[(t, "pub")], "subscribers": graph[(t, "sub")],
                           "priority": MUX_INPUTS[t]} for t in MUX_INPUTS},
           "topics": sorted(t for t, _ in node.get_topic_names_and_types())}
    node.destroy_node()
    rclpy.shutdown()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--timeout", type=float, default=60.0, help="сколько ждать, с")
    ap.add_argument("--run-dir", default=os.path.join(os.environ.get("TWIN_RUN_ROOT", os.path.expanduser(
        "~/twin_runs")), "current"), help="каталог прогона двойника (режим hold — для ответа)")
    a = ap.parse_args()
    out = run(a.timeout, hold_mode(a.run_dir))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
