"""Патчи neurobot к MCP-серверам лаборатории (накладываются на присланные версии).

ros_mcp_server: пропатченный сервер отвечает по streamable HTTP, пишет в /cmd_vel_agent, ничего не печатает в stdout,
автостоп срабатывает и при исключении в цикле толчка (finally), и при SIGTERM (atexit); robot_stop агента шлёт ещё и
стоп в /neurobot/stop_now (решение 1а: safety_gate ставит ноль сразу), а автостоп и выход — только нули;
robot_get_status отдаёт состояние safety_gate (state, reasons, estop), robot_nudge — отказ blocked/estop с причиной
вместо ok, если safety_gate не пускает или не пропустил толчок; против подставного rosbridge с подставным
safety_state.
robot_vision_mcp_proxy: без патча — дубли инструментов движения (FastMCP оставляет первую регистрацию, мс уходят
воркеру как секунды, N1), doctor видит это по tools/list; с патчем — дублей нет, длительность и скорость ограничены,
HTTP на 127.0.0.1, стоп воркеру при SIGTERM; против подставного воркера 8766.
Серверы лаборатории запускаются копией во временном каталоге. Нужны Python ≥ 3.10, mcp, websocket-client,
websockets; иначе тест пропускается."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "neurobot" / "deploy"
LAB = ROOT / "third_party/neurochip/ros_mcp_server/ros_mcp_server-master/ros_mcp_server.py"
PATCH = DEPLOY / "mcp/ros_mcp_server.patch"
LAB_PROXY = ROOT / "third_party/neurochip/robotic_platform/robotic_platform/robot_vision_mcp_proxy.py"
PROXY_PATCH = DEPLOY / "mcp/robot_vision_mcp_proxy.patch"
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))

from neurobot_deploy import doctor  # noqa: E402
from neurobot_deploy.config import Config  # noqa: E402
from neurobot_deploy.probes import McpClient, ProbeError, probe_mcp  # noqa: E402

try:
    import mcp  # noqa: F401
    import websocket  # noqa: F401
    from websockets.asyncio.server import serve
    from websockets.exceptions import ConnectionClosed
    DEPS = sys.version_info >= (3, 10)
except ImportError:
    DEPS = False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


SAFETY = "/neurobot/safety_state"
STOP = "/neurobot/stop_now"


class FakeRosbridge:
    """Принимает WebSocket rosbridge v2 и пишет (номер соединения, сообщение); соединения из reject — HTTP 500;
    drop_after — закрыть соединение после стольких публикаций в нём (обрыв, рестарт rosbridge).
    Подписчику /neurobot/safety_state шлёт состояние 10 Гц: safety — шаблон, req — последний толчок агента, если он
    дошёл до safety_gate (delivered), out — он же, если safety_gate его пропускает (passes), иначе ноль; silent —
    состояния не шлёт (safety_gate не запущен). На вызов сервиса (/rosapi/topics) отвечает списком тем."""

    def __init__(self):
        self.messages, self.conns, self.reject, self.drop_after = [], 0, set(), 0
        self.safety = {"state": "slow", "reasons": ["lidar_blind: перед в слепом секторе лидара"],
                       "limits": {"v_max": 0.15, "v_rev": 0.05, "w_max": 0.8}, "estop": {"engaged": False}}
        self.passes, self.delivered, self.silent = True, True, False
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        async def state(ws):
            while True:
                last = ([m["msg"] for _, m in self.messages if m.get("op") == "publish"
                         and m.get("topic") == "/cmd_vel_agent"] or [None])[-1]
                req = [last["linear"]["x"], last["angular"]["z"]] if last and self.delivered else [0.0, 0.0]
                out = req if self.passes else [0.0, 0.0]
                if not self.silent:
                    await ws.send(json.dumps({"op": "publish", "topic": SAFETY,
                                              "msg": {"data": json.dumps(dict(self.safety, req=req, out=out))}}))
                await asyncio.sleep(0.1)

        async def handler(ws):
            tasks = []
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    self.messages.append((ws.neurobot_no, msg))
                    if msg.get("op") == "subscribe" and msg.get("topic") == SAFETY:
                        tasks.append(asyncio.ensure_future(state(ws)))
                    elif msg.get("op") == "call_service":
                        await ws.send(json.dumps({"op": "service_response", "id": msg["id"], "result": True,
                                                  "values": {"topics": ["/cmd_vel_agent", SAFETY]}}))
                    elif msg.get("op") == "publish" and self.drop_after and sum(
                            1 for n, m in self.messages if n == ws.neurobot_no and m.get("op") == "publish") \
                            >= self.drop_after:
                        await ws.close()
            except ConnectionClosed:
                pass
            finally:
                for t in tasks:
                    t.cancel()

        def process_request(conn, request):
            self.conns += 1
            conn.neurobot_no = self.conns
            if self.conns in self.reject:
                return conn.respond(HTTPStatus.INTERNAL_SERVER_ERROR, "reject\n")
            return None

        async def main():
            self.server = await serve(handler, "127.0.0.1", 0, process_request=process_request)
            self.port = self.server.sockets[0].getsockname()[1]
            ready.set()
            await self.server.serve_forever()

        self.thread = threading.Thread(target=lambda: self._run(main()), daemon=True)
        self.thread.start()
        ready.wait(5)

    def _run(self, coro):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(coro)
        except asyncio.CancelledError:
            pass

    def close(self):
        self.loop.call_soon_threadsafe(self.server.close)
        self.thread.join(5)

    def twists(self, after_conn: int = 0) -> list:
        return [(n, m["msg"]) for n, m in self.messages
                if m.get("op") == "publish" and m.get("topic") == "/cmd_vel_agent" and n > after_conn]

    def publishes(self, since: int = 0) -> list:
        """(тема, сообщение) публикаций после since-го сообщения сервера."""
        return [(m["topic"], m["msg"]) for _, m in self.messages[since:] if m.get("op") == "publish"]


def is_zero(t: dict) -> bool:
    return not any(t["linear"].values()) and not any(t["angular"].values())


@unittest.skipUnless(DEPS, "нужны Python ≥ 3.10, mcp, websocket-client, websockets")
class PatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        shutil.copy(LAB, self.tmp)
        r = subprocess.run(["patch", "-p1", "--quiet", "-i", str(PATCH)], cwd=self.tmp, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.bridge = FakeRosbridge()
        self.addCleanup(self.bridge.close)
        self.port = free_port()
        cfg = {"server": {"host": "127.0.0.1", "port": self.port},
               "rosbridge": {"url": "ws://127.0.0.1:{}".format(self.bridge.port), "timeout_s": 3.0},
               "topics": {"cmd_vel": {"name": "/cmd_vel_agent", "type": "geometry_msgs/msg/Twist", "access": "write"},
                          "safety_state": {"name": SAFETY, "type": "std_msgs/msg/String", "access": "read"},
                          "stop_now": {"name": STOP, "type": "std_msgs/msg/String", "access": "write"}}}
        with open(os.path.join(self.tmp, "cfg.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        self.proc = subprocess.Popen([sys.executable, "ros_mcp_server.py", "--config", "cfg.json"], cwd=self.tmp,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.addCleanup(self._kill)
        self.url = "http://127.0.0.1:{}/mcp".format(self.port)
        deadline = time.monotonic() + 20
        while True:
            try:
                self.tools = probe_mcp(self.url, 2.0).tools
                break
            except ProbeError:
                if time.monotonic() > deadline or self.proc.poll() is not None:
                    self.fail("сервер не поднялся: " + self._kill()[1].decode(errors="replace")[-2000:])
                time.sleep(0.2)

    def _kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        return self.proc.communicate(timeout=10)

    def call(self, name: str, args: dict) -> dict:
        c = McpClient(self.url, 20.0)
        try:
            c.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                 "clientInfo": {"name": "test", "version": "1"}})
            c.notify("notifications/initialized")
            return c.rpc("tools/call", {"name": name, "arguments": args})
        finally:
            c.close()

    def test_patch_is_small(self):
        # 17 строк транспорта и автостопа, чтение safety_state для статуса и итога толчка, постоянный издатель, стоп
        # агента в safety_gate
        lines = PATCH.read_text(encoding="utf-8").splitlines()
        added = [ln for ln in lines if ln.startswith("+") and not ln.startswith("+++")]
        self.assertLessEqual(len(added), 140)
        self.assertEqual(self.tools, ["robot_get_status", "robot_stop", "robot_nudge"])

    def result(self, name: str, args: dict) -> dict:
        res = self.call(name, args)
        return json.loads("".join(c.get("text", "") for c in res.get("content", [])))

    def test_status_carries_safety_state(self):
        self.bridge.safety = dict(self.bridge.safety, state="estop", reasons=["shock: удар 9.9 м/с²"],
                                  estop={"engaged": True})
        st = self.result("robot_get_status", {})
        self.assertEqual(st["safety"]["state"], "estop")
        self.assertEqual((st["safety"]["reasons"], st["safety"]["estop"]), (["shock: удар 9.9 м/с²"], True))
        self.assertFalse(st["motion_allowed"])
        subs = [m for _, m in self.bridge.messages if m.get("op") == "subscribe"]
        self.assertEqual([m["topic"] for m in subs], [SAFETY])  # только чтение состояния, другие темы — нет
        self.bridge.safety = {"state": "estop", "reasons": ["estop: кнопка"], "limits": {}}  # без поля estop
        self.assertTrue(self.result("robot_get_status", {})["safety"]["estop"])
        self.bridge.silent = True  # safety_gate не запущен: состояния нет — двигаться нельзя
        st = self.result("robot_get_status", {})
        self.assertEqual((st["safety"]["state"], st["motion_allowed"]), ("unknown", False))

    def test_nudge_refused_by_latch_or_limit_without_motion(self):
        for state, limits, status in (("estop", {"v_max": 0.0, "v_rev": 0.0, "w_max": 0.0}, "estop"),
                                      ("blocked", {"v_max": 0.0, "v_rev": 0.05, "w_max": 0.8}, "blocked")):
            with self.subTest(state=state):
                self.bridge.safety = dict(self.bridge.safety, state=state, limits=limits,
                                          reasons=["lidar_nodata: нет данных лидара перед роботом"])
                before = len(self.bridge.twists())
                res = self.result("robot_nudge", {"direction": "forward", "duration_ms": 300, "speed": 0.1})
                self.assertEqual((res["ok"], res["status"]), (False, status))
                self.assertIn("lidar_nodata", res["reasons"][0])
                self.assertEqual(len(self.bridge.twists()), before)  # ни одного толчка
        res = self.result("robot_nudge", {"direction": "turn_left", "duration_ms": 200, "speed": 0.3})
        self.assertTrue(res["ok"], res)  # поворот при блоке вперёд разрешён

    def test_nudge_cut_by_gate_is_reported(self):
        self.bridge.passes = False  # толчок дошёл до safety_gate, а его выход остался нулём (срезал по пути)
        res = self.result("robot_nudge", {"direction": "forward", "duration_ms": 300, "speed": 0.1})
        self.assertEqual((res["ok"], res["status"]), (False, "blocked"))
        self.assertEqual(res["error"], "safety_gate не пропустил толчок")
        self.assertIn("lidar_blind", res["reasons"][0])
        self.bridge.passes = True
        res = self.result("robot_nudge", {"direction": "forward", "duration_ms": 300, "speed": 0.1})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["safety"]["state"], "slow")
        res = self.result("robot_nudge", {"direction": "backward", "duration_ms": 300, "speed": 0.05})
        self.assertTrue(res["ok"], res)  # знак оси: назад — отрицательный выход

    def test_nudge_not_seen_by_gate_is_unconfirmed(self):
        # публикация ушла, но до safety_gate не дошла (M3: новый издатель rosbridge не нашёл подписчика) или
        # safety_gate молчит — «готово» агент сказать не может
        for flag in ("delivered", "silent"):
            with self.subTest(flag=flag):
                self.bridge.delivered, self.bridge.silent = flag != "delivered", flag == "silent"
                res = self.result("robot_nudge", {"direction": "forward", "duration_ms": 300, "speed": 0.1})
                self.assertEqual((res["ok"], res["status"]), (False, "unconfirmed"))

    def test_nudge_goes_to_agent_topic_then_zero(self):
        res = self.call("robot_nudge", {"direction": "forward", "duration_ms": 200, "speed": 0.05})
        self.assertFalse(res.get("isError"), res)
        twists = [t for _, t in self.bridge.twists()]
        moving = [t for t in twists if not is_zero(t)]
        self.assertTrue(moving and all(t["linear"]["x"] == 0.05 for t in moving))
        self.assertEqual([is_zero(t) for t in twists[-5:]], [True] * 5)
        self.assertFalse(any(m.get("topic") == "/cmd_vel" for _, m in self.bridge.messages))

    def test_publisher_connection_is_kept(self):
        # M3: одно соединение с advertise на тему, а не новое на каждую публикацию — издатель rosbridge живёт
        before = self.bridge.conns
        for _ in range(2):
            self.call("robot_nudge", {"direction": "turn_left", "duration_ms": 200, "speed": 0.3})
        pub_conns = {n for n, m in self.bridge.messages if n > before and m.get("op") == "advertise"}
        self.assertEqual(len(pub_conns), 1)
        self.assertGreaterEqual(len(self.bridge.twists(after_conn=before)), 2 * (2 + 5))

    def test_auto_stop_when_publish_fails_mid_nudge(self):
        # соединение издателя оборвано посреди толчка: одно переподключение отвергнуто — толчок едет дальше по
        # новому; отвергнуты оба — исключение в цикле, автостоп (finally) открывает своё соединение и шлёт пять нулей
        self.call("robot_stop", {})  # соединение издателя уже открыто
        self.bridge.drop_after, self.bridge.reject = 7, {self.bridge.conns + 2}  # +1 — подписка на safety_state
        res = self.call("robot_nudge", {"direction": "turn_left", "duration_ms": 500, "speed": 0.3})
        self.assertFalse(res.get("isError"), res)
        conn = max(n for n, m in self.bridge.messages if m.get("op") == "advertise")
        self.bridge.reject = {self.bridge.conns + 2, self.bridge.conns + 3}
        res = self.call("robot_nudge", {"direction": "turn_left", "duration_ms": 1000, "speed": 0.3})
        self.assertTrue(res.get("isError"), res)
        after = [t for n, t in self.bridge.twists(after_conn=conn)]
        self.assertEqual([is_zero(t) for t in after], [True] * 5)

    def test_sigterm_publishes_zero_and_stdout_stays_clean(self):
        before = self.bridge.conns
        self.proc.send_signal(signal.SIGTERM)
        out, err = self.proc.communicate(timeout=15)
        self.assertEqual(self.proc.returncode, 0, err.decode(errors="replace")[-1500:])
        self.assertEqual(out, b"")
        self.assertEqual([is_zero(t) for _, t in self.bridge.twists(after_conn=before)], [True] * 5)
        self.assertNotIn(STOP, [t for t, _ in self.bridge.publishes()])  # выход сервера — не стоп агента

    def test_agent_stop_goes_to_gate_first_auto_stop_does_not(self):
        # решение 1а: robot_stop агента — стоп по безопасности: перед каждым нулём — стоп в safety_gate (один id на
        # вызов, safety_gate исполняет его раз); автостоп в конце толчка — штатный, только нули (плавный стоп)
        res = self.result("robot_stop", {"reason": "k6"})
        self.assertEqual((res["ok"], res["stopped"]), (True, True))
        pubs = self.bridge.publishes()
        self.assertEqual([t for t, _ in pubs], [STOP, "/cmd_vel_agent"] * 5)
        docs = [json.loads(m["data"]) for t, m in pubs if t == STOP]
        self.assertEqual({d["id"] for d in docs}, {docs[0]["id"]})
        self.assertEqual((docs[0]["by"], docs[0]["reason"]), ("agent", "k6"))
        self.assertLess(abs(docs[0]["t"] - time.time()), 30.0)
        self.assertTrue(all(is_zero(m) for t, m in pubs if t != STOP))
        mark = len(self.bridge.messages)
        self.result("robot_stop", {})
        later = {json.loads(m["data"])["id"] for t, m in self.bridge.publishes(mark) if t == STOP}
        self.assertEqual(len(later), 1)
        self.assertNotIn(docs[0]["id"], later)  # новый вызов — новый стоп
        mark = len(self.bridge.messages)
        res = self.result("robot_nudge", {"direction": "forward", "duration_ms": 300, "speed": 0.1})
        self.assertTrue(res["ok"], res)
        self.assertNotIn(STOP, [t for t, _ in self.bridge.publishes(mark)])

    def test_agent_stop_without_gate_topic_still_sends_zeros(self):
        # тема стопа недоступна (оба соединения отвергнуты) — нули идут всё равно: остановка штатная, плавная
        self.bridge.reject = {self.bridge.conns + 1, self.bridge.conns + 2}
        mark = len(self.bridge.messages)
        res = self.result("robot_stop", {})
        self.assertTrue(res["ok"], res)
        pubs = self.bridge.publishes(mark)
        self.assertEqual([t for t, _ in pubs], ["/cmd_vel_agent"] * 5)
        self.assertIn("стоп агента не отправлен", self._kill()[1].decode(errors="replace"))


# Запуск прокси лаборатории из копии: адрес воркера подменяется до старта; «main» — как в юните (после патча),
# иначе — тот же модуль без патча, но по HTTP (у него только stdio), чтобы посмотреть его инструменты.
PROXY_RUNNER = """
import sys
sys.path.insert(0, sys.argv[1])
import robot_vision_mcp_proxy as m
m.WORKER_URL = sys.argv[2]
if sys.argv[3] == "main":
    m.main()
