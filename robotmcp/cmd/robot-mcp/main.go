// Команда robot-mcp — компактный MCP-адаптер мобильного робота (R-compact v2) поверх rosbridge.
//
//	robot-mcp [-rosbridge ws://127.0.0.1:9090] [-config places.yaml] [-transport stdio|http] ...
//
// См. README.md компонента.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/robotmcp/configs"
	"github.com/local/robotmcp/internal/canon"
	"github.com/local/robotmcp/internal/clock"
	"github.com/local/robotmcp/internal/robot"
	"github.com/local/robotmcp/internal/rosbridge"
	"github.com/local/robotmcp/internal/server"
	"github.com/local/robotmcp/internal/trace"
)

// version задаётся при сборке: -ldflags "-X main.version=..."
var version = "dev"

type config struct {
	rosbridge   string
	configPath  string
	transport   string
	addr        string
	path        string
	controlAddr string
	readyFile   string
	clockMode   string
	clockStart  float64
	payload     string
	motionScan  int
	poseThr     int
	scanMaxAge  float64
	sensorTO    time.Duration
	frameTO     time.Duration
	imageWidth  int
	jpegQ       int
	goalTO      float64
	cancelTO    time.Duration
	stuckS      float64
	obstacleM   float64
	obstacleH   float64
	lowBattery  float64
	battery     string
	batteryInit float64
	notifyCtx   bool
	detector    string
	waitParam   bool
	lookPlace   bool
	simStep     int
	tracePath   string
	runID       string
	protocol    string
	logLevel    string
	tick        time.Duration

	printTools, printInventory, showVersion bool
}

