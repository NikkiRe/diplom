// Package collect собирает сэмпл ресурсов выбранных процессов: с Linux /proc и cgroup v2 (ProcfsCollector)
// или упрощённо через ps на macOS (PSCollector). Формат записей JSONL описан в README компонента.
//
// Поля, которых нет у источника (PSS, ввод-вывод, переключения контекста у ps), в JSON отсутствуют, а не
// равны нулю: ноль — это измерение, отсутствие — его нет.
package collect

import (
	"context"
	"sort"
)

// IOCounters — счётчики /proc/<pid>/io, байты и число вызовов.
type IOCounters struct {
	RChar               uint64 `json:"rchar"`
	WChar               uint64 `json:"wchar"`
	SyscR               uint64 `json:"syscr"`
	SyscW               uint64 `json:"syscw"`
	ReadBytes           uint64 `json:"read_bytes"`
	WriteBytes          uint64 `json:"write_bytes"`
	CancelledWriteBytes uint64 `json:"cancelled_write_bytes"`
}

func (c *IOCounters) add(o IOCounters) {
	c.RChar += o.RChar
	c.WChar += o.WChar
	c.SyscR += o.SyscR
	c.SyscW += o.SyscW
	c.ReadBytes += o.ReadBytes
	c.WriteBytes += o.WriteBytes
	c.CancelledWriteBytes += o.CancelledWriteBytes
}

// Ctxsw — переключения контекста (сумма по живым потокам).
type Ctxsw struct {
	Voluntary   uint64 `json:"vol"`
	Involuntary uint64 `json:"invol"`
}

// ProcSample — один процесс. Счётчики (CPU, отказы страниц, IO, ctxsw) — накопленные с запуска процесса.
type ProcSample struct {
	PID        int    `json:"pid"`
	PPID       int    `json:"ppid"`
	Comm       string `json:"comm"`
	StartTicks uint64 `json:"start_ticks,omitempty"` // starttime из stat: (pid, start_ticks) различает повторно выданный PID
	Threads    int64  `json:"threads,omitempty"`

	RSSKiB      uint64  `json:"rss_kib"`
	VSZKiB      uint64  `json:"vsz_kib"`
	HWMKiB      *uint64 `json:"hwm_kib,omitempty"` // VmHWM: пик RSS процесса
	PSSKiB      *uint64 `json:"pss_kib,omitempty"`
	PSSAnonKiB  *uint64 `json:"pss_anon_kib,omitempty"`
	PSSFileKiB  *uint64 `json:"pss_file_kib,omitempty"`
	PSSShmemKiB *uint64 `json:"pss_shmem_kib,omitempty"`
	SwapKiB     *uint64 `json:"swap_kib,omitempty"`
	SwapPSSKiB  *uint64 `json:"swap_pss_kib,omitempty"`

	CPUUserS     float64  `json:"cpu_user_s"`
	CPUSysS      float64  `json:"cpu_sys_s"`
	CPUChildrenS *float64 `json:"cpu_children_s,omitempty"` // cutime+cstime: дождавшиеся (wait) потомки
	MinFlt       *uint64  `json:"minflt,omitempty"`
	MajFlt       *uint64  `json:"majflt,omitempty"`

	IO    *IOCounters `json:"io,omitempty"`
	Ctxsw *Ctxsw      `json:"ctxsw,omitempty"`
}

// Totals — сумма по группе процессов. Опциональное поле присутствует, если оно было хотя бы у одного
// процесса группы; Missing считает процессы, у которых его не было (например, EACCES на smaps_rollup),
// — тогда сумма неполная, и это видно в записи.
type Totals struct {
	Procs   int   `json:"procs"`
	Threads int64 `json:"threads,omitempty"`

	RSSKiB      uint64  `json:"rss_kib"`
	VSZKiB      uint64  `json:"vsz_kib"`
	PSSKiB      *uint64 `json:"pss_kib,omitempty"`
	PSSAnonKiB  *uint64 `json:"pss_anon_kib,omitempty"`
	PSSFileKiB  *uint64 `json:"pss_file_kib,omitempty"`
	PSSShmemKiB *uint64 `json:"pss_shmem_kib,omitempty"`
	SwapKiB     *uint64 `json:"swap_kib,omitempty"`
	SwapPSSKiB  *uint64 `json:"swap_pss_kib,omitempty"`

	CPUUserS     float64  `json:"cpu_user_s"`
	CPUSysS      float64  `json:"cpu_sys_s"`
	CPUS         float64  `json:"cpu_s"` // user+sys живых процессов группы
	CPUChildrenS *float64 `json:"cpu_children_s,omitempty"`
	// CPUPct — загрузка за интервал с прошлого сэмпла в процентах одного ядра (может быть > 100);
	// считается по cpu_s + cpu_children_s, отрицательные приращения (завершение процесса) обрезаются до 0.
	CPUPct *float64 `json:"cpu_pct,omitempty"`
	MinFlt *uint64  `json:"minflt,omitempty"`
	MajFlt *uint64  `json:"majflt,omitempty"`

	IO    *IOCounters `json:"io,omitempty"`
	Ctxsw *Ctxsw      `json:"ctxsw,omitempty"`

	Missing map[string]int `json:"missing,omitempty"`
}

