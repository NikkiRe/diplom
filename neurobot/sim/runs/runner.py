"""(ВМ sim, root) Прогон одной системы (A0 или A1) по общему расписанию (README §2–§4).

  sudo python3 -m neurobot.sim.runs.runner run --system a0|a1 --sched DIR --out DIR --mockllm URL [флаги]
  sudo python3 -m neurobot.sim.runs.runner a1-install --mockllm URL      пакет на двойнике (setup_twin.sh + правки)
  sudo python3 -m neurobot.sim.runs.runner a1-teardown

run: подготовка и подъём системы → наблюдатель жестов (neurobot/sim/gestures/observer.py) и сэмплер памяти
(neurobot/sim/budget/collect.py) → по якорю таймлайна события плана: отказы (kill -9, SIGSTOP; отказы камеры
вносят подставной pyrealsense2 у A0 и relay.py у A1 сами), реплики пользователя в канал pico PicoClaw → оператор:
человек, который через --operator-delay с замечает, что робот глух, процесс умер, агент не ответил, робот застрял
или стоит на защёлке, и чинит это; каждое действие оператора — вмешательство человека (видимый перезапуск,
PLAN §1). В --out: run.json (события, TTR, вмешательства, запросы агенту, рестарты), truth.jsonl, safety.jsonl,
наблюдатель (events.jsonl, summary.json, gestures.jsonl), collect.jsonl, mockllm.jsonl, журналы. Логи — stderr.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional

from neurobot.sim.gestures.clock import mono, read_anchor
from neurobot.sim.runs import stacks
from neurobot.sim.runs.pico import MockLLM, PicoClient, final_text

PHRASES = {"status": "Какое сейчас состояние робота?", "move": "Проедь немного вперёд."}
SPAWN = (0.0, 0.0, 0.01, 0.0)
DEAF_S = 10.0
STUCK_S = 20.0
CONTACT_GAP_S = 1.0
TTR_MAX_S = 300.0
ERROR_MARKS = ("error", "not available", "failed", "connection closed", "refused", "timed out", "timeout")


class ProbeTail:
    """Журнал подставных модулей (строки раз в секунду: m, pid, det_calls): идёт ли детектор."""

    def __init__(self, path: str):
        self.path, self.pos, self.rows = path, 0, []
        self.lock = threading.Lock()

    def poll(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                f.seek(self.pos)
                chunk = f.read()
        except OSError:
            return
        lines = chunk.split("\n")
        tail = lines.pop()
        with self.lock:
            self.pos += len(chunk.encode("utf-8")) - len(tail.encode("utf-8"))
            for line in lines:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if "det_calls" in r and "m" in r:
                    self.rows.append((float(r["m"]), int(r.get("pid", 0)), int(r["det_calls"])))

    def progress_since(self, m0: float) -> Optional[float]:
        """Момент первой строки после m0, у которой det_calls вырос против прошлой строки того же процесса (тоже
        после m0); None — детектор с m0 не работал."""
        last: Dict[int, int] = {}
        with self.lock:
            for m, pid, calls in self.rows:
                if m <= m0:
                    continue
                if pid in last and calls > last[pid]:
                    return m
                last[pid] = calls
        return None

    def last_progress(self) -> Optional[float]:
        """Момент последнего прироста det_calls."""
        prev: Dict[int, int] = {}
        out = None
        with self.lock:
            for m, pid, calls in self.rows:
                if pid in prev and calls > prev[pid]:
                    out = m
                prev[pid] = calls
        return out


def tool_outcomes(session_dir: str, text: str) -> List[dict]:
    """Вызовы инструментов хода, начатого репликой text, по сессиям PicoClaw на диске: [{name, ok, content}]."""
    out: List[dict] = []
    for path in sorted(glob.glob(os.path.join(session_dir, "*.jsonl"))):
        try:
            with open(path, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f if line.strip()]
        except (OSError, ValueError):
            continue
        names: Dict[str, str] = {}
        inside = False
        for r in rows:
            if r.get("role") == "user":
                inside = text in str(r.get("content", ""))
                continue
            if not inside:
                continue
            for c in r.get("tool_calls") or []:
                names[c.get("id", "")] = (c.get("function") or {}).get("name", "")
            if r.get("role") == "tool":
                content = str(r.get("content", ""))
                out.append({"name": names.get(r.get("tool_call_id", ""), ""), "ok": tool_ok(content),
                            "content": content[:300]})
    return out


def tool_ok(content: str) -> bool:
    try:
        d = json.loads(content)
    except ValueError:
        d = None
    if isinstance(d, dict):
        return d.get("ok") is not False and "error" not in d
    low = content.lower()
    return not any(mark in low for mark in ERROR_MARKS)


class Runner:
    def __init__(self, stack: stacks.Stack, plan: dict, out: str, mock: MockLLM, operator_delay: float,
                 tail_s: float, collect_interval: float, label: str):
        self.stack, self.plan, self.out, self.mock = stack, plan, out, mock
        self.operator_delay, self.tail_s, self.collect_interval, self.label = operator_delay, tail_s, \
            collect_interval, label
        self.lock = threading.Lock()
        self.events: List[dict] = []
        self.agent: List[dict] = []
        self.probe = ProbeTail(stack.probe_path)
        self.flags: Dict[str, float] = {}           # что заметил оператор: ключ → момент (mono)
        self.truth_last: Optional[dict] = None
        self.contact_since: Optional[float] = None    # касание без перерывов дольше CONTACT_GAP_S
        self.contact_last = 0.0
        self.safety_last: Optional[dict] = None
        self.estop_since: Optional[float] = None
        self.procs: Dict[str, subprocess.Popen] = {}
        self.threads: List[threading.Thread] = []
        self.done = threading.Event()             # конец прогона: наблюдение за восстановлением прекращается
        self.anchor = None

    # ---- журнал событий

    def event(self, **ev) -> dict:
        ev.update(t=round(time.time(), 3), m=round(mono(), 3))
        with self.lock:
            self.events.append(ev)
        stacks.log("%s %s" % (self.stack.name, json.dumps(ev, ensure_ascii=False)))
        return ev

    # ---- наблюдатели

    def ros_env(self) -> Dict[str, str]:
        """Окружение DDS системы; журналы rcl обвязки — в свой каталог (каталог системы пишут её процессы)."""
        return dict(self.stack.ros_env(), ROS_LOG_DIR=os.path.join(self.out, "logs", "ros_runner"))

    def env(self) -> Dict[str, str]:
        path = ":".join(p for p in (stacks.SRC, os.environ.get("PYTHONPATH", "")) if p)
        return dict(os.environ, PYTHONPATH=path, PYTHONDONTWRITEBYTECODE="1", **self.ros_env())

    def start_observer(self, sched: str) -> None:
        world = os.path.join(stacks.SRC, "neurobot/sim/runs/worlds/hall.sdf")
        argv = ["python3", "-m", "neurobot.sim.gestures.observer", "--out", os.path.join(self.out, "events.jsonl"),
                "--truth", os.path.join(sched, "truth.jsonl"), "--anchor", self.stack.anchor_path,
                "--summary", os.path.join(self.out, "summary.json"),
                "--per-gesture", os.path.join(self.out, "gestures.jsonl"), "--cmd-vel-type", "twist",
                "--world-sdf", world, "--wall-regex", stacks.WALL_REGEX,
                "--footprint-radius", str(stacks.FOOTPRINT_R)] + self.stack.observer_extra
        logf = open(os.path.join(self.out, "logs", "observer.log"), "a")
        self.procs["observer"] = subprocess.Popen(argv, env=self.env(), stdout=logf, stderr=logf,
                                                  cwd=stacks.SRC)

    def start_collect(self) -> None:
        units = self.stack.write_units_json()
        logf = open(os.path.join(self.out, "logs", "collect.log"), "a")
        self.procs["collect"] = subprocess.Popen(
            ["python3", "-m", "neurobot.sim.budget.collect", "--units", units, "--out",
             os.path.join(self.out, "collect.jsonl"), "--interval", str(self.collect_interval)],
            env=self.env(), stdout=logf, stderr=logf, cwd=stacks.SRC)

    def stop_procs(self) -> None:
        for name in ("observer", "collect"):
            p = self.procs.get(name)
            if p and p.poll() is None:
                p.send_signal(signal.SIGINT if name == "observer" else signal.SIGTERM)
                try:
                    p.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    p.kill()
            if p:
                self.event(kind="proc_exit", name=name, rc=p.returncode)

    # ---- ROS: истина и состояние безопасности

    def ros(self):
        import rclpy
        from ros_gz_interfaces.srv import SetEntityPose
        from std_msgs.msg import String
        os.environ.update(self.ros_env())
        rclpy.init()
        node = rclpy.create_node("runs_runner_" + self.stack.name)
        truth_f = open(os.path.join(self.out, "truth.jsonl"), "a", encoding="utf-8")
        safety_f = open(os.path.join(self.out, "safety.jsonl"), "a", encoding="utf-8")
        last_write = {"truth": 0.0, "safety": 0.0}

        def on_truth(msg):
            try:
                d = json.loads(msg.data)
            except ValueError:
                return
            m = mono()
            prev = self.truth_last
            self.truth_last = dict(d, m=m)
            if d.get("contacts"):
                self.contact_since = self.contact_since or m
                self.contact_last = m
            elif self.contact_since and m - self.contact_last > CONTACT_GAP_S:
                self.contact_since = None
            changed = prev is None or prev.get("contacts") != d.get("contacts")
            if changed or m - last_write["truth"] >= 0.2:
                last_write["truth"] = m
                keep = ("t", "x", "y", "yaw", "v", "w", "clearance_m", "nearest", "contacts", "rtf", "rtf_ok")
                truth_f.write(json.dumps(dict({k: d.get(k) for k in keep}, m=round(m, 3)), ensure_ascii=False)
                              + "\n")

        def on_safety(msg):
            try:
                d = json.loads(msg.data)
            except ValueError:
                return
            m = mono()
            prev = self.safety_last
            self.safety_last = dict(d, m=m)
            if d.get("state") == "estop":
                self.estop_since = self.estop_since or m
            else:
                self.estop_since = None
            changed = prev is None or (prev.get("state"), prev.get("reasons")) != (d.get("state"), d.get("reasons"))
            if changed or m - last_write["safety"] >= 1.0:
                last_write["safety"] = m
                safety_f.write(json.dumps({"m": round(m, 3), "t": round(time.time(), 3), "state": d.get("state"),
                                           "reasons": d.get("reasons"), "min_clearance_m": d.get("min_clearance_m")},
                                          ensure_ascii=False) + "\n")

        node.create_subscription(String, "/twin/truth", on_truth, 50)
        node.create_subscription(String, "/neurobot/safety_state", on_safety, 50)
        self.set_pose_cli = node.create_client(SetEntityPose, "/world/default/set_pose")
        self.node, self.rclpy = node, rclpy
        self.files = (truth_f, safety_f)

    def spin(self, seconds: float) -> None:
        end = mono() + seconds
        while mono() < end:
            self.rclpy.spin_once(self.node, timeout_sec=min(0.05, max(end - mono(), 0.0)))

    def set_pose(self, x: float, y: float, z: float, yaw: float) -> bool:
        from ros_gz_interfaces.msg import Entity
        from ros_gz_interfaces.srv import SetEntityPose
        req = SetEntityPose.Request()
        req.entity.name, req.entity.type = "neurochip", Entity.MODEL
        req.pose.position.x, req.pose.position.y, req.pose.position.z = x, y, z
        req.pose.orientation.z, req.pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)
        if not self.set_pose_cli.wait_for_service(timeout_sec=5.0):
            return False
        fut = self.set_pose_cli.call_async(req)
        end = mono() + 10.0
        while not fut.done() and mono() < end:
            self.rclpy.spin_once(self.node, timeout_sec=0.05)
        return bool(fut.done() and fut.result().success)

    # ---- события плана

    def fire(self, ev: dict) -> None:
        kind, role = ev["kind"], ev.get("target")
        base = {"plan_n": ev["n"], "plan_kind": kind, "seg": ev["seg"], "at_s": ev["at_s"]}
        if kind == "agent":
            self.spawn(self.agent_request, ev)
            return
        if kind == "camera":
            rec = self.event(kind="fault", fault="camera_" + ev["fault"], duration_s=ev["duration_s"], **base)
            self.spawn(self.watch_recovery, rec, "perception", ev["duration_s"])
            return
        if not self.stack.has(role):
            self.event(kind="fault", fault="%s_%s" % (kind, role), skipped="у системы нет компонента", **base)
            return
        pid = self.stack.pid(role)
        if pid <= 0:
            self.event(kind="fault", fault="%s_%s" % (kind, role), skipped="процесс не найден", **base)
            return
        if kind == "kill":
            os.kill(pid, signal.SIGKILL)
            rec = self.event(kind="fault", fault="kill_" + role, pid=pid, **base)
            self.spawn(self.watch_recovery, rec, role, 0.0, pid)
        elif kind == "stop":
            os.kill(pid, signal.SIGSTOP)
            rec = self.event(kind="fault", fault="stop_" + role, pid=pid, duration_s=ev["duration_s"], **base)
            self.spawn(self.cont_after, pid, ev["duration_s"])
            self.spawn(self.watch_recovery, rec, role, ev["duration_s"])

    def spawn(self, fn, *args) -> None:
        th = threading.Thread(target=fn, args=args, daemon=True)
        th.start()
        self.threads.append(th)

    def cont_after(self, pid: int, seconds: float) -> None:
        time.sleep(seconds)
        try:
            os.kill(pid, signal.SIGCONT)
            self.event(kind="sigcont", pid=pid)
        except ProcessLookupError:
            self.event(kind="sigcont", pid=pid, gone=True)

    def watch_recovery(self, rec: dict, role: str, hold_s: float, killed: int = 0) -> None:
        """TTR: от отказа до «роль снова работает» (perception — детектор снова вызывается после конца отказа;
        после kill -9 — работает уже новый процесс)."""
        t0 = rec["m"]
        rec["ttr_s"] = None
        time.sleep(min(hold_s, 1.0))
        while mono() - t0 < TTR_MAX_S and not self.done.is_set():
            self.probe.poll()
            if role == "perception":
                got = self.probe.progress_since(t0 + hold_s)
                ok = got is not None
            else:
                ok = mono() - t0 >= hold_s and self.stack.pid(role) not in (0, killed) and self.stack.up(role)
                got = mono() if ok else None
            if ok:
                rec["ttr_s"] = round(got - t0, 2)
                return
            time.sleep(0.25)

    def agent_request(self, ev: dict) -> None:
        n, kind = ev["n"], ev["request"]
        text = "[runs %s %s #%d] %s" % (self.label, self.stack.name, n, PHRASES[kind])
        input_id = "%s-%s-%d" % (self.label, self.stack.name, n)
        rec = {"n": n, "request": kind, "text": text, "seg": ev["seg"], "at_s": ev["at_s"], "t": round(time.time(), 3),
               "m": round(mono(), 3)}
        try:
            self.mock.expect(input_id, text, self.stack.agent_tools[kind])
        except OSError as e:
            rec["error"] = "mockllm: %s" % e
        msgs: List[dict] = []
        if "error" not in rec:
            try:
                client = PicoClient("127.0.0.1", self.stack.gateway_port, self.stack.token, "runs-" + self.label)
                try:
                    msgs = client.send(text, timeout=60.0)
                finally:
                    client.close()
            except (OSError, ConnectionError, ValueError) as e:
                rec["error"] = "pico: %s" % e
        rec["latency_s"] = round(mono() - rec["m"], 3)
        rec["final"] = final_text(msgs)
        rec["channel_errors"] = [m.get("payload") for m in msgs if m.get("type") == "error"]
        time.sleep(1.0)
        rec["tools"] = tool_outcomes(self.stack.session_dir(), text)
        rec["ok"] = bool(rec["final"]) and bool(rec["tools"]) and all(t["ok"] for t in rec["tools"])
        try:
            self.mock.clear(input_id)
        except OSError:
            pass
        with self.lock:
            self.agent.append(rec)
        self.event(kind="agent", n=n, request=kind, ok=rec["ok"], latency_s=rec["latency_s"],
                   error=rec.get("error"))
        if not rec["ok"]:
            self.flags.setdefault("agent", mono())

    # ---- оператор

    def operator(self) -> None:
        now = mono()
        st = self.stack
        if self.contact_since and now - self.contact_since > STUCK_S:
            self.contact_since = None
            self.relocate()
        if isinstance(st, stacks.A0):
            self.probe.poll()
            last = self.probe.last_progress()
            demo_dead = not stacks.unit_active(st.UNITS["demo"])
            deaf = last is not None and now - last > DEAF_S
            if demo_dead or deaf:
                self.flags.setdefault("demo", now)
            else:
                self.flags.pop("demo", None)
            if not stacks.unit_active(st.UNITS["picoclaw"]):
                self.flags.setdefault("agent", now)
            for key, unit_key in (("demo", "demo"), ("agent", "picoclaw")):
                since = self.flags.get(key)
                if since is not None and now - since >= self.operator_delay:
                    self.flags.pop(key)
                    st.restart(unit_key)
                    self.event(kind="operator", action="restart", what=unit_key,
                               why="процесс умер" if key == "demo" and demo_dead else
                               "глухой (детектор не вызывается)" if key == "demo" else "агент не ответил или умер")
        else:
            if self.estop_since and now - self.estop_since >= self.operator_delay:
                reasons = (self.safety_last or {}).get("reasons")
                res = st.estop_release()
                self.estop_since = None
                self.event(kind="operator", action="estop_release", reasons=reasons, rc=res["rc"])
            failed = [u for u in st.units().values() if stacks.unit_prop(u, "ActiveState") == "failed"]
            if failed:
                self.flags.setdefault("failed", now)
                if now - self.flags["failed"] >= self.operator_delay:
                    self.flags.pop("failed")
                    for u in failed:
                        stacks.sh(["systemctl", "reset-failed", u], check=False)
                        stacks.sh(["systemctl", "start", "--no-block", u], check=False)
                    self.event(kind="operator", action="start_failed", units=failed)
            else:
                self.flags.pop("failed", None)

    def relocate(self) -> None:
        """Робот застрял в касании: человек переносит его на место старта (у A1 — при выключенном safety_gate:
        перенос — удар для IMU, как harness.relocate наборов)."""
        a1 = isinstance(self.stack, stacks.A1)
        contacts = (self.truth_last or {}).get("contacts")
        if a1:
            stacks.sh(["systemctl", "stop", "neurobot-safety"], check=False)
        ok = self.set_pose(*SPAWN)
        self.spin(1.5)
        if a1:
            stacks.sh(["systemctl", "start", "neurobot-safety"], check=False, timeout=60)
        self.event(kind="operator", action="relocate", contacts=contacts, ok=ok)

    # ---- прогон

    def run(self, sched: str) -> dict:
        self.stack.prepare()
        self.start_observer(sched)
        t_start = time.time()
        seq0 = self.mock.state().get("journal_seq", 0)
        self.event(kind="start")
        self.stack.start()
        self.event(kind="ready")
        self.start_collect()
        self.ros()
        self.anchor = read_anchor(self.stack.anchor_path)
        todo = sorted(self.plan["events"], key=lambda e: e["at_s"])
        end = self.anchor.mono_of(self.plan["duration_s"] + self.tail_s)
        next_op = 0.0
        while mono() < end:
            self.spin(0.1)
            while todo and self.anchor.mono_of(todo[0]["at_s"]) <= mono():
                self.fire(todo.pop(0))
            if mono() >= next_op:
                next_op = mono() + 2.0
                self.operator()
        self.done.set()
        for th in self.threads:
            th.join(timeout=70.0)
        self.event(kind="end")
        self.stop_procs()
        restarts = self.stack.restarts()
        try:
            journal = self.mock.journal(seq0)
        except OSError as e:
            journal = []
            self.event(kind="mockllm_journal_error", error=str(e))
        with open(os.path.join(self.out, "mockllm.jsonl"), "w", encoding="utf-8") as f:
            for r in journal:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        for f in self.files:
            f.close()
        self.node.destroy_node()
        self.rclpy.try_shutdown()
        return {"system": self.stack.name, "label": self.label, "plan": self.plan, "t_start": t_start,
                "t_end": time.time(), "anchor": self.anchor.__dict__, "events": self.events, "agent": self.agent,
                "restarts": restarts, "operator_delay_s": self.operator_delay, "units": self.stack.units()}


def journal_dump(units: List[str], since: float, path: str) -> None:
    cmd = ["journalctl", "--no-pager", "-o", "short-iso-precise", "--since", "@%d" % int(since)]
    for u in units:
        cmd += ["-u", u]
    with open(path, "w", encoding="utf-8") as f:
        f.write(stacks.sh(cmd, check=False, timeout=300).stdout)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m neurobot.sim.runs.runner", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="прогон системы по расписанию")
    r.add_argument("--system", required=True, choices=("a0", "a1"), help="A0 — стек лаборатории, A1 — пакет 1 + v2")
    r.add_argument("--sched", required=True, help="каталог расписания (schedule.py make) в ВМ")
    r.add_argument("--out", required=True, help="новый каталог прогона в ВМ")
    r.add_argument("--mockllm", required=True, help="bench/mockllm, напр. http://host.lima.internal:18150")
    r.add_argument("--label", default="main", help="метка прогона в репликах агенту и в run.json")
    r.add_argument("--operator-delay", type=float, default=30.0, help="через сколько с человек чинит замеченное")
    r.add_argument("--tail-s", type=float, default=40.0, help="запись после конца таймлайна, с")
    r.add_argument("--collect-interval", type=float, default=10.0, help="период сэмплера памяти, с")
    r.add_argument("--ml-python", default="/home/chosenundead.guest/nb/venv/bin/python",
                   help="Python с torch для демо A0")
    r.add_argument("--picoclaw", default="/var/tmp/nb-pc028/picoclaw", help="PicoClaw 0.2.8 linux-arm64 (A0)")
    r.add_argument("--mcp-venv", default=os.path.join(stacks.BASE, "mcpvenv"),
                   help="venv с mcp для прокси лаборатории (A0)")
    r.add_argument("--keep", action="store_true", help="не останавливать систему в конце")
    i = sub.add_parser("a1-install", help="поставить пакет на двойник (setup_twin.sh + правки прогона)")
    i.add_argument("--mockllm", required=True, help="bench/mockllm на Mac")
    sub.add_parser("a1-teardown", help="снять пакет с двойника")
    a = p.parse_args(argv)
    if os.geteuid() != 0:
        print("runner: нужен root (sudo)", file=sys.stderr)
        return 2
    if a.cmd == "a1-install":
        stacks.A1(os.path.join(stacks.BASE, "a1-install"), "", a.mockllm).install()
        return 0
    if a.cmd == "a1-teardown":
        stacks.A1.teardown()
        return 0
    if os.path.exists(a.out):
        print("runner: --out уже есть: %s (не перезаписываю)" % a.out, file=sys.stderr)
        return 2
    os.makedirs(os.path.join(a.out, "logs"))
    with open(os.path.join(a.sched, "plan.json"), encoding="utf-8") as f:
        plan = json.load(f)
    if a.system == "a0":
        stack = stacks.A0(a.out, a.sched, a.mockllm, a.ml_python, a.picoclaw, a.mcp_venv)
    else:
        if not stacks.A1.installed():
            print("runner: пакет A1 не поставлен (a1-install)", file=sys.stderr)
            return 2
        stack = stacks.A1(a.out, a.sched, a.mockllm)
    runner = Runner(stack, plan, a.out, MockLLM(a.mockllm), a.operator_delay, a.tail_s, a.collect_interval, a.label)
    t0 = time.time()
    rc = 0

    def interrupt(*_):
        raise KeyboardInterrupt("SIGTERM")
    signal.signal(signal.SIGTERM, interrupt)
    try:
        doc = runner.run(a.sched)
    except (Exception, KeyboardInterrupt) as e:  # прогон прерван: что успели — в run.json, система остановлена
        rc = 1
        runner.stop_procs()
        doc = {"system": a.system, "label": a.label, "error": "%s: %s" % (type(e).__name__, e),
               "events": runner.events, "agent": runner.agent}
        stacks.log("прогон прерван: %s" % doc["error"])
    for path in (stack.probe_path, stack.anchor_path):
        if os.path.exists(path) and not os.path.exists(os.path.join(a.out, os.path.basename(path))):
            shutil.copy(path, a.out)
    for name, dest in (("truth.jsonl", "truth_schedule.jsonl"), ("plan.json", "plan.json"),
                       ("faults.json", "faults.json")):
        shutil.copy(os.path.join(a.sched, name), os.path.join(a.out, dest))
    if isinstance(stack, stacks.A1):
        journal_dump(list(stack.units().values()) + [stacks.A1.RELAY], t0, os.path.join(a.out, "logs", "journal.txt"))
    if not a.keep:
        stack.stop()
    with open(os.path.join(a.out, "run.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1, default=str)
    stacks.sh(["chmod", "-R", "a+rX", a.out], check=False)
    return rc


if __name__ == "__main__":
    sys.exit(main())
