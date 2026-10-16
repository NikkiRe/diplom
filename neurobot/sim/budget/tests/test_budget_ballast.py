"""Балласт: размер с учётом своего PSS, резидентность страниц, цикл CPU, выполнение скрипта в своём процессе."""
import os
import signal
import subprocess
import sys
import time

import pytest

from neurobot.sim.budget import ballast as B

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
ENV = dict(os.environ, PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1")


def test_alloc_net_subtracts_own_pss():
    assert B.alloc_mib(30, 10 * 1024, True) == 20
    assert B.alloc_mib(30, 40 * 1024, True) == 0
    assert B.alloc_mib(30, 10 * 1024, False) == 30
    assert B.alloc_mib(30, None, True) == 30                # не Linux: PSS неизвестен


def test_hold_touches_every_page():
    buf = B.hold(0.05)
    assert len(buf) == int(0.05 * B.MIB)
    assert all(buf[i] == 1 for i in range(0, len(buf), B.PAGE))
    assert B.hold(0) == bytearray()


def test_own_pss_parses_rollup(tmp_path):
    f = tmp_path / "smaps_rollup"
    f.write_text("00400000-ffff [rollup]\nRss:  2048 kB\nPss:  1500 kB\n")
    assert B.own_pss_kib(str(f)) == 1500
    assert B.own_pss_kib(str(tmp_path / "нет")) is None


def test_duty_split_and_limits():
    assert B.duty(25, 0.1) == pytest.approx((0.025, 0.075))
    for pct, period in ((0, 0.1), (101, 0.1), (10, 0)):
        with pytest.raises(ValueError):
            B.duty(pct, period)


def test_run_script_as_main_in_same_process(tmp_path):
    script = tmp_path / "child.py"
    marker = tmp_path / "out.txt"
    script.write_text("import os, sys\nopen(%r, 'w').write('%%s|%%s|%%d' %% (__name__, ' '.join(sys.argv[1:]), "
                      "os.getpid()))\nsys.exit(7)\n" % str(marker))
    p = subprocess.Popen([sys.executable, "-m", "neurobot.sim.budget.ballast", "--mib", "1", "--run", str(script),
                          "--", "--a", "b"], env=ENV, cwd=ROOT, stderr=subprocess.PIPE, text=True)
    assert p.wait(timeout=30) == 7                          # код скрипта — код процесса
    name, args, pid = marker.read_text().split("|")
    assert name == "__main__" and args == "--a b" and int(pid) == p.pid
    assert "ballast: 1.0 МиБ" in p.stderr.read()


@pytest.mark.parametrize("argv", [["--mib", "-1"], ["--mib", "1", "--cpu-pct", "150"], ["--mib", "1", "--", "x"],
                                  ["--mib", "1", "--net", "--run", "x.py"], ["--mib", "nan"]])
def test_bad_arguments_exit_2(argv):
    with pytest.raises(SystemExit) as e:
        B.main(argv)
    assert e.value.code == 2


def test_standalone_burns_and_stops_on_sigterm():
    p = subprocess.Popen([sys.executable, "-m", "neurobot.sim.budget.ballast", "--mib", "1", "--net", "--cpu-pct",
                          "20", "--period-ms", "20"], env=ENV, cwd=ROOT, stderr=subprocess.PIPE, text=True)
    time.sleep(1.5)
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=10) == 0
    assert "CPU 20.0 %" in p.stderr.read()
