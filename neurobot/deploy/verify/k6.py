#!/usr/bin/env python3
"""K6 (PLAN §1) на двойнике или роботе: задержки остановки по агентскому пути и по скану, --runs замеров каждого.

    sudo python3 k6.py --out FILE.json [--runs 30] [--cases mcp,scan] [--obstacle twin|wait] [--heading ГРАД]

mcp — «stop через MCP → ноль в /cmd_vel ≤ 100 мс (p95)». Робот едет манёвром жеста: сценарий пишет во вход жестов
исполнителя (--drive-topic), распознавание на это время остановлено (--pause-unit), как в наборах двойника. Агент —
tools/call robot_stop по MCP (HTTP, сессия открыта заранее, как у PicoClaw). Метки от момента перед запросом: ноль в
/cmd_vel_agent (дошёл через rosbridge), ноль в /neurobot/cmd_vel_req (исполнитель), начало торможения (первая /cmd_vel
медленнее скорости перед вызовом) и ноль в /cmd_vel (safety_gate; штатный стоп он делает плавно — S-кривой), ответ
инструмента. После вызова сценарий ещё 1,5 с просит ехать: исполнитель обязан погасить манёвр, робот стоит.
scan — «препятствие → ноль ≤ 150 мс после скана»: робот едет вперёд (--scan-speed), перед ним появляется препятствие:
twin — столб (Ø 5 см) сервисом мира Gazebo в 2 см от корпуса на пеленге +13° (вне слепого сектора лидара, R7 RS3),
по позе из истины двойника; wait — его ставит человек (робот на HIL). От приёма первого скана с отражением ближе --near
в секторе --sector до первой /cmd_vel с нулевой линейной скоростью. Ноль раньше скана (камера глубины увидела раньше
лидара) — отрицательная задержка: критерий он выполняет.
Между замерами — разворот на месте на 180° по /odom: робот ходит туда-обратно у старта; --heading — сначала
развернуться на этот курс (градусы, кадр odom). После замера scan — рестарт safety_gate (--reset-unit): препятствие
вплотную остаётся в его памяти глубины и после того, как его убрали (пол под ним камере не виден). Итог — FILE.json:
замеры, p50/p95/max, доля в пределе. Метки — монотонные часы этого процесса при приёме (stand.py). Числа двойника —
ориентир, не результат робота.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import threading
import time
from typing import Dict, List, Optional

import stand
from neurobot_deploy.config import load

log = logging.getLogger("k6")
HZ = 20.0
POLE = "k6_pole"
POLE_AT = (0.24, 0.06)          # м, кадр базы: центр столба (край корпуса Ø 0,39 м [оценка по фото] — 0,195)
POLE_AWAY = (5.0, 5.0)          # за стенами мира lab
MCP_LIMIT, SCAN_LIMIT = 0.1, 0.15   # с: K6 (PLAN §1)
EPS = 0.001                     # м/с: «медленнее скорости перед вызовом»; плавный стоп начинается с малого шага


def mcp_run(t_call: float, t_resp: Optional[float], agent: List[tuple], req: List[tuple], cmd: List[tuple],
            v_ref: float) -> Dict[str, Optional[float]]:
    """Задержки одного стопа через MCP от t_call, с (None — не было за окно записи)."""
    def rel(t: Optional[float]) -> Optional[float]:
        return None if t is None else round(t - t_call, 4)
    return {"v_ref": round(v_ref, 3), "call_s": rel(t_resp),
            "agent_zero_s": rel(stand.first(agent, t_call, stand.zero)),
            "req_zero_s": rel(stand.first(req, t_call, stand.zero)),
            "brake_s": rel(stand.first(cmd, t_call, lambda r: r[1] < v_ref - EPS)),
            "zero_s": rel(stand.first(cmd, t_call, stand.zero))}


def scan_run(t_set: float, scans: List[tuple], cmd: List[tuple], near_m: float) -> Dict[str, Optional[float]]:
    """Скан с препятствием и ноль линейной скорости после t_set; задержка = ноль − скан (может быть < 0)."""
    t_scan = stand.first(scans, t_set, lambda r: r[1] < near_m)
    t_zero = stand.first(cmd, t_set, lambda r: r[1] <= 0.0)
    lat = None if t_scan is None or t_zero is None else round(t_zero - t_scan, 4)
    return {"seen_s": None if t_scan is None else round(t_scan - t_set, 4),
            "zero_after_set_s": None if t_zero is None else round(t_zero - t_set, 4), "zero_after_scan_s": lat}


def max_gap(times: List[float], t0: float, t1: float) -> Optional[float]:
    """Самый долгий промежуток между приёмами в [t0, t1] (самопроверка записи: узел сценария не стоял), с."""
    ts = [t for t in times if t0 <= t <= t1]
    return round(max(b - a for a, b in zip(ts, ts[1:])), 4) if len(ts) > 1 else None


def summary(rows: List[dict], key: str, limit: float) -> dict:
    vals = [r[key] for r in rows if r.get(key) is not None]
    q = stand.quantiles(vals)
    q["within"] = sum(1 for v in vals if v <= limit)
    q["limit_s"] = limit
    return q


class K6:
    def __init__(self, args, cfg):
        self.a, self.cfg = args, cfg
        self.rec = stand.Rec(cfg, "k6")
        for topic in (cfg.get("NEUROBOT_CMD_VEL"), cfg.get("NEUROBOT_CMD_VEL_REQ"), cfg.get("NEUROBOT_AGENT_TOPIC")):
            self.rec.twist(topic)
        self.rec.json(cfg.get("NEUROBOT_SAFETY_STATE_TOPIC"))
        self.rec.odom()
        lo, hi = args.sector
        self.rec.scan(cfg.get("NEUROBOT_SCAN_TOPIC"), lo, hi)
        if args.obstacle == "twin":
            self.rec.json("/twin/truth")
            self.world = stand.World(self.rec.node)
        self.rec.start()

    # ---- движение

    def drive(self, v: float, w: float, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.rec.pub(self.a.drive_topic, v, w)
            time.sleep(1.0 / HZ)

    def yaw(self) -> float:
        r = self.rec.last("odom")
        if r is None:
            raise RuntimeError("нет /odom")
        return r[3]

    def turn_to(self, target: float, timeout: float = 15.0) -> bool:
        """Поворот на месте до курса target (рад, кадр odom) с точностью 3°."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            err = stand.wrap(target - self.yaw())
            if abs(err) < math.radians(3.0):
                self.drive(0.0, 0.0, 0.6)
                return True
            self.drive(0.0, math.copysign(min(0.6, max(0.25, abs(err))), err), 1.0 / HZ)
        self.drive(0.0, 0.0, 0.6)
        return False

    def moving(self, t0: float) -> float:
        """Последняя линейная скорость /cmd_vel после t0 (0 — safety_gate не пустил)."""
        rows = self.rec.since(self.cfg.get("NEUROBOT_CMD_VEL"), t0)
        return rows[-1][1] if rows else 0.0

    def state(self) -> dict:
        r = self.rec.last(self.cfg.get("NEUROBOT_SAFETY_STATE_TOPIC"))
        return r[1] if r else {}

    def latched(self) -> bool:
        """Защёлка: серию прерываем — снимает её только человек (sudo neurobot estop release)."""
        s = self.state()
        if s.get("state") == "estop":
            log.error("защёлка safety_gate: %s — серия прервана", s.get("reasons"))
            return True
        return False

    # ---- замеры

    def mcp_case(self, heading: float) -> List[dict]:
        mcp = stand.Mcp(self.cfg.get("NEUROBOT_MCP_URL"), 60.0 * self.a.runs + 120.0)
        out = []
        try:
            for k in range(self.a.runs):
                if self.latched():
                    break
                self.turn_to(heading + (math.pi if k % 2 else 0.0))
                t0 = time.monotonic()
                self.drive(self.a.speed, 0.0, self.a.drive_s)
                v_ref = self.moving(t0)
                row: Dict[str, object] = {"run": k}
                if v_ref <= EPS:
                    row.update(skipped="safety_gate не пустил вперёд", state=self.state().get("reasons"))
                    out.append(row)
                    self.drive(0.0, 0.0, 0.6)
                    continue
                box: Dict[str, tuple] = {}
                worker = threading.Thread(target=lambda: box.__setitem__("r", mcp.call("robot_stop", {"reason": "k6"})),
                                          daemon=True)
                worker.start()
                self.drive(self.a.speed, 0.0, 1.5)
                worker.join(20.0)
                t_call, t_resp, res = box.get("r", (time.monotonic(), None, "нет ответа"))
                rows = {key: self.rec.since(self.cfg.get(key), t_call - 0.5) for key in
                        ("NEUROBOT_AGENT_TOPIC", "NEUROBOT_CMD_VEL_REQ", "NEUROBOT_CMD_VEL")}
                row.update(mcp_run(t_call, t_resp, rows["NEUROBOT_AGENT_TOPIC"], rows["NEUROBOT_CMD_VEL_REQ"],
                                   rows["NEUROBOT_CMD_VEL"], v_ref))
                row["moved_after_stop"] = any(r[1] > EPS for r in rows["NEUROBOT_CMD_VEL"]
                                              if r[0] > t_call + 1.0)
                row["rec_gap_s"] = max_gap([r[0] for r in self.rec.since("odom", t_call - 0.5)], t_call - 0.5,
                                           t_call + 1.0)
                row["result"] = res if isinstance(res, dict) else str(res)[:200]
                out.append(row)
                log.info("mcp %d: %s", k, json.dumps({k_: row[k_] for k_ in ("brake_s", "zero_s", "req_zero_s")}))
                self.drive(0.0, 0.0, 1.0)
        finally:
            mcp.close()
        return out

    def scan_case(self, heading: float) -> List[dict]:
        out = []
        if self.a.obstacle == "twin":
            self.world.create_pole(POLE, *POLE_AWAY)
        try:
            for k in range(self.a.runs):
                if self.latched():
                    break
                self.turn_to(heading + (math.pi if k % 2 else 0.0))
                t0 = time.monotonic()
                self.drive(self.a.scan_speed, 0.0, self.a.drive_s)
                row: Dict[str, object] = {"run": k, "v_ref": round(self.moving(t0), 3)}
                if row["v_ref"] <= EPS:
                    row.update(skipped="safety_gate не пустил вперёд", state=self.state().get("reasons"))
                    out.append(row)
                    self.drive(0.0, 0.0, 0.6)
                    self.reset()
                    continue
                t_set = self.place()
                self.drive(self.a.scan_speed, 0.0, 1.5)
                scans = self.rec.since("scan", t_set - 0.3)
                cmd = self.rec.since(self.cfg.get("NEUROBOT_CMD_VEL"), t_set)
                row.update(scan_run(t_set, scans, cmd, self.a.near))
                row["rec_gap_s"] = max_gap([r[0] for r in self.rec.since("odom", t_set - 0.5)], t_set - 0.5,
                                           t_set + 1.0)
                if (row.get("zero_after_scan_s") or 0.0) > SCAN_LIMIT:  # разбор: приёмы сканов и /cmd_vel у столба
                    row["timeline"] = sorted([(round(t - t_set, 4), "scan", round(d, 3)) for t, d in scans
                                              if t < t_set + 0.8] +
                                             [(round(t - t_set, 4), "cmd", round(v, 3)) for t, v, _ in cmd
                                              if t < t_set + 0.8])
                stops = [s for _, s in self.rec.since(self.cfg.get("NEUROBOT_SAFETY_STATE_TOPIC"), t_set)
                         if s.get("state") in ("blocked", "estop")]
                row["reasons"] = stops[0].get("reasons", [])[:3] if stops else []
                if self.a.obstacle == "twin":
                    row["contacts"] = sorted({c for _, tr in self.rec.since("/twin/truth", t_set)
                                              for c in tr.get("contacts", [])})
                    self.world.set_pose(POLE, *POLE_AWAY)
                else:
                    print("k6: уберите препятствие", file=sys.stderr, flush=True)
                    self.clear(30.0)
                out.append(row)
                log.info("scan %d: %s", k, json.dumps({k_: row.get(k_) for k_ in ("seen_s", "zero_after_scan_s")}))
                self.drive(0.0, 0.0, 1.0)
                self.reset()
        finally:
            if self.a.obstacle == "twin":
                self.world.remove(POLE)
        return out

    def reset(self) -> bool:
        """Рестарт safety_gate (--reset-unit) между замерами scan и ожидание взвода: память глубины держит убранное
        препятствие — пол ближе ≈1,2 м камера на 0,66 м не видит, и клетку снять нечем, — и без сброса следующий заезд
        упрётся в неё. Робот при этом стоит."""
        if not self.a.reset_unit:
            return True
        ok = stand.restart_armed(self.rec, self.cfg, self.a.reset_unit)
        self.drive(0.0, 0.0, 0.6)
        return ok

    def clear(self, timeout: float) -> bool:
        """Ждать, пока препятствие уберут: в последнем скане сектор свободен дальше --near."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            r = self.rec.last("scan")
            if r and r[1] >= self.a.near:
                return True
            time.sleep(0.1)
        return False

    def place(self) -> float:
        """Препятствие перед роботом; момент появления (twin — после ответа сервиса мира)."""
        if self.a.obstacle == "wait":
            print("k6: поставьте препятствие перед роботом (пеленг {}…{}°, ближе {} м от лидара)".format(
                *self.a.sector, self.a.near), file=sys.stderr, flush=True)
            return time.monotonic()
        self.world.set_pose(POLE, *stand.ahead(self.rec, *POLE_AT))
        return time.monotonic()


def parse(argv=None):
    p = argparse.ArgumentParser(description="K6: stop через MCP и препятствие по скану → ноль в /cmd_vel")
    p.add_argument("--out", required=True, help="итог JSON (существующий не перезаписывается)")
    p.add_argument("--runs", type=int, default=30, help="замеров каждого случая (по умолчанию 30)")
    p.add_argument("--cases", default="mcp,scan", help="mcp, scan или оба через запятую")
    p.add_argument("--obstacle", choices=("twin", "wait"), default="twin",
                   help="twin — столб сервисом мира двойника; wait — препятствие ставит человек")
    p.add_argument("--heading", type=float, help="курс первого заезда, градусы (кадр odom); по умолчанию — текущий")
    p.add_argument("--drive-topic", default="/cmd_vel_gesture", help="вход исполнителя для езды (манёвр жеста)")
    p.add_argument("--pause-unit", default="neurobot-perception",
                   help="юнит, который пишет в --drive-topic: остановлен на время замера ('' — не трогать)")
    p.add_argument("--reset-unit", default="neurobot-safety",
                   help="юнит safety_gate: рестарт после каждого замера scan (память глубины; '' — не трогать)")
    p.add_argument("--speed", type=float, default=0.15, help="скорость манёвра для mcp, м/с")
    p.add_argument("--scan-speed", type=float, default=0.08, help="скорость для scan, м/с")
    p.add_argument("--drive-s", type=float, default=1.5, help="разгон перед вызовом или препятствием, с")
    p.add_argument("--near", type=float, default=0.35, help="препятствие в скане — ближе этого, м от лидара")
    p.add_argument("--sector", type=lambda s: tuple(float(x) for x in s.split(",")), default=(5.0, 25.0),
                   help="сектор поиска препятствия в скане, «от,до» градусов (видимая лидару часть)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", stream=sys.stderr)
    a = parse(argv)
    try:
        open(a.out, "x").close()
    except FileExistsError:
        print("k6: {} уже есть — не перезаписываю".format(a.out), file=sys.stderr)
        return 2
    cases = [c for c in a.cases.split(",") if c]
    cfg = load()
    k6 = K6(a, cfg)
    doc = {"tool": "neurobot/deploy/verify/k6.py", "args": {k: v for k, v in vars(a).items()},
           "label": "двойник: задержки — ориентир, не результат робота", "t_start": time.time()}
    try:
        if a.pause_unit:
            stand.systemctl("stop", a.pause_unit)
        time.sleep(1.0)
        heading = math.radians(a.heading) if a.heading is not None else k6.yaw()
        if "mcp" in cases:
            rows = k6.mcp_case(heading)
            doc["mcp_stop"] = {"runs": rows, "brake_s": summary(rows, "brake_s", MCP_LIMIT),
                               "zero_s": summary(rows, "zero_s", MCP_LIMIT),
                               "req_zero_s": summary(rows, "req_zero_s", MCP_LIMIT),
                               "agent_zero_s": summary(rows, "agent_zero_s", MCP_LIMIT),
                               "call_s": summary(rows, "call_s", 1.0),
                               "moved_after_stop": sum(1 for r in rows if r.get("moved_after_stop"))}
        if "scan" in cases:
            rows = k6.scan_case(heading)
            doc["scan_stop"] = {"runs": rows,
                                "zero_after_scan_s": summary(rows, "zero_after_scan_s", SCAN_LIMIT),
                                "seen_s": summary(rows, "seen_s", 1.0),
                                "contacts": sum(1 for r in rows if r.get("contacts"))}
    finally:
        k6.drive(0.0, 0.0, 0.5)
        if a.pause_unit:
            stand.systemctl("start", "--no-block", a.pause_unit, check=False)
        k6.rec.close()
        doc["t_end"] = time.time()
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
