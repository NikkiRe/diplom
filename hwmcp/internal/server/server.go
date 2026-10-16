// Package server — MCP-сервер hw-mcp поверх официального Go SDK (github.com/modelcontextprotocol/go-sdk).
//
// Инструменты (IMPLEMENTATION_SPEC §4):
//   - generic: list_devices, read_property, write_property, invoke_action (+ board_health);
//   - explicit: read_<устройство>_<свойство>, set_<устройство>_<свойство>, <действие>_<устройство>
//     (+ board_health) — строятся из инвентаря.
//
// Ресурсы: hw://inventory (модель для агента), hw://inventory/text (проекция L0), hw://td (WoT TD 1.1),
// hw://probe (сырой инвентарь sysfs), hw://diag (счётчики hw-mcp), hw://state/<устройство> — с
// notifications/resources/updated при изменении (после resources/subscribe). Уведомление несёт новые
// значения в _meta (state, device, hw/changed), так что клиенту не нужно читать ресурс; рассылка идёт
// из отдельной горутины (dispatch.go) и не задерживает опрос и сторожевой таймер.
//
// Результат инструмента — одно текстовое содержимое с каноническим JSON. Отказ шлюза — isError и
// {"denied": true, "verdict": ..., "reason": ..., "detail": ...}. Сведения, которые даёт оркестратор,
// а не LLM (ключ идемпотентности, момент решения, подтверждение пользователя, dry-run), передаются
// в _meta запроса tools/call — LLM формирует только arguments и подделать их не может.
package server

import (
	"context"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/hwmcp/internal/argschema"
	"github.com/local/hwmcp/internal/canon"
	"github.com/local/hwmcp/internal/gate"
	"github.com/local/hwmcp/internal/hub"
	"github.com/local/hwmcp/internal/inventory"
	"github.com/local/hwmcp/internal/sysfs"
)

// Каталоги инструментов.
const (
	CatalogGeneric  = "generic"
	CatalogExplicit = "explicit"
)

// Ключи _meta запроса tools/call (задаёт оркестратор).
const (
	MetaCmdID     = "edgectx/cmd_id"    // string: ключ идемпотентности
	MetaIssuedAt  = "edgectx/issued_at" // number: момент решения LLM по часам hw-mcp, с
	MetaTTL       = "edgectx/ttl_s"     // number: TTL решения, с
	MetaConfirmed = "edgectx/confirmed" // bool: пользователь подтвердил команду
	MetaDryRun    = "edgectx/dry_run"   // bool: только проверить
)

// Options — параметры сервера.
type Options struct {
	Catalog          string
	BoardHealth      bool     // добавить инструмент board_health
	Version          string   // версия реализации
	ProtocolVersions []string // ограничить ревизии MCP (nil — все, что знает SDK)
	Logger           *slog.Logger
}

// Server — MCP-сервер hw-mcp.
type Server struct {
	mcp  *mcp.Server
	hub  *hub.Hub
	inv  *inventory.Inventory
	opts Options
	log  *slog.Logger

	tools     []*mcp.Tool // в порядке регистрации (для -print-tools)
	resources map[string]bool
	disp      *dispatcher
	sessions  *idleSessions // закрытие брошенных HTTP-сессий (nil — выключено)
}

// New строит сервер и подписывает его на изменения состояния в hub.
func New(h *hub.Hub, opts Options) (*Server, error) {
	if opts.Catalog != CatalogGeneric && opts.Catalog != CatalogExplicit {
		return nil, fmt.Errorf("catalog must be %q or %q, got %q", CatalogGeneric, CatalogExplicit, opts.Catalog)
	}
	if opts.Logger == nil {
		opts.Logger = slog.New(slog.DiscardHandler)
	}
	if opts.Version == "" {
		opts.Version = "dev"
	}
	s := &Server{hub: h, inv: h.Inventory(), opts: opts, log: opts.Logger, resources: map[string]bool{}}
	// предупреждения SDK о недоставленных уведомлениях (мёртвые HTTP-сессии) — не чаще раза в минуту
	sdkLog := slog.New(newRateLimitHandler(opts.Logger.Handler(), time.Minute))
	s.mcp = mcp.NewServer(&mcp.Implementation{Name: "hw-mcp", Title: "Hardware context MCP server", Version: opts.Version},
		&mcp.ServerOptions{
			Instructions: "Hardware of the single-board computer: sensors (sysfs IIO/hwmon/1-Wire) and GPIO actuators. " +
				"Every actuator command passes a deterministic safety gate; a denial is returned as " +
				`{"denied":true,"reason":...}. Values are in SenML units.`,
			Logger:             sdkLog,
			SubscribeHandler:   s.subscribe,
			UnsubscribeHandler: func(context.Context, *mcp.UnsubscribeRequest) error { return nil },
			Capabilities: &mcp.ServerCapabilities{
				Tools:     &mcp.ToolCapabilities{},
				Resources: &mcp.ResourceCapabilities{Subscribe: true},
			},
			SupportedProtocolVersions: opts.ProtocolVersions,
		})
	if opts.Catalog == CatalogGeneric {
		s.addGenericTools()
	} else {
		s.addExplicitTools()
	}
	if opts.BoardHealth {
		s.addTool(&mcp.Tool{Name: "board_health",
			Description: "Board health: CPU load, memory, SoC temperature, throttling, hw-mcp counters",
			InputSchema: objectSchema(nil, nil), Annotations: readOnly()},
			func(ctx context.Context, args map[string]any, meta mcp.Meta) result {
				return ok(s.hub.BoardHealth())
			})
	}
	s.addResources()
	s.disp = newDispatcher(s.sendUpdate)
	h.OnChange(s.disp.enqueue)
	return s, nil
}

