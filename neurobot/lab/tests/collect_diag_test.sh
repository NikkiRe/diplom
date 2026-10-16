#!/usr/bin/env bash
# Проверка collect_diag.sh на фикстуре. Только Linux; на Mac — через ВМ:
#   limactl shell --workdir / sbc -- bash "$PWD/neurobot/lab/tests/collect_diag_test.sh"
#
# Фикстура во временном каталоге повторяет нынешний стек робота (R7): поддельный HOME с PicoClaw 0.2.8 (конфиг с
# build_info, каналы VK и pico, MCP robot_vision_demo_mcp; .security.yml; журналы шлюза и лаунчера с ключом long poll
# VK в URL, токеном бота в пути, числовым паролем, JSON внутри строки JSON, заголовком «Authorization: token …»,
# URL-кодировкой; stderr MCP-сервера, heartbeat, сессия с перепиской); код лаборатории (демо в git, прокси и скрипт
# стека как присланные, изменённый yolo_hand_worker.py с ключами в формах кода — присваивание по индексу, setdefault,
# getenv с запасным значением, «or», значение на следующей строке, словарь, список и getenv в несколько строк в
# одинарных кавычках, тройные кавычки, аннотация типа, «if not token: token = …», несколько целей, заголовок
# Authorization из выражения, перенос «\» перед «or», "".join( в несколько строк, перенос у оператора внутри скобок
# вызова и словаря, как у black (or, + в начале строки и в конце), «"a" + str(» с переносом внутри вызова, склейка
# строк на одной строке и через перенос, комментарий между значением и «or», « #» в строке, add_argument по
# аргументу на строке, лямбда, аннотация и имя в индексе, перенесённые black; launch-файлы XML и Python с парами
# name/value, Makefile, скрипт запуска с curl -u, sshpass, «echo | sudo -S», nmcli и списком ключей через запятую;
# nohup.out с конфигом YAML (список ключей в несколько строк); файл .python-version, venv без pip, файл весов);
# процессы-пустышки «демо», «прокси MCP», «YOLO-воркер» (вывод в /tmp/yolo_hand_mcp_logs, если этого каталога в ВМ
# нет), «PicoClaw» (ключ в окружении), «драйвер лидара» (свой ROS_DOMAIN_ID), старый ros_mcp_server.py вне HOME;
# настоящий HTTP-воркер на 127.0.0.1:8766 (если порт свободен); запущенный picoclaw-launcher (копия sleep); консоль
# tmux с ключом; пользовательский юнит с ключом в Environment; подставные picoclaw (ELF: копия env, «version» — скрипт
# в PATH), tegrastats, ros2 и rs-enumerate-devices.
# Сбор запускается, как у лаборатории, из копии трёх файлов в $HOME/neurobot_lab.
# Проверяется (по разделам вывода):
#   1) ни один фиктивный секрет и текст сессии не попал в архив, stdout и stderr, а файлы с ними в архиве есть
#      (иначе проверка утечек пуста); код вокруг вырезанных значений читается; HOME фикстуры не изменён;
#      лаунчер не запускался; собрано нужное (журналы шлюза, лаунчера, MCP, воркеров; порт 8766 с кодом воркера и
#      ответом GET /state; версии кода; .python-version; пакеты venv без pip; веса; параметры diff_cont и twist_mux
#      с --print для Foxy; число издателей); --camera не трогает камеру, пока жив процесс демо;
#   2) с --sudo журнал пользовательского юнита собран (в Lima sudo без пароля);
#   3) на системе без ROS, Jetson, PicoClaw и юнитов сбор завершается с кодом 0 и пометками «нет»; --camera
#      вызывает rs-enumerate-devices, когда камеру никто не держит;
#   4) ошибки запуска — код 2 и понятное сообщение; --help описывает флаги без ссылок на файлы репозитория;
#   5) сбой redact.py во время сбора не теряется молча: метка в файле и строка «ошибка» в отчёте;
#   6) зависший ps не вешает сбор: строка «таймаут», архив есть.
# Процессы и юнит фикстуры гасятся при выходе, временный каталог удаляется.
set -euo pipefail

