// Package thermal читает температуры зон /sys/class/thermal/thermal_zone*/temp (милли-°C).
package thermal

import (
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// Zone — одна термозона.
type Zone struct {
	Name  string  // содержимое type (cpu-thermal, soc-thermal ...); при совпадении имён добавляется номер зоны
	TempC float64 // градусы Цельсия
}

// Read возвращает температуры всех зон под sysRoot (обычно "/sys"). Отсутствие каталога — не ошибка:
// в ВМ и на macOS термозон нет. Зоны, которые не читаются (выключенный датчик даёт EINVAL), пропускаются.
func Read(sysRoot string) []Zone {
	dirs, _ := filepath.Glob(filepath.Join(sysRoot, "class", "thermal", "thermal_zone*"))
	sort.Strings(dirs)
	var zones []Zone
	seen := map[string]bool{}
	for _, dir := range dirs {
		raw, err := os.ReadFile(filepath.Join(dir, "temp"))
		if err != nil {
			continue
		}
		milli, err := strconv.ParseInt(strings.TrimSpace(string(raw)), 10, 64)
		if err != nil {
			continue
		}
		name := filepath.Base(dir)
		if t, err := os.ReadFile(filepath.Join(dir, "type")); err == nil {
			if s := strings.TrimSpace(string(t)); s != "" {
				name = s
			}
		}
		if seen[name] {
			name = name + "/" + strings.TrimPrefix(filepath.Base(dir), "thermal_zone")
		}
		seen[name] = true
		zones = append(zones, Zone{Name: name, TempC: float64(milli) / 1000})
	}
	return zones
}
