"""Метрики PLAN §1 (K1–K9) одного прогона runner.py (README §5). Без ROS: читает каталог прогона.

  python3 -m neurobot.sim.runs.metrics RUN_DIR [--observer DIR] [--out metrics.json]

Источники: run.json (события, TTR, вмешательства, агент), truth.jsonl расписания (сегменты), наблюдатель жестов
(summary.json, gestures.jsonl — задержки, реакции, K6, K7, глухота), truth.jsonl прогона (касания, зазор, RTF, пробег,
время в движении), safety.jsonl (доли состояний safety_gate, защёлки), probe.jsonl (вызовы детектора), collect.jsonl
(память), fds.jsonl (файловые дескрипторы, с 20.10), mockllm.jsonl (вызовы LLM), logs/journal.json (journalctl -o json,
с 20.10) или logs/journal.txt (текст, прогоны до 20.10) — рестарты systemd и сторожа у A1. Все числа — стенд: ориентир.

Честный счёт (20.10, REVIEW H13–H15): видимость перезапуска по всем четырём пунктам PLAN §1 с окном жеста до t0 + 3 с и
оборванными манёврами, причины — раздельно, рестарты вне расписания — из журнала; TTD, TTR после снятия причины,
недоступность, первый обслуженный запрос; у квантилей — ранговый ДИ, у нулей — верхняя граница (≈ 3/n); наклон памяти —
по жизням процессов (батч-средние, TOST с δ из запаса памяти), МНК — справочно. Прежний счёт (`*_legacy`, `slope_mib_h`)
остаётся рядом для сравнения. harness/stats.py (numpy) — лениво: без numpy (сборщик «Записи» пульта) интервалов и
наклона по сегментам нет, остальное то же.
"""
from __future__ import annotations

import argparse
import bisect
import datetime
import json
import os
import re
import sys
from typing import Dict, List, Optional, Sequence, Tuple

DEAF_SHARE = 0.5           # жест «глухой», если детектор отработал меньше половины кадров сегмента
MOVING_V, MOVING_W = 0.02, 0.05   # робот едет (истина Gazebo), м/с и рад/с
SERVICE_S = 5.0            # служебные вызовы LLM хода (сжатие истории PicoClaw) — до стольких с после ответа
PAUSE_S = 5.0              # стенные часы ушли вперёд монотонных больше — ВМ стояла (сон Mac)
LOST_REACTIONS = ("none", "interrupted")
CONTACT_GAP_S = 1.0        # касания с перерывом короче — один эпизод (дребезг контакта у препятствия)
REACTION_WINDOW_S = 12.0   # окно манёвра после t0 (самый длинный манёвр демо — think, 11 с)
# путь жеста: камера (кадры), драйверы (без /scan и глубины safety_gate не пустит, без базы робот не едет),
# perception, исполнитель, safety_gate; остальное — агентский путь
GESTURE_PATH = ("camera", "perception", "executor", "safety", "lidar", "base")
UNIT_ROLES = {"neurobot-perception": ("camera", "perception"), "neurobot-rosbridge": ("rosbridge",),
              "neurobot-mcp": ("mcp",), "neurobot-picoclaw": ("agent",), "neurobot-executor": ("executor",),
              "neurobot-safety": ("safety",), "neurobot-driver@lidar": ("lidar",),
              "neurobot-driver@camera": ("camera",), "neurobot-driver@twin": ("base",)}
SAFETY_ACTIONS = ("relocate", "estop_release")
SAFETY_STATES = ("ok", "slow", "blocked", "estop")
MOVE_MIN_M = 0.01          # реплика «вперёд» выполнена: робот сдвинулся по истине хотя бы на столько
MOVE_TAIL_S = 2.0          # после ответа агента робот ещё может доехать толчок
TELEPORT_M_S, TELEPORT_EPS_M = 1.0, 0.05   # скачок позы быстрее — перенос оператором или мир двойника заново, не пробег
LATCH_AFTER_S = 3.0        # защёлка safety_gate не позже стольких с после восстановления роли — следствие отказа
LATCH_TOUCH_S = 2.0        # …если рядом (± столько с) нет касания по истине: иначе её причина — касание
DEAD_END_S = 60.0          # тупик защиты: blocked без движения по истине хотя бы столько, и жесты не помогают
MIB = 1024.0
# драйверы двойника — Gazebo и мосты, узлы лидара и камеры на Python; на роботе там urg_node2, realsense2_camera и мост
# STM32: в сумму памяти пакета не входят
NOT_ROBOT = ("twin_driver", "lidar_driver", "camera_driver")
# честный счёт K3 и K5 (H13)
REACTION_S = 3.0           # верная реакция — не позже стольких с от t0 (PLAN §1); позже — late наблюдателя
CUT_LEAD_S, CUT_LAG_S = 0.5, 1.0   # манёвр оборван отказом: оборвался от CUT_LEAD_S до начала окна до CUT_LAG_S после
FAULT_SAFETY = ("stale_", "shock", "lidar_fault")   # причины safety_gate от пропавшего датчика — следствие отказа
AGENT_PATH = ("rosbridge", "mcp", "agent")
CONSEQUENCE_S = 5.0        # рестарт другого юнита до стольких с после восстановления — следствие отказа, не свой
# память во времени (H14)
PERSIST_N = 3              # процесс прожил меньше стольких сэмплов — служебный (journalctl, systemctl): не в счёт
MISSION_H = 168.0          # время миссии между плановыми перезагрузками (PROTOCOL §5) [допущение]
MARGIN_MIB = 183.0         # пессимистичный запас памяти ветки GNOME (budget_v5b, 19.10) [замер стенда]
DELTA_MIB_H = MARGIN_MIB / MISSION_H   # граница TOST для наклона суммы памяти робота, ≈ 1,09 МиБ/ч


