"""Сборка бандла «Записи» из артефактов прогонов на двойнике (neurobot/sim/runs, README §5): поток `neurobot-ui/1`
по прогону и итоговые числа.

  python -m neurobot.ui replay --runs DIR [ID=DIR …] --out bundle.json.gz

Каталог прогона — то, что пишет runner.py: run.json, safety.jsonl, events.jsonl и gestures.jsonl наблюдателя,
truth.jsonl, collect.jsonl, mockllm.jsonl, truth_schedule.jsonl, summary.json. Числа не считаются заново: задержка K1
(без ранних), касания, вызовы LLM на жест, доли состояний safety_gate, пробег, память — функциями
neurobot/sim/runs/metrics.py, ячейки «до/после» — форматтерами compare.py, поэтому в конце записи пульт показывает
те же числа, что compare.md. Источник на экране — «запись»; всё — стенд, ориентир, не результат робота.
"""
from __future__ import annotations

import datetime
import inspect
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from neurobot.ui import alarms as A
from neurobot.ui import fmt, robot, stream

STATS_EVERY_S = 10.0       # сводка прогона — одна точка в 10 с (PLAN §0а, п. 6)
KEEPALIVE_S = 1.0          # cmd и health повторяются не реже, пока писатель жив (живой шлюз — так же)
SAFETY_SILENT_S = 2.5      # safety.jsonl записан ≈1 Гц: перерыв дольше — safety_state молчит (живой шлюз — 1 с)
K1_WINDOW = 20             # «жест → команда» на «Обзоре» — последние 20 жестов (PLAN §4)
NONE_JUDGED_S = 10.0       # «нет реакции» наблюдатель судит в окне match_window_s после t0
CAMERA_LOST_S = 1.0        # возраст кадра perception больше — камера молчит (P2)
ROOT = Path(__file__).resolve().parents[2]                    # корень репозитория: dir прогона — от него
MSK = datetime.timezone(datetime.timedelta(hours=3))          # дата прогона — по часам лаборатории, не машины сборки

# fault_role → юнит прогона, если имена не совпадают (у A0 процессы без юнитов: демо, PicoClaw с прокси MCP)
ROLE_UNIT = {"a1": {"lidar": "lidar_driver", "camera": "camera_driver", "base": "twin_driver"},
             "a0": {"perception": "demo", "camera": "demo", "mcp": "picoclaw", "agent": "picoclaw"}}
OUTCOME = {"correct": "done", "wrong": "wrong", "none": "none", "interrupted": "cut", "unobserved": "unobserved"}
TITLES = {"a0": "A0 — стек лаборатории", "a1": "A1 — пакет 1 + безопасность v2"}


def _metrics():
    """metrics.py и compare.py прогонов — лениво: шлюзу на роботе они не нужны."""
    from neurobot.sim.runs import compare as C
    from neurobot.sim.runs import metrics as M
    return M, C


# ---- строки «до/после»: названия и цели — как в compare.md (compare.build), ячейки — его форматтерами

def compare_rows() -> List[Tuple[str, Callable[[dict], str], str, str]]:
    """(название и цель — как в compare.md, ячейка, короткое название для пульта)."""
    _, C = _metrics()
    k7 = lambda m: m.get("K7") or {}  # noqa: E731
    return [
        ("K1 t0 → первая ненулевая /cmd_vel, p50 / p95, с (без ранних)", lambda m: C.lat(m.get("K1_t0_to_cmd_s")),
         "стенд ≤ 0,5 (p95); Э2: p50 ≤ 0,7", "K1 жест → команда, p50 / p95, с"),
        ("K2 t0 → начало движения, p50 / p95, с (без ранних)", lambda m: C.lat(m.get("K2_t0_to_motion_s")),
         "K1 + ≤ 0,3", "K2 жест → движение, p50 / p95, с"),
        ("Верная реакция на жест (разбор — ниже)", lambda m: "%d из %d" % C.correct(m), "все", "Верная реакция"),
        ("K4 вызовы LLM на жест", lambda m: str((m.get("K4") or {}).get("llm_calls_outside_requests")), "0",
         "K4 LLM на жест"),
        ("Реплики «вперёд»: поехал или агент сообщил о запрете", C.moves, "все; 0 «готово» без движения",
         "Реплики «вперёд»"),
        ("Пробег по истине; время в движении", C.travel, "—", "Пробег; в движении"),
        ("safety_gate: доля времени ok / slow / blocked / estop", C.safety_share, "—",
         "safety_gate: ok / slow / blocked / estop"),
        ("K7 нежелательные действия (в час)",
         lambda m: "%s (%s)" % (k7(m).get("total"), C.num(k7(m).get("per_hour"), 1)), "0 по «не распознано»",
         "K7 нежелательные (в час)"),
        ("Касания (истина Gazebo)", C.touches, "0", "Касания (истина Gazebo)"),
        ("K8 память всех процессов системы (PSS)", C.memory, "≤ 1 МиБ/ч (8 ч)", "K8 память PSS"),
    ]


