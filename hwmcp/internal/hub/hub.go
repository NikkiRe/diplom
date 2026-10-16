// Package hub — среда исполнения hw-mcp: состояние устройств (L1), фоновый сэмплер с порогом
// изменения, исполнение команд через шлюз приводов, таймеры (аренда, импульсы) и журнал.
//
// Время берётся только из clock.Clock. В реальном режиме RunSampler и RunTimers (run.go) спят до
// ближайшего срока: опрос — по расписанию каждого свойства с сохранением фазы, таймеры — до
// ближайшего конца аренды или импульса (без аренд и импульсов пробуждений нет совсем; Actuate будит
// таймеры через канал). В виртуальном режиме AdvanceTo проходит все сроки по порядку, поэтому события
// получают точное виртуальное время.
//
// Уведомления об изменениях (OnChange) вызываются синхронно из сэмплера, таймеров и обработчика
// фронтов, поэтому слушатель обязан не блокироваться: MCP-сервер кладёт их в свою очередь
// (server.dispatcher), и зависший клиент не останавливает ни опрос, ни сторожевой таймер аренды.
//
// Блокировки: actMu сериализует шлюз, запись в линии и таймеры (проверка и исполнение команды атомарны —
// нет TOCTOU между решением и записью); stMu защищает состояние ключей; smpMu сериализует опросы.
// Порядок захвата: smpMu -> actMu -> stMu. Чтение датчиков (в т.ч. блокирующее 750 мс у DS18B20) идёт
// без actMu и stMu; результат опроса не записывается, если за время чтения ключ обновился (фронт GPIO,
// read_property): у каждого ключа есть номер версии.
//
// Живые датчики (sensors.go): пропавшее устройство, нечитаемый атрибут или «застрявшее» значение дают
// null с причиной; сбои считаются по свойствам, журнал пишет не чаще раза в faultLogEvery на свойство;
// пропавшие устройства IIO и hwmon ищутся заново под новыми номерами узлов.
package hub

import (
	"errors"
	"fmt"
	"log/slog"
	"math"
	"slices"
	"sort"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/local/hwmcp/internal/argschema"
	"github.com/local/hwmcp/internal/clock"
	"github.com/local/hwmcp/internal/gate"
	"github.com/local/hwmcp/internal/gpio"
	"github.com/local/hwmcp/internal/inventory"
	"github.com/local/hwmcp/internal/sysfs"
	"github.com/local/hwmcp/internal/trace"
)

// Значения по умолчанию политики уведомлений и опроса.
const (
	DefaultFailLimit   = 3     // неудачных чтений подряд до объявления ошибки (значение null)
	DefaultPersist     = 3     // отсчётов, которые должно продержаться изменение на один шаг квантования
	DefaultIdemMaxAge  = 600.0 // с: ключи идемпотентности старше вытесняются (если политика не задала свой)
	defaultSlowDriverW = "w1"
)

// DefaultDriverPeriods — периоды опроса медленных драйверов по умолчанию (Options.DriverPeriods; из
// командной строки hw-mcp не переопределяются, действующие периоды пишутся в журнал ready):
// dht11 (и DHT22) — драйвер ядра всё равно кэширует измерение на 2 с (DHT11_DATA_VALID_TIME);
// w1 (DS18B20) — каждое чтение запускает преобразование 750 мс на шине 1-Wire (у w1-gpio — с
// программными задержками), а температура трубы меняется медленно. Это параметры эксперимента.
var DefaultDriverPeriods = map[string]float64{"dht11": 2, "w1": 10}

// Options — параметры среды исполнения.
type Options struct {
	Clock        clock.Clock
	SamplePeriod float64 // период фонового опроса по умолчанию, с
	// Periods — явные периоды опроса: "<устройство>" или "<устройство>.<свойство>" -> с
	// (declared.json: sample_periods; приоритет свойства выше устройства).
	Periods map[string]float64
	// DriverPeriods — периоды по умолчанию для медленных драйверов ("w1", "dht11"), не меньше
	// SamplePeriod. nil — DefaultDriverPeriods; пустая карта — без особых периодов.
	DriverPeriods map[string]float64
	// CacheTTL — read_property перечитывает значение старше, с; 0 — два периода опроса свойства.
	CacheTTL float64
	// NotifyMinInterval — не чаще одного уведомления на числовое свойство за столько секунд (0 — без
	// ограничения). Изменение внутри интервала не теряется: оно сообщается первым опросом после него.
	NotifyMinInterval float64
	// FailLimit — столько неудачных чтений подряд, прежде чем значение станет неизвестным (null);
	// до этого остаётся последнее хорошее значение, и его возраст растёт. 0 — DefaultFailLimit.
	FailLimit int
	// Persist — изменение не больше одного наблюдённого шага квантования датчика сообщается, только
	// если продержалось столько отсчётов подряд (дрожание младшего разряда не даёт уведомлений).
	// 0 — DefaultPersist, 1 — сразу.
	Persist int
	// StuckAfter — окна «застревания» (declared.json: stuck_after_s): "<устройство>" или
	// "<устройство>.<свойство>" -> с. Число, не менявшееся дольше окна, становится null с причиной stuck.
	StuckAfter map[string]float64
	// RescanPeriod — пока устройство IIO или hwmon пропало (чтение absent), эти источники инвентаризуются
	// заново: вернувшееся устройство ядро может пронумеровать иначе. Первый раз — через RescanPeriod после
	// пропажи, дальше период удваивается до rescanMaxPeriod. 0 — не искать.
	RescanPeriod float64
	// KeepOpen — держать дескрипторы атрибутов sysfs открытыми и читать pread с нуля (sysfs.Reader).
	KeepOpen bool
	Trace    *trace.Writer
	Logger   *slog.Logger
	Root     string // корень sysfs/procfs (board_health, повторная инвентаризация); пусто — /
	Consumer string // имя потребителя линий GPIO
	// Edges — подписаться на фронты входов GPIO (драйвер Cdev): изменение сообщается сразу,
	// а не в следующем опросе, и короткий импульс PIR не теряется между опросами.
	Edges bool
}

// Reading — значение свойства.
type Reading struct {
	Value any     // float64 | bool | nil (неизвестно)
	TS    float64 // время получения (часы hw-mcp)
	Err   string  // причина, если значение неизвестно
}

// Change — изменение свойства, о котором сообщается.
type Change struct {
	Property string
	Value    any // float64 | bool | nil
	TS       float64
}

// Update — изменения одного устройства (одно уведомление resources/updated).
type Update struct {
	Device  string
	Changes []Change // по имени свойства
	TS      float64
}

