package agent

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/edgectx/internal/config"
	"github.com/local/edgectx/internal/fakemcp"
	"github.com/local/edgectx/internal/llm"
	"github.com/local/edgectx/internal/trace"
)

func noop(llm.Request, map[string]string) (*llm.Response, error) {
	return textResp("NO_ACTION", 10), nil
}

func (f *fixture) connectMem(t *testing.T, name string, s *mcp.Server) {
	t.Helper()
	st, ct := mcp.NewInMemoryTransports()
	sess, err := s.Connect(context.Background(), st, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { sess.Close() })
	if err := f.a.Host().ConnectTransport(context.Background(), config.MCPServer{Name: name, Transport: "stdio", TimeoutS: 5}, ct); err != nil {
		t.Fatal(err)
	}
}

// all — все записи трассы по порядку.
func (f *fixture) all() []map[string]any {
	f.a.WaitIdle(5 * time.Second)
	var out []map[string]any
	for _, line := range bytes.Split(bytes.TrimSpace(f.buf.Bytes()), []byte("\n")) {
		var r map[string]any
		if json.Unmarshal(line, &r) == nil {
			out = append(out, r)
		}
	}
	return out
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timeout waiting for %s", what)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// run_start — первая запись, даже если правило истинно с самого старта, а
// сервер прислал уведомление ещё до него.
func TestRunStartIsFirstRecord(t *testing.T) {
	rules := filepath.Join(t.TempDir(), "rules.yaml")
	os.WriteFile(rules, []byte("- {id: open_door, device: door1, property: open, op: eq, value: true, action: notify, text: открыта}\n"), 0o644)
	f := build(t, "policy: {wake: gate, gate: {rules_path: "+rules+", default: ignore}}\n", noop)
	room := fakemcp.NewRoom()
	room.MetaState = true
	ctx := context.Background()
	room.Set(ctx, "door1", "open", true)
	f.connectMem(t, "room", room.Server())
	if _, err := f.a.Host().Subscribe(ctx, "room", []string{"hw://state/extra"}); err != nil {
		t.Fatal(err)
	}
	room.Set(ctx, "extra", "level", 1.0) // уведомление до Start
	waitFor(t, "early notification queued", func() bool { return f.a.tracker.Active() == 1 })
	if err := f.a.Start(ctx); err != nil {
		t.Fatal(err)
	}
	recs := f.all()
	if recs[0]["type"] != trace.RunStart {
		t.Fatalf("first record %v", recs[0])
	}
	var fired, early bool
	for _, r := range recs[1:] {
		fired = fired || (r["type"] == trace.RuleFired && r["rule_id"] == "open_door")
		early = early || (r["type"] == trace.Input && r["device"] == "extra")
	}
	if !fired || !early {
		t.Fatalf("rule fired=%v, early update=%v after run_start", fired, early)
	}
	rt := recs[0]["runtime"].(map[string]any)
	if rt["gomemlimit"].(float64) <= 0 || rt["gogc"] == nil || rt["gomaxprocs"].(float64) < 1 {
		t.Fatalf("run_start.runtime %v", rt)
	}
}

// switchable — HTTP-сервер MCP, который можно «уронить» и «перезапустить».
type switchable struct {
	mu  sync.Mutex
	h   http.Handler
	srv *httptest.Server
}

func newSwitchable(t *testing.T, s *mcp.Server) *switchable {
	sw := &switchable{}
	sw.restart(s)
	sw.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sw.mu.Lock()
		h := sw.h
		sw.mu.Unlock()
		h.ServeHTTP(w, r)
	}))
	t.Cleanup(sw.srv.Close)
	return sw
}

func (sw *switchable) restart(s *mcp.Server) {
	sw.mu.Lock()
	sw.h = mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return s }, nil)
	sw.mu.Unlock()
}

func (sw *switchable) crash() {
	sw.mu.Lock()
	sw.h = http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusServiceUnavailable) })
	sw.mu.Unlock()
	sw.srv.CloseClientConnections()
}

// Упавший MCP-сервер: показания помечаются «нет связи», после переподключения
// подписка восстанавливается, а пропущенное изменение проходит через гейт.
func TestMCPServerRestart(t *testing.T) {
	room := fakemcp.NewRoom()
	sw := newSwitchable(t, room.Server()) // закрывается после агента (очистка — в обратном порядке)
	f := build(t, "policy: {wake: gate, hwctx: delta_tail, gate: {debounce_s: 0, coalesce_s: 0, default: ignore, "+
		"signals: [{device: door1, property: open, kind: change}]}}\n", noop)
	ctx := context.Background()
	err := f.a.Host().Connect(ctx, config.MCPServer{Name: "hw", Transport: "http", URL: sw.srv.URL,
		Subscribe: []string{"*/state/*"}, TimeoutS: 5, ReconnectS: 0.05, ReconnectMaxS: 0.2})
	if err != nil {
		t.Fatal(err)
	}
	if err := f.a.Start(ctx); err != nil {
		t.Fatal(err)
	}
	mcpCount := func(k string) int { return f.a.Metrics()["mcp"].(map[string]int)[k] }
	f.user(t, "1")

	sw.crash()
	waitFor(t, "mcp_down", func() bool { return mcpCount("down") == 1 })
	f.user(t, "2")
	room.Set(ctx, "door1", "open", true) // изменение, пока связи нет: уведомить некому
	sw.restart(room.Server())
	waitFor(t, "mcp_up", func() bool { return mcpCount("up") == 1 })

	reqs := f.requests()
	if len(reqs) != 3 {
		t.Fatalf("requests %d, want 2 user turns + 1 gate wake", len(reqs))
	}
	stale := reqs[1].Messages[len(reqs[1].Messages)-2].Content
	if !strings.Contains(stale, "door1.open=false [<1м, нет связи]") {
		t.Fatalf("stale readings must be marked:\n%s", stale)
	}
	woke := reqs[2].Messages[len(reqs[2].Messages)-2].Content
	if !strings.Contains(woke, "door1.open: false → true") || !strings.Contains(woke, "door1.open: false→true [<1м]") {
		t.Fatalf("missed change must reach the gate and the delta:\n%s", woke)
	}
	var order []string
	for _, r := range f.all() {
		switch r["type"] {
		case trace.MCPDown, trace.MCPUp:
			order = append(order, r["type"].(string)+":"+r["server"].(string))
		case trace.Input:
			if r["device"] == "door1" {
				order = append(order, "input:door1")
			}
		}
	}
	if strings.Join(order, ",") != "mcp_down:hw,mcp_up:hw,input:door1" {
		t.Fatalf("trace order %v", order)
	}
	if srv := f.a.Host().Servers()[0]; srv.Down || srv.Subscribed != 4 {
		t.Fatalf("server after reconnect %+v", srv)
	}
}

