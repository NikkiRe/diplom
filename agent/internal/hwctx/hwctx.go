// Package hwctx — контекст оборудования (концепция §9):
//
//   - L0 инвентарь — текстовая проекция в стабильном префиксе промпта;
//   - L1 состояние — последние значения и их возраст; в промпт попадает по
//     политике: snapshot_head (полный снимок в префиксе, обновляется не чаще
//     snapshot_period_s), delta_tail (только изменения с прошлого запроса — в
//     хвосте), pull (не попадает; LLM читает инструментами). Когда MCP-сервер
//     падает, показания его устройств помечаются «нет связи» и не считаются
//     известными для правил, пока не придёт свежее показание устройства;
//   - L2 события — кольцевой журнал изменений; в промпт — только разбудившее
//     событие, остальное по запросу (инструмент recent_events, top-k);
//   - L3 плата и бюджет — по запросу (инструмент board_health MCP-сервера).
//
// Числа округляются до точности датчика (из инвентаря или по единице измерения),
// поэтому дрожание младших разрядов не порождает «изменений» и токенов.
package hwctx

import (
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strings"
	"sync"

	"github.com/local/edgectx/internal/canon"
)

// Режимы L1 в промпте.
const (
	SnapshotHead = "snapshot_head"
	DeltaTail    = "delta_tail"
	Pull         = "pull"
)

// Reading — последнее значение свойства.
type Reading struct {
	Value any     `json:"value"`
	T     float64 `json:"t"`
}

// Change — изменение свойства, обнаруженное при обновлении состояния.
type Change struct {
	Device string
	Prop   string
	Value  any
	Old    any
	HadOld bool
}

// Event — запись журнала L2.
type Event struct {
	T       float64 `json:"t"`
	Device  string  `json:"device"`
	Prop    string  `json:"property"`
	Value   any     `json:"value"`
	Old     any     `json:"old,omitempty"`
	InputID string  `json:"input_id,omitempty"`
}

// Context — контекст оборудования. Потокобезопасен.
type Context struct {
	mu        sync.RWMutex
	mode      string
	period    float64
	inv       Inventory
	invText   string
	precision map[string]float64
	serverOf  map[string]string // устройство -> MCP-сервер (из инвентаря)
	stale     map[string]bool   // устройства без свежих показаний после падения их сервера
	state     map[string]map[string]Reading
	sent      map[string]string // ключ -> значение, показанное LLM в подтверждённом ответе (delta_tail)
	log       []Event           // кольцевой буфер L2 ёмкостью logCap
	logHead   int               // самое старое событие заполненного буфера
	logCap    int
	snapText  string
	snapT     float64
	snapOK    bool
}

// New создаёт контекст. mode — snapshot_head | delta_tail | pull; logCap —
// ёмкость журнала L2.
func New(mode string, snapshotPeriodS float64, logCap int) *Context {
	return &Context{mode: mode, period: snapshotPeriodS, logCap: max(logCap, 1),
		precision: map[string]float64{}, serverOf: map[string]string{}, stale: map[string]bool{},
		state: map[string]map[string]Reading{}, sent: map[string]string{}}
}

// Mode — режим L1.
func (c *Context) Mode() string { return c.mode }

// SetInventory заменяет инвентарь и пересчитывает точности свойств.
func (c *Context) SetInventory(inv Inventory) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.inv = inv
	c.invText = inv.Text()
	c.precision = map[string]float64{}
	c.serverOf = map[string]string{}
	for _, d := range inv.Devices {
		c.serverOf[d.ID] = d.Server
		for _, p := range d.Props {
			c.precision[d.ID+"."+p.Name] = precisionFor(p)
		}
	}
	c.snapOK = false
}

// MarkStale отмечает показания устройств сервера устаревшими (связь с
// сервером потеряна): до свежего показания (Apply) устройство неизвестно.
func (c *Context) MarkStale(server string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for d, srv := range c.serverOf {
		if srv == server {
			c.stale[d] = true
		}
	}
	c.snapOK = false
}

