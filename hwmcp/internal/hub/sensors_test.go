package hub

import (
	"errors"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/local/hwmcp/internal/clock"
	"github.com/local/hwmcp/internal/declared"
	"github.com/local/hwmcp/internal/gate"
	"github.com/local/hwmcp/internal/gpio"
	"github.com/local/hwmcp/internal/inventory"
	"github.com/local/hwmcp/internal/sysfs"
	"github.com/local/hwmcp/internal/testutil"
)

// Пути BME280 фикстуры: каталог устройства и ссылка шины IIO на него.
const (
	bmeDir  = "sys/devices/platform/soc/1f00074000.i2c/i2c-1/1-0076"
	iioBus  = "sys/bus/iio/devices"
	bmeTemp = iioBus + "/iio:device0/in_temp_input"
)

func (e *env) prop(dev, prop string) map[string]any {
	e.t.Helper()
	st, ok := e.hub.DeviceState(dev)
	if !ok {
		e.t.Fatalf("no device %s", dev)
	}
	return st["properties"].(map[string]any)[prop].(map[string]any)
}

func (e *env) notesOf(dev string) []string {
	var out []string
	for _, n := range e.takeNotes() {
		if strings.HasPrefix(n, dev+":") {
			out = append(out, n)
		}
	}
	return out
}

func (e *env) recordsOf(typ, dev string) []map[string]any {
	var out []map[string]any
	for _, r := range e.records(typ) {
		if r["device"] == dev {
			out = append(out, r)
		}
	}
	return out
}

// Горячее отключение: показания пропавшего устройства становятся null с причиной absent (после
// FailLimit чтений), это сообщается и видно в board_health; вернувшееся устройство читается снова.
// Пока устройство отсутствует меньше периода повторной инвентаризации, её не бывает.
func TestSensorUnplugAndReplug(t *testing.T) {
	e := newEnv(t, relayPolicy, false, func(o *Options) { o.RescanPeriod = 30 })
	dev := filepath.Join(e.root, bmeDir, "iio:device0")
	e.takeNotes()
	if err := os.Rename(dev, dev+".unplugged"); err != nil { // ссылка шины IIO повисает: ENOENT
		t.Fatal(err)
	}
	e.advance(float64(DefaultFailLimit - 1))
	if n := e.notesOf("kitchen_bme280"); len(n) != 0 || e.prop("kitchen_bme280", "temperature")["value"] != 22.51 {
		t.Fatalf("within FailLimit the last good value must stay: %v", n)
	}
	e.advance(1)
	if n := e.notesOf("kitchen_bme280"); len(n) != 1 || n[0] != "kitchen_bme280:humidity,pressure,temperature" {
		t.Fatalf("unplug must be notified once for all properties: %v", n)
	}
	if v := e.prop("kitchen_bme280", "temperature"); v["value"] != nil || v["error"] != sysfs.ReasonAbsent {
		t.Fatalf("unplugged: %v", v)
	}
	faults := e.hub.BoardHealth()["sensor_faults"].(map[string]any)
	if faults["kitchen_bme280.temperature"] != sysfs.ReasonAbsent || len(faults) != 4 { // + PIR без файла линии
		t.Fatalf("board_health faults: %v", faults)
	}
	if r := e.recordsOf("sample_error", "kitchen_bme280"); len(r) != 3 || r[0]["error"] != sysfs.ReasonAbsent {
		t.Fatalf("sample_error records: %v", r)
	}

	if err := os.Rename(dev+".unplugged", dev); err != nil {
		t.Fatal(err)
	}
	e.advance(1)
	if n := e.notesOf("kitchen_bme280"); len(n) != 1 || e.prop("kitchen_bme280", "temperature")["value"] != 22.51 {
		t.Fatalf("replug must be read and notified: %v", n)
	}
	if r := e.recordsOf("sample_recovered", "kitchen_bme280"); len(r) != 3 || r[0]["down_s"] != 1.0 {
		t.Fatalf("sample_recovered records: %v", r)
	}
	e.advance(120)
	st := e.hub.Stats()
	if st["outages"].(int64) < 3 || st["recoveries"] != int64(3) || st["rescans"] != int64(0) {
		t.Fatalf("stats: %v", st)
	}
	hist := e.hub.Diag()["sensors"].(map[string]any)["kitchen_bme280.temperature"].(map[string]any)
	if hist["outages"] != 1 || hist["read_errors"] != DefaultFailLimit || hist["error"] != nil {
		t.Fatalf("diag history: %v", hist)
	}
}

