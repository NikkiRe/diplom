// Package rosbridge — клиент протокола rosbridge v2 (WebSocket, JSON; CBOR для тяжёлых тем) без ROS на плате.
//
// Отличия от ros-mcp-server 3.1.2, которые и дают экономию (docs/robot_sim.md §8, §12.4):
//   - одно постоянное соединение на сессию с переподключением; подписки и advertise восстанавливаются сами;
//   - постоянные подписки — с throttle_rate и queue_length 1 (rosbridge сам прореживает поток, на плату
//     приходит только последнее значение интервала); последние сообщения кэшируются у вызывающего;
//   - разовое чтение (Once) — subscribe → первое сообщение → unsubscribe в том же соединении;
//   - цели действий — без feedback (Nav2 шлёт его ≈100 раз/с, 0,9–1,6 МБ на цель): только итог;
//   - цели «своих» действий (Options.Actions) не остаются без хозяина: rosbridge 2.7.1 не отменяет цели
//     отключившегося клиента (у capabilities/send_action_goal.py нет finish), поэтому клиент отменяет их сам
//     при подключении — до того, как соединение станет доступно для новых целей; отмена, оставшаяся без
//     ответа, рвёт соединение (выполненная позже, она сняла бы следующую цель);
//   - учёт трафика: байты TCP (кадры WebSocket и рукопожатие — как счётный прокси замеров 07.10) и
//     сообщения по видам (op/тема) в обе стороны.
package rosbridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"sort"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/coder/websocket"

	"github.com/local/robotmcp/internal/cbor"
)

// ErrDisconnected — нет соединения с rosbridge (или оно потеряно во время ожидания).
var ErrDisconnected = errors.New("rosbridge: disconnected")

// errWithdrawn — цель не отправлена: пока шла отмена, она стала не нужна (valid в SendGoal).
var errWithdrawn = errors.New("rosbridge: goal withdrawn")

// Handler получает поле "msg" сообщения publish (из горутины чтения: обработчик должен быть быстрым).
type Handler func(msg map[string]any)

// Sub — параметры подписки rosbridge.
type Sub struct {
	Topic       string
	Type        string
	ThrottleMs  int    // throttle_rate: не чаще одного сообщения за интервал (0 — без прореживания)
	QueueLength int    // queue_length (по умолчанию 1)
	Compression string // "" | "none" | "cbor"
}

func (s Sub) op(id string) map[string]any {
	m := map[string]any{"op": "subscribe", "id": id, "topic": s.Topic, "type": s.Type}
	if s.ThrottleMs > 0 {
		m["throttle_rate"] = s.ThrottleMs
	}
	q := s.QueueLength
	if q <= 0 {
		q = 1
	}
	m["queue_length"] = q
	if s.Compression != "" && s.Compression != "none" {
		m["compression"] = s.Compression
	}
	return m
}

// Options — параметры клиента.
type Options struct {
	URL          string
	Logger       *slog.Logger
	DialTimeout  time.Duration // по умолчанию 5 с
	WriteTimeout time.Duration // по умолчанию 5 с
	ReconnectMin time.Duration // по умолчанию 0,5 с
	ReconnectMax time.Duration // по умолчанию 5 с
	ReadLimit    int64         // по умолчанию 32 МиБ
	// StableAfter — соединение, прожившее меньше, считается «мигающим»: перед новым подключением — пауза с
	// нарастанием (иначе сервер, который принимает рукопожатие и сразу закрывает, даёт тысячи переподключений
	// в секунду). По умолчанию 10 с.
	StableAfter time.Duration
	// Actions — действия, целями которых распоряжается только этот клиент. Все их цели отменяются
	// (CancelAllGoals) при первом подключении — их мог оставить прошлый процесс адаптера — и после обрыва с
	// неоконченной целью, иначе робот доехал бы до цели, о которой уже никто не знает. Отмена идёт раньше,
	// чем соединение станет доступно (Connected, Send, SendGoal): «отменить все» не снимет новую цель.
	// Неподтверждённая отмена повторяется при следующем подключении.
	Actions []string
	// CancelTimeout — ожидание ответа сервиса отмены целей, по умолчанию 1 с. Должно быть меньше
	// default_call_service_timeout rosbridge (5 с): иначе его ответ-ошибка по таймауту сочтётся итогом, хотя
	// отмена ещё может быть выполнена.
	CancelTimeout time.Duration
	// OnConnect вызывается после (пере)подключения и восстановления подписок; OnDisconnect — при потере;
	// OnCancel — с итогом каждой отмены всех целей действия.
	OnConnect    func(n int)
	OnDisconnect func(err error)
	OnCancel     func(Cancel)
}

