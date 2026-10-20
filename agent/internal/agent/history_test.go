package agent

import (
	"strconv"
	"strings"
	"testing"

	"github.com/local/edgectx/internal/llm"
)

// Пустые фоновые ходы и окно истории (M2 обзора оптимизации, 20.10.2026): после
// тихих часов плановые проверки не вытесняют из окна диалог с пользователем.

// noopLLM отвечает NO_ACTION на фоновые ходы и «Включил.» на реплики.
func noopLLM(req llm.Request, h map[string]string) (*llm.Response, error) {
	if h["X-Edgectx-Wake-Reason"] == "user" {
		return textResp("Включил.", 100), nil
	}
	return textResp("NO_ACTION", 100), nil
}

// sawText — есть ли текст в сообщениях запроса.
func sawText(req llm.Request, text string) bool {
	for _, m := range req.Messages {
		if strings.Contains(m.Content, text) {
			return true
		}
	}
	return false
}

func TestNoopHeartbeatTurnsDoNotEvictDialog(t *testing.T) {
	for mode, wantSeen := range map[string]bool{"keep": false, "skip_heartbeat": true, "skip": true} {
		t.Run(mode, func(t *testing.T) {
			f := newAgent(t, "policy: {wake: heartbeat, heartbeat_min: 30, history: mask, window_turns: 2, "+
				"mask_keep_turns: 1, history_noop: "+mode+"}\n", noopLLM)
			f.user(t, "включи свет в прихожей")
			f.after(3 * 3600) // 6 плановых проверок без действий
			f.user(t, "а теперь выключи")
			reqs := f.requests()
			last := reqs[len(reqs)-1]
			if got := sawText(last, "включи свет в прихожей"); got != wantSeen {
				t.Fatalf("реплика в окне: %v, ждали %v", got, wantSeen)
			}
			skipped := f.a.m.get("history.skipped_noop")
			if (mode == "keep") != (skipped == 0) || f.a.m.get("wakes.heartbeat") != 6 {
				t.Fatalf("пропущено %d, проверок %d", skipped, f.a.m.get("wakes.heartbeat"))
			}
			if want := 2 + 6 - skipped; f.store.TurnCount() != want {
				t.Fatalf("в истории %d ходов, ждали %d", f.store.TurnCount(), want)
			}
		})
	}
}

// skip_heartbeat хранит пустые ходы по событиям (в них описание события), skip —
// нет; ход с действием или уведомлением хранится всегда.
func TestNoopEventTurns(t *testing.T) {
	for mode, want := range map[string]int{"skip_heartbeat": 2, "skip": 1} {
		t.Run(mode, func(t *testing.T) {
			f := newAgent(t, "policy: {wake: gate, history_noop: "+mode+
				", gate: {debounce_s: 0, coalesce_s: 0, cooldown_s: 0, default: change}}\n",
				func(req llm.Request, h map[string]string) (*llm.Response, error) {
					if sawText(req, "door2") && req.Messages[len(req.Messages)-1].Role != "tool" {
						return toolResp("notify_user", map[string]any{"text": "Открыта дверь 2"}), nil
					}
					return textResp("NO_ACTION", 100), nil
				})
			f.event(t, "e1", "door1", "open", true) // пустой ход
			f.event(t, "e2", "door2", "open", true) // уведомление
			if got := f.store.TurnCount(); got != want {
				t.Fatalf("в истории %d ходов, ждали %d", got, want)
			}
		})
	}
}

// head — сообщения промпта до сегмента состояния («Время: …»): префикс, профиль и
// история — то, что провайдер может взять из кэша.
func head(req llm.Request) []string {
	var out []string
	for _, m := range req.Messages {
		if m.Role == "user" && strings.HasPrefix(m.Content, "Время:") {
			break
		}
		b, _ := m.MarshalJSON()
		out = append(out, string(b))
	}
	return out
}

// Пакетное вытеснение на уровне промпта: между точками вытеснения начало
// промпта первого запроса хода (система, инструменты, профиль, история) —
// префикс следующего; при B = 1 после заполнения окна оно меняется каждый ход.
func TestBatchEvictionKeepsPromptPrefix(t *testing.T) {
	const turns = 14
	for b, wantMax := range map[int]int{1: turns, 3: (turns + 2) / 3} {
		f := newAgent(t, "policy: {wake: gate, history: mask, window_turns: 4, mask_keep_turns: 1, "+
			"history_batch_turns: "+strconv.Itoa(b)+"}\n",
			func(req llm.Request, h map[string]string) (*llm.Response, error) {
				if req.Messages[len(req.Messages)-1].Role == "user" {
					return toolResp("recent_events", map[string]any{}), nil
				}
				return textResp("Готово.", 100), nil
			})
		var prev []string
		breaks := 0
		for i := 0; i < turns; i++ {
			f.user(t, "вопрос "+strconv.Itoa(i))
			reqs := f.requests()
			cur := head(reqs[len(reqs)-2]) // первый запрос хода
			if i > 0 && !(len(prev) <= len(cur) && strings.Join(prev, "\n") == strings.Join(cur[:len(prev)], "\n")) {
				breaks++
			}
			prev = cur
		}
		if b == 1 && breaks < turns-5 || b == 3 && (breaks == 0 || breaks > wantMax) {
			t.Fatalf("B=%d: начало промпта сменилось %d раз из %d ходов", b, breaks, turns)
		}
	}
}
