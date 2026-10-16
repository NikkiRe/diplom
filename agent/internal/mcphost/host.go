// Package mcphost — MCP host агента на официальном Go SDK
// (github.com/modelcontextprotocol/go-sdk), ревизия протокола 2025-11-25.
//
// Подключает серверы по stdio (дочерний процесс) и streamable HTTP, собирает
// каталог инструментов (как его отдаёт сервер: режим explicit/generic выбирается
// на стороне сервера), вызывает инструменты, читает ресурсы и подписывается на
// ресурсы состояния: уведомления notifications/resources/updated превращаются в
// обновления, которые агент отдаёт гейту.
//
// События серверов (уведомления, смена каталога, падение и восстановление
// связи) идут агенту одной упорядоченной очередью; до Start очередь копится, и
// агент получает их только после записи run_start. Упавший сервер (конец
// сессии: процесс stdio завершился, HTTP-сессия оборвана) host переподключает
// сам: заново запускает процесс или соединение с паузой по настенным часам
// (экспонента с джиттером, свойство транспорта, а не логики агента), повторяет
// инициализацию и каталог; подписку и состояние восстанавливает агент по OnUp,
// а если восстановлено не всё — host повторяет OnUp с той же паузой. Сессия,
// оборвавшаяся быстрее reconnect_max_s, паузу не сбрасывает: сервер, падающий
// сразу после старта, переподключается всё реже. Пока сервер недоступен, его
// инструменты остаются в каталоге (префикс промпта не меняется), а вызовы сразу
// возвращают ErrServerDown.
package mcphost

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/edgectx/internal/activity"
	"github.com/local/edgectx/internal/backoff"
	"github.com/local/edgectx/internal/canon"
	"github.com/local/edgectx/internal/config"
)

// ProtocolVersion — ревизия MCP, которую запрашивает агент.
const ProtocolVersion = "2025-11-25"

// Tool — инструмент MCP в каталоге агента.
type Tool struct {
	Server      string
	Name        string // имя, под которым инструмент видит LLM
	Original    string // имя на сервере
	Description string
	Schema      map[string]any
}

// Result — результат вызова инструмента.
type Result struct {
	// Text — текст для LLM (JSON приводится к каноническому виду).
	Text    string
	IsError bool
	// Denied — структурный отказ (шлюз приводов): {"denied": true, "reason": …}.
	Denied bool
	Reason string
}

// Update — уведомление об изменении ресурса.
type Update struct {
	Server string
	URI    string
	Meta   map[string]any
}

// ServerInfo — сведения о подключении (для /v1/health).
type ServerInfo struct {
	Name         string `json:"name"`
	Transport    string `json:"transport"`
	Protocol     string `json:"protocol"`
	ServerName   string `json:"server_name"`
	Tools        int    `json:"tools"`
	Subscribed   int    `json:"subscribed"`
	CanSubscribe bool   `json:"can_subscribe"`
	Inventory    string `json:"inventory,omitempty"`
	Down         bool   `json:"down"`
}

// Options — параметры host'а.
type Options struct {
	// Tracker учитывает необработанные события серверов как незавершённую работу.
	Tracker *activity.Tracker
	// OnUpdate вызывается для изменений ресурсов состояния (не в горутине чтения
	// протокола, поэтому из него можно читать ресурсы).
	OnUpdate func(ctx context.Context, u Update)
	// OnInventory вызывается, когда меняется инвентарь или каталог инструментов.
	OnInventory func(server string)
	// OnDown — сессия сервера оборвалась (сервер упал); host переподключается сам.
	OnDown func(server string, err error)
	// OnUp — упавший сервер снова подключён (инициализация и каталог уже
	// повторены) с попытки attempts; подписку и состояние восстанавливает агент.
	// Ошибка — восстановлено не всё: host сообщает её (mcp_resync) и повторяет
	// OnUp с паузой переподключения, пока эта сессия жива.
	OnUp func(ctx context.Context, server string, attempts int) error
	// OnError — сбои, не прерывающие работу (уведомления, чтение ресурсов).
	OnError func(where string, err error)
	// UpdateQueueMax ограничивает число ожидающих уведомлений ресурсов. Лишние
	// отбрасываются, а их число сообщается одной ошибкой mcp_queue_full перед
	// следующим событием очереди; обработчик протокола не блокируется. Ноль —
	// стандартный предел 1024.
	UpdateQueueMax int
	// Reserved — имена, занятые встроенными инструментами агента.
	Reserved []string
	// Dir — рабочий каталог stdio-серверов.
	Dir     string
	Version string
	// Seed — зерно джиттера пауз переподключения.
	Seed uint64
}