def load_jsonl(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def load_json(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def segments(truth_path: str) -> List[dict]:
    """Сегменты истины gesturegen (без заголовка)."""
    return [r for r in load_jsonl(truth_path) if "seg" in r]


def quantiles(values: Sequence[float]) -> dict:
    v = sorted(x for x in values if x is not None)
    if not v:
        return {"n": 0}

    def q(p: float) -> float:
        k = (len(v) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(v) - 1)
        return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 4)
    return {"n": len(v), "p50": q(0.5), "p95": q(0.95), "max": round(v[-1], 4)}


def _stats():
    """harness/stats.py — лениво и необязательно: ему нужен numpy, а сборщику «Записи» пульта — нет."""
    try:
        from harness import stats
    except ImportError:
        return None
    return stats


def quantiles_ci(values: Sequence[float], conf: float = 0.95) -> dict:
    """quantiles и ранговый 95 % ДИ p50 и p95 (harness.stats.quantile_ci): [низ, верх], сторона, которой при этом n
    нет, — None (у p95 верхней нет при n < 72). Без numpy — просто quantiles."""
    out = quantiles(values)
    st = _stats()
    if st is None or not out.get("n"):
        return out
    v = [x for x in values if x is not None]
    out["ci95"] = {}
    for key, q in (("p50", 0.5), ("p95", 0.95)):
        r = st.quantile_ci(v, q, conf)
        out["ci95"][key] = [None if r[side] is None else round(r[side], 4) for side in ("lo", "hi")]
    return out


def zero_bound(k: int, n: int, conf: float = 0.95) -> Optional[dict]:
    """Доля k из n с границей: при k = 0 — верхняя односторонняя 1 − α^(1/n) (≈ 3/n, правило трёх), иначе — двусторонний
    ДИ Клоппера–Пирсона. None — n = 0 или нет numpy."""
    st = _stats()
    if st is None or n <= 0:
        return None
    if k == 0:
        return {"k": 0, "n": n, "upper": round(st.zero_upper(n, conf), 4), "rule3": round(3.0 / n, 4)}
    lo, hi = st.clopper_pearson(k, n, conf)
    return {"k": k, "n": n, "ci": [round(lo, 4), round(hi, 4)]}


def rate_bound(k: int, hours: float, conf: float = 0.95) -> Optional[dict]:
    """События за часы наблюдения в час: при k = 0 — верхняя односторонняя граница (−ln α / ч ≈ 3 / ч), иначе — точный
    ДИ Пуассона (Гарвуд). None — нет часов или numpy."""
    st = _stats()
    if st is None or not hours or hours <= 0:
        return None
    if k == 0:
        return {"k": 0, "hours": round(hours, 3), "upper_per_h": round(st.poisson_ci(0, conf, "upper")[1] / hours, 3)}
    lo, hi = st.poisson_ci(k, conf)
    return {"k": k, "hours": round(hours, 3), "ci_per_h": [round(lo / hours, 3), round(hi / hours, 3)]}


# ---- детектор по журналу probe

def det_rate(probe: Sequence[dict], rel0: float, rel1: float, t_mono0: float) -> Optional[float]:
    """Вызовы детектора за [rel0, rel1] с от якоря: приращения det_calls между соседними строками одного процесса,
    поделённые по доле перекрытия с окном (строки probe — раз в секунду: окно жеста короче 2 с иначе ловит одну строку
    и ноль вызовов); рестарт процесса не даёт отрицательного прироста. None — ни одна пара строк окно не перекрывает
    (процесс не пишет probe: умер или стоит)."""
    a, b = t_mono0 + rel0, t_mono0 + rel1
    total, seen, last = 0.0, False, {}
    for r in probe:
        if "det_calls" not in r:
            continue
        prev = last.get(r.get("pid"))
        last[r.get("pid")] = r
        if prev is None or r["m"] <= prev["m"]:
            continue
        lo, hi = max(a, prev["m"]), min(b, r["m"])
        if hi > lo:
            seen = True
            total += max(0, r["det_calls"] - prev["det_calls"]) * (hi - lo) / (r["m"] - prev["m"])
    return round(total, 1) if seen else None


def share_in(rows: Sequence[dict], a: float, b: float, pred) -> float:
    """Доля строк с моментом m в [a, b], для которых pred истинно (0 — строк нет)."""
    got = [pred(r) for r in rows if a <= r["m"] <= b]
    return sum(got) / len(got) if got else 0.0


def moving(r: dict) -> bool:
    return abs(r.get("v") or 0.0) > MOVING_V or abs(r.get("w") or 0.0) > MOVING_W


def deaf_gestures(segs: Sequence[dict], probe: Sequence[dict], t_mono0: float, fps: float,
                  truth: Sequence[dict] = (), safety: Sequence[dict] = (),
                  reacted: Sequence[int] = ()) -> Dict[str, List[int]]:
    """Сегменты-жесты без реакции (reacted — те, на которые робот ответил манёвром: распознал раньше конца окна и
    по замыслу перестал смотреть), во время которых детектор отработал меньше DEAF_SHARE кадров. Причина по окну жеста:
    latched — стоит аварийная защёлка safety_gate (детекция на паузе, снимает человек); busy — робот больше половины
    окна едет своим манёвром (жесты принимаются только на стоящем роботе, PLAN; perception v2 детекцию на манёвре
    ставит на паузу — L6); deaf — остальное: процесс умер, завис, поток камеры умер, кадров нет — жест потерян из-за
    глухоты, а не из-за распознавания."""
    out: Dict[str, List[int]] = {"deaf": [], "busy": [], "latched": []}
    for s in segs:
        if s["kind"] != "gesture" or s["seg"] in reacted:
            continue
        span = s["t_leave"] - s["t_start"]
        got = det_rate(probe, s["t_start"], s["t_leave"], t_mono0)
        if got is not None and got >= DEAF_SHARE * fps * span:
            continue
        a, b = t_mono0 + s["t_start"], t_mono0 + s["t_leave"]
        if any(a <= r["m"] <= b and r.get("state") == "estop" for r in safety):
            out["latched"].append(s["seg"])
        elif share_in(truth, a, b, moving) > DEAF_SHARE:
            out["busy"].append(s["seg"])
        else:
            out["deaf"].append(s["seg"])
    return out


# ---- касания и RTF по истине

def contacts(truth: Sequence[dict]) -> dict:
    """Эпизоды касания (касания с перерывами короче CONTACT_GAP_S — один эпизод), их длительность и модели;
    наименьший зазор; доля rtf_ok."""
    eps, cur, last, models = [], None, None, {}
    for r in truth:
        c = r.get("contacts") or []
        if c:
            if cur is None or r["m"] - last > CONTACT_GAP_S:
                if cur is not None:
                    eps.append(last - cur)
                cur = r["m"]
                for name in c:
                    models[name] = models.get(name, 0) + 1
            last = r["m"]
    if cur is not None:
        eps.append(last - cur)
    clear = [r["clearance_m"] for r in truth if r.get("clearance_m") is not None]
    rtf = [r for r in truth if r.get("rtf") is not None]
    return {"episodes": len(eps), "total_s": round(sum(eps), 2), "max_s": round(max(eps), 2) if eps else 0.0,
            "models": models, "min_clearance_m": round(min(clear), 4) if clear else None,
            "rtf_ok_share": round(sum(1 for r in rtf if r.get("rtf_ok")) / len(rtf), 4) if rtf else None,
            "rtf_median": quantiles([r["rtf"] for r in rtf]).get("p50")}


# ---- журнал супервизора: рестарты systemd и сторожа

UNIT_LINE = re.compile(r"^(neurobot-[\w@.-]+?)\.service: (.*)$")
STARTED_LINE = re.compile(r"^Started (neurobot-[\w@.-]+?)\.service\b")
WATCHDOG_LINE = re.compile(r"^neurobot-watchdog: ([^:]+): ([a-z_]+): restart (neurobot-[\w@.-]+?)(?:\.service)?: (.*)$")
LIMIT = re.compile(r"limit (\d+(?:\.\d+)?)(ms|s|min)\b")
AGE = re.compile(r"\((\d+(?:[.,]\d+)?) с\)")
UNIT_S = {"ms": 0.001, "s": 1.0, "min": 60.0}
STARTED_ID = "39f53479d3a045ac8e11786248231fbf"   # MESSAGE_ID «юнит запущен» (SD_MESSAGE_UNIT_STARTED, sd-messages.h)
TZ_NO_COLON = re.compile(r"([+-]\d\d)(\d\d)$")
UNITS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir, "deploy", "units")
_STARTED_BY_DESC: List[Tuple[re.Pattern, str]] = []   # «Started Описание.» → юнит (лениво)


def unit_roles(unit: str) -> Tuple[str, ...]:
    """Роли юнита (UNIT_ROLES); у прочих (сторож) — имя без префикса."""
    unit = unit.replace(".service", "")
    return UNIT_ROLES.get(unit, (unit.replace("neurobot-", ""),))


def _wall(stamp: str) -> Optional[float]:
    """Метка journalctl -o short-iso(-precise) → секунды эпохи. Смещение пояса — «+03:00» (systemd 255) или «+0300»
    (245, Ubuntu 20.04): второе datetime.fromisoformat Python 3.8 не разбирает — двоеточие вставляется."""
    try:
        return datetime.datetime.fromisoformat(TZ_NO_COLON.sub(r"\1:\2", stamp)).timestamp()
    except ValueError:
        return None


def _started_by_description(msg: str) -> Optional[str]:
    """Юнит строки «Started <Description>.» — так пишет systemd 245 (без имени юнита): по Description= юнитов пакета
    (neurobot/deploy/units; у шаблона %i — имя экземпляра). None — не «Started» юнита пакета."""
    if not _STARTED_BY_DESC and os.path.isdir(UNITS_DIR):
        for name in sorted(os.listdir(UNITS_DIR)):
            if not name.endswith(".service"):
                continue
            with open(os.path.join(UNITS_DIR, name), encoding="utf-8") as f:
                desc = next((x.split("=", 1)[1].strip() for x in f if x.startswith("Description=")), None)
            if desc:
                pat = "^Started %s\\.$" % re.escape(desc).replace("%i", "(.+)")
                _STARTED_BY_DESC.append((re.compile(pat), name[:-len(".service")]))
    for pat, unit in _STARTED_BY_DESC:
        m = pat.match(msg)
        if m:
            return unit + (m.group(1) if pat.groups else "")
    return None


def _message(value) -> str:
    """MESSAGE журнала JSON: не-UTF-8 строку journalctl отдаёт массивом байтов."""
    if isinstance(value, list):
        return bytes(value).decode("utf-8", errors="replace")
    return value or ""


def journal_entries(path: str):
    """Записи журнала прогона → (стенные с, юнит systemd или None — строка не от systemd, текст, «Started»?).

    Строка-объект — journalctl -o json (runner.journal_dump с 20.10): время — _SOURCE_REALTIME_TIMESTAMP (момент записи
    источником; его же показывает текст), без него — __REALTIME_TIMESTAMP; systemd — _PID 1, юнит — поле UNIT,
    «Started» — MESSAGE_ID; от версии systemd и формата времени это не зависит. Иначе — текст
    -o short-iso-precise (прогоны до 20.10): systemd 255 пишет «Started ЮНИТ.service - Описание.», 245 —
    «Started Описание.» (юнит — по Description=, _started_by_description), остановку — «Deactivated successfully.»
    и «Succeeded.» соответственно. Текст systemd — без префикса «ЮНИТ.service: »."""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("{"):
                try:
                    e = json.loads(line)
                    wall = int(e.get("_SOURCE_REALTIME_TIMESTAMP") or e["__REALTIME_TIMESTAMP"]) / 1e6
                except (ValueError, KeyError):
                    continue
                msg = _message(e.get("MESSAGE"))
                if e.get("_PID") != "1":
                    yield wall, None, msg, False
                    continue
                unit = e.get("UNIT") or ""
                if unit.startswith("neurobot-") and unit.endswith(".service"):
                    prefix = unit + ": "
                    yield (wall, unit[:-len(".service")], msg[len(prefix):] if msg.startswith(prefix) else msg,
                           e.get("MESSAGE_ID") == STARTED_ID)
                continue
            stamp, _, rest = line.rstrip("\n").partition(" ")
            wall = _wall(stamp)
            if wall is None:
                continue
            _, _, rest = rest.partition(" ")                  # имя хоста
            src, _, msg = rest.partition(": ")
            if not src.startswith("systemd["):
                yield wall, None, msg, False
                continue
            m = STARTED_LINE.match(msg)
            unit = m.group(1) if m else _started_by_description(msg)
            if unit:
                yield wall, unit, msg, True
                continue
            m = UNIT_LINE.match(msg)
            if m:
                yield wall, m.group(1), m.group(2), False


