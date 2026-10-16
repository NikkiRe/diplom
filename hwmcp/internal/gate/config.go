package gate

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"

	"github.com/local/hwmcp/internal/argschema"
)

// Config — политика шлюза в declared.json (раздел "gate") или в отдельном файле.
//
//	{
//	  "default_ttl_s": 10, "max_state_age_s": 5, "commit_window_s": 5, "idem_capacity": 4096, "idem_max_age_s": 600,
//	  "devices": {
//	    "heater": {"tools": {"set_power": {<JSON Schema аргументов>}}, "read_only": [],
//	               "min_interval_s": 30, "max_per_min": null, "requires_confirm": [],
//	               "lease_s": null, "safe_state": {"on": false}}
//	  },
//	  "interlocks": [
//	    {"name": "heater_window", "device": "heater", "tool": "set_power",
//	     "needs": ["window_open", "temp_c"],
//	     "deny": [{"if": {"all": [{"arg": "on", "eq": true}, {"state": "window_open", "eq": true}]},
//	               "reason": "окно открыто"}],
//	     "clamp": null}
//	  ]
//	}
//
// В hw-mcp инструменты устройств (set_<свойство>, действия) строятся из инвентаря; раздел devices
// задаёт для них только политику. Ключ tools нужен устройствам, которых нет в инвентаре
// (демонстрационный стенд прототипа, тесты паритета).
type Config struct {
	DefaultTTL   *float64                `json:"default_ttl_s,omitempty"`
	MaxStateAge  *float64                `json:"max_state_age_s,omitempty"`
	CommitWindow *float64                `json:"commit_window_s,omitempty"`
	IdemCapacity *int                    `json:"idem_capacity,omitempty"`
	IdemMaxAge   *float64                `json:"idem_max_age_s,omitempty"` // hw-mcp: 600 с, если не задано
	Devices      map[string]DeviceConfig `json:"devices,omitempty"`
	Interlocks   []InterlockConfig       `json:"interlocks,omitempty"`
}

// DeviceConfig — политика одного привода.
type DeviceConfig struct {
	Tools           map[string]json.RawMessage `json:"tools,omitempty"`
	ReadOnly        []string                   `json:"read_only,omitempty"`
	MinInterval     float64                    `json:"min_interval_s,omitempty"`
	MaxPerMin       *int                       `json:"max_per_min,omitempty"`
	RequiresConfirm []string                   `json:"requires_confirm,omitempty"`
	Lease           *float64                   `json:"lease_s,omitempty"`
	SafeState       map[string]json.RawMessage `json:"safe_state,omitempty"`
}

// InterlockConfig — блокировка: предикат над аргументами команды и свежим состоянием.
type InterlockConfig struct {
	Name   string          `json:"name"`
	Device string          `json:"device"`
	Tool   json.RawMessage `json:"tool"`            // "имя", ["имя", ...] или "*"
	Needs  []string        `json:"needs,omitempty"` // ключи состояния; по умолчанию — все упомянутые в условиях
	Deny   []RuleConfig    `json:"deny"`
	// Clamp — вместо отказа ограничить аргументы: {"pct": {"set": 30}} или {"pct": {"max": 30}}.
	Clamp map[string]ClampConfig `json:"clamp,omitempty"`
}

// RuleConfig — правило отказа: если условие истинно, команда нарушает блокировку с причиной Reason.
type RuleConfig struct {
	If     json.RawMessage `json:"if"`
	Reason string          `json:"reason"`
}

// ClampConfig — ограничение одного аргумента.
type ClampConfig struct {
	Set json.RawMessage `json:"set,omitempty"`
	Min *float64        `json:"min,omitempty"`
	Max *float64        `json:"max,omitempty"`
}

// ParseConfig разбирает политику (неизвестные ключи — ошибка).
func ParseConfig(b []byte) (Config, error) {
	var c Config
	if len(bytes.TrimSpace(b)) == 0 {
		return c, nil
	}
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&c); err != nil {
		return c, fmt.Errorf("gate config: %w", err)
	}
	if _, err := dec.Token(); err != io.EOF {
		return c, fmt.Errorf("gate config: unexpected data after the top-level object")
	}
	return c, nil
}

// ----------------------------------------------------------------------------------------------
// Условия

// cond — скомпилированное условие:
//
//	{"arg": "pct", "gt": 30}          аргумент команды
//	{"state": "temp_c", "ge": 30.0}   свежее значение состояния
//	{"all": [...]}, {"any": [...]}, {"not": {...}}
//
// Операции: eq, ne (равенство JSON), lt, le, gt, ge (только числа).
type cond struct {
	all, any []*cond
	not      *cond
	arg      string
	state    string
	op       string
	value    any
	num      float64 // value как число для lt/le/gt/ge
}

var compareOps = map[string]bool{"eq": true, "ne": true, "lt": true, "le": true, "gt": true, "ge": true}

func compileCond(raw json.RawMessage) (*cond, error) {
	v, err := argschema.Decode(raw)
	if err != nil {
		return nil, err
	}
	return compileCondValue(v)
}

