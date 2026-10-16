package agent

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/local/edgectx/internal/clock"
	"github.com/local/edgectx/internal/gate"
	"github.com/local/edgectx/internal/llm"
	"github.com/local/edgectx/internal/trace"
)

var (
	errOffline = &llm.Error{Kind: llm.KindNetwork, Attempts: 1, Err: errors.New("connection refused")}
	errBusy    = &llm.Error{Kind: llm.KindServer, Attempts: 3, Waited: 1500 * time.Millisecond, Status: 503,
		Body: "upstream overloaded"}
)

// offlineLLM — модель, недоступная, пока down = true; иначе отвечает fn.
func offlineLLM(down *atomic.Bool, fn func(req llm.Request, h map[string]string) *llm.Response) func(llm.Request, map[string]string) (*llm.Response, error) {
	return func(req llm.Request, h map[string]string) (*llm.Response, error) {
		if down.Load() {
			return nil, errOffline
		}
		return fn(req, h), nil
	}
}

func (f *fixture) setProbe(probe func(ctx context.Context) error) {
	f.stub.mu.Lock()
	defer f.stub.mu.Unlock()
	f.stub.probe = probe
}

// probeWhile — проба не проходит, пока down = true.
func probeWhile(down *atomic.Bool) func(context.Context) error {
	return func(context.Context) error {
		if down.Load() {
			return errOffline
		}
		return nil
	}
}

func (f *fixture) after(sec float64) {
	f.a.Advance(clock.Seconds(f.v)+sec, AdvanceOptions{})
	f.a.WaitIdle(5 * time.Second)
}

func (f *fixture) event(t *testing.T, id, device, prop string, v any) {
	t.Helper()
	if _, err := f.a.Event(EventInput{ID: id, Device: device, Property: prop, Value: v}); err != nil {
		t.Fatal(err)
	}
	f.a.WaitIdle(5 * time.Second)
}

func (f *fixture) calls() int {
	f.stub.mu.Lock()
	defer f.stub.mu.Unlock()
	return len(f.stub.reqs)
}

const outageConfig = "policy: {wake: gate, hwctx: delta_tail, gate: {debounce_s: 0, coalesce_s: 0, cooldown_s: 0, default: change}}\n"

