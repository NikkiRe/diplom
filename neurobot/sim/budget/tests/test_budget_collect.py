"""Сэмплер: разбор файлов cgroup и /proc, процессы вложенных групп, вышедшие процессы, CLI."""
import json
import os

import pytest

from neurobot.sim.budget import collect as C

STAT = ("1234 (python3 (x) y) S 1 1234 1234 0 -1 4194560 5000 0 7 0 300 45 0 0 20 0 9 0 777 0 0 18446744073709551615 "
        "0 0 0 0 0 0 0 16781312 16386 0 0 0 17 3 0 0 0 0 0")
SMAPS = ("aaaa-bbbb ---p 00000000 00:00 0 [rollup]\nRss:  4096 kB\nPss:  3000 kB\nPss_Anon:  2000 kB\n"
         "Pss_File:  900 kB\nPss_Shmem:  100 kB\nShared_Clean:  1 kB\nSwap:  0 kB\n")


def test_parse_flat_skips_garbage():
    assert C.parse_flat("usage_usec 10\nnr_periods 3\nbad line here\nx y\n") == {"usage_usec": 10, "nr_periods": 3}


def test_parse_smaps_rollup():
    assert C.parse_smaps_rollup(SMAPS) == {"rss_kib": 4096, "pss_kib": 3000, "pss_anon_kib": 2000,
                                           "pss_file_kib": 900, "pss_shmem_kib": 100, "swap_kib": 0}


def test_parse_proc_stat_name_with_parens():
    st = C.parse_proc_stat(STAT)
    assert st == {"comm": "python3 (x) y", "minflt": 5000, "majflt": 7, "utime": 300, "stime": 45, "threads": 9,
                  "start": 777}


def test_parse_pressure_and_cpu():
    psi = C.parse_pressure("some avg10=1.50 avg60=0.00 avg300=0.00 total=123\nfull avg10=0.00 avg60=0.00 "
                           "avg300=0.00 total=45\n")
    assert psi["some"]["avg10"] == 1.5 and psi["full"]["total"] == 45
    assert C.parse_proc_cpu("cpu  10 1 5 100 4 1 1 2 0 0\ncpu0 1 1 1 1 1 1 1 1 0 0\n") == {"total": 124, "idle": 104}


def make_cg(root, name, pids, **files):
    d = root / name
    d.mkdir(parents=True)
    (d / "cgroup.procs").write_text("".join("%d\n" % p for p in pids))
    defaults = {"memory.current": "1048576\n", "memory.peak": "2097152\n", "memory.max": "max\n",
                "memory.events": "low 0\nhigh 0\nmax 2\noom 1\noom_kill 1\n",
                "memory.stat": "anon 100\nfile 200\nkernel 5\nworkingset_refault_file 3\nunknown 9\n",
                "cpu.stat": "usage_usec 5000000\nnr_periods 10\nnr_throttled 2\nthrottled_usec 900\n",
                "pids.current": "3\n", "memory.pressure": "some avg10=0.00 avg60=0.00 avg300=0.00 total=10\n",
                "cpu.pressure": "some avg10=0.00 avg60=0.00 avg300=0.00 total=20\n"}
    defaults.update({k.replace("_", "."): v for k, v in files.items()})
    for k, v in defaults.items():
        (d / k).write_text(v)
    return d


def make_proc(root, pid):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "stat").write_text(STAT.replace("1234", str(pid), 1))
    (d / "smaps_rollup").write_text(SMAPS)


@pytest.fixture
def fs(tmp_path):
    cg, proc = tmp_path / "cg", tmp_path / "proc"
    sl = make_cg(cg, "jetson.slice", [])
    make_cg(sl, "jetson-a.service", [11])
    make_cg(sl / "jetson-a.service", "sub", [12])            # вложенная группа — её процессы тоже юнита
    make_proc(proc, 11)
    make_proc(proc, 12)
    make_cg(sl, "jetson-b.service", [13], memory_max="104857600\n")   # процесс 13 уже вышел
    (proc / "loadavg").write_text("1.5 0.5 0.2 1/100 999\n")
    (proc / "meminfo").write_text("MemTotal: 8000000 kB\nMemAvailable: 4000000 kB\n")
    (proc / "stat").write_text("cpu  10 0 5 100 4 0 0 0 0 0\n")
    units = {"slice": str(sl), "units": {
        "a": {"unit": "jetson-a.service", "role": "measured", "cgroup": str(sl / "jetson-a.service")},
        "b": {"unit": "jetson-b.service", "role": "measured", "cgroup": str(sl / "jetson-b.service")},
        "gone": {"unit": "jetson-gone.service", "role": "proxy", "cgroup": str(sl / "jetson-gone.service")}}}
    return units, str(proc)


