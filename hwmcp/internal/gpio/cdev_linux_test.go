//go:build linux

package gpio

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

// Раскладка структур должна совпадать с include/uapi/linux/gpio.h, иначе ioctl испортит память.
func TestUAPIStructSizes(t *testing.T) {
	for name, c := range map[string]struct{ got, want uintptr }{
		"gpiochip_info":          {unsafe.Sizeof(gpioChipInfo{}), 68},
		"gpio_v2_line_request":   {unsafe.Sizeof(gpioV2LineRequest{}), 592},
		"gpio_v2_line_config":    {unsafe.Sizeof(gpioV2LineConfig{}), 272},
		"gpio_v2_line_values":    {unsafe.Sizeof(gpioV2LineValues{}), 16},
		"gpio_v2_line_info":      {unsafe.Sizeof(gpioV2LineInfo{}), 256},
		"gpio_v2_line_attribute": {unsafe.Sizeof(gpioV2LineAttribute{}), 16},
		"gpio_v2_line_event":     {unsafe.Sizeof(gpioV2LineEvent{}), 48},
		"gpioline_info":          {unsafe.Sizeof(gpioLineInfoV1{}), 72},
		"gpiohandle_request":     {unsafe.Sizeof(gpioHandleRequest{}), 364},
		"gpiohandle_data":        {unsafe.Sizeof(gpioHandleData{}), 64},
		"gpioevent_request":      {unsafe.Sizeof(gpioEventRequest{}), 48},
		"gpioevent_data":         {unsafe.Sizeof(gpioEventData{}), 16},
	} {
		if c.got != c.want {
			t.Errorf("sizeof(%s) = %d, want %d", name, c.got, c.want)
		}
	}
	if off := unsafe.Offsetof(gpioEventData{}.ID); off != unsafe.Offsetof(gpioV2LineEvent{}.ID) || off != 8 {
		t.Errorf("event id offset %d", off)
	}
	// значения макросов _IOR/_IOWR из linux/gpio.h (ядро 6.8, совпадают с 4.9 для v1)
	for name, c := range map[string]struct{ got, want uintptr }{
		"GPIO_GET_CHIPINFO_IOCTL":          {ioctlGetChipInfo, 0x8044B401},
		"GPIO_V2_GET_LINEINFO_IOCTL":       {ioctlV2GetLineInfo, 0xC100B405},
		"GPIO_V2_GET_LINE_IOCTL":           {ioctlV2GetLine, 0xC250B407},
		"GPIO_V2_LINE_GET_VALUES_IOCTL":    {ioctlV2GetValues, 0xC010B40E},
		"GPIO_V2_LINE_SET_VALUES_IOCTL":    {ioctlV2SetValues, 0xC010B40F},
		"GPIO_GET_LINEINFO_IOCTL":          {ioctlV1GetLineInfo, 0xC048B402},
		"GPIO_GET_LINEHANDLE_IOCTL":        {ioctlV1GetLineHandle, 0xC16CB403},
		"GPIO_GET_LINEEVENT_IOCTL":         {ioctlV1GetLineEvent, 0xC030B404},
		"GPIOHANDLE_GET_LINE_VALUES_IOCTL": {ioctlV1GetValues, 0xC040B408},
		"GPIOHANDLE_SET_LINE_VALUES_IOCTL": {ioctlV1SetValues, 0xC040B409},
	} {
		if c.got != c.want {
			t.Errorf("%s = %#x, want %#x", name, c.got, c.want)
		}
	}
}

// Выбор uAPI в режиме auto по исходу пробного ioctl v2. На ядре 6.8 auto всегда даёт v2, поэтому ветка v1
// (ядро 4.9 Jetson Nano отвечает EINVAL) проверяется здесь.
func TestUAPIByProbe(t *testing.T) {
	for _, c := range []struct {
		err  error
		want string
	}{{nil, UAPIv2}, {unix.EINVAL, UAPIv1}, {unix.ENOTTY, UAPIv1}, {unix.EPERM, ""}, {unix.ENODEV, ""}} {
		got, err := uapiByProbe(c.err)
		if got != c.want || (err != nil) != (c.want == "") {
			t.Errorf("%v: %q %v, want %q", c.err, got, err, c.want)
		}
	}
}