func parseFlags(args []string, stderr io.Writer) (*config, error) {
	c := &config{}
	fs := flag.NewFlagSet("robot-mcp", flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.StringVar(&c.rosbridge, "rosbridge", "ws://127.0.0.1:9090", "URL rosbridge (с платы sbc: ws://host.lima.internal:9090)")
	fs.StringVar(&c.configPath, "config", "", "YAML мест и тем (пусто — встроенный configs/turtlebot3_world.yaml)")
	fs.StringVar(&c.transport, "transport", "stdio", "транспорт MCP: stdio | http (streamable HTTP)")
	fs.StringVar(&c.addr, "addr", "127.0.0.1:18110", "адрес HTTP (порт 0 — свободный)")
	fs.StringVar(&c.path, "path", "/mcp", "путь конечной точки MCP")
	fs.StringVar(&c.controlAddr, "control-addr", "", "отдельный HTTP для /control/* при stdio (пусто — нет)")
	fs.StringVar(&c.readyFile, "ready-file", "", "записать сюда URL после запуска HTTP")
	fs.StringVar(&c.clockMode, "clock", "real", "часы таймеров: real | virtual (время двигает POST /control/clock)")
	fs.Float64Var(&c.clockStart, "clock-start", 0, "начальное время виртуальных часов, с")
	fs.StringVar(&c.payload, "payload", "summary", "summary | raw (R-raw: status/look отдают сырые /amcl_pose и /scan)")
	fs.IntVar(&c.motionScan, "motion-scan-ms", 0, "throttle_rate подписки /scan на время движения, мс (0 — нет: скан только по запросу)")
	fs.IntVar(&c.poseThr, "pose-throttle-ms", 2000, "throttle_rate подписки /amcl_pose, мс (0 — каждое обновление AMCL)")
	fs.Float64Var(&c.scanMaxAge, "scan-max-age", 30, "в простое скан из кэша не старше, с (и снятый после остановки)")
	fs.DurationVar(&c.sensorTO, "sensor-timeout", 1500*time.Millisecond, "ожидание разового /scan")
	fs.DurationVar(&c.frameTO, "frame-timeout", 3*time.Second, "ожидание кадра ленивой камеры")
	fs.IntVar(&c.imageWidth, "image-width", 320, "ширина кадра для LLM (уменьшение в целое число раз; 0 — как есть)")
	fs.IntVar(&c.jpegQ, "jpeg-quality", 70, "качество JPEG уменьшенного кадра")
	fs.Float64Var(&c.goalTO, "goal-timeout", 150, "таймаут участка по часам адаптера, с (0 — нет)")
	fs.DurationVar(&c.cancelTO, "cancel-timeout", time.Second, "ожидание ответа Nav2 на отмену всех целей (stop, таймаут, завершение, подключение); без ответа — разрыв соединения с rosbridge")
	fs.Float64Var(&c.stuckS, "stuck-s", 30, "без обновления позы при активной цели дольше — alert stuck, с (0 — нет)")
	fs.Float64Var(&c.obstacleM, "obstacle-m", 0.25, "препятствие впереди ближе (по скану движения) … (0 — нет)")
	fs.Float64Var(&c.obstacleH, "obstacle-hold-s", 4, "… дольше — alert blocked до прерывания цели Nav2, с")
	fs.Float64Var(&c.lowBattery, "low-battery-pct", 20, "порог тревоги low_battery, %")
	fs.StringVar(&c.battery, "battery", "emulate", "заряд: emulate (POST /control/battery) | topic (/battery_state)")
	fs.Float64Var(&c.batteryInit, "battery-start", 100, "начальный заряд в режиме emulate, %")
	fs.BoolVar(&c.notifyCtx, "notify-context", false, "уведомлять и об изменениях place/battery_pct (не только значимые)")
	fs.StringVar(&c.detector, "detector", "none", "детектор look/patrol: none | stub (объекты задаёт POST /control/detector)")
	fs.BoolVar(&c.lookPlace, "look-place", true, "параметр place у look (поездка + сводка одним вызовом; false — каталог короче)")
	fs.BoolVar(&c.waitParam, "wait-param", false, "параметр wait у go_to (синхронный режим, только реальные часы)")
	fs.IntVar(&c.simStep, "sim-step-iters", 0, "на паузе мира шагать его на N итераций, пока ждём /scan или кадр (стенд --world-control)")
	fs.StringVar(&c.tracePath, "trace", "", "журнал robot-mcp (JSONL)")
	fs.StringVar(&c.runID, "run-id", "", "идентификатор прогона в журнале")
	fs.StringVar(&c.protocol, "protocol", "2025-11-25", "ревизии MCP через запятую (пусто — все, что знает SDK)")
	fs.StringVar(&c.logLevel, "log-level", "warn", "уровень журнала stderr: debug | info | warn | error")
	fs.DurationVar(&c.tick, "tick", time.Second, "период проверки таймеров в реальных часах")
	fs.BoolVar(&c.printTools, "print-tools", false, "напечатать каталог (tools/list) и оценку токенов, выйти")
	fs.BoolVar(&c.printInventory, "print-inventory", false, "напечатать robot://inventory и выйти")
	fs.BoolVar(&c.showVersion, "version", false, "версия")
	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	switch {
	case fs.NArg() > 0:
		return nil, fmt.Errorf("unexpected arguments: %v", fs.Args())
	case c.transport != "stdio" && c.transport != "http":
		return nil, fmt.Errorf("-transport must be stdio or http")
	case c.clockMode != "real" && c.clockMode != "virtual":
		return nil, fmt.Errorf("-clock must be real or virtual")
	case c.payload != "summary" && c.payload != "raw":
		return nil, fmt.Errorf("-payload must be summary or raw")
	case c.battery != "emulate" && c.battery != "topic":
		return nil, fmt.Errorf("-battery must be emulate or topic")
	case c.detector != "none" && c.detector != "stub":
		return nil, fmt.Errorf("-detector must be none or stub")
	case c.cancelTO <= 0:
		return nil, fmt.Errorf("-cancel-timeout must be > 0")
	case c.waitParam && c.clockMode == "virtual":
		return nil, fmt.Errorf("-wait-param needs -clock real (sync go_to deadlocks the replayer)")
	}
	return c, nil
}

func main() {
	c, err := parseFlags(os.Args[1:], os.Stderr)
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			os.Exit(0)
		}
		fmt.Fprintln(os.Stderr, "robot-mcp:", err)
		os.Exit(2)
	}
	if err := run(c, os.Stdout, os.Stderr); err != nil {
		fmt.Fprintln(os.Stderr, "robot-mcp:", err)
		os.Exit(1)
	}
}

