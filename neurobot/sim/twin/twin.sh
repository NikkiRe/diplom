#!/usr/bin/env bash
# Цифровой двойник робота NeuroChip в ВМ sim — с хоста (README двойника §6).
#   twin.sh up [--world lab|step|ramp|/путь/в/ВМ.sdf] [--hold | --cmd-vel-timeout 0.5] [--x X --y Y --z Z --yaw R]
#              [--lidar-hz 10] [--color-hz 15] [--depth-hz 15] [--imu-hz 200] [--no-rosbridge] [--rosbridge-port 9090]
#              [--domain N] [--ready-timeout 120] [-- ключ:=значение]
#       подъём; возвращается, когда двойник готов (twin_ready.py: темы идут, контроллеры активны, цепочка движения
#       связана). --hold — cmd_vel_timeout 0: diff_drive_controller держит последнюю команду (худший случай);
#       --domain — ROS_DOMAIN_ID двойника (по умолчанию из vm_env.sh, 30); печатает «run_dir=…»
#   twin.sh ready [--timeout 60]    готовность поднятого двойника: JSON в stdout, код 0 — готов, 1 — нет
#   twin.sh down [--run <run_dir>]  остановка двойника (без --run — текущего); стенд TB3 не трогает
set -euo pipefail
TWIN="$(cd "$(dirname "$0")" && pwd)"
source "$TWIN/../../../scripts/sim/lib.sh"
CUR='~/twin_runs/current'
CMD="${1:-}"
[ $# -gt 0 ] && shift
ready() {
  vm $((${1%.*} + 30)) "source $SIM_DIR/vm_env.sh; export ROS_DOMAIN_ID=\$(cat $CUR/domain); \
    python3 $TWIN/twin_ready.py --timeout $1"
}
case "$CMD" in
  up)
    LA=() DOMAIN="" READY=120
    while [ $# -gt 0 ]; do
      case "$1" in
        --world) LA+=(world:="$2"); shift 2;;
        --hold) LA+=(cmd_vel_timeout:=0); shift;;
        --cmd-vel-timeout) LA+=(cmd_vel_timeout:="$2"); shift 2;;
        --x) LA+=(x_pose:="$2"); shift 2;;  --y) LA+=(y_pose:="$2"); shift 2;;
        --z) LA+=(z_pose:="$2"); shift 2;;  --yaw) LA+=(yaw:="$2"); shift 2;;
        --lidar-hz) LA+=(lidar_hz:="$2"); shift 2;;  --color-hz) LA+=(color_hz:="$2"); shift 2;;
        --depth-hz) LA+=(depth_hz:="$2"); shift 2;;  --imu-hz) LA+=(imu_hz:="$2"); shift 2;;
        --no-rosbridge) LA+=(rosbridge:=false); shift;;
        --rosbridge-port) LA+=(rosbridge_port:="$2"); shift 2;;
        --domain) DOMAIN="$2"; shift 2;;
        --ready-timeout) READY="$2"; shift 2;;
        --) shift; LA+=("$@"); break;;
        *) echo "неизвестный аргумент: $1" >&2; exit 2;;
      esac
    done
    T0=$(date +%s)
    vm 60 ${DOMAIN:+TWIN_DOMAIN="$DOMAIN"} bash "$TWIN/twin_vm.sh" start "${LA[@]+"${LA[@]}"}"
    RUN="$(vm 20 readlink -f "$CUR")"
    while ! ready 10 >/dev/null; do
      if ! vm 10 "pgrep -g \$(cat $RUN/pid) -f twin.launch.py" >/dev/null; then
        echo "[twin] ros2 launch завершился; первые ошибки ($RUN/launch.log):" >&2
        vm 10 "grep -m 8 -E 'ERROR|Error|RuntimeError' $RUN/launch.log" >&2 || true
        vm 90 bash "$TWIN/twin_vm.sh" stop "$RUN" >&2
        exit 1
      fi
      if [ $(( $(date +%s) - T0 )) -ge "$READY" ]; then
        echo "[twin] не готов за $READY с; лог: limactl shell $SIM_VM -- tail -50 $RUN/launch.log" >&2
        ready 5 >&2 || true
        exit 1
      fi
    done
    echo "[twin] готов за $(( $(date +%s) - T0 )) с: ${LA[*]-} run_dir=$RUN";;
  ready)
    T=60
    [ "${1:-}" = --timeout ] && T="$2"
    ready "$T";;
  down)
    RUN=""
    [ "${1:-}" = --run ] && RUN="$2"
    vm 120 bash "$TWIN/twin_vm.sh" stop $RUN;;
  *)
    sed -n 2,12p "$0"; exit 2;;
esac
