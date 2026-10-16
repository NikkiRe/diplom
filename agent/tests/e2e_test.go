package tests

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/local/edgectx/internal/trace"
)

// TestBinaryEndToEnd собирает cmd/edgectx (CGO_ENABLED=0), запускает его с
// конфигурацией из файла, stdio MCP-сервером (этот же тестовый бинарник в роли
// «комнаты») и фейковым LLM, проходит HTTP-канал и проверяет трассу.
func TestBinaryEndToEnd(t *testing.T) {
	if testing.Short() {
		t.Skip("e2e: сборка бинарника")
	}
	gobin, err := exec.LookPath("go")
	if err != nil {
		t.Skip("e2e: нет go для сборки бинарника (ВМ, плата)")
	}
	dir := t.TempDir()
	bin := filepath.Join(dir, "edgectx")
	build := exec.Command(gobin, "build", "-o", bin, "../cmd/edgectx")
	build.Env = append(os.Environ(), "CGO_ENABLED=0")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}
	exe, err := os.Executable()
	if err != nil {
		t.Skip(err)
	}
	fl := newFakeLLM(t, roomOracle)
	cfg := fmt.Sprintf(`
clock: {mode: virtual, epoch: "2026-10-06T00:00:00Z", start_t: 25200}
listen: 127.0.0.1:0
llm: {base_url: %q, model: fake-model, trace_headers: true, timeout_s: 10}
mcp:
  - {name: room, transport: stdio, cmd: [%q, "-test.run=^$"], env: {EDGECTX_FAKE_MCP: stdio}, timeout_s: 20}
policy:
  wake: hybrid
  heartbeat_min: 30
  gate: {debounce_s: 0, coalesce_s: 0, default: change}
userctx: {db: state/edgectx.db, reset: true}
trace: out/trace.jsonl
`, fl.URL(), exe)
	cfgPath := filepath.Join(dir, "c.yaml")
	os.WriteFile(cfgPath, []byte(cfg), 0o644)

	cmd := exec.Command(bin, "-config", cfgPath, "-run-id", "e2e")
	stderr, _ := cmd.StderrPipe()
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	exited := make(chan error, 1)
	t.Cleanup(func() {
		if cmd.ProcessState == nil {
			cmd.Process.Kill() // остановка по PID, если тест упал раньше
			<-exited
		}
	})
	addrCh := make(chan string, 1)
	var logBuf bytes.Buffer
	go func() {
		sc := bufio.NewScanner(stderr)
		for sc.Scan() {
			line := sc.Text()
			logBuf.WriteString(line + "\n")
			if i := strings.Index(line, "listening on http://"); i >= 0 {
				addrCh <- strings.Fields(line[i+len("listening on "):])[0]
			}
		}
		exited <- cmd.Wait()
	}()
	var base string
	select {
	case base = <-addrCh:
	case err := <-exited:
		t.Fatalf("edgectx exited early: %v\n%s", err, logBuf.String())
	case <-time.After(30 * time.Second):
		t.Fatalf("edgectx did not start\n%s", logBuf.String())
	}

	post := func(path, body string) map[string]any {
		t.Helper()
		resp, err := http.Post(base+path, "application/json", strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		var m map[string]any
		json.Unmarshal(b, &m)
		if resp.StatusCode/100 != 2 {
			t.Fatalf("POST %s: %d %s", path, resp.StatusCode, b)
		}
		return m
	}
	get := func(path string) map[string]any {
		t.Helper()
		resp, err := http.Get(base + path)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var m map[string]any
		json.NewDecoder(resp.Body).Decode(&m)
		return m
	}

	health := get("/v1/health")
	srv := health["servers"].([]any)[0].(map[string]any)
	if health["clock"] != "virtual" || srv["name"] != "room" || srv["protocol"] != "2025-11-25" || srv["subscribed"].(float64) != 4 {
		t.Fatalf("health %v", health)
	}
	if ids := post("/v1/event", `{"id":"e1","device":"door1","property":"open","value":true}`)["input_ids"].([]any); ids[0] != "e1" {
		t.Fatalf("event ids %v", ids)
	}
	adv := post("/v1/clock/advance", `{"to_t": 27060}`) // 07:31: heartbeat 07:30, правило for_s=300 в 07:05
	if adv["now_t"].(float64) != 27060 || adv["timers_fired"].(float64) < 2 {
		t.Fatalf("advance %v", adv)
	}
	rep := post("/v1/user", `{"id":"u1","text":"включи свет"}`)
	if rep["text"] != "Свет включён." || rep["ok"] != true {
		t.Fatalf("user reply %v", rep)
	}
	post("/v1/clock/advance", `{"to_t": 27100}`)
	m := get("/v1/metrics")
	if m["llm"].(map[string]any)["requests"].(float64) < 5 {
		t.Fatalf("metrics %v", m["llm"])
	}
	post("/v1/shutdown", "")
	select {
	case err := <-exited:
		if err != nil {
			t.Fatalf("exit: %v\n%s", err, logBuf.String())
		}
	case <-time.After(20 * time.Second):
		t.Fatalf("edgectx did not stop\n%s", logBuf.String())
	}

	recs := readTrace(t, filepath.Join(dir, "out", "trace.jsonl"))
	counts := checkTrace(t, recs)
	if recs[0].s("run_id") != "e2e" || counts[trace.RuleFired] != 1 || counts[trace.UserReply] != 1 {
		t.Fatalf("counts %v", counts)
	}
	if hb := wakeTimes(recs, "heartbeat", false); len(hb) != 1 || hb[0] != hm("07:30") {
		t.Fatalf("heartbeat %v", hb)
	}
	if _, err := os.Stat(filepath.Join(dir, "state", "edgectx.db")); err != nil {
		t.Fatalf("sqlite db not created: %v", err)
	}
}
