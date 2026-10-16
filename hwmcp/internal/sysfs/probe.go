// Package sysfs — детерминированная инвентаризация оборудования Linux по sysfs без опроса шин.
//
// Порт research/new_topic/GAP2_artifacts/sysfs_probe.py: те же источники, те же правила разбора
// и единиц, тот же формат результата (паритет проверяется тестами на фикстуре и эталоне,
// полученном из прототипа, см. tools/gen_golden.py):
//
//   - IIO   sys/bus/iio/devices/iio:deviceN  -> каналы с единицами из ABI ядра;
//     значение = *_input, иначе (raw + offset) × scale (sysfs-bus-iio);
//   - hwmon sys/class/hwmon/hwmonN           -> tempN/inN/currN/powerN/humidityN _input;
//   - 1-Wire sys/bus/w1/devices/<fam>-<id>   -> temperature (1/1000 °C, w1_therm);
//   - V4L2  sys/class/video4linux/videoN     -> имя и узлы (тип узла — только ioctl);
//   - USB   sys/bus/usb/devices/*            -> VID:PID, product (корневые хабы 1d6b скрыты);
//   - I2C   sys/bus/i2c/devices/N-00AA       -> клиенты без драйвера IIO (только адрес и имя);
//   - GPIO  sys/bus/gpio/devices/gpiochipN   -> список чипов (линии — из declared.json);
//   - HAT   proc/device-tree/hat/*           -> vendor/product из EEPROM HAT.
//
// Шина I²C не сканируется: man i2cdetect предупреждает, что опрос может сбить устройство.
// Уровни семантической наблюдаемости (Octopus, arXiv:2605.09055): A — ОС знает тип, каналы и
// единицы; B — только идентичность; C — только транспорт (нужен профиль из declared.json).
package sysfs

import (
	"fmt"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/local/hwmcp/internal/declared"
)

// Channel — канал устройства (будущее свойство модели WoT).
type Channel struct {
	Name     string   // имя свойства: temperature, humidity, voltage0, ...
	Quantity string   // тип канала ABI: temp, humidityrelative, in, ...; для GPIO — из declared.json
	Unit     string   // единица SenML (Cel, %RH, Pa, ...), "/" — логическое значение
	ABIUnit  string   // единица ABI ядра; пусто у GPIO
	Value    *float64 // значение в единицах SenML на момент инвентаризации; nil — неизвестно
	How      string   // input | (raw+offset)*scale | raw-without-scale | absent | temperature | gpiod
	Path     string   // путь относительно корня (для GPIO — dev/<chip>#<line>)
	Writable bool

	src source // как перечитать значение (не сериализуется)
}

// source описывает, откуда перечитывать значение канала.
type source struct {
	kind       string // iio | file | gpio
	dir        string // IIO: каталог устройства
	base       string // IIO: in_<type><idx><mod>
	typ        string // IIO: тип канала
	file       string // file: абсолютный путь
	k          float64
	renumbered bool // номер узла выдаётся заново при повторном появлении (Renumbered)
}

// Device — запись инвентаря. Набор и порядок полей в JSON зависит от источника и повторяет
// словари прототипа (см. MarshalJSON).
type Device struct {
	ID            string
	Source        string // iio | hwmon | w1 | v4l2 | usb | i2c | gpio | hat | declared
	Name          *string
	Vendor        *string
	Bus           *string
	Observability string // A | B | C
	Location      *string
	Description   *string
	Channels      []Channel
	Notes         []string
	Nodes         []V4L2Node
	HAT           *HATInfo

	// Признаки присутствия необязательных ключей в JSON (паритет с прототипом).
	hasVendor, hasNotes, hasNodes, hasLocation, hasDescription bool

	// GPIO — назначение линии для устройств из declared.json (nil для обнаруженных).
	GPIO *declared.GPIO
}

// V4L2Node — узел /dev/videoN одного устройства захвата.
type V4L2Node struct {
	Dev   string  `json:"dev"`
	Index *string `json:"index"`
}

