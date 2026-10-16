package collect

import (
	"fmt"
	"sort"
	"strings"

	"github.com/local/sysmon/internal/cgroup"
)

// OOMSample observes cgroup counters, not the reason a particular PID exited.
// A shared cgroup may contain unrelated processes; SIGKILL alone is not proof of OOM.
type OOMSample struct {
	Status string     `json:"status"` // observed | not_observed | unknown
	Scope  string     `json:"scope"`  // cgroup, never PID
	Groups []OOMGroup `json:"groups,omitempty"`
	Error  string     `json:"error,omitempty"`
}

type OOMGroup struct {
	Path   string  `json:"path"`
	Before *uint64 `json:"oom_kill_before,omitempty"`
	After  *uint64 `json:"oom_kill_after,omitempty"`
	Delta  *uint64 `json:"oom_kill_delta,omitempty"`
	Error  string  `json:"error,omitempty"`
}

type oomObservation struct {
	groups []OOMGroup
	error  string
}

// ObserveOOM establishes a baseline on its first call and reads the final counters
// on later calls. The saved paths remain usable after the target process exits.
func (c *ProcfsCollector) ObserveOOM() OOMSample {
	if c.oom == nil {
		c.oom = &oomObservation{}
		paths := map[string]bool{}
		var problems []string
		if c.Target.Cgroup != "" {
			paths[c.Target.Cgroup] = true
		} else {
			root := c.CgroupRoot
			if root == "" {
				root = "/sys/fs/cgroup"
			}
			for _, pid := range c.Target.PIDs {
				path, err := CgroupOfPID(c.Proc, root, pid)
				if err != nil {
					problems = append(problems, fmt.Sprintf("PID %d: %v", pid, err))
				} else {
					paths[path] = true
				}
			}
		}
		if len(paths) == 0 && len(problems) == 0 {
			problems = append(problems, "no cgroup v2 target")
		}
		for path := range paths {
			g := OOMGroup{Path: path}
			if n, err := oomKills(path); err != nil {
				g.Error = "baseline: " + err.Error()
			} else {
				g.Before = &n
			}
			c.oom.groups = append(c.oom.groups, g)
		}
		sort.Slice(c.oom.groups, func(i, j int) bool { return c.oom.groups[i].Path < c.oom.groups[j].Path })
		c.oom.error = strings.Join(problems, "; ")
	}
	result := OOMSample{Status: "not_observed", Scope: "cgroup", Error: c.oom.error}
	unknown := c.oom.error != ""
	observed := false
	for _, baseline := range c.oom.groups {
		g := baseline
		n, err := oomKills(g.Path)
		switch {
		case err != nil:
			g.Error = "final: " + err.Error()
			unknown = true
		case g.Before == nil:
			g.After = &n
			unknown = true
		case n < *g.Before:
			g.After = &n
			g.Error = "oom_kill counter decreased; cgroup may have been recreated"
			unknown = true
		default:
			g.After = &n
			delta := n - *g.Before
			g.Delta = &delta
			observed = observed || delta > 0
		}
		result.Groups = append(result.Groups, g)
	}
	if unknown {
		result.Status = "unknown"
	}
	if observed {
		result.Status = "observed"
	}
	return result
}

func oomKills(path string) (uint64, error) {
	events, err := (cgroup.Group{Path: path}).MemoryEvents()
	if err != nil {
		return 0, err
	}
	n, ok := events["oom_kill"]
	if !ok {
		return 0, fmt.Errorf("memory.events has no oom_kill counter")
	}
	return n, nil
}
