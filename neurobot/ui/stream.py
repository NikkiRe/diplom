"""Формат потока пульта `neurobot-ui/1` — один для «Записи» и живого шлюза (README, «Формат потока»).

Сообщение — объект JSON `{"k": вид, "t": стенные часы робота, с, ...}`; виды, поля и их типы — KINDS. «Запись» кладёт
в бандл список сообщений прогона, отсортированный по t. Живой шлюз (gateway.py) шлёт их по SSE
(`id: <запуск шлюза>.<номер>`, `data: <json>`, одно событие на сообщение; `hb` раз в 0,5 с и последний `scan` — без
номера и мимо буфера, на своём месте по t) и держит кольцевой буфер последних BUFFER_S секунд (live.Hub).
Подключение (connect):
  1. `hello` — формат, источник и `active`: тревоги, стоящие в момент hello;
  2. без Last-Event-ID (или с номером, которого в буфере уже нет) — снимок State на начало буфера: последнее
     сообщение каждого вида состояния, скорость по каждому topic, стоящие тревоги (t — с какого момента стоит),
     незакрытые жесты одним сообщением со всеми полями (t — первое сообщение жеста); всё с исходным t, по возрастанию;
  3. буфер целиком, затем живой хвост. С Last-Event-ID из буфера — `hello` с `resume` и только пропущенное.
Так клиент, подключившийся или переподключившийся в любой момент, видит то же, что видел бы с начала: стоящие
тревоги, начатые жесты, последние значения (тест test_ui_stream.ConnectTest). Браузер не знает, откуда сообщения,
кроме подписи источника.

Бандл `neurobot-ui-bundle/1` — JSON (можно .gz): заголовок (геометрия робота, словарь тревог, строки «до/после»)
и прогоны: meta, summary (итоговые числа metrics.py и ячейки compare.md), stream.
"""
from __future__ import annotations

import gzip
import io
import json
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

FORMAT = "neurobot-ui/1"
BUNDLE_FORMAT = "neurobot-ui-bundle/1"
SOURCES = ("робот", "двойник", "запись")
BUFFER_S = 900.0        # кольцевой буфер шлюза — 15 мин: самое длинное окно «Обзора»; длиннее — агрегаты stats

# Типы полей: "num" — конечное число, "int", "str", "bool", "dict", "list", "[str]" — список строк, "[dict]";
# "?" в конце — допускается null; кортеж — перечисление допустимых значений.
NUM, INT, STR, BOOL, DICT, LIST = "num", "int", "str", "bool", "dict", "list"
STATES = ("ok", "slow", "blocked", "estop")
OUTCOMES = ("done", "wrong", "none", "cut", "preempted", "unobserved", "false_action")
REFS = ("t0", "t_end")      # t0 — истина генератора жестов (двойник), t_end — конец жеста по perception (робот)

