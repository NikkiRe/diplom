package config

import (
	"fmt"
	"slices"
	"strings"
)

// Проверка уровня рассуждения (llm.extra.reasoning_effort или
// llm.extra.reasoning.effort) и verbosity против семейства модели. Правила —
// документация OpenAI «Reasoning models» и «Using the latest model» (снята
// 20.10.2026) и supported_parameters каталога RouterAI. Ошибка конфигурации
// лучше дня ходов, на каждом из которых провайдер отвечает 4xx или модель не
// может вызвать инструмент.

// ReasoningEffort — уровень рассуждения из llm.extra ("" — не задан).
func ReasoningEffort(extra map[string]any) string {
	if v, ok := extra["reasoning_effort"].(string); ok {
		return v
	}
	if r, ok := extra["reasoning"].(map[string]any); ok {
		if v, ok := r["effort"].(string); ok {
			return v
		}
	}
	return ""
}

// modelFamily — имя модели без провайдера и суффикса закрепления endpoint'а:
// "openai/gpt-6-luna@provider=openai" → "gpt-6-luna".
func modelFamily(model string) string {
	m := strings.ToLower(strings.TrimSpace(model))
	if i := strings.IndexByte(m, '@'); i >= 0 {
		m = m[:i]
	}
	if i := strings.LastIndexByte(m, '/'); i >= 0 {
		m = m[i+1:]
	}
	return m
}

func checkReasoning(model string, extra map[string]any) []string {
	var errs []string
	add := func(format string, a ...any) { errs = append(errs, fmt.Sprintf(format, a...)) }

	_, hasFlat := extra["reasoning_effort"]
	_, hasNested := extra["reasoning"]
	if hasFlat && hasNested {
		add("llm.extra: уровень рассуждения задан дважды (reasoning_effort и reasoning) — оставьте один")
	}
	if v, ok := extra["reasoning_effort"]; ok {
		if s, isStr := v.(string); !isStr || !slices.Contains(ReasoningEfforts, s) {
			add("llm.extra.reasoning_effort: %v не из %v", v, ReasoningEfforts)
		}
	}
	if r, ok := extra["reasoning"].(map[string]any); ok {
		if v, ok := r["effort"]; ok {
			if s, isStr := v.(string); !isStr || !slices.Contains(ReasoningEfforts, s) {
				add("llm.extra.reasoning.effort: %v не из %v", v, ReasoningEfforts)
			}
		}
	}
	_, hasVerbosity := extra["verbosity"]
	if v, ok := extra["verbosity"]; ok {
		if s, isStr := v.(string); !isStr || !slices.Contains(Verbosities, s) {
			add("llm.extra.verbosity: %v не из %v", v, Verbosities)
		}
	}

	effort := ReasoningEffort(extra)
	fam := modelFamily(model)
	switch {
	case strings.HasPrefix(fam, "gpt-4") || strings.HasPrefix(fam, "gpt-3.5"):
		if hasFlat || hasNested || hasVerbosity {
			add("llm.extra: %s — не reasoning-модель, reasoning_effort и verbosity провайдер отклонит (4xx на каждом запросе)", model)
		}
	case strings.HasPrefix(fam, "gpt-6-astra") || strings.HasPrefix(fam, "gpt-6.1-sol"):
		add("llm.model: у %s инструменты — только в Responses API, а клиент edgectx работает с Chat Completions", model)
	case strings.HasPrefix(fam, "gpt-6-luna") || strings.HasPrefix(fam, "gpt-6-sol"):
		if effort != "none" {
			add("llm.extra.reasoning_effort: у %s вызов функций в Chat Completions — только при none (задано %q)", model, effort)
		}
	}
	return errs
}
