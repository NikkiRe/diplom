// Package gate — событийный гейт: решает, будить ли LLM по событию датчика.
//
// Конвейер для события датчика (ключ — устройство.свойство):
//
//  1. дебаунс: значение должно продержаться debounce_s, иначе оно вытесняется
//     следующим (suppress: debounce); при debounce_s = 0 — сразу. Если значения
//     сменяют друг друга дольше debounceChainMax·debounce_s подряд, это не
//     дребезг, а непрерывное изменение (быстрый нагрев, шум датчика больше шага
//     квантования): подтверждается текущее значение;
//  2. подтверждённое значение, равное прежнему, отбрасывается (no_change);
//  3. локальные правила: если условие правила истинно (в его окне времени),
//     событие обрабатывается правилом без LLM (rule_handled), а правило
//     срабатывает по фронту, возможно с задержкой for_s;
//  4. эхо собственной команды агента (self_caused) и политика heartbeat
//     (policy_heartbeat) — событие не будит LLM;
//  5. значимость по виду сигнала: change, threshold (порог с гистерезисом), delta,
//     episode («конец эпизода» после gap_s тишины), ignore;
//  6. cooldown по ключу;
//  7. коалесценция: значимые события копятся в окне coalesce_s и уходят одним
//     пробуждением LLM с наивысшим приоритетом из окна; набрав coalesce_max
//     событий, окно закрывается досрочно (память и размер промпта ограничены).
//
// Каждое входное событие получает ровно один исход: suppress с причиной либо
// участие в одном пробуждении (Wake.Events). Производные события (конец эпизода)
// сначала сообщаются как новые входы (Sink.Derived).
//
// Все таймеры — только через clock.Clock.
package gate

import (
	"fmt"
	"math"
	"sort"
	"strings"
	"sync"

	"github.com/local/edgectx/internal/clock"
	"github.com/local/edgectx/internal/config"
)

// Причины подавления (поле reason записи suppress).
const (
	ReasonDebounce        = "debounce"
	ReasonNoChange        = "no_change"
	ReasonRuleHandled     = "rule_handled"
	ReasonPolicyHeartbeat = "policy_heartbeat"
	ReasonIgnored         = "ignored"
	ReasonBelowThreshold  = "below_threshold"
	ReasonThresholdHold   = "threshold_hold"
	ReasonThresholdClear  = "threshold_clear"
	ReasonNotNumeric      = "not_numeric"
	ReasonBaseline        = "baseline"
	ReasonBelowDelta      = "below_delta"
	ReasonEpisode         = "episode"
	ReasonCooldown        = "cooldown"
	ReasonSelfCaused      = "self_caused"
)

// debounceChainMax — предел цепочки дебаунса в долях debounce_s (допущение). Без него датчик,
// меняющийся чаще debounce_s (hw-mcp опрашивает раз в секунду), не получил бы ни одного
// подтверждённого значения, пока меняется: порог перегрева при нагреве не сработал бы вовсе.
const debounceChainMax = 2

// Приоритеты пробуждения от гейта.
const (
	PrioritySafety     = "safety"
	PriorityBackground = "background"
)

// Event — входное событие гейта.
type Event struct {
	InputID string  `json:"input_id"`
	T       float64 `json:"t"`    // секунды часов агента
	Kind    string  `json:"kind"` // sensor | situation
	Device  string  `json:"device,omitempty"`
	Prop    string  `json:"property,omitempty"`
	Value   any     `json:"value,omitempty"`
	Old     any     `json:"old,omitempty"`
	// Name, Text — для ситуаций (внешних значимых событий).
	Name string `json:"name,omitempty"`
	Text string `json:"text,omitempty"`
	// Derived — вид производного события (episode_end); DerivedFrom — вход-источник.
	Derived     string         `json:"derived,omitempty"`
	DerivedFrom string         `json:"derived_from,omitempty"`
	Extra       map[string]any `json:"extra,omitempty"`
	// Priority заполняется гейтом для событий, попавших в пробуждение.
	Priority string `json:"priority,omitempty"`
	// Self — изменение вызвано командой самого агента (эхо записи в привод):
	// правила его видят, но LLM оно не будит.
	Self bool `json:"self,omitempty"`
}

