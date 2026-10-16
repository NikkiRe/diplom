package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"runtime"
	"sort"
	"strings"
	"time"

	"github.com/local/edgectx/internal/canon"
	"github.com/local/edgectx/internal/clock"
	"github.com/local/edgectx/internal/config"
	"github.com/local/edgectx/internal/gate"
	"github.com/local/edgectx/internal/llm"
	"github.com/local/edgectx/internal/sched"
	"github.com/local/edgectx/internal/trace"
	"github.com/local/edgectx/internal/userctx"
)

func obj(props map[string]any, required ...string) map[string]any {
	s := map[string]any{"type": "object", "properties": props}
	if len(required) > 0 {
		s["required"] = required
	}
	return s
}

var (
	tString = map[string]any{"type": "string"}
	tNumber = map[string]any{"type": "number"}
)

func enumOf(vals ...string) map[string]any {
	l := make([]any, len(vals))
	for i, v := range vals {
		l[i] = v
	}
	return map[string]any{"type": "string", "enum": l}
}

// localToolDef — описание встроенного инструмента агента.
func localToolDef(name string) llm.Tool {
	var desc string
	var params map[string]any
	switch name {
	case "notify_user":
		desc = "Send a notification to the user."
		params = obj(map[string]any{"text": tString, "situation_id": tString}, "text")
	case "create_rule":
		desc = "Create a local rule executed by the agent without the LLM: when device.property op value " +
			"(optionally for for_s seconds, within after..before HH:MM) then notify, call a tool, or wake the LLM."
		params = obj(map[string]any{
			"description": tString, "device": tString, "property": tString,
			"op": enumOf(gate.Ops...), "value": map[string]any{}, "for_s": tNumber,
			"after": tString, "before": tString, "action": enumOf(gate.Actions...),
			"text": tString, "tool": tString, "args": map[string]any{"type": "object"},
			"cooldown_s": tNumber, "once": map[string]any{"type": "boolean"},
		}, "device", "property", "op", "action")
	case "delete_rule":
		desc = "Delete a local rule by id."
		params = obj(map[string]any{"rule_id": tString}, "rule_id")
	case "update_profile":
		desc = "Add, update or delete a fact about the user (persistent preferences)."
		params = obj(map[string]any{"op": enumOf("ADD", "UPDATE", "DELETE"), "id": tString, "text": tString}, "op")
	case "recent_events":
		desc = "Recent sensor events from the local log, newest last."
		params = obj(map[string]any{"device": tString, "property": tString, "k": map[string]any{"type": "integer"}})
	case "agent_status":
		desc = "Agent status: LLM budget usage, queue, memory."
		params = obj(map[string]any{})
	}
	return llm.Tool{Type: "function", Function: llm.FunctionDef{Name: name, Description: desc, Parameters: params}}
}

// rebuildTools собирает каталог для LLM: инструменты MCP + встроенные, по имени.
func (a *Agent) rebuildTools() {
	var tools []llm.Tool
	for _, t := range a.host.Tools() {
		tools = append(tools, llm.Tool{Type: "function", Function: llm.FunctionDef{Name: t.Name,
			Description: t.Description, Parameters: t.Schema}})
	}
	for name := range a.local {
		tools = append(tools, localToolDef(name))
	}
	sort.Slice(tools, func(i, j int) bool { return tools[i].Function.Name < tools[j].Function.Name })
	a.mu.Lock()
	a.toolDefs = tools
	a.mu.Unlock()
}

func str(args map[string]any, k string) string {
	s, _ := args[k].(string)
	return strings.TrimSpace(s)
}

func number(args map[string]any, k string) float64 {
	switch x := args[k].(type) {
	case float64:
		return x
	case string:
		var f float64
		fmt.Sscan(x, &f)
		return f
	}
	return 0
}

func ok(fields map[string]any) toolOutcome {
	fields["ok"] = true
	return toolOutcome{text: canon.String(fields)}
}

func fail(err error) toolOutcome {
	return toolOutcome{text: canon.String(map[string]any{"ok": false, "error": err.Error()}), err: err}
}

