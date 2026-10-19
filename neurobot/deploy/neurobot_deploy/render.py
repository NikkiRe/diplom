"""Юниты и прочие файлы из шаблонов (units/, logrotate/): подстановка значений env-файла (install.sh, тесты).

systemd не раскрывает переменные окружения в User=, Group=, путях и зависимостях, поэтому эти значения
подставляются при установке; в Exec*-строках юниты читают тот же env-файл сами (EnvironmentFile=).
Подстановки: @USER@ @GROUP@ @SUPPLEMENTARY_GROUPS@ @PREFIX@ @ETC@ @PICOCLAW_HOME@; строка, целиком равная
@AFTER_DRIVERS@ или @AFTER_LABWORKERS@, заменяется на After= с юнитами драйверов (воркеров) или удаляется.
Здесь же — статические проверки состава: камеру не открывают и распознавание, и воркер лаборатории (N12);
исполнитель не бывает без safety_gate.
"""
from __future__ import annotations

import os
import re
from typing import Dict, List

from .config import Config, EnvError

_LEFT = re.compile(r"@[A-Z_]+@")
_NAME = re.compile(r"[a-z_][a-z0-9_-]*")


def values(cfg: Config) -> Dict[str, str]:
    v = {"USER": cfg.get("NEUROBOT_USER"), "GROUP": cfg.get("NEUROBOT_GROUP"),
         "SUPPLEMENTARY_GROUPS": " ".join(cfg.get("NEUROBOT_SUPPLEMENTARY_GROUPS").split()),
         "PREFIX": cfg.get("NEUROBOT_PREFIX").rstrip("/"), "ETC": os.path.dirname(os.path.abspath(cfg.path))}
    for key in ("USER", "GROUP"):
        if not _NAME.fullmatch(v[key]):
            raise EnvError("NEUROBOT_{}: ожидалось имя пользователя/группы, получено {!r}".format(key, v[key]))
    bad = [g for g in v["SUPPLEMENTARY_GROUPS"].split() if not _NAME.fullmatch(g)]
    if bad:
        raise EnvError("NEUROBOT_SUPPLEMENTARY_GROUPS: недопустимые имена {}".format(bad))
    if not os.path.isabs(v["PREFIX"]) or " " in v["PREFIX"]:
        raise EnvError("NEUROBOT_PREFIX: нужен абсолютный путь без пробелов, получено {!r}".format(v["PREFIX"]))
    v["PICOCLAW_HOME"] = cfg.picoclaw_home()
    comps = cfg.components()
    cameras = [w for w in cfg.labworkers() if cfg.labworker(w, "CAMERA").lower() in ("1", "yes", "true")]
    if cameras and "perception" in comps:
        raise EnvError("NEUROBOT_LABWORKER_{}_CAMERA: у камеры один владелец (N12) — уберите perception из "
                       "NEUROBOT_COMPONENTS или этот воркер из NEUROBOT_LABWORKERS".format(cameras[0]))
    if "executor" in comps and "safety" not in comps:
        raise EnvError("NEUROBOT_COMPONENTS: исполнитель без safety — в /cmd_vel пишет только safety_gate (PLAN §13.3)")
    return v


def render(cfg: Config, src_dir: str) -> Dict[str, str]:
    v = values(cfg)
    after = {"@AFTER_DRIVERS@": ["neurobot-driver@{}.service".format(d) for d in cfg.drivers()],
             "@AFTER_LABWORKERS@": ["neurobot-labworker@{}.service".format(w) for w in cfg.labworkers()]}
    out = {}
    for name in sorted(os.listdir(src_dir)):
        with open(os.path.join(src_dir, name), encoding="utf-8") as f:
            lines = []
            for line in f.read().splitlines():
                if line.strip() in after:
                    if after[line.strip()]:
                        lines.append("After=" + " ".join(after[line.strip()]))
                    continue
                for key, value in v.items():
                    line = line.replace("@{}@".format(key), value)
                lines.append(line)
        text = "\n".join(lines) + "\n"
        left = _LEFT.findall(text)
        if left:
            raise EnvError("{}: не подставлены {}".format(name, sorted(set(left))))
        out[name] = text
    return out


def enable_list(cfg: Config) -> List[str]:
    """Что включает install.sh: цель, компоненты, экземпляры драйверов и воркеров, сторож (если ему есть кого
    сторожить: драйверы, rosbridge, MCP или PicoClaw)."""
    watched = cfg.drivers() or {"rosbridge", "mcp", "picoclaw"} & set(cfg.components())
    return ["neurobot.target"] + [u for _, u in cfg.plan()] + (["neurobot-watchdog.service"] if watched else [])