// Key — ключ события (устройство.свойство или situation:имя).
func (e Event) Key() string {
	if e.Kind == "situation" {
		n := e.Name
		if n == "" {
			n = e.InputID
		}
		return "situation:" + n
	}
	return e.Device + "." + e.Prop
}

// Wake — решение разбудить LLM.
type Wake struct {
	Reason   string  // gate | rule
	Priority string  // safety | background
	Events   []Event // события окна коалесценции (или событие правила)
	RuleID   string
}

// InputIDs — идентификаторы входов пробуждения.
func (w Wake) InputIDs() []string {
	ids := make([]string, 0, len(w.Events))
	for _, e := range w.Events {
		ids = append(ids, e.InputID)
	}
	return ids
}

// Sink получает исходы гейта. Методы вызываются последовательно, в порядке
// принятия решений, без удержания внутренних блокировок гейта.
type Sink interface {
	Suppress(ev Event, reason string)
	Wake(w Wake)
	RuleFired(r Rule, ev Event)
	Derived(ev Event)
}

// StateReader даёт гейту текущее значение свойства (для проверки нового правила
// по уже известному состоянию).
type StateReader interface {
	Get(device, prop string) (any, bool)
}

// Options — параметры гейта.
type Options struct {
	Config config.Gate
	// Wakes — будит ли гейт LLM (false для политики heartbeat).
	Wakes bool
	// Rules — исполнять ли локальные правила.
	Rules bool
	// NewInputID выдаёт идентификатор производного входа (конец эпизода).
	NewInputID func(prefix string) string
	State      StateReader
}

// Gate — событийный гейт. Потокобезопасен.
type Gate struct {
	clk  clock.Clock
	opt  Options
	sink Sink

	mu     sync.Mutex
	emitMu sync.Mutex // сохраняет порядок доставки исходов
	keys   map[string]*keyState
	rules  []*ruleState
	buf    []Event // окно коалесценции
	bufTm  clock.Timer
	nextID int
	stats  map[string]int
}

type keyState struct {
	confirmed    any
	hasConfirmed bool
	pending      *Event
	pendingSince float64 // начало цепочки дебаунса: первое из ещё не подтверждённых значений
	debounceTm   clock.Timer
	lastWake     float64
	hasWake      bool
	alarmAbove   bool
	alarmBelow   bool
	lastSig      float64
	hasLastSig   bool
	epOpen       bool
	epStart      float64
	epLast       float64
	epCount      int
	epLastEv     Event
	epTm         clock.Timer
}

type ruleState struct {
	r    Rule
	inst map[string]*ruleInst // по ключу устройство.свойство
}

type ruleInst struct {
	condTrue  bool
	trigger   Event
	timer     clock.Timer // for_s или ожидание окна времени
	lastFired float64
	hasFired  bool
}

// outcome — отложенный до снятия блокировки исход.
type outcome struct {
	kind   string // suppress | wake | rule | derived
	ev     Event
	reason string
	wake   Wake
	rule   Rule
}

// New создаёт гейт.
func New(clk clock.Clock, opt Options, sink Sink) *Gate {
	if opt.NewInputID == nil {
		n := 0
		opt.NewInputID = func(prefix string) string { n++; return fmt.Sprintf("%s-%d", prefix, n) }
	}
	return &Gate{clk: clk, opt: opt, sink: sink, keys: map[string]*keyState{}, stats: map[string]int{}}
}

// run выполняет f под блокировкой состояния и доставляет исходы по порядку.
func (g *Gate) run(f func(out *[]outcome)) {
	var out []outcome
	g.mu.Lock()
	f(&out)
	g.emitMu.Lock()
	g.mu.Unlock()
	defer g.emitMu.Unlock()
	for _, o := range out {
		switch o.kind {
		case "suppress":
			g.sink.Suppress(o.ev, o.reason)
		case "wake":
			g.sink.Wake(o.wake)
		case "rule":
			g.sink.RuleFired(o.rule, o.ev)
		case "derived":
			g.sink.Derived(o.ev)
		}
	}
}

