"""Настоящий hw-mcp на дереве bench.sysfsim: бэкенд линий files, виртуальные часы у обоих, сценарий dense.

hw-mcp собирается из hwmcp/ во временный каталог и читает корень тем же кодом, что на плате. Проверяется:
инвентарь совпадает с таблицей устройств симулятора; значения hw://state/<устройство> равны показаниям из
журнала истины на момент отсчёта hw-mcp точно (допуск 1e-9 — только на разбор чисел JSON); отказы видны как
null с ошибкой (исчезновение DHT11, read_error BME280) или как 85 °C (сброс DS18B20); реле, включённое через
hw-mcp, охлаждает кухню в симуляторе (замкнутый контур).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import AnyUrl

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.sysfsim.sensors import DEVICES, SENSORS  # noqa: E402

GO = shutil.which("go")
ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
DECLARED = ROOT / "bench/sysfsim/configs/declared.sim.json"


def wait_file(path: Path, proc: subprocess.Popen, log: Path) -> str:
    deadline = time.monotonic() + 20
    while not path.exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            raise AssertionError(f"процесс не запустился: {log.read_text()}")
        time.sleep(0.02)
    return path.read_text().strip()


def stop(proc: subprocess.Popen) -> int:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)
    return proc.returncode


class Truth:
    """Журнал истины симулятора: показание датчика и уровень линии на момент t (последняя запись не позже t)."""

    def __init__(self, file):
        self.file = file
        self.series: dict[tuple[str, str], tuple[list[float], list]] = defaultdict(lambda: ([], []))
        self.records = []

    def update(self) -> None:
        """Дочитать записи, добавленные с прошлого раза (симулятор пишет строку целиком до ответа на POST)."""
        for line in self.file:
            r = json.loads(line)
            self.records.append(r)
            if r["type"] == "value":
                self._add((r["device"], r["property"]), r["t"], r["reading"])
            elif r["type"] in ("gpio", "actuator"):
                self._add((r["device"], "state"), r["t"], r["value"])
            elif r["type"] == "device":
                self._add((r["device"], "present"), r["t"], r["present"])

    def _add(self, key, t, value):
        times, values = self.series[key]
        times.append(t)
        values.append(value)

    def at(self, device: str, prop: str, t: float, default=None):
        times, values = self.series[device, prop]
        i = bisect_right(times, t)
        return values[i - 1] if i else default


@unittest.skipUnless(GO, "нет go — hw-mcp не собрать")
class SysfsimHwMcpTest(unittest.TestCase):
    def test_hw_mcp_sees_the_simulated_board(self):
        with tempfile.TemporaryDirectory(prefix="sysfsim-hw-") as name:
            tmp = Path(name)
            hw_bin = tmp / "hw-mcp"
            built = subprocess.run([GO, "build", "-trimpath", "-o", str(hw_bin), "./cmd/hw-mcp"], cwd=ROOT / "hwmcp",
                                   env={**os.environ, "CGO_ENABLED": "0"}, capture_output=True, text=True, timeout=300)
            self.assertEqual(built.returncode, 0, built.stderr[-3000:])
            root = tmp / "root"
            subprocess.run([sys.executable, "-m", "bench.sysfsim", "init", "--root", str(root)], cwd=ROOT, env=ENV,
                           check=True)
            procs, logs = [], []
            try:
                logs.append(open(tmp / "sim.log", "w"))
                procs.append(subprocess.Popen(
                    [sys.executable, "-m", "bench.sysfsim", "run", "--root", str(root), "--scenario", "dense",
                     "--clock", "virtual", "--truth", str(tmp / "truth.jsonl"), "--ready-file", str(tmp / "sim.json")],
                    cwd=ROOT, env=ENV, stdout=logs[-1], stderr=subprocess.STDOUT))
                sim_url = json.loads(wait_file(tmp / "sim.json", procs[-1], tmp / "sim.log"))["url"]
                logs.append(open(tmp / "hw.log", "w"))
                procs.append(subprocess.Popen(
                    [str(hw_bin), "-root", str(root), "-declared", str(DECLARED), "-transport", "http",
                     "-clock", "virtual", "-ready-file", str(tmp / "hw.url"), "-trace", str(tmp / "hw.jsonl")],
                    cwd=tmp, stdout=logs[-1], stderr=subprocess.STDOUT))
                mcp_url = wait_file(tmp / "hw.url", procs[-1], tmp / "hw.log")
                with open(tmp / "truth.jsonl") as log:
                    asyncio.run(self.drive(sim_url, mcp_url, Truth(log)))
                self.assertEqual(stop(procs[0]), 0, (tmp / "sim.log").read_text())
            finally:
                for proc in reversed(procs):
                    stop(proc)
                for log in logs:
                    log.close()

    async def drive(self, sim_url: str, mcp_url: str, truth: Truth) -> None:
        hw_url = mcp_url.rsplit("/mcp", 1)[0]
        async with httpx.AsyncClient(timeout=10) as http, streamable_http_client(mcp_url) as (rd, wr, _), \
                ClientSession(rd, wr) as session:
            await session.initialize()
            inventory = (await session.read_resource(AnyUrl("hw://inventory/text"))).contents[0].text
            self.assertEqual({line.split(":")[0].split(" @")[0] for line in inventory.splitlines()},
                             {d.name for d in DEVICES})

            async def state(device: str) -> dict:
                body = (await session.read_resource(AnyUrl(f"hw://state/{device}"))).contents[0].text
                return json.loads(body)["properties"]

            seen, fan_on_at, kitchen_at_fan = defaultdict(set), None, None
            for t in range(1, 901):
                (await http.post(sim_url + "/clock", json={"t": t})).raise_for_status()
                (await http.post(hw_url + "/control/clock", json={"t": t})).raise_for_status()
                if t % 5 == 0 or t in (125, 250, 768):
                    truth.update()
                    for d in DEVICES:
                        for prop, view in (await state(d.name)).items():
                            self.check(truth, d.name, prop, view, t, seen)
                kitchen = (await state("kitchen_bme280"))["temperature"]["value"]
                if fan_on_at is None and kitchen is not None and kitchen >= 35:   # «агент» включает вентилятор
                    result = await session.call_tool("write_property", {"device": "relay_fan", "property": "state",
                                                                         "value": True})
                    self.assertFalse(result.isError, result.content[0].text)
                    fan_on_at, kitchen_at_fan = t, kitchen
            # отказы, видимые через hw-mcp
            self.assertIn(("bathroom_dht11", "humidity", 250), seen["null"])
            self.assertIn(("kitchen_bme280", "temperature", 768), seen["null"])
            self.assertIn(("boiler_pipe_ds18b20", "temperature", 125), seen["85"])
            # замкнутый контур: реле включено через hw-mcp, кухня остывает и в журнале истины, и в hw-mcp
            self.assertIsNotNone(fan_on_at)
            truth.update()
            self.assertEqual(truth.at("relay_fan", "state", fan_on_at), True)
            self.assertLess(fan_on_at, 480)
            kitchen_late = (await state("kitchen_bme280"))["temperature"]["value"]
            self.assertLess(truth.at("kitchen_bme280", "temperature", 650), kitchen_at_fan - 5)
            self.assertLess(kitchen_late, kitchen_at_fan - 5)
            question = next(r for r in truth.records if r["type"] == "question" and r["id"] == "q5")
            self.assertTrue(question["value"])

    def check(self, truth: Truth, device: str, prop: str, view: dict, now: int, seen: dict) -> None:
        """Значение hw-mcp = показание симулятора на момент отсчёта hw-mcp (ts)."""
        ts, value = view["ts"], view["value"]
        if device in {d.name for d in SENSORS} and not truth.at(device, "present", ts, True):
            self.assertIsNone(value, (device, prop, ts))
        expected = truth.at(device, prop, ts)
        if value is None:
            self.assertIn("error", view, (device, prop))
            self.assertTrue(expected is None or not truth.at(device, "present", ts, True), (device, prop, ts))
            seen["null"].add((device, prop, now))
        elif isinstance(value, bool):
            self.assertEqual(value, bool(expected), (device, prop, ts))
        else:
            self.assertAlmostEqual(value, expected, delta=1e-9, msg=(device, prop, ts))
            if value == 85.0:
                seen["85"].add((device, prop, now))


if __name__ == "__main__":
    unittest.main()
