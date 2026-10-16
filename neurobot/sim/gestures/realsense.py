"""Подставной pyrealsense2: те имена, которые использует predict_live.py, и то, что нужно лесенке
восстановления perception v2 (R1 в ANALYSIS: try_wait_for_frames, hardware_reset, рестарт конвейера).

Пакет-заглушка `shim/pyrealsense2` реэкспортирует этот модуль; чтобы он заслонил настоящий, каталог
`neurobot/sim/gestures/shim` ставится в PYTHONPATH (корень проекта — тоже, для импорта отсюда).

Кадры — цветной поток W×H (по конфигу, в демо 640×480) bgr8/rgb8: серый фон и метка времени захвата
(clock.py). Темп — темп детекции, а не 30 к/с сенсора: запрошенная в enable_stream частота
игнорируется, берётся NEUROBOT_FAKE_RS_FPS (иначе det_fps из заголовка таймлайна, иначе 10). Семантика
«последний кадр»: медленный потребитель получает самый свежий кадр, пропущенные теряются. Номер кадра и
последний кадр — у устройства, а не у конвейера: рестарт конвейера их не сбрасывает.

Отказы (вид, параметры) — по расписанию или по команде:
  delay  (duration_s)  кадров нет duration_s; wait_for_frames(5000) бросает
                       RuntimeError("Frame didn't arrive within 5000"), если ждать дольше таймаута;
  stall  [duration_s]  кадров нет до рестарта конвейера (stop + start) после начала отказа;
  gone   [duration_s]  устройство «зависло»: кадров нет, рестарт конвейера не помогает; лечит только
                       device.hardware_reset(), после него устройства нет NEUROBOT_FAKE_RS_RESET_S
                       (2,5 с): query_devices() пуст, start() → RuntimeError("No device connected"),
                       а старый конвейер кадров больше не получит — нужен stop() и новый start();
  freeze (duration_s)  камера отдаёт один и тот же кадр (данные, timestamp, номер) любому конвейеру этого
                       процесса, в том числе новому после рестарта;
  black  (duration_s)  чёрные кадры (без метки — детектор ничего не видит);
  slow   (duration_s, fps) темп падает до fps.
Поле cure — чем отказ снимается раньше duration_s: none | restart (рестарт конвейера после начала отказа) |
reset (hardware_reset после начала отказа). У stall это всегда restart, у gone — reset, у остальных по
умолчанию none, и тогда нужна duration_s. Расписание NEUROBOT_FAKE_RS_FAULTS — JSON-список {at_s, kind, …};
at_s — секунды от якоря таймлайна (NEUROBOT_GESTURES_ANCHOR, без него расписание — ошибка при старте),
поэтому одно расписание одинаково ложится на A0 и A1 и переживает рестарт процесса; до появления якоря
расписание не действует. Якорь от прошлого прогона ловит FakeYolo того же процесса (anchor_stale), а
расписание, целиком оставшееся в прошлом после законного рестарта, ничего не сообщает.
Команда — строка JSON {kind, …} в файле NEUROBOT_FAKE_RS_CONTROL, дописанная во время работы
(echo '{"kind":"delay","duration_s":7}' >> control.jsonl): действует с момента прочтения и только на
процесс, который её прочитал. Строки, которые уже были в файле, когда процесс впервые обратился к камере,
пропускаются (событие control_skipped): их прочитал прошлый процесс (рестарт после аварии) или они от
прошлого прогона, и рестарт не повторяет старые отказы. Файл проверяется при каждом ожидании кадра и не
реже 10 раз в секунду, пока кадров нет; неверная строка отклоняется с ошибкой в stderr и в журнале probe,
работа камеры не меняется. Состояние устройства живёт в процессе: после рестарта процесса отказ gone из
расписания снова требует hardware_reset (как «камера занята после аварийного выхода», R2), а freeze
застывает на первом кадре нового процесса.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from dataclasses import dataclass

import numpy as np

from .clock import ENV_ANCHOR, ENV_TIMELINE, encode_marker, mono, read_anchor
from .probe import probe as default_probe

ENV_FPS = "NEUROBOT_FAKE_RS_FPS"
ENV_FAULTS = "NEUROBOT_FAKE_RS_FAULTS"
ENV_CONTROL = "NEUROBOT_FAKE_RS_CONTROL"
ENV_RESET_S = "NEUROBOT_FAKE_RS_RESET_S"
FAULT_KINDS = ("delay", "stall", "gone", "freeze", "black", "slow")
BLOCKING = ("delay", "stall", "gone")
CURES = ("none", "restart", "reset")
FIXED_CURE = {"stall": "restart", "gone": "reset"}
BACKGROUND = 64
POLL_S = 0.1
INF = float("inf")

log = logging.getLogger("fake_pyrealsense2")


class stream:
    color = "color"
    depth = "depth"


class format:
    bgr8 = "bgr8"
    rgb8 = "rgb8"
    z16 = "z16"


class camera_info:
    name = "name"


class option:
    enable_auto_exposure = "enable_auto_exposure"
    enable_auto_white_balance = "enable_auto_white_balance"
    exposure = "exposure"
    gain = "gain"
    power_line_frequency = "power_line_frequency"


@dataclass(frozen=True)
class Fault:
    kind: str
    start: float
    duration: float = None
    fps: float = None
    cure: str = "none"

    def covers(self, now: float) -> bool:
        return self.start <= now and (self.duration is None or now < self.start + self.duration)


def _positive(entry: dict, key: str, kind: str):
    """Необязательное число > 0 из записи отказа; не число — ValueError (а не TypeError из float)."""
    v = entry.get(key)
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ValueError("%s: %s должно быть числом, а не %r" % (kind, key, v)) from None
    if not v > 0:
        raise ValueError("%s: %s должно быть > 0" % (kind, key))
    return v


def parse_fault(entry, start: float) -> Fault:
    """Проверить запись отказа; start — момент начала (CLOCK_MONOTONIC). Любая ошибка — ValueError."""
    if not isinstance(entry, dict):
        raise ValueError("запись отказа — JSON-объект {kind, …}, а не %r" % (entry,))
    kind = entry.get("kind")
    if kind not in FAULT_KINDS:
        raise ValueError("вид отказа %r, допустимы: %s" % (kind, ", ".join(FAULT_KINDS)))
    allowed = (FIXED_CURE[kind],) if kind in FIXED_CURE else CURES
    cure = entry.get("cure", allowed[0])
    if cure not in allowed:
        raise ValueError("%s: cure %r, допустимо: %s" % (kind, cure, " | ".join(allowed)))
    dur = _positive(entry, "duration_s", kind)
    if dur is None and cure == "none":
        raise ValueError("%s: нужна duration_s или cure" % kind)
    fps = _positive(entry, "fps", kind)
    if kind == "slow" and fps is None:
        raise ValueError("slow: нужен fps > 0")
    return Fault(kind, start, dur, fps, cure)


def load_schedule(path: str) -> list:
    """Расписание — JSON-список {at_s, kind, …}; ошибки — сразу (до запуска прогона)."""
    with open(path, encoding="utf-8") as fh:
        entries = json.load(fh)
    if not isinstance(entries, list):
        raise ValueError("%s: ожидался JSON-список отказов" % path)
    for e in entries:
        parse_fault(e, 0.0)
        at = e.get("at_s")
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not at >= 0:
            raise ValueError("%s: нужен at_s ≥ 0 в %r" % (path, e))
    return entries


def _default_fps(env) -> float:
    if env.get(ENV_FPS):
        return float(env[ENV_FPS])
    if env.get(ENV_TIMELINE):
        with open(env[ENV_TIMELINE], encoding="utf-8") as fh:
            return float(json.loads(fh.readline())["header"]["det_fps"])
    return 10.0


class _State:
    """Устройство и план отказов процесса; все поля — под self.cond."""

    def __init__(self, fps: float, reset_s: float, schedule: list, control_path, anchor_path, probe):
        if fps <= 0:
            raise ValueError("fps должен быть > 0")
        if schedule and not anchor_path:
            raise ValueError("расписание отказов задаётся от якоря таймлайна: нужен %s" % ENV_ANCHOR)
        self.fps = fps
        self.reset_s = reset_s
        self.schedule = schedule
        self.control_path = control_path
        self.anchor_path = anchor_path
        self.probe = probe
        self.cond = threading.Condition()
        self.device = device(self)
        self.epoch = 0
        self.reset_until = 0.0
        self.resets = []
        self.starts = []
        self.faults = []
        self.number = 0
        self.last = None        # последний кадр устройства: (данные, timestamp, номер)
        self.frozen = None      # кадр, на котором застыла камера (freeze)
        self._anchor_checked = -INF
        self._schedule_loaded = not schedule
        self._control_pos = self._skip_old_commands()
        self._active_kinds = ()

    def poll(self, now: float) -> None:
        if not self._schedule_loaded and now - self._anchor_checked >= 1.0:
            self._anchor_checked = now
            anchor = self._anchor()
            if anchor is not None:
                self.faults.extend(parse_fault(e, anchor.mono_of(float(e["at_s"]))) for e in self.schedule)
                self._schedule_loaded = True
        if self.control_path:
            self._read_control(now)
        active = self.active(now)
        self.faults = [f for f in self.faults if f.start > now or f in active]
        kinds = tuple(sorted({f.kind for f in active} | ({"reset"} if now < self.reset_until else set())))
        if kinds != self._active_kinds:
            for k in set(kinds) - set(self._active_kinds):
                self.probe.event("fault_start", kind=k)
            for k in set(self._active_kinds) - set(kinds):
                self.probe.event("fault_end", kind=k)
            self._active_kinds = kinds
            self.probe.set_faults(kinds)

    def _anchor(self):
        """Якорь, если он уже создан и дописан (FakeYolo создаёт его при первом вызове). Якорь другой загрузки
        ОС — ошибка обвязки, а не камеры: она пишется в stderr и журнал, расписание не применяется (FakeYolo на
        том же якоре остановит прогон явной ошибкой)."""
        if not os.path.exists(self.anchor_path):
            return None
        try:
            return read_anchor(self.anchor_path)
        except (ValueError, KeyError):
            return None
        except RuntimeError as e:
            log.error("расписание отказов не применено: %s", e)
            self.probe.event("anchor_error", error=str(e))
            self._schedule_loaded = True
            return None

    def _skip_old_commands(self) -> int:
        """Позиция чтения файла команд при создании устройства: конец последней полной строки. Недописанная
        строка — команда, которую пишут прямо сейчас, она будет прочитана."""
        if not self.control_path:
            return 0
        try:
            with open(self.control_path, "rb") as fh:
                data = fh.read()
        except OSError:
            return 0
        pos = data.rfind(b"\n") + 1
        skipped = sum(1 for line in data[:pos].splitlines() if line.strip())
        if skipped:
            log.info("файл команд %s: строк до старта процесса — %d, пропущены (рестарт не повторяет отказы)",
                     self.control_path, skipped)
            self.probe.event("control_skipped", lines=skipped)
        return pos

    def _read_control(self, now: float) -> None:
        try:
            size = os.path.getsize(self.control_path)
        except OSError:
            return
        if size < self._control_pos:
            log.warning("файл команд %s стал короче (перезаписан, а не дописан?) — читаю с начала", self.control_path)
            self._control_pos = 0
        if size == self._control_pos:
            return
        with open(self.control_path, "rb") as fh:
            fh.seek(self._control_pos)
            chunk = fh.read(size - self._control_pos)
        complete = chunk[:chunk.rfind(b"\n") + 1]
        self._control_pos += len(complete)
        for raw in complete.splitlines():
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                self.faults.append(parse_fault(json.loads(line), now))
                self.probe.event("control", command=line)
            except ValueError as e:
                log.error("команда отказа %r отклонена: %s", line, e)
                self.probe.event("control_error", command=line, error=str(e))

    def active(self, now: float) -> list:
        out = []
        for f in self.faults:
            if not f.covers(now):
                continue
            if f.cure == "restart" and any(s > f.start for s in self.starts):
                continue
            if f.cure == "reset" and any(r > f.start for r in self.resets):
                continue
            out.append(f)
        return out

    def blocked_until(self, now: float, epoch: int):
        """None — кадры идут; иначе момент, раньше которого их точно не будет (inf — до события)."""
        if epoch != self.epoch:
            return INF
        ends = [f.start + f.duration if f.duration is not None else INF
                for f in self.active(now) if f.kind in BLOCKING]
        return max(ends) if ends else None

    def fps_at(self, now: float) -> float:
        slow = [f.fps for f in self.active(now) if f.kind == "slow"]
        return min(slow) if slow else self.fps

    def frame(self, shape: tuple, t_frame: float, now: float):
        """(данные, timestamp, номер) очередного кадра устройства."""
        kinds = {f.kind for f in self.active(now)}
        if "freeze" in kinds and self.last is not None:
            self.frozen = self.frozen or self.last
            return self.frozen
        self.frozen = None
        self.number += 1
        if "black" in kinds:
            data = np.zeros(shape, dtype=np.uint8)
        else:
            data = np.full(shape, BACKGROUND, dtype=np.uint8)
            encode_marker(data, t_frame)
        self.last = (data, t_frame * 1000.0, self.number)
        return self.last


_STATE = None
_STATE_LOCK = threading.Lock()


def configure(fps: float = 10.0, reset_s: float = 2.5, schedule=(), control_path=None, anchor_path=None,
              probe=None) -> None:
    """Задать устройство явно (тесты, обвязка); иначе — из окружения при первом обращении."""
    global _STATE
    with _STATE_LOCK:
        _STATE = _State(fps, reset_s, list(schedule), control_path, anchor_path, probe or default_probe())


def _state() -> _State:
    global _STATE
    with _STATE_LOCK:
        if _STATE is None:
            env = os.environ
            _STATE = _State(_default_fps(env), float(env.get(ENV_RESET_S, "2.5")),
                            load_schedule(env[ENV_FAULTS]) if env.get(ENV_FAULTS) else [],
                            env.get(ENV_CONTROL) or None, env.get(ENV_ANCHOR) or None, default_probe())
        return _STATE


class sensor:
    def __init__(self, name: str):
        self._name = name
        self._options = {}

    def get_info(self, info) -> str:
        return self._name

    def set_option(self, opt, value) -> None:
        self._options[opt] = float(value)

    def get_option(self, opt) -> float:
        return self._options.get(opt, 0.0)


class device:
    NAME = "Intel RealSense D435I"

    def __init__(self, state: _State):
        self._state = state
        self.sensors = [sensor("Stereo Module"), sensor("RGB Camera"), sensor("Motion Module")]

    def get_info(self, info) -> str:
        return self.NAME

    def hardware_reset(self) -> None:
        st = self._state
        with st.cond:
            now = mono()
            st.epoch += 1
            st.reset_until = now + st.reset_s
            st.resets.append(now)
            st.probe.event("hardware_reset", reset_s=st.reset_s)
            st.poll(now)
            st.cond.notify_all()


class context:
    def query_devices(self) -> list:
        st = _state()
        with st.cond:
            return [] if mono() < st.reset_until else [st.device]


class config:
    def __init__(self):
        self.streams = []

    def enable_stream(self, stream_type, width=640, height=480, fmt=format.bgr8, framerate=30) -> None:
        self.streams.append((stream_type, int(width), int(height), fmt, framerate))


class pipeline_profile:
    def __init__(self, dev: device):
        self._device = dev

    def get_device(self) -> device:
        return self._device


class video_frame:
    def __init__(self, data=None, timestamp_ms: float = 0.0, number: int = 0):
        self._data = data
        self._timestamp = timestamp_ms
        self._number = number

    def __bool__(self) -> bool:
        return self._data is not None

    def get_data(self):
        return self._data

    def get_timestamp(self) -> float:
        return self._timestamp

    def get_frame_number(self) -> int:
        return self._number

    def get_width(self) -> int:
        return self._data.shape[1]

    def get_height(self) -> int:
        return self._data.shape[0]


class composite_frame:
    def __init__(self, color: video_frame = None):
        self._color = color if color is not None else video_frame()

    def __bool__(self) -> bool:
        return bool(self._color)

    def get_color_frame(self) -> video_frame:
        return self._color


class pipeline:
    def __init__(self):
        self._started = False
        self._epoch = None
        self._shape = (480, 640, 3)
        self._next_t = None
        self._resume = True

    def start(self, cfg: config = None) -> pipeline_profile:
        st = _state()
        with st.cond:
            if self._started:
                raise RuntimeError("start() cannot be called before stop()")
            now = mono()
            if now < st.reset_until:
                raise RuntimeError("No device connected")
            self._shape = self._resolve(cfg)
            self._started = True
            self._epoch = st.epoch
            self._resume = True
            st.starts.append(now)
            st.probe.event("pipeline_start")
            st.poll(now)
            st.cond.notify_all()
            return pipeline_profile(st.device)

    @staticmethod
    def _resolve(cfg) -> tuple:
        streams = cfg.streams if cfg is not None else [(stream.color, 640, 480, format.bgr8, 30)]
        for kind, w, h, fmt, _ in streams:
            if kind != stream.color or fmt not in (format.bgr8, format.rgb8) or w < 24 or h < 1:
                raise RuntimeError("Couldn't resolve requests")
        return streams[-1][2], streams[-1][1], 3

    def stop(self) -> None:
        st = _state()
        with st.cond:
            if not self._started:
                raise RuntimeError("stop() cannot be called before start()")
            self._started = False
            st.probe.event("pipeline_stop")
            st.cond.notify_all()

    def get_active_profile(self) -> pipeline_profile:
        if not self._started:
            raise RuntimeError("get_active_profile() can only be called between a start() and a following stop()")
        return pipeline_profile(_state().device)

    def wait_for_frames(self, timeout_ms: int = 5000) -> composite_frame:
        frames = self._wait(timeout_ms)
        if frames is None:
            raise RuntimeError("Frame didn't arrive within %d" % timeout_ms)
        return frames

    def try_wait_for_frames(self, timeout_ms: int = 5000):
        frames = self._wait(timeout_ms)
        return (False, composite_frame()) if frames is None else (True, frames)

    def _wait(self, timeout_ms: int):
        st = _state()
        deadline = mono() + timeout_ms / 1000.0
        with st.cond:
            while True:
                if not self._started:
                    raise RuntimeError("wait_for_frames cannot be called before start()")
                now = mono()
                st.poll(now)
                blocked = st.blocked_until(now, self._epoch)
                if blocked is None:
                    fps = st.fps_at(now)
                    if self._resume or self._next_t is None:
                        self._next_t, self._resume = now, False
                    if self._next_t <= now:
                        t_frame = self._next_t + math.floor((now - self._next_t) * fps) / fps
                        self._next_t = t_frame + 1.0 / fps
                        data, ts, number = st.frame(self._shape, t_frame, now)
                        st.probe.inc("frames")
                        return composite_frame(video_frame(data.copy(), ts, number))
                    wake = self._next_t
                else:
                    self._resume = True
                    wake = min(blocked, now + POLL_S)
                if now >= deadline:
                    st.probe.inc("timeouts")
                    return None
                st.cond.wait(min(wake, deadline) - now)
