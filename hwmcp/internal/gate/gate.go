// Package gate — детерминированный шлюз команд исполнительным устройствам.
//
// Порт research/new_topic/GAP3_artifacts/actuator_gate.py. Шлюз стоит между внешней (недоверенной)
// LLM и приводами и проверяет каждую команду по порядку:
//
//  0. идемпотентность: повтор ключа команды возвращает прежнее решение без повторного срабатывания
//     (в прототипе эта проверка первая — повтор исполненной команды не зависит от текущего состояния);
//  1. реестр устройств и инструментов;
//  2. схема и диапазон аргументов (JSON Schema, целые — строго);
//  3. TTL решения LLM (решение устаревает: Gat 1998; cmd_vel_timeout в ROS 2);
//  4. свежесть состояния: блокировки проверяются только по свежим показаниям, неизвестное = опасное;
//  5. блокировки (interlocks) — предикаты над аргументами и состоянием; часть не отклоняет,
//     а ограничивает команду (clamp, «пропорциональное соблюдение»);
//  6. частота: минимальный интервал и не больше N принятых команд за скользящие 60 с;
//  7. подтверждение пользователя для необратимых инструментов.
//
// Плюс dry-run/commit (план проверяется заранее и заново при фиксации — защита от TOCTOU) и аренда со
// сторожевым тиком: непрерывный привод без продления уходит в безопасное состояние.
//
// Поведение совпадает с прототипом на одинаковых входах (тесты паритета на эталонных потоках из
// tools/gen_golden.py). Расширения: условия блокировок задаются данными (config.go), а не лямбдами;
// значение состояния nil (датчик не прочитан) считается отсутствующим; шлюз сообщает срез состояния,
// по которому принято решение (для журнала и независимой перепроверки монитором), и ближайший срок аренды.
//
// Шлюз не потокобезопасен: вызывающий сериализует обращения (в hw-mcp — мьютекс привода).
package gate

import (
	"encoding/json"
	"fmt"
	"strconv"

	"github.com/local/hwmcp/internal/argschema"
)

// Verdict — исход проверки.
type Verdict string

const (
	Allow     Verdict = "allow"
	Clamp     Verdict = "clamp"
	Confirm   Verdict = "confirm"
	Duplicate Verdict = "duplicate"
	Reject    Verdict = "reject"
)

// Причины отказа (как REASONS прототипа).
const (
	ReasonUnknownDevice = "unknown_device"
	ReasonUnknownTool   = "unknown_tool"
	ReasonInvalidArgs   = "invalid_args"
	ReasonStaleCommand  = "stale_command"
	ReasonStaleState    = "stale_state"
	ReasonInterlock     = "interlock"
	ReasonRateLimit     = "rate_limit"
	ReasonNeedsConfirm  = "needs_confirm"
	ReasonDuplicate     = "duplicate"
	ReasonCommitExpired = "commit_expired"
)

// Command — команда приводу.
type Command struct {
	CmdID     string  // ключ идемпотентности (задаёт оркестратор, не LLM)
	Device    string  // устройство
	Tool      string  // инструмент устройства (set_<свойство>, действие)
	Args      any     // аргументы (JSON после argschema.Decode)
	IssuedAt  float64 // момент, к которому относится решение LLM (часы шлюза, с)
	TTL       *float64
	Confirmed bool // подтверждение пользователя получено (ставит оркестратор)
	// NoIdempotency — ключ сгенерирован исполнителем (оркестратор ключа не дал): повтор такой команды
	// невозможен, поэтому ключ не ищется и не запоминается (и не может совпасть с ключом хоста).
	NoIdempotency bool
}

// StateSample — значение состояния и его возраст на момент решения.
type StateSample struct {
	Value any     `json:"v"`
	Age   float64 `json:"age_s"`
}

// Decision — решение шлюза.
type Decision struct {
	Verdict Verdict
	Reason  string // "" для allow/clamp
	Detail  string
	Args    any // итоговые аргументы (после clamp); nil для reject/confirm
	// State — значения ключей состояния, по которым проверялись блокировки (nil, если не проверялись).
	State map[string]StateSample
}

