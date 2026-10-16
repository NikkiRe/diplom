package collect

import (
	"context"
	"errors"
	"io/fs"
	"path/filepath"
	"sort"
	"strconv"

	"github.com/local/sysmon/internal/cgroup"
	"github.com/local/sysmon/internal/procfs"
	"github.com/local/sysmon/internal/thermal"
)

// memoryStatKeys — подмножество memory.stat, которое пишется в сэмпл: объёмы по видам памяти и события
// подкачки/вытеснения, нужные для разбора прогонов с memory.max 256/512 МиБ.
var memoryStatKeys = []string{
	"anon", "file", "kernel", "kernel_stack", "pagetables", "sock", "shmem", "slab",
	"file_dirty", "file_writeback", "pgfault", "pgmajfault",
	"workingset_refault_anon", "workingset_refault_file",
}

// ProcfsCollector читает Linux /proc и cgroup v2. Корни путей задаются явно, поэтому его можно
// прогнать на фикстуре на любой ОС.
type ProcfsCollector struct {
	Proc       procfs.FS
	SysRoot    string // корень /sys (термозоны)
	CgroupRoot string // mount cgroup v2, also used to observe OOM in tree mode
	Target     Target
	ClkTck     float64 // тиков в секунду для stat (обычно 100)
	PageKiB    uint64  // размер страницы, КиБ (для RSS из stat при недоступном smaps)
	// Ctxsw — суммировать переключения контекста по /proc/<pid>/task/*/status (дороже на многопоточных
	// процессах, но иначе учитывается только главный поток).
	Ctxsw bool

	rootStart map[int]uint64 // start_ticks корней при первом опросе — защита от повторно выданного PID
	oom       *oomObservation
}

// Backend реализует Collector.
func (c *ProcfsCollector) Backend() string { return "procfs" }

// Avail реализует Collector.
func (c *ProcfsCollector) Avail() []string {
	a := []string{"rss", "vsz", "pss", "swap", "cpu", "faults", "io", "net"}
	if c.Ctxsw {
		a = append(a, "ctxsw")
	}
	if c.Target.Cgroup != "" {
		a = append(a, "cgroup")
	}
	if c.Target.System {
		a = append(a, "system")
	}
	return append(a, "thermal")
}

func errKind(err error) string {
	switch {
	case errors.Is(err, fs.ErrPermission):
		return "perm"
	case errors.Is(err, fs.ErrNotExist):
		return "missing"
	default:
		return "error"
	}
}

// Collect реализует Collector.
func (c *ProcfsCollector) Collect(ctx context.Context) (Snapshot, error) {
	var snap Snapshot
	if c.ClkTck <= 0 {
		c.ClkTck = 100
	}
	if c.PageKiB == 0 {
		c.PageKiB = 4
	}

	stats := map[int]procfs.Stat{}
	readStat := func(pid int) (procfs.Stat, bool) {
		if st, ok := stats[pid]; ok {
			return st, true
		}
		st, err := c.Proc.ReadStat(pid)
		if err != nil {
			if !procfs.IsGone(err) {
				snap.noteError("stat", err)
			}
			return st, false
		}
		stats[pid] = st
		return st, true
	}

	selected := map[int]bool{}

	// 1. Деревья процессов: полный обход /proc ради ppid (без CONFIG_PROC_CHILDREN иначе потомков не найти).
	if len(c.Target.PIDs) > 0 {
		pids, err := c.Proc.ListPIDs()
		if err != nil {
			return snap, err
		}
		parent := make(map[int]int, len(pids))
		for _, pid := range pids {
			if st, ok := readStat(pid); ok {
				parent[pid] = st.PPID
			}
		}
		roots := c.liveRoots(stats)
		snap.Roots = roots
		for _, pid := range SelectTree(parent, roots, c.Target.Children) {
			selected[pid] = true
		}
	}

	// 2. Группа cgroup: все процессы группы и вложенных групп.
	var group cgroup.Group
	if c.Target.Cgroup != "" {
		group = cgroup.Group{Path: c.Target.Cgroup}
		pids, err := group.Procs(true)
		switch {
		case errors.Is(err, fs.ErrNotExist) && !group.Exists():
			snap.CgroupGone = true
		case err != nil:
			snap.noteError("cgroup.procs", err)
		}
		for _, pid := range pids {
			selected[pid] = true
		}
	}
	for _, pid := range c.Target.ExcludePIDs {
		delete(selected, pid)
	}

	pids := make([]int, 0, len(selected))
	for pid := range selected {
		pids = append(pids, pid)
	}
	sort.Ints(pids)
	for _, pid := range pids {
		if err := ctx.Err(); err != nil {
			return snap, err
		}
		if p, ok := c.readProc(pid, readStat, &snap); ok {
			snap.Procs = append(snap.Procs, p)
		}
	}

	if c.Target.Cgroup != "" && !snap.CgroupGone {
		snap.Cgroup = c.readCgroup(group, &snap)
		if snap.Cgroup != nil {
			snap.Cgroup.Procs = len(pids)
		}
	}

	c.readNet(&snap)
	if zones := thermal.Read(c.SysRoot); len(zones) > 0 {
		snap.Thermal = map[string]float64{}
		for _, z := range zones {
			snap.Thermal[z.Name] = z.TempC
		}
	}
	if c.Target.System {
		snap.System = c.readSystem(&snap)
	}
	return snap, nil
}