func (g *Gate) now() float64 { return clock.Seconds(g.clk) }

func (g *Gate) suppress(out *[]outcome, ev Event, reason string) {
	g.stats["suppress:"+reason]++
	*out = append(*out, outcome{kind: "suppress", ev: ev, reason: reason})
}

// OnEvent обрабатывает входное событие (датчик или ситуация).
func (g *Gate) OnEvent(ev Event) {
	if ev.T == 0 {
		ev.T = g.now()
	}
	g.run(func(out *[]outcome) {
		g.stats["events"]++
		if ev.Kind == "situation" {
			g.significant(out, ev, PrioritySafety, nil)
			return
		}
		ev.Kind = "sensor"
		sig := g.signalFor(ev.Device, ev.Prop)
		ks := g.key(ev.Key())
		debounce := g.opt.Config.DebounceS
		if sig.DebounceS != nil {
			debounce = *sig.DebounceS
		}
		if debounce <= 0 || sig.Kind == "episode" {
			// Для эпизодов важна каждая активность, дребезг не фильтруется.
			g.confirm(out, ks, ev, sig)
			return
		}
		now := g.now()
		if ks.pending == nil {
			ks.pendingSince = now
		} else {
			ks.debounceTm.Stop()
			g.suppress(out, *ks.pending, ReasonDebounce)
		}
		wait := min(debounce, ks.pendingSince+debounceChainMax*debounce-now)
		if wait <= 0 {
			ks.pending = nil
			g.confirm(out, ks, ev, sig)
			return
		}
		pending := ev
		ks.pending = &pending
		ks.debounceTm = g.clk.AfterFunc(config.Seconds(wait), func() {
			g.run(func(out *[]outcome) {
				if ks.pending == nil {
					return
				}
				e := *ks.pending
				ks.pending = nil
				e.T = g.now()
				g.confirm(out, ks, e, g.signalFor(e.Device, e.Prop))
			})
		})
	})
}

// confirm обрабатывает подтверждённое (после дебаунса) значение.
func (g *Gate) confirm(out *[]outcome, ks *keyState, ev Event, sig config.Signal) {
	if ks.hasConfirmed && sameValue(ks.confirmed, ev.Value) && sig.Kind != "episode" {
		g.suppress(out, ev, ReasonNoChange)
		return
	}
	ks.confirmed, ks.hasConfirmed = ev.Value, true

	if g.opt.Rules && g.evalRules(out, ev) {
		g.suppress(out, ev, ReasonRuleHandled)
		return
	}
	if ev.Self {
		g.suppress(out, ev, ReasonSelfCaused)
		return
	}
	if !g.opt.Wakes {
		g.suppress(out, ev, ReasonPolicyHeartbeat)
		return
	}
	prio := sig.Priority
	if prio == "" {
		prio = PriorityBackground
	}
	switch sig.Kind {
	case "ignore":
		g.suppress(out, ev, ReasonIgnored)
	case "change":
		g.significant(out, ev, prio, &sig)
	case "threshold":
		g.threshold(out, ks, ev, sig, prio)
	case "delta":
		x, ok := toFloat(ev.Value)
		switch {
		case !ok:
			g.suppress(out, ev, ReasonNotNumeric)
		case !ks.hasLastSig:
			ks.lastSig, ks.hasLastSig = x, true
			g.suppress(out, ev, ReasonBaseline)
		case math.Abs(x-ks.lastSig) >= sig.Delta:
			ks.lastSig = x
			g.significant(out, ev, prio, &sig)
		default:
			g.suppress(out, ev, ReasonBelowDelta)
		}
	case "episode":
		g.episode(out, ks, ev, sig, prio)
	default:
		g.significant(out, ev, prio, &sig)
	}
}