// Устройство вернулось под другим номером (iio:device7 вместо iio:device0): повторная инвентаризация
// (первая — через RescanPeriod после пропажи) находит его по id инвентаря и переводит читатель на новый путь.
func TestRenumberedDeviceIsFoundAgain(t *testing.T) {
	e := newEnv(t, relayPolicy, false, func(o *Options) { o.RescanPeriod = 30 })
	bus, dir := filepath.Join(e.root, iioBus), filepath.Join(e.root, bmeDir)
	must(t, os.Remove(filepath.Join(bus, "iio:device0")))
	must(t, os.Rename(filepath.Join(dir, "iio:device0"), filepath.Join(dir, "iio:device7")))
	must(t, os.Symlink("../../../"+strings.TrimPrefix(bmeDir, "sys/")+"/iio:device7", filepath.Join(bus, "iio:device7")))
	e.advance(float64(DefaultFailLimit))
	if v := e.prop("kitchen_bme280", "pressure"); v["error"] != sysfs.ReasonAbsent {
		t.Fatalf("renumbered device must first read as absent: %v", v)
	}
	if next := e.hub.NextSampleDue(); next > e.clk.Now()+1 {
		t.Fatalf("sampling must go on while the device is lost: next %v", next)
	}
	e.advance(29) // повторная инвентаризация — через RescanPeriod после того, как пропажа замечена
	if e.hub.Stats()["rescans"] != int64(0) {
		t.Fatal("rescan before RescanPeriod")
	}
	e.advance(3)
	if v := e.prop("kitchen_bme280", "temperature"); v["value"] != 22.51 {
		t.Fatalf("rebound device must be read again: %v", v)
	}
	rel := e.recordsOf("sensor_rebound", "kitchen_bme280")
	if len(rel) != 3 || !strings.HasSuffix(rel[0]["path"].(string), "iio:device7/in_humidityrelative_input") {
		t.Fatalf("sensor_rebound: %v", rel)
	}
	st := e.hub.Stats()
	if st["rescans"] != int64(1) || st["rebound"] != int64(3) {
		t.Fatalf("stats: %v", st)
	}
	if next := e.hub.NextSampleDue(); math.IsInf(next, 1) || next > e.clk.Now()+1 {
		t.Fatalf("no rescan must stay scheduled once everything is found: %v", next)
	}
}

// «Мигающий» датчик: каждый перерыв и возврат сообщаются агенту, счётчики полные, а журнал получает не
// больше одной записи о сбое свойства за faultLogEvery.
func TestFlappingSensorLogIsRateLimited(t *testing.T) {
	e := newEnv(t, relayPolicy, false)
	attr := filepath.Join(e.root, "sys/class/hwmon/hwmon0/temp1_input")
	e.takeNotes()
	const cycles = 5
	for i := 0; i < cycles; i++ {
		must(t, os.Remove(attr))
		must(t, os.Mkdir(attr, 0o755)) // атрибут есть, но не читается
		e.advance(float64(DefaultFailLimit))
		if v := e.prop("board_cpu_thermal", "temperature1"); v["error"] != sysfs.ReasonUnreadable {
			t.Fatalf("cycle %d: %v", i, v)
		}
		must(t, os.Remove(attr))
		e.write("sys/class/hwmon/hwmon0/temp1_input", "48300\n")
		e.advance(2)
	}
	if n := e.notesOf("board_cpu_thermal"); len(n) != 2*cycles {
		t.Fatalf("every outage and recovery must be notified: %v", n)
	}
	if r := e.recordsOf("sample_error", "board_cpu_thermal"); len(r) != 1 {
		t.Fatalf("sample_error must be rate limited (%d cycles in %gs): %v", cycles, e.clk.Now()-1000, r)
	}
	if r := e.recordsOf("sample_recovered", "board_cpu_thermal"); len(r) != 1 {
		t.Fatalf("only the logged outage gets sample_recovered: %v", r)
	}
	st := e.hub.Stats()
	if st["quiet_faults"] != int64(cycles-1) || st["recoveries"] != int64(cycles) {
		t.Fatalf("stats: %v", st)
	}
	hist := e.hub.Diag()["sensors"].(map[string]any)["board_cpu_thermal.temperature1"].(map[string]any)
	if hist["read_errors"] != cycles*DefaultFailLimit || hist["outages"] != cycles {
		t.Fatalf("diag history: %v", hist)
	}
	e.advance(faultLogEvery) // окно прошло: следующий сбой снова пишется
	must(t, os.Remove(attr))
	e.advance(float64(DefaultFailLimit))
	if r := e.recordsOf("sample_error", "board_cpu_thermal"); len(r) != 2 || r[1]["outages"] != float64(cycles+1) {
		t.Fatalf("after the window: %v", r)
	}
}

