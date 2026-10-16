"""start_test: холодные старты neurobot.target с doctor и сценарии «сломанного старта» (PLAN §5.4, K9).

Цикл ok: остановить всё → reset-failed → start --no-block neurobot.target → doctor --json, пока не «ОК» (граф ROS
не снялся — у doctor это ошибка ros_unavailable, а не «ОК») → (по желанию) жест: --gesture-cmd, код 0 — успех →
stop neurobot.target → в журнале safety_gate после остановки есть строка `neurobot zero` с кодом zero_sent (нули
ушли подписчику; отказ и ноль без подписчиков — провал цикла). Пишутся: время до active у цели по systemd, время до
«ОК» doctor (с задержкой самого doctor), порядок готовности юнитов, рестарты за цикл, строка явного нуля.
Сценарии поломки — без вреда роботу: подмена значений env-файла drop-in'ом юнита в /run (исчезает при
перезагрузке), занятый порт держит сам start_test:
  safety_down    вместо safety_gate — sleep 3600: READY нет, через TimeoutStartSec (30 с) — отказ и рестарт;
                 исполнитель без safety_gate не стартует (precheck gate_missing)       → safety/start_timeout
  no_camera      NEUROBOT_CAMERA_USB=ffff:ffff у распознавания (кабель не выдёргиваем) → perception/camera_missing
  no_weights     манифест, где первый файл весов указывает на несуществующий путь     → perception/weights_missing
  port_busy      start_test слушает порт rosbridge                                     → rosbridge/port_busy
  rosbridge_down вместо rosbridge — sleep 3600 с теми же аргументами (проверка глобов перед стартом проходит),
                 ожидание порта 5 с                                                     → rosbridge/port_timeout
  worker_down    вместо первого воркера лаборатории — sleep 3600, ожидание 5 с
                                                                        → labworker@<имя>/labworker_timeout
Сценарий пройден, если doctor назвал именно эти компонент и код раньше --ready-timeout. По умолчанию — сценарии
тех компонентов, что есть в NEUROBOT_COMPONENTS и NEUROBOT_LABWORKERS. Подмену снимает finally цикла (и при Ctrl+C, и
при SIGTERM — его CLI превращает в выход); подмены прогона, убитого SIGKILL, start_test убирает перед первым циклом.
Итоги — <out>/cycles.jsonl, summary.json, manifest.json. Нужен root (systemctl, drop-in в /run).
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import platform
import shlex
import socket
import subprocess
import sys
import time
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import __version__, report
from .config import Config, quote
from .sd import Systemd

TARGET = "neurobot.target"
DROPIN_ROOT = "/run/systemd/system"
WORK = "/run/neurobot-start-test"
DROPIN = "50-neurobot-start-test.conf"


class Scenario(NamedTuple):
    name: str
    expect: Optional[Tuple[str, str]]          # (компонент, код), None — ожидается «ОК»
    env: Dict[str, Dict[str, str]] = {}        # компонент -> подменённые значения
    hold_port: str = ""                        # ключ env-файла с портом, который держит start_test


SCENARIOS = {
    "ok": Scenario("ok", None),
    "safety_down": Scenario("safety_down", ("safety", "start_timeout"),
                            {"safety": {"NEUROBOT_SAFETY_CMD": "sleep 3600"}}),
    "no_camera": Scenario("no_camera", ("perception", "camera_missing"),
                          {"perception": {"NEUROBOT_CAMERA_USB": "ffff:ffff"}}),
    "no_weights": Scenario("no_weights", ("perception", "weights_missing"),
                           {"perception": {"NEUROBOT_WEIGHTS": WORK + "/weights.sha256"}}),
    "port_busy": Scenario("port_busy", ("rosbridge", "port_busy"), hold_port="NEUROBOT_ROSBRIDGE_PORT"),
}
BROKEN = ("safety_down", "no_camera", "no_weights", "port_busy", "rosbridge_down", "worker_down")
_NEEDS = {"safety_down": "safety", "no_camera": "perception", "no_weights": "perception", "port_busy": "rosbridge",
          "rosbridge_down": "rosbridge"}


def applicable(cfg: Config) -> List[str]:
    """Сценарии поломки, которые есть смысл гонять при этом составе."""
    comps = cfg.components()
    return [b for b in BROKEN if (_NEEDS[b] in comps if b in _NEEDS else bool(cfg.labworkers()))]


def scenario(cfg: Config, name: str) -> Scenario:
    if name == "rosbridge_down":
        # sh -c отдаёт аргументы команды rosbridge в $0…, их не применяя: precheck видит те же глобы
        return Scenario(name, ("rosbridge", "port_timeout"), {"rosbridge": {
            "NEUROBOT_ROSBRIDGE_CMD": "sh -c 'exec sleep 3600' " + cfg.get("NEUROBOT_ROSBRIDGE_CMD"),
            "NEUROBOT_WAIT_S": "5"}})
    if name == "worker_down":
        w = cfg.labworkers()[0]
        return Scenario(name, ("labworker@" + w, "labworker_timeout"), {"labworker@" + w: {
            "NEUROBOT_LABWORKER_{}_CMD".format(w): "sleep 3600", "NEUROBOT_LABWORKER_WAIT_S": "5"}})
    return SCENARIOS[name]


def clear_leftovers(sd: Systemd, dropin_root: str = DROPIN_ROOT, work: str = WORK) -> List[str]:
    """Подмены прерванного прогона (start_test убит SIGKILL): drop-in'ы start_test в /run и их env-файлы. Без этого
    юнит жил бы с подменой («sleep 3600» вместо safety_gate) до перезагрузки. Убранные пути."""
    gone = sorted(glob.glob(os.path.join(dropin_root, "neurobot-*.d", DROPIN)))
    gone += sorted(os.path.join(work, n) for n in (os.listdir(work) if os.path.isdir(work) else []))
    for path in gone:
        os.remove(path)
    if any(p.endswith(DROPIN) for p in gone):
        sd.ctl("daemon-reload")
    return gone


def quantile(xs: Sequence[float], q: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (pos - lo), 3)


def summarize(cycles: List[dict]) -> dict:
    out = {}
    for name in sorted({c["scenario"] for c in cycles}):
        cs = [c for c in cycles if c["scenario"] == name]
        row = {"cycles": len(cs), "passed": sum(c["passed"] for c in cs),
               "failed": [{"cycle": c["cycle"], "why": c["why"]} for c in cs if not c["passed"]]}
        if name == "ok":
            for key in ("t_target_s", "t_doctor_s"):
                xs = [c[key] for c in cs if c.get(key) is not None]
                row[key] = {"p50": quantile(xs, 0.5), "p95": quantile(xs, 0.95), "max": max(xs) if xs else None}
            row["order_ok"] = sum(bool(c.get("order_ok")) for c in cs)
            row["zero_on_stop"] = sum(bool(c.get("zero_on_stop")) for c in cs)
            row["restarts"] = sum(c.get("restarts", 0) for c in cs)
        out[name] = row
    return out


def weights_missing_manifest(manifest: str) -> str:
    """Копия манифеста весов, где путь первого файла заменён несуществующим."""
    with open(manifest, encoding="utf-8") as f:
        lines = f.read().splitlines()
    for i, line in enumerate(lines):
        if line.strip() and not line.startswith("#"):
            digest, _, path = line.partition(" ")
            lines[i] = "{}  {}.start-test-missing".format(digest, path.strip().lstrip("*"))
            break
    return "\n".join(lines) + "\n"


class StartTest:
    def __init__(self, cfg: Config, sd: Systemd, doctor_cmd: Sequence[str], out_dir: str,
                 ready_timeout: float, stop_timeout: float, gesture_cmd: str = "",
                 sleep: Callable[[float], None] = time.sleep, dropin_root: str = DROPIN_ROOT, work: str = WORK,
                 doctor_env: Optional[Dict[str, str]] = None):
        self.cfg, self.sd, self.doctor_cmd, self.out_dir = cfg, sd, list(doctor_cmd), out_dir
        self.doctor_env = doctor_env
        self.ready_timeout, self.stop_timeout, self.gesture_cmd = ready_timeout, stop_timeout, gesture_cmd
        self.sleep, self.dropin_root, self.work = sleep, dropin_root, work
        self.units = [u for _, u in cfg.plan()]

    # --- systemd ---

    def stop_all(self) -> bool:
        self.sd.ctl("stop", TARGET, *self.units)
        end = time.monotonic() + self.stop_timeout
        while time.monotonic() < end:
            props = self.sd.show(self.units)
            if all(p.get("ActiveState") in ("inactive", "failed") for p in props.values()):
                self.sd.ctl("reset-failed", *self.units)
                return True
            self.sleep(0.5)
        return False

    def doctor(self) -> dict:
        r = subprocess.run(self.doctor_cmd + ["--json"], capture_output=True, text=True, timeout=120,
                           env=self.doctor_env)
        try:
            return json.loads(r.stdout)
        except ValueError:
            return {"ok": False, "exit": r.returncode, "root": {"component": "doctor", "code": "doctor_error",
                                                                "text": (r.stderr or r.stdout)[-300:]}}

    # --- поломки ---

    def _sabotage(self, sc: Scenario) -> Callable[[], None]:
        os.makedirs(self.work, exist_ok=True)
        files, sock = [], None
        if sc.name == "no_weights":
            path = os.path.join(self.work, "weights.sha256")
            with open(path, "w", encoding="utf-8") as f:
                f.write(weights_missing_manifest(self.cfg.get("NEUROBOT_WEIGHTS")))
            os.chmod(path, 0o644)
            files.append(path)
        for comp, env in sc.env.items():
            unit = "neurobot-{}.service".format(comp)
            env_file = os.path.join(self.work, unit + ".env")
            with open(env_file, "w", encoding="utf-8") as f:
                f.write("".join("{}={}\n".format(k, quote(v)) for k, v in env.items()))
            d = os.path.join(self.dropin_root, unit + ".d")
            os.makedirs(d, exist_ok=True)
            dropin = os.path.join(d, DROPIN)
            with open(dropin, "w", encoding="utf-8") as f:
                f.write("# start_test {}: подмена значений env-файла (EnvironmentFile ниже читается последним)\n"
                        "[Service]\nEnvironmentFile={}\n".format(sc.name, env_file))
            files += [env_file, dropin]
        if sc.env:
            self.sd.ctl("daemon-reload")
        if sc.hold_port:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", self.cfg.port(sc.hold_port)))
            sock.listen(1)

        def undo():
            if sock is not None:
                sock.close()
            for path in files:
                if os.path.exists(path):
                    os.remove(path)
            if sc.env:
                self.sd.ctl("daemon-reload")
        return undo

    # --- цикл ---

    def _order(self) -> Tuple[bool, Dict[str, float]]:
        """Готовность по уровням PLAN §5.4: все драйверы (между собой — параллельно) раньше safety_gate, он раньше
        исполнителя и т. д.; воркеры лаборатории — один уровень между rosbridge и MCP-сервером."""
        props = self.sd.show(self.units)
        t = {u: int(props.get(u, {}).get("ActiveEnterTimestampMonotonic") or 0) / 1e6 for u in self.units}
        levels: List[List[str]] = []
        kind_prev = None
        for c, u in self.cfg.plan():
            kind = c.split("@", 1)[0] if "@" in c else None
            if kind and kind == kind_prev:
                levels[-1].append(u)
            else:
                levels.append([u])
            kind_prev = kind
        ok = all(t[u] for u in self.units) and all(
            max(t[u] for u in a) <= min(t[u] for u in b) for a, b in zip(levels, levels[1:]))
        return ok, t

    def _zero_after(self, t_wall: float) -> Tuple[bool, str]:
        """Явный ноль после остановки: в журнале safety_gate строка `neurobot zero` с кодом zero_sent — нули ушли
        хотя бы одному подписчику. Отказ (ros_unavailable) и ноль без подписчиков (zero_undelivered) — не в счёт.
        (доставлен ли, последняя строка neurobot zero)."""
        if "safety" not in self.cfg.components():
            return True, ""
        entries = self.sd.journal(unit="neurobot-safety.service", since="@{}".format(int(t_wall) - 1), lines=200)
        lines = [(r, e.message) for e in entries if e.ts >= t_wall - 1
                 for r in [report.parse(e.message)] if r and r.tool == "zero"]
        return any(r.code == "zero_sent" for r, _ in lines), (lines[-1][1] if lines else "")

    def cycle(self, n: int, sc: Scenario) -> dict:
        rec = {"cycle": n, "scenario": sc.name, "passed": False, "why": ""}
        if not self.stop_all():
            rec["why"] = "не остановилось за {:g} с перед стартом".format(self.stop_timeout)
            return rec
        undo = self._sabotage(sc)
        try:
            t0 = time.monotonic()
            self.sd.ctl("start", "--no-block", TARGET)
            seen, last = [], {}
            while time.monotonic() - t0 < self.ready_timeout:
                last = self.doctor()
                root = last.get("root") or {}
                got = (root.get("component"), root.get("code"))
                if root and got not in seen and root.get("code") != "starting":
                    seen.append(got)
                if (sc.expect is None and last.get("ok")) or (sc.expect is not None and got == sc.expect):
                    rec["passed"] = True
                    rec["t_doctor_s"] = round(time.monotonic() - t0, 3)
                    break
                self.sleep(1.0)
            rec["doctor_named"] = ["{}/{}".format(*g) for g in seen]
            if not rec["passed"]:
                root = last.get("root") or {}
                rec["why"] = "doctor за {:g} с: {}/{}: {}".format(self.ready_timeout, root.get("component"),
                                                                  root.get("code"), root.get("text"))
            if sc.expect is None:
                target = self.sd.show([TARGET]).get(TARGET, {})
                enter = int(target.get("ActiveEnterTimestampMonotonic") or 0) / 1e6
                rec["t_target_s"] = round(enter - t0, 3) if enter > t0 else None
                rec["order_ok"], times = self._order()
                rec["ready_at_s"] = {u: round(t - t0, 3) for u, t in times.items() if t > t0}
                props = self.sd.show(self.units)
                rec["restarts"] = sum(int(p.get("NRestarts") or 0) for p in props.values())
                if rec["passed"] and self.gesture_cmd:
                    g = subprocess.run(self.gesture_cmd, shell=True, capture_output=True, text=True, timeout=120)
                    rec["gesture_ok"] = g.returncode == 0
                    if not rec["gesture_ok"]:
                        rec["passed"], rec["why"] = False, "жест: код {}: {}".format(g.returncode, g.stderr[-200:])
        finally:
            undo()
        t_stop = time.time()
        stopped = self.stop_all()
        if sc.expect is None:
            rec["zero_on_stop"], rec["zero_line"] = self._zero_after(t_stop)
            if rec["passed"] and not (stopped and rec["zero_on_stop"]):
                rec["passed"] = False
                if not stopped:
                    rec["why"] = "после stop: не остановилось за {:g} с".format(self.stop_timeout)
                elif rec["zero_line"]:
                    rec["why"] = "после stop: явный ноль не доставлен: " + rec["zero_line"]
                else:
                    rec["why"] = "после stop: нет явного нуля в журнале safety_gate"
        return rec


def file_hashes(root: str) -> Dict[str, str]:
    out = {}
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        for name in sorted(names):
            path = os.path.join(d, name)
            with open(path, "rb") as f:
                out[os.path.relpath(path, root)] = hashlib.sha256(f.read()).hexdigest()
    return dict(sorted(out.items()))


def run(cfg: Config, sd: Systemd, args, doctor_cmd: Sequence[str], doctor_env: Dict[str, str]) -> int:
    os.makedirs(args.out, exist_ok=True)
    gone = clear_leftovers(sd)
    if gone:
        print("start_test: убраны подмены прерванного прогона: {}".format(", ".join(gone)), file=sys.stderr)
    plan = ["ok"] * args.cycles + [b for b in args.broken] + (["ok"] if args.broken else [])
    st = StartTest(cfg, sd, doctor_cmd, args.out, args.ready_timeout, args.stop_timeout, args.gesture_cmd,
                   doctor_env=doctor_env)
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    cycles = []
    with open(os.path.join(args.out, "cycles.jsonl"), "a", encoding="utf-8") as f:
        for n, name in enumerate(plan, 1):
            rec = st.cycle(n, scenario(cfg, name))
            cycles.append(rec)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            print("start_test {}/{} {}: {}{}".format(n, len(plan), name, "ok" if rec["passed"] else "FAIL",
                                                      "" if rec["passed"] else " — " + rec["why"]),
                  file=sys.stderr, flush=True)
    summary = summarize(cycles)
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    with open(cfg.path, "rb") as f:
        env_sha = hashlib.sha256(f.read()).hexdigest()
    manifest = {
        "tool": "neurobot start-test", "version": __version__, "started": started,
        "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": platform.node(), "platform": platform.platform(), "python": platform.python_version(),
        "systemd": sd.version(), "env_file": cfg.path, "env_sha256": env_sha,
        "components": cfg.components(), "drivers": cfg.drivers(), "labworkers": cfg.labworkers(), "plan": plan,
        "args": {"cycles": args.cycles, "broken": list(args.broken), "ready_timeout": args.ready_timeout,
                 "stop_timeout": args.stop_timeout, "gesture_cmd": args.gesture_cmd},
        "doctor_cmd": " ".join(shlex.quote(a) for a in doctor_cmd),
        "deploy_files_sha256": file_hashes(os.path.dirname(os.path.abspath(__file__))),
        "deviations": list(args.deviation or []),
        "note": "время до готовности на стенде — ориентир, не результат; результат — счётчики пройденных циклов",
    }
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return 0 if all(c["passed"] for c in cycles) else 1
