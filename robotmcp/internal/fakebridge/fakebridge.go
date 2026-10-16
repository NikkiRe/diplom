// Package fakebridge — фейковый сервер rosbridge v2 для тестов robot-mcp: WebSocket, JSON и CBOR, мир
// turtlebot3_world в упрощении (9 колонн, круглая арена), кинематический робот, Nav2-подобное действие
// /navigate_to_pose (succeeded / aborted у «ящика» / canceled вытеснением или сервисом
// /navigate_to_pose/_action/cancel_goal), латченная /amcl_pose (публикуется при сдвиге ≥ 0,25 м или повороте
// ≥ 0,2 рад, как AMCL), /scan 10 Гц, ленивая камера JPEG, /battery_state, истинная поза, сервис
// /world/default/control (пауза и multi_step) и журнал всего, что прислал клиент.
//
// Как rosbridge 2.7.1, цель отключившегося клиента не отменяется: робот едет дальше, итог некому отдать.
//
// Время мира — сим. секунды; вне паузы они идут со скоростью Speed × стенное время.
package fakebridge

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"math"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/coder/websocket"

	"github.com/local/robotmcp/internal/cbor"
	"github.com/local/robotmcp/internal/imgutil"
)

// Options — параметры мира и робота.
type Options struct {
	Speed       float64       // ускорение сим. времени (по умолчанию 20)
	X, Y, Yaw   float64       // поза спавна (по умолчанию dock −2; −0,5)
	V           float64       // скорость робота, м/с сим. (0,26)
	Tick        time.Duration // шаг мира (5 мс стенного)
	FrameDelay  time.Duration // задержка первого кадра ленивой камеры (стенная, 50 мс)
	Paused      bool          // мир на паузе с начала
	BlockAbortS float64       // через сколько сим. с цель у «ящика» прерывается (5)
	NoCamera    bool
	Addr        string // адрес прослушивания (пусто — свободный порт 127.0.0.1)
}

// Obstacle — препятствие-круг (колонна, «ящик», «человек»).
type Obstacle struct {
	X, Y, R float64
	Label   string // "box" — блокирует цели рядом; "person" — для детектора
}

// Server — фейковый rosbridge.
type Server struct {
	opts Options
	http *httptest.Server
	URL  string

	mu        sync.Mutex
	t         float64 // сим. время
	paused    bool
	down      bool // связь «потеряна»: соединения отклоняются
	reply     Reply
	x, y, yaw float64
	amclX     float64
	amclY     float64
	amclYaw   float64
	goal      *goal
	obstacles []Obstacle
	battery   float64
	clients   map[*client]bool
	received  []map[string]any
	frame     []byte
	stop      chan struct{}
	done      chan struct{}
	BytesSent atomic.Int64
	Feedbacks atomic.Int64
	ZeroTwist atomic.Int64
}

type goal struct {
	id       string
	c        *client
	x, y     float64
	start    float64
	blocked  bool
	feedback bool
	stopAt   float64 // для заблокированной цели: где остановиться (расстояние до цели)
}

type topicSub struct {
	ids         map[string]bool
	throttleMs  float64
	compression string
	lastWall    time.Time
	lastSim     float64
	since       time.Time // момент подписки (ленивая камера)
	subT        float64   // сим. время подписки
}

type client struct {
	conn *websocket.Conn
	mu   sync.Mutex
	subs map[string]*topicSub // тема -> объединённая подписка (как в rosbridge: одна на тему у клиента)
	adv  map[string]string
	out  chan []byte
	bin  chan []byte
	dead chan struct{}
}

// Pillars — колонны turtlebot3_world (сетка 3×3 с шагом 1,1 м, радиус 0,15 м).
func Pillars() []Obstacle {
	var out []Obstacle
	for _, x := range []float64{-1.1, 0, 1.1} {
		for _, y := range []float64{-1.1, 0, 1.1} {
			out = append(out, Obstacle{X: x, Y: y, R: 0.15, Label: "pillar"})
		}
	}
	return out
}