func (g *Gate) threshold(out *[]outcome, ks *keyState, ev Event, sig config.Signal, prio string) {
	x, ok := toFloat(ev.Value)
	if !ok {
		g.suppress(out, ev, ReasonNotNumeric)
		return
	}
	entered, cleared, hold := false, false, false
	if sig.Above != nil {
		switch {
		case !ks.alarmAbove && x > *sig.Above:
			ks.alarmAbove, entered = true, true
		case ks.alarmAbove && x < *sig.Above-sig.Hysteresis:
			ks.alarmAbove, cleared = false, true
		case ks.alarmAbove:
			hold = true
		}
	}
	if sig.Below != nil {
		switch {
		case !ks.alarmBelow && x < *sig.Below:
			ks.alarmBelow, entered = true, true
		case ks.alarmBelow && x > *sig.Below+sig.Hysteresis:
			ks.alarmBelow, cleared = false, true
		case ks.alarmBelow:
			hold = true
		}
	}
	switch {
	case entered:
		g.significant(out, ev, prio, &sig)
	case cleared:
		g.suppress(out, ev, ReasonThresholdClear)
	case hold:
		g.suppress(out, ev, ReasonThresholdHold)
	default:
		g.suppress(out, ev, ReasonBelowThreshold)
	}
}

func (g *Gate) episode(out *[]outcome, ks *keyState, ev Event, sig config.Signal, prio string) {
	if !ks.epOpen {
		ks.epOpen, ks.epStart, ks.epCount = true, ev.T, 0
	}
	ks.epCount++
	ks.epLast = ev.T
	ks.epLastEv = ev
	if ks.epTm != nil {
		ks.epTm.Stop()
	}
	g.suppress(out, ev, ReasonEpisode)
	ks.epTm = g.clk.AfterFunc(config.Seconds(sig.GapS), func() {
		g.run(func(out *[]outcome) {
			if !ks.epOpen {
				return
			}
			ks.epOpen = false
			last := ks.epLastEv
			d := Event{
				InputID: g.opt.NewInputID("ep"), T: g.now(), Kind: "sensor",
				Device: last.Device, Prop: last.Prop, Value: last.Value,
				Derived: "episode_end", DerivedFrom: last.InputID,
				Extra: map[string]any{"events": ks.epCount, "start_t": ks.epStart,
					"end_t": ks.epLast, "duration_s": ks.epLast - ks.epStart},
			}
			*out = append(*out, outcome{kind: "derived", ev: d})
			g.significant(out, d, prio, &sig)
		})
	})
}

// significant проводит значимое событие через cooldown в окно коалесценции.
func (g *Gate) significant(out *[]outcome, ev Event, prio string, sig *config.Signal) {
	if !g.opt.Wakes {
		g.suppress(out, ev, ReasonPolicyHeartbeat)
		return
	}
	cooldown := g.opt.Config.CooldownS
	if sig != nil && sig.CooldownS != nil {
		cooldown = *sig.CooldownS
	}
	ks := g.key(ev.Key())
	if ks.hasWake && cooldown > 0 && ev.T-ks.lastWake < cooldown {
		g.suppress(out, ev, ReasonCooldown)
		return
	}
	ks.lastWake, ks.hasWake = ev.T, true
	ev.Priority = prio
	g.buf = append(g.buf, ev)
	g.stats["significant"]++
	if c := g.opt.Config; c.CoalesceS <= 0 || (c.CoalesceMax > 0 && len(g.buf) >= c.CoalesceMax) {
		g.flush(out)
		return
	}
	if g.bufTm == nil {
		g.bufTm = g.clk.AfterFunc(config.Seconds(g.opt.Config.CoalesceS), func() {
			g.run(func(out *[]outcome) { g.flush(out) })
		})
	}
}

func (g *Gate) flush(out *[]outcome) {
	if g.bufTm != nil {
		g.bufTm.Stop()
		g.bufTm = nil
	}
	if len(g.buf) == 0 {
		return
	}
	prio := PriorityBackground
	for _, e := range g.buf {
		if e.Priority == PrioritySafety {
			prio = PrioritySafety
		}
	}
	w := Wake{Reason: "gate", Priority: prio, Events: g.buf}
	g.buf = nil
	g.stats["wakes"]++
	*out = append(*out, outcome{kind: "wake", wake: w})
}

