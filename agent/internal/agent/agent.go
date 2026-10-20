// Package agent — ядро edgectx: связывает каналы, часы, гейт, планировщик, два
// контекста, сборщик промпта, LLM-клиент, MCP host и трассу (спецификация §3).
//
// Цикл: входы (уведомления MCP resources/updated, реплики пользователя, события
// /v1/event, тики heartbeat) → гейт/heartbeat решает, будить ли LLM →
// планировщик (приоритеты, бюджет) → ход: промпт → LLM → инструменты (до
// max_steps) → ответ пользователю / уведомление → трасса.
package agent

import (
	"context"
	"errors"
	"fmt"
	"runtime"
	"runtime/debug"
	rtmetrics "runtime/metrics"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/local/edgectx/internal/activity"
	"github.com/local/edgectx/internal/backoff"
	"github.com/local/edgectx/internal/breaker"
	"github.com/local/edgectx/internal/clock"
	"github.com/local/edgectx/internal/config"
	"github.com/local/edgectx/internal/gate"
	"github.com/local/edgectx/internal/hwctx"
	"github.com/local/edgectx/internal/llm"
	"github.com/local/edgectx/internal/mcphost"
	"github.com/local/edgectx/internal/sched"
	"github.com/local/edgectx/internal/trace"
	"github.com/local/edgectx/internal/userctx"
)

// LLM — клиент модели (реализуется llm.Client; в тестах — подмена). Ошибки
// обращений к провайдеру — *llm.Error.
type LLM interface {
	// Complete отправляет тело запроса (llm.Body) с дополнительными заголовками.
	Complete(ctx context.Context, body []byte, headers map[string]string) (*llm.Response, error)
	// Probe — пробное обращение без токенов (проверка связи).
	Probe(ctx context.Context) (llm.ProbeResult, error)
}

// Deps — зависимости агента. Store и Trace закрывает владелец (cmd).
type Deps struct {
	Config  *config.Config
	Clock   clock.Clock
	Trace   *trace.Writer
	Store   *userctx.Store
	LLM     LLM
	Version string
}

// Agent — агент edgectx.
type Agent struct {
	cfg     *config.Config
	clk     clock.Clock
	tr      *trace.Writer
	store   *userctx.Store
	llmc    LLM
	host    *mcphost.Host
	hw      *hwctx.Context
	gate    *gate.Gate
	sched   *sched.Scheduler
	tracker *activity.Tracker
	version string
	local   map[string]bool // включённые встроенные инструменты

	ctx    context.Context
	cancel context.CancelFunc
	// probeCtx — контекст проб связи: у пробы нет побочных эффектов, и она
	// отменяется в начале остановки, не дожидаясь ходов.
	probeCtx   context.Context
	stopProbes context.CancelFunc

	idMu sync.Mutex
	ids  map[string]int

	mu          sync.Mutex
	toolDefs    []llm.Tool
	profileText string
	profileTm   clock.Timer
	hbTm        clock.Timer
	lastPrompt  int  // оплаченные входные токены последнего запроса (порог сводки)
	hbQueued    bool // тик heartbeat ждёт в очереди планировщика
	started     bool
	closed      bool
	startWall   time.Time

	link     *breaker.Breaker   // предохранитель внешней LLM (под mu)
	probeTm  clock.Timer        // проба связи с LLM (под mu)
	downT    float64            // когда модель стала недоступной (под mu)
	deferred []*wake            // отложенные до восстановления связи пробуждения, без слитых (под mu)
	mcpDownT map[string]float64 // упавший MCP-сервер -> с какого t (под mu)

	inMu   sync.Mutex
	inCond *sync.Cond
	inputs map[string]int

	selfMu    sync.Mutex
	selfUntil map[string]float64 // устройство -> до какого t изменения — эхо команды агента

	m *metrics
}

// ErrNotVirtual — продвижение часов возможно только для виртуальных часов.
var ErrNotVirtual = errors.New("clock is not virtual")

// ErrClosed — агент остановлен.
var ErrClosed = errors.New("agent is closed")

