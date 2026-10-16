"""Паритетный прогон EdgeCtx в том же сжатом реальном времени, что и аналоги.

    .venv/bin/python -m bench.baselines.drive_edgectx --day day.jsonl --k 60 [--host sbc]

Основной режим EdgeCtx — виртуальные часы (T0, bench/replay): день за секунды, LLM — ноль виртуального
времени. У аналогов виртуальных часов нет, поэтому для сравнения «один в один» EdgeCtx можно гнать так же:
`clock: real`, все периоды политики /k (timecomp.edgectx_compressed_config: heartbeat_min, гейт
debounce/cooldown/coalesce/self_echo, gap_s сигналов, snapshot_period_s, profile_consolidate_s), события —
уведомлениями sim-mcp по подписке (часы sim-mcp двигает тот же проигрыватель, в реальном сжатом времени),
реплики — штатный канал POST /v1/user. Пуш драйвера выключен (у EdgeCtx свой гейт). Подсказки X-Bench-*
llmtap не добавляет: EdgeCtx сам передаёт X-Edgectx-Wake-Reason/Purpose/Input-Ids (по ним же — класс вызова).

Базовый конфиг — bench/simmcp/edgectx_simday.yaml (как в T0); бинарник — снимок agent/bin/edgectx-<платформа>
(копируется в каталог прогона: другой процесс может пересобрать agent/).
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import yaml

from . import timecomp
from .dayplay import (ROOT, Adapter, DayPlayer, Endpoints, board_mcp_command, common_args, config_from_args,
                      http_json, print_summary, sha256_file)
from .executor import LocalExec

BASE_CONFIG = ROOT / "bench" / "simmcp" / "edgectx_simday.yaml"


class EdgeCtxAdapter(Adapter):
    system = "edgectx"
    rules = "generic"
    noop_text = ""                      # как в T0 (mock-llm по умолчанию): пустой ответ без вызовов
    needs_broker = False

    def __init__(self, cfg, ex, run_dir):
        super().__init__(cfg, ex, run_dir)
        cfg.hints = False
        cfg.push = "none"
        self.bin = f"{self.workdir}/bin/edgectx"
        self.cfg_path = f"{self.workdir}/edgectx.yaml"
        self.trace = f"{self.workdir}/edgectx_trace.jsonl"
        self.log = f"{self.workdir}/logs/edgectx.log"
        self.proc = None
        self._stopped = False
        self.base_config = Path(cfg.extra.get("base_config") or BASE_CONFIG)

    def src_bin(self) -> Path:
        plat = "darwin-arm64" if isinstance(self.ex, LocalExec) else "linux-arm64"
        return ROOT / "agent" / "bin" / f"edgectx-{plat}"

    def plan(self) -> timecomp.Plan:
        base = yaml.safe_load(self.base_config.read_text(encoding="utf-8"))
        if self.cfg.heartbeat_min != 30.0:
            base.setdefault("policy", {})["heartbeat_min"] = self.cfg.heartbeat_min
        self._compressed, plan = timecomp.edgectx_compressed_config(base, self.cfg.k)
        return plan

    def prepare(self, ep: Endpoints, plan: timecomp.Plan, day) -> dict:
        ex, wd = self.ex, self.workdir
        src = self.src_bin()
        if not src.exists():
            raise FileNotFoundError(f"{src} (make -C agent build-mac / build-linux-arm64)")
        ex.mkdir(f"{wd}/bin")
        if isinstance(ex, LocalExec):
            shutil.copy2(src, self.bin)
        else:
            ex.run(["bash", "-c", f"cp {src} {self.bin} && chmod +x {self.bin}"])
        c = self._compressed
        c["run_id"] = self.run_dir.name
        c["listen"] = f"127.0.0.1:{self.cfg.port('gateway')}"
        c.setdefault("llm", {})["base_url"] = ep.llm_base_url
        c["llm"]["trace_headers"] = True
        servers = []
        info: dict = {"binary": {"src": str(src), "sha256": sha256_file(src)}}
        for m in c.get("mcp") or []:
            if m.get("name") == "sim" and "sim" in self.cfg.mcp:
                m = dict(m, url=ep.sim_mcp_url)
                servers.append(m)
        if "board" in self.cfg.mcp and self.cfg.extra.get("with_board"):
            cmd, args, binfo = board_mcp_command(ex, self.run_dir)
            servers.append({"name": "hw", "transport": "stdio", "cmd": [cmd, *args], "inventory_uri": "hw://inventory"})
            info["hw_mcp"] = binfo
        c["mcp"] = servers
        c["trace"] = self.trace
        c.setdefault("userctx", {})["db"] = f"{wd}/edgectx.db"
        c["userctx"]["reset"] = True
        text = yaml.safe_dump(c, allow_unicode=True, sort_keys=False)
        ex.write_file(self.cfg_path, text)
        (self.run_dir / "edgectx_config.yaml").write_text(text, encoding="utf-8")
        info.update({"config": c, "base_config": str(self.base_config)})
        return info

    def env(self) -> dict:
        return {"TZ": "Europe/Moscow"}

    def start(self) -> None:
        self.proc = self.ex.spawn([self.bin, "-config", self.cfg_path, "-trace", self.trace,
                                   "-run-id", self.run_dir.name], log=self.log, env=self.env(), cwd=self.workdir)

    def _health(self) -> dict | None:
        url = f"http://127.0.0.1:{self.cfg.port('gateway')}/v1/health"
        if isinstance(self.ex, LocalExec):
            try:
                st, r = http_json("GET", url, timeout=2)
                return r if st == 200 else None
            except OSError:
                return None
        r = self.ex.run(["curl", "-s", "-m", "2", url], timeout=30)
        try:
            return json.loads(r.stdout.decode()) if r.returncode == 0 else None
        except ValueError:
            return None

    def wait_ready(self, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            h = self._health()
            if h and h.get("ok"):
                return
            if not self.ex.alive(self.proc):
                raise RuntimeError(f"edgectx exited: {self.ex.read_file(self.log).decode(errors='replace')[-2000:]}")
            time.sleep(0.3)
        raise RuntimeError("edgectx not ready")

    def root_pids(self) -> list[int]:
        return [self.proc.pid]

    def send_user(self, input_id: str, text: str, timeout: float) -> dict:
        url = f"http://127.0.0.1:{self.cfg.port('gateway')}/v1/user"
        body = {"id": input_id, "text": text}
        if isinstance(self.ex, LocalExec):
            st, r = http_json("POST", url, body, timeout=timeout)
        else:
            p = self.ex.run(["curl", "-s", "-m", str(int(timeout)), "-XPOST", url, "-d",
                             json.dumps(body, ensure_ascii=False)], timeout=timeout + 30)
            try:
                st, r = 200, json.loads(p.stdout.decode())
            except ValueError:
                st, r = 0, None
        ok = st == 200 and isinstance(r, dict) and r.get("ok", True)
        return {"ok": bool(ok), "reply": (r or {}).get("text") if isinstance(r, dict) else None,
                "llm_calls": (r or {}).get("llm_calls") if isinstance(r, dict) else None}

    def push_event(self, text: str) -> dict:
        return {"ok": False, "error": "edgectx: события идут уведомлениями sim-mcp, пуш драйвера выключен"}

    def stop(self) -> dict:
        if self._stopped:
            return {}
        self._stopped = True
        out: dict = {}
        url = f"http://127.0.0.1:{self.cfg.port('gateway')}"
        try:
            if isinstance(self.ex, LocalExec):
                st, m = http_json("GET", url + "/v1/metrics", timeout=5)
                (self.run_dir / "edgectx_metrics.json").write_text(json.dumps(m, ensure_ascii=False, indent=1),
                                                                   encoding="utf-8")
                http_json("POST", url + "/v1/shutdown", {}, timeout=5)
            else:
                self.ex.run(["curl", "-s", "-m", "5", "-XPOST", url + "/v1/shutdown"], timeout=30)
        except OSError:
            pass
        if self.proc is not None:
            deadline = time.time() + 15
            while time.time() < deadline and self.ex.alive(self.proc):
                time.sleep(0.2)
            out["edgectx_rc"] = self.ex.stop(self.proc, timeout=5)
        return out

    def collect(self) -> list[str]:
        got = []
        for remote, name in ((self.trace, "edgectx_trace.jsonl"), (self.log, "logs/edgectx.log")):
            if self.ex.fetch(remote, self.run_dir / name):
                got.append(name)
        return got

    def versions(self) -> dict:
        r = self.ex.run([self.bin, "-version"], timeout=30)
        return {"edgectx": r.stdout.decode().strip() or r.stderr.decode().strip(),
                "binary_sha256": sha256_file(self.src_bin())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.baselines.drive_edgectx", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    common_args(ap, "edgectx")
    ap.add_argument("--base-config", default=str(BASE_CONFIG))
    ap.add_argument("--with-board", action="store_true", help="подключить и hw-mcp (stdio, фикстура)")
    a = ap.parse_args(argv)
    if "--mcp" not in (argv if argv is not None else __import__("sys").argv):
        a.mcp = "sim,board" if a.with_board else "sim"
    if "--port-base" not in (argv if argv is not None else __import__("sys").argv):
        a.port_base = 18134
    cfg = config_from_args(a, "t0-policy", {"base_config": a.base_config, "with_board": a.with_board})
    run_dir = DayPlayer(cfg, EdgeCtxAdapter).run()
    print_summary(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
