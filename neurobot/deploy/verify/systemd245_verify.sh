#!/usr/bin/env bash
# Юниты neurobot глазами systemd 245 (Ubuntu 20.04) на ВМ с более новым systemd: systemd-analyze из пакета focal
# (ports.ubuntu.com, arm64, sha256 сверяется), распакованный во временный каталог, проверяет установленные юниты.
# Новый systemd-analyze не годится: он знает и директивы, которых в 245 нет (RestartSteps и т. п.).
#   sudo verify/systemd245_verify.sh [КАТАЛОГ_РАБОТЫ]     по умолчанию /var/tmp/sd245
# Печатает замечания 245 к юнитам neurobot (пусто — замечаний нет) и контроль: ключ RestartSteps 245 обязан
# отвергнуть. Замечания 245 к системным юнитам самой ВМ (их пишет systemd 255) отбрасываются.
# Коды: 0 — замечаний нет и контроль сработал, 1 — есть замечания, 2 — ошибка окружения.
set -euo pipefail

DEB=systemd_245.4-4ubuntu3.24_arm64.deb
SHA=e6c77177cb171e48c1c173619844baa9170155b4f6df851ece024a6376bdac3f
WORK=${1:-/var/tmp/sd245}
UNIT_DIR=/etc/systemd/system

die() { echo "systemd245_verify: $*" >&2; exit 2; }
[[ $(dpkg --print-architecture) == arm64 ]] || die "пакет — для arm64"

mkdir -p "$WORK"
if [[ ! -x $WORK/root/usr/bin/systemd-analyze ]]; then
  curl -fsSL --max-time 120 -o "$WORK/$DEB" "http://ports.ubuntu.com/ubuntu-ports/pool/main/s/systemd/$DEB"
  echo "$SHA  $WORK/$DEB" | sha256sum -c --quiet || die "sha256 $DEB не совпал"
  dpkg-deb -x "$WORK/$DEB" "$WORK/root"
fi
analyze() { LD_LIBRARY_PATH=$WORK/root/lib/systemd SYSTEMD_COLORS=0 "$WORK/root/usr/bin/systemd-analyze" "$@" 2>&1; }
analyze --version | head -1

units=()
for f in "$UNIT_DIR"/neurobot.target "$UNIT_DIR"/neurobot-*.service; do
  if [[ -f $f ]]; then units+=("$(basename "${f/%@.service/@verify.service}")"); fi
done
((${#units[@]})) || die "юниты neurobot не установлены в $UNIT_DIR"
notes=$(cd "$UNIT_DIR" && analyze verify "${units[@]}" | grep -E 'neurobot' || true)

control=$WORK/control
mkdir -p "$control"
printf '[Service]\nExecStart=/bin/true\nRestartSteps=3\n' >"$control/nb-control.service"
[[ $(analyze verify "$control/nb-control.service") == *"Unknown key name 'RestartSteps'"* ]] || die "контроль не сработал"

echo "юниты: ${units[*]}"
if [[ -n $notes ]]; then
  echo "$notes"
  exit 1
fi
echo "systemd 245: замечаний к юнитам neurobot нет; контроль (RestartSteps) отвергнут"
