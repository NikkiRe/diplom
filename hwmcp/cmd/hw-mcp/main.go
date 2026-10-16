// Команда hw-mcp — MCP-сервер контекста оборудования одноплатного компьютера.
//
//	hw-mcp [-root /] [-declared declared.json] [-catalog generic|explicit] [-transport stdio|http] ...
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
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/hwmcp/internal/clock"
	"github.com/local/hwmcp/internal/declared"
	"github.com/local/hwmcp/internal/gate"
	"github.com/local/hwmcp/internal/gpio"
	"github.com/local/hwmcp/internal/hub"
	"github.com/local/hwmcp/internal/inventory"
	"github.com/local/hwmcp/internal/server"
	"github.com/local/hwmcp/internal/sysfs"
	"github.com/local/hwmcp/internal/trace"
)

// version задаётся при сборке: -ldflags "-X main.version=..."
var version = "dev"

type config struct {
	root           string
	declared       string
	catalog        string
	transport      string
	addr           string
	path           string
	readyFile      string
	clockMode      string
	clockStart     float64
	controlAddr    string
	samplePeriod   time.Duration
	cacheTTL       time.Duration
	rescanPeriod   time.Duration
	sessionTimeout time.Duration
	gpioDriver     string
	gpioUAPI       string
	gpioDir        string
	gpioEdges      bool
	tracePath      string
	runID          string
	protocol       string
	boardHealth    bool
	logLevel       string

	printInventory, printTools, printTD, printText, showVersion bool
}

// realRoot — корень настоящий (-root /: плата или ВМ), а не каталог фикстуры или симулятора.
func (c *config) realRoot() bool { return filepath.Clean(c.root) == "/" }

