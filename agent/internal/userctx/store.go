// Package userctx — контекст пользователя в SQLite (чистый Go, modernc.org/sqlite):
// факты профиля (операции ADD/UPDATE/DELETE по образцу Mem0, без LLM-извлечения
// на каждом ходе), история ходов, сводка истории и правила-договорённости.
//
// Политики истории (спецификация §3):
//   - full           — вся история;
//   - window         — последние window_turns ходов;
//   - mask           — результаты инструментов старше mask_keep_turns ходов
//     заменяются заглушкой (observation masking), плюс окно window_turns, если задано;
//   - summary_system — старые ходы сжимаются в сводку (системное сообщение), после
//     сводки — ходы целиком; когда сжимать, решает агент по порогу оплаченных токенов.
package userctx

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"

	_ "modernc.org/sqlite" // драйвер "sqlite" на чистом Go (CGO_ENABLED=0)

	"github.com/local/edgectx/internal/config"
	"github.com/local/edgectx/internal/llm"
)

// MaskedText — заглушка вместо скрытого результата инструмента.
const MaskedText = "[результат скрыт]"

// Fact — факт профиля.
type Fact struct {
	ID       string  `json:"id"`
	Text     string  `json:"text"`
	CreatedT float64 `json:"created_t"`
	UpdatedT float64 `json:"updated_t"`
	order    int64
}

// Turn — завершённый ход агента.
type Turn struct {
	Seq      int64         `json:"seq"`
	InputID  string        `json:"input_id"`
	Kind     string        `json:"kind"` // user | gate | heartbeat | rule
	T        float64       `json:"t"`
	Messages []llm.Message `json:"messages"`
}

// RuleRecord — сохранённое правило (спецификация правила — JSON пакета gate).
type RuleRecord struct {
	ID       string
	Spec     json.RawMessage
	Source   string
	CreatedT float64
}

// Storage — параметры SQLite. Умолчания рассчитаны на SD-карту платы: журнал
// WAL с synchronous=NORMAL не делает fsync на каждой фиксации (только на
// контрольной точке), но база остаётся согласованной при сбое питания —
// теряются лишь последние фиксации (документация SQLite: «Write-Ahead
// Logging», PRAGMA synchronous). Вне WAL допустим только FULL.
type Storage struct {
	CacheKiB    int    // предел кэша страниц, КиБ (PRAGMA cache_size = −CacheKiB)
	JournalMode string // wal | delete | truncate | persist
	Synchronous string // normal | full
}

// StorageOf — параметры хранилища из конфигурации.
func StorageOf(u config.UserCtx) Storage {
	return Storage{CacheKiB: u.CacheKiB, JournalMode: u.JournalMode, Synchronous: u.Synchronous}
}

// Policy — политика выдачи истории.
type Policy struct {
	Kind          string // full | window | mask | summary_system
	WindowTurns   int
	MaskKeepTurns int
}

// Ошибки операций с фактами.
var (
	ErrFactExists   = errors.New("факт с таким id уже есть (используйте UPDATE)")
	ErrFactNotFound = errors.New("факта с таким id нет")
)

// Store — хранилище контекста пользователя. Потокобезопасно; нужные политике
// ходы кешируются в памяти, полный архив остаётся в SQLite.
type Store struct {
	db *sql.DB

	mu          sync.Mutex
	facts       map[string]Fact
	factOrder   int64
	turns       []Turn
	turnCount   int
	policy      Policy
	summary     string
	summaryUpto int64
}

const schema = `
CREATE TABLE IF NOT EXISTS facts(id TEXT PRIMARY KEY, text TEXT NOT NULL, created_t REAL, updated_t REAL, ord INTEGER);
CREATE TABLE IF NOT EXISTS turns(seq INTEGER PRIMARY KEY AUTOINCREMENT, input_id TEXT, kind TEXT, t REAL, messages TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS summary(id INTEGER PRIMARY KEY CHECK (id = 1), text TEXT NOT NULL, upto_seq INTEGER NOT NULL, t REAL);
CREATE TABLE IF NOT EXISTS rules(id TEXT PRIMARY KEY, spec TEXT NOT NULL, source TEXT, created_t REAL);
`

// Open открывает (создаёт) базу. path ":memory:" — база в памяти. reset очищает
// все таблицы (воспроизводимый старт прогона).
func Open(path string, reset bool) (*Store, error) {
	return OpenWithPolicy(path, reset, Policy{Kind: "full"}, StorageOf(config.Default().UserCtx))
}

