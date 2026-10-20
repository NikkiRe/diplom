"""Общее расписание прогона A0/A1 (README §2): жесты gesturegen, отказы и запросы агенту — в секундах от якоря
таймлайна (t = 0 — первый вызов детектора, его ставит FakeYolo). Одно расписание ложится на A0 и A1 одинаково.

  python -m neurobot.sim.runs.schedule make --profile main|long|start --out DIR [--seed N]
  python -m neurobot.sim.runs.schedule info DIR

Пишет в DIR: timeline.jsonl и truth.jsonl (gesturegen), faults.json — отказы камеры в формате подставного
pyrealsense2 (A0 читает его сам, A1 — узел relay.py), plan.json — все события по порядку. Отказ ставится через 1 с
после последнего бокса сегмента, запрос агенту — через 2 с: успеет ли компонент подняться до следующего жеста, и
есть видимость перезапуска (PLAN §1). Отказы драйверов (DRIVER_CYCLE: kill -9 узлов urg_node2, realsense2_camera и
базы, SIGSTOP узлов лидара и камеры; приёмка 17.10) — на своих сегментах, между отказами и репликами: жесты и прежние
отказы профиля от них не меняются (тот же таймлайн, что у прогонов 16.10). Ошибки — код 2, сообщение в stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Sequence, Tuple

from neurobot.sim.gestures.gesturegen import GenConfig, summarize, write_outputs
from neurobot.sim.gestures.timeline import read_jsonl

FAULT_OFFSET_S = 1.0
AGENT_OFFSET_S = 2.0
# цикл отказов: камера (как у подставного pyrealsense2 — задержка дольше 5 с даёт RuntimeError демо, R1), kill -9 и
# SIGSTOP процессов по ролям (стенд сопоставляет роль своему процессу), агентский путь
FAULT_CYCLE: Tuple[dict, ...] = (
    {"kind": "camera", "fault": "delay", "duration_s": 7.0},
    {"kind": "kill", "target": "perception"},
    {"kind": "camera", "fault": "freeze", "duration_s": 6.0},
    {"kind": "kill", "target": "rosbridge"},
    {"kind": "camera", "fault": "black", "duration_s": 4.0},
    {"kind": "kill", "target": "mcp"},
    {"kind": "stop", "target": "perception", "duration_s": 8.0},
    {"kind": "kill", "target": "agent"},
    {"kind": "kill", "target": "executor"},
    {"kind": "kill", "target": "safety"},
)
# отказы драйверов: kill -9 узла лидара (urg_node2), камеры (realsense2_camera) и базы (на двойнике — сервер Gazebo с
# gz_ros2_control: controller_manager и diff_drive_controller; на роботе — ros2_control с мостом STM32), затем
# зависание узлов лидара и камеры; SIGSTOP 30 с — дольше любой паузы между жестами: восстановить вовремя может только
# сторож (свежесть темы), а не SIGCONT в конце
DRIVER_CYCLE: Tuple[dict, ...] = (
    {"kind": "kill", "target": "lidar"},
    {"kind": "kill", "target": "camera"},
    {"kind": "kill", "target": "base"},
    {"kind": "stop", "target": "lidar", "duration_s": 30.0},
    {"kind": "stop", "target": "camera", "duration_s": 30.0},
)
PROFILES: Dict[str, dict] = {
    # ≈33 мин: 80 жестов и 20 отрицательных, паузы 12–18 с (дольше манёвра демо 11 с)
    "main": {"gen": {"gestures": 80, "negatives": 20, "lead_in_s": 20.0},
             "fault_first": 4, "fault_every": 8, "agent_first": 7, "agent_every": 8, "moves": (1, 5, 9),
             "driver_first": 8, "driver_every": 8},
    # ≈60 мин: жесты 1–4 в минуту (паузы 10–55 с)
    "long": {"gen": {"gestures": 75, "negatives": 20, "lead_in_s": 20.0, "pause_s": (10.0, 55.0)},
             "fault_first": 3, "fault_every": 6, "agent_first": 5, "agent_every": 6, "moves": (2, 7, 12),
             "driver_first": 6, "driver_every": 6},
    # ≈30 мин, те же темп и цикл отказов: длительный прогон, когда ноутбук от батареи (README §8)
    "long30": {"gen": {"gestures": 37, "negatives": 10, "lead_in_s": 20.0, "pause_s": (10.0, 55.0)},
               "fault_first": 3, "fault_every": 4, "agent_first": 5, "agent_every": 4, "moves": (2, 5, 8),
               "driver_first": 4, "driver_every": 4},
    # дымовой (≈4 мин): проверка обвязки — все виды событий (отказы камеры и процесса, весь цикл драйверов, обе
    # реплики) на коротком таймлайне
    "smoke": {"gen": {"gestures": 10, "negatives": 2, "lead_in_s": 15.0},
              "fault_first": 1, "fault_every": 4, "agent_first": 3, "agent_every": 4, "moves": (1,),
              "driver_first": 2, "driver_every": 2},
    # отложенный набор (PROTOCOL §4): темп main, другие моменты событий — отказы на других сегментах и позже после
    # жеста (2,5 с, а не 1 с), драйверы и реплики тоже сдвинуты; зерно — своё (PROTOCOL), мир — hall_m. Только для
    # итогового прогона кода, на нём ничего не настраивают
    "holdout": {"gen": {"gestures": 80, "negatives": 20, "lead_in_s": 20.0},
                "fault_first": 5, "fault_every": 8, "agent_first": 3, "agent_every": 8, "moves": (1, 5, 9),
                "driver_first": 1, "driver_every": 8, "fault_offset_s": 2.5},
    # ≈8 ч без отказов (H14, PROTOCOL §5): старение — наклон памяти и fd без ступеней рестартов; темп жестов и реплик
    # как у long, «вперёд» — каждая пятая реплика
    "soak": {"gen": {"gestures": 640, "negatives": 160, "lead_in_s": 20.0, "pause_s": (10.0, 55.0)},
             "fault_first": 0, "fault_every": 0, "agent_first": 5, "agent_every": 6, "moves": tuple(range(2, 140, 5))},
    # start_test: каждые 7–10 с от якоря «крючок» слайда 12 — сеть лаборатории на полилинии отвечает классом 0
    # (shapeclass расписания main: 1,0), то есть «полный оборот» — поворот на месте: за 100 стартов робот не уезжает
    "start": {"gen": {"gestures": 24, "negatives": 0, "classes": (1,), "shapes": ("hook", "hook_m"),
                      "dur_s": (2.0, 3.0), "pause_s": (5.0, 7.0), "lead_in_s": 4.0},
              "fault_first": 0, "fault_every": 0, "agent_first": 0, "agent_every": 0, "moves": ()},
}


def place(truth: Sequence[dict], first: int, every: int, offset: float) -> List[Tuple[int, float]]:
    """(сегмент, время) событий: каждый every-й сегмент начиная с first, через offset после его последнего бокса.
    Последний сегмент не берётся — после него нечего проверять."""
    if every <= 0:
        return []
    return [(t["seg"], round(t["t_leave"] + offset, 3)) for t in truth[first:-1:every]]


def build_events(truth: Sequence[dict], prof: dict) -> List[dict]:
    """События плана по порядку времени: отказы по циклу FAULT_CYCLE, отказы драйверов по DRIVER_CYCLE (cycle
    driver) — через fault_offset_s профиля после жеста (по умолчанию FAULT_OFFSET_S), и запросы агенту (status, на
    номерах moves — move)."""
    events = []
    offset = prof.get("fault_offset_s", FAULT_OFFSET_S)
    for n, (seg, at) in enumerate(place(truth, prof["fault_first"], prof["fault_every"], offset)):
        events.append(dict(FAULT_CYCLE[n % len(FAULT_CYCLE)], at_s=at, seg=seg, n=n))
    for n, (seg, at) in enumerate(place(truth, prof.get("driver_first", 0), prof.get("driver_every", 0), offset)):
        events.append(dict(DRIVER_CYCLE[n % len(DRIVER_CYCLE)], at_s=at, seg=seg, n=n, cycle="driver"))
    for n, (seg, at) in enumerate(place(truth, prof["agent_first"], prof["agent_every"], AGENT_OFFSET_S)):
        events.append({"kind": "agent", "request": "move" if n in prof["moves"] else "status", "at_s": at,
                       "seg": seg, "n": n})
    return sorted(events, key=lambda e: e["at_s"])


def camera_faults(events: Sequence[dict]) -> List[dict]:
    """faults.json подставного pyrealsense2 (README компонента gestures, §5): {at_s, kind, duration_s}."""
    return [{"at_s": e["at_s"], "kind": e["fault"], "duration_s": e["duration_s"]}
            for e in events if e["kind"] == "camera"]


def make(profile: str, out_dir: str, seed: int) -> dict:
    if profile not in PROFILES:
        raise ValueError("profile: %s, получено %r" % (" | ".join(sorted(PROFILES)), profile))
    prof = PROFILES[profile]
    if os.path.exists(os.path.join(out_dir, "plan.json")):
        raise FileExistsError("уже есть %s/plan.json" % out_dir)
    cfg = GenConfig(seed=seed, **prof["gen"])
    _, truth_path = write_outputs(cfg, out_dir)
    header, truth = read_jsonl(truth_path)
    events = build_events(truth, prof)
    plan = {"profile": profile, "seed": seed, "duration_s": header["duration_s"],
            "timeline_sha256": header["timeline_sha256"], "events": events}
    with open(os.path.join(out_dir, "faults.json"), "w", encoding="utf-8") as f:
        json.dump(camera_faults(events), f, ensure_ascii=False, indent=1)
    with open(os.path.join(out_dir, "plan.json"), "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    return plan


def info(out_dir: str) -> dict:
    with open(os.path.join(out_dir, "plan.json"), encoding="utf-8") as f:
        plan = json.load(f)
    kinds: Dict[str, int] = {}
    for e in plan["events"]:
        key = "%s:%s" % (e["kind"], e.get("target") or e.get("fault") or e.get("request"))
        kinds[key] = kinds.get(key, 0) + 1
    return {"profile": plan["profile"], "seed": plan["seed"], "duration_s": plan["duration_s"],
            "events": kinds, "gestures": summarize(os.path.join(out_dir, "truth.jsonl"))}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m neurobot.sim.runs.schedule", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make", help="новое расписание в каталог")
    m.add_argument("--profile", required=True,
                   help="main (≈33 мин) | long (≈60 мин) | long30 (≈30 мин) | smoke (≈4 мин) | holdout (≈33 мин, "
                   "отложенный набор) | soak (≈8 ч без отказов) | start (для start_test)")
    m.add_argument("--out", required=True, help="каталог расписания (plan.json в нём не перезаписывается)")
    m.add_argument("--seed", type=int, default=20261016, help="зерно gesturegen")
    i = sub.add_parser("info", help="сводка расписания")
    i.add_argument("dir", help="каталог расписания")
    a = p.parse_args(argv)
    try:
        out = make(a.profile, a.out, a.seed) if a.cmd == "make" else info(a.dir)
    except (ValueError, OSError) as e:
        print("schedule: %s" % e, file=sys.stderr)
        return 2
    if a.cmd == "info":
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print("schedule: %s, %d событий, %.0f с" % (a.out, len(out["events"]), out["duration_s"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
