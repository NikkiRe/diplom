package procfs

import "strconv"

// IO — счётчики /proc/<pid>/io (байты и число системных вызовов). Счётчики суммируются по всем потокам
// процесса, включая завершившиеся. read_bytes/write_bytes — обращения к блочному уровню (флеш-память
// платы); rchar/wchar — всё, что прошло через read/write, включая сокеты и page cache.
type IO struct {
	RChar               uint64
	WChar               uint64
	SyscR               uint64
	SyscW               uint64
	ReadBytes           uint64
	WriteBytes          uint64
	CancelledWriteBytes uint64
}

// ParseIO разбирает /proc/<pid>/io.
func ParseIO(data []byte) IO {
	var io IO
	forEachKV(data, func(key, value []byte) {
		var dst *uint64
		switch string(key) {
		case "rchar":
			dst = &io.RChar
		case "wchar":
			dst = &io.WChar
		case "syscr":
			dst = &io.SyscR
		case "syscw":
			dst = &io.SyscW
		case "read_bytes":
			dst = &io.ReadBytes
		case "write_bytes":
			dst = &io.WriteBytes
		case "cancelled_write_bytes":
			dst = &io.CancelledWriteBytes
		default:
			return
		}
		if v, err := parseUint(value); err == nil {
			*dst = v
		}
	})
	return io
}

// ReadIO читает /proc/<pid>/io (нужны права ptrace на процесс).
func (f FS) ReadIO(pid int) (IO, error) {
	data, err := f.ReadFile(strconv.Itoa(pid), "io")
	if err != nil {
		return IO{}, err
	}
	return ParseIO(data), nil
}
