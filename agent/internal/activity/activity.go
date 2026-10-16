// Package activity — счётчик незавершённой работы агента.
//
// Каждый асинхронный кусок работы (задание планировщика, обработка уведомления
// MCP, действие правила) вызывает Begin при постановке и End по завершении.
// WaitIdle блокируется до нуля — так виртуальные часы дожидаются «затишья»
// перед тем, как сдвинуть время дальше.
package activity

import (
	"sync"
	"time"
)

// Tracker — счётчик активности. Нулевое значение готово к работе.
type Tracker struct {
	mu   sync.Mutex
	cond *sync.Cond
	n    int
}

func (t *Tracker) init() {
	if t.cond == nil {
		t.cond = sync.NewCond(&t.mu)
	}
}

// Begin отмечает начало работы.
func (t *Tracker) Begin() {
	t.mu.Lock()
	t.init()
	t.n++
	t.mu.Unlock()
}

// End отмечает завершение работы.
func (t *Tracker) End() {
	t.mu.Lock()
	t.init()
	t.n--
	if t.n < 0 {
		t.mu.Unlock()
		panic("activity: End without Begin")
	}
	if t.n == 0 {
		t.cond.Broadcast()
	}
	t.mu.Unlock()
}

// Active — текущее число незавершённых работ.
func (t *Tracker) Active() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.n
}

// WaitIdle ждёт, пока незавершённых работ не останется, но не дольше timeout
// (timeout <= 0 — без ограничения). Возвращает true, если затишье достигнуто.
func (t *Tracker) WaitIdle(timeout time.Duration) bool {
	t.mu.Lock()
	t.init()
	if timeout > 0 {
		// sync.Cond не умеет ждать с таймаутом: будим ожидающих по таймеру.
		expired := false
		tm := time.AfterFunc(timeout, func() {
			t.mu.Lock()
			expired = true
			t.cond.Broadcast()
			t.mu.Unlock()
		})
		defer tm.Stop()
		for t.n > 0 && !expired {
			t.cond.Wait()
		}
	} else {
		for t.n > 0 {
			t.cond.Wait()
		}
	}
	idle := t.n == 0
	t.mu.Unlock()
	return idle
}
