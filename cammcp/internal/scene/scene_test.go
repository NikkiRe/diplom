package scene

import (
	"image"
	"math"
	"testing"
)

func render(s *Scene, t float64) *image.Gray {
	w, h := s.Size()
	g := image.NewGray(image.Rect(0, 0, w, h))
	s.Render(t, g)
	return g
}

func mean(g *image.Gray, box [4]float64) float64 {
	w, h := float64(g.Rect.Dx()), float64(g.Rect.Dy())
	sum, n := 0.0, 0
	for y := int(box[1] * h); y < int(box[3]*h); y++ {
		for x := int(box[0] * w); x < int(box[2]*w); x++ {
			sum += float64(g.Pix[y*g.Stride+x])
			n++
		}
	}
	return sum / float64(n)
}

var whole = [4]float64{0, 0, 1, 1}

func TestDemoParses(t *testing.T) {
	sc := Demo()
	if sc.Width != 320 || len(sc.Objects) != 4 || sc.Duration() != 220 {
		t.Fatalf("%+v", sc)
	}
}

func TestParseRejects(t *testing.T) {
	for name, js := range map[string]string{
		"unknown":    `{"width":64,"height":48,"light":{"base":0.5},"extra":1}`,
		"size":       `{"width":16,"height":48,"light":{"base":0.5}}`,
		"dark":       `{"width":64,"height":48,"light":{"base":0.2,"amp":0.3,"period_s":10}}`,
		"lamp":       `{"width":64,"height":48,"light":{"base":0.5},"lamps":[{"on_s":5,"off_s":5,"gain":0.2}]}`,
		"path":       `{"width":64,"height":48,"light":{"base":0.5},"objects":[{"id":"a","w":0.1,"h":0.1,"path":[[1,0,0]]}]}`,
		"back":       `{"width":64,"height":48,"light":{"base":0.5},"objects":[{"id":"a","w":0.1,"h":0.1,"path":[[2,0,0],[1,1,1]]}]}`,
		"repeated":   `{"width":64,"height":48,"light":{"base":0.5},"objects":[{"id":"a","w":0.1,"h":0.1,"path":[[1,0,0],[2,1,1]]},{"id":"a","w":0.1,"h":0.1,"path":[[1,0,0],[2,1,1]]}]}`,
		"not json":   `{`,
		"big object": `{"width":64,"height":48,"light":{"base":0.5},"objects":[{"id":"a","w":1.5,"h":0.1,"path":[[1,0,0],[2,1,1]]}]}`,
	} {
		if _, err := Parse([]byte(js)); err == nil {
			t.Errorf("%s accepted", name)
		}
	}
	if _, err := Load("/nonexistent/scene.json"); err == nil {
		t.Fatal("missing file accepted")
	}
}

// Кадр определяется только seed и временем: другой экземпляр сцены и другой порядок вызовов дают те же байты.
func TestRenderDeterministic(t *testing.T) {
	a, b := New(Demo()), New(Demo())
	f20 := render(a, 20)
	render(a, 5)
	if string(render(a, 20).Pix) != string(f20.Pix) || string(render(b, 20).Pix) != string(f20.Pix) {
		t.Fatal("frame depends on call order or instance")
	}
	if string(render(a, 20.1).Pix) == string(f20.Pix) {
		t.Fatal("noise does not change between frames")
	}
	g := image.NewGray(image.Rect(0, 0, 320, 240))
	if n := testing.AllocsPerRun(5, func() { a.Render(21, g) }); n != 0 {
		t.Fatalf("%v allocs per frame", n)
	}
}

// Лампа включается ступенькой: средняя яркость кадра растёт в (L+gain)/L раз за один кадр.
func TestLampStep(t *testing.T) {
	sc := Demo()
	s := New(sc)
	before, after := mean(render(s, 69.9), whole), mean(render(s, 70.1), whole)
	l := sc.Light.Base + sc.Light.Amp*math.Cos(2*math.Pi*70/sc.Light.PeriodS)
	if ratio, want := after/before, (l+0.3)/l; math.Abs(ratio-want) > 0.03 {
		t.Fatalf("lamp step ratio %.3f, want %.3f", ratio, want)
	}
	// неравномерность: справа лампа ярче (gradient 0.1)
	off, on := render(s, 69.9), render(s, 70.1)
	gainL := mean(on, [4]float64{0, 0, 0.2, 1}) / mean(off, [4]float64{0, 0, 0.2, 1})
	gainR := mean(on, [4]float64{0.8, 0, 1, 1}) / mean(off, [4]float64{0.8, 0, 1, 1})
	if gainR <= gainL {
		t.Fatalf("gradient: left %.3f right %.3f", gainL, gainR)
	}
}

// Истина: объект виден, пока его эллипс пересекает кадр; на месте тёмного объекта кадр темнее фона.
func TestTruthAndObjects(t *testing.T) {
	s := New(Demo())
	if v := s.Truth(10); len(v) != 0 {
		t.Fatalf("nothing expected at 10 s: %v", v)
	}
	v := s.Truth(21)
	if len(v) != 1 || v[0].ID != "person_walk" {
		t.Fatalf("%v", v)
	}
	if want := math.Pi / 4 * 0.12 * 0.5; math.Abs(v[0].Area-want) > 0.003 {
		t.Fatalf("area %.4f, want %.4f", v[0].Area, want)
	}
	if bg, obj := mean(render(s, 10), v[0].Box), mean(render(s, 21), v[0].Box); obj > bg-15 {
		t.Fatalf("dark object not visible: background %.1f, object %.1f", bg, obj)
	}
	edge := s.Truth(15.6) // входит слева: видна только часть
	if len(edge) != 1 || edge[0].Box[0] != 0 || edge[0].Area >= v[0].Area {
		t.Fatalf("partly visible object: %v", edge)
	}
	if v := s.Truth(219.99); len(v) != 0 {
		t.Fatalf("object outside the frame is visible: %v", v)
	}
}

func TestDemoNoiseSigma(t *testing.T) {
	sc := Demo()
	sc.JitterPx = 0
	s := New(sc)
	a, b := render(s, 1), render(s, 1.001)
	var sum2 float64
	for i := range a.Pix {
		d := float64(a.Pix[i]) - float64(b.Pix[i])
		sum2 += d * d
	}
	// разность двух кадров: σ·√2
	if sigma := math.Sqrt(sum2/float64(len(a.Pix))) / math.Sqrt2; math.Abs(sigma-sc.Noise) > 0.3 {
		t.Fatalf("noise sigma %.2f, want %.1f", sigma, sc.Noise)
	}
}
