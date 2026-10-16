// Package camera — конвейер cam-mcp: кадр источника → детектор → эпизоды движения и состояние камеры.
//
// Кадры не уходят в LLM: плата сама находит движение, агенту сообщается только начало и конец эпизода
// (OnEvent), а маленький JPEG отдаётся по запросу (Snapshot). Частота обработки адаптивная: IdleFPS в
// покое и ActiveFPS, пока детектор видит движение или кандидата на него.
//
// Время — только из часов cam-mcp. В реальном режиме Run спит до срока следующего кадра (без опроса);
// в виртуальном кадры обрабатывает AdvanceTo — каждый в свой виртуальный срок, поэтому прогон дня
// идёт быстрее реального времени, а эпизоды получают точное время.
//
// Отказ источника — как у датчиков hw-mcp: после maxFails ошибок подряд камера недоступна (идущий
// эпизод завершается, motion = null с причиной, агент получает уведомление), первый удачный кадр
// возвращает motion = false и переучивает фон.
package camera

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"image"
	"image/jpeg"
	"io"
	"math"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"github.com/local/cammcp/internal/clock"
	"github.com/local/cammcp/internal/detect"
	"github.com/local/cammcp/internal/luma"
	"github.com/local/cammcp/internal/source"
	"github.com/local/cammcp/internal/trace"
)

// Options — параметры камеры.
type Options struct {
	Device             string // id устройства в инвентаре
	Source             source.Source
	Detector           detect.Params
	IdleFPS, ActiveFPS float64
	Clock              clock.Clock
	Trace              *trace.Writer
	// OnEvent получает переходы (Event) из горутины обработки кадров; не должен блокироваться.
	OnEvent func(Event)
}

// Виды событий.
const (
	MotionStart = "motion_start"
	MotionEnd   = "motion_end"
	CameraLost  = "camera_lost" // maxFails ошибок источника подряд: motion = null
	CameraBack  = "camera_back" // первый удачный кадр после отказа: motion = false
)

// maxFails — ошибок источника подряд до перехода в «камера недоступна» (как у датчиков hw-mcp).
const maxFails = 3

// Event — переход состояния камеры: начало или конец эпизода движения, отказ или возврат источника.
type Event struct {
	Kind    string         // MotionStart | MotionEnd | CameraLost | CameraBack
	T       float64        // время кадра (попытки захвата) перехода
	State   map[string]any // новые значения свойств: motion, area_pct, zone
	Changed []string       // изменившиеся свойства (по имени)
	Episode map[string]any // сводка эпизода (завершённого отказом — у CameraLost; nil — эпизода нет)
}

// Camera — камера с детектором. Методы потокобезопасны.
type Camera struct {
	dev  string
	clk  clock.Clock
	src  source.Source
	info source.Info
	tr   *trace.Writer
	on   func(Event)
	t0   float64
	cpu0 float64

	stepMu sync.Mutex  // кадры по очереди (Step, AdvanceTo, Close); захват идёт вне procMu
	work   *image.Gray // кадр захвата (stepMu)

	procMu sync.Mutex // детектор, параметры, расписание
	det    *detect.Detector
	idle   float64
	active float64
	next   float64
	ended  bool
	fails  int    // ошибок источника подряд
	errMsg string // текст последней ошибки источника (пусто — последний захват удался)
	reset  bool   // предыдущий кадр переучил фон

	mu    sync.Mutex // опубликованное: последний кадр и его итог, эпизод, состояние, счётчики
	last  *image.Gray
	lastT float64
	frame detect.Frame   // итог детектора по кадру last
	ep    detect.Episode // текущий или последний эпизод (ID = 0 — эпизодов ещё не было)
	going bool
	st    state
	n     counters
}

// state — свойства камеры для агента: значения последнего уведомления.
type state struct {
	motion any     // true | false; nil — камера недоступна
	area   float64 // %, округлено до 0.1
	zone   any     // string или nil (движения ещё не было)
	err    string  // причина недоступности
	ts     float64 // время последнего изменения
}

func (s state) values() map[string]any {
	return map[string]any{"motion": s.motion, "area_pct": s.area, "zone": s.zone}
}

