"""Запись наблюдателя без ROS: сообщения тем → компактные события JSONL (их разбирает analyze.py).

У каждого события t — time.time() и m — CLOCK_MONOTONIC (clock.mono()) в момент приёма (часы ВМ sim; m — для
сопоставления с якорем таймлайна, t — для меток в сообщениях perception). Поток сжимается:
  cmd    — серия одинаковых команд темы {topic, t0, m0, t1, m1, n, x, z}; серия закрывается при смене
           значения или тишине дольше run_gap_s (тишина важна для «едет без свежей команды»);
  motion — переход «едет / стоит» по источнику (odom | gt): |v| > v_thr или |ω| > w_thr;
  pose   — истинная поза раз в pose_period_s {x, y, yaw, d — до ближайшего препятствия};
  wall   — переход касания препятствия {touch, d, wall} (контур робота — прямоугольник или радиус, walls.py);
  rtf    — отношение сим. времени к стенному за окно {src, rtf, wall_s, sim_s};
  gesture, health — JSON из /neurobot/gesture и /neurobot/perception_health как есть (msg) или {raw, error};
  cmd_type_mismatch — у темы скоростей есть издатели другого типа, чем подписка (Twist против TwistStamped):
           DDS такую пару не соединяет, и без этого события в записи просто не было бы команд {topic, ours, theirs}.
"""
from __future__ import annotations

import json
import math

from .timeline import rnd
from .walls import FOOTPRINT_TB3_WAFFLE_PI, nearest_wall

EPS = 1e-3


class CmdRuns:
    def __init__(self, topic: str, gap_s: float = 0.25):
        self.topic = topic
        self.gap_s = gap_s
        self._run = None

    def _close(self) -> list:
        run, self._run = self._run, None
        return [run] if run else []

    def feed(self, t: float, m: float, x: float, z: float) -> list:
        x = 0.0 if abs(x) <= EPS else x
        z = 0.0 if abs(z) <= EPS else z
        out = []
        run = self._run
        if run and (run["x"] != rnd(x, 4) or run["z"] != rnd(z, 4) or m - run["m1"] > self.gap_s):
            out = self._close()
        if self._run is None:
            self._run = {"ev": "cmd", "topic": self.topic, "t0": rnd(t), "m0": rnd(m), "t1": rnd(t), "m1": rnd(m),
                         "n": 0, "x": rnd(x, 4), "z": rnd(z, 4)}
        self._run.update(t1=rnd(t), m1=rnd(m), n=self._run["n"] + 1)
        return out

    def flush(self, m: float) -> list:
        """Закрыть серию, если тема молчит дольше gap_s."""
        if self._run and m - self._run["m1"] > self.gap_s:
            return self._close()
        return []

    def close(self) -> list:
        return self._close()


class MotionTracker:
    def __init__(self, src: str, v_thr: float = 0.05, w_thr: float = 0.1):
        self.src = src
        self.v_thr = v_thr
        self.w_thr = w_thr
        self.moving = False

    def feed(self, t: float, m: float, v: float, w: float, stamp: float):
        moving = abs(v) > self.v_thr or abs(w) > self.w_thr
        if moving == self.moving:
            return None
        self.moving = moving
        return {"ev": "motion", "src": self.src, "t": rnd(t), "m": rnd(m), "moving": moving, "v": rnd(v, 4),
                "w": rnd(w, 4), "stamp": rnd(stamp)}


class PoseVelocity:
    """Скорости по последовательным позам (интервал — по меткам header.stamp, т. е. сим. время)."""

    def __init__(self):
        self._prev = None

    def feed(self, x: float, y: float, yaw: float, stamp: float):
        prev, self._prev = self._prev, (x, y, yaw, stamp)
        if prev is None or stamp <= prev[3]:
            return None
        dt = stamp - prev[3]
        dyaw = math.atan2(math.sin(yaw - prev[2]), math.cos(yaw - prev[2]))
        return math.hypot(x - prev[0], y - prev[1]) / dt, dyaw / dt


class RtfMeter:
    def __init__(self, src: str, window_s: float = 10.0):
        self.src = src
        self.window_s = window_s
        self._start = None

    def feed(self, t: float, m: float, stamp: float):
        if self._start is None or stamp < self._start[1]:
            self._start = (m, stamp)
            return None
        wall = m - self._start[0]
        if wall < self.window_s:
            return None
        sim = stamp - self._start[1]
        self._start = (m, stamp)
        return {"ev": "rtf", "src": self.src, "t": rnd(t), "m": rnd(m), "rtf": rnd(sim / wall, 4),
                "wall_s": rnd(wall, 3), "sim_s": rnd(sim, 3)}


