#!/bin/bash
# Проверка бэкенда gpiosim симулятора bench/sysfsim в Linux (ВМ Lima sbc, Ubuntu 24.04 arm64), под root:
#  0) test_sysfsim_gpiosim.py на модуле ядра gpio-sim: линии, подтяжки, чип с занятой линией не снимается,
#     уборка после сбоя запуска; run --gpio gpiosim держит чип до сигнала и снимает его по сигналу;
#  1) ядро: чип с меткой и именами линий (gpioinfo); переходы входов видит независимый потребитель uAPI
#     (gpiomon, libgpiod); выход, выставленный gpioset, охлаждает кухню в симуляторе;
#  2) hw-mcp: корень sysfs симулятора и линии через /dev/gpiochipN (-gpio-driver cdev); фронты приходят
#     с подавлением дребезга ядром (debounce_ms из declared.sim.json), команда реле по MCP (stdio) охлаждает
#     кухню, показание датчика читается из дерева симулятора;
#  3) порядок остановки — сначала потребители линий, затем симулятор (SIGTERM; по концу сценария он держит
#     чип): симулятор выходит с кодом 0, устройства configfs нет, в журнале ядра нет новых WARNING/Oops,
#     флаги tainted не изменились; печатаются CPU и пиковый RSS симулятора.
# Ядро после Oops (флаг D в tainted) скрипт не проверяет — код 2: упавшая задача могла оставить захваченным
# мьютекс ядра, и запрос IRQ фронтов (gpiomon, hw-mcp) зависает в состоянии D до перезагрузки ВМ. Процессы
# ждутся с пределом: не вышедший процесс (состояние D) печатается со стеком ядра, а чип под ним остаётся.
# Инструменты libgpiod получают номер чипа, а не имя: по имени libgpiod 1.6 сначала ищет метку и открывает
# все чипы ВМ, в том числе чужие. Из корня проекта на Mac (каталог проекта смонтирован в ВМ только для чтения):
#   (cd hwmcp && GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -trimpath -o /tmp/hw-mcp-arm64 ./cmd/hw-mcp)
#   limactl copy /tmp/hw-mcp-arm64 sbc:work/sysfsim/hw-mcp
#   limactl shell sbc -- bash -c 'sudo bash bench/sysfsim/vm_check.sh ~/work/sysfsim/hw-mcp'
# Нужен пакет gpiod (libgpiod 1.6, ≈0,4 МБ). Создаёт и убирает за собой временные каталоги ВМ /tmp/sysfsim-check.*
# и /tmp/sysfsim-gpiosim-* (фаза 0) и устройства configfs gpio-sim sysfsim-<pid>; больше ничего не пишет.
set -euo pipefail
HW=${1:?использование: vm_check.sh <hw-mcp linux/arm64>}
cd "$(dirname "$0")/../.."
export PYTHONDONTWRITEBYTECODE=1
command -v gpiomon >/dev/null || { echo "нужен libgpiod: apt-get install gpiod" >&2; exit 2; }
TAINTED=$(cat /proc/sys/kernel/tainted)
if ((TAINTED & 128)); then   # TAINT_DIE, Documentation/admin-guide/tainted-kernels.rst
  echo "ядро ВМ уже пережило Oops (tainted $TAINTED, флаг D): запросы IRQ могут зависнуть — перезагрузите ВМ" >&2
  exit 2
fi
modprobe gpio-sim
W=$(mktemp -d /tmp/sysfsim-check.XXXXXX)
KMSG=$(dmesg | wc -l)
PIDS=()   # фоновые процессы в порядке запуска: симулятор, затем его потребители

reap() {   # ждать выхода фонового процесса $1 не дольше $2 с; код — его код выхода или 124, если не вышел
  local i
  for ((i = 0; i < $2 * 10; i++)); do
    kill -0 "$1" 2>/dev/null || { wait "$1"; return; }
    sleep 0.1
  done
  return 124
}

