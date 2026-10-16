package robot

import (
	"math"
	"sort"
	"strconv"

	"github.com/local/robotmcp/internal/rosbridge"
)

// Scan — лидар, приведённый к точкам (дальность, пеленг в градусах: 0 — вперёд, + — влево).
type Scan struct {
	Pts    []ScanPt
	RMax   float64
	IncDeg float64
	T      float64 // время прихода по часам адаптера
	Raw    map[string]any
}

// ScanPt — отражение лидара.
type ScanPt struct{ R, Deg float64 }

// ParseScan разбирает sensor_msgs/LaserScan (JSON или CBOR) так же, как прототип robot_mcp_compact.py.
func ParseScan(m map[string]any, t float64) (*Scan, error) {
	ranges, err := rosbridge.Floats(m["ranges"])
	if err != nil {
		return nil, err
	}
	a0, _ := m["angle_min"].(float64)
	da, _ := m["angle_increment"].(float64)
	rmax, ok := m["range_max"].(float64)
	if !ok {
		rmax = 3.5
	}
	rmin, _ := m["range_min"].(float64)
	s := &Scan{RMax: rmax, IncDeg: da * 180 / math.Pi, T: t, Raw: m, Pts: make([]ScanPt, 0, len(ranges))}
	for i, r := range ranges {
		if math.IsNaN(r) || math.IsInf(r, 0) || r < rmin || r > rmax {
			continue
		}
		deg := pyMod((a0+float64(i)*da)*180/math.Pi+180, 360) - 180
		s.Pts = append(s.Pts, ScanPt{R: r, Deg: deg})
	}
	return s, nil
}

// pyMod — остаток со знаком делителя (как % в Python).
func pyMod(a, b float64) float64 {
	m := math.Mod(a, b)
	if m < 0 {
		m += b
	}
	return m
}

func round2(x float64) float64 { return math.RoundToEven(x*100) / 100 }

func fmtG(x float64) string { return strconv.FormatFloat(x, 'g', -1, 64) }

// sector — ближайшее отражение в секторе (м, 0,01) или ">rmax", если отражений нет.
func (s *Scan) sector(pred func(d float64) bool) any {
	best := math.Inf(1)
	for _, p := range s.Pts {
		if pred(p.Deg) && p.R < best {
			best = p.R
		}
	}
	if math.IsInf(best, 1) {
		return ">" + fmtG(s.RMax)
	}
	return round2(best)
}

// FreeM — свободное пространство по секторам: вперёд ±30°, влево 60…120°, вправо −120…−60°, назад ≥150°.
func (s *Scan) FreeM() map[string]any {
	return map[string]any{
		"front": s.sector(func(d float64) bool { return math.Abs(d) <= 30 }),
		"left":  s.sector(func(d float64) bool { return d >= 60 && d <= 120 }),
		"right": s.sector(func(d float64) bool { return d >= -120 && d <= -60 }),
		"back":  s.sector(func(d float64) bool { return math.Abs(d) >= 150 }),
	}
}

// Front — ближайшее отражение впереди (±30°), +Inf — нет.
func (s *Scan) Front() float64 {
	best := math.Inf(1)
	for _, p := range s.Pts {
		if math.Abs(p.Deg) <= 30 && p.R < best {
			best = p.R
		}
	}
	return best
}

// Nearest — ближайшее отражение {m, deg} (nil — нет отражений).
func (s *Scan) Nearest() map[string]any {
	if len(s.Pts) == 0 {
		return nil
	}
	pts := append([]ScanPt(nil), s.Pts...)
	sort.Slice(pts, func(i, j int) bool {
		if pts[i].R != pts[j].R {
			return pts[i].R < pts[j].R
		}
		return pts[i].Deg < pts[j].Deg
	})
	return map[string]any{"m": round2(pts[0].R), "deg": math.RoundToEven(pts[0].Deg)}
}

// InView — сводка лидара в поле зрения камеры (hfov): ближайшее отражение, сторона и доля лучей с
// отражением; как look() прототипа.
func (s *Scan) InView(hfovDeg float64) any {
	half := hfovDeg / 2
	var v []ScanPt
	for _, p := range s.Pts {
		if p.Deg >= -half && p.Deg <= half {
			v = append(v, p)
		}
	}
	if len(v) == 0 {
		return "clear >" + fmtG(s.RMax) + " m"
	}
	best := v[0]
	for _, p := range v[1:] {
		if p.R < best.R || p.R == best.R && p.Deg < best.Deg {
			best = p
		}
	}
	side := "center"
	if best.Deg > 10 {
		side = "left"
	} else if best.Deg < -10 {
		side = "right"
	}
	rays := math.Max(1, math.RoundToEven(2*half/s.IncDeg))
	// лучей в [−half, half] может быть на один больше rays (у прототипа тогда выходит 102 %): ограничено 100
	return map[string]any{"nearest_m": round2(best.R), "side": side,
		"occupied_pct": math.Min(100, math.RoundToEven(100*float64(len(v))/rays))}
}

// RawJSON — сообщение, пригодное для JSON (inf/NaN → null), для режима --payload raw.
func RawJSON(v any) any {
	switch x := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, e := range x {
			out[k] = RawJSON(e)
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = RawJSON(e)
		}
		return out
	case []float64:
		out := make([]any, len(x))
		for i, f := range x {
			if math.IsInf(f, 0) || math.IsNaN(f) {
				out[i] = nil
			} else {
				out[i] = f
			}
		}
		return out
	case float64:
		if math.IsInf(x, 0) || math.IsNaN(x) {
			return nil
		}
	case []byte:
		return len(x)
	}
	return v
}

// yawOf — курс по кватерниону.
func yawOf(q map[string]any) float64 {
	g := func(k string, d float64) float64 {
		if f, ok := q[k].(float64); ok {
			return f
		}
		return d
	}
	w, x, y, z := g("w", 1), g("x", 0), g("y", 0), g("z", 0)
	return math.Atan2(2*(w*z+x*y), 1-2*(y*y+z*z))
}
