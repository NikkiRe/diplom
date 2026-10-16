package prompt

import (
	"encoding/json"
	"testing"

	"github.com/local/edgectx/internal/llm"
)

func tools() []llm.Tool {
	return []llm.Tool{{Type: "function", Function: llm.FunctionDef{Name: "read_property",
		Description: "Read a device property", Parameters: map[string]any{"type": "object",
			"properties": map[string]any{"device": map[string]any{"type": "string"}}}}}}
}

func TestOrderAndAccounting(t *testing.T) {
	in := Input{
		System:  "Ты — агент.\nИнвентарь:\ndoor1: open [bool, r]",
		Tools:   tools(),
		Profile: "- f1: любит 22 °C",
		Summary: "пользователь включал свет",
		History: []llm.Message{{Role: "user", Content: "привет"}, {Role: "assistant", Content: "здравствуйте"}},
		State:   "Время: 22:14\nИзменения: door1.open=true",
		Turn:    []llm.Message{{Role: "user", Content: "закрой дверь"}},
	}
	b := Build(in)
	roles := ""
	for _, m := range b.Messages {
		roles += m.Role[:1]
	}
	if roles != "sssuauu" {
		t.Fatalf("roles %s", roles)
	}
	if b.Messages[1].Content != ProfileHeader+in.Profile || b.Messages[2].Content != SummaryHeader+in.Summary {
		t.Fatal("profile/summary placement")
	}
	if b.Messages[5].Content != in.State || b.Messages[6].Content != "закрой дверь" {
		t.Fatal("state/turn placement")
	}
	tj, _ := json.Marshal(in.Tools) // длина канонического JSON совпадает с компактным для этих данных
	if b.Chars.Prefix != len([]rune(in.System))+len([]rune(string(tj))) {
		t.Fatalf("prefix chars %d", b.Chars.Prefix)
	}
	if b.Chars.State != len([]rune(in.State)) || b.Chars.Turn != len([]rune("закрой дверь")) {
		t.Fatalf("chars %+v", b.Chars)
	}
	if b.Tokens.Prefix <= 0 || b.Tokens.History <= 0 || b.Tokens.Total() <= b.Tokens.Prefix {
		t.Fatalf("tokens %+v", b.Tokens)
	}
	m := b.Chars.Map()
	if len(m) != 5 || m[SegTurn] != b.Chars.Turn {
		t.Fatalf("map %v", m)
	}
}

func TestEmptySegmentsOmitted(t *testing.T) {
	b := Build(Input{System: "s", Turn: []llm.Message{{Role: "user", Content: "x"}}})
	if len(b.Messages) != 2 || b.Chars.Profile != 0 || b.Chars.History != 0 || b.Chars.State != 0 {
		t.Fatalf("%+v", b)
	}
}

func TestPrefixStableAcrossTails(t *testing.T) {
	a := Build(Input{System: "S", Tools: tools(), State: "t=1", Turn: []llm.Message{{Role: "user", Content: "a"}}})
	b := Build(Input{System: "S", Tools: tools(), State: "t=2", Turn: []llm.Message{{Role: "user", Content: "bb"}},
		History: []llm.Message{{Role: "user", Content: "h"}}})
	if a.Messages[0].Content != b.Messages[0].Content || a.Chars.Prefix != b.Chars.Prefix || a.Tokens.Prefix != b.Tokens.Prefix {
		t.Fatal("prefix must not depend on tail segments")
	}
}

func TestEstimateTokens(t *testing.T) {
	if EstimateTokens("") != 0 {
		t.Fatal("empty")
	}
	en := EstimateTokens("turn on the light in the kitchen") // 33 символа ≈ 8 токенов
	ru := EstimateTokens("включи свет на кухне")             // 20 символов ≈ 6 токенов
	if en < 6 || en > 10 || ru < 5 || ru > 8 {
		t.Fatalf("en=%d ru=%d", en, ru)
	}
	// Русский дороже английского на символ.
	if EstimateTokens("ааааааааааааааааааааааааааааааа") <= EstimateTokens("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa") {
		t.Fatal("cyrillic must cost more per char")
	}
	tc := llm.Message{Role: "assistant", ToolCalls: []llm.ToolCall{{Function: llm.FunctionCall{Name: "f", Arguments: `{"a":1}`}}}}
	if MessageTokens(tc) <= MessageOverheadTokens {
		t.Fatal("tool calls must count")
	}
}
