// Package trace пишет трассу edgectx в JSONL (спецификация §6): одна запись —
// одна строка канонического JSON.
//
// Обязательные поля каждой записи: ts_wall (Unix-время настенных часов, секунды с
// дробной частью до микросекунд), ts_clock (секунды от эпохи часов агента — та же
// шкала, что t в файле дня), type, run_id; дополнительно seq — сквозной номер
// записи (порядок записи в файл).
//
// Запись буферизована: буфер сбрасывается не позже чем через заданный период
// после первой несброшенной записи, сразу после run_start и run_end и при
// закрытии. Таймер сброса заводится только при непустом буфере — простаивающий
// агент не просыпается ради трассы. fsync не вызывается: на диск данные
// выносит ядро (writeback), при SIGKILL/OOM теряется не больше одного периода.
package trace

import (
	"bufio"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/local/edgectx/internal/canon"
	"github.com/local/edgectx/internal/clock"
)

// Типы записей (§6) и дополнительные типы edgectx.
const (
	RunStart    = "run_start"
	Input       = "input"
	Wake        = "wake"
	Suppress    = "suppress"
	LLMRequest  = "llm_request"
	LLMResponse = "llm_response"
	ToolCall    = "tool_call"
	ToolResult  = "tool_result"
	UserReply   = "user_reply"
	Notify      = "notify"
	RuleFired   = "rule_fired"
	RunEnd      = "run_end"
	// Дополнительные типы edgectx (поля — в README агента, раздел «Трасса»).
	// Error — сбой подсистемы (MCP, LLM, хранилище), не прервавший прогон.
	Error = "error"
	// LLMUnavailable, LLMProbe, LLMAvailable — предохранитель внешней LLM:
	// размыкание, пробное обращение, восстановление связи.
	LLMUnavailable = "llm_unavailable"
	LLMProbe       = "llm_probe"
	LLMAvailable   = "llm_available"
	// Deferred — пробуждение отложено до восстановления связи с LLM.
	Deferred = "deferred"
	// MCPDown, MCPUp — MCP-сервер упал и снова подключён.
	MCPDown = "mcp_down"
	MCPUp   = "mcp_up"
)

// Types перечисляет все типы записей, которые может писать edgectx.
var Types = []string{RunStart, Input, Wake, Suppress, LLMRequest, LLMResponse, ToolCall,
	ToolResult, UserReply, Notify, RuleFired, RunEnd, Error, LLMUnavailable, LLMProbe, LLMAvailable,
	Deferred, MCPDown, MCPUp}

// F — поля записи.
type F = map[string]any

// Writer — потокобезопасный писатель трассы.
type Writer struct {
	mu     sync.Mutex
	runID  string
	clk    clock.Clock
	w      *bufio.Writer
	closer io.Closer
	seq    uint64
	counts map[string]int
	err    error
	nowFn  func() time.Time // настенные часы; подменяется в тестах
	every  time.Duration    // период сброса буфера (0 — после каждой записи)
	flushT *time.Timer      // назначенный сброс непустого буфера
}

// New создаёт писатель поверх w с периодом сброса буфера flush (0 — после
// каждой записи). Если w реализует io.Closer, Close закроет его.
func New(w io.Writer, runID string, clk clock.Clock, flush time.Duration) *Writer {
	tw := &Writer{runID: runID, clk: clk, w: bufio.NewWriterSize(w, 64<<10),
		counts: map[string]int{}, nowFn: time.Now, every: flush}
	if c, ok := w.(io.Closer); ok {
		tw.closer = c
	}
	return tw
}

// Open открывает файл трассы (создаёт каталоги, дописывает в конец). Путь "" —
// трасса отключена (записи только считаются), "-" — стандартный вывод.
func Open(path, runID string, clk clock.Clock, flush time.Duration) (*Writer, error) {
	switch path {
	case "":
		return New(io.Discard, runID, clk, flush), nil
	case "-":
		return New(nopCloser{os.Stdout}, runID, clk, flush), nil
	}
	if dir := filepath.Dir(path); dir != "." {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, err
		}
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	return New(f, runID, clk, flush), nil
}

type nopCloser struct{ io.Writer }

func (nopCloser) Close() error { return nil }

// RunID — идентификатор прогона.
func (w *Writer) RunID() string { return w.runID }

// Emit пишет запись типа typ с полями fields. Обязательные поля добавляются
// автоматически и не могут быть переопределены.
func (w *Writer) Emit(typ string, fields F) {
	rec := make(map[string]any, len(fields)+5)
	for k, v := range fields {
		rec[k] = v
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	w.seq++
	rec["type"] = typ
	rec["run_id"] = w.runID
	rec["seq"] = w.seq
	rec["ts_wall"] = roundMicro(float64(w.nowFn().UnixNano()) / 1e9)
	rec["ts_clock"] = roundMicro(clock.Seconds(w.clk))
	line, err := canon.Marshal(rec)
	if err != nil {
		// Несериализуемое поле не должно терять запись: пишем её без полей.
		line = []byte(canon.String(map[string]any{"type": typ, "run_id": w.runID, "seq": w.seq,
			"ts_wall": rec["ts_wall"], "ts_clock": rec["ts_clock"], "trace_error": err.Error()}))
	}
	w.counts[typ]++
	if w.err != nil {
		return
	}
	if _, err := w.w.Write(append(line, '\n')); err != nil {
		w.err = err
		return
	}
	switch {
	case w.every <= 0 || typ == RunStart || typ == RunEnd:
		w.flushLocked()
	case w.flushT == nil:
		w.flushT = time.AfterFunc(w.every, w.flush)
	}
}

func (w *Writer) flush() {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.flushLocked()
}

func (w *Writer) flushLocked() {
	if w.flushT != nil {
		w.flushT.Stop()
		w.flushT = nil
	}
	if err := w.w.Flush(); err != nil && w.err == nil {
		w.err = err
	}
}

// Counts — число записей по типам.
func (w *Writer) Counts() map[string]int {
	w.mu.Lock()
	defer w.mu.Unlock()
	out := make(map[string]int, len(w.counts))
	for k, v := range w.counts {
		out[k] = v
	}
	return out
}

// Close сбрасывает буфер и закрывает файл. Возвращает первую ошибку записи.
func (w *Writer) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.flushLocked()
	if w.closer != nil {
		if err := w.closer.Close(); err != nil && w.err == nil {
			w.err = fmt.Errorf("trace: %w", err)
		}
		w.closer = nil
	}
	return w.err
}

func roundMicro(x float64) float64 {
	return math.Round(x*1e6) / 1e6
}
