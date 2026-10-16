package hub

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/local/hwmcp/internal/argschema"
	"github.com/local/hwmcp/internal/clock"
	"github.com/local/hwmcp/internal/declared"
	"github.com/local/hwmcp/internal/gate"
	"github.com/local/hwmcp/internal/gpio"
	"github.com/local/hwmcp/internal/inventory"
	"github.com/local/hwmcp/internal/sysfs"
	"github.com/local/hwmcp/internal/testutil"
	"github.com/local/hwmcp/internal/trace"
)

// declaredBase — declared.json исследования (места, PIR, реле) плюс политика шлюза из теста.
const declaredBase = `{
 "locations": {"iio:bme280@i2c-1/0x76": "kitchen", "iio:dht11@dht11@4": "balcony",
               "w1:28-0316a2795cff": "boiler_pipe", "hwmon:cpu_thermal": "board"},
 "gpio": [
  {"id": "pir_hall", "chip": "gpiochip0", "line": 17, "direction": "input", "quantity": "motion", "location": "hall"},
  {"id": "relay_fan", "chip": "gpiochip0", "line": 27, "direction": "output", "quantity": "switch",
   "location": "kitchen", "requires_confirmation": %CONFIRM%}
 ],
 "gate": %GATE%
}`

type env struct {
	t       *testing.T
	root    string
	gdir    string
	backend gpio.Backend
	clk     *clock.Virtual
	hub     *Hub
	trace   *bytes.Buffer
	mu      sync.Mutex
	notes   []string // "device:prop,prop"
}

func newEnv(t *testing.T, gateJSON string, confirm bool, mods ...func(*Options)) *env {
	t.Helper()
	e := fixtureEnv(t)
	if err := e.start(gateJSON, confirm, mods...); err != nil {
		t.Fatal(err)
	}
	return e
}

// fixtureEnv — копия фикстуры и эмуляция линий файлами; тест может поменять их до start.
func fixtureEnv(t *testing.T) *env {
	e := &env{t: t, root: testutil.FixtureCopy(t), clk: clock.NewVirtual(1000), trace: &bytes.Buffer{}}
	e.gdir = filepath.Join(e.root, "dev")
	e.backend = &gpio.Files{Dir: e.gdir}
	return e
}

// start строит среду по declaredBase с политикой шлюза gateJSON.
func (e *env) start(gateJSON string, confirm bool, mods ...func(*Options)) error {
	raw := strings.Replace(declaredBase, "%GATE%", gateJSON, 1)
	raw = strings.Replace(raw, "%CONFIRM%", map[bool]string{true: "true", false: "false"}[confirm], 1)
	decl, err := declared.Parse([]byte(raw))
	if err != nil {
		return err
	}
	cfg, err := gate.ParseConfig(decl.Gate)
	if err != nil {
		return err
	}
	leased := map[string]bool{}
	for name, d := range cfg.Devices {
		if d.Lease != nil {
			leased[name] = true
		}
	}
	inv, err := inventory.Build(sysfs.Probe(e.root, decl), decl, inventory.Options{Leased: leased})
	if err != nil {
		return err
	}
	opts := Options{Clock: e.clk, SamplePeriod: 1, Trace: trace.New(e.trace, e.clk, "test-run"), Root: e.root}
	for _, m := range mods {
		m(&opts)
	}
	if e.hub, err = New(inv, cfg, e.backend, opts); err != nil {
		return err
	}
	e.hub.OnChange(func(u Update) {
		e.mu.Lock()
		e.notes = append(e.notes, u.Device+":"+strings.Join(u.Props(), ","))
		e.mu.Unlock()
	})
	return nil
}

func (e *env) advance(dt float64) {
	e.t.Helper()
	if err := e.hub.AdvanceTo(e.clk, e.clk.Now()+dt); err != nil {
		e.t.Fatal(err)
	}
}

func (e *env) takeNotes() []string {
	e.mu.Lock()
	defer e.mu.Unlock()
	n := e.notes
	e.notes = nil
	return n
}

