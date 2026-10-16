package agent

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/edgectx/internal/clock"
	"github.com/local/edgectx/internal/config"
	"github.com/local/edgectx/internal/gate"
	"github.com/local/edgectx/internal/llm"
	"github.com/local/edgectx/internal/trace"
	"github.com/local/edgectx/internal/userctx"
)

// stubLLM — подмена LLM без HTTP: ответ на разобранный запрос вычисляет
// функция, проба связи — probe (nil — связь есть).
type stubLLM struct {
	mu     sync.Mutex
	reqs   []llm.Request
	hdrs   []map[string]string
	probes int
	fn     func(req llm.Request, h map[string]string) (*llm.Response, error)
	probe  func(ctx context.Context) error
}

// Complete, как настоящий клиент, прерывается отменой контекста.
func (s *stubLLM) Complete(ctx context.Context, body []byte, h map[string]string) (*llm.Response, error) {
	var req llm.Request
	if err := json.Unmarshal(body, &req); err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.reqs = append(s.reqs, req)
	s.hdrs = append(s.hdrs, h)
	s.mu.Unlock()
	type result struct {
		resp *llm.Response
		err  error
	}
	done := make(chan result, 1)
	go func() {
		resp, err := s.fn(req, h)
		done <- result{resp, err}
	}()
	select {
	case r := <-done:
		return r.resp, r.err
	case <-ctx.Done():
		return nil, &llm.Error{Kind: llm.KindCanceled, Err: ctx.Err()}
	}
}

func (s *stubLLM) Probe(ctx context.Context) (llm.ProbeResult, error) {
	s.mu.Lock()
	s.probes++
	probe := s.probe
	s.mu.Unlock()
	if probe != nil {
		if err := probe(ctx); err != nil {
			return llm.ProbeResult{}, err
		}
	}
	return llm.ProbeResult{Status: 200, Bytes: 40}, nil
}

func textResp(s string, promptTokens int) *llm.Response {
	return &llm.Response{Content: s, FinishReason: "stop", HasUsage: true,
		Usage: llm.Usage{PromptTokens: promptTokens, CompletionTokens: 3}}
}

func toolResp(name string, args map[string]any) *llm.Response {
	b, _ := json.Marshal(args)
	return &llm.Response{FinishReason: "tool_calls", HasUsage: true, Usage: llm.Usage{PromptTokens: 100},
		ToolCalls: []llm.ToolCall{{ID: "x1", Type: "function", Function: llm.FunctionCall{Name: name, Arguments: string(b)}}}}
}

type fixture struct {
	a     *Agent
	v     *clock.Virtual
	buf   *bytes.Buffer
	stub  *stubLLM
	store *userctx.Store
}

func newAgent(t *testing.T, yaml string, fn func(req llm.Request, h map[string]string) (*llm.Response, error)) *fixture {
	t.Helper()
	f := build(t, yaml, fn)
	if err := f.a.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	return f
}

// build создаёт агента без Start (чтобы подключить серверы или правила до старта).
func build(t *testing.T, yaml string, fn func(req llm.Request, h map[string]string) (*llm.Response, error)) *fixture {
	t.Helper()
	if !strings.Contains(yaml, "llm:") {
		yaml = "llm: {model: m, trace_headers: true}\n" + yaml
	}
	cfg, err := config.Parse([]byte("clock: {mode: virtual, epoch: \"2026-10-06T00:00:00Z\", start_t: 25200}\n" + yaml))
	if err != nil {
		t.Fatal(err)
	}
	v := clock.NewVirtual(cfg.EpochTime(), 7*time.Hour)
	buf := &bytes.Buffer{}
	store, err := userctx.OpenWithPolicy(":memory:", true, userctx.Policy{Kind: cfg.Policy.History,
		WindowTurns: cfg.Policy.WindowTurns, MaskKeepTurns: cfg.Policy.MaskKeepTurns}, userctx.StorageOf(cfg.UserCtx))
	if err != nil {
		t.Fatal(err)
	}
	stub := &stubLLM{fn: fn}
	a, err := New(Deps{Config: cfg, Clock: v, Trace: trace.New(buf, "t", v, 0), Store: store, LLM: stub})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { a.Close(5 * time.Second); store.Close() })
	return &fixture{a: a, v: v, buf: buf, stub: stub, store: store}
}

func TestInitialMCPUpdateCounterIsExplicitZero(t *testing.T) {
	f := newAgent(t, "policy: {wake: gate}\n", func(llm.Request, map[string]string) (*llm.Response, error) {
		return textResp("NO_ACTION", 10), nil
	})
	m, ok := f.a.Metrics()["mcp"].(map[string]int)
	if !ok || m["updates"] != 0 {
		t.Fatalf("missing initial MCP counter: %#v", f.a.Metrics()["mcp"])
	}
	if _, present := m["updates"]; !present {
		t.Fatal("initial zero must be present, not inferred from a missing field")
	}
}

