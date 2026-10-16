package hub

import (
	"math"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
)

// BoardHealth — уровень L3 контекста оборудования (инструмент board_health для LLM): нагрузка, память,
// температура SoC, троттлинг и сбои датчиков (какие свойства сейчас недоступны и почему, сколько всего
// было неудачных чтений). Только то, что нужно модели для решения, с округлением до значащих знаков:
// остальные счётчики hw-mcp и ресурсы процесса — внутренняя диагностика, они в Diag (ресурс hw://diag)
// и в журнале run_end, а не в токенах промпта.
//
// Сведения о плате читаются относительно корня (--root): на фикстуре без /proc поля равны null.
func (h *Hub) BoardHealth() map[string]any {
	root := h.opts.Root
	return map[string]any{
		"now":           math.Round(h.Now()*10) / 10,
		"load":          loadAvg(filepath.Join(root, "proc/loadavg")),
		"memory":        memInfo(filepath.Join(root, "proc/meminfo")),
		"uptime_s":      roundAny(firstFloat(filepath.Join(root, "proc/uptime")), 1),
		"cpu_temp_c":    roundAny(h.cpuTemp(root), 10),
		"throttled":     readTrim(filepath.Join(root, "sys/devices/platform/soc/soc:firmware/get_throttled")),
		"sensor_faults": h.SensorFaults(),
		"sensor_errors": h.sampleErrors.Load(),
	}
}

// Diag — диагностика hw-mcp для оператора и стенда (ресурс hw://diag): счётчики, сбои по свойствам и
// ресурсы процесса.
func (h *Hub) Diag() map[string]any {
	return map[string]any{"now": h.Now(), "hw_mcp": h.Stats(), "process": processStats(),
		"sample_periods": h.SamplePeriods(), "sensors": h.sensorHistory()}
}

// roundAny округляет число до 1/scale (nil и не-числа — как есть).
func roundAny(v any, scale float64) any {
	if f, ok := v.(float64); ok {
		return math.Round(f*scale) / scale
	}
	return v
}

func readTrim(path string) any {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	return strings.TrimSpace(string(b))
}

func firstFloat(path string) any {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	f := strings.Fields(string(b))
	if len(f) == 0 {
		return nil
	}
	v, err := strconv.ParseFloat(f[0], 64)
	if err != nil {
		return nil
	}
	return v
}

func loadAvg(path string) any {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	f := strings.Fields(string(b))
	if len(f) < 3 {
		return nil
	}
	var out []any
	for _, s := range f[:3] {
		v, err := strconv.ParseFloat(s, 64)
		if err != nil {
			return nil
		}
		out = append(out, v)
	}
	return out
}

// memInfo — MemTotal и MemAvailable из /proc/meminfo, МиБ.
func memInfo(path string) any {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	res := map[string]any{}
	for _, line := range strings.Split(string(b), "\n") {
		f := strings.Fields(line)
		if len(f) < 2 {
			continue
		}
		kib, err := strconv.ParseFloat(f[1], 64)
		if err != nil {
			continue
		}
		switch f[0] {
		case "MemTotal:":
			res["total_mib"] = math.Round(kib / 1024)
		case "MemAvailable:":
			res["available_mib"] = math.Round(kib / 1024)
		}
	}
	if len(res) == 0 {
		return nil
	}
	return res
}

// cpuTemp — температура SoC: из инвентаря (hwmon cpu_thermal), иначе thermal_zone0.
func (h *Hub) cpuTemp(root string) any {
	for _, d := range h.inv.Devices {
		if d.Source == "hwmon" && d.Kind == "cpu_thermal" {
			for _, p := range d.Properties {
				if p.Quantity == "temp" {
					if v, _, ok := h.Get(d.ID + "." + p.Name); ok {
						return v
					}
				}
			}
		}
	}
	v := firstFloat(filepath.Join(root, "sys/class/thermal/thermal_zone0/temp"))
	if f, ok := v.(float64); ok {
		return round3(f / 1000)
	}
	return nil
}

// processStats — ресурсы процесса hw-mcp.
func processStats() map[string]any {
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	out := map[string]any{
		"goroutines": runtime.NumGoroutine(),
		"heap_mib":   round3(float64(ms.HeapAlloc) / (1 << 20)),
		"sys_mib":    round3(float64(ms.Sys) / (1 << 20)),
		"rss_mib":    nil,
	}
	if b, err := os.ReadFile("/proc/self/status"); err == nil {
		for _, line := range strings.Split(string(b), "\n") {
			if strings.HasPrefix(line, "VmRSS:") {
				f := strings.Fields(line)
				if len(f) >= 2 {
					if kib, err := strconv.ParseFloat(f[1], 64); err == nil {
						out["rss_mib"] = round3(kib / 1024)
					}
				}
			}
		}
	}
	return out
}