// Обрыв связи с LLM: первый сорвавшийся ход и всё, что пришло без связи, —
// включая вопрос пользователя — уходит одним ходом после восстановления;
// локальные правила работают и без модели.
func TestOutageDefersAndCoalescesIntoOneTurn(t *testing.T) {
	var down atomic.Bool
	down.Store(true)
	f := newAgent(t, outageConfig, offlineLLM(&down, func(req llm.Request, h map[string]string) *llm.Response {
		return textResp("Сейчас 31 °C, дверь открыта.", 100)
	}))
	f.setProbe(probeWhile(&down))
	if _, err := f.a.gate.AddRule(gate.Rule{ID: "r1", Device: "door1", Property: "open", Op: "eq", Value: false,
		Action: "notify", Text: "дверь закрыта"}); err != nil {
		t.Fatal(err)
	}

	f.event(t, "e1", "door1", "open", true) // ход начат и сорвался: модель недоступна
	f.event(t, "e2", "t1", "temperature", 31.0)
	rep := f.user(t, "какая температура?")
	if !rep.Deferred || rep.OK || rep.Text != deferredReplyText || rep.LLMCalls != 0 {
		t.Fatalf("user reply while offline %+v", rep)
	}
	f.event(t, "e3", "door1", "open", false) // правило исполняется без модели
	if n := f.calls(); n != 1 {
		t.Fatalf("only the first turn may reach the model while it is down, got %d requests", n)
	}
	if n := len(f.records(trace.RuleFired)); n != 1 {
		t.Fatalf("local rule must fire offline: %d", n)
	}

	f.after(31) // первая проба: связи нет
	down.Store(false)
	f.after(61) // вторая проба: связь есть → один ход
	reqs := f.requests()
	if len(reqs) != 2 {
		t.Fatalf("recovery must cost exactly one request, got %d total", len(reqs))
	}
	f.stub.mu.Lock()
	hdr := f.stub.hdrs[1]
	f.stub.mu.Unlock()
	state := reqs[1].Messages[len(reqs[1].Messages)-2].Content
	for _, want := range []string{"Пока связи с моделью не было, произошло:", "- 07:00:00 door1.open = true",
		"- 07:00:00 t1.temperature = 31", "- 07:00:00 сообщение пользователя: какая температура?", "door1.open=false"} {
		if !strings.Contains(state, want) {
			t.Errorf("recovery state lacks %q:\n%s", want, state)
		}
	}
	if hdr["X-Edgectx-Wake-Reason"] != reasonDeferred {
		t.Fatalf("wake reason header %q", hdr["X-Edgectx-Wake-Reason"])
	}

	if n := len(f.records(trace.LLMUnavailable)); n != 1 {
		t.Fatalf("llm_unavailable records %d", n)
	}
	probes := f.records(trace.LLMProbe)
	if len(probes) != 2 || probes[0]["ok"] != false || probes[0]["error_kind"] != "network" || probes[1]["ok"] != true {
		t.Fatalf("probes %v", probes)
	}
	avail := f.records(trace.LLMAvailable)
	if len(avail) != 1 || avail[0]["down_s"].(float64) < 30 {
		t.Fatalf("llm_available %v", avail)
	}
	deferred := f.records(trace.Deferred)
	if len(deferred) != 3 || deferred[0]["retry"] != true || deferred[1]["retry"] != false {
		t.Fatalf("deferred %v", deferred)
	}
	var merged map[string]any
	for _, w := range f.records(trace.Wake) {
		if w["reason"] == reasonDeferred {
			merged = w
		}
	}
	// e1 уже получил исход в сорвавшемся ходе, у e2 и вопроса — это первое пробуждение.
	if ids := strings.Join(toStrings(merged["input_ids"]), ","); ids != "e2,user-1" || len(toStrings(merged["coalesced"])) != 3 ||
		merged["priority"] != "user" {
		t.Fatalf("merged wake %v", merged)
	}
	var reply map[string]any
	for _, r := range f.records(trace.UserReply) {
		reply = r
	}
	if reply["deferred"] != true || reply["ok"] != true || reply["text"] != "Сейчас 31 °C, дверь открыта." {
		t.Fatalf("deferred user_reply %v", reply)
	}
	m := f.a.Metrics()
	link := m["llm_link"].(map[string]any)
	llmM := m["llm"].(map[string]int)
	if link["state"] != "closed" || link["deferred"] != 0 || llmM["errors_network"] != 1 || llmM["probes"] != 2 ||
		llmM["probe_bytes"] != 40 || llmM["unavailable"] != 1 {
		t.Fatalf("metrics llm_link %v, llm %v", link, llmM)
	}
}

func toStrings(v any) []string {
	var out []string
	for _, x := range v.([]any) {
		out = append(out, x.(string))
	}
	return out
}

// Вопрос, первым наткнувшийся на обрыв, ждёт неудачного обращения и тоже
// получает «отложено» — с честными задержкой и числом обращений.
func TestQuestionHittingOutageIsDeferred(t *testing.T) {
	var down atomic.Bool
	down.Store(true)
	f := newAgent(t, outageConfig, offlineLLM(&down, func(llm.Request, map[string]string) *llm.Response {
		return textResp("на связи", 10)
	}))
	f.setProbe(probeWhile(&down))
	rep := f.user(t, "ты тут?")
	if !rep.Deferred || rep.OK || rep.LLMCalls != 1 || rep.LatencyMs <= 0 {
		t.Fatalf("reply %+v", rep)
	}
	if d := f.records(trace.Deferred); len(d) != 1 || d[0]["retry"] != true {
		t.Fatalf("deferred %v", d)
	}
	down.Store(false)
	f.after(31)
	replies := f.records(trace.UserReply)
	if len(replies) != 1 || replies[0]["deferred"] != true || replies[0]["text"] != "на связи" {
		t.Fatalf("user_reply %v", replies)
	}
}

