package trace

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/local/cammcp/internal/clock"
)

func TestEmit(t *testing.T) {
	var buf bytes.Buffer
	w := New(&buf, clock.NewVirtual(5), "r1")
	w.Emit("episode_start", map[string]any{"episode": map[string]any{"id": 1}})
	w.Emit("episode_end", map[string]any{"ts_clock": 7.5})
	w.Emit("bad", map[string]any{"f": func() {}})
	lines := strings.Split(strings.TrimSpace(buf.String()), "\n")
	if len(lines) != 3 {
		t.Fatal(buf.String())
	}
	for i, want := range []struct {
		typ string
		ts  float64
	}{{"episode_start", 5}, {"episode_end", 7.5}, {"trace_error", 5}} {
		var rec map[string]any
		if err := json.Unmarshal([]byte(lines[i]), &rec); err != nil {
			t.Fatal(err)
		}
		if rec["type"] != want.typ || rec["ts_clock"] != want.ts || rec["run_id"] != "r1" || rec["ts_wall"] == nil {
			t.Fatalf("line %d: %v", i, rec)
		}
	}
	if !strings.HasPrefix(lines[0], `{"episode":{"id":1},"run_id"`) {
		t.Fatalf("not canonical: %s", lines[0])
	}
}

type failing struct{}

func (failing) Write([]byte) (int, error) { return 0, errors.New("disk full") }

func TestErrAndNil(t *testing.T) {
	w := New(failing{}, clock.NewVirtual(0), "")
	w.Emit("x", nil)
	if w.Err() == nil {
		t.Fatal("write error lost")
	}
	var none *Writer
	none.Emit("x", nil)
	if none.Err() != nil || none.Close() != nil {
		t.Fatal("nil writer must be a no-op")
	}
}

func TestOpenAppends(t *testing.T) {
	p := filepath.Join(t.TempDir(), "t.jsonl")
	for i := 0; i < 2; i++ {
		w, err := Open(p, clock.NewVirtual(0), "r")
		if err != nil {
			t.Fatal(err)
		}
		w.Emit("run_start", nil)
		if err := w.Close(); err != nil {
			t.Fatal(err)
		}
	}
	b, _ := os.ReadFile(p)
	if n := strings.Count(string(b), "\n"); n != 2 {
		t.Fatalf("%d lines", n)
	}
}
