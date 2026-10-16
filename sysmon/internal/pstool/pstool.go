// Package pstool — упрощённый источник данных для macOS (только для разработки): вывод ps(1) и
// netstat(1). Здесь нет PSS, ввода-вывода и переключений контекста — только RSS, VSZ и время CPU.
// Разбор вывода — чистые функции и проверяется на любой ОС; запуск команд — в Run*.
package pstool

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strconv"
	"strings"
)

// PSArgs — аргументы ps: все процессы, без заголовков, comm последним (в пути могут быть пробелы).
var PSArgs = []string{"-A", "-o", "pid=,ppid=,rss=,vsz=,utime=,time=,comm="}

// Proc — строка вывода ps.
type Proc struct {
	PID, PPID int
	RSSKiB    uint64
	VSZKiB    uint64
	UserS     float64 // utime
	CPUS      float64 // time = user + system
	Comm      string
}

// ParseCPUTime разбирает время CPU ps: "[[ДД-]ЧЧ:]ММ:СС[.сс]" (на macOS минуты бывают > 59: "316:52.43").
func ParseCPUTime(s string) (float64, error) {
	s = strings.TrimSpace(s)
	if s == "" || s == "-" {
		return 0, errors.New("пустое время")
	}
	var days float64
	if i := strings.IndexByte(s, '-'); i >= 0 {
		d, err := strconv.ParseFloat(s[:i], 64)
		if err != nil {
			return 0, err
		}
		days, s = d, s[i+1:]
	}
	parts := strings.Split(s, ":")
	if len(parts) > 3 {
		return 0, fmt.Errorf("время CPU %q: слишком много ':'", s)
	}
	total := 0.0
	for _, p := range parts {
		v, err := strconv.ParseFloat(p, 64)
		if err != nil || v < 0 {
			return 0, fmt.Errorf("время CPU %q: %v", s, err)
		}
		total = total*60 + v
	}
	return days*86400 + total, nil
}

// ParsePS разбирает вывод ps с аргументами PSArgs. Строки, которые не разбираются, пропускаются
// (например, процесс-зомби с "-" вместо чисел); их число возвращается для журнала ошибок.
func ParsePS(out []byte) (procs []Proc, skipped int) {
	sc := bufio.NewScanner(bytes.NewReader(out))
	sc.Buffer(make([]byte, 64*1024), 1<<20)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" {
			continue
		}
		f := strings.Fields(line)
		if len(f) < 7 {
			skipped++
			continue
		}
		var p Proc
		var err error
		ints := []*int{&p.PID, &p.PPID}
		for i, dst := range ints {
			if *dst, err = strconv.Atoi(f[i]); err != nil {
				break
			}
		}
		if err == nil {
			p.RSSKiB, err = strconv.ParseUint(f[2], 10, 64)
		}
		if err == nil {
			p.VSZKiB, err = strconv.ParseUint(f[3], 10, 64)
		}
		if err == nil {
			p.UserS, err = ParseCPUTime(f[4])
		}
		if err == nil {
			p.CPUS, err = ParseCPUTime(f[5])
		}
		if err != nil {
			skipped++
			continue
		}
		// comm — остаток строки после шестого поля (сохраняем пробелы внутри пути).
		rest := line
		for i := 0; i < 6; i++ {
			rest = strings.TrimLeft(rest, " \t")
			if j := strings.IndexAny(rest, " \t"); j >= 0 {
				rest = rest[j:]
			}
		}
		p.Comm = strings.TrimSpace(rest)
		procs = append(procs, p)
	}
	return procs, skipped
}

// NetIf — счётчики интерфейса из netstat -ibn (строка <Link#N>).
type NetIf struct {
	RxBytes, RxPackets, RxErrs uint64
	TxBytes, TxPackets, TxErrs uint64
}

// ParseNetstat разбирает netstat -ibn. Берутся только строки уровня канала (<Link#N>): строки адресов
// повторяют те же счётчики. Колонка Address у lo0 пустая, поэтому числа читаются с конца строки:
// Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll. Звёздочка после имени (выключенный интерфейс) убирается.
func ParseNetstat(out []byte) map[string]NetIf {
	res := map[string]NetIf{}
	for _, line := range strings.Split(string(out), "\n") {
		f := strings.Fields(line)
		if len(f) < 10 || !strings.HasPrefix(f[2], "<Link#") {
			continue
		}
		n := len(f)
		var v [6]uint64
		ok := true
		for i := 0; i < 6; i++ {
			x, err := strconv.ParseUint(f[n-7+i], 10, 64)
			if err != nil {
				ok = false
				break
			}
			v[i] = x
		}
		if !ok {
			continue
		}
		name := strings.TrimSuffix(f[0], "*")
		res[name] = NetIf{RxPackets: v[0], RxErrs: v[1], RxBytes: v[2], TxPackets: v[3], TxErrs: v[4], TxBytes: v[5]}
	}
	return res
}

// RunPS запускает ps и возвращает разобранный вывод.
func RunPS(ctx context.Context) ([]Proc, int, error) {
	out, err := exec.CommandContext(ctx, "ps", PSArgs...).Output()
	if err != nil {
		return nil, 0, fmt.Errorf("ps: %w", err)
	}
	procs, skipped := ParsePS(out)
	return procs, skipped, nil
}

// RunNetstat запускает netstat -ibn.
func RunNetstat(ctx context.Context) (map[string]NetIf, error) {
	out, err := exec.CommandContext(ctx, "netstat", "-ibn").Output()
	if err != nil {
		return nil, fmt.Errorf("netstat: %w", err)
	}
	return ParseNetstat(out), nil
}
