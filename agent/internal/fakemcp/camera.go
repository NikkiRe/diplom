package fakemcp

import (
	"context"
	"encoding/json"
	"strings"
	"sync"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// Camera — фейковый сервер камеры со сводкой детектора: тот же контракт
// ресурсов, что у «комнаты» (инвентарь …inventory, состояние …/state/<device>),
// но состояние отдаётся шаблоном ресурса cam://state/{device}, а не списком, и
// инструмент read_property совпадает по имени с инструментом «комнаты». Нужен
// для проверки агента с несколькими MCP-серверами одновременно.
type Camera struct {
	mu     sync.Mutex
	people float64
	server *mcp.Server
}

// CameraInventory — инвентарь камеры.
const CameraInventory = `{"devices":[{"id":"cam1","location":"hall","properties":[` +
	`{"name":"people","type":"number","precision":1},{"name":"person","type":"boolean"}]}]}`

// NewCamera создаёт камеру, в кадре никого нет.
func NewCamera() *Camera {
	c := &Camera{}
	s := mcp.NewServer(&mcp.Implementation{Name: "fake-camera", Version: "test"}, &mcp.ServerOptions{
		SubscribeHandler:   func(context.Context, *mcp.SubscribeRequest) error { return nil },
		UnsubscribeHandler: func(context.Context, *mcp.UnsubscribeRequest) error { return nil },
	})
	s.AddTool(&mcp.Tool{Name: "read_property", Description: "Read a camera summary property",
		InputSchema: schema(`{"type":"object","properties":{"device":{"type":"string"},"property":{"type":"string"}},"required":["device","property"]}`)},
		func(context.Context, *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			return text(c.stateJSON()), nil
		})
	s.AddResource(&mcp.Resource{URI: "cam://inventory", Name: "inventory", MIMEType: "application/json"},
		func(context.Context, *mcp.ReadResourceRequest) (*mcp.ReadResourceResult, error) {
			return &mcp.ReadResourceResult{Contents: []*mcp.ResourceContents{{URI: "cam://inventory", Text: CameraInventory}}}, nil
		})
	s.AddResourceTemplate(&mcp.ResourceTemplate{URITemplate: "cam://state/{device}", Name: "state", MIMEType: "application/json"},
		func(_ context.Context, req *mcp.ReadResourceRequest) (*mcp.ReadResourceResult, error) {
			if !strings.HasSuffix(req.Params.URI, "/cam1") {
				return nil, mcp.ResourceNotFoundError(req.Params.URI)
			}
			return &mcp.ReadResourceResult{Contents: []*mcp.ResourceContents{{URI: req.Params.URI, Text: c.stateJSON()}}}, nil
		})
	c.server = s
	return c
}

// Server — MCP-сервер камеры.
func (c *Camera) Server() *mcp.Server { return c.server }

func (c *Camera) stateJSON() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	b, _ := json.Marshal(map[string]any{"device": "cam1", "properties": map[string]any{"people": c.people, "person": c.people > 0}})
	return string(b)
}

// Detect — детектор насчитал people человек: уведомление подписчикам
// cam://state/cam1 (новое состояние — только в ресурсе, без _meta).
func (c *Camera) Detect(ctx context.Context, people float64) error {
	c.mu.Lock()
	c.people = people
	c.mu.Unlock()
	return c.server.ResourceUpdated(ctx, &mcp.ResourceUpdatedNotificationParams{URI: "cam://state/cam1"})
}
