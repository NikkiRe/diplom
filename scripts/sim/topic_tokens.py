#!/usr/bin/env python3
"""Сколько стоит в токенах LLM одно сообщение каждой темы робота, если отдавать его «как есть».

Для каждой темы берётся одно сообщение через rosbridge (JSON, как его получает ros-mcp-server) и считается:
  json_bytes / json_tokens    — {"msg": <сообщение>} в компактном JSON: ровно так subscribe_once ros-mcp-server
                                отдаёт неизображения в TextContent;
  rounded_tokens              — то же с float, округлёнными до 3 знаков (дешёвая нормализация без потери смысла
                                для LLM; IMPLEMENTATION_SPEC §0.5 «округление до точности датчика»);
  для изображений ещё b64_tokens (base64 данных, если клиент вставит его в текст) и vision-токены кадра по
  правилам OpenAI (developers.openai.com/api/docs/guides/images-vision, проверено 07.10.2026):
    патчи 32×32: ceil(w/32)·ceil(h/32)·k, k = 1,62 (gpt-4.1-mini), 2,46 (gpt-4.1-nano), 1,2 (gpt-5-mini),
                 1,5 (gpt-5-nano);
    плитки 512: база + плитка·N (без увеличения малых кадров): gpt-4o/4.1 85+170N, gpt-5 70+140N,
                 gpt-4o-mini 2833+5667N; detail=low — только база.
Токенизатор — o200k_base через bench/mockllm/tokens.py (локальный словарь, как у mock-llm). ₽ — по
bench/costmodel/prices.py (замороженный прайс RouterAI), за однократную подачу на вход без кэша.

  .venv/bin/python scripts/sim/topic_tokens.py --url ws://127.0.0.1:9090 --out DIR
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import sys
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bench.costmodel.prices import default_pricebook  # noqa: E402
from bench.mockllm.tokens import load_tokenizer  # noqa: E402

TOPICS = [
    ("/odom", "nav_msgs/msg/Odometry"),
    ("/amcl_pose", "geometry_msgs/msg/PoseWithCovarianceStamped"),
    ("/ground_truth/pose", "geometry_msgs/msg/PoseStamped"),
    ("/scan", "sensor_msgs/msg/LaserScan"),
    ("/imu", "sensor_msgs/msg/Imu"),
    ("/tf", "tf2_msgs/msg/TFMessage"),
    ("/joint_states", "sensor_msgs/msg/JointState"),
    ("/clock", "rosgraph_msgs/msg/Clock"),
    ("/camera/camera_info", "sensor_msgs/msg/CameraInfo"),
    ("/camera/image_raw/compressed", "sensor_msgs/msg/CompressedImage"),
    ("/camera/image_raw", "sensor_msgs/msg/Image"),
    ("/map", "nav_msgs/msg/OccupancyGrid"),
]
MODELS = ["openai/gpt-4.1-mini", "openai/gpt-5-mini", "openai/gpt-4.1-nano", "openai/gpt-5-nano"]
PATCH_K = {"gpt-4.1-mini": 1.62, "gpt-4.1-nano": 2.46, "gpt-5-mini": 1.2, "gpt-5-nano": 1.5}
TILE = {"gpt-4o/4.1": (85, 170), "gpt-5": (70, 140), "gpt-4o-mini": (2833, 5667)}


def vision_tokens(w: int, h: int) -> dict:
    out = {f"{m} patch": math.ceil(round(math.ceil(w / 32) * math.ceil(h / 32) * k, 6)) for m, k in PATCH_K.items()}
    s = min(1.0, 2048 / max(w, h))
    w2, h2 = w * s, h * s
    if min(w2, h2) > 768:
        s2 = 768 / min(w2, h2)
        w2, h2 = w2 * s2, h2 * s2
    n = math.ceil(w2 / 512) * math.ceil(h2 / 512)
    for m, (base, tile) in TILE.items():
        out[f"{m} high"] = base + tile * n
        out[f"{m} low"] = base
    return out


def rounded(o, nd: int = 3):
    if isinstance(o, float):
        return round(o, nd)
    if isinstance(o, list):
        return [rounded(x, nd) for x in o]
    if isinstance(o, dict):
        return {k: rounded(v, nd) for k, v in o.items()}
    return o


def jpeg_wh(b: bytes) -> tuple[int, int] | None:
    i = 2
    while i + 9 < len(b):
        if b[i] != 0xFF:
            i += 1
            continue
        seg = int.from_bytes(b[i + 2:i + 4], "big")
        if b[i + 1] in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(b[i + 7:i + 9], "big"), int.from_bytes(b[i + 5:i + 7], "big")
        i += 2 + seg
    return None


async def grab(url: str, topic: str, typ: str, timeout: float) -> tuple[dict | None, float]:
    t0 = time.monotonic()
    async with websockets.connect(url, max_size=1 << 26, ping_interval=None, compression=None) as ws:
        await ws.send(json.dumps({"op": "subscribe", "id": "tt", "topic": topic, "type": typ, "queue_length": 1}))
        end = t0 + timeout
        while time.monotonic() < end:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), end - time.monotonic()))
            except asyncio.TimeoutError:
                break
            if m.get("op") == "publish":
                return m["msg"], time.monotonic() - t0
    return None, time.monotonic() - t0


async def run(a) -> dict:
    tok = load_tokenizer("o200k_base")
    pb = default_pricebook()
    rub = {m: pb.price(m).input for m in MODELS}
    rows = []
    for topic, typ in TOPICS:
        msg, dt = await grab(a.url, topic, typ, a.timeout)
        row: dict = {"topic": topic, "type": typ, "first_msg_s": round(dt, 2)}
        if msg is None:
            row["note"] = "нет сообщения за таймаут"
            rows.append(row)
            continue
        text = json.dumps({"msg": msg}, separators=(",", ":"), ensure_ascii=False)
        row["json_bytes"] = len(text.encode())
        row["json_tokens"] = tok.count(text)
        row["rounded_tokens"] = tok.count(json.dumps({"msg": rounded(msg)}, separators=(",", ":"), ensure_ascii=False))
        row["rub_per_msg"] = {m: round(row["json_tokens"] * p, 5) for m, p in rub.items()}
        if isinstance(msg.get("data"), str) and ("format" in msg or "encoding" in msg):
            b64 = msg["data"]
            row["b64_chars"] = len(b64)
            row["b64_tokens"] = tok.count(b64)
            raw = base64.b64decode(b64)
            row["data_bytes"] = len(raw)
            wh = jpeg_wh(raw) if "format" in msg else (msg.get("width"), msg.get("height"))
            if wh and wh[0]:
                row["w"], row["h"] = wh
                row["vision_tokens"] = vision_tokens(*wh)
                if "format" in msg:
                    row["vision_tokens_320x240"] = vision_tokens(320, 240)
            meta = {k: v for k, v in msg.items() if k != "data"}
            row["meta_tokens"] = tok.count(json.dumps({"msg": meta}, separators=(",", ":")))
        if topic == "/map":
            row["map_wh"] = [msg["info"]["width"], msg["info"]["height"]]
        rows.append(row)
        print(f"[tokens] {topic:32s} {row.get('json_bytes', 0):>9} B {row.get('json_tokens', 0):>8} tok "
              f"(rounded {row.get('rounded_tokens', 0)})", file=sys.stderr, flush=True)
    rep = {"url": a.url, "tokenizer": tok.name, "rub_per_input_token": rub, "price_source": pb.provenance(),
           "date": time.strftime("%Y-%m-%dT%H:%M:%S"), "topics": rows}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "topic_tokens.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1))
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="ws://127.0.0.1:9090")
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=float, default=6.0)
    a = ap.parse_args()
    asyncio.run(run(a))
    return 0


if __name__ == "__main__":
    sys.exit(main())