// Повторная синхронизация после mcp_up не удалась (датчики не отвечают):
// непрочитанные устройства остаются «нет связи» и неизвестны правилам, пока
// повтор с паузой переподключения их не прочитает; mcp_up пишется один раз.
func TestMCPResyncRetried(t *testing.T) {
	room := fakemcp.NewRoom()
	sw := newSwitchable(t, room.Server())
	f := build(t, "policy: {wake: gate, hwctx: delta_tail, gate: {debounce_s: 0, coalesce_s: 0, default: ignore}}\n", noop)
	ctx := context.Background()
	err := f.a.Host().Connect(ctx, config.MCPServer{Name: "hw", Transport: "http", URL: sw.srv.URL,
		Subscribe: []string{"*/state/*"}, TimeoutS: 5, ReconnectS: 0.05, ReconnectMaxS: 0.2})
	if err != nil {
		t.Fatal(err)
	}
	if err := f.a.Start(ctx); err != nil {
		t.Fatal(err)
	}
	mcpCount := func(k string) int { return f.a.Metrics()["mcp"].(map[string]int)[k] }
	sw.crash()
	waitFor(t, "mcp_down", func() bool { return mcpCount("down") == 1 })
	room.FailReads.Store(true)
	room.Set(ctx, "door1", "open", true)
	sw.restart(room.Server())
	waitFor(t, "failed resync", func() bool { return f.a.m.get("errors.mcp_resync") > 0 })
	if _, ok := f.a.hw.Get("door1", "open"); ok {
		t.Fatal("a device not re-read after reconnection must stay unknown")
	}
	room.FailReads.Store(false)
	waitFor(t, "resync retried", func() bool {
		v, ok := f.a.hw.Get("door1", "open")
		return ok && v == true
	})
	if mcpCount("up") != 1 {
		t.Fatalf("mcp_up written %d times", mcpCount("up"))
	}
	for _, e := range f.records(trace.Error) {
		if e["where"] != "mcp_resync" {
			t.Fatalf("unexpected error %v", e)
		}
	}
}

// Два MCP-сервера одновременно: инвентари объединяются, шаблон состояния
// камеры раскрывается только по её устройствам, совпавшие имена инструментов
// получают префикс сервера, уведомления приходят от обоих.
func TestTwoServersHardwareAndCamera(t *testing.T) {
	f := build(t, "policy: {wake: gate, hwctx: delta_tail, gate: {debounce_s: 0, coalesce_s: 0, cooldown_s: 0, default: change}}\n", noop)
	room, cam := fakemcp.NewRoom(), fakemcp.NewCamera()
	room.MetaState = true
	f.connectMem(t, "hw", room.Server())
	f.connectMem(t, "cam", cam.Server())
	ctx := context.Background()
	if err := f.a.Start(ctx); err != nil {
		t.Fatal(err)
	}
	if errs := f.records(trace.Error); len(errs) != 0 {
		t.Fatalf("start errors %v", errs)
	}
	subs := map[string]int{}
	for _, s := range f.a.Host().Servers() {
		subs[s.Name] = s.Subscribed
	}
	if subs["hw"] != 4 || subs["cam"] != 1 {
		t.Fatalf("subscriptions %v", subs)
	}
	var names []string
	for _, tl := range f.a.Host().Tools() {
		names = append(names, tl.Name)
	}
	if got := strings.Join(names, ","); got != "board_health,cam__read_property,hw__read_property,list_devices,write_property" {
		t.Fatalf("tools %s", got)
	}
	if v, ok := f.a.hw.Get("cam1", "person"); !ok || v != false {
		t.Fatalf("camera initial state %v %v", v, ok)
	}
	cam.Detect(ctx, 2)
	waitFor(t, "camera input", func() bool { return f.a.inputCounts()["sensor"] >= 2 })
	room.Set(ctx, "door1", "open", true)
	waitFor(t, "room input", func() bool { return f.a.inputCounts()["sensor"] >= 3 })
	reqs := f.requests()
	joined := ""
	for _, r := range reqs {
		joined += r.Messages[0].Content + r.Messages[len(r.Messages)-2].Content
	}
	for _, want := range []string{"cam1 (hall): people, person [bool]", "cam1.people: 0 → 2", "door1.open: false → true"} {
		if !strings.Contains(joined, want) {
			t.Errorf("prompts lack %q:\n%s", want, joined)
		}
	}
}
