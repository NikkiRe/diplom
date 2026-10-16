// Package detect — детектор движения на плате без ML: вычитание фона в уменьшенном кадре яркости.
//
// Конвейер кадра (все буферы выделяются в New, Process память не выделяет):
//  1. уменьшение до W×H усреднением по площади (по умолчанию 160×120);
//  2. компенсация общей яркости: усиление g — медиана отношений «кадр / фон» по пикселям (гистограмма
//     log2 с шагом 1/64, без пересвеченных и слишком тёмных). Включение лампы и автоэкспозиция меняют
//     яркость почти мультипликативно (в том числе после гамма-кривой камеры), а медиана устойчива,
//     пока движущиеся области занимают меньше половины кадра;
//  3. маска движения: |кадр − g·фон| > Threshold. Пересвет датчика (яркость у 255) учитывается: фон
//     хранит оценку истинной яркости, и светлая поверхность, пересвеченная под лампой, не даёт тревоги
//     ни при включении, ни при выключении лампы (см. subtract);
//  4. фон — скользящее среднее с постоянной времени BgTau секунд (коэффициент считается по
//     фактическому интервалу кадров, поэтому не зависит от адаптивной частоты; фон в Q16.16 с
//     округлением — без смещения и застревания при долгой BgTau); под маской фон обновляется в 8 раз
//     медленнее, чтобы медленный объект не растворялся в фоне;
//  5. если маска покрывает больше MaxArea кадра — глобальное изменение (свет, сдвиг камеры): фон
//     переучивается по кадру, кадр не считается активным;
//  6. морфологическое открытие 3×3 (эрозия, затем наращивание) убирает шум и тонкие края от
//     дрожания камеры;
//  7. связные области (8-связность), меньше MinArea кадра отбрасываются; кадр активен, если
//     осталась хоть одна;
//  8. гистерезис по времени: эпизод начинается, когда активны не меньше K из N последних кадров, и
//     кончается после N неактивных кадров подряд. Начало, рамка и пики эпизода собираются из активных
//     кадров этого окна, а не из более ранней редкой активности (мерцание), не набравшей K из N.
//
// Это вычитание фона, а не распознавание: детектор видит «что-то изменилось и держится», а не
// «человек». Объект, простоявший много минут, со временем станет фоном.
package detect

import (
	"errors"
	"fmt"
	"image"
	"math"
	"math/bits"

	"github.com/local/cammcp/internal/luma"
)

// Params — параметры детектора.
type Params struct {
	W, H      int     // разрешение обработки
	Threshold int     // порог |кадр − g·фон|, уровни яркости 0..255
	MinArea   float64 // минимальная площадь связной области, доля кадра
	MaxArea   float64 // доля изменившихся пикселей, выше которой изменение считается глобальным
	BgTau     float64 // постоянная времени фона, с
	K, N      int     // начало эпизода: ≥ K активных из N последних кадров; конец: N неактивных подряд
}

// DefaultParams — параметры по умолчанию (допущения, подобранные на синтетической сцене; на настоящей
// камере их нужно проверить).
func DefaultParams() Params {
	return Params{W: 160, H: 120, Threshold: 20, MinArea: 0.005, MaxArea: 0.6, BgTau: 30, K: 3, N: 5}
}

// Пределы параметров: история активности кадров хранится в битах uint32; при BgTau до часа и частоте
// до 30 кадров/с коэффициент фона под маской в Q24 не меньше 19 (ошибка квантования до 3 %).
const (
	maxN     = 32
	maxBgTau = 3600
)

// Validate проверяет согласованность параметров.
func (p Params) Validate() error {
	switch {
	case p.W < 16 || p.H < 12 || p.W > 640 || p.H > 480:
		return fmt.Errorf("detector size %dx%d outside 16x12..640x480", p.W, p.H)
	case p.Threshold < 1 || p.Threshold > 254:
		return errors.New("threshold must be in [1, 254]")
	case !(p.MinArea > 0) || !(p.MaxArea <= 1) || p.MinArea >= p.MaxArea:
		return errors.New("need 0 < min area < max area <= 1")
	case !(p.BgTau > 0) || p.BgTau > maxBgTau:
		return fmt.Errorf("background time constant must be in (0, %d] s", maxBgTau)
	case p.K < 1 || p.N < p.K || p.N > maxN:
		return fmt.Errorf("need 1 <= k <= n <= %d", maxN)
	}
	return nil
}