const arenaR = 2.9

// Start запускает сервер.
func Start(opts Options) *Server {
	if opts.Speed <= 0 {
		opts.Speed = 20
	}
	if opts.V <= 0 {
		opts.V = 0.26
	}
	if opts.Tick <= 0 {
		opts.Tick = 5 * time.Millisecond
	}
	if opts.FrameDelay <= 0 {
		opts.FrameDelay = 50 * time.Millisecond
	}
	if opts.BlockAbortS <= 0 {
		opts.BlockAbortS = 5
	}
	if opts.X == 0 && opts.Y == 0 {
		opts.X, opts.Y = -2.0, -0.5
	}
	s := &Server{opts: opts, x: opts.X, y: opts.Y, yaw: opts.Yaw, amclX: opts.X, amclY: opts.Y, amclYaw: opts.Yaw,
		obstacles: Pillars(), battery: 1.0, paused: opts.Paused, clients: map[*client]bool{},
		stop: make(chan struct{}), done: make(chan struct{})}
	if !opts.NoCamera {
		s.frame = imgutil.SyntheticJPEG(640, 480, 80)
	}
	s.http = httptest.NewUnstartedServer(http.HandlerFunc(s.serveWS))
	if opts.Addr != "" {
		ln, err := net.Listen("tcp", opts.Addr)
		if err != nil {
			panic(err)
		}
		s.http.Listener.Close()
		s.http.Listener = ln
	}
	s.http.Start()
	s.URL = "ws" + strings.TrimPrefix(s.http.URL, "http")
	go s.loop()
	return s
}

// Close останавливает сервер.
func (s *Server) Close() {
	close(s.stop)
	<-s.done
	s.DropConnections()
	s.http.Close()
}

// DropConnections разрывает все соединения клиентов (проверка переподключения).
func (s *Server) DropConnections() {
	s.mu.Lock()
	cs := make([]*client, 0, len(s.clients))
	for c := range s.clients {
		cs = append(cs, c)
	}
	s.mu.Unlock()
	for _, c := range cs {
		c.conn.CloseNow()
	}
}

// Received — копия журнала сообщений клиентов.
func (s *Server) Received() []map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]map[string]any(nil), s.received...)
}

// Count — сколько сообщений клиента с op (и темой, действием или сервисом name, если name != "").
func (s *Server) Count(op, name string) int {
	n := 0
	for _, m := range s.Received() {
		if m["op"] == op && (name == "" || m["topic"] == name || m["action"] == name || m["service"] == name) {
			n++
		}
	}
	return n
}

// Pose — истинная поза робота и сим. время.
func (s *Server) Pose() (x, y, yaw, t float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.x, s.y, s.yaw, s.t
}

// Moving — есть ли активная цель.
func (s *Server) Moving() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.goal != nil
}

// SetBattery задаёт заряд 0..1 (публикуется в /battery_state).
func (s *Server) SetBattery(f float64) { s.mu.Lock(); s.battery = f; s.mu.Unlock() }

// SetPaused ставит мир на паузу (датчики не публикуются) или снимает её.
func (s *Server) SetPaused(p bool) { s.mu.Lock(); s.paused = p; s.mu.Unlock() }

// SetLink обрывает связь (разрывает соединения и отклоняет новые) или восстанавливает её.
func (s *Server) SetLink(up bool) {
	s.mu.Lock()
	s.down = !up
	s.mu.Unlock()
	if !up {
		s.DropConnections()
	}
}

// Reply — ответы на call_service; нулевое значение — как rosbridge и Nav2 без задержки.
type Reply struct {
	// Delay — выполнить вызов и ответить через столько стенного времени, не задерживая следующие сообщения
	// клиента: rosbridge по умолчанию вызывает сервисы в отдельных потоках (call_services_in_new_thread).
	Delay  time.Duration
	Mute   bool // вызов записывается в журнал, но не выполняется и остаётся без ответа
	Fail   bool // "result": false — ошибка rosbridge (сервиса нет, истёк его таймаут)
	Reject bool // cancel_goal: return_code 1 (ERROR_REJECTED), цели не отменяются
}

