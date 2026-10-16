package camera

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"image"
	"image/jpeg"
	"math"
	"math/rand/v2"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/local/cammcp/internal/clock"
	"github.com/local/cammcp/internal/detect"
	"github.com/local/cammcp/internal/scene"
	"github.com/local/cammcp/internal/source"
	"github.com/local/cammcp/internal/trace"
)

type rig struct {
	cam    *Camera
	vc     *clock.Virtual
	sc     *scene.Scene
	log    bytes.Buffer
	mu     sync.Mutex
	events []Event
}

func newRig(t *testing.T, sc *scene.Scenario, src source.Source) *rig {
	t.Helper()
	r := &rig{vc: clock.NewVirtual(0), sc: scene.New(sc)}
	if src == nil {
		src = source.Synthetic{Scene: r.sc, Name: "test"}
	}
	var err error
	r.cam, err = New(Options{Device: "cam1", Source: src, Detector: detect.DefaultParams(), IdleFPS: 2, ActiveFPS: 8,
		Clock: r.vc, Trace: trace.New(&r.log, r.vc, "t"), OnEvent: func(e Event) {
			r.mu.Lock()
			r.events = append(r.events, e)
			r.mu.Unlock()
		}})
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func (r *rig) advance(t *testing.T, to float64) {
	t.Helper()
	if err := r.cam.AdvanceTo(r.vc, to); err != nil {
		t.Fatal(err)
	}
}

type interval struct{ a, b float64 }

// truth — интервалы, когда в кадре виден объект площадью не меньше minArea (по сцене, шаг 0,05 с).
func truth(s *scene.Scene, minArea, until float64) []interval {
	var out []interval
	in := false
	for t := 0.0; t <= until; t += 0.05 {
		vis := false
		for _, v := range s.Truth(t) {
			vis = vis || v.Area >= minArea
		}
		switch {
		case vis && !in:
			out = append(out, interval{a: t})
		case !vis && in:
			out[len(out)-1].b = t
		}
		in = vis
	}
	return out
}

// episodes — эпизоды из событий: [начало серии, последний активный кадр], подтверждение.
func (r *rig) episodes(t *testing.T) (eps []interval, confirm []float64) {
	t.Helper()
	r.mu.Lock()
	defer r.mu.Unlock()
	for i, e := range r.events {
		want := MotionStart
		if i%2 == 1 {
			want = MotionEnd
		}
		if e.Kind != want {
			t.Fatalf("event %d is %s, want %s", i, e.Kind, want)
		}
		if e.Kind == MotionEnd {
			eps = append(eps, interval{e.Episode["start"].(float64), e.Episode["end"].(float64)})
			confirm = append(confirm, e.Episode["confirm"].(float64))
		}
	}
	return eps, confirm
}

func overlap(x, y interval, tol float64) bool { return x.a <= y.b+tol && y.a <= x.b+tol }

// score сопоставляет эпизоды с истиной сцены (объект площадью не меньше MinArea в кадре): найденные
// объекты (полнота), эпизоды, соответствующие объекту (точность), наибольшая задержка подтверждения.
func score(t *testing.T, r *rig, end float64) (gt, eps []interval, found, correct int, worst float64) {
	t.Helper()
	gt = truth(r.sc, detect.DefaultParams().MinArea, end)
	eps, confirm := r.episodes(t)
	const tol = 1.0 // конец эпизода ждёт N тихих кадров, а начало серии бывает раньше порога площади истины
	for _, g := range gt {
		for i, e := range eps {
			if overlap(g, e, tol) {
				found++
				worst = max(worst, confirm[i]-g.a)
				t.Logf("object %5.1f–%5.1f s: episode %5.1f–%5.1f s, latency %.2f s", g.a, g.b, e.a, e.b, confirm[i]-g.a)
				break
			}
		}
	}
	for _, e := range eps {
		for _, g := range gt {
			if overlap(g, e, tol) {
				correct++
				break
			}
		}
	}
	t.Logf("recall %d/%d, precision %d/%d, worst latency %.2f s", found, len(gt), correct, len(eps), worst)
	return gt, eps, found, correct, worst
}

// Главная проверка детектора на демонстрационной сцене (дрейф света, лампа, шум, дрожание, 4 объекта):
// каждый объект найден (полнота), каждый эпизод соответствует объекту (точность, в том числе нет
// тревоги на включение и выключение лампы), задержка обнаружения ограничена.
func TestDemoDetection(t *testing.T) {
	r := newRig(t, scene.Demo(), nil)
	end := scene.Demo().Duration() + 5
	r.advance(t, end)
	gt, eps, found, correct, worst := score(t, r, end)
	if len(gt) != 4 || found != len(gt) || correct != len(eps) || worst > 1.5 {
		t.Fatalf("truth %v, episodes %v", gt, eps)
	}
	st := r.cam.Status()
	if st["episodes"] != int64(len(eps)) || st["errors"] != int64(0) || st["skipped"] != int64(0) {
		t.Fatalf("%v", st)
	}
	if !strings.Contains(r.log.String(), `"type":"episode_end"`) {
		t.Fatal("episodes are not traced")
	}
}

// Сцена смоука в ВМ (configs/scene.smoke.json): лампа гаснет, пока в кадре идёт человек.
func TestSmokeScene(t *testing.T) {
	sc, err := scene.Load("../../configs/scene.smoke.json")
	if err != nil {
		t.Fatal(err)
	}
	r := newRig(t, sc, nil)
	r.advance(t, 40)
	gt, eps, found, correct, worst := score(t, r, 40)
	if len(gt) != 2 || found != 2 || correct != len(eps) || worst > 1.5 {
		t.Fatalf("truth %v, episodes %v", gt, eps)
	}
}

// Только помехи 10 минут: сильный шум, дрожание 1,5 px, быстрый дрейф света и лампа, щёлкающая каждые
// 20 с, — ни одного эпизода.
func TestNoFalseAlarms(t *testing.T) {
	sc := scene.Demo()
	sc.Objects, sc.Noise, sc.JitterPx = nil, 8, 1.5
	sc.Light = scene.Light{Base: 0.5, Amp: 0.25, PeriodS: 60}
	sc.Lamps = nil
	for on := 10.0; on < 600; on += 40 {
		sc.Lamps = append(sc.Lamps, scene.Lamp{OnS: on, OffS: on + 20, Gain: 0.5, Gradient: 0.2})
	}
	r := newRig(t, sc, nil)
	r.advance(t, 600)
	if eps, _ := r.episodes(t); len(eps) != 0 {
		t.Fatalf("false alarms: %v", eps)
	}
	if st := r.cam.Status(); st["frames"].(int64) < 1150 {
		t.Fatalf("quiet scene must run at the idle rate: %v", st)
	}
}

// Светлая поверхность (альбедо 0,95, около 12 % кадра) пересвечивается лампой ×2: ни включение, ни
// выключение лампы — после короткой и после долгой работы, в том числе при старте под лампой, — не дают
// эпизодов, а человек перед пересвеченной поверхностью найден.
func TestLampOnBrightSurface(t *testing.T) {
	for _, firstOn := range []float64{0, 10} {
		sc := scene.Demo()
		sc.Width, sc.Height = 160, 120
		sc.Light = scene.Light{Base: 0.75}
		sc.Lamps = []scene.Lamp{{OnS: firstOn, OffS: 25, Gain: 0.75, Gradient: 0.1}, {OnS: 50, OffS: 130, Gain: 0.75, Gradient: 0.1}}
		sc.Objects = []scene.Object{
			{ID: "white_wall", W: 0.3, H: 0.5, Albedo: 0.95, Path: [][3]float64{{0, 0.8, 0.3}, {200, 0.8, 0.3}}},
			{ID: "person", W: 0.12, H: 0.5, Albedo: 0.2, Path: [][3]float64{{135, 0.5, 0.35}, {145, 1.08, 0.3}}},
		}
		r := newRig(t, sc, nil)
		r.advance(t, 160)
		if eps, _ := r.episodes(t); len(eps) != 1 || eps[0].a < 135 || eps[0].b > 145 {
			t.Fatalf("lamp first on at %v s: episodes %v", firstOn, eps)
		}
	}
}

// Частота адаптивная: в покое 2 кадра/с, при движении — 8.
func TestAdaptiveFPS(t *testing.T) {
	r := newRig(t, scene.Demo(), nil)
	frames := func() int64 { return r.cam.Status()["frames"].(int64) }
	r.advance(t, 10)
	quiet := frames()
	r.advance(t, 20)
	r.advance(t, 26)
	busy := frames()
	r.advance(t, 26.5)
	if s := r.cam.Status(); s["detector"].(map[string]any)["state"] != "motion" || s["fps"].(map[string]any)["target"] != 8.0 {
		t.Fatalf("%v", s)
	}
	r.advance(t, 32)
	r.advance(t, 62)
	idle := frames()
	r.advance(t, 72)
	if quiet != 21 { // t = 0, 0.5, …, 10
		t.Fatalf("idle frames in 10 s: %d", quiet)
	}
	if n := frames() - idle; n != 20 {
		t.Fatalf("idle frames 62–72 s: %d", n)
	}
	if busy-quiet < 6*8 {
		t.Fatalf("frames 10–26 s with a walking person: %d", busy-quiet)
	}
}

func TestStateAndMotion(t *testing.T) {
	r := newRig(t, scene.Demo(), nil)
	st := r.cam.State()
	props := st["properties"].(map[string]any)
	if props["motion"].(map[string]any)["value"] != false || props["zone"].(map[string]any)["value"] != nil {
		t.Fatalf("%v", st)
	}
	r.advance(t, 21)
	st = r.cam.State()
	props = st["properties"].(map[string]any)
	if props["motion"].(map[string]any)["value"] != true || props["area_pct"].(map[string]any)["unit"] != "%" {
		t.Fatalf("%v", st)
	}
	if st["device"] != "cam1" || props["area_pct"].(map[string]any)["value"].(float64) < 1 {
		t.Fatalf("%v", st)
	}
	m := r.cam.Motion()
	ep := m["episode"].(map[string]any)
	if m["motion"] != true || ep["id"] != 1 || ep["started_s_ago"].(float64) <= 0 || ep["ended_s_ago"] != nil {
		t.Fatalf("%v", m)
	}
	if fr := m["frame"].(map[string]any); fr["area_pct"].(float64) < 1 || fr["zone"] == nil {
		t.Fatalf("motion in the last frame: %v", m)
	}
	r.advance(t, 23) // сводка эпизода живая: кадров и времени стало больше, рамка шире
	m2 := r.cam.Motion()
	ep2 := m2["episode"].(map[string]any)
	if ep2["frames"].(int) <= ep["frames"].(int) || ep2["duration_s"].(float64) <= ep["duration_s"].(float64) ||
		ep2["box"].([]float64)[2] <= ep["box"].([]float64)[2] {
		t.Fatalf("episode summary is not live: %v then %v", ep, ep2)
	}
	r.mu.Lock()
	e := r.events[0]
	r.mu.Unlock()
	if e.Kind != MotionStart || e.State["motion"] != true || e.Changed[0] != "motion" || len(e.Changed) != 3 {
		t.Fatalf("%+v", e)
	}
	r.advance(t, 40)
	m = r.cam.Motion()
	if m["motion"] != false || m["episodes"] != int64(1) || m["episode"].(map[string]any)["ended_s_ago"] == nil {
		t.Fatalf("%v", m)
	}
	if zoneOf([2]float64{0.9, 0.1}) != "top-right" || zoneOf([2]float64{0.5, 0.5}) != "center" || len(Zones()) != 9 {
		t.Fatal("zones")
	}
}

func TestSnapshot(t *testing.T) {
	r := newRig(t, scene.Demo(), nil)
	if _, _, err := r.cam.Snapshot(320, 60); err == nil {
		t.Fatal("snapshot before the first frame")
	}
	r.advance(t, 21)
	b, meta, err := r.cam.Snapshot(160, 50)
	if err != nil {
		t.Fatal(err)
	}
	img, err := jpeg.Decode(bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	if bb := img.Bounds(); bb.Dx() != 160 || bb.Dy() != 120 || meta["bytes"] != len(b) || meta["motion"] != true {
		t.Fatalf("%v %v", bb, meta)
	}
	if _, ok := img.(*image.Gray); !ok {
		t.Fatalf("snapshot must be grayscale, got %T", img)
	}
	box := meta["box"].([]float64) // рамка движения в этом кадре: человек около x = 0,5 в момент 21 с
	if box[0] > 0.5 || box[2] < 0.5 || box[2]-box[0] > 0.3 {
		t.Fatalf("box %v is not around the person", box)
	}
	g := img.(*image.Gray)
	x0, y0 := int(box[0]*160), int(box[1]*120)
	if v := g.Pix[y0*g.Stride+x0+3]; v < 200 { // белая внешняя линия рамки (после JPEG — светлая)
		t.Fatalf("box is not drawn: pixel %d", v)
	}
	big, meta2, _ := r.cam.Snapshot(10000, 60)
	if meta2["w"] != 320 || len(big) <= len(b) {
		t.Fatalf("width must be capped by the source: %v", meta2)
	}
	if st := r.cam.Status(); st["snapshots"] != int64(2) || st["snapshot_bytes"] != int64(len(b)+len(big)) {
		t.Fatalf("%v", st)
	}
}

// Каталог кадров кончается во время движения: эпизод завершается, камера останавливается.
func TestDirSourceEnds(t *testing.T) {
	dir := t.TempDir()
	s := scene.New(scene.Demo())
	g := image.NewGray(image.Rect(0, 0, 320, 240))
	for i := 0; i < 50; i++ { // 5 кадров/с, 10–20 с: человек входит в кадр на 15 с
		s.Render(10+float64(i)*0.2, g)
		var buf bytes.Buffer
		if err := jpeg.Encode(&buf, g, &jpeg.Options{Quality: 90}); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, fmt.Sprintf("f%03d.jpg", i)), buf.Bytes(), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	src, err := source.OpenDir(dir, 5)
	if err != nil {
		t.Fatal(err)
	}
	r := newRig(t, scene.Demo(), src)
	if _, _, active := r.cam.Params(); active != 5 {
		t.Fatalf("active fps must be capped by the directory rate: %v", active)
	}
	r.advance(t, 30)
	eps, _ := r.episodes(t)
	if len(eps) != 1 || !math.IsInf(r.cam.NextDue(), 1) {
		t.Fatalf("episodes %v, next %v", eps, r.cam.NextDue())
	}
	if st := r.cam.Status(); st["detector"].(map[string]any)["state"] != "ended" || st["frames"].(int64) > 50 {
		t.Fatalf("%v", st)
	}
	if !strings.Contains(r.log.String(), `"type":"source_end"`) {
		t.Fatal("source end is not traced")
	}
}

type flaky struct {
	fails int
	err   error
}

func (f *flaky) Grab(float64, *image.Gray) error {
	if f.fails > 0 {
		f.fails--
		return f.err
	}
	return nil
}
func (f *flaky) Info() source.Info { return source.Info{Kind: "flaky", W: 160, H: 120} }
func (f *flaky) Close() error      { return nil }

// Сбой источника короче maxFails ошибок: ошибки считаются, в журнал пишется смена текста ошибки, кадр
// повторяется через период покоя, состояние для агента не меняется.
func TestSourceErrors(t *testing.T) {
	r := newRig(t, scene.Demo(), &flaky{fails: 2, err: errors.New("usb glitch")})
	r.advance(t, 0.5) // кадры 0 и 0,5 — ошибки
	st := r.cam.Status()
	if st["errors"] != int64(2) || st["frames"] != int64(0) || st["last_error"] != "usb glitch" ||
		st["detector"].(map[string]any)["state"] != "idle" {
		t.Fatalf("%v", st)
	}
	r.advance(t, 1.0)
	if st := r.cam.Status(); st["frames"] != int64(1) || st["last_error"] != nil {
		t.Fatalf("%v", st)
	}
	if n := strings.Count(r.log.String(), `"type":"source_error"`); n != 1 || len(r.events) != 0 {
		t.Fatalf("source_error traced %d times, events %v", n, r.events)
	}
}

// outage — синтетика, захват которой отказывает на [from, to) (камеру выдернули из USB).
type outage struct {
	source.Synthetic
	from, to float64
}

func (o outage) Grab(t float64, dst *image.Gray) error {
	if t >= o.from && t < o.to {
		return errors.New("VIDIOC_DQBUF: no such device")
	}
	return o.Synthetic.Grab(t, dst)
}

// Камера отказала посреди эпизода: после maxFails ошибок подряд эпизод завершён, motion = null с
// причиной (одно уведомление); первый удачный кадр — motion = false и переученный фон, дальше эпизоды
// находятся как обычно.
func TestSourceLost(t *testing.T) {
	sc := scene.Demo()
	r := newRig(t, sc, outage{Synthetic: source.Synthetic{Scene: scene.New(sc)}, from: 20, to: 60})
	r.advance(t, 25) // человек в кадре 15–27 с; ошибки в 20; 20,5; 21 с
	r.mu.Lock()
	kinds := []string{r.events[0].Kind, r.events[1].Kind}
	lost := r.events[1]
	r.mu.Unlock()
	if kinds[0] != MotionStart || kinds[1] != CameraLost || lost.T != 21 || lost.State["motion"] != nil ||
		lost.Episode["end"].(float64) >= 20 {
		t.Fatalf("%v %+v", kinds, lost)
	}
	motion := r.cam.State()["properties"].(map[string]any)["motion"].(map[string]any)
	if motion["value"] != nil || motion["error"] != "VIDIOC_DQBUF: no such device" {
		t.Fatalf("%v", motion)
	}
	st := r.cam.Status()
	if st["detector"].(map[string]any)["state"] != "lost" || st["episodes"] != int64(1) {
		t.Fatalf("%v", st)
	}
	if m := r.cam.Motion(); m["motion"] != nil || m["error"] == nil {
		t.Fatalf("%v", m)
	}
	r.advance(t, 120) // возврат в 60 с; человек 95–115 с
	r.mu.Lock()
	defer r.mu.Unlock()
	var got []string
	for _, e := range r.events {
		got = append(got, e.Kind)
	}
	if strings.Join(got, ",") != "motion_start,camera_lost,camera_back,motion_start,motion_end" ||
		r.events[2].T != 60 || r.events[2].State["motion"] != false {
		t.Fatalf("%v %+v", got, r.events[2])
	}
	log := r.log.String()
	for typ, n := range map[string]int{"source_lost": 1, "source_recovered": 1, "episode_end": 2, "source_error": 1} {
		if c := strings.Count(log, `"type":"`+typ+`"`); c != n {
			t.Errorf("%s traced %d times, want %d", typ, c, n)
		}
	}
}

// hanging — источник, захват которого ждёт release (зависшая камера); entered — захват начался.
type hanging struct{ entered, release chan struct{} }

func (h hanging) Grab(float64, *image.Gray) error {
	h.entered <- struct{}{}
	<-h.release
	return nil
}
func (h hanging) Info() source.Info { return source.Info{Kind: "hanging", W: 160, H: 120} }
func (h hanging) Close() error      { return nil }

// Пока захват ждёт кадр, camera_status, параметры и set_detector отвечают сразу.
func TestStatusDoesNotWaitForGrab(t *testing.T) {
	src := hanging{make(chan struct{}, 1), make(chan struct{})}
	r := newRig(t, scene.Demo(), src)
	done := make(chan struct{})
	go func() { r.cam.Step(0); close(done) }()
	<-src.entered
	answered := make(chan error, 1)
	go func() {
		r.cam.Status()
		p, idle, active := r.cam.Params()
		answered <- r.cam.SetParams(p, idle, active)
	}()
	select {
	case err := <-answered:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("status waits for the frame grab")
	}
	close(src.release)
	<-done
}

// cuts — источник, картинка которого целиком меняется в моменты at (камеру повернули).
type cuts struct{ at []float64 }

func (c cuts) Grab(t float64, dst *image.Gray) error {
	n := uint64(0)
	for _, a := range c.at {
		if t >= a {
			n++
		}
	}
	rng := rand.New(rand.NewPCG(n, 0))
	for i := range dst.Pix {
		dst.Pix[i] = uint8(rng.IntN(256))
	}
	return nil
}
func (c cuts) Info() source.Info { return source.Info{Kind: "cuts", W: 160, H: 120} }
func (c cuts) Close() error      { return nil }

// Глобальная смена картинки: фон переучивается без тревоги; в журнал — одна запись bg_reset на серию
// сбросов подряд (смены в 5; 5,5; 6 с — одна серия).
func TestBgResetTraced(t *testing.T) {
	r := newRig(t, scene.Demo(), cuts{[]float64{5, 5.5, 6, 20}})
	r.advance(t, 30)
	if n := strings.Count(r.log.String(), `"type":"bg_reset"`); n != 2 {
		t.Fatalf("bg_reset traced %d times", n)
	}
	if st := r.cam.Status(); st["bg_resets"] != int64(4) || len(r.events) != 0 {
		t.Fatalf("%v %v", st, r.events)
	}
}

// Отставание обработки: кадры, срок которых прошёл, считаются пропущенными.
func TestSkipped(t *testing.T) {
	r := newRig(t, scene.Demo(), nil)
	r.cam.Step(0)
	if err := r.vc.Set(2.2); err != nil {
		t.Fatal(err)
	}
	r.cam.Step(2.2) // срок был 0,5 с: пропущены 0,5 1,0 1,5 2,0 → 3 полных периода сверх текущего
	if st := r.cam.Status(); st["skipped"] != int64(3) || st["frames"] != int64(2) {
		t.Fatalf("%v", st)
	}
	if next := r.cam.NextDue(); next != 2.7 {
		t.Fatalf("next frame %v, want 2.7", next)
	}
}

func TestSetParams(t *testing.T) {
	r := newRig(t, scene.Demo(), nil)
	p, idle, active := r.cam.Params()
	p.Threshold = 30
	if err := r.cam.SetParams(p, 1, 40); err == nil {
		t.Fatal("fps above the limit accepted")
	}
	if q, _, _ := r.cam.Params(); q.Threshold != 20 {
		t.Fatal("a rejected change must not touch the detector")
	}
	if err := r.cam.SetParams(p, 1, active); err != nil {
		t.Fatal(err)
	}
	if q, i, _ := r.cam.Params(); q.Threshold != 30 || i != 1 || idle != 2 {
		t.Fatal("params not applied")
	}
	if !strings.Contains(r.log.String(), `"type":"set_detector"`) {
		t.Fatal("change is not traced")
	}
	if _, err := New(Options{Device: "c", Source: source.Synthetic{Scene: r.sc}, Detector: p, IdleFPS: 0, ActiveFPS: 5,
		Clock: r.vc}); err == nil {
		t.Fatal("zero idle fps accepted")
	}
	if _, err := New(Options{Source: source.Synthetic{Scene: r.sc}, Clock: r.vc}); err == nil {
		t.Fatal("camera without device accepted")
	}
}

// Реальное время: Run спит до сроков кадров и останавливается по отмене контекста.
func TestRunRealTime(t *testing.T) {
	cam, err := New(Options{Device: "cam1", Source: source.Synthetic{Scene: scene.New(scene.Demo()),
		Name: "/home/user/scenes/demo.json"}, Detector: detect.DefaultParams(), IdleFPS: 20, ActiveFPS: 20,
		Clock: clock.NewReal()})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { cam.Run(ctx); close(done) }()
	time.Sleep(500 * time.Millisecond)
	cancel()
	<-done
	st := cam.Status()
	if n := st["frames"].(int64); n < 6 || n > 12 {
		t.Fatalf("20 fps for 0.5 s gave %d frames", n)
	}
	if name := st["source"].(map[string]any)["name"]; name != "demo.json" {
		t.Fatalf("status must not leak host paths to the model: %v", name)
	}
	if err := cam.Close(); err != nil {
		t.Fatal(err)
	}
}

// Стоимость кадра конвейера (кадр синтетики 320×240 + детектор 160×120, человек всё время ходит по
// кадру); без выделений памяти.
func BenchmarkStep(b *testing.B) {
	sc := scene.Demo()
	sc.Lamps = nil
	path := make([][3]float64, 0, 401)
	for i := 0; i <= 400; i++ {
		path = append(path, [3]float64{float64(i) * 10, 0.2 + 0.6*float64(i%2), 0.6})
	}
	sc.Objects = []scene.Object{{ID: "walker", W: 0.12, H: 0.5, Albedo: 0.2, Path: path}}
	vc := clock.NewVirtual(0)
	cam, err := New(Options{Device: "cam1", Source: source.Synthetic{Scene: scene.New(sc)},
		Detector: detect.DefaultParams(), IdleFPS: 8, ActiveFPS: 8, Clock: vc})
	if err != nil {
		b.Fatal(err)
	}
	b.ReportAllocs()
	for b.Loop() {
		next := cam.NextDue()
		if err := vc.Set(next); err != nil {
			b.Fatal(err)
		}
		cam.Step(next)
	}
}
