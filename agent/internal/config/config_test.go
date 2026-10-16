package config

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestExampleConfigLoads(t *testing.T) {
	c, err := Load(filepath.Join("..", "..", "configs", "example.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if c.Clock.Mode != "virtual" || c.Clock.StartT != 25200 {
		t.Fatalf("clock %+v", c.Clock)
	}
	if c.Policy.Wake != "hybrid" || c.Policy.HWCtx != "delta_tail" || c.Policy.Catalog != "generic" {
		t.Fatalf("policy %+v", c.Policy)
	}
	if len(c.Policy.Gate.Signals) != 4 || c.Policy.Gate.Signals[1].Above == nil || *c.Policy.Gate.Signals[1].Above != 30 {
		t.Fatalf("signals %+v", c.Policy.Gate.Signals)
	}
	if c.LLM.Extra["provider"] == nil {
		t.Fatal("llm.extra lost")
	}
	if !filepath.IsAbs(c.Path(c.Trace)) {
		t.Fatalf("trace path not resolved: %s", c.Path(c.Trace))
	}
	if c.LLM.Breaker.ProbePath != "models" || c.UserCtx.JournalMode != "wal" || c.Sched.DeferredMax != 32 {
		t.Fatalf("new keys %+v %+v %+v", c.LLM.Breaker, c.UserCtx, c.Sched)
	}
}

// Закомментированный блок hw-mcp примера — рабочая конфигурация: раскомментированный, он
// разбирается, а команда указывает на бинарник, который собирает hwmcp (make build → .build/).
func TestExampleHWBlock(t *testing.T) {
	path := filepath.Join("..", "..", "configs", "example.yaml")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(string(data), "\n")
	in := false
	for i, l := range lines {
		switch {
		case strings.HasPrefix(l, "  # - name: hw"):
			in = true
		case in && !strings.HasPrefix(l, "  #   "):
			in = false
		}
		if in {
			lines[i] = "  " + strings.TrimPrefix(l, "  # ")
		}
	}
	dir := filepath.Dir(path)
	c, err := Parse([]byte(strings.Join(lines, "\n")))
	if err != nil {
		t.Fatal(err)
	}
	abs, _ := filepath.Abs(dir)
	c.BaseDir = abs
	if len(c.MCP) != 2 || c.MCP[1].Name != "hw" {
		t.Fatalf("hw block not found: %+v", c.MCP)
	}
	hw := c.Expand(c.MCP[1])
	root := filepath.Join(abs, "..", "..")
	if hw.Cmd[0] != filepath.Join(root, "hwmcp", ".build", "hw-mcp") {
		t.Fatalf("hw-mcp binary path %s", hw.Cmd[0])
	}
	for _, p := range []string{filepath.Join(root, "hwmcp", "Makefile"), filepath.Join(abs, hw.Cmd[4])} {
		if _, err := os.Stat(p); err != nil {
			t.Errorf("%v", err)
		}
	}
}

func TestDefaultsAndShortClock(t *testing.T) {
	c, err := Parse([]byte("clock: virtual\nllm: {model: m}\n"))
	if err != nil {
		t.Fatal(err)
	}
	if c.Clock.Mode != "virtual" || c.LLM.Model != "m" || c.LLM.BaseURL == "" {
		t.Fatalf("%+v", c)
	}
	if c.Policy.Wake != "hybrid" || c.Policy.HeartbeatMin != 30 || c.Sched.MaxInFlight != 1 {
		t.Fatalf("defaults %+v", c.Policy)
	}
	if !reflect.DeepEqual(c.Policy.LocalTools, DefaultLocalTools) {
		t.Fatalf("local tools %v", c.Policy.LocalTools)
	}
	if !c.RulesEnabled() || !c.HistoryBackground() {
		t.Fatal("bool defaults")
	}
	if c.EpochTime().Hour() != 0 {
		t.Fatal("virtual epoch must be midnight")
	}
	l := c.LLM
	if l.ConnectTimeoutS != 10 || l.Retries != 2 || l.RetryBackoffMaxMs != 8000 || l.Breaker.Failures != 1 ||
		l.Breaker.ProbeS != 30 || l.Breaker.ProbeMaxS != 300 || c.TraceFlushS != 2 || c.Policy.Gate.CoalesceMax != 32 {
		t.Fatalf("link defaults %+v trace_flush_s=%v", l, c.TraceFlushS)
	}
	u := c.UserCtx
	if u.CacheKiB != 256 || u.JournalMode != "wal" || u.Synchronous != "normal" {
		t.Fatalf("storage defaults %+v", u)
	}
	m, _ := Parse([]byte("mcp: [{name: a, transport: http, url: x}]"))
	if s := m.MCP[0]; s.ReconnectS != 1 || s.ReconnectMaxS != 60 || s.TimeoutS != 30 {
		t.Fatalf("mcp defaults %+v", s)
	}
}

func TestEmptyConfigIsValid(t *testing.T) {
	if _, err := Parse(nil); err != nil {
		t.Fatal(err)
	}
}

func TestValidationErrors(t *testing.T) {
	cases := map[string]string{
		"clock: sundial":                                 "clock",
		"policy: {wake: sometimes}":                      "policy.wake",
		"policy: {history: window, window_turns: 0}":     "window_turns",
		"policy: {hwctx: everything}":                    "policy.hwctx",
		"policy: {gate: {signals: [{kind: threshold}]}}": "above",
		"mcp: [{name: a, transport: stdio}]":             "cmd",
		"mcp: [{name: a, transport: http, url: x}, {name: a, transport: http, url: y}]": "повторяется",
		"sched: {max_in_flight: 3}":                      "max_in_flight",
		"policy: {local_tools: [rm_rf]}":                 "local_tools",
		"unknown_key: 1":                                 "unknown_key",
		"userctx: {journal_mode: memory}":                "journal_mode",
		"userctx: {journal_mode: delete}":                "synchronous",
		"llm: {breaker: {probe_s: 60, probe_max_s: 30}}": "probe_max_s",
		"llm: {retry_backoff_ms: 9000}":                  "retry_backoff_max_ms",
		"mcp: [{name: a, transport: http, url: x, reconnect_s: 5, reconnect_max_s: 2}]": "reconnect_max_s",
	}
	for in, want := range cases {
		_, err := Parse([]byte(in))
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("%q: err=%v, want mention of %q", in, err, want)
		}
	}
}

