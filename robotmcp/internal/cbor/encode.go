package cbor

import (
	"encoding/binary"
	"fmt"
	"math"
	"sort"
)

// Float32Array кодируется типизированным массивом float32 LE (тег 85), как числовые массивы rosbridge.
type Float32Array []float32

// Encode кодирует дерево (map[string]any с сортировкой ключей, []any, string, []byte, числа, bool, nil,
// Float32Array). Нужен фейковому rosbridge в тестах и для проверок декодера.
func Encode(v any) ([]byte, error) {
	var e encoder
	if err := e.value(v); err != nil {
		return nil, err
	}
	return e.b, nil
}

type encoder struct{ b []byte }

func (e *encoder) head(major byte, n uint64) {
	switch {
	case n < 24:
		e.b = append(e.b, major<<5|byte(n))
	case n <= math.MaxUint8:
		e.b = append(e.b, major<<5|24, byte(n))
	case n <= math.MaxUint16:
		e.b = append(e.b, major<<5|25)
		e.b = binary.BigEndian.AppendUint16(e.b, uint16(n))
	case n <= math.MaxUint32:
		e.b = append(e.b, major<<5|26)
		e.b = binary.BigEndian.AppendUint32(e.b, uint32(n))
	default:
		e.b = append(e.b, major<<5|27)
		e.b = binary.BigEndian.AppendUint64(e.b, n)
	}
}

func (e *encoder) int(i int64) {
	if i >= 0 {
		e.head(0, uint64(i))
	} else {
		e.head(1, uint64(-1-i))
	}
}

func (e *encoder) value(v any) error {
	switch x := v.(type) {
	case nil:
		e.b = append(e.b, 0xf6)
	case bool:
		if x {
			e.b = append(e.b, 0xf5)
		} else {
			e.b = append(e.b, 0xf4)
		}
	case int:
		e.int(int64(x))
	case int64:
		e.int(x)
	case uint32:
		e.head(0, uint64(x))
	case float32:
		e.b = append(e.b, 0xfa)
		e.b = binary.BigEndian.AppendUint32(e.b, math.Float32bits(x))
	case float64:
		e.b = append(e.b, 0xfb)
		e.b = binary.BigEndian.AppendUint64(e.b, math.Float64bits(x))
	case string:
		e.head(3, uint64(len(x)))
		e.b = append(e.b, x...)
	case []byte:
		e.head(2, uint64(len(x)))
		e.b = append(e.b, x...)
	case Float32Array:
		e.head(6, 85)
		e.head(2, uint64(4*len(x)))
		for _, f := range x {
			e.b = binary.LittleEndian.AppendUint32(e.b, math.Float32bits(f))
		}
	case []any:
		e.head(4, uint64(len(x)))
		for _, it := range x {
			if err := e.value(it); err != nil {
				return err
			}
		}
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		e.head(5, uint64(len(keys)))
		for _, k := range keys {
			e.head(3, uint64(len(k)))
			e.b = append(e.b, k...)
			if err := e.value(x[k]); err != nil {
				return err
			}
		}
	default:
		return fmt.Errorf("cbor: cannot encode %T", v)
	}
	return nil
}