// Перерыв, начавшийся внутри окна ограничения журнала, записывается по окончании окна, если ещё длится:
// датчик «мигнул» и пропал насовсем — в журнале не только первое «мигание».
func TestLastingOutageInQuietWindowIsJournaled(t *testing.T) {
	e := newEnv(t, relayPolicy, false)
	const rel = "sys/class/hwmon/hwmon0/temp1_input"
	attr := filepath.Join(e.root, rel)
	must(t, os.Remove(attr))
	e.advance(float64(DefaultFailLimit)) // перерыв с 1003 записан, окно — до 1063
	e.write(rel, "48300\n")
	e.advance(2)
	must(t, os.Remove(attr))
	e.advance(10) // второй перерыв с 1008, внутри окна
	if r := e.recordsOf("sample_error", "board_cpu_thermal"); len(r) != 1 {
		t.Fatalf("outage inside the window is not written at once: %v", r)
	}
	e.advance(faultLogEvery)
	r := e.recordsOf("sample_error", "board_cpu_thermal")
	if len(r) != 2 || r[1]["ts_clock"] != 1063.0 || r[1]["down_s"] != 55.0 || r[1]["outages"] != 2.0 {
		t.Fatalf("lasting outage must be written when the window ends: %v", r)
	}
	if st := e.hub.Stats(); st["quiet_faults"] != int64(1) {
		t.Fatalf("stats: %v", st)
	}
	e.write(rel, "48300\n")
	e.advance(1)
	if r := e.recordsOf("sample_recovered", "board_cpu_thermal"); len(r) != 2 || r[1]["down_s"] != 68.0 {
		t.Fatalf("its end is written too: %v", r)
	}
}

// Два одинаковых чипа hwmon (одно имя драйвера, разные родители): вернувшийся под новым номером находится по
// родителю, а не отбрасывается как неоднозначный. Каждый перерыв даёт свою запись sensor_rebound, и после
// возвращения период повторной инвентаризации сбрасывается: второй перерыв тоже ищется через RescanPeriod.
func TestDuplicateHwmonFoundByParent(t *testing.T) {
	e := newEnv(t, relayPolicy, false, func(o *Options) { o.RescanPeriod = 30 })
	const zones = "sys/devices/virtual/thermal/"
	e.write(zones+"thermal_zone1/hwmon1/name", "cpu_thermal\n")
	e.write(zones+"thermal_zone1/hwmon1/temp1_input", "30000\n")
	class := filepath.Join(e.root, "sys/class/hwmon")
	must(t, os.Symlink("../../devices/virtual/thermal/thermal_zone1/hwmon1", filepath.Join(class, "hwmon1")))
	prev := "hwmon0"
	for i, node := range []string{"hwmon2", "hwmon3"} {
		zone := filepath.Join(e.root, zones, "thermal_zone0")
		must(t, os.Remove(filepath.Join(class, prev)))
		must(t, os.Rename(filepath.Join(zone, prev), filepath.Join(zone, node)))
		must(t, os.Symlink("../../devices/virtual/thermal/thermal_zone0/"+node, filepath.Join(class, node)))
		prev = node
		e.advance(40) // FailLimit чтений, затем RescanPeriod
		if v := e.prop("board_cpu_thermal", "temperature1"); v["value"] != 48.3 {
			t.Fatalf("%s: device must be found again by its parent: %v", node, v)
		}
		rel := e.recordsOf("sensor_rebound", "board_cpu_thermal")
		if len(rel) != i+1 || !strings.HasSuffix(rel[i]["path"].(string), node+"/temp1_input") {
			t.Fatalf("%s: sensor_rebound: %v", node, rel)
		}
	}
}

