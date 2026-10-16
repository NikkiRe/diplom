// Package robot — ядро MCP-адаптера робота R-compact v2 (research/new_topic/robot_scenario.md §6–7):
// задачи уровня «место» (go_to, patrol, look, stop) исполняются на плате через rosbridge, в LLM уходят
// короткие сводки, а состояние робота — событиями (notifications/resources/updated) только на значимые
// изменения: итог задачи, тревога (blocked, stuck, low_battery, person).
//
// Состояние для агента (L1, ресурс robot://state/robot):
//
//	place        ближайшее именованное место (≤ place_radius_m), в пути — "between", до первой позы — "unknown"
//	nav          idle | moving | succeeded | aborted | canceled (итог последней задачи)
//	battery_pct  заряд, шаг 5 % (эмуляция через /control/battery или тема /battery_state)
//	alert        none | blocked | stuck | low_battery | person
//	done         итог последней задачи, начатой LLM: {seq, task, place, status, …}; задачи правил
//	             (_meta edgectx/by = rule) его не меняют и LLM не будят; свой stop его тоже не меняет,
//	             поэтому status canceled — цель отменил не адаптер (другой клиент rosbridge)
//	seen         последняя значимая находка детектора на обходе {label, place, …} (заглушка детектора)
//
// Время — только из Clock: таймеры «застрял» и таймаут цели считаются по часам адаптера (в T1 их ведёт
// реплеер через /control/clock), поэтому go_to асинхронный: синхронное ожидание сим. времени в ходе агента
// заблокировало бы реплеер (robot_scenario.md §10.2).
//
// Цели Nav2 отменяются сервисом «отменить все» (rosbridge.Client.CancelAllGoals): по stop, по таймауту участка
// и при завершении адаптера; цели, оставшиеся после обрыва rosbridge или от прошлого процесса, отменяет сам
// клиент при подключении (rosbridge.Options.Actions).
//
// Устаревшие данные в сводке помечаются, а не выдаются за текущие: место при потере связи — place_age_s
// (сколько секунд связи нет), сдвиг робота извне в простое — displaced (место по цели сбрасывается, дальше —
// по позе AMCL), скан — только снятый там, где робот стоит сейчас (иначе нет данных), с lidar_age_s, если он
// старше ScanMaxAge и обновить его не удалось.
package robot

import (
	"context"
	"errors"
	"log/slog"
	"math"
	"sort"
	"sync"
	"time"

	"github.com/local/robotmcp/internal/clock"
	"github.com/local/robotmcp/internal/imgutil"
	"github.com/local/robotmcp/internal/rosbridge"
	"github.com/local/robotmcp/internal/trace"
)

// Состояния навигации и тревоги.
const (
	NavIdle      = "idle"
	NavMoving    = "moving"
	NavSucceeded = "succeeded"
	NavAborted   = "aborted"
	NavCanceled  = "canceled"

	AlertNone       = "none"
	AlertBlocked    = "blocked"
	AlertStuck      = "stuck"
	AlertLowBattery = "low_battery"
	AlertPerson     = "person"

	Between = "between"
	Unknown = "unknown"

	ByLLM  = "llm"
	ByRule = "rule"
)

// NavStates и Alerts — перечисления для инвентаря.
var (
	NavStates = []string{NavIdle, NavMoving, NavSucceeded, NavAborted, NavCanceled}
	Alerts    = []string{AlertNone, AlertBlocked, AlertStuck, AlertLowBattery, AlertPerson}
)

const (
	navType   = "nav2_msgs/action/NavigateToPose"
	scanType  = "sensor_msgs/msg/LaserScan"
	amclType  = "geometry_msgs/msg/PoseWithCovarianceStamped"
	imageType = "sensor_msgs/msg/CompressedImage"
	battType  = "sensor_msgs/msg/BatteryState"
	twistType = "geometry_msgs/msg/TwistStamped"
)

// Options — параметры ядра.
type Options struct {
	Clock    clock.Clock
	Bridge   *rosbridge.Client
	Logger   *slog.Logger
	Trace    *trace.Writer
	Detector Detector

	Payload        string        // summary | raw (R-raw: status/look отдают сырые /amcl_pose и /scan)
	MotionScanMs   int           // подписка /scan с этим throttle_rate только во время движения (0 — нет)
	PoseThrottleMs int           // throttle_rate подписки /amcl_pose (rosbridge отдаёт последнюю позу интервала)
	ScanMaxAge     float64       // с: в простое скан из кэша не старше этого и снятый после остановки
	SensorTimeout  time.Duration // разовое чтение /scan
	FrameTimeout   time.Duration // кадр ленивой камеры
	ImageWidth     int           // ширина кадра для LLM (0 — не уменьшать)
	JPEGQuality    int
	GoalTimeout    float64 // с по часам адаптера: цель дольше — отмена, alert stuck
	StuckS         float64 // с без обновления позы при активной цели — alert stuck
	ObstacleM      float64 // препятствие впереди ближе (по скану движения) …
	ObstacleHoldS  float64 // … дольше этого — alert blocked до прерывания цели Nav2
	LowBatteryPct  float64
	BatteryTopic   bool // заряд из темы (иначе эмуляция через SetBattery)
	BatteryStart   float64
	NotifyContext  bool // уведомлять и об изменениях place/battery_pct (по умолчанию — только значимые)
	SimStepIters   int  // --sim-step: при разовом чтении на паузе мира шагать его на N итераций
	SimStepWait    time.Duration

	// OnEvent получает уведомление (вызывается под мьютексом ядра: должен только ставить в очередь).
	OnEvent func(Event)
}

// Event — уведомление об изменении состояния.
type Event struct {
	Kind    string         // task_end | alert | context
	State   map[string]any // полное состояние
	Changed []string       // изменившиеся свойства
	Detail  map[string]any
}

