"""Живой шлюз пульта без ROS (docs/ui/PLAN.md §0а, П2): из сообщений графа — поток `neurobot-ui/1`, кольцевой буфер
15 мин и раздача клиентам SSE. Узел rclpy (node.py) только подписывается и зовёт сюда, HTTP — gateway.py. Python 3.8,
только stdlib: логика проверяется тестами без ROS под 3.8 и 3.12.

Что считает шлюз, а не браузер (одни стенные часы робота, U2 §3 п. 4):
- safety — состояние safety_gate как есть: причины с запятой в дробях (fmt.reason), уровень тревоги, req и out —
  {v, w}; при смене состояния, причин или защёлки — сразу, иначе не чаще SAFETY_EVERY_S (safety_gate шлёт 5 Гц);
- тревоги — тем же AlarmTracker, что «Запись»: причины safety_state, его молчание дольше SILENT_S (P1), юниты, камера;
- cmd — req (поле req safety_state, 5 Гц: своей подписки на Twist исполнителя нет) и out (/cmd_vel — она нужна и для
  задержки жеста): при изменении (шаг 0,01, не чаще 10 Гц; переход в ноль — сразу) и раз в 1 с;
- жест — /neurobot/gesture → первая ненулевая /cmd_vel → исход. Задержка — от t_end, конца жеста по perception
  (ref t_end): t0 — истины генератора жестов — на роботе нет. Меряется, только если в t_end робот стоял (U2 §4 п. 3):
  иначе первая команда может быть чужой;
- скан — бины 1° пеленга в базе робота (поза лидара из /tf_static), не чаще 5 Гц, мимо буфера;
- ресурсы — RSS и CPU процессов юнитов из /proc (cgroup процесса → юнит), юниты — systemctl show;
- stats раз в 10 с — доли состояний safety_gate, время в движении, K1 по последним K1_KEEP жестам (тренды — агрегатами);
- стоп пульта подтверждает safety_gate: счётчик counters.stop_now в следующем safety_state (stop_mark, stop_taken).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from neurobot.ui import alarms as A
from neurobot.ui import fmt, robot, stream

log = logging.getLogger("neurobot-ui")

# Всё, во что пишет шлюз: константа (PLAN §5). Тест, wait-graph ui и doctor сверяют издателей узла с этим списком;
# safety_gate слушает обе темы под этими именами. Задач, скоростей и снятия защёлки в коде шлюза нет.
STOP_TOPIC = "/neurobot/stop_now"
ESTOP_TOPIC = "/neurobot/estop"
PUBLISHERS = (STOP_TOPIC, ESTOP_TOPIC)
ROS_INFRA = ("/parameter_events", "/rosout")    # издатели, которые rclpy заводит сам (rosout шлюз выключает)
STOP_COPIES = 5             # как robot_stop MCP-сервера: копии с одним id — один стоп
ESTOP_COPIES = 3            # как neurobot estop engage
BY = "ui:operator"          # одна роль (PLAN §0а п. 2); самозаявленная метка, как by у агента и CLI

SAFETY_EVERY_S = 0.5        # safety без перемен — не чаще 2 Гц
SILENT_S = 1.0              # safety_state молчит дольше — P1 «безопасность неизвестна» (PLAN §4)
START_GRACE_S = 3.0         # после старта шлюза граф DDS собирается: молчание до первого safety_state — не сразу
CMD_EVERY_S = 0.1           # cmd: изменение — не чаще 10 Гц
KEEPALIVE_S = 1.0           # cmd — раз в 1 с, пока писатель жив (как в «Записи»)
HEALTH_EVERY_S = 5.0
CAMERA_LOST_S = 1.0         # кадр perception старше — камера молчит (P2), как в «Записи»
SCAN_EVERY_S = 0.2          # скан — не чаще 5 Гц
SCAN_BIN_DEG = 1.0
TF_WAIT_S = 10.0            # нет позы лидара столько после старта — строка в журнале
STATS_EVERY_S = 10.0        # тренды — одна точка в 10 с (PLAN §0а п. 6)
UNITS_EVERY_S = 10.0
K1_WINDOW = 20
K1_KEEP = 1000              # K1 «с запуска» — по последним стольким жестам: шлюз работает неделями, список не растёт
CMD_WAIT_S = 3.0            # жест с манёвром, а первой команды нет столько после распознавания — «нет реакции»
END_ZERO_S = 0.5            # выход снова ноль столько — манёвр кончился
MANEUVER_MAX_S = 20.0       # исход не ясен столько после распознавания — закрываем
MOVING_EPS = 1e-3
STATES = ("ok", "slow", "blocked", "estop")
NO_MANEUVER = (None, "unknown", "none")
CUT = ("estop", "blocked", "safety_lost")


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def quantiles(values: Sequence[float]) -> dict:
    """p50, p95, max с линейной интерполяцией — как neurobot/sim/runs/metrics.quantiles (на робот он не ставится)."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return {"n": 0}

    def q(p: float) -> float:
        k = (len(v) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(v) - 1)
        return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 4)
    return {"n": len(v), "p50": q(0.5), "p95": q(0.95), "max": round(v[-1], 4)}


