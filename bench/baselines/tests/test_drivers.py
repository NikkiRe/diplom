"""Конфиги драйверов и «дрейф маркеров»: тексты, по которым llmtap относит вызов к heartbeat/событию/сводке,
должны существовать в закреплённых версиях PicoClaw v0.3.1 и nanobot 0.3.5.

Медленные тесты с настоящими бинарниками (≈ 30 с каждый) — только при BASELINES_SLOW=1."""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from bench.baselines import timecomp
from bench.baselines.dayplay import Endpoints, PlayConfig, port_free
from bench.baselines.drive_picoclaw import (CHECK_SCRIPT, HEARTBEAT_PROMPT_TMPL, VENDOR, cron_job, deep_merge,
                                            overlay)
from bench.baselines.llmtap import RULES, classify

ROOT = Path(__file__).resolve().parents[3]
PC_SRC = ROOT / "bench" / "baselines" / "vendor" / "src" / "picoclaw"
NB_PKG = ROOT / ".venv-nanobot" / "lib" / "python3.12" / "site-packages" / "nanobot"
EP = Endpoints("127.0.0.1", "http://127.0.0.1:18122/v1", "http://127.0.0.1:18121/mcp", "127.0.0.1", 18126)


def test_picoclaw_overlay_profiles():
    plan = timecomp.picoclaw_plan(60)
    servers = {"sim": {"enabled": True, "type": "http", "url": EP.sim_mcp_url}}
    shipped = overlay("shipped", EP, plan, servers, "/w", 18123, False, "r1")
    best = overlay("best", EP, plan, servers, "/w", 18123, False, "r1")
    for o in (shipped, best):
        assert o["model_list"][0]["api_base"] == EP.llm_base_url
        assert o["channel_list"]["mqtt"]["settings"]["broker"] == "tcp://127.0.0.1:18126"
        assert o["tools"]["web"]["enabled"] is False and o["tools"]["web_fetch"]["enabled"] is False
        assert o["tools"]["mcp"]["discovery"]["enabled"] is False
        assert o["heartbeat"]["enabled"] is False            # k = 60 → эмуляция задачей cron
    assert "context_manager" not in shipped["agents"]["defaults"]
    assert best["agents"]["defaults"]["context_manager"] == "legacy"
    assert best["tools"]["exec"]["allow_remote"] is True
    assert best["tools"]["exec"]["enabled"] is True  # нужен штатной cron command
    for name in ("read_file", "write_file", "edit_file", "list_dir", "spawn", "subagent", "load_image", "i2c"):
        assert best["tools"][name]["enabled"] is False
        assert name not in shipped["tools"]
    native = overlay("shipped", EP, timecomp.picoclaw_plan(6), servers, "/w", 18123, False, "r1")
    assert native["heartbeat"] == {"enabled": True, "interval": 5}


def test_default_compression_is_15():
    import argparse
    from bench.baselines.dayplay import common_args
    ap = argparse.ArgumentParser()
    common_args(ap, "picoclaw")
    assert PlayConfig(day="x").k == ap.parse_args(["--day", "x"]).k == 15


def test_deep_merge_keeps_shipped_defaults():
    base = {"tools": {"web": {"enabled": True, "brave": {"x": 1}}, "exec": {"enabled": True}}, "a": 1}
    out = deep_merge(base, {"tools": {"web": {"enabled": False}}})
    assert out["tools"]["web"] == {"enabled": False, "brave": {"x": 1}} and out["tools"]["exec"]["enabled"]


def test_cron_job_shape_matches_go_struct():
    j = cron_job("id1", "n", 30, "msg", "mqtt", "mqtt:hb", command="sh ./x.sh")
    assert set(j) == {"id", "name", "enabled", "schedule", "payload", "state", "createdAtMs", "updatedAtMs",
                      "deleteAfterRun"}
    assert j["schedule"] == {"kind": "every", "everyMs": 30000}
    assert j["payload"]["kind"] == "agent_turn" and j["payload"]["command"] == "sh ./x.sh"
    if PC_SRC.exists():
        go = (PC_SRC / "pkg" / "cron" / "service.go").read_text()
        for tag in ("everyMs", "nextRunAtMs", "createdAtMs", "deleteAfterRun", '"command,omitempty"'):
            assert tag in go


def test_check_script_is_valid_sh(tmp_path):
    p = tmp_path / "c.sh"
    p.write_text(CHECK_SCRIPT.format(sim="http://127.0.0.1:1"))
    assert subprocess.run(["sh", "-n", str(p)]).returncode == 0
    for pat in (r"\$\([^)]+\)", r"\|\s*sh\b"):         # шаблоны запрета exec PicoClaw проверяют только команду
        assert not re.search(pat, "sh ./check_room.sh")


def test_emulated_heartbeat_is_classified_as_heartbeat():
    body = {"messages": [{"role": "user", "content": HEARTBEAT_PROMPT_TMPL.format(content="- task")}], "tools": [{}]}
    assert classify(body, RULES["picoclaw"])[0] == "heartbeat"


