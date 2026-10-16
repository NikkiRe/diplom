package procfs

import (
	"strconv"
)

// Smaps — сводка памяти процесса в КиБ из smaps_rollup (ядро ≥ 4.14) или суммы по smaps.
//
// PSS (proportional set size) делит каждую разделяемую страницу поровну между процессами, которые её
// отображают, поэтому сумма PSS по процессам не считает общие библиотеки дважды — это основная метрика
// памяти в методике (§12.2 THESIS_CONCEPT). RSS считает разделяемые страницы в каждом процессе.
type Smaps struct {
	RssKiB      uint64
	PssKiB      uint64
	PssAnonKiB  uint64
	PssFileKiB  uint64
	PssShmemKiB uint64
	SwapKiB     uint64
	SwapPssKiB  uint64
	// HasPssBreakdown — в источнике есть Pss_Anon/Pss_File/Pss_Shmem (smaps_rollup, ядро ≥ 5.10;
	// в построчном smaps их нет).
	HasPssBreakdown bool
	// Source — "smaps_rollup" или "smaps" (откат для старых ядер, например 4.9 у Jetson Nano).
	Source string
}

// ParseSmaps суммирует поля по всем строкам: для smaps_rollup каждое поле встречается один раз,
// для smaps — по разу на каждую область отображения (VMA). Поэтому один разборщик подходит для обоих.
func ParseSmaps(data []byte) Smaps {
	var s Smaps
	forEachKV(data, func(key, value []byte) {
		var dst *uint64
		switch string(key) {
		case "Rss":
			dst = &s.RssKiB
		case "Pss":
			dst = &s.PssKiB
		case "Pss_Anon":
			dst = &s.PssAnonKiB
			s.HasPssBreakdown = true
		case "Pss_File":
			dst = &s.PssFileKiB
			s.HasPssBreakdown = true
		case "Pss_Shmem":
			dst = &s.PssShmemKiB
			s.HasPssBreakdown = true
		case "Swap":
			dst = &s.SwapKiB
		case "SwapPss":
			dst = &s.SwapPssKiB
		default:
			return
		}
		if v, err := parseUint(value); err == nil {
			*dst += v
		}
	})
	return s
}

// ReadSmaps читает smaps_rollup, а если его нет (ядро < 4.14) — построчный smaps. Построчный файл
// заметно дороже: ядро обходит все VMA и форматирует несколько килобайт текста на каждую.
func (f FS) ReadSmaps(pid int) (Smaps, error) {
	p := strconv.Itoa(pid)
	data, err := f.ReadFile(p, "smaps_rollup")
	if err == nil {
		s := ParseSmaps(data)
		s.Source = "smaps_rollup"
		return s, nil
	}
	if !IsGone(err) {
		return Smaps{}, err
	}
	// ENOENT: либо процесс завершился, либо у ядра нет smaps_rollup. Различаем по наличию smaps.
	data, err2 := f.ReadFile(p, "smaps")
	if err2 != nil {
		return Smaps{}, err2
	}
	s := ParseSmaps(data)
	s.Source = "smaps"
	return s, nil
}