// Accepted — команда допущена к исполнению.
func (d Decision) Accepted() bool { return d.Verdict == Allow || d.Verdict == Clamp }

// StateReader — источник состояния (контекст оборудования): последнее значение и время его получения.
type StateReader interface {
	Get(key string) (value any, ts float64, ok bool)
}

// Device — привод в реестре шлюза.
type Device struct {
	Name            string
	Tools           map[string]*argschema.Schema
	ReadOnly        map[string]bool // инструменты чтения — без проверок актуации
	MinInterval     float64         // мин. интервал между принятыми командами, с (0 — нет)
	MaxPerMin       *int            // не больше N принятых команд за скользящие 60 с
	RequiresConfirm map[string]bool // инструменты, требующие подтверждения пользователя
	Lease           *float64        // аренда, с: команда действует, пока продлевается
	SafeState       map[string]any  // что выставить при истечении аренды
}

// Interlock — блокировка.
type Interlock struct {
	Name   string
	Device string
	Needs  []string // ключи состояния, от которых зависит проверка
	tools  toolMatcher
	deny   []denyRule
	clamp  []clampRule
}

type denyRule struct {
	when   *cond
	reason string
}

type clampRule struct {
	arg      string
	set      any
	hasSet   bool
	min, max *float64
}

// SafeEvent — срабатывание сторожевого таймера аренды.
type SafeEvent struct {
	TS        float64
	Device    string
	SafeState map[string]any
}

// Token — план, проверенный dry-run, для последующей фиксации.
type Token struct {
	Cmd     Command
	Expires float64
}

// Gate — шлюз приводов.
type Gate struct {
	devices    map[string]*Device
	interlocks []*Interlock
	state      StateReader

	DefaultTTL   float64
	MaxStateAge  float64
	CommitWindow float64
	IdemCapacity int
	// IdemMaxAge — ключи идемпотентности старше вытесняются (0 — только по ёмкости, как в прототипе).
	IdemMaxAge float64

	accepted map[string]*tsRing // устройство -> отметки принятых команд
	// seen — ключ исполненной команды -> её итоговые аргументы (канонический JSON): для повтора нужны
	// только они, а не всё решение со срезом состояния.
	seen     map[string]json.RawMessage
	seenFIFO []idemKey // ключи в порядке вставки (вытеснение самого старого, как popitem(last=False))
	seenHead int
	leases   leaseTable
	byTarget map[[2]string][]*Interlock

	SafeCount int // срабатываний сторожевого таймера; сами события — во внешней трассе
}

