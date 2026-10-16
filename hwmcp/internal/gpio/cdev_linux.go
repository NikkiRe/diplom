//go:build linux

package gpio

import (
	"encoding/binary"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

const gpioMaxNameSize = 32

// gpiochip_info — общая для v1 и v2 (GPIO_GET_CHIPINFO_IOCTL), 68 байт.
type gpioChipInfo struct {
	Name  [gpioMaxNameSize]byte
	Label [gpioMaxNameSize]byte
	Lines uint32
}

// Номера ioctl: _IOWR(0xB4, nr, size) = 3<<30 | size<<16 | 0xB4<<8 | nr; _IOR — 2<<30.
func iowr(nr, size uintptr) uintptr { return 3<<30 | size<<16 | 0xB4<<8 | nr }
func ior(nr, size uintptr) uintptr  { return 2<<30 | size<<16 | 0xB4<<8 | nr }

var ioctlGetChipInfo = ior(0x01, unsafe.Sizeof(gpioChipInfo{}))

func ioctl(fd int, req uintptr, arg unsafe.Pointer) error {
	_, _, errno := unix.Syscall(unix.SYS_IOCTL, uintptr(fd), req, uintptr(arg))
	if errno != 0 {
		return errno
	}
	return nil
}

// Cdev — линии через /dev/gpiochipN.
//
// Версия uAPI определяется для каждого чипа один раз: в режиме auto пробуется v2
// (GPIO_V2_GET_LINEINFO_IOCTL — только чтение сведений линии 0), а EINVAL или ENOTTY означают, что ядро
// v2 не знает (gpio_ioctl ядра 4.9 на неизвестную команду отвечает EINVAL), — тогда v1.
type Cdev struct {
	DevDir string // каталог устройств, по умолчанию /dev
	UAPI   string // auto | v1 | v2; пусто — auto

	mu    sync.Mutex
	chips map[string]chipRef // чип из LineConfig -> узел и версия uAPI
}

type chipRef struct {
	node string // gpiochipN
	uapi string // v1 | v2
}

// Name — имя драйвера для журнала.
func (c *Cdev) Name() string { return "cdev:" + c.devDir() + " uapi=" + c.mode() }

// Chips — как разрешены чипы открытых линий (для журнала): чип из declared.json -> узел и uAPI.
func (c *Cdev) Chips() map[string]any {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make(map[string]any, len(c.chips))
	for chip, ref := range c.chips {
		out[chip] = map[string]any{"node": ref.node, "uapi": ref.uapi}
	}
	return out
}

func (c *Cdev) devDir() string {
	if c.DevDir == "" {
		return "/dev"
	}
	return c.DevDir
}

func (c *Cdev) mode() string {
	if c.UAPI == "" {
		return UAPIAuto
	}
	return c.UAPI
}

func (c *Cdev) openChip(node string) (*os.File, error) {
	return os.OpenFile(filepath.Join(c.devDir(), node), os.O_RDWR, 0)
}

// Open запрашивает линию у ядра. Выход сразу получает начальное значение.
func (c *Cdev) Open(cfg LineConfig) (Line, error) {
	ref, err := c.resolve(cfg.Chip)
	if err != nil {
		return nil, err
	}
	if cfg.Consumer == "" {
		cfg.Consumer = "hw-mcp"
	}
	chip, err := c.openChip(ref.node)
	if err != nil {
		return nil, fmt.Errorf("gpio: open %s: %w", ref.node, err)
	}
	defer chip.Close()
	request, eventSize := requestV2, gpioV2LineEventSize
	if ref.uapi == UAPIv1 {
		request, eventSize = requestV1, gpioV1EventSize
	}
	fd, err := request(int(chip.Fd()), cfg)
	if err != nil {
		return nil, fmt.Errorf("gpio: request %s on %s (uAPI %s): %w", cfg, ref.node, ref.uapi, err)
	}
	l := &cdevLine{fd: fd, cfg: cfg, v1: ref.uapi == UAPIv1}
	if cfg.OnEdge != nil && !cfg.Output {
		if err := l.watch(eventSize); err != nil {
			_ = unix.Close(fd)
			return nil, fmt.Errorf("gpio: %s: %w", cfg, err)
		}
	}
	return l, nil
}

// resolve находит узел чипа и версию uAPI; результат запоминается (линии открываются при старте).
func (c *Cdev) resolve(chip string) (chipRef, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if ref, ok := c.chips[chip]; ok {
		return ref, nil
	}
	mode := c.mode()
	if mode != UAPIAuto && mode != UAPIv1 && mode != UAPIv2 {
		return chipRef{}, fmt.Errorf("gpio: uAPI must be auto, v1 or v2, got %q", c.UAPI)
	}
	node, err := c.findNode(chip)
	if err != nil {
		return chipRef{}, err
	}
	ref := chipRef{node: node, uapi: mode}
	if mode == UAPIAuto {
		if ref.uapi, err = c.detect(node); err != nil {
			return chipRef{}, err
		}
	}
	if c.chips == nil {
		c.chips = map[string]chipRef{}
	}
	c.chips[chip] = ref
	return ref, nil
}

var nodeRe = regexp.MustCompile(`^gpiochip[0-9]+$`)

// findNode: существующий узел gpiochipN берётся как есть, иначе chip — метка чипа, и узел ищется
// перебором <DevDir>/gpiochip*; метка должна быть ровно у одного чипа.
func (c *Cdev) findNode(chip string) (string, error) {
	if nodeRe.MatchString(chip) {
		if _, err := os.Stat(filepath.Join(c.devDir(), chip)); err == nil {
			return chip, nil
		}
	}
	entries, err := os.ReadDir(c.devDir())
	if err != nil {
		return "", fmt.Errorf("gpio: %w", err)
	}
	var found, other []string
	for _, e := range entries {
		node := e.Name()
		if !nodeRe.MatchString(node) {
			continue
		}
		info, err := c.chipInfo(node)
		switch {
		case err != nil:
			other = append(other, fmt.Sprintf("%s: %v", node, err))
		case info.Label == chip:
			found = append(found, node)
		default:
			other = append(other, fmt.Sprintf("%s %q", node, info.Label))
		}
	}
	switch {
	case len(found) == 1:
		return found[0], nil
	case len(found) > 1:
		return "", fmt.Errorf("gpio: chip label %q is ambiguous: %s", chip, strings.Join(found, ", "))
	case len(other) == 0:
		other = append(other, "none")
	}
	return "", fmt.Errorf("gpio: no chip named or labelled %q in %s (chips: %s)", chip, c.devDir(),
		strings.Join(other, ", "))
}

func (c *Cdev) chipInfo(node string) (ChipInfo, error) {
	f, err := c.openChip(node)
	if err != nil {
		return ChipInfo{}, err
	}
	defer f.Close()
	var ci gpioChipInfo
	if err := ioctl(int(f.Fd()), ioctlGetChipInfo, unsafe.Pointer(&ci)); err != nil {
		return ChipInfo{}, err
	}
	return ChipInfo{Name: cstr(ci.Name[:]), Label: cstr(ci.Label[:]), Lines: int(ci.Lines)}, nil
}

// detect — версия uAPI, которую поддерживает ядро для чипа node (режим auto).
func (c *Cdev) detect(node string) (string, error) {
	f, err := c.openChip(node)
	if err != nil {
		return "", fmt.Errorf("gpio: open %s: %w", node, err)
	}
	defer f.Close()
	_, err = lineInfoV2(int(f.Fd()), 0)
	uapi, err := uapiByProbe(err)
	if err != nil {
		return "", fmt.Errorf("gpio: %s: detect uAPI: %w", node, err)
	}
	return uapi, nil
}

// uapiByProbe — версия uAPI по исходу пробного GPIO_V2_GET_LINEINFO_IOCTL: успех — v2; EINVAL (так ядро 4.9
// отвечает на неизвестную команду) или ENOTTY — v1; другая ошибка — отказ.
func uapiByProbe(err error) (string, error) {
	switch {
	case err == nil:
		return UAPIv2, nil
	case errors.Is(err, unix.EINVAL), errors.Is(err, unix.ENOTTY):
		return UAPIv1, nil
	}
	return "", err
}

// Info читает сведения о чипе (имя узла или метка) и его линиях — без запроса линий (ничего не меняет).
func (c *Cdev) Info(chip string) (ChipInfo, []LineInfo, error) {
	ref, err := c.resolve(chip)
	if err != nil {
		return ChipInfo{}, nil, err
	}
	info, err := c.chipInfo(ref.node)
	if err != nil {
		return ChipInfo{}, nil, fmt.Errorf("gpio: chipinfo %s: %w", ref.node, err)
	}
	info.UAPI = ref.uapi
	f, err := c.openChip(ref.node)
	if err != nil {
		return info, nil, err
	}
	defer f.Close()
	lineInfo := lineInfoV2
	if ref.uapi == UAPIv1 {
		lineInfo = lineInfoV1
	}
	lines := make([]LineInfo, 0, info.Lines)
	for off := 0; off < info.Lines; off++ {
		li, err := lineInfo(int(f.Fd()), off)
		if err != nil {
			return info, lines, fmt.Errorf("gpio: lineinfo %s#%d: %w", ref.node, off, err)
		}
		lines = append(lines, li)
	}
	return info, lines, nil
}

type cdevLine struct {
	mu     sync.Mutex
	fd     int      // дескриптор линии (для ioctl); -1 — закрыта
	events *os.File // тот же дескриптор в неблокирующем режиме для чтения событий (если OnEdge)
	cfg    LineConfig
	v1     bool
	done   chan struct{}
}

// watch запускает чтение событий фронтов. Дескриптор неблокирующий и отдан планировщику Go: Close
// прерывает ожидание события.
func (l *cdevLine) watch(eventSize int) error {
	if err := unix.SetNonblock(l.fd, true); err != nil {
		return err
	}
	level, err := l.Get() // исходный уровень для подавления дребезга в v1
	if err != nil {
		return err
	}
	l.events = os.NewFile(uintptr(l.fd), l.cfg.Chip+"-line")
	l.done = make(chan struct{})
	go l.readEvents(eventSize, level)
	return nil
}

// readEvents читает события фронтов до закрытия линии. У событий v1 (16 байт) и v2 (48 байт) id лежит по
// смещению 8; 1 — фронт вверх, то есть линия стала активной (с учётом active_low).
//
// В uAPI v1 у ядра нет подавления дребезга, поэтому при DebounceMs > 0 его делает драйвер: после фронта
// события вычитываются, пока линия не простоит DebounceMs без них, затем сообщается установившийся
// уровень, если он изменился (так же ведёт себя debounce ядра в v2).
func (l *cdevLine) readEvents(size int, level bool) {
	defer close(l.done)
	buf := make([]byte, size*16)
	settle := time.Duration(l.cfg.DebounceMs) * time.Millisecond
	for {
		n, err := l.events.Read(buf)
		if err != nil {
			return
		}
		if !l.v1 || settle == 0 {
			for off := 0; off+size <= n; off += size {
				l.cfg.OnEdge(binary.NativeEndian.Uint32(buf[off+8:]) == 1)
			}
			continue
		}
		if !l.quiet(buf, settle) {
			return
		}
		if v, err := l.Get(); err == nil && v != level {
			level = v
			l.cfg.OnEdge(v)
		}
	}
}

// quiet вычитывает события, пока линия не простоит d без них; false — линия закрыта.
func (l *cdevLine) quiet(buf []byte, d time.Duration) bool {
	for {
		if err := l.events.SetReadDeadline(time.Now().Add(d)); err != nil {
			return false
		}
		_, err := l.events.Read(buf)
		if errors.Is(err, os.ErrDeadlineExceeded) {
			return l.events.SetReadDeadline(time.Time{}) == nil
		}
		if err != nil {
			return false
		}
	}
}

func (l *cdevLine) Get() (bool, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.fd < 0 {
		return false, os.ErrClosed
	}
	get := getV2
	if l.v1 {
		get = getV1
	}
	v, err := get(l.fd)
	if err != nil {
		return false, fmt.Errorf("gpio: get %s: %w", l.cfg, err)
	}
	return v, nil
}

func (l *cdevLine) Set(on bool) error {
	if !l.cfg.Output {
		return ErrNotOutput
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.fd < 0 {
		return os.ErrClosed
	}
	set := setV2
	if l.v1 {
		set = setV1
	}
	if err := set(l.fd, on); err != nil {
		return fmt.Errorf("gpio: set %s: %w", l.cfg, err)
	}
	return nil
}

func (l *cdevLine) Close() error {
	l.mu.Lock()
	if l.fd < 0 {
		l.mu.Unlock()
		return nil
	}
	var err error
	if l.events != nil {
		err = l.events.Close() // закрывает дескриптор и будит readEvents
	} else {
		err = unix.Close(l.fd)
	}
	l.fd = -1
	done := l.done
	l.mu.Unlock()
	if done != nil {
		<-done
	}
	return err
}

func cstr(b []byte) string {
	for i, c := range b {
		if c == 0 {
			return string(b[:i])
		}
	}
	return string(b)
}