# вид → (обязательные поля, необязательные), кроме k и t; лишнее поле — ошибка: формат один для двух концов
KINDS: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {
    # заголовок соединения (живой режим); в бандле его роль играют meta прогона и заголовок бандла
    "hello": ({"format": (FORMAT,), "source": SOURCES},
              {"run": STR, "mode": ("replay", "live"), "geometry": DICT, "alarms": DICT, "active": "[dict]",
               "resume": BOOL, "boot": STR, "started": NUM, "node": STR, "publishers": "[str]", "ref": REFS}),
    "hb": ({}, {}),
    # скан лидара (живой режим): бины пеленга в базе робота от a0 через da градусов, в каждом — ближайшее
    # попадание от центра корпуса, м; null — попаданий нет (нет данных, «свободно» или лучей нет); не в буфере
    "scan": ({"a0": NUM, "da": NUM, "r": "[num?]"}, {"frame": STR, "range_max": NUM}),
    # /neurobot/safety_state (подмножество в записи) + уровень тревоги шлюза (0 — норма, 1, 2)
    "safety": ({"state": STATES, "reasons": "[str]", "level": (0, 1, 2)},
               {"warnings": "[str]", "min_clearance_m": "num?", "front_m": "num?", "limits": DICT, "req": DICT,
                "out": DICT, "cut": BOOL, "tilt_deg": "num?", "estop": DICT, "lidar_mask_deg": LIST}),
    # скорость: req — /neurobot/cmd_vel_req (исполнитель), out — /cmd_vel (safety_gate), base — вход базы;
    # шлётся при изменении и раз в 1 с, пока писатель жив; d_stop_m — тормозной путь safety_gate при v
    "cmd": ({"topic": ("req", "out", "base"), "v": NUM, "w": NUM}, {"d_stop_m": "num?"}),
    # /neurobot/perception_health
    "health": ({"state": STR}, {"fps": "num?", "frame_age_s": "num?", "recoveries": INT, "dark": BOOL}),
    # жест: частичные обновления по id (распознан → первая команда → исход); t_cmd — стенные часы первой команды
    # (в снимке сообщения жеста слиты); rtf_ok — жест входит в K1 по RTF
    "gesture": ({"id": STR},
                {"seg": INT, "kind": ("gesture", "negative"), "name": "str?", "cls": "int?", "conf": "num?",
                 "margin": "num?", "n_boxes": "int?", "side": "str?", "t_end": NUM, "t_rec": NUM, "expected": "str?",
                 "d_rec_s": NUM, "d_cmd_s": NUM, "t_cmd": NUM, "ref": REFS, "early": BOOL, "rtf_ok": BOOL,
                 "maneuver": "str?", "outcome": OUTCOMES, "stopped_by": "[str]", "neg": "str?"}),
    # задержка «жест → команда»: последние window жестов и весь прогон (квантили metrics.quantiles); у живого шлюза
    # run — по последним run_window жестам (кольцо: шлюз работает неделями)
    "k1": ({"last": DICT, "run": DICT}, {"window": INT, "ref": REFS, "run_window": INT}),
    # вызов LLM: kind — request (реплика человека), service (служебный вызов хода), gesture (вне реплик, K4)
    "llm": ({"kind": ("request", "service", "gesture")},
            {"model": STR, "prompt_tokens": INT, "completion_tokens": INT, "cached_tokens": INT, "rub_fact": NUM,
             "rub_est": NUM, "latency_ms": NUM}),
    # реплика человека агенту и её итог
    "agent": ({"n": INT, "request": STR, "ok": BOOL}, {"latency_s": "num?", "llm_calls": INT, "outcome": STR}),
    # память и CPU компонентов робота (без драйверов двойника); metric — PSS (сэмплер стенда) или RSS (/proc шлюза)
    "mem": ({"total_mib": NUM}, {"units": DICT, "cpu_pct": NUM, "budget_mib": NUM, "metric": ("pss", "rss")}),
    # юниты (процессы у A0): в работе из всего; list — по юниту (живой режим: systemctl show)
    "units": ({"up": INT, "total": INT}, {"down": "[str]", "restarts": DICT, "list": "[dict]"}),
    # события стенда и человека
    "fault": ({"fault": STR}, {"role": STR, "ttr_s": "num?", "path": STR}),
    "operator": ({"action": STR}, {"what": STR, "why": STR}),
    "touch": ({"episodes": INT}, {"models": "[str]"}),
    # тревога (AlarmTracker): встала / снята; n — сколько раз вставала
    "alarm": ({"key": STR, "code": STR, "prio": (1, 2), "on": BOOL, "text": STR}, {"n": INT, "src": STR}),
    # сводка прогона на момент t (функции metrics.py), раз в 10 с и в конце
    "stats": ({}, {"distance_m": NUM, "moving_s": NUM, "moving_share": "num?", "safety_share": DICT, "touches": INT,
                   "llm": DICT, "k1": DICT}),
    "end": ({}, {"reason": STR}),
}
# виды состояния: новому клиенту — последнее сообщение (cmd — по каждому topic)
STATE_KINDS = ("safety", "health", "k1", "stats", "mem", "units", "touch", "scan")
# не в кольцевой буфер шлюза и без номера: hb — у каждого соединения свой, scan — только последний (5 Гц × 15 мин
# скана — десятки МиБ шлюзу и браузеру ради картинки, которой нужен один кадр)
TRANSIENT = ("hb", "scan")


