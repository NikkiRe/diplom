package collect

import (
	"context"
	"math"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/local/sysmon/internal/procfs"
	"github.com/local/sysmon/internal/pstool"
	"github.com/local/sysmon/internal/testfs"
)

func pids(procs []ProcSample) []int {
	out := []int{}
	for _, p := range procs {
		out = append(out, p.PID)
	}
	return out
}

func approx(a, b float64) bool { return math.Abs(a-b) < 1e-9 }

// synthetic — дерево: init(1) → edgectx(100, 3 потока) → {hw-mcp(110), sim-mcp(120) → worker(121),
// чужой(130, smaps_rollup недоступен)}; посторонний процесс 200.
func synthetic(t *testing.T) (procRoot string, all []testfs.Proc) {
	t.Helper()
	root := t.TempDir()
	io := func(r, w uint64) *[7]uint64 { return &[7]uint64{r * 10, w * 10, 5, 6, r, w, 0} }
	all = []testfs.Proc{
		{PID: 1, PPID: 0, Comm: "systemd", Rollup: &testfs.Rollup{Rss: 9000, Pss: 3000}},
		{PID: 100, PPID: 1, Comm: "edgectx", UTime: 250, STime: 50, CUTime: 30, CSTime: 10, StartTime: 5000,
			MinFlt: 100, MajFlt: 2, VSizeBytes: 800 << 20, RSSPages: 3000, VmRSS: 12000, VmHWM: 15000,
			Rollup: &testfs.Rollup{Rss: 12000, Pss: 10000, PssAnon: 8000, PssFile: 2000},
			Tasks:  []testfs.Task{{TID: 100, Vol: 5, Nonvol: 1}, {TID: 1001, Vol: 100, Nonvol: 7}, {TID: 1002, Vol: 40, Nonvol: 2}}, IO: io(4096, 8192),
			NetDev: testfs.NetDev(map[string][4]uint64{"lo": {10, 1, 10, 1}, "veth-agent": {5000, 40, 3000, 30}})},
		{PID: 110, PPID: 100, Comm: "hw-mcp", UTime: 20, STime: 10, StartTime: 5010,
			Rollup: &testfs.Rollup{Rss: 6000, Pss: 4000, PssAnon: 3500, PssFile: 500}, IO: io(0, 0),
			Tasks: []testfs.Task{{TID: 110, Vol: 3, Nonvol: 0}}},
		{PID: 120, PPID: 100, Comm: "python3 sim-mcp", UTime: 100, STime: 0, StartTime: 5020,
			Smaps: []testfs.VMA{{Rss: 20000, Pss: 18000, Swap: 100, SwapPss: 100}}, IO: io(1, 1)},
		{PID: 121, PPID: 120, Comm: "worker", UTime: 1, StartTime: 5030,
			Rollup: &testfs.Rollup{Rss: 1000, Pss: 500, PssAnon: 500}, IO: io(0, 0)},
		{PID: 130, PPID: 100, Comm: "foreign", StartTime: 5040, VmRSS: 777, IO: io(1, 1),
			Rollup: &testfs.Rollup{Rss: 777, Pss: 700}, Unreadable: []string{"smaps_rollup", "io"}},
		{PID: 200, PPID: 1, Comm: "unrelated", UTime: 999, StartTime: 10, Rollup: &testfs.Rollup{Rss: 50000, Pss: 50000}},
	}
	testfs.WriteProcs(t, root, all)
	testfs.WriteFile(t, filepath.Join(root, "net", "dev"),
		testfs.NetDev(map[string][4]uint64{"lo": {1, 1, 1, 1}, "eth0": {111, 2, 222, 3}}))
	return root, all
}

func newCollector(root string, target Target) *ProcfsCollector {
	return &ProcfsCollector{Proc: procfs.New(root), SysRoot: filepath.Join(root, "nosys"), Target: target,
		ClkTck: 100, PageKiB: 4, Ctxsw: true}
}

