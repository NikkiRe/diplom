// Команда sysmon — сэмплер ресурсов дерева процессов или группы cgroup v2 с записью в JSONL.
//
//	sysmon --pid 1234 --interval 30s --out run/sysmon.jsonl
//	sysmon --cgroup /sys/fs/cgroup/edgectx.slice --system --out run/sysmon.jsonl
//	sysmon --interval 5s --out run/sysmon.jsonl -- ./edgectx --config cfg.yaml
//
// Формат записей и гарантии — README.md компонента.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/local/sysmon/internal/cgroup"
	"github.com/local/sysmon/internal/collect"
	"github.com/local/sysmon/internal/procfs"
	"github.com/local/sysmon/internal/sampler"
)

// version — версия утилиты (пишется в заголовок JSONL).
const version = "0.1.0"

// Допустимый период сэмплирования по методике (THESIS_CONCEPT §7.2: «сэмплер 1–30 с»).
const (
	minInterval = time.Second
	maxInterval = 30 * time.Second
)

// Коды выхода.
const (
	exitOK    = 0
	exitError = 1
	exitUsage = 2
)

type intList []int

func (l *intList) String() string { return fmt.Sprint([]int(*l)) }
func (l *intList) Set(s string) error {
	for _, part := range strings.Split(s, ",") {
		pid, err := strconv.Atoi(strings.TrimSpace(part))
		if err != nil || pid <= 0 {
			return fmt.Errorf("неверный PID %q", part)
		}
		*l = append(*l, pid)
	}
	return nil
}

type strList []string

func (l *strList) String() string { return strings.Join(*l, ",") }
func (l *strList) Set(s string) error {
	for _, part := range strings.Split(s, ",") {
		if part = strings.TrimSpace(part); part != "" {
			*l = append(*l, part)
		}
	}
	return nil
}

type options struct {
	pids        intList
	cgroup      string
	noChildren  bool
	interval    time.Duration
	count       int
	duration    time.Duration
	out         string
	perProc     bool
	ifaces      strList
	netSelf     bool
	system      bool
	backend     string
	procRoot    string
	sysRoot     string
	cgroupRoot  string
	clkTck      int
	threadCtxsw bool
	showVersion bool
	cmd         []string
}