type app struct {
	cfg    *config
	clk    clock.Clock
	vclock *clock.Virtual
	br     *rosbridge.Client
	robot  *robot.Robot
	srv    *server.Server
	stub   *robot.Stub
	trace  *trace.Writer
	log    *slog.Logger
}

func build(c *config, stderr io.Writer) (*app, error) {
	var level slog.Level
	if err := level.UnmarshalText([]byte(c.logLevel)); err != nil {
		return nil, fmt.Errorf("-log-level: %w", err)
	}
	logger := slog.New(slog.NewTextHandler(stderr, &slog.HandlerOptions{Level: level}))
	raw := configs.TurtleBot3World
	if c.configPath != "" {
		var err error
		if raw, err = os.ReadFile(c.configPath); err != nil {
			return nil, err
		}
	}
	rc, err := robot.ParseConfig(raw)
	if err != nil {
		return nil, err
	}
	a := &app{cfg: c, log: logger}
	if c.clockMode == "virtual" {
		a.vclock = clock.NewVirtual(c.clockStart)
		a.clk = a.vclock
	} else {
		a.clk = clock.NewReal()
	}
	if c.tracePath != "" {
		if a.trace, err = trace.Open(c.tracePath, a.clk, c.runID); err != nil {
			return nil, err
		}
	}
	a.br = rosbridge.New(rosbridge.Options{URL: c.rosbridge, Logger: logger,
		Actions: []string{rc.Topics.Nav}, CancelTimeout: c.cancelTO,
		OnConnect:    func(n int) { a.trace.Emit("rosbridge_connect", map[string]any{"n": n, "url": c.rosbridge}) },
		OnDisconnect: func(err error) { a.trace.Emit("rosbridge_disconnect", map[string]any{"error": fmt.Sprint(err)}) },
		OnCancel: func(x rosbridge.Cancel) {
			a.trace.Emit("nav_cancel", map[string]any{"action": x.Action, "why": x.Why, "goals": x.Goals,
				"ms": float64(x.Took.Microseconds()) / 1000, "error": errString(x.Err)})
		}})
	var det robot.Detector = robot.None{}
	if c.detector == "stub" {
		a.stub = robot.NewStub()
		det = a.stub
	}
	var protocols []string
	for _, p := range strings.Split(c.protocol, ",") {
		if p = strings.TrimSpace(p); p != "" {
			protocols = append(protocols, p)
		}
	}
	var srv *server.Server
	a.robot = robot.New(rc, robot.Options{Clock: a.clk, Bridge: a.br, Logger: logger, Trace: a.trace, Detector: det,
		Payload: c.payload, MotionScanMs: c.motionScan, PoseThrottleMs: c.poseThr, ScanMaxAge: c.scanMaxAge, SensorTimeout: c.sensorTO,
		FrameTimeout: c.frameTO, ImageWidth: c.imageWidth, JPEGQuality: c.jpegQ, GoalTimeout: c.goalTO,
		StuckS: c.stuckS, ObstacleM: c.obstacleM, ObstacleHoldS: c.obstacleH, LowBatteryPct: c.lowBattery,
		BatteryTopic: c.battery == "topic", BatteryStart: c.batteryInit, NotifyContext: c.notifyCtx,
		SimStepIters: c.simStep,
		OnEvent: func(e robot.Event) {
			if srv != nil {
				srv.Notify(e)
			}
		}})
	srv = server.New(a.robot, a.br, server.Options{Version: version, ProtocolVersions: protocols,
		WaitParam: c.waitParam, NoLookPlace: !c.lookPlace, Logger: logger, Trace: a.trace})
	a.srv = srv
	sum := sha256.Sum256(append(append([]byte(nil), raw...), fmt.Sprintf("%+v", *c)...))
	a.trace.Emit("run_start", map[string]any{"version": version, "config_hash": hex.EncodeToString(sum[:8]),
		"rosbridge": c.rosbridge, "transport": c.transport, "clock": c.clockMode, "payload": c.payload,
		"motion_scan_ms": c.motionScan, "detector": c.detector, "protocol": protocols, "places": rc.IDs(),
		"catalog_tokens_est": srv.CatalogTokens()})
	return a, nil
}

