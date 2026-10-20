"""Живой шлюз пульта: HTTP на 127.0.0.1 поверх live.Hub и узла rclpy (node.py). Python 3.8, только stdlib.

Вход (PLAN §0а п. 6, §3.3): шлюз слушает только 127.0.0.1, с ноутбука — SSH-туннель (`ssh -L`) на любой локальный
порт. Токен (128 бит) — в StateDirectory шлюза (0700, свой пользователь: агенту его не прочитать), переживает
рестарт. `sudo neurobot ui-url` кладёт в codes/ одноразовый код (файл — его sha256 и срок) и печатает адрес
/login?code=…; шлюз меняет код на сессию — случайный идентификатор со сроком на сервере (SESSION_TTL_S, файл в
sessions/) — в cookie `HttpOnly; SameSite=Strict` (имя — с отпечатком токена: у двух роботов на разных портах
туннеля cookie не путаются) и уводит на / без кода в адресе. Cookie не различают порты: браузер покажет её любому
HTTP-сервису на 127.0.0.1 и localhost ноутбука — поэтому сессия недолгая и отзывается одна, удалением её файла.

Проверки каждого запроса: Host — локальное имя с любым портом (туннель), Origin, если есть, — тот же адрес, что Host
(DNS rebinding); /api/* — только с живой сессией (иначе 401). POST — ещё и с Origin, равным адресу страницы, и
заголовком X-Neurobot-CSRF (HMAC токена от сессии): чужая страница, даже с localhost на другом порту, без него не
пройдёт — свой заголовок требует предварительного OPTIONS, а его шлюз не разрешает (405).

Команды — со временем отправки по часам робота (issued_at; браузер знает их по hb): старше CMD_MAX_AGE_S — отказ 409 и
строка аудита. Запоздавший на минуты через ожившее TCP СТОП оборвал бы уже другой манёвр, а защёлка заперла бы робот
до sudo у робота; браузер сам бросает запрос через 2 с и пишет «нет ответа». СТОП подтверждает safety_gate — рост
counters.stop_now в safety_state, защёлку — state = estop; «есть подписчик» — не подтверждение: им может быть
кто угодно в сети лаборатории.

API: GET /api/meta, /api/stream (SSE: hello, снимок, буфер, хвост; hb раз в HB_S), /api/bundle («Запись», если
задан бандл), /api/doctor (последний doctor --json); POST /api/stop, /api/estop (только engage), /api/doctor (по
кнопке, не чаще раза в NEUROBOT_UI_DOCTOR_S). Снятия защёлки, задач, скоростей нет: маршрута нет — 404. Каждое
действие и вход — строка в audit.jsonl (StateDirectory) и в журнале. Токен, код и сессия в журнал не пишутся.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

from neurobot.ui import alarms as A
from neurobot.ui import catalog, live, robot, server, stream

log = logging.getLogger("neurobot-ui")

COOKIE_PREFIX = "nbui_"
SESSION_TTL_S = 12 * 3600   # вход — на смену; срок держит сервер, а не только Max-Age в браузере
CSRF_HEADER = "X-Neurobot-CSRF"
MAX_BODY = 4096
HB_S = 0.5                  # heartbeat SSE: «нет связи» браузер показывает после 1,5 с тишины (PLAN §7 U8: ≤ 2 с)
RETRY_MS = 1000             # переподключение EventSource
CMD_MAX_AGE_S = 1.0         # команда шла дольше (issued_at → шлюз) — не исполняется: связь плохая, стоп — у робота
STOP_WAIT_S = 0.5           # ждать роста counters.stop_now (safety_state — 5 Гц)
ESTOP_WAIT_S = 0.8          # ждать state = estop после engage (safety_gate подтверждает ≤ 0,3 с — README safety)
REASON_MAX = 120
DEFAULT_REASON = "кнопка пульта"
CHUNK = 64 * 1024           # начало потока — кусками: без копии всего буфера на каждое подключение
STREAM_SNDBUF = 64 * 1024   # малый буфер сокета потока: застрявший клиент быстрее упирается в тайм-аут записи
STREAM_WRITE_S = 10.0       # запись в поток не проходит столько — поток закрывается
_SID = re.compile(r"[A-Za-z0-9_-]{16,64}")


def _hmac(token: str, what: bytes) -> str:
    return hmac.new(token.encode("ascii"), what, hashlib.sha256).hexdigest()


class Auth:
    """Токен в каталоге состояния, одноразовые коды входа, сессии и CSRF.

    Сессия — случайный идентификатор на каждый вход (redeem → new_session) со сроком на сервере: файл
    sessions/<sha256 идентификатора> с {exp, created}. Переживает рестарт шлюза; отзыв одной сессии — удалить её файл
    (время входа — в created и в аудите), всех — каталог sessions/ целиком; токен для этого менять не нужно."""

    TOKEN = "token"
    CODES = "codes"
    SESSIONS = "sessions"

    def __init__(self, state_dir: str, clock: Callable[[], float] = time.time, ttl_s: float = SESSION_TTL_S):
        self.dir = state_dir
        self.clock = clock
        self.ttl_s = ttl_s
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        if os.stat(state_dir).st_mode & 0o077:
            os.chmod(state_dir, 0o700)          # каталог наш: токен не должен быть виден группе и прочим
        for sub in (self.CODES, self.SESSIONS):
            os.makedirs(os.path.join(state_dir, sub), mode=0o700, exist_ok=True)
        self.token = self._token()
        self.cookie_name = COOKIE_PREFIX + hashlib.sha256(self.token.encode()).hexdigest()[:8]
        self._purge(self.SESSIONS, self.clock())

    def _token(self) -> str:
        path = os.path.join(self.dir, self.TOKEN)
        try:
            with open(path, encoding="ascii") as f:
                token = f.read().strip()
            if len(token) == 32 and all(c in "0123456789abcdef" for c in token):
                return token
        except (OSError, UnicodeDecodeError):
            pass
        token = secrets.token_hex(16)
        _write_private(path, token + "\n")
        return token

    # -- сессии

    def _session_path(self, sid: str) -> str:
        return os.path.join(self.dir, self.SESSIONS, hashlib.sha256(sid.encode("ascii")).hexdigest())

    def new_session(self) -> str:
        sid = secrets.token_urlsafe(24)
        now = self.clock()
        _write_private(self._session_path(sid), json.dumps({"exp": round(now + self.ttl_s, 3),
                                                            "created": round(now, 3)}))
        return sid

    def session_of(self, header: Optional[str]) -> Optional[str]:
        """Идентификатор живой сессии из заголовка Cookie или None (нет, чужой, истёк, отозван)."""
        for part in (header or "").split(";"):
            name, sep, value = part.strip().partition("=")
            if not sep or name != self.cookie_name:
                continue
            value = value.strip()
            if not _SID.fullmatch(value):
                return None
            path = self._session_path(value)
            try:
                with open(path, encoding="utf-8") as f:
                    exp = json.load(f).get("exp")
            except (OSError, ValueError, AttributeError):
                return None
            if not isinstance(exp, (int, float)) or exp < self.clock():
                try:
                    os.remove(path)
                except OSError:
                    pass
                return None
            return value
        return None

    def cookie_ok(self, header: Optional[str]) -> bool:
        return self.session_of(header) is not None

    def set_cookie(self, sid: str) -> str:
        return "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (self.cookie_name, sid, int(self.ttl_s))

    @staticmethod
    def fingerprint(sid: str) -> str:
        """Отпечаток сессии для аудита (по нему находится её файл: sessions/<sha256>), сама сессия в журнал не идёт."""
        return hashlib.sha256(sid.encode("ascii")).hexdigest()[:12]

    def csrf_for(self, sid: str) -> str:
        return _hmac(self.token, b"csrf:" + sid.encode("ascii"))[:32]

    def csrf_ok(self, value: Optional[str], sid: Optional[str]) -> bool:
        return bool(value) and bool(sid) and hmac.compare_digest(value.strip().encode(), self.csrf_for(sid).encode())

    # -- коды входа

    def redeem(self, code: str) -> bool:
        """Одноразовый код ui-url: файл codes/<sha256 кода> с {"exp"}; удаляется при первом предъявлении."""
        now = self.clock()
        self._purge(self.CODES, now)
        self._purge(self.SESSIONS, now)
        if not code or len(code) > 128:
            return False
        path = os.path.join(self.dir, self.CODES, hashlib.sha256(code.encode("utf-8")).hexdigest())
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            os.remove(path)
        except (OSError, ValueError):
            return False
        exp = doc.get("exp") if isinstance(doc, dict) else None
        return isinstance(exp, (int, float)) and exp >= now

    def _purge(self, sub: str, now: float) -> None:
        d = os.path.join(self.dir, sub)
        for name in os.listdir(d) if os.path.isdir(d) else []:
            path = os.path.join(d, name)
            try:
                with open(path, encoding="utf-8") as f:
                    exp = json.load(f).get("exp", 0)
                if exp < now:
                    os.remove(path)
            except (OSError, ValueError, AttributeError):
                continue


def _write_private(path: str, text: str) -> None:
    """Файл 0600 атомарно (временный, fsync, rename)."""
    tmp = "%s.%s.tmp" % (path, secrets.token_hex(4))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Audit:
    """Действия оператора: строка JSON в audit.jsonl (StateDirectory, ротация по размеру) и в журнале юнита."""

    def __init__(self, path: str, max_bytes: int = 1 << 20):
        self.path = path
        self.max_bytes = max_bytes
        self.lock = threading.Lock()

    def write(self, rec: dict) -> None:
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        log.info("действие: %s", line)
        with self.lock:
            try:
                if os.path.exists(self.path) and os.path.getsize(self.path) > self.max_bytes:
                    os.replace(self.path, self.path + ".1")
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as e:
                log.error("аудит не записан в %s: %s", self.path, e)


class Doctor:
    """neurobot doctor --json по кнопке: один за раз, не чаще раза в min_s (0 — выключен: во время замеров). Пульт сам
    doctor не запускает (PLAN §0а п. 6) — только по просьбе оператора; результат хранится с возрастом."""

    def __init__(self, cmd: Sequence[str], min_s: float, timeout: float = 120.0, run: Callable = subprocess.run,
                 clock: Callable[[], float] = time.time):
        self.cmd = list(cmd)
        self.min_s = min_s
        self.timeout = timeout
        self._run = run
        self.clock = clock
        self.lock = threading.Lock()
        self.result: Optional[dict] = None

    def last(self) -> dict:
        now = self.clock()
        out = {"enabled": self.min_s > 0, "running": self.lock.locked(), "min_s": self.min_s}
        if self.result:
            out.update(self.result, age_s=round(now - self.result["t"], 1),
                       next_in_s=round(max(0.0, self.result["t"] + self.min_s - now), 1))
        return out

    def run(self) -> Tuple[int, dict]:
        if self.min_s <= 0:
            return 403, dict(self.last(), error="doctor из пульта выключен (NEUROBOT_UI_DOCTOR_S=0: идут замеры)")
        if self.result and self.clock() - self.result["t"] < self.min_s:
            return 200, dict(self.last(), cached=True)
        if not self.lock.acquire(blocking=False):
            return 409, dict(self.last(), error="doctor уже идёт")
        try:
            t0 = self.clock()
            try:
                r = self._run(self.cmd, capture_output=True, text=True, timeout=self.timeout)
                report = json.loads(r.stdout) if r.stdout.strip() else None
                res = {"t": t0, "took_s": round(self.clock() - t0, 1), "exit": r.returncode, "report": report}
                if report is None:
                    res["error"] = (r.stderr or "").strip()[-300:] or "doctor ничего не вывел"
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                res = {"t": t0, "took_s": round(self.clock() - t0, 1), "exit": None, "report": None, "error": str(e)}
            self.result = res
        finally:
            self.lock.release()
        return 200, self.last()


def _age(body: dict, now: float) -> Tuple[Optional[float], Optional[str]]:
    """Возраст команды по issued_at (часы робота) и причина отказа: нет времени, шла дольше CMD_MAX_AGE_S, из
    будущего (часы страницы не сверены с роботом)."""
    issued = body.get("issued_at")
    if not isinstance(issued, (int, float)) or isinstance(issued, bool) or issued != issued:
        return None, "нет issued_at: команда без времени отправки не исполняется — обновите страницу пульта"
    age = round(now - float(issued), 3)
    if age > CMD_MAX_AGE_S:
        return age, ("команда шла %.1f с (дольше %g с) — не исполнена: связь с роботом плохая, останавливайте у "
                     "робота" % (age, CMD_MAX_AGE_S))
    if age < -CMD_MAX_AGE_S:
        return age, "часы страницы не сверены с роботом (%.1f с) — обновите страницу пульта" % age
    return age, None


def stop_message(reason: str, t: Optional[float] = None) -> dict:
    """Стоп движения: тот же контракт, что robot_stop агента (README safety, /neurobot/stop_now)."""
    return {"id": secrets.token_hex(16), "by": live.BY, "reason": reason,
            "t": round(time.time() if t is None else t, 3)}


def engage_message(reason: str, t: Optional[float] = None) -> dict:
    """Поставить защёлку: команда всегда engage — другой в коде шлюза нет (снимает только человек у робота)."""
    return {"cmd": "engage", "by": live.BY, "reason": reason, "id": secrets.token_hex(16),
            "t": round(time.time() if t is None else t, 3)}


def _reason(body: dict) -> str:
    r = body.get("reason") if isinstance(body.get("reason"), str) else ""
    r = " ".join(r.split())[:REASON_MAX]
    return r or DEFAULT_REASON


class Gateway(server.Static):
    """Состояние шлюза: поток (hub, live), узел (actions: stop, engage, publishers, name), вход, аудит, doctor,
    необязательный бандл «Записи»."""

    def __init__(self, hub: live.Hub, lv: live.Live, actions, auth: Auth, source: str, run: str,
                 audit: Optional[Audit] = None, doctor: Optional[Doctor] = None, bundle: Optional[str] = None,
                 web: str = server.WEB, clock: Callable[[], float] = time.time):
        super().__init__(web)
        if source not in stream.SOURCES[:2]:
            raise ValueError("источник живого режима — робот или двойник, не %r" % source)
        self.hub, self.live, self.actions, self.auth = hub, lv, actions, auth
        self.source, self.run_label = source, run
        self.audit = audit
        self.doctor = doctor
        self.bundle = server.Bundle(bundle) if bundle else None
        self.clock = clock
        self.started = clock()
        self.stopping = threading.Event()
        self.catalog = catalog.Registry()

    def head(self) -> dict:
        """Поля hello живого потока (Hub добавит k, t, format, active, boot)."""
        return {"source": self.source, "run": self.run_label, "mode": "live", "geometry": robot.geometry(),
                "alarms": A.dictionary(), "started": round(self.started, 3), "node": self.actions.name,
                "publishers": list(live.PUBLISHERS), "ref": "t_end"}

    def meta(self, sid: str) -> dict:
        """t — часы робота: по ним страница ставит issued_at командам, пока живого потока нет (режим «Запись»)."""
        return {"mode": "live", "modes": ["live"] + (["replay"] if self.bundle else []), "source": self.source,
                "run": self.run_label, "format": stream.FORMAT, "csrf": self.auth.csrf_for(sid),
                "t": round(self.clock(), 3), "hb_s": HB_S, "cmd_max_age_s": CMD_MAX_AGE_S, "node": self.actions.name,
                "publishers": list(live.PUBLISHERS), "boot": self.hub.boot, "started": round(self.started, 3),
                "doctor_s": self.doctor.min_s if self.doctor else 0,
                "bundle": self.bundle.name if self.bundle else None,
                "runs": self.bundle.runs if self.bundle else []}

    def health(self) -> dict:
        """/healthz без входа (doctor): коротко — проба читает начало тела; буфер — для замеров памяти."""
        st = self.hub.stats()
        return {"ok": True, "node": self.actions.name, "publishers": self.actions.publishers(),
                "clients": st["clients"], "uptime_s": round(self.clock() - self.started, 1),
                "buffer_kib": round(st["bytes"] / 1024.0), "buffer_s": st["span_s"]}

    def stop(self, body: dict) -> Tuple[int, dict]:
        """СТОП: 200 — safety_gate принял (counters.stop_now вырос), 202 — отправлен, подтверждения нет, 503 — тему
        никто не слушает, 409 — команда устарела в пути, 400 — без issued_at."""
        reason = _reason(body)
        now = self.clock()
        msg = stop_message(reason, now)
        age, late = _age(body, now)
        if late:
            res = {"ok": False, "sent": False, "id": msg["id"], "subs": 0, "t": msg["t"], "age_s": age, "error": late}
            self._audit("stop", msg, res)
            return (400 if age is None else 409), res
        mark = self.live.stop_mark()
        t0 = time.monotonic()
        subs = self.actions.stop(msg)
        self.live.operator("СТОП движения", reason, msg["t"])
        taken = self.live.stop_taken(mark)
        while subs and mark is not None and not taken and time.monotonic() - t0 < STOP_WAIT_S:
            time.sleep(0.02)
            taken = self.live.stop_taken(mark)
        res = {"ok": taken, "sent": subs > 0, "id": msg["id"], "subs": subs, "t": msg["t"], "age_s": age}
        if taken:
            res["confirmed_s"] = round(time.monotonic() - t0, 3)
        elif not subs:
            res["error"] = "никто не слушает %s: safety_gate не запущен — останавливайте у робота" % live.STOP_TOPIC
        elif mark is None:
            res["error"] = "отправлен; safety_gate не сообщает счётчик стопов — подтвердить нечем, смотрите на робот"
        else:
            res["error"] = "отправлен, но safety_gate не подтвердил за %g с — смотрите на робот" % STOP_WAIT_S
        self._audit("stop", msg, res)
        return (200 if taken else 202 if subs else 503), res

    def engage(self, body: dict) -> Tuple[int, dict]:
        reason = _reason(body)
        now = self.clock()
        msg = engage_message(reason, now)
        age, late = _age(body, now)
        if late:
            res = {"ok": False, "sent": False, "id": msg["id"], "subs": 0, "t": msg["t"], "age_s": age,
                   "state": self.live.state(), "error": late}
            self._audit("estop_engage", msg, res)
            return (400 if age is None else 409), res
        t0 = time.monotonic()
        subs = self.actions.engage(msg)
        self.live.operator("Поставить защёлку", reason, msg["t"])
        state = self.live.state()
        while subs and state != "estop" and time.monotonic() - t0 < ESTOP_WAIT_S:
            time.sleep(0.02)
            state = self.live.state()
        res = {"ok": bool(subs) and state == "estop", "sent": subs > 0, "id": msg["id"], "subs": subs, "t": msg["t"],
               "age_s": age, "state": state}
        if state == "estop":
            res["confirmed_s"] = round(time.monotonic() - t0, 3)
        elif not subs:
            res["error"] = "никто не слушает %s: safety_gate не запущен" % live.ESTOP_TOPIC
        else:
            res["error"] = "safety_gate не подтвердил защёлку за %g с" % ESTOP_WAIT_S
        self._audit("estop_engage", msg, res)
        return 200 if res["ok"] else 503, res

    def login(self, sid: str) -> None:
        if self.audit:
            self.audit.write({"t": round(self.clock(), 3), "action": "login", "session": self.auth.fingerprint(sid),
                              "ttl_s": self.auth.ttl_s, "ok": True})

    def _audit(self, action: str, msg: dict, res: dict) -> None:
        if self.audit:
            self.audit.write({"t": msg["t"], "action": action, "id": msg["id"], "by": live.BY,
                              "reason": msg["reason"], "subs": res["subs"], "ok": res["ok"], "sent": res["sent"],
                              "age_s": res.get("age_s"), "confirmed_s": res.get("confirmed_s"),
                              "state": res.get("state"), "error": res.get("error")})


def _same_origin(headers, required: bool) -> bool:
    """Origin: у GET может не быть; если есть — тот же адрес, что Host (туннель на любой локальный порт)."""
    origin = headers.get("Origin")
    if origin is None:
        return not required
    host = (headers.get("Host") or "").strip().lower()
    return server.host_ok(host) and origin.strip().lower() == "http://" + host


def handler(gw: Gateway):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "neurobot-ui"
        sys_version = ""
        timeout = 60            # простаивающее соединение и мёртвый туннель не держат поток дольше

        def log_message(self, fmt, *args):   # noqa: A003 — только 4xx и 5xx, путь без запроса (код входа не пишем)
            if len(args) >= 2 and str(args[1])[:1] in ("4", "5"):
                line = str(args[0]).split("?", 1)[0]
                sys.stderr.write("neurobot-ui: %s %s\n" % (args[1], line))

        def send(self, code: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", gw.csp)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json(self, code: int, doc: dict) -> None:
            self.send(code, json.dumps(doc, ensure_ascii=False).encode("utf-8"), "application/json")

        def deny(self, code: int, text: str) -> None:
            self.send(code, text.encode("utf-8"), "text/plain; charset=utf-8")

        def authed(self) -> bool:
            self.sid = gw.auth.session_of(self.headers.get("Cookie"))
            return self.sid is not None

        def do_GET(self):   # noqa: N802
            if not server.host_ok(self.headers.get("Host")) or not _same_origin(self.headers, False):
                return self.deny(403, "запрещено: пульт открывается только с локального адреса")
            url = urlsplit(self.path)
            path = url.path
            if path == "/healthz":
                return self.json(200, gw.health())
            if path == "/login":
                code = (parse_qs(url.query).get("code") or [""])[0]
                if not gw.auth.redeem(code):
                    return self.deny(401, "ссылка входа недействительна или уже использована: на роботе — "
                                          "sudo neurobot ui-url")
                sid = gw.auth.new_session()
                gw.login(sid)
                return self.send(303, b"", "text/plain; charset=utf-8",
                                 {"Location": "/", "Set-Cookie": gw.auth.set_cookie(sid)})
            if path.startswith("/api/"):
                if not self.authed():
                    return self.json(401, {"error": "нужен вход: на роботе — sudo neurobot ui-url"})
                if path == "/api/catalog":
                    return self.json(200, gw.catalog.snapshot())
                if path == "/api/meta":
                    return self.json(200, gw.meta(self.sid))
                if path == "/api/stream" and self.command == "GET":     # ?last= — номер события для resume
                    return self.stream()
                if path == "/api/bundle" and gw.bundle:
                    if "gzip" in (self.headers.get("Accept-Encoding") or ""):
                        return self.send(200, gw.bundle.gz, "application/json", {"Content-Encoding": "gzip"})
                    return self.send(200, gw.bundle.raw, "application/json")
                if path == "/api/doctor" and gw.doctor:
                    return self.json(200, gw.doctor.last())
                return self.deny(404, "нет такого адреса")
            got = gw.static(path)
            if got is None:
                return self.deny(404, "нет такого файла")
            return self.send(200, got[0], got[1])

        do_HEAD = do_GET    # noqa: N815

        def do_POST(self):   # noqa: N802
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                self.close_connection = True        # тело не читаем: соединение дальше не годится
                return self.json(413, {"error": "тело запроса больше %d байт" % MAX_BODY})
            raw = self.rfile.read(length) if length else b""
            if not server.host_ok(self.headers.get("Host")) or not _same_origin(self.headers, True):
                return self.deny(403, "запрещено: команда только со страницы пульта")
            if not self.authed():
                return self.json(401, {"error": "нужен вход: на роботе — sudo neurobot ui-url"})
            if not gw.auth.csrf_ok(self.headers.get(CSRF_HEADER), self.sid):
                return self.json(403, {"error": "нет заголовка %s: команда не со страницы пульта" % CSRF_HEADER})
            try:
                body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except (UnicodeDecodeError, ValueError):
                body = None
            if not isinstance(body, dict):
                return self.json(400, {"error": "тело — объект JSON"})
            path = urlsplit(self.path).path
            if path in ("/api/catalog/sources", "/api/catalog/disconnect"):
                return self.json(*catalog.mutate(gw.catalog, path, body))
            if path == "/api/stop":
                return self.json(*gw.stop(body))
            if path == "/api/estop":
                return self.json(*gw.engage(body))
            if path == "/api/doctor" and gw.doctor:
                return self.json(*gw.doctor.run())
            return self.json(404, {"error": "нет такой команды: пульт умеет только «СТОП движения» и «Поставить "
                                            "защёлку»"})

        def refuse(self):
            self.deny(405, "метод не поддерживается")

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = refuse   # noqa: N815

        def frames(self, frames: Sequence[bytes]) -> None:
            """Кадры кусками ≈CHUNK. Начало потока — 1–5 МБ (буфер 15 мин); b"".join всего списка и его копия с retry
            на каждое подключение оседали в аренах malloc потоков, и память шлюза росла с каждым переподключением
            [замер ревью: 48 → 81 МиБ anon]."""
            part: List[bytes] = []
            size = 0
            for f in frames:
                part.append(f)
                size += len(f)
                if size >= CHUNK:
                    self.wfile.write(b"".join(part))
                    part, size = [], 0
            if part:
                self.wfile.write(b"".join(part))

        def stream(self) -> None:
            me, old = gw.hub.join()
            if old is not None:
                log.warning("потоков пульта больше %d: самый старый (№%d) закрыт — его туннель, вероятно, застрял",
                            gw.hub.max_clients, old.n)
            try:
                try:        # застрявший клиент упирается в малый буфер, запись — в тайм-аут, поток закрывается
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, STREAM_SNDBUF)
                except OSError:
                    pass
                self.connection.settimeout(STREAM_WRITE_S)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                # продолжить с номера: заголовок EventSource или ?last= (свой EventSource страницы после CLOSED)
                last = self.headers.get("Last-Event-ID") or (parse_qs(urlsplit(self.path).query).get("last") or [""])[0]
                start, cursor, gen = gw.hub.attach(gw.head(), last[:64], gw.clock())
                self.wfile.write(("retry: %d\n\n" % RETRY_MS).encode())
                self.frames(start)
                del start
                next_hb = time.monotonic() + HB_S
                while not gw.stopping.is_set():
                    out, cursor, gen = gw.hub.wait(cursor, gen, max(0.05, next_hb - time.monotonic()), me)
                    if out is None:
                        break           # отстал от буфера или вытеснен: браузер переподключится и получит снимок
                    if time.monotonic() >= next_hb:
                        out.append(stream.sse({"k": "hb", "t": round(gw.clock(), 3)}))
                        next_hb = time.monotonic() + HB_S
                    if out:
                        self.frames(out)
            except (OSError, socket.timeout):
                pass                    # браузер закрыл вкладку, туннель оборвался, запись не прошла
            finally:
                gw.hub.leave(me)
    return Handler


class Server(ThreadingHTTPServer):
    # очередь соединений: браузер открывает ≈25 модулей страницы разом, у socketserver по умолчанию 5 — лишние SYN
    # теряются и страница минутами висит на «Загрузке пульта» [опыт e2e]
    request_queue_size = 128
    daemon_threads = True

    def shutdown(self):
        if hasattr(self, "catalog"):
            self.catalog.close()
        super().shutdown()

    def server_close(self):
        if hasattr(self, "catalog"):
            self.catalog.close()
        super().server_close()


def make_server(gw: Gateway, port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    if host not in server.LOOPBACK:
        raise ValueError("шлюз слушает только 127.0.0.1 или ::1, не %r: доступ снаружи — через SSH-туннель" % host)
    cls = Server
    if ":" in host:
        class V6(Server):
            address_family = socket.AF_INET6
        cls = V6
    srv = cls((host, port), handler(gw))
    srv.daemon_threads = True
    srv.catalog = gw.catalog
    return srv


def units_rows(show: Dict[str, Dict[str, str]], plan: Sequence[Tuple[str, str]]) -> List[dict]:
    """systemctl show → строки юнитов для live.on_units (в порядке запуска; не установленный — inactive)."""
    rows = []
    for comp, unit in plan:
        p = show.get(unit, {})
        try:
            restarts = int(p.get("NRestarts") or 0)
        except ValueError:
            restarts = 0
        rows.append({"comp": comp, "unit": unit, "active": p.get("ActiveState") or "inactive",
                     "sub": p.get("SubState") or "dead", "restarts": restarts})
    return rows
