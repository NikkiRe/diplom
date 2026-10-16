// Package canon — каноническая сериализация JSON (IMPLEMENTATION_SPEC §0 п. 5), как в hw-mcp:
// ключи объектов отсортированы, без отступов, UTF-8 без \u- и HTML-экранирования, числа — кратчайшая
// запись Go. Одинаковое состояние даёт побайтно одинаковый текст.
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

func encode(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(buf.Bytes(), []byte("\n")), nil
}
