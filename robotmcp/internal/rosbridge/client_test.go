package rosbridge_test

import (
	"context"
	"errors"
	"math"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/local/robotmcp/internal/fakebridge"
	"github.com/local/robotmcp/internal/rosbridge"
)

const (
	nav       = "/navigate_to_pose"
	navType   = "nav2_msgs/action/NavigateToPose"
	cancelSrv = nav + "/_action/cancel_goal"
)

func start(t *testing.T, fo fakebridge.Options, o rosbridge.Options) (*fakebridge.Server, *rosbridge.Client, context.Context) {
	t.Helper()
	fb := fakebridge.Start(fo)
	ctx, cancel := context.WithCancel(context.Background())
	o.URL, o.ReconnectMin = fb.URL, 20*time.Millisecond
	c := rosbridge.New(o)
	done := make(chan struct{})
	go func() { c.Run(ctx); close(done) }()
	t.Cleanup(func() { cancel(); <-done; fb.Close() })
	wctx, wc := context.WithTimeout(ctx, 5*time.Second)
	defer wc()
	if err := c.WaitConnected(wctx); err != nil {
		t.Fatal(err)
	}
	return fb, c, ctx
}

func TestOnceJSONAndCBOR(t *testing.T) {
	_, c, ctx := start(t, fakebridge.Options{}, rosbridge.Options{})
	for _, comp := range []string{"none", "cbor"} {
		m, err := c.Once(ctx, rosbridge.Sub{Topic: "/scan", Type: "sensor_msgs/msg/LaserScan", Compression: comp}, 2*time.Second, nil)
		if err != nil {
			t.Fatal(comp, err)
		}
		r, err := rosbridge.Floats(m["ranges"])
		if err != nil || len(r) != 360 {
			t.Fatal(comp, err, len(r))
		}
		inf := 0
		for _, v := range r {
			if math.IsInf(v, 1) {
				inf++
			}
		}
		if inf == 360 {
			t.Fatal(comp, "all inf")
		}
	}
	st := c.Stats()
	if st.Rx["publish /scan"][0] < 2 || st.TCPRx < st.BytesRx || st.Conns != 1 {
		t.Fatalf("stats %+v", st)
	}
}

func TestLatchedAndReconnect(t *testing.T) {
	fb, c, ctx := start(t, fakebridge.Options{}, rosbridge.Options{})
	var n atomic.Int64
	c.Subscribe(ctx, rosbridge.Sub{Topic: "/amcl_pose", Type: "geometry_msgs/msg/PoseWithCovarianceStamped"},
		func(m map[string]any) { n.Add(1) })
	c.Advertise(ctx, "/cmd_vel", "geometry_msgs/msg/TwistStamped")
	waitFor(t, func() bool { return n.Load() >= 1 })
	fb.DropConnections()
	waitFor(t, func() bool { return c.Stats().Conns >= 2 && n.Load() >= 2 }) // подписка восстановлена → латченная поза снова
	waitFor(t, func() bool { return fb.Count("advertise", "/cmd_vel") >= 2 })
}

func goalTo(x, y float64) map[string]any {
	return map[string]any{"pose": map[string]any{"header": map[string]any{"frame_id": "map"},
		"pose": map[string]any{"position": map[string]any{"x": x, "y": y, "z": 0.0},
			"orientation": map[string]any{"w": 1.0}}}}
}

func TestGoalAndPreemption(t *testing.T) {
	fb, c, ctx := start(t, fakebridge.Options{Speed: 40}, rosbridge.Options{})
	goal := goalTo(-1, -0.5)
	ch, err := c.SendGoal(ctx, c.NewID("go"), nav, navType, goal, nil)
	if err != nil {
		t.Fatal(err)
	}
	select {
	case m := <-ch:
		if m["status"] != 4.0 {
			t.Fatal(m)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("no result")
	}
	if x, _, _, _ := fb.Pose(); math.Abs(x+1) > 0.05 {
		t.Fatal("robot not at goal", x)
	}
	for _, m := range fb.Received() {
		if m["op"] == "send_action_goal" && m["feedback"] != false {
			t.Fatal("feedback requested", m)
		}
	}
	ch, _ = c.SendGoal(ctx, c.NewID("go"), nav, navType, goal, nil)
	if _, err := c.SendGoal(ctx, c.NewID("go"), nav, navType, goalTo(1.8, -0.5), nil); err != nil {
		t.Fatal(err)
	}
	if m := <-ch; m["status"] != 5.0 { // вытеснена
		t.Fatal(m)
	}
	resp, err := c.CallService(ctx, "/world/default/control", map[string]any{"world_control": map[string]any{"pause": true}}, time.Second)
	if err != nil || resp["result"] != true {
		t.Fatal(resp, err)
	}
}

// cancels — журнал OnCancel.
type cancels struct {
	mu   sync.Mutex
	list []rosbridge.Cancel
}

func (c *cancels) add(x rosbridge.Cancel) { c.mu.Lock(); c.list = append(c.list, x); c.mu.Unlock() }

func (c *cancels) get() []rosbridge.Cancel {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]rosbridge.Cancel(nil), c.list...)
}

