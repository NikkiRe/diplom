// Package procfs разбирает файлы Linux /proc, нужные сэмплеру: stat, status, io, smaps_rollup (с откатом на
// smaps для ядер < 4.14), net/dev, meminfo, /proc/stat, loadavg, auxv.
//
// Все пути строятся от корня FS.Root, поэтому разбор проверяется на фикстурах (снимках /proc) на любой ОС.
// Разборщики — чистые функции над []byte; методы FS только читают файл и вызывают разборщик.
package procfs

import (
	"bytes"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"syscall"
)

// FS — файловая система /proc с заданным корнем ("/proc" на живой системе, каталог фикстуры в тестах).
type FS struct {
	Root string
}

// New возвращает FS для корня root; пустой root означает "/proc".
func New(root string) FS {
	if root == "" {
		root = "/proc"
	}
	return FS{Root: root}
}

// Path склеивает путь внутри /proc.
func (f FS) Path(elem ...string) string {
	return filepath.Join(append([]string{f.Root}, elem...)...)
}

func (f FS) pidPath(pid int, elem ...string) string {
	return f.Path(append([]string{strconv.Itoa(pid)}, elem...)...)
}

// ReadFile читает файл /proc целиком. У файлов /proc нулевой размер в stat(2), поэтому os.ReadFile
// (читает до EOF) здесь подходит, а предвыделение по размеру — нет.
func (f FS) ReadFile(elem ...string) ([]byte, error) {
	return os.ReadFile(f.Path(elem...))
}

// IsGone сообщает, что процесс (или поток) завершился между перечислением и чтением: это нормальная
// гонка при сэмплировании, а не ошибка.
func IsGone(err error) bool {
	return errors.Is(err, fs.ErrNotExist) || errors.Is(err, syscall.ESRCH)
}

// IsPermission сообщает об отказе в доступе (smaps_rollup и io чужого пользователя без CAP_SYS_PTRACE).
func IsPermission(err error) bool {
	return errors.Is(err, fs.ErrPermission)
}

// ListPIDs возвращает PID всех процессов (числовые каталоги корня). Порядок — как в каталоге.
func (f FS) ListPIDs() ([]int, error) {
	entries, err := os.ReadDir(f.Root)
	if err != nil {
		return nil, err
	}
	pids := make([]int, 0, len(entries))
	for _, e := range entries {
		name := e.Name()
		if name == "" || name[0] < '0' || name[0] > '9' {
			continue
		}
		pid, err := strconv.Atoi(name)
		if err != nil || pid <= 0 {
			continue
		}
		pids = append(pids, pid)
	}
	return pids, nil
}

// ListTasks возвращает TID потоков процесса (/proc/<pid>/task).
func (f FS) ListTasks(pid int) ([]int, error) {
	entries, err := os.ReadDir(f.pidPath(pid, "task"))
	if err != nil {
		return nil, err
	}
	tids := make([]int, 0, len(entries))
	for _, e := range entries {
		tid, err := strconv.Atoi(e.Name())
		if err == nil && tid > 0 {
			tids = append(tids, tid)
		}
	}
	return tids, nil
}

// --- общие помощники разбора ---------------------------------------------------------------------------

// forEachKV вызывает fn для каждой строки вида "Key:   value [unit]" (status, smaps, meminfo).
// Значение — первое поле после двоеточия; единица (kB) отбрасывается.
func forEachKV(data []byte, fn func(key, value []byte)) {
	for len(data) > 0 {
		var line []byte
		if i := bytes.IndexByte(data, '\n'); i >= 0 {
			line, data = data[:i], data[i+1:]
		} else {
			line, data = data, nil
		}
		colon := bytes.IndexByte(line, ':')
		if colon <= 0 {
			continue
		}
		key := line[:colon]
		rest := bytes.TrimLeft(line[colon+1:], " \t")
		if sp := bytes.IndexAny(rest, " \t"); sp >= 0 {
			rest = rest[:sp]
		}
		fn(key, rest)
	}
}

func parseUint(b []byte) (uint64, error) {
	return strconv.ParseUint(string(b), 10, 64)
}

// fieldError — ошибка разбора с указанием файла и поля, чтобы в журнале было видно, что именно сломалось.
type fieldError struct {
	file, field string
	err         error
}

func (e *fieldError) Error() string {
	return fmt.Sprintf("%s: поле %s: %v", e.file, e.field, e.err)
}

func (e *fieldError) Unwrap() error { return e.err }