func TestCdevRejectsBadUAPI(t *testing.T) {
	if _, err := (&Cdev{UAPI: "v3"}).Open(LineConfig{Chip: "gpiochip0"}); err == nil ||
		!strings.Contains(err.Error(), "auto, v1 or v2") {
		t.Fatalf("unknown uAPI must be refused: %v", err)
	}
	if _, err := (&Cdev{DevDir: t.TempDir()}).Open(LineConfig{Chip: "relay-board"}); err == nil ||
		!strings.Contains(err.Error(), `no chip named or labelled "relay-board"`) {
		t.Fatalf("missing label: %v", err)
	}
}

// Проверка на модуле ядра gpio-sim (настоящее ядро, обе версии uAPI): чипы создаёт
// tools/vm_gpiosim_check.sh и передаёт метки. Нужен root; меняет только линии этих чипов.
//
//	HWMCP_GPIOSIM_LABEL=<метка чипа на 8 линий> HWMCP_GPIOSIM_DUP=<метка двух других чипов> ./gpio.test
func TestCdevOnGpioSim(t *testing.T) {
	label := os.Getenv("HWMCP_GPIOSIM_LABEL")
	if label == "" {
		t.Skip("set HWMCP_GPIOSIM_LABEL (see tools/vm_gpiosim_check.sh) to test on gpio-sim")
	}
	auto := &Cdev{}
	info, _, err := auto.Info(label)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("label %q -> %s, %d lines, auto uAPI %s", label, info.Name, info.Lines, info.UAPI)
	if info.Label != label || info.Lines < 8 || info.UAPI != UAPIv2 {
		t.Fatalf("chip %+v: want 8 lines and uAPI v2 detected on a modern kernel", info)
	}
	if dup := os.Getenv("HWMCP_GPIOSIM_DUP"); dup != "" {
		_, err := auto.Open(LineConfig{Chip: dup, Offset: 0})
		if err == nil || !strings.Contains(err.Error(), "ambiguous") {
			t.Fatalf("label shared by two chips must be refused: %v", err)
		}
		t.Logf("ambiguous label: %v", err)
	}
	sim := simChip{t: t, dir: filepath.Join("/sys/bus/gpio/devices", info.Name)}
	for _, uapi := range []string{UAPIv1, UAPIv2} {
		t.Run(uapi, func(t *testing.T) {
			sim.t = t
			c := &Cdev{UAPI: uapi}
			if got, _, err := c.Info(label); err != nil || got.UAPI != uapi {
				t.Fatalf("forced uAPI: %+v %v", got, err)
			}
			testSimInput(t, c, label, &sim)
			testSimEdges(t, c, label, &sim)
			testSimOutput(t, c, label, &sim)
		})
	}
}

// simChip — линии gpio-sim: вход двигается записью pull-up/pull-down в sim_gpio<K>/pull, уровень
// выхода читается из sim_gpio<K>/value (Documentation/admin-guide/gpio/gpio-sim.rst).
type simChip struct {
	t   *testing.T
	dir string
}

func (s *simChip) pull(line int, up bool) {
	s.t.Helper()
	v := map[bool]string{true: "pull-up", false: "pull-down"}[up]
	if err := os.WriteFile(filepath.Join(s.dir, "sim_gpio"+strconv.Itoa(line), "pull"), []byte(v), 0); err != nil {
		s.t.Fatal(err)
	}
}

func (s *simChip) value(line int) string {
	s.t.Helper()
	b, err := os.ReadFile(filepath.Join(s.dir, "sim_gpio"+strconv.Itoa(line), "value"))
	if err != nil {
		s.t.Fatal(err)
	}
	return strings.TrimSpace(string(b))
}

