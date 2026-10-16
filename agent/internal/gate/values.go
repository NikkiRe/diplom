package gate

import (
	"encoding/json"
	"math"
	"strconv"
	"strings"

	"github.com/local/edgectx/internal/canon"
)

// toFloat приводит значение датчика к числу: числа как есть, bool -> 1/0.
// Строки с числами тоже принимаются (некоторые серверы отдают значения строками).
func toFloat(v any) (float64, bool) {
	switch x := v.(type) {
	case float64:
		return x, true
	case float32:
		return float64(x), true
	case int:
		return float64(x), true
	case int64:
		return float64(x), true
	case json.Number:
		f, err := x.Float64()
		return f, err == nil
	case bool:
		if x {
			return 1, true
		}
		return 0, true
	case string:
		f, err := strconv.ParseFloat(strings.TrimSpace(x), 64)
		return f, err == nil && !math.IsNaN(f)
	}
	return 0, false
}

// toFloatLoose — как toFloat, но понимает и слова-состояния (on/off, true/false,
// open/closed), которыми LLM может записать значение в правиле.
func toFloatLoose(v any) (float64, bool) {
	if f, ok := toFloat(v); ok {
		return f, true
	}
	if s, ok := v.(string); ok {
		switch strings.ToLower(strings.TrimSpace(s)) {
		case "true", "on", "yes", "open", "opened", "active", "вкл", "включено", "открыто", "да":
			return 1, true
		case "false", "off", "no", "closed", "close", "inactive", "выкл", "выключено", "закрыто", "нет":
			return 0, true
		}
	}
	return 0, false
}

// sameValue — равенство значений для обнаружения изменения: числа и bool
// сравниваются численно, остальное — по каноническому JSON.
func sameValue(a, b any) bool {
	fa, oka := toFloat(a)
	fb, okb := toFloat(b)
	_, sa := a.(string)
	_, sb := b.(string)
	if oka && okb && !sa && !sb {
		return fa == fb
	}
	return canon.String(a) == canon.String(b)
}

// compare вычисляет условие правила «value op target».
func compare(op string, value, target any) bool {
	switch op {
	case "changed":
		return true
	case "eq", "ne":
		eq := false
		fv, okv := toFloatLoose(value)
		ft, okt := toFloatLoose(target)
		if okv && okt {
			eq = fv == ft
		} else {
			eq = strings.EqualFold(strings.TrimSpace(asString(value)), strings.TrimSpace(asString(target)))
		}
		if op == "eq" {
			return eq
		}
		return !eq
	case "gt", "ge", "lt", "le":
		fv, okv := toFloatLoose(value)
		ft, okt := toFloatLoose(target)
		if !okv || !okt {
			return false
		}
		switch op {
		case "gt":
			return fv > ft
		case "ge":
			return fv >= ft
		case "lt":
			return fv < ft
		default:
			return fv <= ft
		}
	}
	return false
}

func asString(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return canon.String(v)
}