// ErrServerDown — сервер упал, идёт переподключение.
var ErrServerDown = errors.New("сервер недоступен (переподключение)")

// Host — набор подключений к MCP-серверам.
type Host struct {
	opt Options

	mu       sync.RWMutex
	servers  map[string]*server
	down     map[string]bool // упавшие серверы (до переподключения)
	tools    []Tool
	byName   map[string]Tool
	stopping bool           // Close начат: новые сессии не регистрируются
	watchers sync.WaitGroup // горутины сессий и переподключений

	qmu     sync.Mutex
	qcond   *sync.Cond
	queue   []item
	started bool // Start: события доставляются агенту
	closed  bool
	dropped int // отброшено уведомлений с прошлого отчёта
	qdone   chan struct{}
	ctx     context.Context
	cancel  context.CancelFunc
}

// item — событие очереди host → агент.
type item struct {
	kind     itemKind
	u        Update
	err      error // itemDown — причина
	attempts int   // itemUp — с какой попытки подключились
	resync   int   // itemUp — номер повтора OnUp (0 — первый вызов)
}

type itemKind int

const (
	itemResource itemKind = iota // resources/updated
	itemTools                    // tools/list_changed
	itemDown                     // сессия оборвалась
	itemUp                       // переподключились
)

type server struct {
	cfg          config.MCPServer
	dial         func() mcp.Transport // nil — без переподключения
	sess         *mcp.ClientSession
	tools        []*mcp.Tool
	resources    []string
	templates    []string
	inventoryURI string
	inventory    string // последний прочитанный инвентарь (отдаётся, пока сервер недоступен)
	canSubscribe bool
	subscribed   []string
	timeout      time.Duration
	since        time.Time // когда установлена сессия
	tries        int       // попыток переподключения подряд, приведших к этой сессии
}

// New создаёт host. События серверов доставляются после Start.
func New(opt Options) *Host {
	if opt.Tracker == nil {
		opt.Tracker = &activity.Tracker{}
	}
	if opt.OnError == nil {
		opt.OnError = func(string, error) {}
	}
	if opt.Version == "" {
		opt.Version = "dev"
	}
	if opt.UpdateQueueMax <= 0 {
		opt.UpdateQueueMax = 1024
	}
	h := &Host{opt: opt, servers: map[string]*server{}, down: map[string]bool{}, byName: map[string]Tool{},
		qdone: make(chan struct{})}
	h.qcond = sync.NewCond(&h.qmu)
	h.ctx, h.cancel = context.WithCancel(context.Background())
	go h.dispatch()
	return h
}

// Start начинает доставку событий серверов агенту (накопленных — по порядку).
func (h *Host) Start() {
	h.qmu.Lock()
	h.started = true
	h.qcond.Broadcast()
	h.qmu.Unlock()
}

// Connect подключает сервер по описанию из конфигурации (cmd/url уже раскрыты).
// Упавший сервер переподключается с паузами reconnect_s … reconnect_max_s.
func (h *Host) Connect(ctx context.Context, cfg config.MCPServer) error {
	if cfg.Transport != "stdio" && cfg.Transport != "http" {
		return fmt.Errorf("mcp %s: неизвестный транспорт %q", cfg.Name, cfg.Transport)
	}
	if cfg.Transport == "stdio" && len(cfg.Cmd) == 0 {
		return fmt.Errorf("mcp %s: пустая команда", cfg.Name)
	}
	dial := func() mcp.Transport { return h.transport(cfg) }
	s, err := h.open(ctx, cfg, dial(), dial)
	if err == nil {
		h.observe(s)
	}
	return err
}

