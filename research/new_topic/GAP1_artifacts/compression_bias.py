"""Смещение стоимости при сжатии времени и протокол сжатия с учётом TTL кэша (GAP1, §6–§7).

СОБСТВЕННАЯ МОДЕЛЬ-ИЛЛЮСТРАЦИЯ, не измерение. Цель — показать знак и порядок смещения ₽/сут и доли
кэш-попаданий, если «день работы» агента прогонять ускоренно, и проверить, что предложенная схема
сжатия с учётом TTL (TTL-aware: возраст префикса по уровням + принудительный сброс «солью» + прореживание)
это смещение убирает.

Что берётся из источников (подробно в GAP1.md):
  * пользовательские запросы — кластеры Bentley et al. 2018 (IMWUT 2(3):91, табл. 6): команд/сут и команд/сессию;
    VisionClaw (arXiv:2604.03486 v2, §6.2): 10,1 взаимодействия/активный день, 555/118 ≈ 4,7 на сессию,
    глубина цепочки инструментов 21 % / 27 % / 23 % / 29 % (0 / 1 / 2–3 / ≥4 инструмента);
  * события датчиков — РЕАЛЬНЫЕ моменты начала активностей CASAS (casas_act_days.json, 497 дом-суток);
  * суточный профиль запросов — часовые доли начала активностей CASAS (casas_rates_output.json);
  * TTL кэша — документация OpenAI/Azure (06.10.2026): in_memory «около 5–10 мин бездействия»,
    GPT-5.6+ «не менее 30 мин после последней записи или использования», запись 1,25×;
  * цены — снимок RouterAI F_artifacts/routerai_models_2026-10-06.json (₽ за 1M токенов).
ДОПУЩЕНИЯ (помечены в коде): структура промпта (S=4000 и т. д., как в D §9.3), паузы внутри сессии
(логнормаль, медиана 90 с, обрезка [5, 600] с), 4 с между вызовами внутри команды, TTL in_memory ~ U(300, 600) с
независимо на каждую проверку, геометрическое число команд в сессии, активные часы 07–23.

Запуск: python3 compression_bias.py [дней=300] [seed=1]
"""
import json, math, os, random, statistics as st, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DAY0, DAY1 = 7 * 3600, 23 * 3600            # ДОПУЩЕНИЕ: активные часы агента 07:00–23:00 (16 ч)
MIN_CACHE, GRAN = 1024, 128
INF = float('inf')

# ₽ за 1 токен: (вход, чтение кэша, запись кэша или None, выход, класс TTL)
PRICES = {
    'gpt-4o-mini':  (16.5615255e-6, 8.28076275e-6, None, 66.246102e-6, 'in_memory'),
    'gpt-5-mini':   (27.6025425e-6, 2.76025425e-6, None, 220.82034e-6, 'in_memory'),
    'gpt-5.4-mini': (82.8076275e-6, 8.28076275e-6, None, 496.845765e-6, 'in_memory'),
    'gpt-6-luna':   (11.041017e-6, 1.1041017e-6, 13.80127125e-6, 55.205085e-6, '30m'),
}
TTL_LO = {'in_memory': 300.0, '30m': 1800.0}    # ниже — попадание гарантировано (в модели)
TTL_HI = {'in_memory': 600.0, '30m': 1800.0}    # не ниже — промах гарантирован (в модели)

# ДОПУЩЕНИЕ: структура промпта, токены (как D §9.3)
S, P, SNAP, U, O, R = 4000, 300, 600, 60, 150, 300
SENSOR_IN, SENSOR_OUT = 400, 100            # событие датчика: S + описание события и среза состояния
HB_IN, HB_OUT, HB_PERIOD = 1000, 20, 1800   # heartbeat (изолированный, как OpenClaw isolatedSession)
CALL_SPACING = 4.0                          # ДОПУЩЕНИЕ: с между вызовами LLM внутри одной команды
K_DIST = [(1, .21), (2, .27), (3, .23), (5, .29)]   # вызовов LLM на команду, из VisionClaw (GAP1 §2.1)