// liveRoots возвращает корни, которые живы и не подменены другим процессом с тем же PID.
func (c *ProcfsCollector) liveRoots(stats map[int]procfs.Stat) []int {
	if c.rootStart == nil {
		c.rootStart = map[int]uint64{}
		for _, pid := range c.Target.PIDs {
			if st, ok := stats[pid]; ok {
				c.rootStart[pid] = st.StartTime
			}
		}
	}
	var live []int
	for _, pid := range c.Target.PIDs {
		st, ok := stats[pid]
		if !ok || st.State == 'Z' || st.State == 'X' {
			continue // зомби уже не работает: ждёт, пока родитель заберёт код выхода
		}
		start, known := c.rootStart[pid]
		if known && start != st.StartTime {
			continue // PID выдан заново другому процессу
		}
		if !known {
			c.rootStart[pid] = st.StartTime // корень появился позже первого опроса
		}
		live = append(live, pid)
	}
	return live
}

func (c *ProcfsCollector) ticks(t uint64) float64 { return float64(t) / c.ClkTck }

func (c *ProcfsCollector) readProc(pid int, readStat func(int) (procfs.Stat, bool), snap *Snapshot) (ProcSample, bool) {
	st, ok := readStat(pid)
	if !ok {
		return ProcSample{}, false
	}
	p := ProcSample{
		PID: pid, PPID: st.PPID, Comm: st.Comm, StartTicks: st.StartTime, Threads: st.NumThreads,
		VSZKiB:   st.VSize / 1024,
		CPUUserS: c.ticks(st.UTime),
		CPUSysS:  c.ticks(st.STime),
		MinFlt:   ptr(st.MinFlt),
		MajFlt:   ptr(st.MajFlt),
	}
	if st.RSSPages > 0 {
		p.RSSKiB = uint64(st.RSSPages) * c.PageKiB
	}
	children := float64(max(st.CUTime, 0)+max(st.CSTime, 0)) / c.ClkTck
	p.CPUChildrenS = &children

	// Состояние процесса между чтениями может смениться (завершение): IsGone — не ошибка, а пропуск поля.
	if sm, err := c.Proc.ReadSmaps(pid); err == nil {
		p.RSSKiB = sm.RssKiB
		p.PSSKiB = ptr(sm.PssKiB)
		p.SwapKiB = ptr(sm.SwapKiB)
		p.SwapPSSKiB = ptr(sm.SwapPssKiB)
		if sm.HasPssBreakdown {
			p.PSSAnonKiB = ptr(sm.PssAnonKiB)
			p.PSSFileKiB = ptr(sm.PssFileKiB)
			p.PSSShmemKiB = ptr(sm.PssShmemKiB)
		}
	} else if !procfs.IsGone(err) {
		snap.noteError("smaps", err)
	}
	if status, err := c.Proc.ReadStatus(pid); err == nil {
		if status.VmHWMKiB > 0 {
			p.HWMKiB = ptr(status.VmHWMKiB)
		}
		if p.PSSKiB == nil && status.VmRSSKiB > 0 {
			p.RSSKiB = status.VmRSSKiB
		}
		if !c.Ctxsw && status.HasCtxsw {
			p.Ctxsw = &Ctxsw{status.VolCtxsw, status.NonvolCtxsw} // только главный поток
		}
	} else if !procfs.IsGone(err) {
		snap.noteError("status", err)
	}
	if io, err := c.Proc.ReadIO(pid); err == nil {
		p.IO = &IOCounters{io.RChar, io.WChar, io.SyscR, io.SyscW, io.ReadBytes, io.WriteBytes, io.CancelledWriteBytes}
	} else if !procfs.IsGone(err) {
		snap.noteError("io", err)
	}
	if c.Ctxsw {
		if vol, nonvol, n, err := c.Proc.SumTaskCtxsw(pid); err == nil && n > 0 {
			p.Ctxsw = &Ctxsw{vol, nonvol}
		} else if err != nil && !procfs.IsGone(err) {
			snap.noteError("task_status", err)
		}
	}
	return p, true
}

