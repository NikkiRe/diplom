// Package config — конфигурация edgectx в YAML (спецификация §3).
//
// Ключи верхнего уровня: run_id, clock, listen, llm, mcp, policy, budget, sched,
// trace, trace_flush_s, userctx, prompt. Отсутствующие значения заполняются умолчаниями
// (Default), затем конфигурация проверяется (Validate). Относительные пути
// (trace, userctx.db, policy.gate.rules_path, команда stdio-сервера вида ./x или
// ../x) отсчитываются от каталога файла конфигурации.
package config

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/local/edgectx/internal/canon"
)

// Допустимые значения перечислений.
var (
	ClockModes      = []string{"real", "virtual"}
	WakePolicies    = []string{"heartbeat", "gate", "hybrid"}
	HistoryPolicies = []string{"full", "window", "mask", "summary_system"}
	HWCtxModes      = []string{"snapshot_head", "delta_tail", "pull"}
	Catalogs        = []string{"explicit", "generic"}
	Transports      = []string{"stdio", "http"}
	JournalModes    = []string{"wal", "delete", "truncate", "persist"}
	SyncModes       = []string{"normal", "full"}
	SignalKinds     = []string{"change", "threshold", "delta", "episode", "ignore"}
	Priorities      = []string{"safety", "background"}
	// LocalToolNames — встроенные инструменты агента (не MCP).
	LocalToolNames = []string{"notify_user", "create_rule", "delete_rule", "update_profile",
		"recent_events", "agent_status"}
	// DefaultLocalTools — встроенные инструменты, которые видит LLM по умолчанию.
	DefaultLocalTools = []string{"notify_user", "create_rule", "delete_rule", "update_profile", "recent_events"}
)

var (
	// HistoryNoopModes — что делать с пустым фоновым ходом (ответ noop_reply без
	// инструментов и уведомлений): keep — хранить; skip_heartbeat — не хранить
	// плановые проверки (в них нет ничего, кроме времени тика); skip — не хранить и
	// пустые ходы по событиям (события остаются в журнале L2, recent_events).
	HistoryNoopModes = []string{"keep", "skip_heartbeat", "skip"}
	// ReasoningEfforts, Verbosities — значения llm.extra.reasoning_effort и
	// llm.extra.verbosity (документация OpenAI «Reasoning models», 20.10.2026).
	ReasoningEfforts = []string{"none", "minimal", "low", "medium", "high", "xhigh"}
	Verbosities      = []string{"low", "medium", "high"}
)

// Config — полная конфигурация агента.
type Config struct {
	RunID       string      `yaml:"run_id" json:"run_id"`
	Clock       Clock       `yaml:"clock" json:"clock"`
	Listen      string      `yaml:"listen" json:"listen"`
	LLM         LLM         `yaml:"llm" json:"llm"`
	MCP         []MCPServer `yaml:"mcp" json:"mcp"`
	Policy      Policy      `yaml:"policy" json:"policy"`
	Budget      Budget      `yaml:"budget" json:"budget"`
	Sched       Sched       `yaml:"sched" json:"sched"`
	Trace       string      `yaml:"trace" json:"trace"`
	TraceFlushS float64     `yaml:"trace_flush_s" json:"trace_flush_s"` // период сброса буфера трассы, с (0 — каждая запись)
	UserCtx     UserCtx     `yaml:"userctx" json:"userctx"`
	Prompt      Prompt      `yaml:"prompt" json:"prompt"`

	// BaseDir — каталог файла конфигурации (для относительных путей); не из YAML.
	BaseDir string `yaml:"-" json:"-"`
}

// Clock — часы. В YAML допускается краткая форма `clock: virtual`.
type Clock struct {
	Mode string `yaml:"mode" json:"mode"`
	// Epoch — полночь дня прогона в RFC 3339 (например 2026-10-06T00:00:00+03:00).
	// Пусто: для virtual — 2026-10-06T00:00:00 по местному времени, для real —
	// полночь текущего дня.
	Epoch string `yaml:"epoch" json:"epoch"`
	// StartT — начальное время виртуальных часов, секунды от эпохи (например
	// 25200 = 07:00, начало окна дня).
	StartT float64 `yaml:"start_t" json:"start_t"`
	// SettleTimeoutS — сколько настенных секунд /v1/clock/advance ждёт затишья
	// агента после каждого таймера (защита от зависания прогона).
	SettleTimeoutS float64 `yaml:"settle_timeout_s" json:"settle_timeout_s"`
}

