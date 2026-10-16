// Команда edgectx — ИИ-агент одноплатного компьютера (ВКР «Разработка ИИ-агента
// для приложений граничного ИИ»).
//
// Использование:
//
//	edgectx -config configs/example.yaml [-trace run/trace.jsonl] [-listen 127.0.0.1:18080] [-run-id id]
//	edgectx -config c.yaml -check-config
//	edgectx -version
//
// Остановка — SIGINT/SIGTERM или POST /v1/shutdown: агент дорабатывает текущие
// ходы, пишет run_end и закрывает MCP-серверы.
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/local/edgectx/internal/agent"
	"github.com/local/edgectx/internal/backoff"
	"github.com/local/edgectx/internal/channel"
	"github.com/local/edgectx/internal/clock"
	"github.com/local/edgectx/internal/config"
	"github.com/local/edgectx/internal/llm"
	"github.com/local/edgectx/internal/trace"
	"github.com/local/edgectx/internal/userctx"
)

// version задаётся при сборке: -ldflags "-X main.version=…".
var version = "dev"

func main() {
	os.Exit(run(os.Args[1:], os.Stderr))
}

func run(args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("edgectx", flag.ContinueOnError)
	fs.SetOutput(stderr)
	cfgPath := fs.String("config", "", "файл конфигурации YAML (без него — умолчания)")
	tracePath := fs.String("trace", "", "файл трассы JSONL (переопределяет trace из конфигурации)")
	listen := fs.String("listen", "", "адрес HTTP-канала (переопределяет listen; :0 — свободный порт)")
	runID := fs.String("run-id", "", "идентификатор прогона (переопределяет run_id)")
	checkOnly := fs.Bool("check-config", false, "проверить конфигурацию и выйти")
	showVersion := fs.Bool("version", false, "показать версию и выйти")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	logf := func(format string, a ...any) { fmt.Fprintf(stderr, "edgectx: "+format+"\n", a...) }
	if *showVersion {
		fmt.Fprintln(stderr, "edgectx", version)
		return 0
	}

	cfg := config.Default()
	if *cfgPath != "" {
		c, err := config.Load(*cfgPath)
		if err != nil {
			logf("%v", err)
			return 2
		}
		cfg = c
	}
	if *checkOnly {
		logf("config OK (hash %s)", cfg.Hash()[:16])
		return 0
	}
	if *runID != "" {
		cfg.RunID = *runID
	}
	if cfg.RunID == "" {
		cfg.RunID = newRunID()
	}
	if *listen != "" {
		cfg.Listen = *listen
	}
	tpath := cfg.Path(cfg.Trace)
	if *tracePath != "" {
		tpath = *tracePath // путь из флага — относительно текущего каталога
	}

	var clk clock.Clock
	if cfg.Clock.Mode == "virtual" {
		clk = clock.NewVirtual(cfg.EpochTime(), config.Seconds(cfg.Clock.StartT))
	} else {
		clk = clock.NewReal(cfg.EpochTime())
	}

	tw, err := trace.Open(tpath, cfg.RunID, clk, config.Seconds(cfg.TraceFlushS))
	if err != nil {
		logf("trace: %v", err)
		return 1
	}
	defer tw.Close()
	u := cfg.UserCtx
	store, err := userctx.OpenWithPolicy(cfg.Path(u.DB), u.Reset, userctx.Policy{
		Kind: cfg.Policy.History, WindowTurns: cfg.Policy.WindowTurns, MaskKeepTurns: cfg.Policy.MaskKeepTurns,
	}, userctx.StorageOf(u))
	if err != nil {
		logf("userctx: %v", err)
		return 1
	}
	defer store.Close()

	apiKey := ""
	if cfg.LLM.APIKeyEnv != "" {
		apiKey = os.Getenv(cfg.LLM.APIKeyEnv)
	}
	l := cfg.LLM
	client := llm.New(llm.Options{BaseURL: l.BaseURL, APIKey: apiKey, ConnectTimeout: config.Seconds(l.ConnectTimeoutS),
		Timeout: config.Seconds(l.TimeoutS), Retries: l.Retries, Backoff: backoff.Policy{
			Base: time.Duration(l.RetryBackoffMs) * time.Millisecond, Max: time.Duration(l.RetryBackoffMaxMs) * time.Millisecond},
		Seed: backoff.Seed(cfg.RunID), ProbePath: l.Breaker.ProbePath, MaxConns: cfg.Sched.MaxInFlight,
		UserAgent: "edgectx/" + version})

	a, err := agent.New(agent.Deps{Config: cfg, Clock: clk, Trace: tw, Store: store, LLM: client, Version: version})
	if err != nil {
		logf("%v", err)
		return 1
	}
	ctx := context.Background()
	for _, s := range cfg.MCP {
		cctx, cancel := context.WithTimeout(ctx, config.Seconds(s.TimeoutS))
		err := a.Host().Connect(cctx, cfg.Expand(s))
		cancel()
		if err != nil {
			// Прогон без одного из серверов давал бы тихо искажённые метрики.
			logf("%v", err)
			a.Close(time.Second)
			return 1
		}
	}
	if err := a.Start(ctx); err != nil {
		logf("start: %v", err)
		a.Close(time.Second)
		return 1
	}

	ln, err := net.Listen("tcp", cfg.Listen)
	if err != nil {
		logf("listen: %v", err)
		a.Close(time.Second)
		return 1
	}
	stop := make(chan struct{})
	var once sync.Once
	shutdown := func() { once.Do(func() { close(stop) }) }
	srv := channel.NewServer(cfg.Listen, channel.Handler(a, channel.Options{Shutdown: shutdown}))
	go func() {
		if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logf("http: %v", err)
			shutdown()
		}
	}()
	logf("listening on http://%s (run_id=%s, clock=%s, wake=%s, trace=%s)", ln.Addr(), cfg.RunID,
		cfg.Clock.Mode, cfg.Policy.Wake, tpath)

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	select {
	case s := <-sig:
		logf("signal %v, stopping", s)
	case <-stop:
		logf("shutdown requested, stopping")
	}
	sctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	if err := srv.Shutdown(sctx); err != nil {
		logf("http shutdown: %v", err) // запросы, не завершённые за 10 с, оборвёт выход процесса
	}
	cancel()
	a.Close(30 * time.Second)
	if err := tw.Close(); err != nil {
		logf("trace write error: %v", err)
		return 1
	}
	return 0
}

func newRunID() string {
	b := make([]byte, 3)
	rand.Read(b)
	return "edgectx-" + time.Now().Format("20060102-150405") + "-" + hex.EncodeToString(b)
}
