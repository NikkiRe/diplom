package sysfs

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"testing"
	"time"

	"golang.org/x/sys/unix"

	"github.com/local/hwmcp/internal/declared"
	"github.com/local/hwmcp/internal/testutil"
)

func loadDeclared(t *testing.T) *declared.Declared {
	t.Helper()
	d, err := declared.Load(testutil.Testdata("declared.json"))
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func probeFixture(t *testing.T) ([]*Device, map[string]*Device) {
	t.Helper()
	inv := Probe(testutil.Testdata("fixture_root"), loadDeclared(t))
	by := map[string]*Device{}
	for _, d := range inv {
		by[d.ID] = d
	}
	return inv, by
}

func channel(t *testing.T, by map[string]*Device, dev, name string) Channel {
	t.Helper()
	d, ok := by[dev]
	if !ok {
		t.Fatalf("no device %s", dev)
	}
	for _, c := range d.Channels {
		if c.Name == name {
			return c
		}
	}
	t.Fatalf("no channel %s.%s", dev, name)
	return Channel{}
}

func approx(t *testing.T, got *float64, want float64) {
	t.Helper()
	if got == nil || math.Abs(*got-want) > 1e-7 {
		t.Fatalf("value %v, want %v", got, want)
	}
}

// --- паритет с прототипом: вся инвентаризация совпадает с эталоном sysfs_probe.py ---

func TestParityWithPrototypeInventory(t *testing.T) {
	for _, tc := range []struct {
		golden string
		decl   bool
	}{{"inventory.json", true}, {"inventory_nodecl.json", false}} {
		t.Run(tc.golden, func(t *testing.T) {
			var decl *declared.Declared
			if tc.decl {
				decl = loadDeclared(t)
			}
			got, err := MarshalInventory(Probe(testutil.Testdata("fixture_root"), decl), 0)
			if err != nil {
				t.Fatal(err)
			}
			want, err := os.ReadFile(testutil.Testdata("golden", tc.golden))
			if err != nil {
				t.Fatal(err)
			}
			if d := testutil.Diff(testutil.MustDecode(t, want), testutil.MustDecode(t, got)); d != "" {
				t.Fatalf("inventory differs from prototype: %s", d)
			}
		})
	}
}

// Порядок ключей тоже как у прототипа (json.dumps словаря): сравниваем последовательность ключей устройств.
func TestParityKeyOrder(t *testing.T) {
	got, _ := MarshalInventory(Probe(testutil.Testdata("fixture_root"), loadDeclared(t)), 0)
	want, _ := os.ReadFile(testutil.Testdata("golden", "inventory.json"))
	g, w := topKeys(t, got), topKeys(t, want)
	if len(g) != len(w) {
		t.Fatalf("device count %d vs %d", len(g), len(w))
	}
	for i := range g {
		if fmt.Sprint(g[i]) != fmt.Sprint(w[i]) {
			t.Fatalf("device %d keys %v vs %v", i, g[i], w[i])
		}
	}
}

// topKeys — ключи верхнего уровня каждого объекта массива в порядке следования.
func topKeys(t *testing.T, b []byte) [][]string {
	t.Helper()
	var raw []json.RawMessage
	if err := json.Unmarshal(b, &raw); err != nil {
		t.Fatal(err)
	}
	var out [][]string
	for _, r := range raw {
		dec := json.NewDecoder(bytes.NewReader(r))
		if _, err := dec.Token(); err != nil { // '{'
			t.Fatal(err)
		}
		var keys []string
		for dec.More() {
			tok, err := dec.Token()
			if err != nil {
				t.Fatal(err)
			}
			keys = append(keys, tok.(string))
			var skip json.RawMessage
			if err := dec.Decode(&skip); err != nil {
				t.Fatal(err)
			}
		}
		out = append(out, keys)
	}
	return out
}

// Копия фикстуры в testdata совпадает с фикстурой исследования (если репозиторий целиком).
func TestFixtureIsResearchFixture(t *testing.T) {
	research := filepath.Join(testutil.RepoRoot(), "research/new_topic/GAP2_artifacts/fixture_root")
	if _, err := os.Stat(research); err != nil {
		t.Skip("research fixture not available")
	}
	a, _ := MarshalInventory(Probe(research, loadDeclared(t)), 0)
	b, _ := MarshalInventory(Probe(testutil.Testdata("fixture_root"), loadDeclared(t)), 0)
	if string(a) != string(b) {
		t.Fatal("testdata/fixture_root differs from research fixture")
	}
}

func TestIIOChannelValueCases(t *testing.T) {
	raw, err := os.ReadFile(testutil.Testdata("golden", "iio_cases.json"))
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Files map[string]string `json:"files"`
		Base  string            `json:"base"`
		Typ   string            `json:"typ"`
		Value *float64          `json:"value"`
		How   string            `json:"how"`
	}
	if err := json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	for i, c := range cases {
		dir := filepath.Join(t.TempDir(), "iio")
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		for name, text := range c.Files {
			testutil.WriteFile(t, filepath.Join(dir, name), text)
		}
		v, how := IIOChannelValue(dir, c.Base, c.Typ)
		if how != c.How || (v == nil) != (c.Value == nil) || (v != nil && *v != *c.Value) {
			t.Errorf("case %d (%v): got (%v, %s), want (%v, %s)", i, c.Files, deref(v), how, deref(c.Value), c.How)
		}
	}
}

