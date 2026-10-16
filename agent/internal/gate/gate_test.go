package gate

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/local/edgectx/internal/clock"
	"github.com/local/edgectx/internal/config"
)

var epoch = time.Date(2026, 10, 6, 0, 0, 0, 0, time.UTC)

type rec struct {
	clk  clock.Clock
	mu   sync.Mutex
	log  []string
	wake []Wake
	sup  map[string]string // input -> reason
	fire []string
	der  []Event
}

func newRec() *rec { return &rec{sup: map[string]string{}} }

func (r *rec) Suppress(ev Event, reason string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if prev, dup := r.sup[ev.InputID]; dup {
		panic(fmt.Sprintf("input %s suppressed twice (%s, %s)", ev.InputID, prev, reason))
	}
	r.sup[ev.InputID] = reason
	r.log = append(r.log, "S:"+ev.InputID+":"+reason)
}
func (r *rec) Wake(w Wake) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.wake = append(r.wake, w)
	r.log = append(r.log, "W:"+strings.Join(w.InputIDs(), ","))
}
func (r *rec) RuleFired(rl Rule, ev Event) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.fire = append(r.fire, fmt.Sprintf("%s@%g:%s", rl.ID, clock.Seconds(r.clk), ev.InputID))
}
func (r *rec) Derived(ev Event) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.der = append(r.der, ev)
	r.log = append(r.log, "D:"+ev.InputID)
}

type fixture struct {
	t  *testing.T
	v  *clock.Virtual
	g  *Gate
	r  *rec
	n  int
	st map[string]any
}

func newFixture(t *testing.T, cfg config.Gate, wakes bool) *fixture {
	f := &fixture{t: t, v: clock.NewVirtual(epoch, 0), r: newRec(), st: map[string]any{}}
	f.r.clk = f.v
	f.g = New(f.v, Options{Config: cfg, Wakes: wakes, Rules: true, State: f}, f.r)
	return f
}

func (f *fixture) Get(device, prop string) (any, bool) {
	v, ok := f.st[device+"."+prop]
	return v, ok
}

// at продвигает часы до t секунд и подаёт событие.
func (f *fixture) at(t float64, device, prop string, value any) string {
	f.adv(t)
	f.n++
	id := fmt.Sprintf("s%d", f.n)
	f.st[device+"."+prop] = value
	f.g.OnEvent(Event{InputID: id, T: t, Kind: "sensor", Device: device, Prop: prop, Value: value})
	return id
}

func (f *fixture) adv(t float64) {
	f.t.Helper()
	if _, err := f.v.AdvanceTo(clock.At(f.v, t), nil); err != nil {
		f.t.Fatal(err)
	}
}

func fp(x float64) *float64 { return &x }

func TestChangeDebounce(t *testing.T) {
	f := newFixture(t, config.Gate{DebounceS: 2, Default: "change"}, true)
	a := f.at(100, "door1", "open", true)
	b := f.at(101, "door1", "open", false) // дребезг: вытесняет a
	c := f.at(101.5, "door1", "open", true)
	f.adv(110)
	if f.r.sup[a] != ReasonDebounce || f.r.sup[b] != ReasonDebounce {
		t.Fatalf("debounce: %v", f.r.sup)
	}
	if len(f.r.wake) != 1 || f.r.wake[0].Events[0].InputID != c {
		t.Fatalf("wakes %+v", f.r.wake)
	}
	if f.r.wake[0].Events[0].T != 103.5 {
		t.Fatalf("confirmation time %v", f.r.wake[0].Events[0].T)
	}
	// То же значение после подтверждения — no_change.
	d := f.at(200, "door1", "open", true)
	f.adv(210)
	if f.r.sup[d] != ReasonNoChange {
		t.Fatalf("no_change: %v", f.r.sup[d])
	}
}