// New создаёт агента. MCP-серверы подключаются через Host() до вызова Start.
func New(d Deps) (*Agent, error) {
	if d.Config == nil || d.Clock == nil || d.Trace == nil || d.Store == nil || d.LLM == nil {
		return nil, errors.New("agent: incomplete deps")
	}
	cfg := d.Config
	seed := backoff.Seed(cfg.RunID)
	a := &Agent{cfg: cfg, clk: d.Clock, tr: d.Trace, store: d.Store, llmc: d.LLM, version: d.Version,
		tracker: &activity.Tracker{}, ids: map[string]int{}, inputs: map[string]int{}, m: newMetrics(),
		local: map[string]bool{}, selfUntil: map[string]float64{}, mcpDownT: map[string]float64{},
		link: breaker.New(cfg.LLM.Breaker.Failures, backoff.Policy{Base: config.Seconds(cfg.LLM.Breaker.ProbeS),
			Max: config.Seconds(cfg.LLM.Breaker.ProbeMaxS)}, seed)}
	a.inCond = sync.NewCond(&a.inMu)
	a.ctx, a.cancel = context.WithCancel(context.Background())
	a.probeCtx, a.stopProbes = context.WithCancel(a.ctx)
	for _, n := range cfg.Policy.LocalTools {
		if (n == "create_rule" || n == "delete_rule") && !cfg.RulesEnabled() {
			continue // без правил инструмент не нужен и не должен стоить токенов
		}
		a.local[n] = true
	}
	a.hw = hwctx.New(cfg.Policy.HWCtx, cfg.Policy.SnapshotPeriodS, eventLogCap)
	a.gate = gate.New(a.clk, gate.Options{
		Config: cfg.Policy.Gate, Wakes: cfg.Policy.Wake != "heartbeat", Rules: cfg.RulesEnabled(),
		NewInputID: a.newID, State: a.hw,
	}, a)
	a.sched = sched.New(sched.Options{MaxInFlight: cfg.Sched.MaxInFlight, QueueMax: cfg.Sched.QueueMax,
		Budget: cfg.Budget, Clock: a.clk, Tracker: a.tracker})
	reserved := make([]string, 0, len(a.local))
	for n := range a.local {
		reserved = append(reserved, n)
	}
	sort.Strings(reserved)
	a.host = mcphost.New(mcphost.Options{Tracker: a.tracker, OnUpdate: a.onUpdate, OnInventory: a.onInventory,
		OnDown: a.onServerDown, OnUp: a.onServerUp, OnError: a.traceError, Reserved: reserved, Dir: cfg.BaseDir,
		Version: d.Version, Seed: seed})
	return a, nil
}

// eventLogCap — ёмкость журнала событий L2 (recent_events): ≈5000 × 0,1–0,2 КБ.
const eventLogCap = 5000

// Host — MCP host (для подключения серверов до Start).
func (a *Agent) Host() *mcphost.Host { return a.host }

// Clock — часы агента.
func (a *Agent) Clock() clock.Clock { return a.clk }

// newID выдаёт идентификатор вида <prefix>-<n>.
func (a *Agent) newID(prefix string) string {
	a.idMu.Lock()
	defer a.idMu.Unlock()
	a.ids[prefix]++
	return fmt.Sprintf("%s-%d", prefix, a.ids[prefix])
}

func (a *Agent) now() float64 { return clock.Seconds(a.clk) }

func (a *Agent) clockAt(sec float64) time.Time { return clock.At(a.clk, sec) }

func (a *Agent) traceError(where string, err error) {
	if err == nil {
		return
	}
	a.m.inc("errors." + where)
	a.tr.Emit(trace.Error, trace.F{"where": where, "msg": err.Error()})
}