// Устройство на месте, а атрибут читается как «нет устройства» (так iio_hwmon отвечает ENODEV, когда ушёл
// поставщик IIO). С удержанием дескрипторов повторная инвентаризация каждый раз находит устройство и
// привязывает читатель заново, но журнал получает одну запись sensor_rebound на перерыв. Период
// инвентаризации, пока устройство не вернулось, удваивается до rescanMaxPeriod и сбрасывается после
// возвращения.
func TestReboundIsJournaledOncePerOutage(t *testing.T) {
	e := fixtureEnv(t)
	const rel = "sys/class/hwmon/hwmon0/temp1_input"
	attr := filepath.Join(e.root, rel)
	must(t, os.Remove(attr))
	must(t, os.Symlink("gone", attr)) // атрибут виден в каталоге, открытие — ENOENT
	must(t, e.start(relayPolicy, false, func(o *Options) { o.KeepOpen, o.RescanPeriod = true, 5 }))
	rescans := func(want int64) {
		t.Helper()
		if st := e.hub.Stats(); st["rescans"] != want || st["rebound"] != want {
			t.Fatalf("at %g: stats %v, want %d rescans", e.clk.Now(), st, want)
		}
	}
	e.advance(60) // через 5, 15 и 35 с после пропажи (1001)
	rescans(3)
	if r := e.recordsOf("sensor_rebound", "board_cpu_thermal"); len(r) != 1 {
		t.Fatalf("sensor_rebound must be written once per outage: %v", r)
	}
	e.advance(3600) // ещё 1076, 1156, 1316, затем раз в 300 с: 1616 … 4616
	rescans(17)
	must(t, os.Remove(attr))
	e.write(rel, "48300\n")
	e.advance(300)
	if v := e.prop("board_cpu_thermal", "temperature1"); v["value"] != 48.3 {
		t.Fatalf("attribute back: %v", v)
	}
	rescans(18)
	if next := e.hub.NextSampleDue(); next > e.clk.Now()+1 {
		t.Fatalf("no rescan must stay scheduled once the device is back: next %v", next)
	}
}

// Одиночные сбои чтения не прерывают окно «застревания» и не возвращают «застрявшее» значение: у DHT11
// сбои обмена бывают чаще любого окна.
func TestStuckDespiteGlitches(t *testing.T) {
	e := newEnv(t, relayPolicy, false, func(o *Options) {
		o.StuckAfter = map[string]float64{"board_cpu_thermal": 10}
	})
	const rel = "sys/class/hwmon/hwmon0/temp1_input"
	glitch := func() {
		must(t, os.Remove(filepath.Join(e.root, rel)))
		e.advance(1) // одно неудачное чтение
		e.write(rel, "48300\n")
	}
	for i := 0; i < 2; i++ {
		e.advance(3)
		glitch()
	}
	e.advance(2)
	if v := e.prop("board_cpu_thermal", "temperature1"); v["value"] != nil || v["error"] != ReasonStuck {
		t.Fatalf("unchanged for 10 s despite glitches: %v", v)
	}
	glitch()
	e.advance(1)
	if v := e.prop("board_cpu_thermal", "temperature1"); v["value"] != nil || v["error"] != ReasonStuck {
		t.Fatalf("a glitch must not make a stuck value trusted again: %v", v)
	}
	hist := e.hub.Diag()["sensors"].(map[string]any)["board_cpu_thermal.temperature1"].(map[string]any)
	if st := e.hub.Stats(); st["sample_glitches"] != int64(3) || hist["outages"] != 1 || hist["read_errors"] != 3 {
		t.Fatalf("stats: %v, history: %v", st, hist)
	}
}