// Отмена всех целей: сервис action_msgs/srv/CancelGoal с нулевыми uuid и stamp; цель этого соединения
// получает итог canceled; без соединения — ErrDisconnected.
func TestCancelAllGoals(t *testing.T) {
	var log cancels
	fb, c, ctx := start(t, fakebridge.Options{Speed: 2}, rosbridge.Options{OnCancel: log.add})
	ch, err := c.SendGoal(ctx, c.NewID("go"), nav, navType, goalTo(1.8, 0.5), nil)
	if err != nil {
		t.Fatal(err)
	}
	waitFor(t, fb.Moving)
	if err := c.CancelAllGoals(ctx, nav, "stop"); err != nil {
		t.Fatal(err)
	}
	if m := <-ch; m["status"] != 5.0 || fb.Moving() {
		t.Fatal("goal not cancelled", m)
	}
	var req map[string]any
	for _, m := range fb.Received() {
		if m["service"] == cancelSrv {
			req = m
		}
	}
	info := req["args"].(map[string]any)["goal_info"].(map[string]any)
	uuid := info["goal_id"].(map[string]any)["uuid"].([]any)
	stamp := info["stamp"].(map[string]any)
	if len(uuid) != 16 || uuid[0] != 0.0 || uuid[15] != 0.0 || stamp["sec"] != 0.0 || stamp["nanosec"] != 0.0 {
		t.Fatal("not a cancel-all request", req)
	}
	if l := log.get(); len(l) != 1 || l[0].Why != "stop" || l[0].Goals != 1 || l[0].Err != nil {
		t.Fatal(l)
	}
	off := rosbridge.New(rosbridge.Options{URL: "ws://127.0.0.1:1"})
	if err := off.CancelAllGoals(ctx, nav, "stop"); !errors.Is(err, rosbridge.ErrDisconnected) {
		t.Fatal(err)
	}
}

// Цель, отправленная во время отмены всех целей, уходит после её ответа и под неё не попадает (фейк, как
// rosbridge, выполняет отмену в отдельном потоке: выполненная позже, она сняла бы и цель). valid проверяется
// после ожидания: цель, ставшая за это время ненужной, не уходит.
func TestGoalWaitsForCancel(t *testing.T) {
	var cancels atomic.Int64
	fb, c, ctx := start(t, fakebridge.Options{Speed: 2},
		rosbridge.Options{OnCancel: func(rosbridge.Cancel) { cancels.Add(1) }})
	fb.SetReply(fakebridge.Reply{Delay: 200 * time.Millisecond})
	inFlight := func(n int) {
		go func() { _ = c.CancelAllGoals(ctx, nav, "stop") }()
		waitFor(t, func() bool { return fb.Count("call_service", cancelSrv) == n })
	}
	inFlight(1)
	t0 := time.Now()
	if _, err := c.SendGoal(ctx, c.NewID("go"), nav, navType, goalTo(1.8, 0.5), nil); err != nil {
		t.Fatal(err)
	}
	if d := time.Since(t0); d < 150*time.Millisecond {
		t.Fatal("goal sent while the cancel was in flight", d)
	}
	waitFor(t, fb.Moving)
	inFlight(2)
	_, err := c.SendGoal(ctx, c.NewID("go"), nav, navType, goalTo(-1, -0.5), func() bool { return cancels.Load() == 1 })
	if err == nil || fb.Count("send_action_goal", nav) != 1 {
		t.Fatal("withdrawn goal sent", err)
	}
	waitFor(t, func() bool { return !fb.Moving() })
}

// Цели своего действия не остаются без хозяина: при первом подключении и после обрыва с неоконченной целью
// отмена «всех» — первое сообщение соединения (раньше подписок и новых целей); пока связи нет, робот едет
// (rosbridge 2.7.1 цель не отменяет). Обрыв без цели (в том числе после завершённой) — без отмены. Обработчик
// связи узнаёт об обрыве раньше ожидающего цель.
func TestOrphanGoalsCancelledOnConnect(t *testing.T) {
	var log cancels
	var lmu sync.Mutex
	var links []bool
	fb, c, ctx := start(t, fakebridge.Options{Speed: 2}, rosbridge.Options{Actions: []string{nav}, OnCancel: log.add})
	c.OnLink(func(up bool) { lmu.Lock(); links = append(links, up); lmu.Unlock() })
	c.Subscribe(ctx, rosbridge.Sub{Topic: "/amcl_pose", Type: "geometry_msgs/msg/PoseWithCovarianceStamped"}, func(map[string]any) {})
	if m := fb.Received()[0]; m["op"] != "call_service" || m["service"] != cancelSrv {
		t.Fatal("first message is not cancel", m)
	}
	if l := log.get(); len(l) != 1 || l[0].Why != "connect" || l[0].Err != nil || l[0].Goals != 0 {
		t.Fatal(l)
	}
	ch, err := c.SendGoal(ctx, c.NewID("go"), nav, navType, goalTo(1.8, 0.5), nil)
	if err != nil {
		t.Fatal(err)
	}
	waitFor(t, fb.Moving)
	n0 := len(fb.Received())
	fb.SetLink(false)
	if m := <-ch; m["op"] != "disconnected" {
		t.Fatal(m)
	}
	lmu.Lock()
	down := len(links) > 0 && !links[len(links)-1]
	lmu.Unlock()
	if !down {
		t.Fatal("link hook not called before pending goal", links)
	}
	time.Sleep(100 * time.Millisecond)
	if !fb.Moving() {
		t.Fatal("goal must survive the disconnect (rosbridge 2.7.1)")
	}
	fb.SetLink(true)
	waitFor(t, func() bool { return c.Connected() && !fb.Moving() })
	if m := fb.Received()[n0]; m["op"] != "call_service" || m["service"] != cancelSrv {
		t.Fatal("first message after reconnect is not cancel", m)
	}
	if l := log.get(); len(l) != 2 || l[1].Why != "connect" || l[1].Goals != 1 {
		t.Fatal(l)
	}
	x, y, _, _ := fb.Pose()
	if ch, err = c.SendGoal(ctx, c.NewID("go"), nav, navType, goalTo(x, y), nil); err != nil {
		t.Fatal(err)
	}
	if m := <-ch; m["status"] != 4.0 {
		t.Fatal(m)
	}
	fb.DropConnections()
	waitFor(t, func() bool { return c.Stats().Conns >= 3 && c.Connected() })
	if n := fb.Count("call_service", cancelSrv); n != 2 {
		t.Fatal("cancel without orphan goal", n)
	}
}

