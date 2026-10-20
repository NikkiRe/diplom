package config

import (
	"strings"
	"testing"
)

// Уровень рассуждения и verbosity проверяются против семейства модели:
// неподдержанный параметр дал бы 4xx на каждом запросе, а у GPT-6 Luna и Sol без
// none модель не может вызвать инструмент.
func TestReasoningValidation(t *testing.T) {
	cases := []struct {
		model, extra, wantErr string // wantErr "" — конфигурация верна
	}{
		{"openai/gpt-5-mini", "{reasoning_effort: low, verbosity: low}", ""},
		{"openai/gpt-5-mini", "{reasoning_effort: minimal}", ""},
		{"openai/gpt-5-mini", "{reasoning: {effort: low}}", ""},
		{"openai/gpt-5-mini", "{}", ""}, // умолчание провайдера допустимо, но не рекомендуется
		{"openai/gpt-5-mini", "{reasoning_effort: turbo}", "reasoning_effort"},
		{"openai/gpt-5-mini", "{reasoning_effort: 1}", "reasoning_effort"},
		{"openai/gpt-5-mini", "{verbosity: tiny}", "verbosity"},
		{"openai/gpt-5-mini", "{reasoning_effort: low, reasoning: {effort: low}}", "дважды"},
		{"openai/gpt-6-luna", "{reasoning_effort: none, verbosity: low}", ""},
		{"openai/gpt-6-luna@provider=openai", "{reasoning: {effort: none}}", ""},
		{"openai/gpt-6-luna", "{}", "только при none"},
		{"gpt-6-sol-pro", "{reasoning_effort: low}", "только при none"},
		{"openai/gpt-6.1-sol", "{reasoning_effort: none}", "Responses API"},
		{"openai/gpt-6-astra", "{}", "Responses API"},
		{"openai/gpt-5.6-luna", "{reasoning_effort: low}", ""},
		{"openai/gpt-4o-mini", "{}", ""},
		{"openai/gpt-4o-mini", "{reasoning_effort: low}", "не reasoning-модель"},
		{"gpt-4.1-nano", "{verbosity: low}", "не reasoning-модель"},
	}
	for _, c := range cases {
		_, err := Parse([]byte("llm: {model: " + c.model + ", extra: " + c.extra + "}\n"))
		switch {
		case c.wantErr == "" && err != nil:
			t.Errorf("%s %s: лишняя ошибка %v", c.model, c.extra, err)
		case c.wantErr != "" && (err == nil || !strings.Contains(err.Error(), c.wantErr)):
			t.Errorf("%s %s: ждали ошибку с %q, получили %v", c.model, c.extra, c.wantErr, err)
		}
	}
}

func TestHistoryBatchAndNoopValidation(t *testing.T) {
	c, err := Parse(nil)
	if err != nil || c.Policy.HistoryBatchTurns != 1 || c.Policy.HistoryNoop != "keep" {
		t.Fatalf("умолчания: B=%d noop=%q err=%v", c.Policy.HistoryBatchTurns, c.Policy.HistoryNoop, err)
	}
	for in, want := range map[string]string{
		"policy: {history_noop: drop}": "history_noop",
	} {
		if _, err := Parse([]byte(in)); err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: ждали ошибку с %q, получили %v", in, want, err)
		}
	}
	if _, err := Parse([]byte("policy: {window_turns: 8, history_batch_turns: 3, history_noop: skip_heartbeat}")); err != nil {
		t.Fatal(err)
	}
}
