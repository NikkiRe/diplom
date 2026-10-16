// Package inventory — модель устройств для агента, построенная из инвентаря sysfs и declared.json.
//
// Внутреннее представление — «вещи» в духе W3C WoT TD 1.1 (минимально нужное): устройство со
// свойствами (чтение / запись) и действиями. Из модели строятся каталог инструментов MCP (generic и
// explicit), ресурсы hw://inventory и hw://td и текстовая проекция L0 для стабильного префикса промпта.
//
// Имена устройств для агента детерминированы: объявленные устройства — их id из declared.json,
// обнаруженные — names[<id sysfs>] из declared.json, иначе slug("<место>_<имя драйвера>"), при
// совпадении — суффикс _2, _3 в порядке инвентаря.
package inventory

import (
	"fmt"
	"regexp"
	"sort"
	"strings"

	"github.com/local/hwmcp/internal/declared"
	"github.com/local/hwmcp/internal/sysfs"
)

// Тип значения свойства.
const (
	TypeNumber  = "number"
	TypeBoolean = "boolean"
)

// Стандартные действия.
const (
	ActionPulse      = "pulse"       // включить выход на N секунд, затем выключить (таймер в hw-mcp)
	ActionRenewLease = "renew_lease" // продлить аренду привода
	MaxPulseSeconds  = 3600.0
)

// Property — свойство устройства.
type Property struct {
	Name      string // имя для агента (slug имени канала)
	Title     string // имя канала из инвентаря
	Quantity  string // величина ABI (temp, humidityrelative, ...) или из declared.json (motion, switch)
	Unit      string // единица SenML; "" для логических
	Type      string // number | boolean
	Writable  bool
	Threshold float64 // порог изменения для уведомления (0 — любое изменение)
	Href      string  // привязка протокола для TD: file:///sys/... или gpiod://chip/line

	Channel *sysfs.Channel // источник значения (для GPIO — nil-источник, читает драйвер линий)
	GPIO    *declared.GPIO // назначение линии (только для объявленных GPIO)
}

// Key — ключ состояния "<устройство>.<свойство>" (так на него ссылаются блокировки шлюза).
func Key(device, property string) string { return device + "." + property }

// Action — действие устройства.
type Action struct {
	Name        string
	Description string
	Input       map[string]any // JSON Schema аргументов (объект)
	Property    string         // свойство, которым управляет действие (для pulse)
}

// Device — устройство для агента.
type Device struct {
	ID            string
	SourceID      string // id в инвентаре sysfs ("iio:bme280@i2c-1/0x76")
	Source        string // iio | hwmon | w1 | v4l2 | usb | i2c | gpio | hat | declared
	Kind          string // имя драйвера/чипа (bme280, DS18B20, ...)
	Location      string
	Description   string
	Observability string
	Properties    []*Property // по имени
	Actions       []*Action   // по имени
	Notes         []string

	Raw *sysfs.Device
}

// Property — свойство по имени (nil — нет).
func (d *Device) Property(name string) *Property {
	for _, p := range d.Properties {
		if p.Name == name {
			return p
		}
	}
	return nil
}

// Action — действие по имени (nil — нет).
func (d *Device) Action(name string) *Action {
	for _, a := range d.Actions {
		if a.Name == name {
			return a
		}
	}
	return nil
}

// Actionable — у устройства есть свойства или действия (показывается в list_devices по умолчанию).
func (d *Device) Actionable() bool { return len(d.Properties) > 0 || len(d.Actions) > 0 }

// Inventory — набор устройств.
type Inventory struct {
	Devices []*Device // по ID
	Raw     []*sysfs.Device
	byID    map[string]*Device
}

// Device — устройство по ID (nil — нет).
func (inv *Inventory) Device(id string) *Device { return inv.byID[id] }

// Options — параметры построения модели.
type Options struct {
	// Leased — устройства с арендой (из политики шлюза): им добавляется действие renew_lease.
	Leased map[string]bool
}

// defaultThresholds — пороги уведомлений по величине (единицы SenML). Это параметры эксперимента:
// фиксируются до прогонов (решение 5 концепции), переопределяются в declared.json → thresholds.
var defaultThresholds = map[string]float64{
	"temp": 0.2, "humidityrelative": 1.0, "humidity": 1.0, "pressure": 50,
	"illuminance": 10, "voltage": 0.01, "in": 0.01, "current": 0.01, "curr": 0.01,
	"power": 0.05, "accel": 0.1,
}

var nonSlug = regexp.MustCompile(`[^a-z0-9]+`)

// Slug — имя из строчных латинских букв, цифр и "_", начинается с буквы.
func Slug(s string) string {
	out := strings.Trim(nonSlug.ReplaceAllString(strings.ToLower(s), "_"), "_")
	if out == "" {
		return "dev"
	}
	if out[0] >= '0' && out[0] <= '9' {
		out = "d" + out
	}
	if len(out) > 48 {
		out = strings.TrimRight(out[:48], "_")
	}
	return out
}

