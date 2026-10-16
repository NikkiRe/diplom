// Команда robot-probe — MCP-клиент замеров robot-mcp на живом стенде (аналог scripts/sim/rosmcp_probe.py,
// но статический Go-бинарник: работает на «плате» без Python).
//
//	robot-probe -server "robot-mcp -rosbridge ws://{proxy}" -rosbridge host:9090 -out DIR [-scenario compact|camera|idle]
//
// Что делает:
//  1. ставит между сервером и rosbridge TCP-прокси со счётчиком (байты в обе стороны, соединения) — независимая
//     проверка учёта трафика самого адаптера; {proxy} в команде сервера заменяется на 127.0.0.1:<порт>;
//  2. запускает сервер по stdio, подписывается на robot://state/robot, прогоняет сценарий;
//  3. для каждого шага пишет: задержку, размер ответа MCP, текст для LLM, картинки, трафик rosbridge, CPU и
//     PSS/RSS процесса сервера (/proc на Linux, ps на macOS), истинную позу до/после (/ground_truth/pose —
//     отдельным соединением мимо прокси), для асинхронных задач — время до уведомления task_end и его размер.
//
// Формат probe.json совместим с scripts/sim/rosmcp_analyze.py (steps/, tools.json).
package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/local/robotmcp/internal/imgutil"
	"github.com/local/robotmcp/internal/rosbridge"
)

type proxy struct {
	target         string
	up, down, conn atomic.Int64
	ln             net.Listener
}

func (p *proxy) start() error {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	p.ln = ln
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			p.conn.Add(1)
			go p.handle(c)
		}
	}()
	return nil
}

func (p *proxy) handle(c net.Conn) {
	t, err := net.Dial("tcp", p.target)
	if err != nil {
		c.Close()
		return
	}
	pipe := func(dst, src net.Conn, ctr *atomic.Int64) {
		buf := make([]byte, 64<<10)
		for {
			n, err := src.Read(buf)
			if n > 0 {
				ctr.Add(int64(n))
				if _, werr := dst.Write(buf[:n]); werr != nil {
					break
				}
			}
			if err != nil {
				break
			}
		}
		dst.Close()
		src.Close()
	}
	go pipe(t, c, &p.up)
	pipe(c, t, &p.down)
}

// ---------------------------------------------------------------------------------------------- ресурсы

func procSample(pid int) map[string]any {
	out := map[string]any{}
	if pid <= 0 {
		return out
	}
	if runtime.GOOS == "linux" {
		if b, err := os.ReadFile(fmt.Sprintf("/proc/%d/smaps_rollup", pid)); err == nil {
			for _, line := range strings.Split(string(b), "\n") {
				k, v, ok := strings.Cut(line, ":")
				if !ok {
					continue
				}
				switch k {
				case "Rss", "Pss", "Private_Clean", "Private_Dirty", "Anonymous", "Shared_Clean":
					f := strings.Fields(v)
					if len(f) > 0 {
						n, _ := strconv.Atoi(f[0])
						out[strings.ToLower(k)+"_kib"] = n
					}
				}
			}
		}
		if b, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid)); err == nil {
			s := string(b)
			f := strings.Fields(s[strings.LastIndex(s, ")")+2:])
			if len(f) > 17 {
				ut, _ := strconv.Atoi(f[11])
				st, _ := strconv.Atoi(f[12])
				th, _ := strconv.Atoi(f[17])
				out["cpu_s"] = float64(ut+st) / 100 // CLK_TCK = 100 на Linux/arm64
				out["threads"] = th
			}
		}
		return out
	}
	b, err := exec.Command("ps", "-o", "rss=,time=", "-p", strconv.Itoa(pid)).Output()
	if err == nil {
		f := strings.Fields(string(b))
		if len(f) == 2 {
			rss, _ := strconv.Atoi(f[0])
			out["rss_kib"] = rss
			out["cpu_s"] = parsePsTime(f[1])
		}
	}
	return out
}

func parsePsTime(s string) float64 {
	parts := strings.Split(s, ":")
	total := 0.0
	for _, p := range parts {
		v, _ := strconv.ParseFloat(p, 64)
		total = total*60 + v
	}
	return total
}

// ---------------------------------------------------------------------------------------------- истина

type truth struct {
	br  *rosbridge.Client
	mu  sync.Mutex
	x   float64
	y   float64
	t   time.Time
	cnt int
}

