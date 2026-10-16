package mcphost

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/edgectx/internal/activity"
	"github.com/local/edgectx/internal/config"
	"github.com/local/edgectx/internal/fakemcp"
)

// TestMain позволяет тестовому бинарнику работать stdio-сервером: так проверяется
// настоящий транспорт с дочерним процессом без отдельной сборки.
func TestMain(m *testing.M) {
	if os.Getenv("EDGECTX_FAKE_MCP") == "stdio" {
		if f := os.Getenv("EDGECTX_FAKE_MCP_PIDS"); f != "" {
			// Для теста перезапуска: PID процесса-сервера, по строке на запуск.
			pids, err := os.OpenFile(f, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
			if err != nil {
				os.Exit(1)
			}
			fmt.Fprintln(pids, os.Getpid())
			pids.Close()
		}
		if life, err := time.ParseDuration(os.Getenv("EDGECTX_FAKE_MCP_LIFE")); err == nil {
			time.AfterFunc(life, func() { os.Exit(0) }) // сервер, падающий вскоре после старта
		}
		if err := fakemcp.NewRoom().Server().Run(context.Background(), &mcp.StdioTransport{}); err != nil {
			os.Exit(1)
		}
		os.Exit(0)
	}
	os.Exit(m.Run())
}

type updates chan Update

func newHost(t *testing.T, reserved ...string) (*Host, updates, *activity.Tracker) {
	ch := make(updates, 16)
	tr := &activity.Tracker{}
	h := New(Options{Tracker: tr, Reserved: reserved, OnUpdate: func(_ context.Context, u Update) { ch <- u },
		OnError: func(w string, err error) { t.Logf("%s: %v", w, err) }})
	h.Start()
	t.Cleanup(h.Close)
	return h, ch, tr
}

func connectMem(t *testing.T, h *Host, name string, room *fakemcp.Room) {
	t.Helper()
	st, ct := mcp.NewInMemoryTransports()
	if _, err := room.Server().Connect(context.Background(), st, nil); err != nil {
		t.Fatal(err)
	}
	if err := h.ConnectTransport(context.Background(), config.MCPServer{Name: name, Transport: "stdio"}, ct); err != nil {
		t.Fatal(err)
	}
}

func wait(t *testing.T, ch updates) Update {
	t.Helper()
	select {
	case u := <-ch:
		return u
	case <-time.After(3 * time.Second):
		t.Fatal("no resources/updated notification")
	}
	return Update{}
}

func TestUpdateQueueBoundedAndTracked(t *testing.T) {
	tr := &activity.Tracker{}
	entered, release := make(chan struct{}), make(chan struct{})
	errs := make(chan error, 4)
	var got []string
	h := New(Options{Tracker: tr, UpdateQueueMax: 2,
		OnUpdate: func(_ context.Context, u Update) {
			if u.URI == "first" {
				close(entered)
				<-release
			}
			got = append(got, u.URI)
		},
		OnDown:  func(server string, _ error) { got = append(got, "down:"+server) },
		OnError: func(where string, err error) { errs <- fmt.Errorf("%s: %w", where, err) },
	})
	t.Cleanup(h.Close)
	h.Start()
	h.push(item{kind: itemResource, u: Update{URI: "first"}})
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("dispatcher did not start")
	}
	for _, uri := range []string{"second", "third", "overflow1", "overflow2"} {
		h.push(item{kind: itemResource, u: Update{URI: uri}})
	}
	h.push(item{kind: itemDown, u: Update{Server: "hw"}}) // события связи не отбрасываются
	h.qmu.Lock()
	n := len(h.queue)
	h.qmu.Unlock()
	if n != 3 || tr.Active() != 4 {
		t.Errorf("queue=%d active=%d, want 3 queued plus one running", n, tr.Active())
	}
	close(release)
	if !tr.WaitIdle(time.Second) {
		t.Fatal("discarded update leaked activity")
	}
	if err := <-errs; !strings.Contains(err.Error(), "mcp_queue_full") || !strings.Contains(err.Error(), "отброшено 2") {
		t.Errorf("overflow report %v", err)
	}
	if len(errs) != 0 {
		t.Error("overflow must be reported once per burst")
	}
	if strings.Join(got, ",") != "first,second,third,down:hw" {
		t.Errorf("delivered %v", got)
	}
}

func TestEventsHeldUntilStart(t *testing.T) {
	delivered := make(chan string, 2)
	h := New(Options{OnUpdate: func(_ context.Context, u Update) { delivered <- u.URI }})
	t.Cleanup(h.Close)
	h.push(item{kind: itemResource, u: Update{URI: "early"}})
	select {
	case u := <-delivered:
		t.Fatalf("update %s delivered before Start", u)
	case <-time.After(50 * time.Millisecond):
	}
	h.Start()
	select {
	case u := <-delivered:
		if u != "early" {
			t.Fatalf("got %s", u)
		}
	case <-time.After(time.Second):
		t.Fatal("held update not delivered after Start")
	}
}