func parseFlags(args []string, stderr io.Writer) (*config, error) {
	c := &config{}
	fs := flag.NewFlagSet("hw-mcp", flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.StringVar(&c.root, "root", "/", "корень sysfs/procfs: / на плате, каталог фикстуры для эмуляции")
	fs.StringVar(&c.declared, "declared", "", "declared.json: назначение GPIO, места, политика шлюза")
	fs.StringVar(&c.catalog, "catalog", server.CatalogGeneric, "каталог инструментов: generic | explicit")
	fs.StringVar(&c.transport, "transport", "stdio", "транспорт MCP: stdio | http (streamable HTTP)")
	fs.StringVar(&c.addr, "addr", "127.0.0.1:0", "адрес HTTP (порт 0 — свободный; адрес печатается в stderr и -ready-file)")
	fs.StringVar(&c.path, "path", "/mcp", "путь конечной точки MCP")
	fs.StringVar(&c.readyFile, "ready-file", "", "записать сюда URL после запуска HTTP")
	fs.StringVar(&c.clockMode, "clock", "real", "часы: real | virtual (время двигает POST /control/clock)")
	fs.Float64Var(&c.clockStart, "clock-start", 0, "начальное время виртуальных часов, с")
	fs.StringVar(&c.controlAddr, "control-addr", "", "отдельный HTTP для /control/* (stdio + virtual)")
	fs.DurationVar(&c.samplePeriod, "sample-period", time.Second, "период фонового опроса датчиков")
	fs.DurationVar(&c.cacheTTL, "cache-ttl", 0, "read_property перечитывает значения старше (0 — 2 периода опроса)")
	fs.DurationVar(&c.rescanPeriod, "rescan-period", 10*time.Second,
		"пока устройство IIO/hwmon пропало, искать его новый путь: первый раз через этот период, дальше с удвоением до 5 мин (0 — не искать)")
	fs.DurationVar(&c.sessionTimeout, "session-timeout", time.Hour,
		"закрывать HTTP-сессию без запросов и без открытого потока SSE дольше этого (0 — не закрывать)")
	fs.StringVar(&c.gpioDriver, "gpio-driver", "auto",
		"линии GPIO: auto (cdev при -root /, иначе files) | cdev (/dev/gpiochipN) | files (эмуляция файлами)")
	fs.StringVar(&c.gpioUAPI, "gpio-uapi", gpio.UAPIAuto, "uAPI символьного устройства GPIO: auto (v2, при ENOTTY/EINVAL — v1) | v1 | v2")
	fs.StringVar(&c.gpioDir, "gpio-dir", "", "каталог линий: files — эмуляция (по умолчанию <root>/dev), cdev — узлы gpiochipN (/dev)")
	fs.BoolVar(&c.gpioEdges, "gpio-edges", true, "события фронтов входов GPIO (только драйвер cdev)")
	fs.StringVar(&c.tracePath, "trace", "", "журнал hw-mcp (JSONL): решения шлюза, аренда, уведомления")
	fs.StringVar(&c.runID, "run-id", "", "идентификатор прогона в журнале")
	fs.StringVar(&c.protocol, "protocol", "2025-11-25", "ревизии MCP через запятую (пусто — все, что знает SDK)")
	fs.BoolVar(&c.boardHealth, "board-health", true, "инструмент board_health")
	fs.StringVar(&c.logLevel, "log-level", "warn", "уровень журнала в stderr: debug | info | warn | error")
	fs.BoolVar(&c.printInventory, "print-inventory", false, "напечатать инвентарь sysfs (формат sysfs_probe.py) и выйти")
	fs.BoolVar(&c.printTools, "print-tools", false, "напечатать каталог инструментов (tools/list) и выйти")
	fs.BoolVar(&c.printTD, "print-td", false, "напечатать WoT TD устройств и выйти")
	fs.BoolVar(&c.printText, "print-text", false, "напечатать текстовую проекцию L0 и выйти")
	fs.BoolVar(&c.showVersion, "version", false, "версия")
	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	if fs.NArg() > 0 {
		return nil, fmt.Errorf("unexpected arguments: %v", fs.Args())
	}
	switch {
	case c.catalog != server.CatalogGeneric && c.catalog != server.CatalogExplicit:
		return nil, fmt.Errorf("-catalog must be generic or explicit")
	case c.transport != "stdio" && c.transport != "http":
		return nil, fmt.Errorf("-transport must be stdio or http")
	case c.clockMode != "real" && c.clockMode != "virtual":
		return nil, fmt.Errorf("-clock must be real or virtual")
	case c.samplePeriod <= 0:
		return nil, fmt.Errorf("-sample-period must be > 0")
	case c.rescanPeriod < 0 || c.sessionTimeout < 0:
		return nil, fmt.Errorf("-rescan-period and -session-timeout must be >= 0")
	case c.gpioDriver != "auto" && c.gpioDriver != "cdev" && c.gpioDriver != "files":
		return nil, fmt.Errorf("-gpio-driver must be auto, cdev or files")
	case c.gpioUAPI != gpio.UAPIAuto && c.gpioUAPI != gpio.UAPIv1 && c.gpioUAPI != gpio.UAPIv2:
		return nil, fmt.Errorf("-gpio-uapi must be auto, v1 or v2")
	}
	return c, nil
}

func main() {
	c, err := parseFlags(os.Args[1:], os.Stderr)
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			os.Exit(0)
		}
		fmt.Fprintln(os.Stderr, "hw-mcp:", err)
		os.Exit(2)
	}
	if err := run(c, os.Stdout, os.Stderr); err != nil {
		fmt.Fprintln(os.Stderr, "hw-mcp:", err)
		os.Exit(1)
	}
}

// app — собранные компоненты.
type app struct {
	cfg    *config
	clk    clock.Clock
	vclock *clock.Virtual
	hub    *hub.Hub
	srv    *server.Server
	trace  *trace.Writer
	log    *slog.Logger
}

