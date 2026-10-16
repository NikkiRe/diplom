"""bench.sysfsim без hw-mcp: сценарии и их проверка, модели датчиков, детерминизм, отказы, замкнутый контур
реле → температура, атомарность записи, дерево sysfs, линии-файлы, журнал истины и CLI."""
from __future__ import annotations

import json
import os
import random
import signal
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.sysfsim import physics, scenario  # noqa: E402
from bench.sysfsim.gpio import CONFIGFS, FilesGpio, GpioSim  # noqa: E402
from bench.sysfsim.runner import (CgroupLoad, ClockHandler, ClockServer, Runner, run_real,  # noqa: E402
                                  serve_virtual, wait)
from bench.sysfsim.sensors import MODELS, PARAMS, iio_fractional  # noqa: E402
from bench.sysfsim.tree import FIXTURE, Tree, atomic_write, break_attr, init_root  # noqa: E402
from bench.sysfsim.world import ClockError, World, summarize  # noqa: E402

ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
DECLARED = ROOT / "bench/sysfsim/configs/declared.sim.json"
# CLI, в котором функция-«заминка» (имя в модуле __main__ или в его классе) печатает «stall», спит 1 с — окно,
# в которое тест посылает сигналы, — и печатает «done», если сон не прерван
STALL = """import sys, time
from bench.sysfsim import __main__ as cli
owner, _, name = sys.argv[1].rpartition(".")
setattr(getattr(cli, owner) if owner else cli, name,
        lambda *a: (print("stall", flush=True), time.sleep(1), print("done", flush=True)))
sys.exit(cli.main(sys.argv[2:]))
"""


def make(**fields) -> scenario.Scenario:
    """Короткий сценарий (10 мин от полудня) с заданными разделами."""
    return scenario.parse({"format": "sysfsim/1", "name": "test", "duration_s": 600, "start": "12:00"} | fields)


def renders(records, device: str) -> list:
    return [r for r in records if r.type == "render" and r.data["device"] == device]


def play(world: World, times) -> list:
    out = []
    for t in times:
        out += world.advance(t)
    return out


def state_at(records, t: float) -> dict:
    """Содержимое атрибутов и линий на момент t по записям мира (последнее не позже t)."""
    state = {}
    for r in records:
        if r.t > t:
            break
        if r.type == "render":
            state[r.data["device"]] = r.data["attrs"]
        elif r.type in ("gpio", "link"):
            state[r.type, r.data["device"]] = r.data.get("value", r.data.get("present"))
    return state


class ScenarioTest(unittest.TestCase):
    def test_builtin_scenarios_are_valid_and_every_situation_happens(self):
        self.assertEqual(scenario.builtin(), ["day", "dense"])
        for name in scenario.builtin():
            summary = summarize(scenario.load(name), seed=1)
            self.assertNotIn(None, summary["situations"].values(), name)
        dense = scenario.load("dense")
        self.assertEqual(dense.duration_s, 900)
        self.assertGreaterEqual(len(dense.questions), 5)
        self.assertEqual({f["kind"] for f in dense.faults}, set(scenario.FAULTS))
        self.assertLessEqual({"overheat", "door_left_open", "motion_at_night", "leak", "sensor_glitch", "sensor_lost"},
                             {s["kind"] for s in dense.situations})
        self.assertEqual(scenario.load("day").duration_s, 8 * 3600)

    def test_every_device_parameter_has_a_check(self):
        for params in PARAMS.values():
            self.assertLessEqual(params.keys(), scenario.PARAM_CHECKS.keys())

    def test_errors_name_the_field(self):
        heat = {"t": 1, "kind": "heat", "zone": "kitchen", "rise_c": 1, "duration_s": 1}
        cases = [
            ({"bogus": 1}, "сценарий.bogus: неизвестный ключ"),
            ({"format": "sysfsim/2"}, "сценарий.format: неизвестное значение"),
            ({"duration_s": True}, "сценарий.duration_s: ожидается число"),
            ({"start": "25:00"}, "сценарий.start: ожидается время"),
            ({"zones": {"attic": {}}}, "сценарий.zones: неизвестное значение 'attic'"),
            ({"zones": {"kitchen": {"tau_s": 0}}}, "сценарий.zones.kitchen.tau_s: ожидается число > 0"),
            ({"devices": {"kitchen_bme280": {"fail_rate": 0.1}}}, "devices.kitchen_bme280.fail_rate: неизвестный ключ"),
            ({"devices": {"boiler_pipe_ds18b20": {"period_s": 0.5}}}, "не чаще раза в 0.75 с"),
            ({"events": [heat | {"zone": "kitchn"}]}, "events[0].zone: неизвестное значение 'kitchn'"),
            ({"events": [heat | {"kind": "fire"}]}, "events[0].kind: неизвестное значение 'fire'"),
            ({"events": [heat | {"t": 600}]}, "events[0].t: момент 600 не раньше конца"),
            ({"events": [{"t": 1, "kind": "door", "device": "pir_hall", "duration_s": 1}]}, "events[0].device"),
            ({"faults": [{"t": 1, "kind": "por85", "device": "kitchen_bme280", "duration_s": 1}]}, "faults[0].device"),
            ({"faults": [{"t": 1, "rate_per_h": 1, "kind": "stuck", "device": "kitchen_bme280", "duration_s": 1}]},
             "ровно одно из t"),
            ({"faults": [{"t": 1, "kind": "spike", "device": "kitchen_bme280", "amplitude": 1, "duration_s": 1}]},
             "faults[0].property: обязательное поле"),
            ({"faults": [{"t": 1, "kind": "stuck", "device": "kitchen_bme280", "property": "pressure",
                          "duration_s": 1}]}, "действует на всё устройство"),
            ({"faults": [{"t": 1, "kind": "bounce", "device": "door_entry", "ms": 5, "pulses": 0, "duration_s": 1}]},
             "faults[0].pulses: ожидается целое в [1; 50]"),
            ({"faults": [{"t": 1, "kind": "bounce", "device": "door_entry", "ms": 500, "pulses": 1, "duration_s": 1}]},
             "faults[0].ms: ожидается число в [0; 100]"),
            ({"questions": [{"id": "q", "t": 1, "text": "?", "expect": {"device": "pir_hall", "property": "motion"}}]},
             "у pir_hall нет свойства 'motion'"),
            ({"situations": [{"id": "s", "kind": "x", "t": 1, "deadline_s": 1, "expect": "actuate"}]}, "action"),
            ({"situations": [{"id": "s", "kind": "x", "deadline_s": 1, "expect": "notify",
                              "when": {"device": "kitchen_bme280", "property": "temperature", "ge": 1, "le": 2}}]},
             "ровно одно сравнение"),
            ({"situations": [{"id": "s", "kind": "x", "deadline_s": 1, "expect": "notify",
                              "when": {"device": "pir_hall", "property": "state", "ge": 1}}]}, "только eq"),
            ({"questions": [{"id": "x", "t": 1, "text": "?", "expect": {"device": "pir_hall", "property": "state"}}],
              "situations": [{"id": "x", "kind": "x", "t": 1, "deadline_s": 1, "expect": "notify"}]},
             "повторяются id"),
        ]
        for fields, message in cases:
            with self.subTest(message):
                with self.assertRaises(scenario.ScenarioError) as e:
                    make(**fields)
                self.assertIn(message, str(e.exception))

    def test_load_reports_bad_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp, "bad.json")
            bad.write_text("{")
            with self.assertRaisesRegex(scenario.ScenarioError, "bad.json: не JSON"):
                scenario.load(str(bad))
            with self.assertRaisesRegex(scenario.ScenarioError, "absent.json"):
                scenario.load(str(Path(tmp, "absent.json")))