// Сдвиг позы, заметный для AMCL: он публикует позу при сдвиге ≥ 0,25 м или повороте ≥ 0,2 рад
// (docs/robot_sim.md §12.7 п. 6), а повтор латченной позы после переподключения совпадает с прежней.
// Пороги ниже шага AMCL и запас окна settleMargin — допущение (на живом AMCL не проверялось; кандидаты в
// параметры предрегистрации).
const (
	shiftM       = 0.1
	shiftRad     = 0.1
	settleMargin = time.Second
)

// Robot — ядро адаптера.
type Robot struct {
	cfg    *Config
	o      Options
	clk    clock.Clock
	br     *rosbridge.Client
	log    *slog.Logger
	tr     *trace.Writer
	ctx    context.Context
	settle time.Duration // окно после остановки для поз поездки, задержанных прореживанием

	mu        sync.Mutex
	pose      pose
	amclRaw   map[string]any
	scan      *Scan
	battery   float64 // −1 — неизвестно
	task      *task
	taskSeq   int
	doneSeq   int
	nav       string
	snap      string    // место, где Nav2 подтвердил прибытие (до следующего движения)
	lastStop  float64   // конец последней задачи (часы адаптера)
	stopWall  time.Time // он же по стенным часам — для окна settle
	displaced bool      // в простое робота сдвинули извне (или AMCL перелокализовался); до следующей задачи
	shiftedAt float64   // последний такой сдвиг
	offline   bool      // нет связи с rosbridge …
	offlineAt float64   // … с этого момента
	alert     string
	done      map[string]any
	seen      map[string]any
	reported  map[string]any
	motionSub string
	cnt       map[string]int64
}

type pose struct {
	x, y, yaw, t float64
	ok           bool
}

type task struct {
	seq      int
	kind     string // go_to | patrol | look
	queue    []string
	cur      string
	by       string
	look     bool
	quiet    bool // итог не публикуется в done: LLM его уже знает (go_to с wait=true, stop)
	goalID   string
	start    float64
	legStart float64
	lastPose float64
	stuck    bool
	blocked  bool
	obsSince float64
	total    int
	visited  int
	seen     []map[string]any
	view     map[string]any
	rb0      rosbridge.Stats
	ended    chan struct{}
	result   chan map[string]any
}

// New создаёт ядро.
func New(cfg *Config, o Options) *Robot {
	if o.Logger == nil {
		o.Logger = slog.New(slog.DiscardHandler)
	}
	if o.Detector == nil {
		o.Detector = None{}
	}
	if o.Payload == "" {
		o.Payload = "summary"
	}
	if o.SensorTimeout <= 0 {
		o.SensorTimeout = 1500 * time.Millisecond
	}
	if o.FrameTimeout <= 0 {
		o.FrameTimeout = 3 * time.Second
	}
	if o.JPEGQuality <= 0 {
		o.JPEGQuality = 70
	}
	if o.SimStepWait <= 0 {
		o.SimStepWait = 300 * time.Millisecond
	}
	r := &Robot{cfg: cfg, o: o, clk: o.Clock, br: o.Bridge, log: o.Logger, tr: o.Trace, ctx: context.Background(),
		settle: time.Duration(o.PoseThrottleMs)*time.Millisecond + settleMargin, battery: -1, nav: NavIdle,
		lastStop: math.Inf(-1), shiftedAt: math.Inf(-1), alert: AlertNone, cnt: map[string]int64{}}
	if !o.BatteryTopic {
		r.battery = o.BatteryStart
	}
	r.reported = r.stateLocked()
	return r
}

// Config — конфигурация мира.
func (r *Robot) Config() *Config { return r.cfg }

// Options — параметры ядра.
func (r *Robot) Options() Options { return r.o }

// Start подписывается на позу AMCL (латченная, событийная) и заряд, объявляет /cmd_vel. Подписки
// восстанавливаются клиентом rosbridge после переподключения.
func (r *Robot) Start(ctx context.Context) {
	r.ctx = ctx
	r.br.OnLink(r.onLink)
	t := r.cfg.Topics
	r.br.Advertise(ctx, t.CmdVel, twistType)
	r.br.Subscribe(ctx, rosbridge.Sub{Topic: t.AMCL, Type: amclType, ThrottleMs: r.o.PoseThrottleMs}, r.onPose)
	if r.o.BatteryTopic {
		r.br.Subscribe(ctx, rosbridge.Sub{Topic: t.Battery, Type: battType, ThrottleMs: 5000}, r.onBattery)
	}
}

func (r *Robot) inc(k string) { r.cnt[k]++ }

func (r *Robot) onLink(up bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.offline = !up
	if !up {
		r.offlineAt = r.clk.Now()
	}
}

// ---------------------------------------------------------------------------------------------
// Состояние

func q5(pct float64) any {
	if pct < 0 {
		return nil
	}
	return int(math.Round(pct/5) * 5)
}

// placeLocked — место для L1 и расстояние до него.
func (r *Robot) placeLocked() (string, float64) {
	if r.task != nil {
		if r.pose.ok {
			_, d := r.cfg.Nearest(r.pose.x, r.pose.y)
			return Between, round2(d)
		}
		return Between, 0
	}
	if r.snap != "" {
		// Nav2 подтвердил прибытие (в допуске цели). Поза AMCL после остановки может быть устаревшей на
		// 0,25 м и больше (обновляется по сдвигу ≥ 0,25 м; docs/robot_sim.md §12.7 п. 6) — тогда расстояние
		// не сообщается (−1), а не выдумывается.
		p, _ := r.cfg.Place(r.snap)
		if r.pose.ok && r.pose.t >= r.lastStop {
			return r.snap, round2(dist(r.pose.x, r.pose.y, p.X(), p.Y()))
		}
		return r.snap, -1
	}
	if !r.pose.ok {
		return Unknown, 0
	}
	p, d := r.cfg.Nearest(r.pose.x, r.pose.y)
	if d <= r.cfg.PlaceRadiusM {
		return p.ID, round2(d)
	}
	return Between, round2(d)
}

