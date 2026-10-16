package procfs

import (
	"bytes"
	"errors"
	"strconv"
)

// CPUTimes — первая строка /proc/stat ("cpu ..."), тики USER_HZ по всем процессорам.
type CPUTimes struct {
	User, Nice, System, Idle, IOWait, IRQ, SoftIRQ, Steal uint64
}

// Busy — всё, кроме простоя и ожидания ввода-вывода.
func (c CPUTimes) Busy() uint64 { return c.User + c.Nice + c.System + c.IRQ + c.SoftIRQ + c.Steal }

// Total — сумма всех учтённых полей.
func (c CPUTimes) Total() uint64 { return c.Busy() + c.Idle + c.IOWait }

// ParseCPUStat разбирает строку "cpu" в /proc/stat (поля guest уже входят в user и не добавляются).
func ParseCPUStat(data []byte) (CPUTimes, error) {
	for _, line := range bytes.Split(data, []byte{'\n'}) {
		f := bytes.Fields(line)
		if len(f) < 9 || string(f[0]) != "cpu" {
			continue
		}
		var v [8]uint64
		for i := range v {
			n, err := strconv.ParseUint(string(f[i+1]), 10, 64)
			if err != nil {
				return CPUTimes{}, &fieldError{"stat", "cpu", err}
			}
			v[i] = n
		}
		return CPUTimes{v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7]}, nil
	}
	return CPUTimes{}, errors.New("/proc/stat: нет строки cpu")
}

// ParseMeminfo разбирает /proc/meminfo в карту "поле → КиБ" (поля без единицы, например HugePages_Total,
// тоже попадают в карту как есть).
func ParseMeminfo(data []byte) map[string]uint64 {
	out := map[string]uint64{}
	forEachKV(data, func(key, value []byte) {
		if v, err := parseUint(value); err == nil {
			out[string(key)] = v
		}
	})
	return out
}

// Loadavg — средняя загрузка за 1, 5, 15 минут.
type Loadavg struct{ Load1, Load5, Load15 float64 }

// ParseLoadavg разбирает /proc/loadavg.
func ParseLoadavg(data []byte) (Loadavg, error) {
	f := bytes.Fields(data)
	if len(f) < 3 {
		return Loadavg{}, errors.New("/proc/loadavg: мало полей")
	}
	var v [3]float64
	for i := range v {
		x, err := strconv.ParseFloat(string(f[i]), 64)
		if err != nil {
			return Loadavg{}, &fieldError{"loadavg", strconv.Itoa(i), err}
		}
		v[i] = x
	}
	return Loadavg{v[0], v[1], v[2]}, nil
}
