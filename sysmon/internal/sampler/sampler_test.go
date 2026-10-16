package sampler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"math"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/local/sysmon/internal/collect"
	"github.com/local/sysmon/internal/procfs"
	"github.com/local/sysmon/internal/selfstat"
	"github.com/local/sysmon/internal/testfs"
)

// fakeCollector отдаёт заранее заданную последовательность сэмплов; CPU корня растёт на 0,25 с за опрос.
type fakeCollector struct {
	mu        sync.Mutex
	calls     int
	rootsFrom int // начиная с этого вызова корней нет (0 — всегда есть)
	goneFrom  int // начиная с этого вызова группа удалена
	errAt     int // на этом вызове ошибка
	onCollect func(call int)
}

func (f *fakeCollector) Backend() string { return "fake" }
func (f *fakeCollector) Avail() []string { return []string{"rss", "cpu"} }
func (f *fakeCollector) Collect(ctx context.Context) (collect.Snapshot, error) {
	f.mu.Lock()
	f.calls++
	n := f.calls
	f.mu.Unlock()
	if f.onCollect != nil {
		f.onCollect(n)
	}
	if f.errAt == n {
		return collect.Snapshot{}, errors.New("сбой источника")
	}
	children := 0.5
	snap := collect.Snapshot{
		Procs: []collect.ProcSample{
			{PID: 10, PPID: 1, Comm: "edgectx", RSSKiB: 1000, CPUUserS: 0.25 * float64(n), CPUChildrenS: &children},
			{PID: 11, PPID: 10, Comm: "hw-mcp", RSSKiB: 500},
		},
		Roots: []int{10},
	}
	if f.rootsFrom > 0 && n >= f.rootsFrom {
		snap.Roots = nil
	}
	if f.goneFrom > 0 && n >= f.goneFrom {
		snap.CgroupGone = true
	}
	return snap, nil
}

// fakeClock: каждый вызов Now сдвигает время на step — длительности в записи детерминированы.
type fakeClock struct {
	mu   sync.Mutex
	t    time.Time
	step time.Duration
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(c.step)
	return c.t
}

func fakeSelf() (selfstat.Usage, error) {
	return selfstat.Usage{UserS: 0.2, SysS: 0.1, MaxRSSKiB: 4096, RSSKiB: 3000, PSSKiB: 2500,
		VolCtxsw: 17, NonvolCtxsw: 3}, nil
}

type output struct {
	header  Header
	samples []Sample
	end     End
}

func parse(t *testing.T, data []byte) output {
	t.Helper()
	lines := strings.Split(strings.TrimRight(string(data), "\n"), "\n")
	if len(lines) < 2 {
		t.Fatalf("слишком мало строк: %q", data)
	}
	var out output
	for i, line := range lines {
		var probe struct{ Type string }
		if err := json.Unmarshal([]byte(line), &probe); err != nil {
			t.Fatalf("строка %d — не JSON: %v", i, err)
		}
		var err error
		switch {
		case i == 0 && probe.Type == "header":
			err = json.Unmarshal([]byte(line), &out.header)
		case i == len(lines)-1 && probe.Type == "end":
			err = json.Unmarshal([]byte(line), &out.end)
		case probe.Type == "sample":
			var s Sample
			err = json.Unmarshal([]byte(line), &s)
			out.samples = append(out.samples, s)
		default:
			t.Fatalf("строка %d: неожиданный тип %q", i, probe.Type)
		}
		if err != nil {
			t.Fatal(err)
		}
	}
	return out
}

func newSampler(c collect.Collector) (*Sampler, *fakeClock) {
	clk := &fakeClock{t: time.Unix(1_800_000_000, 0), step: 100 * time.Millisecond}
	return &Sampler{Collector: c, Interval: 5 * time.Millisecond, PerProc: true, RootPIDs: []int{10},
		Now: clk.Now, ReadSelf: fakeSelf}, clk
}

