// Интеграционный тест «мини-день»: агент на виртуальных часах, фейковый
// MCP-сервер «комната» (go-sdk, in-memory транспорт, настоящие уведомления
// resources/updated) и фейковый LLM (httptest, режим oracle). Проверяется, что
// heartbeat будит LLM каждые N минут, гейт — только на значимые события,
// правило исполняется без LLM, бюджет переводит в деградированный режим, а
// трасса содержит все типы событий и согласована.
package tests

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/edgectx/internal/agent"
	"github.com/local/edgectx/internal/clock"
	"github.com/local/edgectx/internal/config"
	"github.com/local/edgectx/internal/fakemcp"
	"github.com/local/edgectx/internal/llm"
	"github.com/local/edgectx/internal/sched"
	"github.com/local/edgectx/internal/trace"
	"github.com/local/edgectx/internal/userctx"
)

// TestMain: тестовый бинарник может работать stdio-сервером «комната» (для e2e).
func TestMain(m *testing.M) {
	if os.Getenv("EDGECTX_FAKE_MCP") == "stdio" {
		if err := fakemcp.NewRoom().Server().Run(context.Background(), &mcp.StdioTransport{}); err != nil {
			os.Exit(1)
		}
		os.Exit(0)
	}
	os.Exit(m.Run())
}

// hm — секунды дня для «чч:мм[:сс]».
func hm(s string) float64 {
	var h, m, sec int
	fmt.Sscanf(strings.ReplaceAll(s, ":", " "), "%d %d %d", &h, &m, &sec)
	return float64(h*3600 + m*60 + sec)
}

const baseConfig = `
clock: {mode: virtual, epoch: "2026-10-06T00:00:00Z", start_t: 25200, settle_timeout_s: 20}
llm: {model: fake-model, trace_headers: true, timeout_s: 10, retries: 0}
userctx: {db: ":memory:"}
policy:
  heartbeat_min: 30
  max_steps: 4
  catalog: generic
  gate:
    debounce_s: 2
    cooldown_s: 300
    coalesce_s: 10
    default: ignore
    signals:
      - {device: "door*", property: open, kind: change}
      - {property: temperature, kind: threshold, above: 30, hysteresis: 1, priority: safety}
      - {device: "pir*", property: motion, kind: episode, gap_s: 600}
`

type harness struct {
	t         *testing.T
	a         *agent.Agent
	room      *fakemcp.Room
	llm       *fakeLLM
	tracePath string
	tw        *trace.Writer
	store     *userctx.Store
	sensors   int // ожидаемое число входов sensor
	closed    bool
}

func newHarness(t *testing.T, extraYAML string, oracle func(llmCall) reply) *harness {
	t.Helper()
	cfg, err := config.Parse([]byte(baseConfig + extraYAML))
	if err != nil {
		t.Fatal(err)
	}
	h := &harness{t: t, room: fakemcp.NewRoom(), llm: newFakeLLM(t, oracle)}
	cfg.LLM.BaseURL = h.llm.URL()
	cfg.RunID = "minday-" + t.Name()
	v := clock.NewVirtual(cfg.EpochTime(), time.Duration(cfg.Clock.StartT)*time.Second)
	h.tracePath = filepath.Join(t.TempDir(), "edgectx.trace.jsonl")
	if h.tw, err = trace.Open(h.tracePath, cfg.RunID, v, time.Second); err != nil {
		t.Fatal(err)
	}
	if h.store, err = userctx.Open(":memory:", true); err != nil {
		t.Fatal(err)
	}
	client := llm.New(llm.Options{BaseURL: cfg.LLM.BaseURL, Timeout: 10 * time.Second})
	h.a, err = agent.New(agent.Deps{Config: cfg, Clock: v, Trace: h.tw, Store: h.store, LLM: client, Version: "test"})
	if err != nil {
		t.Fatal(err)
	}
	st, ct := mcp.NewInMemoryTransports()
	if _, err := h.room.Server().Connect(context.Background(), st, nil); err != nil {
		t.Fatal(err)
	}
	if err := h.a.Host().ConnectTransport(context.Background(), config.MCPServer{Name: "room", Transport: "stdio", TimeoutS: 10}, ct); err != nil {
		t.Fatal(err)
	}
	if err := h.a.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { h.close() })
	return h
}

// at продвигает часы, дождавшись всех ожидаемых входов датчиков.
func (h *harness) at(s string) {
	h.t.Helper()
	res, err := h.a.Advance(hm(s), agent.AdvanceOptions{MinInputs: map[string]int{"sensor": h.sensors}, Timeout: 5 * time.Second})
	if err != nil {
		h.t.Fatal(err)
	}
	if len(res.MissingIn) > 0 {
		h.t.Fatalf("at %s: missing inputs %v", s, res.MissingIn)
	}
}

