// Package backoff — экспоненциальные паузы с детерминированным джиттером.
//
// Пауза n-й попытки (n ≥ 1): d = min(Max, Base·2^(n−1)), затем «равный джиттер»
// d/2 + u·d/2 (M. Brooker, «Exponential Backoff And Jitter», AWS Architecture
// Blog, 2015): пауза не короче половины d и не синхронизирует клиентов,
// повторяющих попытки одновременно. Случайное u берётся из хеша (зерно, номер),
// а не из глобального генератора, поэтому при одном зерне (run_id) паузы
// воспроизводятся — в том числе на виртуальных часах.
package backoff

import (
	"hash/fnv"
	"time"
)

// Policy — параметры паузы.
type Policy struct {
	Base time.Duration // пауза первой попытки до джиттера
	Max  time.Duration // верхняя граница до джиттера (0 — без границы)
}

// Delay — пауза n-й попытки (n < 1 считается как 1) при джиттере u ∈ [0, 1).
func (p Policy) Delay(n int, u float64) time.Duration {
	d := p.Base
	for i := 1; i < n && (p.Max <= 0 || d < p.Max); i++ {
		d *= 2
	}
	if p.Max > 0 && d > p.Max {
		d = p.Max
	}
	return d/2 + time.Duration(u*float64(d/2))
}

// Seed — зерно джиттера из строки (FNV-1a), например из run_id.
func Seed(s string) uint64 {
	h := fnv.New64a()
	h.Write([]byte(s))
	return h.Sum64()
}

// Unit — псевдослучайное число в [0, 1), однозначно определённое парой
// (seed, n): перемешивание splitmix64 (S. Vigna).
func Unit(seed, n uint64) float64 {
	z := seed + (n+1)*0x9e3779b97f4a7c15
	z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9
	z = (z ^ (z >> 27)) * 0x94d049bb133111eb
	z ^= z >> 31
	return float64(z>>11) / (1 << 53)
}
