package userctx

import (
	"fmt"
	"path/filepath"
	"strings"
	"testing"

	"github.com/local/edgectx/internal/config"
	"github.com/local/edgectx/internal/llm"
)

var defaultStorage = StorageOf(config.Default().UserCtx)

func open(t *testing.T, path string, reset bool) *Store {
	t.Helper()
	s, err := Open(path, reset)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	return s
}

func TestFactsOps(t *testing.T) {
	s := open(t, ":memory:", false)
	if err := s.SeedFacts([]config.Fact{{ID: "f1", Text: "любит 22 °C"}}, 1); err != nil {
		t.Fatal(err)
	}
	s.SeedFacts([]config.Fact{{ID: "f1", Text: "другое"}}, 2) // уже есть — не меняется
	f, err := s.AddFact("", "  встаёт в 7:00 ", 3)
	if err != nil || f.ID != "f2" || f.Text != "встаёт в 7:00" {
		t.Fatalf("add: %+v %v", f, err)
	}
	if _, err := s.AddFact("f1", "x", 4); err != ErrFactExists {
		t.Fatalf("dup add: %v", err)
	}
	if err := s.UpdateFact("f1", "любит 21 °C", 5); err != nil {
		t.Fatal(err)
	}
	if err := s.UpdateFact("f9", "x", 5); err != ErrFactNotFound {
		t.Fatalf("update missing: %v", err)
	}
	if err := s.DeleteFact("f2"); err != nil {
		t.Fatal(err)
	}
	if err := s.DeleteFact("f2"); err != ErrFactNotFound {
		t.Fatal("double delete")
	}
	if _, err := s.AddFact("", "", 6); err == nil {
		t.Fatal("empty fact must fail")
	}
	f3, _ := s.AddFact("", "кот по имени\nБарсик", 7)
	if f3.ID != "f2" {
		t.Fatalf("id reuse after delete: %s", f3.ID)
	}
	want := "- f1: любит 21 °C\n- f2: кот по имени Барсик"
	if got := s.ProfileText(); got != want {
		t.Fatalf("profile:\n%s", got)
	}
}

func turn(i int, withTool bool) Turn {
	msgs := []llm.Message{{Role: "user", Content: fmt.Sprintf("q%d", i)}}
	if withTool {
		msgs = append(msgs,
			llm.Message{Role: "assistant", ToolCalls: []llm.ToolCall{{ID: fmt.Sprintf("c%d", i), Type: "function",
				Function: llm.FunctionCall{Name: "read_property", Arguments: "{}"}}}},
			llm.Message{Role: "tool", ToolCallID: fmt.Sprintf("c%d", i), Content: fmt.Sprintf("result%d", i)})
	}
	msgs = append(msgs, llm.Message{Role: "assistant", Content: fmt.Sprintf("a%d", i)})
	return Turn{InputID: fmt.Sprintf("u%d", i), Kind: "user", T: float64(i), Messages: msgs}
}

func contents(msgs []llm.Message) string {
	var parts []string
	for _, m := range msgs {
		c := m.Content
		if c == "" && len(m.ToolCalls) > 0 {
			c = "call"
		}
		parts = append(parts, c)
	}
	return strings.Join(parts, ",")
}

func TestHistoryPolicies(t *testing.T) {
	s := open(t, ":memory:", false)
	for i := 1; i <= 4; i++ {
		if _, err := s.AppendTurn(turn(i, true)); err != nil {
			t.Fatal(err)
		}
	}
	_, full := s.View(Policy{Kind: "full"})
	if len(full) != 16 {
		t.Fatalf("full %d", len(full))
	}
	_, win := s.View(Policy{Kind: "window", WindowTurns: 2})
	if contents(win) != "q3,call,result3,a3,q4,call,result4,a4" {
		t.Fatalf("window %s", contents(win))
	}
	_, mask := s.View(Policy{Kind: "mask", MaskKeepTurns: 1})
	if got := contents(mask); got != "q1,call,"+MaskedText+",a1,q2,call,"+MaskedText+",a2,q3,call,"+MaskedText+",a3,q4,call,result4,a4" {
		t.Fatalf("mask %s", got)
	}
	_, mw := s.View(Policy{Kind: "mask", WindowTurns: 3, MaskKeepTurns: 2})
	if got := contents(mw); got != "q2,call,"+MaskedText+",a2,q3,call,result3,a3,q4,call,result4,a4" {
		t.Fatalf("mask+window %s", got)
	}
	// Заглушка не портит исходные данные.
	_, full2 := s.View(Policy{Kind: "full"})
	if full2[2].Content != "result1" {
		t.Fatal("masking mutated stored turns")
	}
	// Целостность пар вызов/результат при обрезке окна.
	for _, m := range win {
		if m.Role == "tool" && !strings.HasPrefix(m.ToolCallID, "c") {
			t.Fatal("broken pair")
		}
	}
}

func TestSummaryPolicy(t *testing.T) {
	s := open(t, ":memory:", false)
	for i := 1; i <= 5; i++ {
		s.AppendTurn(turn(i, false))
	}
	cands := s.ToSummarize(2)
	if len(cands) != 3 || cands[2].Seq != 3 {
		t.Fatalf("candidates %+v", cands)
	}
	if err := s.SetSummary("пользователь спрашивал q1–q3", cands[2].Seq, 10); err != nil {
		t.Fatal(err)
	}
	sum, msgs := s.View(Policy{Kind: "summary_system"})
	if sum != "пользователь спрашивал q1–q3" || contents(msgs) != "q4,a4,q5,a5" {
		t.Fatalf("summary=%q msgs=%s", sum, contents(msgs))
	}
	if len(s.ToSummarize(2)) != 0 {
		t.Fatal("nothing left to summarize")
	}
}