// HATInfo — строки EEPROM HAT из /proc/device-tree/hat.
type HATInfo struct {
	Vendor, Product, ProductID, ProductVer, UUID *string
}

// iioAttrRe — имя атрибута канала IIO; ленивый квантификатор, как в прототипе.
var iioAttrRe = regexp.MustCompile(`^in_([a-z]+?)(\d*)((?:_[xyz])?)_(input|raw)$`)

var hwmonAttrRe = regexp.MustCompile(`^(temp|in|curr|power|humidity)(\d+)_input$`)

// IIOChannelValue — значение канала IIO в единицах ABI: *_input, иначе (raw + offset) × scale.
// scale и offset бывают на канал (in_voltage0_scale) или на тип (in_voltage_scale).
// Возвращает значение (nil — неизвестно) и способ получения.
func IIOChannelValue(dir, base, typ string) (*float64, string) {
	if v, ok := readFloat(filepath.Join(dir, base+"_input")); ok {
		return &v, "input"
	}
	raw, ok := readFloat(filepath.Join(dir, base+"_raw"))
	if !ok {
		return nil, "absent"
	}
	scale, hasScale := readFloat(filepath.Join(dir, base+"_scale"))
	if !hasScale {
		scale, hasScale = readFloat(filepath.Join(dir, "in_"+typ+"_scale"))
	}
	offset, hasOffset := readFloat(filepath.Join(dir, base+"_offset"))
	if !hasOffset {
		offset, _ = readFloat(filepath.Join(dir, "in_"+typ+"_offset")) // нет файла -> 0
	}
	if !hasScale {
		return nil, "raw-without-scale"
	}
	v := (raw + offset) * scale
	return &v, "(raw+offset)*scale"
}

// probeIIO — устройства IIO; values=false — без чтения значений каналов (Value nil, How — найденный атрибут:
// input или raw).
func probeIIO(root string, values bool) []*Device {
	var out []*Device
	dirs := listDir(filepath.Join(root, "sys/bus/iio/devices"), func(n string) bool {
		return strings.HasPrefix(n, "iio:device")
	})
	for _, d := range dirs {
		name, ok := readAttr(filepath.Join(d, "name"))
		if !ok || name == "" {
			name = "unknown"
		}
		parent := realParent(d)
		bus, isI2C := i2cLabel(parent)
		if !isI2C {
			bus = parent
		}
		var chans []Channel
		seen := map[string]bool{}
		for _, f := range listNames(d) {
			m := iioAttrRe.FindStringSubmatch(f)
			if m == nil {
				continue
			}
			typ, idx, mod := m[1], m[2], m[3]
			base := "in_" + typ + idx + mod
			u, known := iioUnits[typ]
			if seen[base] || !known {
				continue
			}
			seen[base] = true
			v, how := (*float64)(nil), m[4] // имена отсортированы: при обоих атрибутах первым найден _input
			if values {
				v, how = IIOChannelValue(d, base, typ)
			}
			suffix := "_raw"
			if how == "input" {
				suffix = "_input"
			}
			chans = append(chans, Channel{
				Name: iioNames[typ] + idx + mod, Quantity: typ, Unit: u.SenML, ABIUnit: u.ABI,
				Value: scaled(v, u.K), How: how,
				Path: relPath(root, filepath.Join(d, base+suffix)),
				src:  source{kind: "iio", dir: d, base: base, typ: typ, k: u.K, renumbered: true},
			})
		}
		out = append(out, &Device{
			ID: "iio:" + name + "@" + bus, Source: "iio", Name: strPtr(name), Bus: strPtr(bus),
			Observability: levelByChannels(chans), Channels: chans,
		})
	}
	return out
}

