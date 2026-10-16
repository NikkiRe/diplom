package server

import (
	"bytes"
	"log/slog"
	"strings"
	"testing"
	"time"
)

// Однотипные предупреждения SDK (недоставленные уведомления мёртвым сессиям) — не чаще раза за
// период по тексту сообщения; остальные уровни и другие сообщения проходят.
func TestRateLimitHandler(t *testing.T) {
	var buf bytes.Buffer
	h := newRateLimitHandler(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug}), time.Minute).(*rateLimitHandler)
	now := time.Unix(1000, 0)
	h.now = func() time.Time { return now }
	log := slog.New(h).With("session", "s1")
	for i := 0; i < 5; i++ {
		log.Warn("undelivered message")
		log.Info("routine")
	}
	log.Error("other failure")
	now = now.Add(time.Minute)
	log.Warn("undelivered message")
	out := buf.String()
	if n := strings.Count(out, "undelivered message"); n != 2 {
		t.Fatalf("warnings within the period must be dropped: %d\n%s", n, out)
	}
	if strings.Count(out, "routine") != 5 || strings.Count(out, "other failure") != 1 {
		t.Fatalf("other records must pass:\n%s", out)
	}
}