// New строит шлюз по политике cfg. extraTools — инструменты устройств, известные вызывающему
// (в hw-mcp — из инвентаря: устройство -> инструмент -> JSON Schema аргументов); они дополняют
// cfg.Devices[...].Tools. state — источник состояния для блокировок.
func New(cfg Config, extraTools map[string]map[string]any, state StateReader) (*Gate, error) {
	g := &Gate{
		devices:      map[string]*Device{},
		state:        state,
		DefaultTTL:   10.0,
		MaxStateAge:  5.0,
		CommitWindow: 5.0,
		IdemCapacity: 4096,
		accepted:     map[string]*tsRing{},
		seen:         map[string]json.RawMessage{},
		byTarget:     map[[2]string][]*Interlock{},
	}
	if cfg.DefaultTTL != nil {
		g.DefaultTTL = *cfg.DefaultTTL
	}
	if cfg.MaxStateAge != nil {
		g.MaxStateAge = *cfg.MaxStateAge
	}
	if cfg.CommitWindow != nil {
		g.CommitWindow = *cfg.CommitWindow
	}
	if cfg.IdemCapacity != nil {
		g.IdemCapacity = *cfg.IdemCapacity
	}
	if cfg.IdemMaxAge != nil {
		g.IdemMaxAge = *cfg.IdemMaxAge
	}
	if g.DefaultTTL < 0 || g.MaxStateAge < 0 || g.CommitWindow < 0 || g.IdemCapacity < 1 || g.IdemMaxAge < 0 {
		return nil, fmt.Errorf("gate config: negative timings or idem_capacity < 1")
	}

	for _, name := range sortedKeys(extraTools) {
		dev := g.device(name)
		for _, tool := range sortedKeys(extraTools[name]) {
			if err := dev.addTool(tool, extraTools[name][tool]); err != nil {
				return nil, err
			}
		}
	}
	for _, name := range sortedKeys(cfg.Devices) {
		dc := cfg.Devices[name]
		dev := g.device(name)
		for _, tool := range sortedKeys(dc.Tools) {
			raw, err := argschema.Decode(dc.Tools[tool])
			if err != nil {
				return nil, fmt.Errorf("gate: %s.%s schema: %w", name, tool, err)
			}
			if err := dev.addTool(tool, raw); err != nil {
				return nil, err
			}
		}
		if len(dev.Tools) == 0 {
			return nil, fmt.Errorf("gate: device %q has no tools (not in inventory and no \"tools\" in config)", name)
		}
		for _, t := range dc.ReadOnly {
			if dev.Tools[t] == nil {
				return nil, fmt.Errorf("gate: %s.read_only: unknown tool %q", name, t)
			}
			dev.ReadOnly[t] = true
		}
		for _, t := range dc.RequiresConfirm {
			if dev.Tools[t] == nil {
				return nil, fmt.Errorf("gate: %s.requires_confirm: unknown tool %q", name, t)
			}
			dev.RequiresConfirm[t] = true
		}
		if dc.MinInterval < 0 {
			return nil, fmt.Errorf("gate: %s.min_interval_s < 0", name)
		}
		dev.MinInterval = dc.MinInterval
		if dc.MaxPerMin != nil && *dc.MaxPerMin < 0 {
			return nil, fmt.Errorf("gate: %s.max_per_min < 0", name)
		}
		dev.MaxPerMin = dc.MaxPerMin
		if dc.Lease != nil && *dc.Lease <= 0 {
			return nil, fmt.Errorf("gate: %s.lease_s must be > 0", name)
		}
		dev.Lease = dc.Lease
		if dc.SafeState != nil {
			dev.SafeState = map[string]any{}
			for k, raw := range dc.SafeState {
				v, err := argschema.Decode(raw)
				if err != nil {
					return nil, fmt.Errorf("gate: %s.safe_state.%s: %w", name, k, err)
				}
				dev.SafeState[k] = v
			}
		}
		if dev.Lease != nil && dev.SafeState == nil {
			return nil, fmt.Errorf("gate: %s has lease_s but no safe_state", name)
		}
	}

	names := map[string]bool{}
	for i, ic := range cfg.Interlocks {
		il, err := g.compileInterlock(ic)
		if err != nil {
			return nil, fmt.Errorf("gate: interlocks[%d] %q: %w", i, ic.Name, err)
		}
		if names[il.Name] {
			return nil, fmt.Errorf("gate: duplicate interlock name %q", il.Name)
		}
		names[il.Name] = true
		g.interlocks = append(g.interlocks, il)
	}
	return g, nil
}

func (g *Gate) device(name string) *Device {
	if d, ok := g.devices[name]; ok {
		return d
	}
	d := &Device{Name: name, Tools: map[string]*argschema.Schema{}, ReadOnly: map[string]bool{},
		RequiresConfirm: map[string]bool{}}
	g.devices[name] = d
	return d
}

func (d *Device) addTool(tool string, raw any) error {
	if _, dup := d.Tools[tool]; dup {
		return fmt.Errorf("gate: tool %s.%s defined twice", d.Name, tool)
	}
	if m, ok := raw.(map[string]any); !ok || m["type"] != "object" {
		return fmt.Errorf("gate: %s.%s: argument schema must be an object schema with \"type\": \"object\"", d.Name, tool)
	}
	chk := argschema.CheckSchema(raw)
	if len(chk.Problems) > 0 {
		return fmt.Errorf("gate: %s.%s schema: %v", d.Name, tool, chk.Problems)
	}
	if len(chk.Unchecked) > 0 {
		// шлюз не должен молча пропускать ограничения, которые он не умеет проверять
		return fmt.Errorf("gate: %s.%s schema uses unsupported keywords %v", d.Name, tool, chk.Unchecked)
	}
	d.Tools[tool] = argschema.Compile(raw)
	return nil
}