# ---- поза лидара: /tf_static без tf2 и numpy

def _rotate(q: Sequence[float], v: Sequence[float]) -> Tuple[float, float, float]:
    """Вектор v, повёрнутый кватернионом q = (x, y, z, w)."""
    x, y, z, w = q
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    # v' = v + 2w(q×v) + 2 q×(q×v)
    cx, cy, cz = y * v[2] - z * v[1], z * v[0] - x * v[2], x * v[1] - y * v[0]
    dx, dy, dz = y * cz - z * cy, z * cx - x * cz, x * cy - y * cx
    return v[0] + 2 * (w * cx + dx), v[1] + 2 * (w * cy + dy), v[2] + 2 * (w * cz + dz)


class Tf:
    """Дерево /tf_static: ребёнок → (родитель, сдвиг, поворот). lookup — функция «точка кадра → точка базы»."""

    def __init__(self) -> None:
        self.edges: Dict[str, Tuple[str, Tuple[float, float, float], Tuple[float, float, float, float]]] = {}

    def add(self, parent: str, child: str, xyz: Sequence[float], quat: Sequence[float]) -> None:
        self.edges[child.strip("/")] = (parent.strip("/"), tuple(xyz), tuple(quat))   # type: ignore[assignment]

    def lookup(self, base: str, frame: str) -> Optional[Callable[[Sequence[float]], Tuple[float, float, float]]]:
        base, frame = base.strip("/"), frame.strip("/")
        chain = []
        seen = set()
        while frame != base:
            if frame in seen or frame not in self.edges:
                return None
            seen.add(frame)
            parent, t, q = self.edges[frame]
            chain.append((t, q))
            frame = parent

        def apply(p: Sequence[float]) -> Tuple[float, float, float]:
            out = tuple(p)
            for t, q in chain:
                r = _rotate(q, out)
                out = (r[0] + t[0], r[1] + t[1], r[2] + t[2])
            return out  # type: ignore[return-value]
        return apply


class ScanBins:
    """Скан → бины пеленга в базе: ближайшее попадание от центра корпуса. Направления лучей в базе считаются один раз
    на геометрию скана и позу (683 луча по 5 Гц — без тригонометрии на каждый)."""

    def __init__(self, step_deg: float = SCAN_BIN_DEG):
        self.step = step_deg
        self.key = None
        self.origin = (0.0, 0.0, 0.0)
        self.dirs: List[Tuple[float, float]] = []

    def bins(self, angle_min: float, inc: float, ranges: Sequence[float], rmin: float, rmax: float,
             pose: Callable) -> Optional[Tuple[float, List[Optional[float]]]]:
        key = (angle_min, inc, len(ranges), pose)
        if key != self.key:
            o = pose((0.0, 0.0, 0.0))
            self.origin = o
            self.dirs = []
            for i in range(len(ranges)):
                a = angle_min + i * inc
                p = pose((math.cos(a), math.sin(a), 0.0))
                self.dirs.append((p[0] - o[0], p[1] - o[1]))
            self.key = key
        ox, oy = self.origin[0], self.origin[1]
        best: Dict[int, float] = {}
        for (dx, dy), r in zip(self.dirs, ranges):
            # годное попадание (REP 117): NaN, ±inf, коды ошибок URG ниже range_min и «дальше range_max» — не точка
            if not (rmin <= r <= rmax):
                continue
            x, y = ox + r * dx, oy + r * dy
            k = int(math.floor(math.degrees(math.atan2(y, x)) / self.step))
            d = math.hypot(x, y)
            if d < best.get(k, math.inf):
                best[k] = d
        if not best:
            return None
        k0, k1 = min(best), max(best)
        return k0 * self.step, [round(best[k], 2) if k in best else None for k in range(k0, k1 + 1)]


# ---- ресурсы: /proc (читается всеми; PSS чужого пользователя без прав не читается — README sysmon)

_SERVICE = re.compile(r"[^/]+\.service$")


