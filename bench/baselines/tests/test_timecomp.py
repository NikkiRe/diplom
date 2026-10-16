"""Сжатие времени: шкала, планы систем, допустимость метрик."""
import time

import pytest

from bench.baselines import timecomp as tc


def test_timemap_roundtrip():
    tm = tc.TimeMap(60, t0=25200, wall0=1000.0)
    assert tm.wall(25200) == 1000.0
    assert tm.wall(25200 + 3600) == pytest.approx(1060.0)
    assert tm.day(1060.0) == pytest.approx(28800.0)
    assert tm.day_s(2.0) == 120.0


def test_timemap_sleep_until_reports_lag():
    tm = tc.TimeMap(1000, t0=0, wall0=time.time())
    lag = tm.sleep_until(50)          # 50 с дня = 50 мс стенных
    assert 0 <= lag < 0.2
    lag2 = tm.sleep_until(10)         # уже прошло — опоздание > 0
    assert lag2 > 0


def test_picoclaw_heartbeat_native_when_exact():
    for k, minutes in ((1, 30), (2, 15), (3, 10), (5, 6), (6, 5)):
        p = tc.picoclaw_plan(k)
        assert p.values["heartbeat_mode"] == "native"
        assert p.values["heartbeat_interval_min"] == minutes
        assert not [d for d in p.deviations if "heartbeat" in d]


def test_picoclaw_heartbeat_cron_emulation_at_k60():
    p = tc.picoclaw_plan(60)
    assert p.values["heartbeat_mode"] == "cron"
    assert p.values["heartbeat_every_s"] == 30
    assert p.values["cron_check_every_s"] == 5
    assert any("эмуляция задачей cron" in d for d in p.deviations)
    # принудительно native: минимум 5 мин → 300 мин суток вместо 30
    n = tc.picoclaw_plan(60, hb_mode="native")
    s = [x for x in n.scaled if x.name == "heartbeat.interval"][0]
    assert s.config_value == 5 and not s.exact and s.effective_day_s == 5 * 60 * 60


def test_nanobot_plan_k60():
    p = tc.nanobot_plan(60)
    v = p.values
    assert v["heartbeat_interval_s"] == 30
    assert v["dream_cron"] == "*/2 * * * *"
    assert v["idle_compact_min"] == 1
    assert v["idle_check_s"] == 1
    assert v["max_session_messages_per_minute"] == 360
    assert any("idleCompactAfterMinutes" in d for d in p.deviations)
    assert any("local_triggers" in d for d in p.deviations)


def test_nanobot_dream_k1_and_k2_use_interval_h():
    assert tc.nanobot_plan(1).values["dream_interval_h"] == 2
    assert tc.nanobot_plan(2).values["dream_interval_h"] == 1


def test_nanobot_dream_nondivisor_reports_effective_rate():
    p = tc.nanobot_plan(15)        # цель 8 мин: */8 → 8 запусков в час (7,5 нужно)
    s = [x for x in p.scaled if x.name.endswith("dream.cron")][0]
    assert not s.exact
    assert s.config_value == "*/8 * * * *"
    assert s.effective_day_s == pytest.approx(60 / 8 * 60 * 15)


def test_exact_k_sets():
    assert tc.exact_k("picoclaw", range(1, 61)) == [1, 2, 3, 5, 6]
    assert tc.exact_k("nanobot", range(1, 61)) == [1]


def test_mock_plan_and_latency():
    p = tc.mock_plan(60, cache_ttl_s=300, latency="lognormal:800:0.5")
    assert p.values["cache_ttl_s"] == pytest.approx(5.0)
    assert p.values["latency"] == "lognormal:13.3333:0.5"
    assert tc.scale_latency("fixed:600", 60) == "fixed:10"
    assert tc.scale_latency("0", 60) == "0"
    with pytest.raises(ValueError):
        tc.scale_latency("weird:1", 60)


def test_edgectx_compressed_config():
    cfg = {"clock": {"mode": "virtual", "start_t": 25200, "epoch": "x"},
           "policy": {"heartbeat_min": 30, "snapshot_period_s": 60, "profile_consolidate_s": 600,
                      "gate": {"debounce_s": 2, "cooldown_s": 300, "coalesce_s": 10, "self_echo_s": 10,
                               "signals": [{"device": "pir_*", "kind": "episode", "gap_s": 600}]}}}
    out, plan = tc.edgectx_compressed_config(cfg, 60)
    assert out["clock"]["mode"] == "real" and "start_t" not in out["clock"]
    assert out["policy"]["heartbeat_min"] == pytest.approx(0.5)
    assert out["policy"]["gate"]["cooldown_s"] == pytest.approx(5)
    assert out["policy"]["gate"]["signals"][0]["gap_s"] == pytest.approx(10)
    assert cfg["policy"]["heartbeat_min"] == 30          # исходный не изменён
    assert plan.deviations


def test_metric_validity_covers_key_metrics():
    names = " ".join(m["metric"] for m in tc.METRIC_VALIDITY)
    for word in ("вызовы LLM", "токены", "кэш", "байты", "RSS", "CPU", "задержки"):
        assert word in names