def journal_restarts(path: str, run: dict) -> Optional[List[dict]]:
    """Рестарты юнитов пакета по журналу прогона (runner.journal_dump, только A1; формат — journal_entries):
    автоматические — systemd («Scheduled restart job») и сторожа («neurobot-watchdog: …: restart ЮНИТ»); юнит умер и не
    поднят до конца записи — тоже (by none). Ручные (doctor, перенос робота, запуск упавшего) сюда не попадают: таких
    строк у них нет, они — в run.json. Время — секунды от якоря по монотонным часам: стенные часы журнала переводятся
    смещением t − m ближайшего события run.json (пауза ВМ меняет смещение). [{unit, by: systemd | watchdog | none,
    cause, start_s — оценка начала отказа (смерть; у зависания — минус предел WatchdogSec или возраст темы из строки
    сторожа), detect_s — первая строка о нём, action_s — рестарт, up_s — «Started» (None — не поднялся)}]; None —
    журнала нет."""
    if not os.path.exists(path):
        return None
    marks = sorted((e["t"], e["t"] - e["m"]) for e in run.get("events", []) if "t" in e and "m" in e)
    if not marks or "anchor" not in run:
        return []
    walls = [t for t, _ in marks]
    t_mono0 = run["anchor"]["t_mono"]

    def rel(wall: float) -> float:
        i = bisect.bisect_left(walls, wall)
        k = min((k for k in (i - 1, i) if 0 <= k < len(walls)), key=lambda k: abs(walls[k] - wall))
        return round(wall - marks[k][1] - t_mono0, 3)

    pending: Dict[str, Tuple[float, str, float]] = {}      # юнит → (первая строка, причина, длительность зависания)
    waiting: Dict[str, dict] = {}                          # юнит → рестарт без «Started»
    out: List[dict] = []
    for wall, unit, what, started in journal_entries(path):
        if not walls[0] - 1.0 <= wall <= walls[-1] + 5.0:
            continue
        r = rel(wall)
        if unit is None:
            m = WATCHDOG_LINE.match(what)
            if m and "не удался" not in m.group(4):
                ages = [float(x.replace(",", ".")) for x in AGE.findall(m.group(4))]
                rec = {"unit": m.group(3), "by": "watchdog", "cause": m.group(2), "detect_s": r,
                       "start_s": round(r - max(ages, default=0.0), 3), "action_s": r, "up_s": None}
                out.append(rec)
                waiting[rec["unit"]] = rec
                pending.pop(rec["unit"], None)
            continue
        if started:
            if unit in waiting:
                waiting.pop(unit)["up_s"] = r
            pending.pop(unit, None)
            continue
        if what.startswith("Watchdog timeout"):
            lim = LIMIT.search(what)
            pending.setdefault(unit, (r, "watchdog", float(lim.group(1)) * UNIT_S[lim.group(2)] if lim else 0.0))
        elif what.startswith("Main process exited"):
            code = re.search(r"code=(\w+)", what)
            pending.setdefault(unit, (r, code.group(1) if code else "exited", 0.0))
        elif what.startswith(("Deactivated successfully", "Succeeded")):
            pending.setdefault(unit, (r, "exited", 0.0))
        elif what.startswith("Scheduled restart job"):
            detect, cause, stall = pending.pop(unit, (r, "exited", 0.0))
            rec = {"unit": unit, "by": "systemd", "cause": cause, "detect_s": detect,
                   "start_s": round(detect - stall, 3), "action_s": r, "up_s": None}
            out.append(rec)
            waiting[unit] = rec
    for unit, (detect, cause, stall) in pending.items():
        out.append({"unit": unit, "by": "none", "cause": cause, "detect_s": detect, "start_s": round(detect - stall, 3),
                    "action_s": None, "up_s": None})
    return sorted(out, key=lambda x: x["start_s"])


# ---- отказы, восстановление, видимость перезапуска

def fault_role(fault: str) -> str:
    """camera_delay → camera, kill_perception → perception, stop_perception → perception."""
    return fault.split("_", 1)[1] if fault.startswith(("kill_", "stop_")) else fault.split("_", 1)[0]


def path_of(role: str) -> str:
    return "жест" if role in GESTURE_PATH else "агент" if role in AGENT_PATH else "надзор"


def op_roles(o: dict) -> set:
    """Роли, которые чинит действие оператора: перезапуск демо — путь камеры, PicoClaw — агент и его MCP (прокси A0 —
    дочерний процесс шлюза), юниты пакета — по UNIT_ROLES. Перенос робота и снятие защёлки — не ремонт отказа, а
    безопасность (касание, удар): ни одной роли."""
    if o.get("action") == "restart":
        return {"demo": {"camera", "perception"}, "picoclaw": {"agent", "mcp"}}.get(o.get("what"), {o.get("what")})
    units = o.get("units") or ([o["what"]] if o.get("what") else [])
    return {r for u in units for r in UNIT_ROLES.get(u.replace(".service", ""), ())}


def latches(safety: Sequence[dict], ops: Sequence[dict]) -> List[dict]:
    """Защёлки safety_gate по safety.jsonl: [{m0, m1 (None — до конца записи), reasons, released}]; released —
    её снял оператор (событие estop_release не раньше начала и не позже 5 с после конца: CLI ждёт новое состояние)."""
    out: List[dict] = []
    cur: Optional[dict] = None
    for r in safety:
        if r.get("state") == "estop" and cur is None:
            cur = {"m0": r["m"], "m1": None, "reasons": [str(x).split(":")[0] for x in r.get("reasons") or []]}
            out.append(cur)
        elif r.get("state") != "estop" and cur is not None:
            cur["m1"], cur = r["m"], None
    for c in out:
        hi = float("inf") if c["m1"] is None else c["m1"] + 5.0
        c["released"] = any(o.get("action") == "estop_release" and c["m0"] <= o["m"] <= hi for o in ops)
    return out


def from_fault(reasons) -> bool:
    """Причины safety_gate, которые даёт пропавший или зависший датчик (stale_*, удар, отказ лидара): остановка по ним в
    окне отказа — следствие отказа, а не препятствия."""
    return any(r.startswith(FAULT_SAFETY) for r in reasons)


def progress_after(probe: Sequence[dict], m0: float) -> Optional[float]:
    """Первая строка probe после m0 с приростом det_calls против прошлой строки того же процесса (тоже после m0) — как
    runner.ProbeTail.progress_since: детектор снова работает. None — до конца записи нет."""
    last: Dict[object, int] = {}
    for r in probe:
        if "det_calls" not in r or r["m"] <= m0:
            continue
        pid = r.get("pid")
        if pid in last and r["det_calls"] > last[pid]:
            return r["m"]
        last[pid] = r["det_calls"]
    return None


def cut_at(s: dict, p: dict) -> Optional[float]:
    """Момент (от якоря), когда оборвался манёвр сегмента s, или None — не оборван: реакция наблюдателя «оборвана»
    (начало ожидаемого манёвра) или «неверная» с началом другого манёвра (partial:…); конец эпизода — t0 + задержка
    команды + длительности фаз."""
    if p.get("reaction") != "interrupted" and not str(p.get("maneuver") or "").startswith("partial:"):
        return None
    lat = (p.get("lat") or {}).get("cmd")
    if lat is None:
        return None
    return s["t0"] + lat + sum(d for _, d in p.get("phases") or [])


def maneuver_span(s: dict, p: dict) -> Optional[Tuple[float, float]]:
    """[начало, конец] эпизода движения сегмента от якоря; None — реакции нет."""
    lat = (p.get("lat") or {}).get("cmd")
    if lat is None or not p.get("phases"):
        return None
    return s["t0"] + lat, s["t0"] + lat + sum(d for _, d in p["phases"])


class _Judge:
    """Видимость перезапуска по PLAN §1 для окна недоступности [a, b] (секунды от якоря) одной роли."""

    def __init__(self, run: dict, segs: Sequence[dict], per: Dict[int, dict], deaf: Sequence[int], busy: set,
                 stopped: Optional[dict], excluded: set, safety: Sequence[dict], truth: Sequence[dict]):
        self.t0 = run["anchor"]["t_mono"]
        self.ops = [e for e in run["events"] if e.get("kind") == "operator"]
        self.gestures = [s for s in segs if s["kind"] == "gesture"]
        self.per, self.deaf, self.safety = per, set(deaf), safety
        touched = [r["m"] for r in truth if r.get("contacts")]
        self.caused = [c for c in latches(safety, self.ops) if (c["released"] or c["m1"] is None)
                       and not any(abs(m - c["m0"]) <= LATCH_TOUCH_S for m in touched)]
        if stopped is None:                       # прежний вызов: исключения — как дали
            self.skip = set(excluded)
        else:                                     # остановки защитой по препятствию — не отказ; по датчику — отказ
            by = stopped.get("by_seg") or {}
            self.skip = set(busy) | {int(k) for k, v in by.items() if not from_fault(v.get("reasons") or [])}

    def humans(self, a: float, b: float, role: str) -> Tuple[List[dict], List[dict]]:
        ops = [o for o in self.ops if a <= o["m"] - self.t0 <= b + 1.0 and role in op_roles(o)]
        return ops, [c for c in self.caused if a <= c["m0"] - self.t0 <= b + LATCH_AFTER_S]

    def safety_reasons(self, lo: float, hi: float) -> set:
        return {str(x).split(":")[0] for r in self.safety if lo <= r["m"] - self.t0 <= hi
                and r.get("state") in ("blocked", "estop") for x in r.get("reasons") or []}

    def gesture_path(self, a: float, b: float) -> dict:
        """Оборванные манёвры и потерянные жесты окна: жест задет, если отрезок от его начала до t0 + REACTION_S
        пересекает [a, b]; манёвр оборван отказом, если оборвался от CUT_LEAD_S до a до CUT_LAG_S после b."""
        cut, cut_safety, lost, late = [], [], [], []
        for s in self.gestures:
            p = self.per.get(s["seg"], {})
            c = cut_at(s, p)
            if c is not None and a - CUT_LEAD_S <= c <= b + CUT_LAG_S:
                why = self.safety_reasons(c - 1.0, c + 0.5)
                if from_fault(why):
                    cut_safety.append(s["seg"])
                elif not why:
                    cut.append(s["seg"])
                continue                                    # оборвала защита по препятствию — не отказ
            if not (s["t_start"] <= b and s["t0"] + REACTION_S >= a) or s["seg"] in self.skip:
                continue
            if p.get("late"):
                late.append(s["seg"])
            elif s["seg"] in self.deaf or p.get("reaction") == "none":
                lost.append(s["seg"])
        return {"cut": cut, "cut_safety": cut_safety, "lost": lost, "late": late}

    def next_gesture_gap(self, a: float) -> Optional[float]:
        later = [s["t_start"] for s in self.gestures if s["t_start"] > a]
        return min(later) - a if later else None


