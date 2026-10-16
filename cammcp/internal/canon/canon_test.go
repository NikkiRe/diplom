package canon

import "testing"

func TestMarshal(t *testing.T) {
	type s struct {
		Z int    `json:"z"`
		A string `json:"a"`
	}
	b, err := Marshal(map[string]any{"b": []any{s{Z: 1, A: "<кадр>"}}, "a": 0.1})
	if err != nil {
		t.Fatal(err)
	}
	if got, want := string(b), `{"a":0.1,"b":[{"a":"<кадр>","z":1}]}`; got != want {
		t.Fatalf("%s != %s", got, want)
	}
	if _, err := Marshal(func() {}); err == nil {
		t.Fatal("function marshalled")
	}
}
