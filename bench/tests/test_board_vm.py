"""board.sh в настоящей ВМ Lima (по умолчанию sbc): запуск под лимитами, код и причина выхода, срок юнита, сигнал
board.sh, диск, netns с netem, расписание, откат прерванного net-up, идемпотентность и уборка; корневой netns
не меняется.
Только с BOARD_VM=1 (имя ВМ — BOARD_VM_NAME); не параллельно с verify или лестницей — ВМ общая, а сравнение
корневого netns и cleanup видят чужие объекты board. Перед проверкой и после неё ядро ВМ должно быть без tainted."""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.board.server import ProbeServer  # noqa: E402
from bench.board.vm import BOARD_SH, Vm, VmError, parse_record, probe_command  # noqa: E402

VM = Vm(os.environ.get("BOARD_VM_NAME", "sbc"))
TAG = f"tb{os.getpid() % 10000}"
# Корневой netns, который board.sh не должен менять: маршруты, eth0 и его qdisc, устройства ifb (свои ifb board.sh
# создаёт только в netns), чужие таблицы nft, пересылка (включается только на время netns board) и число netns board.
ROOT_NETNS = ("ip route; ip -br link show dev eth0; tc qdisc show dev eth0; ip -br link show type ifb; "
              "nft list tables | grep -v board_; sysctl -n net.ipv4.ip_forward; ip netns list | grep -c board- || true")
# Объекты board.sh в ВМ: netns, таблицы nft, юниты.
BOARD_OBJECTS = ("ip netns list | grep -o 'board-[a-z0-9]*' || true; "
                 "nft list tables | grep -o 'board_[a-z0-9]*' || true; "
                 "systemctl list-units --no-legend --plain 'board-*' | cut -d' ' -f1")


def board_objects() -> set[str]:
    return set(VM.shell("sudo", "sh", "-c", BOARD_OBJECTS).split())