func TestInMemoryCatalogCallsAndUpdates(t *testing.T) {
	h, ch, tr := newHost(t)
	room := fakemcp.NewRoom()
	connectMem(t, h, "room", room)

	var names []string
	for _, tl := range h.Tools() {
		names = append(names, tl.Name)
		if tl.Schema["type"] != "object" {
			t.Errorf("%s schema %v", tl.Name, tl.Schema)
		}
	}
	if strings.Join(names, ",") != "board_health,list_devices,read_property,write_property" {
		t.Fatalf("tools %v", names)
	}
	inv, err := h.Inventory(context.Background())
	if err != nil || !strings.Contains(inv["room"], `"relay1"`) {
		t.Fatalf("inventory %v %v", inv, err)
	}
	uris := h.StateURIs("room", nil)
	if len(uris) != 4 || uris[0] != "hw://state/door1" {
		t.Fatalf("state uris %v", uris)
	}
	done, err := h.Subscribe(context.Background(), "room", uris)
	if err != nil || len(done) != 4 || len(room.Subscribed()) != 4 {
		t.Fatalf("subscribe %v %v %v", done, err, room.Subscribed())
	}

	// Чтение: JSON с отступами сжимается в канонический вид.
	res, err := h.Call(context.Background(), "read_property", map[string]any{"device": "t1", "property": "temperature"})
	if err != nil || res.Text != `{"device":"t1","property":"temperature","value":22}` || res.IsError || res.Denied {
		t.Fatalf("read %+v %v", res, err)
	}
	// Отказ шлюза — структурный.
	res, _ = h.Call(context.Background(), "write_property", map[string]any{"device": "door1", "property": "open", "value": true})
	if !res.Denied || res.Reason != "not_writable" {
		t.Fatalf("denied %+v", res)
	}
	res, _ = h.Call(context.Background(), "write_property", map[string]any{"device": "relay1", "property": "on", "value": "yes"})
	if !res.Denied || res.Reason != "schema" {
		t.Fatalf("denied in text %+v", res)
	}
	res, _ = h.Call(context.Background(), "read_property", map[string]any{"device": "nope", "property": "x"})
	if !res.IsError {
		t.Fatalf("expected tool error %+v", res)
	}
	if _, err := h.Call(context.Background(), "rm_rf", nil); !errors.Is(err, ErrUnknownTool) {
		t.Fatalf("unknown tool err %v", err)
	}

	// Успешная запись меняет состояние и порождает уведомление.
	res, _ = h.Call(context.Background(), "write_property", map[string]any{"device": "relay1", "property": "on", "value": true})
	if res.Denied || res.Text != `{"ok":true}` {
		t.Fatalf("write %+v", res)
	}
	u := wait(t, ch)
	if u.Server != "room" || u.URI != "hw://state/relay1" {
		t.Fatalf("update %+v", u)
	}
	tr.WaitIdle(time.Second)
	text, err := h.ReadResource(context.Background(), "room", u.URI)
	if err != nil || !strings.Contains(text, `"on": true`) {
		t.Fatalf("read resource %q %v", text, err)
	}
	room.MetaState = true
	room.Set(context.Background(), "door1", "open", true)
	u = wait(t, ch)
	st, _ := u.Meta["state"].(map[string]any)
	if u.URI != "hw://state/door1" || st["open"] != true {
		t.Fatalf("meta update %+v", u)
	}
	info := h.Servers()
	if len(info) != 1 || info[0].Protocol != ProtocolVersion || info[0].Subscribed != 4 || !info[0].CanSubscribe {
		t.Fatalf("info %+v", info)
	}
}

func TestNameCollisions(t *testing.T) {
	h, _, _ := newHost(t, "board_health")
	connectMem(t, h, "a", fakemcp.NewRoom())
	connectMem(t, h, "b", fakemcp.NewRoom())
	var names []string
	for _, tl := range h.Tools() {
		names = append(names, tl.Name)
	}
	want := "a__board_health,a__list_devices,a__read_property,a__write_property,b__board_health,b__list_devices,b__read_property,b__write_property"
	if strings.Join(names, ",") != want {
		t.Fatalf("names %v", names)
	}
	tl, ok := h.Lookup("b__read_property")
	if !ok || tl.Server != "b" || tl.Original != "read_property" {
		t.Fatalf("lookup %+v", tl)
	}
	if _, err := h.Call(context.Background(), "b__read_property", map[string]any{"device": "door1", "property": "open"}); err != nil {
		t.Fatal(err)
	}
}