class ModelTest(unittest.TestCase):
    def test_iio_fractional_matches_kernel_format(self):
        self.assertEqual(iio_fractional(22510, 1), "22510.000000000")
        self.assertEqual(iio_fractional(25939201, 256000), "101.325003906")
        self.assertEqual(iio_fractional(-5, 1000), "-0.005000000")
        self.assertEqual(iio_fractional(-1500, 1000), "-1.500000000")
        self.assertEqual(iio_fractional(0, 7), "0.000000000")

    def test_codes_and_attributes_follow_drivers(self):
        bme = MODELS["bme280"].encode({"temperature": 22.514, "humidity": 41.2304, "pressure": 101325.004})
        self.assertEqual(bme, {"in_temp_raw": "2251", "in_temp_input": "22510.000000000",
                               "in_humidityrelative_raw": "42220", "in_humidityrelative_input": "41230.468750000",
                               "in_pressure_raw": "25939201", "in_pressure_input": "101.325003906"})
        self.assertEqual(MODELS["bme280"].read(bme), {"temperature": 22.51, "humidity": 41.230469,
                                                      "pressure": 101325.003906})
        self.assertEqual(MODELS["bme280"].encode({"temperature": 0, "humidity": 120, "pressure": 1e5})
                         ["in_humidityrelative_raw"], "102400")
        dht = MODELS["dht11"]
        self.assertEqual(dht.encode({"temperature": 23.6, "humidity": 95}), {"in_temp_input": "24000",
                                                                          "in_humidityrelative_input": "90000"})
        ds = MODELS["ds18b20"]
        cases = ((21.44, "21437"), (-0.0625, "-62"), (-10.125, "-10125"), (85.0, "85000"), (200, "125000"))
        for celsius, text in cases:
            self.assertEqual(ds.encode({"temperature": celsius}), {"temperature": text})
        self.assertEqual(ds.read({"temperature": "21437"}), {"temperature": 21.437})
        ads = MODELS["ads1115"]
        self.assertEqual(ads.encode({"voltage0": 1.0}), {"in_voltage0_raw": "16000"})
        self.assertEqual(ads.encode({"voltage0": 3.0}), {"in_voltage0_raw": "32767"})
        self.assertEqual(ads.read({"in_voltage0_raw": "16000"}), {"voltage0": 1.0})
        self.assertEqual(MODELS["soc"].encode({"temperature1": 48.3}), {"temp": "48500", "hwmon0/temp1_input": "48500"})

    def test_readings_in_a_run_are_quantized(self):
        records = play(World(scenario.load("dense"), 1), range(0, 901, 5))
        steps = {"kitchen_bme280": {"temperature": 0.01}, "bathroom_dht11": {"humidity": 1.0, "temperature": 1.0},
                 "bathroom_leak": {"voltage0": 2.048 / 32768}, "board_cpu_thermal": {"temperature1": 0.5}}
        for device, props in steps.items():
            for r in renders(records, device):
                for prop, step in props.items():
                    if (v := r.data["readings"][prop]) is not None:   # hw-mcp округляет до 1e-6
                        self.assertLessEqual(abs(v - round(v / step) * step), 6e-7, (device, prop, v))
        for r in renders(records, "boiler_pipe_ds18b20"):   # 1/16 °C, затем м°C с отбрасыванием
            sixteenths = r.data["readings"]["temperature"] * 16
            self.assertAlmostEqual(sixteenths, round(sixteenths), delta=0.017)


