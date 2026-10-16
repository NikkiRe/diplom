// Команда cpubench — CPU-бенчмарк хода агента для калибровки квоты CPU виртуальной платы (bench/board).
//
//	cpubench -duration 10s -workers 1
//
// Одна операция — работа агента с контекстом оборудования на каждом ходе: разбор JSON ответа MCP
// (encoding/json), каноническая сериализация (ключи отсортированы, без экранирования HTML), SHA-256
// канонической формы и поиск по регулярному выражению. Документ — синтетический инвентарь
// с состоянием и событиями (≈12 КБ), одинаковый на любой платформе: поле sha256 в выводе это подтверждает.
// Печатает одну строку JSON; главное — ops_per_s.
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"regexp"
	"runtime"
	"sync"
	"time"
)

// Устройства синтетического инвентаря: имя, тип, свойства.
var kinds = []struct {
	kind  string
	props []string
}{
	{"temp", []string{"value", "unit", "min", "max", "precision"}},
	{"humidity", []string{"value", "unit", "precision"}},
	{"motion", []string{"active", "since"}},
	{"door", []string{"open", "since"}},
	{"relay", []string{"on", "since", "lease_s"}},
	{"light", []string{"lux", "unit"}},
}

var pattern = regexp.MustCompile(`"(temp|humidity|motion|door|relay|light)_[0-9]+\.[a-z_]+":`)

type result struct {
	Ops          int64   `json:"ops"`
	Seconds      float64 `json:"seconds"`
	OpsPerSec    float64 `json:"ops_per_s"`
	Workers      int     `json:"workers"`
	PayloadBytes int     `json:"payload_bytes"`
	SHA256       string  `json:"sha256"`
	Go           string  `json:"go"`
	GOARCH       string  `json:"goarch"`
	GOMAXPROCS   int     `json:"gomaxprocs"`
}

// document строит документ из rooms×len(kinds) устройств; значения — детерминированные числа и строки.
func document(rooms int) []byte {
	inventory := []any{}
	state := map[string]any{}
	events := []any{}
	for r := 0; r < rooms; r++ {
		for i, k := range kinds {
			id := fmt.Sprintf("%s_%d", k.kind, r)
			props := map[string]any{}
			for j, p := range k.props {
				props[p] = map[string]any{"type": "number", "unit": "si", "readable": true, "writable": k.kind == "relay"}
				state[id+"."+p] = float64((r*31+i*7+j*3)%997) / 10
			}
			inventory = append(inventory, map[string]any{
				"id": id, "kind": k.kind, "room": fmt.Sprintf("комната <%d>", r),
				"description": "датчик & исполнительное устройство «" + k.kind + "»", "properties": props,
			})
			events = append(events, map[string]any{"t": float64(r*60 + i), "device": id, "property": k.props[0],
				"value": float64((r+i)%2) / 2})
		}
	}
	doc, err := json.Marshal(map[string]any{"state": state, "inventory": inventory, "events": events})
	if err != nil {
		panic(err) // документ из чисел и строк сериализуется всегда
	}
	return doc
}

// op — одна операция: разбор, каноническая сериализация, SHA-256, поиск ключей состояния.
func op(payload []byte, buf *bytes.Buffer) ([32]byte, int, error) {
	var doc map[string]any
	if err := json.Unmarshal(payload, &doc); err != nil {
		return [32]byte{}, 0, err
	}
	buf.Reset()
	enc := json.NewEncoder(buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(doc); err != nil {
		return [32]byte{}, 0, err
	}
	canon := bytes.TrimSuffix(buf.Bytes(), []byte("\n"))
	return sha256.Sum256(canon), len(pattern.FindAllIndex(canon, -1)), nil
}

// run выполняет операции в workers горутинах до истечения duration.
func run(payload []byte, duration time.Duration, workers int) (result, error) {
	var (
		wg    sync.WaitGroup
		mu    sync.Mutex
		total int64
		fail  error
	)
	start := time.Now()
	deadline := start.Add(duration)
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			var buf bytes.Buffer
			n := int64(0)
			var err error
			for time.Now().Before(deadline) {
				if _, _, err = op(payload, &buf); err != nil {
					break
				}
				n++
			}
			mu.Lock()
			total += n
			if err != nil && fail == nil {
				fail = err
			}
			mu.Unlock()
		}()
	}
	wg.Wait()
	elapsed := time.Since(start).Seconds()
	if fail != nil {
		return result{}, fail
	}
	var buf bytes.Buffer
	sum, _, err := op(payload, &buf)
	if err != nil {
		return result{}, err
	}
	return result{
		Ops: total, Seconds: elapsed, OpsPerSec: float64(total) / elapsed, Workers: workers,
		PayloadBytes: len(payload), SHA256: hex.EncodeToString(sum[:]),
		Go: runtime.Version(), GOARCH: runtime.GOARCH, GOMAXPROCS: runtime.GOMAXPROCS(0),
	}, nil
}

func main() {
	duration := flag.Duration("duration", 10*time.Second, "длительность замера")
	workers := flag.Int("workers", 1, "число параллельных горутин с операциями")
	flag.Parse()
	if *duration <= 0 || *workers < 1 || flag.NArg() > 0 {
		fmt.Fprintln(os.Stderr, "cpubench: нужны -duration > 0 и -workers ≥ 1, без позиционных аргументов")
		os.Exit(2)
	}
	res, err := run(document(4), *duration, *workers)
	if err != nil {
		fmt.Fprintln(os.Stderr, "cpubench:", err)
		os.Exit(1)
	}
	if err := json.NewEncoder(os.Stdout).Encode(res); err != nil {
		fmt.Fprintln(os.Stderr, "cpubench:", err)
		os.Exit(1)
	}
}
