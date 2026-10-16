package server

import (
	"bytes"
	"context"
	"encoding/json"
	"image/jpeg"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/cammcp/internal/camera"
	"github.com/local/cammcp/internal/clock"
	"github.com/local/cammcp/internal/detect"
	"github.com/local/cammcp/internal/scene"
	"github.com/local/cammcp/internal/source"
	"github.com/local/cammcp/internal/trace"
)

type fixture struct {
	t   *testing.T
	vc  *clock.Virtual
	cam *camera.Camera
	srv *Server
	log *syncBuf
}

type syncBuf struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuf) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuf) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	f := &fixture{t: t, vc: clock.NewVirtual(0), log: &syncBuf{}}
	tr := trace.New(f.log, f.vc, "t")
	var err error
	f.cam, err = camera.New(camera.Options{Device: "cam1", Source: source.Synthetic{Scene: scene.New(scene.Demo())},
		Detector: detect.DefaultParams(), IdleFPS: 2, ActiveFPS: 8, Clock: f.vc, Trace: tr,
		OnEvent: func(e camera.Event) { f.srv.Notify(e) }})
	if err != nil {
		t.Fatal(err)
	}
	f.srv, err = New(f.cam, Options{Location: "hall", SnapshotWidth: 320, SnapshotQuality: 60, SetDetector: true,
		Trace: tr, ProtocolVersions: []string{"2025-11-25"}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(f.srv.Close)
	return f
}

type notes struct {
	mu    sync.Mutex
	metas []mcp.Meta
	uris  []string
}

func (n *notes) handler(_ context.Context, req *mcp.ResourceUpdatedNotificationRequest) {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.metas = append(n.metas, req.Params.Meta)
	n.uris = append(n.uris, req.Params.URI)
}

func (n *notes) count() int {
	n.mu.Lock()
	defer n.mu.Unlock()
	return len(n.metas)
}

func (f *fixture) connect(n *notes) *mcp.ClientSession {
	f.t.Helper()
	ctx := context.Background()
	ct, st := mcp.NewInMemoryTransports()
	if _, err := f.srv.MCP().Connect(ctx, st, nil); err != nil {
		f.t.Fatal(err)
	}
	opts := &mcp.ClientOptions{}
	if n != nil {
		opts.ResourceUpdatedHandler = n.handler
	}
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "1"}, opts).Connect(ctx, ct, nil)
	if err != nil {
		f.t.Fatal(err)
	}
	f.t.Cleanup(func() { _ = cs.Close() })
	return cs
}

func (f *fixture) advance(to float64) {
	f.t.Helper()
	if err := f.cam.AdvanceTo(f.vc, to); err != nil {
		f.t.Fatal(err)
	}
}

// call вызывает инструмент: JSON первого (текстового) содержимого, признак isError и весь результат.
func call(t *testing.T, cs *mcp.ClientSession, name string, args any) (map[string]any, bool, *mcp.CallToolResult) {
	t.Helper()
	res, err := cs.CallTool(context.Background(), &mcp.CallToolParams{Name: name, Arguments: args})
	if err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	if res.StructuredContent != nil {
		t.Fatalf("%s: structuredContent doubles the traffic", name)
	}
	text := res.Content[0].(*mcp.TextContent).Text
	var body map[string]any
	if json.Unmarshal([]byte(text), &body) != nil && !res.IsError {
		t.Fatalf("%s: result is not JSON: %s", name, text)
	}
	return body, res.IsError, res
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timeout waiting for %s", what)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func TestTools(t *testing.T) {
	f := newFixture(t)
	cs := f.connect(nil)
	res, err := cs.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, tl := range res.Tools {
		names = append(names, tl.Name)
	}
	if strings.Join(names, ",") != "camera_status,motion_state,set_detector,snapshot" {
		t.Fatal(names)
	}
	plain, err := New(f.cam, Options{SnapshotWidth: 320, SnapshotQuality: 60})
	if err != nil {
		t.Fatal(err)
	}
	defer plain.Close()
	if n := len(plain.Tools()); n != 3 {
		t.Fatalf("set_detector must be opt-in: %d tools", n)
	}
	b, err := f.srv.ToolsJSON()
	if err != nil || !strings.Contains(string(b), `"maximum":320`) || !strings.Contains(string(b), `"default":60`) {
		t.Fatalf("%v %s", err, b)
	}
	f.advance(21)
	st, isErr, _ := call(t, cs, "camera_status", nil)
	if isErr || st["device"] != "cam1" || st["detector"].(map[string]any)["state"] != "motion" {
		t.Fatalf("%v", st)
	}
	m, isErr, _ := call(t, cs, "motion_state", map[string]any{})
	if isErr || m["motion"] != true || m["frame"].(map[string]any)["zone"] == nil {
		t.Fatalf("%v", m)
	}
	if _, isErr, _ := call(t, cs, "motion_state", map[string]any{"extra": 1}); !isErr {
		t.Fatal("unknown argument accepted")
	}
}

