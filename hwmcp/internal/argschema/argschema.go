// Package argschema — мини-валидатор JSON Schema для аргументов команд.
//
// Порт monitor/argschema.py (им пользуется прототип шлюза actuator_gate.py): то же подмножество
// ключевых слов и те же правила, паритет проверяется на корпусе из tools/gen_golden.py.
//
// Проверяемые ключевые слова: type (строка или список), enum, const, properties, required,
// additionalProperties (bool или схема), items (схема или кортеж), minimum, maximum,
// exclusiveMinimum, exclusiveMaximum (числовая и булева форма draft-04), minLength, maxLength,
// minItems, maxItems, uniqueItems, minProperties, maxProperties, anyOf, oneOf, allOf, nullable (OpenAPI).
// Остальные ключевые слова не проверяются (CheckSchema возвращает их как «непроверенные»).
//
// Значения — результат разбора JSON с UseNumber (nil, bool, json.Number, string, []any, map[string]any);
// допускаются и числа Go (int, float64). Тонкости, как в прототипе:
//   - bool не integer и не number;
//   - integer — число с нулевой дробной частью (1.0 тоже); режим strict принимает только целые литералы;
//   - enum/const/uniqueItems сравнивают по правилам JSON: 1 == 1.0, но 1 != true.
//
// Отличие от прототипа: свойства объекта и ключи значения обходятся в отсортированном порядке
// (в Python — в порядке вставки), поэтому порядок ошибок может отличаться; набор ошибок тот же.
// Числа сравниваются как float64 (в Python целые сравниваются точно; разница — только за 2^53).
package argschema

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

// JSONTypes — типы JSON Schema.
var JSONTypes = []string{"null", "boolean", "integer", "number", "string", "array", "object"}

// Error — одно нарушение схемы. Kind: type, required, enum, const, additional, bounds, length,
// count, unique, combinator, false_schema. Path — JSON Pointer на значение ("" — корень).
type Error struct {
	Kind    string `json:"kind"`
	Path    string `json:"path"`
	Message string `json:"message"`
}

func (e Error) String() string {
	if e.Path == "" {
		return e.Message
	}
	return e.Path + ": " + e.Message
}