func (e *env) write(rel, content string) {
	e.t.Helper()
	testutil.WriteFile(e.t, filepath.Join(e.root, rel), content)
}

func (e *env) lineLevel(n int) string {
	b, err := os.ReadFile(filepath.Join(e.gdir, "gpiochip0", "line"+itoa(n)))
	if err != nil {
		return "missing"
	}
	return strings.TrimSpace(string(b))
}

func itoa(n int) string { b, _ := json.Marshal(n); return string(b) }

func (e *env) set(v bool, meta ...func(*ActRequest)) ActResult {
	e.t.Helper()
	req := ActRequest{Device: "relay_fan", Tool: "set_state", Args: map[string]any{"value": v}, Origin: "test"}
	for _, m := range meta {
		m(&req)
	}
	return e.hub.Actuate(req)
}

func (e *env) records(typ string) []map[string]any {
	var out []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(e.trace.String()), "\n") {
		if line == "" {
			continue
		}
		var rec map[string]any
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			e.t.Fatalf("bad trace line %q: %v", line, err)
		}
		for _, k := range []string{"ts_wall", "ts_clock", "type", "run_id"} {
			if _, ok := rec[k]; !ok {
				e.t.Fatalf("trace record without %s: %s", k, line)
			}
		}
		if rec["type"] == typ {
			out = append(out, rec)
		}
	}
	return out
}

const relayPolicy = `{"devices": {"relay_fan": {"min_interval_s": 2, "safe_state": {"state": false}}}}`

func TestOutputInitializedToSafeState(t *testing.T) {
	e := newEnv(t, relayPolicy, false)
	if e.lineLevel(27) != "0" {
		t.Fatalf("relay line level %s", e.lineLevel(27))
	}
	r, err := e.hub.Read("relay_fan", "state")
	if err != nil || r.Value != false {
		t.Fatalf("relay state %+v %v", r, err)
	}
	if len(e.records("output_init")) != 1 {
		t.Fatal("output_init must be traced")
	}
}

func TestWriteThroughGate(t *testing.T) {
	e := newEnv(t, relayPolicy, false)
	res := e.set(true)
	if !res.Executed || res.Decision.Verdict != gate.Allow || e.lineLevel(27) != "1" {
		t.Fatalf("%+v level=%s", res, e.lineLevel(27))
	}
	if n := e.takeNotes(); len(n) != 1 || n[0] != "relay_fan:state" {
		t.Fatalf("notes %v", n)
	}
	// частота: второй вызов раньше min_interval
	e.advance(1)
	res = e.set(false)
	if res.Executed || res.Decision.Reason != gate.ReasonRateLimit || e.lineLevel(27) != "1" {
		t.Fatalf("rate limit: %+v", res)
	}
	e.advance(1.5)
	if res = e.set(false); !res.Executed || e.lineLevel(27) != "0" {
		t.Fatalf("after interval: %+v", res)
	}
	recs := e.records("gate")
	if len(recs) != 3 || recs[1]["reason"] != "rate_limit" || recs[0]["executed"] != true {
		t.Fatalf("gate records: %v", recs)
	}
	// неверный тип значения — отказ схемы, линия не тронута
	if res = e.set(false, func(r *ActRequest) { r.Args = map[string]any{"value": "off"} }); res.Decision.Reason != gate.ReasonInvalidArgs {
		t.Fatalf("invalid args: %+v", res)
	}
}

func TestIdempotentRetryAndTTL(t *testing.T) {
	e := newEnv(t, relayPolicy, false)
	withID := func(r *ActRequest) { r.CmdID = "edgectx-42" }
	if res := e.set(true, withID); !res.Executed {
		t.Fatalf("%+v", res)
	}
	e.advance(5)
	res := e.set(true, withID)
	if res.Decision.Verdict != gate.Duplicate || res.Executed {
		t.Fatalf("retry: %+v", res)
	}
	old := e.clk.Now() - 11
	res = e.set(false, func(r *ActRequest) { r.IssuedAt = &old })
	if res.Decision.Reason != gate.ReasonStaleCommand {
		t.Fatalf("stale command: %+v", res)
	}
}

