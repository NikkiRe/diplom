// Package server — MCP-сервер robot-mcp поверх официального Go SDK (github.com/modelcontextprotocol/go-sdk):
// 5 инструментов уровня задачи (R-compact v2), ресурсы robot://inventory, robot://state/robot (с
// notifications/resources/updated только на значимые события, новое состояние — в _meta.state) и
// robot://diag (счётчики, не для LLM).
//
// Ответ инструмента — одно текстовое содержимое с каноническим JSON без structuredContent (не удваивать
// трафик MCP; у ros-mcp текст и structuredContent дублируются) и, для look(image=true), картинка JPEG.
// Сведения оркестратора — в _meta запроса tools/call: edgectx/by = rule | llm (итоги задач правил не
// публикуются в done и не будят LLM).
package server

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/robotmcp/internal/canon"
	"github.com/local/robotmcp/internal/robot"
	"github.com/local/robotmcp/internal/rosbridge"
	"github.com/local/robotmcp/internal/tokens"
	"github.com/local/robotmcp/internal/trace"
)

// MetaBy — ключ _meta tools/call: кто начал задачу (llm | rule).
const MetaBy = "edgectx/by"

// URI ресурсов.
const (
	URIInventory = "robot://inventory"
	URIState     = "robot://state/robot"
	URIDiag      = "robot://diag"
)

// Options — параметры сервера.
type Options struct {
	Version          string
	ProtocolVersions []string
	WaitParam        bool // показать в go_to параметр wait (синхронный режим для сравнения в T2)
	NoLookPlace      bool // убрать look(place): каталог −≈57 токенов, «осмотр места» — go_to + look по событию
	Logger           *slog.Logger
	Trace            *trace.Writer
}

// Server — MCP-сервер робота.
type Server struct {
	mcp   *mcp.Server
	r     *robot.Robot
	br    *rosbridge.Client
	opts  Options
	log   *slog.Logger
	tr    *trace.Writer
	tools []*mcp.Tool
	disp  *dispatcher

	mu    sync.Mutex
	calls map[string]*callStat
}

type callStat struct {
	N, Bytes, Tokens, Images, ImageBytes int64
	Ms                                   float64
}

// New строит сервер; события ядра нужно направить в Notify (robot.Options.OnEvent).
func New(r *robot.Robot, br *rosbridge.Client, opts Options) *Server {
	if opts.Logger == nil {
		opts.Logger = slog.New(slog.DiscardHandler)
	}
	if opts.Version == "" {
		opts.Version = "dev"
	}
	s := &Server{r: r, br: br, opts: opts, log: opts.Logger, tr: opts.Trace, calls: map[string]*callStat{}}
	s.mcp = mcp.NewServer(&mcp.Implementation{Name: "robot-mcp", Title: "Mobile robot (task-level) MCP server",
		Version: opts.Version},
		&mcp.ServerOptions{
			Instructions: "Mobile robot (TurtleBot3) with on-board navigation. Tasks run on the robot; " +
				"results and alerts arrive as updates of robot://state/robot.",
			Logger:             opts.Logger,
			SubscribeHandler:   s.subscribe,
			UnsubscribeHandler: func(context.Context, *mcp.UnsubscribeRequest) error { return nil },
			Capabilities: &mcp.ServerCapabilities{Tools: &mcp.ToolCapabilities{},
				Resources: &mcp.ResourceCapabilities{Subscribe: true}},
			SupportedProtocolVersions: opts.ProtocolVersions,
		})
	s.addTools()
	s.addResources()
	s.disp = newDispatcher(s.sendUpdate)
	return s
}

// MCP — сервер SDK (для транспортов).
func (s *Server) MCP() *mcp.Server { return s.mcp }

// Close останавливает рассылку уведомлений.
func (s *Server) Close() { s.disp.close() }

// Notify ставит событие ядра в очередь уведомлений (не блокируется).
func (s *Server) Notify(e robot.Event) { s.disp.enqueue(e) }

// NotifyStats — счётчики рассылки (реплеер ждёт, пока sent догонит mcp.updates агента).
func (s *Server) NotifyStats() map[string]any { return s.disp.stats() }

