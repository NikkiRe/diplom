#!/usr/bin/env bash
# Проверка ROS-части collect_diag.sh на настоящем ROS 2 (ВМ sim, Jazzy):
#   limactl shell --workdir / sim -- bash "$PWD/neurobot/lab/tests/collect_diag_ros_test.sh"
#
# В отдельном домене DDS (NB_LAB_DOMAIN или первый из 87–101 без чужих узлов и тем: в ВМ работают тесты и стенды
# других компонентов, иногда в том же домене; чужие графы не затрагиваются) публикуются /scan как на
# роботе (683 луча, ±119,9°, best effort, блок NaN на индексах 168–353 — ANALYSIS RS3) и /cmd_vel, работают узлы
# с параметрами /diff_cont и /twist_mux; сбор запускается с этим доменом и пустым HOME. Проверяется: темы с типами,
# узлы, издатели и QoS (topic info -v), сводка /scan по секторам (блок NaN прямо по курсу найден, массива в архиве
# нет), параметры /diff_cont и /twist_mux (param dump без --print — Jazzy), пометки про отсутствующие /odom,
# /cmd_vel_tracker и /controller_manager, служебный ros2 daemon домена поднят и остановлен, журналы ros2 CLI не
# остались в ~/.ros/log, ошибок в отчёте нет; если в ВМ установлен шаблон юнита робота (neurobot-driver@.service
# пакета развёртывания), у него снимается только файл.
set -euo pipefail