func TestCountAndRecords(t *testing.T) {
	s, _ := newSampler(&fakeCollector{})
	s.Count = 3
	var buf bytes.Buffer
	end, err := s.Run(context.Background(), Header{Version: "test", PageSize: 4096}, &buf)
	if err != nil {
		t.Fatal(err)
	}
	out := parse(t, buf.Bytes())
	h := out.header
	if h.Schema != Schema || h.Backend != "fake" || h.IntervalS != 0.005 || h.PageSize != 4096 || len(h.Avail) != 2 ||
		h.StartedWall != 1_800_000_000.1 {
		t.Fatalf("header: %+v", h)
	}
	if len(out.samples) != 3 || end.Reason != StopCount || out.end.Samples != 3 || out.end.Reason != StopCount {
		t.Fatalf("samples=%d end=%+v", len(out.samples), out.end)
	}
	for i, s := range out.samples {
		if s.Seq != i || s.Tree.Procs != 2 || s.Root.Procs != 1 || s.Tree.RSSKiB != 1500 || s.Root.RSSKiB != 1000 ||
			len(s.Procs) != 2 || s.Self.PSSKiB != 2500 || s.Self.CPUS == nil || !approxEq(*s.Self.CPUS, 0.3) {
			t.Fatalf("sample %d: %+v", i, s)
		}
		// Опрос длится один шаг часов (две отметки Now) — 100 мс.
		if !approxEq(s.Self.CollectMS, 100) {
			t.Fatalf("collect_ms: %v", s.Self.CollectMS)
		}
		if s.Self.Status != "available" || s.Self.Ctxsw == nil || s.Self.Ctxsw.Voluntary != 17 ||
			s.Self.Ctxsw.Involuntary != 3 || s.Self.Wakeups != nil {
			t.Fatalf("self context switches/wakeups: %+v", s.Self)
		}
	}
	first, second := out.samples[0], out.samples[1]
	if first.DtS != 0 || first.Tree.CPUPct != nil {
		t.Fatalf("у первого сэмпла нет интервала: %+v", first)
	}
	// CPU корня растёт на 0,25 с за опрос; cpu_pct = 100·Δcpu/dt.
	if second.DtS <= 0 || second.Tree.CPUPct == nil || !approxEq(*second.Tree.CPUPct, round(100*0.25/second.DtS, 3)) {
		t.Fatalf("cpu_pct: dt=%v pct=%v", second.DtS, second.Tree.CPUPct)
	}
	o := out.end.Overhead
	if o.CPUS == nil || !approxEq(*o.CPUS, 0.3) || o.MaxRSSKiB == nil || *o.MaxRSSKiB != 4096 ||
		!approxEq(o.CollectMSMean, 100) || !approxEq(o.CollectMSMax, 100) || o.CPUFrac == nil || *o.CPUFrac <= 0 ||
		o.Ctxsw == nil || o.Ctxsw.Voluntary != 17 || o.Ctxsw.Involuntary != 3 || o.Wakeups != nil {
		t.Fatalf("overhead: %+v", o)
	}
}

func approxEq(a, b float64) bool { return math.Abs(a-b) < 1e-6 }

func TestStopReasons(t *testing.T) {
	cases := []struct {
		name    string
		fc      *fakeCollector
		setup   func(s *Sampler)
		reason  string
		samples int
	}{
		{"target_exited", &fakeCollector{rootsFrom: 2}, func(s *Sampler) { s.StopWhenTargetGone = true }, StopTargetExited, 1},
		{"roots_ignored_without_flag", &fakeCollector{rootsFrom: 2}, func(s *Sampler) { s.Count = 3 }, StopCount, 3},
		{"cgroup_gone", &fakeCollector{goneFrom: 3}, nil, StopCgroupGone, 2},
		{"error", &fakeCollector{errAt: 2}, nil, StopError, 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s, _ := newSampler(tc.fc)
			if tc.setup != nil {
				tc.setup(s)
			}
			var buf bytes.Buffer
			end, err := s.Run(context.Background(), Header{}, &buf)
			if err != nil {
				t.Fatal(err)
			}
			out := parse(t, buf.Bytes())
			if end.Reason != tc.reason || len(out.samples) != tc.samples || out.end.Samples != tc.samples {
				t.Fatalf("reason=%s samples=%d", end.Reason, len(out.samples))
			}
			if tc.reason == StopError && out.end.Error != "сбой источника" {
				t.Fatalf("error: %q", out.end.Error)
			}
		})
	}
}

