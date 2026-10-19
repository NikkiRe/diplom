#!/usr/bin/env bash
# (в ВМ sim, root) Мир двойника для пакета, поставленного setup_twin.sh: drop-in юнита neurobot-driver@twin в /run
# (исчезает при перезагрузке ВМ) подменяет команду драйвера, затем перезапуск neurobot.target и ожидание doctor «ОК».
#   sudo twin_world.sh lab|step|ramp|hall [--hold]  --hold — cmd_vel_timeout 0: база держит последнюю команду (отказ
#                                                 controller_manager при STM32, держащем уставку, — RS8); hall — зал
#                                                 9 × 9 м прогонов A0/A1 (neurobot/sim/runs/worlds/hall.sdf)
# Аварийная защёлка safety_gate перезапуск переживает (StateDirectory) — снимает её человек. Код 2 — ошибка аргументов.
set -euo pipefail

TW=/var/lib/neurobot-twin
DROP=/run/systemd/system/neurobot-driver@twin.service.d
ENVF=/run/neurobot-twin-world.env

die() { echo "twin_world: $*" >&2; exit 2; }
[[ $EUID -eq 0 ]] || die "нужен root"
[[ ${1:-} =~ ^(lab|step|ramp|hall)$ ]] || die "аргументы: lab|step|ramp|hall [--hold]"
world=$1 hold=${2:-}
arg=$world
[[ $world == hall ]] && arg=$TW/src/neurobot/sim/runs/worlds/hall.sdf
[[ -z $hold || $hold == --hold ]] || die "неизвестный аргумент: $hold"
[[ -f $TW/twin.env ]] || die "пакет на двойнике не поставлен (setup_twin.sh)"

rm -f "$ENVF" "$DROP/world.conf"
if [[ $world != lab || -n $hold ]]; then
  launch="world:=$arg"
  [[ -n $hold ]] && launch+=" cmd_vel_timeout:=0"
  grep '^NEUROBOT_DRIVER_twin_CMD=' "$TW/twin.env" | sed "s#world:=[a-z]*\( cmd_vel_timeout:=0\)\{0,1\}#$launch#" >"$ENVF"
  install -d "$DROP"
  printf '[Service]\nEnvironmentFile=%s\n' "$ENVF" >"$DROP/world.conf"
fi
systemctl daemon-reload
systemctl restart neurobot.target
for _ in $(seq 1 40); do
  sleep 3
  if neurobot doctor >/dev/null 2>&1; then
    echo "twin_world: мир $world${hold:+ (hold)}, doctor — ОК" >&2
    exit 0
  fi
done
neurobot doctor >&2 || true
die "doctor не дал «ОК» за 120 с (мир $world)"
