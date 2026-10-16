package llm

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/local/edgectx/internal/backoff"
)

const okResponse = `{"id":"gen-1","model":"openai/gpt-5-mini","service_tier":"default","provider":"OpenAI",
"choices":[{"finish_reason":"tool_calls","message":{"role":"assistant","content":null,
"tool_calls":[{"id":"call_a","type":"function","function":{"name":"read_property","arguments":"{\"device\":\"t1\"}"}}]}}],
"usage":{"prompt_tokens":1200,"completion_tokens":30,"total_tokens":1230,"cost":0.0123,
"prompt_tokens_details":{"cached_tokens":1024},"completion_tokens_details":{"reasoning_tokens":12}}}`

func TestRequestFormatAndParse(t *testing.T) {
	var body []byte
	var hdr http.Header
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/chat/completions" {
			t.Errorf("path %s", r.URL.Path)
		}
		body, _ = io.ReadAll(r.Body)
		hdr = r.Header.Clone()
		io.WriteString(w, okResponse)
	}))
	defer srv.Close()

	c := New(Options{BaseURL: srv.URL + "/v1/", APIKey: "test-key"})
	req := Request{
		Model: "openai/gpt-5-mini",
		Messages: []Message{
			{Role: "system", Content: "sys"},
			{Role: "user", Content: "включи свет"},
			{Role: "assistant", ToolCalls: []ToolCall{{ID: "c1", Type: "function", Function: FunctionCall{Name: "f", Arguments: "{}"}}}},
			{Role: "tool", ToolCallID: "c1", Content: "ok"},
		},
		Tools:     []Tool{{Type: "function", Function: FunctionDef{Name: "f", Parameters: map[string]any{"type": "object"}}}},
		MaxTokens: 256,
		Extra:     map[string]any{"provider": map[string]any{"only": []any{"openai"}}, "model": "must-not-override"},
	}
	sent, err := Body(req)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := c.Complete(context.Background(), sent, map[string]string{"X-Edgectx-Req-Id": "r-1"})
	if err != nil {
		t.Fatal(err)
	}
	if hdr.Get("Authorization") != "Bearer test-key" || hdr.Get("X-Edgectx-Req-Id") != "r-1" {
		t.Fatalf("headers %v", hdr)
	}
	s := string(body)
	if !strings.HasPrefix(s, `{"max_completion_tokens":256,"messages":[`) {
		t.Fatalf("body not canonical: %s", s)
	}
	for _, want := range []string{`"stream":false`, `"tool_choice":"auto"`, `"model":"openai/gpt-5-mini"`,
		`"provider":{"only":["openai"]}`, `{"content":null,"role":"assistant","tool_calls":`, `"content":"включи свет"`} {
		if !strings.Contains(s, want) {
			t.Errorf("body lacks %s: %s", want, s)
		}
	}
	if resp.ID != "gen-1" || resp.ServiceTier != "default" || resp.Provider != "OpenAI" || resp.FinishReason != "tool_calls" {
		t.Fatalf("resp %+v", resp)
	}
	if len(resp.ToolCalls) != 1 || resp.ToolCalls[0].Function.Name != "read_property" {
		t.Fatalf("tool calls %+v", resp.ToolCalls)
	}
	u := resp.Usage
	if u.PromptTokens != 1200 || u.CompletionTokens != 30 || u.CachedTokens != 1024 || u.ReasoningTokens != 12 {
		t.Fatalf("usage %+v", u)
	}
	if resp.UsageRaw["cost"] == nil {
		t.Fatal("raw usage must be kept in full")
	}
	if string(body) != string(sent) || resp.BytesIn == 0 || resp.Attempts != 1 {
		t.Fatalf("bytes/attempts %+v", resp)
	}
}

func TestNoToolsNoAuth(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		if strings.Contains(string(b), "tools") || r.Header.Get("Authorization") != "" {
			t.Errorf("unexpected tools/auth: %s", b)
		}
		io.WriteString(w, `{"choices":[{"finish_reason":"stop","message":{"content":"NO_ACTION"}}]}`)
	}))
	defer srv.Close()
	body, _ := Body(Request{Model: "m", Messages: []Message{{Role: "user", Content: "x"}}})
	resp, err := New(Options{BaseURL: srv.URL}).Complete(context.Background(), body, nil)
	if err != nil || resp.Content != "NO_ACTION" || resp.HasUsage {
		t.Fatalf("%+v %v", resp, err)
	}
}

// tiny — минимальное тело запроса для тестов обмена.
var tiny = []byte(`{"model":"m"}`)