// SetReply задаёт ответы на call_service.
func (s *Server) SetReply(r Reply) { s.mu.Lock(); s.reply = r; s.mu.Unlock() }

// AddObstacle добавляет препятствие («ящик» с Label "box" блокирует цели в радиусе 0,6 м).
func (s *Server) AddObstacle(o Obstacle) {
	s.mu.Lock()
	s.obstacles = append(s.obstacles, o)
	s.mu.Unlock()
}

// ClearObstacles возвращает только колонны.
func (s *Server) ClearObstacles() { s.mu.Lock(); s.obstacles = Pillars(); s.mu.Unlock() }

// Teleport ставит робота (и AMCL) в точку.
func (s *Server) Teleport(x, y, yaw float64) {
	s.mu.Lock()
	s.x, s.y, s.yaw = x, y, yaw
	s.mu.Unlock()
	s.publishAMCL(true)
}

func (s *Server) serveWS(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	down := s.down
	s.mu.Unlock()
	if down {
		http.Error(w, "link down", http.StatusServiceUnavailable)
		return
	}
	conn, err := websocket.Accept(w, r, nil)
	if err != nil {
		return
	}
	conn.SetReadLimit(1 << 24)
	c := &client{conn: conn, subs: map[string]*topicSub{}, adv: map[string]string{},
		out: make(chan []byte, 256), bin: make(chan []byte, 256), dead: make(chan struct{})}
	s.mu.Lock()
	s.clients[c] = true
	s.mu.Unlock()
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		defer cancel()
		for {
			select {
			case b := <-c.out:
				if conn.Write(ctx, websocket.MessageText, b) != nil {
					return
				}
				s.BytesSent.Add(int64(len(b)))
			case b := <-c.bin:
				if conn.Write(ctx, websocket.MessageBinary, b) != nil {
					return
				}
				s.BytesSent.Add(int64(len(b)))
			case <-ctx.Done():
				return
			}
		}
	}()
	for {
		_, data, err := conn.Read(ctx)
		if err != nil {
			break
		}
		var m map[string]any
		if json.Unmarshal(data, &m) != nil {
			continue
		}
		s.mu.Lock()
		s.received = append(s.received, m)
		s.mu.Unlock()
		s.handle(c, m)
	}
	cancel()
	close(c.dead)
	s.mu.Lock()
	delete(s.clients, c)
	s.mu.Unlock()
	conn.CloseNow()
}

