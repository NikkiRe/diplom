package source

import (
	"bytes"
	"errors"
	"fmt"
	"image"
	"image/color"
	"image/jpeg"
	"slices"
	"strings"

	"github.com/local/cammcp/internal/luma"
)

// Платформенно-независимая часть источника V4L2: форматы пикселей и перевод кадра в яркость
// (проверяется тестами на любой ОС; ioctl и mmap — в v4l2_linux.go).

// Форматы пикселей V4L2 (fourcc, videodev2.h).
const (
	pixYUYV = 'Y' | 'U'<<8 | 'Y'<<16 | 'V'<<24
	pixMJPG = 'M' | 'J'<<8 | 'P'<<16 | 'G'<<24
	pixJPEG = 'J' | 'P'<<8 | 'E'<<16 | 'G'<<24
)

// fourcc — запись формата буквами («YUYV»); старший бит — признак big-endian («Y16 -BE»).
func fourcc(v uint32) string {
	s := string([]byte{byte(v), byte(v >> 8), byte(v >> 16), byte(v>>24) & 0x7F})
	if v&(1<<31) != 0 {
		s += "-BE"
	}
	return s
}

// chooseFormat выбирает формат захвата из поддерживаемых камерой: yuyv — яркость берётся без
// декодирования (дёшево по CPU, но больше байтов по USB), mjpeg — сжатый кадр, который нужно
// декодировать; auto — YUYV, если он есть, иначе MJPEG.
func chooseFormat(have []uint32, want string) (uint32, error) {
	var order []uint32
	switch want {
	case "yuyv":
		order = []uint32{pixYUYV}
	case "mjpeg":
		order = []uint32{pixMJPG, pixJPEG}
	case "auto":
		order = []uint32{pixYUYV, pixMJPG, pixJPEG}
	default:
		return 0, fmt.Errorf("v4l2 format must be auto, yuyv or mjpeg, got %q", want)
	}
	for _, f := range order {
		if slices.Contains(have, f) {
			return f, nil
		}
	}
	names := make([]string, len(have))
	for i, h := range have {
		names[i] = fourcc(h)
	}
	return 0, fmt.Errorf("camera has no %s format (it offers %s)", want, strings.Join(names, ", "))
}

// frameDecoder переводит кадр камеры в яркость; scratch — буфер для кадров MJPEG без таблиц Хаффмана.
type frameDecoder struct {
	format  uint32
	stride  int
	scratch []byte
}

func (fd *frameDecoder) decode(data []byte, dst *image.Gray) error {
	if fd.format == pixYUYV {
		return luma.FromYUYV(dst, data, fd.stride)
	}
	if !hasDHT(data) {
		fd.scratch = append(append(append(fd.scratch[:0], data[:2]...), stdDHT...), data[2:]...)
		data = fd.scratch
	}
	img, err := jpeg.Decode(bytes.NewReader(data))
	if err != nil {
		return fmt.Errorf("mjpeg: %w", err)
	}
	return luma.Copy(dst, img)
}

// hasDHT — есть ли в JPEG таблицы Хаффмана до начала данных скана. Многие USB-камеры шлют MJPEG без
// них (по соглашению MJPEG таблицы фиксированы и опускаются); декодер подставляет стандартные таблицы
// ITU-T T.81, приложение K.3 — так делают Chromium и libjpeg-turbo (ссылки в README).
func hasDHT(b []byte) bool {
	if len(b) < 4 || b[0] != 0xFF || b[1] != 0xD8 {
		return true // не JPEG: пусть ошибку сообщит декодер
	}
	for i := 2; i+3 < len(b); {
		if b[i] != 0xFF {
			return true
		}
		switch m := b[i+1]; {
		case m == 0xC4:
			return true
		case m == 0xDA:
			return false
		case m == 0xFF:
			i++ // заполняющий байт
			continue
		}
		i += 2 + segLen(b, i)
	}
	return true
}

// segLen — длина сегмента JPEG с маркером в b[i:] (поле длины включает себя, но не маркер).
func segLen(b []byte, i int) int { return int(b[i+2])<<8 | int(b[i+3]) }

// stdDHT — сегмент DHT со стандартными таблицами K.3. Его не нужно набирать вручную: кодер
// image/jpeg пишет именно эти таблицы, сегмент берётся из закодированного цветного кадра 8×8.
var stdDHT = func() []byte {
	var buf bytes.Buffer
	img := image.NewRGBA(image.Rect(0, 0, 8, 8))
	img.Set(1, 1, color.RGBA{200, 10, 10, 255})
	if err := jpeg.Encode(&buf, img, nil); err != nil {
		panic(err)
	}
	b := buf.Bytes()
	for i := 2; i+3 < len(b); i += 2 + segLen(b, i) {
		if b[i+1] == 0xC4 {
			return append([]byte(nil), b[i:i+2+segLen(b, i)]...)
		}
	}
	panic(errors.New("image/jpeg wrote no DHT segment"))
}()