// Props — имена изменившихся свойств.
func (u Update) Props() []string {
	out := make([]string, len(u.Changes))
	for i, c := range u.Changes {
		out[i] = c.Property
	}
	return out
}

// ActRequest — команда приводу (из инструмента MCP).
type ActRequest struct {
	Device    string
	Tool      string // set_<свойство> | pulse | renew_lease
	Args      any
	CmdID     string   // ключ идемпотентности от оркестратора; пусто — сгенерировать
	IssuedAt  *float64 // момент решения LLM (часы hw-mcp); nil — сейчас
	TTL       *float64
	Confirmed bool
	DryRun    bool
	Origin    string // имя инструмента MCP (для журнала)
}

// ActResult — исход команды.
type ActResult struct {
	CmdID      string
	Decision   gate.Decision
	Executed   bool           // привод действительно переключён
	Applied    map[string]any // свойство -> записанное значение
	Err        error          // ошибка записи после решения allow/clamp
	LeaseUntil *float64       // срок аренды после команды
	PulseUntil *float64       // конец импульса
	Renewed    *bool          // для renew_lease
}

// Классы стоимости чтения: порядок опроса внутри цикла (быстрые — раньше, результаты быстрых
// записываются и сообщаются до чтения медленных).
const (
	classGPIO = iota
	classSysfs
	classSlow // dht11
	classW1   // DS18B20: 750 мс
)

// keyState — состояние одного свойства (ключ "<устройство>.<свойство>"). Поля после комментария
// «под stMu» меняются только под stMu; остальные неизменны после New.
type keyState struct {
	key      string
	dev      *inventory.Device
	prop     *inventory.Property
	output   bool
	line     gpio.Line     // линия GPIO (nil — датчик sysfs или линия не открылась)
	reader   *sysfs.Reader // датчик sysfs
	relocate bool          // устройство может вернуться под другим номером узла (sysfs.Channel.Renumbered)
	class    int
	period   float64 // период опроса входа, с
	source   string  // откуда период: default | driver | declared
	ttl      float64 // кэш read_property, с
	stuck    float64 // окно «застревания», с (0 — не проверяется)

	box atomic.Value // последнее упакованное в any число (boxed)

	// под stMu
	next       float64 // срок следующего опроса
	r          Reading // текущее значение (при сбое в пределах FailLimit — последнее хорошее)
	has        bool
	ver        uint64 // растёт при каждой записи: опрос не перезаписывает более новое значение
	hasBase    bool
	reported   any     // последнее сообщённое значение
	reportedAt float64 // когда сообщено
	q          float64 // наблюдённый шаг квантования (минимальное ненулевое |Δ| соседних отсчётов)
	prev       float64 // предыдущий хороший отсчёт (одиночные сбои между ними не в счёт, перерыв — в счёт)
	hasPrev    bool
	since      float64 // с какого момента число равно prev (для stuck)
	pending    int     // отсчётов подряд с изменением на один шаг квантования
	fails      int     // неудачных чтений подряд
	faults     faults
}

// fixed — значение не перечитывается: выход (линия занята hw-mcp монопольно) или линия, не открывшаяся при
// старте (её значение — ошибка запроса; заново линия не запрашивается).
func (ks *keyState) fixed() bool { return ks.output || ks.prop.GPIO != nil && ks.line == nil }

// view — значение для чтения: у выхода значение известно в любой момент (линия занята hw-mcp
// монопольно), поэтому его отметка времени — сейчас.
func (ks *keyState) view(now float64) Reading {
	r := ks.r
	if ks.output && ks.has && r.Err == "" {
		r.TS = now
	}
	return r
}

// Hub — среда исполнения.
type Hub struct {
	opts Options
	inv  *inventory.Inventory
	gate *gate.Gate
	log  *slog.Logger

	keys    map[string]*keyState // неизменна после New
	inputs  []*keyState          // опрашиваемые свойства в порядке опроса
	outputs []*keyState

	actMu  sync.Mutex
	pulses map[string]pulse // устройство -> активный импульс
	cmdSeq int64
	counts map[string]int // решения по вердиктам
	closed atomic.Bool

	stMu        sync.RWMutex
	rescanAt    float64 // срок повторной инвентаризации (+Inf — не нужна); под stMu
	rescanEvery float64 // текущий период повторной инвентаризации (растёт, пока устройство не найдено); под stMu

	smpMu  sync.Mutex // опросы (StepSample, первый опрос, закрытие читателей)
	dueBuf []*keyState
	verBuf []uint64
	batch  []polled

	advMu sync.Mutex // AdvanceTo

	wake chan struct{} // сроки таймеров изменились (RunTimers)

	lmu       sync.Mutex
	listeners []func(Update)

	samples, sampleErrors, glitches, superseded atomic.Int64
	notifications, edges                        atomic.Int64
	quietQuant, quietInterval                   atomic.Int64
	timerWakeups, sampleWakeups                 atomic.Int64
	outages, recoveries, quietFaults            atomic.Int64
	rescans, rebound                            atomic.Int64
	started                                     float64
}

type pulse struct {
	prop  string
	until float64
}

type polled struct {
	ks  *keyState
	ver uint64
	r   Reading
}

// New строит среду: открывает линии GPIO (выходы — сразу в безопасное значение), шлюз и первый опрос.
func New(inv *inventory.Inventory, gateCfg gate.Config, backend gpio.Backend, opts Options) (*Hub, error) {
	if opts.Clock == nil {
		return nil, errors.New("hub: clock is required")
	}
	if opts.SamplePeriod <= 0 {
		return nil, errors.New("hub: sample period must be > 0")
	}
	if opts.Logger == nil {
		opts.Logger = slog.New(slog.DiscardHandler)
	}
	if opts.Consumer == "" {
		opts.Consumer = "hw-mcp"
	}
	if opts.Root == "" {
		opts.Root = "/"
	}
	if opts.FailLimit <= 0 {
		opts.FailLimit = DefaultFailLimit
	}
	if opts.Persist <= 0 {
		opts.Persist = DefaultPersist
	}
	if opts.DriverPeriods == nil {
		opts.DriverPeriods = DefaultDriverPeriods
	}
	if opts.NotifyMinInterval < 0 || opts.CacheTTL < 0 || opts.RescanPeriod < 0 {
		return nil, errors.New("hub: negative notify interval, cache TTL or rescan period")
	}
	h := &Hub{
		opts: opts, inv: inv, log: opts.Logger,
		keys: map[string]*keyState{}, pulses: map[string]pulse{}, counts: map[string]int{},
		wake: make(chan struct{}, 1), rescanAt: math.Inf(1), rescanEvery: opts.RescanPeriod,
	}
	if gateCfg.IdemMaxAge == nil {
		v := DefaultIdemMaxAge
		gateCfg.IdemMaxAge = &v
	}
	g, err := gate.New(withDeclaredConfirm(gateCfg, inv), inv.GateTools(), h)
	if err != nil {
		return nil, err
	}
	h.gate = g
	if err := h.validateGate(); err != nil {
		return nil, err
	}
	if err := h.buildKeys(); err != nil {
		return nil, err
	}
	now := opts.Clock.Now()
	h.started = now
	h.openLines(backend, now)
	h.sampleAll(now)
	return h, nil
}