// Start синхронизирует оборудование (инвентарь, подписки, начальное состояние),
// загружает правила и пишет run_start. Только после него правила взводятся (и
// могут сразу сработать по известному состоянию), а MCP-серверы начинают
// доставлять уведомления: run_start — всегда первая запись трассы. Затем
// запускаются планировщик и heartbeat.
func (a *Agent) Start(ctx context.Context) error {
	a.mu.Lock()
	if a.started {
		a.mu.Unlock()
		return errors.New("agent: already started")
	}
	a.started, a.startWall = true, time.Now()
	a.mu.Unlock()

	var errs []error
	if err := a.syncHardware(ctx); err != nil {
		errs = append(errs, err)
	}
	if err := a.store.SeedFacts(a.cfg.UserCtx.Profile, a.now()); err != nil {
		errs = append(errs, fmt.Errorf("profile seed: %w", err))
	}
	a.mu.Lock()
	a.profileText = a.store.ProfileText()
	a.mu.Unlock()
	a.rebuildTools()
	rules, err := a.loadRules()
	if err != nil {
		errs = append(errs, err)
	}
	a.emitRunStart(len(rules))
	for _, r := range rules {
		if _, err := a.gate.AddRule(r); err != nil {
			errs = append(errs, fmt.Errorf("rule %s: %w", r.ID, err))
		}
	}
	for _, e := range errs {
		a.traceError("start", e)
	}
	a.host.Start()
	a.sched.Start(a.ctx)
	if a.cfg.Policy.Wake != "gate" {
		a.armHeartbeat()
	}
	return nil
}

func (a *Agent) emitRunStart(nRules int) {
	p := a.cfg.Policy
	var servers []map[string]any
	for _, s := range a.host.Servers() {
		servers = append(servers, map[string]any{"name": s.Name, "transport": s.Transport, "protocol": s.Protocol,
			"tools": s.Tools, "subscribed": s.Subscribed, "server_name": s.ServerName})
	}
	a.mu.Lock()
	nTools := len(a.toolDefs)
	a.mu.Unlock()
	// Параметры запроса, от которых зависят цена и кэш: неизменны весь прогон.
	extra := a.cfg.LLM.Extra
	llmParams := map[string]any{"reasoning_effort": config.ReasoningEffort(extra), "verbosity": extra["verbosity"],
		"service_tier": extra["service_tier"]}
	a.tr.Emit(trace.RunStart, trace.F{
		"config_hash": a.cfg.Hash(),
		"policy": map[string]any{
			"wake": p.Wake, "heartbeat_min": p.HeartbeatMin, "history": p.History, "window_turns": p.WindowTurns,
			"mask_keep_turns": p.MaskKeepTurns, "history_batch_turns": p.HistoryBatchTurns,
			"history_noop": p.HistoryNoop, "hwctx": p.HWCtx, "catalog": p.Catalog, "max_steps": p.MaxSteps,
			"snapshot_period_s": p.SnapshotPeriodS, "summary_threshold_tokens": p.SummaryThresholdTokens,
			"rules_enabled": a.cfg.RulesEnabled(), "local_tools": sortedKeys(a.local),
			"gate": map[string]any{"debounce_s": p.Gate.DebounceS, "cooldown_s": p.Gate.CooldownS,
				"coalesce_s": p.Gate.CoalesceS, "coalesce_max": p.Gate.CoalesceMax, "default": p.Gate.Default,
				"signals": len(p.Gate.Signals)},
		},
		"clock":     a.cfg.Clock.Mode,
		"start_t":   a.now(),
		"model":     a.cfg.LLM.Model,
		"base_url":  a.cfg.LLM.BaseURL,
		"llm":       llmParams,
		"budget":    a.cfg.Budget,
		"sched":     a.cfg.Sched,
		"servers":   servers,
		"n_tools":   nTools,
		"n_devices": len(a.hw.Inventory().Devices),
		"n_rules":   nRules,
		"version":   a.version,
		"protocol":  mcphost.ProtocolVersion,
		"breaker":   a.cfg.LLM.Breaker,
		"runtime":   goRuntime(),
	})
}

// goRuntime — фактические параметры рантайма Go: предел памяти (GOMEMLIMIT
// задаётся снаружи; math.MaxInt64 — предела нет), GOGC (−1 — сборка выключена)
// и число потоков исполнения (с учётом квоты CPU cgroup).
func goRuntime() map[string]any {
	gogc := []rtmetrics.Sample{{Name: "/gc/gogc:percent"}}
	rtmetrics.Read(gogc)
	return map[string]any{"gomemlimit": debug.SetMemoryLimit(-1), "gogc": int64(gogc[0].Value.Uint64()),
		"gomaxprocs": runtime.GOMAXPROCS(0), "go": runtime.Version()}
}

