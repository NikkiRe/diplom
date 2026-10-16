"""Конфиг PicoClaw для робота: версия, схема полей и наложение оверлея neurobot (PLAN §4; R3 §7.1; R7 N11, N15).

PicoClaw 0.2.x–0.3.x не стартует, если в config.json есть поле, которого нет в его структуре Config, — поэтому
оверлей накладывается с учётом установленной версии: поля, которых в ней нет (у 0.2.8 — max_llm_retries,
tools.load_image), отбрасываются с сообщением. Схемы — picoclaw/schema-<версия>.json, сняты с исходников
(verify/picoclaw_schema.go); для версии без схемы берётся пересечение известных схем (только общие поля).
Версия: NEUROBOT_PICOCLAW_VERSION → «<бинарник> version» из NEUROBOT_PICOCLAW_CMD → build_info.version конфига.

Наложение: рекурсивно; записи tools.mcp.servers с полем type заменяются целиком (иначе от stdio-записи остались
бы command/args), записи без type (например {"enabled": false}) правят только уже существующий сервер;
model_list[].request_timeout ставится каждой модели (массив оверлеем не слить). Ключи и каналы лаборатории не
трогаются. Перед записью — копия config.json.neurobot-<время>, запись атомарная с прежними правами и владельцем.
"""
from __future__ import annotations

import copy
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from typing import Any, Dict, List, Tuple

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "picoclaw")
OVERLAY = os.path.join(SCHEMA_DIR, "config.overlay.json")
_VERSION = re.compile(r"v?(\d+\.\d+\.\d+)")
_BINARY = re.compile(r"picoclaw\s+v?(\d+\.\d+\.\d+)")  # «🦞 picoclaw 0.2.8 (git: 6e1fab80)» после логотипа


def load_overlay(path: str = OVERLAY) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def lab_servers(overlay: Dict[str, Any]) -> List[str]:
    """MCP-серверы лаборатории, которые оверлей выключает (записи без type): по stdio это серверы робота."""
    servers = ((overlay.get("tools") or {}).get("mcp") or {}).get("servers") or {}
    return sorted(name for name, entry in servers.items() if "type" not in entry)


def known_versions(schema_dir: str = SCHEMA_DIR) -> List[str]:
    names = (os.path.basename(p)[len("schema-"):-len(".json")]
             for p in glob.glob(os.path.join(schema_dir, "schema-*.json")))
    return sorted(names, key=lambda v: tuple(int(x) for x in v.split(".")))


def _intersect(a: Any, b: Any) -> Any:
    """Поля, допустимые в обеих схемах; лист (None) допускает что угодно, поэтому строже — другая сторона."""
    if a is None or b is None:
        return b if a is None else a
    return {k: _intersect(a[k], b[k]) for k in a if k in b}


def load_schema(version: str, schema_dir: str = SCHEMA_DIR) -> Tuple[Any, bool]:
    """(схема, точная ли): для версии без своей схемы — пересечение всех известных."""
    path = os.path.join(schema_dir, "schema-{}.json".format(version))
    if version and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f), True
    schema = None
    for v in known_versions(schema_dir):
        with open(os.path.join(schema_dir, "schema-{}.json".format(v)), encoding="utf-8") as f:
            s = json.load(f)
        schema = s if schema is None else _intersect(schema, s)
    return schema, False


def unknown_fields(data: Any, schema: Any, path: str = "") -> List[str]:
    """Пути полей data, которых нет в schema (как collectUnknownJSONFields PicoClaw)."""
    if not isinstance(schema, dict):
        return []
    if "[]" in schema:
        return [p for i, item in enumerate(data if isinstance(data, list) else [])
                for p in unknown_fields(item, schema["[]"], "{}[{}]".format(path, i))]
    if not isinstance(data, dict):
        return []
    out = []
    for key, value in data.items():
        sub = path + "." + key if path else key
        if "*" in schema:
            out += unknown_fields(value, schema["*"], sub)
        elif key not in schema:
            out.append(sub)
        else:
            out += unknown_fields(value, schema[key], sub)
    return sorted(out)


def prune(overlay: Dict[str, Any], schema: Any) -> Tuple[Dict[str, Any], List[str]]:
    """Оверлей без полей, которых нет в схеме версии; второй элемент — что отброшено."""
    dropped = unknown_fields(overlay, schema)
    out = copy.deepcopy(overlay)
    for path in dropped:
        *parents, leaf = path.split(".")
        node = out
        for key in parents:
            node = node[key]
        del node[leaf]
    return out, dropped


def parse_version(text: str, pattern=_VERSION) -> str:
    m = pattern.search(text or "")
    return m.group(1) if m else ""


def detect_version(env_version: str, command: str, data: Dict[str, Any]) -> Tuple[str, str]:
    """(версия, откуда взята) или ("", "") — см. шапку модуля."""
    if env_version:
        return parse_version(env_version), "NEUROBOT_PICOCLAW_VERSION"
    argv = [a for a in shlex.split(command or "") if "=" not in a and a != "env"]
    if argv:
        try:
            r = subprocess.run([argv[0], "--no-color", "version"], capture_output=True, text=True, timeout=5)
            version = parse_version(r.stdout, _BINARY)
            if version:
                return version, "{} version".format(argv[0])
        except (OSError, subprocess.SubprocessError):
            pass
    version = parse_version(str((data.get("build_info") or {}).get("version", "")))
    return (version, "build_info.version в config.json") if version else ("", "")


def deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def merged(config: Dict[str, Any], overlay: Dict[str, Any], request_timeout: int) -> Dict[str, Any]:
    out = copy.deepcopy(config)
    overlay = copy.deepcopy(overlay)
    servers = ((overlay.get("tools") or {}).get("mcp") or {}).pop("servers", {}) or {}
    deep_merge(out, overlay)
    have = out.setdefault("tools", {}).setdefault("mcp", {}).setdefault("servers", {})
    for name, entry in servers.items():
        if "type" in entry:
            have[name] = entry
        elif isinstance(have.get(name), dict):
            deep_merge(have[name], entry)
    for model in out.get("model_list") or []:
        if isinstance(model, dict):
            model["request_timeout"] = request_timeout
    return out


def apply(path: str, overlay: Dict[str, Any], request_timeout: int) -> str:
    """Наложить оверлей (уже подогнанный под версию) на path; вернуть путь резервной копии."""
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    backup = "{}.neurobot-{}".format(path, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, backup)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged(config, overlay, request_timeout), f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    st = os.stat(path)
    os.chmod(tmp, st.st_mode & 0o7777)
    if os.geteuid() == 0:
        os.chown(tmp, st.st_uid, st.st_gid)  # sudo не должен отнять конфиг у пользователя PicoClaw
    os.replace(tmp, path)
    return backup