func (s *Server) handle(c *client, m map[string]any) {
	op, _ := m["op"].(string)
	id, _ := m["id"].(string)
	topic, _ := m["topic"].(string)
	switch op {
	case "advertise":
		c.mu.Lock()
		c.adv[topic], _ = m["type"].(string)
		c.mu.Unlock()
	case "publish":
		if topic == "/cmd_vel" {
			if tw, _ := m["msg"].(map[string]any); tw != nil {
				lin, _ := mapAt(tw, "twist", "linear")["x"].(float64)
				ang, _ := mapAt(tw, "twist", "angular")["z"].(float64)
				if lin == 0 && ang == 0 {
					s.ZeroTwist.Add(1)
				}
			}
		}
	case "subscribe":
		thr, _ := m["throttle_rate"].(float64)
		comp, _ := m["compression"].(string)
		s.mu.Lock()
		now := s.t
		s.mu.Unlock()
		c.mu.Lock()
		ts := c.subs[topic]
		if ts == nil {
			ts = &topicSub{ids: map[string]bool{}, throttleMs: thr, since: time.Now(), subT: now}
			c.subs[topic] = ts
		} else if thr < ts.throttleMs {
			ts.throttleMs = thr
		}
		ts.ids[id] = true
		if comp == "cbor" {
			ts.compression = "cbor"
		}
		c.mu.Unlock()
		if topic == "/amcl_pose" { // латченная: последняя поза сразу новому подписчику
			s.sendAMCL(c)
		}
	case "unsubscribe":
		c.mu.Lock()
		if ts := c.subs[topic]; ts != nil {
			delete(ts.ids, id)
			if len(ts.ids) == 0 {
				delete(c.subs, topic)
			}
		}
		c.mu.Unlock()
	case "send_action_goal":
		x, _ := floatAt(m, "args", "pose", "pose", "position", "x")
		y, _ := floatAt(m, "args", "pose", "pose", "position", "y")
		fb, _ := m["feedback"].(bool)
		s.mu.Lock()
		old := s.goal
		g := &goal{id: id, c: c, x: x, y: y, start: s.t, feedback: fb}
		for _, o := range s.obstacles {
			if o.Label == "box" && math.Hypot(o.X-x, o.Y-y) < 0.6 {
				g.blocked, g.stopAt = true, 0.56
			}
		}
		s.goal = g
		s.mu.Unlock()
		if old != nil { // вытеснение (Nav2 preemption)
			s.result(old.c, old.id, 5)
		}
	case "call_service":
		s.mu.Lock()
		rep := s.reply
		s.mu.Unlock()
		switch {
		case rep.Mute:
		case rep.Delay > 0:
			go func() {
				time.Sleep(rep.Delay)
				s.callService(c, m, rep)
			}()
		default:
			s.callService(c, m, rep)
		}
	}
}

// callService выполняет вызов сервиса и отвечает клиенту.
func (s *Server) callService(c *client, m map[string]any, rep Reply) {
	service, _ := m["service"].(string)
	resp := map[string]any{"op": "service_response", "id": m["id"], "service": service, "result": true,
		"values": map[string]any{}}
	switch {
	case rep.Fail:
		resp["result"], resp["values"] = false, "service call failed"
	case service == "/navigate_to_pose/_action/cancel_goal":
		if rep.Reject {
			resp["values"] = map[string]any{"return_code": 1, "goals_canceling": []any{}}
		} else {
			resp["values"] = s.cancelGoals(mapAt(m, "args", "goal_info"))
		}
	case strings.HasSuffix(service, "/control"):
		wc := mapAt(m, "args", "world_control")
		s.mu.Lock()
		if p, ok := wc["pause"].(bool); ok {
			s.paused = p
		}
		n, _ := wc["multi_step"].(float64)
		s.mu.Unlock()
		if n > 0 {
			s.advance(n * 0.001)
		}
		resp["values"] = map[string]any{"success": true}
	default:
		resp["result"] = false
		resp["values"] = "service not found"
	}
	s.sendJSON(c, resp)
}

// cancelGoals — action_msgs/srv/CancelGoal: нулевые uuid и stamp — отменить все цели (единственную у
// Nav2-подобного действия); итог canceled получает её клиент, если он ещё подключён. Ответ — как у rcl_action:
// return_code 0 (ERROR_NONE) и список отменяемых целей (в упрощении — id цели rosbridge вместо GoalInfo), при
// отсутствии целей — пустой.
func (s *Server) cancelGoals(info map[string]any) map[string]any {
	all := true
	uuid, _ := mapAt(info, "goal_id")["uuid"].([]any)
	for _, b := range uuid {
		all = all && b == 0.0
	}
	sec, _ := floatAt(info, "stamp", "sec")
	nsec, _ := floatAt(info, "stamp", "nanosec")
	if !all || sec != 0 || nsec != 0 {
		return map[string]any{"return_code": 2, "goals_canceling": []any{}} // ERROR_UNKNOWN_GOAL_ID: по id не умеем
	}
	s.mu.Lock()
	g := s.goal
	s.goal = nil
	s.mu.Unlock()
	if g == nil {
		return map[string]any{"return_code": 0, "goals_canceling": []any{}}
	}
	s.result(g.c, g.id, 5)
	return map[string]any{"return_code": 0, "goals_canceling": []any{map[string]any{"goal_id": g.id}}}
}