// driverOf — медленный драйвер устройства ("w1", "dht11") или "".
func driverOf(d *inventory.Device) string {
	switch {
	case d.Source == "w1":
		return defaultSlowDriverW
	case d.Source == "iio" && d.Kind == "dht11":
		return "dht11"
	}
	return ""
}

// checkKeys проверяет ключи настройки declared.json: "<устройство>" или "<устройство>.<свойство>"
// из инвентаря и положительное значение.
func (h *Hub) checkKeys(name string, m map[string]float64) error {
	for k, v := range m {
		dev, prop, isProp := cutKey(k)
		if !isProp {
			dev = k
		}
		d := h.inv.Device(dev)
		if d == nil || (isProp && d.Property(prop) == nil) {
			return fmt.Errorf("%s: unknown device or property %q", name, k)
		}
		if !(v > 0) {
			return fmt.Errorf("%s[%q]: must be > 0", name, k)
		}
	}
	return nil
}

// perKey — настройка ключа: настройка свойства важнее настройки устройства.
func perKey(m map[string]float64, dev, key string) (float64, bool) {
	if v, ok := m[key]; ok {
		return v, true
	}
	v, ok := m[dev]
	return v, ok
}

// buildKeys создаёт состояние ключей, периоды опроса и порядок опроса.
func (h *Hub) buildKeys() error {
	if err := h.checkKeys("sample_periods", h.opts.Periods); err != nil {
		return err
	}
	if err := h.checkKeys("stuck_after_s", h.opts.StuckAfter); err != nil {
		return err
	}
	needed := map[string]bool{} // ключи, от которых зависят блокировки шлюза
	for _, il := range h.gate.Interlocks() {
		for _, k := range il.Needs {
			needed[k] = true
		}
	}
	for _, d := range h.inv.Devices {
		drv := driverOf(d)
		for _, p := range d.Properties {
			key := inventory.Key(d.ID, p.Name)
			ks := &keyState{key: key, dev: d, prop: p, output: p.Writable, class: classSysfs,
				faults: faults{quietUntil: math.Inf(-1)}}
			switch {
			case p.GPIO != nil:
				ks.class = classGPIO
			case drv == "w1":
				ks.class = classW1
			case drv != "":
				ks.class = classSlow
			}
			if p.Channel != nil && p.GPIO == nil {
				ks.reader = p.Channel.NewReader(h.opts.KeepOpen)
				ks.relocate = p.Channel.Renumbered()
			}
			h.keys[key] = ks
			if ks.output {
				h.outputs = append(h.outputs, ks)
				continue
			}
			ks.period, ks.source = h.opts.SamplePeriod, "default"
			if v, ok := h.opts.DriverPeriods[drv]; ok && drv != "" && v > ks.period {
				ks.period, ks.source = v, "driver"
			}
			if v, ok := perKey(h.opts.Periods, d.ID, key); ok {
				ks.period, ks.source = v, "declared"
			}
			if needed[key] && ks.period >= h.gate.MaxStateAge {
				switch {
				case ks.source == "driver" && h.opts.SamplePeriod < h.gate.MaxStateAge:
					// блокировке нужна свежесть: медленный драйвер опрашивается с общим периодом
					ks.period, ks.source = h.opts.SamplePeriod, "interlock"
				default:
					return fmt.Errorf("sample period %gs of %s must be shorter than gate max_state_age_s %gs: "+
						"otherwise interlocked commands always see stale state", ks.period, key, h.gate.MaxStateAge)
				}
			}
			ks.ttl = h.opts.CacheTTL
			if ks.ttl <= 0 {
				ks.ttl = 2 * ks.period
			}
			if v, ok := perKey(h.opts.StuckAfter, d.ID, key); ok && p.Type == inventory.TypeNumber {
				if v <= ks.period {
					return fmt.Errorf("stuck_after_s %gs of %s must be longer than its sample period %gs", v, key, ks.period)
				}
				ks.stuck = v
			}
			h.inputs = append(h.inputs, ks)
		}
	}
	for k := range h.opts.StuckAfter {
		applied := false
		for key, ks := range h.keys {
			applied = applied || ks.stuck > 0 && (key == k || ks.dev.ID == k)
		}
		if !applied {
			return fmt.Errorf("stuck_after_s: %s is not a sampled number or a device with one", k)
		}
	}
	sort.SliceStable(h.inputs, func(i, j int) bool { return h.inputs[i].class < h.inputs[j].class })
	return nil
}

// SamplePeriods — действующие периоды опроса по ключам и их источник, окна «застревания» (для журнала
// ready и hw://diag).
func (h *Hub) SamplePeriods() map[string]any {
	out := make(map[string]any, len(h.inputs))
	for _, ks := range h.inputs {
		v := map[string]any{"period_s": ks.period, "source": ks.source}
		if ks.stuck > 0 {
			v["stuck_after_s"] = ks.stuck
		}
		out[ks.key] = v
	}
	return out
}

// withDeclaredConfirm добавляет в политику requires_confirmation из declared.json: для объявленного
// выхода подтверждения требуют и запись свойства, и импульс.
func withDeclaredConfirm(cfg gate.Config, inv *inventory.Inventory) gate.Config {
	out := cfg
	out.Devices = map[string]gate.DeviceConfig{}
	for k, v := range cfg.Devices {
		out.Devices[k] = v
	}
	for _, d := range inv.Devices {
		for _, p := range d.Properties {
			if p.GPIO == nil || !p.Writable || !p.GPIO.RequiresConfirmation {
				continue
			}
			dc := out.Devices[d.ID]
			need := []string{inventory.WriteToolName(p.Name)}
			if d.Action(inventory.ActionPulse) != nil {
				need = append(need, inventory.ActionPulse)
			}
			rc := append([]string(nil), dc.RequiresConfirm...)
			for _, t := range need {
				if !slices.Contains(rc, t) {
					rc = append(rc, t)
				}
			}
			dc.RequiresConfirm = rc
			out.Devices[d.ID] = dc
		}
	}
	return out
}