func run(c *config, stdout, stderr io.Writer) error {
	if c.showVersion {
		fmt.Fprintln(stdout, "robot-mcp", version)
		return nil
	}
	if c.printTools || c.printInventory {
		c.tracePath = ""
	}
	a, err := build(c, stderr)
	if err != nil {
		return err
	}
	defer a.trace.Close()
	switch {
	case c.printTools:
		b, err := a.srv.ToolsJSON()
		if err != nil {
			return err
		}
		fmt.Fprintln(stdout, string(b))
		fmt.Fprintf(stderr, "robot-mcp: %d tools, catalog ≈%d tokens (Cookbook, o200k estimate), tools/list %d B\n",
			len(a.srv.Tools()), a.srv.CatalogTokens(), len(b))
		return nil
	case c.printInventory:
		fmt.Fprintln(stdout, canon.String(a.robot.Inventory()))
		return nil
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	err = a.serve(ctx, stderr)
	a.srv.Close()
	a.trace.Emit("run_end", map[string]any{"diag": a.srv.Diag(), "error": errString(err)})
	return err
}

func errString(err error) any {
	if err == nil {
		return nil
	}
	return err.Error()
}

// serve работает до отмены ctx (сигнал) или конца сессии stdio. Соединение с rosbridge и таймеры живут
// дольше: при завершении активная цель Nav2 отменяется (robot.Shutdown, не дольше -cancel-timeout), в
// HTTP — до остановки сервера; повторный вызов при выходе снимает цель, начатую в это время.
func (a *app) serve(ctx context.Context, stderr io.Writer) error {
	bctx, cancel := context.WithCancel(context.Background())
	brDone := make(chan struct{})
	tickDone := make(chan struct{})
	defer func() {
		_ = a.robot.Shutdown(bctx) // итог отмены — в журнале (nav_cancel)
		cancel()
		<-brDone
		<-tickDone
	}()
	go func() { a.br.Run(bctx); close(brDone) }()
	a.robot.Start(bctx)
	go func() {
		defer close(tickDone)
		if a.vclock != nil {
			return // в виртуальных часах таймеры проверяются на /control/clock
		}
		t := time.NewTicker(a.cfg.tick)
		defer t.Stop()
		for {
			select {
			case <-bctx.Done():
				return
			case <-t.C:
				a.robot.Tick()
			}
		}
	}()
	if a.cfg.controlAddr != "" {
		srv, url, err := a.listen(a.cfg.controlAddr, a.controlMux(nil))
		if err != nil {
			return err
		}
		fmt.Fprintf(stderr, "robot-mcp: control on %s\n", url)
		defer srv.Close()
	}
	if a.cfg.transport == "stdio" {
		err := a.srv.MCP().Run(ctx, &mcp.StdioTransport{})
		if ctx.Err() != nil || errors.Is(err, io.EOF) {
			return nil
		}
		return err
	}
	h := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return a.srv.MCP() }, nil)
	srv, url, err := a.listen(a.cfg.addr, a.controlMux(h))
	if err != nil {
		return err
	}
	url += a.cfg.path
	fmt.Fprintf(stderr, "robot-mcp: listening on %s (rosbridge %s, clock %s)\n", url, a.cfg.rosbridge, a.cfg.clockMode)
	if a.cfg.readyFile != "" {
		if err := os.WriteFile(a.cfg.readyFile, []byte(url+"\n"), 0o644); err != nil {
			return err
		}
	}
	<-ctx.Done()
	// Сначала цель: закрытие сессии ждёт идущих вызовов, а go_to с wait=true ждёт конца задачи. Затем сессии
	// MCP: поток уведомлений подписчика (GET SSE) висит, пока открыта сессия, и держал бы Shutdown до таймаута.
	_ = a.robot.Shutdown(bctx)
	for ss := range a.srv.MCP().Sessions() {
		_ = ss.Close()
	}
	sctx, scancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer scancel()
	return srv.Shutdown(sctx)
}