func build(c *config, stderr io.Writer) (*app, error) {
	var level slog.Level
	if err := level.UnmarshalText([]byte(c.logLevel)); err != nil {
		return nil, fmt.Errorf("-log-level: %w", err)
	}
	logger := slog.New(slog.NewTextHandler(stderr, &slog.HandlerOptions{Level: level}))

	var decl *declared.Declared
	var declRaw []byte
	if c.declared != "" {
		var err error
		if declRaw, err = os.ReadFile(c.declared); err != nil {
			return nil, err
		}
		if decl, err = declared.Parse(declRaw); err != nil {
			return nil, err
		}
	}
	a := &app{cfg: c, log: logger}
	if c.clockMode == "virtual" {
		a.vclock = clock.NewVirtual(c.clockStart)
		a.clk = a.vclock
	} else {
		a.clk = clock.NewReal()
	}

	var gateCfg gate.Config
	var samplePeriods, stuckAfter map[string]float64
	if decl != nil {
		samplePeriods, stuckAfter = decl.SamplePeriods, decl.StuckAfter
		var err error
		if gateCfg, err = gate.ParseConfig(decl.Gate); err != nil {
			return nil, err
		}
	}
	leased := map[string]bool{}
	for name, d := range gateCfg.Devices {
		if d.Lease != nil {
			leased[name] = true
		}
	}
	raw := sysfs.Probe(c.root, decl)
	inv, err := inventory.Build(raw, decl, inventory.Options{Leased: leased})
	if err != nil {
		return nil, err
	}

	if c.tracePath != "" {
		if a.trace, err = trace.Open(c.tracePath, a.clk, c.runID); err != nil {
			return nil, err
		}
	}
	var protocols []string
	for _, p := range strings.Split(c.protocol, ",") {
		if p = strings.TrimSpace(p); p != "" {
			protocols = append(protocols, p)
		}
	}
	sum := sha256.Sum256(append(append([]byte(nil), declRaw...), []byte(fmt.Sprintf("|%s|%s|%s|%v|%s|%s|%v",
		c.root, c.catalog, c.clockMode, c.samplePeriod, c.gpioDriver, c.gpioUAPI, c.rescanPeriod))...))
	a.trace.Emit("run_start", map[string]any{
		"config_hash": hex.EncodeToString(sum[:8]), "version": version, "root": c.root, "declared": c.declared,
		"catalog": c.catalog, "transport": c.transport, "clock": c.clockMode, "protocol": protocols,
		"sample_period_s": c.samplePeriod.Seconds(), "rescan_period_s": c.rescanPeriod.Seconds(),
		"session_timeout_s": c.sessionTimeout.Seconds(), "gate": json.RawMessage(nonEmptyJSON(decl)),
		"devices": deviceIDs(inv),
	})
	backend, edges := gpioBackend(c)
	a.hub, err = hub.New(inv, gateCfg, backend, hub.Options{
		Clock: a.clk, SamplePeriod: c.samplePeriod.Seconds(), CacheTTL: c.cacheTTL.Seconds(),
		Periods: samplePeriods, StuckAfter: stuckAfter, RescanPeriod: c.rescanPeriod.Seconds(),
		KeepOpen: c.realRoot(), Trace: a.trace, Logger: logger, Root: c.root, Edges: edges,
	})
	if err != nil {
		return nil, err
	}
	if a.srv, err = server.New(a.hub, server.Options{Catalog: c.catalog, BoardHealth: c.boardHealth,
		Version: version, ProtocolVersions: protocols, Logger: logger}); err != nil {
		return nil, err
	}
	g := a.hub.Gate()
	ready := map[string]any{
		"max_state_age_s": g.MaxStateAge, "default_ttl_s": g.DefaultTTL, "commit_window_s": g.CommitWindow,
		"gate_devices": g.Devices(), "n_tools": len(a.srv.Tools()), "gpio_backend": backend.Name(),
		"gpio_edges": edges, "sample_periods": a.hub.SamplePeriods(), "sysfs_keep_open": c.realRoot(),
	}
	if cd, ok := backend.(*gpio.Cdev); ok {
		ready["gpio_chips"] = cd.Chips()
	}
	a.trace.Emit("ready", ready)
	return a, nil
}

// gpioBackend выбирает драйвер линий: auto — /dev/gpiochipN на настоящем корне (-root /), иначе эмуляция
// файлами; cdev и files — явно. cdev с -root каталога симулятора — смешанный режим: sysfs эмулируется,
// линии GPIO настоящие (например, gpio-sim ядра в ВМ). Фронты входов бывают только у cdev.
func gpioBackend(c *config) (gpio.Backend, bool) {
	driver := c.gpioDriver
	if driver == "auto" {
		driver = "files"
		if c.realRoot() {
			driver = "cdev"
		}
	}
	if driver == "cdev" {
		return &gpio.Cdev{DevDir: c.gpioDir, UAPI: c.gpioUAPI}, c.gpioEdges
	}
	dir := c.gpioDir
	if dir == "" {
		dir = filepath.Join(c.root, "dev")
	}
	return &gpio.Files{Dir: dir}, false
}

func nonEmptyJSON(d *declared.Declared) []byte {
	if d == nil || len(d.Gate) == 0 {
		return []byte("null")
	}
	return d.Gate
}

func deviceIDs(inv *inventory.Inventory) []string {
	out := make([]string, 0, len(inv.Devices))
	for _, d := range inv.Devices {
		out = append(out, d.ID)
	}
	return out
}