// Inventory — текущий инвентарь.
func (c *Context) Inventory() Inventory {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.inv
}

// InventoryText — L0 для префикса.
func (c *Context) InventoryText() string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.invText
}

// precisionFor — точность свойства: из инвентаря, иначе по единице измерения.
func precisionFor(p Prop) float64 {
	if p.Precision > 0 {
		return p.Precision
	}
	switch strings.ToLower(p.Unit) {
	case "cel", "°c", "c", "degc", "hpa", "kpa":
		return 0.1
	case "%", "%rh", "lx", "lux", "ppm", "pa", "rpm", "w":
		return 1
	case "v", "a":
		return 0.01
	}
	return 0
}

// defaultPrecision — точность для свойств, которых нет в инвентаре.
const defaultPrecision = 0.01

func (c *Context) round(device, prop string, v any) any {
	f, ok := v.(float64)
	if !ok {
		if n, isNum := v.(json.Number); isNum {
			ff, err := n.Float64()
			if err != nil {
				return v
			}
			f, ok = ff, true
		}
	}
	if !ok || math.IsNaN(f) || math.IsInf(f, 0) {
		return v
	}
	step, known := c.precision[device+"."+prop]
	if !known {
		step = defaultPrecision
	}
	return canon.Round(f, step)
}

// Seed задаёт начальное состояние без порождения изменений.
func (c *Context) Seed(t float64, device string, props map[string]any) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for p, v := range props {
		c.setLocked(t, device, p, c.round(device, p, v))
	}
}

func (c *Context) setLocked(t float64, device, prop string, v any) {
	m := c.state[device]
	if m == nil {
		m = map[string]Reading{}
		c.state[device] = m
	}
	m[prop] = Reading{Value: v, T: t}
}

// Apply применяет свежее состояние устройства (снимая пометку «нет связи») и
// возвращает изменения (по округлённым значениям, в порядке имён свойств).
func (c *Context) Apply(t float64, device string, props map[string]any) []Change {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.stale, device)
	names := make([]string, 0, len(props))
	for p := range props {
		names = append(names, p)
	}
	sort.Strings(names)
	var out []Change
	for _, p := range names {
		v := c.round(device, p, props[p])
		old, had := c.state[device][p]
		if had && canon.String(old.Value) == canon.String(v) {
			continue
		}
		c.setLocked(t, device, p, v)
		ch := Change{Device: device, Prop: p, Value: v, HadOld: had}
		if had {
			ch.Old = old.Value
		}
		out = append(out, ch)
	}
	return out
}

// Get — текущее значение свойства (реализует gate.StateReader). Показание
// устройства без связи с сервером неизвестно.
func (c *Context) Get(device, prop string) (any, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	r, ok := c.state[device][prop]
	if !ok || c.stale[device] {
		return nil, false
	}
	return r.Value, true
}

// State — копия состояния.
func (c *Context) State() map[string]map[string]Reading {
	c.mu.RLock()
	defer c.mu.RUnlock()
	out := make(map[string]map[string]Reading, len(c.state))
	for d, m := range c.state {
		cp := make(map[string]Reading, len(m))
		for k, v := range m {
			cp[k] = v
		}
		out[d] = cp
	}
	return out
}

// Record добавляет событие в журнал L2; в заполненном буфере вытесняется
// самое старое.
func (c *Context) Record(ev Event) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.log) < c.logCap {
		c.log = append(c.log, ev)
		return
	}
	c.log[c.logHead] = ev
	c.logHead = (c.logHead + 1) % c.logCap
}

// Recent — последние k событий (по возрастанию времени), при device != "" — только
// этого устройства, при prop != "" — только этого свойства.
func (c *Context) Recent(k int, device, prop string) []Event {
	c.mu.RLock()
	defer c.mu.RUnlock()
	var out []Event
	n := len(c.log)
	for i := n - 1; i >= 0 && (k <= 0 || len(out) < k); i-- {
		e := c.log[(c.logHead+i)%n]
		if (device == "" || e.Device == device) && (prop == "" || e.Prop == prop) {
			out = append(out, e)
		}
	}
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	return out
}

