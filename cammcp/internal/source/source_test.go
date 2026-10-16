package source

import (
	"bytes"
	"errors"
	"fmt"
	"image"
	"image/jpeg"
	"image/png"
	"io"
	"os"
	"path/filepath"
	"testing"

	"github.com/local/cammcp/internal/scene"
)

func TestSynthetic(t *testing.T) {
	s := Synthetic{Scene: scene.New(scene.Demo()), Name: "demo"}
	info := s.Info()
	if info.Kind != "synthetic" || info.W != 320 || info.H != 240 || info.FPS != 0 {
		t.Fatalf("%+v", info)
	}
	a, b := image.NewGray(image.Rect(0, 0, 320, 240)), image.NewGray(image.Rect(0, 0, 320, 240))
	if err := s.Grab(21, a); err != nil {
		t.Fatal(err)
	}
	scene.New(scene.Demo()).Render(21, b)
	if !bytes.Equal(a.Pix, b.Pix) || s.Close() != nil {
		t.Fatal("synthetic source must render the scene")
	}
}

func frame(w, h int, v uint8) *image.Gray {
	g := image.NewGray(image.Rect(0, 0, w, h))
	for i := range g.Pix {
		g.Pix[i] = v + uint8(i%3)
	}
	return g
}

func writeFile(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatal(err)
	}
}

func pgm(g *image.Gray, header string) []byte {
	return append([]byte(header), g.Pix...)
}

func encode(t *testing.T, g *image.Gray, f func(io.Writer, image.Image) error) []byte {
	t.Helper()
	var buf bytes.Buffer
	if err := f(&buf, g); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

// Каталог: кадры по имени, номер = ⌊t·fps⌋, после последнего — EOF; PGM, PNG и JPEG вперемешку.
func TestDir(t *testing.T) {
	dir := t.TempDir()
	f0, f1, f2 := frame(8, 6, 10), frame(8, 6, 100), frame(8, 6, 200)
	writeFile(t, filepath.Join(dir, "000.pgm"), pgm(f0, "P5\n# камера 1\n8 6\n255\n"))
	writeFile(t, filepath.Join(dir, "001.png"), encode(t, f1, png.Encode))
	writeFile(t, filepath.Join(dir, "002.JPG"), encode(t, f2, func(w io.Writer, m image.Image) error {
		return jpeg.Encode(w, m, &jpeg.Options{Quality: 100})
	}))
	writeFile(t, filepath.Join(dir, "notes.txt"), []byte("не кадр"))
	d, err := OpenDir(dir, 2)
	if err != nil {
		t.Fatal(err)
	}
	if info := d.Info(); info.W != 8 || info.H != 6 || info.FPS != 2 || info.Format != "pgm/png/jpeg" {
		t.Fatalf("%+v", info)
	}
	dst := image.NewGray(image.Rect(0, 0, 8, 6))
	for _, c := range []struct {
		t    float64
		want *image.Gray
	}{{0, f0}, {0.49, f0}, {0.5, f1}, {1.2, f2}, {0.1, f0}} {
		if err := d.Grab(c.t, dst); err != nil {
			t.Fatal(err)
		}
		for i := range dst.Pix {
			if diff := int(dst.Pix[i]) - int(c.want.Pix[i]); diff > 2 || diff < -2 { // JPEG q100 ±2
				t.Fatalf("t=%v pixel %d: %d, want %d", c.t, i, dst.Pix[i], c.want.Pix[i])
			}
		}
	}
	if err := d.Grab(1.5, dst); !errors.Is(err, io.EOF) {
		t.Fatalf("after the last frame: %v", err)
	}
	if d.Close() != nil {
		t.Fatal("close")
	}
}

func TestDirErrors(t *testing.T) {
	empty := t.TempDir()
	if _, err := OpenDir(empty, 5); err == nil {
		t.Fatal("empty dir accepted")
	}
	if _, err := OpenDir(empty, 0); err == nil {
		t.Fatal("fps 0 accepted")
	}
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "a.pgm"), pgm(frame(8, 6, 1), "P5 8 6 255\n"))
	writeFile(t, filepath.Join(dir, "b.pgm"), pgm(frame(4, 4, 1), "P5 4 4 255\n"))
	d, err := OpenDir(dir, 1)
	if err != nil {
		t.Fatal(err)
	}
	if err := d.Grab(1, image.NewGray(image.Rect(0, 0, 8, 6))); err == nil {
		t.Fatal("frame of another size accepted")
	}
}

