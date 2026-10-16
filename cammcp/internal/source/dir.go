package source

import (
	"bytes"
	"errors"
	"fmt"
	"image"
	_ "image/jpeg" // декодеры для image.Decode
	_ "image/png"
	"io"
	"math"
	"os"
	"path/filepath"
	"strings"

	"github.com/local/cammcp/internal/luma"
)

// frameKinds — расширения файлов кадров.
var frameKinds = map[string]string{".pgm": "pgm", ".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}

// Dir — каталог кадров (PGM P5, PNG, JPEG; порядок — по имени файла), проигрываемых с частотой fps.
// Кадр декодируется при первом обращении к его номеру; все кадры должны быть одного размера.
type Dir struct {
	path  string
	files []string
	fps   float64
	idx   int
	cur   *image.Gray
	kinds string
}

// OpenDir открывает каталог кадров.
func OpenDir(path string, fps float64) (*Dir, error) {
	if !(fps > 0) {
		return nil, errors.New("dir: fps must be > 0")
	}
	entries, err := os.ReadDir(path)
	if err != nil {
		return nil, err
	}
	d := &Dir{path: path, fps: fps}
	present := map[string]bool{}
	for _, e := range entries {
		if k, ok := frameKinds[strings.ToLower(filepath.Ext(e.Name()))]; ok && !e.IsDir() {
			d.files = append(d.files, filepath.Join(path, e.Name()))
			present[k] = true
		}
	}
	if len(d.files) == 0 {
		return nil, fmt.Errorf("dir %s: no .pgm/.png/.jpg frames", path)
	}
	var kinds []string
	for _, k := range []string{"pgm", "png", "jpeg"} {
		if present[k] {
			kinds = append(kinds, k)
		}
	}
	d.kinds = strings.Join(kinds, "/")
	img, err := decodeFile(d.files[0])
	if err != nil {
		return nil, err
	}
	b := img.Bounds()
	d.cur = image.NewGray(image.Rect(0, 0, b.Dx(), b.Dy()))
	if err := luma.Copy(d.cur, img); err != nil {
		return nil, err
	}
	return d, nil
}

// Grab копирует в dst кадр номер ⌊t·fps⌋; после последнего — io.EOF.
func (d *Dir) Grab(t float64, dst *image.Gray) error {
	i := max(0, int(math.Floor(t*d.fps+1e-9)))
	if i >= len(d.files) {
		return io.EOF
	}
	if i != d.idx {
		img, err := decodeFile(d.files[i])
		if err != nil {
			return err
		}
		if err := luma.Copy(d.cur, img); err != nil {
			return fmt.Errorf("%s: %w", d.files[i], err)
		}
		d.idx = i
	}
	copy(dst.Pix, d.cur.Pix)
	return nil
}

// Info — описание источника.
func (d *Dir) Info() Info {
	return Info{Kind: "dir", Name: d.path, W: d.cur.Rect.Dx(), H: d.cur.Rect.Dy(), Format: d.kinds, FPS: d.fps}
}

// Close ничего не делает: файлы читаются целиком.
func (d *Dir) Close() error { return nil }

func decodeFile(path string) (image.Image, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var img image.Image
	if strings.EqualFold(filepath.Ext(path), ".pgm") {
		img, err = decodePGM(b)
	} else {
		img, _, err = image.Decode(bytes.NewReader(b))
	}
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return img, nil
}

// decodePGM разбирает двоичный PGM (P5) с maxval ≤ 255; значения приводятся к 0..255. Размер из
// заголовка сверяется с данными до выделения кадра: битый заголовок — ошибка, а не паника или гигабайты.
func decodePGM(b []byte) (*image.Gray, error) {
	r := bytes.NewReader(b)
	var hdr [4]int
	for i := range hdr {
		tok, err := pgmToken(r)
		if err != nil {
			return nil, fmt.Errorf("pgm header: %w", err)
		}
		if i == 0 {
			if tok != "P5" {
				return nil, errors.New("pgm: only binary P5 is supported")
			}
			continue
		}
		if _, err := fmt.Sscan(tok, &hdr[i]); err != nil || hdr[i] <= 0 {
			return nil, fmt.Errorf("pgm header: bad number %q", tok)
		}
	}
	w, h, maxval := hdr[1], hdr[2], hdr[3]
	if maxval > 255 {
		return nil, errors.New("pgm: 16-bit samples are not supported")
	}
	if h > r.Len()/w {
		return nil, fmt.Errorf("pgm %dx%d: only %d bytes of samples", w, h, r.Len())
	}
	g := image.NewGray(image.Rect(0, 0, w, h))
	copy(g.Pix, b[len(b)-r.Len():])
	if maxval != 255 {
		for i, v := range g.Pix {
			g.Pix[i] = uint8(min(255, (int(v)*255+maxval/2)/maxval))
		}
	}
	return g, nil
}

// pgmToken читает слово заголовка PGM, пропуская пробелы и комментарии (#…), и съедает один
// разделитель после него — после maxval сразу идут данные.
func pgmToken(r io.ByteReader) (string, error) {
	var tok []byte
	comment := false
	for {
		c, err := r.ReadByte()
		if err != nil {
			return "", err
		}
		switch {
		case comment:
			comment = c != '\n'
		case c == '#' && len(tok) == 0:
			comment = true
		case c == ' ' || c == '\t' || c == '\n' || c == '\r':
			if len(tok) > 0 {
				return string(tok), nil
			}
		default:
			tok = append(tok, c)
		}
	}
}