WHY = {"cut": "оборван манёвр: %d", "cut_safety": "оборван защитой по датчику отказа: %d", "lost": "потерян жест: %d",
       "late": "поздняя реакция (> 3 с): %d"}


def judge_window(j: _Judge, a: float, b: Optional[float], end_rel: float, role: str,
                 agent_failed: Sequence[int] = ()) -> dict:
    """Причины видимости окна [a, b] роли: codes (cut, cut_safety, lost, late, outage, human, latch, not_recovered,
    agent) и тексты why; b = None — не восстановилось до конца записи."""
    stop = end_rel if b is None else b
    codes, why, out = [], [], {}
    ops, latched = j.humans(a, stop, role)
    if ops:
        codes.append("human")
        why.append("понадобился человек")
    if latched:
        codes.append("latch")
        why.append("защёлка safety_gate (%s): %s" % (", ".join(sorted({x for c in latched for x in c["reasons"]}))
                                                     or "—", "снял человек" if all(c["released"] for c in latched)
                                                     else "стоит до конца прогона"))
    if b is None:
        codes.append("not_recovered")
        why.append("не восстановилось само")
    if role in GESTURE_PATH:
        g = j.gesture_path(a, stop)
        for key in ("cut", "cut_safety", "lost", "late"):
            if g[key]:
                codes.append(key)
                why.append(WHY[key] % len(g[key]))
        gap = j.next_gesture_gap(a)
        if gap is not None and stop - a > gap:
            codes.append("outage")
            why.append("недоступна дольше паузы до жеста (%.1f > %.1f с)" % (stop - a, gap))
        out.update(g, gap_to_next_gesture_s=None if gap is None else round(gap, 1))
    elif agent_failed:
        codes.append("agent")
        why.append("реплик без ответа: %d" % len(agent_failed))
    out.update(codes=codes, why=why, visible=bool(codes), operator=[o["action"] + ":" + str(o.get("what") or "")
                                                                    for o in ops] +
               ["estop_release:" for c in latched if c["released"]],
               latch=[{"at_s": round(c["m0"] - j.t0, 1), "reasons": c["reasons"], "released": c["released"]}
                      for c in latched])
    return out


def _legacy(j: _Judge, f: dict, start: float, stop: float, role: str, excluded: set,
            failed: Sequence[int]) -> List[str]:
    """Причины по счёту до 20.10 (окно жеста — пересечение [t_start, t_leave] с окном отказа, оборванный манёвр
    прошлого жеста и «поздно» не в счёт, исключены все остановки защитой) — для сравнения."""
    ops, latched = j.humans(start, stop, role)
    why = ["человек"] * bool(ops) + ["защёлка"] * bool(latched) + ["не поднялось"] * (f.get("ttr_s") is None)
    if role in GESTURE_PATH:
        lost = [s for s in j.gestures if s["t_leave"] >= start and s["t_start"] <= stop and s["seg"] not in excluded
                and (s["seg"] in j.deaf or j.per.get(s["seg"], {}).get("reaction") in LOST_REACTIONS)]
        gap = j.next_gesture_gap(start)
        why += ["потерян жест"] * bool(lost) + ["пауза"] * (f.get("ttr_s") is not None and gap is not None
                                                           and f["ttr_s"] > gap)
    elif failed:
        why.append("реплика")
    return why


def first_served(a: float, role: str, j: _Judge, run: dict) -> Optional[float]:
    """Время от отказа до первого обслуженного запроса: путь жеста — первый жест, начатый после отказа, на который робот
    отреагировал движением (до начала реакции; ждёт и паузу расписания до жеста); агентский путь — первая реплика после
    отказа с ответом (до ответа). None — такого до конца записи нет или путь не тот."""
    if role in GESTURE_PATH:
        for s in j.gestures:
            lat = (j.per.get(s["seg"], {}).get("lat") or {}).get("cmd")
            if s["t_start"] >= a and lat is not None:
                return round(s["t0"] + lat - a, 2)
        return None
    if role in AGENT_PATH:
        for r in sorted(run.get("agent", []), key=lambda r: r["m"]):
            if r["m"] - j.t0 >= a and r.get("ok"):
                return round(r["m"] - j.t0 + (r.get("latency_s") or 0.0) - a, 2)
    return None


def fault_rows(run: dict, segs: Sequence[dict], per: Dict[int, dict], deaf: Sequence[int],
               excluded: Sequence[int] = (), safety: Sequence[dict] = (), truth: Sequence[dict] = (),
               busy: Sequence[int] = (), stopped: Optional[dict] = None, probe: Sequence[dict] = (),
               restarts: Optional[List[dict]] = None) -> List[dict]:
    """Строка на внесённый отказ: видимость перезапуска по PLAN §1 и времена восстановления (K5).

    Окно недоступности [a, b]: a — отказ; b — роль снова работает (perception и отказы камеры — заново по probe:
    детектор снова вызывается, у камеры — после конца удержания; остальное — TTR runner.py), у невосстановившегося — до
    конца записи. Видим (codes, why), если: понадобился человек — чинил роль отказа или снял защёлку, вставшую в окне
    (до LATCH_AFTER_S после) не от касания; не поднялось само; путь жеста — оборван манёвр (сам или защитой по датчику,
    который выключил отказ), потерян жест или реакция позже 3 с (жест задет, если [начало жеста; t0 + 3 с] пересекает
    окно; кроме занятых своим манёвром и остановленных защитой по препятствию — stopped, busy), недоступен дольше паузы
    до следующего жеста; агентский путь — реплика без ответа (каждая — последнему отказу пути, в окно которого попала).
    Прежний счёт — visible_legacy и why_legacy. Без stopped исключения — excluded, как до 20.10.

    Времена: unavailable_s — длительность окна; ttd_s — от отказа до первой строки супервизора о нём (journal_restarts;
    None — супервизор не действовал: отказ камеры вне процесса или нет журнала); ttr_after_s — от снятия причины (kill —
    сразу, SIGSTOP — конец удержания или раньше, если процесс убил супервизор, камера — конец удержания) до подъёма;
    first_served_s — до первого обслуженного запроса (first_served). restarts — рестарты журнала, отнесённые к отказу по
    первой строке о них (от 1 с до отказа до CONSEQUENCE_S после восстановления): своей роли и других — следствие."""
    t0 = run["anchor"]["t_mono"]
    faults = [e for e in run["events"] if e.get("kind") == "fault"]
    end_rel = run["events"][-1]["m"] - t0 if run["events"] else 0.0
    excluded = set(excluded)
    j = _Judge(run, segs, per, deaf, set(busy), stopped, excluded, safety, truth)

    def legacy_window(f: dict) -> Tuple[float, float]:
        start = f["m"] - t0
        return start, start + f["ttr_s"] if f.get("ttr_s") is not None else end_rel

    def window(f: dict) -> Tuple[float, Optional[float]]:
        a, hold, role = f["m"] - t0, f.get("duration_s") or 0.0, fault_role(f["fault"])
        if probe and (role == "perception" or f["fault"].startswith("camera_")):
            up = progress_after(probe, f["m"] + (hold if f["fault"].startswith("camera_") else 0.0))
            return a, None if up is None else up - t0
        return a, None if f.get("ttr_s") is None else a + f["ttr_s"]

    agent_faults = [(i, legacy_window(f)) for i, f in enumerate(faults)
                    if not f.get("skipped") and fault_role(f["fault"]) not in GESTURE_PATH]
    blamed: Dict[int, List[int]] = {}
    for r in run.get("agent", []):
        if not r.get("ok"):
            hit = [i for i, (lo, hi) in agent_faults if lo <= r["m"] - t0 <= hi]
            if hit:
                blamed.setdefault(hit[-1], []).append(r["n"])
    pool = list(restarts or [])
    out = []
    for i, f in enumerate(faults):
        row = {"fault": f["fault"], "at_s": round(f["m"] - t0, 1), "seg": f.get("seg"), "ttr_s": f.get("ttr_s"),
               "skipped": f.get("skipped")}
        if f.get("skipped"):
            out.append(row)
            continue
        role = fault_role(f["fault"])
        a, b = window(f)
        hold = f.get("duration_s") or 0.0
        reach = end_rel if b is None else max(b, a + hold) + CONSEQUENCE_S
        mine = [r for r in pool if a - 1.0 <= r["detect_s"] <= reach and role in unit_roles(r["unit"])]
        other = [r for r in pool if r not in mine and a - 1.0 <= r["detect_s"] <= reach]
        pool = [r for r in pool if r not in mine and r not in other]
        failed = blamed.get(i, [])
        res = judge_window(j, a, b, end_rel, role, failed)
        legacy = _legacy(j, f, *legacy_window(f), role, excluded, failed)
        removed = a if f["fault"].startswith("kill_") else min([a + hold] + [r["detect_s"] for r in mine
                                                                              if f["fault"].startswith("stop_")])
        unavailable = None if b is None else round(b - a, 2)
        row.update(res, path=path_of(role), agent_failed=failed if role not in GESTURE_PATH else [],
                   visible_legacy=bool(legacy), why_legacy=legacy, hold_s=hold or None, unavailable_s=unavailable,
                   ttd_s=round(min(r["detect_s"] for r in mine) - a, 2) if mine else None,
                   detected_by=("%s:%s" % (mine[0]["by"], mine[0]["cause"])) if mine else None,
                   ttr_after_s=None if b is None else round(max(0.0, b - removed), 2),
                   first_served_s=first_served(a, role, j, run),
                   restarts=[dict(r, consequence=False) for r in mine] + [dict(r, consequence=True) for r in other])
        if role in GESTURE_PATH:
            row["lost_gestures"] = sorted(res["lost"] + res["late"])
        out.append(row)
    return out


