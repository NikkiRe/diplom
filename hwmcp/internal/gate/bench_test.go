package gate

import (
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/local/hwmcp/internal/testutil"
)

// Бенчмарк проверки шлюза — аналог research/new_topic/GAP3_artifacts/bench_gate.py на Go.
//
//	go test -run '^$' -bench . -benchmem ./internal/gate
//
// BenchmarkCheckStream проигрывает тот же поток команд, что и прототип (bench_gate.run, seed 1),
// и дополнительно к ns/op сообщает перцентили задержки одной проверки (по часам time.Now, поэтому
// в каждую отметку входит ~десятки нс накладных расходов на чтение часов). Цифры, полученные при
// разработке, — не результаты: замеры для ВКР делаются по протоколу на целевой плате.

type benchStep struct {
	cat   string
	now   float64
	state []benchState
	cmd   Command
}

type benchState struct {
	key string
	v   any
	ts  float64
}

func loadBenchSteps(b *testing.B) []benchStep {
	b.Helper()
	var steps []benchStep
	readGzLines(b, "gate_bench.jsonl.gz", func(line []byte) {
		var s struct {
			Cat   string              `json:"cat"`
			Now   float64             `json:"now"`
			State [][]json.RawMessage `json:"state"`
			Cmd   goldenCmd           `json:"cmd"`
		}
		if err := json.Unmarshal(line, &s); err != nil {
			b.Fatal(err)
		}
		st := benchStep{cat: s.Cat, now: s.Now, cmd: s.Cmd.toCommand(b)}
		for _, u := range s.State {
			var key string
			var ts float64
			_ = json.Unmarshal(u[0], &key)
			_ = json.Unmarshal(u[2], &ts)
			st.state = append(st.state, benchState{key, testutil.MustDecode(b, u[1]), ts})
		}
		steps = append(steps, st)
	})
	return steps
}

func percentile(xs []int64, q float64) int64 {
	if len(xs) == 0 {
		return 0
	}
	k := int(q/100*float64(len(xs)-1) + 0.5)
	return xs[k]
}

func BenchmarkCheckStream(b *testing.B) {
	steps := loadBenchSteps(b)
	st := NewMapState()
	g := demoGate(b, st)
	lat := map[string][]int64{}
	var all []int64
	i := 0
	b.ReportAllocs()
	for b.Loop() {
		if i == len(steps) { // поток закончился — начать заново с чистого шлюза
			b.StopTimer()
			st = NewMapState()
			g = demoGate(b, st)
			i = 0
			b.StartTimer()
		}
		s := &steps[i]
		for _, u := range s.state {
			st.Set(u.key, u.v, u.ts)
		}
		t0 := time.Now()
		g.Check(s.cmd, s.now)
		d := time.Since(t0).Nanoseconds()
		g.Tick(s.now)
		lat[s.cat] = append(lat[s.cat], d)
		all = append(all, d)
		i++
	}
	sort.Slice(all, func(a, c int) bool { return all[a] < all[c] })
	b.ReportMetric(float64(percentile(all, 50)), "p50-ns")
	b.ReportMetric(float64(percentile(all, 99)), "p99-ns")
	b.ReportMetric(float64(percentile(all, 99.9)), "p99.9-ns")

	var lines []string
	for cat, xs := range lat {
		sort.Slice(xs, func(a, c int) bool { return xs[a] < xs[c] })
		lines = append(lines, fmt.Sprintf("%-15s n=%7d p50=%6d p95=%6d p99=%6d ns", cat, len(xs),
			percentile(xs, 50), percentile(xs, 95), percentile(xs, 99)))
	}
	sort.Strings(lines)
	b.Logf("gate.Check latency by category (time.Now resolution included):\n%s", strings.Join(lines, "\n"))
}

func BenchmarkCheckAllowLED(b *testing.B) {
	g := demoGate(b, NewMapState())
	// ключей больше ёмкости журнала идемпотентности (4096): к повтору ключ уже вытеснен
	ids := make([]string, 1<<16)
	for i := range ids {
		ids[i] = "c" + strconv.Itoa(i)
	}
	c := Command{Device: "led", Tool: "set_color", Args: args(b, `{"r":10,"g":20,"b":30}`)}
	i := 0
	b.ReportAllocs()
	for b.Loop() {
		c.CmdID = ids[i%len(ids)] // путь allow с записью
		c.IssuedAt = float64(i)
		g.Check(c, float64(i))
		i++
	}
}

func BenchmarkCheckInterlockHeater(b *testing.B) {
	st := NewMapState()
	g := demoGate(b, st)
	c := Command{CmdID: "x", Device: "heater", Tool: "set_power", Args: args(b, `{"on":true}`)}
	st.Set("window_open", true, 0)
	st.Set("temp_c", 21.0, 0)
	b.ReportAllocs()
	for b.Loop() {
		if d := g.Check(c, 0); d.Reason != ReasonInterlock {
			b.Fatal(d)
		}
	}
}

func BenchmarkCheckInvalidArgs(b *testing.B) {
	g := demoGate(b, NewMapState())
	c := Command{CmdID: "x", Device: "led", Tool: "set_color", Args: args(b, `{"r":300,"g":0,"b":0}`)}
	b.ReportAllocs()
	for b.Loop() {
		if d := g.Check(c, 0); d.Reason != ReasonInvalidArgs {
			b.Fatal(d)
		}
	}
}

func BenchmarkTick(b *testing.B) {
	st := NewMapState()
	g := demoGate(b, st)
	setDefault(st, 0)
	g.Check(Command{CmdID: "f", Device: "fan", Tool: "set_speed", Args: args(b, `{"pct":20}`)}, 0)
	b.ReportAllocs()
	for b.Loop() {
		g.Tick(1.0) // аренда действует до 2.0 — событий нет
	}
}