func (g *Gate) compileInterlock(ic InterlockConfig) (*Interlock, error) {
	if ic.Name == "" {
		return nil, fmt.Errorf("name is required")
	}
	dev, ok := g.devices[ic.Device]
	if !ok {
		return nil, fmt.Errorf("unknown device %q", ic.Device)
	}
	tm, err := parseToolMatcher(ic.Tool)
	if err != nil {
		return nil, err
	}
	for t := range tm.names {
		if dev.Tools[t] == nil {
			return nil, fmt.Errorf("unknown tool %s.%s", ic.Device, t)
		}
	}
	if len(ic.Deny) == 0 {
		return nil, fmt.Errorf("at least one deny rule is required")
	}
	il := &Interlock{Name: ic.Name, Device: ic.Device, tools: tm}
	referenced := map[string]bool{}
	for j, r := range ic.Deny {
		c, err := compileCond(r.If)
		if err != nil {
			return nil, fmt.Errorf("deny[%d]: %w", j, err)
		}
		if r.Reason == "" {
			return nil, fmt.Errorf("deny[%d]: reason is required", j)
		}
		c.stateKeys(referenced)
		il.deny = append(il.deny, denyRule{when: c, reason: r.Reason})
	}
	if ic.Needs == nil {
		il.Needs = sortedKeys(referenced)
	} else {
		il.Needs = append([]string(nil), ic.Needs...)
		have := map[string]bool{}
		for _, k := range ic.Needs {
			have[k] = true
		}
		for k := range referenced {
			if !have[k] {
				return nil, fmt.Errorf("state key %q is used in conditions but missing in needs", k)
			}
		}
	}
	for _, arg := range sortedKeys(ic.Clamp) {
		cc := ic.Clamp[arg]
		r := clampRule{arg: arg, min: cc.Min, max: cc.Max}
		if len(cc.Set) > 0 {
			v, err := argschema.Decode(cc.Set)
			if err != nil {
				return nil, fmt.Errorf("clamp.%s.set: %w", arg, err)
			}
			r.set, r.hasSet = v, true
		}
		if r.hasSet == (r.min != nil || r.max != nil) {
			return nil, fmt.Errorf("clamp.%s: give either \"set\" or \"min\"/\"max\"", arg)
		}
		il.clamp = append(il.clamp, r)
	}
	return il, nil
}

// Devices — имена устройств реестра.
func (g *Gate) Devices() []string { return sortedKeys(g.devices) }

// Device — привод по имени (nil — нет в реестре).
func (g *Gate) Device(name string) *Device { return g.devices[name] }

// Interlocks — блокировки в порядке объявления.
func (g *Gate) Interlocks() []*Interlock { return append([]*Interlock(nil), g.interlocks...) }

// ----------------------------------------------------------------------------------------------
// Основная проверка

// Check проверяет команду и, если она принята, фиксирует её (частота, идемпотентность, аренда).
func (g *Gate) Check(cmd Command, now float64) Decision { return g.check(cmd, now, false) }

// CheckDry — проверка без фиксации (dry-run): не расходует лимит частоты и не запоминает ключ.
func (g *Gate) CheckDry(cmd Command, now float64) Decision { return g.check(cmd, now, true) }

