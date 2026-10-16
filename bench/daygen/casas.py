"""Адаптер трасс CASAS: разбор исходного CSV, компактное подмножество 14 домов, разбиение tune/holdout.

Источник: CASAS Smart Home dataset — free living, motion, door, activity labels (D. Cook, Zenodo,
DOI 10.5281/zenodo.15708568, CC BY 4.0), файл ``labeled_data.zip``, внутри ``labeled/<дом>.csv``.

Формат строки исходника (проверено на архиве 2025-06-21)::

    2012-07-20,10:38:54.512364,OutsideDoor,ON,Step_Out="begin"
    дата,время,датчик(=локация),сообщение[,активность]

* датчик — имя локации (Bathroom, Kitchen, LivingRoom, OutsideDoor, ...), а не id датчика;
* сообщение — ON/OFF (движение), OPEN/CLOSE (контакт двери/дверцы), целое 0–100 (у части домов;
  смысл в описании набора не раскрыт — в день не попадает, но сохраняется в подмножестве);
* активность — ``Имя="begin"`` / ``Имя="end"`` или просто ``Имя`` (внутри активности);
* у части домов строки оканчиваются на ``\\r``; встречаются пустые поля и опечатки (``ONf``).

Компактное подмножество (``data/casas/subset/<дом>.csv.gz``): заголовок ``date,t,sensor,value,activity``,
``t`` — секунды от полуночи с точностью 1 мс, ``activity`` — ``Имя:begin`` / ``Имя:end`` или пусто
(«внутри активности» отбрасывается, восстанавливается по begin/end). Только полные сутки
(первый и последний день записи отброшены, как в research/new_topic/GAP1_artifacts/casas_rates.py).
gzip пишется с mtime=0, поэтому извлечение воспроизводится байт в байт.
"""
from __future__ import annotations

import csv
import dataclasses
import gzip
import hashlib
import io
import json
import os
import random
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ZIP = PROJECT_ROOT / "data" / "casas" / "labeled_data.zip"
DEFAULT_SUBSET = PROJECT_ROOT / "data" / "casas" / "subset"

# 14 домов анализа GAP1 (research/new_topic/GAP1_artifacts/casas_rates.py): дом -> число жителей.
HOMES: dict[str, int] = {
    "hh101": 1, "hh102": 1, "hh103": 1, "hh105": 1, "hh106": 1, "hh110": 1, "hh114": 1,
    "hh118": 1, "hh122": 1, "hh126": 1, "hh129": 1, "hh130": 1, "hh107": 2, "hh121": 2,
}
SPLIT_SEED = 20261006          # фиксируется в предрегистрации (тот же seed, что у бутстрепа протокола)
WINDOW_DEFAULT = (7 * 3600, 23 * 3600)
MIN_WINDOW_EVENTS = 50         # день пригоден, если в окне ≥ 50 сообщений ON/OPEN (отсекает сбои записи)

SOURCE_DOI = "10.5281/zenodo.15708568"
SOURCE_URL = "https://zenodo.org/records/15708568/files/labeled_data.zip"
LICENSE = "CC BY 4.0"
CITATION = ("Cook D. CASAS Smart Home dataset - free living, motion, door, activity labels. "
            "Zenodo, 2025. DOI 10.5281/zenodo.15708568")

SUBSET_HEADER = ["date", "t", "sensor", "value", "activity"]


@dataclasses.dataclass(frozen=True, slots=True)
class Record:
    """Одно сообщение датчика. value: "ON"/"OFF"/"OPEN"/"CLOSE" или int."""

    date: str
    t: float
    sensor: str
    value: str | int
    activity: str = ""   # "Имя:begin" | "Имя:end" | ""

    @property
    def is_motion(self) -> bool:
        return self.value in ("ON", "OFF")

    @property
    def is_contact(self) -> bool:
        return self.value in ("OPEN", "CLOSE")


def parse_time(s: str) -> float:
    """'10:43:05.0317' -> секунды от полуночи (дробная часть любой длины)."""
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


def parse_activity(field: str) -> str:
    field = field.strip()
    if field.endswith('="begin"'):
        return field[: -len('="begin"')] + ":begin"
    if field.endswith('="end"'):
        return field[: -len('="end"')] + ":end"
    return ""


def parse_line(line: str) -> Record | None:
    """Разбор строки исходного CSV; None для битых/неизвестных строк."""
    p = line.rstrip("\r\n").replace("\r", "").split(",")
    if len(p) < 4 or not p[2] or not p[0]:
        return None
    try:
        t = parse_time(p[1])
    except (ValueError, IndexError):
        return None
    msg = p[3].strip()
    value: str | int
    if msg in ("ON", "OFF", "OPEN", "CLOSE"):
        value = msg
    elif msg.isdigit():
        value = int(msg)
    else:
        return None
    act = parse_activity(p[4]) if len(p) >= 5 else ""
    return Record(p[0], round(t, 3), p[2].strip(), value, act)


