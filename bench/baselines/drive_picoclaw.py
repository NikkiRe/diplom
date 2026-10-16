"""Драйвер PicoClaw v0.3.1: день daygen в сжатом времени → вызовы LLM, токены, RSS.

    .venv/bin/python -m bench.baselines.drive_picoclaw --day day.jsonl --k 15 --profile best [--host sbc]

Путь без человека — штатный MQTT-канал PicoClaw (docs/channels/mqtt): реплики пользователя публикуются в
`/picoclaw/edge/user/request` ({"text": …}), ответ — `/picoclaw/edge/user/response`; события датчиков
(режим «пуш», pushfilter) — отдельным клиентом `sensors` (своя сессия PicoClaw, своя история).
MQTT-клиент PicoClaw (paho) подключается к брокеру стенда на Mac (mosquitto, иначе mqttlite).

Профили (THESIS_CONCEPT §12.7; в обоих выключено лишнее: веб-поиск, web_fetch, реестры навыков):
* `shipped` (B1-shipped) — конфиг после `picoclaw onboard` + модель/канал/MCP; heartbeat 30 мин с задачей
  наблюдения в HEARTBEAT.md; пуша нет (--push none по умолчанию);
* `best` (B1-best) — shipped + cron `command` (проверка помещения без LLM каждые 5 мин суток, вывод — в
  MQTT-клиент alerts; требует tools.exec.allow_remote) + пуш событий в MQTT + legacy как ContextManager.
  Файловые, медиа-, bus- и subagent-инструменты, не нужные сценарию датчиков, выключены штатным конфигом.
  Tool Discovery (tools.mcp.discovery) здесь выключен: скрытые инструменты MCP вызываемы только после
  поиска (TTL), а оракул mock-llm вызывает инструменты из списка запроса — включается флагом --discovery
  (тогда вызовы оракула к MCP будут отклоняться, см. README).

MCP: sim — sim-mcp (streamable HTTP; PicoClaw `type: http` отключает standalone SSE — уведомления не нужны,
клиент PicoClaw их и не обрабатывает); board — hw-mcp (stdio, фикстура sysfs, generic-каталог).
"""
from __future__ import annotations

import argparse
import json
import shlex
import threading
import time
import uuid
from pathlib import Path

from . import timecomp
from .dayplay import (ROOT, Adapter, DayPlayer, Endpoints, board_mcp_command, common_args, config_from_args,
                      http_json, print_summary, sha256_file)
from .executor import LocalExec
from .mqttlite import MQTTClient

VERSION = "0.3.1"
GIT = "2cf030d2fd3b871d7ec17e3be34c24688aac76da"
VENDOR = ROOT / "bench" / "baselines" / "vendor" / "picoclaw" / f"v{VERSION}"
AGENT_ID = "edge"
TOPIC_PREFIX = "/picoclaw"
HEARTBEAT_TASK = ("Плановая проверка помещения: оцени состояние устройств и недавние события. "
                  "Если нужно действие или уведомление пользователя — выполни его.")
# Текст промпта heartbeat PicoClaw (pkg/heartbeat/service.go buildPrompt) без строки «Current time» — для
# эмуляции heartbeat задачей cron при k, на которое штатный интервал (целые минуты ≥ 5) не делится.
HEARTBEAT_PROMPT_TMPL = """# Heartbeat Check

You are a proactive AI assistant. This is a scheduled heartbeat check.
Review the following tasks and execute any necessary actions using available skills.
If there is nothing that requires attention, respond ONLY with: HEARTBEAT_OK

{content}
"""
CHECK_SCRIPT = """#!/bin/sh
# PicoClaw-best: проверка помещения без LLM (задача cron `command`). Источник — истинное состояние sim-mcp
# (/control/state); на плате здесь было бы чтение датчиков (hw-mcp / sysfs). Вывод публикуется в канал.
S=$(curl -s -m 2 "{sim}/control/state") || exit 0
echo "$S" | grep -q '"leak_[a-z_]*":{{"wet":1}}' && echo "ALERT: протечка"
T=$(echo "$S" | sed -n 's/.*"boiler":{{[^}}]*"temperature":\\([0-9.]*\\).*/\\1/p')
[ -n "$T" ] && awk -v t="$T" 'BEGIN{{exit !(t>85)}}' && echo "ALERT: перегрев котла $T"
echo "$S" | grep -q '"door_front":{{"open":1}}' && echo "INFO: входная дверь открыта"
exit 0
"""


def binary_for(ex) -> Path:
    plat = "darwin-arm64" if isinstance(ex, LocalExec) else "linux-arm64"
    p = VENDOR / plat / "picoclaw"
    if not p.exists():
        raise FileNotFoundError(f"{p}: запустите bench/baselines/install_picoclaw.sh")
    return p


