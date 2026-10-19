"""(в ВМ sim, root, окружение ROS) Помощник e2e пульта на двойнике (e2e_live.py зовёт его через limactl).

  twin_helper.py url                          одноразовая ссылка входа (sudo neurobot ui-url)
  twin_helper.py gestures [--n 6]             новый таймлайн жестов и якорь, рестарт perception (как sensors.py chain)
  twin_helper.py drive --seconds S [--speed V] [--watch S2]
                                              робот едет заявкой агента (/cmd_vel_agent, как MCP через rosbridge):
                                              0,5 с нулей (взвод), затем V; параллельно пишет /neurobot/stop_now,
                                              /cmd_vel и safety_state; итог — JSON в stdout: стопы (id, копии), первый
                                              ноль /cmd_vel после стопа, ехал ли робот после стопа
  twin_helper.py release                      снять защёлку как человек: sudo neurobot estop release в псевдотерминале
  twin_helper.py stats                        память и CPU юнита пульта (systemctl show) и RSS его процессов
  twin_helper.py audit [--after N]            строки аудита пульта после N-й (JSON: lines — их число, records)
  twin_helper.py kill-ui                      kill -9 шлюза и ожидание нового (JSON: через сколько active и /healthz)
  twin_helper.py accept                       приёмка П2 на стенде (PLAN §7): U3 счётчики защёлки, U4 доступ и токен
                                              не в журнале, U5 агент без новых прав, U8 kill -9 пульта — робот не
                                              замечает, рестарт; итог — JSON
DDS и темы — из env-файла пакета (/etc/neurobot/neurobot.env), как у CLI neurobot.
"""
from __future__ import annotations

import argparse
import json
import os
import pty
import select
import subprocess
import sys
import threading
import time

sys.path.insert(0, "/opt/neurobot/lib")
from neurobot_deploy import config  # noqa: E402

CFG = config.load()


