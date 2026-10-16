// Package clock — единственный источник времени агента (спецификация §0.2).
//
// Все таймеры логики агента (heartbeat, дебаунс, cooldown, окно коалесценции,
// «конец эпизода», задержки правил, консолидация профиля) создаются только через
// Clock.AfterFunc, а «текущее время» берётся только из Clock.Now. Это позволяет
// проигрывать рабочий день на виртуальных часах за секунды (режим T0).
//
// Время дня измеряется в секундах от эпохи часов (Epoch) — полуночи дня прогона;
// это та же шкала, что поле t в файле дня (спецификация §2).
//
// Сетевые таймауты и паузы между повторами HTTP-запросов — не логика агента, а
// свойство реальной сети, поэтому они идут по настенным часам (см. пакет llm).
package clock

import (
	"container/heap"
	"errors"
	"sync"
	"time"
)

// Timer — отменяемый таймер.
type Timer interface {
	// Stop отменяет таймер; возвращает false, если он уже сработал или отменён.
	Stop() bool
}

// Clock — источник времени.
type Clock interface {
	// Now — текущее время.
	Now() time.Time
	// Epoch — начало шкалы «секунд дня» (полночь дня прогона).
	Epoch() time.Time
	// AfterFunc вызывает f через d. Для виртуальных часов f исполняется во время
	// продвижения часов (Virtual.AdvanceTo), синхронно в продвигающей горутине.
	AfterFunc(d time.Duration, f func()) Timer
	// Virtual сообщает, виртуальные ли это часы.
	Virtual() bool
}

// Seconds — текущее время в секундах от эпохи часов (поле ts_clock трассы).
func Seconds(c Clock) float64 {
	return c.Now().Sub(c.Epoch()).Seconds()
}

// At переводит «секунды дня» в момент времени.
func At(c Clock, sec float64) time.Time {
	return c.Epoch().Add(time.Duration(sec * float64(time.Second)))
}

// Midnight — полночь дня t в часовом поясе t.
func Midnight(t time.Time) time.Time {
	y, m, d := t.Date()
	return time.Date(y, m, d, 0, 0, 0, 0, t.Location())
}

// ---------------------------------------------------------------- реальные часы

// Real — настенные часы.
type Real struct {
	epoch time.Time
}

// NewReal создаёт реальные часы. Нулевая эпоха означает полночь текущего дня
// (по местному времени), чтобы ts_clock совпадал с «секундами дня».
func NewReal(epoch time.Time) *Real {
	if epoch.IsZero() {
		epoch = Midnight(time.Now())
	}
	return &Real{epoch: epoch}
}

func (r *Real) Now() time.Time   { return time.Now() }
func (r *Real) Epoch() time.Time { return r.epoch }
func (r *Real) Virtual() bool    { return false }

func (r *Real) AfterFunc(d time.Duration, f func()) Timer {
	return realTimer{time.AfterFunc(d, f)}
}

type realTimer struct{ t *time.Timer }

func (t realTimer) Stop() bool { return t.t.Stop() }

// ------------------------------------------------------------ виртуальные часы

// ErrBackwards — попытка повернуть виртуальные часы назад.
var ErrBackwards = errors.New("clock: cannot move virtual time backwards")

// Virtual — виртуальные часы с дискретно-событийным продвижением.
//
// Время стоит, пока его не продвинут вызовом AdvanceTo. Таймеры срабатывают в
// порядке своих сроков (при равенстве — в порядке создания), а после каждого
// срабатывания вызывается settle — функция ожидания «затишья» агента (все
// вызванные таймером обращения к LLM и инструментам завершены). Так результат
// прогона не зависит от скорости машины: обращение к LLM занимает ноль
// виртуального времени.
type Virtual struct {
	advMu sync.Mutex // сериализует AdvanceTo

	mu    sync.Mutex
	epoch time.Time
	now   time.Time
	seq   uint64
	q     timerHeap
}

// NewVirtual создаёт виртуальные часы с эпохой epoch и начальным временем
// epoch+start.
func NewVirtual(epoch time.Time, start time.Duration) *Virtual {
	return &Virtual{epoch: epoch, now: epoch.Add(start)}
}

func (v *Virtual) Now() time.Time {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.now
}

func (v *Virtual) Epoch() time.Time { return v.epoch }
func (v *Virtual) Virtual() bool    { return true }

// AfterFunc регистрирует таймер. Таймер с d <= 0 срабатывает при ближайшем
// вызове AdvanceTo (в том числе AdvanceTo(Now())).
func (v *Virtual) AfterFunc(d time.Duration, f func()) Timer {
	v.mu.Lock()
	defer v.mu.Unlock()
	if d < 0 {
		d = 0
	}
	v.seq++
	t := &vtimer{v: v, when: v.now.Add(d), seq: v.seq, f: f, index: -1}
	heap.Push(&v.q, t)
	return t
}

// Pending — число ожидающих таймеров.
func (v *Virtual) Pending() int {
	v.mu.Lock()
	defer v.mu.Unlock()
	return len(v.q)
}

// NextDeadline — срок ближайшего таймера (ok=false, если таймеров нет).
func (v *Virtual) NextDeadline() (time.Time, bool) {
	v.mu.Lock()
	defer v.mu.Unlock()
	if len(v.q) == 0 {
		return time.Time{}, false
	}
	return v.q[0].when, true
}

// AdvanceTo продвигает время до to, по очереди исполняя все таймеры со сроком
// <= to. После каждого таймера и в конце вызывается settle (если не nil).
// Возвращает число сработавших таймеров.
func (v *Virtual) AdvanceTo(to time.Time, settle func()) (int, error) {
	v.advMu.Lock()
	defer v.advMu.Unlock()
	if settle == nil {
		settle = func() {}
	}
	v.mu.Lock()
	if to.Before(v.now) {
		v.mu.Unlock()
		return 0, ErrBackwards
	}
	v.mu.Unlock()

	settle() // сначала доработать то, что пришло до продвижения
	fired := 0
	for {
		v.mu.Lock()
		if len(v.q) == 0 || v.q[0].when.After(to) {
			v.now = to
			v.mu.Unlock()
			break
		}
		t := heap.Pop(&v.q).(*vtimer)
		if t.when.After(v.now) {
			v.now = t.when
		}
		t.fired = true
		f := t.f
		v.mu.Unlock()

		f()
		fired++
		settle()
	}
	settle()
	return fired, nil
}

type vtimer struct {
	v     *Virtual
	when  time.Time
	seq   uint64
	f     func()
	index int
	fired bool
}

func (t *vtimer) Stop() bool {
	t.v.mu.Lock()
	defer t.v.mu.Unlock()
	if t.fired || t.index < 0 {
		return false
	}
	heap.Remove(&t.v.q, t.index)
	return true
}

type timerHeap []*vtimer

func (h timerHeap) Len() int { return len(h) }
func (h timerHeap) Less(i, j int) bool {
	if h[i].when.Equal(h[j].when) {
		return h[i].seq < h[j].seq
	}
	return h[i].when.Before(h[j].when)
}
func (h timerHeap) Swap(i, j int) {
	h[i], h[j] = h[j], h[i]
	h[i].index = i
	h[j].index = j
}
func (h *timerHeap) Push(x any) {
	t := x.(*vtimer)
	t.index = len(*h)
	*h = append(*h, t)
}
func (h *timerHeap) Pop() any {
	old := *h
	n := len(old)
	t := old[n-1]
	old[n-1] = nil
	t.index = -1
	*h = old[:n-1]
	return t
}