// Decode разбирает JSON-значение с сохранением литералов чисел (json.Number).
func Decode(b []byte) (any, error) {
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

// ----------------------------------------------------------------------------------------------
// Числа и типы

// number — число JSON: значение и признак целого литерала (как int в Python).
type number struct {
	f     float64
	isInt bool // литерал без дробной части и экспоненты (Python int)
}

func asNumber(v any) (number, bool) {
	switch n := v.(type) {
	case json.Number:
		s := string(n)
		f, err := strconv.ParseFloat(s, 64)
		if err != nil && !isRangeErr(err) {
			return number{}, false
		}
		return number{f: f, isInt: !strings.ContainsAny(s, ".eE")}, true
	case float64:
		return number{f: n}, true
	case float32:
		return number{f: float64(n)}, true
	case int:
		return number{f: float64(n), isInt: true}, true
	case int64:
		return number{f: float64(n), isInt: true}, true
	case int32:
		return number{f: float64(n), isInt: true}, true
	case uint64:
		return number{f: float64(n), isInt: true}, true
	}
	return number{}, false
}

func isRangeErr(err error) bool {
	ne, ok := err.(*strconv.NumError)
	return ok && ne.Err == strconv.ErrRange
}

// IsNumber — число (не bool).
func IsNumber(v any) bool { _, ok := asNumber(v); return ok }

// TypeMatches — соответствует ли значение типу JSON Schema.
func TypeMatches(v any, typeName string, strictInteger bool) bool {
	switch typeName {
	case "null":
		return v == nil
	case "boolean":
		_, ok := v.(bool)
		return ok
	case "integer":
		n, ok := asNumber(v)
		if !ok {
			return false
		}
		if n.isInt {
			return true
		}
		return !strictInteger && !math.IsInf(n.f, 0) && !math.IsNaN(n.f) && n.f == math.Trunc(n.f)
	case "number":
		n, ok := asNumber(v)
		return ok && (n.isInt || (!math.IsInf(n.f, 0) && !math.IsNaN(n.f)))
	case "string":
		_, ok := v.(string)
		return ok
	case "array":
		_, ok := v.([]any)
		return ok
	case "object":
		_, ok := v.(map[string]any)
		return ok
	}
	return false
}

// TypeName — имя JSON-типа значения (integer для целых литералов).
func TypeName(v any) string {
	switch x := v.(type) {
	case nil:
		return "null"
	case bool:
		return "boolean"
	case string:
		return "string"
	case []any:
		return "array"
	case map[string]any:
		return "object"
	default:
		if n, ok := asNumber(x); ok {
			if n.isInt {
				return "integer"
			}
			return "number"
		}
		return fmt.Sprintf("%T", v)
	}
}

// Equal — равенство JSON-значений: числа по величине, bool отличается от чисел.
func Equal(a, b any) bool {
	if ab, ok := a.(bool); ok {
		bb, ok := b.(bool)
		return ok && ab == bb
	}
	if _, ok := b.(bool); ok {
		return false
	}
	if na, ok := asNumber(a); ok {
		nb, ok := asNumber(b)
		return ok && na.f == nb.f
	}
	switch x := a.(type) {
	case nil:
		return b == nil
	case string:
		y, ok := b.(string)
		return ok && x == y
	case []any:
		y, ok := b.([]any)
		if !ok || len(x) != len(y) {
			return false
		}
		for i := range x {
			if !Equal(x[i], y[i]) {
				return false
			}
		}
		return true
	case map[string]any:
		y, ok := b.(map[string]any)
		if !ok || len(x) != len(y) {
			return false
		}
		for k, xv := range x {
			yv, ok := y[k]
			if !ok || !Equal(xv, yv) {
				return false
			}
		}
		return true
	}
	return false
}

func pointer(path, token string) string {
	token = strings.ReplaceAll(token, "~", "~0")
	token = strings.ReplaceAll(token, "/", "~1")
	return path + "/" + token
}

// jpath — JSON Pointer, который собирается в строку только при ошибке (без выделений памяти
// на успешном пути проверки).
type jpath struct {
	parent *jpath
	token  string
	index  int
	isIdx  bool
}

func (p *jpath) String() string {
	if p == nil {
		return ""
	}
	if p.isIdx {
		return pointer(p.parent.String(), strconv.Itoa(p.index))
	}
	return pointer(p.parent.String(), p.token)
}

func short(v any) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return fmt.Sprint(v)
	}
	s := strings.TrimSuffix(buf.String(), "\n")
	if utf8.RuneCountInString(s) > 60 {
		r := []rune(s)
		s = string(r[:57]) + "..."
	}
	return s
}

func formatNum(f float64) string { return strconv.FormatFloat(f, 'g', -1, 64) }

// ----------------------------------------------------------------------------------------------
// Скомпилированная схема

// Schema — схема, подготовленная к многократной проверке (без разбора map на каждом вызове).
type Schema struct {
	// always: для булевой схемы и «не-схем» (значения, не являющиеся объектом): true — пропустить всё.
	boolean  bool
	always   bool
	hasTypes bool
	types    []string
	nullable bool

	hasEnum  bool
	enum     []any
	hasConst bool
	constV   any

	min, max, xmin, xmax *float64

	minLen, maxLen, minItems, maxItems, minProps, maxProps *int
	unique                                                 bool

	items      *Schema
	tuple      []*Schema
	props      []propSchema // отсортированы по имени
	propIndex  map[string]*Schema
	required   []string
	addlMode   int // 0 — не проверять, 1 — запрещены, 2 — по схеме
	addlSchema *Schema

	allOf, anyOf, oneOf []*Schema

	// Raw — исходное дерево схемы (для журналов и описаний).
	Raw any
}