func TestStreamableHTTP(t *testing.T) {
	room := fakemcp.NewRoom()
	srv := httptest.NewServer(mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return room.Server() }, nil))
	// Порядок очистки обратный: сначала закрывается host (и его поток SSE), потом сервер.
	t.Cleanup(srv.Close)
	h, ch, _ := newHost(t)
	cfg := config.MCPServer{Name: "sim", Transport: "http", URL: srv.URL, Subscribe: []string{"*/state/*"}, TimeoutS: 5}
	if err := h.Connect(context.Background(), cfg); err != nil {
		t.Fatal(err)
	}
	uris := h.StateURIs("sim", nil)
	if len(uris) != 4 {
		t.Fatalf("filtered uris %v", uris)
	}
	if _, err := h.Subscribe(context.Background(), "sim", uris); err != nil {
		t.Fatal(err)
	}
	room.Set(context.Background(), "pir1", "motion", 1.0)
	if u := wait(t, ch); u.URI != "hw://state/pir1" {
		t.Fatalf("update %+v", u)
	}
	if h.Servers()[0].Protocol != ProtocolVersion {
		t.Fatalf("protocol %s", h.Servers()[0].Protocol)
	}
}

func TestStdioChildProcess(t *testing.T) {
	exe, err := os.Executable()
	if err != nil {
		t.Skip(err)
	}
	h, _, _ := newHost(t)
	cfg := config.MCPServer{Name: "hw", Transport: "stdio", Cmd: []string{exe, "-test.run=^$"},
		Env: map[string]string{"EDGECTX_FAKE_MCP": "stdio"}, TimeoutS: 10}
	if err := h.Connect(context.Background(), cfg); err != nil {
		t.Fatal(err)
	}
	res, err := h.Call(context.Background(), "board_health", nil)
	if err != nil || !strings.Contains(res.Text, `"temp_c":41.5`) {
		t.Fatalf("board_health %+v %v", res, err)
	}
	start := time.Now()
	h.Close()
	if time.Since(start) > 4*time.Second {
		t.Fatal("stdio server did not stop promptly")
	}
}

// statusHost — host с каналом событий связи (для тестов переподключения);
// первые failUp вызовов OnUp сообщают, что состояние восстановлено не всё.
func statusHost(t *testing.T, failUp int) (*Host, chan string) {
	ev := make(chan string, 8)
	var ups atomic.Int32
	h := New(Options{
		OnDown: func(server string, err error) { ev <- "down:" + server },
		OnUp: func(_ context.Context, server string, attempts int) error {
			ev <- fmt.Sprintf("up:%s:%d", server, attempts)
			if ups.Add(1) <= int32(failUp) {
				return errors.New("resync failed")
			}
			return nil
		},
		OnError: func(w string, err error) { t.Logf("%s: %v", w, err) },
	})
	h.Start()
	t.Cleanup(h.Close)
	return h, ev
}

func expect(t *testing.T, ev chan string, prefix string) string {
	t.Helper()
	select {
	case e := <-ev:
		if !strings.HasPrefix(e, prefix) {
			t.Fatalf("event %q, want %s…", e, prefix)
		}
		return e
	case <-time.After(10 * time.Second):
		t.Fatalf("no %s event", prefix)
	}
	return ""
}

// Перезапуск HTTP-сервера: прежняя сессия теряется, host получает down,
// переподключается с повторной инициализацией и сообщает up; если агент
// восстановил не всё, OnUp повторяется с паузой.
func TestHTTPServerRestartReconnects(t *testing.T) {
	room := fakemcp.NewRoom()
	fresh := func() http.Handler {
		return mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return room.Server() }, nil)
	}
	var mu sync.Mutex
	handler := fresh()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		h := handler
		mu.Unlock()
		h.ServeHTTP(w, r)
	}))
	t.Cleanup(srv.Close)
	h, ev := statusHost(t, 1)
	cfg := config.MCPServer{Name: "sim", Transport: "http", URL: srv.URL, TimeoutS: 5, ReconnectS: 0.05, ReconnectMaxS: 0.2}
	if err := h.Connect(context.Background(), cfg); err != nil {
		t.Fatal(err)
	}
	h.Inventory(context.Background())

	mu.Lock()
	handler = http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusServiceUnavailable) })
	mu.Unlock()
	srv.CloseClientConnections()
	expect(t, ev, "down:sim")
	if _, err := h.Call(context.Background(), "board_health", nil); !errors.Is(err, ErrServerDown) {
		t.Fatalf("call to a down server: %v", err)
	}
	if !h.Servers()[0].Down || len(h.Tools()) != 4 {
		t.Fatalf("down server must keep its catalog: %+v %d", h.Servers(), len(h.Tools()))
	}
	if inv, err := h.Inventory(context.Background()); err != nil || !strings.Contains(inv["sim"], "relay1") {
		t.Fatalf("inventory of a down server must be the last known one: %v %v", inv, err)
	}

	mu.Lock()
	handler = fresh() // сервер перезапущен: старых сессий нет
	mu.Unlock()
	expect(t, ev, "up:sim:")
	expect(t, ev, "up:sim:") // повтор OnUp после неполного восстановления
	if res, err := h.Call(context.Background(), "board_health", nil); err != nil || !strings.Contains(res.Text, "temp_c") {
		t.Fatalf("call after reconnect: %+v %v", res, err)
	}
	if h.Servers()[0].Down {
		t.Fatal("server must be up")
	}
}

