// Package breaker — автомат-предохранитель обращений к внешней LLM (circuit
// breaker: M. Nygard, «Release It!», 2007; M. Fowler, «CircuitBreaker», 2014).
//
// Замкнутый предохранитель пропускает ходы. После threshold временных сбоев
// подряд (нет связи, таймаут, 429, 5xx) он размыкается: агент не тратит ходы на
// недоступную модель, а проверяет связь дешёвой пробой. Пауза до следующей
// пробы растёт с каждым сбоем подряд (экспонента с джиттером и верхней
// границей) и не короче Retry-After провайдера. Удачная проба лишь
// полуоткрывает предохранитель: она показывает, что провайдер достижим, но не
// что модель отвечает, — ходы снова пропускаются, а счётчик сбоев сохраняется.
// Замыкает предохранитель и обнуляет счётчик только ответ модели; сбой хода
// после пробы снова размыкает его с паузой больше прежней.
//
// Пакет времени не измеряет и не синхронизирован: таймер пробы заводит, а
// блокировку держит агент (вместе с очередью отложенных пробуждений).
package breaker

import (
	"time"

	"github.com/local/edgectx/internal/backoff"
)

// Breaker — состояние предохранителя.
type Breaker struct {
	threshold int
	interval  backoff.Policy
	seed      uint64

	fails  int    // временных сбоев подряд
	probed bool   // после последнего сбоя была удачная проба: полуоткрыт
	n      uint64 // номер паузы — вход джиттера
}

// New создаёт замкнутый предохранитель: threshold — сбоев подряд до
// размыкания (не меньше 1), interval — пауза до пробы связи, seed — зерно
// джиттера.
func New(threshold int, interval backoff.Policy, seed uint64) *Breaker {
	return &Breaker{threshold: max(threshold, 1), interval: interval, seed: seed}
}

// Failure учитывает временный сбой обращения или пробы. Возвращает, разомкнул
// ли предохранитель именно этот сбой, и паузу до следующей пробы.
func (b *Breaker) Failure(retryAfter time.Duration) (opened bool, next time.Duration) {
	b.fails++
	b.n++
	b.probed = false
	next = b.interval.Delay(b.fails, backoff.Unit(b.seed, b.n))
	return b.fails == b.threshold, max(next, retryAfter)
}

// Probed — проба связи удалась: предохранитель после сбоя полуоткрыт.
func (b *Breaker) Probed() { b.probed = b.fails > 0 }

// Success — модель ответила: предохранитель замкнут, счётчик обнулён.
// Возвращает, был ли он разомкнут (или полуоткрыт после размыкания).
func (b *Breaker) Success() (wasOpen bool) {
	wasOpen = b.fails >= b.threshold
	b.fails, b.probed = 0, false
	return wasOpen
}

// Allow — можно ли начать ход: предохранитель замкнут или полуоткрыт.
func (b *Breaker) Allow() bool { return b.probed || b.fails < b.threshold }

// Waiting — ждёт ли предохранитель исхода пробы: сбой был, а удачной пробы
// после него ещё нет. Тогда отложенные пробуждения отправит восстановление
// связи; иначе их можно отправлять сразу.
func (b *Breaker) Waiting() bool { return b.fails > 0 && !b.probed }

// State — closed | open | half_open.
func (b *Breaker) State() string {
	switch {
	case b.probed:
		return "half_open"
	case b.fails >= b.threshold:
		return "open"
	}
	return "closed"
}

// Failures — временных сбоев подряд.
func (b *Breaker) Failures() int { return b.fails }
