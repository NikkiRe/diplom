package server

import (
	"sync"
	"sync/atomic"

	"github.com/local/robotmcp/internal/robot"
)

// dispatcher отвязывает рассылку уведомлений от ядра: запись в stdout/SSE блокируется, если клиент не
// читает, а ядро вызывает Notify под своим мьютексом. Очередь ограничена: при maxQueued неотправленных
// событиях новое сворачивается с последним (состояние в событии полное, теряются только промежуточные
// состояния; списки changed объединяются).
type dispatcher struct {
	mu      sync.Mutex
	queue   []robot.Event
	signal  chan struct{}
	stop    chan struct{}
	done    chan struct{}
	stopped bool
	send    func(robot.Event)

	enqueued, coalesced, sent atomic.Int64
}

const maxQueued = 64

func newDispatcher(send func(robot.Event)) *dispatcher {
	d := &dispatcher{signal: make(chan struct{}, 1), stop: make(chan struct{}), done: make(chan struct{}), send: send}
	go d.loop()
	return d
}

func (d *dispatcher) enqueue(e robot.Event) {
	d.mu.Lock()
	if d.stopped {
		d.mu.Unlock()
		return
	}
	d.enqueued.Add(1)
	if n := len(d.queue); n >= maxQueued {
		last := &d.queue[n-1]
		seen := map[string]bool{}
		for _, c := range last.Changed {
			seen[c] = true
		}
		for _, c := range e.Changed {
			if !seen[c] {
				last.Changed = append(last.Changed, c)
			}
		}
		last.State, last.Kind, last.Detail = e.State, e.Kind, e.Detail
		d.coalesced.Add(1)
	} else {
		d.queue = append(d.queue, e)
	}
	d.mu.Unlock()
	select {
	case d.signal <- struct{}{}:
	default:
	}
}

func (d *dispatcher) loop() {
	defer close(d.done)
	for {
		select {
		case <-d.stop:
			return
		case <-d.signal:
		}
		for {
			d.mu.Lock()
			if len(d.queue) == 0 {
				d.mu.Unlock()
				break
			}
			e := d.queue[0]
			d.queue = d.queue[1:]
			d.mu.Unlock()
			select {
			case <-d.stop:
				return
			default:
			}
			d.send(e)
			d.sent.Add(1)
		}
	}
}

func (d *dispatcher) close() {
	d.mu.Lock()
	if d.stopped {
		d.mu.Unlock()
		return
	}
	d.stopped = true
	d.mu.Unlock()
	close(d.stop)
}

func (d *dispatcher) stats() map[string]any {
	d.mu.Lock()
	q := len(d.queue)
	d.mu.Unlock()
	return map[string]any{"enqueued": d.enqueued.Load(), "coalesced": d.coalesced.Load(), "sent": d.sent.Load(),
		"queued": q}
}
