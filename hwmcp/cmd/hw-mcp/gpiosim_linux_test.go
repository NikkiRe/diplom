//go:build linux

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/hwmcp/internal/gpio"
	"github.com/local/hwmcp/internal/testutil"
)

// Сквозная проверка смешанного режима на модуле ядра gpio-sim: sysfs — копия фикстуры, линии — настоящий
// чип, найденный по метке, через uAPI v1 и v2. Чип создаёт tools/vm_gpiosim_check.sh и передаёт метку в
// HWMCP_GPIOSIM_LABEL (нужен root). Вход — линия 6, выход — линия 7.
func TestGpioSimEndToEnd(t *testing.T) {
	label := os.Getenv("HWMCP_GPIOSIM_LABEL")
	if label == "" {
		t.Skip("set HWMCP_GPIOSIM_LABEL (see tools/vm_gpiosim_check.sh) to run hw-mcp on gpio-sim")
	}
	bin := buildBinary(t)
	info, _, err := (&gpio.Cdev{}).Info(label)
	if err != nil {
		t.Fatal(err)
	}
	sim := filepath.Join("/sys/bus/gpio/devices", info.Name)
	pull := func(line int, up bool) {
		t.Helper()
		v := map[bool]string{true: "pull-up", false: "pull-down"}[up]
		testutil.WriteFile(t, filepath.Join(sim, fmt.Sprintf("sim_gpio%d", line), "pull"), v)
	}
	level := func(line int) string {
		b, _ := os.ReadFile(filepath.Join(sim, fmt.Sprintf("sim_gpio%d", line), "value"))
		return strings.TrimSpace(string(b))
	}
	for _, uapi := range []string{gpio.UAPIv1, gpio.UAPIv2} {
		t.Run(uapi, func(t *testing.T) {
			decl := filepath.Join(t.TempDir(), "declared.json")
			testutil.WriteFile(t, decl, fmt.Sprintf(`{"gpio": [
  {"id": "sim_button", "chip": %q, "line": 6, "direction": "input", "quantity": "button"},
  {"id": "sim_relay", "chip": %q, "line": 7, "direction": "output", "quantity": "switch"}],
 "gate": {"devices": {"sim_relay": {"safe_state": {"state": false}}}}}`, label, label))
			tracePath := filepath.Join(t.TempDir(), "trace.jsonl")
			pull(6, false)
			// период опроса 60 с: изменение входа в пределах секунды приходит только событием фронта
			cmd := exec.Command(bin, "-root", testutil.FixtureCopy(t), "-declared", decl, "-gpio-driver", "cdev",
				"-gpio-uapi", uapi, "-sample-period", "60s", "-trace", tracePath)
			states := make(chan any, 8)
			client := mcp.NewClient(&mcp.Implementation{Name: "gpiosim", Version: "1"}, &mcp.ClientOptions{
				ResourceUpdatedHandler: func(_ context.Context, r *mcp.ResourceUpdatedNotificationRequest) {
					states <- r.Params.Meta["state"].(map[string]any)["state"]
				}})
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			cs, err := client.Connect(ctx, &mcp.CommandTransport{Command: cmd}, nil)
			if err != nil {
				t.Fatal(err)
			}
			call := func(name string, args map[string]any) map[string]any {
				t.Helper()
				res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: name, Arguments: args})
				if err != nil || res.IsError {
					t.Fatalf("%s: %v %v", name, res, err)
				}
				return textOf(t, res)
			}
			if err := cs.Subscribe(ctx, &mcp.SubscribeParams{URI: "hw://state/sim_button"}); err != nil {
				t.Fatal(err)
			}
			if v := call("read_property", map[string]any{"device": "sim_button", "property": "state"}); v["value"] != false {
				t.Fatalf("input: %v", v)
			}
			for _, up := range []bool{true, false} {
				pull(6, up)
				select {
				case got := <-states:
					if got != up {
						t.Fatalf("edge notification %v, want %v", got, up)
					}
				case <-time.After(2 * time.Second):
					t.Fatalf("no edge notification for pull-up=%v", up)
				}
			}
			for _, on := range []bool{true, false, true} {
				call("write_property", map[string]any{"device": "sim_relay", "property": "state", "value": on})
				if want := map[bool]string{true: "1", false: "0"}[on]; level(7) != want {
					t.Fatalf("relay %v: line level %s", on, level(7))
				}
			}
			_ = cs.Close()
			_ = cmd.Wait()
			if level(7) != "0" {
				t.Fatal("exit must leave the relay in its safe state")
			}
			b, err := os.ReadFile(tracePath)
			if err != nil {
				t.Fatal(err)
			}
			for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
				var rec map[string]any
				if err := json.Unmarshal([]byte(line), &rec); err != nil || rec["type"] != "ready" {
					continue
				}
				chip, _ := rec["gpio_chips"].(map[string]any)[label].(map[string]any)
				if chip["node"] != info.Name || chip["uapi"] != uapi || rec["gpio_edges"] != true {
					t.Fatalf("ready: %v", rec)
				}
				t.Logf("ready: gpio_backend=%v gpio_chips=%v", rec["gpio_backend"], rec["gpio_chips"])
				return
			}
			t.Fatal("no ready record")
		})
	}
}
