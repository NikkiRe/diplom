//go:build !(linux && (arm64 || amd64))

package source

import "errors"

// OpenV4L2 — захват V4L2 есть только в сборке для 64-битного Linux.
func OpenV4L2(path, format string, w, h int, fps float64) (Source, error) {
	return nil, errors.New("v4l2: camera capture is built only for linux/arm64 and linux/amd64")
}
