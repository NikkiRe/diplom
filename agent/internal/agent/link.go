package agent

import (
	"cmp"
	"slices"
	"time"

	"github.com/local/edgectx/internal/llm"
	"github.com/local/edgectx/internal/sched"
	"github.com/local/edgectx/internal/trace"
)

// Связь с внешней LLM. Временный сбой обращения (нет сети, таймаут, 429, 5xx)
// учитывает предохранитель (internal/breaker) и заводит по часам агента пробу
// связи GET /models без токенов. Пока предохранитель разомкнут, ходы не
// начинаются: пробуждения откладываются в ограниченную очередь (не больше
// sched.deferred_max пробуждений), туда же — ход, сорвавшийся на первом
// обращении к модели, если запрос заведомо не оплачен. Локальные правила и гейт
// работают как обычно. Удачная проба полуоткрывает предохранитель, и всё
// отложенное уходит одним пробным ходом с причиной deferred — без шквала
// запросов после обрыва. Замыкает предохранитель только ответ модели; сбой
// пробного хода возвращает его части в очередь и снова размыкает
// предохранитель — с паузой больше прежней. Пользователь, чей вопрос отложен,
// сразу получает ответ «отложено» (HTTP 202), а ответ модели — в трассе как
// user_reply с deferred: true.
//
// Предохранитель, таймер пробы и очередь отложенного меняются вместе под a.mu,
// поэтому пробуждение не застревает в очереди при уже восстановленной связи.

// Причины пробуждения и подавления, которые назначает сам агент.
const (
	reasonDeferred         = "deferred"          // ход, в который слиты отложенные пробуждения
	reasonHeartbeatPending = "heartbeat_pending" // плановая проверка уже ждёт хода
	reasonDeferredFull     = "deferred_full"     // очередь отложенного переполнена
)

// linkOK — модель ответила: предохранитель замкнут, отложенное уходит одним ходом.
func (a *Agent) linkOK() {
	a.mu.Lock()
	wasOpen := a.link.Success()
	if a.probeTm != nil {
		a.probeTm.Stop()
	}
	downS := a.now() - a.downT
	ws := a.takeDeferredLocked()
	a.mu.Unlock()
	if wasOpen {
		a.tr.Emit(trace.LLMAvailable, trace.F{"down_s": downS})
	}
	a.flushDeferred(ws)
}

// linkFailed учитывает временный сбой обращения к модели или пробы связи
// (probe — запись llm_probe о ней, nil у обращения): при размыкании пишет
// llm_unavailable и заводит пробу через паузу предохранителя.
func (a *Agent) linkFailed(e *llm.Error, probe trace.F) {
	a.mu.Lock()
	opened, next := a.link.Failure(e.RetryAfter)
	failures := a.link.Failures()
	if opened {
		a.downT = a.now()
	}
	if a.probeTm != nil {
		a.probeTm.Stop()
	}
	if !a.closed {
		a.probeTm = a.clk.AfterFunc(next, a.probe)
	}
	a.mu.Unlock()
	if probe != nil {
		probe["retry_in_s"] = next.Seconds()
		a.tr.Emit(trace.LLMProbe, probe)
	}
	if opened {
		a.m.inc("llm.unavailable")
		a.tr.Emit(trace.LLMUnavailable, trace.F{"failures": failures, "error_kind": e.Kind, "error": e.Error(),
			"retry_in_s": next.Seconds()})
	}
}

// probe — проба связи по таймеру часов агента. Удачная проба полуоткрывает
// предохранитель, и отложенное уходит пробным ходом (нет отложенного — пробным
// станет следующий ход).
func (a *Agent) probe() {
	a.tracker.Begin()
	defer a.tracker.End()
	if a.isClosed() {
		return
	}
	start := time.Now()
	res, err := a.llmc.Probe(a.probeCtx)
	a.m.inc("llm.probes")
	a.m.add("llm.probe_bytes", res.Bytes)
	f := trace.F{"ok": err == nil, "ms": float64(time.Since(start).Microseconds()) / 1000,
		"status": res.Status, "bytes": res.Bytes}
	if err == nil {
		a.tr.Emit(trace.LLMProbe, f)
		a.mu.Lock()
		a.link.Probed()
		ws := a.takeDeferredLocked()
		a.mu.Unlock()
		a.flushDeferred(ws)
		return
	}
	e, typed := llm.AsError(err)
	if !typed {
		e = &llm.Error{Kind: llm.KindNetwork, Err: err}
	}
	if e.Kind == llm.KindCanceled {
		return // остановка агента
	}
	f["error"], f["error_kind"] = e.Error(), e.Kind
	a.linkFailed(e, f)
}

// rejection — пробуждение, отвергнутое очередью отложенного, и причина.
type rejection struct {
	w      *wake
	reason string
}