// Close останавливает рассылку уведомлений (не ждёт отправки, зависшей на клиенте).
func (s *Server) Close() { s.disp.close() }

// NotifyStats — счётчики рассыльщика уведомлений.
func (s *Server) NotifyStats() map[string]any { return s.disp.stats() }

// MCP — сервер SDK (для транспортов).
func (s *Server) MCP() *mcp.Server { return s.mcp }

// Tools — зарегистрированные инструменты (для -print-tools и тестов), по имени.
func (s *Server) Tools() []*mcp.Tool {
	out := append([]*mcp.Tool(nil), s.tools...)
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// ----------------------------------------------------------------------------------------------
// Результаты

type result struct {
	body  any
	isErr bool
}

func ok(body any) result { return result{body: body} }

func fail(code, detail string) result {
	return result{body: map[string]any{"error": code, "detail": detail}, isErr: true}
}

func denied(verdict, reason, detail string, extra map[string]any) result {
	b := map[string]any{"denied": true, "verdict": verdict, "reason": reason, "detail": detail}
	for k, v := range extra {
		b[k] = v
	}
	return result{body: b, isErr: true}
}

func (r result) toMCP() *mcp.CallToolResult {
	text, err := canon.Marshal(r.body)
	if err != nil {
		text = []byte(fmt.Sprintf(`{"error":"internal","detail":%q}`, err.Error()))
	}
	return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: string(text)}}, IsError: r.isErr}
}

// ----------------------------------------------------------------------------------------------
// Регистрация инструментов

type handler func(ctx context.Context, args map[string]any, meta mcp.Meta) result

// addTool регистрирует инструмент: аргументы разбираются с сохранением литералов чисел и
// проверяются по входной схеме тем же валидатором, что и в шлюзе.
func (s *Server) addTool(t *mcp.Tool, h handler) {
	schema := argschema.Compile(t.InputSchema)
	isActuator := t.Annotations != nil && !t.Annotations.ReadOnlyHint
	s.tools = append(s.tools, t)
	s.mcp.AddTool(t, func(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		var args any = map[string]any{}
		if raw := req.Params.Arguments; len(raw) > 0 && string(raw) != "null" {
			v, err := argschema.Decode(raw)
			if err != nil {
				return s.badArgs(isActuator, "arguments are not valid JSON: "+err.Error()).toMCP(), nil
			}
			args = v
		}
		if errs := schema.Validate(args, true); len(errs) > 0 {
			msgs := make([]string, 0, 3)
			for i, e := range errs {
				if i == 3 {
					break
				}
				msgs = append(msgs, e.String())
			}
			return s.badArgs(isActuator, strings.Join(msgs, "; ")).toMCP(), nil
		}
		return h(ctx, args.(map[string]any), req.Params.Meta).toMCP(), nil
	})
}

func (s *Server) badArgs(actuator bool, detail string) result {
	if actuator {
		return denied(string(gate.Reject), gate.ReasonInvalidArgs, detail, nil)
	}
	return fail(gate.ReasonInvalidArgs, detail)
}

func objectSchema(props map[string]any, required []string) map[string]any {
	if props == nil {
		props = map[string]any{}
	}
	m := map[string]any{"type": "object", "properties": props, "additionalProperties": false}
	if len(required) > 0 {
		req := make([]any, len(required))
		for i, r := range required {
			req[i] = r
		}
		m["required"] = req
	}
	return m
}

