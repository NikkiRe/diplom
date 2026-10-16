#!/usr/bin/env python3
"""MCP-клиент для замеров MCP-сервера робота поверх rosbridge: ros-mcp-server (R-generic) или свой (R-compact).

Что делает:
  1. запускает MCP-сервер по stdio (через обёртку, которая пишет PID — для замера RSS/PSS/CPU процесса);
  2. ставит между сервером и rosbridge TCP-прокси со счётчиком байт (TCP-полезная нагрузка: кадры WS +
     HTTP-рукопожатие) и числом соединений;
  3. прогоняет сценарий вызовов (--scenario generic|compact, --only РЕГВЫР) и для каждого пишет: задержку
     вызова, размер ответа MCP (result JSON-RPC), текст для LLM (TextContent), картинки (ImageContent),
     structuredContent, байты rosbridge вверх/вниз, новые WS-соединения, CPU-время сервера, истинную позу
     робота до/после (/ground_truth/pose — отдельным соединением мимо прокси, в счёт не входит).
Токены и ₽ считает rosmcp_analyze.py (на Mac: tiktoken o200k из .venv). Здесь — только mcp и websockets,
чтобы скрипт работал и на «плате» (~/.venv-rosmcp в VM sbc, где есть только зависимости ros-mcp).

  rosmcp_probe.py --scenario generic --server-cmd ".venv-rosmcp/bin/ros-mcp" --rosbridge 127.0.0.1:9090 --out DIR
  rosmcp_probe.py --scenario camera-generic|camera-compact --reps 10 --gap 6 ...   (кадр по запросу, ленивая камера)
  rosmcp_probe.py --scenario compact --server-cmd ".venv-rosmcp/bin/python scripts/sim/robot_mcp_compact.py --rosbridge ws://{proxy}" ...
В --server-cmd подстрока {proxy} заменяется на 127.0.0.1:<порт прокси>.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import platform
import re
import shlex
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import websockets
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

NAV_TYPE = "nav2_msgs/action/NavigateToPose"
TWIST = "geometry_msgs/msg/TwistStamped"
DOCK = (-2.0, -0.5)
EAST = (1.8, 0.5)


# ----------------------------------------------------------------------------------------------- прокси
class CountingProxy:
    """TCP-прокси 127.0.0.1:<port> → rosbridge; считает байты в обе стороны и соединения."""

    def __init__(self, target_host: str, target_port: int):
        self.th, self.tp = target_host, target_port
        self.up = self.down = self.conns = 0
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        self.conns += 1
        try:
            tr, tw = await asyncio.open_connection(self.th, self.tp)
        except OSError:
            w.close()
            return

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter, up: bool) -> None:
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    if up:
                        self.up += len(data)
                    else:
                        self.down += len(data)
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.gather(pipe(r, tw, True), pipe(tr, w, False))

    def snap(self) -> tuple[int, int, int]:
        return self.up, self.down, self.conns


# ------------------------------------------------------------------------------------- ресурсы процесса
def proc_sample(pid: int | None) -> dict:
    """RSS/PSS (КиБ) и CPU-время (с) процесса сервера. Linux — /proc, macOS — ps (без PSS)."""
    if not pid:
        return {}
    out: dict[str, Any] = {}
    if platform.system() == "Linux":
        try:
            for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
                k, _, v = line.partition(":")
                if k in ("Rss", "Pss", "Private_Clean", "Private_Dirty", "Anonymous"):
                    out[k.lower() + "_kib"] = int(v.split()[0])
            st = Path(f"/proc/{pid}/stat").read_text()
            f = st[st.rindex(")") + 2:].split()
            tck = os.sysconf("SC_CLK_TCK")
            out["cpu_s"] = (int(f[11]) + int(f[12])) / tck
            out["threads"] = int(f[17])
        except (OSError, ValueError, IndexError):
            pass
    else:
        import subprocess

        try:
            r = subprocess.run(["ps", "-o", "rss=,time=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
            rss, t = r.stdout.split()
            out["rss_kib"] = int(rss)
            m, s = t.split(":")[-2:]
            h = t.split(":")[0] if t.count(":") == 2 else "0"
            out["cpu_s"] = int(h) * 3600 + int(m) * 60 + float(s)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return out


# --------------------------------------------------------------------------------- истина и сброс стенда
def yaw_of(q: dict) -> float:
    return math.atan2(2 * (q.get("w", 1) * q.get("z", 0) + q.get("x", 0) * q.get("y", 0)),
                      1 - 2 * (q.get("y", 0) ** 2 + q.get("z", 0) ** 2))


async def gt_pose(url: str, timeout: float = 6.0) -> dict | None:
    """Истинная поза (x, y, yaw) из /ground_truth/pose — отдельное соединение, мимо прокси."""
    try:
        async with websockets.connect(url, max_size=1 << 24, ping_interval=None, open_timeout=timeout) as ws:
            await ws.send(json.dumps({"op": "subscribe", "id": "gt", "topic": "/ground_truth/pose",
                                      "type": "geometry_msgs/msg/PoseStamped"}))
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                m = json.loads(await asyncio.wait_for(ws.recv(), end - time.monotonic()))
                if m.get("op") == "publish":
                    p = m["msg"]["pose"]
                    return {"x": round(p["position"]["x"], 3), "y": round(p["position"]["y"], 3),
                            "yaw": round(yaw_of(p["orientation"]), 3)}
    except (OSError, asyncio.TimeoutError, websockets.WebSocketException, KeyError):
        return None
    return None


async def nav_direct(url: str, x: float, y: float, yaw: float = 0.0, timeout: float = 120.0) -> dict:
    """Сброс стенда: navigate_to_pose напрямую (без feedback), мимо прокси."""
    goal = {"pose": {"header": {"frame_id": "map"},
                     "pose": {"position": {"x": x, "y": y, "z": 0.0},
                              "orientation": {"z": math.sin(yaw / 2), "w": math.cos(yaw / 2)}}}}
    t0 = time.monotonic()
    async with websockets.connect(url, max_size=1 << 24, ping_interval=None) as ws:
        await ws.send(json.dumps({"op": "send_action_goal", "id": "reset", "action": "/navigate_to_pose",
                                  "action_type": NAV_TYPE, "args": goal, "feedback": False}))
        while time.monotonic() - t0 < timeout:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if m.get("op") == "action_result":
                return {"status": m.get("status"), "s": round(time.monotonic() - t0, 1)}
    return {"status": None, "s": round(time.monotonic() - t0, 1)}


def dist(a: dict | None, b: dict | None) -> float | None:
    if not a or not b:
        return None
    return round(math.hypot(a["x"] - b["x"], a["y"] - b["y"]), 3)


# ------------------------------------------------------------------------------------------- сценарии
def nav_goal(x: float, y: float) -> dict:
    return {"pose": {"header": {"frame_id": "map"},
                     "pose": {"position": {"x": x, "y": y, "z": 0.0}, "orientation": {"z": 0.0, "w": 1.0}}}}


def scenario_generic(rb_host: str, rb_port: int) -> list[dict]:
    """Вызовы ros-mcp-server 3.1.2: обнаружение, датчики, движение, навигация. {proxy_port} подставляется."""
    S = [
        dict(step="connect_direct", tool="connect_to_robot", args={"ip": rb_host, "port": rb_port}),
        dict(step="connect_proxy", tool="connect_to_robot", args={"ip": "127.0.0.1", "port": "{proxy_port}"}),
        dict(step="get_topics", tool="get_topics", args={}),
        dict(step="get_topic_type_cmd_vel", tool="get_topic_type", args={"topic": "/cmd_vel"}),
        dict(step="msg_details_twiststamped", tool="get_message_details", args={"message_type": TWIST}),
        dict(step="odom", tool="subscribe_once",
             args={"topic": "/odom", "msg_type": "nav_msgs/msg/Odometry", "expects_image": "false"}),
        dict(step="scan", tool="subscribe_once",
             args={"topic": "/scan", "msg_type": "sensor_msgs/msg/LaserScan", "expects_image": "false"}),
        dict(step="amcl_pose", tool="subscribe_once",
             args={"topic": "/amcl_pose", "msg_type": "geometry_msgs/msg/PoseWithCovarianceStamped",
                   "expects_image": "false", "timeout": 3}),
        dict(step="camera_jpeg", tool="subscribe_once",
             args={"topic": "/camera/image_raw/compressed", "msg_type": "sensor_msgs/msg/CompressedImage",
                   "expects_image": "true", "timeout": 5}),
        dict(step="camera_raw", tool="subscribe_once",
             args={"topic": "/camera/image_raw", "msg_type": "sensor_msgs/msg/Image",
                   "expects_image": "true", "timeout": 5}),
        # Пример из описания инструмента: TwistStamped без обёртки twist — так LLM и напишет по образцу.
        dict(step="cmd_vel_example_style", tool="publish_once", gt=True, post_wait=2.0,
             args={"topic": "/cmd_vel", "msg_type": TWIST, "msg": {"linear": {"x": 0.15}}}),
        dict(step="cmd_vel_once", tool="publish_once", gt=True, post_wait=2.0,
             args={"topic": "/cmd_vel", "msg_type": TWIST, "msg": {"twist": {"linear": {"x": 0.15}}}}),
        dict(step="cmd_vel_stop", tool="publish_once", gt=True, post_wait=1.5,
             args={"topic": "/cmd_vel", "msg_type": TWIST, "msg": {"twist": {"linear": {"x": 0.0}}}}),
        dict(step="cmd_vel_durations", tool="publish_for_durations", gt=True, post_wait=1.0,
             args={"topic": "/cmd_vel", "msg_type": TWIST,
                   "messages": [{"twist": {"linear": {"x": 0.2}}}, {"twist": {"linear": {"x": 0.0}}}],
                   "durations": [2.0, 0.5], "rate_hz": 10}),
        dict(step="get_actions", tool="get_actions", args={}),
        dict(step="action_details_nav", tool="get_action_details",
             args={"action": "/navigate_to_pose", "action_type": NAV_TYPE}),
        dict(step="nav_east", tool="send_action_goal", gt=True, target=EAST,
             args={"action_name": "/navigate_to_pose", "action_type": NAV_TYPE, "goal": nav_goal(*EAST),
                   "timeout": 120}),
        dict(step="action_status", tool="get_action_status", args={"action_name": "/navigate_to_pose"}),
        # Без timeout: по умолчанию 5 с, цель на ~20 с. Смотрим, что будет с целью после закрытия соединения.
        dict(step="nav_dock_default_timeout", tool="send_action_goal", gt=True, post_wait=30.0, target=DOCK,
             args={"action_name": "/navigate_to_pose", "action_type": NAV_TYPE, "goal": nav_goal(*DOCK)}),
        dict(step="scan_2s", tool="subscribe_for_duration",
             args={"topic": "/scan", "msg_type": "sensor_msgs/msg/LaserScan", "duration": 2,
                   "expects_image": "false"}),
        dict(step="res_metadata_all", resource="ros-mcp://ros-metadata/all"),
    ]
    return S


def scenario_compact() -> list[dict]:
    """Вызовы собственного тонкого адаптера (robot_mcp_compact.py)."""
    return [
        dict(step="status", tool="status", args={}),
        dict(step="look_summary", tool="look", args={}),
        dict(step="look_image", tool="look", args={"image": True}),
        dict(step="go_east", tool="go_to", gt=True, target=EAST, args={"place": "east"}),
        dict(step="status_after", tool="status", args={}),
        dict(step="patrol", tool="patrol", gt=True, target=DOCK, args={"places": ["north", "dock"]}),
        dict(step="go_unknown", tool="go_to", args={"place": "kitchen"}),
        dict(step="stop", tool="stop", args={}),
        dict(step="status_end", tool="status", args={}),
    ]


def scenario_camera(kind: str, reps: int, gap: float) -> list[dict]:
    """Кадр по запросу при ленивой камере: reps попыток с паузой gap (камера успевает «уснуть»)."""
    if kind == "generic":
        head = [dict(step="connect_proxy", tool="connect_to_robot", args={"ip": "127.0.0.1", "port": "{proxy_port}"})]
        one = dict(tool="subscribe_once", args={"topic": "/camera/image_raw/compressed",
                                                "msg_type": "sensor_msgs/msg/CompressedImage",
                                                "expects_image": "true", "timeout": 5})
    else:
        head = []
        one = dict(tool="look", args={"image": True})
    return head + [dict(one, step=f"frame_{i:02d}", post_wait=gap) for i in range(reps)]


# ----------------------------------------------------------------------------------------- разбор ответа
def jpeg_size(b: bytes) -> tuple[int, int] | None:
    """Ширина и высота JPEG по маркеру SOF (без Pillow)."""
    i = 2
    while i + 9 < len(b):
        if b[i] != 0xFF:
            i += 1
            continue
        marker = b[i + 1]
        seg = int.from_bytes(b[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(b[i + 7:i + 9], "big"), int.from_bytes(b[i + 5:i + 7], "big")
        i += 2 + seg
    return None


def dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def digest(res: Any, step_dir: Path, idx: int, name: str) -> dict:
    d = res.model_dump(mode="json", by_alias=True, exclude_none=True)
    rec: dict[str, Any] = {"mcp_bytes": len(dump(d).encode()), "is_error": bool(d.get("isError", False))}
    texts, images = [], []
    for k, c in enumerate(d.get("content") or d.get("contents") or []):
        if c.get("type") == "text" or "text" in c:
            texts.append(c.get("text", ""))
        elif c.get("type") == "image" or "blob" in c:
            raw = base64.b64decode(c.get("data") or c.get("blob") or "")
            wh = jpeg_size(raw)
            p = step_dir / f"{idx:02d}_{name}_{k}.jpg"
            p.write_bytes(raw)
            images.append({"mime": c.get("mimeType"), "b64_chars": len(c.get("data") or c.get("blob") or ""),
                           "bytes": len(raw), "w": wh[0] if wh else None, "h": wh[1] if wh else None,
                           "file": p.name})
    text = "\n".join(texts)
    (step_dir / f"{idx:02d}_{name}.txt").write_text(text)
    rec.update(text_bytes=len(text.encode()), text_file=f"{idx:02d}_{name}.txt", images=images,
               structured_bytes=len(dump(d["structuredContent"]).encode()) if "structuredContent" in d else 0,
               text_head=text[:300])
    return rec


# ------------------------------------------------------------------------------------------------ прогон
async def run(a: argparse.Namespace) -> dict:
    out = Path(a.out).resolve()
    step_dir = out / "steps"
    step_dir.mkdir(parents=True, exist_ok=True)
    srv_cwd = out / "server_cwd"      # ros-mcp пишет кадры в ./camera/ — каталог должен быть записываемым
    srv_cwd.mkdir(exist_ok=True)
    rb_host, rb_port = a.rosbridge.rsplit(":", 1)
    rb_url = f"ws://{a.rosbridge}"
    proxy = CountingProxy(rb_host, int(rb_port))
    await proxy.start()
    pidfile = out / "server.pid"
    if pidfile.exists():
        pidfile.unlink()
    cmd = shlex.split(a.server_cmd.replace("{proxy}", f"127.0.0.1:{proxy.port}"))
    params = StdioServerParameters(
        command="/bin/sh", args=["-c", f'echo $$ > "{pidfile}"; exec "$@"', "sh", *cmd],
        cwd=str(srv_cwd), env={**os.environ, "PYTHONUNBUFFERED": "1"})
    if a.scenario == "generic":
        steps = scenario_generic(rb_host, int(rb_port))
    elif a.scenario == "compact":
        steps = scenario_compact()
    else:
        steps = scenario_camera(a.scenario.split("-")[1], a.reps, a.gap)
    if a.only:
        steps = [s for s in steps if re.search(a.only, s["step"])]
    report: dict[str, Any] = {"scenario": a.scenario, "server_cmd": cmd, "rosbridge": a.rosbridge,
                              "host": platform.node(), "system": platform.platform(),
                              "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "steps": []}
    if a.reset:
        report["reset_before"] = {"gt": await gt_pose(rb_url)}
        if (dist(report["reset_before"]["gt"], {"x": DOCK[0], "y": DOCK[1]}) or 9) > 0.3:
            report["reset_before"]["nav"] = await nav_direct(rb_url, *DOCK)
    errlog = open(out / "server_stderr.log", "w")
    t_spawn = time.monotonic()
    async with stdio_client(params, errlog=errlog) as (r, w):
        async with ClientSession(r, w) as session:
            init = await session.initialize()
            report["init_ms"] = round((time.monotonic() - t_spawn) * 1000, 1)
            report["server_info"] = init.serverInfo.model_dump(mode="json")
            report["protocol"] = init.protocolVersion
            pid = int(pidfile.read_text().strip()) if pidfile.exists() else None
            report["server_pid"] = pid
            t0 = time.monotonic()
            tools = await session.list_tools()
            report["list_tools_ms"] = round((time.monotonic() - t0) * 1000, 1)
            td = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools.tools]
            (out / "tools.json").write_text(json.dumps(td, ensure_ascii=False, indent=1))
            report["n_tools"] = len(td)
            report["tools_list_bytes"] = len(dump({"tools": td}).encode())
            for kind, fn in (("resources", session.list_resources), ("prompts", session.list_prompts)):
                try:
                    lst = await fn()
                    items = getattr(lst, kind)
                    report[f"n_{kind}"] = len(items)
                    report[f"{kind}_list_bytes"] = len(dump(lst.model_dump(mode="json", exclude_none=True)).encode())
                except Exception as e:  # noqa: BLE001 — у компактного сервера их может не быть
                    report[f"n_{kind}"] = 0
                    report[f"{kind}_err"] = str(e)[:200]
            report["proc_idle"] = proc_sample(pid)
            for i, s in enumerate(steps):
                s = json.loads(json.dumps(s).replace('"{proxy_port}"', str(proxy.port)))
                rec: dict[str, Any] = {"step": s["step"], "tool": s.get("tool") or "resources/read",
                                       "args": s.get("args", {"uri": s.get("resource")})}
                if s.get("gt"):
                    rec["gt_before"] = await gt_pose(rb_url)
                cpu0 = proc_sample(pid).get("cpu_s")
                up0, dn0, c0 = proxy.snap()
                t0 = time.monotonic()
                try:
                    if "resource" in s:
                        res = await session.read_resource(s["resource"])
                    else:
                        res = await session.call_tool(s["tool"], s["args"],
                                                      read_timeout_seconds=timedelta(seconds=a.call_timeout))
                    rec["ms"] = round((time.monotonic() - t0) * 1000, 1)
                    rec.update(digest(res, step_dir, i, s["step"]))
                except Exception as e:  # noqa: BLE001 — фиксируем и идём дальше
                    rec["ms"] = round((time.monotonic() - t0) * 1000, 1)
                    rec["exception"] = f"{type(e).__name__}: {e}"[:500]
                await asyncio.sleep(0.3)   # дать прокси дочитать хвост закрывающегося соединения
                up1, dn1, c1 = proxy.snap()
                rec.update(rb_up=up1 - up0, rb_down=dn1 - dn0, rb_conns=c1 - c0)
                ps = proc_sample(pid)
                if cpu0 is not None and ps.get("cpu_s") is not None:
                    rec["server_cpu_ms"] = round((ps["cpu_s"] - cpu0) * 1000, 1)
                rec["server_rss_kib"] = ps.get("rss_kib")
                rec["server_pss_kib"] = ps.get("pss_kib")
                if s.get("post_wait"):
                    await asyncio.sleep(s["post_wait"])
                    up2, dn2, c2 = proxy.snap()
                    rec.update(rb_up_after_wait=up2 - up1, rb_down_after_wait=dn2 - dn1)
                if s.get("gt"):
                    rec["gt_after"] = await gt_pose(rb_url)
                    rec["moved_m"] = dist(rec["gt_before"], rec["gt_after"])
                    if s.get("target"):
                        rec["err_to_target_m"] = dist(rec["gt_after"], {"x": s["target"][0], "y": s["target"][1]})
                report["steps"].append(rec)
                print(f"[probe] {s['step']:28s} {rec['ms']:9.1f} ms  mcp={rec.get('mcp_bytes')} "
                      f"text={rec.get('text_bytes')} img={len(rec.get('images') or [])} "
                      f"rb↑{rec['rb_up']} ↓{rec['rb_down']} conns={rec['rb_conns']} "
                      f"err={rec.get('is_error')} {rec.get('exception', '')[:80]}", file=sys.stderr, flush=True)
            report["proc_end"] = proc_sample(pid)
    errlog.close()
    report["rb_total"] = {"up": proxy.up, "down": proxy.down, "conns": proxy.conns}
    if a.reset:
        g = await gt_pose(rb_url)
        report["reset_after"] = {"gt": g}
        if (dist(g, {"x": DOCK[0], "y": DOCK[1]}) or 9) > 0.3:
            report["reset_after"]["nav"] = await nav_direct(rb_url, *DOCK)
    (out / "probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", choices=["generic", "compact", "camera-generic", "camera-compact"],
                    default="generic")
    ap.add_argument("--reps", type=int, default=10, help="camera-*: число кадров")
    ap.add_argument("--gap", type=float, default=6.0, help="camera-*: пауза между кадрами, с")
    ap.add_argument("--server-cmd", required=True)
    ap.add_argument("--rosbridge", default="127.0.0.1:9090", help="host:port rosbridge (за прокси)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", help="регвыр по именам шагов")
    ap.add_argument("--call-timeout", type=float, default=180.0)
    ap.add_argument("--no-reset", dest="reset", action="store_false", help="не возвращать робота на dock")
    a = ap.parse_args()
    rep = asyncio.run(run(a))
    print(json.dumps({k: v for k, v in rep.items() if k != "steps"}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