// syncHardware читает инвентарь всех серверов, подписывается на их ресурсы
// состояния и читает начальное состояние (без порождения событий).
func (a *Agent) syncHardware(ctx context.Context) error {
	if len(a.host.Servers()) == 0 {
		return nil
	}
	var errs []string
	if err := a.reloadInventory(ctx); err != nil {
		errs = append(errs, err.Error())
	}
	for _, s := range a.host.Servers() {
		states, err := a.syncServer(ctx, s)
		if err != nil {
			errs = append(errs, err.Error())
		}
		for dev, props := range states {
			a.hw.Seed(a.now(), dev, props)
		}
	}
	if len(errs) > 0 {
		return errors.New(strings.Join(errs, "; "))
	}
	return nil
}

// syncServer подписывается на ресурсы состояния сервера (шаблоны раскрываются
// по его собственным устройствам) и читает их. Возвращает прочитанное
// состояние по устройствам.
func (a *Agent) syncServer(ctx context.Context, s mcphost.ServerInfo) (map[string]map[string]any, error) {
	var ids []string
	for _, d := range a.hw.Inventory().Devices {
		if d.Server == s.Name {
			ids = append(ids, d.ID)
		}
	}
	var errs []string
	uris := a.host.StateURIs(s.Name, ids)
	done, err := a.host.Subscribe(ctx, s.Name, uris)
	if err != nil {
		errs = append(errs, err.Error())
	}
	read := done
	if !s.CanSubscribe {
		read = uris
	}
	states := map[string]map[string]any{}
	for _, u := range read {
		text, err := a.host.ReadResource(ctx, s.Name, u)
		if err != nil {
			errs = append(errs, err.Error())
			continue
		}
		if dev, props, ok := hwctx.ParseState(u, text); ok {
			if states[dev] == nil {
				states[dev] = map[string]any{}
			}
			for p, v := range props {
				states[dev][p] = v
			}
		}
	}
	if len(errs) > 0 {
		return states, errors.New(strings.Join(errs, "; "))
	}
	return states, nil
}

// reloadInventory перечитывает инвентарь всех серверов в L0.
func (a *Agent) reloadInventory(ctx context.Context) error {
	raw, err := a.host.Inventory(ctx)
	var parts []hwctx.Inventory
	values := map[string]map[string]any{}
	for _, srv := range sortedKeys(raw) {
		inv, vals := hwctx.ParseInventory(srv, raw[srv])
		parts = append(parts, inv)
		for dev, props := range vals {
			values[dev] = props
		}
	}
	// Точность свойств должна быть известна до округления начальных значений,
	// в том числе у серверов без отдельного ресурса состояния.
	a.hw.SetInventory(hwctx.Merge(parts...))
	knownState := a.hw.State()
	for _, dev := range sortedKeys(values) {
		if _, known := knownState[dev]; !known {
			a.hw.Seed(a.now(), dev, values[dev])
		}
	}
	return err
}

// onServerDown — MCP-сервер упал: показания его устройств больше не свежие.
func (a *Agent) onServerDown(server string, err error) {
	a.mu.Lock()
	a.mcpDownT[server] = a.now()
	a.mu.Unlock()
	a.hw.MarkStale(server)
	a.m.inc("mcp.down")
	a.tr.Emit(trace.MCPDown, trace.F{"server": server, "error": err.Error()})
}

// onServerUp — упавший MCP-сервер снова подключён: перечитываются инвентарь и
// каталог, восстанавливается подписка, а состояние читается заново — то, что
// изменилось без связи, проходит через гейт как обычные изменения. Устройство
// снова считается свежим, только когда его состояние прочитано; если прочитано
// не всё, host повторит вызов с паузой (mcp_up пишется один раз).
func (a *Agent) onServerUp(ctx context.Context, server string, attempts int) error {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	a.mu.Lock()
	downT, first := a.mcpDownT[server]
	delete(a.mcpDownT, server)
	a.mu.Unlock()
	if first {
		if err := a.reloadInventory(ctx); err != nil {
			a.traceError("inventory", err)
		}
		a.rebuildTools()
		a.m.inc("mcp.up")
		a.tr.Emit(trace.MCPUp, trace.F{"server": server, "attempts": attempts, "down_s": a.now() - downT})
	}
	var states map[string]map[string]any
	var err error
	for _, s := range a.host.Servers() {
		if s.Name == server {
			states, err = a.syncServer(ctx, s)
		}
	}
	for _, dev := range sortedKeys(states) {
		a.ingest(dev, states[dev], "")
	}
	return err
}

