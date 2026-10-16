// Package declared — разбор declared.json: то, чего ОС не знает о подключённом оборудовании
// (назначение линий GPIO, место установки, что стоит за USB-UART), и расширения hw-mcp:
// имена устройств для агента, пороги уведомлений и политика шлюза приводов.
//
// Базовая часть формата (locations, gpio, serial) совпадает с прототипом
// research/new_topic/GAP2_artifacts/declared.json; неизвестные ключи считаются ошибкой,
// чтобы опечатка в настройке безопасности не проходила молча.
package declared

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"os"
	"regexp"
)

// Declared — содержимое declared.json.
type Declared struct {
	Comment   string            `json:"_comment,omitempty"`
	Locations map[string]string `json:"locations,omitempty"` // id из инвентаря sysfs -> место
	GPIO      []GPIO            `json:"gpio,omitempty"`
	Serial    []Serial          `json:"serial,omitempty"`

	// Расширения hw-mcp.

	// Names задаёт имя устройства для агента по id из инвентаря sysfs ("iio:bme280@i2c-1/0x76" -> "bme280").
	Names map[string]string `json:"names,omitempty"`
	// Thresholds — пороги изменения для уведомлений по ключу "<устройство>.<свойство>".
	Thresholds map[string]float64 `json:"thresholds,omitempty"`
	// SamplePeriods задаёт секунды между опросами устройства или отдельного свойства.
	// Ключи проверяются по обнаруженному инвентарю при создании Hub.
	SamplePeriods map[string]float64 `json:"sample_periods,omitempty"`
	// StuckAfter — секунды, дольше которых неизменное число считается «застрявшим» показанием датчика
	// (значение null с причиной stuck), по устройству или свойству; ключи проверяются при создании Hub.
	StuckAfter map[string]float64 `json:"stuck_after_s,omitempty"`
	// Gate — политика шлюза приводов (разбирается пакетом gate).
	Gate json.RawMessage `json:"gate,omitempty"`
}

// GPIO — назначение линии GPIO.
type GPIO struct {
	ID                   string  `json:"id"`
	Chip                 string  `json:"chip"`
	Line                 int     `json:"line"`
	Direction            string  `json:"direction"` // input | output
	Quantity             string  `json:"quantity"`
	Location             *string `json:"location,omitempty"`
	Description          *string `json:"description,omitempty"`
	RequiresConfirmation bool    `json:"requires_confirmation,omitempty"`

	// Расширения hw-mcp.
	ActiveLow  bool   `json:"active_low,omitempty"`  // логическая 1 = низкий уровень на линии
	Property   string `json:"property,omitempty"`    // имя свойства, по умолчанию "state"
	DebounceMs int    `json:"debounce_ms,omitempty"` // подавление дребезга входа, мс (uAPI v2 — ядром, v1 — hw-mcp)
}

// Serial — устройство за последовательным портом.
type Serial struct {
	ID                string  `json:"id"`
	TTY               string  `json:"tty"`
	Description       *string `json:"description,omitempty"`
	ObservabilityNote *string `json:"observability_note,omitempty"`
}

// PropertyName возвращает имя свойства линии GPIO ("state", если не задано).
func (g GPIO) PropertyName() string {
	if g.Property != "" {
		return g.Property
	}
	return "state"
}

var idRe = regexp.MustCompile(`^[a-z][a-z0-9_]{0,47}$`)

// Load читает и проверяет declared.json.
func Load(path string) (*Declared, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return Parse(b)
}

// Parse разбирает declared.json из памяти.
func Parse(b []byte) (*Declared, error) {
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	var d Declared
	if err := dec.Decode(&d); err != nil {
		return nil, fmt.Errorf("declared.json: %w", err)
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, fmt.Errorf("declared.json: unexpected data after the top-level object")
	}
	if err := d.Validate(); err != nil {
		return nil, fmt.Errorf("declared.json: %w", err)
	}
	return &d, nil
}

// Validate проверяет согласованность назначений.
func (d *Declared) Validate() error {
	seen := map[string]bool{}
	lines := map[string]string{}
	for i, g := range d.GPIO {
		if !idRe.MatchString(g.ID) {
			return fmt.Errorf("gpio[%d]: id %q must match %s", i, g.ID, idRe)
		}
		if seen[g.ID] {
			return fmt.Errorf("gpio[%d]: duplicate id %q", i, g.ID)
		}
		seen[g.ID] = true
		if g.Chip == "" {
			return fmt.Errorf("gpio %q: chip is required", g.ID)
		}
		if g.Line < 0 || g.Line > 1023 {
			return fmt.Errorf("gpio %q: line %d out of range", g.ID, g.Line)
		}
		if g.Direction != "input" && g.Direction != "output" {
			return fmt.Errorf("gpio %q: direction must be input or output, got %q", g.ID, g.Direction)
		}
		if g.DebounceMs < 0 || g.DebounceMs > 1000 || (g.DebounceMs > 0 && g.Direction != "input") {
			return fmt.Errorf("gpio %q: debounce_ms must be 0..1000 and only for inputs", g.ID)
		}
		if g.Property != "" && !idRe.MatchString(g.Property) {
			return fmt.Errorf("gpio %q: property %q must match %s", g.ID, g.Property, idRe)
		}
		key := fmt.Sprintf("%s#%d", g.Chip, g.Line)
		if other, dup := lines[key]; dup {
			return fmt.Errorf("gpio %q: line %s already assigned to %q", g.ID, key, other)
		}
		lines[key] = g.ID
	}
	for i, s := range d.Serial {
		if !idRe.MatchString(s.ID) {
			return fmt.Errorf("serial[%d]: id %q must match %s", i, s.ID, idRe)
		}
		if seen[s.ID] {
			return fmt.Errorf("serial[%d]: duplicate id %q", i, s.ID)
		}
		seen[s.ID] = true
	}
	for src, name := range d.Names {
		if !idRe.MatchString(name) {
			return fmt.Errorf("names[%q]: %q must match %s", src, name, idRe)
		}
	}
	for k, v := range d.Thresholds {
		if v < 0 {
			return fmt.Errorf("thresholds[%q]: negative threshold %g", k, v)
		}
	}
	for name, m := range map[string]map[string]float64{"sample_periods": d.SamplePeriods, "stuck_after_s": d.StuckAfter} {
		for k, v := range m {
			if k == "" || !(v > 0) || math.IsInf(v, 0) {
				return fmt.Errorf("%s[%q]: must be a finite positive number of seconds", name, k)
			}
		}
	}
	return nil
}