# SIGTERM фоновым процессам в обратном порядке запуска — потребителям линий раньше симулятора, который
# ждёт их до 5 с и занятый чип не снимает. Код — код выхода симулятора.
stop_all() {
  local i code=0
  for ((i = ${#PIDS[@]} - 1; i >= 0; i--)); do
    kill "${PIDS[i]}" 2>/dev/null || true
    code=0
    reap "${PIDS[i]}" 10 || code=$?
    if ((code == 124)); then
      echo "не вышел за 10 с:$(ps -o pid=,stat=,args= -p "${PIDS[i]}")" >&2
      sed 's/^/    /' "/proc/${PIDS[i]}/stack" >&2 || true
    fi
  done
  PIDS=()
  return "$code"
}
trap 'stop_all || true; rm -rf "$W"' EXIT
fail() { echo "FAIL $*" >&2; exit 1; }

# 20 с: кухня греется (быстрые постоянные времени, чтобы охлаждение было видно за секунды), дверь
# открыта 3–7 с с сильным дребезгом при закрытии, движение в прихожей с 4 с
cat > "$W/check.json" <<'JSON'
{"format": "sysfsim/1", "name": "vm-check", "duration_s": 20, "start": "12:00",
 "zones": {"kitchen": {"tau_s": 60, "fan_tau_s": 10}},
 "devices": {"pir_hall": {"false_per_h": 0}},
 "events": [{"t": 0, "kind": "heat", "zone": "kitchen", "rise_c": 25, "duration_s": 20},
            {"t": 3, "kind": "door", "device": "door_entry", "duration_s": 4},
            {"t": 4, "kind": "motion", "device": "pir_hall", "duration_s": 2}],
 "faults": [{"t": 6, "kind": "bounce", "device": "door_entry", "ms": 5, "pulses": 3, "duration_s": 2}]}
JSON

start_sim() {   # $1 — фаза; задаёт CHIP (номер чипа gpiochipN)
  python3 -m bench.sysfsim init --root "$W/$1"
  python3 -m bench.sysfsim run --root "$W/$1" --scenario "$W/check.json" --gpio gpiosim \
    --gpio-label "sysfsim-$1-$$" --truth "$W/$1.truth.jsonl" --ready-file "$W/$1.ready" \
    --declared-out "$W/$1.declared.json" &
  PIDS+=("$!")
  for _ in $(seq 200); do [ -f "$W/$1.ready" ] && break; sleep 0.05; done
  [ -f "$W/$1.ready" ] || fail "$1: симулятор не готов за 10 с"
  CHIP=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["gpio"]["chip"])' "$W/$1.ready")
  CHIP=${CHIP#gpiochip}
}

echo "== 0. test_sysfsim_gpiosim.py"
python3 -m unittest discover -s bench/tests -p test_sysfsim_gpiosim.py

echo "== 1. ядро: gpioinfo, gpiomon, gpioset"
start_sim kernel
gpioinfo "$CHIP" | grep -E '"(pir_hall|door_entry|relay_fan)"'
gpiomon -b -F '%o %e %s.%n' "$CHIP" 17 22 > "$W/kernel.edges" 2>/dev/null &
MON=$!
PIDS+=("$MON")
# gpiomon готов, когда занял обе линии и уснул в ожидании событий (S); завис в запросе IRQ — состояние D
mon_ready() { [ "$(gpioinfo "$CHIP" | grep -c '"gpiomon"')" = 2 ] && [[ $(ps -o stat= -p "$MON") == S* ]]; }
for _ in $(seq 30); do mon_ready && break; sleep 0.1; done
mon_ready || fail "gpiomon не занял линии 17 и 22 за 3 с:$(ps -o stat=,args= -p "$MON")"
sleep 10
gpioset --mode=time --sec=6 "$CHIP" 27=1
sleep 5   # конец сценария: симулятор держит чип до SIGTERM
stop_all || fail "kernel: симулятор вышел с кодом $?"

echo "== 2. hw-mcp: sysfs симулятора + /dev/gpiochip$CHIP (cdev)"
start_sim hwmcp
rpc() { printf '{"jsonrpc":"2.0","id":%s,"method":"tools/call","params":{"name":"%s","arguments":%s}}\n' "$@"; }
{
  echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},'\
'"clientInfo":{"name":"vm_check","version":"1"}}}'
  echo '{"jsonrpc":"2.0","method":"notifications/initialized"}'
  sleep 10
  rpc 2 read_property '{"device":"kitchen_bme280","property":"temperature"}'
  rpc 3 write_property '{"device":"relay_fan","property":"state","value":true}'
  sleep 6
  rpc 4 read_property '{"device":"kitchen_bme280","property":"temperature"}'
  sleep 1
} | "$HW" -root "$W/hwmcp" -declared "$W/hwmcp.declared.json" -gpio-driver cdev -trace "$W/hw.jsonl" > "$W/hw.out" &
HWP=$!
PIDS+=("$HWP")
reap "$HWP" 30 || fail "hwmcp: hw-mcp вышел с кодом $? (124 — не вышел за 30 с по концу ввода)"
PIDS=("${PIDS[0]}")   # hw-mcp вышел, линии свободны: остался симулятор
stop_all || fail "hwmcp: симулятор вышел с кодом $?"

echo "== 3. сверка"
python3 - "$W" "$KMSG" "$TAINTED" <<'PY'
import json, math, re, subprocess, sys
from pathlib import Path