class WallTracker:
    def __init__(self, walls: list, fp=FOOTPRINT_TB3_WAFFLE_PI, touch_m: float = 0.02):
        self.walls = walls
        self.fp = fp
        self.touch_m = touch_m
        self.touch = False

    def feed(self, t: float, m: float, x: float, y: float, yaw: float):
        """(расстояние до ближайшей стены, событие перехода касания или None)."""
        d, name = nearest_wall(self.walls, x, y, yaw, self.fp)
        touch = d <= self.touch_m
        if touch == self.touch:
            return d, None
        self.touch = touch
        return d, {"ev": "wall", "t": rnd(t), "m": rnd(m), "touch": touch, "d": rnd(d, 4), "wall": name}


class Recorder:
    """Сводит трекеры; sink(dict) получает готовые события по мере их появления."""

    def __init__(self, sink, cmd_topics=("/cmd_vel",), walls=(), fp=FOOTPRINT_TB3_WAFFLE_PI, v_thr: float = 0.05,
                 w_thr: float = 0.1, touch_m: float = 0.02, pose_period_s: float = 0.5, rtf_window_s: float = 10.0,
                 run_gap_s: float = 0.25):
        self.sink = sink
        self.runs = {topic: CmdRuns(topic, run_gap_s) for topic in cmd_topics}
        self.motion = {src: MotionTracker(src, v_thr, w_thr) for src in ("odom", "gt")}
        self.rtf = {src: RtfMeter(src, rtf_window_s) for src in ("odom", "gt")}
        self.gt_vel = PoseVelocity()
        self.wall = WallTracker(list(walls), fp, touch_m) if walls else None
        self.pose_period_s = pose_period_s
        self.mismatched = set()
        self._last_pose = -math.inf

    def _emit(self, events) -> None:
        for e in events:
            if e is not None:
                self.sink(e)

    def on_cmd(self, topic: str, t: float, m: float, x: float, z: float) -> None:
        self._emit(self.runs[topic].feed(t, m, x, z))

    def on_odom(self, t: float, m: float, v: float, w: float, stamp: float) -> None:
        self._emit([self.motion["odom"].feed(t, m, v, w, stamp), self.rtf["odom"].feed(t, m, stamp)])

    def on_gt(self, t: float, m: float, x: float, y: float, yaw: float, stamp: float) -> None:
        events = [self.rtf["gt"].feed(t, m, stamp)]
        vel = self.gt_vel.feed(x, y, yaw, stamp)
        if vel is not None:
            events.append(self.motion["gt"].feed(t, m, vel[0], vel[1], stamp))
        d = None
        if self.wall is not None:
            d, ev = self.wall.feed(t, m, x, y, yaw)
            events.append(ev)
        if m - self._last_pose >= self.pose_period_s:
            self._last_pose = m
            events.append({"ev": "pose", "t": rnd(t), "m": rnd(m), "x": rnd(x, 4), "y": rnd(y, 4), "yaw": rnd(yaw, 4),
                           "d": rnd(d, 4)})
        self._emit(events)

    def on_json(self, kind: str, t: float, m: float, data: str) -> None:
        ev = {"ev": kind, "t": rnd(t), "m": rnd(m)}
        try:
            msg = json.loads(data)
            if not isinstance(msg, dict):
                raise ValueError("ожидался JSON-объект")
            ev["msg"] = msg
        except ValueError as e:
            ev.update(raw=data[:500], error=str(e))
        self.sink(ev)

    def on_publishers(self, topic: str, ours: str, theirs, t: float, m: float) -> bool:
        """Типы издателей темы скоростей; другой тип — событие (одно на тему). True — тема впервые не совпала."""
        other = sorted(set(theirs) - {ours})
        if not other or topic in self.mismatched:
            return False
        self.mismatched.add(topic)
        self.sink({"ev": "cmd_type_mismatch", "t": rnd(t), "m": rnd(m), "topic": topic, "ours": ours, "theirs": other})
        return True

    def tick(self, m: float) -> None:
        for runs in self.runs.values():
            self._emit(runs.flush(m))

    def close(self, t: float, m: float) -> None:
        for runs in self.runs.values():
            self._emit(runs.close())
        self.sink({"ev": "stop", "t": rnd(t), "m": rnd(m)})
