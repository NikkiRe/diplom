package robot

import (
	"math"
	"strings"
	"testing"

	"github.com/local/robotmcp/configs"
)

func TestConfig(t *testing.T) {
	c, err := ParseConfig(configs.TurtleBot3World)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(c.IDs(), ",") != "dock,door,corridor,window,shelf,desk,pass_ne,pass_sw" {
		t.Fatal(c.IDs())
	}
	p, d := c.Nearest(1.7, 0.45)
	if p.ID != "door" || d > 0.12 {
		t.Fatal(p, d)
	}
	if eta := c.ETA(3.93); math.Abs(eta-(11.1+2.0*1.2*3.93)) > 1e-9 {
		t.Fatal(eta)
	}
	if c.ETA(0.1) != 0 {
		t.Fatal("eta at place")
	}
	bad := []string{
		"places: []",
		"places: [{id: Dock, xy: [0, 0]}]",
		"places: [{id: a, xy: [0, 0]}, {id: a, xy: [5, 5]}]",
		"places: [{id: a, xy: [0]}]",
		"places: [{id: a, xy: [0, 0]}, {id: b, xy: [0.5, 0]}]",
		"places: [{id: between, xy: [0, 0]}]",
		"places: [{id: unknown, xy: [0, 0]}]",
	}
	for _, b := range bad {
		if _, err := ParseConfig([]byte(b)); err == nil {
			t.Errorf("accepted %q", b)
		}
	}
}

// Скан с известной геометрией: лучи 0..359° (0 — вперёд), препятствие 0,5 м спереди (±5°), 1 м слева (90°),
// остальное — без отражения (inf), один луч NaN, один ближе range_min.
func synthScan() map[string]any {
	r := make([]any, 360)
	for i := range r {
		switch {
		case i <= 5 || i >= 355:
			r[i] = 0.5
		case i >= 85 && i <= 95:
			r[i] = 1.0
		case i == 200:
			r[i] = math.NaN()
		case i == 210:
			r[i] = 0.05
		default:
			r[i] = nil
		}
	}
	return map[string]any{"angle_min": 0.0, "angle_increment": math.Pi / 180, "range_min": 0.12, "range_max": 3.5, "ranges": r}
}

func TestScanSummary(t *testing.T) {
	s, err := ParseScan(synthScan(), 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(s.Pts) != 22 {
		t.Fatal(len(s.Pts))
	}
	f := s.FreeM()
	if f["front"] != 0.5 || f["left"] != 1.0 || f["right"] != ">3.5" || f["back"] != ">3.5" {
		t.Fatal(f)
	}
	n := s.Nearest()
	if n["m"] != 0.5 || n["deg"] != -5.0 {
		t.Fatal(n)
	}
	iv := s.InView(62.2).(map[string]any)
	// 11 лучей в поле зрения из round(62.2/1) = 62
	if iv["nearest_m"] != 0.5 || iv["side"] != "center" || iv["occupied_pct"] != 18.0 {
		t.Fatal(iv)
	}
	if s.Front() != 0.5 {
		t.Fatal(s.Front())
	}
	empty, _ := ParseScan(map[string]any{"ranges": []any{nil, nil}, "angle_increment": 0.1, "range_max": 3.5}, 0)
	if empty.InView(62.2) != "clear >3.5 m" || empty.Nearest() != nil {
		t.Fatal(empty.InView(62.2))
	}
	if _, err := ParseScan(map[string]any{"ranges": "x"}, 0); err == nil {
		t.Fatal("bad ranges accepted")
	}
}

func TestQuantize(t *testing.T) {
	for in, want := range map[float64]any{100: 100, 77.4: 75, 77.6: 80, 2: 0, -1: nil} {
		if got := q5(in); got != want {
			t.Errorf("q5(%v) = %v, want %v", in, got, want)
		}
	}
}

func TestRawJSON(t *testing.T) {
	v := RawJSON(map[string]any{"a": []float64{1, math.Inf(1)}, "b": []byte{1, 2}, "c": math.NaN()}).(map[string]any)
	if v["a"].([]any)[1] != nil || v["b"] != 2 || v["c"] != nil {
		t.Fatal(v)
	}
}

func TestYaw(t *testing.T) {
	if y := yawOf(map[string]any{"z": math.Sin(0.5), "w": math.Cos(0.5)}); math.Abs(y-1) > 1e-9 {
		t.Fatal(y)
	}
}

func TestStub(t *testing.T) {
	s := NewStub()
	s.Set("door", []Object{{Label: "person"}, {Label: "box"}})
	if o := s.Detect("door", nil); len(o) != 2 || o[0].Label != "box" {
		t.Fatal(o)
	}
	s.Set("door", nil)
	if len(s.Detect("door", nil)) != 0 {
		t.Fatal("not cleared")
	}
}