// UnmarshalYAML принимает и скаляр (`clock: virtual`), и отображение.
func (c *Clock) UnmarshalYAML(n *yaml.Node) error {
	if n.Kind == yaml.ScalarNode {
		c.Mode = n.Value
		return nil
	}
	type plain Clock
	var p plain
	if err := n.Decode(&p); err != nil {
		return err
	}
	*c = Clock(p)
	return nil
}

// LLM — внешний OpenAI-совместимый endpoint.
type LLM struct {
	BaseURL   string `yaml:"base_url" json:"base_url"`
	Model     string `yaml:"model" json:"model"`
	APIKeyEnv string `yaml:"api_key_env" json:"api_key_env"`
	// ConnectTimeoutS — предел установления TCP-соединения и TLS-рукопожатия.
	ConnectTimeoutS float64 `yaml:"connect_timeout_s" json:"connect_timeout_s"`
	// TimeoutS — предел одной попытки запроса целиком.
	TimeoutS float64 `yaml:"timeout_s" json:"timeout_s"`
	// MaxTokens — ограничение длины ответа (0 — не передавать).
	MaxTokens int `yaml:"max_tokens" json:"max_tokens"`
	// Retries — число повторов после сетевой ошибки до ответа, 429 и 5xx.
	Retries int `yaml:"retries" json:"retries"`
	// RetryBackoffMs, RetryBackoffMaxMs — первая пауза и предел паузы между
	// попытками (до джиттера); Retry-After длиннее предела ждёт агент.
	RetryBackoffMs    int `yaml:"retry_backoff_ms" json:"retry_backoff_ms"`
	RetryBackoffMaxMs int `yaml:"retry_backoff_max_ms" json:"retry_backoff_max_ms"`
	// Breaker — предохранитель на случай недоступности модели.
	Breaker Breaker `yaml:"breaker" json:"breaker"`
	// Extra — дополнительные поля тела запроса (например закрепление endpoint'а:
	// provider: {only: [openai], allow_fallbacks: false}, уровень рассуждения
	// reasoning_effort, verbosity, service_tier). Поля неизменны весь прогон:
	// reasoning_effort и verbosity входят в ключ кэша провайдера.
	Extra map[string]any `yaml:"extra" json:"extra"`
	// TraceHeaders — передавать заголовки X-Edgectx-* (run, req, wake, входы);
	// нужно mock-llm в режиме oracle, для реального провайдера не требуется.
	TraceHeaders bool `yaml:"trace_headers" json:"trace_headers"`
}

// Breaker — предохранитель: после Failures временных сбоев подряд модель
// считается недоступной, связь проверяет проба GET {base_url}/{probe_path}
// через ProbeS·2^(k−1) с (k — сбоев подряд), не реже чем раз в ProbeMaxS.
// Удачная проба допускает пробный ход; счётчик обнуляет только ответ модели.
type Breaker struct {
	Failures  int     `yaml:"failures" json:"failures"`
	ProbeS    float64 `yaml:"probe_s" json:"probe_s"`
	ProbeMaxS float64 `yaml:"probe_max_s" json:"probe_max_s"`
	ProbePath string  `yaml:"probe_path" json:"probe_path"`
}