// deferWake откладывает пробуждение w до восстановления связи с моделью: само
// пробуждение или, у слитого хода, каждую его часть отдельно, поэтому
// sched.deferred_max ограничивает число отложенных пробуждений, а с ним и
// хвост промпта слитого хода. Новое пробуждение (calls = 0) откладывается,
// только пока предохранитель не пропускает ходы; ход, сорвавшийся на первом
// обращении к модели (calls — сколько их было), — всегда. Возвращает false,
// если w не отложено: ход надо начать или, при остановке, завершить как обычно.
func (a *Agent) deferWake(w *wake, calls int) bool {
	a.mu.Lock()
	if a.closed {
		a.mu.Unlock()
		if calls > 0 {
			return false
		}
		a.rejectWake(w, sched.ReasonShutdown)
		return true
	}
	if calls == 0 && a.link.Allow() {
		a.mu.Unlock()
		return false
	}
	var rejected []rejection
	for _, p := range w.flat() {
		if out, reason := a.enqueueLocked(p); out != nil {
			rejected = append(rejected, rejection{out, reason})
		}
	}
	var kept, users []*wake
	for _, p := range w.flat() {
		if !slices.Contains(a.deferred, p) {
			continue
		}
		kept = append(kept, p)
		if p.reason == "user" && !p.replied {
			p.replied = true
			users = append(users, p)
		}
	}
	// Запись — под блокировкой: отложенное может сразу уйти в слитый ход.
	rec := trace.F{"wake_id": w.id, "reason": w.reason, "priority": w.prio.String(), "input_ids": w.inputIDs,
		"retry": w.admitted, "pending": len(a.deferred)}
	if w.reason == reasonDeferred {
		rec["coalesced"] = wakeIDs(kept)
	}
	ws := a.takeDeferredLocked() // связь успела восстановиться: отправить сразу
	a.mu.Unlock()

	if len(kept) > 0 {
		a.tr.Emit(trace.Deferred, rec)
		a.m.inc("deferred.wakes")
	}
	for _, p := range users {
		p.done <- Reply{InputID: p.firstInput(), WakeID: w.id, Text: deferredReplyText, Deferred: true,
			LatencyMs: float64(time.Since(p.submitWall).Microseconds()) / 1000, LLMCalls: calls}
	}
	for _, r := range rejected {
		a.rejectWake(r.w, r.reason)
	}
	a.flushDeferred(ws)
	return true
}

// enqueueLocked ставит пробуждение в очередь отложенного (под a.mu) и
// возвращает отвергнутое — само p или вытесненное им — с причиной. Новая
// плановая проверка не копится, если одна уже ждёт. При переполнении
// вытесняется худшее по приоритету и самое старое пробуждение, если p лучше,
// иначе отвергается p (как в очереди планировщика).
func (a *Agent) enqueueLocked(p *wake) (*wake, string) {
	isHeartbeat := func(d *wake) bool { return d.reason == "heartbeat" }
	if isHeartbeat(p) && !p.admitted && slices.ContainsFunc(a.deferred, isHeartbeat) {
		return p, reasonHeartbeatPending
	}
	if len(a.deferred) < a.cfg.Sched.DeferredMax {
		a.deferred = append(a.deferred, p)
		return nil, ""
	}
	worst := slices.MaxFunc(a.deferred, func(x, y *wake) int {
		return cmp.Or(cmp.Compare(x.prio, y.prio), cmp.Compare(y.submitT, x.submitT))
	})
	if worst.prio <= p.prio {
		return p, reasonDeferredFull
	}
	a.deferred = append(slices.DeleteFunc(a.deferred, func(d *wake) bool { return d == worst }), p)
	return worst, reasonDeferredFull
}

// takeDeferredLocked забирает отложенное (под a.mu), если его пора отправить:
// предохранитель не ждёт исхода пробы.
func (a *Agent) takeDeferredLocked() []*wake {
	if a.closed || a.link.Waiting() {
		return nil
	}
	ws := a.deferred
	a.deferred = nil
	return ws
}

// flushDeferred сливает отложенные пробуждения в один ход.
func (a *Agent) flushDeferred(ws []*wake) {
	if len(ws) == 0 {
		return
	}
	slices.SortStableFunc(ws, func(x, y *wake) int { return cmp.Compare(x.submitT, y.submitT) })
	m := &wake{id: a.newID("w"), reason: reasonDeferred, prio: sched.Background, parts: ws,
		submitWall: time.Now(), submitT: a.now()}
	for _, p := range ws {
		m.prio = min(m.prio, p.prio)
		if !p.admitted {
			m.inputIDs = append(m.inputIDs, p.inputIDs...)
		}
		m.events = append(m.events, p.events...)
	}
	a.m.inc("deferred.turns")
	a.submit(m)
}

// dropDeferred отклоняет всё отложенное (остановка агента).
func (a *Agent) dropDeferred() {
	a.mu.Lock()
	ws := a.deferred
	a.deferred = nil
	a.mu.Unlock()
	for _, w := range ws {
		a.rejectWake(w, sched.ReasonShutdown)
	}
}
