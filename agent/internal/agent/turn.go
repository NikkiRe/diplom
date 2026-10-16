package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/local/edgectx/internal/canon"
	"github.com/local/edgectx/internal/gate"
	"github.com/local/edgectx/internal/hwctx"
	"github.com/local/edgectx/internal/llm"
	"github.com/local/edgectx/internal/prompt"
	"github.com/local/edgectx/internal/sched"
	"github.com/local/edgectx/internal/trace"
	"github.com/local/edgectx/internal/userctx"
)

// wake — решение разбудить LLM, ставшее заданием планировщика (один ход).
type wake struct {
	id       string
	reason   string // user | gate | heartbeat | rule | deferred
	prio     sched.Priority
	inputIDs []string // входы без исхода: у deferred — только у ещё не допущенных частей
	events   []gate.Event
	rule     *gate.Rule
	userText string
	done     chan Reply // только для user

	parts    []*wake // deferred: слитые отложенные пробуждения (по времени)
	admitted bool    // запись wake уже была: входы получили исход
	replied  bool    // пользователю уже ответили «отложено» (под Agent.mu; ответ модели — в трассе)

	submitWall time.Time
	submitT    float64
}

// flat — сам wake или его части, если в него уже слиты отложенные.
func (w *wake) flat() []*wake {
	if w.reason == reasonDeferred {
		return w.parts
	}
	return []*wake{w}
}

// users — пробуждения по репликам пользователя, которым нужен ответ.
func (w *wake) users() []*wake {
	var out []*wake
	for _, p := range w.flat() {
		if p.reason == "user" {
			out = append(out, p)
		}
	}
	return out
}

func wakeIDs(ws []*wake) []string {
	ids := make([]string, len(ws))
	for i, w := range ws {
		ids[i] = w.id
	}
	return ids
}

// firstInput — первый вход пробуждения ("" у правила, сработавшего по уже
// известному состоянию при создании).
func (w *wake) firstInput() string {
	if len(w.inputIDs) == 0 {
		return ""
	}
	return w.inputIDs[0]
}

func (w *wake) situationID() string {
	for _, e := range w.events {
		if e.Kind == "situation" {
			return e.InputID
		}
	}
	return ""
}

// submit ставит пробуждение в планировщик.
func (a *Agent) submit(w *wake) {
	a.sched.Submit(&sched.Job{ID: w.id, Priority: w.prio,
		Run:    func(ctx context.Context) { a.runTurn(ctx, w) },
		Reject: func(reason string) { a.rejectWake(w, reason) }})
}

func (a *Agent) wakeFields(w *wake) trace.F {
	f := trace.F{"wake_id": w.id, "reason": w.reason, "priority": w.prio.String(), "input_ids": w.inputIDs,
		"queued_ms": float64(time.Since(w.submitWall).Microseconds()) / 1000}
	if w.rule != nil {
		f["rule_id"] = w.rule.ID
	}
	if len(w.parts) > 0 {
		f["coalesced"] = wakeIDs(w.parts)
	}
	return f
}

// rejectWake — пробуждение отклонено (бюджет, очередь, остановка, переполнение
// отложенных): входы без исхода подавляются, пользователь получает отказ, а
// тот, чей ход уже был допущен или кому ответили «отложено», — ещё и
// user_reply с ошибкой (у каждой реплики ровно один user_reply).
func (a *Agent) rejectWake(w *wake, reason string) {
	a.heartbeatDequeued(w)
	if !w.admitted {
		f := a.wakeFields(w)
		f["suppressed"] = true
		f["suppress_reason"] = reason
		a.tr.Emit(trace.Wake, f)
		a.m.inc("wakes_suppressed." + reason)
		for _, id := range w.inputIDs {
			a.tr.Emit(trace.Suppress, trace.F{"reason": reason, "input_id": id, "wake_id": w.id})
			a.m.inc("suppress." + reason)
		}
	}
	var lost []string
	for _, p := range w.flat() {
		if p.admitted && p.reason != "user" {
			lost = append(lost, p.id)
		}
	}
	if len(lost) > 0 {
		// Ход уже был допущен и сорвался из-за сбоя LLM: его входы учтены, но повтора не будет.
		a.traceError("deferred_lost", fmt.Errorf("отложенные пробуждения %s отброшены (%s)", strings.Join(lost, ", "), reason))
	}
	for _, p := range w.users() {
		if !p.admitted && !p.replied {
			p.done <- Reply{InputID: p.firstInput(), WakeID: w.id, Error: reason}
			continue
		}
		text := fmt.Sprintf(deferredLostText, reason)
		lat := a.emitUserReply(p, w.id, text, false, 0, 0)
		if !p.replied {
			p.done <- Reply{InputID: p.firstInput(), WakeID: w.id, Text: text, LatencyMs: lat, Error: reason}
		}
	}
}

