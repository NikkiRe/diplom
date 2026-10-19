"""neurobot/deploy: юниты из шаблонов с env-файлом робота — директивы есть в systemd 245 (Ubuntu 20.04), порядок
по готовности (PLAN §5.4), супервизия (§5.1), явный ноль; README и RUNBOOK покрывают ключи и коды; скрипты —
bash -n и shellcheck (если есть). systemd-analyze verify — в ВМ (README «Проверка»)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from configparser import RawConfigParser
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))

from neurobot_deploy import render, report  # noqa: E402
from neurobot_deploy.config import DEFAULTS, ROS_KEYS, Config, parse_env  # noqa: E402

V245 = {ln.strip() for ln in (DEPLOY / "verify/systemd245-directives.txt").read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")}
# появились позже 245 — на Ubuntu 20.04 systemd их проигнорирует или откажет
NEWER = {"RestartSteps", "RestartMaxDelaySec", "ExitType", "OpenFile", "SetCredential", "LogFilterPatterns",
         "NotifyAccess=exec"}


def parse_unit(text: str) -> dict:
    """{секция: [(ключ, значение), …]} с повторами ключей."""
    out, section = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line.strip("[]")
            out.setdefault(section, [])
            continue
        key, _, value = line.partition("=")
        out[section].append((key, value))
    return out


def one(unit: dict, section: str, key: str) -> str:
    values = [v for k, v in unit.get(section, []) if k == key]
    assert len(values) == 1, (section, key, values)
    return values[0]


class UnitsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        example = parse_env((DEPLOY / "neurobot.env.example").read_text(encoding="utf-8"))
        cls.cfg = Config(example, "/etc/neurobot/neurobot.env")  # так рендерит install.sh
        cls.texts = render.render(cls.cfg, str(DEPLOY / "units"))
        cls.units = {name: parse_unit(text) for name, text in cls.texts.items()}

    def test_all_directives_exist_in_systemd_245(self):
        for name, unit in self.units.items():
            for items in unit.values():
                for key, _ in items:
                    with self.subTest(unit=name, key=key):
                        self.assertIn(key, V245)
                        self.assertNotIn(key, NEWER)
        journald = parse_unit((DEPLOY / "journald/50-neurobot.conf").read_text(encoding="utf-8"))
        self.assertEqual(set(journald), {"Journal"})
        for key, _ in journald["Journal"]:
            self.assertIn(key, V245)

    def test_supervision_matches_plan(self):
        want = {  # PLAN §5.1, §13.3: Type, WatchdogSec, RestartSec
            "neurobot-safety.service": ("notify", "3", "500ms"),
            "neurobot-executor.service": ("notify", "3", "500ms"),
            "neurobot-perception.service": ("notify", "5", "1"),
            "neurobot-rosbridge.service": ("exec", None, "1"),
            "neurobot-mcp.service": ("exec", None, "1"),
            "neurobot-picoclaw.service": ("exec", None, "2"),
            "neurobot-labworker@.service": ("exec", None, "2"),
        }
        for name, (typ, wd, restart_sec) in want.items():
            u = self.units[name]
            with self.subTest(unit=name):
                self.assertEqual(one(u, "Service", "Type"), typ)
                self.assertEqual(dict(u["Service"]).get("WatchdogSec"), wd)
                self.assertEqual(one(u, "Service", "Restart"), "always")
                self.assertEqual(one(u, "Service", "RestartSec"), restart_sec)
                if typ == "notify":
                    self.assertEqual(one(u, "Service", "NotifyAccess"), "all")
        self.assertGreaterEqual(int(one(self.units["neurobot-perception.service"], "Service", "TimeoutStartSec")), 60)
        # причину называет wait-http (NEUROBOT_LABWORKER_WAIT_S), а не таймаут systemd
        self.assertGreater(int(one(self.units["neurobot-labworker@.service"], "Service", "TimeoutStartSec")),
                           int(DEFAULTS["NEUROBOT_LABWORKER_WAIT_S"]))

    def test_every_component_is_part_of_target_and_never_gives_up(self):
        for name, u in self.units.items():
            if name == "neurobot.target":
                self.assertEqual(one(u, "Install", "WantedBy"), "multi-user.target")
                continue
            with self.subTest(unit=name):
                self.assertEqual(one(u, "Unit", "PartOf"), "neurobot.target")
                self.assertEqual(one(u, "Install", "WantedBy"), "neurobot.target")
                if name != "neurobot-ui.service":      # пульт роботу не нужен: он сдаётся (test_operator_console_unit)
                    self.assertEqual(one(u, "Unit", "StartLimitIntervalSec"), "0")  # [Unit] с v230
                self.assertEqual(one(u, "Service", "EnvironmentFile"), "/etc/neurobot/neurobot.env")

    def after(self, units: dict, name: str) -> set:
        return {u for k, v in units[name]["Unit"] if k == "After" for u in v.split()}

    def test_start_order_is_by_readiness(self):
        drivers = {"neurobot-driver@{}.service".format(d) for d in ("lidar", "base", "camera", "imu")}
        want = {  # каждый юнит — после всех предыдущих уровней PLAN §5.4, §13.3 (отсутствующие After= не мешают)
            "neurobot-driver@.service": set(),
            "neurobot-safety.service": drivers,
            "neurobot-executor.service": drivers | {"neurobot-safety.service"},
            "neurobot-perception.service": drivers | {"neurobot-safety.service", "neurobot-executor.service"},
            "neurobot-rosbridge.service": {"neurobot-safety.service", "neurobot-executor.service",
                                           "neurobot-perception.service"},
            "neurobot-labworker@.service": {"neurobot-perception.service", "neurobot-rosbridge.service"},
            "neurobot-mcp.service": {"neurobot-rosbridge.service"},
            "neurobot-picoclaw.service": {"neurobot-rosbridge.service", "neurobot-mcp.service"},
            # сторож — вместе с драйверами: зависший драйвер не даёт подняться остальным, а перезапускает его он
            "neurobot-watchdog.service": set(),
            # пульт — после safety_gate и ни от кого не зависит: робот работает и без него
            "neurobot-ui.service": {"neurobot-safety.service"},
        }
        self.assertEqual(set(want), set(self.units) - {"neurobot.target"})
        for name, after in want.items():
            with self.subTest(unit=name):
                self.assertEqual(self.after(self.units, name), after)
                self.assertNotIn("Requires", dict(self.units[name]["Unit"]))  # Requires= не даст Restart= поднять юнит

    def test_lab_workers_between_rosbridge_and_mcp(self):
        cfg = Config(dict(self.cfg.values, NEUROBOT_LABWORKERS="yolo vision"), self.cfg.path)
        units = {n: parse_unit(t) for n, t in render.render(cfg, str(DEPLOY / "units")).items()}
        self.assertEqual(self.after(units, "neurobot-mcp.service"), {
            "neurobot-rosbridge.service", "neurobot-labworker@yolo.service", "neurobot-labworker@vision.service"})

    def test_logrotate_for_picoclaw_journal(self):
        text = render.render(self.cfg, str(DEPLOY / "logrotate"))["neurobot-picoclaw"]
        self.assertIn('"/home/jetson/.picoclaw/logs/gateway.log" {', text)
        for word in ("su jetson jetson", "copytruncate", "maxsize 20M", "missingok"):
            self.assertIn(word, text)

    def test_readiness_probes_and_prechecks(self):
        bin_ = "/opt/neurobot/bin/neurobot"
        cases = {"neurobot-driver@.service": ("driver@%i", "wait-topic driver@%i"),
                 "neurobot-safety.service": ("safety", None),
                 "neurobot-executor.service": ("executor", "wait-graph executor"),
                 "neurobot-perception.service": ("perception", "wait-graph perception"),
                 "neurobot-rosbridge.service": ("rosbridge", "wait-port rosbridge"),
                 "neurobot-mcp.service": ("mcp", "wait-mcp"),
                 "neurobot-picoclaw.service": ("picoclaw", "+wait-agent"),
                 "neurobot-labworker@.service": ("labworker@%i", "wait-http labworker@%i"),
                 "neurobot-ui.service": ("ui", "wait-graph ui")}
        for name, (comp, post) in cases.items():
            u = self.units[name]
            with self.subTest(unit=name):
                self.assertEqual(one(u, "Service", "ExecStartPre"), "{} precheck {}".format(bin_, comp))
                if post:
                    want = ("+" + bin_ + " " + post[1:]) if post.startswith("+") else bin_ + " " + post
                    self.assertEqual(one(u, "Service", "ExecStartPost"), want)
                self.assertTrue(one(u, "Service", "ExecStart").startswith("/opt/neurobot/bin/neurobot-run "))
        # явный ноль после любой остановки шлёт только единственный писатель /cmd_vel; ExecStopPost входит в
        # TimeoutStopSec, а запуск rclpy на Nano и под нагрузкой — секунды
        self.assertGreaterEqual(int(one(self.units["neurobot-safety.service"], "Service", "TimeoutStopSec")), 15)
        stop_post = {n: dict(u.get("Service", [])).get("ExecStopPost") for n, u in self.units.items()}
        self.assertEqual({n: v for n, v in stop_post.items() if v}, {"neurobot-safety.service": bin_ + " zero"})
        # safety_gate и исполнитель — из <prefix>/src; ROS-окружение (neurobot-run --ros) дописывает к PYTHONPATH
        for name in ("neurobot-safety.service", "neurobot-executor.service"):
            self.assertIn("PYTHONPATH=/opt/neurobot/src", [v for k, v in self.units[name]["Service"]
                                                           if k == "Environment"])
        self.assertNotIn("--ros", one(self.units["neurobot-mcp.service"], "Service", "ExecStart"))
        # precheck, сам исполнитель и wait-graph — три запуска rclpy (под нагрузкой ВМ — до 14 с каждый)
        self.assertGreaterEqual(int(one(self.units["neurobot-executor.service"], "Service", "TimeoutStartSec")), 60)

    def test_estop_latch_survives_restart_and_busy_camera_is_not_restarted(self):
        # защёлку safety_gate хранит в $STATE_DIRECTORY: RuntimeDirectory стирается при остановке, память — при
        # рестарте; там же метка wait-graph исполнителя и распознавания — переживает рестарт, иначе каждый снова писал
        # бы мимо safety_gate
        states = {n: one(u, "Service", "StateDirectory") for n, u in self.units.items()
                  if dict(u.get("Service", [])).get("StateDirectory")}
        self.assertEqual(states, {"neurobot-safety.service": "neurobot/safety",
                                  "neurobot-executor.service": "neurobot/executor",
                                  "neurobot-perception.service": "neurobot/perception",
                                  "neurobot-ui.service": "neurobot/ui"})
        # --camera rs, камеру держит другой процесс: код 6 — без рестарта по кругу (каждый грузил бы модель заново)
        self.assertEqual(one(self.units["neurobot-perception.service"], "Service", "RestartPreventExitStatus"), "6")

    def test_operator_console_unit(self):
        # пульт: свой пользователь (агент не читает токен), токен в StateDirectory 0700, свой предел памяти и slice,
        # без Requires= и без ExecStopPost (ноль шлёт только safety_gate); WATCHDOG — от тика узла
        u = self.units["neurobot-ui.service"]
        self.assertEqual(one(u, "Service", "User"), "neurobot-ui")
        self.assertNotEqual(one(u, "Service", "User"), self.cfg.get("NEUROBOT_USER"))
        self.assertEqual(one(u, "Service", "StateDirectoryMode"), "0700")
        self.assertEqual(one(u, "Service", "MemoryMax"), "128M")      # шлюз ≈43 МиБ + doctor по кнопке ≈40 (soak)
        self.assertTrue(one(u, "Service", "Slice").endswith(".slice"))
        self.assertEqual((one(u, "Service", "Type"), one(u, "Service", "NotifyAccess"), one(u, "Service", "Restart")),
                         ("notify", "main", "always"))
        # постоянный отказ пульта не должен съедать ядро: код 2 (env, порт, vendor) — без рестарта, рестарт через 3 с,
        # 5 неудачных запусков за 5 мин — сдаётся (doctor — предупреждение); под нагрузкой уступает perception
        self.assertEqual((one(u, "Service", "RestartSec"), one(u, "Service", "RestartPreventExitStatus")), ("3", "2"))
        self.assertEqual((one(u, "Unit", "StartLimitIntervalSec"), one(u, "Unit", "StartLimitBurst")), ("300", "5"))
        self.assertEqual((one(u, "Service", "CPUWeight"), one(u, "Service", "Nice")), ("20", "10"))
        # арены glibc потоков HTTP: две и порог trim/mmap — память возвращается после переподключений
        self.assertIn("MALLOC_ARENA_MAX=2 MALLOC_TRIM_THRESHOLD_=131072",
                      [v for k, v in u["Service"] if k == "Environment"])
        self.assertEqual(one(u, "Service", "NoNewPrivileges"), "yes")
        self.assertIn("PYTHONPATH=/opt/neurobot/src", [v for k, v in u["Service"] if k == "Environment"])
        self.assertEqual(one(u, "Service", "ExecStart"), "/opt/neurobot/bin/neurobot-run --ros --var NEUROBOT_UI_CMD")
        with self.assertRaisesRegex(Exception, "свой пользователь"):
            render.values(Config(dict(self.cfg.values, NEUROBOT_UI_USER="jetson"), self.cfg.path))
        with self.assertRaisesRegex(Exception, "не root"):
            render.values(Config(dict(self.cfg.values, NEUROBOT_UI_USER="root"), self.cfg.path))
        # в составе по умолчанию пульта нет: ставится по согласию лаборатории
        self.assertNotIn("ui", DEFAULTS["NEUROBOT_COMPONENTS"].split())
        self.assertNotIn("neurobot-ui.service", render.enable_list(self.cfg))
        cfg = Config(dict(self.cfg.values, NEUROBOT_COMPONENTS=self.cfg.get("NEUROBOT_COMPONENTS") + " ui"),
                     self.cfg.path)
        self.assertEqual(render.enable_list(cfg)[-2:], ["neurobot-ui.service", "neurobot-watchdog.service"])

    def test_rendered_values(self):
        ex = self.units["neurobot-safety.service"]
        self.assertEqual(one(ex, "Service", "User"), "jetson")
        self.assertEqual(one(ex, "Service", "SupplementaryGroups"), "dialout video plugdev")
        self.assertFalse(any("@" in t.replace("driver@", "").replace("labworker@", "").replace("%i", "")
                             for t in self.texts.values()))

    def test_env_example_is_parseable_and_documented(self):
        readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
        example = parse_env((DEPLOY / "neurobot.env.example").read_text(encoding="utf-8"))
        fake = parse_env((DEPLOY / "fakes/fake.env").read_text(encoding="utf-8"))

        def generic(keys):
            return {re.sub(r"NEUROBOT_(DRIVER|LABWORKER)_[a-z]\w*?_(CMD|DEVICE|TOPIC|USB|URL|MODULES|CAMERA|FRESH)$",
                           r"NEUROBOT_\1_<имя>_\2", k) for k in keys}
        keys = generic((set(DEFAULTS) | set(example) | set(fake)) - set(ROS_KEYS))
        keys |= {"NEUROBOT_LABWORKER_<имя>_{}".format(f) for f in ("CMD", "URL", "MODULES", "CAMERA")}
        for key in sorted(keys):
            with self.subTest(key=key):
                self.assertIn("`{}`".format(key), readme)

    def test_env_files_read_the_same_by_systemd_rules_and_bash(self):
        # env-файл читают systemd (EnvironmentFile=) и люди — `source` в bash: значения не должны расходиться, а
        # «>» без кавычек bash принял бы за перенаправление в файл
        for name in ("neurobot.env.example", "fakes/fake.env"):
            path = DEPLOY / name
            with tempfile.TemporaryDirectory() as tmp, self.subTest(env=name):
                r = subprocess.run(["bash", "-c", 'set -a; source "$1"; env -0', "bash", str(path)], cwd=tmp,
                                   capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(os.listdir(tmp), [])
                got = dict(item.split("=", 1) for item in r.stdout.split("\0") if "=" in item)
                for key, value in parse_env(path.read_text(encoding="utf-8")).items():
                    self.assertEqual(got.get(key), value, key)

    def test_runbook_has_every_section_codes_point_to(self):
        runbook = (DEPLOY / "RUNBOOK.md").read_text(encoding="utf-8")
        sections = set(re.findall(r"^#{2,3} (\d+(?:\.\d+)?)\. ", runbook, flags=re.M))
        for code, (section, _) in report.CODES.items():
            with self.subTest(code=code):
                self.assertIn(section, sections)
                self.assertIn("`{}`".format(code), runbook)

    def test_ini_shape(self):
        for name, text in self.texts.items():
            with self.subTest(unit=name):
                p = RawConfigParser(strict=False)
                p.optionxform = str
                p.read_string(text)


class ScriptsTest(unittest.TestCase):
    SCRIPTS = ["install.sh", "bin/neurobot-run", "fakes/setup_stand.sh", "verify/systemd245_verify.sh"]

    def test_bash_syntax(self):
        for s in self.SCRIPTS:
            with self.subTest(script=s):
                r = subprocess.run(["bash", "-n", str(DEPLOY / s)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipUnless(shutil.which("shellcheck"), "нет shellcheck (uvx --from shellcheck-py shellcheck)")
    def test_shellcheck(self):
        r = subprocess.run(["shellcheck", "-x"] + [str(DEPLOY / s) for s in self.SCRIPTS],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_neurobot_run_resolves_variable_with_quotes(self):
        env = dict(os.environ, CMD="printf '%s|' 'два слова' x", NEUROBOT_ROS_SETUP="")
        r = subprocess.run([str(DEPLOY / "bin/neurobot-run"), "--var", "CMD"], capture_output=True, text=True, env=env)
        self.assertEqual((r.returncode, r.stdout), (0, "два слова|x|"))
        r = subprocess.run([str(DEPLOY / "bin/neurobot-run"), "--ros", "--var", "CMD"], capture_output=True,
                           text=True, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("нет окружения ROS 2", r.stderr)
        r = subprocess.run([str(DEPLOY / "bin/neurobot-run"), "--var", "NOPE"], capture_output=True, text=True,
                           env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("NOPE не задана", r.stderr)

    def test_neurobot_run_sources_ros_setup_before_exec(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup = os.path.join(tmp, "setup.bash")
            with open(setup, "w", encoding="utf-8") as f:
                f.write('export FROM_SETUP="ok:$#"\n')
            env = dict(os.environ, NEUROBOT_ROS_SETUP=setup)
            r = subprocess.run([str(DEPLOY / "bin/neurobot-run"), "--ros", "--", "bash", "-c", 'echo "$FROM_SETUP"'],
                               capture_output=True, text=True, env=env)
        self.assertEqual(r.stdout.strip(), "ok:0")  # setup.bash не видит аргументов вызова


if __name__ == "__main__":
    unittest.main()
