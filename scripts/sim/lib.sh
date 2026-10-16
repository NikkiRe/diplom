# Общие функции хостовых скриптов сценария «робот» (source scripts/sim/lib.sh).
SIM_VM="${SIM_VM:-sim}"
SIM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$(cd "$SIM_DIR/../.." && pwd)"
# Таймаут без coreutils: на macOS нет timeout(1). to <сек> <команда…>
to() { local s="$1"; shift; perl -e 'alarm shift; exec @ARGV or die "exec: $!"' "$s" "$@"; }
# Выполнить команду bash в VM (с таймаутом).
vm() { local s="$1"; shift; to "$s" limactl shell --workdir / "$SIM_VM" -- bash -lc "$*"; }