func (a *Agent) onInventory(server string) {
	ctx, cancel := context.WithTimeout(a.ctx, 30*time.Second)
	defer cancel()
	if err := a.reloadInventory(ctx); err != nil {
		a.traceError("inventory", err)
	}
	a.rebuildTools()
}

// loadRules читает и проверяет правила из файла (rules_path) и сохранённые
// LLM; взводит их Start после записи run_start.
func (a *Agent) loadRules() ([]gate.Rule, error) {
	if !a.cfg.RulesEnabled() {
		return nil, nil
	}
	var errs []string
	var rules []gate.Rule
	if p := a.cfg.Policy.Gate.RulesPath; p != "" {
		fromFile, err := gate.LoadRules(a.cfg.Path(p))
		if err != nil {
			errs = append(errs, err.Error())
		}
		rules = append(rules, fromFile...)
	}
	recs, err := a.store.Rules()
	if err != nil {
		errs = append(errs, err.Error())
	}
	for _, rec := range recs {
		r, err := decodeRule(rec.Spec)
		if err == nil {
			err = r.Validate()
		}
		if err != nil {
			errs = append(errs, fmt.Sprintf("rule %s: %v", rec.ID, err))
			continue
		}
		rules = append(rules, r)
	}
	if len(errs) > 0 {
		return rules, errors.New(strings.Join(errs, "; "))
	}
	return rules, nil
}

// ------------------------------------------------------------------- входы

// countInput учитывает вход (для метрик и ожидания в Advance).
func (a *Agent) countInput(kind string) {
	a.inMu.Lock()
	a.inputs[kind]++
	a.inCond.Broadcast()
	a.inMu.Unlock()
	a.m.inc("inputs." + kind)
}

func (a *Agent) emitInput(kind, id string, extra trace.F) {
	f := trace.F{"kind": kind, "id": id}
	for k, v := range extra {
		f[k] = v
	}
	a.tr.Emit(trace.Input, f)
	a.countInput(kind)
}

// Reply — ответ на реплику пользователя.
type Reply struct {
	InputID   string  `json:"input_id"`
	WakeID    string  `json:"wake_id"`
	Text      string  `json:"text"`
	LatencyMs float64 `json:"latency_ms"`
	LLMCalls  int     `json:"llm_calls"`
	ToolCalls int     `json:"tool_calls"`
	OK        bool    `json:"ok"`
	Error     string  `json:"error,omitempty"`
	// Deferred — модель недоступна: вопрос отложен, ответ будет после
	// восстановления связи (в трассе — user_reply с deferred: true).
	Deferred bool `json:"deferred,omitempty"`
}

// User обрабатывает реплику пользователя и ждёт ответа.
func (a *Agent) User(ctx context.Context, id, text string) (Reply, error) {
	if a.isClosed() {
		return Reply{}, ErrClosed
	}
	if id == "" {
		id = a.newID("user")
	}
	a.emitInput("user", id, trace.F{"text": text})
	w := &wake{id: a.newID("w"), reason: "user", prio: sched.User, inputIDs: []string{id},
		userText: text, done: make(chan Reply, 1), submitWall: time.Now(), submitT: a.now()}
	a.submit(w)
	select {
	case r := <-w.done:
		return r, nil
	case <-ctx.Done():
		return Reply{InputID: id, WakeID: w.id}, ctx.Err()
	}
}

