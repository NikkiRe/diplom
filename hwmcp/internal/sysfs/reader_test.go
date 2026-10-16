package sysfs

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/sys/unix"

	"github.com/local/hwmcp/internal/testutil"
)

// sameRead сравнивает Reader с Channel.Read (эталон — порт прототипа): значения и способ получения
// совпадают, причина неудачи у Reader уточняет причину прототипа.
func sameRead(t *testing.T, label string, c *Channel, r *Reader) {
	t.Helper()
	want, wantHow, _ := c.Read()
	got, how, has := r.Read()
	if has != (want != nil) || !refines(how, wantHow) || (has && got != *want) {
		t.Errorf("%s %s: Reader (%v, %q, %v), Channel.Read (%v, %q)", label, c.Path, got, how, has, deref(want), wantHow)
	}
}

// refines: "absent" прототипа — любая неудача чтения; Reader называет её точнее.
func refines(reader, proto string) bool {
	if proto == ReasonAbsent {
		return reader == ReasonAbsent || reader == ReasonNotANumber || reader == ReasonUnreadable
	}
	return reader == proto
}

// Reader совпадает с Channel.Read на всех каналах фикстуры: и с удерживаемым дескриптором, и без,
// и после изменения файлов на месте.
func TestReaderMatchesChannelRead(t *testing.T) {
	root := testutil.FixtureCopy(t)
	devs := Probe(root, nil)
	n := 0
	for _, keep := range []bool{false, true} {
		for _, d := range devs {
			for i := range d.Channels {
				c := &d.Channels[i]
				r := c.NewReader(keep)
				if r == nil {
					continue
				}
				n++
				sameRead(t, "initial", c, r)
				sameRead(t, "repeat", c, r) // второй раз — через pread удерживаемого дескриптора
				// значение меняется на месте (как пишет драйвер фикстуры и как sysfs формирует его заново)
				if err := os.WriteFile(filepath.Join(root, c.Path), []byte("12345\n"), 0o644); err != nil {
					t.Fatal(err)
				}
				sameRead(t, "rewritten", c, r)
				if err := os.WriteFile(filepath.Join(root, c.Path), []byte("  7\x00\n"), 0o644); err != nil {
					t.Fatal(err)
				}
				sameRead(t, "nul", c, r)
				if err := os.WriteFile(filepath.Join(root, c.Path), []byte("garbage\n"), 0o644); err != nil {
					t.Fatal(err)
				}
				sameRead(t, "garbage", c, r)
				r.Close()
				if _, how, has := r.Read(); has || how != "closed" {
					t.Errorf("read after Close: %v %q", has, how)
				}
			}
		}
		root = testutil.FixtureCopy(t)
		devs = Probe(root, nil)
	}
	if n < 16 {
		t.Fatalf("only %d sysfs channels checked", n)
	}
}

// Эталонные случаи IIO (scale/offset на канал и на тип, raw без scale, мусор, NUL) — через Reader.
func TestReaderIIOCases(t *testing.T) {
	raw, err := os.ReadFile(testutil.Testdata("golden", "iio_cases.json"))
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Files map[string]string `json:"files"`
		Base  string            `json:"base"`
		Typ   string            `json:"typ"`
		Value *float64          `json:"value"`
		How   string            `json:"how"`
	}
	if err := json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	for i, c := range cases {
		for _, keep := range []bool{false, true} {
			dir := filepath.Join(t.TempDir(), "iio")
			if err := os.MkdirAll(dir, 0o755); err != nil {
				t.Fatal(err)
			}
			for name, text := range c.Files {
				testutil.WriteFile(t, filepath.Join(dir, name), text)
			}
			ch := Channel{src: source{kind: "iio", dir: dir, base: c.Base, typ: c.Typ, k: 1}}
			r := ch.NewReader(keep)
			for rep := 0; rep < 2; rep++ {
				v, how, has := r.Read()
				var want *float64
				if c.Value != nil {
					w := round6(*c.Value)
					want = &w
				}
				if !refines(how, c.How) || has != (want != nil) || (has && v != *want) {
					t.Errorf("case %d keep=%v rep=%d (%v): got (%v, %s), want (%v, %s)", i, keep, rep, c.Files, v, how,
						deref(want), c.How)
				}
			}
			r.Close()
		}
	}
}

