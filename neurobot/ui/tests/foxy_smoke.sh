#!/usr/bin/env bash
# Дымовой прогон живого шлюза пульта на ROS 2 Foxy — до П6 (PLAN §0а п. 7, ревью П2): двойник — Jazzy и Cyclone,
# а на роботе Foxy (Python 3.8, rclpy 1.0.x) и Fast DDS с общей памятью между разными пользователями.
#   neurobot/ui/tests/foxy_smoke.sh [--image ros:foxy-ros-base]     контейнер Docker (образ скачивается один раз)
#   foxy_smoke.sh --inner /opt/ros/<дистрибутив>/setup.bash          то же без контейнера (root; так скрипт проверен
#                                                                     в ВМ sim на Jazzy)
# Шаги (всё — от отдельного пользователя uitest, как neurobot-ui на роботе; подставной safety_gate — от root):
#   1) версии Python, rclpy, RMW; py_compile пакета пульта и deploy;
#   2) test_ui_node.py — процессы: издатели, wait-graph ui, поток, сырой скан, СТОП и защёлка;
#   3) между пользователями: шлюз (uitest) видит safety_state подставного safety_gate (root), СТОП доходит до него и
#      подтверждается счётчиком; у Fast DDS — сначала с общей памятью (как без профиля шлюза: ожидаемо данных нет),
#      затем как в юните — профиль шлюза «только UDP» (neurobot/ui/fastdds_udp.xml): должен пройти;
#   4) kill -9 шлюза, новый запуск: через сколько wait-graph ui снова проходит и сколько узлов ui_gateway в графе
#      (призрак участника: у Fast DDS lease по умолчанию 20 с).
# Код 0 — всё прошло; итог — строки «foxy-smoke: …» в stderr.
set -euo pipefail

say() { echo "foxy-smoke: $*" >&2; }
die() { say "ОШИБКА: $*"; exit 1; }

if [[ ${1:-} != --inner ]]; then
  image=ros:foxy-ros-base
  if [[ ${1:-} == --image ]]; then image=${2:?образ}; shift 2; fi
  root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
  command -v docker >/dev/null || die "нет docker"
  exec docker run --rm -v "$root:/src:ro" -w /src -e RMW_IMPLEMENTATION=rmw_fastrtps_cpp "$image" \
    bash /src/neurobot/ui/tests/foxy_smoke.sh --inner /opt/ros/foxy/setup.bash
fi

