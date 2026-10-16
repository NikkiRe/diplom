// Package canon — каноническая сериализация JSON (IMPLEMENTATION_SPEC §0 п. 5): ключи объектов
// отсортированы, без отступов, UTF-8 без \u-экранирования (и без HTML-экранирования <, >, &), числа —
// кратчайшая запись Go. Копия hwmcp/internal/canon (internal-пакеты другого модуля не импортируются).
package canon

import (
	"bytes"
	"encoding/json"
)

// Marshal сериализует v канонически. Структуры сначала превращаются в обобщённое дерево
// (map[string]any с json.Number), чтобы порядок ключей не зависел от порядка полей.
func Marshal(v any) ([]byte, error) {
	raw, err := encode(v)
	if err != nil {
		return nil, err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var generic any
	if err := dec.Decode(&generic); err != nil {
		return nil, err
	}
	return encode(generic)
}

// String — Marshal для значений, собранных в коде (ошибка — паника).
func String(v any) string {
	b, err := Marshal(v)
	if err != nil {
		panic(err)
	}
	return string(b)
}

func encode(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(buf.Bytes(), []byte("\n")), nil
}
