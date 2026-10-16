"""CLI daygen.

  python -m bench.daygen gen --seed 1 --profile M+ --split tune --out day.jsonl
  python -m bench.daygen batch --seed0 1 --n 30 --profile M+ --split tune --outdir experiments/days/tune_Mplus
  (--split обязателен для CASAS: tune | holdout | any; any — явно разрешить дома обеих частей)
  python -m bench.daygen stats day1.jsonl day2.jsonl ...
  python -m bench.daygen timeline day.jsonl [--sensors]
  python -m bench.daygen casas-check | casas-extract | massive-build
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import statistics as st
import sys
from collections import Counter
from pathlib import Path

from . import casas, massive
from .generator import PROFILES, DayConfig, generate_day, read_day, write_day


def _add_day_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--profile", default="M+", choices=sorted(PROFILES))
    p.add_argument("--source", default="casas", choices=["casas", "synthetic"],
                   help="трасса датчиков: подмножество CASAS или синтетический дом")
    p.add_argument("--home", help="дом CASAS (hh101 …); по умолчанию — по расписанию части (schedule_pair)")
    p.add_argument("--split", choices=["tune", "holdout", "any"],
                   help="часть домов CASAS (обязательна без --home; any — любые дома, в т. ч. отложенные); "
                        "она же — соль потоков ГСЧ, поэтому нагрузка tune и holdout при одном seed различна")
    p.add_argument("--date", help="сутки CASAS (YYYY-MM-DD); по умолчанию — по расписанию части")
    p.add_argument("--lang", default="ru", choices=["ru", "en", "mix"])
    p.add_argument("--window", default="07:00-23:00", help="окно дня ЧЧ:ММ-ЧЧ:ММ")
    p.add_argument("--rate-scale", type=float, default=1.0,
                   help="множитель профиля (реплик на окно = команд/сут × множитель), напр. для окна-доли суток")
    p.add_argument("--question-share", type=float, default=0.25)
    p.add_argument("--trap-share", type=float, default=0.10)
    p.add_argument("--synthetic-situations", type=int, default=2)
    p.add_argument("--deadline", type=float, default=300.0, help="дедлайн реакции на ситуацию, с")
    p.add_argument("--door-open-min", type=float, default=10.0)
    p.add_argument("--night-start", default="22:00")
    p.add_argument("--no-motion-h", type=float, default=2.0)
    p.add_argument("--absence-h", type=float, default=4.0)
    p.add_argument("--awake", default="07:00-21:00",
                   help="часы бодрствования для no_motion_home ЧЧ:ММ-ЧЧ:ММ (тишина вне их — сон, не ситуация)")
    p.add_argument("--no-manual-switch", action="store_true", help="без ручных переключений света жителем")
    p.add_argument("--subset-dir", help="каталог подмножества CASAS (по умолчанию data/casas/subset)")
    p.add_argument("--templates", help="файл шаблонов MASSIVE (по умолчанию data/massive/iot_templates.json)")


def _cfg(a: argparse.Namespace, seed: int) -> DayConfig:
    w = a.window.split("-")
    if len(w) != 2:
        raise SystemExit("--window: ЧЧ:ММ-ЧЧ:ММ")
    if a.source == "casas" and not a.split and not a.home:
        raise SystemExit("--split обязателен для дней CASAS: tune | holdout | any (any — дома обеих частей)")
    return DayConfig(seed=seed, profile=a.profile, source=a.source, home=a.home, split=a.split, date=a.date,
                     lang=a.lang, window=(w[0], w[1]), rate_scale=a.rate_scale,
                     question_share=a.question_share, trap_share=a.trap_share,
                     synthetic_situations=a.synthetic_situations, deadline_s=a.deadline,
                     door_open_min=a.door_open_min, night_start=a.night_start, no_motion_h=a.no_motion_h,
                     absence_h=a.absence_h, awake=tuple(a.awake.split("-")), manual_switch=not a.no_manual_switch,
                     subset_dir=a.subset_dir,
                     templates=a.templates)


def _summary(header: dict, events: list[dict]) -> str:
    c = header["counts"]
    return (f"{header['home']} {header['date']} ({header['split']}) {header['profile']}: "
            f"датчики {c['sensor']}, команды {c['user_cmd']}, вопросы {c['question']}, ситуации {c['situation']}")


def cmd_gen(a: argparse.Namespace) -> int:
    header, events = generate_day(_cfg(a, a.seed))
    if a.out == "-":
        from .generator import dumps
        sys.stdout.write(dumps(header) + "\n" + "".join(dumps(e) + "\n" for e in events))
    else:
        write_day(a.out, header, events)
        print(_summary(header, events), "->", a.out, file=sys.stderr)
    return 0


def cmd_batch(a: argparse.Namespace) -> int:
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    tag = a.profile.replace("+", "plus")
    seen: Counter[tuple[str, str]] = Counter()
    for i in range(a.n):
        seed = a.seed0 + i
        header, events = generate_day(_cfg(a, seed))
        path = out / f"day_{a.split or header['split']}_{tag}_{seed:05d}.jsonl"
        write_day(path, header, events)
        seen[(header["home"], header["date"])] += 1
        print(_summary(header, events), "->", path, file=sys.stderr)
    rep = {k: v for k, v in seen.items() if v > 1}
    if rep and a.source == "casas":
        print(f"внимание: {sum(rep.values()) - len(rep)} повторов (дом, сутки) — серия длиннее расписания части: "
              + ", ".join(f"{h} {d}×{n}" for (h, d), n in sorted(rep.items())), file=sys.stderr)
    return 0


def _ci(xs: list[float]) -> tuple[float, float, float]:
    m = st.mean(xs)
    if len(xs) < 2:
        return m, float("nan"), float("nan")
    h = 1.96 * st.stdev(xs) / math.sqrt(len(xs))
    return m, m - h, m + h


def cmd_stats(a: argparse.Namespace) -> int:
    files = sorted({f for pat in a.files for f in glob.glob(pat)})
    if not files:
        raise SystemExit("нет файлов")
    rows: list[dict[str, float]] = []
    profiles: Counter[str] = Counter()
    for f in files:
        h, ev = read_day(f)
        profiles[h["profile"]] += 1
        c = Counter(e["kind"] for e in ev)
        cls = Counter(e.get("class") for e in ev if e["kind"] in ("user_cmd", "question"))
        sit = Counter(e["name"] for e in ev if e["kind"] == "situation")
        row = {"utterances": c["user_cmd"] + c["question"], "user_cmd": c["user_cmd"], "question": c["question"],
               "sensor": c["sensor"], "situation": c["situation"],
               **{f"class:{k}": v for k, v in cls.items()}, **{f"situation:{k}": v for k, v in sit.items()}}
        sessions = {e["session"] for e in ev if e["kind"] in ("user_cmd", "question")}
        row["sessions"] = len(sessions)
        ks = [e["k"] for e in ev if e["kind"] in ("user_cmd", "question")]
        row["k_total"] = sum(ks)   # вызовов LLM на день по модели VisionClaw (E[k] ≈ 2,9 на реплику)
        rows.append(row)
    keys = sorted({k for r in rows for k in r})
    res = {k: dict(zip(("mean", "ci95_lo", "ci95_hi"), _ci([float(r.get(k, 0)) for r in rows]), strict=True))
           for k in keys}
    out = {"n_days": len(files), "profiles": dict(profiles), "per_day": res}
    if len(profiles) == 1:
        p = next(iter(profiles))
        out["expected_utterances_per_day"] = PROFILES[p][0]
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def cmd_timeline(a: argparse.Namespace) -> int:
    """Читаемая лента дня: реплики, ответы, ситуации (и датчики с --sensors)."""
    h, ev = read_day(a.file)
    print(_summary(h, ev))
    for e in ev:
        t = f"{int(e['t']) // 3600:02d}:{int(e['t']) % 3600 // 60:02d}:{int(e['t']) % 60:02d}"
        if e["kind"] == "sensor":
            if a.sensors:
                print(f"{t}  ·  {e['device']}.{e['property']} = {e['value']}  ({e['source']})")
        elif e["kind"] == "user_cmd":
            calls = "; ".join(f"{c['name']}({json.dumps(c['args'], ensure_ascii=False)})" for c in e["expect"]["calls"])
            print(f"{t}  {e['id']:>4} [{e['class']}] «{e['text']}» -> {calls}")
        elif e["kind"] == "question":
            a_ = e["answer"]
            print(f"{t}  {e['id']:>4} [{e['class']}] «{e['text']}» -> {a_['value']} {a_['unit']}")
        else:
            print(f"{t}  {e['id']:>4} [ситуация] {e['name']} (дедлайн {e['deadline_s']:.0f} с)")
    return 0


def cmd_casas_check(a: argparse.Namespace) -> int:
    ok, msg = casas.check_zip(Path(a.zip))
    print(("OK: " if ok else "ОШИБКА: ") + msg)
    return 0 if ok else 1


def cmd_casas_extract(a: argparse.Namespace) -> int:
    ok, msg = casas.check_zip(Path(a.zip))
    if not ok:
        print("архив непригоден:", msg, file=sys.stderr)
        return 1
    idx = casas.extract_subset(Path(a.zip), Path(a.out), seed=a.split_seed)
    for h, m in idx["homes"].items():
        print(f"{h}: {m['split']:7s} суток {len(m['days']):3d} (пригодны {len(m['eligible_days'])}), "
              f"записей {m['n_records']}", file=sys.stderr)
    return 0


def cmd_massive_build(a: argparse.Namespace) -> int:
    doc = massive.build(Path(a.src), Path(a.out))
    print(f"шаблонов {doc['n_templates']} из {doc['n_source_pairs']} пар, исключено {len(doc['excluded'])}",
          file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.daygen", description="Генератор «рабочего дня» EdgeCtx")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen", help="сгенерировать один день")
    g.add_argument("--seed", type=int, required=True)
    g.add_argument("--out", required=True, help="файл JSONL или - (stdout)")
    _add_day_args(g)
    g.set_defaults(fn=cmd_gen)
    b = sub.add_parser("batch", help="серия дней с seed0, seed0+1, …")
    b.add_argument("--seed0", type=int, required=True)
    b.add_argument("--n", type=int, required=True)
    b.add_argument("--outdir", required=True)
    _add_day_args(b)
    b.set_defaults(fn=cmd_batch)
    s = sub.add_parser("stats", help="частоты событий по набору дней (среднее и 95 %% ДИ на сутки)")
    s.add_argument("files", nargs="+")
    s.set_defaults(fn=cmd_stats)
    tl = sub.add_parser("timeline", help="читаемая лента дня")
    tl.add_argument("file")
    tl.add_argument("--sensors", action="store_true", help="показывать и события датчиков")
    tl.set_defaults(fn=cmd_timeline)
    c = sub.add_parser("casas-check", help="проверить архив CASAS (аналог unzip -t)")
    c.add_argument("--zip", default=str(casas.DEFAULT_ZIP))
    c.set_defaults(fn=cmd_casas_check)
    e = sub.add_parser("casas-extract", help="извлечь подмножество 14 домов в data/casas/subset")
    e.add_argument("--zip", default=str(casas.DEFAULT_ZIP))
    e.add_argument("--out", default=str(casas.DEFAULT_SUBSET))
    e.add_argument("--split-seed", type=int, default=casas.SPLIT_SEED)
    e.set_defaults(fn=cmd_casas_extract)
    m = sub.add_parser("massive-build", help="собрать шаблоны команд MASSIVE в data/massive")
    m.add_argument("--src", default=str(massive.DEFAULT_SRC))
    m.add_argument("--out", default=str(massive.DEFAULT_OUT))
    m.set_defaults(fn=cmd_massive_build)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
