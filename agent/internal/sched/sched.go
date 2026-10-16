// Package sched — планировщик обращений к LLM.
//
// Задания (ходы агента) упорядочены по приоритету «пользователь > безопасность >
// фон», внутри приоритета — по времени постановки. Одновременно исполняется не
// больше MaxInFlight заданий (1–2 запроса к LLM в полёте). Суточный бюджет
// (вызовы и входные токены) переводит планировщик в деградированный режим:
//
//   - normal    — допускаются все задания;
//   - degraded  — израсходовано >= degrade_at лимита: фон отклоняется;
//   - exhausted — лимит исчерпан: отклоняются фон и безопасность (их берут на
//     себя локальные правила), пользователь обслуживается всегда.
//
// Решение о допуске принимается при извлечении задания из очереди, а сутки
// отсчитываются по Clock (полночь эпохи часов).
package sched

import (
	"context"
	"math"
	"sort"
	"sync"

	"github.com/local/edgectx/internal/activity"
	"github.com/local/edgectx/internal/clock"
	"github.com/local/edgectx/internal/config"
)

// Priority — приоритет задания (меньше — важнее).
type Priority int

const (
	User Priority = iota
	Safety
	Background
)

func (p Priority) String() string {
	switch p {
	case User:
		return "user"
	case Safety:
		return "safety"
	default:
		return "background"
	}
}

// ParsePriority — из строки safety|background|user.
func ParsePriority(s string) Priority {
	switch s {
	case "user":
		return User
	case "safety":
		return Safety
	default:
		return Background
	}
}

// Level — режим бюджета.
type Level int

const (
	Normal Level = iota
	Degraded
	Exhausted
)

func (l Level) String() string {
	return [...]string{"normal", "degraded", "exhausted"}[l]
}

// Причины отклонения задания.
const (
	ReasonBudgetDegraded  = "budget_degraded"
	ReasonBudgetExhausted = "budget_exhausted"
	ReasonQueueFull       = "queue_full"
	ReasonShutdown        = "shutdown"
)

// Job — задание: один ход агента.
type Job struct {
	ID       string
	Priority Priority
	// Run исполняет ход; вызывается в горутине исполнителя.
	Run func(ctx context.Context)
	// Reject вызывается вместо Run, если задание отклонено.
	Reject func(reason string)

	seq uint64
}

// Options — параметры планировщика.
type Options struct {
	MaxInFlight int
	QueueMax    int
	Budget      config.Budget
	Clock       clock.Clock
	Tracker     *activity.Tracker
}

// Usage — расход бюджета за текущие сутки.
type Usage struct {
	Day         int     `json:"day"`
	Calls       int     `json:"calls"`
	InputTokens int     `json:"input_tokens"`
	Fraction    float64 `json:"fraction"`
	Level       string  `json:"level"`
}

// Scheduler — планировщик. Создаётся New, запускается Start.
type Scheduler struct {
	opt Options

	mu      sync.Mutex
	cond    *sync.Cond
	queue   []*Job
	seq     uint64
	running int
	stopped bool
	wg      sync.WaitGroup

	day         int
	calls       int
	inputTokens int
	stats       map[string]int
}

// New создаёт планировщик.
func New(opt Options) *Scheduler {
	if opt.MaxInFlight <= 0 {
		opt.MaxInFlight = 1
	}
	if opt.QueueMax <= 0 {
		opt.QueueMax = 64
	}
	if opt.Tracker == nil {
		opt.Tracker = &activity.Tracker{}
	}
	s := &Scheduler{opt: opt, stats: map[string]int{}}
	s.cond = sync.NewCond(&s.mu)
	s.day = s.today()
	return s
}

// Start запускает исполнителей; ctx передаётся в Run заданий.
func (s *Scheduler) Start(ctx context.Context) {
	for i := 0; i < s.opt.MaxInFlight; i++ {
		s.wg.Add(1)
		go s.worker(ctx)
	}
}

// Submit ставит задание в очередь. Никогда не блокируется.
func (s *Scheduler) Submit(j *Job) {
	s.opt.Tracker.Begin()
	s.mu.Lock()
	if s.stopped {
		s.mu.Unlock()
		s.reject(j, ReasonShutdown)
		return
	}
	s.seq++
	j.seq = s.seq
	var evicted *Job
	if len(s.queue) >= s.opt.QueueMax {
		// Вытесняется худшее по приоритету и самое старое задание; если новое не
		// лучше всех в очереди — отклоняется оно само.
		worst := s.worstIndex()
		if s.queue[worst].Priority > j.Priority {
			evicted = s.queue[worst]
			s.queue = append(s.queue[:worst], s.queue[worst+1:]...)
		} else {
			s.mu.Unlock()
			s.reject(j, ReasonQueueFull)
			return
		}
	}
	s.queue = append(s.queue, j)
	s.stats["submitted"]++
	s.cond.Signal()
	s.mu.Unlock()
	if evicted != nil {
		s.reject(evicted, ReasonQueueFull)
	}
}