func TestDeadbandNotifications(t *testing.T) {
	e := newEnv(t, relayPolicy, false)
	temp := "sys/bus/iio/devices/iio:device0/in_temp_input"
	e.write(temp, "22600\n") // +0.09 °C от базы 22.51 — ниже порога 0.2
	e.advance(1)
	if n := e.takeNotes(); len(n) != 0 {
		t.Fatalf("below threshold: %v", n)
	}
	e.write(temp, "22700\n") // +0.19 от базы — всё ещё ниже (сравнение с последним сообщённым)
	e.advance(1)
	e.write(temp, "22720\n") // +0.21 — уведомление
	e.advance(1)
	if n := e.takeNotes(); len(n) != 1 || n[0] != "kitchen_bme280:temperature" {
		t.Fatalf("drift accumulated: %v", n)
	}
	st, _ := e.hub.DeviceState("kitchen_bme280")
	tv := st["properties"].(map[string]any)["temperature"].(map[string]any)
	if tv["value"] != 22.72 || tv["unit"] != "Cel" {
		t.Fatalf("state view %v", tv)
	}
	// PIR: эмулятор пишет уровень линии
	testutil.WriteFile(t, filepath.Join(e.gdir, "gpiochip0", "line17"), "1\n")
	e.advance(1)
	if n := e.takeNotes(); len(n) != 1 || n[0] != "pir_hall:state" {
		t.Fatalf("pir: %v", n)
	}
	if r, _ := e.hub.Read("pir_hall", "state"); r.Value != true {
		t.Fatalf("pir value %+v", r)
	}
}

func TestInterlockOnSensorState(t *testing.T) {
	gateJSON := `{"devices": {"relay_fan": {"safe_state": {"state": false}}},
	 "interlocks": [{"name": "overheat_keep_fan", "device": "relay_fan", "tool": "*",
	   "deny": [{"if": {"all": [{"arg": "value", "eq": false}, {"state": "kitchen_bme280.temperature", "ge": 35}]},
	             "reason": "перегрев: вентилятор нельзя выключать"}]}]}`
	e := newEnv(t, gateJSON, false)
	if res := e.set(true); !res.Executed {
		t.Fatalf("%+v", res)
	}
	e.write("sys/bus/iio/devices/iio:device0/in_temp_input", "36000\n")
	e.advance(1)
	res := e.set(false)
	if res.Decision.Reason != gate.ReasonInterlock || res.Executed || e.lineLevel(27) != "1" {
		t.Fatalf("interlock: %+v", res)
	}
	if res.Decision.State["kitchen_bme280.temperature"].Value != 36.0 {
		t.Fatalf("decision state %+v", res.Decision.State)
	}
	// импульс не задевает правило (нет аргумента value) — разрешён
	if res = e.hub.Actuate(ActRequest{Device: "relay_fan", Tool: "pulse", Args: map[string]any{"seconds": json.Number("1")}}); !res.Executed {
		t.Fatalf("pulse: %+v", res)
	}
	// датчик пропал: одиночный сбой чтения оставляет последнее хорошее значение (блокировка действует)
	_ = os.Remove(filepath.Join(e.root, "sys/bus/iio/devices/iio:device0/in_temp_input"))
	_ = os.Remove(filepath.Join(e.root, "sys/bus/iio/devices/iio:device0/in_temp_raw"))
	e.advance(1)
	if res = e.set(false); res.Decision.Reason != gate.ReasonInterlock || res.Executed {
		t.Fatalf("single failed read must keep the last good value: %+v", res)
	}
	// после FailLimit сбоев подряд значение неизвестно -> stale_state для любого инструмента реле ("*")
	e.advance(DefaultFailLimit - 1)
	if res = e.set(false); res.Decision.Reason != gate.ReasonStaleState {
		t.Fatalf("unknown state: %+v", res)
	}
	n := 0
	for _, r := range e.records("sample_error") {
		if r["device"] == "kitchen_bme280" {
			n++
		}
	}
	if n != 1 {
		t.Fatalf("sample error must be traced once per transition, got %d", n)
	}
}