// LogLen — число событий в журнале.
func (c *Context) LogLen() int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return len(c.log)
}

// ------------------------------------------------------------------ рендер

// FormatValue — значение с единицей измерения (для промпта).
func (c *Context) FormatValue(device, prop string, v any) string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.formatLocked(device, prop, v)
}

func (c *Context) formatLocked(device, prop string, v any) string {
	s := canon.Value(v)
	if d, ok := c.inv.Find(device); ok {
		for _, p := range d.Props {
			if p.Name == prop && p.Unit != "" && p.Unit != "/" {
				if _, isBool := v.(bool); !isBool {
					s += " " + p.Unit
				}
			}
		}
	}
	return s
}

// age — возраст значения в компактной записи.
func age(sec float64) string {
	switch {
	case sec < 60:
		return "<1м"
	case sec < 7200:
		return fmt.Sprintf("%dм", int(sec/60))
	default:
		return fmt.Sprintf("%dч", int(sec/3600))
	}
}

func (c *Context) devicesLocked() []string {
	return sortedKeys(c.state)
}

// renderSnapshotLocked — полный снимок L1: строка на устройство.
func (c *Context) renderSnapshotLocked(t float64) string {
	var b strings.Builder
	b.WriteString("Состояние устройств [возраст]:\n")
	for _, d := range c.devicesLocked() {
		props := sortedKeys(c.state[d])
		b.WriteString(d + ": ")
		latest := 0.0
		for i, p := range props {
			r := c.state[d][p]
			if i > 0 {
				b.WriteString(", ")
			}
			b.WriteString(p + "=" + c.formatLocked(d, p, r.Value))
			latest = math.Max(latest, r.T)
		}
		b.WriteString(" [" + c.ageLocked(d, t-latest) + "]\n")
	}
	return strings.TrimRight(b.String(), "\n")
}

// ageLocked — возраст показания устройства с отметкой о потере связи.
func (c *Context) ageLocked(device string, sec float64) string {
	if c.stale[device] {
		return age(sec) + ", нет связи"
	}
	return age(sec)
}

// SnapshotText — снимок L1 для режима snapshot_head. Текст пересчитывается не
// чаще одного раза в snapshot_period_s секунд (решение 5 концепции): между
// пересчётами префикс не меняется.
func (c *Context) SnapshotText(t float64) string {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.state) == 0 {
		return ""
	}
	if c.snapOK && t-c.snapT < c.period {
		return c.snapText
	}
	c.snapText, c.snapT, c.snapOK = c.renderSnapshotLocked(t), t, true
	return c.snapText
}

// Delta — значения, показанные LLM в дельте: Commit запоминает их после
// успешного ответа модели.
type Delta map[string]string

// staleMark — префикс показанного значения устройства без связи с сервером.
const staleMark = "?"

// DeltaText — изменения L1 с последнего подтверждённого показа LLM (режим
// delta_tail) и показанные значения. Первый вызов отдаёт всё состояние;
// потеря и восстановление связи с сервером устройства — тоже изменения.
// Показанное запоминается только Commit: если модель не ответила, следующий
// ход снова увидит те же изменения.
func (c *Context) DeltaText(t float64) (string, Delta) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	var lines []string
	shown := Delta{}
	for _, d := range c.devicesLocked() {
		stale := c.stale[d]
		for _, p := range sortedKeys(c.state[d]) {
			r := c.state[d][p]
			key := d + "." + p
			cur := canon.String(r.Value)
			if stale {
				cur = staleMark + cur
			}
			prev, had := c.sent[key]
			if had && prev == cur {
				continue
			}
			shown[key] = cur
			val := c.formatLocked(d, p, r.Value)
			line := key + "=" + val
			if old := strings.TrimPrefix(prev, staleMark); had && !stale && old != canon.String(r.Value) {
				line = key + ": " + old + "→" + val
			}
			lines = append(lines, line+" ["+c.ageLocked(d, t-r.T)+"]")
		}
	}
	if len(lines) == 0 {
		return "", nil
	}
	return "Изменения состояния с прошлого запроса [возраст]:\n" + strings.Join(lines, "\n"), shown
}

