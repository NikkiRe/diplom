"""(ВМ sim) Квота CPU по 7-zip: одно ядро ВМ против одного ядра Cortex-A57 Jetson Nano (model.json, раздел cpu).

  python3 -m neurobot.sim.budget.cpucal host --bin 7za [--cpu 5] [--repeats 3] [--out cpucal.json]
        `7za b -mmt1` на одном CPU ВМ (taskset) repeats раз; медиана Tot → отношение A57/ВМ и CPUQuota
  sudo python3 -m neurobot.sim.budget.cpucal check --bin 7za --quota PCT [--cpus 4] [--period-ms 20] [--out …]
        проверка квоты: `7za b -mmt1` и `-mmt<cpus>` в юните временного slice budget-cal.slice с теми же
        CPUQuota, CPUQuotaPeriodSec и AllowedCPUs, что у jetson.slice; ожидание — 7-zip Nano (sbc-bench)

Бинарник — p7zip 16.02 из Debian bullseye, та же сборка, что дала 1578 MIPS на Nano (README, «Подготовка»).
JSON — в stdout и в --out; ход — в stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
CAL_SLICE = "budget-cal.slice"


def parse_7z(text: str) -> Dict[str, object]:
    """Итог `7za b`: строка «Tot: usage R/U rating», версия и число потоков."""
    out: Dict[str, object] = {}
    for line in text.splitlines():
        parts = line.split()
        if line.startswith("7-Zip") and len(parts) > 3:
            out["version"] = parts[3]
        elif "Benchmark threads:" in line:
            out["threads"] = int(line.rsplit(":", 1)[1])
        elif parts[:1] == ["Tot:"] and len(parts) == 4:
            out.update(usage_pct=int(parts[1]), ru_mips=int(parts[2]), rating_mips=int(parts[3]))
    if "rating_mips" not in out:
        raise ValueError("в выводе 7za нет строки Tot:")
    return out


def bench(argv: List[str]) -> Dict[str, object]:
    t0 = time.monotonic()
    r = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    if r.returncode:
        raise RuntimeError("%s: код %d: %s" % (" ".join(argv), r.returncode, r.stderr.strip()))
    res = parse_7z(r.stdout)
    res["wall_s"] = round(time.monotonic() - t0, 1)
    return res


def summary(model: dict, host: List[int], cores: int) -> Dict[str, object]:
    nano = model["cpu"]["nano_7zip_single_mips"]
    med = statistics.median(host)
    ratio = nano / med
    return {"host_single_mips": host, "host_single_median": med, "nano_single_mips": nano,
            "ratio": round(ratio, 4), "cores": cores, "quota_pct": max(1, round(cores * 100 * ratio))}


def emit(res: dict, out: Optional[str]) -> None:
    text = json.dumps(res, ensure_ascii=False)
    print(text)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("host")
    h.add_argument("--bin", required=True, help="7za p7zip 16.02")
    h.add_argument("--cpu", type=int, default=5, help="CPU ВМ для taskset")
    h.add_argument("--repeats", type=int, default=3)
    h.add_argument("--cores", type=int, default=4, help="ядер платы")
    h.add_argument("--out")
    c = sub.add_parser("check")
    c.add_argument("--bin", required=True)
    c.add_argument("--quota", type=int, required=True, help="CPUQuota, %% одного ядра ВМ")
    c.add_argument("--cpus", type=int, default=4)
    c.add_argument("--period-ms", type=int, default=20)
    c.add_argument("--out")
    a = p.parse_args(argv)
    with open(os.path.join(HERE, "model.json"), encoding="utf-8") as f:
        model = json.load(f)
    if a.cmd == "host":
        if a.repeats < 1 or a.cpu < 0:
            p.error("--repeats ≥ 1, --cpu ≥ 0")
        runs = []
        for i in range(a.repeats):
            res = bench(["taskset", "-c", str(a.cpu), a.bin, "b", "-mmt1"])
            print("cpucal: прогон %d: %s MIPS" % (i + 1, res["rating_mips"]), file=sys.stderr)
            runs.append(res)
        out = summary(model, [r["rating_mips"] for r in runs], a.cores)
        out.update(version=runs[0].get("version"), runs=runs, load1=os.getloadavg()[0])
        emit(out, a.out)
        return 0
    if os.geteuid() != 0 or not os.environ.get("SUDO_USER"):
        p.error("check — от root через sudo")
    if not 5 <= a.quota <= 100 * a.cpus:
        p.error("--quota 5…100×cpus")
    props = ["CPUQuota=%d%%" % a.quota, "CPUQuotaPeriodSec=%dms" % a.period_ms, "AllowedCPUs=0-%d" % (a.cpus - 1)]
    subprocess.run(["systemctl", "set-property", "--runtime", CAL_SLICE] + props, check=True)
    try:
        out = {"quota_pct": a.quota, "cpus": a.cpus, "period_ms": a.period_ms,
               "nano_single_mips": model["cpu"]["nano_7zip_single_mips"],
               "nano_multi_mips": model["cpu"]["nano_7zip_multi_mips"]}
        for threads in (1, a.cpus):
            res = bench(["systemd-run", "--wait", "--pipe", "--quiet", "--slice", CAL_SLICE,
                         "--uid", os.environ["SUDO_USER"], "--", a.bin, "b", "-mmt%d" % threads])
            print("cpucal: в slice, потоков %d: %s MIPS" % (threads, res["rating_mips"]), file=sys.stderr)
            out["slice_mmt%d" % threads] = res
        out["single_vs_nano"] = round(out["slice_mmt1"]["rating_mips"] / out["nano_single_mips"], 2)
        out["multi_vs_nano"] = round(out["slice_mmt%d" % a.cpus]["rating_mips"] / out["nano_multi_mips"], 2)
        out["load1"] = os.getloadavg()[0]
        emit(out, a.out)
    finally:
        subprocess.run(["systemctl", "revert", CAL_SLICE], capture_output=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
