// Команда cam-mcp — MCP-сервер камеры с детектором движения на плате.
//
//	cam-mcp [-source synthetic|dir|v4l2] [-transport stdio|http] [-clock real|virtual] ...
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
	"regexp"
	"strings"
	"syscall"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/cammcp/internal/camera"
	"github.com/local/cammcp/internal/canon"
	"github.com/local/cammcp/internal/clock"
	"github.com/local/cammcp/internal/detect"
	"github.com/local/cammcp/internal/scene"
	"github.com/local/cammcp/internal/server"
	"github.com/local/cammcp/internal/source"
	"github.com/local/cammcp/internal/trace"
)

// version задаётся при сборке: -ldflags "-X main.version=..."
var version = "dev"

type config struct {
	device, location                 string
	source, scene, dir               string
	dirFPS                           float64
	v4l2Device, v4l2Format           string
	v4l2W, v4l2H                     int
	det                              detect.Params
	idleFPS, activeFPS               float64
	snapWidth, snapQuality           int
	setDetector                      bool
	transport, addr, path, readyFile string
	clockMode                        string
	clockStart                       float64
	controlAddr, tracePath, runID    string
	protocol, logLevel               string
	printTools, showVersion          bool
}

// size — флаг вида 160x120.
type size struct{ w, h *int }

func (s size) String() string {
	if s.w == nil {
		return ""
	}
	return fmt.Sprintf("%dx%d", *s.w, *s.h)
}

func (s size) Set(v string) error {
	var w, h int
	if n, err := fmt.Sscanf(v, "%dx%d", &w, &h); err != nil || n != 2 || w <= 0 || h <= 0 || fmt.Sprintf("%dx%d", w, h) != v {
		return fmt.Errorf("size must look like 160x120, got %q", v)
	}
	*s.w, *s.h = w, h
	return nil
}