func TestInventoryOnlySeedUsesDeclaredPrecision(t *testing.T) {
	f := newAgent(t, "policy: {wake: gate}\n", func(llm.Request, map[string]string) (*llm.Response, error) {
		return textResp("NO_ACTION", 10), nil
	})
	s := mcp.NewServer(&mcp.Implementation{Name: "inventory-only", Version: "test"}, nil)
	s.AddResource(&mcp.Resource{URI: "hw://inventory", Name: "inventory"},
		func(context.Context, *mcp.ReadResourceRequest) (*mcp.ReadResourceResult, error) {
			return &mcp.ReadResourceResult{Contents: []*mcp.ResourceContents{{URI: "hw://inventory",
				Text: `{"devices":[{"id":"meter","properties":[{"name":"reading","precision":10,"value":101}]}]}`}}}, nil
		})
	st, ct := mcp.NewInMemoryTransports()
	sess, err := s.Connect(context.Background(), st, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { sess.Close() })
	if err := f.a.Host().ConnectTransport(context.Background(), config.MCPServer{Name: "fixture", Transport: "stdio"}, ct); err != nil {
		t.Fatal(err)
	}
	if err := f.a.syncHardware(context.Background()); err != nil {
		t.Fatal(err)
	}
	if v, ok := f.a.hw.Get("meter", "reading"); !ok || v != 100.0 {
		t.Fatalf("inventory-only initial reading=%v, want 100", v)
	}
}

func (f *fixture) user(t *testing.T, text string) Reply {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	r, err := f.a.User(ctx, "", text)
	if err != nil {
		t.Fatal(err)
	}
	f.a.WaitIdle(5 * time.Second)
	return r
}

// requests — копия запросов к LLM.
func (f *fixture) requests() []llm.Request {
	f.a.WaitIdle(5 * time.Second)
	f.stub.mu.Lock()
	defer f.stub.mu.Unlock()
	return append([]llm.Request(nil), f.stub.reqs...)
}

// records читает трассу после затишья агента (после ответа пользователю ход ещё
// может дописывать сводку истории).
func (f *fixture) records(typ string) []map[string]any {
	f.a.WaitIdle(5 * time.Second)
	var out []map[string]any
	for _, line := range bytes.Split(f.buf.Bytes(), []byte("\n")) {
		var r map[string]any
		if json.Unmarshal(line, &r) == nil && r["type"] == typ {
			out = append(out, r)
		}
	}
	return out
}

func TestSummarySystemPolicy(t *testing.T) {
	var mu sync.Mutex
	turns := 0
	f := newAgent(t, "policy: {wake: gate, history: summary_system, window_turns: 1, summary_threshold_tokens: 500}\n",
		func(req llm.Request, h map[string]string) (*llm.Response, error) {
			if h["X-Edgectx-Purpose"] == "summary" {
				return textResp("СВОДКА: пользователь здоровался", 50), nil
			}
			mu.Lock()
			turns++
			mu.Unlock()
			return textResp("привет", 1000), nil // оплачено 1000 > порога 500
		})
	f.user(t, "первый")
	f.user(t, "второй")
	f.user(t, "третий")
	summaries := 0
	for _, r := range f.records(trace.LLMRequest) {
		if r["purpose"] == "summary" {
			summaries++
			if r["segments"].(map[string]any)["history"].(float64) <= 0 {
				t.Fatalf("summary request must account the transcript as history: %v", r["segments"])
			}
		}
	}
	if summaries < 1 {
		t.Fatal("summary was never requested")
	}
	sum, upto := f.store.Summary()
	if sum != "СВОДКА: пользователь здоровался" || upto < 1 {
		t.Fatalf("summary %q upto %d", sum, upto)
	}
	// Последний запрос хода: сводка в системном сообщении, старые ходы — нет.
	var last llm.Request
	for _, r := range f.requests() {
		if r.Messages[len(r.Messages)-1].Content == "третий" {
			last = r
		}
	}
	joined := ""
	for _, m := range last.Messages {
		joined += m.Role + ":" + m.Content + "\n"
	}
	if !strings.Contains(joined, "system:Сводка предыдущего диалога:\nСВОДКА") || strings.Contains(joined, "user:первый") {
		t.Fatalf("history with summary:\n%s", joined)
	}
}

