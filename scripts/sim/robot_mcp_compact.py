#!/usr/bin/env python3
"""Прототип тонкого MCP-адаптера робота (R-compact) поверх rosbridge — для замеров против ros-mcp-server.

Идея: LLM получает 5 инструментов уровня задачи, а не примитивы ROS. Всё тяжёлое (сообщения ROS, ожидание
навигации, лидар, кадры) остаётся на плате; в LLM уходит короткий канонический JSON (≈30–80 токенов).

  status()                 где робот (место/координаты/курс), что делает навигация, свободно ли вокруг (лидар)
  go_to(place)             цель Nav2 в именованное место; ждёт результата локально (feedback не запрашивается)
  patrol(places)           обход нескольких мест одной командой (последовательность целей исполняется здесь)
  stop()                   отмена активной цели + нулевая скорость
  look(image=false)        что перед камерой: по умолчанию сводка по лидару в поле зрения камеры (детектора на
                           плате пока нет); image=true — один кадр JPEG (уменьшенный, если есть Pillow)

Отличия от ros-mcp-server 3.1.2, измеренные в docs/robot_sim.md §12:
  - одно постоянное WebSocket-соединение с rosbridge (а не новое на каждый вызов);
  - подписки только по запросу и с отпиской; /amcl_pose — постоянная, но событийная (latched, редкая);
  - navigate_to_pose без feedback (Nav2 шлёт его ~100 раз/с);
  - кадр камеры ждётся до --frame-timeout (ленивая камера отдаёт первый кадр ≈0,5–1 с);
  - /cmd_vel объявляется один раз при подключении (публикация stop без задержки обнаружения DDS);
  - ответ — один TextContent, без structuredContent (не удваивать трафик MCP).
Безопасность: LLM не может задать скорость или произвольную позу — только места из конфигурации (enum в схеме).

Зависимости: mcp (SDK 1.x), websockets; необязательно cbor2 (лидар в CBOR: 3 КБ вместо 8 КБ) и Pillow
(уменьшение кадра). Запуск: robot_mcp_compact.py --rosbridge ws://host.lima.internal:9090 [--places places.json]
Прототип для измерений; целевая реализация — Go рядом с hw-mcp (THESIS_CONCEPT, решение 2).
"""
# без "from __future__ import annotations": тип Place создаётся во время выполнения (enum мест в схеме)

import argparse
import asyncio
import base64
import io
import itertools
import json
import math
import struct
import sys
import time
from typing import Any, Literal

import websockets
from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations

NAV = "/navigate_to_pose"
NAV_TYPE = "nav2_msgs/action/NavigateToPose"
STATUS = {4: "succeeded", 5: "canceled", 6: "aborted"}
# turtlebot3_world, карта turtlebot3_navigation2/map/map.yaml (research/new_topic/robot_scenario.md §2)
DEFAULT_PLACES = {
    "dock": [-2.0, -0.5], "east": [1.8, 0.5], "east_south": [1.8, -0.5], "north": [0.0, 1.8],
    "south": [0.5, -1.7], "north_east": [1.6, 1.6], "center_ne": [0.55, 0.55], "center_sw": [-0.55, -0.55],
}
CAM_HFOV_DEG = 62.2      # waffle_pi: horizontal_fov 1.0856 рад