// EventInput — событие от реплеера (POST /v1/event).
type EventInput struct {
	ID       string `json:"id"`
	Kind     string `json:"kind"` // sensor (по умолчанию) | situation
	Device   string `json:"device"`
	Property string `json:"property"`
	Value    any    `json:"value"`
	// Name, Text — для situation.
	Name string `json:"name"`
	Text string `json:"text"`
}

// Event принимает событие датчика или ситуацию. Возвращает идентификаторы
// порождённых входов; гейт обрабатывает событие до возврата.
func (a *Agent) Event(ev EventInput) ([]string, error) {
	if a.isClosed() {
		return nil, ErrClosed
	}
	switch ev.Kind {
	case "", "sensor":
		if ev.Device == "" || ev.Property == "" {
			return nil, errors.New("device и property обязательны")
		}
		return a.ingest(ev.Device, map[string]any{ev.Property: ev.Value}, ev.ID), nil
	case "situation":
		id := ev.ID
		if id == "" {
			id = a.newID("sit")
		}
		a.emitInput("situation", id, trace.F{"name": ev.Name, "text": ev.Text})
		a.gate.OnEvent(gate.Event{InputID: id, T: a.now(), Kind: "situation", Name: ev.Name, Text: ev.Text})
		return []string{id}, nil
	}
	return nil, fmt.Errorf("неизвестный kind %q", ev.Kind)
}

// ingest применяет новое состояние устройства: каждое изменение свойства —
// отдельный вход sensor, журнал L2 и событие гейта. idHint — идентификатор
// входа от реплеера (для единственного свойства).
func (a *Agent) ingest(device string, props map[string]any, idHint string) []string {
	t := a.now()
	changes := a.hw.Apply(t, device, props)
	if len(changes) == 0 && idHint != "" {
		// Реплеер прислал уже известное значение: вход фиксируется, но не будит.
		var v any
		for _, x := range props {
			v = x
		}
		prop := ""
		for p := range props {
			prop = p
		}
		a.emitInput("sensor", idHint, trace.F{"device": device, "property": prop, "value": v})
		a.Suppress(gate.Event{InputID: idHint, Device: device, Prop: prop, Value: v}, gate.ReasonNoChange)
		return []string{idHint}
	}
	self := a.isSelfEcho(device, t)
	var ids []string
	for _, ch := range changes {
		id := idHint
		if id == "" || len(changes) > 1 {
			id = a.newID("s")
		}
		f := trace.F{"device": ch.Device, "property": ch.Prop, "value": ch.Value}
		if ch.HadOld {
			f["old"] = ch.Old
		}
		if self {
			f["self"] = true
		}
		a.emitInput("sensor", id, f)
		a.hw.Record(hwctx.Event{T: t, Device: ch.Device, Prop: ch.Prop, Value: ch.Value, Old: ch.Old, InputID: id})
		ev := gate.Event{InputID: id, T: t, Kind: "sensor", Device: ch.Device, Prop: ch.Prop, Value: ch.Value, Self: self}
		if ch.HadOld {
			ev.Old = ch.Old
		}
		a.gate.OnEvent(ev)
		ids = append(ids, id)
	}
	return ids
}

// onUpdate — уведомление resources/updated от MCP-сервера.
func (a *Agent) onUpdate(ctx context.Context, u mcphost.Update) {
	if a.isClosed() {
		return
	}
	a.m.inc("mcp.updates")
	var (
		device string
		props  map[string]any
		ok     bool
	)
	if st, has := u.Meta["state"].(map[string]any); has {
		// Сервер положил новое состояние прямо в уведомление: чтение не нужно.
		_, props, ok = hwctx.ParseStatePayload(map[string]any{"properties": st})
		device, _ = u.Meta["device"].(string)
		if device == "" {
			device = hwctx.DeviceOfURI(u.URI)
		}
	} else {
		text, err := a.host.ReadResource(ctx, u.Server, u.URI)
		if err != nil {
			a.traceError("mcp_read", err)
			return
		}
		device, props, ok = hwctx.ParseState(u.URI, text)
	}
	if !ok || device == "" {
		a.traceError("mcp_state", fmt.Errorf("не удалось разобрать состояние %s", u.URI))
		return
	}
	a.ingest(device, props, "")
}