// Frame — итог обработки одного кадра.
type Frame struct {
	Active bool       // есть области движения не меньше MinArea
	Area   float64    // их суммарная площадь, доля кадра
	Box    [4]float64 // их общая рамка: x0, y0, x1, y1 в долях кадра
	Center [2]float64 // их центр масс, доли кадра
	Score  float64    // средняя |разность| в областях, в порогах (≥ 1): насколько движение выше порога
	Blobs  int        // число областей
	Gain   float64    // оценка общего изменения яркости относительно фона
	Reset  bool       // глобальное изменение: фон переучен по этому кадру
}

// Event — переход эпизода движения после кадра.
type Event int

// Переходы эпизода.
const (
	None Event = iota
	Start
	End
)

// Episode — эпизод движения.
type Episode struct {
	ID        int
	Start     float64    // время первого активного кадра окна, в котором набралось K из N
	Confirm   float64    // время подтверждения (≥ K из N): с этого момента эпизод сообщается
	End       float64    // время последнего активного кадра (пока эпизод идёт — последнего на сейчас)
	Frames    int        // число активных кадров
	PeakArea  float64    // наибольшая площадь, доля кадра
	PeakScore float64    // наибольшая оценка Score
	Box       [4]float64 // общая рамка за эпизод
	Center    [2]float64 // центр движения в кадре наибольшей площади
}

func (e *Episode) add(t float64, f *Frame) {
	if e.Frames == 0 {
		e.Start, e.Box = t, f.Box
	}
	e.Frames++
	e.End = t
	e.Box = [4]float64{min(e.Box[0], f.Box[0]), min(e.Box[1], f.Box[1]), max(e.Box[2], f.Box[2]), max(e.Box[3], f.Box[3])}
	if f.Area > e.PeakArea {
		e.PeakArea, e.Center = f.Area, f.Center
	}
	e.PeakScore = max(e.PeakScore, f.Score)
}

// Шкала гистограммы отношений яркости: log2(v+1) с шагом 1/logScale, отношения от 2^-8 до 2^8.
const (
	logScale = 64
	histMid  = 8 * logScale
	histBins = 2*histMid + 1
)

// Пиксели темнее dark или ярче bright (около насыщения датчика) не участвуют в оценке усиления;
// фон ярче bright считается пересвеченным.
const (
	dark   = 8
	bright = 247
)

var (
	logLUT  [256]int        // round(logScale · log2(v+1))
	gainLUT [histBins]int64 // усиление корзины в Q16
)

func init() {
	for v := range logLUT {
		logLUT[v] = int(math.Round(logScale * math.Log2(float64(v+1))))
	}
	for b := range gainLUT {
		gainLUT[b] = int64(math.Round(65536 * math.Exp2(float64(b-histMid)/logScale)))
	}
}

// Detector — детектор движения. Не потокобезопасен: Process и SetParams вызываются по очереди.
type Detector struct {
	p     Params
	minPx int // MinArea в пикселях
	rs    *luma.Resizer
	cur   *image.Gray
	bg    []uint32 // фон, Q16.16
	mask  []uint8  // 0 — фон, 1 — движение, 2 — уже размечено при поиске областей
	tmp   []uint8
	diff  []uint8 // |кадр − g·фон|, для оценки областей
	stack []int32
	hist  [histBins]uint32
	ready bool
	lastT float64

	active uint32     // история активности: бит 0 — последний кадр
	frames uint       // номер кадра
	marks  [maxN]mark // итоги активных кадров по номеру кадра (кольцо): из них собирается эпизод
	inEp   bool
	ep     Episode // текущий эпизод, а между эпизодами — последний завершённый
	nextID int
}

// mark — итог активного кадра.
type mark struct {
	t float64
	f Frame
}

// New создаёт детектор для кадров источника srcW×srcH (не меньше W×H).
func New(p Params, srcW, srcH int) (*Detector, error) {
	if err := p.Validate(); err != nil {
		return nil, err
	}
	rs, err := luma.NewResizer(srcW, srcH, p.W, p.H)
	if err != nil {
		return nil, err
	}
	n := p.W * p.H
	d := &Detector{rs: rs, cur: image.NewGray(image.Rect(0, 0, p.W, p.H)), bg: make([]uint32, n),
		mask: make([]uint8, n), tmp: make([]uint8, n), diff: make([]uint8, n), stack: make([]int32, 0, n)}
	d.setParams(p)
	return d, nil
}

func (d *Detector) setParams(p Params) {
	d.p = p
	d.minPx = max(1, int(math.Ceil(p.MinArea*float64(p.W*p.H))))
}

// Params — текущие параметры.
func (d *Detector) Params() Params { return d.p }

