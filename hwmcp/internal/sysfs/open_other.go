//go:build !linux

package sysfs

import (
	"errors"

	"golang.org/x/sys/unix"
)

// openAttr вне Linux (разработка на Mac) — через unix.Open, который копирует путь.
func openAttr(p cpath) (int, error) {
	for {
		fd, err := unix.Open(p.String(), unix.O_RDONLY|unix.O_CLOEXEC, 0)
		if !errors.Is(err, unix.EINTR) {
			return fd, err
		}
	}
}
