package clock

import (
	"testing"
	"time"
)

func TestVirtualMonotonic(t *testing.T) {
	v := NewVirtual(10)
	if v.Now() != 10 {
		t.Fatal(v.Now())
	}
	if err := v.Set(12.5); err != nil || v.Now() != 12.5 {
		t.Fatal(err, v.Now())
	}
	if err := v.Set(12.5); err != nil {
		t.Fatal("same time must be allowed")
	}
	if err := v.Set(3); err == nil || v.Now() != 12.5 {
		t.Fatal("going back must fail and keep the time")
	}
}

func TestRealIsUnixSecondsAndMonotonic(t *testing.T) {
	r := NewReal()
	a := r.Now()
	wall := float64(time.Now().UnixNano()) / 1e9
	if a < wall-1 || a > wall+1 {
		t.Fatalf("real clock %f far from wall %f", a, wall)
	}
	time.Sleep(2 * time.Millisecond)
	if b := r.Now(); b <= a {
		t.Fatalf("not monotonic: %f <= %f", b, a)
	}
}
