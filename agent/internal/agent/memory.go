package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/local/edgectx/internal/llm"
	"github.com/local/edgectx/internal/userctx"
)

var ErrContextBusy = errors.New("контекст занят текущим ходом агента")

type inspectedFact struct {
	userctx.Fact
	TextOmitted       bool `json:"text_omitted,omitempty"`
	OriginalTextBytes int  `json:"original_text_bytes,omitempty"`
}

// RefreshContextHardware reads the current published inventory/state without
// waking the model. It is explicit and cannot split an active reasoning turn.
func (a *Agent) RefreshContextHardware(ctx context.Context) (map[string]any, error) {
	if !a.memoryMu.TryLock() {
		return nil, ErrContextBusy
	}
	defer a.memoryMu.Unlock()
	if a.isClosed() {
		return nil, ErrClosed
	}
	previous := a.hw.Inventory()
	if err := a.host.RefreshReadCatalog(ctx); err != nil {
		return nil, err
	}
	if err := a.syncHardwareMode(ctx, true); err != nil {
		return nil, err
	}
	a.hw.ReconcileObservedInventory(previous)
	a.rebuildTools()
	a.m.inc("context.hardware_refreshes")
	return a.Context()
}

// Context exposes persisted memory and its prompt projection, never private
// model reasoning. Inspection is bounded; the durable archive is unchanged.
func (a *Agent) Context() (map[string]any, error) {
	turns, err := a.store.RecentArchive(50)
	if err != nil {
		return nil, err
	}
	summary, upto := a.store.Summary()
	facts := a.store.Facts()
	factCount := len(facts)
	if len(facts) > 256 {
		facts = facts[:256]
	}
	inspectedFacts := []inspectedFact{}
	factBudget := 256 << 10
	for _, fact := range facts {
		if len(fact.ID) > 512 {
			continue
		}
		item := inspectedFact{Fact: fact, OriginalTextBytes: len(fact.Text)}
		raw, err := json.Marshal(fact)
		if err != nil || len(raw) > factBudget {
			item.Text = ""
			item.TextOmitted = true
		} else {
			factBudget -= len(raw)
		}
		inspectedFacts = append(inspectedFacts, item)
	}
	visibleSummary, visible := a.store.View(userctx.PolicyOf(a.cfg.Policy))
	visibleCount := len(visible)
	if len(visible) > 100 {
		visible = visible[len(visible)-100:]
	}
	// Byte bounds complement item limits; one large model/tool message must not
	// hide all profile controls or allocate an unbounded HTTP response.
	remaining := 384 << 10
	bounded := []llm.Message{}
	for i := len(visible) - 1; i >= 0; i-- {
		raw, err := json.Marshal(visible[i])
		if err != nil || len(raw) > remaining {
			continue
		}
		bounded = append(bounded, visible[i])
		remaining -= len(raw)
	}
	for i, j := 0, len(bounded)-1; i < j; i, j = i+1, j-1 {
		bounded[i], bounded[j] = bounded[j], bounded[i]
	}
	visible = bounded
	a.mu.Lock()
	profile := a.profileText
	a.mu.Unlock()
	profileBytes, summaryBytes, visibleSummaryBytes := len(profile), len(summary), len(visibleSummary)
	if profileBytes > 128<<10 {
		profile = ""
	}
	if summaryBytes > 32<<10 {
		summary = ""
	}
	if visibleSummaryBytes > 32<<10 {
		visibleSummary = ""
	}
	return map[string]any{"facts": inspectedFacts, "fact_count": factCount, "facts_omitted": factCount - len(inspectedFacts),
		"summary": summary, "summary_upto": upto, "turns": turns, "history_turns": a.store.TurnCount(),
		"archive_limit": 50, "visible_history": visible, "visible_history_omitted": visibleCount - len(visible),
		"visible_summary": visibleSummary, "visible_summary_omitted": visibleSummaryBytes > 32<<10,
		"summary_omitted": summaryBytes > 32<<10, "summary_bytes": summaryBytes,
		"profile_text": profile, "profile_text_omitted": profileBytes > 128<<10, "profile_text_bytes": profileBytes,
		"policy": map[string]any{"history": a.cfg.Policy.History, "window_turns": a.cfg.Policy.WindowTurns,
			"mask_keep_turns": a.cfg.Policy.MaskKeepTurns, "history_batch_turns": a.cfg.Policy.HistoryBatchTurns},
		"hardware": map[string]any{"devices": len(a.hw.Inventory().Devices)}, "busy": !a.Idle()}, nil
}

func (a *Agent) MutateFact(operation, id, text string) (map[string]any, error) {
	if !a.memoryMu.TryLock() {
		return nil, ErrContextBusy
	}
	defer a.memoryMu.Unlock()
	if a.isClosed() {
		return nil, ErrClosed
	}
	if len(id) > 128 || len(text) > 4096 {
		return nil, fmt.Errorf("id <=128 bytes, text <=4096 bytes")
	}
	var err error
	switch strings.ToLower(operation) {
	case "add":
		if len(a.store.Facts()) >= 256 {
			return nil, fmt.Errorf("предел 256 фактов профиля")
		}
		_, err = a.store.AddFact(id, text, a.now())
	case "update":
		err = a.store.UpdateFact(id, text, a.now())
	case "delete":
		err = a.store.DeleteFact(id)
	default:
		return nil, fmt.Errorf("operation: add, update или delete")
	}
	if err != nil {
		return nil, err
	}
	a.consolidateProfile()
	return a.contextAfterMutation()
}

func (a *Agent) ClearContextHistory(confirm bool) (map[string]any, error) {
	if !confirm {
		return nil, fmt.Errorf("для очистки истории требуется confirm: true")
	}
	if !a.memoryMu.TryLock() {
		return nil, ErrContextBusy
	}
	defer a.memoryMu.Unlock()
	if a.isClosed() {
		return nil, ErrClosed
	}
	if err := a.store.ClearHistory(); err != nil {
		return nil, err
	}
	return a.contextAfterMutation()
}

func (a *Agent) contextAfterMutation() (map[string]any, error) {
	doc, err := a.Context()
	if err != nil {
		return map[string]any{"ok": true, "inspection_error": err.Error()}, nil
	}
	doc["ok"] = true
	return doc, nil
}
