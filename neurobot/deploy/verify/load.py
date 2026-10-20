#!/usr/bin/env python3
"""CPU юнитов и трафик тем за окно — замеры «до/после» правок конфига (сторож камеры по camera_info, madgwick без TF).

    sudo python3 load.py --out FILE.json [--seconds 60] [--label ТЕКСТ] [--unit ЮНИТ …] [--topic /тема …]

Юнит: прирост usage_usec из cpu.stat его cgroup (cgroup v2) за окно — доля одного ядра, и memory.current в конце;
юнит, перезапущенный за окно, помечается (счётчик новой cgroup начался с нуля). Тема: подписка raw (без разбора, QoS
сенсоров) — сообщений в секунду и МБ/с сериализованных данных, как их доставил DDS этому процессу; тип — из графа.
DDS и домен — из env-файла пакета (NEUROBOT_ENV), как у CLI neurobot. Итог — FILE.json (существующий не
перезаписывается). Числа стенда — ориентир, не робот.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Dict, List, Optional

import stand
from neurobot_deploy.config import load
from neurobot_deploy.rosgraph import msg_class

log = logging.getLogger("load")


def cpu_usec(unit: str) -> Optional[int]:
    """usage_usec из cpu.stat cgroup юнита; None — юнита нет или cgroup не v2."""
    cgroup = stand.unit_props(unit, "ControlGroup").get("ControlGroup", "")
    try:
        with open("/sys/fs/cgroup{}/cpu.stat".format(cgroup)) as f:
            return next(int(v) for k, v in (ln.split() for ln in f) if k == "usage_usec")
    except (OSError, StopIteration, ValueError):
        return None


def mem_bytes(unit: str) -> Optional[int]:
    cgroup = stand.unit_props(unit, "ControlGroup").get("ControlGroup", "")
    try:
        with open("/sys/fs/cgroup{}/memory.current".format(cgroup)) as f:
            return int(f.read())
    except (OSError, ValueError):
        return None


def unit_row(before: Optional[int], after: Optional[int], inv0: str, inv1: str, seconds: float,
             mem: Optional[int]) -> dict:
    """Строка юнита: доля ядра за окно (None — нет данных или юнит перезапускался) и память в конце, МиБ."""
    row = {"restarted": inv0 != inv1, "mem_mib": round(mem / 2 ** 20, 1) if mem is not None else None}
    ok = before is not None and after is not None and not row["restarted"] and after >= before
    row["cpu_core_pct"] = round(100.0 * (after - before) / 1e6 / seconds, 2) if ok else None
    return row


def topic_row(count: int, nbytes: int, seconds: float) -> dict:
    return {"msgs": count, "rate_hz": round(count / seconds, 2), "mb_s": round(nbytes / 1e6 / seconds, 4),
            "bytes_per_msg": round(nbytes / count) if count else None}


def measure(cfg, units: List[str], topics: List[str], seconds: float) -> dict:
    rec = stand.Rec(cfg, "load")
    stats: Dict[str, List[int]] = {t: [0, 0] for t in topics}
    try:
        types = {}
        end = time.monotonic() + 10.0
        while time.monotonic() < end and len(types) < len(topics):  # граф DDS собирается со временем
            types = {t: ts[0] for t, ts in rec.node.get_topic_names_and_types() if t in stats and ts}
            time.sleep(0.2)
        for t, typ in types.items():
            def count(m, s=stats[t]):
                s[0] += 1
                s[1] += len(m)
            rec.node.create_subscription(msg_class(typ), t, count, rec.qos.qos_profile_sensor_data, raw=True)
        rec.start()
        time.sleep(2.0)  # подписки установились: окно — без разгона обнаружения
        for s in stats.values():
            s[0] = s[1] = 0
        inv0 = {u: stand.unit_props(u, "InvocationID").get("InvocationID", "") for u in units}
        cpu0 = {u: cpu_usec(u) for u in units}
        t0 = time.monotonic()
        time.sleep(seconds)
        dt = time.monotonic() - t0
        cpu1 = {u: cpu_usec(u) for u in units}
        snap = {t: list(s) for t, s in stats.items()}
        inv1 = {u: stand.unit_props(u, "InvocationID").get("InvocationID", "") for u in units}
    finally:
        rec.close()
    return {"seconds": round(dt, 2), "load1": os.getloadavg()[0],
            "units": {u: unit_row(cpu0[u], cpu1[u], inv0[u], inv1[u], dt, mem_bytes(u)) for u in units},
            "topics": {t: dict(topic_row(snap[t][0], snap[t][1], dt), type=types.get(t)) for t in topics}}


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", stream=sys.stderr)
    p = argparse.ArgumentParser(description="CPU юнитов (cgroup) и трафик тем (raw) за окно")
    p.add_argument("--out", required=True, help="итог JSON (существующий не перезаписывается)")
    p.add_argument("--seconds", type=float, default=60.0, help="окно замера, с (60)")
    p.add_argument("--label", default="", help="подпись замера (условие «до/после»)")
    p.add_argument("--unit", action="append", default=[], help="юнит systemd (повторяется)")
    p.add_argument("--topic", action="append", default=[], help="тема ROS 2 (повторяется)")
    a = p.parse_args(argv)
    try:
        open(a.out, "x").close()
    except FileExistsError:
        print("load: {} уже есть — не перезаписываю".format(a.out), file=sys.stderr)
        return 2
    res = measure(load(), a.unit, a.topic, a.seconds)
    doc = dict({"tool": "neurobot/deploy/verify/load.py", "label": a.label, "t_start": time.time(),
                "note": "стенд: ориентир, не результат робота"}, **res)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    log.info("%s", json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