func TestLeaseWatchdog(t *testing.T) {
	e := newEnv(t, `{"devices": {"relay_fan": {"lease_s": 2, "safe_state": {"state": false}}}}`, false)
	if e.hub.Inventory().Device("relay_fan").Action(inventory.ActionRenewLease) == nil {
		t.Fatal("leased device must offer renew_lease")
	}
	start := e.clk.Now()
	if res := e.set(true); !res.Executed || res.LeaseUntil == nil || *res.LeaseUntil != start+2 {
		t.Fatalf("%+v", res)
	}
	e.advance(1.5)
	ren := e.hub.Actuate(ActRequest{Device: "relay_fan", Tool: inventory.ActionRenewLease})
	if ren.Renewed == nil || !*ren.Renewed || *ren.LeaseUntil != start+3.5 {
		t.Fatalf("renew %+v", ren)
	}
	e.advance(1.0) // t = start+2.5: аренда продлена до +3.5
	if e.lineLevel(27) != "1" {
		t.Fatal("renewed lease must keep the relay on")
	}
	e.advance(10)
	if e.lineLevel(27) != "0" {
		t.Fatal("expired lease must switch the relay off")
	}
	recs := e.records("lease_expired")
	if len(recs) != 1 || recs[0]["ts_clock"] != start+3.5 || recs[0]["ok"] != true {
		t.Fatalf("lease_expired at exact virtual time: %v", recs)
	}
	ren = e.hub.Actuate(ActRequest{Device: "relay_fan", Tool: inventory.ActionRenewLease})
	if ren.Decision.Reason != ReasonNoActiveLease {
		t.Fatalf("renew without lease: %+v", ren)
	}
}

func TestPulse(t *testing.T) {
	e := newEnv(t, relayPolicy, false)
	start := e.clk.Now()
	res := e.hub.Actuate(ActRequest{Device: "relay_fan", Tool: "pulse", Args: map[string]any{"seconds": json.Number("1.5")}})
	if !res.Executed || *res.PulseUntil != start+1.5 || e.lineLevel(27) != "1" {
		t.Fatalf("%+v", res)
	}
	e.advance(10)
	if e.lineLevel(27) != "0" {
		t.Fatal("pulse must end")
	}
	recs := e.records("pulse_end")
	if len(recs) != 1 || recs[0]["ts_clock"] != start+1.5 {
		t.Fatalf("pulse_end: %v", recs)
	}
	for _, bad := range []string{`{"seconds": 0}`, `{"seconds": 3601}`, `{}`, `{"seconds": "1"}`} {
		a, _ := argschema.Decode([]byte(bad))
		if r := e.hub.Actuate(ActRequest{Device: "relay_fan", Tool: "pulse", Args: a}); r.Decision.Reason != gate.ReasonInvalidArgs {
			t.Errorf("pulse %s: %+v", bad, r)
		}
	}
}

func TestConfirmationFromHost(t *testing.T) {
	e := newEnv(t, relayPolicy, true)
	res := e.set(true)
	if res.Decision.Verdict != gate.Confirm || res.Executed {
		t.Fatalf("%+v", res)
	}
	res = e.set(true, func(r *ActRequest) { r.Confirmed = true })
	if !res.Executed {
		t.Fatalf("confirmed: %+v", res)
	}
	e.advance(3) // за пределами min_interval: частота проверяется раньше подтверждения
	pulse := e.hub.Actuate(ActRequest{Device: "relay_fan", Tool: "pulse", Args: map[string]any{"seconds": 1.0}})
	if pulse.Decision.Verdict != gate.Confirm {
		t.Fatalf("pulse needs confirmation too: %+v", pulse)
	}
}

func TestDryRunDoesNotActuate(t *testing.T) {
	e := newEnv(t, relayPolicy, false)
	res := e.set(true, func(r *ActRequest) { r.DryRun = true })
	if res.Decision.Verdict != gate.Allow || res.Executed || e.lineLevel(27) != "0" {
		t.Fatalf("%+v", res)
	}
	if res = e.set(true); !res.Executed {
		t.Fatalf("dry run must not consume the rate limit: %+v", res)
	}
}