// Таймаут попытки: запрос мог быть уже обработан и оплачен, поэтому ход не
// откладывается и вопрос не отправляется снова; модель признана недоступной.
func TestTimedOutTurnIsNotResent(t *testing.T) {
	f := newAgent(t, outageConfig, func(llm.Request, map[string]string) (*llm.Response, error) {
		return nil, &llm.Error{Kind: llm.KindTimeout, Attempts: 1, Err: context.DeadlineExceeded}
	})
	rep := f.user(t, "сколько градусов?")
	if rep.Deferred || rep.OK || !strings.Contains(rep.Text, "timeout") {
		t.Fatalf("reply %+v", rep)
	}
	resp := f.records(trace.LLMResponse)
	if len(resp) != 1 || resp[0]["maybe_billed"] != true || resp[0]["error_kind"] != "timeout" {
		t.Fatalf("llm_response %v", resp)
	}
	if len(f.records(trace.LLMUnavailable)) != 1 || len(f.records(trace.Deferred)) != 0 {
		t.Fatal("timeout must open the breaker without deferring the turn")
	}
	f.after(31) // проба удалась: повторять нечего
	if n := f.calls(); n != 1 || len(f.records(trace.UserReply)) != 1 {
		t.Fatalf("timed-out request resent: %d calls", n)
	}
}

// Провайдер достижим (/models отвечает), а модель — нет (5xx): удачная проба
// лишь полуоткрывает предохранитель, сбой пробного хода снова размыкает его с
// большей паузой. Паузы 15–30, 30–60, 60–120, 120–240, затем 150–300 с: за час
// не больше 4 + 3600/150 пробных ходов (без роста паузы было 157).
func TestProbeOKButModelFailingBacksOff(t *testing.T) {
	f := newAgent(t, outageConfig, func(llm.Request, map[string]string) (*llm.Response, error) { return nil, errBusy })
	f.event(t, "e1", "door1", "open", true)
	for i := 0; i < 60; i++ {
		f.after(60)
	}
	probes := f.records(trace.LLMProbe)
	if n := f.calls(); n > 1+4+24 || n != len(probes)+1 {
		t.Fatalf("model calls %d, probes %d", n, len(probes))
	}
	last := len(probes) - 1
	if gap := probes[last]["ts_clock"].(float64) - probes[last-1]["ts_clock"].(float64); gap < 150 {
		t.Fatalf("probe interval must reach the cap range, last gap %.0f s", gap)
	}
	if len(f.records(trace.LLMUnavailable)) != 1 || len(f.records(trace.LLMAvailable)) != 0 {
		t.Fatal("the outage is one unavailable period without recovery")
	}
	resp := f.records(trace.LLMResponse)[0]
	if resp["attempts"] != 3.0 || resp["retry_wait_ms"] != 1500.0 || resp["maybe_billed"] != nil {
		t.Fatalf("llm_response %v", resp)
	}
	if st := f.a.Metrics()["llm_link"].(map[string]any)["state"]; st != "open" {
		t.Fatalf("breaker state %v", st)
	}
}

