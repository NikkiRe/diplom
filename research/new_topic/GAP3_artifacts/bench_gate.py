#!/usr/bin/env python3
"""Пилот GAP3: (1) самопроверка слоя actuator_gate на размеченном синтетическом потоке команд;
(2) задержка, которую добавляет проверка одной команды на хосте; (3) иллюстративная стоимость
отказов в ₽/сутки против LLM-ограждения (RoboGuard-подобного) по прайсу RouterAI из F_artifacts.

ВАЖНО про смысл чисел:
  * Доля отклонённых команд здесь = 100 % ПО ПОСТРОЕНИЮ: поток сгенерирован из той же спецификации,
    что и правила. Это проверка реализации, а не оценка полноты слоя. Полноту (долю небезопасных
    команд LLM, пойманных слоем) нужно мерить на независимых данных: команды, которые LLM реально
    сгенерировала на задачах HomeBench / SafeAgentBench / собственной трассе дня.
  * Задержка измерена на MacBook (Apple M4 Pro), CPython. На Raspberry Pi 5 её нужно перемерить.
  * Стоимость — иллюстрация с явно заданными допущениями, а не измерение.

Запуск: python3 bench_gate.py [N]   (по умолчанию N = 200000)
"""
from __future__ import annotations

import gc
import platform
import random
import statistics
import sys
import time
import tracemalloc
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "F_artifacts"))

from actuator_gate import Command, Gate, StateStore, demo_setup  # noqa: E402

CATS = {  # категория -> вес в потоке
    "valid": 0.55, "read_only": 0.10, "unknown_device": 0.03, "unknown_tool": 0.02,
    "invalid_args": 0.06, "stale_command": 0.04, "stale_state": 0.03, "interlock": 0.05,
    "clamp": 0.04, "rate_limit": 0.04, "needs_confirm": 0.02, "duplicate": 0.02,
}
EXPECT = {  # категория -> (вердикт, причина)
    "valid": ("allow", None), "read_only": ("allow", None),
    "unknown_device": ("reject", "unknown_device"), "unknown_tool": ("reject", "unknown_tool"),
    "invalid_args": ("reject", "invalid_args"), "stale_command": ("reject", "stale_command"),
    "stale_state": ("reject", "stale_state"), "interlock": ("reject", "interlock"),
    "clamp": ("clamp", None), "rate_limit": ("reject", "rate_limit"),
    "needs_confirm": ("confirm", "needs_confirm"), "duplicate": ("duplicate", "duplicate"),
}
DEFAULT_STATE = {"window_open": False, "temp_c": 21.0, "water_level_pct": 50.0, "human_near": False}


def pct(xs, q):
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(q / 100 * (len(xs) - 1)))))
    return xs[k]


