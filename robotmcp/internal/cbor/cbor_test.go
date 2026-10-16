package cbor

import (
	"bytes"
	"encoding/hex"
	"math"
	"reflect"
	"testing"
)

func TestRoundTrip(t *testing.T) {
	in := map[string]any{
		"op": "publish", "topic": "/scan",
		"msg": map[string]any{
			"angle_min": 0.0, "n": -5, "ok": true, "none": nil,
			"ranges": Float32Array{1.5, float32(math.Inf(1)), 0.25},
			"data":   []byte{0xff, 0xd8, 0x00},
			"list":   []any{"a", 1, 2.5},
		},
	}
	b, err := Encode(in)
	if err != nil {
		t.Fatal(err)
	}
	out, err := Decode(b)
	if err != nil {
		t.Fatal(err)
	}
	m := out.(map[string]any)["msg"].(map[string]any)
	r := m["ranges"].([]float64)
	if r[0] != 1.5 || !math.IsInf(r[1], 1) || r[2] != 0.25 {
		t.Fatalf("ranges %v", r)
	}
	if !bytes.Equal(m["data"].([]byte), []byte{0xff, 0xd8, 0}) {
		t.Fatalf("data %v", m["data"])
	}
	if m["n"].(float64) != -5 || m["ok"] != true || m["none"] != nil {
		t.Fatalf("scalars %v", m)
	}
	if !reflect.DeepEqual(m["list"], []any{"a", 1.0, 2.5}) {
		t.Fatalf("list %v", m["list"])
	}
}

// Векторы RFC 8949, приложение A.
func TestRFCVectors(t *testing.T) {
	cases := []struct {
		hex  string
		want any
	}{
		{"00", 0.0}, {"1903e8", 1000.0}, {"3863", -100.0}, {"f93c00", 1.0}, {"f97bff", 65504.0},
		{"fa47c35000", 100000.0}, {"fb3ff199999999999a", 1.1}, {"f4", false}, {"f6", nil},
		{"6449455446", "IETF"}, {"83010203", []any{1.0, 2.0, 3.0}},
		{"a26161016162820203", map[string]any{"a": 1.0, "b": []any{2.0, 3.0}}},
		{"7f657374726561646d696e67ff", "streaming"},
		{"9f018202039f0405ffff", []any{1.0, []any{2.0, 3.0}, []any{4.0, 5.0}}},
		{"bf61610161629f0203ffff", map[string]any{"a": 1.0, "b": []any{2.0, 3.0}}},
		{"c11a514b67b0", 1363896240.0}, // тег 1 (время) — отдаётся вложенное значение
	}
	for _, c := range cases {
		b, _ := hex.DecodeString(c.hex)
		got, err := Decode(b)
		if err != nil {
			t.Errorf("%s: %v", c.hex, err)
			continue
		}
		if !reflect.DeepEqual(got, c.want) {
			t.Errorf("%s: got %#v want %#v", c.hex, got, c.want)
		}
	}
}

func TestTypedArrays(t *testing.T) {
	// тег 86 (float64 LE) из двух элементов и тег 78 (int32 LE)
	b := []byte{0xd8, 86, 0x50}
	b = append(b, make([]byte, 16)...)
	putF64LE(b[3:], 2.5)
	putF64LE(b[11:], -1)
	got, err := Decode(b)
	if err != nil || !reflect.DeepEqual(got, []float64{2.5, -1}) {
		t.Fatalf("%v %v", got, err)
	}
	got, err = Decode([]byte{0xd8, 78, 0x44, 0xff, 0xff, 0xff, 0xff})
	if err != nil || !reflect.DeepEqual(got, []float64{-1}) {
		t.Fatalf("%v %v", got, err)
	}
	if _, err := Decode([]byte{0xd8, 85, 0x43, 1, 2, 3}); err == nil {
		t.Fatal("bad typed array length accepted")
	}
}

func putF64LE(b []byte, f float64) {
	u := math.Float64bits(f)
	for i := 0; i < 8; i++ {
		b[i] = byte(u >> (8 * i))
	}
}

func TestMalformed(t *testing.T) {
	for _, h := range []string{"", "18", "5a0000", "9a7fffffff", "ff", "1c", "a1"} {
		b, _ := hex.DecodeString(h)
		if _, err := Decode(b); err == nil {
			t.Errorf("%q: no error", h)
		}
	}
	if _, err := Decode([]byte{0x01, 0x02}); err == nil {
		t.Error("trailing bytes accepted")
	}
}

func FuzzDecode(f *testing.F) {
	f.Add([]byte{0xa1, 0x61, 0x61, 0x01})
	f.Fuzz(func(t *testing.T, b []byte) { _, _ = Decode(b) })
}
