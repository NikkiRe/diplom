// Package server — MCP-сервер cam-mcp поверх официального Go SDK (github.com/modelcontextprotocol/go-sdk).
//
// Контракт тот же, что у hw-mcp (IMPLEMENTATION_SPEC §4), поэтому агент edgectx подключает камеру без
// изменений: инвентарь cam://inventory ({"devices": [...]}, свойства с типом и единицей), состояние
// cam://state/<камера> ({"device", "now", "properties": {имя: {"value", "unit", "ts", "age_s"}}}) и
// notifications/resources/updated с новыми значениями в _meta ("device", "state"). Уведомление
// приходит только на начало и конец эпизода движения и на отказ и возврат камеры, а не на каждый кадр.
//
// Инструменты: camera_status, motion_state, snapshot (маленький JPEG — ImageContent), set_detector.
// Аргументы проверяет SDK по входной схеме с пределами. Ответ — одно текстовое содержимое с
// каноническим JSON без structuredContent (не удваивать трафик), у snapshot — ещё картинка.
package server

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"sort"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/cammcp/internal/camera"
	"github.com/local/cammcp/internal/canon"
	"github.com/local/cammcp/internal/trace"
)

// URIInventory — ресурс инвентаря.
const URIInventory = "cam://inventory"

// Пределы снимка: ширина и качество, которые конфигурация задаёт как наибольшие (и по умолчанию), а
// инструмент snapshot позволяет LLM уменьшить до минимума.
const (
	minSnapshotWidth   = 64
	maxSnapshotWidth   = 1280
	minSnapshotQuality = 20
	maxSnapshotQuality = 95
)

// Options — параметры сервера.
type Options struct {
	Version          string
	ProtocolVersions []string
	Location         string // место установки камеры (инвентарь)
	SnapshotWidth    int    // наибольшая ширина снимка и ширина по умолчанию
	SnapshotQuality  int    // наибольшее качество JPEG и качество по умолчанию
	// SetDetector добавляет инструмент set_detector: ≈85 токенов каталога в каждом запросе к LLM ради
	// редкой подстройки (штатная оценка стенда, см. README), поэтому он включается явно.
	SetDetector bool
	Logger      *slog.Logger
	Trace       *trace.Writer
}

// Server — MCP-сервер камеры.
type Server struct {
	mcp   *mcp.Server
	cam   *camera.Camera
	opts  Options
	uri   string // cam://state/<камера>
	tools []*mcp.Tool
	disp  *dispatcher
}

// New строит сервер; события камеры нужно направить в Notify (camera.Options.OnEvent).
func New(cam *camera.Camera, opts Options) (*Server, error) {
	if opts.SnapshotWidth < minSnapshotWidth || opts.SnapshotWidth > maxSnapshotWidth ||
		opts.SnapshotQuality < minSnapshotQuality || opts.SnapshotQuality > maxSnapshotQuality {
		return nil, fmt.Errorf("snapshot width must be in [%d, %d], quality in [%d, %d]",
			minSnapshotWidth, maxSnapshotWidth, minSnapshotQuality, maxSnapshotQuality)
	}
	if opts.Logger == nil {
		opts.Logger = slog.New(slog.DiscardHandler)
	}
	if opts.Version == "" {
		opts.Version = "dev"
	}
	s := &Server{cam: cam, opts: opts, uri: "cam://state/" + cam.Device()}
	s.mcp = mcp.NewServer(&mcp.Implementation{Name: "cam-mcp", Title: "Camera with on-board motion detector",
		Version: opts.Version},
		&mcp.ServerOptions{
			Instructions: "Camera with an on-board motion detector. Frames are not sent: motion episodes arrive as " +
				"updates of " + s.uri + " (start and end only). Call snapshot only when the motion summary is not enough.",
			Logger:             opts.Logger,
			SubscribeHandler:   s.subscribe,
			UnsubscribeHandler: func(context.Context, *mcp.UnsubscribeRequest) error { return nil },
			Capabilities: &mcp.ServerCapabilities{Tools: &mcp.ToolCapabilities{},
				Resources: &mcp.ResourceCapabilities{Subscribe: true}},
			SupportedProtocolVersions: opts.ProtocolVersions,
		})
	s.mcp.AddReceivingMiddleware(s.traceCalls)
	s.addTools()
	s.addResources()
	s.disp = newDispatcher(s.sendUpdate)
	return s, nil
}

// MCP — сервер SDK (для транспортов).
func (s *Server) MCP() *mcp.Server { return s.mcp }

// Notify ставит событие камеры в очередь уведомлений (не блокируется).
func (s *Server) Notify(e camera.Event) { s.disp.enqueue(e) }

// NotifyStats — счётчики рассылки.
func (s *Server) NotifyStats() map[string]any { return s.disp.stats() }

// Close останавливает рассылку, не дожидаясь отправки, зависшей на клиенте.
func (s *Server) Close() { s.disp.close() }

// ----------------------------------------------------------------------------------------------
// Инструменты

