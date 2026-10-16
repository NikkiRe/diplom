// Package trace — журнал hw-mcp в JSONL: решения шлюза, срабатывания аренды, уведомления.
//
// Каждая запись — одна строка канонического JSON с обязательными полями (как у трассы edgectx,
// IMPLEMENTATION_SPEC §6): ts_wall (секунды Unix), ts_clock (часы hw-mcp), type, run_id.
// Строка пишется одним вызовом write в файл, открытый с O_APPEND, поэтому строки не перемешиваются,
// а после аварийного завершения в журнале остаются все записанные решения.
package trace

import (
	"io"
	"os"
	"sync"
	"time"

	"github.com/local/hwmcp/internal/canon"
	"github.com/local/hwmcp/internal/clock"
)

// Writer — журнал. Нулевой *Writer (nil) ничего не пишет.
type Writer struct {
	mu    sync.Mutex
	w     io.Writer
	close func() error
	clk   clock.Clock
	runID string
	n     int
	err   error
}

// Open открывает (создаёт) журнал для дописывания.
func Open(path string, clk clock.Clock, runID string) (*Writer, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	return &Writer{w: f, close: f.Close, clk: clk, runID: runID}, nil
}

// New — журнал поверх произвольного io.Writer (тесты).
func New(w io.Writer, clk clock.Clock, runID string) *Writer {
	return &Writer{w: w, clk: clk, runID: runID}
}

// Emit пишет запись типа typ с полями fields (обязательные поля добавляются автоматически).
func (t *Writer) Emit(typ string, fields map[string]any) {
	if t == nil {
		return
	}
	rec := make(map[string]any, len(fields)+4)
	for k, v := range fields {
		rec[k] = v
	}
	rec["type"] = typ
	rec["run_id"] = t.runID
	rec["ts_wall"] = float64(time.Now().UnixMicro()) / 1e6
	if _, ok := rec["ts_clock"]; !ok {
		rec["ts_clock"] = t.clk.Now()
	}
	line, err := canon.Marshal(rec)
	if err != nil {
		line, _ = canon.Marshal(map[string]any{"type": "trace_error", "run_id": t.runID, "error": err.Error(),
			"ts_wall": rec["ts_wall"], "ts_clock": rec["ts_clock"], "lost_type": typ})
	}
	line = append(line, '\n')
	t.mu.Lock()
	defer t.mu.Unlock()
	if _, err := t.w.Write(line); err != nil && t.err == nil {
		t.err = err
	}
	t.n++
}

// Count — число записанных строк.
func (t *Writer) Count() int {
	if t == nil {
		return 0
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.n
}

// Err — первая ошибка записи.
func (t *Writer) Err() error {
	if t == nil {
		return nil
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.err
}

// Close закрывает файл журнала.
func (t *Writer) Close() error {
	if t == nil || t.close == nil {
		return nil
	}
	return t.close()
}
