#!/usr/bin/env bash
# Тесты обёртки ros2 launch (test_deploy_launch.py) против launch из Foxy: исходники launch 0.10 (ветка foxy, чистый
# Python) и ament_index_python — в КАТАЛОГ, рядом префикс ament с grammar.lark; Python 3.8, как на роботе (uv).
# ROS 2 Foxy целиком не нужен: обёртка опирается только на пакет launch.
#   foxy_launch.sh КАТАЛОГ        клонирует (если нет) и запускает тесты; код — как у pytest
set -euo pipefail

[[ $# -eq 1 ]] || { echo "использование: foxy_launch.sh КАТАЛОГ" >&2; exit 2; }
dir=$1
root=$(cd "$(dirname "$0")/../../.." && pwd)
mkdir -p "$dir"
for repo in ros2/launch ament/ament_index; do
  name=${repo#*/}
  [[ -d $dir/$name ]] || git clone -q --depth 1 -b foxy "https://github.com/$repo.git" "$dir/$name"
done
prefix=$dir/prefix
mkdir -p "$prefix/share/ament_index/resource_index/packages" "$prefix/share/launch/frontend"
touch "$prefix/share/ament_index/resource_index/packages/launch"
cp "$dir/launch/launch/share/launch/frontend/grammar.lark" "$prefix/share/launch/frontend/"
echo "launch $(git -C "$dir/launch" log -1 --format='%h %cs') ($(grep -o '<version>[^<]*' "$dir/launch/launch/package.xml" | cut -c10-))" >&2
cd "$root"
AMENT_PREFIX_PATH=$prefix PYTHONPATH=$dir/launch/launch:$dir/ament_index/ament_index_python PYTHONDONTWRITEBYTECODE=1 \
  uv run --python 3.8 --no-project --with osrf_pycommon --with lark-parser --with pyyaml --with setuptools \
  --with pytest python -m pytest -q -p no:cacheprovider neurobot/tests/test_deploy_launch.py
