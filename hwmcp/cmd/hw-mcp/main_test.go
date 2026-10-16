package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/hwmcp/internal/gpio"
	"github.com/local/hwmcp/internal/testutil"
)

const e2eDeclared = `{
 "locations": {"iio:bme280@i2c-1/0x76": "kitchen", "hwmon:cpu_thermal": "board"},
 "gpio": [
  {"id": "pir_hall", "chip": "gpiochip0", "line": 17, "direction": "input", "quantity": "motion", "location": "hall"},
  {"id": "relay_fan", "chip": "gpiochip0", "line": 27, "direction": "output", "quantity": "switch", "location": "kitchen"}
 ],
 "gate": {"devices": {"relay_fan": {"lease_s": 30, "safe_state": {"state": false}}}}
}`

func TestParseFlags(t *testing.T) {
	var buf bytes.Buffer
	for _, bad := range [][]string{
		{"-catalog", "full"}, {"-transport", "sse"}, {"-clock", "fake"}, {"-sample-period", "0s"}, {"extra"},
		{"-watchdog-tick", "100ms"}, // флаг убран: таймеры спят до срока
		{"-gpio-driver", "sysfs"}, {"-gpio-uapi", "v3"}, {"-rescan-period", "-1s"}, {"-session-timeout", "-1s"},
	} {
		if _, err := parseFlags(bad, &buf); err == nil {
			t.Errorf("%v must fail", bad)
		}
	}
	c, err := parseFlags([]string{"-catalog", "explicit", "-transport", "http", "-clock", "virtual"}, &buf)
	if err != nil || c.catalog != "explicit" || c.protocol != "2025-11-25" || !c.boardHealth ||
		c.gpioDriver != "auto" || c.gpioUAPI != "auto" || c.rescanPeriod != 10*time.Second || c.sessionTimeout != time.Hour {
		t.Fatalf("%+v %v", c, err)
	}
}

// Драйвер GPIO: auto — /dev/gpiochipN только на настоящем корне; cdev с каталогом симулятора — смешанный
// режим (sysfs из каталога, линии — настоящие); files — эмуляция в <root>/dev или -gpio-dir.
func TestGPIOBackendChoice(t *testing.T) {
	for _, tc := range []struct {
		args  []string
		want  string
		edges bool
	}{
		{[]string{"-root", "/"}, "cdev auto", true},
		{[]string{"-root", "/sim"}, "files /sim/dev", false},
		{[]string{"-root", "/sim", "-gpio-driver", "cdev", "-gpio-uapi", "v1"}, "cdev v1", true},
		{[]string{"-root", "/", "-gpio-driver", "files", "-gpio-dir", "/tmp/lines"}, "files /tmp/lines", false},
		{[]string{"-root", "/", "-gpio-edges=false"}, "cdev auto", false},
	} {
		c, err := parseFlags(tc.args, io.Discard)
		if err != nil {
			t.Fatal(err)
		}
		b, edges := gpioBackend(c)
		got := ""
		switch b := b.(type) {
		case *gpio.Cdev:
			got = "cdev " + b.UAPI
		case *gpio.Files:
			got = "files " + b.Dir
		}
		if got != tc.want || edges != tc.edges {
			t.Errorf("%v: %s edges=%v, want %s edges=%v", tc.args, got, edges, tc.want, tc.edges)
		}
	}
}

