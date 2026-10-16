package cgroup

import (
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"testing"

	"github.com/local/sysmon/internal/testfs"
)

// Реальная группа systemd-run --user --scope -p MemoryMax=256M из фикстуры Ubuntu 24.04 (ядро 6.8).
var realGroup = filepath.Join("..", "..", "testdata", "real_ubuntu2404_arm64", "sys", "fs", "cgroup",
	"user.slice", "user-501.slice", "user@501.service", "app.slice", "sysmon-fx-14017.scope")

func TestRealGroup(t *testing.T) {
	g := Group{Path: realGroup}
	cur, err := g.MemoryCurrent()
	if err != nil || cur != 29622272 {
		t.Fatalf("memory.current = %d, %v", cur, err)
	}
	peak, ok, err := g.MemoryPeak()
	if err != nil || !ok || peak != 29720576 {
		t.Fatalf("memory.peak = %d %v %v", peak, ok, err)
	}
	max, ok, err := g.MemoryMax()
	if err != nil || !ok || max != 256<<20 {
		t.Fatalf("memory.max = %d %v %v", max, ok, err)
	}
	cpu, err := g.CPUStat()
	if err != nil || cpu["usage_usec"] != 11821 || cpu["user_usec"] != 4925 || cpu["system_usec"] != 6896 {
		t.Fatalf("cpu.stat = %v %v", cpu, err)
	}
	ev, err := g.MemoryEvents()
	if err != nil || ev["oom_kill"] != 0 || len(ev) != 6 {
		t.Fatalf("memory.events = %v %v", ev, err)
	}
	ms, err := g.MemoryStat()
	if err != nil || ms["anon"] != 29265920 || ms["kernel_stack"] != 32768 {
		t.Fatalf("memory.stat anon=%d kernel_stack=%d %v", ms["anon"], ms["kernel_stack"], err)
	}
	io, err := g.IOStat()
	if err != nil || len(io) != 0 {
		t.Fatalf("io.stat (пустой — блочного IO не было) = %v %v", io, err)
	}
	pids, err := g.Procs(true)
	if err != nil || !reflect.DeepEqual(pids, []int{4540, 4542, 4543}) {
		t.Fatalf("procs = %v %v", pids, err)
	}
}

func TestNoLimitAndOldKernel(t *testing.T) {
	dir := t.TempDir()
	testfs.WriteCgroup(t, dir, testfs.Cgroup{Procs: []int{1}, MemCurrent: 4096, MemMax: "max"})
	g := Group{Path: dir}
	if _, ok, err := g.MemoryMax(); ok || err != nil {
		t.Fatalf("memory.max=max: ok=%v err=%v", ok, err)
	}
	if _, ok, err := g.MemoryPeak(); ok || err != nil { // нет файла memory.peak (ядро < 5.19)
		t.Fatalf("memory.peak отсутствует: ok=%v err=%v", ok, err)
	}
	os.Remove(filepath.Join(dir, "memory.max")) // корневая группа: файла нет
	if _, ok, err := g.MemoryMax(); ok || err != nil {
		t.Fatalf("нет memory.max: ok=%v err=%v", ok, err)
	}
	os.Remove(filepath.Join(dir, "io.stat")) // контроллер io выключен
	if io, err := g.IOStat(); err != nil || len(io) != 0 {
		t.Fatalf("нет io.stat: %v %v", io, err)
	}
}

func TestNestedProcs(t *testing.T) {
	dir := t.TempDir()
	testfs.WriteCgroup(t, dir, testfs.Cgroup{Procs: []int{10}})
	testfs.WriteCgroup(t, filepath.Join(dir, "hwmcp.scope"), testfs.Cgroup{Procs: []int{11, 12}})
	testfs.WriteCgroup(t, filepath.Join(dir, "sub", "deeper"), testfs.Cgroup{Procs: []int{13}})
	pids, err := Group{Path: dir}.Procs(true)
	sort.Ints(pids)
	if err != nil || !reflect.DeepEqual(pids, []int{10, 11, 12, 13}) {
		t.Fatalf("рекурсивно: %v %v", pids, err)
	}
	pids, err = Group{Path: dir}.Procs(false)
	if err != nil || !reflect.DeepEqual(pids, []int{10}) {
		t.Fatalf("без вложенных: %v %v", pids, err)
	}
}

func TestParseIOStat(t *testing.T) {
	data := "8:0 rbytes=100 wbytes=200 rios=1 wios=2 dbytes=0 dios=0\n179:0 rbytes=5 wbytes=7 rios=1 wios=1 dbytes=0 dios=0\n"
	got := ParseIOStat([]byte(data))
	if got["rbytes"] != 105 || got["wbytes"] != 207 || got["rios"] != 2 || got["wios"] != 3 {
		t.Fatalf("io.stat: %v", got)
	}
}

func TestParseProcCgroup(t *testing.T) {
	p, err := ParseProcCgroup([]byte("0::/user.slice/user-501.slice/session-6.scope\n"))
	if err != nil || p != "/user.slice/user-501.slice/session-6.scope" {
		t.Fatalf("%q %v", p, err)
	}
	// Гибридная иерархия (v1 + v2): берётся строка 0::.
	p, err = ParseProcCgroup([]byte("12:memory:/x\n1:name=systemd:/y\n0::/edgectx.slice\n"))
	if err != nil || p != "/edgectx.slice" {
		t.Fatalf("%q %v", p, err)
	}
	if _, err := ParseProcCgroup([]byte("4:memory:/only-v1\n")); err == nil {
		t.Fatal("только v1 — ожидалась ошибка")
	}
}