class Generator:
    """Порождает команду нужной категории и обновления состояния; ведёт свою (независимую) запись
    принятых команд, чтобы «валидные» команды не упирались в ограничение частоты."""

    def __init__(self, seed=1):
        self.r = random.Random(seed)
        self.n = 0
        self.last = defaultdict(lambda: -1e9)   # device -> время последней принятой команды
        self.valve_hist = []                    # времена принятых команд valve
        self.accepted_ids = []

    def _id(self):
        self.n += 1
        return f"c{self.n}"

    def accepted(self, cmd, now):
        self.last[cmd.device] = now
        if cmd.device == "valve":
            self.valve_hist.append(now)
        self.accepted_ids.append(cmd)

    def valid(self, now, st):
        r = self.r
        opts = ["led", "fan", "door_lock"]
        if now - self.last["heater"] >= 30.0:
            opts.append("heater")
        if now - self.last["pump"] >= 10.0:
            opts.append("pump")
        self.valve_hist = [t for t in self.valve_hist if now - t < 60.0]
        if len(self.valve_hist) < 20 and now - self.last["valve"] >= 0:
            opts.append("valve")
        d = r.choice(opts)
        if d == "led":
            return Command(self._id(), "led", "set_color", {c: r.randint(0, 255) for c in "rgb"}, now - r.uniform(0, 3))
        if d == "fan":
            return Command(self._id(), "fan", "set_speed", {"pct": r.randint(0, 100)}, now - r.uniform(0, 3))
        if d == "door_lock":
            return Command(self._id(), "door", "lock", {}, now - r.uniform(0, 3))
        if d == "heater":
            return Command(self._id(), "heater", "set_power", {"on": r.random() < 0.5}, now - r.uniform(0, 3))
        if d == "pump":
            return Command(self._id(), "pump", "set_power", {"on": r.random() < 0.5}, now - r.uniform(0, 3))
        return Command(self._id(), "valve", "set_angle", {"deg": r.randint(0, 90)}, now - r.uniform(0, 3))

    def make(self, cat, now, st, stale_keys):
        r = self.r
        if cat == "valid":
            return self.valid(now, st)
        if cat == "read_only":
            if r.random() < 0.5:
                return Command(self._id(), "thermo", "read", {}, now)
            return Command(self._id(), "camera", "capture", {"w": r.choice([320, 640, 1280])}, now)
        if cat == "unknown_device":
            return Command(self._id(), r.choice(["oven", "garage", "heater2", "lamp_kitchen"]), "set_power", {"on": True}, now)
        if cat == "unknown_tool":
            return Command(self._id(), "heater", r.choice(["set_temp", "reboot", "set_mode"]), {"value": 25}, now)
        if cat == "invalid_args":
            return r.choice([
                Command(self._id(), "valve", "set_angle", {"deg": r.choice([-10, 91, 180, 360])}, now),
                Command(self._id(), "valve", "set_angle", {"deg": 45.5}, now),
                Command(self._id(), "fan", "set_speed", {"pct": "full"}, now),
                Command(self._id(), "led", "set_color", {"r": 300, "g": 0, "b": 0}, now),
                Command(self._id(), "heater", "set_power", {"on": True, "temp": 90}, now),
                Command(self._id(), "pump", "set_power", {}, now),
            ])
        if cat == "stale_command":
            return Command(self._id(), "led", "set_color", {"r": 1, "g": 2, "b": 3}, now - r.uniform(10.5, 120.0))
        if cat == "stale_state":
            stale_keys.append(r.choice(["window_open", "temp_c"]))
            return Command(self._id(), "heater", "set_power", {"on": True}, now)
        if cat == "interlock":
            if r.random() < 0.5:
                if r.random() < 0.5:
                    st["window_open"] = True
                else:
                    st["temp_c"] = r.uniform(30.0, 45.0)
                return Command(self._id(), "heater", "set_power", {"on": True}, now)
            st["water_level_pct"] = r.uniform(0.0, 9.9)
            return Command(self._id(), "pump", "set_power", {"on": True}, now)
        if cat == "clamp":
            st["human_near"] = True
            return Command(self._id(), "fan", "set_speed", {"pct": r.randint(31, 100)}, now)
        if cat == "rate_limit":
            # возможен только если недавно была принята команда реле; иначе вернём None (категория пропускается)
            if now - self.last["heater"] < 30.0:
                return Command(self._id(), "heater", "set_power", {"on": False}, now)
            if now - self.last["pump"] < 10.0:
                return Command(self._id(), "pump", "set_power", {"on": False}, now)
            return None
        if cat == "needs_confirm":
            return Command(self._id(), "door", "unlock", {}, now)
        if cat == "duplicate":
            if not self.accepted_ids:
                return None
            old = r.choice(self.accepted_ids[-200:])
            return old
        raise ValueError(cat)


def run(n=200_000, seed=1, dt=0.5):
    devices, interlocks = demo_setup()
    state = StateStore()
    gate = Gate(devices, interlocks, state)
    gen = Generator(seed)
    r = random.Random(seed + 1)
    cats, weights = zip(*CATS.items())
    lat = defaultdict(list)       # категория -> [нс]
    tick_lat = []
    got = Counter()
    mismatch = Counter()
    now = 0.0
    examples = {}
    for i in range(n):
        now += dt
        cat = r.choices(cats, weights)[0]
        st = dict(DEFAULT_STATE)
        stale = []
        cmd = gen.make(cat, now, st, stale)
        if cmd is None:
            continue
        for k, v in st.items():
            state.set(k, v, now - (10.0 if k in stale else 0.0))
        t0 = time.perf_counter_ns()
        dec = gate.check(cmd, now)
        t1 = time.perf_counter_ns()
        lat[cat].append(t1 - t0)
        t2 = time.perf_counter_ns()
        gate.tick(now)
        tick_lat.append(time.perf_counter_ns() - t2)
        if dec.verdict in ("allow", "clamp") and cmd.tool not in devices[cmd.device].read_only:
            gen.accepted(cmd, now)
        exp = EXPECT[cat]
        got[(cat, dec.verdict, dec.reason)] += 1
        if (dec.verdict, dec.reason) != exp:
            mismatch[(cat, dec.verdict, dec.reason)] += 1
            examples.setdefault((cat, dec.verdict, dec.reason), (cmd, dec))
    return gate, lat, tick_lat, got, mismatch, examples