func (a *app) listen(addr string, h http.Handler) (*http.Server, string, error) {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, "", err
	}
	srv := &http.Server{Handler: h, ReadHeaderTimeout: 10 * time.Second}
	go func() {
		if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			a.log.Error("http server", "err", err)
		}
	}()
	return srv, "http://" + ln.Addr().String(), nil
}

// controlMux — /healthz, /control/* (часы, состояние, заряд, детектор) и, если mcpHandler != nil, MCP.
func (a *app) controlMux(mcpHandler http.Handler) *http.ServeMux {
	mux := http.NewServeMux()
	if mcpHandler != nil {
		mux.Handle(a.cfg.path, mcpHandler)
	}
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "now": a.clk.Now(), "clock": a.cfg.clockMode,
			"rosbridge": a.br.Connected()})
	})
	mux.HandleFunc("GET /control/state", func(w http.ResponseWriter, r *http.Request) {
		n := a.srv.NotifyStats()
		writeJSON(w, http.StatusOK, map[string]any{"now": a.clk.Now(), "active": a.robot.Active(),
			"connected": a.br.Connected(), "notifications": n["sent"], "notify": n, "state": a.robot.State(),
			"diag": a.srv.Diag()})
	})
	mux.HandleFunc("POST /control/clock", func(w http.ResponseWriter, r *http.Request) {
		if a.vclock == nil {
			writeJSON(w, http.StatusConflict, map[string]any{"error": "clock is real; start with -clock virtual"})
			return
		}
		var body struct {
			T *float64 `json:"t"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&body); err != nil || body.T == nil {
			writeJSON(w, http.StatusBadRequest, map[string]any{"error": `body must be {"t": <seconds>}`})
			return
		}
		if err := a.vclock.Set(*body.T); err != nil {
			writeJSON(w, http.StatusConflict, map[string]any{"error": err.Error(), "now": a.vclock.Now()})
			return
		}
		a.robot.Tick()
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "now": a.vclock.Now(), "active": a.robot.Active()})
	})
	mux.HandleFunc("POST /control/battery", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Pct *float64 `json:"pct"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&body); err != nil || body.Pct == nil {
			writeJSON(w, http.StatusBadRequest, map[string]any{"error": `body must be {"pct": <0..100>}`})
			return
		}
		a.robot.SetBattery(*body.Pct)
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "state": a.robot.State()})
	})
	mux.HandleFunc("POST /control/detector", func(w http.ResponseWriter, r *http.Request) {
		if a.stub == nil {
			writeJSON(w, http.StatusConflict, map[string]any{"error": "start with -detector stub"})
			return
		}
		var body struct {
			Place   string         `json:"place"`
			Objects []robot.Object `json:"objects"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&body); err != nil || body.Place == "" {
			writeJSON(w, http.StatusBadRequest, map[string]any{"error": `body must be {"place": id, "objects": [...]}`})
			return
		}
		a.stub.Set(body.Place, body.Objects)
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	})
	return mux
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