type counters struct {
	frames, skipped, errors, episodes, resets, snapshots, snapBytes int64
	grabNs, detectNs, detectMaxNs                                   int64
}

// New готовит камеру; срок первого кадра — момент создания.
func New(o Options) (*Camera, error) {
	if o.Source == nil || o.Clock == nil || o.Device == "" {
		return nil, errors.New("camera: source, clock and device are required")
	}
	info := o.Source.Info()
	det, err := detect.New(o.Detector, info.W, info.H)
	if err != nil {
		return nil, err
	}
	c := &Camera{dev: o.Device, clk: o.Clock, src: o.Source, info: info, tr: o.Trace, on: o.OnEvent, det: det,
		work: image.NewGray(image.Rect(0, 0, info.W, info.H)), last: image.NewGray(image.Rect(0, 0, info.W, info.H))}
	if c.idle, c.active, err = c.rates(o.IdleFPS, o.ActiveFPS); err != nil {
		return nil, err
	}
	if c.on == nil {
		c.on = func(Event) {}
	}
	c.t0 = c.clk.Now()
	c.next, c.lastT = c.t0, math.Inf(-1)
	c.st = state{motion: false, ts: c.t0}
	c.cpu0 = processCPU()
	return c, nil
}

// maxFPS — предел частоты кадров.
const maxFPS = 30

// rates проверяет частоты: активная — не выше собственной частоты источника, покоя — не выше активной.
func (c *Camera) rates(idle, active float64) (float64, float64, error) {
	if !(idle > 0) || !(active >= idle) || active > maxFPS {
		return 0, 0, fmt.Errorf("need 0 < idle fps <= active fps <= %d", maxFPS)
	}
	if c.info.FPS > 0 {
		active = min(active, c.info.FPS)
	}
	return min(idle, active), active, nil
}

// Info — описание источника.
func (c *Camera) Info() source.Info { return c.info }

// Device — id устройства.
func (c *Camera) Device() string { return c.dev }

// period — интервал до следующего кадра (procMu).
func (c *Camera) period() float64 {
	if c.det.Hot() {
		return 1 / c.active
	}
	return 1 / c.idle
}

// NextDue — срок следующего кадра (+Inf, если источник кончился).
func (c *Camera) NextDue() float64 {
	c.procMu.Lock()
	defer c.procMu.Unlock()
	if c.ended {
		return math.Inf(1)
	}
	return c.next
}

// Run обрабатывает кадры в реальном времени до отмены ctx, засыпая до срока следующего кадра.
func (c *Camera) Run(ctx context.Context) {
	t := time.NewTimer(time.Hour)
	t.Stop()
	defer t.Stop()
	for {
		next := c.NextDue()
		if math.IsInf(next, 1) {
			<-ctx.Done()
			return
		}
		// запас 1 мкс: время — секунды Unix в float64 (шаг ≈ 0,24 мкс), без него таймер мог бы
		// проснуться на единицу округления раньше срока
		t.Reset(time.Duration(max(0, next-c.clk.Now())*float64(time.Second)) + time.Microsecond)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			c.Step(c.clk.Now())
		}
	}
}

// AdvanceTo продвигает виртуальные часы до to, обрабатывая каждый кадр в его срок.
func (c *Camera) AdvanceTo(vc *clock.Virtual, to float64) error {
	c.stepMu.Lock()
	defer c.stepMu.Unlock()
	if to < vc.Now() {
		return fmt.Errorf("clock cannot go back: %g < %g", to, vc.Now())
	}
	for next := c.NextDue(); next <= to; next = c.NextDue() {
		if err := vc.Set(max(next, vc.Now())); err != nil {
			return err
		}
		c.step(vc.Now())
	}
	return vc.Set(to)
}

// Step обрабатывает кадр, срок которого наступил к моменту now; кадры, срок которых прошёл, пока
// обработка отставала (перегрузка, квота CPU), считаются пропущенными.
func (c *Camera) Step(now float64) {
	c.stepMu.Lock()
	defer c.stepMu.Unlock()
	c.step(now)
}

