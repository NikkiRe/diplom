package clock

import (
	"sync/atomic"
	"testing"
	"time"
)

var epoch = time.Date(2026, 10, 6, 0, 0, 0, 0, time.UTC)

func TestVirtualOrderAndTies(t *testing.T) {
	v := NewVirtual(epoch, 7*time.Hour)
	var got []string
	v.AfterFunc(10*time.Second, func() { got = append(got, "b10") })
	v.AfterFunc(5*time.Second, func() { got = append(got, "a5") })
	v.AfterFunc(10*time.Second, func() { got = append(got, "c10") })
	v.AfterFunc(30*time.Second, func() { got = append(got, "d30") })

	n, err := v.AdvanceTo(epoch.Add(7*time.Hour+20*time.Second), nil)
	if err != nil || n != 3 {
		t.Fatalf("fired=%d err=%v", n, err)
	}
	want := []string{"a5", "b10", "c10"}
	if len(got) != 3 || got[0] != want[0] || got[1] != want[1] || got[2] != want[2] {
		t.Fatalf("order %v", got)
	}
	if s := Seconds(v); s != 7*3600+20 {
		t.Fatalf("seconds %v", s)
	}
	if v.Pending() != 1 {
		t.Fatalf("pending %d", v.Pending())
	}
}

func TestVirtualNowInsideCallback(t *testing.T) {
	v := NewVirtual(epoch, 0)
	var at float64
	v.AfterFunc(90*time.Second, func() { at = Seconds(v) })
	if _, err := v.AdvanceTo(epoch.Add(time.Hour), nil); err != nil {
		t.Fatal(err)
	}
	if at != 90 {
		t.Fatalf("callback saw t=%v, want 90", at)
	}
}

func TestVirtualNestedTimersAndSettle(t *testing.T) {
	v := NewVirtual(epoch, 0)
	var ticks []float64
	var settles int32
	var tick func()
	tick = func() {
		ticks = append(ticks, Seconds(v))
		v.AfterFunc(30*time.Minute, tick) // периодический таймер, как heartbeat
	}
	v.AfterFunc(30*time.Minute, tick)
	n, err := v.AdvanceTo(epoch.Add(2*time.Hour), func() { atomic.AddInt32(&settles, 1) })
	if err != nil {
		t.Fatal(err)
	}
	if n != 4 || len(ticks) != 4 || ticks[3] != 7200 {
		t.Fatalf("n=%d ticks=%v", n, ticks)
	}
	// settle: один раз в начале, после каждого таймера и один раз в конце.
	if settles != int32(n+2) {
		t.Fatalf("settles=%d", settles)
	}
}

func TestVirtualStop(t *testing.T) {
	v := NewVirtual(epoch, 0)
	fired := false
	tm := v.AfterFunc(time.Second, func() { fired = true })
	if !tm.Stop() {
		t.Fatal("first Stop must succeed")
	}
	if tm.Stop() {
		t.Fatal("second Stop must fail")
	}
	v.AdvanceTo(epoch.Add(time.Minute), nil)
	if fired {
		t.Fatal("stopped timer fired")
	}
	tm2 := v.AfterFunc(time.Second, func() {})
	v.AdvanceTo(epoch.Add(2*time.Minute), nil)
	if tm2.Stop() {
		t.Fatal("Stop after fire must return false")
	}
}

func TestVirtualBackwards(t *testing.T) {
	v := NewVirtual(epoch, time.Hour)
	if _, err := v.AdvanceTo(epoch, nil); err != ErrBackwards {
		t.Fatalf("err=%v", err)
	}
}

func TestVirtualZeroDelayFiresOnFlush(t *testing.T) {
	v := NewVirtual(epoch, 0)
	fired := false
	v.AfterFunc(0, func() { fired = true })
	if fired {
		t.Fatal("must not fire synchronously")
	}
	v.AdvanceTo(v.Now(), nil)
	if !fired {
		t.Fatal("zero-delay timer must fire on AdvanceTo(now)")
	}
}

func TestRealClock(t *testing.T) {
	r := NewReal(time.Time{})
	if r.Virtual() {
		t.Fatal("real clock reports virtual")
	}
	if s := Seconds(r); s < 0 || s > 86400 {
		t.Fatalf("seconds of day out of range: %v", s)
	}
	done := make(chan struct{})
	r.AfterFunc(5*time.Millisecond, func() { close(done) })
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("real timer did not fire")
	}
}

func TestAt(t *testing.T) {
	v := NewVirtual(epoch, 0)
	if !At(v, 3600.5).Equal(epoch.Add(3600*time.Second + 500*time.Millisecond)) {
		t.Fatal("At mismatch")
	}
}