func (g *Gate) check(cmd Command, now float64, dryRun bool) Decision {
	if !cmd.NoIdempotency {
		g.expireIdem(now)
		if prev, ok := g.seen[cmd.CmdID]; ok {
			var args any
			if prev != nil {
				args = prev
			}
			return Decision{Verdict: Duplicate, Reason: ReasonDuplicate, Detail: "команда с этим ключом уже исполнена", Args: args}
		}
	}
	dev := g.devices[cmd.Device]
	if dev == nil {
		return Decision{Verdict: Reject, Reason: ReasonUnknownDevice, Detail: cmd.Device}
	}
	schema := dev.Tools[cmd.Tool]
	if schema == nil {
		return Decision{Verdict: Reject, Reason: ReasonUnknownTool, Detail: cmd.Device + "." + cmd.Tool}
	}
	if errs := schema.Validate(cmd.Args, true); len(errs) > 0 {
		return Decision{Verdict: Reject, Reason: ReasonInvalidArgs, Detail: joinErrors(errs, 3)}
	}
	if dev.ReadOnly[cmd.Tool] {
		return Decision{Verdict: Allow, Args: cmd.Args}
	}

	ttl := g.DefaultTTL
	if cmd.TTL != nil {
		ttl = *cmd.TTL
	}
	if now-cmd.IssuedAt > ttl {
		return Decision{Verdict: Reject, Reason: ReasonStaleCommand,
			Detail: fmt.Sprintf("возраст %.1f с > TTL %s с", now-cmd.IssuedAt, pyFloat(ttl))}
	}

	args := cmd.Args
	clamped := false
	var used map[string]StateSample
	for _, il := range g.interlocksFor(cmd) {
		snap := make(map[string]any, len(il.Needs))
		for _, key := range il.Needs {
			v, ts, ok := g.readState(key)
			if !ok || now-ts > g.MaxStateAge {
				return Decision{Verdict: Reject, Reason: ReasonStaleState,
					Detail: fmt.Sprintf("%s: нет свежего значения %s", il.Name, key), State: used}
			}
			snap[key] = v
			if used == nil {
				used = map[string]StateSample{}
			}
			used[key] = StateSample{Value: v, Age: now - ts}
		}
		why, violated := il.violation(args.(map[string]any), snap)
		if violated {
			if len(il.clamp) == 0 || why.evalErr {
				return Decision{Verdict: Reject, Reason: ReasonInterlock, Detail: il.Name + ": " + why.text, State: used}
			}
			args = il.applyClamp(args.(map[string]any))
			clamped = true
		}
	}

	if dq := g.accepted[cmd.Device]; dq != nil && dq.len() > 0 {
		if dev.MinInterval != 0 && now-dq.last() < dev.MinInterval {
			return Decision{Verdict: Reject, Reason: ReasonRateLimit,
				Detail: fmt.Sprintf("интервал %.1f с < %s с", now-dq.last(), pyFloat(dev.MinInterval)), State: used}
		}
		if dev.MaxPerMin != nil {
			for dq.len() > 0 && now-dq.first() >= 60.0 {
				dq.popFirst()
			}
			if dq.len() >= *dev.MaxPerMin {
				return Decision{Verdict: Reject, Reason: ReasonRateLimit,
					Detail: fmt.Sprintf("%d команд за 60 с", dq.len()), State: used}
			}
		}
	}

	if dev.RequiresConfirm[cmd.Tool] && !cmd.Confirmed {
		return Decision{Verdict: Confirm, Reason: ReasonNeedsConfirm,
			Detail: fmt.Sprintf("%s.%s требует подтверждения", cmd.Device, cmd.Tool), State: used}
	}

	dec := Decision{Verdict: Allow, Args: args, State: used}
	if clamped {
		dec.Verdict = Clamp
	}
	if !dryRun {
		g.record(cmd, dev, dec, now)
	}
	return dec
}

// readState читает ключ состояния; значение nil (датчик не прочитан) считается отсутствующим.
func (g *Gate) readState(key string) (any, float64, bool) {
	if g.state == nil {
		return nil, 0, false
	}
	v, ts, ok := g.state.Get(key)
	if !ok || v == nil {
		return nil, 0, false
	}
	return v, ts, true
}

type violationReason struct {
	text    string
	evalErr bool
}

// violation — первое сработавшее правило блокировки (как check() прототипа: None или причина).
// Условие, которое нельзя вычислить, считается нарушением (fail-closed).
func (il *Interlock) violation(args map[string]any, snap map[string]any) (violationReason, bool) {
	for _, r := range il.deny {
		ok, err := r.when.eval(args, snap)
		if err != nil {
			return violationReason{text: "условие не вычисляется: " + err.Error(), evalErr: true}, true
		}
		if ok {
			return violationReason{text: r.reason}, true
		}
	}
	return violationReason{}, false
}

