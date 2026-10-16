#!/usr/bin/env bash
# Снимок файлов /proc и cgroup v2 для дерева процессов — фикстура для тестов sysmon на macOS.
#
# Запуск на Linux (плата, Lima VM):
#   scripts/capture_fixture.sh <root_pid> <out_dir> [cgroup_dir]
# Результат: <out_dir>/proc/<pid>/{stat,status,io,smaps_rollup,smaps,task/<tid>/status},
#            <out_dir>/proc/net/dev, <out_dir>/proc/<root_pid>/net/dev, <out_dir>/proc/{meminfo,stat,loadavg},
#            <out_dir>/sys/fs/cgroup/<...>/{memory.*,cpu.stat,io.stat,cgroup.procs}, <out_dir>/sys/class/thermal/...
#            и <out_dir>/MANIFEST (ядро, архитектура, размер страницы, CLK_TCK, дата).
# Файлы /proc читаются через cat: у них нулевой размер в stat(2), tar сохранил бы пустые файлы.
set -euo pipefail

root_pid=${1:?root pid}
out=${2:?out dir}
cg=${3:-}

mkdir -p "$out/proc/net"

# Все потомки root_pid (обход по ppid из /proc/*/stat; поле comm может содержать пробелы и скобки).
descendants() {
  local -A children=()
  local stat pid ppid rest
  for stat in /proc/[0-9]*/stat; do
    rest=$(cat "$stat" 2>/dev/null) || continue
    pid=${rest%% *}
    rest=${rest##*) }
    ppid=$(echo "$rest" | cut -d' ' -f2)
    children[$ppid]+="$pid "
  done
  local queue=("$1") cur
  while ((${#queue[@]})); do
    cur=${queue[0]}; queue=("${queue[@]:1}")
    echo "$cur"
    for c in ${children[$cur]:-}; do queue+=("$c"); done
  done
}

for pid in $(descendants "$root_pid"); do
  d="$out/proc/$pid"
  mkdir -p "$d"
  for f in stat status io smaps_rollup smaps cgroup; do
    cat "/proc/$pid/$f" > "$d/$f" 2>/dev/null || rm -f "$d/$f"
  done
  for t in /proc/"$pid"/task/*; do
    tid=${t##*/}
    mkdir -p "$d/task/$tid"
    cat "$t/status" > "$d/task/$tid/status" 2>/dev/null || true
  done
done

cat /proc/net/dev > "$out/proc/net/dev"
mkdir -p "$out/proc/$root_pid/net"
cat "/proc/$root_pid/net/dev" > "$out/proc/$root_pid/net/dev"
for f in meminfo stat loadavg; do cat "/proc/$f" > "$out/proc/$f"; done

if [[ -n "$cg" ]]; then
  rel=${cg#/sys/fs/cgroup}
  dst="$out/sys/fs/cgroup$rel"
  mkdir -p "$dst"
  for f in cgroup.procs memory.current memory.peak memory.max memory.stat memory.events cpu.stat io.stat; do
    cat "$cg/$f" > "$dst/$f" 2>/dev/null || true
  done
fi

for z in /sys/class/thermal/thermal_zone*; do
  [[ -e "$z/temp" ]] || continue
  dz="$out/sys/class/thermal/${z##*/}"
  mkdir -p "$dz"
  cat "$z/type" > "$dz/type" 2>/dev/null || true
  cat "$z/temp" > "$dz/temp" 2>/dev/null || true
done

{
  echo "kernel=$(uname -r)"
  echo "arch=$(uname -m)"
  echo "page_size=$(getconf PAGESIZE)"
  echo "clk_tck=$(getconf CLK_TCK)"
  echo "root_pid=$root_pid"
  echo "cgroup=$cg"
  echo "captured=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$out/MANIFEST"
echo "captured $(descendants "$root_pid" | wc -l) processes into $out"