func TestReadCache(t *testing.T) {
	e := newEnv(t, relayPolicy, false)
	temp := "sys/bus/iio/devices/iio:device0/in_temp_input"
	e.write(temp, "25000\n")
	if r, _ := e.hub.Read("kitchen_bme280", "temperature"); r.Value != 22.51 {
		t.Fatalf("fresh cache must be used: %+v", r)
	}
	_ = e.clk.Set(e.clk.Now() + 5) // без Step: кэш старше CacheTTL (2 периода)
	if r, _ := e.hub.Read("kitchen_bme280", "temperature"); r.Value != 25.0 {
		t.Fatalf("stale cache must be re-read: %+v", r)
	}
	if _, err := e.hub.Read("nosuch", "x"); err != ErrUnknownDevice {
		t.Fatal(err)
	}
	if _, err := e.hub.Read("kitchen_bme280", "x"); err != ErrUnknownProperty {
		t.Fatal(err)
	}
}

func TestCloseAppliesSafeState(t *testing.T) {
	e := newEnv(t, relayPolicy, false)
	e.set(true)
	e.hub.Close()
	if e.lineLevel(27) != "0" || len(e.records("shutdown_safe_state")) != 1 {
		t.Fatal("shutdown must apply safe_state")
	}
}

func TestPolicyValidation(t *testing.T) {
	for name, g := range map[string]string{
		"unknown state key":   `{"devices":{"relay_fan":{}},"interlocks":[{"name":"x","device":"relay_fan","tool":"*","deny":[{"if":{"state":"kitchen.temp","gt":1},"reason":"r"}]}]}`,
		"sensor as actuator":  `{"devices":{"kitchen_bme280":{"min_interval_s":1}}}`,
		"safe_state not bool": `{"devices":{"relay_fan":{"safe_state":{"state":0}}}}`,
		"safe_state unknown":  `{"devices":{"relay_fan":{"safe_state":{"speed":false}}}}`,
		"custom tools":        `{"devices":{"relay_fan":{"tools":{"set_speed":{"type":"object"}}}}}`,
		"device not in inv":   `{"devices":{"heater":{"tools":{"set_power":{"type":"object"}}}}}`,
	} {
		if err := fixtureEnv(t).start(g, false); err == nil {
			t.Errorf("%s: expected an error", name)
		}
	}
}

func TestBoardHealthOnFixture(t *testing.T) {
	e := newEnv(t, relayPolicy, false)
	e.write("proc/loadavg", "0.10 0.20 0.30 1/100 1234\n")
	e.write("proc/meminfo", "MemTotal:        4096000 kB\nMemAvailable:    2048000 kB\n")
	bh := e.hub.BoardHealth()
	if bh["cpu_temp_c"] != 48.3 || bh["uptime_s"] != nil {
		t.Fatalf("%v", bh)
	}
	if l := bh["load"].([]any); len(l) != 3 || l[2] != 0.30 {
		t.Fatalf("load %v", bh["load"])
	}
	if m := bh["memory"].(map[string]any); m["available_mib"] != 2000.0 {
		t.Fatalf("memory %v", m)
	}
	// счётчики hw-mcp и ресурсы процесса — не для LLM: они в Diag (hw://diag), а не в board_health
	if _, has := bh["hw_mcp"]; has {
		t.Fatalf("board_health must not carry hw-mcp counters: %v", bh)
	}
	if _, has := bh["process"]; has {
		t.Fatalf("board_health must not carry process stats: %v", bh)
	}
	d := e.hub.Diag()
	if d["hw_mcp"].(map[string]any)["devices"] != 13 { // в declaredBase нет последовательного порта
		t.Fatalf("stats %v", d["hw_mcp"])
	}
	if d["process"] == nil {
		t.Fatalf("diag without process stats: %v", d)
	}
}