// fast — клиент с короткими паузами для тестов.
func fast(url string, retries int) *Client {
	return New(Options{BaseURL: url, Retries: retries, Seed: 7,
		Backoff: backoff.Policy{Base: 4 * time.Millisecond, Max: 50 * time.Millisecond}})
}

func kindOf(t *testing.T, err error) *Error {
	t.Helper()
	e, ok := AsError(err)
	if !ok {
		t.Fatalf("error %v (%T) is not *llm.Error", err, err)
	}
	return e
}

func TestRejectedNotRetried(t *testing.T) {
	var n atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n.Add(1)
		w.WriteHeader(400)
		io.WriteString(w, `{"error":"bad model"}`)
	}))
	defer srv.Close()
	_, err := fast(srv.URL, 3).Complete(context.Background(), tiny, nil)
	e := kindOf(t, err)
	if e.Kind != KindRejected || e.Status != 400 || e.Transient() || n.Load() != 1 || Retryable(err) {
		t.Fatalf("err=%+v requests=%d", e, n.Load())
	}
	if !strings.Contains(err.Error(), "HTTP 400: {\"error\":\"bad model\"}") {
		t.Fatalf("message %q", err.Error())
	}
}

func TestServerErrorsRetriedWithDeterministicBackoff(t *testing.T) {
	run := func() (*Response, int32) {
		var n atomic.Int32
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if n.Add(1) <= 2 {
				w.WriteHeader(503)
				return
			}
			io.WriteString(w, okResponse)
		}))
		defer srv.Close()
		resp, err := fast(srv.URL, 2).Complete(context.Background(), tiny, nil)
		if err != nil {
			t.Fatal(err)
		}
		return resp, n.Load()
	}
	a, n := run()
	b, _ := run()
	if a.Attempts != 3 || n != 3 || a.Waited <= 0 {
		t.Fatalf("attempts=%d requests=%d waited=%v", a.Attempts, n, a.Waited)
	}
	// Пауза 1: 2–4 мс, пауза 2: 4–8 мс; при одном зерне — одинаковые.
	if a.Waited != b.Waited || a.Waited < 6*time.Millisecond || a.Waited >= 12*time.Millisecond {
		t.Fatalf("waited %v vs %v", a.Waited, b.Waited)
	}
}

func TestRetryAfter(t *testing.T) {
	var n atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch n.Add(1) {
		case 1:
			w.Header().Set("Retry-After-Ms", "30")
			w.WriteHeader(429)
		case 2:
			w.Header().Set("Retry-After", "120") // длиннее предела паузы — ждёт агент, а не клиент
			w.WriteHeader(503)
		default:
			io.WriteString(w, okResponse)
		}
	}))
	defer srv.Close()
	_, err := fast(srv.URL, 5).Complete(context.Background(), tiny, nil)
	e := kindOf(t, err)
	if e.Kind != KindServer || e.RetryAfter != 120*time.Second || e.Attempts != 2 || !e.Transient() {
		t.Fatalf("err %+v", e)
	}
	if e.Waited < 30*time.Millisecond {
		t.Fatalf("Retry-After-Ms 30 not honoured: waited %v", e.Waited)
	}
	now := time.Date(2026, 10, 9, 12, 0, 0, 0, time.UTC)
	cases := map[string]time.Duration{"7": 7 * time.Second, "0": 0, "-3": 0, "soon": 0,
		now.Add(90 * time.Second).Format(http.TimeFormat): 90 * time.Second, now.Add(-time.Minute).Format(http.TimeFormat): 0}
	for v, want := range cases {
		if got := retryAfter(http.Header{"Retry-After": {v}}, now); got != want {
			t.Errorf("Retry-After %q = %v, want %v", v, got, want)
		}
	}
	if got := retryAfter(http.Header{"Retry-After-Ms": {"Inf"}, "Retry-After": {"2"}}, now); got != 2*time.Second {
		t.Errorf("infinite retry-after-ms must be ignored: %v", got)
	}
}

func TestNetworkErrorRetried(t *testing.T) {
	var n atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if n.Add(1) == 1 {
			// Обрыв соединения до ответа — сетевая ошибка.
			conn, _, _ := w.(http.Hijacker).Hijack()
			conn.Close()
			return
		}
		io.WriteString(w, okResponse)
	}))
	defer srv.Close()
	resp, err := fast(srv.URL, 1).Complete(context.Background(), tiny, nil)
	if err != nil || resp.Attempts != 2 {
		t.Fatalf("resp=%+v err=%v", resp, err)
	}
}

func TestConnectFailureIsTransient(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	ln.Close() // порт закрыт: соединение отклоняется
	_, err = fast("http://"+addr, 2).Complete(context.Background(), tiny, nil)
	e := kindOf(t, err)
	if e.Kind != KindNetwork || e.Attempts != 3 || !Retryable(err) || e.MaybeBilled() {
		t.Fatalf("err %+v", e)
	}
}

