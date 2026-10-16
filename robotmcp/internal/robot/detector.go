package robot

import (
	"sort"
	"sync"
)

// Object — объект в поле зрения: метка, расстояние, сторона (сводка детектора, 20–40 токенов вместо
// 96–486 vision-токенов кадра; robot_scenario.md §7.3).
type Object struct {
	Label string  `json:"label"`
	M     float64 `json:"m,omitempty"`
	Side  string  `json:"side,omitempty"`
}

// Detector — детектор объектов на плате или «умная камера» стенда. Сейчас есть только заглушки:
// None (детектора нет — look отдаёт сводку лидара) и Stub (объекты задаёт драйвер стенда через
// POST /control/detector — заменитель логической камеры Gazebo до её подключения).
type Detector interface {
	Name() string
	Detect(place string, frame []byte) []Object
}

// None — детектора нет.
type None struct{}

// Name — "none".
func (None) Name() string { return "none" }

// Detect — всегда пусто.
func (None) Detect(string, []byte) []Object { return nil }

// Stub — объекты по местам, заданные извне (истина мира от драйвера стенда или теста).
type Stub struct {
	mu      sync.Mutex
	byPlace map[string][]Object
}

// NewStub — пустая заглушка.
func NewStub() *Stub { return &Stub{byPlace: map[string][]Object{}} }

// Name — "stub".
func (s *Stub) Name() string { return "stub" }

// Set задаёт объекты места (пустой список — место пусто).
func (s *Stub) Set(place string, objs []Object) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(objs) == 0 {
		delete(s.byPlace, place)
		return
	}
	s.byPlace[place] = append([]Object(nil), objs...)
}

// Detect — объекты места.
func (s *Stub) Detect(place string, _ []byte) []Object {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := append([]Object(nil), s.byPlace[place]...)
	sort.Slice(out, func(i, j int) bool { return out[i].Label < out[j].Label })
	return out
}

func objectsJSON(objs []Object) []any {
	out := make([]any, 0, len(objs))
	for _, o := range objs {
		m := map[string]any{"label": o.Label}
		if o.M > 0 {
			m["m"] = round2(o.M)
		}
		if o.Side != "" {
			m["side"] = o.Side
		}
		out = append(out, m)
	}
	return out
}
