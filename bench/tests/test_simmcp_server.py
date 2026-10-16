"""sim-mcp через транспорт: отдельный процесс (CLI) на свободном порту + клиент mcp SDK (streamable HTTP).

Проверяется: инициализация (ревизия 2025-11-25, подписки), список инструментов generic/explicit, вызовы и
структурные отказы, ресурсы, notifications/resources/updated после POST /control/clock (только подписанным
или всем при --notify-all), оценка answer/команд через /control/report, журнал JSONL и отчёт при остановке.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from pydantic import AnyUrl

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bench.daygen.generator import DayConfig, generate_day, write_day  # noqa: E402
from bench.simmcp import catalog  # noqa: E402

PY = sys.executable
START_TIMEOUT_S = 20


def body(r: types.CallToolResult) -> dict:
    """Результат инструмента — канонический JSON в первом текстовом блоке."""
    return json.loads(r.content[0].text)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerProc:
    """Запуск `python -m bench.simmcp` и остановка по PID (SIGTERM, затем SIGKILL)."""

    def __init__(self, day: Path, tmp: Path, *extra: str):
        self.port = free_port()
        self.journal = tmp / f"journal_{self.port}.jsonl"
        self.report = tmp / f"report_{self.port}.json"
        self.log = open(tmp / f"server_{self.port}.log", "w")
        self.proc = subprocess.Popen(
            [PY, "-m", "bench.simmcp", "--day", str(day), "--port", str(self.port), "--journal", str(self.journal),
             "--report", str(self.report), "--run-id", "test-run", *extra],
            cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT, env={**os.environ, "PYTHONUNBUFFERED": "1"})
        self.base = f"http://127.0.0.1:{self.port}"
        deadline = time.monotonic() + START_TIMEOUT_S
        while True:
            try:
                httpx.get(self.base + "/healthz", timeout=1).raise_for_status()
                break
            except (httpx.HTTPError, OSError):
                if self.proc.poll() is not None or time.monotonic() > deadline:
                    self.stop()
                    raise RuntimeError("sim-mcp не запустился: " +
                                       (tmp / f"server_{self.port}.log").read_text()) from None
                time.sleep(0.1)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self.log.close()


class Client:
    """Сессия mcp SDK + сбор уведомлений resources/updated."""

    def __init__(self, url: str):
        self.url = url
        self.updated: list[str] = []
        self.changed: dict[str, set[str]] = {}

    async def handler(self, msg) -> None:
        if isinstance(msg, types.ServerNotification) and isinstance(msg.root, types.ResourceUpdatedNotification):
            uri = str(msg.root.params.uri)
            self.updated.append(uri)
            meta = msg.root.params.meta
            extra = (meta.model_extra if meta is not None else None) or {}
            self.changed.setdefault(uri, set()).update(extra.get("hw/changed", []))

    async def wait_for(self, pred, timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            await asyncio.sleep(0.05)
        return pred()


def make_day(tmp: Path, seed: int = 21) -> tuple[Path, dict, list[dict]]:
    h, ev = generate_day(DayConfig(seed=seed, profile="H", source="synthetic", question_share=0.4, trap_share=0.2))
    path = tmp / f"day_{seed}.jsonl"
    write_day(path, h, ev)
    return path, h, ev


class GenericServerTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.day, cls.h, cls.ev = make_day(cls.tmp)
        cls.srv = ServerProc(cls.day, cls.tmp)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls._tmp.cleanup()

    async def test_scenario(self):
        """Один сценарий по возрастанию часов (часы сервера монотонны, поэтому шаги не разбиты на тесты)."""
        srv, h, ev = self.srv, self.h, self.ev
        cl = Client(srv.base + "/mcp")
        async with httpx.AsyncClient(base_url=srv.base, timeout=10) as http, \
                streamable_http_client(cl.url) as (rd, wr, _), \
                ClientSession(rd, wr, message_handler=cl.handler) as cs:
            init = await cs.initialize()
            self.assertEqual(init.protocolVersion, "2025-11-25")
            self.assertTrue(init.capabilities.resources.subscribe)
            self.assertEqual(init.serverInfo.name, "sim-mcp")

            # --- инструменты и ресурсы
            tools = (await cs.list_tools()).tools
            self.assertEqual([t.name for t in tools], [t["name"] for t in catalog.generic_tools()[0]])
            uris = {str(r.uri) for r in (await cs.list_resources()).resources}
            self.assertTrue({"hw://inventory", "hw://state/door_front"} <= uris)
            self.assertFalse({"hw://state", "hw://events"} & uris)          # их нет у hw-mcp (флаг --extra-resources)
            self.assertEqual(len(uris), 1 + len(h["devices"]))
            inv = json.loads((await cs.read_resource(AnyUrl("hw://inventory"))).contents[0].text)
            self.assertEqual(len(inv["devices"]), len(h["devices"]))
            self.assertEqual(inv["devices"][0]["properties"][next(iter(inv["devices"][0]["properties"]))].keys()
                             >= {"type", "writable"}, True)

            r = await cs.call_tool("list_devices", {})
            self.assertFalse(r.isError)
            self.assertEqual(len(body(r)["devices"]), len(h["devices"]))
            r = await cs.call_tool("read_property", {"device": "door_front", "property": "open"})
            self.assertEqual(body(r)["value"], h["initial_state"]["door_front"]["open"])
            self.assertIsNone(r.structuredContent)                       # как hw-mcp: только текст
            r = await cs.call_tool("write_property", {"device": "light_kitchen", "property": "brightness",
                                                      "value": 500})
            self.assertTrue(r.isError)
            self.assertEqual((body(r)["denied"], body(r)["reason"]), (True, "invalid_args"))
            self.assertIn("out_of_range", body(r)["detail"])
            r = await cs.call_tool("write_property", {"device": "light_kitchen", "property": "on"})
            self.assertEqual(body(r)["reason"], "invalid_args")
            r = await cs.call_tool("read_property", {"device": "garage_light", "property": "on"})
            self.assertEqual((r.isError, body(r)["error"]), (True, "unknown_device"))   # чтение: error, не denied
            self.assertNotIn("denied", body(r))
            r = await cs.call_tool("write_property", {"device": "light_kitchen", "property": "on", "value": True},
                                   meta={"edgectx/cmd_id": "cmd-7"})
            self.assertEqual((body(r)["ok"], body(r)["cmd_id"]), (True, "cmd-7"))    # ключ из _meta, как у hw-mcp
            r = await cs.call_tool("teleport", {})
            self.assertEqual((r.isError, body(r)["error"]), (True, "unknown_tool"))
            r = await cs.call_tool("board_health", {})
            self.assertEqual(sorted(body(r)), ["cpu_temp_c", "load", "memory", "now", "sensor_errors", "sensor_faults",
                                               "throttled", "uptime_s"])     # поля hw-mcp: hub/health.go

            # --- подписка и уведомления после /control/clock
            sensors = [e for e in ev if e["kind"] == "sensor"]
            dev_x = sensors[0]["device"]
            t_adv = sensors[0]["t"]
            others = {e["device"] for e in sensors if e["t"] <= t_adv + 3600} - {dev_x}
            t_adv = max(e["t"] for e in sensors if e["t"] <= t_adv + 3600)
            await cs.subscribe_resource(AnyUrl(f"hw://state/{dev_x}"))
            cl.updated.clear()
            resp = (await http.post("/control/clock", json={"t": t_adv})).json()
            self.assertGreater(resp["applied"], 0)
            self.assertTrue(await cl.wait_for(lambda: f"hw://state/{dev_x}" in cl.updated))
            await asyncio.sleep(0.2)
            self.assertEqual(set(cl.updated), {f"hw://state/{dev_x}"})            # только подписанное
            self.assertTrue(others, "в окне должны меняться и другие устройства")
            last = [e for e in sensors if e["device"] == dev_x and e["t"] <= t_adv][-1]
            st = json.loads((await cs.read_resource(AnyUrl(f"hw://state/{dev_x}"))).contents[0].text)
            self.assertEqual(set(st), {"device", "now", "properties"})                # формат hw-mcp (DeviceState)
            self.assertEqual(st["properties"][last["property"]]["value"], last["value"])
            self.assertEqual(st["properties"][last["property"]]["ts"], last["t"])
            self.assertIn(last["property"], cl.changed[f"hw://state/{dev_x}"])       # _meta["hw/changed"]
            self.assertEqual((await http.get("/control/clock")).json()["t"], t_adv)
            r = await http.post("/control/clock", json={"t": t_adv - 10})
            self.assertEqual(r.status_code, 409)
            self.assertEqual((await http.post("/control/clock", json={"x": 1})).status_code, 400)

            # запись агента тоже меняет ресурс — уведомление подписчику
            await cs.subscribe_resource(AnyUrl("hw://state/plug_living_room"))
            cl.updated.clear()
            r = await cs.call_tool("write_property", {"device": "plug_living_room", "property": "on", "value": True})
            self.assertTrue(body(r)["ok"])
            self.assertTrue(await cl.wait_for(lambda: "hw://state/plug_living_room" in cl.updated))
            await cs.call_tool("write_property", {"device": "plug_living_room", "property": "on", "value": False})

            # --- вопрос и команда: эталонные вызовы засчитываются
            q = next(e for e in ev if e["kind"] == "question" and e["t"] > t_adv)
            u = next(e for e in ev if e["kind"] == "user_cmd" and e["t"] > q["t"] and e["class"] == "control")
            await http.post("/control/clock", json={"t": q["t"]})
            self.assertEqual((await http.post("/control/input", json={"id": q["id"]})).status_code, 200)
            self.assertEqual((await http.post("/control/input", json={"id": "zz"})).status_code, 404)
            c = q["expect"]["calls"][0]
            r = await cs.call_tool(c["name"], c["args"])
            self.assertEqual(body(r), {"ok": True, "recorded": True})
            await http.post("/control/clock", json={"t": u["t"]})
            await http.post("/control/input", json={"id": u["id"]})
            for c in u["expect"]["calls"]:
                r = await cs.call_tool(c["name"], c["args"])
                self.assertFalse(r.isError, body(r))
            rep = (await http.get("/control/report")).json()
            rows = {x["id"]: x for x in rep["inputs"]}
            self.assertTrue(rows[q["id"]]["ok"], rows[q["id"]])
            self.assertTrue(rows[u["id"]]["ok"], rows[u["id"]])
            self.assertEqual(rep["catalog"], "generic")
            state = (await http.get("/control/state")).json()["state"]
            for g in u["expect"]["goal"]:
                if g["op"] == "eq":
                    self.assertEqual(state[g["device"]][g["property"]], g["value"])
            fin = (await http.post("/control/finish")).json()
            self.assertIn("summary", fin)

        # --- остановка: журнал и отчёт
        srv.stop()
        lines = [json.loads(x) for x in srv.journal.read_text(encoding="utf-8").splitlines()]
        types_seen = {x["type"] for x in lines}
        self.assertTrue({"run_start", "tool_call", "tool_result", "clock", "sensor", "input", "answer",
                         "subscribe", "report", "run_end"} <= types_seen, types_seen)
        for x in lines:
            self.assertTrue({"ts_wall", "ts_clock", "type", "run_id"} <= set(x))
            self.assertEqual(x["run_id"], "test-run")
        self.assertEqual(lines[-1]["type"], "run_end")
        report = json.loads(srv.report.read_text(encoding="utf-8"))
        self.assertEqual(report["day"]["seed"], h["seed"])


class ExplicitNotifyAllServerTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.day, cls.h, cls.ev = make_day(cls.tmp, seed=22)
        cls.srv = ServerProc(cls.day, cls.tmp, "--catalog", "explicit", "--notify-all", "--extra-resources")

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls._tmp.cleanup()

    async def test_explicit_tools_and_notify_all(self):
        cl = Client(self.srv.base + "/mcp")
        async with httpx.AsyncClient(base_url=self.srv.base, timeout=10) as http, \
                streamable_http_client(cl.url) as (rd, wr, _), \
                ClientSession(rd, wr, message_handler=cl.handler) as cs:
            await cs.initialize()
            names = [t.name for t in (await cs.list_tools()).tools]
            self.assertEqual(names, [t["name"] for t in catalog.explicit_tools(self.h["devices"])[0]])
            r = await cs.call_tool("set_light_bedroom_color", {"value": "green"})
            self.assertEqual(body(r)["value"], "green")
            r = await cs.call_tool("set_light_bedroom_color", {"value": "cyan"})
            self.assertEqual(body(r)["reason"], "invalid_args")       # enum в схеме инструмента
            r = await cs.call_tool("read_light_bedroom_color", {})
            self.assertEqual(body(r)["value"], "green")
            cl.updated.clear()
            t = next(e["t"] for e in self.ev if e["kind"] == "sensor")
            await http.post("/control/clock", json={"t": t})
            # без подписки: при --notify-all (и --extra-resources) приходят state/<dev>, state и events
            self.assertTrue(await cl.wait_for(lambda: "hw://events" in cl.updated))
            self.assertIn("hw://state", cl.updated)
            r = await cs.call_tool("brew_coffee_kitchen", {})
            self.assertTrue(body(r)["ok"])
            self.assertEqual(body(r)["done_in_s"], 300)
            ev = json.loads((await cs.read_resource(AnyUrl("hw://events"))).contents[0].text)["events"]
            self.assertEqual((ev[-1]["device"], ev[-1]["source"]), ("coffee_kitchen", "tool"))  # и записи агента
            # кофе готов через 300 с виртуального времени — изменение от таймера тоже рассылается
            cl.updated.clear()
            await http.post("/control/clock", json={"t": t + 400})
            self.assertTrue(await cl.wait_for(lambda: "hw://state/coffee_kitchen" in cl.updated))
            r = await cs.call_tool("read_coffee_kitchen_brewing", {})
            self.assertFalse(body(r)["value"])


if __name__ == "__main__":
    unittest.main()