func deref(p *float64) any {
	if p == nil {
		return nil
	}
	return *p
}

// --- перенос TestProbe из test_gap2.py ---

func TestIIOUnitsConverted(t *testing.T) {
	_, by := probeFixture(t)
	c := channel(t, by, "iio:bme280@i2c-1/0x76", "temperature")
	approx(t, c.Value, 22.51)
	if c.Unit != "Cel" {
		t.Fatalf("unit %s", c.Unit)
	}
	approx(t, channel(t, by, "iio:bme280@i2c-1/0x76", "humidity").Value, 41.23)
	p := channel(t, by, "iio:bme280@i2c-1/0x76", "pressure")
	approx(t, p.Value, 101325.0)
	if p.Unit != "Pa" {
		t.Fatalf("unit %s", p.Unit)
	}
}

func TestIIORawOffsetScale(t *testing.T) {
	_, by := probeFixture(t)
	c := channel(t, by, "iio:ads1015@i2c-1/0x48", "voltage0")
	if c.How != "(raw+offset)*scale" {
		t.Fatalf("how %s", c.How)
	}
	approx(t, c.Value, (1650-25)*2.0/1000)
}

func TestTypeWideScale(t *testing.T) {
	d := t.TempDir()
	testutil.WriteFile(t, filepath.Join(d, "in_voltage1_raw"), "100\n")
	testutil.WriteFile(t, filepath.Join(d, "in_voltage_scale"), "0.5\n")
	v, how := IIOChannelValue(d, "in_voltage1", "voltage")
	if v == nil || *v != 50.0 || how != "(raw+offset)*scale" {
		t.Fatalf("got %v %s", deref(v), how)
	}
}

func TestRawWithoutScaleIsNotGuessed(t *testing.T) {
	d := t.TempDir()
	testutil.WriteFile(t, filepath.Join(d, "in_temp_raw"), "123\n")
	v, how := IIOChannelValue(d, "in_temp", "temp")
	if v != nil || how != "raw-without-scale" {
		t.Fatalf("got %v %s", deref(v), how)
	}
}

func TestW1AndHwmon(t *testing.T) {
	_, by := probeFixture(t)
	approx(t, channel(t, by, "w1:28-0316a2795cff", "temperature").Value, 21.437)
	w1 := by["w1:28-0316a2795cff"]
	if *w1.Name != "DS18B20" {
		t.Fatalf("name %s", *w1.Name)
	}
	if len(w1.Notes) == 0 || !contains(w1.Notes[0], "750 ms") {
		t.Fatalf("notes %v", w1.Notes)
	}
	approx(t, channel(t, by, "hwmon:cpu_thermal", "temperature1").Value, 48.3)
}

func TestObservabilityLevels(t *testing.T) {
	_, by := probeFixture(t)
	for id, want := range map[string]string{
		"iio:bme280@i2c-1/0x76": "A", "usb:1a86:7523@1-2": "B", "i2c:i2c-1/0x40": "C", "gpio:relay_fan": "C",
	} {
		if got := by[id].Observability; got != want {
			t.Errorf("%s: %s, want %s", id, got, want)
		}
	}
}

func TestI2CClaimedNotDuplicatedAndRootHubHidden(t *testing.T) {
	inv, _ := probeFixture(t)
	var i2c []string
	for _, d := range inv {
		if d.Source == "i2c" {
			i2c = append(i2c, d.ID)
		}
		if len(d.ID) >= 8 && d.ID[:8] == "usb:1d6b" {
			t.Fatalf("root hub not hidden: %s", d.ID)
		}
	}
	if len(i2c) != 1 || i2c[0] != "i2c:i2c-1/0x40" {
		t.Fatalf("i2c %v", i2c)
	}
}