func newTruth(ctx context.Context, url string) *truth {
	g := &truth{br: rosbridge.New(rosbridge.Options{URL: url})}
	go g.br.Run(ctx)
	wctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if g.br.WaitConnected(wctx) != nil {
		return g
	}
	g.br.Subscribe(ctx, rosbridge.Sub{Topic: "/ground_truth/pose", Type: "geometry_msgs/msg/PoseStamped", ThrottleMs: 100,
		Compression: "cbor"}, func(m map[string]any) {
		x, ok1 := rosbridge.Num(m, "pose", "position", "x")
		y, ok2 := rosbridge.Num(m, "pose", "position", "y")
		if ok1 && ok2 {
			g.mu.Lock()
			g.x, g.y, g.t = x, y, time.Now()
			g.cnt++
			g.mu.Unlock()
		}
	})
	return g
}

func (g *truth) get() map[string]any {
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		g.mu.Lock()
		ok := g.cnt > 0 && time.Since(g.t) < time.Second
		x, y := g.x, g.y
		g.mu.Unlock()
		if ok {
			return map[string]any{"x": round3(x), "y": round3(y)}
		}
		time.Sleep(50 * time.Millisecond)
	}
	return nil
}

func (g *truth) navTo(ctx context.Context, x, y float64) map[string]any {
	t0 := time.Now()
	ch, err := g.br.SendGoal(ctx, g.br.NewID("reset"), "/navigate_to_pose", "nav2_msgs/action/NavigateToPose",
		map[string]any{"pose": map[string]any{"header": map[string]any{"frame_id": "map"},
			"pose": map[string]any{"position": map[string]any{"x": x, "y": y, "z": 0.0}, "orientation": map[string]any{"w": 1.0}}}}, nil)
	if err != nil {
		return map[string]any{"error": err.Error()}
	}
	select {
	case m := <-ch:
		return map[string]any{"status": m["status"], "s": round1(time.Since(t0).Seconds())}
	case <-time.After(150 * time.Second):
		return map[string]any{"status": "timeout"}
	}
}

func round1(x float64) float64 { return math.Round(x*10) / 10 }
func round3(x float64) float64 { return math.Round(x*1000) / 1000 }

func dist(a, b map[string]any) any {
	if a == nil || b == nil {
		return nil
	}
	return round3(math.Hypot(a["x"].(float64)-b["x"].(float64), a["y"].(float64)-b["y"].(float64)))
}

// ---------------------------------------------------------------------------------------------- сценарии

type step struct {
	Name     string
	Tool     string
	Args     map[string]any
	Async    bool        // ждать уведомление task_end
	Target   *[2]float64 // проверка прибытия по истине
	GT       bool
	PostWait time.Duration
	StopAt   time.Duration // для асинхронной задачи: через сколько вызвать stop (шаг «стоп в пути»)
	Resource string
}

var places = map[string][2]float64{"dock": {-2.0, -0.5}, "door": {1.8, 0.5}, "corridor": {0.0, 1.8}, "window": {0.5, -1.7}}

func tgt(p string) *[2]float64 { v := places[p]; return &v }

func scenario(name string, reps int, gap time.Duration) []step {
	switch name {
	case "compact":
		return []step{
			{Name: "status", Tool: "status"},
			{Name: "look_summary", Tool: "look", Args: map[string]any{}},
			{Name: "look_image", Tool: "look", Args: map[string]any{"image": true}},
			{Name: "go_door", Tool: "go_to", Args: map[string]any{"place": "door"}, Async: true, GT: true, Target: tgt("door")},
			{Name: "status_after", Tool: "status"},
			{Name: "look_at_door", Tool: "look", Args: map[string]any{}},
			{Name: "patrol", Tool: "patrol", Args: map[string]any{"places": []any{"corridor", "dock"}}, Async: true, GT: true, Target: tgt("dock")},
			{Name: "go_unknown", Tool: "go_to", Args: map[string]any{"place": "kitchen"}},
			{Name: "go_window_stop", Tool: "go_to", Args: map[string]any{"place": "window"}, Async: true, GT: true, StopAt: 4 * time.Second},
			{Name: "status_end", Tool: "status"},
			{Name: "return_dock", Tool: "go_to", Args: map[string]any{"place": "dock"}, Async: true, GT: true, Target: tgt("dock")},
			{Name: "diag", Resource: "robot://diag"},
		}
	case "camera":
		var out []step
		for i := 0; i < reps; i++ {
			out = append(out, step{Name: fmt.Sprintf("frame_%02d", i), Tool: "look", Args: map[string]any{"image": true}, PostWait: gap})
		}
		return out
	case "soak": // повторяющийся цикл поездок и чтений: PSS/CPU во времени (длительность — -gap)
		var out []step
		for i := 0; i < reps; i++ {
			out = append(out,
				step{Name: fmt.Sprintf("soak%02d_go_door", i), Tool: "go_to", Args: map[string]any{"place": "door"}, Async: true},
				step{Name: fmt.Sprintf("soak%02d_status", i), Tool: "status"},
				step{Name: fmt.Sprintf("soak%02d_look_image", i), Tool: "look", Args: map[string]any{"image": true}},
				step{Name: fmt.Sprintf("soak%02d_go_dock", i), Tool: "go_to", Args: map[string]any{"place": "dock"}, Async: true},
				step{Name: fmt.Sprintf("soak%02d_status_dock", i), Tool: "status"})
		}
		return out
	case "idle":
		return []step{{Name: "idle_status", Tool: "status", PostWait: gap}, {Name: "idle_end", Tool: "status"}}
	}
	return nil
}

