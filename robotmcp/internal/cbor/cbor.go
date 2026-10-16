// Package cbor — минимальный декодер/кодер CBOR (RFC 8949) для сообщений rosbridge с compression "cbor".
//
// rosbridge (rosbridge_library/internal/cbor_conversion.py) кодирует всё сообщение протокола
// ({"op":"publish","topic":…,"msg":{…}}) одним объектом CBOR; числовые массивы — типизированными
// массивами RFC 8746 (тег 85 — float32 LE, 86 — float64 LE, 69–79 — целые LE, 72 — int8), uint8[]/char[] —
// байтовой строкой. Декодер отдаёт то же дерево, что encoding/json (map[string]any, []any, float64, string,
// bool, nil), плюс []byte для байтовых строк и []float64 для типизированных числовых массивов: так лидар
// (360 float32) разбирается без 360 аллокаций интерфейсов.
package cbor

import (
	"encoding/binary"
	"errors"
	"fmt"
	"math"
)

// ErrTruncated — данные кончились раньше объекта.
var ErrTruncated = errors.New("cbor: truncated")

const maxDepth = 64

// Decode разбирает один объект CBOR (лишние байты после него — ошибка).
func Decode(b []byte) (any, error) {
	d := decoder{b: b}
	v, err := d.value(0)
	if err != nil {
		return nil, err
	}
	if d.i != len(d.b) {
		return nil, fmt.Errorf("cbor: %d trailing bytes", len(d.b)-d.i)
	}
	return v, nil
}

type decoder struct {
	b []byte
	i int
}

func (d *decoder) byte1() (byte, error) {
	if d.i >= len(d.b) {
		return 0, ErrTruncated
	}
	c := d.b[d.i]
	d.i++
	return c, nil
}

func (d *decoder) take(n uint64) ([]byte, error) {
	if n > uint64(len(d.b)-d.i) {
		return nil, ErrTruncated
	}
	s := d.b[d.i : d.i+int(n)]
	d.i += int(n)
	return s, nil
}

// head читает начальный байт и аргумент; indef — неопределённая длина (ai = 31).
func (d *decoder) head() (major byte, ai byte, arg uint64, indef bool, err error) {
	c, err := d.byte1()
	if err != nil {
		return
	}
	major, ai = c>>5, c&0x1f
	switch {
	case ai < 24:
		arg = uint64(ai)
	case ai == 24:
		var x []byte
		if x, err = d.take(1); err == nil {
			arg = uint64(x[0])
		}
	case ai == 25:
		var x []byte
		if x, err = d.take(2); err == nil {
			arg = uint64(binary.BigEndian.Uint16(x))
		}
	case ai == 26:
		var x []byte
		if x, err = d.take(4); err == nil {
			arg = uint64(binary.BigEndian.Uint32(x))
		}
	case ai == 27:
		var x []byte
		if x, err = d.take(8); err == nil {
			arg = binary.BigEndian.Uint64(x)
		}
	case ai == 31 && major >= 2 && major <= 5 || ai == 31 && major == 7:
		indef = true
	default:
		err = fmt.Errorf("cbor: bad additional info %d (major %d)", ai, major)
	}
	return
}

func (d *decoder) value(depth int) (any, error) {
	if depth > maxDepth {
		return nil, errors.New("cbor: nesting too deep")
	}
	major, ai, arg, indef, err := d.head()
	if err != nil {
		return nil, err
	}
	switch major {
	case 0:
		return float64(arg), nil
	case 1:
		return -1 - float64(arg), nil
	case 2, 3:
		var s []byte
		if indef {
			for {
				if d.i < len(d.b) && d.b[d.i] == 0xff {
					d.i++
					break
				}
				m, _, n, ind, err := d.head()
				if err != nil {
					return nil, err
				}
				if m != major || ind {
					return nil, errors.New("cbor: bad chunk in indefinite string")
				}
				c, err := d.take(n)
				if err != nil {
					return nil, err
				}
				s = append(s, c...)
			}
		} else {
			c, err := d.take(arg)
			if err != nil {
				return nil, err
			}
			s = append([]byte(nil), c...)
		}
		if major == 3 {
			return string(s), nil
		}
		return s, nil
	case 4:
		var out []any
		if !indef {
			if arg > uint64(len(d.b)-d.i) { // каждый элемент — минимум 1 байт
				return nil, ErrTruncated
			}
			out = make([]any, 0, arg)
		}
		for n := uint64(0); indef || n < arg; n++ {
			if indef && d.i < len(d.b) && d.b[d.i] == 0xff {
				d.i++
				break
			}
			v, err := d.value(depth + 1)
			if err != nil {
				return nil, err
			}
			out = append(out, v)
		}
		if out == nil {
			out = []any{}
		}
		return out, nil
	case 5:
		out := map[string]any{}
		for n := uint64(0); indef || n < arg; n++ {
			if indef && d.i < len(d.b) && d.b[d.i] == 0xff {
				d.i++
				break
			}
			k, err := d.value(depth + 1)
			if err != nil {
				return nil, err
			}
			v, err := d.value(depth + 1)
			if err != nil {
				return nil, err
			}
			ks, ok := k.(string)
			if !ok {
				ks = fmt.Sprint(k)
			}
			out[ks] = v
		}
		return out, nil
	case 6:
		inner, err := d.value(depth + 1)
		if err != nil {
			return nil, err
		}
		return typedArray(arg, inner)
	case 7:
		switch ai {
		case 20:
			return false, nil
		case 21:
			return true, nil
		case 22, 23:
			return nil, nil
		case 25:
			return float64(halfToFloat(uint16(arg))), nil
		case 26:
			return float64(math.Float32frombits(uint32(arg))), nil
		case 27:
			return math.Float64frombits(arg), nil
		case 31:
			return nil, errors.New("cbor: unexpected break")
		}
		if ai < 24 || ai == 24 {
			return nil, nil // простые значения без смысла для rosbridge
		}
	}
	return nil, fmt.Errorf("cbor: unsupported major %d", major)
}