// Сорвавшийся пробный ход возвращает в очередь свои части по отдельности:
// sched.deferred_max ограничивает число отложенных пробуждений, а с ним —
// память и хвост промпта слитого хода, сколько бы ни длился обрыв.
func TestRepeatedTrialFailureKeepsQueueBounded(t *testing.T) {
	var tail atomic.Value
	f := newAgent(t, outageConfig+"sched: {deferred_max: 4}\n", func(req llm.Request, h map[string]string) (*llm.Response, error) {
		tail.Store(req.Messages[len(req.Messages)-2].Content)
		return nil, errBusy
	})
	open := false
	for i := 1; i <= 120; i++ {
		open = !open
		f.event(t, fmt.Sprintf("e%d", i), "door1", "open", open)
		f.after(60)
		f.a.mu.Lock()
		n := len(f.a.deferred)
		merged := slices.ContainsFunc(f.a.deferred, func(w *wake) bool { return len(w.parts) > 0 })
		f.a.mu.Unlock()
		if n > 4 || merged {
			t.Fatalf("minute %d: %d deferred, merged inside: %v", i, n, merged)
		}
	}
	last := tail.Load().(string)
	if n := strings.Count(last, "\n- "); n == 0 || n > 4 {
		t.Fatalf("merged turn tail must list at most deferred_max wakes, got %d:\n%s", n, last)
	}
	full := 0
	for _, r := range f.records(trace.Suppress) {
		if r["reason"] == reasonDeferredFull {
			full++
		}
	}
	if full < 100 {
		t.Fatalf("overflowing wakes must get an outcome: %d deferred_full", full)
	}
}

// Очередь отложенного ограничена: вытесняется худшее пробуждение, повторная
// плановая проверка не копится, а при остановке всё отложенное получает исход.
func TestDeferredQueueBoundedAndDroppedOnShutdown(t *testing.T) {
	var down atomic.Bool
	down.Store(true)
	f := newAgent(t, "policy: {wake: hybrid, heartbeat_min: 1, gate: {debounce_s: 0, coalesce_s: 0, cooldown_s: 0, default: change}}\n"+
		"sched: {deferred_max: 3}\n", offlineLLM(&down, func(llm.Request, map[string]string) *llm.Response {
		return textResp("NO_ACTION", 10)
	}))
	f.setProbe(probeWhile(&down))
	f.event(t, "e1", "door1", "open", true) // ход сорвался и отложен
	f.after(60)                             // тик 07:01 — отложен
	f.after(60)                             // тик 07:02 — плановая проверка уже ждёт
	f.event(t, "e2", "door1", "open", false)
	f.event(t, "e3", "door1", "open", true) // очередь полна, новое не лучше — отклонено
	if rep := f.user(t, "ты тут?"); !rep.Deferred {
		t.Fatalf("user reply %+v", rep) // пользователь важнее — вытесняет самое старое фоновое (e1)
	}
	sup := map[string]string{}
	for _, r := range f.records(trace.Suppress) {
		sup[r["input_id"].(string)] = r["reason"].(string)
	}
	if sup["hb-2"] != reasonHeartbeatPending || sup["e3"] != reasonDeferredFull || len(sup) != 2 {
		t.Fatalf("suppress %v", sup)
	}
	if errs := f.records(trace.Error); len(errs) != 1 || errs[0]["where"] != "deferred_lost" {
		t.Fatalf("evicted retried wake must be reported: %v", errs)
	}
	if n := f.calls(); n != 1 {
		t.Fatalf("requests while offline %d", n)
	}
	f.a.Close(time.Second)
	for _, r := range f.records(trace.Suppress) {
		sup[r["input_id"].(string)] = r["reason"].(string)
	}
	if sup["hb-1"] != "shutdown" || sup["e2"] != "shutdown" || sup["user-1"] != "shutdown" {
		t.Fatalf("deferred inputs after shutdown %v", sup)
	}
	replies := f.records(trace.UserReply)
	if len(replies) != 1 || replies[0]["ok"] != false || replies[0]["deferred"] != true {
		t.Fatalf("deferred user must get a final reply: %v", replies)
	}
}

