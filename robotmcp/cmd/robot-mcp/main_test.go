package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/robotmcp/internal/fakebridge"
)

var bin string

const cancelSrv = "/navigate_to_pose/_action/cancel_goal"

func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "robot-mcp-test-")
	if err != nil {
		panic(err)
	}
	bin = filepath.Join(dir, "robot-mcp")
	if b := os.Getenv("ROBOTMCP_BIN"); b != "" { // готовый бинарник (например, linux/arm64 в VM)
		bin = b
	} else {
		cmd := exec.Command("go", "build", "-o", bin, ".")
		cmd.Env = append(os.Environ(), "CGO_ENABLED=0")
		if out, err := cmd.CombinedOutput(); err != nil {
			panic(string(out))
		}
	}
	code := m.Run()
	os.RemoveAll(dir)
	os.Exit(code)
}

func TestFlags(t *testing.T) {
	for _, bad := range [][]string{{"-transport", "x"}, {"-clock", "x"}, {"-payload", "x"},
		{"-battery", "x"}, {"-detector", "x"}, {"-cancel-timeout", "0"}, {"-wait-param", "-clock", "virtual"}, {"extra"}} {
		if _, err := parseFlags(bad, io.Discard); err == nil {
			t.Errorf("accepted %v", bad)
		}
	}
	c, err := parseFlags(nil, io.Discard)
	if err != nil || c.addr != "127.0.0.1:18110" || c.motionScan != 0 || c.poseThr != 2000 || c.cancelTO != time.Second {
		t.Fatal(c, err)
	}
}

func TestPrintTools(t *testing.T) {
	out, err := exec.Command(bin, "-print-tools").Output()
	if err != nil {
		t.Fatal(err)
	}
	var v struct {
		Tools []struct{ Name string } `json:"tools"`
	}
	if err := json.Unmarshal(out, &v); err != nil || len(v.Tools) != 5 {
		t.Fatal(string(out), err)
	}
	out, err = exec.Command(bin, "-print-inventory").Output()
	if err != nil || !bytes.Contains(out, []byte(`"mobile_robot"`)) {
		t.Fatal(string(out), err)
	}
}

// Собранный бинарник по stdio: вызовы, уведомление об итоге задачи, журнал.
func TestStdioE2E(t *testing.T) {
	fb := fakebridge.Start(fakebridge.Options{})
	defer fb.Close()
	dir := t.TempDir()
	tracePath := filepath.Join(dir, "trace.jsonl")
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	notes := make(chan map[string]any, 8)
	client := mcp.NewClient(&mcp.Implementation{Name: "e2e"}, &mcp.ClientOptions{
		ResourceUpdatedHandler: func(_ context.Context, r *mcp.ResourceUpdatedNotificationRequest) {
			notes <- r.Params.Meta
		}})
	cmd := exec.Command(bin, "-rosbridge", fb.URL, "-trace", tracePath, "-run-id", "e2e")
	cs, err := client.Connect(ctx, &mcp.CommandTransport{Command: cmd}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if cs.InitializeResult().ProtocolVersion != "2025-11-25" {
		t.Fatal(cs.InitializeResult().ProtocolVersion)
	}
	if err := cs.Subscribe(ctx, &mcp.SubscribeParams{URI: "robot://state/robot"}); err != nil {
		t.Fatal(err)
	}
	time.Sleep(200 * time.Millisecond)
	res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: "go_to", Arguments: map[string]any{"place": "corridor"}})
	if err != nil || !strings.Contains(res.Content[0].(*mcp.TextContent).Text, `"accepted":"corridor"`) {
		t.Fatal(res, err)
	}
	select {
	case n := <-notes:
		st := n["state"].(map[string]any)
		if n["robot/event"] != "task_end" || st["place"] != "corridor" {
			t.Fatal(n)
		}
	case <-ctx.Done():
		t.Fatal("no notification")
	}
	cs.Close()
	b, _ := os.ReadFile(tracePath)
	for _, typ := range []string{"run_start", "nav_cancel", "rosbridge_connect", "tool_call", "task_start", "task_end",
		"notify", "run_end"} {
		if !bytes.Contains(b, []byte(`"type":"`+typ+`"`)) {
			t.Errorf("trace has no %s", typ)
		}
	}
	if n := fb.Count("call_service", cancelSrv); n != 1 { // при подключении; в простое при завершении — нет
		t.Fatal("cancel calls", n)
	}
}

