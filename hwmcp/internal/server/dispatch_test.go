package server

import (
	"sync"
	"testing"
	"time"

	"github.com/local/hwmcp/internal/hub"
)

func upd(dev, prop string, v any, ts float64) hub.Update {
	return hub.Update{Device: dev, Changes: []hub.Change{{Property: prop, Value: v, TS: ts}}, TS: ts}
}

// Клиент перестал читать (отправка висит): enqueue из сэмплера, таймеров и фронтов всё равно
// возвращается сразу; очередь ограничена (числа сворачиваются, переходы логического значения
// сохраняются до maxPerDevice), последнее значение не теряется; close не ждёт зависшую отправку.
func TestDispatcherNeverBlocks(t *testing.T) {
	release := make(chan struct{})
	stuck := make(chan struct{})
	var once sync.Once
	var mu sync.Mutex
	var sent []pendingUpdate
	var devs []string
	d := newDispatcher(func(dev string, u pendingUpdate) {
		once.Do(func() { close(stuck) })
		<-release
		mu.Lock()
		sent, devs = append(sent, u), append(devs, dev)
		mu.Unlock()
	})
	d.enqueue(upd("pir_hall", "state", true, 0))
	<-stuck // рассыльщик завис на первой отправке

	const n = 10000
	done := make(chan struct{})
	go func() {
		defer close(done)
		for i := 1; i <= n; i++ {
			d.enqueue(upd("pir_hall", "state", i%2 == 0, float64(i)))
			d.enqueue(upd("kitchen_bme280", "temperature", float64(i), float64(i)))
		}
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("enqueue blocked behind a stalled send")
	}
	st := d.stats()
	if st["queued"] != maxPerDevice+1 || st["enqueued"] != int64(2*n+1) {
		t.Fatalf("queue must stay bounded: %v", st)
	}

	close(release)
	deadline := time.Now().Add(5 * time.Second)
	for d.stats()["sent"] != int64(maxPerDevice+2) {
		if time.Now().After(deadline) {
			t.Fatalf("backlog not sent: %v", d.stats())
		}
		time.Sleep(5 * time.Millisecond)
	}
	mu.Lock()
	var pir []any
	var kitchen []pendingUpdate
	for i, u := range sent {
		switch devs[i] {
		case "pir_hall":
			pir = append(pir, u.values["state"])
		case "kitchen_bme280":
			kitchen = append(kitchen, u)
		}
	}
	mu.Unlock()
	if len(pir) != maxPerDevice+1 || pir[len(pir)-1] != (n%2 == 0) {
		t.Fatalf("pir sends %v: transitions up to maxPerDevice, last value must win", pir)
	}
	for i := 1; i < len(pir); i++ {
		if pir[i] == pir[i-1] && i < len(pir)-1 {
			t.Fatalf("pir transitions lost: %v", pir)
		}
	}
	if len(kitchen) != 1 || kitchen[0].values["temperature"] != float64(n) || kitchen[0].ts != float64(n) {
		t.Fatalf("numeric changes must coalesce to the last value: %+v", kitchen)
	}

	// close не ждёт отправку, висящую на зависшем клиенте; после close enqueue ничего не делает
	hang := make(chan struct{})
	defer close(hang)
	entered := make(chan struct{})
	d2 := newDispatcher(func(string, pendingUpdate) { close(entered); <-hang })
	d2.enqueue(upd("relay_fan", "state", true, 0))
	<-entered
	closed := make(chan struct{})
	go func() { d2.close(); d2.close(); close(closed) }()
	select {
	case <-closed:
	case <-time.After(5 * time.Second):
		t.Fatal("close waited for a stalled send")
	}
	d2.enqueue(upd("relay_fan", "state", false, 1))
	if q := d2.stats()["queued"]; q != 0 {
		t.Fatalf("enqueue after close: queued %v", q)
	}
	d.close()
}