class WorldTest(unittest.TestCase):
    def test_state_is_a_function_of_seed_and_time(self):
        sc = scenario.load("dense")
        rng, coarse = random.Random(0), [0.0]
        while coarse[-1] < 900:   # неровные шаги, кратные 1 мс
            coarse.append(min(900.0, round(coarse[-1] + rng.uniform(0.1, 12), 3)))
        fine = sorted({*coarse, *(k * 0.25 for k in range(3601))})
        a, b = play(World(sc, 7), fine), play(World(sc, 7), coarse)
        for t in coarse:
            self.assertEqual(state_at(a, t), state_at(b, t), t)
        situations = [(r.t, r.data["id"]) for r in a if r.type == "situation"]
        self.assertEqual(situations, [(r.t, r.data["id"]) for r in b if r.type == "situation"])
        other = play(World(sc, 8), coarse)
        self.assertNotEqual(state_at(b, 450.0), state_at(other, 450.0))

    def test_clock_does_not_go_back(self):
        world = World(make(), 1)
        world.advance(10)
        with self.assertRaises(ClockError):
            world.advance(9.5)

    def test_clock_jump_skips_intermediate_moments(self):
        """Скачок часов не копит записи: по одному обновлению датчика и одному снимку истины."""
        world = World(make(duration_s=20000, devices={"pir_hall": {"false_per_h": 0}}), 1)
        records = world.advance(20000)
        self.assertEqual(Counter(r.type for r in records), {"gpio": 2, "render": 5, "truth": 1})
        self.assertEqual([r.t for r in records if r.type == "truth"], [20000])

    def test_every_fault_kind(self):
        sc = make(
            devices={"bathroom_dht11": {"fail_rate": 0.0}},
            events=[{"t": 100, "kind": "heat", "zone": "boiler_pipe", "rise_c": 30, "duration_s": 400},
                    {"t": 10, "kind": "door", "device": "door_entry", "duration_s": 100}],
            faults=[{"t": 20, "kind": "por85", "device": "boiler_pipe_ds18b20", "duration_s": 10},
                    {"t": 50, "kind": "disconnect", "device": "bathroom_dht11", "duration_s": 30},
                    {"t": 100, "kind": "read_error", "device": "kitchen_bme280", "duration_s": 10},
                    {"t": 200, "kind": "read_error", "device": "boiler_pipe_ds18b20", "duration_s": 5},
                    {"t": 120, "kind": "stuck", "device": "boiler_pipe_ds18b20", "duration_s": 60},
                    {"t": 150, "kind": "drift", "device": "bathroom_dht11", "property": "humidity", "per_h": 360,
                     "duration_s": 100},
                    {"t": 200, "kind": "spike", "device": "kitchen_bme280", "property": "temperature", "amplitude": 10,
                     "duration_s": 1},
                    {"t": 220, "kind": "noise_burst", "device": "bathroom_leak", "sigma": 0.1, "duration_s": 60},
                    {"t": 300, "kind": "dht_fail_rate", "device": "bathroom_dht11", "p": 1.0, "duration_s": 20},
                    {"t": 105, "kind": "bounce", "device": "door_entry", "ms": 20, "pulses": 5, "duration_s": 10},
                    {"rate_per_h": 3600, "kind": "spike", "device": "kitchen_bme280", "property": "pressure",
                     "amplitude": 500, "duration_s": 0.5}])
        world = World(sc, 3)
        records = play(world, range(0, 601))

        def window(device, t0, t1):
            return [r for r in renders(records, device) if t0 <= r.t < t1]

        self.assertTrue(all(r.data["readings"]["temperature"] == 85.0 for r in window("boiler_pipe_ds18b20", 20, 30)))
        self.assertNotEqual(window("boiler_pipe_ds18b20", 30, 31)[0].data["readings"]["temperature"], 85.0)
        links = [(r.t, r.data["present"]) for r in records if r.type == "link"]
        self.assertEqual(links, [(50, False), (80, True)])
        self.assertFalse([r for r in window("bathroom_dht11", 50, 80)])
        relink = records.index(next(r for r in records if r.type == "link" and r.data["present"]))
        self.assertEqual(records[relink - 1].type, "render")   # атрибуты обновлены до возвращения ссылки
        for r in window("kitchen_bme280", 100, 110):
            self.assertEqual(set(r.data["attrs"].values()), {None})
            self.assertEqual(set(r.data["readings"].values()), {None})
        for r in window("boiler_pipe_ds18b20", 200, 205):   # w1_therm при сбое отдаёт пустое чтение
            self.assertEqual((r.data["attrs"], r.data["readings"]), ({"temperature": ""}, {"temperature": None}))
        stuck = {r.data["readings"]["temperature"] for r in window("boiler_pipe_ds18b20", 120, 180)}
        truth = {r.data["values"]["temperature"] for r in window("boiler_pipe_ds18b20", 120, 180)}
        self.assertEqual(len(stuck), 1)
        self.assertGreater(max(truth) - min(truth), 4)
        last = window("bathroom_dht11", 248, 250)[0].data
        self.assertAlmostEqual(last["readings"]["humidity"] - last["values"]["humidity"], 9.8, delta=1.5)
        spike = window("kitchen_bme280", 200, 201)[0].data
        self.assertAlmostEqual(spike["readings"]["temperature"] - spike["values"]["temperature"], 10, delta=0.05)

        def deviation(t0, t1):
            return statistics.pstdev(r.data["readings"]["voltage0"] - r.data["values"]["voltage0"]
                                     for r in window("bathroom_leak", t0, t1))
        self.assertGreater(deviation(220, 280), 0.05)
        self.assertLess(deviation(400, 600), 0.001)
        self.assertTrue(all(r.data["attrs"]["in_temp_input"] is None for r in window("bathroom_dht11", 300, 320)))
        self.assertTrue(all(r.data["readings"]["temperature"] is not None for r in window("bathroom_dht11", 320, 600)))
        door = [r for r in records if r.type == "gpio" and r.data["device"] == "door_entry" and r.t > 0]
        opening, closing = [r for r in door if 10 <= r.t < 11], [r for r in door if 110 <= r.t < 111]
        self.assertEqual([r.data["bounce"] for r in opening], [False] + [True] * 4)
        self.assertEqual(len(closing), 11)
        self.assertLess(closing[-1].t - closing[0].t, 0.02)
        self.assertEqual([r.data["value"] for r in (opening[-1], closing[-1])], [True, False])
        spikes = [f for f in world.faults if f.prop == "pressure"]
        self.assertTrue(480 <= len(spikes) <= 720, len(spikes))   # пуассоновский поток 1/с на 600 с
        self.assertEqual([f.t0 for f in spikes], [f.t0 for f in World(sc, 3).faults if f.prop == "pressure"])

    def test_dht_fail_rate_is_a_probability(self):
        sc = make(duration_s=2000, devices={"bathroom_dht11": {"fail_rate": 0.3}})
        failed = [r.data["readings"]["humidity"] is None for r in renders(play(World(sc, 5), range(2001)),
                                                                         "bathroom_dht11")]
        self.assertAlmostEqual(sum(failed) / len(failed), 0.3, delta=0.07)

    def test_relay_closes_the_loop(self):
        sc = make(events=[{"t": 0, "kind": "heat", "zone": "kitchen", "rise_c": 25, "duration_s": 600}])
        idle, fan = World(sc, 1), World(sc, 1)
        play(idle, range(0, 451))
        play(fan, range(0, 301))
        fan.set_actuator("relay_fan", True)
        before = fan.truth("kitchen_bme280", "temperature", 300)
        records = play(fan, range(301, 451))
        self.assertTrue(fan.truth("relay_fan", "state", 450))
        after = fan.truth("kitchen_bme280", "temperature", 450)
        self.assertLess(after, before - 3)
        self.assertLess(after, idle.truth("kitchen_bme280", "temperature", 450))
        readings = [r.data["readings"]["temperature"] for r in renders(records, "kitchen_bme280")]
        self.assertLess(readings[-1], readings[0] - 3)

    def test_bounce_never_overtakes_the_next_front(self):
        """Дребезг длиннее открытия двери обрезается следующим фронтом: переходы упорядочены и чередуются,
        записи линии одинаковы при любом шаге часов."""
        doors = [(10, 0.005), (10.01, 5)]
        sc = make(events=[{"t": t, "kind": "door", "device": "door_entry", "duration_s": d} for t, d in doors],
                  faults=[{"t": 9, "kind": "bounce", "device": "door_entry", "ms": 20, "pulses": 3, "duration_s": 10}])
        fronts = [(t, True) for t, _ in doors] + [(t + d, False) for t, d in doors]
        for seed in range(20):
            edges = World(sc, seed).signals["door_entry"].edges
            self.assertEqual([e.t for e in edges], sorted(e.t for e in edges), seed)
            self.assertTrue(all(a.level != b.level for a, b in zip(edges, edges[1:])), seed)
            self.assertEqual([(e.t, e.level) for e in edges if not e.bounce], sorted(fronts))

        def door(step: float) -> list:
            records = play(World(sc, 11), [k * step for k in range(round(12 / step) + 1)])
            return [(r.t, r.data["value"]) for r in records if r.type == "gpio" and r.data["device"] == "door_entry"]
        self.assertEqual(door(0.002), door(1.0))

    def test_pir_retrigger_and_block(self):
        once = 1e-12   # интенсивность, при которой в интервале остаётся только стимул входа
        motions = [(10, 11, once), (13, 14, once), (20, 21, once)]
        p = PARAMS["pir"] | {"false_per_h": 0}
        rng = random.Random(1)
        self.assertEqual([(e.t, e.level) for e in physics.pir_edges(p, motions, 60, rng)], [(10, True), (18, False)])
        p_l = p | {"retrigger": False}
        self.assertEqual([(e.t, e.level) for e in physics.pir_edges(p_l, motions, 60, rng)],
                         [(10, True), (15, False), (20, True), (25, False)])

    def test_steps(self):
        s = physics.Steps.total([(0, 10, 0.1), (5, 15, 0.2)])
        self.assertEqual([s.at(t) for t in (-1, 0, 5, 10, 15)], [0.0, 0.1, 0.3, 0.2, 0.0])
        s.set(20, 1.0)
        s.forget(25)
        self.assertEqual((s.ts, s.at(30)), ([20], 1.0))
        with self.assertRaises(ValueError):
            s.set(19, 0.0)


class TreeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sysfsim-")
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "root"
        init_root(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_root_is_the_fixture_with_driver_differences(self):
        adc = self.root / "sys/bus/iio/devices/iio:device2"
        self.assertEqual((adc / "name").read_text(), "ads1115\n")
        self.assertFalse((adc / "in_voltage0_offset").exists())
        self.assertEqual((adc / "in_voltage0_scale").read_text(), "0.062500000\n")
        self.assertEqual((self.root / "sys/class/thermal/thermal_zone0/type").read_text(), "cpu-thermal\n")
        fixture = {p.relative_to(FIXTURE) for p in FIXTURE.rglob("*")}
        ours = {p.relative_to(self.root) for p in self.root.rglob("*")}
        self.assertLessEqual(fixture - ours, {Path("sys/devices/platform/soc/1f00074000.i2c/i2c-1/1-0048/iio:device2/"
                                                   "in_voltage0_offset")})
        with self.assertRaises(FileExistsError):
            init_root(self.root)

    def test_attribute_is_replaced_whole_or_unreadable(self):
        path = self.root / "attr"
        long, short = "1" * 4000 + "\n", "2\n"
        atomic_write(path, short)
        seen, stop = set(), threading.Event()

        def reader():
            while not stop.is_set():
                seen.add(path.read_text())
        thread = threading.Thread(target=reader)
        thread.start()
        for i in range(3000):
            atomic_write(path, long if i % 2 else short)
        stop.set()
        thread.join()
        self.assertEqual(seen, {long, short})
        break_attr(path)
        with self.assertRaises(OSError):   # как EIO драйвера: открыть можно, прочитать нельзя
            with open(path, "rb") as f:
                f.read()
        atomic_write(path, short)
        self.assertEqual(path.read_text(), short)

    def test_runner_writes_files_lines_and_truth(self):
        sc = make(events=[{"t": 5, "kind": "door", "device": "door_entry", "duration_s": 20}],
                  faults=[{"t": 10, "kind": "disconnect", "device": "boiler_pipe_ds18b20", "duration_s": 10},
                          {"t": 30, "kind": "read_error", "device": "kitchen_bme280", "duration_s": 5}],
                  questions=[{"id": "q", "t": 40, "text": "?", "expect": {"device": "relay_fan", "property": "state"}}])
        world, gpio = World(sc, 1), FilesGpio(self.root / "dev", "board-sim")
        attr = self.root / "sys/bus/iio/devices/iio:device0/in_temp_input"
        w1 = self.root / "sys/bus/w1/devices/28-0316a2795cff"
        count = self.root / "sys/devices/w1_bus_master1/w1_master_slave_count"
        door = self.root / "dev/board-sim/line22"
        with open(self.tmp / "truth.jsonl", "w") as truth:
            runner = Runner(world, Tree(self.root), gpio, truth)
            runner.start({"scenario": sc.name})
            first = attr.read_text()
            self.assertEqual(door.read_text(), "0\n")
            runner.step(6)
            self.assertEqual(door.read_text(), "1\n")
            self.assertEqual(runner.stats["gpio"], 3)   # начальные уровни двух входов и фронт без дребезга
            runner.step(12)
            self.assertFalse(w1.exists())
            self.assertEqual(count.read_text(), "0\n")
            runner.step(21)
            self.assertTrue((w1 / "temperature").is_file())
            self.assertEqual(count.read_text(), "1\n")
            runner.step(31)
            with self.assertRaises(OSError):
                attr.read_text()
            (self.root / "dev/board-sim/line27").write_text("1\n")   # hw-mcp включил реле в момент 31
            runner.sense()   # виртуальные часы: входы читаются перед шагом
            runner.step(36)
            self.assertTrue(attr.read_text().endswith(".000000000\n"))
            runner.step(41)
            runner.close()
        records = [json.loads(line) for line in (self.tmp / "truth.jsonl").read_text().splitlines()]
        self.assertEqual((records[0]["type"], records[-1]["type"]), ("run_start", "run_end"))
        reading = next(r["reading"] for r in records if r["type"] == "value" and r["device"] == "kitchen_bme280"
                       and r["property"] == "temperature")
        self.assertEqual(round(float(first) * 1e-3, 6), reading)   # журнал истины = то, что прочтёт hw-mcp
        self.assertTrue(all({"type", "t", "mono"} <= r.keys() for r in records))
        actuator = [r for r in records if r["type"] == "actuator"]
        self.assertEqual([(r["t"], r["value"]) for r in actuator], [(0.0, False), (31.0, True)])
        self.assertEqual([r["value"] for r in records if r["type"] == "question"], [True])
        self.assertEqual([(r["t"], r["present"]) for r in records if r["type"] == "device"], [(10, False), (20, True)])
        self.assertIn("max_rss_mib", records[-1])

    def test_real_clock_applies_relay_when_seen(self):
        """Реальные часы: реле, записанное во сне симулятора, действует с пробуждения, на котором замечено."""
        gpio, clock, wakes = FilesGpio(self.root / "dev", "board-sim"), [100.0], []

        def wait(stop: int, seconds: float) -> bool:
            clock[0] += seconds
            wakes.append(clock[0] - 100)
            if len(wakes) == 5:
                (gpio.dir / "line27").write_text("1\n")
            return False
        with open(self.tmp / "truth.jsonl", "w") as truth, mock.patch("bench.sysfsim.runner.wait", wait), \
                mock.patch("bench.sysfsim.runner.time.monotonic", lambda: clock[0]):
            runner = Runner(World(make(), 1), Tree(self.root), gpio, truth)
            runner.start({})
            run_real(runner, 1.0, 10, mock.sentinel.stop)
        records = [json.loads(line) for line in (self.tmp / "truth.jsonl").read_text().splitlines()]
        self.assertEqual([(r["t"], r["value"]) for r in records if r["type"] == "actuator"],
                         [(0.0, False), (round(wakes[4], 6), True)])

    def stop_channel(self) -> tuple[int, int]:
        """Канал остановки (читающий и пишущий концы), как у CLI; закрывается в конце теста."""
        fds = os.pipe()
        for fd in fds:
            self.addCleanup(os.close, fd)
        return fds

    def assertReturns(self, func, *args) -> None:
        """func(*args) возвращается не дольше чем за 5 с (в потоке-демоне: зависший не держит тесты)."""
        thread = threading.Thread(target=func, args=args, daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_stop_channel_ends_the_wait(self):
        """Сигнал остановки (байт в канале) заканчивает ожидание реальных и виртуальных часов до конца прогона.
        Виртуальные ждут его только в select: handle_request без соединения (клиент сбросил его между select и
        accept) возвращается сразу, а не ждёт следующего клиента."""
        stop, signalled = self.stop_channel()
        runner = Runner(World(make(), 1), Tree(self.root), FilesGpio(self.root / "dev", "board-sim"), None)
        runner.start({})
        with ClockServer(("127.0.0.1", 0), runner, 600) as server:
            self.assertReturns(server.handle_request)
            self.assertFalse(wait(stop, 0))
            os.write(signalled, bytes([signal.SIGTERM]))
            self.assertReturns(serve_virtual, server, stop)
            run_real(runner, 60.0, 600, stop)   # без остановки дошёл бы до конца за 10 с
        self.assertLess(runner.world.t, 1)

    def test_silent_clock_client_is_dropped(self):
        """Молчащий клиент часов задерживает остальных не дольше ClockHandler.timeout (10 с; здесь 0,3 с)."""
        runner = Runner(World(make(), 1), Tree(self.root), FilesGpio(self.root / "dev", "board-sim"), None)
        runner.start({})
        with mock.patch.object(ClockHandler, "timeout", 0.3), ClockServer(("127.0.0.1", 0), runner, 2) as server:
            thread = threading.Thread(target=serve_virtual, args=(server, self.stop_channel()[0]))
            thread.start()
            with socket.create_connection(server.server_address, timeout=5) as silent:
                started = time.monotonic()
                self.assertEqual(post(server.url + "/clock", {"t": 1}), (200, {"ok": True, "now": 1.0}))
                self.assertGreaterEqual(time.monotonic() - started, 0.3)
                self.assertEqual(silent.recv(1), b"")   # соединение молчащего клиента закрыто без ответа
            self.assertEqual(post(server.url + "/clock", {"t": 2})[0], 200)   # конец прогона
            thread.join(5)
            self.assertFalse(thread.is_alive())

    def test_cgroup_cpus_are_quota_and_cpuset(self):
        cg = self.tmp / "cgroup"
        cg.mkdir()
        (cg / "cpu.stat").write_text("usage_usec 0\n")
        cases = [({"cpu.max": "max 20000", "cpuset.cpus.effective": "0"}, 1),
                 ({"cpu.max": "40000 20000", "cpuset.cpus.effective": "0-3"}, 2),
                 ({"cpu.max": "400000 100000", "cpuset.cpus.effective": "0-1,3"}, 3),
                 ({"cpuset.cpus.effective": "2"}, 1),   # контроллер cpu выключен: cpu.max нет
                 ({}, os.cpu_count())]
        for files, cpus in cases:
            with self.subTest(files):
                for name in ("cpu.max", "cpuset.cpus.effective"):
                    (cg / name).unlink(missing_ok=True)
                for name, text in files.items():
                    (cg / name).write_text(text + "\n")
                self.assertEqual(CgroupLoad(cg).cpus, cpus)

    def test_cgroup_load_drives_soc_temperature(self):
        cg = self.tmp / "cgroup"
        cg.mkdir()
        (cg / "cpu.max").write_text("50000 100000\n")
        (cg / "cpu.stat").write_text("usage_usec 1000000\nuser_usec 1\n")
        clock = iter([100.0, 100.5, 102.0])
        with mock.patch("bench.sysfsim.runner.time.monotonic", lambda: next(clock)):
            load = CgroupLoad(cg)
            self.assertIsNone(load.sample())   # окно 1 с ещё не прошло
            (cg / "cpu.stat").write_text("usage_usec 1600000\n")
            self.assertEqual(load.sample(), 0.6)   # 0,6 с CPU за 2 с при квоте 0,5 CPU
        world = World(make(), 1, external_load=True)
        world.set_load(1.0)
        play(world, range(0, 301))
        idle = World(make(), 1)
        play(idle, range(0, 301))
        hot, cold = (w.truth("board_cpu_thermal", "temperature1", 300) for w in (world, idle))
        self.assertAlmostEqual(hot - cold, physics.ZONES["board"]["load_rise_c"], delta=0.5)


class GpioSimSetupTest(unittest.TestCase):
    """GpioSim без ядра: configfs и /dev — временные каталоги; права root, ioctl, rmdir (как в configfs) и чтение
    создаваемых ядром имён подменены. Настоящий модуль gpio-sim — test_sysfsim_gpiosim.py (Linux, root)."""

    def test_failure_after_live_removes_the_device(self):
        """Сбой после live=1, когда имя чипа ещё не прочитано: __init__ проверяет линии чипа, имя которого берёт
        из configfs, снимает чип (live=0), убирает устройство и пробрасывает исходный сбой."""
        names = {"chip_name": "gpiochip7\n", "dev_name": "gpio-sim.0\n"}   # их создаёт ядро по live=1
        real_read = Path.read_text
        for error in (OSError("сбой чтения"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory(prefix="sysfsim-") as tmp:
                configfs, dev = Path(tmp, "configfs"), Path(tmp, "dev")
                configfs.mkdir()
                dev.mkdir()
                (dev / "gpiochip7").touch()
                failed, live_at_rmdir = [], []

                def read_text(path: Path, *args, **kw) -> str:
                    if path.name not in names:
                        return real_read(path, *args, **kw)
                    if not failed:   # первое же чтение после live=1
                        failed.append(path)
                        raise error
                    return names[path.name]

                def rmdir(path: Path) -> None:   # в configfs атрибуты уходят вместе с каталогом
                    if (path / "live").exists():
                        live_at_rmdir.append(real_read(path / "live"))
                    for attr in path.iterdir():
                        if attr.is_file():
                            attr.unlink()
                    os.rmdir(path)
                with mock.patch.multiple("bench.sysfsim.gpio", CONFIGFS=configfs, DEV=dev), \
                        mock.patch("os.geteuid", return_value=0), mock.patch("fcntl.ioctl") as ioctl, \
                        mock.patch.object(Path, "read_text", read_text), mock.patch.object(Path, "rmdir", rmdir), \
                        self.assertRaises(type(error)) as raised:
                    GpioSim("board-sim", {17: "pir_hall", 22: "door_entry", 27: "relay_fan"})
                self.assertIs(raised.exception, error)
                self.assertEqual(ioctl.call_count, 28)   # все линии банка 0…27 чипа gpiochip7
                self.assertEqual(ioctl.call_args.args[0].name, str(dev / "gpiochip7"))
                self.assertEqual(live_at_rmdir, ["0"])
                self.assertEqual(list(configfs.iterdir()), [])


def cli(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "bench.sysfsim", *args], cwd=ROOT, env=ENV, capture_output=True,
                          text=True, timeout=60, **kw)


def post(url: str, body) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class CliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="sysfsim-cli-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.assertEqual(cli("init", "--root", str(self.tmp / "root")).returncode, 0)

    def launch(self, *args: str, stall: str = "", prefix: tuple[str, ...] = ()) -> subprocess.Popen:
        """run в фоне — с заминкой stall (см. STALL), под prefix (nohup); процесс убирается в конце теста."""
        cmd = [sys.executable, "-c", STALL, stall] if stall else [sys.executable, "-m", "bench.sysfsim"]
        proc = subprocess.Popen([*prefix, *cmd, "run", "--root", str(self.tmp / "root"), *args], cwd=ROOT, env=ENV,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def stop():
            if proc.poll() is None:
                proc.kill()
            proc.communicate()
        self.addCleanup(stop)
        return proc

    def start_virtual(self, *args: str, **kw) -> tuple[subprocess.Popen, dict]:
        """run --clock virtual в фоне (launch); вернуть процесс и ready-file."""
        ready = self.tmp / "ready.json"
        ready.unlink(missing_ok=True)
        proc = self.launch("--clock", "virtual", "--ready-file", str(ready), *args, **kw)
        deadline = time.monotonic() + 15
        while not ready.exists():
            self.assertIsNone(proc.poll())
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        return proc, json.loads(ready.read_text())

    def test_list_check_and_errors(self):
        self.assertIn("dense\t900 с", cli("list-scenarios").stdout)
        checked = cli("check", "--scenario", "dense")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(json.loads(checked.stdout)["situations"]["s_overheat"], 471.0)
        again = cli("init", "--root", str(self.tmp / "root"))
        self.assertEqual((again.returncode, again.stderr.strip()),
                         (1, f"sysfsim: {self.tmp / 'root'}: каталог не пуст"))
        missing = cli("run", "--root", str(self.tmp / "none"), "--duration", "1")
        self.assertIn("сначала python -m bench.sysfsim init", missing.stderr)
        self.assertIn("ожидается число", cli("check", "--scenario", str(self._bad_scenario())).stderr)
        self.assertNotEqual(cli("run", "--root", "x", "--clock", "virtual", "--speed", "2").returncode, 0)

    def test_failed_start_leaves_no_journal(self):
        truth = self.tmp / "truth.jsonl"
        failures = [("--load-cgroup", str(self.tmp / "no-cgroup"))]
        if not CONFIGFS.is_dir():   # без модуля gpio-sim чип не создать
            failures.append(("--gpio", "gpiosim"))
        for args in failures:
            done = cli("run", "--root", str(self.tmp / "root"), "--truth", str(truth), *args)
            self.assertEqual(done.returncode, 1, done.stderr)
            self.assertFalse(truth.exists(), args)

    def _bad_scenario(self) -> Path:
        path = self.tmp / "bad.json"
        path.write_text(json.dumps({"format": "sysfsim/1", "name": "bad", "duration_s": "long", "start": "12:00"}))
        return path

    def test_virtual_clock_over_http(self):
        truth, declared = self.tmp / "truth.jsonl", self.tmp / "declared.json"
        proc, info = self.start_virtual("--truth", str(truth), "--gpio-label", "lab-b", "--declared-out", str(declared))
        self.assertEqual(info["gpio"], {"backend": "files", "label": "lab-b", "chip": "lab-b"})
        url = info["url"]
        for t in range(1, 31):
            self.assertEqual(post(url + "/clock", {"t": t}), (200, {"ok": True, "now": float(t)}))
        self.assertEqual(post(url + "/clock", {"t": 5})[0], 409)
        self.assertEqual(post(url + "/clock", {"time": 5})[0], 400)
        with urllib.request.urlopen(url + "/healthz", timeout=5) as res:
            self.assertEqual(json.loads(res.read())["now"], 30.0)
        expected = json.loads(DECLARED.read_text())   # у всех линий платы — метка созданного чипа, остальное то же
        for line in expected["gpio"]:
            line["chip"] = "lab-b"
        self.assertEqual(json.loads(declared.read_text()), expected)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(15), 0, proc.stderr.read())
        records = [json.loads(line) for line in truth.read_text().splitlines()]
        self.assertEqual(records[-1]["type"], "run_end")
        self.assertEqual(records[-1]["t"], 30.0)
        self.assertEqual(cli("run", "--root", str(self.tmp / "root"), "--truth", str(truth)).returncode, 1)

    def test_virtual_clock_outlives_bad_clients(self):
        """Обрыв соединения клиентом, Content-Length вне 0…65536 и момент за концом прогона не останавливают часы."""
        proc, info = self.start_virtual("--duration", "60")
        url = info["url"]
        address = url.removeprefix("http://").rsplit(":", 1)
        address = (address[0], int(address[1]))
        for t in (1, 2, 3):   # запрос и сброс соединения (RST), не дожидаясь ответа; часы отвечают дальше
            with socket.create_connection(address) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                s.sendall(b'POST /clock HTTP/1.1\r\nHost: sim\r\nContent-Length: 8\r\n\r\n{"t": %d}' % t)
            with urllib.request.urlopen(url + "/healthz", timeout=5) as res:
                self.assertEqual(res.status, 200)
        for length in (b"-1", b"65537"):
            with socket.create_connection(address, timeout=5) as s:
                s.sendall(b"POST /clock HTTP/1.1\r\nHost: sim\r\nContent-Length: %s\r\n\r\n" % length)
                self.assertIn(b" 400 ", s.recv(4096))
        self.assertEqual(post(url + "/clock", {"t": 61})[0], 409)
        self.assertEqual(post(url + "/clock", {"t": 4}), (200, {"ok": True, "now": 4.0}))
        self.assertIsNone(proc.poll())

    def test_virtual_clock_failure_stops_the_run(self):
        proc, info = self.start_virtual()
        (self.tmp / "root/dev/board-sim/line27").write_text("x\n")   # испорченный выход реле
        self.assertEqual(post(info["url"] + "/clock", {"t": 1})[0], 500)
        self.assertEqual(proc.wait(15), 1)
        self.assertIn("уровень 'x'", proc.stderr.read())

    def test_stop_signals(self):
        """SIGHUP и SIGINT останавливают прогон, как SIGTERM: код 0 и run_end; под nohup SIGHUP игнорируется."""
        for sig in (signal.SIGHUP, signal.SIGINT):
            truth = self.tmp / f"truth-{sig.name}.jsonl"
            proc, _ = self.start_virtual("--truth", str(truth))
            proc.send_signal(sig)
            self.assertEqual(proc.wait(15), 0, proc.stderr.read())
            self.assertEqual(json.loads(truth.read_text().splitlines()[-1])["type"], "run_end")
        proc, info = self.start_virtual(prefix=("nohup",))
        proc.send_signal(signal.SIGHUP)
        self.assertEqual(post(info["url"] + "/clock", {"t": 1}), (200, {"ok": True, "now": 1.0}))
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(15), 0)

    def test_signal_at_startup_and_during_cleanup(self):
        """Сигнал ничего не прерывает. Пришедший при запуске останавливает прогон сразу после запуска: уборка,
        run_end и код 0 без трассировки. Уборку (здесь — close линий) не обрывают ни повторные сигналы, ни сигнал
        после сбоя, и код 1 сбоя остаётся."""
        truth = self.tmp / "truth.jsonl"
        proc = self.launch("--truth", str(truth), "--declared-out", str(self.tmp / "declared.json"),
                           stall="resolve_declared")
        self.assertEqual(proc.stdout.readline(), "stall\n")
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(15), 0)
        self.assertEqual((proc.stdout.read(), proc.stderr.read()), ("done\n", ""))
        end = json.loads(truth.read_text().splitlines()[-1])
        self.assertEqual(end["type"], "run_end")
        self.assertLess(end["t"], 1)   # сценарий — 900 с: остановлен на первом же ожидании

        proc, _ = self.start_virtual(stall="FilesGpio.close")
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.stdout.readline(), "stall\n")   # уборка началась
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            proc.send_signal(sig)
        self.assertEqual(proc.wait(15), 0)
        self.assertEqual(proc.stdout.read(), "done\n")
        self.assertRegex(proc.stderr.read(), r"\Asysfsim: виртуальные часы — POST http://\S+/clock\n\Z")

        proc, info = self.start_virtual(stall="FilesGpio.close")
        (self.tmp / "root/dev/board-sim/line27").write_text("x\n")
        self.assertEqual(post(info["url"] + "/clock", {"t": 1})[0], 500)
        self.assertEqual(proc.stdout.readline(), "stall\n")
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(15), 1)
        self.assertEqual(proc.stdout.read(), "done\n")
        self.assertIn("уровень 'x'", proc.stderr.read())

    def test_real_clock_sleeps_between_deadlines(self):
        truth = self.tmp / "truth.jsonl"
        started = time.monotonic()
        done = cli("run", "--root", str(self.tmp / "root"), "--speed", "40", "--duration", "40", "--truth", str(truth))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertGreaterEqual(time.monotonic() - started, 1.0)
        end = json.loads(truth.read_text().splitlines()[-1])
        # сроки мира: DS18B20 0,75 с, BME280/ADS1115/SoC 1 с, DHT11 2 с, снимки 10 с — около 2 пробуждений на с
        self.assertLess(end["stats"]["steps"], 4 * 40)
        self.assertEqual(end["t"], 40.0)


if __name__ == "__main__":
    unittest.main()
