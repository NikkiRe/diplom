#!/usr/bin/env bash
# Бюджет Jetson Nano 4 ГБ на стенде — с Mac (README бюджета). ВМ «sim» должна быть запущена; каталог проекта
# смонтирован в ней только для чтения по тому же пути, результаты пишутся в $BUDGET_BASE ВМ и забираются fetch.
#   budget.sh prepare          в ВМ: apt-пакеты ROS (realsense2_camera, urg_node), p7zip 16.02 (Debian bullseye) и
#                              PicoClaw 0.2.8 linux-arm64 с проверкой SHA-256, venv mcp 1.27.2, копия ros_mcp_server
#                              лаборатории с патчем deploy; повторный вызов ничего не перекачивает
#   budget.sh cpucal RUN       7-zip одного ядра ВМ ×3 → RUN/cpucal_host.json; проверка квоты в slice →
#                              RUN/cpucal_check.json
#   budget.sh run RUN [--main-mem M] [--ladder 4096,3072,2406,2048,1536] [--cpu-variant Q] [--seconds 600]
#                     [--ladder-seconds 180]
#                              шаги: main (MemoryMax M, по умолчанию бюджет headless из model.json, квота из cpucal),
#                              лестница MemoryMax и вариант квоты Q (шаг cpu<Q> с памятью main); затем report
#   budget.sh report RUN       report.json и report.md в RUN
#   budget.sh fetch RUN DEST   записать RUN/manifest.json в ВМ и забрать RUN в DEST (DEST не должно существовать)
#   budget.sh down             остановить юниты бюджета (и двойник прогона RUN, если задан BUDGET_RUN=RUN)
# RUN — имя прогона в $BUDGET_BASE/runs (ВМ). Переменные: BUDGET_BASE (~/budget_20261016 в ВМ). Код 2 — ошибка
# аргументов.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJ="$(cd "$HERE/../../.." && pwd)"
# shellcheck source=../../../scripts/sim/lib.sh
source "$PROJ/scripts/sim/lib.sh"
BASE="${BUDGET_BASE:-\$HOME/budget_20261016}"     # раскрывается в ВМ
PY="cd $PROJ && PYTHONDONTWRITEBYTECODE=1"
MOD=neurobot.sim.budget
SEVENZA="$BASE/tools/p7zip16/usr/lib/p7zip/7za"
GET="python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])'"
P7ZIP_DEB=p7zip-full_16.02+dfsg-8_arm64.deb
P7ZIP_URL=https://deb.debian.org/debian/pool/main/p/p7zip/$P7ZIP_DEB
P7ZIP_SHA=f2f18a83a5b1157876401fa7d5ad04b0f7d891642c021de7cfbe07cd847bd8be
PC_TGZ=picoclaw_Linux_arm64.tar.gz
PC_URL=https://github.com/sipeed/picoclaw/releases/download/v0.2.8/$PC_TGZ
PC_SHA=9ce9fbf76da9c967baffb18c4b502dec2055b3ad050fc2381b6260429e1acf70
MCP_SRC=$PROJ/third_party/neurochip/ros_mcp_server/ros_mcp_server-master/ros_mcp_server.py
MCP_PATCH=$PROJ/neurobot/deploy/mcp/ros_mcp_server.patch

