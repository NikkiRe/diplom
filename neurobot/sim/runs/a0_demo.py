"""Демо лаборатории как есть для A0 (README §2.1): неизменённый `predict_live.py` выполняется как `__main__` в этом же
процессе, а снаружи подменены три вещи — это отклонения A0 (manifest.deviations):
- `torch.hub.load` → FakeYolo (`neurobot.sim.gestures.fakeyolo.hub_load`): `yolo_hand.pt` не исполняется, бокс руки
  даёт таймлайн gesturegen;
- `torch.compile` скрыт: на роботе torch 1.13, где его нет, и `hasattr(torch, "compile")` у демо ложно (R4);
- `torch.load` всегда с `weights_only=True`: в ВМ веса SNN лаборатории загружаются только так.
Кадры даёт подставной pyrealsense2 (PYTHONPATH с `neurobot/sim/gestures/shim`), окно OpenCV — Xvfb. Пути
`/home/jetson/…` демо — через ссылку в ВМ.

  python3 a0_demo.py --demo .../src/camera/predict_live.py
"""
from __future__ import annotations

import os
import sys

# Запуск файлом ставит каталог скрипта (neurobot/sim/runs) первым в sys.path, а там queue.py очереди прогонов: он
# заслонил бы стандартный queue, который импортирует torch (torch.export → torch.fx), и демо падало бы на импорте.
# Свои модули демо берёт по PYTHONPATH (снимок), поэтому каталог скрипта убирается до остальных импортов.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != _HERE]

import argparse  # noqa: E402
import functools  # noqa: E402
import runpy  # noqa: E402

from neurobot.sim.gestures.fakeyolo import hub_load  # noqa: E402


def patch_torch(module) -> None:
    load = module.load

    @functools.wraps(load)
    def weights_only_load(*args, **kwargs):
        kwargs["weights_only"] = True
        return load(*args, **kwargs)

    module.hub.load = hub_load
    module.load = weights_only_load
    if hasattr(module, "compile"):
        delattr(module, "compile")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="демо лаборатории как есть (A0) с подставными модулями стенда")
    p.add_argument("--demo", required=True, help="путь к predict_live.py лаборатории")
    a = p.parse_args(argv)
    import torch
    patch_torch(torch)
    print("a0_demo: torch.hub.load → FakeYolo, torch.compile скрыт, torch.load(weights_only=True): %s" % a.demo,
          file=sys.stderr, flush=True)
    sys.argv = [a.demo]
    runpy.run_path(a.demo, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