def unscheduled_rows(run: dict, segs: Sequence[dict], per: Dict[int, dict], deaf: Sequence[int],
                     restarts: Sequence[dict], faults: Sequence[dict], safety: Sequence[dict] = (),
                     truth: Sequence[dict] = (), busy: Sequence[int] = (),
                     stopped: Optional[dict] = None) -> List[dict]:
    """Рестарты вне расписания (журнал, не отнесённые ни к одному внесённому отказу — fault_rows) под те же правила
    видимости: окно — от оценки начала отказа до «Started» юнита (нижняя граница недоступности: готовность по READY или
    старт launch); на HIL и в 8-часовом прогоне без инъекций других и не будет."""
    t0 = run["anchor"]["t_mono"]
    end_rel = run["events"][-1]["m"] - t0 if run["events"] else 0.0
    taken = {(r["unit"], r["start_s"]) for f in faults for r in f.get("restarts") or []}
    j = _Judge(run, segs, per, deaf, set(busy), stopped, set(), safety, truth)
    out = []
    for r in restarts:
        if (r["unit"], r["start_s"]) in taken:
            continue
        role = unit_roles(r["unit"])[-1]
        a, b = r["start_s"], r["up_s"]
        failed = [q["n"] for q in run.get("agent", []) if not q.get("ok") and a <= q["m"] - t0 <= (b or end_rel)]
        res = judge_window(j, a, b, end_rel, role, failed if role in AGENT_PATH else ())
        out.append(dict(res, fault="restart:" + r["unit"].replace("neurobot-", ""), at_s=round(a, 1), unit=r["unit"],
                        path=path_of(role), detected_by="%s:%s" % (r["by"], r["cause"]),
                        ttd_s=round(r["detect_s"] - a, 2), unavailable_s=None if b is None else round(b - a, 2),
                        first_served_s=first_served(a, role, j, run)))
    return out


def k5_summary(rows: Sequence[dict]) -> Dict[str, dict]:
    """K5 по типу отказа: n, не поднялось, p50/p95/max TTD, TTR после снятия причины, недоступности и первого
    обслуженного запроса; ttr_legacy — TTR runner.py (от отказа, как до 20.10). Без средних (Davidovič, 2021)."""
    out: Dict[str, dict] = {}
    for f in rows:
        if f.get("skipped"):
            continue
        g = out.setdefault(f["fault"], {"rows": []})
        g["rows"].append(f)
    for name, g in out.items():
        rs = g.pop("rows")
        g.update(n=len(rs), not_recovered=sum(1 for f in rs if f.get("unavailable_s") is None),
                 **{key: quantiles([f.get(key) for f in rs])
                    for key in ("ttd_s", "ttr_after_s", "unavailable_s", "first_served_s")},
                 ttr_legacy_s=quantiles([f.get("ttr_s") for f in rs]))
    return out


# ---- агент и LLM

def request_windows(run: dict) -> List[Tuple[float, float, int]]:
    """Окна реплик пользователя по стенным часам: от начала реплики до ответа + SERVICE_S (не дальше следующей)."""
    reqs = sorted(run.get("agent", []), key=lambda r: r["t"])
    out = []
    for i, r in enumerate(reqs):
        hi = r["t"] + (r.get("latency_s") or 0.0) + SERVICE_S
        if i + 1 < len(reqs):
            hi = min(hi, reqs[i + 1]["t"])
        out.append((r["t"], hi, r["n"]))
    return out


def service_calls(run: dict, llm: Sequence[dict], model: str) -> Dict[int, int]:
    """Вызовы LLM этой системы (по имени модели) без ожидания оракула внутри окна реплики — служебные вызовы её хода
    (сжатие истории сессии PicoClaw): {номер реплики: вызовов}."""
    out: Dict[int, int] = {}
    wins = request_windows(run)
    for r in llm:
        if r.get("type") != "llm" or r.get("model") != model or (r.get("oracle") or {}).get("input_ids"):
            continue
        n = next((n for lo, hi, n in wins if lo <= float(r["ts_wall"]) <= hi), None)
        if n is not None:
            out[n] = out.get(n, 0) + 1
    return out


def agent_summary(run: dict, llm: Sequence[dict], model: str) -> dict:
    """Реплики пользователя: успешные (финальный ответ и все инструменты без ошибки), задержка; вызовы LLM по
    input_id оракула и служебные вызовы хода (service_calls)."""
    prefix = "%s-%s-" % (run["label"], run["system"])
    mine = [r for r in llm if r.get("type") == "llm" and any(
        i.startswith(prefix) for i in (r.get("oracle") or {}).get("input_ids", []))]
    reqs = run.get("agent", [])
    per_req = {}
    for r in mine:
        for i in r["oracle"]["input_ids"]:
            if i.startswith(prefix):
                per_req[i] = per_req.get(i, 0) + 1
    failed = [{"n": r["n"], "request": r["request"], "error": r.get("error"),
               "tools": [t["content"][:120] for t in r.get("tools", []) if not t["ok"]]}
              for r in reqs if not r.get("ok")]
    prompt = [r["usage"]["prompt_tokens"] for r in mine if r.get("usage")]
    service = service_calls(run, llm, model)
    return {"requests": len(reqs), "ok": sum(1 for r in reqs if r.get("ok")), "failed": failed,
            "latency_s": quantiles([r["latency_s"] for r in reqs if r.get("ok")]),
            "llm_calls": len(mine), "llm_calls_per_request": quantiles(list(per_req.values())),
            "service_calls": sum(service.values()), "service_calls_by_request": service,
            "prompt_tokens_per_call": quantiles(prompt), "n_tools_in_catalog": sorted({r.get("n_tools") for r in mine})}


def llm_outside_requests(run: dict, llm: Sequence[dict], model: str) -> int:
    """Вызовы LLM этой системы (по имени модели) вне окон реплик пользователя — на жест их быть не должно (K4)."""
    lo, hi = run["t_start"], run["t_end"]
    wins = request_windows(run)
    return sum(1 for r in llm if r.get("type") == "llm" and r.get("model") == model
               and lo <= float(r["ts_wall"]) <= hi and not (r.get("oracle") or {}).get("input_ids")
               and not any(a <= float(r["ts_wall"]) <= b for a, b, _ in wins))


# ---- память и дескрипторы во времени (K8, H14)

def unit_series(samples: Sequence[dict], units: Sequence[str], value) -> Tuple[List[float], List[float], List[int]]:
    """Ряд суммы value(процесс) по устойчивым процессам юнитов units — прожили не меньше PERSIST_N сэмплов (короткие —
    служебные дочерние: journalctl и systemctl сторожа, проба doctor) — и метка сегмента: новый сегмент — только когда
    устойчивый процесс исчез (рестарт — омоложение: его ступень вниз в наклон не попадает). Новый процесс без
    исчезновения прежних сегмент не рвёт: его ступень — рост (утечка через накопление дочерних процессов видна). Сэмпл
    без устойчивых процессов (юнит лежит) пропускается. → (t_mono, y, seg)."""
    seen: Dict[tuple, int] = {}
    for s in samples:
        for name in units:
            for p in (s["units"].get(name) or {}).get("procs", []):
                key = (name, p.get("pid"), p.get("start"))
                seen[key] = seen.get(key, 0) + 1
    t: List[float] = []
    y: List[float] = []
    seg: List[int] = []
    prev, k = None, -1
    for s in samples:
        total, alive = 0.0, set()
        for name in units:
            for p in (s["units"].get(name) or {}).get("procs", []):
                key = (name, p.get("pid"), p.get("start"))
                if seen[key] >= PERSIST_N:
                    total += value(p)
                    alive.add(key)
        if not alive:
            continue
        if prev is None or prev - alive:
            k += 1
        prev = alive
        t.append(s["t_mono"])
        y.append(total)
        seg.append(k)
    return t, y, seg


def trend(t_mono: Sequence[float], y: Sequence[float], seg: Sequence[int], delta: Optional[float] = None,
          digits: int = 3, conf: float = 0.90) -> Optional[dict]:
    """Наклон ряда в час по сегментам жизни процессов (H14): прогрев каждого сегмента отсекает MSER-5, дальше —
    батч-средние внутри сегментов и сезонный Сен (harness.stats, ДИ уровня conf); при delta — TOST (PROTOCOL §1, K8):
    equivalent — ДИ внутри (−delta; delta), not_equivalent — нет; undetermined — при любом положении ДИ, если lag-1
    батч-средних ≥ 0,2 (ДИ недопокрывает) или прогрев какого-то сегмента не отделён (MSER на пределе: остаток прогрева
    тянет наклон вверх) — тогда ненадёжно и «тренда нет», и «растёт». beyond — весь ДИ за ±delta (при undetermined —
    справочно). None — нет numpy или ряда; decision short — сегменты короче двух батчей."""
    st = _stats()
    if st is None or len(y) < 2:
        return None
    groups: Dict[int, List[int]] = {}
    for i, g in enumerate(seg):
        groups.setdefault(g, []).append(i)
    kt: List[float] = []
    ky: List[float] = []
    kg: List[int] = []
    warm, at_limit = 0.0, 0
    for g, ix in groups.items():
        m = st.mser_truncation([y[i] for i in ix])
        if m["cut"]:
            warm += t_mono[ix[m["cut"]]] - t_mono[ix[0]]
        at_limit += m["at_limit"]
        for i in ix[m["cut"]:]:
            kt.append((t_mono[i] - t_mono[0]) / 3600.0)
            ky.append(y[i])
            kg.append(g)
    out = {"segments": len(groups), "warmup_s": round(warm), "warmup_at_limit": at_limit,
           "hours": round((t_mono[-1] - t_mono[0]) / 3600.0, 3)}
    try:
        if delta:
            r = st.tost_slope(kt, ky, delta, segments=kg)
            ci = r["ci"]
        else:
            r, ci = None, st.sen_slope_batch_means(kt, ky, conf=conf, segments=kg)
    except ValueError:
        return dict(out, decision="short")
    out.update(slope=round(ci["slope"], digits), ci90=[round(ci["ci_low"], digits), round(ci["ci_high"], digits)],
               segments_used=ci["n_segments_used"], batches=ci["n_batches"], lag1_ok=ci["lag1_ok"])
    if conf != 0.90:
        out["ci_conf"] = conf
    if r is not None:
        sure = ci["lag1_ok"] and not at_limit
        out.update(delta=round(delta, 3), decision=r["decision"] if sure else "undetermined",
                   beyond=bool(ci["ci_low"] >= delta or ci["ci_high"] <= -delta))
    return out


