#!/usr/bin/env bash
# Установка стека симулятора в VM "sim" (запускать ВНУТРИ гостя от обычного пользователя, sudo без пароля — Lima).
#   ROS 2 Jazzy (ros-base, без desktop/rviz по возможности), Gazebo Harmonic (ros-jazzy-ros-gz),
#   TurtleBot3 (gazebo/simulations/navigation2), Nav2 (nav2-bringup), rosbridge_server, Mesa (программный GL/EGL).
# С хоста: scripts/sim/provision.sh (пишет лог в experiments/results/robot_smoke_*/install.log).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
APT="sudo -E apt-get -y -o Dpkg::Use-Pty=0 -o APT::Acquire::Retries=3"
log() { echo "[install_ros $(date +%H:%M:%S)] $*"; }

log "1/5 базовые пакеты"
$APT update
$APT install --no-install-recommends ca-certificates curl gnupg locales software-properties-common lsb-release
sudo add-apt-repository -y universe
sudo locale-gen en_US.UTF-8 >/dev/null
sudo update-locale LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8

log "2/5 репозиторий packages.ros.org (ros2-apt-source)"
if ! dpkg -s ros2-apt-source >/dev/null 2>&1; then
  V="$(curl -fsS -m 30 https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest | grep -F '"tag_name"' | awk -F'"' '{print $4}')"
  CN="$(. /etc/os-release && echo "$VERSION_CODENAME")"
  curl -fsSL -m 120 -o /tmp/ros2-apt-source.deb \
    "https://github.com/ros-infrastructure/ros-apt-source/releases/download/${V}/ros2-apt-source_${V}.${CN}_all.deb"
  sudo dpkg -i /tmp/ros2-apt-source.deb
fi
$APT update

log "3/5 ROS 2 Jazzy + Gazebo Harmonic + TurtleBot3 + Nav2 + rosbridge"
# --no-install-recommends: не тянуть GUI/документацию сверх зависимостей.
PKGS=(
  ros-jazzy-ros-base
  ros-jazzy-ros-gz
  ros-jazzy-turtlebot3-gazebo
  ros-jazzy-turtlebot3-simulations
  ros-jazzy-turtlebot3-navigation2
  ros-jazzy-nav2-bringup
  ros-jazzy-navigation2
  ros-jazzy-rosbridge-server
  ros-jazzy-rmw-cyclonedds-cpp
  ros-jazzy-image-transport-plugins
)
$APT install --no-install-recommends "${PKGS[@]}"

log "4/5 Mesa (llvmpipe: GL/EGL без GPU), утилиты замеров"
$APT install --no-install-recommends mesa-utils libgl1-mesa-dri libegl-mesa0 libegl1 libgles2 \
  xvfb sysstat procps psmisc python3-websockets

log "5/5 окружение; буферы сокетов для DDS; фоновые службы, мешающие замерам, — выключить"
# Cyclone DDS (scripts/sim/cyclonedds.xml) просит приёмный буфер 16 МБ — ядро должно разрешать.
printf 'net.core.rmem_max=33554432\nnet.core.rmem_default=8388608\n' | sudo tee /etc/sysctl.d/60-ros-dds.conf >/dev/null
sudo sysctl -q --system
# apt-daily/unattended-upgrades и lttng-sessiond (тянется ros2 tracing) не нужны стенду и дают фоновую нагрузку.
sudo systemctl disable --now unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer \
  lttng-sessiond.service 2>/dev/null || true
if ! grep -q 'robot-sim env' ~/.bashrc; then
  cat >> ~/.bashrc <<'RC'
# robot-sim env
source /opt/ros/jazzy/setup.bash
export TURTLEBOT3_MODEL=waffle_pi
export ROS_DOMAIN_ID=30
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
RC
fi
sudo apt-get clean
du -sh /opt/ros/jazzy 2>/dev/null || true
dpkg -l | awk '/^ii/ && $2 ~ /^ros-jazzy-/' | wc -l | xargs echo "ros-jazzy пакетов:"
log "готово"
