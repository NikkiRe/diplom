package channel

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/local/edgectx/internal/agent"
)

type memoryAPI struct {
	fakeAPI
	edits     int
	refreshes int
	userCalls int
}

func (f *memoryAPI) User(ctx context.Context, id, text string) (agent.Reply, error) {
	f.userCalls++
	return f.fakeAPI.User(ctx, id, text)
}

func (f *memoryAPI) Context() (map[string]any, error) {
	return map[string]any{"facts": []any{}, "history_turns": 0}, nil
}
func (f *memoryAPI) MutateFact(op, id, text string) (map[string]any, error) {
	f.edits++
	return map[string]any{"operation": op, "text": text}, nil
}
func (f *memoryAPI) ClearContextHistory(confirm bool) (map[string]any, error) {
	return map[string]any{"cleared": confirm}, nil
}
func (f *memoryAPI) RefreshContextHardware(ctx context.Context) (map[string]any, error) {
	f.refreshes++
	return map[string]any{"refreshed": true}, nil
}

func TestContextStrictBodyAndReadRefresh(t *testing.T) {
	api := &memoryAPI{}
	handler := Handler(api, Options{})
	for _, tt := range []struct {
		body          string
		want          int
		unknownLength bool
	}{
		{`{"operation":"add","text":"valid"}`, 200, false},
		{`null`, 400, false},
		{`{"operation":"add","text":"x"} {}`, 400, false},
		{`{"operation":"add","text":"x"}broken`, 400, false},
		{`{"operation":"add","text":"x","execute":true}`, 400, false},
		{`{"operation":"add","text":"x"}` + strings.Repeat(" ", maxBody), 413, false},
		{`{"operation":"add","text":"x"}` + strings.Repeat(" ", maxBody), 413, true},
	} {
		req := httptest.NewRequest("POST", "http://127.0.0.1:18080/v1/context/facts", strings.NewReader(tt.body))
		req.RemoteAddr = "127.0.0.1:12345"
		req.Header.Set("Content-Type", "application/json")
		if tt.unknownLength {
			req.ContentLength = -1
		}
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		if w.Code != tt.want {
			t.Fatalf("wanted %d got %d: %s", tt.want, w.Code, w.Body)
		}
	}
	if api.edits != 1 {
		t.Fatalf("invalid bodies mutated facts: %d", api.edits)
	}
	for _, origin := range []string{"", "http://evil.test"} {
		req := httptest.NewRequest("POST", "http://127.0.0.1:18080/v1/context/hardware/refresh", strings.NewReader(`{}`))
		req.RemoteAddr = "127.0.0.1:12345"
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Origin", origin)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		want := 200
		if origin != "" {
			want = 403
		}
		if w.Code != want {
			t.Fatal(w.Code, w.Body)
		}
	}
	if api.refreshes != 1 {
		t.Fatal(api.refreshes)
	}
}
func TestMemoryAPIRejectsBrowserCrossOriginAndDispatchesJSON(t *testing.T) {
	api := &memoryAPI{}
	handler := Handler(api, Options{})
	for _, tt := range []struct {
		host, origin, content string
		want                  int
	}{
		{"127.0.0.1:18080", "", "application/json", 200},
		{"127.0.0.1:18080", "http://evil.test", "application/json", 403},
		{"evil.test:18080", "", "application/json", 403},
		{"127.0.0.1:18080", "", "text/plain", 415},
	} {
		req := httptest.NewRequest("POST", "http://127.0.0.1:18080/v1/context/facts", bytes.NewBufferString(`{"operation":"add","text":"test"}`))
		req.RemoteAddr = "127.0.0.1:12345"
		req.Host = tt.host
		req.Header.Set("Origin", tt.origin)
		req.Header.Set("Content-Type", tt.content)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		if w.Code != tt.want {
			t.Fatalf("%+v: %d %s", tt, w.Code, w.Body)
		}
	}
	if api.edits != 1 {
		t.Fatal("rejected requests reached memory mutation")
	}
	req := httptest.NewRequest(http.MethodGet, "http://localhost:18080/v1/context", nil)
	req.RemoteAddr = "127.0.0.1:12345"
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)
	if w.Code != 200 {
		t.Fatal(w.Code)
	}
}

func TestBrowserCannotWakeModelAndRemotePeerCannotReadContext(t *testing.T) {
	api := &memoryAPI{}
	handler := Handler(api, Options{})
	for _, tt := range []struct {
		host, origin string
		want         int
	}{
		{"127.0.0.1:18080", "", 200},
		{"127.0.0.1:18080", "http://evil.test", 403},
		{"127.0.0.1:18080", "null", 403},
		{"evil.test:18080", "http://evil.test:18080", 403},
		{"127.0.0.1:18080", "http://127.0.0.1:18081", 403},
		{"127.0.0.1:18080", "http://127.0.0.1:18080", 200},
	} {
		req := httptest.NewRequest("POST", "http://127.0.0.1:18080/v1/user", strings.NewReader(`{"text":"must require trusted origin"}`))
		req.RemoteAddr = "127.0.0.1:12345"
		req.Host = tt.host
		req.Header.Set("Origin", tt.origin)
		req.Header.Set("Content-Type", "text/plain")
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		if w.Code != tt.want {
			t.Fatal(tt, w.Code, w.Body)
		}
	}
	if api.userCalls != 2 {
		t.Fatal("rejected browser origin reached User", api.userCalls)
	}
	req := httptest.NewRequest("GET", "http://localhost:18080/v1/context", nil)
	req.RemoteAddr = "192.0.2.1:12345"
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)
	if w.Code != 403 {
		t.Fatal("remote peer bypassed loopback by forging Host", w.Code)
	}
}