w, kmsg, tainted, failed = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3], []


def check(ok, text):
    print(("ok   " if ok else "FAIL ") + text)
    if not ok:
        failed.append(text)


def load(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def num(x):   # нет числа — nan: сравнение с ним даёт FAIL, а не трассировку
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else math.nan


for phase in ("kernel", "hwmcp"):
    truth = load(w / f"{phase}.truth.jsonl")
    pid = json.loads((w / f"{phase}.ready").read_text())["pid"]
    check(not Path(f"/sys/kernel/config/gpio-sim/sysfsim-{pid}").exists(), f"{phase}: устройство configfs убрано")
    act = [(round(r["t"], 1), r["value"]) for r in truth if r["type"] == "actuator"]
    kitchen = [(r["t"], num(r["reading"])) for r in truth if r["type"] == "value"
               and r["device"] == "kitchen_bme280" and r["property"] == "temperature"]

    def kitchen_at(t):
        return next((v for tt, v in reversed(kitchen) if tt <= t), math.nan)
    on = next((t for t, v in act if v), math.nan)
    before, after = kitchen_at(on), kitchen_at(on + 5)
    check(after < before - 3, f"{phase}: реле включено в {on} с, кухня {before} → {after} °C за 5 с")
    end = truth[-1] if truth else {}
    check(end.get("type") == "run_end", f"{phase}: журнал истины закончен run_end")
    print(f"     симулятор: CPU {end.get('cpu_s')} с за {end.get('wall_s')} с, "
          f"пиковый RSS {end.get('max_rss_mib')} МиБ")
    gpio = [r for r in truth if r["type"] == "gpio" and not r.get("initial")]
    if phase == "kernel":
        seen = [tuple(map(int, line.split()[:2])) for line in (w / "kernel.edges").read_text().splitlines()]
        pir = [(r["line"], int(r["value"])) for r in gpio if r["line"] == 17]
        check(pir and [e for e in seen if e[0] == 17] == pir,
              f"gpiomon: PIR — {len(pir)} фронтов, как в журнале истины")
        # uAPI v1 (libgpiod 1.6) берёт тип фронта из уровня линии в потоке IRQ, а близкие импульсы дребезга
        # сливаются: событий двери не меньше основных фронтов и не больше переходов, последнее — итоговый уровень
        door = [v for line, v in seen if line == 22]
        moves = [r for r in gpio if r["line"] == 22]
        main = sum(not r["bounce"] for r in moves)
        check(0 < main <= len(door) <= len(moves) and door[-1:] == [int(r["value"]) for r in moves[-1:]],
              f"gpiomon: дверь — {len(door)} событий из {len(moves)} переходов ({len(moves) - main} — дребезг)")
        check([v for _, v in act] == [False, True, False], f"gpioset: реле {act}")
    else:
        hw = load(w / "hw.jsonl")
        backend = next((r["gpio_backend"] for r in hw if r["type"] == "ready"), "")
        check(backend.startswith("cdev"), f"hw-mcp: драйвер линий {backend}")
        notify = [r["device"] for r in hw if r["type"] == "notify" and r["device"] in ("door_entry", "pir_hall")]
        pir = sum(r["line"] == 17 for r in gpio)
        check(notify.count("door_entry") == 2, "hw-mcp: дверь — 2 уведомления, дребезг подавлен ядром")
        check(0 < pir == notify.count("pir_hall"), f"hw-mcp: PIR — {pir} уведомлений")
        check(not [r for r in hw if r["type"] == "sample_error"], "hw-mcp: датчики дерева читаются без ошибок")
        out = {r["id"]: json.loads(r["result"]["content"][0]["text"]) for r in load(w / "hw.out")
               if r.get("id", 1) > 1 and "result" in r}   # ответы tools/call; ошибка JSON-RPC — без result
        relay, t2, t4 = out.get(3, {}), num(out.get(2, {}).get("value")), num(out.get(4, {}).get("value"))
        check(relay.get("ok") is True, f"hw-mcp: команда реле {relay.get('verdict')}")
        check(t4 < t2 - 3, f"hw-mcp: кухня {t2} → {t4} °C")

kernel = subprocess.run(["dmesg"], capture_output=True, text=True, check=True).stdout.splitlines()[kmsg:]
bad = [line for line in kernel if re.search(r"WARNING|Oops|BUG|STILL REQUESTED|leaking", line)]
check(not bad, "ядро: нет новых WARNING/Oops" + "".join(f"\n       {line}" for line in bad[:5]))
check(Path("/proc/sys/kernel/tainted").read_text().strip() == tainted, f"ядро: tainted {tainted} не изменился")
sys.exit(1 if failed else 0)
PY
echo "vm_check: всё в порядке"
