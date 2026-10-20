package mcphost

import "context"

// RefreshReadCatalog explicitly reloads tool/resource descriptions without
// calling equipment tools or emitting resource updates. Inventory changes such
// as a removed state URI must take effect before the next explicit state read.
func (h *Host) RefreshReadCatalog(ctx context.Context) error {
	for _, info := range h.Servers() {
		server, err := h.live(info.Name)
		if err != nil {
			return err
		}
		if err := h.refresh(ctx, server); err != nil {
			return err
		}
	}
	h.mu.Lock()
	h.rebuildLocked()
	h.mu.Unlock()
	return nil
}
