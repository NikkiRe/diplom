package hwctx

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/local/edgectx/internal/canon"
)

// Prop — свойство (канал) устройства.
type Prop struct {
	Name        string   `json:"name"`
	Unit        string   `json:"unit,omitempty"`
	Type        string   `json:"type,omitempty"`
	Writable    bool     `json:"writable,omitempty"`
	Min         *float64 `json:"min,omitempty"`
	Max         *float64 `json:"max,omitempty"`
	Precision   float64  `json:"precision,omitempty"`
	Description string   `json:"description,omitempty"`
}

// Device — устройство инвентаря (уровень L0).
type Device struct {
	ID          string   `json:"id"`
	Server      string   `json:"server,omitempty"`
	Location    string   `json:"location,omitempty"`
	Description string   `json:"description,omitempty"`
	Props       []Prop   `json:"props"`
	Actions     []string `json:"actions,omitempty"`
}

// Inventory — инвентарь всех серверов, упорядоченный по ID устройства.
type Inventory struct {
	Devices []Device `json:"devices"`
	// Raw — тексты инвентаря, которые не удалось разобрать (по серверам); они
	// попадают в L0 как канонический JSON, чтобы LLM не осталась без инвентаря.
	Raw map[string]string `json:"raw,omitempty"`
}

// Merge объединяет инвентари (устройства с одинаковым ID — последнее побеждает).
func Merge(parts ...Inventory) Inventory {
	byID := map[string]Device{}
	raw := map[string]string{}
	for _, p := range parts {
		for _, d := range p.Devices {
			byID[d.ID] = d
		}
		for k, v := range p.Raw {
			raw[k] = v
		}
	}
	out := Inventory{}
	for _, d := range byID {
		out.Devices = append(out.Devices, d)
	}
	sort.Slice(out.Devices, func(i, j int) bool { return out.Devices[i].ID < out.Devices[j].ID })
	if len(raw) > 0 {
		out.Raw = raw
	}
	return out
}

// Find — устройство по ID.
func (inv Inventory) Find(id string) (Device, bool) {
	for _, d := range inv.Devices {
		if d.ID == id {
			return d, true
		}
	}
	return Device{}, false
}

// Text — текстовая проекция инвентаря для стабильного префикса: одна строка на
// устройство (ср. GAP2 ctx_formats.text_inventory: 86 токенов на 7 устройств
// против 1 027 у полного TD).
func (inv Inventory) Text() string {
	var b strings.Builder
	if len(inv.Devices) > 0 {
		b.WriteString("Устройства (id (место): свойство [единица, rw — запись, мин..макс]):\n")
	}
	for _, d := range inv.Devices {
		b.WriteString(d.ID)
		if d.Location != "" {
			b.WriteString(" (" + d.Location + ")")
		}
		b.WriteString(": ")
		for i, p := range d.Props {
			if i > 0 {
				b.WriteString(", ")
			}
			b.WriteString(p.Name)
			attrs := []string{}
			if u := displayUnit(p); u != "" {
				attrs = append(attrs, u)
			}
			if p.Writable {
				attrs = append(attrs, "rw")
			}
			if p.Min != nil || p.Max != nil {
				attrs = append(attrs, rangeText(p.Min, p.Max))
			}
			if len(attrs) > 0 {
				b.WriteString(" [" + strings.Join(attrs, ", ") + "]")
			}
		}
		if len(d.Actions) > 0 {
			b.WriteString("; действия: " + strings.Join(d.Actions, ", "))
		}
		if d.Description != "" {
			b.WriteString(" — " + oneLine(d.Description))
		}
		b.WriteString("\n")
	}
	servers := make([]string, 0, len(inv.Raw))
	for s := range inv.Raw {
		servers = append(servers, s)
	}
	sort.Strings(servers)
	for _, s := range servers {
		fmt.Fprintf(&b, "Инвентарь сервера %s: %s\n", s, inv.Raw[s])
	}
	return strings.TrimRight(b.String(), "\n")
}

func displayUnit(p Prop) string {
	if p.Unit != "" && p.Unit != "/" {
		return p.Unit
	}
	if p.Type == "boolean" || p.Type == "bool" || p.Unit == "/" {
		return "bool"
	}
	return ""
}

func rangeText(min, max *float64) string {
	f := func(x *float64) string {
		if x == nil {
			return ""
		}
		s, _ := canon.FormatFloat(*x)
		return s
	}
	return f(min) + ".." + f(max)
}

func oneLine(s string) string { return strings.Join(strings.Fields(s), " ") }

// ------------------------------------------------------------------- разбор

// ParseInventory разбирает инвентарь сервера в одном из распространённых видов:
// массив устройств; {"devices": [...]} или {"devices": {id: {...}}}; у устройства
// свойства в "properties" / "channels" / "props" (массив объектов или отображение
// имя → описание). Возвращает инвентарь и начальные значения свойств (поле value),
// если сервер их прислал. Неразобранный документ попадает в Inventory.Raw.
func ParseInventory(server, raw string) (Inventory, map[string]map[string]any) {
	values := map[string]map[string]any{}
	var doc any
	dec := json.NewDecoder(strings.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&doc); err != nil {
		return rawInventory(server, raw), values
	}
	var items []any
	switch x := doc.(type) {
	case []any:
		items = x
	case map[string]any:
		devs, ok := x["devices"]
		if !ok {
			devs = x["things"]
		}
		switch d := devs.(type) {
		case []any:
			items = d
		case map[string]any:
			for _, id := range sortedKeys(d) {
				if m, ok := d[id].(map[string]any); ok {
					if _, has := m["id"]; !has {
						m["id"] = id
					}
					items = append(items, m)
				}
			}
		}
	}
	inv := Inventory{}
	for _, it := range items {
		m, ok := it.(map[string]any)
		if !ok {
			continue
		}
		d, vals := parseDevice(m)
		if d.ID == "" {
			continue
		}
		d.Server = server
		inv.Devices = append(inv.Devices, d)
		if len(vals) > 0 {
			values[d.ID] = vals
		}
	}
	if len(inv.Devices) == 0 {
		return rawInventory(server, raw), values
	}
	return Merge(inv), values
}

