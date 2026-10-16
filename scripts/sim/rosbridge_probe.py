#!/usr/bin/env python3
"""Проверка и замеры симулятора через rosbridge (WebSocket, протокол rosbridge v2) — с хоста или из VM.

Без ROS на клиенте: только websockets (+ cbor2, Pillow для режимов compression=cbor/cbor-raw/png).
Подкоманды (результат — JSON в stdout, при --out ещё и в файл):
  topics                          список тем/типов через rosapi
  rates   [--topics ...]          частота, байт/сообщение и байт/с по каждой теме (по очереди, одна подписка за раз)
          [--compression none|cbor|cbor-raw|png] [--throttle-ms N] [--seconds S]
  drive   --vx 0.2 --wz 0 --seconds 3   публикует cmd_vel, сравнивает /odom до/после
  snapshot [--topic /camera/image_raw/compressed] [--n 10] [--gap 3]  кадр по запросу: задержка и размер
  nav     --x X --y Y [--yaw R] [--init-x ... ]  /initialpose (опц.) + действие navigate_to_pose, ждёт результат
Трафик считается по длине кадров WebSocket (полезная нагрузка, без заголовков WS/TCP).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import math
import sys
import time
from typing import Any

import websockets

DEFAULT_TOPICS = ["/scan", "/odom", "/imu", "/camera/image_raw", "/camera/camera_info", "/clock", "/tf"]


def _now() -> float:
    return time.monotonic()


def decode_frame(raw: str | bytes) -> dict:
    """Кадр rosbridge -> dict. Поддержка JSON, CBOR (бинарный кадр) и op=png."""
    if isinstance(raw, bytes):
        import cbor2

        return cbor2.loads(raw)
    msg = json.loads(raw)
    if msg.get("op") == "png":
        from PIL import Image

        img = Image.open(io.BytesIO(base64.b64decode(msg["data"])))
        data = img.tobytes().rstrip(b"\x00")
        return json.loads(data.decode("utf-8"))
    return msg


class Bridge:
    def __init__(self, url: str, timeout: float):
        self.url = url
        self.timeout = timeout
        self.ws = None
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.rx_frames = 0
        self._id = 0

    async def __aenter__(self):
        t0 = _now()
        self.ws = await asyncio.wait_for(
            websockets.connect(self.url, max_size=64 * 1024 * 1024, ping_interval=None, compression=None),
            self.timeout,
        )
        self.connect_ms = (_now() - t0) * 1000
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()

    def nid(self, p: str) -> str:
        self._id += 1
        return f"{p}:{self._id}"

    async def send(self, obj: dict) -> None:
        s = json.dumps(obj, separators=(",", ":"))
        self.tx_bytes += len(s.encode())
        await self.ws.send(s)

    async def recv(self, timeout: float | None = None) -> tuple[dict, int]:
        raw = await asyncio.wait_for(self.ws.recv(), timeout or self.timeout)
        n = len(raw) if isinstance(raw, bytes) else len(raw.encode())
        self.rx_bytes += n
        self.rx_frames += 1
        return decode_frame(raw), n

    async def call_service(self, service: str, args: dict | None = None, timeout: float | None = None) -> Any:
        cid = self.nid("call")
        await self.send({"op": "call_service", "id": cid, "service": service, "args": args or {}})
        deadline = _now() + (timeout or self.timeout)
        while True:
            msg, _ = await self.recv(max(0.1, deadline - _now()))
            if msg.get("op") == "service_response" and msg.get("id") == cid:
                if not msg.get("result", True):
                    raise RuntimeError(f"service {service} failed: {msg.get('values')}")
                return msg.get("values")

    async def topic_type(self, topic: str) -> str:
        return (await self.call_service("/rosapi/topic_type", {"topic": topic})).get("type", "")

    async def wait_one(self, topic: str, msg_type: str | None = None, timeout: float = 10.0) -> dict:
        sid = self.nid("once")
        sub = {"op": "subscribe", "id": sid, "topic": topic, "queue_length": 1}
        if msg_type:
            sub["type"] = msg_type
        await self.send(sub)
        deadline = _now() + timeout
        try:
            while True:
                msg, _ = await self.recv(max(0.1, deadline - _now()))
                if msg.get("op") == "publish" and msg.get("topic") == topic:
                    return msg["msg"]
        finally:
            await self.send({"op": "unsubscribe", "id": sid, "topic": topic})


def yaw_of(q: dict) -> float:
    return math.atan2(2 * (q["w"] * q["z"] + q["x"] * q["y"]), 1 - 2 * (q["y"] ** 2 + q["z"] ** 2))


def quat(yaw: float) -> dict:
    return {"x": 0.0, "y": 0.0, "z": math.sin(yaw / 2), "w": math.cos(yaw / 2)}


def pose_of(odom: dict) -> dict:
    p = odom["pose"]["pose"]
    return {"x": p["position"]["x"], "y": p["position"]["y"], "yaw": yaw_of(p["orientation"])}


async def cmd_topics(a) -> dict:
    async with Bridge(a.url, a.timeout) as b:
        v = await b.call_service("/rosapi/topics")
        return {"connect_ms": round(b.connect_ms, 1), "topics": dict(zip(v["topics"], v["types"]))}


async def measure_topic(b: Bridge, topic: str, seconds: float, compression: str, throttle_ms: int) -> dict:
    sid = b.nid("rate")
    sub = {"op": "subscribe", "id": sid, "topic": topic, "queue_length": 1, "throttle_rate": throttle_ms}
    if compression != "none":
        sub["compression"] = compression
    await b.send(sub)
    stamps: list[float] = []
    sizes: list[int] = []
    first_wait = None
    t_sub = _now()
    deadline = t_sub + seconds
    while _now() < deadline:
        try:
            msg, n = await b.recv(max(0.05, deadline - _now()))
        except asyncio.TimeoutError:
            break
        if msg.get("op") == "publish" and msg.get("topic") == topic:
            t = _now()
            if first_wait is None:
                first_wait = t - t_sub
            stamps.append(t)
            sizes.append(n)
    await b.send({"op": "unsubscribe", "id": sid, "topic": topic})
    # Дочитать хвост, чтобы он не попал в замер следующей темы.
    t_end = _now() + 0.3
    while _now() < t_end:
        try:
            await b.recv(0.1)
        except asyncio.TimeoutError:
            break
    out: dict[str, Any] = {"topic": topic, "compression": compression, "throttle_ms": throttle_ms, "n": len(stamps)}
    if first_wait is not None:
        out["first_msg_s"] = round(first_wait, 3)
    if len(stamps) >= 2:
        span = stamps[-1] - stamps[0]
        out["hz"] = round((len(stamps) - 1) / span, 2) if span > 0 else None
        out["bytes_per_msg"] = round(sum(sizes) / len(sizes))
        out["bytes_per_s"] = round(sum(sizes[1:]) / span) if span > 0 else None
    elif sizes:
        out["bytes_per_msg"] = sizes[0]
    return out


async def cmd_rates(a) -> dict:
    res = []
    async with Bridge(a.url, a.timeout) as b:
        for topic in a.topics:
            for comp in a.compression:
                res.append(await measure_topic(b, topic, a.seconds, comp, a.throttle_ms))
                print(json.dumps(res[-1], ensure_ascii=False), file=sys.stderr)
        return {"connect_ms": round(b.connect_ms, 1), "seconds": a.seconds, "results": res}


async def cmd_snapshot(a) -> dict:
    """Кадр «по запросу»: subscribe → первое сообщение → unsubscribe, N раз с паузой (ленивая цепочка успевает
    остановиться). Задержка — от отправки subscribe до получения кадра."""
    lat, sizes = [], []
    async with Bridge(a.url, a.timeout) as b:
        for i in range(a.n):
            sid = b.nid("snap")
            sub = {"op": "subscribe", "id": sid, "topic": a.topic, "queue_length": 1}
            if a.compression != "none":
                sub["compression"] = a.compression
            t0 = _now()
            await b.send(sub)
            got = None
            while got is None:
                msg, n = await b.recv(a.timeout)
                if msg.get("op") == "publish" and msg.get("topic") == a.topic:
                    got = n
            lat.append(_now() - t0)
            sizes.append(got)
            await b.send({"op": "unsubscribe", "id": sid, "topic": a.topic})
            t_end = _now() + a.gap
            while _now() < t_end:  # дочитать хвост
                try:
                    await b.recv(max(0.05, t_end - _now()))
                except asyncio.TimeoutError:
                    break
        srt = sorted(lat)
        return {"topic": a.topic, "compression": a.compression, "n": a.n, "gap_s": a.gap,
                "latency_s": {"min": round(srt[0], 3), "p50": round(srt[len(srt) // 2], 3),
                              "p95": round(srt[min(len(srt) - 1, int(0.95 * len(srt)))], 3), "max": round(srt[-1], 3)},
                "bytes_per_frame": round(sum(sizes) / len(sizes)), "tx_bytes": b.tx_bytes, "rx_bytes": b.rx_bytes}


async def resolve_cmd_vel_type(b: Bridge, topic: str) -> str:
    t = await b.topic_type(topic)
    return t or "geometry_msgs/msg/TwistStamped"


def twist_msg(msg_type: str, vx: float, wz: float) -> dict:
    tw = {"linear": {"x": vx, "y": 0.0, "z": 0.0}, "angular": {"x": 0.0, "y": 0.0, "z": wz}}
    if msg_type.endswith("TwistStamped"):
        return {"header": {"frame_id": "base_link"}, "twist": tw}
    return tw


async def cmd_drive(a) -> dict:
    async with Bridge(a.url, a.timeout) as b:
        mtype = a.cmd_type or await resolve_cmd_vel_type(b, a.cmd_topic)
        before = pose_of(await b.wait_one("/odom", "nav_msgs/msg/Odometry"))
        await b.send({"op": "advertise", "topic": a.cmd_topic, "type": mtype})
        t0 = _now()
        n = 0
        while _now() - t0 < a.seconds:
            await b.send({"op": "publish", "topic": a.cmd_topic, "msg": twist_msg(mtype, a.vx, a.wz)})
            n += 1
            await asyncio.sleep(0.1)
        for _ in range(3):
            await b.send({"op": "publish", "topic": a.cmd_topic, "msg": twist_msg(mtype, 0.0, 0.0)})
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        after = pose_of(await b.wait_one("/odom", "nav_msgs/msg/Odometry"))
        await b.send({"op": "unadvertise", "topic": a.cmd_topic})
        dist = math.hypot(after["x"] - before["x"], after["y"] - before["y"])
        dyaw = math.atan2(math.sin(after["yaw"] - before["yaw"]), math.cos(after["yaw"] - before["yaw"]))
        return {
            "cmd_topic": a.cmd_topic, "cmd_type": mtype, "vx": a.vx, "wz": a.wz, "wall_s": a.seconds,
            "published": n, "odom_before": before, "odom_after": after,
            "moved_m": round(dist, 3), "turned_rad": round(dyaw, 3),
            "ok": dist > 0.05 if a.vx else abs(dyaw) > 0.05,
            "tx_bytes": b.tx_bytes, "rx_bytes": b.rx_bytes,
        }


async def cmd_nav(a) -> dict:
    async with Bridge(a.url, a.timeout) as b:
        if a.init_x is not None:
            init = {
                "header": {"frame_id": "map"},
                "pose": {
                    "pose": {"position": {"x": a.init_x, "y": a.init_y, "z": 0.0}, "orientation": quat(a.init_yaw)},
                    "covariance": [0.25 if i in (0, 7) else 0.0685 if i == 35 else 0.0 for i in range(36)],
                },
            }
            await b.send({"op": "advertise", "topic": "/initialpose",
                          "type": "geometry_msgs/msg/PoseWithCovarianceStamped"})
            for _ in range(3):
                await b.send({"op": "publish", "topic": "/initialpose", "msg": init})
                await asyncio.sleep(0.5)
            await asyncio.sleep(a.settle)
        start = pose_of(await b.wait_one("/odom", "nav_msgs/msg/Odometry"))
        gid = b.nid("nav")
        goal = {"pose": {"header": {"frame_id": "map"},
                         "pose": {"position": {"x": a.x, "y": a.y, "z": 0.0}, "orientation": quat(a.yaw)}}}
        t0 = _now()
        await b.send({"op": "send_action_goal", "id": gid, "action": "/navigate_to_pose",
                      "action_type": "nav2_msgs/action/NavigateToPose", "args": goal, "feedback": not a.no_feedback})
        fb_n, fb_bytes, last_fb = 0, 0, None
        result = None
        deadline = t0 + a.nav_timeout
        while _now() < deadline:
            try:
                msg, n = await b.recv(max(0.1, deadline - _now()))
            except asyncio.TimeoutError:
                break
            op = msg.get("op")
            if op == "action_feedback" and msg.get("id") == gid:
                fb_n += 1
                fb_bytes += n
                last_fb = msg.get("values")
            elif op == "action_result" and msg.get("id") == gid:
                result = msg
                break
        wall = _now() - t0
        if result is None:
            await b.send({"op": "cancel_action_goal", "id": gid, "action": "/navigate_to_pose"})
        end = pose_of(await b.wait_one("/odom", "nav_msgs/msg/Odometry"))
        rx_nav = b.rx_bytes
        gt = None
        try:  # истинная поза из Gazebo (если симуляция запущена с ground_truth_hz > 0)
            g = (await b.wait_one("/ground_truth/pose", "geometry_msgs/msg/PoseStamped", timeout=5.0))["pose"]
            gt = {"x": round(g["position"]["x"], 3), "y": round(g["position"]["y"], 3),
                  "yaw": round(yaw_of(g["orientation"]), 3)}
        except (asyncio.TimeoutError, KeyError):
            pass
        out = {
            "goal": {"x": a.x, "y": a.y, "yaw": a.yaw}, "wall_s": round(wall, 2),
            "status": None if result is None else result.get("status"),
            "result_ok": None if result is None else result.get("result"),
            # GoalStatus: 4 SUCCEEDED, 5 CANCELED, 6 ABORTED
            "succeeded": result is not None and result.get("status") == 4,
            "feedback_msgs": fb_n, "feedback_bytes": fb_bytes,
            "odom_start": start, "odom_end": end,
            "odom_path_straight_m": round(math.hypot(end["x"] - start["x"], end["y"] - start["y"]), 3),
            "feedback_requested": not a.no_feedback,
            "tx_bytes": b.tx_bytes, "rx_bytes_until_result": rx_nav,
        }
        if gt:
            out["ground_truth_end"] = gt
            out["ground_truth_err_m"] = round(math.hypot(gt["x"] - a.x, gt["y"] - a.y), 3)
        if last_fb:
            fb = last_fb.get("feedback", last_fb)
            out["last_feedback"] = {k: fb.get(k) for k in ("distance_remaining", "number_of_recoveries",
                                                          "navigation_time", "estimated_time_remaining") if k in fb}
            cp = fb.get("current_pose", {}).get("pose", {}).get("position")
            if cp:
                out["map_pose_end"] = {"x": round(cp["x"], 3), "y": round(cp["y"], 3)}
                out["map_err_m"] = round(math.hypot(cp["x"] - a.x, cp["y"] - a.y), 3)
        return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="ws://127.0.0.1:9090")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--out", help="записать JSON-результат в файл")
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("topics")
    r = sp.add_parser("rates")
    r.add_argument("--topics", nargs="+", default=DEFAULT_TOPICS)
    r.add_argument("--compression", nargs="+", default=["none"], choices=["none", "cbor", "cbor-raw", "png"])
    r.add_argument("--throttle-ms", type=int, default=0)
    r.add_argument("--seconds", type=float, default=5.0)
    sn = sp.add_parser("snapshot")
    sn.add_argument("--topic", default="/camera/image_raw/compressed")
    sn.add_argument("--compression", default="none", choices=["none", "cbor", "cbor-raw", "png"])
    sn.add_argument("--n", type=int, default=10)
    sn.add_argument("--gap", type=float, default=3.0)
    d = sp.add_parser("drive")
    d.add_argument("--vx", type=float, default=0.2)
    d.add_argument("--wz", type=float, default=0.0)
    d.add_argument("--seconds", type=float, default=3.0)
    d.add_argument("--cmd-topic", default="/cmd_vel")
    d.add_argument("--cmd-type", default=None, help="по умолчанию — тип темы через rosapi")
    n = sp.add_parser("nav")
    n.add_argument("--x", type=float, required=True)
    n.add_argument("--y", type=float, required=True)
    n.add_argument("--yaw", type=float, default=0.0)
    n.add_argument("--init-x", type=float, default=None)
    n.add_argument("--init-y", type=float, default=0.0)
    n.add_argument("--init-yaw", type=float, default=0.0)
    n.add_argument("--settle", type=float, default=3.0, help="пауза после /initialpose, с")
    n.add_argument("--nav-timeout", type=float, default=120.0)
    n.add_argument("--no-feedback", action="store_true", help="не запрашивать action_feedback (экономия трафика)")
    a = p.parse_args()
    fn = {"topics": cmd_topics, "rates": cmd_rates, "snapshot": cmd_snapshot, "drive": cmd_drive, "nav": cmd_nav}[a.cmd]
    res = asyncio.run(fn(a))
    s = json.dumps(res, ensure_ascii=False, indent=1)
    print(s)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(s + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