// ---------------------------------------------------------------------------------------------- прогон

func main() {
	serverCmd := flag.String("server", "", "команда сервера ({proxy} → 127.0.0.1:<порт прокси>)")
	rb := flag.String("rosbridge", "127.0.0.1:9090", "host:port rosbridge (за прокси)")
	out := flag.String("out", "", "каталог результата")
	scen := flag.String("scenario", "compact", "compact | camera | idle")
	reps := flag.Int("reps", 10, "camera: число кадров")
	gap := flag.Duration("gap", 6*time.Second, "camera: пауза между кадрами; idle: длительность простоя")
	reset := flag.Bool("reset", true, "вернуть робота на dock до и после (мимо прокси)")
	callTO := flag.Duration("call-timeout", 180*time.Second, "таймаут вызова / ожидания уведомления")
	only := flag.String("only", "", "регвыр по именам шагов")
	flag.Parse()
	if *serverCmd == "" || *out == "" {
		flag.Usage()
		os.Exit(2)
	}
	if err := run(*serverCmd, *rb, *out, *scen, *reps, *gap, *reset, *callTO, *only); err != nil {
		fmt.Fprintln(os.Stderr, "robot-probe:", err)
		os.Exit(1)
	}
}

func run(serverCmd, rb, outDir, scen string, reps int, gap time.Duration, reset bool, callTO time.Duration, only string) error {
	var onlyRe *regexp.Regexp
	if only != "" {
		var err error
		if onlyRe, err = regexp.Compile(only); err != nil {
			return err
		}
	}
	stepDir := filepath.Join(outDir, "steps")
	if err := os.MkdirAll(stepDir, 0o755); err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	p := &proxy{target: rb}
	if err := p.start(); err != nil {
		return err
	}
	rbURL := "ws://" + rb
	gt := newTruth(ctx, rbURL)
	host, _ := os.Hostname()
	report := map[string]any{"scenario": scen, "server_cmd": serverCmd, "rosbridge": rb, "host": host,
		"system": runtime.GOOS + "/" + runtime.GOARCH, "started": time.Now().Format(time.RFC3339)}
	if reset {
		g := gt.get()
		report["reset_before"] = map[string]any{"gt": g}
		if d := dist(g, map[string]any{"x": -2.0, "y": -0.5}); d == nil || d.(float64) > 0.3 {
			report["reset_before"].(map[string]any)["nav"] = gt.navTo(ctx, -2.0, -0.5)
		}
	}
	args := strings.Fields(strings.ReplaceAll(serverCmd, "{proxy}", p.ln.Addr().String()))
	cmd := exec.Command(args[0], args[1:]...)
	errLog, _ := os.Create(filepath.Join(outDir, "server_stderr.log"))
	defer errLog.Close()
	cmd.Stderr = errLog
	notes := make(chan noteRec, 32)
	client := mcp.NewClient(&mcp.Implementation{Name: "robot-probe", Version: "1"}, &mcp.ClientOptions{
		ResourceUpdatedHandler: func(_ context.Context, r *mcp.ResourceUpdatedNotificationRequest) {
			b, _ := json.Marshal(r.Params)
			notes <- noteRec{meta: r.Params.Meta, bytes: len(b), at: time.Now()}
		}})
	t0 := time.Now()
	cs, err := client.Connect(ctx, &mcp.CommandTransport{Command: cmd}, nil)
	if err != nil {
		return err
	}
	report["init_ms"] = ms(time.Since(t0))
	pid := cmd.Process.Pid
	report["server_pid"] = pid
	ir := cs.InitializeResult()
	report["server_info"] = ir.ServerInfo
	report["protocol"] = ir.ProtocolVersion
	t1 := time.Now()
	lt, err := cs.ListTools(ctx, nil)
	if err != nil {
		return err
	}
	report["list_tools_ms"] = ms(time.Since(t1))
	tj, _ := json.MarshalIndent(lt.Tools, "", " ")
	os.WriteFile(filepath.Join(outDir, "tools.json"), tj, 0o644)
	tl, _ := json.Marshal(map[string]any{"tools": lt.Tools})
	report["n_tools"] = len(lt.Tools)
	report["tools_list_bytes"] = len(tl)
	if rl, err := cs.ListResources(ctx, nil); err == nil {
		b, _ := json.Marshal(rl)
		report["n_resources"], report["resources_list_bytes"] = len(rl.Resources), len(b)
	}
	report["n_prompts"] = 0
	if err := cs.Subscribe(ctx, &mcp.SubscribeParams{URI: "robot://state/robot"}); err != nil {
		return err
	}
	time.Sleep(500 * time.Millisecond) // подключение к rosbridge и латченная поза
	report["proc_idle"] = procSample(pid)
	var steps []map[string]any
	var allNotes []map[string]any
	drain := func() {
		for {
			select {
			case n := <-notes:
				allNotes = append(allNotes, map[string]any{"event": n.meta["robot/event"], "bytes": n.bytes, "state": n.meta["state"]})
			default:
				return
			}
		}
	}
	for i, s := range scenario(scen, reps, gap) {
		if onlyRe != nil && !onlyRe.MatchString(s.Name) {
			continue
		}
		drain()
		rec := map[string]any{"step": s.Name, "tool": s.Tool, "args": s.Args}
		if s.Resource != "" {
			rec["tool"], rec["args"] = "resources/read", map[string]any{"uri": s.Resource}
		}
		if s.GT {
			rec["gt_before"] = gt.get()
		}
		cpu0, _ := procSample(pid)["cpu_s"].(float64)
		up0, dn0, c0 := p.up.Load(), p.down.Load(), p.conn.Load()
		ts := time.Now()
		var res *mcp.CallToolResult
		var rres *mcp.ReadResourceResult
		var callErr error
		cctx, ccancel := context.WithTimeout(ctx, callTO)
		if s.Resource != "" {
			rres, callErr = cs.ReadResource(cctx, &mcp.ReadResourceParams{URI: s.Resource})
		} else {
			res, callErr = cs.CallTool(cctx, &mcp.CallToolParams{Name: s.Tool, Arguments: s.Args})
		}
		ccancel()
		rec["ms"] = ms(time.Since(ts))
		if callErr != nil {
			rec["exception"] = callErr.Error()
		} else if res != nil {
			digest(rec, res, stepDir, i, s.Name)
		} else if rres != nil {
			text := rres.Contents[0].Text
			os.WriteFile(filepath.Join(stepDir, fmt.Sprintf("%02d_%s.txt", i, s.Name)), []byte(text), 0o644)
			rec["text_bytes"] = len(text)
			var d any
			json.Unmarshal([]byte(text), &d)
			rec["diag"] = d
		}
		if s.Async && callErr == nil {
			if s.StopAt > 0 {
				time.Sleep(s.StopAt)
				st := time.Now()
				sres, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: "stop"})
				rec["stop_ms"] = ms(time.Since(st))
				if err == nil {
					rec["stop_text"] = sres.Content[0].(*mcp.TextContent).Text
				}
				stopGT := gt.get()
				time.Sleep(1500 * time.Millisecond)
				rec["coast_m"] = dist(stopGT, gt.get()) // сколько проехал после stop
			}
			wctx, wcancel := context.WithTimeout(ctx, callTO)
		wait:
			for {
				select {
				case n := <-notes:
					allNotes = append(allNotes, map[string]any{"event": n.meta["robot/event"], "bytes": n.bytes, "state": n.meta["state"]})
					if n.meta["robot/event"] == "task_end" {
						rec["event_ms"] = ms(n.at.Sub(ts))
						rec["event_bytes"] = n.bytes
						rec["event_state"] = n.meta["state"]
						rec["event_changed"] = n.meta["robot/changed"]
						break wait
					}
					rec["alerts"] = append(anyList(rec["alerts"]), n.meta["state"].(map[string]any)["alert"])
				case <-wctx.Done():
					rec["event_timeout"] = true
					break wait
				}
			}
			wcancel()
		}
		time.Sleep(300 * time.Millisecond) // хвост трафика
		up1, dn1, c1 := p.up.Load(), p.down.Load(), p.conn.Load()
		rec["rb_up"], rec["rb_down"], rec["rb_conns"] = up1-up0, dn1-dn0, c1-c0
		ps := procSample(pid)
		if c, ok := ps["cpu_s"].(float64); ok {
			rec["server_cpu_ms"] = round1((c - cpu0) * 1000)
		}
		rec["server_rss_kib"], rec["server_pss_kib"] = ps["rss_kib"], ps["pss_kib"]
		if s.PostWait > 0 && s.StopAt == 0 {
			time.Sleep(s.PostWait)
			up2, dn2 := p.up.Load(), p.down.Load()
			rec["rb_up_after_wait"], rec["rb_down_after_wait"] = up2-up1, dn2-dn1
			rec["post_wait_s"] = s.PostWait.Seconds()
		}
		if s.GT {
			rec["gt_after"] = gt.get()
			rec["moved_m"] = dist(rec["gt_before"].(map[string]any), rec["gt_after"].(map[string]any))
			if s.Target != nil {
				rec["err_to_target_m"] = dist(rec["gt_after"].(map[string]any), map[string]any{"x": s.Target[0], "y": s.Target[1]})
			}
		}
		steps = append(steps, rec)
		fmt.Fprintf(os.Stderr, "[probe] %-16s %9.1f ms text=%v img=%v event=%v rb↑%v ↓%v conns=%v err=%v %v\n",
			s.Name, rec["ms"], rec["text_bytes"], len(anyList(rec["images"])), rec["event_ms"], rec["rb_up"], rec["rb_down"],
			rec["rb_conns"], rec["is_error"], rec["exception"])
	}
	drain()
	report["proc_end"] = procSample(pid)
	report["steps"] = steps
	report["notifications"] = allNotes
	cs.Close()
	report["rb_total"] = map[string]any{"up": p.up.Load(), "down": p.down.Load(), "conns": p.conn.Load()}
	if reset {
		g := gt.get()
		report["reset_after"] = map[string]any{"gt": g}
		if d := dist(g, map[string]any{"x": -2.0, "y": -0.5}); d == nil || d.(float64) > 0.3 {
			report["reset_after"].(map[string]any)["nav"] = gt.navTo(ctx, -2.0, -0.5)
		}
	}
	b, _ := json.MarshalIndent(report, "", " ")
	return os.WriteFile(filepath.Join(outDir, "probe.json"), b, 0o644)
}