// CPUTotalS — накопленное время CPU группы вместе с дождавшимися потомками.
func (t Totals) CPUTotalS() float64 {
	if t.CPUChildrenS != nil {
		return t.CPUS + *t.CPUChildrenS
	}
	return t.CPUS
}

func addOpt(dst **uint64, v *uint64) {
	if v == nil {
		return
	}
	if *dst == nil {
		*dst = new(uint64)
	}
	**dst += *v
}

// Sum складывает процессы в Totals.
func Sum(procs []ProcSample) Totals {
	var t Totals
	missing := map[string]int{}
	miss := func(ok bool, name string) {
		if !ok {
			missing[name]++
		}
	}
	for i := range procs {
		p := &procs[i]
		t.Procs++
		t.Threads += p.Threads
		t.RSSKiB += p.RSSKiB
		t.VSZKiB += p.VSZKiB
		addOpt(&t.PSSKiB, p.PSSKiB)
		addOpt(&t.PSSAnonKiB, p.PSSAnonKiB)
		addOpt(&t.PSSFileKiB, p.PSSFileKiB)
		addOpt(&t.PSSShmemKiB, p.PSSShmemKiB)
		addOpt(&t.SwapKiB, p.SwapKiB)
		addOpt(&t.SwapPSSKiB, p.SwapPSSKiB)
		addOpt(&t.MinFlt, p.MinFlt)
		addOpt(&t.MajFlt, p.MajFlt)
		t.CPUUserS += p.CPUUserS
		t.CPUSysS += p.CPUSysS
		if p.CPUChildrenS != nil {
			if t.CPUChildrenS == nil {
				t.CPUChildrenS = new(float64)
			}
			*t.CPUChildrenS += *p.CPUChildrenS
		}
		if p.IO != nil {
			if t.IO == nil {
				t.IO = &IOCounters{}
			}
			t.IO.add(*p.IO)
		}
		if p.Ctxsw != nil {
			if t.Ctxsw == nil {
				t.Ctxsw = &Ctxsw{}
			}
			t.Ctxsw.Voluntary += p.Ctxsw.Voluntary
			t.Ctxsw.Involuntary += p.Ctxsw.Involuntary
		}
		miss(p.PSSKiB != nil, "pss")
		miss(p.IO != nil, "io")
		miss(p.Ctxsw != nil, "ctxsw")
	}
	t.CPUS = t.CPUUserS + t.CPUSysS
	// Отсутствие поля у всех процессов — свойство источника (ps), а не пропуск: не помечаем.
	for name, n := range missing {
		if n == t.Procs {
			delete(missing, name)
		}
	}
	if len(missing) > 0 {
		t.Missing = missing
	}
	return t
}

// NetSample — счётчики интерфейса (с момента его поднятия).
type NetSample struct {
	RxBytes   uint64 `json:"rx_bytes"`
	TxBytes   uint64 `json:"tx_bytes"`
	RxPackets uint64 `json:"rx_packets"`
	TxPackets uint64 `json:"tx_packets"`
	RxErrs    uint64 `json:"rx_errs"`
	TxErrs    uint64 `json:"tx_errs"`
	RxDrop    uint64 `json:"rx_drop"`
	TxDrop    uint64 `json:"tx_drop"`
}

