package gate

import "sync"

// MapState — простое хранилище состояния (StateStore прототипа): последнее значение и время получения.
type MapState struct {
	mu sync.RWMutex
	v  map[string]stateRec
}

type stateRec struct {
	value any
	ts    float64
}

// NewMapState создаёт пустое хранилище.
func NewMapState() *MapState { return &MapState{v: map[string]stateRec{}} }

// Set записывает значение ключа и момент его получения.
func (s *MapState) Set(key string, value any, ts float64) {
	s.mu.Lock()
	s.v[key] = stateRec{value, ts}
	s.mu.Unlock()
}

// Get возвращает значение и время; ok=false — значения не было.
func (s *MapState) Get(key string) (any, float64, bool) {
	s.mu.RLock()
	r, ok := s.v[key]
	s.mu.RUnlock()
	return r.value, r.ts, ok
}
