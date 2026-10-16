//go:build !linux && !darwin

package main

func kernelRelease() string { return "" }
