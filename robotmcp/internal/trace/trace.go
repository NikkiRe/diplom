// Package trace — журнал robot-mcp (JSONL, запись на событие): вызовы инструментов с размером ответа и
// оценкой токенов, задачи (поездки) с трафиком rosbridge, уведомления, тревоги, соединение с rosbridge.
// Поля каждой записи: ts_wall, ts_clock, type, run_id (как трасса edgectx, IMPLEMENTATION_SPEC §6).
// Нулевой *Writer — журнал выключен (все методы — no-op).
package trace

import (
	"bufio"
	"os"
	"sync"
	"time"

	"github.com/local/robotmcp/internal/canon"
	"github.com/local/robotmcp/internal/clock"
)

// Writer пишет записи журнала.
type Writer struct {
	mu    sync.Mutex
	f     *os.File
	w     *bufio.Writer
	clk   clock.Clock
	runID string
}

// Open открывает (дописывает) файл журнала.
func Open(path string, clk clock.Clock, runID string) (*Writer, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	return &Writer{f: f, w: bufio.NewWriterSize(f, 16<<10), clk: clk, runID: runID}, nil
}

// Emit пишет запись типа typ с полями fields (каноническим JSON) и сбрасывает буфер.
func (t *Writer) Emit(typ string, fields map[string]any) {
	if t == nil {
		return
	}
	rec := make(map[string]any, len(fields)+4)
	for k, v := range fields {
		rec[k] = v
	}
	rec["type"] = typ
	rec["ts_wall"] = float64(time.Now().UnixMicro()) / 1e6
	rec["ts_clock"] = t.clk.Now()
	rec["run_id"] = t.runID
	b, err := canon.Marshal(rec)
	if err != nil {
		b, _ = canon.Marshal(map[string]any{"type": "trace_error", "error": err.Error(), "of": typ})
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.w == nil {
		return
	}
	t.w.Write(b)
	t.w.WriteByte('\n')
	t.w.Flush()
}

// Close закрывает журнал.
func (t *Writer) Close() error {
	if t == nil {
		return nil
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.w == nil {
		return nil
	}
	t.w.Flush()
	t.w = nil
	return t.f.Close()
}
