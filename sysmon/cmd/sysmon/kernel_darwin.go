package main

import "syscall"

func kernelRelease() string {
	s, err := syscall.Sysctl("kern.osrelease")
	if err != nil {
		return ""
	}
	return "darwin " + s
}
