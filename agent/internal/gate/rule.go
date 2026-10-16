package gate

import (
	"fmt"
	"os"
	"path"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// Rule — локальное правило-договорённость: условие по свойству устройства →
// действие без LLM. Правила создаёт LLM инструментом create_rule (один раз),
// дальше их исполняет гейт. Срабатывание — по фронту: правило срабатывает, когда
// условие становится истинным (и, если задано for_s, остаётся истинным for_s
// секунд), и перевзводится, когда условие снова ложно.
type Rule struct {
	ID          string `json:"id" yaml:"id"`
	Description string `json:"description,omitempty" yaml:"description"`
	// Device, Property — устройство и свойство (допускаются шаблоны path.Match).
	Device   string `json:"device" yaml:"device"`
	Property string `json:"property" yaml:"property"`
	// Op — eq | ne | gt | ge | lt | le | changed.
	Op    string `json:"op" yaml:"op"`
	Value any    `json:"value,omitempty" yaml:"value"`
	// ForS — сколько секунд условие должно держаться до срабатывания.
	ForS float64 `json:"for_s,omitempty" yaml:"for_s"`
	// After, Before — окно времени суток "HH:MM" (возможен переход через полночь).
	After  string `json:"after,omitempty" yaml:"after"`
	Before string `json:"before,omitempty" yaml:"before"`
	// Action — notify (уведомить пользователя текстом Text), tool (вызвать
	// инструмент MCP Tool с аргументами Args), wake (разбудить LLM).
	Action    string         `json:"action" yaml:"action"`
	Text      string         `json:"text,omitempty" yaml:"text"`
	Tool      string         `json:"tool,omitempty" yaml:"tool"`
	Args      map[string]any `json:"args,omitempty" yaml:"args"`
	CooldownS float64        `json:"cooldown_s,omitempty" yaml:"cooldown_s"`
	Once      bool           `json:"once,omitempty" yaml:"once"`
	// Priority — приоритет пробуждения для action=wake: safety | background.
	Priority string `json:"priority,omitempty" yaml:"priority"`
	// Source — откуда правило: llm | file.
	Source string `json:"source,omitempty" yaml:"source"`
}

// Ops и Actions — допустимые значения.
var (
	Ops     = []string{"eq", "ne", "gt", "ge", "lt", "le", "changed"}
	Actions = []string{"notify", "tool", "wake"}
)

// Validate проверяет правило.
func (r *Rule) Validate() error {
	var errs []string
	if strings.TrimSpace(r.Device) == "" || strings.TrimSpace(r.Property) == "" {
		errs = append(errs, "device и property обязательны")
	}
	for _, p := range []string{r.Device, r.Property} {
		if _, err := path.Match(p, ""); err != nil {
			errs = append(errs, fmt.Sprintf("плохой шаблон %q", p))
		}
	}
	if !contains(Ops, r.Op) {
		errs = append(errs, fmt.Sprintf("op %q не из %v", r.Op, Ops))
	}
	if r.Op != "changed" && r.Value == nil {
		errs = append(errs, "value обязателен для op="+r.Op)
	}
	if r.ForS < 0 || r.CooldownS < 0 {
		errs = append(errs, "for_s и cooldown_s не могут быть отрицательными")
	}
	for _, hm := range []string{r.After, r.Before} {
		if hm != "" {
			if _, err := parseHHMM(hm); err != nil {
				errs = append(errs, err.Error())
			}
		}
	}
	if !contains(Actions, r.Action) {
		errs = append(errs, fmt.Sprintf("action %q не из %v", r.Action, Actions))
	}
	if r.Action == "notify" && strings.TrimSpace(r.Text) == "" {
		errs = append(errs, "для action=notify нужен text")
	}
	if r.Action == "tool" && strings.TrimSpace(r.Tool) == "" {
		errs = append(errs, "для action=tool нужен tool")
	}
	if r.Priority != "" && r.Priority != "safety" && r.Priority != "background" {
		errs = append(errs, "priority: safety | background")
	}
	if len(errs) > 0 {
		return fmt.Errorf("правило: %s", strings.Join(errs, "; "))
	}
	return nil
}

// Matches — относится ли правило к свойству устройства.
func (r *Rule) Matches(device, property string) bool {
	return globMatch(r.Device, device) && globMatch(r.Property, property)
}

// Summary — краткое описание правила для журнала и промпта.
func (r *Rule) Summary() string {
	if r.Description != "" {
		return r.Description
	}
	cond := r.Device + "." + r.Property + " " + r.Op
	if r.Op != "changed" {
		cond += " " + asString(r.Value)
	}
	if r.ForS > 0 {
		cond += fmt.Sprintf(" for %gs", r.ForS)
	}
	return cond + " -> " + r.Action
}

// LoadRules читает правила из файла YAML или JSON (список правил или объект
// {rules: [...]}).
func LoadRules(file string) ([]Rule, error) {
	data, err := os.ReadFile(file)
	if err != nil {
		return nil, err
	}
	// YAML — надмножество JSON, поэтому один разборщик подходит для обоих форматов.
	var list []Rule
	if err := yaml.Unmarshal(data, &list); err != nil {
		var wrap struct {
			Rules []Rule `yaml:"rules"`
		}
		if err2 := yaml.Unmarshal(data, &wrap); err2 != nil {
			return nil, fmt.Errorf("%s: %w", file, err)
		}
		list = wrap.Rules
	}
	for i := range list {
		if list[i].Source == "" {
			list[i].Source = "file"
		}
		list[i].Value = normalizeYAML(list[i].Value)
		for k, v := range list[i].Args {
			list[i].Args[k] = normalizeYAML(v)
		}
		if err := list[i].Validate(); err != nil {
			return nil, fmt.Errorf("%s: правило %d: %w", file, i, err)
		}
	}
	return list, nil
}

// normalizeYAML приводит значения из YAML (int, map[string]any) к виду JSON.
func normalizeYAML(v any) any {
	switch x := v.(type) {
	case int:
		return float64(x)
	case int64:
		return float64(x)
	case map[string]any:
		for k, e := range x {
			x[k] = normalizeYAML(e)
		}
		return x
	case []any:
		for i, e := range x {
			x[i] = normalizeYAML(e)
		}
		return x
	}
	return v
}

// ------------------------------------------------------------ окно времени суток

func parseHHMM(s string) (time.Duration, error) {
	parts := strings.Split(strings.TrimSpace(s), ":")
	if len(parts) != 2 {
		return 0, fmt.Errorf("время %q: нужен формат HH:MM", s)
	}
	h, err1 := strconv.Atoi(parts[0])
	m, err2 := strconv.Atoi(parts[1])
	if err1 != nil || err2 != nil || h < 0 || h > 24 || m < 0 || m > 59 || (h == 24 && m != 0) {
		return 0, fmt.Errorf("время %q: нужен формат HH:MM", s)
	}
	return time.Duration(h)*time.Hour + time.Duration(m)*time.Minute, nil
}

// inWindow — попадает ли момент now в окно [after, before) по времени суток.
func inWindow(now time.Time, after, before string) bool {
	if after == "" && before == "" {
		return true
	}
	tod := now.Sub(midnight(now))
	a, b := time.Duration(0), 24*time.Hour
	if after != "" {
		a, _ = parseHHMM(after)
	}
	if before != "" {
		b, _ = parseHHMM(before)
	}
	if a <= b {
		return tod >= a && tod < b
	}
	return tod >= a || tod < b // окно через полночь (22:00–06:00)
}

// untilWindow — сколько ждать до начала окна (0, если уже внутри).
func untilWindow(now time.Time, after, before string) time.Duration {
	if inWindow(now, after, before) {
		return 0
	}
	start := time.Duration(0)
	if after != "" {
		start, _ = parseHHMM(after)
	}
	m := midnight(now)
	next := m.Add(start)
	if !next.After(now) {
		next = m.AddDate(0, 0, 1).Add(start)
	}
	return next.Sub(now)
}

func midnight(t time.Time) time.Time {
	y, mo, d := t.Date()
	return time.Date(y, mo, d, 0, 0, 0, 0, t.Location())
}

func globMatch(pattern, s string) bool {
	if pattern == "" || pattern == "*" {
		return true
	}
	ok, err := path.Match(pattern, s)
	return err == nil && ok
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}