lab=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tp=$lab/../../third_party/neurochip
rp=$tp/robotic_platform/robotic_platform
T=$(mktemp -d /tmp/nb-lab-diag-test-XXXXXX)
ylog=/tmp/yolo_hand_mcp_logs
own_ylog=0
pids=()
unit=nb-lab-robot-$$.service
export TMUX_TMPDIR=$T/tmux
cleanup() {
  set +e  # уборка до конца, даже если что-то уже остановлено
  ((${#pids[@]})) && kill "${pids[@]}" 2> /dev/null && wait "${pids[@]}" 2> /dev/null
  tmux kill-server 2> /dev/null
  systemctl --user stop "$unit" 2> /dev/null
  ((own_ylog)) && rm -rf "$ylog"
  rm -rf "$T"
}
trap cleanup EXIT

fails=0
ok() { echo "ok   $*"; }
fail() { echo "FAIL $*"; fails=$((fails + 1)); }
expect() {  # expect <описание> <команда…> — команда должна завершиться успешно
  local what=$1
  shift
  if "$@" > /dev/null 2>&1; then ok "$what"; else fail "$what"; fi
}
no_leaks() {  # no_leaks <что> <пути…> — ни одного фиктивного секрета в путях
  local what=$1 s n=0
  shift
  for s in "${SECRETS[@]}"; do
    if grep -rqF -- "$s" "$@"; then
      fail "$what: секрет в выводе: ${s:0:10}…"
      n=$((n + 1))
    fi
  done
  ((n)) || ok "$what: ни один из ${#SECRETS[@]} фиктивных секретов не попал в вывод"
}
tree_state() { find "$1" -type f -print0 | sort -z | xargs -0 stat -c '%n %s %Y'; find "$1" -type f -print0 | sort -z |
  xargs -0 sha256sum; }

SECRETS=(
  sk-TESTconfig0123456789abcdef TESTvkGroupTOKEN0123456789 TESTpicoTOKEN31 TESTenvKEY0123456789
  TESTbearer0123456789 TESTargKEY0123 sk-TESTsecurity0123456789 TESTplainlistKEY99 TESTyamlToken777
  TESTlogBearer0123456789 sk-TESTlog0123456789abcd TESTurlPass 0123456789abcdef0123456789abcdefTESTVK01
  TESTtgURLtoken0123456789abcdefghijk 8217364509 TESTlauncherKEY5 TESTstderrKEY77 TESTsessionTEXT
  TESTbashrcKEY123 sk-TESTcode0123456789abc TESTnohupTOKEN42 TESTmcpTOKEN555 sk-TESTscript0123456789ab
  sk-TESTbinary0123456789ab TESTargvTOKEN31337 sk-TESTconsole0123456789 sk-TESTenviron0123456789 TESTunitKEY4242
  glpat-TESTgitlabTOKEN0123456789 sk-TESTtmux0123456789 TESTworkerTOKEN9
  TESTescKEY1 TESTghToken99 TESTencKEY77 TESTcookie55 TESTpyEnvTok0123 TESTsetdefault TESTdictAssign
  TESTgetenvDefault TESTfallbackOr TESTnextLine TESTcurlPass TESTsshpass TESTsudoPw TESTcontinued
  TESTlaunchXML TESTlaunchEnv TESTlaunchPy TESTmakefile
  TESTdictSingle TESTlistSingle TESTgetenvMulti TESTtripleQuoted TESTannotated TESTafterColon TESTtupleTarget
  TESTyamlFlow1 TESTenvList1 TESTenvList2 TESTcontOr TESTjoinList TESTnmcliPw
  TESTblackOr TESTblackDict TESTblackPlus TESTblackTail
  TESTgluedLine TESTgluedBlack TESTcommentOr TESThashDefault TESTargparseKw TESTlambdaKey TESTannotSplit TESTnameAlone
)

H=$T/home
pc=$H/.picoclaw
nc=$H/NeuroChip
mcp=$T/opt/ros_mcp
mkdir -p "$pc/logs" "$pc/workspace/sessions" "$pc/workspace/mcp" "$pc/workspace/memory" "$nc/demo/src/camera" \
  "$nc/demo/models" "$nc/yolo_mcp_server/.venv/bin" "$nc/yolo_hand_worker" "$nc/robot_vision_worker" "$mcp" \
  "$T/bin" "$T/rsbin" "$T/hang" "$T/empty" "$TMUX_TMPDIR"
cat > "$pc/config.json" << EOF
{"agents": {"defaults": {"workspace": "$pc/workspace", "max_tokens": 0, "max_tool_iterations": 50,
   "summarize_token_percent": 75}},
 "model_list": [{"model_name": "cloudrugpt", "model": "openai/gpt-4o-mini",
                 "api_key": "sk-TESTconfig0123456789abcdef"}],
 "channels": {"vk": {"enabled": true, "type": "vk", "settings": {"token": "TESTvkGroupTOKEN0123456789"}},
              "pico": {"enabled": true, "type": "pico", "token": "TESTpicoTOKEN31"}},
 "tools": {"exec": {"enabled": true}, "mcp": {"enabled": true, "servers": {"robot_vision_demo_mcp": {"enabled": true,
   "type": "stdio", "command": "$nc/yolo_mcp_server/.venv/bin/python3",
   "args": ["$nc/yolo_mcp_server/robot_vision_mcp_proxy.py", "--api-key", "TESTargKEY0123"],
   "env": {"OPENAI_API_KEY": "TESTenvKEY0123456789"}, "headers": {"Authorization": "Bearer TESTbearer0123456789"}}}}},
 "gateway": {"host": "localhost", "port": 18790, "hot_reload": false, "log_level": "warn"},
 "build_info": {"version": "0.2.8", "git_commit": "6e1fab80", "build_time": "2026-04-30T14:52:03Z"}}
EOF
cat > "$pc/.security.yml" << 'EOF'
model_list:
  cloudrugpt:
    api_keys:
      - "sk-TESTsecurity0123456789"
      - TESTplainlistKEY99
channels:
  vk:
    token: TESTyamlToken777
EOF
vk_lp='https://lp.vk.com/whp/227?act=a_check&key=0123456789abcdef0123456789abcdefTESTVK01&ts=42&wait=25'
tg_url='https://api.telegram.org/bot123456789:TESTtgURLtoken0123456789abcdefghijk/getUpdates'
printf '%s\n' \
  '{"level":"warn","msg":"Failed to close server connection: signal: interrupt"}' \
  '2026/10/14 10:00:02 DEBUG request headers Authorization: Bearer TESTlogBearer0123456789' \
  '{"level":"error","api_key":"sk-TESTlog0123456789abcd","msg":"MCP tool execution failed: connection closed"}' \
  '2026/10/14 10:00:03 WARN provider url=https://robot:TESTurlPass@routerai.ru/v1' \
  '2026/10/14 10:00:04 ERROR invalid trailing data at the end of stream' \
  "{\"level\":\"warn\",\"msg\":\"vk Long poll failed: Get \\\"$vk_lp\\\": context deadline exceeded\"}" \
  "2026/10/14 10:00:05 ERROR telegram Post \"$tg_url\": proxyconnect tcp" \
  '2026/10/14 10:00:06 WARN mqtt MQTT_PASSWORD=8217364509' \
  '{"level":"debug","msg":"tool args","args":"{\"api_key\":\"TESTescKEY1\",\"x\":1}"}' \
  '2026/10/14 10:00:07 DEBUG Authorization: token TESTghToken99' \
  '2026/10/14 10:00:08 DEBUG POST /hook?q=%22api_key%22%3A%22TESTencKEY77%22%2C%22n%22%3A1' \
  '2026/10/14 10:00:09 DEBUG Cookie: session=TESTcookie55; theme=dark' > "$pc/logs/gateway.log"
printf '%s\n' 'MARK_LAUNCHER_LOG launcher starting' 'auth token=TESTlauncherKEY5' > "$pc/logs/launcher.log"
printf '%s\n' 'MARK_MCP_STDERR Traceback (most recent call last):' 'KeyboardInterrupt' \
  'export ROUTERAI_KEY=TESTstderrKEY77' > "$pc/workspace/mcp/robot_mcp.stderr.log"
echo 'MARK_HEARTBEAT heartbeat started' > "$pc/workspace/heartbeat.log"
echo '{"role":"user","content":"TESTsessionTEXT"}' > "$pc/workspace/sessions/vk_user.jsonl"
echo '# Long-term Memory MARK_MEMORY' > "$pc/workspace/memory/MEMORY.md"
printf '%s\n' 'source /opt/ros/foxy/setup.bash' 'export ROUTERAI_API_KEY=TESTbashrcKEY123' > "$H/.bashrc"
cp "$tp/demo/demo-ros2_feature/src/camera/predict_live.py" "$nc/demo/src/camera/predict_live.py"
git -C "$nc/demo" init -q
git -C "$nc/demo" add src/camera/predict_live.py
git -C "$nc/demo" -c user.name=t -c user.email=t@t commit -qm "ros2_feature"
git -C "$nc/demo" remote add origin https://oauth2:glpat-TESTgitlabTOKEN0123456789@gitlab.se.ifmo.ru/neurochip/demo.git
echo "# локальная правка" > "$nc/demo/src/camera/local_fix.py"
head -c 4096 /dev/urandom > "$nc/demo/models/yolo_hand.pt"
printf '%s\n' 'Exception in thread Thread-2:' "RuntimeError: Frame didn't arrive within 5000" 'token=TESTnohupTOKEN42' \
  'loaded config:' 'api_keys: [' '  TESTyamlFlow1' ']' 'model: gpt' > "$nc/demo/nohup.out"
cp "$rp/robot_vision_mcp_proxy.py" "$rp/yolo_mcp_server.py" "$rp/run_yolo_mcp_stack.sh" "$nc/yolo_mcp_server/"
echo 3.12 > "$nc/yolo_mcp_server/.python-version"
echo "home = /usr/bin" > "$nc/yolo_mcp_server/.venv/pyvenv.cfg"
ln -s "$(command -v python3)" "$nc/yolo_mcp_server/.venv/bin/python"
ln -s "$(command -v python3)" "$nc/yolo_mcp_server/.venv/bin/python3"
{
  cat "$rp/yolo_hand_worker.py"
  cat << 'EOF'
# правка на роботе
os.environ["VK_TOKEN"] = "vk1.a.TESTpyEnvTok0123"
os.environ.setdefault("ROUTERAI_API_KEY", "TESTsetdefault")
config = {}
config["token"] = "TESTdictAssign"
token = os.getenv("VK_TOKEN", "TESTgetenvDefault")
bot = dict(api=os.environ.get("VK_GROUP_KEY") or "TESTfallbackOr")
os.environ.setdefault(
    "ROUTERAI_API_KEY",
    "TESTnextLine",
)
credentials = {
    'user': 'robot',
    'password': 'TESTdictSingle',
}
TOKENS = [
    'TESTlistSingle',
]
ROUTER_KEY = os.getenv(
    'ROUTERAI_API_KEY',
    'TESTgetenvMulti',
)
PROMPT_TOKEN = """
TESTtripleQuoted
"""


def connect(token: str = "TESTannotated"):
    if not token: token = "TESTafterColon"
    return requests.post(URL, headers={"Authorization": "Bearer " + API_KEY}, json={"t": token})


TOKEN, CHAT_ID = "TESTtupleTarget", 123
VK_KEY = os.getenv("VK_KEY") \
    or "TESTcontOr"
ROUTER_TOKEN = "".join([
    "TESTjoinList",
])
client = make_client(
    base_url=URL,
    api_key=os.getenv("ROUTERAI_API_KEY")
    or "TESTblackOr",
)
headers = {
    "X-Api-Key": os.environ.get("K")
    or "TESTblackDict",
}
send(token="robot-"
     + "TESTblackPlus")
send(token="robot-" +
     "TESTblackTail")
os.environ["VK_TOKEN"] = "a" + str(
    x)
client = OpenAI(api_key="sk-or-v1-" "TESTgluedLine")
vk = vk_api.VkApi(
    token="vk1.a."
    "TESTgluedBlack",
    api_version="5.131",
)
fallback = make_client(
    api_key=os.getenv("ROUTERAI_API_KEY")
    # запасной ключ
    or "TESTcommentOr",
)
token = os.getenv("VK_TOKEN", "lab #TESThashDefault")
key = line.split(" #")[0].strip()
parser.add_argument(
    "--token",
    type=str,
    default="TESTargparseKw",
    help="ключ VK",
)
worker = Worker(key=lambda: os.getenv("K") or "TESTlambdaKey")


async def main(
    api_key: Optional[
        str
    ] = "TESTannotSplit",
) -> None:
    os.environ[
        "VK_TOKEN"
    ] = "TESTnameAlone"
EOF
} > "$nc/yolo_hand_worker/yolo_hand_worker.py"
mkdir "$nc/demo/launch"
cat > "$nc/demo/launch/robot.launch.xml" << 'EOF'
<launch>
  <node pkg="demo" exec="predict_live.py">
    <param name="api_key" value="TESTlaunchXML"/>
    <env name="VK_TOKEN" value="TESTlaunchEnv"/>
  </node>
</launch>
EOF
cat > "$nc/demo/launch/robot.launch.py" << 'EOF'
from launch.actions import SetEnvironmentVariable
# запуск predict_live.py с ключом в окружении
ACTIONS = [SetEnvironmentVariable(name="ROUTERAI_API_KEY", value="TESTlaunchPy")]
EOF
printf '%s\n' 'TOKEN ?= TESTmakefile' 'run:' '	python3 src/camera/predict_live.py' > "$nc/Makefile"
cat > "$nc/robot_vision_worker/robot_vision_worker.py" << 'EOF'
# MARK_WORKER_CODE: подставной воркер 8766; запросы пишет в stderr
import http.server
import json
import sys

WORKER_TOKEN = "TESTworkerTOKEN9"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True, "mark": "MARK_STATE", "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("REQ " + self.requestline + "\n")
        sys.stderr.flush()


http.server.HTTPServer(("127.0.0.1", int(sys.argv[2])), Handler).serve_forever()
EOF
printf '%s\n' '# изменённая версия' "API_KEY = 'sk-TESTcode0123456789abc'" > "$mcp/ros_mcp_server.py"
echo '{"rosbridge": {"url": "ws://127.0.0.1:9090"}, "auth": {"token": "TESTmcpTOKEN555"}}' > "$mcp/ros_mcp_config.json"
printf '%s\n' '#!/bin/bash' 'export OPENAI_API_KEY=sk-TESTscript0123456789ab' \
  'export API_KEYS=TESTenvList1,TESTenvList2' 'python3 src/camera/predict_live.py &' \
  'curl -u robot:TESTcurlPass http://127.0.0.1:1/' "sshpass -p 'TESTsshpass' ssh jetson@127.0.0.1 true" \
  'echo TESTsudoPw | sudo -S jetson_clocks' "export VK_TOKEN=\\" '  TESTcontinued' \
  'nmcli dev wifi hotspot ifname wlan0 ssid NeuroBot password TESTnmcliPw' 'picoclaw gateway' > "$H/start_robot.sh"
# picoclaw — настоящий ELF (копия env): «picoclaw version» запускает скрипт version из PATH
cp "$(command -v env)" "$T/bin/picoclaw"
printf '%s\n' '#!/bin/sh' 'echo "picoclaw 0.2.8-fake (key sk-TESTbinary0123456789ab)"' > "$T/bin/version"
cat > "$T/bin/tegrastats" << 'EOF'
#!/bin/sh
while :; do echo "RAM 2100/3964MB (lfb 4x1MB) CPU [12%@1479,8%@1479,10%@1479,9%@1479] GPU@41C"; sleep 0.2; done
EOF
cat > "$T/rsbin/rs-enumerate-devices" << 'EOF'
#!/bin/sh
echo "$*" >> "$FAKE_RS_LOG"
echo "Intel RealSense D435I MARK_CAMERA"
EOF
printf '%s\n' '#!/bin/sh' 'case "$*" in *-eww*) exec sleep 600 ;; esac' "exec $(command -v ps) \"\$@\"" > "$T/hang/ps"
# Подставной ros2: отвечает на команды сбора, как ros2cli, и записывает вызовы с ROS_DOMAIN_ID.
cat > "$T/bin/ros2" << 'EOF'
#!/bin/sh
echo "$* domain=${ROS_DOMAIN_ID:-}" >> "$FAKE_ROS2_LOG"
case "$1 $2" in
  "daemon status") echo "The daemon is not running" ;;
  "daemon start") echo "The daemon has been started" ;;
  "daemon stop") echo "The daemon has been stopped" ;;
  "topic list") printf '%s\n' "/scan [sensor_msgs/msg/LaserScan]" "/cmd_vel [geometry_msgs/msg/Twist]" \
    "/cmd_vel_tracker [geometry_msgs/msg/Twist]" "/diff_cont/cmd_vel_unstamped [geometry_msgs/msg/Twist]" \
    "/odom [nav_msgs/msg/Odometry]" ;;
  "node list") printf '%s\n' /controller_manager /diff_cont /robot_state_publisher /topic_watchdog /twist_mux \
    /urg_node2 /urg_node2 ;;
  "topic info") [ "$4" = /scan ] && n=2 || n=1
    printf 'Type: x\nPublisher count: %s\nSubscription count: 1\n  Reliability: BEST_EFFORT\n' "$n" ;;
  "topic echo") printf 'header:\n  frame_id: odom\ntwist:\n  twist:\n    linear:\n      x: 0.0\n---\nheader:\n---\n'
    exec sleep 30 ;;
  "param dump") if [ "$3" = /diff_cont ]; then
      printf '/diff_cont:\n  ros__parameters:\n    cmd_vel_timeout: 0.5\n    use_stamped_vel: false\n'
      printf '    wheel_radius: 0.036\n'
    else
      printf '/twist_mux:\n  ros__parameters:\n    topics:\n      tracker:\n        priority: 20\n'
    fi ;;
  "param get") echo 'String value is: <robot name="MARK_URDF"/>' ;;
  "control list_controllers") echo "diff_cont[diff_drive_controller/DiffDriveController] active" ;;
  "node info") printf '/topic_watchdog\n  Subscribers:\n    /cmd_vel: geometry_msgs/msg/Twist\n' ;;