// Flush немедленно закрывает окно коалесценции.
func (g *Gate) Flush() {
	g.run(func(out *[]outcome) { g.flush(out) })
}

// Drain при остановке агента подавляет с причиной reason все входы, ещё не
// получившие исхода (ожидающие дебаунса и окна коалесценции), и останавливает
// таймеры: у каждого входа в трассе остаётся ровно один исход.
func (g *Gate) Drain(reason string) {
	g.run(func(out *[]outcome) {
		for _, k := range sortedKeyNames(g.keys) {
			ks := g.keys[k]
			if ks.pending != nil {
				ks.debounceTm.Stop()
				g.suppress(out, *ks.pending, reason)
				ks.pending = nil
			}
			if ks.epTm != nil {
				ks.epTm.Stop()
				ks.epOpen = false
			}
		}
		if g.bufTm != nil {
			g.bufTm.Stop()
			g.bufTm = nil
		}
		for _, e := range g.buf {
			g.suppress(out, e, reason)
		}
		g.buf = nil
		for _, rs := range g.rules {
			for _, in := range rs.inst {
				if in.timer != nil {
					in.timer.Stop()
					in.timer = nil
				}
			}
		}
	})
}

func sortedKeyNames(m map[string]*keyState) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func (g *Gate) key(k string) *keyState {
	ks := g.keys[k]
	if ks == nil {
		ks = &keyState{}
		g.keys[k] = ks
	}
	return ks
}

// signalFor — первый подходящий сигнал из конфигурации или сигнал по умолчанию.
func (g *Gate) signalFor(device, prop string) config.Signal {
	for _, s := range g.opt.Config.Signals {
		if globMatch(s.Device, device) && globMatch(s.Property, prop) {
			return s
		}
	}
	kind := g.opt.Config.Default
	if kind == "" {
		kind = "change"
	}
	return config.Signal{Kind: kind}
}

// ----------------------------------------------------------------- правила

// evalRules проверяет правила, относящиеся к событию. Возвращает true, если
// событие «принадлежит» правилу (условие истинно в окне правила).
func (g *Gate) evalRules(out *[]outcome, ev Event) bool {
	handled := false
	now := g.clk.Now()
	for _, rs := range g.rules {
		r := &rs.r
		if !r.Matches(ev.Device, ev.Prop) {
			continue
		}
		key := ev.Key()
		in := rs.inst[key]
		if in == nil {
			in = &ruleInst{}
			rs.inst[key] = in
		}
		cond := compare(r.Op, ev.Value, r.Value)
		if !cond {
			in.condTrue = false
			if in.timer != nil {
				in.timer.Stop()
				in.timer = nil
			}
			continue
		}
		window := inWindow(now, r.After, r.Before)
		if window {
			handled = true
		}
		if r.Op == "changed" {
			if window {
				g.fireRule(out, rs, in, ev)
			}
			continue
		}
		if in.condTrue {
			continue // фронт уже был; ждём, пока условие станет ложным
		}
		in.condTrue = true
		in.trigger = ev
		g.armRule(rs, in)
		if window && r.ForS <= 0 {
			g.fireRule(out, rs, in, ev)
		}
	}
	return handled
}

// armRule заводит таймер правила: ожидание окна времени и/или for_s.
func (g *Gate) armRule(rs *ruleState, in *ruleInst) {
	if in.timer != nil {
		in.timer.Stop()
		in.timer = nil
	}
	r := &rs.r
	wait := untilWindow(g.clk.Now(), r.After, r.Before)
	if wait == 0 && r.ForS <= 0 {
		return // сработает сразу (решает вызывающий)
	}
	delay := wait + config.Seconds(r.ForS)
	in.timer = g.clk.AfterFunc(delay, func() {
		g.run(func(out *[]outcome) {
			in.timer = nil
			if !in.condTrue || !g.hasRule(rs) {
				return
			}
			if !inWindow(g.clk.Now(), r.After, r.Before) {
				g.armRule(rs, in) // окно успело закрыться — ждём следующего
				return
			}
			ev := in.trigger
			g.fireRule(out, rs, in, ev)
		})
	})
}

