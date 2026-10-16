package selfstat

import (
	"runtime"
	"testing"
)

func TestRead(t *testing.T) {
	// Немного работы, чтобы время CPU было ненулевым.
	x := 0
	for i := 0; i < 20_000_000; i++ {
		x += i % 7
	}
	_ = x
	u, err := Read()
	if err != nil {
		t.Fatal(err)
	}
	if u.CPUS() <= 0 || u.MaxRSSKiB == 0 {
		t.Fatalf("usage: %+v", u)
	}
	// Пик RSS тестового процесса Go — мегабайты, а не гигабайты: ловит ошибку единиц (байты/КиБ на macOS).
	if u.MaxRSSKiB < 1024 || u.MaxRSSKiB > 4<<20 {
		t.Fatalf("max_rss_kib вне правдоподобного диапазона: %d", u.MaxRSSKiB)
	}
	if runtime.GOOS == "linux" && (u.PSSKiB == 0 || u.RSSKiB < u.PSSKiB) {
		t.Fatalf("linux: rss=%d pss=%d", u.RSSKiB, u.PSSKiB)
	}
}
