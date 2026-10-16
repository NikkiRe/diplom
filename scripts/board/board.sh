#!/usr/bin/env bash
# Эмуляция «убогой» платы в ВМ sbc готовыми механизмами ядра Linux: сеть — netns + veth + netem в обе стороны,
# ресурсы — cgroup v2 через systemd. Уровни и профили — bench/board/profiles.json, описание — bench/board/README.md.
# С Mac (каталог проекта смонтирован в ВМ по тому же пути): limactl shell sbc -- sudo bash scripts/board/board.sh …
#   net-up <ns> <профиль>      netns board-<ns>, veth bh-<ns> (корень) / bn-<ns> (netns), подсеть 10.77.<n>.0/24,
#                              NAT только для неё; netem вверх — на bn-<ns>, вниз — на ifb bi-<ns> (входящее
#                              bn-<ns>), оба в netns; повторный вызов — как net-set
#   net-set <ns> <профиль>     сменить профиль; профиль-расписание (flaky) ведёт фоновый юнит board-sched-<ns>
#   net-schedule <ns> <файл>   своё расписание {"repeat": true, "steps": [{"profile": p, "for_s": s}, …]} или
#                              {"repeat": false, "steps": […], "after": p} — p после последнего шага;
#                              файл — путь в ВМ или /dev/stdin
#   net-stats <ns>             JSON: профиль, подсеть, начало расписания, счётчики netem вверх (up) и вниз (down)
#   net-down <ns>              снять netns, veth, NAT и расписание; повторный вызов — не ошибка
#   run <имя> --mem МиБ --cpu % [--cpus N] [--disk профиль] [--net ns] [--workdir каталог]
#       [--gomemlimit %|off] [--timeout с] [--env K=V …] -- команда …
#                              команда (абсолютный путь или имя из PATH) в юните board-run-<имя> от BOARD_USER
#                              (по умолчанию SUDO_USER) под лимитами; последняя строка stderr —
#                              «board-run: {итог JSON}», код выхода — команды
#   cleanup                    остановить и снять всё, что создано board.sh
# Коды: 2 — ошибка аргументов или состояния, 125 — run не получил итога юнита.
set -euo pipefail

