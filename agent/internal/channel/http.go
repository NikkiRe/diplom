// Package channel — локальный HTTP-канал агента (спецификация §3, §5.1):
//
//	POST /v1/user           {id?, text}                → ответ агента (ждёт окончания хода; 202 — модель
//	                                                     недоступна, вопрос отложен до восстановления связи)
//	POST /v1/event          {id?, kind?, device, property, value, name?, text?} или массив таких
//	POST /v1/clock/advance  {to_t, min_inputs?, timeout_ms?} (только виртуальные часы)
//	GET  /v1/metrics        счётчики
//	GET  /v1/health         состояние
//	POST /v1/shutdown       корректная остановка (запись run_end)
package channel

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/local/edgectx/internal/agent"
	"github.com/local/edgectx/internal/clock"
)

// API — то, что канал требует от агента.
type API interface {
	User(ctx context.Context, id, text string) (agent.Reply, error)
	Event(ev agent.EventInput) ([]string, error)
	Advance(toT float64, opt agent.AdvanceOptions) (agent.AdvanceResult, error)
	Metrics() map[string]any
	Health() map[string]any
}

// Options — параметры канала.
type Options struct {
	// UserTimeout — сколько ждать ответа на реплику (настенное время).
	UserTimeout time.Duration
	// Shutdown вызывается по POST /v1/shutdown (nil — эндпойнт отключён).
	Shutdown func()
}

const maxBody = 1 << 20

// Handler возвращает обработчик HTTP-канала.
func Handler(api API, opt Options) http.Handler {
	if opt.UserTimeout <= 0 {
		opt.UserTimeout = 5 * time.Minute
	}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/user", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			ID   string `json:"id"`
			Text string `json:"text"`
		}
		if !decode(w, r, &req) {
			return
		}
		if req.Text == "" {
			writeErr(w, http.StatusBadRequest, errors.New("text обязателен"))
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), opt.UserTimeout)
		defer cancel()
		rep, err := api.User(ctx, req.ID, req.Text)
		switch {
		case errors.Is(err, context.DeadlineExceeded):
			writeJSON(w, http.StatusGatewayTimeout, map[string]any{"input_id": rep.InputID, "error": "timeout"})
		case errors.Is(err, agent.ErrClosed):
			writeErr(w, http.StatusServiceUnavailable, err)
		case err != nil:
			writeErr(w, http.StatusInternalServerError, err)
		case rep.Deferred:
			writeJSON(w, http.StatusAccepted, rep)
		default:
			writeJSON(w, http.StatusOK, rep)
		}
	})
	mux.HandleFunc("POST /v1/event", func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(io.LimitReader(r.Body, maxBody))
		if err != nil {
			writeErr(w, http.StatusBadRequest, err)
			return
		}
		var batch []agent.EventInput
		if len(body) > 0 && body[0] == '[' {
			err = json.Unmarshal(body, &batch)
		} else {
			var one agent.EventInput
			err = json.Unmarshal(body, &one)
			batch = []agent.EventInput{one}
		}
		if err != nil {
			writeErr(w, http.StatusBadRequest, fmt.Errorf("bad JSON: %w", err))
			return
		}
		ids := []string{}
		for i, ev := range batch {
			got, err := api.Event(ev)
			if err != nil {
				code := http.StatusBadRequest
				if errors.Is(err, agent.ErrClosed) {
					code = http.StatusServiceUnavailable
				}
				writeJSON(w, code, map[string]any{"error": err.Error(), "index": i, "input_ids": ids})
				return
			}
			ids = append(ids, got...)
		}
		writeJSON(w, http.StatusOK, map[string]any{"input_ids": ids})
	})
	mux.HandleFunc("POST /v1/clock/advance", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			ToT       *float64       `json:"to_t"`
			MinInputs map[string]int `json:"min_inputs"`
			TimeoutMs float64        `json:"timeout_ms"`
		}
		if !decode(w, r, &req) {
			return
		}
		if req.ToT == nil {
			writeErr(w, http.StatusBadRequest, errors.New("to_t обязателен"))
			return
		}
		res, err := api.Advance(*req.ToT, agent.AdvanceOptions{MinInputs: req.MinInputs,
			Timeout: time.Duration(req.TimeoutMs * float64(time.Millisecond))})
		switch {
		case errors.Is(err, agent.ErrNotVirtual):
			writeErr(w, http.StatusConflict, err)
		case errors.Is(err, clock.ErrBackwards):
			writeErr(w, http.StatusBadRequest, err)
		case errors.Is(err, agent.ErrClosed):
			writeErr(w, http.StatusServiceUnavailable, err)
		case err != nil:
			writeErr(w, http.StatusInternalServerError, err)
		default:
			writeJSON(w, http.StatusOK, res)
		}
	})
	mux.HandleFunc("GET /v1/metrics", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, api.Metrics())
	})
	mux.HandleFunc("GET /v1/health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, api.Health())
	})
	// Optional memory API preserves existing channel adapters.
	if memory, ok := api.(interface {
		Context() (map[string]any, error)
		MutateFact(string, string, string) (map[string]any, error)
		ClearContextHistory(bool) (map[string]any, error)
	}); ok {
		memoryReply := func(w http.ResponseWriter, doc map[string]any, err error) {
			if err != nil {
				code := http.StatusBadRequest
				if errors.Is(err, agent.ErrContextBusy) {
					code = http.StatusConflict
				}
				if errors.Is(err, agent.ErrClosed) {
					code = http.StatusServiceUnavailable
				}
				writeErr(w, code, err)
				return
			}
			writeJSON(w, http.StatusOK, doc)
		}
		mux.HandleFunc("GET /v1/context", func(w http.ResponseWriter, r *http.Request) {
			if !memoryAllowed(w, r, false) {
				return
			}
			doc, err := memory.Context()
			memoryReply(w, doc, err)
		})
		mux.HandleFunc("POST /v1/context/facts", func(w http.ResponseWriter, r *http.Request) {
			if !memoryAllowed(w, r, true) {
				return
			}
			var req struct {
				Operation string `json:"operation"`
				ID        string `json:"id"`
				Text      string `json:"text"`
			}
			if !decodeContext(w, r, &req) {
				return
			}
			doc, err := memory.MutateFact(req.Operation, req.ID, req.Text)
			memoryReply(w, doc, err)
		})
		mux.HandleFunc("POST /v1/context/history/clear", func(w http.ResponseWriter, r *http.Request) {
			if !memoryAllowed(w, r, true) {
				return
			}
			var req struct {
				Confirm bool `json:"confirm"`
			}
			if !decodeContext(w, r, &req) {
				return
			}
			doc, err := memory.ClearContextHistory(req.Confirm)
			memoryReply(w, doc, err)
		})
		if refresher, ok := api.(interface {
			RefreshContextHardware(context.Context) (map[string]any, error)
		}); ok {
			mux.HandleFunc("POST /v1/context/hardware/refresh", func(w http.ResponseWriter, r *http.Request) {
				if !memoryAllowed(w, r, true) {
					return
				}
				var req struct{}
				if !decodeContext(w, r, &req) {
					return
				}
				ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
				defer cancel()
				doc, err := refresher.RefreshContextHardware(ctx)
				memoryReply(w, doc, err)
			})
		}
	}
	mux.HandleFunc("POST /v1/shutdown", func(w http.ResponseWriter, r *http.Request) {
		if opt.Shutdown == nil {
			writeErr(w, http.StatusNotFound, errors.New("shutdown disabled"))
			return
		}
		writeJSON(w, http.StatusAccepted, map[string]any{"ok": true})
		go opt.Shutdown()
	})
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		// A browser can submit a valid JSON text/plain form without CORS. Such
		// a request must never wake a paid model or manipulate the local agent.
		// Origin-less CLI/replayer clients keep their existing transport contract.
		if r.Method == http.MethodPost && r.Header.Get("Origin") != "" && !memoryAllowed(w, r, false) {
			return
		}
		mux.ServeHTTP(w, r)
	})
}

