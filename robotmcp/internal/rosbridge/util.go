package rosbridge

import (
	"encoding/base64"
	"fmt"
	"math"
)

var posInf = math.Inf(1)

// Bytes приводит поле uint8[] (JSON: base64-строка; CBOR: байтовая строка) к []byte.
func Bytes(v any) ([]byte, error) {
	switch x := v.(type) {
	case []byte:
		return x, nil
	case string:
		return base64.StdEncoding.DecodeString(x)
	case []any: // массив чисел (редко: старые версии rosbridge)
		out := make([]byte, len(x))
		for i, e := range x {
			f, ok := e.(float64)
			if !ok {
				return nil, fmt.Errorf("element %d: %T", i, e)
			}
			out[i] = byte(f)
		}
		return out, nil
	}
	return nil, fmt.Errorf("not bytes: %T", v)
}

// Num — число из дерева сообщения (0, false — нет или не число).
func Num(m map[string]any, path ...string) (float64, bool) {
	var cur any = m
	for _, p := range path {
		mm, ok := cur.(map[string]any)
		if !ok {
			return 0, false
		}
		cur = mm[p]
	}
	f, ok := cur.(float64)
	return f, ok
}

// Obj — вложенный объект сообщения.
func Obj(m map[string]any, path ...string) map[string]any {
	var cur any = m
	for _, p := range path {
		mm, ok := cur.(map[string]any)
		if !ok {
			return nil
		}
		cur = mm[p]
	}
	mm, _ := cur.(map[string]any)
	return mm
}