def sum_trend(parts: Sequence[Optional[dict]], delta: float) -> Optional[dict]:
    """Наклон суммы памяти компонентов робота: сумма наклонов компонентов (у каждого — по жизням его процессов), ДИ —
    сумма границ ДИ компонентов уровня 1 − 0,10/k (Бонферрони: общий уровень не ниже 90 %, консервативно), TOST при
    delta — по нему, «не определено» — как у trend: lag-1 или прогрев на пределе хоть у одного компонента, при любом
    положении ДИ. Сумма одним рядом не годится: её сегменты рвёт рестарт любого компонента, и в короткие куски
    попадает прогрев новых процессов. None — нет numpy; decision short — у какого-то компонента мало данных."""
    if not parts or any(p is None for p in parts):
        return None
    if any(p.get("decision") == "short" for p in parts):
        return {"decision": "short", "components": len(parts)}
    lo, hi = sum(p["ci90"][0] for p in parts), sum(p["ci90"][1] for p in parts)
    inside, reliable = -delta < lo and hi < delta, all(p["lag1_ok"] for p in parts)
    at_limit = sum(p["warmup_at_limit"] for p in parts)
    decision = ("undetermined" if not reliable or at_limit else "equivalent" if inside else "not_equivalent")
    return {"slope": round(sum(p["slope"] for p in parts), 3), "ci90": [round(lo, 3), round(hi, 3)],
            "components": len(parts), "hours": max(p["hours"] for p in parts), "lag1_ok": reliable,
            "warmup_at_limit": at_limit, "delta": round(delta, 3), "decision": decision,
            "beyond": bool(lo >= delta or hi <= -delta), "method": "sum of per-unit slopes, Bonferroni CI"}


