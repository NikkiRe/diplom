//go:build linux

package collect

import (
	"context"
	"os"
	"os/exec"
	"testing"
	"time"

	"github.com/local/sysmon/internal/procfs"
)

// TestLiveProcfs — проверка на настоящем /proc (Linux: Lima VM, плата): дерево из самого теста и дочернего
// sleep, правдоподобные значения всех групп полей и cgroup процесса.
func TestLiveProcfs(t *testing.T) {
	child := exec.Command("sleep", "5")
	if err := child.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = child.Process.Kill(); _ = child.Wait() }()
	time.Sleep(50 * time.Millisecond)

	proc := procfs.New("/proc")
	cg, err := CgroupOfPID(proc, "/sys/fs/cgroup", os.Getpid())
	if err != nil {
		t.Logf("cgroup v2 недоступна: %v", err)
		cg = ""
	}
	c := &ProcfsCollector{Proc: proc, SysRoot: "/sys", Target: Target{PIDs: []int{os.Getpid()}, Children: true,
		System: true}, ClkTck: 100, PageKiB: uint64(os.Getpagesize() / 1024), Ctxsw: true}
	snap, err := c.Collect(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	// Только свои процессы — ни одной ошибки чтения.
	if len(snap.Errors) != 0 {
		t.Fatalf("ошибки чтения: %v", snap.Errors)
	}
	found := map[int]bool{}
	for _, p := range snap.Procs {
		found[p.PID] = true
	}
	if !found[os.Getpid()] || !found[child.Process.Pid] {
		t.Fatalf("нет теста или sleep в дереве: %v", found)
	}
	self := snap.Procs[0]
	for _, p := range snap.Procs {
		if p.PID == os.Getpid() {
			self = p
		}
	}
	if self.PSSKiB == nil || *self.PSSKiB == 0 || self.RSSKiB < *self.PSSKiB || self.IO == nil || self.Ctxsw == nil ||
		self.Ctxsw.Voluntary == 0 || self.Threads < 2 || self.HWMKiB == nil {
		t.Fatalf("тестовый процесс: %+v", self)
	}
	if cg != "" {
		// Группа сессии (session-N.scope) содержит и чужие процессы (sshd от root): для них EACCES на
		// smaps_rollup/io ожидаем и учитываем в errors, а счётчики группы должны читаться.
		c.Target.Cgroup = cg
		gs, err := c.Collect(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		if gs.Cgroup == nil || gs.Cgroup.MemoryCurrentKiB == 0 || gs.Cgroup.CPUUsageS <= 0 || gs.Cgroup.Procs < 2 {
			t.Fatalf("cgroup %s: %+v", cg, gs.Cgroup)
		}
		for k := range gs.Errors {
			if k != "smaps:perm" && k != "io:perm" {
				t.Fatalf("неожиданная ошибка %s: %v", k, gs.Errors)
			}
		}
		t.Logf("cgroup %s: %d процессов, ошибки доступа к чужим: %v", cg, gs.Cgroup.Procs, gs.Errors)
	}
	if snap.System == nil || snap.System.MemTotalKiB == 0 || snap.System.CPU["idle"] <= 0 {
		t.Fatalf("system: %+v", snap.System)
	}
	aux, err := proc.ReadAuxv()
	if err != nil || aux[procfs.AtClkTck] == 0 || aux[procfs.AtPageSz] != uint64(os.Getpagesize()) {
		t.Fatalf("auxv: %v %v", aux, err)
	}
}