setup=${2:?путь к setup.bash}
[[ $EUID -eq 0 ]] || die "нужен root (пользователь uitest и подставной safety_gate)"
src=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
set +u
# shellcheck disable=SC1090
source "$setup"
set -u
export ROS_DOMAIN_ID=${NEUROBOT_TEST_DOMAIN:-49} PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$src:$src/neurobot/deploy:${PYTHONPATH:-}"
work=$(mktemp -d /tmp/foxy-smoke.XXXXXX)
chmod 0755 "$work"
pids=()
cleanup() {
  for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done
  pkill -u uitest -f "neurobot.ui live --env $work" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

# 1) версии и компиляция
rclpy_v=$(sed -n 's:.*<version>\(.*\)</version>.*:\1:p' "$(dirname "$setup")/share/rclpy/package.xml" | head -1)
say "python $(python3 -c 'import platform; print(platform.python_version())'), rclpy $rclpy_v," \
    "RMW ${RMW_IMPLEMENTATION:-по умолчанию}, ROS_DISTRO ${ROS_DISTRO:-?}"
python3 -c "import glob, py_compile
for f in glob.glob('$src/neurobot/ui/**/*.py', recursive=True) + glob.glob('$src/neurobot/deploy/neurobot_deploy/*.py'):
    compile(open(f, encoding='utf-8').read(), f, 'exec')" || die "py_compile"
id uitest &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin uitest
install -d -o uitest -g uitest -m 0700 "$work/home"
# журналы rcl — в свой каталог: у системного пользователя нет $HOME (как у юнита пульта)
as_ui() { runuser -u uitest -- env HOME="$work/home" ROS_LOG_DIR="$work/home/log" "$@"; }

# 2) процессные тесты шлюза
as_ui env NEUROBOT_TEST_ROS_SETUP="$setup" NEUROBOT_TEST_DOMAIN="$ROS_DOMAIN_ID" \
  python3 -W ignore::ResourceWarning -m unittest discover -s "$src/neurobot/ui/tests" -p test_ui_node.py \
  >"$work/node_tests.log" 2>&1 || { cat "$work/node_tests.log" >&2; die "test_ui_node.py"; }
grep -q '^OK$' "$work/node_tests.log" || { cat "$work/node_tests.log" >&2; die "test_ui_node.py: тесты пропущены"; }
say "test_ui_node.py — $(grep -m1 '^Ran ' "$work/node_tests.log")"

# 3) шлюз от uitest, подставной safety_gate от root
port=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
state=$work/ui
env_file=$work/neurobot.env
cat >"$env_file" <<EOF
NEUROBOT_COMPONENTS="safety ui"
NEUROBOT_ROS_SETUP=$setup
ROS_DOMAIN_ID=$ROS_DOMAIN_ID
NEUROBOT_UI_PORT=$port
NEUROBOT_UI_SOURCE=двойник
NEUROBOT_UI_DOCTOR_S=0
NEUROBOT_UI_STATE_DIR=$state
NEUROBOT_WAIT_S=40
EOF
chmod 0644 "$env_file"
install -d -o uitest -g uitest -m 0700 "$state"
cat >"$work/gate.py" <<'EOF'
# подставной safety_gate: safety_state 5 Гц со счётчиком стопов; копии стопа с одним id — один стоп
import json
import rclpy
from std_msgs.msg import String
rclpy.init()
node = rclpy.create_node("safety_gate")
pub = node.create_publisher(String, "/neurobot/safety_state", 10)
seen = set()
def on_stop(m):
    seen.add(json.loads(m.data).get("id"))
node.create_subscription(String, "/neurobot/stop_now", on_stop, 10)
node.create_timer(0.2, lambda: pub.publish(String(data=json.dumps({
    "boot": "smoke", "state": "slow", "reasons": ["lidar_blind: x"], "warnings": [], "req": [0.0, 0.0],
    "out": [0.0, 0.0], "counters": {"stop_now": len(seen)}}))))
try:
    rclpy.spin(node)
except BaseException:       # SIGTERM при уборке: в Jazzy — ExternalShutdownException, в Foxy её нет
    pass
EOF
python3 "$work/gate.py" &
pids+=($!)
# шлюз — процесс python под runuser: его PID (а не runuser) нужен для kill -9; порт свободен — прежний шлюз ушёл
start_ui() {        # аргументы — переменные окружения шлюза (VAR=…)
  ! curl -fs -o /dev/null -H "Host: 127.0.0.1:$port" "http://127.0.0.1:$port/healthz" || die "порт $port занят"
  as_ui env "$@" python3 -m neurobot.ui live --env "$env_file" --state-dir "$state" >"$work/ui.log" 2>&1 &
  pids+=($!)
  for _ in $(seq 1 150); do
    if curl -fs -o /dev/null -H "Host: 127.0.0.1:$port" "http://127.0.0.1:$port/healthz"; then
      ui_pid=$(pgrep -n -u uitest -f "neurobot.ui live --env $env_file")
      return 0
    fi
    sleep 0.2
  done
  cat "$work/ui.log" >&2
  die "шлюз не поднялся за 30 с"
}
stop_ui() {
  kill "$ui_pid" 2>/dev/null || true
  for _ in $(seq 1 50); do kill -0 "$ui_pid" 2>/dev/null || return 0; sleep 0.2; done
  die "шлюз $ui_pid не вышел за 10 с"
}
cat >"$work/cross.py" <<'EOF'
# вход по коду (как ui-url), поток: дошёл ли safety_state подставного safety_gate; СТОП — подтверждён ли счётчиком
import hashlib, http.client, json, os, pwd, secrets, socket, sys, time
port, state, label = int(sys.argv[1]), sys.argv[2], sys.argv[3]
host = "127.0.0.1:%d" % port
code = secrets.token_urlsafe(12)
path = os.path.join(state, "codes", hashlib.sha256(code.encode()).hexdigest())
with open(path, "w") as f:
    json.dump({"exp": time.time() + 60}, f)
ui = pwd.getpwnam("uitest")
os.chown(path, ui.pw_uid, ui.pw_gid)
c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
c.request("GET", "/login?code=" + code, headers={"Host": host})
r = c.getresponse(); r.read()
cookie = r.getheader("Set-Cookie").split(";")[0]
c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
c.request("GET", "/api/meta", headers={"Host": host, "Cookie": cookie})
csrf = json.loads(c.getresponse().read())["csrf"]
s = socket.create_connection(("127.0.0.1", port), timeout=10)
s.sendall(("GET /api/stream HTTP/1.1\r\nHost: %s\r\nCookie: %s\r\n\r\n" % (host, cookie)).encode())
got, end = b"", time.monotonic() + 15
while b'"k":"safety"' not in got and time.monotonic() < end:
    got += s.recv(65536)
s.close()
if b'"k":"safety"' not in got:
    print("foxy-smoke: %s: safety_state от root шлюз (uitest) за 15 с не увидел" % label, file=sys.stderr)
    sys.exit(1)
c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
c.request("POST", "/api/stop", body=json.dumps({"issued_at": time.time()}).encode(), headers={
    "Host": host, "Origin": "http://" + host, "Cookie": cookie, "X-Neurobot-CSRF": csrf,
    "Content-Type": "application/json"})
r = c.getresponse()
res = json.loads(r.read())
print("foxy-smoke: %s: safety_state дошёл, СТОП — HTTP %d, подтверждён safety_gate за %s с"
      % (label, r.status, res.get("confirmed_s")), file=sys.stderr)
sys.exit(0 if r.status == 200 and res["ok"] else 1)
EOF
cross() { python3 "$work/cross.py" "$port" "$state" "$1"; }
if [[ ${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp} == rmw_fastrtps* ]]; then
  # пустой FASTRTPS_DEFAULT_PROFILES_FILE — профиль шлюза не ставится (node.dds_environ), Fast DDS — с общей памятью
  start_ui FASTRTPS_DEFAULT_PROFILES_FILE=
  if cross "Fast DDS с общей памятью"; then shm=идут; else shm="нет (обнаружение есть)"; fi
  say "Fast DDS с общей памятью между пользователями: данные — $shm"
  stop_ui
fi
start_ui
as_ui python3 -m neurobot_deploy --env "$env_file" wait-graph ui 2>&1 | tail -1 >&2 || die "wait-graph ui"
cross "как в юните (у Fast DDS — только UDP)" || { cat "$work/ui.log" >&2; die "между пользователями: поток или СТОП"; }

# 4) kill -9 и новый запуск: призрак участника в графе — глазами долгоживущего участника (как safety_gate и doctor
# в одном процессе): свежий `ros2 node list` мёртвых участников не узнаёт, а давний помнит их до конца lease
cat >"$work/watch.py" <<'EOF'
import json, sys, time
import rclpy
rclpy.init()
node = rclpy.create_node("neurobot_smoke_watch")
out, end = [], time.monotonic() + float(sys.argv[1])
while time.monotonic() < end:
    rclpy.spin_once(node, timeout_sec=0.1)
    out.append((round(time.monotonic(), 2), node.get_node_names().count("ui_gateway")))
print(json.dumps(out))
EOF
python3 "$work/watch.py" 45 >"$work/watch.json" &
watch=$!
pids+=("$watch")
sleep 3
kill -9 "$ui_pid"
t0=$(date +%s.%N)
k0=$(python3 -c 'import time; print(time.monotonic())')
for _ in $(seq 1 50); do kill -0 "$ui_pid" 2>/dev/null || break; sleep 0.1; done
start_ui
as_ui python3 -m neurobot_deploy --env "$env_file" wait-graph ui 2>&1 | tail -1 >&2 || die "wait-graph ui после kill -9"
t1=$(date +%s.%N)
wait "$watch" || die "наблюдатель графа"
python3 - "$work/watch.json" "$k0" "$(python3 -c "print(round($t1 - $t0, 1))")" <<'EOF' >&2
import json, sys
seq, k0, back = json.load(open(sys.argv[1])), float(sys.argv[2]), sys.argv[3]
after = [(t - k0, n) for t, n in seq if t >= k0]
two = [t for t, n in after if n >= 2]
print("foxy-smoke: kill -9 → wait-graph ui снова прошёл за %s с; давний участник видел до %d узлов ui_gateway, "
      "два и больше — %s" % (back, max(n for _, n in after), "с %.1f по %.1f с после kill" % (two[0], two[-1])
                             if two else "ни разу"))
EOF
say "всё прошло"
