package agent

import (
	"strings"
	"testing"

	"github.com/local/edgectx/internal/llm"
	"github.com/local/edgectx/internal/prompt"
	"github.com/local/edgectx/internal/trace"
)

func TestPromptSnapshotExactAndBounded(t *testing.T) {
	b := prompt.Build(prompt.Input{System: "system", State: "sensor=0", Turn: []llm.Message{{Role: "user", Content: "hello"}}})
	s := promptSnapshot(b, nil, 4096)
	if s["complete"] != true || s["bytes"].(int) <= 0 || len(s["sha256"].(string)) != 64 {
		t.Fatalf("snapshot %+v", s)
	}
	changed := b
	changed.Messages = append([]llm.Message(nil), b.Messages...)
	changed.Messages[len(changed.Messages)-1].Content = "different"
	if promptSnapshot(changed, nil, 4096)["sha256"] == s["sha256"] {
		t.Fatal("changed context has same digest")
	}
	limited := promptSnapshot(b, nil, 1)
	if limited["complete"] != false || limited["messages"] != nil || limited["sha256"] != s["sha256"] {
		t.Fatalf("oversized snapshot %+v", limited)
	}
}

func TestContextCaptureOptIn(t *testing.T) {
	for _, enabled := range []bool{false, true} {
		cfg := "policy: {wake: gate}\n"
		if enabled {
			cfg += "trace_context: true\n"
		}
		f := newAgent(t, cfg, func(llm.Request, map[string]string) (*llm.Response, error) {
			return textResp("done", 10), nil
		})
		f.user(t, "unique user input")
		req := f.records(trace.LLMRequest)[0]
		s, ok := req["context_snapshot"].(map[string]any)
		if ok != enabled {
			t.Fatalf("enabled=%v snapshot=%v", enabled, req)
		}
		if enabled {
			if s["complete"] != true {
				t.Fatalf("incomplete snapshot: %v", s)
			}
			found := false
			for _, m := range s["messages"].([]any) {
				if strings.Contains(m.(map[string]any)["content"].(string), "unique user input") {
					found = true
				}
			}
			if !found {
				t.Fatal("user input missing from captured prompt")
			}
		}
	}
}
