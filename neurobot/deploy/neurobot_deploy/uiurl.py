"""sudo neurobot ui-url — одноразовая ссылка входа в пульт оператора (docs/ui/PLAN.md §3.3; образец — Jupyter).

Токен пульта лежит в его каталоге состояния (NEUROBOT_UI_STATE_DIR = StateDirectory юнита, 0700, владелец — пользователь
пульта): агенту (NEUROBOT_USER) его не прочитать. root кладёт туда, в codes/, файл с именем sha256 случайного кода и
сроком {"exp"}; шлюз пульта (neurobot/ui/gateway.Auth.redeem) меняет код на cookie один раз и удаляет файл. Сам код
в файл не пишется, токен в ссылку не попадает. Ссылка — в stdout, подсказка про туннель — в stderr.
root пишет в каталог менее привилегированного пользователя: по ссылкам на пути не идёт (write_code), иначе взломанный
шлюз подменил бы codes/ ссылкой и получил бы от root файл в любом каталоге.
Коды: 0 — ссылка выдана, 1 — нельзя (не root, пульта нет в составе, пульт ещё не запускался, codes/ подменён).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import time
from typing import Callable

from .config import Config
from .report import emit

CODES = "codes"
TTL_S = 120.0
SESSION_H = 12      # срок сессии пульта (neurobot/ui/gateway.SESSION_TTL_S — тест сверяет)


class UnsafeDir(OSError):
    pass


def write_code(state_dir: str, code: str, ttl: float, now: float, chown: bool = False) -> str:
    """Файл кода codes/<sha256 кода> с {"exp"}, 0600; chown — владелец и группа каталога состояния (пользователь
    пульта). Ни одной ссылки не следуем: каталог состояния и codes/ открываются O_NOFOLLOW | O_DIRECTORY, codes/ —
    каталог того же владельца, без записи для группы и прочих; файл создаётся O_CREAT | O_EXCL | O_NOFOLLOW
    относительно открытого codes/, владелец ставится fchown по дескриптору. Без rename: имя случайное, а неполный
    файл шлюз просто не примет. Путь; UnsafeDir или OSError — не записан."""
    name = hashlib.sha256(code.encode("utf-8")).hexdigest()
    dflags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    sfd = os.open(state_dir, dflags)
    try:
        st = os.fstat(sfd)
        dfd = os.open(CODES, dflags, dir_fd=sfd)
        try:
            cst = os.fstat(dfd)
            if cst.st_uid != st.st_uid or cst.st_mode & 0o022:
                raise UnsafeDir("{}/{}: владелец {} или права {:o} не те, что у каталога пульта ({})".format(
                    state_dir, CODES, cst.st_uid, cst.st_mode & 0o777, st.st_uid))
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"exp": round(now + ttl, 3)}, f)
                f.flush()
                os.fsync(f.fileno())
                if chown:
                    os.fchown(f.fileno(), st.st_uid, st.st_gid)
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)
    return os.path.join(state_dir, CODES, name)


def run(cfg: Config, ttl: float, euid: int, clock: Callable[[], float] = time.time) -> int:
    if euid != 0:
        emit("ui-url", "ui", "ui_denied", "ссылку входа в пульт выдаёт только root: sudo neurobot ui-url (токен — в "
             "{}, доступен только пульту)".format(cfg.get("NEUROBOT_UI_STATE_DIR")))
        return 1
    if "ui" not in cfg.components():
        emit("ui-url", "ui", "not_installed", "пульта нет в составе (NEUROBOT_COMPONENTS)")
        return 1
    state = cfg.get("NEUROBOT_UI_STATE_DIR")
    if not os.path.isdir(os.path.join(state, CODES)):
        emit("ui-url", "ui", "not_running", "пульт ещё не запускался (нет {}): sudo systemctl start neurobot-ui".format(
            os.path.join(state, CODES)))
        return 1
    code = secrets.token_urlsafe(18)
    try:
        write_code(state, code, ttl, clock(), chown=True)
    except OSError as e:
        emit("ui-url", "ui", "ui_codes_unsafe", "код входа не записан: {} — каталог пульта подменён или испорчен; "
             "ссылок не выдавать, проверить /var/lib/neurobot/ui".format(e.strerror or e))
        return 1
    port = cfg.port("NEUROBOT_UI_PORT")
    print("http://127.0.0.1:{}/login?code={}".format(port, code))
    sys.stderr.write("ссылка одноразовая, действует {:g} с. С ноутбука: ssh -o ServerAliveInterval=5 -o "
                     "ServerAliveCountMax=3 -L {}:127.0.0.1:{} <пользователь>@<робот>, затем открыть ссылку (другой "
                     "локальный порт туннеля — тот же порт и в ссылке). Вход действует {:g} ч. «СТОП движения» "
                     "пульта — не аварийная кнопка: при обрыве связи не сработает.\n".format(ttl, port, port,
                                                                                         SESSION_H))
    return 0
