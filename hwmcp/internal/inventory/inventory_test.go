package inventory

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/santhosh-tekuri/jsonschema/v6"

	"github.com/local/hwmcp/internal/canon"
	"github.com/local/hwmcp/internal/declared"
	"github.com/local/hwmcp/internal/sysfs"
	"github.com/local/hwmcp/internal/testutil"
)

func fixtureInventory(t *testing.T, opts Options) *Inventory {
	t.Helper()
	decl, err := declared.Load(testutil.Testdata("declared.json"))
	if err != nil {
		t.Fatal(err)
	}
	inv, err := Build(sysfs.Probe(testutil.Testdata("fixture_root"), decl), decl, opts)
	if err != nil {
		t.Fatal(err)
	}
	return inv
}

func TestDeviceIDs(t *testing.T) {
	inv := fixtureInventory(t, Options{})
	var ids []string
	for _, d := range inv.Devices {
		ids = append(ids, d.ID+"<-"+d.SourceID)
	}
	want := []string{
		"ads1015<-iio:ads1015@i2c-1/0x48",
		"balcony_dht11<-iio:dht11@dht11@4",
		"board_cpu_thermal<-hwmon:cpu_thermal",
		"boiler_pipe_ds18b20<-w1:28-0316a2795cff",
		"dummy<-i2c:i2c-1/0x40",
		"esp32_bridge<-tty:esp32_bridge",
		"gpiochip0<-gpiochip:gpiochip0",
		"kitchen_bme280<-iio:bme280@i2c-1/0x76",
		"pir_hall<-gpio:pir_hall",
		"relay_fan<-gpio:relay_fan",
		"special_sensor_board<-hat:Special Sensor Board",
		"usb_serial<-usb:1a86:7523@1-2",
		"uvc_camera_046d_0825<-v4l2:UVC Camera (046d:0825)",
		"webcam_c270<-usb:046d:0825@1-1",
	}
	if strings.Join(ids, "\n") != strings.Join(want, "\n") {
		t.Fatalf("ids:\n%s", strings.Join(ids, "\n"))
	}
}

func TestPropertiesAndActions(t *testing.T) {
	inv := fixtureInventory(t, Options{Leased: map[string]bool{"relay_fan": true}})
	bme := inv.Device("kitchen_bme280")
	if bme == nil || len(bme.Properties) != 3 || bme.Property("temperature").Unit != "Cel" ||
		bme.Property("temperature").Threshold != 0.2 || bme.Property("pressure").Threshold != 50 {
		t.Fatalf("bme280: %+v", bme)
	}
	relay := inv.Device("relay_fan")
	st := relay.Property("state")
	if st == nil || !st.Writable || st.Type != TypeBoolean || st.Href != "gpiod://gpiochip0/27" || st.GPIO == nil {
		t.Fatalf("relay state: %+v", st)
	}
	if relay.Action(ActionPulse) == nil || relay.Action(ActionRenewLease) == nil {
		t.Fatalf("relay actions: %+v", relay.Actions)
	}
	if inv.Device("pir_hall").Property("state").Writable || len(inv.Device("pir_hall").Actions) != 0 {
		t.Fatal("PIR is an input")
	}
	tools := inv.GateTools()
	if len(tools) != 1 || tools["relay_fan"]["set_state"] == nil || tools["relay_fan"]["pulse"] == nil ||
		tools["relay_fan"]["renew_lease"] != nil {
		t.Fatalf("gate tools: %v", tools)
	}
}

func TestDeclaredExtensions(t *testing.T) {
	raw := `{"locations":{"iio:bme280@i2c-1/0x76":"kitchen"},
		"names":{"iio:bme280@i2c-1/0x76":"climate","iio:dht11@dht11@4":"climate"},
		"thresholds":{"climate.temperature":0.5}}`
	decl, err := declared.Parse([]byte(raw))
	if err != nil {
		t.Fatal(err)
	}
	inv, err := Build(sysfs.Probe(testutil.Testdata("fixture_root"), decl), decl, Options{})
	if err != nil {
		t.Fatal(err)
	}
	// одинаковые имена разводятся суффиксом в порядке инвентаря
	if inv.Device("climate").SourceID != "iio:bme280@i2c-1/0x76" || inv.Device("climate_2") == nil {
		t.Fatal("alias collision handling")
	}
	if inv.Device("climate").Property("temperature").Threshold != 0.5 {
		t.Fatal("threshold override")
	}
	bad, _ := declared.Parse([]byte(`{"thresholds":{"nosuch.temperature":1}}`))
	if _, err := Build(sysfs.Probe(testutil.Testdata("fixture_root"), bad), bad, Options{}); err == nil {
		t.Fatal("unknown threshold key must fail")
	}
	bad2, _ := declared.Parse([]byte(`{"names":{"iio:nosuch":"x"}}`))
	if _, err := Build(sysfs.Probe(testutil.Testdata("fixture_root"), bad2), bad2, Options{}); err == nil {
		t.Fatal("unknown alias source must fail")
	}
}

