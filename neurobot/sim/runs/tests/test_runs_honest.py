"""Честный счёт 20.10 (REVIEW H13–H15) на синтетике: рестарты по журналу супервизора (systemd и сторож, пауза ВМ,
ручные рестарты не в счёт), видимость перезапуска по PLAN §1 — окно жеста до t0 + 3 с, оборванный манёвр (сам и
защитой по датчику отказа), «поздно», остановка защитой по препятствию не в счёт, прежний счёт рядом; TTD, TTR после
снятия причины, недоступность perception по probe, первый обслуженный запрос; рестарты вне расписания; SLI в окнах;
память по жизням процессов (служебные дочерние не в счёт), fd; сэмплер fd; ячейки и вердикты сводки (граница нулей,
p95 — от 59 замеров)."""
from __future__ import annotations

import datetime
import json
import math
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from neurobot.sim.runs import compare, fds, metrics as M  # noqa: E402

T0 = 1000.0     # t_mono якоря
MSK = datetime.timezone(datetime.timedelta(hours=3))


def seg(i, t_start, t_leave, kind="gesture"):
    return {"seg": i, "kind": kind, "t_start": t_start, "t0": t_leave - 0.5, "t_leave": t_leave}


def stamp(wall):
    return datetime.datetime.fromtimestamp(wall, MSK).isoformat(timespec="microseconds")


def description(unit):
    """Description= юнита пакета (neurobot/deploy/units): его пишет «Started» systemd."""
    with open(os.path.join(M.UNITS_DIR, unit + ".service"), encoding="utf-8") as f:
        return next(x.split("=", 1)[1].strip() for x in f if x.startswith("Description="))


PERCEPTION, SAFETY = description("neurobot-perception"), description("neurobot-safety")
LIDAR = description("neurobot-driver@").replace("%i", "lidar")
STARTED = re.compile(r"^(?:Started|Stopping) (neurobot-\S+?)\.service - (.*)$")


def v245(msg):
    """Текст systemd 245 (Ubuntu 20.04): «Started Описание.» без имени юнита, «Succeeded.» вместо «Deactivated
    successfully.»."""
    m = STARTED.match(msg)
    if m and msg.startswith("Started "):
        return "Started " + m.group(2)
    return msg.replace("Deactivated successfully.", "Succeeded.")


def entry(wall, src, msg, old):
    """Запись journalctl -o json: от systemd — _PID 1, UNIT, у «Started» — MESSAGE_ID, момент записи — в
    _SOURCE_REALTIME_TIMESTAMP (journald принял на 3 мс позже); текст — 255 или 245 (old)."""
    ident, pid = src.rstrip("]").split("[")
    e = {"__REALTIME_TIMESTAMP": str(round(wall * 1e6)), "_PID": pid, "SYSLOG_IDENTIFIER": ident,
         "MESSAGE": v245(msg) if old else msg}
    if pid == "1":
        e["_SOURCE_REALTIME_TIMESTAMP"] = e["__REALTIME_TIMESTAMP"]
        e["__REALTIME_TIMESTAMP"] = str(round((wall + 0.003) * 1e6))
        m = STARTED.match(msg) or re.match(r"^(neurobot-\S+?\.service): ", msg)
        e["UNIT"] = m.group(1) + ("" if m.group(1).endswith(".service") else ".service")
        if msg.startswith("Started "):
            e.update(MESSAGE_ID=M.STARTED_ID, JOB_TYPE="start", JOB_RESULT="done")
    elif "rosbridge_down" in msg:
        e["MESSAGE"] = list(msg.encode("utf-8"))           # не-UTF-8 журнал отдаёт MESSAGE массивом байтов
    return e