@unittest.skipUnless(os.environ.get("BOARD_VM") == "1", "нужна ВМ: BOARD_VM=1")
class BoardVmTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if VM.tainted():
            raise unittest.SkipTest("ядро ВМ tainted — проверка не начата")
        cls.root_before = VM.shell("sudo", "sh", "-c", ROOT_NETNS)

    @classmethod
    def tearDownClass(cls):
        for ns in (TAG, f"{TAG}s", f"{TAG}i"):
            VM.board("net-down", ns)
        assert VM.tainted() == 0, "ядро ВМ стало tainted"

    def test_exit_code_user_and_go_environment(self):
        res = VM.run(f"{TAG}-exit", ["sh", "-c", 'id -u; echo "$GOMEMLIMIT $GOMAXPROCS $X"; exit 3'], 60,
                     mem=64, cpu=50, cpus=2, env={"X": "a b", "GOMAXPROCS": "3"})
        uid, env = res.stdout.split("\n")[:2]
        self.assertNotEqual(uid, "0")
        self.assertEqual(env, f"{64 * 1048576 * 45 // 100} 3 a b")   # --env побеждает умолчание GOMAXPROCS
        self.assertEqual((res.code, res.record["reason"], res.record["exit_code"]), (3, "exit", 3))
        self.assertEqual(res.record["cpu"]["max"], "10000 20000")
        self.assertEqual(res.record["cpu"]["cpus"], "0-1")
        self.assertEqual(res.record["memory"]["swap_max"], 0)
        res = VM.run(f"{TAG}-gml", ["sh", "-c", 'echo "$GOMEMLIMIT"'], 60, mem=64, cpu=100, gomemlimit="10")
        self.assertEqual(res.stdout.strip(), str(64 * 1048576 * 10 // 100))

    def test_record_barely_raises_peaks(self):
        # Запись итога — встроенные команды bash до остального файла: к memory.peak, каким его видела сама команда,
        # добавляется только минимальный ExecStopPost (≈ 0,15 МиБ), а не разбор всего board.sh (≈ 0,6 МиБ).
        cg = f"/sys/fs/cgroup/system.slice/board-run-{TAG}-peak.service"
        res = VM.run(f"{TAG}-peak", ["bash", "-c", f"read -r p <{cg}/memory.peak; echo $p"], 60, mem=64, cpu=100)
        self.assertLess(res.record["memory"]["peak"] - int(res.stdout), 400 * 1024)
        self.assertLessEqual(res.record["pids_peak"], 2)

    def test_oom_kill_and_signal(self):
        res = VM.run(f"{TAG}-oom", probe_command("mem", max_mib=200), 60, mem=24, cpu=100, gomemlimit="off")
        self.assertEqual((res.code, res.record["reason"], res.record["signal"]), (137, "oom-kill", "SIGKILL"))
        self.assertGreaterEqual(res.record["memory"]["events"]["oom_kill"], 1)
        self.assertIsNone(res.phases["start"]["env"]["GOMEMLIMIT"])
        res = VM.run(f"{TAG}-term", ["sh", "-c", "kill -TERM $$"], 60, mem=64, cpu=100)
        self.assertEqual((res.code, res.record["reason"], res.record["signal"]), (143, "signal", "SIGTERM"))

    def test_timeout_is_enforced_in_the_vm_and_after_a_lost_client(self):
        # срок юнита соблюдает сама ВМ (RuntimeMaxSec)
        res = VM.run(f"{TAG}-tmo", ["sleep", "30"], 1.5, mem=64, cpu=100)
        self.assertEqual((res.code, res.record["reason"], res.record["service_result"]), (143, "timeout", "timeout"))
        self.assertLess(res.record["wall_s"], 10)
        # limactl не дождался ответа (здесь — искусственно короткое ожидание): Vm.run останавливает юнит сам
        unit = f"board-run-{TAG}-lost.service"
        real = VM._call

        def impatient(cmd, timeout, stdin=None):
            return real(cmd, 3 if "run" in cmd else timeout, stdin)

        with mock.patch.object(VM, "_call", side_effect=impatient), self.assertRaisesRegex(VmError, "нет ответа"):
            VM.run(f"{TAG}-lost", ["sleep", "60"], 30, mem=64, cpu=100)
        self.assertEqual(VM.shell("sh", "-c", f"systemctl is-active {unit} || true").strip(), "inactive")
        self.assertEqual(VM.run(f"{TAG}-lost", ["true"], 30, mem=64, cpu=100).code, 0)   # имя снова свободно

    def test_signal_to_board_sh_stops_the_unit(self):
        # TERM самому board.sh run (а не клиенту limactl) останавливает юнит; итог печатается, как обычно
        unit = f"board-run-{TAG}-sig.service"
        script = (f"err=$(mktemp); trap 'rm -f $err' EXIT; bash {BOARD_SH} run {TAG}-sig --mem 64 --cpu 100 "
                  f"-- sleep 60 2>$err & p=$!; sleep 1; kill -TERM $p; rc=0; wait $p || rc=$?; "
                  f"echo $rc; systemctl is-active {unit} || true; tail -1 $err")
        rc, state, last = VM.shell("sudo", "bash", "-c", script).splitlines()
        record = parse_record(last)
        self.assertEqual((rc, state, record["reason"], record["signal"]), ("143", "inactive", "signal", "SIGTERM"))
        self.assertLess(record["wall_s"], 10)

    def test_disk_limits_need_a_block_device(self):
        res = VM.run(f"{TAG}-disk", ["true"], 60, mem=64, cpu=100, disk="sd-awful", workdir=VM.workdir())
        (limits,) = res.record["io"]["max"].values()
        self.assertEqual(limits, {"rbps": 12500000, "wbps": 4000000, "riops": 300, "wiops": 20})
        # каталог проекта смонтирован с Mac (virtiofs), /dev/shm — tmpfs: io.max не к чему применить
        for elsewhere in (str(ROOT), "/dev/shm"):
            with self.assertRaisesRegex(VmError, "не на блочном устройстве"):
                VM.board("run", f"{TAG}-x", "--mem", "64", "--cpu", "100", "--disk", "sd-awful", "--workdir",
                         elsewhere, "--", "true")

    def test_bad_arguments(self):
        run = ["run", f"{TAG}-x", "--mem", "64"]
        for args in ([*run[:3], "0", "--cpu", "50", "--", "true"],
                     [*run, "--cpu", "50", "--disk", "ssd", "--", "true"],
                     [*run, "--cpu", "50", "--net", "nosuchns", "--", "true"],
                     [*run, "--cpu", "2", "--", "true"],                      # < 1 мс за период 20 мс
                     [*run, "--cpu", "101", "--cpus", "1", "--", "true"],     # больше, чем дают ядра
                     [*run, "--cpu", "50", "--timeout", "0", "--", "true"],
                     [*run, "--cpu", "50", "--gomemlimit", "0", "--", "true"],
                     [*run, "--cpu", "50", "--", "./cpubench"],               # относительный путь
                     ["net-up", "Bad", "4g"], ["net-up", TAG, "5g"], ["net-set", "nosuchns", "4g"]):
            with self.assertRaisesRegex(VmError, "код 2", msg=args):
                VM.board(*args)

    def test_network_lifecycle(self):
        VM.board("net-up", TAG, "lan")
        VM.board("net-up", TAG, "4g")   # повторный вызов — смена профиля
        stats = json.loads(VM.board("net-stats", TAG))
        self.assertEqual((stats["profile"], stats["netem"]), ("4g", "4g"))
        self.assertEqual(stats["up"]["options"]["delay"]["delay"], 0.035)
        self.assertEqual(stats["down"]["options"]["rate"]["rate"], 12_000_000 // 8)
        self.assertRegex(stats["subnet"], r"^10\.77\.\d+\.0/24$")

        with ProbeServer() as srv:
            res = VM.run(f"{TAG}-net", probe_command("net", host="host.lima.internal", tcp_port=srv.tcp_port,
                                                     udp_port=srv.udp_port, connects=1, echo=5, udp_pps=0), 60,
                         mem=128, cpu=100, net=TAG)
        self.assertEqual(res.code, 0, res.stderr)
        self.assertGreaterEqual(statistics.median(res.phases["echo"]["ms"]), 70)

        schedule = {"repeat": True, "steps": [{"profile": "lan", "for_s": 0.6}, {"profile": "offline", "for_s": 0.6}]}
        VM.board("net-schedule", TAG, "/dev/stdin", stdin=json.dumps(schedule))
        start = json.loads(VM.board("net-stats", TAG))["schedule_start_us"]
        self.assertIsNotNone(start)
        seen = set()
        deadline = time.monotonic() + 5
        while len(seen) < 2 and time.monotonic() < deadline:
            seen.add(json.loads(VM.board("net-stats", TAG))["netem"])
        self.assertEqual(seen, {"lan", "offline"})
        for bad in ('{"repeat": true, "steps": [{"profile": "flaky", "for_s": 1}]}',
                    '{"repeat": true, "steps": [{"profile": "lan", "for_s": 0.001}]}',     # busy-loop
                    '{"repeat": false, "steps": [{"profile": "lan", "for_s": 1}]}'):        # нет after
            with self.assertRaisesRegex(VmError, "расписание", msg=bad):
                VM.board("net-schedule", TAG, "/dev/stdin", stdin=bad)
        self.assertTrue(VM.shell("systemctl", "is-active", f"board-sched-{TAG}.service").strip() == "active")

        VM.board("net-down", TAG)
        VM.board("net-down", TAG)
        with self.assertRaisesRegex(VmError, "не поднят"):
            VM.board("net-stats", TAG)
        self.assertEqual(VM.shell("sudo", "sh", "-c", ROOT_NETNS), self.root_before)

    def test_schedule_without_repeat_ends_with_after(self):
        ns = f"{TAG}s"
        # профиль-расписание виден в net-stats сразу после net-up — первый шаг применён до запуска юнита
        stats = json.loads(VM.shell("sudo", "sh", "-c", f"bash {BOARD_SH} net-up {ns} flaky && "
                                                        f"bash {BOARD_SH} net-stats {ns}"))
        self.assertEqual((stats["profile"], stats["netem"]), ("flaky", "wifi"))
        self.assertIsNotNone(stats["schedule_start_us"])
        once = {"repeat": False, "steps": [{"profile": "offline", "for_s": 0.5}], "after": "4g"}
        VM.board("net-schedule", ns, "/dev/stdin", stdin=json.dumps(once))
        self.assertEqual(json.loads(VM.board("net-stats", ns))["netem"], "offline")
        time.sleep(1.5)
        stats = json.loads(VM.board("net-stats", ns))
        self.assertEqual((stats["profile"], stats["netem"], stats["schedule_start_us"]), ("4g", "4g", None))
        self.assertEqual(VM.shell("sh", "-c", f"systemctl is-active board-sched-{ns}.service || true").strip(),
                         "inactive")
        VM.board("net-down", ns)

    def test_interrupted_net_up_leaves_all_or_nothing(self):
        # TERM в разные моменты net-up (весь он ≈ 35 мс): netns, veth, таблица nft и состояние — все или ни одного
        ns = f"{TAG}i"
        script = (f"for d in 0.005 0.01 0.015 0.02 0.025 0.03 0.06; do bash {BOARD_SH} net-up {ns} 4g & p=$!; "
                  "sleep $d; kill -TERM $p 2>/dev/null; rc=0; wait $p || rc=$?; "
                  f"echo $rc $(test -e /run/netns/board-{ns} && echo 1 || echo 0)"
                  f"$(test -e /sys/class/net/bh-{ns} && echo 1 || echo 0)"
                  f"$(nft list table ip board_{ns} >/dev/null 2>&1 && echo 1 || echo 0)"
                  f"$(test -d /run/board/net/{ns} && echo 1 || echo 0); bash {BOARD_SH} net-down {ns}; done")
        outcomes = [line.split() for line in VM.shell("sudo", "bash", "-c", script, timeout=120).splitlines()]
        self.assertEqual(len(outcomes), 7)
        for rc, state in outcomes:
            self.assertIn(state, ("0000", "1111"), outcomes)
            self.assertIn(rc, ("0", "143"), outcomes)

    def test_cleanup_removes_everything(self):
        # cleanup снимает все объекты board в ВМ — только если чужих нет (ВМ общая)
        foreign = {name for name in board_objects() if TAG not in name}
        if foreign:
            self.skipTest(f"в ВМ чужие объекты board: {sorted(foreign)}")
        VM.board("net-up", f"{TAG}c", "flaky")
        VM.board("cleanup")
        self.assertEqual(board_objects(), set())
        self.assertEqual(VM.shell("sudo", "sh", "-c", ROOT_NETNS), self.root_before)


if __name__ == "__main__":
    unittest.main()