// validateGate проверяет, что политика шлюза ссылается на существующие свойства:
// опечатка в ключе состояния иначе давала бы вечный stale_state, а в safe_state — незаписываемое значение.
func (h *Hub) validateGate() error {
	for _, il := range h.gate.Interlocks() {
		for _, key := range il.Needs {
			dev, prop, ok := cutKey(key)
			if !ok || h.inv.Device(dev) == nil || h.inv.Device(dev).Property(prop) == nil {
				return fmt.Errorf("gate: interlock %q needs unknown state key %q (want <device>.<property>)", il.Name, key)
			}
		}
	}
	invTools := h.inv.GateTools()
	for _, name := range h.gate.Devices() {
		gd := h.gate.Device(name)
		d := h.inv.Device(name)
		if d == nil {
			return fmt.Errorf("gate: device %q is not in the inventory", name)
		}
		for tool := range gd.Tools {
			if _, ok := invTools[name][tool]; !ok {
				// в hw-mcp инструменты строятся из инвентаря; "tools" в политике — только для стенда прототипа
				return fmt.Errorf("gate: %s.%s is not an inventory tool (custom tools are not supported by hw-mcp)", name, tool)
			}
		}
		for prop, v := range gd.SafeState {
			p := d.Property(prop)
			if p == nil || !p.Writable {
				return fmt.Errorf("gate: %s.safe_state: %q is not a writable property", name, prop)
			}
			if !argschema.Compile(inventory.ValueSchema(p)).Valid(v, true) {
				return fmt.Errorf("gate: %s.safe_state.%s: value %v does not match %s", name, prop, v, p.Type)
			}
		}
	}
	return nil
}

func cutKey(key string) (string, string, bool) {
	for i := len(key) - 1; i >= 0; i-- {
		if key[i] == '.' {
			return key[:i], key[i+1:], i > 0 && i < len(key)-1
		}
	}
	return "", "", false
}

// safeValue — безопасное значение выхода: из safe_state политики, иначе false (выключено).
func (h *Hub) safeValue(dev, prop string) bool {
	if gd := h.gate.Device(dev); gd != nil {
		if v, ok := gd.SafeState[prop].(bool); ok {
			return v
		}
	}
	return false
}

func (h *Hub) openLines(backend gpio.Backend, now float64) {
	for _, d := range h.inv.Devices {
		for _, p := range d.Properties {
			if p.GPIO == nil {
				continue
			}
			ks := h.keys[inventory.Key(d.ID, p.Name)]
			cfg := gpio.LineConfig{Chip: p.GPIO.Chip, Offset: p.GPIO.Line, Output: p.Writable,
				ActiveLow: p.GPIO.ActiveLow, Consumer: h.opts.Consumer}
			if p.Writable {
				cfg.Initial = h.safeValue(d.ID, p.Name)
			} else if h.opts.Edges {
				cfg.OnEdge = func(rising bool) { h.onEdge(ks, rising) }
				cfg.DebounceMs = p.GPIO.DebounceMs
			}
			line, err := backend.Open(cfg)
			if err != nil && cfg.OnEdge != nil {
				// у линии нет прерывания (расширитель GPIO без вывода INT: ENXIO в uAPI v2, ENODEV в v1) —
				// фронтов не будет, вход только опрашивается
				edgeErr := err
				cfg.OnEdge, cfg.DebounceMs = nil, 0
				if line, err = backend.Open(cfg); err == nil {
					h.lineError(d.ID, p.Name, cfg, edgeErr, true)
				}
			}
			if err != nil {
				h.lineError(d.ID, p.Name, cfg, err, false)
				h.stMu.Lock()
				ks.r, ks.has, ks.fails = Reading{TS: now, Err: err.Error()}, true, h.opts.FailLimit
				ks.faults.outages, ks.faults.downSince = 1, now
				h.stMu.Unlock()
				h.outages.Add(1)
				continue
			}
			ks.line = line
			if p.Writable {
				h.stMu.Lock()
				ks.r, ks.has, ks.hasBase, ks.reported, ks.reportedAt = Reading{Value: cfg.Initial, TS: now}, true, true, cfg.Initial, now
				h.stMu.Unlock()
				h.opts.Trace.Emit("output_init", map[string]any{"device": d.ID, "property": p.Name,
					"line": cfg.String(), "value": cfg.Initial, "backend": backend.Name()})
			}
		}
	}
	h.inputs = slices.DeleteFunc(h.inputs, (*keyState).fixed) // не открывшиеся линии опрашивать нечем
}

// lineError — запрос линии не удался (журнал line_error); polled — линия открыта повторно без фронтов и
// только опрашивается, иначе её значение — эта ошибка.
func (h *Hub) lineError(dev, prop string, cfg gpio.LineConfig, err error, polled bool) {
	rec := map[string]any{"device": dev, "property": prop, "line": cfg.String(), "error": err.Error()}
	msg := "gpio line unavailable"
	if polled {
		rec["fallback"], msg = "poll", "gpio edges unavailable, line is polled"
	}
	h.log.Warn(msg, "device", dev, "property", prop, "err", err)
	h.opts.Trace.Emit("line_error", rec)
}

// Inventory — модель устройств.
func (h *Hub) Inventory() *inventory.Inventory { return h.inv }

// Gate — шлюз (только для чтения политики; проверки идут через Actuate).
func (h *Hub) Gate() *gate.Gate { return h.gate }

// Now — текущее время часов hw-mcp.
func (h *Hub) Now() float64 { return h.opts.Clock.Now() }

// OnChange подписывает fn на изменения состояния. fn вызывается синхронно из сэмплера, таймеров и
// обработчика фронтов и не должна блокироваться (MCP-сервер ставит уведомление в очередь).
func (h *Hub) OnChange(fn func(Update)) {
	h.lmu.Lock()
	h.listeners = append(h.listeners, fn)
	h.lmu.Unlock()
}

func (h *Hub) notify(changed map[string][]Change, now float64) {
	if len(changed) == 0 {
		return
	}
	h.lmu.Lock()
	ls := slices.Clone(h.listeners)
	h.lmu.Unlock()
	for _, dev := range sortedKeys(changed) {
		ch := changed[dev]
		sort.Slice(ch, func(i, j int) bool { return ch[i].Property < ch[j].Property })
		u := Update{Device: dev, Changes: ch, TS: now}
		h.notifications.Add(1)
		h.opts.Trace.Emit("notify", map[string]any{"uri": "hw://state/" + dev, "device": dev,
			"changed": u.Props(), "ts_clock": now})
		for _, fn := range ls {
			fn(u)
		}
	}
}

