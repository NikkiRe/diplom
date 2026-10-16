#!/usr/bin/env python3
"""Подставные компоненты neurobot: проверка запуска без робота (ВМ sim, тесты). Это не робот — ни жестов, ни
движения; только то, что видят systemd и doctor: READY/WATCHDOG, порты, темы, строки журнала PicoClaw.

    fake_component.py safety              узел safety_gate: нули в /cmd_vel 10 Гц, /neurobot/safety_state 5 Гц,
                                          защёлка по /neurobot/estop (engage — кто угодно, release — by=human), она
                                          хранится в $STATE_DIRECTORY/estop.json и читается до READY (файл не
                                          читается — старт с защёлкой); READY после первого нуля, WATCHDOG от тика,
                                          ноль при выходе
    fake_component.py executor            узел motion_executor (как страж v1): нули в «/cmd_vel» 10 Гц — во вход
                                          safety_gate их уводит ремап из --ros-args; /neurobot/motion_state 2 Гц;
                                          READY после первого нуля, WATCHDOG от тика
    fake_component.py perception          узел velocity_publisher (как predict_live): /neurobot/perception_health 1 Гц,
                                          нули в «/cmd_vel» 10 Гц (DriveController legacy, ремап — из --ros-args);
                                          READY через --warmup с; WATCHDOG по «кадрам»
    fake_component.py driver --topic T    LaserScan в T 10 Гц (frame_id — NEUROBOT_SCAN_FRAME)
    fake_component.py base                узел twist_mux: /cmd_vel -> NEUROBOT_BASE_CMD_TOPIC; датчики
                                          NEUROBOT_SENSORS — пустые сообщения с удвоенной минимальной частотой
                                          (не реже 2 Гц)
    fake_component.py publish --topic T   нулевой Twist в T 10 Гц от узла --node (лишний писатель скорости)
    fake_component.py rosbridge [имя:=значение …]
                                          ответ 101 на WebSocket-рукопожатие на NEUROBOT_ROSBRIDGE_PORT; аргументы как
                                          у ros2 launch rosbridge (topics_glob, services_glob, params_glob) — для
                                          precheck, сам подставной их не применяет
    fake_component.py worker --name N     HTTP-воркер лаборатории на адресе NEUROBOT_LABWORKER_<N>_URL: сразу держит
                                          порт, --warmup с отвечает 503 «loading» (загрузка модели), потом 200
    fake_component.py mcp                 MCP streamable HTTP (JSON) на адресе NEUROBOT_MCP_URL:
                                          robot_get_status, robot_stop, robot_nudge
    fake_component.py picoclaw            подключается к MCP и пишет строки журнала PicoClaw; держит порт шлюза;
                                          --tool-every S — вызов robot_get_status без таймаута (как PicoClaw, PC4);
                                          --channel-fail-after S — строка «Long poll failed» (канал VK оглох, N10)
Отказы: --hang-after S — перестать слать WATCHDOG и отвечать (MCP, воркер), --hang-tools-after S — MCP висит только
на tools/call, --exit-after S — выйти с кодом 1, --ignore-remap — узел не применяет ремапы из --ros-args (потерянный
ремап: исполнитель или распознавание пишут /cmd_vel сами).
Аргументы после --ros-args — для rclpy (ремапы). Темы, порты и адреса — из окружения юнита (NEUROBOT_* env-файла).
Логи — в stderr, кроме picoclaw: настоящий пишет в stdout, и сторож с doctor читают именно его строки.
"""
import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from neurobot_deploy.probes import McpClient, ProbeError, probe_mcp  # noqa: E402

START = time.monotonic()
NUDGE = {"direction": {"type": "string"}, "duration_ms": {"type": "integer"}, "speed": {"type": "number"}}
TOOLS = [{"name": n, "description": "fake", "inputSchema": {"type": "object", "properties": props}}
         for n, props in (("robot_get_status", {}), ("robot_stop", {}), ("robot_nudge", NUDGE))]