// step — Step под stepMu. Захват (у V4L2 — ожидание свежего кадра, при зависшей камере — до тайм-аута)
// идёт вне procMu: camera_status и set_detector камеру не ждут.
func (c *Camera) step(now float64) {
	c.procMu.Lock()
	due := !c.ended && now >= c.next
	skipped := int64(max(0, math.Floor((now-c.next)/c.period())))
	c.procMu.Unlock()
	if !due {
		return
	}
	g0 := time.Now()
	err := c.src.Grab(now-c.t0, c.work)
	g1 := time.Now()

	c.procMu.Lock()
	defer c.procMu.Unlock()
	if err != nil {
		c.failed(now, err)
		return
	}
	if c.fails >= maxFails {
		c.recovered(now)
	}
	c.fails, c.errMsg = 0, ""
	f, ev := c.det.Process(c.work, now)
	g2 := time.Now()
	ep, going := c.det.Episode()

	c.mu.Lock()
	c.last, c.work = c.work, c.last
	c.lastT, c.frame, c.ep, c.going = now, f, ep, going
	n := &c.n
	n.frames++
	n.skipped += skipped
	n.grabNs += g1.Sub(g0).Nanoseconds()
	dn := g2.Sub(g1).Nanoseconds()
	n.detectNs += dn
	n.detectMaxNs = max(n.detectMaxNs, dn)
	if f.Reset {
		n.resets++
	}
	if ev == detect.End {
		n.episodes++
	}
	c.mu.Unlock()

	if f.Reset && !c.reset {
		c.tr.Emit("bg_reset", map[string]any{"device": c.dev, "gain": round(f.Gain, 2), "ts_clock": now})
	}
	c.reset = f.Reset
	switch ev {
	case detect.Start:
		c.emit(MotionStart, episodeState(ep, true, now), c.traceEpisode("episode_start", ep, true, now))
	case detect.End:
		c.emit(MotionEnd, episodeState(ep, false, now), c.traceEpisode("episode_end", ep, false, now))
	}
	c.schedule(now)
}

// episodeState — состояние по эпизоду: area_pct — его пиковая площадь, zone — зона в кадре наибольшей
// площади.
func episodeState(ep detect.Episode, motion bool, now float64) state {
	return state{motion: motion, area: round(ep.PeakArea*100, 1), zone: zoneOf(ep.Center), ts: now}
}

// traceEpisode пишет переход эпизода в журнал и возвращает сводку для уведомления.
func (c *Camera) traceEpisode(typ string, ep detect.Episode, going bool, now float64) map[string]any {
	v := episodeView(ep, going, now)
	c.tr.Emit(typ, map[string]any{"device": c.dev, "episode": v, "ts_clock": now})
	return v
}

// emit меняет состояние для агента и отдаёт событие для уведомления.
func (c *Camera) emit(kind string, st state, episode map[string]any) {
	c.mu.Lock()
	changed := []string{"motion"}
	if st.area != c.st.area {
		changed = append(changed, "area_pct")
	}
	if st.zone != c.st.zone {
		changed = append(changed, "zone")
	}
	c.st = st
	c.mu.Unlock()
	c.on(Event{Kind: kind, T: st.ts, State: st.values(), Changed: changed, Episode: episode})
}

// schedule назначает следующий кадр: держит ритм от срока текущего, а при отставании — от now.
func (c *Camera) schedule(now float64) {
	if c.next += c.period(); c.next <= now {
		c.next = now + c.period()
	}
}

// failed — ошибка источника (procMu). Конец кадров (io.EOF) останавливает камеру, идущий эпизод
// завершается. Прочие ошибки считаются и пишутся в журнал при смене текста, кадр повторяется через
// период покоя; на maxFails-й ошибке подряд камера становится недоступной.
func (c *Camera) failed(now float64, err error) {
	if errors.Is(err, io.EOF) {
		c.ended = true
		c.tr.Emit("source_end", map[string]any{"device": c.dev, "ts_clock": now})
		if ep, ok := c.flush(); ok {
			c.emit(MotionEnd, episodeState(ep, false, now), c.traceEpisode("episode_end", ep, false, now))
		}
		return
	}
	c.mu.Lock()
	c.n.errors++
	c.mu.Unlock()
	if msg := err.Error(); msg != c.errMsg {
		c.errMsg = msg
		c.tr.Emit("source_error", map[string]any{"device": c.dev, "error": msg, "ts_clock": now})
	}
	c.next = now + 1/c.idle
	if c.fails++; c.fails == maxFails {
		c.lost(now)
	}
}

