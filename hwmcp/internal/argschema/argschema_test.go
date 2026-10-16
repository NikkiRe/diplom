package argschema

import (
	"compress/gzip"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
	"testing"

	"github.com/local/hwmcp/internal/testutil"
)

func mustDecode(t *testing.T, s string) any {
	t.Helper()
	v, err := Decode([]byte(s))
	if err != nil {
		t.Fatalf("decode %s: %v", s, err)
	}
	return v
}

func kinds(t *testing.T, instance, schema string, strict bool) string {
	t.Helper()
	errs := Validate(mustDecode(t, instance), mustDecode(t, schema), strict)
	var ks []string
	for _, e := range errs {
		ks = append(ks, e.Kind)
	}
	sort.Strings(ks)
	return strings.Join(ks, ",")
}

// --- перенос monitor/tests/test_argschema.py (основные случаи) ---

func TestBoolIsNotANumber(t *testing.T) {
	for _, typ := range []string{"integer", "number"} {
		if TypeMatches(true, typ, false) || TypeMatches(false, typ, false) {
			t.Fatalf("bool matched %s", typ)
		}
	}
	if !TypeMatches(true, "boolean", false) || TypeMatches(json.Number("1"), "boolean", false) {
		t.Fatal("boolean semantics")
	}
}

func TestIntegerSemantics(t *testing.T) {
	cases := []struct {
		lit    string
		typ    string
		strict bool
		want   bool
	}{
		{"3", "integer", false, true},
		{"1000000000000000000000000000000", "integer", false, true},
		{"3.0", "integer", false, true}, // нулевая дробная часть
		{"3.0", "integer", true, false},
		{"3e0", "integer", true, false},
		{"3.5", "integer", false, false},
		{"3", "number", false, true},
		{"3.5", "number", false, true},
	}
	for _, c := range cases {
		if got := TypeMatches(json.Number(c.lit), c.typ, c.strict); got != c.want {
			t.Errorf("TypeMatches(%s, %s, strict=%v) = %v", c.lit, c.typ, c.strict, got)
		}
	}
}

func TestOtherTypes(t *testing.T) {
	if !TypeMatches(nil, "null", false) || TypeMatches(json.Number("0"), "null", false) {
		t.Fatal("null")
	}
	if !TypeMatches("", "string", false) || !TypeMatches([]any{}, "array", false) ||
		TypeMatches(map[string]any{}, "array", false) || !TypeMatches(map[string]any{}, "object", false) ||
		TypeMatches("x", "unknown-type", false) {
		t.Fatal("types")
	}
}

func TestTypeListAndNullable(t *testing.T) {
	s := `{"type": ["integer", "null"]}`
	for inst, want := range map[string]string{"null": "", "4": "", `"4"`: "type"} {
		if got := kinds(t, inst, s, false); got != want {
			t.Errorf("%s: %q", inst, got)
		}
	}
	if kinds(t, "null", `{"type":"string","nullable":true}`, false) != "" ||
		kinds(t, "null", `{"type":"string"}`, false) != "type" {
		t.Fatal("nullable")
	}
}

func TestStrictIntegerPropagates(t *testing.T) {
	s := `{"type":"object","properties":{"n":{"type":"integer"}}}`
	if kinds(t, `{"n":2.0}`, s, false) != "" || kinds(t, `{"n":2.0}`, s, true) != "type" {
		t.Fatal("strict integer must propagate into properties")
	}
}

func TestJSONEquality(t *testing.T) {
	eq := func(a, b string) bool { return Equal(mustDecode(t, a), mustDecode(t, b)) }
	if !eq("1", "1.0") || eq("1", "true") || eq("0", "false") || eq(`"1"`, "1") ||
		!eq(`{"a":[1,{"b":null}]}`, `{"a":[1.0,{"b":null}]}`) || eq(`{"a":1}`, `{"a":1,"b":2}`) || eq("[1,2]", "[2,1]") {
		t.Fatal("json equality")
	}
	if !Equal(30, json.Number("30.0")) || Equal(1, true) || !Equal(2.5, json.Number("2.5")) {
		t.Fatal("go numbers vs literals")
	}
}

