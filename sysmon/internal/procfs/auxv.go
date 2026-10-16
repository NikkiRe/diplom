package procfs

import (
	"encoding/binary"
	"errors"
	"strconv"
)

// Типы записей вспомогательного вектора ELF (linux/auxvec.h).
const (
	AtNull   = 0
	AtPageSz = 6  // размер страницы
	AtClkTck = 17 // частота тиков times(2) и /proc/<pid>/stat (USER_HZ)
)

// ParseAuxv разбирает /proc/self/auxv: пары машинных слов (тип, значение) в порядке байтов платформы.
// Нужен, потому что CLK_TCK без cgo (sysconf) иначе не узнать; на всех актуальных платформах он 100,
// но на ядрах с иным USER_HZ ошибка в CPU-секундах была бы кратной.
func ParseAuxv(data []byte, wordSize int, order binary.ByteOrder) (map[uint64]uint64, error) {
	if wordSize != 4 && wordSize != 8 {
		return nil, errors.New("auxv: размер слова должен быть 4 или 8")
	}
	if len(data)%(2*wordSize) != 0 {
		return nil, errors.New("auxv: длина не кратна паре слов")
	}
	word := func(b []byte) uint64 {
		if wordSize == 8 {
			return order.Uint64(b)
		}
		return uint64(order.Uint32(b))
	}
	out := map[uint64]uint64{}
	for i := 0; i+2*wordSize <= len(data); i += 2 * wordSize {
		typ := word(data[i:])
		if typ == AtNull {
			break
		}
		out[typ] = word(data[i+wordSize:])
	}
	return out, nil
}

// ReadAuxv читает /proc/self/auxv текущего процесса (корень FS должен указывать на настоящий /proc).
func (f FS) ReadAuxv() (map[uint64]uint64, error) {
	data, err := f.ReadFile("self", "auxv")
	if err != nil {
		return nil, err
	}
	return ParseAuxv(data, strconv.IntSize/8, binary.NativeEndian)
}
