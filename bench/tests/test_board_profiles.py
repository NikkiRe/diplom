"""bench.board.profiles: проверка profiles.json (ключи, источники, ограничения ядра и netem) и выводимые величины."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.board import profiles  # noqa: E402
from bench.board.profiles import ProfileError  # noqa: E402

DOC = json.loads(profiles.PROFILES_PATH.read_text(encoding="utf-8"))


def broken(mutate) -> dict:
    doc = copy.deepcopy(DOC)
    mutate(doc)
    return doc


class ProfilesFileTest(unittest.TestCase):
    def test_file_is_valid_and_complete(self):
        doc = profiles.load()
        self.assertEqual(doc["memory"]["levels_mib"], [1024, 512, 256, 128, 96, 64, 48, 32, 24, 16])
        self.assertEqual(doc["cpu"]["quota_pct"], [400, 200, 100, 50, 25, 10, 5])
        self.assertEqual(doc["cpu"]["cores"], [4, 2, 1])
        self.assertEqual(set(doc["disk"]), {"nvme", "sd-good", "sd-slow", "sd-awful"})
        self.assertEqual(set(doc["net"]["profiles"]), {"lan", "wifi", "wifi-weak", "4g", "3g", "slow-3g", "edge",
                                                       "gprs", "lossy", "satellite", "offline", "flaky"})
        self.assertEqual(set(doc["boards"]), {"jetson-nano-4g", "jetson-nano-2g", "pi-zero-2w", "potato", "abyss"})

    def test_every_profile_names_a_source_or_assumption(self):
        for section in (DOC["disk"], DOC["net"]["profiles"]):
            for name, entry in section.items():
                self.assertTrue(entry["source"].strip(), name)
        for name, board in DOC["boards"].items():
            self.assertIn("допущение", board["basis"], name)   # CPU пресетов — допущение до калибровки

    def test_lossy_uses_gilbert_elliott_and_flaky_schedules_an_outage(self):
        lossy, flaky = DOC["net"]["profiles"]["lossy"], DOC["net"]["profiles"]["flaky"]
        self.assertIn("gemodel", lossy["up"])
        self.assertIn("gemodel", lossy["down"])
        outage = [s for s in flaky["schedule"]["steps"]
                  if profiles.round_trip_loss(DOC["net"]["profiles"][s["profile"]]) == 1]
        self.assertTrue(outage)
        self.assertTrue(profiles.is_schedule(flaky))

    def test_load_reads_a_given_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text(json.dumps(broken(lambda d: d.update(format="board-profiles/0"))), encoding="utf-8")
            with self.assertRaisesRegex(ProfileError, "^format"):
                profiles.load(path)


class ValidationTest(unittest.TestCase):
    def assertRejected(self, mutate, where: str):
        with self.assertRaises(ProfileError) as ctx:
            profiles.validate(broken(mutate))
        self.assertTrue(str(ctx.exception).startswith(where), str(ctx.exception))

    def test_structure(self):
        self.assertRejected(lambda d: d.pop("tasks_max"), "profiles")
        self.assertRejected(lambda d: d["memory"].update(extra=1), "memory")
        self.assertRejected(lambda d: d["net"]["queue"].pop("source"), "net.queue")

    def test_wrong_types_are_profile_errors(self):
        # не объект или не хешируемое значение — ProfileError с путём к ключу, а не AttributeError/TypeError
        self.assertRejected(lambda d: d.update(disk=[]), "disk")
        self.assertRejected(lambda d: d.update(boards=[]), "boards")
        self.assertRejected(lambda d: d["net"].update(profiles=[]), "net.profiles")
        self.assertRejected(lambda d: d["net"]["profiles"].update(odd=1), "net.profiles.odd")
        self.assertRejected(lambda d: d["boards"]["potato"].update(disk=["sd-awful"]), "boards.potato.disk")
        self.assertRejected(lambda d: d["boards"]["potato"].update(cores=True), "boards.potato.cores")

    def test_levels_strictly_decrease(self):
        self.assertRejected(lambda d: d["memory"].update(levels_mib=[64, 64]), "memory.levels_mib")
        self.assertRejected(lambda d: d["cpu"].update(cores=[1, 2]), "cpu.cores")
        self.assertRejected(lambda d: d["memory"].update(levels_mib=[]), "memory.levels_mib")
        self.assertRejected(lambda d: d["memory"].update(levels_mib=[64, True]), "memory.levels_mib[1]")

    def test_quota_below_kernel_minimum(self):
        # 5 % за 10 мс — 0,5 мс квоты, ядро требует ≥ 1 мс
        self.assertRejected(lambda d: d["cpu"].update(period_ms=10), "cpu.period_ms")

    def test_queue_bounds(self):
        self.assertRejected(lambda d: d["net"]["queue"].update(max_packets=10), "net.queue.max_packets")

    def test_disk_source_name_and_limits(self):
        self.assertRejected(lambda d: d["disk"]["sd-good"].update(source=" "), "disk.sd-good.source")
        self.assertRejected(lambda d: d["disk"]["sd-good"].update(wbps=0), "disk.sd-good.wbps")
        self.assertRejected(lambda d: d["disk"]["sd-good"].update(iops=1), "disk.sd-good")
        self.assertRejected(lambda d: d["disk"].update({"SD": {"source": "x"}}), "disk.SD")

    def test_net_direction_rules(self):
        wifi = ("net", "profiles", "wifi", "up")

        def edit(**fields):
            return lambda d: d[wifi[0]][wifi[1]][wifi[2]][wifi[3]].update(fields)

        self.assertRejected(edit(rate_kbit=0), "net.profiles.wifi.up")          # разброс без rate
        self.assertRejected(edit(jitter_ms=5), "net.profiles.wifi.up")          # разброс больше задержки
        self.assertRejected(edit(loss_pct=101), "net.profiles.wifi.up.loss_pct")
        self.assertRejected(edit(loss_pct=1, gemodel={"p_pct": 1, "r_pct": 1, "bad_loss_pct": 1,
                                                      "good_loss_pct": 0}), "net.profiles.wifi.up")
        self.assertRejected(edit(gemodel={"p_pct": 0, "r_pct": 1, "bad_loss_pct": 1, "good_loss_pct": 0}),
                            "net.profiles.wifi.up.gemodel")
        self.assertRejected(edit(burst=1), "net.profiles.wifi.up")

    def test_schedule_steps_reference_static_profiles(self):
        steps = ("net", "profiles", "flaky", "schedule", "steps")

        def step(**fields):
            return lambda d: d[steps[0]][steps[1]][steps[2]][steps[3]][steps[4]][0].update(fields)

        self.assertRejected(step(profile="flaky"), "net.profiles.flaky.schedule.steps[0].profile")
        self.assertRejected(step(profile="nope"), "net.profiles.flaky.schedule.steps[0].profile")
        self.assertRejected(step(profile=["wifi"]), "net.profiles.flaky.schedule.steps[0].profile")
        self.assertRejected(step(for_s=0.05), "net.profiles.flaky.schedule.steps[0].for_s")   # < schedule_min_step_s
        self.assertRejected(lambda d: d["net"]["profiles"]["flaky"]["schedule"].update(repeat="yes"),
                            "net.profiles.flaky.schedule.repeat")
        self.assertRejected(lambda d: d["net"].update(schedule_min_step_s=0), "net.schedule_min_step_s")

    def test_schedule_after_only_and_always_without_repeat(self):
        schedule = ("net", "profiles", "flaky", "schedule")

        def edit(**fields):
            return lambda d: d[schedule[0]][schedule[1]][schedule[2]][schedule[3]].update(fields)

        self.assertRejected(edit(after="wifi"), "net.profiles.flaky.schedule")              # repeat: true
        self.assertRejected(edit(repeat=False), "net.profiles.flaky.schedule")              # нет after
        self.assertRejected(edit(repeat=False, after="flaky"), "net.profiles.flaky.schedule.after")
        profiles.validate(broken(edit(repeat=False, after="wifi")))

    def test_boards_use_existing_levels(self):
        self.assertRejected(lambda d: d["boards"]["potato"].update(memory=100), "boards.potato.memory")
        self.assertRejected(lambda d: d["boards"]["potato"].update(net="5g"), "boards.potato.net")
        self.assertRejected(lambda d: d["boards"]["potato"].update(basis=""), "boards.potato.basis")


class DerivedTest(unittest.TestCase):
    def test_direction_loss_random_and_gemodel(self):
        self.assertEqual(profiles.direction_loss({}), 0)
        self.assertAlmostEqual(profiles.direction_loss({"loss_pct": 1}), 0.01)
        # стационарная доля плохого состояния p/(p+r) = 1/31, в нём теряется 70 %
        ge = {"gemodel": {"p_pct": 1, "r_pct": 30, "bad_loss_pct": 70, "good_loss_pct": 0}}
        self.assertAlmostEqual(profiles.direction_loss(ge), 0.7 / 31)

    def test_round_trip_loss_and_added_rtt(self):
        profile = {"up": {"loss_pct": 10, "delay_ms": 20}, "down": {"loss_pct": 20, "delay_ms": 30}}
        self.assertAlmostEqual(profiles.round_trip_loss(profile), 1 - 0.9 * 0.8)
        self.assertEqual(profiles.added_rtt_ms(profile), 50)
        self.assertEqual(profiles.round_trip_loss(DOC["net"]["profiles"]["offline"]), 1)


if __name__ == "__main__":
    unittest.main()