func TestTimeoutNotRetried(t *testing.T) {
	var n atomic.Int32
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n.Add(1)
		select {
		case <-release:
		case <-r.Context().Done():
		}
	}))
	defer srv.Close()
	defer close(release)
	start := time.Now()
	_, err := New(Options{BaseURL: srv.URL, Timeout: 50 * time.Millisecond, Retries: 2}).
		Complete(context.Background(), tiny, nil)
	e := kindOf(t, err)
	if e.Kind != KindTimeout || !e.Transient() || !e.MaybeBilled() || Retryable(err) || n.Load() != 1 ||
		time.Since(start) > 2*time.Second {
		t.Fatalf("err=%+v requests=%d", e, n.Load())
	}
}

func TestCutResponseNotRetried(t *testing.T) {
	var n atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n.Add(1)
		w.Header().Set("Content-Length", "1000")
		io.WriteString(w, `{"choices":`) // ответ начат и оборван
		w.(http.Flusher).Flush()
		conn, _, _ := w.(http.Hijacker).Hijack()
		conn.Close()
	}))
	defer srv.Close()
	_, err := fast(srv.URL, 3).Complete(context.Background(), tiny, nil)
	e := kindOf(t, err)
	if e.Kind != KindNetwork || !e.Transient() || !e.MaybeBilled() || n.Load() != 1 {
		t.Fatalf("err=%+v requests=%d", e, n.Load())
	}
}

func TestCanceledDuringBackoff(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(502) }))
	defer srv.Close()
	ctx, cancel := context.WithCancel(context.Background())
	c := New(Options{BaseURL: srv.URL, Retries: 3, Backoff: backoff.Policy{Base: time.Hour, Max: time.Hour}})
	time.AfterFunc(20*time.Millisecond, cancel)
	_, err := c.Complete(ctx, tiny, nil)
	if e := kindOf(t, err); e.Kind != KindCanceled || e.Transient() {
		t.Fatalf("err %+v", e)
	}
}

func TestProbe(t *testing.T) {
	status := 200
	var paths []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.Method+" "+r.URL.Path)
		w.WriteHeader(status)
		io.WriteString(w, `{"data":[{"id":"openai/gpt-5-mini"}]}`)
	}))
	c := New(Options{BaseURL: srv.URL + "/v1"})
	res, err := c.Probe(context.Background())
	if err != nil || res.Status != 200 || res.Bytes != 37 || paths[0] != "GET /v1/models" {
		t.Fatalf("probe %+v %v %v", res, err, paths)
	}
	status = 401 // ключ неверен, но связь есть
	if _, err := c.Probe(context.Background()); err != nil {
		t.Fatalf("401 means reachable: %v", err)
	}
	status = 503
	if _, err := c.Probe(context.Background()); kindOf(t, err).Kind != KindServer {
		t.Fatalf("503 probe: %v", err)
	}
	srv.Close()
	if _, err := c.Probe(context.Background()); kindOf(t, err).Kind != KindNetwork {
		t.Fatalf("closed server probe: %v", err)
	}
}

func TestKeepAliveSingleConnection(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		io.WriteString(w, okResponse)
	}))
	defer srv.Close()
	c := New(Options{BaseURL: srv.URL})
	for i := 0; i < 5; i++ {
		if _, err := c.Complete(context.Background(), tiny, nil); err != nil {
			t.Fatal(err)
		}
	}
	if c.Connections() != 1 {
		t.Fatalf("connections %d, want 1 (keep-alive)", c.Connections())
	}
}

func TestBadResponses(t *testing.T) {
	for _, body := range []string{`not json`, `{"choices":[]}`, `{"error":{"message":"x"},"choices":[]}`} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, body) }))
		_, err := New(Options{BaseURL: srv.URL}).Complete(context.Background(), tiny, nil)
		srv.Close()
		if err == nil {
			t.Errorf("body %q must fail", body)
		}
	}
}

func TestMessageJSONRoundTrip(t *testing.T) {
	m := Message{Role: "assistant", ToolCalls: []ToolCall{{ID: "a", Type: "function", Function: FunctionCall{Name: "n", Arguments: "{}"}}}}
	b, _ := json.Marshal(m)
	var back Message
	if err := json.Unmarshal(b, &back); err != nil || back.Content != "" || len(back.ToolCalls) != 1 {
		t.Fatalf("%s -> %+v %v", b, back, err)
	}
	if m.Chars() != 3 {
		t.Fatalf("chars %d", m.Chars())
	}
}
