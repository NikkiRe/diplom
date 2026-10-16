#!/usr/bin/env bash
# Стенд проверки запуска на подставных компонентах (ВМ sim, ROS 2 Jazzy; README.md «Проверка на стенде»).
#   sudo ./setup_stand.sh                       пользователь neurobot, подставные /sys (камера 8086:0b3a, USB 3) и
#                                               веса, конфиги fake.env / ros_mcp.json / picoclaw.json, install.sh
#   sudo ./setup_stand.sh --picoclaw БИНАРНИК   то же, но агент — настоящий PicoClaw (linux-arm64): его config.json —
#                                               picoclaw.json + оверлей neurobot (picoclaw-config merge по версии)
#   sudo ./setup_stand.sh --domain N            другой домен DDS вместо 77 из fake.env (ВМ общая: чужой издатель /scan
#                                               или /cmd_vel в том же домене precheck справедливо не пустит);
#                                               сочетается с --picoclaw
#   sudo ./setup_stand.sh --teardown            install.sh --uninstall, убрать /var/lib/neurobot-fake, /etc/neurobot и
#                                               состояние safety_gate (/var/lib/neurobot: защёлка)
# Каталог проекта в ВМ смонтирован только для чтения — ставится копия, исходники не меняются. Код 2 — ошибка.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
FAKE=/var/lib/neurobot-fake
ETC=/etc/neurobot

die() { echo "setup_stand: $*" >&2; exit 2; }
[[ $EUID -eq 0 ]] || die "нужен root"

picoclaw="" domain=""
while (($#)); do
  case $1 in
    --teardown)
      [[ $# -eq 1 ]] || die "--teardown — без других аргументов"
      "$HERE/../install.sh" --uninstall
      rm -rf "$FAKE" "$ETC" /var/lib/neurobot
      exit 0
      ;;
    --picoclaw)
      [[ $# -ge 2 && -x $2 ]] || die "--picoclaw: нужен исполняемый файл PicoClaw"
      picoclaw=$(realpath "$2")
      shift 2
      ;;
    --domain)
      [[ $# -ge 2 && $2 =~ ^[0-9]{1,3}$ && $2 -le 101 ]] || die "--domain: номер домена DDS 0–101"
      domain=$2
      shift 2
      ;;
    *) die "аргументы: [--picoclaw БИНАРНИК] [--domain N] | --teardown" ;;
  esac
done
[[ -f /opt/ros/jazzy/setup.bash ]] || die "нет ROS 2 Jazzy (/opt/ros/jazzy)"

id neurobot &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin neurobot
dev=$FAKE/sys/bus/usb/devices/2-1
install -d "$dev" "$FAKE/models" "$ETC"
printf '8086\n' >"$dev/idVendor"
printf '0b3a\n' >"$dev/idProduct"
printf '5000\n' >"$dev/speed"
head -c 4096 /dev/urandom >"$FAKE/models/yolo_hand.bin"
head -c 1024 /dev/urandom >"$FAKE/models/snn.bin"
(cd "$FAKE/models" && sha256sum "$PWD/yolo_hand.bin" "$PWD/snn.bin") >"$ETC/weights.sha256"
chmod -R a+rX "$FAKE"
install -m 0644 "$HERE/ros_mcp.json" "$HERE/picoclaw.json" "$ETC/"

env_file=$HERE/fake.env
edits=()
if [[ -n $picoclaw ]]; then
  home=$FAKE/picoclaw
  install -d -o neurobot -g neurobot "$home" "$home/workspace"
  install -m 0755 "$picoclaw" "$FAKE/picoclaw-bin"
  install -o neurobot -g neurobot -m 0600 "$HERE/picoclaw.json" "$home/config.json"
  edits+=(-e "s#^NEUROBOT_PICOCLAW_CMD=.*#NEUROBOT_PICOCLAW_CMD=\"$FAKE/picoclaw-bin --no-color gateway\"#"
    -e "s#^NEUROBOT_PICOCLAW_VERSION=.*#NEUROBOT_PICOCLAW_VERSION=#" -e "s#^PICOCLAW_CONFIG=.*#PICOCLAW_HOME=$home#")
fi
if [[ -n $domain ]]; then
  edits+=(-e "s#^ROS_DOMAIN_ID=.*#ROS_DOMAIN_ID=$domain#")
fi
if ((${#edits[@]})); then
  env_file=$FAKE/neurobot.env
  sed "${edits[@]}" "$HERE/fake.env" >"$env_file"
fi
"$HERE/../install.sh" --env "$env_file" --no-journald
if [[ -n $picoclaw ]]; then
  neurobot picoclaw-config merge
fi
