package detect

import (
	"fmt"
	"image"
	"math"
	"math/rand/v2"
	"testing"

	"github.com/local/cammcp/internal/scene"
)

// textured — кадр 320×240 с узором (фон) и шумом σ ≈ 2 по seed.
func textured(seed uint64) *image.Gray {
	g := image.NewGray(image.Rect(0, 0, 320, 240))
	rng := rand.New(rand.NewPCG(seed, 1))
	for y := 0; y < 240; y++ {
		for x := 0; x < 320; x++ {
			v := 100 + 40*math.Sin(float64(x)/9) + 25*math.Cos(float64(y)/13) + 2*rng.NormFloat64()
			g.Pix[y*g.Stride+x] = uint8(v)
		}
	}
	return g
}

func clone(src *image.Gray) *image.Gray {
	g := image.NewGray(src.Rect)
	copy(g.Pix, src.Pix)
	return g
}

// withBlock — копия кадра с тёмным квадратом side×side в (x, y).
func withBlock(src *image.Gray, x, y, side int) *image.Gray {
	g := clone(src)
	for yy := max(0, y); yy < min(240, y+side); yy++ {
		for xx := max(0, x); xx < min(320, x+side); xx++ {
			g.Pix[yy*g.Stride+xx] = 15
		}
	}
	return g
}

