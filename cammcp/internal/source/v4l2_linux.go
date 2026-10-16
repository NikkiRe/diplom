//go:build linux && (arm64 || amd64)

package source

import (
	"errors"
	"fmt"
	"image"
	"math"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

// Захват V4L2 без cgo: структуры videodev2.h повторены в Go для 64-битного Linux (размеры проверяет
// тест), запросы — ioctl через golang.org/x/sys/unix, кадры — mmap-стриминг. Проверено на
// виртуальном драйвере ядра vivid (YUYV); на USB-камере (UVC) и в формате MJPEG — не проверено.

type v4l2Capability struct {
	Driver       [16]byte
	Card         [32]byte
	BusInfo      [32]byte
	Version      uint32
	Capabilities uint32
	DeviceCaps   uint32
	Reserved     [3]uint32
}

type v4l2Fmtdesc struct {
	Index       uint32
	Type        uint32
	Flags       uint32
	Description [32]byte
	Pixelformat uint32
	MbusCode    uint32
	Reserved    [3]uint32
}

type v4l2PixFormat struct {
	Width, Height, Pixelformat, Field, Bytesperline, Sizeimage uint32
	Colorspace, Priv, Flags, YcbcrEnc, Quantization, XferFunc  uint32
}

type v4l2Format struct {
	Type uint32
	_    uint32 // объединение fmt выровнено на 8: в нём есть указатели
	Pix  v4l2PixFormat
	_    [200 - unsafe.Sizeof(v4l2PixFormat{})]byte
}

type v4l2RequestBuffers struct {
	Count, Type, Memory, Capabilities uint32
	Flags                             uint8
	Reserved                          [3]uint8
}

type v4l2Timecode struct {
	Type, Flags                     uint32
	Frames, Seconds, Minutes, Hours uint8
	Userbits                        [4]uint8
}

type v4l2Buffer struct {
	Index, Type, Bytesused, Flags, Field uint32
	Timestamp                            unix.Timeval
	Timecode                             v4l2Timecode
	Sequence, Memory                     uint32
	M                                    uintptr // объединение m: offset (младшие 32 бита) | userptr | planes | fd
	Length, Reserved2                    uint32
	RequestFD                            int32
}

type v4l2Fract struct{ Numerator, Denominator uint32 }

type v4l2Captureparm struct {
	Capability, Capturemode   uint32
	Timeperframe              v4l2Fract
	Extendedmode, Readbuffers uint32
	Reserved                  [4]uint32
}

type v4l2Streamparm struct {
	Type    uint32
	Capture v4l2Captureparm
	_       [200 - unsafe.Sizeof(v4l2Captureparm{})]byte
}

// Номера ioctl: _IOR/_IOW/_IOWR('V', nr, тип) общей схемы Linux (arm64, amd64).
func ioc(dir, nr, size uintptr) uintptr { return dir<<30 | size<<16 | 'V'<<8 | nr }

const (
	iocW  = 1
	iocR  = 2
	iocRW = iocR | iocW
)

var (
	vidiocQuerycap  = ioc(iocR, 0, unsafe.Sizeof(v4l2Capability{}))
	vidiocEnumFmt   = ioc(iocRW, 2, unsafe.Sizeof(v4l2Fmtdesc{}))
	vidiocSFmt      = ioc(iocRW, 5, unsafe.Sizeof(v4l2Format{}))
	vidiocReqbufs   = ioc(iocRW, 8, unsafe.Sizeof(v4l2RequestBuffers{}))
	vidiocQuerybuf  = ioc(iocRW, 9, unsafe.Sizeof(v4l2Buffer{}))
	vidiocQbuf      = ioc(iocRW, 15, unsafe.Sizeof(v4l2Buffer{}))
	vidiocDqbuf     = ioc(iocRW, 17, unsafe.Sizeof(v4l2Buffer{}))
	vidiocStreamon  = ioc(iocW, 18, unsafe.Sizeof(int32(0)))
	vidiocStreamoff = ioc(iocW, 19, unsafe.Sizeof(int32(0)))
	vidiocGParm     = ioc(iocRW, 21, unsafe.Sizeof(v4l2Streamparm{}))
	vidiocSParm     = ioc(iocRW, 22, unsafe.Sizeof(v4l2Streamparm{}))
)

const (
	bufTypeVideoCapture = 1
	memoryMmap          = 1
	capVideoCapture     = 0x00000001
	capStreaming        = 0x04000000
	capDeviceCaps       = 0x80000000
	capTimeperframe     = 0x1000
	bufFlagError        = 0x40
	nBuffers            = 4
)

func ioctl(fd int, req uintptr, arg unsafe.Pointer) error {
	for {
		_, _, e := unix.Syscall(unix.SYS_IOCTL, uintptr(fd), req, uintptr(arg))
		switch e {
		case 0:
			return nil
		case unix.EINTR:
			continue
		}
		return e
	}
}

// V4L2 — камера V4L2 (mmap-стриминг). Перед каждым кадром накопившиеся буферы возвращаются драйверу
// без обработки, поэтому кадр всегда свежий, а камера при редкой обработке не копит задержку.
type V4L2 struct {
	fd      int
	path    string
	w, h    int
	fps     float64
	bufs    [][]byte
	dec     frameDecoder
	timeout time.Duration
	pfd     [1]unix.PollFd
	seq     uint32 // номер у драйвера последнего отданного кадра (по нему тест проверяет свежесть кадра)
}

// OpenV4L2 открывает устройство, выбирает формат (auto | yuyv | mjpeg), просит размер w×h и частоту
// fps (драйвер может выбрать ближайшие поддерживаемые) и запускает стриминг.
func OpenV4L2(path, format string, w, h int, fps float64) (Source, error) {
	fd, err := unix.Open(path, unix.O_RDWR|unix.O_NONBLOCK|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, fmt.Errorf("v4l2 %s: %w", path, err)
	}
	v := &V4L2{fd: fd, path: path}
	if err := v.setup(format, w, h, fps); err != nil {
		v.Close()
		return nil, fmt.Errorf("v4l2 %s: %w", path, err)
	}
	return v, nil
}

func (v *V4L2) setup(format string, w, h int, fps float64) error {
	var c v4l2Capability
	if err := ioctl(v.fd, vidiocQuerycap, unsafe.Pointer(&c)); err != nil {
		return fmt.Errorf("VIDIOC_QUERYCAP: %w", err)
	}
	caps := c.Capabilities
	if caps&capDeviceCaps != 0 {
		caps = c.DeviceCaps
	}
	if caps&capVideoCapture == 0 || caps&capStreaming == 0 {
		return errors.New("not a video capture device with streaming I/O")
	}
	var have []uint32
	for i := uint32(0); ; i++ {
		d := v4l2Fmtdesc{Index: i, Type: bufTypeVideoCapture}
		if ioctl(v.fd, vidiocEnumFmt, unsafe.Pointer(&d)) != nil {
			break
		}
		have = append(have, d.Pixelformat)
	}
	pf, err := chooseFormat(have, format)
	if err != nil {
		return err
	}
	f := v4l2Format{Type: bufTypeVideoCapture, Pix: v4l2PixFormat{Width: uint32(w), Height: uint32(h), Pixelformat: pf}}
	if err := ioctl(v.fd, vidiocSFmt, unsafe.Pointer(&f)); err != nil {
		return fmt.Errorf("VIDIOC_S_FMT %s %dx%d: %w", fourcc(pf), w, h, err)
	}
	if f.Pix.Pixelformat != pf {
		return fmt.Errorf("driver switched format to %s", fourcc(f.Pix.Pixelformat))
	}
	v.w, v.h = int(f.Pix.Width), int(f.Pix.Height)
	v.dec = frameDecoder{format: pf, stride: max(int(f.Pix.Bytesperline), 2*v.w)}

	p := v4l2Streamparm{Type: bufTypeVideoCapture}
	if ioctl(v.fd, vidiocGParm, unsafe.Pointer(&p)) == nil && p.Capture.Capability&capTimeperframe != 0 {
		p.Capture.Timeperframe = v4l2Fract{Numerator: 1000, Denominator: uint32(math.Round(fps * 1000))}
		if err := ioctl(v.fd, vidiocSParm, unsafe.Pointer(&p)); err != nil {
			return fmt.Errorf("VIDIOC_S_PARM %g fps: %w", fps, err)
		}
	}
	if tf := p.Capture.Timeperframe; tf.Numerator > 0 && tf.Denominator > 0 {
		v.fps = float64(tf.Denominator) / float64(tf.Numerator)
	}
	v.timeout = 2 * time.Second
	if v.fps > 0 {
		v.timeout = max(v.timeout, time.Duration(3/v.fps*float64(time.Second)))
	}

	rb := v4l2RequestBuffers{Count: nBuffers, Type: bufTypeVideoCapture, Memory: memoryMmap}
	if err := ioctl(v.fd, vidiocReqbufs, unsafe.Pointer(&rb)); err != nil {
		return fmt.Errorf("VIDIOC_REQBUFS: %w", err)
	}
	if rb.Count < 2 {
		return fmt.Errorf("driver gave %d buffers, need at least 2", rb.Count)
	}
	for i := uint32(0); i < rb.Count; i++ {
		b := v4l2Buffer{Index: i, Type: bufTypeVideoCapture, Memory: memoryMmap}
		if err := ioctl(v.fd, vidiocQuerybuf, unsafe.Pointer(&b)); err != nil {
			return fmt.Errorf("VIDIOC_QUERYBUF: %w", err)
		}
		m, err := unix.Mmap(v.fd, int64(uint32(b.M)), int(b.Length), unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
		if err != nil {
			return fmt.Errorf("mmap buffer %d: %w", i, err)
		}
		v.bufs = append(v.bufs, m)
		if err := v.queue(i); err != nil {
			return err
		}
	}
	typ := int32(bufTypeVideoCapture)
	if err := ioctl(v.fd, vidiocStreamon, unsafe.Pointer(&typ)); err != nil {
		return fmt.Errorf("VIDIOC_STREAMON: %w", err)
	}
	v.pfd[0] = unix.PollFd{Fd: int32(v.fd), Events: unix.POLLIN}
	return nil
}

func (v *V4L2) queue(i uint32) error {
	b := v4l2Buffer{Index: i, Type: bufTypeVideoCapture, Memory: memoryMmap}
	if err := ioctl(v.fd, vidiocQbuf, unsafe.Pointer(&b)); err != nil {
		return fmt.Errorf("VIDIOC_QBUF: %w", err)
	}
	return nil
}

// dequeue забирает заполненный буфер; unix.EAGAIN — готовых нет.
func (v *V4L2) dequeue() (v4l2Buffer, error) {
	b := v4l2Buffer{Type: bufTypeVideoCapture, Memory: memoryMmap}
	err := ioctl(v.fd, vidiocDqbuf, unsafe.Pointer(&b))
	return b, err
}

// Grab возвращает драйверу накопившиеся кадры и ждёт следующего (не дольше timeout).
func (v *V4L2) Grab(_ float64, dst *image.Gray) error {
	for {
		b, err := v.dequeue()
		if errors.Is(err, unix.EAGAIN) {
			break
		}
		if err != nil {
			return fmt.Errorf("VIDIOC_DQBUF: %w", err)
		}
		if err := v.queue(b.Index); err != nil {
			return err
		}
	}
	deadline := time.Now().Add(v.timeout)
	for {
		left := time.Until(deadline)
		if left <= 0 {
			return fmt.Errorf("v4l2 %s: no frame in %v", v.path, v.timeout)
		}
		if _, err := unix.Poll(v.pfd[:], int(left.Milliseconds())+1); err != nil && !errors.Is(err, unix.EINTR) {
			return fmt.Errorf("poll: %w", err)
		}
		b, err := v.dequeue()
		if errors.Is(err, unix.EAGAIN) {
			continue
		}
		if err != nil {
			return fmt.Errorf("VIDIOC_DQBUF: %w", err)
		}
		if b.Flags&bufFlagError != 0 { // испорченный кадр: вернуть буфер и ждать следующий
			if err := v.queue(b.Index); err != nil {
				return err
			}
			continue
		}
		v.seq = b.Sequence
		err = v.dec.decode(v.bufs[b.Index][:b.Bytesused], dst)
		if qerr := v.queue(b.Index); qerr != nil {
			return qerr
		}
		return err
	}
}

// Info — описание источника.
func (v *V4L2) Info() Info {
	return Info{Kind: "v4l2", Name: v.path, W: v.w, H: v.h, Format: fourcc(v.dec.format), FPS: v.fps}
}

// Close останавливает стриминг, освобождает буферы и закрывает устройство.
func (v *V4L2) Close() error {
	var errs []error
	if len(v.bufs) > 0 {
		typ := int32(bufTypeVideoCapture)
		errs = append(errs, ioctl(v.fd, vidiocStreamoff, unsafe.Pointer(&typ)))
	}
	for _, b := range v.bufs {
		errs = append(errs, unix.Munmap(b))
	}
	v.bufs = nil
	return errors.Join(append(errs, unix.Close(v.fd))...)
}