func (r *Robot) stateLocked() map[string]any {
	place, _ := r.placeLocked()
	nav := r.nav
	if r.task != nil {
		nav = NavMoving
	}
	return map[string]any{"place": place, "nav": nav, "battery_pct": q5(r.battery), "alert": r.alert,
		"done": r.done, "seen": r.seen}
}

// State — текущее состояние (ресурс robot://state/robot).
func (r *Robot) State() map[string]any {
	r.mu.Lock()
	defer r.mu.Unlock()
	st := r.stateLocked()
	st["device"] = "robot"
	return st
}

// emitLocked отправляет уведомление с полным состоянием и списком изменившихся свойств.
func (r *Robot) emitLocked(kind string, detail map[string]any) {
	st := r.stateLocked()
	var changed []string
	for k, v := range st {
		if !equalJSON(v, r.reported[k]) {
			changed = append(changed, k)
		}
	}
	sort.Strings(changed)
	r.reported = st
	r.inc("notify_" + kind)
	r.tr.Emit("notify", map[string]any{"event": kind, "changed": changed, "state": st, "detail": detail})
	if r.o.OnEvent != nil {
		r.o.OnEvent(Event{Kind: kind, State: st, Changed: changed, Detail: detail})
	}
}

func (r *Robot) raiseLocked(kind string, detail map[string]any) {
	if r.alert == kind && kind != AlertPerson {
		return
	}
	if r.alert == AlertLowBattery && kind != AlertLowBattery {
		// низкий заряд важнее: тревога остаётся, но событие сообщается
		detail = withKV(detail, "also", kind)
	} else {
		r.alert = kind
	}
	d := withKV(detail, "alert", kind)
	r.tr.Emit("alert", d)
	r.emitLocked("alert", d)
}

func withKV(m map[string]any, k string, v any) map[string]any {
	out := map[string]any{k: v}
	for kk, vv := range m {
		out[kk] = vv
	}
	return out
}

// ---------------------------------------------------------------------------------------------
// Входы из rosbridge

func (r *Robot) onPose(m map[string]any) {
	p := rosbridge.Obj(m, "pose", "pose")
	x, ok1 := rosbridge.Num(p, "position", "x")
	y, ok2 := rosbridge.Num(p, "position", "y")
	if !ok1 || !ok2 {
		return
	}
	yaw := yawOf(rosbridge.Obj(p, "orientation"))
	r.mu.Lock()
	defer r.mu.Unlock()
	now := r.clk.Now()
	prev := r.pose
	r.pose = pose{x: x, y: y, yaw: yaw, t: now, ok: true}
	r.amclRaw = m
	r.inc("amcl_rx")
	if t := r.task; t != nil {
		t.lastPose = now // AMCL публикует позу только при сдвиге ≥ 0,25 м / повороте ≥ 0,2 рад: это и есть продвижение
		if t.stuck {
			t.stuck = false
			if r.alert == AlertStuck {
				r.alert = AlertNone // движение возобновилось: тревога снимается без уведомления (уйдёт с итогом)
			}
		}
		return
	}
	// В простое новая поза (позже поз поездки, задержанных прореживанием) — робота сдвинули извне или AMCL
	// перелокализовался: место по цели и снятый до сдвига скан больше не верны. Окно settle — по стенным
	// часам, как прореживание в rosbridge: в T1 виртуальные часы прыгают вперёд за миллисекунды.
	if prev.ok && time.Since(r.stopWall) > r.settle && shifted(prev, r.pose) {
		r.shiftedAt = now
		if !r.displaced {
			r.displaced, r.snap = true, ""
			r.inc("displaced")
			place, _ := r.placeLocked()
			r.tr.Emit("displaced", map[string]any{"m": round2(dist(prev.x, prev.y, x, y)), "place": place})
		}
	}
	if r.o.NotifyContext {
		if st := r.stateLocked(); !equalJSON(st["place"], r.reported["place"]) {
			r.emitLocked("context", map[string]any{"why": "place"})
		}
	}
}

func (r *Robot) onBattery(m map[string]any) {
	pct, ok := m["percentage"].(float64)
	if !ok || math.IsNaN(pct) {
		return
	}
	if pct <= 1.0 {
		pct *= 100 // sensor_msgs/BatteryState: 0..1
	}
	r.SetBattery(pct)
}

// SetBattery задаёт заряд, % (эмуляция: POST /control/battery; или тема /battery_state).
func (r *Robot) SetBattery(pct float64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	old := q5(r.battery)
	r.battery = math.Max(0, math.Min(100, pct))
	switch {
	case r.battery <= r.o.LowBatteryPct && r.alert != AlertLowBattery:
		r.alert = AlertLowBattery
		d := map[string]any{"alert": AlertLowBattery, "battery_pct": q5(r.battery)}
		r.tr.Emit("alert", d)
		r.emitLocked("alert", d)
	case r.battery > r.o.LowBatteryPct+5 && r.alert == AlertLowBattery:
		r.alert = AlertNone // снятие тревоги — без уведомления, уйдёт в следующем
	}
	if r.o.NotifyContext && !equalJSON(old, q5(r.battery)) {
		r.emitLocked("context", map[string]any{"why": "battery"})
	}
}