// lost — камера недоступна (procMu): идущий эпизод завершается, motion = null с причиной.
func (c *Camera) lost(now float64) {
	c.mu.Lock()
	st := c.st
	c.mu.Unlock()
	var episode map[string]any
	if ep, ok := c.flush(); ok {
		st, episode = episodeState(ep, false, now), c.traceEpisode("episode_end", ep, false, now)
	}
	st.motion, st.err, st.ts = nil, c.errMsg, now
	c.tr.Emit("source_lost", map[string]any{"device": c.dev, "error": c.errMsg, "ts_clock": now})
	c.emit(CameraLost, st, episode)
}

// flush — перерыв в кадрах: детектор завершает идущий эпизод и забывает фон; ok — эпизод шёл.
func (c *Camera) flush() (detect.Episode, bool) {
	if c.det.Flush() != detect.End {
		return detect.Episode{}, false
	}
	ep, _ := c.det.Episode()
	c.mu.Lock()
	c.ep, c.going = ep, false
	c.n.episodes++
	c.mu.Unlock()
	return ep, true
}

// recovered — первый удачный кадр после отказа (procMu): motion = false, фон переучит этот кадр;
// down_s — сколько камера была недоступна.
func (c *Camera) recovered(now float64) {
	c.mu.Lock()
	st := c.st
	c.mu.Unlock()
	c.tr.Emit("source_recovered", map[string]any{"device": c.dev, "down_s": round(now-st.ts, 3), "ts_clock": now})
	st.motion, st.err, st.ts = false, "", now
	c.emit(CameraBack, st, nil)
}

// zones — части кадра 3×3 (по центру движения).
var zones = [3][3]string{{"top-left", "top", "top-right"}, {"left", "center", "right"},
	{"bottom-left", "bottom", "bottom-right"}}

// Zones — все значения свойства zone.
func Zones() []string {
	out := make([]string, 0, 9)
	for _, row := range zones {
		out = append(out, row[:]...)
	}
	return out
}

func zoneOf(c [2]float64) string {
	return zones[min(2, max(0, int(c[1]*3)))][min(2, max(0, int(c[0]*3)))]
}

// round округляет до digits знаков после запятой; деление на целую степень 10 даёт ближайшее к
// десятичной записи число, и JSON печатает «3.9», а не «3.9000000000000004».
func round(x float64, digits int) float64 {
	p := math.Pow10(digits)
	return math.Round(x*p) / p
}

func roundBox(b [4]float64) []float64 {
	return []float64{round(b[0], 2), round(b[1], 2), round(b[2], 2), round(b[3], 2)}
}

// episodeSummary — сводка эпизода на момент now (идущего — по последний кадр).
func episodeSummary(ep detect.Episode, going bool, now float64) map[string]any {
	end := ep.End
	if going {
		end = now
	}
	return map[string]any{"id": ep.ID, "frames": ep.Frames, "peak_area_pct": round(ep.PeakArea*100, 1),
		"peak_score": round(ep.PeakScore, 2), "box": roundBox(ep.Box), "zone": zoneOf(ep.Center),
		"duration_s": round(end-ep.Start, 1)}
}

// episodeView — сводка с временами по часам cam-mcp и центром движения (уведомление, журнал).
func episodeView(ep detect.Episode, going bool, now float64) map[string]any {
	v := episodeSummary(ep, going, now)
	v["start"], v["confirm"] = round(ep.Start, 3), round(ep.Confirm, 3)
	v["center"] = []float64{round(ep.Center[0], 2), round(ep.Center[1], 2)}
	if !going {
		v["end"] = round(ep.End, 3)
	}
	return v
}