func (s *Server) result(c *client, id string, status int) {
	s.sendJSON(c, map[string]any{"op": "action_result", "id": id, "action": "/navigate_to_pose",
		"status": status, "result": status == 4, "values": map[string]any{"result": map[string]any{}}})
}

func (s *Server) sendJSON(c *client, m map[string]any) {
	b, _ := json.Marshal(m)
	select {
	case c.out <- b:
	case <-c.dead:
	}
}

func (s *Server) loop() {
	defer close(s.done)
	t := time.NewTicker(s.opts.Tick)
	defer t.Stop()
	last := time.Now()
	for {
		select {
		case <-s.stop:
			return
		case now := <-t.C:
			dt := now.Sub(last).Seconds()
			last = now
			s.mu.Lock()
			paused := s.paused
			s.mu.Unlock()
			if !paused {
				s.advance(dt * s.opts.Speed)
			}
			s.publishCamera()
		}
	}
}

// advance двигает мир на dt сим. секунд мелкими шагами и публикует датчики.
func (s *Server) advance(dt float64) {
	for dt > 1e-9 {
		h := math.Min(dt, 0.02)
		dt -= h
		s.mu.Lock()
		s.t += h
		var done *goal
		status := 0
		if g := s.goal; g != nil {
			d := math.Hypot(g.x-s.x, g.y-s.y)
			switch {
			case g.blocked && d <= g.stopAt:
				if s.t-g.start >= s.opts.BlockAbortS {
					done, status = g, 6
				}
			case d < 0.02:
				done, status = g, 4
			default:
				step := math.Min(d-g.stopAt*b2f(g.blocked), s.opts.V*h)
				if step > 0 {
					s.yaw = math.Atan2(g.y-s.y, g.x-s.x)
					s.x += step * (g.x - s.x) / d
					s.y += step * (g.y - s.y) / d
				}
			}
			if done != nil {
				s.goal = nil
			}
		}
		feedback := s.goal != nil && s.goal.feedback
		gc, gid := (*client)(nil), ""
		if feedback {
			gc, gid = s.goal.c, s.goal.id
		}
		s.mu.Unlock()
		if done != nil {
			s.publishAMCL(true)
			s.result(done.c, done.id, status)
		}
		if feedback && int(s.t*100) != int((s.t-h)*100) {
			s.Feedbacks.Add(1)
			s.sendJSON(gc, map[string]any{"op": "action_feedback", "id": gid, "action": "/navigate_to_pose",
				"values": map[string]any{"distance_remaining": 1.0, "current_pose": poseMsg(0, 0, 0, 0)}})
		}
		s.publishAMCL(false)
		s.publishPeriodic()
	}
}

func b2f(b bool) float64 {
	if b {
		return 1
	}
	return 0
}

// publishAMCL — латченная поза AMCL при сдвиге ≥ 0,25 м или повороте ≥ 0,2 рад (force — всегда).
func (s *Server) publishAMCL(force bool) {
	s.mu.Lock()
	moved := math.Hypot(s.x-s.amclX, s.y-s.amclY) >= 0.25 || math.Abs(angDiff(s.yaw, s.amclYaw)) >= 0.2
	if !moved && !force {
		s.mu.Unlock()
		return
	}
	s.amclX, s.amclY, s.amclYaw = s.x, s.y, s.yaw
	cs := s.clientList()
	s.mu.Unlock()
	for _, c := range cs {
		if c.subscribed("/amcl_pose") {
			s.sendAMCL(c)
		}
	}
}