// writeLike — похоже ли имя инструмента на команду приводу.
func writeLike(name string) bool {
	n := strings.ToLower(name)
	for _, w := range []string{"write", "set", "invoke", "toggle", "turn", "switch", "actuate"} {
		if strings.Contains(n, w) {
			return true
		}
	}
	return false
}

// markSelfEcho запоминает устройство, которому агент только что успешно
// отправил команду: его ближайшие изменения — эхо команды, а не внешнее событие.
func (a *Agent) markSelfEcho(tool string, args map[string]any) {
	win := a.cfg.Policy.Gate.SelfEchoS
	if win <= 0 || !writeLike(tool) {
		return
	}
	var devices []string
	for _, k := range []string{"device", "device_id"} {
		if d, ok := args[k].(string); ok && d != "" {
			devices = append(devices, d)
		}
	}
	if len(devices) == 0 { // каталог explicit: устройство — в имени инструмента (set_<dev>_<prop>)
		for _, d := range a.hw.Inventory().Devices {
			if strings.Contains(tool, d.ID) {
				devices = append(devices, d.ID)
			}
		}
	}
	until := a.now() + win
	a.selfMu.Lock()
	for _, d := range devices {
		a.selfUntil[d] = until
	}
	a.selfMu.Unlock()
}

func (a *Agent) isSelfEcho(device string, t float64) bool {
	a.selfMu.Lock()
	defer a.selfMu.Unlock()
	until, ok := a.selfUntil[device]
	return ok && t <= until
}

// ---------------------------------------------------------------- heartbeat

func (a *Agent) armHeartbeat() {
	period := config.Seconds(a.cfg.Policy.HeartbeatMin * 60)
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.closed {
		return
	}
	if a.hbTm != nil {
		a.hbTm.Stop()
	}
	a.hbTm = a.clk.AfterFunc(period, a.onHeartbeat)
}

func (a *Agent) onHeartbeat() {
	if a.isClosed() {
		return
	}
	a.armHeartbeat() // фиксированный период; в hybrid таймер сбрасывают пробуждения
	id := a.newID("hb")
	a.emitInput("tick", id, nil)
	a.mu.Lock()
	pending := a.hbQueued
	a.hbQueued = true
	a.mu.Unlock()
	if pending {
		// Предыдущий тик ещё ждёт в очереди (LLM медленнее периода): тики не копятся.
		a.Suppress(gate.Event{InputID: id}, reasonHeartbeatPending)
		return
	}
	a.submit(&wake{id: a.newID("w"), reason: "heartbeat", prio: sched.Background, inputIDs: []string{id},
		submitWall: time.Now(), submitT: a.now()})
}

// heartbeatDequeued снимает отметку «тик в очереди» (ход начат или отклонён).
func (a *Agent) heartbeatDequeued(w *wake) {
	if w.reason != "heartbeat" {
		return
	}
	a.mu.Lock()
	a.hbQueued = false
	a.mu.Unlock()
}

// ----------------------------------------------------------- время (T0)

// AdvanceOptions — параметры продвижения виртуальных часов.
type AdvanceOptions struct {
	// MinInputs — дождаться, пока число входов каждого вида достигнет порога
	// (например {"sensor": 120}), прежде чем двигать время: так реплеер
	// синхронизируется с уведомлениями sim-mcp, которые идут отдельным потоком.
	MinInputs map[string]int
	// Timeout — предел ожидания MinInputs (по умолчанию 5 с).
	Timeout time.Duration
}

// AdvanceResult — итог продвижения.
type AdvanceResult struct {
	NowT        float64        `json:"now_t"`
	TimersFired int            `json:"timers_fired"`
	Inputs      map[string]int `json:"inputs"`
	Idle        bool           `json:"idle"`
	WaitedMs    float64        `json:"waited_ms"`
	MissingIn   map[string]int `json:"missing_inputs,omitempty"`
}