// State — ресурс состояния (как hw://state/<устройство> у hw-mcp): значения свойств с единицами,
// временем изменения и возрастом; у недоступной камеры motion — null с причиной ("error").
func (c *Camera) State() map[string]any {
	now := c.clk.Now()
	c.mu.Lock()
	st := c.st
	c.mu.Unlock()
	prop := func(v any, unit string) map[string]any {
		p := map[string]any{"value": v, "ts": round(st.ts, 3), "age_s": round(now-st.ts, 3)}
		if unit != "" {
			p["unit"] = unit
		}
		return p
	}
	motion := prop(st.motion, "")
	if st.err != "" {
		motion["error"] = st.err
	}
	return map[string]any{"device": c.dev, "now": now, "properties": map[string]any{
		"motion": motion, "area_pct": prop(st.area, "%"), "zone": prop(st.zone, "")}}
}

// Motion — результат motion_state: состояние, движение в последнем кадре (если есть) и сводка текущего
// или последнего эпизода на сейчас, с относительными временами (секунды назад — короче и понятнее LLM,
// чем время Unix).
func (c *Camera) Motion() map[string]any {
	now := c.clk.Now()
	c.mu.Lock()
	defer c.mu.Unlock()
	out := map[string]any{"motion": c.st.motion, "since_s": round(now-c.st.ts, 1), "episodes": c.n.episodes}
	if c.st.err != "" {
		out["error"] = c.st.err
	}
	if c.frame.Active {
		out["frame"] = map[string]any{"area_pct": round(c.frame.Area*100, 1), "zone": zoneOf(c.frame.Center),
			"box": roundBox(c.frame.Box)}
	}
	if c.ep.ID > 0 {
		ep := episodeSummary(c.ep, c.going, now)
		ep["started_s_ago"] = round(now-c.ep.Start, 1)
		if !c.going {
			ep["ended_s_ago"] = round(now-c.ep.End, 1)
		}
		out["episode"] = ep
	}
	return out
}

// Status — результат camera_status и итог run_end: частоты, кадры, затраты CPU, состояние детектора.
func (c *Camera) Status() map[string]any {
	c.procMu.Lock()
	p := c.det.Params()
	_, going := c.det.Episode()
	target := 1 / c.period()
	var detState string
	switch {
	case c.ended:
		detState, target = "ended", 0
	case c.fails >= maxFails:
		detState = "lost"
	case going:
		detState = "motion"
	case c.det.Hot():
		detState = "candidate"
	default:
		detState = "idle"
	}
	idle, active, errMsg := c.idle, c.active, c.errMsg
	c.procMu.Unlock()

	now := c.clk.Now()
	cpu := processCPU() - c.cpu0
	c.mu.Lock()
	n := c.n
	c.mu.Unlock()
	per := func(total int64) float64 {
		if n.frames == 0 {
			return 0
		}
		return float64(total) / float64(n.frames)
	}
	out := map[string]any{
		"device": c.dev,
		// имя без каталога: путь на хосте модели не нужен (полный — в run_start журнала)
		"source": map[string]any{"kind": c.info.Kind, "name": filepath.Base(c.info.Name), "w": c.info.W,
			"h": c.info.H, "format": c.info.Format, "fps": c.info.FPS},
		"detector": map[string]any{"state": detState, "w": p.W, "h": p.H, "threshold": p.Threshold,
			"min_area_pct": round(p.MinArea*100, 2), "max_area_pct": round(p.MaxArea*100, 1),
			"bg_tau_s": p.BgTau, "k": p.K, "n": p.N},
		"fps": map[string]any{"idle": idle, "active": active, "target": round(target, 2),
			"mean": round(float64(n.frames)/max(now-c.t0, 1e-9), 2)},
		"frames": n.frames, "skipped": n.skipped, "errors": n.errors, "episodes": n.episodes,
		"bg_resets": n.resets, "snapshots": n.snapshots, "snapshot_bytes": n.snapBytes,
		"grab_us": round(per(n.grabNs)/1e3, 0), "detect_us": round(per(n.detectNs)/1e3, 0),
		"detect_us_max": round(float64(n.detectMaxNs)/1e3, 0), "uptime_s": round(now-c.t0, 1),
		"cpu_s": round(cpu, 3), "cpu_ms_per_frame": round(per(int64(cpu*1e9))/1e6, 3),
	}
	if errMsg != "" {
		out["last_error"] = errMsg
	}
	return out
}