// turnState — накопленное за ход.
type turnState struct {
	w         *wake
	msgs      []llm.Message // сообщения хода (первое — реплика/задача)
	llmCalls  int
	toolCalls int
	final     string
	err       error
	notified  bool
	exhausted bool // ход упёрся в max_steps с невыполненным намерением модели
}

// runTurn исполняет один ход: промпт → LLM → инструменты (до max_steps). Пока
// модель недоступна, ход не начинается, а откладывается.
func (a *Agent) runTurn(ctx context.Context, w *wake) {
	a.heartbeatDequeued(w)
	if a.deferWake(w, 0) {
		return
	}
	f := a.wakeFields(w)
	f["suppressed"] = false
	a.tr.Emit(trace.Wake, f)
	w.admitted = true
	for _, p := range w.parts {
		p.admitted = true
	}
	a.m.inc("wakes." + w.reason)
	if a.cfg.Policy.Wake == "hybrid" && w.reason != "heartbeat" {
		a.armHeartbeat() // heartbeat — только после heartbeat_min тишины LLM
	}

	ts := &turnState{w: w, msgs: []llm.Message{{Role: "user", Content: a.turnText(w)}}}
	now := a.clk.Now()
	sys := a.systemText(a.now())
	state, delta := a.stateText(w, now)
	a.mu.Lock()
	profile := a.profileText
	tools := a.toolDefs
	a.mu.Unlock()
	summary, history := a.store.View(userctx.Policy{Kind: a.cfg.Policy.History,
		WindowTurns: a.cfg.Policy.WindowTurns, MaskKeepTurns: a.cfg.Policy.MaskKeepTurns})

	for step := 0; step < a.cfg.Policy.MaxSteps; step++ {
		built := prompt.Build(prompt.Input{System: sys, Tools: tools, Profile: profile, Summary: summary,
			History: history, State: state, Turn: ts.msgs})
		resp, err := a.callLLM(ctx, w, step, "turn", built, tools)
		ts.llmCalls++
		if err != nil {
			ts.err = err
			break
		}
		if step == 0 {
			a.hw.Commit(delta) // дельта L1 «показана» только после ответа модели
		}
		ts.msgs = append(ts.msgs, llm.Message{Role: "assistant", Content: resp.Content, ToolCalls: resp.ToolCalls})
		ts.final = resp.Content
		if len(resp.ToolCalls) == 0 {
			break
		}
		ts.exhausted = step == a.cfg.Policy.MaxSteps-1
		for _, tc := range resp.ToolCalls {
			out := a.execTool(ctx, ts, tc)
			ts.msgs = append(ts.msgs, llm.Message{Role: "tool", ToolCallID: tc.ID, Content: out})
			ts.toolCalls++
		}
	}
	if ts.exhausted {
		a.m.inc("turns_max_steps")
	}
	a.finishTurn(ctx, ts)
}