func testSimInput(t *testing.T, c *Cdev, label string, sim *simChip) {
	for _, activeLow := range []bool{false, true} {
		sim.pull(0, false)
		in, err := c.Open(LineConfig{Chip: label, Offset: 0, ActiveLow: activeLow, Consumer: "hwmcp-test"})
		if err != nil {
			t.Fatal(err)
		}
		for _, up := range []bool{true, false, true} {
			sim.pull(0, up)
			if v, err := in.Get(); err != nil || v != (up != activeLow) {
				t.Fatalf("active_low=%v pull-up=%v: got %v %v", activeLow, up, v, err)
			}
		}
		if err := in.Set(true); err != ErrNotOutput {
			t.Fatalf("input Set: %v", err)
		}
		if _, err := c.Open(LineConfig{Chip: label, Offset: 0}); err == nil {
			t.Fatal("second request of a busy line must fail")
		}
		if err := in.Close(); err != nil {
			t.Fatal(err)
		}
	}
}

func testSimEdges(t *testing.T, c *Cdev, label string, sim *simChip) {
	edges := make(chan bool, 16)
	expect := func(want ...bool) {
		t.Helper()
		for i, w := range want {
			select {
			case got := <-edges:
				if got != w {
					t.Fatalf("edge %d: got %v, want %v", i, got, w)
				}
			case <-time.After(2 * time.Second):
				t.Fatalf("edge %d (%v) did not arrive", i, w)
			}
		}
		select {
		case extra := <-edges:
			t.Fatalf("unexpected edge %v", extra)
		case <-time.After(100 * time.Millisecond):
		}
	}
	onEdge := func(v bool) { edges <- v }

	sim.pull(1, false)
	in, err := c.Open(LineConfig{Chip: label, Offset: 1, Consumer: "hwmcp-test", OnEdge: onEdge})
	if err != nil {
		t.Fatal(err)
	}
	sim.pull(1, true)
	expect(true)
	sim.pull(1, false)
	expect(false)
	closeWithin(t, in)

	// active_low: подтяжка вверх — линия неактивна
	sim.pull(2, true)
	in, err = c.Open(LineConfig{Chip: label, Offset: 2, ActiveLow: true, Consumer: "hwmcp-test", OnEdge: onEdge})
	if err != nil {
		t.Fatal(err)
	}
	sim.pull(2, false)
	expect(true)
	sim.pull(2, true)
	expect(false)
	closeWithin(t, in)

	// дребезг короче окна: одно событие с установившимся уровнем (v2 — ядро, v1 — драйвер)
	sim.pull(3, false)
	in, err = c.Open(LineConfig{Chip: label, Offset: 3, Consumer: "hwmcp-test", DebounceMs: 50, OnEdge: onEdge})
	if err != nil {
		t.Fatal(err)
	}
	for _, up := range []bool{true, false, true, false, true} {
		sim.pull(3, up)
	}
	expect(true)
	closeWithin(t, in)
}

func testSimOutput(t *testing.T, c *Cdev, label string, sim *simChip) {
	out, err := c.Open(LineConfig{Chip: label, Offset: 4, Output: true, Initial: true, Consumer: "hwmcp-test"})
	if err != nil {
		t.Fatal(err)
	}
	if sim.value(4) != "1" {
		t.Fatalf("initial value: %s", sim.value(4))
	}
	for _, want := range []bool{false, true, false} {
		if err := out.Set(want); err != nil {
			t.Fatal(err)
		}
		if got, err := out.Get(); err != nil || got != want {
			t.Fatalf("readback %v %v", got, err)
		}
		if lvl := sim.value(4); lvl != map[bool]string{true: "1", false: "0"}[want] {
			t.Fatalf("set %v: sim level %s", want, lvl)
		}
	}
	_, lines, err := c.Info(label)
	if err != nil {
		t.Fatal(err)
	}
	if l := lines[4]; l.Consumer != "hwmcp-test" || !l.Used || !l.Output {
		t.Fatalf("line info after request: %+v", l)
	}
	closeWithin(t, out)

	low, err := c.Open(LineConfig{Chip: label, Offset: 5, Output: true, ActiveLow: true, Consumer: "hwmcp-test"})
	if err != nil {
		t.Fatal(err)
	}
	if sim.value(5) != "1" { // логический 0 при active_low — высокий уровень
		t.Fatalf("active-low initial level %s", sim.value(5))
	}
	if err := low.Set(true); err != nil || sim.value(5) != "0" {
		t.Fatalf("active-low set: %v, level %s", err, sim.value(5))
	}
	closeWithin(t, low)
}

