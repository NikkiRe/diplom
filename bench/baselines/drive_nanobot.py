"""Драйвер nanobot 0.3.5: день daygen в сжатом времени → вызовы LLM, токены, RSS.

    .venv/bin/python -m bench.baselines.drive_nanobot --day day.jsonl --k 60 [--host sbc]

nanobot ставится в свой venv (uv, Python 3.12): на Mac — .venv-nanobot в корне проекта, в ВМ — ~/.venv-nanobot
(bench/baselines/install_nanobot.sh). Конфиг — после `nanobot onboard` (штатные умолчания 0.3.5) +
модель/канал/MCP; HOME процесса — каталог прогона (nanobot пишет и в ~/.nanobot).

Профиль `default` (B2, «как поставляется»): heartbeat 30 мин с задачей в HEARTBEAT.md (## Active Tasks),
Dream (каждые 2 ч, учитывается отдельно — класс internal:dream), idle-компакция сессий 15 мин, события
через local triggers (пуш, pushfilter). Выключено лишнее: веб-инструменты. Канал — WebSocket (как у WebUI),
`streaming: false` (влияет только на кадры канала; провайдера nanobot всё равно вызывает потоком — SSE даёт
llmtap). SSRF-фильтр nanobot блокирует MCP по адресам 127/8 и 192.168/16 — они разрешены штатным
`tools.ssrfWhitelist`.

Особенности, которые измеряются как свойства системы (не исправляются): ресурсы MCP становятся
инструментами (`mcp_<srv>_resource_<имя>`: sim-mcp — 26, hw-mcp — 11), heartbeat идёт в сессии «heartbeat»
с историей и после непустого ответа делает второй вызов — оценщик «уведомлять ли» (utils/evaluator.py);
пока нет ни одной сессии канала, кроме cli, heartbeat пропускается (cli/gateway_runtime.py).
"""
from __future__ import annotations

import argparse
import json
import queue
import secrets
import threading
import time
from pathlib import Path

from . import timecomp
from .dayplay import (ROOT, Adapter, DayPlayer, Endpoints, board_mcp_command, common_args, config_from_args,
                      print_summary, sha256_file)
from .drive_picoclaw import HEARTBEAT_TASK, deep_merge
from .executor import LocalExec

VERSION = "0.3.5"
TOKEN = "bench-local"   # статический токен WebSocket-канала (только 127.0.0.1)