func (il *Interlock) applyClamp(args map[string]any) map[string]any {
	out := make(map[string]any, len(args)+len(il.clamp))
	for k, v := range args {
		out[k] = v
	}
	for _, c := range il.clamp {
		if c.hasSet {
			out[c.arg] = c.set
			continue
		}
		x, ok := toFloat(out[c.arg])
		if !ok {
			continue // аргумента нет или не число — схема уже проверена, ограничивать нечего
		}
		y := x
		if c.max != nil && y > *c.max {
			y = *c.max
		}
		if c.min != nil && y < *c.min {
			y = *c.min
		}
		if y != x {
			out[c.arg] = json.Number(strconv.FormatFloat(y, 'f', -1, 64))
		}
	}
	return out
}

func (g *Gate) interlocksFor(cmd Command) []*Interlock {
	key := [2]string{cmd.Device, cmd.Tool}
	lst, ok := g.byTarget[key]
	if !ok {
		for _, il := range g.interlocks {
			if il.Device == cmd.Device && il.tools.match(cmd.Tool) {
				lst = append(lst, il)
			}
		}
		g.byTarget[key] = lst
	}
	return lst
}

func (g *Gate) record(cmd Command, dev *Device, dec Decision, now float64) {
	// История ограничена: не больше max_per_min отметок (или одна — для min_interval).
	dq := g.accepted[cmd.Device]
	if dq == nil {
		capacity := 1
		if dev.MaxPerMin != nil && *dev.MaxPerMin > 0 {
			capacity = *dev.MaxPerMin
		}
		dq = newTSRing(capacity)
		g.accepted[cmd.Device] = dq
	}
	dq.push(now)
	if !cmd.NoIdempotency {
		g.remember(cmd.CmdID, dec.Args, now)
	}
	if dev.Lease != nil {
		g.leases.set(cmd.Device, now+*dev.Lease)
	}
}

type idemKey struct {
	id string
	ts float64
}

func (g *Gate) remember(id string, args any, now float64) {
	g.expireIdem(now)
	if len(g.seenFIFO)-g.seenHead >= g.IdemCapacity {
		g.popIdem() // заполнено: вытеснить самый старый ключ
	}
	var raw json.RawMessage
	if args != nil {
		if b, err := json.Marshal(args); err == nil {
			raw = b
		}
	}
	g.seen[id] = raw
	g.seenFIFO = append(g.seenFIFO, idemKey{id, now})
}

func (g *Gate) popIdem() {
	delete(g.seen, g.seenFIFO[g.seenHead].id)
	g.seenFIFO[g.seenHead] = idemKey{}
	g.seenHead++
	if g.seenHead >= 64 && 2*g.seenHead >= len(g.seenFIFO) { // сжать очередь: память не растёт
		n := copy(g.seenFIFO, g.seenFIFO[g.seenHead:])
		clear(g.seenFIFO[n:])
		g.seenFIFO, g.seenHead = g.seenFIFO[:n], 0
	}
}

// expireIdem вытесняет ключи старше IdemMaxAge: повтор после обрыва связи приходит за секунды,
// а команда старше TTL решения всё равно была бы отклонена как stale_command.
func (g *Gate) expireIdem(now float64) {
	if g.IdemMaxAge <= 0 {
		return
	}
	for g.seenHead < len(g.seenFIFO) && now-g.seenFIFO[g.seenHead].ts > g.IdemMaxAge {
		g.popIdem()
	}
}

// IdemSize — число запомненных ключей идемпотентности.
func (g *Gate) IdemSize() int { return len(g.seen) }

// ----------------------------------------------------------------------------------------------
// dry-run / commit

// DryRun проверяет команду без исполнения; для принятой возвращает токен фиксации.
func (g *Gate) DryRun(cmd Command, now float64) (Decision, *Token) {
	dec := g.check(cmd, now, true)
	if dec.Accepted() {
		return dec, &Token{Cmd: cmd, Expires: now + g.CommitWindow}
	}
	return dec, nil
}

// Commit фиксирует план: после окна фиксации — отказ, иначе повторная полная проверка (TOCTOU).
func (g *Gate) Commit(tok *Token, now float64) Decision {
	if now > tok.Expires {
		return Decision{Verdict: Reject, Reason: ReasonCommitExpired, Detail: "окно фиксации истекло"}
	}
	return g.check(tok.Cmd, now, false)
}

