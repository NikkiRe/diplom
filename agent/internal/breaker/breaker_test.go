package breaker

import (
	"testing"
	"time"

	"github.com/local/edgectx/internal/backoff"
)

var interval = backoff.Policy{Base: 30 * time.Second, Max: 300 * time.Second}

func TestOpensAfterThresholdAndCloses(t *testing.T) {
	b := New(2, interval, 1)
	if opened, next := b.Failure(0); opened || !b.Allow() || !b.Waiting() || next < 15*time.Second || next >= 30*time.Second {
		t.Fatalf("first failure: opened=%v next=%v", opened, next)
	}
	if opened, next := b.Failure(0); !opened || b.Allow() || b.State() != "open" || next < 30*time.Second || next >= 60*time.Second {
		t.Fatalf("second failure must open: opened=%v next=%v", opened, next)
	}
	if opened, _ := b.Failure(0); opened || b.Failures() != 3 {
		t.Fatal("an open breaker reports opening only once")
	}
	for i := 0; i < 10; i++ {
		if _, next := b.Failure(0); next > 300*time.Second {
			t.Fatalf("interval above the cap: %v", next)
		}
	}
	if !b.Success() || b.State() != "closed" || b.Failures() != 0 || b.Waiting() {
		t.Fatal("success must close and reset")
	}
	if b.Success() {
		t.Fatal("a closed breaker was not open")
	}
}

// Удачная проба полуоткрывает предохранитель, но не обнуляет счётчик: сбой
// хода после неё снова размыкает его с паузой больше прежней.
func TestProbeHalfOpensWithoutReset(t *testing.T) {
	b := New(1, interval, 3)
	b.Probed()
	if b.State() != "closed" {
		t.Fatal("a probe without a failure changes nothing")
	}
	var prev time.Duration
	for i := 1; i <= 6; i++ {
		opened, next := b.Failure(0)
		if opened != (i == 1) || b.Allow() {
			t.Fatalf("failure %d: opened=%v allow=%v", i, opened, b.Allow())
		}
		if i > 1 && next < prev/2 {
			t.Fatalf("interval must keep growing to the cap: %v after %v", next, prev)
		}
		prev = next
		b.Probed()
		if !b.Allow() || b.Waiting() || b.State() != "half_open" || b.Failures() != i {
			t.Fatalf("probe after failure %d: allow=%v waiting=%v state=%s", i, b.Allow(), b.Waiting(), b.State())
		}
	}
	if prev < 150*time.Second {
		t.Fatalf("after six failures the interval must reach the cap range: %v", prev)
	}
	if !b.Success() || b.State() != "closed" {
		t.Fatal("only a model response closes the breaker")
	}
}

func TestRetryAfterIsLowerBoundAndJitterDeterministic(t *testing.T) {
	b := New(1, interval, 42)
	if _, next := b.Failure(10 * time.Minute); next != 10*time.Minute {
		t.Fatalf("Retry-After must bound the interval: %v", next)
	}
	run := func() []time.Duration {
		b := New(1, interval, 42)
		var out []time.Duration
		for i := 0; i < 5; i++ {
			_, next := b.Failure(0)
			out = append(out, next)
		}
		return out
	}
	a, c := run(), run()
	for i := range a {
		if a[i] != c[i] {
			t.Fatalf("intervals differ for the same seed: %v vs %v", a, c)
		}
	}
	if New(0, interval, 1).threshold != 1 {
		t.Fatal("threshold below 1 must become 1")
	}
}
