package gate

import (
	"bufio"
	"compress/gzip"
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/local/hwmcp/internal/argschema"
	"github.com/local/hwmcp/internal/testutil"
)

func demoGate(t testing.TB, state StateReader) *Gate {
	t.Helper()
	raw, err := os.ReadFile(testutil.Testdata("gate_demo.json"))
	if err != nil {
		t.Fatal(err)
	}
	cfg, err := ParseConfig(raw)
	if err != nil {
		t.Fatal(err)
	}
	g, err := New(cfg, nil, state)
	if err != nil {
		t.Fatal(err)
	}
	return g
}

func args(t testing.TB, s string) any {
	t.Helper()
	v, err := argschema.Decode([]byte(s))
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func cmd(t testing.TB, id, dev, tool, a string, issued float64) Command {
	return Command{CmdID: id, Device: dev, Tool: tool, Args: args(t, a), IssuedAt: issued}
}

var defaultState = map[string]any{"window_open": false, "temp_c": 21.0, "water_level_pct": 50.0, "human_near": false}

func setDefault(st *MapState, ts float64) {
	for k, v := range defaultState {
		st.Set(k, v, ts)
	}
}

// --- перенос bench_gate.unit_tests() ---

func TestPrototypeUnitScenario(t *testing.T) {
	st := NewMapState()
	g := demoGate(t, st)
	setDefault(st, 0)

	// dry-run -> состояние изменилось -> commit отклоняется (TOCTOU)
	dec, tok := g.DryRun(cmd(t, "t1", "pump", "set_power", `{"on":true}`, 0), 0)
	if dec.Verdict != Allow || tok == nil {
		t.Fatalf("dry-run: %+v", dec)
	}
	st.Set("water_level_pct", 5.0, 1.0)
	if d := g.Commit(tok, 1.0); d.Reason != ReasonInterlock {
		t.Fatalf("commit after state change: %+v", d)
	}
	// commit после окна фиксации
	st.Set("water_level_pct", 50.0, 1.0)
	_, tok = g.DryRun(cmd(t, "t2", "pump", "set_power", `{"on":true}`, 1.0), 1.0)
	if d := g.Commit(tok, 1.0+g.CommitWindow+0.1); d.Reason != ReasonCommitExpired {
		t.Fatalf("commit after window: %+v", d)
	}
	// dry-run не расходует лимит частоты и не помечает ключ
	setDefault(st, 2.0)
	if d := g.Check(cmd(t, "t3", "pump", "set_power", `{"on":true}`, 2.0), 2.0); d.Verdict != Allow {
		t.Fatalf("t3: %+v", d)
	}
	if d := g.Check(cmd(t, "t3", "pump", "set_power", `{"on":true}`, 2.0), 2.5); d.Verdict != Duplicate {
		t.Fatalf("t3 repeat: %+v", d)
	}
	if d := g.Check(cmd(t, "t4", "pump", "set_power", `{"on":false}`, 3.0), 3.0); d.Reason != ReasonRateLimit {
		t.Fatalf("t4: %+v", d)
	}
	// аренда: вентилятор без продления уходит в безопасное состояние через lease_s
	if d := g.Check(cmd(t, "t5", "fan", "set_speed", `{"pct":20}`, 3.0), 3.0); d.Verdict != Allow {
		t.Fatalf("t5: %+v", d)
	}
	if ev := g.Tick(4.0); len(ev) != 0 || !g.Renew("fan", 4.0) {
		t.Fatal("renew at 4.0")
	}
	ev := g.Tick(6.5)
	if len(ev) != 1 || ev[0].Device != "fan" || !argschema.Equal(ev[0].SafeState["pct"], 0) {
		t.Fatalf("lease events: %+v", ev)
	}
	// подтверждение
	if d := g.Check(cmd(t, "t6", "door", "unlock", `{}`, 7.0), 7.0); d.Verdict != Confirm {
		t.Fatalf("t6: %+v", d)
	}
	c7 := cmd(t, "t7", "door", "unlock", `{}`, 7.0)
	c7.Confirmed = true
	if d := g.Check(c7, 7.0); d.Verdict != Allow {
		t.Fatalf("t7: %+v", d)
	}
	// clamp рядом с человеком
	st.Set("human_near", true, 8.0)
	d := g.Check(cmd(t, "t8", "fan", "set_speed", `{"pct":90}`, 8.0), 8.0)
	if d.Verdict != Clamp || testutil.Diff(testutil.Normalize(t, d.Args), map[string]any{"pct": json.Number("30")}) != "" {
		t.Fatalf("t8: %+v", d)
	}
	// неизвестное состояние = опасное
	g2 := demoGate(t, NewMapState())
	if d := g2.Check(cmd(t, "t9", "heater", "set_power", `{"on":false}`, 0), 0); d.Reason != ReasonStaleState {
		t.Fatalf("t9: %+v", d)
	}
}

// --- паритет с actuator_gate.py на эталонных потоках ---

type goldenCmd struct {
	CmdID     string          `json:"cmd_id"`
	Device    string          `json:"device"`
	Tool      string          `json:"tool"`
	Args      json.RawMessage `json:"args"`
	IssuedAt  float64         `json:"issued_at"`
	TTL       *float64        `json:"ttl_s"`
	Confirmed bool            `json:"confirmed"`
}

type goldenDec struct {
	Verdict string          `json:"verdict"`
	Reason  *string         `json:"reason"`
	Args    json.RawMessage `json:"args"`
}

func (c goldenCmd) toCommand(t testing.TB) Command {
	return Command{CmdID: c.CmdID, Device: c.Device, Tool: c.Tool, Args: args(t, string(c.Args)),
		IssuedAt: c.IssuedAt, TTL: c.TTL, Confirmed: c.Confirmed}
}

// reportFn печатает расхождение (t.Errorf) или молча считает его (мутационные проверки).
type reportFn func(format string, args ...any)

func compareDecision(t testing.TB, report reportFn, where string, got Decision, want goldenDec) bool {
	t.Helper()
	reason := ""
	if want.Reason != nil {
		reason = *want.Reason
	}
	if string(got.Verdict) != want.Verdict || got.Reason != reason {
		report("%s: got (%s, %s, %s), want (%s, %s)", where, got.Verdict, got.Reason, got.Detail, want.Verdict, reason)
		return false
	}
	wantArgs := testutil.MustDecode(t, want.Args)
	var gotArgs any
	if got.Args != nil {
		gotArgs = testutil.Normalize(t, got.Args)
	}
	if d := testutil.Diff(wantArgs, gotArgs); d != "" {
		report("%s: args differ: %s", where, d)
		return false
	}
	return true
}

func readGzLines(t testing.TB, name string, fn func(line []byte)) int {
	t.Helper()
	f, err := os.Open(testutil.Testdata("golden", name))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	zr, err := gzip.NewReader(f)
	if err != nil {
		t.Fatal(err)
	}
	sc := bufio.NewScanner(zr)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	n := 0
	for sc.Scan() {
		fn(sc.Bytes())
		n++
	}
	if err := sc.Err(); err != nil {
		t.Fatal(err)
	}
	return n
}

func compareEvents(t testing.TB, report reportFn, where string, got []SafeEvent, want [][]json.RawMessage) bool {
	t.Helper()
	if len(got) != len(want) {
		report("%s: %d safe events, want %d", where, len(got), len(want))
		return false
	}
	for i, ev := range want {
		var ts float64
		var dev string
		_ = json.Unmarshal(ev[0], &ts)
		_ = json.Unmarshal(ev[1], &dev)
		if got[i].TS != ts || got[i].Device != dev ||
			testutil.Diff(testutil.MustDecode(t, ev[2]), testutil.Normalize(t, got[i].SafeState)) != "" {
			report("%s: event %+v, want %s", where, got[i], ev)
			return false
		}
	}
	return true
}

// replayBench проигрывает поток bench_gate.run(seed=1); возвращает число шагов и расхождений.
func replayBench(t testing.TB, g *Gate, st *MapState, report reportFn) (n, bad int) {
	n = readGzLines(t, "gate_bench.jsonl.gz", func(line []byte) {
		var step struct {
			Cat   string              `json:"cat"`
			Now   float64             `json:"now"`
			State [][]json.RawMessage `json:"state"`
			Cmd   goldenCmd           `json:"cmd"`
			Want  goldenDec           `json:"want"`
			Tick  [][]json.RawMessage `json:"tick"`
		}
		if err := json.Unmarshal(line, &step); err != nil {
			t.Fatal(err)
		}
		for _, u := range step.State {
			var key string
			var ts float64
			_ = json.Unmarshal(u[0], &key)
			_ = json.Unmarshal(u[2], &ts)
			st.Set(key, testutil.MustDecode(t, u[1]), ts)
		}
		dec := g.Check(step.Cmd.toCommand(t), step.Now)
		if !compareDecision(t, report, step.Cat+"@"+jsonStr(step.Now), dec, step.Want) {
			bad++
		}
		if !compareEvents(t, report, "tick@"+jsonStr(step.Now), g.Tick(step.Now), step.Tick) {
			bad++
		}
	})
	return n, bad
}

// replayFuzz проигрывает случайные операции с границами (TTL, возраст состояния, пороги блокировок,
// окна частоты, повторы ключей, dry-run/commit, аренда).
func replayFuzz(t testing.TB, g *Gate, st *MapState, report reportFn) (n, bad int) {
	tokens := map[string]*Token{}
	n = readGzLines(t, "gate_fuzz.jsonl.gz", func(line []byte) {
		var op struct {
			Op        string          `json:"op"`
			Now       float64         `json:"now"`
			Key       string          `json:"key"`
			Value     json.RawMessage `json:"value"`
			TS        float64         `json:"ts"`
			Device    string          `json:"device"`
			Cmd       goldenCmd       `json:"cmd"`
			Token     *string         `json:"token"`
			Want      json.RawMessage `json:"want"`
			SafeCount int             `json:"safe_count"`
		}
		if err := json.Unmarshal(line, &op); err != nil {
			t.Fatal(err)
		}
		where := op.Op + "@" + jsonStr(op.Now)
		ok := true
		switch op.Op {
		case "state":
			st.Set(op.Key, testutil.MustDecode(t, op.Value), op.TS)
		case "tick":
			var want [][]json.RawMessage
			_ = json.Unmarshal(op.Want, &want)
			ok = compareEvents(t, report, where, g.Tick(op.Now), want)
		case "renew":
			var want bool
			_ = json.Unmarshal(op.Want, &want)
			if got := g.Renew(op.Device, op.Now); got != want {
				report("%s %s: renew %v, want %v", where, op.Device, got, want)
				ok = false
			}
		case "check":
			var want goldenDec
			_ = json.Unmarshal(op.Want, &want)
			ok = compareDecision(t, report, where+" "+op.Cmd.CmdID, g.Check(op.Cmd.toCommand(t), op.Now), want)
		case "dry_run":
			var want goldenDec
			_ = json.Unmarshal(op.Want, &want)
			dec, tok := g.DryRun(op.Cmd.toCommand(t), op.Now)
			ok = compareDecision(t, report, where+" "+op.Cmd.CmdID, dec, want)
			if (tok != nil) != (op.Token != nil) {
				report("%s: token presence %v, want %v", where, tok != nil, op.Token != nil)
				ok = false
			}
			if tok != nil && op.Token != nil {
				tokens[*op.Token] = tok
			}
		case "commit":
			var want goldenDec
			_ = json.Unmarshal(op.Want, &want)
			tok := tokens[*op.Token]
			delete(tokens, *op.Token)
			if tok == nil { // токен не выдан из-за более раннего расхождения
				ok = false
				break
			}
			ok = compareDecision(t, report, where+" "+*op.Token, g.Commit(tok, op.Now), want)
		case "final":
			if g.SafeCount != op.SafeCount {
				report("safe_count %d, want %d", g.SafeCount, op.SafeCount)
				ok = false
			}
		default:
			t.Fatalf("unknown op %q", op.Op)
		}
		if !ok {
			bad++
		}
	})
	return n, bad
}

func limitedReport(t *testing.T) reportFn {
	shown := 0
	return func(format string, args ...any) {
		shown++
		if shown <= 10 {
			t.Errorf(format, args...)
		}
	}
}

// Поток bench_gate.run(seed=1): те же команды и состояния -> те же решения и события аренды.
func TestParityBenchStream(t *testing.T) {
	st := NewMapState()
	n, bad := replayBench(t, demoGate(t, st), st, limitedReport(t))
	if bad > 0 {
		t.Fatalf("%d mismatches", bad)
	}
	t.Logf("%d steps of the bench stream match actuator_gate.py", n)
}

func TestParityFuzzOps(t *testing.T) {
	st := NewMapState()
	n, bad := replayFuzz(t, demoGate(t, st), st, limitedReport(t))
	if bad > 0 {
		t.Fatalf("%d mismatches", bad)
	}
	t.Logf("%d fuzz operations match actuator_gate.py", n)
}

// Эталонные потоки различают даже малые изменения политики: каждая мутация конфигурации
// демонстрационного стенда должна давать расхождения. Иначе тест паритета был бы слабым.
func TestParityDetectsPolicyMutations(t *testing.T) {
	if testing.Short() {
		t.Skip("replays the golden streams for every mutation")
	}
	raw, err := os.ReadFile(testutil.Testdata("gate_demo.json"))
	if err != nil {
		t.Fatal(err)
	}
	mutations := map[string][2]string{
		"heater threshold":   {`"ge": 30.0`, `"gt": 30.0`},
		"pump threshold":     {`"lt": 10.0`, `"le": 10.0`},
		"fan clamp boundary": {`{"arg": "pct", "gt": 30}`, `{"arg": "pct", "ge": 30}`},
		"fan clamp value":    {`{"set": 30}`, `{"set": 29}`},
		"command ttl":        {`"default_ttl_s": 10.0`, `"default_ttl_s": 10.0001`},
		"state age":          {`"max_state_age_s": 5.0`, `"max_state_age_s": 5.00005`},
		"commit window":      {`"commit_window_s": 5.0`, `"commit_window_s": 4.0`},
		"heater interval":    {`"min_interval_s": 30.0`, `"min_interval_s": 29.0`},
		"valve per minute":   {`"max_per_min": 20`, `"max_per_min": 19`},
		"fan lease":          {`"lease_s": 2.0`, `"lease_s": 2.5`},
		"confirm dropped":    {`"requires_confirm": ["unlock"]`, `"requires_confirm": []`},
		"valve range":        {`"maximum": 90`, `"maximum": 91`},
	}
	for name, m := range mutations {
		src := string(raw)
		if !strings.Contains(src, m[0]) {
			t.Fatalf("%s: pattern %q not found", name, m[0])
		}
		cfg, err := ParseConfig([]byte(strings.Replace(src, m[0], m[1], 1)))
		if err != nil {
			t.Fatal(err)
		}
		quiet := func(string, ...any) {}
		total := 0
		for _, replay := range []func(testing.TB, *Gate, *MapState, reportFn) (int, int){replayBench, replayFuzz} {
			st := NewMapState()
			g, err := New(cfg, nil, st)
			if err != nil {
				t.Fatal(err)
			}
			_, bad := replay(t, g, st, quiet)
			total += bad
		}
		if total == 0 {
			t.Errorf("mutation %q is not detected by the golden streams", name)
		} else {
			t.Logf("mutation %-18s -> %d mismatches", name, total)
		}
	}
}

func jsonStr(v any) string { b, _ := json.Marshal(v); return string(b) }

// --- поведение, которого нет в прототипе ---

func TestNilStateIsUnknown(t *testing.T) {
	st := NewMapState()
	g := demoGate(t, st)
	setDefault(st, 0)
	st.Set("temp_c", nil, 0) // датчик не прочитан
	d := g.Check(cmd(t, "a", "heater", "set_power", `{"on":true}`, 0), 0)
	if d.Reason != ReasonStaleState || !strings.Contains(d.Detail, "temp_c") {
		t.Fatalf("%+v", d)
	}
}

func TestDecisionReportsStateUsed(t *testing.T) {
	st := NewMapState()
	g := demoGate(t, st)
	setDefault(st, 1.0)
	d := g.Check(cmd(t, "a", "heater", "set_power", `{"on":true}`, 2.0), 2.0)
	if d.Verdict != Allow || len(d.State) != 2 || d.State["temp_c"].Age != 1.0 {
		t.Fatalf("%+v", d)
	}
}

func TestConditionEvalErrorFailsClosed(t *testing.T) {
	cfg, err := ParseConfig([]byte(`{"devices":{"relay":{"tools":{"set_state":{"type":"object",
		"properties":{"value":{"type":"boolean"}},"required":["value"],"additionalProperties":false}}}},
		"interlocks":[{"name":"hot","device":"relay","tool":"*",
		"deny":[{"if":{"state":"t","gt":30},"reason":"hot"}],"clamp":{"value":{"set":false}}}]}`))
	if err != nil {
		t.Fatal(err)
	}
	st := NewMapState()
	g, err := New(cfg, nil, st)
	if err != nil {
		t.Fatal(err)
	}
	st.Set("t", "warm", 0) // не число: сравнение невозможно
	d := g.Check(cmd(t, "a", "relay", "set_state", `{"value":true}`, 0), 0)
	if d.Verdict != Reject || d.Reason != ReasonInterlock || !strings.Contains(d.Detail, "не вычисляется") {
		t.Fatalf("%+v", d)
	}
	st.Set("t", 20.0, 0)
	if d := g.Check(cmd(t, "b", "relay", "set_state", `{"value":true}`, 0), 0); d.Verdict != Allow {
		t.Fatalf("%+v", d)
	}
}

func TestClampMinMaxKeepsInteger(t *testing.T) {
	cfg, _ := ParseConfig([]byte(`{"devices":{"fan":{"tools":{"set_speed":{"type":"object",
		"properties":{"pct":{"type":"integer","minimum":0,"maximum":100}},"required":["pct"],"additionalProperties":false}}}},
		"interlocks":[{"name":"near","device":"fan","tool":"set_speed",
		"deny":[{"if":{"all":[{"state":"human_near","eq":true},{"arg":"pct","gt":30}]},"reason":"near"}],
		"clamp":{"pct":{"max":30}}}]}`))
	st := NewMapState()
	g, err := New(cfg, nil, st)
	if err != nil {
		t.Fatal(err)
	}
	st.Set("human_near", true, 0)
	d := g.Check(cmd(t, "a", "fan", "set_speed", `{"pct":75}`, 0), 0)
	if d.Verdict != Clamp || jsonStr(d.Args) != `{"pct":30}` {
		t.Fatalf("%+v %s", d, jsonStr(d.Args))
	}
}

func TestNeedsDerivedFromConditions(t *testing.T) {
	cfg, _ := ParseConfig([]byte(`{"devices":{"r":{"tools":{"t":{"type":"object"}}}},
		"interlocks":[{"name":"x","device":"r","tool":"t",
		"deny":[{"if":{"any":[{"state":"b","eq":true},{"not":{"state":"a","lt":3}}]},"reason":"x"}]}]}`))
	g, err := New(cfg, nil, NewMapState())
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(g.interlocks[0].Needs, ","); got != "a,b" {
		t.Fatalf("needs %s", got)
	}
}

func TestConfigErrors(t *testing.T) {
	obj := `{"type":"object","properties":{"on":{"type":"boolean"}},"required":["on"]}`
	for name, cfg := range map[string]string{
		"unknown key":          `{"devices":{},"bogus":1}`,
		"device without tools": `{"devices":{"x":{"min_interval_s":1}}}`,
		"non-object schema":    `{"devices":{"x":{"tools":{"t":{"type":"string"}}}}}`,
		"unsupported keyword":  `{"devices":{"x":{"tools":{"t":{"type":"object","not":{}}}}}}`,
		"bad schema":           `{"devices":{"x":{"tools":{"t":{"type":"object","minimum":"0"}}}}}`,
		"lease without safe":   `{"devices":{"x":{"tools":{"t":` + obj + `},"lease_s":2}}}`,
		"read_only unknown":    `{"devices":{"x":{"tools":{"t":` + obj + `},"read_only":["q"]}}}`,
		"interlock device":     `{"devices":{"x":{"tools":{"t":` + obj + `}}},"interlocks":[{"name":"i","device":"y","tool":"t","deny":[{"if":{"arg":"on","eq":true},"reason":"r"}]}]}`,
		"interlock tool":       `{"devices":{"x":{"tools":{"t":` + obj + `}}},"interlocks":[{"name":"i","device":"x","tool":"q","deny":[{"if":{"arg":"on","eq":true},"reason":"r"}]}]}`,
		"needs incomplete":     `{"devices":{"x":{"tools":{"t":` + obj + `}}},"interlocks":[{"name":"i","device":"x","tool":"t","needs":["a"],"deny":[{"if":{"state":"b","eq":true},"reason":"r"}]}]}`,
		"bad operator":         `{"devices":{"x":{"tools":{"t":` + obj + `}}},"interlocks":[{"name":"i","device":"x","tool":"t","deny":[{"if":{"arg":"on","is":true},"reason":"r"}]}]}`,
		"lt on string":         `{"devices":{"x":{"tools":{"t":` + obj + `}}},"interlocks":[{"name":"i","device":"x","tool":"t","deny":[{"if":{"arg":"on","lt":"a"},"reason":"r"}]}]}`,
		"no reason":            `{"devices":{"x":{"tools":{"t":` + obj + `}}},"interlocks":[{"name":"i","device":"x","tool":"t","deny":[{"if":{"arg":"on","eq":true}}]}]}`,
		"clamp both":           `{"devices":{"x":{"tools":{"t":` + obj + `}}},"interlocks":[{"name":"i","device":"x","tool":"t","deny":[{"if":{"arg":"on","eq":true},"reason":"r"}],"clamp":{"on":{"set":false,"max":1}}}]}`,
		"duplicate name":       `{"devices":{"x":{"tools":{"t":` + obj + `}}},"interlocks":[{"name":"i","device":"x","tool":"t","deny":[{"if":{"arg":"on","eq":true},"reason":"r"}]},{"name":"i","device":"x","tool":"*","deny":[{"if":{"arg":"on","eq":true},"reason":"r"}]}]}`,
	} {
		c, err := ParseConfig([]byte(cfg))
		if err == nil {
			_, err = New(c, nil, NewMapState())
		}
		if err == nil {
			t.Errorf("%s: expected an error", name)
		}
	}
}

func TestExtraToolsMergeAndConflict(t *testing.T) {
	extra := map[string]map[string]any{"relay": {"set_state": map[string]any{"type": "object",
		"properties": map[string]any{"value": map[string]any{"type": "boolean"}}, "required": []any{"value"}}}}
	cfg, _ := ParseConfig([]byte(`{"devices":{"relay":{"min_interval_s":5,"requires_confirm":["set_state"]}}}`))
	g, err := New(cfg, extra, NewMapState())
	if err != nil {
		t.Fatal(err)
	}
	if d := g.Device("relay"); d == nil || d.MinInterval != 5 || !d.RequiresConfirm["set_state"] {
		t.Fatalf("%+v", d)
	}
	cfg2, _ := ParseConfig([]byte(`{"devices":{"relay":{"tools":{"set_state":{"type":"object"}}}}}`))
	if _, err := New(cfg2, extra, NewMapState()); err == nil {
		t.Fatal("tool defined twice must fail")
	}
}

func TestIdempotencyCapacityEvictsOldest(t *testing.T) {
	cfg, _ := ParseConfig([]byte(`{"idem_capacity":2,"devices":{"led":{"tools":{"set":{"type":"object"}}}}}`))
	g, _ := New(cfg, nil, NewMapState())
	for _, id := range []string{"a", "b", "c"} {
		if d := g.Check(cmd(t, id, "led", "set", `{}`, 0), 0); d.Verdict != Allow {
			t.Fatalf("%s: %+v", id, d)
		}
	}
	if d := g.Check(cmd(t, "a", "led", "set", `{}`, 0), 0); d.Verdict != Allow {
		t.Fatalf("evicted key must be accepted again: %+v", d)
	}
	if d := g.Check(cmd(t, "c", "led", "set", `{}`, 0), 0); d.Verdict != Duplicate {
		t.Fatalf("recent key must be duplicate: %+v", d)
	}
}

func TestNextExpiry(t *testing.T) {
	st := NewMapState()
	g := demoGate(t, st)
	setDefault(st, 0)
	if _, ok := g.NextExpiry(); ok {
		t.Fatal("no leases yet")
	}
	g.Check(cmd(t, "f", "fan", "set_speed", `{"pct":10}`, 0), 0.5)
	if e, ok := g.NextExpiry(); !ok || e != 2.5 {
		t.Fatalf("expiry %v %v", e, ok)
	}
	if ev := g.Tick(2.5); len(ev) != 1 {
		t.Fatal("lease must fire exactly at expiry")
	}
	if _, ok := g.NextExpiry(); ok {
		t.Fatal("lease must be gone")
	}
}
