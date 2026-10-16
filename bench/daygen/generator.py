"""Генерация «рабочего дня» (IMPLEMENTATION_SPEC §2, THESIS_CONCEPT §12.1).

Схема одного прохода (всё по виртуальному времени t — секунды от полуночи суток):

1. Трасса датчиков дома: CASAS (подмножество data/casas/subset) или синтетический дом. Сообщения
   ON/OFF/OPEN/CLOSE → изменения свойств pir_*/door_front/contact_* (повторы одного значения
   отбрасываются: событие = изменение ресурса). Всё до начала окна сворачивается в initial_state.
2. Синтетика: климат-датчик, котёл (эпизоды перегрева), датчик протечки — [допущения] synth.py.
3. Моменты реплик пользователя: сессии по Bentley 2018 / VisionClaw (команд/сут, команд/сессию),
   паузы внутри сессии логнормальные (медиана 90 с, [5; 600] с), час начала — по часовому профилю
   активностей дома (как compression_bias.py:gen_day из GAP1).
4. Проход по времени (очередь событий): применяются изменения датчиков, ручные переключения света
   жителем [допущение], таймеры устройств (кофе, пылесос); в момент реплики генерируется вопрос
   или команда с учётом «идеального» состояния (предусловия: выключают включённое и т. п.).
5. Значимые ситуации — предикаты над итоговой трассой (situations.py).

Каждый случайный выбор берёт свой поток random.Random(f"daygen/{split}/{seed}/<имя>"), поэтому день
воспроизводится байт в байт, а изменение одной части генератора не сдвигает остальные потоки. Соль split
(tune | holdout — часть, к которой относится дом) разводит пользовательскую нагрузку tune и holdout при
одинаковых seed: иначе дни holdout повторяли бы число реплик, k и порядок вопросов/команд дней tune.

Пара (дом, сутки) CASAS по умолчанию берётся из расписания части (schedule_pair): круги по всем домам
части в перемешанном порядке, у каждого дома — свои сутки без повторов, пока они не кончатся. Подряд идущие
seed дают разные дни, а дома представлены поровну (без возвращения, в отличие от прежнего выбора по seed).
"""
from __future__ import annotations

import copy
import heapq
import json
import math
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import casas as casas_mod
from . import massive, room, situations, synth
from .commands import CommandContext, make_command
from .questions import QuestionContext, make_question

GENERATOR_VERSION = "1.1"   # 1.1: соль split в потоках ГСЧ, расписание (дом, сутки), обрезка реплик по окну
FORMAT = "daygen/1"

# команд/сут, команд/сессию: Bentley et al. 2018 (IMWUT 2(3):91, табл. 6) — L, M, H;
# M+ — VisionClaw (arXiv:2604.03486, §6.2): 10,1 взаимодействия/активный день, 555/118 ≈ 4,7 на сессию;
# S69/S200 — стресс только для T0 (THESIS_CONCEPT §12.1), команд/сессию как у H.
PROFILES: dict[str, tuple[float, float]] = {
    "L": (2.61, 2.42), "M": (5.55, 2.74), "M+": (10.1, 4.7), "H": (30.44, 5.40),
    "S69": (69.0, 5.40), "S200": (200.0, 5.40),
}
SPLITS = ("tune", "holdout", "any")

# Вызовов LLM на команду (VisionClaw: 0 / 1 / 2–3 / ≥4 инструмента → 1 / 2 / 3 / 5 вызовов), E[k] ≈ 2,9.
K_DIST = [(1, 0.21), (2, 0.27), (3, 0.23), (5, 0.29)]
PAUSE_MEDIAN_S, PAUSE_SIGMA, PAUSE_MIN_S, PAUSE_MAX_S = 90.0, 1.0, 5.0, 600.0

# Ручные переключения света жителем при смене комнаты [допущение]: «тёмные часы» — первый час окна и после 19:30.
MANUAL_P = {"dark": (0.3, 0.5), "day": (0.02, 0.2)}   # (включить свет в новой комнате, выключить в прежней)
MANUAL_MIN_GAP_S = 1800.0
MANUAL_DELAY_S = 2.0
DARK_EVENING_S = 19.5 * 3600

# Порядок событий с одинаковым t: сначала датчики, потом ситуации, потом реплики пользователя.
KIND_ORDER = {"sensor": 0, "situation": 1, "question": 2, "user_cmd": 2}


def hhmm(s: float) -> str:
    s = round(s)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}"