// Cancel — итог отмены всех целей действия.
type Cancel struct {
	Action string
	Why    string // connect — при подключении; иначе — причина от вызывающего (stop, timeout, shutdown)
	Goals  int    // целей перешло в отмену (goals_canceling)
	Took   time.Duration
	Err    error
}

// Client — клиент rosbridge с одним соединением.
type Client struct {
	opts Options
	log  *slog.Logger

	mu        sync.Mutex
	conn      *websocket.Conn
	connected chan struct{} // закрыт, пока есть соединение
	adverts   map[string]string
	subs      map[string]persistent // id -> подписка
	waiters   map[string][]chan map[string]any
	handlers  map[string]map[string]Handler // тема -> id -> обработчик
	pending   map[string]chan map[string]any
	goals     map[string]string // id неоконченной цели -> действие
	owned     map[string]bool   // Options.Actions
	orphans   map[string]bool   // действия, цели которых надо отменить при подключении
	links     []func(up bool)
	seq       atomic.Int64
	// goalMu: отмена всех целей и отправка цели не перемежаются — цель, отправленная после начала отмены,
	// уходит после её ответа и под неё не попадает.
	goalMu sync.Mutex

	st stats
}

type persistent struct {
	sub Sub
	h   Handler
}

// New создаёт клиент; соединение устанавливает Run.
func New(opts Options) *Client {
	if opts.Logger == nil {
		opts.Logger = slog.New(slog.DiscardHandler)
	}
	if opts.DialTimeout <= 0 {
		opts.DialTimeout = 5 * time.Second
	}
	if opts.WriteTimeout <= 0 {
		opts.WriteTimeout = 5 * time.Second
	}
	if opts.ReconnectMin <= 0 {
		opts.ReconnectMin = 500 * time.Millisecond
	}
	if opts.ReconnectMax <= 0 {
		opts.ReconnectMax = 5 * time.Second
	}
	if opts.ReadLimit <= 0 {
		opts.ReadLimit = 32 << 20
	}
	if opts.StableAfter <= 0 {
		opts.StableAfter = 10 * time.Second
	}
	if opts.CancelTimeout <= 0 {
		opts.CancelTimeout = time.Second
	}
	c := &Client{opts: opts, log: opts.Logger, connected: make(chan struct{}),
		adverts: map[string]string{}, subs: map[string]persistent{}, waiters: map[string][]chan map[string]any{},
		handlers: map[string]map[string]Handler{}, pending: map[string]chan map[string]any{},
		goals: map[string]string{}, owned: map[string]bool{}, orphans: map[string]bool{},
		st: stats{rx: map[string]*kv{}, tx: map[string]*kv{}}}
	for _, a := range opts.Actions {
		c.owned[a], c.orphans[a] = true, true
	}
	return c
}

// NewID — уникальный идентификатор сообщения rosbridge с префиксом.
func (c *Client) NewID(prefix string) string {
	return prefix + ":" + strconv.FormatInt(c.seq.Add(1), 10)
}

// Run держит соединение: подключается, отменяет цели без хозяина (Options.Actions), восстанавливает
// advertise/подписки, читает; при обрыве переподключается: сразу, если соединение прожило ≥ StableAfter,
// иначе после паузы с нарастанием (как и после неудачного подключения). Возвращается по отмене ctx.
func (c *Client) Run(ctx context.Context) {
	backoff := c.opts.ReconnectMin
	var lastWarn time.Time
	sleep := func() bool {
		select {
		case <-ctx.Done():
			return false
		case <-time.After(backoff):
		}
		backoff = min(2*backoff, c.opts.ReconnectMax)
		return true
	}
	for n := 1; ctx.Err() == nil; {
		conn, err := c.dial(ctx)
		if err != nil {
			c.st.dialFail.Add(1)
			c.log.Debug("rosbridge dial failed", "url", c.opts.URL, "err", err)
			if !sleep() {
				return
			}
			continue
		}
		c.st.conns.Add(1)
		t0 := time.Now()
		err = c.serve(ctx, conn, n)
		n++
		if ctx.Err() != nil {
			return
		}
		lived := time.Since(t0)
		if c.opts.OnDisconnect != nil {
			c.opts.OnDisconnect(err)
		}
		// Предупреждение — не чаще раза в 30 с (мигающее соединение не должно заливать stderr).
		if time.Since(lastWarn) >= 30*time.Second {
			lastWarn = time.Now()
			c.log.Warn("rosbridge connection lost", "err", err, "lived", lived.Round(time.Millisecond),
				"short_conns", c.st.shortConns.Load())
		} else {
			c.log.Debug("rosbridge connection lost", "err", err, "lived", lived.Round(time.Millisecond))
		}
		if lived >= c.opts.StableAfter {
			backoff = c.opts.ReconnectMin
			continue
		}
		c.st.shortConns.Add(1)
		if !sleep() {
			return
		}
	}
}

