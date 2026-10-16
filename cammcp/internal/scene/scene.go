// Package scene — синтетическая сцена: источник кадров, пока камеры нет, и «истина» для проверки
// детектора движения.
//
// Модель кадра (яркость 0..255): пиксель = альбедо · освещённость · 255 + шум сенсора.
//   - Фон — текстура по seed: крупные плоскости (мебель, дверь) и мелкий узор из синусоид.
//   - Освещённость — медленный дрейф «день/ночь» (косинус с периодом period_s) плюс СТУПЕНЧАТО
//     включаемые лампы со слабой неравномерностью слева направо (gradient). Ступенька — классическая
//     ложная тревога детектора на разности кадров.
//   - Шум сенсора — гауссов, σ = noise уровней; дрожание камеры — сдвиг всего кадра на случайную
//     дробную величину до ±jitter_px по каждой оси (билинейная выборка фона).
//   - Объекты — эллипсы своего альбедо, движутся по ломаной [t, x, y] (центр в долях кадра);
//     существуют от первой до последней точки пути, вне кадра просто не видны.
//
// Кадр зависит только от seed и времени t (с точностью до 1 мс), а не от порядка вызовов, поэтому
// прогон воспроизводим при любой частоте кадров. Параметры сцены — допущения для проверки детектора,
// а не модель конкретной камеры.
package scene

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"math"
	"math/rand/v2"
	"os"
)

// Light — освещённость без ламп: base + amp·cos(2π(t+phase_s)/period_s); period_s = 0 — без дрейфа.
type Light struct {
	Base    float64 `json:"base"`
	Amp     float64 `json:"amp"`
	PeriodS float64 `json:"period_s"`
	PhaseS  float64 `json:"phase_s"`
}

// Lamp — лампа: прибавка освещённости gain на [on_s, off_s) (off_s = 0 — не выключается), на левом
// краю кадра в (1 − gradient) раз, на правом — в (1 + gradient).
type Lamp struct {
	OnS      float64 `json:"on_s"`
	OffS     float64 `json:"off_s"`
	Gain     float64 `json:"gain"`
	Gradient float64 `json:"gradient"`
}

// Object — движущийся объект: эллипс w×h (доли кадра) с альбедо albedo; путь — точки [t, x, y].
type Object struct {
	ID     string       `json:"id"`
	W      float64      `json:"w"`
	H      float64      `json:"h"`
	Albedo float64      `json:"albedo"`
	Path   [][3]float64 `json:"path"`
}

// Scenario — описание сцены (JSON, см. demo.json и README).
type Scenario struct {
	Width    int      `json:"width"`
	Height   int      `json:"height"`
	Seed     uint64   `json:"seed"`
	Noise    float64  `json:"noise"`
	JitterPx float64  `json:"jitter_px"`
	Light    Light    `json:"light"`
	Lamps    []Lamp   `json:"lamps"`
	Objects  []Object `json:"objects"`
}

// maxObjects — предел числа объектов сценария.
const maxObjects = 32

//go:embed demo.json
var demoJSON []byte

// Demo — встроенный демонстрационный сценарий (demo.json): четыре объекта, лампа и дрейф света за 220 с.
func Demo() *Scenario {
	sc, err := Parse(demoJSON)
	if err != nil {
		panic(err)
	}
	return sc
}

// Load читает сценарий из файла.
func Load(path string) (*Scenario, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	sc, err := Parse(b)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return sc, nil
}

// Parse разбирает и проверяет сценарий; неизвестные поля — ошибка.
func Parse(b []byte) (*Scenario, error) {
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	var sc Scenario
	if err := dec.Decode(&sc); err != nil {
		return nil, fmt.Errorf("scenario: %w", err)
	}
	if err := sc.validate(); err != nil {
		return nil, fmt.Errorf("scenario: %w", err)
	}
	return &sc, nil
}

func (sc *Scenario) validate() error {
	switch {
	case sc.Width < 32 || sc.Width > 1920 || sc.Height < 24 || sc.Height > 1080:
		return fmt.Errorf("size %dx%d outside 32x24..1920x1080", sc.Width, sc.Height)
	case sc.Noise < 0 || sc.Noise > 50:
		return errors.New("noise must be in [0, 50]")
	case sc.JitterPx < 0 || sc.JitterPx > 8:
		return errors.New("jitter_px must be in [0, 8]")
	case sc.Light.Base <= 0 || sc.Light.Amp < 0 || sc.Light.Amp >= sc.Light.Base || sc.Light.PeriodS < 0:
		return errors.New("light: need base > amp >= 0 and period_s >= 0")
	case len(sc.Objects) > maxObjects:
		return fmt.Errorf("at most %d objects", maxObjects)
	}
	for i, l := range sc.Lamps {
		if l.OnS < 0 || (l.OffS != 0 && l.OffS <= l.OnS) || l.Gain <= 0 || math.Abs(l.Gradient) > 1 {
			return fmt.Errorf("lamp %d: need on_s >= 0, off_s = 0 or > on_s, gain > 0, |gradient| <= 1", i)
		}
	}
	seen := map[string]bool{}
	for _, o := range sc.Objects {
		if o.ID == "" || seen[o.ID] {
			return fmt.Errorf("object id %q is empty or repeated", o.ID)
		}
		seen[o.ID] = true
		if o.W <= 0 || o.W > 1 || o.H <= 0 || o.H > 1 || o.Albedo < 0 || o.Albedo > 1 || len(o.Path) < 2 {
			return fmt.Errorf("object %s: need w, h in (0, 1], albedo in [0, 1], at least 2 path points", o.ID)
		}
		for i := 1; i < len(o.Path); i++ {
			if o.Path[i][0] <= o.Path[i-1][0] {
				return fmt.Errorf("object %s: path times must increase", o.ID)
			}
		}
	}
	return nil
}

