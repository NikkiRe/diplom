#!/usr/bin/env python3
"""Сценарные события стенда робота через rosbridge: препятствие и «человек» как модели Gazebo.

Это зародыш драйвера стенда (research/new_topic/robot_scenario.md §4, §11): события дня вида
{"kind": "world", "op": "spawn", "model": "box", "at": "east"} исполняются здесь, агент о них не знает и видит
только последствия (лидар, цель Nav2 прервана, детектор). Нужен стенд с start_sim.sh --world-control
(сервисы /world/<мир>/{create,remove,set_pose} через ros_gz_bridge).

Подкоманды (JSON в stdout и --out):
  spawn  --name N --shape box|person --x X --y Y   создать модель (box — ящик 0,4 м; person — цилиндр Ø0,4 × 1,7 м)
  remove --name N                                   удалить модель
  move   --name N --x X --y Y                       переставить модель
  blocked --place east [--shape box] [--dt 0.5]     ящик ставится в точку цели, цель Nav2 в пошаговом режиме:
                                                    сколько сим. секунд до ABORTED (это событие агенту), трафик;
                                                    затем ящик убирается
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lockstep_probe import PLACES, Stand, leg  # noqa: E402

SHAPES = {
    "box": '<box><size>0.4 0.4 0.4</size></box>', "person": '<cylinder><radius>0.2</radius><length>1.7</length></cylinder>',
}
HEIGHT = {"box": 0.2, "person": 0.85}


def sdf(name: str, shape: str) -> str:
    g = SHAPES[shape]
    return (f'<?xml version="1.0"?><sdf version="1.9"><model name="{name}"><static>true</static><link name="l">'
            f'<collision name="c"><geometry>{g}</geometry></collision>'
            f'<visual name="v"><geometry>{g}</geometry></visual></link></model></sdf>')


async def spawn(s: Stand, name: str, shape: str, x: float, y: float) -> dict:
    t0 = time.monotonic()
    r = await s.call(f"/world/{s.world}/create", {"entity_factory": {
        "name": name, "allow_renaming": False, "sdf": sdf(name, shape),
        "pose": {"position": {"x": x, "y": y, "z": HEIGHT[shape]}, "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}},
        "relative_to": "world"}})
    return {"op": "spawn", "name": name, "shape": shape, "at": [x, y], "ok": bool((r.get("values") or {}).get("success")),
            "ms": round((time.monotonic() - t0) * 1000, 1)}


async def remove(s: Stand, name: str) -> dict:
    t0 = time.monotonic()
    r = await s.call(f"/world/{s.world}/remove", {"entity": {"name": name, "type": 2}})
    return {"op": "remove", "name": name, "ok": bool((r.get("values") or {}).get("success")),
            "ms": round((time.monotonic() - t0) * 1000, 1)}


async def move(s: Stand, name: str, x: float, y: float) -> dict:
    t0 = time.monotonic()
    r = await s.call(f"/world/{s.world}/set_pose", {"entity": {"name": name, "type": 2}, "pose": {
        "position": {"x": x, "y": y, "z": 0.2}, "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}}})
    return {"op": "move", "name": name, "at": [x, y], "ok": bool((r.get("values") or {}).get("success")),
            "ms": round((time.monotonic() - t0) * 1000, 1)}


async def main_async(a) -> dict:
    async with Stand(a.url, a.world, a.timeout) as s:
        await s.wait_clock(15)
        if a.cmd == "spawn":
            return await spawn(s, a.name, a.shape, a.x, a.y)
        if a.cmd == "remove":
            return await remove(s, a.name)
        if a.cmd == "move":
            return await move(s, a.name, a.x, a.y)
        # blocked: препятствие в точке цели, цель в пошаговом режиме
        assert await s.control(pause=True)
        await asyncio.sleep(0.5)
        x, y = PLACES[a.place]
        sp = await spawn(s, "obstacle_probe", a.shape, x, y)
        rx0 = s.rx
        r = await leg(a, s, a.place, False)
        rm = await remove(s, "obstacle_probe")
        await s.control(pause=False)
        return {"op": "blocked", "spawn": sp, "leg": r, "remove": rm, "client_rx_bytes_leg": s.rx - rx0}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="ws://127.0.0.1:9090")
    p.add_argument("--world", default="default")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--out")
    sp = p.add_subparsers(dest="cmd", required=True)
    for name in ("spawn", "move"):
        c = sp.add_parser(name)
        c.add_argument("--name", required=True)
        c.add_argument("--x", type=float, required=True)
        c.add_argument("--y", type=float, required=True)
        if name == "spawn":
            c.add_argument("--shape", choices=sorted(SHAPES), default="box")
    sp.add_parser("remove").add_argument("--name", required=True)
    b = sp.add_parser("blocked")
    b.add_argument("--place", default="east", choices=sorted(PLACES))
    b.add_argument("--shape", choices=sorted(SHAPES), default="box")
    b.add_argument("--dt", type=float, default=0.5)
    b.add_argument("--leg-limit", type=float, default=240.0)
    a = p.parse_args()
    out = asyncio.run(asyncio.wait_for(main_async(a), 600))
    s = json.dumps(out, ensure_ascii=False, indent=1)
    print(s)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(s + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
