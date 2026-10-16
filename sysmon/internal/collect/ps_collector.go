package collect

import (
	"context"
	"sort"

	"github.com/local/sysmon/internal/pstool"
)

// PSCollector — упрощённый источник для macOS (разработка): один вызов ps на сэмпл и netstat для сети.
// Нет PSS, ввода-вывода, переключений контекста и cgroup; время CPU — с точностью ps (0,01 с).
type PSCollector struct {
	Target Target
	// RunPS и RunNetstat подменяются в тестах; по умолчанию — запуск ps(1) и netstat(1).
	RunPS      func(ctx context.Context) ([]pstool.Proc, int, error)
	RunNetstat func(ctx context.Context) (map[string]pstool.NetIf, error)
}

// Backend реализует Collector.
func (c *PSCollector) Backend() string { return "ps" }

// Avail реализует Collector.
func (c *PSCollector) Avail() []string {
	if len(c.Target.Ifaces) > 0 {
		return []string{"rss", "vsz", "cpu", "net"}
	}
	return []string{"rss", "vsz", "cpu"}
}

// Collect реализует Collector.
func (c *PSCollector) Collect(ctx context.Context) (Snapshot, error) {
	var snap Snapshot
	runPS, runNet := c.RunPS, c.RunNetstat
	if runPS == nil {
		runPS = pstool.RunPS
	}
	if runNet == nil {
		runNet = pstool.RunNetstat
	}
	procs, skipped, err := runPS(ctx)
	if err != nil {
		return snap, err
	}
	if skipped > 0 {
		if snap.Errors == nil {
			snap.Errors = map[string]int{}
		}
		snap.Errors["ps:unparsed"] = skipped
	}
	byPID := make(map[int]pstool.Proc, len(procs))
	parent := make(map[int]int, len(procs))
	for _, p := range procs {
		byPID[p.PID] = p
		parent[p.PID] = p.PPID
	}
	for _, r := range c.Target.PIDs {
		if _, ok := byPID[r]; ok {
			snap.Roots = append(snap.Roots, r)
		}
	}
	exclude := map[int]bool{}
	for _, pid := range c.Target.ExcludePIDs {
		exclude[pid] = true
	}
	for _, pid := range SelectTree(parent, snap.Roots, c.Target.Children) {
		if exclude[pid] {
			continue
		}
		p := byPID[pid]
		sys := p.CPUS - p.UserS
		if sys < 0 {
			sys = 0
		}
		snap.Procs = append(snap.Procs, ProcSample{
			PID: p.PID, PPID: p.PPID, Comm: p.Comm, RSSKiB: p.RSSKiB, VSZKiB: p.VSZKiB,
			CPUUserS: p.UserS, CPUSysS: sys,
		})
	}
	sort.Slice(snap.Procs, func(i, j int) bool { return snap.Procs[i].PID < snap.Procs[j].PID })

	// Счётчики интерфейсов macOS — общие для всего хоста и к агенту не относятся; читаем их, только если
	// интерфейсы названы явно (--iface), чтобы не засорять запись десятком виртуальных интерфейсов.
	if len(c.Target.Ifaces) == 0 {
		return snap, nil
	}
	ifs, err := runNet(ctx)
	if err != nil {
		snap.noteError("net", err)
	}
	for name, d := range ifs {
		if !includeIface(name, c.Target.Ifaces) {
			continue
		}
		if snap.Net == nil {
			snap.Net = map[string]NetSample{}
		}
		snap.Net[name] = NetSample{RxBytes: d.RxBytes, TxBytes: d.TxBytes, RxPackets: d.RxPackets,
			TxPackets: d.TxPackets, RxErrs: d.RxErrs, TxErrs: d.TxErrs}
	}
	return snap, nil
}