// callLLM отправляет запрос, пишет llm_request/llm_response, учитывает бюджет и
// связь с моделью.
func (a *Agent) callLLM(ctx context.Context, w *wake, step int, purpose string, built prompt.Built, tools []llm.Tool) (*llm.Response, error) {
	body, err := llm.Body(llm.Request{Model: a.cfg.LLM.Model, Messages: built.Messages, Tools: tools,
		MaxTokens: a.cfg.LLM.MaxTokens, Extra: a.cfg.LLM.Extra})
	if err != nil {
		return nil, fmt.Errorf("llm: тело запроса: %w", err)
	}
	reqID := a.newID("r")
	est := built.Tokens.Total()
	a.tr.Emit(trace.LLMRequest, trace.F{
		"req_id": reqID, "wake_id": w.id, "step": step, "purpose": purpose, "model": a.cfg.LLM.Model,
		"prompt_tokens_est": est, "segments": built.Chars.Map(), "segments_tokens_est": built.Tokens.Map(),
		"n_tools": len(tools), "n_messages": len(built.Messages), "bytes": len(body),
	})
	a.m.inc("llm.requests")
	a.m.inc("llm.requests_" + purpose)
	a.m.add("llm.prompt_tokens_est", est)
	a.m.add("llm.bytes_out", len(body))

	var headers map[string]string
	if a.cfg.LLM.TraceHeaders {
		headers = map[string]string{
			"X-Edgectx-Run-Id": a.tr.RunID(), "X-Edgectx-Req-Id": reqID, "X-Edgectx-Wake-Id": w.id,
			"X-Edgectx-Wake-Reason": w.reason, "X-Edgectx-Input-Ids": strings.Join(w.inputIDs, ","),
			"X-Edgectx-Purpose": purpose, "X-Edgectx-Step": fmt.Sprint(step),
			"X-Edgectx-Clock-T": canon.String(a.now()),
		}
	}
	start := time.Now()
	resp, err := a.llmc.Complete(ctx, body, headers)
	latency := float64(time.Since(start).Microseconds()) / 1000
	f := trace.F{"req_id": reqID, "latency_ms": latency}
	if err != nil {
		f["error"] = err.Error()
		f["finish_reason"] = "error"
		f["n_tool_calls"] = 0
		f["usage"] = map[string]any{"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
		e, typed := llm.AsError(err)
		if typed {
			f["error_kind"], f["attempts"] = e.Kind, e.Attempts
			if e.Waited > 0 {
				f["retry_wait_ms"] = float64(e.Waited.Microseconds()) / 1000
			}
			if e.MaybeBilled() {
				f["maybe_billed"] = true
			}
			a.m.inc("llm.errors_" + e.Kind)
		}
		a.tr.Emit(trace.LLMResponse, f)
		a.m.inc("llm.errors")
		// Неудачный запрос — обращение к лимиту вызовов; токены неизвестны (usage нет).
		a.sched.Charge(1, 0)
		if typed && e.Transient() {
			a.linkFailed(e, nil)
		}
		return nil, err
	}
	u := resp.Usage
	f["usage"] = map[string]any{"prompt_tokens": u.PromptTokens, "completion_tokens": u.CompletionTokens,
		"cached_tokens": u.CachedTokens, "reasoning_tokens": u.ReasoningTokens, "total_tokens": u.TotalTokens}
	if resp.UsageRaw != nil {
		f["usage_raw"] = resp.UsageRaw
	}
	f["has_usage"] = resp.HasUsage
	f["finish_reason"] = resp.FinishReason
	f["n_tool_calls"] = len(resp.ToolCalls)
	f["gen_id"] = resp.ID
	f["model"] = resp.Model
	f["service_tier"] = resp.ServiceTier
	f["provider"] = resp.Provider
	f["bytes"] = resp.BytesIn
	f["attempts"] = resp.Attempts
	if resp.Waited > 0 {
		f["retry_wait_ms"] = float64(resp.Waited.Microseconds()) / 1000
	}
	a.tr.Emit(trace.LLMResponse, f)
	a.linkOK()

	billed := u.PromptTokens
	if !resp.HasUsage {
		billed = est // провайдер не вернул usage — учитываем оценку
	}
	a.sched.Charge(1, billed)
	a.mu.Lock()
	a.lastPrompt = billed
	a.mu.Unlock()
	a.m.add("llm.prompt_tokens", u.PromptTokens)
	a.m.add("llm.completion_tokens", u.CompletionTokens)
	a.m.add("llm.cached_tokens", u.CachedTokens)
	a.m.add("llm.bytes_in", resp.BytesIn)
	return resp, nil
}

// finishTurn сохраняет ход в историю и выдаёт результат (ответ/уведомление).
// Ход, сорвавшийся на первом обращении из-за недоступности модели, ничего не
// успел сделать; если запрос заведомо не оплачен (не таймаут и не оборванный
// ответ), ход откладывается и повторится после восстановления связи.
func (a *Agent) finishTurn(ctx context.Context, ts *turnState) {
	w := ts.w
	if ts.err != nil && ts.llmCalls == 1 && llm.Retryable(ts.err) && a.deferWake(w, ts.llmCalls) {
		return
	}
	users := w.users()
	if len(users) > 0 || a.cfg.HistoryBackground() {
		msgs := append([]llm.Message{{Role: "user", Content: a.historyText(w)}}, ts.msgs[1:]...)
		if _, err := a.store.AppendTurn(userctx.Turn{InputID: w.firstInput(), Kind: w.reason, T: w.submitT, Messages: msgs}); err != nil {
			a.traceError("history", err)
		}
	}
	switch {
	case len(users) > 0:
		text := ts.final
		if ts.err != nil && text == "" {
			text = "Не удалось получить ответ модели: " + ts.err.Error()
		}
		if ts.exhausted && strings.TrimSpace(text) == "" {
			text = fmt.Sprintf("Не успел завершить: исчерпан лимит шагов (max_steps = %d).", a.cfg.Policy.MaxSteps)
		}
		for _, p := range users {
			lat := a.emitUserReply(p, w.id, text, ts.err == nil, ts.llmCalls, ts.toolCalls)
			if !p.replied {
				p.done <- Reply{InputID: p.firstInput(), WakeID: w.id, Text: text, LatencyMs: lat, LLMCalls: ts.llmCalls,
					ToolCalls: ts.toolCalls, OK: ts.err == nil, Error: errString(ts.err)}
			}
		}
	case ts.err == nil && !isNoop(ts.final, a.cfg.Policy.NoopReply):
		// Текст фонового хода — уведомление пользователю.
		f := trace.F{"text": ts.final, "source": "llm_reply", "wake_id": w.id, "input_ids": w.inputIDs}
		if sid := w.situationID(); sid != "" {
			f["situation_id"] = sid
		}
		a.tr.Emit(trace.Notify, f)
		a.m.inc("notify.llm_reply")
	default:
		if !ts.notified && ts.toolCalls == 0 {
			a.m.inc("wakes_empty") // «пустое» пробуждение: ни действия, ни уведомления
		}
	}
	if a.cfg.Policy.History == "summary_system" {
		a.maybeSummarize(ctx, w)
	}
}

// emitUserReply пишет user_reply на реплику p (ход wakeID) и возвращает
// задержку ответа, мс. Ответ после «отложено» помечается deferred.
func (a *Agent) emitUserReply(p *wake, wakeID, text string, ok bool, llmCalls, toolCalls int) float64 {
	lat := float64(time.Since(p.submitWall).Microseconds()) / 1000
	f := trace.F{"input_id": p.firstInput(), "wake_id": wakeID, "text": text, "latency_ms": lat,
		"latency_clock_s": a.now() - p.submitT, "llm_calls": llmCalls, "tool_calls": toolCalls, "ok": ok}
	if p.replied {
		f["deferred"] = true
	}
	a.tr.Emit(trace.UserReply, f)
	a.m.inc("user_replies")
	return lat
}

func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

// ----------------------------------------------------------- сегменты промпта

// systemText — префикс: системный промпт + инвентарь L0 (+ снимок L1).
func (a *Agent) systemText(t float64) string {
	sys := a.cfg.Prompt.System
	if sys == "" {
		sys = systemPrompt(a.cfg.Policy.NoopReply, a.local)
	}
	if inv := a.hw.InventoryText(); inv != "" {
		sys += "\n\nИнвентарь оборудования:\n" + inv
	}
	if a.hw.Mode() == hwctx.SnapshotHead {
		if snap := a.hw.SnapshotText(t); snap != "" {
			sys += "\n\n" + snap
		}
	}
	return sys
}

// stateText — хвост: время, дельта состояния (delta_tail) и разбудившее событие
// (у слитого хода — всё отложенное). Возвращает и показанную дельту: её
// запоминают только после ответа модели.
func (a *Agent) stateText(w *wake, now time.Time) (string, hwctx.Delta) {
	parts := []string{timeLine(now)}
	var delta hwctx.Delta
	if a.hw.Mode() == hwctx.DeltaTail {
		var d string
		if d, delta = a.hw.DeltaText(a.now()); d != "" {
			parts = append(parts, d)
		}
	}
	switch {
	case w.reason == reasonDeferred:
		parts = append(parts, a.deferredText(w))
	case len(w.events) > 0:
		lines := make([]string, 0, len(w.events))
		for _, e := range w.events {
			lines = append(lines, "- "+a.eventLine(e))
		}
		head := "События:"
		if w.reason == "rule" && w.rule != nil {
			head = fmt.Sprintf("Сработало правило %s (%s):", w.rule.ID, w.rule.Summary())
		}
		parts = append(parts, head+"\n"+strings.Join(lines, "\n"))
	}
	return strings.Join(parts, "\n"), delta
}

// turnText — реплика пользователя или задача фонового хода.
func (a *Agent) turnText(w *wake) string {
	noop := a.cfg.Policy.NoopReply
	switch w.reason {
	case "user":
		return w.userText
	case "heartbeat":
		if t := a.cfg.Policy.HeartbeatTask; t != "" {
			return t
		}
		return fmt.Sprintf(defaultHeartbeatTask, noop)
	case "rule":
		task := "Сработало правило, созданное ранее по просьбе пользователя."
		if w.rule != nil && w.rule.Text != "" {
			task += " Задача правила: " + w.rule.Text
		}
		return task + " Выполни нужное действие или уведомление; если ничего не нужно — ответь ровно " + noop + "."
	case reasonDeferred:
		return a.deferredTask(w)
	default:
		if t := a.cfg.Policy.EventTask; t != "" {
			return t
		}
		return fmt.Sprintf(defaultEventTask, noop)
	}
}

// historyText — что сохраняется в истории вместо текущей задачи хода.
func (a *Agent) historyText(w *wake) string {
	at := hhmm(a.clockAt(w.submitT))
	switch w.reason {
	case "user":
		return w.userText
	case "heartbeat":
		return "[плановая проверка " + at + "]"
	case reasonDeferred:
		return "[отложено без связи с моделью]\n" + strings.Join(a.deferredLines(w), "\n")
	default:
		lines := []string{}
		if w.rule != nil {
			lines = append(lines, "[правило "+w.rule.ID+"] "+w.rule.Summary())
		}
		for _, e := range w.events {
			lines = append(lines, "[событие] "+a.eventLine(e))
		}
		if len(lines) == 0 {
			return "[фоновый ход " + at + "]"
		}
		return strings.Join(lines, "\n")
	}
}

// ------------------------------------------------------------- инструменты

// execTool исполняет вызов инструмента и возвращает текст результата для LLM.
func (a *Agent) execTool(ctx context.Context, ts *turnState, tc llm.ToolCall) string {
	callID := a.newID("c")
	name := tc.Function.Name
	var args map[string]any
	argErr := error(nil)
	if s := strings.TrimSpace(tc.Function.Arguments); s != "" && s != "null" {
		dec := json.NewDecoder(strings.NewReader(s))
		dec.UseNumber()
		if err := dec.Decode(&args); err != nil {
			argErr = fmt.Errorf("аргументы не являются JSON-объектом: %v", err)
		}
	}
	args = numbersToFloat(args)
	server := "local"
	if !a.local[name] {
		if t, ok := a.host.Lookup(name); ok {
			server = t.Server
		} else {
			server = "unknown"
		}
	}
	a.tr.Emit(trace.ToolCall, trace.F{"call_id": callID, "llm_call_id": tc.ID, "wake_id": ts.w.id,
		"server": server, "name": name, "args": argsForTrace(args, tc.Function.Arguments), "by": "llm"})
	a.m.inc("tools.calls")

	start := time.Now()
	var res toolOutcome
	switch {
	case argErr != nil:
		res = toolOutcome{text: "Ошибка: " + argErr.Error(), err: argErr}
	case server == "local":
		res = a.localTool(ctx, ts, name, args)
	case server == "unknown":
		err := fmt.Errorf("инструмента %q нет", name)
		res = toolOutcome{text: "Ошибка: " + err.Error(), err: err}
	default:
		// Эхо отмечается до вызова: уведомление об изменении может прийти раньше ответа.
		a.markSelfEcho(name, args)
		r, err := a.host.Call(ctx, name, args)
		if err != nil {
			res = toolOutcome{text: "Ошибка: " + err.Error(), err: err}
		} else {
			res = toolOutcome{text: r.Text, denied: r.Denied, reason: r.Reason, isError: r.IsError}
		}
	}
	ms := float64(time.Since(start).Microseconds()) / 1000
	a.emitToolResult(callID, res, ms)
	return truncateRunes(res.text, a.cfg.Policy.MaxToolResultChars)
}

type toolOutcome struct {
	text    string
	err     error
	isError bool
	denied  bool
	reason  string
}

func (a *Agent) emitToolResult(callID string, r toolOutcome, ms float64) {
	ok := r.err == nil && !r.isError && !r.denied
	f := trace.F{"call_id": callID, "ok": ok, "denied": r.denied, "ms": ms, "chars": len([]rune(r.text))}
	if r.err != nil {
		f["error"] = r.err.Error()
	} else if r.isError {
		f["error"] = truncateRunes(r.text, 300)
	}
	if r.denied {
		f["reason"] = r.reason
		a.m.inc("tools.denied")
	}
	if !ok {
		a.m.inc("tools.failed")
	}
	a.tr.Emit(trace.ToolResult, f)
}

// argsForTrace — аргументы для трассы: разобранный объект или исходная строка.
func argsForTrace(args map[string]any, raw string) any {
	if args != nil {
		return args
	}
	if strings.TrimSpace(raw) == "" {
		return map[string]any{}
	}
	return raw
}

// numbersToFloat переводит json.Number в float64 (аргументы для MCP и правил).
func numbersToFloat(m map[string]any) map[string]any {
	if m == nil {
		return nil
	}
	var conv func(v any) any
	conv = func(v any) any {
		switch x := v.(type) {
		case json.Number:
			f, err := x.Float64()
			if err != nil {
				return x.String()
			}
			return f
		case map[string]any:
			for k, e := range x {
				x[k] = conv(e)
			}
			return x
		case []any:
			for i, e := range x {
				x[i] = conv(e)
			}
			return x
		}
		return v
	}
	for k, v := range m {
		m[k] = conv(v)
	}
	return m
}

// ---------------------------------------------------------------- сводка

// maybeSummarize сжимает старые ходы в сводку, когда оплаченные входные токены
// последнего запроса превысили порог (политика summary_system).
func (a *Agent) maybeSummarize(ctx context.Context, w *wake) {
	a.mu.Lock()
	last := a.lastPrompt
	a.mu.Unlock()
	if a.cfg.Policy.SummaryThresholdTokens <= 0 || last <= a.cfg.Policy.SummaryThresholdTokens {
		return
	}
	keep := a.cfg.Policy.WindowTurns
	if keep <= 0 {
		keep = 2
	}
	cands := a.store.ToSummarize(keep)
	if len(cands) == 0 {
		return
	}
	prev, _ := a.store.Summary()
	var b strings.Builder
	for _, t := range cands {
		for _, m := range t.Messages {
			switch {
			case m.Role == "user":
				fmt.Fprintf(&b, "Вход: %s\n", oneLine(m.Content))
			case m.Role == "assistant" && len(m.ToolCalls) > 0:
				for _, tc := range m.ToolCalls {
					fmt.Fprintf(&b, "Вызов: %s %s\n", tc.Function.Name, truncateRunes(tc.Function.Arguments, 200))
				}
			case m.Role == "assistant" && m.Content != "":
				fmt.Fprintf(&b, "Агент: %s\n", oneLine(m.Content))
			case m.Role == "tool":
				fmt.Fprintf(&b, "Результат: %s\n", truncateRunes(oneLine(m.Content), 200))
			}
		}
	}
	transcript := b.String()
	sys := "Сожми историю работы агента умного помещения в краткую сводку фактов: что просил пользователь, " +
		"какие действия выполнены, какие правила и договорённости приняты. Не более 10 строк, без вступлений."
	user := "Прежняя сводка:\n" + nonEmpty(prev, "(нет)") + "\n\nНовые ходы:\n" + transcript
	built := prompt.Build(prompt.Input{System: sys, Turn: []llm.Message{{Role: "user", Content: user}}})
	// Учёт сегментов: инструкция — префикс, расшифровка — история.
	built.Chars = prompt.Sizes{Prefix: built.Chars.Prefix, History: built.Chars.Turn}
	built.Tokens = prompt.Sizes{Prefix: built.Tokens.Prefix, History: built.Tokens.Turn}
	summary := ""
	if resp, err := a.callLLM(ctx, w, 0, "summary", built, nil); err == nil {
		summary = strings.TrimSpace(resp.Content)
	}
	if summary == "" || isNoop(summary, a.cfg.Policy.NoopReply) {
		// Модель не дала сводку (например оракул T0) — извлекающая сводка без LLM.
		summary = strings.TrimSpace(nonEmpty(prev, "") + "\n" + truncateRunes(transcript, 1500))
	}
	if err := a.store.SetSummary(summary, cands[len(cands)-1].Seq, a.now()); err != nil {
		a.traceError("summary", err)
	}
	a.m.inc("summaries")
}

func oneLine(s string) string { return strings.Join(strings.Fields(s), " ") }
