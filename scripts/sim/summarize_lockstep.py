#!/usr/bin/env python3
"""Сводка пошагового режима (lockstep_probe.py, world_events.py) → summary.json и summary.md в каталоге замеров.

Участки обхода (dock→east→north→center_sw→south→dock) сравниваются по режимам: пошаговый (мир на паузе, шаги
run_to_sim_time), свободный ход без ограничения скорости (RTF 0) и реальное время (RTF 1). Для модели робота T0
оценивается t = a + b·L по истинной длине пути (наименьшие квадраты) и разброс времени участка.
Запуск: .venv/bin/python scripts/sim/summarize_lockstep.py experiments/results/robot_smoke_20261007/lockstep
"""
from __future__ import annotations

import glob
import json
import statistics as st
import sys
from pathlib import Path

ORDER = ["east", "north", "center_sw", "south", "dock"]
FROM = {"east": "dock", "north": "east", "center_sw": "north", "south": "center_sw", "dock": "south"}


def mode_of(name: str) -> str:
    if "lockstep" in name:
        return "lockstep_rtf0"
    if "rtf1" in name:
        return "free_rtf1"
    return "free_rtf0"


def main() -> int:
    d = Path(sys.argv[1])
    legs: list[dict] = []
    tours = []
    for f in sorted(glob.glob(str(d / "tour_*.json"))):
        j = json.loads(Path(f).read_text())
        m = mode_of(Path(f).name)
        tours.append({"file": Path(f).name, "mode": m, "succeeded": j["succeeded"], "legs": len(j["legs"]),
                      "sim_s": j["sim_s"], "wall_s": j["wall_s"], "speedup": j["speedup"]})
        for x in j["legs"]:
            legs.append(dict(x, mode=m, file=Path(f).name, leg=f"{FROM[x['place']]}→{x['place']}"))
    by_leg: dict[str, dict] = {}
    for leg in [f"{FROM[p]}→{p}" for p in ORDER]:
        row = {}
        for m in ("lockstep_rtf0", "free_rtf0", "free_rtf1"):
            v = [x["sim_s"] for x in legs if x["leg"] == leg and x["mode"] == m and x["status"] == "succeeded"]
            row[m] = v
        allv = [t for vs in row.values() for t in vs]
        paths = [x["gt_path_m"] for x in legs if x["leg"] == leg]
        row["all_median_s"] = round(st.median(allv), 2)
        row["all_min_max_s"] = [min(allv), max(allv)]
        row["path_m_median"] = round(st.median(paths), 2)
        by_leg[leg] = row
    ok = [x for x in legs if x["status"] == "succeeded"]
    # t = a + b·L, без выброса (> 3 медиан участка — восстановление Nav2)
    pts = [(x["gt_path_m"], x["sim_s"]) for x in ok if x["sim_s"] <= 3 * by_leg[x["leg"]]["all_median_s"]]
    n = len(pts)
    mx = sum(p for p, _ in pts) / n
    my = sum(t for _, t in pts) / n
    b = sum((p - mx) * (t - my) for p, t in pts) / sum((p - mx) ** 2 for p, _ in pts)
    a = my - b * mx
    res = [t - (a + b * p) for p, t in pts]
    outliers = [x for x in ok if x["sim_s"] > 3 * by_leg[x["leg"]]["all_median_s"]]
    summ = {"tours": tours, "by_leg": by_leg, "model": {"a_s": round(a, 2), "b_s_per_m": round(b, 2), "n": n,
            "resid_sd_s": round(st.pstdev(res), 2)},
            "speed_m_per_s": round(sum(p for p, _ in pts) / sum(t for _, t in pts), 3),
            "outliers": [{k: x[k] for k in ("leg", "mode", "sim_s", "gt_path_m", "file")} for x in outliers],
            "gt_err_m": {"median": round(st.median(x["gt_err_m"] for x in ok), 3),
                         "max": max(x["gt_err_m"] for x in ok)}}
    for name in ("step.json", "free.json", "hold60_r2.json", "blocked_east.json", "paused_reads.json"):
        p = d / name
        if p.exists():
            summ[name.removesuffix(".json")] = json.loads(p.read_text())
    (d / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1) + "\n")
    L = ["# Пошаговый режим стенда (smoke 07.10.2026, не результаты ВКР)", "",
         "| Прогон | режим | успешно | сим. с | стенные с | ускорение |", "|---|---|---|---|---|---|"]
    for t in tours:
        L.append(f"| {t['file']} | {t['mode']} | {t['succeeded']}/{t['legs']} | {t['sim_s']} | {t['wall_s']} | {t['speedup']} |")
    L += ["", "| Участок | путь, м | пошаговый RTF 0, сим. с | свободный RTF 0 | реальное время RTF 1 | медиана |",
          "|---|---|---|---|---|---|"]
    for leg, r in by_leg.items():
        f = lambda v: ", ".join(f"{x:g}" for x in v) or "—"  # noqa: E731
        L.append(f"| {leg} | {r['path_m_median']} | {f(r['lockstep_rtf0'])} | {f(r['free_rtf0'])} | "
                 f"{f(r['free_rtf1'])} | {r['all_median_s']} |")
    m = summ["model"]
    L += ["", f"Модель участка (без выбросов): t ≈ {m['a_s']} + {m['b_s_per_m']}·L с, n = {m['n']}, "
          f"СКО остатка {m['resid_sd_s']} с; средняя скорость {summ['speed_m_per_s']} м/с. "
          f"Выбросы: {', '.join(f'{o['leg']} {o['sim_s']} с ({o['mode']})' for o in summ['outliers']) or 'нет'}.",
          f"Ошибка по истинной позе: медиана {summ['gt_err_m']['median']} м, максимум {summ['gt_err_m']['max']} м "
          "(допуск цели Nav2 0,25 м)."]
    if "step" in summ:
        L += ["", "| шаг dt, с | шагов | сим. с | стенные с | ускорение | стенное время шага p50 / max, мс |",
              "|---|---|---|---|---|---|"]
        for r in summ["step"]["rows"]:
            L.append(f"| {r['dt_s']} | {r['steps']} | {r['sim_s']} | {r['wall_s']} | {r['speedup']} | "
                     f"{r['step_wall_ms']['p50']} / {r['step_wall_ms']['max']} |")
    if "free" in summ:
        L.append(f"\nСвободный ход RTF 0 (Nav2 + лидар, камера ленивая): RTF {summ['free']['rtf']} за {summ['free']['wall_s']} с.")
    if "hold60_r2" in summ:
        h = summ["hold60_r2"]
        L.append(f"Пауза {h['hold_wall_s']} с стенного времени: часы стоят — {h['clock_frozen_during_hold']}; цель после "
                 f"паузы — {h['leg_after']['status']} за {h['leg_after']['sim_s']} сим. с.")
    if "blocked_east" in summ:
        b2 = summ["blocked_east"]
        L.append(f"Ящик 0,4 м в точке цели east: spawn {b2['spawn']['ms']} мс, цель — {b2['leg']['status']} через "
                 f"{b2['leg']['sim_s']} сим. с, робот остановился в {b2['leg']['gt_err_m']} м от цели; remove "
                 f"{b2['remove']['ms']} мс.")
    if "paused_reads" in summ:
        r = summ["paused_reads"]
        f = lambda k: f"таймаут {r[k]['timeout_s']} с" if "timeout_s" in r[k] else f"{r[k]['ms']} мс"  # noqa: E731
        L.append(f"Чтение по запросу на паузе: /scan — {f('scan_paused')}, кадр JPEG — {f('jpeg_paused')}, "
                 f"/amcl_pose (latched) — {f('amcl_paused')}; после шага 0,1 с: /scan {f('scan_step0.1')}, кадр "
                 f"{f('jpeg_step0.1')} ({r['note']}).")
    (d / "summary.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    sys.exit(main())