// probeHwmon — устройства hwmon; values=false — без чтения значений каналов (Value nil).
func probeHwmon(root string, values bool) []*Device {
	var out []*Device
	dirs := listDir(filepath.Join(root, "sys/class/hwmon"), func(n string) bool {
		return strings.HasPrefix(n, "hwmon")
	})
	for _, d := range dirs {
		name, ok := readAttr(filepath.Join(d, "name"))
		if !ok || name == "" {
			name = "unknown"
		}
		var chans []Channel
		for _, f := range listNames(d) {
			m := hwmonAttrRe.FindStringSubmatch(f)
			if m == nil {
				continue
			}
			spec := hwmonUnits[m[1]]
			file := filepath.Join(d, f)
			var v *float64
			if values {
				if x, ok := readFloat(file); ok {
					v = &x
				}
			}
			cname := spec.Quantity + m[2]
			if label, ok := readAttr(filepath.Join(d, m[1]+m[2]+"_label")); ok && label != "" {
				cname = label
			}
			chans = append(chans, Channel{
				Name: cname, Quantity: m[1], Unit: spec.SenML, ABIUnit: spec.ABI,
				Value: scaled(v, spec.K), How: "input", Path: relPath(root, file),
				src: source{kind: "file", file: file, k: spec.K, renumbered: true},
			})
		}
		out = append(out, &Device{
			ID: "hwmon:" + name, Source: "hwmon", Name: strPtr(name), Bus: strPtr(realParent(d)),
			Observability: levelByChannels(chans), Channels: chans,
		})
	}
	return out
}

func probeW1(root string) []*Device {
	var out []*Device
	dirs := listDir(filepath.Join(root, "sys/bus/w1/devices"), func(n string) bool {
		return strings.Contains(n, "-")
	})
	for _, d := range dirs {
		sid := filepath.Base(d)
		fam := strings.ToLower(strings.SplitN(sid, "-", 2)[0])
		chip, isTherm := w1ThermFamilies[fam]
		var chans []Channel
		notes := []string{}
		file := filepath.Join(d, "temperature")
		if t, ok := readFloat(file); isTherm && ok {
			chans = append(chans, Channel{
				Name: "temperature", Quantity: "temp", Unit: "Cel", ABIUnit: "1/1000 degC",
				Value: scaled(&t, 1e-3), How: "temperature", Path: relPath(root, file),
				src: source{kind: "file", file: file, k: 1e-3},
			})
			if ct, ok := readFloat(filepath.Join(d, "conv_time")); ok && ct != 0 {
				notes = append(notes, fmt.Sprintf("blocking read: conversion %d ms per sample", int(ct)))
			}
		}
		name := chip
		if !isTherm {
			name = "family 0x" + fam
		}
		out = append(out, &Device{
			ID: "w1:" + sid, Source: "w1", Name: strPtr(name), Bus: strPtr(realParent(d)),
			Observability: levelByChannels(chans), Channels: chans, Notes: notes, hasNotes: true,
		})
	}
	return out
}

func probeV4L2(root string) []*Device {
	type group struct {
		parent string
		name   *string
		nodes  []V4L2Node
	}
	var groups []*group
	byParent := map[string]*group{}
	for _, d := range listDir(filepath.Join(root, "sys/class/video4linux"), func(n string) bool {
		return strings.HasPrefix(n, "video")
	}) {
		// родитель — USB-интерфейс или платформенное устройство: .../<parent>/video4linux/videoN
		parent := filepath.Dir(filepath.Dir(realPath(d)))
		g, ok := byParent[parent]
		if !ok {
			g = &group{parent: parent, name: readAttrPtr(filepath.Join(d, "name"))}
			byParent[parent] = g
			groups = append(groups, g)
		}
		g.nodes = append(g.nodes, V4L2Node{Dev: filepath.Base(d), Index: readAttrPtr(filepath.Join(d, "index"))})
	}
	var out []*Device
	for _, g := range groups {
		out = append(out, &Device{
			ID: "v4l2:" + pyStr(g.name), Source: "v4l2", Name: g.name, Bus: strPtr(filepath.Base(g.parent)),
			Observability: "B", Channels: []Channel{}, Nodes: g.nodes, hasNodes: true, hasNotes: true,
			Notes: []string{"capture vs metadata node needs VIDIOC_QUERYCAP device_caps; " +
				"on Raspberry Pi CSI cameras use libcamera, not raw /dev/videoN"},
		})
	}
	return out
}