// MCPServer — подключение к MCP-серверу.
type MCPServer struct {
	Name      string `yaml:"name" json:"name"`
	Transport string `yaml:"transport" json:"transport"`
	// Cmd — команда stdio-сервера: строка (делится по пробелам, кавычки
	// поддерживаются) или список. Подстановка {catalog} — значение policy.catalog.
	Cmd StringList        `yaml:"cmd" json:"cmd"`
	Env map[string]string `yaml:"env" json:"env"`
	// URL — endpoint streamable HTTP; подстановка {catalog} поддерживается.
	URL string `yaml:"url" json:"url"`
	// Subscribe — шаблоны URI ресурсов для подписки («*» — любая подстрока,
	// «?» — один символ; пусто — все ресурсы, кроме инвентаря).
	Subscribe []string `yaml:"subscribe" json:"subscribe"`
	// InventoryURI — ресурс инвентаря (пусто — первый ресурс, чей URI оканчивается
	// на "inventory", иначе инструмент list_devices).
	InventoryURI string  `yaml:"inventory_uri" json:"inventory_uri"`
	TimeoutS     float64 `yaml:"timeout_s" json:"timeout_s"`
	// ReconnectS, ReconnectMaxS — первая и наибольшая пауза (до джиттера)
	// между попытками переподключения упавшего сервера.
	ReconnectS    float64 `yaml:"reconnect_s" json:"reconnect_s"`
	ReconnectMaxS float64 `yaml:"reconnect_max_s" json:"reconnect_max_s"`
}

// StringList — список строк, в YAML допускается одна строка.
type StringList []string

// UnmarshalYAML разбирает строку как командную строку или принимает список.
func (s *StringList) UnmarshalYAML(n *yaml.Node) error {
	if n.Kind == yaml.ScalarNode {
		parts, err := SplitCommand(n.Value)
		if err != nil {
			return err
		}
		*s = parts
		return nil
	}
	var l []string
	if err := n.Decode(&l); err != nil {
		return err
	}
	*s = l
	return nil
}

// Policy — политики агента (факторы экспериментов).
type Policy struct {
	Wake         string  `yaml:"wake" json:"wake"`
	HeartbeatMin float64 `yaml:"heartbeat_min" json:"heartbeat_min"`
	// HeartbeatTask — содержательная задача наблюдения на тике heartbeat.
	HeartbeatTask string `yaml:"heartbeat_task" json:"heartbeat_task"`
	// EventTask — инструкция к фоновому ходу по событиям гейта.
	EventTask string `yaml:"event_task" json:"event_task"`
	MaxSteps  int    `yaml:"max_steps" json:"max_steps"`
	Gate      Gate   `yaml:"gate" json:"gate"`

	History     string `yaml:"history" json:"history"`
	WindowTurns int    `yaml:"window_turns" json:"window_turns"`
	// MaskKeepTurns — у скольких последних ходов результаты инструментов не
	// скрываются (политика mask).
	MaskKeepTurns int `yaml:"mask_keep_turns" json:"mask_keep_turns"`
	// SummaryThresholdTokens — порог сжатия истории (summary_system) в оплаченных
	// входных токенах последнего запроса (usage.prompt_tokens).
	SummaryThresholdTokens int `yaml:"summary_threshold_tokens" json:"summary_threshold_tokens"`
	// HistoryBackground — сохранять ли в историю фоновые ходы (гейт, heartbeat).
	HistoryBackground *bool `yaml:"history_background" json:"history_background"`
	// HistoryBatchTurns — шаг вытеснения истории B: окно и маска сдвигаются сразу
	// на B ходов (1 — на каждом ходе, как до 20.10.2026).
	HistoryBatchTurns int `yaml:"history_batch_turns" json:"history_batch_turns"`
	// HistoryNoop — пустые фоновые ходы в истории: keep | skip_heartbeat | skip.
	HistoryNoop string `yaml:"history_noop" json:"history_noop"`

	HWCtx           string  `yaml:"hwctx" json:"hwctx"`
	SnapshotPeriodS float64 `yaml:"snapshot_period_s" json:"snapshot_period_s"`
	Catalog         string  `yaml:"catalog" json:"catalog"`

	// LocalTools — встроенные инструменты, доступные LLM (nil — по умолчанию).
	LocalTools []string `yaml:"local_tools" json:"local_tools"`
	// NoopReply — ответ LLM «ничего делать не нужно» на фоновом ходе.
	NoopReply           string  `yaml:"noop_reply" json:"noop_reply"`
	RecentEventsK       int     `yaml:"recent_events_k" json:"recent_events_k"`
	ProfileConsolidateS float64 `yaml:"profile_consolidate_s" json:"profile_consolidate_s"`
	MaxToolResultChars  int     `yaml:"max_tool_result_chars" json:"max_tool_result_chars"`
}