func TestTextProjection(t *testing.T) {
	inv := fixtureInventory(t, Options{})
	want := strings.Join([]string{
		"ads1015: voltage0 [V]",
		"balcony_dht11: humidity [%RH], temperature [Cel]",
		"board_cpu_thermal: temperature1 [Cel]",
		"boiler_pipe_ds18b20: temperature [Cel]",
		"kitchen_bme280: humidity [%RH], pressure [Pa], temperature [Cel]",
		"pir_hall: state [/]",
		"relay_fan @kitchen: state [/, rw]; actions: pulse(seconds:number)",
	}, "\n")
	if got := inv.Text(); got != want {
		t.Fatalf("text:\n%s", got)
	}
}

func TestCompactListIsCanonicalAndStable(t *testing.T) {
	a := canon.MustString(fixtureInventory(t, Options{}).CompactList(false))
	b := canon.MustString(fixtureInventory(t, Options{}).CompactList(false))
	if a != b {
		t.Fatal("list must be deterministic")
	}
	if !strings.Contains(a, `{"actions":{"pulse":"seconds:number"},"id":"relay_fan","loc":"kitchen","props":{"state":"bool,rw"}}`) {
		t.Fatalf("compact list: %s", a)
	}
	all := canon.MustString(fixtureInventory(t, Options{}).CompactList(true))
	if !strings.Contains(all, `"webcam_c270"`) || strings.Contains(a, `"webcam_c270"`) {
		t.Fatal("all=true must include devices without properties")
	}
}

// Сгенерированные TD проходят официальную схему W3C TD 1.1 (version 1.1-12-March-2025),
// как в test_gap2.py (TestFormats.test_generated_tds_valid).
func TestTDsValidAgainstOfficialSchema(t *testing.T) {
	c := jsonschema.NewCompiler()
	f, err := os.Open(testutil.Testdata("td-schema-v1.1.json"))
	if err != nil {
		t.Fatal(err)
	}
	doc, err := jsonschema.UnmarshalJSON(f)
	f.Close()
	if err != nil {
		t.Fatal(err)
	}
	if err := c.AddResource("https://www.w3.org/2022/wot/td-schema/v1.1", doc); err != nil {
		t.Fatal(err)
	}
	sch, err := c.Compile("https://www.w3.org/2022/wot/td-schema/v1.1")
	if err != nil {
		t.Fatal(err)
	}
	// валидатор отвергает испорченные TD (иначе проверка ниже ничего не значит)
	var examples []any
	raw, _ := os.ReadFile(testutil.Testdata("td11_spec_examples.json"))
	if err := json.Unmarshal(raw, &examples); err != nil {
		t.Fatal(err)
	}
	for _, ex := range examples {
		if err := sch.Validate(ex); err != nil {
			t.Fatalf("spec example rejected: %v", err)
		}
	}
	broken := map[string]any{"@context": TDContext, "securityDefinitions": map[string]any{"nosec_sc": map[string]any{"scheme": "nosec"}}, "security": "nosec_sc"}
	if sch.Validate(broken) == nil {
		t.Fatal("TD without title must be rejected")
	}

	inv := fixtureInventory(t, Options{Leased: map[string]bool{"relay_fan": true}})
	tds := inv.TDs()
	if len(tds) != 7 {
		t.Fatalf("%d TDs", len(tds))
	}
	for _, td := range tds {
		// через JSON, чтобы валидатор видел те же типы, что и клиент
		b, _ := json.Marshal(td)
		v, err := jsonschema.UnmarshalJSON(strings.NewReader(string(b)))
		if err != nil {
			t.Fatal(err)
		}
		if err := sch.Validate(v); err != nil {
			t.Errorf("TD %s invalid: %v", td.(map[string]any)["title"], err)
		}
	}
}

func TestSlug(t *testing.T) {
	for in, want := range map[string]string{
		"UVC Camera (046d:0825)": "uvc_camera_046d_0825", "DS18B20": "ds18b20", "": "dev",
		"28-0316": "d28_0316", "__x__": "x",
	} {
		if got := Slug(in); got != want {
			t.Errorf("Slug(%q) = %q, want %q", in, got, want)
		}
	}
}