func TestSnapshot(t *testing.T) {
	f := newFixture(t)
	cs := f.connect(nil)
	if body, isErr, _ := call(t, cs, "snapshot", nil); !isErr || body["error"] != "snapshot_failed" || body["detail"] != "no frame yet" {
		t.Fatalf("%v", body)
	}
	f.advance(21)
	meta, isErr, res := call(t, cs, "snapshot", nil)
	if isErr || len(res.Content) != 2 || meta["w"] != 320.0 || meta["quality"] != 60.0 || meta["motion"] != true {
		t.Fatalf("%v", meta)
	}
	img := res.Content[1].(*mcp.ImageContent)
	if img.MIMEType != "image/jpeg" || float64(len(img.Data)) != meta["bytes"] {
		t.Fatalf("%s %d %v", img.MIMEType, len(img.Data), meta["bytes"])
	}
	if cfg, err := jpeg.DecodeConfig(bytes.NewReader(img.Data)); err != nil || cfg.Width != 320 || cfg.Height != 240 {
		t.Fatal(err, cfg)
	}
	small, _, res2 := call(t, cs, "snapshot", map[string]any{"max_width": 96, "quality": 30})
	if small["w"] != 96.0 || len(res2.Content[1].(*mcp.ImageContent).Data) >= len(img.Data) {
		t.Fatalf("%v", small)
	}
	for _, bad := range []map[string]any{{"max_width": 640}, {"quality": 95}, {"max_width": 10}, {"quality": "high"}} {
		if _, isErr, _ := call(t, cs, "snapshot", bad); !isErr {
			t.Fatalf("%v accepted", bad)
		}
	}
	log := f.log.String()
	if !strings.Contains(log, `"image_bytes":`) || strings.Count(log, `"type":"tool_call"`) != 7 {
		t.Fatalf("tool calls are not traced:\n%s", log)
	}
}

func TestSetDetector(t *testing.T) {
	f := newFixture(t)
	cs := f.connect(nil)
	d, isErr, _ := call(t, cs, "set_detector", map[string]any{"threshold": 30, "k": 2, "idle_fps": 1})
	if isErr || d["threshold"] != 30.0 || d["k"] != 2.0 || d["n"] != 5.0 {
		t.Fatalf("%v", d)
	}
	if _, idle, active := f.cam.Params(); idle != 1 || active != 8 {
		t.Fatalf("fps %v %v", idle, active)
	}
	for _, bad := range []map[string]any{{"k": 6}, {"threshold": 200}, {"active_fps": 30}, {"idle_fps": 9, "active_fps": 4},
		{"min_area_pct": 0}, {"bg_tau_s": 1}} {
		if body, isErr, _ := call(t, cs, "set_detector", bad); !isErr {
			t.Fatalf("%v accepted: %v", bad, body)
		}
	}
	if p, _, _ := f.cam.Params(); p.Threshold != 30 || p.K != 2 {
		t.Fatalf("rejected calls changed params: %+v", p)
	}
}

// Подписка: уведомления только на начало и конец эпизода, хотя кадров обработано много; новое
// состояние — в _meta (агенту не нужно читать ресурс).
func TestSubscriptionNotifiesEpisodesOnly(t *testing.T) {
	f := newFixture(t)
	n := &notes{}
	cs := f.connect(n)
	ctx := context.Background()
	if err := cs.Subscribe(ctx, &mcp.SubscribeParams{URI: "cam://state/cam1"}); err != nil {
		t.Fatal(err)
	}
	if err := cs.Subscribe(ctx, &mcp.SubscribeParams{URI: "cam://state/other"}); err == nil {
		t.Fatal("unknown resource subscribed")
	}
	f.advance(40)
	waitFor(t, "two notifications", func() bool { return n.count() == 2 })
	time.Sleep(50 * time.Millisecond)
	frames := f.cam.Status()["frames"].(int64)
	n.mu.Lock()
	defer n.mu.Unlock()
	if len(n.metas) != 2 || frames < 100 {
		t.Fatalf("%d notifications for %d frames", len(n.metas), frames)
	}
	start, end := n.metas[0], n.metas[1]
	if n.uris[0] != "cam://state/cam1" || start["device"] != "cam1" || start["cam/event"] != camera.MotionStart ||
		end["cam/event"] != camera.MotionEnd {
		t.Fatalf("%v %v", start, end)
	}
	if st := start["state"].(map[string]any); st["motion"] != true || st["zone"] == nil {
		t.Fatalf("%v", st)
	}
	if st := end["state"].(map[string]any); st["motion"] != false || st["area_pct"].(float64) < 1 {
		t.Fatalf("%v", st)
	}
	if ep := end["cam/episode"].(map[string]any); ep["duration_s"].(float64) < 9 {
		t.Fatalf("%v", ep)
	}
	if s := f.srv.NotifyStats(); s["sent"] != int64(2) {
		t.Fatalf("%v", s)
	}
}

