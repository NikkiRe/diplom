package gpio

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

// Files — эмуляция линий файлами: <Dir>/<chip>/line<N> содержит физический уровень "0" или "1".
type Files struct {
	Dir string
}

// Name — имя драйвера для журнала.
func (f *Files) Name() string { return "files:" + f.Dir }

type fileLine struct {
	mu   sync.Mutex
	path string
	cfg  LineConfig
}

// Open открывает линию. Выход сразу получает начальное значение (как запрос линии с
// GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES): после старта состояние привода известно.
func (f *Files) Open(cfg LineConfig) (Line, error) {
	if cfg.Chip == "" || strings.ContainsAny(cfg.Chip, `/\`) || cfg.Offset < 0 {
		return nil, fmt.Errorf("gpio: bad line %s", cfg)
	}
	l := &fileLine{path: filepath.Join(f.Dir, cfg.Chip, fmt.Sprintf("line%d", cfg.Offset)), cfg: cfg}
	if cfg.Output {
		if err := l.Set(cfg.Initial); err != nil {
			return nil, err
		}
	}
	return l, nil
}

func (l *fileLine) Get() (bool, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	b, err := os.ReadFile(l.path)
	if os.IsNotExist(err) {
		return false, ErrUnknown
	}
	if err != nil {
		return false, err
	}
	var level bool
	switch strings.TrimSpace(string(b)) {
	case "0":
		level = false
	case "1":
		level = true
	default:
		return false, fmt.Errorf("gpio: %s: bad level %q", l.path, strings.TrimSpace(string(b)))
	}
	return level != l.cfg.ActiveLow, nil
}

func (l *fileLine) Set(v bool) error {
	if !l.cfg.Output {
		return ErrNotOutput
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	level := v != l.cfg.ActiveLow
	text := "0\n"
	if level {
		text = "1\n"
	}
	if err := os.MkdirAll(filepath.Dir(l.path), 0o755); err != nil {
		return err
	}
	// запись через временный файл и rename: читатель не увидит пустой файл
	tmp := l.path + ".tmp"
	if err := os.WriteFile(tmp, []byte(text), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, l.path)
}

func (l *fileLine) Close() error { return nil }
