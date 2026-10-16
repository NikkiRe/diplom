package procfs

import (
	"bytes"
	"strconv"
)

// NetDev — счётчики интерфейса из /proc/net/dev (с момента поднятия интерфейса).
type NetDev struct {
	RxBytes, RxPackets, RxErrs, RxDrop uint64
	TxBytes, TxPackets, TxErrs, TxDrop uint64
}

// ParseNetDev разбирает /proc/net/dev: две строки заголовка, затем "iface: 16 чисел".
// Имя интерфейса может вплотную примыкать к числу ("eth0:123"), поэтому делим по ':'.
func ParseNetDev(data []byte) (map[string]NetDev, error) {
	out := map[string]NetDev{}
	lines := bytes.Split(data, []byte{'\n'})
	for _, line := range lines {
		colon := bytes.IndexByte(line, ':')
		if colon < 0 {
			continue
		}
		name := string(bytes.TrimSpace(line[:colon]))
		if name == "" || bytes.ContainsAny([]byte(name), "|") {
			continue // строки заголовка содержат '|'
		}
		fields := bytes.Fields(line[colon+1:])
		if len(fields) < 16 {
			continue
		}
		var v [16]uint64
		ok := true
		for i := 0; i < 16; i++ {
			n, err := strconv.ParseUint(string(fields[i]), 10, 64)
			if err != nil {
				ok = false
				break
			}
			v[i] = n
		}
		if !ok {
			continue
		}
		out[name] = NetDev{
			RxBytes: v[0], RxPackets: v[1], RxErrs: v[2], RxDrop: v[3],
			TxBytes: v[8], TxPackets: v[9], TxErrs: v[10], TxDrop: v[11],
		}
	}
	return out, nil
}

// ReadNetDev читает счётчики сетевого пространства имён процесса pid (/proc/<pid>/net/dev);
// pid ≤ 0 — пространство имён самого сэмплера (/proc/net/dev). Для агента в своём netns (§12.3
// методики) это даёт трафик именно его интерфейсов.
func (f FS) ReadNetDev(pid int) (map[string]NetDev, error) {
	var data []byte
	var err error
	if pid > 0 {
		data, err = f.ReadFile(strconv.Itoa(pid), "net", "dev")
	} else {
		data, err = f.ReadFile("net", "dev")
	}
	if err != nil {
		return nil, err
	}
	return ParseNetDev(data)
}