// Значение, не менявшееся дольше stuck_after_s, становится null с причиной stuck; первое изменение
// возвращает его. Это не ошибка чтения: счётчик неудачных чтений не растёт.
func TestStuckValueBecomesUnknown(t *testing.T) {
	e := newEnv(t, relayPolicy, false, func(o *Options) {
		o.StuckAfter = map[string]float64{"kitchen_bme280.temperature": 10}
	})
	e.takeNotes()
	e.advance(9)
	if v := e.prop("kitchen_bme280", "temperature"); v["value"] != 22.51 {
		t.Fatalf("before the window: %v", v)
	}
	e.advance(1)
	if v := e.prop("kitchen_bme280", "temperature"); v["value"] != nil || v["error"] != ReasonStuck {
		t.Fatalf("stuck: %v", v)
	}
	if n := e.notesOf("kitchen_bme280"); len(n) != 1 || n[0] != "kitchen_bme280:temperature" {
		t.Fatalf("notes: %v", n)
	}
	if e.prop("kitchen_bme280", "humidity")["value"] == nil {
		t.Fatal("window applies only to the configured property")
	}
	e.advance(30)
	e.write(bmeTemp, "22600\n")
	e.advance(1)
	if v := e.prop("kitchen_bme280", "temperature"); v["value"] != 22.6 {
		t.Fatalf("changed value must be trusted again: %v", v)
	}
	if n := e.notesOf("kitchen_bme280"); len(n) != 1 {
		t.Fatalf("recovery notes: %v", n)
	}
	hist := e.hub.Diag()["sensors"].(map[string]any)["kitchen_bme280.temperature"].(map[string]any)
	if hist["read_errors"] != 0 || hist["outages"] != 1 {
		t.Fatalf("stuck is an outage, not a read error: %v", hist)
	}
	if p := e.hub.SamplePeriods()["kitchen_bme280.temperature"].(map[string]any); p["stuck_after_s"] != 10.0 {
		t.Fatalf("sample periods: %v", p)
	}
}

func TestStuckConfigValidation(t *testing.T) {
	for name, stuck := range map[string]map[string]float64{
		"unknown key":            {"kitchen_bme280.speed": 60},
		"boolean input":          {"pir_hall.state": 60},
		"output":                 {"relay_fan.state": 60},
		"device without numbers": {"pir_hall": 60},
		"output device":          {"relay_fan": 60},
		"shorter period":         {"kitchen_bme280": 1},
	} {
		if err := fixtureEnv(t).start(relayPolicy, false, func(o *Options) { o.StuckAfter = stuck }); err == nil {
			t.Errorf("%s: expected an error", name)
		}
	}
}

// refusing — драйвер линий, отвергающий запросы с фронтами (у линии нет прерывания — ядро отвечает ENXIO)
// или, при all, любые.
type refusing struct {
	gpio.Files
	all bool
}

var errNoIRQ = errors.New("no such device or address")

func (b *refusing) Open(cfg gpio.LineConfig) (gpio.Line, error) {
	if b.all || cfg.OnEdge != nil {
		return nil, errNoIRQ
	}
	return b.Files.Open(cfg)
}

// Вход на линии без прерывания (расширитель GPIO без вывода INT) открывается без фронтов и опрашивается.
func TestEdgesFallBackToPolling(t *testing.T) {
	e := fixtureEnv(t)
	e.backend = &refusing{Files: gpio.Files{Dir: e.gdir}}
	must(t, e.start(relayPolicy, false, func(o *Options) { o.Edges = true }))
	r := e.records("line_error")
	if len(r) != 1 || r[0]["device"] != "pir_hall" || r[0]["fallback"] != "poll" || r[0]["error"] != errNoIRQ.Error() {
		t.Fatalf("line_error: %v", r)
	}
	e.write("dev/gpiochip0/line17", "1\n")
	e.advance(1)
	if v := e.prop("pir_hall", "state"); v["value"] != true {
		t.Fatalf("polled input: %v", v)
	}
}

// Линия, не открывшаяся при старте, заново не запрашивается: её значение — ошибка драйвера, она не
// опрашивается и не перечитывается, счётчик неудачных чтений не растёт.
func TestUnopenedLineIsNotPolled(t *testing.T) {
	e := fixtureEnv(t)
	e.backend = &refusing{Files: gpio.Files{Dir: e.gdir}, all: true}
	must(t, e.start(relayPolicy, false))
	e.advance(10)
	if _, err := e.hub.Read("pir_hall", "state"); err != nil {
		t.Fatal(err)
	}
	bh := e.hub.BoardHealth()
	faults := bh["sensor_faults"].(map[string]any)
	if faults["pir_hall.state"] != errNoIRQ.Error() || faults["relay_fan.state"] != errNoIRQ.Error() ||
		bh["sensor_errors"] != int64(0) {
		t.Fatalf("board_health: %v", bh)
	}
	if _, polled := e.hub.SamplePeriods()["pir_hall.state"]; polled || len(e.records("line_error")) != 2 {
		t.Fatalf("unopened input must not be polled: %v", e.hub.SamplePeriods())
	}
}

