package gpio

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestFilesOutputInitialAndSet(t *testing.T) {
	dir := t.TempDir()
	f := &Files{Dir: dir}
	l, err := f.Open(LineConfig{Chip: "gpiochip0", Offset: 27, Output: true, Initial: false})
	if err != nil {
		t.Fatal(err)
	}
	if v, err := l.Get(); err != nil || v {
		t.Fatalf("initial: %v %v", v, err)
	}
	if err := l.Set(true); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(filepath.Join(dir, "gpiochip0", "line27"))
	if string(b) != "1\n" {
		t.Fatalf("file level %q", b)
	}
	if v, _ := l.Get(); !v {
		t.Fatal("set true not visible")
	}
}

func TestFilesActiveLow(t *testing.T) {
	dir := t.TempDir()
	f := &Files{Dir: dir}
	out, _ := f.Open(LineConfig{Chip: "gpiochip0", Offset: 5, Output: true, ActiveLow: true, Initial: true})
	b, _ := os.ReadFile(filepath.Join(dir, "gpiochip0", "line5"))
	if string(b) != "0\n" { // логическая 1 при active_low = низкий уровень
		t.Fatalf("level %q", b)
	}
	if v, _ := out.Get(); !v {
		t.Fatal("logical value must be true")
	}
	in, _ := f.Open(LineConfig{Chip: "gpiochip0", Offset: 5, ActiveLow: true})
	if v, _ := in.Get(); !v {
		t.Fatal("input with active_low must read logical true")
	}
}

func TestFilesInputUnknownAndExternalChange(t *testing.T) {
	dir := t.TempDir()
	f := &Files{Dir: dir}
	in, err := f.Open(LineConfig{Chip: "gpiochip0", Offset: 17})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := in.Get(); !errors.Is(err, ErrUnknown) {
		t.Fatalf("missing file must be unknown, got %v", err)
	}
	if err := in.Set(true); !errors.Is(err, ErrNotOutput) {
		t.Fatalf("input must refuse Set: %v", err)
	}
	// симулятор «нажимает» PIR, записывая уровень
	if err := os.MkdirAll(filepath.Join(dir, "gpiochip0"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "gpiochip0", "line17"), []byte("1"), 0o644); err != nil {
		t.Fatal(err)
	}
	if v, err := in.Get(); err != nil || !v {
		t.Fatalf("external level: %v %v", v, err)
	}
	if err := os.WriteFile(filepath.Join(dir, "gpiochip0", "line17"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := in.Get(); err == nil {
		t.Fatal("garbage level must be an error")
	}
}

// Метка чипа в эмуляции — имя каталога: declared.json с меткой работает и на фикстуре.
func TestFilesChipLabel(t *testing.T) {
	dir := t.TempDir()
	l, err := (&Files{Dir: dir}).Open(LineConfig{Chip: "tegra-gpio", Offset: 2, Output: true, Initial: true})
	if err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(filepath.Join(dir, "tegra-gpio", "line2")); string(b) != "1\n" {
		t.Fatalf("file level %q", b)
	}
	if err := l.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestFilesRejectsBadChip(t *testing.T) {
	f := &Files{Dir: t.TempDir()}
	for _, c := range []LineConfig{{Chip: "../x", Offset: 1}, {Chip: "", Offset: 1}, {Chip: "gpiochip0", Offset: -1}} {
		if _, err := f.Open(c); err == nil {
			t.Errorf("%+v must be rejected", c)
		}
	}
}