// Период свойства перекрывает период устройства; неизвестные ключи и периоды,
// несовместимые со свежестью блокировки, должны отвергаться до запуска сервера.
func TestDeclaredSamplePeriods(t *testing.T) {
	for _, tc := range []struct {
		name, raw string
		wantErr   bool
	}{
		{"valid", `{"locations":{"iio:bme280@i2c-1/0x76":"kitchen"},"sample_periods":{"kitchen_bme280":5,"kitchen_bme280.temperature":2},"stuck_after_s":{"kitchen_bme280.temperature":600}}`, false},
		{"stuck shorter than period", `{"locations":{"iio:bme280@i2c-1/0x76":"kitchen"},"stuck_after_s":{"kitchen_bme280":0.5}}`, true},
		{"unknown", `{"sample_periods":{"missing_sensor":2}}`, true},
		{"stale interlock", `{"locations":{"iio:bme280@i2c-1/0x76":"kitchen"},"sample_periods":{"kitchen_bme280.temperature":5},"gate":{"max_state_age_s":5,"interlocks":[{"name":"fresh","device":"*","tool":"*","needs":["kitchen_bme280.temperature"]}]}}`, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			decl := filepath.Join(t.TempDir(), "declared.json")
			testutil.WriteFile(t, decl, tc.raw)
			c, err := parseFlags([]string{"-root", testutil.Testdata("fixture_root"), "-declared", decl,
				"-clock", "virtual", "-gpio-dir", t.TempDir()}, io.Discard)
			if err != nil {
				t.Fatal(err)
			}
			a, err := build(c, io.Discard)
			if tc.wantErr {
				if err == nil {
					a.srv.Close()
					a.hub.Close()
					t.Fatal("invalid sampling configuration accepted")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			defer a.srv.Close()
			defer a.hub.Close()
			periods := a.hub.SamplePeriods()
			for key, want := range map[string]float64{"kitchen_bme280.temperature": 2, "kitchen_bme280.humidity": 5} {
				got := periods[key].(map[string]any)
				if got["period_s"] != want || got["source"] != "declared" {
					t.Fatalf("%s: %v", key, got)
				}
			}
			if got := periods["kitchen_bme280.temperature"].(map[string]any); got["stuck_after_s"] != 600.0 {
				t.Fatalf("stuck_after_s must reach the hub: %v", got)
			}
		})
	}
}

func TestPrintModes(t *testing.T) {
	root := testutil.Testdata("fixture_root")
	decl := testutil.Testdata("declared.json")
	var out bytes.Buffer
	c, _ := parseFlags([]string{"-root", root, "-declared", decl, "-print-inventory"}, io.Discard)
	if err := run(c, &out, io.Discard); err != nil {
		t.Fatal(err)
	}
	want, _ := os.ReadFile(testutil.Testdata("golden", "inventory.json"))
	if d := testutil.Diff(testutil.MustDecode(t, want), testutil.MustDecode(t, out.Bytes())); d != "" {
		t.Fatalf("-print-inventory differs from sysfs_probe.py: %s", d)
	}
	out.Reset()
	c, _ = parseFlags([]string{"-root", root, "-declared", decl, "-catalog", "explicit", "-print-tools"}, io.Discard)
	if err := run(c, &out, io.Discard); err != nil {
		t.Fatal(err)
	}
	var tools struct {
		Tools []struct{ Name string } `json:"tools"`
	}
	if err := json.Unmarshal(out.Bytes(), &tools); err != nil || len(tools.Tools) < 10 {
		t.Fatalf("%v %s", err, out.String())
	}
	if _, err := os.Stat(filepath.Join(root, "dev")); err == nil {
		t.Fatal("print modes must not write emulated GPIO into the fixture")
	}
}

// buildBinary собирает hw-mcp для сквозных тестов (HWMCP_BIN — готовый бинарник, например в ВМ без Go).
func buildBinary(t *testing.T) string {
	t.Helper()
	if bin := os.Getenv("HWMCP_BIN"); bin != "" {
		return bin
	}
	if testing.Short() {
		t.Skip("end-to-end test builds the binary")
	}
	bin := filepath.Join(t.TempDir(), "hw-mcp")
	cmd := exec.Command("go", "build", "-o", bin, ".")
	cmd.Env = append(os.Environ(), "CGO_ENABLED=0")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}
	return bin
}

func e2eFiles(t *testing.T) (root, decl, tracePath string) {
	t.Helper()
	root = testutil.FixtureCopy(t)
	decl = filepath.Join(t.TempDir(), "declared.json")
	testutil.WriteFile(t, decl, e2eDeclared)
	return root, decl, filepath.Join(t.TempDir(), "trace.jsonl")
}

func traceTypes(t *testing.T, path string) map[string]int {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	types := map[string]int{}
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		var rec map[string]any
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatalf("bad trace line %q", line)
		}
		types[rec["type"].(string)]++
	}
	return types
}

func postClock(t *testing.T, base string, to float64) {
	t.Helper()
	resp, err := http.Post(base+"/control/clock", "application/json", strings.NewReader(`{"t": `+jsonNum(to)+`}`))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("clock: %d %s", resp.StatusCode, b)
	}
}

func jsonNum(f float64) string { b, _ := json.Marshal(f); return string(b) }

func textOf(t *testing.T, res *mcp.CallToolResult) map[string]any {
	t.Helper()
	var body map[string]any
	if err := json.Unmarshal([]byte(res.Content[0].(*mcp.TextContent).Text), &body); err != nil {
		t.Fatal(err)
	}
	return body
}