func TestProfileConsolidation(t *testing.T) {
	step := 0
	f := newAgent(t, "policy: {wake: gate, profile_consolidate_s: 600}\nuserctx: {profile: [{id: f1, text: \"любит 22 °C\"}]}\n",
		func(req llm.Request, h map[string]string) (*llm.Response, error) {
			step++
			if step == 1 {
				return toolResp("update_profile", map[string]any{"op": "ADD", "text": "встаёт в 7:00"}), nil
			}
			return textResp("Запомнил.", 10), nil
		})
	f.user(t, "я встаю в 7")
	f.a.mu.Lock()
	p := f.a.profileText
	f.a.mu.Unlock()
	if strings.Contains(p, "7:00") {
		t.Fatal("profile segment must not change before consolidation")
	}
	f.a.Advance(clock.Seconds(f.v)+601, AdvanceOptions{})
	f.a.mu.Lock()
	p = f.a.profileText
	f.a.mu.Unlock()
	if p != "- f1: любит 22 °C\n- f2: встаёт в 7:00" {
		t.Fatalf("profile after consolidation: %q", p)
	}
	// Профиль — отдельное системное сообщение после префикса.
	f.user(t, "ещё")
	reqs := f.requests()
	last := reqs[len(reqs)-1]
	if last.Messages[1].Role != "system" || !strings.Contains(last.Messages[1].Content, "встаёт в 7:00") {
		t.Fatalf("profile message %+v", last.Messages[1])
	}
}

func TestLocalToolsAndErrors(t *testing.T) {
	script := []*llm.Response{
		toolResp("create_rule", map[string]any{"device": "door1", "property": "open", "op": "between", "action": "notify"}),
		toolResp("create_rule", map[string]any{"device": "door1", "property": "open", "op": "eq", "value": true,
			"action": "notify", "text": "открыта", "for_s": 60}),
		toolResp("recent_events", map[string]any{"k": 5}),
		toolResp("no_such_tool", nil),
		textResp("готово", 10),
	}
	i := 0
	f := newAgent(t, "policy: {wake: gate, max_steps: 6}\n", func(req llm.Request, h map[string]string) (*llm.Response, error) {
		r := script[i]
		i++
		return r, nil
	})
	f.a.Event(EventInput{Device: "door1", Property: "open", Value: false, ID: "e0"})
	f.a.Advance(clock.Seconds(f.v)+5, AdvanceOptions{})
	rep := f.user(t, "следи за дверью")
	if rep.Text != "готово" || rep.ToolCalls != 4 || rep.LLMCalls != 5 {
		t.Fatalf("reply %+v", rep)
	}
	res := f.records(trace.ToolResult)
	if len(res) != 4 || res[0]["ok"] != false || res[1]["ok"] != true || res[2]["ok"] != true || res[3]["ok"] != false {
		t.Fatalf("tool results %v", res)
	}
	if rules := f.a.gate.Rules(); len(rules) != 1 || rules[0].Source != "llm" || rules[0].ForS != 60 {
		t.Fatalf("rules %+v", rules)
	}
	stored, _ := f.store.Rules()
	if len(stored) != 1 {
		t.Fatalf("rule not persisted: %+v", stored)
	}
	// recent_events видит событие e0 из журнала L2.
	var toolMsg string
	for _, m := range f.requests()[3].Messages {
		if m.Role == "tool" {
			toolMsg = m.Content
		}
	}
	if !strings.Contains(toolMsg, `"device":"door1"`) {
		t.Fatalf("recent_events result %s", toolMsg)
	}
	calls := f.records(trace.ToolCall)
	if calls[3]["server"] != "unknown" || calls[0]["server"] != "local" {
		t.Fatalf("tool_call servers %v", calls)
	}
}

func TestLLMErrorReplyAndBudgetCharge(t *testing.T) {
	f := newAgent(t, "policy: {wake: gate}\nbudget: {daily_calls: 10}\n", func(req llm.Request, h map[string]string) (*llm.Response, error) {
		return nil, errors.New("connection refused")
	})
	rep := f.user(t, "привет")
	if rep.OK || !strings.Contains(rep.Text, "connection refused") {
		t.Fatalf("reply %+v", rep)
	}
	resp := f.records(trace.LLMResponse)
	if len(resp) != 1 || resp[0]["finish_reason"] != "error" || resp[0]["error"] == nil {
		t.Fatalf("llm_response %v", resp)
	}
	if u := f.a.sched.Usage(); u.Calls != 1 || u.InputTokens != 0 {
		t.Fatalf("usage %+v", u)
	}
}

func TestTraceHeadersAndNoop(t *testing.T) {
	f := newAgent(t, "policy: {wake: heartbeat, heartbeat_min: 30}\n", func(req llm.Request, h map[string]string) (*llm.Response, error) {
		return textResp("NO_ACTION — всё спокойно", 10), nil
	})
	f.a.Advance(hmTest("07:30"), AdvanceOptions{})
	f.requests()
	f.stub.mu.Lock()
	hdrs := append([]map[string]string(nil), f.stub.hdrs...)
	f.stub.mu.Unlock()
	if len(hdrs) != 1 || hdrs[0]["X-Edgectx-Wake-Reason"] != "heartbeat" || hdrs[0]["X-Edgectx-Input-Ids"] != "hb-1" {
		t.Fatalf("headers %v", hdrs)
	}
	if n := len(f.records(trace.Notify)); n != 0 {
		t.Fatalf("noop reply must not notify (%d)", n)
	}
	if f.a.m.get("wakes_empty") != 1 {
		t.Fatal("empty wake not counted")
	}
}

