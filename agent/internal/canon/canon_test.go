package canon

import (
	"encoding/json"
	"math"
	"testing"
)

func TestMarshalSortedCompactUTF8(t *testing.T) {
	v := map[string]any{
		"b":     1,
		"a":     []any{true, nil, "x<y&z"},
		"текст": "привет\n",
		"num":   json.Number("1.50"),
		"f":     2.0,
	}
	got, err := Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"a":[true,null,"x<y&z"],"b":1,"f":2,"num":1.5,"текст":"привет\n"}`
	if string(got) != want {
		t.Fatalf("got  %s\nwant %s", got, want)
	}
}

func TestMarshalStructAndRaw(t *testing.T) {
	type inner struct {
		Z int    `json:"z"`
		A string `json:"a,omitempty"`
	}
	type outer struct {
		Name  string          `json:"name"`
		Inner inner           `json:"inner"`
		Raw   json.RawMessage `json:"raw"`
	}
	got := String(outer{Name: "дом", Inner: inner{Z: 3}, Raw: json.RawMessage(`{"y": 2, "x": [1, 2.0]}`)})
	want := `{"inner":{"z":3},"name":"дом","raw":{"x":[1,2],"y":2}}`
	if got != want {
		t.Fatalf("got  %s\nwant %s", got, want)
	}
}

func TestMarshalDeterministic(t *testing.T) {
	m := map[string]any{}
	for _, k := range []string{"k9", "k1", "k5", "k3", "k7", "k2"} {
		m[k] = k
	}
	first := String(m)
	for i := 0; i < 50; i++ {
		if String(m) != first {
			t.Fatal("non-deterministic output")
		}
	}
}

func TestControlCharsEscaped(t *testing.T) {
	got := String("a\x01b\tc\"d\\")
	want := `"a\u0001b\tc\"d\\"`
	if got != want {
		t.Fatalf("got %s want %s", got, want)
	}
	// U+2028 не экранируется (encoding/json экранирует его как  ).
	if got := String(" "); got != "\" \"" {
		t.Fatalf("U+2028 escaped: %q", got)
	}
}

func TestNonFinite(t *testing.T) {
	if _, err := Marshal(math.NaN()); err == nil {
		t.Fatal("NaN must fail")
	}
}

func TestFormatFloat(t *testing.T) {
	a, b := 0.1, 0.2 // не константы: сумма вычисляется в float64
	cases := []struct {
		in   float64
		want string
	}{{0, "0"}, {math.Copysign(0, -1), "0"}, {1, "1"}, {22.5, "22.5"}, {a + b, "0.30000000000000004"}, {1e21, "1e+21"}, {101325, "101325"}}
	for _, c := range cases {
		got, err := FormatFloat(c.in)
		if err != nil || got != c.want {
			t.Errorf("FormatFloat(%v) = %q, %v; want %q", c.in, got, err, c.want)
		}
	}
}

func TestRound(t *testing.T) {
	cases := []struct{ x, step, want float64 }{
		{22.5134, 0.1, 22.5},
		{22.55, 0.1, 22.6},
		{0.30000000000000004, 0.1, 0.3},
		{41.23, 1, 41},
		{3.14159, 0.25, 3.25},
		{7.123, 0, 7.123},
		{-1.26, 0.05, -1.25},
	}
	for _, c := range cases {
		if got := Round(c.x, c.step); got != c.want {
			t.Errorf("Round(%v, %v) = %v; want %v", c.x, c.step, got, c.want)
		}
	}
	if s := String(Round(0.30000000000000004, 0.1)); s != "0.3" {
		t.Errorf("rounded value prints as %s", s)
	}
}