// stdio: как edgectx запускает hw-mcp дочерним процессом; виртуальные часы — через -control-addr.
func TestStdioEndToEnd(t *testing.T) {
	bin := buildBinary(t)
	root, decl, tracePath := e2eFiles(t)
	cmd := exec.Command(bin, "-root", root, "-declared", decl, "-clock", "virtual", "-clock-start", "0",
		"-control-addr", "127.0.0.1:0", "-trace", tracePath, "-run-id", "e2e-stdio")
	stderr, err := cmd.StderrPipe()
	if err != nil {
		t.Fatal(err)
	}
	updates := make(chan string, 8)
	client := mcp.NewClient(&mcp.Implementation{Name: "edgectx-test", Version: "1"}, &mcp.ClientOptions{
		ResourceUpdatedHandler: func(_ context.Context, r *mcp.ResourceUpdatedNotificationRequest) {
			updates <- r.Params.URI
		}})
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cs, err := client.Connect(ctx, &mcp.CommandTransport{Command: cmd}, nil)
	if err != nil {
		t.Fatal(err)
	}
	control := ""
	sc := bufio.NewScanner(stderr)
	for sc.Scan() {
		if line := sc.Text(); strings.HasPrefix(line, "hw-mcp: control on ") {
			control = strings.TrimPrefix(line, "hw-mcp: control on ")
			break
		}
	}
	go func() { _, _ = io.Copy(io.Discard, stderr) }()
	if control == "" {
		t.Fatal("no control address in stderr")
	}

	res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: "read_property",
		Arguments: map[string]any{"device": "kitchen_bme280", "property": "temperature"}})
	if err != nil || textOf(t, res)["value"] != 22.51 {
		t.Fatalf("%v %v", res, err)
	}
	if err := cs.Subscribe(ctx, &mcp.SubscribeParams{URI: "hw://state/kitchen_bme280"}); err != nil {
		t.Fatal(err)
	}
	testutil.WriteFile(t, filepath.Join(root, "sys/bus/iio/devices/iio:device0/in_temp_input"), "30000\n")
	postClock(t, control, 5)
	select {
	case u := <-updates:
		if u != "hw://state/kitchen_bme280" {
			t.Fatalf("update %s", u)
		}
	case <-ctx.Done():
		t.Fatal("no resources/updated over stdio")
	}
	res, _ = cs.CallTool(ctx, &mcp.CallToolParams{Name: "write_property",
		Arguments: map[string]any{"device": "relay_fan", "property": "state", "value": true}})
	if body := textOf(t, res); body["lease_expires_in_s"] != 30.0 {
		t.Fatalf("%v", body)
	}
	postClock(t, control, 40) // аренда истекла на t = 35 -> безопасное состояние
	lvl, _ := os.ReadFile(filepath.Join(root, "dev/gpiochip0/line27"))
	if strings.TrimSpace(string(lvl)) != "0" {
		t.Fatal("lease watchdog must switch the relay off")
	}
	if err := cs.Close(); err != nil {
		t.Logf("close: %v", err)
	}
	_ = cmd.Wait()
	types := traceTypes(t, tracePath)
	for _, typ := range []string{"run_start", "output_init", "notify", "gate", "lease_expired", "run_end"} {
		if types[typ] == 0 {
			t.Errorf("trace has no %s: %v", typ, types)
		}
	}
}

// streamable HTTP + /control/clock на том же порту; SIGTERM -> безопасное состояние и run_end.
func TestHTTPEndToEnd(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("signals")
	}
	bin := buildBinary(t)
	root, decl, tracePath := e2eFiles(t)
	ready := filepath.Join(t.TempDir(), "url")
	cmd := exec.Command(bin, "-root", root, "-declared", decl, "-catalog", "explicit", "-transport", "http",
		"-clock", "virtual", "-ready-file", ready, "-trace", tracePath)
	cmd.Stderr = io.Discard
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = cmd.Process.Kill() }()
	var url string
	for i := 0; i < 100 && url == ""; i++ {
		if b, err := os.ReadFile(ready); err == nil && len(b) > 0 {
			url = strings.TrimSpace(string(b))
		} else {
			time.Sleep(50 * time.Millisecond)
		}
	}
	if url == "" {
		t.Fatal("server did not start")
	}
	base := strings.TrimSuffix(url, "/mcp")
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "http", Version: "1"}, nil).
		Connect(ctx, &mcp.StreamableClientTransport{Endpoint: url}, nil)
	if err != nil {
		t.Fatal(err)
	}
	res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: "set_relay_fan_state", Arguments: map[string]any{"value": true}})
	if err != nil || res.IsError {
		t.Fatalf("%v %v", res, err)
	}
	postClock(t, base, 10)
	resp, err := http.Post(base+"/control/clock", "application/json", strings.NewReader(`{"t": 5}`))
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("clock going back must be refused, got %d", resp.StatusCode)
	}
	resp.Body.Close()
	_ = cs.Close()
	if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		t.Fatal("hw-mcp did not stop on SIGTERM")
	}
	lvl, _ := os.ReadFile(filepath.Join(root, "dev/gpiochip0/line27"))
	if strings.TrimSpace(string(lvl)) != "0" {
		t.Fatal("shutdown must apply safe_state")
	}
	types := traceTypes(t, tracePath)
	if types["run_end"] != 1 || types["shutdown_safe_state"] != 1 {
		t.Fatalf("trace: %v", types)
	}
}

