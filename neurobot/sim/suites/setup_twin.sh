#!/usr/bin/env bash
# (в ВМ sim, root) Пакет neurobot целиком на цифровом двойнике, как на роботе (README §2): ВМ-локальный снимок проекта,
# окружения perception и MCP, PicoClaw, mockllm, таймлайн жестов, затем install.sh пакета deploy с профилем twin.env.
#   sudo setup_twin.sh [--domain N] [--world lab|step|ramp] [--hold] [--seed S]
#                      [--venv DIR] [--mcp-venv DIR] [--picoclaw БИНАРНИК]
#   sudo setup_twin.sh --teardown      остановить и снять пакет, mockllm и /var/lib/neurobot-twin
# --domain — ROS_DOMAIN_ID (23); --world и --hold — аргументы launch двойника (мир, cmd_vel_timeout 0); --seed — зерно
# gesturegen (16); --venv — Python с torch CPU для perception (~/nb/venv пользователя ВМ), --mcp-venv — Python ≥ 3.10 с
# mcp и websocket-client, --picoclaw — PicoClaw 0.2.8 linux-arm64 (копируются в /var/lib/neurobot-twin).
# Каталог проекта в ВМ смонтирован только для чтения: ставится снимок, хеши — в /var/lib/neurobot-twin/snapshot.sha256.
# Запуск после установки: sudo systemctl start neurobot.target. Код 2 — ошибка аргументов или окружения.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
TW=/var/lib/neurobot-twin
MOCKLLM_UNIT=neurobot-twin-mockllm
MOCKLLM_PORT=18124
LAB_MCP=third_party/neurochip/ros_mcp_server/ros_mcp_server-master/ros_mcp_server.py
SNN=third_party/neurochip/demo/demo-ros2_feature/models/snn_emnist_small.pth

die() { echo "setup_twin: $*" >&2; exit 2; }
[[ $EUID -eq 0 ]] || die "нужен root"

domain=23 world=lab hold=0 seed=16
venv=/home/chosenundead.guest/nb/venv mcp_venv=/var/lib/neurobot-mcpvenv picoclaw=/var/tmp/nb-pc028/picoclaw
while (($#)); do
  case $1 in
    --teardown)
      [[ $# -eq 1 ]] || die "--teardown — без других аргументов"
      if [[ -f /etc/neurobot/neurobot.env ]]; then "$TW/src/neurobot/deploy/install.sh" --uninstall || true; fi
      systemctl stop "$MOCKLLM_UNIT" 2>/dev/null || true
      rm -rf "$TW" /etc/neurobot /var/lib/neurobot /run/systemd/system/neurobot-driver@twin.service.d \
        /run/neurobot-twin-world.env
      systemctl daemon-reload
      exit 0
      ;;
    --domain) [[ ${2:-} =~ ^[0-9]{1,3}$ && $2 -le 101 ]] || die "--domain: 0–101"; domain=$2; shift 2 ;;
    --world) [[ ${2:-} =~ ^(lab|step|ramp)$ ]] || die "--world: lab | step | ramp"; world=$2; shift 2 ;;
    --hold) hold=1; shift ;;
    --seed) [[ ${2:-} =~ ^[0-9]+$ ]] || die "--seed: целое ≥ 0"; seed=$2; shift 2 ;;
    --venv) venv=${2:-}; shift 2 ;;
    --mcp-venv) mcp_venv=${2:-}; shift 2 ;;
    --picoclaw) picoclaw=${2:-}; shift 2 ;;
    *) die "аргументы: [--domain N] [--world W] [--hold] [--seed S] [--venv D] [--mcp-venv D] [--picoclaw B] | --teardown" ;;
  esac
done
[[ -f /opt/ros/jazzy/setup.bash ]] || die "нет ROS 2 Jazzy"
[[ -x $venv/bin/python && -x $mcp_venv/bin/python && -x $picoclaw ]] || die "нет --venv, --mcp-venv или --picoclaw"
[[ ! -e /etc/neurobot/neurobot.env ]] || die "пакет neurobot уже установлен (/etc/neurobot) — сначала --teardown"

id neurobot &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin neurobot
install -d -m 0755 "$TW" "$TW/src" "$TW/models" "$TW/ros-mcp"
install -d -o neurobot -g neurobot "$TW/home" "$TW/run" "$TW/gestures" "$TW/picoclaw" "$TW/picoclaw/workspace"

# снимок кода: neurobot целиком, окружение DDS стенда, mockllm; патч neurobot к ros_mcp_server лаборатории
cp -R "$ROOT/neurobot" "$TW/src/"
install -D -m 0644 "$ROOT/scripts/sim/cyclonedds.xml" "$TW/src/scripts/sim/cyclonedds.xml"
install -d "$TW/src/bench"
cp -R "$ROOT/bench/__init__.py" "$ROOT/bench/mockllm" "$TW/src/bench/"
find "$TW/src" -name __pycache__ -prune -exec rm -rf {} +
cp "$ROOT/$LAB_MCP" "$TW/ros-mcp/"
(cd "$TW/ros-mcp" && patch -p1 --quiet <"$TW/src/neurobot/deploy/mcp/ros_mcp_server.patch")
install -m 0644 "$ROOT/$SNN" "$TW/models/"
cp -a "$venv" "$TW/venv"
cp -a "$mcp_venv" "$TW/mcpvenv"
install -m 0755 "$picoclaw" "$TW/picoclaw-bin"
install -o neurobot -g neurobot -m 0600 "$HERE/picoclaw.json" "$TW/picoclaw/config.json"

# камера на USB для precheck perception (у двойника её нет), манифест весов SNN
dev=$TW/sys/bus/usb/devices/2-1
install -d "$dev" /etc/neurobot
printf '8086\n' >"$dev/idVendor"
printf '0b3a\n' >"$dev/idProduct"
printf '5000\n' >"$dev/speed"
(cd "$TW/models" && sha256sum "$PWD/snn_emnist_small.pth") >/etc/neurobot/weights.sha256
install -m 0644 "$HERE/ros_mcp.json" /etc/neurobot/ros_mcp.json

# жесты: «помахал — поехал» (классы 1 come и 2 go_left/right), окно ≈1,5 мин от якоря; якорь создаёт perception
sudo -u neurobot PYTHONPATH="$TW/src" PYTHONDONTWRITEBYTECODE=1 python3 -m neurobot.sim.gestures.gesturegen gen \
  --seed "$seed" --gestures 4 --negatives 1 --classes 1,2 --lead-in-s 15 --out-dir "$TW/gestures" >/dev/null

launch="world:=$world"
((hold)) && launch+=" cmd_vel_timeout:=0"
sed -e "s#^ROS_DOMAIN_ID=.*#ROS_DOMAIN_ID=$domain#" -e "s#world:=lab#$launch#" "$HERE/twin.env" >"$TW/twin.env"
chmod -R a+rX "$TW/src" "$TW/models" "$TW/ros-mcp" "$TW/sys" "$TW/twin.env"
(cd "$TW" && find src ros-mcp models picoclaw-bin twin.env -type f -exec sha256sum {} + | sort -k2) \
  >"$TW/snapshot.sha256"

systemd-run --unit "$MOCKLLM_UNIT" -p User=neurobot -p WorkingDirectory="$TW/src" \
  -E PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m bench.mockllm --mode oracle --tokenizer heuristic \
  --port "$MOCKLLM_PORT" >/dev/null
"$TW/src/neurobot/deploy/install.sh" --env "$TW/twin.env" --no-journald
neurobot picoclaw-config merge
echo "setup_twin: установлено (домен $domain, $launch); запуск — systemctl start neurobot.target" >&2