func TestTreeSelectionAndTotals(t *testing.T) {
	root, _ := synthetic(t)
	c := newCollector(root, Target{PIDs: []int{100}, Children: true})
	snap, err := c.Collect(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if got := pids(snap.Procs); !reflect.DeepEqual(got, []int{100, 110, 120, 121, 130}) {
		t.Fatalf("дерево: %v", got)
	}
	if !reflect.DeepEqual(snap.Roots, []int{100}) {
		t.Fatalf("корни: %v", snap.Roots)
	}
	edge := snap.Procs[0]
	if edge.Comm != "edgectx" || !approx(edge.CPUUserS, 2.5) || !approx(edge.CPUSysS, 0.5) ||
		!approx(*edge.CPUChildrenS, 0.4) || *edge.PSSKiB != 10000 || *edge.PSSAnonKiB != 8000 ||
		edge.RSSKiB != 12000 || *edge.HWMKiB != 15000 || edge.VSZKiB != 800<<10 || edge.Threads != 3 ||
		*edge.MinFlt != 100 || *edge.MajFlt != 2 || edge.IO.ReadBytes != 4096 || edge.IO.WChar != 81920 {
		t.Fatalf("edgectx: %+v", edge)
	}
	// Переключения контекста — сумма по трём потокам, а не только главный поток (5/1).
	if *edge.Ctxsw != (Ctxsw{145, 10}) {
		t.Fatalf("ctxsw: %+v", *edge.Ctxsw)
	}
	sim := snap.Procs[2]
	if *sim.PSSKiB != 18000 || sim.PSSAnonKiB != nil || *sim.SwapPSSKiB != 100 {
		t.Fatalf("sim-mcp (откат на smaps): %+v", sim)
	}
	foreign := snap.Procs[4]
	if foreign.PSSKiB != nil || foreign.IO != nil || foreign.RSSKiB != 777 {
		t.Fatalf("чужой процесс: %+v", foreign)
	}
	if snap.Errors["smaps:perm"] != 1 || snap.Errors["io:perm"] != 1 {
		t.Skipf("права не проверяются (запуск от root?): %v", snap.Errors)
	}

	tree := Sum(snap.Procs)
	if tree.Procs != 5 || *tree.PSSKiB != 10000+4000+18000+500 || tree.RSSKiB != 12000+6000+20000+1000+777 ||
		!approx(tree.CPUS, 2.5+0.5+0.3+1.0+0.01) || !approx(tree.CPUTotalS(), tree.CPUS+0.4) ||
		*tree.PSSAnonKiB != 8000+3500+500 {
		t.Fatalf("сумма: %+v pss=%d", tree, *tree.PSSKiB)
	}
	// PSS неполна: у одного процесса нет smaps_rollup — это должно быть видно.
	if !reflect.DeepEqual(tree.Missing, map[string]int{"pss": 1, "io": 1}) {
		t.Fatalf("missing: %v", tree.Missing)
	}
	// Сеть — из netns корня (/proc/100/net/dev), lo по умолчанию исключён.
	if !reflect.DeepEqual(snap.Net, map[string]NetSample{"veth-agent": {RxBytes: 5000, RxPackets: 40, TxBytes: 3000, TxPackets: 30}}) {
		t.Fatalf("net: %+v", snap.Net)
	}
}

func TestNoChildrenExcludeAndCtxswMainThread(t *testing.T) {
	root, _ := synthetic(t)
	c := newCollector(root, Target{PIDs: []int{100}, Children: false})
	c.Ctxsw = false
	snap, _ := c.Collect(context.Background())
	if got := pids(snap.Procs); !reflect.DeepEqual(got, []int{100}) {
		t.Fatalf("без потомков: %v", got)
	}
	if *snap.Procs[0].Ctxsw != (Ctxsw{5, 1}) {
		t.Fatalf("ctxsw главного потока: %+v", *snap.Procs[0].Ctxsw)
	}
	c = newCollector(root, Target{PIDs: []int{100}, Children: true, ExcludePIDs: []int{120}})
	snap, _ = c.Collect(context.Background())
	if got := pids(snap.Procs); !reflect.DeepEqual(got, []int{100, 110, 121, 130}) {
		t.Fatalf("исключение 120 (его потомок остаётся): %v", got)
	}
}

func TestRootPIDReuseAndZombie(t *testing.T) {
	root, all := synthetic(t)
	c := newCollector(root, Target{PIDs: []int{100}, Children: true})
	if snap, _ := c.Collect(context.Background()); len(snap.Roots) != 1 {
		t.Fatal("корень должен быть жив")
	}
	// PID 100 выдан заново другому процессу (иной starttime) — это уже не наш корень.
	reused := all[1]
	reused.StartTime = 99999
	reused.Comm = "impostor"
	testfs.WriteFile(t, filepath.Join(root, "100", "stat"), testfs.StatLine(reused))
	snap, _ := c.Collect(context.Background())
	if len(snap.Roots) != 0 || len(snap.Procs) != 0 {
		t.Fatalf("подменённый PID: roots=%v procs=%v", snap.Roots, pids(snap.Procs))
	}
	// Зомби тоже не считается живым корнем.
	zombie := all[1]
	zombie.State = 'Z'
	testfs.WriteFile(t, filepath.Join(root, "100", "stat"), testfs.StatLine(zombie))
	c = newCollector(root, Target{PIDs: []int{100}, Children: true})
	if snap, _ := c.Collect(context.Background()); len(snap.Roots) != 0 {
		t.Fatalf("зомби-корень: %v", snap.Roots)
	}
}

func TestCgroupMode(t *testing.T) {
	root, _ := synthetic(t)
	cg := filepath.Join(t.TempDir(), "edgectx.slice")
	testfs.WriteCgroup(t, cg, testfs.Cgroup{Procs: []int{110}, MemCurrent: 64 << 20, MemPeak: 80 << 20,
		MemMax: "268435456", CPUUsageUsec: 2_500_000, UserUsec: 2_000_000, SystemUsec: 500_000, NrThrottled: 3,
		OOMKill: 1, IOStat: "179:0 rbytes=4096 wbytes=8192 rios=1 wios=2 dbytes=0 dios=0\n"})
	testfs.WriteCgroup(t, filepath.Join(cg, "sim.scope"), testfs.Cgroup{Procs: []int{120, 121}, MemCurrent: 1 << 20})
	c := newCollector(root, Target{Cgroup: cg})
	snap, err := c.Collect(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if got := pids(snap.Procs); !reflect.DeepEqual(got, []int{110, 120, 121}) {
		t.Fatalf("процессы группы: %v", got)
	}
	g := snap.Cgroup
	if g == nil || g.Procs != 3 || g.MemoryCurrentKiB != 64<<10 || *g.MemoryPeakKiB != 80<<10 || *g.MemoryMaxKiB != 256<<10 ||
		!approx(g.CPUUsageS, 2.5) || !approx(g.CPUUserS, 2.0) || !approx(g.CPUSysS, 0.5) || g.NrThrottled != 3 ||
		g.MemoryEvents["oom_kill"] != 1 || g.IO["wbytes"] != 8192 || g.MemoryStat["pgmajfault"] != 1 {
		t.Fatalf("cgroup: %+v", g)
	}
	if _, ok := g.MemoryStat["workingset_refault_file"]; ok {
		t.Fatal("в memory.stat фикстуры нет этого ключа — он не должен появиться")
	}
	// Без корней сеть читается из netns первого процесса группы; у 110 своего net/dev нет → netns сэмплера.
	if !reflect.DeepEqual(snap.Net, map[string]NetSample{"eth0": {RxBytes: 111, RxPackets: 2, TxBytes: 222, TxPackets: 3}}) {
		t.Fatalf("net: %+v", snap.Net)
	}
	// Группа удалена — сигнал остановки.
	c = newCollector(root, Target{Cgroup: filepath.Join(cg, "gone")})
	if snap, _ := c.Collect(context.Background()); !snap.CgroupGone || snap.Cgroup != nil {
		t.Fatalf("удалённая группа: %+v", snap)
	}
}

func TestNetSelfIfaceFilterAndSystem(t *testing.T) {
	root, _ := synthetic(t)
	testfs.WriteFile(t, filepath.Join(root, "stat"), "cpu  1522 144 587 144767 77 0 61 0 0 0\ncpu0 1 1 1 1 1 0 0 0 0 0\n")
	testfs.WriteFile(t, filepath.Join(root, "meminfo"), "MemTotal:        3999212 kB\nMemFree:         2090508 kB\nMemAvailable:    3537308 kB\nCached:          1534400 kB\nSwapTotal:             0 kB\nSwapFree:              0 kB\n")
	testfs.WriteFile(t, filepath.Join(root, "loadavg"), "0.08 0.05 0.01 1/203 5236\n")
	c := newCollector(root, Target{PIDs: []int{100}, Children: true, NetSelf: true, Ifaces: []string{"lo", "eth0"}, System: true})
	snap, _ := c.Collect(context.Background())
	if len(snap.Net) != 2 || snap.Net["lo"].RxBytes != 1 || snap.Net["eth0"].TxBytes != 222 {
		t.Fatalf("net-self с фильтром: %+v", snap.Net)
	}
	s := snap.System
	if s == nil || !approx(s.CPU["user"], 15.22) || !approx(s.CPU["idle"], 1447.67) || s.MemAvailableKiB != 3537308 ||
		s.MemTotalKiB != 3999212 || s.Load1 != 0.08 {
		t.Fatalf("system: %+v", s)
	}
}

func TestRealFixture(t *testing.T) {
	base := filepath.Join("..", "..", "testdata", "real_ubuntu2404_arm64")
	cg := filepath.Join(base, "sys", "fs", "cgroup", "user.slice", "user-501.slice", "user@501.service", "app.slice", "sysmon-fx-14017.scope")
	c := &ProcfsCollector{Proc: procfs.New(filepath.Join(base, "proc")), SysRoot: filepath.Join(base, "sys"),
		Target: Target{PIDs: []int{4540}, Children: true, Cgroup: cg}, ClkTck: 100, PageKiB: 4, Ctxsw: true}
	snap, err := c.Collect(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if got := pids(snap.Procs); !reflect.DeepEqual(got, []int{4540, 4542, 4543}) {
		t.Fatalf("процессы: %v", got)
	}
	if len(snap.Errors) != 0 {
		t.Fatalf("ошибки: %v", snap.Errors)
	}
	tree := Sum(snap.Procs)
	var wantPSS uint64
	for _, p := range snap.Procs {
		wantPSS += *p.PSSKiB
	}
	if *tree.PSSKiB != wantPSS || *snap.Procs[1].PSSKiB != 29604 || tree.Missing != nil {
		t.Fatalf("PSS дерева %d, ожидалось %d", *tree.PSSKiB, wantPSS)
	}
	// memory.current группы (байты → КиБ) больше суммы анонимной PSS: группе заряжены ещё ядро и кэш.
	if snap.Cgroup.MemoryCurrentKiB != 29622272/1024 || *snap.Cgroup.MemoryMaxKiB != 256<<10 ||
		snap.Cgroup.MemoryCurrentKiB < *tree.PSSAnonKiB {
		t.Fatalf("cgroup: %+v, pss_anon=%d", snap.Cgroup, *tree.PSSAnonKiB)
	}
	if snap.Net["eth0"].RxBytes != 42501979 || len(snap.Net) != 1 {
		t.Fatalf("net: %+v", snap.Net)
	}
}

func TestSelectTree(t *testing.T) {
	parent := map[int]int{1: 0, 2: 1, 3: 2, 4: 1, 5: 4, 6: 6 /* сам себе родитель — защита от цикла */, 7: 6}
	cases := []struct {
		roots    []int
		children bool
		want     []int
	}{
		{[]int{2}, true, []int{2, 3}},
		{[]int{4, 2}, true, []int{2, 3, 4, 5}},
		{[]int{1}, false, []int{1}},
		{[]int{6}, true, []int{6, 7}},
		{[]int{42}, true, []int{}},
	}
	for _, tc := range cases {
		if got := SelectTree(parent, tc.roots, tc.children); !reflect.DeepEqual(got, tc.want) {
			t.Errorf("roots=%v: got %v want %v", tc.roots, got, tc.want)
		}
	}
}

func TestPSCollector(t *testing.T) {
	c := &PSCollector{
		Target: Target{PIDs: []int{10}, Children: true, ExcludePIDs: []int{12}, Ifaces: []string{"en0"}},
		RunPS: func(context.Context) ([]pstool.Proc, int, error) {
			return []pstool.Proc{
				{PID: 1, PPID: 0, RSSKiB: 1, Comm: "launchd"},
				{PID: 10, PPID: 1, RSSKiB: 1000, VSZKiB: 5000, UserS: 1.5, CPUS: 2.0, Comm: "edgectx"},
				{PID: 11, PPID: 10, RSSKiB: 300, UserS: 0.1, CPUS: 0.1, Comm: "hw-mcp"},
				{PID: 12, PPID: 10, RSSKiB: 99, Comm: "sysmon"},
			}, 2, nil
		},
		RunNetstat: func(context.Context) (map[string]pstool.NetIf, error) {
			return map[string]pstool.NetIf{"en0": {RxBytes: 5, TxBytes: 6}, "utun0": {RxBytes: 1}}, nil
		},
	}
	snap, err := c.Collect(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if got := pids(snap.Procs); !reflect.DeepEqual(got, []int{10, 11}) {
		t.Fatalf("процессы: %v", got)
	}
	p := snap.Procs[0]
	if !approx(p.CPUUserS, 1.5) || !approx(p.CPUSysS, 0.5) || p.PSSKiB != nil || p.IO != nil {
		t.Fatalf("edgectx: %+v", p)
	}
	tree := Sum(snap.Procs)
	if tree.RSSKiB != 1300 || tree.PSSKiB != nil || tree.Missing != nil {
		t.Fatalf("сумма: %+v", tree)
	}
	if snap.Errors["ps:unparsed"] != 2 || len(snap.Net) != 1 || snap.Net["en0"].TxBytes != 6 {
		t.Fatalf("errors=%v net=%v", snap.Errors, snap.Net)
	}
	c.Target.Ifaces = nil // без --iface сеть хоста не пишется
	if snap, _ := c.Collect(context.Background()); snap.Net != nil {
		t.Fatalf("net без --iface: %v", snap.Net)
	}
}