// Причина неудачи: атрибута нет (устройство пропало) — absent, атрибут не читается — unreadable,
// мусор — not_a_number; у IIO причина берётся от того атрибута, который есть.
func TestReaderFailureReasons(t *testing.T) {
	for _, c := range []struct {
		name      string
		files     map[string]string // "" — каталог вместо атрибута: open удаётся, pread — EISDIR
		file, iio string
	}{
		{"nothing", nil, ReasonAbsent, ReasonAbsent},
		{"garbage", map[string]string{"temp1_input": "x\n", "in_temp_raw": "bad\n"}, ReasonNotANumber, ReasonNotANumber},
		{"raw garbage only", map[string]string{"in_temp_raw": "bad\n"}, ReasonAbsent, ReasonNotANumber},
		{"unreadable", map[string]string{"temp1_input": "", "in_temp_input": "", "in_temp_raw": "bad\n"},
			ReasonUnreadable, ReasonUnreadable},
	} {
		for _, keep := range []bool{false, true} {
			dir := t.TempDir()
			for name, text := range c.files {
				if text == "" {
					if err := os.Mkdir(filepath.Join(dir, name), 0o755); err != nil {
						t.Fatal(err)
					}
				} else {
					testutil.WriteFile(t, filepath.Join(dir, name), text)
				}
			}
			file := Channel{How: "input", src: source{kind: "file", file: filepath.Join(dir, "temp1_input"), k: 1e-3}}
			iio := Channel{src: source{kind: "iio", dir: dir, base: "in_temp", typ: "temp", k: 1e-3}}
			for _, rc := range []struct {
				ch   *Channel
				want string
			}{{&file, c.file}, {&iio, c.iio}} {
				r := rc.ch.NewReader(keep)
				if _, how, ok := r.Read(); ok || how != rc.want {
					t.Errorf("%s keep=%v %s: %q %v, want %q", c.name, keep, rc.ch.src.kind, how, ok, rc.want)
				}
				r.Close()
			}
		}
	}
}

// С KeepOpen пропавшее устройство (Read даёт absent) «потеряно»: у источника с переменной нумерацией
// читатель не открывает старый путь сам (там может оказаться чужое устройство) и ждёт Retarget, у
// 1-Wire — привязывается заново по пути. Отсутствие необязательного атрибута запоминается до привязки.
func TestReaderKeepOpenRebinding(t *testing.T) {
	root := t.TempDir()
	numbered := Channel{src: source{kind: "iio", dir: filepath.Join(root, "iio:device0"), base: "in_voltage0",
		typ: "voltage", k: 1e-3, renumbered: true}}
	serial := Channel{How: "temperature", src: source{kind: "file", file: filepath.Join(root, "28-01", "temperature"),
		k: 1e-3}}
	nr, sr := numbered.NewReader(true), serial.NewReader(true)
	defer nr.Close()
	defer sr.Close()
	for _, r := range []*Reader{nr, sr} {
		if _, how, ok := r.Read(); ok || how != ReasonAbsent {
			t.Fatalf("missing device: %q %v", how, ok)
		}
	}
	testutil.WriteFile(t, filepath.Join(root, "iio:device0", "in_voltage0_raw"), "100\n")
	testutil.WriteFile(t, filepath.Join(root, "iio:device0", "in_voltage0_scale"), "2\n")
	testutil.WriteFile(t, filepath.Join(root, "28-01", "temperature"), "21500\n")
	if v, _, ok := sr.Read(); !ok || v != 21.5 {
		t.Fatalf("1-Wire must rebind by its serial path: %v %v", v, ok)
	}
	if _, how, ok := nr.Read(); ok || how != ReasonAbsent {
		t.Fatalf("renumbered device must wait for Retarget: %q %v", how, ok)
	}
	if !nr.Retarget(&numbered) {
		t.Fatal("Retarget of a lost reader to the same path must rebind")
	}
	if v, _, ok := nr.Read(); !ok || v != 0.2 {
		t.Fatalf("after Retarget: %v %v", v, ok)
	}
	if nr.Retarget(&numbered) {
		t.Fatal("bound reader on the same path: nothing to do")
	}
	// offset появился после привязки: набор атрибутов привязанного устройства не перечитывается
	testutil.WriteFile(t, filepath.Join(root, "iio:device0", "in_voltage0_offset"), "50\n")
	if v, _, _ := nr.Read(); v != 0.2 {
		t.Fatalf("absent offset must stay remembered until rebinding: %v", v)
	}
}