# ---- чтение прогона

class Run:
    """Артефакты одного прогона runner.py."""

    def __init__(self, run_dir: str):
        M, _ = _metrics()
        self.dir = run_dir
        self.run = M.load_json(os.path.join(run_dir, "run.json")) or {}
        if "anchor" not in self.run:
            raise ValueError("%s: нет run.json или прогон не дошёл до якоря" % run_dir)
        self.system = self.run["system"]
        self.label = self.run["label"]
        self.model = M.MODELS[self.system]
        a = self.run["anchor"]
        self.anchor_t, self.t_mono0 = float(a["t_wall"]), float(a["t_mono"])
        self.offset = self.anchor_t - self.t_mono0          # монотонные часы ВМ → стенные (пауз ВМ нет — vm_pauses)
        self.t_start, self.t_end = float(self.run["t_start"]), float(self.run["t_end"])
        events = self.run["events"]
        t_end_rel = events[-1]["m"] - self.t_mono0
        sched = os.path.join(run_dir, "truth_schedule.jsonl")
        self.segs = [s for s in M.segments(sched) if s["t_leave"] <= t_end_rel]
        self.obs = M.load_json(os.path.join(run_dir, "summary.json")) or {}
        self.per = {p["seg"]: p for p in M.load_jsonl(os.path.join(run_dir, "gestures.jsonl")) if "seg" in p}
        self.truth = M.load_jsonl(os.path.join(run_dir, "truth.jsonl"))
        self.safety = M.load_jsonl(os.path.join(run_dir, "safety.jsonl"))
        self.llm = M.load_jsonl(os.path.join(run_dir, "mockllm.jsonl"))
        self.collect = M.load_jsonl(os.path.join(run_dir, "collect.jsonl"))
        self.observer = M.load_jsonl(os.path.join(run_dir, "events.jsonl"))
        self.world = os.path.basename(self.run.get("world") or "hall.sdf").rsplit(".", 1)[0]

    def wall(self, m: float) -> float:
        return m + self.offset

    def metrics(self) -> dict:
        """Те же ключи и те же функции, что metrics.summarize, — только то, что показывает пульт (без разбора
        наблюдателя заново и без K6: им нужен numpy, а сборщик — stdlib)."""
        M, _ = _metrics()
        r, per = self.run, list(self.per.values())
        events = r["events"]
        gestures = [s for s in self.segs if s["kind"] == "gesture"]
        stopped = M.safety_stops(self.per, self.segs, self.safety, self.t_mono0)
        return {
            "system": self.system, "label": self.label, "world": self.world,
            "duration_s": round(events[-1]["m"] - events[0]["m"], 1),
            "segments": {"gestures": len(gestures), "negatives": len(self.segs) - len(gestures)},
            "K1_t0_to_cmd_s": M.latency(per, "cmd")["on_time"],
            "K2_t0_to_motion_s": M.latency(per, "motion")["on_time"],
            "early_t0_to_cmd_s": M.latency(per, "cmd")["early"],
            "reactions": self.obs.get("reactions"),
            "K4": {"llm_calls_outside_requests": M.llm_outside_requests(r, self.llm, self.model),
                   "agent": M.agent_summary(r, self.llm, self.model)},
            "stopped_by_safety": stopped,
            "K7": self.obs.get("k7"),
            "touches": M.contacts(self.truth),
            "travel": M.travel(self.truth, self.safety),
            "move_requests": M.move_requests(r, self.truth),
            "K8": {"memory": M.memory(self.collect)},
            "restarts": r.get("restarts"),
        }


