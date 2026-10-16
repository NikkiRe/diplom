// Package fakemcp — фейковые MCP-серверы «комната» и «камера» для тестов edgectx.
//
// «Комната» — сервер на официальном go-sdk с каталогом generic (list_devices,
// read_property, write_property, board_health), ресурсами hw://inventory и
// hw://state/<device>, поддержкой подписки и уведомлений resources/updated.
// Запись в реле с недопустимым значением возвращает структурный отказ
// {"denied": true, "reason": …}, как шлюз приводов hw-mcp. Используется в
// тестах mcphost и agent и в интеграционном тесте «мини-день»; в бинарник
// агента не входит.
package fakemcp

import (
	"context"
	"encoding/json"
	"errors"
	"sort"
	"sync"
	"sync/atomic"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// Room — эмулятор помещения.
type Room struct {
	mu     sync.Mutex
	state  map[string]map[string]any
	subs   map[string]bool
	server *mcp.Server
	// MetaState — класть ли новое состояние в _meta уведомления (иначе клиент
	// должен прочитать ресурс сам).
	MetaState bool
	// FailReads — пока true, чтение ресурсов состояния завершается ошибкой
	// (датчики не отвечают).
	FailReads atomic.Bool
}

// Inventory — инвентарь комнаты (формат, который понимает hwctx.ParseInventory).
const Inventory = `{"devices":[
 {"id":"door1","location":"hall","properties":[{"name":"open","type":"boolean","writable":false}]},
 {"id":"pir1","location":"hall","properties":[{"name":"motion","type":"number","writable":false}]},
 {"id":"t1","location":"lab","properties":[{"name":"temperature","unit":"Cel","precision":0.1},{"name":"humidity","unit":"%RH"}]},
 {"id":"relay1","location":"lab","properties":[{"name":"on","type":"boolean","writable":true}],"actions":["pulse"]}
]}`

// NewRoom создаёт комнату с начальным состоянием.
func NewRoom() *Room {
	r := &Room{
		state: map[string]map[string]any{
			"door1":  {"open": false},
			"pir1":   {"motion": 0.0},
			"t1":     {"temperature": 22.0, "humidity": 40.0},
			"relay1": {"on": false},
		},
		subs: map[string]bool{},
	}
	r.server = r.build()
	return r
}

// Server — MCP-сервер комнаты.
func (r *Room) Server() *mcp.Server { return r.server }

func schema(s string) json.RawMessage { return json.RawMessage(s) }

func (r *Room) build() *mcp.Server {
	s := mcp.NewServer(&mcp.Implementation{Name: "fake-room", Version: "test"}, &mcp.ServerOptions{
		SubscribeHandler: func(_ context.Context, req *mcp.SubscribeRequest) error {
			r.mu.Lock()
			r.subs[req.Params.URI] = true
			r.mu.Unlock()
			return nil
		},
		UnsubscribeHandler: func(_ context.Context, req *mcp.UnsubscribeRequest) error {
			r.mu.Lock()
			delete(r.subs, req.Params.URI)
			r.mu.Unlock()
			return nil
		},
	})
	s.AddTool(&mcp.Tool{Name: "list_devices", Description: "List devices and their properties",
		InputSchema: schema(`{"type":"object","properties":{}}`)}, r.listDevices)
	s.AddTool(&mcp.Tool{Name: "read_property", Description: "Read a device property",
		InputSchema: schema(`{"type":"object","properties":{"device":{"type":"string"},"property":{"type":"string"}},"required":["device","property"]}`)}, r.readProperty)
	s.AddTool(&mcp.Tool{Name: "write_property", Description: "Write a writable device property",
		InputSchema: schema(`{"type":"object","properties":{"device":{"type":"string"},"property":{"type":"string"},"value":{}},"required":["device","property","value"]}`)}, r.writeProperty)
	s.AddTool(&mcp.Tool{Name: "board_health", Description: "Board CPU, memory and temperature",
		InputSchema: schema(`{"type":"object","properties":{}}`)}, func(context.Context, *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		return text(`{"cpu_pct": 3, "mem_mb": 512, "temp_c": 41.5}`), nil
	})
	s.AddResource(&mcp.Resource{URI: "hw://inventory", Name: "inventory", MIMEType: "application/json"},
		func(context.Context, *mcp.ReadResourceRequest) (*mcp.ReadResourceResult, error) {
			return &mcp.ReadResourceResult{Contents: []*mcp.ResourceContents{{URI: "hw://inventory", MIMEType: "application/json", Text: Inventory}}}, nil
		})
	for _, d := range r.devices() {
		uri := "hw://state/" + d
		dev := d
		s.AddResource(&mcp.Resource{URI: uri, Name: "state-" + d, MIMEType: "application/json"},
			func(context.Context, *mcp.ReadResourceRequest) (*mcp.ReadResourceResult, error) {
				if r.FailReads.Load() {
					return nil, errors.New("sensor read timeout")
				}
				return &mcp.ReadResourceResult{Contents: []*mcp.ResourceContents{{URI: uri, MIMEType: "application/json", Text: r.stateJSON(dev)}}}, nil
			})
	}
	return s
}

func (r *Room) devices() []string {
	var out []string
	for d := range r.state {
		out = append(out, d)
	}
	sort.Strings(out)
	return out
}

func (r *Room) stateJSON(device string) string {
	r.mu.Lock()
	defer r.mu.Unlock()
	b, _ := json.MarshalIndent(map[string]any{"device": device, "properties": r.state[device]}, "", "  ")
	return string(b)
}

func text(s string) *mcp.CallToolResult {
	return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: s}}}
}

