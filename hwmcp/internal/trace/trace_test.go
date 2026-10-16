package trace

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/local/hwmcp/internal/clock"
)

func TestRequiredFieldsAndCanonicalLines(t *testing.T) {
	var buf bytes.Buffer
	w := New(&buf, clock.NewVirtual(42), "r1")
	w.Emit("gate", map[string]any{"device": "relay_fan", "detail": "<&>"})
	w.Emit("notify", map[string]any{"ts_clock": 7.5})
	lines := strings.Split(strings.TrimSpace(buf.String()), "\n")
	if len(lines) != 2 || w.Count() != 2 {
		t.Fatalf("%q", buf.String())
	}
	var rec map[string]any
	if err := json.Unmarshal([]byte(lines[0]), &rec); err != nil {
		t.Fatal(err)
	}
	if rec["type"] != "gate" || rec["run_id"] != "r1" || rec["ts_clock"] != 42.0 || rec["ts_wall"] == nil {
		t.Fatalf("%v", rec)
	}
	if !strings.HasPrefix(lines[0], `{"detail":"<&>","device"`) {
		t.Fatalf("keys must be sorted and not HTML-escaped: %s", lines[0])
	}
	_ = json.Unmarshal([]byte(lines[1]), &rec)
	if rec["ts_clock"] != 7.5 {
		t.Fatal("explicit ts_clock must be kept")
	}
}

func TestNilWriterIsNoop(t *testing.T) {
	var w *Writer
	w.Emit("x", nil)
	if w.Count() != 0 || w.Err() != nil || w.Close() != nil {
		t.Fatal("nil writer")
	}
}

func TestConcurrentAppend(t *testing.T) {
	path := filepath.Join(t.TempDir(), "t.jsonl")
	w, err := Open(path, clock.NewVirtual(0), "r")
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			for j := 0; j < 200; j++ {
				w.Emit("e", map[string]any{"i": i, "j": j, "pad": strings.Repeat("x", 300)})
			}
		}(i)
	}
	wg.Wait()
	_ = w.Close()
	b, _ := os.ReadFile(path)
	lines := strings.Split(strings.TrimSpace(string(b)), "\n")
	if len(lines) != 1600 {
		t.Fatalf("%d lines", len(lines))
	}
	for _, l := range lines {
		var rec map[string]any
		if err := json.Unmarshal([]byte(l), &rec); err != nil {
			t.Fatalf("torn line: %q", l)
		}
	}
}