func parseFlags(args []string, stderr io.Writer) (*config, error) {
	c := &config{det: detect.DefaultParams(), v4l2W: 640, v4l2H: 480}
	fs := flag.NewFlagSet("cam-mcp", flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.StringVar(&c.device, "device", "cam1", "id камеры в инвентаре и в URI cam://state/<device>")
	fs.StringVar(&c.location, "location", "", "место установки камеры (инвентарь)")
	fs.StringVar(&c.source, "source", "synthetic", "источник кадров: synthetic | dir | v4l2")
	fs.StringVar(&c.scene, "scene", "", "сценарий синтетической сцены (JSON); пусто — встроенный demo")
	fs.StringVar(&c.dir, "dir", "", "каталог кадров PGM/PNG/JPEG (-source dir)")
	fs.Float64Var(&c.dirFPS, "dir-fps", 5, "частота кадров каталога")
	fs.StringVar(&c.v4l2Device, "v4l2-device", "/dev/video0", "устройство V4L2 (-source v4l2)")
	fs.StringVar(&c.v4l2Format, "v4l2-format", "auto", "формат V4L2: auto | yuyv | mjpeg")
	fs.Var(size{&c.v4l2W, &c.v4l2H}, "v4l2-size", "запрошенный размер кадра V4L2 (драйвер выберет ближайший)")
	fs.Var(size{&c.det.W, &c.det.H}, "det-size", "разрешение детектора")
	fs.IntVar(&c.det.Threshold, "threshold", c.det.Threshold, "порог разности яркости, уровни 0..255")
	minArea := fs.Float64("min-area", c.det.MinArea*100, "минимальная площадь области движения, % кадра")
	maxArea := fs.Float64("max-area", c.det.MaxArea*100, "доля изменившегося кадра (%), выше которой фон переучивается")
	bgTau := fs.Duration("bg-tau", time.Duration(c.det.BgTau*float64(time.Second)), "постоянная времени фона (до 1h)")
	fs.IntVar(&c.det.K, "k", c.det.K, "эпизод начинается при k активных кадрах из n последних")
	fs.IntVar(&c.det.N, "n", c.det.N, "окно гистерезиса; n неактивных кадров подряд завершают эпизод")
	fs.Float64Var(&c.idleFPS, "idle-fps", 2, "частота обработки без движения, кадров/с")
	fs.Float64Var(&c.activeFPS, "active-fps", 8, "частота обработки при движении, кадров/с")
	fs.IntVar(&c.snapWidth, "snapshot-width", 320, "наибольшая ширина снимка, px (и по умолчанию), 64..1280")
	fs.IntVar(&c.snapQuality, "snapshot-quality", 60, "наибольшее качество JPEG снимка (и по умолчанию), 20..95")
	fs.BoolVar(&c.setDetector, "set-detector", false, "инструмент set_detector (≈85 токенов каталога в каждом запросе)")
	fs.StringVar(&c.transport, "transport", "stdio", "транспорт MCP: stdio | http (streamable HTTP)")
	fs.StringVar(&c.addr, "addr", "127.0.0.1:0", "адрес HTTP (порт 0 — свободный; адрес печатается в stderr и -ready-file)")
	fs.StringVar(&c.path, "path", "/mcp", "путь конечной точки MCP")
	fs.StringVar(&c.readyFile, "ready-file", "", "записать сюда URL после запуска HTTP")
	fs.StringVar(&c.clockMode, "clock", "real", "часы: real | virtual (кадры по POST /control/clock)")
	fs.Float64Var(&c.clockStart, "clock-start", 0, "начальное время виртуальных часов, с")
	fs.StringVar(&c.controlAddr, "control-addr", "", "отдельный HTTP для /control/clock (stdio + virtual)")
	fs.StringVar(&c.tracePath, "trace", "", "журнал cam-mcp (JSONL): эпизоды, вызовы инструментов, ошибки")
	fs.StringVar(&c.runID, "run-id", "", "идентификатор прогона в журнале")
	fs.StringVar(&c.protocol, "protocol", "2025-11-25", "ревизии MCP через запятую (пусто — все, что знает SDK)")
	fs.StringVar(&c.logLevel, "log-level", "warn", "уровень журнала в stderr: debug | info | warn | error")
	fs.BoolVar(&c.printTools, "print-tools", false, "напечатать каталог инструментов (tools/list) и выйти")
	fs.BoolVar(&c.showVersion, "version", false, "версия")
	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	if fs.NArg() > 0 {
		return nil, fmt.Errorf("unexpected arguments: %v", fs.Args())
	}
	c.det.MinArea, c.det.MaxArea, c.det.BgTau = *minArea/100, *maxArea/100, bgTau.Seconds()
	switch {
	case c.source != "synthetic" && c.source != "dir" && c.source != "v4l2":
		return nil, errors.New("-source must be synthetic, dir or v4l2")
	case c.source == "dir" && c.dir == "":
		return nil, errors.New("-source dir needs -dir")
	case c.source == "v4l2" && c.clockMode == "virtual":
		return nil, errors.New("-source v4l2 needs -clock real: a camera cannot run in virtual time")
	case c.transport != "stdio" && c.transport != "http":
		return nil, errors.New("-transport must be stdio or http")
	case c.clockMode != "real" && c.clockMode != "virtual":
		return nil, errors.New("-clock must be real or virtual")
	case !plainPath.MatchString(c.path) || c.path == "/healthz" || c.path == "/control/clock":
		return nil, errors.New("-path must be a plain URL path like /mcp, other than /healthz and /control/clock")
	}
	return c, c.det.Validate()
}

// plainPath — путь -path без шаблонных и особых символов http.ServeMux (иначе Handle паникует).
var plainPath = regexp.MustCompile(`^/[\w./-]*$`)

func main() {
	c, err := parseFlags(os.Args[1:], os.Stderr)
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			os.Exit(0)
		}
		fmt.Fprintln(os.Stderr, "cam-mcp:", err)
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	err = run(ctx, c, os.Stdout, os.Stderr)
	stop()
	if err != nil {
		fmt.Fprintln(os.Stderr, "cam-mcp:", err)
		os.Exit(1)
	}
}