// result — ответ инструмента: тело (канонический JSON) и, для snapshot, JPEG.
type result struct {
	body  any
	isErr bool
	image []byte
}

func fail(code, detail string) result {
	return result{body: map[string]any{"error": code, "detail": detail}, isErr: true}
}

func (r result) toMCP() *mcp.CallToolResult {
	text, err := canon.Marshal(r.body)
	if err != nil {
		text = []byte(fmt.Sprintf(`{"error":"internal","detail":%q}`, err.Error()))
	}
	res := &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: string(text)}}, IsError: r.isErr}
	if r.image != nil {
		res.Content = append(res.Content, &mcp.ImageContent{Data: r.image, MIMEType: "image/jpeg"})
	}
	return res
}

// add регистрирует инструмент: SDK проверяет аргументы по схеме (с пределами и значениями по
// умолчанию) и разбирает их в In; structuredContent не отдаётся.
func add[In any](s *Server, t *mcp.Tool, h func(In) result) {
	s.tools = append(s.tools, t)
	mcp.AddTool(s.mcp, t, func(ctx context.Context, req *mcp.CallToolRequest, in In) (*mcp.CallToolResult, any, error) {
		return h(in).toMCP(), nil, nil
	})
}

func object(props map[string]any) map[string]any {
	return map[string]any{"type": "object", "properties": props, "additionalProperties": false}
}

// number — схема числа с пределами; пустое описание не пишется (каталог — в каждом запросе к LLM).
func number(typ string, lo, hi float64, desc string) map[string]any {
	m := map[string]any{"type": typ, "minimum": lo, "maximum": hi}
	if desc != "" {
		m["description"] = desc
	}
	return m
}

func readOnly() *mcp.ToolAnnotations {
	f := false
	return &mcp.ToolAnnotations{ReadOnlyHint: true, OpenWorldHint: &f}
}

type snapshotArgs struct {
	MaxWidth int `json:"max_width"`
	Quality  int `json:"quality"`
}

type detectorArgs struct {
	Threshold  *int     `json:"threshold"`
	MinAreaPct *float64 `json:"min_area_pct"`
	K          *int     `json:"k"`
	N          *int     `json:"n"`
	IdleFPS    *float64 `json:"idle_fps"`
	ActiveFPS  *float64 `json:"active_fps"`
}

func (s *Server) addTools() {
	add(s, &mcp.Tool{Name: "camera_status", Annotations: readOnly(), InputSchema: object(map[string]any{}),
		Description: "Camera and on-board detector status: frame rates, frames, CPU per frame"},
		func(struct{}) result { return result{body: s.cam.Status()} })
	add(s, &mcp.Tool{Name: "motion_state", Annotations: readOnly(), InputSchema: object(map[string]any{}),
		Description: "Motion from the on-board detector (no image): current frame and episode"},
		func(struct{}) result { return result{body: s.cam.Motion()} })

	w, q := s.opts.SnapshotWidth, s.opts.SnapshotQuality
	wSchema := number("integer", minSnapshotWidth, float64(w), "")
	wSchema["default"] = w
	qSchema := number("integer", minSnapshotQuality, float64(q), "JPEG")
	qSchema["default"] = q
	add(s, &mcp.Tool{Name: "snapshot", Annotations: readOnly(),
		InputSchema: object(map[string]any{"max_width": wSchema, "quality": qSchema}),
		Description: "Current frame: small grayscale JPEG with the motion box. Costs image tokens: " +
			"use only if motion_state is not enough"},
		func(a snapshotArgs) result {
			img, meta, err := s.cam.Snapshot(a.MaxWidth, a.Quality)
			if err != nil {
				return fail("snapshot_failed", err.Error())
			}
			return result{body: meta, image: img}
		})

	if !s.opts.SetDetector {
		return
	}
	f := false
	add(s, &mcp.Tool{Name: "set_detector",
		Annotations: &mcp.ToolAnnotations{DestructiveHint: &f, IdempotentHint: true, OpenWorldHint: &f},
		InputSchema: object(map[string]any{
			"threshold":    number("integer", 8, 64, "brightness difference"),
			"min_area_pct": number("number", 0.1, 25, "% of frame"),
			"k":            number("integer", 1, 10, "start: k moving of last n frames"),
			"n":            number("integer", 1, 20, "end: n still frames"),
			"idle_fps":     number("number", 0.2, 10, ""),
			"active_fps":   number("number", 0.5, 15, ""),
		}),
		Description: "Tune the on-board motion detector; omitted fields keep their values"},
		s.setDetector)
}

func (s *Server) setDetector(a detectorArgs) result {
	p, idle, active := s.cam.Params()
	set := func(dst *int, src *int) {
		if src != nil {
			*dst = *src
		}
	}
	set(&p.Threshold, a.Threshold)
	set(&p.K, a.K)
	set(&p.N, a.N)
	if a.MinAreaPct != nil {
		p.MinArea = *a.MinAreaPct / 100
	}
	if a.IdleFPS != nil {
		idle = *a.IdleFPS
	}
	if a.ActiveFPS != nil {
		active = *a.ActiveFPS
	}
	if err := s.cam.SetParams(p, idle, active); err != nil {
		return fail("invalid_args", err.Error())
	}
	return result{body: s.cam.Status()["detector"]}
}