def unit_of(cgroup_text: str) -> str:
    """Юнит systemd процесса по /proc/<pid>/cgroup (cgroup v2 и v1, как checks._unit_of deploy)."""
    for line in cgroup_text.splitlines():
        for part in reversed(line.rsplit(":", 1)[-1].split("/")):
            if _SERVICE.match(part):
                return part
    return ""


class ProcReader:
    """RSS и время CPU процессов по юнитам: {компонент: (rss КиБ, тики CPU, процессов)}."""

    def __init__(self, units: Dict[str, str], proc: str = "/proc"):
        self.units = units          # юнит → компонент
        self.proc = proc
        self.page_kib = os.sysconf("SC_PAGE_SIZE") // 1024 if hasattr(os, "sysconf") else 4
        self.clk_tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

    def _read(self, pid: str, name: str) -> str:
        try:
            with open(os.path.join(self.proc, pid, name), encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def sample(self) -> Dict[str, Tuple[int, int, int]]:
        out: Dict[str, List[int]] = {}
        try:
            pids = [p for p in os.listdir(self.proc) if p.isdigit()]
        except OSError:
            return {}
        for pid in pids:
            comp = self.units.get(unit_of(self._read(pid, "cgroup")))
            if comp is None:
                continue
            statm, stat = self._read(pid, "statm").split(), self._read(pid, "stat")
            rest = stat[stat.rfind(")") + 2:].split() if ")" in stat else []
            if len(statm) < 2 or len(rest) < 13:
                continue        # процесс вышел между чтениями
            acc = out.setdefault(comp, [0, 0, 0])
            acc[0] += int(statm[1]) * self.page_kib
            acc[1] += int(rest[11]) + int(rest[12])   # utime + stime: поля 14 и 15 /proc/<pid>/stat
            acc[2] += 1
        return {k: (v[0], v[1], v[2]) for k, v in out.items()}


# ---- кольцевой буфер и клиенты

class Client:
    """Поток SSE одного браузера. evicted — его место отдано новому потоку: соединение за застрявшим туннелем (ноутбук
    уснул, ssh на паузе) держало бы слот, пока sshd робота не потеряет TCP, — до ≈15 мин ретрансляций [замер ревью]."""

    __slots__ = ("n", "evicted")

    def __init__(self, n: int) -> None:
        self.n = n
        self.evicted = False


class Hub:
    """Кольцевой буфер шлюза (BUFFER_S, кадры SSE как байты) и раздача клиентам. Вытесненные сообщения кормят
    stream.State — снимок для нового клиента; all — состояние на сейчас (тревоги для hello.active). hb и scan — мимо
    буфера: hb у каждого соединения свой, scan — только последний (stream.TRANSIENT), в кадрах — на своём месте по t.
    Номер события — «запуск.номер»: после рестарта шлюза Last-Event-ID прежнего запуска не сойдёт за свой.
    Потоков не больше max_clients: новый вытесняет самый старый (новый — это живой человек, старый мог застрять)."""

    def __init__(self, buffer_s: float = stream.BUFFER_S, boot: Optional[str] = None, max_clients: int = 6):
        self.buffer_s = buffer_s
        self.boot = boot or secrets.token_hex(4)
        self.max_clients = max_clients
        self.cond = threading.Condition()
        self.buf: Deque[Tuple[int, float, bytes]] = deque()
        self.seq = 0
        self.last_t = -math.inf
        self.evicted = stream.State()
        self.all = stream.State()
        self.scan: Optional[Tuple[float, bytes]] = None     # (t, кадр) последнего скана
        self.scan_gen = 0
        self.clients: List[Client] = []
        self.joined = 0

    def ident(self, seq: int) -> str:
        return "%s.%d" % (self.boot, seq)

    def publish(self, msg: dict) -> dict:
        """Сообщение в поток: t не убывает (часы читаются в разных потоках), формат проверен до буфера."""
        with self.cond:
            msg["t"] = max(round(float(msg["t"]), 3), self.last_t)
            stream.check(msg)
            self.last_t = msg["t"]
            if msg["k"] in stream.TRANSIENT:
                if msg["k"] == "scan":
                    self.scan = (msg["t"], stream.sse(msg))
                    self.scan_gen += 1
                    self.cond.notify_all()
                return msg
            self.seq += 1
            self.buf.append((self.seq, msg["t"], stream.sse(msg, self.ident(self.seq))))
            self.all.push(msg)
            while self.buf and self.buf[0][1] < msg["t"] - self.buffer_s:
                self.evicted.push(_decode(self.buf.popleft()[2]))
            self.cond.notify_all()
            return msg

    def _resume(self, last_id: str) -> Optional[int]:
        boot, _, n = (last_id or "").strip().partition(".")
        if boot != self.boot or not n.isdigit():
            return None
        n = int(n)
        first = self.buf[0][0] if self.buf else self.seq + 1
        return n if first - 1 <= n <= self.seq else None

    def _with_scan(self, items: List[Tuple[float, bytes]], scan: bool) -> List[bytes]:
        """Кадры (по t) и последний скан на своём месте по t: формат требует, чтобы t не убывал (check_stream), а скан
        мимо буфера старше сообщений, пришедших после него. Скан почти всегда в конце — поиск с конца."""
        out = [f for _, f in items]
        if scan and self.scan is not None:
            st, frame = self.scan
            i = len(items)
            while i > 0 and items[i - 1][0] > st:
                i -= 1
            out.insert(i, frame)
        return out

    def attach(self, head: dict, last_id: str = "", now: Optional[float] = None) -> Tuple[List[bytes], int, int]:
        """Начало соединения (stream.connect): hello (active — тревоги сейчас), снимок на начало буфера, буфер,
        последний скан; с Last-Event-ID этого запуска, ещё в буфере, — hello с resume и только пропущенное.
        Возвращает (кадры, номер, поколение скана); кадры — ссылки на байты буфера, без копий."""
        with self.cond:
            n = self._resume(last_id)
            t = max(self.last_t, now if now is not None else time.time())
            hello = dict(head, k="hello", t=round(t, 3), format=stream.FORMAT, active=self.all.active(),
                         boot=self.boot)
            if n is not None:
                hello["resume"] = True
                items = [(ts, f) for s, ts, f in self.buf if s > n]
            else:
                # снимок — вытесненное, оно старше буфера: вместе с буфером уже по t
                items = [(m["t"], stream.sse(m)) for m in self.evicted.snapshot()] + [(ts, f) for _, ts, f in self.buf]
            return [stream.sse(hello)] + self._with_scan(items, True), self.seq, self.scan_gen

    def wait(self, cursor: int, scan_gen: int, timeout: float,
             client: Optional[Client] = None) -> Tuple[Optional[List[bytes]], int, int]:
        """Новое после cursor (и новый скан) или [] по таймауту; None — клиент отстал (его номера уже вытеснены из
        буфера: пусть переподключится и получит снимок вместо дыры) или его место отдано новому потоку."""
        with self.cond:
            if client is not None and client.evicted:
                return None, cursor, scan_gen
            if self.seq == cursor and self.scan_gen == scan_gen:
                self.cond.wait(timeout)
            if client is not None and client.evicted:
                return None, cursor, scan_gen
            items: List[Tuple[float, bytes]] = []
            if self.seq > cursor:
                first = self.buf[0][0] if self.buf else self.seq + 1
                if cursor + 1 < first:
                    return None, cursor, scan_gen
                need = self.seq - cursor
                for _, ts, f in reversed(self.buf):
                    if len(items) == need:
                        break
                    items.append((ts, f))
                items.reverse()
                cursor = self.seq
            fresh_scan = self.scan_gen != scan_gen
            scan_gen = self.scan_gen
            return self._with_scan(items, fresh_scan), cursor, scan_gen

    def join(self) -> Tuple[Client, Optional[Client]]:
        """Новый поток; если мест нет — самый старый вытесняется (его цикл выйдет на ближайшем wait). Возвращает
        (новый, вытесненный или None)."""
        with self.cond:
            old = None
            if len(self.clients) >= self.max_clients:
                old = self.clients.pop(0)
                old.evicted = True
                self.cond.notify_all()
            self.joined += 1
            c = Client(self.joined)
            self.clients.append(c)
            return c, old

    def leave(self, client: Client) -> None:
        with self.cond:
            if client in self.clients:
                self.clients.remove(client)

    def stats(self) -> dict:
        with self.cond:
            return {"messages": len(self.buf), "bytes": sum(len(f) for _, _, f in self.buf),
                    "span_s": round(self.buf[-1][1] - self.buf[0][1], 1) if self.buf else 0.0,
                    "clients": len(self.clients)}


def _decode(frame: bytes) -> dict:
    return json.loads(frame[frame.index(b"data: ") + 6:].decode("utf-8"))


# ---- граф → поток

class Live:
    """Перевод сообщений графа в поток пульта. Входы зовут колбэки узла (поток rclpy) и опросчик юнитов (свой поток),
    поэтому всё — под одной блокировкой. t — стенные часы робота (time.time())."""

    def __init__(self, hub: Hub, base_frame: str = "base_link", not_robot: Iterable[str] = (),
                 clock: Callable[[], float] = time.time):
        self.hub = hub
        self.lock = threading.RLock()
        self.clock = clock
        self.started = clock()
        self.base_frame = base_frame
        self.not_robot = set(not_robot)
        self.tracker = A.AlarmTracker()
        self.tf = Tf()
        self.poses: Dict[str, Callable] = {}       # кадр скана → поза в базе (сбрасывается новым /tf_static)
        self.scan_bins = ScanBins()
        self.warned = set()
        # safety_state
        self.safety_t: Optional[float] = None
        self.safety_last: Optional[dict] = None
        self.safety_key = None
        self.safety_emit_t = -math.inf
        self.link_down = False
        self.share = dict.fromkeys(STATES, 0.0)
        self.share_at: Optional[Tuple[str, float]] = None
        # скорости
        self.cmd_last: Dict[str, Tuple[Tuple[float, float], float]] = {}
        self.out_hist: Deque[Tuple[float, bool]] = deque(maxlen=64)   # смены «стоит / едет» по /cmd_vel
        self.moving_s = 0.0
        self.moving_at: Optional[Tuple[bool, float]] = None
        # жесты
        self.open: Dict[str, dict] = {}
        self.k1: Deque[float] = deque(maxlen=K1_KEEP)
        # стопы, принятые safety_gate: (boot safety_gate, counters.stop_now) последнего safety_state
        self.stops: Optional[Tuple[str, int]] = None
        # прочее
        self.health_key = None
        self.health_t = -math.inf
        self.scan_t = -math.inf
        self.units_key = None
        self.units_t = -math.inf
        self.mem_prev: Optional[Tuple[float, Dict[str, int]]] = None
        self.stats_t = self.started

    # -- вывод

    def emit(self, msg: dict) -> None:
        try:
            self.hub.publish(msg)
        except (stream.FormatError, ValueError) as e:   # формат один для двух концов: лучше потерять, чем сломать
            self._warn("format:" + msg.get("k", "?"), "сообщение не по формату, отброшено: %s" % e)

    def _warn(self, key: str, text: str) -> None:
        if key not in self.warned:
            self.warned.add(key)
            log.warning("%s", text)

    def _alarms(self, t: float, src: str, present: Sequence[A.Alarm], off_delay: Optional[float] = None) -> None:
        for m in self.tracker.update(t, src, present, off_delay):
            self.emit(m)

    # -- /neurobot/safety_state

    def on_safety(self, doc: dict, t: float) -> None:
        with self.lock:
            state = doc.get("state")
            if state not in STATES:
                self._warn("safety_state", "safety_state: непонятное состояние %r" % (state,))
                return
            reasons = [fmt.reason(r) for r in doc.get("reasons") or [] if isinstance(r, str)]
            warnings = [fmt.reason(w) for w in doc.get("warnings") or [] if isinstance(w, str)]
            level = A.level(state, reasons, warnings)
            msg = {"k": "safety", "t": t, "state": state, "reasons": reasons, "level": level, "warnings": warnings}
            for f in ("min_clearance_m", "front_m", "tilt_deg"):
                v = doc.get(f)
                if v is None or _num(v):
                    msg[f] = v
            if isinstance(doc.get("limits"), dict):
                msg["limits"] = {k: v for k, v in doc["limits"].items() if v is None or _num(v)}
            for f in ("req", "out"):
                v = doc.get(f)
                if isinstance(v, list) and len(v) == 2 and all(x is None or _num(x) for x in v):
                    msg[f] = {"v": v[0], "w": v[1]}
            counters = doc.get("counters") if isinstance(doc.get("counters"), dict) else {}
            n_stop = counters.get("stop_now")
            if isinstance(n_stop, int) and not isinstance(n_stop, bool):
                self.stops = (str(doc.get("boot") or ""), n_stop)
            if isinstance(doc.get("cut"), bool):
                msg["cut"] = doc["cut"]
            if isinstance(doc.get("lidar_mask_deg"), list):
                msg["lidar_mask_deg"] = doc["lidar_mask_deg"]
            est = doc.get("estop") if isinstance(doc.get("estop"), dict) else {}
            if est.get("engaged"):
                since = est.get("since") if _num(est.get("since")) else t
                msg["estop"] = {"engaged": True, "since": round(since, 3),
                                "reasons": [A.code_of(r) for r in est.get("reasons") or [] if isinstance(r, str)]}
            # доли состояний — по времени между сообщениями (молчание дольше SILENT_S не в счёт)
            if self.share_at is not None:
                dt = t - self.share_at[1]
                if 0 < dt <= SILENT_S:
                    self.share[self.share_at[0]] += dt
            self.share_at = (state, t)
            key = (state, level, tuple(A.code_of(r) for r in reasons), tuple(A.code_of(w) for w in warnings),
                   bool(est.get("engaged")))
            if key != self.safety_key or t - self.safety_emit_t >= SAFETY_EVERY_S:
                self.emit(msg)
                self.safety_key, self.safety_emit_t = key, t
            self.safety_last = msg
            self.safety_t = t
            if self.link_down:
                self._alarms(t, "link", [], off_delay=0.0)      # конец молчания известен точно
                self.link_down = False
            self._alarms(t, "safety", A.safety_alarms(state, reasons, warnings))
            req = msg.get("req")
            if req:     # запрос исполнителя — из safety_state: отдельная подписка на Twist — лишние 10 колбэков/с
                self.on_cmd("req", req["v"], req["w"], t)

    def state(self) -> Optional[str]:
        with self.lock:
            return self.safety_last["state"] if self.safety_last else None

    def stop_mark(self) -> Optional[Tuple[str, int]]:
        """Счётчик стопов safety_gate до команды (None — safety_state без counters: подтвердить нечем)."""
        with self.lock:
            return self.stops

    def stop_taken(self, mark: Optional[Tuple[str, int]]) -> bool:
        """safety_gate принял стоп после mark: счётчик вырос (или safety_gate перезапущен и уже считал стоп). Чей
        стоп — не различить (агент тоже шлёт robot_stop), но safety_gate его принял и ноль поставил."""
        with self.lock:
            if mark is None or self.stops is None:
                return False
            boot, n = self.stops
            return n > mark[1] if boot == mark[0] else n >= 1

    # -- скорости: req (исполнитель), out (safety_gate), base (вход базы)

    def on_cmd(self, role: str, v: float, w: float, t: float) -> None:
        if not (_num(v) and _num(w)):
            return
        with self.lock:
            if role == "out":
                self._out(abs(v) > MOVING_EPS or abs(w) > MOVING_EPS, t)
            r = (round(v, 2) + 0.0, round(w, 2) + 0.0)
            last = self.cmd_last.get(role)
            due = last is None or t - last[1] >= KEEPALIVE_S or (
                r != last[0] and (t - last[1] >= CMD_EVERY_S or r == (0.0, 0.0)))
            if not due:
                return
            msg = {"k": "cmd", "t": t, "topic": role, "v": r[0], "w": r[1]}
            if role == "out":
                msg["d_stop_m"] = robot.d_stop(max(r[0], 0.0))
            self.emit(msg)
            self.cmd_last[role] = (r, t)

    def _out(self, moving: bool, t: float) -> None:
        if self.moving_at is not None:
            dt = t - self.moving_at[1]
            if self.moving_at[0] and 0 < dt <= KEEPALIVE_S:
                self.moving_s += dt
        self.moving_at = (moving, t)
        was = self.out_hist[-1][1] if self.out_hist else False
        if moving == was:
            return
        self.out_hist.append((t, moving))
        if moving:      # первая ненулевая команда — тем жестам, что её ждут
            for g in list(self.open.values()):
                if g["measure"] and g["cmd"] is None and t >= g["t_end"] and t - g["t_rec"] <= CMD_WAIT_S:
                    self._cmd(g, t, t)

    def _moving_at(self, t: float) -> bool:
        state = False
        for ts, moving in self.out_hist:
            if ts > t:
                break
            state = moving
        return state

    # -- жесты: /neurobot/gesture, /neurobot/events, исход

    def on_gesture(self, doc: dict, t: float) -> None:
        with self.lock:
            gid = "%s:%s" % (doc.get("boot") or "?", doc.get("seq") if doc.get("seq") is not None else int(t * 1000))
            name = doc.get("name") if isinstance(doc.get("name"), str) else None
            msg = {"k": "gesture", "t": t, "id": gid, "kind": "gesture", "ref": "t_end", "name": name}
            for f, kind in (("cls", int), ("n_boxes", int), ("side", str)):
                if isinstance(doc.get(f), kind) and not isinstance(doc.get(f), bool):
                    msg[f] = doc[f]
            for f in ("conf", "margin"):
                if _num(doc.get(f)):
                    msg[f] = doc[f]
            t_end = doc.get("t_end") if _num(doc.get("t_end")) else t
            t_rec = doc.get("t_rec") if _num(doc.get("t_rec")) else t
            if _num(doc.get("t_end")) and _num(doc.get("t_rec")):
                msg.update(t_end=round(t_end, 3), t_rec=round(t_rec, 3), d_rec_s=round(t_rec - t_end, 4))
            self.emit(msg)
            g = {"id": gid, "name": name, "t_end": t_end, "t_rec": t_rec, "cmd": None,
                 "measure": not self._moving_at(t_end)}
            if name in NO_MANEUVER:
                self._close(g, t, "none")
                return
            self.open[gid] = g
            if g["measure"]:     # DriveController кладёт манёвр раньше, чем публикует событие: команда могла прийти
                start = next((ts for ts, moving in self.out_hist if moving and ts >= t_end), None)
                if start is not None and start - t_rec <= CMD_WAIT_S:
                    self._cmd(g, start, t)

    def _cmd(self, g: dict, t_cmd: float, t: float) -> None:
        g["cmd"] = t_cmd
        d = round(t_cmd - g["t_end"], 4)
        self.emit({"k": "gesture", "t": t, "id": g["id"], "d_cmd_s": d, "t_cmd": round(t_cmd, 3),
                   "maneuver": g["name"]})
        self.k1.append(d)
        recent = list(self.k1)
        self.emit({"k": "k1", "t": t, "window": K1_WINDOW, "ref": "t_end", "last": quantiles(recent[-K1_WINDOW:]),
                   "run": quantiles(recent), "run_window": K1_KEEP})

    def _close(self, g: dict, t: float, outcome: str, stopped_by: Optional[List[str]] = None) -> None:
        msg = {"k": "gesture", "t": t, "id": g["id"], "outcome": outcome}
        if stopped_by:
            msg["stopped_by"] = stopped_by
        self.emit(msg)
        self.open.pop(g["id"], None)

    def on_event(self, doc: dict, t: float) -> None:
        """task_end исполнителя (отмена по safety_gate, вытеснение) — исход последнего открытого жеста."""
        with self.lock:
            if doc.get("type") != "task_end" or doc.get("task") != "gesture" or not self.open:
                return
            g = list(self.open.values())[-1]
            status = doc.get("status")
            codes = [A.code_of(r) for r in doc.get("reasons") or [] if isinstance(r, str)]
            if status in CUT:
                self._close(g, t, "cut", codes or [str(status)])
            elif status == "preempted":
                self._close(g, t, "preempted")
            elif status == "done":
                self._close(g, t, "done")

    # -- /neurobot/perception_health

    def on_health(self, doc: dict, t: float) -> None:
        with self.lock:
            state = doc.get("state") if isinstance(doc.get("state"), str) else "ok"
            age = doc.get("frame_age_s") if _num(doc.get("frame_age_s")) else None
            lost = age is not None and age > CAMERA_LOST_S
            key = (state, bool(doc.get("dark")), lost)
            if key != self.health_key or t - self.health_t >= HEALTH_EVERY_S:
                msg = {"k": "health", "t": t, "state": state}
                for f in ("fps", "frame_age_s"):
                    if _num(doc.get(f)):
                        msg[f] = doc[f]
                if isinstance(doc.get("recoveries"), int) and not isinstance(doc.get("recoveries"), bool):
                    msg["recoveries"] = doc["recoveries"]
                if isinstance(doc.get("dark"), bool):
                    msg["dark"] = doc["dark"]
                self.emit(msg)
                self.health_key, self.health_t = key, t
            present = []
            if state == "recovering":
                present.append(A.Alarm("ui_perception_recovering", A.P2, A.CODES["ui_perception_recovering"].text))
            if lost:
                present.append(A.Alarm("ui_camera_lost", A.P2, A.CODES["ui_camera_lost"].text))
            self._alarms(t, "perception", present)

    # -- скан

    def on_tf_static(self, transforms: Sequence[Tuple[str, str, Sequence[float], Sequence[float]]]) -> None:
        with self.lock:
            for parent, child, xyz, quat in transforms:
                self.tf.add(parent, child, xyz, quat)
            self.poses.clear()

    def want_scan(self, t: float) -> bool:
        """Колбэк /scan разбирает сообщение, только если пора (5 Гц из 10)."""
        return t - self.scan_t >= SCAN_EVERY_S

    def on_scan(self, frame: str, angle_min: float, inc: float, ranges: Sequence[float], rmin: float, rmax: float,
                t: float) -> None:
        with self.lock:
            if not self.want_scan(t):
                return
            self.scan_t = t
            pose = self.poses.get(frame) or self.tf.lookup(self.base_frame, frame or "")
            if pose is None:     # /tf_static приходит не сразу после старта: молчим, пока он мог не дойти
                if t - self.started > TF_WAIT_S:
                    self._warn("tf:" + frame, "скан не рисуется: нет /tf_static %s → %s" % (self.base_frame, frame))
                return
            self.poses[frame] = pose
            got = self.scan_bins.bins(angle_min, inc, ranges, rmin, rmax, pose)
            if got is None:
                return
            self.emit({"k": "scan", "t": t, "a0": got[0], "da": self.scan_bins.step, "r": got[1],
                       "frame": self.base_frame, "range_max": round(rmax, 2)})

    # -- юниты и ресурсы (опросчик шлюза)

    def on_units(self, rows: Sequence[dict], t: float) -> None:
        """rows — [{comp, unit, active, sub, restarts}] в порядке запуска."""
        with self.lock:
            down = [r["comp"] for r in rows if r["active"] != "active"]
            restarts = {r["comp"]: int(r["restarts"]) for r in rows}
            key = (tuple(down), tuple(sorted(restarts.items())), tuple((r["active"], r["sub"]) for r in rows))
            if key != self.units_key or t - self.units_t >= UNITS_EVERY_S:
                self.emit({"k": "units", "t": t, "up": len(rows) - len(down), "total": len(rows), "down": down,
                           "restarts": restarts,
                           "list": [{"comp": r["comp"], "active": r["active"], "sub": r["sub"],
                                     "restarts": int(r["restarts"])} for r in rows]})
                self.units_key, self.units_t = key, t
            self._alarms(t, "unit", [A.Alarm("ui_unit_down:" + c, A.P2, "%s: %s" % (A.CODES["ui_unit_down"].text, c))
                                     for c in down], off_delay=0.0)

    def on_mem(self, usage: Dict[str, Tuple[int, int, int]], t: float, clk_tck: int = 100) -> None:
        """usage — ProcReader.sample(): RSS по компонентам; CPU — приращение тиков между опросами, % одного ядра."""
        with self.lock:
            robot_kib = sum(rss for comp, (rss, _, _) in usage.items() if comp not in self.not_robot)
            msg = {"k": "mem", "t": t, "total_mib": round(robot_kib / 1024.0, 1), "metric": "rss",
                   "units": {comp: round(rss / 1024.0, 1) for comp, (rss, _, _) in sorted(usage.items())}}
            ticks = {comp: tk for comp, (_, tk, _) in usage.items() if comp not in self.not_robot}
            if self.mem_prev is not None and t > self.mem_prev[0]:
                prev = self.mem_prev[1]
                used = sum(max(0, tk - prev.get(comp, tk)) for comp, tk in ticks.items())
                msg["cpu_pct"] = round(100.0 * used / clk_tck / (t - self.mem_prev[0]), 1)
            self.mem_prev = (t, ticks)
            self.emit(msg)

    # -- действия оператора: строка в ленте событий

    def operator(self, action: str, why: str, t: float) -> None:
        with self.lock:
            msg = {"k": "operator", "t": t, "action": action, "what": "пульт"}
            if why:
                msg["why"] = why
            self.emit(msg)

    # -- раз в SILENT_S / 2: молчание safety_state, исходы жестов, сводка

    def tick(self, t: float) -> None:
        with self.lock:
            ref = self.safety_t if self.safety_t is not None else self.started + START_GRACE_S - SILENT_S
            if t - ref > SILENT_S:
                self.link_down = True
                self._alarms(t, "link", [A.Alarm("ui_safety_silent", A.P1, A.CODES["ui_safety_silent"].text)])
            for g in list(self.open.values()):
                if g["cmd"] is None:
                    if t - g["t_rec"] > CMD_WAIT_S:
                        self._close(g, t, "none" if g["measure"] else "unobserved")
                    continue
                last = self.out_hist[-1] if self.out_hist else None
                if last is not None and not last[1] and last[0] > g["cmd"] and t - last[0] >= END_ZERO_S:
                    self._close(g, t, "done")
                elif t - g["t_rec"] > MANEUVER_MAX_S:
                    self._close(g, t, "done")
            if t - self.stats_t >= STATS_EVERY_S:
                self.stats_t = t
                self.emit(self.stats(t))

    def stats(self, t: float) -> dict:
        with self.lock:
            msg = {"k": "stats", "t": t, "moving_s": round(self.moving_s, 1)}
            span = t - self.started
            if span > 0:
                msg["moving_share"] = round(min(1.0, self.moving_s / span), 4)
            seen = sum(self.share.values())
            if seen > 0:
                msg["safety_share"] = {s: round(v / seen, 4) for s, v in self.share.items()}
            msg["k1"] = quantiles(self.k1)
            return msg