PROFILES = {  # команд/сут, команд/сессию
    'light (Bentley)':   (2.61, 2.42),
    'medium (Bentley)':  (5.55, 2.74),
    'VisionClaw':        (10.1, 4.7),
    'super (Bentley)':   (30.44, 5.40),
}


def p_hit_true(cls, a):
    """Истинная (в модели) вероятность попадания при возрасте префикса a, с."""
    if cls == '30m':
        return 1.0 if a < 1800 else 0.0
    return min(1.0, max(0.0, (600.0 - a) / 300.0))


def load_hour_weights():
    try:
        agg = json.load(open(os.path.join(HERE, 'casas_rates_output.json')))['aggregate']['act']['hour_share_mean']
    except OSError:
        agg = [1.0] * 24
    w = [agg[h] if DAY0 // 3600 <= h < DAY1 // 3600 else 0.0 for h in range(24)]
    return [x / sum(w) for x in w]


def load_sensor_days():
    d = json.load(open(os.path.join(HERE, 'casas_act_days.json')))['homes']
    return [[t for t in ts if DAY0 <= t < DAY1] for h in d.values() for ts in h.values()]


def poisson(rng, lam):
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L: return k
        k += 1


def sample_k(rng):
    x, acc = rng.random(), 0.0
    for k, p in K_DIST:
        acc += p
        if x < acc: return k
    return K_DIST[-1][0]


def gen_day(rng, profile, with_sensors, with_hb, hour_w, sensor_days):
    """Список событий (t, kind, payload), отсортированный по t (секунды от полуночи)."""
    ev = []
    if profile:
        cpd, cps = PROFILES[profile]
        for _ in range(poisson(rng, cpd / cps)):
            h = rng.choices(range(24), hour_w)[0]
            t = h * 3600 + rng.random() * 3600
            n_cmd = 1
            while rng.random() > 1.0 / cps: n_cmd += 1            # ДОПУЩЕНИЕ: геометрическое, среднее cps
            sid = (t, rng.random())
            for j in range(n_cmd):
                if t >= DAY1: break
                ev.append((t, 'user', (sid, sample_k(rng))))
                g = math.exp(math.log(90) + rng.gauss(0, 1.0))     # ДОПУЩЕНИЕ: пауза внутри сессии
                t += min(max(g, 5.0), 600.0)
    if with_sensors:
        ev += [(t, 'sensor', None) for t in rng.choice(sensor_days)]
    if with_hb:                       # with_hb: период heartbeat в секундах (0/False — нет heartbeat)
        period = HB_PERIOD if with_hb is True else with_hb
        t = DAY0 + period
        while t < DAY1:
            ev.append((t, 'hb', None)); t += period
    ev.sort(key=lambda e: e[0])
    return ev


def expand_calls(events):
    """События -> вызовы LLM: (индекс события, смещение внутри события, тип, вход, выход, сессия, позиция)."""
    calls, hist = [], {}
    for i, (t, kind, pl) in enumerate(events):
        if kind == 'user':
            sid, k = pl
            H, turn = hist.get(sid, 0), U
            for c in range(k):
                calls.append((i, c * CALL_SPACING, 'user', S + P + H + SNAP + turn, O, sid, c, S + P + H))
                turn += (O + R) if c < k - 1 else O
            hist[sid] = H + turn
        elif kind == 'sensor':
            calls.append((i, 0.0, 'sensor', S + SENSOR_IN, SENSOR_OUT, None, 0, None))
        else:
            calls.append((i, 0.0, 'hb', S + HB_IN, HB_OUT, None, 0, None))
    return calls


def call_times(events, calls, gap_map):
    """Новые моменты вызовов: паузы МЕЖДУ событиями отображаются gap_map, смещения внутри события
    (задержка LLM и инструментов) остаются реальными — их нельзя сжать."""
    ev_t, prev, cur = [], None, None
    for (t, _, _) in events:
        cur = t if prev is None else cur + gap_map(t - prev)
        ev_t.append(cur); prev = t
    return [ev_t[c[0]] + c[1] for c in calls]


def ages(calls, times):
    """Возраст префиксов на каждом вызове: (уровень 0 — общий S; уровень 1 — цепочка сессии)."""
    last_any, chain_last, out = None, {}, []
    for c, t in zip(calls, times):
        a0 = INF if last_any is None else t - last_any
        a1 = (t - chain_last[c[5]]) if (c[2] == 'user' and c[5] in chain_last) else None
        out.append((a0, a1))
        last_any = t
        if c[2] == 'user': chain_last[c[5]] = t
    return out


def price(calls, times, model, rng, forced=None):
    """₽ за сутки. forced[n] = (сброс уровня 0, сброс уровня 1) перед вызовом n — «соль» в начале
    префикса (уровень 0) или в начале блока истории (уровень 1)."""
    p_in, p_cr, p_cw, p_out, cls = PRICES[model]
    last_any, chain_last, last_inp, cmd_hist = -INF, {}, {}, {}
    unc = cac = out = wr = 0
    for n, (c, t) in enumerate(zip(calls, times)):
        _, _, kind, inp, o, sid, pos, hist_prefix = c
        if forced and forced[n][0]:
            last_any, chain_last = -INF, {}
        if forced and forced[n][1] and sid in chain_last:
            del chain_last[sid]
        draw = lambda: rng.uniform(300, 600) if cls == 'in_memory' else 1800.0
        hit = S if t - last_any < draw() else 0
        if kind == 'user' and sid in chain_last and t - chain_last[sid] < draw():
            # внутри команды общий префикс — весь предыдущий вызов; между командами — S+P+история до
            # предыдущей команды (снимок оборудования стоит после истории, D §9.3)
            hit = max(hit, last_inp[sid] if pos > 0 else cmd_hist[sid])
        hit = 0 if hit < MIN_CACHE else min((hit // GRAN) * GRAN, inp)
        cac += hit; unc += inp - hit; out += o
        if inp >= MIN_CACHE: wr += inp - hit
        last_any = t
        if kind == 'user':
            chain_last[sid], last_inp[sid] = t, inp
            if pos == 0: cmd_hist[sid] = hist_prefix      # S+P+история до этой команды
    if p_cw is None:
        cost = unc * p_in + cac * p_cr + out * p_out
    else:  # GPT-5.6+, implicit-режим: некэшированный вход вызовов >= 1024 токенов записывается по 1,25×
        cost = (unc - wr) * p_in + wr * p_cw + cac * p_cr + out * p_out
    return cost, cac / max(1, cac + unc)


def ttl_aware_forced(calls, real_t, comp_t, model, rng, mode):
    """Протокол TTL-aware. Для каждого уровня префикса сравниваются реальный возраст a и сжатый a' (a' <= a).
       a' == a          -> ничего не делаем (паузу не сжимали, исход «естественный»);
       a >= TTL_HI      -> сброс (гарантированный промах в реальном времени);
       a <  TTL_LO      -> ничего (a' < a < TTL_LO, попадание и так);
       иначе (зона неопределённости): mode='mc' — сброс с вероятностью 1 - p(a)/p(a') (прореживание по
       калибровочной кривой p), 'lo' — без сброса (нижняя граница ₽), 'hi' — сброс (верхняя граница ₽)."""
    cls = PRICES[model][4]
    lo, hi = TTL_LO[cls], TTL_HI[cls]
    ra, ca = ages(calls, real_t), ages(calls, comp_t)

    def need_reset(a, a2):
        if a is None or a2 is None or a == a2: return False
        if a >= hi: return True
        if a < lo: return False
        if mode == 'lo': return False
        if mode == 'hi': return True
        p_a, p_a2 = p_hit_true(cls, a), p_hit_true(cls, a2)
        return rng.random() < (1.0 - p_a / p_a2 if p_a2 > 0 else 1.0)

    forced = []
    for (a0, a1), (b0, b1) in zip(ra, ca):
        f0 = need_reset(a0, b0)
        f1 = (not f0) and need_reset(a1, b1)
        forced.append((f0, f1))
    return forced


SCHEMES = ['real', 'x10', 'x60', 'skip', 'ttl-mc', 'ttl-lo', 'ttl-hi']
G0 = 60.0   # паузы короче G0 воспроизводятся в реальном времени (очереди, перекрытие с задержкой LLM)


def run_scheme(sch, ev, calls, model, rng):
    real_t = call_times(ev, calls, lambda g: g)
    if sch == 'real':
        return price(calls, real_t, model, rng) + (span(real_t),)
    if sch in ('x10', 'x60'):
        k = 10.0 if sch == 'x10' else 60.0
        t = call_times(ev, calls, lambda g: g / k)
        return price(calls, t, model, rng) + (span(t),)
    if sch == 'skip':                       # «следующее событие сразу» (DES без привязки к часам)
        t = call_times(ev, calls, lambda g: min(g, 5.0))
        return price(calls, t, model, rng) + (span(t),)
    t = call_times(ev, calls, lambda g: g if g <= G0 else G0)
    forced = ttl_aware_forced(calls, real_t, t, model, rng, sch.split('-')[1])
    return price(calls, t, model, rng, forced) + (span(t),)


def span(ts):
    return (max(ts) - min(ts)) / 60.0 if ts else 0.0


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    hour_w, sensor_days = load_hour_weights(), load_sensor_days()
    scenarios = [
        ('только heartbeat 30 мин (пустой агент)', None, False, True),
        ('только heartbeat 29 мин (пустой агент)', None, False, 29 * 60),
        ('пользователь medium', 'medium (Bentley)', False, False),
        ('medium + датчики CASAS + heartbeat', 'medium (Bentley)', True, True),
        ('VisionClaw + датчики + heartbeat', 'VisionClaw', True, True),
        ('super + датчики + heartbeat', 'super (Bentley)', True, True),
    ]
    report = {}
    for sc_name, prof, sens, hb in scenarios:
        rng_w = random.Random(seed)
        work = [gen_day(rng_w, prof, sens, hb, hour_w, sensor_days) for _ in range(days)]
        work = [(ev, expand_calls(ev)) for ev in work]
        n_calls = st.mean(len(c) for _, c in work)
        n_ev = st.mean(len(e) for e, _ in work)
        print(f'\n=== {sc_name}: событий/сут {n_ev:.1f}, вызовов LLM/сут {n_calls:.1f} ({days} сут.)')
        print(f"{'модель':<13}{'схема':<8}{'₽/сут':>9}{'кэш %':>7}{'ошибка':>9}{'стенд, мин':>11}")
        report[sc_name] = {'events_per_day': n_ev, 'calls_per_day': n_calls, 'models': {}}
        for model in PRICES:
            pi, _, _, po, _ = PRICES[model]
            nocache = st.mean(sum(c[3] * pi + c[4] * po for c in calls) for _, calls in work)
            print(f"{model:<13}{'нет кэша':<8}{nocache:>9.2f}{0.0:>7.1f}{'—':>9}{'—':>11}")
            res = {}
            for si, sch in enumerate(SCHEMES):
                rng = random.Random(seed * 1000 + si)
                rows = [run_scheme(sch, ev, calls, model, rng) if calls else (0.0, 0.0, 0.0) for ev, calls in work]
                res[sch] = tuple(st.mean(r[j] for r in rows) for j in range(3))
            base = res['real'][0]
            for sch in SCHEMES:
                c, h, w = res[sch]
                err = 100 * (c - base) / base if base else 0.0
                print(f"{model:<13}{sch:<8}{c:>9.2f}{100*h:>7.1f}{err:>+8.1f}%{w:>11.0f}")
            report[sc_name]['models'][model] = {k: {'rub_per_day': v[0], 'hit_share': v[1], 'wall_min': v[2]}
                                                for k, v in res.items()}
        if hb:
            for k in (10, 60):
                period = HB_PERIOD if hb is True else hb
                print(f"  heartbeat на таймере настенных часов при x{k}: {int((DAY1 - DAY0) / k // period)} тиков "
                      f"вместо {len([1 for e in work[0][0] if e[1] == 'hb'])}")
    json.dump(report, open(os.path.join(HERE, 'compression_bias_output.json'), 'w'), ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
