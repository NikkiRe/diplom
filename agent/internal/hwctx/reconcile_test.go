package hwctx

import (
	"strings"
	"testing"
)

func TestExplicitRefreshReconcilesRemovedDeviceAndPropertyOnly(t *testing.T) {
	c := New("delta_tail", 60, 10)
	old := Inventory{Devices: []Device{{ID: "meter", Props: []Prop{{Name: "zero"}, {Name: "off"}}},
		{ID: "removed", Props: []Prop{{Name: "reading"}}}}}
	c.SetInventory(old)
	c.Seed(0, "meter", map[string]any{"zero": 0, "off": false, "unknown": 7})
	c.Seed(0, "removed", map[string]any{"reading": 123})
	c.Seed(0, "unlisted", map[string]any{"valid": 9})
	_, shown := c.DeltaText(0)
	c.Commit(shown)
	current := Inventory{Devices: []Device{{ID: "meter", Props: []Prop{{Name: "zero"}}}}}
	c.SetInventory(current)
	c.Seed(1, "meter", map[string]any{"zero": 42})
	c.ReconcileObservedInventory(old)
	state := c.State()
	if _, ok := state["removed"]; ok {
		t.Fatal("removed device remains current")
	}
	if _, ok := state["meter"]["off"]; ok {
		t.Fatal("removed property remains current")
	}
	if state["meter"]["zero"].Value != 42 || state["meter"]["unknown"].Value != 7 || state["unlisted"]["valid"].Value != 9 {
		t.Fatal("refresh changed valid known/unknown values", state)
	}
	text, _ := c.DeltaText(1)
	if strings.Contains(text, "removed") || strings.Contains(text, "off=false") {
		t.Fatal(text)
	}
	// A reappearing false/zero must be sent again, rather than suppressed by an
	// obsolete last-shown cache entry.
	c.SetInventory(old)
	c.Seed(2, "meter", map[string]any{"off": false})
	c.Seed(2, "removed", map[string]any{"reading": 0})
	c.ReconcileObservedInventory(current)
	text, _ = c.DeltaText(2)
	if !strings.Contains(text, "off=false") || !strings.Contains(text, "removed.reading=0") {
		t.Fatal(text)
	}
}
