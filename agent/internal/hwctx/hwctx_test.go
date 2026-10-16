package hwctx

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const invDoc = `{"devices":[
 {"id":"t1","location":"lab","properties":[
   {"name":"temperature","unit":"Cel","writable":false,"value":22.513},
   {"name":"humidity","unit":"%RH","value":41.2}]},
 {"id":"relay1","location":"lab","properties":{"on":{"type":"boolean","writable":true,"value":false}},"actions":["pulse"]},
 {"id":"door1","properties":{"open":{"type":"boolean","readOnly":true}}},
 {"id":"heater","properties":[{"name":"setpoint","unit":"Cel","rw":"rw","range":[15,30],"precision":0.5}]}
]}`

func TestParseInventoryAndText(t *testing.T) {
	inv, vals := ParseInventory("sim", invDoc)
	if len(inv.Devices) != 4 || inv.Devices[0].ID != "door1" {
		t.Fatalf("devices %+v", inv.Devices)
	}
	if vals["t1"]["temperature"] != 22.513 || vals["relay1"]["on"] != false {
		t.Fatalf("values %+v", vals)
	}
	want := strings.Join([]string{
		"Устройства (id (место): свойство [единица, rw — запись, мин..макс]):",
		"door1: open [bool]",
		"heater: setpoint [Cel, rw, 15..30]",
		"relay1 (lab): on [bool, rw]; действия: pulse",
		"t1 (lab): humidity [%RH], temperature [Cel]",
	}, "\n")
	if got := inv.Text(); got != want {
		t.Fatalf("text:\n%s\nwant:\n%s", got, want)
	}
}

// Инвентарь в формате прототипа GAP2 (sysfs_probe.py): массив устройств с channels.
func TestParseInventoryGAP2Fixture(t *testing.T) {
	p := filepath.Join("..", "..", "..", "research", "new_topic", "GAP2_artifacts", "inventory_fixture.json")
	data, err := os.ReadFile(p)
	if err != nil {
		t.Skip("GAP2 fixture not available:", err)
	}
	inv, vals := ParseInventory("hw", string(data))
	if len(inv.Devices) < 5 || len(inv.Raw) != 0 {
		t.Fatalf("devices %d raw %v", len(inv.Devices), inv.Raw)
	}
	d, ok := inv.Find("iio:bme280@i2c-1/0x76")
	if !ok || d.Location != "kitchen" || len(d.Props) != 3 || d.Props[2].Unit != "Cel" {
		t.Fatalf("bme280 %+v", d)
	}
	if vals["iio:bme280@i2c-1/0x76"]["temperature"] != 22.51 {
		t.Fatalf("value %+v", vals["iio:bme280@i2c-1/0x76"])
	}
	if !strings.Contains(inv.Text(), "iio:bme280@i2c-1/0x76 (kitchen): humidity [%RH], pressure [Pa], temperature [Cel]") {
		t.Fatalf("text:\n%s", inv.Text())
	}
}

func TestUnparsedInventoryFallsBackToRaw(t *testing.T) {
	inv, _ := ParseInventory("odd", `{"rooms": {"lab": 3},  "b": "Привет"}`)
	if len(inv.Devices) != 0 || inv.Raw["odd"] != `{"b":"Привет","rooms":{"lab":3}}` {
		t.Fatalf("%+v", inv)
	}
	if !strings.Contains(inv.Text(), "Инвентарь сервера odd: {") {
		t.Fatal(inv.Text())
	}
	inv2, _ := ParseInventory("txt", "relay1: on [rw]")
	if inv2.Raw["txt"] != "relay1: on [rw]" {
		t.Fatalf("%+v", inv2)
	}
}

func newCtx(mode string) *Context {
	c := New(mode, 60, 3)
	inv, vals := ParseInventory("sim", invDoc)
	c.SetInventory(inv)
	for d, v := range vals {
		c.Seed(0, d, v)
	}
	return c
}

func TestApplyRoundingAndChanges(t *testing.T) {
	c := newCtx(DeltaTail)
	if v, _ := c.Get("t1", "temperature"); v != 22.5 {
		t.Fatalf("seed rounding: %v", v)
	}
	if ch := c.Apply(10, "t1", map[string]any{"temperature": 22.52}); len(ch) != 0 {
		t.Fatalf("jitter below precision must not change: %+v", ch)
	}
	ch := c.Apply(20, "t1", map[string]any{"temperature": 22.66, "humidity": 41.4})
	if len(ch) != 1 || ch[0].Prop != "temperature" || ch[0].Value != 22.7 || ch[0].Old != 22.5 || !ch[0].HadOld {
		t.Fatalf("changes %+v", ch)
	}
	ch = c.Apply(30, "pir1", map[string]any{"motion": 1.0})
	if len(ch) != 1 || ch[0].HadOld || ch[0].Value != 1.0 {
		t.Fatalf("new device %+v", ch)
	}
	if v := c.Apply(40, "heater", map[string]any{"setpoint": 21.3}); v[0].Value != 21.5 {
		t.Fatalf("precision 0.5: %+v", v)
	}
}