class FormatError(ValueError):
    pass


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _ok(v: Any, spec: Any) -> bool:
    if isinstance(spec, tuple):                    # перечисление: значение и тип (True — не 1)
        return any(v == x and type(v) is type(x) for x in spec)
    if spec.endswith("?"):
        return v is None or _ok(v, spec[:-1])
    if spec == NUM:
        return _num(v)
    if spec == INT:
        return isinstance(v, int) and not isinstance(v, bool)
    if spec == STR:
        return isinstance(v, str)
    if spec == BOOL:
        return isinstance(v, bool)
    if spec == DICT:
        return isinstance(v, dict)
    if spec == LIST:
        return isinstance(v, list)
    if spec.startswith("[") and spec.endswith("]"):
        return isinstance(v, list) and all(_ok(x, spec[1:-1]) for x in v)
    raise ValueError("тип поля %r" % (spec,))


def check(msg: Any) -> None:
    """Сообщение потока по KINDS: вид, t, поля и их типы; FormatError — с видом и полем."""
    if not isinstance(msg, dict):
        raise FormatError("сообщение — не объект: %r" % (msg,))
    k = msg.get("k")
    if k not in KINDS:
        raise FormatError("неизвестный вид %r" % (k,))
    if not _num(msg.get("t")):
        raise FormatError("%s: t — не число" % k)
    need, opt = KINDS[k]
    missing = [f for f in need if f not in msg]
    if missing:
        raise FormatError("%s: нет полей %s" % (k, ", ".join(missing)))
    extra = sorted(set(msg) - set(need) - set(opt) - {"k", "t"})
    if extra:
        raise FormatError("%s: лишние поля %s" % (k, ", ".join(extra)))
    for f, v in msg.items():
        spec = need.get(f, opt.get(f))
        if spec is not None and not _ok(v, spec):
            raise FormatError("%s: %s = %r" % (k, f, v))
    if k == "hello":
        for a in msg.get("active", []):            # стоящая тревога — те же поля, что alarm, и since вместо t
            a = dict(a)
            since = a.pop("since", None)
            check(dict(a, k="alarm", t=since, on=True))


def check_stream(msgs: Iterable[dict]) -> int:
    """Поток прогона: каждое сообщение по формату, t не убывает. Возвращает число сообщений."""
    n, last = 0, -math.inf
    for m in msgs:
        check(m)
        if m["t"] < last:
            raise FormatError("t убывает на сообщении %d (%s)" % (n, m["k"]))
        last = m["t"]
        n += 1
    return n


# ---- подключение: снимок состояния и кольцевой буфер шлюза

class State:
    """Состояние потока для нового клиента: последнее сообщение каждого вида состояния (cmd — по topic), стоящие
    тревоги, незакрытые жесты. Шлюз кормит его сообщениями, вытесненными из кольцевого буфера (push)."""

    def __init__(self) -> None:
        self.last: Dict[str, dict] = {}                 # вид или cmd:topic → сообщение
        self.alarms: Dict[Tuple[str, str], dict] = {}   # (src, key) → сообщение alarm с t = since
        self.gestures: Dict[str, dict] = {}             # id → слитое сообщение (t — первое)

    def push(self, m: dict) -> None:
        k = m["k"]
        if k in STATE_KINDS:
            self.last[k] = m
        elif k == "cmd":
            self.last["cmd:" + m["topic"]] = m
        elif k == "alarm":
            key = (m.get("src", ""), m["key"])
            if not m["on"]:
                self.alarms.pop(key, None)
            else:
                cur = self.alarms.get(key)
                self.alarms[key] = dict(m, t=cur["t"] if cur else m["t"])     # эскалация не сдвигает since
        elif k == "gesture":
            g = self.gestures.get(m["id"])
            g = dict(m) if g is None else dict(g, **{f: v for f, v in m.items() if f != "t"})
            if "outcome" in m:
                self.gestures.pop(m["id"], None)
            else:
                self.gestures[m["id"]] = g

    def copy(self) -> "State":
        s = State()
        s.last, s.alarms, s.gestures = dict(self.last), dict(self.alarms), dict(self.gestures)
        return s

    def active(self) -> List[dict]:
        """Тревоги, стоящие сейчас: поле hello.active."""
        out = []
        for m in sorted(self.alarms.values(), key=lambda a: (a["prio"], a["t"], a["key"])):
            a = {"key": m["key"], "code": m["code"], "prio": m["prio"], "text": m["text"], "since": m["t"]}
            for f in ("n", "src"):
                if f in m:
                    a[f] = m[f]
            out.append(a)
        return out

    def snapshot(self) -> List[dict]:
        """Снимок: сообщения с исходными t, по возрастанию t."""
        msgs = list(self.last.values()) + list(self.alarms.values()) + list(self.gestures.values())
        return sorted(msgs, key=lambda m: m["t"])