// Duration — момент последнего события сценария (конец пути, включение или выключение лампы), с.
func (sc *Scenario) Duration() float64 {
	d := 0.0
	for _, l := range sc.Lamps {
		d = max(d, l.OnS, l.OffS)
	}
	for _, o := range sc.Objects {
		d = max(d, o.Path[len(o.Path)-1][0])
	}
	return d
}

// position — центр объекта в момент t (доли кадра); ok = false вне пути.
func (o *Object) position(t float64) (x, y float64, ok bool) {
	p := o.Path
	if t < p[0][0] || t > p[len(p)-1][0] {
		return 0, 0, false
	}
	i := 1
	for p[i][0] < t {
		i++
	}
	a, b := p[i-1], p[i]
	f := (t - a[0]) / (b[0] - a[0])
	return a[1] + f*(b[1]-a[1]), a[2] + f*(b[2]-a[2]), true
}

// ellipse — объект в пикселях кадра.
type ellipse struct {
	cx, cy, rx, ry float64
	albedo         float32
}

// span — столбцы [x0, x1] строки y, центры пикселей которых внутри эллипса (ok = false — нет таких).
func (e *ellipse) span(y, w int) (x0, x1 int, ok bool) {
	dy := (float64(y) + 0.5 - e.cy) / e.ry
	if dy <= -1 || dy >= 1 {
		return 0, 0, false
	}
	hw := e.rx * math.Sqrt(1-dy*dy)
	x0 = max(0, int(math.Ceil(e.cx-hw-0.5)))
	x1 = min(w-1, int(math.Floor(e.cx+hw-0.5)))
	return x0, x1, x0 <= x1
}

// Scene — подготовленная сцена. Render не потокобезопасен: буферы строки и освещённости переиспользуются.
type Scene struct {
	sc     Scenario
	albedo []float32 // фон, W×H
	light  []float32 // освещённость текущего кадра по столбцам, уже ×255
	row    []float32 // альбедо текущей строки
	placed []ellipse // объекты текущего кадра
	pcg    *rand.PCG
	rng    *rand.Rand
}

// New готовит сцену: строит фон по seed.
func New(sc *Scenario) *Scene {
	pcg := rand.NewPCG(sc.Seed, 0)
	return &Scene{sc: *sc, albedo: background(sc), light: make([]float32, sc.Width), row: make([]float32, sc.Width),
		placed: make([]ellipse, 0, len(sc.Objects)), pcg: pcg, rng: rand.New(pcg)}
}

// Size — размер кадра.
func (s *Scene) Size() (w, h int) { return s.sc.Width, s.sc.Height }

// background строит альбедо фона: светлая стена, 6 прямоугольников (мебель, дверь) и узор из трёх
// синусоид с длиной волны 8–40 пикселей.
func background(sc *Scenario) []float32 {
	w, h := sc.Width, sc.Height
	rng := rand.New(rand.NewPCG(sc.Seed, 0xBAC6))
	a := make([]float64, w*h)
	for i := range a {
		a[i] = 0.5
	}
	for range 6 {
		x0, y0 := rng.IntN(w), rng.IntN(h)
		x1, y1 := min(w, x0+w/8+rng.IntN(w/3)), min(h, y0+h/8+rng.IntN(h/3))
		v := 0.3 + 0.5*rng.Float64()
		for y := y0; y < y1; y++ {
			for x := x0; x < x1; x++ {
				a[y*w+x] = v
			}
		}
	}
	var waves [3][3]float64 // kx, ky, фаза
	for i := range waves {
		dir, k := math.Pi*rng.Float64(), 2*math.Pi/(8+32*rng.Float64())
		waves[i] = [3]float64{k * math.Cos(dir), k * math.Sin(dir), 2 * math.Pi * rng.Float64()}
	}
	out := make([]float32, w*h)
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			v := a[y*w+x]
			for _, wv := range waves {
				v += 0.03 * math.Sin(wv[0]*float64(x)+wv[1]*float64(y)+wv[2])
			}
			out[y*w+x] = float32(min(0.95, max(0.1, v)))
		}
	}
	return out
}