func TestIntegerReadingsRespectCoarsePrecision(t *testing.T) {
	c := New(DeltaTail, 60, 3)
	inv, _ := ParseInventory("fixture", `{"devices":[{"id":"meter","properties":[{"name":"reading","precision":10}]}]}`)
	c.SetInventory(inv)
	c.Seed(0, "meter", map[string]any{"reading": 100.0})
	if changes := c.Apply(1, "meter", map[string]any{"reading": 101.0}); len(changes) != 0 {
		t.Fatalf("integer jitter below precision changed state: %+v", changes)
	}
	changes := c.Apply(2, "meter", map[string]any{"reading": 106.0})
	if len(changes) != 1 || changes[0].Value != 110.0 {
		t.Fatalf("coarse integer rounding: %+v", changes)
	}
}

func TestDeltaTextCommit(t *testing.T) {
	c := newCtx(DeltaTail)
	first, shown := c.DeltaText(100)
	if !strings.Contains(first, "t1.temperature=22.5 Cel [1м]") || !strings.Contains(first, "relay1.on=false") {
		t.Fatalf("first delta must contain full state:\n%s", first)
	}
	again, _ := c.DeltaText(100)
	if again != first {
		t.Fatal("without Commit (LLM failed) the same delta must be shown again")
	}
	c.Commit(shown)
	if d, s := c.DeltaText(100); d != "" || s != nil {
		t.Fatalf("no changes after commit -> empty delta, got %q", d)
	}
	c.Apply(200, "relay1", map[string]any{"on": true})
	d, shown := c.DeltaText(230)
	if d != "Изменения состояния с прошлого запроса [возраст]:\nrelay1.on: false→true [<1м]" {
		t.Fatalf("delta:\n%s", d)
	}
	c.Commit(shown)
	if d, _ := c.DeltaText(231); d != "" {
		t.Fatalf("committed change shown again:\n%s", d)
	}
}

// Падение сервера: показания всех его устройств «нет связи» и неизвестны, пока
// не придёт свежее показание именно этого устройства.
func TestStaleServer(t *testing.T) {
	c := newCtx(DeltaTail)
	_, shown := c.DeltaText(100)
	c.Commit(shown)
	c.MarkStale("sim")
	if _, ok := c.Get("relay1", "on"); ok {
		t.Fatal("reading of a server without connection must be unknown")
	}
	d, shown := c.DeltaText(200)
	if !strings.Contains(d, "relay1.on=false [3м, нет связи]") || !strings.Contains(d, "t1.temperature=22.5 Cel [3м, нет связи]") {
		t.Fatalf("lost connection must be shown as a change:\n%s", d)
	}
	c.Commit(shown)
	if d, _ := c.DeltaText(300); d != "" {
		t.Fatalf("stale state shown twice:\n%s", d)
	}
	snap := New(SnapshotHead, 60, 3)
	snap.SetInventory(c.Inventory())
	snap.Seed(0, "door1", map[string]any{"open": true})
	snap.MarkStale("sim")
	if s := snap.SnapshotText(120); !strings.Contains(s, "door1: open=true [2м, нет связи]") {
		t.Fatalf("snapshot:\n%s", s)
	}
	c.Apply(400, "relay1", map[string]any{"on": true})
	d, shown = c.DeltaText(400)
	if d != "Изменения состояния с прошлого запроса [возраст]:\nrelay1.on: false→true [<1м]" {
		t.Fatalf("only the device with a fresh reading is restored:\n%s", d)
	}
	c.Commit(shown)
	if v, ok := c.Get("relay1", "on"); !ok || v != true {
		t.Fatal("fresh reading must be known again")
	}
	if _, ok := c.Get("t1", "temperature"); ok {
		t.Fatal("a device not re-read after reconnection must stay unknown")
	}
	if ch := c.Apply(410, "t1", map[string]any{"temperature": 22.5, "humidity": 41.0}); len(ch) != 0 {
		t.Fatalf("unchanged reading must not be a change: %v", ch)
	}
	if d, _ := c.DeltaText(410); !strings.Contains(d, "t1.temperature=22.5 Cel [6м]") || strings.Contains(d, "нет связи") {
		t.Fatalf("unchanged fresh reading clears the mark:\n%s", d)
	}
}

