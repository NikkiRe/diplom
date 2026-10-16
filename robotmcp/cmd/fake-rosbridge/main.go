// Команда fake-rosbridge — фейковый rosbridge (internal/fakebridge) отдельным процессом: прогон robot-mcp и
// robot-probe без симулятора (проверка обвязки, учёта трафика и уведомлений; числа стенда — только с VM sim).
//
//	fake-rosbridge -addr 127.0.0.1:18119 [-speed 20] [-paused]
package main

import (
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/local/robotmcp/internal/fakebridge"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:18119", "адрес WebSocket")
	speed := flag.Float64("speed", 20, "ускорение сим. времени")
	paused := flag.Bool("paused", false, "мир на паузе (датчики только после шага /world/default/control)")
	box := flag.String("box", "", "«ящик» в месте: door | window | … (цель туда прерывается)")
	flag.Parse()
	fb := fakebridge.Start(fakebridge.Options{Addr: *addr, Speed: *speed, Paused: *paused})
	places := map[string][2]float64{"door": {1.8, 0.5}, "window": {0.5, -1.7}, "corridor": {0, 1.8}}
	if p, ok := places[*box]; ok {
		fb.AddObstacle(fakebridge.Obstacle{X: p[0], Y: p[1], R: 0.2, Label: "box"})
	}
	fmt.Fprintln(os.Stderr, "fake-rosbridge:", fb.URL)
	ch := make(chan os.Signal, 1)
	signal.Notify(ch, os.Interrupt, syscall.SIGTERM)
	<-ch
	fb.Close()
}