// SetParams меняет параметры на ходу; размер обработки и постоянная фона не меняются.
func (d *Detector) SetParams(p Params) error {
	if p.W != d.p.W || p.H != d.p.H || p.BgTau != d.p.BgTau {
		return errors.New("detector size and background time constant are fixed at start")
	}
	if err := p.Validate(); err != nil {
		return err
	}
	d.setParams(p)
	return nil
}

// Hot — эпизод идёт или в последних N кадрах есть активные (кандидат): нужна высокая частота кадров.
func (d *Detector) Hot() bool { return d.inEp || d.window() != 0 }

// Episode — текущий эпизод (ok = true, если он идёт) либо последний завершённый.
func (d *Detector) Episode() (Episode, bool) { return d.ep, d.inEp }

func (d *Detector) window() uint32 { return d.active & (1<<d.p.N - 1) }

// Process обрабатывает кадр src (размер источника из New) момента t (секунды, по возрастанию).
func (d *Detector) Process(src *image.Gray, t float64) (Frame, Event) {
	d.rs.Resize(d.cur, src)
	if !d.ready {
		d.relearn()
		d.ready, d.lastT = true, t
		return Frame{Gain: 1}, d.track(t, &Frame{})
	}
	alpha := 1 - math.Exp(-(t-d.lastT)/d.p.BgTau)
	d.lastT = t
	g := d.gain()
	f := Frame{Gain: float64(g) / 65536}
	if moved := d.subtract(g, int64(math.Round(alpha*(1<<24)))); moved > int(d.p.MaxArea*float64(len(d.mask))) {
		d.relearn()
		f.Reset = true
	} else {
		d.open()
		d.regions(&f)
	}
	return f, d.track(t, &f)
}

// Flush — перерыв в кадрах (источник кончился или отказал): идущий эпизод завершается (End = время
// последнего активного кадра), история активности и фон забываются — первый кадр после перерыва
// переучит фон, ведь сцена за перерыв могла измениться.
func (d *Detector) Flush() Event {
	ev := None
	if d.inEp {
		ev = End
	}
	d.inEp, d.active, d.ready = false, 0, false
	return ev
}

// relearn — фон := текущий кадр.
func (d *Detector) relearn() {
	for i, v := range d.cur.Pix {
		d.bg[i] = uint32(v) << 16
	}
}

// gain — медиана отношения «кадр / фон» в Q16 (1 << 16, если надёжных пикселей меньше десятой части).
func (d *Detector) gain() int64 {
	clear(d.hist[:])
	n := 0
	for i, c := range d.cur.Pix {
		b := d.bg[i] >> 16
		if c < dark || c > bright || b < dark || b > bright {
			continue
		}
		d.hist[logLUT[c]-logLUT[b]+histMid]++
		n++
	}
	if n < len(d.cur.Pix)/10 {
		return 1 << 16
	}
	half, acc := uint32(n+1)/2, uint32(0)
	for b, c := range d.hist {
		if acc += c; acc >= half {
			return gainLUT[b]
		}
	}
	return 1 << 16
}

// subtract строит маску движения и разности и обновляет фон (alpha — в Q24, с округлением); возвращает
// число пикселей маски.
//
// Датчик не отдаёт больше 255, поэтому пиксель ярче bright — лишь нижняя оценка истинной яркости. Фон
// хранит оценку истинной яркости (в Q16.16 она может быть выше 255): пересвеченный кадр не темнее
// прогноза g·фон — не движение, и фон идёт к прогнозу, а не к 255, поэтому светлая поверхность под
// лампой светлеет и темнеет вместе с остальным кадром. Кадр светлее прогноза над пересвеченным фоном
// (запомненным при пересвете, например при старте под лампой) — тоже не движение: фон поднимается до
// кадр / g.
func (d *Detector) subtract(g, alpha int64) int {
	thr := int64(d.p.Threshold)
	slow := alpha / 8
	moved := 0
	for i, c := range d.cur.Pix {
		b, v := int64(d.bg[i]), int64(c)
		p := (b*g + 1<<31) >> 32 // прогноз, уровни
		target := v << 16
		switch {
		case v > bright && p >= v:
			target, p = min(p, 0xFFFF)<<16, v
		case b>>16 > bright && p < v:
			b, p = (v<<32+g/2)/g, v
		}
		r := v - p
		if r < 0 {
			r = -r
		}
		d.diff[i] = uint8(min(r, 255))
		a := alpha
		if r > thr {
			d.mask[i] = 1
			moved++
			a = slow
		} else {
			d.mask[i] = 0
		}
		d.bg[i] = uint32(b + ((target-b)*a+1<<23)>>24)
	}
	return moved
}

