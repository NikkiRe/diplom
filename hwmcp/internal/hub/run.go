package hub

import (
	"context"
	"math"
	"time"
)

// Циклы реального времени. Оба спят до ближайшего срока (time.Timer), а не опрашивают по тикеру:
// без аренд и импульсов таймеры не просыпаются вовсе, а сэмплер просыпается ровно в сроки опроса
// (один раз на совпадающие сроки нескольких свойств). Это и экономия CPU в простое, и точность:
// аренда истекает в свой срок, а не на следующем тике.
//
// Время сна считается по часам hw-mcp (clock.Clock); в виртуальном режиме циклы не нужны — сроки
// проходит AdvanceTo.

// wakeMargin — запас сверх расчётного срока: часы hw-mcp — секунды Unix в float64 (шаг ≈ 0,24 мкс),
// и без запаса таймер мог бы проснуться на единицу округления раньше срока.
const wakeMargin = time.Microsecond

func sleepFor(dt float64) time.Duration {
	if !(dt > 0) {
		return 0
	}
	if dt > 24*3600 {
		dt = 24 * 3600
	}
	return time.Duration(math.Ceil(dt*1e9)) + wakeMargin
}

// kickTimers сообщает циклу таймеров, что сроки аренды или импульсов изменились (не блокируется).
func (h *Hub) kickTimers() {
	select {
	case h.wake <- struct{}{}:
	default:
	}
}

// RunTimers — сторожевой таймер аренды и концы импульсов в реальном времени (до отмены ctx).
func (h *Hub) RunTimers(ctx context.Context) {
	t := time.NewTimer(time.Hour)
	t.Stop()
	defer t.Stop()
	for {
		armed := false
		if next, ok := h.NextTimerDue(); ok {
			t.Reset(sleepFor(next - h.Now()))
			armed = true
		}
		select {
		case <-ctx.Done():
			return
		case <-h.wake:
			if armed {
				t.Stop() // Go ≥ 1.23: после Stop канал таймера не отдаёт устаревшее значение
			}
		case <-t.C:
			h.timerWakeups.Add(1)
			h.StepTimers(h.Now())
		}
	}
}

// RunSampler — фоновый опрос в реальном времени (до отмены ctx). Сроки — у каждого свойства свои,
// следующий срок = предыдущий + период, поэтому фактический период равен заданному.
func (h *Hub) RunSampler(ctx context.Context) {
	t := time.NewTimer(time.Hour)
	t.Stop()
	defer t.Stop()
	for {
		next := h.NextSampleDue()
		if math.IsInf(next, 1) {
			<-ctx.Done()
			return
		}
		t.Reset(sleepFor(next - h.Now()))
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			h.sampleWakeups.Add(1)
			h.StepSample(h.Now())
		}
	}
}
