#!/usr/bin/env python3
"""Пауза / запуск мира стенда через rosbridge (/world/default/control; стенд с start_sim.sh --world-control).

  .venv/bin/python robotmcp/tools/world_pause.py pause|run [--url ws://127.0.0.1:9090]
"""
import argparse
import asyncio
import json

import websockets


async def main(pause: bool, url: str) -> None:
    async with websockets.connect(url, max_size=1 << 24) as ws:
        await ws.send(json.dumps({"op": "call_service", "id": "p", "service": "/world/default/control",
                                  "args": {"world_control": {"pause": pause}}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if m.get("op") == "service_response":
                print(m.get("result"), m.get("values"))
                return


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["pause", "run"])
    ap.add_argument("--url", default="ws://127.0.0.1:9090")
    a = ap.parse_args()
    asyncio.run(main(a.action == "pause", a.url))