func TestEnumAndConst(t *testing.T) {
	for _, c := range []struct{ inst, schema, want string }{
		{"true", `{"enum":[1,2]}`, "enum"},
		{"1", `{"enum":[true]}`, "enum"},
		{"2.0", `{"enum":[1,2]}`, ""},
		{"[1,2]", `{"enum":[[1,2],[3]]}`, ""},
		{`"c"`, `{"type":"string","enum":["a","b"]}`, "enum"},
		{`"x"`, `{"const":"x"}`, ""},
		{"false", `{"const":0}`, "const"},
		{"null", `{"const":null}`, ""},
		{"0", `{"const":null}`, "const"},
	} {
		if got := kinds(t, c.inst, c.schema, false); got != c.want {
			t.Errorf("%s ~ %s: %q, want %q", c.inst, c.schema, got, c.want)
		}
	}
}

func TestObjects(t *testing.T) {
	s := `{"type":"object","properties":{"a":{"type":"string"},"b":{"type":"integer"}},"required":["a"],"additionalProperties":false}`
	for _, c := range []struct{ inst, want string }{
		{`{"a":"x"}`, ""},
		{`{"b":1}`, "required"},
		{`{"a":1}`, "type"},
		{`{"a":"x","c":1}`, "additional"},
		{`{"a":"x","b":"y","c":1}`, "additional,type"},
	} {
		if got := kinds(t, c.inst, s, false); got != c.want {
			t.Errorf("%s: %q, want %q", c.inst, got, c.want)
		}
	}
	errs := Validate(mustDecode(t, `{"a/b":1}`), mustDecode(t, `{"properties":{"a/b":{"type":"string"}}}`), false)
	if len(errs) != 1 || errs[0].Path != "/a~1b" {
		t.Fatalf("pointer escaping: %+v", errs)
	}
}

func TestBoundsDraft04Boolean(t *testing.T) {
	s := `{"type":"number","minimum":0,"exclusiveMinimum":true,"maximum":10,"exclusiveMaximum":true}`
	for inst, want := range map[string]string{"0": "bounds", "10": "bounds", "5": "", "0.0001": ""} {
		if got := kinds(t, inst, s, false); got != want {
			t.Errorf("%s: %q, want %q", inst, got, want)
		}
	}
}

func TestCombinators(t *testing.T) {
	if kinds(t, "3", `{"anyOf":[{"type":"string"},{"type":"integer","minimum":5}]}`, false) != "combinator" {
		t.Fatal("anyOf")
	}
	if kinds(t, "3", `{"oneOf":[{"type":"integer"},{"type":"number","minimum":0}]}`, false) != "combinator" {
		t.Fatal("oneOf matched twice")
	}
	if kinds(t, "-3", `{"oneOf":[{"type":"integer"},{"type":"number","minimum":0}]}`, false) != "" {
		t.Fatal("oneOf matched once")
	}
	if kinds(t, "5", `{"allOf":[{"type":"number"},{"maximum":3},{"minimum":1}]}`, false) != "bounds" {
		t.Fatal("allOf")
	}
}

func TestFalseSchemaAndNonSchemas(t *testing.T) {
	if kinds(t, "1", "false", false) != "false_schema" || kinds(t, "1", "true", false) != "" ||
		kinds(t, "1", "null", false) != "" || kinds(t, "1", "7", false) != "" {
		t.Fatal("boolean schemas")
	}
}

func TestCheckSchemaHints(t *testing.T) {
	r := CheckSchema(mustDecode(t, `{"type":"float"}`))
	if len(r.Problems) != 1 || !strings.Contains(r.Problems[0], "number") {
		t.Fatalf("%+v", r)
	}
	r = CheckSchema(mustDecode(t, `{"$ref":"#/x","description":"d","x-ext":1,"not":{}}`))
	if len(r.Problems) != 0 || strings.Join(r.Unchecked, ",") != "$ref,not" {
		t.Fatalf("%+v", r)
	}
	if !CheckSchema(mustDecode(t, `{"type":"object","properties":{"on":{"type":"boolean"}},"required":["on"]}`)).OK() {
		t.Fatal("simple schema must be OK")
	}
}

