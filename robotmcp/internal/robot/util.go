package robot

import (
	"math"

	"github.com/local/robotmcp/internal/canon"
)

// equalJSON — равенство значений состояния по канонической сериализации.
func equalJSON(a, b any) bool {
	ab, err1 := canon.Marshal(a)
	bb, err2 := canon.Marshal(b)
	return err1 == nil && err2 == nil && string(ab) == string(bb)
}

// shifted — поза сдвинулась заметно для AMCL (shiftM, shiftRad).
func shifted(a, b pose) bool {
	return dist(a.x, a.y, b.x, b.y) > shiftM || math.Abs(pyMod(a.yaw-b.yaw+math.Pi, 2*math.Pi)-math.Pi) > shiftRad
}