// ----------------------------------------------------------------------------------------------
// Состояние

// Get — StateReader для шлюза: значение по ключу "<устройство>.<свойство>".
func (h *Hub) Get(key string) (any, float64, bool) {
	ks := h.keys[key]
	if ks == nil {
		return nil, 0, false
	}
	now := h.Now()
	h.stMu.RLock()
	r := ks.view(now)
	h.stMu.RUnlock()
	if r.Value == nil {
		return nil, 0, false
	}
	return r.Value, r.TS, true
}

// commitLocked применяет отсчёт к ключу и решает, сообщать ли об изменении (под stMu).
//
// Ошибка чтения: пока сбоев подряд меньше FailLimit, остаётся прежнее значение — последнее хорошее (его
// возраст растёт, и шлюз со временем увидит stale_state) или stuck; после FailLimit значение становится
// null с причиной, и это сообщается. Число, не менявшееся дольше окна stuck_after_s, становится null сразу
// (окно уже выдержано); одиночные сбои окно не прерывают и «застрявшее» значение не возвращают, перерыв
// (FailLimit сбоев подряд) — прерывает. Хорошее значение уточняет наблюдённый шаг квантования q.
func (h *Hub) commitLocked(ks *keyState, r Reading, now float64) bool {
	ks.ver++
	if r.Err != "" {
		ks.fails++
		ks.faults.reads++
		h.sampleErrors.Add(1)
		if ks.has && (ks.r.Err == "" || ks.r.Err == ReasonStuck) && ks.fails < h.opts.FailLimit {
			h.glitches.Add(1)
			return false
		}
		ks.hasPrev = false // перерыв: шаг квантования и «застревание» — заново по отсчётам после него
		return h.unknownLocked(ks, r, now)
	}
	ks.fails = 0
	if ks.stuckLocked(r, now) {
		return h.unknownLocked(ks, Reading{TS: r.TS, Err: ReasonStuck}, now)
	}
	if ks.has && ks.r.Err != "" {
		h.recoveredLocked(ks, now)
	}
	ks.r, ks.has = r, true
	report := h.reportLocked(ks, r.Value, now)
	// шаг q уточняется после решения: это шаг уже наблюдённых отсчётов, иначе первое же изменение
	// считалось бы «одним шагом» самого себя и откладывалось на Persist отсчётов
	if f, ok := r.Value.(float64); ok {
		if !ks.hasPrev || f != ks.prev {
			ks.since = now
		}
		if ks.hasPrev {
			if d := math.Abs(f - ks.prev); d > 1e-9 && (ks.q == 0 || d < ks.q) {
				ks.q = d
			}
		}
		ks.prev, ks.hasPrev = f, true
	}
	return report
}

// reportLocked — политика уведомлений. Сравнение — с последним сообщённым значением (медленный дрейф
// накапливается и в итоге даёт уведомление), первый отсчёт задаёт базу без уведомления.
// Числа: изменение должно строго превышать порог свойства; изменение не больше одного наблюдённого шага
// квантования q сообщается, только если держится Persist отсчётов подряд (дрожание младшего разряда
// DHT11 не даёт уведомлений); не чаще NotifyMinInterval. Логические значения и переходы в null и из
// null сообщаются сразу.
func (h *Hub) reportLocked(ks *keyState, v any, now float64) bool {
	if !ks.hasBase {
		ks.hasBase, ks.reported, ks.reportedAt = true, v, now
		return false
	}
	old := ks.reported
	switch {
	case (old == nil) != (v == nil):
	case old == nil:
		return false
	default:
		of, okO := old.(float64)
		cf, okC := v.(float64)
		if !okO || !okC {
			if old == v {
				return false
			}
			break
		}
		d := math.Abs(cf - of)
		thr := ks.prop.Threshold
		if d == 0 || (thr > 0 && !(d > thr*(1+1e-9))) {
			ks.pending = 0
			return false
		}
		if ks.q > 0 && d <= ks.q*(1+1e-9) && h.opts.Persist > 1 {
			ks.pending++
			if ks.pending < h.opts.Persist {
				h.quietQuant.Add(1)
				return false
			}
		}
		if h.opts.NotifyMinInterval > 0 && now-ks.reportedAt < h.opts.NotifyMinInterval {
			h.quietInterval.Add(1)
			return false
		}
	}
	ks.pending = 0
	ks.reported, ks.reportedAt = v, now
	return true
}

// readKey читает значение свойства из источника (без блокировок hub; не для fixed).
func (h *Hub) readKey(ks *keyState, now float64) Reading {
	if ks.prop.GPIO != nil {
		v, err := ks.line.Get()
		if err != nil {
			return Reading{TS: now, Err: err.Error()}
		}
		return Reading{Value: v, TS: now}
	}
	if ks.reader == nil {
		return Reading{TS: now, Err: "no source"}
	}
	v, how, ok := ks.reader.Read()
	if !ok {
		return Reading{TS: now, Err: how}
	}
	return Reading{Value: ks.boxed(v), TS: now}
}

// sampleAll опрашивает все входы (первый опрос при старте) и назначает следующие сроки.
func (h *Hub) sampleAll(now float64) {
	h.smpMu.Lock()
	defer h.smpMu.Unlock()
	due, vers := h.dueBuf[:0], h.verBuf[:0]
	h.stMu.Lock()
	for _, ks := range h.inputs {
		due, vers = append(due, ks), append(vers, ks.ver)
		ks.next = now + ks.period
	}
	h.stMu.Unlock()
	h.dueBuf, h.verBuf = due, vers
	h.poll(due, vers)
}

// poll читает ключи и записывает результаты: перед чтением медленного датчика накопленное
// записывается и сообщается, чтобы DS18B20 (750 мс) не задерживал уведомления быстрых датчиков.
func (h *Hub) poll(due []*keyState, vers []uint64) {
	batch := h.batch[:0]
	for i, ks := range due {
		if ks.class >= classSlow && len(batch) > 0 {
			h.commitBatch(batch)
			batch = batch[:0]
		}
		batch = append(batch, polled{ks: ks, ver: vers[i], r: h.readKey(ks, h.Now())})
	}
	h.commitBatch(batch)
	clear(batch) // не держать ссылки на значения до следующего опроса
	h.batch = batch[:0]
}