func TestConcurrentActuateAndStep(t *testing.T) {
	e := newEnv(t, `{"devices": {"relay_fan": {"lease_s": 0.5, "safe_state": {"state": false}}}}`, false)
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			for j := 0; j < 50; j++ {
				e.set(j%2 == 0)
				_, _ = e.hub.Read("kitchen_bme280", "temperature")
				e.hub.DeviceState("relay_fan")
				e.hub.Stats()
			}
		}(i)
	}
	for k := 0; k < 100; k++ {
		e.advance(0.1)
	}
	wg.Wait()
}

// fakeBackend — линии в памяти; фронты порождает тест.
type fakeBackend struct {
	mu    sync.Mutex
	edges map[int]func(bool)
	vals  map[int]bool
}

type fakeLine struct {
	b   *fakeBackend
	cfg gpio.LineConfig
}

func (f *fakeBackend) Name() string { return "fake" }

func (f *fakeBackend) Open(cfg gpio.LineConfig) (gpio.Line, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if cfg.OnEdge != nil {
		f.edges[cfg.Offset] = cfg.OnEdge
	}
	if cfg.Output {
		f.vals[cfg.Offset] = cfg.Initial
	}
	return &fakeLine{f, cfg}, nil
}

func (l *fakeLine) Get() (bool, error) {
	l.b.mu.Lock()
	defer l.b.mu.Unlock()
	return l.b.vals[l.cfg.Offset], nil
}

func (l *fakeLine) Set(v bool) error {
	l.b.mu.Lock()
	defer l.b.mu.Unlock()
	l.b.vals[l.cfg.Offset] = v
	return nil
}

func (l *fakeLine) Close() error { return nil }

func TestEdgeEventsNotifyImmediately(t *testing.T) {
	root := testutil.FixtureCopy(t)
	raw := strings.Replace(strings.Replace(declaredBase, "%GATE%", relayPolicy, 1), "%CONFIRM%", "false", 1)
	decl, _ := declared.Parse([]byte(raw))
	cfg, _ := gate.ParseConfig(decl.Gate)
	inv, err := inventory.Build(sysfs.Probe(root, decl), decl, inventory.Options{})
	if err != nil {
		t.Fatal(err)
	}
	fb := &fakeBackend{edges: map[int]func(bool){}, vals: map[int]bool{}}
	clk := clock.NewVirtual(0)
	h, err := New(inv, cfg, fb, Options{Clock: clk, SamplePeriod: 60, Edges: true})
	if err != nil {
		t.Fatal(err)
	}
	if fb.edges[17] == nil || fb.edges[27] != nil {
		t.Fatal("edges must be requested for inputs only")
	}
	var got []string
	var mu sync.Mutex
	h.OnChange(func(u Update) {
		mu.Lock()
		got = append(got, u.Device)
		mu.Unlock()
	})
	_ = clk.Set(0.25)
	fb.edges[17](true) // короткий импульс PIR между опросами (период 60 с)
	_ = clk.Set(0.5)
	fb.edges[17](false)
	mu.Lock()
	defer mu.Unlock()
	if len(got) != 2 || got[0] != "pir_hall" {
		t.Fatalf("notifications %v", got)
	}
	if h.Stats()["edges"] != int64(2) {
		t.Fatalf("stats %v", h.Stats())
	}
}

// Блокировка по состоянию другого выхода: значение выхода известно всегда (линия занята hw-mcp),
// поэтому не устаревает между командами.
func TestInterlockOnOutputStateStaysFresh(t *testing.T) {
	g := `{"devices": {"relay_fan": {"safe_state": {"state": false}}},
	 "interlocks": [{"name": "fan_only_when_motion_known", "device": "relay_fan", "tool": "pulse",
	   "deny": [{"if": {"state": "relay_fan.state", "eq": true}, "reason": "already on"}]}]}`
	e := newEnv(t, g, false)
	e.advance(30) // далеко за max_state_age_s
	res := e.hub.Actuate(ActRequest{Device: "relay_fan", Tool: "pulse", Args: map[string]any{"seconds": 1.0}})
	if !res.Executed {
		t.Fatalf("output state must stay fresh: %+v", res)
	}
	res = e.hub.Actuate(ActRequest{Device: "relay_fan", Tool: "pulse", Args: map[string]any{"seconds": 1.0}})
	if res.Decision.Reason != gate.ReasonInterlock {
		t.Fatalf("%+v", res)
	}
}