func (c *ProcfsCollector) readCgroup(g cgroup.Group, snap *Snapshot) *CgroupSample {
	cur, err := g.MemoryCurrent()
	if err != nil {
		snap.noteError("cgroup.memory", err)
		return nil
	}
	cs := &CgroupSample{MemoryCurrentKiB: cur / 1024}
	if v, ok, err := g.MemoryPeak(); err != nil {
		snap.noteError("cgroup.memory_peak", err)
	} else if ok {
		cs.MemoryPeakKiB = ptr(v / 1024)
	}
	if v, ok, err := g.MemoryMax(); err != nil {
		snap.noteError("cgroup.memory_max", err)
	} else if ok {
		cs.MemoryMaxKiB = ptr(v / 1024)
	}
	if cpu, err := g.CPUStat(); err == nil {
		cs.CPUUsageS = float64(cpu["usage_usec"]) / 1e6
		cs.CPUUserS = float64(cpu["user_usec"]) / 1e6
		cs.CPUSysS = float64(cpu["system_usec"]) / 1e6
		cs.NrThrottled = cpu["nr_throttled"]
		cs.ThrottledS = float64(cpu["throttled_usec"]) / 1e6
	} else {
		snap.noteError("cgroup.cpu", err)
	}
	if ev, err := g.MemoryEvents(); err == nil {
		cs.MemoryEvents = ev
	} else {
		snap.noteError("cgroup.memory_events", err)
	}
	if ms, err := g.MemoryStat(); err == nil {
		cs.MemoryStat = map[string]uint64{}
		for _, k := range memoryStatKeys {
			if v, ok := ms[k]; ok {
				cs.MemoryStat[k] = v
			}
		}
	} else {
		snap.noteError("cgroup.memory_stat", err)
	}
	if io, err := g.IOStat(); err == nil {
		cs.IO = io
	} else {
		snap.noteError("cgroup.io", err)
	}
	return cs
}

// readNet читает /proc/<pid>/net/dev сетевого пространства имён измеряемой системы: первого живого
// корня, иначе первого процесса группы; при NetSelf или без процессов — netns сэмплера.
func (c *ProcfsCollector) readNet(snap *Snapshot) {
	netPID := 0
	if !c.Target.NetSelf {
		switch {
		case len(snap.Roots) > 0:
			netPID = snap.Roots[0]
		case len(snap.Procs) > 0:
			netPID = snap.Procs[0].PID
		}
	}
	devs, err := c.Proc.ReadNetDev(netPID)
	if err != nil && netPID > 0 && procfs.IsGone(err) {
		devs, err = c.Proc.ReadNetDev(0)
	}
	if err != nil {
		snap.noteError("net", err)
		return
	}
	for name, d := range devs {
		if !includeIface(name, c.Target.Ifaces) {
			continue
		}
		if snap.Net == nil {
			snap.Net = map[string]NetSample{}
		}
		snap.Net[name] = NetSample{
			RxBytes: d.RxBytes, TxBytes: d.TxBytes, RxPackets: d.RxPackets, TxPackets: d.TxPackets,
			RxErrs: d.RxErrs, TxErrs: d.TxErrs, RxDrop: d.RxDrop, TxDrop: d.TxDrop,
		}
	}
}

func (c *ProcfsCollector) readSystem(snap *Snapshot) *SystemSample {
	s := &SystemSample{}
	if data, err := c.Proc.ReadFile("stat"); err == nil {
		if cpu, err := procfs.ParseCPUStat(data); err == nil {
			t := func(v uint64) float64 { return float64(v) / c.ClkTck }
			s.CPU = map[string]float64{
				"user": t(cpu.User), "nice": t(cpu.Nice), "system": t(cpu.System), "idle": t(cpu.Idle),
				"iowait": t(cpu.IOWait), "irq": t(cpu.IRQ), "softirq": t(cpu.SoftIRQ), "steal": t(cpu.Steal),
			}
		} else {
			snap.noteError("system.stat", err)
		}
	} else {
		snap.noteError("system.stat", err)
	}
	if data, err := c.Proc.ReadFile("meminfo"); err == nil {
		m := procfs.ParseMeminfo(data)
		s.MemTotalKiB, s.MemAvailableKiB, s.MemFreeKiB = m["MemTotal"], m["MemAvailable"], m["MemFree"]
		s.CachedKiB, s.SwapTotalKiB, s.SwapFreeKiB = m["Cached"], m["SwapTotal"], m["SwapFree"]
	} else {
		snap.noteError("system.meminfo", err)
	}
	if data, err := c.Proc.ReadFile("loadavg"); err == nil {
		if la, err := procfs.ParseLoadavg(data); err == nil {
			s.Load1, s.Load5, s.Load15 = la.Load1, la.Load5, la.Load15
		}
	}
	return s
}

// CgroupOfPID возвращает каталог cgroup v2 процесса: <cgroupRoot>/<путь из /proc/<pid>/cgroup>.
func CgroupOfPID(proc procfs.FS, cgroupRoot string, pid int) (string, error) {
	data, err := proc.ReadFile(strconv.Itoa(pid), "cgroup")
	if err != nil {
		return "", err
	}
	rel, err := cgroup.ParseProcCgroup(data)
	if err != nil {
		return "", err
	}
	return filepath.Join(cgroupRoot, filepath.FromSlash(rel)), nil
}

func ptr[T any](v T) *T { return &v }
