package sysfs

import (
	"errors"
	"path/filepath"
	"strconv"
	"sync"
	"unsafe"

	"golang.org/x/sys/unix"
)

// Причины, по которым Reader не вернул значение (how при has=false). Прототип (Channel.Read) любую
// неудачу называет "absent"; Reader различает их: по причине hw-mcp отличает пропавшее устройство (его
// стоит искать заново) от временно нечитаемого атрибута или мусора в файле.
const (
	ReasonAbsent     = "absent"            // атрибута или устройства нет (ENOENT, ENODEV)
	ReasonUnreadable = "unreadable"        // атрибут есть, но чтение не удаётся (EIO, ETIMEDOUT, EACCES…)
	ReasonNotANumber = "not_a_number"      // прочитано не число
	ReasonNoScale    = "raw-without-scale" // IIO: есть raw, нет scale (как в прототипе)
	ReasonClosed     = "closed"            // читатель закрыт
)

// Атрибуты канала: у IIO — *_input, иначе (raw + offset) × scale с атрибутами канала или типа; у file —
// только attrValue.
const (
	attrValue = iota // *_input или атрибут file
	attrRaw
	attrScale
	attrTypScale
	attrOffset
	attrTypOffset
	nAttrs
)

// Удерживаемые дескрипторы (Reader.fds): номер дескриптора или одно из состояний.
const (
	fdUnopened = -1
	fdAbsent   = -2 // у привязанного устройства такого атрибута нет
)

// Reader — повторный опрос канала на горячем пути сэмплера hw-mcp.
//
// Channel.Read повторяет прототип: каждый атрибут читается через os.ReadFile (на Linux это около
// 8 системных вызовов: openat, fcntl, fstat, epoll_ctl с EPERM на sysfs, read×2, close), а пути
// собираются заново при каждом чтении. Reader вычисляет пути один раз и читает атрибут тремя
// вызовами (openat, pread, close) в собственный буфер, без выделений памяти (на Linux; вне Linux
// unix.Open копирует путь).
//
// С KeepOpen (настоящий sysfs, -root /) нужные атрибуты открываются один раз при привязке к устройству,
// а чтение — один pread с нулевого смещения: ядро при этом заново вызывает show() драйвера
// (Documentation/filesystems/sysfs.rst: «If userspace seeks back to zero or does a pread(2) with an
// offset of '0' the show() method will be called again»), так что значение свежее. Отсутствие
// необязательного атрибута (offset, scale на канал) запоминается. Дескриптор привязан к узлу sysfs
// конкретного устройства: когда устройство пропадает, pread даёт ENODEV, даже если его номер
// (iio:deviceN) уже достался другому устройству. Тогда читатель «потерян»: у источников с переменной
// нумерацией (Renumbered) он не открывает путь заново сам — по старому пути может оказаться чужое
// устройство, — а ждёт Retarget после повторной инвентаризации, которая сверяет id устройства и его
// родителя; у 1-Wire путь содержит серийный номер, и читатель привязывается заново по нему.
//
// Без KeepOpen (фикстура, симулятор) каждый атрибут читается по пути: файлы фикстуры заменяют через
// rename и удаляют, и удерживаемый дескриптор читал бы старый inode. Пропажа устройства тогда видна
// как отсутствие файла, а подмена устройства под тем же номером — не видна.
//
// Значения и способ получения (how) совпадают с Channel.Read; при неудаче how — причина (Reason*).
// Это проверяет тест на фикстуре и на эталонных случаях IIO.
type Reader struct {
	mu       sync.Mutex
	t        target
	keepOpen bool
	fds      [nAttrs]int
	lost     bool // KeepOpen: устройство пропало, дескрипторы закрыты
	closed   bool
	buf      [96]byte
}

// target — что читать: пути атрибутов вычисляются один раз.
type target struct {
	kind       string // iio | file
	k          float64
	how        string // способ для kind=file (как Channel.How)
	renumbered bool
	path       [nAttrs]cpath
}

// cpath — путь с завершающим NUL: open на горячем пути передаёт его ядру без копирования (openAttr).
type cpath string

func newCPath(elem ...string) cpath { return cpath(filepath.Join(elem...) + "\x00") }

func (p cpath) String() string { return string(p[:len(p)-1]) }

func (c *Channel) target() (target, bool) {
	t := target{kind: c.src.kind, k: c.src.k, how: c.How, renumbered: c.src.renumbered}
	switch c.src.kind {
	case "iio":
		d, b, typ := c.src.dir, c.src.base, c.src.typ
		t.path = [nAttrs]cpath{newCPath(d, b+"_input"), newCPath(d, b+"_raw"), newCPath(d, b+"_scale"),
			newCPath(d, "in_"+typ+"_scale"), newCPath(d, b+"_offset"), newCPath(d, "in_"+typ+"_offset")}
	case "file":
		t.path[attrValue] = newCPath(c.src.file)
	default:
		return target{}, false
	}
	return t, true
}

// NewReader возвращает читатель канала; nil — канал читается не через sysfs (GPIO).
func (c *Channel) NewReader(keepOpen bool) *Reader {
	t, ok := c.target()
	if !ok {
		return nil
	}
	r := &Reader{t: t, keepOpen: keepOpen}
	for i := range r.fds {
		r.fds[i] = fdUnopened
	}
	return r
}

// Retarget привязывает читатель к каналу c — тому же устройству, найденному повторной инвентаризацией,
// в том числе под новым номером узла (iio:device3 вместо iio:device0). false — читатель и так читает
// этот путь и не потерян.
func (r *Reader) Retarget(c *Channel) bool {
	t, ok := c.target()
	r.mu.Lock()
	defer r.mu.Unlock()
	if !ok || r.closed || (t == r.t && !r.lost) {
		return false
	}
	r.unbind()
	r.t, r.lost = t, false
	return true
}

