"""Модель бюджета Jetson Nano 4 ГБ (model.json рядом): сколько памяти остаётся стеку робота и какая квота CPU
приближает четыре ядра Cortex-A57 в ВМ sim.

  python3 -m neurobot.sim.budget.model [--model model.json] [--host-mips N] [--json]

Память стека (MemoryMax cgroup jetson.slice) = видимая память − ОС ветки − резерв; ветки — headless, lxde, gnome.
Случаи: value — средние значения; pessimistic — меньше видимой памяти и больше ОС; optimistic — наоборот.
Квота CPU = ядра × 100 % × (7-zip одного ядра A57 / 7-zip одного ядра ВМ); --host-mips — замер cpucal.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(HERE, "model.json")
TAGS = {"ист.", "оценка", "допущение", "замер"}
BRANCHES = ("headless", "lxde", "gnome")
CASES = ("value", "pessimistic", "optimistic")


def load(path: str = MODEL) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate(m: dict) -> List[str]:
    """Ошибки структуры модели: обязательные поля, пометки, ссылки на источники, порядок границ."""
    errs = []
    srcs = set(m.get("sources", {}))

    def item(path: str, it: dict, bounds: bool) -> None:
        if it.get("tag") not in TAGS:
            errs.append("%s: пометка %r не из %s" % (path, it.get("tag"), sorted(TAGS)))
        for s in it.get("src", []):
            if s not in srcs:
                errs.append("%s: нет источника %r" % (path, s))
        if bounds and not it.get("low", 0) <= it.get("value", 0) <= it.get("high", 0):
            errs.append("%s: нужно low ≤ value ≤ high" % path)

    mem = m.get("memory_mib", {})
    for key in ("visible", "reserve"):
        if key not in mem:
            errs.append("memory_mib.%s: нет" % key)
    if "visible" in mem:
        item("memory_mib.visible", mem["visible"], True)
    if "reserve" in mem:
        item("memory_mib.reserve", mem["reserve"], False)
    for b in BRANCHES:
        if b not in m.get("os_branches", {}):
            errs.append("os_branches.%s: нет" % b)
        else:
            item("os_branches." + b, m["os_branches"][b], True)
    for name, it in m.get("ballast", {}).items():
        item("ballast." + name, it, False)
        if it.get("mib", 0) <= 0:
            errs.append("ballast.%s: mib > 0" % name)
    for key in ("zram", "cpu"):
        if key not in m:
            errs.append("%s: нет" % key)
        else:
            item(key, m[key], False)
    return errs


def stack_budget(m: dict, branch: str, case: str = "value") -> int:
    """MemoryMax стека, МиБ, для ветки ОС и случая."""
    vis, os_ = m["memory_mib"]["visible"], m["os_branches"][branch]
    reserve = m["memory_mib"]["reserve"]["value"]
    if case == "value":
        return vis["value"] - os_["value"] - reserve
    if case == "pessimistic":
        return vis["low"] - os_["high"] - reserve
    if case == "optimistic":
        return vis["high"] - os_["low"] - reserve
    raise ValueError("случай: %s" % "|".join(CASES))


def budgets(m: dict) -> Dict[str, Dict[str, int]]:
    return {b: {c: stack_budget(m, b, c) for c in CASES} for b in BRANCHES}


def cpu_ratio(m: dict, host_mips: float) -> float:
    """Во сколько раз ядро A57 медленнее ядра ВМ по 7-zip одного потока."""
    if host_mips <= 0:
        raise ValueError("host_mips > 0")
    return m["cpu"]["nano_7zip_single_mips"] / host_mips


def cpu_quota_pct(m: dict, host_mips: float, cores: Optional[int] = None) -> int:
    """CPUQuota slice в процентах одного ядра ВМ."""
    cores = m["cpu"]["cores"] if cores is None else cores
    return max(1, round(cores * 100 * cpu_ratio(m, host_mips)))


def zram_credit(m: dict, cold_anon_mib: float, ratio: float) -> float:
    """Сколько RAM освободит zram, сжав холодные анонимные страницы: их объём (не больше размера zram) × (1 − 1/k)."""
    if ratio < 1:
        raise ValueError("коэффициент сжатия ≥ 1")
    return min(max(cold_anon_mib, 0.0), m["zram"]["size_mib"]) * (1 - 1 / ratio)


def ballast_mib(m: dict) -> Dict[str, int]:
    return {k: v["mib"] for k, v in m["ballast"].items()}


def table(m: dict, host_mips: Optional[float]) -> str:
    rows = ["ветка ОС   | value | pessimistic | optimistic   (MemoryMax стека, МиБ)"]
    for b, cs in budgets(m).items():
        rows.append("%-10s | %5d | %11d | %10d" % (b, cs["value"], cs["pessimistic"], cs["optimistic"]))
    rows.append("балласт, МиБ: " + ", ".join("%s %d" % kv for kv in ballast_mib(m).items()))
    z = m["zram"]
    rows.append("zram: %d МиБ, %s; кредит при 500 МиБ холодных страниц и k = %s: %s МиБ" % (
        z["size_mib"], z["compressor"], "–".join(str(k) for k in z["ratio"]),
        "–".join("%.0f" % zram_credit(m, 500, k) for k in z["ratio"])))
    if host_mips:
        rows.append("CPU: A57/ВМ = %.3f, CPUQuota = %d %% на %d ядрах" % (
            cpu_ratio(m, host_mips), cpu_quota_pct(m, host_mips), m["cpu"]["cores"]))
    return "\n".join(rows)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--model", default=MODEL)
    p.add_argument("--host-mips", type=float, help="7-zip одного ядра ВМ (cpucal)")
    p.add_argument("--json", action="store_true", help="JSON вместо таблицы")
    a = p.parse_args(argv)
    m = load(a.model)
    errs = validate(m)
    if errs:
        for e in errs:
            print("model: " + e, file=sys.stderr)
        return 2
    if a.host_mips is not None and a.host_mips <= 0:
        print("model: --host-mips > 0", file=sys.stderr)
        return 2
    if a.json:
        out = {"budgets_mib": budgets(m), "ballast_mib": ballast_mib(m)}
        if a.host_mips:
            out["cpu"] = {"ratio": cpu_ratio(m, a.host_mips), "quota_pct": cpu_quota_pct(m, a.host_mips)}
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(table(m, a.host_mips))
    return 0


if __name__ == "__main__":
    sys.exit(main())
