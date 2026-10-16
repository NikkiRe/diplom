package thermal

import (
	"path/filepath"
	"testing"

	"github.com/local/sysmon/internal/testfs"
)

func TestRead(t *testing.T) {
	root := t.TempDir()
	z := func(n, typ, temp string) {
		dir := filepath.Join(root, "class", "thermal", "thermal_zone"+n)
		if typ != "" {
			testfs.WriteFile(t, filepath.Join(dir, "type"), typ+"\n")
		}
		testfs.WriteFile(t, filepath.Join(dir, "temp"), temp+"\n")
	}
	z("0", "cpu-thermal", "48312")
	z("1", "cpu-thermal", "-1500") // повтор имени и отрицательная температура
	z("2", "", "30000")            // без type — имя каталога
	z("3", "broken", "N/A")        // нечитаемое значение пропускается
	got := Read(root)
	want := []Zone{{"cpu-thermal", 48.312}, {"cpu-thermal/1", -1.5}, {"thermal_zone2", 30}}
	if len(got) != len(want) {
		t.Fatalf("got %+v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("zone %d: got %+v want %+v", i, got[i], want[i])
		}
	}
	if zones := Read(filepath.Join(root, "nope")); zones != nil {
		t.Fatalf("нет каталога: %v", zones)
	}
}