// OpenWithPolicy открывает базу с кешем выбранной политики истории. Для window
// и mask с окном кеш содержит только хвост, для summary_system — только ходы
// после сводки. Политики full и mask без окна намеренно держат всю историю.
// View следует вызывать с той же политикой; Open сохраняет прежний полный кеш.
// Параметры st применяются к файлу базы (у ":memory:" всё и так в памяти).
func OpenWithPolicy(path string, reset bool, policy Policy, st Storage) (*Store, error) {
	dsn := path
	if path != ":memory:" {
		if dir := dirOf(path); dir != "" {
			if err := os.MkdirAll(dir, 0o755); err != nil {
				return nil, err
			}
		}
		dsn = fmt.Sprintf("file:%s?_pragma=busy_timeout(5000)&_pragma=journal_mode(%s)&_pragma=synchronous(%s)"+
			"&_pragma=cache_size(-%d)", path, st.JournalMode, st.Synchronous, st.CacheKiB)
	}
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	// Одно соединение: для ":memory:" каждое соединение — отдельная база, а
	// нагрузка агента (единицы записей в минуту) параллелизма не требует.
	db.SetMaxOpenConns(1)
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("userctx: schema: %w", err)
	}
	if reset {
		for _, tbl := range []string{"facts", "turns", "summary", "rules"} {
			if _, err := db.Exec("DELETE FROM " + tbl); err != nil {
				db.Close()
				return nil, err
			}
		}
		db.Exec("DELETE FROM sqlite_sequence WHERE name = 'turns'")
	}
	s := &Store{db: db, facts: map[string]Fact{}, policy: policy}
	if err := s.load(); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

func dirOf(p string) string {
	i := strings.LastIndex(p, "/")
	if i <= 0 {
		return ""
	}
	return p[:i]
}

func (s *Store) load() error {
	rows, err := s.db.Query("SELECT id, text, created_t, updated_t, ord FROM facts")
	if err != nil {
		return err
	}
	for rows.Next() {
		var f Fact
		if err := rows.Scan(&f.ID, &f.Text, &f.CreatedT, &f.UpdatedT, &f.order); err != nil {
			rows.Close()
			return err
		}
		s.facts[f.ID] = f
		if f.order > s.factOrder {
			s.factOrder = f.order
		}
	}
	rows.Close()

	err = s.db.QueryRow("SELECT text, upto_seq FROM summary WHERE id = 1").Scan(&s.summary, &s.summaryUpto)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	if err := s.db.QueryRow("SELECT COUNT(*) FROM turns").Scan(&s.turnCount); err != nil {
		return err
	}
	query := "SELECT seq, input_id, kind, t, messages FROM turns"
	var args []any
	if s.policy.Kind == "summary_system" {
		query += " WHERE seq > ? ORDER BY seq"
		args = []any{s.summaryUpto}
	} else if s.windowLimit() > 0 {
		query = "SELECT seq, input_id, kind, t, messages FROM (" + query + " ORDER BY seq DESC LIMIT ?) ORDER BY seq"
		args = []any{s.windowLimit()}
	} else {
		query += " ORDER BY seq"
	}
	rows, err = s.db.Query(query, args...)
	if err != nil {
		return err
	}
	for rows.Next() {
		var t Turn
		var msgs string
		if err := rows.Scan(&t.Seq, &t.InputID, &t.Kind, &t.T, &msgs); err != nil {
			rows.Close()
			return err
		}
		if err := json.Unmarshal([]byte(msgs), &t.Messages); err != nil {
			rows.Close()
			return fmt.Errorf("userctx: turn %d: %w", t.Seq, err)
		}
		s.turns = append(s.turns, t)
	}
	err = rows.Err()
	rows.Close()
	return err
}

func (s *Store) windowLimit() int {
	if s.policy.Kind == "window" || s.policy.Kind == "mask" {
		return s.policy.WindowTurns
	}
	return 0
}

func (s *Store) pruneTurnsLocked() {
	start := 0
	if s.policy.Kind == "summary_system" {
		for start < len(s.turns) && s.turns[start].Seq <= s.summaryUpto {
			start++
		}
	} else if n := s.windowLimit(); n > 0 && len(s.turns) > n {
		start = len(s.turns) - n
	}
	if start > 0 {
		// Копия освобождает ссылки на сообщения, вышедшие за пределы кеша.
		s.turns = append([]Turn(nil), s.turns[start:]...)
	}
}

