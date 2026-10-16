package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestOpCanonicalAndDeterministic(t *testing.T) {
	payload := document(2)
	var buf bytes.Buffer
	sum1, matches, err := op(payload, &buf)
	if err != nil {
		t.Fatal(err)
	}
	canon := buf.String()
	if !strings.HasPrefix(canon, `{"events":[`) {
		t.Errorf("ключи не отсортированы: %.40s", canon)
	}
	if !strings.Contains(canon, "комната <1>") || !strings.Contains(canon, "датчик & исполнительное") {
		t.Error("HTML-символы или UTF-8 экранированы")
	}
	// 2 комнаты × (5+3+2+2+3+2) свойств в state
	if matches != 34 {
		t.Errorf("совпадений %d, ожидалось 34", matches)
	}
	sum2, _, _ := op(payload, &buf)
	if sum1 != sum2 {
		t.Error("хеш не повторяется")
	}
	var reparsed map[string]any
	if err := json.Unmarshal(buf.Bytes(), &reparsed); err != nil {
		t.Fatalf("каноническая форма — не JSON: %v", err)
	}
}

func TestOpRejectsBrokenJSON(t *testing.T) {
	var buf bytes.Buffer
	if _, _, err := op([]byte(`{"state":`), &buf); err == nil {
		t.Error("ошибка разбора не возвращена")
	}
}

func TestRunCountsAllWorkers(t *testing.T) {
	payload := document(4)
	res, err := run(payload, 50*time.Millisecond, 2)
	if err != nil {
		t.Fatal(err)
	}
	if res.Ops <= 0 || res.Workers != 2 || res.OpsPerSec <= 0 || res.PayloadBytes != len(payload) {
		t.Errorf("неверный итог: %+v", res)
	}
	if res.Seconds < 0.05 {
		t.Errorf("замер короче заданного: %.3f с", res.Seconds)
	}
	if len(res.SHA256) != 64 {
		t.Errorf("sha256: %q", res.SHA256)
	}
}

func TestRunReportsOpError(t *testing.T) {
	if _, err := run([]byte("не JSON"), 10*time.Millisecond, 1); err == nil {
		t.Error("ошибка операции не возвращена")
	}
}

func BenchmarkOp(b *testing.B) {
	payload := document(4)
	var buf bytes.Buffer
	for b.Loop() {
		if _, _, err := op(payload, &buf); err != nil {
			b.Fatal(err)
		}
	}
}
