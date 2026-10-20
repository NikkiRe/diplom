#!/usr/bin/env bash
# Прогоны A0/A1 на двойнике — с хоста, из корня проекта (README §4). Всё идёт в ВМ sim (на Mac — только limactl):
# mockllm и прогоны — юниты ВМ, сессию на Mac можно останавливать и продолжать.
#   run.sh snapshot                       ВМ-локальный снимок кода (/var/tmp/nb-runs/src), venv mcp прокси и mockllm
#   run.sh mockllm start|stop             bench/mockllm в ВМ (юнит runs-mockllm, 127.0.0.1:$RUNS_MOCK_PORT), журнал —
#                                         /var/tmp/nb-runs/mockllm/mockllm_all.jsonl
#   run.sh sched PROFILE [ИМЯ [ЗЕРНО]]    расписание main | long | long30 | smoke | holdout | soak | start →
#                                         /var/tmp/nb-runs/sched_ИМЯ (по умолчанию ИМЯ = PROFILE, зерно gesturegen —
#                                         20261016, у отложенного набора — своё, PROTOCOL §4); готовое не перезаписывается
#   run.sh shapeclass PROFILE             «форма → класс» сетью лаборатории на жестах расписания → shapeclass_PROFILE
#   run.sh a1-install | a1-teardown       пакет neurobot на двойнике (setup_twin.sh снимка + правки прогона)
#   run.sh queue NAME ШАГ…                очередь шагов юнитом runs-NAME (queue.py): run:a0|a1:МЕТКА:РАСПИСАНИЕ
#                                         [:world=…,no_return=…], budget:МЕТКА:МиБ:РАСПИСАНИЕ,
#                                         start:МЕТКА:N[:СЦЕНАРИИ|all]; готовые пропускает
#   run.sh wait NAME                      дождаться конца очереди
#   run.sh fetch LABEL DEST               каталог прогона ВМ → DEST на хосте (DEST не должен существовать); тестовые
#                                         токены канала pico (pico_token, .security.yml) не копируются
#   run.sh versions DEST                  версии ВМ (пакеты ROS и Gazebo, Python, torch, mcp, PicoClaw) → DEST
# Код 2 — ошибка аргументов.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../../../scripts/sim/lib.sh
source "$HERE/../../../scripts/sim/lib.sh"
BASE=/var/tmp/nb-runs
SRC=$BASE/src
PORT=${RUNS_MOCK_PORT:-18150}
MOCK=http://127.0.0.1:$PORT
VENV_MCP=${RUNS_VENV_MCP:-/home/chosenundead.guest/budget_20261016/venv-mcp}
TK=research/new_topic/GAP2_artifacts/.cache/tk
die() { echo "run.sh: $*" >&2; exit 2; }
py() { echo "source $SRC/scripts/sim/vm_env.sh; cd $SRC; PYTHONPATH=$SRC:\$PYTHONPATH PYTHONDONTWRITEBYTECODE=1 python3 $*"; }

