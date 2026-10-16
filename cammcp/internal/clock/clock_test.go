package clock

import (
	"testing"
	"time"
)

func TestVirtual(t *testing.T) {
	v := NewVirtual(10)
	if v.Now() != 10 {
		t.Fatal(v.Now())
	}
	if err := v.Set(12.5); err != nil || v.Now() != 12.5 {
		t.Fatal(err, v.Now())
	}
	if err := v.Set(12); err == nil {
		t.Fatal("clock went back")
	}
}

func TestRealIsUnixAndMonotonic(t *testing.T) {
	r := NewReal()
	a := r.Now()
	if d := a - float64(time.Now().UnixNano())/1e9; d > 1 || d < -1 {
		t.Fatalf("not Unix seconds: %f", a)
	}
	if b := r.Now(); b < a {
		t.Fatal("not monotonic")
	}
}
