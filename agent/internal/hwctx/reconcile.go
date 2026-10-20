package hwctx

// ReconcileObservedInventory is used after an explicit successful refresh. It
// prunes only data that was previously inventoried and is now absent, preserving
// legitimate unknown devices/fields delivered by otherwise valid resources.
// The historical event log is unchanged; removed values are no longer current.
func (c *Context) ReconcileObservedInventory(previous Inventory) {
	c.mu.Lock()
	defer c.mu.Unlock()
	current := map[string]map[string]bool{}
	for _, device := range c.inv.Devices {
		props := map[string]bool{}
		for _, prop := range device.Props {
			props[prop.Name] = true
		}
		current[device.ID] = props
	}
	for _, device := range previous.Devices {
		props, exists := current[device.ID]
		if !exists {
			for property := range c.state[device.ID] {
				delete(c.sent, device.ID+"."+property)
			}
			delete(c.state, device.ID)
			delete(c.stale, device.ID)
			continue
		}
		for _, property := range device.Props {
			if !props[property.Name] {
				delete(c.state[device.ID], property.Name)
				delete(c.sent, device.ID+"."+property.Name)
			}
		}
	}
	c.snapOK = false
}