# ---- поток

class Builder:
    def __init__(self, run: Run, price=None):
        self.r = run
        self.msgs: List[dict] = []
        self.tracker = A.AlarmTracker()
        self.price = price

    def add(self, msg: dict) -> None:
        msg["t"] = round(float(msg["t"]), 3)
        self.msgs.append(msg)

    def alarms(self, t: float, src: str, present: Sequence[A.Alarm], off_delay: Optional[float] = None) -> None:
        for m in self.tracker.update(t, src, present, off_delay):
            self.add(m)

    # safety_state
    def safety(self) -> None:
        prev_t: Optional[float] = None
        since: Optional[float] = None
        for row in self.r.safety:
            t = float(row["t"])
            if prev_t is not None and t - prev_t > SAFETY_SILENT_S:
                silent = A.Alarm("ui_safety_silent", A.P1, A.CODES["ui_safety_silent"].text)
                self.alarms(prev_t + SAFETY_SILENT_S, "link", [silent])
            if prev_t is not None:
                self.alarms(t, "link", [], off_delay=0.0)
            state, reasons = row["state"], [fmt.reason(r) for r in row.get("reasons") or []]
            msg = {"k": "safety", "t": t, "state": state, "reasons": reasons, "level": A.level(state, reasons)}
            if row.get("min_clearance_m") is not None:
                msg["min_clearance_m"] = row["min_clearance_m"]
            if state == "estop":
                since = t if since is None else since
                msg["estop"] = {"engaged": True, "since": round(since, 3),
                                "reasons": [A.code_of(x) for x in reasons]}
            else:
                since = None
            self.add(msg)
            self.alarms(t, "safety", A.safety_alarms(state, reasons))
            prev_t = t

    # скорости: /neurobot/cmd_vel_req и /cmd_vel по сериям наблюдателя
    def cmd(self) -> None:
        topics = {"/neurobot/cmd_vel_req": "req", "/cmd_vel": "out"}
        for name, topic in topics.items():
            runs = sorted((e for e in self.r.observer if e.get("ev") == "cmd" and e.get("topic") == name),
                          key=lambda e: e["t0"])
            last: Optional[tuple] = None
            last_t = -1e18
            for e in runs:
                v, w = round(e.get("x") or 0.0, 2) + 0.0, round(e.get("z") or 0.0, 2) + 0.0
                t0, t1 = float(e["t0"]), float(e["t1"])
                ts = [t0] if (v, w) != last or t0 - last_t >= KEEPALIVE_S else []
                nxt = (ts[-1] if ts else last_t) + KEEPALIVE_S
                while nxt <= t1:
                    ts.append(nxt)
                    nxt += KEEPALIVE_S
                for t in ts:
                    msg = {"k": "cmd", "t": t, "topic": topic, "v": v, "w": w}
                    if topic == "out":
                        msg["d_stop_m"] = robot.d_stop(max(v, 0.0))
                    self.add(msg)
                    last_t = t
                last = (v, w)

    # /neurobot/perception_health (у A0 демо его не публикует)
    def health(self) -> None:
        last: Optional[tuple] = None
        last_t = -1e18
        for e in self.r.observer:
            if e.get("ev") != "health":
                continue
            h, t = e.get("msg") or {}, float(e["t"])
            state = h.get("state") or "ok"
            age = h.get("frame_age_s")
            lost = age is not None and age > CAMERA_LOST_S
            key = (state, bool(h.get("dark")), lost)
            if key != last or t - last_t >= 5.0:
                msg = {"k": "health", "t": t, "state": state}
                for f in ("fps", "frame_age_s", "recoveries", "dark"):
                    if h.get(f) is not None:
                        msg[f] = h[f]
                self.add(msg)
                last, last_t = key, t
            present = []
            if state == "recovering":
                present.append(A.Alarm("ui_perception_recovering", A.P2,
                                       A.CODES["ui_perception_recovering"].text))
            if lost:
                present.append(A.Alarm("ui_camera_lost", A.P2, A.CODES["ui_camera_lost"].text))
            self.alarms(t, "perception", present)

    # жесты: наблюдатель (истина t0, реакция) + сообщения /neurobot/gesture (t_end, t_rec)
    def gestures(self) -> None:
        M, _ = _metrics()
        rtf_min = inspect.signature(M.latency).parameters["rtf_min"].default    # то же правило RTF, что у K1
        raw = [e for e in self.r.observer if e.get("ev") == "gesture"]
        stopped = M.safety_stops(self.r.per, self.r.segs, self.r.safety, self.r.t_mono0)["by_seg"]
        seen: List[dict] = []
        for row in sorted(self.r.per.values(), key=lambda p: p["seg"]):
            t0, lat = float(row["t0"]), row.get("lat") or {}
            gid = "seg%d" % row["seg"]
            cmd_t = t0 + lat["cmd"] if "cmd" in lat else None
            if row.get("kind") != "gesture":
                if row.get("actions") and cmd_t is not None:      # ложное действие на отрицательном сегменте (K7)
                    self.add({"k": "gesture", "t": max(cmd_t, t0), "id": gid, "seg": row["seg"], "kind": "negative",
                              "neg": row.get("name"), "d_cmd_s": round(lat["cmd"], 4), "t_cmd": round(cmd_t, 3),
                              "ref": "t0",
                              "outcome": "false_action", "maneuver": row.get("maneuver")})
                continue
            rec = row.get("rec") or {}
            first = {"k": "gesture", "id": gid, "seg": row["seg"], "kind": "gesture", "expected": row.get("expected"),
                     "ref": "t0", "early": bool(row.get("early")),
                     "rtf_ok": row.get("rtf") is None or row["rtf"] >= rtf_min}
            t_first = t0 + lat["rec"] if "rec" in lat else (cmd_t if cmd_t is not None else t0)
            if rec:
                first.update(name=rec.get("name"), cls=rec.get("cls"), conf=rec.get("conf"),
                             margin=rec.get("margin"), n_boxes=rec.get("n_boxes"), side=rec.get("side"))
                msg = min(raw, key=lambda e: abs(float(e["t"]) - t_first), default=None)
                if msg is not None and abs(float(msg["t"]) - t_first) < 0.1:
                    g = msg.get("msg") or {}
                    if g.get("t_end") is not None and g.get("t_rec") is not None:
                        first.update(t_end=g["t_end"], t_rec=g["t_rec"], d_rec_s=round(g["t_rec"] - g["t_end"], 4))
            first["t"] = t_first
            self.add(first)
            if cmd_t is not None:
                seen.append(row)
                self.add({"k": "gesture", "t": max(cmd_t, t0), "id": gid, "d_cmd_s": round(lat["cmd"], 4),
                          "t_cmd": round(cmd_t, 3), "maneuver": row.get("maneuver")})
                self.add({"k": "k1", "t": max(cmd_t, t0), "window": K1_WINDOW, "ref": "t0",
                          "last": M.latency(seen[-K1_WINDOW:], "cmd")["on_time"],
                          "run": M.latency(seen, "cmd")["on_time"]})
            reaction = row.get("reaction") or "unobserved"
            span = sum(float(p[1]) for p in row.get("phases") or [])
            t_out = max(cmd_t, t0) + span if cmd_t is not None and span else t0 + NONE_JUDGED_S
            out = {"k": "gesture", "t": t_out, "id": gid, "outcome": OUTCOME.get(reaction, "unobserved")}
            hit = stopped.get(str(row["seg"]))
            if hit:
                out["stopped_by"] = hit["reasons"]
            self.add(out)

    # LLM: журнал mockllm (вызовы этой системы — по имени модели, как metrics.py)
    def llm(self) -> None:
        M, _ = _metrics()
        r = self.r.run
        prefix = "%s-%s-" % (r["label"], r["system"])
        wins = M.request_windows(r)
        for row in self.r.llm:
            if row.get("type") != "llm" or row.get("model") != self.r.model:
                continue
            t = float(row["ts_wall"])
            ids = (row.get("oracle") or {}).get("input_ids") or []
            if ids:
                if not any(i.startswith(prefix) for i in ids):
                    continue                                       # реплика другого прогона на общем mockllm
                kind = "request"
            elif any(a <= t <= b for a, b, _ in wins):
                kind = "service"
            elif r["t_start"] <= t <= r["t_end"]:
                kind = "gesture"
            else:
                continue
            u = row.get("usage") or {}
            msg = {"k": "llm", "t": t, "kind": kind, "model": row.get("model"),
                   "prompt_tokens": u.get("prompt_tokens", 0), "completion_tokens": u.get("completion_tokens", 0),
                   "cached_tokens": u.get("cached_tokens", 0), "rub_fact": 0.0,
                   "latency_ms": round((row.get("latency_ms") or {}).get("total") or 0.0, 1)}
            if self.price is not None:
                msg["rub_est"] = round(self.price(msg["model"], msg["prompt_tokens"], msg["cached_tokens"],
                                                  msg["completion_tokens"]), 6)
            self.add(msg)

    # реплики агенту
    def agent(self, moves: Dict[int, str]) -> None:
        r = self.r.run
        prefix = "%s-%s-" % (r["label"], r["system"])
        calls: Dict[str, int] = {}
        for row in self.r.llm:
            for i in (row.get("oracle") or {}).get("input_ids") or []:
                if row.get("type") == "llm" and i.startswith(prefix):
                    calls[i] = calls.get(i, 0) + 1
        for a in r.get("agent", []):
            msg = {"k": "agent", "t": float(a["t"]) + float(a.get("latency_s") or 0.0), "n": a["n"],
                   "request": a.get("request"), "ok": bool(a.get("ok")), "latency_s": a.get("latency_s"),
                   "llm_calls": calls.get(prefix + str(a["n"]), 0)}
            if a["n"] in moves:
                msg["outcome"] = moves[a["n"]]
            self.add(msg)

    # память и CPU (сэмплер collect, 10 с), юниты; отказы и человек — события runner.py
    def resources(self) -> None:
        M, _ = _metrics()
        samples = [s for s in self.r.collect if s.get("type") == "sample"]
        header = next((s for s in self.r.collect if s.get("type") == "header"), {})
        tck = float(header.get("clk_tck") or 100)
        units = list(self.r.run.get("units") or (samples[0]["units"].keys() if samples else []))
        cpu_prev: Dict[int, float] = {}
        t_prev: Optional[float] = None
        down_windows = self.down_windows(units)
        for s in samples:
            t = float(s["t_wall"])
            per_unit, total, cpu = {}, 0.0, 0.0
            cpu_now: Dict[int, float] = {}
            for name, u in s["units"].items():
                pss = sum(p.get("pss_kib", 0) for p in u.get("procs", [])) / M.MIB
                per_unit[name] = round(pss, 1)
                if name not in M.NOT_ROBOT:
                    total += pss
                    for p in u.get("procs", []):
                        cpu_now[p["pid"]] = (p.get("utime", 0) + p.get("stime", 0)) / tck
            if t_prev is not None and t > t_prev:
                cpu = sum(max(0.0, v - cpu_prev[pid]) for pid, v in cpu_now.items() if pid in cpu_prev)
                cpu = round(100.0 * cpu / (t - t_prev), 1)
            msg = {"k": "mem", "t": t, "total_mib": round(total, 1), "units": per_unit}
            if t_prev is not None:
                msg["cpu_pct"] = cpu
            self.add(msg)
            cpu_prev, t_prev = cpu_now, t
            empty = {name for name, u in s["units"].items() if not u.get("procs")}
            self.units_msg(t, units, empty | {u for a, b, u in down_windows if a <= t <= b})
        for a, b, u in down_windows:
            self.units_msg(a, units, {x for a2, b2, x in down_windows if a2 <= a <= b2})
            self.units_msg(b, units, {x for a2, b2, x in down_windows if a2 <= b < b2})

    def units_msg(self, t: float, units: Sequence[str], down: set) -> None:
        down = sorted(d for d in down if d in units)
        self.add({"k": "units", "t": t, "up": len(units) - len(down), "total": len(units), "down": down})

    def down_windows(self, units: Sequence[str]) -> List[Tuple[float, float, str]]:
        """Окна «компонент не работает» по отказам kill/stop: от отказа до восстановления (TTR runner.py), без
        восстановления — до конца записи. Отказы потока камеры (нет кадров, застывший, чёрный) юнит не роняют."""
        M, _ = _metrics()
        out = []
        for e in self.r.run["events"]:
            if e.get("kind") != "fault" or e.get("skipped") or e.get("plan_kind") not in ("kill", "stop"):
                continue
            role = M.fault_role(e["fault"])
            unit = ROLE_UNIT.get(self.r.system, {}).get(role, role)
            if unit not in units:
                continue
            a = self.r.wall(e["m"])
            b = a + e["ttr_s"] if e.get("ttr_s") is not None else self.r.t_end
            out.append((a, b, unit))
        return out

    def events(self) -> None:
        """Отказы стенда и действия человека (оператор прогона), тревога «компонент не работает»."""
        M, _ = _metrics()
        units = list(self.r.run.get("units") or [])
        for e in self.r.run["events"]:
            t = self.r.wall(e["m"])
            if e.get("kind") == "fault" and not e.get("skipped"):
                role = M.fault_role(e["fault"])
                self.add({"k": "fault", "t": t, "fault": e["fault"], "role": role, "ttr_s": e.get("ttr_s"),
                          "path": "жест" if role in M.GESTURE_PATH else "агент"})
            elif e.get("kind") == "operator":
                msg = {"k": "operator", "t": t, "action": e["action"]}
                if e.get("what"):
                    msg["what"] = e["what"]
                why = e.get("why") or e.get("reasons") or e.get("contacts")
                if why:
                    msg["why"] = fmt.reason(why if isinstance(why, str) else ", ".join(map(str, why)))
                self.add(msg)
        windows = self.down_windows(units)
        for t in sorted({t for a, b, _ in windows for t in (a, b)}):
            down = sorted({u for a, b, u in windows if a <= t < b})
            present = [A.Alarm("ui_unit_down:" + u, A.P2, "%s: %s" % (A.CODES["ui_unit_down"].text, u)) for u in down]
            self.alarms(t, "unit", present, off_delay=0.0)

    # истина стенда: касания; сводка раз в 10 с — функциями metrics.py по префиксу записи
    def truth_and_stats(self) -> None:
        M, _ = _metrics()
        truth = self.r.truth
        last_ep, last_contact = 0, None
        for i, row in enumerate(truth):
            if not row.get("contacts"):
                continue
            if last_contact is None or row["m"] - last_contact > M.CONTACT_GAP_S:
                c = M.contacts(truth[:i + 1])
                if c["episodes"] > last_ep:
                    last_ep = c["episodes"]
                    self.add({"k": "touch", "t": self.r.wall(row["m"]), "episodes": last_ep,
                              "models": sorted(row["contacts"])})
            last_contact = row["m"]
        t = self.r.t_start
        llm = [m for m in self.msgs if m["k"] == "llm"]
        k1 = [m for m in self.msgs if m["k"] == "k1"]
        while True:
            t = min(t + STATS_EVERY_S, self.r.t_end)
            self.add(self.stats(t, llm, k1))
            if t >= self.r.t_end:
                break

    def stats(self, t: float, llm: Sequence[dict], k1: Sequence[dict]) -> dict:
        """Сводка на момент t: пробег, доли состояний и касания — функциями metrics.py по префиксу записи; вызовы
        LLM по видам и ₽ оценка — за прогон; K1 — последний k1. Исходы жестов и токены браузер видит сам."""
        M, _ = _metrics()
        tm = t - self.r.offset
        truth = [r for r in self.r.truth if r["m"] <= tm]
        safety = [r for r in self.r.safety if r["t"] <= t]
        tr = M.travel(truth, safety)
        mine = [m for m in llm if m["t"] <= t]
        msg = {"k": "stats", "t": t, "distance_m": tr["distance_m"], "moving_s": tr["moving_s"],
               "moving_share": tr["moving_share"], "touches": M.contacts(truth)["episodes"] if truth else 0,
               "llm": {k: sum(1 for m in mine if m["kind"] == k) for k in ("gesture", "request", "service")}}
        msg["llm"]["rub_est"] = round(sum(m.get("rub_est") or 0.0 for m in mine), 4)
        if tr["safety_share"]:
            msg["safety_share"] = tr["safety_share"]
        last = [m for m in k1 if m["t"] <= t]
        if last:
            msg["k1"] = last[-1]["run"]
        return msg

    def build(self, moves: Dict[int, str]) -> List[dict]:
        if self.r.safety:
            self.safety()
        self.cmd()
        self.health()
        self.gestures()
        self.llm()
        self.agent(moves)
        self.resources()
        self.events()
        self.truth_and_stats()
        for m in self.tracker.close(self.r.t_end):
            self.add(m)
        self.add({"k": "end", "t": self.r.t_end, "reason": "конец записи"})
        order = {"safety": 0, "alarm": 1}
        self.msgs.sort(key=lambda m: (m["t"], order.get(m["k"], 2)))
        return self.msgs


