#!/bin/bash
# Проверка драйвера GPIO hw-mcp на настоящем ядре Linux (ВМ Lima "sbc", Ubuntu 24.04 arm64) с модулем
# ядра gpio-sim (Documentation/admin-guide/gpio/gpio-sim.rst). Для uAPI v1 и v2:
#   - internal/gpio: чтение входа (в т.ч. active_low), события фронтов, подавление дребезга (v2 — ядро,
#     v1 — драйвер hw-mcp), запись выхода с проверкой уровня через sim_gpio<K>/value, разрешение чипа по
#     метке и отказ при неоднозначной метке;
#   - hw-mcp целиком в смешанном режиме (sysfs — копия фикстуры, GPIO — этот чип по метке, stdio):
#     read_property входа, уведомление по фронту, write_property выхода, safe_state при выходе.
# Запуск с Mac: tools/vm_gpiosim_check.sh   (собирает статические бинарники linux/arm64 в .build)
# Чипы получают уникальные метки hwmcp-chk-<pid> и удаляются при выходе (trap); другие чипы не трогаются.
#
# Порядок уборки важен: ядро 6.8 обращается к освобождённой памяти (Oops в lineevent_release -> gpiod_free),
# если чип gpio-sim снят, пока линию держит запрос. Поэтому тесты идут в своей группе процессов, уборка
# завершает её целиком (тест и запущенный им hw-mcp) и дожидается выхода, затем проверяет по debugfs, что
# линии чипа никем не запрошены, и только потом снимает чип; иначе чип остаётся. Проверка не начинается на
# ядре с ненулевым tainted, после неё tainted проверяется снова.
set -euo pipefail
VM=${VM:-sbc}
HERE=$(cd "$(dirname "$0")/.." && pwd)
cd "$HERE"
mkdir -p .build/vm
make -s build-linux-arm64 >/dev/null
for pkg in internal/gpio cmd/hw-mcp; do
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go test -c -o ".build/vm/$(basename "$pkg").test" "./$pkg"
done
limactl shell "$VM" -- sudo bash -s "$HERE" <<'INVM'
set -euo pipefail
cd "$1"
CFG=/sys/kernel/config/gpio-sim
DBG=/sys/kernel/debug/gpio
ID=hwmcp-chk-$$
DEVS=("$ID" "$ID-dup1" "$ID-dup2")
PG=   # группа процессов идущего теста
kept= # чип оставлен: его линии ещё запрошены
untainted() {
	local t
	t=$(cat /proc/sys/kernel/tainted)
	echo "kernel tainted=$t"
	[ "$t" = 0 ]
}
untainted || { echo "kernel is already tainted: check not started" >&2; exit 1; }
[ -r "$DBG" ] || { echo "$DBG is unreadable: released lines cannot be verified" >&2; exit 1; }
# run <тест> <аргументы>: тест в своей группе процессов (setsid) — уборка завершит и его, и его hw-mcp
run() {
	setsid "$@" &
	PG=$!
	wait "$PG"
	PG=
}
# alive <группа>: в группе есть процесс, ещё не вышедший (у зомби дескрипторы уже закрыты)
alive() {
	ps -e -o pgid=,stat= | awk -v g="$1" '$1 == g && $2 !~ /^Z/ {found = 1} END {exit !found}'
}
# stop: завершить группу идущего теста (TERM, затем KILL) и дождаться выхода всех её процессов
stop() {
	[ -n "$PG" ] || return 0
	local sig
	for sig in TERM KILL; do
		kill -"$sig" -- -"$PG" 2>/dev/null || true
		for _ in $(seq 50); do
			alive "$PG" || return 0
			sleep 0.1
		done
	done
	return 1
}
# requested <gpiochipN>: запрошенные линии чипа (debugfs ядра перечисляет только их)
requested() {
	awk -v chip="$1:" '$1 == chip {on = 1; next} /^gpiochip/ {on = 0} on && /^ *gpio-/' "$DBG"
}
cleanup() {
	trap - EXIT
	set +e # убрать всё, что можно, даже после ошибки
	if ! stop; then
		echo "processes of group $PG are still running: gpio-sim chips are left in place" >&2
		exit 1
	fi
	for dev in "${DEVS[@]}"; do
		[ -d "$CFG/$dev" ] || continue
		if [ "$(cat "$CFG/$dev/live")" = 1 ]; then
			node=$(cat "$CFG/$dev/bank0/chip_name")
			if [ -n "$(requested "$node")" ]; then
				echo "$dev ($node): lines are still requested, chip left in place" >&2
				kept=1
				continue
			fi
			echo 0 > "$CFG/$dev/live"
		fi
		rmdir "$CFG/$dev/bank0" "$CFG/$dev"
	done
	untainted && [ -z "$kept" ] || exit 1
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP PIPE
# chip <каталог configfs> <метка>: один банк на 8 линий (две банки с одной меткой gpio-sim не принимает)
chip() {
	mkdir "$CFG/$1" "$CFG/$1/bank0"
	echo 8 > "$CFG/$1/bank0/num_lines"
	echo "$2" > "$CFG/$1/bank0/label"
	echo 1 > "$CFG/$1/live"
	echo "  $(cat "$CFG/$1/dev_name")/$(cat "$CFG/$1/bank0/chip_name"): label $2"
}
modprobe gpio-sim
echo "kernel $(uname -r); gpio-sim chips:"
chip "$ID" "$ID"
chip "$ID-dup1" "$ID-dup"
chip "$ID-dup2" "$ID-dup"
export HWMCP_GPIOSIM_LABEL=$ID HWMCP_GPIOSIM_DUP=$ID-dup HWMCP_BIN=$PWD/.build/hw-mcp-linux-arm64
echo "--- internal/gpio"
run ./.build/vm/gpio.test -test.v -test.count=1 -test.run 'UAPIStruct|UAPIByProbe|BadUAPI|GpioSim'
echo "--- cmd/hw-mcp (mixed mode: fixture sysfs + gpio-sim lines)"
run ./.build/vm/hw-mcp.test -test.v -test.count=1 -test.run GpioSim
cleanup
echo "gpio-sim check: OK"
INVM
