package procfs

import (
	"encoding/binary"
	"errors"
	"io/fs"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"testing"

	"github.com/local/sysmon/internal/testfs"
)

// realRoot — снимок /proc с Ubuntu 24.04 arm64 (ядро 6.8) в Lima VM: bash (4540) → python3 (4542),
// sleep (4543); python3 держит 24 МиБ bytearray. Снят scripts/capture_fixture.sh.
var realRoot = filepath.Join("..", "..", "testdata", "real_ubuntu2404_arm64", "proc")

func TestParseStatReal(t *testing.T) {
	st, err := New(realRoot).ReadStat(4542)
	if err != nil {
		t.Fatal(err)
	}
	want := Stat{PID: 4542, Comm: "python3", State: 'S', PPID: 4540, MinFlt: 7253, MajFlt: 0,
		UTime: 0, STime: 0, CUTime: 0, CSTime: 0, NumThreads: 1, StartTime: 36600, VSize: 43220992, RSSPages: 8448}
	if st != want {
		t.Fatalf("got %+v\nwant %+v", st, want)
	}
}

func TestParseStatCommWithSpacesAndParens(t *testing.T) {
	// comm может содержать пробелы и ')' — граница определяется по последней ')'.
	line := testfs.StatLine(testfs.Proc{PID: 77, PPID: 1, Comm: "my) evil (proc", UTime: 250, STime: 50,
		CUTime: 7, CSTime: 3, StartTime: 999, VSizeBytes: 1 << 20, RSSPages: 10})
	st, err := ParseStat([]byte(line))
	if err != nil {
		t.Fatal(err)
	}
	if st.Comm != "my) evil (proc" || st.PPID != 1 || st.UTime != 250 || st.STime != 50 ||
		st.CUTime != 7 || st.CSTime != 3 || st.StartTime != 999 || st.RSSPages != 10 {
		t.Fatalf("неверный разбор: %+v", st)
	}
}

func TestParseStatErrors(t *testing.T) {
	for _, bad := range []string{"", "123", "1 (x) S 0", "abc (x) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22"} {
		if _, err := ParseStat([]byte(bad)); err == nil {
			t.Errorf("ожидалась ошибка для %q", bad)
		}
	}
}

func TestParseStatusReal(t *testing.T) {
	st, err := New(realRoot).ReadStatus(4542)
	if err != nil {
		t.Fatal(err)
	}
	if st.Name != "python3" || st.VmRSSKiB != 33932 || st.VmHWMKiB != 33932 || st.Threads != 1 ||
		st.VolCtxsw != 1 || st.NonvolCtxsw != 0 || !st.HasCtxsw {
		t.Fatalf("got %+v", st)
	}
}

func TestSumTaskCtxsw(t *testing.T) {
	root := t.TempDir()
	testfs.WriteProcs(t, root, []testfs.Proc{{PID: 10, PPID: 1, Comm: "edgectx",
		Tasks: []testfs.Task{{TID: 10, Vol: 5, Nonvol: 1}, {TID: 11, Vol: 100, Nonvol: 7}, {TID: 12, Vol: 40, Nonvol: 2}}}})
	vol, nonvol, n, err := New(root).SumTaskCtxsw(10)
	if err != nil || vol != 145 || nonvol != 10 || n != 3 {
		t.Fatalf("vol=%d nonvol=%d n=%d err=%v", vol, nonvol, n, err)
	}
	// Главный поток в /proc/<pid>/status видит только свои переключения.
	st, _ := New(root).ReadStatus(10)
	if st.VolCtxsw != 5 {
		t.Fatalf("status главного потока: %d", st.VolCtxsw)
	}
}

func TestParseIOReal(t *testing.T) {
	io, err := New(realRoot).ReadIO(4542)
	if err != nil {
		t.Fatal(err)
	}
	if io != (IO{RChar: 314229, SyscR: 51}) {
		t.Fatalf("got %+v", io)
	}
}

