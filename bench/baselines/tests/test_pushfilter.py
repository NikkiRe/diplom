"""Фильтр пуша: предикаты сигналов гейта EdgeCtx без debounce/cooldown/коалесценции."""
from pathlib import Path

import pytest

from bench.baselines.llmtap import EVENT_MARKER
from bench.baselines.pushfilter import PushEvent, PushFilter, Signal, format_event

ROOT = Path(__file__).resolve().parents[3]


def mk(mode="signals", signals=None, default="ignore", init=None):
    f = PushFilter(mode=mode, signals=signals or [], default=default)
    f.set_initial(init or {})
    return f


def test_change_baseline_and_repeats():
    f = mk(signals=[Signal("door_front", "open", "change")], init={"door_front": {"open": 0}})
    assert f.on_sensor(1, "door_front", "open", 0) == []          # не изменилось
    ev = f.on_sensor(2, "door_front", "open", 1)
    assert len(ev) == 1 and ev[0].prev == 0 and ev[0].reason == "change"
    assert f.on_sensor(3, "door_front", "open", 1) == []
    g = mk(signals=[Signal("door_*", "open", "change")])           # без начального состояния — база
    assert g.on_sensor(1, "door_back", "open", 1) == []
    assert len(g.on_sensor(2, "door_back", "open", 0)) == 1


def test_threshold_above_with_hysteresis():
    f = mk(signals=[Signal("boiler", "temperature", "threshold", above=85, hysteresis=1)],
           init={"boiler": {"temperature": 60}})
    assert f.on_sensor(1, "boiler", "temperature", 84.9) == []
    assert len(f.on_sensor(2, "boiler", "temperature", 85.5)) == 1
    assert f.on_sensor(3, "boiler", "temperature", 86) == []        # уже выше — не будит
    assert f.on_sensor(4, "boiler", "temperature", 84.5) == []      # в полосе гистерезиса — не перевзведён
    assert f.on_sensor(5, "boiler", "temperature", 85.2) == []
    assert f.on_sensor(6, "boiler", "temperature", 83.9) == []      # ниже 84 — перевзведён
    assert len(f.on_sensor(7, "boiler", "temperature", 85.1)) == 1


def test_threshold_initially_above_is_not_armed():
    f = mk(signals=[Signal("t", "x", "threshold", above=30)], init={"t": {"x": 35}})
    assert f.on_sensor(1, "t", "x", 36) == []


def test_delta():
    f = mk(signals=[Signal("h", "rh", "delta", delta=5)], init={"h": {"rh": 50}})
    assert f.on_sensor(1, "h", "rh", 53) == []
    assert len(f.on_sensor(2, "h", "rh", 55)) == 1
    assert f.on_sensor(3, "h", "rh", 58) == []


def test_episode_end_after_gap():
    f = mk(signals=[Signal("pir_*", "motion", "episode", gap_s=600)])
    assert f.on_sensor(100, "pir_k", "motion", 1) == []
    assert f.on_sensor(200, "pir_k", "motion", 0) == []
    assert f.next_deadline() == 800
    assert f.tick(799) == []
    ev = f.tick(900)
    assert len(ev) == 1 and ev[0].reason == "episode_end" and ev[0].t == 800
    assert f.tick(2000) == [] and f.next_deadline() is None


def test_default_and_ignore_and_modes():
    f = mk(signals=[Signal("light_*", "*", "ignore")], default="change", init={"light_k": {"on": False}, "x": {"y": 1}})
    assert f.on_sensor(1, "light_k", "on", True) == []
    assert len(f.on_sensor(2, "x", "y", 2)) == 1
    a = mk(mode="all")
    assert len(a.on_sensor(1, "anything", "p", 1)) == 1
    n = mk(mode="none")
    assert n.on_sensor(1, "door", "open", 1) == [] and n.seen == 1
    with pytest.raises(ValueError):
        PushFilter(mode="bogus")


def test_from_edgectx_gate_config():
    f = PushFilter.from_gate_config(ROOT / "bench" / "simmcp" / "edgectx_simday.yaml",
                                    initial_state={"door_front": {"open": 0}, "boiler": {"temperature": 60}})
    kinds = {(s.device, s.property): s.kind for s in f.signals}
    assert kinds[("door_front", "open")] == "change"
    assert kinds[("pir_*", "motion")] == "episode"
    assert f.default == "ignore"
    assert len(f.on_sensor(1, "door_front", "open", 1)) == 1
    assert f.on_sensor(2, "light_kitchen", "on", True) == []


def test_format_event():
    t = format_event(PushEvent(27251.5, "door_front", "open", 1, 0, "change"))
    assert t.startswith(EVENT_MARKER) and "07:34:11" in t and "door_front.open: 0 → 1" in t
    t2 = format_event(PushEvent(800, "pir_k", "motion", 0, None, "episode_end", "600 с тишины"))
    assert "конец эпизода" in t2 and "00:13:20" in t2