def parse_hhmm(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 3600 + int(m) * 60


@dataclass
class DayConfig:
    """Параметры дня. Все поля попадают в заголовок (params) — по нему день воспроизводится."""

    seed: int
    profile: str = "M+"
    source: str = "casas"                 # casas | synthetic
    home: str | None = None               # None — по расписанию части split (schedule_pair)
    split: str | None = None              # tune | holdout | any; для CASAS без home обязателен
    date: str | None = None               # None — выбрать пригодные сутки по seed
    lang: str = "ru"                      # ru | en | mix
    window: tuple[str, str] = ("07:00", "23:00")
    rate_scale: float = 1.0               # множитель профиля: реплик на окно = команд/сут × rate_scale
    question_share: float = 0.25          # доля вопросов среди реплик [допущение]
    trap_share: float = 0.10              # доля невалидных команд (ловушки) среди команд [допущение]
    synthetic_situations: int = 2         # перегрев/протечка в сутки (0 / 2 / 5 — чувствительность)
    deadline_s: float = 300.0             # допустимое время реакции на значимую ситуацию
    door_open_min: float = 10.0           # «дверь оставлена открытой» дольше N мин
    night_start: str = "22:00"            # «дверь открыта поздно вечером/ночью» после этого времени
    no_motion_h: float = 2.0              # «нет движения дома» дольше M ч
    absence_h: float = 4.0                # «долгое отсутствие» дольше K ч
    awake: tuple[str, str] = ("07:00", "21:00")   # «часы бодрствования» для no_motion_home (сон — не ситуация)
    manual_switch: bool = True            # ручные переключения света жителем
    subset_dir: str | None = None         # каталог подмножества CASAS (по умолчанию data/casas/subset)
    templates: str | None = None          # файл шаблонов MASSIVE (по умолчанию data/massive/iot_templates.json)

    def validate(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError(f"профиль {self.profile!r}: допустимы {sorted(PROFILES)}")
        if self.source not in ("casas", "synthetic"):
            raise ValueError("source: casas | synthetic")
        if self.split not in (None, *SPLITS):
            raise ValueError("split: tune | holdout | any")
        if self.source == "casas" and self.split is None and self.home is None:
            raise ValueError("для дня CASAS нужен split (tune | holdout | any) или явный home: "
                             "иначе можно незаметно взять дом отложенной части")
        if self.lang not in ("ru", "en", "mix"):
            raise ValueError("lang: ru | en | mix")
        w0, w1 = (parse_hhmm(x) for x in self.window)
        if not 0 <= w0 < w1 <= 24 * 3600:
            raise ValueError("окно: начало < конец в пределах суток")
        if self.rate_scale < 0:
            raise ValueError("rate_scale ≥ 0")
        for name in ("question_share", "trap_share"):
            v = getattr(self, name)
            if not 0 <= v <= 1:
                raise ValueError(f"{name} ∈ [0; 1]")
        if self.synthetic_situations < 0:
            raise ValueError("synthetic_situations ≥ 0")
        a0, a1 = (parse_hhmm(x) for x in self.awake)
        if not 0 <= a0 < a1 <= 24 * 3600:
            raise ValueError("awake: начало < конец в пределах суток")


@dataclass
class Utterance:
    t: float
    session: int
    turn: int


@dataclass(order=True)
class _QItem:
    t: float
    order: int
    seq: int
    payload: Any = field(compare=False)


class DayBuilder:
    """Состояние одного прохода генерации (см. описание модуля)."""

    def __init__(self, cfg: DayConfig):
        cfg.validate()
        self.cfg = cfg
        self.w0 = parse_hhmm(cfg.window[0])
        self.w1 = parse_hhmm(cfg.window[1])
        self.templates = massive.load_templates(Path(cfg.templates) if cfg.templates else None)
        self._seq = 0
        self.events: list[dict] = []
        self.sensor_log: list[dict] = []     # выданные события датчиков в окне (для вопросов и ситуаций)
        self.history: list[dict] = []        # выполненные (идеально) команды пользователя
        # «поколение» действий устройства: новое действие отменяет таймер предыдущего (как в sim-mcp World)
        self.action_gen: dict[str, int] = {}
        self.manual_last: dict[str, float] = {}
        self.user_room: str | None = None
        self.utterances_truncated = 0
        # соль потоков ГСЧ — часть дома; до выбора дома (и для синтетики) — заданная часть или tune
        self.salt = cfg.split if cfg.split in ("tune", "holdout") else "tune"

    def rng(self, name: str) -> random.Random:
        """Поток ГСЧ шага генерации; соль — часть дома (tune | holdout), см. описание модуля."""
        return random.Random(f"daygen/{self.salt}/{self.cfg.seed}/{name}")

    # ------------------------------------------------------------------ дом и трасса
    def load_home(self) -> None:
        cfg = self.cfg
        if cfg.source == "synthetic":
            self.home, self.split = "synthetic", cfg.split if cfg.split in ("tune", "holdout") else "tune"
            self.salt = self.split
            self.date = cfg.date or "synthetic"
            self.records = synth.synthetic_casas_day(self.rng("synthetic-home"), self.date)
            locs = {k: set(v) for k, v in synth.SYNTH_LOCATIONS.items()}
            self.hour_w = synth.synthetic_hour_weights()
            self.casas_meta: dict = {}
        else:
            subset = Path(cfg.subset_dir) if cfg.subset_dir else casas_mod.env_subset_dir()
            index = casas_mod.load_index(subset)
            homes = index["homes"]
            sched_date = None
            if cfg.home:
                if cfg.home not in homes:
                    raise KeyError(f"дома {cfg.home} нет в подмножестве")
                home = cfg.home
            else:
                home, sched_date = schedule_pair(index, cfg.split, cfg.seed)
            meta = homes[home]
            if cfg.split in ("tune", "holdout") and meta["split"] != cfg.split:
                raise ValueError(f"дом {home} относится к {meta['split']}, а не к {cfg.split}")
            self.salt = meta["split"]
            date = cfg.date or sched_date or self.rng("date").choice(meta["eligible_days"])
            if date not in meta["days"]:
                raise KeyError(f"{home}: нет полных суток {date}")
            self.home, self.split, self.date = home, meta["split"], date
            self.records = casas_mod.load_day(subset, home, date, index)
            locs = {k: set(v) for k, v in meta["locations"].items()}
            self.hour_w = meta["hour_weights_act"]
            self.casas_meta = {"subset_sha256": meta["sha256"], "zip_sha256": index["source"]["sha256"],
                               "doi": index["source"]["doi"]}
        self.devices, self.rooms = room.build_inventory(locs)
        self.dev = room.device_index(self.devices)
        self.state = room.default_state(self.devices)

    # ------------------------------------------------------------------ события
    def _push(self, q: list, t: float, order: int, payload: Any) -> None:
        self._seq += 1
        heapq.heappush(q, _QItem(round(t, 3), order, self._seq, payload))

    def _casas_change(self, r: casas_mod.Record) -> tuple[str, str, int] | None:
        """Сообщение CASAS → (устройство, свойство, значение) или None (числовые значения не используются)."""
        if r.sensor not in room.CASAS_LOCATIONS:
            return None
        loc_id, _ = room.CASAS_LOCATIONS[r.sensor]
        if r.is_motion:
            return f"pir_{loc_id}", "motion", 1 if r.value == "ON" else 0
        if r.is_contact:
            dev = "door_front" if r.sensor == "OutsideDoor" else f"contact_{loc_id}"
            return dev, "open", 1 if r.value == "OPEN" else 0
        return None

    def _emit_sensor(self, t: float, dev: str, prop: str, value: Any, source: str) -> None:
        ev = {"t": round(t, 3), "kind": "sensor", "device": dev, "property": prop, "value": value,
              "source": source}
        self.events.append(ev)
        self.sensor_log.append(ev)

    def _set(self, dev: str, prop: str, value: Any) -> bool:
        if self.state[dev].get(prop) == value:
            return False
        self.state[dev][prop] = value
        return True

    def _motion_room(self, dev: str) -> str:
        return self.dev[dev]["location"]

    def build(self) -> tuple[dict, list[dict]]:
        self.load_home()
        q: list[_QItem] = []
        # 1. трасса CASAS: до окна — в начальное состояние, в окне — в очередь
        self.pre_motion_t: float | None = None     # последнее движение до окна (для предикатов тишины)
        self.pre_door_t: list[float] = []          # активность у двери до окна
        door_proxy = "door_front" if "door_front" in self.dev else "pir_entrance"
        for r in self.records:
            ch = self._casas_change(r)
            if ch is None or ch[0] not in self.dev:
                continue
            if r.t < self.w0:
                dev, prop, val = ch
                if not self._set(dev, prop, val):
                    continue
                if prop == "motion" and val == 1:
                    self.user_room = self._motion_room(dev)
                    self.pre_motion_t = r.t
                if dev == door_proxy and (dev == "door_front" or val == 1):
                    self.pre_door_t.append(r.t)
            elif r.t < self.w1:
                self._push(q, r.t, 0, ("casas", ch))
        # 2. синтетика
        self._synthetic(q)
        self.initial_state = copy.deepcopy(self.state)
        self.initial_user_room = self.user_room
        # 3. реплики пользователя
        for u in self._utterances():
            self._push(q, u.t, 2, ("utter", u))
        # 4. проход по времени
        self._run(q)
        # 5. ситуации
        sits = situations.detect(self)
        self.events += sits
        return self._finish()

    def _synthetic(self, q: list) -> None:
        cfg, w0, w1 = self.cfg, self.w0, self.w1
        thermo = next(d for d in self.devices if d["type"] == "climate_sensor")
        series = synth.climate_series(self.rng("climate"), w0, w1)
        for prop in ("temperature", "humidity", "pressure"):
            res = thermo["properties"][prop]["resolution"]
            pts = synth.quantize_series([(t, v[prop]) for t, v in series], res)
            self.state[thermo["id"]][prop] = pts[0][1]
            for t, v in pts[1:]:
                self._push(q, t, 0, ("synth", (thermo["id"], prop, v)))
        n = cfg.synthetic_situations
        kinds = ["overheat" if i % 2 == 0 else "leak" for i in range(n)]
        times = synth.pick_episode_times(self.rng("episodes"), n, w0, w1)
        self.rng("episodes-order").shuffle(kinds)
        overheats = [t for t, k in zip(times, kinds, strict=True) if k == "overheat"]
        leaks = [t for t, k in zip(times, kinds, strict=True) if k == "leak"]
        boiler, self.overheat_t = synth.boiler_series(self.rng("boiler"), w0, w1, overheats)
        pts = synth.quantize_series(boiler, 0.1)
        self.state["boiler"]["temperature"] = pts[0][1]
        for t, v in pts[1:]:
            self._push(q, t, 0, ("synth", ("boiler", "temperature", v)))
        leak_dev = next(d["id"] for d in self.devices if d["type"] == "leak_sensor")
        self.leak_t = []
        for t in leaks:
            self._push(q, t, 0, ("synth", (leak_dev, "wet", 1)))
            self.leak_t.append(round(t, 3))
            if t + synth.LEAK_DURATION_S < w1:
                self._push(q, t + synth.LEAK_DURATION_S, 0, ("synth", (leak_dev, "wet", 0)))

    def _utterances(self) -> list[Utterance]:
        rng = self.rng("sessions")
        cpd, cps = PROFILES[self.cfg.profile]
        hours = [h for h in range(24) if h * 3600 < self.w1 and (h + 1) * 3600 > self.w0]
        weights = [self.hour_w[h] * (min((h + 1) * 3600, self.w1) - max(h * 3600, self.w0)) / 3600
                   for h in hours]
        if sum(weights) <= 0:
            weights = [1.0] * len(hours)
        out: list[Utterance] = []
        # последняя реплика — не позже чем за 60 с до конца окна (агенту есть время ответить); в окне ≤ 2 мин — за 1 с
        limit = self.w1 - 60.0 if self.w1 - self.w0 > 120 else self.w1 - 1.0
        # профиль задаёт число реплик на окно дня («активный день»); для доли суток — rate_scale
        for s in range(poisson(rng, cpd * self.cfg.rate_scale / cps)):
            n = 1
            while rng.random() > 1.0 / cps:       # геометрическое число команд в сессии, среднее cps
                n += 1
            gaps = [min(max(math.exp(math.log(PAUSE_MEDIAN_S) + rng.gauss(0, PAUSE_SIGMA)), PAUSE_MIN_S),
                        PAUSE_MAX_S) for _ in range(n - 1)]
            dur = sum(gaps)
            h = rng.choices(hours, weights)[0]
            start = max(self.w0, h * 3600) + rng.random() * (min((h + 1) * 3600, self.w1) - max(self.w0, h * 3600))
            # сессия целиком в окне: сдвиг начала вместо отбрасывания хвоста (иначе среднее < cpd)
            start = max(self.w0 + 1.0, min(start, limit - dur))
            t = start
            for i in range(n):
                out.append(Utterance(round(t, 3), s, i))
                if i < n - 1:
                    t += gaps[i]
        out.sort(key=lambda u: (u.t, u.session, u.turn))
        # реплики разных сессий не совпадают по времени (иначе привязка ответов к входам неоднозначна)
        for i in range(1, len(out)):
            if out[i].t <= out[i - 1].t:
                out[i] = Utterance(round(out[i - 1].t + 1.0, 3), out[i].session, out[i].turn)
        # сессия длиннее окна (короткое окно, большой профиль без --rate-scale) и сдвиги на 1 с не выходят за
        # окно: хвост отбрасывается (число отброшенных — в counts.utterances_truncated заголовка)
        kept = [u for u in out if u.t <= limit]
        self.utterances_truncated = len(out) - len(kept)
        out = kept
        # номера сессий — в порядке времени их начала
        first: dict[int, float] = {}
        for u in out:
            first.setdefault(u.session, u.t)
        renum = {s: i + 1 for i, s in enumerate(sorted(first, key=lambda s: (first[s], s)))}
        return [Utterance(u.t, renum[u.session], u.turn + 1) for u in out]

    def _run(self, q: list[_QItem]) -> None:
        r_manual = self.rng("manual")
        r_kind = self.rng("utter-kind")
        r_cmd = self.rng("commands")
        r_q = self.rng("questions")
        r_k = self.rng("k")
        r_lang = self.rng("lang")
        while q:
            it = heapq.heappop(q)
            t = it.t
            kind, payload = it.payload
            if kind == "timer":
                dev, values, gen = payload
                if self.action_gen.get(dev) != gen:
                    continue
                for p, v in values.items():
                    self._set(dev, p, v)
                continue
            if kind in ("casas", "synth", "manual"):
                dev, prop, val = payload
                if not self._set(dev, prop, val):
                    continue
                src = {"casas": f"casas:{self.home}", "synth": "synthetic", "manual": "manual"}[kind]
                self._emit_sensor(t, dev, prop, val, src)
                if prop == "motion" and val == 1:
                    new_room = self._motion_room(dev)
                    if self.cfg.manual_switch and new_room != self.user_room:
                        self._manual_switch(q, r_manual, t, self.user_room, new_room)
                    self.user_room = new_room
                continue
            if kind == "utter":
                u: Utterance = payload
                lang = self.cfg.lang if self.cfg.lang != "mix" else r_lang.choice(["ru", "en"])
                k = sample_k(r_k)
                ev = None
                if r_kind.random() < self.cfg.question_share:
                    ev = make_question(QuestionContext(self, t), r_q, lang)
                if ev is None:
                    ev = self._command(q, r_cmd, t, lang)
                if ev is None:
                    continue
                ev.update({"t": t, "lang": lang, "session": u.session, "turn": u.turn, "k": k})
                self.events.append(ev)

    def _manual_switch(self, q: list, rng: random.Random, t: float, old: str | None, new: str) -> None:
        local = t % 86400
        mode = "dark" if (local < self.w0 + 3600 or local >= DARK_EVENING_S) else "day"
        p_on, p_off = MANUAL_P[mode]
        on_dev, off_dev = f"light_{new}", f"light_{old}" if old else None
        u_on, u_off = rng.random(), rng.random()   # оба числа берутся всегда — поток не зависит от состояния
        if t + MANUAL_DELAY_S >= self.w1:
            return
        if on_dev in self.dev and u_on < p_on and not self.state[on_dev]["on"] \
                and t - self.manual_last.get(on_dev, -1e9) >= MANUAL_MIN_GAP_S:
            self.manual_last[on_dev] = t
            self._push(q, t + MANUAL_DELAY_S, 0, ("manual", (on_dev, "on", True)))
        if off_dev and off_dev in self.dev and u_off < p_off and self.state[off_dev]["on"] \
                and t - self.manual_last.get(off_dev, -1e9) >= MANUAL_MIN_GAP_S:
            self.manual_last[off_dev] = t
            self._push(q, t + MANUAL_DELAY_S, 0, ("manual", (off_dev, "on", False)))

    # ------------------------------------------------------------------ команды
    def _command(self, q: list, rng: random.Random, t: float, lang: str) -> dict | None:
        ctx = CommandContext(self, t)
        trap = rng.random() < self.cfg.trap_share
        cmd = make_command(ctx, rng, lang, trap)
        if cmd is None:
            return None
        for call in cmd["expect"].get("calls", []):
            if call["name"] == "invoke_action":
                dev = call["args"]["device"]
                self.action_gen[dev] = self.action_gen.get(dev, 0) + 1
        for t_end, dev, values in cmd.pop("_timers", []):
            self._push(q, t_end, 0, ("timer", (dev, values, self.action_gen.get(dev, 0))))
        return cmd

    # ------------------------------------------------------------------ итог
    def _finish(self) -> tuple[dict, list[dict]]:
        self.events.sort(key=lambda e: (e["t"], KIND_ORDER[e["kind"]]))
        counters = {"user_cmd": 0, "question": 0, "situation": 0}
        prefix = {"user_cmd": "u", "question": "q", "situation": "s"}
        for e in self.events:
            if e["kind"] in counters:
                counters[e["kind"]] += 1
                e["id"] = f"{prefix[e['kind']]}{counters[e['kind']]}"
        cfg = self.cfg
        cpd, cps = PROFILES[cfg.profile]
        counts = {"sensor": sum(e["kind"] == "sensor" for e in self.events), **counters,
                  "utterances_truncated": self.utterances_truncated}
        by_class: dict[str, int] = {}
        for e in self.events:
            if e["kind"] in ("user_cmd", "question"):
                by_class[e["class"]] = by_class.get(e["class"], 0) + 1
        header = {
            "kind": "header", "format": FORMAT,
            "generator": {"name": "bench.daygen", "version": GENERATOR_VERSION},
            "seed": cfg.seed, "profile": cfg.profile,
            "profile_params": {"cmd_per_day": cpd, "cmd_per_session": cps},
            "source": cfg.source, "home": self.home, "split": self.split, "date": self.date,
            "window": list(cfg.window), "window_s": [self.w0, self.w1], "t_origin": "midnight",
            "lang": cfg.lang,
            "devices": self.devices,
            "rooms": room.room_table(self.rooms),
            "initial_state": self.initial_state,
            "initial_user_room": self.initial_user_room,
            "situation_rules": situations.rules(self),
            "counts": counts, "counts_by_class": dict(sorted(by_class.items())),
            "params": asdict(cfg),
        }
        if self.casas_meta:
            header["casas"] = self.casas_meta
        header["params"]["window"] = list(cfg.window)
        header["params"]["awake"] = list(cfg.awake)
        return header, self.events


def schedule_pair(index: dict, split: str, seed: int) -> tuple[str, str]:
    """(дом, сутки) для seed по расписанию части split (tune | holdout | any).

    Круг r = seed // H обходит все H домов части в своём перемешанном порядке (позиция seed % H); дом на
    r-м круге берёт r-е сутки из своего перемешанного списка пригодных суток. Поэтому подряд идущие seed
    дают разные дома и (пока у дома не кончились сутки) разные дни: без повторов для H × min(суток) подряд
    идущих seed (tune: 7 × 25 = 175, holdout: 7 × 9 = 63 — у hh121 9 суток; any: 14 × 9 = 126).
    Расписание зависит только от части и подмножества CASAS, а не от параметров нагрузки.
    """
    homes = sorted(h for h, m in index["homes"].items() if split == "any" or m["split"] == split)
    if not homes:
        raise ValueError(f"в части {split!r} нет домов")
    rnd, pos = divmod(seed, len(homes))
    order = list(homes)
    random.Random(f"daygen/schedule/{split}/round/{rnd}").shuffle(order)
    home = order[pos]
    days = sorted(index["homes"][home]["eligible_days"])
    random.Random(f"daygen/schedule/{split}/{home}").shuffle(days)
    return home, days[rnd % len(days)]


def poisson(rng: random.Random, lam: float) -> int:
    """Пуассон методом Кнута (λ ≤ ~700 без потери точности exp)."""
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def sample_k(rng: random.Random) -> int:
    x, acc = rng.random(), 0.0
    for k, p in K_DIST:
        acc += p
        if x < acc:
            return k
    return K_DIST[-1][0]


def generate_day(cfg: DayConfig) -> tuple[dict, list[dict]]:
    """Заголовок и события дня (события по возрастанию t)."""
    return DayBuilder(cfg).build()


def dumps(obj: Any) -> str:
    """Каноническая сериализация: ключи отсортированы, без пробелов, UTF-8 без \\u-экранирования."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_day(path: str | Path, header: dict, events: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(dumps(header) + "\n")
        for e in events:
            f.write(dumps(e) + "\n")


def read_day(path: str | Path) -> tuple[dict, list[dict]]:
    with open(path, encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    if not lines or lines[0].get("kind") != "header":
        raise ValueError(f"{path}: первая строка — не заголовок дня")
    return lines[0], lines[1:]
