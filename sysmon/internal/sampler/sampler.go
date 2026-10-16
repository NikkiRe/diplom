// Package sampler — цикл сэмплирования: заголовок JSONL, сэмпл раз в период, итоговая запись с
// накладными расходами самого сэмплера.
package sampler

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"math"
	"sort"
	"time"

	"github.com/local/sysmon/internal/collect"
	"github.com/local/sysmon/internal/selfstat"
)

// Schema — версия формата записей. Меняется при несовместимом изменении полей.
const Schema = "sysmon.v1"

// Header — первая строка JSONL.
type Header struct {
	Type        string   `json:"type"` // "header"
	Schema      string   `json:"schema"`
	Version     string   `json:"version"`
	Backend     string   `json:"backend"` // procfs | ps
	Avail       []string `json:"avail"`   // группы полей, которые даёт источник
	Host        string   `json:"host"`
	OS          string   `json:"os"`
	Arch        string   `json:"arch"`
	Kernel      string   `json:"kernel,omitempty"`
	NumCPU      int      `json:"num_cpu"`
	PageSize    int      `json:"page_size"` // байты; методика требует указывать рядом с числами памяти
	ClkTck      int      `json:"clk_tck,omitempty"`
	IntervalS   float64  `json:"interval_s"`
	StartedWall float64  `json:"started_wall"` // Unix-время, с
	SysmonPID   int      `json:"sysmon_pid"`
	Target      Target   `json:"target"`
	ProcRoot    string   `json:"proc_root,omitempty"`
	SysRoot     string   `json:"sys_root,omitempty"`
}

// Target — описание цели в заголовке.
type Target struct {
	PIDs     []int    `json:"pids,omitempty"`
	Children bool     `json:"children"`
	Cgroup   string   `json:"cgroup,omitempty"`
	Cmd      []string `json:"cmd,omitempty"`
	Ifaces   []string `json:"ifaces,omitempty"`
	NetSelf  bool     `json:"net_self,omitempty"`
}

// Self — собственные расходы сэмплера на момент сэмпла.
type Self struct {
	Status    string         `json:"status"`          // available | unknown
	CPUS      *float64       `json:"cpu_s,omitempty"` // накопленное время CPU процесса сэмплера
	MaxRSSKiB *uint64        `json:"max_rss_kib,omitempty"`
	RSSKiB    uint64         `json:"rss_kib,omitempty"`
	PSSKiB    uint64         `json:"pss_kib,omitempty"`
	CollectMS float64        `json:"collect_ms"`      // длительность опроса источника
	Ctxsw     *collect.Ctxsw `json:"ctxsw,omitempty"` // getrusage, all threads including exited ones
	Wakeups   *uint64        `json:"wakeups"`         // unknown: context switches are not a wakeup counter
	Error     string         `json:"error,omitempty"`
}

// Sample — строка сэмпла.
type Sample struct {
	Type    string                       `json:"type"` // "sample"
	Seq     int                          `json:"seq"`
	TsWall  float64                      `json:"ts_wall"` // Unix-время, с (для сопоставления с трассой edgectx)
	TS      float64                      `json:"t_s"`     // монотонное время от старта, с
	DtS     float64                      `json:"dt_s"`    // с прошлого сэмпла (0 у первого)
	Tree    collect.Totals               `json:"tree"`    // все выбранные процессы
	Root    collect.Totals               `json:"root"`    // только корневые процессы («оркестратор»)
	Procs   []collect.ProcSample         `json:"procs,omitempty"`
	Cgroup  *collect.CgroupSample        `json:"cgroup,omitempty"`
	Net     map[string]collect.NetSample `json:"net,omitempty"`
	Thermal map[string]float64           `json:"thermal_c,omitempty"`
	System  *collect.SystemSample        `json:"system,omitempty"`
	Self    Self                         `json:"self"`
	Errors  map[string]int               `json:"errors,omitempty"`
}

// Overhead — итог накладных расходов сэмплера.
type Overhead struct {
	SelfStatus    string         `json:"self_status"` // available | unknown
	CPUS          *float64       `json:"cpu_s,omitempty"`
	CPUFrac       *float64       `json:"cpu_frac,omitempty"` // доля одного ядра за прогон
	CollectMSMean float64        `json:"collect_ms_mean"`
	CollectMSP50  float64        `json:"collect_ms_p50"`
	CollectMSP95  float64        `json:"collect_ms_p95"`
	CollectMSMax  float64        `json:"collect_ms_max"`
	MaxRSSKiB     *uint64        `json:"max_rss_kib,omitempty"`
	PSSKiBLast    uint64         `json:"pss_kib_last,omitempty"`
	Ctxsw         *collect.Ctxsw `json:"ctxsw,omitempty"`
	Wakeups       *uint64        `json:"wakeups"`
}

