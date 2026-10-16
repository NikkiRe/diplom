"""Настоящие EdgeCtx + hw-mcp + mockllm на изменяемой копии sysfs-фикстуры.

Проверяет протокольную интеграцию и гейт, а не качество модели или работу
конкретного будущего датчика. Бинарники собираются из исходников во временный
каталог; исходная фикстура и сохранённые результаты прогонов не меняются.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.mockllm import RunningServer, ServerConfig  # noqa: E402

GO = shutil.which("go")
# Это имена уже существующей синтетической фикстуры, не выбранная периферия ВКР.
FIXTURE_DEVICE = "bme280"
FIXTURE_INPUT = "sys/bus/iio/devices/iio:device0/in_temp_input"


def request(url: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as res:
        return json.loads(res.read())


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)


@unittest.skipUnless(GO, "нет go — настоящие компоненты не собрать")
class HwMcpEdgectxTest(unittest.TestCase):
    def test_discovery_subscription_context_and_gate(self):
        with tempfile.TemporaryDirectory(prefix="hw-edgectx-") as tmp:
            self.run_scenario(Path(tmp))

    def run_scenario(self, tmp: Path):
        binaries = {}
        for component, target in (("agent", "edgectx"), ("hwmcp", "hw-mcp")):
            binary = tmp / target
            result = subprocess.run([GO, "build", "-trimpath", "-o", str(binary), f"./cmd/{target}"],
                                    cwd=ROOT / component, env={**os.environ, "CGO_ENABLED": "0"},
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr[-3000:])
            binaries[target] = binary

        fixture = tmp / "sysfs"
        shutil.copytree(ROOT / "hwmcp/testdata/fixture_root", fixture, symlinks=True)
        sensor = fixture / FIXTURE_INPUT
        sensor.write_text("22510\n")
        ready, trace = tmp / "hw.url", tmp / "edge.jsonl"
        procs, logs = [], []
        bodies = []
        try:
            hw_log = open(tmp / "hw.log", "w")
            logs.append(hw_log)
            procs.append(subprocess.Popen(
                [str(binaries["hw-mcp"]), "-root", str(fixture), "-transport", "http", "-clock", "virtual",
                 "-ready-file", str(ready), "-trace", str(tmp / "hw.jsonl"), "-sample-period", "1s"],
                cwd=tmp, stdout=hw_log, stderr=subprocess.STDOUT))
            deadline = time.monotonic() + 15
            while not ready.exists():
                self.assertIsNone(procs[-1].poll(), (tmp / "hw.log").read_text())
                self.assertLess(time.monotonic(), deadline, "hw-mcp ready timeout")
                time.sleep(0.02)
            hw_mcp = ready.read_text().strip()
            hw_url = hw_mcp.rsplit("/mcp", 1)[0]

            with RunningServer(ServerConfig(port=0)) as mock:
                original_chat = mock.app.chat

                def chat(body, headers, seq):
                    bodies.append(body)
                    return original_chat(body, headers, seq)

                mock.app.chat = chat
                cfg = yaml.safe_load((ROOT / "agent/configs/example.yaml").read_text())
                cfg["clock"].update(start_t=0, settle_timeout_s=10)
                cfg["llm"].update(base_url=mock.url + "/v1", api_key_env="", retries=0)
                cfg["llm"].pop("extra", None)
                cfg["mcp"] = [{"name": "hw", "transport": "http", "url": hw_mcp,
                               "subscribe": ["*/state/*"], "inventory_uri": "hw://inventory"}]
                cfg["policy"].update(wake="gate", history="window", window_turns=2, local_tools=[])
                cfg["policy"]["gate"].update(
                    debounce_s=2, cooldown_s=0, coalesce_s=2, default="ignore", rules_enabled=False,
                    signals=[{"device": FIXTURE_DEVICE, "property": "temperature", "kind": "threshold",
                              "above": 30, "hysteresis": 1}])
                cfg["userctx"].update(db=str(tmp / "history.db"), reset=True, profile=[])
                cfg_path = tmp / "edge.yaml"
                cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True))
                edge_url = f"http://127.0.0.1:{free_port()}"
                edge_log = open(tmp / "edge.log", "w")
                logs.append(edge_log)
                procs.append(subprocess.Popen(
                    [str(binaries["edgectx"]), "-config", str(cfg_path), "-listen", edge_url.removeprefix("http://"),
                     "-trace", str(trace), "-run-id", "hw-e2e"],
                    cwd=tmp, stdout=edge_log, stderr=subprocess.STDOUT))
                deadline = time.monotonic() + 15
                while True:
                    try:
                        health = request(edge_url + "/v1/health")
                        break
                    except OSError:
                        self.assertIsNone(procs[-1].poll(), (tmp / "edge.log").read_text())
                        self.assertLess(time.monotonic(), deadline, "EdgeCtx ready timeout")
                        time.sleep(0.02)
                self.assertGreater(health["servers"][0]["subscribed"], 0)
                self.assertEqual(health["servers"][0]["server_name"], "hw-mcp")

                def advance(t: float, min_sensors: int = 0):
                    body = {"to_t": t, "timeout_ms": 5000}
                    if min_sensors:
                        body["min_inputs"] = {"sensor": min_sensors}
                    result = request(edge_url + "/v1/clock/advance", body)
                    self.assertTrue(result["idle"], result)
                    self.assertFalse(result.get("missing_inputs"), result)

                # Ниже порога: физическое чтение меняется, L1 обновляется,
                # но debounce + threshold не создают вызов модели.
                advance(10)
                sensor.write_text("25000\n")
                request(hw_url + "/control/clock", {"t": 10})
                advance(14, min_sensors=1)
                self.assertEqual(len(bodies), 0, "insignificant reading woke LLM")
                reply = request(edge_url + "/v1/user", {"id": "inspect", "text": "Покажи текущее состояние"})
                self.assertTrue(reply["ok"], reply)
                first = json.dumps(bodies[0], ensure_ascii=False)
                self.assertIn(FIXTURE_DEVICE + ".temperature=25", first)
                self.assertIn(FIXTURE_DEVICE, first)  # discovered inventory in the stable prefix

                # Значимое показание проходит debounce и coalesce и будит модель.
                advance(20)
                sensor.write_text("35000\n")
                request(hw_url + "/control/clock", {"t": 20})
                advance(24, min_sensors=2)
                self.assertEqual(len(bodies), 2, "significant reading must cause exactly one background request")
                second = json.dumps(bodies[-1], ensure_ascii=False)
                self.assertIn(FIXTURE_DEVICE + ".temperature: 25→35", second)
                metrics = request(edge_url + "/v1/metrics")
                self.assertGreaterEqual(metrics["mcp"]["updates"], 2)
                self.assertEqual(metrics.get("errors", {}), {})
                self.assertEqual(metrics["wakes"]["gate"], 1)
                self.assertGreater(metrics["suppress"]["below_threshold"], 0)
                self.assertGreater(metrics["hw"]["log_events"], 0)
                # Закрытие дописывает буфер трассы перед её чтением.
                stop(procs[-1])
                records = [json.loads(line) for line in trace.read_text().splitlines()]
                self.assertFalse([r for r in records if r["type"] == "error"])
                self.assertTrue(any(r["type"] == "input" and r.get("value") == 35 for r in records))
                self.assertTrue(any(r["type"] == "suppress" and r.get("reason") == "below_threshold" for r in records))
        finally:
            for proc in reversed(procs):
                stop(proc)
            for log in logs:
                log.close()


if __name__ == "__main__":
    unittest.main()
