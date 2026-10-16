package imgutil

import (
	"bytes"
	"image"
	"image/color"
	"image/jpeg"
	"math"
	"testing"
)

func TestShrink(t *testing.T) {
	src := SyntheticJPEG(640, 480, 80)
	out, w, h, err := Shrink(src, 320, 70)
	if err != nil {
		t.Fatal(err)
	}
	if w != 320 || h != 240 {
		t.Fatalf("%dx%d", w, h)
	}
	if ww, hh := JPEGSize(out); ww != 320 || hh != 240 {
		t.Fatalf("JPEGSize %dx%d", ww, hh)
	}
	if len(out) >= len(src) {
		t.Fatalf("not smaller: %d >= %d", len(out), len(src))
	}
	// содержимое сохранилось: средний цвет кадров близок
	a, _ := jpeg.Decode(bytes.NewReader(src))
	b, _ := jpeg.Decode(bytes.NewReader(out))
	if d := meanDiff(a, b); d > 6 {
		t.Fatalf("mean color diff %.1f", d)
	}
	same, w2, _, err := Shrink(out, 320, 70)
	if err != nil || w2 != 320 || !bytes.Equal(same, out) {
		t.Fatal("frame of target width must be returned as is")
	}
	if _, _, _, err := Shrink([]byte("nope"), 320, 70); err == nil {
		t.Fatal("garbage accepted")
	}
}

func meanDiff(a, b image.Image) float64 {
	mean := func(m image.Image) [3]float64 {
		var s [3]float64
		bb := m.Bounds()
		n := 0.0
		for y := bb.Min.Y; y < bb.Max.Y; y += 2 {
			for x := bb.Min.X; x < bb.Max.X; x += 2 {
				r, g, b, _ := m.At(x, y).RGBA()
				s[0] += float64(r >> 8)
				s[1] += float64(g >> 8)
				s[2] += float64(b >> 8)
				n++
			}
		}
		return [3]float64{s[0] / n, s[1] / n, s[2] / n}
	}
	ma, mb := mean(a), mean(b)
	return math.Abs(ma[0]-mb[0]) + math.Abs(ma[1]-mb[1]) + math.Abs(ma[2]-mb[2])
}

func TestDownscaleGrayAndGeneric(t *testing.T) {
	g := image.NewGray(image.Rect(0, 0, 5, 3))
	for i := range g.Pix {
		g.Pix[i] = 100
	}
	d := Downscale(g, 2).(*image.Gray)
	if d.Bounds().Dx() != 2 || d.Bounds().Dy() != 1 || d.Pix[0] != 100 {
		t.Fatal(d.Bounds(), d.Pix)
	}
	rgba := image.NewRGBA(image.Rect(0, 0, 4, 4))
	for i := range rgba.Pix {
		rgba.Pix[i] = 200
	}
	r := Downscale(rgba, 2)
	if c := color.RGBAModel.Convert(r.At(1, 1)).(color.RGBA); c.R != 200 {
		t.Fatal(c)
	}
}

func BenchmarkShrink640(b *testing.B) {
	src := SyntheticJPEG(640, 480, 80)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, _, _, err := Shrink(src, 320, 70); err != nil {
			b.Fatal(err)
		}
	}
}
