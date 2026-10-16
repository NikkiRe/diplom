// Package testutil — общие помощники тестов: пути к testdata, сравнение JSON по правилам JSON
// (числа по величине: 101325 == 101325.0), копирование фикстуры sysfs во временный каталог.
package testutil

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"testing"
)

// ModuleRoot — корень модуля hwmcp (каталог с go.mod).
func ModuleRoot() string {
	_, file, _, _ := runtime.Caller(0)
	return filepath.Clean(filepath.Join(filepath.Dir(file), "..", ".."))
}

// Testdata — путь внутри hwmcp/testdata.
func Testdata(parts ...string) string {
	return filepath.Join(append([]string{ModuleRoot(), "testdata"}, parts...)...)
}

// RepoRoot — корень репозитория ВКР (родитель hwmcp).
func RepoRoot() string { return filepath.Dir(ModuleRoot()) }

// DecodeJSON разбирает JSON с сохранением чисел как json.Number.
func DecodeJSON(b []byte) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, fmt.Errorf("trailing data after JSON value")
	}
	return v, nil
}

// MustDecode — DecodeJSON или t.Fatal.
func MustDecode(t testing.TB, b []byte) any {
	t.Helper()
	v, err := DecodeJSON(b)
	if err != nil {
		t.Fatalf("decode JSON: %v\n%s", err, b)
	}
	return v
}

// Normalize переводит значение Go в обобщённое дерево JSON (через сериализацию).
func Normalize(t testing.TB, v any) any {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return MustDecode(t, b)
}

// Diff возвращает первое расхождение двух JSON-деревьев ("" — равны). Числа сравниваются по величине.
func Diff(a, b any) string { return diff("$", a, b) }

func diff(path string, a, b any) string {
	switch x := a.(type) {
	case nil:
		if b != nil {
			return fmt.Sprintf("%s: null vs %v", path, b)
		}
		return ""
	case bool:
		y, ok := b.(bool)
		if !ok || x != y {
			return fmt.Sprintf("%s: %v vs %v", path, a, b)
		}
		return ""
	case string:
		y, ok := b.(string)
		if !ok || x != y {
			return fmt.Sprintf("%s: %q vs %v", path, x, b)
		}
		return ""
	case []any:
		y, ok := b.([]any)
		if !ok {
			return fmt.Sprintf("%s: array vs %T", path, b)
		}
		if len(x) != len(y) {
			return fmt.Sprintf("%s: len %d vs %d", path, len(x), len(y))
		}
		for i := range x {
			if d := diff(fmt.Sprintf("%s[%d]", path, i), x[i], y[i]); d != "" {
				return d
			}
		}
		return ""
	case map[string]any:
		y, ok := b.(map[string]any)
		if !ok {
			return fmt.Sprintf("%s: object vs %T", path, b)
		}
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		for k := range y {
			if _, ok := x[k]; !ok {
				return fmt.Sprintf("%s: key %q only on the right", path, k)
			}
		}
		sort.Strings(keys)
		for _, k := range keys {
			yv, ok := y[k]
			if !ok {
				return fmt.Sprintf("%s: key %q only on the left", path, k)
			}
			if d := diff(path+"."+k, x[k], yv); d != "" {
				return d
			}
		}
		return ""
	default:
		fa, okA := toFloat(a)
		fb, okB := toFloat(b)
		if !okA || !okB || fa != fb {
			return fmt.Sprintf("%s: %v (%T) vs %v (%T)", path, a, a, b, b)
		}
		return ""
	}
}

func toFloat(v any) (float64, bool) {
	switch n := v.(type) {
	case json.Number:
		f, err := strconv.ParseFloat(string(n), 64)
		return f, err == nil
	case float64:
		return n, true
	case int:
		return float64(n), true
	case int64:
		return float64(n), true
	}
	return 0, false
}

// CopyTree копирует каталог src в dst с сохранением символических ссылок (как cp -a для фикстуры sysfs).
func CopyTree(t testing.TB, src, dst string) {
	t.Helper()
	err := filepath.Walk(src, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(src, path)
		target := filepath.Join(dst, rel)
		switch {
		case info.Mode()&os.ModeSymlink != 0:
			link, err := os.Readlink(path)
			if err != nil {
				return err
			}
			return os.Symlink(link, target)
		case info.IsDir():
			return os.MkdirAll(target, 0o755)
		default:
			b, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			return os.WriteFile(target, b, 0o644)
		}
	})
	if err != nil {
		t.Fatalf("copy %s -> %s: %v", src, dst, err)
	}
}

// FixtureCopy копирует testdata/fixture_root во временный каталог теста и возвращает его путь.
func FixtureCopy(t testing.TB) string {
	t.Helper()
	dst := filepath.Join(t.TempDir(), "root")
	CopyTree(t, Testdata("fixture_root"), dst)
	return dst
}

// WriteFile пишет файл, создавая каталоги.
func WriteFile(t testing.TB, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}