func probeUSB(root string) []*Device {
	var out []*Device
	for _, d := range listDir(filepath.Join(root, "sys/bus/usb/devices"), func(string) bool { return true }) {
		vid, okV := readAttr(filepath.Join(d, "idVendor"))
		if !okV || vid == "" || vid == "1d6b" { // 1d6b = Linux Foundation (корневые хабы)
			continue
		}
		pid := readAttrPtr(filepath.Join(d, "idProduct"))
		name, ok := readAttr(filepath.Join(d, "product"))
		if !ok || name == "" {
			name = vid + ":" + pyStr(pid)
		}
		base := filepath.Base(d)
		out = append(out, &Device{
			ID: "usb:" + vid + ":" + pyStr(pid) + "@" + base, Source: "usb", Name: strPtr(name),
			Vendor: readAttrPtr(filepath.Join(d, "manufacturer")), hasVendor: true,
			Bus: strPtr(base), Observability: "B", Channels: []Channel{},
		})
	}
	return out
}

func probeI2CUnbound(root string, claimed map[string]bool) []*Device {
	var out []*Device
	for _, d := range listDir(filepath.Join(root, "sys/bus/i2c/devices"), func(n string) bool {
		return strings.Contains(n, "-00")
	}) {
		lab, ok := i2cLabel(filepath.Base(d))
		var labPtr *string
		if ok {
			labPtr = &lab
		}
		if claimed[pyStr(labPtr)] && ok {
			continue
		}
		out = append(out, &Device{
			ID: "i2c:" + pyStr(labPtr), Source: "i2c", Name: readAttrPtr(filepath.Join(d, "name")),
			Bus: labPtr, Observability: "C", Channels: []Channel{}, hasNotes: true,
			Notes: []string{"no IIO/hwmon driver bound: only address is known"},
		})
	}
	return out
}

func probeGPIOChips(root string) []*Device {
	var out []*Device
	for _, d := range listDir(filepath.Join(root, "sys/bus/gpio/devices"), func(n string) bool {
		return strings.HasPrefix(n, "gpiochip")
	}) {
		name := filepath.Base(d)
		out = append(out, &Device{
			ID: "gpiochip:" + name, Source: "gpio", Name: strPtr(name), Bus: nil,
			Observability: "C", Channels: []Channel{}, hasNotes: true,
			Notes: []string{"line names/flags via GPIO_V2_GET_LINEINFO_IOCTL (libgpiod)"},
		})
	}
	return out
}

func probeHAT(root string) []*Device {
	d := filepath.Join(root, "proc/device-tree/hat")
	if !isDir(d) {
		return nil
	}
	h := &HATInfo{
		Vendor:     readAttrPtr(filepath.Join(d, "vendor")),
		Product:    readAttrPtr(filepath.Join(d, "product")),
		ProductID:  readAttrPtr(filepath.Join(d, "product_id")),
		ProductVer: readAttrPtr(filepath.Join(d, "product_ver")),
		UUID:       readAttrPtr(filepath.Join(d, "uuid")),
	}
	return []*Device{{
		ID: "hat:" + pyStr(h.Product), Source: "hat", Name: h.Product, Vendor: h.Vendor, hasVendor: true,
		Bus: strPtr("ID_SD/ID_SC EEPROM"), Observability: "B", Channels: []Channel{}, HAT: h,
	}}
}

