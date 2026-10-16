#!/usr/bin/env bash
# Стенд робота NeuroChip в ВМ sim (профиль neurobot) — с хоста.
#   stand.sh up [--base tb3|neurochip] [--scan-occluder] [--scan-codes] [--stm32-timeout-ms 0] [--stm32-delay-ms 20]
#               [--stm32-max-lin 0] [--stm32-max-ang 0] [--stm32-max-lin-acc 0] [--stm32-max-ang-acc 0]
#               [--cmd-vel-type Twist|TwistStamped] [флаги start_sim.sh] [-- ключ:=значение]
#       подъём; возвращается, когда стенд готов (stand_ready.py: темы идут, stm32_sim связан с /cmd_vel и DiffDrive);
#       --stm32-timeout-ms 0 — hold (база держит последнюю команду), N — тишина N мс → ноль; пределы 0 — без предела;
#       --scan-occluder / --scan-codes — NaN окклюдера D435i и коды URG в /scan (scan_mask.py)
#   stand.sh ready [--timeout 30]   готовность поднятого стенда: JSON в stdout, код 0 — готов, 1 — нет
#   stand.sh down [--vm]            остановка любого стенда в ВМ (scripts/sim/stop_sim.sh; --vm — и ВМ)
#   stand.sh down --run <run_dir>   остановка только своего стенда: если ~/robot_sim/current — всё ещё <run_dir>
#                                   (его печатает up: «run_dir=…»); чужой стенд не трогает
set -euo pipefail
STAND="$(cd "$(dirname "$0")" && pwd)"
source "$STAND/../../../scripts/sim/lib.sh"
CMD="${1:-}"
[ $# -gt 0 ] && shift
case "$CMD" in
  up)
    PASS=() LA=()
    while [ $# -gt 0 ]; do
      case "$1" in
        --base) LA+=(base:="$2"); shift 2;;
        --scan-occluder) LA+=(scan_occluder:=true); shift;;
        --scan-codes) LA+=(scan_codes:=true); shift;;
        --stm32-timeout-ms) LA+=(stm32_timeout_ms:="$2"); shift 2;;
        --stm32-delay-ms) LA+=(stm32_delay_ms:="$2"); shift 2;;
        --stm32-max-lin) LA+=(stm32_max_lin:="$2"); shift 2;;
        --stm32-max-ang) LA+=(stm32_max_ang:="$2"); shift 2;;
        --stm32-max-lin-acc) LA+=(stm32_max_lin_acc:="$2"); shift 2;;
        --stm32-max-ang-acc) LA+=(stm32_max_ang_acc:="$2"); shift 2;;
        --cmd-vel-type) LA+=(stm32_cmd_vel_type:="$2"); shift 2;;
        --) shift; break;;
        *) PASS+=("$1"); shift;;
      esac
    done
    exec "$SIM_DIR/start_sim.sh" --profile neurobot "${PASS[@]+"${PASS[@]}"}" -- "${LA[@]+"${LA[@]}"}" "$@";;
  ready)
    T=30
    [ "${1:-}" = --timeout ] && T="$2"
    vm $((${T%.*} + 30)) "source $SIM_DIR/vm_env.sh; python3 $STAND/stand_ready.py --timeout $T";;
  down)
    if [ "${1:-}" = --run ]; then
      vm 120 "if [ \"\$(readlink -f ~/robot_sim/current)\" = '$2' ]; then bash $SIM_DIR/vm_stop.sh; \
        else echo '[stand] $2 — уже не текущий стенд, не трогаю'; fi"
    else
      exec "$SIM_DIR/stop_sim.sh" "$@"
    fi;;
  *)
    sed -n 2,12p "$0"; exit 2;;
esac
