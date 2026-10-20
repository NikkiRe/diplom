package agent

import (
	"strings"
	"testing"
	"time"

	"github.com/local/edgectx/internal/llm"
	"github.com/local/edgectx/internal/trace"
)

// Параметры запроса (H1 обзора оптимизации, 20.10.2026): уровень рассуждения
// неизменен весь прогон, а 4xx от неподдержанного параметра не размыкает
// предохранитель.

func (f *fixture) bodies() []map[string]any {
	f.a.WaitIdle(5 * time.Second)
	f.stub.mu.Lock()
	defer f.stub.mu.Unlock()
	return append([]map[string]any(nil), f.stub.bodies...)
}

func (f *fixture) breakerState() string {
	f.a.mu.Lock()
	defer f.a.mu.Unlock()
	return f.a.link.State()
}

const paramsLLM = "llm: {model: m, trace_headers: true, extra: {reasoning_effort: low, verbosity: low}}\n"

// 4xx — неверен запрос, а не связь: ответ с ошибкой, предохранитель замкнут,
// следующий ход идёт к модели, а не откладывается.
func TestRejectedParamKeepsBreakerClosed(t *testing.T) {
	rejected := &llm.Error{Kind: llm.KindRejected, Status: 400, Attempts: 1,
		Body: `{"error":{"message":"Unsupported value: 'reasoning_effort'","code":"unsupported_value"}}`}
	f := newAgent(t, paramsLLM+"policy: {wake: gate}\n",
		func(llm.Request, map[string]string) (*llm.Response, error) { return nil, rejected })
	for i := 0; i < 2; i++ {
		rep := f.user(t, "включи свет")
		if rep.OK || rep.Deferred || !strings.Contains(rep.Text, "HTTP 400") {
			t.Fatalf("ход %d: %+v", i, rep)
		}
	}
	if f.calls() != 2 || len(f.records(trace.LLMUnavailable)) != 0 || len(f.records(trace.Deferred)) != 0 {
		t.Fatalf("calls=%d unavailable=%d deferred=%d", f.calls(), len(f.records(trace.LLMUnavailable)),
			len(f.records(trace.Deferred)))
	}
	if st := f.breakerState(); st != "closed" {
		t.Fatalf("предохранитель %s", st)
	}
	if resp := f.records(trace.LLMResponse); resp[0]["error_kind"] != llm.KindRejected {
		t.Fatalf("llm_response %v", resp[0])
	}
}

// Уровень рассуждения — во всех запросах прогона, включая шаги после
// инструментов и сводку истории, и в run_start.
func TestReasoningParamsConstantInRun(t *testing.T) {
	f := newAgent(t, paramsLLM+
		"policy: {wake: gate, history: summary_system, window_turns: 1, summary_threshold_tokens: 10}\n",
		func(req llm.Request, h map[string]string) (*llm.Response, error) {
			if h["X-Edgectx-Purpose"] == "turn" && req.Messages[len(req.Messages)-1].Role == "user" {
				return toolResp("recent_events", map[string]any{}), nil
			}
			return textResp("Готово.", 100), nil
		})
	f.user(t, "что было?")
	f.user(t, "а ещё?")
	purposes := map[string]bool{}
	for _, r := range f.records(trace.LLMRequest) {
		purposes[r["purpose"].(string)] = true
	}
	b := f.bodies()
	if len(b) < 5 || !purposes["summary"] {
		t.Fatalf("запросов %d, цели %v", len(b), purposes)
	}
	for i, x := range b {
		if x["reasoning_effort"] != "low" || x["verbosity"] != "low" {
			t.Fatalf("запрос %d: %v", i, x)
		}
	}
	rs := f.records(trace.RunStart)[0]["llm"].(map[string]any)
	if rs["reasoning_effort"] != "low" || rs["verbosity"] != "low" {
		t.Fatalf("run_start.llm %v", rs)
	}
}