func TestUnavailableSelfIsUnknown(t *testing.T) {
	s, _ := newSampler(&fakeCollector{})
	s.Count = 1
	s.ReadSelf = func() (selfstat.Usage, error) { return selfstat.Usage{}, errors.New("getrusage unavailable") }
	var buf bytes.Buffer
	_, err := s.Run(context.Background(), Header{}, &buf)
	if err != nil {
		t.Fatal(err)
	}
	out := parse(t, buf.Bytes())
	self, overhead := out.samples[0].Self, out.end.Overhead
	if self.Status != "unknown" || self.CPUS != nil || self.MaxRSSKiB != nil || self.Ctxsw != nil ||
		overhead.SelfStatus != "unknown" || overhead.CPUS != nil || overhead.CPUFrac != nil || overhead.MaxRSSKiB != nil {
		t.Fatalf("unavailable self stats must not become zero: self=%+v overhead=%+v", self, overhead)
	}
	if !strings.Contains(buf.String(), `"wakeups":null`) || out.end.OOM.Status != "unknown" {
		t.Fatalf("unknown source not labelled: %s", buf.String())
	}
}

func TestTargetAlreadyExitedHasNoSamples(t *testing.T) {
	s, _ := newSampler(&fakeCollector{rootsFrom: 1})
	s.StopWhenTargetGone = true
	var buf bytes.Buffer
	_, err := s.Run(context.Background(), Header{}, &buf)
	if err != nil {
		t.Fatal(err)
	}
	out := parse(t, buf.Bytes())
	if len(out.samples) != 0 || out.end.Samples != 0 || out.end.Reason != StopTargetExited {
		t.Fatalf("terminal empty snapshot became a sample: %+v", out)
	}
}

func TestDoneAlreadyClosedDoesNotCollect(t *testing.T) {
	f := &fakeCollector{}
	s, _ := newSampler(f)
	done := make(chan struct{})
	close(done)
	s.Done = done
	var buf bytes.Buffer
	_, err := s.Run(context.Background(), Header{}, &buf)
	if err != nil {
		t.Fatal(err)
	}
	out := parse(t, buf.Bytes())
	if f.calls != 0 || len(out.samples) != 0 || out.end.Reason != StopCmdExited {
		t.Fatalf("collected after command exited: %+v calls=%d", out, f.calls)
	}
}

type exitOOMCollector struct {
	*collect.ProcfsCollector
	beforeCollect func()
}

func (c *exitOOMCollector) Collect(ctx context.Context) (collect.Snapshot, error) {
	c.beforeCollect()
	return c.ProcfsCollector.Collect(ctx)
}