// Читатель закрывает только свои дескрипторы (регрессия: новый читатель закрывал дескриптор 0 —
// stdin транспорта stdio).
func TestReaderClosesOnlyOwnDescriptors(t *testing.T) {
	c := Channel{How: "input", src: source{kind: "file", file: filepath.Join(t.TempDir(), "absent"), k: 1}}
	for _, keep := range []bool{false, true} {
		r := c.NewReader(keep)
		r.Read()
		r.Close()
	}
	if _, err := unix.FcntlInt(0, unix.F_GETFD, 0); err != nil {
		t.Fatalf("descriptor 0 was closed: %v", err)
	}
}

// Устройство вернулось под другим номером узла: Retarget переводит читатель на новый путь.
func TestReaderRetarget(t *testing.T) {
	root := t.TempDir()
	at := func(n string) *Channel {
		return &Channel{src: source{kind: "iio", dir: filepath.Join(root, n), base: "in_temp", typ: "temp", k: 1e-3}}
	}
	testutil.WriteFile(t, filepath.Join(root, "iio:device0", "in_temp_input"), "21000\n")
	r := at("iio:device0").NewReader(false) // удерживаемый дескриптор на обычной ФС читал бы удалённый inode
	if v, _, ok := r.Read(); !ok || v != 21 {
		t.Fatalf("%v %v", v, ok)
	}
	if err := os.RemoveAll(filepath.Join(root, "iio:device0")); err != nil {
		t.Fatal(err)
	}
	if _, how, ok := r.Read(); ok || how != ReasonAbsent {
		t.Fatalf("removed device: %q %v", how, ok)
	}
	testutil.WriteFile(t, filepath.Join(root, "iio:device3", "in_temp_input"), "23000\n")
	if r.Retarget(at("iio:device0")) || !r.Retarget(at("iio:device3")) || r.Retarget(at("iio:device3")) {
		t.Fatal("Retarget must report only a changed path")
	}
	if v, _, ok := r.Read(); !ok || v != 23 {
		t.Fatalf("after retarget: %v %v", v, ok)
	}
	r.Close()
	if r.Retarget(at("iio:device0")) {
		t.Fatal("closed reader must not be retargeted")
	}
}

func BenchmarkChannelRead(b *testing.B) {
	root := testutil.FixtureCopy(b)
	var chans []*Channel
	for _, d := range Probe(root, nil) {
		for i := range d.Channels {
			if d.Channels[i].NewReader(false) != nil {
				chans = append(chans, &d.Channels[i])
			}
		}
	}
	b.Run("Channel.Read", func(b *testing.B) {
		b.ReportAllocs()
		for i := 0; i < b.N; i++ {
			for _, c := range chans {
				c.Read()
			}
		}
	})
	for _, keep := range []bool{false, true} {
		rs := make([]*Reader, len(chans))
		for i, c := range chans {
			rs[i] = c.NewReader(keep)
		}
		name := "Reader"
		if keep {
			name = "Reader.keepOpen"
		}
		b.Run(name, func(b *testing.B) {
			b.ReportAllocs()
			for i := 0; i < b.N; i++ {
				for _, r := range rs {
					r.Read()
				}
			}
		})
		for _, r := range rs {
			r.Close()
		}
	}
}