// set меняет состояние в комнате (уведомление resources/updated → вход sensor).
func (h *harness) set(at, device, prop string, value any) {
	h.t.Helper()
	h.at(at)
	if err := h.room.Set(context.Background(), device, prop, value); err != nil {
		h.t.Fatal(err)
	}
	h.sensors++
	h.at(at) // дождаться приёма уведомления на том же моменте времени
}

// user — реплика пользователя; extraSensors — сколько изменений состояния она вызовет.
func (h *harness) user(at, id, text string, extraSensors int) agent.Reply {
	h.t.Helper()
	h.at(at)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	r, err := h.a.User(ctx, id, text)
	if err != nil {
		h.t.Fatal(err)
	}
	h.sensors += extraSensors
	h.at(at)
	return r
}

func (h *harness) close() []rec {
	if !h.closed {
		h.closed = true
		h.a.Close(10 * time.Second)
		h.tw.Close()
		h.store.Close()
	}
	return readTrace(h.t, h.tracePath)
}

// wakeTimes — ts_clock допущенных (suppressed=false) пробуждений данной причины.
func wakeTimes(recs []rec, reason string, suppressed bool) []float64 {
	var out []float64
	for _, r := range byType(recs, trace.Wake) {
		if r.s("reason") == reason && r.b("suppressed") == suppressed {
			out = append(out, r.f("ts_clock"))
		}
	}
	return out
}

func suppressReasons(recs []rec) map[string]string {
	out := map[string]string{}
	for _, r := range byType(recs, trace.Suppress) {
		out[r.s("input_id")] = r.s("reason")
	}
	return out
}

func inputByDevice(recs []rec, device, prop string) []rec {
	var out []rec
	for _, r := range byType(recs, trace.Input) {
		if r.s("device") == device && r.s("property") == prop {
			out = append(out, r)
		}
	}
	return out
}

func eqFloats(a, b []float64) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// ----------------------------------------------------------- heartbeat

func TestMiniDayHeartbeat(t *testing.T) {
	h := newHarness(t, "  wake: heartbeat\n  hwctx: snapshot_head\n  history: full\n", roomOracle)
	h.set("07:05", "door1", "open", true)
	h.set("07:12", "t1", "temperature", 31.0)
	rep := h.user("07:45", "u1", "включи свет", 1)
	if rep.Text != "Свет включён." || !rep.OK || rep.LLMCalls != 2 || rep.ToolCalls != 1 {
		t.Fatalf("reply %+v", rep)
	}
	h.set("08:10", "door1", "open", false)
	h.at("09:00")
	recs := h.close()
	counts := checkTrace(t, recs)

	want := []float64{hm("07:30"), hm("08:00"), hm("08:30"), hm("09:00")}
	if got := wakeTimes(recs, "heartbeat", false); !eqFloats(got, want) {
		t.Fatalf("heartbeat wakes at %v, want %v", got, want)
	}
	if got := wakeTimes(recs, "gate", false); len(got) != 0 {
		t.Fatalf("heartbeat policy must not wake on events: %v", got)
	}
	if counts[trace.LLMRequest] != 4+2 {
		t.Fatalf("llm requests %d, want 6 (4 heartbeat + 2 user)", counts[trace.LLMRequest])
	}
	sup := suppressReasons(recs)
	for _, in := range byType(recs, trace.Input) {
		want := "policy_heartbeat"
		if in.s("device") == "relay1" {
			want = "self_caused" // эхо команды пользователя «включи свет»
		}
		if in.s("kind") == "sensor" && sup[in.s("id")] != want {
			t.Errorf("sensor input %s: suppress %q, want %s", in.s("id"), sup[in.s("id")], want)
		}
	}
	// snapshot_head: снимок L1 — в системном сообщении (префиксе), и он меняется.
	calls := h.llm.Calls()
	first, lastHB := calls[0].system(), calls[len(calls)-1].system()
	if !strings.Contains(first, "Состояние устройств") || !strings.Contains(first, "door1: open=true") {
		t.Fatalf("snapshot not in prefix:\n%s", first)
	}
	if !strings.Contains(lastHB, "door1: open=false") {
		t.Fatalf("snapshot not refreshed:\n%s", lastHB)
	}
	if strings.Contains(calls[0].state(), "Изменения состояния") {
		t.Fatal("snapshot_head must not put delta into the tail")
	}
	if h.room.Get("relay1", "on") != true {
		t.Fatal("relay must be on after the user command")
	}
}