// app — собранные компоненты.
type app struct {
	cfg    *config
	clk    clock.Clock
	vclock *clock.Virtual
	cam    *camera.Camera
	srv    *server.Server
	trace  *trace.Writer
	log    *slog.Logger
}

func openSource(c *config) (source.Source, error) {
	switch c.source {
	case "dir":
		return source.OpenDir(c.dir, c.dirFPS)
	case "v4l2":
		return source.OpenV4L2(c.v4l2Device, c.v4l2Format, c.v4l2W, c.v4l2H, c.activeFPS)
	}
	sc, name := scene.Demo(), "demo"
	if c.scene != "" {
		var err error
		if sc, err = scene.Load(c.scene); err != nil {
			return nil, err
		}
		name = c.scene
	}
	return source.Synthetic{Scene: scene.New(sc), Name: name}, nil
}

func build(c *config, stderr io.Writer) (*app, error) {
	var level slog.Level
	if err := level.UnmarshalText([]byte(c.logLevel)); err != nil {
		return nil, fmt.Errorf("-log-level: %w", err)
	}
	a := &app{cfg: c, log: slog.New(slog.NewTextHandler(stderr, &slog.HandlerOptions{Level: level}))}
	if c.clockMode == "virtual" {
		a.vclock = clock.NewVirtual(c.clockStart)
		a.clk = a.vclock
	} else {
		a.clk = clock.NewReal()
	}
	src, err := openSource(c)
	if err != nil {
		return nil, err
	}
	if c.tracePath != "" {
		if a.trace, err = trace.Open(c.tracePath, a.clk, c.runID); err != nil {
			return nil, errors.Join(err, src.Close())
		}
	}
	a.cam, err = camera.New(camera.Options{Device: c.device, Source: src, Detector: c.det, IdleFPS: c.idleFPS,
		ActiveFPS: c.activeFPS, Clock: a.clk, Trace: a.trace, OnEvent: func(e camera.Event) { a.srv.Notify(e) }})
	if err == nil {
		a.srv, err = server.New(a.cam, server.Options{Version: version, ProtocolVersions: protocols(c.protocol),
			Location: c.location, SnapshotWidth: c.snapWidth, SnapshotQuality: c.snapQuality,
			SetDetector: c.setDetector, Logger: a.log, Trace: a.trace})
	}
	if err != nil {
		return nil, errors.Join(err, src.Close(), a.trace.Close())
	}
	info := a.cam.Info()
	_, idle, active := a.cam.Params()
	cfg := map[string]any{"version": version, "device": c.device, "location": c.location, "transport": c.transport,
		"clock": c.clockMode, "protocol": protocols(c.protocol),
		"source": map[string]any{"kind": info.Kind, "name": info.Name, "w": info.W, "h": info.H,
			"format": info.Format, "fps": info.FPS},
		"detector": map[string]any{"w": c.det.W, "h": c.det.H, "threshold": c.det.Threshold,
			"min_area": c.det.MinArea, "max_area": c.det.MaxArea, "bg_tau_s": c.det.BgTau, "k": c.det.K, "n": c.det.N},
		"fps":      map[string]any{"idle": idle, "active": active},
		"snapshot": map[string]any{"width": c.snapWidth, "quality": c.snapQuality}, "set_detector": c.setDetector}
	b, err := canon.Marshal(cfg)
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(b)
	cfg["config_hash"] = hex.EncodeToString(sum[:8])
	a.trace.Emit("run_start", cfg)
	return a, nil
}

