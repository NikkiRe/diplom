package robot_test

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/robotmcp/configs"
	"github.com/local/robotmcp/internal/clock"
	"github.com/local/robotmcp/internal/fakebridge"
	"github.com/local/robotmcp/internal/robot"
	"github.com/local/robotmcp/internal/rosbridge"
	"github.com/local/robotmcp/internal/tokens"
)

// TestParityWithPrototype — сводки status/look Go-адаптера совпадают со сводками Python-прототипа
// scripts/sim/robot_mcp_compact.py на одном и том же фейковом rosbridge (поле в поле: free_m, in_view;
// место и расстояние до него). Нужен .venv проекта с mcp, websockets, cbor2.
func TestParityWithPrototype(t *testing.T) {
	if testing.Short() {
		t.Skip("short")
	}
	root, _ := filepath.Abs("../../..")
	py := filepath.Join(root, ".venv/bin/python")
	proto := filepath.Join(root, "scripts/sim/robot_mcp_compact.py")
	if _, err := os.Stat(py); err != nil {
		t.Skip("no .venv:", err)
	}
	if _, err := os.Stat(proto); err != nil {
		t.Skip("no prototype:", err)
	}
	fb := fakebridge.Start(fakebridge.Options{})
	defer fb.Close()
	fb.Teleport(-1.75, -0.3, 0.7) // не на месте: проверяется и расстояние до ближайшего места
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	// Python-прототип
	cmd := exec.Command(py, proto, "--rosbridge", fb.URL)
	cmd.Env = append(os.Environ(), "PYTHONDONTWRITEBYTECODE=1")
	client := mcp.NewClient(&mcp.Implementation{Name: "parity"}, nil)
	cs, err := client.Connect(ctx, &mcp.CommandTransport{Command: cmd}, nil)
	if err != nil {
		t.Skip("prototype did not start:", err)
	}
	defer cs.Close()
	text := func(name string, args map[string]any) map[string]any {
		res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: name, Arguments: args})
		if err != nil {
			t.Fatal(name, err)
		}
		var m map[string]any
		if err := json.Unmarshal([]byte(res.Content[0].(*mcp.TextContent).Text), &m); err != nil {
			t.Fatal(err)
		}
		return m
	}
	pStatus := text("status", nil)
	pLook := text("look", map[string]any{})

	// Go-ядро на том же стенде
	cfg, _ := robot.ParseConfig(configs.TurtleBot3World)
	br := rosbridge.New(rosbridge.Options{URL: fb.URL})
	go br.Run(ctx)
	if err := br.WaitConnected(ctx); err != nil {
		t.Fatal(err)
	}
	r := robot.New(cfg, robot.Options{Clock: clock.NewReal(), Bridge: br, BatteryStart: 100, ScanMaxAge: 30})
	r.Start(ctx)
	time.Sleep(200 * time.Millisecond)
	gStatus := r.Status(ctx)
	gLook, _ := r.Look(ctx, "", false, robot.ByLLM)

	norm := func(v any) any { b, _ := json.Marshal(v); var x any; _ = json.Unmarshal(b, &x); return x }
	if !reflect.DeepEqual(norm(gStatus["free_m"]), pStatus["free_m"]) {
		t.Errorf("free_m: go %v, prototype %v", gStatus["free_m"], pStatus["free_m"])
	}
	if !reflect.DeepEqual(norm(gLook["in_view"]), pLook["in_view"]) {
		t.Errorf("in_view: go %v, prototype %v", gLook["in_view"], pLook["in_view"])
	}
	pp := pStatus["pose"].(map[string]any)
	if gStatus["place_m"] != pp["near_m"] {
		t.Errorf("place_m %v vs near_m %v", gStatus["place_m"], pp["near_m"])
	}
	if pp["near"] != "dock" || gStatus["place"] != "dock" {
		t.Errorf("place: go %v, prototype near %v", gStatus["place"], pp["near"])
	}
	t.Logf("prototype status %v", pStatus)
	t.Logf("go status        %v", gStatus)
	t.Logf("prototype look %v / go look %v", pLook, gLook)
	// короткая ошибка неизвестного места против pydantic-ошибки прототипа (88 токенов в smoke 07.10)
	res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: "go_to", Arguments: map[string]any{"place": "kitchen"}})
	if err == nil {
		pe := res.Content[0].(*mcp.TextContent).Text
		ge, _ := json.Marshal(r.UnknownPlace("kitchen"))
		t.Logf("unknown place: prototype %d est. tokens, go %d", tokens.Count(pe), tokens.Count(string(ge)))
		if tokens.Count(string(ge)) >= tokens.Count(pe) {
			t.Errorf("short error is not shorter: %s vs %s", ge, pe)
		}
	}
}