// ---------------------------------------------------------------- gate

func TestMiniDayGate(t *testing.T) {
	h := newHarness(t, "  wake: gate\n  hwctx: delta_tail\n  history: mask\n  window_turns: 6\n  mask_keep_turns: 1\n", roomOracle)
	h.set("07:05", "t1", "temperature", 22.5) // ниже порога
	h.set("07:10", "door1", "open", true)     // значимо: LLM создаёт правило
	h.set("07:14", "door1", "open", false)    // cooldown
	h.at("07:39")
	nBefore := len(h.llm.Calls())
	h.set("07:40", "door1", "open", true) // принадлежит правилу: без LLM
	h.at("07:49")
	if n := len(h.llm.Calls()); n != nBefore {
		t.Fatalf("rule event must not call the LLM: %d -> %d", nBefore, n)
	}
	h.set("07:50", "door1", "open", false) // условие правила ложно → гейт
	h.set("08:00", "pir1", "motion", 1.0)
	h.set("08:01", "pir1", "motion", 0.0)
	h.set("08:02", "pir1", "motion", 1.0) // конец эпизода в 08:12
	h.set("08:20", "t1", "temperature", 31.0)
	rep := h.user("08:30", "u1", "включи свет", 1)
	if rep.Text != "Свет включён." {
		t.Fatalf("reply %+v", rep)
	}
	rep = h.user("08:40", "u2", "что с дверью?", 0)
	if rep.Text != "Дверь закрыта." || rep.LLMCalls != 1 {
		t.Fatalf("reply %+v", rep)
	}
	h.at("09:00")
	recs := h.close()
	counts := checkTrace(t, recs)

	// Все типы событий §6 присутствуют.
	for _, typ := range []string{trace.RunStart, trace.Input, trace.Wake, trace.Suppress, trace.LLMRequest,
		trace.LLMResponse, trace.ToolCall, trace.ToolResult, trace.UserReply, trace.Notify, trace.RuleFired, trace.RunEnd} {
		if counts[typ] == 0 {
			t.Errorf("trace has no %s records", typ)
		}
	}
	if counts[trace.Error] != 0 {
		t.Errorf("unexpected error records: %v", byType(recs, trace.Error))
	}
	gateWakes := wakeTimes(recs, "gate", false)
	want := []float64{hm("07:10:12"), hm("07:50:12"), hm("08:12:10"), hm("08:20:12")}
	if !eqFloats(gateWakes, want) {
		t.Fatalf("gate wakes at %v, want %v", gateWakes, want)
	}
	if hb := wakeTimes(recs, "heartbeat", false); len(hb) != 0 {
		t.Fatalf("gate policy must not heartbeat: %v", hb)
	}
	if counts[trace.LLMRequest] != 9 {
		t.Fatalf("llm requests %d, want 9", counts[trace.LLMRequest])
	}
	// Исходы входов датчиков.
	sup := suppressReasons(recs)
	temps := inputByDevice(recs, "t1", "temperature")
	doors := inputByDevice(recs, "door1", "open")
	if sup[temps[0].s("id")] != "below_threshold" || sup[doors[1].s("id")] != "cooldown" ||
		sup[doors[2].s("id")] != "rule_handled" || sup[temps[1].s("id")] != "" {
		t.Fatalf("outcomes: temps=%v doors=%v sup=%v", temps, doors, sup)
	}
	relay := inputByDevice(recs, "relay1", "on")
	if len(relay) != 1 || sup[relay[0].s("id")] != "self_caused" || relay[0]["self"] != true {
		t.Fatalf("relay change after the agent's own write must be self_caused: %v", relay)
	}
	// Правило: создано LLM, сработало без LLM в 07:45:02 с уведомлением.
	fired := byType(recs, trace.RuleFired)
	if len(fired) != 1 || fired[0].f("ts_clock") != hm("07:45:02") || fired[0].s("input_id") != doors[2].s("id") {
		t.Fatalf("rule_fired %v", fired)
	}
	var ruleNotify, llmNotify, toolNotify int
	for _, n := range byType(recs, trace.Notify) {
		switch n.s("source") {
		case "rule":
			ruleNotify++
		case "llm_reply":
			llmNotify++
		case "llm_tool":
			toolNotify++
		}
	}
	if ruleNotify != 1 || llmNotify != 1 || toolNotify != 1 {
		t.Fatalf("notify sources rule=%d llm_reply=%d llm_tool=%d", ruleNotify, llmNotify, toolNotify)
	}
	// Эпизод: производный вход с числом срабатываний.
	var ep rec
	for _, in := range byType(recs, trace.Input) {
		if in.s("derived") == "episode_end" {
			ep = in
		}
	}
	if ep == nil || ep["extra"].(map[string]any)["events"].(float64) != 3 {
		t.Fatalf("episode input %v", ep)
	}
	// Температурное пробуждение — приоритет безопасности.
	for _, w := range byType(recs, trace.Wake) {
		if w.f("ts_clock") == hm("08:20:12") && w.s("priority") != "safety" {
			t.Fatalf("temperature wake priority %v", w)
		}
	}
	// delta_tail: первый запрос несёт всё состояние, последующие — только изменения.
	calls := h.llm.Calls()
	if !strings.Contains(calls[0].state(), "Изменения состояния") || strings.Contains(calls[0].system(), "Состояние устройств") {
		t.Fatalf("delta must be in the tail:\n%s", calls[0].state())
	}
	if !strings.Contains(calls[0].system(), "door1 (hall): open [bool]") {
		t.Fatalf("L0 inventory missing in prefix:\n%s", calls[0].system())
	}
	// Стабильный префикс: системное сообщение одинаково во всех ходах.
	for _, c := range calls {
		if c.Purpose == "turn" && c.system() != calls[0].system() {
			t.Fatal("prefix changed between requests")
		}
	}
	// Каталог: инструменты MCP + встроенные, по алфавиту.
	if got := strings.Join(calls[0].Tools, ","); got != "board_health,create_rule,delete_rule,list_devices,notify_user,read_property,recent_events,update_profile,write_property" {
		t.Fatalf("tools %s", got)
	}
	// Ответ про дверь видит историю (mask: результаты старых инструментов скрыты).
	lastCall := calls[len(calls)-1]
	hist := ""
	for _, m := range lastCall.Messages {
		hist += m.Role + ":" + m.Content + "\n"
	}
	if !strings.Contains(hist, "включи свет") || !strings.Contains(hist, userctx.MaskedText) {
		t.Fatalf("history (mask) not as expected:\n%s", hist)
	}
	// Сегменты учтены.
	for _, r := range byType(recs, trace.LLMRequest) {
		seg := r["segments"].(map[string]any)
		if seg["prefix"].(float64) <= 0 || seg["turn"].(float64) <= 0 {
			t.Fatalf("segments %v", seg)
		}
	}
	if h.room.Get("relay1", "on") != true {
		t.Fatal("relay not switched")
	}
}