class JournalTest(unittest.TestCase):
    """Стенные часы журнала → секунды от якоря по смещению t − m ближайшего события (пауза ВМ его меняет). Один и тот
    же журнал в четырёх видах: текст systemd 255 (ВМ) и 245 (Ubuntu 20.04 робота: «+0300», «Started Описание.»,
    «Succeeded.»), journalctl -o json обеих версий."""

    RUN = {"anchor": {"t_mono": 510.0},
           "events": [{"kind": "start", "t": 1000.0, "m": 500.0}, {"kind": "fault", "t": 1100.0, "m": 600.0},
                      {"kind": "agent", "t": 2000.0, "m": 700.0}, {"kind": "end", "t": 2100.0, "m": 800.0}]}
    LINES = [(990.0, "systemd[1]", "neurobot-driver@lidar.service: Deactivated successfully."),   # до прогона
             (1100.05, "systemd[1]", "neurobot-perception.service: Main process exited, code=killed, status=9/KILL"),
             (1101.0, "systemd[1]", "neurobot-perception.service: Scheduled restart job, restart counter is at 1."),
             (1103.5, "systemd[1]", "Started neurobot-perception.service - %s." % PERCEPTION),
             (1150.0, "neurobot[77]", "neurobot-watchdog: driver@lidar: sensor_stale: restart "
                                      "neurobot-driver@lidar.service: темы молчат 2 проб подряд: /scan (3.1 с)"),
             (1150.3, "systemd[1]", "neurobot-driver@lidar.service: Deactivated successfully."),
             (1151.0, "systemd[1]", "Started neurobot-driver@lidar.service - %s." % LIDAR),
             (1200.0, "systemd[1]", "neurobot-perception.service: Watchdog timeout (limit 5s)!"),
             (1200.1, "systemd[1]", "neurobot-perception.service: Main process exited, code=dumped, status=6/ABRT"),
             (1201.0, "systemd[1]", "neurobot-perception.service: Scheduled restart job, restart counter is at 2."),
             (1204.0, "systemd[1]", "Started neurobot-perception.service - %s." % PERCEPTION),
             # перенос робота оператором: стоп и старт safety_gate — не автоматический рестарт
             (1250.0, "systemd[1]", "Stopping neurobot-safety.service - %s..." % SAFETY),
             (1250.5, "systemd[1]", "neurobot-safety.service: Deactivated successfully."),
             (1251.0, "systemd[1]", "Started neurobot-safety.service - %s." % SAFETY),
             (1260.0, "systemd[1]", "neurobot-mcp.service: Main process exited, code=killed, status=9/KILL"),
             (1261.0, "neurobot-run[5]", "neurobot-executor.service: Scheduled restart job, restart counter is at 9."),
             (2050.0, "neurobot[77]", "neurobot-watchdog: rosbridge: rosbridge_down: restart neurobot-rosbridge"
                                      ".service: WebSocket не ответил 2 раза подряд")]
    WANT = [("neurobot-perception", "systemd", "killed", 90.05, 90.05, 93.5),
            ("neurobot-driver@lidar", "watchdog", "sensor_stale", 136.9, 140.0, 141.0),
            ("neurobot-perception", "systemd", "watchdog", 185.0, 190.0, 194.0),   # зависание: минус WatchdogSec
            ("neurobot-rosbridge", "watchdog", "rosbridge_down", 240.0, 240.0, None),   # после паузы ВМ 900 с
            ("neurobot-mcp", "none", "killed", 250.0, 250.0, None)]                    # умер и не поднят

    def write(self, d, kind):
        """kind: text255, text245, json255, json245."""
        path = os.path.join(d, "journal." + kind)
        old = kind.endswith("245")
        with open(path, "w", encoding="utf-8") as f:
            for wall, src, msg in self.LINES:
                if kind.startswith("json"):
                    f.write(json.dumps(entry(wall, src, msg, old), ensure_ascii=False) + "\n")
                else:
                    ts = stamp(wall)
                    f.write("%s lima-sim %s: %s\n" % (ts[:-3] + ts[-2:] if old else ts, src, v245(msg) if old else msg))
        return path

    def test_restarts_by_systemd_and_watchdog(self):
        for kind in ("text255", "text245", "json255", "json245"):
            with self.subTest(kind), tempfile.TemporaryDirectory() as d:
                got = M.journal_restarts(self.write(d, kind), self.RUN)
                self.assertEqual([(r["unit"], r["by"], r["cause"], r["start_s"], r["detect_s"], r["up_s"])
                                  for r in got], self.WANT)
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(M.journal_restarts(os.path.join(d, "nope"), self.RUN))
        self.assertEqual(M.unit_roles("neurobot-perception.service"), ("camera", "perception"))
        self.assertEqual(M.unit_roles("neurobot-watchdog"), ("watchdog",))

    def test_245_text_details(self):
        # смещение пояса без двоеточия (fromisoformat Python 3.8 его не берёт), «Started» по Description= шаблона
        self.assertEqual(M._wall("2026-10-19T01:18:56.630103+0300"), M._wall("2026-10-19T01:18:56.630103+03:00"))
        self.assertEqual(M._wall("2026-10-19T01:18:56+0300"), datetime.datetime(2026, 10, 19, 1, 18, 56,
                                                                                  tzinfo=MSK).timestamp())
        self.assertIsNone(M._wall("-- Boot 3f1c… --"))
        self.assertEqual(M._started_by_description("Started %s." % LIDAR), "neurobot-driver@lidar")
        self.assertEqual(M._started_by_description("Started %s." % SAFETY), "neurobot-safety")
        self.assertIsNone(M._started_by_description("Started Session 3 of user lab."))