// End — последняя строка JSONL.
type End struct {
	Type     string            `json:"type"` // "end"
	TsWall   float64           `json:"ts_wall"`
	TS       float64           `json:"t_s"`
	Samples  int               `json:"samples"`
	Reason   string            `json:"reason"` // count | duration | signal | target_exited | cgroup_gone | cmd_exited | error
	Error    string            `json:"error,omitempty"`
	ExitCode *int              `json:"exit_code,omitempty"` // код выхода команды (режим -- cmd)
	Overhead Overhead          `json:"overhead"`
	OOM      collect.OOMSample `json:"oom"`
}

// Причины остановки.
const (
	StopCount        = "count"
	StopDuration     = "duration"
	StopSignal       = "signal"
	StopTargetExited = "target_exited"
	StopCgroupGone   = "cgroup_gone"
	StopCmdExited    = "cmd_exited"
	StopError        = "error"
)

// Sampler — параметры цикла.
type Sampler struct {
	Collector collect.Collector
	Interval  time.Duration
	Count     int           // 0 — без ограничения
	Duration  time.Duration // 0 — без ограничения
	PerProc   bool          // писать процессы поимённо
	RootPIDs  []int         // для группы «root»; пусто — корень не выделяется
	// StopWhenTargetGone — остановиться, когда все корни завершились (режим --pid / --cmd).
	StopWhenTargetGone bool
	// Done — внешний сигнал остановки (завершение команды); причина берётся из DoneReason.
	Done       <-chan struct{}
	DoneReason func() (reason string, exitCode *int)
	Now        func() time.Time // подмена часов в тестах
	ReadSelf   func() (selfstat.Usage, error)
}

// Run пишет заголовок, сэмплы и итог в w. Возвращает причину остановки. Ошибка записи прерывает цикл.
// Отмена ctx (сигнал) — штатная остановка с причиной "signal".
func (s *Sampler) Run(ctx context.Context, header Header, w io.Writer) (End, error) {
	now := s.Now
	if now == nil {
		now = time.Now
	}
	readSelf := s.ReadSelf
	if readSelf == nil {
		readSelf = selfstat.Read
	}
	bw := bufio.NewWriter(w)
	enc := json.NewEncoder(bw)
	enc.SetEscapeHTML(false)
	write := func(v any) error {
		if err := enc.Encode(v); err != nil {
			return err
		}
		return bw.Flush() // каждая строка сразу на диск: при аварии прогона данные остаются
	}

	start := now()
	header.Type = "header"
	header.Schema = Schema
	header.Backend = s.Collector.Backend()
	header.Avail = s.Collector.Avail()
	header.IntervalS = s.Interval.Seconds()
	header.StartedWall = unixSeconds(start)
	if err := write(header); err != nil {
		return End{}, err
	}

	var (
		collectMS []float64
		prevTree  *collect.Totals
		prevT     float64
		lastSelf  *selfstat.Usage
		end       = End{Type: "end"}
	)
	oomReporter, hasOOMReporter := s.Collector.(interface{ ObserveOOM() collect.OOMSample })
	if hasOOMReporter {
		oomReporter.ObserveOOM() // baseline before the target disappears
	}
	ticker := time.NewTicker(s.Interval)
	defer ticker.Stop()
	var deadline <-chan time.Time // nil-канал никогда не срабатывает: без --duration ждём только тиков
	if s.Duration > 0 {
		timer := time.NewTimer(s.Duration)
		defer timer.Stop()
		deadline = timer.C
	}

	finish := func(reason string) (End, error) {
		t := now()
		end.TsWall = unixSeconds(t)
		end.TS = round(t.Sub(start).Seconds(), 6)
		end.Reason = reason
		end.Samples = len(collectMS)
		lastSelf = nil
		if u, err := readSelf(); err == nil {
			lastSelf = &u
		}
		end.Overhead = overhead(lastSelf, collectMS, end.TS)
		end.OOM = collect.OOMSample{Status: "unknown", Scope: "cgroup", Error: "backend has no cgroup v2 counters"}
		if hasOOMReporter {
			end.OOM = oomReporter.ObserveOOM()
		}
		return end, write(end)
	}
	finishDone := func() (End, error) {
		reason, code := StopCmdExited, (*int)(nil)
		if s.DoneReason != nil {
			reason, code = s.DoneReason()
		}
		end.ExitCode = code
		return finish(reason)
	}

	for seq := 0; ; seq++ {
		select {
		case <-s.Done:
			return finishDone()
		default:
		}
		t0 := now()
		snap, err := s.Collector.Collect(ctx)
		collectDur := now().Sub(t0)
		if ctx.Err() != nil {
			return finish(StopSignal)
		}
		if err != nil {
			end.Error = err.Error()
			return finish(StopError)
		}
		// An empty terminal snapshot describes absence of a target, not a
		// measured zero-memory/zero-CPU workload. Keep it out of the time series.
		if s.StopWhenTargetGone && len(snap.Roots) == 0 && len(s.RootPIDs) > 0 {
			select {
			case <-s.Done:
				return finishDone()
			default:
				return finish(StopTargetExited)
			}
		}
		if snap.CgroupGone {
			return finish(StopCgroupGone)
		}
		ms := float64(collectDur.Microseconds()) / 1000
		collectMS = append(collectMS, ms)
		ts := t0.Sub(start).Seconds()

		sample := Sample{Type: "sample", Seq: seq, TsWall: unixSeconds(t0), TS: round(ts, 6), Errors: snap.Errors}
		if prevTree != nil {
			sample.DtS = round(ts-prevT, 6)
		}
		sample.Tree = collect.Sum(snap.Procs)
		sample.Root = collect.Sum(filterRoots(snap.Procs, s.RootPIDs))
		if prevTree != nil && sample.DtS > 0 {
			d := sample.Tree.CPUTotalS() - prevTree.CPUTotalS()
			pct := round(100*math.Max(d, 0)/sample.DtS, 3)
			sample.Tree.CPUPct = &pct
		}
		if s.PerProc {
			sample.Procs = snap.Procs
		}
		sample.Cgroup, sample.Net, sample.Thermal, sample.System = snap.Cgroup, snap.Net, snap.Thermal, snap.System
		sample.Self = Self{Status: "unknown", CollectMS: ms}
		if u, err := readSelf(); err == nil {
			cpu := round(u.CPUS(), 6)
			sample.Self = Self{Status: "available", CPUS: &cpu, MaxRSSKiB: &u.MaxRSSKiB,
				RSSKiB: u.RSSKiB, PSSKiB: u.PSSKiB, CollectMS: ms,
				Ctxsw: &collect.Ctxsw{Voluntary: u.VolCtxsw, Involuntary: u.NonvolCtxsw}}
		} else {
			sample.Self.Error = err.Error()
		}
		if err := write(sample); err != nil {
			return End{}, err
		}
		tree := sample.Tree
		prevTree, prevT = &tree, ts

		switch {
		case s.Count > 0 && seq+1 >= s.Count:
			return finish(StopCount)
		}

		if s.Duration > 0 && now().Sub(start) >= s.Duration {
			return finish(StopDuration)
		}
		select {
		case <-ctx.Done():
			return finish(StopSignal)
		case <-s.Done:
			return finishDone()
		case <-deadline:
			return finish(StopDuration)
		case <-ticker.C:
		}
	}
}