// Вопрос, чей ход уже был допущен и сорвался, когда очередь отложенного занята
// другим вопросом, получает итог и по HTTP, и в трассе (user_reply с ошибкой).
func TestRetriedQuestionRejectedByFullQueueGetsReply(t *testing.T) {
	var down atomic.Bool
	down.Store(true)
	f := newAgent(t, "llm: {model: m, trace_headers: true, breaker: {failures: 2}}\n"+outageConfig+
		"sched: {deferred_max: 1}\n", offlineLLM(&down, nil))
	f.setProbe(probeWhile(&down))
	if rep := f.user(t, "первый"); !rep.Deferred { // сбой ниже порога: вопрос отложен
		t.Fatalf("first reply %+v", rep)
	}
	rep := f.user(t, "второй") // второй сбой размыкает предохранитель, очередь занята первым вопросом
	if rep.Deferred || rep.OK || rep.Error != reasonDeferredFull || rep.Text == "" {
		t.Fatalf("second reply %+v", rep)
	}
	replies := f.records(trace.UserReply)
	if len(replies) != 1 || replies[0]["input_id"] != "user-2" || replies[0]["ok"] != false || replies[0]["deferred"] != nil {
		t.Fatalf("user_reply %v", replies)
	}
}

// Дельта L1 считается показанной модели только после её ответа.
func TestDeltaShownAgainAfterLLMError(t *testing.T) {
	var calls atomic.Int32
	f := newAgent(t, "policy: {wake: gate, hwctx: delta_tail, gate: {default: ignore}}\n",
		func(req llm.Request, h map[string]string) (*llm.Response, error) {
			if calls.Add(1) == 2 {
				return nil, errors.New("bad request") // ошибка запроса, не связи: ход не откладывается
			}
			return textResp("ок", 10), nil
		})
	f.user(t, "1")
	f.event(t, "e1", "door1", "open", true)
	if rep := f.user(t, "2"); rep.OK {
		t.Fatal("second turn must fail")
	}
	f.user(t, "3")
	f.user(t, "4")
	reqs := f.requests()
	state := func(i int) string { return reqs[i].Messages[len(reqs[i].Messages)-2].Content }
	if !strings.Contains(state(1), "door1.open=true") || !strings.Contains(state(2), "door1.open=true") {
		t.Fatalf("change must be shown again after the failed request:\n%s\n---\n%s", state(1), state(2))
	}
	if strings.Contains(state(3), "door1") {
		t.Fatalf("change acknowledged by the model must not repeat:\n%s", state(3))
	}
}

// Порог 2: первый сбой хода не размыкает предохранитель, размыкает его
// неудачная проба — с записью llm_unavailable после llm_probe; длительность
// обрыва в llm_available отсчитывается от неё.
func TestProbeFailureOpensBreaker(t *testing.T) {
	var down atomic.Bool
	down.Store(true)
	f := newAgent(t, "llm: {model: m, trace_headers: true, breaker: {failures: 2}}\n"+outageConfig,
		offlineLLM(&down, func(llm.Request, map[string]string) *llm.Response { return textResp("NO_ACTION", 10) }))
	f.setProbe(probeWhile(&down))
	f.event(t, "e1", "door1", "open", true)
	if len(f.records(trace.LLMUnavailable)) != 0 || len(f.records(trace.Deferred)) != 1 {
		t.Fatal("a failure below the threshold defers the turn without opening the breaker")
	}
	f.after(31)
	var order []string
	for _, r := range f.all() {
		if typ := r["type"].(string); typ == trace.LLMProbe || typ == trace.LLMUnavailable {
			order = append(order, typ)
		}
	}
	un := f.records(trace.LLMUnavailable)
	if strings.Join(order, ",") != "llm_probe,llm_unavailable" || un[0]["failures"] != 2.0 || f.a.m.get("llm.unavailable") != 1 {
		t.Fatalf("failed probe must open the breaker: %v %v", order, un)
	}
	down.Store(false)
	f.after(61)
	avail := f.records(trace.LLMAvailable)
	if len(avail) != 1 || avail[0]["down_s"].(float64) < 30 || avail[0]["down_s"].(float64) > 61 {
		t.Fatalf("llm_available %v", avail)
	}
}

