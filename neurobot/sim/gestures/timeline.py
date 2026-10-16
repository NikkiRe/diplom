"""Формат файлов gesturegen и поиск бокса по времени.

Оба файла — JSONL, первая строка {"header": {...}} (версия формата, seed, все параметры генерации), дальше записи.

Таймлайн — ступенчатая функция «что отдал бы детектор руки»: запись {t, bbox, conf, seg} действует на
[t, t_следующей). bbox = [x1, y1, x2, y2] в пикселях кадра 640×480 в той ориентации, в которой кадр
подаётся детектору в predict_live.py (после cv2.flip(·, 1), «зеркало»); null — детекции нет. seg — номер
сегмента истины (null — пауза). Время t — секунды от якоря (clock.py).

Истина — по сегменту в строке: {seg, kind: gesture|negative, cls, name, side, shape, reverse, end, t_start, t0,
t_leave, dist_m, h_m, amp_px, fit, dur_s, cut, cut_top, n_boxes}. t0 — конец жеста (рука закончила траекторию),
t_leave — последний бокс (рука ушла из кадра), t_start — первый бокс; reverse — форма пройдена в обратную
сторону, h_m — высота кисти (null при fit), fit — множитель размаха, cut и cut_top — доля отсчётов, потерянных
у края кадра. Для отрицательных сегментов cls = −1, t0 = t_leave, а из геометрии — только поля их вида:
dist_m (hand_still, passerby), direction (passerby), rate_hz (false_boxes).
"""
from __future__ import annotations

import bisect
import hashlib
import json
import os
import tempfile

FORMAT_VERSION = 1


def rnd(x, nd: int = 6):
    """Округление перед записью; + 0.0 превращает −0.0 в 0.0 (иначе в JSON попадает «-0.0», и байты файла
    зависят от знака нуля). None — как есть."""
    return None if x is None else round(x + 0.0, nd)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_jsonl(path: str, header: dict, records) -> None:
    """Атомарная запись: временный файл в том же каталоге + os.replace; права 0644, как у обычного файла
    (mkstemp создаёт 0600, а таймлайн читают процессы других пользователей, например юниты systemd)."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=d)
    try:
        os.chmod(tmp, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(dumps({"header": header}) + "\n")
            for r in records:
                fh.write(dumps(r) + "\n")
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def read_jsonl(path: str):
    """(header, records). Без заголовка или с чужой версией формата — ValueError."""
    with open(path, encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip()]
    if not lines:
        raise ValueError("%s: пустой файл" % path)
    head = json.loads(lines[0])
    if "header" not in head:
        raise ValueError("%s: первая строка должна быть {\"header\": …}" % path)
    header = head["header"]
    if header.get("version") != FORMAT_VERSION:
        raise ValueError("%s: версия формата %r, ожидалась %d" % (path, header.get("version"), FORMAT_VERSION))
    return header, [json.loads(ln) for ln in lines[1:]]


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


class Timeline:
    """Таймлайн в памяти: поиск записи, действующей в момент t (двоичный поиск)."""

    def __init__(self, header: dict, records: list, sha256: str = ""):
        self.header = header
        self.sha256 = sha256
        self._t = [float(r["t"]) for r in records]
        if any(b <= a for a, b in zip(self._t, self._t[1:])):
            raise ValueError("времена таймлайна должны строго возрастать")
        self._records = records

    @classmethod
    def load(cls, path: str) -> Timeline:
        header, records = read_jsonl(path)
        return cls(header, records, file_sha256(path))

    @property
    def end(self) -> float:
        """Время последней записи: дальше детектор ничего нового не увидит."""
        return self._t[-1] if self._t else 0.0

    def at(self, t: float):
        """Запись, действующая в момент t, или None до первой записи."""
        i = bisect.bisect_right(self._t, t) - 1
        return self._records[i] if i >= 0 else None

    def box_at(self, t: float):
        """(x1, y1, x2, y2, conf) или None."""
        r = self.at(t)
        if r is None or r["bbox"] is None:
            return None
        x1, y1, x2, y2 = r["bbox"]
        return x1, y1, x2, y2, r["conf"]