lab=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
setup=$(compgen -G '/opt/ros/*/setup.bash' | head -n 1)
[[ -n $setup ]] || { echo "нет /opt/ros/*/setup.bash — тест только для ВМ с ROS 2"; exit 2; }
set +u
# shellcheck disable=SC1090
source "$setup"
set -u
# Как в ВМ sim (scripts/sim/vm_env.sh), но в своём домене; всё задано явно, чтобы сбор не взял значения
# из окружения чужих процессов ROS 2 в этой ВМ.
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI=file://$lab/../../scripts/sim/cyclonedds.xml PYTHONDONTWRITEBYTECODE=1
free_domain() {  # первый домен без чужих узлов и тем (смотрит узел rclpy 3 с, ros2 daemon не поднимается)
  local dom
  for dom in ${NB_LAB_DOMAIN:-$(seq 87 101)}; do
    ROS_DOMAIN_ID=$dom python3 - << 'EOF' && echo "$dom" && return
import sys
import time

import rclpy

rclpy.init()
node = rclpy.create_node("_nb_lab_probe")
end = time.monotonic() + 3
while time.monotonic() < end:
    rclpy.spin_once(node, timeout_sec=0.2)
others = [n for n in node.get_node_names() if n != "_nb_lab_probe"]
topics = [t for t, _ in node.get_topic_names_and_types() if t not in ("/rosout", "/parameter_events")]
sys.exit(1 if others or topics else 0)
EOF
  done
  return 1
}
ROS_DOMAIN_ID=$(free_domain) || { echo "нет свободного домена DDS среди ${NB_LAB_DOMAIN:-87–101}"; exit 2; }
export ROS_DOMAIN_ID
echo "домен DDS $ROS_DOMAIN_ID"
T=$(mktemp -d /tmp/nb-lab-ros-test-XXXXXX)
mkdir -p "$T/home"
pids=()
cleanup() {
  set +e  # уборка до конца, даже если что-то уже остановлено
  ((${#pids[@]})) && kill "${pids[@]}" 2> /dev/null && wait "${pids[@]}" 2> /dev/null
  rm -rf "$T"
}
trap cleanup EXIT

fails=0
ok() { echo "ok   $*"; }
fail() { echo "FAIL $*"; fails=$((fails + 1)); }
expect() {
  local what=$1
  shift
  if "$@" > /dev/null 2>&1; then ok "$what"; else fail "$what"; fi
}

if ros2 daemon status | grep -q 'daemon is running'; then
  echo "в домене $ROS_DOMAIN_ID уже есть ros2 daemon — выберите другой NB_LAB_DOMAIN"
  exit 2
fi
ranges=$(python3 -c 'print(", ".join(".nan" if 168 <= i <= 353 else "2.0" for i in range(683)))')
ros2 topic pub -r 10 --qos-reliability best_effort /scan sensor_msgs/msg/LaserScan \
  "{header: {frame_id: laser_frame}, angle_min: -2.0923497677, angle_max: 2.0923497677,
    angle_increment: 0.0061359231, range_min: 0.02, range_max: 5.6, ranges: [$ranges]}" > /dev/null 2>&1 &
pids+=($!)
ros2 topic pub -r 10 /cmd_vel geometry_msgs/msg/Twist '{}' > /dev/null 2>&1 &
pids+=($!)
python3 - > /dev/null 2>&1 << 'EOF' &
import rclpy
from rclpy.executors import SingleThreadedExecutor

rclpy.init()
diff_cont = rclpy.create_node("diff_cont")
for name, value in (("cmd_vel_timeout", 0.5), ("use_stamped_vel", False), ("wheel_separation", 0.38),
                    ("wheel_radius", 0.036)):
    diff_cont.declare_parameter(name, value)
twist_mux = rclpy.create_node("twist_mux")
for name, value in (("topics.tracker.topic", "/cmd_vel_tracker"), ("topics.tracker.priority", 20),
                    ("topics.tracker.timeout", 0.5)):
    twist_mux.declare_parameter(name, value)
executor = SingleThreadedExecutor()
executor.add_node(diff_cont)
executor.add_node(twist_mux)
executor.spin()
EOF
pids+=($!)
# Ждём издателей и узлы (без ros2 daemon): под нагрузкой «ros2 topic pub» поднимается дольше нескольких секунд.
python3 - << 'EOF'
import time

import rclpy

rclpy.init()
node = rclpy.create_node("_nb_lab_wait")
deadline = time.monotonic() + 60
while time.monotonic() < deadline and not (
        node.count_publishers("/scan") and node.count_publishers("/cmd_vel")
        and {"diff_cont", "twist_mux"} <= set(node.get_node_names())):
    rclpy.spin_once(node, timeout_sec=0.5)
EOF

set +e
HOME=$T/home bash "$lab/collect_diag.sh" --ros-setup "$setup" --tegrastats-sec 0 --out "$T" > "$T/stdout" 2> "$T/stderr"
rc=$?
set -e
expect "код выхода 0 (получен $rc)" test "$rc" -eq 0
d=$(compgen -G "$T/neurobot_diag_*/" | head -n 1)
d=${d%/}
r=$d/REPORT.txt
expect "темы с типами" bash -c "grep -q '^/scan \[sensor_msgs/msg/LaserScan\]' '$d/ros/topics.txt' &&
  grep -q '^/cmd_vel \[geometry_msgs/msg/Twist\]' '$d/ros/topics.txt'"
expect "узлы" grep -q '^\[ок\] ROS 2: узлы' "$r"
expect "/scan: издатель и QoS best effort" bash -c "grep -q 'Publisher count: 1' '$d/ros/topic_scan.txt' &&
  grep -q 'Reliability: BEST_EFFORT' '$d/ros/topic_scan.txt'"
expect "/cmd_vel: издатель" grep -q 'Publisher count: 1' "$d/ros/topic_cmd_vel.txt"
expect "число издателей в отчёте" grep -q '^\[ок\] ROS 2: число издателей — /scan 1, /cmd_vel 1$' "$r"
expect "/scan по секторам: блок NaN прямо по курсу" grep -q \
  '^\[ок\] ROS 2: /scan по секторам 10° — блок NaN: -60.8…+4.2° (186 лучей, индексы 168–353), задевает' "$r"
expect "/scan по секторам: сектора −60…0° — только NaN, массива нет" \
  bash -c "test \$(grep -c '  1\.00  ' '$d/ros/scan_sectors.txt') -eq 6 &&
  grep -q '^  -60…  -50  .*  1\.00  ' '$d/ros/scan_sectors.txt' &&
  grep -q '^тема /scan, кадр laser_frame, лучей 683' '$d/ros/scan_sectors.txt' &&
  ! grep -q '2\.0, 2\.0' '$d/ros/scan_sectors.txt'"
expect "/diff_cont: параметры (Jazzy — без --print) и сводка" \
  bash -c "grep -q 'cmd_vel_timeout: 0.5' '$d/ros/diff_cont.txt' && ! grep -q -- '--print' '$d/ros/diff_cont.txt' &&
  grep -q '^\[ок\] ROS 2: /diff_cont, остановка при тишине /cmd_vel — cmd_vel_timeout: 0.5;use_stamped_vel: false' '$r'"
expect "/twist_mux: параметры" grep -q 'priority: 20' "$d/ros/twist_mux.txt"
expect "/odom, /cmd_vel_tracker, /controller_manager — пометки «нет»" \
  bash -c "grep -q '^\[нет\] ROS 2: тема /odom — нет в графе' '$r' &&
  grep -q '^\[нет\] ROS 2: тема /cmd_vel_tracker — нет в графе' '$r' &&
  grep -q '^\[нет\] ROS 2: узел /controller_manager — нет в графе' '$r'"
expect "служебный daemon поднят и остановлен" bash -c "grep -q '^\[ок\] ROS 2: служебный ros2 daemon поднят' '$r' &&
  grep -q '^\[ок\] ROS 2: служебный ros2 daemon остановлен' '$r'"
expect "после сбора daemon домена не работает" bash -c "ros2 daemon status | grep -q 'daemon is not running'"
expect "журналы ros2 CLI не остались в ~/.ros/log" bash -c "! find '$T/home' -type f | grep -q ."
expect "версия rosbridge_server" grep -q '<version>' "$d/ros/rosbridge_version.txt"
expect "ошибок в отчёте нет" bash -c "! grep -q '^\[ошибка\]' '$r'"
mapfile -t tmpls < <(systemctl list-unit-files --no-legend --no-pager |
  awk '$1 ~ /@\./ && $1 ~ /neuro|robot|ros|mcp/ {print $1}')
if ((${#tmpls[@]})); then
  tmpl=${tmpls[0]}
  expect "шаблон юнита $tmpl: снят только файл" grep -q "^\[ок\] юнит $tmpl: файл шаблона" "$r"
else
  echo "skip шаблон юнита: в ВМ нет подходящего"
fi

echo "итог: $fails провалено"
((fails == 0))
