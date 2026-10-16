"""Камера RealSense с восстановлением для predict_live v2 (R1, R2) — без torch и rclpy.

Демо ждало кадр wait_for_frames() (5 с, затем RuntimeError, поток «зрения» умирал, процесс оставался жив и
глух). Здесь кадр ждётся try_wait_for_frames(timeout_ms) (по умолчанию 1 с), а «нет кадра» — это таймаут,
RuntimeError драйвера или застывший поток (номер кадра не меняется). Чёрный кадр с новым номером — живая камера
в темноте: кадр хороший, лесенки нет, только признак dark и строка журнала. Когда хорошего кадра нет step_s
секунд, включается следующая ступень:
  1) перезапуск конвейера (stop → start);
  2) hardware_reset() всех устройств + пауза reset_pause_s → start;
  3) CameraFailed — процесс выходит с кодом, дальше перезапускает systemd.
Хороший кадр возвращает лесенку на ноль и считается восстановлением (recoveries), если она шла.

Камера занята (старт конвейера — «Device or resource busy»; на роботе ей обычно владеет realsense2_camera, R7a
N12): чужое устройство не сбрасываем. Вместо ступени 2 — CameraBusy, процесс выходит со своим кодом; кто держит
камеру — в журнале. open() сначала просто запускает конвейер; hardware_reset при старте (R2) — только если
запуск не удался не из-за «занято». Ошибка старта не роняет процесс: её ловит та же лесенка.

Модуль rs (pyrealsense2 или подставной стенда) передаётся явно; часы и пауза — тоже (для тестов).

TopicCamera (--camera ros) — кадры из темы ROS, когда камерой владеет другой узел: устройство не трогаем вовсе.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, Iterator, List, Optional

import numpy as np

from node_util import LatestSlot

log = logging.getLogger(__name__)

# Средняя яркость редкой сетки кадра (из 255), ниже которой кадр «тёмный»; обратно — на DARK_HYST выше, чтобы
# журнал не дребезжал у порога [оценка: закрытый объектив и выключенный свет дают единицы]
DARK_LEVEL = 8.0
DARK_HYST = 4.0
CAMERA_DEVICES = ("/dev/video", "/dev/bus/usb/")  # узлы V4L2 и libusb (сборка librealsense с RSUSB)
TOPIC_ENCODINGS = ("bgr8", "rgb8")  # rgb8 — у realsense2_camera и моста камеры Gazebo


class CameraFailed(Exception):
    """Лесенка восстановления исчерпана."""


class CameraBusy(CameraFailed):
    """Камерой владеет другой процесс: hardware_reset чужого устройства не делаем."""


def _fd_targets(fd_dir: str) -> Iterator[str]:
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        return
    for fd in fds:
        try:
            yield os.readlink(os.path.join(fd_dir, fd))
        except OSError:
            continue


def holders(proc: str = "/proc", own_pid: Optional[int] = None) -> List[str]:
    """Кандидаты во владельцы камеры — «имя (pid)» процессов, кроме своего, с открытыми /dev/video* или
    /dev/bus/usb/*. Процессы, чьи /proc/<pid>/fd не читаются (другой пользователь), не видны."""
    own = str(os.getpid() if own_pid is None else own_pid)
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit() and p != own]
    except OSError:
        return []
    found = []
    for pid in pids:
        if not any(t.startswith(CAMERA_DEVICES) for t in _fd_targets(os.path.join(proc, pid, "fd"))):
            continue
        try:
            with open(os.path.join(proc, pid, "comm"), encoding="utf-8", errors="replace") as fh:
                name = fh.read().strip()
        except OSError:
            name = "?"
        found.append("{} ({})".format(name, pid))
    return sorted(found)


def is_dark(img: np.ndarray, was_dark: bool) -> bool:
    """Тёмный ли кадр: средняя яркость редкой сетки ниже DARK_LEVEL (обратно — от DARK_LEVEL + DARK_HYST).
    Смена — строка журнала."""
    level = float(img[::8, ::8].mean())  # редкая сетка: ≈14 тыс. значений на кадр 640×480
    dark = level < DARK_LEVEL + (DARK_HYST if was_dark else 0.0)
    if dark and not was_dark:
        log.warning("кадр тёмный (средняя яркость %.1f из 255): камера работает, но руку не найти — свет, "
                    "крышка объектива?", level)
    elif was_dark and not dark:
        log.info("кадр снова светлый (средняя яркость %.1f)", level)
    return dark


class CameraGuard:
    def __init__(self, rs, width: int = 640, height: int = 480, fps: int = 30, timeout_ms: int = 1000,
                 step_s: float = 3.0, reset_pause_s: float = 3.0, configure: Optional[Callable] = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        if not (timeout_ms > 0 and step_s > 0 and reset_pause_s >= 0):
            raise ValueError("камера: нужно timeout_ms > 0, step_s > 0, reset_pause_s ≥ 0")
        self.rs = rs
        self.width, self.height, self.fps = width, height, fps
        self.timeout_ms = timeout_ms
        self.step_s = step_s
        self.reset_pause_s = reset_pause_s
        self.configure = configure
        self.clock = clock
        self.sleep = sleep
        self.pipe = None
        self.level = 0          # 0 — норма; 1 — конвейер перезапущен; 2 — после hardware_reset
        self.frames = 0         # хорошие кадры — прогресс для WATCHDOG и fps
        self.recoveries = 0     # восстановления без рестарта процесса
        self.dark = False       # камера работает, но кадр тёмный (в /neurobot/perception_health)
        self.last_good = clock()
        self._since = self.last_good  # начало текущей ступени ожидания
        self._number = None     # номер прошлого кадра; рестарт его не сбрасывает — тот же застывший кадр после
        self._failing = False   # рестарта не считается восстановлением
        self._busy = False      # последний старт конвейера: камера занята другим процессом

    def open(self, reset: bool = True) -> None:
        self._start()
        if self.pipe is None and reset and not self._busy:
            self._hardware_reset()
            self._start()
        self.last_good = self._since = self.clock()

    def close(self) -> None:
        self._stop()

    def frame_age(self) -> float:
        return self.clock() - self.last_good

    def read(self) -> Optional[np.ndarray]:
        """Кадр BGR или None (кадра нет — лесенка двигается сама). CameraFailed — ступени кончились."""
        img = self._grab()
        now = self.clock()
        if img is not None:
            if self.level:
                self.recoveries += 1
                log.warning("камера восстановлена на ступени %d, без кадров было %.1f с", self.level,
                            now - self.last_good)
            self.level = 0
            self._failing = False
            self.frames += 1
            self.last_good = self._since = now
            return img
        if now - self._since >= self.step_s:
            self._escalate(now)
        return None

    def _grab(self) -> Optional[np.ndarray]:
        if self.pipe is None:
            self.sleep(self.timeout_ms / 1000)
            return None
        try:
            ok, frames = self.pipe.try_wait_for_frames(self.timeout_ms)
            color = frames.get_color_frame() if ok else None
            if not color:
                return None
            number = color.get_frame_number()
            img = np.asanyarray(color.get_data())
        except RuntimeError as e:
            if not self._failing:
                log.warning("кадр не получен: %s", e)
            self._failing = True
            self.sleep(min(0.1, self.timeout_ms / 1000))  # драйвер может бросать сразу — без холостого цикла
            return None
        if number == self._number:
            return None  # застывший поток: драйвер отдаёт тот же кадр
        self._number = number
        self.dark = is_dark(img, self.dark)
        return img

    def _escalate(self, now: float) -> None:
        stall = now - self.last_good
        if self.level == 0:
            log.warning("нет кадров %.1f с — перезапуск конвейера", stall)
            self._stop()
            self._start()
        elif self._busy:
            raise CameraBusy("камера занята другим процессом {:.1f} с — hardware_reset чужого устройства не "
                             "делаем".format(stall))
        elif self.level == 1:
            log.warning("нет кадров %.1f с — hardware_reset и пауза %.1f с", stall, self.reset_pause_s)
            self._stop()
            self._hardware_reset()
            self._start()
        else:
            raise CameraFailed("нет кадров {:.1f} с после перезапуска конвейера и hardware_reset".format(stall))
        self.level += 1
        self._since = self.clock()

    def _start(self) -> None:
        rs = self.rs
        pipe = rs.pipeline()
        cfg = rs.config()
        cfg.enable_stream(rs.stream.color, self.width, self.height, rs.format.bgr8, self.fps)
        try:
            profile = pipe.start(cfg)
        except RuntimeError as e:
            self._busy = "busy" in str(e).lower()
            if self._busy:
                # N12: на роботе камерой обычно владеет узел realsense2_camera — владелец должен быть один
                log.error("конвейер камеры не запустился: камера занята другим процессом (держат: %s): %s",
                          ", ".join(holders()) or "не видно", e)
            else:
                log.exception("конвейер камеры не запустился")
            return
        self._busy = False
        self.pipe = pipe
        if self.configure is not None:
            try:
                self.configure(profile)
            except Exception:
                log.exception("настройка камеры не удалась, работаем с настройками устройства")

    def _stop(self) -> None:
        pipe, self.pipe = self.pipe, None
        if pipe is not None:
            try:
                pipe.stop()
            except RuntimeError as e:
                log.warning("остановка конвейера: %s", e)

    def _hardware_reset(self) -> None:
        try:
            devices = list(self.rs.context().query_devices())
            for dev in devices:
                dev.hardware_reset()
        except RuntimeError:
            log.exception("hardware_reset не удался")
            return
        log.info("hardware_reset: устройств %d, пауза %.1f с", len(devices), self.reset_pause_s)
        self.sleep(self.reset_pause_s)


class TopicCamera:
    """Кадры из темы ROS sensor_msgs/Image (--camera ros): камерой владеет другой узел — на роботе realsense2_camera
    (его глубина и IMU нужны safety_gate, PLAN §13.3), в двойнике — камера Gazebo. Устройство не трогаем: ни
    перезапуска конвейера, ни hardware_reset, ни выхода — восстанавливает владелец (и его супервизор).

    on_image — колбэк подписки (поток rclpy), read — поток «зрения»: ждёт сообщение до timeout_ms, непрочитанное
    вытесняется новым. Кадр — сообщение с новым header.stamp; тот же stamp (застывший издатель) — не кадр. Нового
    кадра нет step_s — level 1 (в /neurobot/perception_health — recovering) и строка журнала; кадр вернулся —
    восстановление (recoveries). Кодировки — TOPIC_ENCODINGS, размер — width×height; другое — не кадр и строка
    журнала. Кадр — BGR, как у CameraGuard (поток bgr8 лаборатории); тёмный кадр — признак dark, как там же.
    """

    def __init__(self, width: int = 640, height: int = 480, timeout_ms: int = 1000, step_s: float = 3.0,
                 clock: Callable[[], float] = time.monotonic):
        if not (timeout_ms > 0 and step_s > 0):
            raise ValueError("камера: нужно timeout_ms > 0, step_s > 0")
        self.width, self.height = width, height
        self.timeout_ms = timeout_ms
        self.step_s = step_s
        self.clock = clock
        self.level = 0          # 0 — кадры идут; 1 — нового кадра нет дольше step_s
        self.frames = 0
        self.recoveries = 0
        self.dark = False
        self.last_good = clock()
        self._slot = LatestSlot()
        self._stamp = None      # (sec, nanosec) прошлого кадра
        self._bad = None        # почему не подошло прошлое сообщение: журнал — раз на серию

    def on_image(self, msg) -> None:
        self._slot.put(msg)

    def close(self) -> None:
        """Устройством владеет другой узел — закрывать нечего."""

    def frame_age(self) -> float:
        return self.clock() - self.last_good

    def read(self) -> Optional[np.ndarray]:
        """Кадр BGR или None. CameraFailed не бывает: без кадров ждём издателя."""
        msg = self._slot.take(self.timeout_ms / 1000)
        img = None if msg is None else self._decode(msg)
        now = self.clock()
        if img is not None:
            if self.level:
                self.recoveries += 1
                log.warning("кадры из темы снова идут, без кадров было %.1f с", now - self.last_good)
            self.level = 0
            self.frames += 1
            self.last_good = now
            return img
        if not self.level and now - self.last_good >= self.step_s:
            self.level = 1
            log.warning("нет новых кадров из темы (сообщений с новым header.stamp) %.1f с — ждём издателя: камерой "
                        "владеет другой узел, устройство не трогаем", now - self.last_good)
        return None

    def _decode(self, msg) -> Optional[np.ndarray]:
        stamp = (msg.header.stamp.sec, msg.header.stamp.nanosec)
        if stamp == self._stamp:
            return None  # застывший издатель: тот же кадр
        w, h, step = msg.width, msg.height, msg.step
        bad = None
        if msg.encoding not in TOPIC_ENCODINGS:
            bad = "кодировка {} (нужна {})".format(msg.encoding, " или ".join(TOPIC_ENCODINGS))
        elif (w, h) != (self.width, self.height):
            bad = "размер {}×{} (нужен {}×{} — --width, --height)".format(w, h, self.width, self.height)
        elif step < 3 * w or len(msg.data) < h * step:
            bad = "step {} и {} байт на кадр {}×{}".format(step, len(msg.data), w, h)
        if bad is not None:
            if bad != self._bad:
                log.error("кадр из темы не подходит: %s", bad)
            self._bad = bad
            return None
        self._bad = None
        self._stamp = stamp
        img = np.frombuffer(msg.data, np.uint8, h * step).reshape(h, step)[:, :3 * w].reshape(h, w, 3)
        if msg.encoding == "rgb8":
            img = img[:, :, ::-1]
        img = np.ascontiguousarray(img)
        self.dark = is_dark(img, self.dark)
        return img
