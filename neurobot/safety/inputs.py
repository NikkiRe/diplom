"""Входы датчиков safety_gate по header.stamp (H7; O5 H3, O3 п. 5): свежесть по штампу, застывший вход и замер
задержек — без rclpy.

Свежесть. «Сейчас − stamp» как возраст брать нельзя: на двойнике штампы — время симуляции (README двойника), а на
роботе смещение часов драйвера неизвестно. Поэтому возраст — задержка относительно самого быстрого из сообщений,
снятых за WINDOW_S до этого (по штампу), как односторонняя задержка в NTP: x = приход − stamp, lag = x − min x ≥ 0.
Постоянное смещение часов сокращается; остаётся то, на сколько сообщение пришло позже лучшего: очередь DDS, колбэк
другого входа впереди, задержка драйвера. Вход считается снятым в момент «приход − lag»: скан, который ждал, стареет
по штампу, а не считается свежим с момента приёма. Окно — по штампу, а не по приходу: драйвер встал и отдал
накопленное — первый скан пришёл через секунды после прошлого, но снят сразу после него, опора окна ещё есть, и
задержка видна при любой длине. Цена окна: задержка, которая держится дольше WINDOW_S по часам источника, перестаёт
быть видна (её покажет только age ниже; на роботе часы одни); медленный ход часов источника (RTF двойника ρ < 1)
завышает lag не больше чем на (1/ρ − 1)·WINDOW_S — 0,02 с при ρ = 0,98; после паузы источника (Gazebo на паузе)
lag — вся пауза, пока опора в окне: вход устарел, робот стоит до WINDOW_S.

Скачок часов источника назад больше чем на JUMP_S (рестарт Gazebo, шаг NTP, второй издатель) начинает отсчёт заново.
Скачок вперёд отсчёт не сбрасывает: опора старше WINDOW_S по новому штампу уходит из окна сама, а «штамп обогнал
приход» бывает и без скачка — у первого вовремя пришедшего сообщения после опоздавшего. Приход, отставший от штампа, —
не скачок, а задержка: она остаётся в lag.

Застывший вход (frozen): сообщение не принимается, и вход устаревает с причиной frozen —
- штамп тот же, что у прошлого принятого (застывший издатель шлёт тот же кадр), или меньше не больше чем на JUMP_S
  (старое сообщение после нового, малый шаг часов назад);
- frozen_n сообщений подряд с одинаковой сводкой измерений (CRC32): у измерений есть шум, совпадение невозможно.
  Сообщение без измерений (весь скан «нет данных», кадр без глубины) по содержимому не сравнивается: их совпадение
  естественно, а отказ ловят свои правила (lidar_fault, покрытие глубины).
Штамп 0 (драйвер его не ставит) — проверок по штампу нет, свежесть по приходу; об этом строка журнала.

Замер (O5 H3 п. 1–2): lag, возраст age = now_ros − stamp (если часы сравнимы, |age| < CLOCK_SANE_S) и длительность
колбэка — p50, p95 и max за последние WINDOW сообщений: в safety_state (timing) и в журнал.
"""
from __future__ import annotations

import collections
import logging
import zlib
from typing import Optional

import numpy as np

log = logging.getLogger("safety_gate")

WINDOW_S = 1.0        # с по штампу: окно самого быстрого сообщения
JUMP_S = 1.0          # с: штамп ушёл назад больше — скачок часов источника, отсчёт заново
CLOCK_SANE_S = 3600.0  # с: |now_ros − stamp| больше — часы источника и узла несравнимы (время симуляции)
WINDOW = 600          # сообщений в рядах замера (минута скана 10 Гц)


def stamp_s(stamp) -> float:
    """builtin_interfaces/Time → секунды."""
    return stamp.sec + stamp.nanosec * 1e-9


def digest(values) -> Optional[int]:
    """CRC32 измерений или None, если среди них нет ни одного конечного ненулевого (сравнивать нечего)."""
    a = np.ascontiguousarray(values)
    if not (np.isfinite(a) & (a != 0)).any():
        return None
    return zlib.crc32(a)


def _ms(series) -> Optional[list]:
    if not series:
        return None
    p50, p95 = np.percentile(np.fromiter(series, float, len(series)), (50, 95))
    return [round(p50 * 1e3, 1), round(p95 * 1e3, 1), round(max(series) * 1e3, 1)]


class StampWatch:
    """Свежесть и застывание одного входа; ряды lag, age и длительности колбэка для замера."""

    def __init__(self, name: str, frozen_n: int = 3):
        self.name = name
        self.frozen_n = frozen_n     # 0 — по содержимому не сравнивать
        self.lag = 0.0               # задержка последнего принятого по штампу сообщения, с
        self.frozen = 0              # сообщений не принято как застывшие
        self.resets = 0              # скачков часов источника
        self.lags = collections.deque(maxlen=WINDOW)
        self.ages = collections.deque(maxlen=WINDOW)
        self.callbacks = collections.deque(maxlen=WINDOW)
        self._fastest = collections.deque()  # (stamp, x) — кандидаты в минимум окна, x растёт
        self._prev: Optional[float] = None  # штамп последнего принятого сообщения
        self._digest: Optional[int] = None
        self._same = 0
        self._unstamped = False

    def stamp(self, arrival: float, stamp: float, now_ros: Optional[float] = None) -> Optional[str]:
        """Штамп сообщения (с) и момент прихода (монотонные с); причина frozen или None. После — self.lag."""
        self.lag = 0.0
        if stamp == 0.0:
            if not self._unstamped:
                log.warning("%s без header.stamp — свежесть по приходу, застывание по штампу не проверяется",
                            self.name)
                self._unstamped = True
            return None
        if self._prev is not None:
            d_stamp = stamp - self._prev
            if d_stamp < -JUMP_S:
                log.warning("%s: часы источника ушли назад на %.2f с — свежесть по штампу заново", self.name, -d_stamp)
                self._fastest.clear()
                self._digest, self._same = None, 0
                self.resets += 1
            elif d_stamp <= 0.0:
                self.frozen += 1
                return "frozen: штамп не вырос ({:+.3f} с к прошлому сообщению)".format(d_stamp)
        x = arrival - stamp
        fastest = self._fastest
        while fastest and fastest[-1][1] >= x:
            fastest.pop()
        fastest.append((stamp, x))
        while stamp - fastest[0][0] > WINDOW_S:
            fastest.popleft()
        self.lag = x - fastest[0][1]
        self._prev = stamp
        self.lags.append(self.lag)
        if now_ros is not None and abs(now_ros - stamp) < CLOCK_SANE_S:
            self.ages.append(now_ros - stamp)
        return None

    def content(self, value: Optional[int]) -> Optional[str]:
        """Сводка измерений сообщения (digest; None — измерений нет); причина frozen или None."""
        if not self.frozen_n or value is None or value != self._digest:
            self._digest, self._same = value, 1
            return None
        self._same += 1
        if self._same < self.frozen_n:
            return None
        self.frozen += 1
        return "frozen: одинаковые данные подряд: {}".format(self._same)

    def summary(self) -> dict:
        """p50, p95, max (мс) по рядам и счётчики — для safety_state и журнала."""
        return {"lag_ms": _ms(self.lags), "age_ms": _ms(self.ages), "callback_ms": _ms(self.callbacks),
                "frozen": self.frozen, "clock_resets": self.resets}
