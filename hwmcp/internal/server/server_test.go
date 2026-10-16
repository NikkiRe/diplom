package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/hwmcp/internal/clock"
	"github.com/local/hwmcp/internal/declared"
	"github.com/local/hwmcp/internal/gate"
	"github.com/local/hwmcp/internal/gpio"
	"github.com/local/hwmcp/internal/hub"
	"github.com/local/hwmcp/internal/inventory"
	"github.com/local/hwmcp/internal/sysfs"
	"github.com/local/hwmcp/internal/testutil"
)

const testDeclared = `{
 "locations": {"iio:bme280@i2c-1/0x76": "kitchen", "iio:dht11@dht11@4": "balcony",
               "w1:28-0316a2795cff": "boiler_pipe", "hwmon:cpu_thermal": "board"},
 "gpio": [
  {"id": "pir_hall", "chip": "gpiochip0", "line": 17, "direction": "input", "quantity": "motion", "location": "hall"},
  {"id": "relay_fan", "chip": "gpiochip0", "line": 27, "direction": "output", "quantity": "switch", "location": "kitchen"},
  {"id": "door_lock", "chip": "gpiochip0", "line": 22, "direction": "output", "quantity": "lock", "location": "hall",
   "requires_confirmation": true}
 ],
 "gate": {"devices": {"relay_fan": {"min_interval_s": 1, "safe_state": {"state": false}}}}
}`

type fixture struct {
	t    *testing.T
	root string
	clk  *clock.Virtual
	hub  *hub.Hub
	srv  *Server
}

func newFixture(t *testing.T, catalog string) *fixture {
	t.Helper()
	f := &fixture{t: t, root: testutil.FixtureCopy(t), clk: clock.NewVirtual(100)}
	decl, err := declared.Parse([]byte(testDeclared))
	if err != nil {
		t.Fatal(err)
	}
	cfg, err := gate.ParseConfig(decl.Gate)
	if err != nil {
		t.Fatal(err)
	}
	inv, err := inventory.Build(sysfs.Probe(f.root, decl), decl, inventory.Options{})
	if err != nil {
		t.Fatal(err)
	}
	f.hub, err = hub.New(inv, cfg, &gpio.Files{Dir: filepath.Join(f.root, "dev")},
		hub.Options{Clock: f.clk, SamplePeriod: 1, Root: f.root})
	if err != nil {
		t.Fatal(err)
	}
	f.srv, err = New(f.hub, Options{Catalog: catalog, BoardHealth: true, ProtocolVersions: []string{"2025-11-25"}})
	if err != nil {
		t.Fatal(err)
	}
	return f
}

type notes struct {
	mu    sync.Mutex
	uris  []string
	metas map[string]mcp.Meta // последнее _meta по URI
	ch    chan string
}

func (n *notes) handler(ctx context.Context, req *mcp.ResourceUpdatedNotificationRequest) {
	n.mu.Lock()
	n.uris = append(n.uris, req.Params.URI)
	if n.metas == nil {
		n.metas = map[string]mcp.Meta{}
	}
	n.metas[req.Params.URI] = req.Params.Meta
	n.mu.Unlock()
	select {
	case n.ch <- req.Params.URI:
	default:
	}
}

func (f *fixture) connect(n *notes) *mcp.ClientSession {
	f.t.Helper()
	ctx := context.Background()
	ct, st := mcp.NewInMemoryTransports()
	if _, err := f.srv.MCP().Connect(ctx, st, nil); err != nil {
		f.t.Fatal(err)
	}
	opts := &mcp.ClientOptions{}
	if n != nil {
		opts.ResourceUpdatedHandler = n.handler
	}
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "1"}, opts).Connect(ctx, ct, nil)
	if err != nil {
		f.t.Fatal(err)
	}
	f.t.Cleanup(func() { _ = cs.Close() })
	return cs
}

