"""Разбор прогона: окно замера, PSS и CPU компонентов, балласт, OOM и его жертвы, исход шага, отчёт."""
import json

import pytest

from neurobot.sim.budget import report as R
from neurobot.sim.budget.model import load


def proc(pid, pss_mib, majflt=0, comm="python3"):
    return {"pid": pid, "comm": comm, "pss_kib": int(pss_mib * 1024), "rss_kib": int(pss_mib * 1024) + 512,
            "pss_anon_kib": 0, "pss_file_kib": 0, "majflt": majflt, "threads": 4}


def sample(t, perc_usage, perc_pid=77, oom=0, psi=0, alive=True):
    units = {
        "perception": {"cg": {"memory_current": 1, "cpu": {"usage_usec": perc_usage}},
                       "procs": [proc(perc_pid, 1300, comm="pt_main_thread")] if alive else []},
        "a": {"cg": {"memory_current": 1, "cpu": {"usage_usec": int(t * 1e5)}}, "procs": [proc(10, 40, majflt=t)]},
        "bal": {"cg": {"memory_current": 1, "cpu": {"usage_usec": 0}}, "procs": [proc(20, 30)]},
    }
    sl = {"memory_current": 1500 << 20, "memory_peak": 1600 << 20, "events": {"oom_kill": oom, "max": 0},
          "cpu": {"usage_usec": int(t * 2e5), "nr_periods": t, "nr_throttled": t // 10, "throttled_usec": 0},
          "psi_mem": {"some": {"total": psi}}}
    return {"type": "sample", "t_wall": float(t), "units": units, "slice": sl,
            "sys": {"load1": 2.0, "cpu_ticks": {"total": t * 600, "idle": t * 300}}}


@pytest.fixture
def step_dir(tmp_path):
    d = tmp_path / "run" / "main"
    d.mkdir(parents=True)
    (d / "step.json").write_text(json.dumps({
        "label": "main", "mem_mib": 3336, "quota_pct": 102, "ratio": 0.25, "t_measure_start": 100,
        "t_measure_end": 200, "ready_s": {"perception": 2.0, "a": 1.0},
        "status_end": {"jetson-perception.service": {"ActiveState": "active", "NRestarts": "2"},
                       "jetson-a.service": {"ActiveState": "active", "NRestarts": "0"},
                       "jetson-x.scope": {"ActiveState": "active"}},
        "twin_alive_end": True,
        "kernel_oom": ["1792 kernel: Memory cgroup out of memory: Killed process 77 (pt_main_thread) total-vm:1kB"]}))
    (d / "units.json").write_text(json.dumps({"slice": "/s", "units": {
        "perception": {"role": "measured"}, "a": {"role": "measured"}, "bal": {"role": "ballast"}}}))
    rows = [{"type": "header"}, sample(90, 0, oom=0), sample(100, 1_000_000, oom=0, psi=0),
            sample(150, 3_000_000, oom=1, psi=1_000_000, alive=False),
            sample(200, 500_000, perc_pid=78, oom=1, psi=1_000_000)]   # perception перезапущен: счётчик с нуля
    (d / "collect.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n{недописано")
    health = [{"t_wall": float(t), "dt": 1.0, "n": {"safety": 5, "gesture": 1 if t == 150 else 0, "health": 1,
                                                    "truth": 1},
               "safety": {"ok": 5}, "cmd_vel_nonzero": 2, "fps": 10.0, "perception": "ok", "rtf": 0.95}
              for t in range(100, 201, 10)]
    health[3] = {"t_wall": 130.0, "dt": 1.0, "n": {"safety": 5}, "safety": {"ok": 5}, "cmd_vel_nonzero": 0,
                 "fps": 3.0, "perception": "ok"}    # perception молчит; старые значения в строке не учитываются
    (d / "health.jsonl").write_text("\n".join(json.dumps(r) for r in health))
    return d


def test_counter_delta_handles_restart():
    assert R.counter_delta([10, 20, 5, 15]) == 10 + 5 + 10
    assert R.counter_delta([7]) == 0


def test_step_summary(step_dir):
    s = R.step_summary(str(step_dir), 0.25, 1024)
    p, a = s["components"]["perception"], s["components"]["a"]
    assert s["samples"] == 3 and s["window_s"] == 100
    assert p["pss_mib"] == 1300 and p["code_pss_mib"] == 276 and p["pss_peak_mib"] == 1300
    assert p["cpu_host_pct"] == pytest.approx(2.5)                # (2 + 0,5) с CPU за 100 с
    assert p["cpu_a57_pct"] == pytest.approx(10.0)
    assert a["cpu_host_pct"] == pytest.approx(10.0) and a["majflt_per_s"] == pytest.approx(1.0)
    assert s["total_pss_mib"] == 1370 and s["total_pss_peak_mib"] == 1370
    assert s["ballast_pss_mib"] == 1054                            # balласт-процесс 30 + cuda 1024
    assert s["oom_kills"] == 1 and s["oom_victims"] == [{"unit": "perception", "comm": "pt_main_thread", "kills": 1}]
    assert s["psi_mem_some_pct"] == pytest.approx(1.0) and s["throttled_frac"] == pytest.approx(0.1)
    assert s["slice_peak_mib"] == 1500                             # по сэмплам, не memory.peak за жизнь slice
    assert s["slice_cpu_a57_cores"] == pytest.approx(0.8) and s["vm_busy_frac"] == 0.5
    assert s["restarts"] == {"jetson-perception.service": 2} and s["not_active_end"] == []
    h = s["health"]
    assert h["safety_hz"] == 5.0 and h["gestures"] == 1 and h["fps_median"] == 10.0 and h["window_s"] == 11
    assert h["perception_silent_frac"] == pytest.approx(1 / 11, abs=1e-3) and h["perception_states"] == {"ok": 10}
    assert h["rtf_median"] == 0.95


def test_classify():
    base = {"ready_s": {"a": 1.0}, "restarts": {}, "components": {"a": {"majflt_per_s": 0.0}}, "oom_kills": 0,
            "oom_victims": [], "twin_alive_end": True, "psi_mem_some_pct": 0.0,
            "health": {"fps_median": 10.0, "safety_hz": 5.0}}
    assert R.classify(base, 10.0) == {"outcome": "норма", "first": "", "reasons": []}
    bad = dict(base, ready_s={"perception": None}, psi_mem_some_pct=12.0, twin_alive_end=False,
               health={"fps_median": 6.0, "safety_hz": 3.0, "perception_silent_frac": 0.5})
    v = R.classify(bad, 10.0)
    assert v["outcome"] == "деградация" and v["first"] == "не готов: perception" and len(v["reasons"]) == 6
    assert "perception молчит 50 % времени" in v["reasons"]
    oom = dict(base, oom_kills=3, oom_victims=[{"unit": "perception", "comm": "pt_main_thread", "kills": 3}])
    assert R.classify(oom, 10.0)["outcome"] == "OOM"
    assert R.classify(oom, 10.0)["first"] == "perception ×3 (pt_main_thread)"


def test_oom_victims_grouped_and_mapped_by_name():
    samples = [{"units": {"perception": {"procs": [{"pid": 1, "comm": "pt_main_thread"}]},
                          "safety": {"procs": [{"pid": 2, "comm": "python3"}]},
                          "executor": {"procs": [{"pid": 3, "comm": "python3"}]}}}]
    units = {"perception": {}, "safety": {}, "executor": {}}
    lines = ["Killed process 1 (pt_main_thread)", "Killed process 9 (pt_main_thread)",
             "Killed process 8 (pt_main_thread)", "Killed process 7 (python3)", "Killed process 2 (python3)",
             "oom-kill:constraint=CONSTRAINT_MEMCG"]
    assert R.oom_victims(lines, samples, units) == [
        {"unit": "perception", "comm": "pt_main_thread", "kills": 3},   # 8 и 9 — между сэмплами, по имени
        {"unit": None, "comm": "python3", "kills": 1},                  # python3 — у двух юнитов, не угадываем
        {"unit": "safety", "comm": "python3", "kills": 1}]


def test_build_and_markdown(step_dir, tmp_path):
    run = step_dir.parent
    second = run / "mem2048"
    second.mkdir()
    for f in ("units.json", "collect.jsonl", "health.jsonl"):
        (second / f).write_text((step_dir / f).read_text())
    st = json.loads((step_dir / "step.json").read_text())
    st.update(label="mem2048", mem_mib=2048)
    (second / "step.json").write_text(json.dumps(st))
    rep = R.build(str(run), "main", load(), {"ratio": 0.25, "host_single_median": 6000, "nano_single_mips": 1578,
                                             "quota_pct": 102, "cores": 4}, None)
    assert rep["main"]["label"] == "main" and [s["label"] for s in rep["steps"]] == ["mem2048"]
    assert rep["budget"]["headless"]["value"]["headroom_mib"] == pytest.approx(3336 - 1370)
    assert rep["first_break"].startswith("2048 МиБ — OOM")
    md = R.md_report(rep)
    assert "perception | measured | 1300.0 (код 276.0 + cuda 1024)" in md and "| headless | 3336 |" in md
    assert R.main(["--run", str(run), "--out", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "report.json").read_text())["main"]["total_pss_mib"] == 1370


def test_unique_pages_preferred_for_budget(step_dir):
    (step_dir / "uniq.json").write_text(json.dumps({"slice_unique_kib": 1200 * 1024, "slice_pss_kib": 1100 * 1024,
                                                    "slice_rss_kib": 1500 * 1024,
                                                    "units_kib": {"perception": 1290 * 1024, "a": None}}))
    rep = R.build(str(step_dir.parent), "main", load(), None, None)
    m = rep["main"]
    assert m["unique_mib"] == 1200 and m["components"]["perception"]["unique_mib"] == 1290
    assert "unique_mib" not in m["components"]["a"]
    assert rep["budget_basis"] == "unique" and rep["budget"]["gnome"]["value"]["headroom_mib"] == 2406 - 1200
    md = R.md_report(rep)
    assert "Уникальные страницы slice" in md and "сравнивается с уникальными страницами slice" in md
    (step_dir / "uniq.json").write_text(json.dumps({"slice_unique_kib": None, "slice_pss_kib": 0, "slice_rss_kib": 0,
                                                    "units_kib": {}}))
    assert R.build(str(step_dir.parent), "main", load(), None, None)["budget_basis"] == "pss"   # не root — PSS


def test_main_without_steps(tmp_path):
    assert R.main(["--run", str(tmp_path)]) == 2