// Завершение с активной целью — по SIGTERM или концу сессии stdio: цель Nav2 отменяется до выхода; если
// Nav2 не отвечает, выход не позже -cancel-timeout (робот тогда едет дальше — это видно в журнале). В HTTP
// ни поток уведомлений подписчика, ни идущий go_to с wait=true (он ждёт конца задачи) не задерживают ни
// отмену, ни выход (код 0).
func TestShutdownCancelsGoal(t *testing.T) {
	for _, tc := range []struct {
		name                string
		http, sigterm, mute bool
	}{{name: "sigterm", sigterm: true}, {name: "stdin-eof"}, {name: "unanswered", sigterm: true, mute: true},
		{name: "http-subscribed", http: true, sigterm: true}} {
		t.Run(tc.name, func(t *testing.T) {
			fb := fakebridge.Start(fakebridge.Options{Speed: 2})
			defer fb.Close()
			tracePath := filepath.Join(t.TempDir(), "trace.jsonl")
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			args := []string{"-rosbridge", fb.URL, "-trace", tracePath, "-cancel-timeout", "300ms"}
			client := mcp.NewClient(&mcp.Implementation{Name: "e2e"}, nil)
			var cmd *exec.Cmd
			var cs *mcp.ClientSession
			var err error
			if tc.http {
				var url string
				cmd, url = startHTTP(t, append(args, "-wait-param")...)
				if cs, err = client.Connect(ctx, &mcp.StreamableClientTransport{Endpoint: url}, nil); err == nil {
					err = cs.Subscribe(ctx, &mcp.SubscribeParams{URI: "robot://state/robot"})
				}
			} else {
				cmd = exec.Command(bin, args...)
				// TerminateDuration больше ожидаемого выхода: по концу stdin процесс должен выйти сам, без SIGTERM.
				cs, err = client.Connect(ctx, &mcp.CommandTransport{Command: cmd, TerminateDuration: 10 * time.Second}, nil)
			}
			if err != nil {
				t.Fatal(err)
			}
			for !fb.Moving() { // до подключения к rosbridge go_to отвечает «robot offline»
				if _, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: "go_to", Arguments: map[string]any{"place": "window"}}); err != nil {
					t.Fatal(err)
				}
				time.Sleep(20 * time.Millisecond)
			}
			if tc.http {
				go cs.CallTool(ctx, &mcp.CallToolParams{Name: "go_to", Arguments: map[string]any{"place": "window", "wait": true}})
				waitFor(t, func() bool { return fb.Count("send_action_goal", "") == 2 })
			}
			fb.SetReply(fakebridge.Reply{Mute: tc.mute})
			t0 := time.Now()
			if tc.sigterm {
				if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
					t.Fatal(err)
				}
			}
			var exitErr error
			if tc.http {
				exit := make(chan error, 1)
				go func() { exit <- cmd.Wait() }()
				select {
				case exitErr = <-exit:
				case <-time.After(5 * time.Second):
					t.Fatal("no exit after SIGTERM")
				}
			} else {
				if tc.sigterm {
					waitTrace(t, tracePath, "run_end")
				}
				cs.Close() // конец stdin; после SIGTERM — только ожидание выхода
			}
			took := time.Since(t0)
			rec := traceRecord(t, tracePath, "nav_cancel", "shutdown")
			if tc.mute {
				if took < 300*time.Millisecond || took > 3*time.Second || rec["error"] == nil || !fb.Moving() {
					t.Fatal(took, rec)
				}
				return
			}
			if took > time.Second || exitErr != nil || rec["error"] != nil || rec["goals"] != 1.0 || fb.Moving() ||
				fb.Count("call_service", cancelSrv) != 2 {
				t.Fatal(took, exitErr, rec)
			}
		})
	}
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatal("condition not met")
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// waitTrace ждёт запись типа typ в журнале.
func waitTrace(t *testing.T, path, typ string) {
	t.Helper()
	waitFor(t, func() bool {
		b, _ := os.ReadFile(path)
		return bytes.Contains(b, []byte(`"type":"`+typ+`"`))
	})
}