// Пример конфигурации из configs/ собирается и даёт ожидаемый каталог.
func TestLabConfigBuilds(t *testing.T) {
	var out bytes.Buffer
	c, err := parseFlags([]string{"-root", testutil.Testdata("fixture_root"),
		"-declared", filepath.Join(testutil.ModuleRoot(), "configs", "declared.lab.json"), "-print-text"}, io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	if err := run(c, &out, io.Discard); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "relay_fan @kitchen: state [/, rw]; actions: pulse(seconds:number)") {
		t.Fatalf("%s", out.String())
	}
}

// Реальные часы: сторожевой таймер (спит до срока аренды) сам переводит привод в безопасное состояние
// без обращений клиента.
func TestRealClockLeaseWatchdog(t *testing.T) {
	bin := buildBinary(t)
	root, _, tracePath := e2eFiles(t)
	decl := filepath.Join(t.TempDir(), "declared.json")
	testutil.WriteFile(t, decl, strings.Replace(e2eDeclared, `"lease_s": 30`, `"lease_s": 0.3`, 1))
	ready := filepath.Join(t.TempDir(), "url")
	cmd := exec.Command(bin, "-root", root, "-declared", decl, "-transport", "http", "-ready-file", ready,
		"-sample-period", "100ms", "-trace", tracePath)
	cmd.Stderr = io.Discard
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = cmd.Process.Kill(); _ = cmd.Wait() }()
	var url string
	for i := 0; i < 100 && url == ""; i++ {
		if b, err := os.ReadFile(ready); err == nil && len(b) > 0 {
			url = strings.TrimSpace(string(b))
		} else {
			time.Sleep(50 * time.Millisecond)
		}
	}
	if url == "" {
		t.Fatal("server did not start")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "rt", Version: "1"}, nil).
		Connect(ctx, &mcp.StreamableClientTransport{Endpoint: url}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()
	res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: "write_property",
		Arguments: map[string]any{"device": "relay_fan", "property": "state", "value": true}})
	if err != nil || res.IsError {
		t.Fatalf("%v %v", res, err)
	}
	level := func() string {
		b, _ := os.ReadFile(filepath.Join(root, "dev/gpiochip0/line27"))
		return strings.TrimSpace(string(b))
	}
	if level() != "1" {
		t.Fatal("relay must be on")
	}
	deadline := time.Now().Add(5 * time.Second)
	for level() != "0" {
		if time.Now().After(deadline) {
			t.Fatal("lease watchdog did not fire in real time")
		}
		time.Sleep(20 * time.Millisecond)
	}
	res, _ = cs.CallTool(ctx, &mcp.CallToolParams{Name: "read_property",
		Arguments: map[string]any{"device": "relay_fan", "property": "state"}})
	if body := textOf(t, res); body["value"] != false {
		t.Fatalf("state after lease expiry: %v", body)
	}
}