func TestV4L2Grouped(t *testing.T) {
	inv, _ := probeFixture(t)
	var v []*Device
	for _, d := range inv {
		if d.Source == "v4l2" {
			v = append(v, d)
		}
	}
	if len(v) != 1 || len(v[0].Nodes) != 2 || v[0].Nodes[0].Dev != "video0" || v[0].Nodes[1].Dev != "video1" {
		t.Fatalf("v4l2 %+v", v)
	}
}

func TestHATStringsNulStripped(t *testing.T) {
	inv, _ := probeFixture(t)
	for _, d := range inv {
		if d.Source == "hat" {
			if *d.Name != "Special Sensor Board" {
				t.Fatalf("hat name %q", *d.Name)
			}
			return
		}
	}
	t.Fatal("no hat")
}

func TestDeclaredLocationsApplied(t *testing.T) {
	_, by := probeFixture(t)
	if l := by["iio:bme280@i2c-1/0x76"].Location; l == nil || *l != "kitchen" {
		t.Fatalf("location %v", l)
	}
}

// --- дополнительно для Go-порта ---

func TestChannelRereadFollowsFiles(t *testing.T) {
	root := testutil.FixtureCopy(t)
	inv := Probe(root, nil)
	var bme *Device
	for _, d := range inv {
		if d.ID == "iio:bme280@i2c-1/0x76" {
			bme = d
		}
	}
	var temp *Channel
	for i := range bme.Channels {
		if bme.Channels[i].Name == "temperature" {
			temp = &bme.Channels[i]
		}
	}
	testutil.WriteFile(t, filepath.Join(root, "sys/bus/iio/devices/iio:device0/in_temp_input"), "23000\n")
	v, how, ok := temp.Read()
	if !ok || how != "input" || v == nil || *v != 23.0 {
		t.Fatalf("reread %v %s %v", deref(v), how, ok)
	}
	if err := os.Remove(filepath.Join(root, "sys/bus/iio/devices/iio:device0/in_temp_input")); err != nil {
		t.Fatal(err)
	}
	v, how, _ = temp.Read() // остался raw + scale: 2251 × 10 m°C
	if how != "(raw+offset)*scale" || v == nil || *v != 22.51 {
		t.Fatalf("fallback %v %s", deref(v), how)
	}
}

// Повторная инвентаризация не читает значения каналов (чтение запускает измерение на шине): атрибуты
// значений — FIFO без писателя, и чтение любого из них заблокировало бы ProbeRenumbered.
func TestProbeRenumberedReadsNoValues(t *testing.T) {
	root := testutil.FixtureCopy(t)
	attrs := []string{"sys/bus/iio/devices/iio:device0/in_temp_input", "sys/class/hwmon/hwmon0/temp1_input"}
	for _, a := range attrs {
		if err := os.Remove(filepath.Join(root, a)); err != nil {
			t.Fatal(err)
		}
		if err := unix.Mkfifo(filepath.Join(root, a), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	done := make(chan []*Device, 1)
	go func() { done <- ProbeRenumbered(root) }()
	select {
	case devs := <-done:
		paths := map[string]bool{}
		for _, d := range devs {
			for _, c := range d.Channels {
				paths[c.Path] = c.Value == nil
			}
		}
		if !paths[attrs[0]] || !paths[attrs[1]] {
			t.Fatalf("channels found without values: %v", paths)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("ProbeRenumbered read a channel value")
	}
}

func TestEmptyRoot(t *testing.T) {
	inv := Probe(t.TempDir(), nil)
	if len(inv) != 0 {
		t.Fatalf("expected empty inventory, got %d", len(inv))
	}
	b, err := MarshalInventory(inv, 0)
	if err != nil || string(b) != "[]" {
		t.Fatalf("%s %v", b, err)
	}
}

func TestRound6MatchesPython(t *testing.T) {
	// round() Python: корректное округление точного значения, половина — к чётному.
	for _, c := range []struct{ in, want float64 }{
		{22.51, 22.51}, {101.325 * 1e3, 101325.0}, {0.0000005, 0.0}, {0.0000015, 0.000002},
		{1.0000005, 1.000001}, {2.675, 2.675}, {-1.2345675, -1.234568}, {48300 * 1e-3, 48.3},
		{0.0000025, 0.000003}, {2.5e-7, 0.0},
	} {
		if got := round6(c.in); got != c.want {
			t.Errorf("round6(%v) = %v, want %v", c.in, got, c.want)
		}
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