func (g *Gate) fireRule(out *[]outcome, rs *ruleState, in *ruleInst, ev Event) {
	r := rs.r
	now := g.now()
	if in.hasFired && r.CooldownS > 0 && now-in.lastFired < r.CooldownS {
		g.stats["rule_cooldown"]++
		return
	}
	in.lastFired, in.hasFired = now, true
	g.stats["rule_fired"]++
	*out = append(*out, outcome{kind: "rule", rule: r, ev: ev})
	if r.Once {
		g.removeRuleLocked(r.ID)
	}
}

func (g *Gate) hasRule(rs *ruleState) bool {
	for _, x := range g.rules {
		if x == rs {
			return true
		}
	}
	return false
}

// AddRule добавляет или заменяет правило (по ID; пустой ID назначается) и сразу
// проверяет его по текущему состоянию. Возвращает правило с назначенным ID.
func (g *Gate) AddRule(r Rule) (Rule, error) {
	if err := r.Validate(); err != nil {
		return r, err
	}
	var res Rule
	g.run(func(out *[]outcome) {
		if r.ID == "" {
			g.nextID++
			r.ID = fmt.Sprintf("r%d", g.nextID)
			for g.findRule(r.ID) >= 0 {
				g.nextID++
				r.ID = fmt.Sprintf("r%d", g.nextID)
			}
		}
		g.removeRuleLocked(r.ID)
		rs := &ruleState{r: r, inst: map[string]*ruleInst{}}
		g.rules = append(g.rules, rs)
		sort.SliceStable(g.rules, func(i, j int) bool { return g.rules[i].r.ID < g.rules[j].r.ID })
		res = r
		// Начальное состояние: если условие уже истинно (дверь уже открыта),
		// правило взводится сразу. Шаблоны устройств здесь не раскрываются.
		if g.opt.State != nil && !strings.ContainsAny(r.Device+r.Property, "*?[") && r.Op != "changed" {
			if v, ok := g.opt.State.Get(r.Device, r.Property); ok && compare(r.Op, v, r.Value) {
				key := r.Device + "." + r.Property
				in := &ruleInst{condTrue: true, trigger: Event{InputID: "rule:" + r.ID, T: g.now(),
					Kind: "sensor", Device: r.Device, Prop: r.Property, Value: v}}
				rs.inst[key] = in
				g.armRule(rs, in)
				if in.timer == nil {
					g.fireRule(out, rs, in, in.trigger)
				}
			}
		}
	})
	return res, nil
}

// RemoveRule удаляет правило; false — правила нет.
func (g *Gate) RemoveRule(id string) bool {
	var ok bool
	g.run(func(out *[]outcome) { ok = g.removeRuleLocked(id) })
	return ok
}

func (g *Gate) removeRuleLocked(id string) bool {
	i := g.findRule(id)
	if i < 0 {
		return false
	}
	for _, in := range g.rules[i].inst {
		if in.timer != nil {
			in.timer.Stop()
		}
	}
	g.rules = append(g.rules[:i], g.rules[i+1:]...)
	return true
}

func (g *Gate) findRule(id string) int {
	for i, rs := range g.rules {
		if rs.r.ID == id {
			return i
		}
	}
	return -1
}

// Rules — копия списка правил (по ID).
func (g *Gate) Rules() []Rule {
	g.mu.Lock()
	defer g.mu.Unlock()
	out := make([]Rule, 0, len(g.rules))
	for _, rs := range g.rules {
		out = append(out, rs.r)
	}
	return out
}

// Stats — счётчики гейта.
func (g *Gate) Stats() map[string]int {
	g.mu.Lock()
	defer g.mu.Unlock()
	out := make(map[string]int, len(g.stats))
	for k, v := range g.stats {
		out[k] = v
	}
	return out
}
