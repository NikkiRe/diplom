"""Проигрывание файла дня в сжатом времени для аналогов (общая часть drive_picoclaw / drive_nanobot).

Интерфейс как у реплеера (bench/replay): день JSONL (daygen) → ожидания оракулу mock-llm → часы sim-mcp →
реплики через штатный канал системы и события (пуш) → сбор журналов в <out>/<run_id>/ и manifest.json.
Отличие от реплеера: у аналогов нет виртуальных часов, поэтому день идёт в реальном времени, сжатом в k раз
(timecomp.TimeMap); событие с моментом t подаётся в стенной момент wall0 + (t − t0)/k.

Порядок для события дня с моментом t (как в bench/replay):
  situation  — POST mock /control/expect {situation} в t − ε (до датчиков момента t); в t + deadline_s —
               /control/expire; агенту ничего не подаётся;
  sensor     — POST sim /control/clock {t} (sim-mcp применяет события ≤ t), затем фильтр пуша → канал системы;
  user_cmd / question — в потоке пользователя (реплики строго по одной, пользователь ждёт ответа):
               /control/expect → sim /control/input {id} → реплика в канал → ответ;
  тик        — каждые tick_day_s секунд дня и в моменты концов эпизодов: часы sim-mcp, истечения ожиданий,
               закрытие эпизодов фильтра пуша.
Вызовы LLM, токены и байты берутся из журнала mock-llm и llmtap (прокси между агентом и mock-llm).

Порты (base = --port-base; диапазон аналогов 18120–18139): +0 mock-llm, +1 sim-mcp, +2 llmtap, +3 шлюз агента
(health), +4 канал агента (WebSocket nanobot), +5 API агента, +6 MQTT-брокер (только PicoClaw).
По умолчанию PicoClaw — 18120–18126, nanobot — 18127–18133 (можно гонять параллельно), тесты — 18134–18136.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import timecomp
from .executor import LocalExec, make_executor
from .llmtap import RULES, TapServer
from .procmon import PsutilSampler
from .pushfilter import MODES as PUSH_MODES
from .pushfilter import PushFilter, format_event

ROOT = Path(__file__).resolve().parents[2]
VENV_PY = ROOT / ".venv" / "bin" / "python"
USER_KINDS = ("user_cmd", "question")
SITUATION_EPS_S = 0.001
PORT_OFFSETS = {"mock": 0, "sim": 1, "tap": 2, "gateway": 3, "channel": 4, "api": 5, "broker": 6}


def load_day(path: str | Path):
    """Тот же разбор, что у реплеера (bench/replay/dayfile.py)."""
    from bench.replay.dayfile import load_day as _load  # noqa: PLC0415

    return _load(path)


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def http_json(method: str, url: str, obj: Any = None, timeout: float = 10.0) -> tuple[int, Any]:
    data = None if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        return status, raw.decode("utf-8", errors="replace")


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def wait_http(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            st, _ = http_json("GET", url, timeout=2)
            if st == 200:
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


@dataclass
class Endpoints:
    """Адреса сервисов стенда так, как их видит агент (на Mac — 127.0.0.1, в ВМ — host.lima.internal)."""

    service_host: str
    llm_base_url: str
    sim_mcp_url: str
    broker_host: str | None = None
    broker_port: int | None = None
    ports: dict[str, int] = field(default_factory=dict)


@dataclass
class PlayConfig:
    day: str
    k: float = 15.0
    out: str = str(ROOT / "bench" / "baselines" / "runs")
    run_id: str | None = None
    host: str = "mac"
    port_base: int = 18120
    profile: str = "default"
    push: str = "signals"
    gate_config: str | None = None
    heartbeat_min: float = 30.0
    cache_ttl_s: float = 300.0
    latency: str = "0"
    mcp: tuple[str, ...] = ("sim", "board")
    sim_catalog: str = "generic"
    sample_interval_s: float = 0.5
    user_timeout_s: float = 30.0
    warmup_s: float = 3.0
    drain_s: float = 5.0
    tick_day_s: float = 60.0
    window_end: float | None = None
    hints: bool = True
    summary_tokens: int = 400
    fit_summary: bool = True
    force: bool = False
    extra: dict = field(default_factory=dict)

    def port(self, name: str) -> int:
        return self.port_base + PORT_OFFSETS[name]


class Adapter:
    """Аналог под управлением драйвера. Реализации — drive_picoclaw.PicoClawAdapter, drive_nanobot.NanobotAdapter."""

    system = "abstract"
    rules = "generic"            # набор правил llmtap
    noop_text = ""               # «ничего не делать» по протоколу системы (ответ оракула на пустой фон)
    needs_broker = False

    def __init__(self, cfg: PlayConfig, ex, run_dir: Path):
        self.cfg, self.ex, self.run_dir = cfg, ex, run_dir
        self.workdir = ex.workdir(run_dir)
        self.deviations: list[str] = []

    def plan(self) -> timecomp.Plan:
        raise NotImplementedError

    def prepare(self, ep: Endpoints, plan: timecomp.Plan, day) -> dict:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def wait_ready(self, timeout: float) -> None:
        raise NotImplementedError

    def root_pids(self) -> list[int]:
        raise NotImplementedError

    def send_user(self, input_id: str, text: str, timeout: float) -> dict:
        raise NotImplementedError

    def push_event(self, text: str) -> dict:
        raise NotImplementedError

    def stop(self) -> dict:
        raise NotImplementedError

    def collect(self) -> list[str]:
        return []

    def versions(self) -> dict:
        return {}


class Services:
    """mock-llm, sim-mcp, llmtap и (для PicoClaw) MQTT-брокер на Mac — свои процессы, останавливаются по PID."""

    def __init__(self, cfg: PlayConfig, run_dir: Path, day_path: Path, noop_text: str, rules: str,
                 mock_plan: timecomp.Plan, need_broker: bool, bind_host: str = "127.0.0.1"):
        self.cfg, self.run_dir, self.day_path = cfg, run_dir, day_path
        self.local = LocalExec()
        self.noop_text, self.rules, self.mock_plan = noop_text, rules, mock_plan
        self.need_broker = need_broker
        self.bind_host = bind_host
        self.tap: TapServer | None = None
        self.broker_thread = None
        self.procs: dict[str, Any] = {}
        self.info: dict[str, Any] = {}

    @property
    def mock_url(self) -> str:
        return f"http://127.0.0.1:{self.cfg.port('mock')}"

    @property
    def sim_url(self) -> str:
        return f"http://127.0.0.1:{self.cfg.port('sim')}"

    def start(self) -> None:
        busy = [n for n in ("mock", "sim", "tap") + (("broker",) if self.need_broker else ())
                if not port_free(self.cfg.port(n))]
        if busy:
            raise RuntimeError(f"ports busy: {[(n, self.cfg.port(n)) for n in busy]} (диапазон аналогов 18120–18139)")
        mp = self.mock_plan.values
        argv = [str(VENV_PY), "-m", "bench.mockllm", "--mode", "oracle", "--host", "127.0.0.1",
                "--port", str(self.cfg.port("mock")), "--clock", "wall", "--cache-ttl", str(mp["cache_ttl_s"]),
                "--latency", mp["latency"], "--noop-text", self.noop_text,
                "--summary-tokens", str(self.cfg.summary_tokens),
                "--journal", str(self.run_dir / "mockllm_journal_full.jsonl")]
        self.procs["mock"] = self.local.spawn(argv, log=str(self.run_dir / "logs" / "mockllm.log"), cwd=str(ROOT),
                                              env={"PYTHONDONTWRITEBYTECODE": "1"})
        self.info["mock_argv"] = argv
        argv = [str(VENV_PY), "-m", "bench.simmcp", "--day", str(self.day_path), "--host", "127.0.0.1",
                "--port", str(self.cfg.port("sim")), "--catalog", self.cfg.sim_catalog,
                "--journal", str(self.run_dir / "simmcp_journal.jsonl"),
                "--report", str(self.run_dir / "simmcp_report_final.json"), "--run-id", self.run_dir.name]
        self.procs["sim"] = self.local.spawn(argv, log=str(self.run_dir / "logs" / "simmcp.log"), cwd=str(ROOT),
                                             env={"PYTHONDONTWRITEBYTECODE": "1"})
        self.info["sim_argv"] = argv
        if not wait_http(self.mock_url + "/healthz", 30):
            raise RuntimeError("mock-llm did not start (logs/mockllm.log)")
        if not wait_http(self.sim_url + "/healthz", 30):
            raise RuntimeError("sim-mcp did not start (logs/simmcp.log)")
        self.tap = TapServer(self.mock_url, RULES[self.rules], host=self.bind_host, port=self.cfg.port("tap"),
                             journal=str(self.run_dir / "llmtap.jsonl"), hints=self.cfg.hints,
                             fit_summary=self.cfg.fit_summary).start()
        if self.need_broker:
            mosq = shutil.which("mosquitto")
            if mosq:
                conf = self.run_dir / "logs" / "mosquitto.conf"
                conf.parent.mkdir(parents=True, exist_ok=True)
                conf.write_text(f"listener {self.cfg.port('broker')} {self.bind_host}\nallow_anonymous true\n"
                                "persistence false\n", encoding="utf-8")
                self.procs["broker"] = self.local.spawn([mosq, "-c", str(conf)],
                                                        log=str(self.run_dir / "logs" / "mosquitto.log"))
                self.info["broker"] = {"impl": "mosquitto", "path": mosq,
                                       "version": _cmd_out([mosq, "-h"]).splitlines()[0:1]}
            else:
                from .mqttlite import BrokerThread  # noqa: PLC0415

                self.broker_thread = BrokerThread(self.bind_host, self.cfg.port("broker")).__enter__()
                self.info["broker"] = {"impl": "mqttlite"}
            deadline = time.time() + 10
            while port_free(self.cfg.port("broker")) and time.time() < deadline:
                time.sleep(0.1)

    def reset_mock(self) -> int:
        st, r = http_json("POST", self.mock_url + "/control/reset", {"journal": True})
        if st != 200:
            raise RuntimeError(f"mock-llm reset failed: {st} {r}")
        return int((r or {}).get("journal_seq") or 0)

    def stop(self) -> None:
        if self.tap is not None:
            self.tap.stop()
            self.tap = None
        if self.broker_thread is not None:
            self.broker_thread.stop()
            self.broker_thread = None
        for name in ("broker", "sim", "mock"):
            p = self.procs.pop(name, None)
            if p is not None:
                self.local.stop(p)


def _cmd_out(argv: list[str]) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, timeout=10)
        return (r.stdout + r.stderr).decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""


class DayPlayer:
    def __init__(self, cfg: PlayConfig, adapter_cls: type[Adapter]):
        self.cfg = cfg
        self.day = load_day(cfg.day)
        rid = cfg.run_id or f"{adapter_cls.system}-{Path(cfg.day).stem}-k{cfg.k:g}-{time.strftime('%Y%m%d-%H%M%S')}"
        self.run_dir = (Path(cfg.out) / rid).resolve()        # абсолютный: агент запускается с cwd = workdir
        if self.run_dir.exists() and any(self.run_dir.iterdir()) and not cfg.force:
            raise RuntimeError(f"run directory exists and is not empty: {self.run_dir} (--force)")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "logs").mkdir(exist_ok=True)
        self.ex = make_executor(cfg.host)
        self.adapter = adapter_cls(cfg, self.ex, self.run_dir)
        self.log_f = open(self.run_dir / "play.jsonl", "w", encoding="utf-8")
        self._log_lock = threading.Lock()
        self.tm: timecomp.TimeMap | None = None
        self.sim_t = None
        self.stats: dict[str, Any] = {"pushes": 0, "push_errors": 0, "user_sent": 0, "user_ok": 0, "user_timeouts": 0,
                                      "situations": 0, "expired": 0, "sensors": 0, "ticks": 0, "sim_clock_errors": 0,
                                      "max_lag_wall_s": 0.0}
        self.user_q: queue.Queue = queue.Queue()
        self.user_pending: list[float] = []      # моменты реплик, поставленных в очередь, но ещё не отправленных
        self._upl = threading.Lock()
        self._simlock = threading.Lock()       # sim_clock вызывают основной поток и поток пользователя
        self.user_records: list[dict] = []
        self.expiries: list[tuple[float, str]] = []
        self.errors: list[str] = []

    # ---------------------------------------------------------------- журнал

    def log(self, typ: str, **kw) -> None:
        rec = {"ts_wall": time.time(), "type": typ}
        if self.tm is not None:
            rec["t_day"] = round(self.tm.day(rec["ts_wall"]), 3)
        rec.update(kw)
        with self._log_lock:
            self.log_f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            self.log_f.flush()

    # ---------------------------------------------------------------- шаги

    def sim_clock(self, t: float) -> None:
        with self._simlock:
            self._sim_clock(t)

    def _sim_clock(self, t: float) -> None:
        # Часы sim-mcp не обгоняют реплику, которая ждёт отправки (пользователь ещё ждёт прошлого ответа):
        # иначе sim-mcp откроет окно следующей реплики по часам и припишет ей вызовы текущего хода.
        with self._upl:
            hold = min(self.user_pending) if self.user_pending else None
        if hold is not None and t >= hold:
            self.stats["sim_clock_held"] = self.stats.get("sim_clock_held", 0) + 1
            t = hold - SITUATION_EPS_S
        if self.sim_t is not None and t <= self.sim_t:
            return
        try:
            st, r = http_json("POST", self.services.sim_url + "/control/clock", {"t": t})
            if st != 200:
                self.stats["sim_clock_errors"] += 1
                self.log("sim_clock_error", t=t, status=st, body=r)
            self.sim_t = t
        except OSError as e:
            self.stats["sim_clock_errors"] += 1
            self.log("sim_clock_error", t=t, error=str(e))

    def wait_until(self, t: float) -> float:
        lag = self.tm.sleep_until(t)
        if lag > self.stats["max_lag_wall_s"]:
            self.stats["max_lag_wall_s"] = round(lag, 4)
        return lag

    def push(self, evs) -> None:
        for ev in evs:
            text = format_event(ev)
            t0 = time.time()
            try:
                r = self.adapter.push_event(text)
                ok = bool(r.get("ok", True)) if isinstance(r, dict) else True
            except Exception as e:  # noqa: BLE001
                ok, r = False, {"error": repr(e)}
            self.stats["pushes"] += 1
            if not ok:
                self.stats["push_errors"] += 1
            self.log("push", ev=ev.as_dict(), text=text, ok=ok, ms=round((time.time() - t0) * 1000, 2), result=r)

    def tick(self, t: float) -> None:
        self.wait_until(t)
        self.stats["ticks"] += 1
        keep = []
        for te, iid in self.expiries:
            if te <= t:
                http_json("POST", self.services.mock_url + "/control/expire", {"input_id": iid})
                self.stats["expired"] += 1
                self.log("expire", input_id=iid, deadline_t=te)
            else:
                keep.append((te, iid))
        self.expiries = keep
        self.push(self.pf.tick(t))
        self.sim_clock(t)

    def situations(self, group) -> None:
        t = group[0].t
        self.wait_until(max(self.tm.t0, t - SITUATION_EPS_S))
        for e in group:
            raw = e.raw
            situation = {k: raw[k] for k in ("id", "name", "significant", "deadline_s", "t") if k in raw}
            payload = {"input_id": e.id, "kind": "situation", "t": e.t,
                       "expect": dict(raw.get("expect") or {"type": "notify_or_action"}), "situation": situation}
            st, r = http_json("POST", self.services.mock_url + "/control/expect", payload)
            self.stats["situations"] += 1
            self.log("expect", input_id=e.id, kind="situation", name=raw.get("name"), status=st)
            if raw.get("deadline_s") is not None:
                self.expiries.append((e.t + float(raw["deadline_s"]), e.id))
        self.expiries.sort()

    def sensor(self, e) -> None:
        self.wait_until(e.t)
        self.stats["sensors"] += 1
        self.sim_clock(e.t)
        raw = e.raw
        self.push(self.pf.on_sensor(e.t, raw.get("device"), raw.get("property"), raw.get("value"), raw.get("source", "")))

    def user_worker(self) -> None:
        while True:
            e = self.user_q.get()
            if e is None:
                return
            raw = e.raw
            if e.kind == "question":
                expect = dict(raw.get("expect") or {})
                expect.setdefault("type", "answer")
                if "answer" in raw:
                    expect.setdefault("answer", raw["answer"])
            else:
                expect = dict(raw.get("expect") or {"type": "action", "calls": []})
            payload = {"input_id": e.id, "kind": e.kind, "text": raw.get("text"), "t": e.t, "expect": expect}
            if raw.get("k") is not None:
                payload["k"] = raw["k"]
            http_json("POST", self.services.mock_url + "/control/expect", payload)
            with self._upl:
                if e.t in self.user_pending:
                    self.user_pending.remove(e.t)
            self.sim_clock(e.t)                 # часы — ровно до реплики (если их держали)
            http_json("POST", self.services.sim_url + "/control/input", {"id": e.id})
            t_send = self.tm.day()
            self.stats["user_sent"] += 1
            t0 = time.time()
            try:
                r = self.adapter.send_user(e.id, raw.get("text", ""), self.cfg.user_timeout_s)
            except Exception as ex:  # noqa: BLE001
                r = {"ok": False, "error": repr(ex)}
            rtt = time.time() - t0
            ok = bool(r.get("ok"))
            self.stats["user_ok" if ok else "user_timeouts"] += 1
            rec = {"input_id": e.id, "kind": e.kind, "t_event": e.t, "t_sent": round(t_send, 3),
                   "lag_day_s": round(t_send - e.t, 3), "rtt_wall_ms": round(rtt * 1000, 2),
                   "rtt_day_s": round(rtt * self.cfg.k, 3), "ok": ok, "reply": (r.get("reply") or "")[:300],
                   "extra": {k: v for k, v in r.items() if k not in ("ok", "reply")}}
            self.user_records.append(rec)
            self.log("user", **rec)

    # ---------------------------------------------------------------- прогон

    def run(self) -> Path:
        cfg = self.cfg
        day = self.day
        if threading.current_thread() is threading.main_thread():
            # SIGTERM → KeyboardInterrupt: finally останавливает агента и сервисы по PID (а не бросает их)
            import signal  # noqa: PLC0415

            def _term(*_a):
                raise KeyboardInterrupt("SIGTERM")

            signal.signal(signal.SIGTERM, _term)
        if cfg.push not in PUSH_MODES:
            raise ValueError(f"--push must be one of {PUSH_MODES}")
        start_t, end_t = day.span()
        if cfg.window_end is not None:
            end_t = min(end_t, cfg.window_end)
        plan = self.adapter.plan()
        mplan = timecomp.mock_plan(cfg.k, cache_ttl_s=cfg.cache_ttl_s, latency=cfg.latency)
        day_copy = self.run_dir / "day.jsonl"
        shutil.copy2(cfg.day, day_copy)
        self.services = Services(cfg, self.run_dir, day_copy, self.adapter.noop_text, self.adapter.rules, mplan,
                                 self.adapter.needs_broker)
        manifest: dict[str, Any] = {
            "manifest_version": 1, "dev_run": True, "run_id": self.run_dir.name, "system": self.adapter.system,
            "profile": cfg.profile, "config": dataclasses.asdict(cfg), "k": cfg.k,
            "day": {"path": str(cfg.day), "sha256": day.sha256,
                    "header": {k: day.header.get(k) for k in ("seed", "profile", "home", "date", "split", "window",
                                                              "window_s", "lang", "counts", "format")},
                    "span": [start_t, end_t]},
            "timecomp": {"system": plan.as_dict(), "mock": mplan.as_dict(),
                         "metric_validity": timecomp.METRIC_VALIDITY},
            "host": {"executor": self.ex.name, "uname": self.ex.uname(), "driver_python": sys.version},
            "errors": self.errors, "warnings": [],
        }
        sampler = None
        remote_sampler = None
        try:
            self.services.start()
            manifest["services"] = self.services.info
            since = self.services.reset_mock()
            manifest["mock_journal_since"] = since
            sh = self.ex.service_host
            ep = Endpoints(service_host=sh,
                           llm_base_url=f"http://{sh}:{cfg.port('tap')}/v1",
                           sim_mcp_url=f"http://{sh}:{cfg.port('sim')}/mcp",
                           broker_host=sh if self.adapter.needs_broker else None,
                           broker_port=cfg.port("broker") if self.adapter.needs_broker else None,
                           ports={n: cfg.port(n) for n in PORT_OFFSETS})
            manifest["endpoints"] = dataclasses.asdict(ep)
            manifest["adapter"] = self.adapter.prepare(ep, plan, day)
            manifest["versions"] = self.adapter.versions()
            wall_start = time.time()
            self.adapter.start()
            self.adapter.wait_ready(120)
            manifest["agent_ready_s"] = round(time.time() - wall_start, 3)
            pids = self.adapter.root_pids()
            if isinstance(self.ex, LocalExec):
                sampler = PsutilSampler(pids, self.run_dir / "procmon.jsonl", cfg.sample_interval_s).start()
            else:
                remote_sampler = self._start_remote_sysmon(pids)
            time.sleep(cfg.warmup_s)
            self.pf = PushFilter.from_gate_config(cfg.gate_config, cfg.push, day.header.get("initial_state") or {})
            manifest["push"] = {"mode": cfg.push, "gate_config": str(cfg.gate_config or "bench/simmcp/edgectx_simday.yaml"),
                                "signals": [dataclasses.asdict(s) for s in self.pf.signals], "default": self.pf.default}
            self.tm = timecomp.TimeMap(cfg.k, start_t)
            manifest["wall0"] = self.tm.wall0
            manifest["t0"] = start_t
            manifest["t_end"] = end_t
            manifest["wall_end_planned"] = self.tm.wall(end_t)
            self.log("start", run_id=self.run_dir.name, span=[start_t, end_t], k=cfg.k, counts=day.counts())
            self.sim_clock(start_t)
            worker = threading.Thread(target=self.user_worker, name="user", daemon=True)
            worker.start()
            self._play(start_t, end_t)
            self.user_q.put(None)
            worker.join(timeout=cfg.user_timeout_s * 2 + 5)
            manifest["wall_end"] = time.time()
            self._drain(cfg.drain_s)
            st, rep = http_json("POST", self.services.sim_url + "/control/finish", {})
            (self.run_dir / "simmcp_report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                                             encoding="utf-8")
            manifest["agent_stop"] = self.adapter.stop()
        except Exception as e:
            self.errors.append(repr(e))
            self.log("error", error=repr(e))
            raise
        finally:
            try:
                manifest["agent_stop"] = manifest.get("agent_stop") or self.adapter.stop()
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"adapter.stop: {e!r}")
            if sampler is not None:
                sampler.stop()
            if remote_sampler is not None:
                self._stop_remote_sysmon(remote_sampler)
            try:
                manifest["collected"] = self.adapter.collect()
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"adapter.collect: {e!r}")
            try:
                wd = self.ex.workdir(self.run_dir)
                self.ex.cleanup(wd)
                if not isinstance(self.ex, LocalExec):
                    if self.ex.fetch(wd, self.run_dir / "agent"):
                        manifest.setdefault("collected", []).append("agent/ (рабочий каталог из ВМ)")
                    if not cfg.extra.get("keep_remote"):
                        self.ex.run(["rm", "-rf", wd])
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"cleanup: {e!r}")
            self._fetch_mock_journal(manifest.get("mock_journal_since", 0))
            self.services.stop()
            manifest["stats"] = self.stats
            manifest["deviations"] = plan.deviations + mplan.deviations + self.adapter.deviations
            manifest["finished_wall"] = time.time()
            manifest["files"] = {p.name: {"sha256": sha256_file(p), "bytes": p.stat().st_size}
                                 for p in sorted(self.run_dir.iterdir()) if p.is_file() and p.name != "manifest.json"}
            (self.run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1,
                                                                   default=str), encoding="utf-8")
            self.log("end", stats=self.stats)
            self.log_f.close()
        from .summary import summarize, write_summary  # noqa: PLC0415

        write_summary(self.run_dir, summarize(self.run_dir))
        return self.run_dir

    def _play(self, start_t: float, end_t: float) -> None:
        events = [e for e in self.day.events if e.t <= end_t]
        next_tick = start_t
        i = 0

        def advance_ticks(limit: float, inclusive: bool) -> None:
            nonlocal next_tick
            while True:
                cands = [next_tick]
                d = self.pf.next_deadline()
                if d is not None:
                    cands.append(d)
                if self.expiries:
                    cands.append(self.expiries[0][0])
                c = min(cands)
                if c > limit or (c == limit and not inclusive):
                    return
                self.tick(c)
                if c >= next_tick:
                    next_tick += self.cfg.tick_day_s

        while i < len(events):
            e = events[i]
            advance_ticks(e.t, inclusive=False)
            if e.kind == "situation":
                j = i
                while j < len(events) and events[j].kind == "situation" and events[j].t == e.t:
                    j += 1
                self.situations(events[i:j])
                i = j
                continue
            i += 1
            if e.kind in USER_KINDS:
                self.wait_until(e.t)
                with self._upl:
                    self.user_pending.append(e.t)
                self.user_q.put(e)
            elif e.kind == "sensor":
                self.sensor(e)
            else:
                self.wait_until(e.t)
                self.sim_clock(e.t)
        advance_ticks(end_t, inclusive=True)
        self.wait_until(end_t)
        self.sim_clock(end_t)

    def _drain(self, max_s: float) -> None:
        """Дождаться тишины llmtap (нет запросов в полёте ≥ 1 с), не дольше max_s стенных секунд."""
        deadline = time.time() + max_s
        while time.time() < deadline:
            st = self.services.tap.tap.state()
            if st["inflight"] == 0 and st["idle_ms"] >= 1000:
                return
            time.sleep(0.1)

    def _fetch_mock_journal(self, since: int) -> None:
        try:
            req = urllib.request.Request(f"{self.services.mock_url}/control/journal?since={since}")
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            (self.run_dir / "mockllm_journal.jsonl").write_bytes(data)
            st, state = http_json("GET", self.services.mock_url + "/control/state")
            (self.run_dir / "mockllm_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=1),
                                                             encoding="utf-8")
        except OSError as e:
            self.errors.append(f"mock journal: {e!r}")

    # ---------------------------------------------------------------- sysmon в ВМ

    def _start_remote_sysmon(self, pids: list[int]):
        binp = ROOT / "sysmon" / "bin" / "sysmon-linux-arm64"
        if not binp.exists():
            self.errors.append("sysmon-linux-arm64 not found; памяти в ВМ нет")
            return None
        out = f"{self.ex.workdir(self.run_dir)}/sysmon.jsonl"
        argv = [str(binp), "--interval", f"{max(1, int(round(self.cfg.sample_interval_s)))}s", "--out", out]
        for p in pids:
            argv += ["--pid", str(p)]
        proc = self.ex.spawn(argv, log=f"{self.ex.workdir(self.run_dir)}/logs/sysmon.log")
        return (proc, out)

    def _stop_remote_sysmon(self, handle) -> None:
        if not handle:
            return
        proc, out = handle
        self.ex.stop(proc)
        self.ex.fetch(out, self.run_dir / "sysmon.jsonl")


def common_args(ap: argparse.ArgumentParser, system: str) -> None:
    ap.add_argument("--day", required=True, help="файл дня daygen (JSONL)")
    ap.add_argument("--k", type=float, default=15.0, help="сжатие времени: день идёт в k раз быстрее (15 → 16 ч за 64 мин)")
    ap.add_argument("--out", default=str(ROOT / "bench" / "baselines" / "runs"), help="корень результатов")
    ap.add_argument("--run-id")
    ap.add_argument("--host", default="mac", help="mac | sbc (Lima VM) — где запускается аналог")
    ap.add_argument("--port-base", type=int, default=18120 if system == "picoclaw" else 18127)
    ap.add_argument("--push", default="signals", choices=PUSH_MODES, help="события в канал: signals | all | none")
    ap.add_argument("--gate-config", help="YAML edgectx с policy.gate.signals для фильтра пуша")
    ap.add_argument("--heartbeat-min", type=float, default=30.0, help="период heartbeat в сутках, мин (0 — выкл.)")
    ap.add_argument("--cache-ttl", type=float, default=300.0, help="TTL кэша провайдера в сутках, с (делится на k)")
    ap.add_argument("--latency", default="0", help="задержка mock-llm в сутках (делится на k): fixed:800 и т. п.")
    ap.add_argument("--mcp", default="sim,board", help="MCP-серверы: sim (sim-mcp HTTP), board (hw-mcp stdio)")
    ap.add_argument("--sim-catalog", default="generic", choices=("generic", "explicit"))
    ap.add_argument("--sample-interval", type=float, default=0.5, help="период сэмплера памяти/CPU, с стенных")
    ap.add_argument("--user-timeout", type=float, default=30.0, help="ожидание ответа на реплику, с стенных")
    ap.add_argument("--warmup", type=float, default=3.0, help="пауза после готовности агента до начала дня, с")
    ap.add_argument("--drain", type=float, default=5.0)
    ap.add_argument("--window-end", type=float, help="остановить день раньше (с от полуночи)")
    ap.add_argument("--no-hints", action="store_true", help="не передавать оракулу X-Bench-Wake/Purpose")
    ap.add_argument("--summary-tokens", type=int, default=400, help="длина сводки оракула (mock-llm), токенов")
    ap.add_argument("--no-fit-summary", action="store_true",
                    help="не укорачивать сводку до цели запроса (Target length) — см. llmtap")
    ap.add_argument("--force", action="store_true")


def config_from_args(a: argparse.Namespace, profile: str, extra: dict | None = None) -> PlayConfig:
    return PlayConfig(day=a.day, k=a.k, out=a.out, run_id=a.run_id, host=a.host, port_base=a.port_base,
                      profile=profile, push=a.push, gate_config=a.gate_config, heartbeat_min=a.heartbeat_min,
                      cache_ttl_s=a.cache_ttl, latency=a.latency,
                      mcp=tuple(x.strip() for x in a.mcp.split(",") if x.strip()), sim_catalog=a.sim_catalog,
                      sample_interval_s=a.sample_interval, user_timeout_s=a.user_timeout, warmup_s=a.warmup,
                      drain_s=a.drain, window_end=a.window_end, hints=not a.no_hints,
                      summary_tokens=a.summary_tokens, fit_summary=not a.no_fit_summary, force=a.force,
                      extra=extra or {})


def board_mcp_command(ex, run_dir: Path) -> tuple[str, list[str], dict]:
    """hw-mcp (stdio) на копии фикстуры sysfs: бинарник и фикстура копируются в рабочий каталог прогона
    (на Mac — снимок hwmcp/.build/hw-mcp, в ВМ — hw-mcp-linux-arm64), чтобы пересборка hwmcp другим
    процессом не меняла прогон."""
    wd = ex.workdir(run_dir)
    local = isinstance(ex, LocalExec)
    src_bin = ROOT / "hwmcp" / ".build" / ("hw-mcp" if local else "hw-mcp-linux-arm64")
    if not src_bin.exists():
        raise FileNotFoundError(f"{src_bin} not built (make -C hwmcp build / build-linux-arm64)")
    dst_bin = f"{wd}/bin/hw-mcp"
    fixture = ROOT / "hwmcp" / "testdata" / "fixture_root"
    declared = ROOT / "hwmcp" / "configs" / "declared.lab.json"
    if local:
        Path(dst_bin).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_bin, dst_bin)
        dst_fx = Path(wd) / "fixture_root"
        if dst_fx.exists():
            shutil.rmtree(dst_fx)
        shutil.copytree(fixture, dst_fx, symlinks=True)
        dst_decl = Path(wd) / "declared.lab.json"
        shutil.copy2(declared, dst_decl)
    else:
        ex.run(["bash", "-c", f"mkdir -p {wd}/bin && cp {src_bin} {dst_bin} && chmod +x {dst_bin} && "
                              f"rm -rf {wd}/fixture_root && cp -a {fixture} {wd}/fixture_root && "
                              f"cp {declared} {wd}/declared.lab.json"])
        dst_fx = f"{wd}/fixture_root"
        dst_decl = f"{wd}/declared.lab.json"
    args = ["-root", str(dst_fx), "-declared", str(dst_decl), "-catalog", "generic",
            "-trace", f"{wd}/hwmcp_trace.jsonl", "-run-id", run_dir.name]
    info = {"binary_src": str(src_bin), "sha256": sha256_file(src_bin), "fixture": str(fixture),
            "declared": str(declared)}
    return dst_bin, args, info


def print_summary(run_dir: Path) -> None:
    p = run_dir / "summary.md"
    if p.exists():
        print(p.read_text(encoding="utf-8"))
    print(f"run dir: {run_dir}")


def env_python_path() -> dict:
    return {"PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}


__all__ = ["Adapter", "DayPlayer", "Endpoints", "PlayConfig", "ROOT", "board_mcp_command", "common_args",
           "config_from_args", "env_python_path", "http_json", "print_summary", "sha256_file", "os"]
