"""Мост драйвера к nanobot 0.3.5 (запускается Python'ом nanobot рядом со шлюзом — на Mac или в ВМ).

Штатные пути без человека:
* реплики пользователя — WebSocket-канал nanobot (channels.websocket, тот же, что у WebUI): соединение
  `?client_id=user&token=…`, кадр `{"content": текст}`, ответ — кадры `{"event": "message", …}` до
  `{"event": "turn_end"}` (по соединению — своя сессия chat_id, на весь день одна);
* события датчиков — local triggers: во втором соединении (`client_id=sensors`, своя сессия) командой
  `/trigger sensors` создаётся триггер, события ставятся в очередь `LocalTriggerStore.enqueue` — ровно то,
  что делает CLI `nanobot trigger <id> "…"` (cli/commands.py), без запуска Python на каждое событие
  (режим --trigger-via cli запускает CLI на каждое событие: так делал бы скрипт-источник на плате).
  Шлюз забирает очередь опросом раз в 0,5 с (triggers/local_runner.py).

Протокол с драйвером — JSON-строки: stdin {"op": "user", "id", "text", "timeout"} | {"op": "event", "text"} |
{"op": "quit"}; stdout {"ev": "ready", "trigger_id", …} | {"ev": "reply", "id", "ok", "text", "ms", "frames"} |
{"ev": "event", "ok", "delivery_id"|"error"} | {"ev": "error", …}. Все кадры WebSocket пишутся в --frames.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import threading
import time


def out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class Conn:
    def __init__(self, name: str, url: str, frames_f):
        self.name, self.url, self.frames_f = name, url, frames_f
        self.ws = None
        self.chat_id: str | None = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.count = 0

    async def open(self) -> None:
        import websockets  # noqa: PLC0415 — зависимость nanobot

        self.ws = await websockets.connect(self.url, max_size=None, ping_interval=20, ping_timeout=60)
        first = json.loads(await self.ws.recv())
        self._log(first)
        self.chat_id = first.get("chat_id")
        self.task = asyncio.create_task(self._reader())

    def _log(self, frame: dict) -> None:
        self.count += 1
        self.frames_f.write(json.dumps({"ts_wall": time.time(), "conn": self.name, "frame": frame},
                                       ensure_ascii=False) + "\n")
        self.frames_f.flush()

    async def _reader(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    frame = json.loads(raw)
                except ValueError:
                    frame = {"raw": raw if isinstance(raw, str) else repr(raw)}
                self._log(frame)
                await self.queue.put(frame)
        except Exception as e:  # noqa: BLE001
            await self.queue.put({"event": "__closed__", "error": repr(e)})

    async def send(self, text: str) -> None:
        await self.ws.send(json.dumps({"content": text}, ensure_ascii=False))

    def drain(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()


def frame_text(frame: dict) -> str:
    for k in ("text", "content", "message"):
        v = frame.get(k)
        if isinstance(v, str):
            return v
    return ""


async def turn(conn: Conn, text: str, timeout: float) -> dict:
    """Отправить сообщение и собрать кадры до turn_end (или таймаута)."""
    conn.drain()
    t0 = time.time()
    await conn.send(text)
    msgs: list[str] = []
    frames = 0
    deadline = t0 + timeout
    while True:
        rest = deadline - time.time()
        if rest <= 0:
            return {"ok": False, "timeout": True, "text": "\n".join(msgs), "frames": frames,
                    "ms": round((time.time() - t0) * 1000, 2)}
        try:
            fr = await asyncio.wait_for(conn.queue.get(), timeout=rest)
        except asyncio.TimeoutError:
            continue
        frames += 1
        ev = fr.get("event")
        if ev == "message":
            msgs.append(frame_text(fr))
        elif ev == "turn_end":
            return {"ok": True, "text": msgs[-1] if msgs else "", "all": msgs, "frames": frames,
                    "ms": round((time.time() - t0) * 1000, 2), "usage": fr.get("usage")}
        elif ev == "__closed__":
            return {"ok": False, "error": fr.get("error"), "text": "\n".join(msgs), "frames": frames}


async def main_async(a) -> int:
    frames_f = open(a.frames, "a", encoding="utf-8")
    sep = "&" if "?" in a.ws_url else "?"
    user = Conn("user", f"{a.ws_url}{sep}client_id=user&token={a.token}", frames_f)
    sensors = Conn("sensors", f"{a.ws_url}{sep}client_id=sensors&token={a.token}", frames_f)
    await user.open()
    trigger_id = None
    store = None
    if not a.no_trigger:
        await sensors.open()
        r = await turn(sensors, f"/trigger {a.trigger_name}", 30)
        m = re.search(r"ID:\s*(\S+)", "\n".join(r.get("all") or [r.get("text") or ""]))
        if not m:
            out({"ev": "error", "error": "trigger not created", "reply": r})
            return 2
        trigger_id = m.group(1)
        from nanobot.triggers.local_store import LocalTriggerStore  # noqa: PLC0415

        store = LocalTriggerStore(a.workspace and __import__("pathlib").Path(a.workspace))
    out({"ev": "ready", "trigger_id": trigger_id, "user_chat": user.chat_id, "sensors_chat": sensors.chat_id})

    loop = asyncio.get_running_loop()
    cmds: asyncio.Queue = asyncio.Queue()

    def reader() -> None:
        for line in sys.stdin:
            line = line.strip()
            if line:
                loop.call_soon_threadsafe(cmds.put_nowait, line)
        loop.call_soon_threadsafe(cmds.put_nowait, '{"op": "quit"}')

    threading.Thread(target=reader, daemon=True).start()
    while True:
        try:
            cmd = json.loads(await cmds.get())
        except ValueError:
            continue
        op = cmd.get("op")
        if op == "quit":
            break
        if op == "user":
            r = await turn(user, cmd.get("text", ""), float(cmd.get("timeout") or 30))
            out({"ev": "reply", "id": cmd.get("id"), **{k: v for k, v in r.items() if k != "all"}})
        elif op == "event":
            if trigger_id is None:
                out({"ev": "event", "ok": False, "error": "no trigger"})
                continue
            try:
                if a.trigger_via == "cli":
                    p = subprocess.run([a.nanobot, "trigger", trigger_id, cmd.get("text", ""), "--workspace",
                                        a.workspace], capture_output=True, timeout=60)
                    out({"ev": "event", "ok": p.returncode == 0, "rc": p.returncode})
                else:
                    d = store.enqueue(trigger_id, cmd.get("text", ""))
                    out({"ev": "event", "ok": True, "delivery_id": d.id})
            except Exception as e:  # noqa: BLE001
                out({"ev": "event", "ok": False, "error": repr(e)})
    for c in (user, sensors):
        if c.ws is not None:
            await c.ws.close()
    out({"ev": "bye", "frames": {"user": user.count, "sensors": sensors.count}})
    frames_f.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws-url", required=True, help="ws://127.0.0.1:PORT/")
    ap.add_argument("--token", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--frames", required=True)
    ap.add_argument("--trigger-name", default="sensors")
    ap.add_argument("--trigger-via", default="store", choices=("store", "cli"))
    ap.add_argument("--nanobot", default="nanobot", help="CLI nanobot для --trigger-via cli")
    ap.add_argument("--no-trigger", action="store_true")
    a = ap.parse_args(argv)
    return asyncio.run(main_async(a))


if __name__ == "__main__":
    sys.exit(main())