func boolPtr(b bool) *bool { return &b }

func readOnly() *mcp.ToolAnnotations {
	return &mcp.ToolAnnotations{ReadOnlyHint: true, OpenWorldHint: boolPtr(false)}
}

func actuator() *mcp.ToolAnnotations {
	return &mcp.ToolAnnotations{ReadOnlyHint: false, DestructiveHint: boolPtr(false), OpenWorldHint: boolPtr(false)}
}

var str = map[string]any{"type": "string"}

func (s *Server) addGenericTools() {
	s.addTool(&mcp.Tool{Name: "list_devices",
		Description: "List connected devices: properties (unit; rw = writable) and actions with arguments",
		InputSchema: objectSchema(map[string]any{"all": map[string]any{"type": "boolean",
			"description": "include devices without properties (cameras, USB, buses)"}}, nil),
		Annotations: readOnly()},
		func(ctx context.Context, args map[string]any, meta mcp.Meta) result {
			all, _ := args["all"].(bool)
			return ok(s.inv.CompactList(all))
		})
	s.addTool(&mcp.Tool{Name: "read_property", Description: "Read the current value of a device property",
		InputSchema: objectSchema(map[string]any{"device": str, "property": str}, []string{"device", "property"}),
		Annotations: readOnly()},
		func(ctx context.Context, args map[string]any, meta mcp.Meta) result {
			return s.read(args["device"].(string), args["property"].(string))
		})
	s.addTool(&mcp.Tool{Name: "write_property",
		Description: "Write a writable device property (checked by the safety gate)",
		InputSchema: objectSchema(map[string]any{"device": str, "property": str, "value": map[string]any{}},
			[]string{"device", "property", "value"}),
		Annotations: actuator()},
		func(ctx context.Context, args map[string]any, meta mcp.Meta) result {
			dev, prop := args["device"].(string), args["property"].(string)
			return s.actuate(dev, inventory.WriteToolName(prop), map[string]any{"value": args["value"]}, meta,
				"write_property", prop)
		})
	s.addTool(&mcp.Tool{Name: "invoke_action", Description: "Invoke a device action (checked by the safety gate)",
		InputSchema: objectSchema(map[string]any{"device": str, "action": str, "args": map[string]any{"type": "object"}},
			[]string{"device", "action"}),
		Annotations: actuator()},
		func(ctx context.Context, args map[string]any, meta mcp.Meta) result {
			a, ok := args["args"].(map[string]any)
			if !ok {
				a = map[string]any{}
			}
			return s.actuate(args["device"].(string), args["action"].(string), a, meta, "invoke_action", "")
		})
}

// toolName строит имя инструмента explicit и разводит совпадения (a_b + c и a + b_c) суффиксом-хешем;
// длина не больше 64 символов (ограничение имён функций OpenAI).
func toolName(used map[string]bool, parts ...string) string {
	name := strings.Join(parts, "_")
	if len(name) <= 64 && !used[name] {
		used[name] = true
		return name
	}
	sum := sha1.Sum([]byte(strings.Join(parts, ".")))
	suffix := "_" + hex.EncodeToString(sum[:])[:6]
	if len(name) > 64-len(suffix) {
		name = strings.TrimRight(name[:64-len(suffix)], "_")
	}
	name += suffix
	used[name] = true
	return name
}

func (s *Server) addExplicitTools() {
	used := map[string]bool{"board_health": true}
	for _, d := range s.inv.Devices {
		where := ""
		if d.Location != "" && !strings.Contains(d.ID, inventory.Slug(d.Location)) {
			where = " @" + d.Location
		}
		for _, p := range d.Properties {
			dev, prop := d.ID, p.Name
			unit := ""
			if p.Unit != "" {
				unit = " (" + p.Unit + ")"
			}
			s.addTool(&mcp.Tool{Name: toolName(used, "read", dev, prop),
				Description: fmt.Sprintf("Read %s%s of %s%s", prop, unit, dev, where),
				InputSchema: objectSchema(nil, nil), Annotations: readOnly()},
				func(ctx context.Context, args map[string]any, meta mcp.Meta) result {
					return s.read(dev, prop)
				})
			if p.Writable {
				s.addTool(&mcp.Tool{Name: toolName(used, "set", dev, prop),
					Description: fmt.Sprintf("Set %s (%s) of %s%s", prop, p.Type, dev, where),
					InputSchema: objectSchema(map[string]any{"value": inventory.ValueSchema(p)}, []string{"value"}),
					Annotations: actuator()},
					func(ctx context.Context, args map[string]any, meta mcp.Meta) result {
						return s.actuate(dev, inventory.WriteToolName(prop), map[string]any{"value": args["value"]},
							meta, "set", prop)
					})
			}
		}
		for _, a := range d.Actions {
			dev, act := d.ID, a.Name
			s.addTool(&mcp.Tool{Name: toolName(used, act, dev),
				Description: a.Description + " (" + dev + where + ")",
				InputSchema: a.Input, Annotations: actuator()},
				func(ctx context.Context, args map[string]any, meta mcp.Meta) result {
					return s.actuate(dev, act, args, meta, "invoke", "")
				})
		}
	}
}

