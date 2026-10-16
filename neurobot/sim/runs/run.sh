#!/usr/bin/env bash
# Прогоны A0/A1 на двойнике — с хоста, из корня проекта (README §4). ВМ sim запущена; mockllm — на Mac (127.0.0.1).
#   run.sh snapshot                       ВМ-локальный снимок кода (/var/tmp/nb-runs/src) и venv mcp прокси
#   run.sh mockllm start|stop DIR         bench/mockllm на 127.0.0.1:$RUNS_MOCK_PORT, журнал — DIR/mockllm_all.jsonl
#   run.sh sched PROFILE                  расписание main | long | start → /var/tmp/nb-runs/sched_PROFILE
#   run.sh shapeclass PROFILE             «форма → класс» сетью лаборатории на жестах расписания → shapeclass_PROFILE
#   run.sh a1-install | a1-teardown       пакет neurobot на двойнике (setup_twin.sh снимка + правки прогона)
#   run.sh run a0|a1 LABEL PROFILE        прогон юнитом runs-LABEL в ВМ (переживает обрыв limactl)
#   run.sh wait LABEL                     дождаться конца прогона
#   run.sh start-test LABEL N [СЦЕН]      start_test пакета: N холодных стартов с жестом; СЦЕН — «сломанные» старты
#                                         через запятую ("" — без них; без аргумента — все по составу)
#   run.sh budget LABEL MEM PROFILE       A1 в jetson.slice (budget_a1.py) по расписанию PROFILE, MemoryMax MEM МиБ
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
MOCK=http://host.lima.internal:$PORT
VENV_MCP=${RUNS_VENV_MCP:-/home/chosenundead.guest/budget_20261016/venv-mcp}
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
      wc -l < $BASE/snapshot.sha256"
    ;;
  mockllm)
    [[ $# -eq 2 ]] || die "mockllm start|stop DIR"
    mkdir -p "$2"
    if [[ $1 == start ]]; then
      PYTHONDONTWRITEBYTECODE=1 nohup "$PROJ/.venv/bin/python" -m bench.mockllm --mode oracle --port "$PORT" \
        --journal "$2/mockllm_all.jsonl" >"$2/mockllm.out" 2>"$2/mockllm.err" </dev/null &
      echo $! >"$2/mockllm.pid"
      sleep 2
      curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null || die "mockllm не отвечает (см. $2/mockllm.err)"
      vm 20 "curl -fsS $MOCK/healthz" >/dev/null || die "ВМ не видит mockllm на $MOCK"
    else
      kill -TERM "$(cat "$2/mockllm.pid")" 2>/dev/null || true
    fi
    ;;
  sched)
    [[ $# -eq 1 ]] || die "sched PROFILE"
    vm 120 "sudo bash -c '$(py -m neurobot.sim.runs.schedule make --profile "$1" --out $BASE/sched_"$1")' && \
      sudo chmod -R a+rX $BASE/sched_$1"
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
  run)
    [[ $# -eq 3 && $1 =~ ^a[01]$ ]] || die "run a0|a1 LABEL PROFILE"
    vm 60 "sudo systemd-run --unit runs-$2 --collect --quiet -p StandardOutput=truncate:$BASE/$2.log \
      -p StandardError=append:$BASE/$2.log -E SUDO_USER=\$(id -un) --working-directory $SRC /bin/bash -c \
      '$(py -m neurobot.sim.runs.runner run --system "$1" --label "$2" --sched $BASE/sched_"$3" \
        --out $BASE/"$2" --mockllm "$MOCK")'"
    ;;
  wait)
    [[ $# -eq 1 ]] || die "wait LABEL"
    while vm 30 "systemctl is-active --quiet runs-$1"; do sleep 60; done
    vm 30 "tail -n 5 $BASE/$1.log; test -f $BASE/$1/run.json"
    ;;
  start-test)
    [[ $# -ge 2 ]] || die "start-test LABEL N [СЦЕНАРИИ через запятую; пусто — без сломанных]"
    broken=""
    [[ $# -ge 3 ]] && broken="--broken=$3"
    gest="python3 -m neurobot.sim.runs.start_gesture check --sched $BASE/sched_start --log $BASE/$1/gestures.jsonl"
    vm 60 "sudo systemd-run --unit runs-$1 --collect --quiet -p StandardOutput=truncate:$BASE/$1.log \
      -p StandardError=append:$BASE/$1.log --working-directory $SRC /bin/bash -c 'source $SRC/scripts/sim/vm_env.sh; \
      export PYTHONPATH=$SRC:\$PYTHONPATH PYTHONDONTWRITEBYTECODE=1; \
      python3 -m neurobot.sim.runs.start_gesture prepare --sched $BASE/sched_start && \
      neurobot start-test --cycles $2 $broken --out $BASE/$1 --gesture-cmd \"$gest\" \
        --deviation \"двойник (мир hall), perception — FakeYolo по таймлайну start, кадры через relay.py\"'"
    ;;
  budget)
    [[ $# -eq 3 ]] || die "budget LABEL MEM PROFILE"
    vm 60 "sudo systemd-run --unit runs-$1 --collect --quiet -p StandardOutput=truncate:$BASE/$1.log \
      -p StandardError=append:$BASE/$1.log -E SUDO_USER=\$(id -un) --working-directory $SRC /bin/bash -c \
      '$(py -m neurobot.sim.runs.budget_a1 --out $BASE/"$1" --mem "$2" --sched $BASE/sched_"$3" --label "$1" \
        --mockllm "$MOCK")'"
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
