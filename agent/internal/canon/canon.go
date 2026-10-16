// Package canon реализует каноническую сериализацию JSON (спецификация §0.5):
// ключи объектов отсортированы, без отступов и пробелов, UTF-8 без \u-экранирования
// (кроме управляющих символов, которые JSON обязан экранировать), числа — в
// кратчайшей десятичной записи. Одинаковые данные всегда дают одинаковые байты,
// поэтому стабильный префикс промпта не «дрожит» между запросами.
package canon

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strconv"
	"unicode/utf8"
)

// Marshal возвращает каноническое JSON-представление v.
//
// v может быть любым значением, которое понимает encoding/json (структуры с тегами,
// map, срезы, json.RawMessage). Сначала значение приводится к дереву
// map[string]any / []any / json.Number, затем печатается заново.
func Marshal(v any) ([]byte, error) {
	tree, err := normalize(v)
	if err != nil {
		return nil, err
	}
	var b bytes.Buffer
	if err := write(&b, tree); err != nil {
		return nil, err
	}
	return b.Bytes(), nil
}

// String — то же, что Marshal, но возвращает строку; при ошибке возвращает
// строку с описанием ошибки в виде JSON-строки (удобно для журналов и промпта,
// где ошибка сериализации не должна ронять агента).
func String(v any) string {
	b, err := Marshal(v)
	if err != nil {
		return strconv.Quote("canon error: " + err.Error())
	}
	return string(b)
}

// normalize превращает произвольное значение в дерево из простых типов.
func normalize(v any) (any, error) {
	switch x := v.(type) {
	case nil, bool, string, json.Number:
		return x, nil
	case float64:
		return x, nil
	case float32:
		return float64(x), nil
	case int:
		return json.Number(strconv.FormatInt(int64(x), 10)), nil
	case int64:
		return json.Number(strconv.FormatInt(x, 10)), nil
	case int32:
		return json.Number(strconv.FormatInt(int64(x), 10)), nil
	case uint64:
		return json.Number(strconv.FormatUint(x, 10)), nil
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, e := range x {
			n, err := normalize(e)
			if err != nil {
				return nil, err
			}
			out[k] = n
		}
		return out, nil
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			n, err := normalize(e)
			if err != nil {
				return nil, err
			}
			out[i] = n
		}
		return out, nil
	case json.RawMessage:
		return decode(x)
	}
	// Общий путь: стандартный маршалинг без HTML-экранирования и разбор с UseNumber.
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return decode(buf.Bytes())
}

func decode(data []byte) (any, error) {
	if len(bytes.TrimSpace(data)) == 0 {
		return nil, nil
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	var out any
	if err := dec.Decode(&out); err != nil {
		return nil, err
	}
	return out, nil
}

func write(b *bytes.Buffer, v any) error {
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		if x {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case string:
		writeString(b, x)
	case json.Number:
		s, err := numberString(x)
		if err != nil {
			return err
		}
		b.WriteString(s)
	case float64:
		s, err := FormatFloat(x)
		if err != nil {
			return err
		}
		b.WriteString(s)
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteByte(',')
			}
			writeString(b, k)
			b.WriteByte(':')
			if err := write(b, x[k]); err != nil {
				return err
			}
		}
		b.WriteByte('}')
	case []any:
		b.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := write(b, e); err != nil {
				return err
			}
		}
		b.WriteByte(']')
	default:
		return fmt.Errorf("canon: unexpected type %T", v)
	}
	return nil
}

// numberString печатает json.Number: целые — как есть, дробные — в кратчайшей
// записи float64 (1.50 -> 1.5, 2.0 -> 2).
func numberString(n json.Number) (string, error) {
	s := string(n)
	if _, err := strconv.ParseInt(s, 10, 64); err == nil {
		return s, nil
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return "", fmt.Errorf("canon: bad number %q", s)
	}
	return FormatFloat(f)
}

// FormatFloat печатает число в кратчайшей десятичной записи без экспоненты для
// «обычных» величин; целые значения печатаются без дробной части.
func FormatFloat(f float64) (string, error) {
	if math.IsNaN(f) || math.IsInf(f, 0) {
		return "", fmt.Errorf("canon: non-finite number %v", f)
	}
	if f == 0 {
		return "0", nil // в том числе -0
	}
	if f == math.Trunc(f) && math.Abs(f) < 1e15 {
		return strconv.FormatFloat(f, 'f', -1, 64), nil
	}
	abs := math.Abs(f)
	if abs >= 1e-6 && abs < 1e21 {
		return strconv.FormatFloat(f, 'f', -1, 64), nil
	}
	return strconv.FormatFloat(f, 'g', -1, 64), nil
}

// writeString пишет JSON-строку без \u-экранирования не-ASCII символов и без
// HTML-экранирования; экранируются только кавычка, обратная косая черта и
// управляющие символы U+0000–U+001F (так требует RFC 8259).
func writeString(b *bytes.Buffer, s string) {
	b.WriteByte('"')
	for i := 0; i < len(s); {
		c := s[i]
		if c < utf8.RuneSelf {
			switch c {
			case '"':
				b.WriteString(`\"`)
			case '\\':
				b.WriteString(`\\`)
			case '\n':
				b.WriteString(`\n`)
			case '\r':
				b.WriteString(`\r`)
			case '\t':
				b.WriteString(`\t`)
			default:
				if c < 0x20 {
					fmt.Fprintf(b, `\u%04x`, c)
				} else {
					b.WriteByte(c)
				}
			}
			i++
			continue
		}
		r, size := utf8.DecodeRuneInString(s[i:])
		if r == utf8.RuneError && size == 1 {
			b.WriteString("�") // невалидный UTF-8 заменяется, как в encoding/json
		} else {
			b.WriteString(s[i : i+size])
		}
		i += size
	}
	b.WriteByte('"')
}

// Round округляет x до шага step (точность датчика). step <= 0 — без округления.
// Результат дополнительно очищается от двоичного «хвоста» (0.1+0.2 -> 0.3).
func Round(x, step float64) float64 {
	if step <= 0 || math.IsNaN(x) || math.IsInf(x, 0) {
		return x
	}
	r := math.Round(x/step) * step
	// Число знаков после запятой у шага (0.1 -> 1, 0.05 -> 2, 0.25 -> 2, 1 -> 0).
	dec := 0
	for s := step; dec < 12 && math.Abs(s-math.Round(s)) > 1e-9*math.Max(1, s); s *= 10 {
		dec++
	}
	p := math.Pow(10, float64(dec))
	return math.Round(r*p) / p
}

// Value печатает скалярное значение датчика канонически: числа — кратко,
// строки — в кавычках, bool — true/false, составные — каноническим JSON.
func Value(v any) string {
	return String(v)
}