// ----------------------------------------------------------------------------------------------
// Чтение и исполнение

func (s *Server) read(device, property string) result {
	r, err := s.hub.Read(device, property)
	switch err {
	case nil:
	case hub.ErrUnknownDevice:
		return fail("unknown_device", fmt.Sprintf("no device %q; call list_devices", device))
	case hub.ErrUnknownProperty:
		return fail("unknown_property", fmt.Sprintf("device %q has no property %q", device, property))
	default:
		return fail("read_failed", err.Error())
	}
	p := s.inv.Device(device).Property(property)
	body := map[string]any{"device": device, "property": property, "value": r.Value,
		"age_s": roundAge(s.hub.Now() - r.TS)}
	if p.Unit != "" {
		body["unit"] = p.Unit
	}
	if r.Err != "" {
		body["error"] = r.Err
	}
	return ok(body)
}

func roundAge(a float64) float64 {
	if a < 0 {
		a = 0
	}
	return float64(int64(a*10+0.5)) / 10
}

// metaRequest переносит в команду сведения оркестратора из _meta.
func metaRequest(meta mcp.Meta, req *hub.ActRequest) {
	if v, ok := meta[MetaCmdID].(string); ok && v != "" {
		req.CmdID = v
	}
	if v, ok := meta[MetaIssuedAt].(float64); ok {
		req.IssuedAt = &v
	}
	if v, ok := meta[MetaTTL].(float64); ok {
		req.TTL = &v
	}
	if v, ok := meta[MetaConfirmed].(bool); ok {
		req.Confirmed = v
	}
	if v, ok := meta[MetaDryRun].(bool); ok {
		req.DryRun = v
	}
}

func (s *Server) actuate(device, tool string, args map[string]any, meta mcp.Meta, origin, prop string) result {
	req := hub.ActRequest{Device: device, Tool: tool, Args: args, Origin: origin}
	metaRequest(meta, &req)
	res := s.hub.Actuate(req)
	dec := res.Decision
	extra := map[string]any{"device": device, "cmd_id": res.CmdID}
	if req.DryRun {
		body := map[string]any{"dry_run": true, "verdict": string(dec.Verdict), "device": device, "cmd_id": res.CmdID}
		if dec.Reason != "" {
			body["reason"], body["detail"] = dec.Reason, dec.Detail
		}
		if dec.Args != nil {
			body["args"] = dec.Args
		}
		return ok(body)
	}
	switch {
	case dec.Verdict == gate.Duplicate:
		// повтор уже исполненной команды: привод не трогается, это не ошибка
		return ok(map[string]any{"ok": true, "verdict": string(dec.Verdict), "executed": false,
			"device": device, "cmd_id": res.CmdID, "args": dec.Args,
			"detail": "command with this cmd_id was already executed"})
	case !dec.Accepted():
		return denied(string(dec.Verdict), dec.Reason, s.explain(device, tool, prop, dec), extra)
	case res.Err != nil:
		return result{body: map[string]any{"ok": false, "error": "actuation_failed", "detail": res.Err.Error(),
			"device": device, "cmd_id": res.CmdID}, isErr: true}
	}
	body := map[string]any{"ok": true, "verdict": string(dec.Verdict), "executed": true, "device": device,
		"cmd_id": res.CmdID}
	for p, v := range res.Applied {
		body["property"], body["value"] = p, v
	}
	if dec.Verdict == gate.Clamp {
		body["requested"] = args
		body["applied_args"] = dec.Args
	}
	if res.PulseUntil != nil {
		body["off_in_s"] = roundAge(*res.PulseUntil - s.hub.Now())
	}
	if res.LeaseUntil != nil {
		body["lease_expires_in_s"] = roundAge(*res.LeaseUntil - s.hub.Now())
	}
	if res.Renewed != nil {
		body["renewed"] = *res.Renewed
	}
	return ok(body)
}