def deep_merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def overlay(profile: str, ep: Endpoints, plan: timecomp.Plan, mcp_servers: dict, workspace: str, gw_port: int,
            discovery: bool, run_id: str) -> dict:
    """Изменения поверх config.json, созданного `picoclaw onboard` (штатные умолчания v0.3.1)."""
    hb_native = plan.values.get("heartbeat_mode") == "native"
    o: dict = {
        "agents": {"defaults": {"workspace": workspace, "model_name": "bench"}},
        "model_list": [{"model_name": "bench", "provider": "openai", "model": "gpt-5-mini",
                        "api_base": ep.llm_base_url, "api_keys": ["sk-mock-not-a-key"]}],
        "channel_list": {"mqtt": {"enabled": True, "type": "mqtt", "allow_from": ["*"],
                                  "settings": {"broker": f"tcp://{ep.broker_host}:{ep.broker_port}",
                                               "agent_id": AGENT_ID, "topic_prefix": TOPIC_PREFIX,
                                               "client_id": f"picoclaw-{run_id}"[:60], "qos": 0}}},
        "tools": {"web": {"enabled": False}, "web_fetch": {"enabled": False},
                  "find_skills": {"enabled": False}, "install_skill": {"enabled": False},
                  "mcp": {"enabled": True, "servers": mcp_servers,
                          "discovery": {"enabled": bool(discovery)}}},
        "heartbeat": {"enabled": hb_native, "interval": int(plan.values.get("heartbeat_interval_min") or 30)},
        "gateway": {"host": "127.0.0.1", "port": gw_port, "log_level": "info"},
    }
    if profile == "best":
        o["agents"]["defaults"]["context_manager"] = "legacy"
        for name in ("append_file", "edit_file", "list_dir", "load_image", "read_file", "serial", "send_file",
                     "send_tts", "spawn", "spawn_status", "spi", "i2c", "subagent", "write_file"):
            o["tools"][name] = {"enabled": False}
        # cron command использует ExecTool; отключать exec вместе с лишними LLM-инструментами нельзя.
        o["tools"]["exec"] = {"enabled": True, "allow_remote": True}
        o["tools"]["cron"] = {"enabled": True, "allow_command": True}
    return o


def cron_job(job_id: str, name: str, every_s: int, message: str, channel: str, to: str,
             command: str = "") -> dict:
    now = int(time.time() * 1000)
    every_ms = int(every_s) * 1000
    payload = {"kind": "agent_turn", "message": message, "channel": channel, "to": to}
    if command:
        payload["command"] = command
    return {"id": job_id, "name": name, "enabled": True, "schedule": {"kind": "every", "everyMs": every_ms},
            "payload": payload, "state": {"nextRunAtMs": now + every_ms}, "createdAtMs": now, "updatedAtMs": now,
            "deleteAfterRun": False}