func TestTreeOOMWrittenToEndWithoutEmptySample(t *testing.T) {
	root := t.TempDir()
	procRoot, cgRoot := filepath.Join(root, "proc"), filepath.Join(root, "cgroup")
	group := filepath.Join(cgRoot, "workload")
	p := testfs.Proc{PID: 10, Comm: "agent", StartTime: 1, Cgroup: "/workload"}
	testfs.WriteProcs(t, procRoot, []testfs.Proc{p})
	testfs.WriteFile(t, filepath.Join(group, "memory.events"), "oom_kill 0\n")
	calls := 0
	c := &exitOOMCollector{
		ProcfsCollector: &collect.ProcfsCollector{Proc: procfs.New(procRoot), SysRoot: root,
			CgroupRoot: cgRoot, Target: collect.Target{PIDs: []int{10}, Children: true}},
		beforeCollect: func() {
			calls++
			if calls == 2 {
				p.State = 'Z'
				testfs.WriteProcs(t, procRoot, []testfs.Proc{p})
				testfs.WriteFile(t, filepath.Join(group, "memory.events"), "oom_kill 1\n")
			}
		},
	}
	s, _ := newSampler(c)
	s.StopWhenTargetGone = true
	var buf bytes.Buffer
	_, err := s.Run(context.Background(), Header{}, &buf)
	if err != nil {
		t.Fatal(err)
	}
	out := parse(t, buf.Bytes())
	if len(out.samples) != 1 || out.end.Reason != StopTargetExited || out.end.OOM.Status != "observed" ||
		out.end.OOM.Scope != "cgroup" || *out.end.OOM.Groups[0].Delta != 1 {
		t.Fatalf("tree-mode OOM missing from end: %s", buf.String())
	}
	if out.samples[0].Cgroup != nil {
		t.Fatal("OOM reporting must not change tree selection")
	}
}

func TestSignalDurationAndDone(t *testing.T) {
	t.Run("signal", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		s, _ := newSampler(&fakeCollector{onCollect: func(n int) {
			if n == 2 {
				cancel()
			}
		}})
		var buf bytes.Buffer
		end, _ := s.Run(ctx, Header{}, &buf)
		if end.Reason != StopSignal {
			t.Fatalf("reason=%s", end.Reason)
		}
		parse(t, buf.Bytes())
	})
	t.Run("duration", func(t *testing.T) {
		s := &Sampler{Collector: &fakeCollector{}, Interval: 5 * time.Millisecond, Duration: 40 * time.Millisecond,
			ReadSelf: fakeSelf}
		var buf bytes.Buffer
		start := time.Now()
		end, _ := s.Run(context.Background(), Header{}, &buf)
		if end.Reason != StopDuration || time.Since(start) > 2*time.Second || end.Samples < 2 {
			t.Fatalf("reason=%s samples=%d", end.Reason, end.Samples)
		}
	})
	t.Run("cmd_exited", func(t *testing.T) {
		done := make(chan struct{})
		code := 3
		s, _ := newSampler(&fakeCollector{onCollect: func(n int) {
			if n == 2 {
				close(done)
			}
		}})
		s.Done = done
		s.DoneReason = func() (string, *int) { return StopCmdExited, &code }
		var buf bytes.Buffer
		end, _ := s.Run(context.Background(), Header{}, &buf)
		out := parse(t, buf.Bytes())
		if end.Reason != StopCmdExited || out.end.ExitCode == nil || *out.end.ExitCode != 3 {
			t.Fatalf("end=%+v", out.end)
		}
	})
}

type failingWriter struct{}

func (failingWriter) Write([]byte) (int, error) { return 0, errors.New("диск заполнен") }

func TestWriteErrorStops(t *testing.T) {
	s, _ := newSampler(&fakeCollector{})
	s.Count = 5
	if _, err := s.Run(context.Background(), Header{}, failingWriter{}); err == nil {
		t.Fatal("ошибка записи должна прервать цикл")
	}
}

func TestQuantile(t *testing.T) {
	v := []float64{1, 2, 3, 4, 5, 6, 7, 8, 9, 10}
	if q := quantile(v, 0.5); q != 5.5 {
		t.Fatalf("p50=%v", q)
	}
	if q := quantile(v, 0.95); q != 9.55 { // как numpy.percentile(v, 95)
		t.Fatalf("p95=%v", q)
	}
	if q := quantile([]float64{7}, 0.95); q != 7 {
		t.Fatalf("один элемент: %v", q)
	}
}