// OnLink добавляет обработчик смены связи: up — после (пере)подключения и восстановления подписок; !up —
// при обрыве, раньше, чем ожидающие ответа узнают о нём. Вызывается из горутины соединения и должен быстро
// возвращаться.
func (c *Client) OnLink(f func(up bool)) {
	c.mu.Lock()
	c.links = append(c.links, f)
	c.mu.Unlock()
}

func (c *Client) dial(ctx context.Context) (*websocket.Conn, error) {
	d := &net.Dialer{Timeout: c.opts.DialTimeout, KeepAlive: 30 * time.Second}
	tr := &http.Transport{
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			conn, err := d.DialContext(ctx, network, addr)
			if err != nil {
				return nil, err
			}
			if tc, ok := conn.(*net.TCPConn); ok {
				_ = tc.SetNoDelay(true)
			}
			return &countConn{Conn: conn, st: &c.st}, nil
		},
		DisableCompression: true,
	}
	dctx, cancel := context.WithTimeout(ctx, c.opts.DialTimeout)
	defer cancel()
	conn, _, err := websocket.Dial(dctx, c.opts.URL, &websocket.DialOptions{
		HTTPClient: &http.Client{Transport: tr}, CompressionMode: websocket.CompressionDisabled})
	if err != nil {
		return nil, err
	}
	conn.SetReadLimit(c.opts.ReadLimit)
	return conn, nil
}

// serve отменяет цели без хозяина, открывает соединение для остальных, восстанавливает регистрации и читает
// до обрыва.
func (c *Client) serve(ctx context.Context, conn *websocket.Conn, n int) error {
	readErr := make(chan error, 1)
	rctx, cancel := context.WithCancel(ctx)
	go func() { readErr <- c.readLoop(rctx, conn); cancel() }() // обрыв прерывает и отмену ниже

	c.mu.Lock()
	orphans := sortedKeys(c.orphans)
	c.mu.Unlock()
	for _, a := range orphans {
		// итог — в OnCancel; неподтверждённая повторится при следующем подключении
		if err := c.cancelAll(rctx, conn, a, "connect"); unanswered(err) {
			return <-readErr // соединение закрыто: обрыв, завершение или отмена без ответа
		}
	}
	if rctx.Err() != nil { // обрыв или завершение раньше, чем соединение открылось
		conn.CloseNow()
		return <-readErr
	}

	c.mu.Lock()
	c.conn = conn
	close(c.connected)
	adverts := make(map[string]string, len(c.adverts))
	for t, ty := range c.adverts {
		adverts[t] = ty
	}
	subs := make(map[string]Sub, len(c.subs))
	for id, p := range c.subs {
		subs[id] = p.sub
	}
	c.mu.Unlock()

	for _, t := range sortedKeys(adverts) {
		_ = c.send(ctx, conn, map[string]any{"op": "advertise", "topic": t, "type": adverts[t]})
	}
	for _, id := range sortedKeys(subs) {
		_ = c.send(ctx, conn, subs[id].op(id))
	}
	// Подписку могли снять (Unsubscribe) между снимком и повторной отправкой: тогда её unsubscribe мог уйти
	// раньше нашего subscribe, и rosbridge продолжил бы поток, о котором клиент уже забыл. Снимаем такие.
	c.mu.Lock()
	var gone []string
	for _, id := range sortedKeys(subs) {
		if _, ok := c.subs[id]; !ok {
			gone = append(gone, id)
		}
	}
	links := append([]func(bool){}, c.links...)
	c.mu.Unlock()
	for _, id := range gone {
		_ = c.send(ctx, conn, map[string]any{"op": "unsubscribe", "id": id, "topic": subs[id].Topic})
	}
	if c.opts.OnConnect != nil {
		c.opts.OnConnect(n)
	}
	for _, f := range links {
		f(true)
	}
	var err error
	select {
	case err = <-readErr:
	case <-ctx.Done():
		err = ctx.Err()
	}
	cancel()
	c.mu.Lock()
	c.conn = nil
	c.connected = make(chan struct{})
	pending := c.pending
	c.pending = map[string]chan map[string]any{}
	for _, a := range c.goals { // цели соединения rosbridge не отменит: они остаются роботу без хозяина
		if c.owned[a] {
			c.orphans[a] = true
		}
	}
	clear(c.goals)
	links = append([]func(bool){}, c.links...)
	c.mu.Unlock()
	_ = conn.Close(websocket.StatusNormalClosure, "")
	conn.CloseNow()
	for _, f := range links {
		f(false)
	}
	for _, ch := range pending { // ожидающие ответы и цели: соединение потеряно
		select {
		case ch <- map[string]any{"op": "disconnected"}:
		default:
		}
	}
	return err
}