// CgroupSample — счётчики группы cgroup v2 (иерархические: включают вложенные группы).
type CgroupSample struct {
	Procs            int               `json:"procs"`
	MemoryCurrentKiB uint64            `json:"memory_current_kib"`
	MemoryPeakKiB    *uint64           `json:"memory_peak_kib,omitempty"` // ядро ≥ 5.19
	MemoryMaxKiB     *uint64           `json:"memory_max_kib,omitempty"`  // нет — предела нет
	CPUUsageS        float64           `json:"cpu_usage_s"`
	CPUUserS         float64           `json:"cpu_user_s"`
	CPUSysS          float64           `json:"cpu_sys_s"`
	NrThrottled      uint64            `json:"nr_throttled"`
	ThrottledS       float64           `json:"throttled_s"`
	MemoryEvents     map[string]uint64 `json:"memory_events,omitempty"`
	MemoryStat       map[string]uint64 `json:"memory_stat,omitempty"` // подмножество memory.stat, как в ядре (байты / штуки)
	IO               map[string]uint64 `json:"io,omitempty"`          // сумма io.stat по устройствам
}

// SystemSample — состояние всей системы (режим --system: прогон «плата без агента» для вычитания фона).
type SystemSample struct {
	CPU             map[string]float64 `json:"cpu_s"` // накопленные секунды /proc/stat по видам
	MemTotalKiB     uint64             `json:"mem_total_kib"`
	MemAvailableKiB uint64             `json:"mem_available_kib"`
	MemFreeKiB      uint64             `json:"mem_free_kib"`
	CachedKiB       uint64             `json:"cached_kib"`
	SwapTotalKiB    uint64             `json:"swap_total_kib"`
	SwapFreeKiB     uint64             `json:"swap_free_kib"`
	Load1           float64            `json:"load1"`
	Load5           float64            `json:"load5"`
	Load15          float64            `json:"load15"`
}

// Snapshot — сырой результат одного опроса источника.
type Snapshot struct {
	Procs      []ProcSample // выбранные процессы, по возрастанию PID
	Roots      []int        // живые корни из Target.PIDs (с тем же start_ticks, что при старте)
	Cgroup     *CgroupSample
	CgroupGone bool // каталог cgroup удалён
	Net        map[string]NetSample
	Thermal    map[string]float64
	System     *SystemSample
	Errors     map[string]int // "источник:вид" -> число; гонки завершения процессов сюда не попадают
}

func (s *Snapshot) noteError(source string, err error) {
	if err == nil {
		return
	}
	if s.Errors == nil {
		s.Errors = map[string]int{}
	}
	s.Errors[source+":"+errKind(err)]++
}

// Target — что измеряем.
type Target struct {
	PIDs        []int  // корни деревьев процессов (с потомками, если Children)
	Children    bool   // включать потомков корней
	Cgroup      string // каталог cgroup v2: все процессы группы и вложенных групп
	ExcludePIDs []int  // не учитывать (сам сэмплер, если он попал в группу)
	Ifaces      []string
	NetSelf     bool // счётчики сети из netns сэмплера, а не корневого процесса
	System      bool // писать SystemSample
}

// Collector — источник сэмплов.
type Collector interface {
	Backend() string
	// Avail — какие группы полей даёт источник (для заголовка JSONL).
	Avail() []string
	Collect(ctx context.Context) (Snapshot, error)
}

// SelectTree возвращает объединение поддеревьев с корнями roots по карте parent (pid -> ppid).
// Корни, которых нет в карте, пропускаются. Результат отсортирован.
func SelectTree(parent map[int]int, roots []int, withChildren bool) []int {
	children := map[int][]int{}
	for pid, ppid := range parent {
		if pid != ppid {
			children[ppid] = append(children[ppid], pid)
		}
	}
	seen := map[int]bool{}
	var queue []int
	for _, r := range roots {
		if _, ok := parent[r]; ok && !seen[r] {
			seen[r] = true
			queue = append(queue, r)
		}
	}
	for i := 0; i < len(queue) && withChildren; i++ {
		for _, c := range children[queue[i]] {
			if !seen[c] {
				seen[c] = true
				queue = append(queue, c)
			}
		}
	}
	out := make([]int, 0, len(seen))
	for pid := range seen {
		out = append(out, pid)
	}
	sort.Ints(out)
	return out
}

func includeIface(name string, filter []string) bool {
	if len(filter) == 0 {
		return name != "lo" && name != "lo0"
	}
	for _, f := range filter {
		if f == name {
			return true
		}
	}
	return false
}