func TestEventValidationAndDuplicate(t *testing.T) {
	f := newAgent(t, "policy: {wake: gate}\n", func(req llm.Request, h map[string]string) (*llm.Response, error) {
		return textResp("NO_ACTION", 1), nil
	})
	if _, err := f.a.Event(EventInput{Device: "", Property: "x"}); err == nil {
		t.Fatal("missing device must fail")
	}
	if _, err := f.a.Event(EventInput{Kind: "weird", Device: "d", Property: "p"}); err == nil {
		t.Fatal("bad kind must fail")
	}
	f.a.Event(EventInput{ID: "a1", Device: "d", Property: "p", Value: 1.0})
	ids, _ := f.a.Event(EventInput{ID: "a2", Device: "d", Property: "p", Value: 1.0}) // то же значение
	if len(ids) != 1 || ids[0] != "a2" {
		t.Fatalf("ids %v", ids)
	}
	var reasons []string
	for _, r := range f.records(trace.Suppress) {
		if r["input_id"] == "a2" {
			reasons = append(reasons, r["reason"].(string))
		}
	}
	if len(reasons) != 1 || reasons[0] != gate.ReasonNoChange {
		t.Fatalf("duplicate event outcome %v", reasons)
	}
	sit, _ := f.a.Event(EventInput{Kind: "situation", Name: "leak", Text: "протечка"})
	if len(sit) != 1 || !strings.HasPrefix(sit[0], "sit-") {
		t.Fatalf("situation ids %v", sit)
	}
	if _, err := (&Agent{clk: clock.NewReal(time.Time{})}).Advance(1, AdvanceOptions{}); !errors.Is(err, ErrNotVirtual) {
		t.Fatalf("real clock advance: %v", err)
	}
}

func hmTest(s string) float64 {
	t, _ := time.Parse("15:04", s)
	return float64(t.Hour()*3600 + t.Minute()*60)
}

func TestHelpers(t *testing.T) {
	if !isNoop("", "NO_ACTION") || !isNoop("  no_action.", "NO_ACTION") || isNoop("Дверь открыта", "NO_ACTION") {
		t.Fatal("isNoop")
	}
	if got := truncateRunes("абвгд", 3); got != "абв…[обрезано 2 симв.]" {
		t.Fatal(got)
	}
	if !writeLike("write_property") || !writeLike("set_relay1_on") || writeLike("read_property") || writeLike("board_health") {
		t.Fatal("writeLike")
	}
	p := systemPrompt("NO_ACTION", map[string]bool{"notify_user": true})
	if strings.Contains(p, "create_rule") || !strings.Contains(p, "notify_user") || !strings.Contains(p, "NO_ACTION") {
		t.Fatalf("system prompt:\n%s", p)
	}
}

func TestMaxStepsExhausted(t *testing.T) {
	f := newAgent(t, "policy: {wake: gate, max_steps: 2}\n", func(req llm.Request, h map[string]string) (*llm.Response, error) {
		return toolResp("recent_events", map[string]any{}), nil // модель зациклилась на инструменте
	})
	rep := f.user(t, "что было?")
	if rep.LLMCalls != 2 || rep.ToolCalls != 2 || !strings.Contains(rep.Text, "max_steps = 2") {
		t.Fatalf("reply %+v", rep)
	}
	if f.a.m.get("turns_max_steps") != 1 {
		t.Fatal("metric turns_max_steps")
	}
}

func TestCloseDrainsPendingInputs(t *testing.T) {
	f := newAgent(t, "policy: {wake: gate, gate: {debounce_s: 0, coalesce_s: 60, default: change}}\n",
		func(req llm.Request, h map[string]string) (*llm.Response, error) {
			return textResp("NO_ACTION", 1), nil
		})
	ids, _ := f.a.Event(EventInput{Device: "door1", Property: "open", Value: true})
	f.a.Close(time.Second)
	var got string
	for _, r := range f.records(trace.Suppress) {
		if r["input_id"] == ids[0] {
			got = r["reason"].(string)
		}
	}
	if got != "shutdown" {
		t.Fatalf("pending input outcome %q", got)
	}
	if len(f.records(trace.RunEnd)) != 1 {
		t.Fatal("run_end missing")
	}
	if _, err := f.a.Event(EventInput{Device: "d", Property: "p", Value: 1}); !errors.Is(err, ErrClosed) {
		t.Fatalf("event after close: %v", err)
	}
}
