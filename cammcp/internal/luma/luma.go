// Package luma — операции над кадром яркости (*image.Gray, 8 бит, начало в (0,0)).
//
// Весь конвейер cam-mcp работает только с яркостью: для обнаружения движения цвет не нужен, YUYV
// отдаёт её без преобразования (каждый второй байт), а JPEG — готовой плоскостью Y. Снимок для LLM —
// тоже оттенки серого: вдвое меньше байтов при той же пользе для проверки «что в кадре».
package luma

import (
	"fmt"
	"image"
	"image/color"
)

// Resizer уменьшает кадр усреднением по площади (box filter) в произвольное, в том числе нецелое,
// число раз: пиксель приёмника — среднее блока источника между соседними границами таблиц xb, yb.
// Строки блока сначала складываются в аккумулятор ширины источника (простой проход без ветвлений),
// затем аккумулятор сворачивается по столбцам. Таблицы и аккумулятор готовятся один раз на пару
// размеров, поэтому Resize не выделяет память.
type Resizer struct {
	dw, dh int
	xb, yb []int
	acc    []uint32 // сумма строк блока, ширина источника
}

// NewResizer готовит уменьшение sw×sh → dw×dh (0 < dw ≤ sw, 0 < dh ≤ sh).
func NewResizer(sw, sh, dw, dh int) (*Resizer, error) {
	if dw <= 0 || dh <= 0 || dw > sw || dh > sh {
		return nil, fmt.Errorf("resize %dx%d -> %dx%d: only shrinking to a non-empty frame", sw, sh, dw, dh)
	}
	return &Resizer{dw: dw, dh: dh, xb: bounds(sw, dw), yb: bounds(sh, dh), acc: make([]uint32, sw)}, nil
}

func bounds(src, dst int) []int {
	b := make([]int, dst+1)
	for i := range b {
		b[i] = i * src / dst
	}
	return b
}

// Resize пишет в dst (dw×dh) уменьшенный src (sw×sh).
func (r *Resizer) Resize(dst, src *image.Gray) {
	acc := r.acc
	for y := 0; y < r.dh; y++ {
		clear(acc)
		for sy := r.yb[y]; sy < r.yb[y+1]; sy++ {
			row := src.Pix[sy*src.Stride : sy*src.Stride+len(acc)]
			for x, v := range row {
				acc[x] += uint32(v)
			}
		}
		rows := uint32(r.yb[y+1] - r.yb[y])
		out := dst.Pix[y*dst.Stride : y*dst.Stride+r.dw]
		for x := range out {
			s := uint32(0)
			for _, v := range acc[r.xb[x]:r.xb[x+1]] {
				s += v
			}
			n := uint32(r.xb[x+1]-r.xb[x]) * rows
			out[x] = uint8((s + n/2) / n)
		}
	}
}

// FromYUYV извлекает яркость из кадра YUYV 4:2:2 (байты Y0 U Y1 V; stride — байт на строку), без
// выделения памяти. Размер кадра задаёт dst.
func FromYUYV(dst *image.Gray, src []byte, stride int) error {
	w, h := dst.Rect.Dx(), dst.Rect.Dy()
	if stride < 2*w || len(src) < stride*(h-1)+2*w {
		return fmt.Errorf("yuyv %dx%d: %d bytes with stride %d are too few", w, h, len(src), stride)
	}
	for y := 0; y < h; y++ {
		row := src[y*stride : y*stride+2*w]
		out := dst.Pix[y*dst.Stride : y*dst.Stride+w]
		for x := range out {
			out[x] = row[2*x]
		}
	}
	return nil
}

// Copy переносит яркость изображения img того же размера в dst: плоскость Y у JPEG (YCbCr), строки
// у оттенков серого, иначе — преобразование цвета по Rec. 601 (color.GrayModel).
func Copy(dst *image.Gray, img image.Image) error {
	b := img.Bounds()
	w, h := dst.Rect.Dx(), dst.Rect.Dy()
	if b.Dx() != w || b.Dy() != h {
		return fmt.Errorf("frame %dx%d, expected %dx%d", b.Dx(), b.Dy(), w, h)
	}
	switch m := img.(type) {
	case *image.YCbCr:
		copyPlane(dst, m.Y, m.YStride, m.YOffset(b.Min.X, b.Min.Y))
	case *image.Gray:
		copyPlane(dst, m.Pix, m.Stride, m.PixOffset(b.Min.X, b.Min.Y))
	default:
		for y := 0; y < h; y++ {
			for x := 0; x < w; x++ {
				dst.Pix[y*dst.Stride+x] = color.GrayModel.Convert(img.At(b.Min.X+x, b.Min.Y+y)).(color.Gray).Y
			}
		}
	}
	return nil
}

func copyPlane(dst *image.Gray, src []byte, stride, off int) {
	w := dst.Rect.Dx()
	for y := 0; y < dst.Rect.Dy(); y++ {
		copy(dst.Pix[y*dst.Stride:y*dst.Stride+w], src[off+y*stride:off+y*stride+w])
	}
}
