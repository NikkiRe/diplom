#!/usr/bin/env bash
# Установка ros-mcp-server (robotmcp, PyPI ros-mcp 3.1.2, Apache-2.0) в отдельный venv пользователя.
#   scripts/sim/install_rosmcp.sh mac   → <проект>/.venv-rosmcp (uv из Homebrew, CPython 3.12)
#   scripts/sim/install_rosmcp.sh sbc   → ~/.venv-rosmcp внутри VM "sbc" (системный python3 3.12 VM не меняется:
#                                         uv кладётся в ~/.local/bin пользователя, venv — в домашний каталог)
# Состав закреплён и ставится `uv pip sync` (ровно эти пакеты, без разрешения зависимостей):
#   rosmcp/requirements.lock      — macOS arm64 (uv pip freeze, 83 пакета);
#   rosmcp/requirements-sbc.lock  — Linux aarch64: + jeepney/secretstorage (зависимости keyring на Linux) и
#                                   opencv-python-headless вместо opencv-python (см. ниже), 85 пакетов.
# cbor2 6.1.5 — только для прототипа R-compact (robot_mcp_compact.py: /scan в CBOR), ros-mcp он не нужен.
# Лог — experiments/results/robot_smoke_<дата>/install_rosmcp_<цель>.log. Идемпотентно.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
TARGET="${1:-mac}"
LOCK="$SIM_DIR/rosmcp/requirements.lock"
LOCK_SBC="$SIM_DIR/rosmcp/requirements-sbc.lock"
UV_VER=0.12.23
UV_SHA=6524bd338177ed50d035d39354e12545e993bbeba2ecbddf0480c5b3a81d313f   # uv-aarch64-unknown-linux-gnu.tar.gz
LOGDIR="$PROJ/experiments/results/robot_smoke_$(date +%Y%m%d)"; mkdir -p "$LOGDIR"
LOG="$LOGDIR/install_rosmcp_$TARGET.log"
case "$TARGET" in
  mac)
    {
      [ -x "$PROJ/.venv-rosmcp/bin/python" ] || to 300 uv venv --python 3.12 "$PROJ/.venv-rosmcp"
      to 900 uv pip sync --python "$PROJ/.venv-rosmcp/bin/python" "$LOCK"
      "$PROJ/.venv-rosmcp/bin/python" -c 'import importlib.metadata as m; print("ros-mcp", m.version("ros-mcp"), "fastmcp", m.version("fastmcp"), "mcp", m.version("mcp"))'
      du -sh "$PROJ/.venv-rosmcp"
    } 2>&1 | tee "$LOG"
    ;;
  sbc)
    # VM "sbc" монтирует ~ хоста только на чтение: lock и скрипты читаются по тому же пути.
    to 1200 limactl shell --workdir / sbc -- bash -s "$UV_VER" "$UV_SHA" "$LOCK_SBC" <<'EOS' 2>&1 | tee "$LOG"
set -euo pipefail
UV_VER="$1"; UV_SHA="$2"; LOCK="$3"
mkdir -p ~/.local/bin
if ! ~/.local/bin/uv --version 2>/dev/null | grep -q "$UV_VER"; then
  tmp=$(mktemp -d)
  curl -fsSL --max-time 120 -o "$tmp/uv.tgz" "https://github.com/astral-sh/uv/releases/download/$UV_VER/uv-aarch64-unknown-linux-gnu.tar.gz"
  echo "$UV_SHA  $tmp/uv.tgz" | sha256sum -c -
  tar -xzf "$tmp/uv.tgz" -C "$tmp"
  install -m 0755 "$tmp"/uv-aarch64-unknown-linux-gnu/uv ~/.local/bin/uv
  rm -rf "$tmp"
fi
~/.local/bin/uv --version
[ -x ~/.venv-rosmcp/bin/python ] || ~/.local/bin/uv venv --python /usr/bin/python3 ~/.venv-rosmcp
# opencv-python (зависимость ros-mcp) тянет libGL.so.1 (пакет libgl1), которого на безголовой плате нет: ros-mcp
# падает при импорте cv2. Системные пакеты платы не трогаем — в lock стоит opencv-python-headless той же версии
# (тот же модуль cv2). Без кэша uv в ~ (на плате диск дорог).
timeout 900 nice -n 10 ~/.local/bin/uv pip sync --no-cache --python ~/.venv-rosmcp/bin/python "$LOCK"
~/.venv-rosmcp/bin/python -c 'import cv2, cbor2; print("cv2", cv2.__version__)'
~/.venv-rosmcp/bin/python -c 'import importlib.metadata as m; print("ros-mcp", m.version("ros-mcp"), "fastmcp", m.version("fastmcp"), "mcp", m.version("mcp"))'
du -sh ~/.venv-rosmcp; df -h / | tail -1
EOS
    ;;
  *) echo "цель: mac | sbc" >&2; exit 2;;
esac