esac
EOF
chmod +x "$T/bin/"* "$T/rsbin/"* "$T/hang/ps" "$H/start_robot.sh"
# сбор — как у лаборатории: три файла скопированы в каталог внутри HOME
mkdir "$H/neurobot_lab"
cp "$lab/collect_diag.sh" "$lab/redact.py" "$lab/scan_sectors.py" "$H/neurobot_lab/"
diag=$H/neurobot_lab/collect_diag.sh
export FAKE_ROS2_LOG=$T/ros2.calls FAKE_RS_LOG=$T/rs.calls

# Процессы-пустышки: «демо» пишет консоль в файл; «прокси MCP» и «YOLO-воркер» — как в run_yolo_mcp_stack.sh;
# «MCP» работает из каталога вне HOME; «PicoClaw» держит ключ в окружении (его читать нельзя); «драйвер лидара»
# задаёт ROS_DOMAIN_ID, которого нет у сбора; лаунчер — ELF с именем picoclaw-launcher.
console=$nc/demo/console.log
printf '%s\n' "RuntimeError: Frame didn't arrive within 5000" 'api_key=sk-TESTconsole0123456789' > "$console"
(cd "$nc/demo" && exec -a "python3 src/camera/predict_live.py --token TESTargvTOKEN31337" sleep 300) \
  >> "$console" 2>&1 &