def run_doc(faults, ops=(), agent=(), end=400.0):
    ev = [dict(f, kind="fault") for f in faults] + [dict(o, kind="operator") for o in ops]
    ev.append({"kind": "end", "m": T0 + end})
    return {"anchor": {"t_mono": T0}, "events": sorted(ev, key=lambda e: e["m"]), "agent": list(agent)}


class HonestFaultsTest(unittest.TestCase):
    """Видимость по PLAN §1 честно — и прежний счёт рядом."""

    SEGS = [seg(0, 10, 15), seg(1, 40, 45), seg(2, 70, 75), seg(3, 100, 105), seg(4, 130, 135), seg(5, 148, 152),
            seg(6, 200, 205)]
    PER = {0: {"reaction": "interrupted", "lat": {"cmd": 0.6}, "phases": [["F", 1.5]]},       # оборван на 16,6 с
           1: {"reaction": "wrong", "maneuver": "partial:think", "lat": {"cmd": 0.5}, "phases": [["R", 1.0]]},
           2: {"reaction": "interrupted", "lat": {"cmd": 0.5}, "phases": [["F", 1.0]]},       # оборвало препятствие
           3: {"reaction": "none", "lat": {}},                                                  # t0 104,5 — до отказа
           4: {"reaction": "correct", "late": True, "lat": {"cmd": 3.4}, "phases": [["F", 3.0]]},
           5: {"reaction": "none", "lat": {}},
           6: {"reaction": "correct", "lat": {"cmd": 0.6}, "phases": [["F", 3.0]]}}
    SAFETY = [{"m": T0 + 45.8, "state": "blocked", "reasons": ["stale_scan: /scan — 0.41 с без данных"]},
              {"m": T0 + 46.5, "state": "slow", "reasons": []},
              {"m": T0 + 75.9, "state": "blocked", "reasons": ["obstacle: глубина…"]},
              {"m": T0 + 76.5, "state": "slow", "reasons": []}]
    FAULTS = [{"fault": "kill_executor", "m": T0 + 16.0, "ttr_s": 3.0},
              {"fault": "kill_lidar", "m": T0 + 45.5, "ttr_s": 1.7},
              {"fault": "camera_freeze", "m": T0 + 75.5, "ttr_s": 7.5, "duration_s": 6.0},
              {"fault": "stop_perception", "m": T0 + 106.0, "ttr_s": 9.8, "duration_s": 8.0},
              {"fault": "kill_perception", "m": T0 + 133.0, "ttr_s": 3.0}]
    RESTARTS = [{"unit": "neurobot-executor", "by": "systemd", "cause": "killed", "detect_s": 16.02, "start_s": 16.02,
                 "action_s": 16.5, "up_s": 18.9},
                {"unit": "neurobot-perception", "by": "systemd", "cause": "watchdog", "detect_s": 110.9,
                 "start_s": 105.9, "action_s": 112.0, "up_s": 114.0},
                {"unit": "neurobot-perception", "by": "systemd", "cause": "killed", "detect_s": 150.0,
                 "start_s": 150.0, "action_s": 151.0, "up_s": 154.0},
                {"unit": "neurobot-mcp", "by": "systemd", "cause": "killed", "detect_s": 300.0, "start_s": 300.0,
                 "action_s": 301.0, "up_s": 302.0}]

    def rows(self):
        run = run_doc(self.FAULTS)
        stopped = {"by_seg": {"2": {"reaction": "interrupted", "reasons": ["obstacle"]}}}
        faults = M.fault_rows(run, self.SEGS, self.PER, deaf=[], excluded={2}, safety=self.SAFETY, busy=[],
                              stopped=stopped, restarts=self.RESTARTS)
        extra = M.unscheduled_rows(run, self.SEGS, self.PER, [], self.RESTARTS, faults, self.SAFETY, (), [], stopped)
        return run, faults, extra

    def test_visibility_reasons_and_legacy(self):
        _, f, _ = self.rows()
        got = [(r["fault"], r["visible"], r["codes"], r["visible_legacy"]) for r in f]
        self.assertEqual(got, [
            ("kill_executor", True, ["cut"], False),          # манёвр прошлого жеста оборван — прежде не считался
            ("kill_lidar", True, ["cut_safety"], False),      # оборвала защита по stale_scan — следствие отказа
            ("camera_freeze", False, [], False),              # оборвала защита по препятствию — не отказ
            ("stop_perception", True, ["lost"], False),       # t0 + 3 с задевает окно: жест 3 потерян
            ("kill_perception", True, ["late"], False)])      # реакция позже 3 с — в счёт
        self.assertEqual((f[0]["cut"], f[1]["cut_safety"], f[3]["lost_gestures"], f[4]["lost_gestures"]),
                         ([0], [1], [3], [4]))

    def test_times_ttd_cause_removal_and_first_served(self):
        _, f, _ = self.rows()
        ex, stop = f[0], f[3]
        self.assertEqual((ex["unavailable_s"], ex["ttd_s"], ex["detected_by"], ex["ttr_after_s"]),
                         (3.0, 0.02, "systemd:killed", 3.0))
        # SIGSTOP 8 с: systemd убил процесс по WatchdogSec на 4,9 с — причина снята тогда, а не в конце удержания
        self.assertEqual((stop["ttd_s"], stop["ttr_after_s"]), (4.9, 4.9))
        self.assertEqual(f[2]["ttr_after_s"], 1.5)            # камера: от конца удержания 6 с
        self.assertIsNone(f[2]["ttd_s"])                      # отказ вне процесса — супервизор не действовал
        self.assertEqual(ex["first_served_s"], 29.0)          # жест 1 (начат после отказа): t0 44,5 + 0,5 − 16
        self.assertEqual([r["unit"] for r in ex["restarts"]], ["neurobot-executor"])

    def test_unscheduled_restarts_and_summary(self):
        _, f, extra = self.rows()
        self.assertEqual([(r["fault"], r["visible"], r["codes"]) for r in extra],
                         [("restart:perception", True, ["lost"]), ("restart:mcp", False, [])])
        self.assertEqual((extra[0]["ttd_s"], extra[0]["unavailable_s"]), (0.0, 4.0))
        h = M.k3_honest(f, extra, self.PER, self.SEGS)
        self.assertEqual((h["faults"], h["visible"], h["visible_legacy"]), (5, 4, 0))
        self.assertEqual(h["by_reason"], {"cut": 1, "cut_safety": 1, "lost": 2, "late": 1})
        self.assertEqual(h["unscheduled"]["n"], 2)
        self.assertEqual(h["bound"]["ci"], [0.2836, 0.9949])
        # отказ пришёлся на манёвр: исполнитель, лидар и камера — во время манёвров жестов 0, 1 и 2
        self.assertEqual(h["on_maneuver"], 3)
        self.assertEqual(h["sli_correct_3s"]["outside"]["k"], 1)                  # жест 6 — вне окон, верно
        self.assertIsNone(M.k3_honest(f, None, self.PER, self.SEGS)["unscheduled"])
        k5 = M.k5_summary(f)
        self.assertEqual((k5["kill_executor"]["n"], k5["kill_executor"]["ttd_s"]["p50"]), (1, 0.02))

    def test_old_call_keeps_old_exclusions(self):
        run = run_doc(self.FAULTS[:1])
        rows = M.fault_rows(run, self.SEGS, self.PER, deaf=[], excluded={0})
        self.assertEqual((rows[0]["visible_legacy"], rows[0]["unavailable_s"], rows[0]["ttd_s"]), (False, 3.0, None))

    def test_perception_recovery_by_probe_not_masked_by_hold(self):
        # SIGSTOP perception 8 с: runner считал TTR от конца удержания (9,8 с); новый процесс пошёл уже на 7-й секунде
        probe = [{"m": T0 + t, "pid": 1, "det_calls": 10 * t} for t in range(100, 107)]
        probe += [{"m": T0 + t, "pid": 2, "det_calls": 10 * (t - 112)} for t in range(112, 126)]
        run = run_doc([{"fault": "stop_perception", "m": T0 + 106.0, "ttr_s": 9.8, "duration_s": 8.0},
                       {"fault": "camera_delay", "m": T0 + 112.0, "ttr_s": 9.0, "duration_s": 7.0}])
        rows = M.fault_rows(run, [], {}, [], probe=probe)
        # отказ камеры — после конца удержания: две строки probe с приростом (119 → 121 с)
        self.assertEqual((rows[0]["unavailable_s"], rows[1]["unavailable_s"]), (7.0, 9.0))
        self.assertIsNone(M.progress_after(probe, T0 + 124.0))