// call вызывает инструмент и возвращает разобранный JSON результата и признак isError.
func call(t *testing.T, cs *mcp.ClientSession, name string, args any, meta mcp.Meta) (map[string]any, bool) {
	t.Helper()
	res, err := cs.CallTool(context.Background(), &mcp.CallToolParams{Name: name, Arguments: args, Meta: meta})
	if err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	if len(res.Content) != 1 {
		t.Fatalf("%s: %d contents", name, len(res.Content))
	}
	text := res.Content[0].(*mcp.TextContent).Text
	var body map[string]any
	if err := json.Unmarshal([]byte(text), &body); err != nil {
		t.Fatalf("%s: result is not JSON: %s", name, text)
	}
	return body, res.IsError
}

func toolNames(t *testing.T, cs *mcp.ClientSession) []string {
	t.Helper()
	res, err := cs.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, tl := range res.Tools {
		names = append(names, tl.Name)
	}
	return names
}

func (f *fixture) level(line int) string {
	b, err := os.ReadFile(filepath.Join(f.root, "dev", "gpiochip0", "line"+itoa(line)))
	if err != nil {
		return "missing"
	}
	return strings.TrimSpace(string(b))
}

func itoa(n int) string { b, _ := json.Marshal(n); return string(b) }

func TestGenericCatalog(t *testing.T) {
	f := newFixture(t, CatalogGeneric)
	cs := f.connect(nil)
	if got := strings.Join(toolNames(t, cs), ","); got != "board_health,invoke_action,list_devices,read_property,write_property" {
		t.Fatalf("tools: %s", got)
	}
	if v := cs.InitializeResult().ProtocolVersion; v != "2025-11-25" {
		t.Fatalf("protocol %s", v)
	}
	body, isErr := call(t, cs, "list_devices", map[string]any{}, nil)
	if isErr {
		t.Fatal(body)
	}
	devs := body["devices"].([]any)
	if len(devs) != 8 {
		t.Fatalf("%d actionable devices: %v", len(devs), devs)
	}
	body, _ = call(t, cs, "list_devices", map[string]any{"all": true}, nil)
	if len(body["devices"].([]any)) != 14 {
		t.Fatal("all=true must list every device")
	}
}

func TestExplicitCatalog(t *testing.T) {
	f := newFixture(t, CatalogExplicit)
	cs := f.connect(nil)
	names := toolNames(t, cs)
	want := 0
	for _, d := range f.hub.Inventory().Devices {
		for _, p := range d.Properties {
			want++
			if p.Writable {
				want++
			}
		}
		want += len(d.Actions)
	}
	if len(names) != want+1 {
		t.Fatalf("%d tools, want %d: %v", len(names), want+1, names)
	}
	for _, n := range []string{"read_kitchen_bme280_temperature", "set_relay_fan_state", "pulse_relay_fan",
		"read_pir_hall_state", "board_health"} {
		if !contains(names, n) {
			t.Errorf("missing %s", n)
		}
	}
	body, isErr := call(t, cs, "read_kitchen_bme280_temperature", nil, nil)
	if isErr || body["value"] != 22.51 || body["unit"] != "Cel" {
		t.Fatalf("%v", body)
	}
	body, isErr = call(t, cs, "set_relay_fan_state", map[string]any{"value": true}, nil)
	if isErr || body["executed"] != true || f.level(27) != "1" {
		t.Fatalf("%v", body)
	}
	body, isErr = call(t, cs, "set_relay_fan_state", map[string]any{"value": 1}, nil)
	if !isErr || body["reason"] != "invalid_args" || body["denied"] != true {
		t.Fatalf("schema denial: %v", body)
	}
}

func contains(l []string, s string) bool {
	for _, x := range l {
		if x == s {
			return true
		}
	}
	return false
}

