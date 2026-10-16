#!/usr/bin/env bash
# Наборы «датчики работают» и «безопасность» на двойнике с пакетом neurobot целиком — с хоста (README §3).
#   run.sh --out КАТАЛОГ [--before КАТАЛОГ_SAFETY] [--domain 23] [--keep]
# --out — новый каталог результатов на хосте (существующий не перезаписывается); --before — прежний код safety_gate
# (каталог neurobot/safety) для прогона «до» правила (А): ставится на время сценария start_poses вместо нынешнего;
# --domain — ROS_DOMAIN_ID пакета; --keep — не снимать пакет в конце. Порядок: setup_twin.sh → neurobot.target →
# doctor → датчики (lab) → [«до»] → безопасность (lab) → step → ramp (+ наклон) → hold (kill -9 и SIGSTOP
# safety_gate) → lab → журналы и хеши → limactl copy КАТАЛОГ_ВМ → --out. Код 1 — набор не прошёл, 2 — ошибка
# аргументов или стенда.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$HERE/../../../scripts/sim/lib.sh"
VMOUT=/var/tmp/nb-integ/run
out="" before="" domain=23 keep=0
while (($#)); do
  case $1 in
    --out) out=$2; shift 2 ;;
    --before) before=$(cd "$2" && pwd); shift 2 ;;
    --domain) domain=$2; shift 2 ;;
    --keep) keep=1; shift ;;
    *) echo "run.sh: аргументы: --out КАТАЛОГ [--before КАТАЛОГ] [--domain N] [--keep]" >&2; exit 2 ;;
  esac
done
[[ -n $out && ! -e $out ]] || { echo "run.sh: --out — новый каталог (не перезаписываем: $out)" >&2; exit 2; }

py() { vm "$1" "sudo bash -c 'source /opt/ros/jazzy/setup.bash; export PYTHONDONTWRITEBYTECODE=1; cd /var/tmp; python3 $HERE/$2'"; }
rc=0
vm 30 "sudo rm -rf $VMOUT && sudo mkdir -p $VMOUT"
vm 900 "sudo bash $HERE/setup_twin.sh --domain $domain" >&2
vm 60 "sudo systemctl start --no-block neurobot.target"
vm 200 "for i in \$(seq 1 60); do sleep 3; sudo neurobot doctor >/dev/null 2>&1 && exit 0; done; sudo neurobot doctor; exit 1"
vm 60 "sudo bash -c 'neurobot doctor > $VMOUT/doctor_start.txt 2>&1'; cat $VMOUT/doctor_start.txt" >&2
py 900 "sensors.py --out $VMOUT" || rc=1
if [[ -n $before ]]; then
  limactl copy -r "$before" "$SIM_VM:/var/tmp/nb-integ/safety_before"
  vm 60 "sudo cp /var/tmp/nb-integ/safety_before/*.py /opt/neurobot/src/neurobot/safety/ && sudo systemctl restart neurobot-safety"
  py 600 "safety.py --out $VMOUT --label before start_poses" || true
  vm 60 "sudo cp /var/lib/neurobot-twin/src/neurobot/safety/*.py /opt/neurobot/src/neurobot/safety/ && \
    sudo systemctl restart neurobot-safety"
fi
py 3600 "safety.py --out $VMOUT --label after" || rc=1
vm 200 "sudo bash $HERE/twin_world.sh step" >&2
py 600 "safety.py --out $VMOUT --label after step_edge" || rc=1
vm 200 "sudo bash $HERE/twin_world.sh ramp" >&2
py 300 "sensors.py --out $VMOUT slope" || rc=1
py 900 "safety.py --out $VMOUT --label after ramp_drive slope_latch" || rc=1
vm 200 "sudo bash $HERE/twin_world.sh lab --hold" >&2
py 600 "safety.py --out $VMOUT --label hold kill_gate freeze_gate" || rc=1
vm 200 "sudo bash $HERE/twin_world.sh lab" >&2
vm 60 "sudo bash -c 'neurobot doctor > $VMOUT/doctor_end.txt 2>&1; cp /var/lib/neurobot-twin/snapshot.sha256 \
  /var/lib/neurobot-twin/twin.env /etc/neurobot/neurobot.env $VMOUT/; journalctl -u \"neurobot*\" --no-pager \
  -o short-iso-precise > $VMOUT/journal.txt; chmod -R a+rX $VMOUT'"
limactl copy -r "$SIM_VM:$VMOUT" "$out"
if ((!keep)); then vm 300 "sudo bash $HERE/setup_twin.sh --teardown" >&2; fi
exit $rc