// ---------------------------------------------------- hybrid + бюджет

func TestMiniDayHybridBudget(t *testing.T) {
	h := newHarness(t, "  wake: hybrid\n  hwctx: pull\n  history: window\n  window_turns: 4\nbudget: {daily_calls: 4, degrade_at: 0.5}\n", noopOracle)
	h.set("07:10", "door1", "open", true) // вызов 1; heartbeat сдвигается на 07:40:12
	h.set("08:20", "t1", "temperature", 31.0)
	h.user("08:30", "u1", "привет", 0)
	h.set("08:40", "t1", "temperature", 28.5) // сброс порога
	h.set("08:45", "t1", "temperature", 31.5) // повторный выход за порог — лимит исчерпан
	h.at("09:00")
	recs := h.close()
	counts := checkTrace(t, recs)

	if hb := wakeTimes(recs, "heartbeat", false); !eqFloats(hb, []float64{hm("07:40:12")}) {
		t.Fatalf("admitted heartbeats %v, want [07:40:12] (hybrid resets the timer after a gate wake)", hb)
	}
	type sw struct {
		t      float64
		reason string
	}
	var got []sw
	for _, w := range byType(recs, trace.Wake) {
		if w.b("suppressed") {
			got = append(got, sw{w.f("ts_clock"), w.s("reason") + "/" + w.s("suppress_reason")})
		}
	}
	sort.Slice(got, func(i, j int) bool { return got[i].t < got[j].t })
	want := []sw{{hm("08:10:12"), "heartbeat/budget_degraded"}, {hm("08:45:12"), "gate/budget_exhausted"},
		{hm("09:00"), "heartbeat/budget_exhausted"}}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("suppressed wakes %v, want %v", got, want)
	}
	if counts[trace.LLMRequest] != 4 {
		t.Fatalf("llm requests %d, want 4 (budget)", counts[trace.LLMRequest])
	}
	if g := wakeTimes(recs, "gate", false); !eqFloats(g, []float64{hm("07:10:12"), hm("08:20:12")}) {
		t.Fatalf("gate wakes %v", g)
	}
	// pull: состояние в промпт не попадает, только время и событие.
	for _, c := range h.llm.Calls() {
		if strings.Contains(c.state(), "Изменения состояния") || strings.Contains(c.system(), "Состояние устройств") {
			t.Fatal("pull mode must not inject state")
		}
	}
	if u, ok := h.a.Metrics()["budget"].(sched.Usage); !ok || u.Level != "exhausted" || u.Calls != 4 {
		t.Fatalf("metrics.budget %+v", h.a.Metrics()["budget"])
	}
}

