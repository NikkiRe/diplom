//go:build linux

package gpio

import "unsafe"

// uAPI v2 (include/uapi/linux/gpio.h, ядра ≥ 5.10). Раскладка структур проверяется тестом
// (размеры 592/272/16/256/48 байт).

const (
	gpioV2LinesMax       = 64
	gpioV2LineNumAttrMax = 10

	gpioV2LineFlagUsed        = 1 << 0
	gpioV2LineFlagActiveLow   = 1 << 1
	gpioV2LineFlagInput       = 1 << 2
	gpioV2LineFlagOutput      = 1 << 3
	gpioV2LineFlagEdgeRising  = 1 << 4
	gpioV2LineFlagEdgeFalling = 1 << 5

	gpioV2LineAttrIDOutputValues = 2
	gpioV2LineAttrIDDebounce     = 3
)

type gpioV2LineAttribute struct {
	ID      uint32
	Padding uint32
	Value   uint64 // объединение flags / values / debounce_period_us
}

type gpioV2LineConfigAttribute struct {
	Attr gpioV2LineAttribute
	Mask uint64
}

type gpioV2LineConfig struct {
	Flags    uint64
	NumAttrs uint32
	Padding  [5]uint32
	Attrs    [gpioV2LineNumAttrMax]gpioV2LineConfigAttribute
}

type gpioV2LineRequest struct {
	Offsets         [gpioV2LinesMax]uint32
	Consumer        [gpioMaxNameSize]byte
	Config          gpioV2LineConfig
	NumLines        uint32
	EventBufferSize uint32
	Padding         [5]uint32
	Fd              int32
}

// gpioV2LineEvent — событие фронта; id (1 — фронт вверх) по смещению 8, как у gpioevent_data v1.
type gpioV2LineEvent struct {
	TimestampNs uint64
	ID          uint32
	Offset      uint32
	Seqno       uint32
	LineSeqno   uint32
	Padding     [6]uint32
}

type gpioV2LineValues struct {
	Bits uint64
	Mask uint64
}

type gpioV2LineInfo struct {
	Name     [gpioMaxNameSize]byte
	Consumer [gpioMaxNameSize]byte
	Offset   uint32
	NumAttrs uint32
	Flags    uint64
	Attrs    [gpioV2LineNumAttrMax]gpioV2LineAttribute
	Padding  [4]uint32
}

var (
	ioctlV2GetLineInfo  = iowr(0x05, unsafe.Sizeof(gpioV2LineInfo{}))
	ioctlV2GetLine      = iowr(0x07, unsafe.Sizeof(gpioV2LineRequest{}))
	ioctlV2GetValues    = iowr(0x0E, unsafe.Sizeof(gpioV2LineValues{}))
	ioctlV2SetValues    = iowr(0x0F, unsafe.Sizeof(gpioV2LineValues{}))
	gpioV2LineEventSize = int(unsafe.Sizeof(gpioV2LineEvent{}))
)

// requestV2 запрашивает одну линию (GPIO_V2_GET_LINE_IOCTL) и возвращает её дескриптор. Выход сразу
// получает начальное значение; у входа с фронтами дребезг подавляет ядро.
func requestV2(chipFd int, cfg LineConfig) (int, error) {
	var req gpioV2LineRequest
	req.Offsets[0] = uint32(cfg.Offset)
	req.NumLines = 1
	copy(req.Consumer[:gpioMaxNameSize-1], cfg.Consumer)
	if cfg.Output {
		req.Config.Flags = gpioV2LineFlagOutput
		req.Config.NumAttrs = 1
		req.Config.Attrs[0].Attr.ID = gpioV2LineAttrIDOutputValues
		if cfg.Initial {
			req.Config.Attrs[0].Attr.Value = 1
		}
		req.Config.Attrs[0].Mask = 1
	} else {
		req.Config.Flags = gpioV2LineFlagInput
		if cfg.OnEdge != nil {
			req.Config.Flags |= gpioV2LineFlagEdgeRising | gpioV2LineFlagEdgeFalling
		}
		if cfg.DebounceMs > 0 {
			i := req.Config.NumAttrs
			req.Config.Attrs[i].Attr.ID = gpioV2LineAttrIDDebounce
			req.Config.Attrs[i].Attr.Value = uint64(cfg.DebounceMs) * 1000 // мкс
			req.Config.Attrs[i].Mask = 1
			req.Config.NumAttrs++
		}
	}
	if cfg.ActiveLow {
		req.Config.Flags |= gpioV2LineFlagActiveLow
	}
	if err := ioctl(chipFd, ioctlV2GetLine, unsafe.Pointer(&req)); err != nil {
		return -1, err
	}
	return int(req.Fd), nil
}

func getV2(fd int) (bool, error) {
	v := gpioV2LineValues{Mask: 1}
	err := ioctl(fd, ioctlV2GetValues, unsafe.Pointer(&v))
	return v.Bits&1 == 1, err
}

func setV2(fd int, on bool) error {
	v := gpioV2LineValues{Mask: 1}
	if on {
		v.Bits = 1
	}
	return ioctl(fd, ioctlV2SetValues, unsafe.Pointer(&v))
}

// lineInfoV2 — GPIO_V2_GET_LINEINFO_IOCTL; запрос только читает сведения и ничего не меняет.
func lineInfoV2(chipFd, offset int) (LineInfo, error) {
	li := gpioV2LineInfo{Offset: uint32(offset)}
	if err := ioctl(chipFd, ioctlV2GetLineInfo, unsafe.Pointer(&li)); err != nil {
		return LineInfo{}, err
	}
	return LineInfo{Offset: offset, Name: cstr(li.Name[:]), Consumer: cstr(li.Consumer[:]),
		Used: li.Flags&gpioV2LineFlagUsed != 0, Output: li.Flags&gpioV2LineFlagOutput != 0}, nil
}