func TestSamplePeriodMustBeShorterThanStateAge(t *testing.T) {
	root := testutil.FixtureCopy(t)
	g := `{"max_state_age_s": 1, "devices": {"relay_fan": {}},
	 "interlocks": [{"name": "x", "device": "relay_fan", "tool": "*",
	   "deny": [{"if": {"state": "kitchen_bme280.temperature", "ge": 50}, "reason": "hot"}]}]}`
	raw := strings.Replace(strings.Replace(declaredBase, "%GATE%", g, 1), "%CONFIRM%", "false", 1)
	decl, _ := declared.Parse([]byte(raw))
	cfg, _ := gate.ParseConfig(decl.Gate)
	inv, _ := inventory.Build(sysfs.Probe(root, decl), decl, inventory.Options{})
	_, err := New(inv, cfg, &gpio.Files{Dir: t.TempDir()}, Options{Clock: clock.NewVirtual(0), SamplePeriod: 1})
	if err == nil || !strings.Contains(err.Error(), "max_state_age_s") {
		t.Fatalf("expected period/age error, got %v", err)
	}
}

// Периоды опроса медленных драйверов: DS18B20 (w1) и DHT11 опрашиваются реже общего периода.
func TestDriverSamplePeriods(t *testing.T) {
	e := newEnv(t, relayPolicy, false)
	got := e.hub.SamplePeriods()
	for key, want := range map[string][2]any{
		"kitchen_bme280.temperature":      {1.0, "default"},
		"balcony_dht11.temperature":       {DefaultDriverPeriods["dht11"], "driver"},
		"boiler_pipe_ds18b20.temperature": {DefaultDriverPeriods["w1"], "driver"},
	} {
		p, ok := got[key].(map[string]any)
		if !ok || p["period_s"] != want[0] || p["source"] != want[1] {
			t.Errorf("%s: %v, want %v", key, got[key], want)
		}
	}
}

// Дрожание младшего разряда DHT11 (1 °C при пороге 0.2) не даёт шторма уведомлений, изменение на
// один шаг сообщается, когда продержалось Persist отсчётов, а одиночный сбой чтения не порождает
// уведомлений (остаётся последнее хорошее значение).
func TestQuantizationJitterAndGlitches(t *testing.T) {
	e := newEnv(t, relayPolicy, false)
	temp := "sys/devices/platform/dht11@4/iio:device1/in_temp_input"
	period := DefaultDriverPeriods["dht11"]
	dht := func() []string {
		var out []string
		for _, n := range e.takeNotes() {
			if strings.HasPrefix(n, "balcony_dht11:") {
				out = append(out, n)
			}
		}
		return out
	}
	e.takeNotes()
	for i := 0; i < 20; i++ { // 23.1 <-> 24.1 на каждом опросе
		e.write(temp, map[bool]string{true: "24100\n", false: "23100\n"}[i%2 == 0])
		e.advance(period)
	}
	if n := dht(); len(n) != 1 {
		t.Fatalf("jitter of one quantization step: %d notifications %v, want 1", len(n), n)
	}
	e.write(temp, "26100\n") // два шага от сообщённого 24.1 — сразу
	e.advance(period)
	if n := dht(); len(n) != 1 {
		t.Fatalf("change above one step must be reported at once: %v", n)
	}
	e.write(temp, "27100\n") // один шаг — после Persist отсчётов подряд
	for i := 1; i < DefaultPersist; i++ {
		e.advance(period)
		if n := dht(); len(n) != 0 {
			t.Fatalf("one-step change reported after %d samples: %v", i, n)
		}
	}
	e.advance(period)
	if n := dht(); len(n) != 1 {
		t.Fatalf("one-step change held for %d samples must be reported: %v", DefaultPersist, n)
	}
	// одиночные сбои чтения (меньше FailLimit подряд): ни уведомлений, ни null, ни sample_error
	path := filepath.Join(e.root, temp)
	_ = os.Remove(path)
	e.advance(period * float64(DefaultFailLimit-1))
	st, _ := e.hub.DeviceState("balcony_dht11") // без перечитывания (оно тоже считалось бы попыткой)
	if v := st["properties"].(map[string]any)["temperature"].(map[string]any); v["value"] != 27.1 || v["error"] != nil {
		t.Fatalf("last good value must be kept: %v", v)
	}
	e.write(temp, "27100\n")
	e.advance(period)
	if n := dht(); len(n) != 0 {
		t.Fatalf("glitches must not notify: %v", n)
	}
	if g := e.hub.Stats()["sample_glitches"]; g != int64(DefaultFailLimit-1) {
		t.Fatalf("glitches %v", g)
	}
	for _, r := range e.records("sample_error") {
		if r["device"] == "balcony_dht11" {
			t.Fatalf("sample_error for a glitch: %v", r)
		}
	}
}