pids+=($!)
ys=$nc/yolo_mcp_server
(cd "$ys" && exec -a "$ys/.venv/bin/python3 $ys/robot_vision_mcp_proxy.py" sleep 300) &
proxy_pid=$!
pids+=($!)
if [[ ! -e $ylog ]]; then
  mkdir "$ylog"
  own_ylog=1
  echo 'MARK_OLD_WORKER_LOG OSError: [Errno 98] Address already in use' > "$ylog/old_worker.log"
  (cd "$nc/yolo_hand_worker" && exec -a "/home/jetson/.pyenv/shims/python3 yolo_hand_worker.py" sleep 300) \
    > "$ylog/yolo_hand_worker.log" 2>&1 &
  pids+=($!)
fi
(cd "$mcp" && exec -a "python3 ros_mcp_server.py --config ./ros_mcp_config.json" sleep 300) &
pids+=($!)
PICOCLAW_HOME=$pc OPENAI_API_KEY=sk-TESTenviron0123456789 bash -c 'exec -a "picoclaw gateway" sleep 300' &
pids+=($!)
ROS_DOMAIN_ID=77 bash -c 'exec -a "ros2 run urg_node2 urg_node2_node" sleep 300' &
pids+=($!)
cp "$(command -v sleep)" "$T/bin/picoclaw-launcher"
"$T/bin/picoclaw-launcher" 300 &
pids+=($!)
have_worker=0
if ! ss -ltnH '( sport = :8766 )' | grep -q .; then
  (cd "$nc/robot_vision_worker" && exec python3 robot_vision_worker.py --port 8766) 2> "$T/worker.req" &
  pids+=($!)
  have_worker=1
