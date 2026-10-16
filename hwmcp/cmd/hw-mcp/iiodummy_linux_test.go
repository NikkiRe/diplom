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

	"github.com/local/hwmcp/internal/testutil"
)

// Горячее отключение на настоящем sysfs: устройства модуля ядра iio_dummy создаются и удаляются через
// configfs (/sys/kernel/config/iio/devices/dummy/<имя>), номер iio:deviceN ядро выделяет заново.
// Устройство пропадает, и его прежний номер сразу занимает другое (с теми же значениями): hw-mcp не должен
// читать чужое устройство по старому пути — значение null, absent. Само устройство возвращается под новым
// номером, и hw-mcp находит его повторной инвентаризацией по id. Префикс имён задаёт
// tools/vm_hotplug_check.sh в HWMCP_IIO_DUMMY (нужен root).
func TestIIODummyHotplug(t *testing.T) {
	prefix := os.Getenv("HWMCP_IIO_DUMMY")
	if prefix == "" {
		t.Skip("set HWMCP_IIO_DUMMY (see tools/vm_hotplug_check.sh) to test hot-plug on iio_dummy")
	}
	bin := buildBinary(t)
	dummy := func(name string, create bool) {
		t.Helper()
		dir := filepath.Join("/sys/kernel/config/iio/devices/dummy", prefix+"-"+name)
		op := os.Mkdir
		if !create {
			op = func(p string, _ os.FileMode) error { return os.Remove(p) }
		}
		if err := op(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		if create {
			t.Cleanup(func() { _ = os.Remove(dir) })
		}
	}
	node := func(name string) string {
		t.Helper()
		ents, _ := os.ReadDir("/sys/bus/iio/devices")
		for _, e := range ents {
			if b, err := os.ReadFile(filepath.Join("/sys/bus/iio/devices", e.Name(), "name")); err == nil &&
				strings.TrimSpace(string(b)) == prefix+"-"+name {
				return e.Name()
			}
		}
		return ""
	}
	dummy("a", true)
	before := node("a")
	decl := filepath.Join(t.TempDir(), "declared.json")
	testutil.WriteFile(t, decl, fmt.Sprintf(`{"names": {"iio:%s-a@devices": "dummy_a"}}`, prefix))
	tracePath := filepath.Join(t.TempDir(), "trace.jsonl")
	cmd := exec.Command(bin, "-root", "/", "-declared", decl, "-gpio-driver", "files", "-gpio-dir", t.TempDir(),
		"-sample-period", "200ms", "-rescan-period", "1s", "-trace", tracePath)
	updates := make(chan any, 16)
	client := mcp.NewClient(&mcp.Implementation{Name: "hotplug", Version: "1"}, &mcp.ClientOptions{
		ResourceUpdatedHandler: func(_ context.Context, r *mcp.ResourceUpdatedNotificationRequest) {
			if st, ok := r.Params.Meta["state"].(map[string]any); ok {
				if v, has := st["voltage0"]; has {
					updates <- v
				}
			}
		}})
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cs, err := client.Connect(ctx, &mcp.CommandTransport{Command: cmd}, nil)
	if err != nil {
		t.Fatal(err)
	}
	read := func() map[string]any {
		t.Helper()
		res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: "read_property",
			Arguments: map[string]any{"device": "dummy_a", "property": "voltage0"}})
		if err != nil || res.IsError {
			t.Fatalf("read_property: %v %v", res, err)
		}
		return textOf(t, res)
	}
	await := func(what string, ok func(any) bool) {
		t.Helper()
		for {
			select {
			case v := <-updates:
				if ok(v) {
					return
				}
			case <-time.After(5 * time.Second):
				t.Fatalf("no notification: %s", what)
			}
		}
	}
	if err := cs.Subscribe(ctx, &mcp.SubscribeParams{URI: "hw://state/dummy_a"}); err != nil {
		t.Fatal(err)
	}
	first := read()
	if first["value"] == nil {
		t.Fatalf("iio_dummy voltage0: %v", first)
	}

	dummy("a", false)
	dummy("b", true)
	if got := node("b"); got != before {
		t.Fatalf("%s-b got %s, not the freed %s: reading a foreign device under the old number is not tested",
			prefix, got, before)
	}
	await("device removed", func(v any) bool { return v == nil })
	if body := read(); body["value"] != nil || body["error"] != "absent" {
		t.Fatalf("removed device: %v", body)
	}
	dummy("a", true)
	t.Logf("dummy_a: %s -> %s, its old number taken by %s-b", before, node("a"), prefix)
	await("device back", func(v any) bool { return v == first["value"] })
	_ = cs.Close()
	_ = cmd.Wait()

	b, err := os.ReadFile(tracePath)
	if err != nil {
		t.Fatal(err)
	}
	types := map[string]int{}
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		var rec map[string]any
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatal(err)
		}
		if rec["device"] == "dummy_a" && rec["property"] == "voltage0" {
			types[rec["type"].(string)]++
			t.Logf("%s", line)
		}
	}
	if types["sample_error"] != 1 || types["sample_recovered"] != 1 || types["sensor_rebound"] != 1 {
		t.Fatalf("trace records: %v", types)
	}
}
