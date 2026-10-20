package userctx

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/local/edgectx/internal/config"
	"github.com/local/edgectx/internal/llm"
)

func TestInspectionReadsDurableArchiveAndClearSurvivesReopen(t *testing.T) {
	path := filepath.Join(t.TempDir(), "context.db")
	p := Policy{Kind: "window", WindowTurns: 1}
	s, err := OpenWithPolicy(path, false, p, StorageOf(config.Default().UserCtx))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.AddFact("f1", "keep this", 0); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 4; i++ {
		if _, err = s.AppendTurn(Turn{Kind: "user", Messages: []llm.Message{{Role: "user", Content: "history"}}}); err != nil {
			t.Fatal(err)
		}
	}
	archive, err := s.RecentArchive(50)
	if err != nil || len(archive) != 4 {
		t.Fatalf("%v %v", archive, err)
	}
	if len(s.Turns()) >= len(archive) {
		t.Fatal("test did not distinguish cache and archive")
	}
	if err = s.SetSummary("old summary", 3, 0); err != nil {
		t.Fatal(err)
	}
	if err = s.ClearHistory(); err != nil {
		t.Fatal(err)
	}
	s.Close()
	s, err = OpenWithPolicy(path, false, p, StorageOf(config.Default().UserCtx))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if s.TurnCount() != 0 || len(s.Facts()) != 1 {
		t.Fatal("clear did not persist or deleted fact")
	}
	if text, _ := s.Summary(); text != "" {
		t.Fatal("summary survived clear")
	}
}

func TestInspectionOmitsLargePayloadWithoutChangingArchive(t *testing.T) {
	s, err := Open(":memory:", false)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	large := strings.Repeat("x", 300<<10)
	if _, err = s.AppendTurn(Turn{Kind: "user", Messages: []llm.Message{{Role: "user", Content: large}}}); err != nil {
		t.Fatal(err)
	}
	view, err := s.RecentArchive(50)
	if err != nil || len(view) != 1 || !view[0].InspectionTruncated || view[0].OriginalMessageBytes < 300<<10 || len(view[0].Messages) != 0 {
		t.Fatal(view, err)
	}
	if got := s.Turns()[0].Messages[0].Content; got != large {
		t.Fatal("inspection changed durable/cached content")
	}
	for i := 0; i < 4; i++ {
		if _, err = s.AppendTurn(Turn{Kind: "user", Messages: []llm.Message{{Role: "user", Content: strings.Repeat("y", 200<<10)}}}); err != nil {
			t.Fatal(err)
		}
	}
	view, err = s.RecentArchive(50)
	if err != nil {
		t.Fatal(err)
	}
	kept, omitted := 0, 0
	for _, turn := range view {
		if turn.InspectionTruncated {
			omitted++
		} else {
			kept += turn.OriginalMessageBytes
		}
	}
	if kept > 384<<10 || omitted < 3 || s.TurnCount() != 5 {
		t.Fatal(kept, omitted, s.TurnCount())
	}
}