// transport создаёт новый транспорт: процесс stdio запускается заново.
func (h *Host) transport(cfg config.MCPServer) mcp.Transport {
	if cfg.Transport == "http" {
		// У http.Client нет общего таймаута: поток SSE для уведомлений живёт всю
		// сессию; таймауты задаются контекстом каждого вызова. Обрыв потока сразу
		// завершает сессию (MaxRetries < 0): серверы стенда не хранят события для
		// возобновления SSE, так что повторы SDK лишь откладывали бы обнаружение
		// падения, а переподключение host'а заново читает состояние целиком.
		return &mcp.StreamableClientTransport{Endpoint: cfg.URL, HTTPClient: &http.Client{}, MaxRetries: -1}
	}
	cmd := exec.Command(cfg.Cmd[0], cfg.Cmd[1:]...)
	cmd.Dir = h.opt.Dir
	cmd.Env = os.Environ()
	keys := make([]string, 0, len(cfg.Env))
	for k := range cfg.Env {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		cmd.Env = append(cmd.Env, k+"="+cfg.Env[k])
	}
	cmd.Stderr = os.Stderr // диагностика сервера — в stderr агента
	return &mcp.CommandTransport{Command: cmd, TerminateDuration: 3 * time.Second}
}

// ConnectTransport подключает сервер через готовый транспорт (используется и в
// тестах с mcp.NewInMemoryTransports). Такой сервер не переподключается.
func (h *Host) ConnectTransport(ctx context.Context, cfg config.MCPServer, t mcp.Transport) error {
	s, err := h.open(ctx, cfg, t, nil)
	if err == nil {
		h.observe(s)
	}
	return err
}

// open устанавливает сессию через t, читает каталог и регистрирует сервер,
// заменяя прежнюю сессию с тем же именем; dial создаёт транспорт для
// переподключения (nil — не переподключать).
func (h *Host) open(ctx context.Context, cfg config.MCPServer, t mcp.Transport, dial func() mcp.Transport) (*server, error) {
	s := &server{cfg: cfg, dial: dial, timeout: config.Seconds(cfg.TimeoutS), since: time.Now()}
	if s.timeout <= 0 {
		s.timeout = 30 * time.Second
	}
	name := cfg.Name
	client := mcp.NewClient(&mcp.Implementation{Name: "edgectx", Version: h.opt.Version}, &mcp.ClientOptions{
		// Агент не предоставляет roots/sampling/elicitation.
		Capabilities: &mcp.ClientCapabilities{},
		ResourceUpdatedHandler: func(_ context.Context, req *mcp.ResourceUpdatedNotificationRequest) {
			if req == nil || req.Params == nil {
				return
			}
			h.push(item{kind: itemResource, u: Update{Server: name, URI: req.Params.URI, Meta: req.Params.Meta}})
		},
		ToolListChangedHandler: func(context.Context, *mcp.ToolListChangedRequest) {
			h.push(item{kind: itemTools, u: Update{Server: name}})
		},
	})
	cctx, cancel := context.WithTimeout(ctx, s.timeout)
	defer cancel()
	sess, err := client.Connect(cctx, t, &mcp.ClientSessionOptions{ProtocolVersion: ProtocolVersion})
	if err != nil {
		return nil, fmt.Errorf("mcp %s: connect: %w", name, err)
	}
	s.sess = sess
	if caps := sess.InitializeResult().Capabilities; caps != nil && caps.Resources != nil {
		s.canSubscribe = caps.Resources.Subscribe
	}
	if err := h.refresh(cctx, s); err != nil {
		sess.Close()
		return nil, fmt.Errorf("mcp %s: %w", name, err)
	}
	h.mu.Lock()
	if h.stopping {
		h.mu.Unlock()
		sess.Close()
		return nil, fmt.Errorf("mcp %s: host закрыт", name)
	}
	old := h.servers[name]
	if old != nil {
		s.inventory = old.inventory
	}
	h.servers[name] = s
	delete(h.down, name)
	h.rebuildLocked()
	h.mu.Unlock()
	if old != nil {
		old.sess.Close()
	}
	return s, nil
}

// observe запускает наблюдение за сессией сервера (если host не закрывается).
func (h *Host) observe(s *server) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.stopping {
		return
	}
	h.watchers.Add(1)
	go h.watch(s)
}

