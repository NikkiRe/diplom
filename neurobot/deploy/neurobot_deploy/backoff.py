"""Нарастающая пауза перед частыми автоматическими рестартами распознавания (ExecStartPre, `neurobot precheck`).

`RestartSteps=` появился только в systemd 254, а на роботе 245. С `RestartSec=1` распознавание при нехватке памяти
крутится по кругу: старт, прогрев, OOM, рестарт через ≈2 с. Повторные импорты torch съедают ядра A57 у safety_gate:
2,56 ядра против 1,27 в норме [замер, budget §6.2].

Перед стартом precheck решает так:
- старт ручной (человек, сторож, start_test) — `NRestarts` = 0, его обнуляет systemd [опыт, ВМ, systemd 255] — сразу;
- автоматический рестарт — смотрим серию. Серия — запуски подряд, каждый не позже `NEUROBOT_RESTART_RESET_S`
  (600 с) после прошлого: «≈10 мин работы — сброс», как у Kubernetes. Пауза k-го рестарта серии — k-е число
  `NEUROBOT_RESTART_PAUSES_S` (0 5 15 30, последнее повторяется).

Первый рестарт идёт сразу: одиночный отказ не удлиняет TTR. Время прошлого запуска хранится в `restart.json` в
StateDirectory юнита; после перезагрузки (другой boot_id) серия начинается заново.
"""
from __future__ import annotations

import json
import os
from typing import Optional, Sequence, Tuple

STATE = "restart.json"
COMPONENTS = {"perception": "neurobot-perception.service"}


def plan(n_restarts: int, prev: Optional[dict], boot: str, now: float, pauses: Sequence[float],
         reset_s: float) -> Tuple[float, int, dict]:
    """(пауза, с; номер рестарта в серии, 0 — серии нет; новое состояние). now — монотонные часы, с."""
    streak = 0
    if n_restarts > 0 and prev and prev.get("boot") == boot:
        try:
            if 0.0 <= now - float(prev["t"]) < reset_s:
                streak = int(prev.get("streak", 0)) + 1
        except (KeyError, TypeError, ValueError):
            pass
    pause = float(pauses[min(streak, len(pauses)) - 1]) if streak and pauses else 0.0
    return pause, streak, {"boot": boot, "t": now + pause, "streak": streak}


def read_state(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def boot_id(path: str = "/proc/sys/kernel/random/boot_id") -> str:
    try:
        with open(path, encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return ""