cmd=${1:-}
[[ $# -gt 0 ]] && shift
case $cmd in
  snapshot)
    vm 300 "sudo rm -rf $SRC && sudo mkdir -p $SRC/bench $SRC/third_party && cd $PROJ && \
      sudo cp -R neurobot scripts harness $SRC/ && sudo cp -R bench/__init__.py bench/mockllm $SRC/bench/ && \
      sudo cp -R --parents third_party/neurochip/demo/demo-ros2_feature/src/camera/predict_live.py \
        third_party/neurochip/demo/demo-ros2_feature/models/snn_emnist_small.pth \
        third_party/neurochip/ros_mcp_server/ros_mcp_server-master/ros_mcp_server.py \
        third_party/neurochip/robotic_platform/robotic_platform/robot_vision_mcp_proxy.py \
        third_party/neurochip/robotic_platform/robotic_platform/.picoclaw/config.json $SRC/ && \
      sudo find $SRC -name __pycache__ -prune -exec rm -rf {} + && sudo chmod -R a+rX $SRC && \
      (cd $SRC && sudo find . -type f -exec sha256sum {} + | sort -k2 | sudo tee $BASE/snapshot.sha256 >/dev/null) && \
      if [ ! -d $BASE/mcpvenv ]; then sudo cp -a $VENV_MCP $BASE/mcpvenv; fi && \
      if [ ! -x $BASE/mockvenv/bin/python ]; then sudo python3 -m venv $BASE/mockvenv && \
        sudo $BASE/mockvenv/bin/pip install -q tiktoken==0.14.0; fi && \
      sudo rm -rf $BASE/tkcache && sudo cp -R $PROJ/$TK $BASE/tkcache && \
      wc -l < $BASE/snapshot.sha256"
    ;;
  mockllm)
    [[ $# -eq 1 && $1 =~ ^(start|stop)$ ]] || die "mockllm start|stop"
    if [[ $1 == start ]]; then
      vm 60 "systemctl is-active --quiet runs-mockllm || { sudo mkdir -p $BASE/mockllm && \
        sudo systemd-run --unit runs-mockllm --collect --quiet -p Restart=always \
          -p StandardOutput=append:$BASE/mockllm/mockllm.log -p StandardError=append:$BASE/mockllm/mockllm.log \
          --setenv=PYTHONPATH=$SRC --setenv=PYTHONDONTWRITEBYTECODE=1 --setenv=TIKTOKEN_CACHE_DIR=$BASE/tkcache \
          $BASE/mockvenv/bin/python -m bench.mockllm --mode oracle --host 127.0.0.1 --port $PORT \
          --journal $BASE/mockllm/mockllm_all.jsonl; }; \
        for i in \$(seq 20); do curl -fsS $MOCK/healthz >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1" \
        || die "mockllm не отвечает (см. $BASE/mockllm/mockllm.log в ВМ)"
    else
      vm 60 "sudo systemctl stop runs-mockllm"
    fi
    ;;
  sched)
    [[ $# -ge 1 && $# -le 3 ]] || die "sched PROFILE [ИМЯ [ЗЕРНО]]"
    name=${2:-$1}
    [[ -z ${3:-} || $3 =~ ^[0-9]+$ ]] || die "ЗЕРНО — целое число"
    vm 120 "sudo bash -c '$(py -m neurobot.sim.runs.schedule make --profile "$1" --out $BASE/sched_"$name" \
      ${3:+--seed $3})' && sudo chmod -R a+rX $BASE/sched_$name"
    ;;
  shapeclass)
    [[ $# -eq 1 ]] || die "shapeclass PROFILE"
    vm 900 "source $SRC/scripts/sim/vm_env.sh; cd $SRC; PYTHONPATH=$SRC:\$PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
      ${RUNS_ML_PYTHON:-/home/chosenundead.guest/nb/venv/bin/python} -m neurobot.sim.gestures.shapeclass \
      --gen $BASE/sched_$1 --out /var/tmp/nb-runs-dev/shapeclass_$1 --votes 50 && \
      sudo cp -R /var/tmp/nb-runs-dev/shapeclass_$1 $BASE/shapeclass_$1"
    ;;
  a1-install)
    vm 1200 "sudo bash -c '$(py -m neurobot.sim.runs.runner a1-install --mockllm "$MOCK")'"
    ;;
  a1-teardown)
    vm 600 "sudo bash -c '$(py -m neurobot.sim.runs.runner a1-teardown)'"
    ;;
  queue)
    [[ $# -ge 2 ]] || die "queue NAME ШАГ…"
    name=$1
    shift
    steps=$(printf "%q " "$@")
    vm 60 "sudo systemd-run --unit runs-$name --collect --quiet -p StandardOutput=append:$BASE/$name.queue.log \
      -p StandardError=append:$BASE/$name.queue.log -E SUDO_USER=\$(id -un) --working-directory $SRC /bin/bash -c \
      '$(py -m neurobot.sim.runs.queue --mockllm "$MOCK" --log $BASE/"$name".queue.jsonl "$steps")'"
    ;;
  wait)
    [[ $# -eq 1 ]] || die "wait NAME"
    while vm 30 "systemctl is-active --quiet runs-$1"; do sleep 60; done
    vm 30 "tail -n 5 $BASE/$1.queue.jsonl"
    ;;
  fetch)
    [[ $# -eq 2 ]] || die "fetch LABEL DEST"
    [[ ! -e $2 ]] || die "$2 уже есть — не перезаписываю"
    vm 120 "sudo chmod -R a+rX $BASE/$1 $BASE/$1.log 2>/dev/null; true"
    limactl copy -r "$SIM_VM:$BASE/$1" "$2"
    limactl copy "$SIM_VM:$BASE/$1.log" "$2/runner.log" 2>/dev/null || true
    find "$2" \( -name pico_token -o -name .security.yml \) -delete
    ;;
  versions)
    [[ $# -eq 1 ]] || die "versions DEST"
    vm 120 "uname -r; systemctl --version | head -1; python3 --version; \
      dpkg-query -W -f='\${Package} \${Version}\n' 'ros-jazzy-ros-base' 'ros-jazzy-ros-gz*' 'ros-jazzy-gz-ros2-control' \
        'ros-jazzy-ros2-controllers' 'ros-jazzy-diff-drive-controller' 'ros-jazzy-twist-mux' 'ros-jazzy-rosbridge-server' \
        'ros-jazzy-imu-filter-madgwick' 'ros-jazzy-rmw-cyclonedds-cpp' 'ros-jazzy-realsense2-camera' \
        'ros-jazzy-urg-node' 'gz-harmonic' xvfb 2>/dev/null; \
      ${RUNS_ML_PYTHON:-/home/chosenundead.guest/nb/venv/bin/python} -c 'import torch, snntorch, numpy, cv2; \
        print(\"torch\", torch.__version__, \"snntorch\", snntorch.__version__, \"numpy\", numpy.__version__, \
        \"cv2\", cv2.__version__)'; \
      $BASE/mcpvenv/bin/python -c 'import importlib.metadata as m; print(\"mcp (A0)\", m.version(\"mcp\"))'; \
      /var/lib/neurobot-mcpvenv/bin/python -c 'import importlib.metadata as m; print(\"mcp (A1)\", m.version(\"mcp\"))'; \
      sha256sum /var/tmp/nb-pc028/picoclaw /var/tmp/nb-pc028/picoclaw_Linux_arm64.tar.gz" >"$1"
    ;;
  *) sed -n 2,18p "$0"; exit 2 ;;
esac
