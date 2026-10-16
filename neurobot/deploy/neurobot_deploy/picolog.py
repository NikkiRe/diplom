"""Журнал PicoClaw (0.2.8 и 0.3.1): подключён ли MCP, не «зомби» ли агент, не висит ли вызов, жив ли канал
(ANALYSIS §3, R3 §6, R7 §5). Строки нужного уровня печатаются только при gateway.log_level info (N15).

Строки — как их пишет zerolog ConsoleWriter PicoClaw, например
    03:14:08 INF tool …/registry.go:258 > Tool execution started args={…} tool=mcp_ros_mcp_robot_nudge
Разбирается журнал одного запуска юнита (InvocationID): после рестарта старые строки не мешают.
Признаки (строки — pkg/mcp/manager.go, pkg/agent/agent_mcp.go, pkg/tools/registry.go, pkg/gateway):
  - «All MCP servers failed to connect» / «Failed to load MCP servers» — агентный цикл завершён, а /health
    отвечает 200 (PC5): поможет только рестарт;
  - «invalid trailing data» (мусор в stdout stdio-сервера, PC2) и «connection closed» (PC1) — фатальны,
    пока после них нет ни одного успешного вызова MCP-инструмента;
  - «Tool execution started» без completed/failed — вызов висит (у PicoClaw нет таймаута вызова, PC4);
  - «Long poll failed» не от остановки шлюза (не «context canceled») — канал VK больше не слушает: цикл long poll
    после любой ошибки не перезапускается (N10), поможет только рестарт;
  - перезагрузка конфига (PC3) — для MCP по HTTP безвредна, только считается.
Строки с ключами (key=, access_token=, токены eyJ…) хранятся и показываются с <REDACTED>.
"""
from __future__ import annotations

import re
from typing import Iterable, List, NamedTuple, Optional, Tuple

from .sd import Entry

ZOMBIE = re.compile(r"All MCP servers failed to connect|Failed to load MCP servers")
BROKEN = re.compile(r"invalid trailing data|connection closed|client is closing")
CHANNEL = re.compile(r"Long poll failed")
SHUTDOWN = "context canceled"
_SECRET = re.compile(r"((?:key|access_token|token)=)[^&\s\"']+|eyJ[\w-]{8,}(?:\.[\w-]+){1,2}")
RELOAD = re.compile(r"Manual reload triggered|Config file changed, reloading")
STARTED = "Tool execution started"
FINISHED = ("Tool execution completed", "Tool execution failed", "Tool started (async)",
            "Tool execution panic recovered")
_FIELD = r"(?:^|\s){}=(\S+)"


class Fatal(NamedTuple):
    code: str        # agent_zombie | agent_mcp_lost | agent_channel_lost
    ts: float
    line: str


class AgentLog(NamedTuple):
    connected: List[str]               # MCP-серверы из «Connected to MCP server … server=<имя>»
    init_done: bool                    # «MCP server initialization complete»
    fatal: Optional[Fatal]
    pending: List[Tuple[str, float]]   # (инструмент, время начала) вызовов без завершения
    reloads: int

    def pending_age(self, now: float) -> float:
        return max((now - ts for _, ts in self.pending), default=0.0)


def redact(line: str) -> str:
    return _SECRET.sub(lambda m: (m.group(1) or "") + "<REDACTED>", line)


def _fatal_code(line: str) -> Optional[str]:
    if ZOMBIE.search(line):
        return "agent_zombie"
    if BROKEN.search(line):
        return "agent_mcp_lost"
    if CHANNEL.search(line) and SHUTDOWN not in line:
        return "agent_channel_lost"
    return None


def _field(name: str, line: str) -> str:
    found = re.findall(_FIELD.format(name), line)
    return found[-1] if found else ""


def _drop_first(pending: List[Tuple[str, float]], tool: str) -> List[Tuple[str, float]]:
    for i, (name, _) in enumerate(pending):
        if name == tool:
            return pending[:i] + pending[i + 1:]
    return pending


def analyze(entries: Iterable[Entry]) -> AgentLog:
    connected, init_done, fatal, pending, reloads = [], False, None, [], 0
    for e in sorted(entries, key=lambda x: x.ts):
        line = e.message
        code = _fatal_code(line)
        if code and (code == "agent_zombie" or fatal is None or fatal.code != "agent_zombie"):
            fatal = Fatal(code, e.ts, redact(line.strip()))
        if "Connected to MCP server" in line:
            connected.append(_field("server", line) or "?")
        elif "MCP server initialization complete" in line:
            init_done = True
        elif RELOAD.search(line):
            reloads += 1
        if STARTED in line:
            pending.append((_field("tool", line), e.ts))
        elif any(f in line for f in FINISHED):
            tool = _field("tool", line)
            pending = _drop_first(pending, tool)
            if (FINISHED[0] in line and tool.startswith("mcp_") and fatal is not None
                    and fatal.code == "agent_mcp_lost"):
                fatal = None  # MCP снова отвечает — обрыв был временным
    return AgentLog(connected, init_done, fatal, pending, reloads)