func newDet(t *testing.T, p Params) *Detector {
	t.Helper()
	d, err := New(p, 320, 240)
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func TestValidate(t *testing.T) {
	if err := DefaultParams().Validate(); err != nil {
		t.Fatal(err)
	}
	for name, mod := range map[string]func(*Params){
		"size": func(p *Params) { p.W = 8 }, "thr": func(p *Params) { p.Threshold = 0 },
		"area": func(p *Params) { p.MinArea = 0.7 }, "max": func(p *Params) { p.MaxArea = 1.5 },
		"tau": func(p *Params) { p.BgTau = 0 }, "tau>1h": func(p *Params) { p.BgTau = 3601 },
		"k>n": func(p *Params) { p.K = 6 }, "n": func(p *Params) { p.K, p.N = 1, 40 },
		"nan": func(p *Params) { p.MinArea = math.NaN() },
	} {
		p := DefaultParams()
		mod(&p)
		if p.Validate() == nil {
			t.Errorf("%s accepted", name)
		}
	}
	if _, err := New(DefaultParams(), 100, 100); err == nil {
		t.Fatal("source smaller than the detector accepted")
	}
}

// Квадрат 30×30 (1,2 % кадра) идёт через кадр: начало — на K-м активном кадре, начало серии — первый
// активный кадр, конец — после N неактивных подряд; рамка эпизода покрывает весь путь.
func TestEpisode(t *testing.T) {
	d := newDet(t, DefaultParams())
	bg := textured(1)
	var starts, ends []float64
	tt := 0.0
	step := func(g *image.Gray) (Frame, Event) {
		f, ev := d.Process(g, tt)
		switch ev {
		case Start:
			starts = append(starts, tt)
		case End:
			ends = append(ends, tt)
		}
		tt += 0.5
		return f, ev
	}
	for i := 0; i < 10; i++ {
		if f, _ := step(textured(uint64(100 + i))); f.Active {
			t.Fatalf("background frame %d active: %+v", i, f)
		}
	}
	firstActive := tt
	for i := 0; i < 10; i++ {
		f, _ := step(withBlock(bg, 40+20*i, 100, 30))
		if !f.Active || f.Blobs != 1 || math.Abs(f.Area-0.0117) > 0.002 {
			t.Fatalf("block frame %d: %+v", i, f)
		}
		if f.Score < 1 || f.Center[1] < 0.45 || f.Center[1] > 0.52 {
			t.Fatalf("block frame %d: %+v", i, f)
		}
	}
	lastActive := tt - 0.5
	for i := 0; i < 10; i++ {
		step(textured(uint64(200 + i)))
	}
	if len(starts) != 1 || starts[0] != firstActive+1 || len(ends) != 1 || ends[0] != lastActive+5*0.5 {
		t.Fatalf("starts %v ends %v (first active %v, last %v)", starts, ends, firstActive, lastActive)
	}
	ep, going := d.Episode()
	if going || ep.ID != 1 || ep.Start != firstActive || ep.Confirm != firstActive+1 || ep.End != lastActive || ep.Frames != 10 {
		t.Fatalf("%+v going=%v", ep, going)
	}
	if ep.Box[0] > 40.0/320 || ep.Box[2] < (40+20*9+30)/320.0-0.01 || ep.PeakArea < 0.01 {
		t.Fatalf("box %+v", ep)
	}
	if d.Hot() {
		t.Fatal("hot after the episode")
	}
}

// Гистерезис: одиночный активный кадр не начинает эпизод; 3 из 5 — начинают.
func TestHysteresis(t *testing.T) {
	d := newDet(t, DefaultParams())
	bg := textured(1)
	moving := func(i int) *image.Gray { return withBlock(bg, 20+25*i, 60, 30) }
	seq := []bool{false, true, false, false, false, false, false, true, false, true, false, true}
	var events []Event
	for i, on := range seq {
		g := textured(uint64(300 + i))
		if on {
			g = moving(i)
		}
		_, ev := d.Process(g, float64(i))
		events = append(events, ev)
		if i == 1 && !d.Hot() {
			t.Fatal("one active frame must make the detector hot (higher frame rate)")
		}
	}
	for i, ev := range events {
		want := None
		if i == 11 {
			want = Start
		}
		if ev != want {
			t.Fatalf("frame %d: event %v, want %v (all: %v)", i, ev, want, events)
		}
	}
	ep, going := d.Episode()
	if !going || ep.Start != 7 || ep.Frames != 3 {
		t.Fatalf("%+v", ep)
	}
	if d.Flush() != End || d.Flush() != None {
		t.Fatal("flush must end the running episode once")
	}
}

// Редкая активность (мерцание в одном кадре из пяти: K из N не набирается) не попадает в эпизод: его
// начало, кадры и рамка — из активных кадров окна, в котором набралось K из N.
func TestEpisodeFromWindowOnly(t *testing.T) {
	d := newDet(t, DefaultParams())
	bg := textured(1)
	quiet := make([]*image.Gray, 8)
	for i := range quiet {
		quiet[i] = textured(uint64(500 + i))
	}
	flicker := withBlock(bg, 20, 20, 30)
	tt := 0.0
	for i := 0; i < 400; i++ { // 50 с при 8 кадрах/с, блок вверху слева
		g := quiet[i%len(quiet)]
		if i%5 == 1 {
			g = flicker
		}
		if _, ev := d.Process(g, tt); ev != None {
			t.Fatalf("flicker started an episode at %v s", tt)
		}
		tt += 0.125
	}
	start := tt
	var ev Event
	for i := 0; i < 3; i++ { // настоящее движение справа внизу
		_, ev = d.Process(withBlock(bg, 200+10*i, 150, 30), tt)
		tt += 0.125
	}
	ep, going := d.Episode()
	if ev != Start || !going || ep.Start != start || ep.Frames != 3 || ep.Box[0] < 0.6 || ep.Center[1] < 0.6 {
		t.Fatalf("%v %+v", ev, ep)
	}
}

// Фон — экспоненциальное среднее с постоянной времени BgTau: без смещения от округления и без
// застревания при долгой BgTau, когда шаг за кадр меньше уровня яркости; под маской — в 8 раз медленнее.
func TestBackgroundAverage(t *testing.T) {
	p := DefaultParams()
	p.W, p.H, p.BgTau = 32, 24, 1200
	d, err := New(p, p.W, p.H)
	if err != nil {
		t.Fatal(err)
	}
	g := image.NewGray(image.Rect(0, 0, p.W, p.H))
	fill := func(v uint8) {
		for i := range g.Pix {
			g.Pix[i] = v
		}
	}
	level := func(i int) float64 { return float64(d.bg[i]) / 65536 }
	const dt, n = 0.125, 2000
	fill(100)
	tt := 0.0
	d.Process(g, tt)
	for k := 0; k < n; k++ { // шум ±2 вокруг 100: среднее не уплывает
		fill(uint8(98 + 4*(k%2)))
		tt += dt
		d.Process(g, tt)
	}
	if b := level(0); math.Abs(b-100) > 0.1 {
		t.Fatalf("background drifted to %.2f", b)
	}
	b0 := level(0)
	for k := 0; k < n; k++ { // всё светлеет до 110, а в углу встал тёмный блок 8×8 (маска)
		fill(110)
		for y := 0; y < 8; y++ {
			clear(g.Pix[y*p.W : y*p.W+8])
		}
		tt += dt
		d.Process(g, tt)
	}
	decay := math.Exp(-n * dt / p.BgTau)
	if want := 110 - (110-b0)*decay; math.Abs(level(len(g.Pix)-1)-want) > 0.05 {
		t.Fatalf("background %.2f, want %.2f", level(len(g.Pix)-1), want)
	}
	if want := b0 * math.Pow(decay, 1.0/8); math.Abs(level(0)-want) > 0.05 {
		t.Fatalf("masked background %.2f, want %.2f", level(0), want)
	}
}

// Ступенька освещённости (лампа: ×1,5 и неравномерность ±5 %) не даёт активных кадров: её съедает
// компенсация общей яркости, а фон затем подтягивается.
func TestLampStepIsNotMotion(t *testing.T) {
	d := newDet(t, DefaultParams())
	for i := 0; i < 5; i++ {
		d.Process(textured(uint64(i)), float64(i))
	}
	for i := 0; i < 20; i++ {
		g := textured(uint64(50 + i))
		for y := 0; y < 240; y++ {
			for x := 0; x < 320; x++ {
				k := 1.5 * (1 + 0.05*(2*float64(x)/320-1))
				g.Pix[y*g.Stride+x] = uint8(min(255, float64(g.Pix[y*g.Stride+x])*k))
			}
		}
		f, ev := d.Process(g, float64(5+i))
		if f.Active || f.Reset || ev != None {
			t.Fatalf("lamp frame %d: %+v %v", i, f, ev)
		}
		if i == 0 && math.Abs(f.Gain-1.5) > 0.05 {
			t.Fatalf("gain %.3f, want 1.5", f.Gain)
		}
	}
}

// Смена всей картинки (камеру повернули) — глобальное изменение: фон переучивается, тревоги нет.
func TestGlobalChangeRelearns(t *testing.T) {
	d := newDet(t, DefaultParams())
	d.Process(textured(1), 0)
	d.Process(textured(2), 1)
	other := image.NewGray(image.Rect(0, 0, 320, 240))
	for i := range other.Pix {
		other.Pix[i] = uint8(30 + (i/7)%200)
	}
	if f, _ := d.Process(other, 2); !f.Reset || f.Active {
		t.Fatalf("%+v", f)
	}
	if f, _ := d.Process(other, 3); f.Reset || f.Active {
		t.Fatalf("after relearning: %+v", f)
	}
}

// Шум «соль и перец» (1 % пикселей) убирает открытие 3×3; область меньше MinArea не активна.
func TestSpecklesAndMinArea(t *testing.T) {
	d := newDet(t, DefaultParams())
	bg := textured(1)
	d.Process(bg, 0)
	rng := rand.New(rand.NewPCG(9, 9))
	salt := clone(bg)
	for i := 0; i < len(salt.Pix)/100; i++ {
		salt.Pix[rng.IntN(len(salt.Pix))] = 255
	}
	if f, _ := d.Process(salt, 1); f.Active {
		t.Fatalf("speckles: %+v", f)
	}
	if f, _ := d.Process(withBlock(bg, 100, 100, 16), 2); f.Active { // 0,33 % < 0,5 %
		t.Fatalf("small block: %+v", f)
	}
	if f, _ := d.Process(withBlock(bg, 100, 100, 24), 3); !f.Active { // 0,75 %
		t.Fatalf("block above min area: %+v", f)
	}
}

func TestSetParams(t *testing.T) {
	d := newDet(t, DefaultParams())
	p := d.Params()
	p.Threshold, p.K, p.N = 40, 2, 4
	if err := d.SetParams(p); err != nil || d.Params().Threshold != 40 {
		t.Fatal(err)
	}
	p.W = 80
	if d.SetParams(p) == nil {
		t.Fatal("size change accepted")
	}
	p = d.Params()
	p.K = 9
	if d.SetParams(p) == nil || d.Params().K != 2 {
		t.Fatal("invalid params accepted")
	}
}

// demoFrames — кадры демонстрационной сцены w×h с объектом в кадре (для нагрузки на конвейер).
func demoFrames(w, h, n int) []*image.Gray {
	sc := scene.Demo()
	sc.Width, sc.Height = w, h
	s := scene.New(sc)
	out := make([]*image.Gray, n)
	for i := range out {
		out[i] = image.NewGray(image.Rect(0, 0, w, h))
		s.Render(18+0.25*float64(i), out[i])
	}
	return out
}

func TestProcessDoesNotAllocate(t *testing.T) {
	frames := demoFrames(320, 240, 8)
	d := newDet(t, DefaultParams())
	i := 0
	allocs := testing.AllocsPerRun(50, func() {
		d.Process(frames[i%len(frames)], float64(i)*0.25)
		i++
	})
	if allocs != 0 {
		t.Fatalf("%v allocs per frame", allocs)
	}
	if _, going := d.Episode(); !going {
		t.Fatal("the walking person must be detected (the loop must exercise region search)")
	}
}

// Стоимость кадра: источник 640×480 (типичная USB-камера), обработка 160×120 и 320×240.
func BenchmarkProcess(b *testing.B) {
	frames := demoFrames(640, 480, 16)
	for _, size := range [][2]int{{160, 120}, {320, 240}} {
		b.Run(fmt.Sprintf("%dx%d", size[0], size[1]), func(b *testing.B) {
			p := DefaultParams()
			p.W, p.H = size[0], size[1]
			d, err := New(p, 640, 480)
			if err != nil {
				b.Fatal(err)
			}
			b.ReportAllocs()
			i := 0
			for b.Loop() {
				d.Process(frames[i%len(frames)], float64(i)*0.25)
				i++
			}
		})
	}
}