// Нагрев при опросе раз в секунду: значения сменяются чаще debounce_s. Цепочка дебаунса ограничена
// 2·debounce_s, поэтому порог срабатывает во время нагрева (без предела — только после него, в 122 с).
func TestDebounceChainBounded(t *testing.T) {
	sig := config.Signal{Property: "temperature", Kind: "threshold", Above: fp(35), Hysteresis: 1, Priority: "safety"}
	f := newFixture(t, config.Gate{Signals: []config.Signal{sig}, DebounceS: 2}, true)
	for i := range 21 {
		f.at(100+float64(i), "k1", "temperature", 30+0.5*float64(i))
	}
	f.adv(130)
	if len(f.r.wake) != 1 {
		t.Fatalf("wakes %+v", f.r.wake)
	}
	if e := f.r.wake[0].Events[0]; e.T != 112 || e.Value != 35.5 {
		t.Fatalf("threshold confirmed at %v with %v, want 112 with 35.5", e.T, e.Value)
	}
}

func TestThresholdHysteresis(t *testing.T) {
	sig := config.Signal{Property: "temperature", Kind: "threshold", Above: fp(30), Hysteresis: 1, Priority: "safety"}
	f := newFixture(t, config.Gate{Signals: []config.Signal{sig}}, true)
	vals := []float64{25, 31, 32, 29.5, 28.9, 31}
	want := []string{ReasonBelowThreshold, "wake", ReasonThresholdHold, ReasonThresholdHold, ReasonThresholdClear, "wake"}
	for i, v := range vals {
		id := f.at(float64(10*(i+1)), "t1", "temperature", v)
		got := f.r.sup[id]
		if got == "" {
			got = "wake"
		}
		if got != want[i] {
			t.Fatalf("value %v: got %s want %s", v, got, want[i])
		}
	}
	if len(f.r.wake) != 2 || f.r.wake[0].Priority != PrioritySafety {
		t.Fatalf("wakes %+v", f.r.wake)
	}
}

func TestDelta(t *testing.T) {
	sig := config.Signal{Property: "lux", Kind: "delta", Delta: 2}
	f := newFixture(t, config.Gate{Signals: []config.Signal{sig}}, true)
	ids := []string{f.at(1, "l", "lux", 20), f.at(2, "l", "lux", 21), f.at(3, "l", "lux", 22.5), f.at(4, "l", "lux", 23), f.at(5, "l", "lux", "dark")}
	want := []string{ReasonBaseline, ReasonBelowDelta, "", ReasonBelowDelta, ReasonNotNumeric}
	for i, id := range ids {
		if f.r.sup[id] != want[i] {
			t.Fatalf("%d: got %q want %q", i, f.r.sup[id], want[i])
		}
	}
}

func TestEpisodeEnd(t *testing.T) {
	sig := config.Signal{Device: "pir*", Property: "motion", Kind: "episode", GapS: 600}
	f := newFixture(t, config.Gate{Signals: []config.Signal{sig}, DebounceS: 5}, true)
	f.at(1000, "pir1", "motion", 1)
	f.at(1100, "pir1", "motion", 0)
	last := f.at(1200, "pir1", "motion", 1)
	f.adv(1700)
	if len(f.r.wake) != 0 {
		t.Fatal("episode must not wake before gap")
	}
	f.adv(1900)
	if len(f.r.der) != 1 || len(f.r.wake) != 1 {
		t.Fatalf("derived=%d wakes=%d", len(f.r.der), len(f.r.wake))
	}
	d := f.r.der[0]
	if d.Derived != "episode_end" || d.DerivedFrom != last || d.T != 1800 {
		t.Fatalf("derived %+v", d)
	}
	if d.Extra["events"] != 3 || d.Extra["duration_s"] != 200.0 {
		t.Fatalf("extra %+v", d.Extra)
	}
	if f.r.wake[0].Events[0].InputID != d.InputID {
		t.Fatal("wake must reference derived input")
	}
	// Порядок: производный вход сообщается до пробуждения.
	if f.r.log[len(f.r.log)-2] != "D:"+d.InputID {
		t.Fatalf("log %v", f.r.log)
	}
}

func TestCooldown(t *testing.T) {
	f := newFixture(t, config.Gate{CooldownS: 300, Default: "change"}, true)
	f.at(0, "door1", "open", true)
	b := f.at(100, "door1", "open", false)
	f.at(400, "door1", "open", true)
	if len(f.r.wake) != 2 || f.r.sup[b] != ReasonCooldown {
		t.Fatalf("wakes=%d sup=%v", len(f.r.wake), f.r.sup)
	}
}