// lampOn — горит ли лампа в момент t.
func (l *Lamp) lampOn(t float64) bool { return t >= l.OnS && (l.OffS == 0 || t < l.OffS) }

// illuminate заполняет освещённость столбцов кадра в момент t.
func (s *Scene) illuminate(t float64) {
	g := s.sc.Light.Base
	if p := s.sc.Light.PeriodS; p > 0 {
		g += s.sc.Light.Amp * math.Cos(2*math.Pi*(t+s.sc.Light.PhaseS)/p)
	}
	w := float64(s.sc.Width)
	for x := range s.light {
		l := g
		for i := range s.sc.Lamps {
			if lp := &s.sc.Lamps[i]; lp.lampOn(t) {
				l += lp.Gain * (1 + lp.Gradient*(2*(float64(x)+0.5)/w-1))
			}
		}
		s.light[x] = float32(255 * l)
	}
}

// place заполняет s.placed объектами, существующими в момент t, со сдвигом кадра (dx, dy).
func (s *Scene) place(t, dx, dy float64) {
	s.placed = s.placed[:0]
	w, h := float64(s.sc.Width), float64(s.sc.Height)
	for i := range s.sc.Objects {
		o := &s.sc.Objects[i]
		if x, y, ok := o.position(t); ok {
			s.placed = append(s.placed, ellipse{cx: x*w - dx, cy: y*h - dy, rx: o.W * w / 2, ry: o.H * h / 2,
				albedo: float32(o.Albedo)})
		}
	}
}

// Render рисует кадр момента t (секунды сценария) в dst размера сцены; память не выделяет.
func (s *Scene) Render(t float64, dst *image.Gray) {
	w, h := s.sc.Width, s.sc.Height
	s.pcg.Seed(s.sc.Seed, uint64(math.Round(t*1000)))
	j := s.sc.JitterPx
	dx, dy := j*(2*s.rng.Float64()-1), j*(2*s.rng.Float64()-1)
	s.illuminate(t)
	s.place(t, dx, dy)
	noise := float32(s.sc.Noise)
	for y := 0; y < h; y++ {
		s.sampleRow(y, dx, dy)
		for i := range s.placed {
			if x0, x1, ok := s.placed[i].span(y, w); ok {
				for x := x0; x <= x1; x++ {
					s.row[x] = s.placed[i].albedo
				}
			}
		}
		out := dst.Pix[y*dst.Stride : y*dst.Stride+w]
		for x := range out {
			v := s.row[x] * s.light[x]
			if noise > 0 {
				v += noise * float32(s.rng.NormFloat64())
			}
			out[x] = uint8(min(255, max(0, v+0.5)))
		}
	}
}

// sampleRow — строка y фона, сдвинутого на (dx, dy), с билинейной интерполяцией.
func (s *Scene) sampleRow(y int, dx, dy float64) {
	w, h := s.sc.Width, s.sc.Height
	sy := min(float64(h-1), max(0, float64(y)+dy))
	y0 := int(sy)
	y1, fy := min(y0+1, h-1), float32(sy-float64(y0))
	r0, r1 := s.albedo[y0*w:y0*w+w], s.albedo[y1*w:y1*w+w]
	for x := range s.row {
		sx := min(float64(w-1), max(0, float64(x)+dx))
		x0 := int(sx)
		x1, fx := min(x0+1, w-1), float32(sx-float64(x0))
		top := r0[x0] + fx*(r0[x1]-r0[x0])
		bot := r1[x0] + fx*(r1[x1]-r1[x0])
		s.row[x] = top + fy*(bot-top)
	}
}

// Visible — объект в кадре: доля площади кадра и рамка видимой части (доли кадра x0, y0, x1, y1).
type Visible struct {
	ID   string
	Area float64
	Box  [4]float64
}

// Truth — объекты, видимые в кадре в момент t (без дрожания камеры): истина для проверки детектора.
func (s *Scene) Truth(t float64) []Visible {
	w, h := s.sc.Width, s.sc.Height
	var out []Visible
	for i := range s.sc.Objects {
		o := &s.sc.Objects[i]
		x, y, ok := o.position(t)
		if !ok {
			continue
		}
		e := ellipse{cx: x * float64(w), cy: y * float64(h), rx: o.W * float64(w) / 2, ry: o.H * float64(h) / 2}
		n, bx0, by0, bx1, by1 := 0, w, h, -1, -1
		for row := 0; row < h; row++ {
			if x0, x1, ok := e.span(row, w); ok {
				n += x1 - x0 + 1
				bx0, bx1 = min(bx0, x0), max(bx1, x1)
				by0, by1 = min(by0, row), max(by1, row)
			}
		}
		if n > 0 {
			out = append(out, Visible{ID: o.ID, Area: float64(n) / float64(w*h), Box: [4]float64{
				float64(bx0) / float64(w), float64(by0) / float64(h), float64(bx1+1) / float64(w), float64(by1+1) / float64(h)}})
		}
	}
	return out
}
