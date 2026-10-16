package procfs

import "strconv"

// Status — поля /proc/<pid>/status (или /proc/<pid>/task/<tid>/status). Память — в КиБ.
//
// Важно: счётчики переключений контекста в status относятся к одной задаче (потоку), а не ко всему
// процессу: в /proc/<pid>/status — только главный поток. Для многопоточного процесса (Go) сумму по
// потокам даёт обход /proc/<pid>/task/*/status (см. SumTaskCtxsw).
type Status struct {
	Name        string
	VmRSSKiB    uint64
	VmHWMKiB    uint64 // пик RSS за жизнь процесса
	RssAnonKiB  uint64
	RssFileKiB  uint64
	RssShmemKiB uint64
	VmSwapKiB   uint64
	Threads     uint64
	VolCtxsw    uint64 // voluntary_ctxt_switches — процесс уступил процессор сам (ожидание, sleep)
	NonvolCtxsw uint64 // nonvoluntary_ctxt_switches — вытеснен планировщиком
	HasCtxsw    bool
}

// ParseStatus разбирает status; неизвестные строки пропускаются.
func ParseStatus(data []byte) Status {
	var s Status
	forEachKV(data, func(key, value []byte) {
		var dst *uint64
		switch string(key) {
		case "Name":
			s.Name = string(value)
			return
		case "VmRSS":
			dst = &s.VmRSSKiB
		case "VmHWM":
			dst = &s.VmHWMKiB
		case "RssAnon":
			dst = &s.RssAnonKiB
		case "RssFile":
			dst = &s.RssFileKiB
		case "RssShmem":
			dst = &s.RssShmemKiB
		case "VmSwap":
			dst = &s.VmSwapKiB
		case "Threads":
			dst = &s.Threads
		case "voluntary_ctxt_switches":
			dst = &s.VolCtxsw
			s.HasCtxsw = true
		case "nonvoluntary_ctxt_switches":
			dst = &s.NonvolCtxsw
			s.HasCtxsw = true
		default:
			return
		}
		if v, err := parseUint(value); err == nil {
			*dst = v
		}
	})
	return s
}

// ReadStatus читает /proc/<pid>/status.
func (f FS) ReadStatus(pid int) (Status, error) {
	data, err := f.ReadFile(strconv.Itoa(pid), "status")
	if err != nil {
		return Status{}, err
	}
	return ParseStatus(data), nil
}

// SumTaskCtxsw суммирует переключения контекста по живым потокам процесса. Завершившиеся потоки
// не учитываются (их счётчики ядро в /proc не сохраняет) — для долгоживущих потоков Go это несущественно.
// Возвращает число прочитанных потоков; 0 потоков при отсутствии процесса — ошибка IsGone.
func (f FS) SumTaskCtxsw(pid int) (vol, nonvol uint64, tasks int, err error) {
	tids, err := f.ListTasks(pid)
	if err != nil {
		return 0, 0, 0, err
	}
	for _, tid := range tids {
		data, err := f.ReadFile(strconv.Itoa(pid), "task", strconv.Itoa(tid), "status")
		if err != nil {
			if IsGone(err) {
				continue // поток завершился между перечислением и чтением
			}
			return vol, nonvol, tasks, err
		}
		st := ParseStatus(data)
		vol += st.VolCtxsw
		nonvol += st.NonvolCtxsw
		tasks++
	}
	return vol, nonvol, tasks, nil
}
