"""python -m bench.board calibrate: cpubench в ВМ под несколькими CPUQuota — опора для выбора квоты под плату.

Тот же статический бинарник cpubench запускают на настоящей плате (cpubench -duration 10s); по его ops/s и таблице
отсюда выбирается CPUQuota, при которой ВМ даёт столько же. До такого замера соответствие «квота ↔ плата» в пресетах
profiles.json — допущение. Скорость в ВМ при малой квоте нестабильна между прогонами (README, «Найденные ограничения
стенда»), поэтому каждая квота меряется несколько раз по кругу, а в таблице — медиана и разброс.
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import tempfile
from itertools import pairwise
from pathlib import Path

from .vm import Vm, VmError, environment, sha256

CPUBENCH = Path(__file__).with_name("cpubench")
DEFAULT_QUOTAS = (100, 50, 25, 10, 5)


def build(dest: Path) -> Path:
    """Статический cpubench для linux/arm64 (CGO_ENABLED=0)."""
    out = dest / "cpubench-linux-arm64"
    env = {**os.environ, "GOOS": "linux", "GOARCH": "arm64", "CGO_ENABLED": "0"}
    done = subprocess.run(["go", "build", "-trimpath", "-ldflags", "-s -w", "-o", str(out), "."], cwd=CPUBENCH,
                          env=env, capture_output=True, text=True)
    if done.returncode:
        raise VmError(f"сборка cpubench: {done.stderr.strip()}")
    return out


def parse_cpubench(stdout: str) -> dict:
    """Итог cpubench — последняя непустая строка stdout."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise VmError("cpubench ничего не напечатал")
    return json.loads(lines[-1])


def quota_for(target_ops: float, table: list[dict]) -> float | None:
    """Квота (% ядра), при которой ВМ даёт target_ops, — линейно между соседними по квоте медианами таблицы.
    Скорость на стенде при малой квоте нестабильна между прогонами (README, «Найденные ограничения стенда», п. 2),
    поэтому за пределы таблицы не экстраполируем: None."""
    points = sorted((r["quota"], r["ops_per_s"]) for r in table)
    for (q1, o1), (q2, o2) in pairwise(points):
        if min(o1, o2) <= target_ops <= max(o1, o2):
            return q1 if o1 == o2 else q1 + (target_ops - o1) * (q2 - q1) / (o2 - o1)
    return None


def summarize(quota: int, cores: int, workers: int, runs: list[dict]) -> dict:
    """Строка таблицы по замерам одной квоты: медиана, разброс, троттлинг."""
    ops = [r["ops_per_s"] for r in runs]
    median = statistics.median(ops)
    return {"quota": quota, "cores": cores, "workers": workers, "ops_per_s": median, "min": min(ops),
            "max": max(ops), "per_pct": median / quota, "runs": runs}


def calibrate(vm: Vm, quotas: list[int], cores: int, workers: int, seconds: float, repeats: int) -> dict:
    """Отчёт calibrate: окружение (в том числе версия Go и SHA-256 бинарника) и таблица по квотам. Квоты идут
    по кругу repeats раз: медленный дрейф нагрузки хоста ложится на все квоты, а не на одну."""
    remote = f"{vm.workdir()}/cpubench"
    with tempfile.TemporaryDirectory(prefix="board-cpubench-") as tmp:
        binary = build(Path(tmp))
        digest = sha256(binary)
        done = subprocess.run(["limactl", "copy", str(binary), f"{vm.name}:{remote}"], capture_output=True, text=True)
        if done.returncode:
            raise VmError(f"limactl copy: {done.stderr.strip()}")
    env = environment(vm, sorted(CPUBENCH.glob("*.go")))
    runs: dict[int, list[dict]] = {quota: [] for quota in quotas}
    for _ in range(repeats):
        for quota in quotas:
            res = vm.run(f"c{os.getpid() % 100000}-{quota}",
                         [remote, "-duration", f"{seconds}s", "-workers", str(workers)], seconds + 60,
                         mem=256, cpu=quota, cpus=cores)
            if res.code:
                raise VmError(f"cpubench при квоте {quota} %: код {res.code}, {res.record['reason']}")
            bench = parse_cpubench(res.stdout)
            runs[quota].append({"ops_per_s": bench["ops_per_s"], "gomaxprocs": bench["gomaxprocs"],
                                "nr_throttled": res.record["cpu"]["stat"].get("nr_throttled")})
    env["cpubench"] = {"sha256": digest, "go": bench["go"], "goarch": bench["goarch"],
                       "document_sha256": bench["sha256"]}
    return {"format": "board-calibrate/1", "env": env, "seconds": seconds, "repeats": repeats,
            "table": [summarize(quota, cores, workers, runs[quota]) for quota in quotas]}


def render(report: dict, target: float | None) -> str:
    env, vm, host = report["env"], report["env"]["vm"], report["env"]["host"]
    lines = [f"# cpubench в ВМ по квотам CPU ({env['utc']})", "",
             f"**Ориентир, не результат**: ВМ Lima `{vm['name']}` на Mac, общая с другими задачами. "
             f"Замеров на квоту: {report['repeats']} по {report['seconds']:g} с, квоты по кругу.", "",
             f"- ВМ: ядро {vm['kernel']}, loadavg до замеров {vm['loadavg']}, tainted {vm['tainted']}",
             f"- Mac: {host['cpu']}, loadavg {host['loadavg']}, питание: {host['power']}",
             f"- cpubench: {env['cpubench']['go']} {env['cpubench']['goarch']}, бинарник sha256 "
             f"{env['cpubench']['sha256'][:16]}…", "",
             "| квота, % ядра | ядра | горутины | ops/s, медиана | мин–макс | ops/s на 1 % |",
             "|---|---|---|---|---|---|"]
    lines += [f"| {r['quota']} | {r['cores']} | {r['workers']} | {r['ops_per_s']:.0f} | "
              f"{r['min']:.0f}–{r['max']:.0f} | {r['per_pct']:.1f} |" for r in report["table"]]
    if target:
        quota = quota_for(target, report["table"])
        lines += ["", f"Плата: {target:g} ops/s → " + (
            f"CPUQuota ≈ {quota:.1f} % (линейно между соседними квотами таблицы)"
            if quota is not None else "вне диапазона таблицы — добавьте квоты (--quotas)")]
    return "\n".join(lines) + "\n"