def pricebook():
    """₽ оценка по замороженному прайсу RouterAI 06.10.2026 (bench/costmodel); нет прайса — None."""
    try:
        from bench.costmodel.prices import default_pricebook
        pb = default_pricebook()
        return pb.call_cost_rub
    except Exception:   # noqa: BLE001 — без прайса пульт пишет «—», а не падает
        return None


def rel_dir(path: str) -> str:
    """Каталог прогона от корня репозитория (не от текущего каталога и без путей машины); вне репозитория — имя."""
    full = Path(path).resolve()
    try:
        return full.relative_to(ROOT).as_posix()
    except ValueError:
        return full.name


def run_meta(r: Run, run_id: str, label: Optional[str]) -> dict:
    day = datetime.datetime.fromtimestamp(r.t_start, MSK).strftime("%d.%m")
    title = TITLES.get(r.system, r.system.upper())
    return {"id": run_id, "label": label or "%s · %s · %s" % (title, r.label, day), "title": title,
            "system": r.system, "run": r.label, "date": day, "world": r.world,
            "source": "запись", "origin": "двойник", "has_safety": bool(r.safety),
            "t_start": round(r.t_start, 3), "t_end": round(r.t_end, 3), "anchor_t": round(r.anchor_t, 3),
            "units": list(r.run.get("units") or []), "model": r.model,
            "schedule": r.run.get("plan", {}).get("profile"),
            "schedule_sha": (r.run.get("plan", {}).get("timeline_sha256") or "")[:12],
            "dir": rel_dir(r.dir)}