def gate_memory_kib(n, seed=7):
    """Память, удерживаемая самим слоем после n шагов: трассировка с начала прогона, снимок после
    возврата, когда живы только структуры слоя (данные стенда и генератора уже освобождены)."""
    gc.collect()
    tracemalloc.start(5)
    gate = run(n, seed=seed)[0]
    gc.collect()
    snap = tracemalloc.take_snapshot().filter_traces(
        [tracemalloc.Filter(True, "*actuator_gate.py"), tracemalloc.Filter(True, "*argschema.py")])
    kib = sum(st.size for st in snap.statistics("lineno")) / 1024
    tracemalloc.stop()
    del gate
    return kib


def unit_tests():
    devices, interlocks = demo_setup()
    st = StateStore()
    g = Gate(devices, interlocks, st)
    for k, v in DEFAULT_STATE.items():
        st.set(k, v, 0.0)
    # dry-run -> состояние изменилось -> commit отклоняется (TOCTOU)
    dec, tok = g.dry_run(Command("t1", "pump", "set_power", {"on": True}, 0.0), 0.0)
    assert dec.verdict == "allow" and tok is not None
    st.set("water_level_pct", 5.0, 1.0)
    assert g.commit(tok, 1.0).reason == "interlock"
    # commit после окна фиксации
    st.set("water_level_pct", 50.0, 1.0)
    dec, tok = g.dry_run(Command("t2", "pump", "set_power", {"on": True}, 1.0), 1.0)
    assert g.commit(tok, 1.0 + g.commit_window_s + 0.1).reason == "commit_expired"
    # dry-run не расходует лимит частоты и не помечает ключ
    for k, v in DEFAULT_STATE.items():
        st.set(k, v, 2.0)
    assert g.check(Command("t3", "pump", "set_power", {"on": True}, 2.0), 2.0).verdict == "allow"
    assert g.check(Command("t3", "pump", "set_power", {"on": True}, 2.0), 2.5).verdict == "duplicate"
    assert g.check(Command("t4", "pump", "set_power", {"on": False}, 3.0), 3.0).reason == "rate_limit"
    # аренда: вентилятор без продления уходит в безопасное состояние через lease_s
    assert g.check(Command("t5", "fan", "set_speed", {"pct": 20}, 3.0), 3.0).verdict == "allow"
    assert g.tick(4.0) == [] and g.renew("fan", 4.0)
    ev = g.tick(6.5)
    assert ev and ev[0][1] == "fan" and ev[0][2] == {"pct": 0}
    # подтверждение
    assert g.check(Command("t6", "door", "unlock", {}, 7.0), 7.0).verdict == "confirm"
    assert g.check(Command("t7", "door", "unlock", {}, 7.0, confirmed=True), 7.0).verdict == "allow"
    # clamp рядом с человеком
    st.set("human_near", True, 8.0)
    d = g.check(Command("t8", "fan", "set_speed", {"pct": 90}, 8.0), 8.0)
    assert d.verdict == "clamp" and d.args == {"pct": 30}
    # неизвестное состояние = опасное
    g2 = Gate(devices, interlocks, StateStore())
    assert g2.check(Command("t9", "heater", "set_power", {"on": False}, 0.0), 0.0).reason == "stale_state"
    print("unit tests: OK (dry-run/commit TOCTOU, commit window, idempotency, rate limit, lease watchdog, "
          "confirm, clamp, unknown state = unsafe)")


