#!/usr/bin/env bash
# Установка пакета запуска neurobot: юниты systemd, CLI neurobot, исполнитель и safety_gate (код рядом с deploy),
# лимиты journald, ротация журнала PicoClaw (README.md, RUNBOOK.md §1).
#   sudo ./install.sh --env ФАЙЛ [--no-journald] [--no-enable]   установить; ФАЙЛ копируется в /etc/neurobot/neurobot.env
#   sudo ./install.sh [--no-journald] [--no-enable]               переустановить с уже лежащим /etc/neurobot/neurobot.env
#   sudo ./install.sh --uninstall                                 остановить, выключить и убрать юниты и CLI
#   --no-journald — не ставить drop-in journald (лимиты, журнал на диске) и не перезапускать journald;
#   --no-enable — не включать юниты в автозапуск (systemctl enable), только поставить.
# Прежние юниты, env-файл, drop-in journald и правило logrotate перед заменой или удалением переносятся в
# /var/backups/neurobot/<время>/ (откат — RUNBOOK.md §8). Конфиги в /etc/neurobot и состояние в /var/lib/neurobot
# (аварийная защёлка safety_gate, метки wait-graph) при удалении остаются.
# Запуск после установки — вручную: sudo systemctl start neurobot.target.
# Коды: 0 — успех, 2 — ошибка аргументов или окружения.
set -euo pipefail

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ETC=/etc/neurobot
ENV_DST=$ETC/neurobot.env
UNIT_DIR=/etc/systemd/system
JOURNALD_CONF=/etc/systemd/journald.conf.d/50-neurobot.conf
LOGROTATE=/etc/logrotate.d/neurobot-picoclaw
LINK=/usr/local/bin/neurobot
BACKUP=/var/backups/neurobot/$(date +%Y%m%d-%H%M%S)

die() { echo "install: $*" >&2; exit 2; }