func args(req *mcp.CallToolRequest) map[string]any {
	m := map[string]any{}
	if raw := req.Params.Arguments; len(raw) > 0 {
		json.Unmarshal(raw, &m)
	}
	return m
}

func (r *Room) listDevices(context.Context, *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	return text(Inventory), nil
}

func (r *Room) readProperty(_ context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	a := args(req)
	d, _ := a["device"].(string)
	p, _ := a["property"].(string)
	r.mu.Lock()
	v, ok := r.state[d][p]
	r.mu.Unlock()
	if !ok {
		return &mcp.CallToolResult{IsError: true, Content: []mcp.Content{&mcp.TextContent{Text: "unknown device or property"}}}, nil
	}
	b, _ := json.Marshal(map[string]any{"device": d, "property": p, "value": v})
	return text(string(b)), nil
}

func (r *Room) writeProperty(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	a := args(req)
	d, _ := a["device"].(string)
	p, _ := a["property"].(string)
	if d != "relay1" || p != "on" {
		return &mcp.CallToolResult{StructuredContent: map[string]any{"denied": true, "reason": "not_writable"},
			Content: []mcp.Content{&mcp.TextContent{Text: `{"denied":true,"reason":"not_writable"}`}}}, nil
	}
	v, isBool := a["value"].(bool)
	if !isBool {
		return text(`{"denied": true, "reason": "schema"}`), nil
	}
	r.Set(ctx, d, p, v)
	return text(`{"ok": true}`), nil
}

// Set меняет состояние и уведомляет подписчиков ресурса hw://state/<device>.
func (r *Room) Set(ctx context.Context, device, prop string, value any) error {
	r.mu.Lock()
	if r.state[device] == nil {
		r.state[device] = map[string]any{}
	}
	r.state[device][prop] = value
	meta := mcp.Meta(nil)
	if r.MetaState {
		props := map[string]any{}
		for k, v := range r.state[device] {
			props[k] = v
		}
		meta = mcp.Meta{"state": props}
	}
	r.mu.Unlock()
	return r.server.ResourceUpdated(ctx, &mcp.ResourceUpdatedNotificationParams{URI: "hw://state/" + device, Meta: meta})
}

// Get — текущее значение.
func (r *Room) Get(device, prop string) any {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.state[device][prop]
}

// Subscribed — URI, на которые подписан клиент.
func (r *Room) Subscribed() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	var out []string
	for u := range r.subs {
		out = append(out, u)
	}
	sort.Strings(out)
	return out
}
