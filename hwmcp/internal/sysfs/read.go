package sysfs

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unsafe"
)

// readAttr читает атрибут sysfs как строку, как _read() прототипа: UTF-8 с заменой
// недопустимых байтов, без пробелов по краям и без завершающих NUL (строки EEPROM HAT
// в /proc/device-tree заканчиваются на \0). ok=false — файла нет или он не читается.
func readAttr(path string) (string, bool) {
	b, err := os.ReadFile(path)
	if err != nil {
		return "", false
	}
	return cleanAttr(string(b)), true
}

// cleanAttr — нормализация содержимого атрибута, как в _read() прототипа.
func cleanAttr(s string) string {
	s = strings.ToValidUTF8(s, "�")
	s = strings.TrimSpace(s)
	s = strings.Trim(s, "\x00")
	return strings.TrimSpace(s)
}

// readAttrPtr — readAttr с nil вместо отсутствующего значения (для полей, которые в JSON бывают null).
func readAttrPtr(path string) *string {
	s, ok := readAttr(path)
	if !ok {
		return nil
	}
	return &s
}

// parseFloat — аналог _float(): число или ok=false (нет файла / не число).
func parseFloat(s string, present bool) (float64, bool) {
	if !present {
		return 0, false
	}
	v, err := strconv.ParseFloat(strings.TrimSpace(s), 64)
	if err != nil {
		return 0, false
	}
	return v, true
}

func readFloat(path string) (float64, bool) {
	s, ok := readAttr(path)
	return parseFloat(s, ok)
}

// listDir — замена glob.glob(dir/pattern) прототипа без интерпретации спецсимволов в пути корня:
// имена записей каталога, подходящих под match, в лексикографическом порядке (как sorted(glob(...))).
// Скрытые записи пропускаются, как это делает glob для шаблонов без ведущей точки.
func listDir(dir string, match func(name string) bool) []string {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var out []string
	for _, e := range entries {
		name := e.Name()
		if strings.HasPrefix(name, ".") || !match(name) {
			continue
		}
		out = append(out, filepath.Join(dir, name))
	}
	sort.Strings(out)
	return out
}

// listNames — отсортированные имена записей каталога (os.listdir + sorted), включая скрытые.
func listNames(dir string) []string {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		names = append(names, e.Name())
	}
	sort.Strings(names)
	return names
}

// realParent — имя родительского каталога после разрешения ссылок (_real_parent прототипа).
func realParent(path string) string {
	rp, err := filepath.EvalSymlinks(path)
	if err != nil {
		rp, _ = filepath.Abs(path)
	}
	return filepath.Base(filepath.Dir(rp))
}

func realPath(path string) string {
	rp, err := filepath.EvalSymlinks(path)
	if err != nil {
		rp, _ = filepath.Abs(path)
	}
	return rp
}

var i2cClientRe = regexp.MustCompile(`^(\d+)-00([0-9a-f]{2})$`)

// i2cLabel превращает имя клиента I2C "1-0076" в "i2c-1/0x76" (_i2c_label прототипа).
func i2cLabel(parent string) (string, bool) {
	m := i2cClientRe.FindStringSubmatch(parent)
	if m == nil {
		return "", false
	}
	return "i2c-" + m[1] + "/0x" + m[2], true
}

// relPath — os.path.relpath(path, root).
func relPath(root, path string) string {
	r, err := filepath.Rel(root, path)
	if err != nil {
		return path
	}
	return filepath.ToSlash(r)
}

// pyStr воспроизводит f-строку Python для необязательной строки: None печатается как "None".
// Нужна только для паритета идентификаторов вида "v4l2:None" на неполных деревьях sysfs.
func pyStr(s *string) string {
	if s == nil {
		return "None"
	}
	return *s
}

func strPtr(s string) *string { return &s }

// round6 — round(x, 6) Python: корректное округление точного двоичного значения до 6 знаков
// с правилом «половина — к чётному» (strconv делает именно это). Текст числа — в буфере на стеке:
// round6 вызывается на каждом опросе датчика и не должен выделять память.
func round6(x float64) float64 {
	var buf [64]byte
	b := strconv.AppendFloat(buf[:0], x, 'f', 6, 64)
	v, err := strconv.ParseFloat(unsafe.String(&b[0], len(b)), 64)
	if err != nil {
		return x
	}
	return v
}
