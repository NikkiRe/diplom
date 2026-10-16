"""Каталог инструментов: имя → JSON Schema аргументов, по серверам MCP.

Принимаемые форматы файла --tools (и поля run_start.tools):
  * список инструментов — MCP (``{"name", "inputSchema"}``), OpenAI (``{"type": "function", "function":
    {"name", "parameters"}}``) или краткий (``{"name", "parameters"}``); у элемента может быть ``"server"``;
  * ``{"tools": [...]}`` — результат MCP ``tools/list``;
  * ``{"servers": {"hw": [...] | {"tools": [...]}, "local": [...]}}`` — по серверам.
Инструменты без сервера ищутся по имени для любого ``tool_call.server``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from monitor import argschema
from monitor.jsonl import loads_strict

ANY_SERVER = ""  # ключ для инструментов без указания сервера


@dataclass
class Catalog:
    tools: dict[str, dict[str, dict]] = field(default_factory=dict)  # сервер -> имя -> схема
    problems: list[str] = field(default_factory=list)
    unchecked: set[str] = field(default_factory=set)               # непроверяемые ключевые слова схем

    @classmethod
    def from_file(cls, path: str | Path) -> "Catalog":
        with open(path, encoding="utf-8") as f:
            return cls.from_obj(loads_strict(f.read()))

    @classmethod
    def from_obj(cls, obj) -> "Catalog":
        cat = cls()
        if isinstance(obj, dict) and isinstance(obj.get("servers"), dict):
            for server, tools in obj["servers"].items():
                if isinstance(tools, dict):
                    tools = tools.get("tools")
                cat._add_list(tools, server)
        elif isinstance(obj, dict) and "tools" in obj:
            cat._add_list(obj["tools"], None)
        elif isinstance(obj, list):
            cat._add_list(obj, None)
        else:
            cat.problems.append("каталог: ожидается список инструментов, {tools: [...]} или {servers: {...}}")
        return cat

    def _add_list(self, tools, server: str | None) -> None:
        if not isinstance(tools, list):
            self.problems.append(f"каталог {server or ''}: ожидается список инструментов")
            return
        for i, item in enumerate(tools):
            if not isinstance(item, dict):
                self.problems.append(f"инструмент #{i}: ожидается объект")
                continue
            fn = item.get("function") if isinstance(item.get("function"), dict) else item
            name = fn.get("name")
            schema = fn.get("inputSchema", fn.get("parameters", {"type": "object"}))
            srv = server if server is not None else item.get("server", ANY_SERVER)
            if not isinstance(name, str) or not name:
                self.problems.append(f"инструмент #{i}: нет имени")
                continue
            if not isinstance(srv, str):
                self.problems.append(f"{name}: server должен быть строкой")
                continue
            bucket = self.tools.setdefault(srv, {})
            if name in bucket:
                self.problems.append(f"{srv + '/' if srv else ''}{name}: повтор имени")
                continue
            check = argschema.check_schema(schema, f"{name}#")
            self.problems.extend(check.problems)
            self.unchecked |= check.unchecked
            bucket[name] = schema

    def __len__(self) -> int:
        return sum(len(b) for b in self.tools.values())

    @property
    def servers(self) -> list[str]:
        return sorted(s for s in self.tools if s != ANY_SERVER)

    def lookup(self, server: str | None, name: str) -> tuple[dict | None, str | None]:
        """Схема инструмента или (None, причина)."""
        if server and server in self.tools and name in self.tools[server]:
            return self.tools[server][name], None
        if name in self.tools.get(ANY_SERVER, {}):
            return self.tools[ANY_SERVER][name], None
        if server and self.servers and server not in self.tools:
            return None, f"сервера {server!r} нет в каталоге (есть: {', '.join(self.servers)})"
        where = f" сервера {server!r}" if server else ""
        return None, f"инструмента {name!r}{where} нет в каталоге"

    def to_json(self) -> str:
        return json.dumps(self.tools, ensure_ascii=False, sort_keys=True)