func protocols(list string) []string {
	var out []string
	for _, p := range strings.Split(list, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// run работает до отмены ctx (сигнал) или конца stdio-сессии.
func run(ctx context.Context, c *config, stdout, stderr io.Writer) error {
	if c.showVersion {
		fmt.Fprintln(stdout, "cam-mcp", version)
		return nil
	}
	if c.printTools {
		c.source, c.scene, c.tracePath = "synthetic", "", "" // каталог не зависит от источника
	}
	a, err := build(c, stderr)
	if err != nil {
		return err
	}
	defer a.trace.Close()
	if c.printTools {
		a.srv.Close()
		b, err := a.srv.ToolsJSON()
		if err != nil {
			return err
		}
		_, err = fmt.Fprintln(stdout, string(b))
		return errors.Join(err, a.cam.Close())
	}
	err = a.serve(ctx, stderr)
	a.srv.Close()
	a.trace.Emit("run_end", map[string]any{"stats": a.cam.Status(), "notify_dispatch": a.srv.NotifyStats(),
		"error": errString(err)})
	return errors.Join(err, a.cam.Close(), a.trace.Err())
}

func errString(err error) any {
	if err == nil {
		return nil
	}
	return err.Error()
}

// shutdownTimeout — сколько ждать закрытия транспорта после сигнала.
const shutdownTimeout = 3 * time.Second

// serve запускает обработку кадров (в реальном времени) и транспорт; возвращается по отмене ctx или
// концу stdio-сессии. Транспорт ждётся не дольше shutdownTimeout: запись клиенту, который не читает
// stdout, может висеть, но выход процесса она не держит.
func (a *app) serve(ctx context.Context, stderr io.Writer) error {
	ctx, cancel := context.WithCancel(ctx)
	camDone := make(chan struct{})
	defer func() { cancel(); <-camDone }()
	if a.vclock == nil {
		go func() { a.cam.Run(ctx); close(camDone) }()
	} else {
		close(camDone)
	}

	if a.cfg.controlAddr != "" {
		srv, url, err := a.listen(a.cfg.controlAddr, a.controlMux(nil))
		if err != nil {
			return err
		}
		fmt.Fprintf(stderr, "cam-mcp: control on %s\n", url)
		defer srv.Close()
	}

	if a.cfg.transport == "stdio" {
		done := make(chan error, 1)
		go func() { done <- a.srv.MCP().Run(ctx, &mcp.StdioTransport{}) }()
		select {
		case err := <-done:
			if ctx.Err() != nil || errors.Is(err, io.EOF) {
				return nil
			}
			return err
		case <-ctx.Done():
		}
		select {
		case <-done:
		case <-time.After(shutdownTimeout):
			a.log.Warn("stdio session did not close in time (client is not reading?)", "timeout", shutdownTimeout)
		}
		return nil
	}
	h := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return a.srv.MCP() }, nil)
	srv, url, err := a.listen(a.cfg.addr, a.controlMux(h))
	if err != nil {
		return err
	}
	url += a.cfg.path
	fmt.Fprintf(stderr, "cam-mcp: listening on %s (source %s, clock %s)\n", url, a.cfg.source, a.cfg.clockMode)
	if a.cfg.readyFile != "" {
		if err := os.WriteFile(a.cfg.readyFile, []byte(url+"\n"), 0o644); err != nil {
			return err
		}
	}
	<-ctx.Done()
	// поток SSE сессии держит запрос открытым, а Shutdown обработчики не прерывает: сначала закрываются
	// сессии MCP, а клиент, не отпустивший соединение за shutdownTimeout, отключается принудительно
	for ss := range a.srv.MCP().Sessions() {
		_ = ss.Close() // ошибка закрытия сессии при остановке не меняет итог
	}
	shutdown, cancelShutdown := context.WithTimeout(context.Background(), shutdownTimeout)
	defer cancelShutdown()
	if err := srv.Shutdown(shutdown); err != nil {
		a.log.Warn("http clients did not disconnect in time; closing", "timeout", shutdownTimeout)
		return srv.Close()
	}
	return nil
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

// controlMux — /healthz, /control/clock (виртуальные часы) и, если mcpHandler != nil, конечная точка MCP.
func (a *app) controlMux(mcpHandler http.Handler) *http.ServeMux {
	mux := http.NewServeMux()
	if mcpHandler != nil {
		mux.Handle(a.cfg.path, mcpHandler)
	}
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "now": a.clk.Now(), "clock": a.cfg.clockMode})
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
		if err := a.cam.AdvanceTo(a.vclock, *body.T); err != nil {
			writeJSON(w, http.StatusConflict, map[string]any{"error": err.Error(), "now": a.vclock.Now()})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "now": a.vclock.Now()})
	})
	return mux
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