func (s *Scheduler) worstIndex() int {
	w := 0
	for i, q := range s.queue {
		if q.Priority > s.queue[w].Priority || (q.Priority == s.queue[w].Priority && q.seq < s.queue[w].seq) {
			w = i
		}
	}
	return w
}

func (s *Scheduler) worker(ctx context.Context) {
	defer s.wg.Done()
	for {
		s.mu.Lock()
		for len(s.queue) == 0 && !s.stopped {
			s.cond.Wait()
		}
		if len(s.queue) == 0 && s.stopped {
			s.mu.Unlock()
			return
		}
		sort.SliceStable(s.queue, func(a, b int) bool {
			if s.queue[a].Priority != s.queue[b].Priority {
				return s.queue[a].Priority < s.queue[b].Priority
			}
			return s.queue[a].seq < s.queue[b].seq
		})
		j := s.queue[0]
		s.queue = s.queue[1:]
		if s.stopped {
			s.mu.Unlock()
			s.reject(j, ReasonShutdown)
			continue
		}
		reason := s.admitLocked(j.Priority)
		if reason == "" {
			s.running++
			s.stats["admitted"]++
			s.stats["admitted_"+j.Priority.String()]++
		}
		s.mu.Unlock()

		if reason != "" {
			s.reject(j, reason)
			continue
		}
		func() {
			defer func() {
				s.mu.Lock()
				s.running--
				s.mu.Unlock()
				s.opt.Tracker.End()
			}()
			if j.Run != nil {
				j.Run(ctx)
			}
		}()
	}
}

func (s *Scheduler) reject(j *Job, reason string) {
	s.mu.Lock()
	s.stats["rejected_"+reason]++
	s.mu.Unlock()
	defer s.opt.Tracker.End()
	if j.Reject != nil {
		j.Reject(reason)
	}
}

// admitLocked возвращает причину отклонения или "".
func (s *Scheduler) admitLocked(p Priority) string {
	if p == User {
		return ""
	}
	switch s.levelLocked() {
	case Degraded:
		if p == Background {
			return ReasonBudgetDegraded
		}
	case Exhausted:
		return ReasonBudgetExhausted
	}
	return ""
}

func (s *Scheduler) today() int {
	if s.opt.Clock == nil {
		return 0
	}
	return int(math.Floor(clock.Seconds(s.opt.Clock) / 86400))
}

func (s *Scheduler) rollDayLocked() {
	if d := s.today(); d != s.day {
		s.day, s.calls, s.inputTokens = d, 0, 0
	}
}

// fractionLocked — наибольшая из долей израсходованных лимитов.
func (s *Scheduler) fractionLocked() float64 {
	s.rollDayLocked()
	f := 0.0
	if b := s.opt.Budget.DailyCalls; b > 0 {
		f = math.Max(f, float64(s.calls)/float64(b))
	}
	if b := s.opt.Budget.DailyInputTokens; b > 0 {
		f = math.Max(f, float64(s.inputTokens)/float64(b))
	}
	return f
}

func (s *Scheduler) levelLocked() Level {
	f := s.fractionLocked()
	switch {
	case f >= 1:
		return Exhausted
	case f >= s.opt.Budget.DegradeAt && s.opt.Budget.DegradeAt > 0:
		return Degraded
	}
	return Normal
}

// Charge учитывает обращение к LLM (вызовы и оплаченные входные токены).
func (s *Scheduler) Charge(calls, inputTokens int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.rollDayLocked()
	s.calls += calls
	s.inputTokens += inputTokens
}

// Level — текущий режим бюджета.
func (s *Scheduler) Level() Level {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.levelLocked()
}

// Usage — расход за текущие сутки.
func (s *Scheduler) Usage() Usage {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.fractionLocked()
	return Usage{Day: s.day, Calls: s.calls, InputTokens: s.inputTokens,
		Fraction: math.Round(f*1e4) / 1e4, Level: s.levelLocked().String()}
}

// Queued — длина очереди; Running — число исполняемых заданий.
func (s *Scheduler) Queued() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.queue)
}

func (s *Scheduler) Running() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.running
}

// Stats — счётчики планировщика.
func (s *Scheduler) Stats() map[string]int {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make(map[string]int, len(s.stats))
	for k, v := range s.stats {
		out[k] = v
	}
	return out
}

// Stop отклоняет ожидающие задания (shutdown) и ждёт завершения исполняемых.
func (s *Scheduler) Stop() {
	s.mu.Lock()
	s.stopped = true
	pending := s.queue
	s.queue = nil
	s.cond.Broadcast()
	s.mu.Unlock()
	for _, j := range pending {
		s.reject(j, ReasonShutdown)
	}
	s.wg.Wait()
}