// SetParams меняет параметры детектора и частоты кадров на ходу.
func (c *Camera) SetParams(p detect.Params, idleFPS, activeFPS float64) error {
	c.procMu.Lock()
	defer c.procMu.Unlock()
	idle, active, err := c.rates(idleFPS, activeFPS)
	if err != nil {
		return err
	}
	if err := c.det.SetParams(p); err != nil {
		return err
	}
	c.idle, c.active = idle, active
	c.tr.Emit("set_detector", map[string]any{"device": c.dev, "threshold": p.Threshold, "min_area": p.MinArea,
		"max_area": p.MaxArea, "k": p.K, "n": p.N, "idle_fps": c.idle, "active_fps": c.active})
	return nil
}

// Params — текущие параметры детектора и частоты кадров.
func (c *Camera) Params() (p detect.Params, idleFPS, activeFPS float64) {
	c.procMu.Lock()
	defer c.procMu.Unlock()
	return c.det.Params(), c.idle, c.active
}

// Snapshot — последний кадр в оттенках серого шириной не больше maxW, JPEG с качеством quality; рамка
// движения в этом кадре нарисована на нём. meta — размер, байты, возраст кадра, движение, рамка.
func (c *Camera) Snapshot(maxW, quality int) ([]byte, map[string]any, error) {
	c.mu.Lock()
	if math.IsInf(c.lastT, -1) {
		c.mu.Unlock()
		return nil, nil, errors.New("no frame yet")
	}
	w := min(maxW, c.info.W)
	h := max(1, int(math.Round(float64(c.info.H)*float64(w)/float64(c.info.W))))
	img := image.NewGray(image.Rect(0, 0, w, h))
	rs, err := luma.NewResizer(c.info.W, c.info.H, w, h)
	if err == nil {
		rs.Resize(img, c.last)
	}
	age, motion := c.clk.Now()-c.lastT, c.st.motion
	var box []float64
	if c.frame.Active {
		box = roundBox(c.frame.Box)
	}
	c.mu.Unlock()
	if err != nil {
		return nil, nil, err
	}
	if box != nil {
		drawBox(img, box)
	}
	var buf bytes.Buffer
	if err := jpeg.Encode(&buf, img, &jpeg.Options{Quality: quality}); err != nil {
		return nil, nil, err
	}
	c.mu.Lock()
	c.n.snapshots++
	c.n.snapBytes += int64(buf.Len())
	c.mu.Unlock()
	meta := map[string]any{"w": w, "h": h, "bytes": buf.Len(), "quality": quality, "age_s": round(age, 1),
		"motion": motion}
	if box != nil {
		meta["box"] = box
	}
	return buf.Bytes(), meta, nil
}

// drawBox рисует рамку (доли кадра) двойной линией — белой снаружи и чёрной внутри, чтобы её было
// видно на любом фоне.
func drawBox(img *image.Gray, box []float64) {
	w, h := img.Rect.Dx(), img.Rect.Dy()
	x0, y0 := int(box[0]*float64(w)), int(box[1]*float64(h))
	x1, y1 := min(w-1, int(box[2]*float64(w))), min(h-1, int(box[3]*float64(h)))
	for i, v := range []uint8{255, 0} {
		ax0, ay0, ax1, ay1 := x0+i, y0+i, x1-i, y1-i
		if ax0 > ax1 || ay0 > ay1 {
			return
		}
		for x := ax0; x <= ax1; x++ {
			img.Pix[ay0*img.Stride+x], img.Pix[ay1*img.Stride+x] = v, v
		}
		for y := ay0; y <= ay1; y++ {
			img.Pix[y*img.Stride+ax0], img.Pix[y*img.Stride+ax1] = v, v
		}
	}
}

// Close закрывает источник (дождавшись идущего захвата).
func (c *Camera) Close() error {
	c.stepMu.Lock()
	defer c.stepMu.Unlock()
	return c.src.Close()
}

// processCPU — процессорное время процесса (user + system), с. getrusage(RUSAGE_SELF) с верным
// указателем отказать не может (ошибки — только EFAULT и EINVAL), поэтому ошибка даёт 0.
func processCPU() float64 {
	var ru syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &ru); err != nil {
		return 0
	}
	return float64(ru.Utime.Nano()+ru.Stime.Nano()) / 1e9
}