// ----------------------------------------------------------------------------------------------
// Инструменты

func obj(props map[string]any, required ...string) map[string]any {
	m := map[string]any{"type": "object", "properties": props, "additionalProperties": false}
	if len(required) > 0 {
		m["required"] = required
	}
	return m
}

func boolPtr(b bool) *bool { return &b }

// Tools — каталог (для -print-tools и тестов), по имени.
func (s *Server) Tools() []*mcp.Tool {
	out := append([]*mcp.Tool(nil), s.tools...)
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

type handler func(ctx context.Context, args map[string]any, by string) (map[string]any, *robot.Image)

func (s *Server) addTools() {
	places := s.r.Config().IDs()
	place := map[string]any{"type": "string", "enum": places}
	ro := &mcp.ToolAnnotations{ReadOnlyHint: true, OpenWorldHint: boolPtr(false)}
	act := &mcp.ToolAnnotations{DestructiveHint: boolPtr(false), OpenWorldHint: boolPtr(false)}

	s.add(&mcp.Tool{Name: "status", Annotations: ro, InputSchema: obj(map[string]any{}),
		Description: "Robot state: place (between = moving), nav, battery, alert, task progress, " +
			"free space by lidar sector (m)."},
		func(ctx context.Context, _ map[string]any, _ string) (map[string]any, *robot.Image) {
			return s.r.Status(ctx), nil
		})

	goProps := map[string]any{"place": place}
	desc := "Drive to a named place. Returns at once with eta_s; the result arrives later as a state update."
	if s.opts.WaitParam {
		goProps["wait"] = map[string]any{"type": "boolean", "description": "block until arrived"}
	}
	s.add(&mcp.Tool{Name: "go_to", Annotations: act, InputSchema: obj(goProps, "place"), Description: desc},
		func(ctx context.Context, a map[string]any, by string) (map[string]any, *robot.Image) {
			w, _ := a["wait"].(bool)
			return s.r.GoTo(ctx, a["place"].(string), by, w && s.opts.WaitParam), nil
		})

	s.add(&mcp.Tool{Name: "patrol", Annotations: act,
		InputSchema: obj(map[string]any{
			"places": map[string]any{"type": "array", "items": place, "minItems": 1, "maxItems": 12},
			"look":   map[string]any{"type": "boolean", "description": "detector at each place"},
		}, "places"),
		Description: "Visit places in order on board in one call; the result arrives as a state update."},
		func(ctx context.Context, a map[string]any, by string) (map[string]any, *robot.Image) {
			var ps []string
			for _, p := range a["places"].([]any) {
				ps = append(ps, p.(string))
			}
			look := true
			if v, ok := a["look"].(bool); ok {
				look = v
			}
			return s.r.Patrol(ctx, ps, look, by), nil
		})

	lookProps := map[string]any{"image": map[string]any{"type": "boolean", "description": "attach a 320x240 JPEG (here only)"}}
	lookDesc := "Short summary of what is ahead of the camera. place: go there first (result arrives as a state update)."
	if s.opts.NoLookPlace {
		lookProps["image"] = map[string]any{"type": "boolean", "description": "attach a 320x240 JPEG"}
		lookDesc = "Short summary of what is ahead of the camera."
	} else {
		lookProps["place"] = place
	}
	s.add(&mcp.Tool{Name: "look", Annotations: act, InputSchema: obj(lookProps), Description: lookDesc},
		func(ctx context.Context, a map[string]any, by string) (map[string]any, *robot.Image) {
			p, _ := a["place"].(string)
			img, _ := a["image"].(bool)
			return s.r.Look(ctx, p, img, by)
		})

	s.add(&mcp.Tool{Name: "stop", Annotations: act, InputSchema: obj(map[string]any{}),
		Description: "Cancel the active task and stop."},
		func(ctx context.Context, _ map[string]any, _ string) (map[string]any, *robot.Image) {
			return s.r.Stop(ctx), nil
		})
}

// validate проверяет аргументы по схеме инструмента (типы, enum, обязательные, лишние поля).
func validate(schema map[string]any, args map[string]any) string {
	props, _ := schema["properties"].(map[string]any)
	for k := range args {
		if _, ok := props[k]; !ok {
			return "unknown argument " + k
		}
	}
	if req, ok := schema["required"].([]string); ok {
		for _, k := range req {
			if _, ok := args[k]; !ok {
				return "missing " + k
			}
		}
	}
	for k, v := range args {
		if msg := checkValue(props[k].(map[string]any), v); msg != "" {
			return k + ": " + msg
		}
	}
	return ""
}

func checkValue(sch map[string]any, v any) string {
	switch sch["type"] {
	case "string":
		sv, ok := v.(string)
		if !ok {
			return "want string"
		}
		if enum, ok := sch["enum"].([]string); ok {
			for _, e := range enum {
				if e == sv {
					return ""
				}
			}
			return "unknown place"
		}
	case "boolean":
		if _, ok := v.(bool); !ok {
			return "want boolean"
		}
	case "array":
		arr, ok := v.([]any)
		if !ok {
			return "want array"
		}
		if mn, ok := sch["minItems"].(int); ok && len(arr) < mn {
			return fmt.Sprintf("want at least %d items", mn)
		}
		if mx, ok := sch["maxItems"].(int); ok && len(arr) > mx {
			return fmt.Sprintf("want at most %d items", mx)
		}
		for _, it := range arr {
			if msg := checkValue(sch["items"].(map[string]any), it); msg != "" {
				return msg
			}
		}
	}
	return ""
}

func (s *Server) add(t *mcp.Tool, h handler) {
	s.tools = append(s.tools, t)
	schema := t.InputSchema.(map[string]any)
	s.mcp.AddTool(t, func(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		t0 := time.Now()
		rb0 := s.br.Stats()
		args := map[string]any{}
		var body map[string]any
		var img *robot.Image
		if raw := req.Params.Arguments; len(raw) > 0 && string(raw) != "null" {
			if err := json.Unmarshal(raw, &args); err != nil {
				body = map[string]any{"ok": false, "error": "arguments are not a JSON object"}
			}
		}
		by := robot.ByLLM
		if v, ok := req.Params.Meta[MetaBy].(string); ok && v == robot.ByRule {
			by = robot.ByRule
		}
		if body == nil {
			if msg := validate(schema, args); strings.HasSuffix(msg, "unknown place") {
				body = s.r.UnknownPlace("")
			} else if msg != "" {
				body = map[string]any{"ok": false, "error": msg}
			} else {
				body, img = h(ctx, args, by)
			}
		}
		text := canon.String(body)
		res := &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: text}}}
		if ok, has := body["ok"].(bool); has && !ok {
			res.IsError = true
		}
		if img != nil {
			res.Content = append(res.Content, &mcp.ImageContent{Data: img.JPEG, MIMEType: "image/jpeg"})
		}
		ms := float64(time.Since(t0).Microseconds()) / 1000
		ntok := tokens.Count(text)
		rb1 := s.br.Stats()
		s.mu.Lock()
		cs := s.calls[t.Name]
		if cs == nil {
			cs = &callStat{}
			s.calls[t.Name] = cs
		}
		cs.N++
		cs.Bytes += int64(len(text))
		cs.Tokens += int64(ntok)
		cs.Ms += ms
		if img != nil {
			cs.Images++
			cs.ImageBytes += int64(len(img.JPEG))
		}
		s.mu.Unlock()
		rec := map[string]any{"name": t.Name, "args": args, "by": by, "ms": ms, "text_bytes": len(text),
			"tokens_est": ntok, "is_error": res.IsError, "rb_tcp_rx": rb1.TCPRx - rb0.TCPRx,
			"rb_tcp_tx": rb1.TCPTx - rb0.TCPTx, "text": text}
		if img != nil {
			rec["image"] = map[string]any{"w": img.W, "h": img.H, "bytes": len(img.JPEG)}
		}
		s.tr.Emit("tool_call", rec)
		return res, nil
	})
}