// localTool исполняет встроенный инструмент.
func (a *Agent) localTool(_ context.Context, ts *turnState, name string, args map[string]any) toolOutcome {
	if args == nil {
		args = map[string]any{}
	}
	switch name {
	case "notify_user":
		text := str(args, "text")
		if text == "" {
			return fail(errors.New("text обязателен"))
		}
		f := trace.F{"text": text, "source": "llm_tool", "wake_id": ts.w.id, "input_ids": ts.w.inputIDs}
		sid := str(args, "situation_id")
		if sid == "" {
			sid = ts.w.situationID()
		}
		if sid != "" {
			f["situation_id"] = sid
		}
		a.tr.Emit(trace.Notify, f)
		a.m.inc("notify.llm_tool")
		ts.notified = true
		return ok(map[string]any{})

	case "create_rule":
		r := gate.Rule{Description: str(args, "description"), Device: str(args, "device"),
			Property: str(args, "property"), Op: str(args, "op"), Value: args["value"], ForS: number(args, "for_s"),
			After: str(args, "after"), Before: str(args, "before"), Action: str(args, "action"),
			Text: str(args, "text"), Tool: str(args, "tool"), CooldownS: number(args, "cooldown_s"), Source: "llm"}
		if m, isMap := args["args"].(map[string]any); isMap {
			r.Args = m
		}
		if b, isBool := args["once"].(bool); isBool {
			r.Once = b
		}
		if r.Action == "tool" {
			if _, exists := a.host.Lookup(r.Tool); !exists {
				return fail(fmt.Errorf("инструмента %q нет в каталоге", r.Tool))
			}
		}
		added, err := a.gate.AddRule(r)
		if err != nil {
			return fail(err)
		}
		if err := a.store.SaveRule(added.ID, added, "llm", a.now()); err != nil {
			a.traceError("rule_save", err)
		}
		a.m.inc("rules.created")
		return ok(map[string]any{"rule_id": added.ID})

	case "delete_rule":
		id := str(args, "rule_id")
		if !a.gate.RemoveRule(id) {
			return fail(fmt.Errorf("правила %q нет", id))
		}
		if err := a.store.DeleteRule(id); err != nil {
			a.traceError("rule_delete", err)
		}
		a.m.inc("rules.deleted")
		return ok(map[string]any{"rule_id": id})

	case "update_profile":
		op := strings.ToUpper(str(args, "op"))
		id, text := str(args, "id"), str(args, "text")
		var err error
		switch op {
		case "ADD":
			var fact userctx.Fact
			fact, err = a.store.AddFact(id, text, a.now())
			id = fact.ID
		case "UPDATE":
			err = a.store.UpdateFact(id, text, a.now())
		case "DELETE":
			err = a.store.DeleteFact(id)
		default:
			err = fmt.Errorf("op %q: ADD | UPDATE | DELETE", op)
		}
		if err != nil {
			return fail(err)
		}
		a.m.inc("profile." + strings.ToLower(op))
		a.scheduleConsolidation()
		return ok(map[string]any{"id": id, "op": op})

	case "recent_events":
		k := int(number(args, "k"))
		if k <= 0 || k > 100 {
			k = a.cfg.Policy.RecentEventsK
		}
		evs := a.hw.Recent(k, str(args, "device"), str(args, "property"))
		out := make([]any, 0, len(evs))
		for _, e := range evs {
			item := map[string]any{"time": hhmmss(a.clockAt(e.T)), "t": math.Round(e.T*10) / 10,
				"device": e.Device, "property": e.Prop, "value": e.Value}
			if e.Old != nil {
				item["old"] = e.Old
			}
			out = append(out, item)
		}
		return toolOutcome{text: canon.String(map[string]any{"events": out})}

	case "agent_status":
		var ms runtime.MemStats
		runtime.ReadMemStats(&ms)
		return toolOutcome{text: canon.String(map[string]any{
			"budget": a.sched.Usage(), "queued": a.sched.Queued(), "rules": len(a.gate.Rules()),
			"heap_mb": math.Round(float64(ms.HeapAlloc)/(1<<20)*10) / 10,
		})}
	}
	return fail(fmt.Errorf("инструмента %q нет", name))
}