fi
have_tmux=0
command -v tmux > /dev/null && tmux new-session -d -s nbdemo \
  "printf '%s\n' 'Exception in thread Thread-2:' 'api_key=sk-TESTtmux0123456789'; sleep 300" && have_tmux=1
have_unit=0
systemd-run --user --unit "$unit" --collect -E ROUTERAI_API_KEY=TESTunitKEY4242 \
  sh -c 'echo MARK_UNIT_STARTED; exec sleep 300' > /dev/null 2>&1 && have_unit=1
sleep 1

echo "== 1. фикстура: секреты, только чтение, состав архива"
tree_state "$H" > "$T/home.before"
set +e
HOME=$H PATH=$T/bin:$T/rsbin:$PATH ROS_DISTRO=foxy env -u ROS_DOMAIN_ID bash "$diag" \
  --tegrastats-sec 2 --camera --out "$T" > "$T/stdout" 2> "$T/stderr"
rc=$?
set -e
tree_state "$H" > "$T/home.after"
expect "код выхода 0 (получен $rc)" test "$rc" -eq 0
expect "в stdout одна строка — путь к архиву" test "$(wc -l < "$T/stdout")" -eq 1
archive=$(cat "$T/stdout")
expect "архив существует" test -f "$archive"
mkdir "$T/x"
tar -xzf "$archive" -C "$T/x"
d=$(echo "$T"/x/neurobot_diag_*)
r=$d/REPORT.txt
no_leaks "фикстура" "$T/x" "$T/stdout" "$T/stderr"
expect "в ходе сбора нет предупреждений и трассировок Python" bash -c "! grep -qE 'Warning|Traceback' '$T/stderr'"
expect "файлы с ключами в формах кода собраны (иначе проверка утечек пуста)" \
  bash -c "cd '$d/startup/scripts' && ls *robot.launch.xml *robot.launch.py *Makefile &&
    grep -q 'tool args' '$d/picoclaw/home_1/logs/gateway.log' && grep -q 'sshpass' '$d'/startup/scripts/*start_robot.sh"
expect "код вокруг вырезанных значений читается" \
  bash -c "grep -qF 'os.environ[\"VK_TOKEN\"] = \"<redacted>\"' '$d'/lab_code/*_yolo_hand_worker.py &&
    grep -qF 'def connect(token: str = \"<redacted>\"):' '$d'/lab_code/*_yolo_hand_worker.py &&
    grep -qF 'headers={\"Authorization\": <redacted>}, json={\"t\": token})' '$d'/lab_code/*_yolo_hand_worker.py &&
    grep -qxF \"    '<redacted>': '<redacted>',\" '$d'/lab_code/*_yolo_hand_worker.py &&
    grep -qx 'api_keys: \[' '$d'/lab_code/logs/*nohup.out &&
    grep -qxF '    or \"<redacted>\"' '$d'/lab_code/*_yolo_hand_worker.py &&
    grep -qxF 'ROUTER_TOKEN = \"\"<redacted>([' '$d'/lab_code/*_yolo_hand_worker.py &&
    grep -qxF '    api_key=<redacted>' '$d'/lab_code/*_yolo_hand_worker.py &&
    grep -qxF '    or \"<redacted>\",' '$d'/lab_code/*_yolo_hand_worker.py &&
    grep -qxF '     + \"<redacted>\")' '$d'/lab_code/*_yolo_hand_worker.py &&
    grep -qxF '    api_version=\"5.131\",' '$d'/lab_code/*_yolo_hand_worker.py &&
    grep -qxF '    # запасной ключ' '$d'/lab_code/*_yolo_hand_worker.py &&
    grep -qxF '    default=\"<redacted>\",' '$d'/lab_code/*_yolo_hand_worker.py &&
    grep -qxF '    ] = \"<redacted>\",' '$d'/lab_code/*_yolo_hand_worker.py &&
    grep -qxF 'key = <redacted>' '$d'/lab_code/*_yolo_hand_worker.py &&
    grep -qF '<param name=\"api_key\" value=\"<redacted>\"/>' '$d'/startup/scripts/*robot.launch.xml &&
    grep -qF 'curl -u robot:<redacted> http://127.0.0.1:1/' '$d'/startup/scripts/*start_robot.sh &&
    grep -qF 'ssid NeuroBot password <redacted>' '$d'/startup/scripts/*start_robot.sh &&
    grep -qF 'DEBUG Authorization: <redacted>' '$d/picoclaw/home_1/logs/gateway.log'"
expect "нет строк вида sk-… в архиве" bash -c "! grep -rqE 'sk-[A-Za-z0-9_-]{8,}' '$T/x'"
expect "HOME фикстуры не изменён (содержимое и mtime всех файлов)" cmp -s "$T/home.before" "$T/home.after"
expect "лаунчер не запускался, его sha256 записан" \
  bash -c "! grep -q 'picoclaw-launcher version' '$d/picoclaw/version.txt' &&
    grep -qF '\$ sha256sum $T/bin/picoclaw-launcher' '$d/picoclaw/version.txt' &&
    grep -qE '^[0-9a-f]{64}  $T/bin/picoclaw-launcher\$' '$d/picoclaw/version.txt'"
expect "версия PicoClaw (сам picoclaw)" grep -q 'picoclaw 0.2.8-fake' "$d/picoclaw/version.txt"
expect "версия PicoClaw по build_info" grep -q '^\[ок\] PicoClaw: версия по build_info .* — 0.2.8 (git 6e1fab80)' "$r"
expect "конфиг PicoClaw скопирован, ключи вырезаны, пределы видны" \
  bash -c "grep -q '\"api_key\": \"<redacted>\"' '$d/picoclaw/home_1/config.json' &&
    grep -q '\"max_tool_iterations\": 50' '$d/picoclaw/home_1/config.json' &&
    grep -q '\"summarize_token_percent\": 75' '$d/picoclaw/home_1/config.json' &&
    grep -q '\"log_level\": \"warn\"' '$d/picoclaw/home_1/config.json'"
expect ".security.yml не скопирован, отмечен в отчёте" \
  bash -c "! find '$T/x' -name '*security*' | grep -q . && grep -q 'security.yml — есть; не копируется' '$r'"
expect "журнал шлюза: текст ошибок сохранён, ключи в URL вырезаны" \
  bash -c "grep -q 'connection closed' '$d/picoclaw/home_1/logs/gateway.log' &&
    grep -q 'act=a_check&key=<redacted>&ts=42' '$d/picoclaw/home_1/logs/gateway.log' &&
    grep -q '/bot<redacted>/getUpdates' '$d/picoclaw/home_1/logs/gateway.log' &&
    grep -q 'MQTT_PASSWORD=<redacted>' '$d/picoclaw/home_1/logs/gateway.log'"
expect "журнал лаунчера, stderr MCP-сервера и heartbeat собраны" \
  bash -c "grep -q MARK_LAUNCHER_LOG '$d/picoclaw/home_1/logs/launcher.log' &&
    grep -q MARK_MCP_STDERR '$d/picoclaw/home_1/workspace/mcp/robot_mcp.stderr.log' &&
    grep -q MARK_HEARTBEAT '$d/picoclaw/home_1/workspace/heartbeat.log'"
expect "сессии: только число и размер" bash -c "grep -q 'сессии .*sessions — файлов: 1, ' '$r' &&
  grep -q 'workspace/sessions/vk_user.jsonl' '$d/picoclaw/home_1/ls.txt'"
expect "консоль процесса демо из файла" grep -rq "Frame didn't arrive within 5000" "$d/processes"
expect "ключ в аргументах процесса вырезан" grep -q 'predict_live.py --token <redacted>' "$d/processes/ps.txt"
expect "прокси MCP опознан как процесс робота (вывод и рабочий каталог)" \
  grep -q "^$proxy_pid cwd $nc/yolo_mcp_server\$" "$d/processes/fds.txt"
expect "predict_live.py и прокси совпадают с присланными версиями" \
  bash -c "grep -q 'predict_live.py — совпадает с версией' '$r' &&
    grep -q 'robot_vision_mcp_proxy.py — совпадает с версией' '$r'"
expect "изменённый yolo_hand_worker.py скопирован" \
  bash -c "grep -q 'yolo_hand_worker.py — ОТЛИЧАЕТСЯ' '$r' &&
    grep -q 'правка на роботе' '$d'/lab_code/*_yolo_hand_worker.py"
expect "поиск расширен рабочим каталогом процесса MCP" grep -q "поиск кода и скриптов расширен — $mcp" "$r"
expect "изменённый ros_mcp_server.py скопирован" \
  bash -c "grep -q 'ros_mcp_server.py — ОТЛИЧАЕТСЯ' '$r' && ls '$d'/lab_code/*_ros_mcp_server.py"
expect "конфиг MCP (рядом с сервером и из --config) скопирован один раз" \
  bash -c "grep -q '\"token\": \"<redacted>\"' '$d'/lab_code/mcp_config_*ros_mcp_config.json &&
    test \$(ls '$d'/lab_code/mcp_config_* | wc -l) -eq 1"
expect "nohup.out демо скопирован" bash -c "grep -q 'Exception in thread' '$d'/lab_code/logs/*nohup.out"
expect "git демо: коммит, неотслеживаемая правка, remote без токена" \
  bash -c "grep -q 'ros2_feature' '$d'/lab_code/git_*.txt &&
    grep -q '^?? src/camera/local_fix.py' '$d'/lab_code/git_*.txt &&
    grep -q 'https://<redacted>@gitlab.se.ifmo.ru/neurochip/demo.git' '$d'/lab_code/git_*.txt"
expect "веса: sha256 без загрузки" bash -c "grep -q 'yolo_hand.pt' '$d/lab_code/weights.txt' &&
  grep -q '^\[ок\] веса моделей: sha256 .* — 1 шт.' '$r'"
if ((own_ylog)); then
  expect "журналы воркеров из /tmp/yolo_hand_mcp_logs (и упавшего, и живого)" \
    bash -c "grep -rq MARK_OLD_WORKER_LOG '$d/lab_code/logs' &&
      grep -q 'журнал воркера $ylog/yolo_hand_worker.log' '$r'"
else
  echo "skip журналы воркеров: $ylog в ВМ уже есть"
fi
if ((have_worker)); then
  expect "порт 8766: кто слушает" grep -q \
    '^\[ок\] порт 8766 (воркер robot_vision) — слушает 127.0.0.1:8766; pid [0-9]*: python3 robot_vision_worker.py' "$r"
  expect "порт 8766: ответ GET /state" bash -c "grep -q '^HTTP 200' '$d/network/http_8766.txt' &&
    grep -q MARK_STATE '$d/network/http_8766.txt'"
  expect "порт 8766: к воркеру — только GET /state" \
    bash -c "test \"\$(cat '$T/worker.req')\" = 'REQ GET /state HTTP/1.1'"
  expect "порт 8766: скрипт воркера скопирован, ключ вырезан" \
    bash -c "grep -q MARK_WORKER_CODE '$d/processes/port8766_robot_vision_worker.py' &&
      grep -q 'WORKER_TOKEN = \"<redacted>\"' '$d/processes/port8766_robot_vision_worker.py'"
else
  echo "skip порт 8766: занят в ВМ"
fi
expect "порт 8765 — не слушается" grep -q '^\[нет\] порт 8765 (YOLO-воркер) — не слушается' "$r"
if ((have_tmux)); then
  expect "консоль tmux снята, ключ вырезан" \
    bash -c "grep -q 'Exception in thread' '$d/processes/tmux_nbdemo_0_0.txt' &&
      grep -q 'api_key=<redacted>' '$d/processes/tmux_nbdemo_0_0.txt'"
else
  echo "skip tmux: не установлен"
fi
expect "скрипты запуска найдены (свой и run_yolo_mcp_stack.sh)" \
  bash -c "ls '$d'/startup/scripts/*start_robot.sh && ls '$d'/startup/scripts/*run_yolo_mcp_stack.sh"
expect "строки .bashrc про ROS" grep -q 'source /opt/ros/foxy/setup.bash' "$d/startup/shellrc.txt"
expect "свой каталог сбора не собирается как код и скрипты запуска" \
  bash -c "! find '$d/startup' '$d/lab_code' -name '*neurobot_lab*' | grep -q ."
expect ".python-version собран" grep -q 'yolo_mcp_server/.python-version:3.12' "$d/python/python_version_files.txt"
expect "venv без pip: пакеты через importlib.metadata" \
  grep -q "^\[ок\] пакеты $nc/yolo_mcp_server/.venv/bin/python (importlib.metadata" "$r"
expect "tegrastats записан" \
  bash -c "grep -q '^\[ок\] tegrastats, 2 с' '$r' && grep -q '^RAM ' '$d/system/tegrastats.txt'"
expect "повторная проверка секретов чистая" grep -q '^\[ок\] повторная проверка секретов' "$r"
expect "итог в отчёте, ошибок нет" grep -q '^итого: ок [0-9]*, нет [0-9]*, права [0-9]*, таймаут 0, ошибка 0;' "$r"
expect "ROS 2: ROS_DOMAIN_ID взят из окружения процесса" grep -q 'ROS_DOMAIN_ID=77  (из процесса' "$d/ros/env.txt"
expect "ROS 2: команды идут в домен процесса" grep -q '^topic list -t domain=77' "$T/ros2.calls"
expect "ROS 2: служебный daemon поднят и остановлен" \
  bash -c "grep -q 'daemon поднят на время сбора' '$r' && grep -q '^daemon stop domain=77' '$T/ros2.calls'"
expect "ROS 2: число издателей (/scan — 2)" \
  grep -q '^\[ок\] ROS 2: число издателей — /scan 2, /cmd_vel 1, /odom 1, /cmd_vel_tracker 1' "$r"
expect "ROS 2: /cmd_vel_joy — нет в графе" grep -q '^\[нет\] ROS 2: тема /cmd_vel_joy — нет в графе' "$r"
expect "ROS 2: параметры /diff_cont (Foxy — с --print) и сводка" \
  bash -c "grep -q '^param dump /diff_cont --print domain=77' '$T/ros2.calls' &&
    grep -q '^\[ок\] ROS 2: /diff_cont, остановка .* — cmd_vel_timeout: 0.5;use_stamped_vel: false;wheel_radius' '$r'"
expect "ROS 2: twist_mux, контроллеры, /topic_watchdog, URDF" \
  bash -c "grep -q 'priority: 20' '$d/ros/twist_mux.txt' && grep -q DiffDriveController '$d/ros/controllers.txt' &&
    grep -q '/cmd_vel' '$d/ros/topic_watchdog.txt' && grep -q MARK_URDF '$d/ros/robot_description.txt'"
expect "ROS 2: сводка /scan без rclpy — пометка «нет»" grep -q '^\[нет\] ROS 2: /scan по секторам 10° — нет rclpy' "$r"
expect "ROS 2: одно сообщение /odom без массивов" \
  bash -c "grep -q 'frame_id: odom' '$d/ros/echo_odom.txt' && test \$(grep -c '^---\$' '$d/ros/echo_odom.txt') -eq 1"
expect "--camera: камера не тронута, пока жив процесс демо" \
  bash -c "grep -q '^\[нет\] камера: параметры .* — не запускался: камерой может владеть процесс' '$r' &&
    test ! -e '$T/rs.calls'"
if ((have_unit)); then
  expect "юнит найден, состояние и файл записаны" bash -c "grep -q '^ActiveState=active' '$d/units/user_$unit.txt' &&
    grep -q 'transient unit file' '$d/units/user_$unit.txt'"
  expect "Environment юнита без ключа" grep -q 'ROUTERAI_API_KEY=<redacted>' "$d/units/user_$unit.txt"
else
  echo "skip юнит: systemd-run --user недоступен"
fi

echo "== 2. --sudo: журнал пользовательского юнита"
if ((have_unit)) && sudo -n true 2> /dev/null; then
  mkdir "$T/s"
  set +e
  HOME=$H PATH=$T/bin:$PATH env -u ROS_DOMAIN_ID bash "$diag" --sudo --tegrastats-sec 0 --out "$T/s" \
    > "$T/stdout_s" 2> "$T/stderr_s"
  rc=$?
  set -e
  expect "код выхода 0 (получен $rc)" test "$rc" -eq 0
  ds=$(compgen -G "$T/s/neurobot_diag_*/" | head -n 1)
  expect "журнал юнита собран через sudo" grep -q MARK_UNIT_STARTED "${ds}units/user_$unit.txt"
  expect "лаунчер не запускался и с sudo" bash -c "! grep -q 'picoclaw-launcher version' '${ds}picoclaw/version.txt'"
  no_leaks "--sudo" "$T/s" "$T/stdout_s" "$T/stderr_s"