// typedArray раскрывает типизированный массив RFC 8746; прочие теги отдают вложенное значение.
func typedArray(tag uint64, inner any) (any, error) {
	b, ok := inner.([]byte)
	if !ok || tag < 64 || tag > 87 {
		return inner, nil
	}
	size, conv := elem(tag)
	if conv == nil {
		if tag == 64 || tag == 68 {
			return b, nil // uint8
		}
		return inner, nil
	}
	if len(b)%size != 0 {
		return nil, fmt.Errorf("cbor: typed array tag %d: %d bytes not multiple of %d", tag, len(b), size)
	}
	out := make([]float64, len(b)/size)
	for i := range out {
		out[i] = conv(b[i*size : (i+1)*size])
	}
	return out, nil
}

func elem(tag uint64) (int, func([]byte) float64) {
	le, be := binary.LittleEndian, binary.BigEndian
	switch tag {
	case 65:
		return 2, func(x []byte) float64 { return float64(be.Uint16(x)) }
	case 66:
		return 4, func(x []byte) float64 { return float64(be.Uint32(x)) }
	case 67:
		return 8, func(x []byte) float64 { return float64(be.Uint64(x)) }
	case 69:
		return 2, func(x []byte) float64 { return float64(le.Uint16(x)) }
	case 70:
		return 4, func(x []byte) float64 { return float64(le.Uint32(x)) }
	case 71:
		return 8, func(x []byte) float64 { return float64(le.Uint64(x)) }
	case 72:
		return 1, func(x []byte) float64 { return float64(int8(x[0])) }
	case 73:
		return 2, func(x []byte) float64 { return float64(int16(be.Uint16(x))) }
	case 74:
		return 4, func(x []byte) float64 { return float64(int32(be.Uint32(x))) }
	case 75:
		return 8, func(x []byte) float64 { return float64(int64(be.Uint64(x))) }
	case 77:
		return 2, func(x []byte) float64 { return float64(int16(le.Uint16(x))) }
	case 78:
		return 4, func(x []byte) float64 { return float64(int32(le.Uint32(x))) }
	case 79:
		return 8, func(x []byte) float64 { return float64(int64(le.Uint64(x))) }
	case 81:
		return 4, func(x []byte) float64 { return float64(math.Float32frombits(be.Uint32(x))) }
	case 82:
		return 8, func(x []byte) float64 { return math.Float64frombits(be.Uint64(x)) }
	case 85:
		return 4, func(x []byte) float64 { return float64(math.Float32frombits(le.Uint32(x))) }
	case 86:
		return 8, func(x []byte) float64 { return math.Float64frombits(le.Uint64(x)) }
	}
	return 0, nil
}

func halfToFloat(h uint16) float32 {
	sign := uint32(h>>15) << 31
	exp := uint32(h>>10) & 0x1f
	frac := uint32(h & 0x3ff)
	switch exp {
	case 0:
		f := float32(frac) / 1024 * float32(math.Pow(2, -14))
		if sign != 0 {
			return -f
		}
		return f
	case 31:
		if frac == 0 {
			return math.Float32frombits(sign | 0x7f800000)
		}
		return float32(math.NaN())
	}
	return math.Float32frombits(sign | (exp+112)<<23 | frac<<13)
}