// explain уточняет причину отказа реестра для LLM (код причины — как у шлюза).
func (s *Server) explain(device, tool, prop string, dec gate.Decision) string {
	d := s.inv.Device(device)
	switch {
	case dec.Reason == gate.ReasonUnknownDevice && d == nil:
		return fmt.Sprintf("no device %q; call list_devices", device)
	case dec.Reason == gate.ReasonUnknownDevice:
		return fmt.Sprintf("device %q has no writable properties or actions", device)
	case dec.Reason == gate.ReasonUnknownTool && prop != "" && d != nil && d.Property(prop) != nil:
		return fmt.Sprintf("property %q of %q is read-only", prop, device)
	case dec.Reason == gate.ReasonUnknownTool && prop != "":
		return fmt.Sprintf("device %q has no property %q", device, prop)
	case dec.Reason == gate.ReasonUnknownTool:
		return fmt.Sprintf("device %q has no action %q", device, tool)
	}
	return dec.Detail
}

// ----------------------------------------------------------------------------------------------
// Ресурсы

func (s *Server) addResource(uri, name, mime, desc string, read func() (string, error)) {
	s.resources[uri] = true
	s.mcp.AddResource(&mcp.Resource{URI: uri, Name: name, MIMEType: mime, Description: desc},
		func(ctx context.Context, req *mcp.ReadResourceRequest) (*mcp.ReadResourceResult, error) {
			text, err := read()
			if err != nil {
				return nil, err
			}
			return &mcp.ReadResourceResult{Contents: []*mcp.ResourceContents{{URI: uri, MIMEType: mime, Text: text}}}, nil
		})
}

func canonText(v any) (string, error) {
	b, err := canon.Marshal(v)
	return string(b), err
}

func (s *Server) addResources() {
	s.addResource("hw://inventory", "inventory", "application/json",
		"Device model for the agent: properties, units, actions, locations",
		func() (string, error) { return canonText(s.inv.AgentView()) })
	s.addResource("hw://inventory/text", "inventory-text", "text/plain",
		"Compact text projection of the inventory (L0, stable prompt prefix)",
		func() (string, error) { return s.inv.Text(), nil })
	s.addResource("hw://td", "thing-descriptions", "application/td+json",
		"W3C WoT Thing Descriptions 1.1 of devices with properties or actions",
		func() (string, error) { return canonText(s.inv.TDs()) })
	s.addResource("hw://diag", "diagnostics", "application/json",
		"hw-mcp counters (samples, notifications, gate decisions) and process resources; not for the LLM",
		func() (string, error) {
			d := s.hub.Diag()
			d["notify_dispatch"] = s.disp.stats()
			d["http_sessions"] = s.SessionStats()
			return canonText(d)
		})
	s.addResource("hw://probe", "sysfs-probe", "application/json",
		"Raw sysfs inventory (format of sysfs_probe.py)",
		func() (string, error) {
			b, err := sysfs.MarshalInventory(s.inv.Raw, 0)
			return string(b), err
		})
	for _, d := range s.inv.Devices {
		if len(d.Properties) == 0 {
			continue
		}
		dev := d.ID
		s.addResource("hw://state/"+dev, "state-"+dev, "application/json",
			"Current values of "+dev+" with their age; subscribe for updates",
			func() (string, error) {
				st, _ := s.hub.DeviceState(dev)
				return canonText(st)
			})
	}
}

func (s *Server) subscribe(ctx context.Context, req *mcp.SubscribeRequest) error {
	if !s.resources[req.Params.URI] {
		return mcp.ResourceNotFoundError(req.Params.URI)
	}
	return nil
}

// sendUpdate рассылает notifications/resources/updated подписчикам hw://state/<устройство> (из
// горутины рассыльщика). _meta: "hw/changed" — изменившиеся свойства, "device", "state" — их новые
// значения (как --meta-state у sim-mcp): edgectx берёт состояние из уведомления без resources/read,
// и быстрые переходы (PIR 1→0) не теряются между уведомлением и чтением.
func (s *Server) sendUpdate(device string, u pendingUpdate) {
	meta := mcp.Meta{"hw/changed": sortedProps(u.values), "device": device, "state": u.values}
	if err := s.mcp.ResourceUpdated(context.Background(),
		&mcp.ResourceUpdatedNotificationParams{URI: "hw://state/" + device, Meta: meta}); err != nil {
		s.log.Warn("resource updated notification failed", "device", device, "err", err)
	}
}

// ToolsJSON — каталог инструментов в виде tools/list (для -print-tools и подсчёта токенов каталога).
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