func (s *Server) sendAMCL(c *client) {
	s.mu.Lock()
	msg := map[string]any{"header": header(s.t, "map"),
		"pose": map[string]any{"pose": poseMsg(s.amclX, s.amclY, s.amclYaw, 0), "covariance": make([]any, 36)}}
	cov := msg["pose"].(map[string]any)["covariance"].([]any)
	for i := range cov {
		cov[i] = 0.0
	}
	cov[0], cov[7], cov[35] = 0.0123, 0.0117, 0.0061
	s.mu.Unlock()
	s.publish(c, "/amcl_pose", msg, "")
}

func (s *Server) clientList() []*client {
	out := make([]*client, 0, len(s.clients))
	for c := range s.clients {
		out = append(out, c)
	}
	return out
}

func (c *client) subscribed(topic string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.subs[topic] != nil
}

// due — пора ли отдать сообщение темы с периодом period (сим. с) с учётом throttle_rate (стенное время,
// как в rosbridge). Возвращает компрессию подписки.
func (c *client) due(topic string, simT, period float64) (bool, string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	ts := c.subs[topic]
	if ts == nil {
		return false, ""
	}
	if simT-ts.lastSim < period-1e-9 && ts.lastSim > 0 {
		return false, ""
	}
	if ts.throttleMs > 0 && time.Since(ts.lastWall) < time.Duration(ts.throttleMs)*time.Millisecond {
		return false, ""
	}
	ts.lastSim, ts.lastWall = simT, time.Now()
	return true, ts.compression
}

func (s *Server) publishPeriodic() {
	s.mu.Lock()
	t := s.t
	cs := s.clientList()
	s.mu.Unlock()
	for _, c := range cs {
		if ok, comp := c.due("/scan", t, 0.1); ok {
			s.publish(c, "/scan", s.scan(), comp)
		}
		if ok, comp := c.due("/battery_state", t, 1.0); ok {
			s.mu.Lock()
			b := s.battery
			s.mu.Unlock()
			s.publish(c, "/battery_state", map[string]any{"header": header(t, ""), "percentage": b,
				"voltage": 11.1, "present": true}, comp)
		}
		if ok, comp := c.due("/ground_truth/pose", t, 0.1); ok {
			s.mu.Lock()
			msg := map[string]any{"header": header(t, "map"), "pose": poseMsg(s.x, s.y, s.yaw, 0)}
			s.mu.Unlock()
			s.publish(c, "/ground_truth/pose", msg, comp)
		}
	}
}

// publishCamera — ленивая камера: кадры 30 Гц стенного времени, пока есть подписчик, первый — через
// FrameDelay после подписки; на паузе кадр рендерится только после шага мира (как gz-sensors).
func (s *Server) publishCamera() {
	if s.frame == nil {
		return
	}
	s.mu.Lock()
	paused := s.paused
	t := s.t
	cs := s.clientList()
	s.mu.Unlock()
	for _, c := range cs {
		c.mu.Lock()
		ts := c.subs["/camera/image_raw/compressed"]
		ready := ts != nil && time.Since(ts.since) >= s.opts.FrameDelay &&
			time.Since(ts.lastWall) >= 33*time.Millisecond && (!paused || t > ts.subT && t != ts.lastSim)
		var comp string
		if ready {
			ts.lastWall, ts.lastSim, comp = time.Now(), t, ts.compression
		}
		c.mu.Unlock()
		if ready {
			s.publish(c, "/camera/image_raw/compressed", map[string]any{"header": header(t, "camera_rgb_optical_frame"),
				"format": "rgb8; jpeg compressed bgr8", "data": s.frame}, comp)
		}
	}
}

