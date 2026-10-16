// Package clock — единственный источник времени hw-mcp (IMPLEMENTATION_SPEC §0 п. 2).
//
// Время везде — число секунд float64, как в прототипе шлюза (actuator_gate.py):
//   - Real: секунды Unix, но отсчитываемые по монотонным часам от момента старта, поэтому
//     перевод системных часов (NTP) не сдвигает сроки аренды и TTL внутри процесса;
//   - Virtual: секунды от начала «дня» (T0, тесты); время двигается только явно (Set).
package clock

import (
	"fmt"
	"sync"
	"time"
)

// Clock возвращает текущее время в секундах.
type Clock interface {
	Now() float64
}

// Real — реальные часы.
type Real struct {
	startWall float64
	start     time.Time
}

// NewReal создаёт реальные часы с привязкой к текущему времени Unix.
func NewReal() *Real {
	now := time.Now()
	return &Real{startWall: float64(now.UnixNano()) / 1e9, start: now}
}

// Now — секунды Unix (монотонно от старта процесса).
func (r *Real) Now() float64 {
	return r.startWall + time.Since(r.start).Seconds()
}

// Virtual — виртуальные часы, управляемые извне (реплеер, тесты).
type Virtual struct {
	mu sync.Mutex
	t  float64
}

// NewVirtual создаёт виртуальные часы с начальным временем t0.
func NewVirtual(t0 float64) *Virtual { return &Virtual{t: t0} }

// Now возвращает текущее виртуальное время.
func (v *Virtual) Now() float64 {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.t
}

// Set переводит часы вперёд. Назад нельзя: сроки аренды и окна частоты считаются от монотонного времени.
func (v *Virtual) Set(t float64) error {
	v.mu.Lock()
	defer v.mu.Unlock()
	if t < v.t {
		return fmt.Errorf("virtual clock cannot go back: %g < %g", t, v.t)
	}
	v.t = t
	return nil
}