// Build строит модель из инвентаря sysfs и declared.json (decl может быть nil).
func Build(raw []*sysfs.Device, decl *declared.Declared, opts Options) (*Inventory, error) {
	if decl == nil {
		decl = &declared.Declared{}
	}
	inv := &Inventory{Raw: raw, byID: map[string]*Device{}}
	used := map[string]bool{}
	for _, rd := range raw {
		d := &Device{SourceID: rd.ID, Source: rd.Source, Observability: rd.Observability, Raw: rd,
			Notes: append([]string(nil), rd.Notes...)}
		if rd.Name != nil {
			d.Kind = *rd.Name
		}
		if rd.Location != nil {
			d.Location = *rd.Location
		}
		if rd.Description != nil {
			d.Description = *rd.Description
		}
		d.ID = uniqueID(baseID(rd, d, decl), used)
		if err := addProperties(d, rd, decl); err != nil {
			return nil, err
		}
		addActions(d, opts)
		inv.Devices = append(inv.Devices, d)
		inv.byID[d.ID] = d
	}
	sort.Slice(inv.Devices, func(i, j int) bool { return inv.Devices[i].ID < inv.Devices[j].ID })
	for key := range decl.Thresholds {
		dev, prop, ok := strings.Cut(key, ".")
		if d := inv.byID[dev]; !ok || d == nil || d.Property(prop) == nil {
			return nil, fmt.Errorf("declared.json thresholds: unknown property %q", key)
		}
	}
	for src := range decl.Names {
		found := false
		for _, rd := range raw {
			if rd.ID == src {
				found = true
			}
		}
		if !found {
			return nil, fmt.Errorf("declared.json names: unknown inventory id %q", src)
		}
	}
	return inv, nil
}

func baseID(rd *sysfs.Device, d *Device, decl *declared.Declared) string {
	if rd.GPIO != nil {
		return rd.GPIO.ID
	}
	if rd.Source == "declared" && rd.Name != nil { // последовательные устройства из declared.json
		return *rd.Name
	}
	if alias, ok := decl.Names[rd.ID]; ok {
		return alias
	}
	name := d.Kind
	if name == "" {
		name = rd.ID
	}
	if d.Location != "" {
		return Slug(d.Location + "_" + name)
	}
	return Slug(name)
}

func uniqueID(base string, used map[string]bool) string {
	id := base
	for i := 2; used[id]; i++ {
		id = fmt.Sprintf("%s_%d", base, i)
	}
	used[id] = true
	return id
}

func addProperties(d *Device, rd *sysfs.Device, decl *declared.Declared) error {
	names := map[string]bool{}
	for i := range rd.Channels {
		ch := &rd.Channels[i]
		p := &Property{Title: ch.Name, Quantity: ch.Quantity, Writable: ch.Writable, Channel: ch}
		p.Name = uniqueID(Slug(ch.Name), names)
		if ch.Unit == "/" {
			p.Type = TypeBoolean
		} else {
			p.Type, p.Unit = TypeNumber, ch.Unit
			p.Threshold = defaultThresholds[ch.Quantity]
		}
		if t, ok := decl.Thresholds[Key(d.ID, p.Name)]; ok {
			p.Threshold = t
		}
		if ch.IsGPIO() {
			p.GPIO = rd.GPIO
			p.Href = fmt.Sprintf("gpiod://%s/%d", rd.GPIO.Chip, rd.GPIO.Line) // нестандартная схема URI
		} else {
			p.Href = "file:///" + ch.Path
		}
		if p.Writable && (p.Type != TypeBoolean || p.GPIO == nil) {
			return fmt.Errorf("device %s: writable property %s is not a GPIO output", d.ID, p.Name)
		}
		d.Properties = append(d.Properties, p)
	}
	sort.Slice(d.Properties, func(i, j int) bool { return d.Properties[i].Name < d.Properties[j].Name })
	return nil
}

func addActions(d *Device, opts Options) {
	for _, p := range d.Properties {
		if p.Writable && p.Type == TypeBoolean {
			d.Actions = append(d.Actions, &Action{
				Name:        ActionPulse,
				Description: fmt.Sprintf("Turn %s on for the given number of seconds, then off", p.Name),
				Property:    p.Name,
				Input: map[string]any{
					"type": "object",
					"properties": map[string]any{"seconds": map[string]any{
						"type": "number", "exclusiveMinimum": 0, "maximum": MaxPulseSeconds,
						"description": "pulse duration, s"}},
					"required":             []any{"seconds"},
					"additionalProperties": false,
				},
			})
			break // одно действие pulse на устройство: у линии GPIO одно свойство
		}
	}
	if opts.Leased[d.ID] {
		d.Actions = append(d.Actions, &Action{
			Name:        ActionRenewLease,
			Description: "Extend the lease of the running actuator; without renewal it returns to its safe state",
			Input:       map[string]any{"type": "object", "properties": map[string]any{}, "additionalProperties": false},
		})
	}
	sort.Slice(d.Actions, func(i, j int) bool { return d.Actions[i].Name < d.Actions[j].Name })
}

// WriteToolName — имя инструмента шлюза для записи свойства.
func WriteToolName(property string) string { return "set_" + property }

// GateTools — инструменты устройств для реестра шлюза: устройство -> инструмент -> схема аргументов.
// Запись свойства P — инструмент set_P с аргументом value; действия — под своими именами
// (renew_lease шлюз не проверяет: это продление уже принятой команды).
func (inv *Inventory) GateTools() map[string]map[string]any {
	out := map[string]map[string]any{}
	for _, d := range inv.Devices {
		tools := map[string]any{}
		for _, p := range d.Properties {
			if p.Writable {
				tools[WriteToolName(p.Name)] = map[string]any{
					"type":                 "object",
					"properties":           map[string]any{"value": ValueSchema(p)},
					"required":             []any{"value"},
					"additionalProperties": false,
				}
			}
		}
		for _, a := range d.Actions {
			if a.Name != ActionRenewLease {
				tools[a.Name] = a.Input
			}
		}
		if len(tools) > 0 {
			out[d.ID] = tools
		}
	}
	return out
}

// ValueSchema — JSON Schema значения свойства.
func ValueSchema(p *Property) map[string]any {
	if p.Type == TypeBoolean {
		return map[string]any{"type": "boolean"}
	}
	return map[string]any{"type": "number"}
}