else
  echo "skip --sudo: нет юнита или sudo без пароля"
fi

kill "${pids[@]}" 2> /dev/null
wait "${pids[@]}" 2> /dev/null || true
pids=()
tmux kill-server 2> /dev/null || true
systemctl --user stop "$unit" 2> /dev/null || true

echo "== 3. система без ROS, Jetson, PicoClaw и юнитов"
set +e
HOME=$T/empty PATH=$T/rsbin:$PATH bash "$lab/collect_diag.sh" --tegrastats-sec 0 --camera --out "$T/empty" \
  > "$T/stdout2" 2> "$T/stderr2"
rc=$?
set -e
expect "код выхода 0 (получен $rc)" test "$rc" -eq 0
expect "в stdout одна строка — путь к архиву" test "$(wc -l < "$T/stdout2")" -eq 1
r2=$(echo "$T"/empty/neurobot_diag_*/REPORT.txt)
marks=('^\[нет\] ROS 2 — нет /opt/ros' '^\[нет\] модель Jetson' '^\[нет\] код лаборатории'
  '^\[нет\] tegrastats — отключено')
((have_worker)) && marks+=('^\[нет\] порт 8766 (воркер robot_vision) — не слушается')
if pgrep -f picoclaw > /dev/null || command -v picoclaw > /dev/null; then
  echo "skip PicoClaw: в ВМ запущен или установлен свой"
