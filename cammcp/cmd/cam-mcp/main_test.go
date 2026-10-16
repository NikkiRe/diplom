package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// Тестовый бинарник сам становится cam-mcp при CAMMCP_RUN_MAIN=1 (сквозная проверка stdio).
func TestMain(m *testing.M) {
	if os.Getenv("CAMMCP_RUN_MAIN") == "1" {
		os.Args = append([]string{"cam-mcp"}, strings.Fields(os.Getenv("CAMMCP_ARGS"))...)
		main()
		os.Exit(0)
	}
	os.Exit(m.Run())
}

func TestParseFlags(t *testing.T) {
	for _, bad := range [][]string{
		{"-source", "rtsp"}, {"-source", "dir"}, {"-source", "v4l2", "-clock", "virtual"}, {"-transport", "sse"},
		{"-clock", "fake"}, {"-det-size", "160*120"}, {"-det-size", "8x8"}, {"-k", "6"}, {"-min-area", "0"},
		{"-bg-tau", "2h"}, {"-path", "mcp"}, {"-path", "/a b"}, {"-path", "/{id}"}, {"-path", "/healthz"}, {"extra"},
	} {
		if _, err := parseFlags(bad, io.Discard); err == nil {
			t.Errorf("%v must fail", bad)
		}
	}
	c, err := parseFlags([]string{"-det-size", "320x240", "-min-area", "1", "-bg-tau", "1m"}, io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	if c.det.W != 320 || c.det.H != 240 || c.det.MinArea != 0.01 || c.det.BgTau != 60 || c.v4l2W != 640 || c.protocol != "2025-11-25" {
		t.Fatalf("%+v", c)
	}
}

func TestPrintTools(t *testing.T) {
	c, err := parseFlags([]string{"-print-tools", "-trace", filepath.Join(t.TempDir(), "never.jsonl")}, io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	if err := run(context.Background(), c, &out, io.Discard); err != nil {
		t.Fatal(err)
	}
	var tools struct {
		Tools []struct{ Name string } `json:"tools"`
	}
	if err := json.Unmarshal(out.Bytes(), &tools); err != nil || len(tools.Tools) != 3 {
		t.Fatalf("%v %s", err, out.String())
	}
	if _, err := os.Stat(c.tracePath); err == nil {
		t.Fatal("print mode must not write a trace")
	}
	c, _ = parseFlags([]string{"-print-tools", "-set-detector"}, io.Discard)
	out.Reset()
	if err := run(context.Background(), c, &out, io.Discard); err != nil || !strings.Contains(out.String(), `"set_detector"`) {
		t.Fatalf("%v %s", err, out.String())
	}
	for _, bad := range [][]string{{"-snapshot-width", "32"}, {"-snapshot-quality", "99"}} {
		c, _ = parseFlags(append([]string{"-print-tools"}, bad...), io.Discard)
		if err := run(context.Background(), c, io.Discard, io.Discard); err == nil {
			t.Errorf("%v must fail", bad)
		}
	}
}

func TestVersion(t *testing.T) {
	c, err := parseFlags([]string{"-version"}, io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	if err := run(context.Background(), c, &out, io.Discard); err != nil || out.String() != "cam-mcp dev\n" {
		t.Fatalf("%v %q", err, out.String())
	}
}

type notes struct {
	mu    sync.Mutex
	metas []mcp.Meta
}

func (n *notes) handler(_ context.Context, req *mcp.ResourceUpdatedNotificationRequest) {
	n.mu.Lock()
	n.metas = append(n.metas, req.Params.Meta)
	n.mu.Unlock()
}

func (n *notes) wait(t *testing.T, count int, timeout time.Duration) []mcp.Meta {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for {
		n.mu.Lock()
		got := append([]mcp.Meta(nil), n.metas...)
		n.mu.Unlock()
		if len(got) >= count {
			return got
		}
		if time.Now().After(deadline) {
			t.Fatalf("got %d notifications, want %d", len(got), count)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func readTrace(t *testing.T, path string) []map[string]any {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var out []map[string]any
	sc := bufio.NewScanner(bytes.NewReader(b))
	for sc.Scan() {
		var rec map[string]any
		if err := json.Unmarshal(sc.Bytes(), &rec); err != nil {
			t.Fatal(err)
		}
		out = append(out, rec)
	}
	return out
}

func types(recs []map[string]any) string {
	var out []string
	for _, r := range recs {
		out = append(out, r["type"].(string))
	}
	return strings.Join(out, ",")
}

// Streamable HTTP + виртуальные часы: реплеер двигает время через /control/clock, агент получает
// уведомления о начале и конце эпизода и берёт снимок.
func TestHTTPVirtual(t *testing.T) {
	dir := t.TempDir()
	tracePath, ready := filepath.Join(dir, "cam.jsonl"), filepath.Join(dir, "url")
	c, err := parseFlags([]string{"-transport", "http", "-clock", "virtual", "-trace", tracePath, "-ready-file", ready,
		"-run-id", "e2e", "-location", "hall"}, io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- run(ctx, c, io.Discard, io.Discard) }()
	var url string
	for deadline := time.Now().Add(5 * time.Second); url == ""; time.Sleep(10 * time.Millisecond) {
		if b, err := os.ReadFile(ready); err == nil {
			url = strings.TrimSpace(string(b))
		} else if time.Now().After(deadline) {
			t.Fatal("no ready file")
		}
	}
	n := &notes{}
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "1"},
		&mcp.ClientOptions{ResourceUpdatedHandler: n.handler}).Connect(ctx, &mcp.StreamableClientTransport{Endpoint: url}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := cs.Subscribe(ctx, &mcp.SubscribeParams{URI: "cam://state/cam1"}); err != nil {
		t.Fatal(err)
	}
	base := strings.TrimSuffix(url, "/mcp")
	post := func(body string) int {
		resp, err := http.Post(base+"/control/clock", "application/json", strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}
	if code := post(`{"t": 21}`); code != http.StatusOK {
		t.Fatalf("advance: %d", code)
	}
	res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: "snapshot"})
	if err != nil || res.IsError || len(res.Content) != 2 {
		t.Fatalf("%v %+v", err, res)
	}
	if code := post(`{"t": 40}`); code != http.StatusOK {
		t.Fatalf("advance: %d", code)
	}
	metas := n.wait(t, 2, 5*time.Second)
	if metas[0]["cam/event"] != "motion_start" || metas[1]["cam/event"] != "motion_end" {
		t.Fatalf("%v", metas)
	}
	if post(`{"t": 1}`) != http.StatusConflict || post(`{}`) != http.StatusBadRequest {
		t.Fatal("clock must not go back and needs t")
	}
	resp, err := http.Get(base + "/healthz")
	if err != nil || resp.StatusCode != http.StatusOK {
		t.Fatal(err)
	}
	resp.Body.Close()
	cs.Close()
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	recs := readTrace(t, tracePath)
	got := types(recs)
	for _, want := range []string{"run_start", "episode_start", "tool_call", "episode_end", "notify", "run_end"} {
		if !strings.Contains(got, want) {
			t.Fatalf("trace has no %s: %s", want, got)
		}
	}
	first, last := recs[0], recs[len(recs)-1]
	if first["config_hash"] == nil || first["run_id"] != "e2e" || last["type"] != "run_end" {
		t.Fatalf("%v … %v", first, last)
	}
	if st := last["stats"].(map[string]any); st["episodes"] != 1.0 || st["snapshots"] != 1.0 {
		t.Fatalf("%v", st)
	}
}

// stdio и виртуальные часы: время двигает отдельный HTTP (-control-addr, адрес — в stderr), /healthz
// отдаёт режим и время часов.
func TestStdioVirtualControl(t *testing.T) {
	cmd := exec.Command(os.Args[0])
	cmd.Env = append(os.Environ(), "CAMMCP_RUN_MAIN=1", "CAMMCP_ARGS=-clock virtual -control-addr 127.0.0.1:0")
	stderr, err := cmd.StderrPipe()
	if err != nil {
		t.Fatal(err)
	}
	n := &notes{}
	ctx := context.Background()
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "1"},
		&mcp.ClientOptions{ResourceUpdatedHandler: n.handler}).Connect(ctx, &mcp.CommandTransport{Command: cmd}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()
	line := bufio.NewScanner(stderr)
	if !line.Scan() {
		t.Fatal("no control address in stderr")
	}
	base, ok := strings.CutPrefix(line.Text(), "cam-mcp: control on ")
	if !ok {
		t.Fatalf("stderr: %q", line.Text())
	}
	if err := cs.Subscribe(ctx, &mcp.SubscribeParams{URI: "cam://state/cam1"}); err != nil {
		t.Fatal(err)
	}
	resp, err := http.Post(base+"/control/clock", "application/json", strings.NewReader(`{"t": 40}`))
	if err != nil || resp.StatusCode != http.StatusOK {
		t.Fatal(err, resp)
	}
	resp.Body.Close()
	n.wait(t, 2, 5*time.Second)
	resp, err = http.Get(base + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var health map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&health); err != nil || health["ok"] != true ||
		health["clock"] != "virtual" || health["now"] != 40.0 {
		t.Fatal(err, health)
	}
}

// stdio (как дочерний процесс edgectx) и реальные часы: объект проходит перед камерой за 1,5 с; конец
// сессии завершает процесс с записью run_end.
func TestStdioRealTime(t *testing.T) {
	if testing.Short() {
		t.Skip("real time")
	}
	dir := t.TempDir()
	sceneFile, tracePath := filepath.Join(dir, "scene.json"), filepath.Join(dir, "cam.jsonl")
	scn := `{"width": 160, "height": 120, "seed": 3, "noise": 2, "jitter_px": 0.3, "light": {"base": 0.6},
	  "objects": [{"id": "p", "w": 0.2, "h": 0.6, "albedo": 0.15, "path": [[0.4, -0.12, 0.6], [1.9, 1.12, 0.6]]}]}`
	if err := os.WriteFile(sceneFile, []byte(scn), 0o644); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(os.Args[0])
	cmd.Env = append(os.Environ(), "CAMMCP_RUN_MAIN=1", "CAMMCP_ARGS=-scene "+sceneFile+" -trace "+tracePath+
		" -det-size 80x60 -idle-fps 10 -active-fps 20")
	n := &notes{}
	ctx := context.Background()
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "1"},
		&mcp.ClientOptions{ResourceUpdatedHandler: n.handler}).Connect(ctx, &mcp.CommandTransport{Command: cmd}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := cs.Subscribe(ctx, &mcp.SubscribeParams{URI: "cam://state/cam1"}); err != nil {
		t.Fatal(err)
	}
	metas := n.wait(t, 2, 6*time.Second)
	if metas[0]["cam/event"] != "motion_start" || metas[1]["state"].(map[string]any)["motion"] != false {
		t.Fatalf("%v", metas)
	}
	if err := cs.Close(); err != nil {
		t.Fatal(err)
	}
	recs := readTrace(t, tracePath)
	if last := recs[len(recs)-1]; last["type"] != "run_end" || last["stats"].(map[string]any)["frames"].(float64) < 20 {
		t.Fatalf("%v", last)
	}
}
