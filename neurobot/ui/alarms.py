"""Словарь тревог пульта (философия тревог: PLAN §0а п. 2 — два уровня; U4 §5; проект — утверждает студент) и их
отслеживание.

Уровни (ISA-18.2: приоритет задаёт философия объекта, а не общая палитра):
  1 — P1, красный квадрат «1»: защёлка и её причины, отказ лидара, safety_state молчит — безопасность неизвестна
      или робот заперт;
  2 — P2, оранжевый треугольник «2»: всё, что требует внимания человека, но не защёлка: устаревшие датчики, чужой
      /scan, компонент не работает, робот стоит перед препятствием (blocked), геозабор, задний ход исчерпан, геймпад;
  0 — норма, серым: slow с lidar_blind / lidar_unseen (на роботе это обычное рабочее состояние: перед всегда в
      слепом секторе корпуса камеры, `ok` не бывает по построению — README safety), unarmed, обучение маски, пауза
      perception в движении. Иначе вечная плашка приучит не замечать тревоги.

Причины «препятствия» (obstacle, cliff, approach, lidar_nodata, touch) — P2 только при `blocked` или `estop`: при
`slow` safety_gate лишь сбавляет скорость, действий человека это не требует.

Предупреждения safety_state (`warnings`) — тихо, серым (PLAN §4: «`warnings` тихо»): они не ограничивают движение,
а их последствие, если оно есть, приходит причиной в `reasons`. Тревогу поднимают только коды с `warn` — те, что
требуют взгляда человека и сами по себе: `override` (есть издатель главнее safety_gate), `override_active` (команда
не доходит до базы), `scan_foreign`, `scan_publishers` (чужой /scan). `mask_outside` safety_gate держит в warnings всё
время процесса, а запрет вперёд по тем же лучам и так приходит причиной `lidar_nodata` — вечная плашка P2 приучила
бы не замечать тревоги. Политика одна для «Записи» и живого шлюза (`level`, `safety_alarms`; тест).

Тревогу ведёт шлюз (живой режим) или сборщик записи — одним кодом (AlarmTracker), браузер только рисует: повтор
причины — счётчик той же строки, а не новая строка; снятие — после OFF_DELAY_S без причины (запись safety_state —
1 Гц, причина мерцает); скрытия тревог нет.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

NORM, P1, P2 = 0, 1, 2
OFF_DELAY_S = 3.0      # тревога снимается, если условия нет столько секунд подряд (антидребезг, ISA-18.2 off-delay)


class Code(NamedTuple):
    prio: int                # уровень, если условие есть
    text: str                # короткий текст для оператора
    only_stopped: bool = False   # тревога только при blocked / estop; при slow — норма
    warn: bool = False       # тревога и тогда, когда код пришёл в warnings; без флага warnings — тихо


# Коды причин safety_state — README safety, «/neurobot/safety_state»; свои коды пульта — с префиксом ui_.
CODES: Dict[str, Code] = {
    # P1 — защёлка и полный отказ
    "estop": Code(P1, "аварийная защёлка"),
    "tilt": Code(P1, "наклон — защёлка"),
    "shock": Code(P1, "удар — защёлка"),
    "stall": Code(P1, "упор: команда есть, база стоит — защёлка"),
    "foreign_writer": Code(P1, "чужой писатель /cmd_vel — защёлка"),
    "lidar_fault": Code(P1, "лидар не видит — полный стоп"),
    "ui_safety_silent": Code(P1, "safety_state молчит — безопасность неизвестна"),
    # P2 — данные и компоненты
    "stale_scan": Code(P2, "/scan устарел"),
    "stale_odom": Code(P2, "/odom устарел"),
    "stale_imu": Code(P2, "IMU (ориентация) устарел"),
    "stale_accel": Code(P2, "IMU (ускорение) устарел"),
    "stale_depth": Code(P2, "глубина устарела"),
    "scan_foreign": Code(P2, "/scan публикует не драйвер", warn=True),
    "scan_publishers": Code(P2, "у /scan второй издатель", warn=True),
    "motion_time": Code(P2, "движение дольше предела — стоп"),
    "override_active": Code(P2, "команда не доходит до базы", warn=True),
    "ui_unit_down": Code(P2, "компонент не работает"),
    "ui_camera_lost": Code(P2, "нет кадров камеры"),
    # P2 — робот стоит или ограничен: нужен взгляд человека
    "obstacle": Code(P2, "препятствие — вперёд нельзя", True),
    "cliff": Code(P2, "обрыв — вперёд нельзя", True),
    "approach": Code(P2, "подъезд к препятствию", True),
    "lidar_nodata": Code(P2, "нет данных лидара впереди", True),
    "touch": Code(P2, "поворот на месте заденет", True),
    "geofence": Code(P2, "геозабор"),
    "reverse_limit": Code(P2, "задний ход исчерпан"),
    "override": Code(P2, "есть издатель главнее safety_gate (геймпад)", warn=True),
    "mask_outside": Code(P2, "«нет данных» вне маски корпуса"),
    "ui_perception_recovering": Code(P2, "perception восстанавливает камеру"),
    "ui_llm_open": Code(P2, "канал LLM закрыт"),
    # норма — серым
    "lidar_blind": Code(NORM, "перед в слепом секторе лидара — только глубина (штатно)"),
    "lidar_unseen": Code(NORM, "участок вне поля глубины — как слепой сектор (штатно)"),
    "lidar_unknown": Code(NORM, "неизвестные лучи, не ограничивают"),
    "lidar_mask": Code(NORM, "учим маску слепых лучей (старт)"),
    "unarmed": Code(NORM, "не взведён: ждёт нулевой запрос"),
}
UNKNOWN = Code(P2, "неизвестная причина")        # новый код safety_gate без словаря — лучше заметить, чем скрыть
STATES = ("ok", "slow", "blocked", "estop")


def code_of(reason: str) -> str:
    """«код: текст» → код."""
    return str(reason).split(":", 1)[0].strip()


def prio(code: str, state: Optional[str] = None) -> int:
    """Уровень причины при состоянии state (0 — норма)."""
    c = CODES.get(code, UNKNOWN)
    if c.only_stopped and state not in ("blocked", "estop"):
        return NORM
    return c.prio


def loud(warnings: Iterable[str]) -> List[str]:
    """Предупреждения, которые поднимают тревогу (код с warn); остальные — тихо."""
    return [w for w in warnings if CODES.get(code_of(w), UNKNOWN).warn]


def level(state: Optional[str], reasons: Iterable[str] = (), warnings: Iterable[str] = ()) -> int:
    """Уровень состояния safety_state: наивысший (меньший номер) среди причин и громких предупреждений (loud);
    estop — не ниже P1. 0 — норма (в том числе slow с lidar_blind / lidar_unseen)."""
    levels = [prio(code_of(r), state) for r in list(reasons) + loud(warnings)]
    if state == "estop":
        levels.append(P1)
    active = [x for x in levels if x > NORM]
    return min(active) if active else NORM


def dictionary() -> Dict[str, dict]:
    """Словарь для клиента (заголовок потока и бандла)."""
    return {k: {"prio": c.prio, "text": c.text, "only_stopped": c.only_stopped, "warn": c.warn}
            for k, c in CODES.items()}


class Alarm(NamedTuple):
    key: str          # код или код:объект (ui_unit_down:perception)
    prio: int
    text: str


class AlarmTracker:
    """Тревоги во времени: условие есть — тревога встаёт (повтор после снятия — тот же ключ, счётчик +1); условия
    нет OFF_DELAY_S — снимается. update() возвращает сообщения потока `alarm` ({key, code, prio, on, text, n, src})."""

    def __init__(self, off_delay_s: float = OFF_DELAY_S):
        self.off_delay_s = off_delay_s
        self.active: Dict[Tuple[str, str], dict] = {}     # (src, key) → {prio, text, seen}
        self.counts: Dict[Tuple[str, str], int] = {}

    def update(self, t: float, src: str, present: Sequence[Alarm], off_delay: Optional[float] = None) -> List[dict]:
        """Полный набор тревог источника src на момент t (safety, unit, perception, link). off_delay — своя задержка
        снятия для источника, у которого конец условия известен точно (юнит поднялся, safety_state снова идёт)."""
        delay = self.off_delay_s if off_delay is None else off_delay
        out: List[dict] = []
        now = {(src, a.key): a for a in present if a.prio > NORM}
        for k, a in now.items():
            cur = self.active.get(k)
            if cur is None:
                self.counts[k] = self.counts.get(k, 0) + 1
                self.active[k] = {"prio": a.prio, "text": a.text, "seen": t}
                out.append(self._msg(t, k, a.prio, a.text, True))
            else:
                cur["seen"] = t
                if a.prio < cur["prio"]:          # стала важнее — новое сообщение с тем же ключом
                    cur["prio"], cur["text"] = a.prio, a.text
                    out.append(self._msg(t, k, a.prio, a.text, True))
        for k, cur in list(self.active.items()):
            if k[0] == src and k not in now and t - cur["seen"] >= delay:
                del self.active[k]
                out.append(self._msg(t, k, cur["prio"], cur["text"], False))
        return out

    def close(self, t: float) -> List[dict]:
        """Конец записи: все активные тревоги снимаются."""
        out = [self._msg(t, k, cur["prio"], cur["text"], False) for k, cur in sorted(self.active.items())]
        self.active.clear()
        return out

    def _msg(self, t: float, k: Tuple[str, str], p: int, text: str, on: bool) -> dict:
        return {"k": "alarm", "t": t, "src": k[0], "key": k[1], "code": k[1].split(":", 1)[0], "prio": p,
                "on": on, "text": text, "n": self.counts.get(k, 0)}


def safety_alarms(state: Optional[str], reasons: Iterable[str], warnings: Iterable[str] = ()) -> List[Alarm]:
    """Тревоги из одного safety_state: причины и громкие предупреждения (loud) с уровнем > 0; estop без причины-кода
    P1 — сам `estop`."""
    out: Dict[str, Alarm] = {}
    for r in list(reasons) + loud(warnings):
        code = code_of(r)
        p = prio(code, state)
        if p > NORM:
            text = CODES.get(code, UNKNOWN).text
            if code not in CODES:
                text = "%s: %s" % (code, text)
            out[code] = Alarm(code, p, text)
    if state == "estop" and not any(a.prio == P1 for a in out.values()):
        out["estop"] = Alarm("estop", P1, CODES["estop"].text)
    return sorted(out.values(), key=lambda a: (a.prio, a.key))