else
  marks+=('^\[нет\] PicoClaw: бинарник' '^\[нет\] PicoClaw: каталог')
fi
for line in "${marks[@]}"; do
  expect "пометка «${line//\\/}»" grep -q "$line" "$r2"
done
expect "юнита фикстуры в отчёте нет" bash -c "! grep -q 'юнит $unit' '$r2'"
expect "без ошибок в отчёте" bash -c "! grep -q '^\[ошибка\]' '$r2'"
if pgrep -f 'predict_live|realsense|vision|yolo|worker\.py' > /dev/null; then
  echo "skip --camera: в ВМ есть процесс, который мог бы держать камеру"
else
  expect "--camera: rs-enumerate-devices -c вызван, вывод собран" \
    bash -c "grep -qx -- '-c' '$T/rs.calls' && grep -q MARK_CAMERA \$(dirname '$r2')/system/camera.txt"
fi

echo "== 4. ошибки запуска и --help"
set +e
msg=$(bash "$lab/collect_diag.sh" --days x 2>&1)
rc=$?
set -e
expect "неверное число — код 2" test "$rc" -eq 2
expect "неверное число — сообщение" grep -q 'ожидалось целое число' <<< "$msg"
cp "$lab/collect_diag.sh" "$T/alone.sh"
set +e
msg=$(bash "$T/alone.sh" --out "$T" 2>&1)
rc=$?
set -e
expect "без redact.py сбор не начинается — код 2" test "$rc" -eq 2
expect "без redact.py — сообщение" grep -q 'нет redact.py' <<< "$msg"
msg=$(bash "$lab/collect_diag.sh" --help)
expect "--help: флаги со значениями по умолчанию" \
  bash -c "grep -q -- '--max-log-mb N .*(5)' <<< \"\$1\" && grep -q -- '--camera' <<< \"\$1\"" _ "$msg"