def iter_source(zip_path: Path, home: str) -> Iterator[Record]:
    """Записи дома из исходного архива (в порядке файла)."""
    with zipfile.ZipFile(zip_path) as z:
        with z.open(f"labeled/{home}.csv") as f:
            for raw in io.TextIOWrapper(f, encoding="utf-8", errors="ignore", newline=""):
                r = parse_line(raw)
                if r is not None:
                    yield r


def full_days(records: list[Record]) -> list[str]:
    """Полные сутки: все даты, кроме первой и последней."""
    dates = sorted({r.date for r in records})
    return dates[1:-1]


def split_homes(homes: dict[str, int], seed: int = SPLIT_SEED) -> dict[str, str]:
    """Детерминированное разбиение 7/7, стратифицированное по числу жителей.

    Дома каждой страты сортируются по имени и перемешиваются random.Random(f"{seed}/residents=<n>");
    страты склеиваются по возрастанию n, и дома по порядку попеременно уходят в tune и holdout.
    Так каждая страта делится пополам (12 + 2 -> 6 + 1 / 6 + 1).
    """
    strata: dict[int, list[str]] = defaultdict(list)
    for h, n in homes.items():
        strata[n].append(h)
    order: list[str] = []
    for n in sorted(strata):
        hs = sorted(strata[n])
        random.Random(f"{seed}/residents={n}").shuffle(hs)
        order += hs
    return dict(sorted((h, "tune" if i % 2 == 0 else "holdout") for i, h in enumerate(order)))


