package collect

import (
	"path/filepath"
	"testing"

	"github.com/local/sysmon/internal/procfs"
	"github.com/local/sysmon/internal/testfs"
)

func TestTreeOOMUsesSavedCgroupAfterTargetExit(t *testing.T) {
	root := t.TempDir()
	procRoot, cgRoot := filepath.Join(root, "proc"), filepath.Join(root, "cgroup")
	group := filepath.Join(cgRoot, "workload")
	testfs.WriteProcs(t, procRoot, []testfs.Proc{{PID: 10, Comm: "agent", Cgroup: "/workload"}})
	testfs.WriteFile(t, filepath.Join(group, "memory.events"), "oom_kill 2\n")
	c := &ProcfsCollector{Proc: procfs.New(procRoot), CgroupRoot: cgRoot, Target: Target{PIDs: []int{10}}}
	before := c.ObserveOOM()
	if before.Status != "not_observed" || before.Scope != "cgroup" || *before.Groups[0].Delta != 0 {
		t.Fatalf("baseline: %+v", before)
	}
	// The PID no longer has a usable cgroup entry; final observation must use
	// the saved cgroup path rather than look up a potentially recycled PID.
	testfs.WriteFile(t, filepath.Join(procRoot, "10", "cgroup"), "1:memory:/legacy\n")
	testfs.WriteFile(t, filepath.Join(group, "memory.events"), "oom_kill 3\n")
	after := c.ObserveOOM()
	if after.Status != "observed" || after.Scope != "cgroup" || len(after.Groups) != 1 ||
		*after.Groups[0].Before != 2 || *after.Groups[0].After != 3 || *after.Groups[0].Delta != 1 {
		t.Fatalf("OOM after exit not captured: %+v", after)
	}
	if c.Target.Cgroup != "" {
		t.Fatal("OOM observation changed tree selection into cgroup selection")
	}
}

func TestOOMUnknownWithoutCounterOrBaseline(t *testing.T) {
	for _, contents := range []string{"oom 0\n", "oom_kill invalid\n"} {
		t.Run(contents, func(t *testing.T) {
			group := t.TempDir()
			testfs.WriteFile(t, filepath.Join(group, "memory.events"), contents)
			c := &ProcfsCollector{Target: Target{Cgroup: group}}
			c.ObserveOOM()
			testfs.WriteFile(t, filepath.Join(group, "memory.events"), "oom_kill 0\n")
			o := c.ObserveOOM()
			if o.Status != "unknown" || o.Groups[0].Delta != nil || o.Groups[0].Before != nil || o.Groups[0].Error == "" {
				t.Fatalf("missing counter baseline reported as zero: %+v", o)
			}
		})
	}
	c := &ProcfsCollector{Proc: procfs.New(t.TempDir()), Target: Target{PIDs: []int{10}}}
	if o := c.ObserveOOM(); o.Status != "unknown" || o.Error == "" || len(o.Groups) != 0 {
		t.Fatalf("missing v2 cgroup reported as zero: %+v", o)
	}
}

func TestOOMCounterResetIsUnknown(t *testing.T) {
	group := t.TempDir()
	testfs.WriteFile(t, filepath.Join(group, "memory.events"), "oom_kill 4\n")
	c := &ProcfsCollector{Target: Target{Cgroup: group}}
	c.ObserveOOM()
	testfs.WriteFile(t, filepath.Join(group, "memory.events"), "oom_kill 1\n")
	o := c.ObserveOOM()
	if o.Status != "unknown" || o.Groups[0].Delta != nil || o.Groups[0].Error == "" {
		t.Fatalf("reset treated as valid delta: %+v", o)
	}
}

func TestOOMMissingFinalCounterIsUnknown(t *testing.T) {
	group := t.TempDir()
	testfs.WriteFile(t, filepath.Join(group, "memory.events"), "oom_kill 0\n")
	c := &ProcfsCollector{Target: Target{Cgroup: group}}
	c.ObserveOOM()
	testfs.WriteFile(t, filepath.Join(group, "memory.events"), "oom 0\n")
	o := c.ObserveOOM()
	if o.Status != "unknown" || o.Groups[0].Before == nil || o.Groups[0].After != nil ||
		o.Groups[0].Delta != nil || o.Groups[0].Error == "" {
		t.Fatalf("missing final counter reported as zero: %+v", o)
	}
}