// Циклы реального времени спят до ближайшего срока: без аренд и импульсов таймер не просыпается
// вовсе, аренда истекает в свой срок, сэмплер просыпается не чаще сроков опроса (не по тикеру).
func TestRealTimeLoopsSleepUntilDue(t *testing.T) {
	root := testutil.FixtureCopy(t)
	gdir := filepath.Join(root, "dev")
	raw := strings.Replace(strings.Replace(declaredBase, "%GATE%",
		`{"devices": {"relay_fan": {"lease_s": 0.2, "safe_state": {"state": false}}}}`, 1), "%CONFIRM%", "false", 1)
	decl, err := declared.Parse([]byte(raw))
	if err != nil {
		t.Fatal(err)
	}
	cfg, _ := gate.ParseConfig(decl.Gate)
	inv, err := inventory.Build(sysfs.Probe(root, decl), decl, inventory.Options{Leased: map[string]bool{"relay_fan": true}})
	if err != nil {
		t.Fatal(err)
	}
	const period = 0.1
	h, err := New(inv, cfg, &gpio.Files{Dir: gdir}, Options{Clock: clock.NewReal(), SamplePeriod: period})
	if err != nil {
		t.Fatal(err)
	}
	defer h.Close()
	ctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); h.RunTimers(ctx) }()
	go func() { defer wg.Done(); h.RunSampler(ctx) }()
	defer func() { cancel(); wg.Wait() }()
	start := time.Now()
	time.Sleep(500 * time.Millisecond)
	if w := h.Stats()["timer_wakeups"]; w != int64(0) {
		t.Fatalf("timers woke %v times without leases or pulses", w)
	}
	// не больше одного пробуждения на срок опроса (+1 на несовпадение сроков медленных драйверов)
	if w, max := h.Stats()["sample_wakeups"].(int64), int64(time.Since(start).Seconds()/period)+2; w > max || w == 0 {
		t.Fatalf("sampler woke %d times in %v, want 1..%d", w, time.Since(start), max)
	}
	res := h.Actuate(ActRequest{Device: "relay_fan", Tool: "set_state", Args: map[string]any{"value": true}})
	if !res.Executed || res.LeaseUntil == nil {
		t.Fatalf("%+v", res)
	}
	level := func() string {
		b, _ := os.ReadFile(filepath.Join(gdir, "gpiochip0", "line27"))
		return strings.TrimSpace(string(b))
	}
	deadline := time.Now().Add(5 * time.Second)
	for level() != "0" {
		if time.Now().After(deadline) {
			t.Fatal("lease watchdog did not fire in real time")
		}
		time.Sleep(10 * time.Millisecond)
	}
	// сработал ровно по сроку аренды (одно пробуждение; второе — только если таймер пришёл на
	// единицу округления часов раньше срока)
	if w := h.Stats()["timer_wakeups"].(int64); w < 1 || w > 2 {
		t.Fatalf("timer wakeups for one lease: %d", w)
	}
}