def hour_weights(records: Iterable[Record], dates: set[str]) -> list[float]:
    """Доли начал размеченных активностей по часам суток (24 значения, сумма 1)."""
    c: Counter[int] = Counter()
    for r in records:
        if r.date in dates and r.activity.endswith(":begin"):
            c[int(r.t // 3600) % 24] += 1
    tot = sum(c.values())
    if tot == 0:
        return [1 / 24] * 24
    return [round(c.get(h, 0) / tot, 6) for h in range(24)]


def locations(records: Iterable[Record]) -> dict[str, list[str]]:
    """Локации дома и типы сообщений в них: {'Kitchen': ['contact', 'motion'], ...}."""
    kinds: dict[str, set[str]] = defaultdict(set)
    for r in records:
        if r.is_motion:
            kinds[r.sensor].add("motion")
        elif r.is_contact:
            kinds[r.sensor].add("contact")
    return {k: sorted(v) for k, v in sorted(kinds.items())}


def _write_gz_csv(path: Path, rows: Iterable[Record]) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(SUBSET_HEADER)
    for r in rows:
        w.writerow([r.date, f"{r.t:.3f}", r.sensor, r.value, r.activity])
    data = buf.getvalue().encode("utf-8")
    with open(path, "wb") as f:
        with gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0, compresslevel=9) as gz:
            gz.write(data)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_subset(zip_path: Path = DEFAULT_ZIP, out_dir: Path = DEFAULT_SUBSET,
                   seed: int = SPLIT_SEED, homes: dict[str, int] | None = None,
                   window: tuple[int, int] = WINDOW_DEFAULT) -> dict:
    """Извлекает компактное подмножество и пишет index.json + PROVENANCE.txt. Возвращает индекс."""
    homes = homes or HOMES
    out_dir.mkdir(parents=True, exist_ok=True)
    split = split_homes(homes, seed)
    index: dict = {
        "format": "casas-subset/1",
        "source": {"doi": SOURCE_DOI, "url": SOURCE_URL, "file": "labeled_data.zip",
                   "sha256": sha256_file(zip_path), "license": LICENSE, "citation": CITATION},
        "split_seed": seed,
        "window_s": list(window),
        "min_window_events": MIN_WINDOW_EVENTS,
        "columns": SUBSET_HEADER,
        "homes": {},
    }
    for home in sorted(homes):
        recs = list(iter_source(zip_path, home))
        days = full_days(recs)
        dayset = set(days)
        # стабильная сортировка по (дата, время): исходник почти упорядочен, но не гарантирует этого
        sub = sorted((r for r in recs if r.date in dayset), key=lambda r: (r.date, r.t))
        fname = f"{home}.csv.gz"
        _write_gz_csv(out_dir / fname, sub)
        per_day = Counter(r.date for r in sub
                          if r.value in ("ON", "OPEN") and window[0] <= r.t < window[1])
        eligible = [d for d in days if per_day.get(d, 0) >= MIN_WINDOW_EVENTS]
        index["homes"][home] = {
            "residents": homes[home],
            "split": split[home],
            "file": fname,
            "sha256": sha256_file(out_dir / fname),
            "n_records": len(sub),
            "days": days,
            "eligible_days": eligible,
            "window_on_open_per_day": {d: per_day.get(d, 0) for d in days},
            "locations": locations(sub),
            "hour_weights_act": hour_weights(sub, dayset),
        }
    (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                        encoding="utf-8")
    _write_provenance(out_dir, index)
    return index


def _write_provenance(out_dir: Path, index: dict) -> None:
    tune = sorted(h for h, v in index["homes"].items() if v["split"] == "tune")
    hold = sorted(h for h, v in index["homes"].items() if v["split"] == "holdout")
    n_days = sum(len(v["days"]) for v in index["homes"].values())
    n_elig = sum(len(v["eligible_days"]) for v in index["homes"].values())
    src = index["source"]
    text = f"""Подмножество трасс CASAS для daygen (bench/daygen/casas.py, extract_subset).

Источник: {src['citation']}
URL: {src['url']}
Исходный файл: {src['file']}, SHA-256 {src['sha256']}
Лицензия исходника: {src['license']} (https://creativecommons.org/licenses/by/4.0/) — производные данные
распространяются на тех же условиях с указанием авторства; изменения перечислены ниже.

Изменения относительно исходника:
- взяты 14 домов анализа GAP1 (12 с одним жителем, 2 с двумя): {', '.join(sorted(index['homes']))};
- оставлены только полные сутки (первая и последняя даты записи каждого дома отброшены): {n_days} дом-суток,
  из них пригодны для генерации дня {n_elig} (≥ {index['min_window_events']} сообщений ON/OPEN в окне
  {index['window_s'][0] // 3600:02d}:00–{index['window_s'][1] // 3600:02d}:00);
- поля: дата, секунды от полуночи (округление до 1 мс), локация датчика, сообщение (ON/OFF/OPEN/CLOSE или
  целое), метка активности только для начала/конца (Имя:begin / Имя:end); строки с неизвестными сообщениями
  (опечатки, пустые поля) отброшены; удалены символы \\r;
- файлы — CSV в gzip (mtime=0, извлечение воспроизводится байт в байт).

Разбиение домов на настройку (tune) и отложенную оценку (holdout): seed {index['split_seed']},
стратификация по числу жителей (bench/daygen/casas.py: split_homes).
  tune:    {', '.join(tune)}
  holdout: {', '.join(hold)}

Воспроизведение:
  .venv/bin/python -m bench.daygen casas-extract --zip data/casas/labeled_data.zip --out data/casas/subset
SHA-256 файлов подмножества — в index.json (поле homes.<дом>.sha256).
"""
    (out_dir / "PROVENANCE.txt").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Чтение подмножества
# ---------------------------------------------------------------------------


def load_index(subset_dir: Path = DEFAULT_SUBSET) -> dict:
    path = subset_dir / "index.json"
    if not path.exists():
        raise FileNotFoundError(
            f"нет {path}: извлеките подмножество командой `python -m bench.daygen casas-extract` "
            "или используйте --source synthetic")
    return json.loads(path.read_text(encoding="utf-8"))


def load_day(subset_dir: Path, home: str, date: str, index: dict | None = None) -> list[Record]:
    """Записи одних суток дома (по возрастанию t)."""
    index = index or load_index(subset_dir)
    meta = index["homes"][home]
    path = subset_dir / meta["file"]
    out: list[Record] = []
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        rd = csv.reader(f)
        header = next(rd)
        if header != SUBSET_HEADER:
            raise ValueError(f"{path}: неожиданный заголовок {header}")
        for d, t, sensor, value, act in rd:
            if d != date:
                if out:
                    break  # файл отсортирован по дате
                continue
            v: str | int = int(value) if value.isdigit() else value
            out.append(Record(d, float(t), sensor, v, act))
    if not out:
        raise KeyError(f"{home}: нет записей за {date}")
    return out


def check_zip(zip_path: Path = DEFAULT_ZIP) -> tuple[bool, str]:
    """Проверка архива: существует и проходит CRC всех членов (аналог unzip -t)."""
    if not zip_path.exists():
        return False, f"нет файла {zip_path}"
    try:
        with zipfile.ZipFile(zip_path) as z:
            bad = z.testzip()
            names = set(z.namelist())
    except zipfile.BadZipFile as e:
        return False, f"битый архив: {e}"
    if bad is not None:
        return False, f"ошибка CRC в {bad}"
    missing = [h for h in HOMES if f"labeled/{h}.csv" not in names]
    if missing:
        return False, f"в архиве нет домов {missing}"
    return True, "ok"


def subset_exists(subset_dir: Path = DEFAULT_SUBSET) -> bool:
    return (subset_dir / "index.json").exists()


def env_subset_dir() -> Path:
    """Каталог подмножества (переопределяется переменной окружения DAYGEN_CASAS_SUBSET)."""
    return Path(os.environ.get("DAYGEN_CASAS_SUBSET", str(DEFAULT_SUBSET)))