func rawInventory(server, raw string) Inventory {
	text := strings.TrimSpace(raw)
	var v any
	if json.Unmarshal([]byte(text), &v) == nil {
		text = canon.String(v)
	}
	if text == "" {
		return Inventory{}
	}
	return Inventory{Raw: map[string]string{server: text}}
}

func parseDevice(m map[string]any) (Device, map[string]any) {
	d := Device{ID: firstString(m, "id", "device", "device_id", "name", "title")}
	d.Location = firstString(m, "location", "room", "place")
	d.Description = firstString(m, "description")
	vals := map[string]any{}
	for _, key := range []string{"properties", "channels", "props"} {
		switch ps := m[key].(type) {
		case []any:
			for _, p := range ps {
				if pm, ok := p.(map[string]any); ok {
					prop, v, has := parseProp("", pm)
					if prop.Name != "" {
						d.Props = append(d.Props, prop)
						if has {
							vals[prop.Name] = v
						}
					}
				}
			}
		case map[string]any:
			for _, name := range sortedKeys(ps) {
				switch pv := ps[name].(type) {
				case map[string]any:
					prop, v, has := parseProp(name, pv)
					d.Props = append(d.Props, prop)
					if has {
						vals[prop.Name] = v
					}
				default: // имя -> значение
					d.Props = append(d.Props, Prop{Name: name, Type: jsonType(pv)})
					vals[name] = normalizeNumber(pv)
				}
			}
		}
	}
	sort.SliceStable(d.Props, func(i, j int) bool { return d.Props[i].Name < d.Props[j].Name })
	switch as := m["actions"].(type) {
	case []any:
		for _, a := range as {
			switch x := a.(type) {
			case string:
				d.Actions = append(d.Actions, x)
			case map[string]any:
				if n := firstString(x, "name", "id"); n != "" {
					d.Actions = append(d.Actions, n)
				}
			}
		}
	case map[string]any:
		d.Actions = sortedKeys(as)
	}
	sort.Strings(d.Actions)
	return d, vals
}

func parseProp(name string, m map[string]any) (Prop, any, bool) {
	p := Prop{Name: name}
	if p.Name == "" {
		p.Name = firstString(m, "name", "id", "property")
	}
	p.Unit = firstString(m, "unit", "units")
	p.Type = firstString(m, "type")
	p.Description = firstString(m, "description", "title")
	switch w := m["writable"].(type) {
	case bool:
		p.Writable = w
	}
	if rw, ok := m["rw"]; ok {
		switch x := rw.(type) {
		case bool:
			p.Writable = x
		case string:
			p.Writable = strings.Contains(strings.ToLower(x), "w")
		}
	}
	if acc, ok := m["access"].(string); ok {
		p.Writable = strings.Contains(strings.ToLower(acc), "w")
	}
	if ro, ok := m["readOnly"].(bool); ok {
		p.Writable = !ro
	}
	if ro, ok := m["read_only"].(bool); ok {
		p.Writable = !ro
	}
	p.Min = firstNumber(m, "min", "minimum")
	p.Max = firstNumber(m, "max", "maximum")
	if r, ok := m["range"].([]any); ok && len(r) == 2 {
		if f, ok := num(r[0]); ok {
			p.Min = &f
		}
		if f, ok := num(r[1]); ok {
			p.Max = &f
		}
	}
	if pr := firstNumber(m, "precision", "resolution", "step", "multipleOf"); pr != nil {
		p.Precision = *pr
	}
	v, has := m["value"]
	if p.Type == "" && has {
		p.Type = jsonType(v)
	}
	return p, normalizeNumber(v), has
}

func firstString(m map[string]any, keys ...string) string {
	for _, k := range keys {
		if s, ok := m[k].(string); ok && s != "" {
			return s
		}
	}
	return ""
}

func firstNumber(m map[string]any, keys ...string) *float64 {
	for _, k := range keys {
		if f, ok := num(m[k]); ok {
			return &f
		}
	}
	return nil
}

func num(v any) (float64, bool) {
	switch x := v.(type) {
	case json.Number:
		f, err := x.Float64()
		return f, err == nil
	case float64:
		return x, true
	case int:
		return float64(x), true
	}
	return 0, false
}

// normalizeNumber переводит json.Number в float64 (значения состояния).
func normalizeNumber(v any) any {
	switch x := v.(type) {
	case json.Number:
		if f, err := x.Float64(); err == nil {
			return f
		}
	case map[string]any:
		for k, e := range x {
			x[k] = normalizeNumber(e)
		}
	case []any:
		for i, e := range x {
			x[i] = normalizeNumber(e)
		}
	}
	return v
}

func jsonType(v any) string {
	switch v.(type) {
	case bool:
		return "boolean"
	case json.Number, float64:
		return "number"
	case string:
		return "string"
	}
	return ""
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
