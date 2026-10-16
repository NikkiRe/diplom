"""Сколько стоит «контекст оборудования» в разных представлениях.

Из инвентаря sysfs_probe.py строятся представления двух уровней (как L0/L1
в заметке B, §6.1):

L0 - каталог (что подключено и что с ним можно делать):
  td_full       W3C WoT TD 1.1 на каждое устройство (с @context, security, forms);
  td_agent      те же TD без служебных полей (то, что шлюз может показать LLM);
  mcp_explicit  MCP-инструменты «по инструменту на свойство» (стратегия explicit
                в macc-n/wot-mcp): get_* на чтение, set_* на запись;
  mcp_generic   4 универсальных инструмента (list_devices, read_property,
                write_property, invoke_action; как generic в wot-mcp и core
                tools в Web-of-Drones) + ответ list_devices;
  hw_manifest   самодельный компактный манифест в духе заметок B/D (hw://, DCP);
  text          одна строка текста на устройство.
L1 - снимок состояния:
  senml         SenML JSON (RFC 8428) с bn/bt;
  custom_json   {id: {канал: значение}} + ts (как пример hw:// в заметке B);
  text_state    одна строка «id канал=значение единица».

Считаются байты компактного JSON и токены o200k_base (GPT-4o/4.1/5 mini/nano).
Токены инструментов считаются по JSON-полю tools запроса OpenAI Chat
Completions. Как провайдер разворачивает tools внутри промпта, не публикуется,
поэтому это оценка снизу/сбоку, а не точная цена; сравнение представлений
между собой при этом корректно (один токенизатор, один набор данных).

Все TD проверяются по официальной JSON Schema TD 1.1 (W3C, версия
1.1-12-March-2025) мини-валидатором minijsonschema.py.

Запуск: python3 ctx_formats.py inventory_fixture.json [--scale 1,5,20]
(токены - только если доступен tiktoken и .cache/o200k_base.tiktoken).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
from typing import Any, Callable

from minijsonschema import Validator

HERE = os.path.dirname(os.path.abspath(__file__))
TD_SCHEMA = os.path.join(HERE, "td-schema-v1.1_w3c_2025-03-12.json")
O200K = os.path.join(HERE, ".cache", "o200k_base.tiktoken")
TS = 1791295200  # 2026-10-06T16:00:00Z, фиксированное время для воспроизводимости

# ₽ за 1 млн входных токенов RouterAI (F_artifacts/price_table_output_2026-10-06.txt)
PRICES = {"gpt-5-mini": (27.60, 2.76), "gpt-4.1-nano": (11.04, 2.76),
          "gpt-4o-mini": (16.56, 8.28)}
CALLS_PER_DAY = 480  # сценарий F: 3 x 20 x 8


def compact(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def get_counter() -> tuple[Callable[[str], int] | None, str]:
    try:
        import tiktoken
        from tiktoken.load import load_tiktoken_bpe
    except ImportError:
        return None, "tiktoken не установлен: только байты"
    if not os.path.exists(O200K):
        return None, "нет .cache/o200k_base.tiktoken: запустите prepare_o200k.py"
    # pat_str и спецтокены скопированы из tiktoken_ext/openai_public.py (tiktoken 0.14.0);
    # вызывать o200k_base() нельзя: функция сама скачивает словарь из сети.
    pat_str = "|".join([
        r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
        r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
        r"""\p{N}{1,3}""",
        r""" ?[^\s\p{L}\p{N}]+[\r\n/]*""",
        r"""\s*[\r\n]+""",
        r"""\s+(?!\S)""",
        r"""\s+""",
    ])
    ranks = load_tiktoken_bpe(O200K, expected_hash="446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d")
    enc = tiktoken.Encoding(name="o200k_base", pat_str=pat_str, mergeable_ranks=ranks,
                            special_tokens={"<|endoftext|>": 199999, "<|endofprompt|>": 200018})
    return (lambda s: len(enc.encode(s, disallowed_special=()))), "o200k_base (sha256 сверен)"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def devices_with_channels(inv: list[dict]) -> list[dict]:
    return [d for d in inv if d["channels"]]


def short_id(d: dict) -> str:
    base = slug(d["name"]) if d["source"] != "declared" else slug(d["name"])
    return f"{d.get('location') or 'unplaced'}.{base}"


def ch_type(c: dict) -> str:
    return "boolean" if c["unit"] == "/" else "number"


def form_href(c: dict) -> str:
    if c["how"] == "gpiod":
        chip, line = c["path"].split("#")
        return f"gpiod://{os.path.basename(chip)}/{line}"  # нестандартная схема URI
    return "file:///" + c["path"]


# ---------- L0 ----------
def td_full(d: dict) -> dict:
    props = {}
    for c in d["channels"]:
        ops = ["readproperty"] + (["writeproperty"] if c["writable"] else [])
        p = {"title": c["name"], "type": ch_type(c), "readOnly": not c["writable"],
             "forms": [{"href": form_href(c), "contentType": "text/plain", "op": ops}]}
        if ch_type(c) == "number":
            p["unit"] = c["unit"]
        props[slug(c["name"])] = p
    td = {"@context": "https://www.w3.org/2022/wot/td/v1.1",
          "id": "urn:dev:edge:" + short_id(d),
          "title": short_id(d),
          "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
          "security": "nosec_sc",
          "properties": props}
    if d.get("description"):
        td["description"] = d["description"]
    return td


def td_agent(td: dict) -> dict:
    t = {k: v for k, v in td.items() if k in ("title", "description")}
    t["properties"] = {}
    for n, p in td["properties"].items():
        q = {k: v for k, v in p.items() if k in ("type", "unit", "readOnly")}
        t["properties"][n] = q
    return t


def mcp_tool(name: str, desc: str, props: dict, required: list[str]) -> dict:
    params = {"type": "object", "properties": props}
    if required:
        params["required"] = required
    return {"type": "function", "function": {"name": name, "description": desc,
                                              "parameters": params}}


def mcp_explicit(tds: list[dict]) -> list[dict]:
    tools = []
    for td in tds:
        tid = slug(td["title"])
        for n, p in td["properties"].items():
            unit = f" ({p['unit']})" if "unit" in p else ""
            tools.append(mcp_tool(f"get_{tid}_{n}", f"Read {n}{unit} of {td['title']}", {}, []))
            if not p["readOnly"]:
                tools.append(mcp_tool(f"set_{tid}_{n}", f"Write {n} of {td['title']}",
                                      {"value": {"type": p["type"]}}, ["value"]))
    return tools


def mcp_generic_tools() -> list[dict]:
    s = {"type": "string"}
    return [
        mcp_tool("list_devices", "List connected devices and their properties", {}, []),
        mcp_tool("read_property", "Read a device property",
                 {"device_id": s, "property": s}, ["device_id", "property"]),
        mcp_tool("write_property", "Write a writable device property",
                 {"device_id": s, "property": s, "value": {}}, ["device_id", "property", "value"]),
        mcp_tool("invoke_action", "Invoke a device action",
                 {"device_id": s, "action": s, "params": {"type": "object"}}, ["device_id", "action"]),
    ]


def list_devices_result(tds: list[dict]) -> list[dict]:
    out = []
    for td in tds:
        props = {}
        for n, p in td["properties"].items():
            props[n] = (p.get("unit", "bool")) + ("" if p["readOnly"] else ",rw")
        out.append({"id": td["title"], "props": props})
    return out


def hw_manifest(devs: list[dict]) -> dict:
    return {"devices": [{"id": short_id(d),
                         "ch": [[slug(c["name"]), c["unit"], "rw" if c["writable"] else "r"]
                                for c in d["channels"]]} for d in devs]}


def text_inventory(devs: list[dict]) -> str:
    lines = []
    for d in devs:
        cs = ", ".join(f"{slug(c['name'])} [{c['unit']}{', rw' if c['writable'] else ''}]"
                       for c in d["channels"])
        lines.append(f"{short_id(d)}: {cs}")
    return "\n".join(lines)


# ---------- L1 ----------
def readings(devs: list[dict]) -> list[tuple[str, str, str, Any]]:
    r = []
    for d in devs:
        for c in d["channels"]:
            v = c["value"]
            if v is None and c["how"] == "gpiod":
                v = False  # синтетическое состояние линии
            r.append((short_id(d), slug(c["name"]), c["unit"], v))
    return r


def senml(devs: list[dict]) -> list[dict]:
    pack, first, cur = [], True, None
    for did, ch, unit, v in readings(devs):
        rec: dict[str, Any] = {}
        if did != cur:
            rec["bn"] = f"urn:dev:edge:{did}:"
            cur = did
        if first:
            rec["bt"] = TS
            first = False
        rec["n"] = ch
        if unit == "/":
            rec["vb"] = bool(v)
        else:
            rec["u"] = unit
            rec["v"] = v
        pack.append(rec)
    return pack


def custom_json(devs: list[dict]) -> dict:
    o: dict[str, Any] = {"ts": "2026-10-06T16:00:00Z"}
    for did, ch, unit, v in readings(devs):
        o.setdefault(did, {})[ch] = v
    return o


def text_state(devs: list[dict]) -> str:
    parts: dict[str, list[str]] = {}
    for did, ch, unit, v in readings(devs):
        u = "" if unit == "/" else f" {unit}"
        parts.setdefault(did, []).append(f"{ch}={v}{u}")
    return "ts=2026-10-06T16:00Z; " + "; ".join(f"{k} " + " ".join(v) for k, v in parts.items())


def scale_inventory(devs: list[dict], k: int) -> list[dict]:
    """k «комнат» с одинаковым набором устройств (для оценки роста каталога)."""
    out = []
    for i in range(k):
        for d in devs:
            e = copy.deepcopy(d)
            if k > 1:
                e["location"] = f"{d.get('location') or 'unplaced'}{i + 1}"
            out.append(e)
    return out


def build_all(devs: list[dict]) -> tuple[dict[str, str], list[dict]]:
    tds = [td_full(d) for d in devs]
    reps = {
        "L0.td_full": compact(tds),
        "L0.td_agent": compact([td_agent(t) for t in tds]),
        "L0.mcp_explicit.tools": compact(mcp_explicit(tds)),
        "L0.mcp_generic.tools": compact(mcp_generic_tools()),
        "L0.mcp_generic.list_devices": compact(list_devices_result(tds)),
        "L0.hw_manifest": compact(hw_manifest(devs)),
        "L0.text": text_inventory(devs),
        "L1.senml": compact(senml(devs)),
        "L1.custom_json": compact(custom_json(devs)),
        "L1.text_state": text_state(devs),
    }
    return reps, tds


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("inventory")
    ap.add_argument("--scale", default="1,5,20")
    ap.add_argument("--dump", help="каталог для сохранения представлений (k=1)")
    a = ap.parse_args()
    inv = json.load(open(a.inventory))
    base = devices_with_channels(inv)
    count, how = get_counter()
    validator = Validator(json.load(open(TD_SCHEMA)))
    print(f"# устройств с каналами: {len(base)}; токенизатор: {how}")
    results = {}
    for k in [int(x) for x in a.scale.split(",")]:
        devs = scale_inventory(base, k)
        reps, tds = build_all(devs)
        bad = [(t["title"], validator.errors(t)[:2]) for t in tds if validator.errors(t)]
        n_props = sum(len(t["properties"]) for t in tds)
        print(f"\n## k={k}: устройств {len(devs)}, свойств {n_props}, "
              f"TD валидны по схеме TD 1.1: {len(tds) - len(bad)}/{len(tds)} {bad[:1] if bad else ''}")
        print(f"{'представление':32s} {'байт':>8s} {'токенов':>8s}")
        results[k] = {}
        for name, s in reps.items():
            b = len(s.encode())
            t = count(s) if count else None
            results[k][name] = (b, t)
            print(f"{name:32s} {b:8d} {t if t is not None else '-':>8}")
        if count:
            g = results[k]["L0.mcp_generic.tools"][1] + results[k]["L0.mcp_generic.list_devices"][1]
            e = results[k]["L0.mcp_explicit.tools"][1]
            print(f"explicit/generic(tools+list) = {e}/{g} = {e / g:.2f}")
            for m, (pin, pcache) in PRICES.items():
                day_e = e * CALLS_PER_DAY * pin / 1e6
                day_g = g * CALLS_PER_DAY * pin / 1e6
                print(f"  {m}: каталог в каждом из {CALLS_PER_DAY} вызовов/сутки без кэша: "
                      f"explicit {day_e:.2f} ₽, generic {day_g:.2f} ₽; "
                      f"с полным попаданием в кэш: {e * CALLS_PER_DAY * pcache / 1e6:.2f} / "
                      f"{g * CALLS_PER_DAY * pcache / 1e6:.2f} ₽")
        if a.dump and k == 1:
            os.makedirs(a.dump, exist_ok=True)
            for name, s in reps.items():
                ext = "txt" if name.endswith(("text", "text_state")) else "json"
                with open(os.path.join(a.dump, f"{name}.{ext}"), "w") as f:
                    f.write(s if ext == "txt" else json.dumps(json.loads(s), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
