"""shapeclass — таблица «форма → класс» настоящей сетью лаборатории на выходе gesturegen (R7b §4.6, вход B).

Боксы каждого сегмента таймлайна — как их собрало бы демо: уверенность > 0,35 (model.conf), по порядку, в целых
пикселях (write_bbox). Из них два холста, и каждый votes раз идёт в SNN лаборатории (независимые кодирования
Бернулли одним батчем, зерно — seed + номер сегмента):
  demo — write_map_number(scale=0.1) и предобработка лаборатории: путь демо как есть (G1, «немая» сеть → 0);
  poly — линия через центры рамок (флаг --polyline perception v2) и та же предобработка: какой класс сеть
         даёт самой форме, когда холст не бледный.
Сегменты меньше чем с 6 боксами демо не распознаёт (MIN_FRAMES_TO_DETECT) — они считаются отдельно.
Класс сети, предобработка и кодирование импортируются из исходника лаборатории (их predict_live.py, без main()),
полилиния — из perception v2; веса — torch.load(weights_only=True) и строгая загрузка state_dict без
torch.compile (R4). Нужны torch, snntorch, cv2, rclpy и подставной pyrealsense2 (shim) — то есть ВМ sim.
Результат — ориентир: сеть та же, что на роботе, но боксы — синтетика gesturegen, а формы — гипотезы shapes.py.

  python -m neurobot.sim.gestures.shapeclass --gen DIR --out DIR [--votes 50] [--seed 0] [--weights PTH]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

from .gesturegen import TIMELINE_NAME, TRUTH_NAME
from .timeline import dumps, file_sha256, read_jsonl, rnd

ROOT = Path(__file__).resolve().parents[3]
LAB_SRC = ROOT / "third_party/neurochip/demo/demo-ros2_feature/src/camera/predict_live.py"
LAB_WEIGHTS = ROOT / "third_party/neurochip/demo/demo-ros2_feature/models/snn_emnist_small.pth"
V2_SRC = ROOT / "neurobot/perception/predict_live.py"
SHIM = Path(__file__).resolve().parent / "shim"
CONF_THR = 0.35          # predict_live.py:554
MIN_BOXES = 6            # predict_live.py:41 MIN_FRAMES_TO_DETECT
CANVASES = ("demo", "poly")
N_CLASSES = 4


def segment_boxes(records: list, conf_thr: float = CONF_THR) -> dict:
    """{сегмент: [(x1, y1, x2, y2)]} — боксы, которые демо положило бы в карту траектории."""
    out = {}
    for r in records:
        if r["bbox"] is not None and r["seg"] is not None and r["conf"] > conf_thr:
            out.setdefault(r["seg"], []).append(tuple(int(v) for v in r["bbox"]))
    return out


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class LabNet:
    """SNN лаборатории с их предобработкой; холсты demo (их write_map_number) и poly (perception v2)."""

    def __init__(self, weights: Path, lab_src: Path = LAB_SRC, v2_src: Path = V2_SRC):
        import torch
        for p in (str(SHIM), str(v2_src.parent)):
            if p not in sys.path:
                sys.path.insert(0, p)
        self.torch = torch
        self.lab = _load("lab_predict_live", lab_src)
        self.v2 = _load("v2_predict_live", v2_src)
        self.net = self.lab.SNN().to(self.lab.device).eval()
        state = torch.load(str(weights), map_location=self.lab.device, weights_only=True)
        self.net.load_state_dict(state["model_state_dict"], strict=True)

    def canvases(self, boxes: list):
        """({demo, poly} → PIL-холст, сторона по правилу демо: 0 — go_left, 1 — go_right)."""
        demo, direction = self.lab.write_map_number(boxes, scale=0.1)
        return {"demo": demo, "poly": self.v2.write_map_number(boxes, scale=0.1, polyline=True)}, direction

    def classify(self, image, votes: int, seed: int) -> dict:
        """Как recognize_gesture лаборатории (без окна): {hist — ответы по классам, mute — без выходных спайков,
        margin — средний отрыв первого класса от второго, доля шагов}."""
        torch = self.torch
        _, _, x = self.lab.preprocess_single_image(image, thresh_rel=0.2, inner=20)
        torch.manual_seed(seed)
        with torch.no_grad():
            out = self.net(self.lab.rate_encoding(x.to(self.lab.device).repeat(votes, 1, 1, 1), self.lab.num_steps))
        pred = torch.softmax(out.mean(dim=0), dim=1).argmax(dim=1)
        counts = out.sum(dim=0)
        top2 = counts.topk(2, dim=1).values
        return {"hist": [int((pred == c).sum()) for c in range(N_CLASSES)], "mute": int((counts.sum(dim=1) == 0).sum()),
                "margin": rnd(float(((top2[:, 0] - top2[:, 1]) / self.lab.num_steps).mean()), 4)}


def classify_segments(net, records: list, truth: list, votes: int, seed: int) -> list:
    boxes = segment_boxes(records)
    rows = []
    for s in truth:
        b = boxes.get(s["seg"], [])
        row = {k: s.get(k) for k in ("seg", "kind", "cls", "name", "shape", "side", "end", "dist_m", "cut")}
        row["boxes"] = len(b)
        if len(b) >= MIN_BOXES:
            images, direction = net.canvases(b)
            for kind in CANVASES:
                row[kind] = net.classify(images[kind], votes, seed + s["seg"])
            if s["side"]:
                row["side_ok"] = direction == (0 if s["side"] == "left" else 1)
        rows.append(row)
    return rows


def _shares(rows: list, kind: str, label) -> dict:
    hist = [sum(r[kind]["hist"][c] for r in rows) for c in range(N_CLASSES)]
    total = sum(hist)
    out = {"p": [rnd(h / total, 3) for h in hist], "mute": rnd(sum(r[kind]["mute"] for r in rows) / total, 3),
           "margin": rnd(sum(r[kind]["margin"] for r in rows) / len(rows), 3)}
    if label is not None and label >= 0:
        out["match_label"] = out["p"][label]
    return out


def aggregate(rows: list) -> dict:
    """По форме (жесты) и по виду (отрицательные сегменты): сегментов, меньше 6 боксов, доли ответов и «немых»
    по холстам demo и poly; сторона по правилу демо; итог по всем жестам."""
    groups = {}
    for r in rows:
        key = r["shape"] if r["kind"] == "gesture" else "neg:" + r["name"]
        groups.setdefault(key, []).append(r)
    out = {}
    for key, rs in sorted(groups.items()):
        seen = [r for r in rs if CANVASES[0] in r]
        g = {"cls": rs[0]["cls"], "segments": len(rs), "below_6": len(rs) - len(seen)}
        if seen:
            g.update({kind: _shares(seen, kind, rs[0]["cls"]) for kind in CANVASES})
        sides = [r["side_ok"] for r in seen if "side_ok" in r]
        if sides:
            g["side_ok"] = rnd(sum(sides) / len(sides), 3)
        out[key] = g
    gestures = [r for r in rows if r["kind"] == "gesture"]
    seen = [r for r in gestures if CANVASES[0] in r]
    total = {"gestures": len(gestures), "below_6": len(gestures) - len(seen)}
    if seen:
        for kind in CANVASES:
            votes = sum(sum(r[kind]["hist"]) for r in seen)
            total[kind] = {"mute": rnd(sum(r[kind]["mute"] for r in seen) / votes, 3),
                           "pred0": rnd(sum(r[kind]["hist"][0] for r in seen) / votes, 3),
                           "match_label": rnd(sum(r[kind]["hist"][r["cls"]] for r in seen) / votes, 3)}
    return {"groups": out, "gestures": total}


def format_table(table: dict) -> str:
    lines = ["%-14s %3s %4s %4s  %-26s %5s  %-26s %5s" % ("форма", "cls", "n", "<6", "demo p(0..3)", "немые",
                                                         "poly p(0..3)", "немые")]
    for key, g in table["groups"].items():
        cells = []
        for kind in CANVASES:
            k = g.get(kind)
            cells += [" ".join("%.2f" % p for p in k["p"]) if k else "—", "%.2f" % k["mute"] if k else "—"]
        lines.append("%-14s %3d %4d %4d  %-26s %5s  %-26s %5s" % (key, g["cls"], g["segments"], g["below_6"],
                                                                  *cells))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m neurobot.sim.gestures.shapeclass", description=__doc__.split("\n")[0])
    p.add_argument("--gen", required=True, help="каталог gesturegen (timeline.jsonl, truth.jsonl)")
    p.add_argument("--out", required=True, help="новый каталог результата: per_segment.jsonl, table.json")
    p.add_argument("--weights", default=str(LAB_WEIGHTS), help="веса SNN лаборатории (.pth, только тензоры)")
    p.add_argument("--votes", type=int, default=50, help="кодирований Бернулли на сегмент")
    p.add_argument("--seed", type=int, default=0, help="зерно кодирований: сегменту — seed + его номер")
    return p


def main(argv=None) -> int:
    p = build_parser()
    a = p.parse_args(argv)
    if a.votes < 1:
        p.error("--votes ≥ 1")
    os.makedirs(a.out)
    header, records = read_jsonl(os.path.join(a.gen, TIMELINE_NAME))
    _, truth = read_jsonl(os.path.join(a.gen, TRUTH_NAME))
    net = LabNet(Path(a.weights))
    rows = classify_segments(net, records, truth, a.votes, a.seed)
    table = dict(aggregate(rows), label="ориентир: сеть лаборатории, боксы gesturegen", votes=a.votes, seed=a.seed,
                 gen=dict(header["params"], f_px=header["f_px"]),
                 timeline_sha256=file_sha256(os.path.join(a.gen, TIMELINE_NAME)),
                 weights_sha256=file_sha256(a.weights), lab_src_sha256=file_sha256(str(LAB_SRC)),
                 v2_src_sha256=file_sha256(str(V2_SRC)), torch=net.torch.__version__, snntorch=net.lab.snn.__version__)
    with open(os.path.join(a.out, "per_segment.jsonl"), "w", encoding="utf-8") as fh:
        fh.writelines(dumps(r) + "\n" for r in rows)
    with open(os.path.join(a.out, "table.json"), "w", encoding="utf-8") as fh:
        json.dump(table, fh, ensure_ascii=False, indent=1, sort_keys=True)
    print(format_table(table))
    print(dumps(table["gestures"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