class NanobotAdapter(Adapter):
    system = "nanobot"
    rules = "nanobot"
    noop_text = "All clear."            # преамбула heartbeat nanobot: «If nothing needs reporting … 'All clear.'»
    needs_broker = False

    def __init__(self, cfg, ex, run_dir):
        super().__init__(cfg, ex, run_dir)
        local = isinstance(ex, LocalExec)
        self.venv = str(ROOT / ".venv-nanobot") if local else f"{ex.home()}/.venv-nanobot"
        self.nanobot = f"{self.venv}/bin/nanobot"
        self.python = f"{self.venv}/bin/python"
        self.home = f"{self.workdir}/nb_home"
        self.cfg_path = f"{self.home}/.nanobot/config.json"
        self.workspace = f"{self.home}/.nanobot/workspace"
        self.log = f"{self.workdir}/logs/nanobot.log"
        self.frames = f"{self.workdir}/nb_frames.jsonl"
        self.proc = None
        self.bridge = None
        self.bridge_q: queue.Queue = queue.Queue()
        self.events_out: list[dict] = []
        self._replies: dict[str, dict] = {}
        self._cv = threading.Condition()
        self.trigger_id = None
        self._stopped = False
        self.trigger_via = cfg.extra.get("trigger_via", "store")

    def env(self) -> dict:
        return {"HOME": self.home, "NO_COLOR": "1", "TZ": "Europe/Moscow", "PYTHONDONTWRITEBYTECODE": "1"}

    def plan(self) -> timecomp.Plan:
        return timecomp.nanobot_plan(self.cfg.k, heartbeat_min=self.cfg.heartbeat_min)

    def prepare(self, ep: Endpoints, plan: timecomp.Plan, day) -> dict:
        ex = self.ex
        if not ex.exists(self.nanobot):
            raise FileNotFoundError(f"{self.nanobot}: запустите bench/baselines/install_nanobot.sh")
        ex.mkdir(self.home)
        r = ex.run([self.nanobot, "onboard"], env=self.env(), timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"nanobot onboard failed: {(r.stdout + r.stderr).decode()[-600:]}")
        base = json.loads(ex.read_file(self.cfg_path))
        pv = plan.values
        servers: dict = {}
        info: dict = {}
        if "sim" in self.cfg.mcp:
            servers["sim"] = {"type": "streamableHttp", "url": ep.sim_mcp_url}
        if "board" in self.cfg.mcp:
            cmd, args, binfo = board_mcp_command(ex, self.run_dir)
            servers["board"] = {"type": "stdio", "command": cmd, "args": args}
            info["hw_mcp"] = binfo
        dream = {"enabled": True}
        if pv.get("dream_cron"):
            dream["cron"] = pv["dream_cron"]
        elif pv.get("dream_interval_h"):
            dream["intervalH"] = pv["dream_interval_h"]
        whitelist = ["127.0.0.0/8"] + ([] if isinstance(ex, LocalExec) else ["192.168.5.0/24"])
        o = {
            "agents": {"defaults": {"workspace": self.workspace, "model": "gpt-5-mini", "provider": "custom",
                                    "timezone": "Europe/Moscow", "timezoneMode": "manual",
                                    "idleCompactAfterMinutes": pv["idle_compact_min"],
                                    "idleCompactCheckIntervalSeconds": pv["idle_check_s"], "dream": dream}},
            "providers": {"custom": {"apiKey": "sk-mock-not-a-key", "apiBase": ep.llm_base_url}},
            "channels": {"websocket": {"enabled": True, "host": "127.0.0.1", "port": self.cfg.port("channel"),
                                       "token": TOKEN, "streaming": False}},
            "gateway": {"host": "127.0.0.1", "port": self.cfg.port("gateway"),
                        "heartbeat": {"enabled": self.cfg.heartbeat_min > 0,
                                      "intervalS": int(pv.get("heartbeat_interval_s") or 1800)}},
            "api": {"host": "127.0.0.1", "port": self.cfg.port("api")},
            "tools": {"web": {"enable": False}, "ssrfWhitelist": whitelist, "mcpServers": servers,
                      "maxSessionMessagesPerMinute": pv["max_session_messages_per_minute"]},
        }
        cfg = deep_merge(base, o)
        if pv.get("dream_cron"):
            cfg["agents"]["defaults"]["dream"].pop("intervalH", None)
        ex.write_file(self.cfg_path, json.dumps(cfg, ensure_ascii=False, indent=1))
        hb = ("# Heartbeat Tasks\n\n## Active Tasks\n\n" + (f"- {HEARTBEAT_TASK}\n" if self.cfg.heartbeat_min > 0 else ""))
        ex.write_file(f"{self.workspace}/HEARTBEAT.md", hb)
        (self.run_dir / "nanobot_config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1),
                                                          encoding="utf-8")
        info.update({"config_path": self.cfg_path, "overlay": o, "heartbeat_md": hb, "env": self.env(),
                     "venv": self.venv, "trigger_via": self.trigger_via})
        return info

    def start(self) -> None:
        self.proc = self.ex.spawn([self.nanobot, "gateway", "--foreground"], log=self.log, env=self.env(),
                                  cwd=self.workdir)

    def _log_text(self) -> str:
        try:
            return self.ex.read_file(self.log).decode("utf-8", errors="replace")
        except (OSError, FileNotFoundError):
            return ""

    def _bridge_reader(self) -> None:
        for line in self.bridge.stdout:
            try:
                msg = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            ev = msg.get("ev")
            if ev == "reply":
                with self._cv:
                    self._replies[str(msg.get("id"))] = msg
                    self._cv.notify_all()
            elif ev == "event":
                self.events_out.append(msg)
            else:
                self.bridge_q.put(msg)

    def wait_ready(self, timeout: float) -> None:
        deadline = time.time() + timeout
        need = ["WebSocket server listening", "Agent loop started"]
        if self.cfg.mcp:
            need.append("MCP connected servers")
        while time.time() < deadline:
            txt = self._log_text()
            if all(n in txt for n in need):
                break
            if not self.ex.alive(self.proc):
                raise RuntimeError(f"nanobot exited during startup; log tail:\n{txt[-2000:]}")
            time.sleep(0.3)
        else:
            raise RuntimeError(f"nanobot not ready in {timeout}s; log tail:\n{self._log_text()[-2000:]}")
        argv = [self.python, "-m", "bench.baselines.nb_bridge", "--ws-url", f"ws://127.0.0.1:{self.cfg.port('channel')}/",
                "--token", TOKEN, "--workspace", self.workspace, "--frames", self.frames,
                "--trigger-via", self.trigger_via, "--nanobot", self.nanobot]
        if self.cfg.push == "none":
            argv.append("--no-trigger")
        env = dict(self.env(), PYTHONPATH=str(ROOT))
        self.bridge = self.ex.popen_pipe(argv, env=env, cwd=str(ROOT) if isinstance(self.ex, LocalExec) else None,
                                         stderr_log=str(self.run_dir / "logs" / "nb_bridge.err"))
        threading.Thread(target=self._bridge_reader, name="nb-bridge", daemon=True).start()
        try:
            msg = self.bridge_q.get(timeout=max(5.0, deadline - time.time()))
        except queue.Empty:
            raise RuntimeError("nb_bridge did not become ready") from None
        if msg.get("ev") != "ready":
            raise RuntimeError(f"nb_bridge: {msg}")
        self.trigger_id = msg.get("trigger_id")
        self.bridge_ready = msg

    def _cmd(self, obj: dict) -> None:
        self.bridge.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.bridge.stdin.flush()

    def root_pids(self) -> list[int]:
        return [self.proc.pid]

    def send_user(self, input_id: str, text: str, timeout: float) -> dict:
        self._cmd({"op": "user", "id": input_id, "text": text, "timeout": timeout})
        deadline = time.time() + timeout + 5
        with self._cv:
            while input_id not in self._replies:
                rest = deadline - time.time()
                if rest <= 0:
                    return {"ok": False, "reply": None, "timeout": True}
                self._cv.wait(rest)
            r = self._replies.pop(input_id)
        return {"ok": bool(r.get("ok")), "reply": r.get("text"), "bridge_ms": r.get("ms"), "frames": r.get("frames"),
                "turn_usage": r.get("usage")}

    def push_event(self, text: str) -> dict:
        self._cmd({"op": "event", "text": text})
        return {"ok": True, "queued": True}

    def stop(self) -> dict:
        if self._stopped:
            return {}
        self._stopped = True
        out: dict = {"trigger_id": self.trigger_id,
                     "events_ok": sum(1 for e in self.events_out if e.get("ok")),
                     "events_failed": sum(1 for e in self.events_out if not e.get("ok"))}
        if self.bridge is not None:
            try:
                self._cmd({"op": "quit"})
                self.bridge.wait(timeout=10)
            except Exception:  # noqa: BLE001
                self.bridge.kill()
            out["bridge_rc"] = self.bridge.returncode
        if self.proc is not None:
            out["nanobot_rc"] = self.ex.stop(self.proc, timeout=15)
        return out

    def collect(self) -> list[str]:
        got = []
        for remote, name in ((self.log, "logs/nanobot.log"), (self.frames, "nb_frames.jsonl"),
                             (f"{self.workdir}/hwmcp_trace.jsonl", "hwmcp_trace.jsonl"),
                             (f"{self.workspace}/memory", "nanobot_memory"),
                             (f"{self.workspace}/sessions", "nanobot_sessions")):
            try:
                if self.ex.fetch(remote, self.run_dir / name):
                    got.append(name)
            except OSError:
                continue
        return got

    def versions(self) -> dict:
        r = self.ex.run([self.nanobot, "--version"], env=self.env(), timeout=60)
        py = self.ex.run([self.python, "-c", "import sys,platform;print(sys.version.split()[0], platform.platform())"],
                         timeout=60)
        uv = "uv" if isinstance(self.ex, LocalExec) else f"{self.ex.home()}/.local/bin/uv"
        fr = self.ex.run([uv, "pip", "freeze", "--python", self.python], timeout=120)
        lic = self.ex.run([self.python, "-c", "import importlib.metadata as m; d=m.metadata('nanobot-ai'); "
                                              "print(d.get('License-Expression') or d.get('License') or '')"], timeout=60)
        freeze = fr.stdout.decode("utf-8", errors="replace")
        (self.run_dir / f"nanobot_freeze_{self.ex.name}.txt").write_text(freeze, encoding="utf-8")
        return {"nanobot": VERSION, "reported": r.stdout.decode("utf-8", errors="replace").strip()[-100:],
                "python": py.stdout.decode().strip(), "license": lic.stdout.decode().strip() or "MIT",
                "freeze_sha256": __import__("hashlib").sha256(freeze.encode()).hexdigest(),
                "packages": len([x for x in freeze.splitlines() if "==" in x])}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.baselines.drive_nanobot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    common_args(ap, "nanobot")
    ap.add_argument("--trigger-via", default="store", choices=("store", "cli"),
                    help="как ставить событие в очередь local triggers: store (как CLI, без процесса) | cli")
    a = ap.parse_args(argv)
    cfg = config_from_args(a, "default", {"trigger_via": a.trigger_via})
    run_dir = DayPlayer(cfg, NanobotAdapter).run()
    print_summary(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
