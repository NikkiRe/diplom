// Package cgroup читает счётчики cgroup v2 (единая иерархия, /sys/fs/cgroup): memory.current,
// memory.peak, memory.max, memory.stat, memory.events, cpu.stat, io.stat, cgroup.procs.
//
// Счётчики cgroup иерархические: memory.current и cpu.stat каталога включают все вложенные группы.
// В методике (§12.3) каждая система работает в своём slice; его счётчики — граница измерения
// «оркестратор + серверы».
package cgroup

import (
	"bytes"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Group — каталог cgroup v2.
type Group struct {
	Path string
}

func (g Group) read(name string) ([]byte, error) {
	return os.ReadFile(filepath.Join(g.Path, name))
}

// Exists — каталог группы существует (группа не удалена).
func (g Group) Exists() bool {
	st, err := os.Stat(g.Path)
	return err == nil && st.IsDir()
}

// readUint читает файл с одним числом. "max" (нет ограничения) возвращается как ok=false без ошибки.
func (g Group) readUint(name string) (v uint64, ok bool, err error) {
	data, err := g.read(name)
	if err != nil {
		return 0, false, err
	}
	s := strings.TrimSpace(string(data))
	if s == "max" {
		return 0, false, nil
	}
	v, err = strconv.ParseUint(s, 10, 64)
	if err != nil {
		return 0, false, err
	}
	return v, true, nil
}

// MemoryCurrent — текущий заряд памяти группы, байты (анонимная память, page cache, ядро).
func (g Group) MemoryCurrent() (uint64, error) {
	v, _, err := g.readUint("memory.current")
	return v, err
}

// MemoryPeak — пик memory.current с создания группы (ядро ≥ 5.19). ok=false, если файла нет.
func (g Group) MemoryPeak() (v uint64, ok bool, err error) {
	v, ok, err = g.readUint("memory.peak")
	if errors.Is(err, fs.ErrNotExist) {
		return 0, false, nil
	}
	return v, ok, err
}

// MemoryMax — жёсткий предел (memory.max); ok=false — предела нет ("max") или файла нет (корень).
func (g Group) MemoryMax() (v uint64, ok bool, err error) {
	v, ok, err = g.readUint("memory.max")
	if errors.Is(err, fs.ErrNotExist) {
		return 0, false, nil
	}
	return v, ok, err
}

// ParseFlatKeyed разбирает файлы формата "ключ значение" по строке (cpu.stat, memory.stat, memory.events).
func ParseFlatKeyed(data []byte) map[string]uint64 {
	out := map[string]uint64{}
	for _, line := range bytes.Split(data, []byte{'\n'}) {
		f := bytes.Fields(line)
		if len(f) != 2 {
			continue
		}
		if v, err := strconv.ParseUint(string(f[1]), 10, 64); err == nil {
			out[string(f[0])] = v
		}
	}
	return out
}

// CPUStat — cpu.stat: usage_usec, user_usec, system_usec, nr_periods, nr_throttled, throttled_usec ...
func (g Group) CPUStat() (map[string]uint64, error) {
	data, err := g.read("cpu.stat")
	if err != nil {
		return nil, err
	}
	return ParseFlatKeyed(data), nil
}

// MemoryStat — memory.stat (байты для объёмов, штуки для событий pgfault/pgmajfault ...).
func (g Group) MemoryStat() (map[string]uint64, error) {
	data, err := g.read("memory.stat")
	if err != nil {
		return nil, err
	}
	return ParseFlatKeyed(data), nil
}

// MemoryEvents — memory.events: low, high, max, oom, oom_kill (важно для прогонов с memory.max 256 МиБ).
func (g Group) MemoryEvents() (map[string]uint64, error) {
	data, err := g.read("memory.events")
	if err != nil {
		return nil, err
	}
	return ParseFlatKeyed(data), nil
}

// ParseIOStat суммирует io.stat по всем блочным устройствам: rbytes, wbytes, rios, wios, dbytes, dios.
// Формат строки: "MAJ:MIN rbytes=N wbytes=N rios=N wios=N dbytes=N dios=N".
func ParseIOStat(data []byte) map[string]uint64 {
	out := map[string]uint64{}
	for _, line := range bytes.Split(data, []byte{'\n'}) {
		f := bytes.Fields(line)
		for _, kv := range f[min(1, len(f)):] {
			eq := bytes.IndexByte(kv, '=')
			if eq <= 0 {
				continue
			}
			if v, err := strconv.ParseUint(string(kv[eq+1:]), 10, 64); err == nil {
				out[string(kv[:eq])] += v
			}
		}
	}
	return out
}

// IOStat читает io.stat; при выключенном контроллере io файла нет — возвращается пустая карта.
func (g Group) IOStat() (map[string]uint64, error) {
	data, err := g.read("io.stat")
	if errors.Is(err, fs.ErrNotExist) {
		return map[string]uint64{}, nil
	}
	if err != nil {
		return nil, err
	}
	return ParseIOStat(data), nil
}

// Procs возвращает PID процессов группы и (recursive=true) всех вложенных групп.
func (g Group) Procs(recursive bool) ([]int, error) {
	var pids []int
	readProcs := func(dir string) error {
		data, err := os.ReadFile(filepath.Join(dir, "cgroup.procs"))
		if err != nil {
			return err
		}
		for _, f := range bytes.Fields(data) {
			if pid, err := strconv.Atoi(string(f)); err == nil && pid > 0 {
				pids = append(pids, pid)
			}
		}
		return nil
	}
	if !recursive {
		return pids, readProcs(g.Path)
	}
	err := filepath.WalkDir(g.Path, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			if path != g.Path && errors.Is(err, fs.ErrNotExist) {
				return nil // вложенная группа удалена во время обхода
			}
			return err
		}
		if !d.IsDir() {
			return nil
		}
		if err := readProcs(path); err != nil && !errors.Is(err, fs.ErrNotExist) {
			return err
		}
		return nil
	})
	return pids, err
}

// ParseProcCgroup извлекает путь группы v2 из /proc/<pid>/cgroup (строка "0::/путь").
func ParseProcCgroup(data []byte) (string, error) {
	for _, line := range bytes.Split(data, []byte{'\n'}) {
		if bytes.HasPrefix(line, []byte("0::")) {
			return string(bytes.TrimSpace(line[3:])), nil
		}
	}
	return "", errors.New("/proc/<pid>/cgroup: нет записи cgroup v2 (0::)")
}