def cj(obj: Any) -> str:
    """Каноническая сериализация (IMPLEMENTATION_SPEC §0.5)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def yaw_of(q: dict) -> float:
    return math.atan2(2 * (q.get("w", 1) * q.get("z", 0) + q.get("x", 0) * q.get("y", 0)),
                      1 - 2 * (q.get("y", 0) ** 2 + q.get("z", 0) ** 2))


def decode_cbor(raw: bytes) -> dict:
    import cbor2

    def hook(*args):  # типизированные массивы rosbridge (RFC 8746): 85 — float32 LE, 86 — float64 LE
        # cbor2 < 6: hook(decoder, tag); cbor2 6.x: hook(tag, immutable)
        tag = next(x for x in args if isinstance(x, cbor2.CBORTag))
        if tag.tag in (85, 86):
            f = "f" if tag.tag == 85 else "d"
            n = len(tag.value) // struct.calcsize(f)
            return list(struct.unpack(f"<{n}{f}", tag.value))
        return tag

    return cbor2.loads(raw, tag_hook=hook)


class Bridge:
    """Одно постоянное соединение rosbridge v2: подписки по запросу, действия, публикация."""

    def __init__(self, url: str, compression: str):
        self.url = url
        self.compression = compression
        self.ws = None
        self.ids = itertools.count(1)
        self.waiters: dict[str, list[asyncio.Queue]] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.amcl: dict | None = None
        self.amcl_t: float | None = None
        self.lock = asyncio.Lock()
        self.reader: asyncio.Task | None = None

    async def ensure(self) -> None:
        async with self.lock:
            if self.ws is not None and self.reader is not None and not self.reader.done():
                return
            self.ws = await websockets.connect(self.url, max_size=1 << 25, ping_interval=None,
                                               compression=None, open_timeout=5)
            self.reader = asyncio.create_task(self._read())
            await self.send({"op": "advertise", "topic": "/cmd_vel", "type": "geometry_msgs/msg/TwistStamped"})
            # /amcl_pose: публикатор transient_local → rosbridge сам выберет latched QoS, придёт последняя поза
            await self.send({"op": "subscribe", "id": "amcl", "topic": "/amcl_pose",
                             "type": "geometry_msgs/msg/PoseWithCovarianceStamped"})

    async def send(self, obj: dict) -> None:
        await self.ws.send(json.dumps(obj, separators=(",", ":")))

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    m = decode_cbor(raw) if isinstance(raw, bytes) else json.loads(raw)
                except Exception as e:  # noqa: BLE001 — битый кадр не должен ронять чтение
                    print(f"[robot-compact] не разобран кадр ({len(raw)} Б): {e}", file=sys.stderr)
                    continue
                op = m.get("op")
                if op == "publish":
                    t = m.get("topic")
                    if t == "/amcl_pose":
                        self.amcl, self.amcl_t = m["msg"], time.monotonic()
                    for q in self.waiters.get(t, []):
                        q.put_nowait(m["msg"])
                elif op in ("action_result", "service_response"):
                    f = self.pending.pop(m.get("id"), None)
                    if f and not f.done():
                        f.set_result(m)
        except (websockets.WebSocketException, OSError):
            pass
        finally:
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(ConnectionError("rosbridge disconnected"))
            self.pending.clear()

    async def once(self, topic: str, typ: str, timeout: float, compression: str = "none") -> dict | None:
        """Первое сообщение темы: subscribe → ждать до timeout → unsubscribe."""
        await self.ensure()
        q: asyncio.Queue = asyncio.Queue()
        self.waiters.setdefault(topic, []).append(q)
        sid = f"once:{next(self.ids)}"
        msg = {"op": "subscribe", "id": sid, "topic": topic, "type": typ, "queue_length": 1}
        if compression != "none":
            msg["compression"] = compression
        await self.send(msg)
        try:
            return await asyncio.wait_for(q.get(), timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self.waiters[topic].remove(q)
            await self.send({"op": "unsubscribe", "id": sid, "topic": topic})

    async def goal(self, gid: str, goal: dict) -> asyncio.Future:
        await self.ensure()
        f = asyncio.get_running_loop().create_future()
        self.pending[gid] = f
        await self.send({"op": "send_action_goal", "id": gid, "action": NAV, "action_type": NAV_TYPE,
                         "args": goal, "feedback": False})
        return f

    async def cancel(self, gid: str) -> None:
        await self.send({"op": "cancel_action_goal", "id": gid, "action": NAV})

    async def zero(self) -> None:
        await self.send({"op": "publish", "topic": "/cmd_vel",
                         "msg": {"header": {"frame_id": "base_link"}, "twist": {}}})


def build(a: argparse.Namespace) -> FastMCP:
    spots: dict[str, list[float]] = DEFAULT_PLACES
    if a.places:
        with open(a.places) as f:
            spots = json.load(f)
    compression = a.compression
    if compression == "auto":
        try:
            import cbor2  # noqa: F401

            compression = "cbor"
        except ImportError:
            compression = "none"
    br = Bridge(a.rosbridge, compression)
    state: dict[str, Any] = {"active": None, "last": None}
    Place = Literal[tuple(spots)]  # enum в схеме инструмента: LLM видит допустимые места
    names = ", ".join(f"{k}({v[0]:g},{v[1]:g})" for k, v in spots.items())
    mcp = FastMCP("robot-compact", instructions=f"Мобильный робот TurtleBot3. Места (x,y в м, карта): {names}.")

    def pose() -> dict | None:
        if not br.amcl:
            return None
        p = br.amcl["pose"]["pose"]
        x, y = p["position"]["x"], p["position"]["y"]
        near = min(spots, key=lambda k: math.hypot(spots[k][0] - x, spots[k][1] - y))
        d = math.hypot(spots[near][0] - x, spots[near][1] - y)
        return {"x": round(x, 2), "y": round(y, 2), "heading_deg": round(math.degrees(yaw_of(p["orientation"]))),
                "near": near, "near_m": round(d, 2)}

    async def scan() -> dict | None:
        m = await br.once("/scan", "sensor_msgs/msg/LaserScan", a.sensor_timeout, br.compression)
        if not m:
            return None
        rmax = m.get("range_max", 3.5)
        a0, da = m["angle_min"], m["angle_increment"]
        pts = []                      # (дальность, пеленг в градусах: 0 — вперёд, + — влево)
        for i, r in enumerate(m["ranges"]):
            if r is None or not math.isfinite(r) or r < m.get("range_min", 0) or r > rmax:
                continue
            pts.append((r, (math.degrees(a0 + i * da) + 180) % 360 - 180))
        return {"pts": pts, "rmax": rmax, "inc_deg": math.degrees(da)}

    def sector(s: dict, pred) -> float | str:
        v = [r for r, d in s["pts"] if pred(d)]
        return round(min(v), 2) if v else f">{s['rmax']:g}"

    def scan_summary(s: dict | None) -> dict:
        if s is None:
            return {"lidar": "no data"}
        out: dict[str, Any] = {"free_m": {"front": sector(s, lambda d: abs(d) <= 30),
                                          "left": sector(s, lambda d: 60 <= d <= 120),
                                          "right": sector(s, lambda d: -120 <= d <= -60),
                                          "back": sector(s, lambda d: abs(d) >= 150)}}
        if s["pts"]:
            r, d = min(s["pts"])
            out["nearest"] = {"m": round(r, 2), "deg": round(d)}
        return out

    async def nav_to(place: str, timeout: float) -> dict:
        x, y = spots[place]
        gid = f"go:{place}:{next(br.ids)}"
        t0 = time.monotonic()
        # Новая цель вытесняет активную (Nav2 preemption); вызов со старой целью получит её action_result.
        state["active"] = {"id": gid, "place": place, "t0": t0}
        fut = await br.goal(gid, {"pose": {"header": {"frame_id": "map"},
                                           "pose": {"position": {"x": x, "y": y, "z": 0.0},
                                                    "orientation": {"w": 1.0}}}})
        try:
            m = await asyncio.wait_for(asyncio.shield(fut), timeout)
            st = STATUS.get(m.get("status"), str(m.get("status")))
        except asyncio.TimeoutError:
            return {"ok": False, "place": place, "status": "running", "s": round(time.monotonic() - t0, 1)}
        except ConnectionError:
            st = "disconnected"
        if state["active"] and state["active"]["id"] == gid:
            state["active"] = None
        await asyncio.sleep(0.3)   # AMCL успевает выдать позу после остановки
        p = pose()
        res = {"ok": st == "succeeded", "place": place, "status": st, "s": round(time.monotonic() - t0, 1)}
        if p:
            res["err_m"] = round(math.hypot(p["x"] - x, p["y"] - y), 2)
        state["last"] = {"place": place, "status": st, "s": res["s"]}
        return res

    @mcp.tool(structured_output=False, annotations=ToolAnnotations(readOnlyHint=True),
              description="Robot state: pose (map, m/deg), nearest named place, navigation state, "
                          "lidar free space (front/left/right/back, m) and nearest obstacle.")
    async def status() -> str:
        await br.ensure()
        s = await scan()
        out: dict[str, Any] = {"pose": pose() or "unknown", **scan_summary(s)}
        act = state["active"]
        out["nav"] = f"to {act['place']} {time.monotonic() - act['t0']:.0f}s" if act else "idle"
        if state["last"]:
            out["last"] = state["last"]
        return cj(out)

    @mcp.tool(structured_output=False, annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True),
              description="Drive to a named place with the on-board navigator; returns when arrived/failed.")
    async def go_to(place: Place) -> str:  # type: ignore[valid-type]
        if place not in spots:
            return cj({"ok": False, "error": f"unknown place; known: {list(spots)}"})
        return cj(await nav_to(place, a.goal_timeout))

    @mcp.tool(structured_output=False, annotations=ToolAnnotations(destructiveHint=False),
              description="Visit several named places in order (executed on board, one call).")
    async def patrol(places: list[Place]) -> str:  # type: ignore[valid-type]
        legs = []
        for p in places:
            if p not in spots:
                legs.append({"place": p, "status": "unknown place"})
                break
            r = await nav_to(p, a.goal_timeout)
            legs.append({k: r[k] for k in ("place", "status", "s") if k in r})
            if not r["ok"]:
                break
        ok = len(legs) == len(places) and all(leg["status"] == "succeeded" for leg in legs)
        return cj({"ok": ok, "legs": legs})

    @mcp.tool(structured_output=False, annotations=ToolAnnotations(destructiveHint=False),
              description="Stop: cancel the active navigation goal and command zero velocity.")
    async def stop() -> str:
        await br.ensure()
        act = state["active"]
        if act:
            await br.cancel(act["id"])
        await br.zero()
        state["active"] = None
        return cj({"ok": True, "cancelled": act["place"] if act else None})

    @mcp.tool(structured_output=False, annotations=ToolAnnotations(readOnlyHint=True),
              description="What is in front of the camera. Default: compact text summary (nearest object "
                          "in the camera field of view). image=true: also attach one small JPEG frame.")
    async def look(image: bool = False) -> list[TextContent | ImageContent]:
        await br.ensure()
        half = CAM_HFOV_DEG / 2
        t0 = time.monotonic()
        # Лидар и кадр — параллельно: задержка = max(скан ≈0,1 с, первый кадр ленивой камеры ≈0,5–0,7 с).
        frame_task = asyncio.create_task(br.once("/camera/image_raw/compressed", "sensor_msgs/msg/CompressedImage",
                                                 a.frame_timeout, br.compression)) if image else None
        s = await scan()
        out: dict[str, Any] = {"detector": "none"}
        if s:
            v = [(r, d) for r, d in s["pts"] if -half <= d <= half]
            if v:
                r, d = min(v)
                side = "left" if d > 10 else "right" if d < -10 else "center"
                rays = max(1, round(2 * half / s["inc_deg"]))
                out["in_view"] = {"nearest_m": round(r, 2), "side": side,
                                  "occupied_pct": round(100 * len(v) / rays)}   # доля лучей с отражением
            else:
                out["in_view"] = f"clear >{s['rmax']:g} m"
        parts: list[TextContent | ImageContent] = []
        if frame_task:
            m = await frame_task
            if m:
                # JSON: data — base64-строка; CBOR: байтовая строка (кадр по rosbridge ≈13 КБ вместо ≈17 КБ)
                raw = base64.b64decode(m["data"]) if isinstance(m["data"], str) else bytes(m["data"])
                raw, w, h = shrink(raw, a.image_width, a.jpeg_quality)
                out["frame"] = {"w": w, "h": h, "kb": round(len(raw) / 1024, 1),
                                "ms": round((time.monotonic() - t0) * 1000)}
                parts.append(ImageContent(type="image", data=base64.b64encode(raw).decode(), mimeType="image/jpeg"))
            else:
                out["frame"] = "timeout"
        return [TextContent(type="text", text=cj(out)), *parts]

    return mcp


def shrink(raw: bytes, width: int, quality: int) -> tuple[bytes, int | None, int | None]:
    """Уменьшить JPEG до ширины width (если есть Pillow и кадр шире); иначе вернуть как есть."""
    try:
        from PIL import Image
    except ImportError:
        return raw, None, None
    img = Image.open(io.BytesIO(raw))
    if width and img.width > width:
        img = img.resize((width, round(img.height * width / img.width)))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        raw = buf.getvalue()
    return raw, img.width, img.height


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rosbridge", default="ws://127.0.0.1:9090")
    ap.add_argument("--places", help="JSON {имя: [x, y]} в кадре map")
    ap.add_argument("--compression", choices=["auto", "none", "cbor"], default="auto", help="для /scan")
    ap.add_argument("--goal-timeout", type=float, default=150.0)
    ap.add_argument("--sensor-timeout", type=float, default=2.0)
    ap.add_argument("--frame-timeout", type=float, default=3.0)
    ap.add_argument("--image-width", type=int, default=320, help="0 — не уменьшать")
    ap.add_argument("--jpeg-quality", type=int, default=70)
    a = ap.parse_args()
    build(a).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