func TestReadAndWriteThroughGate(t *testing.T) {
	f := newFixture(t, CatalogGeneric)
	cs := f.connect(nil)
	body, isErr := call(t, cs, "read_property", map[string]any{"device": "kitchen_bme280", "property": "pressure"}, nil)
	if isErr || body["value"] != 101325.0 || body["unit"] != "Pa" || body["age_s"] != 0.0 {
		t.Fatalf("%v", body)
	}
	body, isErr = call(t, cs, "read_property", map[string]any{"device": "nosuch", "property": "x"}, nil)
	if !isErr || body["error"] != "unknown_device" {
		t.Fatalf("%v", body)
	}
	body, isErr = call(t, cs, "read_property", map[string]any{"device": "kitchen_bme280"}, nil)
	if !isErr || body["error"] != "invalid_args" || body["denied"] != nil {
		t.Fatalf("missing argument: %v", body)
	}

	body, isErr = call(t, cs, "write_property", map[string]any{"device": "relay_fan", "property": "state", "value": true}, nil)
	if isErr || body["ok"] != true || body["value"] != true || f.level(27) != "1" {
		t.Fatalf("%v", body)
	}
	body, isErr = call(t, cs, "write_property", map[string]any{"device": "relay_fan", "property": "state", "value": false}, nil)
	if !isErr || body["denied"] != true || body["reason"] != "rate_limit" || f.level(27) != "1" {
		t.Fatalf("rate limit: %v", body)
	}
	body, _ = call(t, cs, "write_property", map[string]any{"device": "kitchen_bme280", "property": "temperature", "value": 30}, nil)
	if body["reason"] != "unknown_device" || !strings.Contains(body["detail"].(string), "no writable") {
		t.Fatalf("sensor write: %v", body)
	}
	body, _ = call(t, cs, "write_property", map[string]any{"device": "relay_fan", "property": "speed", "value": 30}, nil)
	if body["reason"] != "unknown_tool" || !strings.Contains(body["detail"].(string), "no property") {
		t.Fatalf("unknown property: %v", body)
	}
	body, _ = call(t, cs, "invoke_action", map[string]any{"device": "relay_fan", "action": "explode"}, nil)
	if body["reason"] != "unknown_tool" || !strings.Contains(body["detail"].(string), "no action") {
		t.Fatalf("unknown action: %v", body)
	}
	if err := f.hub.AdvanceTo(f.clk, 102); err != nil {
		t.Fatal(err)
	}
	body, isErr = call(t, cs, "invoke_action", map[string]any{"device": "relay_fan", "action": "pulse",
		"args": map[string]any{"seconds": 2.5}}, nil)
	if isErr || body["off_in_s"] != 2.5 {
		t.Fatalf("pulse: %v", body)
	}
	if err := f.hub.AdvanceTo(f.clk, 105); err != nil {
		t.Fatal(err)
	}
	if f.level(27) != "0" {
		t.Fatal("pulse must end")
	}
}

func TestMetaFromOrchestrator(t *testing.T) {
	f := newFixture(t, CatalogGeneric)
	cs := f.connect(nil)
	args := map[string]any{"device": "relay_fan", "property": "state", "value": true}
	body, isErr := call(t, cs, "write_property", args, mcp.Meta{MetaDryRun: true})
	if isErr || body["dry_run"] != true || body["verdict"] != "allow" || f.level(27) != "0" {
		t.Fatalf("dry run: %v", body)
	}
	meta := mcp.Meta{MetaCmdID: "plan-7"}
	if body, isErr = call(t, cs, "write_property", args, meta); isErr || body["cmd_id"] != "plan-7" {
		t.Fatalf("%v", body)
	}
	body, isErr = call(t, cs, "write_property", args, meta) // повтор после обрыва связи
	if isErr || body["verdict"] != "duplicate" || body["executed"] != false {
		t.Fatalf("duplicate: %v", body)
	}
	_ = f.clk.Set(200)
	body, _ = call(t, cs, "write_property", args, mcp.Meta{MetaIssuedAt: 180.0})
	if body["reason"] != "stale_command" {
		t.Fatalf("stale: %v", body)
	}
	body, isErr = call(t, cs, "write_property", args, mcp.Meta{MetaIssuedAt: 180.0, MetaTTL: 30.0})
	if isErr {
		t.Fatalf("ttl override: %v", body)
	}
	// подтверждение: LLM не может подтвердить сама (поле не из arguments)
	lock := map[string]any{"device": "door_lock", "property": "state", "value": true, "confirmed": true}
	body, _ = call(t, cs, "write_property", lock, nil)
	if body["reason"] != "invalid_args" {
		t.Fatalf("extra argument must be rejected: %v", body)
	}
	delete(lock, "confirmed")
	body, isErr = call(t, cs, "write_property", lock, nil)
	if !isErr || body["verdict"] != "confirm" || body["reason"] != "needs_confirm" {
		t.Fatalf("confirm: %v", body)
	}
	body, isErr = call(t, cs, "write_property", lock, mcp.Meta{MetaConfirmed: true})
	if isErr || f.level(22) != "1" {
		t.Fatalf("confirmed: %v", body)
	}
}

