#!/usr/bin/env python3
"""Пошаговый режим стенда робота: часы симуляции ведёт клиент (реплеер), а не Gazebo.

Нужен для ускоренных прогонов «рабочего дня» робота (research/new_topic/robot_scenario.md §7): мир стоит на паузе,
клиент продвигает его вызовом /world/<мир>/control (ros_gz_interfaces/srv/ControlWorld через ros_gz_bridge, стенд
запущен с start_sim.sh --world-control) до нужного сим. времени, пока агент думает — мир стоит. Только rosbridge
(websockets + cbor2), без ROS на клиенте.

Подкоманды (JSON-результат в stdout и в --out):
  free   --seconds 20                 эталон: мир идёт сам (без паузы), RTF по /clock за S секунд стенного времени
  step   --dt 0.1 0.5 1 5 --total 20  пауза, затем шаги run_to_sim_time по dt: стенное время шага, ускорение,
                                      накладные расходы шага (сколько стоит «продвинуть часы» на один шаг)
  tour   --places dock east … --dt 0.5 [--free]
                                      цели Nav2 (без feedback) по местам; мир продвигается шагами dt, пока не придёт
                                      результат: сим. время и стенное время участка, статус, ошибка по истинной позе,
                                      длина пути по /ground_truth/pose. --free — без шагов (мир идёт сам)
  hold   --seconds 30                 мир на паузе S секунд стенного времени, затем одна цель Nav2: переживает ли стек
                                      долгую паузу (агент думает, LLM отвечает медленно)
  reads  [--dt 0.1 0.5]               чтение по запросу на паузе: /scan, JPEG-кадр, /amcl_pose — приходит ли первое
                                      сообщение без шага мира и после шага dt (отдельное соединение на подписку)
Часы: подписка /clock с throttle_rate 20 мс и queue_length 1 (rosbridge доставляет последнее значение после
интервала, поэтому момент остановки виден). Трафик клиента стенда в метрики «платы» не входит.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import cbor2
import websockets

PLACES = {  # как в robot_mcp_compact.py (research/new_topic/robot_scenario.md §2)
    "dock": (-2.0, -0.5), "east": (1.8, 0.5), "east_south": (1.8, -0.5), "north": (0.0, 1.8),
    "south": (0.5, -1.7), "north_east": (1.6, 1.6), "center_ne": (0.55, 0.55), "center_sw": (-0.55, -0.55),
}
STATUS = {4: "succeeded", 5: "canceled", 6: "aborted"}


def now() -> float:
    return time.monotonic()


def yaw_quat(yaw: float) -> dict:
    return {"x": 0.0, "y": 0.0, "z": math.sin(yaw / 2), "w": math.cos(yaw / 2)}


class Stand:
    """Одно WS-соединение с rosbridge, фоновый разбор кадров: часы, истинная поза, ответы сервисов, итоги целей."""

    def __init__(self, url: str, world: str, timeout: float):
        self.url, self.world, self.timeout = url, world, timeout
        self.clock: float | None = None
        self.clock_wall: float = 0.0
        self.gt: tuple[float, float] | None = None
        self.gt_path_m = 0.0
        self.gt_track = False
        self.pending: dict[str, asyncio.Future] = {}
        self.results: dict[str, dict] = {}
        self.rx = self.tx = 0
        self._id = 0
        self._clock_evt = asyncio.Event()

    async def __aenter__(self):
        self.ws = await asyncio.wait_for(websockets.connect(self.url, max_size=64 << 20, ping_interval=None,
                                                            compression=None), self.timeout)
        self.reader = asyncio.create_task(self._read())
        await self.send({"op": "subscribe", "id": "clk", "topic": "/clock", "type": "rosgraph_msgs/msg/Clock",
                         "throttle_rate": 20, "queue_length": 1, "compression": "cbor"})
        await self.send({"op": "subscribe", "id": "gt", "topic": "/ground_truth/pose",
                         "type": "geometry_msgs/msg/PoseStamped", "throttle_rate": 100, "queue_length": 1,
                         "compression": "cbor"})
        return self

    async def __aexit__(self, *exc):
        self.reader.cancel()
        await self.ws.close()

    def nid(self, p: str) -> str:
        self._id += 1
        return f"{p}:{self._id}"

    async def send(self, obj: dict) -> None:
        s = json.dumps(obj, separators=(",", ":"))
        self.tx += len(s)
        await self.ws.send(s)

    async def _read(self) -> None:
        async for raw in self.ws:
            self.rx += len(raw)
            msg = cbor2.loads(raw) if isinstance(raw, bytes) else json.loads(raw)
            op = msg.get("op")
            if op == "publish" and msg.get("topic") == "/clock":
                c = msg["msg"]["clock"]
                self.clock = c["sec"] + c["nanosec"] * 1e-9
                self.clock_wall = now()
                self._clock_evt.set()
            elif op == "publish" and msg.get("topic") == "/ground_truth/pose":
                p = msg["msg"]["pose"]["position"]
                xy = (p["x"], p["y"])
                if self.gt_track and self.gt is not None:
                    self.gt_path_m += math.hypot(xy[0] - self.gt[0], xy[1] - self.gt[1])
                self.gt = xy
            elif op == "service_response":
                f = self.pending.pop(msg.get("id"), None)
                if f and not f.done():
                    f.set_result(msg)
            elif op == "action_result":
                self.results[msg.get("id")] = msg

    async def call(self, service: str, args: dict, timeout: float | None = None) -> dict:
        cid = self.nid("call")
        fut = asyncio.get_running_loop().create_future()
        self.pending[cid] = fut
        await self.send({"op": "call_service", "id": cid, "service": service, "args": args})
        return await asyncio.wait_for(fut, timeout or self.timeout)

    async def control(self, **wc: Any) -> bool:
        r = await self.call(f"/world/{self.world}/control", {"world_control": wc})
        return bool(r.get("result")) and bool((r.get("values") or {}).get("success"))

    async def wait_clock(self, timeout: float = 10.0) -> float:
        t0 = now()
        while self.clock is None:
            self._clock_evt.clear()
            await asyncio.wait_for(self._clock_evt.wait(), max(0.05, timeout - (now() - t0)))
        return self.clock

    async def run_to(self, target: float, timeout: float = 120.0) -> float:
        """Продвинуть мир до сим. времени target (мир на паузе до и после). Возвращает стенное время шага."""
        t0 = now()
        sec = int(target)
        ok = await self.control(run_to_sim_time={"sec": sec, "nanosec": int(round((target - sec) * 1e9))})
        if not ok:
            raise RuntimeError("run_to_sim_time отклонён")
        while self.clock is None or self.clock < target - 0.0015:
            self._clock_evt.clear()
            try:
                await asyncio.wait_for(self._clock_evt.wait(), max(0.05, timeout - (now() - t0)))
            except asyncio.TimeoutError:
                raise RuntimeError(f"часы не дошли до {target:.3f} за {timeout} с (сейчас {self.clock})")
        return now() - t0

    async def send_goal(self, x: float, y: float, yaw: float = 0.0) -> str:
        gid = self.nid("nav")
        goal = {"pose": {"header": {"frame_id": "map"},
                         "pose": {"position": {"x": x, "y": y, "z": 0.0}, "orientation": yaw_quat(yaw)}}}
        await self.send({"op": "send_action_goal", "id": gid, "action": "/navigate_to_pose",
                         "action_type": "nav2_msgs/action/NavigateToPose", "args": goal, "feedback": False})
        return gid


async def cmd_free(a, s: Stand) -> dict:
    await s.control(pause=False)
    await asyncio.sleep(1.0)
    c0, w0 = await s.wait_clock(), now()
    await asyncio.sleep(a.seconds)
    c1, w1 = s.clock, now()
    return {"mode": "free", "wall_s": round(w1 - w0, 3), "sim_s": round(c1 - c0, 3), "rtf": round((c1 - c0) / (w1 - w0), 3)}


async def cmd_step(a, s: Stand) -> dict:
    assert await s.control(pause=True)
    await asyncio.sleep(0.5)
    rows = []
    for dt in a.dt:
        c = await s.wait_clock()
        n = max(1, int(round(a.total / dt)))
        walls = []
        w0 = now()
        for i in range(n):
            walls.append(await s.run_to(c + dt * (i + 1)))
        w = now() - w0
        walls.sort()
        rows.append({"dt_s": dt, "steps": n, "sim_s": round(dt * n, 3), "wall_s": round(w, 3),
                     "speedup": round(dt * n / w, 3),
                     "step_wall_ms": {"p50": round(walls[len(walls) // 2] * 1000, 1),
                                      "max": round(walls[-1] * 1000, 1)}})
        print(json.dumps(rows[-1], ensure_ascii=False), file=sys.stderr)
    return {"mode": "step", "rows": rows}


async def leg(a, s: Stand, name: str, free: bool) -> dict:
    x, y = PLACES[name]
    await s.wait_clock()
    start_sim, start_wall = s.clock, now()
    s.gt_path_m, s.gt_track = 0.0, True
    gid = await s.send_goal(x, y)
    steps, step_walls = 0, []
    while gid not in s.results:
        if s.clock - start_sim > a.leg_limit:
            await s.send({"op": "cancel_action_goal", "id": gid, "action": "/navigate_to_pose"})
            break
        if free:
            await asyncio.sleep(0.05)
        else:
            step_walls.append(await s.run_to(s.clock + a.dt))
            steps += 1
    res = s.results.get(gid)
    end_sim, end_wall = s.clock, now()
    s.gt_track = False
    if not free:  # истинная поза после остановки: ещё один короткий шаг, чтобы PosePublisher опубликовал
        await s.run_to(s.clock + 0.2)
    else:
        await asyncio.sleep(0.3)
    gt = s.gt
    out = {"place": name, "goal": [x, y], "status": STATUS.get(res.get("status"), res.get("status")) if res else "timeout",
           "sim_s": round(end_sim - start_sim, 2), "wall_s": round(end_wall - start_wall, 2),
           "speedup": round((end_sim - start_sim) / max(1e-6, end_wall - start_wall), 2),
           "gt_err_m": round(math.hypot(gt[0] - x, gt[1] - y), 3) if gt else None,
           "gt_path_m": round(s.gt_path_m, 2)}
    if steps:
        step_walls.sort()
        out.update(steps=steps, step_wall_ms_p50=round(step_walls[len(step_walls) // 2] * 1000, 1))
    print(json.dumps(out, ensure_ascii=False), file=sys.stderr)
    return out


async def cmd_tour(a, s: Stand) -> dict:
    if a.free:
        await s.control(pause=False)
    else:
        assert await s.control(pause=True)
    await asyncio.sleep(0.5)
    rx0 = s.rx
    legs = [await leg(a, s, p, a.free) for p in a.places]
    sim = sum(x["sim_s"] for x in legs)
    wall = sum(x["wall_s"] for x in legs)
    return {"mode": "tour_free" if a.free else "tour_lockstep", "dt_s": None if a.free else a.dt, "legs": legs,
            "succeeded": sum(x["status"] == "succeeded" for x in legs), "sim_s": round(sim, 2),
            "wall_s": round(wall, 2), "speedup": round(sim / wall, 2), "client_rx_bytes": s.rx - rx0}


async def cmd_hold(a, s: Stand) -> dict:
    assert await s.control(pause=True)
    await asyncio.sleep(0.5)                       # последние (прореженные) значения /clock до паузы
    c0 = await s.wait_clock()
    await asyncio.sleep(a.seconds)
    frozen = abs((s.clock or c0) - c0) < 0.002
    a.free = False
    r = await leg(a, s, a.places[0], False)
    return {"mode": "hold", "hold_wall_s": a.seconds, "clock_frozen_during_hold": frozen, "leg_after": r}


async def first_message(s: Stand, topic: str, typ: str, wait: float, step_dt: float | None = None) -> dict:
    """Подписка отдельным соединением → первое сообщение; при step_dt через 0,3 с мир продвигается на step_dt."""
    ws = await websockets.connect(s.url, max_size=64 << 20, ping_interval=None, compression=None)
    await ws.send(json.dumps({"op": "subscribe", "id": "first", "topic": topic, "type": typ, "queue_length": 1,
                              "compression": "cbor"}))
    t0, got, info = now(), asyncio.Event(), {}

    async def rd():
        async for raw in ws:
            m = cbor2.loads(raw) if isinstance(raw, bytes) else json.loads(raw)
            if m.get("op") == "publish":
                info.update(ms=round((now() - t0) * 1000, 1), bytes=len(raw))
                got.set()
                return

    task = asyncio.create_task(rd())
    if step_dt:
        await asyncio.sleep(0.3)
        await s.run_to(s.clock + step_dt)
    try:
        await asyncio.wait_for(got.wait(), wait)
    except asyncio.TimeoutError:
        info["timeout_s"] = wait
    task.cancel()
    await ws.close()
    return info


async def cmd_reads(a, s: Stand) -> dict:
    assert await s.control(pause=True)
    await asyncio.sleep(0.5)
    out = {"mode": "reads", "note": "ms включает 0,3 с ожидания до шага"}
    for name, topic, typ in (("scan", "/scan", "sensor_msgs/msg/LaserScan"),
                             ("jpeg", "/camera/image_raw/compressed", "sensor_msgs/msg/CompressedImage"),
                             ("amcl", "/amcl_pose", "geometry_msgs/msg/PoseWithCovarianceStamped")):
        out[name + "_paused"] = await first_message(s, topic, typ, 4.0)
        for dt in a.dt:
            out[f"{name}_step{dt}"] = await first_message(s, topic, typ, 6.0, dt)
    return out


async def main_async(a) -> dict:
    async with Stand(a.url, a.world, a.timeout) as s:
        await s.wait_clock(15)
        t0 = now()
        out = await {"free": cmd_free, "step": cmd_step, "tour": cmd_tour, "hold": cmd_hold,
                         "reads": cmd_reads}[a.cmd](a, s)
        if a.cmd != "free" and not a.keep_paused:
            await s.control(pause=False)   # вернуть мир в обычный ход
        out.update(url=a.url, world=a.world, total_wall_s=round(now() - t0, 2), client_tx_bytes=s.tx)
        return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="ws://127.0.0.1:9090")
    p.add_argument("--world", default="default")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--out")
    p.add_argument("--keep-paused", action="store_true")
    sp = p.add_subparsers(dest="cmd", required=True)
    f = sp.add_parser("free")
    f.add_argument("--seconds", type=float, default=20.0)
    st = sp.add_parser("step")
    st.add_argument("--dt", type=float, nargs="+", default=[0.1, 0.5, 1.0, 5.0])
    st.add_argument("--total", type=float, default=20.0, help="сим. секунд на каждое dt")
    for name in ("tour", "hold"):
        t = sp.add_parser(name)
        t.add_argument("--places", nargs="+", default=["east", "north", "center_sw", "south", "dock"]
                       if name == "tour" else ["east"], choices=sorted(PLACES))
        t.add_argument("--dt", type=float, default=0.5)
        t.add_argument("--leg-limit", type=float, default=120.0, help="предел участка, сим. с")
        t.add_argument("--free", action="store_true")
        if name == "hold":
            t.add_argument("--seconds", type=float, default=30.0)
    rd = sp.add_parser("reads")
    rd.add_argument("--dt", type=float, nargs="+", default=[0.1, 0.5])
    a = p.parse_args()
    out = asyncio.run(asyncio.wait_for(main_async(a), 900))
    s = json.dumps(out, ensure_ascii=False, indent=1)
    print(s)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(s + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
