#!/usr/bin/env bash
# Установка nanobot 0.3.5 (PyPI nanobot-ai, HKUDS/nanobot, MIT) в отдельный venv (uv, Python 3.12):
#   bench/baselines/install_nanobot.sh            # Mac: <проект>/.venv-nanobot
#   bench/baselines/install_nanobot.sh --vm sbc   # Lima VM: ~/.venv-nanobot (uv из ~/.local/bin, системный Python не трогается)
# Если рядом есть lock-файл requirements/nanobot-<host>.lock — ставится ровно он (uv pip sync), иначе
# nanobot-ai==0.3.5 с разрешением зависимостей и lock записывается (uv pip freeze).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VER=0.3.5
if [[ "${1:-}" == "--vm" ]]; then
  VM="${2:-sbc}"
  LOCK="$HERE/requirements/nanobot-$VM.lock"
  limactl shell --workdir / "$VM" -- bash -lc "
    set -euo pipefail
    UV=\$HOME/.local/bin/uv; command -v \$UV >/dev/null || UV=uv
    [[ -x \$HOME/.venv-nanobot/bin/python ]] || \$UV venv --python 3.12 \$HOME/.venv-nanobot
    if [[ -f '$LOCK' ]]; then \$UV pip sync --python \$HOME/.venv-nanobot/bin/python '$LOCK';
    else \$UV pip install --python \$HOME/.venv-nanobot/bin/python 'nanobot-ai==$VER'; fi
    \$HOME/.venv-nanobot/bin/nanobot --version
    \$UV pip freeze --python \$HOME/.venv-nanobot/bin/python > /tmp/nanobot-freeze.txt"
  mkdir -p "$HERE/requirements"
  [[ -f "$LOCK" ]] || limactl shell --workdir / "$VM" -- cat /tmp/nanobot-freeze.txt > "$LOCK"
  echo "nanobot $VER OK in $VM:~/.venv-nanobot (lock: $LOCK)"
  exit 0
fi
LOCK="$HERE/requirements/nanobot-mac.lock"
V="$ROOT/.venv-nanobot"
[[ -x "$V/bin/python" ]] || uv venv --python 3.12 "$V"
if [[ -f "$LOCK" ]]; then uv pip sync --python "$V/bin/python" "$LOCK"; else uv pip install --python "$V/bin/python" "nanobot-ai==$VER"; fi
"$V/bin/nanobot" --version
mkdir -p "$HERE/requirements"
[[ -f "$LOCK" ]] || uv pip freeze --python "$V/bin/python" > "$LOCK"
echo "nanobot $VER OK in $V (lock: $LOCK)"
