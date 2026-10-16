package llm

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/local/edgectx/internal/backoff"
	"github.com/local/edgectx/internal/canon"
)

// Options — параметры клиента.
type Options struct {
	// BaseURL — например http://127.0.0.1:18100/v1 (к нему добавляется /chat/completions).
	BaseURL string
	// APIKey — ключ Bearer (пусто — без заголовка Authorization).
	APIKey string
	// ConnectTimeout — предел установления TCP-соединения и, отдельно, TLS-рукопожатия.
	ConnectTimeout time.Duration
	// Timeout — предел одной попытки целиком: от отправки до конца ответа.
	Timeout time.Duration
	// Retries — число повторов после сетевой ошибки до ответа, 429 и 5xx.
	Retries int
	// Backoff — пауза перед повтором. Retry-After длиннее Backoff.Max не
	// ожидается внутри обращения: обращение завершается ошибкой, а ожидание
	// берёт на себя агент (предохранитель по часам агента).
	Backoff backoff.Policy
	// Seed — зерно джиттера пауз (backoff.Seed(run_id)).
	Seed uint64
	// ProbePath — путь пробного запроса относительно BaseURL (по умолчанию models).
	ProbePath string
	// MaxConns — предел соединений к хосту (= числу запросов в полёте).
	MaxConns  int
	UserAgent string
}

// Client — клиент Chat Completions. Безопасен для параллельного использования.
type Client struct {
	opt   Options
	http  *http.Client
	conns atomic.Int64  // число установленных TCP-соединений (для метрик)
	calls atomic.Uint64 // номер обращения — вход джиттера
}

// Пределы чтения ответов: защита от неограниченного тела.
const (
	maxResponseBytes = 8 << 20
	maxProbeBytes    = 256 << 10
)

// New создаёт клиент с одним пулом keep-alive соединений.
func New(opt Options) *Client {
	if opt.ConnectTimeout <= 0 {
		opt.ConnectTimeout = 10 * time.Second
	}
	if opt.Timeout <= 0 {
		opt.Timeout = 60 * time.Second
	}
	if opt.Backoff.Base <= 0 {
		opt.Backoff = backoff.Policy{Base: 500 * time.Millisecond, Max: 8 * time.Second}
	}
	if opt.ProbePath == "" {
		opt.ProbePath = "models"
	}
	if opt.MaxConns <= 0 {
		opt.MaxConns = 1
	}
	if opt.UserAgent == "" {
		opt.UserAgent = "edgectx"
	}
	opt.BaseURL = strings.TrimRight(opt.BaseURL, "/")
	c := &Client{opt: opt}
	dialer := &net.Dialer{Timeout: opt.ConnectTimeout, KeepAlive: 30 * time.Second}
	tr := &http.Transport{
		Proxy: http.ProxyFromEnvironment,
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			conn, err := dialer.DialContext(ctx, network, addr)
			if err == nil {
				c.conns.Add(1)
			}
			return conn, err
		},
		ForceAttemptHTTP2:   true,
		MaxIdleConns:        opt.MaxConns,
		MaxIdleConnsPerHost: opt.MaxConns,
		MaxConnsPerHost:     opt.MaxConns,
		IdleConnTimeout:     15 * time.Minute,
		TLSHandshakeTimeout: opt.ConnectTimeout,
		// Возобновление TLS по билету экономит ≈4 КБ на каждом новом соединении.
		TLSClientConfig: &tls.Config{ClientSessionCache: tls.NewLRUClientSessionCache(8)},
	}
	c.http = &http.Client{Transport: tr}
	return c
}

// Connections — сколько TCP-соединений клиент установил за всё время.
func (c *Client) Connections() int64 { return c.conns.Load() }

// Body строит каноническое тело запроса.
func Body(req Request) ([]byte, error) {
	body := map[string]any{}
	for k, v := range req.Extra {
		body[k] = v
	}
	body["model"] = req.Model
	body["messages"] = req.Messages
	body["stream"] = false
	if len(req.Tools) > 0 {
		body["tools"] = req.Tools
		body["tool_choice"] = "auto"
	} else {
		delete(body, "tools")
		delete(body, "tool_choice")
	}
	if req.MaxTokens > 0 {
		body["max_completion_tokens"] = req.MaxTokens
	}
	return canon.Marshal(body)
}

