package procfs

import (
	"bytes"
	"errors"
	"strconv"
)

// Stat — поля /proc/<pid>/stat, нужные сэмплеру (man 5 proc_pid_stat; номера полей — с 1).
// Времена — в тиках часов (CLK_TCK, обычно 100 Гц); перевод в секунды делает вызывающий.
type Stat struct {
	PID        int
	Comm       string
	State      byte
	PPID       int    // (4)
	MinFlt     uint64 // (10) малые отказы страниц самого процесса
	MajFlt     uint64 // (12) большие отказы (чтение с диска)
	UTime      uint64 // (14) пользовательское время, тики; включает все потоки процесса
	STime      uint64 // (15) системное время, тики
	CUTime     int64  // (16) время дождавшихся (wait) потомков, тики
	CSTime     int64  // (17)
	NumThreads int64  // (20)
	StartTime  uint64 // (22) момент запуска в тиках от загрузки — вместе с PID однозначно задаёт процесс
	VSize      uint64 // (23) байты
	RSSPages   int64  // (24) страницы
}

var errStatFormat = errors.New("неверный формат /proc/<pid>/stat")

// ParseStat разбирает содержимое /proc/<pid>/stat.
//
// Поле comm заключено в скобки и может содержать пробелы и даже ')', поэтому граница ищется по
// последней ')' в строке, а не разбиением по пробелам.
func ParseStat(data []byte) (Stat, error) {
	var s Stat
	open := bytes.IndexByte(data, '(')
	closing := bytes.LastIndexByte(data, ')')
	if open <= 0 || closing < open {
		return s, errStatFormat
	}
	pid, err := strconv.Atoi(string(bytes.TrimSpace(data[:open])))
	if err != nil {
		return s, &fieldError{"stat", "pid", err}
	}
	s.PID = pid
	s.Comm = string(data[open+1 : closing])

	rest := bytes.Fields(data[closing+1:])
	// rest[0] — поле (3) state; поле (n) находится в rest[n-3].
	const lastNeeded = 24
	if len(rest) < lastNeeded-2 {
		return s, errStatFormat
	}
	field := func(n int) []byte { return rest[n-3] }
	if len(field(3)) != 1 {
		return s, &fieldError{"stat", "state", errStatFormat}
	}
	s.State = field(3)[0]

	type target struct {
		n    int
		name string
		u    *uint64
		i    *int64
	}
	var ppid int64
	targets := []target{
		{4, "ppid", nil, &ppid},
		{10, "minflt", &s.MinFlt, nil},
		{12, "majflt", &s.MajFlt, nil},
		{14, "utime", &s.UTime, nil},
		{15, "stime", &s.STime, nil},
		{16, "cutime", nil, &s.CUTime},
		{17, "cstime", nil, &s.CSTime},
		{20, "num_threads", nil, &s.NumThreads},
		{22, "starttime", &s.StartTime, nil},
		{23, "vsize", &s.VSize, nil},
		{24, "rss", nil, &s.RSSPages},
	}
	for _, t := range targets {
		raw := string(field(t.n))
		if t.u != nil {
			v, err := strconv.ParseUint(raw, 10, 64)
			if err != nil {
				return s, &fieldError{"stat", t.name, err}
			}
			*t.u = v
		} else {
			v, err := strconv.ParseInt(raw, 10, 64)
			if err != nil {
				return s, &fieldError{"stat", t.name, err}
			}
			*t.i = v
		}
	}
	s.PPID = int(ppid)
	return s, nil
}

// ReadStat читает и разбирает /proc/<pid>/stat.
func (f FS) ReadStat(pid int) (Stat, error) {
	data, err := f.ReadFile(strconv.Itoa(pid), "stat")
	if err != nil {
		return Stat{}, err
	}
	return ParseStat(data)
}