// --- паритет с monitor/argschema.py на корпусе из tools/gen_golden.py ---

type goldenArgschema struct {
	Schemas   []json.RawMessage `json:"schemas"`
	Instances []json.RawMessage `json:"instances"`
	Cases     []struct {
		S      int        `json:"s"`
		I      int        `json:"i"`
		Strict bool       `json:"strict"`
		Errors [][]string `json:"errors"`
	} `json:"cases"`
	CheckSchema []struct {
		Schema    json.RawMessage `json:"schema"`
		NProblems int             `json:"n_problems"`
		Unchecked []string        `json:"unchecked"`
	} `json:"check_schema"`
}

func loadGolden(t *testing.T) goldenArgschema {
	t.Helper()
	f, err := os.Open(testutil.Testdata("golden", "argschema.json.gz"))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	zr, err := gzip.NewReader(f)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := io.ReadAll(zr)
	if err != nil {
		t.Fatal(err)
	}
	var g goldenArgschema
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatal(err)
	}
	return g
}

func TestParityWithPrototypeValidator(t *testing.T) {
	g := loadGolden(t)
	schemas := make([]*Schema, len(g.Schemas))
	for i, s := range g.Schemas {
		c, err := CompileJSON(s)
		if err != nil {
			t.Fatal(err)
		}
		schemas[i] = c
	}
	instances := make([]any, len(g.Instances))
	for i, s := range g.Instances {
		instances[i] = mustDecode(t, string(s))
	}
	mismatches := 0
	for _, c := range g.Cases {
		errs := schemas[c.S].Validate(instances[c.I], c.Strict)
		got := make([]string, 0, len(errs))
		for _, e := range errs {
			got = append(got, e.Kind+" "+e.Path)
		}
		sort.Strings(got)
		want := make([]string, 0, len(c.Errors))
		for _, e := range c.Errors {
			want = append(want, e[0]+" "+e[1])
		}
		sort.Strings(want)
		if fmt.Sprint(got) != fmt.Sprint(want) {
			mismatches++
			if mismatches <= 10 {
				t.Errorf("schema %s instance %s strict=%v: got %v, want %v",
					g.Schemas[c.S], g.Instances[c.I], c.Strict, got, want)
			}
		}
	}
	if mismatches > 0 {
		t.Fatalf("%d of %d cases differ from monitor/argschema.py", mismatches, len(g.Cases))
	}
	t.Logf("%d validation cases match the prototype", len(g.Cases))
}

func TestParityCheckSchema(t *testing.T) {
	g := loadGolden(t)
	for _, c := range g.CheckSchema {
		r := CheckSchema(mustDecode(t, string(c.Schema)))
		if len(r.Problems) != c.NProblems || fmt.Sprint(r.Unchecked) != fmt.Sprint(nonNil(c.Unchecked)) {
			t.Errorf("schema %s: problems %d %v, unchecked %v; want %d, %v",
				c.Schema, len(r.Problems), r.Problems, r.Unchecked, c.NProblems, c.Unchecked)
		}
	}
}

func nonNil(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

func BenchmarkValidateSetColor(b *testing.B) {
	s, _ := CompileJSON([]byte(`{"type":"object","properties":{"r":{"type":"integer","minimum":0,"maximum":255},
		"g":{"type":"integer","minimum":0,"maximum":255},"b":{"type":"integer","minimum":0,"maximum":255}},
		"required":["r","g","b"],"additionalProperties":false}`))
	v, _ := Decode([]byte(`{"r":12,"g":200,"b":255}`))
	b.ReportAllocs()
	for b.Loop() {
		if len(s.Validate(v, true)) != 0 {
			b.Fatal("unexpected errors")
		}
	}
}