def pss_sample(t, procs):
    return {"type": "sample", "t_mono": t, "units": {name: {"procs": [
        {"pid": pid, "start": pid * 100, "pss_kib": 1024 * v} for pid, v in ps]} for name, ps in procs.items()}}


class MemoryTest(unittest.TestCase):
    def test_restart_is_a_segment_and_children_are_not_memory(self):
        samples = []
        for k in range(800):                       # 2 ч 13 мин, шаг 10 с; рестарт на половине — уровень выше на 40
            pid, base = (1, 100.0) if k < 400 else (2, 140.0)
            procs = [(pid, base + 0.03 * ((k * 7) % 5))]
            if k in (100, 500):                    # journalctl сторожа — один сэмпл
                procs.append((99 + k, 50.0))
            samples.append(pss_sample(10.0 * k, {"a": procs, "twin_driver": [(9, 500.0)]}))
        fd_samples = [{"type": "sample", "t_mono": 10.0 * k, "units": {"a": {"procs": [
            {"pid": 1 if k < 400 else 2, "start": 100 if k < 400 else 200, "fds": 20 + k // 200}]}}}
            for k in range(800)]
        mem = M.memory(samples, fds=fd_samples)
        a, total = mem["a"], mem["_total"]
        self.assertGreater(a["slope_mib_h"], 10.0)                      # МНК видит ступень рестарта как «утечку»
        self.assertEqual((a["sen"]["segments"], a["sen"]["segments_used"]), (2, 2))
        self.assertLess(abs(a["sen"]["slope"]), 0.2)
        self.assertEqual((total["sen"]["decision"], total["sen"]["delta"]), ("equivalent", round(M.DELTA_MIB_H, 3)))
        self.assertNotIn("delta", a["sen"])                             # TOST — только у суммы робота
        self.assertEqual((a["fd"]["start"], a["fd"]["end"], a["fd"]["max"]), (20, 23, 23))
        self.assertNotIn("fd", mem["twin_driver"])
        # сумма робота — сумма наклонов компонентов, ДИ — сумма границ (Бонферрони): рестарт одного не рвёт другие
        part = {"slope": 0.1, "ci90": [-0.2, 0.4], "lag1_ok": True, "warmup_at_limit": 0, "hours": 1.0}
        both = M.sum_trend([part, dict(part, slope=0.3, ci90=[0.1, 0.5])], 1.0)
        self.assertEqual((both["slope"], both["ci90"], both["decision"]), (0.4, [-0.1, 0.9], "equivalent"))
        self.assertEqual(M.sum_trend([part, dict(part, warmup_at_limit=1)], 1.0)["decision"], "undetermined")
        self.assertEqual(M.sum_trend([part, {"decision": "short"}], 1.0)["decision"], "short")
        self.assertIsNone(M.sum_trend([part, None], 1.0))
        t, y, s = M.unit_series(samples, ["a"], lambda p: p["pss_kib"] / M.MIB)
        self.assertEqual((len(y), max(s)), (800, 1))
        self.assertEqual(M.trend([0.0, 10.0], [1.0, 1.0], [0, 1])["decision"], "short")

    def test_undetermined_whatever_the_ci(self):
        # PROTOCOL §1, K8: lag-1 ≥ 0,2 или прогрев на пределе — «не определено», даже если весь ДИ за δ (это справочно)
        part = {"slope": 0.1, "ci90": [-0.2, 0.4], "lag1_ok": True, "warmup_at_limit": 0, "hours": 1.0}
        far = dict(part, slope=3.0, ci90=[2.0, 4.0])
        self.assertEqual(M.sum_trend([far, part], 1.0)["decision"], "not_equivalent")
        for bad in ({"warmup_at_limit": 3}, {"lag1_ok": False}):
            got = M.sum_trend([far, dict(part, **bad)], 1.0)
            self.assertEqual((got["decision"], got["beyond"]), ("undetermined", True))
        self.assertEqual(M.sum_trend([part, dict(part, lag1_ok=False)], 1.0)["decision"], "undetermined")
        # прогрев 40 мин в часовом ряду: MSER режет не больше половины, остаток прогрева тянет наклон выше δ
        t = [10.0 * k for k in range(360)]
        y = [100.0 + 30.0 * (1.0 - math.exp(-x / 2400.0)) + 0.02 * ((k * 7) % 5) for k, x in enumerate(t)]
        got = M.trend(t, y, [0] * len(t), delta=1.0)
        self.assertEqual((got["warmup_at_limit"], got["beyond"], got["decision"]), (1, True, "undetermined"))

    def test_children_piling_up_without_restart_are_growth(self):
        # 8 ч, шаг 10 с: главный процесс ровный, раз в час — новый устойчивый дочерний по 5 МиБ, никто не умирает:
        # +5 МиБ/ч. Прежде каждая ступень «+процесс» рвала сегмент, ряд внутри сегментов ровный — TOST «тренда нет»
        samples = [pss_sample(10.0 * k, {"a": [(1, 100.0 + 0.03 * ((k * 7) % 5))] +
                                          [(10 + j, 5.0) for j in range(k // 360)]}) for k in range(2880)]
        _, _, seg = M.unit_series(samples, ["a"], lambda p: p["pss_kib"] / M.MIB)
        self.assertEqual(max(seg), 0)
        sen = M.memory(samples)["_total"]["sen"]
        self.assertNotEqual(sen["decision"], "equivalent")
        self.assertGreater(sen["ci90"][0], M.DELTA_MIB_H)
        # исчезновение устойчивого процесса (рестарт — омоложение) по-прежнему начинает сегмент
        samples[1500]["units"]["a"]["procs"] = [p for p in samples[1500]["units"]["a"]["procs"] if p["pid"] != 1]
        for s in samples[1501:]:
            s["units"]["a"]["procs"][0].update(pid=2, start=200)
        self.assertEqual(max(M.unit_series(samples, ["a"], lambda p: p["pss_kib"] / M.MIB)[2]), 1)


class FdsTest(unittest.TestCase):
    def test_counts_fds_of_cgroup_processes_and_their_lives(self):
        with tempfile.TemporaryDirectory() as d:
            cg, proc = os.path.join(d, "cg"), os.path.join(d, "proc")
            os.makedirs(os.path.join(cg, "child"))
            with open(os.path.join(cg, "cgroup.procs"), "w") as f:
                f.write("11\n12\n")
            with open(os.path.join(cg, "child", "cgroup.procs"), "w") as f:
                f.write("13\n")
            stat = "11 (py (x) thon) S " + " ".join(str(i) for i in range(1, 19)) + " 777 20\n"
            os.makedirs(os.path.join(proc, "11", "fd"))
            for n in range(3):
                open(os.path.join(proc, "11", "fd", str(n)), "w").close()
            with open(os.path.join(proc, "11", "stat"), "w") as f:
                f.write(stat)
            os.makedirs(os.path.join(proc, "12"))                       # fd не видны (не root) — пропуск
            with open(os.path.join(proc, "12", "stat"), "w") as f:
                f.write(stat.replace("11 ", "12 ", 1))
            got = fds.sample({"units": {"x": {"cgroup": cg}}}, proc_root=proc)
            self.assertEqual(got["units"], {"x": {"procs": [{"pid": 11, "start": 777, "fds": 3}]}})
            self.assertEqual(fds.pids(cg), [11, 12, 13])
            out = os.path.join(d, "fds.jsonl")
            units = os.path.join(d, "units.json")
            with open(units, "w") as f:
                json.dump({"units": {}}, f)
            self.assertEqual(fds.main(["--units", units, "--out", out, "--interval", "0.5", "--duration", "0.1"]), 0)
            kinds = [json.loads(line)["type"] for line in open(out)]
            self.assertEqual((kinds[0], set(kinds[1:])), ("header", {"sample"}))


def k6_series(zero):
    return {"mcp_stop": {"runs": [{"req_zero_s": 0.002, "brake_s": 0.002, "zero_s": z} for z in zero]}}


class CompareHonestTest(unittest.TestCase):
    def test_cells(self):
        self.assertEqual(compare.lat_ci({"n": 63, "ci95": {"p50": [0.62, 0.7], "p95": [0.87, None]}}),
                         "p50 [0,62; 0,70]; p95 [0,87; —]")
        self.assertEqual(compare.bound(M.zero_bound(0, 31)), "(≤ 9,2 %)")
        self.assertEqual(compare.bound(M.zero_bound(11, 24)), "[26 %; 67 %]")
        self.assertEqual(compare.zero_of(0, 80, " жестов"), "0 (≤ 3,7 % из 80 жестов)")
        self.assertEqual(compare.zero_rate(0, 2.14), "0 (≤ 1,4 в час за 2,14 ч)")
        m = {"K3": {"honest": {"faults": 24, "visible": 11, "visible_legacy": 2, "bound": M.zero_bound(11, 24),
                               "by_reason": {"cut_safety": 7, "latch": 2}, "on_maneuver": 17,
                               "unscheduled": {"n": 1, "visible": 0}}}}
        self.assertEqual(compare.visible_cell(m), "11 из 24 [26 %; 67 %]; вне расписания 0 из 1; до 20.10 — 2")
        self.assertEqual(compare.reasons_cell(m), "оборван защитой по датчику отказа 7, защёлка снята человеком 2")
        sen = {"components": 7, "hours": 0.57, "slope": 5.5, "ci90": [1.8, 30.2], "decision": "not_equivalent",
               "beyond": True, "delta": 1.089}
        self.assertEqual(compare.memory_honest({"K8": {"memory": {"_total": {"sen": sen}}}}),
                         "5,5 [1,8; 30,2] МиБ/ч — растёт быстрее δ (δ 1,09; 0,57 ч; сумма 7 компонентов, у каждого — "
                         "по жизням процессов)")
        self.assertEqual(compare.decision(dict(sen, beyond=False)), "эквивалентность не показана")
        # A0 16.10: весь ДИ выше δ, но прогрев трёх сегментов не отделён — «не определено», рост — справочно
        a0 = dict(sen, slope=12.0, ci90=[8.3, 18.1], decision="undetermined", lag1_ok=True, warmup_at_limit=3)
        self.assertEqual(compare.decision(a0), "не определено: прогрев не отделён в 3 сегм.; справочно весь ДИ выше δ")
        self.assertEqual(compare.decision(dict(a0, lag1_ok=False, beyond=False, warmup_at_limit=0)),
                         "не определено: lag-1 ≥ 0,2")

    def test_k6_needs_59_in_a_row(self):
        col = compare.column("A1", {}, k6=[("s", k6_series([0.003] * 59))])
        self.assertEqual(compare.k6_verdict(col, "mcp", "zero", 0.1)[1], True)
        col = compare.column("A1", {}, k6=[("s", k6_series([0.003] * 30))])
        text, ok = compare.k6_verdict(col, "mcp", "zero", 0.1)
        self.assertIsNone(ok)
        self.assertIn("превышений 0 — доля ≤ 9,5 %", text)
        col = compare.column("A1", {}, k6=[("s", k6_series([0.003] * 50 + [0.5] * 10))])
        self.assertEqual(compare.k6_verdict(col, "mcp", "zero", 0.1)[1], False)

    def test_k1_p95_rule(self):
        m = {"K1_t0_to_cmd_s": {"n": 63, "p95": 0.95, "max": 1.2, "ci95": {"p95": [0.92, None]}}}
        self.assertEqual(compare.p95_rule(m), ("0,95 [0,92; —] (n=63)", False))
        m = {"K1_t0_to_cmd_s": {"n": 60, "p95": 0.4, "max": 0.45, "ci95": {"p95": [0.35, None]}}}
        self.assertEqual(compare.p95_rule(m)[1], True)                     # 60 подряд без превышения
        m["K1_t0_to_cmd_s"]["n"] = 40
        self.assertIsNone(compare.p95_rule(m)[1])                          # 40 мало: доля превышений до 7,2 %

    def test_p50_by_median_ci(self):
        m = {"K1_t0_to_cmd_s": {"n": 63, "p50": 0.67, "ci95": {"p50": [0.62, 0.70]}}}
        self.assertEqual(compare.p50_ci(m), ("0,67 [0,62; 0,70]", True))
        m["K1_t0_to_cmd_s"]["ci95"]["p50"] = [0.62, 0.72]
        self.assertEqual(compare.p50_ci(m)[1], None)
        m["K1_t0_to_cmd_s"]["ci95"]["p50"] = [0.71, 0.8]
        self.assertEqual(compare.p50_ci(m)[1], False)
        self.assertEqual(compare.p50_ci({"K1_t0_to_cmd_s": {"p50": 0.6}}), ("0,60", True))   # metrics.json до 20.10


if __name__ == "__main__":
    unittest.main()