func TestCoalesce(t *testing.T) {
	sigs := []config.Signal{{Property: "leak", Kind: "change", Priority: "safety"}}
	f := newFixture(t, config.Gate{CoalesceS: 10, Default: "change", Signals: sigs}, true)
	a := f.at(0, "door1", "open", true)
	b := f.at(5, "sink", "leak", true)
	f.adv(9.9)
	if len(f.r.wake) != 0 {
		t.Fatal("window not closed yet")
	}
	f.adv(10)
	if len(f.r.wake) != 1 {
		t.Fatalf("wakes %d", len(f.r.wake))
	}
	w := f.r.wake[0]
	if strings.Join(w.InputIDs(), ",") != a+","+b || w.Priority != PrioritySafety || w.Reason != "gate" {
		t.Fatalf("wake %+v", w)
	}
	c := f.at(30, "door1", "open", false)
	f.adv(40)
	if len(f.r.wake) != 2 || f.r.wake[1].Events[0].InputID != c || f.r.wake[1].Priority != PriorityBackground {
		t.Fatalf("second window %+v", f.r.wake)
	}
}

func TestCoalesceMaxClosesWindowEarly(t *testing.T) {
	f := newFixture(t, config.Gate{CoalesceS: 60, CoalesceMax: 3, Default: "change"}, true)
	for i := 0; i < 4; i++ {
		f.at(float64(i), fmt.Sprintf("door%d", i), "open", true)
	}
	if len(f.r.wake) != 1 || len(f.r.wake[0].Events) != 3 {
		t.Fatalf("a full window must close at once: %+v", f.r.wake)
	}
	f.adv(63)
	if len(f.r.wake) != 2 || len(f.r.wake[1].Events) != 1 {
		t.Fatalf("the rest goes by the window timer: %+v", f.r.wake)
	}
}

func TestHeartbeatPolicySuppressesButRulesRun(t *testing.T) {
	f := newFixture(t, config.Gate{Default: "change"}, false)
	if _, err := f.g.AddRule(Rule{Device: "door1", Property: "open", Op: "eq", Value: true, Action: "notify", Text: "открыта"}); err != nil {
		t.Fatal(err)
	}
	a := f.at(10, "door1", "open", true)
	b := f.at(20, "t1", "temperature", 22)
	f.g.OnEvent(Event{InputID: "sit1", Kind: "situation", Name: "leak"})
	if f.r.sup[a] != ReasonRuleHandled || f.r.sup[b] != ReasonPolicyHeartbeat || f.r.sup["sit1"] != ReasonPolicyHeartbeat {
		t.Fatalf("%v", f.r.sup)
	}
	if len(f.r.fire) != 1 || len(f.r.wake) != 0 {
		t.Fatalf("fire=%v wakes=%d", f.r.fire, len(f.r.wake))
	}
}

func TestRuleWindowForAndRearm(t *testing.T) {
	f := newFixture(t, config.Gate{Default: "change"}, true)
	r, err := f.g.AddRule(Rule{Device: "door1", Property: "open", Op: "eq", Value: "open",
		ForS: 300, After: "22:00", Before: "06:00", Action: "notify", Text: "Дверь открыта ночью"})
	if err != nil || r.ID != "r1" {
		t.Fatalf("rule %+v %v", r, err)
	}
	h := func(hh, mm int) float64 { return float64(hh*3600 + mm*60) }
	// 21:50 — вне окна: событие идёт в гейт как обычно, правило ждёт окна.
	a := f.at(h(21, 50), "door1", "open", true)
	if f.r.sup[a] != "" || len(f.r.wake) != 1 {
		t.Fatalf("21:50 event must wake via gate: sup=%q wakes=%d", f.r.sup[a], len(f.r.wake))
	}
	f.adv(h(22, 4))
	if len(f.r.fire) != 0 {
		t.Fatal("fired too early")
	}
	f.adv(h(22, 6)) // 22:00 + 300 с
	if len(f.r.fire) != 1 || f.r.fire[0] != "r1@79500:"+a {
		t.Fatalf("fire %v", f.r.fire)
	}
	// Условие держится — повторно не срабатывает.
	f.adv(h(23, 0))
	if len(f.r.fire) != 1 {
		t.Fatal("level must not refire")
	}
	// Закрыли и открыли в окне: событие принадлежит правилу, срабатывание через 300 с.
	f.at(h(23, 10), "door1", "open", false)
	b := f.at(h(23, 20), "door1", "open", true)
	if f.r.sup[b] != ReasonRuleHandled {
		t.Fatalf("in-window event must be rule_handled, got %q", f.r.sup[b])
	}
	// Закрыли раньше for_s — срабатывания нет.
	f.at(h(23, 22), "door1", "open", false)
	f.adv(h(23, 40))
	if len(f.r.fire) != 1 {
		t.Fatalf("closed before for_s, fire=%v", f.r.fire)
	}
}

