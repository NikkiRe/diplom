package trace

import (
	"bufio"
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/local/edgectx/internal/clock"
)

func TestEmitMandatoryFields(t *testing.T) {
	var buf bytes.Buffer
	v := clock.NewVirtual(time.Date(2026, 10, 6, 0, 0, 0, 0, time.UTC), 7*time.Hour)
	w := New(&buf, "run-1", v, 0)
	w.Emit(Input, F{"kind": "user", "id": "u1", "type": "evil", "run_id": "evil"})
	v.AdvanceTo(v.Now().Add(90*time.Second), nil)
	w.Emit(Wake, F{"reason": "user", "suppressed": false})

	lines := bytes.Split(bytes.TrimSpace(buf.Bytes()), []byte("\n"))
	if len(lines) != 2 {
		t.Fatalf("lines=%d", len(lines))
	}
	var r0, r1 map[string]any
	if err := json.Unmarshal(lines[0], &r0); err != nil {
		t.Fatal(err)
	}
	json.Unmarshal(lines[1], &r1)
	for _, k := range []string{"ts_wall", "ts_clock", "type", "run_id", "seq"} {
		if _, ok := r0[k]; !ok {
			t.Errorf("missing %s", k)
		}
	}
	if r0["type"] != "input" || r0["run_id"] != "run-1" {
		t.Fatalf("mandatory fields overridden: %v", r0)
	}
	if r0["ts_clock"].(float64) != 25200 || r1["ts_clock"].(float64) != 25290 {
		t.Fatalf("ts_clock %v %v", r0["ts_clock"], r1["ts_clock"])
	}
	if r1["seq"].(float64) != 2 {
		t.Fatal("seq")
	}
	if w.Counts()[Input] != 1 || w.Counts()[Wake] != 1 {
		t.Fatal("counts")
	}
}

func TestOpenFileConcurrent(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "sub", "trace.jsonl")
	w, err := Open(p, "r", clock.NewReal(time.Time{}), time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			for j := 0; j < 50; j++ {
				w.Emit(Input, F{"i": i, "j": j, "text": "привет"})
			}
		}(i)
	}
	wg.Wait()
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	f, _ := os.Open(p)
	defer f.Close()
	sc := bufio.NewScanner(f)
	n := 0
	seen := map[float64]bool{}
	for sc.Scan() {
		var r map[string]any
		if err := json.Unmarshal(sc.Bytes(), &r); err != nil {
			t.Fatalf("bad line %q: %v", sc.Text(), err)
		}
		seen[r["seq"].(float64)] = true
		n++
	}
	if n != 400 || len(seen) != 400 {
		t.Fatalf("n=%d unique seq=%d", n, len(seen))
	}
}

func TestDiscard(t *testing.T) {
	w, err := Open("", "r", clock.NewReal(time.Time{}), 0)
	if err != nil {
		t.Fatal(err)
	}
	w.Emit(RunEnd, nil)
	if w.Counts()[RunEnd] != 1 {
		t.Fatal("count")
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestUnmarshalableFieldKeepsRecord(t *testing.T) {
	var buf bytes.Buffer
	w := New(&buf, "r", clock.NewReal(time.Time{}), 0)
	w.Emit(Error, F{"bad": make(chan int)})
	var r map[string]any
	if err := json.Unmarshal(bytes.TrimSpace(buf.Bytes()), &r); err != nil {
		t.Fatal(err)
	}
	if r["type"] != "error" || r["trace_error"] == nil {
		t.Fatalf("record %v", r)
	}
}

// lockedBuffer — буфер, безопасный для записи из таймера сброса.
type lockedBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (l *lockedBuffer) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.Write(p)
}

func (l *lockedBuffer) lines() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return bytes.Count(l.b.Bytes(), []byte("\n"))
}

func TestBufferedFlush(t *testing.T) {
	var out lockedBuffer
	w := New(&out, "r", clock.NewReal(time.Time{}), 30*time.Millisecond)
	w.Emit(RunStart, nil)
	if out.lines() != 1 {
		t.Fatal("run_start must be flushed at once")
	}
	w.Emit(Input, F{"id": "a"})
	w.Emit(Input, F{"id": "b"})
	if out.lines() != 1 {
		t.Fatal("ordinary records must wait for the flush period")
	}
	deadline := time.Now().Add(2 * time.Second)
	for out.lines() != 3 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if out.lines() != 3 {
		t.Fatalf("buffer not flushed by the timer: %d lines", out.lines())
	}
	w.mu.Lock()
	idle := w.flushT == nil
	w.mu.Unlock()
	if !idle {
		t.Fatal("no flush timer may stay armed for an empty buffer")
	}
	w.Emit(Input, F{"id": "c"})
	w.Emit(RunEnd, nil)
	if out.lines() != 5 {
		t.Fatal("run_end must flush everything before it")
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
}