expect "--help: без ссылок на README и PLAN" bash -c "! grep -qE 'README|PLAN' <<< \"\$1\"" _ "$msg"

echo "== 5. сбой redact.py во время сбора"
mkdir "$T/broken" "$T/out5"
cp "$lab/collect_diag.sh" "$T/broken/"
cat > "$T/broken/redact.py" << 'EOF'
import sys
sys.exit(0 if sys.argv[1:2] == ["--check"] else 3)
EOF
set +e
HOME=$T/empty bash "$T/broken/collect_diag.sh" --tegrastats-sec 0 --out "$T/out5" > /dev/null 2>&1
rc=$?
set -e
r5=$(echo "$T"/out5/neurobot_diag_*/REPORT.txt)
expect "сбор завершён (код $rc)" test "$rc" -eq 0
expect "в отчёте — ошибка «сбой redact.py»" grep -q '^\[ошибка\] сбой redact.py — файлы неполные: ' "$r5"
expect "метка в файле вывода" grep -q 'сбой redact.py (код 3)' "$(dirname "$r5")/system/versions.txt"

echo "== 6. зависший ps"
mkdir "$T/out6"
set +e
HOME=$T/empty PATH=$T/hang:$PATH timeout 120 bash "$lab/collect_diag.sh" --tegrastats-sec 0 --out "$T/out6" \
  > "$T/stdout6" 2> /dev/null
rc=$?
set -e
expect "сбор завершён (код $rc), архив есть" bash -c "test $rc -eq 0 && test -f \"\$(cat '$T/stdout6')\""
expect "в отчёте — таймаут ps" grep -q '^\[таймаут\] процессы — processes/ps.txt' "$T"/out6/neurobot_diag_*/REPORT.txt

echo "итог: $fails провалено"
((fails == 0))
