"""Решение safety_gate без rclpy: что из желаемой скорости исполнителя уходит в /cmd_vel (PLAN §13.3, слой L2).

Порядок проверок на каждом шаге:
1. Защёлка (estop) — ноль по всем осям, пока её не снимет человек.
2. Блокировка старта и устаревшие входы — ноль: нет или старше порога скан, одометрия, IMU; годной глубины (кадра с
   покрытием коридора не меньше depth_min_valid) ещё не было;
   маска лидара не выучена; превышено время непрерывного движения; издатели входов twist_mux главнее /cmd_vel при
   override_policy=block.
3. Пределы по осям:
   - вперёд: тормозной путь от края корпуса до ближайшей опасности в коридоре (лидар — сейчас, глубина — память
     клеток), гистерезис снятия блока; неизвестные лучи лидара, чей участок в коридоре выходит за корпус и не виден
     свежей глубиной, — нельзя (lidar.unknown_front); перед в слепом секторе лидара — не быстрее blind_speed при
     свежей глубине, иначе нельзя; глубина устарела — по depth_stale; геозабор;
   - назад: не быстрее reverse_speed и не дальше reverse_dist за самую дальнюю точку пути (сзади датчиков нет):
     задний ход по одометрии тратит бюджет, ход вперёд его возвращает;
   - поворот на месте: можно, если ничто не ближе turn_margin к кругу корпуса.
4. Взвод: после старта, защёлки и любого полного стопа ненулевой запрос не проходит, пока исполнитель не пришлёт
   ноль (или замолчит), — движение само не продолжается. Нечисловой запрос — явный ноль.
5. Разгон: ускорение и рывок ограничены, торможение — нет.
6. Упор: команда дошла до базы (выход twist_mux), а одометрия стоит дольше stall_s — защёлка.
Наклон, удар и чужой писатель /cmd_vel ставят защёлку сразу, в колбэке входа.

Время — монотонные секунды от вызывающего (узел или тест), кроме поля t команды защёлки (часы робота).
"""
from __future__ import annotations

import collections
import logging
import math
from typing import Dict, NamedTuple, Optional, Sequence, Tuple

import numpy as np

from .config import Params
from .depth import FreeSpace, Hazards, Memory
from .geometry import Pose, edge_distance
from .latch import EstopCmd, Latch
from .lidar import LidarView, MaskLearner, Rays, ScanMeta, check_meta, mask_spans, self_hits, view
from .monitors import Shock, Stall
from .motion import Ramp, allowed_speed

log = logging.getLogger("safety_gate")

OK, SLOW, BLOCKED, ESTOP = "ok", "slow", "blocked", "estop"
RELEASE_MAX_AGE_S = 10.0  # release старше по полю t (или без t) не принимается: поздний повтор из transient_local
MUX_FRESH_S = 0.3         # выход twist_mux свежее — по нему видно, дошла ли команда до базы
MUX_TOL = (0.03, 0.08)    # м/с, рад/с: «дошла» — выход twist_mux не дальше от нашей команды
OVERRIDE_S = 0.5          # наша ненулевая команда не доходит до базы дольше — предупреждение
ODOM_JUMP_M = 0.5         # скачок позы одометрии между сообщениями больше — её сбросили


class Decision(NamedTuple):
    v: float                      # в /cmd_vel
    w: float
    state: str                    # OK | SLOW | BLOCKED | ESTOP
    reasons: Tuple[str, ...]      # «код: текст»: почему стоп, блок или замедление
    warnings: Tuple[str, ...]
    v_max: float                  # что разрешено сейчас: вперёд, назад (модуль), поворот (модуль)
    v_rev: float
    w_max: float
    front_m: float                # путь вперёд до ближайшей опасности в коридоре, от края корпуса
    clearance_m: float            # от края корпуса до ближайшей опасности в любом направлении
    req: Tuple[float, float]      # запрос исполнителя (устаревший — ноль)
    cut: bool                     # ненулевая ось запроса обнулена

    @property
    def codes(self) -> Tuple[str, ...]:
        return tuple(r.split(":", 1)[0] for r in self.reasons)