func (h *Hub) commitBatch(items []polled) {
	if len(items) == 0 {
		return
	}
	var changed map[string][]Change
	now := 0.0
	h.stMu.Lock()
	for _, it := range items {
		h.samples.Add(1)
		if it.r.TS > now {
			now = it.r.TS
		}
		if it.ks.ver != it.ver { // за время чтения пришло более новое значение (фронт, read_property)
			h.superseded.Add(1)
			continue
		}
		if h.commitLocked(it.ks, it.r, it.r.TS) {
			if changed == nil {
				changed = map[string][]Change{}
			}
			changed[it.ks.dev.ID] = append(changed[it.ks.dev.ID], Change{it.ks.prop.Name, it.ks.r.Value, it.r.TS})
		}
	}
	h.stMu.Unlock()
	h.notify(changed, now)
}

// onEdge — фронт входа GPIO: «вверх» — линия стала активной (с учётом active_low).
func (h *Hub) onEdge(ks *keyState, rising bool) {
	now := h.Now()
	h.edges.Add(1)
	h.stMu.Lock()
	ch := h.commitLocked(ks, Reading{Value: rising, TS: now}, now)
	h.stMu.Unlock()
	if ch {
		h.notify(map[string][]Change{ks.dev.ID: {{ks.prop.Name, rising, now}}}, now)
	}
}

// Read — значение свойства для read_property: из кэша, если оно не старше срока кэша свойства, иначе
// перечитывается из источника.
func (h *Hub) Read(device, property string) (Reading, error) {
	d := h.inv.Device(device)
	if d == nil {
		return Reading{}, ErrUnknownDevice
	}
	if d.Property(property) == nil {
		return Reading{}, ErrUnknownProperty
	}
	ks := h.keys[inventory.Key(device, property)]
	now := h.Now()
	h.stMu.RLock()
	r, has, ver := ks.view(now), ks.has, ks.ver
	h.stMu.RUnlock()
	if ks.fixed() || (has && r.Err == "" && now-r.TS <= ks.ttl) {
		return r, nil
	}
	fresh := h.readKey(ks, now)
	h.stMu.Lock()
	ch := false
	if ks.ver == ver {
		ch = h.commitLocked(ks, fresh, now)
	}
	out := ks.view(now)
	h.stMu.Unlock()
	if ch {
		h.notify(map[string][]Change{device: {{property, out.Value, now}}}, now)
	}
	return out, nil
}

// Ошибки чтения.
var (
	ErrUnknownDevice   = errors.New("unknown_device")
	ErrUnknownProperty = errors.New("unknown_property")
)

// DeviceState — снимок состояния устройства (ресурс hw://state/<устройство>).
func (h *Hub) DeviceState(device string) (map[string]any, bool) {
	d := h.inv.Device(device)
	if d == nil {
		return nil, false
	}
	now := h.Now()
	props := map[string]any{}
	h.stMu.RLock()
	for _, p := range d.Properties {
		ks := h.keys[inventory.Key(device, p.Name)]
		props[p.Name] = readingView(p, ks.view(now), ks.has, now)
	}
	h.stMu.RUnlock()
	return map[string]any{"device": device, "now": now, "properties": props}, true
}

func readingView(p *inventory.Property, r Reading, ok bool, now float64) map[string]any {
	v := map[string]any{"value": r.Value}
	if p.Unit != "" {
		v["unit"] = p.Unit
	}
	if ok {
		v["ts"] = r.TS
		v["age_s"] = round3(now - r.TS)
	}
	if r.Err != "" {
		v["error"] = r.Err
	}
	return v
}

func round3(x float64) float64 { return math.Round(x*1000) / 1000 }

// ----------------------------------------------------------------------------------------------
// Команды

// ReasonShutdown — команда пришла после начала завершения работы (приводы уже в безопасном состоянии).
const ReasonShutdown = "shutting_down"

// Actuate проверяет команду шлюзом и при допуске исполняет её (атомарно относительно других команд).
func (h *Hub) Actuate(req ActRequest) ActResult {
	h.actMu.Lock()
	res, changed, now := h.actuateLocked(req)
	h.actMu.Unlock()
	if res.Executed {
		h.kickTimers() // мог измениться срок аренды или импульса
	}
	h.notify(changed, now)
	return res
}

func (h *Hub) actuateLocked(req ActRequest) (ActResult, map[string][]Change, float64) {
	now := h.Now()
	res := ActResult{CmdID: req.CmdID}
	generated := res.CmdID == ""
	if generated {
		h.cmdSeq++
		res.CmdID = "hw-" + strconv.FormatInt(h.cmdSeq, 10)
	}
	if h.closed.Load() {
		res.Decision = gate.Decision{Verdict: gate.Reject, Reason: ReasonShutdown, Detail: "hw-mcp is shutting down"}
		return res, nil, now
	}
	if req.Tool == inventory.ActionRenewLease {
		return h.renewLocked(req, res, now), nil, now
	}
	issued := now
	if req.IssuedAt != nil {
		issued = *req.IssuedAt
	}
	cmd := gate.Command{CmdID: res.CmdID, Device: req.Device, Tool: req.Tool, Args: req.Args,
		IssuedAt: issued, TTL: req.TTL, Confirmed: req.Confirmed, NoIdempotency: generated}
	t0 := time.Now()
	if req.DryRun {
		res.Decision = h.gate.CheckDry(cmd, now)
	} else {
		res.Decision = h.gate.Check(cmd, now)
	}
	checkNs := time.Since(t0).Nanoseconds()

	var changed map[string][]Change
	if res.Decision.Accepted() && !req.DryRun {
		changed, res.Err = h.applyLocked(req, &res, now)
		res.Executed = res.Err == nil
	}
	if !req.DryRun {
		h.counts[string(res.Decision.Verdict)]++
	}
	if exp, ok := h.gate.LeaseExpiry(req.Device); ok && res.Executed {
		res.LeaseUntil = &exp
	}
	h.traceDecision(req, cmd, res, checkNs, now)
	return res, changed, now
}

func (h *Hub) applyLocked(req ActRequest, res *ActResult, now float64) (map[string][]Change, error) {
	d := h.inv.Device(req.Device)
	args, _ := res.Decision.Args.(map[string]any)
	var prop string
	var value bool
	switch req.Tool {
	case inventory.ActionPulse:
		a := d.Action(inventory.ActionPulse)
		prop, value = a.Property, true
	default:
		prop = req.Tool[len("set_"):]
		value, _ = args["value"].(bool)
	}
	changed, err := h.writeLocked(d.ID, prop, value, now)
	if err != nil {
		return nil, err
	}
	res.Applied = map[string]any{prop: value}
	delete(h.pulses, d.ID) // явная команда отменяет незавершённый импульс
	if req.Tool == inventory.ActionPulse {
		secs, _ := toFloat(args["seconds"])
		until := now + secs
		h.pulses[d.ID] = pulse{prop: prop, until: until}
		res.PulseUntil = &until
	}
	if !changed { // повторное «выключить» при выключенном: состояние то же, уведомлять не о чем
		return nil, nil
	}
	return map[string][]Change{d.ID: {{prop, value, now}}}, nil
}