func TestSnapshotPeriod(t *testing.T) {
	c := newCtx(SnapshotHead)
	s1 := c.SnapshotText(1000)
	if !strings.Contains(s1, "t1: humidity=41 %RH, temperature=22.5 Cel [16м]") {
		t.Fatalf("snapshot:\n%s", s1)
	}
	c.Apply(1010, "relay1", map[string]any{"on": true})
	if c.SnapshotText(1030) != s1 {
		t.Fatal("snapshot must be frozen within the period")
	}
	s2 := c.SnapshotText(1060)
	if s2 == s1 || !strings.Contains(s2, "relay1: on=true [<1м]") {
		t.Fatalf("snapshot after period:\n%s", s2)
	}
}

func TestRecentRing(t *testing.T) {
	c := newCtx(Pull)
	for i, d := range []string{"a", "b", "a", "c", "d"} {
		c.Record(Event{T: float64(i), Device: d, Prop: "x", Value: i})
	}
	if c.LogLen() != 3 {
		t.Fatalf("ring cap: %d", c.LogLen())
	}
	r := c.Recent(10, "", "")
	if len(r) != 3 || r[0].Device != "a" || r[1].Device != "c" || r[2].Device != "d" {
		t.Fatalf("recent %+v", r)
	}
	if r := c.Recent(1, "a", "x"); len(r) != 1 || r[0].T != 2 {
		t.Fatalf("filtered %+v", r)
	}
	if r := c.Recent(2, "", ""); len(r) != 2 || r[0].T != 3 || r[1].T != 4 {
		t.Fatalf("last two %+v", r)
	}
}

func TestParseState(t *testing.T) {
	cases := []struct {
		uri, raw, dev string
		props         map[string]any
	}{
		{"hw://state/door1", `{"open": true, "ts": 123}`, "door1", map[string]any{"open": true}},
		{"hw://state/x", `{"device":"t1","properties":{"temperature":{"value":22.5,"ts":1},"humidity":40}}`, "t1", map[string]any{"temperature": 22.5, "humidity": 40.0}},
		{"sim://state/pir1", `{"property":"motion","value":1}`, "pir1", map[string]any{"motion": 1.0}},
		{"hw://devices/relay1", `{"state":{"on":false}}`, "relay1", map[string]any{"on": false}},
		{"", `[{"bn":"urn:dev:t2:","n":"temperature","v":21},{"n":"ok","vb":true}]`, "t2", map[string]any{"temperature": 21.0, "ok": true}},
	}
	for _, c := range cases {
		dev, props, ok := ParseState(c.uri, c.raw)
		if !ok || dev != c.dev || len(props) != len(c.props) {
			t.Errorf("%s %s -> %s %v %v", c.uri, c.raw, dev, props, ok)
			continue
		}
		for k, v := range c.props {
			if props[k] != v {
				t.Errorf("%s: %s=%v want %v", c.raw, k, props[k], v)
			}
		}
	}
	if _, _, ok := ParseState("hw://state/a", "not json"); ok {
		t.Fatal("bad json must fail")
	}
	if DeviceOfURI("cam://state/cam1/") != "cam1" || DeviceOfURI("sim://x/y") != "y" || DeviceOfURI("") != "" {
		t.Fatal("DeviceOfURI")
	}
}

func TestFormatValueUnits(t *testing.T) {
	c := newCtx(Pull)
	if s := c.FormatValue("t1", "temperature", 22.5); s != "22.5 Cel" {
		t.Fatal(s)
	}
	if s := c.FormatValue("relay1", "on", true); s != "true" {
		t.Fatal(s)
	}
	if s := c.FormatValue("unknown", "x", "текст"); s != `"текст"` {
		t.Fatal(s)
	}
}

func TestRecordFullRingDoesNotAllocate(t *testing.T) {
	c := New(Pull, 60, 64)
	ev := Event{Device: "t1", Prop: "temperature", Value: 22.5}
	for i := 0; i < 64; i++ {
		c.Record(ev)
	}
	if n := testing.AllocsPerRun(1000, func() { c.Record(ev) }); n != 0 {
		t.Fatalf("Record allocates %v times per event in a full ring", n)
	}
}
