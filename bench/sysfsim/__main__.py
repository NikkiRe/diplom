"""python -m bench.sysfsim {init,run,check,list-scenarios} — симулятор датчиков «живое sysfs». См. README.md."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from contextlib import ExitStack
from pathlib import Path

from . import scenario
from .gpio import FilesGpio, GpioError, GpioSim
from .runner import CgroupLoad, ClockServer, Runner, run_real, serve_virtual, wait
from .sensors import DEVICES
from .tree import Tree, atomic_write, init_root
from .world import World, summarize

DECLARED = Path(__file__).resolve().parent / "configs/declared.sim.json"
LINES = {d.line: d.name for d in DEVICES if d.line >= 0}   # линии GPIO платы: номер → имя
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)   # Ctrl+C, kill, оборванная сессия ssh


def _address(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError("ожидается хост:порт, например 127.0.0.1:0")
    return host, int(port)


def _positive(text: str) -> float:
    value = float(text)
    if not 0 < value < float("inf"):
        raise argparse.ArgumentTypeError("ожидается число > 0")
    return value


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m bench.sysfsim", description="Симулятор датчиков «живое sysfs».")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="создать корень sysfs по фикстуре hw-mcp")
    p.add_argument("--root", type=Path, required=True, help="новый или пустой каталог")
    p = sub.add_parser("run", help="вести датчики по сценарию")
    p.add_argument("--root", type=Path, required=True, help="корень, созданный init")
    p.add_argument("--scenario", default="dense", help="встроенный сценарий (list-scenarios) или путь к JSON")
    p.add_argument("--seed", type=int, default=1, help="seed шума, отказов и сигналов (по умолчанию 1)")
    p.add_argument("--truth", type=Path, help="журнал истины JSONL; файл не должен существовать")
    p.add_argument("--gpio", choices=("files", "gpiosim"), default="files", help="линии GPIO (по умолчанию files)")
    p.add_argument("--gpio-label", default="board-sim", help="метка чипа; для files — имя каталога в <root>/dev")
    p.add_argument("--declared", type=Path, default=DECLARED, help="declared.json для --declared-out")
    p.add_argument("--declared-out", type=Path, help="записать declared.json с именем созданного чипа у линий платы")
    p.add_argument("--ready-file", type=Path, help="записать JSON о готовности (pid, root, clock, gpio, url)")
    p.add_argument("--duration", type=_positive, help="секунды сценария до конца прогона (по умолчанию — весь)")
    p.add_argument("--clock", choices=("real", "virtual"), default="real", help="часы (по умолчанию real)")
    p.add_argument("--speed", type=_positive, default=1.0, help="секунд сценария на секунду (только real)")
    p.add_argument("--control-addr", type=_address, default=("127.0.0.1", 0), help="адрес POST /clock (virtual)")
    p.add_argument("--load-cgroup", type=Path, help="каталог cgroup v2 «платы»: нагрузка для температуры SoC")
    p = sub.add_parser("check", help="проверить сценарий и проиграть его без записи (реле выключено)")
    p.add_argument("--scenario", default="dense")
    p.add_argument("--seed", type=int, default=1)
    sub.add_parser("list-scenarios", help="встроенные сценарии")
    a = ap.parse_args(argv)
    if a.command == "run" and a.clock == "virtual" and (a.load_cgroup or a.speed != 1.0):
        ap.error("--load-cgroup и --speed — только с --clock real")
    return a


def resolve_declared(src: Path, dst: Path, chip: str) -> None:
    """declared.json, где у линий платы симулятора чип — созданный (gpiochipN у gpio-sim, метка у files):
    для hw-mcp, не знающего меток, и для меток, отличных от board-sim."""
    doc = json.loads(src.read_text(encoding="utf-8"))
    for line in doc.get("gpio", []):
        if line["id"] in LINES.values():
            line["chip"] = chip
    atomic_write(dst, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")


def _stop_channel() -> int:
    """Канал сигналов остановки; вернуть его читающий конец. Обработчик пуст, номер сигнала Python пишет в канал
    (signal.set_wakeup_fd), а прогон ждёт канал там, где и так ждёт: между сроками мира, между запросами часов,
    после конца сценария. Поэтому сигнал ничего не прерывает — ни запуск, ни шаг мира, ни уборку. Сигнал,
    игнорируемый с запуска, таким и остаётся: под nohup — SIGHUP, у фонового процесса неинтерактивной
    оболочки — SIGINT."""
    read, write = os.pipe()
    os.set_blocking(write, False)
    signal.set_wakeup_fd(write)
    for sig in STOP_SIGNALS:
        if signal.getsignal(sig) is not signal.SIG_IGN:
            signal.signal(sig, lambda signum, frame: None)
    return read


def init(a: argparse.Namespace) -> int:
    init_root(a.root)
    return 0


def run(a: argparse.Namespace) -> int:
    """Прогон до конца (у gpiosim — затем до сигнала) или до сигнала остановки, уборка, код 0; ошибка —
    исключение для main (код 1)."""
    stop = _stop_channel()
    sc = scenario.load(a.scenario)
    end = a.duration or sc.duration_s
    world = World(sc, a.seed, external_load=a.load_cgroup is not None)
    tree = Tree(a.root)
    load = CgroupLoad(a.load_cgroup) if a.load_cgroup else None
    with ExitStack() as stack:
        gpio = GpioSim(a.gpio_label, LINES) if a.gpio == "gpiosim" else FilesGpio(a.root / "dev", a.gpio_label)
        stack.callback(gpio.close)
        truth = stack.enter_context(a.truth.open("x", encoding="utf-8")) if a.truth else None
        runner = Runner(world, tree, gpio, truth, load)
        stack.callback(runner.close)
        ready = {"pid": os.getpid(), "root": str(a.root.resolve()), "clock": a.clock,
                 "gpio": {"backend": a.gpio, "label": a.gpio_label, "chip": gpio.chip}}
        runner.start({"format": "sysfsim-truth/1", "scenario": sc.name, "scenario_sha256": sc.sha256,
                      "seed": a.seed, "duration_s": end, "speed": a.speed,
                      "load_cgroup": str(a.load_cgroup) if a.load_cgroup else None} | ready)
        if a.declared_out:
            resolve_declared(a.declared, a.declared_out, gpio.chip)
        if a.clock == "virtual":
            server = stack.enter_context(ClockServer(a.control_addr, runner, end))
            print(f"sysfsim: виртуальные часы — POST {server.url}/clock", file=sys.stderr)
            ready["url"] = server.url
        if a.ready_file:
            atomic_write(a.ready_file, json.dumps(ready, ensure_ascii=False) + "\n")
        if a.clock == "virtual":
            serve_virtual(server, stop)
        else:
            run_real(runner, a.speed, end, stop)
        if a.gpio == "gpiosim" and not wait(stop, 0):   # чип снимают только после потребителей (README, «GPIO»)
            print("sysfsim: сценарий окончен; чип держится до SIGTERM — сначала остановите hw-mcp", file=sys.stderr)
            wait(stop)
    return 0


def check(a: argparse.Namespace) -> int:
    summary = summarize(scenario.load(a.scenario), a.seed)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    missing = [sid for sid, t in summary["situations"].items() if t is None]
    if missing:
        print(f"sysfsim: без реле не наступили ситуации: {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


def list_scenarios(a: argparse.Namespace) -> int:
    for name in scenario.builtin():
        sc = scenario.load(name)
        print(f"{name}\t{sc.duration_s:g} с\t{sc.description}")
    return 0


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    try:
        return {"init": init, "run": run, "check": check, "list-scenarios": list_scenarios}[a.command](a)
    except (ValueError, OSError, GpioError) as e:   # сценарий, declared.json, cgroup, файлы, чип GPIO
        print(f"sysfsim: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