func (r *Robot) onMotionScan(m map[string]any) {
	now := r.clk.Now()
	s, err := ParseScan(m, now)
	if err != nil {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.scan = s
	r.inc("scan_motion_rx")
	t := r.task
	if t == nil || r.o.ObstacleM <= 0 || t.blocked {
		return
	}
	if f := s.Front(); f < r.o.ObstacleM {
		if t.obsSince == 0 {
			t.obsSince = now
		} else if now-t.obsSince >= r.o.ObstacleHoldS {
			t.blocked = true
			r.raiseLocked(AlertBlocked, map[string]any{"front_m": round2(f), "goal": t.cur, "why": "obstacle"})
		}
	} else {
		t.obsSince = 0
	}
}

// ---------------------------------------------------------------------------------------------
// Таймеры

// Tick проверяет таймеры по часам адаптера (раз в секунду в реальных часах, на каждом /control/clock
// в виртуальных).
func (r *Robot) Tick() {
	now := r.clk.Now()
	r.mu.Lock()
	t := r.task
	if t == nil {
		r.mu.Unlock()
		return
	}
	timedOut := r.o.GoalTimeout > 0 && now-t.legStart > r.o.GoalTimeout
	id := t.goalID
	if timedOut {
		r.raiseLocked(AlertStuck, map[string]any{"goal": t.cur, "why": "timeout", "s": math.Round(now - t.legStart)})
		r.finishLocked(t, NavAborted)
	} else if r.o.StuckS > 0 && !t.stuck && now-math.Max(t.lastPose, t.legStart) > r.o.StuckS {
		t.stuck = true
		r.raiseLocked(AlertStuck, map[string]any{"goal": t.cur, "why": "no progress", "s": math.Round(now - t.lastPose)})
	}
	r.mu.Unlock()
	if timedOut {
		r.br.Forget(id)
		// итог — в журнале (OnCancel клиента); неподтверждённая отмена повторится при подключении
		_ = r.br.CancelAllGoals(r.ctx, r.cfg.Topics.Nav, "timeout")
	}
}

// ---------------------------------------------------------------------------------------------
// Задачи

func goalArgs(cfg *Config, p Place) map[string]any {
	yaw := 0.0
	if p.YawDeg != nil {
		yaw = *p.YawDeg * math.Pi / 180
	}
	return map[string]any{"pose": map[string]any{"header": map[string]any{"frame_id": cfg.Frame},
		"pose": map[string]any{"position": map[string]any{"x": p.X(), "y": p.Y(), "z": 0.0},
			"orientation": map[string]any{"x": 0.0, "y": 0.0, "z": math.Sin(yaw / 2), "w": math.Cos(yaw / 2)}}}}
}

// etaLocked — оценка времени маршрута от текущей позы, с (−1 — поза неизвестна).
func (r *Robot) etaLocked(places []string) float64 {
	if !r.pose.ok {
		return -1
	}
	x, y := r.pose.x, r.pose.y
	total := 0.0
	for _, id := range places {
		p, _ := r.cfg.Place(id)
		total += r.cfg.ETA(dist(x, y, p.X(), p.Y()))
		x, y = p.X(), p.Y()
	}
	return math.Round(total)
}

// hereLocked — робот стоит в месте id (в допуске цели).
func (r *Robot) hereLocked(id string) bool {
	if r.task != nil {
		return false
	}
	if r.snap == id {
		return true
	}
	p, _ := r.cfg.Place(id)
	return r.pose.ok && dist(r.pose.x, r.pose.y, p.X(), p.Y()) <= r.cfg.GoalTolM
}

// begin создаёт задачу, вытесняя активную (Nav2 вытесняет цель новой целью — отдельная отмена не нужна).
func (r *Robot) beginLocked(kind string, places []string, by string) *task {
	if old := r.task; old != nil {
		r.br.Forget(old.goalID)
		r.endLocked(old, "preempted")
	}
	r.taskSeq++
	now := r.clk.Now()
	t := &task{seq: r.taskSeq, kind: kind, queue: append([]string(nil), places...), by: by, start: now,
		total: len(places), ended: make(chan struct{}), result: make(chan map[string]any, 1), rb0: r.br.Stats()}
	r.task = t
	r.snap, r.displaced = "", false
	if r.alert != AlertLowBattery {
		r.alert = AlertNone // тревога прошлой задачи снимается без уведомления
	}
	r.inc("task_" + kind)
	r.tr.Emit("task_start", map[string]any{"seq": t.seq, "task": kind, "places": places, "by": by})
	if r.o.MotionScanMs > 0 && r.motionSub == "" {
		r.motionSub = "pending"
	}
	return t
}

// startMotionScan / stopMotionScan — подписка на /scan только на время движения.
func (r *Robot) startMotionScan() {
	r.mu.Lock()
	need := r.motionSub == "pending"
	r.mu.Unlock()
	if !need {
		return
	}
	id := r.br.Subscribe(r.ctx, rosbridge.Sub{Topic: r.cfg.Topics.Scan, Type: scanType, ThrottleMs: r.o.MotionScanMs,
		Compression: "cbor"}, r.onMotionScan)
	r.mu.Lock()
	if r.motionSub == "pending" && r.task != nil {
		r.motionSub = id
		id = ""
	}
	r.mu.Unlock()
	if id != "" { // задача уже кончилась
		r.br.Unsubscribe(r.ctx, id)
	}
}

func (r *Robot) stopMotionScanLocked() {
	if id := r.motionSub; id != "" {
		r.motionSub = ""
		if id != "pending" {
			go r.br.Unsubscribe(r.ctx, id)
		}
	}
}

// launch отправляет цель текущего участка задачи.
func (r *Robot) launch(t *task) {
	r.mu.Lock()
	if r.task != t || len(t.queue) == 0 {
		r.mu.Unlock()
		return
	}
	p, _ := r.cfg.Place(t.queue[0])
	id := r.br.NewID("nav")
	now := r.clk.Now()
	t.goalID, t.cur, t.legStart, t.lastPose = id, p.ID, now, now
	t.stuck, t.blocked, t.obsSince = false, false, 0
	r.mu.Unlock()
	r.startMotionScan()
	// Задачу могли остановить, пока цель ждала идущую отмену: тогда она не отправляется.
	ch, err := r.br.SendGoal(r.ctx, id, r.cfg.Topics.Nav, navType, goalArgs(r.cfg, p), func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.task == t && t.goalID == id
	})
	if err != nil {
		r.onResult(t, id, map[string]any{"op": "disconnected", "error": err.Error()})
		return
	}
	go func() {
		select {
		case m := <-ch:
			r.onResult(t, id, m)
		case <-t.ended:
		case <-r.ctx.Done():
		}
	}()
}