func TestSmapsRollupMatchesSmapsSum(t *testing.T) {
	// На реальном снимке сумма построчного smaps должна совпасть со smaps_rollup (процесс спал между
	// чтениями) — это проверяет откат для ядер без smaps_rollup (Jetson Nano, 4.9).
	f := New(realRoot)
	for _, pid := range []int{4540, 4542, 4543} {
		rollup, err := f.ReadSmaps(pid)
		if err != nil {
			t.Fatal(err)
		}
		if rollup.Source != "smaps_rollup" || !rollup.HasPssBreakdown {
			t.Fatalf("pid %d: источник %q, breakdown %v", pid, rollup.Source, rollup.HasPssBreakdown)
		}
		data, err := f.ReadFile(strconv.Itoa(pid), "smaps")
		if err != nil {
			t.Fatal(err)
		}
		sum := ParseSmaps(data)
		if sum.RssKiB != rollup.RssKiB || sum.SwapKiB != rollup.SwapKiB {
			t.Errorf("pid %d: smaps Rss=%d rollup Rss=%d", pid, sum.RssKiB, rollup.RssKiB)
		}
		// PSS построчно округляется в каждой VMA, поэтому допускаем расхождение в пределах числа VMA.
		if diff := int64(sum.PssKiB) - int64(rollup.PssKiB); diff < -64 || diff > 64 {
			t.Errorf("pid %d: smaps Pss=%d rollup Pss=%d", pid, sum.PssKiB, rollup.PssKiB)
		}
	}
	py, _ := f.ReadSmaps(4542)
	want := Smaps{RssKiB: 33932, PssKiB: 29604, PssAnonKiB: 28224, PssFileKiB: 1380, HasPssBreakdown: true, Source: "smaps_rollup"}
	if py != want {
		t.Fatalf("got %+v", py)
	}
}

func TestReadSmapsFallbackWithoutRollup(t *testing.T) {
	root := t.TempDir()
	testfs.WriteProcs(t, root, []testfs.Proc{{PID: 5, PPID: 1, Comm: "old-kernel",
		Smaps: []VMA{{Rss: 100, Pss: 50, Swap: 4, SwapPss: 2}, {Rss: 20, Pss: 20}}}})
	s, err := New(root).ReadSmaps(5)
	if err != nil {
		t.Fatal(err)
	}
	if s.Source != "smaps" || s.RssKiB != 120 || s.PssKiB != 70 || s.SwapKiB != 4 || s.SwapPssKiB != 2 || s.HasPssBreakdown {
		t.Fatalf("got %+v", s)
	}
}

// VMA — псевдоним, чтобы тест читался без префикса пакета testfs.
type VMA = testfs.VMA

func TestReadSmapsGoneAndPermission(t *testing.T) {
	root := t.TempDir()
	if _, err := New(root).ReadSmaps(424242); !IsGone(err) {
		t.Fatalf("несуществующий процесс: %v", err)
	}
	testfs.WriteProcs(t, root, []testfs.Proc{{PID: 6, PPID: 1, Comm: "other-user",
		Rollup: &testfs.Rollup{Rss: 1, Pss: 1}, Unreadable: []string{"smaps_rollup"}}})
	_, err := New(root).ReadSmaps(6)
	if !IsPermission(err) {
		t.Skipf("права не проверяются (запуск от root?): %v", err)
	}
}

func TestParseNetDevReal(t *testing.T) {
	devs, err := New(realRoot).ReadNetDev(4540)
	if err != nil {
		t.Fatal(err)
	}
	if len(devs) != 2 {
		t.Fatalf("интерфейсы: %v", devs)
	}
	if devs["eth0"] != (NetDev{RxBytes: 42501979, RxPackets: 38043, TxBytes: 648805, TxPackets: 8944}) {
		t.Fatalf("eth0: %+v", devs["eth0"])
	}
	if devs["lo"].RxBytes != 19718 {
		t.Fatalf("lo: %+v", devs["lo"])
	}
	self, err := New(realRoot).ReadNetDev(0)
	if err != nil || len(self) != 2 {
		t.Fatalf("/proc/net/dev: %v %v", self, err)
	}
}

