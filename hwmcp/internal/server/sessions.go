package server

import (
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// Брошенные HTTP-сессии. SDK умеет закрывать сессию без запросов (StreamableHTTPOptions.SessionTimeout),
// но активностью считает только POST: агент, который часами лишь слушает уведомления в потоке GET (ночью
// LLM не будят), потерял бы сессию и перестал получать resources/updated. Поэтому сессия считается
// брошенной, если дольше timeout у неё нет ни одного запроса в работе — ни POST, ни открытого потока SSE:
// клиент завершился, не закрыв сессию (DELETE), и его соединения оборвались. Тогда сессия закрывается,
// а с ней — её подписки и рассылка уведомлений в никуда.

const sessionHeader = "Mcp-Session-Id"

type idleSessions struct {
	next    http.Handler
	server  *Server
	timeout time.Duration

	mu     sync.Mutex
	inUse  map[string]*sessionUse
	reaped atomic.Int64
}

type sessionUse struct {
	open  int         // запросов сессии в работе (поток GET — тоже запрос)
	timer *time.Timer // закрывает сессию через timeout после последнего запроса
}

// ReapIdleSessions оборачивает обработчик streamable HTTP: сессия, у которой дольше timeout нет
// запросов в работе (в том числе открытого потока SSE), закрывается. timeout <= 0 — не закрывать.
func (s *Server) ReapIdleSessions(next http.Handler, timeout time.Duration) http.Handler {
	if timeout <= 0 {
		return next
	}
	s.sessions = &idleSessions{next: next, server: s, timeout: timeout, inUse: map[string]*sessionUse{}}
	return s.sessions
}

// SessionStats — счётчики HTTP-сессий (nil — закрытие брошенных сессий выключено или транспорт stdio).
func (s *Server) SessionStats() map[string]any {
	if s.sessions == nil {
		return nil
	}
	s.sessions.mu.Lock()
	defer s.sessions.mu.Unlock()
	return map[string]any{"tracked": len(s.sessions.inUse), "reaped": s.sessions.reaped.Load(),
		"timeout_s": s.sessions.timeout.Seconds()}
}

func (h *idleSessions) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	id := r.Header.Get(sessionHeader)
	switch {
	case id == "": // initialize: идентификатор выдаёт SDK в заголовке ответа
		h.next.ServeHTTP(w, r)
		if id = w.Header().Get(sessionHeader); id != "" {
			h.acquire(id)
			h.release(id)
		}
		return
	case h.session(id) == nil: // неизвестная или закрытая сессия: SDK ответит 404, учитывать нечего
		h.next.ServeHTTP(w, r)
		return
	}
	h.acquire(id)
	defer h.release(id)
	h.next.ServeHTTP(w, r)
}

func (h *idleSessions) acquire(id string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	u := h.inUse[id]
	if u == nil {
		u = &sessionUse{}
		h.inUse[id] = u
	}
	if u.timer != nil {
		u.timer.Stop()
	}
	u.open++
}

func (h *idleSessions) release(id string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	u := h.inUse[id]
	if u.open--; u.open > 0 {
		return
	}
	if u.timer == nil {
		u.timer = time.AfterFunc(h.timeout, func() { h.expire(id, u) })
	} else {
		u.timer.Reset(h.timeout)
	}
}

// expire закрывает сессию, если за время срабатывания таймера не начался новый запрос. Записи о
// сессиях, закрытых клиентом (DELETE), тоже уходят здесь: учёт ограничен живыми сессиями и закрытыми за
// последний timeout.
func (h *idleSessions) expire(id string, u *sessionUse) {
	h.mu.Lock()
	if h.inUse[id] != u || u.open > 0 {
		h.mu.Unlock()
		return
	}
	delete(h.inUse, id)
	h.mu.Unlock()
	if ss := h.session(id); ss != nil {
		h.reaped.Add(1)
		h.server.log.Info("idle HTTP session closed", "session", id, "idle", h.timeout)
		_ = ss.Close()
	}
}

// session — живая сессия SDK с этим идентификатором (nil — нет).
func (h *idleSessions) session(id string) *mcp.ServerSession {
	for ss := range h.server.mcp.Sessions() {
		if ss.ID() == id {
			return ss
		}
	}
	return nil
}
