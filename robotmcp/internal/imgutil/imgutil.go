// Package imgutil — уменьшение кадра JPEG на плате перед отдачей в LLM (кадр 640×480 → 320×240 q70,
// ≈4,6 КБ и 96–130 vision-токенов вместо 13–18 КБ и 360–486; docs/robot_sim.md §12.5).
//
// Уменьшение — в целое число раз k = ⌈W/maxW⌉ усреднением блоков k×k прямо на плоскостях YCbCr: без
// перевода в RGBA и без сторонних пакетов (для 640→320 это точное усреднение 2×2, по качеству не хуже
// бикубики Pillow в прототипе и в разы дешевле по CPU).
package imgutil

import (
	"bytes"
	"fmt"
	"image"
	"image/color"
	"image/jpeg"
)

// Shrink уменьшает JPEG до ширины не больше maxW (0 — не уменьшать) и пережимает с качеством quality.
// Кадр уже не шире maxW возвращается как есть (без пережатия).
func Shrink(src []byte, maxW, quality int) (out []byte, w, h int, err error) {
	img, err := jpeg.Decode(bytes.NewReader(src))
	if err != nil {
		return nil, 0, 0, fmt.Errorf("jpeg decode: %w", err)
	}
	b := img.Bounds()
	if maxW <= 0 || b.Dx() <= maxW {
		return src, b.Dx(), b.Dy(), nil
	}
	k := (b.Dx() + maxW - 1) / maxW
	dst := Downscale(img, k)
	var buf bytes.Buffer
	if err := jpeg.Encode(&buf, dst, &jpeg.Options{Quality: quality}); err != nil {
		return nil, 0, 0, err
	}
	db := dst.Bounds()
	return buf.Bytes(), db.Dx(), db.Dy(), nil
}

// Downscale уменьшает изображение в k раз усреднением блоков k×k (остаток по краям отбрасывается).
func Downscale(img image.Image, k int) image.Image {
	if k <= 1 {
		return img
	}
	b := img.Bounds()
	w, h := b.Dx()/k, b.Dy()/k
	if w < 1 || h < 1 {
		return img
	}
	switch m := img.(type) {
	case *image.YCbCr:
		dst := image.NewYCbCr(image.Rect(0, 0, w, h), m.SubsampleRatio)
		boxPlane(dst.Y, dst.YStride, w, h, m.Y, m.YStride, k)
		cw, ch := chromaSize(w, h, m.SubsampleRatio)
		boxPlane(dst.Cb, dst.CStride, cw, ch, m.Cb, m.CStride, k)
		boxPlane(dst.Cr, dst.CStride, cw, ch, m.Cr, m.CStride, k)
		return dst
	case *image.Gray:
		dst := image.NewGray(image.Rect(0, 0, w, h))
		boxPlane(dst.Pix, dst.Stride, w, h, m.Pix, m.Stride, k)
		return dst
	}
	// общий случай (на практике не встречается: камера отдаёт JPEG YCbCr)
	dst := image.NewRGBA(image.Rect(0, 0, w, h))
	n := uint32(k * k)
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			var r, g, bl, a uint32
			for dy := 0; dy < k; dy++ {
				for dx := 0; dx < k; dx++ {
					cr, cg, cb, ca := img.At(b.Min.X+x*k+dx, b.Min.Y+y*k+dy).RGBA()
					r, g, bl, a = r+cr, g+cg, bl+cb, a+ca
				}
			}
			dst.Set(x, y, color.RGBA64{uint16(r / n), uint16(g / n), uint16(bl / n), uint16(a / n)})
		}
	}
	return dst
}

func chromaSize(w, h int, r image.YCbCrSubsampleRatio) (int, int) {
	switch r {
	case image.YCbCrSubsampleRatio422:
		return (w + 1) / 2, h
	case image.YCbCrSubsampleRatio420:
		return (w + 1) / 2, (h + 1) / 2
	case image.YCbCrSubsampleRatio440:
		return w, (h + 1) / 2
	case image.YCbCrSubsampleRatio411:
		return (w + 3) / 4, h
	case image.YCbCrSubsampleRatio410:
		return (w + 3) / 4, (h + 1) / 2
	}
	return w, h
}

// boxPlane: dst[w×h] = среднее блоков k×k src (src не меньше w·k × h·k, иначе край повторяется).
func boxPlane(dst []byte, dstStride, w, h int, src []byte, srcStride, k int) {
	srcH := len(src) / srcStride
	n := k * k
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			sum := 0
			for dy := 0; dy < k; dy++ {
				sy := y*k + dy
				if sy >= srcH {
					sy = srcH - 1
				}
				row := src[sy*srcStride:]
				for dx := 0; dx < k; dx++ {
					sx := x*k + dx
					if sx >= srcStride {
						sx = srcStride - 1
					}
					sum += int(row[sx])
				}
			}
			dst[y*dstStride+x] = byte((sum + n/2) / n)
		}
	}
}

// JPEGSize — ширина и высота JPEG по маркеру SOF без декодирования (0, 0 — не JPEG).
func JPEGSize(b []byte) (int, int) {
	cfg, err := jpeg.DecodeConfig(bytes.NewReader(b))
	if err != nil {
		return 0, 0
	}
	return cfg.Width, cfg.Height
}

// SyntheticJPEG — синтетический кадр w×h (градиент и вертикальные полосы), JPEG с качеством q: кадр
// ленивой камеры для фейкового rosbridge и тестов (640×480 q80 ≈ 15 КБ — порядок кадра стенда).
func SyntheticJPEG(w, h, q int) []byte {
	img := image.NewYCbCr(image.Rect(0, 0, w, h), image.YCbCrSubsampleRatio420)
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			yy, cb, cr := color.RGBToYCbCr(uint8(x*255/w), uint8(y*255/h), uint8((x/40%2)*120+(x*y)%37))
			img.Y[img.YOffset(x, y)] = yy
			ci := img.COffset(x, y)
			img.Cb[ci], img.Cr[ci] = cb, cr
		}
	}
	var buf bytes.Buffer
	_ = jpeg.Encode(&buf, img, &jpeg.Options{Quality: q})
	return buf.Bytes()
}