// Падение stdio-сервера: процесс запускается заново.
func TestStdioServerRestarts(t *testing.T) {
	exe, err := os.Executable()
	if err != nil {
		t.Skip(err)
	}
	pidFile := filepath.Join(t.TempDir(), "pids")
	h, ev := statusHost(t, 0)
	cfg := config.MCPServer{Name: "hw", Transport: "stdio", Cmd: []string{exe, "-test.run=^$"}, TimeoutS: 10,
		Env: map[string]string{"EDGECTX_FAKE_MCP": "stdio", "EDGECTX_FAKE_MCP_PIDS": pidFile}, ReconnectS: 0.05, ReconnectMaxS: 0.2}
	if err := h.Connect(context.Background(), cfg); err != nil {
		t.Fatal(err)
	}
	pids := func() []string {
		b, _ := os.ReadFile(pidFile)
		return strings.Fields(string(b))
	}
	var pid int
	fmt.Sscan(pids()[0], &pid)
	proc, err := os.FindProcess(pid)
	if err != nil {
		t.Fatal(err)
	}
	proc.Kill()
	expect(t, ev, "down:hw")
	expect(t, ev, "up:hw:")
	if p := pids(); len(p) != 2 || p[1] == p[0] {
		t.Fatalf("server process must be started again: %v", p)
	}
	if _, err := h.Call(context.Background(), "board_health", nil); err != nil {
		t.Fatal(err)
	}
}

// Сервер, падающий вскоре после старта: пауза переподключения растёт от
// сессии к сессии, а не начинается заново после каждой инициализации.
func TestFlappingServerBacksOff(t *testing.T) {
	exe, err := os.Executable()
	if err != nil {
		t.Skip(err)
	}
	h, ev := statusHost(t, 0)
	// reconnect_max_s с запасом длиннее сессии: под -race процесс-сервер живёт около 1,5 с.
	cfg := config.MCPServer{Name: "hw", Transport: "stdio", Cmd: []string{exe, "-test.run=^$"}, TimeoutS: 10,
		Env: map[string]string{"EDGECTX_FAKE_MCP": "stdio", "EDGECTX_FAKE_MCP_LIFE": "200ms"}, ReconnectS: 0.2, ReconnectMaxS: 5}
	if err := h.Connect(context.Background(), cfg); err != nil {
		t.Fatal(err)
	}
	var gaps []time.Duration
	for i := 0; i < 3; i++ {
		expect(t, ev, "down:hw")
		down := time.Now()
		expect(t, ev, "up:hw:1")
		gaps = append(gaps, time.Since(down))
	}
	// Паузы 100–200, 200–400 и 400–800 мс; со сбросом каждая была бы 100–200 мс.
	if gaps[2] < 400*time.Millisecond {
		t.Fatalf("reconnect pauses do not grow across short sessions: %v", gaps)
	}
}

func TestMatchAny(t *testing.T) {
	cases := []struct {
		p, s string
		want bool
	}{
		{"*/state/*", "hw://state/door1", true}, {"hw://state/*", "hw://state/door1", true},
		{"*/state/*", "hw://inventory", false}, {"*", "anything", true}, {"sim://*", "hw://state/x", false},
	}
	for _, c := range cases {
		if got := matchAny([]string{c.p}, c.s); got != c.want {
			t.Errorf("match(%s, %s) = %v", c.p, c.s, got)
		}
	}
}

func TestSchemaOf(t *testing.T) {
	m := schemaOf(nil)
	if m["type"] != "object" || m["properties"] == nil {
		t.Fatalf("%v", m)
	}
	if sanitize("hw.read-prop/x") != "hw_read-prop_x" {
		t.Fatal(sanitize("hw.read-prop/x"))
	}
}