// Advance продвигает виртуальные часы до toT (секунды от эпохи), исполняя
// таймеры по порядку и дожидаясь затишья агента после каждого.
func (a *Agent) Advance(toT float64, opt AdvanceOptions) (AdvanceResult, error) {
	v, ok := a.clk.(*clock.Virtual)
	if !ok {
		return AdvanceResult{}, ErrNotVirtual
	}
	if a.isClosed() {
		return AdvanceResult{}, ErrClosed
	}
	start := time.Now()
	res := AdvanceResult{}
	if len(opt.MinInputs) > 0 {
		res.MissingIn = a.waitInputs(opt.MinInputs, opt.Timeout)
	}
	settleTimeout := config.Seconds(a.cfg.Clock.SettleTimeoutS)
	settle := func() {
		if !a.tracker.WaitIdle(settleTimeout) {
			a.traceError("settle", fmt.Errorf("агент не пришёл в затишье за %s (активных работ: %d)",
				settleTimeout, a.tracker.Active()))
		}
	}
	fired, err := v.AdvanceTo(clock.At(v, toT), settle)
	if err != nil {
		return res, err
	}
	res.NowT = a.now()
	res.TimersFired = fired
	res.Idle = a.tracker.Active() == 0
	res.Inputs = a.inputCounts()
	res.WaitedMs = float64(time.Since(start).Microseconds()) / 1000
	return res, nil
}

func (a *Agent) waitInputs(min map[string]int, timeout time.Duration) map[string]int {
	if timeout <= 0 {
		timeout = 5 * time.Second
	}
	deadline := time.Now().Add(timeout)
	tm := time.AfterFunc(timeout, func() {
		a.inMu.Lock()
		a.inCond.Broadcast()
		a.inMu.Unlock()
	})
	defer tm.Stop()
	a.inMu.Lock()
	defer a.inMu.Unlock()
	for {
		missing := map[string]int{}
		for k, n := range min {
			if a.inputs[k] < n {
				missing[k] = n - a.inputs[k]
			}
		}
		if len(missing) == 0 {
			return nil
		}
		if !time.Now().Before(deadline) {
			return missing
		}
		a.inCond.Wait()
	}
}

func (a *Agent) inputCounts() map[string]int {
	a.inMu.Lock()
	defer a.inMu.Unlock()
	out := make(map[string]int, len(a.inputs))
	for k, v := range a.inputs {
		out[k] = v
	}
	return out
}

// Idle — нет ли незавершённой работы.
func (a *Agent) Idle() bool { return a.tracker.Active() == 0 }

// WaitIdle ждёт затишья (для тестов и остановки).
func (a *Agent) WaitIdle(timeout time.Duration) bool { return a.tracker.WaitIdle(timeout) }

// ------------------------------------------------------------------ остановка

func (a *Agent) isClosed() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.closed
}

// Close останавливает агента: таймеры и пробы связи, окно коалесценции,
// планировщик (текущие ходы дорабатывают не дольше timeout, затем их обращения
// прерываются; очередь отклоняется), MCP-сессии; пишет run_end, если был
// run_start.
func (a *Agent) Close(timeout time.Duration) error {
	a.mu.Lock()
	if a.closed {
		a.mu.Unlock()
		return nil
	}
	a.closed = true
	started := a.started
	for _, tm := range []clock.Timer{a.hbTm, a.profileTm, a.probeTm} {
		if tm != nil {
			tm.Stop()
		}
	}
	a.mu.Unlock()
	a.stopProbes()

	// Входы, застрявшие в дебаунсе и окне коалесценции, получают исход shutdown;
	// ходы в очереди и в работе дорабатывают, отложенные до связи с LLM — отклоняются.
	a.gate.Drain("shutdown")
	idle := a.tracker.WaitIdle(timeout)
	a.cancel()
	a.dropDeferred()
	a.sched.Stop()
	a.host.Close()
	if !started {
		return nil
	}
	counts := a.tr.Counts()
	u := a.sched.Usage()
	a.tr.Emit(trace.RunEnd, trace.F{
		"idle":       idle,
		"records":    counts,
		"llm_calls":  a.m.get("llm.requests"),
		"tokens_in":  a.m.get("llm.prompt_tokens"),
		"tokens_out": a.m.get("llm.completion_tokens"),
		"budget":     u,
		"wall_s":     time.Since(a.startWall).Seconds(),
	})
	return nil
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
