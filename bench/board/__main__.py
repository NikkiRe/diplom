"""python -m bench.board {verify,calibrate} — эмуляция «убогой» платы в ВМ Lima. См. README.md."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from . import calibrate, profiles, verify
from .vm import Vm, VmError


def _positive(kind):
    def parse(text: str):
        value = kind(text)
        if not 0 < value < math.inf:
            raise argparse.ArgumentTypeError("ожидается число > 0")
        return value
    return parse


def _quotas(text: str) -> list[int]:
    try:
        quotas = [int(q) for q in text.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("ожидается список процентов через запятую, например 100,50,25") from None
    if not quotas or min(quotas) < 1:
        raise argparse.ArgumentTypeError("проценты ≥ 1")
    return quotas


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m bench.board", description="Эмуляция «убогой» платы в ВМ Lima.")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("verify", help="пробы по осям в ВМ → verify.json и verify.md «задано vs измерено»")
    p.add_argument("--profiles", nargs="+", default=[], metavar="ОСЬ=УРОВНИ",
                   help="поверх умолчаний: " + " ".join(verify.DEFAULT_SPECS)
                   + "; ось=none — пропустить; board=<плата> — уровни пресета")
    p.add_argument("--out", type=Path, required=True, help="каталог отчёта (новый или пустой)")
    p.add_argument("--vm", default="sbc", help="ВМ Lima (по умолчанию sbc)")
    p = sub.add_parser("calibrate", help="ops/s cpubench в ВМ при нескольких CPUQuota")
    p.add_argument("--quotas", type=_quotas, default=list(calibrate.DEFAULT_QUOTAS),
                   help="квоты, %% одного ядра (по умолчанию 100,50,25,10,5)")
    p.add_argument("--cores", type=_positive(int), default=1, help="AllowedCPUs: число ядер (по умолчанию 1)")
    p.add_argument("--workers", type=_positive(int), default=1, help="горутин cpubench (по умолчанию 1)")
    p.add_argument("--seconds", type=_positive(float), default=5.0, help="длительность одного замера, с")
    p.add_argument("--repeats", type=_positive(int), default=3, help="замеров на квоту, по кругу (по умолчанию 3)")
    p.add_argument("--target", type=_positive(float), help="ops/s того же cpubench на настоящей плате")
    p.add_argument("--out", type=Path, help="каталог для calibrate.json и calibrate.md (новый или пустой)")
    p.add_argument("--vm", default="sbc", help="ВМ Lima (по умолчанию sbc)")
    return ap.parse_args(argv)


def _verify(a: argparse.Namespace) -> int:
    doc = profiles.load()
    code = verify.verify(Vm(a.vm), doc, verify.parse_selection(a.profiles, doc), a.out)
    print(f"board: отчёт {a.out / 'verify.md'}" + (" — есть расхождения" if code else ""), file=sys.stderr)
    return code


def _calibrate(a: argparse.Namespace) -> int:
    doc = profiles.load()
    least = math.ceil(100 * profiles.MIN_CFS_QUOTA_MS / doc["cpu"]["period_ms"])
    if min(a.quotas) < least or max(a.quotas) > 100 * a.cores:
        raise ValueError(f"квоты от {least} % (минимум ядра при периоде {doc['cpu']['period_ms']} мс) "
                      f"до {100 * a.cores} % ({a.cores} яд.)")
    if a.out and a.out.exists() and any(a.out.iterdir()):
        raise ValueError(f"{a.out} не пуст — отчёты не перезаписываются")
    report = calibrate.calibrate(Vm(a.vm), a.quotas, a.cores, a.workers, a.seconds, a.repeats)
    report |= {"target_ops_per_s": a.target,
               "quota_for_target": calibrate.quota_for(a.target, report["table"]) if a.target else None}
    text = calibrate.render(report, a.target)
    print(text, end="")
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "calibrate.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                                              encoding="utf-8")
        (a.out / "calibrate.md").write_text(text, encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    try:
        return {"verify": _verify, "calibrate": _calibrate}[a.command](a)
    except (VmError, OSError, ValueError) as e:   # ВМ и её вывод, файлы, аргументы (ProfileError, SelectionError)
        print(f"board: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:   # юнит пробы и netns уже сняты (Vm.run, finally в verify)
        print("board: прервано", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
