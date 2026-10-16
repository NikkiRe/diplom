// Package selfstat меряет накладные расходы самого сэмплера: время CPU (getrusage), пик RSS и, на Linux,
// текущие RSS/PSS из настоящего /proc/self/smaps_rollup (независимо от корня фикстуры).
//
// По методике (§12.3) sysmon работает в своём cgroup, а его расходы меряются и вычитаются; эти числа
// пишутся в каждый сэмпл и в итоговую запись.
package selfstat

import (
	"runtime"
	"syscall"
	"time"

	"github.com/local/sysmon/internal/procfs"
)

// Usage — собственные расходы процесса на момент замера.
type Usage struct {
	UserS, SysS float64
	MaxRSSKiB   uint64 // пик RSS за жизнь процесса (getrusage ru_maxrss)
	RSSKiB      uint64 // текущий RSS (только Linux; 0 — неизвестно)
	PSSKiB      uint64 // текущий PSS (только Linux)
	VolCtxsw    uint64 // ru_nvcsw — по всем потокам, включая завершившиеся
	NonvolCtxsw uint64 // ru_nivcsw
}

// CPUS — суммарное время CPU, с.
func (u Usage) CPUS() float64 { return u.UserS + u.SysS }

func tv(t syscall.Timeval) float64 {
	return time.Duration(t.Nano()).Seconds()
}

// Read возвращает собственные расходы. Ошибка чтения /proc/self (например, /proc не смонтирован) не
// фатальна: остаются данные getrusage.
func Read() (Usage, error) {
	var ru syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &ru); err != nil {
		return Usage{}, err
	}
	u := Usage{
		UserS:       tv(ru.Utime),
		SysS:        tv(ru.Stime),
		MaxRSSKiB:   uint64(ru.Maxrss),
		VolCtxsw:    uint64(ru.Nvcsw),
		NonvolCtxsw: uint64(ru.Nivcsw),
	}
	if runtime.GOOS == "darwin" {
		u.MaxRSSKiB /= 1024 // на macOS ru_maxrss в байтах, на Linux — в КиБ
	}
	if runtime.GOOS == "linux" {
		if sm, err := procfs.New("/proc").ReadSmaps(selfPID()); err == nil {
			u.RSSKiB, u.PSSKiB = sm.RssKiB, sm.PssKiB
		}
	}
	return u, nil
}

func selfPID() int { return syscall.Getpid() }
