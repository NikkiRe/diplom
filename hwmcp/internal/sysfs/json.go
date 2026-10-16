package sysfs

import (
	"bytes"
	"encoding/json"
	"os"
)

// Сериализация инвентаря в формате прототипа sysfs_probe.py: те же ключи, тот же порядок,
// null там, где прототип пишет None. Это «сырой» инвентарь (ресурс hw://probe, флаг -print-inventory);
// агенту показывается модель из пакета inventory.

type kv struct {
	k string
	v any
}

func marshalOrdered(pairs []kv) ([]byte, error) {
	var buf bytes.Buffer
	buf.WriteByte('{')
	for i, p := range pairs {
		if i > 0 {
			buf.WriteByte(',')
		}
		kb, _ := json.Marshal(p.k)
		buf.Write(kb)
		buf.WriteByte(':')
		vb, err := marshalNoEscape(p.v)
		if err != nil {
			return nil, err
		}
		buf.Write(vb)
	}
	buf.WriteByte('}')
	return buf.Bytes(), nil
}

func marshalNoEscape(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(buf.Bytes(), []byte("\n")), nil
}

// MarshalJSON — канал в порядке ключей прототипа.
func (c Channel) MarshalJSON() ([]byte, error) {
	pairs := []kv{{"name", c.Name}, {"quantity", c.Quantity}, {"unit", c.Unit}}
	if c.ABIUnit != "" {
		pairs = append(pairs, kv{"abi_unit", c.ABIUnit})
	}
	pairs = append(pairs, kv{"value", c.Value}, kv{"how", c.How}, kv{"path", c.Path}, kv{"writable", c.Writable})
	return marshalOrdered(pairs)
}

// MarshalJSON — устройство с набором ключей, зависящим от источника (как словари прототипа).
func (d *Device) MarshalJSON() ([]byte, error) {
	chans := d.Channels
	if chans == nil {
		chans = []Channel{}
	}
	pairs := []kv{{"id", d.ID}, {"source", d.Source}, {"name", d.Name}}
	if d.hasVendor {
		pairs = append(pairs, kv{"vendor", d.Vendor})
	}
	pairs = append(pairs, kv{"bus", d.Bus}, kv{"observability", d.Observability})
	declaredGPIO := d.Source == "declared" && d.GPIO != nil
	if declaredGPIO {
		// у объявленных линий location и description стоят перед channels
		pairs = append(pairs, kv{"location", d.Location}, kv{"description", d.Description})
	} else if d.hasDescription {
		pairs = append(pairs, kv{"description", d.Description})
	}
	pairs = append(pairs, kv{"channels", chans})
	if d.hasNodes {
		pairs = append(pairs, kv{"nodes", d.Nodes})
	}
	if d.hasNotes {
		notes := d.Notes
		if notes == nil {
			notes = []string{}
		}
		pairs = append(pairs, kv{"notes", notes})
	}
	if d.HAT != nil {
		h := d.HAT
		hat, err := marshalOrdered([]kv{{"vendor", h.Vendor}, {"product", h.Product},
			{"product_id", h.ProductID}, {"product_ver", h.ProductVer}, {"uuid", h.UUID}})
		if err != nil {
			return nil, err
		}
		pairs = append(pairs, kv{"hat", json.RawMessage(hat)})
	}
	if d.hasLocation && !declaredGPIO {
		pairs = append(pairs, kv{"location", d.Location})
	}
	return marshalOrdered(pairs)
}

// MarshalInventory сериализует инвентарь (компактно; indent > 0 — с отступами).
func MarshalInventory(inv []*Device, indent int) ([]byte, error) {
	if inv == nil {
		inv = []*Device{}
	}
	b, err := marshalNoEscape(inv)
	if err != nil || indent <= 0 {
		return b, err
	}
	var out bytes.Buffer
	if err := json.Indent(&out, b, "", string(bytes.Repeat([]byte(" "), indent))); err != nil {
		return nil, err
	}
	return out.Bytes(), nil
}

func isDir(path string) bool {
	st, err := os.Stat(path)
	return err == nil && st.IsDir()
}
