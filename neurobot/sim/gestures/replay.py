"""replay — логика конца жеста perception v2 офлайн: таймлайн gesturegen → кадры → gesture_logic → SNN лаборатории
(калибровка и проверка H10 без Gazebo).

Кадры идут с темпом камеры двойника (fps, 15 к/с) с фазой от номера прогона; рамка кадра — запись таймлайна в момент
кадра, а детектор, как FakeYolo, отдаёт её при уверенности выше своего порога (min(conf_start, conf_keep) —
`model.conf` perception). Эпизод и решение — тот же код, что в perception (gesture_logic.EpisodeSegmenter и
Recognizer); SNN и холст — функции predict_live v2 (--polyline, 5 голосований, зерно 0) или подставной классификатор
тестов. После жеста с действием детекция стоит, пока длится манёвр демо (L6, MANEUVERS разбора), — как у perception
со своим манёвром. Сообщения относятся к сегментам истины, как в analyze: последний начавшийся сегмент, если от его t0
прошло не больше match_window_s; иначе действие самопроизвольное.

Метрики по жестам (сопоставимы с analyze, но без цепочки исполнитель → safety_gate → /cmd_vel и без защиты):
  k1      — t_rec − t0 первого действия сегмента: K1 до /neurobot/gesture (на двойнике до /cmd_vel ещё ≈0,03–0,1 с);
  early   — действие раньше t0 (ранняя реакция: SNN распознала префикс);
  correct — решающее сообщение просит ожидаемый манёвр (таблица рефлекса); unknown — «не распознано»; none — сообщений
            нет; wrong — другой манёвр; split — у жеста больше одного сообщения (жест разрезан);
  k7      — нежелательные действия: на отрицательных сегментах (false_actions) и вне окон (spontaneous); extra — второе
            и дальше действие одного сегмента (на двойнике его глушит пауза L6, здесь тоже).
Числа — ориентир стенда: боксы — синтетика gesturegen, формы — гипотезы; задержки SNN и кадра на Jetson не учтены.

  python -m neurobot.sim.gestures.replay --gen DIR [DIR ...] --variants variants.json --out DIR [--fps 15]
variants.json — {"имя": {параметры ReplayConfig}}; в DIR — summary.json (по вариантам и прогонам) и per_gesture.jsonl.
SNN — в ВМ sim (torch, snntorch, cv2, rclpy и подставной pyrealsense2, как у shapeclass).
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .analyze import MANEUVERS, REFLEX, expected_maneuver, requested_maneuver
from .gesturegen import TIMELINE_NAME, TRUTH_NAME
from .timeline import Timeline, dumps, file_sha256, read_jsonl, rnd

ROOT = Path(__file__).resolve().parents[3]
PERCEPTION = ROOT / "neurobot" / "perception"
SHIM = Path(__file__).resolve().parent / "shim"
LAB_WEIGHTS = ROOT / "third_party/neurochip/demo/demo-ros2_feature/models/snn_emnist_small.pth"
FRAME = (640, 480)

if str(PERCEPTION) not in sys.path:  # gesture_logic — модуль perception v2 без пакета (кладётся в src/camera/)
    sys.path.insert(0, str(PERCEPTION))
from gesture_logic import EpisodeSegmenter, Recognizer, program_for, side_of  # noqa: E402


@dataclass
class ReplayConfig:
    """Параметры perception v2 (флаги predict_live) и стенда. Умолчания — perception до H10."""
    fps: float = 15.0                 # темп камеры двойника (color_hz), к/с
    end_gap: float = 0.5
    window: float = 8.0
    min_boxes: int = 3
    min_path: float = 0.1
    deadband: float = 0.02
    edge_gap: Optional[float] = None  # None — как end_gap (выкл.)
    edge_frac: float = 0.02
    edges: tuple = ("top",)
    conf_start: float = 0.35
    conf_keep: Optional[float] = None  # None — как conf_start (выкл.)
    conf_gate: float = 1.0
    min_margin: float = 0.04
    early_margin: float = 0.0
    early_gap: float = 0.25
    max_cut: float = 0.0
    steps: int = 125                  # шагов SNN: 25 × 5 голосований
    match_window_s: float = 10.0      # как у analyze
    pause_margin_s: float = 0.3       # пауза L6 дольше манёвра: исполнитель сообщает «стоит» не сразу [оценка]


def recognizer(cfg: ReplayConfig, classify) -> Recognizer:
    seg = EpisodeSegmenter(FRAME[0], end_gap=cfg.end_gap, window=cfg.window, min_boxes=cfg.min_boxes,
                           min_path=cfg.min_path, deadband=cfg.deadband, frame_height=FRAME[1], edge_gap=cfg.edge_gap,
                           edge_frac=cfg.edge_frac, edges=cfg.edges, start_conf=cfg.conf_start,
                           keep_conf=cfg.conf_keep, gate=cfg.conf_gate)
    return Recognizer(seg, classify, cfg.steps, cfg.min_margin, early_margin=cfg.early_margin,
                      early_gap=cfg.early_gap, max_cut=cfg.max_cut)


def run_timeline(timeline: Timeline, cfg: ReplayConfig, classify: Callable, phase: float = 0.0) -> List[dict]:
    """Сообщения /neurobot/gesture, которые perception v2 выдал бы на этом таймлайне: {t, cls, name, side, margin,
    end, cut, n_boxes, program}."""
    rec = recognizer(cfg, classify)
    det_conf = min(cfg.conf_start, cfg.conf_start if cfg.conf_keep is None else cfg.conf_keep)
    out, paused_until = [], -math.inf
    n = int(math.ceil((timeline.end + cfg.window) * cfg.fps))
    for k in range(n):
        t = (k + phase) / cfg.fps
        if t < paused_until:
            rec.reset()  # L6: свой манёвр идёт — YOLO стоит, рамки не копятся
            continue
        hit = timeline.box_at(t)
        box, conf = (hit[:4], hit[4]) if hit is not None and hit[4] > det_conf else (None, 0.0)
        res = rec.step(t, box, conf)
        if res is None or res.decision is None:
            continue
        d = res.decision
        side = side_of(res.episode.boxes)
        program = program_for(d.cls, side)
        out.append({"t": rnd(t, 4), "cls": d.cls, "name": program or "unknown", "side": side,
                    "margin": rnd(d.margin, 4), "end": res.end, "cut": rnd(res.episode.cut, 3),
                    "n_boxes": len(res.episode.boxes), "program": program})
        if program is not None:
            paused_until = t + sum(s for _, s in MANEUVERS[program]) + cfg.pause_margin_s
    return out


def judge(truth: Sequence[dict], msgs: Sequence[dict], match_window_s: float = 10.0) -> List[dict]:
    """Строки по сегментам истины: сообщения сегмента, первое действие, решающее сообщение и исход (как analyze)."""
    starts = [s["t_start"] for s in truth]
    by_seg: Dict[Optional[int], list] = {}
    for m in msgs:
        k = bisect.bisect_right(starts, m["t"]) - 1
        k = k if k >= 0 and m["t"] - truth[k]["t0"] <= match_window_s else None
        by_seg.setdefault(k, []).append(m)
    rows = []
    for k, s in enumerate(truth):
        mine = by_seg.get(k, [])
        acts = [m for m in mine if m["program"] is not None]
        decisive = acts[0] if acts else mine[-1] if mine else None
        row = {"seg": s["seg"], "kind": s["kind"], "cls": s["cls"], "name": s["name"], "shape": s.get("shape"),
               "side": s.get("side"), "cut": s.get("cut"), "cut_top": s.get("cut_top"), "msgs": len(mine),
               "actions": len(acts)}
        if decisive is not None:
            row["rec"] = {key: decisive[key] for key in ("cls", "side", "margin", "end", "cut", "n_boxes")}
        if acts:
            row["k1"] = rnd(acts[0]["t"] - s["t0"], 4)
            row["early"] = acts[0]["t"] < s["t0"]
        if s["kind"] == "gesture":
            expected = expected_maneuver(REFLEX, s)
            asked = requested_maneuver(REFLEX, decisive) if decisive is not None else None
            row["expected"] = expected
            row["outcome"] = ("none" if decisive is None else "correct" if asked == expected
                              else "unknown" if decisive["cls"] < 0 else "wrong")
        rows.append(row)
    rows.append({"spontaneous": sum(1 for m in by_seg.get(None, []) if m["program"] is not None)})
    return rows


def _quantiles(xs: list) -> Optional[dict]:
    if not xs:
        return None
    xs = sorted(xs)

    def q(p):  # ближайший ранг, как harness/stats для задержек стенда
        return xs[min(len(xs) - 1, max(0, int(math.ceil(p * len(xs))) - 1))]
    return {"n": len(xs), "p50": rnd(q(0.5), 3), "p95": rnd(q(0.95), 3), "max": rnd(xs[-1], 3)}


def summarize(rows: List[dict]) -> dict:
    """Сводка по строкам judge одного или нескольких прогонов."""
    segs = [r for r in rows if "seg" in r]
    g = [r for r in segs if r["kind"] == "gesture"]
    outcomes = {o: sum(1 for r in g if r["outcome"] == o) for o in ("correct", "wrong", "unknown", "none")}
    k1 = [r["k1"] for r in g if "k1" in r]
    false_actions = sum(1 for r in segs if r["kind"] == "negative" and r["actions"])
    spontaneous = sum(r["spontaneous"] for r in rows if "spontaneous" in r)
    return {"gestures": len(g), "negatives": len(segs) - len(g), "outcomes": outcomes,
            "early": sum(1 for r in g if r.get("early")), "split": sum(1 for r in g if r["msgs"] > 1),
            "extra_actions": sum(max(0, r["actions"] - 1) for r in segs),
            "k1_s": _quantiles(k1), "k1_not_early_s": _quantiles([x for x in k1 if x >= 0]),
            "k7": {"false_actions": false_actions, "spontaneous": spontaneous, "total": false_actions + spontaneous},
            "ends": {e: sum(1 for r in g if r.get("rec", {}).get("end") == e)
                     for e in ("gap", "edge", "early", "cut", "still")}}


class LabSnn:
    """classify(boxes) → суммы выходных спайков, как predict_live v2.classify: холст polyline, зерно 0, 5 голосований.
    Кэш по координатам рамок: варианты с одинаковым эпизодом SNN не пересчитывают."""

    def __init__(self, weights: Path = LAB_WEIGHTS, polyline: bool = True, votes: int = 5, seed: int = 0):
        for p in (str(SHIM), str(PERCEPTION)):
            if p not in sys.path:
                sys.path.insert(0, p)
        import importlib.util
        spec = importlib.util.spec_from_file_location("v2_predict_live", str(PERCEPTION / "predict_live.py"))
        self.v2 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.v2)
        self.net = self.v2.load_snn(str(weights), False)
        self.gen = self.v2.snn_generator(seed)
        self.polyline, self.votes, self.seed = polyline, votes, seed
        self.calls = 0
        self._cache: Dict[tuple, list] = {}

    def __call__(self, boxes) -> list:
        coords = tuple((b.x1, b.y1, b.x2, b.y2) for b in boxes)
        if coords not in self._cache:
            img = self.v2.write_map_number(list(coords), orig_size=FRAME, scale=0.1, polyline=self.polyline)
            if self.gen is not None:
                self.gen.manual_seed(self.seed)
            self._cache[coords] = self.v2.recognize_gesture(img, self.net, votes=self.votes, generator=self.gen)
            self.calls += 1
        return self._cache[coords]


def config_of(params: dict, base: ReplayConfig) -> ReplayConfig:
    names = {f.name for f in fields(ReplayConfig)}
    unknown = set(params) - names
    if unknown:
        raise ValueError("неизвестные параметры: %s" % ", ".join(sorted(unknown)))
    cfg = ReplayConfig(**dict(asdict(base), **params))
    cfg.edges = tuple(cfg.edges)
    return cfg


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m neurobot.sim.gestures.replay", description=__doc__.split("\n")[0])
    p.add_argument("--gen", nargs="+", required=True, help="каталоги gesturegen (timeline.jsonl, truth.jsonl)")
    p.add_argument("--variants", required=True, help="JSON {имя: {параметры ReplayConfig}}")
    p.add_argument("--out", required=True, help="новый каталог результата")
    p.add_argument("--fps", type=float, default=ReplayConfig.fps, help="темп камеры, к/с")
    p.add_argument("--weights", default=str(LAB_WEIGHTS), help="веса SNN лаборатории")
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    with open(a.variants, encoding="utf-8") as fh:
        variants = json.load(fh)
    base = ReplayConfig(fps=a.fps)
    configs = {name: config_of(v, base) for name, v in variants.items()}
    os.makedirs(a.out)
    snn = LabSnn(Path(a.weights))
    runs = [(d, Timeline.load(os.path.join(d, TIMELINE_NAME)), read_jsonl(os.path.join(d, TRUTH_NAME))[1])
            for d in a.gen]
    summary = {"label": "ориентир стенда: боксы gesturegen, сеть лаборатории; без цепочки до /cmd_vel",
               "gen": {d: file_sha256(os.path.join(d, TIMELINE_NAME)) for d in a.gen},
               "weights_sha256": file_sha256(a.weights), "variants": {}}
    with open(os.path.join(a.out, "per_gesture.jsonl"), "w", encoding="utf-8") as fh:
        for name, cfg in configs.items():
            rows_all, per_run = [], {}
            for i, (d, tl, truth) in enumerate(runs):
                msgs = run_timeline(tl, cfg, snn, phase=(0.37 * (i + 1)) % 1.0)
                rows = judge(truth, msgs, cfg.match_window_s)
                per_run[os.path.basename(d.rstrip("/"))] = summarize(rows)
                rows_all += rows
                fh.writelines(dumps(dict(r, variant=name, gen=os.path.basename(d.rstrip("/")))) + "\n"
                              for r in rows)
            summary["variants"][name] = {"config": asdict(cfg), "all": summarize(rows_all), "runs": per_run}
            s = summary["variants"][name]["all"]
            print("%-22s correct %3d unknown %3d wrong %3d none %3d early %3d split %3d k7 %2d k1 %s" % (
                name, s["outcomes"]["correct"], s["outcomes"]["unknown"], s["outcomes"]["wrong"],
                s["outcomes"]["none"], s["early"], s["split"], s["k7"]["total"], s["k1_s"]), flush=True)
    summary["snn_calls"] = snn.calls
    with open(os.path.join(a.out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
