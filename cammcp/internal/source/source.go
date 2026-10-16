// Package source — источники кадров яркости для cam-mcp: синтетическая сцена (основной, пока камеры
// нет), каталог кадров (повтор записи) и USB-камера через V4L2 (только Linux).
package source

import (
	"image"

	"github.com/local/cammcp/internal/scene"
)

// Source — источник кадров.
type Source interface {
	// Grab кладёт в dst (размер из Info) кадр момента t — секунд от старта камеры: синтетика рисует
	// сцену момента t, каталог берёт кадр номер ⌊t·fps⌋, V4L2 время не использует и отдаёт свежий
	// кадр. io.EOF — кадры кончились.
	Grab(t float64, dst *image.Gray) error
	Info() Info
	Close() error
}

// Info — описание источника.
type Info struct {
	Kind   string  // synthetic | dir | v4l2
	Name   string  // сценарий, каталог или устройство
	W, H   int     // размер кадра
	Format string  // gray8 (синтетика), pgm/png/jpeg (каталог), YUYV/MJPG (V4L2)
	FPS    float64 // собственная частота кадров; 0 — любая (кадры чаще неё не обрабатываются)
}

// Synthetic — синтетическая сцена как источник (Render не выделяет память).
type Synthetic struct {
	Scene *scene.Scene
	Name  string
}

// Grab рисует сцену момента t.
func (s Synthetic) Grab(t float64, dst *image.Gray) error {
	s.Scene.Render(t, dst)
	return nil
}

// Info — описание источника.
func (s Synthetic) Info() Info {
	w, h := s.Scene.Size()
	return Info{Kind: "synthetic", Name: s.Name, W: w, H: h, Format: "gray8"}
}

// Close ничего не делает.
func (s Synthetic) Close() error { return nil }
