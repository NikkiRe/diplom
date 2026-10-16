package agent

import (
	"math"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/local/edgectx/internal/mcphost"
)

// metrics — счётчики агента вида «группа.имя».
type metrics struct {
	mu sync.Mutex
	c  map[string]int
}

func newMetrics() *metrics {
	// Реплеер синхронизирует обработку уведомлений с этим монотонным счётчиком,
	// включая начальное состояние до первого MCP-уведомления.
	return &metrics{c: map[string]int{"mcp.updates": 0}}
}

func (m *metrics) inc(k string) { m.add(k, 1) }

func (m *metrics) add(k string, n int) {
	m.mu.Lock()
	m.c[k] += n
	m.mu.Unlock()
}

func (m *metrics) get(k string) int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.c[k]
}

// grouped — {"группа": {"имя": n}}.
func (m *metrics) grouped() map[string]map[string]int {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := map[string]map[string]int{}
	for k, v := range m.c {
		g, name := k, "total"
		if i := strings.IndexByte(k, '.'); i >= 0 {
			g, name = k[:i], k[i+1:]
		}
		if out[g] == nil {
			out[g] = map[string]int{}
		}
		out[g][name] = v
	}
	return out
}

// Metrics — счётчики для GET /v1/metrics. Это оперативная сводка; метрики
// экспериментов считаются по трассе (bench/metrics).
func (a *Agent) Metrics() map[string]any {
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	out := map[string]any{}
	for g, v := range a.m.grouped() {
		out[g] = v
	}
	out["inputs_total"] = a.inputCounts()
	out["budget"] = a.sched.Usage()
	out["sched"] = map[string]any{"queued": a.sched.Queued(), "running": a.sched.Running(), "stats": a.sched.Stats()}
	out["gate"] = a.gate.Stats()
	rules := a.gate.Rules()
	ids := make([]string, 0, len(rules))
	for _, r := range rules {
		ids = append(ids, r.ID)
	}
	sort.Strings(ids)
	out["rules_active"] = ids
	out["history_turns"] = a.store.TurnCount()
	out["history_cached_turns"] = len(a.store.Turns())
	out["profile_facts"] = len(a.store.Facts())
	out["hw"] = map[string]any{"devices": len(a.hw.Inventory().Devices), "log_events": a.hw.LogLen()}
	a.mu.Lock()
	link := map[string]any{"state": a.link.State(), "failures": a.link.Failures(), "deferred": len(a.deferred)}
	a.mu.Unlock()
	out["llm_link"] = link
	out["trace_records"] = a.tr.Counts()
	out["runtime"] = map[string]any{
		"goroutines": runtime.NumGoroutine(), "heap_alloc_bytes": ms.HeapAlloc, "heap_sys_bytes": ms.HeapSys,
		"sys_bytes": ms.Sys, "num_gc": ms.NumGC,
	}
	if c, ok := a.llmc.(interface{ Connections() int64 }); ok {
		out["llm_connections"] = c.Connections()
	}
	out["now_t"] = a.now()
	out["uptime_s"] = math.Round(time.Since(a.startWall).Seconds()*1000) / 1000
	out["run_id"] = a.tr.RunID()
	return out
}

// Health — состояние для GET /v1/health.
func (a *Agent) Health() map[string]any {
	servers := a.host.Servers()
	return map[string]any{
		"ok":       !a.isClosed(),
		"run_id":   a.tr.RunID(),
		"clock":    a.cfg.Clock.Mode,
		"now_t":    a.now(),
		"idle":     a.Idle(),
		"active":   a.tracker.Active(),
		"queued":   a.sched.Queued(),
		"inputs":   a.inputCounts(),
		"servers":  servers,
		"version":  a.version,
		"wake":     a.cfg.Policy.Wake,
		"budget":   a.sched.Usage().Level,
		"protocol": mcphost.ProtocolVersion,
	}
}