class Gate:
    def __init__(self, p: Params, latch: Latch):
        self.p = p
        self.latch = latch
        self.learner = MaskLearner(p)
        self.memory = Memory(p)
        self.stall = Stall(p)
        self.lidar: Optional[LidarView] = None
        self.mask_deg: list = []
        self.outside_deg: list = []
        self.tilt: Optional[float] = None
        self.counters = {"invalid": 0, "cut": 0, "estop_engaged": 0, "estop_released": 0, "release_denied": 0}
        self._req: Tuple[Optional[float], float, float] = (None, 0.0, 0.0)
        self._t: Dict[str, Optional[float]] = dict.fromkeys(("scan", "odom", "depth", "imu", "accel"))
        self._errors: Dict[str, str] = {}
        self._rays: Optional[Rays] = None
        self._free: Optional[FreeSpace] = None   # последний принятый кадр глубины — для неизвестных лучей лидара
        self._rays_key = None
        self._pose: Optional[Tuple[float, float, float]] = None
        self._odom_vw = (0.0, 0.0)
        self._shock = Shock()
        self._mux: Tuple[Optional[float], float, float] = (None, 0.0, 0.0)
        self._foreign: Tuple[str, ...] = ()
        self._overrides: Tuple[str, ...] = ()
        self._ids: collections.deque = collections.deque(maxlen=32)
        self._armed = False
        self._home: Optional[Tuple[float, float]] = None
        self._front_block = False
        self._clear_since: Optional[float] = None
        self._ramp_v = Ramp(p.max_accel, p.max_jerk)
        self._ramp_w = Ramp(p.max_turn_accel)
        self._tick_t: Optional[float] = None
        self._moving_since: Optional[float] = None
        self._motion_over = False
        self._rev_used = 0.0
        self._override_since: Optional[float] = None
        self._cut = False

    # ---- входы

    def request(self, now: float, v: float, w: float) -> None:
        """Желаемая скорость исполнителя; нечисловая — явный ноль (NaN обходил бы любые сравнения)."""
        if not (math.isfinite(v) and math.isfinite(w)):
            self.counters["invalid"] += 1
            v = w = 0.0
        self._req = (now, v, w)

    def error(self, sensor: str, text: str) -> None:
        """Вход не принят (нет TF, негодное сообщение): он устареет, причина попадёт в reasons."""
        self._errors[sensor] = text

    def _fresh(self, sensor: str, now: float) -> None:
        self._t[sensor] = now
        self._errors.pop(sensor, None)

    def scan(self, now: float, ranges: Sequence[float], meta: ScanMeta, laser: Pose) -> Optional[str]:
        """Скан с позой лидара в базе; причина отказа (скан не принят и устареет) или None."""
        why = check_meta(meta) or (None if len(ranges) == meta.n else "лучей {} при n {}".format(len(ranges), meta.n))
        if why:
            self.error("scan", why)
            return why
        key = (meta, laser.rot.tobytes(), laser.pos.tobytes())
        if key != self._rays_key:
            self._rays, self._rays_key = Rays(meta, laser), key
        p, rays, learner = self.p, self._rays, self.learner
        blind, beyond = self_hits(ranges, rays, p.robot_radius)
        if learner.mask is not None and len(learner.mask) == meta.n:
            freed = learner.watch(beyond)
            if freed.any():
                self.mask_deg = mask_spans(learner.mask, rays)
                log.warning("лучи %s дают попадание дальше корпуса %d сканов подряд — сняты с маски, маска: %s",
                            mask_spans(freed, rays), p.mask_scans, self.mask_deg or "пусто")
        elif learner.feed(blind, rays.sector(p.mask_from_deg, p.mask_to_deg)) is not None:
            self.mask_deg = mask_spans(learner.mask, rays)
            self.outside_deg = mask_spans(learner.outside & rays.sector(-90.0, 90.0), rays)  # сзади — не важно
            log.info("маска лидара по %d сканам: %s", learner.seen, self.mask_deg or "пусто")
            if self.outside_deg:
                log.warning("на старте «нет данных» впереди вне сектора корпуса %g…%g°: %s — не в маске",
                            p.mask_from_deg, p.mask_to_deg, self.outside_deg)
        t_depth = self._t["depth"]
        free = self._free if t_depth is not None and now - t_depth <= p.depth_timeout else None
        self.lidar = None if learner.mask is None else view(ranges, rays, learner.mask, p, free)
        self._fresh("scan", now)
        return None

    def odom(self, now: float, x: float, y: float, yaw: float, v: float, w: float) -> None:
        if self._pose is not None:
            step = math.hypot(x - self._pose[0], y - self._pose[1])
            if step > ODOM_JUMP_M:
                # одометрию сбросили (рестарт базы): память глубины и точка старта к новой системе не относятся
                log.warning("одометрия прыгнула на %.2f м — память глубины сброшена, геозабор — от этой точки", step)
                self.memory = Memory(self.p)
                self._home = (x, y)
            else:
                # задний ход тратит бюджет, ход вперёд возвращает: назад — не дальше самой дальней точки пути
                fwd = (x - self._pose[0]) * math.cos(self._pose[2]) + (y - self._pose[1]) * math.sin(self._pose[2])
                self._rev_used = max(0.0, self._rev_used - fwd)
        self._pose, self._odom_vw = (x, y, yaw), (v, w)
        self._fresh("odom", now)

    def depth(self, now: float, hazards: Hazards, free: Optional[FreeSpace] = None) -> None:
        """Опасности кадра глубины в базе; кладутся в память по текущей позе одометрии. Кадр, который не видит
        коридор вперёд (покрытие ниже depth_min_valid), не принят: глубина устареет. free — тот же кадр как проверка
        «точка свободна» для неизвестных лучей лидара."""
        if self._pose is None:
            self.error("depth", "нет одометрии — память глубины не к чему привязать")
            return
        if not hazards.coverage >= self.p.depth_min_valid:
            self.error("depth", "кадр видит коридор вперёд на {:.0%} — нужно не меньше {:.0%} в каждой полосе".format(
                hazards.coverage, self.p.depth_min_valid))
            return
        self.memory.update(self._pose, hazards)
        self._free = free
        self._fresh("depth", now)

    def imu_tilt(self, now: float, deg: float) -> None:
        self.tilt = deg
        self._fresh("imu", now)
        if deg > self.p.tilt_max_deg:
            self._engage("tilt", "наклон {:.1f}° > {:g}°".format(deg, self.p.tilt_max_deg))

    def imu_accel(self, now: float, accel: Sequence[float]) -> None:
        dev = self._shock.feed(now, accel)
        self._fresh("accel", now)
        if dev > self.p.shock_mps2:
            self._engage("shock", "удар: всплеск ускорения {:.1f} м/с² > {:g}".format(dev, self.p.shock_mps2))

    def mux(self, now: float, v: float, w: float) -> None:
        self._mux = (now, v, w)

    def graph(self, foreign: Sequence[str], overrides: Sequence[str]) -> None:
        """Чужие писатели /cmd_vel — защёлка; издатели входов twist_mux главнее /cmd_vel — по override_policy."""
        self._foreign, self._overrides = tuple(foreign), tuple(overrides)
        if foreign:
            self._engage("foreign_writer", "в /cmd_vel пишет ещё {}".format(", ".join(foreign)))
        if overrides and self.p.override_policy == "estop":
            self._engage("override", "есть издатели {} (twist_mux ставит их выше /cmd_vel)".format(
                ", ".join(overrides)))

    def estop(self, wall: float, cmd: EstopCmd) -> str:
        """Команда /neurobot/estop; итог для журнала. engage — кто угодно; release — только человек."""
        if cmd.id and cmd.id in self._ids:
            return "повтор {}".format(cmd.id)
        if cmd.id:
            self._ids.append(cmd.id)
        if cmd.cmd == "engage":
            text = "{}: {}".format(cmd.by, cmd.reason) if cmd.reason else cmd.by
            return "поставлена" if self._engage("estop", text) else "уже стоит"
        why = self._release_denied(wall, cmd)
        if why:
            self.counters["release_denied"] += 1
            return "не снята: " + why
        if not self.latch.engaged:
            return "защёлки нет"
        self.latch.release()
        self.counters["estop_released"] += 1
        self._armed = False
        return "снята"

    def _release_denied(self, wall: float, cmd: EstopCmd) -> Optional[str]:
        p = self.p
        if cmd.by != "human":
            return "снимает только человек (by=human, CLI neurobot estop), пришло by={}".format(cmd.by)
        if cmd.t is None or abs(wall - cmd.t) > RELEASE_MAX_AGE_S:
            return "команда без времени или старше {:g} с".format(RELEASE_MAX_AGE_S)
        if self.tilt is not None and self.tilt > p.tilt_max_deg:
            return "наклон ещё {:.1f}°".format(self.tilt)
        if self._foreign:
            return "в /cmd_vel ещё пишет {}".format(", ".join(self._foreign))
        v, w = self._odom_vw
        if abs(v) >= p.stall_odom or abs(w) >= p.stall_turn_odom:
            return "база ещё движется"
        return None

    def _engage(self, code: str, text: str) -> bool:
        if not self.latch.engage(code, text):
            return False
        self.counters["estop_engaged"] += 1
        self._armed = False
        log.warning("защёлка: %s: %s", code, text)
        return True

    # ---- решение

    def _stale(self, now: float) -> list:
        p = self.p
        out = []
        for sensor, limit, name in (("scan", p.scan_timeout, "/scan"), ("odom", p.odom_timeout, "/odom"),
                                    ("imu", p.imu_timeout, "IMU (ориентация)"),
                                    ("accel", p.imu_timeout, "IMU (ускорение)"), ("depth", math.inf, "глубина")):
            t = self._t[sensor]
            if t is None or now - t > limit:
                what = "нет данных" if t is None else "{:.2f} с без данных".format(now - t)
                err = self._errors.get(sensor)
                out.append("stale_{}: {} — {}{}".format(sensor, name, what, " ({})".format(err) if err else ""))
        if self._t["scan"] is not None and now - self._t["scan"] <= p.scan_timeout and self.lidar is None:
            out.append("lidar_mask: учим маску слепых лучей, {}/{} сканов".format(self.learner.seen, p.mask_scans))
        if self._motion_over:
            out.append("motion_time: непрерывное движение дольше {:g} с — ждём нулевой запрос".format(p.max_motion_s))
        if self._overrides and p.override_policy == "block":
            out.append("override: есть издатели {}".format(", ".join(self._overrides)))
        return out

    def _depth_view(self) -> Tuple[float, float, bool, bool]:
        """(путь вперёд, зазор, касание, ближайшее вперёди — обрыв) по памяти глубины."""
        p = self.p
        pts, drop = self.memory.in_base(self._pose)
        if not len(pts):
            return math.inf, math.inf, False, False
        x, y = pts[:, 0], pts[:, 1]
        centre = np.hypot(x, y) - p.robot_radius
        half = p.cell_m * math.sqrt(0.5)  # клетка — квадрат; до её ближнего угла
        corridor = (x > 0.0) & (np.abs(y) <= p.robot_radius + p.lateral_margin + p.cell_m / 2)
        front, is_drop = math.inf, False
        if corridor.any():
            d = edge_distance(x[corridor], y[corridor], p.robot_radius) - half
            k = int(np.argmin(d))
            front, is_drop = max(float(d[k]), 0.0), bool(drop[corridor][k])
        return front, max(float(np.min(centre)) - half, 0.0), float(np.min(centre)) < p.turn_margin, is_drop

    def tick(self, now: float) -> Decision:
        p = self.p
        dt = 0.0 if self._tick_t is None else min(max(now - self._tick_t, 0.0), 0.2)
        self._tick_t = now
        t_req, rv, rw = self._req
        if t_req is None or now - t_req > p.req_timeout:
            rv = rw = 0.0
        if self._motion_over and rv == 0.0 and rw == 0.0:
            self._motion_over = False
        stop = list(self.latch.reasons) + self._stale(now)
        reasons: list = []
        warnings: list = []
        v_max, v_rev, w_max = p.max_speed, p.reverse_speed, p.max_turn
        front_m = clearance = math.inf
        if stop:
            self._armed = False
            v_max = v_rev = w_max = 0.0
            reasons.extend(stop)
        else:
            lid = self.lidar
            d_front, d_clear, d_touch, d_drop = self._depth_view()
            front_m, clearance = min(lid.front_m, d_front), min(lid.clearance_m, d_clear)
            v_max = self._front_limit(now, front_m, lid.front_m <= d_front, d_drop, reasons)
            if lid.nodata:
                v_max = 0.0
                reasons.append("lidar_nodata: нет данных лидара перед роботом, {}…{}° — участок в коридоре за "
                               "корпусом, глубина его не видит".format(*lid.nodata))
            elif lid.unknown:
                warnings.append("lidar_unknown: нет данных лидара {} — в коридор за корпус не заходят (у плеча) или "
                                "видны глубиной свободными".format(lid.unknown))
            depth_ok = now - self._t["depth"] <= p.depth_timeout
            if not depth_ok and (lid.blind or p.depth_stale == "block"):
                v_max = 0.0
                err = self._errors.get("depth")
                reasons.append("stale_depth: глубина {:.2f} с без данных{} — вперёд нельзя{}".format(
                    now - self._t["depth"], " ({})".format(err) if err else "",
                    " (перед в слепом секторе лидара)" if lid.blind else ""))
            elif (lid.blind or not depth_ok) and v_max > p.blind_speed:
                v_max = p.blind_speed
                reasons.append("lidar_blind: перед в слепом секторе лидара — только глубина, ≤ {:g} м/с".format(
                    p.blind_speed) if lid.blind else "stale_depth: глубина устарела, ≤ {:g} м/с".format(p.blind_speed))
            v_max, v_rev = self._geofence(v_max, v_rev, reasons)
            if v_rev > 0.0 and self._rev_used >= p.reverse_dist:
                v_rev = 0.0
                reasons.append("reverse_limit: назад от дальней точки пути уже {:.2f} м (предел {:g} м) — сначала "
                               "вперёд".format(self._rev_used, p.reverse_dist))
            if lid.touch or d_touch:
                w_max = 0.0
                reasons.append("touch: препятствие у корпуса ({}) — поворот на месте заденет".format(
                    "лидар" if lid.touch else "глубина"))
            if not self._armed:
                if rv == 0.0 and rw == 0.0:
                    self._armed = True
                    if self._home is None:
                        self._home = self._pose[:2]
                else:
                    v_max = v_rev = w_max = 0.0
                    reasons.append("unarmed: ждём нулевой запрос — после старта, защёлки и стопа движение само не "
                                   "продолжается")

        tv = min(max(rv, -v_rev), v_max)
        tw = min(max(rw, -w_max), w_max)
        v, w = self._ramp_v.step(tv, dt), self._ramp_w.step(tw, dt)
        cut = (rv != 0.0 and tv == 0.0) or (rw != 0.0 and tw == 0.0)
        if cut and not self._cut:
            self.counters["cut"] += 1
        self._cut = cut
        self._watch_motion(now, v, w, warnings)
        if self.latch.engaged:  # защёлка могла встать на этом шаге (упор)
            v, w = self._ramp_v.step(0.0, dt), self._ramp_w.step(0.0, dt)
            v_max = v_rev = w_max = 0.0
            reasons = list(dict.fromkeys(list(self.latch.reasons) + reasons))
        if self.outside_deg:
            warnings.append("mask_outside: на старте «нет данных» впереди вне сектора корпуса {} — не в маске "
                            "(обстановка или камера сдвинута)".format(self.outside_deg))
        if self._overrides and p.override_policy == "warn":
            warnings.append("override: есть издатели {} — twist_mux ставит их выше /cmd_vel".format(
                ", ".join(self._overrides)))
        v, w = v + 0.0, w + 0.0  # без «−0.0» в /cmd_vel
        state = ESTOP if self.latch.engaged else BLOCKED if v_max == 0.0 else SLOW if v_max < p.max_speed else OK
        return Decision(v, w, state, tuple(reasons), tuple(warnings), v_max, v_rev, w_max, front_m, clearance,
                        (rv, rw), cut)

    def _front_limit(self, now: float, front_m: float, by_lidar: bool, drop: bool, reasons: list) -> float:
        """Предел вперёд по свободному пути с гистерезисом снятия блока (дребезг у порога)."""
        p = self.p
        allowed = allowed_speed(p, front_m)
        if self._front_block:
            if allowed_speed(p, front_m - p.release_hyst) >= p.min_speed:
                if self._clear_since is None:
                    self._clear_since = now
                if now - self._clear_since >= p.release_s:
                    self._front_block = False
            else:
                self._clear_since = None
        elif allowed < p.min_speed:
            self._front_block, self._clear_since = True, None
        what = "лидар" if by_lidar else "глубина, край пола" if drop else "глубина"
        code = "cliff" if drop and not by_lidar else "obstacle"
        if self._front_block:
            reasons.append("{}: {} — путь {:.2f} м от края корпуса, вперёд нельзя".format(code, what, front_m))
            return 0.0
        if allowed < p.max_speed:
            reasons.append("approach: {} — путь {:.2f} м, ≤ {:.2f} м/с".format(what, front_m, allowed))
            return allowed
        return p.max_speed

    def _geofence(self, v_max: float, v_rev: float, reasons: list) -> Tuple[float, float]:
        p = self.p
        if not (p.geofence_m > 0.0 and self._home is not None):
            return v_max, v_rev
        x, y, yaw = self._pose
        dx, dy = x - self._home[0], y - self._home[1]
        dist = math.hypot(dx, dy)
        if dist < p.geofence_m:
            return v_max, v_rev
        outward = dx * math.cos(yaw) + dy * math.sin(yaw)
        reasons.append("geofence: {:.2f} м от точки старта (предел {:g} м) — только к ней".format(dist, p.geofence_m))
        return (0.0 if outward > 0.0 else v_max), (0.0 if outward < 0.0 else v_rev)

    def _watch_motion(self, now: float, v: float, w: float, warnings: list) -> None:
        """Время непрерывного движения, упор (защёлка), перехват базы входом главнее /cmd_vel (предупреждение)."""
        p = self.p
        if v != 0.0 or w != 0.0:
            if self._moving_since is None:
                self._moving_since = now
            if now - self._moving_since > p.max_motion_s:
                self._motion_over = True
        else:
            self._moving_since = None
        t_mux, mv, mw = self._mux
        if not p.base_cmd_topic:
            delivered = True
        else:
            delivered = (t_mux is not None and now - t_mux <= MUX_FRESH_S
                         and abs(mv - v) <= MUX_TOL[0] and abs(mw - w) <= MUX_TOL[1])
            if (v != 0.0 or w != 0.0) and not delivered:
                if self._override_since is None:
                    self._override_since = now
                if now - self._override_since > OVERRIDE_S:
                    warnings.append("override_active: twist_mux отдаёт базе не нашу команду")
            else:
                self._override_since = None
        t_odom = self._t["odom"]
        odom_fresh = t_odom is not None and now - t_odom <= p.odom_timeout
        if odom_fresh and self.stall.feed(now, v, w, *self._odom_vw, delivered):
            self._engage("stall", "упор: команда ({:.2f} м/с, {:.2f} рад/с) дошла до базы, а одометрия стоит {:g} с"
                         .format(v, w, p.stall_s))