// Сроки опроса свойств с периодами 0,1 и 0,3 с совпадают каждые 0,3 с, но в float64 расходятся на ошибку
// округления: сэмплер должен просыпаться один раз на совпадающие сроки (dueSlack).
func TestCloseDeadlinesShareOneWakeup(t *testing.T) {
	e := newEnv(t, relayPolicy, false, func(o *Options) {
		o.SamplePeriod, o.DriverPeriods = 0.3, map[string]float64{}
		o.Periods = map[string]float64{"kitchen_bme280.temperature": 0.1}
	})
	start := e.clk.Now()
	wakeups := 0
	for {
		next := e.hub.NextSampleDue()
		if next > start+3+dueSlack {
			break
		}
		must(t, e.clk.Set(math.Max(next, e.clk.Now())))
		e.hub.StepSample(e.clk.Now())
		wakeups++
	}
	if wakeups != 30 {
		t.Fatalf("%d wakeups in 3 s with deadlines every 0.1 s, want 30", wakeups)
	}
}

// Горячий путь сэмплера: опрос датчиков sysfs без изменения значений не выделяет память (и с удержанием
// дескрипторов, как на плате, и без него). Только Linux: вне его unix.Open копирует путь (sysfs.openAttr).
func TestSamplingDoesNotAllocate(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("allocation-free open is Linux-only; run in the VM (make vm-test)")
	}
	for _, keep := range []bool{false, true} {
		root := testutil.FixtureCopy(t)
		decl := &declared.Declared{}
		inv, err := inventory.Build(sysfs.Probe(root, decl), decl, inventory.Options{})
		if err != nil {
			t.Fatal(err)
		}
		clk := clock.NewVirtual(0)
		h, err := New(inv, gate.Config{}, &gpio.Files{Dir: t.TempDir()},
			Options{Clock: clk, SamplePeriod: 1, DriverPeriods: map[string]float64{}, KeepOpen: keep, Root: root})
		if err != nil {
			t.Fatal(err)
		}
		i := 0
		allocs := testing.AllocsPerRun(50, func() {
			i++
			_ = clk.Set(float64(i))
			h.StepSample(float64(i))
		})
		if got := h.Stats()["samples"].(int64); got < 50*int64(len(h.inputs)) || len(h.inputs) < 8 {
			t.Fatalf("keep=%v: %d samples of %d inputs", keep, got, len(h.inputs))
		}
		if allocs != 0 {
			t.Errorf("keep=%v: %v allocations per sampling step", keep, allocs)
		}
		h.Close()
	}
}

// BenchmarkStepSample — шаг опроса всех датчиков sysfs фикстуры без изменения значений.
func BenchmarkStepSample(b *testing.B) {
	for _, keep := range []bool{false, true} {
		b.Run(map[bool]string{false: "open-read-close", true: "keep-open"}[keep], func(b *testing.B) {
			root := testutil.FixtureCopy(b)
			decl := &declared.Declared{}
			inv, err := inventory.Build(sysfs.Probe(root, decl), decl, inventory.Options{})
			if err != nil {
				b.Fatal(err)
			}
			clk := clock.NewVirtual(0)
			h, err := New(inv, gate.Config{}, &gpio.Files{Dir: b.TempDir()},
				Options{Clock: clk, SamplePeriod: 1, DriverPeriods: map[string]float64{}, KeepOpen: keep, Root: root})
			if err != nil {
				b.Fatal(err)
			}
			defer h.Close()
			b.ReportAllocs()
			i := 0
			for b.Loop() {
				i++
				_ = clk.Set(float64(i))
				h.StepSample(float64(i))
			}
			b.ReportMetric(float64(len(h.inputs)), "inputs")
		})
	}
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}