func filterRoots(procs []collect.ProcSample, roots []int) []collect.ProcSample {
	if len(roots) == 0 {
		return nil
	}
	want := map[int]bool{}
	for _, r := range roots {
		want[r] = true
	}
	var out []collect.ProcSample
	for _, p := range procs {
		if want[p.PID] {
			out = append(out, p)
		}
	}
	return out
}

// overhead подводит итог. Время CPU — накопленное за всю жизнь процесса сэмплера (запуск занимает
// миллисекунды, отделять его нет смысла); collect_ms хранится по значению на сэмпл: 8 байт на сэмпл,
// т. е. ≈ 230 КиБ за сутки при периоде 1 с — пренебрежимо и не маскирует рост памяти измеряемых процессов.
func overhead(last *selfstat.Usage, collectMS []float64, elapsed float64) Overhead {
	o := Overhead{SelfStatus: "unknown"}
	if last != nil {
		cpu := round(last.CPUS(), 6)
		o.SelfStatus = "available"
		o.CPUS, o.MaxRSSKiB, o.PSSKiBLast = &cpu, &last.MaxRSSKiB, last.PSSKiB
		o.Ctxsw = &collect.Ctxsw{Voluntary: last.VolCtxsw, Involuntary: last.NonvolCtxsw}
		if elapsed > 0 {
			frac := round(cpu/elapsed, 6)
			o.CPUFrac = &frac
		}
	}
	if len(collectMS) == 0 {
		return o
	}
	sorted := append([]float64(nil), collectMS...)
	sort.Float64s(sorted)
	sum := 0.0
	for _, v := range sorted {
		sum += v
	}
	o.CollectMSMean = round(sum/float64(len(sorted)), 3)
	o.CollectMSP50 = quantile(sorted, 0.5)
	o.CollectMSP95 = quantile(sorted, 0.95)
	o.CollectMSMax = sorted[len(sorted)-1]
	return o
}

// quantile — эмпирический квантиль с линейной интерполяцией (тип 7 по Hyndman–Fan, как numpy по умолчанию).
func quantile(sorted []float64, q float64) float64 {
	if len(sorted) == 1 {
		return sorted[0]
	}
	h := q * float64(len(sorted)-1)
	lo := math.Floor(h)
	hi := math.Ceil(h)
	return round(sorted[int(lo)]+(h-lo)*(sorted[int(hi)]-sorted[int(lo)]), 3)
}

func unixSeconds(t time.Time) float64 {
	return round(float64(t.UnixNano())/1e9, 6)
}

func round(v float64, digits int) float64 {
	p := math.Pow(10, float64(digits))
	return math.Round(v*p) / p
}
