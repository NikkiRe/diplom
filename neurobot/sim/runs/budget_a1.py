"""(ВМ sim, root, через sudo) A1 целиком в cgroup «jetson» по методике neurobot/sim/budget (README §7): прогон
runner.py по расписанию, но юниты пакета neurobot.target — в jetson.slice с MemoryMax и квотой CPU модели бюджета.

  sudo python3 -m neurobot.sim.runs.budget_a1 --out DIR --mem MIB --sched DIR --mockllm URL [--quota 106]
       [--ratio 0.265] [--cpus 4]

Что делает поверх обычного прогона A1 (всё снимается в конце, и при ошибке):
- drop-in `Slice=jetson.slice` для юнитов пакета в /run/systemd/system; свойства slice (`systemctl set-property
  --runtime`): MemoryMax, MemorySwapMax=0, CPUQuota, AllowedCPUs — как у budget/stack.py;
- балласт контекста CUDA (1024 МиБ, model.json бюджета) — в процессе perception: команда в env-файле пакета
  оборачивается `neurobot.sim.budget.ballast --run` (OOM на плате выбрал бы его);
- заместители драйверов Jetson (база ros2_control с mock_components, realsense2_camera и urg_node без устройств, балласт
  потоков камеры) — спецификации budget/stack.py в своём домене DDS; узлы двойника, которые на роботе работают на
  Jetson (twist_mux, robot_state_publisher, imu_filter_madgwick), — переносятся в slice scope-юнитами;
- в середине прогона — уникальные страницы slice (budget/collect.unique_snapshot), в конце — budget.json: память по
  компонентам, запас против веток модели (headless / LXDE / GNOME), OOM slice (memory.events).
Квота и отношение A57/ВМ — из калибровки бюджета 16.10 (`neurobot_20261016_budget/cpucal_host.json`) [допущение].
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from typing import Dict, Optional

from neurobot.sim.budget import model as budget_model
from neurobot.sim.budget import stack as budget_stack
from neurobot.sim.budget.collect import parse_flat, unique_snapshot
from neurobot.sim.runs import stacks
from neurobot.sim.runs.metrics import NOT_ROBOT
from neurobot.sim.runs.pico import MockLLM
from neurobot.sim.runs.runner import Runner, journal_dump

SLICE_CG = "/sys/fs/cgroup/jetson.slice"
DROPIN = "/run/systemd/system/%s.service.d/runs-jetson.conf"
PROXIES = ("base", "camera", "camera_streams", "lidar")


def ballast_cmd(cmd: str, mib: int) -> str:
    """Команда perception из env-файла, обёрнутая балластом CUDA в том же процессе (ballast.py --run)."""
    if "neurobot.sim.budget.ballast" in cmd:
        return cmd
    python, script, rest = cmd.split(" ", 2)
    return "%s -m neurobot.sim.budget.ballast --mib %d --run %s -- %s" % (python, mib, script, rest)


class A1Budget(stacks.A1):
    """A1 в jetson.slice: те же юниты и цель, плюс slice, балласт, заместители драйверов и перенос узлов двойника."""

    def __init__(self, run_dir: str, sched: str, mockllm: str, mem: int, quota: int, ratio: float, cpus: int):
        super().__init__(run_dir, sched, mockllm)
        self.model = budget_model.load()
        self.mem, self.quota, self.ratio, self.cpus = mem, quota, ratio, cpus
        self.env_orig: Optional[str] = None
        self.adopted: Dict[str, dict] = {}
        self.cfg = budget_stack.Cfg(user=stacks.sudo_user(), base="", step_dir=run_dir, mem_mib=mem, quota_pct=quota,
                                    ratio=ratio, cpus=cpus, domain=self.domain, twin_run=os.path.join(stacks.TW, "run"),
                                    model=self.model)

    def jetson_units(self):
        """Юниты пакета, которые на роботе работают на Jetson: все, кроме драйверов двойника (Gazebo и база, узлы
        лидара и камеры на Python — на роботе их место занимают заместители PROXIES)."""
        return [u for k, u in stacks.A1.units(self).items() if k not in NOT_ROBOT]

    def prepare(self) -> None:
        super().prepare()
        with open(stacks.ENV_FILE, encoding="utf-8") as f:
            self.env_orig = f.read()
        mib = budget_model.ballast_mib(self.model)["cuda"]
        text = re.sub(r'^(NEUROBOT_PERCEPTION_CMD=")(.*)(")$',
                      lambda m: m.group(1) + ballast_cmd(m.group(2), mib) + m.group(3), self.env_orig, flags=re.M)
        with open(stacks.ENV_FILE, "w", encoding="utf-8") as f:
            f.write(text)
        for u in self.jetson_units():
            path = DROPIN % u
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("[Service]\nSlice=jetson.slice\n")
        stacks.sh(["systemctl", "daemon-reload"])
        stacks.sh(["systemctl", "set-property", "--runtime", "jetson.slice"] + budget_stack.slice_props(self.cfg))

    def start(self) -> None:
        super().start()
        budget_stack.prepare_step(self.cfg)
        for s in budget_stack.specs(self.cfg):
            if s.name in PROXIES:
                stacks.sh(["systemctl", "reset-failed", budget_stack.unit(s.name)], check=False)
                stacks.sh(budget_stack.unit_argv(s, self.cfg))
        pgid = int(stacks.unit_prop("neurobot-driver@twin", "MainPID") or 0)
        self.adopted = budget_stack.adopt_twin(self.cfg, pgid)

    def units(self) -> Dict[str, str]:
        out = super().units()
        for name in PROXIES:
            out[name] = budget_stack.unit(name)
        for name, u in self.adopted.items():
            out[name] = u["unit"]
        return out

    def write_units_json(self) -> str:
        doc = {"slice": SLICE_CG, "units": {}}
        for k, u in self.units().items():
            if k in NOT_ROBOT:
                continue
            cg = stacks.unit_prop(u, "ControlGroup")
            doc["units"][k] = {"unit": u, "role": "ballast" if k == "camera_streams" else
                               "proxy" if k in PROXIES else "measured", "cgroup": "/sys/fs/cgroup" + cg}
        path = os.path.join(self.run_dir, "units.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        return path

    def stop(self) -> None:
        super().stop()
        for name in PROXIES:
            stacks.sh(["systemctl", "stop", budget_stack.unit(name)], check=False, timeout=60)
        for name in self.adopted:
            stacks.sh(["systemctl", "stop", budget_stack.unit(name, "scope")], check=False, timeout=60)

    def restore(self) -> None:
        """Снять всё своё: env-файл, drop-in'ы, свойства slice."""
        if self.env_orig is not None:
            with open(stacks.ENV_FILE, "w", encoding="utf-8") as f:
                f.write(self.env_orig)
        for u in self.jetson_units():
            path = DROPIN % u
            if os.path.exists(path):
                os.remove(path)
        stacks.sh(["systemctl", "revert", "jetson.slice"], check=False)
        stacks.sh(["systemctl", "daemon-reload"])