func TestPersistenceAndReset(t *testing.T) {
	path := filepath.Join(t.TempDir(), "db", "u.db")
	s, err := Open(path, false)
	if err != nil {
		t.Fatal(err)
	}
	s.AddFact("", "факт", 1)
	s.AppendTurn(turn(1, true))
	s.SetSummary("сводка", 1, 2)
	s.SaveRule("r1", map[string]any{"id": "r1", "op": "eq"}, "llm", 3)
	s.SaveRule("r2", map[string]any{"id": "r2"}, "file", 3)
	s.DeleteRule("r2")
	s.Close()

	s2 := open(t, path, false)
	if len(s2.Facts()) != 1 || len(s2.Turns()) != 1 || s2.Turns()[0].Messages[1].ToolCalls[0].ID != "c1" {
		t.Fatalf("reload: facts=%d turns=%+v", len(s2.Facts()), s2.Turns())
	}
	if sum, upto := s2.Summary(); sum != "сводка" || upto != 1 {
		t.Fatalf("summary %q %d", sum, upto)
	}
	rules, err := s2.Rules()
	if err != nil || len(rules) != 1 || rules[0].ID != "r1" || rules[0].Source != "llm" || !strings.Contains(string(rules[0].Spec), `"op":"eq"`) {
		t.Fatalf("rules %+v %v", rules, err)
	}
	md := s2.ExportMarkdown()
	if !strings.Contains(md, "- факт") || !strings.Contains(md, "`r1`") {
		t.Fatalf("markdown:\n%s", md)
	}
	s2.Close()

	s3 := open(t, path, true)
	r3, _ := s3.Rules()
	if len(s3.Facts()) != 0 || len(s3.Turns()) != 0 || len(r3) != 0 {
		t.Fatal("reset must clear everything")
	}
	if seq, _ := s3.AppendTurn(turn(1, false)); seq != 1 {
		t.Fatalf("seq after reset %d", seq)
	}
}

func TestBoundedWindowCachePreservesArchiveOnRestart(t *testing.T) {
	path := filepath.Join(t.TempDir(), "history.db")
	p := Policy{Kind: "mask", WindowTurns: 3, MaskKeepTurns: 1}
	s, err := OpenWithPolicy(path, false, p, defaultStorage)
	if err != nil {
		t.Fatal(err)
	}
	for i := 1; i <= 40; i++ {
		if _, err := s.AppendTurn(turn(i, true)); err != nil {
			t.Fatal(err)
		}
	}
	if s.TurnCount() != 40 || len(s.Turns()) != 3 || s.Turns()[0].Seq != 38 {
		t.Fatalf("archive=%d cache=%+v", s.TurnCount(), s.Turns())
	}
	s.Close()
	s, err = OpenWithPolicy(path, false, p, defaultStorage)
	if err != nil {
		t.Fatal(err)
	}
	_, msgs := s.View(p)
	if s.TurnCount() != 40 || len(s.Turns()) != 3 || !strings.HasPrefix(contents(msgs), "q38,call,"+MaskedText) {
		t.Fatalf("restart archive=%d cache=%+v view=%s", s.TurnCount(), s.Turns(), contents(msgs))
	}
	s.Close()
	// Выбор другой политики при следующем запуске читает исходный архив.
	full := open(t, path, false)
	if len(full.Turns()) != 40 || full.Turns()[0].Messages[2].Content != "result1" {
		t.Fatal("window/masking changed the SQLite archive")
	}
}

func TestSummaryCacheReleasesCoveredTurnsOnRestart(t *testing.T) {
	path := filepath.Join(t.TempDir(), "history.db")
	p := Policy{Kind: "summary_system"}
	s, err := OpenWithPolicy(path, false, p, defaultStorage)
	if err != nil {
		t.Fatal(err)
	}
	for i := 1; i <= 40; i++ {
		s.AppendTurn(turn(i, false))
	}
	if err := s.SetSummary("summary", 38, 41); err != nil {
		t.Fatal(err)
	}
	if s.TurnCount() != 40 || len(s.Turns()) != 2 || len(s.ToSummarize(2)) != 0 {
		t.Fatalf("archive=%d cache=%+v", s.TurnCount(), s.Turns())
	}
	s.Close()
	s, err = OpenWithPolicy(path, false, p, defaultStorage)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	sum, msgs := s.View(p)
	if s.TurnCount() != 40 || len(s.Turns()) != 2 || sum != "summary" || contents(msgs) != "q39,a39,q40,a40" {
		t.Fatalf("restart archive=%d cache=%+v summary=%q view=%s", s.TurnCount(), s.Turns(), sum, contents(msgs))
	}
}

func TestStoragePragmas(t *testing.T) {
	pragmas := func(s *Store) string {
		var journal string
		var sync, cache int
		s.db.QueryRow("PRAGMA journal_mode").Scan(&journal)
		s.db.QueryRow("PRAGMA synchronous").Scan(&sync)
		s.db.QueryRow("PRAGMA cache_size").Scan(&cache)
		return fmt.Sprintf("%s/%d/%d", journal, sync, cache)
	}
	dir := t.TempDir()
	s, err := OpenWithPolicy(filepath.Join(dir, "a.db"), false, Policy{Kind: "full"}, defaultStorage)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if got := pragmas(s); got != "wal/1/-256" {
		t.Fatalf("default pragmas %s, want wal/1 (NORMAL)/-256", got)
	}
	s2, err := OpenWithPolicy(filepath.Join(dir, "b.db"), false, Policy{Kind: "full"},
		Storage{CacheKiB: 64, JournalMode: "truncate", Synchronous: "full"})
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()
	if got := pragmas(s2); got != "truncate/2/-64" {
		t.Fatalf("pragmas %s, want truncate/2 (FULL)/-64", got)
	}
}
