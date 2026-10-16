package agent

import (
	"fmt"
	"strings"
	"time"

	"github.com/local/edgectx/internal/canon"
	"github.com/local/edgectx/internal/gate"
)

// Тексты промпта по умолчанию. Системный промпт — на русском (язык
// пользователя); описания встроенных инструментов — короткие английские (так
// дешевле в токенах, см. GAP4: вариант «система и инструменты en»).

const defaultEventTask = "Произошли события выше. Оцени, нужно ли действие или уведомление пользователя. " +
	"Если ничего не нужно — ответь ровно %s."

const defaultHeartbeatTask = "Плановая проверка помещения: оцени состояние устройств и недавние события. " +
	"Если нужно действие или уведомление — выполни его, иначе ответь ровно %s."

// Ответы пользователю, пока модель недоступна. Канала доставки поздних
// ответов нет (они видны в трассе), поэтому ответ не обещается.
const (
	deferredReplyText = "Нет связи с языковой моделью. Вопрос сохранён и будет обработан после восстановления связи."
	deferredLostText  = "Вопрос не обработан: связь с языковой моделью не восстановилась (%s)."
)

const deferredUserTask = "Ответь пользователю одним сообщением на все его сообщения из списка выше. " +
	"По событиям при необходимости выполни действия или уведомления."

// systemPrompt строит системный промпт с учётом включённых встроенных инструментов.
func systemPrompt(noop string, local map[string]bool) string {
	lines := []string{
		"Ты — EdgeCtx, ИИ-агент одноплатного компьютера в помещении. Устройствами управляешь только через инструменты, " +
			"о состоянии судишь по контексту и результатам инструментов, значения не выдумываешь.",
		"- Отвечай пользователю кратко, по-русски.",
		"- Команды приводам проверяет шлюз; если вызов отклонён (denied), сообщи причину и не пытайся обойти отказ.",
		"- Если есть инструмент answer, ответ на вопрос давай через него; невыполнимую или опасную команду отклоняй через refuse.",
	}
	if local["create_rule"] {
		lines = append(lines, "- Если пользователь просит следить за условием или напоминать, создай правило create_rule: дальше его исполняет агент без тебя.")
	}
	if local["update_profile"] {
		lines = append(lines, "- Устойчивые предпочтения пользователя сохраняй через update_profile.")
	}
	if local["recent_events"] {
		lines = append(lines, "- Прошлые события датчиков — инструмент recent_events.")
	}
	bg := fmt.Sprintf("- На фоновом ходе (событие или плановая проверка): если не нужны ни действие, ни уведомление, ответь ровно %s", noop)
	if local["notify_user"] {
		bg += "; предупредить пользователя — notify_user"
	}
	lines = append(lines, bg+".")
	return strings.Join(lines, "\n")
}

var weekdaysRU = [...]string{"вс", "пн", "вт", "ср", "чт", "пт", "сб"}

// timeLine — строка текущего времени для хвоста промпта.
func timeLine(t time.Time) string {
	return fmt.Sprintf("Время: %s (%s)", t.Format("2006-01-02 15:04:05"), weekdaysRU[t.Weekday()])
}

func hhmmss(t time.Time) string { return t.Format("15:04:05") }

// eventLine — описание события гейта для промпта и истории.
func (a *Agent) eventLine(e gate.Event) string {
	at := hhmmss(a.clockAt(e.T))
	switch {
	case e.Kind == "situation":
		s := fmt.Sprintf("%s ситуация %s", at, nonEmpty(e.Name, e.InputID))
		if e.Text != "" {
			s += ": " + e.Text
		}
		return s
	case e.Derived == "episode_end":
		n, _ := e.Extra["events"].(int)
		dur, _ := e.Extra["duration_s"].(float64)
		start, _ := e.Extra["start_t"].(float64)
		return fmt.Sprintf("%s конец эпизода %s.%s: %d срабатываний за %s (с %s)", at, e.Device, e.Prop, n,
			humanDuration(dur), hhmm(a.clockAt(start)))
	default:
		cur := a.hw.FormatValue(e.Device, e.Prop, e.Value)
		if e.Old != nil {
			return fmt.Sprintf("%s %s.%s: %s → %s", at, e.Device, e.Prop, canon.Value(e.Old), cur)
		}
		return fmt.Sprintf("%s %s.%s = %s", at, e.Device, e.Prop, cur)
	}
}

func hhmm(t time.Time) string { return t.Format("15:04") }

// deferredText — отложенное без связи с моделью (по времени) для хвоста
// промпта слитого хода.
func (a *Agent) deferredText(w *wake) string {
	return "Пока связи с моделью не было, произошло:\n" + strings.Join(a.deferredLines(w), "\n")
}

func (a *Agent) deferredLines(w *wake) []string {
	var lines []string
	for _, p := range w.parts {
		at := hhmmss(a.clockAt(p.submitT))
		switch p.reason {
		case "user":
			lines = append(lines, "- "+at+" сообщение пользователя: "+oneLine(p.userText))
		case "heartbeat":
			lines = append(lines, "- "+at+" плановая проверка")
		case "rule":
			for _, e := range p.events {
				line := "- " + a.eventLine(e) + " (правило " + p.rule.ID + ": " + p.rule.Summary()
				if p.rule.Text != "" {
					line += "; задача: " + oneLine(p.rule.Text)
				}
				lines = append(lines, line+")")
			}
		default:
			for _, e := range p.events {
				lines = append(lines, "- "+a.eventLine(e))
			}
		}
	}
	return lines
}

// deferredTask — задача слитого хода.
func (a *Agent) deferredTask(w *wake) string {
	if len(w.users()) > 0 {
		return deferredUserTask
	}
	if t := a.cfg.Policy.EventTask; t != "" {
		return t
	}
	return fmt.Sprintf(defaultEventTask, a.cfg.Policy.NoopReply)
}

func humanDuration(sec float64) string {
	switch {
	case sec < 60:
		return fmt.Sprintf("%.0f с", sec)
	case sec < 3600:
		return fmt.Sprintf("%.0f мин", sec/60)
	default:
		return fmt.Sprintf("%.1f ч", sec/3600)
	}
}

func nonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

// isNoop — ответ фонового хода «ничего не делать» (пустой или начинается с
// маркера noop_reply, без учёта регистра).
func isNoop(content, noop string) bool {
	c := strings.TrimSpace(content)
	if c == "" {
		return true
	}
	return noop != "" && strings.HasPrefix(strings.ToUpper(c), strings.ToUpper(noop))
}

// truncateRunes обрезает текст до n символов с пометкой.
func truncateRunes(s string, n int) string {
	r := []rune(s)
	if n <= 0 || len(r) <= n {
		return s
	}
	return string(r[:n]) + fmt.Sprintf("…[обрезано %d симв.]", len(r)-n)
}