// ----------------------------------------------------------------------------------------------
// Ресурсы и уведомления

func (s *Server) addResource(uri, name, desc string, read func() any) {
	s.mcp.AddResource(&mcp.Resource{URI: uri, Name: name, MIMEType: "application/json", Description: desc},
		func(ctx context.Context, req *mcp.ReadResourceRequest) (*mcp.ReadResourceResult, error) {
			b, err := canon.Marshal(read())
			if err != nil {
				return nil, err
			}
			return &mcp.ReadResourceResult{Contents: []*mcp.ResourceContents{{URI: uri, MIMEType: "application/json",
				Text: string(b)}}}, nil
		})
}

func (s *Server) addResources() {
	s.addResource(URIInventory, "inventory", "Robot device model for the agent: properties, actions, places, limits, costs",
		func() any { return s.r.Inventory() })
	s.addResource(URIState, "state-robot", "Robot state (place, nav, battery_pct, alert, done, seen); subscribe for updates",
		func() any { return s.r.State() })
	s.addResource(URIDiag, "diagnostics", "robot-mcp counters: tool calls, notifications, rosbridge traffic; not for the LLM",
		func() any { return s.Diag() })
}

// Diag — счётчики сервера, ядра и rosbridge.
func (s *Server) Diag() map[string]any {
	s.mu.Lock()
	calls := map[string]any{}
	for k, c := range s.calls {
		calls[k] = map[string]any{"n": c.N, "text_bytes": c.Bytes, "tokens_est": c.Tokens, "images": c.Images,
			"image_bytes": c.ImageBytes, "ms": c.Ms}
	}
	s.mu.Unlock()
	st := s.br.Stats()
	return map[string]any{"calls": calls, "robot": s.r.Diag(), "notify": s.disp.stats(),
		"rosbridge": st.Map(), "connected": s.br.Connected()}
}

