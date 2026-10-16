package declared

import (
	"strings"
	"testing"

	"github.com/local/hwmcp/internal/testutil"
)

func TestResearchDeclaredParses(t *testing.T) {
	d, err := Load(testutil.Testdata("declared.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(d.GPIO) != 2 || d.GPIO[0].PropertyName() != "state" || d.Locations["hwmon:cpu_thermal"] != "board" {
		t.Fatalf("%+v", d)
	}
}

func TestValidation(t *testing.T) {
	for name, raw := range map[string]string{
		"unknown key":      `{"gpios": []}`,
		"typo in gpio":     `{"gpio": [{"id": "r", "chip": "gpiochip0", "line": 1, "direction": "output", "quantity": "s", "requires_confirm": true}]}`,
		"bad id":           `{"gpio": [{"id": "Relay-1", "chip": "gpiochip0", "line": 1, "direction": "output", "quantity": "s"}]}`,
		"duplicate id":     `{"gpio": [{"id": "r", "chip": "c", "line": 1, "direction": "output", "quantity": "s"}, {"id": "r", "chip": "c", "line": 2, "direction": "input", "quantity": "s"}]}`,
		"same line twice":  `{"gpio": [{"id": "a", "chip": "c", "line": 1, "direction": "output", "quantity": "s"}, {"id": "b", "chip": "c", "line": 1, "direction": "input", "quantity": "s"}]}`,
		"bad direction":    `{"gpio": [{"id": "a", "chip": "c", "line": 1, "direction": "both", "quantity": "s"}]}`,
		"no chip":          `{"gpio": [{"id": "a", "line": 1, "direction": "input", "quantity": "s"}]}`,
		"negative thresh":  `{"thresholds": {"a.b": -1}}`,
		"zero period":      `{"sample_periods": {"sensor": 0}}`,
		"negative period":  `{"sample_periods": {"sensor.value": -1}}`,
		"empty period key": `{"sample_periods": {"": 1}}`,
		"zero stuck":       `{"stuck_after_s": {"sensor": 0}}`,
		"negative stuck":   `{"stuck_after_s": {"sensor.value": -60}}`,
		"bad alias":        `{"names": {"iio:x": "X Y"}}`,
		"serial dup id":    `{"gpio": [{"id": "a", "chip": "c", "line": 1, "direction": "input", "quantity": "s"}], "serial": [{"id": "a", "tty": "ttyUSB0"}]}`,
		"trailing garbage": `{} {}`,
	} {
		if _, err := Parse([]byte(raw)); err == nil {
			t.Errorf("%s: expected an error", name)
		}
	}
	d, err := Parse([]byte(`{"gpio": [{"id": "lamp", "chip": "gpiochip0", "line": 4, "direction": "output",
		"quantity": "switch", "active_low": true, "property": "on"}], "gate": {"devices": {}}}`))
	if err != nil || d.GPIO[0].PropertyName() != "on" || !d.GPIO[0].ActiveLow || !strings.Contains(string(d.Gate), "devices") {
		t.Fatalf("%+v %v", d, err)
	}
}

func TestSamplePeriodsParse(t *testing.T) {
	d, err := Parse([]byte(`{"sample_periods":{"sensor":5,"sensor.temperature":0.5},"stuck_after_s":{"sensor":600}}`))
	if err != nil || d.SamplePeriods["sensor"] != 5 || d.SamplePeriods["sensor.temperature"] != 0.5 ||
		d.StuckAfter["sensor"] != 600 {
		t.Fatalf("%+v %v", d, err)
	}
}
