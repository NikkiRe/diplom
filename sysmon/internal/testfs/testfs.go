// Package testfs строит синтетические деревья /proc и cgroup v2 для тестов. Формат файлов повторяет
// вывод ядра (fs/proc/array.c, task_mmu.c, base.c) — сверено с реальной фикстурой
// testdata/real_ubuntu2404_arm64 (Ubuntu 24.04, ядро 6.8, arm64).
package testfs

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// Rollup — содержимое smaps_rollup (КиБ).
type Rollup struct {
	Rss, Pss, PssAnon, PssFile, PssShmem, Swap, SwapPss uint64
	NoBreakdown                                         bool // без Pss_Anon/Pss_File/Pss_Shmem (ядра 4.14–5.9)
}

// VMA — одна область построчного smaps (КиБ).
type VMA struct{ Rss, Pss, Swap, SwapPss uint64 }

// Task — поток процесса.
type Task struct {
	TID         int
	Vol, Nonvol uint64
}

// Proc — процесс фикстуры.
type Proc struct {
	PID, PPID      int
	Comm           string
	State          byte // 0 → 'S'
	UTime, STime   uint64
	CUTime, CSTime int64
	MinFlt, MajFlt uint64
	StartTime      uint64
	VSizeBytes     uint64
	RSSPages       int64
	VmRSS, VmHWM   uint64

	Rollup *Rollup // nil — нет smaps_rollup (старое ядро) или процесс без доступа
	Smaps  []VMA   // построчный smaps (если задан)
	Tasks  []Task  // потоки; пусто — один поток с TID = PID без переключений
	IO     *[7]uint64
	NetDev string // содержимое /proc/<pid>/net/dev
	Cgroup string // путь группы v2 ("/edgectx.slice")
	// Unreadable — имена файлов, которые создаются с правами 000 (EACCES для непривилегированного теста).
	Unreadable []string
}

