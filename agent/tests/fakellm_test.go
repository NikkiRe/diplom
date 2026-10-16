package tests

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/local/edgectx/internal/llm"
)

// llmCall — запрос, который получил фейковый LLM.
type llmCall struct {
	Reason   string // X-Edgectx-Wake-Reason
	Purpose  string // X-Edgectx-Purpose
	WakeID   string
	Inputs   string
	Messages []llm.Message
	Tools    []string
}

// last — последнее сообщение запроса.
func (c llmCall) last() llm.Message { return c.Messages[len(c.Messages)-1] }

// state — сегмент состояния (сообщение, начинающееся с «Время:»).
func (c llmCall) state() string {
	for _, m := range c.Messages {
		if m.Role == "user" && strings.HasPrefix(m.Content, "Время:") {
			return m.Content
		}
	}
	return ""
}

// system — системное сообщение префикса.
func (c llmCall) system() string { return c.Messages[0].Content }

// lastToolCall — имя инструмента в последнем ответе ассистента.
func (c llmCall) lastToolCall() string {
	for i := len(c.Messages) - 1; i >= 0; i-- {
		if m := c.Messages[i]; m.Role == "assistant" && len(m.ToolCalls) > 0 {
			return m.ToolCalls[0].Function.Name
		}
	}
	return ""
}

// reply — ответ фейкового LLM: текст или вызов инструмента.
type reply struct {
	Content string
	Tool    string
	Args    map[string]any
}

func text(s string) reply { return reply{Content: s} }

func call(name string, args map[string]any) reply { return reply{Tool: name, Args: args} }

// fakeLLM — OpenAI-совместимый сервер с поведением oracle. Пока down — на всё
// отвечает 503 (провайдер недоступен).
type fakeLLM struct {
	mu     sync.Mutex
	srv    *httptest.Server
	calls  []llmCall
	oracle func(c llmCall) reply
	down   atomic.Bool
	failed atomic.Int32 // запросов, получивших 503
	probes atomic.Int32 // GET /models
}

func newFakeLLM(t *testing.T, oracle func(c llmCall) reply) *fakeLLM {
	f := &fakeLLM{oracle: oracle}
	f.srv = httptest.NewServer(http.HandlerFunc(f.handle))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeLLM) URL() string { return f.srv.URL + "/v1" }

func (f *fakeLLM) handle(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodGet && r.URL.Path == "/v1/models" {
		f.probes.Add(1)
	}
	if f.down.Load() {
		if r.Method == http.MethodPost {
			f.failed.Add(1)
		}
		w.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	if r.Method == http.MethodGet {
		io.WriteString(w, `{"data":[{"id":"fake-model"}]}`)
		return
	}
	body, _ := io.ReadAll(r.Body)
	var req struct {
		Messages []llm.Message `json:"messages"`
		Tools    []llm.Tool    `json:"tools"`
		Stream   bool          `json:"stream"`
	}
	if err := json.Unmarshal(body, &req); err != nil || req.Stream {
		http.Error(w, "bad request", 400)
		return
	}
	c := llmCall{Reason: r.Header.Get("X-Edgectx-Wake-Reason"), Purpose: r.Header.Get("X-Edgectx-Purpose"),
		WakeID: r.Header.Get("X-Edgectx-Wake-Id"), Inputs: r.Header.Get("X-Edgectx-Input-Ids"), Messages: req.Messages}
	for _, t := range req.Tools {
		c.Tools = append(c.Tools, t.Function.Name)
	}
	f.mu.Lock()
	f.calls = append(f.calls, c)
	n := len(f.calls)
	f.mu.Unlock()

	rep := f.oracle(c)
	msg := map[string]any{"role": "assistant", "content": rep.Content}
	finish := "stop"
	if rep.Tool != "" {
		args, _ := json.Marshal(rep.Args)
		msg["content"] = nil
		msg["tool_calls"] = []any{map[string]any{"id": fmt.Sprintf("call_%d", n), "type": "function",
			"function": map[string]any{"name": rep.Tool, "arguments": string(args)}}}
		finish = "tool_calls"
	}
	// usage: грубая оценка по длине тела (детерминированно).
	resp := map[string]any{"id": fmt.Sprintf("gen-%d", n), "model": "fake-model", "service_tier": "default",
		"choices": []any{map[string]any{"index": 0, "finish_reason": finish, "message": msg}},
		"usage": map[string]any{"prompt_tokens": len(body) / 4, "completion_tokens": 7, "total_tokens": len(body)/4 + 7,
			"prompt_tokens_details": map[string]any{"cached_tokens": 0}}}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

func (f *fakeLLM) Calls() []llmCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]llmCall(nil), f.calls...)
}

// roomOracle — «идеальная модель» для сценария мини-дня.
func roomOracle(c llmCall) reply {
	last := c.last()
	if c.Purpose == "summary" {
		return text("Пользователь включал свет; есть правило про дверь.")
	}
	switch c.Reason {
	case "heartbeat":
		return text("NO_ACTION")
	case "user":
		if last.Role == "tool" {
			if c.lastToolCall() == "write_property" {
				return text("Свет включён.")
			}
			return text("Готово.")
		}
		switch {
		case strings.Contains(last.Content, "включи свет"):
			return call("write_property", map[string]any{"device": "relay1", "property": "on", "value": true})
		case strings.Contains(last.Content, "дверь"):
			return text("Дверь закрыта.")
		}
		return text("Не понял команду.")
	default: // gate | rule
		st := c.state()
		if last.Role == "tool" {
			if c.lastToolCall() == "create_rule" && strings.Contains(last.Content, `"rule_id"`) {
				return text("Дверь открыта. Буду следить: если она открыта дольше 5 минут, напомню.")
			}
			return text("NO_ACTION")
		}
		hasRule := false
		for _, m := range c.Messages {
			for _, tc := range m.ToolCalls {
				if tc.Function.Name == "create_rule" {
					hasRule = true
				}
			}
		}
		switch {
		case strings.Contains(st, "door1.open: false → true") && !hasRule:
			return call("create_rule", map[string]any{"description": "Дверь открыта дольше 5 минут",
				"device": "door1", "property": "open", "op": "eq", "value": true, "for_s": 300,
				"action": "notify", "text": "Дверь открыта дольше 5 минут"})
		case strings.Contains(st, "t1.temperature") && strings.Contains(st, "→ 31"):
			return call("notify_user", map[string]any{"text": "Перегрев в лаборатории: 31 °C"})
		}
		return text("NO_ACTION")
	}
}

// noopOracle отвечает «ничего не делать» на всё (пользователю — «ок»).
func noopOracle(c llmCall) reply {
	if c.Reason == "user" {
		return text("ок")
	}
	return text("NO_ACTION")
}
