"""Подставной живой шлюз для разработки и e2e без ROS: тот же gateway.py и live.py, вместо узла rclpy — синтетический
граф (safety_state 5 Гц, скорости, скан с /tf_static, жесты, камера, юниты, память). Источник подписан «двойник»:
числа синтетические. СТОП и защёлка доходят до подставного safety_gate: стоп — ноль в выходе и +1 в counters.stop_now
(так шлюз видит, что safety_gate стоп принял), защёлка — estop до `--release` (файл-флаг в каталоге состояния, как у
sudo neurobot estop release у робота).

  python neurobot/ui/tests/fake_live.py --port 18121 --state DIR [--bundle FILE]
  печатает адрес входа (одноразовый код) в stdout; Ctrl+C — выход
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import secrets
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)

from neurobot.ui import gateway, live  # noqa: E402


DOCTOR_OK = "import json; print(json.dumps({'ok': True, 'exit': 0, 'root': None, 'findings': []}))"


class FakeGraph:
    """Подставной граф: safety_gate с защёлкой и стопом, исполнитель, perception, лидар."""

    def __init__(self, lv: live.Live, state_dir: str):
        self.lv = lv
        self.state_dir = state_dir
        self.name = "ui_gateway"
        self.latch_since = None
        self.stop_until = 0.0
        self.stopped = 0
        self.lock = threading.Lock()

    # действия пульта
    def publishers(self):
        return sorted(live.PUBLISHERS)

    def stop(self, doc):
        with self.lock:
            self.stop_until = time.time() + 2.0
            self.stopped += 1
        return 1

    def engage(self, doc):
        assert doc["cmd"] == "engage"
        with self.lock:
            if self.latch_since is None:
                self.latch_since = time.time()
        return 1

    def release_requested(self) -> bool:
        path = os.path.join(self.state_dir, "fake_release")
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    def run(self, stop: threading.Event):
        lv = self.lv
        lv.on_tf_static([("base_link", "laser_frame", (0.07, 0.0, 0.67), (0.0, 0.0, 0.0, 1.0))])
        t_start = time.time()
        i = 0
        gesture_seq = 0
        while not stop.is_set():
            t = time.time()
            k = t - t_start
            with self.lock:
                if self.latch_since is not None and self.release_requested():
                    self.latch_since = None
                    self.stop_until = t + 1.0          # unarmed: до нулевого запроса
                latched = self.latch_since is not None
                stopped = t < self.stop_until
            phase = (k % 30.0)
            want = 0.25 if 8.0 <= phase < 14.0 else 0.0
            v = 0.0 if latched or stopped else min(want, 0.15)
            clearance = 0.6 + 0.4 * math.cos(k / 7.0)
            if latched:
                state, reasons = "estop", ["estop: защёлка (оператор: кнопка пульта)"]
            elif stopped:
                state, reasons = "blocked", ["unarmed: не взведён: ждёт нулевой запрос"]
            else:
                state, reasons = "slow", ["lidar_blind: перед в слепом секторе лидара — только глубина, ≤ 0.15 м/с"]
            if i % 2 == 0:
                lv.on_safety({"boot": "fake", "state": state, "reasons": reasons, "warnings": [],
                              "counters": {"stop_now": self.stopped},
                              "min_clearance_m": round(clearance, 3),
                              "front_m": round(clearance + 0.3, 3),
                              "limits": {"v_max": 0.0 if latched or stopped else 0.15, "v_rev": 0.05, "w_max": 0.8},
                              "req": [want, 0.0], "out": [v, 0.0], "cut": want > v, "tilt_deg": 0.3,
                              "lidar_mask_deg": [[-61.9, 4.9]],
                              "estop": {"engaged": latched, "since": self.latch_since,
                                        "reasons": ["estop: оператор"] if latched else []}}, t)
            lv.on_cmd("out", v, 0.0, t)          # req — из safety_state, как у узла шлюза
            if i % 10 == 0:
                lv.on_health({"fps": 14.8, "frame_age_s": 0.07, "recoveries": 0, "state": "ok", "dark": False}, t)
            if i % 2 == 1:
                ranges = []
                for j in range(683):
                    a = -2.0923 + j * 4.1846 / 682
                    d = 2.2 / max(0.2, abs(math.cos(a))) if abs(a) < 1.2 else 1.5 / max(0.2, abs(math.sin(a)))
                    ranges.append(min(5.5, d) if not (168 <= j < 354) else float("nan"))
                lv.on_scan("laser_frame", -2.0923, 4.1846 / 682, ranges, 0.02, 5.6, t)
            if phase >= 7.7 and phase < 7.8 and i % 1 == 0:
                gesture_seq += 1
                lv.on_gesture({"boot": "fake", "seq": gesture_seq, "cls": 1, "name": "come", "conf": 0.93,
                               "margin": 0.31, "side": "left", "n_boxes": 16, "t_end": t - 0.12, "t_rec": t}, t)
            lv.tick(t)
            i += 1
            stop.wait(0.1)

    def poll(self, stop: threading.Event):
        while not stop.is_set():
            t = time.time()
            rows = [{"comp": c, "unit": "neurobot-%s.service" % c, "active": "active", "sub": "running", "restarts": 0}
                    for c in ("driver@twin", "driver@lidar", "driver@camera", "safety", "executor", "perception",
                              "rosbridge", "mcp", "picoclaw", "watchdog")]
            self.lv.on_units(rows, t)
            self.lv.on_mem({"safety": (66000, int(t * 20), 1), "perception": (700000, int(t * 60), 1),
                            "driver@twin": (520000, int(t * 90), 4)}, t, 100)
            stop.wait(5.0)


def login_code(state_dir: str, ttl: float = 600.0) -> str:
    code = secrets.token_urlsafe(12)
    path = os.path.join(state_dir, "codes", hashlib.sha256(code.encode()).hexdigest())
    with open(path, "w") as f:
        json.dump({"exp": time.time() + ttl}, f)
    return code


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=18121)
    p.add_argument("--state", required=True)
    p.add_argument("--bundle")
    a = p.parse_args()
    hub = live.Hub()
    lv = live.Live(hub, not_robot=["driver@twin"])
    graph = FakeGraph(lv, a.state)
    auth = gateway.Auth(a.state)
    gw = gateway.Gateway(hub, lv, graph, auth, "двойник", "подставной граф · e2e",
                         audit=gateway.Audit(os.path.join(a.state, "audit.jsonl")),
                         doctor=gateway.Doctor([sys.executable, "-c", DOCTOR_OK], 60.0), bundle=a.bundle)
    srv = gateway.make_server(gw, a.port)
    stop = threading.Event()
    threading.Thread(target=graph.run, args=(stop,), daemon=True).start()
    threading.Thread(target=graph.poll, args=(stop,), daemon=True).start()
    print("http://127.0.0.1:%d/login?code=%s" % (srv.server_address[1], login_code(a.state)), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        gw.stopping.set()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
