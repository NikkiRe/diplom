package server

import (
	"sort"
	"sync"
	"sync/atomic"

	"github.com/local/hwmcp/internal/hub"
)

// dispatcher отвязывает рассылку notifications/resources/updated от сэмплера, сторожевого таймера
// аренды и обработчика фронтов GPIO.
//
// Рассылка через SDK синхронна: запись в stdout (stdio) или в поток SSE блокируется, если клиент не
// читает (edgectx завис или остановлен), а контекст запись в pipe не прерывает. Поэтому hub отдаёт
// изменение сюда (enqueue никогда не блокируется), а отправляет одна горутина-рассыльщик.
//
// Очередь — на устройство: новые значения сворачиваются с ещё не отправленными (последнее значение
// свойства побеждает), кроме смены логического значения на противоположное — короткий импульс PIR
// 0→1→0 даёт отдельные уведомления, пока очередь устройства не длиннее maxPerDevice; дальше (клиент
// не читает) всё сворачивается в последнюю запись, и память ограничена числом устройств.
type dispatcher struct {
	mu      sync.Mutex
	pending map[string][]pendingUpdate // устройство -> неотправленные изменения
	order   []string                   // устройства в порядке первого неотправленного изменения
	signal  chan struct{}              // ёмкость 1: есть что отправлять
	stop    chan struct{}
	done    chan struct{}
	stopped bool
	send    func(device string, u pendingUpdate)

	enqueued, coalesced, sent atomic.Int64
}

// pendingUpdate — изменения устройства для одного уведомления: свойство -> значение.
type pendingUpdate struct {
	values map[string]any
	ts     float64
}

const maxPerDevice = 8

func newDispatcher(send func(string, pendingUpdate)) *dispatcher {
	d := &dispatcher{pending: map[string][]pendingUpdate{}, signal: make(chan struct{}, 1),
		stop: make(chan struct{}), done: make(chan struct{}), send: send}
	go d.loop()
	return d
}

// enqueue ставит изменение в очередь (не блокируется).
func (d *dispatcher) enqueue(u hub.Update) {
	d.mu.Lock()
	if d.stopped {
		d.mu.Unlock()
		return
	}
	d.enqueued.Add(1)
	q, had := d.pending[u.Device]
	if n := len(q); n > 0 && (n >= maxPerDevice || mergeable(q[n-1], u)) {
		last := q[n-1]
		for _, c := range u.Changes {
			last.values[c.Property] = c.Value
		}
		last.ts = u.TS
		q[n-1] = last
		d.coalesced.Add(1)
	} else {
		vals := make(map[string]any, len(u.Changes))
		for _, c := range u.Changes {
			vals[c.Property] = c.Value
		}
		q = append(q, pendingUpdate{values: vals, ts: u.TS})
	}
	d.pending[u.Device] = q
	if !had || len(q) == 0 {
		d.order = append(d.order, u.Device)
	}
	d.mu.Unlock()
	select {
	case d.signal <- struct{}{}:
	default:
	}
}

// mergeable — можно ли свернуть u в ещё не отправленную запись без потери перехода логического значения.
func mergeable(last pendingUpdate, u hub.Update) bool {
	for _, c := range u.Changes {
		old, ok := last.values[c.Property]
		if !ok {
			continue
		}
		ob, isB := old.(bool)
		nb, isNB := c.Value.(bool)
		if isB && isNB && ob != nb {
			return false
		}
	}
	return true
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
			if len(d.order) == 0 {
				d.mu.Unlock()
				break
			}
			batch, order := d.pending, d.order
			d.pending, d.order = map[string][]pendingUpdate{}, nil
			d.mu.Unlock()
			for _, dev := range order {
				for _, u := range batch[dev] {
					select {
					case <-d.stop:
						return
					default:
					}
					d.send(dev, u)
					d.sent.Add(1)
				}
			}
		}
	}
}

// close останавливает рассыльщика, не дожидаясь отправки, которая может висеть на зависшем клиенте.
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
	queued := 0
	for _, q := range d.pending {
		queued += len(q)
	}
	d.mu.Unlock()
	return map[string]any{"enqueued": d.enqueued.Load(), "coalesced": d.coalesced.Load(),
		"sent": d.sent.Load(), "queued": queued}
}

func sortedProps(values map[string]any) []string {
	out := make([]string, 0, len(values))
	for k := range values {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