func navStatus(m map[string]any) string {
	if m["op"] == "disconnected" {
		return "disconnected"
	}
	switch st, _ := m["status"].(float64); int(st) {
	case 4:
		return NavSucceeded
	case 5:
		return NavCanceled
	}
	return NavAborted
}

// onResult — итог цели участка.
func (r *Robot) onResult(t *task, id string, m map[string]any) {
	r.mu.Lock()
	if r.task != t || t.goalID != id {
		r.mu.Unlock()
		return
	}
	now := r.clk.Now()
	st := navStatus(m)
	r.tr.Emit("leg_end", map[string]any{"seq": t.seq, "place": t.cur, "status": st, "s": round1(now - t.legStart)})
	switch st {
	case NavSucceeded:
		r.snap = t.cur
		t.visited++
		t.queue = t.queue[1:]
		if t.kind == "patrol" && t.look {
			r.detectLocked(t)
		}
		if t.kind == "look" {
			r.mu.Unlock()
			view := r.lookSummary(r.ctx, t.cur)
			r.mu.Lock()
			if r.task != t {
				r.mu.Unlock()
				return
			}
			t.view = view
		}
		if len(t.queue) > 0 {
			r.snap = ""
			r.mu.Unlock()
			r.launch(t)
			return
		}
		r.finishLocked(t, NavSucceeded)
	case NavCanceled: // свои stop и вытеснение закрывают задачу раньше: цель отменил кто-то другой
		r.finishLocked(t, NavCanceled)
	default:
		why := "nav2 aborted"
		if st == "disconnected" {
			why = "rosbridge disconnected"
		}
		d := map[string]any{"goal": t.cur, "why": why}
		if r.scan != nil && now-r.scan.T < 5 {
			if f := r.scan.Front(); !math.IsInf(f, 1) {
				d["front_m"] = round2(f)
			}
		}
		if !t.blocked {
			t.blocked = true
			r.raiseLocked(AlertBlocked, d)
		}
		r.finishLocked(t, NavAborted)
	}
	r.mu.Unlock()
}

// detectLocked — детектор на точке обхода; человек → тревога person и seen.
func (r *Robot) detectLocked(t *task) {
	objs := r.o.Detector.Detect(t.cur, nil)
	for _, o := range objs {
		if o.Label == "person" {
			s := map[string]any{"label": o.Label, "place": t.cur}
			if o.M > 0 {
				s["m"] = round2(o.M)
			}
			r.seen = s
			t.seen = append(t.seen, s)
			r.raiseLocked(AlertPerson, map[string]any{"place": t.cur, "objects": objectsJSON(objs)})
		}
	}
}

// endLocked закрывает задачу без уведомления (вытеснение).
func (r *Robot) endLocked(t *task, status string) {
	if r.task == t {
		r.task = nil
	}
	select {
	case <-t.ended:
	default:
		close(t.ended)
	}
	now := r.clk.Now()
	rb := r.br.Stats()
	r.inc("task_end_" + status)
	r.tr.Emit("task_end", map[string]any{"seq": t.seq, "task": t.kind, "status": status, "by": t.by,
		"s": round1(now - t.start), "visited": t.visited, "total": t.total,
		"rb_tcp_rx": rb.TCPRx - t.rb0.TCPRx, "rb_tcp_tx": rb.TCPTx - t.rb0.TCPTx,
		"rb_msg_rx": rb.MsgRx - t.rb0.MsgRx, "rb_msg_tx": rb.MsgTx - t.rb0.MsgTx})
	if r.task == nil && status != "preempted" { // при вытеснении подписка движения остаётся новой задаче
		r.stopMotionScanLocked()
	}
	res := map[string]any{"ok": status == NavSucceeded, "place": t.cur, "status": status, "s": round1(now - t.start)}
	if t.kind == "patrol" {
		res["visited"] = t.visited
	}
	select {
	case t.result <- res:
	default:
	}
}

// finishLocked — итог задачи: состояние, done (если задачу начала LLM), уведомление task_end.
func (r *Robot) finishLocked(t *task, status string) {
	r.endLocked(t, status)
	r.nav = status
	r.lastStop, r.stopWall = r.clk.Now(), time.Now()
	if status != NavSucceeded {
		r.snap = ""
	}
	if t.by != ByRule && !t.quiet {
		r.doneSeq++
		d := map[string]any{"seq": r.doneSeq, "task": t.kind, "place": t.cur, "status": status}
		if t.kind == "patrol" {
			d["visited"] = t.visited
		}
		if len(t.seen) > 0 {
			d["seen"] = t.seen
		}
		if t.view != nil {
			d["view"] = t.view
		}
		r.done = d
	}
	r.emitLocked("task_end", map[string]any{"task": t.kind, "status": status, "by": t.by, "seq": t.seq})
}

// ---------------------------------------------------------------------------------------------
// Инструменты

