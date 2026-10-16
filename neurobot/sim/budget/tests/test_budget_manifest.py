"""Манифест прогона: шаги, хеши кода, отклонения."""
import json

from neurobot.sim.budget import manifest as MF


def test_manifest_lists_steps_hashes_and_deviations(tmp_path):
    step = tmp_path / "main"
    step.mkdir()
    (step / "step.json").write_text(json.dumps({"label": "main", "mem_mib": 3336, "quota_pct": 102,
                                                "slice_props": ["MemoryMax=3336M"], "status_end": {}}))
    assert MF.main(["--run", str(tmp_path), "--base", str(tmp_path / "нет")]) == 0
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert m["steps"] == [{"label": "main", "mem_mib": 3336, "quota_pct": 102, "cpus": None, "period_ms": None,
                           "ratio": None, "domain": None, "proxy_domain": None, "slice_props": ["MemoryMax=3336M"],
                           "ready_s": None, "twin_run": None}]
    code = m["code_sha256"]
    assert "neurobot/sim/budget/stack.py" in code and "neurobot/deploy/mcp/ros_mcp_server.patch" in code
    assert all(len(v) == 64 for v in code.values())
    assert m["tools_sha256"] == {} and len(m["deviations"]) >= 8


def test_manifest_requires_run_dir(tmp_path):
    assert MF.main(["--run", str(tmp_path / "нет")]) == 2
