package pstool

import (
	"math"
	"testing"
)

func TestParseCPUTime(t *testing.T) {
	cases := map[string]float64{
		"0:00.00":     0,
		"0:01.50":     1.5,
		"316:52.43":   316*60 + 52.43, // macOS: минуты не переносятся в часы
		"01:02:03":    3723,
		"2-01:00:00":  2*86400 + 3600,
		"  0:00.12  ": 0.12,
	}
	for in, want := range cases {
		got, err := ParseCPUTime(in)
		if err != nil || math.Abs(got-want) > 1e-9 {
			t.Errorf("%q: got %v %v want %v", in, got, err, want)
		}
	}
	for _, bad := range []string{"", "-", "1:2:3:4", "a:b", "-1:00"} {
		if _, err := ParseCPUTime(bad); err == nil {
			t.Errorf("%q: ожидалась ошибка", bad)
		}
	}
}

func TestParsePS(t *testing.T) {
	out := "    1     0  13648 493389808  62:17.65 103:32.04 /sbin/launchd\n" +
		"77127 77124  71040 540032832  50:12.44  74:57.88 /Applications/Claude.app/Contents/MacOS/Claude Helper\n" +
		"  555     1      -         -         -         - (zombie)\n" +
		"\n"
	procs, skipped := ParsePS([]byte(out))
	if skipped != 1 || len(procs) != 2 {
		t.Fatalf("procs=%d skipped=%d", len(procs), skipped)
	}
	p := procs[1]
	if p.PID != 77127 || p.PPID != 77124 || p.RSSKiB != 71040 || p.VSZKiB != 540032832 ||
		p.Comm != "/Applications/Claude.app/Contents/MacOS/Claude Helper" ||
		math.Abs(p.UserS-(50*60+12.44)) > 1e-9 || math.Abs(p.CPUS-(74*60+57.88)) > 1e-9 {
		t.Fatalf("got %+v", p)
	}
}

func TestParseNetstat(t *testing.T) {
	out := `Name       Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll
lo0        16384 <Link#1>                      10117692     0 20666091016 10117692     0 20666091016     0
lo0        16384 127           127.0.0.1       10117692     - 20666091016 10117692     - 20666091016     -
gif0*      1280  <Link#2>                             0     0          0        0     0          0     0
en0        1500  <Link#11>   aa:bb:cc:dd:ee:ff  21402583     3 22922555354  6968232     1 1693141394     0
en0        1500  192.168.1     192.168.1.5     21402583     - 22922555354  6968232     - 1693141394     -
`
	got := ParseNetstat([]byte(out))
	if len(got) != 3 {
		t.Fatalf("интерфейсы: %v", got)
	}
	if got["en0"] != (NetIf{RxBytes: 22922555354, RxPackets: 21402583, RxErrs: 3, TxBytes: 1693141394, TxPackets: 6968232, TxErrs: 1}) {
		t.Fatalf("en0: %+v", got["en0"])
	}
	if got["lo0"].RxBytes != 20666091016 {
		t.Fatalf("lo0 (пустой Address): %+v", got["lo0"])
	}
	if _, ok := got["gif0"]; !ok {
		t.Fatal("звёздочка у выключенного интерфейса не убрана")
	}
}