// Complete отправляет тело запроса (Body) и разбирает ответ. headers —
// дополнительные заголовки (например X-Edgectx-*). Ошибка — всегда *Error.
func (c *Client) Complete(ctx context.Context, body []byte, headers map[string]string) (*Response, error) {
	call := c.calls.Add(1)
	var waited time.Duration
	for attempt := 1; ; attempt++ {
		resp, e := c.once(ctx, body, headers)
		if e == nil {
			resp.Attempts, resp.Waited = attempt, waited
			return resp, nil
		}
		e.Attempts, e.Waited = attempt, waited
		pause, again := c.retryPause(e, call, attempt)
		if !again {
			return nil, e
		}
		// Пауза — по настенным часам: это свойство сети, а не логики агента.
		tm := time.NewTimer(pause)
		select {
		case <-ctx.Done():
			tm.Stop()
			return nil, &Error{Kind: KindCanceled, Attempts: attempt, Waited: waited, Err: ctx.Err()}
		case <-tm.C:
		}
		waited += pause
	}
}

// retryPause — можно ли повторить попытку сразу и с какой паузой.
func (c *Client) retryPause(e *Error, call uint64, attempt int) (time.Duration, bool) {
	if attempt > c.opt.Retries || !e.Retryable() {
		return 0, false
	}
	if e.RetryAfter > 0 && c.opt.Backoff.Max > 0 && e.RetryAfter > c.opt.Backoff.Max {
		return 0, false
	}
	pause := c.opt.Backoff.Delay(attempt, backoff.Unit(c.opt.Seed, call<<8|uint64(attempt)))
	return max(pause, e.RetryAfter), true
}

func (c *Client) once(ctx context.Context, body []byte, headers map[string]string) (*Response, *Error) {
	actx, cancel := context.WithTimeout(ctx, c.opt.Timeout)
	defer cancel()
	hreq, err := c.request(actx, http.MethodPost, "chat/completions", bytes.NewReader(body))
	if err != nil {
		return nil, &Error{Kind: KindRejected, Err: err}
	}
	hreq.Header.Set("Content-Type", "application/json")
	for k, v := range headers {
		hreq.Header.Set(k, v)
	}
	hresp, err := c.http.Do(hreq)
	if err != nil {
		return nil, transportError(ctx, actx, err)
	}
	defer hresp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(hresp.Body, maxResponseBytes))
	if err != nil {
		// Ответ начат, но оборван: запрос уже мог быть оплачен — не повторяем.
		e := transportError(ctx, actx, err)
		e.answered = true
		return nil, e
	}
	if e := statusError(hresp, data); e != nil {
		return nil, e
	}
	resp, err := parse(data)
	if err != nil {
		return nil, &Error{Kind: KindResponse, Err: err}
	}
	resp.BytesIn = len(data)
	return resp, nil
}

func (c *Client) request(ctx context.Context, method, path string, body io.Reader) (*http.Request, error) {
	hreq, err := http.NewRequestWithContext(ctx, method, c.opt.BaseURL+"/"+path, body)
	if err != nil {
		return nil, err
	}
	hreq.Header.Set("Accept", "application/json")
	hreq.Header.Set("User-Agent", c.opt.UserAgent)
	if c.opt.APIKey != "" {
		hreq.Header.Set("Authorization", "Bearer "+c.opt.APIKey)
	}
	return hreq, nil
}

// transportError классифицирует ошибку соединения: отмена обращения, истёкший
// таймаут попытки или сетевой сбой (в том числе таймауты TCP и TLS).
func transportError(parent, attempt context.Context, err error) *Error {
	switch {
	case parent.Err() != nil:
		return &Error{Kind: KindCanceled, Err: err}
	case attempt.Err() != nil:
		return &Error{Kind: KindTimeout, Err: err}
	}
	return &Error{Kind: KindNetwork, Err: err}
}

// statusError — ошибка по коду ответа (nil для 2xx).
func statusError(resp *http.Response, body []byte) *Error {
	switch code := resp.StatusCode; {
	case code/100 == 2:
		return nil
	case code == http.StatusTooManyRequests:
		return &Error{Kind: KindRate, Status: code, Body: string(body), RetryAfter: retryAfter(resp.Header, time.Now())}
	case code >= 500:
		return &Error{Kind: KindServer, Status: code, Body: string(body), RetryAfter: retryAfter(resp.Header, time.Now())}
	default:
		return &Error{Kind: KindRejected, Status: code, Body: string(body)}
	}
}

// retryAfter — пауза из Retry-After (секунды или HTTP-дата, RFC 9110 §10.2.3)
// или retry-after-ms (OpenAI-совместимые провайдеры); 0 — заголовка нет.
func retryAfter(h http.Header, now time.Time) time.Duration {
	if ms, err := strconv.ParseFloat(h.Get("Retry-After-Ms"), 64); err == nil && ms > 0 && !math.IsInf(ms, 0) {
		return time.Duration(ms * float64(time.Millisecond))
	}
	v := h.Get("Retry-After")
	if s, err := strconv.Atoi(v); err == nil && s > 0 {
		return time.Duration(s) * time.Second
	}
	if t, err := http.ParseTime(v); err == nil && t.After(now) {
		return t.Sub(now)
	}
	return 0
}

