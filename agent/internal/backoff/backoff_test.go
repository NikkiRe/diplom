package backoff

import (
	"testing"
	"time"
)

func TestDelayGrowsWithCapAndJitter(t *testing.T) {
	p := Policy{Base: 500 * time.Millisecond, Max: 8 * time.Second}
	cases := []struct {
		n    int
		u    float64
		want time.Duration
	}{
		{0, 0, 250 * time.Millisecond}, {1, 0, 250 * time.Millisecond}, {1, 0.5, 375 * time.Millisecond},
		{2, 0, 500 * time.Millisecond}, {4, 0, 2 * time.Second}, {5, 0.999999, 8 * time.Second},
		{6, 0, 4 * time.Second}, {60, 0, 4 * time.Second},
	}
	for _, c := range cases {
		got := p.Delay(c.n, c.u)
		if c.u > 0.99 {
			if got <= 7900*time.Millisecond || got > 8*time.Second {
				t.Errorf("Delay(%d, %v) = %v, want just below 8s", c.n, c.u, got)
			}
			continue
		}
		if got != c.want {
			t.Errorf("Delay(%d, %v) = %v, want %v", c.n, c.u, got, c.want)
		}
	}
	if d := (Policy{Base: time.Second}).Delay(11, 0); d != 512*time.Second {
		t.Fatalf("uncapped delay %v", d)
	}
}

func TestUnitDeterministicAndSpread(t *testing.T) {
	seed := Seed("edgectx-run")
	if Unit(seed, 3) != Unit(Seed("edgectx-run"), 3) {
		t.Fatal("same seed and number must give the same value")
	}
	if Unit(seed, 3) == Unit(seed, 4) || Unit(seed, 3) == Unit(Seed("other"), 3) {
		t.Fatal("different inputs should differ")
	}
	var sum float64
	for n := uint64(0); n < 1000; n++ {
		u := Unit(seed, n)
		if u < 0 || u >= 1 {
			t.Fatalf("Unit out of [0,1): %v", u)
		}
		sum += u
	}
	if mean := sum / 1000; mean < 0.45 || mean > 0.55 {
		t.Fatalf("mean %v is far from 0.5", mean)
	}
}