func compileCondValue(v any) (*cond, error) {
	m, ok := v.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("condition must be an object, got %s", argschema.TypeName(v))
	}
	keys := sortedKeys(m)
	if sub, ok := m["all"]; ok {
		return compileList(keys, "all", sub, func(c *cond, l []*cond) { c.all = l })
	}
	if sub, ok := m["any"]; ok {
		return compileList(keys, "any", sub, func(c *cond, l []*cond) { c.any = l })
	}
	if sub, ok := m["not"]; ok {
		if len(keys) != 1 {
			return nil, fmt.Errorf("'not' must be the only key, got %v", keys)
		}
		inner, err := compileCondValue(sub)
		if err != nil {
			return nil, err
		}
		return &cond{not: inner}, nil
	}
	c := &cond{}
	for _, k := range keys {
		switch {
		case k == "arg" || k == "state":
			s, ok := m[k].(string)
			if !ok || s == "" {
				return nil, fmt.Errorf("%q must be a non-empty string", k)
			}
			if k == "arg" {
				c.arg = s
			} else {
				c.state = s
			}
		case compareOps[k]:
			if c.op != "" {
				return nil, fmt.Errorf("several operators in one condition: %v", keys)
			}
			c.op, c.value = k, m[k]
		default:
			return nil, fmt.Errorf("unknown key %q in condition", k)
		}
	}
	if (c.arg == "") == (c.state == "") {
		return nil, fmt.Errorf("condition needs exactly one of 'arg' or 'state': %v", keys)
	}
	if c.op == "" {
		return nil, fmt.Errorf("condition needs an operator (eq, ne, lt, le, gt, ge): %v", keys)
	}
	if c.op != "eq" && c.op != "ne" {
		n, ok := toFloat(c.value)
		if !ok {
			return nil, fmt.Errorf("operator %s needs a number, got %s", c.op, argschema.TypeName(c.value))
		}
		c.num = n
	}
	return c, nil
}

func compileList(keys []string, name string, sub any, set func(*cond, []*cond)) (*cond, error) {
	if len(keys) != 1 {
		return nil, fmt.Errorf("%q must be the only key, got %v", name, keys)
	}
	list, ok := sub.([]any)
	if !ok || len(list) == 0 {
		return nil, fmt.Errorf("%q needs a non-empty array", name)
	}
	c := &cond{}
	var out []*cond
	for _, x := range list {
		cc, err := compileCondValue(x)
		if err != nil {
			return nil, err
		}
		out = append(out, cc)
	}
	set(c, out)
	return c, nil
}

// stateKeys — ключи состояния, упомянутые в условии.
func (c *cond) stateKeys(into map[string]bool) {
	if c == nil {
		return
	}
	if c.state != "" {
		into[c.state] = true
	}
	for _, s := range c.all {
		s.stateKeys(into)
	}
	for _, s := range c.any {
		s.stateKeys(into)
	}
	c.not.stateKeys(into)
}

// errEval — условие нельзя вычислить (не число в сравнении): блокировка срабатывает (fail-closed).
type errEval struct{ msg string }

func (e errEval) Error() string { return e.msg }

// eval вычисляет условие. Отсутствующий аргумент делает сравнение ложным (необязательные аргументы);
// значение состояния всегда есть: его наличие и свежесть шлюз проверил до вычисления.
func (c *cond) eval(args map[string]any, snap map[string]any) (bool, error) {
	switch {
	case c.all != nil:
		for _, s := range c.all {
			ok, err := s.eval(args, snap)
			if err != nil || !ok {
				return false, err
			}
		}
		return true, nil
	case c.any != nil:
		for _, s := range c.any {
			ok, err := s.eval(args, snap)
			if err != nil {
				return false, err
			}
			if ok {
				return true, nil
			}
		}
		return false, nil
	case c.not != nil:
		ok, err := c.not.eval(args, snap)
		return !ok, err
	}
	var v any
	var present bool
	if c.arg != "" {
		v, present = args[c.arg]
	} else {
		v, present = snap[c.state]
	}
	if !present {
		return false, nil
	}
	switch c.op {
	case "eq":
		return argschema.Equal(v, c.value), nil
	case "ne":
		return !argschema.Equal(v, c.value), nil
	}
	x, ok := toFloat(v)
	if !ok {
		src := "arg " + c.arg
		if c.state != "" {
			src = "state " + c.state
		}
		return false, errEval{fmt.Sprintf("%s is %s, not a number", src, argschema.TypeName(v))}
	}
	switch c.op {
	case "lt":
		return x < c.num, nil
	case "le":
		return x <= c.num, nil
	case "gt":
		return x > c.num, nil
	default: // ge
		return x >= c.num, nil
	}
}

func toFloat(v any) (float64, bool) {
	switch n := v.(type) {
	case json.Number:
		f, err := n.Float64()
		return f, err == nil
	case float64:
		return n, true
	case int:
		return float64(n), true
	}
	return 0, false
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// toolMatcher — к каким инструментам устройства относится блокировка.
type toolMatcher struct {
	any   bool
	names map[string]bool
}

func parseToolMatcher(raw json.RawMessage) (toolMatcher, error) {
	var one string
	if err := json.Unmarshal(raw, &one); err == nil {
		if one == "*" {
			return toolMatcher{any: true}, nil
		}
		if one == "" {
			return toolMatcher{}, fmt.Errorf("empty tool name")
		}
		return toolMatcher{names: map[string]bool{one: true}}, nil
	}
	var list []string
	if err := json.Unmarshal(raw, &list); err != nil || len(list) == 0 {
		return toolMatcher{}, fmt.Errorf(`"tool" must be a name, a non-empty list of names or "*"`)
	}
	m := toolMatcher{names: map[string]bool{}}
	for _, n := range list {
		if n == "" || n == "*" {
			return toolMatcher{}, fmt.Errorf("bad tool name %q in list", n)
		}
		m.names[n] = true
	}
	return m, nil
}

func (m toolMatcher) match(tool string) bool { return m.any || m.names[tool] }

func (m toolMatcher) String() string {
	if m.any {
		return "*"
	}
	return strings.Join(sortedKeys(m.names), ",")
}