// scheduleConsolidation обновляет сегмент профиля в промпте после паузы
// profile_consolidate_s (профиль стоит в стабильной части и не должен меняться
// на каждом ходе); 0 — сразу.
func (a *Agent) scheduleConsolidation() {
	d := config.Seconds(a.cfg.Policy.ProfileConsolidateS)
	if d <= 0 {
		a.consolidateProfile()
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.profileTm != nil {
		a.profileTm.Stop()
	}
	a.profileTm = a.clk.AfterFunc(d, a.consolidateProfile)
}

func (a *Agent) consolidateProfile() {
	text := a.store.ProfileText()
	a.mu.Lock()
	a.profileText = text
	a.mu.Unlock()
	a.m.inc("profile.consolidations")
}

// decodeRule восстанавливает правило из JSON хранилища.
func decodeRule(spec json.RawMessage) (gate.Rule, error) {
	var r gate.Rule
	if err := json.Unmarshal(spec, &r); err != nil {
		return r, err
	}
	return r, nil
}

// ------------------------------------------------------------ исходы гейта

// Suppress — гейт не будит LLM по входу (реализация gate.Sink).
func (a *Agent) Suppress(ev gate.Event, reason string) {
	f := trace.F{"reason": reason, "input_id": ev.InputID}
	if ev.Device != "" {
		f["key"] = ev.Key()
	}
	a.tr.Emit(trace.Suppress, f)
	a.m.inc("suppress." + reason)
}

// Derived — производный вход гейта (конец эпизода).
func (a *Agent) Derived(ev gate.Event) {
	// В трассе это вход вида sensor с полем derived; в счётчиках — отдельный вид
	// "derived", чтобы ожидание MinInputs{"sensor": N} считало только события датчиков.
	a.tr.Emit(trace.Input, trace.F{"kind": "sensor", "id": ev.InputID, "device": ev.Device, "property": ev.Prop,
		"value": ev.Value, "derived": ev.Derived, "derived_from": ev.DerivedFrom, "extra": ev.Extra})
	a.countInput("derived")
}

// Wake — гейт решил разбудить LLM.
func (a *Agent) Wake(w gate.Wake) {
	if a.isClosed() {
		for _, e := range w.Events {
			a.Suppress(e, "shutdown")
		}
		return
	}
	a.submit(&wake{id: a.newID("w"), reason: "gate", prio: sched.ParsePriority(w.Priority),
		inputIDs: w.InputIDs(), events: w.Events, submitWall: time.Now(), submitT: a.now()})
}

// RuleFired — сработало локальное правило: действие исполняется без LLM
// (notify, tool) или будит LLM (wake).
func (a *Agent) RuleFired(r gate.Rule, ev gate.Event) {
	a.tr.Emit(trace.RuleFired, trace.F{"rule_id": r.ID, "input_id": ev.InputID, "action": r.Action,
		"source": r.Source})
	a.m.inc("rules.fired")
	if r.Once {
		if err := a.store.DeleteRule(r.ID); err != nil {
			a.traceError("rule_delete", err)
		}
	}
	inputs := []string{}
	if ev.InputID != "" {
		inputs = append(inputs, ev.InputID)
	}
	switch r.Action {
	case "notify":
		a.tr.Emit(trace.Notify, trace.F{"text": r.Text, "source": "rule", "rule_id": r.ID, "input_ids": inputs})
		a.m.inc("notify.rule")
	case "tool":
		a.tracker.Begin()
		go func() {
			defer a.tracker.End()
			a.ruleTool(r)
		}()
	case "wake":
		if a.isClosed() {
			return
		}
		prio := r.Priority
		if prio == "" {
			prio = gate.PrioritySafety
		}
		rr := r
		a.submit(&wake{id: a.newID("w"), reason: "rule", prio: sched.ParsePriority(prio), inputIDs: inputs,
			events: []gate.Event{ev}, rule: &rr, submitWall: time.Now(), submitT: clock.Seconds(a.clk)})
	}
}

// ruleTool вызывает инструмент MCP по правилу (без LLM).
func (a *Agent) ruleTool(r gate.Rule) {
	callID := a.newID("c")
	server := "unknown"
	if t, exists := a.host.Lookup(r.Tool); exists {
		server = t.Server
	}
	args := map[string]any{}
	for k, v := range r.Args {
		args[k] = v
	}
	a.tr.Emit(trace.ToolCall, trace.F{"call_id": callID, "server": server, "name": r.Tool, "args": args,
		"by": "rule", "rule_id": r.ID})
	a.m.inc("tools.calls_rule")
	start := time.Now()
	a.markSelfEcho(r.Tool, args)
	res, err := a.host.Call(a.ctx, r.Tool, args)
	out := toolOutcome{text: res.Text, denied: res.Denied, reason: res.Reason, isError: res.IsError, err: err}
	a.emitToolResult(callID, out, float64(time.Since(start).Microseconds())/1000)
}