// Gate — событийный гейт.
type Gate struct {
	DebounceS float64 `yaml:"debounce_s" json:"debounce_s"`
	CooldownS float64 `yaml:"cooldown_s" json:"cooldown_s"`
	CoalesceS float64 `yaml:"coalesce_s" json:"coalesce_s"`
	// CoalesceMax — предел событий в окне коалесценции: окно закрывается досрочно.
	CoalesceMax int `yaml:"coalesce_max" json:"coalesce_max"`
	// SelfEchoS — сколько секунд после успешной команды агента изменения этого
	// устройства считаются эхом команды (self_caused) и не будят LLM; 0 — выкл.
	SelfEchoS float64 `yaml:"self_echo_s" json:"self_echo_s"`
	RulesPath string  `yaml:"rules_path" json:"rules_path"`
	// RulesEnabled — исполнять ли локальные правила (и давать ли LLM create_rule).
	RulesEnabled *bool `yaml:"rules_enabled" json:"rules_enabled"`
	// Default — вид сигнала для свойств, не описанных в Signals: change | ignore.
	Default string   `yaml:"default" json:"default"`
	Signals []Signal `yaml:"signals" json:"signals"`
}

// Signal — правило значимости для свойства устройства.
type Signal struct {
	// Device, Property — шаблоны path.Match ("" или "*" — любое).
	Device   string `yaml:"device" json:"device"`
	Property string `yaml:"property" json:"property"`
	// Kind: change — значимо любое подтверждённое изменение; threshold — пересечение
	// порога Above/Below с гистерезисом; delta — изменение не меньше Delta от
	// последнего значимого значения; episode — «конец эпизода» после GapS секунд
	// тишины; ignore — не будит LLM.
	Kind       string   `yaml:"kind" json:"kind"`
	Above      *float64 `yaml:"above" json:"above,omitempty"`
	Below      *float64 `yaml:"below" json:"below,omitempty"`
	Hysteresis float64  `yaml:"hysteresis" json:"hysteresis"`
	Delta      float64  `yaml:"delta" json:"delta"`
	GapS       float64  `yaml:"gap_s" json:"gap_s"`
	// Priority: safety | background (по умолчанию background).
	Priority string `yaml:"priority" json:"priority"`
	// DebounceS, CooldownS — переопределения общих значений гейта.
	DebounceS *float64 `yaml:"debounce_s" json:"debounce_s,omitempty"`
	CooldownS *float64 `yaml:"cooldown_s" json:"cooldown_s,omitempty"`
}

// Budget — суточные лимиты (0 — без ограничения).
type Budget struct {
	DailyCalls       int `yaml:"daily_calls" json:"daily_calls"`
	DailyInputTokens int `yaml:"daily_input_tokens" json:"daily_input_tokens"`
	// DegradeAt — доля лимита, после которой фон отключается.
	DegradeAt float64 `yaml:"degrade_at" json:"degrade_at"`
}

// Sched — планировщик обращений к LLM.
type Sched struct {
	MaxInFlight int `yaml:"max_in_flight" json:"max_in_flight"`
	QueueMax    int `yaml:"queue_max" json:"queue_max"`
	// DeferredMax — предел пробуждений, отложенных до восстановления связи с LLM.
	DeferredMax int `yaml:"deferred_max" json:"deferred_max"`
}

// UserCtx — контекст пользователя (SQLite).
type UserCtx struct {
	DB string `yaml:"db" json:"db"`
	// Reset — очистить базу при старте (для воспроизводимых прогонов).
	Reset bool `yaml:"reset" json:"reset"`
	// Profile — начальные факты профиля (добавляются, если их ещё нет).
	Profile []Fact `yaml:"profile" json:"profile"`
	// CacheKiB — предел кэша страниц SQLite, КиБ.
	CacheKiB int `yaml:"cache_kib" json:"cache_kib"`
	// JournalMode, Synchronous — режим журнала и синхронизации SQLite.
	JournalMode string `yaml:"journal_mode" json:"journal_mode"`
	Synchronous string `yaml:"synchronous" json:"synchronous"`
}

