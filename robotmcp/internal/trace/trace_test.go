package trace

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"testing"

	"github.com/local/robotmcp/internal/clock"
)

func TestEmit(t *testing.T) {
	p := filepath.Join(t.TempDir(), "t.jsonl")
	w, err := Open(p, clock.NewVirtual(42), "r1")
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(i int) { defer wg.Done(); w.Emit("x", map[string]any{"i": i}) }(i)
	}
	wg.Wait()
	w.Close()
	w.Emit("after_close", nil)
	var nilW *Writer
	nilW.Emit("noop", nil)
	f, _ := os.Open(p)
	defer f.Close()
	n := 0
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		var m map[string]any
		if err := json.Unmarshal(sc.Bytes(), &m); err != nil {
			t.Fatal(err)
		}
		if m["type"] != "x" || m["ts_clock"] != 42.0 || m["run_id"] != "r1" {
			t.Fatal(m)
		}
		n++
	}
	if n != 20 {
		t.Fatal(n)
	}
}