else:
    m.mcp.settings.host, m.mcp.settings.port = "127.0.0.1", int(sys.argv[4])
    m.mcp.run(transport="streamable-http")
"""


class FakeWorker:
    """Подставной «Robot Vision Demo Worker»: пишет (метод, путь, тело) и отвечает {"ok": true}."""

    def __init__(self):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, body):
                outer.requests.append((self.command, self.path, body))
                data = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._reply(None)

            def do_POST(self):
                self._reply(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:{}".format(self.httpd.server_address[1])
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def posts(self, path: str) -> list:
        return [b for m, p, b in self.requests if m == "POST" and p == path]


@unittest.skipUnless(DEPS, "нужны Python ≥ 3.10, mcp, websocket-client, websockets")
class ProxyPatchTest(unittest.TestCase):
    def start(self, patched: bool):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        shutil.copy(LAB_PROXY, tmp)
        if patched:
            r = subprocess.run(["patch", "-p1", "--quiet", "-i", str(PROXY_PATCH)], cwd=tmp, capture_output=True,
                               text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.worker = FakeWorker()
        self.addCleanup(self.worker.close)
        port = free_port()
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", ROBOT_VISION_MCP_PORT=str(port))
        self.proc = subprocess.Popen([sys.executable, "-c", PROXY_RUNNER, tmp, self.worker.url,
                                      "main" if patched else "plain", str(port)], cwd=tmp,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.addCleanup(self._kill)
        self.url = "http://127.0.0.1:{}/mcp".format(port)
        deadline = time.monotonic() + 20
        while True:
            try:
                return probe_mcp(self.url, 2.0)
            except ProbeError:
                if time.monotonic() > deadline or self.proc.poll() is not None:
                    self.fail("прокси не поднялся: " + self._kill()[1].decode(errors="replace")[-2000:])
                time.sleep(0.2)

    def _kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        return self.proc.communicate(timeout=10)

    def call(self, name: str, args: dict) -> dict:
        c = McpClient(self.url, 20.0)
        try:
            c.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                 "clientInfo": {"name": "test", "version": "1"}})
            c.notify("notifications/initialized")
            return c.rpc("tools/call", {"name": name, "arguments": args})
        finally:
            c.close()

    def doctor_mcp(self) -> doctor.Finding:
        return doctor._check_mcp(Config({"NEUROBOT_MCP_URL": self.url, "NEUROBOT_MCP_STATUS_TOOL": ""}, "/e/n.env"))

    def test_lab_proxy_duplicates_send_ms_as_seconds(self):
        res = self.start(patched=False)
        self.assertEqual(res.params["robot_nudge_forward"], ["duration_ms", "speed"])  # первая регистрация (N1)
        self.call("robot_nudge_forward", {})
        self.assertEqual(self.worker.posts("/motion/nudge"),
                         [{"direction": "forward", "duration_s": 2000, "speed": 0.8}])
        f = self.doctor_mcp()
        self.assertEqual((f.level, f.code), (doctor.FAIL, "mcp_schema"))
        self.assertIn("Tool already exists", self._kill()[1].decode(errors="replace"))

    def test_patched_proxy(self):
        lines = PROXY_PATCH.read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len([ln for ln in lines if ln.startswith("+") and not ln.startswith("+++")]), 20)
        res = self.start(patched=True)
        self.assertEqual(len(res.tools), len(set(res.tools)))
        self.assertFalse([t for t, p in res.params.items() if "duration_ms" in p])
        self.assertEqual(self.doctor_mcp().level, doctor.OK)
        self.call("robot_nudge_forward", {})
        self.call("robot_nudge", {"direction": "turn_left", "duration_s": 300, "speed": 0.8})
        # NaN и ±inf до воркера не доходят: FastMCP отклоняет их при проверке аргументов (mcp 1.30)
        for bad in (float("nan"), float("inf"), -float("inf")):
            self.assertTrue(self.call("robot_nudge", {"direction": "forward", "duration_s": bad})["isError"])
        self.assertEqual(self.worker.posts("/motion/nudge"),
                         [{"direction": "forward", "duration_s": 0.3, "speed": 0.25},
                          {"direction": "turn_left", "duration_s": 0.5, "speed": 0.25}])
        self.proc.send_signal(signal.SIGTERM)
        _, err = self.proc.communicate(timeout=15)
        self.assertEqual(self.proc.returncode, 0, err.decode(errors="replace")[-1500:])
        self.assertEqual(self.worker.posts("/motion/stop"), [{}])
        self.assertNotIn("Tool already exists", err.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