func errBody(msg string, extra ...any) map[string]any {
	m := map[string]any{"ok": false, "error": msg}
	for i := 0; i+1 < len(extra); i += 2 {
		m[extra[i].(string)] = extra[i+1]
	}
	return m
}

// UnknownPlace — короткая ошибка с перечнем мест (≈15 токенов + места).
func (r *Robot) UnknownPlace(p string) map[string]any {
	return errBody("unknown place", "known", r.cfg.IDs())
}

// GoTo — поездка в место. Асинхронно (по умолчанию): ответ сразу, итог — событием task_end (done).
// wait=true — дождаться итога (только реальные часы; done при этом не меняется).
func (r *Robot) GoTo(ctx context.Context, place, by string, wait bool) map[string]any {
	if _, ok := r.cfg.Place(place); !ok {
		return r.UnknownPlace(place)
	}
	if !r.br.Connected() {
		return errBody("robot offline")
	}
	r.mu.Lock()
	if r.hereLocked(place) {
		r.mu.Unlock()
		return map[string]any{"ok": true, "place": place, "status": "here"}
	}
	eta := r.etaLocked([]string{place})
	if t := r.task; t != nil && !wait && t.kind == "go_to" && t.cur == place { // уже едет туда: цель не повторяем
		r.mu.Unlock()
		return map[string]any{"ok": true, "accepted": place, "eta_s": eta, "status": "already moving"}
	}
	t := r.beginLocked("go_to", []string{place}, by)
	t.quiet = wait
	r.mu.Unlock()
	r.launch(t)
	if wait {
		select {
		case res := <-t.result:
			delete(res, "visited")
			return res
		case <-ctx.Done():
			return errBody("wait interrupted", "status", "running")
		}
	}
	out := map[string]any{"ok": true, "accepted": place}
	if eta >= 0 {
		out["eta_s"] = eta
	}
	return out
}

// Patrol — обход мест по порядку одной задачей (исполняется на плате, без LLM на участках).
// look=true — на каждой точке детектор (с заглушкой none — ничего).
func (r *Robot) Patrol(ctx context.Context, places []string, look bool, by string) map[string]any {
	if len(places) == 0 {
		return errBody("empty route")
	}
	for _, p := range places {
		if _, ok := r.cfg.Place(p); !ok {
			return r.UnknownPlace(p)
		}
	}
	if !r.br.Connected() {
		return errBody("robot offline")
	}
	r.mu.Lock()
	route := append([]string(nil), places...)
	for len(route) > 0 && r.hereLocked(route[0]) { // первая точка — там, где робот уже стоит
		route = route[1:]
	}
	if len(route) == 0 {
		r.mu.Unlock()
		return map[string]any{"ok": true, "place": places[len(places)-1], "status": "here"}
	}
	eta := r.etaLocked(route)
	t := r.beginLocked("patrol", route, by)
	t.look = look
	t.total = len(places)
	t.visited = len(places) - len(route)
	r.mu.Unlock()
	r.launch(t)
	out := map[string]any{"ok": true, "accepted": len(route)}
	if eta >= 0 {
		out["eta_s"] = eta
	}
	return out
}

// Stop — отмена задачи (разрешён всегда). Сервисом отменяются все цели Nav2, в том числе цель, которой
// адаптер не ведёт (осталась после обрыва или от прошлого процесса). Нулевая скорость публикуется после
// ответа Nav2: пока цель жива, контроллер Nav2 перебил бы её своей. Без ответа соединение с rosbridge
// разрывается (rosbridge.Client.CancelAllGoals), и нулевая скорость не уходит.
func (r *Robot) Stop(ctx context.Context) map[string]any {
	r.mu.Lock()
	var cancelled any
	if t := r.task; t != nil {
		cancelled = t.cur
		t.quiet = true
		r.br.Forget(t.goalID)
		r.finishLocked(t, NavCanceled)
	}
	r.mu.Unlock()
	err := r.br.CancelAllGoals(ctx, r.cfg.Topics.Nav, "stop")
	if perr := r.br.Publish(ctx, r.cfg.Topics.CmdVel, map[string]any{"header": map[string]any{"frame_id": "base_link"},
		"twist": map[string]any{}}); err == nil {
		err = perr
	}
	switch {
	case errors.Is(err, rosbridge.ErrDisconnected):
		return errBody("robot offline", "cancelled", cancelled)
	case err != nil:
		return errBody("stop not confirmed", "cancelled", cancelled)
	}
	return map[string]any{"ok": true, "cancelled": cancelled}
}

// Shutdown — завершение адаптера: активная задача закрывается без уведомления, а её цель отменяется (иначе
// робот доехал бы сам: rosbridge не отменяет цели отключившегося клиента). Ожидание ответа ограничено
// CancelTimeout клиента; в простое rosbridge ничего не отправляется.
func (r *Robot) Shutdown(ctx context.Context) error {
	r.mu.Lock()
	t := r.task
	if t != nil {
		r.br.Forget(t.goalID)
		r.endLocked(t, "shutdown")
	}
	r.mu.Unlock()
	if t == nil {
		return nil
	}
	return r.br.CancelAllGoals(ctx, r.cfg.Topics.Nav, "shutdown")
}

// ---------------------------------------------------------------------------------------------
// Чтения: статус, взгляд

func round1(x float64) float64 { return math.Round(x*10) / 10 }