// Fact — факт профиля пользователя.
type Fact struct {
	ID   string `yaml:"id" json:"id"`
	Text string `yaml:"text" json:"text"`
}

// Prompt — тексты промпта.
type Prompt struct {
	// System — системный промпт (пусто — встроенный).
	System string `yaml:"system" json:"system"`
}

// Default возвращает конфигурацию по умолчанию.
func Default() *Config {
	t := true
	return &Config{
		Clock:  Clock{Mode: "real", SettleTimeoutS: 120},
		Listen: "127.0.0.1:18080",
		LLM: LLM{BaseURL: "http://127.0.0.1:18100/v1", Model: "openai/gpt-5-mini", ConnectTimeoutS: 10,
			TimeoutS: 60, Retries: 2, RetryBackoffMs: 500, RetryBackoffMaxMs: 8000,
			Breaker: Breaker{Failures: 1, ProbeS: 30, ProbeMaxS: 300, ProbePath: "models"}},
		Policy: Policy{
			Wake: "hybrid", HeartbeatMin: 30, MaxSteps: 6,
			Gate: Gate{DebounceS: 2, CooldownS: 300, CoalesceS: 10, CoalesceMax: 32, SelfEchoS: 10, RulesEnabled: &t,
				Default: "change"},
			History:       "mask",
			WindowTurns:   8,
			MaskKeepTurns: 2, SummaryThresholdTokens: 4000, HistoryBackground: &t,
			HistoryBatchTurns: 1, HistoryNoop: "keep",
			HWCtx: "delta_tail", SnapshotPeriodS: 60, Catalog: "generic",
			NoopReply: "NO_ACTION", RecentEventsK: 10, ProfileConsolidateS: 600, MaxToolResultChars: 4000,
		},
		Budget:      Budget{DegradeAt: 0.8},
		Sched:       Sched{MaxInFlight: 1, QueueMax: 64, DeferredMax: 32},
		TraceFlushS: 2,
		UserCtx:     UserCtx{DB: "edgectx.db", CacheKiB: 256, JournalMode: "wal", Synchronous: "normal"},
	}
}

// Load читает и проверяет файл конфигурации.
func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	c, err := Parse(data)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	abs, err := filepath.Abs(filepath.Dir(path))
	if err != nil {
		return nil, err
	}
	c.BaseDir = abs
	return c, nil
}

// Parse разбирает YAML поверх умолчаний и проверяет результат.
func Parse(data []byte) (*Config, error) {
	c := Default()
	dec := yaml.NewDecoder(strings.NewReader(string(data)))
	dec.KnownFields(true)                                            // опечатка в ключе — ошибка, а не тихое умолчание
	if err := dec.Decode(c); err != nil && !errors.Is(err, io.EOF) { // пустой файл допустим
		return nil, err
	}
	c.fillDefaults()
	if err := c.Validate(); err != nil {
		return nil, err
	}
	return c, nil
}