// ----------------------------------------------------------------------------------------------
// Аренда и сторожевой таймер

// Renew продлевает аренду устройства; false — у устройства нет действующей аренды.
func (g *Gate) Renew(device string, now float64) bool {
	dev := g.devices[device]
	if dev == nil || dev.Lease == nil || !g.leases.has(device) {
		return false
	}
	g.leases.set(device, now+*dev.Lease)
	return true
}

// Tick вызывается периодически (и при событиях): истёкшая аренда -> событие безопасного состояния.
func (g *Gate) Tick(now float64) []SafeEvent {
	var fired []SafeEvent
	for _, name := range g.leases.expired(now) {
		g.SafeCount++
		fired = append(fired, SafeEvent{TS: now, Device: name, SafeState: g.devices[name].SafeState})
	}
	return fired
}

// NextExpiry — ближайший срок аренды (для виртуальных часов: до какого момента можно не тикать).
func (g *Gate) NextExpiry() (float64, bool) { return g.leases.min() }

// LeaseExpiry — срок аренды устройства (ok=false — аренды нет).
func (g *Gate) LeaseExpiry(device string) (float64, bool) { return g.leases.get(device) }

// ----------------------------------------------------------------------------------------------
// Вспомогательное

func joinErrors(errs []argschema.Error, n int) string {
	s := ""
	for i, e := range errs {
		if i == n {
			break
		}
		if i > 0 {
			s += "; "
		}
		s += e.String()
	}
	return s
}

// pyFloat печатает число как str(float) Python для коротких значений (10.0, 2.5).
func pyFloat(f float64) string {
	s := strconv.FormatFloat(f, 'f', -1, 64)
	for _, c := range s {
		if c == '.' {
			return s
		}
	}
	return s + ".0"
}

// tsRing — кольцевой буфер отметок времени (deque(maxlen=N) прототипа).
type tsRing struct {
	buf   []float64
	start int
	n     int
}

func newTSRing(capacity int) *tsRing { return &tsRing{buf: make([]float64, capacity)} }

func (r *tsRing) len() int { return r.n }

func (r *tsRing) push(x float64) {
	if r.n == len(r.buf) { // как deque(maxlen): самый старый вытесняется
		r.buf[r.start] = x
		r.start = (r.start + 1) % len(r.buf)
		return
	}
	r.buf[(r.start+r.n)%len(r.buf)] = x
	r.n++
}

func (r *tsRing) first() float64 { return r.buf[r.start] }

func (r *tsRing) last() float64 { return r.buf[(r.start+r.n-1)%len(r.buf)] }

func (r *tsRing) popFirst() {
	r.start = (r.start + 1) % len(r.buf)
	r.n--
}

// leaseTable — сроки аренды в порядке вставки (как dict прототипа: повторная запись сохраняет место,
// удаление и новая запись — в конец), чтобы порядок одновременных событий совпадал.
type leaseTable struct {
	order []string
	exp   map[string]float64
}

func (t *leaseTable) has(name string) bool { _, ok := t.exp[name]; return ok }

func (t *leaseTable) get(name string) (float64, bool) { v, ok := t.exp[name]; return v, ok }

func (t *leaseTable) set(name string, until float64) {
	if t.exp == nil {
		t.exp = map[string]float64{}
	}
	if _, ok := t.exp[name]; !ok {
		t.order = append(t.order, name)
	}
	t.exp[name] = until
}

func (t *leaseTable) expired(now float64) []string {
	var out []string
	keep := t.order[:0]
	for _, name := range t.order {
		if now >= t.exp[name] {
			out = append(out, name)
			delete(t.exp, name)
		} else {
			keep = append(keep, name)
		}
	}
	t.order = keep
	return out
}

func (t *leaseTable) min() (float64, bool) {
	best, ok := 0.0, false
	for _, name := range t.order {
		if v := t.exp[name]; !ok || v < best {
			best, ok = v, true
		}
	}
	return best, ok
}