// Commit запоминает значения, которые модель увидела в успешно обработанном
// запросе.
func (c *Context) Commit(d Delta) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for k, v := range d {
		c.sent[k] = v
	}
}

// ------------------------------------------------------- разбор состояния

// metaKeys — служебные поля документа состояния, не являющиеся свойствами.
var metaKeys = map[string]bool{"device": true, "id": true, "device_id": true, "t": true, "ts": true,
	"time": true, "timestamp": true, "updated": true, "updated_at": true, "uri": true, "age_s": true,
	"stale": true, "unit": true, "units": true, "source": true}

// ParseState разбирает содержимое ресурса состояния устройства. Понимает:
// {"device": d, "properties"|"state"|"values": {p: v | {"value": v}}};
// {"device": d, "property": p, "value": v}; плоский объект {p: v, …};
// SenML-массив [{"bn":…, "n": p, "v"|"vb"|"vs": v}]. Устройство, если его нет в
// документе, берётся из URI (…/state/<device> или последний сегмент).
func ParseState(uri, raw string) (string, map[string]any, bool) {
	dec := json.NewDecoder(strings.NewReader(raw))
	dec.UseNumber()
	var doc any
	if err := dec.Decode(&doc); err != nil {
		return "", nil, false
	}
	device := DeviceOfURI(uri)
	switch x := doc.(type) {
	case map[string]any:
		d, props, ok := ParseStatePayload(x)
		if d != "" {
			device = d
		}
		return device, props, ok && device != ""
	case []any:
		props := map[string]any{}
		bn := ""
		for _, e := range x {
			m, ok := e.(map[string]any)
			if !ok {
				continue
			}
			if s, ok := m["bn"].(string); ok {
				bn = s
			}
			n, _ := m["n"].(string)
			for _, k := range []string{"v", "vb", "vs"} {
				if v, ok := m[k]; ok && n != "" {
					props[n] = normalizeNumber(v)
				}
			}
		}
		if device == "" && bn != "" {
			device = DeviceOfURI(strings.TrimRight(bn, ":/"))
		}
		return device, props, len(props) > 0 && device != ""
	}
	return "", nil, false
}

// ParseStatePayload разбирает объект состояния (содержимое ресурса или _meta
// уведомления resources/updated).
func ParseStatePayload(m map[string]any) (string, map[string]any, bool) {
	device := firstString(m, "device", "device_id", "id")
	for _, key := range []string{"properties", "state", "values"} {
		if ps, ok := m[key].(map[string]any); ok {
			props := map[string]any{}
			for p, v := range ps {
				if vm, ok := v.(map[string]any); ok {
					if inner, has := vm["value"]; has {
						v = inner
					}
				}
				props[p] = normalizeNumber(v)
			}
			return device, props, len(props) > 0
		}
	}
	if p, ok := m["property"].(string); ok {
		if v, has := m["value"]; has {
			return device, map[string]any{p: normalizeNumber(v)}, true
		}
	}
	props := map[string]any{}
	for k, v := range m {
		if metaKeys[k] {
			continue
		}
		if vm, ok := v.(map[string]any); ok {
			inner, has := vm["value"]
			if !has {
				continue
			}
			v = inner
		}
		if _, isList := v.([]any); isList {
			continue
		}
		props[k] = normalizeNumber(v)
	}
	return device, props, len(props) > 0
}

// DeviceOfURI — устройство из URI ресурса состояния: …/state/<device> или
// последний сегмент.
func DeviceOfURI(uri string) string {
	if uri == "" {
		return ""
	}
	if i := strings.Index(uri, "/state/"); i >= 0 {
		return strings.Trim(uri[i+len("/state/"):], "/")
	}
	s := strings.TrimRight(uri, "/")
	if i := strings.LastIndexAny(s, "/:"); i >= 0 {
		s = s[i+1:]
	}
	return s
}