// Context management is local or tunneled; browser cross-origin requests and
// rebinding must not mutate an agent's persistent memory.
func memoryAllowed(w http.ResponseWriter, r *http.Request, write bool) bool {
	peer, _, err := net.SplitHostPort(r.RemoteAddr)
	ipPeer := net.ParseIP(peer)
	if err != nil || ipPeer == nil || !ipPeer.IsLoopback() {
		writeErr(w, http.StatusForbidden, errors.New("context API requires a loopback connection or SSH tunnel"))
		return false
	}
	host := r.Host
	if h, _, err := net.SplitHostPort(host); err == nil {
		host = h
	}
	host = strings.Trim(host, "[]")
	ip := net.ParseIP(host)
	if host != "localhost" && (ip == nil || !ip.IsLoopback()) {
		writeErr(w, http.StatusForbidden, errors.New("context API requires a loopback Host"))
		return false
	}
	if origin := r.Header.Get("Origin"); origin != "" {
		u, err := url.Parse(origin)
		if err != nil || u.Scheme != "http" || u.Host != r.Host {
			writeErr(w, http.StatusForbidden, errors.New("foreign context API origin"))
			return false
		}
	}
	if write {
		kind, _, _ := mime.ParseMediaType(r.Header.Get("Content-Type"))
		if kind != "application/json" {
			writeErr(w, http.StatusUnsupportedMediaType, errors.New("application/json required"))
			return false
		}
	}
	return true
}

func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	dec := json.NewDecoder(io.LimitReader(r.Body, maxBody))
	if err := dec.Decode(v); err != nil {
		writeErr(w, http.StatusBadRequest, fmt.Errorf("bad JSON: %w", err))
		return false
	}
	return true
}

// A context mutation must be exactly one bounded JSON object. Decoding only a
// prefix would accept trailing garbage and bypass the intended request limit.
func decodeContext(w http.ResponseWriter, r *http.Request, v any) bool {
	if r.ContentLength > maxBody {
		writeErr(w, http.StatusRequestEntityTooLarge, errors.New("context request exceeds 1 MiB"))
		return false
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBody+1))
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return false
	}
	if len(body) > maxBody {
		writeErr(w, http.StatusRequestEntityTooLarge, errors.New("context request exceeds 1 MiB"))
		return false
	}
	trimmed := strings.TrimSpace(string(body))
	if !strings.HasPrefix(trimmed, "{") {
		writeErr(w, http.StatusBadRequest, errors.New("context request must be a JSON object"))
		return false
	}
	dec := json.NewDecoder(strings.NewReader(trimmed))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		writeErr(w, http.StatusBadRequest, fmt.Errorf("bad context JSON: %w", err))
		return false
	}
	var extra any
	if err := dec.Decode(&extra); err != io.EOF {
		writeErr(w, http.StatusBadRequest, errors.New("context request must contain exactly one JSON object"))
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	enc.Encode(v)
}

func writeErr(w http.ResponseWriter, code int, err error) {
	writeJSON(w, code, map[string]any{"error": err.Error()})
}

// NewServer — HTTP-сервер канала с разумными таймаутами (без WriteTimeout:
// ответ на реплику может ждать LLM).
func NewServer(addr string, h http.Handler) *http.Server {
	return &http.Server{Addr: addr, Handler: h, ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 2 * time.Minute}
}