def build_run(run_dir: str, run_id: Optional[str] = None, label: Optional[str] = None, price=None) -> dict:
    """Прогон бандла: meta, summary (числа metrics.py и ячейки compare.md), stream."""
    r = Run(run_dir)
    m = r.metrics()
    moves = {row["n"]: row["outcome"] for row in m["move_requests"].get("rows", [])}
    msgs = Builder(r, price).build(moves)
    out = run_meta(r, run_id or r.label, label)
    cells = [fn(m) for _, fn, _, _ in compare_rows()]
    # в бандл — числа, а не журналы: ответы инструментов и тексты ошибок агента остаются в каталоге прогона
    m["move_requests"] = {k: v for k, v in m["move_requests"].items() if k != "rows"}
    agent = m["K4"]["agent"]
    agent["failed"] = [{"n": f["n"], "request": f["request"]} for f in agent.get("failed", [])]
    out["summary"] = {"metrics": m, "compare": cells}
    out["stream"] = msgs
    return out


def build(specs: Sequence[Tuple[Optional[str], str]], note: str = "") -> dict:
    """Бандл из прогонов [(id или None, каталог)]."""
    price = pricebook()
    runs = [build_run(path, rid, price=price) for rid, path in specs]
    return {"format": stream.BUNDLE_FORMAT, "stream_format": stream.FORMAT, "builder": "python -m neurobot.ui replay",
            "note": note or ("Запись прогонов на цифровом двойнике (ВМ sim, Gazebo): все числа — ориентир стенда, "
                             "не результат робота. Задержка K1 — от t0, истины генератора жестов."),
            "geometry": robot.geometry(), "alarms": A.dictionary(),
            "price": "₽ оценка — прайс RouterAI 06.10.2026 (bench/costmodel); ₽ факт — 0: mockllm, денег нет"
            if price else "прайса нет: ₽ оценка не считалась",
            "compare_rows": [{"name": n, "goal": g, "short": sh} for n, _, g, sh in compare_rows()],
            "runs": runs}