// traceCalls пишет в журнал каждый tools/call (и отвергнутый по схеме): имя, аргументы, время, байты
// текста и картинки — трафик MCP входит в метрику затрат.
func (s *Server) traceCalls(next mcp.MethodHandler) mcp.MethodHandler {
	return func(ctx context.Context, method string, req mcp.Request) (mcp.Result, error) {
		call, ok := req.(*mcp.CallToolRequest)
		if !ok || s.opts.Trace == nil {
			return next(ctx, method, req)
		}
		t0 := time.Now()
		res, err := next(ctx, method, req)
		rec := map[string]any{"name": call.Params.Name, "args": json.RawMessage(nonEmpty(call.Params.Arguments)),
			"ms": float64(time.Since(t0).Microseconds()) / 1000}
		if err != nil {
			rec["error"] = err.Error()
		}
		if r, ok := res.(*mcp.CallToolResult); ok && r != nil {
			rec["is_error"] = r.IsError
			for _, c := range r.Content {
				switch x := c.(type) {
				case *mcp.TextContent:
					rec["text_bytes"] = len(x.Text)
				case *mcp.ImageContent:
					rec["image_bytes"] = len(x.Data)
				}
			}
		}
		s.opts.Trace.Emit("tool_call", rec)
		return res, err
	}
}

func nonEmpty(raw json.RawMessage) json.RawMessage {
	if len(raw) == 0 {
		return json.RawMessage("null")
	}
	return raw
}

// Tools — зарегистрированные инструменты по имени (для -print-tools и тестов).
func (s *Server) Tools() []*mcp.Tool {
	out := append([]*mcp.Tool(nil), s.tools...)
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// ToolsJSON — каталог в виде tools/list (для -print-tools и подсчёта токенов каталога).
func (s *Server) ToolsJSON() ([]byte, error) {
	type tool struct {
		Name        string `json:"name"`
		Description string `json:"description"`
		InputSchema any    `json:"inputSchema"`
	}
	var out []tool
	for _, t := range s.Tools() {
		out = append(out, tool{t.Name, t.Description, t.InputSchema})
	}
	return json.Marshal(map[string]any{"tools": out})
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
	s.addResource(URIInventory, "inventory", "Camera device model for the agent: properties, units, source",
		s.inventory)
	s.addResource(s.uri, "state-"+s.cam.Device(),
		"Motion state of the camera with age; subscribe: updates at episode start and end only",
		func() any { return s.cam.State() })
}

// inventory — модель устройства в формате hw://inventory (агент разбирает её тем же кодом).
func (s *Server) inventory() any {
	info := s.cam.Info()
	p, _, _ := s.cam.Params()
	dev := map[string]any{"id": s.cam.Device(), "kind": "camera",
		"description": "on-board motion detector; frame only via snapshot",
		"properties": map[string]any{
			"motion": map[string]any{"type": "boolean", "writable": false, "description": "motion episode in progress"},
			"area_pct": map[string]any{"type": "number", "unit": "%", "writable": false, "precision": 0.1,
				"description": "peak area of the moving region in the current or last episode"},
			"zone": map[string]any{"type": "string", "writable": false, "enum": camera.Zones(),
				"description": "part of the frame (3x3) with the motion"},
		}}
	if s.opts.Location != "" {
		dev["location"] = s.opts.Location
	}
	return map[string]any{"devices": []any{dev}, "camera": map[string]any{"source": info.Kind, "w": info.W,
		"h": info.H, "format": info.Format, "detector_w": p.W, "detector_h": p.H}}
}

func (s *Server) subscribe(ctx context.Context, req *mcp.SubscribeRequest) error {
	if req.Params.URI != s.uri {
		return mcp.ResourceNotFoundError(req.Params.URI)
	}
	return nil
}

// sendUpdate — notifications/resources/updated подписчикам состояния (из горутины рассылки; без
// подписчиков SDK никому не шлёт, а журнал всё равно получает notify — как у hw-mcp). _meta: "device" и
// "state" (новые значения — читать ресурс не нужно), "cam/event" (motion_start | motion_end |
// camera_lost | camera_back), "cam/changed" и "cam/episode" (сводка эпизода, если он есть).
func (s *Server) sendUpdate(e camera.Event) {
	meta := mcp.Meta{"device": s.cam.Device(), "state": e.State, "cam/event": e.Kind, "cam/changed": e.Changed}
	if e.Episode != nil {
		meta["cam/episode"] = e.Episode
	}
	if err := s.mcp.ResourceUpdated(context.Background(),
		&mcp.ResourceUpdatedNotificationParams{URI: s.uri, Meta: meta}); err != nil {
		s.opts.Logger.Warn("resource updated notification failed", "err", err)
	}
	s.opts.Trace.Emit("notify", map[string]any{"uri": s.uri, "event": e.Kind, "ts_clock": e.T})
}
