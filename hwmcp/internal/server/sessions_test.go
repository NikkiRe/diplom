package server

import (
	"context"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// Брошенные HTTP-сессии закрываются через timeout без запросов; клиент, который только слушает
// уведомления в потоке GET (как edgectx ночью), сессию не теряет.
func TestIdleHTTPSessionsAreReaped(t *testing.T) {
	f := newFixture(t, CatalogGeneric)
	const timeout = 150 * time.Millisecond
	ts := httptest.NewServer(f.srv.ReapIdleSessions(
		mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return f.srv.MCP() }, nil), timeout))
	defer ts.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	connect := func(noSSE bool, hc *http.Client) *mcp.ClientSession {
		t.Helper()
		cs, err := mcp.NewClient(&mcp.Implementation{Name: "idle", Version: "1"}, nil).Connect(ctx,
			&mcp.StreamableClientTransport{Endpoint: ts.URL, HTTPClient: hc, DisableStandaloneSSE: noSSE}, nil)
		if err != nil {
			t.Fatal(err)
		}
		return cs
	}
	live := func() map[string]bool {
		ids := map[string]bool{}
		for ss := range f.srv.MCP().Sessions() {
			ids[ss.ID()] = true
		}
		return ids
	}
	waitFor := func(what string, cond func() bool) {
		t.Helper()
		for deadline := time.Now().Add(5 * time.Second); !cond(); time.Sleep(10 * time.Millisecond) {
			if time.Now().After(deadline) {
				t.Fatalf("%s: sessions %v, stats %v", what, live(), f.srv.SessionStats())
			}
		}
	}

	listening := connect(false, nil) // как edgectx: поток SSE открыт
	defer listening.Close()
	requestsOnly := connect(true, nil)
	// «упавший» клиент: поток SSE открыт, затем сеть пропадает без DELETE
	var down atomic.Bool
	var mu sync.Mutex
	var conns []net.Conn
	crashing := connect(false, &http.Client{Transport: &http.Transport{
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			if down.Load() {
				return nil, errors.New("network is down")
			}
			c, err := (&net.Dialer{}).DialContext(ctx, network, addr)
			if err == nil {
				mu.Lock()
				conns = append(conns, c)
				mu.Unlock()
			}
			return c, err
		}}})
	defer crashing.Close()

	waitFor("idle session without a stream must be closed", func() bool { return !live()[requestsOnly.ID()] })
	time.Sleep(3 * timeout)
	if ids := live(); !ids[listening.ID()] || !ids[crashing.ID()] {
		t.Fatalf("sessions with an open SSE stream must stay: %v", ids)
	}
	if _, err := requestsOnly.CallTool(ctx, &mcp.CallToolParams{Name: "board_health"}); err == nil {
		t.Fatal("a closed session must refuse calls")
	}

	down.Store(true)
	mu.Lock()
	for _, c := range conns {
		_ = c.Close()
	}
	mu.Unlock()
	waitFor("abandoned session must be closed", func() bool { return !live()[crashing.ID()] })
	if res, err := listening.CallTool(ctx, &mcp.CallToolParams{Name: "board_health"}); err != nil || res.IsError {
		t.Fatalf("listening client must keep its session: %v %v", res, err)
	}
	if st := f.srv.SessionStats(); st["reaped"] != int64(2) {
		t.Fatalf("stats: %v", st)
	}
}

// Запрос с неизвестным или закрытым идентификатором сессии (SDK отвечает 404) учёта не заводит: память
// ограничена настоящими сессиями.
func TestUnknownSessionIDsAreNotTracked(t *testing.T) {
	f := newFixture(t, CatalogGeneric)
	ts := httptest.NewServer(f.srv.ReapIdleSessions(
		mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return f.srv.MCP() }, nil), time.Hour))
	defer ts.Close()
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "real", Version: "1"}, nil).Connect(context.Background(),
		&mcp.StreamableClientTransport{Endpoint: ts.URL, DisableStandaloneSSE: true}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()
	for _, id := range []string{"bogus-1", "bogus-2", "bogus-3"} {
		req, err := http.NewRequest(http.MethodPost, ts.URL, strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"ping"}`))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Accept", "application/json, text/event-stream")
		req.Header.Set(sessionHeader, id)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusNotFound {
			t.Fatalf("%s: status %d", id, resp.StatusCode)
		}
	}
	if st := f.srv.SessionStats(); st["tracked"] != 1 {
		t.Fatalf("only the real session must be tracked: %v", st)
	}
}

func TestSessionReaperOff(t *testing.T) {
	f := newFixture(t, CatalogGeneric)
	h := http.NewServeMux()
	if f.srv.ReapIdleSessions(h, 0) != http.Handler(h) || f.srv.SessionStats() != nil {
		t.Fatal("timeout 0 must leave the handler as is")
	}
}
