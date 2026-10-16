package channel

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/local/edgectx/internal/agent"
	"github.com/local/edgectx/internal/clock"
)

type fakeAPI struct {
	mu      sync.Mutex
	events  []agent.EventInput
	adv     []float64
	virtual bool
	slow    bool
}

func (f *fakeAPI) User(ctx context.Context, id, text string) (agent.Reply, error) {
	if f.slow {
		<-ctx.Done()
		return agent.Reply{InputID: id}, ctx.Err()
	}
	if text == "offline" {
		return agent.Reply{InputID: id, Text: "отложено", Deferred: true}, nil
	}
	return agent.Reply{InputID: id, Text: "ответ: " + text, OK: true}, nil
}

func (f *fakeAPI) Event(ev agent.EventInput) ([]string, error) {
	if ev.Device == "bad" {
		return nil, errors.New("bad device")
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.events = append(f.events, ev)
	return []string{"s-" + ev.Device}, nil
}

func (f *fakeAPI) Advance(toT float64, opt agent.AdvanceOptions) (agent.AdvanceResult, error) {
	if !f.virtual {
		return agent.AdvanceResult{}, agent.ErrNotVirtual
	}
	if toT < 0 {
		return agent.AdvanceResult{}, clock.ErrBackwards
	}
	f.adv = append(f.adv, toT)
	return agent.AdvanceResult{NowT: toT, TimersFired: 2, MissingIn: opt.MinInputs}, nil
}

func (f *fakeAPI) Metrics() map[string]any {
	return map[string]any{"llm": map[string]int{"requests": 3}}
}
func (f *fakeAPI) Health() map[string]any { return map[string]any{"ok": true} }

func do(t *testing.T, srv *httptest.Server, method, path, body string) (int, map[string]any) {
	t.Helper()
	req, _ := http.NewRequest(method, srv.URL+path, strings.NewReader(body))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	var m map[string]any
	json.Unmarshal(b, &m)
	return resp.StatusCode, m
}

func TestEndpoints(t *testing.T) {
	api := &fakeAPI{virtual: true}
	shut := make(chan struct{})
	srv := httptest.NewServer(Handler(api, Options{Shutdown: func() { close(shut) }}))
	defer srv.Close()

	code, m := do(t, srv, "POST", "/v1/user", `{"id":"u17","text":"включи свет"}`)
	if code != 200 || m["input_id"] != "u17" || m["text"] != "ответ: включи свет" {
		t.Fatalf("user %d %v", code, m)
	}
	code, m = do(t, srv, "POST", "/v1/user", `{"id":"u18","text":"offline"}`)
	if code != 202 || m["deferred"] != true || m["ok"] != false {
		t.Fatalf("deferred user reply %d %v", code, m)
	}
	if code, _ := do(t, srv, "POST", "/v1/user", `{"id":"u1"}`); code != 400 {
		t.Fatalf("user without text: %d", code)
	}
	code, m = do(t, srv, "POST", "/v1/event", `{"device":"door1","property":"open","value":true}`)
	if code != 200 || m["input_ids"].([]any)[0] != "s-door1" {
		t.Fatalf("event %d %v", code, m)
	}
	code, m = do(t, srv, "POST", "/v1/event", `[{"device":"a","property":"p","value":1},{"device":"bad","property":"p"}]`)
	if code != 400 || len(m["input_ids"].([]any)) != 1 || m["index"].(float64) != 1 {
		t.Fatalf("batch %d %v", code, m)
	}
	code, m = do(t, srv, "POST", "/v1/clock/advance", `{"to_t": 3600, "min_inputs": {"sensor": 5}}`)
	if code != 200 || m["now_t"].(float64) != 3600 || m["timers_fired"].(float64) != 2 {
		t.Fatalf("advance %d %v", code, m)
	}
	if code, _ := do(t, srv, "POST", "/v1/clock/advance", `{"to_t": -1}`); code != 400 {
		t.Fatalf("backwards %d", code)
	}
	if code, _ := do(t, srv, "POST", "/v1/clock/advance", `{}`); code != 400 {
		t.Fatalf("missing to_t %d", code)
	}
	if code, _ := do(t, srv, "POST", "/v1/clock/advance", `not json`); code != 400 {
		t.Fatalf("bad json %d", code)
	}
	if code, m := do(t, srv, "GET", "/v1/metrics", ""); code != 200 || m["llm"] == nil {
		t.Fatalf("metrics %d %v", code, m)
	}
	if code, m := do(t, srv, "GET", "/v1/health", ""); code != 200 || m["ok"] != true {
		t.Fatalf("health %d %v", code, m)
	}
	if code, _ := do(t, srv, "GET", "/v1/user", ""); code != 405 {
		t.Fatalf("method not allowed: %d", code)
	}
	if code, _ := do(t, srv, "POST", "/v1/shutdown", ""); code != 202 {
		t.Fatalf("shutdown %d", code)
	}
	select {
	case <-shut:
	case <-time.After(time.Second):
		t.Fatal("shutdown callback not called")
	}
}

func TestRealClockAdvanceConflictAndUserTimeout(t *testing.T) {
	api := &fakeAPI{slow: true}
	srv := httptest.NewServer(Handler(api, Options{UserTimeout: 30 * time.Millisecond}))
	defer srv.Close()
	if code, _ := do(t, srv, "POST", "/v1/clock/advance", `{"to_t": 10}`); code != 409 {
		t.Fatalf("real clock advance: %d", code)
	}
	code, m := do(t, srv, "POST", "/v1/user", `{"id":"u2","text":"x"}`)
	if code != 504 || m["input_id"] != "u2" {
		t.Fatalf("timeout %d %v", code, m)
	}
	if code, _ := do(t, srv, "POST", "/v1/shutdown", ""); code != 404 {
		t.Fatalf("shutdown disabled: %d", code)
	}
}