func TestPGM(t *testing.T) {
	g, err := decodePGM([]byte("P5\n#c\n2 1\n#c2\n100\n\x00\x64"))
	if err != nil || g.Pix[0] != 0 || g.Pix[1] != 255 {
		t.Fatal(err, g)
	}
	for name, b := range map[string]string{
		"ascii": "P2\n2 1\n255\n0 1", "16bit": "P5\n2 1\n65535\n\x00\x00\x00\x00", "short": "P5\n4 4\n255\n\x00",
		"zero": "P5\n0 4\n255\n", "word": "P5\nx 4\n255\n", "huge": "P5\n4294967296 4294967296\n255\n\x00\x00",
		"comment": "P5\n# no end",
	} {
		if _, err := decodePGM([]byte(b)); err == nil {
			t.Errorf("%s accepted", name)
		}
	}
}

func TestChooseFormat(t *testing.T) {
	both := []uint32{pixMJPG, pixYUYV}
	for _, c := range []struct {
		have []uint32
		want string
		got  uint32
	}{{both, "auto", pixYUYV}, {both, "mjpeg", pixMJPG}, {both, "yuyv", pixYUYV}, {[]uint32{pixJPEG}, "auto", pixJPEG}} {
		if f, err := chooseFormat(c.have, c.want); err != nil || f != c.got {
			t.Errorf("%v %s: %s %v", c.have, c.want, fourcc(f), err)
		}
	}
	if _, err := chooseFormat([]uint32{pixMJPG}, "yuyv"); err == nil {
		t.Fatal("missing format accepted")
	}
	if _, err := chooseFormat(both, "h264"); err == nil {
		t.Fatal("unknown format name accepted")
	}
	if fourcc(pixYUYV) != "YUYV" || fourcc(pixMJPG) != "MJPG" || fourcc('Y'|'1'<<8|'6'<<16|' '<<24|1<<31) != "Y16 -BE" {
		t.Fatal(fourcc(pixYUYV))
	}
}

// stripDHT удаляет сегменты DHT (так шлют кадры многие UVC-камеры).
func stripDHT(b []byte) []byte {
	out := append([]byte(nil), b[:2]...)
	i := 2
	for b[i+1] != 0xDA {
		n := 2 + segLen(b, i)
		if b[i+1] != 0xC4 {
			out = append(out, b[i:i+n]...)
		}
		i += n
	}
	return append(out, b[i:]...)
}

func TestFrameDecoder(t *testing.T) {
	src := image.NewYCbCr(image.Rect(0, 0, 16, 8), image.YCbCrSubsampleRatio422)
	for i := range src.Y {
		src.Y[i] = uint8(i * 2)
	}
	var buf bytes.Buffer
	if err := jpeg.Encode(&buf, src, &jpeg.Options{Quality: 100}); err != nil {
		t.Fatal(err)
	}
	full := buf.Bytes()
	bare := stripDHT(full)
	if hasDHT(bare) || !hasDHT(full) || len(bare) >= len(full) {
		t.Fatal("DHT detection")
	}
	if _, err := jpeg.Decode(bytes.NewReader(bare)); err == nil {
		t.Fatal("bare MJPEG decodes without tables: test is meaningless")
	}
	dec := frameDecoder{format: pixMJPG}
	want, got := image.NewGray(image.Rect(0, 0, 16, 8)), image.NewGray(image.Rect(0, 0, 16, 8))
	if err := dec.decode(full, want); err != nil {
		t.Fatal(err)
	}
	if err := dec.decode(bare, got); err != nil || !bytes.Equal(want.Pix, got.Pix) {
		t.Fatalf("MJPEG without DHT: %v", err)
	}
	if err := dec.decode([]byte("garbage"), got); err == nil {
		t.Fatal("garbage decoded")
	}
	// стандартные таблицы K.3: длина сегмента 418, первая таблица — DC яркости с числами кодов 0,1,5,1,…
	if fmt.Sprintf("% x", stdDHT[:8]) != "ff c4 01 a2 00 00 01 05" {
		t.Fatalf("% x", stdDHT[:8])
	}
	yuyv := frameDecoder{format: pixYUYV, stride: 4}
	g := image.NewGray(image.Rect(0, 0, 2, 1))
	if err := yuyv.decode([]byte{7, 128, 9, 128}, g); err != nil || g.Pix[0] != 7 || g.Pix[1] != 9 {
		t.Fatal(err, g.Pix)
	}
}

func TestV4L2OpenMissingDevice(t *testing.T) {
	if _, err := OpenV4L2("/dev/cammcp-no-such-camera", "auto", 640, 480, 8); err == nil {
		t.Fatal("missing device opened")
	}
}
