package canon

import "testing"

func TestMarshal(t *testing.T) {
	type s struct {
		Z int     `json:"z"`
		A float64 `json:"a"`
	}
	cases := []struct {
		in   any
		want string
	}{
		{map[string]any{"b": 1, "a": "дверь <x>"}, `{"a":"дверь <x>","b":1}`},
		{s{Z: 2, A: 0.58}, `{"a":0.58,"z":2}`},
		{[]any{1.0, nil, true}, `[1,null,true]`},
	}
	for _, c := range cases {
		if got := String(c.in); got != c.want {
			t.Errorf("String(%v) = %s, want %s", c.in, got, c.want)
		}
	}
}