type propSchema struct {
	name   string
	schema *Schema
}

// Compile готовит схему (значение после Decode или собранное в коде). Ошибок не бывает: неправильно
// оформленные ключевые слова пропускаются, как в прототипе (их находит CheckSchema).
func Compile(raw any) *Schema {
	s := &Schema{Raw: raw}
	switch x := raw.(type) {
	case bool:
		s.boolean, s.always = true, x
		return s
	case map[string]any:
		s.compileObject(x)
		return s
	default: // null и прочие не-схемы ничего не утверждают
		s.boolean, s.always = true, true
		return s
	}
}

// CompileJSON — Compile для текста схемы.
func CompileJSON(b []byte) (*Schema, error) {
	v, err := Decode(b)
	if err != nil {
		return nil, err
	}
	return Compile(v), nil
}

func numPtr(v any) *float64 {
	if n, ok := asNumber(v); ok {
		f := n.f
		return &f
	}
	return nil
}

func nonNegInt(v any) *int {
	n, ok := asNumber(v)
	if !ok || !n.isInt || n.f < 0 {
		return nil
	}
	i := int(n.f)
	return &i
}

func (s *Schema) compileObject(m map[string]any) {
	if t, ok := m["type"]; ok {
		var names []string
		switch tv := t.(type) {
		case string:
			names = []string{tv}
		case []any:
			for _, n := range tv {
				if ns, ok := n.(string); ok {
					names = append(names, ns)
				}
			}
		}
		if len(names) > 0 {
			s.hasTypes, s.types = true, names
		}
	}
	if b, ok := m["nullable"].(bool); ok && b {
		s.nullable = true
	}
	if e, ok := m["enum"].([]any); ok {
		s.hasEnum, s.enum = true, e
	}
	if c, ok := m["const"]; ok {
		s.hasConst, s.constV = true, c
	}

	s.min, s.max = numPtr(m["minimum"]), numPtr(m["maximum"])
	// булева форма draft-04: exclusiveMinimum: true делает minimum строгим
	if b, ok := m["exclusiveMinimum"].(bool); ok {
		if b && s.min != nil {
			s.xmin, s.min = s.min, nil
		}
	} else {
		s.xmin = numPtr(m["exclusiveMinimum"])
	}
	if b, ok := m["exclusiveMaximum"].(bool); ok {
		if b && s.max != nil {
			s.xmax, s.max = s.max, nil
		}
	} else {
		s.xmax = numPtr(m["exclusiveMaximum"])
	}

	s.minLen, s.maxLen = nonNegInt(m["minLength"]), nonNegInt(m["maxLength"])
	s.minItems, s.maxItems = nonNegInt(m["minItems"]), nonNegInt(m["maxItems"])
	s.minProps, s.maxProps = nonNegInt(m["minProperties"]), nonNegInt(m["maxProperties"])
	if b, ok := m["uniqueItems"].(bool); ok && b {
		s.unique = true
	}

	switch it := m["items"].(type) {
	case []any:
		for _, sub := range it {
			s.tuple = append(s.tuple, Compile(sub))
		}
		if s.tuple == nil {
			s.tuple = []*Schema{}
		}
	case map[string]any, bool:
		s.items = Compile(it)
	}

	s.propIndex = map[string]*Schema{}
	if props, ok := m["properties"].(map[string]any); ok {
		names := make([]string, 0, len(props))
		for k := range props {
			names = append(names, k)
		}
		sort.Strings(names)
		for _, k := range names {
			c := Compile(props[k])
			s.props = append(s.props, propSchema{k, c})
			s.propIndex[k] = c
		}
	}
	if req, ok := m["required"].([]any); ok {
		for _, r := range req {
			if rs, ok := r.(string); ok {
				s.required = append(s.required, rs)
			}
		}
	}
	_, hasPattern := m["patternProperties"]
	if ap, present := m["additionalProperties"]; present && !hasPattern {
		switch a := ap.(type) {
		case bool:
			if !a {
				s.addlMode = 1
			}
		case map[string]any:
			s.addlMode, s.addlSchema = 2, Compile(a)
		}
	}

	if all, ok := m["allOf"].([]any); ok {
		for _, sub := range all {
			s.allOf = append(s.allOf, Compile(sub))
		}
	}
	if any_, ok := m["anyOf"].([]any); ok {
		for _, sub := range any_ {
			s.anyOf = append(s.anyOf, Compile(sub))
		}
	}
	if one, ok := m["oneOf"].([]any); ok {
		for _, sub := range one {
			s.oneOf = append(s.oneOf, Compile(sub))
		}
	}
}

