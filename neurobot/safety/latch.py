"""Аварийная защёлка safety_gate: команды /neurobot/estop и хранение в файле (контракт neurobot/deploy).

Команда — std_msgs/String с JSON {"cmd": "engage" | "release", "by", "reason", "id", "t"}; CLI `neurobot estop`
шлёт её трижды с одним id. Защёлка — состояние safety_gate: причины атомарно (временный файл, fsync, rename)
пишутся в estop.json до того, как состояние estop уйдёт в /neurobot/safety_state, и читаются до READY. Файл есть,
но не читается — старт с защёлкой; не удалось записать — защёлка остаётся в памяти, строка в журнале. Формат файла —
{"engaged", "reasons", "t"}, как у подставного safety_gate deploy.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import List, NamedTuple, Optional

log = logging.getLogger("safety_gate")


class EstopCmd(NamedTuple):
    cmd: str              # engage | release
    by: str               # кто: human (CLI), agent, …
    reason: str
    id: str
    t: Optional[float]    # время отправки (часы робота), с


def parse(data: str) -> EstopCmd:
    """Команда из JSON; ValueError — не разобрана."""
    try:
        doc = json.loads(data)
        cmd = doc["cmd"]
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError("команда защёлки не разобрана: {}".format(e)) from e
    if cmd not in ("engage", "release"):
        raise ValueError("команда защёлки {!r}: ожидается engage | release".format(cmd))
    t = doc.get("t")
    return EstopCmd(cmd, str(doc.get("by") or "?"), str(doc.get("reason") or ""), str(doc.get("id") or ""),
                    float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None)


class Latch:
    """Причины защёлки (строки «код: текст»); пусто — не стоит. path "" — без файла (только память)."""

    def __init__(self, path: str):
        self.path = path
        self.reasons: List[str] = []
        self.since: Optional[float] = None
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    doc = json.load(f)
                if doc["engaged"]:
                    self.reasons = [str(r) for r in doc["reasons"]] or ["estop: причина не записана"]
                    self.since = float(doc.get("t") or time.time())
            except (OSError, ValueError, KeyError, TypeError) as e:
                self.reasons, self.since = ["estop: файл защёлки {} не читается ({})".format(path, e)], time.time()

    @property
    def engaged(self) -> bool:
        return bool(self.reasons)

    def has(self, code: str) -> bool:
        return any(r.split(":", 1)[0] == code for r in self.reasons)

    def engage(self, code: str, text: str) -> bool:
        """Поставить с причиной «code: text»; False — причина с этим кодом уже есть."""
        if self.has(code):
            return False
        if not self.reasons:
            self.since = time.time()
        self.reasons.append("{}: {}".format(code, text))
        self._save()
        return True

    def release(self) -> None:
        self.reasons, self.since = [], None
        self._save()

    def _save(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"engaged": self.engaged, "reasons": self.reasons, "t": self.since or time.time()}, f,
                          ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            log.error("защёлка не записана в %s (%s): действует до рестарта процесса", self.path, e)