func TestRuleCooldownOnceAndChanged(t *testing.T) {
	f := newFixture(t, config.Gate{Default: "ignore"}, true)
	f.g.AddRule(Rule{ID: "c", Device: "btn", Property: "pressed", Op: "changed", Action: "wake", CooldownS: 60})
	f.g.AddRule(Rule{ID: "o", Device: "lamp*", Property: "on", Op: "eq", Value: true, Action: "tool", Tool: "write_property", Once: true})
	f.at(1, "btn", "pressed", 1)
	f.at(2, "btn", "pressed", 0) // cooldown
	f.at(100, "btn", "pressed", 1)
	f.at(105, "lamp1", "on", true)
	f.at(106, "lamp1", "on", false)
	f.at(107, "lamp1", "on", true) // правило once уже удалено
	got := strings.Join(f.r.fire, " ")
	if strings.Count(got, "c@") != 2 || strings.Count(got, "o@") != 1 {
		t.Fatalf("fire %v", f.r.fire)
	}
	if len(f.g.Rules()) != 1 {
		t.Fatalf("once rule not removed: %+v", f.g.Rules())
	}
	if !f.g.RemoveRule("c") || f.g.RemoveRule("c") {
		t.Fatal("RemoveRule")
	}
}

func TestAddRuleFiresOnKnownState(t *testing.T) {
	f := newFixture(t, config.Gate{Default: "change"}, true)
	f.at(10, "door1", "open", true)
	f.adv(20)
	if _, err := f.g.AddRule(Rule{Device: "door1", Property: "open", Op: "eq", Value: true, ForS: 60, Action: "notify", Text: "x"}); err != nil {
		t.Fatal(err)
	}
	f.adv(79)
	if len(f.r.fire) != 0 {
		t.Fatal("too early")
	}
	f.adv(81)
	if len(f.r.fire) != 1 {
		t.Fatalf("rule on known state must fire after for_s: %v", f.r.fire)
	}
}

func TestSituationWakesSafety(t *testing.T) {
	f := newFixture(t, config.Gate{Default: "change", CooldownS: 100}, true)
	f.g.OnEvent(Event{InputID: "x1", T: 5, Kind: "situation", Name: "door_open_at_night"})
	f.g.OnEvent(Event{InputID: "x2", T: 6, Kind: "situation", Name: "door_open_at_night"})
	if len(f.r.wake) != 1 || f.r.wake[0].Priority != PrioritySafety || f.r.sup["x2"] != ReasonCooldown {
		t.Fatalf("wakes %+v sup %v", f.r.wake, f.r.sup)
	}
}

func TestRuleValidation(t *testing.T) {
	bad := []Rule{
		{Property: "p", Op: "eq", Value: 1, Action: "notify", Text: "x"},
		{Device: "d", Property: "p", Op: "between", Value: 1, Action: "notify", Text: "x"},
		{Device: "d", Property: "p", Op: "eq", Action: "notify", Text: "x"},
		{Device: "d", Property: "p", Op: "eq", Value: 1, Action: "notify"},
		{Device: "d", Property: "p", Op: "eq", Value: 1, Action: "tool"},
		{Device: "d", Property: "p", Op: "eq", Value: 1, Action: "wake", After: "25:00"},
		{Device: "[", Property: "p", Op: "changed", Action: "wake"},
	}
	for i, r := range bad {
		if err := r.Validate(); err == nil {
			t.Errorf("rule %d must be invalid: %+v", i, r)
		}
	}
}

