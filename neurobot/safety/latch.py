"""Аварийная защёлка safety_gate: команды /neurobot/estop, снятие через каталог root и хранение в файле (контракт
neurobot/deploy).

Команда — std_msgs/String с JSON {"cmd": "engage" | "release", "by", "reason", "id", "t"}; CLI `neurobot estop`
шлёт её трижды с одним id. Ставит защёлку кто угодно. Снятие по теме не принимается: поле by самозаявленное, и узел
ROS (или клиент rosbridge с открытой темой) подписался бы «human». Снимает защёлку только файл release.json в
каталоге release_dir, которые принадлежат root и не доступны на запись группе и прочим: его пишет `sudo neurobot
estop release` после подтверждения в терминале, а агенту (PicoClaw без exec, MCP-сервер, rosbridge — не root)
создать такой файл нечем. В файле — {"id", "t", "since", "reasons", "reason"}: защёлка, которую видел человек (её
since и причины), и время записи.
Защёлка — состояние safety_gate: причины атомарно (временный файл, fsync, rename) пишутся в estop.json до того, как
состояние estop уйдёт в /neurobot/safety_state, и читаются до READY. Файл есть, но не читается — старт с защёлкой;
не удалось записать — защёлка остаётся в памяти, строка в журнале. Формат файла — {"engaged", "reasons", "t"}, как у
подставного safety_gate deploy.

Стоп агента (/neurobot/stop_now, robot_stop через MCP; решение 1а 19.10) — не защёлка: разовый ноль сразу. Команда —
std_msgs/String с JSON {"id", "by", "reason", "t"}; копии с одним id исполняются один раз.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import time
from typing import List, NamedTuple, Optional, Tuple

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


class StopCmd(NamedTuple):
    """Стоп агента: разовый ноль сразу, без защёлки."""
    by: str
    reason: str
    id: str
    t: Optional[float]    # время отправки (часы робота), с


def parse_stop(data: str) -> StopCmd:
    """Стоп агента из JSON-объекта; ValueError — не разобран."""
    try:
        doc = json.loads(data)
        if not isinstance(doc, dict):
            raise TypeError("ожидается объект JSON")
    except (ValueError, TypeError) as e:
        raise ValueError("стоп агента не разобран: {}".format(e)) from e
    t = doc.get("t")
    return StopCmd(str(doc.get("by") or "?"), str(doc.get("reason") or ""), str(doc.get("id") or ""),
                   float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None)


class ReleaseReq(NamedTuple):
    """Снятие защёлки человеком: файл release.json каталога root (sudo neurobot estop release)."""
    id: str
    t: float                      # время записи (часы робота), с
    since: Optional[float]        # since защёлки, которую видел человек
    reasons: Tuple[str, ...]      # её причины
    reason: str


RELEASE_FILE = "release.json"
RELEASE_MAX_BYTES = 65536


def _root_only(st: os.stat_result, owner: int, what: str, path: str) -> None:
    if st.st_uid != owner:
        raise ValueError("{} {} принадлежит uid {}, а не {} — снятие не принято".format(what, path, st.st_uid, owner))
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("{} {} доступен на запись группе или прочим ({:o}) — снятие не принято".format(
            what, path, stat.S_IMODE(st.st_mode)))


def read_release(directory: str, owner: int = 0) -> Optional[ReleaseReq]:
    """Запрос снятия из directory/release.json; None — файла нет. ValueError — каталог или файл не root (owner),
    доступны на запись группе или прочим, ссылка вместо файла, не разобран."""
    path = os.path.join(directory, RELEASE_FILE)
    try:
        st_dir = os.lstat(directory)
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(st_dir.st_mode):
        raise ValueError("{} — не каталог".format(directory))
    _root_only(st_dir, owner, "каталог", directory)
    if not stat.S_ISREG(st.st_mode) or st.st_size > RELEASE_MAX_BYTES:
        raise ValueError("{} — не обычный файл или больше {} байт".format(path, RELEASE_MAX_BYTES))
    _root_only(st, owner, "файл", path)
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        t, since = doc["t"], doc.get("since")
        return ReleaseReq(str(doc["id"]), float(t), None if since is None else float(since),
                          tuple(str(r) for r in doc.get("reasons") or ()), str(doc.get("reason") or ""))
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ValueError("{} не разобран: {}".format(path, e)) from e


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
