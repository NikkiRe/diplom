"""Модель базы робота NeuroChip для стенда: что цепочка от /cmd_vel до колёс делает с командами скорости.

Без ROS — логика stm32_sim.py, проверяется тестами на Mac. Время — монотонные секунды вызывающего.

На роботе (R7: слайд 5, журнал PicoClaw 02.06) /cmd_vel идёт через twist_mux в diff_drive_controller (ros2_control,
Foxy), оттуда по UART в STM32. Режимы сторожа (timeout_s) — в отчётах A0/A1 каждый отдельно:
  0     — hold: последняя команда держится сколько угодно. Худший случай: умер controller_manager или аппаратный
          интерфейс, а своего сторожа у STM32 нет (на слайде 6 его нет). Так же ведёт себя gz DiffDrive;
  0,3   — просьба PLAN к лаборатории: таймаут 300 мс в мосте;
  0,5   — вероятное поведение робота: cmd_vel_timeout diff_drive_controller Foxy по умолчанию 0,5 с [код
          ros2_controllers foxy; на роботе не подтверждено — Э0: ros2 param get /diff_cont cmd_vel_timeout].
delay_s — чистая задержка команды до базы: кадр UART и 1–2 периода ПИД (R7b §5.5, [гипотеза до HIL]). Сторож
считает тишину от прихода команды на базу.
Неконечная команда (NaN, ±inf) отбрасывается, как битый кадр: цель и сторож не меняются, растёт счётчик rejected.

Пределы скорости и ускорения (0 — без предела) — [допущение]: команда трактуется в СИ (м/с, рад/с), как у
gz DiffDrive и diff_drive_controller. По R7b (слайды 5 и 12) Twist на роботе, по-видимому, в СИ, но до HIL это оценка.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Deque, Optional, Tuple


def ms_param(name: str, value) -> int:
    """Параметр в целых миллисекундах ≥ 0. Дробное (0,5 — секунды по ошибке) или true — ошибка, а не тихое усечение
    до 0 = hold."""
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0
            or value != int(value)):
        raise ValueError("%s — целые миллисекунды ≥ 0, получено %r" % (name, value))
    return int(value)


def num_param(name: str, value) -> float:
    """Конечное число ≥ 0 (целое из командной строки тоже подходит); true и строки — ошибка."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("%s — конечное число ≥ 0, получено %r" % (name, value))
    return float(value)


def _clamp(x: float, limit: float) -> float:
    return x if limit <= 0 else max(-limit, min(limit, x))


def _approach(cur: float, target: float, max_delta: Optional[float]) -> float:
    """Шаг к target не больше max_delta; None — без ограничения ускорения."""
    if max_delta is None:
        return target
    return cur + max(-max_delta, min(max_delta, target - cur))


class Stm32Model:
    def __init__(self, timeout_s: float = 0.0, max_lin: float = 0.0, max_ang: float = 0.0,
                 max_lin_acc: float = 0.0, max_ang_acc: float = 0.0, delay_s: float = 0.0):
        if not all(math.isfinite(v) and v >= 0 for v in (timeout_s, max_lin, max_ang, max_lin_acc, max_ang_acc,
                                                         delay_s)):
            raise ValueError("timeout, задержка и пределы — конечные и неотрицательные")
        self.timeout_s, self.delay_s = timeout_s, delay_s
        self.max_lin, self.max_ang = max_lin, max_ang
        self.max_lin_acc, self.max_ang_acc = max_lin_acc, max_ang_acc
        self.target: Tuple[float, float] = (0.0, 0.0)
        self.out: Tuple[float, float] = (0.0, 0.0)
        self.t_cmd: Optional[float] = None      # когда последняя команда дошла до базы
        self.t_step: Optional[float] = None
        self.stale = False      # сторож обнулил цель, новой команды ещё не было
        self.timeouts = 0       # сколько раз сторож остановил базу (тишина при ненулевой цели)
        self.rejected = 0       # отброшено неконечных команд
        self._queue: Deque[Tuple[float, float, float]] = deque()   # (когда дойдёт до базы, lin, ang)

    def command(self, lin: float, ang: float, now: float) -> bool:
        """Пришла команда (в том числе нулевая): через delay_s — новая цель, сторож взведён заново.
        False — значение неконечное, команда отброшена."""
        if not (math.isfinite(lin) and math.isfinite(ang)):
            self.rejected += 1
            return False
        self._queue.append((now + self.delay_s, lin, ang))
        return True

    def step(self, now: float) -> Tuple[float, float]:
        """Выход базы на момент now: дошедшие команды, сторож, затем разгон или торможение к цели."""
        while self._queue and self._queue[0][0] <= now:
            self.t_cmd, lin, ang = self._queue.popleft()
            self.target = (_clamp(lin, self.max_lin), _clamp(ang, self.max_ang))
            self.stale = False
        if (self.timeout_s > 0 and self.t_cmd is not None and not self.stale
                and now - self.t_cmd > self.timeout_s):
            self.stale = True
            if self.target != (0.0, 0.0):
                self.timeouts += 1
            self.target = (0.0, 0.0)
        dt = 0.0 if self.t_step is None else max(0.0, now - self.t_step)
        self.t_step = now
        lin_d = dt * self.max_lin_acc if self.max_lin_acc > 0 else None
        ang_d = dt * self.max_ang_acc if self.max_ang_acc > 0 else None
        self.out = (_approach(self.out[0], self.target[0], lin_d), _approach(self.out[1], self.target[1], ang_d))
        return self.out
