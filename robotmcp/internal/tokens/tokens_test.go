package tokens

import (
	"encoding/json"
	"math"
	"os"
	"testing"
)

type golden struct {
	Items []struct {
		Name  string `json:"name"`
		Text  string `json:"text"`
		O200k int    `json:"o200k"`
	} `json:"items"`
}

// Оценка против точного o200k на эталоне: суммарная ошибка ≤ 5 %, по коротким JSON-ответам
// (≤ 400 токенов — ответы R-compact, описания инструментов) — ≤ 15 % на 90 % текстов.
func TestGolden(t *testing.T) {
	b, err := os.ReadFile("../../testdata/tokens_golden.json")
	if err != nil {
		t.Skip("нет эталона:", err)
	}
	var g golden
	if err := json.Unmarshal(b, &g); err != nil {
		t.Fatal(err)
	}
	var sumEst, sumRef float64
	var small, smallOK int
	worst := 0.0
	worstName := ""
	for _, it := range g.Items {
		est := float64(Count(it.Text))
		ref := float64(it.O200k)
		sumEst += est
		sumRef += ref
		rel := math.Abs(est-ref) / math.Max(ref, 1)
		if it.O200k <= 400 && it.O200k >= 10 {
			small++
			if rel <= 0.15 {
				smallOK++
			}
		}
		if rel > worst && it.O200k >= 10 {
			worst, worstName = rel, it.Name
		}
		if testing.Verbose() && rel > 0.15 && it.O200k >= 10 {
			t.Logf("%-60s ref %5d est %5d (%+.0f%%)", it.Name, it.O200k, int(est), 100*(est-ref)/ref)
		}
	}
	total := (sumEst - sumRef) / sumRef
	t.Logf("items %d: total est %.0f ref %.0f (%+.1f%%); short texts within 15%%: %d/%d; worst %s %.0f%%",
		len(g.Items), sumEst, sumRef, 100*total, smallOK, small, worstName, 100*worst)
	if math.Abs(total) > 0.05 {
		t.Errorf("total error %.1f%% > 5%%", 100*total)
	}
	if float64(smallOK) < 0.9*float64(small) {
		t.Errorf("short texts within 15%%: %d/%d < 90%%", smallOK, small)
	}
}

func TestBasic(t *testing.T) {
	if Count("") != 0 {
		t.Fatal("empty")
	}
	for _, s := range []string{`{"ok":true}`, "подъедь к двери", "  \n\n x", "1234567", "a_b-c/d"} {
		if Count(s) <= 0 {
			t.Errorf("%q: %d", s, Count(s))
		}
	}
}

func FuzzCount(f *testing.F) {
	f.Add(`{"a":[1,2.5,"x y"]}`)
	f.Fuzz(func(t *testing.T, s string) {
		if n := Count(s); n < 0 || (s != "" && n == 0) {
			t.Fatalf("%q -> %d", s, n)
		}
	})
}

// TestSweep — подбор констант (go test -run TestSweep -v -sweep).
func TestSweep(t *testing.T) {
	if os.Getenv("TOKENS_SWEEP") == "" {
		t.Skip("TOKENS_SWEEP=1 для перебора")
	}
	b, _ := os.ReadFile("../../testdata/tokens_golden.json")
	var g golden
	_ = json.Unmarshal(b, &g)
	l0, c0, p0 := latinPerToken, cyrillicPerToken, punctPerToken
	defer func() { latinPerToken, cyrillicPerToken, punctPerToken = l0, c0, p0 }()
	for _, l := range []float64{5, 6, 7, 8, 10, 12} {
		for _, p := range []float64{2, 3, 4, 6, 8} {
			latinPerToken, punctPerToken = l, p
			var se, sr float64
			ok, n := 0, 0
			for _, it := range g.Items {
				e, r := float64(Count(it.Text)), float64(it.O200k)
				se += e
				sr += r
				if it.O200k >= 10 && it.O200k <= 400 {
					n++
					if math.Abs(e-r)/r <= 0.15 {
						ok++
					}
				}
			}
			t.Logf("latin %4.1f punct %3.1f: total %+5.1f%% short ok %d/%d", l, p, 100*(se-sr)/sr, ok, n)
		}
	}
}