@pytest.mark.skipif(not PC_SRC.exists(), reason="исходники PicoClaw не склонированы (install_picoclaw.sh --src)")
def test_picoclaw_markers_present_in_source():
    hb = (PC_SRC / "pkg" / "heartbeat" / "service.go").read_text()
    assert "# Heartbeat Check" in hb and "HEARTBEAT_OK" in hb and "minIntervalMinutes     = 5" in hb
    sc = (PC_SRC / "pkg" / "seahorse" / "short_compaction.go").read_text()
    assert "Aggressive summary policy" in sc and "Target length: about %d tokens or less." in sc
    tok = (PC_SRC / "pkg" / "tokenizer").glob("*.go")
    assert any("2.5 characters per token" in f.read_text() for f in tok)
    mq = (PC_SRC / "pkg" / "channels" / "mqtt" / "mqtt.go").read_text()
    assert "%s/%s/+/request" in mq and '"%s/%s/%s/response"' in mq
    mgr = (PC_SRC / "pkg" / "mcp" / "manager.go").read_text()
    assert "mcp.NewClient(&mcp.Implementation{" in mgr and "}, nil)" in mgr     # клиент без обработчиков уведомлений


@pytest.mark.skipif(not NB_PKG.exists(), reason="nanobot не установлен (install_nanobot.sh)")
def test_nanobot_markers_present_in_package():
    gw = (NB_PKG / "cli" / "gateway_runtime.py").read_text()
    assert "You are executing periodic heartbeat tasks" in gw and "'All clear.'" in gw
    ev = (NB_PKG / "templates" / "agent" / "evaluator.md").read_text()
    assert re.search(r"\{% if part == 'system' %\}\s*You are a notification gate", ev)
    assert "You are running Dream" in (NB_PKG / "templates" / "agent" / "dream.md").read_text()
    assert "Create a compact replacement checkpoint" in (NB_PKG / "templates" / "agent" /
                                                         "consolidator_archive.md").read_text()
    assert "Please provide your response to the user based on the conversation above." in \
        (NB_PKG / "utils" / "runtime.py").read_text()
    lr = (NB_PKG / "triggers" / "local_runner.py").read_text()
    assert "poll_interval_s: float = 0.5" in lr
    assert '"stream": False' in (NB_PKG / "providers" / "openai_compat_provider.py").read_text()


def test_versions_pinned():
    vj = VENDOR / "versions.json"
    if not vj.exists():
        pytest.skip("install_picoclaw.sh not run")
    v = json.loads(vj.read_text())
    assert v["version"] == "0.3.1" and v["git"].startswith("2cf030d2") and v["license"] == "MIT"
    assert v["assets"]["picoclaw_Linux_arm64.tar.gz"] == "e78ab0de05d000698ff8b32458e71cb3134e52e9eaf540e3323a72e9914de060"
    assert v["assets"]["picoclaw_Darwin_arm64.tar.gz"] == "30266e4edd6b0dfb871c6ac58e69ffdf8039fc65f23f2612f8792f704930765b"
    lock = ROOT / "bench" / "baselines" / "requirements" / "nanobot-mac.lock"
    if lock.exists():
        assert "nanobot-ai==0.3.5" in lock.read_text()


SLOW = os.environ.get("BASELINES_SLOW") == "1"


@pytest.fixture(scope="module")
def tiny_day(tmp_path_factory):
    p = tmp_path_factory.mktemp("d") / "day.jsonl"
    subprocess.run([str(ROOT / ".venv" / "bin" / "python"), "-m", "bench.daygen", "gen", "--seed", "5", "--source",
                    "synthetic", "--profile", "S200", "--window", "07:00-07:30", "--rate-scale", "0.2", "--out", str(p)],
                   cwd=ROOT, check=True, capture_output=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    return p


@pytest.mark.skipif(not SLOW, reason="BASELINES_SLOW=1 — прогон с настоящими PicoClaw/nanobot")
@pytest.mark.parametrize("system", ["picoclaw", "nanobot"])
def test_real_agent_tiny_day(system, tiny_day, tmp_path):
    from bench.baselines.dayplay import DayPlayer

    if system == "picoclaw":
        from bench.baselines.drive_picoclaw import PicoClawAdapter as A
        base, profile, push = 18120, "best", "signals"
    else:
        from bench.baselines.drive_nanobot import NanobotAdapter as A
        base, profile, push = 18127, "default", "signals"
    if not all(port_free(base + i) for i in range(7)):
        pytest.skip("ports busy")
    cfg = PlayConfig(day=str(tiny_day), k=120, out=str(tmp_path), run_id=system, port_base=base, profile=profile,
                     push=push, warmup_s=1, drain_s=3)
    run = DayPlayer(cfg, A).run()
    s = json.loads((run / "summary.json").read_text())
    assert s["user"]["ok"] == s["user"]["sent"] > 0
    assert s["llm"]["day_by_source"]["user"]["calls"] >= 2 * s["user"]["sent"]
    assert s["sim_eval"]["commands"]["rate"] == 1.0
    assert s["llm"]["unmatched_mock"] == 0
    assert (s["resources"]["day"].get("rss_kib") or {}).get("max", 0) > 10 * 1024
