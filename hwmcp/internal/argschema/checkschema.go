package argschema

import (
	"fmt"
	"sort"
	"strings"
)

// CheckedKeywords — ключевые слова, которые валидатор проверяет.
var CheckedKeywords = map[string]bool{
	"type": true, "enum": true, "const": true, "properties": true, "required": true,
	"additionalProperties": true, "items": true, "minimum": true, "maximum": true,
	"exclusiveMinimum": true, "exclusiveMaximum": true, "minLength": true, "maxLength": true,
	"minItems": true, "maxItems": true, "uniqueItems": true, "minProperties": true,
	"maxProperties": true, "anyOf": true, "oneOf": true, "allOf": true, "nullable": true,
}

// AnnotationKeywords — аннотации: по стандарту не влияют на результат проверки.
var AnnotationKeywords = map[string]bool{
	"description": true, "title": true, "default": true, "examples": true, "$schema": true, "$id": true,
	"$anchor": true, "$comment": true, "format": true, "deprecated": true, "readOnly": true,
	"writeOnly": true, "contentMediaType": true, "contentEncoding": true, "contentSchema": true,
	"optional": true, "$defs": true, "definitions": true,
}

var typeHints = map[string]string{
	"dict": "object", "float": "number", "int": "integer", "str": "string", "bool": "boolean",
	"list": "array", "tuple": "array", "any": "(без ограничения типа)",
}

// SchemaCheck — результат проверки самой схемы: Problems — ошибки (схема некорректна или вне
// подмножества), Unchecked — встретившиеся ключевые слова, которые валидатор не проверяет.
type SchemaCheck struct {
	Problems  []string
	Unchecked []string // отсортированы
}

// OK — схема корректна и проверяется полностью.
func (c SchemaCheck) OK() bool { return len(c.Problems) == 0 && len(c.Unchecked) == 0 }

// CheckSchema проверяет, что схема корректна и укладывается в поддерживаемое подмножество.
func CheckSchema(schema any) SchemaCheck {
	unchecked := map[string]bool{}
	var problems []string
	checkSchema(schema, "#", &problems, unchecked)
	res := SchemaCheck{Problems: problems}
	for k := range unchecked {
		res.Unchecked = append(res.Unchecked, k)
	}
	sort.Strings(res.Unchecked)
	return res
}

func isNonNegIntValue(v any) bool { return nonNegInt(v) != nil }

func checkSchema(schema any, path string, problems *[]string, unchecked map[string]bool) {
	if _, ok := schema.(bool); ok {
		return
	}
	m, ok := schema.(map[string]any)
	if !ok {
		*problems = append(*problems, fmt.Sprintf("%s: схема должна быть объектом или bool, а не %s", path, TypeName(schema)))
		return
	}
	add := func(format string, args ...any) { *problems = append(*problems, fmt.Sprintf(format, args...)) }

	for key := range m {
		if CheckedKeywords[key] || AnnotationKeywords[key] || strings.HasPrefix(key, "x-") {
			continue
		}
		unchecked[key] = true
	}

	if t, ok := m["type"]; ok {
		var names []any
		switch tv := t.(type) {
		case []any:
			names = tv
		default:
			names = []any{tv}
		}
		allStr := len(names) > 0
		for _, n := range names {
			if _, ok := n.(string); !ok {
				allStr = false
			}
		}
		if !allStr {
			add("%s/type: ожидается строка или непустой список строк", path)
		} else {
			seen := map[string]bool{}
			dup := false
			for _, n := range names {
				ns := n.(string)
				if !isJSONType(ns) {
					tail := ""
					if h, ok := typeHints[ns]; ok {
						tail = fmt.Sprintf(" (вероятно, имелось в виду %s)", h)
					}
					add("%s/type: неизвестный тип %q%s", path, ns, tail)
				}
				if seen[ns] {
					dup = true
				}
				seen[ns] = true
			}
			if dup {
				add("%s/type: повторяющиеся типы", path)
			}
		}
	}

	if e, ok := m["enum"]; ok {
		if _, isList := e.([]any); !isList {
			add("%s/enum: ожидается массив", path)
		}
	}

	if r, ok := m["required"]; ok {
		list, isList := r.([]any)
		allStr := isList
		seen := map[string]bool{}
		dup := false
		for _, x := range list {
			s, ok := x.(string)
			if !ok {
				allStr = false
				continue
			}
			if seen[s] {
				dup = true
			}
			seen[s] = true
		}
		if !allStr {
			add("%s/required: ожидается массив строк", path)
		} else if dup {
			add("%s/required: повторяющиеся имена", path)
		}
	}

	if p, ok := m["properties"]; ok {
		props, isObj := p.(map[string]any)
		if !isObj {
			add("%s/properties: ожидается объект", path)
		} else {
			names := make([]string, 0, len(props))
			for k := range props {
				names = append(names, k)
			}
			sort.Strings(names)
			for _, k := range names {
				checkSchema(props[k], pointer(path+"/properties", k), problems, unchecked)
			}
		}
	}

	if ap, ok := m["additionalProperties"]; ok {
		switch ap.(type) {
		case map[string]any, bool:
			checkSchema(ap, path+"/additionalProperties", problems, unchecked)
		default:
			add("%s/additionalProperties: ожидается bool или схема", path)
		}
	}

	if it, ok := m["items"]; ok {
		switch x := it.(type) {
		case []any:
			for i, sub := range x {
				checkSchema(sub, fmt.Sprintf("%s/items/%d", path, i), problems, unchecked)
			}
		case map[string]any, bool:
			checkSchema(x, path+"/items", problems, unchecked)
		default:
			add("%s/items: ожидается схема или массив схем", path)
		}
	}

	for _, key := range []string{"minimum", "maximum"} {
		if v, ok := m[key]; ok && !IsNumber(v) {
			add("%s/%s: ожидается число", path, key)
		}
	}
	for _, key := range []string{"exclusiveMinimum", "exclusiveMaximum"} {
		if v, ok := m[key]; ok {
			if _, isBool := v.(bool); !isBool && !IsNumber(v) {
				add("%s/%s: ожидается число (или bool в форме draft-04)", path, key)
			}
		}
	}
	for _, key := range []string{"minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties"} {
		if v, ok := m[key]; ok && !isNonNegIntValue(v) {
			add("%s/%s: ожидается неотрицательное целое", path, key)
		}
	}
	for _, key := range []string{"uniqueItems", "nullable"} {
		if v, ok := m[key]; ok {
			if _, isBool := v.(bool); !isBool {
				add("%s/%s: ожидается bool", path, key)
			}
		}
	}
	for _, key := range []string{"anyOf", "oneOf", "allOf"} {
		if v, ok := m[key]; ok {
			subs, isList := v.([]any)
			if !isList || len(subs) == 0 {
				add("%s/%s: ожидается непустой массив схем", path, key)
				continue
			}
			for i, sub := range subs {
				checkSchema(sub, fmt.Sprintf("%s/%s/%d", path, key, i), problems, unchecked)
			}
		}
	}
}

func isJSONType(s string) bool {
	for _, t := range JSONTypes {
		if t == s {
			return true
		}
	}
	return false
}
