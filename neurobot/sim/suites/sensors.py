#!/usr/bin/env python3
"""Набор «датчики работают» на двойнике с пакетом neurobot целиком (в ВМ sim, root, после setup_twin.sh и
systemctl start neurobot.target):

    python3 sensors.py --out DIR [проверка …]      по умолчанию — все проверки мира lab по порядку

graph — темы, типы и писатели как на роботе (R7a §1.5) и цепочка движения; rates — частоты по времени приёма; tf —
кадры /tf_static и frame_id датчиков; blind — слепой сектор лидара (корпус D435i) против скана робота 02.06 и маска
safety_gate; depth — глубина видит низкий ящик, лидар над ним не видит; imu — покой (g, наклон, гироскоп) и поворот
на месте (гироскоп против истины); odom — маршрут «1 м — поворот 90° — 1 м» через исполнитель, одометрия против
истины; silence — /scan и глубина замолкли на ходу (SIGSTOP узла датчиков) → стоп safety_gate, после возврата
движение само не продолжается; chain — «помахал — поехал»: жест gesturegen → perception → … → колёса Gazebo;
slope — наклон на склоне 12° (мир ramp: IMU, madgwick, истина).
Итог — DIR/sensors.json (на проверку: ok, значения, критерии). Числа — ориентир двойника (Gazebo в общей ВМ), не
результат робота.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)), "/opt/neurobot/src"]

import metrics as m  # noqa: E402
from harness import CFG, Twin  # noqa: E402
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data  # noqa: E402
from sensor_msgs.msg import CameraInfo, Image, Imu, JointState, LaserScan  # noqa: E402
from tf2_msgs.msg import TFMessage  # noqa: E402

from neurobot.safety.config import Params  # noqa: E402
from neurobot.safety.depth import Camera, Sampler, classify, image_array  # noqa: E402
from neurobot.safety.geometry import StaticTf  # noqa: E402

log = logging.getLogger("sensors")
AGENT = CFG.get("NEUROBOT_AGENT_TOPIC")
# темы робота (R7a §1.5; глубина — имя realsense2_camera по умолчанию) и их частоты на двойнике (README двойника §2.3)
ROBOT_TOPICS = {
    "/scan": ("sensor_msgs/msg/LaserScan", 10.0), "/odom": ("nav_msgs/msg/Odometry", 50.0),
    "/tf": ("tf2_msgs/msg/TFMessage", None), "/tf_static": ("tf2_msgs/msg/TFMessage", None),
    "/joint_states": ("sensor_msgs/msg/JointState", 50.0), "/camera/color/image_raw": ("sensor_msgs/msg/Image", 15.0),
    "/camera/color/camera_info": ("sensor_msgs/msg/CameraInfo", 15.0),
    "/camera/depth/image_rect_raw": ("sensor_msgs/msg/Image", 15.0),
    "/camera/depth/camera_info": ("sensor_msgs/msg/CameraInfo", 15.0), "/camera/imu": ("sensor_msgs/msg/Imu", 200.0),
    "/imu/data": ("sensor_msgs/msg/Imu", 200.0),
}
PACKAGE_TOPICS = {"/neurobot/safety_state": 5.0, "/neurobot/motion_state": 2.0, "/neurobot/perception_health": 1.0}
# цепочка движения: тема → (кто пишет, кто читает) — только эти узлы (подписчиков-наблюдателей, как этот, — можно)
CHAIN = {"/cmd_vel": (["safety_gate"], ["twist_mux"]),
         "/neurobot/cmd_vel_req": (["motion_executor"], ["safety_gate"]),
         "/cmd_vel_gesture": (["velocity_publisher"], ["motion_executor"]),
         "/diff_cont/cmd_vel_unstamped": (["twist_mux"], ["twin_cmd_relay"]),
         "/cmd_vel_joy": ([], ["twist_mux"]), "/cmd_vel_tracker": ([], ["twist_mux"])}
ROBOT_BLIND = (-60.8, 4.2)      # блок NaN скана робота 02.06 (R7b §3.3), лучи 168–353
LASER_Z, CAM_Z = 0.67, 0.665    # плоскость скана и оптический центр D435i [оценка по фото, R7b §2.3]
START = (-1.0, 1.0, 0.01, -math.pi / 2)  # мир lab: на юг, проём северной стены за спиной, впереди 1,9 м свободно


class Sensors:
    def __init__(self, twin: Twin):
        self.t = twin
        self.static = StaticTf()
        self.tf_frames = set()
        latched = QoSProfile(depth=100, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                             reliability=ReliabilityPolicy.RELIABLE)
        twin.node.create_subscription(TFMessage, "/tf_static", self._tf_static, latched)

    def _tf_static(self, msg) -> None:
        for tr in msg.transforms:
            p, q = tr.transform.translation, tr.transform.rotation
            self.static.add(tr.header.frame_id, tr.child_frame_id, (p.x, p.y, p.z), (q.x, q.y, q.z, q.w))
            self.tf_frames.add(tr.child_frame_id)

    def listen(self, topic: str, typ, seconds: float, keep: bool = False):
        """Сообщения темы за seconds: [(t приёма, сообщение или None)]."""
        got = []
        sub = self.t.node.create_subscription(typ, topic, lambda msg: got.append((time.monotonic(),
                                                                                  msg if keep else None)),
                                              qos_profile_sensor_data)
        self.t.spin(seconds)
        self.t.node.destroy_subscription(sub)
        return got

    # ---- проверки

    def graph(self) -> dict:
        self.t.spin(2.0)
        node = self.t.node
        have = dict(node.get_topic_names_and_types())
        topics = {}
        for name, (typ, _) in ROBOT_TOPICS.items():
            pubs = sorted(i.node_name for i in node.get_publishers_info_by_topic(name))
            topics[name] = {"type": have.get(name), "publishers": pubs, "ok": have.get(name) == [typ] and bool(pubs)}
        chain = {}
        for name, (writers, readers) in CHAIN.items():
            pubs = sorted(i.node_name for i in node.get_publishers_info_by_topic(name))
            subs = sorted(i.node_name for i in node.get_subscriptions_info_by_topic(name)
                          if i.node_name != node.get_name())
            chain[name] = {"publishers": pubs, "subscribers": subs, "ok": pubs == writers and set(readers) <= set(subs)}
        return {"ok": all(v["ok"] for v in topics.values()) and all(v["ok"] for v in chain.values()),
                "topics": topics, "chain": chain}

    def rates(self, seconds: float = 10.0) -> dict:
        types = {"/scan": LaserScan, "/joint_states": JointState, "/camera/imu": Imu, "/imu/data": Imu,
                 "/camera/color/image_raw": Image, "/camera/depth/image_rect_raw": Image,
                 "/camera/color/camera_info": CameraInfo, "/camera/depth/camera_info": CameraInfo}
        out = {}
        for topic, typ in types.items():
            hz = m.rate([t for t, _ in self.listen(topic, typ, seconds / 2)])
            want = ROBOT_TOPICS[topic][1]
            out[topic] = {"hz": None if hz is None else round(hz, 2), "want": want,
                          "ok": hz is not None and abs(hz - want) <= 0.15 * want}
        t0 = time.monotonic()
        self.t.spin(seconds)
        odom = [r["t"] for r in self.t.since("odom", t0)]
        out["/odom"] = {"hz": round(m.rate(odom) or 0.0, 2), "want": 50.0, "ok": abs((m.rate(odom) or 0) - 50) <= 7.5}
        for topic, key in (("/neurobot/safety_state", "state"), ("/neurobot/motion_state", "motion"),
                           ("/neurobot/perception_health", "health")):
            hz = m.rate([r["t"] for r in self.t.since(key, t0)])
            want = PACKAGE_TOPICS[topic]
            out[topic] = {"hz": None if hz is None else round(hz, 2), "want_min": want * 0.8,
                          "ok": hz is not None and hz >= want * 0.8}
        truth = self.t.since("truth", t0)
        return {"ok": all(v["ok"] for v in out.values()), "topics": out,
                "rtf_min": min((r["rtf"] for r in truth if r.get("rtf") is not None), default=None)}

    def tf(self) -> dict:
        self.t.spin(1.0)
        scan = self.listen("/scan", LaserScan, 1.0, keep=True)
        depth = self.listen("/camera/depth/image_rect_raw", Image, 1.0, keep=True)
        imu = self.listen("/camera/imu", Imu, 0.5, keep=True)
        frames = {"scan": scan[-1][1].header.frame_id if scan else None,
                  "depth": depth[-1][1].header.frame_id if depth else None,
                  "imu": imu[-1][1].header.frame_id if imu else None}
        poses = {}
        for name in ("laser_frame", "camera_depth_optical_frame", "camera_color_optical_frame",
                     "camera_imu_optical_frame"):
            p = self.static.lookup("base_link", name)
            poses[name] = None if p is None else [round(float(v), 3) for v in p.pos]
        odom = self.t.last("odom")
        ok = (frames == {"scan": "laser_frame", "depth": "camera_depth_optical_frame",
                         "imu": "camera_imu_optical_frame"} and None not in poses.values()
              and abs(poses["laser_frame"][2] - LASER_Z) < 0.01
              and abs(poses["camera_depth_optical_frame"][2] - CAM_Z) < 0.01 and odom is not None)
        return {"ok": ok, "frames": frames, "poses_in_base_link": poses, "static_frames": sorted(self.tf_frames)}

    def blind(self, scans: int = 30) -> dict:
        got = self.listen("/scan", LaserScan, scans / 10.0 + 0.5, keep=True)
        if not got:
            return {"ok": False, "error": "нет /scan"}
        msg = got[-1][1]
        stable = np.ones(len(msg.ranges), bool)
        for _, s in got:
            stable &= ~np.isfinite(np.asarray(s.ranges, float))
        runs = m.nan_runs([math.nan if f else 1.0 for f in stable], msg.angle_min, msg.angle_increment, 3)
        block = m.block_at(runs, -30.0, math.degrees(msg.angle_increment) * 2.5)
        state = self.t.last("state") or {}
        meta = {"n": len(msg.ranges), "angle_min": round(msg.angle_min, 6), "angle_max": round(msg.angle_max, 6),
                "range_min": msg.range_min, "range_max": msg.range_max, "frame": msg.header.frame_id}
        holes = [r for r in m.nan_runs(list(msg.ranges), msg.angle_min, msg.angle_increment)
                 if block and block[0] <= r[0] <= block[1]]
        ok = (meta["n"] == 683 and block is not None and abs(block[0] - ROBOT_BLIND[0]) <= 1.0
              and abs(block[1] - ROBOT_BLIND[1]) <= 1.0)
        return {"ok": ok, "scan": meta, "stable_nan_runs": runs, "camera_block": block, "robot_block": ROBOT_BLIND,
                "runs_inside_block_last_scan": holes, "gate_mask_deg": state.get("lidar_mask_deg")}

    def depth(self) -> dict:
        """Низкий ящик (0,15 м) на +30° от курса: глубина его видит, лучи лидара над ним идут дальше (до стены)."""
        t = self.t
        t.relocate(-1.5, -0.3, 0.01, math.radians(-30.0) + math.atan2(0.3, 1.5))
        info = self.listen("/camera/depth/camera_info", CameraInfo, 1.0, keep=True)
        frames = self.listen("/camera/depth/image_rect_raw", Image, 1.0, keep=True)
        scans = self.listen("/scan", LaserScan, 1.0, keep=True)
        if not (info and frames and scans):
            return {"ok": False, "error": "нет глубины или скана"}
        k, img = info[-1][1].k, frames[-1][1]
        cam = Camera(k[0], k[4], k[2], k[5], img.width, img.height)
        pose = self.static.lookup("base_link", img.header.frame_id)
        image, scale = image_array(img.encoding, img.data, img.height, img.width, img.step, bool(img.is_bigendian))
        sampler = Sampler(cam, Params().depth_cols)
        points, _ = sampler.points(sampler.depths(image, scale))
        h = classify(pose.apply(points), pose.pos, Params(), 1.0)
        truth = t.last("truth")
        bx, by = 0.0 - truth["x"], 0.0 - truth["y"]             # центр ящика в мире lab — (0, 0)
        c, s = math.cos(truth["yaw"]), math.sin(truth["yaw"])
        box = (c * bx + s * by, -s * bx + c * by)               # центр ящика в базе
        near = h.obstacle[np.hypot(h.obstacle[:, 0] - box[0], h.obstacle[:, 1] - box[1]) < 0.3] \
            if len(h.obstacle) else h.obstacle
        scan = scans[-1][1]
        bearing = math.degrees(math.atan2(box[1], box[0] + 0.015))
        i = int(round((math.radians(bearing) - scan.angle_min) / scan.angle_increment))
        rays = [r for r in scan.ranges[max(i - 3, 0):i + 4]]
        dist = math.hypot(box[0] + 0.015, box[1])
        over = all(math.isfinite(r) and r > dist + 0.3 for r in rays)
        return {"ok": len(near) >= 3 and over, "box_in_base": [round(v, 3) for v in box],
                "box_bearing_deg": round(bearing, 1), "depth_points_on_box": int(len(near)),
                "depth_nearest_m": round(float(np.min(np.hypot(near[:, 0], near[:, 1]))), 3) if len(near) else None,
                "lidar_rays_at_box_m": [None if not math.isfinite(r) else round(r, 2) for r in rays],
                "lidar_over_box": over, "gate_front_m": (t.last("state") or {}).get("front_m")}

    def imu(self) -> dict:
        t = self.t
        t.relocate(-1.5, -0.8, 0.01, 0.0)
        t.spin(1.0)
        rest = [msg for _, msg in self.listen("/camera/imu", Imu, 2.0, keep=True)]
        acc = np.mean([[r.linear_acceleration.x, r.linear_acceleration.y, r.linear_acceleration.z] for r in rest], 0)
        gyro = np.mean([[r.angular_velocity.x, r.angular_velocity.y, r.angular_velocity.z] for r in rest], 0)
        state = t.last("state") or {}
        rot = self.static.lookup("base_link", rest[-1].header.frame_id).rot
        turn = []
        sub = t.node.create_subscription(Imu, "/camera/imu", lambda msg: turn.append(
            (time.monotonic(), msg.angular_velocity)), qos_profile_sensor_data)
        t0 = time.monotonic()
        t.spin(4.0, drive={AGENT: (0.0, 0.5)})
        t.spin(1.5, drive={AGENT: (0.0, 0.0)})
        t.node.destroy_subscription(sub)
        steady = [(tt, a) for tt, a in turn if t0 + 1.5 <= tt <= t0 + 3.5]
        w_imu = float(np.mean([(rot @ np.array([a.x, a.y, a.z]))[2] for _, a in steady])) if steady else math.nan
        truth = [r for r in t.since("truth", t0 + 1.5, t0 + 3.5)]
        w_truth = float(np.mean([r["w"] for r in truth])) if truth else math.nan
        g = float(np.linalg.norm(acc))
        ok = (abs(g - 9.81) < 0.2 and m.tilt_deg(*acc) < 1.0 and float(np.linalg.norm(gyro)) < 0.02
              and w_truth > 0.4 and abs(w_imu - w_truth) < 0.05 and (state.get("tilt_deg") or 0.0) < 1.0)
        return {"ok": ok, "rest": {"g_mps2": round(g, 3), "tilt_accel_deg": round(m.tilt_deg(*acc), 2),
                                   "gyro_norm": round(float(np.linalg.norm(gyro)), 4),
                                   "gate_tilt_deg": state.get("tilt_deg")},
                "turn": {"cmd_w": 0.5, "imu_w_base": round(w_imu, 3), "truth_w": round(w_truth, 3)}}

    def odom(self) -> dict:
        """Маршрут через исполнитель (вход агента): 1 м на юг, поворот на 90° вправо, 1 м на запад — по одометрии.
        Проём северной стены всё время за спиной робота: «нет отражения» вперёд не мешает."""
        t = self.t
        t.relocate(*START)
        t0 = time.monotonic()

        def leg(dist=0.0, angle=0.0, limit=20.0):
            start = t.last("odom")

            def done():
                o = t.last("odom")
                d = math.atan2(math.sin(o["yaw"] - start["yaw"]), math.cos(o["yaw"] - start["yaw"]))
                return math.hypot(o["x"] - start["x"], o["y"] - start["y"]) >= dist and abs(d) >= abs(angle)
            t.spin(limit, until=done, drive={AGENT: (0.15 if dist else 0.0, math.copysign(0.4, angle) if angle else 0)})
            t.spin(1.0, drive={AGENT: (0.0, 0.0)})
        leg(dist=1.0)
        leg(angle=math.radians(-88.0))
        leg(dist=1.0)
        odom = t.since("odom", t0)
        truth = t.since("truth", t0)
        err = m.odom_error(odom, truth)
        return {"ok": bool(err) and err["path_m"] > 1.8 and err["max_pos_m"] < 0.05 and err["max_yaw_deg"] < 2.0,
                **err, "rtf_ok_share": round(len(m.ok_truth(truth)) / max(len(truth), 1), 3)}

    def silence(self) -> dict:
        """/scan и глубина замолкли на ходу: SIGSTOP узла датчиков двойника (на роботе — urg_node2 и камера)."""
        t = self.t
        t.relocate(*START)
        pid = t.unit_pid("neurobot-driver@twin", "sensor_model.py")
        t.spin(3.0, drive={AGENT: (0.15, 0.0)})
        scans = []
        sub = t.node.create_subscription(LaserScan, "/scan", lambda _: scans.append(time.monotonic()),
                                         qos_profile_sensor_data)
        t.spin(0.5, drive={AGENT: (0.15, 0.0)})
        os.kill(pid, signal.SIGSTOP)
        t_stop = time.monotonic()
        t.spin(3.0, drive={AGENT: (0.15, 0.0)})
        os.kill(pid, signal.SIGCONT)
        t_cont = time.monotonic()
        t.spin(3.0, drive={AGENT: (0.15, 0.0)})       # исполнитель всё ещё просит вперёд — движение не продолжается
        resumed = [r for r in t.since("truth", t_cont + 1.0) if abs(r["v"]) > 0.02]
        t.spin(1.0, drive={AGENT: (0.0, 0.0)})        # ноль — взвод
        t_zero = time.monotonic()
        t.spin(2.0, drive={AGENT: (0.15, 0.0)})
        moves = [r for r in t.since("truth", t_zero + 0.5) if abs(r["v"]) > 0.02]
        t.spin(1.0, drive={AGENT: (0.0, 0.0)})
        t.node.destroy_subscription(sub)
        last_scan = max([s for s in scans if s <= t_stop + 0.2], default=t_stop)
        zero = m.zero_after(t.rec["cmd"], last_scan)
        stale = m.first(t.rec["state"], last_scan, lambda s: any(r.startswith("stale_scan") for r in s["reasons"]))
        br = m.braking(t.rec["truth"], last_scan)
        ok = (zero is not None and zero - last_scan <= 0.6 and br["stop_s"] is not None and not resumed
              and bool(moves) and stale is not None)
        return {"ok": ok, "zero_cmd_after_last_scan_s": None if zero is None else round(zero - last_scan, 3),
                "reason": stale["reasons"][0] if stale else None, **br, "resumed_without_zero": bool(resumed),
                "moves_after_zero_request": bool(moves)}

    def slope(self) -> dict:
        """Мир ramp: робот поставлен на склон 12° (вниз по склону — без защёлки нельзя проверить наклон в покое)."""
        t = self.t
        slope = math.radians(12.0)
        t.relocate(0.0, 0.0, 0.8 * math.tan(slope) + 0.003, 0.0, -slope, arm=False)
        t.spin(4.0)
        rest = [msg for _, msg in self.listen("/camera/imu", Imu, 1.0, keep=True)]
        acc = np.mean([[r.linear_acceleration.x, r.linear_acceleration.y, r.linear_acceleration.z] for r in rest], 0)
        truth = t.last("truth")
        state = t.last("state") or {}
        tilt = m.tilt_deg(*acc)
        t.relocate(-1.5, 0.0, 0.01, 0.0, arm=False)     # защёлку tilt снимает человек, когда робот снова на ровном
        t.spin(2.0)
        release = t.cli("estop", "release", "--reason", "проверка: снят со склона", answer="release")
        return {"ok": abs(tilt - 12.0) < 1.0 and abs(abs(math.degrees(truth["pitch"])) - 12.0) < 1.0
                and abs((state.get("tilt_deg") or 0.0) - 12.0) < 1.5,
                "tilt_accel_deg": round(tilt, 2), "truth_pitch_deg": round(math.degrees(truth["pitch"]), 2),
                "gate_tilt_deg": state.get("tilt_deg"), "gate_state": state.get("state"),
                "gate_reasons": state.get("reasons"), "human_release_flat_rc": release["rc"]}


    def chain(self) -> dict:
        """«Помахал — поехал» на пакете целиком: новый таймлайн gesturegen (формы, которые сеть лаборатории относит к
        задуманному классу: v, two → 1 come; swipe, wave → 2 go_left/right, README gestures §3) и новый якорь —
        perception (FakeYolo по таймлайну, SNN лаборатории) → /cmd_vel_gesture → исполнитель → safety_gate →
        twist_mux → diff_drive_controller → колёса Gazebo. Метки: t0 — конец жеста по истине gesturegen."""
        t = self.t
        gest = Path(CFG.get("NEUROBOT_GESTURES_TIMELINE")).parent
        t.systemctl("stop", "neurobot-perception")
        t.relocate(*START)
        subprocess.run(["sudo", "-u", "neurobot", "env", "PYTHONPATH=/var/lib/neurobot-twin/src",
                        "PYTHONDONTWRITEBYTECODE=1", "python3", "-m", "neurobot.sim.gestures.gesturegen", "gen",
                        "--seed", "17", "--gestures", "4", "--negatives", "1", "--classes", "1,2",
                        "--shapes", "v,swipe,two,wave", "--lead-in-s", "8", "--force", "--out-dir", str(gest)],
                       check=True, stdout=subprocess.DEVNULL)
        (gest / "anchor.json").unlink(missing_ok=True)
        t.systemctl("start", "neurobot-perception")
        anchor = json.loads((gest / "anchor.json").read_text(encoding="utf-8"))
        truth = [json.loads(x) for x in (gest / "truth.jsonl").read_text(encoding="utf-8").splitlines()[1:]]
        end = anchor["t_mono"] + max(r["t_leave"] for r in truth) + 16.0
        t.spin(max(end - time.monotonic(), 0.0))
        rows, false_actions = [], 0
        for seg, nxt in zip(truth, truth[1:] + [None]):
            t0 = anchor["t_mono"] + seg["t0"]
            t_next = anchor["t_mono"] + nxt["t_start"] if nxt else end
            rec = m.first(t.rec["gesture"], t0 - 1.0, lambda g: True)
            rec = rec if rec and rec["t"] < t_next else None
            cmd = m.first(t.rec["cmd"], t0, lambda c: c["v"] != 0.0 or c["w"] != 0.0)
            cmd = cmd if cmd and cmd["t"] < t_next else None
            move = m.first(t.rec["truth"], t0, lambda r: abs(r["v"]) > 0.05 or abs(r["w"]) > 0.1)
            move = move if move and move["t"] < t_next else None
            if seg["kind"] == "negative" and (cmd or move):
                false_actions += 1
            rows.append({"kind": seg["kind"], "shape": seg["shape"], "cls": seg["cls"], "side": seg["side"],
                         "recognized": rec and {k: rec.get(k) for k in ("cls", "name", "conf", "side", "n_boxes")},
                         "t0_to_rec_s": rec and round(rec["t"] - t0, 3), "t0_to_cmd_s": cmd and round(cmd["t"] - t0, 3),
                         "t0_to_motion_s": move and round(move["t"] - t0, 3),
                         "path_m": round(m.path_length(t.since("truth", t0, t_next)), 3)})
        moved = [r for r in rows if r["kind"] == "gesture" and r["t0_to_motion_s"] is not None]
        return {"ok": bool(moved) and false_actions == 0, "gestures": rows, "moved": len(moved),
                "false_actions": false_actions, "contacts": m.touched(t.since("truth", anchor["t_mono"], end))}


CHECKS = ("graph", "rates", "tf", "blind", "depth", "imu", "odom", "silence", "chain")


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="каталог результатов (sensors.json)")
    ap.add_argument("checks", nargs="*", choices=CHECKS + ("slope",), metavar="проверка")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    twin = Twin("neurobot_sensors")
    s = Sensors(twin)
    path = out / "sensors.json"
    results = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    try:
        for name in a.checks or CHECKS:
            log.info("проверка %s", name)
            wall = time.time()
            try:
                results[name] = dict(getattr(s, name)(), started=round(wall, 3))
            except Exception as e:  # noqa: BLE001 — проверка не прошла, остальные идут дальше
                log.exception("проверка %s упала", name)
                results[name] = {"ok": False, "error": repr(e), "started": round(wall, 3)}
            log.info("%s: %s", name, "ОК" if results[name]["ok"] else "НЕ ПРОШЛА")
            path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        twin.close()
    return 0 if all(r["ok"] for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
