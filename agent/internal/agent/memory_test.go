package agent

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/local/edgectx/internal/llm"
	"github.com/local/edgectx/internal/userctx"
)

func TestExplicitMemoryChangesNextPromptAndPreservesFactsOnClear(t *testing.T) {
	f := newAgent(t, "policy: {wake: gate, history: window, window_turns: 1}\n", func(llm.Request, map[string]string) (*llm.Response, error) { return textResp("ok", 10), nil })
	if _, err := f.a.MutateFact("add", "comfort", "unique-memory-value"); err != nil {
		t.Fatal(err)
	}
	f.user(t, "inspect memory")
	bodies := f.bodies()
	found := false
	for _, m := range bodies[len(bodies)-1]["messages"].([]any) {
		if strings.Contains(m.(map[string]any)["content"].(string), "unique-memory-value") {
			found = true
		}
	}
	if !found {
		t.Fatal("edited durable fact absent from next actual request")
	}
	view, err := f.a.Context()
	if err != nil {
		t.Fatal(err)
	}
	if view["history_turns"].(int) != 1 {
		t.Fatal(view)
	}
	if _, err = f.a.ClearContextHistory(false); err == nil {
		t.Fatal("cleared without explicit confirmation")
	}
	if _, err = f.a.ClearContextHistory(true); err != nil {
		t.Fatal(err)
	}
	view, _ = f.a.Context()
	if view["history_turns"].(int) != 0 || view["fact_count"].(int) != 1 {
		t.Fatal(view)
	}
	if _, err = f.a.MutateFact("update", "comfort", "new preference"); err != nil {
		t.Fatal(err)
	}
	if _, err = f.a.MutateFact("delete", "comfort", ""); err != nil {
		t.Fatal(err)
	}
	view, _ = f.a.Context()
	if view["fact_count"].(int) != 0 {
		t.Fatal(view)
	}
}

func TestMemoryEditCannotSplitActiveTurn(t *testing.T) {
	entered, release := make(chan struct{}), make(chan struct{})
	f := newAgent(t, "policy: {wake: gate}\n", func(llm.Request, map[string]string) (*llm.Response, error) {
		close(entered)
		<-release
		return textResp("ok", 10), nil
	})
	done := make(chan struct{})
	go func() { f.user(t, "blocked request"); close(done) }()
	select {
	case <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("LLM request did not begin")
	}
	_, err := f.a.MutateFact("add", "late", "must wait")
	if !errors.Is(err, ErrContextBusy) {
		t.Errorf("expected busy, got %v", err)
	}
	if _, err = f.a.RefreshContextHardware(context.Background()); !errors.Is(err, ErrContextBusy) {
		t.Errorf("refresh split active turn: %v", err)
	}
	close(release)
	<-done
	if _, err := f.a.MutateFact("add", "late", "now accepted"); err != nil {
		t.Fatal(err)
	}
}

func TestContextInspectionBoundsLegacyPayloadAndMutationSuccess(t *testing.T) {
	f := newAgent(t, "policy: {wake: gate, history: full}\n", func(llm.Request, map[string]string) (*llm.Response, error) {
		t.Fatal("inspection woke model")
		return nil, nil
	})
	large := strings.Repeat("legacy", 100000)
	if _, err := f.store.AddFact("legacy", large, 0); err != nil {
		t.Fatal(err)
	}
	if _, err := f.store.AppendTurn(userctx.Turn{Kind: "user", Messages: []llm.Message{{Role: "assistant", Content: large}}}); err != nil {
		t.Fatal(err)
	}
	if err := f.store.SetSummary(large, 0, 0); err != nil {
		t.Fatal(err)
	}
	f.a.consolidateProfile()
	doc, err := f.a.MutateFact("add", "new", "new actual fact")
	if err != nil || doc["ok"] != true {
		t.Fatal(doc, err)
	}
	raw, err := json.Marshal(doc)
	if err != nil || len(raw) > 1<<20 {
		t.Fatal(len(raw), err)
	}
	if doc["profile_text_omitted"] != true || doc["summary_omitted"] != true || doc["visible_history_omitted"].(int) != 1 {
		t.Fatal(doc)
	}
	if len(f.store.Facts()[0].Text) != len(large) || len(f.store.Turns()[0].Messages[0].Content) != len(large) {
		t.Fatal("inspection changed source")
	}
	if _, err = f.a.RefreshContextHardware(context.Background()); err != nil {
		t.Fatal(err)
	}
}