# CLI из исходников пакета (до установки), env-файл — явный
nb() { PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SRC" python3 -m neurobot_deploy --env "$1" "${@:2}"; }

# перенести файл или каталог в резервную копию с сохранением пути
stash() {
  local path
  for path in "$@"; do
    if [[ -e $path || -L $path ]]; then
      mkdir -p "$BACKUP$(dirname "$path")"
      mv "$path" "$BACKUP$path"
    fi
  done
}

installed_units() {
  local f
  for f in "$UNIT_DIR"/neurobot.target "$UNIT_DIR"/neurobot-*.service "$UNIT_DIR"/neurobot.target.wants; do
    if [[ -e $f ]]; then echo "$f"; fi
  done
}

do_install() {
  local env_src=$1 journald=$2 enable=$3 prefix units comps pkg moved=""
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  if [[ -n $env_src ]]; then
    [[ -f $env_src ]] || die "нет env-файла $env_src"
    nb "$env_src" render --out "$TMP/check" >/dev/null || die "env-файл $env_src не годится (причина выше)"
    if [[ -f $ENV_DST ]] && ! cmp -s "$env_src" "$ENV_DST"; then stash "$ENV_DST"; fi
    install -D -m 0644 "$env_src" "$ENV_DST"
  fi
  [[ -f $ENV_DST ]] || die "нет $ENV_DST: укажите --env ФАЙЛ (образец — neurobot.env.example)"
  prefix=$(nb "$ENV_DST" env-get NEUROBOT_PREFIX)
  comps=" $(nb "$ENV_DST" env-get NEUROBOT_COMPONENTS) "
  units=$(nb "$ENV_DST" render --out "$TMP/units")
  nb "$ENV_DST" render --units "$SRC/logrotate" --out "$TMP/logrotate" >/dev/null

  mapfile -t old < <(installed_units)
  if ((${#old[@]})); then
    stash "${old[@]}"
    moved=", прежние — в $BACKUP"
  fi
  rm -rf "$prefix/lib/neurobot_deploy" "$prefix/lib/fakes" "$prefix/lib/picoclaw" "$prefix/src/neurobot/executor" \
    "$prefix/src/neurobot/safety"
  install -d -m 0755 "$prefix/bin" "$prefix/lib" "$prefix/share" "$prefix/src/neurobot"
  cp -R "$SRC/neurobot_deploy" "$SRC/fakes" "$SRC/picoclaw" "$prefix/lib/"
  # исполнитель и safety_gate (neurobot/executor, neurobot/safety рядом с пакетом): python3 -m neurobot.<имя> с
  # PYTHONPATH=$prefix/src; без кода компонент не стартует — предупредить сразу
  for pkg in executor safety; do
    if [[ -d $SRC/../$pkg ]]; then
      cp -R "$SRC/../$pkg" "$prefix/src/neurobot/"
    elif [[ $comps == *" $pkg "* ]]; then
      echo "install: нет $SRC/../$pkg — $pkg запустится, только если NEUROBOT_${pkg^^}_CMD указывает на код" >&2
    fi
  done
  find "$prefix/lib" "$prefix/src" -name __pycache__ -prune -exec rm -rf {} +
  install -m 0755 "$SRC/bin/neurobot" "$SRC/bin/neurobot-run" "$prefix/bin/"
  install -m 0644 "$SRC/README.md" "$SRC/RUNBOOK.md" "$prefix/share/"
  install -D -m 0644 "$SRC/launchfiles/supervised.launch.py" "$prefix/share/launchfiles/supervised.launch.py"
  install -m 0644 "$TMP/units/"* "$UNIT_DIR/"
  ln -sfn "$prefix/bin/neurobot" "$LINK"
  stash "$LOGROTATE"
  if [[ $comps == *" picoclaw "* ]]; then
    install -m 0644 "$TMP/logrotate/neurobot-picoclaw" "$LOGROTATE"
  fi

  if ((journald)); then
    install -D -m 0644 "$SRC/journald/50-neurobot.conf" "$JOURNALD_CONF"
    systemctl restart systemd-journald
  fi
  systemctl daemon-reload
  if ((enable)); then
    # shellcheck disable=SC2086
    systemctl enable $units
  fi
  echo "install: установлено в $prefix, юниты — в $UNIT_DIR$moved" >&2
  echo "install: запуск — sudo systemctl start neurobot.target; проверка — neurobot doctor" >&2
}

do_uninstall() {
  local prefix=/opt/neurobot
  if [[ -f $ENV_DST ]]; then prefix=$(nb "$ENV_DST" env-get NEUROBOT_PREFIX); fi
  systemctl stop neurobot.target 'neurobot-*' || true
  systemctl disable neurobot.target 2>/dev/null || true
  mapfile -t old < <(installed_units)
  stash "${old[@]}" "$JOURNALD_CONF" "$LOGROTATE"
  rm -f "$LINK"
  rm -rf "$prefix/bin/neurobot" "$prefix/bin/neurobot-run" "$prefix/lib/neurobot_deploy" "$prefix/lib/fakes" \
    "$prefix/lib/picoclaw" "$prefix/src/neurobot/executor" "$prefix/src/neurobot/safety" "$prefix/share/README.md" \
    "$prefix/share/RUNBOOK.md" "$prefix/share/launchfiles"
  if [[ -d $prefix ]]; then find "$prefix" -depth -type d -empty -delete; fi
  systemctl daemon-reload
  systemctl reset-failed 'neurobot-*' 2>/dev/null || true
  if [[ -d $BACKUP ]]; then systemctl restart systemd-journald; fi
  echo "install: neurobot выключен; юниты, drop-in journald и logrotate — в $BACKUP, конфиги $ETC и состояние" \
    "/var/lib/neurobot (защёлка, метки wait-graph) оставлены" >&2
}

args=("$@") env_src="" journald=1 enable=1 uninstall=0
while (($#)); do
  case $1 in
    --env) [[ $# -ge 2 ]] || die "--env: нужен путь"; env_src=$(realpath "$2"); shift 2 ;;
    --no-journald) journald=0; shift ;;
    --no-enable) enable=0; shift ;;
    --uninstall) uninstall=1; shift ;;
    -h | --help) sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "неизвестный аргумент: $1 (--help)" ;;
  esac
done
[[ $EUID -eq 0 ]] || die "нужен root: sudo $0 ${args[*]}"
command -v python3 >/dev/null || die "нет python3"
if ((uninstall)); then do_uninstall; else do_install "$env_src" "$journald" "$enable"; fi