// startHTTP запускает бинарник со streamable HTTP на свободном порту и возвращает процесс и URL MCP.
func startHTTP(t *testing.T, args ...string) (*exec.Cmd, string) {
	t.Helper()
	ready := filepath.Join(t.TempDir(), "ready")
	cmd := exec.Command(bin, append(args, "-transport", "http", "-addr", "127.0.0.1:0", "-ready-file", ready)...)
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cmd.Process.Kill(); _ = cmd.Wait() })
	var url string
	waitFor(t, func() bool {
		b, _ := os.ReadFile(ready)
		url = string(b)
		return strings.HasSuffix(url, "\n")
	})
	return cmd, strings.TrimSpace(url)
}

// traceRecord — запись типа typ с полем why из журнала.
func traceRecord(t *testing.T, path, typ, why string) map[string]any {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	for _, line := range bytes.Split(b, []byte("\n")) {
		var m map[string]any
		if json.Unmarshal(line, &m) == nil && m["type"] == typ && m["why"] == why {
			return m
		}
	}
	t.Fatalf("trace has no %s/%s", typ, why)
	return nil
}

// Streamable HTTP + /control/* в виртуальных часах.
func TestHTTPE2E(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip()
	}
	fb := fakebridge.Start(fakebridge.Options{Paused: true})
	defer fb.Close()
	_, url := startHTTP(t, "-rosbridge", fb.URL, "-clock", "virtual", "-clock-start", "25200", "-stuck-s", "30",
		"-detector", "stub")
	base := strings.TrimSuffix(url, "/mcp")
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	notes := make(chan map[string]any, 8)
	client := mcp.NewClient(&mcp.Implementation{Name: "e2e-http"}, &mcp.ClientOptions{
		ResourceUpdatedHandler: func(_ context.Context, r *mcp.ResourceUpdatedNotificationRequest) {
			notes <- r.Params.Meta
		}})
	cs, err := client.Connect(ctx, &mcp.StreamableClientTransport{Endpoint: url}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()
	if err := cs.Subscribe(ctx, &mcp.SubscribeParams{URI: "robot://state/robot"}); err != nil {
		t.Fatal(err)
	}
	post := func(path string, body any) map[string]any {
		b, _ := json.Marshal(body)
		resp, err := http.Post(base+path, "application/json", bytes.NewReader(b))
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var m map[string]any
		json.NewDecoder(resp.Body).Decode(&m)
		return m
	}
	time.Sleep(200 * time.Millisecond)
	// мир на паузе: цель не движется; часы адаптера двигает «реплеер» → alert stuck
	if _, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: "go_to", Arguments: map[string]any{"place": "door"}}); err != nil {
		t.Fatal(err)
	}
	if m := post("/control/clock", map[string]any{"t": 25240}); m["ok"] != true || m["active"] != true {
		t.Fatal(m)
	}
	n := <-notes
	if n["state"].(map[string]any)["alert"] != "stuck" {
		t.Fatal(n)
	}
	if m := post("/control/clock", map[string]any{"t": 1}); m["error"] == nil {
		t.Fatal("clock went back")
	}
	if m := post("/control/battery", map[string]any{"pct": 10}); m["ok"] != true {
		t.Fatal(m)
	}
	n = <-notes
	if n["state"].(map[string]any)["alert"] != "low_battery" {
		t.Fatal(n)
	}
	if m := post("/control/detector", map[string]any{"place": "door", "objects": []any{map[string]any{"label": "person"}}}); m["ok"] != true {
		t.Fatal(m)
	}
	resp, err := http.Get(base + "/control/state")
	if err != nil {
		t.Fatal(err)
	}
	var st map[string]any
	json.NewDecoder(resp.Body).Decode(&st)
	resp.Body.Close()
	if st["notifications"] != 2.0 || st["active"] != true || st["connected"] != true {
		t.Fatal(st)
	}
}