class PicoClawAdapter(Adapter):
    system = "picoclaw"
    rules = "picoclaw"
    noop_text = "HEARTBEAT_OK"          # протокол heartbeat PicoClaw: «нечего делать» = HEARTBEAT_OK
    needs_broker = True

    def __init__(self, cfg, ex, run_dir):
        super().__init__(cfg, ex, run_dir)
        self.home = f"{self.workdir}/picoclaw_home"
        self.workspace = f"{self.home}/workspace"
        self.bin = f"{self.workdir}/bin/picoclaw"
        self.log = f"{self.workdir}/logs/picoclaw.log"
        self.proc = None
        self.mqtt: MQTTClient | None = None
        self.replies: dict[str, list[tuple[float, str]]] = {}
        self._cv = threading.Condition()
        self._stopped = False
        self.discovery = bool(cfg.extra.get("discovery"))

    def env(self) -> dict:
        return {"PICOCLAW_HOME": self.home, "HOME": f"{self.workdir}/home", "NO_COLOR": "1", "TZ": "Europe/Moscow"}

    def plan(self) -> timecomp.Plan:
        check = 5.0 if self.cfg.profile == "best" else None
        return timecomp.picoclaw_plan(self.cfg.k, heartbeat_min=self.cfg.heartbeat_min, cron_check_min=check,
                                      hb_mode=self.cfg.extra.get("hb_mode", "auto"))

    def prepare(self, ep: Endpoints, plan: timecomp.Plan, day) -> dict:
        ex, wd = self.ex, self.workdir
        src = binary_for(ex)
        ex.mkdir(f"{wd}/bin")
        ex.mkdir(f"{wd}/home")
        if isinstance(ex, LocalExec):
            import shutil  # noqa: PLC0415

            shutil.copy2(src, self.bin)
        else:
            ex.run(["bash", "-c", f"cp {shlex.quote(str(src))} {shlex.quote(self.bin)} && chmod +x {shlex.quote(self.bin)}"])
        r = ex.run([self.bin, "--no-color", "onboard"], env=self.env(), input=b"n\n", timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"picoclaw onboard failed: {r.stderr.decode()[:400]}")
        cfg_path = f"{self.home}/config.json"
        base = json.loads(ex.read_file(cfg_path))
        servers = {}
        info: dict = {"binary": {"src": str(src), "sha256": sha256_file(src)}}
        if "sim" in self.cfg.mcp:
            servers["sim"] = {"enabled": True, "type": "http", "url": ep.sim_mcp_url}
        if "board" in self.cfg.mcp:
            cmd, args, binfo = board_mcp_command(ex, self.run_dir)
            servers["board"] = {"enabled": True, "type": "stdio", "command": cmd, "args": args}
            info["hw_mcp"] = binfo
        o = overlay(self.cfg.profile, ep, plan, servers, self.workspace, self.cfg.port("gateway"), self.discovery,
                    self.run_dir.name)
        cfg = deep_merge(base, o)
        cfg["model_list"] = o["model_list"]           # список моделей заменяется целиком
        ex.write_file(cfg_path, json.dumps(cfg, ensure_ascii=False, indent=2))
        hb_text = (f"# Heartbeat Check List\n\nAdd your heartbeat tasks below this line:\n- {HEARTBEAT_TASK}\n"
                   if self.cfg.heartbeat_min > 0 else "")
        mode = plan.values.get("heartbeat_mode")
        # HEARTBEAT.md: с задачей — только для штатного heartbeat; при эмуляции задачей cron штатный выключен
        ex.write_file(f"{self.workspace}/HEARTBEAT.md", hb_text if mode == "native" else
                      "# Heartbeat Check List\n\nAdd your heartbeat tasks below this line:\n")
        jobs = []
        if mode == "cron":
            jobs.append(cron_job("bench-heartbeat", "heartbeat (emulated)", plan.values["heartbeat_every_s"],
                                 HEARTBEAT_PROMPT_TMPL.format(content=hb_text), "mqtt", "mqtt:hb"))
        if self.cfg.profile == "best" and plan.values.get("cron_check_every_s"):
            sim_base = ep.sim_mcp_url.rsplit("/mcp", 1)[0]
            ex.write_file(f"{self.workspace}/check_room.sh", CHECK_SCRIPT.format(sim=sim_base), mode=0o755)
            jobs.append(cron_job("bench-roomcheck", "room check (no LLM)", plan.values["cron_check_every_s"],
                                 "room check", "mqtt", "mqtt:alerts", command="sh ./check_room.sh"))
        ex.write_file(f"{self.workspace}/cron/jobs.json", json.dumps({"version": 1, "jobs": jobs}, indent=2))
        if self.discovery:
            self.deviations.append("picoclaw.discovery: Tool Discovery включён — оракул не умеет поиск инструментов")
        info.update({"config_path": cfg_path, "overlay": o, "cron_jobs": jobs, "heartbeat_md": hb_text,
                     "env": self.env()})
        (self.run_dir / "picoclaw_config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                                           encoding="utf-8")
        return info

    # ----------------------------------------------------------------- MQTT

    def _on_message(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        client = parts[-2] if len(parts) >= 2 else "?"
        try:
            text = json.loads(payload.decode("utf-8")).get("text", "")
        except (ValueError, UnicodeDecodeError):
            text = payload.decode("utf-8", errors="replace")
        with self._cv:
            self.replies.setdefault(client, []).append((time.time(), text))
            self._cv.notify_all()
        with open(self.run_dir / "mqtt_responses.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts_wall": time.time(), "client": client, "text": text}, ensure_ascii=False) + "\n")

    def start(self) -> None:
        self.mqtt = MQTTClient("127.0.0.1", self.cfg.port("broker"), f"bench-driver-{uuid.uuid4().hex[:6]}",
                               keepalive=30, on_message=self._on_message)
        self.mqtt.connect()
        self.mqtt.subscribe(f"{TOPIC_PREFIX}/{AGENT_ID}/+/response")
        self.proc = self.ex.spawn([self.bin, "--no-color", "gateway"], log=self.log, env=self.env(),
                                  cwd=self.workdir)

    def _log_text(self) -> str:
        try:
            return self.ex.read_file(self.log).decode("utf-8", errors="replace")
        except (OSError, FileNotFoundError):
            return ""

    def wait_ready(self, timeout: float) -> None:
        deadline = time.time() + timeout
        need = ["MQTT channel started"]
        if self.cfg.mcp:
            need.append("MCP server initialization complete")
        while time.time() < deadline:
            txt = self._log_text()
            if all(n in txt for n in need):
                return
            if not self.ex.alive(self.proc):
                raise RuntimeError(f"picoclaw exited during startup; log tail:\n{txt[-2000:]}")
            time.sleep(0.3)
        raise RuntimeError(f"picoclaw not ready in {timeout}s; log tail:\n{self._log_text()[-2000:]}")

    def root_pids(self) -> list[int]:
        return [self.proc.pid]

    def send_user(self, input_id: str, text: str, timeout: float) -> dict:
        with self._cv:
            n0 = len(self.replies.get("user", []))
        t0 = time.time()
        self.mqtt.publish(f"{TOPIC_PREFIX}/{AGENT_ID}/user/request", json.dumps({"text": text}, ensure_ascii=False))
        deadline = t0 + timeout
        with self._cv:
            while len(self.replies.get("user", [])) <= n0:
                rest = deadline - time.time()
                if rest <= 0:
                    return {"ok": False, "reply": None, "timeout": True}
                self._cv.wait(rest)
            ts, reply = self.replies["user"][n0]
        return {"ok": True, "reply": reply, "reply_wall_ms": round((ts - t0) * 1000, 2)}

    def push_event(self, text: str) -> dict:
        self.mqtt.publish(f"{TOPIC_PREFIX}/{AGENT_ID}/sensors/request", json.dumps({"text": text}, ensure_ascii=False))
        return {"ok": True}

    def stop(self) -> dict:
        if self._stopped:
            return {}
        self._stopped = True
        out = {}
        if self.proc is not None:
            out["picoclaw_rc"] = self.ex.stop(self.proc, timeout=10)
        if self.mqtt is not None:
            out["mqtt_driver_bytes"] = {"in": self.mqtt.bytes_in, "out": self.mqtt.bytes_out}
            self.mqtt.close()
        with self._cv:
            out["mqtt_responses"] = {k: len(v) for k, v in self.replies.items()}
        return out

    def collect(self) -> list[str]:
        got = []
        for remote, name in ((self.log, "logs/picoclaw.log"), (f"{self.workspace}/heartbeat.log", "logs/heartbeat.log"),
                             (f"{self.workspace}/cron/jobs.json", "picoclaw_cron_jobs_final.json"),
                             (f"{self.workdir}/hwmcp_trace.jsonl", "hwmcp_trace.jsonl")):
            if self.ex.fetch(remote, self.run_dir / name):
                got.append(name)
        if not isinstance(self.ex, LocalExec):
            if self.ex.fetch(f"{self.workspace}/sessions", self.run_dir / "picoclaw_sessions"):
                got.append("picoclaw_sessions/")
        return got

    def versions(self) -> dict:
        src = binary_for(self.ex)
        r = self.ex.run([self.bin, "--no-color", "version"], env=self.env(), timeout=30)
        out = r.stdout.decode("utf-8", errors="replace")
        line = next((ln.strip() for ln in out.splitlines() if "picoclaw" in ln and "git" in ln), out.strip()[-200:])
        lic = VENDOR / ("darwin-arm64" if isinstance(self.ex, LocalExec) else "linux-arm64") / "LICENSE"
        return {"picoclaw": VERSION, "git": GIT, "reported": line, "binary_sha256": sha256_file(src),
                "license": "MIT", "license_sha256": sha256_file(lic) if lic.exists() else None,
                "release_asset": json.loads((VENDOR / "versions.json").read_text(encoding="utf-8"))
                if (VENDOR / "versions.json").exists() else None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.baselines.drive_picoclaw", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    common_args(ap, "picoclaw")
    ap.add_argument("--profile", default="best", choices=("shipped", "best"))
    ap.add_argument("--hb-mode", default="auto", choices=("auto", "native", "cron"),
                    help="heartbeat: штатный (целые минуты ≥ 5) или эмуляция задачей cron; auto — по k")
    ap.add_argument("--discovery", action="store_true", help="включить Tool Discovery (оракул его не поддерживает)")
    a = ap.parse_args(argv)
    if a.profile == "shipped" and "--push" not in (argv or __import__("sys").argv):
        a.push = "none"
    cfg = config_from_args(a, a.profile, {"hb_mode": a.hb_mode, "discovery": a.discovery})
    run_dir = DayPlayer(cfg, PicoClawAdapter).run()
    print_summary(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