func TestResources(t *testing.T) {
	f := newFixture(t, CatalogGeneric)
	cs := f.connect(nil)
	ctx := context.Background()
	res, err := cs.ListResources(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	var uris []string
	for _, r := range res.Resources {
		uris = append(uris, r.URI)
	}
	sort.Strings(uris)
	for _, u := range []string{"hw://inventory", "hw://inventory/text", "hw://td", "hw://probe",
		"hw://state/kitchen_bme280", "hw://state/relay_fan", "hw://state/pir_hall"} {
		if !contains(uris, u) {
			t.Errorf("missing resource %s in %v", u, uris)
		}
	}
	read := func(uri string) string {
		r, err := cs.ReadResource(ctx, &mcp.ReadResourceParams{URI: uri})
		if err != nil {
			t.Fatalf("%s: %v", uri, err)
		}
		return r.Contents[0].Text
	}
	if read("hw://inventory/text") != f.hub.Inventory().Text() {
		t.Fatal("text projection")
	}
	var st map[string]any
	if err := json.Unmarshal([]byte(read("hw://state/kitchen_bme280")), &st); err != nil {
		t.Fatal(err)
	}
	if st["properties"].(map[string]any)["humidity"].(map[string]any)["value"] != 41.23 {
		t.Fatalf("state: %v", st)
	}
	var probe []map[string]any
	if err := json.Unmarshal([]byte(read("hw://probe")), &probe); err != nil || len(probe) != 14 {
		t.Fatalf("probe: %v", err)
	}
	if _, err := cs.ReadResource(ctx, &mcp.ReadResourceParams{URI: "hw://state/nosuch"}); err == nil {
		t.Fatal("unknown resource must fail")
	}
	if err := cs.Subscribe(ctx, &mcp.SubscribeParams{URI: "hw://nope"}); err == nil {
		t.Fatal("subscribe to unknown resource must fail")
	}
}

func waitURI(t *testing.T, n *notes, want string) {
	t.Helper()
	deadline := time.After(5 * time.Second)
	for {
		select {
		case u := <-n.ch:
			if u == want {
				return
			}
		case <-deadline:
			n.mu.Lock()
			defer n.mu.Unlock()
			t.Fatalf("no notification %s; got %v", want, n.uris)
		}
	}
}

func TestSubscriptionNotifications(t *testing.T) {
	f := newFixture(t, CatalogGeneric)
	n := &notes{ch: make(chan string, 16)}
	cs := f.connect(n)
	ctx := context.Background()
	for _, u := range []string{"hw://state/kitchen_bme280", "hw://state/pir_hall"} {
		if err := cs.Subscribe(ctx, &mcp.SubscribeParams{URI: u}); err != nil {
			t.Fatal(err)
		}
	}
	testutil.WriteFile(t, filepath.Join(f.root, "sys/bus/iio/devices/iio:device0/in_temp_input"), "25000\n")
	if err := f.hub.AdvanceTo(f.clk, 101); err != nil {
		t.Fatal(err)
	}
	waitURI(t, n, "hw://state/kitchen_bme280")
	// новые значения — в самом уведомлении: клиенту не нужно читать ресурс
	n.mu.Lock()
	meta := n.metas["hw://state/kitchen_bme280"]
	n.mu.Unlock()
	st, _ := meta["state"].(map[string]any)
	if meta["device"] != "kitchen_bme280" || st["temperature"] != 25.0 || len(st) != 1 {
		t.Fatalf("notification _meta: %v", meta)
	}
	if ch, _ := meta["hw/changed"].([]any); len(ch) != 1 || ch[0] != "temperature" {
		t.Fatalf("hw/changed: %v", meta)
	}
	testutil.WriteFile(t, filepath.Join(f.root, "dev/gpiochip0/line17"), "1\n")
	if err := f.hub.AdvanceTo(f.clk, 102); err != nil {
		t.Fatal(err)
	}
	waitURI(t, n, "hw://state/pir_hall")
	// не подписаны на реле — уведомления нет
	call(t, cs, "write_property", map[string]any{"device": "relay_fan", "property": "state", "value": true}, nil)
	time.Sleep(50 * time.Millisecond)
	n.mu.Lock()
	defer n.mu.Unlock()
	for _, u := range n.uris {
		if u == "hw://state/relay_fan" {
			t.Fatal("notification without subscription")
		}
	}
}

// Streamable HTTP: тот же сервер через httptest, уведомления — по потоку SSE (GET).
func TestStreamableHTTP(t *testing.T) {
	f := newFixture(t, CatalogExplicit)
	h := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return f.srv.MCP() }, nil)
	ts := httptest.NewServer(h)
	defer ts.Close()
	n := &notes{ch: make(chan string, 16)}
	ctx := context.Background()
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "http-test", Version: "1"},
		&mcp.ClientOptions{ResourceUpdatedHandler: n.handler}).Connect(ctx, &mcp.StreamableClientTransport{Endpoint: ts.URL}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()
	if v := cs.InitializeResult().ProtocolVersion; v != "2025-11-25" {
		t.Fatalf("protocol %s", v)
	}
	body, isErr := call(t, cs, "read_boiler_pipe_ds18b20_temperature", nil, nil)
	if isErr || body["value"] != 21.437 {
		t.Fatalf("%v", body)
	}
	if err := cs.Subscribe(ctx, &mcp.SubscribeParams{URI: "hw://state/boiler_pipe_ds18b20"}); err != nil {
		t.Fatal(err)
	}
	time.Sleep(100 * time.Millisecond) // поток SSE (GET) открывается после initialize
	testutil.WriteFile(t, filepath.Join(f.root, "sys/bus/w1/devices/28-0316a2795cff/temperature"), "23000\n")
	// DS18B20 (w1) опрашивается со своим периодом драйвера, а не с общим
	if err := f.hub.AdvanceTo(f.clk, 100+hub.DefaultDriverPeriods["w1"]); err != nil {
		t.Fatal(err)
	}
	waitURI(t, n, "hw://state/boiler_pipe_ds18b20")
}