def slice_events() -> dict:
    out = {}
    for name in ("memory.events", "memory.peak", "memory.max"):
        try:
            with open(os.path.join(SLICE_CG, name)) as f:
                text = f.read().strip()
        except OSError:
            continue
        out[name] = parse_flat(text) if name == "memory.events" else text
    return out


def report(st: A1Budget, uniq: dict, events_before: dict, events_after: dict) -> dict:
    """Память по компонентам (уникальные страницы в середине прогона) и запас против веток модели бюджета."""
    budgets = budget_model.budgets(st.model)
    total = (uniq.get("slice_unique_kib") or 0) / 1024.0
    oom = (events_after.get("memory.events", {}).get("oom_kill", 0) -
           events_before.get("memory.events", {}).get("oom_kill", 0))
    return {"mem_max_mib": st.mem, "quota_pct": st.quota, "ratio_a57": st.ratio, "cpus": st.cpus,
            "unique_mib": {k: None if v is None else round(v / 1024.0, 1)
                           for k, v in uniq.get("units_kib", {}).items()},
            "slice_unique_mib": round(total, 1), "slice_pss_mib": round(uniq.get("slice_pss_kib", 0) / 1024.0, 1),
            "slice_rss_mib": round(uniq.get("slice_rss_kib", 0) / 1024.0, 1),
            "margin_mib": {b: {c: round(v - total, 1) for c, v in cases.items()} for b, cases in budgets.items()},
            "oom_kill": oom, "slice_events_end": events_after}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m neurobot.sim.runs.budget_a1", description=__doc__.split("\n\n")[0])
    p.add_argument("--out", required=True, help="новый каталог прогона в ВМ")
    p.add_argument("--mem", type=int, required=True, help="MemoryMax jetson.slice, МиБ (бюджет ветки ОС)")
    p.add_argument("--sched", required=True, help="каталог расписания")
    p.add_argument("--mockllm", required=True, help="bench/mockllm в ВМ, напр. http://127.0.0.1:18150")
    p.add_argument("--quota", type=int, default=106, help="CPUQuota slice, %% ядра ВМ (cpucal бюджета 16.10)")
    p.add_argument("--ratio", type=float, default=0.265, help="ядро A57 / ядро ВМ (cpucal бюджета 16.10)")
    p.add_argument("--cpus", type=int, default=4, help="AllowedCPUs=0-(N-1)")
    p.add_argument("--label", default="budget", help="метка прогона")
    a = p.parse_args(argv)
    if os.geteuid() != 0 or not stacks.A1.installed() or os.path.exists(a.out):
        print("budget_a1: нужен root, поставленный пакет A1 и новый --out", file=sys.stderr)
        return 2
    os.makedirs(os.path.join(a.out, "logs"))
    with open(os.path.join(a.sched, "plan.json"), encoding="utf-8") as f:
        plan = json.load(f)
    st = A1Budget(a.out, a.sched, a.mockllm, a.mem, a.quota, a.ratio, a.cpus)
    runner = Runner(st, plan, a.out, MockLLM(a.mockllm), 30.0, 40.0, 10.0, a.label)
    uniq: dict = {}
    before = slice_events()

    def snapshot() -> None:
        while runner.anchor is None:
            time.sleep(1.0)
        time.sleep(max(0.0, runner.anchor.mono_of(plan["duration_s"] / 2) - time.monotonic()))
        with open(os.path.join(a.out, "units.json"), encoding="utf-8") as f:
            uniq.update(unique_snapshot(json.load(f)))
    threading.Thread(target=snapshot, daemon=True).start()
    t0, rc = time.time(), 0
    try:
        doc = runner.run(a.sched)
        doc["budget"] = report(st, uniq, before, slice_events())
    except (Exception, KeyboardInterrupt) as e:
        rc = 1
        runner.stop_procs()
        doc = {"system": "a1", "label": a.label, "error": "%s: %s" % (type(e).__name__, e), "events": runner.events}
    finally:
        journal_dump(list(st.units().values()), t0, os.path.join(a.out, "logs", "journal.txt"))
        st.stop()
        st.restore()
    for name, dest in (("truth.jsonl", "truth_schedule.jsonl"), ("plan.json", "plan.json")):
        stacks.sh(["cp", os.path.join(a.sched, name), os.path.join(a.out, dest)])
    for path in (st.probe_path, st.anchor_path):
        if os.path.exists(path):
            stacks.sh(["cp", path, a.out])
    with open(os.path.join(a.out, "run.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1, default=str)
    stacks.sh(["chmod", "-R", "a+rX", a.out], check=False)
    return rc


if __name__ == "__main__":
    sys.exit(main())