def cmd_url(a) -> int:
    r = subprocess.run(["neurobot", "ui-url"], capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    return r.returncode


def cmd_gestures(a) -> int:
    gest = os.path.dirname(CFG.get("NEUROBOT_GESTURES_TIMELINE"))
    subprocess.run(["systemctl", "stop", "neurobot-perception"], check=True)
    subprocess.run(["sudo", "-u", "neurobot", "env", "PYTHONPATH=/var/lib/neurobot-twin/src",
                    "PYTHONDONTWRITEBYTECODE=1", "python3", "-m", "neurobot.sim.gestures.gesturegen", "gen",
                    "--seed", str(a.seed), "--gestures", str(a.n), "--negatives", "1", "--classes", "1,2",
                    "--shapes", "v,swipe,two,wave", "--lead-in-s", str(a.lead_in), "--force", "--out-dir", gest],
                   check=True, stdout=subprocess.DEVNULL)
    try:
        os.remove(os.path.join(gest, "anchor.json"))
    except FileNotFoundError:
        pass
    t = time.time()
    subprocess.run(["systemctl", "start", "neurobot-perception"], check=True)
    print(json.dumps({"perception_ready_s": round(time.time() - t, 1)}))
    return 0


def cmd_drive(a) -> int:
    os.environ.update(CFG.ros_environ())
    import rclpy
    from geometry_msgs.msg import Twist
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import qos_profile_sensor_data
    from std_msgs.msg import String
    rclpy.init()
    node = rclpy.create_node("neurobot_ui_e2e_%d" % os.getpid())     # служебный узел стенда (neurobot_*)
    stops, outs, states = [], [], []
    node.create_subscription(String, "/neurobot/stop_now", lambda m: stops.append((time.time(), m.data)), 10)
    node.create_subscription(Twist, CFG.get("NEUROBOT_CMD_VEL"), lambda m: outs.append((time.time(), m.linear.x)),
                             qos_profile_sensor_data)
    node.create_subscription(String, CFG.get("NEUROBOT_SAFETY_STATE_TOPIC"),
                             lambda m: states.append((time.time(), json.loads(m.data))), qos_profile_sensor_data)
    pub = node.create_publisher(Twist, CFG.get("NEUROBOT_AGENT_TOPIC"), 10)
    ex = SingleThreadedExecutor()
    ex.add_node(node)
    run = [True]

    def spin():
        while run[0]:
            ex.spin_once(timeout_sec=0.02)
    th = threading.Thread(target=spin, daemon=True)
    th.start()
    t0 = time.time()
    end = t0 + a.seconds
    while time.time() < end:
        tw = Twist()
        tw.linear.x = 0.0 if time.time() - t0 < 0.5 else a.speed
        pub.publish(tw)
        time.sleep(0.05)
    zero = Twist()
    for _ in range(10):
        pub.publish(zero)
        time.sleep(0.05)
    time.sleep(a.watch)
    run[0] = False
    th.join(1)
    out = {"t0": t0, "speed": a.speed, "stops": [], "max_out_v": max((v for _, v in outs), default=None)}
    by_id = {}
    for t, data in stops:
        doc = json.loads(data)
        by_id.setdefault(doc.get("id"), []).append((t, doc))
    for sid, got in by_id.items():
        ts = got[0][0]
        zeros = [t for t, v in outs if t >= ts and abs(v) < 1e-6]
        moved = [t for t, v in outs if t > ts + 0.2 and t < end and v > 1e-3]
        st = [s for t, s in states if t >= ts]
        out["stops"].append({"id": sid, "by": got[0][1].get("by"), "copies": len(got), "t": round(ts, 3),
                             "zero_after_s": round(zeros[0] - ts, 4) if zeros else None,
                             "moved_after_stop": bool(moved), "requested_after_s": round(end - ts, 2),
                             "state_after": st[0]["state"] if st else None,
                             "reasons_after": (st[0].get("reasons") or [])[:2] if st else [],
                             "stop_now_counter": (st[-1].get("counters") or {}).get("stop_now") if st else None})
    print(json.dumps(out, ensure_ascii=False))
    node.destroy_node()
    rclpy.shutdown()
    return 0


def cmd_release(a) -> int:
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("neurobot", ["neurobot", "estop", "release", "--reason", "e2e пульта: снял человек у робота"])
    text, sent, end = b"", False, time.monotonic() + 30
    while time.monotonic() < end:
        if not select.select([fd], [], [], 0.2)[0]:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        text += chunk
        if not sent and "Введите".encode() in text:
            os.write(fd, b"release\n")
            sent = True
    _, status = os.waitpid(pid, 0)
    rc = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
    print(json.dumps({"rc": rc, "out": text.decode("utf-8", "replace")[-300:]}, ensure_ascii=False))
    return rc


def cmd_stats(a) -> int:
    props = subprocess.run(["systemctl", "show", "neurobot-ui", "-p", "MainPID,MemoryCurrent,MemoryPeak,"
                            "CPUUsageNSec,ActiveEnterTimestampMonotonic,NRestarts,ControlGroup"],
                           capture_output=True, text=True).stdout
    p = dict(line.split("=", 1) for line in props.splitlines() if "=" in line)
    rss = 0
    try:
        with open("/sys/fs/cgroup%s/cgroup.procs" % p.get("ControlGroup", "")) as f:
            pids = [x.strip() for x in f if x.strip()]
        for pid in pids:
            with open("/proc/%s/statm" % pid) as f:
                rss += int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except OSError:
        pids = []
    with open("/proc/uptime") as f:
        up = float(f.read().split()[0])
    age = up - int(p.get("ActiveEnterTimestampMonotonic", "0") or 0) / 1e6
    cpu_s = int(p.get("CPUUsageNSec", "0") or 0) / 1e9
    print(json.dumps({"memory_current_mib": round(int(p.get("MemoryCurrent", "0") or 0) / 2 ** 20, 1),
                      "memory_peak_mib": round(int(p.get("MemoryPeak", "0") or 0) / 2 ** 20, 1)
                      if p.get("MemoryPeak", "").isdigit() else None,
                      "rss_mib": round(rss / 2 ** 20, 1), "procs": len(pids), "cpu_s": round(cpu_s, 2),
                      "active_s": round(age, 1), "cpu_pct_avg": round(100 * cpu_s / age, 2) if age > 0 else None,
                      "restarts": p.get("NRestarts")}))
    return 0


def cmd_audit(a) -> int:
    path = os.path.join(CFG.get("NEUROBOT_UI_STATE_DIR"), "audit.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    print(json.dumps({"lines": len(lines), "records": [json.loads(x) for x in lines[a.after:]]}, ensure_ascii=False))
    return 0


def cmd_kill_ui(a) -> int:
    pid = subprocess.run(["systemctl", "show", "neurobot-ui", "-p", "MainPID", "--value"], capture_output=True,
                         text=True).stdout.strip()
    t = time.monotonic()
    subprocess.run(["kill", "-9", pid])
    back = None
    while time.monotonic() - t < 60:
        now_pid = subprocess.run(["systemctl", "show", "neurobot-ui", "-p", "MainPID", "--value"],
                                 capture_output=True, text=True).stdout.strip()
        if now_pid not in ("0", pid) and _get("/healthz") == "200":
            back = round(time.monotonic() - t, 2)
            break
        time.sleep(0.1)
    print(json.dumps({"killed": pid, "back_s": back}))
    return 0 if back is not None else 1


def _state_once(timeout: float = 3.0):
    os.environ.update(CFG.ros_environ())
    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from std_msgs.msg import String
    rclpy.init()
    node = rclpy.create_node("neurobot_ui_accept_%d" % os.getpid())
    got = []
    node.create_subscription(String, CFG.get("NEUROBOT_SAFETY_STATE_TOPIC"), lambda m: got.append(
        (time.time(), json.loads(m.data))), qos_profile_sensor_data)
    return rclpy, node, got


def _get(path: str, user: str = "", host: str = "") -> str:
    port = CFG.get("NEUROBOT_UI_PORT")
    cmd = (["sudo", "-u", user] if user else []) + ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-H",
                                                    "Host: " + (host or "127.0.0.1:" + port)]
    return subprocess.run(cmd + ["http://127.0.0.1:%s%s" % (port, path)], capture_output=True, text=True).stdout


def cmd_accept(a) -> int:
    out = {}
    state_dir = CFG.get("NEUROBOT_UI_STATE_DIR")
    with open(os.path.join(state_dir, "token")) as f:
        token = f.read().strip()
    agent = CFG.get("NEUROBOT_USER")
    # U4: без входа — 401, чужой Host — 403, токена нет ни в журнале, ни в аудите
    out["U4"] = {"no_cookie": _get("/api/meta"), "foreign_host": _get("/api/meta", host="evil.example"),
                 "token_in_journal": token in subprocess.run(["journalctl", "-u", "neurobot-ui", "--no-pager", "-o",
                                                               "cat"], capture_output=True, text=True).stdout,
                 "token_in_audit": token in open(os.path.join(state_dir, "audit.jsonl")).read()
                 if os.path.exists(os.path.join(state_dir, "audit.jsonl")) else False}
    # U5: агент (пользователь компонентов) токен не читает, ссылку не выдаёт, без токена — 401; глобы rosbridge те же
    r = subprocess.run(["sudo", "-u", agent, "cat", os.path.join(state_dir, "token")], capture_output=True, text=True)
    url = subprocess.run(["sudo", "-u", agent, "/opt/neurobot/bin/neurobot", "ui-url"], capture_output=True, text=True)
    out["U5"] = {"agent_user": agent, "agent_reads_token": r.returncode == 0, "agent_ui_url_rc": url.returncode,
                 "agent_no_token": _get("/api/meta", user=agent),
                 "rosbridge_cmd": CFG.get("NEUROBOT_ROSBRIDGE_CMD"),
                 "ui_port_in_agent_configs": any(CFG.get("NEUROBOT_UI_PORT") in open(p).read() for p in
                                                 (CFG.get("NEUROBOT_MCP_CONFIG"), CFG.picoclaw_config())
                                                 if p and os.path.exists(p))}
    # U3, U8: счётчики safety_gate; kill -9 пульта — safety_state идёт, состояние то же, systemd поднимает пульт
    rclpy, node, got = _state_once()
    from rclpy.executors import SingleThreadedExecutor
    ex = SingleThreadedExecutor()
    ex.add_node(node)

    def spin(sec):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            ex.spin_once(timeout_sec=0.05)
    spin(2.0)
    pid = subprocess.run(["systemctl", "show", "neurobot-ui", "-p", "MainPID", "--value"], capture_output=True,
                         text=True).stdout.strip()
    n0 = len(got)
    t_kill = time.monotonic()
    subprocess.run(["kill", "-9", pid])
    back = None
    while time.monotonic() - t_kill < 30:
        spin(0.1)
        if subprocess.run(["systemctl", "is-active", "--quiet", "neurobot-ui"]).returncode == 0 and \
                _get("/healthz") == "200" and subprocess.run(["systemctl", "show", "neurobot-ui", "-p", "MainPID",
                                                               "--value"], capture_output=True,
                                                              text=True).stdout.strip() != pid:
            back = round(time.monotonic() - t_kill, 2)
            break
    spin(1.0)
    during = [s for _, s in got[n0:]]
    span = (got[-1][0] - got[n0][0]) if len(got) > n0 + 1 else 0
    after = got[-1][1] if got else {}
    out["U8"] = {"restart_to_healthz_s": back, "safety_state_msgs_during": len(during),
                 "safety_state_hz": round((len(during) - 1) / span, 1) if span else None,
                 "states_during": sorted({s["state"] for s in during}),
                 "foreign_writer": any("foreign_writer" in r for s in during for r in s.get("reasons") or [])}
    c1 = after.get("counters") or {}
    out["U3"] = {"release_denied": c1.get("release_denied"), "estop_engaged": c1.get("estop_engaged"),
                 "estop_released": c1.get("estop_released"), "stop_now": c1.get("stop_now"),
                 "state_now": after.get("state")}
    node.destroy_node()
    rclpy.shutdown()
    print(json.dumps(out, ensure_ascii=False))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("url")
    g = sub.add_parser("gestures")
    g.add_argument("--n", type=int, default=6)
    g.add_argument("--seed", type=int, default=21)
    g.add_argument("--lead-in", type=float, default=10.0)
    d = sub.add_parser("drive")
    d.add_argument("--seconds", type=float, default=8.0)
    d.add_argument("--speed", type=float, default=0.12)
    d.add_argument("--watch", type=float, default=1.0)
    sub.add_parser("release")
    sub.add_parser("stats")
    sub.add_parser("accept")
    au = sub.add_parser("audit")
    au.add_argument("--after", type=int, default=0)
    sub.add_parser("kill-ui")
    a = p.parse_args()
    return {"url": cmd_url, "gestures": cmd_gestures, "drive": cmd_drive, "release": cmd_release,
            "stats": cmd_stats, "accept": cmd_accept, "audit": cmd_audit, "kill-ui": cmd_kill_ui}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