# Итог юнита run снимает ExecStopPost=+ (от root) в cgroup юнита, пока она существует, — и всё, что он делает,
# попадает в её счётчики. Поэтому разбор стоит до остального файла (bash читает скрипт по мере выполнения) и обходится
# встроенными командами без подпроцессов: к memory.peak, pids.peak и cpu.stat добавляется только сам bash записи.
# Файлы сырые; JSON из них собирает run уже вне cgroup (record.jq).
if [[ ${1:-} == _record ]]; then
  read -r cg </proc/self/cgroup
  cg=/sys/fs/cgroup${cg#0::}
  for f in memory.peak memory.max memory.swap.max memory.events cpu.max cpu.stat cpuset.cpus.effective io.max io.stat \
    pids.peak; do
    if [[ -r $cg/$f ]]; then
      mapfile -t lines <"$cg/$f"
      printf '%s\n' "${lines[@]}" >"$2/$f"
    fi
  done
  printf '%s\n' "${SERVICE_RESULT:-}" "${EXIT_CODE:-}" "${EXIT_STATUS:-}" >"$2/exit"
  exit 0
fi

SELF=$(readlink -f "${BASH_SOURCE[0]}")
DIR=${SELF%/*}
PROFILES=$(readlink -f "$DIR/../../bench/board/profiles.json")
STATE=/run/board

die() { echo "board: $*" >&2; exit 2; }

# Файл состояния — целиком или никак: net-stats читает его параллельно с юнитом расписания.
put() {  # <файл> <строка>
  echo "$2" >"$1.tmp"
  mv "$1.tmp" "$1"
}

# --- сеть -----------------------------------------------------------------------------------------------------------

check_ns() {  # имя входит в имена устройств bh-/bn-/bi-<ns> (не длиннее 15 символов)
  [[ $1 =~ ^[a-z][a-z0-9]{0,11}$ ]] || die "имя netns: [a-z][a-z0-9]{0,11}, получено '$1'"
}

ns_ready() {
  [[ -f $STATE/net/$1/idx && -e /run/netns/board-$1 && -e /sys/class/net/bh-$1 ]] \
    && nft list table ip "board_$1" &>/dev/null
}

require_ns() { ns_ready "$1" || die "netns $1 не поднят (net-up)"; }

# Изменения сети, net-stats и cleanup — по очереди: выбор подсети и снятие не пересекаются, а net-stats не видит
# netns, поднятый наполовину.
lock() {
  mkdir -p "$STATE/net"
  exec 9>"$STATE/lock"
  flock 9
}

# static | schedule | пусто, если профиля нет
net_kind() {
  jq -r --arg p "$1" '.net.profiles[$p] // empty | if .schedule then "schedule" else "static" end' "$PROFILES"
}

# Наименьший свободный номер подсети 10.77.<n>.0/24, не занятый и чужими адресами корневого netns.
alloc_idx() {
  local f i used=" "
  for f in "$STATE"/net/*/idx; do
    if [[ -f $f ]]; then used+="$(<"$f") "; fi
  done
  for ((i = 1; i < 255; i++)); do
    if [[ $used != *" $i "* && -z $(ip -o -4 addr show to "10.77.$i.0/24") ]]; then
      echo "$i"
      return
    fi
  done
  echo "board: нет свободной подсети в 10.77.0.0/16" >&2
  return 1
}

# Пересылка нужна NAT; исходное значение возвращается, когда снят последний netns board.
forward_on() {
  if [[ $(sysctl -n net.ipv4.ip_forward) == 0 ]]; then
    echo 0 >"$STATE/ip_forward.orig"
    sysctl -qw net.ipv4.ip_forward=1
  fi
}

forward_restore() {
  if [[ -f $STATE/ip_forward.orig ]] && ! compgen -G "$STATE/net/*/idx" >/dev/null; then
    sysctl -qw "net.ipv4.ip_forward=$(<"$STATE/ip_forward.orig")"
    rm "$STATE/ip_forward.orig"
  fi
}

# Аргументы netem направления (netem.jq) — в массив с именем $3; ошибка jq останавливает скрипт, а не теряется
# в подстановке.
netem_args() {  # <профиль> <up|down> <массив>
  local line
  line=$(jq -r --arg p "$1" --arg d "$2" -f "$DIR/netem.jq" "$PROFILES")
  read -ra "$3" <<<"$line"
}

# Вверх — корень qdisc bn-<ns> (исходящее с платы), вниз — bi-<ns> (входящее на плату, см. net_up); оба в netns.
set_netem() {  # <ns> <статический профиль>
  local -a up down
  netem_args "$2" up up
  netem_args "$2" down down
  tc -n "board-$1" qdisc replace dev "bn-$1" root netem "${up[@]}"
  tc -n "board-$1" qdisc replace dev "bi-$1" root netem "${down[@]}"
  put "$STATE/net/$1/netem" "$2"
}

stop_schedule() {
  if systemctl is-active --quiet "board-sched-$1.service"; then systemctl stop "board-sched-$1.service"; fi
}

# Проверить расписание и запустить его вместо текущего. Шаги — статические профили не короче
# net.schedule_min_step_s (каждая смена шага — два вызова jq и два tc); after — у неповторяющегося расписания.
start_schedule() {  # <ns> <профиль для net-stats> <JSON расписания>
  local dir=$STATE/net/$1 bad
  bad=$(jq -r --slurpfile p "$PROFILES" '
    $p[0].net as $net
    | def static: type == "string" and ($net.profiles[.] // {schedule: 1} | has("schedule") | not);
      if (.steps | type) != "array" or (.steps | length) == 0 then "нет шагов"
      elif (.repeat | type) != "boolean" then "repeat — true или false"
      elif .repeat == has("after") then "after (профиль после последнего шага) — только и обязательно при repeat: false"
      elif has("after") and (.after | static | not) then "after: нет статического профиля \(.after | tojson)"
      else .steps[] | select((.for_s | type) != "number" or .for_s < $net.schedule_min_step_s
                             or (.profile | static | not))
           | "плохой шаг \(tojson): нужны статический профиль и for_s ≥ \($net.schedule_min_step_s)" end' <<<"$3") \
    || die "расписание: не JSON или неверная структура"
  [[ -z $bad ]] || die "расписание: $bad"
  stop_schedule "$1"
  jq -c '{repeat, steps, after}' <<<"$3" >"$dir/schedule.json"
  put "$dir/profile" "$2"
  # Первый шаг — сразу, до юнита: net-stats без гонки видит и профиль, и начало отсчёта.
  put "$dir/schedule.start" "${EPOCHREALTIME//[.,]/}"
  set_netem "$1" "$(jq -r '.steps[0].profile' "$dir/schedule.json")"
  systemd-run --unit "board-sched-$1" --collect --quiet /bin/bash "$SELF" _schedule "$1"
}

net_set() {  # <ns> <профиль>
  local kind
  require_ns "$1"
  kind=$(net_kind "$2")
  [[ -n $kind ]] || die "нет сетевого профиля '$2'"
  if [[ $kind == schedule ]]; then
    start_schedule "$1" "$2" "$(jq --arg p "$2" '.net.profiles[$p].schedule' "$PROFILES")"
    return
  fi
  stop_schedule "$1"
  rm -f "$STATE/net/$1/schedule.json" "$STATE/net/$1/schedule.start"
  set_netem "$1" "$2"
  put "$STATE/net/$1/profile" "$2"
}

net_schedule() {  # <ns> <файл расписания>
  require_ns "$1"
  [[ -r $2 ]] || die "нет файла расписания $2"
  start_schedule "$1" "schedule:$2" "$(<"$2")"
}

net_teardown() {  # снимает всё, что осталось от netns, в любом состоянии
  stop_schedule "$1"
  # Удаление одного конца veth удаляет оба; удаление netns освобождает свой конец асинхронно.
  if [[ -e /sys/class/net/bh-$1 ]]; then ip link del "bh-$1"; fi
  if [[ -e /run/netns/board-$1 ]]; then ip netns del "board-$1"; fi
  if nft list table ip "board_$1" &>/dev/null; then nft delete table ip "board_$1"; fi
  rm -rf "${STATE:?}/net/$1"
}

net_up() {  # <ns> <профиль>
  local ns=$1 idx
  [[ -n $(net_kind "$2") ]] || die "нет сетевого профиля '$2'"
  if ns_ready "$ns"; then
    net_set "$ns" "$2"
    return
  fi
  net_teardown "$ns"
  # Сбой или сигнал на полпути — откат: остатков частично поднятого netns не бывает.
  # shellcheck disable=SC2064  # имя подставляется сейчас: оно проверено check_ns, а локальная ns к выходу исчезнет
  trap "net_teardown $ns; forward_restore" EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  idx=$(alloc_idx)
  mkdir -p "$STATE/net/$ns"
  echo "$idx" >"$STATE/net/$ns/idx"
  ip netns add "board-$ns"
  ip link add "bh-$ns" type veth peer name "bn-$ns" netns "board-$ns"
  ip addr add "10.77.$idx.1/24" dev "bh-$ns"
  ip link set "bh-$ns" up
  ip -n "board-$ns" addr add "10.77.$idx.2/24" dev "bn-$ns"
  ip -n "board-$ns" link set lo up
  ip -n "board-$ns" link set "bn-$ns" up
  ip -n "board-$ns" route add default via "10.77.$idx.1"
  # netem должен видеть пакеты размером в MTU, а не склейки до 64 КиБ: иначе потеря, разброс и очередь достаются
  # склейке целиком. Вверх их не создаёт bn-<ns> без TSO/GSO. Вниз склейки GRO приходят с eth0 (его не трогаем) и
  # доходили бы до netem на bh-<ns> целыми — режутся они только при отправке из bh-<ns> без GSO. Поэтому netem «вниз»
  # стоит после veth: входящее bn-<ns> перенаправляется (tc mirred) на ifb bi-<ns> внутри netns.
  # numifbs=0 — модуль не создаёт свои ifb0/ifb1 в корневом netns.
  ethtool -K "bh-$ns" tso off gso off >/dev/null
  ip netns exec "board-$ns" ethtool -K "bn-$ns" tso off gso off >/dev/null
  modprobe ifb numifbs=0
  ip -n "board-$ns" link add "bi-$ns" type ifb
  ip -n "board-$ns" link set "bi-$ns" up
  tc -n "board-$ns" qdisc add dev "bn-$ns" handle ffff: ingress
  tc -n "board-$ns" filter add dev "bn-$ns" parent ffff: matchall action mirred egress redirect dev "bi-$ns"
  forward_on
  nft -f - <<EOF
table ip board_$ns {
  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
    ip saddr 10.77.$idx.0/24 masquerade
  }
}
EOF
  net_set "$ns" "$2"
  trap - EXIT INT TERM
}

net_stats() {
  local dir=$STATE/net/$1
  require_ns "$1"
  # schedule.start удаляет юнит в конце неповторяющегося расписания — его может не быть.
  jq -n --arg ns "$1" --arg profile "$(<"$dir/profile")" --arg netem "$(<"$dir/netem")" \
    --argjson idx "$(<"$dir/idx")" --arg start "$(cat "$dir/schedule.start" 2>/dev/null || true)" \
    --argjson up "$(tc -n "board-$1" -s -j qdisc show dev "bn-$1")" \
    --argjson down "$(tc -n "board-$1" -s -j qdisc show dev "bi-$1")" \
    '{ns: $ns, profile: $profile, netem: $netem, subnet: "10.77.\($idx).0/24",
      schedule_start_us: (if $start == "" then null else ($start | tonumber) end),
      up: $up[0], down: $down[0]}'
}

# Ждать до момента в микросекундах эпохи; sleep в фоне, чтобы TERM прерывал ожидание сразу.
sleep_until() {
  local d=$(($1 - ${EPOCHREALTIME//[.,]/}))
  if ((d > 0)); then
    sleep "$((d / 1000000)).$(printf %06d $((d % 1000000)))" &
    wait $!
  fi
}

# Тело юнита board-sched-<ns>: первый шаг уже применил start_schedule; сроки шагов отсчитываются от общего начала,
# без накопления дрейфа. Неповторяющееся расписание в конце ставит профиль after и завершается.
schedule_loop() {
  local ns=$1 dir=$STATE/net/$1 i=0 next after
  local -a profiles durs
  trap 'exit 0' TERM INT
  mapfile -t profiles < <(jq -r '.steps[].profile' "$dir/schedule.json")
  mapfile -t durs < <(jq -r '.steps[] | .for_s * 1000000 | round' "$dir/schedule.json")
  after=$(jq -r '.after // empty' "$dir/schedule.json")
  next=$(<"$dir/schedule.start")
  while :; do
    next=$((next + durs[i]))
    sleep_until "$next"
    i=$(((i + 1) % ${#profiles[@]}))
    if ((i == 0)) && [[ -n $after ]]; then
      set_netem "$ns" "$after"
      put "$dir/profile" "$after"
      rm "$dir/schedule.json" "$dir/schedule.start"
      echo "конец расписания: $after"
      return
    fi
    set_netem "$ns" "${profiles[i]}"
    echo "шаг $i: ${profiles[i]} на $((durs[i] / 1000)) мс"
  done
}

# --- запуск под ограничениями ---------------------------------------------------------------------------------------

# Итог в JSON по сырым файлам _record (scripts/board/record.jq).
record_json() {  # <каталог итога> <юнит> <секунды>
  local f how status signum=""
  local -a args=()
  for f in "$1"/*; do
    args+=(--rawfile "${f##*/}" "$f")
  done
  if [[ -f $1/exit ]]; then
    { read -r _; read -r how; read -r status; } <"$1/exit"
    if [[ $how == killed || $how == dumped ]]; then signum=$(kill -l "$status"); fi
  fi
  jq -c -n "${args[@]}" --arg unit "$2" --arg wall_s "$3" --arg signum "$signum" -f "$DIR/record.jq"
}

# Юнит-служба с --wait, а не --scope: только у службы есть NetworkNamespacePath=, User=, WorkingDirectory=,
# BindReadOnlyPaths=, RuntimeMaxSec= (процесс сразу в netns и от обычного пользователя, без обёрток ip netns exec/
# setpriv; срок соблюдается в самой ВМ), ExecStopPost= со счётчиками cgroup до её удаления и EXIT_CODE/EXIT_STATUS/
# SERVICE_RESULT от systemd, в том числе oom-kill и timeout. Пустая scope удаляется сразу после выхода процесса —
# memory.events и memory.peak пришлось бы ловить гонкой.
run_cmd() {
  local name=${1:-} mem="" cpu="" cpus="" disk="" net="" workdir="" gml="" timeout="" period user dev limit unit rec
  local pid value code t0 us
  local -a envs=() props=() limits=() cmd=()
  [[ $name =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] || die "run: имя [a-z0-9][a-z0-9-]{0,31}, получено '$name'"
  shift
  while (($#)); do
    if [[ $1 == -- ]]; then
      shift
      cmd=("$@")
      break
    fi
    (($# >= 2)) || die "run: у $1 нет значения"
    case $1 in
      --mem) mem=$2 ;;
      --cpu) cpu=$2 ;;
      --cpus) cpus=$2 ;;
      --disk) disk=$2 ;;
      --net) net=$2 ;;
      --workdir) workdir=$2 ;;
      --gomemlimit) gml=$2 ;;
      --timeout) timeout=$2 ;;
      --env) [[ $2 =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || die "run: --env K=V, получено '$2'"; envs+=("--setenv=$2") ;;
      *) die "run: неизвестный аргумент '$1'" ;;
    esac
    shift 2
  done
  [[ $mem =~ ^[1-9][0-9]*$ ]] || die "run: --mem — целое число МиБ"
  cpus=${cpus:-$(nproc)}
  if ! [[ $cpus =~ ^[1-9][0-9]*$ ]] || ((cpus > $(nproc))); then die "run: --cpus от 1 до $(nproc)"; fi
  # Квота за период — не меньше 1 мс (минимум ядра: меньшую systemd молча растянет на более длинный период)
  # и не больше, чем дают ядра.
  period=$(jq .cpu.period_ms "$PROFILES")
  if ! [[ $cpu =~ ^[1-9][0-9]*$ ]] || ((cpu * period < 100 || cpu > 100 * cpus)); then
    die "run: --cpu — целое от $(((100 + period - 1) / period)) (1 мс за период $period мс)" \
      "до $((100 * cpus)) % (ядер: $cpus)"
  fi
  [[ -z $timeout || ($timeout =~ ^[0-9]+([.][0-9]+)?$ && $timeout =~ [1-9]) ]] || die "run: --timeout — секунды > 0"
  ((${#cmd[@]})) || die "run: после -- нужна команда"
  # Относительный путь systemd-run ищет не от рабочего каталога — юнит не был бы создан.
  [[ ${cmd[0]} != */* || ${cmd[0]} == /* ]] \
    || die "run: команда — абсолютный путь или имя из PATH, получено '${cmd[0]}'"
  user=${BOARD_USER:-${SUDO_USER:-}}
  [[ -n $user && $user != root ]] || die "run: нужен обычный пользователь — запуск через sudo или BOARD_USER"
  workdir=${workdir:-$(getent passwd "$user" | cut -d: -f6)}
  [[ -d $workdir ]] || die "run: нет каталога $workdir"
  gml=${gml:-$(jq .memory.gomemlimit_pct "$PROFILES")}

  # OOMPolicy=stop: первое OOM-убийство в юните останавливает его целиком — ступень не пройдена;
  # TimeoutStopSec: остановка по сигналу, сроку или OOM не ждёт умолчания systemd (90 с).
  props=(-p "MemoryMax=${mem}M" -p MemorySwapMax=0 -p "CPUQuota=${cpu}%" -p "CPUQuotaPeriodSec=${period}ms"
    -p "AllowedCPUs=0-$((cpus - 1))" -p "TasksMax=$(jq .tasks_max "$PROFILES")" -p IOAccounting=yes
    -p OOMPolicy=stop -p TimeoutStopSec=10s)
  if [[ -n $timeout ]]; then props+=(-p "RuntimeMaxSec=${timeout}s"); fi
  if [[ -n $disk ]]; then
    jq -e --arg d "$disk" '.disk | has($d)' "$PROFILES" >/dev/null || die "run: нет дискового профиля '$disk'"
    mapfile -t limits < <(jq -r --arg d "$disk" '.disk[$d] | {IOReadBandwidthMax: .rbps, IOWriteBandwidthMax: .wbps,
                                                  IOReadIOPSMax: .riops, IOWriteIOPSMax: .wiops}
                                                | to_entries[] | select(.value) | "\(.key)=\(.value)"' "$PROFILES")
  fi
  if ((${#limits[@]})); then
    # systemd сам находит блочное устройство (весь диск) под рабочим каталогом, а без устройства (virtiofs, tmpfs)
    # лишь пишет в журнал и запускает юнит без io.max — ось диска пропала бы молча.
    dev=$(findmnt -nro MAJ:MIN -T "$workdir")
    [[ -e /sys/dev/block/$dev ]] \
      || die "run: --disk: $workdir не на блочном устройстве ($(findmnt -nro FSTYPE -T "$workdir")):" \
        "io.max не к чему применить"
    for limit in "${limits[@]}"; do
      props+=(-p "${limit%%=*}=$workdir ${limit#*=}")
    done
  fi
  if [[ -n $net ]]; then
    check_ns "$net"
    require_ns "$net"
    # resolv.conf корня указывает на 127.0.0.53, недоступный из netns; берём адреса вышестоящих DNS.
    props+=(-p "NetworkNamespacePath=/run/netns/board-$net"
      -p "BindReadOnlyPaths=/run/systemd/resolve/resolv.conf:/etc/resolv.conf")
  fi
  # Рантайм Go: GOMEMLIMIT — доля MemoryMax; GOMAXPROCS — число ядер платы, как без квоты на настоящей плате
  # (иначе Go ≥ 1.25 урежет его по квоте cgroup). --env с тем же именем идёт позже и побеждает.
  case $gml in
    off) ;;
    [1-9] | [1-9][0-9] | 100) envs=("--setenv=GOMEMLIMIT=$((mem * 1048576 * gml / 100))" "${envs[@]}") ;;
    *) die "run: --gomemlimit — процент 1..100 или off" ;;
  esac
  envs=("--setenv=GOMAXPROCS=$cpus" "${envs[@]}")

  unit=board-run-$name
  if systemctl is-active --quiet "$unit.service"; then die "run: $unit уже запущен"; fi
  rec=$STATE/run/$name
  rm -rf "$rec"
  mkdir -p "$rec"
  props+=(-p "ExecStopPost=+/bin/bash \"$SELF\" _record \"$rec\"")
  t0=${EPOCHREALTIME//[.,]/}
  # В фоне с явным stdin, чтобы wait прерывался сигналом и обработчик успевал остановить юнит. Сигнал должен прийти
  # самому board.sh: клиент limactl shell без tty при своём завершении в ВМ ничего не передаёт (см. Vm.run).
  systemd-run --unit "$unit" --wait --pipe --quiet --collect --expand-environment=no \
    --uid "$user" --working-directory "$workdir" "${props[@]}" "${envs[@]}" -- "${cmd[@]}" 0<&0 &
  pid=$!
  trap 'systemctl stop "$unit.service"' INT TERM HUP
  # wait возвращает код systemd-run или >128 после обработанного сигнала — тогда ждём дальше.
  until wait "$pid"; do
    kill -0 "$pid" 2>/dev/null || break
  done
  trap - INT TERM HUP
  if [[ ! -s $rec/exit ]]; then
    rm -rf "$rec"
    echo "board: $unit не оставил итога (юнит не создан?)" >&2
    exit 125
  fi
  us=$((${EPOCHREALTIME//[.,]/} - t0))
  value=$(record_json "$rec" "$unit.service" "$((us / 1000000)).$(printf %06d $((us % 1000000)))")
  rm -rf "$rec"
  echo "board-run: $value" >&2
  code=$(jq .exit_code <<<"$value")
  exit "$code"
}

cleanup_all() {
  local ns
  systemctl stop 'board-run-*.service' 'board-sched-*.service'
  for ns in "$STATE"/net/*/; do
    if [[ -d $ns ]]; then net_teardown "$(basename "$ns")"; fi
  done
  while read -r ns; do
    net_teardown "${ns#board-}"
  done < <(ip netns list | awk '$1 ~ /^board-/ {print $1}')
  while read -r ns; do
    nft delete table ip "$ns"
  done < <(nft list tables ip | awk '$3 ~ /^board_/ {print $3}')
  rm -rf "${STATE:?}/run"
  forward_restore
}

# Без аргументов — справка: комментарий в начале файла.
(($#)) || { awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$SELF"; exit 2; }
[[ $EUID -eq 0 ]] || die "нужен root: limactl shell sbc -- sudo bash $SELF …"
command=$1
shift
case $command in
  net-up | net-set | net-schedule)
    (($# == 2)) || die "$command <ns> <профиль|файл>"
    check_ns "$1"
    lock
    "${command/-/_}" "$@" ;;
  net-down) (($# == 1)) || die "net-down <ns>"; check_ns "$1"; lock; net_teardown "$1"; forward_restore ;;
  net-stats) (($# == 1)) || die "net-stats <ns>"; check_ns "$1"; lock; net_stats "$1" ;;
  cleanup) (($# == 0)) || die "cleanup без аргументов"; lock; cleanup_all ;;
  run) run_cmd "$@" ;;
  _schedule) schedule_loop "$1" ;;
  *) die "неизвестная команда '$command'" ;;
esac
