package sched

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/local/edgectx/internal/activity"
	"github.com/local/edgectx/internal/clock"
	"github.com/local/edgectx/internal/config"
)

var epoch = time.Date(2026, 10, 6, 0, 0, 0, 0, time.UTC)

type journal struct {
	mu  sync.Mutex
	log []string
}

func (j *journal) add(s string) {
	j.mu.Lock()
	j.log = append(j.log, s)
	j.mu.Unlock()
}

func (j *journal) String() string {
	j.mu.Lock()
	defer j.mu.Unlock()
	return strings.Join(j.log, " ")
}

func job(j *journal, id string, p Priority, block <-chan struct{}) *Job {
	return &Job{ID: id, Priority: p,
		Run: func(ctx context.Context) {
			if block != nil {
				<-block
			}
			j.add("run:" + id)
		},
		Reject: func(reason string) { j.add("rej:" + id + ":" + reason) }}
}

func TestPriorityOrder(t *testing.T) {
	tr := &activity.Tracker{}
	s := New(Options{MaxInFlight: 1, Tracker: tr, Clock: clock.NewVirtual(epoch, 0)})
	var j journal
	gate := make(chan struct{})
	s.Submit(job(&j, "first", Background, gate)) // занимает исполнителя
	s.Start(context.Background())
	for s.Running() == 0 {
		time.Sleep(time.Millisecond)
	}
	s.Submit(job(&j, "bg1", Background, nil))
	s.Submit(job(&j, "safe", Safety, nil))
	s.Submit(job(&j, "bg2", Background, nil))
	s.Submit(job(&j, "user", User, nil))
	close(gate)
	if !tr.WaitIdle(2 * time.Second) {
		t.Fatal("not idle")
	}
	want := "run:first run:user run:safe run:bg1 run:bg2"
	if j.String() != want {
		t.Fatalf("order: %s", j.String())
	}
	s.Stop()
}

func TestMaxInFlight(t *testing.T) {
	tr := &activity.Tracker{}
	s := New(Options{MaxInFlight: 2, Tracker: tr})
	var cur, peak int32
	for i := 0; i < 8; i++ {
		s.Submit(&Job{Priority: Background, Run: func(ctx context.Context) {
			n := atomic.AddInt32(&cur, 1)
			for {
				p := atomic.LoadInt32(&peak)
				if n <= p || atomic.CompareAndSwapInt32(&peak, p, n) {
					break
				}
			}
			time.Sleep(5 * time.Millisecond)
			atomic.AddInt32(&cur, -1)
		}})
	}
	s.Start(context.Background())
	tr.WaitIdle(5 * time.Second)
	if peak != 2 {
		t.Fatalf("peak in flight %d, want 2", peak)
	}
	s.Stop()
}

func TestBudgetDegradation(t *testing.T) {
	v := clock.NewVirtual(epoch, 8*time.Hour)
	tr := &activity.Tracker{}
	s := New(Options{MaxInFlight: 1, Tracker: tr, Clock: v,
		Budget: config.Budget{DailyCalls: 10, DailyInputTokens: 1000, DegradeAt: 0.8}})
	s.Start(context.Background())
	var j journal
	run := func(id string, p Priority) {
		s.Submit(job(&j, id, p, nil))
		tr.WaitIdle(2 * time.Second)
	}
	s.Charge(7, 100)
	run("bg-normal", Background)
	if s.Level() != Normal {
		t.Fatal("level must be normal at 70%")
	}
	s.Charge(0, 750) // токены: 85 % — деградация
	run("bg-degraded", Background)
	run("safe-degraded", Safety)
	s.Charge(3, 0) // вызовы: 100 % — исчерпан
	run("safe-exhausted", Safety)
	run("user-exhausted", User)
	want := "run:bg-normal rej:bg-degraded:budget_degraded run:safe-degraded rej:safe-exhausted:budget_exhausted run:user-exhausted"
	if j.String() != want {
		t.Fatalf("got  %s\nwant %s", j.String(), want)
	}
	u := s.Usage()
	if u.Level != "exhausted" || u.Calls != 10 || u.InputTokens != 850 {
		t.Fatalf("usage %+v", u)
	}
	// Новые сутки по часам агента — лимиты сбрасываются.
	v.AdvanceTo(epoch.Add(24*time.Hour+time.Minute), nil)
	if s.Level() != Normal || s.Usage().Calls != 0 || s.Usage().Day != 1 {
		t.Fatalf("day reset failed: %+v", s.Usage())
	}
	s.Stop()
}

func TestUnlimitedBudget(t *testing.T) {
	s := New(Options{})
	s.Charge(1e6, 1e9)
	if s.Level() != Normal {
		t.Fatal("zero limits mean unlimited")
	}
}

func TestQueueOverflow(t *testing.T) {
	tr := &activity.Tracker{}
	s := New(Options{MaxInFlight: 1, QueueMax: 2, Tracker: tr})
	var j journal
	s.Submit(job(&j, "bg1", Background, nil))
	s.Submit(job(&j, "bg2", Background, nil))
	s.Submit(job(&j, "bg3", Background, nil)) // не лучше очереди — отклоняется само
	s.Submit(job(&j, "user", User, nil))      // вытесняет самое старое фоновое
	s.Start(context.Background())
	tr.WaitIdle(2 * time.Second)
	want := "rej:bg3:queue_full rej:bg1:queue_full run:user run:bg2"
	if j.String() != want {
		t.Fatalf("got %s", j.String())
	}
	s.Stop()
}

func TestStopRejectsPending(t *testing.T) {
	tr := &activity.Tracker{}
	s := New(Options{MaxInFlight: 1, Tracker: tr})
	var j journal
	for i := 0; i < 3; i++ {
		s.Submit(job(&j, fmt.Sprint(i), Background, nil))
	}
	s.Stop()
	s.Submit(job(&j, "late", User, nil))
	if j.String() != "rej:0:shutdown rej:1:shutdown rej:2:shutdown rej:late:shutdown" {
		t.Fatalf("got %s", j.String())
	}
	if tr.Active() != 0 {
		t.Fatalf("tracker active %d", tr.Active())
	}
}