die() { echo "budget: $*" >&2; exit 2; }
CMD="${1:-}"
[ $# -gt 0 ] && shift
case "$CMD" in
  prepare)
    vm 900 "set -e; B=$BASE; mkdir -p \$B/dl \$B/tools/p7zip16 \$B/tools/picoclaw028 \$B/mcp \$B/runs
      sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q ros-jazzy-realsense2-camera ros-jazzy-urg-node \
        >/dev/null
      cd \$B/dl
      [ -f $P7ZIP_DEB ] || curl -sSfL -o $P7ZIP_DEB $P7ZIP_URL
      echo '$P7ZIP_SHA  $P7ZIP_DEB' | sha256sum -c --quiet
      [ -f $PC_TGZ ] || curl -sSfL -o $PC_TGZ $PC_URL
      echo '$PC_SHA  $PC_TGZ' | sha256sum -c --quiet
      [ -x $SEVENZA ] || dpkg-deb -x $P7ZIP_DEB \$B/tools/p7zip16
      [ -x \$B/tools/picoclaw028/picoclaw ] || tar xzf $PC_TGZ -C \$B/tools/picoclaw028 picoclaw
      [ -x \$B/venv-mcp/bin/python ] || python3 -m venv \$B/venv-mcp
      \$B/venv-mcp/bin/pip install -q 'mcp==1.27.2' 'websocket-client==1.9.2'
      cp $MCP_SRC \$B/mcp/ros_mcp_server.py && patch -s -d \$B/mcp -p1 < $MCP_PATCH
      echo готово: \$B";;
  cpucal)
    [ $# -eq 1 ] || die "cpucal RUN"
    R="$BASE/runs/$1"
    vm 900 "mkdir -p $R && $PY python3 -m $MOD.cpucal host --bin $SEVENZA --out $R/cpucal_host.json >/dev/null \
      && $PY sudo python3 -m $MOD.cpucal check --bin $SEVENZA --quota \$($GET $R/cpucal_host.json quota_pct) \
           --out $R/cpucal_check.json >/dev/null \
      && cat $R/cpucal_host.json $R/cpucal_check.json";;
  run)
    [ $# -ge 1 ] || die "run RUN [флаги]"
    R="$BASE/runs/$1"; shift
    MAIN="" LADDER="4096,3072,2406,2048,1536" VARIANT="" SEC=600 LSEC=180
    while [ $# -gt 0 ]; do
      case "$1" in
        --main-mem) MAIN="$2"; shift 2;;
        --ladder) LADDER="$2"; shift 2;;
        --cpu-variant) VARIANT="$2"; shift 2;;
        --seconds) SEC="$2"; shift 2;;
        --ladder-seconds) LSEC="$2"; shift 2;;
        *) die "неизвестный аргумент: $1";;
      esac
    done
    [ -n "$MAIN" ] || MAIN=$(cd "$PROJ" && python3 -c "from $MOD.model import load, stack_budget
print(stack_budget(load(), 'headless'))")
    RATIO="\$($GET $R/cpucal_host.json ratio)"
    STACK="$PY sudo python3 -m $MOD.stack run --out $R --ratio $RATIO --keep-twin"
    QUOTA="\$($GET $R/cpucal_host.json quota_pct)"
    vm 7200 "$STACK --quota $QUOTA --mem $MAIN --labels main --seconds $SEC"
    if [ -n "$LADDER" ]; then
      vm 7200 "$STACK --quota $QUOTA --mem $LADDER --seconds $LSEC"
    fi
    if [ -n "$VARIANT" ]; then
      vm 3600 "$STACK --quota $VARIANT --mem $MAIN --labels cpu$VARIANT --seconds $SEC"
    fi
    vm 120 "$PY sudo python3 -m $MOD.stack down --twin-root $R/twin_runs"
    "$0" report "$(basename "$R")";;
  report)
    [ $# -eq 1 ] || die "report RUN"
    R="$BASE/runs/$1"
    vm 300 "$PY python3 -m $MOD.report --run $R --cpucal $R/cpucal_host.json --check $R/cpucal_check.json \
      && cat $R/report.md";;
  fetch)
    [ $# -eq 2 ] || die "fetch RUN DEST"
    [ ! -e "$2" ] || die "$2 уже есть — результаты не перезаписываются"
    R="$(vm 20 "readlink -f $BASE/runs/$1")"
    vm 120 "$PY python3 -m $MOD.manifest --run $R --base $BASE"
    mkdir -p "$2"
    # tar, а не limactl copy: scp спотыкается о висячие ссылки ros_log/latest
    to 900 limactl shell --workdir / "$SIM_VM" -- tar -C "$R" -cf - . | tar -C "$2" -xf -
    echo "budget: $2";;
  down)
    TWIN=""
    [ -z "${BUDGET_RUN:-}" ] || TWIN="--twin-root $BASE/runs/$BUDGET_RUN/twin_runs"
    vm 120 "$PY sudo python3 -m $MOD.stack down $TWIN";;
  *)
    awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"; exit 2;;
esac