// Close закрывает базу.
func (s *Store) Close() error { return s.db.Close() }

// ------------------------------------------------------------------- факты

// SeedFacts добавляет начальные факты профиля, которых ещё нет.
func (s *Store) SeedFacts(facts []config.Fact, t float64) error {
	for _, f := range facts {
		s.mu.Lock()
		_, ok := s.facts[f.ID]
		s.mu.Unlock()
		if ok {
			continue
		}
		if _, err := s.AddFact(f.ID, f.Text, t); err != nil {
			return err
		}
	}
	return nil
}

// AddFact добавляет факт; пустой id назначается (f1, f2, …).
func (s *Store) AddFact(id, text string, t float64) (Fact, error) {
	text = strings.TrimSpace(text)
	if text == "" {
		return Fact{}, errors.New("пустой текст факта")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if id == "" {
		id = s.nextFactIDLocked()
	}
	if _, ok := s.facts[id]; ok {
		return Fact{}, ErrFactExists
	}
	s.factOrder++
	f := Fact{ID: id, Text: text, CreatedT: t, UpdatedT: t, order: s.factOrder}
	if _, err := s.db.Exec("INSERT INTO facts(id, text, created_t, updated_t, ord) VALUES(?,?,?,?,?)",
		f.ID, f.Text, f.CreatedT, f.UpdatedT, f.order); err != nil {
		return Fact{}, err
	}
	s.facts[id] = f
	return f, nil
}

func (s *Store) nextFactIDLocked() string {
	maxN := 0
	for id := range s.facts {
		if strings.HasPrefix(id, "f") {
			if n, err := strconv.Atoi(id[1:]); err == nil && n > maxN {
				maxN = n
			}
		}
	}
	return "f" + strconv.Itoa(maxN+1)
}

// UpdateFact заменяет текст факта.
func (s *Store) UpdateFact(id, text string, t float64) error {
	text = strings.TrimSpace(text)
	if text == "" {
		return errors.New("пустой текст факта")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	f, ok := s.facts[id]
	if !ok {
		return ErrFactNotFound
	}
	if _, err := s.db.Exec("UPDATE facts SET text = ?, updated_t = ? WHERE id = ?", text, t, id); err != nil {
		return err
	}
	f.Text, f.UpdatedT = text, t
	s.facts[id] = f
	return nil
}

// DeleteFact удаляет факт.
func (s *Store) DeleteFact(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.facts[id]; !ok {
		return ErrFactNotFound
	}
	if _, err := s.db.Exec("DELETE FROM facts WHERE id = ?", id); err != nil {
		return err
	}
	delete(s.facts, id)
	return nil
}

// Facts — факты в порядке добавления.
func (s *Store) Facts() []Fact {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Fact, 0, len(s.facts))
	for _, f := range s.facts {
		out = append(out, f)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].order < out[j].order })
	return out
}

// ProfileText — текстовая проекция профиля для промпта.
func (s *Store) ProfileText() string {
	var b strings.Builder
	for _, f := range s.Facts() {
		fmt.Fprintf(&b, "- %s: %s\n", f.ID, oneLine(f.Text))
	}
	return strings.TrimRight(b.String(), "\n")
}

// ExportMarkdown — профиль и правила в виде MEMORY.md (совместимость с
// claw-семейством).
func (s *Store) ExportMarkdown() string {
	var b strings.Builder
	b.WriteString("# MEMORY\n\n## Профиль\n\n")
	for _, f := range s.Facts() {
		fmt.Fprintf(&b, "- %s\n", oneLine(f.Text))
	}
	rules, _ := s.Rules()
	if len(rules) > 0 {
		b.WriteString("\n## Правила\n\n")
		for _, r := range rules {
			fmt.Fprintf(&b, "- `%s`: %s\n", r.ID, string(r.Spec))
		}
	}
	return b.String()
}

func oneLine(s string) string {
	return strings.Join(strings.Fields(s), " ")
}

// ----------------------------------------------------------------- история

// AppendTurn сохраняет ход и возвращает его номер.
func (s *Store) AppendTurn(t Turn) (int64, error) {
	msgs, err := json.Marshal(t.Messages)
	if err != nil {
		return 0, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	res, err := s.db.Exec("INSERT INTO turns(input_id, kind, t, messages) VALUES(?,?,?,?)", t.InputID, t.Kind, t.T, string(msgs))
	if err != nil {
		return 0, err
	}
	t.Seq, _ = res.LastInsertId()
	s.turns = append(s.turns, t)
	s.turnCount++
	s.pruneTurnsLocked()
	return t.Seq, nil
}

// Turns — копия ходов в оперативном кеше (полный архив остаётся в SQLite).
func (s *Store) Turns() []Turn {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]Turn(nil), s.turns...)
}

