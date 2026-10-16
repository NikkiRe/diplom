//go:build linux

package sysfs

import (
	"unsafe"

	"golang.org/x/sys/unix"
)

// openAttr — openat(AT_FDCWD, p, O_RDONLY|O_CLOEXEC) без выделения памяти: unix.Open на каждый вызов
// копирует строку пути в новый буфер с NUL, а cpath уже заканчивается NUL.
func openAttr(p cpath) (int, error) {
	cwd := unix.AT_FDCWD // отрицательная константа: в uintptr — только через переменную
	for {
		fd, _, errno := unix.Syscall6(unix.SYS_OPENAT, uintptr(cwd),
			uintptr(unsafe.Pointer(unsafe.StringData(string(p)))), unix.O_RDONLY|unix.O_CLOEXEC, 0, 0, 0)
		switch errno {
		case 0:
			return int(fd), nil
		case unix.EINTR:
			continue
		}
		return -1, errno
	}
}
