"""Модель бюджета: структура model.json, бюджеты веток, квота CPU, кредит zram, CLI."""
import copy
import json

import pytest

from neurobot.sim.budget import model as M


@pytest.fixture(scope="module")
def m():
    return M.load()


def test_model_json_valid(m):
    assert M.validate(m) == []


def test_every_source_cited(m):
    cited = set()
    for sect in ("memory_mib", "os_branches", "ballast"):
        for it in m[sect].values():
            cited.update(it.get("src", []))
    cited.update(m["zram"]["src"])
    cited.update(m["cpu"]["src"])
    assert set(m["sources"]) - {"R7a", "R7b"} <= cited


def test_budget_value_is_visible_minus_os_minus_reserve(m):
    vis, res = m["memory_mib"]["visible"]["value"], m["memory_mib"]["reserve"]["value"]
    for b in M.BRANCHES:
        assert M.stack_budget(m, b) == vis - m["os_branches"][b]["value"] - res


def test_cases_ordered_and_desktop_smaller(m):
    bs = M.budgets(m)
    for cs in bs.values():
        assert cs["pessimistic"] <= cs["value"] <= cs["optimistic"]
    assert bs["gnome"]["value"] < bs["lxde"]["value"] < bs["headless"]["value"]


def test_unknown_case_rejected(m):
    with pytest.raises(ValueError):
        M.stack_budget(m, "headless", "worst")


def test_cpu_quota_from_7zip_ratio(m):
    nano = m["cpu"]["nano_7zip_single_mips"]
    assert M.cpu_ratio(m, nano * 4) == pytest.approx(0.25)
    assert M.cpu_quota_pct(m, nano * 4) == 100              # 4 ядра × 25 %
    assert M.cpu_quota_pct(m, nano * 4, cores=2) == 50      # режим 5 Вт
    with pytest.raises(ValueError):
        M.cpu_ratio(m, 0)


def test_zram_credit_capped_by_device(m):
    assert M.zram_credit(m, 300, 3.0) == pytest.approx(200)
    assert M.zram_credit(m, 10 ** 6, 2.0) == pytest.approx(m["zram"]["size_mib"] / 2)
    assert M.zram_credit(m, -5, 2.0) == 0
    with pytest.raises(ValueError):
        M.zram_credit(m, 100, 0.5)


def test_validate_catches_errors(m):
    bad = copy.deepcopy(m)
    bad["os_branches"]["gnome"]["tag"] = "догадка"
    bad["os_branches"]["lxde"]["src"] = ["нет-такого"]
    bad["memory_mib"]["visible"]["low"] = 5000
    bad["ballast"]["cuda"]["mib"] = 0
    del bad["zram"]
    errs = "\n".join(M.validate(bad))
    for frag in ("os_branches.gnome: пометка", "нет источника 'нет-такого'", "visible: нужно low",
                 "ballast.cuda: mib", "zram: нет"):
        assert frag in errs


def test_cli_json_and_errors(capsys):
    assert M.main(["--json", "--host-mips", "6000"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["budgets_mib"]["headless"]["value"] > 0 and out["cpu"]["quota_pct"] > 0
    assert M.main(["--host-mips", "-1"]) == 2
    assert M.main([]) == 0
    assert "CPU" not in capsys.readouterr().out