// Предохранитель с порогом 2: первый сбой не размыкает, но ход откладывается и
// уходит вместе со следующим успешным обращением, а заведённая проба
// отменяется.
func TestBreakerThresholdAndRecoveryByResponse(t *testing.T) {
	var fail atomic.Bool
	fail.Store(true)
	f := newAgent(t, "llm: {model: m, trace_headers: true, breaker: {failures: 2}}\n"+outageConfig,
		func(req llm.Request, h map[string]string) (*llm.Response, error) {
			if fail.Swap(false) {
				return nil, &llm.Error{Kind: llm.KindServer, Status: 503, RetryAfter: 2 * time.Minute}
			}
			return textResp("NO_ACTION", 10), nil
		})
	f.event(t, "e1", "door1", "open", true)
	if f.a.Metrics()["llm_link"].(map[string]any)["state"] != "closed" || len(f.records(trace.LLMUnavailable)) != 0 {
		t.Fatal("one failure below the threshold must not open the breaker")
	}
	f.event(t, "e2", "door1", "open", false) // модель отвечает → отложенное уходит следом
	reqs := f.requests()
	if len(reqs) != 3 || !strings.Contains(reqs[2].Messages[len(reqs[2].Messages)-2].Content, "Пока связи с моделью не было") {
		t.Fatalf("requests %d", len(reqs))
	}
	f.after(121) // проба была заведена на Retry-After = 2 мин
	f.stub.mu.Lock()
	probes := f.stub.probes
	f.stub.mu.Unlock()
	if probes != 0 || len(f.records(trace.LLMAvailable)) != 0 {
		t.Fatal("a successful response cancels the probe; the breaker never opened")
	}
}

// Остановка ограничена своим пределом: зависшее обращение к модели прерывается.
func TestCloseInterruptsHungTurn(t *testing.T) {
	release := make(chan struct{})
	defer close(release)
	f := newAgent(t, outageConfig, func(llm.Request, map[string]string) (*llm.Response, error) {
		<-release
		return textResp("NO_ACTION", 1), nil
	})
	if _, err := f.a.Event(EventInput{ID: "e1", Device: "door1", Property: "open", Value: true}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "turn in flight", func() bool { return f.calls() == 1 })
	start := time.Now()
	f.a.Close(200 * time.Millisecond)
	if d := time.Since(start); d > 2*time.Second {
		t.Fatalf("Close took %v", d)
	}
	if end := f.records(trace.RunEnd); len(end) != 1 || end[0]["idle"] != false {
		t.Fatalf("run_end %v", end)
	}
	if resp := f.records(trace.LLMResponse); len(resp) != 1 || resp[0]["error_kind"] != "canceled" {
		t.Fatalf("llm_response %v", resp)
	}
}

// Проба связи, висящая на мёртвой сети, не задерживает остановку.
func TestCloseCancelsHangingProbe(t *testing.T) {
	var down atomic.Bool
	down.Store(true)
	f := newAgent(t, outageConfig, offlineLLM(&down, nil))
	f.setProbe(func(ctx context.Context) error {
		<-ctx.Done()
		return &llm.Error{Kind: llm.KindCanceled, Err: ctx.Err()}
	})
	f.event(t, "e1", "door1", "open", true)
	go f.a.Advance(clock.Seconds(f.v)+31, AdvanceOptions{})
	waitFor(t, "probe in flight", func() bool {
		f.stub.mu.Lock()
		defer f.stub.mu.Unlock()
		return f.stub.probes == 1
	})
	start := time.Now()
	f.a.Close(5 * time.Second)
	if d := time.Since(start); d > time.Second {
		t.Fatalf("Close waited for the probe: %v", d)
	}
	if end := f.records(trace.RunEnd); len(end) != 1 || end[0]["idle"] != true {
		t.Fatalf("run_end %v", end)
	}
}

// Остановка до старта (например, не подключился MCP-сервер) не пишет одинокий run_end.
func TestCloseBeforeStartWritesNothing(t *testing.T) {
	f := build(t, "policy: {wake: gate}\n", noop)
	f.a.Close(time.Second)
	if recs := f.all(); len(recs) != 0 {
		t.Fatalf("records %v", recs)
	}
}