// Неподтверждённая отмена своего действия повторяется первым сообщением следующего соединения, даже если
// итог цели уже не ждут (Forget, как robot.Stop). Отмена без ответа закрывает соединение, и оно не
// открывается, пока отмена при подключении не подтверждена: выполненная позже, она сняла бы новую цель.
// Ошибка rosbridge (result: false) и отказ (return_code 1) — ответ: соединение остаётся.
func TestCancelUnconfirmed(t *testing.T) {
	for _, tc := range []struct {
		name  string
		reply fakebridge.Reply
	}{{"mute", fakebridge.Reply{Mute: true}}, {"fail", fakebridge.Reply{Fail: true}}, {"reject", fakebridge.Reply{Reject: true}}} {
		t.Run(tc.name, func(t *testing.T) {
			var log cancels
			var opened atomic.Int64
			fb, c, ctx := start(t, fakebridge.Options{Speed: 2}, rosbridge.Options{Actions: []string{nav},
				CancelTimeout: 150 * time.Millisecond, OnCancel: log.add, OnConnect: func(int) { opened.Add(1) }})
			id := c.NewID("go")
			if _, err := c.SendGoal(ctx, id, nav, navType, goalTo(1.8, 0.5), nil); err != nil {
				t.Fatal(err)
			}
			waitFor(t, fb.Moving)
			c.Forget(id)
			fb.SetReply(tc.reply)
			mute := tc.reply.Mute
			if err := c.CancelAllGoals(ctx, nav, "stop"); err == nil || errors.Is(err, context.DeadlineExceeded) != mute {
				t.Fatal(err)
			}
			if mute {
				waitFor(t, func() bool { return len(log.get()) >= 3 }) // отмены при подключении — тоже без ответа
				if opened.Load() != 1 {
					t.Fatal("connection opened with an unanswered cancel")
				}
			} else if !c.Connected() {
				t.Fatal("connection closed after an answered cancel")
			}
			n0 := len(fb.Received())
			fb.SetReply(fakebridge.Reply{})
			if !mute {
				fb.DropConnections()
			}
			waitFor(t, func() bool { return c.Connected() && !fb.Moving() })
			if m := fb.Received()[n0]; m["service"] != cancelSrv {
				t.Fatal("first message after reconnect is not cancel", m)
			}
			if l := log.get(); l[len(l)-1].Why != "connect" || l[len(l)-1].Err != nil || l[len(l)-1].Goals != 1 {
				t.Fatal(l)
			}
		})
	}
}

func TestDisconnectedErrors(t *testing.T) {
	c := rosbridge.New(rosbridge.Options{URL: "ws://127.0.0.1:1"})
	ctx := context.Background()
	if _, err := c.Once(ctx, rosbridge.Sub{Topic: "/scan"}, 10*time.Millisecond, nil); err != rosbridge.ErrDisconnected {
		t.Fatal(err)
	}
	if _, err := c.SendGoal(ctx, "x", "/a", "t", nil, nil); err != rosbridge.ErrDisconnected {
		t.Fatal(err)
	}
	if _, err := c.CallService(ctx, "/s", nil, time.Second); err != rosbridge.ErrDisconnected {
		t.Fatal(err)
	}
}

func TestFloatsBytes(t *testing.T) {
	f, err := rosbridge.Floats([]any{1.0, nil})
	if err != nil || f[0] != 1 || !math.IsInf(f[1], 1) {
		t.Fatal(f, err)
	}
	if _, err := rosbridge.Floats("x"); err == nil {
		t.Fatal("string accepted")
	}
	b, err := rosbridge.Bytes("AAE=")
	if err != nil || len(b) != 2 {
		t.Fatal(b, err)
	}
}

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
