package server

import (
	"context"
	"log/slog"
	"sync"
	"time"
)

// rateLimitHandler пропускает записи уровня Warn и выше не чаще одного раза за период
// (по тексту сообщения), остальные уровни передаёт как есть. Нужен, чтобы SDK MCP не
// засорял журнал однотипными предупреждениями о недоставленных уведомлениях мёртвым
// HTTP-сессиям: на плате журнал пишется на SD-карту, и лишние записи — это лишний IO.
type rateLimitHandler struct {
	next   slog.Handler
	period time.Duration
	mu     *sync.Mutex
	last   map[string]time.Time
	now    func() time.Time
}

func newRateLimitHandler(next slog.Handler, period time.Duration) slog.Handler {
	return &rateLimitHandler{next: next, period: period, mu: &sync.Mutex{}, last: map[string]time.Time{}, now: time.Now}
}

func (h *rateLimitHandler) Enabled(ctx context.Context, l slog.Level) bool {
	return h.next.Enabled(ctx, l)
}

func (h *rateLimitHandler) Handle(ctx context.Context, r slog.Record) error {
	if r.Level >= slog.LevelWarn {
		h.mu.Lock()
		t := h.now()
		if prev, ok := h.last[r.Message]; ok && t.Sub(prev) < h.period {
			h.mu.Unlock()
			return nil
		}
		h.last[r.Message] = t
		h.mu.Unlock()
	}
	return h.next.Handle(ctx, r)
}

func (h *rateLimitHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	return &rateLimitHandler{next: h.next.WithAttrs(attrs), period: h.period, mu: h.mu, last: h.last, now: h.now}
}

func (h *rateLimitHandler) WithGroup(name string) slog.Handler {
	return &rateLimitHandler{next: h.next.WithGroup(name), period: h.period, mu: h.mu, last: h.last, now: h.now}
}
