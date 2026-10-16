package inventory

import (
	"fmt"
	"sort"
	"strings"
)

// Представления модели. Все — данные для канонической сериализации (map/slice), порядок детерминирован.

// AgentView — полное описание устройства для агента (ресурс hw://inventory).
func (d *Device) AgentView() map[string]any {
	v := map[string]any{
		"id":            d.ID,
		"source_id":     d.SourceID,
		"source":        d.Source,
		"observability": d.Observability,
	}
	if d.Kind != "" {
		v["kind"] = d.Kind
	}
	if d.Location != "" {
		v["location"] = d.Location
	}
	if d.Description != "" {
		v["description"] = d.Description
	}
	props := map[string]any{}
	for _, p := range d.Properties {
		pv := map[string]any{"type": p.Type, "writable": p.Writable, "quantity": p.Quantity}
		if p.Unit != "" {
			pv["unit"] = p.Unit
		}
		props[p.Name] = pv
	}
	v["properties"] = props
	acts := map[string]any{}
	for _, a := range d.Actions {
		acts[a.Name] = map[string]any{"description": a.Description, "input": a.Input}
	}
	v["actions"] = acts
	if len(d.Notes) > 0 {
		v["notes"] = d.Notes
	}
	return v
}

// CompactView — короткая запись для list_devices (в духе list_devices_result из ctx_formats.py):
// свойства как "единица[,rw]", логические — "bool[,rw]"; действия — список аргументов.
func (d *Device) CompactView() map[string]any {
	v := map[string]any{"id": d.ID}
	if d.Location != "" {
		v["loc"] = d.Location
	}
	if d.Kind != "" && !strings.Contains(d.ID, Slug(d.Kind)) {
		v["kind"] = d.Kind
	}
	if len(d.Properties) > 0 {
		props := map[string]any{}
		for _, p := range d.Properties {
			props[p.Name] = propSig(p)
		}
		v["props"] = props
	}
	if len(d.Actions) > 0 {
		acts := map[string]any{}
		for _, a := range d.Actions {
			acts[a.Name] = argSig(a)
		}
		v["actions"] = acts
	}
	if len(d.Notes) > 0 && d.Actionable() {
		v["notes"] = d.Notes
	}
	return v
}

func propSig(p *Property) string {
	s := p.Unit
	if p.Type == TypeBoolean {
		s = "bool"
	}
	if p.Writable {
		s += ",rw"
	}
	return s
}

// argSig — аргументы действия строкой: "seconds:number".
func argSig(a *Action) string {
	props, _ := a.Input["properties"].(map[string]any)
	names := make([]string, 0, len(props))
	for k := range props {
		names = append(names, k)
	}
	sort.Strings(names)
	parts := make([]string, 0, len(names))
	for _, k := range names {
		t := ""
		if m, ok := props[k].(map[string]any); ok {
			t, _ = m["type"].(string)
		}
		parts = append(parts, k+":"+t)
	}
	return strings.Join(parts, ",")
}

// TextLine — строка текстовой проекции L0 (как text_inventory из ctx_formats.py):
// "kitchen_bme280: humidity [%RH], pressure [Pa], temperature [Cel]".
func (d *Device) TextLine() string {
	var parts []string
	for _, p := range d.Properties {
		unit := p.Unit
		if p.Type == TypeBoolean {
			unit = "/"
		}
		if p.Writable {
			unit += ", rw"
		}
		parts = append(parts, fmt.Sprintf("%s [%s]", p.Name, unit))
	}
	head := d.ID
	if d.Location != "" && !strings.Contains(d.ID, Slug(d.Location)) {
		head += " @" + d.Location
	}
	line := head + ": " + strings.Join(parts, ", ")
	if len(d.Actions) > 0 {
		var acts []string
		for _, a := range d.Actions {
			acts = append(acts, a.Name+"("+argSig(a)+")")
		}
		line += "; actions: " + strings.Join(acts, ", ")
	}
	return line
}

// Text — текстовая проекция инвентаря (только устройства со свойствами или действиями).
func (inv *Inventory) Text() string {
	var lines []string
	for _, d := range inv.Devices {
		if d.Actionable() {
			lines = append(lines, d.TextLine())
		}
	}
	return strings.Join(lines, "\n")
}

// AgentView — ресурс hw://inventory.
func (inv *Inventory) AgentView() map[string]any {
	devs := make([]any, 0, len(inv.Devices))
	for _, d := range inv.Devices {
		devs = append(devs, d.AgentView())
	}
	return map[string]any{"devices": devs}
}

// CompactList — результат list_devices; all=false — только устройства со свойствами или действиями.
func (inv *Inventory) CompactList(all bool) map[string]any {
	devs := []any{}
	for _, d := range inv.Devices {
		if all || d.Actionable() {
			devs = append(devs, d.CompactView())
		}
	}
	return map[string]any{"devices": devs}
}

// ----------------------------------------------------------------------------------------------
// W3C WoT Thing Description 1.1

// TDContext — контекст TD 1.1.
const TDContext = "https://www.w3.org/2022/wot/td/v1.1"

// TD — описание устройства в формате WoT TD 1.1 (проверяется по официальной схеме в тестах).
// Формы: свойства sysfs — file:///<путь>, линии GPIO — gpiod://<чип>/<линия> (нестандартная схема),
// плюс ресурс состояния hw://state/<устройство> для наблюдения через MCP.
func (d *Device) TD() map[string]any {
	td := map[string]any{
		"@context":            TDContext,
		"id":                  "urn:dev:edge:" + d.ID,
		"title":               d.ID,
		"securityDefinitions": map[string]any{"nosec_sc": map[string]any{"scheme": "nosec"}},
		"security":            "nosec_sc",
	}
	if d.Description != "" {
		td["description"] = d.Description
	}
	stateHref := "hw://state/" + d.ID
	props := map[string]any{}
	for _, p := range d.Properties {
		ops := []any{"readproperty"}
		if p.Writable {
			ops = append(ops, "writeproperty")
		}
		pd := map[string]any{
			"title":      p.Title,
			"type":       p.Type,
			"readOnly":   !p.Writable,
			"observable": true,
			"forms": []any{
				map[string]any{"href": p.Href, "contentType": "text/plain", "op": ops},
				map[string]any{"href": stateHref, "contentType": "application/json",
					"op": []any{"readproperty", "observeproperty", "unobserveproperty"}},
			},
		}
		if p.Unit != "" {
			pd["unit"] = p.Unit
		}
		props[p.Name] = pd
	}
	td["properties"] = props
	if len(d.Actions) > 0 {
		acts := map[string]any{}
		for _, a := range d.Actions {
			href := "hw://action/" + d.ID + "/" + a.Name
			if p := d.Property(a.Property); p != nil {
				href = p.Href
			}
			acts[a.Name] = map[string]any{
				"description": a.Description,
				"input":       a.Input,
				"forms":       []any{map[string]any{"href": href, "op": []any{"invokeaction"}}},
			}
		}
		td["actions"] = acts
	}
	return td
}

// TDs — описания всех устройств со свойствами или действиями (ресурс hw://td).
func (inv *Inventory) TDs() []any {
	out := []any{}
	for _, d := range inv.Devices {
		if d.Actionable() {
			out = append(out, d.TD())
		}
	}
	return out
}