// ProbeResult — итог пробного запроса.
type ProbeResult struct {
	Status int
	Bytes  int // байт прочитанного тела
}

// Probe — пробное обращение без токенов: GET {BaseURL}/{ProbePath}. Провайдер
// достижим при любом HTTP-ответе, кроме 429 и 5xx (401/404 тоже означают, что
// связь есть; ошибку запроса покажет ход). Тело читается не больше
// maxProbeBytes и отбрасывается. Ошибка — *Error тех же классов, что у Complete.
func (c *Client) Probe(ctx context.Context) (ProbeResult, error) {
	actx, cancel := context.WithTimeout(ctx, c.opt.Timeout)
	defer cancel()
	hreq, err := c.request(actx, http.MethodGet, c.opt.ProbePath, nil)
	if err != nil {
		return ProbeResult{}, &Error{Kind: KindRejected, Err: err}
	}
	hresp, err := c.http.Do(hreq)
	if err != nil {
		return ProbeResult{}, transportError(ctx, actx, err)
	}
	defer hresp.Body.Close()
	n, err := io.Copy(io.Discard, io.LimitReader(hresp.Body, maxProbeBytes))
	res := ProbeResult{Status: hresp.StatusCode, Bytes: int(n)}
	if e := statusError(hresp, nil); e != nil && e.Transient() {
		return res, e
	}
	if err != nil {
		return res, transportError(ctx, actx, err)
	}
	return res, nil
}

// wire-структуры ответа.
type wireResponse struct {
	ID          string          `json:"id"`
	Model       string          `json:"model"`
	ServiceTier string          `json:"service_tier"`
	Provider    string          `json:"provider"`
	Choices     []wireChoice    `json:"choices"`
	Usage       json.RawMessage `json:"usage"`
	Error       json.RawMessage `json:"error"`
}

type wireChoice struct {
	FinishReason string `json:"finish_reason"`
	Message      struct {
		Content   *string    `json:"content"`
		ToolCalls []ToolCall `json:"tool_calls"`
	} `json:"message"`
}

func parse(data []byte) (*Response, error) {
	var w wireResponse
	if err := json.Unmarshal(data, &w); err != nil {
		return nil, fmt.Errorf("llm: bad response JSON: %w", err)
	}
	if len(w.Error) > 0 && string(w.Error) != "null" {
		return nil, fmt.Errorf("llm: provider error: %s", w.Error)
	}
	if len(w.Choices) == 0 {
		return nil, errors.New("llm: response without choices")
	}
	ch := w.Choices[0]
	r := &Response{ID: w.ID, Model: w.Model, ServiceTier: w.ServiceTier, Provider: w.Provider,
		FinishReason: ch.FinishReason, ToolCalls: ch.Message.ToolCalls}
	if ch.Message.Content != nil {
		r.Content = *ch.Message.Content
	}
	for i := range r.ToolCalls {
		if r.ToolCalls[i].Type == "" {
			r.ToolCalls[i].Type = "function"
		}
		if r.ToolCalls[i].ID == "" {
			r.ToolCalls[i].ID = fmt.Sprintf("call_%d", i)
		}
	}
	if len(w.Usage) > 0 && string(w.Usage) != "null" {
		dec := json.NewDecoder(bytes.NewReader(w.Usage))
		dec.UseNumber()
		var raw map[string]any
		if err := dec.Decode(&raw); err == nil {
			r.UsageRaw, r.HasUsage = raw, true
			r.Usage = normalizeUsage(raw)
		}
	}
	return r, nil
}

// normalizeUsage достаёт счётчики из usage в формате OpenAI (и близких).
func normalizeUsage(raw map[string]any) Usage {
	num := func(m map[string]any, k string) int {
		if m == nil {
			return 0
		}
		switch v := m[k].(type) {
		case json.Number:
			n, _ := v.Int64()
			if n == 0 {
				f, _ := v.Float64()
				n = int64(f)
			}
			return int(n)
		case float64:
			return int(v)
		}
		return 0
	}
	sub := func(k string) map[string]any {
		m, _ := raw[k].(map[string]any)
		return m
	}
	u := Usage{
		PromptTokens:     num(raw, "prompt_tokens"),
		CompletionTokens: num(raw, "completion_tokens"),
		TotalTokens:      num(raw, "total_tokens"),
		CachedTokens:     num(sub("prompt_tokens_details"), "cached_tokens"),
		ReasoningTokens:  num(sub("completion_tokens_details"), "reasoning_tokens"),
	}
	if u.CachedTokens == 0 {
		u.CachedTokens = num(raw, "cached_tokens") // некоторые прокси кладут поле наверх
	}
	return u
}
