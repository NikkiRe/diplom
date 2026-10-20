package agent

import (
	"crypto/sha256"
	"fmt"

	"github.com/local/edgectx/internal/canon"
	"github.com/local/edgectx/internal/llm"
	"github.com/local/edgectx/internal/prompt"
	"github.com/local/edgectx/internal/trace"
)

// promptSnapshot describes exactly the input messages/tools, never the model's
// private reasoning or an inferred mental state. Hash covers the canonical
// {messages,tools} object, not HTTP headers, credentials or model parameters.
// Oversized snapshots keep only a digest: partial text must not look complete.
func promptSnapshot(built prompt.Built, tools []llm.Tool, limit int) trace.F {
	payload := trace.F{"messages": built.Messages, "tools": tools}
	raw, err := canon.Marshal(payload)
	if err != nil {
		return trace.F{"version": 1, "complete": false, "error": "serialization"}
	}
	out := trace.F{"version": 1, "sha256": fmt.Sprintf("%x", sha256.Sum256(raw)),
		"bytes": len(raw), "complete": len(raw) <= limit, "hash_scope": "canonical_messages_tools"}
	if len(raw) <= limit {
		out["messages"], out["tools"] = built.Messages, tools
	} else {
		out["omitted_reason"] = "byte_limit"
	}
	return out
}