def connect(hello: dict, evicted: State, buffer: Sequence[dict], resume: bool = False) -> List[dict]:
    """Начало потока нового подключения: hello (active — тревоги в момент hello), снимок на начало буфера (кроме
    resume — клиент уже всё видел), буфер. Живой хвост шлюз шлёт следом."""
    now = evicted.copy()
    for m in buffer:
        now.push(m)
    head = dict(hello, k="hello", format=FORMAT, active=now.active())
    if resume:
        head["resume"] = True
        return [head] + list(buffer)
    return [head] + evicted.snapshot() + list(buffer)


def check_connect(seq: Sequence[dict]) -> int:
    """Последовательность подключения: первое — hello формата FORMAT, дальше — поток с неубывающим t."""
    if not seq or seq[0].get("k") != "hello":
        raise FormatError("подключение начинается не с hello")
    check(seq[0])
    return 1 + check_stream(seq[1:])


def sse(msg: dict, seq: Optional[Any] = None) -> bytes:
    """Кадр SSE живого шлюза: одно событие на сообщение; id — номер в кольцевом буфере (у hello, hb и scan номера
    нет; шлюз пишет «запуск.номер», чтобы номер прежнего запуска не сошёл за свой)."""
    head = "id: %s\n" % seq if seq is not None else ""
    return (head + "data: " + dumps(msg) + "\n\n").encode("utf-8")


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def write_bundle(path: str, bundle: Dict[str, Any]) -> int:
    """Бандл в файл (.gz — сжатый, mtime 0: одинаковый вход — одинаковые байты). Возвращает размер, байт."""
    data = dumps(bundle).encode("utf-8")
    if path.endswith(".gz"):
        buf = io.BytesIO()
        with gzip.GzipFile(filename="", mode="wb", fileobj=buf, compresslevel=9, mtime=0) as z:
            z.write(data)
        data = buf.getvalue()
    with open(path, "wb") as f:
        f.write(data)
    return len(data)


def read_bundle(path: str) -> Dict[str, Any]:
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    doc = json.loads(data.decode("utf-8"))
    if doc.get("format") != BUNDLE_FORMAT:
        raise FormatError("не бандл пульта: format %r" % (doc.get("format"),))
    return doc


def check_bundle(doc: Dict[str, Any]) -> List[str]:
    """Бандл: заголовок и потоки всех прогонов. Возвращает id прогонов."""
    if doc.get("format") != BUNDLE_FORMAT or doc.get("stream_format") != FORMAT:
        raise FormatError("format / stream_format")
    for key in ("geometry", "alarms", "runs", "compare_rows"):
        if key not in doc:
            raise FormatError("бандл: нет %s" % key)
    ids = []
    for r in doc["runs"]:
        for key in ("id", "label", "system", "source", "t_start", "t_end", "summary", "stream"):
            if key not in r:
                raise FormatError("прогон %s: нет %s" % (r.get("id"), key))
        if r["source"] != "запись":
            raise FormatError("прогон %s: source %r" % (r["id"], r["source"]))
        check_stream(r["stream"])
        ids.append(r["id"])
    if len(set(ids)) != len(ids):
        raise FormatError("id прогонов повторяются")
    return ids