// watch ждёт конца сессии. Если сервер упал (а не закрыт host'ом и не
// заменён), сообщает агенту и переподключается; после короткой сессии — с
// паузой больше прежней.
func (h *Host) watch(s *server) {
	defer h.watchers.Done()
	err := s.sess.Wait()
	h.mu.Lock()
	lost := !h.stopping && h.servers[s.cfg.Name] == s
	if lost {
		h.down[s.cfg.Name] = true
	}
	h.mu.Unlock()
	if !lost {
		return
	}
	if err == nil {
		err = errors.New("сессия закрыта сервером")
	}
	h.push(item{kind: itemDown, u: Update{Server: s.cfg.Name}, err: err})
	if s.dial == nil {
		return
	}
	tries := 0
	if time.Since(s.since) < config.Seconds(s.cfg.ReconnectMaxS) {
		tries = s.tries
	}
	h.reconnect(s.cfg, s.dial, tries)
}

// reconnect повторяет подключение с растущей паузой, пока не получится или
// пока host не закроют; tries — попытки, приведшие к прежней короткой сессии:
// пауза продолжает расти с них.
func (h *Host) reconnect(cfg config.MCPServer, dial func() mcp.Transport, tries int) {
	for attempt := 1; ; attempt++ {
		if !h.sleep(pause(cfg, h.opt.Seed, tries+attempt)) {
			return
		}
		if s, err := h.open(h.ctx, cfg, dial(), dial); err == nil {
			s.tries = tries + attempt
			// Сначала up, потом наблюдение: если новая сессия сразу оборвётся, down придёт после up.
			h.push(item{kind: itemUp, u: Update{Server: cfg.Name}, attempts: attempt})
			h.observe(s)
			return
		}
	}
}

// retryUp повторяет OnUp после паузы, если сессия сервера та же и жива.
func (h *Host) retryUp(it item) {
	name := it.u.Server
	h.mu.Lock()
	s := h.servers[name]
	if h.stopping || s == nil || h.down[name] {
		h.mu.Unlock()
		return
	}
	h.watchers.Add(1)
	h.mu.Unlock()
	it.resync++
	go func() {
		defer h.watchers.Done()
		if !h.sleep(pause(s.cfg, h.opt.Seed, it.resync)) {
			return
		}
		h.mu.RLock()
		same := h.servers[name] == s && !h.down[name]
		h.mu.RUnlock()
		if same {
			h.push(it)
		}
	}()
}

// sleep ждёт d по настенным часам; false — host закрывается.
func (h *Host) sleep(d time.Duration) bool {
	tm := time.NewTimer(d)
	defer tm.Stop()
	select {
	case <-h.ctx.Done():
		return false
	case <-tm.C:
		return true
	}
}

// pause — пауза перед n-й попыткой подряд (n ≥ 1) переподключиться или заново
// синхронизировать сервер: reconnect_s·2^(n−1), не больше reconnect_max_s, с
// джиттером.
func pause(cfg config.MCPServer, seed uint64, n int) time.Duration {
	p := backoff.Policy{Base: config.Seconds(cfg.ReconnectS), Max: config.Seconds(cfg.ReconnectMaxS)}
	return p.Delay(n, backoff.Unit(seed^backoff.Seed(cfg.Name), uint64(n)))
}

// refresh перечитывает инструменты, ресурсы и шаблоны ресурсов сервера.
func (h *Host) refresh(ctx context.Context, s *server) error {
	var tools []*mcp.Tool
	for t, err := range s.sess.Tools(ctx, nil) {
		if err != nil {
			return fmt.Errorf("tools/list: %w", err)
		}
		tools = append(tools, t)
	}
	caps := s.sess.InitializeResult().Capabilities
	var resources, templates []string
	if caps != nil && caps.Resources != nil {
		for r, err := range s.sess.Resources(ctx, nil) {
			if err != nil {
				return fmt.Errorf("resources/list: %w", err)
			}
			resources = append(resources, r.URI)
		}
		for rt, err := range s.sess.ResourceTemplates(ctx, nil) {
			if err != nil {
				break // шаблоны необязательны
			}
			templates = append(templates, rt.URITemplate)
		}
	}
	sort.Strings(resources)
	sort.Strings(templates)
	inv := s.cfg.InventoryURI
	if inv == "" {
		for _, u := range resources {
			if strings.HasSuffix(u, "inventory") {
				inv = u
				break
			}
		}
	}
	h.mu.Lock()
	s.tools, s.resources, s.templates, s.inventoryURI = tools, resources, templates, inv
	h.mu.Unlock()
	return nil
}

