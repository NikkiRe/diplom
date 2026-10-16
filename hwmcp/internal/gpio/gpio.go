// Package gpio — доступ к линиям GPIO.
//
// Два драйвера с одним интерфейсом:
//   - Cdev — символьное устройство /dev/gpiochipN без libgpiod и без cgo (статический бинарник); только
//     Linux. uAPI v2 (ядра ≥ 5.10) или v1 (ядра 4.8+, в том числе 4.9 у Jetson Nano с JetPack 4.6) —
//     выбирается для каждого чипа (cdev_linux.go);
//   - Files — эмуляция для режима --root на фикстуре: уровень линии хранится в файле
//     <dir>/<chip>/line<N> ("0" или "1"); тест или симулятор может менять входы, записывая файлы.
//
// Чип в LineConfig — имя узла (gpiochipN) или метка чипа: у Cdev — label из GPIO_GET_CHIPINFO_IOCTL
// (номер узла может меняться между загрузками, метка — нет), у Files — имя каталога.
//
// Значения логические: true — «активно» (с учётом active_low из declared.json).
package gpio

import (
	"errors"
	"fmt"
)

// LineConfig — запрос линии.
type LineConfig struct {
	Chip      string // gpiochip0 или метка чипа
	Offset    int    // номер линии на чипе
	Output    bool
	ActiveLow bool
	Initial   bool   // начальное логическое значение выхода
	Consumer  string // имя потребителя (видно в gpioinfo)

	// OnEdge, если задан, вызывается при каждом фронте входа с новым логическим уровнем (драйвер Cdev:
	// события ядра; драйвер Files фронтов не порождает — входы только опрашиваются).
	// Вызывается из горутины драйвера; не должен блокироваться надолго.
	OnEdge func(active bool)
	// DebounceMs — подавление дребезга входа (0 — нет): в uAPI v2 — ядром, в v1 — драйвером Cdev.
	DebounceMs int
}

// Line — открытая линия.
type Line interface {
	// Get возвращает логическое значение; для выхода — выставленное значение.
	Get() (bool, error)
	// Set выставляет логическое значение выхода.
	Set(v bool) error
	Close() error
}

// Backend — драйвер линий.
type Backend interface {
	Open(cfg LineConfig) (Line, error)
	Name() string
}

// ChipInfo — сведения о чипе (GPIO_GET_CHIPINFO_IOCTL) и версия uAPI, через которую с ним работает Cdev.
type ChipInfo struct {
	Name, Label, UAPI string
	Lines             int
}

// LineInfo — сведения о линии (GPIO_V2_GET_LINEINFO_IOCTL или GPIO_GET_LINEINFO_IOCTL v1).
type LineInfo struct {
	Offset   int
	Name     string
	Consumer string
	Used     bool
	Output   bool
}

// ErrUnknown — уровень линии неизвестен (в эмуляции файл линии ещё не создан).
var ErrUnknown = errors.New("gpio: line value unknown")

// ErrNotOutput — запись во входную линию.
var ErrNotOutput = errors.New("gpio: line is not an output")

// Версии uAPI символьного устройства (Cdev.UAPI).
const (
	UAPIAuto = "auto"
	UAPIv1   = "v1"
	UAPIv2   = "v2"
)

func (c LineConfig) String() string {
	dir := "input"
	if c.Output {
		dir = "output"
	}
	return fmt.Sprintf("%s#%d (%s)", c.Chip, c.Offset, dir)
}
