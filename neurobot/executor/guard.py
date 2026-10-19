"""Исполнитель v1 (бывший страж /cmd_vel): выбор команды, запрет движения вперёд и отмена задачи по
safety_gate — без rclpy.

Правила (PLAN §2.4, v1; R7 — RS2, RS3, N14):
- входы — Twist жестов (`/cmd_vel_gesture`, ремап демо) и агента (`/cmd_vel_agent`); вход старше
  `input_timeout` не считается заявкой;
- нулевой Twist жестов — не заявка: демо в покое шлёт нули 10 Гц;
- свежая команда агента главнее жеста: ненулевая едет, нулевая — явный стоп. Вмешательство агента в идущий
  манёвр жеста гасит этот манёвр до его конца (пока вход жестов не станет нулевым или устаревшим), иначе
  после стопа агента демо доехало бы остаток манёвра по своим часам (S3, S4);
- без заявок — явный ноль;
- удержание старта: вход, впервые пришедший ненулевым раньше `start_quiet_s` после первого шага, не исполняется,
  пока не станет нулевым или устаревшим, — манёвр жеста или команда агента, начатые до рестарта исполнителя, не
  продолжаются (PLAN §13.3). Демо в покое шлёт нули — вход жестов взводится первым же нулём; вход агента, молчавший
  `start_quiet_s`, взведён;
- нечисловая скорость (NaN, ±inf) — явный ноль и счётчик invalid: NaN обходит любые сравнения;
- задача (манёвр жеста или команда агента) отменяется до конца своего входа, если safety_gate её не пропустит:
  защёлка, его предел по нужной оси равен нулю или его состояния нет дольше safety_timeout (require_safety).
  Отмена — событие task_end{task, status: estop|blocked|safety_lost, reasons}. Новый жест в этом состоянии не
  начинается — он отменяется на первом же шаге;
- выход из тупика (повторная приёмка 18.10): safety_gate дольше escape_after_s держит тупик — поворот на месте заденет
  (причина touch), вперёд нельзя или только подползти к препятствию или краю (approach, obstacle, cliff), назад можно, —
  а заявок нет: исполнитель сам отъезжает назад со скоростью escape_speed по своему следу — не дальше escape_dist и
  прямого пути, пройденного вперёд по /odom после последнего поворота. Следа короче TRAIL_MIN_M нет (робот поставлен
  руками, только что развернулся) — отъезда нет; защёлка, потеря одометрии (stale_odom) и safety_gate след обнуляют.
  Ни один жест не ведёт назад, и без этого робот у стены или ножки стула стоял бы до человека. Заявка агента или жеста
  главнее отъезда и прерывает его (preempted); события — escape{status: start, dist_m} и task_end{task: escape, status:
  done|preempted|estop|blocked|safety_lost};
- вперёд (`linear > 0`) запрещено, если `/scan` старше `scan_timeout` (или скана ещё не было), если в секторе
  ±`sector_deg` есть достоверная дальность ближе тормозной `stop_distance(v)` или если сектор «слеп»: сплошной
  провал (NaN или код ошибки ниже `range_min`) шире `max_gap_deg`. Повороты и задний ход не трогаются.

Тормозная дальность отсчитывается от оси лидара и растёт со скоростью (RS2): d_stop + v·react_s + v²/(2·decel).
Слепой сектор (RS3: на роботе D435i, вероятно, закрывает лидару −61…+4°) по умолчанию значит «нет данных —
вперёд нельзя»; `allow_blind` — работа под присмотром. Пока safety_gate на связи, слепой сектор решает он: у него
есть глубина, которой перед в этом секторе виден.

Используются только `linear.x` и `angular.z`: база дифференциальная. Время — монотонные секунды от
вызывающего (узел или тест).
"""
from __future__ import annotations

import functools
import json
import math
from typing import NamedTuple, Optional, Sequence

FRONT_CLEAR = "clear"
FRONT_OBSTACLE = "obstacle"  # достоверная дальность ближе тормозной
FRONT_BLIND = "blind"        # провал в секторе шире max_gap_deg
FRONT_STALE = "stale"        # скана нет или он старше scan_timeout