// Пропавший датчик глазами клиента MCP: уведомление с null в _meta.state, read_property и ресурс
// состояния — null с причиной, board_health — что недоступно сейчас и счётчик неудачных чтений.
func TestLostSensorThroughMCP(t *testing.T) {
	f := newFixture(t, CatalogGeneric)
	n := &notes{ch: make(chan string, 16)}
	cs := f.connect(n)
	ctx := context.Background()
	if err := cs.Subscribe(ctx, &mcp.SubscribeParams{URI: "hw://state/kitchen_bme280"}); err != nil {
		t.Fatal(err)
	}
	dev := filepath.Join(f.root, "sys/devices/platform/soc/1f00074000.i2c/i2c-1/1-0076/iio:device0")
	if err := os.Rename(dev, dev+".gone"); err != nil {
		t.Fatal(err)
	}
	if err := f.hub.AdvanceTo(f.clk, 100+hub.DefaultFailLimit); err != nil {
		t.Fatal(err)
	}
	waitURI(t, n, "hw://state/kitchen_bme280")
	n.mu.Lock()
	st, _ := n.metas["hw://state/kitchen_bme280"]["state"].(map[string]any)
	n.mu.Unlock()
	if v, has := st["temperature"]; !has || v != nil || len(st) != 3 {
		t.Fatalf("lost device must be notified as null values: %v", st)
	}
	body, isErr := call(t, cs, "read_property", map[string]any{"device": "kitchen_bme280", "property": "temperature"}, nil)
	if isErr || body["value"] != nil || body["error"] != "absent" {
		t.Fatalf("read_property: %v", body)
	}
	body, _ = call(t, cs, "board_health", map[string]any{}, nil)
	faults, _ := body["sensor_faults"].(map[string]any)
	if faults["kitchen_bme280.temperature"] != "absent" || body["sensor_errors"].(float64) < 3*hub.DefaultFailLimit {
		t.Fatalf("board_health: %v", body)
	}
}