func TestHashStableAndSensitive(t *testing.T) {
	a, _ := Parse([]byte("policy: {wake: gate}"))
	b, _ := Parse([]byte("policy:\n  wake: gate\n"))
	c, _ := Parse([]byte("policy: {wake: heartbeat}"))
	if a.Hash() != b.Hash() {
		t.Fatal("same config, different hash")
	}
	if a.Hash() == c.Hash() {
		t.Fatal("different config, same hash")
	}
}

func TestExpandAndPaths(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "c.yaml")
	os.WriteFile(p, []byte(`
policy: {catalog: explicit}
mcp:
  - {name: hw, transport: stdio, cmd: "./bin/hw-mcp --catalog {catalog} --name 'a b'"}
  - {name: sim, transport: http, url: "http://127.0.0.1:18090/mcp?catalog={catalog}"}
`), 0o644)
	c, err := Load(p)
	if err != nil {
		t.Fatal(err)
	}
	hw := c.Expand(c.MCP[0])
	if hw.Cmd[0] != filepath.Join(dir, "bin/hw-mcp") || hw.Cmd[2] != "explicit" || hw.Cmd[4] != "a b" {
		t.Fatalf("cmd %q", hw.Cmd)
	}
	if sim := c.Expand(c.MCP[1]); !strings.HasSuffix(sim.URL, "catalog=explicit") {
		t.Fatalf("url %s", sim.URL)
	}
	if c.Path(":memory:") != ":memory:" || c.Path("/abs") != "/abs" {
		t.Fatal("special paths must not be resolved")
	}
}

func TestSplitCommand(t *testing.T) {
	got, err := SplitCommand(`a "b c" 'd\e' f\ g`)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"a", "b c", `d\e`, "f g"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %q", got)
	}
	if _, err := SplitCommand(`a "b`); err == nil {
		t.Fatal("unterminated quote must fail")
	}
}