// TurnCount — число всех сохранённых ходов, включая вышедшие из кеша.
func (s *Store) TurnCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.turnCount
}

// Summary — текущая сводка и номер последнего вошедшего в неё хода.
func (s *Store) Summary() (string, int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.summary, s.summaryUpto
}

// SetSummary сохраняет сводку, покрывающую ходы с номером <= upto.
func (s *Store) SetSummary(text string, upto int64, t float64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, err := s.db.Exec(`INSERT INTO summary(id, text, upto_seq, t) VALUES(1,?,?,?)
		ON CONFLICT(id) DO UPDATE SET text = excluded.text, upto_seq = excluded.upto_seq, t = excluded.t`, text, upto, t); err != nil {
		return err
	}
	s.summary, s.summaryUpto = text, upto
	s.pruneTurnsLocked()
	return nil
}

// ToSummarize — ходы после сводки, кроме последних keep (кандидаты на сжатие).
func (s *Store) ToSummarize(keep int) []Turn {
	s.mu.Lock()
	defer s.mu.Unlock()
	var after []Turn
	for _, t := range s.turns {
		if t.Seq > s.summaryUpto {
			after = append(after, t)
		}
	}
	if keep < 0 {
		keep = 0
	}
	if len(after) <= keep {
		return nil
	}
	return append([]Turn(nil), after[:len(after)-keep]...)
}

// View возвращает историю по политике: сводку (для summary_system) и сообщения.
func (s *Store) View(p Policy) (string, []llm.Message) {
	s.mu.Lock()
	turns := s.turns
	summary, upto := s.summary, s.summaryUpto
	s.mu.Unlock()

	switch p.Kind {
	case "summary_system":
		var out []llm.Message
		for _, t := range turns {
			if t.Seq > upto {
				out = append(out, t.Messages...)
			}
		}
		return summary, out
	case "window":
		return "", flatten(lastN(turns, p.WindowTurns), -1)
	case "mask":
		if p.WindowTurns > 0 {
			turns = lastN(turns, p.WindowTurns)
		}
		return "", flatten(turns, len(turns)-p.MaskKeepTurns)
	default: // full
		return "", flatten(turns, -1)
	}
}

func lastN(turns []Turn, n int) []Turn {
	if n <= 0 || len(turns) <= n {
		return turns
	}
	return turns[len(turns)-n:]
}

// flatten склеивает сообщения ходов; у ходов с индексом < maskBefore результаты
// инструментов заменяются заглушкой.
func flatten(turns []Turn, maskBefore int) []llm.Message {
	var out []llm.Message
	for i, t := range turns {
		for _, m := range t.Messages {
			if i < maskBefore && m.Role == "tool" {
				m.Content = MaskedText
			}
			out = append(out, m)
		}
	}
	return out
}

// ----------------------------------------------------------------- правила

// SaveRule сохраняет (заменяет) правило.
func (s *Store) SaveRule(id string, spec any, source string, t float64) error {
	b, err := json.Marshal(spec)
	if err != nil {
		return err
	}
	_, err = s.db.Exec(`INSERT INTO rules(id, spec, source, created_t) VALUES(?,?,?,?)
		ON CONFLICT(id) DO UPDATE SET spec = excluded.spec, source = excluded.source`, id, string(b), source, t)
	return err
}

// DeleteRule удаляет правило (отсутствие — не ошибка).
func (s *Store) DeleteRule(id string) error {
	_, err := s.db.Exec("DELETE FROM rules WHERE id = ?", id)
	return err
}

// Rules — сохранённые правила (по id).
func (s *Store) Rules() ([]RuleRecord, error) {
	rows, err := s.db.Query("SELECT id, spec, source, created_t FROM rules ORDER BY id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []RuleRecord
	for rows.Next() {
		var r RuleRecord
		var spec string
		var src sql.NullString
		var ct sql.NullFloat64
		if err := rows.Scan(&r.ID, &spec, &src, &ct); err != nil {
			return nil, err
		}
		r.Spec, r.Source, r.CreatedT = json.RawMessage(spec), src.String, ct.Float64
		out = append(out, r)
	}
	return out, rows.Err()
}