// writeLocked пишет значение в линию и обновляет состояние (под actMu); changed — значение изменилось.
func (h *Hub) writeLocked(dev, prop string, value bool, now float64) (bool, error) {
	ks := h.keys[inventory.Key(dev, prop)]
	if ks == nil || ks.line == nil {
		return false, fmt.Errorf("gpio line for %s is unavailable", inventory.Key(dev, prop))
	}
	if err := ks.line.Set(value); err != nil {
		return false, err
	}
	h.stMu.Lock()
	changed := !ks.has || ks.r.Err != "" || ks.r.Value != value
	ks.ver++
	ks.r, ks.has = Reading{Value: value, TS: now}, true
	ks.hasBase, ks.reported, ks.reportedAt = true, value, now
	h.stMu.Unlock()
	return changed, nil
}

func (h *Hub) renewLocked(req ActRequest, res ActResult, now float64) ActResult {
	d := h.inv.Device(req.Device)
	switch {
	case d == nil:
		res.Decision = gate.Decision{Verdict: gate.Reject, Reason: gate.ReasonUnknownDevice, Detail: req.Device}
	case d.Action(inventory.ActionRenewLease) == nil:
		res.Decision = gate.Decision{Verdict: gate.Reject, Reason: gate.ReasonUnknownTool,
			Detail: req.Device + "." + req.Tool}
	default:
		ok := h.gate.Renew(req.Device, now)
		res.Renewed = &ok
		if ok {
			res.Decision = gate.Decision{Verdict: gate.Allow, Args: map[string]any{}}
			res.Executed = true
			if exp, has := h.gate.LeaseExpiry(req.Device); has {
				res.LeaseUntil = &exp
			}
		} else {
			res.Decision = gate.Decision{Verdict: gate.Reject, Reason: ReasonNoActiveLease,
				Detail: "у устройства нет действующей аренды"}
		}
	}
	h.opts.Trace.Emit("lease_renew", map[string]any{"cmd_id": res.CmdID, "device": req.Device,
		"origin": req.Origin, "renewed": res.Renewed != nil && *res.Renewed, "verdict": string(res.Decision.Verdict),
		"reason": nilIfEmpty(res.Decision.Reason), "lease_until": res.LeaseUntil, "ts_clock": now})
	return res
}

// ReasonNoActiveLease — renew_lease без действующей аренды.
const ReasonNoActiveLease = "no_active_lease"

func (h *Hub) traceDecision(req ActRequest, cmd gate.Command, res ActResult, checkNs int64, now float64) {
	rec := map[string]any{
		"cmd_id": res.CmdID, "device": req.Device, "tool": req.Tool, "origin": req.Origin,
		"args": req.Args, "issued_at": cmd.IssuedAt, "ttl_s": req.TTL, "confirmed": req.Confirmed,
		"dry_run": req.DryRun, "verdict": string(res.Decision.Verdict), "reason": nilIfEmpty(res.Decision.Reason),
		"detail": res.Decision.Detail, "final_args": res.Decision.Args, "executed": res.Executed,
		"check_ns": checkNs, "ts_clock": now,
	}
	if res.Decision.State != nil {
		rec["state"] = res.Decision.State
	}
	if res.Applied != nil {
		rec["applied"] = res.Applied
	}
	if res.Err != nil {
		rec["error"] = res.Err.Error()
	}
	if res.PulseUntil != nil {
		rec["pulse_until"] = *res.PulseUntil
	}
	if res.LeaseUntil != nil {
		rec["lease_until"] = *res.LeaseUntil
	}
	h.opts.Trace.Emit("gate", rec)
}

func nilIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func toFloat(v any) (float64, bool) {
	switch n := v.(type) {
	case float64:
		return n, true
	case int:
		return float64(n), true
	case interface{ Float64() (float64, error) }:
		f, err := n.Float64()
		return f, err == nil
	}
	return 0, false
}

// ----------------------------------------------------------------------------------------------
// Время: опрос, аренда, импульсы

// Step выполняет всё, что наступило к моменту now: сторожевой таймер аренды, конец импульсов, опрос.
func (h *Hub) Step(now float64) {
	h.StepTimers(now)
	h.StepSample(now)
}

// StepTimers — сторожевой таймер аренды и конец импульсов (безопасность — раньше опроса).
func (h *Hub) StepTimers(now float64) {
	h.actMu.Lock()
	changed := h.timersLocked(now)
	h.actMu.Unlock()
	h.notify(changed, now)
}

// dueSlack — срок опроса, наступающий в пределах этого запаса, выполняется в текущем пробуждении:
// совпадающие сроки свойств с разными периодами (0,1 и 0,3 с) расходятся на ошибку округления float64,
// и без запаса сэмплер просыпался бы дважды подряд.
const dueSlack = 1e-3

// StepSample опрашивает свойства, срок опроса которых наступил. Следующий срок — предыдущий плюс
// период (фаза сохраняется, фактический период равен заданному); пропущенные сроки (долгое чтение,
// пауза процесса) не догоняются пачкой опросов. Пока устройство IIO или hwmon пропало, ищется его новый
// путь (rescan): через RescanPeriod после пропажи, дальше с удвоением периода до rescanMaxPeriod.
func (h *Hub) StepSample(now float64) {
	h.smpMu.Lock()
	defer h.smpMu.Unlock()
	if h.closed.Load() {
		return
	}
	due, vers := h.dueBuf[:0], h.verBuf[:0]
	lost := false
	h.stMu.Lock()
	for _, ks := range h.inputs {
		if now >= ks.next-dueSlack {
			due, vers = append(due, ks), append(vers, ks.ver)
			ks.next = nextAfter(ks.next, ks.period, now)
		}
		lost = lost || ks.relocate && ks.r.Err == sysfs.ReasonAbsent
	}
	rescan := h.rescanDueLocked(lost, now)
	h.stMu.Unlock()
	h.dueBuf, h.verBuf = due, vers
	if len(due) > 0 {
		h.poll(due, vers)
	}
	if rescan {
		h.rescan(now)
	}
}

func nextAfter(next, period, now float64) float64 {
	next += period
	if next <= now {
		next += (math.Floor((now-next)/period) + 1) * period
	}
	return next
}

// NextSampleDue — ближайший срок опроса или повторной инвентаризации (+Inf — делать нечего).
func (h *Hub) NextSampleDue() float64 {
	h.stMu.RLock()
	next := h.rescanAt
	for _, ks := range h.inputs {
		if ks.next < next {
			next = ks.next
		}
	}
	h.stMu.RUnlock()
	return next
}