// Read перечитывает значение в единицах SenML. has=false — значение неизвестно, how — причина
// (Reason*) или способ получения, как у Channel.Read.
func (r *Reader) Read() (v float64, how string, has bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	switch {
	case r.closed:
		return 0, ReasonClosed, false
	case r.lost && r.t.renumbered:
		return 0, ReasonAbsent, false // по старому пути может быть чужое устройство: ждём Retarget
	}
	r.lost = false
	v, how, has = r.value()
	if r.keepOpen && how == ReasonAbsent {
		r.unbind()
		r.lost = true
	}
	return v, how, has
}

// value — значение по атрибутам канала, как Channel.Read.
func (r *Reader) value() (float64, string, bool) {
	t := &r.t
	x, why := r.attr(attrValue)
	switch {
	case why == "" && t.kind == "file":
		return round6(x * t.k), t.how, true
	case why == "":
		return round6(x * t.k), "input", true
	case t.kind == "file":
		return 0, why, false
	}
	raw, rawWhy := r.attr(attrRaw)
	if rawWhy != "" {
		if why == ReasonAbsent { // причина — от того атрибута, который есть
			why = rawWhy
		}
		return 0, why, false
	}
	scale, scaleWhy := r.attr(attrScale)
	if scaleWhy != "" {
		scale, scaleWhy = r.attr(attrTypScale)
	}
	offset, offsetWhy := r.attr(attrOffset)
	if offsetWhy != "" {
		offset, _ = r.attr(attrTypOffset) // нет файла -> 0
	}
	if scaleWhy != "" {
		return 0, ReasonNoScale, false
	}
	return round6((raw + offset) * scale * t.k), "(raw+offset)*scale", true
}

// attr читает атрибут i: с KeepOpen — через дескриптор, открытый при первом обращении после привязки,
// иначе — по пути; why — причина неудачи ("" — число прочитано).
func (r *Reader) attr(i int) (float64, string) {
	path := r.t.path[i]
	if !r.keepOpen {
		fd, err := openAttr(path)
		if err != nil {
			return 0, errReason(err)
		}
		x, why := r.preadFloat(fd, path)
		_ = unix.Close(fd)
		return x, why
	}
	switch {
	case r.lost, r.fds[i] == fdAbsent:
		return 0, ReasonAbsent
	case r.fds[i] == fdUnopened:
		fd, err := openAttr(path)
		if err != nil {
			why := errReason(err)
			if why == ReasonAbsent {
				r.fds[i] = fdAbsent
			}
			return 0, why
		}
		r.fds[i] = fd
	}
	x, why := r.preadFloat(r.fds[i], path)
	if why == ReasonAbsent { // узел удалён (ENODEV): устройство пропало, дальше по путям не читать
		r.unbind()
		r.lost = true
	}
	return x, why
}

// unbind закрывает удерживаемые дескрипторы и забывает отсутствующие атрибуты.
func (r *Reader) unbind() {
	for i, fd := range r.fds {
		if fd >= 0 {
			_ = unix.Close(fd)
		}
		r.fds[i] = fdUnopened
	}
}

// Close закрывает удерживаемые дескрипторы; дальнейшие Read возвращают has=false.
func (r *Reader) Close() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.unbind()
	r.closed = true
}

// errReason — причина по ошибке open или pread: атрибута или устройства нет (ENOENT, ENOTDIR; ENODEV —
// узел sysfs удалён или драйвер сообщает, что устройства нет) или атрибут не читается.
func errReason(err error) string {
	if errors.Is(err, unix.ENOENT) || errors.Is(err, unix.ENOTDIR) || errors.Is(err, unix.ENODEV) {
		return ReasonAbsent
	}
	return ReasonUnreadable
}

// preadFloat читает атрибут с нулевого смещения и разбирает число. Быстрый путь — обрезка ASCII-
// пробелов и NUL в буфере и ParseFloat без копирования; если он не удался, разбор повторяется
// точно как readFloat (замена недопустимого UTF-8, TrimSpace Юникода), чтобы значения совпадали
// с Channel.Read на любом содержимом. Содержимое длиннее буфера (редкий случай) перечитывается по
// пути path медленным путём прототипа.
func (r *Reader) preadFloat(fd int, path cpath) (float64, string) {
	var n int
	for {
		var err error
		n, err = unix.Pread(fd, r.buf[:], 0)
		if errors.Is(err, unix.EINTR) {
			continue
		}
		if err != nil {
			return 0, errReason(err)
		}
		break
	}
	if n == len(r.buf) {
		if v, ok := readFloat(path.String()); ok {
			return v, ""
		}
		return 0, ReasonNotANumber
	}
	b := trimASCII(r.buf[:n])
	if len(b) > 0 {
		if x, err := strconv.ParseFloat(unsafe.String(&b[0], len(b)), 64); err == nil {
			return x, ""
		}
	}
	if x, ok := parseFloat(cleanAttr(string(r.buf[:n])), true); ok {
		return x, ""
	}
	return 0, ReasonNotANumber
}

func trimASCII(b []byte) []byte {
	isSpace := func(c byte) bool {
		return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' || c == '\f' || c == 0
	}
	for len(b) > 0 && isSpace(b[0]) {
		b = b[1:]
	}
	for len(b) > 0 && isSpace(b[len(b)-1]) {
		b = b[:len(b)-1]
	}
	return b
}