type noteRec struct {
	meta  mcp.Meta
	bytes int
	at    time.Time
}

func anyList(v any) []any {
	l, _ := v.([]any)
	return l
}

func ms(d time.Duration) float64 { return round1(float64(d.Microseconds()) / 1000) }

func digest(rec map[string]any, res *mcp.CallToolResult, dir string, idx int, name string) {
	b, _ := json.Marshal(res)
	rec["mcp_bytes"] = len(b)
	rec["is_error"] = res.IsError
	var texts []string
	var images []any
	for k, c := range res.Content {
		switch x := c.(type) {
		case *mcp.TextContent:
			texts = append(texts, x.Text)
		case *mcp.ImageContent:
			w, h := imgutil.JPEGSize(x.Data)
			f := fmt.Sprintf("%02d_%s_%d.jpg", idx, name, k)
			os.WriteFile(filepath.Join(dir, f), x.Data, 0o644)
			images = append(images, map[string]any{"mime": x.MIMEType, "b64_chars": base64.StdEncoding.EncodedLen(len(x.Data)),
				"bytes": len(x.Data), "w": w, "h": h, "file": f})
		}
	}
	text := strings.Join(texts, "\n")
	f := fmt.Sprintf("%02d_%s.txt", idx, name)
	os.WriteFile(filepath.Join(dir, f), []byte(text), 0o644)
	rec["text_bytes"] = len(text)
	rec["text_file"] = f
	rec["images"] = images
	rec["structured_bytes"] = 0
	if res.StructuredContent != nil {
		sb, _ := json.Marshal(res.StructuredContent)
		rec["structured_bytes"] = len(sb)
	}
	head := text
	if len(head) > 300 {
		head = head[:300]
	}
	rec["text_head"] = head
	_ = io.Discard
}
