package activity

import (
	"testing"
	"time"
)

func TestWaitIdle(t *testing.T) {
	var tr Tracker
	if !tr.WaitIdle(time.Millisecond) {
		t.Fatal("empty tracker must be idle")
	}
	tr.Begin()
	tr.Begin()
	go func() {
		time.Sleep(10 * time.Millisecond)
		tr.End()
		tr.End()
	}()
	if !tr.WaitIdle(2 * time.Second) {
		t.Fatal("must become idle")
	}
	if tr.Active() != 0 {
		t.Fatal("active != 0")
	}
}

func TestWaitIdleTimeout(t *testing.T) {
	var tr Tracker
	tr.Begin()
	start := time.Now()
	if tr.WaitIdle(20 * time.Millisecond) {
		t.Fatal("must time out")
	}
	if time.Since(start) < 15*time.Millisecond {
		t.Fatal("returned too early")
	}
	tr.End()
}

func TestEndWithoutBeginPanics(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("expected panic")
		}
	}()
	var tr Tracker
	tr.End()
}
