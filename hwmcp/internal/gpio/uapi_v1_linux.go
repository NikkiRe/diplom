//go:build linux

package gpio

import "unsafe"

// uAPI v1 (include/uapi/linux/gpio.h, «ABI v1»; ядра 4.8+, в новых — при CONFIG_GPIO_CDEV_V1=y).
// Нужен для ядра 4.9 Jetson Nano (JetPack 4.6), которое v2 не знает. Раскладка структур проверяется
// тестом (размеры 72/364/64/48/16 байт; 16 — у gpioevent_data на 64-битных платформах: __u64 выравнивает
// структуру до 8 байт).

const (
	gpioLineFlagKernel = 1 << 0 // GPIOLINE_FLAG_KERNEL: линия занята
	gpioLineFlagIsOut  = 1 << 1

	gpioHandleRequestInput     = 1 << 0
	gpioHandleRequestOutput    = 1 << 1
	gpioHandleRequestActiveLow = 1 << 2

	gpioEventRequestBothEdges = 1<<0 | 1<<1
)

type gpioLineInfoV1 struct {
	Offset   uint32
	Flags    uint32
	Name     [gpioMaxNameSize]byte
	Consumer [gpioMaxNameSize]byte
}

type gpioHandleRequest struct {
	Offsets       [64]uint32
	Flags         uint32
	DefaultValues [64]uint8
	Consumer      [gpioMaxNameSize]byte
	Lines         uint32
	Fd            int32
}

type gpioHandleData struct {
	Values [64]uint8
}

type gpioEventRequest struct {
	Offset      uint32
	HandleFlags uint32
	EventFlags  uint32
	Consumer    [gpioMaxNameSize]byte
	Fd          int32
}

// gpioEventData — событие фронта; id (1 — фронт вверх) по смещению 8, как у gpio_v2_line_event.
type gpioEventData struct {
	Timestamp uint64
	ID        uint32
	Padding   uint32 // хвостовое выравнивание структуры ядра на 64-битных платформах
}

var (
	ioctlV1GetLineInfo   = iowr(0x02, unsafe.Sizeof(gpioLineInfoV1{}))
	ioctlV1GetLineHandle = iowr(0x03, unsafe.Sizeof(gpioHandleRequest{}))
	ioctlV1GetLineEvent  = iowr(0x04, unsafe.Sizeof(gpioEventRequest{}))
	ioctlV1GetValues     = iowr(0x08, unsafe.Sizeof(gpioHandleData{}))
	ioctlV1SetValues     = iowr(0x09, unsafe.Sizeof(gpioHandleData{}))
	gpioV1EventSize      = int(unsafe.Sizeof(gpioEventData{}))
)

// requestV1 запрашивает одну линию и возвращает её дескриптор: вход с фронтами — GPIO_GET_LINEEVENT_IOCTL
// (по дескриптору события читаются и значения), иначе — GPIO_GET_LINEHANDLE_IOCTL; выход сразу получает
// начальное значение. Подавления дребезга в v1 нет — его делает cdevLine.
func requestV1(chipFd int, cfg LineConfig) (int, error) {
	var flags uint32
	if cfg.ActiveLow {
		flags |= gpioHandleRequestActiveLow
	}
	if cfg.OnEdge != nil && !cfg.Output {
		req := gpioEventRequest{Offset: uint32(cfg.Offset), HandleFlags: flags | gpioHandleRequestInput,
			EventFlags: gpioEventRequestBothEdges}
		copy(req.Consumer[:gpioMaxNameSize-1], cfg.Consumer)
		if err := ioctl(chipFd, ioctlV1GetLineEvent, unsafe.Pointer(&req)); err != nil {
			return -1, err
		}
		return int(req.Fd), nil
	}
	req := gpioHandleRequest{Lines: 1}
	req.Offsets[0] = uint32(cfg.Offset)
	if cfg.Output {
		req.Flags = flags | gpioHandleRequestOutput
		if cfg.Initial {
			req.DefaultValues[0] = 1
		}
	} else {
		req.Flags = flags | gpioHandleRequestInput
	}
	copy(req.Consumer[:gpioMaxNameSize-1], cfg.Consumer)
	if err := ioctl(chipFd, ioctlV1GetLineHandle, unsafe.Pointer(&req)); err != nil {
		return -1, err
	}
	return int(req.Fd), nil
}

func getV1(fd int) (bool, error) {
	var d gpioHandleData
	err := ioctl(fd, ioctlV1GetValues, unsafe.Pointer(&d))
	return d.Values[0] != 0, err
}

func setV1(fd int, on bool) error {
	var d gpioHandleData
	if on {
		d.Values[0] = 1
	}
	return ioctl(fd, ioctlV1SetValues, unsafe.Pointer(&d))
}

// lineInfoV1 — GPIO_GET_LINEINFO_IOCTL; запрос только читает сведения и ничего не меняет.
func lineInfoV1(chipFd, offset int) (LineInfo, error) {
	li := gpioLineInfoV1{Offset: uint32(offset)}
	if err := ioctl(chipFd, ioctlV1GetLineInfo, unsafe.Pointer(&li)); err != nil {
		return LineInfo{}, err
	}
	return LineInfo{Offset: offset, Name: cstr(li.Name[:]), Consumer: cstr(li.Consumer[:]),
		Used: li.Flags&gpioLineFlagKernel != 0, Output: li.Flags&gpioLineFlagIsOut != 0}, nil
}