func TestParseNetDevGluedName(t *testing.T) {
	// При больших счётчиках ядро не ставит пробел после ':'.
	data := "h1\nh2|x\n  eth0:12345678901 5 0 0 0 0 0 0 99 3 0 0 0 0 0 0\n"
	devs, _ := ParseNetDev([]byte(data))
	if devs["eth0"].RxBytes != 12345678901 || devs["eth0"].TxBytes != 99 {
		t.Fatalf("got %+v", devs)
	}
}

func TestSystemFilesReal(t *testing.T) {
	f := New(realRoot)
	data, _ := f.ReadFile("stat")
	cpu, err := ParseCPUStat(data)
	if err != nil {
		t.Fatal(err)
	}
	if cpu != (CPUTimes{User: 1522, Nice: 144, System: 587, Idle: 144767, IOWait: 77, SoftIRQ: 61}) {
		t.Fatalf("cpu: %+v", cpu)
	}
	if cpu.Total() != cpu.Busy()+144767+77 {
		t.Fatal("Total != Busy + idle + iowait")
	}
	data, _ = f.ReadFile("meminfo")
	m := ParseMeminfo(data)
	if m["MemTotal"] != 3999212 || m["MemAvailable"] != 3537308 {
		t.Fatalf("meminfo: %v %v", m["MemTotal"], m["MemAvailable"])
	}
	data, _ = f.ReadFile("loadavg")
	la, err := ParseLoadavg(data)
	if err != nil || la != (Loadavg{0.08, 0.05, 0.01}) {
		t.Fatalf("loadavg: %+v %v", la, err)
	}
	if _, err := ParseCPUStat([]byte("intr 1 2 3\n")); err == nil {
		t.Fatal("нет строки cpu — должна быть ошибка")
	}
}

func TestParseAuxv(t *testing.T) {
	var buf []byte
	put := func(k, v uint64) {
		buf = binary.LittleEndian.AppendUint64(buf, k)
		buf = binary.LittleEndian.AppendUint64(buf, v)
	}
	put(AtPageSz, 16384)
	put(AtClkTck, 100)
	put(AtNull, 0)
	put(99, 1) // после AT_NULL не читается
	aux, err := ParseAuxv(buf, 8, binary.LittleEndian)
	if err != nil {
		t.Fatal(err)
	}
	if aux[AtClkTck] != 100 || aux[AtPageSz] != 16384 || len(aux) != 2 {
		t.Fatalf("auxv: %v", aux)
	}
	if _, err := ParseAuxv(buf[:5], 8, binary.LittleEndian); err == nil {
		t.Fatal("обрезанный auxv должен дать ошибку")
	}
	var buf32 []byte
	buf32 = binary.LittleEndian.AppendUint32(buf32, AtClkTck)
	buf32 = binary.LittleEndian.AppendUint32(buf32, 250)
	aux, _ = ParseAuxv(buf32, 4, binary.LittleEndian)
	if aux[AtClkTck] != 250 {
		t.Fatalf("32-битный auxv: %v", aux)
	}
}

func TestListPIDsAndTasks(t *testing.T) {
	pids, err := New(realRoot).ListPIDs()
	if err != nil {
		t.Fatal(err)
	}
	sort.Ints(pids)
	if !reflect.DeepEqual(pids, []int{4540, 4542, 4543}) {
		t.Fatalf("pids: %v", pids) // net, stat, meminfo ... — не PID
	}
	tids, err := New(realRoot).ListTasks(4542)
	if err != nil || !reflect.DeepEqual(tids, []int{4542}) {
		t.Fatalf("tasks: %v %v", tids, err)
	}
	if _, err := New(t.TempDir()).ListTasks(1); !errors.Is(err, fs.ErrNotExist) {
		t.Fatalf("ожидалось ErrNotExist: %v", err)
	}
}
