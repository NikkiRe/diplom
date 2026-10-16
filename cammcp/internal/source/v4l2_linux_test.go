//go:build linux && (arm64 || amd64)

package source

import (
	"image"
	"os"
	"testing"
	"time"
	"unsafe"
)

// Размеры структур и номера ioctl совпадают с videodev2.h для 64-битного Linux (значения из заголовков
// ядра: например, VIDIOC_QBUF = 0xc058560f — struct v4l2_buffer в 88 байт).
func TestV4L2ABI(t *testing.T) {
	sizes := map[string][2]uintptr{
		"v4l2_capability":     {unsafe.Sizeof(v4l2Capability{}), 104},
		"v4l2_fmtdesc":        {unsafe.Sizeof(v4l2Fmtdesc{}), 64},
		"v4l2_format":         {unsafe.Sizeof(v4l2Format{}), 208},
		"v4l2_requestbuffers": {unsafe.Sizeof(v4l2RequestBuffers{}), 20},
		"v4l2_buffer":         {unsafe.Sizeof(v4l2Buffer{}), 88},
		"v4l2_streamparm":     {unsafe.Sizeof(v4l2Streamparm{}), 204},
	}
	for name, s := range sizes {
		if s[0] != s[1] {
			t.Errorf("sizeof %s = %d, want %d", name, s[0], s[1])
		}
	}
	if off := unsafe.Offsetof(v4l2Format{}.Pix); off != 8 {
		t.Errorf("v4l2_format.fmt offset %d, want 8", off)
	}
	if off := unsafe.Offsetof(v4l2Buffer{}.M); off != 64 {
		t.Errorf("v4l2_buffer.m offset %d, want 64", off)
	}
	for name, c := range map[string][2]uintptr{
		"QUERYCAP": {vidiocQuerycap, 0x80685600}, "ENUM_FMT": {vidiocEnumFmt, 0xc0405602},
		"S_FMT": {vidiocSFmt, 0xc0d05605}, "REQBUFS": {vidiocReqbufs, 0xc0145608},
		"QUERYBUF": {vidiocQuerybuf, 0xc0585609}, "QBUF": {vidiocQbuf, 0xc058560f},
		"DQBUF": {vidiocDqbuf, 0xc0585611}, "STREAMON": {vidiocStreamon, 0x40045612},
		"STREAMOFF": {vidiocStreamoff, 0x40045613}, "G_PARM": {vidiocGParm, 0xc0cc5615},
		"S_PARM": {vidiocSParm, 0xc0cc5616},
	} {
		if c[0] != c[1] {
			t.Errorf("VIDIOC_%s = %#x, want %#x", name, c[0], c[1])
		}
	}
}

// Захват с настоящего устройства V4L2 (в ВМ — драйвер vivid; см. README); формат — CAMMCP_V4L2_FORMAT:
//
//	CAMMCP_V4L2_DEVICE=/dev/video0 ./source.test -test.run V4L2Device -test.v
func TestV4L2Device(t *testing.T) {
	dev := os.Getenv("CAMMCP_V4L2_DEVICE")
	if dev == "" {
		t.Skip("CAMMCP_V4L2_DEVICE is not set")
	}
	format := os.Getenv("CAMMCP_V4L2_FORMAT")
	if format == "" {
		format = "auto"
	}
	src, err := OpenV4L2(dev, format, 640, 480, 10)
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := src.Close(); err != nil {
			t.Error(err)
		}
	}()
	info := src.Info()
	t.Logf("%+v", info)
	dst := image.NewGray(image.Rect(0, 0, info.W, info.H))
	start := time.Now()
	const n = 10
	for i := 0; i < n; i++ {
		if err := src.Grab(0, dst); err != nil {
			t.Fatal(err)
		}
	}
	lo, hi := 255, 0
	for _, v := range dst.Pix {
		lo, hi = min(lo, int(v)), max(hi, int(v))
	}
	t.Logf("%d frames in %v; luma %d..%d", n, time.Since(start), lo, hi)
	if hi-lo < 32 {
		t.Fatalf("flat frame (%d..%d): luma is not extracted", lo, hi)
	}
	// Пауза: камера сняла около pause·fps кадров, буферы заполнились. Grab должен сбросить их и отдать
	// свежий кадр; без сброса он отдал бы следующий по номеру (разрыв номеров 1).
	const pause = 500 * time.Millisecond
	seq := src.(*V4L2).seq
	time.Sleep(pause)
	if err := src.Grab(0, dst); err != nil {
		t.Fatal(err)
	}
	gap := src.(*V4L2).seq - seq
	t.Logf("sequence gap after %v: %d", pause, gap)
	if info.FPS > 0 && float64(gap) < pause.Seconds()*info.FPS-1 {
		t.Fatalf("stale frame after a pause: sequence gap %d at %g fps", gap, info.FPS)
	}
}
