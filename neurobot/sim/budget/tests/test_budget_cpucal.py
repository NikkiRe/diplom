"""Калибровка CPU: разбор вывода 7-zip (ВМ и Nano из sbc-bench), отношение и квота, ошибки."""
import pytest

from neurobot.sim.budget import cpucal
from neurobot.sim.budget.model import load

VM = """
7-Zip (a) [64] 16.02 : Copyright (c) 1999-2016 Igor Pavlov : 2016-05-21
p7zip Version 16.02 (locale=en_US.UTF-8,Utf16=on,HugeFiles=on,64 bits,6 CPUs LE)

RAM size:    7923 MB,  # CPU hardware threads:   6
RAM usage:    435 MB,  # Benchmark threads:      1

22:       7984   100   7767   7767  |      71565   100   6111   6110
----------------------------------  | ------------------------------
Avr:             100   6408   6408  |              100   6085   6084
Tot:             100   6246   6246
"""
NANO_MT = """7-Zip (a) [64] 16.02 : Copyright (c) 1999-2016 Igor Pavlov : 2016-05-21
RAM size:    3950 MB,  # CPU hardware threads:   4
RAM usage:    882 MB,  # Benchmark threads:      4
Tot:             351   1509   5289
"""


def test_parse_7z():
    assert cpucal.parse_7z(VM) == {"version": "16.02", "threads": 1, "usage_pct": 100, "ru_mips": 6246,
                                   "rating_mips": 6246}
    assert cpucal.parse_7z(NANO_MT)["rating_mips"] == 5289 and cpucal.parse_7z(NANO_MT)["threads"] == 4


def test_parse_7z_without_total():
    with pytest.raises(ValueError):
        cpucal.parse_7z("7-Zip (a) [64] 16.02\nError: …\n")


def test_summary_ratio_and_quota():
    m = load()
    s = cpucal.summary(m, [6187, 6151, 6173], 4)
    assert s["host_single_median"] == 6173
    assert s["ratio"] == pytest.approx(1578 / 6173, abs=1e-4)
    assert s["quota_pct"] == 102


def test_check_requires_root(monkeypatch):
    monkeypatch.setattr(cpucal.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit) as e:
        cpucal.main(["check", "--bin", "7za", "--quota", "100"])
    assert e.value.code == 2