// NextTimerDue — ближайший срок аренды или конца импульса (ok=false — таймеров нет).
func (h *Hub) NextTimerDue() (float64, bool) {
	h.actMu.Lock()
	defer h.actMu.Unlock()
	next, ok := h.gate.NextExpiry()
	for _, p := range h.pulses {
		if !ok || p.until < next {
			next, ok = p.until, true
		}
	}
	return next, ok
}

func (h *Hub) timersLocked(now float64) map[string][]Change {
	if h.closed.Load() {
		return nil
	}
	changed := map[string][]Change{}
	for _, ev := range h.gate.Tick(now) {
		delete(h.pulses, ev.Device)
		applied := map[string]any{}
		var errs []string
		for _, prop := range sortedKeys(ev.SafeState) {
			v, _ := ev.SafeState[prop].(bool)
			ch, err := h.writeLocked(ev.Device, prop, v, now)
			if err != nil {
				errs = append(errs, err.Error())
				continue
			}
			applied[prop] = v
			if ch {
				changed[ev.Device] = append(changed[ev.Device], Change{prop, v, now})
			}
		}
		rec := map[string]any{"device": ev.Device, "safe_state": ev.SafeState, "applied": applied,
			"ok": len(errs) == 0, "ts_clock": now}
		if len(errs) > 0 {
			rec["error"] = errs
		}
		h.opts.Trace.Emit("lease_expired", rec)
	}
	for _, dev := range sortedKeys(h.pulses) {
		p := h.pulses[dev]
		if now < p.until {
			continue
		}
		delete(h.pulses, dev)
		v := h.safeValue(dev, p.prop)
		ch, err := h.writeLocked(dev, p.prop, v, now)
		rec := map[string]any{"device": dev, "property": p.prop, "value": v, "ok": err == nil,
			"scheduled": p.until, "ts_clock": now}
		if err != nil {
			rec["error"] = err.Error()
		} else if ch {
			changed[dev] = append(changed[dev], Change{p.prop, v, now})
		}
		h.opts.Trace.Emit("pulse_end", rec)
	}
	return changed
}

// NextDue — ближайший момент, когда Step должен что-то сделать (+Inf — ничего не запланировано).
func (h *Hub) NextDue() float64 {
	next := h.NextSampleDue()
	if t, ok := h.NextTimerDue(); ok && t < next {
		next = t
	}
	return next
}

// AdvanceTo переводит виртуальные часы до to, выполняя по порядку все наступившие сроки.
func (h *Hub) AdvanceTo(vc *clock.Virtual, to float64) error {
	h.advMu.Lock()
	defer h.advMu.Unlock()
	if to < vc.Now() {
		return fmt.Errorf("clock cannot go back: %g < %g", to, vc.Now())
	}
	for i := 0; ; i++ {
		if i > 10_000_000 {
			return errors.New("advance: too many steps (sample period too small?)")
		}
		next := h.NextDue()
		if next > to {
			break
		}
		if next > vc.Now() {
			if err := vc.Set(next); err != nil {
				return err
			}
		}
		h.Step(vc.Now())
	}
	if err := vc.Set(to); err != nil {
		return err
	}
	h.Step(to)
	return nil
}

// Close переводит приводы с объявленным safe_state в безопасное состояние и освобождает линии.
// Повторный вызов ничего не делает; команды после Close отклоняются (shutting_down), таймеры и
// опрос не трогают линии. Не зависит от транспорта MCP.
func (h *Hub) Close() {
	h.actMu.Lock()
	if h.closed.Load() {
		h.actMu.Unlock()
		return
	}
	now := h.Now()
	for _, name := range h.gate.Devices() {
		gd := h.gate.Device(name)
		for _, prop := range sortedKeys(gd.SafeState) {
			v, _ := gd.SafeState[prop].(bool)
			_, err := h.writeLocked(name, prop, v, now)
			rec := map[string]any{"device": name, "property": prop, "value": v, "ok": err == nil, "ts_clock": now}
			if err != nil {
				rec["error"] = err.Error()
			}
			h.opts.Trace.Emit("shutdown_safe_state", rec)
		}
	}
	h.closed.Store(true)
	for _, ks := range h.outputs {
		if ks.line != nil {
			_ = ks.line.Close()
		}
	}
	h.actMu.Unlock()
	h.kickTimers()
	// входы и дескрипторы sysfs — после текущего опроса (безопасное состояние уже выставлено)
	h.smpMu.Lock()
	for _, ks := range h.inputs {
		if ks.line != nil {
			_ = ks.line.Close()
		}
		if ks.reader != nil {
			ks.reader.Close()
		}
	}
	h.smpMu.Unlock()
}

// Stats — счётчики для журнала run_end и ресурса hw://diag.
func (h *Hub) Stats() map[string]any {
	h.actMu.Lock()
	counts := map[string]any{}
	for k, v := range h.counts {
		counts[k] = v
	}
	var leases, pulses []any
	for _, name := range h.gate.Devices() {
		if exp, ok := h.gate.LeaseExpiry(name); ok {
			leases = append(leases, map[string]any{"device": name, "until": exp})
		}
	}
	for _, dev := range sortedKeys(h.pulses) {
		pulses = append(pulses, map[string]any{"device": dev, "until": h.pulses[dev].until})
	}
	safe := h.gate.SafeCount
	idem := h.gate.IdemSize()
	h.actMu.Unlock()
	return map[string]any{
		"devices": len(h.inv.Devices), "sample_period_s": h.opts.SamplePeriod,
		"samples": h.samples.Load(), "sample_errors": h.sampleErrors.Load(), "sample_glitches": h.glitches.Load(),
		"superseded": h.superseded.Load(), "notifications": h.notifications.Load(), "edges": h.edges.Load(),
		"quiet_quantization": h.quietQuant.Load(), "quiet_interval": h.quietInterval.Load(),
		"timer_wakeups": h.timerWakeups.Load(), "sample_wakeups": h.sampleWakeups.Load(),
		"outages": h.outages.Load(), "recoveries": h.recoveries.Load(), "quiet_faults": h.quietFaults.Load(),
		"rescans": h.rescans.Load(), "rebound": h.rebound.Load(),
		"decisions": counts, "safe_events": safe, "idem_keys": idem,
		"active_leases": nonNilList(leases), "active_pulses": nonNilList(pulses),
		"uptime_s": round3(h.Now() - h.started),
	}
}

func nonNilList(l []any) []any {
	if l == nil {
		return []any{}
	}
	return l
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