// scan — 360 лучей от робота по колоннам, препятствиям и стене арены (TB3: 0,12–3,5 м).
func (s *Server) scan() map[string]any {
	s.mu.Lock()
	x, y, yaw, t := s.x, s.y, s.yaw, s.t
	obs := append([]Obstacle(nil), s.obstacles...)
	s.mu.Unlock()
	ranges := make([]float32, 360)
	for i := range ranges {
		a := yaw + float64(i)*math.Pi/180
		dx, dy := math.Cos(a), math.Sin(a)
		best := rayCircleInside(x, y, dx, dy, arenaR)
		for _, o := range obs {
			if d, ok := rayCircle(x-o.X, y-o.Y, dx, dy, o.R); ok && d < best {
				best = d
			}
		}
		if best < 0.12 || best > 3.5 {
			ranges[i] = float32(math.Inf(1))
		} else {
			ranges[i] = float32(math.Round(best*1000) / 1000)
		}
	}
	return map[string]any{"header": header(t, "base_scan"), "angle_min": 0.0, "angle_max": 6.28,
		"angle_increment": math.Pi / 180, "time_increment": 0.0, "scan_time": 0.0, "range_min": 0.12,
		"range_max": 3.5, "ranges": cbor.Float32Array(ranges), "intensities": cbor.Float32Array(make([]float32, 360))}
}

func rayCircle(ox, oy, dx, dy, r float64) (float64, bool) {
	b := ox*dx + oy*dy
	c := ox*ox + oy*oy - r*r
	disc := b*b - c
	if disc < 0 {
		return 0, false
	}
	t := -b - math.Sqrt(disc)
	if t < 0 {
		return 0, false
	}
	return t, true
}

func rayCircleInside(x, y, dx, dy, r float64) float64 {
	b := x*dx + y*dy
	c := x*x + y*y - r*r
	return -b + math.Sqrt(b*b-c)
}

// publish отправляет сообщение темы клиенту в его компрессии (JSON: inf → null, байты → base64).
func (s *Server) publish(c *client, topic string, msg map[string]any, comp string) {
	env := map[string]any{"op": "publish", "topic": topic, "msg": msg}
	if comp == "cbor" {
		b, err := cbor.Encode(env)
		if err != nil {
			return
		}
		select {
		case c.bin <- b:
		case <-c.dead:
		}
		return
	}
	b, err := json.Marshal(jsonable(env))
	if err != nil {
		return
	}
	select {
	case c.out <- b:
	case <-c.dead:
	}
}

func jsonable(v any) any {
	switch x := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, e := range x {
			out[k] = jsonable(e)
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = jsonable(e)
		}
		return out
	case cbor.Float32Array:
		out := make([]any, len(x))
		for i, f := range x {
			if math.IsInf(float64(f), 0) || math.IsNaN(float64(f)) {
				out[i] = nil
			} else {
				out[i] = float64(f)
			}
		}
		return out
	case []byte:
		return base64.StdEncoding.EncodeToString(x)
	}
	return v
}

func header(t float64, frame string) map[string]any {
	sec := math.Floor(t)
	return map[string]any{"frame_id": frame, "stamp": map[string]any{"sec": sec, "nanosec": math.Round((t - sec) * 1e9)}}
}

func poseMsg(x, y, yaw, z float64) map[string]any {
	return map[string]any{"position": map[string]any{"x": x, "y": y, "z": z},
		"orientation": map[string]any{"x": 0.0, "y": 0.0, "z": math.Sin(yaw / 2), "w": math.Cos(yaw / 2)}}
}

func angDiff(a, b float64) float64 {
	d := math.Mod(a-b+math.Pi, 2*math.Pi)
	if d < 0 {
		d += 2 * math.Pi
	}
	return d - math.Pi
}

func mapAt(m map[string]any, path ...string) map[string]any {
	cur := m
	for _, p := range path {
		next, _ := cur[p].(map[string]any)
		if next == nil {
			return map[string]any{}
		}
		cur = next
	}
	return cur
}

func floatAt(m map[string]any, path ...string) (float64, bool) {
	cur := any(m)
	for _, p := range path {
		mm, ok := cur.(map[string]any)
		if !ok {
			return 0, false
		}
		cur = mm[p]
	}
	f, ok := cur.(float64)
	return f, ok
}