// ----------------------------------------------------------------------------------------------
// Проверка значения

// Validate проверяет значение по скомпилированной схеме; пустой результат — значение корректно.
func (s *Schema) Validate(v any, strictInteger bool) []Error {
	var errs []Error
	s.validate(v, nil, &errs, strictInteger)
	return errs
}

// Valid — короткая форма Validate без сбора сообщений.
func (s *Schema) Valid(v any, strictInteger bool) bool {
	return len(s.Validate(v, strictInteger)) == 0
}

// Validate — проверка по «сырой» схеме (Compile на каждый вызов; для единичных проверок).
func Validate(v any, schema any, strictInteger bool) []Error {
	return Compile(schema).Validate(v, strictInteger)
}

func (s *Schema) validate(v any, path *jpath, errs *[]Error, strict bool) {
	if s.boolean {
		if !s.always {
			*errs = append(*errs, Error{"false_schema", path.String(), "схема false не допускает никакого значения"})
		}
		return
	}
	s.checkType(v, path, errs, strict)
	if s.hasEnum {
		found := false
		for _, e := range s.enum {
			if Equal(v, e) {
				found = true
				break
			}
		}
		if !found {
			*errs = append(*errs, Error{"enum", path.String(), fmt.Sprintf("значение %s не входит в enum %s", short(v), short(s.enum))})
		}
	}
	if s.hasConst && !Equal(v, s.constV) {
		*errs = append(*errs, Error{"const", path.String(), fmt.Sprintf("значение %s не равно const %s", short(v), short(s.constV))})
	}
	switch x := v.(type) {
	case string:
		s.checkCount(utf8.RuneCountInString(x), s.minLen, s.maxLen, "minLength", "maxLength", "length", "длина строки", path, errs)
	case []any:
		s.checkArray(x, path, errs, strict)
	case map[string]any:
		s.checkObject(x, path, errs, strict)
	case bool, nil:
	default:
		if n, ok := asNumber(v); ok {
			s.checkBounds(n.f, path, errs)
		}
	}
	s.checkCombinators(v, path, errs, strict)
}

func (s *Schema) checkType(v any, path *jpath, errs *[]Error, strict bool) {
	if !s.hasTypes {
		return
	}
	if v == nil && s.nullable {
		return
	}
	for _, t := range s.types {
		if TypeMatches(v, t, strict) {
			return
		}
	}
	expected := strings.Join(s.types, "|")
	*errs = append(*errs, Error{"type", path.String(), fmt.Sprintf("ожидается %s, получено %s %s", expected, TypeName(v), short(v))})
}

func (s *Schema) checkBounds(f float64, path *jpath, errs *[]Error) {
	if s.min != nil && f < *s.min {
		*errs = append(*errs, Error{"bounds", path.String(), fmt.Sprintf("%s < minimum %s", formatNum(f), formatNum(*s.min))})
	}
	if s.max != nil && f > *s.max {
		*errs = append(*errs, Error{"bounds", path.String(), fmt.Sprintf("%s > maximum %s", formatNum(f), formatNum(*s.max))})
	}
	if s.xmin != nil && f <= *s.xmin {
		*errs = append(*errs, Error{"bounds", path.String(), fmt.Sprintf("%s <= exclusiveMinimum %s", formatNum(f), formatNum(*s.xmin))})
	}
	if s.xmax != nil && f >= *s.xmax {
		*errs = append(*errs, Error{"bounds", path.String(), fmt.Sprintf("%s >= exclusiveMaximum %s", formatNum(f), formatNum(*s.xmax))})
	}
}