func (c *Client) readLoop(ctx context.Context, conn *websocket.Conn) error {
	for {
		typ, data, err := conn.Read(ctx)
		if err != nil {
			return err
		}
		var m map[string]any
		if typ == websocket.MessageBinary {
			v, derr := cbor.Decode(data)
			mm, ok := v.(map[string]any)
			if derr != nil || !ok {
				c.log.Warn("rosbridge: bad CBOR frame", "bytes", len(data), "err", derr)
				continue
			}
			m = mm
		} else if err := json.Unmarshal(data, &m); err != nil {
			c.log.Warn("rosbridge: bad JSON frame", "bytes", len(data), "err", err)
			continue
		}
		c.dispatch(m, len(data))
	}
}

func (c *Client) dispatch(m map[string]any, size int) {
	op, _ := m["op"].(string)
	key := op
	switch op {
	case "publish":
		topic, _ := m["topic"].(string)
		key = "publish " + topic
		msg, _ := m["msg"].(map[string]any)
		c.mu.Lock()
		hs := make([]Handler, 0, len(c.handlers[topic]))
		for _, id := range sortedKeys(c.handlers[topic]) {
			hs = append(hs, c.handlers[topic][id])
		}
		ws := append([]chan map[string]any(nil), c.waiters[topic]...)
		c.mu.Unlock()
		c.st.add(true, key, size)
		if msg == nil {
			return
		}
		for _, h := range hs {
			h(msg)
		}
		for _, w := range ws {
			select {
			case w <- msg:
			default:
			}
		}
		return
	case "service_response", "action_result":
		id, _ := m["id"].(string)
		c.mu.Lock()
		ch := c.pending[id]
		delete(c.pending, id)
		delete(c.goals, id)
		c.mu.Unlock()
		if ch != nil {
			ch <- m
		}
	case "status":
		c.log.Debug("rosbridge status", "level", m["level"], "msg", m["msg"], "id", m["id"])
	}
	c.st.add(true, key, size)
}

func (c *Client) send(ctx context.Context, conn *websocket.Conn, m map[string]any) error {
	b, err := json.Marshal(m)
	if err != nil {
		return err
	}
	wctx, cancel := context.WithTimeout(ctx, c.opts.WriteTimeout)
	defer cancel()
	if err := conn.Write(wctx, websocket.MessageText, b); err != nil {
		return err
	}
	key, _ := m["op"].(string)
	if t, ok := m["topic"].(string); ok && key == "publish" {
		key += " " + t
	}
	c.st.add(false, key, len(b))
	return nil
}

// Send отправляет сообщение протокола (ErrDisconnected без соединения).
func (c *Client) Send(ctx context.Context, m map[string]any) error {
	c.mu.Lock()
	conn := c.conn
	c.mu.Unlock()
	if conn == nil {
		return ErrDisconnected
	}
	return c.send(ctx, conn, m)
}

// Connected — есть ли соединение.
func (c *Client) Connected() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.conn != nil
}