def notify(state: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.connect(addr)
        s.sendall(state.encode())


class Faults:
    def __init__(self, args):
        self.hang_after, self.exit_after = args.hang_after, args.exit_after
        self.hang_tools_after = getattr(args, "hang_tools_after", None)

    def hung(self) -> bool:
        return self.hang_after is not None and time.monotonic() - START > self.hang_after

    def tools_hung(self) -> bool:
        return self.hang_tools_after is not None and time.monotonic() - START > self.hang_tools_after

    def check_exit(self) -> None:
        if self.exit_after is not None and time.monotonic() - START > self.exit_after:
            print("fake: выход по --exit-after", file=sys.stderr, flush=True)
            os._exit(1)


def ros_node(role: str, body, remap: bool = True) -> int:
    import rclpy

    rclpy.init(args=sys.argv)
    node = rclpy.create_node(role, use_global_arguments=remap)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):  # поверх обработчиков rclpy: выходим сами, с нулём
        signal.signal(sig, lambda *_: stop.set())
    on_exit = body(node)
    try:
        while not stop.is_set():
            rclpy.spin_once(node, timeout_sec=0.05)
    finally:
        if on_exit:
            on_exit()
        node.destroy_node()
        rclpy.shutdown()
    return 0


def _twist(node, topic: str, stamped: bool = False):
    """Издатель Twist или TwistStamped и функция «опубликовать ноль»."""
    from geometry_msgs.msg import Twist, TwistStamped

    pub = node.create_publisher(TwistStamped if stamped else Twist, topic, 10)

    def zero():
        msg = TwistStamped() if stamped else Twist()
        if stamped:
            msg.header.stamp = node.get_clock().now().to_msg()
        pub.publish(msg)
    return zero


def _ticking(node, faults: Faults, zero, extra=None):
    """Тик 10 Гц: ноль, READY после первого, WATCHDOG; extra(n) — своё на каждом тике."""
    n = [0]

    def tick():
        faults.check_exit()
        if faults.hung():
            return
        zero()
        if extra:
            extra(n[0])
        if not n[0]:
            notify("READY=1")
        notify("WATCHDOG=1")
        n[0] += 1
    node.create_timer(0.1, tick)


