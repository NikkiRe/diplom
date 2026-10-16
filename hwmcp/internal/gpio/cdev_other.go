//go:build !linux

package gpio

import "errors"

// Cdev — линии через /dev/gpiochipN; вне Linux недоступно (на Mac — режим --root с эмуляцией Files).
type Cdev struct {
	DevDir string
	UAPI   string
}

var errNotLinux = errors.New("gpio: character device is only supported on Linux; use -gpio-driver files")

// Name — имя драйвера для журнала.
func (c *Cdev) Name() string { return "cdev:unsupported" }

// Chips — разрешённых чипов вне Linux нет.
func (c *Cdev) Chips() map[string]any { return map[string]any{} }

// Open всегда возвращает ошибку вне Linux.
func (c *Cdev) Open(LineConfig) (Line, error) { return nil, errNotLinux }

// Info недоступно вне Linux.
func (c *Cdev) Info(string) (ChipInfo, []LineInfo, error) { return ChipInfo{}, nil, errNotLinux }