func (c *Config) fillDefaults() {
	d := Default()
	if c.Clock.Mode == "" {
		c.Clock.Mode = d.Clock.Mode
	}
	if c.Clock.SettleTimeoutS <= 0 {
		c.Clock.SettleTimeoutS = d.Clock.SettleTimeoutS
	}
	if c.LLM.ConnectTimeoutS <= 0 {
		c.LLM.ConnectTimeoutS = d.LLM.ConnectTimeoutS
	}
	if c.LLM.TimeoutS <= 0 {
		c.LLM.TimeoutS = d.LLM.TimeoutS
	}
	if c.LLM.Retries < 0 {
		c.LLM.Retries = 0
	}
	if c.LLM.RetryBackoffMs <= 0 {
		c.LLM.RetryBackoffMs = d.LLM.RetryBackoffMs
	}
	if c.LLM.RetryBackoffMaxMs <= 0 {
		c.LLM.RetryBackoffMaxMs = d.LLM.RetryBackoffMaxMs
	}
	if c.LLM.Breaker.Failures <= 0 {
		c.LLM.Breaker.Failures = d.LLM.Breaker.Failures
	}
	if c.LLM.Breaker.ProbeS <= 0 {
		c.LLM.Breaker.ProbeS = d.LLM.Breaker.ProbeS
	}
	if c.LLM.Breaker.ProbeMaxS <= 0 {
		c.LLM.Breaker.ProbeMaxS = d.LLM.Breaker.ProbeMaxS
	}
	if c.LLM.Breaker.ProbePath == "" {
		c.LLM.Breaker.ProbePath = d.LLM.Breaker.ProbePath
	}
	if c.Policy.Gate.RulesEnabled == nil {
		c.Policy.Gate.RulesEnabled = d.Policy.Gate.RulesEnabled
	}
	if c.Policy.Gate.Default == "" {
		c.Policy.Gate.Default = "change"
	}
	if c.Policy.Gate.CoalesceMax <= 0 {
		c.Policy.Gate.CoalesceMax = d.Policy.Gate.CoalesceMax
	}
	if c.Policy.HistoryBackground == nil {
		c.Policy.HistoryBackground = d.Policy.HistoryBackground
	}
	if c.Policy.HistoryBatchTurns <= 0 {
		c.Policy.HistoryBatchTurns = d.Policy.HistoryBatchTurns
	}
	if c.Policy.HistoryNoop == "" {
		c.Policy.HistoryNoop = d.Policy.HistoryNoop
	}
	if c.Policy.MaxSteps <= 0 {
		c.Policy.MaxSteps = d.Policy.MaxSteps
	}
	if c.Policy.SnapshotPeriodS < 0 {
		c.Policy.SnapshotPeriodS = 0
	}
	if c.Policy.NoopReply == "" {
		c.Policy.NoopReply = d.Policy.NoopReply
	}
	if c.Policy.RecentEventsK <= 0 {
		c.Policy.RecentEventsK = d.Policy.RecentEventsK
	}
	if c.Policy.MaxToolResultChars <= 0 {
		c.Policy.MaxToolResultChars = d.Policy.MaxToolResultChars
	}
	if c.Policy.LocalTools == nil {
		c.Policy.LocalTools = append([]string(nil), DefaultLocalTools...)
	}
	if c.Budget.DegradeAt <= 0 || c.Budget.DegradeAt > 1 {
		c.Budget.DegradeAt = d.Budget.DegradeAt
	}
	if c.Sched.MaxInFlight <= 0 {
		c.Sched.MaxInFlight = 1
	}
	if c.Sched.QueueMax <= 0 {
		c.Sched.QueueMax = d.Sched.QueueMax
	}
	if c.Sched.DeferredMax <= 0 {
		c.Sched.DeferredMax = d.Sched.DeferredMax
	}
	if c.TraceFlushS < 0 {
		c.TraceFlushS = 0
	}
	if c.UserCtx.CacheKiB <= 0 {
		c.UserCtx.CacheKiB = d.UserCtx.CacheKiB
	}
	if c.UserCtx.JournalMode == "" {
		c.UserCtx.JournalMode = d.UserCtx.JournalMode
	}
	if c.UserCtx.Synchronous == "" {
		c.UserCtx.Synchronous = d.UserCtx.Synchronous
	}
	for i := range c.MCP {
		s := &c.MCP[i]
		if s.TimeoutS <= 0 {
			s.TimeoutS = 30
		}
		if s.ReconnectS <= 0 {
			s.ReconnectS = 1
		}
		if s.ReconnectMaxS <= 0 {
			s.ReconnectMaxS = 60
		}
	}
}