func (s *Schema) checkCount(n int, lo, hi *int, loKey, hiKey, kind, what string, path *jpath, errs *[]Error) {
	if lo != nil && n < *lo {
		*errs = append(*errs, Error{kind, path.String(), fmt.Sprintf("%s %d < %s %d", what, n, loKey, *lo)})
	}
	if hi != nil && n > *hi {
		*errs = append(*errs, Error{kind, path.String(), fmt.Sprintf("%s %d > %s %d", what, n, hiKey, *hi)})
	}
}

func (s *Schema) checkArray(a []any, path *jpath, errs *[]Error, strict bool) {
	s.checkCount(len(a), s.minItems, s.maxItems, "minItems", "maxItems", "count", "число элементов", path, errs)
	if s.tuple != nil {
		// кортежная форма: i-й элемент по i-й схеме; хвост (additionalItems) не проверяется
		for i := 0; i < len(a) && i < len(s.tuple); i++ {
			s.tuple[i].validate(a[i], &jpath{parent: path, index: i, isIdx: true}, errs, strict)
		}
	} else if s.items != nil {
		for i, e := range a {
			s.items.validate(e, &jpath{parent: path, index: i, isIdx: true}, errs, strict)
		}
	}
	if s.unique {
		for i := range a {
			for j := 0; j < i; j++ {
				if Equal(a[i], a[j]) {
					*errs = append(*errs, Error{"unique", path.String(), fmt.Sprintf("элементы %d и %d совпадают (uniqueItems)", j, i)})
					return
				}
			}
		}
	}
}

func (s *Schema) checkObject(o map[string]any, path *jpath, errs *[]Error, strict bool) {
	s.checkCount(len(o), s.minProps, s.maxProps, "minProperties", "maxProperties", "count", "число свойств", path, errs)
	for _, name := range s.required {
		if _, ok := o[name]; !ok {
			*errs = append(*errs, Error{"required", pointer(path.String(), name), fmt.Sprintf("нет обязательного параметра %q", name)})
		}
	}
	for _, p := range s.props {
		if v, ok := o[p.name]; ok {
			p.schema.validate(v, &jpath{parent: path, token: p.name}, errs, strict)
		}
	}
	if s.addlMode == 0 {
		return
	}
	var extra []string
	for k := range o {
		if _, ok := s.propIndex[k]; !ok {
			extra = append(extra, k)
		}
	}
	sort.Strings(extra)
	for _, k := range extra {
		if s.addlMode == 1 {
			*errs = append(*errs, Error{"additional", pointer(path.String(), k), fmt.Sprintf("параметр %q не описан в схеме", k)})
		} else {
			s.addlSchema.validate(o[k], &jpath{parent: path, token: k}, errs, strict)
		}
	}
}

func (s *Schema) checkCombinators(v any, path *jpath, errs *[]Error, strict bool) {
	for _, sub := range s.allOf {
		sub.validate(v, path, errs, strict)
	}
	if len(s.anyOf) > 0 {
		ok := false
		for _, sub := range s.anyOf {
			if sub.Valid(v, strict) {
				ok = true
				break
			}
		}
		if !ok {
			*errs = append(*errs, Error{"combinator", path.String(),
				fmt.Sprintf("значение %s не подходит ни под одну из %d схем anyOf", short(v), len(s.anyOf))})
		}
	}
	if len(s.oneOf) > 0 {
		matched := 0
		for _, sub := range s.oneOf {
			if sub.Valid(v, strict) {
				matched++
			}
		}
		if matched != 1 {
			*errs = append(*errs, Error{"combinator", path.String(),
				fmt.Sprintf("значение %s подходит под %d схем oneOf (нужно ровно 1)", short(v), matched)})
		}
	}
}
