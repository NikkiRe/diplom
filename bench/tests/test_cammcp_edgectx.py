"""Настоящие EdgeCtx + cam-mcp + mockllm: агент подключает камеру без изменений в своём коде.

Проверяет контракт (инвентарь cam://inventory в L0, подписка "*/state/*", новое состояние в _meta
уведомления, пробуждение гейтом по motion, трасса принята монитором), а не качество модели. Бинарники
собираются из исходников во временный каталог; источник кадров — встроенная синтетическая сцена cam-mcp
в виртуальном времени.

    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q bench/tests/test_cammcp_edgectx.py
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
from monitor.check_trace import check  # noqa: E402

GO = shutil.which("go")


def request(url: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as res:
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


def wait_file(path: Path, proc: subprocess.Popen, log: Path) -> str:
    deadline = time.monotonic() + 15
    while not path.exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            raise AssertionError(log.read_text())
        time.sleep(0.02)
    return path.read_text().strip()


@unittest.skipUnless(GO, "нет go — настоящие компоненты не собрать")
class CamMcpEdgectxTest(unittest.TestCase):
    def test_inventory_subscription_and_gate(self):
        with tempfile.TemporaryDirectory(prefix="cam-edgectx-") as tmp:
            self.run_scenario(Path(tmp))

    def run_scenario(self, tmp: Path):
        binaries = {}
        for component, target in (("agent", "edgectx"), ("cammcp", "cam-mcp")):
            binary = tmp / target
            result = subprocess.run([GO, "build", "-trimpath", "-o", str(binary), f"./cmd/{target}"],
                                    cwd=ROOT / component, env={**os.environ, "CGO_ENABLED": "0"},
                                    capture_output=True, text=True, timeout=180)
            self.assertEqual(result.returncode, 0, result.stderr[-3000:])
            binaries[target] = binary

        procs, logs, bodies = [], [], []
        try:
            cam_log = open(tmp / "cam.log", "w")
            logs.append(cam_log)
            procs.append(subprocess.Popen(
                [str(binaries["cam-mcp"]), "-transport", "http", "-clock", "virtual", "-location", "hall",
                 "-ready-file", str(tmp / "cam.url"), "-trace", str(tmp / "cam.jsonl")],
                cwd=tmp, stdout=cam_log, stderr=subprocess.STDOUT))
            cam_mcp = wait_file(tmp / "cam.url", procs[-1], tmp / "cam.log")
            cam_url = cam_mcp.rsplit("/mcp", 1)[0]

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
                cfg["mcp"] = [{"name": "cam", "transport": "http", "url": cam_mcp, "subscribe": ["*/state/*"]}]
                cfg["policy"].update(wake="gate", history="window", window_turns=2, local_tools=[])
                cfg["policy"]["gate"].update(
                    debounce_s=1, cooldown_s=0, coalesce_s=1, default="ignore", rules_enabled=False,
                    signals=[{"device": "cam*", "property": "motion", "kind": "change"}])
                cfg["userctx"].update(db=str(tmp / "history.db"), reset=True, profile=[])
                cfg_path = tmp / "edge.yaml"
                cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True))
                edge_url = f"http://127.0.0.1:{free_port()}"
                edge_log = open(tmp / "edge.log", "w")
                logs.append(edge_log)
                trace = tmp / "edge.jsonl"
                procs.append(subprocess.Popen(
                    [str(binaries["edgectx"]), "-config", str(cfg_path), "-listen", edge_url.removeprefix("http://"),
                     "-trace", str(trace), "-run-id", "cam-e2e"],
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
                server = health["servers"][0]
                self.assertEqual(server["server_name"], "cam-mcp")
                self.assertEqual(server["subscribed"], 1)
                self.assertEqual(server["inventory"], "cam://inventory")

                def advance(t: float, min_sensors: int = 0):
                    request(cam_url + "/control/clock", {"t": t})
                    body = {"to_t": t, "timeout_ms": 10000}
                    if min_sensors:
                        body["min_inputs"] = {"sensor": min_sensors}
                    result = request(edge_url + "/v1/clock/advance", body)
                    self.assertTrue(result["idle"], result)
                    self.assertFalse(result.get("missing_inputs"), result)

                # Демонстрационная сцена: до 15 с в кадре никого — ни уведомлений, ни вызовов модели.
                advance(12)
                self.assertEqual(len(bodies), 0)
                # Человек входит в кадр около 15,5 с: одно уведомление (motion, area_pct, zone) будит LLM.
                advance(22, min_sensors=1)
                advance(24)
                self.assertEqual(len(bodies), 1, "motion start must wake the model once")
                prompt = json.dumps(bodies[0], ensure_ascii=False)
                self.assertIn("cam1 (hall): area_pct [%], motion [bool], zone", prompt)  # L0 из cam://inventory
                self.assertIn("cam1.motion", prompt)
                # Конец эпизода — второе уведомление; вне эпизодов кадры не порождают уведомлений.
                advance(60, min_sensors=4)
                metrics = request(edge_url + "/v1/metrics")
                self.assertEqual(metrics["mcp"]["updates"], 2)
                self.assertEqual(metrics.get("errors", {}), {})
                stop(procs[-1])
                lines = trace.read_text().splitlines()
                records = [json.loads(line) for line in lines]
                self.assertFalse([r for r in records if r["type"] == "error"])
                motion = [r for r in records if r["type"] == "input" and r.get("property") == "motion"]
                self.assertEqual([(r["device"], r["value"]) for r in motion], [("cam1", True), ("cam1", False)])
                # Начало эпизода — вход sensor, который гейт пропустил в пробуждение, а оно — в запрос к LLM.
                woke = [r for r in records if r["type"] == "wake" and motion[0]["id"] in r["input_ids"]]
                self.assertEqual([(r["reason"], r["suppressed"]) for r in woke], [("gate", False)])
                self.assertTrue(any(r["type"] == "llm_request" and r["wake_id"] == woke[0]["wake_id"] for r in records))
                report = check(trace=lines)
                self.assertTrue(report.ok, report.to_dict(5)["violations"])
        finally:
            for proc in reversed(procs):
                stop(proc)
            for log in logs:
                log.close()


if __name__ == "__main__":
    unittest.main()