def test_sample_units_procs_and_system(fs):
    units, proc = fs
    s = C.sample(units, proc)
    a = s["units"]["a"]
    assert [p["pid"] for p in a["procs"]] == [11, 12]
    assert a["procs"][0]["pss_kib"] == 3000 and a["procs"][0]["majflt"] == 7
    assert a["cg"]["memory_max"] is None and a["cg"]["events"]["oom_kill"] == 1
    assert a["cg"]["stat"] == {"anon": 100, "file": 200, "kernel": 5, "workingset_refault_file": 3}
    assert "psi_mem" not in a["cg"] and s["slice"]["psi_mem"]["some"]["total"] == 10
    assert s["units"]["b"]["procs"] == [] and s["units"]["b"]["cg"]["memory_max"] == 104857600
    assert s["units"]["gone"] == {"cg": None, "procs": []}
    assert s["sys"] == {"load1": 1.5, "load5": 0.5, "mem_available_kib": 4000000,
                        "cpu_ticks": {"total": 119, "idle": 104}}


def test_main_writes_header_and_samples(fs, tmp_path, monkeypatch):
    units, proc = fs
    path = tmp_path / "units.json"
    path.write_text(json.dumps(units))
    out = tmp_path / "c.jsonl"
    monkeypatch.setattr(C, "procs", lambda cg, proc_root="/proc": [])
    monkeypatch.setattr(C, "system", lambda proc_root="/proc": {})
    assert C.main(["--units", str(path), "--out", str(out), "--interval", "0.5", "--duration", "1.2"]) == 0
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert rows[0]["type"] == "header" and rows[0]["interval_s"] == 0.5
    assert 2 <= len(rows) - 1 <= 4 and all(r["type"] == "sample" for r in rows[1:])


def write_pagemap(proc, pid, pages, maps=None):
    """pages: {номер виртуальной страницы: PFN или None (нет в памяти)}."""
    d = proc / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    top = max(pages) + 1
    buf = bytearray(8 * top)
    for vpn, pfn in pages.items():
        if pfn is not None:
            buf[8 * vpn:8 * vpn + 8] = ((1 << 63) | pfn).to_bytes(8, "little")
    (d / "pagemap").write_bytes(bytes(buf))
    lo, hi = min(pages) * C.PAGE, top * C.PAGE
    (d / "maps").write_text(maps or "%x-%x r-xp 00000000 00:00 0  /lib/x.so\n" % (lo, hi))


def test_unique_pages_count_shared_once(tmp_path):
    proc = tmp_path / "proc"
    write_pagemap(proc, 1, {1: 100, 2: 101, 3: None})
    write_pagemap(proc, 2, {1: 100, 2: 102})
    p1, p2 = C.pfns(1, str(proc)), C.pfns(2, str(proc))
    assert p1 == {100, 101} and p2 == {100, 102}
    assert C.unique_kib([p1, p2]) == 3 * C.PAGE // 1024           # общая страница 100 — один раз
    assert C.unique_kib([]) == 0 and C.pfns(3, str(proc)) == set()


def test_unique_pages_without_root_is_none(tmp_path):
    proc = tmp_path / "proc"
    write_pagemap(proc, 1, {1: 0, 2: 0})                           # без CAP_SYS_ADMIN ядро отдаёт PFN 0
    assert C.unique_kib([C.pfns(1, str(proc))]) is None


def test_unique_skips_huge_reservations(tmp_path):
    proc = tmp_path / "proc"
    write_pagemap(proc, 1, {1: 7}, maps="1000-2000 r--p 0 00:00 0\n1000000000-f000000000 ---p 0 00:00 0\n")
    assert C.pfns(1, str(proc)) == {7}


def test_unique_snapshot(fs, tmp_path):
    units, proc = fs
    write_pagemap(tmp_path / "proc", 11, {1: 5, 2: 6})
    write_pagemap(tmp_path / "proc", 12, {1: 5})
    snap = C.unique_snapshot(units, proc)
    assert snap["units_kib"]["a"] == 8 and snap["units_kib"]["b"] == 0 and snap["slice_unique_kib"] == 8
    assert snap["slice_pss_kib"] == 6000 and snap["slice_rss_kib"] == 8192


@pytest.mark.skipif(not os.path.exists("/proc/self/smaps_rollup"), reason="нужен Linux с cgroup v2 (ВМ sim)")
def test_live_own_cgroup():
    with open("/proc/self/cgroup") as f:
        cg = "/sys/fs/cgroup" + f.read().strip().partition("::")[2]
    me = [p for p in C.procs(cg) if p["pid"] == os.getpid()]
    assert me and me[0]["pss_kib"] > 0 and me[0]["threads"] >= 1
    while not os.path.exists(os.path.join(cg, "memory.current")):   # контроллер памяти включён не на всех уровнях
        cg = os.path.dirname(cg)
    assert C.cgroup(cg)["memory_current"] > 0 and C.system()["mem_available_kib"] > 0


def test_main_rejects_bad_interval(tmp_path):
    with pytest.raises(SystemExit) as e:
        C.main(["--units", "u", "--out", str(tmp_path / "o"), "--interval", "0.1"])
    assert e.value.code == 2
    assert not os.path.exists(tmp_path / "o")