// WaitConnected ждёт соединения до отмены ctx.
func (c *Client) WaitConnected(ctx context.Context) error {
	c.mu.Lock()
	ch := c.connected
	c.mu.Unlock()
	select {
	case <-ch:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Advertise объявляет тему для публикации (запоминается и повторяется после переподключения).
func (c *Client) Advertise(ctx context.Context, topic, typ string) {
	c.mu.Lock()
	c.adverts[topic] = typ
	c.mu.Unlock()
	_ = c.Send(ctx, map[string]any{"op": "advertise", "topic": topic, "type": typ})
}

// Publish публикует сообщение в объявленную тему.
func (c *Client) Publish(ctx context.Context, topic string, msg any) error {
	return c.Send(ctx, map[string]any{"op": "publish", "topic": topic, "msg": msg})
}

// Subscribe — постоянная подписка (восстанавливается после переподключения). Возвращает её id.
func (c *Client) Subscribe(ctx context.Context, s Sub, h Handler) string {
	id := c.NewID("sub")
	c.mu.Lock()
	c.subs[id] = persistent{sub: s, h: h}
	if c.handlers[s.Topic] == nil {
		c.handlers[s.Topic] = map[string]Handler{}
	}
	c.handlers[s.Topic][id] = h
	c.mu.Unlock()
	_ = c.Send(ctx, s.op(id))
	return id
}

// Unsubscribe снимает постоянную подписку.
func (c *Client) Unsubscribe(ctx context.Context, id string) {
	c.mu.Lock()
	p, ok := c.subs[id]
	delete(c.subs, id)
	if ok {
		delete(c.handlers[p.sub.Topic], id)
	}
	c.mu.Unlock()
	if ok {
		_ = c.Send(ctx, map[string]any{"op": "unsubscribe", "id": id, "topic": p.sub.Topic})
	}
}

// Once — первое сообщение темы: subscribe → ожидание до timeout → unsubscribe (в том же соединении).
// Пока ждём, вызывается poke (если не nil) — например, микрошаг мира на паузе (--sim-step).
func (c *Client) Once(ctx context.Context, s Sub, timeout time.Duration, poke func(context.Context)) (map[string]any, error) {
	c.mu.Lock()
	if c.conn == nil {
		c.mu.Unlock()
		return nil, ErrDisconnected
	}
	ch := make(chan map[string]any, 1)
	c.waiters[s.Topic] = append(c.waiters[s.Topic], ch)
	c.mu.Unlock()
	id := c.NewID("once")
	defer func() {
		c.mu.Lock()
		ws := c.waiters[s.Topic]
		for i, w := range ws {
			if w == ch {
				c.waiters[s.Topic] = append(ws[:i:i], ws[i+1:]...)
				break
			}
		}
		c.mu.Unlock()
		_ = c.Send(context.Background(), map[string]any{"op": "unsubscribe", "id": id, "topic": s.Topic})
	}()
	if err := c.Send(ctx, s.op(id)); err != nil {
		return nil, err
	}
	tctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	if poke != nil {
		go poke(tctx)
	}
	select {
	case m := <-ch:
		return m, nil
	case <-tctx.Done():
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, context.DeadlineExceeded
	}
}

// request отправляет сообщение с id по conn (nil — по текущему соединению) и ждёт ответа с тем же id
// (service_response / action_result). Ожидание регистрируется под той же блокировкой, под которой выбрано
// соединение: при его обрыве ожидающий получит {"op":"disconnected"}.
func (c *Client) request(ctx context.Context, conn *websocket.Conn, id string, m map[string]any) (<-chan map[string]any, error) {
	ch := make(chan map[string]any, 1)
	c.mu.Lock()
	if conn == nil {
		conn = c.conn
	}
	if conn != nil {
		c.pending[id] = ch
	}
	c.mu.Unlock()
	if conn == nil {
		return nil, ErrDisconnected
	}
	if err := c.send(ctx, conn, m); err != nil {
		c.Forget(id)
		return nil, err
	}
	return ch, nil
}

// Forget снимает ожидание ответа (например, цель, вытесненная новой или уже отменённая).
func (c *Client) Forget(id string) {
	c.mu.Lock()
	delete(c.pending, id)
	delete(c.goals, id)
	c.mu.Unlock()
}

// CallService вызывает сервис и ждёт ответа.
func (c *Client) CallService(ctx context.Context, service string, args any, timeout time.Duration) (map[string]any, error) {
	return c.call(ctx, nil, service, args, timeout)
}

func (c *Client) call(ctx context.Context, conn *websocket.Conn, service string, args any, timeout time.Duration) (map[string]any, error) {
	id := c.NewID("call")
	ch, err := c.request(ctx, conn, id, map[string]any{"op": "call_service", "id": id, "service": service, "args": args})
	if err != nil {
		return nil, err
	}
	tctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	select {
	case m := <-ch:
		if m["op"] == "disconnected" {
			return nil, ErrDisconnected
		}
		return m, nil
	case <-tctx.Done():
		c.Forget(id)
		return nil, tctx.Err()
	}
}

// SendGoal отправляет цель действия без feedback; итог (action_result или {"op":"disconnected"})
// придёт в канал. Цель запоминается до отправки: обрыв в любой момент после этого оставит её в отмене.
// Во время CancelAllGoals ждёт её итога; затем valid (если не nil) решает, нужна ли ещё цель: цель задачи,
// которую остановили, пока шла отмена, не уходит (errWithdrawn).
func (c *Client) SendGoal(ctx context.Context, id, action, actionType string, args any, valid func() bool) (<-chan map[string]any, error) {
	c.goalMu.Lock()
	defer c.goalMu.Unlock()
	if valid != nil && !valid() {
		return nil, errWithdrawn
	}
	c.mu.Lock()
	c.goals[id] = action
	c.mu.Unlock()
	ch, err := c.request(ctx, nil, id, map[string]any{"op": "send_action_goal", "id": id, "action": action,
		"action_type": actionType, "args": args, "feedback": false})
	if err != nil {
		c.Forget(id)
	}
	return ch, err
}

// CancelAllGoals отменяет все цели действия сервисом <action>/_action/cancel_goal (action_msgs/srv/CancelGoal
// с нулевыми uuid и stamp — «отменить все») и ждёт ответа не дольше CancelTimeout. Отмена по id
// (cancel_action_goal) не годится: rosbridge знает цели только своего соединения, а цель могла остаться от
// прежнего. why — причина для OnCancel. Неподтверждённая отмена своего действия повторится при подключении.
func (c *Client) CancelAllGoals(ctx context.Context, action, why string) error {
	c.goalMu.Lock()
	defer c.goalMu.Unlock()
	c.mu.Lock()
	conn := c.conn
	c.mu.Unlock()
	return c.cancelAll(ctx, conn, action, why)
}

// cancelAll отменяет все цели действия по соединению conn. Подтверждение — ответ с result: true и
// return_code 0 (ERROR_NONE); ERROR_REJECTED и другие коды — отмена не состоялась. Отмена без ответа
// закрывает соединение: rosbridge вызывает сервисы в отдельных потоках (call_services_in_new_thread, по
// умолчанию true), и выполненная позже отмена сняла бы цель, отправленную после неё. Переподключение снова
// начнётся с отмены, а инструментам соединение откроется только после ответа на неё.
func (c *Client) cancelAll(ctx context.Context, conn *websocket.Conn, action, why string) error {
	t0 := time.Now()
	var resp map[string]any
	err := ErrDisconnected
	if conn != nil {
		resp, err = c.call(ctx, conn, action+"/_action/cancel_goal", cancelAllArgs, c.opts.CancelTimeout)
	}
	values := Obj(resp, "values")
	if code, _ := values["return_code"].(float64); err == nil && (resp["result"] != true || code != 0) {
		err = fmt.Errorf("rosbridge: %s/_action/cancel_goal: %v", action, resp["values"])
	}
	if unanswered(err) {
		conn.CloseNow()
	}
	goals, _ := values["goals_canceling"].([]any)
	c.mu.Lock()
	if err == nil {
		delete(c.orphans, action)
	} else if c.owned[action] {
		c.orphans[action] = true
	}
	c.mu.Unlock()
	if err != nil {
		c.log.Warn("rosbridge: goals not cancelled", "action", action, "why", why, "err", err)
	}
	if c.opts.OnCancel != nil {
		c.opts.OnCancel(Cancel{Action: action, Why: why, Goals: len(goals), Took: time.Since(t0), Err: err})
	}
	return err
}

// unanswered — запрос ушёл, а ответа нет (таймаут или отмена ctx): его ещё могут выполнить.
func unanswered(err error) bool {
	return errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled)
}

// cancelAllArgs — запрос action_msgs/srv/CancelGoal «отменить все цели»: нулевые goal_id и stamp.
var cancelAllArgs = map[string]any{"goal_info": map[string]any{
	"goal_id": map[string]any{"uuid": make([]int, 16)}, "stamp": map[string]any{"sec": 0, "nanosec": 0}}}

// ----------------------------------------------------------------------------------------------
// Учёт трафика

type kv struct{ N, Bytes int64 }

type stats struct {
	tcpRx, tcpTx, conns, dialFail atomic.Int64
	shortConns                    atomic.Int64
	mu                            sync.Mutex
	rx, tx                        map[string]*kv
	msgRx, msgTx, bRx, bTx        int64
}

func (s *stats) add(rx bool, key string, n int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	m := s.tx
	if rx {
		m = s.rx
		s.msgRx++
		s.bRx += int64(n)
	} else {
		s.msgTx++
		s.bTx += int64(n)
	}
	e := m[key]
	if e == nil {
		e = &kv{}
		m[key] = e
	}
	e.N++
	e.Bytes += int64(n)
}

// Stats — счётчики трафика.
type Stats struct {
	TCPRx, TCPTx int64 // байты TCP (кадры WebSocket + рукопожатие)
	Conns        int64 // успешных подключений
	ShortConns   int64 // из них оборвались раньше StableAfter
	DialFail     int64
	MsgRx, MsgTx int64 // сообщений протокола
	BytesRx      int64 // полезная нагрузка сообщений (JSON/CBOR), без кадров WS
	BytesTx      int64
	Rx, Tx       map[string][2]int64 // вид (op или "publish <тема>") -> [число, байты]
}

// Stats возвращает снимок счётчиков.
func (c *Client) Stats() Stats {
	s := &c.st
	out := Stats{TCPRx: s.tcpRx.Load(), TCPTx: s.tcpTx.Load(), Conns: s.conns.Load(), DialFail: s.dialFail.Load(),
		ShortConns: s.shortConns.Load(), Rx: map[string][2]int64{}, Tx: map[string][2]int64{}}
	s.mu.Lock()
	out.MsgRx, out.MsgTx, out.BytesRx, out.BytesTx = s.msgRx, s.msgTx, s.bRx, s.bTx
	for k, v := range s.rx {
		out.Rx[k] = [2]int64{v.N, v.Bytes}
	}
	for k, v := range s.tx {
		out.Tx[k] = [2]int64{v.N, v.Bytes}
	}
	s.mu.Unlock()
	return out
}

// Map — счётчики в виде для JSON (журнал, /control/state).
func (s Stats) Map() map[string]any {
	conv := func(m map[string][2]int64) map[string]any {
		out := make(map[string]any, len(m))
		for k, v := range m {
			out[k] = map[string]any{"n": v[0], "bytes": v[1]}
		}
		return out
	}
	return map[string]any{"tcp_rx": s.TCPRx, "tcp_tx": s.TCPTx, "conns": s.Conns, "short_conns": s.ShortConns, "dial_fail": s.DialFail,
		"msg_rx": s.MsgRx, "msg_tx": s.MsgTx, "bytes_rx": s.BytesRx, "bytes_tx": s.BytesTx,
		"rx": conv(s.Rx), "tx": conv(s.Tx)}
}

type countConn struct {
	net.Conn
	st *stats
}

func (c *countConn) Read(p []byte) (int, error) {
	n, err := c.Conn.Read(p)
	c.st.tcpRx.Add(int64(n))
	return n, err
}

func (c *countConn) Write(p []byte) (int, error) {
	n, err := c.Conn.Write(p)
	c.st.tcpTx.Add(int64(n))
	return n, err
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// Floats приводит числовой массив сообщения (JSON: []any с null для inf/NaN; CBOR: []float64) к []float64;
// null → +Inf (rosbridge JSON отдаёт inf как null).
func Floats(v any) ([]float64, error) {
	switch x := v.(type) {
	case []float64:
		return x, nil
	case []any:
		out := make([]float64, len(x))
		for i, e := range x {
			switch f := e.(type) {
			case float64:
				out[i] = f
			case nil:
				out[i] = posInf
			default:
				return nil, fmt.Errorf("element %d: %T", i, e)
			}
		}
		return out, nil
	}
	return nil, fmt.Errorf("not a numeric array: %T", v)
}
