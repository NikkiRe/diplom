#!/usr/bin/env python3
"""Эталонные данные для тестов паритета hw-mcp (Go) с Python-прототипами исследования.

Прототипы только читаются (каталог research/ не изменяется):
  * research/new_topic/GAP2_artifacts/sysfs_probe.py  — инвентаризация sysfs;
  * research/new_topic/GAP3_artifacts/actuator_gate.py — шлюз приводов (+ bench_gate.py: генератор потока);
  * monitor/argschema.py                               — валидатор JSON Schema, который использует шлюз.

Что пишется в hwmcp/testdata/golden/:
  inventory.json          — sysfs_probe.probe(testdata/fixture_root, testdata/declared.json);
  iio_cases.json          — iio_channel_value на синтетических каталогах (scale на тип, raw без scale, ...);
  argschema.json.gz       — корпус (схема, значение, strict) -> [(kind, path)] и check_schema -> unchecked/problems;
  gate_bench.jsonl.gz     — поток bench_gate.run (seed 1): обновления состояния, команда, решение, tick;
  gate_fuzz.jsonl.gz      — случайные операции (check / dry_run / commit / renew / tick / state) с границами.

Запуск (из корня репозитория):
  .venv/bin/python hwmcp/tools/gen_golden.py
Результат детерминирован (фиксированные seed); повторный запуск даёт те же файлы.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import random
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HW = HERE.parent
REPO = HW.parent
GAP2 = REPO / "research" / "new_topic" / "GAP2_artifacts"
GAP3 = REPO / "research" / "new_topic" / "GAP3_artifacts"
OUT = HW / "testdata" / "golden"

sys.dont_write_bytecode = True  # не оставлять __pycache__ в research/
for p in (REPO, GAP2, GAP3):
    sys.path.insert(0, str(p))

import sysfs_probe as sp  # noqa: E402
from actuator_gate import Command, Gate, StateStore, demo_setup  # noqa: E402
import bench_gate  # noqa: E402
from monitor import argschema  # noqa: E402


def dump_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def write_gz_lines(path: Path, lines) -> None:
    # mtime=0 — чтобы архив был побайтно воспроизводим.
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        for obj in lines:
            gz.write((json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    path.write_bytes(buf.getvalue())


# --------------------------------------------------------------------------------------------------
# 1. Инвентаризация sysfs

def gen_inventory() -> None:
    root = HW / "testdata" / "fixture_root"
    decl = json.loads((HW / "testdata" / "declared.json").read_text(encoding="utf-8"))
    inv = sp.probe(str(root), decl)
    dump_json(OUT / "inventory.json", inv)
    # без declared.json — проверка, что слой профилей ничего не меняет в обнаруженном
    dump_json(OUT / "inventory_nodecl.json", sp.probe(str(root), None))


def gen_iio_cases() -> None:
    """iio_channel_value на маленьких каталогах: (файлы, base, typ) -> (value, how)."""
    cases = [
        ({"in_voltage1_raw": "100\n", "in_voltage_scale": "0.5\n"}, "in_voltage1", "voltage"),
        ({"in_temp_raw": "123\n"}, "in_temp", "temp"),
        ({"in_temp_input": "21500\n", "in_temp_raw": "1\n", "in_temp_scale": "1000\n"}, "in_temp", "temp"),
        ({"in_voltage0_raw": "1650\n", "in_voltage0_scale": "2.000000000\n", "in_voltage0_offset": "-25\n"},
         "in_voltage0", "voltage"),
        ({"in_voltage0_raw": "1650\n", "in_voltage_scale": "0.125\n", "in_voltage_offset": "10\n"},
         "in_voltage0", "voltage"),
        ({"in_voltage0_raw": "1650\n", "in_voltage0_scale": "0.125\n", "in_voltage_offset": "10\n",
          "in_voltage0_offset": "-5\n"}, "in_voltage0", "voltage"),
        ({"in_voltage0_raw": "garbage\n", "in_voltage0_scale": "1\n"}, "in_voltage0", "voltage"),
        ({"in_temp_input": "not-a-number\n", "in_temp_raw": "200\n", "in_temp_scale": "0.01\n"}, "in_temp", "temp"),
        ({}, "in_temp", "temp"),
        ({"in_pressure_raw": "25939200\n", "in_pressure_scale": "0.000003906\n"}, "in_pressure", "pressure"),
        ({"in_accel_x_raw": "-512\n", "in_accel_scale": "0.009576\n"}, "in_accel_x", "accel"),
        ({"in_illuminance_raw": "  42 \n\x00", "in_illuminance_scale": "1.5\x00\n"}, "in_illuminance", "illuminance"),
        ({"in_voltage0_raw": "7\n", "in_voltage0_scale": "0\n", "in_voltage0_offset": "0\n"}, "in_voltage0", "voltage"),
    ]
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (files, base, typ) in enumerate(cases):
            d = os.path.join(tmp, f"c{i}")
            os.makedirs(d)
            for name, text in files.items():
                with open(os.path.join(d, name), "w") as f:
                    f.write(text)
            v, how = sp.iio_channel_value(d, base, typ)
            out.append({"files": files, "base": base, "typ": typ, "value": v, "how": how})
    dump_json(OUT / "iio_cases.json", out)


# --------------------------------------------------------------------------------------------------
# 2. Валидатор аргументов (monitor/argschema.py)

SCHEMAS = [
    {"type": "integer"},
    {"type": "number", "minimum": 0, "maximum": 100},
    {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1},
    {"type": "number", "minimum": 0, "exclusiveMinimum": True, "maximum": 10, "exclusiveMaximum": True},
    {"type": ["integer", "null"]},
    {"type": "string", "nullable": True, "minLength": 2, "maxLength": 4},
    {"type": "boolean"},
    {"enum": [1, 2, "a", None, [1, 2], {"k": True}]},
    {"const": 0},
    {"const": None},
    {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 1, "maxItems": 3, "uniqueItems": True},
    {"type": "array", "items": [{"type": "string"}, {"type": "number"}]},
    {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "integer"}}, "required": ["a"],
     "additionalProperties": False},
    {"type": "object", "properties": {"a/b": {"type": "string"}, "c~d": {"type": "integer"}},
     "additionalProperties": {"type": "boolean"}, "minProperties": 1, "maxProperties": 2},
    {"type": "object", "patternProperties": {"^x": {"type": "string"}}, "additionalProperties": False},
    {"anyOf": [{"type": "string"}, {"type": "integer", "minimum": 5}]},
    {"oneOf": [{"type": "integer"}, {"type": "number", "minimum": 0}]},
    {"allOf": [{"type": "number"}, {"maximum": 3}, {"minimum": 1}]},
    {"type": "object", "properties": {"deg": {"type": "integer", "minimum": 0, "maximum": 90}},
     "required": ["deg"], "additionalProperties": False},
    {"type": "object", "properties": {c: {"type": "integer", "minimum": 0, "maximum": 255} for c in "rgb"},
     "required": ["r", "g", "b"], "additionalProperties": False},
    {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"], "additionalProperties": False},
    {"type": "object", "additionalProperties": False},
    {"type": "object", "properties": {"n": {"type": "object", "properties": {"m": {"type": "array",
     "items": {"type": "object", "required": ["z"]}}}}}},
    True,
    False,
    {},
    {"not": {"type": "string"}, "type": "string"},
    {"type": "object", "required": ["x", "y"], "properties": {"x": False}},
]

INSTANCES = [
    None, True, False, 0, 1, -1, 2, 3, 5, 7, 10, 90, 91, 100, 101, 255, 256, 10 ** 20,
    0.0, 1.0, 2.0, 2.5, 3.0, 45.5, -0.5, 100.0, 1e-9, 0.999999,
    "", "a", "ab", "abcd", "abcde", "1", "привет",
    [], [1], [1, 1], [1, 2, 3], [1, 2, 3, 4], ["x", 1], [1, "x"], ["x", 1, True], [-1], [1.0, 1],
    {}, {"a": "x"}, {"a": 1}, {"a": "x", "b": 2}, {"a": "x", "b": 2.0}, {"a": "x", "c": 1}, {"b": 1},
    {"a/b": "s", "c~d": 1}, {"a/b": 1, "z": "no"}, {"z": True}, {"z": True, "q": False, "a/b": "s"},
    {"x1": "s"}, {"x1": 1, "y": 2}, {"deg": 45}, {"deg": 45.0}, {"deg": 91}, {"deg": -1}, {"deg": 45, "x": 1},
    {"r": 1, "g": 2, "b": 3}, {"r": 300, "g": 0, "b": 0}, {"r": 1, "g": 2}, {"on": True}, {"on": 1}, {"on": True, "temp": 90},
    {"n": {"m": [{"z": 1}, {}, 3]}}, {"x": 1, "y": 2}, {"y": 2},
    {"k": True}, [1, 2.0],
]


def gen_argschema() -> None:
    cases = []
    for si, schema in enumerate(SCHEMAS):
        for ii, inst in enumerate(INSTANCES):
            for strict in (False, True):
                errs = argschema.validate(inst, schema, strict_integer=strict)
                cases.append({"s": si, "i": ii, "strict": strict,
                              "errors": sorted([e.kind, e.path] for e in errs)})
    checks = []
    bad_schemas = [
        {"type": "float"}, {"type": []}, {"type": ["string", "string"]}, {"enum": 3}, {"required": [1]},
        {"required": ["a", "a"]}, {"properties": []}, {"additionalProperties": 3}, {"items": 3},
        {"minimum": "0"}, {"exclusiveMinimum": "x"}, {"minLength": -1}, {"uniqueItems": 1},
        {"anyOf": []}, {"oneOf": [{"type": "dict"}]}, {"$ref": "#/x", "description": "d", "x-ext": 1},
        {"patternProperties": {}, "if": {}, "then": {}}, 7,
    ]
    for schema in SCHEMAS + bad_schemas:
        r = argschema.check_schema(schema)
        checks.append({"schema": schema, "n_problems": len(r.problems), "unchecked": sorted(r.unchecked)})
    buf = {"schemas": SCHEMAS, "instances": INSTANCES, "cases": cases, "check_schema": checks}
    raw = json.dumps(buf, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    bio = io.BytesIO()
    with gzip.GzipFile(fileobj=bio, mode="wb", mtime=0) as gz:
        gz.write(raw)
    (OUT / "argschema.json.gz").write_bytes(bio.getvalue())


# --------------------------------------------------------------------------------------------------
# 3. Шлюз приводов

def cmd_json(c: Command) -> dict:
    return {"cmd_id": c.cmd_id, "device": c.device, "tool": c.tool, "args": c.args,
            "issued_at": c.issued_at, "ttl_s": c.ttl_s, "confirmed": c.confirmed}


def dec_json(d) -> dict:
    return {"verdict": d.verdict, "reason": d.reason, "args": d.args}


def ev_json(evs) -> list:
    return [[ts, dev, safe] for ts, dev, safe in evs]


def gen_gate_bench(n: int = 10_000, seed: int = 1, dt: float = 0.5) -> None:
    """Тот же поток, что bench_gate.run(n, seed), но с записью каждого шага."""
    devices, interlocks = demo_setup()
    state = StateStore()
    gate = Gate(devices, interlocks, state)
    gen = bench_gate.Generator(seed)
    r = random.Random(seed + 1)
    cats, weights = zip(*bench_gate.CATS.items())
    now = 0.0
    lines = []
    for _ in range(n):
        now += dt
        cat = r.choices(cats, weights)[0]
        st = dict(bench_gate.DEFAULT_STATE)
        stale: list = []
        cmd = gen.make(cat, now, st, stale)
        if cmd is None:
            continue
        upd = []
        for k, v in st.items():
            ts = now - (10.0 if k in stale else 0.0)
            state.set(k, v, ts)
            upd.append([k, v, ts])
        dec = gate.check(cmd, now)
        evs = gate.tick(now)
        if dec.verdict in ("allow", "clamp") and cmd.tool not in devices[cmd.device].read_only:
            gen.accepted(cmd, now)
        lines.append({"cat": cat, "now": now, "state": upd, "cmd": cmd_json(cmd), "want": dec_json(dec),
                      "tick": ev_json(evs)})
    write_gz_lines(OUT / "gate_bench.jsonl.gz", lines)


STATE_VALUES = {
    "window_open": [False, True],
    "temp_c": [21.0, 29.999, 30.0, 30.0, 30.0, 30.0001, 45.0, -5.0, 29.99999999],
    "water_level_pct": [50.0, 10.0, 9.9999, 10.0001, 0.0, 100.0],
    "human_near": [False, True],
}
# Списки с повторами — грубые веса: чаще «обычные» значения, реже границы и выбросы.
AGES = [0.0] * 6 + [0.5, 1.0] * 3 + [4.999, 5.0, 5.00001, 5.0001, 7.5, 10.0, 60.0]
ISSUE_AGES = [0.0] * 6 + [0.1, 2.0] * 4 + [9.999, 10.0, 10.0001, 15.0, 29.9, 30.0, 30.5, -1.0]
TTLS = [None] * 6 + [2.0, 30.0, 0.0]
DTS = [0.0] * 4 + [0.1, 0.25, 0.5, 1.0] * 4 + [1.9, 2.0, 2.5, 5.0, 9.9, 10.0, 30.0, 61.0]


def random_args(r: random.Random, device: str, tool: str):
    """Значения аргументов, включая неверные типы, границы и лишние поля."""
    good = {
        ("heater", "set_power"): lambda: {"on": r.choice([True, False])},
        ("pump", "set_power"): lambda: {"on": r.choice([True, False])},
        ("valve", "set_angle"): lambda: {"deg": r.choice([0, 1, 45, 89, 90])},
        ("fan", "set_speed"): lambda: {"pct": r.choice([0, 10, 29, 30, 31, 50, 99, 100])},
        ("door", "lock"): lambda: {},
        ("door", "unlock"): lambda: {},
        ("led", "set_color"): lambda: {c: r.choice([0, 1, 128, 254, 255]) for c in "rgb"},
        ("thermo", "read"): lambda: {},
        ("camera", "capture"): lambda: r.choice([{}, {"w": 160}, {"w": 640}, {"w": 1920}]),
    }
    base = good.get((device, tool), lambda: {"on": True})()
    p = r.random()
    if p < 0.70:
        return base
    bad = [
        lambda: {**base, "extra": 1},
        lambda: {k: str(v) for k, v in base.items()},
        lambda: {k: (v + 0.5 if isinstance(v, int) and not isinstance(v, bool) else v) for k, v in base.items()},
        lambda: {k: (float(v) if isinstance(v, int) and not isinstance(v, bool) else v) for k, v in base.items()},
        lambda: {k: (v * 3 + 300 if isinstance(v, int) and not isinstance(v, bool) else v) for k, v in base.items()},
        lambda: {k: (-1 if isinstance(v, int) and not isinstance(v, bool) else v) for k, v in base.items()},
        lambda: {},
        lambda: {k: None for k in base},
        lambda: {k: (1 if isinstance(v, bool) else True) for k, v in base.items()},
        lambda: {"w": r.choice([159, 1921, 640.0, "640"])},
    ]
    return r.choice(bad)()


def gen_gate_fuzz(n_ops: int = 20_000, seed: int = 20261006) -> None:
    r = random.Random(seed)
    devices, interlocks = demo_setup()
    state = StateStore()
    gate = Gate(devices, interlocks, state)
    targets = [(d, t) for d, dev in devices.items() for t in dev.tools] * 4
    targets += [("heater", "set_power"), ("pump", "set_power"), ("fan", "set_speed"), ("valve", "set_angle")] * 6
    targets += [("heater", "set_temp"), ("oven", "set_power"), ("door", "open"), ("fan", "set_power")]
    now = 0.0
    n_cmd = 0
    issued: list[Command] = []   # для повторов ключа
    tokens: dict[str, tuple] = {}
    lines = []
    for _ in range(n_ops):
        now += r.choice(DTS)
        p = r.random()
        if p < 0.20:
            keys = list(STATE_VALUES) if r.random() < 0.6 else [r.choice(list(STATE_VALUES))]
            for k in keys:
                v = r.choice(STATE_VALUES[k])
                ts = now - r.choice(AGES)
                state.set(k, v, ts)
                lines.append({"op": "state", "key": k, "value": v, "ts": ts})
            continue
        if p < 0.22:
            evs = gate.tick(now)
            lines.append({"op": "tick", "now": now, "want": ev_json(evs)})
            continue
        if p < 0.27:
            dev = r.choice(["fan", "fan", "heater", "nosuch"])
            ok = gate.renew(dev, now)
            lines.append({"op": "renew", "device": dev, "now": now, "want": ok})
            continue
        if p < 0.33 and tokens:
            # чаще фиксируется последний план (в окне фиксации), иногда — старый
            tid = max(tokens, key=lambda t: int(t[1:])) if r.random() < 0.7 else r.choice(sorted(tokens))
            tok = tokens.pop(tid)
            dec = gate.commit(tok, now)
            lines.append({"op": "commit", "token": tid, "now": now, "want": dec_json(dec)})
            continue
        # команда (check или dry_run)
        if issued and r.random() < 0.12:
            cmd = r.choice(issued[-50:])
            if r.random() < 0.5:   # тот же ключ, другая команда — всё равно повтор
                cmd = Command(cmd.cmd_id, "led", "set_color", {"r": 1, "g": 1, "b": 1}, now)
        else:
            n_cmd += 1
            device, tool = r.choice(targets)
            cmd = Command(f"k{n_cmd}", device, tool, random_args(r, device, tool),
                          now - r.choice(ISSUE_AGES), ttl_s=r.choice(TTLS),
                          confirmed=r.random() < 0.3)
            issued.append(cmd)
        if r.random() < 0.15:
            dec, tok = gate.dry_run(cmd, now)
            tid = None
            if tok is not None:
                tid = f"t{len(lines)}"
                tokens[tid] = tok
            lines.append({"op": "dry_run", "now": now, "cmd": cmd_json(cmd), "token": tid, "want": dec_json(dec)})
        else:
            dec = gate.check(cmd, now)
            lines.append({"op": "check", "now": now, "cmd": cmd_json(cmd), "want": dec_json(dec)})
    lines.append({"op": "final", "now": now, "safe_count": gate.safe_count})
    write_gz_lines(OUT / "gate_fuzz.jsonl.gz", lines)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    gen_inventory()
    gen_iio_cases()
    gen_argschema()
    gen_gate_bench()
    gen_gate_fuzz()
    for f in sorted(OUT.iterdir()):
        print(f"{f.relative_to(REPO)}  {f.stat().st_size} B")


if __name__ == "__main__":
    main()
