package tests

import (
	"bufio"
	"encoding/json"
	"os"
	"testing"

	"github.com/local/edgectx/internal/trace"
)

// rec — запись трассы.
type rec map[string]any

func (r rec) s(k string) string { v, _ := r[k].(string); return v }
func (r rec) f(k string) float64 {
	v, _ := r[k].(float64)
	return v
}
func (r rec) b(k string) bool { v, _ := r[k].(bool); return v }
func (r rec) list(k string) []string {
	var out []string
	if l, ok := r[k].([]any); ok {
		for _, x := range l {
			if s, ok := x.(string); ok {
				out = append(out, s)
			}
		}
	}
	return out
}

func readTrace(t *testing.T, path string) []rec {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	var out []rec
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 16<<20)
	for sc.Scan() {
		var r rec
		if err := json.Unmarshal(sc.Bytes(), &r); err != nil {
			t.Fatalf("bad trace line %q: %v", sc.Text(), err)
		}
		out = append(out, r)
	}
	return out
}

func byType(recs []rec, typ string) []rec {
	var out []rec
	for _, r := range recs {
		if r.s("type") == typ {
			out = append(out, r)
		}
	}
	return out
}

// checkTrace проверяет согласованность трассы (контракт §6) и возвращает число
// записей по типам.
func checkTrace(t *testing.T, recs []rec) map[string]int {
	t.Helper()
	known := map[string]bool{}
	for _, k := range trace.Types {
		known[k] = true
	}
	if len(recs) < 2 || recs[0].s("type") != trace.RunStart || recs[len(recs)-1].s("type") != trace.RunEnd {
		t.Fatalf("trace must start with run_start and end with run_end (%d records)", len(recs))
	}
	counts := map[string]int{}
	runID := recs[0].s("run_id")
	lastClock := -1.0
	reqs := map[string]int{}   // req_id -> число ответов
	calls := map[string]int{}  // call_id -> число результатов
	wakes := map[string]rec{}  // wake_id -> запись (допущенные)
	inputs := map[string]rec{} // id -> вход
	inWake := map[string]int{} // вход -> число допущенных пробуждений с ним
	suppressed := map[string]int{}
	userReplies := map[string]int{}
	wakeHasReq := map[string]bool{}
	for i, r := range recs {
		typ := r.s("type")
		counts[typ]++
		if !known[typ] {
			t.Errorf("record %d: unknown type %q", i, typ)
		}
		for _, k := range []string{"ts_wall", "ts_clock"} {
			if _, ok := r[k].(float64); !ok {
				t.Errorf("record %d (%s): %s missing or not a number", i, typ, k)
			}
		}
		if r.s("run_id") != runID {
			t.Errorf("record %d: run_id %q != %q", i, r.s("run_id"), runID)
		}
		if int(r.f("seq")) != i+1 {
			t.Errorf("record %d: seq %v", i, r["seq"])
		}
		if c := r.f("ts_clock"); c < lastClock {
			t.Errorf("record %d (%s): ts_clock went back %v < %v", i, typ, c, lastClock)
		} else {
			lastClock = c
		}
		switch typ {
		case trace.Input:
			id := r.s("id")
			if _, dup := inputs[id]; dup || id == "" {
				t.Errorf("input id %q duplicated or empty", id)
			}
			switch r.s("kind") {
			case "user", "sensor", "situation", "tick":
			default:
				t.Errorf("input %s: bad kind %q", id, r.s("kind"))
			}
			inputs[id] = r
		case trace.Wake:
			if _, ok := r["suppressed"].(bool); !ok {
				t.Errorf("wake %s: suppressed must be bool", r.s("wake_id"))
			}
			switch r.s("reason") {
			case "user", "gate", "heartbeat", "rule", "deferred":
			default:
				t.Errorf("wake %s: bad reason %q", r.s("wake_id"), r.s("reason"))
			}
			for _, id := range r.list("input_ids") {
				if _, ok := inputs[id]; !ok {
					t.Errorf("wake %s references unknown input %s", r.s("wake_id"), id)
				}
			}
			if !r.b("suppressed") {
				wakes[r.s("wake_id")] = r
				for _, id := range r.list("input_ids") {
					inWake[id]++
				}
			}
		case trace.Suppress:
			id := r.s("input_id")
			if _, ok := inputs[id]; !ok {
				t.Errorf("suppress of unknown input %q", id)
			}
			if r.s("reason") == "" {
				t.Errorf("suppress %s without reason", id)
			}
			suppressed[id]++
		case trace.LLMRequest:
			id := r.s("req_id")
			if _, dup := reqs[id]; dup {
				t.Errorf("req_id %s duplicated", id)
			}
			reqs[id] = 0
			if _, ok := wakes[r.s("wake_id")]; !ok {
				t.Errorf("llm_request %s for unknown/suppressed wake %s", id, r.s("wake_id"))
			}
			wakeHasReq[r.s("wake_id")] = true
			seg, ok := r["segments"].(map[string]any)
			if !ok || len(seg) != 5 {
				t.Errorf("llm_request %s: segments %v", id, r["segments"])
			}
			for _, k := range []string{"prefix", "profile", "history", "state", "turn"} {
				if _, ok := seg[k].(float64); !ok {
					t.Errorf("llm_request %s: segment %s missing", id, k)
				}
			}
			if r.f("prompt_tokens_est") <= 0 || r.s("model") == "" {
				t.Errorf("llm_request %s: prompt_tokens_est/model", id)
			}
		case trace.LLMResponse:
			id := r.s("req_id")
			n, ok := reqs[id]
			if !ok {
				t.Errorf("llm_response %s without request", id)
			}
			reqs[id] = n + 1
			u, ok := r["usage"].(map[string]any)
			if !ok {
				t.Errorf("llm_response %s: no usage", id)
			}
			for _, k := range []string{"prompt_tokens", "completion_tokens", "cached_tokens"} {
				if _, ok := u[k].(float64); !ok {
					t.Errorf("llm_response %s: usage.%s missing", id, k)
				}
			}
			if _, ok := r["latency_ms"].(float64); !ok || r.s("finish_reason") == "" {
				t.Errorf("llm_response %s: latency/finish_reason", id)
			}
		case trace.ToolCall:
			id := r.s("call_id")
			if _, dup := calls[id]; dup {
				t.Errorf("call_id %s duplicated", id)
			}
			calls[id] = 0
			if r.s("server") == "" || r.s("name") == "" {
				t.Errorf("tool_call %s: server/name", id)
			}
			if r.s("by") == "llm" {
				if _, ok := wakes[r.s("wake_id")]; !ok {
					t.Errorf("tool_call %s by llm outside an admitted wake", id)
				}
			}
		case trace.ToolResult:
			id := r.s("call_id")
			n, ok := calls[id]
			if !ok {
				t.Errorf("tool_result %s without call", id)
			}
			calls[id] = n + 1
			for _, k := range []string{"ok", "denied"} {
				if _, ok := r[k].(bool); !ok {
					t.Errorf("tool_result %s: %s must be bool", id, k)
				}
			}
		case trace.UserReply:
			userReplies[r.s("input_id")]++
		case trace.RuleFired:
			if r.s("rule_id") == "" {
				t.Error("rule_fired without rule_id")
			}
		case trace.Notify:
			if r.s("text") == "" {
				t.Error("notify without text")
			}
		}
	}
	for id, n := range reqs {
		if n != 1 {
			t.Errorf("llm request %s has %d responses", id, n)
		}
	}
	for id, n := range calls {
		if n != 1 {
			t.Errorf("tool call %s has %d results", id, n)
		}
	}
	for id := range wakes {
		if !wakeHasReq[id] {
			t.Errorf("admitted wake %s without llm_request", id)
		}
	}
	for id, in := range inputs {
		admitted, sup := inWake[id], suppressed[id]
		switch {
		case admitted == 0 && sup == 0:
			t.Errorf("input %s (%s) has no outcome", id, in.s("kind"))
		case admitted > 0 && sup > 0:
			t.Errorf("input %s both woke the LLM and was suppressed", id)
		case admitted > 1:
			t.Errorf("input %s in %d admitted wakes", id, admitted)
		}
		if in.s("kind") == "user" && userReplies[id] != 1 && sup == 0 {
			t.Errorf("user input %s has %d replies", id, userReplies[id])
		}
	}
	return counts
}
