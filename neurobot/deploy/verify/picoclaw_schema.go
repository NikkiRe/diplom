// Схема полей config.json PicoClaw для проверки «неизвестных полей» (neurobot picoclaw-config, R7 N11).
//
// PicoClaw 0.2.x–0.3.x отказывается стартовать, если в config.json есть поле, которого нет в его структуре Config
// (pkg/config/diagnostics.go: collectUnknownJSONFields). Эта программа обходит ту же структуру тем же способом
// (reflect, теги json, встроенные структуры) и печатает дерево: объект — {поле: поддерево}, словарь — {"*": …},
// массив — {"[]": …}, лист — null. Запуск — из корня исходников нужной версии PicoClaw (копия, не vendor):
//
//	mkdir -p cmd/nbschema && cp <neurobot/deploy>/verify/picoclaw_schema.go cmd/nbschema/main.go
//	go run ./cmd/nbschema | python3 -c 'import json,sys; json.dump(json.load(sys.stdin), sys.stdout, sort_keys=True, indent=1)' \
//	    > <neurobot/deploy>/picoclaw/schema-<версия>.json
package main

import (
	"encoding/json"
	"os"
	"reflect"
	"strings"

	"github.com/sipeed/picoclaw/pkg/config"
)

func walk(t reflect.Type, seen map[reflect.Type]bool) any {
	for t != nil && t.Kind() == reflect.Pointer {
		t = t.Elem()
	}
	switch t.Kind() {
	case reflect.Struct:
		if seen[t] { // рекурсивный тип: глубже не проверяем
			return nil
		}
		seen[t] = true
		defer delete(seen, t)
		out := map[string]any{}
		fields(t, out, seen)
		return out
	case reflect.Slice, reflect.Array:
		return map[string]any{"[]": walk(t.Elem(), seen)}
	case reflect.Map:
		return map[string]any{"*": walk(t.Elem(), seen)}
	}
	return nil
}

func fields(t reflect.Type, out map[string]any, seen map[reflect.Type]bool) {
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		if !f.IsExported() {
			continue
		}
		name := strings.Split(f.Tag.Get("json"), ",")[0]
		if name == "-" {
			continue
		}
		ft := f.Type
		for ft.Kind() == reflect.Pointer {
			ft = ft.Elem()
		}
		if f.Anonymous && name == "" {
			if ft.Kind() == reflect.Struct {
				fields(ft, out, seen)
			}
			continue
		}
		if name == "" {
			name = f.Name
		}
		out[name] = walk(f.Type, seen)
	}
}

func main() {
	if err := json.NewEncoder(os.Stdout).Encode(walk(reflect.TypeOf(config.Config{}), map[reflect.Type]bool{})); err != nil {
		os.Exit(1)
	}
}