SAFETY_STATES = ("ok", "slow", "blocked", "estop")
TRAIL_MIN_M = 0.03   # м: след короче — отъезда нет (робот поставлен руками или только что развернулся)
TRAIL_YAW = 0.15     # рад: поворот больше — позади уже не пройденный путь, след считается заново
TRAIL_JUMP_M = 0.5   # м: скачок одометрии больше — её сбросили (рестарт базы), следа нет


class Params(NamedTuple):
    input_timeout: float = 0.4  # с; вход старше — не заявка (deadman)
    scan_timeout: float = 0.5   # с; скан старше — вперёд запрещено
    d_stop: float = 0.35        # м от оси лидара при v → 0 (README лаборатории: 0,35; край базы ≈0,21 — RS2)
    react_s: float = 0.2        # с; период скана и доставка — в тормозную дальность [оценка]
    decel: float = 1.0          # м/с²; замедление базы [оценка до HIL]
    sector_deg: float = 30.0    # полуширина переднего сектора, градусы
    max_gap_deg: float = 3.0    # самый широкий допустимый провал в секторе, градусы
    allow_blind: bool = False   # провал не запрещает движение вперёд (только под присмотром)
    safety_timeout: float = 1.0  # с; /neurobot/safety_state старше — safety_gate потерян
    require_safety: bool = False  # без safety_state задачи не исполняются (в узле — true, README)
    start_quiet_s: float = 2.0  # с; удержание старта: вход, впервые ненулевой раньше, ждёт нуля или паузы [допущение]
    escape_after_s: float = 3.0  # с тупика safety_gate без заявок — отъезд назад; 0 — не отъезжать [допущение]
    escape_speed: float = 0.05  # м/с назад при отъезде (reverse_speed safety_gate) [задача: PLAN §2.4]
    escape_dist: float = 0.1    # м отъезда наибольший (по времени): у стены хватило 0,046 [замер приёмки 18.10, S5]


class Command(NamedTuple):
    linear: float
    angular: float
    by: str      # "agent" | "gesture" | "escape" | "none"
    front: str   # FRONT_*: можно ли вперёд при скорости выбранной команды
    cut: bool    # движение вперёд запрошено и срезано

    @property
    def blocked_front(self) -> bool:
        return self.front != FRONT_CLEAR


class Front(NamedTuple):
    nearest: float  # м; ближайшая достоверная дальность в секторе, inf — ничего
    gap_deg: float  # самый широкий сплошной провал в секторе, градусы


class Safety(NamedTuple):
    """Сводка /neurobot/safety_state, нужная исполнителю."""
    state: str      # SAFETY_STATES
    v_max: float    # что safety_gate пропустит сейчас: вперёд, назад (модуль), поворот (модуль)
    v_rev: float
    w_max: float
    reasons: tuple


FRONT_NEAR = ("approach", "obstacle", "cliff")  # причины safety_gate: впереди близко препятствие или край


def dead_end(s: Safety) -> bool:
    """Тупик: поворот на месте заденет (touch), назад можно, а вперёд нельзя или можно лишь подползти к препятствию
    или краю (FRONT_NEAR: approach — путь короче тормозного на полной скорости, obstacle, cliff): ни один жест робота
    отсюда не выведет."""
    codes = {r.split(":", 1)[0] for r in s.reasons}
    return (s.state in ("blocked", "slow") and s.w_max <= 0.0 and s.v_rev > 0.0 and "touch" in codes
            and (s.v_max <= 0.0 or any(c in codes for c in FRONT_NEAR)))


def parse_safety(data: str) -> Safety:
    """Строка JSON /neurobot/safety_state; ValueError — не разобрана. Предел без числа — 0 (строже)."""
    try:
        doc = json.loads(data)
        state, limits, reasons = doc["state"], doc.get("limits") or {}, doc.get("reasons") or []
        if state not in SAFETY_STATES:
            raise ValueError("state {!r}".format(state))

        def limit(name: str) -> float:
            v = limits.get(name)
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            return float(v) if ok else 0.0
        return Safety(state, limit("v_max"), limit("v_rev"), limit("w_max"),
                      tuple(str(r) for r in reasons) if isinstance(reasons, list) else ())
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise ValueError("safety_state не разобран: {}".format(e)) from e