// ------------------------------------------------------- детерминизм

// Один и тот же день на виртуальных часах даёт одинаковые трассы (без полей
// настенного времени).
func TestMiniDayDeterministic(t *testing.T) {
	run := func() map[string][]string {
		h := newHarness(t, "  wake: hybrid\n  hwctx: delta_tail\n", roomOracle)
		h.set("07:10", "door1", "open", true)
		h.set("07:20", "t1", "temperature", 31.0)
		h.user("07:30", "u1", "включи свет", 1)
		h.at("08:30")
		recs := h.close()
		wall := map[string]bool{"ts_wall": true, "latency_ms": true, "ms": true, "queued_ms": true, "wall_s": true,
			"seq": true, "waited_ms": true, "run_id": true, "records": true,
			"config_hash": true, "base_url": true} // различаются порт фейкового LLM и имя прогона
		out := map[string][]string{}
		for _, r := range recs {
			norm := map[string]any{}
			for k, v := range r {
				if !wall[k] {
					norm[k] = v
				}
			}
			out[r.s("type")] = append(out[r.s("type")], fmt.Sprint(norm))
		}
		return out
	}
	a, b := run(), run()
	for typ, la := range a {
		lb := b[typ]
		if len(la) != len(lb) {
			t.Fatalf("%s: %d vs %d records", typ, len(la), len(lb))
		}
		for i := range la {
			if la[i] != lb[i] {
				t.Fatalf("%s[%d] differs:\n%s\n%s", typ, i, la[i], lb[i])
			}
		}
	}
}

// ------------------------------------------------- обрыв связи с LLM

// Модель недоступна полчаса: сорвавшийся ход, вопрос пользователя, значимое
// событие и плановая проверка откладываются и после восстановления уходят
// одним ходом; к модели за время обрыва — один неудачный запрос и пробы без
// токенов. Трасса остаётся согласованной.
func TestMiniDayLLMOutage(t *testing.T) {
	h := newHarness(t, "  wake: hybrid\n  hwctx: delta_tail\n", roomOracle)
	h.llm.down.Store(true)
	h.set("07:05", "door1", "open", true) // ход 07:05:12 сорвётся (503)
	rep := h.user("07:10", "u1", "что с дверью?", 0)
	if !rep.Deferred || rep.OK {
		t.Fatalf("user reply during outage %+v", rep)
	}
	h.set("07:12", "t1", "temperature", 31.0)
	h.at("07:40") // плановая проверка 07:35:12 тоже отложена
	if n := h.llm.failed.Load(); n != 1 || len(h.llm.Calls()) != 0 {
		t.Fatalf("outage must cost one failed request, got %d failed and %d served", n, len(h.llm.Calls()))
	}
	h.llm.down.Store(false)
	h.at("08:00")
	recs := h.close()
	counts := checkTrace(t, recs)

	if counts[trace.LLMUnavailable] != 1 || counts[trace.LLMAvailable] != 1 || counts[trace.Deferred] != 4 {
		t.Fatalf("link records %v", counts)
	}
	if h.llm.probes.Load() < 2 || counts[trace.LLMProbe] != int(h.llm.probes.Load()) {
		t.Fatalf("probes: server %d, trace %d", h.llm.probes.Load(), counts[trace.LLMProbe])
	}
	merged := wakeTimes(recs, "deferred", false)
	if len(merged) != 1 || merged[0] > hm("07:45") {
		t.Fatalf("deferred wakes %v (the next probe after 07:40 is due within probe_max_s)", merged)
	}
	for _, c := range h.llm.Calls() {
		if c.Reason != "deferred" {
			t.Fatalf("after recovery only the coalesced turn may call the model, got %q", c.Reason)
		}
	}
	for _, r := range byType(recs, trace.UserReply) {
		if r.s("input_id") == "u1" && (!r.b("deferred") || !r.b("ok")) {
			t.Fatalf("deferred user reply %v", r)
		}
	}
}