func run(c *config, stdout, stderr io.Writer) error {
	if c.showVersion {
		fmt.Fprintln(stdout, "hw-mcp", version)
		return nil
	}
	if c.printInventory {
		var decl *declared.Declared
		if c.declared != "" {
			var err error
			if decl, err = declared.Load(c.declared); err != nil {
				return err
			}
		}
		b, err := sysfs.MarshalInventory(sysfs.Probe(c.root, decl), 1)
		if err != nil {
			return err
		}
		_, err = fmt.Fprintln(stdout, string(b))
		return err
	}
	// печать каталога не должна трогать линии GPIO настоящей платы; линии пустой эмуляции «неизвестны»,
	// и предупреждения об этом в stderr только мешали бы
	if c.printTools || c.printTD || c.printText {
		tmp, err := os.MkdirTemp("", "hw-mcp-print-")
		if err != nil {
			return err
		}
		defer os.RemoveAll(tmp)
		c.gpioDir, c.gpioDriver, c.tracePath, c.logLevel = tmp, "files", "", "error"
	}
	a, err := build(c, stderr)
	if err != nil {
		return err
	}
	defer a.trace.Close()
	defer a.srv.Close()
	switch {
	case c.printTools:
		b, err := a.srv.ToolsJSON()
		if err != nil {
			return err
		}
		_, err = fmt.Fprintln(stdout, string(b))
		return err
	case c.printTD:
		b, err := json.MarshalIndent(a.hub.Inventory().TDs(), "", " ")
		if err != nil {
			return err
		}
		_, err = fmt.Fprintln(stdout, string(b))
		return err
	case c.printText:
		_, err := fmt.Fprintln(stdout, a.hub.Inventory().Text())
		return err
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	err = a.serve(ctx, stderr)
	a.hub.Close() // уже вызван по сигналу или концу сессии (serve); повторный вызов ничего не делает
	a.srv.Close()
	a.trace.Emit("run_end", map[string]any{"stats": a.hub.Stats(), "notify_dispatch": a.srv.NotifyStats(),
		"http_sessions": a.srv.SessionStats(), "error": errString(err)})
	return err
}

func errString(err error) any {
	if err == nil {
		return nil
	}
	return err.Error()
}

// shutdownTimeout — сколько ждать закрытия транспорта после сигнала (приводы к этому моменту уже
// в безопасном состоянии).
const shutdownTimeout = 3 * time.Second

// serve запускает таймеры и транспорт; возвращается по отмене ctx или концу stdio-сессии.
//
// Порядок остановки: по сигналу (или концу сессии) приводы сразу уходят в safe_state (hub.Close не
// зависит от транспорта), затем закрывается транспорт. Транспорт ждётся не дольше shutdownTimeout:
// запись клиенту, который не читает stdout, может висеть бесконечно, но выход процесса она не держит.
func (a *app) serve(ctx context.Context, stderr io.Writer) error {
	ctx, cancel := context.WithCancel(ctx)
	var wg sync.WaitGroup
	defer func() { cancel(); wg.Wait() }()
	context.AfterFunc(ctx, a.hub.Close)
	if a.vclock == nil {
		// сторожевой таймер и опрос — в разных горутинах: чтение DS18B20 (~750 мс) не задерживает аренду;
		// оба спят до ближайшего срока (hub/run.go), без аренд и импульсов таймер не просыпается
		wg.Add(2)
		go func() { defer wg.Done(); a.hub.RunTimers(ctx) }()
		go func() { defer wg.Done(); a.hub.RunSampler(ctx) }()
	}

	if a.cfg.controlAddr != "" {
		srv, url, err := a.listen(a.cfg.controlAddr, a.controlMux(nil))
		if err != nil {
			return err
		}
		fmt.Fprintf(stderr, "hw-mcp: control on %s\n", url)
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
	srv, url, err := a.listen(a.cfg.addr, a.controlMux(a.srv.ReapIdleSessions(h, a.cfg.sessionTimeout)))
	if err != nil {
		return err
	}
	url += a.cfg.path
	fmt.Fprintf(stderr, "hw-mcp: listening on %s (catalog %s, clock %s)\n", url, a.cfg.catalog, a.cfg.clockMode)
	if a.cfg.readyFile != "" {
		if err := os.WriteFile(a.cfg.readyFile, []byte(url+"\n"), 0o644); err != nil {
			return err
		}
	}
	<-ctx.Done()
	shutdown, cancelShutdown := context.WithTimeout(context.Background(), shutdownTimeout)
	defer cancelShutdown()
	return srv.Shutdown(shutdown)
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
		if err := a.hub.AdvanceTo(a.vclock, *body.T); err != nil {
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
