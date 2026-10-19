"""Пульт: живой шлюз по HTTP (gateway.py) с подставным узлом и узел rclpy (node.py) с подставным rclpy — без ROS.

Вход: токен 0600 в каталоге 0700 переживает рестарт, одноразовый код → сессия со сроком на сервере (cookie HttpOnly,
SameSite=Strict; переживает рестарт, истекает, отзывается удалением файла; CSRF — от сессии), без cookie /api/* — 401,
чужой Host или Origin — 403 (туннель — любой локальный порт), POST — только с Origin страницы и заголовком CSRF; СТОП
(200 — принял safety_gate по counters.stop_now, 202 — без подтверждения, 503 — никто не слушает) и защёлка (только
engage: тело с release ничего не меняет); команда без issued_at — 400, устаревшая в пути — 409 и в аудите, в граф не
уходит; маршрутов снятия, задач и скоростей нет (404), прочие методы — 405; аудит без токена и сессии; SSE: hello,
снимок, хвост, hb раз в 0,5 с, resume по Last-Event-ID, новый поток сверх предела вытесняет самый старый, начало
потока — кусками; doctor — по кнопке, не чаще раза в N с, 0 — выключен. Узел: Node без глобальных аргументов ROS
(ремапа нет), издатели — ровно live.PUBLISHERS с QoS reliable + volatile, подписки — без Twist исполнителя и входа базы,
скан — сырым, разбор только нужных; стоп — 5 копий с одним id, защёлка — 3, другая команда защёлки — отказ;
create_publisher во всём пакете пульта — одна строка. Только stdlib, Python 3.8+."""
from __future__ import annotations

import hashlib
import http.client
import importlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
for p in (str(ROOT), str(ROOT / "neurobot" / "deploy")):
    if p not in sys.path:
        sys.path.insert(0, p)

from neurobot.ui import gateway, live, stream  # noqa: E402

UI = ROOT / "neurobot" / "ui"


def write_code(state, code, ttl=60.0):
    path = os.path.join(state, "codes", hashlib.sha256(code.encode()).hexdigest())
    with open(path, "w") as f:
        json.dump({"exp": time.time() + ttl}, f)


def estop_state(t, stops=0):
    return {"boot": "g1", "state": "estop", "reasons": ["estop: защёлка (оператор)"], "warnings": [],
            "estop": {"engaged": True, "since": t, "reasons": ["estop: оператор"]}, "counters": {"stop_now": stops}}


def slow_state(stops=0):
    return {"boot": "g1", "state": "slow", "reasons": ["lidar_blind: перед в слепом секторе"], "warnings": [],
            "counters": {"stop_now": stops}}


class StubNode:
    """Узел шлюза с подставным safety_gate: стоп он принимает (counters.stop_now в следующем safety_state), защёлку
    подтверждает state = estop. confirm = False — safety_gate «лежит», а тему слушает кто-то другой."""

    name = "ui_gateway"

    def __init__(self, lv):
        self.lv = lv
        self.sent = []
        self.subs = 1
        self.confirm = True
        self.stops = 0

    def publishers(self):
        return list(live.PUBLISHERS) + ["/parameter_events"]

    def stop(self, doc):
        self.sent.append(("stop", doc))
        if self.subs and self.confirm:
            self.stops += 1
            n = self.stops
            threading.Timer(0.05, lambda: self.lv.on_safety(slow_state(n), time.time())).start()
        return self.subs

    def engage(self, doc):
        self.sent.append(("estop", doc))
        if self.subs and self.confirm:   # safety_gate подтверждает защёлку в safety_state
            threading.Timer(0.1, lambda: self.lv.on_safety(estop_state(time.time(), self.stops), time.time())).start()
        return self.subs


class FakeRun:
    def __init__(self):
        self.calls = 0

    def __call__(self, cmd, **kw):
        self.calls += 1
        out = json.dumps({"ok": True, "exit": 0, "root": None, "findings": []})
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")


class GatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = os.path.join(cls.tmp.name, "ui")
        bundle = os.path.join(cls.tmp.name, "b.json.gz")
        stream.write_bundle(bundle, {"format": stream.BUNDLE_FORMAT, "stream_format": stream.FORMAT, "geometry": {},
                                     "alarms": {}, "compare_rows": [], "runs": [
                                         {"id": "r1", "label": "прогон", "system": "a1", "source": "запись",
                                          "t_start": 0.0, "t_end": 1.0, "summary": {}, "stream": []}]})
        cls.hub = live.Hub(max_clients=3)
        cls.lv = live.Live(cls.hub)
        cls.lv.on_safety(slow_state(), time.time())
        cls.node = StubNode(cls.lv)
        cls.auth = gateway.Auth(cls.state)
        cls.fake_run = FakeRun()
        cls.doctor = gateway.Doctor(["neurobot", "doctor", "--json"], 60.0, run=cls.fake_run)
        cls.gw = gateway.Gateway(cls.hub, cls.lv, cls.node, cls.auth, "двойник", "lima-sim · домен 23",
                                 audit=gateway.Audit(os.path.join(cls.state, "audit.jsonl")), doctor=cls.doctor,
                                 bundle=bundle)
        cls.srv = gateway.make_server(cls.gw, 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        code = "c0de-%d" % os.getpid()
        write_code(cls.state, code)
        r, _ = cls.request(cls, "/login?code=" + code)
        cls.cookie = r.getheader("Set-Cookie").split(";", 1)[0]

    @classmethod
    def tearDownClass(cls):
        cls.gw.stopping.set()
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def request(self, path, method="GET", headers=None, body=None, cookie=True, origin=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": "127.0.0.1:%d" % self.port}
        if cookie and getattr(self, "cookie", None):
            h["Cookie"] = self.cookie
        if origin:
            h["Origin"] = "http://127.0.0.1:%d" % self.port
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    @property
    def sid(self):
        return self.cookie.split("=", 1)[1]

    def post(self, path, body=None, **kw):
        """POST страницы: CSRF своей сессии и issued_at «сейчас» (body — dict; bytes — как есть)."""
        if body is None or isinstance(body, dict):
            body = json.dumps(dict({"issued_at": time.time()}, **(body or {}))).encode()
        h = {"Content-Type": "application/json", gateway.CSRF_HEADER: self.auth.csrf_for(self.sid)}
        h.update(kw.pop("headers", {}))
        return self.request(path, "POST", headers=h, body=body, origin=kw.pop("origin", True), **kw)

    # -- вход

    def test_token_file_and_restart(self):
        token = os.path.join(self.state, "token")
        self.assertEqual(os.stat(token).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o700)
        again = gateway.Auth(self.state)                     # рестарт шлюза — тот же токен, сессия действует
        self.assertEqual(again.token, self.auth.token)
        self.assertEqual(again.csrf_for(self.sid), self.auth.csrf_for(self.sid))
        self.assertTrue(again.cookie_ok("x=1; " + self.cookie))
        wrong = self.cookie[:-1] + ("A" if not self.cookie.endswith("A") else "B")      # другой последний знак
        self.assertFalse(again.cookie_ok(wrong))
        self.assertTrue(self.auth.cookie_name.startswith("nbui_"))
        self.assertNotIn(self.auth.token, self.cookie)
        sessions = os.listdir(os.path.join(self.state, "sessions"))
        self.assertNotIn(self.sid, " ".join(sessions))      # в имени файла — хеш сессии, не она сама
        self.assertEqual(os.stat(os.path.join(self.state, "sessions", sessions[0])).st_mode & 0o777, 0o600)

    def test_session_expires_on_server_and_is_revoked_alone(self):
        now = [1000.0]
        auth = gateway.Auth(self.state, clock=lambda: now[0], ttl_s=60.0)
        a, b = auth.new_session(), auth.new_session()
        self.assertNotEqual(a, b)                            # своя сессия на каждый вход
        ca, cb = "%s=%s" % (auth.cookie_name, a), "%s=%s" % (auth.cookie_name, b)
        self.assertTrue(auth.cookie_ok(ca) and auth.cookie_ok(cb))
        self.assertIn("Max-Age=60", auth.set_cookie(a))
        os.remove(auth._session_path(a))                     # отзыв одной сессии — без смены токена
        self.assertFalse(auth.cookie_ok(ca))
        self.assertTrue(auth.cookie_ok(cb))
        self.assertNotEqual(auth.csrf_for(a), auth.csrf_for(b))
        self.assertFalse(auth.csrf_ok(auth.csrf_for(a), b))   # CSRF чужой сессии не подходит
        now[0] += 61                                         # срок держит сервер, а не Max-Age браузера
        self.assertFalse(auth.cookie_ok(cb))
        self.assertFalse(os.path.exists(auth._session_path(b)))
        self.assertFalse(auth.cookie_ok("%s=../../token" % auth.cookie_name))

    def test_login_is_one_time_and_cookie_is_strict(self):
        write_code(self.state, "once")
        r, _ = self.request("/login?code=once", cookie=False)
        self.assertEqual((r.status, r.getheader("Location")), (303, "/"))
        cookie = r.getheader("Set-Cookie")
        for attr in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attr, cookie)
        self.assertNotIn(self.auth.token, cookie)
        r, data = self.request("/login?code=once", cookie=False)
        self.assertEqual(r.status, 401)
        self.assertIn("sudo neurobot ui-url", data.decode())
        write_code(self.state, "late", ttl=-1)
        self.assertEqual(self.request("/login?code=late", cookie=False)[0].status, 401)

    def test_api_needs_cookie_static_does_not(self):
        for path in ("/api/meta", "/api/stream", "/api/bundle", "/api/doctor"):
            r, data = self.request(path, cookie=False)
            self.assertEqual(r.status, 401, path)
            self.assertIn("ui-url", json.loads(data)["error"])
        self.assertEqual(self.request("/", cookie=False)[0].status, 200)
        r, data = self.request("/healthz", cookie=False)
        h = json.loads(data)
        self.assertEqual((r.status, h["node"], h["publishers"][:2]), (200, "ui_gateway", list(live.PUBLISHERS)))
        self.assertLess(len(data), 400)                      # doctor читает только начало тела

    def test_host_and_origin(self):
        self.assertEqual(self.request("/api/meta", headers={"Host": "evil.example:18120"})[0].status, 403)
        self.assertEqual(self.request("/api/meta", headers={"Host": "localhost:28120"})[0].status, 200)   # туннель
        same = {"Host": "localhost:28120", "Origin": "http://localhost:28120"}
        self.assertEqual(self.request("/api/meta", headers=same)[0].status, 200)
        # страница с другого локального порта — чужая: cookie у неё есть (SameSite не различает порты), Origin — нет
        self.assertEqual(self.request("/api/meta", headers={"Origin": "http://localhost:9999"})[0].status, 403)
        self.assertEqual(self.request("/api/meta", headers={"Origin": "null"})[0].status, 403)

    def test_meta(self):
        r, data = self.request("/api/meta")
        m = json.loads(data)
        self.assertEqual((m["mode"], m["modes"], m["source"], m["csrf"]), ("live", ["live", "replay"], "двойник",
                                                                           self.auth.csrf_for(self.sid)))
        self.assertLess(abs(m["t"] - time.time()), 2)        # часы робота: issued_at команд в «Записи»
        self.assertNotIn(self.sid, json.dumps({k: v for k, v in m.items()}))
        self.assertEqual(m["publishers"], list(live.PUBLISHERS))
        self.assertNotIn(self.auth.token, data.decode())

    # -- команды

    def test_stop(self):
        n = len(self.node.sent)
        r, data = self.post("/api/stop", {"reason": " стоп\nс пульта "})
        res = json.loads(data)
        self.assertEqual((r.status, res["ok"], res["sent"], res["subs"]), (200, True, True, 1))
        self.assertLess(res["confirmed_s"], gateway.STOP_WAIT_S)   # safety_gate принял: счётчик стопов вырос
        kind, doc = self.node.sent[n]
        self.assertEqual((kind, doc["by"], doc["reason"], doc["id"]),
                         ("stop", "ui:operator", "стоп с пульта", res["id"]))
        self.assertEqual(set(doc), {"id", "by", "reason", "t"})       # контракт /neurobot/stop_now
        self.assertTrue(any(m["k"] == "operator" and m["action"] == "СТОП движения"
                            for m in decoded_buffer(self.hub)))
        audit = open(os.path.join(self.state, "audit.jsonl"), encoding="utf-8").read()
        self.assertIn(res["id"], audit)
        self.assertNotIn(self.auth.token, audit)
        self.assertNotIn(self.sid, audit)

    def test_stop_heard_by_someone_else_is_not_confirmed(self):
        # тему слушает не safety_gate (клиент rosbridge, ros2 topic echo, ПК лаборатории): подписчик есть, счётчика нет
        self.node.confirm = False
        try:
            r, data = self.post("/api/stop")
        finally:
            self.node.confirm = True
        res = json.loads(data)
        self.assertEqual((r.status, res["ok"], res["sent"]), (202, False, True))
        self.assertIn("не подтвердил", res["error"])

    def test_stop_without_listener(self):
        self.node.subs = 0
        try:
            r, data = self.post("/api/stop")
        finally:
            self.node.subs = 1
        self.assertEqual(r.status, 503)
        self.assertIn("safety_gate не запущен", json.loads(data)["error"])

    def test_late_command_is_refused_and_audited(self):
        n = len(self.node.sent)
        for path in ("/api/stop", "/api/estop"):
            with self.subTest(path=path):
                r, data = self.post(path, {"issued_at": time.time() - 10.2})      # туннель ожил через 10 с
                res = json.loads(data)
                self.assertEqual((r.status, res["ok"], res["sent"]), (409, False, False))
                self.assertGreater(res["age_s"], 10)
                self.assertIn("у робота", res["error"])
                r, _ = self.post(path, {"issued_at": time.time() + 30})          # часы страницы не сверены
                self.assertEqual(r.status, 409)
                r, _ = self.post(path, b"{}")                                     # без времени отправки
                self.assertEqual(r.status, 400)
        self.assertEqual(len(self.node.sent), n)                  # в граф не ушло ничего
        audit = [json.loads(x) for x in open(os.path.join(self.state, "audit.jsonl"), encoding="utf-8")]
        late = [a for a in audit if a.get("age_s") and a["age_s"] > 10]
        self.assertEqual({a["action"] for a in late}, {"stop", "estop_engage"})
        self.assertTrue(all(not a["ok"] and not a["sent"] for a in late))

    def test_estop_only_engage(self):
        n = len(self.node.sent)
        r, data = self.post("/api/estop", {"cmd": "release", "reason": "снять"})
        res = json.loads(data)
        kind, doc = self.node.sent[n]
        self.assertEqual((kind, doc["cmd"]), ("estop", "engage"))      # release в теле ничего не меняет
        self.assertEqual((r.status, res["ok"], res["state"]), (200, True, "estop"))
        self.assertLess(res["confirmed_s"], gateway.ESTOP_WAIT_S)
        for fn in (gateway.engage_message,):
            self.assertEqual(fn("x")["cmd"], "engage")

    def test_csrf_and_origin_on_post(self):
        n = len(self.node.sent)
        self.assertEqual(self.post("/api/stop", origin=False)[0].status, 403)          # без Origin
        self.assertEqual(self.post("/api/stop", headers={gateway.CSRF_HEADER: ""})[0].status, 403)
        self.assertEqual(self.post("/api/stop", headers={gateway.CSRF_HEADER: "0" * 32})[0].status, 403)
        other = self.auth.csrf_for(self.auth.new_session())          # CSRF другой сессии
        self.assertEqual(self.post("/api/stop", headers={gateway.CSRF_HEADER: other})[0].status, 403)
        self.assertEqual(self.post("/api/stop", headers={"Origin": "http://localhost:9999"})[0].status, 403)
        self.assertEqual(self.post("/api/stop", cookie=False)[0].status, 401)
        self.assertEqual(len(self.node.sent), n)

    def test_no_release_task_or_speed_routes(self):
        for path in ("/api/release", "/api/estop/release", "/api/task", "/api/cmd_vel", "/api/drive"):
            self.assertEqual(self.post(path)[0].status, 404, path)
        for m in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            self.assertEqual(self.request("/api/stop", method=m, origin=True)[0].status, 405, m)
        self.assertEqual(self.post("/api/stop", b"x" * 5000)[0].status, 413)
        self.assertEqual(self.post("/api/stop", b"[1]")[0].status, 400)
        self.assertEqual(self.post("/api/stop", b"{oops")[0].status, 400)

    def test_doctor_by_button(self):
        r, data = self.post("/api/doctor")
        res = json.loads(data)
        self.assertEqual((r.status, res["exit"], res["report"]["ok"]), (200, 0, True))
        calls = self.fake_run.calls
        r, data = self.post("/api/doctor")                   # раньше 60 с — прежний результат с возрастом
        self.assertTrue(json.loads(data)["cached"])
        self.assertEqual(self.fake_run.calls, calls)
        self.assertGreater(json.loads(self.request("/api/doctor")[1])["next_in_s"], 50)
        off = gateway.Doctor(["x"], 0.0, run=FakeRun())
        self.assertEqual(off.run()[0], 403)

    # -- поток

    def test_stream_hello_snapshot_tail_hb_resume(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(("GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nCookie: %s\r\n\r\n"
                   % (self.port, self.cookie)).encode())
        f = s.makefile("rb")
        head = f.readline()
        self.assertIn(b"200", head)
        while f.readline() not in (b"\r\n", b""):
            pass
        events = read_events(f, until=lambda ev: any(e.get("k") == "safety" for e in ev))
        self.assertEqual(events[0]["k"], "hello")
        self.assertEqual((events[0]["source"], events[0]["mode"], events[0]["format"]), ("двойник", "live",
                                                                                        stream.FORMAT))
        self.assertIn("geometry", events[0])
        self.lv.on_cmd("out", 0.12, 0.0, time.time())
        t = time.monotonic()
        tail = read_events(f, until=lambda ev: sum(e.get("k") == "hb" for e in ev) >= 2
                           and any(e.get("k") == "cmd" for e in ev))
        self.assertLess(time.monotonic() - t, 1.6)                   # hb — раз в 0,5 с
        last_id = tail_ids(tail)[-1]
        s.close()
        # переподключение браузера с Last-Event-ID — только пропущенное
        self.lv.operator("проверка", "", time.time())
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(("GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nCookie: %s\r\nLast-Event-ID: %s\r\n\r\n"
                   % (self.port, self.cookie, last_id)).encode())
        f = s.makefile("rb")
        while f.readline() not in (b"\r\n", b""):
            pass
        ev = read_events(f, until=lambda ev: any(e.get("k") == "operator" for e in ev))
        self.assertTrue(ev[0]["resume"])
        self.assertNotIn("safety", [e["k"] for e in ev if e["k"] != "hb"][1:2])
        s.close()
        # свой EventSource страницы после CLOSED (туннель упал) — номер в ?last=, тоже продолжение, а не снимок
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(("GET /api/stream?last=%s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nCookie: %s\r\n\r\n"
                   % (last_id, self.port, self.cookie)).encode())
        f = s.makefile("rb")
        while f.readline() not in (b"\r\n", b""):
            pass
        ev = read_events(f, until=lambda ev: len(ev) >= 1)
        self.assertTrue(ev[0].get("resume"))
        s.close()

    def test_new_stream_evicts_the_oldest(self):
        # застрявший туннель держит поток; новый поток — это живой человек: место отдаётся ему, а не 503
        socks = []
        try:
            for _ in range(self.hub.max_clients):
                s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
                s.sendall(("GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nCookie: %s\r\n\r\n"
                           % (self.port, self.cookie)).encode())
                s.recv(64)
                socks.append(s)
            self.assertEqual(self.hub.stats()["clients"], self.hub.max_clients)
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            s.sendall(("GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nCookie: %s\r\n\r\n"
                       % (self.port, self.cookie)).encode())
            self.assertIn(b"200", s.recv(64))
            socks.append(s)
            first, end = socks[0], time.monotonic() + 5
            first.settimeout(1.0)
            closed = False
            while time.monotonic() < end and not closed:   # вытесненный поток шлюз закрывает
                try:
                    closed = first.recv(65536) == b""
                except socket.timeout:
                    pass
            self.assertTrue(closed, "самый старый поток не закрыт")
            self.assertEqual(self.hub.stats()["clients"], self.hub.max_clients)
        finally:
            for s in socks:
                s.close()
        time.sleep(1.0)       # потоки замечают закрытие на ближайшем hb

    def test_stream_start_in_chunks(self):
        class W:
            def __init__(self):
                self.writes = []

            def write(self, b):
                self.writes.append(len(b))
        h = gateway.handler(self.gw).__new__(gateway.handler(self.gw))
        h.wfile = W()
        frames = [b"x" * 1000] * 300 + [b"y" * 200000]
        h.frames(frames)
        self.assertEqual(sum(h.wfile.writes), sum(map(len, frames)))
        self.assertLessEqual(max(h.wfile.writes[:-1]), gateway.CHUNK + 1000)   # не одним куском в 500 КБ
        self.assertGreater(len(h.wfile.writes), 4)


def decoded_buffer(hub):
    return [live._decode(f) for _, _, f in hub.buf]


def read_events(f, until, timeout=5.0):
    """События SSE из файла сокета, пока until(события) не станет истинным."""
    out, cur = [], {}
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        line = f.readline()
        if not line:
            break
        line = line.rstrip(b"\r\n")
        if not line:
            if "data" in cur:
                ev = json.loads(cur["data"])
                ev["_id"] = cur.get("id")
                out.append(ev)
                if until(out):
                    return out
            cur = {}
            continue
        name, _, value = line.decode("utf-8").partition(": ")
        cur[name] = value
    raise AssertionError("не дождались: %s" % [e.get("k") for e in out])


def tail_ids(events):
    return [e["_id"] for e in events if e.get("_id")]


@unittest.skipUnless(__import__("shutil").which("node"), "нет node: lib/live.js проверяет e2e_live.py в браузере")
class FrontLiveTest(unittest.TestCase):
    def test_live_source_ingest(self):
        import subprocess as sp
        uri = json.dumps((UI / "web" / "lib" / "live.js").as_uri())
        code = """import { LiveSource } from %s;
const s = new LiveSource();
const hello = (t, resume) => ({k: 'hello', t, format: 'neurobot-ui/1', source: 'двойник',
  ...(resume ? {resume: true} : {})});
s.ingest(hello(100));
s.ingest({k: 'safety', t: 99, state: 'slow', reasons: [], level: 0});
s.ingest({k: 'scan', t: 100.1, a0: -120, da: 1, r: [1]});
s.ingest({k: 'scan', t: 100.3, a0: -120, da: 1, r: [2]});
s.ingest({k: 'cmd', t: 100.4, topic: 'out', v: 0.1, w: 0});
s.ingest({k: 'hb', t: 1000});
const off = Math.round(s.now() - 1000);
const a = [s.tl.list('safety').length, s.tl.list('scan').length, s.tl.at('scan', 200).r[0], off];
s.ingest(hello(101, true));                       // resume — без снимка, накопленное остаётся
const b = s.tl.list('safety').length;
const order = s.all().map((m) => m.k).join(',');
s.ingest(hello(102, false));                      // новое подключение — заново со снимка
const c = s.tl.list('safety').length;
s.ingest({k: 'safety', t: 10, state: 'ok', reasons: [], level: 0});
s.ingest({k: 'safety', t: 5000, state: 'ok', reasons: [], level: 0});
s.ingest({k: 'hb', t: 5000});                     // часы робота ушли на 5000 с
s.trim();                                         // подробные виды старше часа браузеру не нужны
const d = s.tl.list('safety').map((m) => m.t);
if (s.bc) s.bc.close();
console.log(JSON.stringify([a, b, order, c, d, Number.isFinite(s.linkAge())]));""" % uri
        out = sp.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        a, b, order, c, d, age = json.loads(out.stdout)
        self.assertEqual(a, [1, 1, 2, 0])       # скан — только последний; часы робота — по hb
        self.assertEqual(b, 1)
        self.assertEqual(order, "safety,scan,cmd")      # по t: новой вкладке — в порядке времени
        self.assertEqual(c, 0)
        self.assertEqual(d, [5000])
        self.assertTrue(age)


    def test_browser_keeps_bounded_history(self):
        # вкладка открыта днями: у каждого вида свой срок; стоящая тревога и незакрытый жест — целиком, последнее
        # значение вида — всегда; новая вкладка получает этот срез
        import subprocess as sp
        uri = json.dumps((UI / "web" / "lib" / "live.js").as_uri())
        code = """import { LiveSource } from %s;
const s = new LiveSource();
s.ingest({k: 'hello', t: 0, format: 'neurobot-ui/1', source: 'двойник', boot: 'a'});
for (let t = 0; t <= 20 * 3600; t += 10) {
  s.ingest({k: 'stats', t, moving_s: 0});
  s.ingest({k: 'safety', t, state: 'slow', reasons: [], level: 0});
}
s.ingest({k: 'alarm', t: 100, key: 'old', code: 'old', prio: 2, on: true, text: 'x'});
s.ingest({k: 'alarm', t: 200, key: 'old', code: 'old', prio: 2, on: false, text: 'x'});
s.ingest({k: 'alarm', t: 300, key: 'stand', code: 'stand', prio: 1, on: true, text: 'y'});
s.ingest({k: 'gesture', t: 400, id: 'g1', kind: 'gesture', name: 'come'});
s.ingest({k: 'gesture', t: 401, id: 'g1', outcome: 'done'});
s.ingest({k: 'gesture', t: 500, id: 'g2', kind: 'gesture', name: 'go'});
s.ingest({k: 'units', t: 600, up: 1, total: 1});
s.ingest({k: 'hb', t: 20 * 3600});
s.trim();
const span = (k) => { const a = s.tl.list(k); return a.length ? (a[a.length - 1].t - a[0].t) / 3600 : null; };
const out = [span('stats'), span('safety'), s.tl.alarms(1e9).map((a) => a.key + (a.on ? '+' : '-')).join(','),
  s.tl.gestures(1e9).map((g) => g.id).join(','), s.tl.list('units').length, s.all().length];
// поток начат заново (шлюз перезапущен): история — с нового hello, и экран об этом знает
s.ingest({k: 'hello', t: 72001, format: 'neurobot-ui/1', source: 'двойник', boot: 'b'});
out.push(s.restart.why, s.tl.list('safety').length);
if (s.bc) s.bc.close();
console.log(JSON.stringify(out));""" % uri
        out = sp.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        stats_h, safety_h, alarms, gestures, units, total, why, after = json.loads(out.stdout)
        self.assertEqual((round(stats_h, 1), round(safety_h, 1)), (8.0, 1.0))   # агрегаты — 8 ч, подробное — час
        self.assertEqual(alarms, "stand+")                          # снятая давно — выброшена, стоящая — осталась
        self.assertEqual(gestures, "g2")                            # закрытый давно — выброшен, незакрытый — нет
        self.assertEqual(units, 1)                                  # последнее значение вида — всегда
        self.assertLess(total, 8 * 360 + 360 + 10)                  # срез для новой вкладки ограничен
        self.assertEqual((why, after), ("шлюз перезапущен", 0))


# ---- узел с подставным rclpy

class _Pub:
    def __init__(self, topic, qos):
        self.topic, self.qos, self.sent = topic, qos, []

    def publish(self, msg):
        self.sent.append(msg.data)


def fake_ros():
    """Модули rclpy и сообщений, которых хватает node.py (API ветки foxy)."""
    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        return m

    class Msg:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class QoSProfile:
        def __init__(self, **kw):
            self.kw = kw

    class Node:
        def __init__(self, name, **kw):
            self._name, self.init_kw, self.pubs, self.subs, self.timers = name, kw, [], [], []

        def create_publisher(self, cls, topic, qos):
            self.pubs.append(_Pub(topic, qos))
            return self.pubs[-1]

        def create_subscription(self, cls, topic, cb, qos, *, callback_group=None, event_callbacks=None, raw=False):
            # сигнатура ветки foxy (rclpy 1.0): raw — сообщение байтами, без разбора
            self.subs.append((cls, topic, cb, qos))
            self.raw = getattr(self, "raw", set()) | ({topic} if raw else set())

        def create_timer(self, period, cb):
            self.timers.append((period, cb))

        def get_name(self):
            return self._name

        def get_namespace(self):
            return "/"

        def count_subscribers(self, topic):
            return 1

        def get_publisher_names_and_types_by_node(self, name, ns):
            return [(p.topic, ["std_msgs/msg/String"]) for p in self.pubs]

    qos = mod("rclpy.qos", QoSProfile=QoSProfile, qos_profile_sensor_data="sensor_data",
              ReliabilityPolicy=types.SimpleNamespace(RELIABLE="reliable", BEST_EFFORT="best_effort"),
              DurabilityPolicy=types.SimpleNamespace(VOLATILE="volatile", TRANSIENT_LOCAL="transient_local"))
    parsed = []

    def deserialize_message(data, cls):
        parsed.append(data)
        return cls(header=types.SimpleNamespace(frame_id="laser_frame"), angle_min=-0.5, angle_increment=0.5,
                   ranges=[1.0, 2.0, 3.0], range_min=0.02, range_max=5.6)
    return {"rclpy": mod("rclpy", init=lambda **kw: None, ok=lambda: False, shutdown=lambda: None),
            "rclpy.node": mod("rclpy.node", Node=Node), "rclpy.qos": qos,
            "rclpy.serialization": mod("rclpy.serialization", deserialize_message=deserialize_message, parsed=parsed),
            "rclpy.executors": mod("rclpy.executors", SingleThreadedExecutor=object),
            "std_msgs": mod("std_msgs"), "std_msgs.msg": mod("std_msgs.msg", String=type("String", (Msg,), {})),
            "sensor_msgs": mod("sensor_msgs"), "sensor_msgs.msg": mod("sensor_msgs.msg", LaserScan=Msg),
            "tf2_msgs": mod("tf2_msgs"), "tf2_msgs.msg": mod("tf2_msgs.msg", TFMessage=Msg),
            "geometry_msgs": mod("geometry_msgs"),
            "geometry_msgs.msg": mod("geometry_msgs.msg", Twist=type("Twist", (Msg,), {}),
                                     TwistStamped=type("TwistStamped", (Msg,), {}))}


class NodeTest(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.dict(sys.modules, fake_ros())
        self.patch.start()
        sys.modules.pop("neurobot.ui.node", None)
        self.node_mod = importlib.import_module("neurobot.ui.node")
        from neurobot_deploy.config import Config
        self.cfg = Config({}, "/etc/neurobot/neurobot.env")
        self.hub = live.Hub()
        self.lv = live.Live(self.hub)
        self.node = self.node_mod.GatewayNode(self.cfg, self.lv)

    def tearDown(self):
        self.patch.stop()
        sys.modules.pop("neurobot.ui.node", None)

    def test_only_listed_publishers_volatile(self):
        n = self.node
        self.assertEqual(n.get_name(), "ui_gateway")
        self.assertFalse(n.get_name().startswith("neurobot_"))
        self.assertEqual(n.init_kw, {"use_global_arguments": False, "enable_rosout": False,
                                     "start_parameter_services": False})
        self.assertEqual([p.topic for p in n.pubs], list(live.PUBLISHERS))
        for p in n.pubs:
            self.assertEqual((p.qos.kw["reliability"], p.qos.kw["durability"]), ("reliable", "volatile"))
        self.assertEqual(n.publishers(), sorted(live.PUBLISHERS))
        subs = {topic for _, topic, _, _ in n.subs}
        # req — из safety_state, вход базы на экранах не нужен: подписок на Twist исполнителя и базы нет
        self.assertEqual(subs, {"/neurobot/safety_state", "/neurobot/perception_health", "/neurobot/gesture",
                                "/neurobot/events", "/cmd_vel", "/scan", "/tf_static"})
        self.assertFalse(subs & {"/neurobot/stop_now", "/neurobot/estop"})
        self.assertEqual(n.raw, {"/scan"})

    def test_stop_five_copies_engage_three_release_refused(self):
        doc = gateway.stop_message("кнопка")
        self.assertEqual(self.node.stop(doc), 1)
        stop, estop = self.node.pubs
        self.assertEqual(len(stop.sent), live.STOP_COPIES)
        self.assertEqual({json.loads(d)["id"] for d in stop.sent}, {doc["id"]})
        self.node.engage(gateway.engage_message("кнопка"))
        self.assertEqual(len(estop.sent), live.ESTOP_COPIES)
        with self.assertRaises(ValueError):
            self.node.engage(dict(gateway.engage_message("x"), cmd="release"))
        self.assertEqual(len(estop.sent), live.ESTOP_COPIES)

    def test_callbacks_feed_live(self):
        msg = sys.modules["std_msgs.msg"].String
        cb = {topic: fn for _, topic, fn, _ in self.node.subs}
        cb["/neurobot/safety_state"](msg(data=json.dumps({"state": "blocked", "reasons": ["obstacle: x"]})))
        cb["/neurobot/safety_state"](msg(data="не json"))
        tw = sys.modules["geometry_msgs.msg"].Twist
        lin = types.SimpleNamespace(x=0.1, y=0.0, z=0.0)
        ang = types.SimpleNamespace(x=0.0, y=0.0, z=0.2)
        cb["/cmd_vel"](tw(linear=lin, angular=ang))
        kinds = [live._decode(f)["k"] for _, _, f in self.hub.buf]
        self.assertEqual(kinds, ["safety", "alarm", "cmd"])
        # запрос исполнителя — из поля req safety_state
        cb["/neurobot/safety_state"](msg(data=json.dumps({"state": "slow", "reasons": [], "req": [0.2, 0.0]})))
        self.assertIn(("cmd", "req"), [(m["k"], m.get("topic")) for m in map(live._decode, (f for _, _, f in
                                                                                         self.hub.buf))])
        # сырой скан разбирается, только если пойдёт в поток (5 Гц из 10)
        parsed = sys.modules["rclpy.serialization"].parsed
        self.lv.on_tf_static([("base_link", "laser_frame", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))])
        cb["/scan"](b"raw-1")
        cb["/scan"](b"raw-2")                 # через миллисекунды — мимо
        self.assertEqual(parsed, [b"raw-1"])
        self.assertIsNotNone(self.hub.scan)

    def test_fast_dds_udp_only_unless_env_file_says_otherwise(self):
        import xml.etree.ElementTree as ET
        m = self.node_mod
        env = {}
        self.assertEqual(m.dds_environ(env), m.FASTDDS_UDP)          # Foxy и Jazzy: Fast DDS по умолчанию
        self.assertEqual(env["FASTRTPS_DEFAULT_PROFILES_FILE"], m.FASTDDS_UDP)
        self.assertIsNone(m.dds_environ({"RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"}))
        lab = {"RMW_IMPLEMENTATION": "rmw_fastrtps_cpp", "FASTRTPS_DEFAULT_PROFILES_FILE": "/etc/lab.xml"}
        self.assertIsNone(m.dds_environ(lab))                         # свой профиль env-файла не трогаем
        self.assertEqual(lab["FASTRTPS_DEFAULT_PROFILES_FILE"], "/etc/lab.xml")
        ns = {"p": "http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles"}
        root = ET.parse(m.FASTDDS_UDP).getroot()
        part = root.find("p:participant", ns)
        self.assertEqual(part.get("is_default_profile"), "true")
        self.assertEqual(part.find("p:rtps/p:useBuiltinTransports", ns).text, "false")   # без SHM
        self.assertEqual([t.text for t in root.findall("p:transport_descriptors/p:transport_descriptor/p:type", ns)],
                         ["UDPv4"])

    def test_single_create_publisher_in_package(self):
        hits = []
        for path in UI.rglob("*.py"):
            if "tests" in path.parts:
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "create_publisher(" in line:
                    hits.append((path.name, line.strip()))
        self.assertEqual(hits, [("node.py", "self._pubs = {topic: self.create_publisher(String, topic, volatile) for "
                                            "topic in live.PUBLISHERS}")])


if __name__ == "__main__":
    unittest.main()
