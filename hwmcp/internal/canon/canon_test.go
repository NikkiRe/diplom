package canon

import "testing"

func TestMarshalSortsKeysNoEscapes(t *testing.T) {
	type inner struct {
		Z int    `json:"z"`
		A string `json:"a"`
	}
	v := map[string]any{"b": inner{Z: 1, A: "<кухня & зал>"}, "a": []any{1.5, true, nil}}
	got := MustString(v)
	want := `{"a":[1.5,true,null],"b":{"a":"<кухня & зал>","z":1}}`
	if got != want {
		t.Fatalf("got %s\nwant %s", got, want)
	}
}

func TestMarshalKeepsNumberLiterals(t *testing.T) {
	got := MustString(map[string]any{"x": 101325.0, "y": 22.51, "n": 30})
	if got != `{"n":30,"x":101325,"y":22.51}` {
		t.Fatalf("got %s", got)
	}
}

func TestMarshalDeterministic(t *testing.T) {
	v := map[string]any{}
	for _, k := range []string{"q", "w", "e", "r", "t", "y"} {
		v[k] = map[string]any{"k" + k: k}
	}
	first := MustString(v)
	for i := 0; i < 50; i++ {
		if MustString(v) != first {
			t.Fatal("non-deterministic output")
		}
	}
}