func parseFlags(args []string, stderr io.Writer) (*options, error) {
	o := &options{}
	fs := flag.NewFlagSet("sysmon", flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.Var(&o.pids, "pid", "корневой PID (можно несколько: --pid 1 --pid 2 или 1,2); потомки включаются")
	fs.StringVar(&o.cgroup, "cgroup", "", "каталог cgroup v2 (все процессы группы и вложенных) или auto — группа первого корня")
	fs.BoolVar(&o.noChildren, "no-children", false, "не включать потомков корневых процессов")
	fs.DurationVar(&o.interval, "interval", 30*time.Second, "период сэмплирования, 1s…30s")
	fs.IntVar(&o.count, "count", 0, "остановиться после N сэмплов (0 — без ограничения)")
	fs.DurationVar(&o.duration, "duration", 0, "остановиться через заданное время (0 — без ограничения)")
	fs.StringVar(&o.out, "out", "-", "файл JSONL (- — stdout)")
	fs.BoolVar(&o.perProc, "per-proc", true, "писать процессы поимённо (поле procs)")
	fs.Var(&o.ifaces, "iface", "сетевые интерфейсы (по умолчанию все, кроме lo)")
	fs.BoolVar(&o.netSelf, "net-self", false, "трафик из netns сэмплера, а не корневого процесса")
	fs.BoolVar(&o.system, "system", false, "писать состояние всей системы (/proc/stat, meminfo, loadavg)")
	fs.StringVar(&o.backend, "backend", "auto", "источник: auto | procfs (Linux) | ps (macOS, упрощённо)")
	fs.StringVar(&o.procRoot, "proc-root", "/proc", "корень /proc (фикстура для тестов)")
	fs.StringVar(&o.sysRoot, "sys-root", "/sys", "корень /sys (термозоны)")
	fs.StringVar(&o.cgroupRoot, "cgroup-root", "/sys/fs/cgroup", "точка монтирования cgroup v2 (для --cgroup auto)")
	fs.IntVar(&o.clkTck, "clk-tck", 0, "тиков в секунду для /proc/<pid>/stat (0 — из /proc/self/auxv, иначе 100)")
	fs.BoolVar(&o.threadCtxsw, "thread-ctxsw", true, "переключения контекста — сумма по потокам (task/*/status)")
	fs.BoolVar(&o.showVersion, "version", false, "показать версию")
	fs.Usage = func() {
		fmt.Fprintf(stderr, "sysmon %s — сэмплер ресурсов дерева процессов / cgroup v2 → JSONL\n\n", version)
		fmt.Fprintf(stderr, "  sysmon [флаги] (--pid PID | --cgroup DIR | -- команда [аргументы])\n\n")
		fs.PrintDefaults()
	}
	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	o.cmd = fs.Args()
	if o.showVersion {
		return o, nil
	}
	if len(o.pids) == 0 && o.cgroup == "" && len(o.cmd) == 0 {
		return nil, errors.New("нужна цель: --pid, --cgroup или команда после --")
	}
	if len(o.cmd) > 0 && len(o.pids) > 0 {
		return nil, errors.New("--pid и запуск команды взаимоисключающие")
	}
	if o.cgroup == "auto" && len(o.pids) == 0 && len(o.cmd) == 0 {
		return nil, errors.New("--cgroup auto требует --pid или команду")
	}
	if o.interval < minInterval || o.interval > maxInterval {
		return nil, fmt.Errorf("--interval %v вне диапазона %v…%v", o.interval, minInterval, maxInterval)
	}
	if o.count < 0 || o.duration < 0 {
		return nil, errors.New("--count и --duration не могут быть отрицательными")
	}
	switch o.backend {
	case "auto":
		o.backend = "procfs"
		if runtime.GOOS == "darwin" {
			o.backend = "ps"
		}
	case "procfs", "ps":
	default:
		return nil, fmt.Errorf("неизвестный --backend %q", o.backend)
	}
	if o.backend == "ps" && o.cgroup != "" {
		return nil, errors.New("--cgroup доступен только с источником procfs (Linux)")
	}
	if o.backend == "ps" && len(o.pids) == 0 && len(o.cmd) == 0 {
		return nil, errors.New("источнику ps нужен --pid или команда")
	}
	return o, nil
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	opts, err := parseFlags(args, stderr)
	if err != nil {
		if !errors.Is(err, flag.ErrHelp) {
			fmt.Fprintln(stderr, "sysmon:", err)
		}
		return exitUsage
	}
	if opts.showVersion {
		fmt.Fprintln(stdout, "sysmon", version)
		return exitOK
	}

	var out io.Writer = stdout
	if opts.out != "-" {
		if dir := filepath.Dir(opts.out); dir != "" {
			if err := os.MkdirAll(dir, 0o755); err != nil {
				fmt.Fprintln(stderr, "sysmon:", err)
				return exitError
			}
		}
		f, err := os.Create(opts.out)
		if err != nil {
			fmt.Fprintln(stderr, "sysmon:", err)
			return exitError
		}
		defer f.Close()
		out = f
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	sigs := make(chan os.Signal, 2)
	signal.Notify(sigs, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(sigs)

	// Режим «-- команда»: запускаем её сами и меряем её дерево; сигналы пересылаем ей, а сэмплер
	// останавливается, когда она завершится (последний сэмпл и итог пишутся после выхода).
	var (
		child    *exec.Cmd
		done     chan struct{}
		exitCode *int
		mu       sync.Mutex
	)
	if len(opts.cmd) > 0 {
		child = exec.Command(opts.cmd[0], opts.cmd[1:]...)
		child.Stdin = os.Stdin
		child.Stdout = os.Stdout
		if opts.out == "-" {
			child.Stdout = stderr // stdout занят JSONL
		}
		child.Stderr = stderr
		if err := child.Start(); err != nil {
			fmt.Fprintln(stderr, "sysmon:", err)
			return exitError
		}
		opts.pids = intList{child.Process.Pid}
		done = make(chan struct{})
		go func() {
			err := child.Wait()
			code := 0
			var ee *exec.ExitError
			if errors.As(err, &ee) {
				code = ee.ExitCode()
			} else if err != nil {
				code = -1
			}
			mu.Lock()
			exitCode = &code
			mu.Unlock()
			close(done)
		}()
	}
	go func() {
		for sig := range sigs {
			if child != nil {
				_ = child.Process.Signal(sig)
				continue
			}
			cancel()
		}
	}()

	collector, header, err := buildCollector(opts)
	if err != nil {
		fmt.Fprintln(stderr, "sysmon:", err)
		if child != nil {
			_ = child.Process.Kill()
		}
		return exitError
	}
	s := &sampler.Sampler{
		Collector:          collector,
		Interval:           opts.interval,
		Count:              opts.count,
		Duration:           opts.duration,
		PerProc:            opts.perProc,
		RootPIDs:           opts.pids,
		StopWhenTargetGone: len(opts.pids) > 0 && opts.cgroup == "",
		Done:               done,
		DoneReason: func() (string, *int) {
			mu.Lock()
			defer mu.Unlock()
			return sampler.StopCmdExited, exitCode
		},
	}
	end, err := s.Run(ctx, header, out)
	if err != nil {
		fmt.Fprintln(stderr, "sysmon: запись:", err)
		return exitError
	}
	if end.Reason == sampler.StopError {
		fmt.Fprintln(stderr, "sysmon:", end.Error)
		return exitError
	}
	if child != nil {
		// Сэмплер мог остановиться раньше команды (--count/--duration): команду не трогаем, ждём её.
		<-done
		mu.Lock()
		defer mu.Unlock()
		if exitCode != nil && *exitCode >= 0 {
			return *exitCode
		}
		return exitError
	}
	return exitOK
}

func buildCollector(o *options) (collect.Collector, sampler.Header, error) {
	host, _ := os.Hostname()
	h := sampler.Header{
		Version:   version,
		Host:      host,
		OS:        runtime.GOOS,
		Arch:      runtime.GOARCH,
		Kernel:    kernelRelease(),
		NumCPU:    runtime.NumCPU(),
		PageSize:  os.Getpagesize(),
		SysmonPID: os.Getpid(),
		Target: sampler.Target{PIDs: o.pids, Children: !o.noChildren, Cmd: o.cmd,
			Ifaces: o.ifaces, NetSelf: o.netSelf},
	}
	target := collect.Target{
		PIDs: o.pids, Children: !o.noChildren, Ifaces: o.ifaces, NetSelf: o.netSelf, System: o.system,
		ExcludePIDs: []int{os.Getpid()},
	}
	if o.backend == "ps" {
		return &collect.PSCollector{Target: target}, h, nil
	}

	proc := procfs.New(o.procRoot)
	if o.procRoot != "/proc" {
		h.ProcRoot = o.procRoot
	}
	if o.sysRoot != "/sys" {
		h.SysRoot = o.sysRoot
	}
	if o.cgroup == "auto" {
		dir, err := collect.CgroupOfPID(proc, o.cgroupRoot, o.pids[0])
		if err != nil {
			return nil, h, fmt.Errorf("--cgroup auto: %w", err)
		}
		// Excluding a PID from tree totals cannot remove its hierarchical cgroup
		// CPU/memory counters. Refuse a shared measurement boundary explicitly.
		pids, err := (cgroup.Group{Path: dir}).Procs(true)
		if err != nil {
			return nil, h, fmt.Errorf("--cgroup auto: проверить границу измерения: %w", err)
		}
		for _, pid := range pids {
			if pid == os.Getpid() {
				return nil, h, errors.New("--cgroup auto: группа включает sysmon; поместите цель в отдельную cgroup или используйте --pid без --cgroup")
			}
		}
		o.cgroup = dir
	}
	target.Cgroup = o.cgroup
	h.Target.Cgroup = o.cgroup

	clk := o.clkTck
	if clk <= 0 {
		clk = 100
		if runtime.GOOS == "linux" {
			if aux, err := procfs.New("/proc").ReadAuxv(); err == nil && aux[procfs.AtClkTck] > 0 {
				clk = int(aux[procfs.AtClkTck])
			}
		}
	}
	h.ClkTck = clk
	return &collect.ProcfsCollector{
		Proc:       proc,
		SysRoot:    o.sysRoot,
		CgroupRoot: o.cgroupRoot,
		Target:     target,
		ClkTck:     float64(clk),
		PageKiB:    uint64(os.Getpagesize() / 1024),
		Ctxsw:      o.threadCtxsw,
	}, h, nil
}