var badNameChars = regexp.MustCompile(`[^a-zA-Z0-9_-]`)

// rebuildLocked пересобирает каталог: имена приводятся к допустимым для
// OpenAI (^[a-zA-Z0-9_-]{1,64}$), при совпадении имён у разных серверов (или с
// встроенными инструментами) добавляется префикс «сервер__». Порядок —
// по имени, чтобы префикс промпта был детерминированным.
func (h *Host) rebuildLocked() {
	count := map[string]int{}
	for _, r := range h.opt.Reserved {
		count[r]++
	}
	for _, s := range h.servers {
		for _, t := range s.tools {
			count[sanitize(t.Name)]++
		}
	}
	var out []Tool
	for _, s := range h.servers {
		for _, t := range s.tools {
			name := sanitize(t.Name)
			if count[name] > 1 {
				name = sanitize(s.cfg.Name + "__" + t.Name)
			}
			out = append(out, Tool{Server: s.cfg.Name, Name: name, Original: t.Name,
				Description: t.Description, Schema: schemaOf(t.InputSchema)})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	h.tools = out
	h.byName = map[string]Tool{}
	for _, t := range out {
		h.byName[t.Name] = t
	}
}

func sanitize(name string) string {
	n := badNameChars.ReplaceAllString(name, "_")
	if len(n) > 64 {
		n = n[:64]
	}
	return n
}

// schemaOf приводит inputSchema к объекту JSON Schema c type=object.
func schemaOf(s any) map[string]any {
	var m map[string]any
	switch x := s.(type) {
	case map[string]any:
		m = x
	case nil:
	default:
		b, err := json.Marshal(x)
		if err == nil {
			json.Unmarshal(b, &m)
		}
	}
	if m == nil {
		m = map[string]any{}
	}
	out := make(map[string]any, len(m)+2)
	for k, v := range m {
		out[k] = v
	}
	if _, ok := out["type"]; !ok {
		out["type"] = "object"
	}
	if _, ok := out["properties"]; !ok {
		out["properties"] = map[string]any{}
	}
	return out
}

// Tools — каталог инструментов всех серверов (по имени).
func (h *Host) Tools() []Tool {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return append([]Tool(nil), h.tools...)
}

// Lookup — инструмент по имени в каталоге.
func (h *Host) Lookup(name string) (Tool, bool) {
	h.mu.RLock()
	defer h.mu.RUnlock()
	t, ok := h.byName[name]
	return t, ok
}

// ErrUnknownTool — инструмента нет в каталоге.
var ErrUnknownTool = errors.New("unknown tool")

// live — подключённый сервер по имени: ошибка, если его нет или он упал.
func (h *Host) live(name string) (*server, error) {
	h.mu.RLock()
	defer h.mu.RUnlock()
	s := h.servers[name]
	switch {
	case s == nil:
		return nil, fmt.Errorf("mcp: нет сервера %q", name)
	case h.down[name]:
		return nil, fmt.Errorf("mcp %s: %w", name, ErrServerDown)
	}
	return s, nil
}

// Call вызывает инструмент по имени из каталога.
func (h *Host) Call(ctx context.Context, name string, args map[string]any) (Result, error) {
	t, ok := h.Lookup(name)
	if !ok {
		return Result{}, fmt.Errorf("%w: %s", ErrUnknownTool, name)
	}
	s, err := h.live(t.Server)
	if err != nil {
		return Result{}, err
	}
	if args == nil {
		args = map[string]any{}
	}
	cctx, cancel := context.WithTimeout(ctx, s.timeout)
	defer cancel()
	res, err := s.sess.CallTool(cctx, &mcp.CallToolParams{Name: t.Original, Arguments: args})
	if err != nil {
		return Result{}, fmt.Errorf("mcp %s: %s: %w", t.Server, t.Original, err)
	}
	return convertResult(res), nil
}

// convertResult превращает результат MCP в текст для LLM и признаки отказа.
func convertResult(res *mcp.CallToolResult) Result {
	r := Result{IsError: res.IsError}
	var parts []string
	for _, c := range res.Content {
		switch x := c.(type) {
		case *mcp.TextContent:
			parts = append(parts, canonicalText(x.Text))
		case *mcp.ImageContent:
			parts = append(parts, fmt.Sprintf("[изображение %s, %d байт]", x.MIMEType, len(x.Data)))
		case *mcp.AudioContent:
			parts = append(parts, fmt.Sprintf("[аудио %s, %d байт]", x.MIMEType, len(x.Data)))
		case *mcp.ResourceLink:
			parts = append(parts, "[ресурс "+x.URI+"]")
		case *mcp.EmbeddedResource:
			if x.Resource != nil {
				parts = append(parts, canonicalText(x.Resource.Text))
			}
		}
	}
	r.Text = strings.Join(parts, "\n")
	if r.Text == "" && res.StructuredContent != nil {
		r.Text = canon.String(res.StructuredContent)
	}
	// Отказ шлюза: в структурном результате или в JSON-тексте.
	if m, ok := res.StructuredContent.(map[string]any); ok {
		r.Denied, r.Reason = deniedOf(m)
	}
	if !r.Denied {
		var m map[string]any
		if json.Unmarshal([]byte(r.Text), &m) == nil {
			r.Denied, r.Reason = deniedOf(m)
		}
	}
	return r
}

func deniedOf(m map[string]any) (bool, string) {
	d, _ := m["denied"].(bool)
	if !d {
		return false, ""
	}
	reason, _ := m["reason"].(string)
	return true, reason
}

// canonicalText сжимает JSON-текст до канонического вида (Python SDK отдаёт
// indent=2, это +30–63 % токенов — GAP4); не-JSON возвращается как есть.
func canonicalText(s string) string {
	t := strings.TrimSpace(s)
	if t == "" || (t[0] != '{' && t[0] != '[') {
		return s
	}
	var v any
	dec := json.NewDecoder(strings.NewReader(t))
	dec.UseNumber()
	if dec.Decode(&v) != nil || dec.More() {
		return s
	}
	return canon.String(v)
}

// ReadResource читает текстовое содержимое ресурса.
func (h *Host) ReadResource(ctx context.Context, serverName, uri string) (string, error) {
	s, err := h.live(serverName)
	if err != nil {
		return "", err
	}
	cctx, cancel := context.WithTimeout(ctx, s.timeout)
	defer cancel()
	res, err := s.sess.ReadResource(cctx, &mcp.ReadResourceParams{URI: uri})
	if err != nil {
		return "", fmt.Errorf("mcp %s: read %s: %w", serverName, uri, err)
	}
	var parts []string
	for _, c := range res.Contents {
		if c.Text != "" {
			parts = append(parts, c.Text)
		} else if len(c.Blob) > 0 {
			parts = append(parts, string(c.Blob))
		}
	}
	return strings.Join(parts, "\n"), nil
}

// Inventory возвращает сырой инвентарь каждого сервера: ресурс инвентаря или,
// если его нет, результат инструмента list_devices. У упавшего сервера —
// последний прочитанный инвентарь: устройства не исчезают из контекста, пока
// нет связи.
func (h *Host) Inventory(ctx context.Context) (map[string]string, error) {
	out := map[string]string{}
	var errs []string
	for _, name := range h.serverNames() {
		h.mu.RLock()
		s, down := h.servers[name], h.down[name]
		if s == nil { // host закрыт
			h.mu.RUnlock()
			continue
		}
		uri, cached := s.inventoryURI, s.inventory
		var listTool string
		for _, t := range h.tools {
			if t.Server == name && t.Original == "list_devices" {
				listTool = t.Name
			}
		}
		h.mu.RUnlock()
		var text string
		var err error
		switch {
		case down:
			text = cached
		case uri != "":
			text, err = h.ReadResource(ctx, name, uri)
		case listTool != "":
			var res Result
			res, err = h.Call(ctx, listTool, map[string]any{})
			text = res.Text
		}
		if err != nil {
			errs = append(errs, err.Error())
			continue
		}
		if text == "" {
			continue
		}
		out[name] = text
		h.mu.Lock()
		s.inventory = text
		h.mu.Unlock()
	}
	if len(errs) > 0 {
		return out, errors.New(strings.Join(errs, "; "))
	}
	return out, nil
}

// StateURIs — кандидаты на подписку: перечисленные ресурсы и шаблоны вида
// …{device}/{id}…, раскрытые по ID устройств инвентаря; кроме ресурса инвентаря;
// с фильтром по шаблонам subscribe из конфигурации.
func (h *Host) StateURIs(serverName string, deviceIDs []string) []string {
	h.mu.RLock()
	s := h.servers[serverName]
	if s == nil {
		h.mu.RUnlock()
		return nil
	}
	resources := append([]string(nil), s.resources...)
	templates := append([]string(nil), s.templates...)
	inventoryURI := s.inventoryURI
	patterns := append([]string(nil), s.cfg.Subscribe...)
	h.mu.RUnlock()
	set := map[string]bool{}
	for _, u := range resources {
		set[u] = true
	}
	for _, tpl := range templates {
		for _, ph := range []string{"{device}", "{id}", "{device_id}"} {
			if strings.Contains(tpl, ph) {
				for _, id := range deviceIDs {
					set[strings.ReplaceAll(tpl, ph, id)] = true
				}
			}
		}
	}
	var out []string
	for u := range set {
		if u == inventoryURI || strings.Contains(u, "{") {
			continue
		}
		if len(patterns) > 0 && !matchAny(patterns, u) {
			continue
		}
		out = append(out, u)
	}
	sort.Strings(out)
	return out
}

// matchAny сопоставляет URI с шаблонами, в которых «*» — любая подстрока (в
// том числе с «/»: URI вида hw://state/door1 содержит «//»), «?» — один символ.
func matchAny(patterns []string, s string) bool {
	for _, p := range patterns {
		if globRegexp(p).MatchString(s) {
			return true
		}
	}
	return false
}

func globRegexp(p string) *regexp.Regexp {
	var b strings.Builder
	b.WriteString("^")
	for _, r := range p {
		switch r {
		case '*':
			b.WriteString(".*")
		case '?':
			b.WriteString(".")
		default:
			b.WriteString(regexp.QuoteMeta(string(r)))
		}
	}
	b.WriteString("$")
	return regexp.MustCompile(b.String())
}

// Subscribe подписывается на ресурсы состояния сервера (если сервер это
// поддерживает) и возвращает список подписанных URI.
func (h *Host) Subscribe(ctx context.Context, serverName string, uris []string) ([]string, error) {
	s, err := h.live(serverName)
	if err != nil {
		return nil, err
	}
	if !s.canSubscribe {
		return nil, nil
	}
	var done []string
	var errs []string
	for _, u := range uris {
		cctx, cancel := context.WithTimeout(ctx, s.timeout)
		err := s.sess.Subscribe(cctx, &mcp.SubscribeParams{URI: u})
		cancel()
		if err != nil {
			errs = append(errs, fmt.Sprintf("%s: %v", u, err))
			continue
		}
		done = append(done, u)
	}
	h.mu.Lock()
	s.subscribed = done
	h.mu.Unlock()
	if len(errs) > 0 {
		return done, fmt.Errorf("mcp %s: subscribe: %s", serverName, strings.Join(errs, "; "))
	}
	return done, nil
}

// inventoryURI — ресурс инвентаря сервера.
func (h *Host) inventoryURI(serverName string) string {
	h.mu.RLock()
	defer h.mu.RUnlock()
	if s := h.servers[serverName]; s != nil {
		return s.inventoryURI
	}
	return ""
}

// Servers — сведения о подключениях.
func (h *Host) Servers() []ServerInfo {
	h.mu.RLock()
	defer h.mu.RUnlock()
	var out []ServerInfo
	for _, name := range h.serverNamesLocked() {
		s := h.servers[name]
		info := ServerInfo{Name: name, Transport: s.cfg.Transport, Tools: len(s.tools),
			Subscribed: len(s.subscribed), CanSubscribe: s.canSubscribe, Inventory: s.inventoryURI, Down: h.down[name]}
		if ir := s.sess.InitializeResult(); ir != nil {
			info.Protocol = ir.ProtocolVersion
			if ir.ServerInfo != nil {
				info.ServerName = ir.ServerInfo.Name
			}
		}
		out = append(out, info)
	}
	return out
}

func (h *Host) serverNames() []string {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return h.serverNamesLocked()
}

func (h *Host) serverNamesLocked() []string {
	names := make([]string, 0, len(h.servers))
	for n := range h.servers {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}

// ---------------------------------------------------------- события серверов

// push кладёт событие в очередь. Обработчик протокола не блокируется: чтение
// ресурса из него привело бы к взаимоблокировке. Уведомления ресурсов сверх
// предела отбрасываются (и считаются), события связи и каталога — никогда.
func (h *Host) push(it item) {
	h.qmu.Lock()
	defer h.qmu.Unlock()
	if h.closed {
		return
	}
	if it.kind == itemResource && len(h.queue) >= h.opt.UpdateQueueMax {
		h.dropped++
		return
	}
	h.opt.Tracker.Begin()
	h.queue = append(h.queue, it)
	h.qcond.Signal()
}

func (h *Host) dispatch() {
	defer close(h.qdone)
	for {
		h.qmu.Lock()
		for (len(h.queue) == 0 || !h.started) && !h.closed {
			h.qcond.Wait()
		}
		if h.closed {
			// Необработанные события после остановки отбрасываются.
			n := len(h.queue)
			h.queue = nil
			h.qmu.Unlock()
			for i := 0; i < n; i++ {
				h.opt.Tracker.End()
			}
			return
		}
		it := h.queue[0]
		h.queue[0] = item{} // не удерживать Meta уже обработанного уведомления
		h.queue = h.queue[1:]
		dropped := h.dropped
		h.dropped = 0
		h.qmu.Unlock()
		if dropped > 0 {
			h.opt.OnError("mcp_queue_full", fmt.Errorf("очередь уведомлений переполнена (предел %d): отброшено %d",
				h.opt.UpdateQueueMax, dropped))
		}
		h.handle(it)
		h.opt.Tracker.End()
	}
}

func (h *Host) handle(it item) {
	defer func() {
		if r := recover(); r != nil {
			h.opt.OnError("mcp_update", fmt.Errorf("panic: %v", r))
		}
	}()
	name := it.u.Server
	switch it.kind {
	case itemDown:
		if h.opt.OnDown != nil {
			h.opt.OnDown(name, it.err)
		}
	case itemUp:
		if h.opt.OnUp == nil {
			return
		}
		if err := h.opt.OnUp(h.ctx, name, it.attempts); err != nil {
			h.opt.OnError("mcp_resync", err)
			h.retryUp(it)
		}
	case itemTools:
		s, err := h.live(name)
		if err != nil {
			return // сервер упал: каталог перечитает переподключение
		}
		ctx, cancel := context.WithTimeout(h.ctx, s.timeout)
		err = h.refresh(ctx, s)
		cancel()
		if err != nil {
			h.opt.OnError("mcp_refresh", err)
			return
		}
		h.mu.Lock()
		h.rebuildLocked()
		h.mu.Unlock()
		if h.opt.OnInventory != nil {
			h.opt.OnInventory(name)
		}
	case itemResource:
		if it.u.URI != "" && it.u.URI == h.inventoryURI(name) {
			if h.opt.OnInventory != nil {
				h.opt.OnInventory(name)
			}
			return
		}
		if h.opt.OnUpdate != nil {
			h.opt.OnUpdate(h.ctx, it.u)
		}
	}
}

// Close останавливает переподключения, очередь событий и сессии (stdio-серверы
// завершаются).
func (h *Host) Close() {
	h.mu.Lock()
	h.stopping = true
	h.mu.Unlock()
	h.cancel() // прерывает переподключения и чтение ресурсов в текущем обработчике
	h.qmu.Lock()
	wasClosed := h.closed
	h.closed = true
	h.qcond.Broadcast()
	h.qmu.Unlock()
	if !wasClosed {
		select {
		case <-h.qdone:
		case <-time.After(5 * time.Second):
		}
	}
	h.mu.Lock()
	servers := h.servers
	h.servers = map[string]*server{}
	h.tools, h.byName = nil, map[string]Tool{}
	h.mu.Unlock()
	for _, s := range servers {
		s.sess.Close()
	}
	h.watchers.Wait()
}
