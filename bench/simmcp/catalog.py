"""Каталоги инструментов sim-mcp — те же имена, описания и схемы, что у hw-mcp (IMPLEMENTATION_SPEC §4).

generic  — list_devices, read_property(device, property), write_property(device, property, value),
           invoke_action(device, action, args) + board_health; тексты описаний и схемы — дословно как в
           hwmcp/internal/server (addGenericTools), чтобы токены каталога в T0 совпадали с платой;
explicit — инструмент на свойство: read_<dev>_<prop>, set_<dev>_<prop>(value) (только для записываемых),
           <action>_<dev>(<параметры действия>) — как в hw-mcp (addExplicitTools) + board_health; у set_*
           в схеме value есть enum/minimum/maximum из daygen (у устройств sysfs их нет).
Инструменты оценки answer(value, unit) и refuse(reason) (THESIS_CONCEPT §12.8) — только в T0: на плате их нет.
Их можно убрать (``eval_tools=False``) для прогонов затрат, а их долю токенов — получить из ``eval_share``.

Порядок инструментов детерминированный (стабильный префикс промпта): generic — фиксированный список,
explicit — по id устройства, внутри: read_*, set_*, действия; затем board_health, answer, refuse.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .world import action_description, action_input, value_schema

REFUSE_CODES = ["no_such_device", "no_such_room", "unsafe", "unsupported", "ambiguous", "other"]
EVAL_TOOLS = ("answer", "refuse")
READ_ONLY_OPS = ("list", "read", "health", "answer", "refuse")


@dataclass(frozen=True)
class Route:
    """Куда ведёт вызов инструмента: op ∈ list|read|write|invoke|health|answer|refuse."""

    op: str
    device: str | None = None
    prop: str | None = None
    action: str | None = None


def _tool(name: str, description: str, props: dict, required: list[str] | None = None,
          read_only: bool = True) -> dict:
    schema: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    ann = {"readOnlyHint": True, "openWorldHint": False} if read_only else \
        {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
    return {"name": name, "description": description, "inputSchema": schema, "annotations": ann}


COMMON_EVAL = [
    _tool("answer", "Final answer to the user's question: value and unit (t = time of day HH:MM, Cel, %RH, "
                    "count, room, color, bool).",
          {"value": {"description": "number, string or boolean"}, "unit": {"type": "string"}},
          ["value", "unit"]),
    _tool("refuse", "Refuse a request that cannot or must not be executed.",
          {"reason": {"type": "string", "description": "|".join(REFUSE_CODES)}}, ["reason"]),
]
HEALTH = _tool("board_health", "Board health: CPU load, memory, SoC temperature, throttling, hw-mcp counters", {})


def _where(dev: dict) -> str:
    """« @<место>», если место не читается из id (как where в hw-mcp)."""
    loc = dev.get("location") or ""
    return f" @{loc}" if loc and loc not in dev["id"] else ""


def generic_tools(eval_tools: bool = True) -> tuple[list[dict], dict[str, Route]]:
    s = {"type": "string"}
    tools = [
        _tool("list_devices", "List connected devices: properties (unit; rw = writable) and actions with arguments",
              {"all": {"type": "boolean", "description": "include devices without properties (cameras, USB, buses)"}}),
        _tool("read_property", "Read the current value of a device property",
              {"device": s, "property": s}, ["device", "property"]),
        _tool("write_property", "Write a writable device property (checked by the safety gate)",
              {"device": s, "property": s, "value": {}}, ["device", "property", "value"], read_only=False),
        _tool("invoke_action", "Invoke a device action (checked by the safety gate)",
              {"device": s, "action": s, "args": {"type": "object"}}, ["device", "action"], read_only=False),
        HEALTH,
    ]
    routes = {"list_devices": Route("list"), "read_property": Route("read"), "write_property": Route("write"),
              "invoke_action": Route("invoke"), "board_health": Route("health")}
    if eval_tools:
        tools += COMMON_EVAL
        routes.update(answer=Route("answer"), refuse=Route("refuse"))
    return tools, routes


def explicit_tools(devices: list[dict], eval_tools: bool = True) -> tuple[list[dict], dict[str, Route]]:
    tools: list[dict] = []
    routes: dict[str, Route] = {}
    for d in sorted(devices, key=lambda x: x["id"]):
        dev, where = d["id"], _where(d)
        props = sorted(d["properties"].items())
        for p, spec in props:
            name = f"read_{dev}_{p}"
            unit = f" ({spec['unit']})" if spec.get("unit") else ""
            tools.append(_tool(name, f"Read {p}{unit} of {dev}{where}", {}))
            routes[name] = Route("read", dev, p)
        for p, spec in props:
            if spec.get("rw"):
                name = f"set_{dev}_{p}"
                tools.append(_tool(name, f"Set {p} ({spec['type']}) of {dev}{where}",
                                   {"value": value_schema(spec)}, ["value"], read_only=False))
                routes[name] = Route("write", dev, p)
        for a, aspec in sorted(d.get("actions", {}).items()):
            name = f"{a}_{dev}"
            inp = action_input(aspec)
            tools.append(_tool(name, f"{action_description(d, a, aspec)} ({dev}{where})", inp["properties"],
                               read_only=False))
            routes[name] = Route("invoke", dev, action=a)
    tools.append(HEALTH)
    routes["board_health"] = Route("health")
    if eval_tools:
        tools += COMMON_EVAL
        routes.update(answer=Route("answer"), refuse=Route("refuse"))
    return tools, routes


def build(catalog: str, devices: list[dict], eval_tools: bool = True) -> tuple[list[dict], dict[str, Route]]:
    if catalog == "generic":
        return generic_tools(eval_tools)
    if catalog == "explicit":
        return explicit_tools(devices, eval_tools)
    raise ValueError("catalog: generic | explicit")


def route_args(route: Route, args: dict) -> dict:
    """Аргументы вызова → нормализованный вид generic-операции (для журнала и оценки)."""
    if route.op == "read":
        return {"device": route.device or args.get("device"), "property": route.prop or args.get("property")}
    if route.op == "write":
        return {"device": route.device or args.get("device"), "property": route.prop or args.get("property"),
                "value": args.get("value")}
    if route.op == "invoke":
        if route.device:
            return {"device": route.device, "action": route.action, "args": dict(args)}
        return {"device": args.get("device"), "action": args.get("action"), "args": args.get("args") or {}}
    return dict(args)


def openai_tools(tools: list[dict]) -> list[dict]:
    """Каталог MCP → tools запроса Chat Completions (как его передаёт LLM edgectx)."""
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["inputSchema"]}} for t in tools]


def _json_bytes(tools: list[dict]) -> int:
    return len(json.dumps(openai_tools(tools), ensure_ascii=False, separators=(",", ":")).encode())


def eval_share_bytes(tools: list[dict]) -> dict:
    """Байты JSON каталога (в виде tools Chat Completions) — всего и инструментов оценки answer/refuse."""
    ev = [t for t in tools if t["name"] in EVAL_TOOLS]
    return {"eval_tools": [t["name"] for t in ev], "bytes_total": _json_bytes(tools), "bytes_eval": _json_bytes(ev)}


def eval_share(tools: list[dict]) -> dict:
    """Доля инструментов оценки (answer/refuse) в каталоге: байты и токены (o200k, приближение OpenAI Cookbook из
    bench/mockllm — тем же счётом mock-llm считает prompt_tokens). Нужна, чтобы вычесть её при переносе токенов
    T0 на плату, где answer/refuse нет."""
    out: dict[str, Any] = eval_share_bytes(tools)
    try:
        from bench.mockllm.tokens import count_tools, load_tokenizer
        tok = load_tokenizer()
        rest = [t for t in tools if t["name"] not in EVAL_TOOLS]
        out["tokens_total"] = count_tools(openai_tools(tools), tok)
        out["tokens_eval"] = out["tokens_total"] - count_tools(openai_tools(rest), tok)   # предельный вклад
        out["tokenizer"] = type(tok).__name__
    except Exception as e:  # noqa: BLE001 — оценка токенов необязательна
        out["tokens_error"] = f"{type(e).__name__}: {e}"
    return out
