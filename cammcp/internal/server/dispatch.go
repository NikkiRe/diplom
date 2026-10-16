package server

import (
	"slices"
	"sync"

	"github.com/local/cammcp/internal/camera"
)

// dispatcher отвязывает рассылку уведомлений от обработки кадров: запись в stdout или поток SSE
// блокируется, если клиент не читает, а кадры должны обрабатываться дальше. Очередь ограничена
// maxQueued; при переполнении новое событие сворачивается с последним (состояние в событии полное —
// теряются только промежуточные переходы, списки изменившихся свойств объединяются).
type dispatcher struct {
	mu      sync.Mutex
	queue   []camera.Event
	signal  chan struct{}
	stop    chan struct{}
	stopped bool
	send    func(camera.Event)

	enqueued, coalesced, sent int64
}

const maxQueued = 64

func newDispatcher(send func(camera.Event)) *dispatcher {
	d := &dispatcher{signal: make(chan struct{}, 1), stop: make(chan struct{}), send: send}
	go d.loop()
	return d
}

func (d *dispatcher) enqueue(e camera.Event) {
	d.mu.Lock()
	if d.stopped {
		d.mu.Unlock()
		return
	}
	d.enqueued++
	if n := len(d.queue); n >= maxQueued {
		last := &d.queue[n-1]
		for _, c := range e.Changed {
			if !slices.Contains(last.Changed, c) {
				last.Changed = append(last.Changed, c)
			}
		}
		last.Kind, last.T, last.State, last.Episode = e.Kind, e.T, e.State, e.Episode
		d.coalesced++
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
	for {
		select {
		case <-d.stop:
			return
		case <-d.signal:
		}
		for {
			d.mu.Lock()
			if len(d.queue) == 0 || d.stopped {
				d.mu.Unlock()
				break
			}
			e := d.queue[0]
			d.queue = d.queue[1:]
			d.mu.Unlock()
			d.send(e)
			d.mu.Lock()
			d.sent++
			d.mu.Unlock()
		}
	}
}

// close останавливает рассылку, не дожидаясь отправки, которая может висеть на зависшем клиенте.
func (d *dispatcher) close() {
	d.mu.Lock()
	defer d.mu.Unlock()
	if !d.stopped {
		d.stopped = true
		close(d.stop)
	}
}

func (d *dispatcher) stats() map[string]any {
	d.mu.Lock()
	defer d.mu.Unlock()
	return map[string]any{"enqueued": d.enqueued, "coalesced": d.coalesced, "sent": d.sent, "queued": len(d.queue)}
}