// WriteFile пишет файл, создавая каталоги.
func WriteFile(t testing.TB, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

// StatLine формирует /proc/<pid>/stat (52 поля, как в ядре 6.x).
func StatLine(p Proc) string {
	state := p.State
	if state == 0 {
		state = 'S'
	}
	threads := len(p.Tasks)
	if threads == 0 {
		threads = 1
	}
	// Поля 3..52; неиспользуемые заполняются правдоподобными значениями.
	f := []string{
		string(state), strconv.Itoa(p.PPID), strconv.Itoa(p.PID), strconv.Itoa(p.PID), "0", "-1", "4194304",
		u(p.MinFlt), "0", u(p.MajFlt), "0", u(p.UTime), u(p.STime), i(p.CUTime), i(p.CSTime), "20", "0",
		strconv.Itoa(threads), "0", u(p.StartTime), u(p.VSizeBytes), i(p.RSSPages), "18446744073709551615",
	}
	for len(f) < 50 {
		f = append(f, "0")
	}
	return fmt.Sprintf("%d (%s) %s\n", p.PID, p.Comm, strings.Join(f, " "))
}

func u(v uint64) string { return strconv.FormatUint(v, 10) }
func i(v int64) string  { return strconv.FormatInt(v, 10) }

func statusText(name string, p Proc, vol, nonvol uint64) string {
	threads := len(p.Tasks)
	if threads == 0 {
		threads = 1
	}
	return fmt.Sprintf("Name:\t%s\nUmask:\t0022\nState:\tS (sleeping)\nTgid:\t%d\nPid:\t%d\nPPid:\t%d\n"+
		"VmPeak:\t   %d kB\nVmSize:\t   %d kB\nVmHWM:\t   %d kB\nVmRSS:\t   %d kB\nRssAnon:\t   %d kB\n"+
		"Threads:\t%d\nvoluntary_ctxt_switches:\t%d\nnonvoluntary_ctxt_switches:\t%d\n",
		name, p.PID, p.PID, p.PPID, p.VSizeBytes/1024, p.VSizeBytes/1024, p.VmHWM, p.VmRSS, p.VmRSS,
		threads, vol, nonvol)
}

func rollupText(r Rollup) string {
	var b strings.Builder
	b.WriteString("00400000-fffffce42000 ---p 00000000 00:00 0                              [rollup]\n")
	fmt.Fprintf(&b, "Rss:            %8d kB\nPss:            %8d kB\nPss_Dirty:      %8d kB\n", r.Rss, r.Pss, r.PssAnon)
	if !r.NoBreakdown {
		fmt.Fprintf(&b, "Pss_Anon:       %8d kB\nPss_File:       %8d kB\nPss_Shmem:      %8d kB\n", r.PssAnon, r.PssFile, r.PssShmem)
	}
	fmt.Fprintf(&b, "Shared_Clean:   %8d kB\nAnonymous:      %8d kB\nSwap:           %8d kB\nSwapPss:        %8d kB\nLocked:                0 kB\n",
		r.Rss-r.Pss, r.PssAnon, r.Swap, r.SwapPss)
	return b.String()
}

func smapsText(vmas []VMA) string {
	var b strings.Builder
	for n, v := range vmas {
		fmt.Fprintf(&b, "%08x-%08x r-xp 00000000 fd:01 %d    /usr/lib/lib%d.so\n", n*0x10000, n*0x10000+0x1000, 100+n, n)
		fmt.Fprintf(&b, "Size:               %d kB\nKernelPageSize:        4 kB\nRss:            %d kB\nPss:            %d kB\n", v.Rss+4, v.Rss, v.Pss)
		fmt.Fprintf(&b, "Shared_Clean:   %d kB\nSwap:           %d kB\nSwapPss:        %d kB\nVmFlags: rd ex mr mw me\n", v.Rss-v.Pss, v.Swap, v.SwapPss)
	}
	return b.String()
}

// WriteProcs создаёт /proc/<pid>/... для каждого процесса под root.
func WriteProcs(t testing.TB, root string, procs []Proc) {
	t.Helper()
	for _, p := range procs {
		dir := filepath.Join(root, strconv.Itoa(p.PID))
		WriteFile(t, filepath.Join(dir, "stat"), StatLine(p))
		tasks := p.Tasks
		if len(tasks) == 0 {
			tasks = []Task{{TID: p.PID}}
		}
		WriteFile(t, filepath.Join(dir, "status"), statusText(p.Comm, p, tasks[0].Vol, tasks[0].Nonvol))
		for _, task := range tasks {
			WriteFile(t, filepath.Join(dir, "task", strconv.Itoa(task.TID), "status"),
				statusText(p.Comm, p, task.Vol, task.Nonvol))
		}
		if p.Rollup != nil {
			WriteFile(t, filepath.Join(dir, "smaps_rollup"), rollupText(*p.Rollup))
		}
		if p.Smaps != nil {
			WriteFile(t, filepath.Join(dir, "smaps"), smapsText(p.Smaps))
		}
		if p.IO != nil {
			v := p.IO
			WriteFile(t, filepath.Join(dir, "io"), fmt.Sprintf(
				"rchar: %d\nwchar: %d\nsyscr: %d\nsyscw: %d\nread_bytes: %d\nwrite_bytes: %d\ncancelled_write_bytes: %d\n",
				v[0], v[1], v[2], v[3], v[4], v[5], v[6]))
		}
		if p.NetDev != "" {
			WriteFile(t, filepath.Join(dir, "net", "dev"), p.NetDev)
		}
		if p.Cgroup != "" {
			WriteFile(t, filepath.Join(dir, "cgroup"), "0::"+p.Cgroup+"\n")
		}
		for _, name := range p.Unreadable {
			if err := os.Chmod(filepath.Join(dir, name), 0); err != nil {
				t.Fatal(err)
			}
		}
	}
}

// NetDev формирует /proc/net/dev с заданными интерфейсами: имя → {rx_bytes, rx_packets, tx_bytes, tx_packets}.
func NetDev(ifaces map[string][4]uint64) string {
	var b strings.Builder
	b.WriteString("Inter-|   Receive                                                |  Transmit\n")
	b.WriteString(" face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n")
	names := make([]string, 0, len(ifaces))
	for n := range ifaces {
		names = append(names, n)
	}
	sort.Strings(names) // порядок строк для разбора не важен; сортировка — ради детерминированного файла
	for _, n := range names {
		v := ifaces[n]
		fmt.Fprintf(&b, "%6s: %d %d 0 0 0 0 0 0 %d %d 0 0 0 0 0 0\n", n, v[0], v[1], v[2], v[3])
	}
	return b.String()
}

// Cgroup описывает группу cgroup v2 фикстуры.
type Cgroup struct {
	Procs        []int
	MemCurrent   uint64 // байты
	MemPeak      uint64 // 0 — файла нет (ядро < 5.19)
	MemMax       string // "max" или число; "" — файла нет
	CPUUsageUsec uint64
	UserUsec     uint64
	SystemUsec   uint64
	NrThrottled  uint64
	OOMKill      uint64
	IOStat       string
}

// WriteCgroup создаёт файлы группы в dir.
func WriteCgroup(t testing.TB, dir string, cg Cgroup) {
	t.Helper()
	var procs strings.Builder
	for _, p := range cg.Procs {
		fmt.Fprintf(&procs, "%d\n", p)
	}
	WriteFile(t, filepath.Join(dir, "cgroup.procs"), procs.String())
	WriteFile(t, filepath.Join(dir, "memory.current"), fmt.Sprintf("%d\n", cg.MemCurrent))
	if cg.MemPeak > 0 {
		WriteFile(t, filepath.Join(dir, "memory.peak"), fmt.Sprintf("%d\n", cg.MemPeak))
	}
	if cg.MemMax != "" {
		WriteFile(t, filepath.Join(dir, "memory.max"), cg.MemMax+"\n")
	}
	WriteFile(t, filepath.Join(dir, "cpu.stat"), fmt.Sprintf(
		"usage_usec %d\nuser_usec %d\nsystem_usec %d\nnr_periods 0\nnr_throttled %d\nthrottled_usec 0\n",
		cg.CPUUsageUsec, cg.UserUsec, cg.SystemUsec, cg.NrThrottled))
	WriteFile(t, filepath.Join(dir, "memory.events"), fmt.Sprintf("low 0\nhigh 0\nmax 0\noom 0\noom_kill %d\noom_group_kill 0\n", cg.OOMKill))
	WriteFile(t, filepath.Join(dir, "memory.stat"), fmt.Sprintf("anon %d\nfile 4096\nkernel 8192\nsock 0\npgfault 10\npgmajfault 1\n", cg.MemCurrent-12288))
	WriteFile(t, filepath.Join(dir, "io.stat"), cg.IOStat)
}