// Клиент stdio перестал читать stdout (edgectx завис или остановлен): уведомления копятся в pipe,
// но опрос и сторожевой таймер аренды не останавливаются, а SIGTERM переводит приводы в safe_state
// и завершает процесс (регрессия ревью: раньше рассылка шла синхронно из сэмплера и таймеров).
func TestStdioStalledClient(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("signals")
	}
	bin := buildBinary(t)
	root, _, tracePath := e2eFiles(t)
	decl := filepath.Join(t.TempDir(), "declared.json")
	testutil.WriteFile(t, decl, strings.Replace(strings.Replace(e2eDeclared,
		`{"id": "relay_fan",`, `{"id": "relay_heater", "chip": "gpiochip0", "line": 28, "direction": "output", "quantity": "switch"},
  {"id": "relay_fan",`, 1),
		`"gate": {"devices": {"relay_fan": {"lease_s": 30, "safe_state": {"state": false}}}}`,
		`"gate": {"devices": {"relay_fan": {"lease_s": 30, "safe_state": {"state": false}},
		  "relay_heater": {"lease_s": 600, "safe_state": {"state": false}}}}`, 1))
	pir := filepath.Join(root, "dev/gpiochip0/line17")
	testutil.WriteFile(t, pir, "0\n")
	cmd := exec.Command(bin, "-root", root, "-declared", decl, "-clock", "virtual", "-clock-start", "0",
		"-sample-period", "10ms", "-control-addr", "127.0.0.1:0", "-trace", tracePath)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = cmd.Process.Kill() }()
	control := ""
	sc := bufio.NewScanner(stderr)
	for sc.Scan() {
		if line := sc.Text(); strings.HasPrefix(line, "hw-mcp: control on ") {
			control = strings.TrimPrefix(line, "hw-mcp: control on ")
			break
		}
	}
	go func() { _, _ = io.Copy(io.Discard, stderr) }()
	if control == "" {
		t.Fatal("no control address in stderr")
	}

	out := bufio.NewReader(stdout)
	send := func(msg string) {
		t.Helper()
		if _, err := io.WriteString(stdin, msg+"\n"); err != nil {
			t.Fatal(err)
		}
	}
	reply := func(id int) map[string]any {
		t.Helper()
		for {
			line, err := out.ReadBytes('\n')
			if err != nil {
				t.Fatalf("reading reply %d: %v", id, err)
			}
			var m map[string]any
			if json.Unmarshal(line, &m) == nil && m["id"] == float64(id) {
				return m
			}
		}
	}
	send(`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"stall","version":"1"}}}`)
	reply(1)
	send(`{"jsonrpc":"2.0","method":"notifications/initialized"}`)
	send(`{"jsonrpc":"2.0","id":2,"method":"resources/subscribe","params":{"uri":"hw://state/pir_hall"}}`)
	reply(2)
	for i, dev := range []string{"relay_fan", "relay_heater"} {
		send(`{"jsonrpc":"2.0","id":` + jsonNum(float64(3+i)) + `,"method":"tools/call","params":{"name":"write_property",` +
			`"arguments":{"device":"` + dev + `","property":"state","value":true}}}`)
		if r := reply(3 + i); r["result"].(map[string]any)["isError"] == true {
			t.Fatalf("%s: %v", dev, r)
		}
	}

	// клиент больше не читает stdout; PIR меняется на каждом опросе — уведомлений больше, чем вмещает pipe
	level := func(line int) string {
		b, _ := os.ReadFile(filepath.Join(root, "dev/gpiochip0", "line"+jsonNum(float64(line))))
		return strings.TrimSpace(string(b))
	}
	client := &http.Client{Timeout: 5 * time.Second} // опрос, зависший на рассылке, дал бы таймаут
	post := func(to float64) {
		t.Helper()
		resp, err := client.Post(control+"/control/clock", "application/json", strings.NewReader(`{"t": `+jsonNum(to)+`}`))
		if err != nil {
			t.Fatalf("clock %g: %v (sampler blocked on a stalled client?)", to, err)
		}
		resp.Body.Close()
		if resp.StatusCode != 200 {
			t.Fatalf("clock %g: %d", to, resp.StatusCode)
		}
	}
	for i := 1; i <= 1500; i++ {
		testutil.WriteFile(t, pir, map[bool]string{true: "1\n", false: "0\n"}[i%2 == 1])
		post(float64(i) * 0.01)
	}
	post(40) // аренда relay_fan истекла на t = 30 — сторожевой таймер работает, хотя клиент не читает
	if level(27) != "0" {
		t.Fatal("lease watchdog must switch relay_fan off while the client is stalled")
	}
	if level(28) != "1" {
		t.Fatal("relay_heater lease (600 s) must still hold")
	}

	start := time.Now()
	if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case <-done:
	case <-time.After(15 * time.Second):
		t.Fatal("hw-mcp did not exit on SIGTERM with a stalled stdio client")
	}
	t.Logf("exit %v after SIGTERM", time.Since(start).Round(time.Millisecond))
	if level(28) != "0" {
		t.Fatal("SIGTERM must apply safe_state")
	}
	b, err := os.ReadFile(tracePath)
	if err != nil {
		t.Fatal(err)
	}
	var runEnd map[string]any
	safe := 0
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		var rec map[string]any
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatalf("bad trace line %q", line)
		}
		switch rec["type"] {
		case "run_end":
			runEnd = rec
		case "shutdown_safe_state":
			safe++
		}
	}
	if runEnd == nil || safe != 2 {
		t.Fatalf("trace: run_end %v, shutdown_safe_state %d", runEnd, safe)
	}
	nd, _ := runEnd["notify_dispatch"].(map[string]any)
	if c, _ := nd["coalesced"].(float64); c == 0 {
		t.Fatalf("the stdout pipe never filled (nothing coalesced), the test proves nothing: %v", nd)
	}
}