// getScan — скан для сводки: свежий из кэша, иначе разовое чтение в CBOR. Если чтение не удалось —
// прежний скан того же места (нестрогий, fresh = false) или nil.
func (r *Robot) getScan(ctx context.Context) (*Scan, bool) {
	r.mu.Lock()
	s, fresh := r.cachedScanLocked(r.clk.Now())
	if fresh {
		r.inc("scan_cache_hit")
	}
	r.mu.Unlock()
	if fresh {
		return s, true
	}
	m, err := r.br.Once(ctx, rosbridge.Sub{Topic: r.cfg.Topics.Scan, Type: scanType, Compression: "cbor"},
		r.o.SensorTimeout, r.poke())
	r.mu.Lock()
	defer r.mu.Unlock()
	if err != nil {
		r.inc("scan_once_fail")
		return s, false
	}
	ns, err := ParseScan(m, r.clk.Now())
	if err != nil {
		return s, false
	}
	r.inc("scan_once")
	r.scan = ns
	return ns, true
}

// cachedScanLocked — скан из кэша и свежий ли он. В движении годится только скан подписки движения не
// старше 2,5 её периодов (не меньше 3 с). В простое — только снятый после последнего перемещения (конец
// задачи, сдвиг извне): свежий, если не старше ScanMaxAge. Скан другого места — nil: его free_m неверны.
func (r *Robot) cachedScanLocked(now float64) (*Scan, bool) {
	s := r.scan
	switch {
	case s == nil:
		return nil, false
	case r.task != nil:
		motion := r.motionSub != "" && r.motionSub != "pending"
		if motion && now-s.T <= math.Max(3, 2.5*float64(r.o.MotionScanMs)/1000) {
			return s, true
		}
		return nil, false
	case s.T < math.Max(r.lastStop, r.shiftedAt):
		return nil, false
	}
	return s, now-s.T <= r.o.ScanMaxAge
}

// poke — микрошаги мира на паузе, пока ждём сообщение (--sim-step; вызовы в трафик платы не входят по
// смыслу, но считаются клиентом — отчёт вычитает их по счётчику sim_steps).
func (r *Robot) poke() func(context.Context) {
	if r.o.SimStepIters <= 0 {
		return nil
	}
	return func(ctx context.Context) {
		for {
			select {
			case <-ctx.Done():
				return
			case <-time.After(r.o.SimStepWait):
			}
			_, err := r.br.CallService(ctx, r.cfg.Topics.WorldCtrl,
				map[string]any{"world_control": map[string]any{"pause": true, "multi_step": r.o.SimStepIters}}, time.Second)
			r.mu.Lock()
			r.inc("sim_steps")
			r.mu.Unlock()
			if err != nil {
				return
			}
		}
	}
}

// Status — сводка состояния: место, навигация, заряд, тревога, свободное пространство по секторам.
func (r *Robot) Status(ctx context.Context) map[string]any {
	s, fresh := r.getScan(ctx)
	r.mu.Lock()
	defer r.mu.Unlock()
	now := r.clk.Now()
	place, placeM := r.placeLocked()
	st := r.stateLocked()
	out := map[string]any{"alert": r.alert, "nav": st["nav"], "place": place}
	if b := q5(r.battery); b != nil {
		out["battery_pct"] = b
	}
	if r.offline {
		out["place_age_s"] = math.Round(now - r.offlineAt)
	}
	if r.displaced {
		out["displaced"] = true
	}
	if r.o.Payload == "raw" {
		out["amcl_pose"] = RawJSON(r.amclRaw)
		if s != nil {
			out["scan"] = RawJSON(s.Raw)
		}
		return out
	}
	if place != Unknown && placeM >= 0 {
		out["place_m"] = placeM
	}
	if t := r.task; t != nil {
		g := map[string]any{"place": t.cur, "s": math.Round(now - t.start)}
		if p, ok := r.cfg.Place(t.cur); ok && r.pose.ok {
			g["left_m"] = round1(dist(r.pose.x, r.pose.y, p.X(), p.Y()))
		}
		if len(t.queue) > 1 {
			g["then"] = t.queue[1:]
		}
		out["goal"] = g
	}
	if s == nil {
		out["lidar"] = "no data"
	} else {
		out["free_m"] = s.FreeM()
		if !fresh {
			out["lidar_age_s"] = math.Round(now - s.T)
		}
	}
	return out
}

// lookSummary — сводка «что впереди» без кадра (детектор или лидар в поле зрения камеры).
func (r *Robot) lookSummary(ctx context.Context, place string) map[string]any {
	s, fresh := r.getScan(ctx)
	out := map[string]any{}
	if place != "" {
		out["place"] = place
	}
	if r.o.Detector.Name() == "none" {
		out["detector"] = "none"
	} else {
		out["objects"] = objectsJSON(r.o.Detector.Detect(place, nil))
	}
	if r.o.Payload == "raw" {
		if s != nil {
			out["scan"] = RawJSON(s.Raw)
		}
		return out
	}
	if s == nil {
		out["lidar"] = "no data"
	} else {
		out["in_view"] = s.InView(r.cfg.CameraHFOV)
		if !fresh {
			out["lidar_age_s"] = math.Round(r.clk.Now() - s.T)
		}
	}
	return out
}

// Image — кадр для LLM.
type Image struct {
	JPEG []byte
	W, H int
}