// open — морфологическое открытие маски окном 3×3 (раздельно по строкам и столбцам); за краем кадра — 0.
func (d *Detector) open() {
	w, h, m, t := d.p.W, d.p.H, d.mask, d.tmp
	for y := 0; y < h; y++ { // эрозия по строкам: m -> t
		r, o := m[y*w:(y+1)*w], t[y*w:(y+1)*w]
		o[0], o[w-1] = 0, 0
		for x := 1; x < w-1; x++ {
			o[x] = r[x-1] & r[x] & r[x+1]
		}
	}
	clear(m[:w]) // эрозия по столбцам: t -> m
	clear(m[(h-1)*w:])
	for i := w; i < (h-1)*w; i++ {
		m[i] = t[i-w] & t[i] & t[i+w]
	}
	for y := 0; y < h; y++ { // наращивание по строкам: m -> t
		r, o := m[y*w:(y+1)*w], t[y*w:(y+1)*w]
		o[0], o[w-1] = r[0]|r[1], r[w-2]|r[w-1]
		for x := 1; x < w-1; x++ {
			o[x] = r[x-1] | r[x] | r[x+1]
		}
	}
	for x := 0; x < w; x++ { // наращивание по столбцам: t -> m
		m[x] = t[x] | t[x+w]
		m[(h-1)*w+x] = t[(h-2)*w+x] | t[(h-1)*w+x]
	}
	for i := w; i < (h-1)*w; i++ {
		m[i] = t[i-w] | t[i] | t[i+w]
	}
}

// regions находит связные области маски (8-связность) не меньше minPx и сводит их в f.
func (d *Detector) regions(f *Frame) {
	w, h := d.p.W, d.p.H
	area, sumX, sumY, sumD := 0, 0, 0, 0
	bx0, by0, bx1, by1 := w, h, -1, -1
	for i, v := range d.mask {
		if v != 1 {
			continue
		}
		st := append(d.stack[:0], int32(i))
		d.mask[i] = 2
		n, sx, sy, sd := 0, 0, 0, 0
		x0, y0, x1, y1 := w, h, -1, -1
		for len(st) > 0 {
			j := int(st[len(st)-1])
			st = st[:len(st)-1]
			x, y := j%w, j/w
			n, sx, sy, sd = n+1, sx+x, sy+y, sd+int(d.diff[j])
			x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
			for ny := max(0, y-1); ny <= min(h-1, y+1); ny++ {
				for nx := max(0, x-1); nx <= min(w-1, x+1); nx++ {
					if k := ny*w + nx; d.mask[k] == 1 {
						d.mask[k] = 2
						st = append(st, int32(k))
					}
				}
			}
		}
		d.stack = st
		if n < d.minPx {
			continue
		}
		f.Blobs++
		area, sumX, sumY, sumD = area+n, sumX+sx, sumY+sy, sumD+sd
		bx0, by0, bx1, by1 = min(bx0, x0), min(by0, y0), max(bx1, x1), max(by1, y1)
	}
	if f.Blobs == 0 {
		return
	}
	fw, fh, fa := float64(w), float64(h), float64(area)
	f.Active = true
	f.Area = fa / (fw * fh)
	f.Box = [4]float64{float64(bx0) / fw, float64(by0) / fh, float64(bx1+1) / fw, float64(by1+1) / fh}
	f.Center = [2]float64{(float64(sumX)/fa + 0.5) / fw, (float64(sumY)/fa + 0.5) / fh}
	f.Score = float64(sumD) / fa / float64(d.p.Threshold)
}

// track — гистерезис k из n и накопление эпизода.
func (d *Detector) track(t float64, f *Frame) Event {
	d.active <<= 1
	d.frames++
	if f.Active {
		d.active |= 1
		d.marks[d.frames%maxN] = mark{t, *f}
	}
	if d.inEp {
		if f.Active {
			d.ep.add(t, f)
		}
		if d.window() == 0 {
			d.inEp = false
			return End
		}
		return None
	}
	if bits.OnesCount32(d.window()) < d.p.K {
		return None
	}
	d.nextID++
	d.ep = Episode{ID: d.nextID, Confirm: t}
	for i := d.p.N - 1; i >= 0; i-- { // активные кадры окна, от старых к новым
		if d.active>>i&1 == 1 {
			m := &d.marks[(d.frames-uint(i))%maxN]
			d.ep.add(m.t, &m.f)
		}
	}
	d.inEp = true
	return Start
}