// applyDeclared добавляет места установки и устройства из declared.json (apply_declared прототипа).
func applyDeclared(inv []*Device, decl *declared.Declared) []*Device {
	for _, dev := range inv {
		if loc, ok := decl.Locations[dev.ID]; ok {
			dev.Location = strPtr(loc)
			dev.hasLocation = true
		}
	}
	for i := range decl.GPIO {
		g := decl.GPIO[i]
		out := g.Direction == "output"
		inv = append(inv, &Device{
			ID: "gpio:" + g.ID, Source: "declared", Name: strPtr(g.ID),
			Bus: strPtr(fmt.Sprintf("%s/line%d", g.Chip, g.Line)), Observability: "C",
			Location: g.Location, hasLocation: true, Description: g.Description, hasDescription: true,
			Channels: []Channel{{
				Name: g.PropertyName(), Quantity: g.Quantity, Unit: "/", Value: nil, How: "gpiod",
				Writable: out, Path: fmt.Sprintf("dev/%s#%d", g.Chip, g.Line),
				src: source{kind: "gpio"},
			}},
			GPIO: &g,
		})
	}
	for _, s := range decl.Serial {
		note := ""
		if s.ObservabilityNote != nil {
			note = *s.ObservabilityNote
		}
		inv = append(inv, &Device{
			ID: "tty:" + s.ID, Source: "declared", Name: strPtr(s.ID), Bus: strPtr(s.TTY),
			Observability: "C", Description: s.Description, hasDescription: true,
			Channels: []Channel{}, Notes: []string{note}, hasNotes: true,
		})
	}
	return inv
}

// Probe — инвентаризация дерева sysfs с корнем root ("/" на плате, каталог фикстуры в тестах)
// и слоем профилей decl (может быть nil).
func Probe(root string, decl *declared.Declared) []*Device {
	inv := probeIIO(root, true)
	inv = append(inv, probeHwmon(root, true)...)
	inv = append(inv, probeW1(root)...)
	claimed := map[string]bool{}
	for _, d := range inv {
		if d.Source == "iio" {
			claimed[pyStr(d.Bus)] = true
		}
	}
	inv = append(inv, probeI2CUnbound(root, claimed)...)
	inv = append(inv, probeV4L2(root)...)
	inv = append(inv, probeUSB(root)...)
	inv = append(inv, probeGPIOChips(root)...)
	inv = append(inv, probeHAT(root)...)
	if decl != nil {
		inv = applyDeclared(inv, decl)
	}
	return inv
}

// Renumbered сообщает, что номер узла устройства канала выдаётся заново при повторном появлении
// (iio:deviceN, hwmonN): вернувшееся устройство может оказаться по другому пути. 1-Wire адресуется
// серийным номером (28-…), его путь не меняется.
func (c *Channel) Renumbered() bool { return c.src.renumbered }

// ProbeRenumbered — инвентаризация только источников с переменной нумерацией (поиск вернувшихся
// устройств) без чтения значений каналов: чтение запускает измерение (I²C у BME280 и ADS1015, обмен с
// DHT11), а для поиска нужны только имена, родители и каналы. Значения (Value) — nil.
func ProbeRenumbered(root string) []*Device {
	return append(probeIIO(root, false), probeHwmon(root, false)...)
}

// Read перечитывает значение канала тем же способом, что и при инвентаризации.
// Для каналов GPIO возвращает ok=false: их читает драйвер линий (пакет gpio).
func (c *Channel) Read() (value *float64, how string, ok bool) {
	switch c.src.kind {
	case "iio":
		v, how := IIOChannelValue(c.src.dir, c.src.base, c.src.typ)
		return scaled(v, c.src.k), how, true
	case "file":
		if v, ok := readFloat(c.src.file); ok {
			return scaled(&v, c.src.k), c.How, true
		}
		return nil, "absent", true
	default:
		return nil, "", false
	}
}

// IsGPIO сообщает, что значение канала читается через линию GPIO.
func (c *Channel) IsGPIO() bool { return c.src.kind == "gpio" }

// scaled переводит значение ABI в единицы SenML с округлением до 6 знаков, как round(v * k, 6).
func scaled(v *float64, k float64) *float64 {
	if v == nil {
		return nil
	}
	x := round6(*v * k)
	return &x
}

func levelByChannels(chans []Channel) string {
	if len(chans) > 0 {
		return "A"
	}
	return "B"
}
