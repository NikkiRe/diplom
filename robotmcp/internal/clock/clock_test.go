package clock

import "testing"

func TestVirtual(t *testing.T) {
	v := NewVirtual(10)
	if v.Now() != 10 {
		t.Fatal(v.Now())
	}
	if err := v.Set(12.5); err != nil || v.Now() != 12.5 {
		t.Fatal(err, v.Now())
	}
	if err := v.Set(1); err == nil {
		t.Fatal("clock went back")
	}
}

func TestRealMonotonic(t *testing.T) {
	r := NewReal()
	a := r.Now()
	b := r.Now()
	if b < a || a < 1e9 {
		t.Fatal(a, b)
	}
}