def memory(collect: Sequence[dict], skip_s: float = 120.0, fds: Sequence[dict] = (),
           delta: float = DELTA_MIB_H) -> dict:
    """PSS по компонентам (сумма процессов юнита), МиБ: медиана в начале и в конце окна, наклон МиБ/ч МНК по сэмплам
    после skip_s (slope_mib_h — счёт до 20.10, справочно: рестарт в ряду — ступень); sen — наклон по сегментам жизни
    процессов (trend, 90 % ДИ); у _total (компоненты робота, без NOT_ROBOT) — сумма наклонов компонентов с TOST при
    delta (sum_trend): пессимистичный запас памяти ветки GNOME / время миссии (MARGIN_MIB / MISSION_H). fd — число
    открытых дескрипторов (fds.jsonl): в начале, в конце, наибольшее и наклон в час."""
    samples = [s for s in collect if s.get("type") == "sample"]
    if len(samples) < 4:
        return {}
    t_first = samples[0]["t_mono"]
    late = [s for s in samples if s["t_mono"] - t_first >= skip_s] or samples
    series: Dict[str, List[Tuple[float, float]]] = {}
    for s in late:
        total = 0.0
        for name, u in s["units"].items():
            pss = sum(p.get("pss_kib", 0) for p in u.get("procs", [])) / MIB
            series.setdefault(name, []).append((s["t_mono"], pss))
            if name not in NOT_ROBOT:
                total += pss
        series.setdefault("_total", []).append((s["t_mono"], total))
    robot = sorted({n for s in samples for n in s["units"] if n not in NOT_ROBOT})
    fd_samples = [s for s in fds if s.get("type") == "sample"]
    out = {}

    def pss(p):
        return p.get("pss_kib", 0) / MIB
    bonferroni = 1.0 - 0.10 / max(1, len(robot))
    for name, pts in series.items():
        k = max(1, len(pts) // 10)
        first = sorted(v for _, v in pts[:k])[k // 2]
        last = sorted(v for _, v in pts[-k:])[k // 2]
        out[name] = {"pss_mib_start": round(first, 1), "pss_mib_end": round(last, 1),
                     "slope_mib_h": slope(pts), "hours": round((pts[-1][0] - pts[0][0]) / 3600, 3)}
        units = robot if name == "_total" else [name]
        if name == "_total":
            sen = sum_trend([trend(*unit_series(samples, [u], pss), conf=bonferroni) for u in robot], delta)
        else:
            sen = trend(*unit_series(samples, units, pss))
        if sen is not None:
            out[name]["sen"] = sen
        if fd_samples:
            t, y, seg = unit_series(fd_samples, units, lambda p: p.get("fds", 0))
            if y:
                out[name]["fd"] = {"start": y[0], "end": y[-1], "max": max(y), "trend": trend(t, y, seg, digits=2)}
    return out


def slope(pts: Sequence[Tuple[float, float]]) -> Optional[float]:
    if len(pts) < 2:
        return None
    n = len(pts)
    mx = sum(t for t, _ in pts) / n
    my = sum(v for _, v in pts) / n
    den = sum((t - mx) ** 2 for t, _ in pts)
    if den == 0:
        return None
    return round(sum((t - mx) * (v - my) for t, v in pts) / den * 3600, 3)


def latency(per: Sequence[dict], key: str, rtf_min: float = 0.95) -> dict:
    """Задержка t0 → key (cmd — первая ненулевая /cmd_vel, motion — начало движения) по жестам наблюдателя: без
    ранних (реакция раньше t0 — perception разрезал длинный жест и поехал по первой части: «задержки» нет) — K1/K2,
    с ранговым ДИ p50 и p95 (quantiles_ci); ранние — отдельно. Жесты при RTF < rtf_min не входят (как у наблюдателя)."""
    vals = [p for p in per if p.get("kind") == "gesture" and key in (p.get("lat") or {})
            and (p.get("rtf") is None or p["rtf"] >= rtf_min)]
    early = [p["lat"][key] for p in vals if p.get("early")]
    return {"on_time": quantiles_ci([p["lat"][key] for p in vals if not p.get("early")]), "early": quantiles(early)}


def travel(truth: Sequence[dict], safety: Sequence[dict], gap_s: float = 2.0) -> dict:
    """Пробег и время в движении по истине: перерывы записи дольше gap_s (пауза ВМ, мир двойника поднимается заново)
    не в счёт, скачок позы быстрее TELEPORT_M_S — перенос робота оператором или новый мир — не пробег (teleports);
    доли времени в состояниях safety_gate (строка safety.jsonl действует до следующей)."""
    dist = moving_s = jumps = 0.0
    n_jumps = 0
    for a, b in zip(truth, truth[1:]):
        dt = b["m"] - a["m"]
        if dt > gap_s or a.get("x") is None or b.get("x") is None:
            continue
        step = ((b["x"] - a["x"]) ** 2 + (b["y"] - a["y"]) ** 2) ** 0.5
        if step > TELEPORT_M_S * dt + TELEPORT_EPS_M:
            n_jumps += 1
            jumps += step
            continue
        dist += step
        if moving(a):
            moving_s += dt
    spans: Dict[str, float] = dict.fromkeys(SAFETY_STATES, 0.0)
    for a, b in zip(safety, safety[1:]):
        dt = b["m"] - a["m"]
        if a.get("state") in spans and dt <= gap_s:
            spans[a["state"]] += dt
    total = sum(spans.values())
    span = truth[-1]["m"] - truth[0]["m"] if len(truth) > 1 else 0.0
    return {"distance_m": round(dist, 2), "moving_s": round(moving_s, 1),
            "moving_share": round(moving_s / span, 4) if span else None,
            "teleports": {"n": n_jumps, "m": round(jumps, 2)},
            "safety_share": {k: round(v / total, 4) for k, v in spans.items()} if total else {}}


def k6_outage(run_dir: str, truth: Sequence[dict], gap_s: float = 2.0) -> dict:
    """Эпизоды K6 наблюдателя (движение без свежей команды, кроме торможения после нуля), пришедшиеся на перерыв
    истины двойника дольше gap_s: мир не шёл (смерть базы — Gazebo заново), трекер движения держит последнее «едет»,
    а робот по истине стоит. Интервалы — тем же разбором наблюдателя (neurobot.sim.gestures.analyze) по events.jsonl
    прогона. {n, total_s, episodes: [{a_s, b_s, cause}]} от якоря; None — нет записи наблюдателя или якоря."""
    events, anchor_path = os.path.join(run_dir, "events.jsonl"), os.path.join(run_dir, "anchor.json")
    if not (os.path.exists(events) and os.path.exists(anchor_path)):
        return None
    from neurobot.sim.gestures import analyze
    from neurobot.sim.gestures.clock import read_anchor
    anchor = read_anchor(anchor_path, live=False)
    sessions, _ = analyze.select_sessions(analyze.read_events(events), anchor)
    cfg = analyze.load_config()
    unsafe = [u for evs in sessions if evs for u in analyze._session(evs, cfg)["unsafe"]
              if u["cause"] != "braking_after_zero"]
    gaps = [(a["m"], b["m"]) for a, b in zip(truth, truth[1:]) if b["m"] - a["m"] > gap_s]
    hit = [u for u in unsafe if any(lo < u["b"] and u["a"] < hi for lo, hi in gaps)]
    return {"n": len(hit), "total_s": round(sum(u["b"] - u["a"] for u in hit), 2),
            "episodes": [{"a_s": round(u["a"] - anchor.t_mono, 1), "b_s": round(u["b"] - anchor.t_mono, 1),
                          "cause": u["cause"]} for u in hit]}


def move_outcome(a: dict, moved: bool) -> str:
    """Исход реплики «вперёд»: moved — робот поехал; refused — не поехал, агент сообщил об отказе safety_gate с
    причиной (blocked, estop); false_ok — не поехал, а агент ответил «готово» (инструмент ok: true) — неправда о
    роботе; unconfirmed — инструмент честно не смог подтвердить толчок (safety_gate его не видел); no_answer — сбой
    агентского пути или ошибка инструмента."""
    if moved:
        return "moved"
    if a.get("ok") and a.get("refused"):
        return "refused"
    if a.get("ok"):
        return "false_ok"
    if any(re.search(r'"status":\s*"unconfirmed"', t.get("content", "")) for t in a.get("tools") or []):
        return "unconfirmed"
    return "no_answer"


MOVE_OUTCOMES = ("moved", "refused", "false_ok", "unconfirmed", "no_answer")


def move_requests(run: dict, truth: Sequence[dict]) -> dict:
    """Реплики «вперёд» (move_outcome): выполнена, если робот поехал (сдвиг по истине ≥ MOVE_MIN_M за ход и
    MOVE_TAIL_S после) или агент сообщил о запрете safety_gate с причиной; ok: true без движения — не успех."""
    rows = []
    for a in run.get("agent", []):
        if a.get("request") != "move":
            continue
        hi = a["m"] + (a.get("latency_s") or 0.0) + MOVE_TAIL_S
        pts = [r for r in truth if a["m"] <= r["m"] <= hi and r.get("x") is not None]
        shift = max((((r["x"] - pts[0]["x"]) ** 2 + (r["y"] - pts[0]["y"]) ** 2) ** 0.5 for r in pts), default=0.0)
        outcome = move_outcome(a, shift >= MOVE_MIN_M)
        rows.append({"n": a["n"], "moved_m": round(shift, 3), "outcome": outcome, "refused": a.get("refused") or [],
                     "success": outcome in ("moved", "refused")})
    out = {k: sum(1 for r in rows if r["outcome"] == k) for k in MOVE_OUTCOMES}
    out.update(n=len(rows), success=sum(r["success"] for r in rows), rows=rows)
    return out


def safety_stops(per: Dict[int, dict], segs: Sequence[dict], safety: Sequence[dict], t_mono0: float) -> dict:
    """Реакции «оборвана» и «нет» (A1), в окне манёвра которых safety_gate был в blocked или estop: остановила защита
    (препятствие, слепой сектор, геозабор), а не отказ — по причинам safety_state."""
    out: Dict[str, dict] = {}
    for s in segs:
        p = per.get(s["seg"], {})
        if s["kind"] != "gesture" or p.get("reaction") not in LOST_REACTIONS:
            continue
        a, b = t_mono0 + s["t0"], t_mono0 + s["t0"] + REACTION_WINDOW_S
        hits = [r for r in safety if a <= r["m"] <= b and r.get("state") in ("blocked", "estop")]
        if hits:
            reasons = sorted({str(x).split(":")[0] for r in hits for x in (r.get("reasons") or [])})
            out[str(s["seg"])] = {"reaction": p["reaction"], "reasons": reasons}
    return {"n": len(out), "by_seg": out}


def dead_ends(safety: Sequence[dict], truth: Sequence[dict], segs: Sequence[dict], t_mono0: float,
              min_s: float = DEAD_END_S) -> dict:
    """Тупики защиты: safety_gate держит blocked без перерыва не меньше min_s, робот по истине всё это время стоит, и
    в это время были жесты — ни один не вывел его (например, у ножки стула запрещены и путь вперёд, и поворот на
    месте). Выйти робот может только с помощью человека (или нового мира двойника после смерти базы); оператор прогона
    тупики не чинит — они видны здесь. {n, total_s, gestures, spans: [{at_s, s, reasons, gestures}]}."""
    spans: List[list] = []
    for a, b in zip(safety, safety[1:]):
        if a.get("state") != "blocked":
            continue
        reasons = {str(x).split(":")[0] for x in a.get("reasons") or []}
        if spans and spans[-1][1] == a["m"]:
            spans[-1][1] = b["m"]
            spans[-1][2] |= reasons
        else:
            spans.append([a["m"], b["m"], reasons])
    out = []
    for lo, hi, reasons in spans:
        if hi - lo < min_s or any(lo <= r["m"] <= hi and moving(r) for r in truth):
            continue
        hit = [s["seg"] for s in segs if s["kind"] == "gesture" and lo <= t_mono0 + s["t0"] <= hi]
        if hit:
            out.append({"at_s": round(lo - t_mono0, 1), "s": round(hi - lo, 1), "reasons": sorted(reasons),
                        "gestures": hit})
    return {"n": len(out), "total_s": round(sum(x["s"] for x in out), 1),
            "gestures": sum(len(x["gestures"]) for x in out), "spans": out}


def escapes(executor: Sequence[dict], safety: Sequence[dict], t_mono0: float) -> dict:
    """Тупики safety_gate и выход из них (повторная приёмка 18.10): эпизоды по safety.jsonl, где поворот на месте
    заденет (touch), а вперёд нельзя (blocked) или только подползти к препятствию (approach, obstacle, cliff), — как их
    видит исполнитель (guard.dead_end); отъезды исполнителя назад — по его событиям (executor.jsonl: escape start,
    task_end escape). Эпизод вышел отъездом, если отъезд начался в нём. reverse_limit — назад уже нельзя, выхода нет.
    {episodes, escaped, max_s, total_s, starts, outcomes: {status: n}, spans: [{at_s, s, escaped, reasons}]}."""
    spans: List[list] = []
    for a, b in zip(safety, safety[1:]):
        reasons = {str(x).split(":")[0] for x in a.get("reasons") or []}
        near = a.get("state") == "blocked" or (a.get("state") == "slow" and reasons & {"approach", "obstacle", "cliff"})
        if "touch" not in reasons or not near:
            continue
        if spans and spans[-1][1] == a["m"]:
            spans[-1][1] = b["m"]
            spans[-1][2] |= reasons
        else:
            spans.append([a["m"], b["m"], reasons])
    starts = [e["m"] for e in executor if e.get("type") == "escape" and e.get("status") == "start"]
    outcomes: Dict[str, int] = {}
    for e in executor:
        if e.get("type") == "task_end" and e.get("task") == "escape":
            outcomes[e["status"]] = outcomes.get(e["status"], 0) + 1
    out = [{"at_s": round(lo - t_mono0, 1), "s": round(hi - lo, 1), "escaped": any(lo <= t <= hi for t in starts),
            "reasons": sorted(reasons)} for lo, hi, reasons in spans]
    return {"episodes": len(out), "escaped": sum(1 for x in out if x["escaped"]),
            "max_s": max((x["s"] for x in out), default=0.0), "total_s": round(sum(x["s"] for x in out), 1),
            "starts": len(starts), "outcomes": outcomes, "spans": out}


def reaction_breakdown(per: Dict[int, dict], segs: Sequence[dict], stopped: dict) -> dict:
    """Разбор «верной реакции» (наблюдатель: класс распознан верно и манёвр демо исполнен целиком) по классу решающего
    сообщения perception: ok — верный, wrong — другой класс, none — сообщения нет или «не распознано» (−1), unknown —
    система класс не публикует (демо A0: судить можно только по манёвру). В каждой группе — реакции; safety — из
    оборванных и не начатых при верном классе сколько остановил safety_gate (stopped_by_safety) и по каким причинам."""
    published = any(p.get("rec") for p in per.values())
    groups: Dict[str, Dict[str, int]] = {}
    cut = {"interrupted": 0, "none": 0}
    reasons: Dict[str, int] = {}
    gestures = [s for s in segs if s["kind"] == "gesture"]
    for s in gestures:
        p = per.get(s["seg"], {})
        rec = p.get("rec") or {}
        key = "unknown" if not published else "none" if rec.get("cls") in (None, -1) else \
            "ok" if rec["cls"] == p.get("cls") else "wrong"
        reaction = p.get("reaction") or "unobserved"
        g = groups.setdefault(key, {})
        g[reaction] = g.get(reaction, 0) + 1
        hit = (stopped.get("by_seg") or {}).get(str(s["seg"]))
        if key == "ok" and hit and reaction in cut:
            cut[reaction] += 1
            for r in hit["reasons"]:
                reasons[r] = reasons.get(r, 0) + 1
    return {"gestures": len(gestures), "by_class": groups, "safety": dict(cut, reasons=reasons)}


def drift(per: Sequence[dict], key: str = "cmd") -> dict:
    """Дрейф задержки t0 → /cmd_vel: p50 и p95 первой и последней трети жестов прогона."""
    vals = [p["lat"][key] for p in sorted(per, key=lambda p: p["seg"])
            if p.get("kind") == "gesture" and key in (p.get("lat") or {})]
    third = len(vals) // 3
    if third < 3:
        return {"n": len(vals)}
    return {"n": len(vals), "first": quantiles(vals[:third]), "last": quantiles(vals[-third:])}


def pauses(events: Sequence[dict], t_mono0: float) -> List[dict]:
    """Остановки ВМ (сон Mac): между соседними событиями стенные часы ушли вперёд монотонных больше PAUSE_S. Монотонные
    часы ВМ на паузе стоят, поэтому таймлайн и метрики по ним паузы не видят; после неё у узлов ROS скачок стенного
    времени."""
    out = []
    for a, b in zip(events, events[1:]):
        jump = (b["t"] - a["t"]) - (b["m"] - a["m"])
        if jump > PAUSE_S:
            out.append({"after_s": round(a["m"] - t_mono0, 1), "before_s": round(b["m"] - t_mono0, 1),
                        "wall_jump_s": round(jump, 1)})
    return out


def k3_honest(faults: Sequence[dict], extra: Optional[Sequence[dict]], per: Dict[int, dict],
              segs: Sequence[dict]) -> dict:
    """Сводка видимости перезапусков (H13): внесённых отказов, видимых честно (с границей: 0 из n — ≈ 3/n, иначе ДИ
    Клоппера–Пирсона) и по счёту до 20.10, причины (у отказа их может быть несколько), сколько отказов пришлось на
    манёвр (расписание ставит отказ через 1 с после жеста — нарочно жёстко), рестарты вне расписания (None — журнала
    нет) и SLI «верная реакция не позже 3 с» в окнах отказов пути жеста и вне их (разница — потери от отказов без
    ручного разбора причин; SRE Workbook)."""
    done = [f for f in faults if not f.get("skipped")]
    rows = done + list(extra or [])
    by: Dict[str, int] = {}
    for f in rows:
        for c in f.get("codes") or []:
            by[c] = by.get(c, 0) + 1
    spans = [maneuver_span(s, per.get(s["seg"], {})) for s in segs if s["kind"] == "gesture"]
    spans = [x for x in spans if x]
    windows = [(f["at_s"], float("inf") if f.get("unavailable_s") is None else f["at_s"] + f["unavailable_s"])
               for f in rows if f.get("path") == "жест"]
    sli = {"in_windows": [0, 0], "outside": [0, 0]}
    for s in segs:
        p = per.get(s["seg"], {})
        if s["kind"] != "gesture" or p.get("reaction") in (None, "unobserved"):
            continue
        key = "in_windows" if any(s["t_start"] <= b and s["t0"] + REACTION_S >= a for a, b in windows) else "outside"
        sli[key][0] += p["reaction"] == "correct" and not p.get("late")
        sli[key][1] += 1
    n_vis = sum(1 for f in done if f["visible"])
    return {"faults": len(done), "visible": n_vis, "visible_legacy": sum(1 for f in done if f.get("visible_legacy")),
            "bound": zero_bound(n_vis, len(done)), "by_reason": by,
            "on_maneuver": sum(1 for f in done if any(lo <= f["at_s"] <= hi for lo, hi in spans)),
            "unscheduled": None if extra is None else {
                "n": len(extra), "visible": sum(1 for f in extra if f["visible"]),
                "rows": [{k: f.get(k) for k in ("fault", "at_s", "detected_by", "ttd_s", "unavailable_s", "visible",
                                                "why")} for f in extra]},
            "sli_correct_3s": {k: {"k": v[0], "n": v[1], "bound": zero_bound(v[0], v[1])} for k, v in sli.items()}}


# ---- сборка

MODELS = {"a0": "openai/gpt-4o-mini", "a1": "gpt-4o-mini"}


def summarize(run_dir: str, truth_path: Optional[str] = None, observer_dir: Optional[str] = None) -> dict:
    """observer_dir — каталог с summary.json и gestures.jsonl разбора наблюдателя, сделанного заново
    (python -m neurobot.sim.gestures.analyze по events.jsonl прогона); по умолчанию — разбор из прогона."""
    run = load_json(os.path.join(run_dir, "run.json")) or {}
    if "anchor" not in run:
        return {"run_dir": run_dir, "error": run.get("error", "нет run.json или прогон не дошёл до якоря")}
    truth_path = truth_path or os.path.join(run_dir, "truth_schedule.jsonl")
    t_end_rel = run["events"][-1]["m"] - run["anchor"]["t_mono"]
    segs = [s for s in segments(truth_path) if s["t_leave"] <= t_end_rel]   # дошедшие до конца записи
    obs_dir = observer_dir or run_dir
    obs = load_json(os.path.join(obs_dir, "summary.json")) or {}
    per = {p["seg"]: p for p in load_jsonl(os.path.join(obs_dir, "gestures.jsonl")) if "seg" in p}
    probe = load_jsonl(os.path.join(run_dir, "probe.jsonl"))
    truth = load_jsonl(os.path.join(run_dir, "truth.jsonl"))
    safety = load_jsonl(os.path.join(run_dir, "safety.jsonl"))
    t_mono0 = run["anchor"]["t_mono"]
    fps = 10.0
    reacted = {n for n, p in per.items() if p.get("reaction") in ("correct", "wrong", "interrupted")}
    lost = deaf_gestures(segs, probe, t_mono0, fps, truth, safety, reacted)
    deaf = lost["deaf"]
    stopped = safety_stops(per, segs, safety, t_mono0)
    llm = load_jsonl(os.path.join(run_dir, "mockllm.jsonl"))
    events = run["events"]
    ops = [e for e in events if e.get("kind") == "operator"]
    lat = obs.get("latency_s", {})
    gestures = [s for s in segs if s["kind"] == "gesture"]
    logs = [os.path.join(run_dir, "logs", n) for n in ("journal.json", "journal.txt")]
    restarts = journal_restarts(next((x for x in logs if os.path.exists(x)), logs[0]), run)
    faults = fault_rows(run, segs, per, deaf, set(lost["busy"]) | {int(k) for k in stopped["by_seg"]}, safety, truth,
                        busy=lost["busy"], stopped=stopped, probe=probe, restarts=restarts)
    extra = None if restarts is None else unscheduled_rows(run, segs, per, deaf, restarts, faults, safety, truth,
                                                           lost["busy"], stopped)
    honest = k3_honest(faults, extra, per, segs)
    model = MODELS[run["system"]]
    return {
        "system": run["system"], "label": run["label"], "profile": run["plan"]["profile"],
        "world": os.path.basename(run.get("world") or "hall.sdf").rsplit(".", 1)[0],
        "no_return": run.get("no_return", "nan"),
        "duration_s": round(events[-1]["m"] - events[0]["m"], 1),
        "wall_duration_s": round(run["t_end"] - run["t_start"], 1),
        "vm_pauses": pauses(events, t_mono0),
        "segments": {"gestures": len(gestures), "negatives": len(segs) - len(gestures)},
        "K1_t0_to_cmd_s": latency(list(per.values()), "cmd")["on_time"],
        "K2_t0_to_motion_s": latency(list(per.values()), "motion")["on_time"],
        "early_t0_to_cmd_s": latency(list(per.values()), "cmd")["early"],
        "K1_observer_all_s": lat.get("cmd"), "latency_excluded_rtf": lat.get("excluded_rtf"),
        "reactions": obs.get("reactions"), "late": obs.get("late"), "visible_failures_observer":
            obs.get("visible_failures"), "recognition": obs.get("recognition"),
        "K3": {"deaf_gestures": deaf, "deaf_gestures_n": len(deaf), "busy_gestures": lost["busy"],
               "latched_gestures": lost["latched"], "operator_interventions": len(ops),
               "operator_safety": sum(1 for o in ops if o["action"] in SAFETY_ACTIONS),
               "operator": [{"at_s": round(o["m"] - t_mono0, 1), "action": o["action"], "what": o.get("what"),
                             "why": o.get("why") or o.get("reasons")} for o in ops],
               "visible_restarts": sum(1 for f in faults + (extra or []) if f.get("visible")),
               "invisible_restarts": sum(1 for f in faults + (extra or []) if f.get("visible") is False),
               "visible_restarts_legacy": honest["visible_legacy"], "honest": honest,
               "deaf_probe": (obs.get("deaf") or {}).get("probe"),
               "deaf_probe_standing": (obs.get("deaf") or {}).get("probe_standing"),
               "deaf_health": (obs.get("deaf") or {}).get("health")},
        "K4": {"llm_calls_outside_requests": llm_outside_requests(run, llm, model),
               "agent": agent_summary(run, llm, model)},
        "K5_faults": faults, "K5_unscheduled": extra, "K5_by_type": k5_summary(faults),
        "stopped_by_safety": stopped,
        "reaction_breakdown": reaction_breakdown(per, segs, stopped),
        "dead_ends": dead_ends(safety, truth, segs, t_mono0),
        "escapes": escapes(load_jsonl(os.path.join(run_dir, "executor.jsonl")), safety, t_mono0),
        "K6_unsafe_no_fresh_cmd": obs.get("unsafe_no_fresh_cmd"),
        "K6_world_outage": k6_outage(run_dir, truth),
        "K7": obs.get("k7"), "false_actions": obs.get("false_actions"),
        "touches": contacts(truth),
        "travel": travel(truth, safety),
        "move_requests": move_requests(run, truth),
        "K8": {"memory": memory(load_jsonl(os.path.join(run_dir, "collect.jsonl")),
                                fds=load_jsonl(os.path.join(run_dir, "fds.jsonl"))),
               "delta_mib_h": round(DELTA_MIB_H, 3), "drift_cmd": drift(list(per.values()))},
        "restarts": run.get("restarts"),
        "rtf_observer": obs.get("rtf"),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m neurobot.sim.runs.metrics", description=__doc__.split("\n\n")[0])
    p.add_argument("run_dir", help="каталог прогона (runner.py)")
    p.add_argument("--truth", default="", help="истина расписания (по умолчанию RUN_DIR/truth_schedule.jsonl)")
    p.add_argument("--observer", default="", help="каталог разбора наблюдателя заново (summary.json, gestures.jsonl); "
                   "по умолчанию — RUN_DIR")
    p.add_argument("--out", default="", help="файл JSON (по умолчанию stdout)")
    a = p.parse_args(argv)
    doc = summarize(a.run_dir, a.truth or None, a.observer or None)
    text = json.dumps(doc, ensure_ascii=False, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 1 if "error" in doc else 0


if __name__ == "__main__":
    sys.exit(main())