def cost_model():
    """Иллюстрация (НЕ измерение): цена отказов при детерминированном слое против LLM-ограждения."""
    from price_table import call_cost_rub
    models = ["openai/gpt-4o-mini", "openai/gpt-4.1-mini", "openai/gpt-5-nano", "openai/gpt-5-mini"]
    print("\n=== Иллюстрация стоимости (допущения в коде) ===")
    # Перепланирование после отказа: +1 вызов LLM с тем же префиксом (3000 вход, из них 2000 из кэша)
    # и ответом 150 токенов (+300 скрытых reasoning у gpt-5-*), как в F_artifacts/price_table.py.
    # LLM-ограждение по образцу RoboGuard: +1 вызов на каждый план, 4329.6 токена на вызов
    # (Ravichandran et al., Table III); доля выхода неизвестна -> допущение 4000 вход без кэша + 330 выход.
    print("Отказ -> перепланирование (+1 вызов). Команд актуаторам в сутки: 100; доля отказов r.")
    print(f"{'model':16s} {'вызов,₽':>8s} | {'r=2%':>7s} {'r=5%':>7s} {'r=20%':>7s} ₽/сут | LLM-guard 100 планов/сут, ₽")
    for m in models:
        reasoning = m.startswith("openai/gpt-5")
        out = 150 + (300 if reasoning else 0)
        c = call_cost_rub(m, 3000, 2000, out)
        g = call_cost_rub(m, 4000, 0, 330 + (300 if reasoning else 0))
        row = " ".join(f"{100 * rr * c:7.3f}" for rr in (0.02, 0.05, 0.20))
        print(f"{m[7:]:16s} {c:8.4f} | {row}      | {100 * g:7.2f} (вызов {g:.4f} ₽)")


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200_000
    print(f"python {platform.python_version()} | {platform.platform()} | {platform.machine()} | gc enabled={gc.isenabled()}")
    unit_tests()
    # Память — в чистом состоянии процесса, до основного прогона (иначе tracemalloc видит остатки).
    gate_kib = gate_memory_kib(20_000)
    gate_kib_long = gate_memory_kib(100_000)
    gc.collect()
    t = time.perf_counter()
    gate, lat, tick_lat, got, mismatch, examples = run(n)
    wall = time.perf_counter() - t
    total = sum(len(v) for v in lat.values())
    print(f"\nПоток: {total} команд (из {n} шагов; пропущены шаги, где категорию нельзя было построить), "
          f"прогон {wall:.1f} с без tracemalloc; память структур слоя (tracemalloc): {gate_kib:.0f} КиБ после 20k шагов, {gate_kib_long:.0f} КиБ после 100k шагов")
    print(f"Несовпадений с ожидаемым вердиктом: {sum(mismatch.values())}")
    for k, v in mismatch.most_common(10):
        print("  MISMATCH", k, v, examples[k])
    print("\nЗадержка gate.check, мкс (perf_counter_ns, M4 Pro, CPython):")
    print(f"{'категория':16s} {'n':>7s} {'p50':>7s} {'p95':>7s} {'p99':>7s} {'p99.9':>7s} {'max':>8s}")
    allx = []
    for cat in CATS:
        xs = lat.get(cat, [])
        if not xs:
            continue
        allx += xs
        print(f"{cat:16s} {len(xs):7d} {pct(xs, 50)/1e3:7.2f} {pct(xs, 95)/1e3:7.2f} {pct(xs, 99)/1e3:7.2f} "
              f"{pct(xs, 99.9)/1e3:7.2f} {max(xs)/1e3:8.1f}")
    print(f"{'ВСЕ':16s} {len(allx):7d} {pct(allx, 50)/1e3:7.2f} {pct(allx, 95)/1e3:7.2f} {pct(allx, 99)/1e3:7.2f} "
          f"{pct(allx, 99.9)/1e3:7.2f} {max(allx)/1e3:8.1f}   mean {statistics.fmean(allx)/1e3:.2f}")
    print(f"gate.tick (сторожевой таймер аренды): p50 {pct(tick_lat, 50)/1e3:.2f} мкс, p99 {pct(tick_lat, 99)/1e3:.2f} мкс; "
          f"событий безопасного состояния: {gate.safe_count}")
    unsafe = [c for c in CATS if EXPECT[c][0] in ("reject", "confirm")]
    n_unsafe = sum(len(lat[c]) for c in unsafe)
    n_caught = sum(v for (c, verd, _), v in got.items() if c in unsafe and verd in ("reject", "confirm"))
    n_safe = sum(len(lat[c]) for c in ("valid", "read_only"))
    n_fr = sum(v for (c, verd, _), v in got.items() if c in ("valid", "read_only") and verd in ("reject", "confirm"))
    print(f"\nМетрика (по построению, самопроверка): поймано {n_caught}/{n_unsafe} недопустимых "
          f"({100 * n_caught / max(1, n_unsafe):.1f} %), ложных отказов {n_fr}/{n_safe}")
    cost_model()


if __name__ == "__main__":
    main()