func closeWithin(t *testing.T, l Line) {
	t.Helper()
	done := make(chan error, 1)
	go func() { done <- l.Close() }()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Close did not interrupt the event reader")
	}
}

// Проверка на настоящем чипе платы — только по явной просьбе (меняет уровни линий!):
//
//	HWMCP_GPIO_TEST_CHIP=gpiochip0 HWMCP_GPIO_TEST_IN=0 HWMCP_GPIO_TEST_OUT=1 [HWMCP_GPIO_TEST_UAPI=v1] sudo -E ./gpio.test
//
// Линии должны быть свободны и ни к чему не подключены. HWMCP_GPIO_TEST_NO_READBACK=1 отключает проверку
// чтения выставленного уровня: модель PL061 в QEMU не возвращает уровень выхода.
func TestCdevOnRealChip(t *testing.T) {
	chip := os.Getenv("HWMCP_GPIO_TEST_CHIP")
	if chip == "" {
		t.Skip("set HWMCP_GPIO_TEST_CHIP (and _IN/_OUT line numbers) to test a real gpiochip")
	}
	inLine, err1 := strconv.Atoi(os.Getenv("HWMCP_GPIO_TEST_IN"))
	outLine, err2 := strconv.Atoi(os.Getenv("HWMCP_GPIO_TEST_OUT"))
	if err1 != nil || err2 != nil {
		t.Fatal("HWMCP_GPIO_TEST_IN and HWMCP_GPIO_TEST_OUT must be line numbers")
	}
	c := &Cdev{UAPI: os.Getenv("HWMCP_GPIO_TEST_UAPI")}
	info, lines, err := c.Info(chip)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("chip %s label=%q lines=%d uapi=%s", info.Name, info.Label, info.Lines, info.UAPI)
	for _, l := range lines {
		t.Logf("  line %d name=%q consumer=%q used=%v output=%v", l.Offset, l.Name, l.Consumer, l.Used, l.Output)
	}
	in, err := c.Open(LineConfig{Chip: chip, Offset: inLine, Consumer: "hw-mcp-test", DebounceMs: 5,
		OnEdge: func(bool) {}})
	if err != nil {
		t.Fatalf("edge request: %v", err)
	}
	v, err := in.Get()
	t.Logf("input line %d = %v (%v)", inLine, v, err)
	if err != nil {
		t.Fatal(err)
	}
	closeWithin(t, in)

	out, err := c.Open(LineConfig{Chip: chip, Offset: outLine, Output: true, Consumer: "hw-mcp-test"})
	if err != nil {
		t.Fatal(err)
	}
	defer out.Close()
	readback := os.Getenv("HWMCP_GPIO_TEST_NO_READBACK") != "1"
	for _, want := range []bool{true, false, true, false} {
		if err := out.Set(want); err != nil {
			t.Fatal(err)
		}
		got, err := out.Get()
		if err != nil {
			t.Fatal(err)
		}
		if readback && got != want {
			t.Fatalf("output readback: got %v, want %v", got, want)
		}
	}
	if _, err := c.Open(LineConfig{Chip: chip, Offset: outLine, Output: true}); err == nil {
		t.Fatal("second request of a busy line must fail")
	}
}