// Look — взгляд. place не задан или робот уже там — сводка сразу (image=true — плюс кадр 320×240 JPEG).
// Иначе — асинхронно: поездка, затем сводка в done (кадр в уведомлении не передаётся).
func (r *Robot) Look(ctx context.Context, place string, image bool, by string) (map[string]any, *Image) {
	if place != "" {
		if _, ok := r.cfg.Place(place); !ok {
			return r.UnknownPlace(place), nil
		}
		r.mu.Lock()
		here := r.hereLocked(place)
		if !here {
			if image {
				r.mu.Unlock()
				return errBody("image only at current place: go_to first"), nil
			}
			if !r.br.Connected() {
				r.mu.Unlock()
				return errBody("robot offline"), nil
			}
			eta := r.etaLocked([]string{place})
			t := r.beginLocked("look", []string{place}, by)
			r.mu.Unlock()
			r.launch(t)
			out := map[string]any{"ok": true, "accepted": place, "then": "look"}
			if eta >= 0 {
				out["eta_s"] = eta + 1
			}
			return out, nil
		}
		r.mu.Unlock()
	}
	if !r.br.Connected() {
		return errBody("robot offline"), nil
	}
	if place == "" {
		r.mu.Lock()
		if p, _ := r.placeLocked(); p != Between && p != Unknown {
			place = p
		}
		r.mu.Unlock()
	}
	var img *Image
	var frameNote any
	var wg sync.WaitGroup
	if image {
		wg.Add(1)
		go func() {
			defer wg.Done()
			img, frameNote = r.frame(ctx)
		}()
	}
	out := r.lookSummary(ctx, place)
	wg.Wait()
	if image {
		if img != nil {
			out["frame"] = map[string]any{"w": img.W, "h": img.H, "kb": round1(float64(len(img.JPEG)) / 1024)}
		} else {
			out["frame"] = frameNote
		}
	}
	return out, img
}

// frame — один кадр ленивой камеры (CBOR: JPEG байтами, без base64), уменьшенный на плате.
func (r *Robot) frame(ctx context.Context) (*Image, any) {
	m, err := r.br.Once(ctx, rosbridge.Sub{Topic: r.cfg.Topics.Camera, Type: imageType, Compression: "cbor"},
		r.o.FrameTimeout, r.poke())
	r.mu.Lock()
	defer r.mu.Unlock()
	if err != nil {
		r.inc("frame_timeout")
		return nil, "timeout"
	}
	raw, err := rosbridge.Bytes(m["data"])
	if err != nil {
		return nil, "bad frame"
	}
	out, w, h, err := imgutil.Shrink(raw, r.o.ImageWidth, r.o.JPEGQuality)
	if err != nil {
		return nil, "bad frame"
	}
	r.inc("frames")
	return &Image{JPEG: out, W: w, H: h}, nil
}

// ---------------------------------------------------------------------------------------------
// Инвентарь и диагностика

// Inventory — устройство robot для L0 агента (схема как у устройств помещения: robot_scenario.md §5),
// места, пределы и цена инструментов.
func (r *Robot) Inventory() map[string]any {
	st := r.State()
	ids := r.cfg.IDs()
	placeEnum := append(toAny(ids), Between, Unknown)
	places := map[string]any{}
	for _, p := range r.cfg.Places {
		e := map[string]any{"xy": []any{p.X(), p.Y()}, "title": map[string]any{"ru": p.RU, "ru_loc": p.RULoc, "en": p.ID}}
		if p.Charger {
			e["charger"] = true
		}
		places[p.ID] = e
	}
	prop := func(m map[string]any, v any) map[string]any { m["rw"] = false; m["value"] = v; return m }
	dev := map[string]any{
		"id": "robot", "type": "mobile_robot", "location": st["place"], "title": map[string]any{"ru": "робот", "en": "robot"},
		"properties": map[string]any{
			"place":       prop(map[string]any{"type": "string", "enum": placeEnum}, st["place"]),
			"nav":         prop(map[string]any{"type": "string", "enum": toAny(NavStates)}, st["nav"]),
			"battery_pct": prop(map[string]any{"type": "integer", "unit": "%", "min": 0, "max": 100, "resolution": 5}, st["battery_pct"]),
			"alert":       prop(map[string]any{"type": "string", "enum": toAny(Alerts)}, st["alert"]),
			"done":        prop(map[string]any{"type": "object", "description": "result of the last task started by the LLM"}, st["done"]),
			"seen":        prop(map[string]any{"type": "object", "description": "last detector finding on patrol"}, st["seen"]),
		},
		"actions": map[string]any{
			"go_to":  map[string]any{"params": map[string]any{"place": map[string]any{"type": "string", "enum": toAny(ids)}}},
			"patrol": map[string]any{"params": map[string]any{"places": map[string]any{"type": "array"}, "look": map[string]any{"type": "boolean"}}},
			"look":   map[string]any{"params": map[string]any{"place": map[string]any{"type": "string"}, "image": map[string]any{"type": "boolean"}}},
			"stop":   map[string]any{"params": map[string]any{}},
		},
	}
	return map[string]any{
		"devices": []any{dev},
		"places":  places,
		"robot": map[string]any{"model": r.cfg.Model, "world": r.cfg.World, "max_v_mps": r.cfg.MaxVMps,
			"goal_tol_m": r.cfg.GoalTolM, "detector": r.o.Detector.Name()},
		"costs": map[string]any{"go_to_s": map[string]any{"a": r.cfg.Travel.A, "b_per_m": r.cfg.Travel.B},
			"look_s": 0.2, "look_image_s": 1.0, "look_image_kb": 5, "status_s": 0.1},
	}
}

func toAny(ss []string) []any {
	out := make([]any, len(ss))
	for i, s := range ss {
		out[i] = s
	}
	return out
}

// Diag — счётчики ядра (не для LLM).
func (r *Robot) Diag() map[string]any {
	r.mu.Lock()
	defer r.mu.Unlock()
	c := map[string]any{}
	for k, v := range r.cnt {
		c[k] = v
	}
	out := map[string]any{"counters": c, "task_active": r.task != nil, "motion_scan": r.motionSub != ""}
	if r.task != nil {
		out["task"] = map[string]any{"seq": r.task.seq, "kind": r.task.kind, "cur": r.task.cur, "queue": r.task.queue}
	}
	return out
}

// Active — есть ли активная задача (для /control/state: реплеер T1 шагает мир, пока робот активен).
func (r *Robot) Active() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.task != nil
}