// Validate проверяет допустимость значений.
func (c *Config) Validate() error {
	var errs []string
	add := func(format string, a ...any) { errs = append(errs, fmt.Sprintf(format, a...)) }
	oneOf := func(field, v string, allowed []string) {
		for _, a := range allowed {
			if v == a {
				return
			}
		}
		add("%s: %q не из %v", field, v, allowed)
	}
	oneOf("clock", c.Clock.Mode, ClockModes)
	if c.Clock.Epoch != "" {
		if _, err := time.Parse(time.RFC3339, c.Clock.Epoch); err != nil {
			add("clock.epoch: %v", err)
		}
	}
	if c.Clock.StartT < 0 {
		add("clock.start_t < 0")
	}
	if c.Clock.Mode == "real" && c.Clock.StartT != 0 {
		add("clock.start_t задаётся только для virtual")
	}
	if c.LLM.BaseURL == "" {
		add("llm.base_url пуст")
	}
	if c.LLM.Model == "" {
		add("llm.model пуст")
	}
	if c.LLM.RetryBackoffMaxMs < c.LLM.RetryBackoffMs {
		add("llm.retry_backoff_max_ms < retry_backoff_ms")
	}
	if c.LLM.Breaker.ProbeMaxS < c.LLM.Breaker.ProbeS {
		add("llm.breaker.probe_max_s < probe_s")
	}
	for _, msg := range checkReasoning(c.LLM.Model, c.LLM.Extra) {
		add("%s", msg)
	}
	names := map[string]bool{}
	for i, s := range c.MCP {
		if s.Name == "" {
			add("mcp[%d].name пуст", i)
		} else if names[s.Name] {
			add("mcp[%d].name %q повторяется", i, s.Name)
		}
		names[s.Name] = true
		oneOf(fmt.Sprintf("mcp[%d].transport", i), s.Transport, Transports)
		if s.Transport == "stdio" && len(s.Cmd) == 0 {
			add("mcp[%d] (%s): для stdio нужен cmd", i, s.Name)
		}
		if s.Transport == "http" && s.URL == "" {
			add("mcp[%d] (%s): для http нужен url", i, s.Name)
		}
		if s.ReconnectMaxS < s.ReconnectS {
			add("mcp[%d] (%s): reconnect_max_s < reconnect_s", i, s.Name)
		}
	}
	p := c.Policy
	oneOf("policy.wake", p.Wake, WakePolicies)
	if (p.Wake == "heartbeat" || p.Wake == "hybrid") && p.HeartbeatMin <= 0 {
		add("policy.heartbeat_min должен быть > 0 для wake=%s", p.Wake)
	}
	oneOf("policy.history", p.History, HistoryPolicies)
	if p.WindowTurns < 0 || p.MaskKeepTurns < 0 {
		add("policy.window_turns/mask_keep_turns < 0")
	}
	if p.History == "window" && p.WindowTurns <= 0 {
		add("policy.window_turns должен быть > 0 для history=window")
	}
	oneOf("policy.history_noop", p.HistoryNoop, HistoryNoopModes)
	oneOf("policy.hwctx", p.HWCtx, HWCtxModes)
	oneOf("policy.catalog", p.Catalog, Catalogs)
	for _, tname := range p.LocalTools {
		oneOf("policy.local_tools", tname, LocalToolNames)
	}
	g := p.Gate
	if g.DebounceS < 0 || g.CooldownS < 0 || g.CoalesceS < 0 || g.SelfEchoS < 0 {
		add("policy.gate: отрицательные интервалы")
	}
	oneOf("policy.gate.default", g.Default, []string{"change", "ignore"})
	for i, s := range g.Signals {
		f := fmt.Sprintf("policy.gate.signals[%d]", i)
		oneOf(f+".kind", s.Kind, SignalKinds)
		if s.Priority != "" {
			oneOf(f+".priority", s.Priority, Priorities)
		}
		if s.Kind == "threshold" && s.Above == nil && s.Below == nil {
			add("%s: для threshold нужен above или below", f)
		}
		if s.Kind == "delta" && s.Delta <= 0 {
			add("%s: для delta нужен delta > 0", f)
		}
		if s.Kind == "episode" && s.GapS <= 0 {
			add("%s: для episode нужен gap_s > 0", f)
		}
		if s.Hysteresis < 0 {
			add("%s: hysteresis < 0", f)
		}
	}
	if c.Budget.DailyCalls < 0 || c.Budget.DailyInputTokens < 0 {
		add("budget: отрицательные лимиты")
	}
	if c.Sched.MaxInFlight > 2 {
		add("sched.max_in_flight: допускается 1–2 запроса в полёте (спецификация), задано %d", c.Sched.MaxInFlight)
	}
	for i, f := range c.UserCtx.Profile {
		if f.ID == "" || f.Text == "" {
			add("userctx.profile[%d]: нужны id и text", i)
		}
	}
	oneOf("userctx.journal_mode", c.UserCtx.JournalMode, JournalModes)
	oneOf("userctx.synchronous", c.UserCtx.Synchronous, SyncModes)
	if c.UserCtx.JournalMode != "wal" && c.UserCtx.Synchronous != "full" {
		// Вне WAL режим normal при сбое питания может испортить базу (документация SQLite, PRAGMA synchronous).
		add("userctx.synchronous: при journal_mode=%s допустим только full", c.UserCtx.JournalMode)
	}
	if len(errs) > 0 {
		return errors.New("config: " + strings.Join(errs, "; "))
	}
	return nil
}