// Отказ камеры доходит до клиента как motion = null в _meta.state; у уведомления без эпизода нет
// "cam/episode" (лишние байты в каждом уведомлении).
func TestCameraLostNotified(t *testing.T) {
	f := newFixture(t)
	n := &notes{}
	cs := f.connect(n)
	if err := cs.Subscribe(context.Background(), &mcp.SubscribeParams{URI: "cam://state/cam1"}); err != nil {
		t.Fatal(err)
	}
	f.srv.Notify(camera.Event{Kind: camera.CameraLost, T: 1, Changed: []string{"motion"},
		State: map[string]any{"motion": nil, "area_pct": 0.0, "zone": nil}})
	waitFor(t, "notification", func() bool { return n.count() == 1 })
	n.mu.Lock()
	defer n.mu.Unlock()
	meta := n.metas[0]
	motion, ok := meta["state"].(map[string]any)["motion"]
	_, episode := meta["cam/episode"]
	if meta["cam/event"] != camera.CameraLost || !ok || motion != nil || episode {
		t.Fatalf("%v", meta)
	}
}

// Ресурсы в формате hw-mcp: агент edgectx разбирает их тем же кодом (ParseInventory, ParseState).
func TestResources(t *testing.T) {
	f := newFixture(t)
	cs := f.connect(nil)
	ctx := context.Background()
	lr, err := cs.ListResources(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(lr.Resources) != 2 {
		t.Fatalf("%v", lr.Resources)
	}
	read := func(uri string) map[string]any {
		res, err := cs.ReadResource(ctx, &mcp.ReadResourceParams{URI: uri})
		if err != nil {
			t.Fatal(err)
		}
		var v map[string]any
		if err := json.Unmarshal([]byte(res.Contents[0].Text), &v); err != nil {
			t.Fatal(err)
		}
		return v
	}
	inv := read(URIInventory)
	dev := inv["devices"].([]any)[0].(map[string]any)
	props := dev["properties"].(map[string]any)
	if dev["id"] != "cam1" || dev["location"] != "hall" || props["motion"].(map[string]any)["type"] != "boolean" ||
		props["area_pct"].(map[string]any)["unit"] != "%" || len(props["zone"].(map[string]any)["enum"].([]any)) != 9 {
		t.Fatalf("%v", inv)
	}
	f.advance(21)
	st := read("cam://state/cam1")
	p := st["properties"].(map[string]any)
	if st["device"] != "cam1" || p["motion"].(map[string]any)["value"] != true || p["area_pct"].(map[string]any)["age_s"] == nil {
		t.Fatalf("%v", st)
	}
}

func TestNewRejectsBadSnapshotLimits(t *testing.T) {
	f := newFixture(t)
	for _, o := range []Options{{SnapshotWidth: 32, SnapshotQuality: 60}, {SnapshotWidth: 1281, SnapshotQuality: 60},
		{SnapshotWidth: 320, SnapshotQuality: 19}, {SnapshotWidth: 320, SnapshotQuality: 96}} {
		if _, err := New(f.cam, o); err == nil {
			t.Fatalf("snapshot limits %d, q%d accepted", o.SnapshotWidth, o.SnapshotQuality)
		}
	}
}

// Клиент не читает: рассылка висит, а очередь ограничена — лишние события сворачиваются в последнее.
func TestDispatcherBounded(t *testing.T) {
	block, called := make(chan struct{}), make(chan struct{}, 1)
	d := newDispatcher(func(camera.Event) {
		select {
		case called <- struct{}{}:
		default:
		}
		<-block
	})
	d.enqueue(camera.Event{Kind: camera.MotionStart})
	<-called
	for i := 0; i < 100; i++ {
		kind := camera.MotionStart
		if i%2 == 1 {
			kind = camera.MotionEnd
		}
		d.enqueue(camera.Event{Kind: kind, T: float64(i), Changed: []string{"motion"}})
	}
	s := d.stats()
	if s["queued"] != maxQueued || s["coalesced"] != int64(100-maxQueued) || s["enqueued"] != int64(101) {
		t.Fatalf("%v", s)
	}
	d.mu.Lock()
	last := d.queue[len(d.queue)-1]
	d.mu.Unlock()
	if last.T != 99 || last.Kind != camera.MotionEnd || len(last.Changed) != 1 {
		t.Fatalf("%+v", last)
	}
	d.close()
	d.close()
	close(block)
	d.enqueue(camera.Event{})
	if d.stats()["enqueued"] != int64(101) {
		t.Fatal("enqueue after close")
	}
}
