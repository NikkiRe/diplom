// Package prompt собирает промпт из сегментов в фиксированном порядке
// (спецификация §3, концепция §7.3 решение 4):
//
//	[prefix:  system + схемы инструментов + инвентарь L0 (+ снимок L1 в режиме snapshot_head)]
//	[profile: факты профиля пользователя]
//	[history: сводка (summary_system) и история ходов]
//	[state:   дельта/снимок состояния, событие, текущее время]
//	[turn:    реплика пользователя или задача фонового хода + сообщения текущего хода]
//
// Всё изменчивое стоит в хвосте; префикс байт-в-байт совпадает между запросами,
// пока не меняются инструменты и инвентарь. Для каждого запроса считается размер
// сегментов в символах и оценка токенов.
package prompt

import (
	"math"
	"unicode"

	"github.com/local/edgectx/internal/canon"
	"github.com/local/edgectx/internal/llm"
)

// Названия сегментов (ключи поля segments в трассе).
const (
	SegPrefix  = "prefix"
	SegProfile = "profile"
	SegHistory = "history"
	SegState   = "state"
	SegTurn    = "turn"
)

// Sizes — размеры сегментов.
type Sizes struct {
	Prefix, Profile, History, State, Turn int
}

// Map — размеры как отображение для трассы.
func (s Sizes) Map() map[string]int {
	return map[string]int{SegPrefix: s.Prefix, SegProfile: s.Profile, SegHistory: s.History,
		SegState: s.State, SegTurn: s.Turn}
}

// Total — сумма сегментов.
func (s Sizes) Total() int { return s.Prefix + s.Profile + s.History + s.State + s.Turn }

// Input — содержимое сегментов.
type Input struct {
	// System — системный текст префикса (инструкция + инвентарь L0 [+ снимок]).
	System string
	Tools  []llm.Tool
	// Profile — текст профиля ("" — сегмент пуст, сообщение не добавляется).
	Profile string
	// Summary — сводка истории (политика summary_system).
	Summary string
	History []llm.Message
	// State — дельта/снимок состояния и событие.
	State string
	// Turn — сообщения текущего хода (первое — реплика или задача).
	Turn []llm.Message
}

// Built — собранный промпт.
type Built struct {
	Messages []llm.Message
	Chars    Sizes
	Tokens   Sizes // оценка токенов по сегментам
}

// Заголовки служебных сообщений.
const (
	ProfileHeader = "Профиль пользователя (факты):\n"
	SummaryHeader = "Сводка предыдущего диалога:\n"
)

// MessageOverheadTokens — служебные токены на сообщение (роль, разделители).
const MessageOverheadTokens = 4

// Build собирает сообщения в порядке сегментов и считает их размеры.
func Build(in Input) Built {
	var b Built
	add := func(seg *int, tok *int, m llm.Message) {
		b.Messages = append(b.Messages, m)
		*seg += m.Chars()
		*tok += MessageTokens(m)
	}
	add(&b.Chars.Prefix, &b.Tokens.Prefix, llm.Message{Role: "system", Content: in.System})
	if len(in.Tools) > 0 {
		tj := canon.String(in.Tools)
		b.Chars.Prefix += runeCount(tj)
		b.Tokens.Prefix += EstimateTokens(tj)
	}
	if in.Profile != "" {
		add(&b.Chars.Profile, &b.Tokens.Profile, llm.Message{Role: "system", Content: ProfileHeader + in.Profile})
	}
	if in.Summary != "" {
		add(&b.Chars.History, &b.Tokens.History, llm.Message{Role: "system", Content: SummaryHeader + in.Summary})
	}
	for _, m := range in.History {
		add(&b.Chars.History, &b.Tokens.History, m)
	}
	if in.State != "" {
		add(&b.Chars.State, &b.Tokens.State, llm.Message{Role: "user", Content: in.State})
	}
	for _, m := range in.Turn {
		add(&b.Chars.Turn, &b.Tokens.Turn, m)
	}
	return b
}

// MessageTokens — оценка токенов сообщения.
func MessageTokens(m llm.Message) int {
	n := MessageOverheadTokens + EstimateTokens(m.Content)
	for _, tc := range m.ToolCalls {
		n += 3 + EstimateTokens(tc.Function.Name) + EstimateTokens(tc.Function.Arguments)
	}
	return n
}

// Коэффициенты эвристики «символов на токен» для o200k_base, оценённые по
// research/new_topic/GAP4_artifacts/token_ru_en_output_2026-10-06.txt: русский
// текст и JSON ≈ 3.3–3.7, английский ≈ 4.3–4.9. Это оценка до калибровки по
// usage.prompt_tokens (концепция §7.3, решение 6), а не оплаченные токены.
const (
	charsPerTokenCyrillic = 3.4
	charsPerTokenOther    = 4.3
)

// EstimateTokens — эвристическая оценка числа токенов текста.
func EstimateTokens(s string) int {
	if s == "" {
		return 0
	}
	var cyr, other float64
	for _, r := range s {
		if unicode.Is(unicode.Cyrillic, r) {
			cyr++
		} else {
			other++
		}
	}
	return int(math.Ceil(cyr/charsPerTokenCyrillic + other/charsPerTokenOther))
}

func runeCount(s string) int { return len([]rune(s)) }