func TestInWindow(t *testing.T) {
	at := func(h, m int) time.Time { return epoch.Add(time.Duration(h)*time.Hour + time.Duration(m)*time.Minute) }
	cases := []struct {
		h, m          int
		after, before string
		want          bool
	}{
		{23, 0, "22:00", "06:00", true}, {3, 0, "22:00", "06:00", true}, {12, 0, "22:00", "06:00", false},
		{12, 0, "09:00", "18:00", true}, {18, 0, "09:00", "18:00", false}, {1, 0, "", "06:00", true},
		{23, 0, "22:00", "", true}, {12, 0, "", "", true},
	}
	for _, c := range cases {
		if got := inWindow(at(c.h, c.m), c.after, c.before); got != c.want {
			t.Errorf("%02d:%02d in [%s,%s) = %v", c.h, c.m, c.after, c.before, got)
		}
	}
	if d := untilWindow(at(21, 50), "22:00", "06:00"); d != 10*time.Minute {
		t.Errorf("untilWindow %v", d)
	}
	if d := untilWindow(at(7, 0), "22:00", "06:00"); d != 15*time.Hour {
		t.Errorf("untilWindow %v", d)
	}
}

func TestCompare(t *testing.T) {
	cases := []struct {
		op   string
		v, t any
		want bool
	}{
		{"eq", true, 1.0, true}, {"eq", 1.0, "on", true}, {"eq", "Open", "open", true}, {"ne", false, true, true},
		{"gt", 31.0, 30.0, true}, {"le", "29", 30.0, true}, {"gt", "abc", 1.0, false}, {"changed", nil, nil, true},
	}
	for _, c := range cases {
		if got := compare(c.op, c.v, c.t); got != c.want {
			t.Errorf("compare(%s, %v, %v) = %v", c.op, c.v, c.t, got)
		}
	}
}

func TestLoadRules(t *testing.T) {
	dir := t.TempDir()
	y := filepath.Join(dir, "r.yaml")
	os.WriteFile(y, []byte("rules:\n  - {id: n1, device: door1, property: open, op: eq, value: 1, action: notify, text: hi, for_s: 30}\n"), 0o644)
	j := filepath.Join(dir, "r.json")
	os.WriteFile(j, []byte(`[{"id":"t1","device":"fan","property":"on","op":"changed","action":"tool","tool":"write_property","args":{"value":1}}]`), 0o644)
	ry, err := LoadRules(y)
	if err != nil || len(ry) != 1 || ry[0].Value != 1.0 || ry[0].Source != "file" || ry[0].ForS != 30 {
		t.Fatalf("yaml: %+v %v", ry, err)
	}
	rj, err := LoadRules(j)
	if err != nil || len(rj) != 1 || rj[0].Args["value"] != 1.0 {
		t.Fatalf("json: %+v %v", rj, err)
	}
	bad := filepath.Join(dir, "bad.yaml")
	os.WriteFile(bad, []byte("- {id: x, device: d, property: p, op: eq, action: notify}\n"), 0o644)
	if _, err := LoadRules(bad); err == nil {
		t.Fatal("invalid rule file must fail")
	}
}

func TestDrainGivesEveryInputAnOutcome(t *testing.T) {
	f := newFixture(t, config.Gate{DebounceS: 5, CoalesceS: 60, Default: "change"}, true)
	a := f.at(10, "door1", "open", true) // в окне коалесценции после дебаунса
	f.adv(20)
	b := f.at(21, "t1", "temperature", 25) // ещё в дебаунсе
	f.g.Drain("shutdown")
	if f.r.sup[a] != "shutdown" || f.r.sup[b] != "shutdown" || len(f.r.wake) != 0 {
		t.Fatalf("sup=%v wakes=%d", f.r.sup, len(f.r.wake))
	}
	f.adv(1000)
	if len(f.r.wake) != 0 {
		t.Fatal("timers must be stopped after Drain")
	}
}