func (s *Server) subscribe(ctx context.Context, req *mcp.SubscribeRequest) error {
	switch req.Params.URI {
	case URIState, URIInventory:
		return nil
	}
	return mcp.ResourceNotFoundError(req.Params.URI)
}

// sendUpdate — notifications/resources/updated с новым состоянием в _meta (агенту не нужно читать ресурс).
func (s *Server) sendUpdate(e robot.Event) {
	meta := mcp.Meta{"device": "robot", "state": e.State, "robot/changed": e.Changed, "robot/event": e.Kind}
	if len(e.Detail) > 0 {
		meta["robot/detail"] = e.Detail
	}
	if err := s.mcp.ResourceUpdated(context.Background(),
		&mcp.ResourceUpdatedNotificationParams{URI: URIState, Meta: meta}); err != nil {
		s.log.Warn("resource updated notification failed", "err", err)
	}
}

// ToolsJSON — каталог в виде tools/list (для -print-tools и подсчёта токенов каталога).
func (s *Server) ToolsJSON() ([]byte, error) {
	type tool struct {
		Name        string               `json:"name"`
		Description string               `json:"description"`
		InputSchema any                  `json:"inputSchema"`
		Annotations *mcp.ToolAnnotations `json:"annotations,omitempty"`
	}
	var out []tool
	for _, t := range s.Tools() {
		out = append(out, tool{t.Name, t.Description, t.InputSchema, t.Annotations})
	}
	return json.Marshal(map[string]any{"tools": out})
}

// CatalogTokens — оценка токенов каталога по формуле OpenAI Cookbook (как bench/mockllm/tokens.py:
// count_tools) с оценщиком o200k вместо словаря; точный счёт — tools/analyze.py.
func (s *Server) CatalogTokens() int {
	n := 0
	for _, t := range s.Tools() {
		n += 7 + tokens.Count(t.Name+":"+t.Description)
		props, _ := t.InputSchema.(map[string]any)["properties"].(map[string]any)
		if len(props) > 0 {
			n += 3
			for _, k := range sortedKeys(props) {
				p := props[k].(map[string]any)
				n += 3
				if enum, ok := p["enum"].([]string); ok {
					n -= 3
					for _, e := range enum {
						n += 3 + tokens.Count(e)
					}
				}
				d, _ := p["description"].(string)
				typ, _ := p["type"].(string)
				n += tokens.Count(k + ":" + typ + ":" + d)
			}
		}
	}
	return n + 12
}

func sortedKeys(m map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
