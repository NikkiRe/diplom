package luma

import (
	"image"
	"image/color"
	"testing"
)

func gray(w, h int, f func(x, y int) uint8) *image.Gray {
	g := image.NewGray(image.Rect(0, 0, w, h))
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			g.Pix[y*g.Stride+x] = f(x, y)
		}
	}
	return g
}

func TestResizeAveragesBlocks(t *testing.T) {
	src := gray(4, 2, func(x, y int) uint8 { return uint8(10*x + 100*y) }) // 0 10 20 30 / 100 110 120 130
	r, err := NewResizer(4, 2, 2, 1)
	if err != nil {
		t.Fatal(err)
	}
	dst := image.NewGray(image.Rect(0, 0, 2, 1))
	r.Resize(dst, src)
	if dst.Pix[0] != 55 || dst.Pix[1] != 75 { // (0+10+100+110)/4, (20+30+120+130)/4
		t.Fatal(dst.Pix)
	}
	// нецелый коэффициент 5 -> 3: блоки столбцов [0], [1,2], [3,4]
	src = gray(5, 1, func(x, y int) uint8 { return uint8(x * 10) })
	r, _ = NewResizer(5, 1, 3, 1)
	dst = image.NewGray(image.Rect(0, 0, 3, 1))
	r.Resize(dst, src)
	if dst.Pix[0] != 0 || dst.Pix[1] != 15 || dst.Pix[2] != 35 {
		t.Fatal(dst.Pix)
	}
}

func TestResizeKeepsFlatFieldAndDoesNotAllocate(t *testing.T) {
	src := gray(320, 240, func(x, y int) uint8 { return 77 })
	r, _ := NewResizer(320, 240, 160, 120)
	dst := image.NewGray(image.Rect(0, 0, 160, 120))
	if n := testing.AllocsPerRun(10, func() { r.Resize(dst, src) }); n != 0 {
		t.Fatalf("%v allocs", n)
	}
	for _, v := range dst.Pix {
		if v != 77 {
			t.Fatal(v)
		}
	}
	for _, bad := range [][4]int{{4, 4, 5, 4}, {4, 4, 0, 2}, {4, 4, 2, 8}} {
		if _, err := NewResizer(bad[0], bad[1], bad[2], bad[3]); err == nil {
			t.Fatalf("%v accepted", bad)
		}
	}
}

func TestFromYUYV(t *testing.T) {
	// 2×2, stride 6 (2 байта выравнивания): Y0 U Y1 V pad pad
	src := []byte{10, 128, 20, 128, 0xEE, 0xEE, 30, 128, 40, 128, 0xEE, 0xEE}
	dst := image.NewGray(image.Rect(0, 0, 2, 2))
	if err := FromYUYV(dst, src, 6); err != nil {
		t.Fatal(err)
	}
	if want := []byte{10, 20, 30, 40}; string(dst.Pix) != string(want) {
		t.Fatal(dst.Pix)
	}
	if n := testing.AllocsPerRun(10, func() { _ = FromYUYV(dst, src, 6) }); n != 0 {
		t.Fatalf("%v allocs", n)
	}
	if err := FromYUYV(dst, src[:9], 6); err == nil {
		t.Fatal("short buffer accepted")
	}
	if err := FromYUYV(dst, src, 3); err == nil {
		t.Fatal("stride below 2*width accepted")
	}
}

func TestCopy(t *testing.T) {
	dst := image.NewGray(image.Rect(0, 0, 3, 2))
	yc := image.NewYCbCr(image.Rect(0, 0, 3, 2), image.YCbCrSubsampleRatio420)
	for i := range yc.Y {
		yc.Y[i] = uint8(i + 1)
	}
	if err := Copy(dst, yc); err != nil || string(dst.Pix) != string(yc.Y[:6]) {
		t.Fatal(err, dst.Pix)
	}
	g := gray(3, 2, func(x, y int) uint8 { return uint8(50 + x + 10*y) })
	if err := Copy(dst, g); err != nil || string(dst.Pix) != string(g.Pix) {
		t.Fatal(err, dst.Pix)
	}
	rgba := image.NewRGBA(image.Rect(0, 0, 3, 2))
	rgba.Set(1, 1, color.RGBA{255, 0, 0, 255})
	if err := Copy(dst, rgba); err != nil || dst.Pix[4] != 76 || dst.Pix[0] != 0 { // 0.299·255
		t.Fatal(err, dst.Pix)
	}
	if err := Copy(dst, image.NewGray(image.Rect(0, 0, 2, 2))); err == nil {
		t.Fatal("size mismatch accepted")
	}
}
