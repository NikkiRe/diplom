package server_test

import (
	"context"
	"encoding/json"
	"math"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/robotmcp/configs"
	"github.com/local/robotmcp/internal/clock"
	"github.com/local/robotmcp/internal/fakebridge"
	"github.com/local/robotmcp/internal/imgutil"
	"github.com/local/robotmcp/internal/robot"
	"github.com/local/robotmcp/internal/rosbridge"
	"github.com/local/robotmcp/internal/server"
)

// harness — фейковый rosbridge + ядро + MCP-сервер + клиент SDK (in-memory) с журналом уведомлений.
type harness struct {
	t     *testing.T
	fb    *fakebridge.Server
	br    *rosbridge.Client
	r     *robot.Robot
	srv   *server.Server
	cs    *mcp.ClientSession
	ctx   context.Context
	vc    *clock.Virtual
	stub  *robot.Stub
	mu    sync.Mutex
	notes []map[string]any
	ch    chan map[string]any
}

type hopt struct {
	fb       fakebridge.Options
	ro       robot.Options
	virtual  bool
	stub     bool
	wait     bool
	cancelTO time.Duration
}

const cancelSrv = "/navigate_to_pose/_action/cancel_goal"

func newHarness(t *testing.T, o hopt) *harness {
	t.Helper()
	h := &harness{t: t, ch: make(chan map[string]any, 64)}
	h.fb = fakebridge.Start(o.fb)
	ctx, cancel := context.WithCancel(context.Background())
	h.ctx = ctx
	cfg, err := robot.ParseConfig(configs.TurtleBot3World)
	if err != nil {
		t.Fatal(err)
	}
	h.br = rosbridge.New(rosbridge.Options{URL: h.fb.URL, ReconnectMin: 20 * time.Millisecond,
		Actions: []string{cfg.Topics.Nav}, CancelTimeout: o.cancelTO})
	brDone := make(chan struct{})
	go func() { h.br.Run(ctx); close(brDone) }()
	ro := o.ro
	if o.virtual {
		h.vc = clock.NewVirtual(1000)
		ro.Clock = h.vc
	} else {
		ro.Clock = clock.NewReal()
	}
	ro.Bridge = h.br
	if ro.LowBatteryPct == 0 {
		ro.LowBatteryPct = 20
	}
	if ro.BatteryStart == 0 {
		ro.BatteryStart = 100
	}
	if ro.ScanMaxAge == 0 {
		ro.ScanMaxAge = 30
	}
	if ro.ImageWidth == 0 {
		ro.ImageWidth = 320
	}
	if o.stub {
		h.stub = robot.NewStub()
		ro.Detector = h.stub
	}
	var srv *server.Server
	ro.OnEvent = func(e robot.Event) { srv.Notify(e) }
	h.r = robot.New(cfg, ro)
	srv = server.New(h.r, h.br, server.Options{WaitParam: o.wait})
	h.srv = srv
	wctx, wc := context.WithTimeout(ctx, 5*time.Second)
	defer wc()
	if err := h.br.WaitConnected(wctx); err != nil {
		t.Fatal(err)
	}
	h.r.Start(ctx)
	st, ct := mcp.NewInMemoryTransports()
	ss, err := srv.MCP().Connect(ctx, st, nil)
	if err != nil {
		t.Fatal(err)
	}
	client := mcp.NewClient(&mcp.Implementation{Name: "test"}, &mcp.ClientOptions{
		ResourceUpdatedHandler: func(_ context.Context, req *mcp.ResourceUpdatedNotificationRequest) {
			m := map[string]any(req.Params.Meta)
			b, _ := json.Marshal(m) // нормализовать типы как у клиента по сети
			var mm map[string]any
			_ = json.Unmarshal(b, &mm)
			h.mu.Lock()
			h.notes = append(h.notes, mm)
			h.mu.Unlock()
			h.ch <- mm
		}})
	h.cs, err = client.Connect(ctx, ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := h.cs.Subscribe(ctx, &mcp.SubscribeParams{URI: server.URIState}); err != nil {
		t.Fatal(err)
	}
	h.waitPose()
	t.Cleanup(func() {
		h.cs.Close()
		ss.Close()
		srv.Close()
		cancel()
		<-brDone
		h.fb.Close()
	})
	return h
}

func (h *harness) waitPose() {
	h.t.Helper()
	waitFor(h.t, func() bool { return h.r.State()["place"] != "unknown" })
}

// call вызывает инструмент; возвращает разобранный JSON текста и результат целиком.
func (h *harness) call(name string, args map[string]any, meta mcp.Meta) (map[string]any, *mcp.CallToolResult) {
	h.t.Helper()
	res, err := h.cs.CallTool(h.ctx, &mcp.CallToolParams{Name: name, Arguments: args, Meta: meta})
	if err != nil {
		h.t.Fatal(err)
	}
	if res.StructuredContent != nil {
		h.t.Fatal("structuredContent must be absent")
	}
	text := res.Content[0].(*mcp.TextContent).Text
	var m map[string]any
	if err := json.Unmarshal([]byte(text), &m); err != nil {
		h.t.Fatalf("%s: %v: %s", name, err, text)
	}
	return m, res
}

// next ждёт уведомление (с событием kind, если не пусто).
func (h *harness) next(kind string, timeout time.Duration) map[string]any {
	h.t.Helper()
	deadline := time.After(timeout)
	for {
		select {
		case m := <-h.ch:
			if kind == "" || m["robot/event"] == kind {
				return m
			}
		case <-deadline:
			h.t.Fatalf("no %q notification", kind)
		}
	}
}

func (h *harness) noNote(d time.Duration) {
	h.t.Helper()
	select {
	case m := <-h.ch:
		h.t.Fatalf("unexpected notification %v", m)
	case <-time.After(d):
	}
}

// statusUntil вызывает status, пока ответ не удовлетворит cond.
func (h *harness) statusUntil(cond func(map[string]any) bool) (st map[string]any) {
	h.t.Helper()
	waitFor(h.t, func() bool { st, _ = h.call("status", nil, nil); return cond(st) })
	return st
}

// counter — счётчик ядра (robot://diag).
func (h *harness) counter(k string) any { return h.r.Diag()["counters"].(map[string]any)[k] }

// other — второй клиент того же rosbridge (другой процесс).
func (h *harness) other() *rosbridge.Client {
	h.t.Helper()
	c := rosbridge.New(rosbridge.Options{URL: h.fb.URL})
	ctx, cancel := context.WithCancel(h.ctx)
	done := make(chan struct{})
	go func() { c.Run(ctx); close(done) }()
	h.t.Cleanup(func() { cancel(); <-done })
	waitFor(h.t, c.Connected)
	return c
}

func state(n map[string]any) map[string]any { return n["state"].(map[string]any) }

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatal("condition not met")
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// ---------------------------------------------------------------------------------------------

func TestCatalog(t *testing.T) {
	h := newHarness(t, hopt{})
	lt, err := h.cs.ListTools(h.ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	names := []string{}
	for _, tl := range lt.Tools {
		names = append(names, tl.Name)
	}
	if strings.Join(names, ",") != "go_to,look,patrol,status,stop" {
		t.Fatal(names)
	}
	b, _ := h.srv.ToolsJSON()
	if !strings.Contains(string(b), `"enum":["dock","door","corridor"`) || strings.Contains(string(b), `"wait"`) {
		t.Fatal(string(b))
	}
	if n := h.srv.CatalogTokens(); n < 150 || n > 320 {
		t.Fatalf("catalog tokens %d", n)
	}
	rl, err := h.cs.ListResources(h.ctx, nil)
	if err != nil || len(rl.Resources) != 3 {
		t.Fatal(rl, err)
	}
	inv, err := h.cs.ReadResource(h.ctx, &mcp.ReadResourceParams{URI: server.URIInventory})
	if err != nil || !strings.Contains(inv.Contents[0].Text, `"mobile_robot"`) {
		t.Fatal(err)
	}
}

func TestStatusIdle(t *testing.T) {
	h := newHarness(t, hopt{})
	m, _ := h.call("status", nil, nil)
	if m["place"] != "dock" || m["nav"] != "idle" || m["alert"] != "none" || m["battery_pct"] != 100.0 {
		t.Fatal(m)
	}
	free := m["free_m"].(map[string]any)
	for _, k := range []string{"front", "left", "right", "back"} {
		if free[k] == nil {
			t.Fatal(m)
		}
	}
	// второй вызов в простое — скан из кэша (без нового subscribe)
	n0 := h.fb.Count("subscribe", "/scan")
	h.call("status", nil, nil)
	if h.fb.Count("subscribe", "/scan") != n0 {
		t.Fatal("idle status must reuse fresh cached scan")
	}
}

func TestGoToAsync(t *testing.T) {
	h := newHarness(t, hopt{ro: robot.Options{MotionScanMs: 200}})
	m, res := h.call("go_to", map[string]any{"place": "door"}, nil)
	if m["ok"] != true || m["accepted"] != "door" || m["eta_s"].(float64) < 11 || res.IsError {
		t.Fatal(m)
	}
	// во время движения статус показывает цель
	st, _ := h.call("status", nil, nil)
	if st["nav"] != "moving" || st["place"] != "between" || st["goal"].(map[string]any)["place"] != "door" {
		t.Fatal(st)
	}
	n := h.next("task_end", 10*time.Second)
	s := state(n)
	if s["nav"] != "succeeded" || s["place"] != "door" {
		t.Fatal(n)
	}
	done := s["done"].(map[string]any)
	if done["seq"] != 1.0 || done["task"] != "go_to" || done["status"] != "succeeded" || done["place"] != "door" {
		t.Fatal(done)
	}
	if ch := n["robot/changed"].([]any); len(ch) < 3 {
		t.Fatal("changed", ch)
	}
	x, y, _, _ := h.fb.Pose()
	if math.Hypot(x-1.8, y-0.5) > 0.05 {
		t.Fatal("not at door", x, y)
	}
	for _, rm := range h.fb.Received() {
		if rm["op"] == "send_action_goal" && rm["feedback"] != false {
			t.Fatal("feedback requested")
		}
	}
	// подписка /scan жила только во время движения
	time.Sleep(50 * time.Millisecond)
	if h.fb.Count("subscribe", "/scan") < 1 || h.fb.Count("unsubscribe", "/scan") < 1 {
		t.Fatal("motion scan not (un)subscribed")
	}
	// уже на месте: без движения и без уведомления
	m, _ = h.call("go_to", map[string]any{"place": "door"}, nil)
	if m["status"] != "here" {
		t.Fatal(m)
	}
	h.noNote(100 * time.Millisecond)
}

func TestRuleTaskDoesNotTouchDone(t *testing.T) {
	h := newHarness(t, hopt{})
	h.call("go_to", map[string]any{"place": "door"}, mcp.Meta{server.MetaBy: "rule"})
	n := h.next("task_end", 10*time.Second)
	if state(n)["done"] != nil || state(n)["nav"] != "succeeded" {
		t.Fatal(n)
	}
	for _, c := range n["robot/changed"].([]any) {
		if c == "done" {
			t.Fatal("done changed by rule task")
		}
	}
}

func TestUnknownPlaceShortError(t *testing.T) {
	h := newHarness(t, hopt{})
	n0 := len(h.fb.Received())
	for _, args := range []map[string]any{{"place": "kitchen"}, {"places": []any{"door", "kitchen"}}} {
		tool := "go_to"
		if args["places"] != nil {
			tool = "patrol"
		}
		m, res := h.call(tool, args, nil)
		if !res.IsError || m["error"] != "unknown place" || len(m["known"].([]any)) != 8 {
			t.Fatal(m)
		}
	}
	m, res := h.call("go_to", map[string]any{"place": "door", "speed": 2}, nil)
	if !res.IsError || !strings.Contains(m["error"].(string), "speed") {
		t.Fatal(m)
	}
	if len(h.fb.Received()) != n0 {
		t.Fatal("invalid call reached rosbridge")
	}
}

func TestPatrol(t *testing.T) {
	h := newHarness(t, hopt{})
	m, _ := h.call("patrol", map[string]any{"places": []any{"dock", "corridor", "dock"}}, nil)
	if m["accepted"] != 2.0 { // dock — уже там
		t.Fatal(m)
	}
	n := h.next("task_end", 15*time.Second)
	d := state(n)["done"].(map[string]any)
	if d["task"] != "patrol" || d["visited"] != 3.0 || d["status"] != "succeeded" || state(n)["place"] != "dock" {
		t.Fatal(n)
	}
	if got := h.fb.Count("send_action_goal", ""); got != 2 {
		t.Fatal("goals", got)
	}
}

// stop: отмена сервисом Nav2, после его ответа — нулевая скорость.
func TestStop(t *testing.T) {
	h := newHarness(t, hopt{fb: fakebridge.Options{Speed: 5}})
	h.call("go_to", map[string]any{"place": "window"}, nil)
	time.Sleep(100 * time.Millisecond)
	n0 := len(h.fb.Received())
	t0 := time.Now()
	m, _ := h.call("stop", nil, nil)
	if m["ok"] != true || m["cancelled"] != "window" || time.Since(t0) > 200*time.Millisecond {
		t.Fatal(m)
	}
	n := h.next("task_end", 2*time.Second)
	if state(n)["nav"] != "canceled" || state(n)["done"] != nil {
		t.Fatal(n)
	}
	waitFor(t, func() bool { return h.fb.ZeroTwist.Load() == 1 })
	var order []string
	for _, rm := range h.fb.Received()[n0:] {
		switch {
		case rm["service"] == cancelSrv:
			order = append(order, "cancel")
		case rm["op"] == "publish" && rm["topic"] == "/cmd_vel":
			order = append(order, "twist")
		}
	}
	if strings.Join(order, ",") != "cancel,twist" || h.fb.Moving() {
		t.Fatal("stop order", order)
	}
}

// stop без своей задачи отменяет и цель, о которой адаптер не знает (чужую, оставшуюся после обрыва или от
// прошлого процесса).
func TestStopCancelsForeignGoal(t *testing.T) {
	h := newHarness(t, hopt{fb: fakebridge.Options{Speed: 2}})
	other := h.other()
	goal := map[string]any{"pose": map[string]any{"header": map[string]any{"frame_id": "map"},
		"pose": map[string]any{"position": map[string]any{"x": 1.8, "y": 0.5}, "orientation": map[string]any{"w": 1.0}}}}
	if _, err := other.SendGoal(h.ctx, other.NewID("go"), "/navigate_to_pose", "nav2_msgs/action/NavigateToPose", goal, nil); err != nil {
		t.Fatal(err)
	}
	waitFor(t, h.fb.Moving)
	m, _ := h.call("stop", nil, nil)
	if m["ok"] != true || m["cancelled"] != nil || h.fb.Moving() {
		t.Fatal(m)
	}
}

// Цель задачи LLM отменил не адаптер (другой клиент, поздно выполненная отмена): итог — в done со статусом
// canceled, иначе LLM о нём не узнала бы (гейт EdgeCtx не будит по nav).
func TestExternalCancel(t *testing.T) {
	h := newHarness(t, hopt{fb: fakebridge.Options{Speed: 2}})
	h.call("go_to", map[string]any{"place": "window"}, nil)
	waitFor(t, h.fb.Moving)
	if err := h.other().CancelAllGoals(h.ctx, "/navigate_to_pose", "test"); err != nil {
		t.Fatal(err)
	}
	n := h.next("task_end", 2*time.Second)
	d, _ := state(n)["done"].(map[string]any)
	if state(n)["nav"] != "canceled" || d["status"] != "canceled" || d["place"] != "window" {
		t.Fatal(n)
	}
}

// Nav2 не подтвердил отмену: stop не дольше -cancel-timeout сообщает «stop not confirmed». Без ответа
// соединение с rosbridge разрывается (нулевая скорость не уходит); ошибка rosbridge или отказ Nav2 — ответ:
// нулевая скорость отправлена. Отмена повторяется при следующем подключении — робот останавливается.
func TestStopUnconfirmed(t *testing.T) {
	for _, tc := range []struct {
		name  string
		reply fakebridge.Reply
	}{{"mute", fakebridge.Reply{Mute: true}}, {"fail", fakebridge.Reply{Fail: true}}, {"reject", fakebridge.Reply{Reject: true}}} {
		t.Run(tc.name, func(t *testing.T) {
			h := newHarness(t, hopt{fb: fakebridge.Options{Speed: 2}, cancelTO: 150 * time.Millisecond})
			h.call("go_to", map[string]any{"place": "window"}, nil)
			h.fb.SetReply(tc.reply)
			mute := tc.reply.Mute
			t0 := time.Now()
			m, res := h.call("stop", nil, nil)
			if d := time.Since(t0); !res.IsError || m["error"] != "stop not confirmed" || m["cancelled"] != "window" ||
				d > time.Second || mute && d < 150*time.Millisecond {
				t.Fatal(m, d)
			}
			if mute {
				waitFor(t, func() bool { return !h.br.Connected() })
				if h.fb.ZeroTwist.Load() != 0 {
					t.Fatal("zero twist sent over a dropped connection")
				}
			} else {
				waitFor(t, func() bool { return h.fb.ZeroTwist.Load() == 1 })
			}
			h.fb.SetReply(fakebridge.Reply{})
			if !mute {
				h.fb.DropConnections()
			}
			waitFor(t, func() bool { return h.br.Connected() && !h.fb.Moving() })
		})
	}
}

func TestBlockedAlert(t *testing.T) {
	h := newHarness(t, hopt{fb: fakebridge.Options{BlockAbortS: 2}})
	h.fb.AddObstacle(fakebridge.Obstacle{X: 1.8, Y: 0.5, R: 0.2, Label: "box"})
	h.call("go_to", map[string]any{"place": "door"}, nil)
	a := h.next("alert", 10*time.Second)
	if state(a)["alert"] != "blocked" {
		t.Fatal(a)
	}
	n := h.next("task_end", 2*time.Second)
	if state(n)["nav"] != "aborted" || state(n)["done"].(map[string]any)["status"] != "aborted" {
		t.Fatal(n)
	}
	// новая задача снимает тревогу без отдельного уведомления
	h.fb.ClearObstacles()
	h.call("go_to", map[string]any{"place": "dock"}, nil)
	n = h.next("", 10*time.Second)
	if n["robot/event"] != "task_end" || state(n)["alert"] != "none" {
		t.Fatal(n)
	}
}

func TestObstacleAheadAlert(t *testing.T) {
	h := newHarness(t, hopt{fb: fakebridge.Options{Speed: 10, BlockAbortS: 60},
		ro: robot.Options{MotionScanMs: 50, ObstacleM: 0.7, ObstacleHoldS: 0.5}})
	h.fb.AddObstacle(fakebridge.Obstacle{X: 1.8, Y: 0.5, R: 0.2, Label: "box"})
	h.call("go_to", map[string]any{"place": "door"}, nil)
	a := h.next("alert", 15*time.Second)
	if state(a)["alert"] != "blocked" || a["robot/detail"].(map[string]any)["why"] != "obstacle" {
		t.Fatal(a)
	}
	h.call("stop", nil, nil)
}

func TestLowBattery(t *testing.T) {
	h := newHarness(t, hopt{})
	h.r.SetBattery(52)
	h.noNote(50 * time.Millisecond) // шаг заряда — не значимое событие
	h.r.SetBattery(19)
	n := h.next("alert", time.Second)
	if state(n)["alert"] != "low_battery" || state(n)["battery_pct"] != 20.0 {
		t.Fatal(n)
	}
	h.r.SetBattery(15)
	h.noNote(50 * time.Millisecond)
}

func TestBatteryTopic(t *testing.T) {
	h := newHarness(t, hopt{ro: robot.Options{BatteryTopic: true}})
	h.fb.SetBattery(0.18)
	n := h.next("alert", 10*time.Second)
	if state(n)["alert"] != "low_battery" {
		t.Fatal(n)
	}
}

func TestLookImage(t *testing.T) {
	h := newHarness(t, hopt{})
	m, res := h.call("look", nil, nil)
	if m["detector"] != "none" || m["place"] != "dock" || m["in_view"] == nil || len(res.Content) != 1 {
		t.Fatal(m)
	}
	m, res = h.call("look", map[string]any{"image": true}, nil)
	if len(res.Content) != 2 {
		t.Fatal(m)
	}
	img := res.Content[1].(*mcp.ImageContent)
	if w, hh := imgutil.JPEGSize(img.Data); w != 320 || hh != 240 || img.MIMEType != "image/jpeg" {
		t.Fatal(w, hh)
	}
	fr := m["frame"].(map[string]any)
	if fr["w"] != 320.0 || fr["kb"].(float64) <= 0 {
		t.Fatal(m)
	}
	// кадр шёл в CBOR (байты, без base64)
	ok := false
	for _, rm := range h.fb.Received() {
		if rm["op"] == "subscribe" && rm["topic"] == "/camera/image_raw/compressed" && rm["compression"] == "cbor" {
			ok = true
		}
	}
	if !ok || h.fb.Count("unsubscribe", "/camera/image_raw/compressed") != 1 {
		t.Fatal("camera subscription")
	}
}

func TestLookAtPlaceAsync(t *testing.T) {
	h := newHarness(t, hopt{stub: true})
	h.stub.Set("corridor", []robot.Object{{Label: "person", M: 1.2, Side: "left"}})
	m, _ := h.call("look", map[string]any{"place": "corridor"}, nil)
	if m["accepted"] != "corridor" || m["then"] != "look" {
		t.Fatal(m)
	}
	n := h.next("task_end", 15*time.Second)
	d := state(n)["done"].(map[string]any)
	view := d["view"].(map[string]any)
	if d["task"] != "look" || view["objects"].([]any)[0].(map[string]any)["label"] != "person" {
		t.Fatal(d)
	}
	m, _ = h.call("look", map[string]any{"place": "door", "image": true}, nil)
	if m["ok"] != false {
		t.Fatal(m)
	}
}

func TestPatrolPersonAlert(t *testing.T) {
	h := newHarness(t, hopt{stub: true})
	h.stub.Set("corridor", []robot.Object{{Label: "person", M: 1.0}})
	h.call("patrol", map[string]any{"places": []any{"corridor", "dock"}}, mcp.Meta{server.MetaBy: "rule"})
	a := h.next("alert", 15*time.Second)
	if state(a)["alert"] != "person" || state(a)["seen"].(map[string]any)["place"] != "corridor" {
		t.Fatal(a)
	}
	n := h.next("task_end", 15*time.Second)
	if state(n)["done"] != nil { // обход правилом: done не меняется, тревога уже разбудила
		t.Fatal(n)
	}
}

func TestStuckAndTimeoutVirtualClock(t *testing.T) {
	h := newHarness(t, hopt{virtual: true, fb: fakebridge.Options{Paused: true},
		ro: robot.Options{StuckS: 30, GoalTimeout: 100}})
	h.call("go_to", map[string]any{"place": "door"}, nil)
	h.vc.Set(1020)
	h.r.Tick()
	h.noNote(50 * time.Millisecond)
	h.vc.Set(1031)
	h.r.Tick()
	a := h.next("alert", time.Second)
	if state(a)["alert"] != "stuck" {
		t.Fatal(a)
	}
	h.vc.Set(1101)
	// Отмена синхронна: к возврату Tick ответ Nav2 уже получен (вторая — после отмены при подключении).
	h.r.Tick()
	if h.fb.Count("call_service", cancelSrv) != 2 || h.fb.Moving() {
		t.Fatal("goal not cancelled on timeout")
	}
	n := h.next("task_end", time.Second)
	if state(n)["nav"] != "aborted" {
		t.Fatal(n)
	}
}

func TestSimStepOnPause(t *testing.T) {
	h := newHarness(t, hopt{fb: fakebridge.Options{Paused: true},
		ro: robot.Options{SimStepIters: 100, SimStepWait: 20 * time.Millisecond, SensorTimeout: 2 * time.Second}})
	m, _ := h.call("status", nil, nil)
	if m["free_m"] == nil {
		t.Fatal(m)
	}
	m, res := h.call("look", map[string]any{"image": true}, nil)
	if len(res.Content) != 2 {
		t.Fatal(m)
	}
	if h.fb.Count("call_service", "/world/default/control") < 1 {
		t.Fatal("no world steps")
	}
}

func TestPausedWithoutSimStep(t *testing.T) {
	h := newHarness(t, hopt{fb: fakebridge.Options{Paused: true}, ro: robot.Options{SensorTimeout: 100 * time.Millisecond,
		FrameTimeout: 100 * time.Millisecond}})
	m, _ := h.call("status", nil, nil)
	if m["lidar"] != "no data" {
		t.Fatal(m)
	}
	m, _ = h.call("look", map[string]any{"image": true}, nil)
	if m["frame"] != "timeout" {
		t.Fatal(m)
	}
}

func TestPayloadRaw(t *testing.T) {
	h := newHarness(t, hopt{ro: robot.Options{Payload: "raw"}})
	m, _ := h.call("status", nil, nil)
	scan := m["scan"].(map[string]any)
	if len(scan["ranges"].([]any)) != 360 || m["amcl_pose"] == nil {
		t.Fatal(m)
	}
}

func TestReconnect(t *testing.T) {
	h := newHarness(t, hopt{})
	h.fb.DropConnections()
	deadline := time.Now().Add(5 * time.Second)
	for h.br.Stats().Conns < 2 || !h.br.Connected() {
		if time.Now().After(deadline) {
			t.Fatal("no reconnect")
		}
		time.Sleep(10 * time.Millisecond)
	}
	h.call("go_to", map[string]any{"place": "pass_sw"}, nil)
	n := h.next("task_end", 10*time.Second)
	if state(n)["nav"] != "succeeded" {
		t.Fatal(n)
	}
}

// Обрыв rosbridge во время цели: агент сразу получает alert blocked (rosbridge disconnected) и task_end; пока
// связи нет, робот едет (rosbridge 2.7.1 цель не отменяет), сводка помечает место place_age_s и не выдаёт
// скан пути за текущий; при восстановлении связи цель отменяется первым же сообщением, доезд без задачи
// помечен displaced, а новая цель после этого не снимается.
func TestDisconnectDuringGoal(t *testing.T) {
	h := newHarness(t, hopt{fb: fakebridge.Options{Speed: 5}, ro: robot.Options{MotionScanMs: 50}})
	h.call("go_to", map[string]any{"place": "window"}, nil)
	time.Sleep(100 * time.Millisecond)
	n0 := len(h.fb.Received())
	h.fb.SetLink(false)
	a := h.next("alert", 5*time.Second)
	if state(a)["alert"] != "blocked" || a["robot/detail"].(map[string]any)["why"] != "rosbridge disconnected" {
		t.Fatal(a)
	}
	if n := h.next("task_end", time.Second); state(n)["nav"] != "aborted" {
		t.Fatal(n)
	}
	st := h.statusUntil(func(st map[string]any) bool { return st["place_age_s"] != nil })
	if st["lidar"] != "no data" || !h.fb.Moving() {
		t.Fatal(st)
	}
	time.Sleep(settle) // связь вернётся позже окна: поза после переподключения — смена места без задачи
	h.fb.SetLink(true)
	waitFor(t, func() bool { return h.br.Connected() && !h.fb.Moving() })
	if m := h.fb.Received()[n0]; m["service"] != cancelSrv {
		t.Fatal("first message after reconnect is not cancel", m)
	}
	if st = h.statusUntil(func(st map[string]any) bool { return st["place_age_s"] == nil }); st["displaced"] != true {
		t.Fatal(st)
	}
	h.call("go_to", map[string]any{"place": "pass_sw"}, nil)
	if n := h.next("task_end", 10*time.Second); state(n)["nav"] != "succeeded" {
		t.Fatal(n)
	}
}

// Без связи с rosbridge место помечено place_age_s (секунд без связи); скан того же места — из кэша, пока
// не старше -scan-max-age, затем с lidar_age_s (обновить нельзя). Со связью отметки исчезают.
func TestStaleWhenOffline(t *testing.T) {
	h := newHarness(t, hopt{virtual: true})
	st, _ := h.call("status", nil, nil) // скан в кэш
	if st["place_age_s"] != nil || st["lidar_age_s"] != nil || st["free_m"] == nil {
		t.Fatal(st)
	}
	h.fb.SetLink(false)
	h.statusUntil(func(st map[string]any) bool { return st["place_age_s"] != nil })
	h.vc.Set(1012)
	st, _ = h.call("status", nil, nil)
	if st["place"] != "dock" || st["place_age_s"] != 12.0 || st["free_m"] == nil || st["lidar_age_s"] != nil {
		t.Fatal(st)
	}
	h.vc.Set(1045)
	st, _ = h.call("status", nil, nil)
	if st["place_age_s"] != 45.0 || st["lidar_age_s"] != 45.0 || st["free_m"] == nil {
		t.Fatal(st)
	}
	h.fb.SetLink(true)
	st = h.statusUntil(func(st map[string]any) bool { return st["place_age_s"] == nil })
	if st["lidar_age_s"] != nil {
		t.Fatal(st)
	}
}

// settle — окно после остановки при -pose-throttle-ms 0 (1 с по стенным часам) с запасом.
const settle = 1100 * time.Millisecond

// Робота сдвинули извне в простое (скачок позы AMCL): место по цели сбрасывается, сводка помечает
// displaced, скан, снятый до сдвига, не используется; новая задача снимает отметку. Поза в окне после
// остановки — задержанная поза поездки, даже если виртуальные часы ушли вперёд (реплеер T1); поворот без
// переноса — тоже сдвиг.
func TestDisplacedExternally(t *testing.T) {
	h := newHarness(t, hopt{virtual: true})
	h.call("go_to", map[string]any{"place": "door"}, nil)
	h.next("task_end", 10*time.Second)
	h.vc.Set(1030)
	amcl := h.counter("amcl_rx")
	h.fb.Teleport(1.5, 0.5, 0)
	waitFor(t, func() bool { return h.counter("amcl_rx") != amcl })
	st, _ := h.call("status", nil, nil)
	if st["place"] != "door" || st["place_m"] != 0.3 || st["displaced"] != nil {
		t.Fatal(st)
	}
	time.Sleep(settle)
	h.vc.Set(1031)
	scans := h.fb.Count("subscribe", "/scan")
	h.fb.Teleport(1.5, 0.5, 0.5)
	waitFor(t, func() bool { return h.counter("displaced") == int64(1) })
	st, _ = h.call("status", nil, nil)
	if st["displaced"] != true || st["place"] != "door" || h.fb.Count("subscribe", "/scan") != scans+1 {
		t.Fatal(st)
	}
	h.call("go_to", map[string]any{"place": "door"}, nil)
	h.next("task_end", 10*time.Second)
	if st, _ = h.call("status", nil, nil); st["displaced"] != nil || st["place"] != "door" {
		t.Fatal(st)
	}
	time.Sleep(settle)
	h.fb.Teleport(0, 1.8, 0)
	waitFor(t, func() bool { return h.counter("displaced") == int64(2) })
	if st, _ = h.call("status", nil, nil); st["displaced"] != true || st["place"] != "corridor" || st["place_m"] != 0.0 {
		t.Fatal(st)
	}
}

func TestWaitParam(t *testing.T) {
	h := newHarness(t, hopt{wait: true})
	m, _ := h.call("go_to", map[string]any{"place": "door", "wait": true}, nil)
	if m["status"] != "succeeded" || m["ok"] != true || m["place"] != "door" {
		t.Fatal(m)
	}
	n := h.next("task_end", time.Second)
	if state(n)["done"] != nil { // LLM уже получила итог — done не меняется
		t.Fatal(n)
	}
}

func TestPreemption(t *testing.T) {
	h := newHarness(t, hopt{fb: fakebridge.Options{Speed: 5}})
	h.call("go_to", map[string]any{"place": "window"}, nil)
	time.Sleep(30 * time.Millisecond)
	m, _ := h.call("go_to", map[string]any{"place": "window"}, nil)
	if m["status"] != "already moving" {
		t.Fatal(m)
	}
	h.call("go_to", map[string]any{"place": "pass_sw"}, nil)
	n := h.next("task_end", 10*time.Second) // только итог второй задачи
	d := state(n)["done"].(map[string]any)
	if d["place"] != "pass_sw" || d["seq"] != 1.0 {
		t.Fatal(n)
	}
}

func TestStuckClearsOnProgress(t *testing.T) {
	h := newHarness(t, hopt{virtual: true, fb: fakebridge.Options{Paused: true}, ro: robot.Options{StuckS: 30}})
	h.call("go_to", map[string]any{"place": "door"}, nil)
	h.vc.Set(1031)
	h.r.Tick()
	if a := h.next("alert", time.Second); state(a)["alert"] != "stuck" {
		t.Fatal(a)
	}
	h.fb.SetPaused(false) // движение возобновилось
	n := h.next("task_end", 10*time.Second)
	if state(n)["alert"] != "none" || state(n)["nav"] != "succeeded" {
		t.Fatal(n)
	}
}

func TestNoLookPlaceCatalog(t *testing.T) {
	h := newHarness(t, hopt{})
	full := h.srv.CatalogTokens()
	cfgSrv := server.New(h.r, h.br, server.Options{NoLookPlace: true})
	defer cfgSrv.Close()
	b, _ := cfgSrv.ToolsJSON()
	if strings.Count(string(b), `"enum"`) != 2 || cfgSrv.CatalogTokens() >= full-20 {
		t.Fatal(cfgSrv.CatalogTokens(), full, string(b))
	}
}