def check_scan_meta(angle_min: float, angle_increment: float, range_min: float, range_max: float, n: int) -> None:
    """ValueError, если по метаданным скана сектор не построить (пустой скан, не числа, шаг 0, диапазон пуст)."""
    if n == 0:
        raise ValueError("пустой скан")
    if not all(math.isfinite(v) for v in (angle_min, angle_increment, range_min, range_max)):
        raise ValueError("метаданные скана — не числа")
    if angle_increment == 0.0 or range_min < 0.0 or range_max <= range_min:
        raise ValueError("метаданные скана: шаг {}, диапазон {}…{}".format(angle_increment, range_min, range_max))


def stop_distance(p: Params, v: float) -> float:
    """Дальность от оси лидара, ближе которой вперёд со скоростью v нельзя (не число — бесконечность)."""
    if not math.isfinite(v):
        return math.inf
    v = max(v, 0.0)
    return p.d_stop + v * p.react_s + v * v / (2 * p.decel)


def _wrap(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


@functools.lru_cache(maxsize=8)
def _sector(angle_min: float, angle_increment: float, n: int, half_width: float) -> tuple:
    """Индексы лучей сектора, упорядоченные по углу (у скана 0…2π сектор переходит через конец массива)."""
    angles = sorted((_wrap(angle_min + i * angle_increment), i) for i in range(n))
    return tuple(i for a, i in angles if abs(a) <= half_width)


def front_scan(ranges: Sequence[float], angle_min: float, angle_increment: float, half_width: float,
               range_min: float, range_max: float) -> Front:
    """Сектор ±half_width (рад) вокруг оси x робота: ближайшая достоверная дальность и самый широкий провал.

    Сектор считается по углам из angle_min/angle_increment, а не по индексам (R8), углы приводятся к (−π, π]:
    годятся и сканы 0…2π (TurtleBot3), и ±120° (URG-04LX). По REP 117 −inf («ближе минимума») — это 0, +inf и
    значения дальше range_max — «ничего нет». Провал — NaN и значения ниже range_min: urg_node2 отдаёт так
    отсутствие отражения и коды ошибок URG (N14); препятствием они не считаются, но и свободу не доказывают.
    """
    nearest = math.inf
    run = longest = 0
    for i in _sector(angle_min, angle_increment, len(ranges), half_width):
        r = ranges[i]
        if r == -math.inf:
            run, nearest = 0, 0.0
        elif math.isnan(r) or r < range_min:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
            if r <= range_max and r < nearest:
                nearest = r
    return Front(nearest, longest * abs(math.degrees(angle_increment)))


class _Input:
    __slots__ = ("t", "linear", "angular", "expired")

    def __init__(self):
        self.t: Optional[float] = None
        self.linear = self.angular = 0.0
        self.expired = False

    def set(self, now: float, linear: float, angular: float) -> None:
        self.t, self.linear, self.angular, self.expired = now, linear, angular, False

    def fresh(self, now: float, timeout: float) -> bool:
        return self.t is not None and now - self.t <= timeout

    @property
    def nonzero(self) -> bool:
        return self.linear != 0.0 or self.angular != 0.0


class Guard:
    """Автомат стража. Вызовы по очереди из одного потока (колбэки и тик узла).

    counters — структурные счётчики переходов (не вызовов): cut_forward — движение вперёд срезано запретом;
    stale — ненулевой вход перестал приходить (сработал deadman); preempted — агент погасил манёвр жеста;
    safety_cancel — задача отменена по safety_gate; escape — отъезд из тупика начат. invalid — нечисловые входы
    (по сообщениям). events — события для /neurobot/events; узел забирает их после command().
    """

    def __init__(self, params: Params = Params()):
        self.p = params
        self._half_width = math.radians(params.sector_deg)
        self._gesture = _Input()
        self._agent = _Input()
        self._scan_t: Optional[float] = None
        self.front = Front(math.inf, 0.0)
        self._suppressed = True  # вход жестов погашен до нуля или паузы: удержание старта, агент, safety_gate
        self._agent_off = True   # вход агента — так же: удержание старта, safety_gate
        self._t0: Optional[float] = None
        self._cut = False
        self._safety: Optional[Safety] = None
        self._safety_t: Optional[float] = None
        self._dead_since: Optional[float] = None    # начало тупика safety_gate
        self._escape_until: Optional[float] = None  # идёт отъезд из тупика — до этого момента
        self._pose: Optional[tuple] = None          # (x, y, курс) последней одометрии
        self._trail = 0.0                           # м прямого пути позади, пройденного вперёд после поворота
        self._trail_yaw = 0.0                       # курс, вдоль которого считается след
        self.events: list = []
        self.counters = {"cut_forward": 0, "stale": 0, "preempted": 0, "safety_cancel": 0, "escape": 0, "invalid": 0}

    def _finite(self, linear: float, angular: float):
        if math.isfinite(linear) and math.isfinite(angular):
            return linear, angular
        self.counters["invalid"] += 1
        return 0.0, 0.0

    def gesture(self, now: float, linear: float, angular: float) -> None:
        linear, angular = self._finite(linear, angular)
        self._gesture.set(now, linear, angular)
        if not self._gesture.nonzero:
            self._suppressed = False

    def agent(self, now: float, linear: float, angular: float) -> None:
        linear, angular = self._finite(linear, angular)
        g = self._gesture
        if g.fresh(now, self.p.input_timeout) and g.nonzero and not self._suppressed:
            self._suppressed = True
            self.counters["preempted"] += 1
        self._agent.set(now, linear, angular)
        if not self._agent.nonzero:
            self._agent_off = False

    def safety(self, now: float, s: Safety) -> None:
        if s.state == "estop" or any(r.startswith("stale_odom") for r in s.reasons):
            # защёлка (удар, наклон: робота могли поднять и переставить) или одометрии нет — след позади не доказан;
            # полный стоп по другим датчикам робот не двигает, след остаётся
            self._trail = 0.0
        self._safety, self._safety_t = s, now

    def odom(self, x: float, y: float, yaw: float) -> None:
        """Одометрия: след — прямой путь позади, пройденный вперёд после последнего поворота (назад его тратит)."""
        if self._pose is None or math.hypot(x - self._pose[0], y - self._pose[1]) > TRAIL_JUMP_M \
                or abs(_wrap(yaw - self._trail_yaw)) > TRAIL_YAW:
            self._trail, self._trail_yaw = 0.0, yaw
        else:
            px, py, pyaw = self._pose
            self._trail = max(0.0, self._trail + (x - px) * math.cos(pyaw) + (y - py) * math.sin(pyaw))
        self._pose = (x, y, yaw)

    def safety_fresh(self, now: float) -> bool:
        return self._safety_t is not None and now - self._safety_t <= self.p.safety_timeout

    def safety_state(self, now: float) -> Optional[str]:
        """Состояние safety_gate, если оно свежее."""
        return self._safety.state if self.safety_fresh(now) else None

    def _safety_block(self, now: float, linear: float, angular: float) -> Optional[str]:
        """Почему safety_gate не пропустит эту команду: estop | blocked | safety_lost, или None."""
        if not self.safety_fresh(now):
            return "safety_lost" if self.p.require_safety else None
        s = self._safety
        if s.state == "estop":
            return "estop"
        if ((linear > 0.0 and s.v_max <= 0.0) or (linear < 0.0 and s.v_rev <= 0.0)
                or (angular != 0.0 and s.w_max <= 0.0)):
            return "blocked"
        return None

    def scan(self, now: float, ranges: Sequence[float], angle_min: float, angle_increment: float,
             range_min: float, range_max: float) -> None:
        """ValueError — негодные метаданные: скан не принят и устареет, вперёд станет нельзя."""
        check_scan_meta(angle_min, angle_increment, range_min, range_max, len(ranges))
        self.front = front_scan(ranges, angle_min, angle_increment, self._half_width, range_min, range_max)
        self._scan_t = now

    def scan_age(self, now: float) -> Optional[float]:
        return None if self._scan_t is None else now - self._scan_t

    def front_state(self, now: float, v: float) -> str:
        """Можно ли вперёд со скоростью v (FRONT_*)."""
        age = self.scan_age(now)
        if age is None or age > self.p.scan_timeout:
            return FRONT_STALE
        if self.front.gap_deg > self.p.max_gap_deg and not (self.p.allow_blind or self.safety_fresh(now)):
            return FRONT_BLIND
        if self.front.nearest < stop_distance(self.p, v):
            return FRONT_OBSTACLE
        return FRONT_CLEAR

    def command(self, now: float) -> Command:
        timeout = self.p.input_timeout
        if self._t0 is None:
            self._t0 = now
        quiet = now - self._t0 >= self.p.start_quiet_s  # вход, не пришедший к этому времени, после старта не шёл
        for inp in (self._gesture, self._agent):
            if inp.nonzero and not inp.expired and inp.t is not None and not inp.fresh(now, timeout):
                inp.expired = True
                self.counters["stale"] += 1
        g, a = self._gesture, self._agent
        gesture_on = g.fresh(now, timeout) and g.nonzero
        if not gesture_on and (g.t is not None or quiet):
            self._suppressed = False
        if not a.fresh(now, timeout) and (a.t is not None or quiet):
            self._agent_off = False
        if a.fresh(now, timeout) and not self._agent_off:
            linear, angular, by = a.linear, a.angular, "agent"
        elif gesture_on and not self._suppressed:
            linear, angular, by = g.linear, g.angular, "gesture"
        else:
            linear, angular, by = self._escape(now)
        if by not in ("escape", "none") and self._escape_until is not None:
            self._end_escape("preempted", [])
        status = self._safety_block(now, linear, angular) if (linear or angular) else None
        if status:
            reasons = [] if status == "safety_lost" else list(self._safety.reasons)
            if by == "escape":
                self._end_escape(status, reasons)
            else:
                if by == "agent":
                    self._agent_off = True
                else:
                    self._suppressed = True
                self.events.append({"type": "task_end", "task": by, "status": status, "reasons": reasons})
            self.counters["safety_cancel"] += 1
            linear, angular, by = 0.0, 0.0, "none"
        front = self.front_state(now, linear)
        cut = front != FRONT_CLEAR and linear > 0.0
        if cut:
            if not self._cut:
                self.counters["cut_forward"] += 1
            linear = 0.0
        self._cut = cut
        return Command(linear, angular, by, front, cut)

    def _escape(self, now: float):
        """Без заявок: отъезд назад из тупика safety_gate (linear, angular, by) или покой."""
        p = self.p
        if self._escape_until is not None:
            if now < self._escape_until:
                return -p.escape_speed, 0.0, "escape"
            self._end_escape("done", [])
            return 0.0, 0.0, "none"
        s = self._safety if self.safety_fresh(now) else None
        if s is None:
            self._trail = 0.0
        if p.escape_after_s <= 0.0 or s is None or not dead_end(s) or self._trail < TRAIL_MIN_M:
            self._dead_since = None
            return 0.0, 0.0, "none"
        if self._dead_since is None:
            self._dead_since = now
        if now - self._dead_since < p.escape_after_s:
            return 0.0, 0.0, "none"
        dist = min(p.escape_dist, self._trail)
        self._escape_until = now + dist / p.escape_speed
        self.counters["escape"] += 1
        self.events.append({"type": "escape", "status": "start", "dist_m": round(dist, 3), "reasons": list(s.reasons)})
        return -p.escape_speed, 0.0, "escape"

    def _end_escape(self, status: str, reasons: list) -> None:
        self._escape_until = self._dead_since = None
        self.events.append({"type": "task_end", "task": "escape", "status": status, "reasons": reasons})