def load_latch(path: str) -> list:
    """Причины защёлки из файла; файла нет — защёлки нет, файл не читается — стоим (до release человеком)."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return [str(r) for r in data["reasons"]] if data["engaged"] else []
    except (OSError, ValueError, KeyError, TypeError):
        return ["estop: файл защёлки {} не читается".format(path)]


def save_latch(path: str, reasons: list) -> None:
    """Атомарно: временный файл, fsync, rename — после сбоя питания файл либо прежний, либо новый."""
    if not path:
        return
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"engaged": bool(reasons), "reasons": reasons, "t": round(time.time(), 3)}, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:  # защёлка остаётся в памяти; после рестарта её не будет — это видно в журнале
        print("fake: защёлка не сохранена в {}: {}".format(path, e), file=sys.stderr, flush=True)


def run_safety(args, faults: Faults) -> int:
    from geometry_msgs.msg import Twist
    from std_msgs.msg import String

    def body(node):
        zero = _twist(node, os.environ.get("NEUROBOT_CMD_VEL", "/cmd_vel"),
                      os.environ.get("NEUROBOT_CMD_VEL_TYPE", "").endswith("TwistStamped"))
        state_pub = node.create_publisher(String, os.environ.get("NEUROBOT_SAFETY_STATE_TOPIC",
                                                                 "/neurobot/safety_state"), 10)
        state_dir = os.environ.get("STATE_DIRECTORY", "").split(":")[0]
        path = os.path.join(state_dir, "estop.json") if state_dir else ""
        latch = load_latch(path)

        def on_estop(msg):
            try:
                cmd = json.loads(msg.data)
            except ValueError:
                return
            if cmd.get("cmd") == "engage" and not latch:
                latch.append("estop: " + (cmd.get("reason") or cmd.get("by") or "?"))
                save_latch(path, latch)
            elif cmd.get("cmd") == "release" and cmd.get("by") == "human" and latch:
                latch.clear()
                save_latch(path, latch)
        node.create_subscription(Twist, os.environ.get("NEUROBOT_CMD_VEL_REQ", "/neurobot/cmd_vel_req"),
                                 lambda _m: None, 10)
        node.create_subscription(String, os.environ.get("NEUROBOT_ESTOP_TOPIC", "/neurobot/estop"), on_estop, 10)

        def state(n):
            if n % 2 == 0:
                state_pub.publish(String(data=json.dumps({
                    "state": "estop" if latch else "ok", "reasons": list(latch), "min_clearance_m": None,
                    "limits": {"v_max": 0.25, "w_max": 0.8}})))
        _ticking(node, faults, zero, state)
        return zero
    return ros_node(os.environ.get("NEUROBOT_SAFETY_NODE") or "safety_gate", body)


def run_executor(args, faults: Faults) -> int:
    from std_msgs.msg import String

    def body(node):
        zero = _twist(node, "/cmd_vel")
        state_pub = node.create_publisher(String, os.environ.get("NEUROBOT_MOTION_STATE_TOPIC",
                                                                 "/neurobot/motion_state"), 10)

        def state(n):
            if n % 5 == 0:
                state_pub.publish(String(data=json.dumps({"moving": False, "by": "none", "fake": True})))
        _ticking(node, faults, zero, state)
        return zero
    return ros_node(os.environ.get("NEUROBOT_EXECUTOR_NODE") or "motion_executor", body, not args.ignore_remap)


def run_perception(args, faults: Faults) -> int:
    from std_msgs.msg import String

    def body(node):
        pub = node.create_publisher(String, os.environ.get("NEUROBOT_HEALTH_TOPIC", "/neurobot/perception_health"), 10)
        node.create_timer(0.1, _twist(node, "/cmd_vel"))

        def tick():
            faults.check_exit()
            warm = time.monotonic() - START >= args.warmup
            hung = faults.hung()
            state = "starting" if not warm else ("recovering" if hung else "ok")
            pub.publish(String(data=json.dumps({"fps": 0.0 if hung else 10.0, "frame_age_s": 9.9 if hung else 0.1,
                                                "recoveries": 0, "state": state})))
            if warm and not hung:
                notify("READY=1")
                notify("WATCHDOG=1")
        node.create_timer(1.0, tick)
        return None
    return ros_node(os.environ.get("NEUROBOT_PERCEPTION_NODE") or "velocity_publisher", body, not args.ignore_remap)


def run_driver(args, faults: Faults) -> int:
    from sensor_msgs.msg import LaserScan

    def body(node):
        pub = node.create_publisher(LaserScan, args.topic, 10)

        def tick():
            faults.check_exit()
            if faults.hung():
                return
            msg = LaserScan(angle_min=-2.0923497677, angle_max=2.0923497677, angle_increment=0.0061359,
                            range_min=0.02, range_max=5.6)
            msg.header.stamp = node.get_clock().now().to_msg()
            msg.header.frame_id = os.environ.get("NEUROBOT_SCAN_FRAME") or "laser_frame"
            msg.ranges = [2.0] * 683
            pub.publish(msg)
        node.create_timer(0.1, tick)
        return None
    return ros_node("urg_node2", body)


def run_base(args, faults: Faults) -> int:
    import importlib

    from geometry_msgs.msg import Twist

    def body(node):
        out = node.create_publisher(Twist, os.environ.get("NEUROBOT_BASE_CMD_TOPIC", "/diff_cont/cmd_vel_unstamped"),
                                    10)
        node.create_subscription(Twist, os.environ.get("NEUROBOT_CMD_VEL", "/cmd_vel"), out.publish, 10)
        for token in os.environ.get("NEUROBOT_SENSORS", "").split():
            topic, type_name, *hz = token.split(":")
            pkg, _, name = type_name.split("/")
            cls = getattr(importlib.import_module(pkg + ".msg"), name)
            pub = node.create_publisher(cls, topic, 10)
            node.create_timer(1.0 / max(2.0, 2 * float(hz[0]) if hz else 2.0),
                              lambda pub=pub, cls=cls: None if faults.hung() else pub.publish(cls()))
        return None
    return ros_node(os.environ.get("NEUROBOT_MUX_NODE") or "twist_mux", body)


def run_publish(args, faults: Faults) -> int:
    def body(node):
        zero = _twist(node, args.topic)
        node.create_timer(0.1, zero)
        return None
    return ros_node(args.node, body)


def serve_tcp(port: int, handle) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(16)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def run_rosbridge(args, faults: Faults) -> int:
    def handle(conn):
        with conn:
            conn.recv(4096)
            if not faults.hung():
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
                time.sleep(0.2)
    threading.Thread(target=lambda: _exit_watch(faults), daemon=True).start()
    serve_tcp(int(os.environ.get("NEUROBOT_ROSBRIDGE_PORT", "9090")), handle)
    return 0


def run_worker(args, faults: Faults) -> int:
    u = urlsplit(os.environ["NEUROBOT_LABWORKER_{}_URL".format(args.name)])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            pass

        def do_GET(self):
            while faults.hung():
                time.sleep(1)
            ready = time.monotonic() - START >= args.warmup
            body = json.dumps({"ok": ready, "state": "ready" if ready else "loading"}).encode()
            self.send_response(200 if ready or self.path != u.path else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    threading.Thread(target=lambda: _exit_watch(faults), daemon=True).start()
    ThreadingHTTPServer((u.hostname, u.port), Handler).serve_forever()
    return 0


def _exit_watch(faults: Faults) -> None:
    while True:
        faults.check_exit()
        time.sleep(0.2)


class McpHandler(BaseHTTPRequestHandler):
    faults: Faults = None

    def log_message(self, fmt, *a):
        pass

    def _send(self, code: int, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        while self.faults.hung():  # как синхронный инструмент FastMCP: цикл событий занят (M5)
            time.sleep(1)
        method, rid = req.get("method"), req.get("id")
        if rid is None:
            return self._send(202)
        if method == "initialize":
            result = {"protocolVersion": req["params"].get("protocolVersion", "2025-03-26"),
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-mcp", "version": "1"}}
            return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": result},
                              {"Mcp-Session-Id": "fake-{}".format(time.monotonic_ns())})
        if method == "tools/list":
            return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        if method == "tools/call":
            while self.faults.tools_hung():  # асинхронный сервер с зависшим инструментом: tools/list отвечает
                time.sleep(1)
            text = json.dumps({"connected": True, "rosbridge": "ok", "fake": True})
            return self._send(200, {"jsonrpc": "2.0", "id": rid,
                                    "result": {"content": [{"type": "text", "text": text}], "isError": False}})
        return self._send(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "нет метода"}})

    def do_DELETE(self):
        self._send(200)


def run_mcp(args, faults: Faults) -> int:
    u = urlsplit(os.environ.get("NEUROBOT_MCP_URL", "http://127.0.0.1:18110/mcp"))
    McpHandler.faults = faults
    threading.Thread(target=lambda: _exit_watch(faults), daemon=True).start()
    ThreadingHTTPServer((u.hostname, u.port), McpHandler).serve_forever()
    return 0


def pc_log(level: str, comp: str, msg: str) -> None:
    """Строка в формате zerolog ConsoleWriter PicoClaw v0.3.1."""
    print("{} {} {} fake/picoclaw.go:1 > {}".format(time.strftime("%H:%M:%S"), level, comp, msg), flush=True)


def run_picoclaw(args, faults: Faults) -> int:
    url = os.environ.get("NEUROBOT_MCP_URL", "http://127.0.0.1:18110/mcp")
    gw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    gw.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    gw.bind(("127.0.0.1", int(os.environ.get("NEUROBOT_PICOCLAW_PORT", "18790"))))
    gw.listen(4)
    try:
        res = probe_mcp(url, 5.0)
        pc_log("INF", "mcp", "Connected to MCP server protocol=2025-03-26 server=robot_mcp serverName=fake-mcp "
                             "tools={}".format(len(res.tools)))
        pc_log("INF", "mcp", "MCP server initialization complete active_servers=1 total_tools={}".format(
            len(res.tools)))
    except ProbeError as e:
        pc_log("ERR", "mcp", "All MCP servers failed to connect error={}".format(str(e).replace(" ", "_")))
    last, channel_failed = time.monotonic(), False
    while True:
        faults.check_exit()
        fail_at = args.channel_fail_after
        if fail_at is not None and not channel_failed and time.monotonic() - START > fail_at:
            channel_failed = True  # как vksdk: ключ сервера long poll — в URL ошибки (здесь фиктивный)
            pc_log("ERR", "vk", 'Long poll failed error="Post \\"https://lp.vk.com/whp1?act=a_check'
                                '&key=fakekey0123456789&ts=7&wait=25\\": context deadline exceeded"')
        if args.tool_every and time.monotonic() - last >= args.tool_every:
            last = time.monotonic()
            tool = "mcp_robot_mcp_robot_get_status"
            pc_log("INF", "tool", "Tool execution started args={{}} tool={}".format(tool))
            t0 = time.monotonic()
            c = McpClient(url, timeout=3600)
            try:
                c.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                     "clientInfo": {"name": "fake-picoclaw", "version": "1"}})
                c.rpc("tools/call", {"name": "robot_get_status", "arguments": {}})
                pc_log("INF", "tool", "Tool execution completed duration_ms={} result_length=40 tool={}".format(
                    int((time.monotonic() - t0) * 1000), tool))
            except ProbeError as e:
                pc_log("ERR", "tool", "Tool execution failed duration={} error=MCP_tool_execution_failed:_{} "
                                      "tool={}".format(int((time.monotonic() - t0) * 1000),
                                                       str(e).replace(" ", "_"), tool))
            finally:
                c.close()
        time.sleep(0.2)


ROLES = {"safety": run_safety, "executor": run_executor, "perception": run_perception, "driver": run_driver,
         "base": run_base, "publish": run_publish, "rosbridge": run_rosbridge, "worker": run_worker, "mcp": run_mcp,
         "picoclaw": run_picoclaw}


def main() -> int:
    p = argparse.ArgumentParser(description="подставной компонент neurobot")
    p.add_argument("role", choices=sorted(ROLES))
    p.add_argument("launch", nargs="*", help="rosbridge: «имя:=значение», как у ros2 launch (смотрит precheck)")
    p.add_argument("--warmup", type=float, default=2.0, help="perception: прогрев до READY; worker: «загрузка», с")
    p.add_argument("--topic", default="/scan", help="driver: тема LaserScan; publish: тема Twist")
    p.add_argument("--node", default="cmd_vel_tracker_pub", help="publish: имя узла")
    p.add_argument("--name", default="", help="worker: имя экземпляра (адрес — NEUROBOT_LABWORKER_<имя>_URL)")
    p.add_argument("--tool-every", type=float, default=0.0, help="picoclaw: вызов инструмента раз в S с (0 — нет)")
    p.add_argument("--channel-fail-after", type=float, help="picoclaw: через S с «Long poll failed» (N10)")
    p.add_argument("--hang-after", type=float, help="через S с перестать слать WATCHDOG и отвечать")
    p.add_argument("--hang-tools-after", type=float, help="mcp: через S с зависать на tools/call (tools/list отвечает)")
    p.add_argument("--exit-after", type=float, help="через S с выйти с кодом 1")
    p.add_argument("--ignore-remap", action="store_true", help="executor, perception: не применять ремапы --ros-args")
    argv = sys.argv[1:]
    args = p.parse_args(argv[:argv.index("--ros-args")] if "--ros-args" in argv else argv)
    bad = [a for a in args.launch if args.role != "rosbridge" or ":=" not in a]
    if bad:
        p.error("лишние аргументы {}".format(bad))
    return ROLES[args.role](args, Faults(args))


if __name__ == "__main__":
    sys.exit(main())
