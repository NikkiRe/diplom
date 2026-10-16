// Package llm — клиент OpenAI-совместимого Chat Completions с инструментами.
//
// Свойства (спецификация §3, концепция §7.3 решение 9): одно keep-alive
// соединение (HTTP/2 для TLS, кэш TLS-сессий), без потоковой передачи, полный
// объект usage сохраняется как есть. Таймауты раздельные: установление
// соединения (TCP, TLS) и попытка запроса целиком. Повтор — только после
// сетевой ошибки до ответа, 429 и 5xx: с экспоненциальной паузой, джиттером и
// учётом Retry-After; таймаут попытки, оборванный ответ и прочие ответы не
// повторяются, чтобы не оплатить запрос дважды. Тело запроса сериализуется
// канонически (Body) один раз — им же агент измеряет запрос для трассы.
package llm

import (
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

// Message — сообщение диалога.
type Message struct {
	Role       string     `json:"role"`
	Content    string     `json:"content"`
	ToolCalls  []ToolCall `json:"tool_calls,omitempty"`
	ToolCallID string     `json:"tool_call_id,omitempty"`
}

// MarshalJSON печатает сообщение в форме API: у ответа ассистента, состоящего
// только из вызовов инструментов, content = null.
func (m Message) MarshalJSON() ([]byte, error) {
	out := map[string]any{"role": m.Role}
	if m.Content == "" && m.Role == "assistant" && len(m.ToolCalls) > 0 {
		out["content"] = nil
	} else {
		out["content"] = m.Content
	}
	if len(m.ToolCalls) > 0 {
		out["tool_calls"] = m.ToolCalls
	}
	if m.ToolCallID != "" {
		out["tool_call_id"] = m.ToolCallID
	}
	return json.Marshal(out)
}

// Chars — размер сообщения в символах (контент + имена и аргументы вызовов);
// используется для учёта сегментов промпта.
func (m Message) Chars() int {
	n := len([]rune(m.Content))
	for _, tc := range m.ToolCalls {
		n += len([]rune(tc.Function.Name)) + len([]rune(tc.Function.Arguments))
	}
	return n
}

// ToolCall — вызов инструмента, запрошенный моделью.
type ToolCall struct {
	ID       string       `json:"id"`
	Type     string       `json:"type"`
	Function FunctionCall `json:"function"`
}

// FunctionCall — имя и аргументы (JSON-строка) вызова.
type FunctionCall struct {
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
}

// Tool — описание инструмента в формате OpenAI.
type Tool struct {
	Type     string      `json:"type"`
	Function FunctionDef `json:"function"`
}

// FunctionDef — имя, описание и JSON Schema параметров.
type FunctionDef struct {
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
	Parameters  any    `json:"parameters"`
}

// Request — запрос к модели.
type Request struct {
	Model     string
	Messages  []Message
	Tools     []Tool
	MaxTokens int
	// Extra — дополнительные поля тела (не переопределяют основные).
	Extra map[string]any
}

// Usage — нормализованные счётчики токенов.
type Usage struct {
	PromptTokens     int `json:"prompt_tokens"`
	CompletionTokens int `json:"completion_tokens"`
	TotalTokens      int `json:"total_tokens"`
	CachedTokens     int `json:"cached_tokens"`
	ReasoningTokens  int `json:"reasoning_tokens"`
}

// Response — ответ модели и сведения об обмене.
type Response struct {
	ID           string
	Model        string
	ServiceTier  string
	Provider     string
	Content      string
	ToolCalls    []ToolCall
	FinishReason string
	Usage        Usage
	// UsageRaw — объект usage ровно в том виде, в каком его вернул провайдер.
	UsageRaw map[string]any
	HasUsage bool
	BytesIn  int // байт тела ответа
	Attempts int
	Waited   time.Duration // паузы между попытками
}

// Классы причин неудачного обращения (поле error_kind трассы).
const (
	KindNetwork  = "network"  // сбой соединения: DNS, TCP, TLS, обрыв
	KindTimeout  = "timeout"  // истёк таймаут попытки (запрос мог быть уже оплачен)
	KindRate     = "http_429" // провайдер ограничил частоту
	KindServer   = "http_5xx" // сбой или перегрузка провайдера
	KindRejected = "http_4xx" // запрос отклонён: ключ, модель, формат
	KindResponse = "response" // ответ получен, но непригоден: JSON, error в теле, нет choices
	KindCanceled = "canceled" // обращение отменено (остановка агента)
)

// Error — неудачное обращение к LLM.
type Error struct {
	Kind       string
	Status     int           // HTTP-код (0 — ответа не было)
	Body       string        // тело ответа с ошибкой
	RetryAfter time.Duration // пауза, которую просил провайдер (0 — не просил)
	Attempts   int
	Waited     time.Duration
	Err        error
	answered   bool // ответ начат, но оборван: запрос мог быть оплачен
}

func (e *Error) Error() string {
	if e.Status != 0 {
		b := e.Body
		if len(b) > 300 {
			b = b[:300] + "…"
		}
		return fmt.Sprintf("llm: HTTP %d: %s", e.Status, b)
	}
	return "llm: " + e.Kind + ": " + e.Err.Error()
}

func (e *Error) Unwrap() error { return e.Err }

// Transient — LLM недоступна (нет связи, таймаут, перегрузка провайдера), а не
// неверен сам запрос: такой сбой учитывает предохранитель агента.
func (e *Error) Transient() bool {
	switch e.Kind {
	case KindNetwork, KindTimeout, KindRate, KindServer:
		return true
	}
	return false
}

// MaybeBilled — запрос мог быть уже обработан и оплачен: истёк таймаут
// попытки или ответ оборван на середине.
func (e *Error) MaybeBilled() bool { return e.Kind == KindTimeout || e.answered }

// Retryable — временный сбой, при котором запрос заведомо не оплачен: его
// можно отправить снова (клиент — сразу, агент — после восстановления связи).
func (e *Error) Retryable() bool { return e.Transient() && !e.MaybeBilled() }

// AsError возвращает *Error из цепочки ошибок.
func AsError(err error) (*Error, bool) {
	var e *Error
	ok := errors.As(err, &e)
	return e, ok
}

// Retryable — можно ли отправить запрос снова (см. Error.Retryable).
func Retryable(err error) bool {
	e, ok := AsError(err)
	return ok && e.Retryable()
}