// RulesEnabled — исполняются ли локальные правила.
func (c *Config) RulesEnabled() bool {
	return c.Policy.Gate.RulesEnabled == nil || *c.Policy.Gate.RulesEnabled
}

// HistoryBackground — сохраняются ли фоновые ходы в историю.
func (c *Config) HistoryBackground() bool {
	return c.Policy.HistoryBackground == nil || *c.Policy.HistoryBackground
}

// Hash — SHA-256 канонического JSON конфигурации (после умолчаний). Ключ API в
// конфигурации не хранится (только имя переменной окружения), поэтому хеш его
// не раскрывает.
func (c *Config) Hash() string {
	sum := sha256.Sum256([]byte(canon.String(c)))
	return hex.EncodeToString(sum[:])
}

// EpochTime — эпоха часов.
func (c *Config) EpochTime() time.Time {
	if c.Clock.Epoch != "" {
		t, err := time.Parse(time.RFC3339, c.Clock.Epoch)
		if err == nil {
			return t
		}
	}
	if c.Clock.Mode == "virtual" {
		return time.Date(2026, 10, 6, 0, 0, 0, 0, time.Local)
	}
	return time.Time{} // реальные часы: полночь текущего дня
}

// Seconds переводит секунды из конфигурации в time.Duration.
func Seconds(s float64) time.Duration { return time.Duration(s * float64(time.Second)) }

// Path разрешает относительный путь от каталога конфигурации.
func (c *Config) Path(p string) string {
	if p == "" || p == "-" || p == ":memory:" || filepath.IsAbs(p) || c.BaseDir == "" {
		return p
	}
	return filepath.Join(c.BaseDir, p)
}

// Expand возвращает копию описания сервера с подстановкой {catalog} и
// разрешёнными относительными путями команды.
func (c *Config) Expand(s MCPServer) MCPServer {
	out := s
	out.URL = strings.ReplaceAll(s.URL, "{catalog}", c.Policy.Catalog)
	out.Cmd = make(StringList, len(s.Cmd))
	for i, a := range s.Cmd {
		a = strings.ReplaceAll(a, "{catalog}", c.Policy.Catalog)
		if i == 0 && (strings.HasPrefix(a, "./") || strings.HasPrefix(a, "../")) {
			a = c.Path(a)
		}
		out.Cmd[i] = a
	}
	return out
}

// SplitCommand делит командную строку по пробелам с учётом одинарных и двойных
// кавычек и обратной косой черты (без подстановок оболочки).
func SplitCommand(s string) ([]string, error) {
	var (
		out     []string
		cur     strings.Builder
		inWord  bool
		quote   rune
		escaped bool
	)
	for _, r := range s {
		switch {
		case escaped:
			cur.WriteRune(r)
			escaped = false
		case r == '\\' && quote != '\'':
			escaped, inWord = true, true
		case quote != 0:
			if r == quote {
				quote = 0
			} else {
				cur.WriteRune(r)
			}
		case r == '"' || r == '\'':
			quote, inWord = r, true
		case r == ' ' || r == '\t' || r == '\n':
			if inWord {
				out = append(out, cur.String())
				cur.Reset()
				inWord = false
			}
		default:
			cur.WriteRune(r)
			inWord = true
		}
	}
	if quote != 0 || escaped {
		return nil, fmt.Errorf("незакрытая кавычка или экранирование в %q", s)
	}
	if inWord {
		out = append(out, cur.String())
	}
	return out, nil
}
