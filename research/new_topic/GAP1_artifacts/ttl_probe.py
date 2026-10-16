"""Калибровка времени жизни префиксного кэша провайдера: кривая p_hit(g) — вероятность попадания
при паузе g с момента последнего использования префикса (GAP1 §6.3).

Зачем: документация OpenAI/Azure даёт только «around 5 to 10 minutes of inactivity, up to one hour»
(in_memory) и «at least 30 minutes» (GPT-5.6+). Протокол TTL-aware сжатия времени (compression_bias.py)
требует числовой кривой p_hit(g) для зоны неопределённости. Кривая снимается в РЕАЛЬНОМ времени.

Схема опыта. Пара запросов A, B с одинаковым уникальным префиксом (nonce + ~1400 токенов текста):
A в момент t0 (запись), B в момент t0 + g (проверка), max_tokens = 1. Попадание: cached_tokens > 0 у B
(если агрегатор не возвращает cached_tokens — по стоимости B через GET /api/v1/generation?id=...).
Каждая пара — свой nonce и свой prompt_cache_key, поэтому пары не мешают друг другу; пары разных g
запускаются параллельно со сдвигом старта. Провайдер внутри агрегатора фиксируется (provider.only),
потому что кэши OpenAI и Azure раздельные. Оценка p_hit(g): изотоническая (невозрастающая) регрессия
PAV по индикаторам попадания + интервалы Уилсона по каждому g.

Режимы:
  python3 ttl_probe.py plan  [модель ...]   — расписание, длительность и стоимость (по снимку цен RouterAI)
  python3 ttl_probe.py selftest             — проверка оценивателя на синтетике (TTL ~ U(300, 600) с)
  python3 ttl_probe.py run МОДЕЛЬ ПРОВАЙДЕР — живой прогон (нужен ROUTERAI_API_KEY; НЕ ЗАПУСКАЛСЯ, ключа нет)
  python3 ttl_probe.py fit ФАЙЛ.jsonl       — оценка кривой по журналу живого прогона
"""
import json, math, os, random, sys, threading, time, urllib.request, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = 'https://routerai.ru/api/v1'
PROMPT_TOKENS = 1400            # оценка длины префикса (>= 1024 — минимум кэшируемой длины)
GRID = {                         # паузы, мин
    'in_memory': [0.5, 2, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 30, 45, 60],
    '30m': [10, 20, 25, 29, 30, 31, 35, 45, 60, 90],
}
CLASS = {'openai/gpt-4o-mini': 'in_memory', 'openai/gpt-4.1-mini': 'in_memory', 'openai/gpt-5-mini': 'in_memory',
         'openai/gpt-5.4-mini': 'in_memory', 'openai/gpt-5.4-nano': 'in_memory', 'openai/gpt-5-nano': 'in_memory',
         'openai/gpt-6-luna': '30m', 'openai/gpt-5.6-luna': '30m'}
REPS = 10
STAGGER_S = 20.0


def filler(nonce):
    words = ('sensor actuator camera relay temperature humidity motion door light schedule user preference '
             'context device state event agent tool reading threshold alert battery network').split()
    rnd = random.Random(nonce)
    body = ' '.join(rnd.choice(words) for _ in range(int(PROMPT_TOKENS * 0.95)))
    return f'[{nonce}] ' + body


def schedule(model):
    cls = CLASS[model]
    pairs = [(g, r) for g in GRID[cls] for r in range(REPS)]
    random.Random(42).shuffle(pairs)
    return [{'gap_min': g, 'rep': r, 'start_s': i * STAGGER_S} for i, (g, r) in enumerate(pairs)]


def prices():
    snap = os.path.join(HERE, '..', 'F_artifacts', 'routerai_models_2026-10-06.json')
    d = json.load(open(snap))
    return {m['id']: m['pricing'] for m in d['data']}


def plan(models):
    pr = prices()
    for m in models:
        sch = schedule(m)
        p = pr[m]
        pin, pcr = float(p['prompt']), float(p.get('input_cache_read') or p['prompt'])
        pcw = float(p['input_cache_write']) if p.get('input_cache_write') else None
        a = PROMPT_TOKENS * (pcw or pin)                 # запрос A: запись (или обычный вход)
        b_hit, b_miss = PROMPT_TOKENS * pcr, PROMPT_TOKENS * (pcw or pin)
        cost = len(sch) * (a + 0.5 * (b_hit + b_miss))   # грубо: половина проверок — попадания
        dur = max(s['start_s'] + s['gap_min'] * 60 for s in sch) / 60
        print(f'{m:<22} класс {CLASS[m]:<9} пар {len(sch):>4}  длительность ≈ {dur:5.0f} мин  стоимость ≈ {cost:6.2f} ₽ '
              f'на одного провайдера')


def pav_decreasing(xs, ys, ws=None):
    """Изотоническая невозрастающая регрессия (pool adjacent violators). xs отсортированы по возрастанию."""
    ws = ws or [1.0] * len(ys)
    blocks = [[y, w, [x]] for x, y, w in zip(xs, ys, ws)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] < blocks[i + 1][0]:              # нарушение невозрастания
            y = (blocks[i][0] * blocks[i][1] + blocks[i + 1][0] * blocks[i + 1][1]) / (blocks[i][1] + blocks[i + 1][1])
            blocks[i] = [y, blocks[i][1] + blocks[i + 1][1], blocks[i][2] + blocks[i + 1][2]]
            del blocks[i + 1]
            i = max(i - 1, 0)
        else:
            i += 1
    return {x: b[0] for b in blocks for x in b[2]}


def wilson(k, n, z=1.96):
    if n == 0: return (0.0, 1.0)
    p = k / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d; h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def fit(records):
    by = {}
    for r in records:
        by.setdefault(r['gap_min'], []).append(1 if r['hit'] else 0)
    xs = sorted(by)
    rates = [sum(by[x]) / len(by[x]) for x in xs]
    iso = pav_decreasing(xs, rates, [len(by[x]) for x in xs])
    out = []
    for x in xs:
        lo, hi = wilson(sum(by[x]), len(by[x]))
        out.append((x, len(by[x]), sum(by[x]) / len(by[x]), iso[x], lo, hi))
    return out


def selftest():
    rng = random.Random(7)
    recs = []
    for s in schedule('openai/gpt-4o-mini'):
        ttl = rng.uniform(300, 600)
        recs.append({'gap_min': s['gap_min'], 'hit': s['gap_min'] * 60 < ttl})
    print(f"{'g, мин':>7}{'n':>4}{'доля':>7}{'PAV':>7}{'Уилсон 95%':>16}{'истина':>8}")
    worst = 0.0
    for x, n, p, iso, lo, hi in fit(recs):
        true = min(1.0, max(0.0, (600 - x * 60) / 300))
        worst = max(worst, abs(iso - true))
        print(f'{x:>7}{n:>4}{p:>7.2f}{iso:>7.2f}    [{lo:.2f}; {hi:.2f}]{true:>8.2f}')
    print(f'макс. |PAV − истина| = {worst:.2f} при {REPS} повторах на точку')


def run(model, provider):                       # НЕ ЗАПУСКАЛСЯ: нет ключа RouterAI на 06.10.2026
    key = os.environ['ROUTERAI_API_KEY']
    log = open(os.path.join(HERE, f'ttl_probe_{model.replace("/", "_")}_{provider}_{int(time.time())}.jsonl'), 'a')
    lock = threading.Lock()

    def call(prompt, cache_key):
        body = {'model': model, 'max_tokens': 1, 'messages': [{'role': 'system', 'content': prompt},
                                                               {'role': 'user', 'content': 'ok'}],
                'prompt_cache_key': cache_key, 'provider': {'only': [provider], 'allow_fallbacks': False}}
        req = urllib.request.Request(BASE + '/chat/completions', data=json.dumps(body).encode(),
                                     headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        t = time.time()
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.load(r); gen = r.headers.get('X-Generation-Id')
        return resp, gen, time.time() - t

    def pair(s):
        nonce = uuid.uuid4().hex
        prompt, ck = filler(nonce), 'ttlprobe-' + nonce
        time.sleep(s['start_s'])
        ra, ga, la = call(prompt, ck)
        time.sleep(s['gap_min'] * 60)
        rb, gb, lb = call(prompt, ck)
        u = rb.get('usage', {})
        cached = (u.get('prompt_tokens_details') or {}).get('cached_tokens')
        rec = {'model': model, 'provider': provider, **s, 'usage_a': ra.get('usage'), 'usage_b': u,
               'gen_a': ga, 'gen_b': gb, 'lat_a': la, 'lat_b': lb, 'cached_b': cached,
               'hit': (cached or 0) > 0, 'ts': time.time()}
        with lock:
            log.write(json.dumps(rec) + '\n'); log.flush()

    threads = [threading.Thread(target=pair, args=(s,)) for s in schedule(model)]
    for th in threads: th.start()
    for th in threads: th.join()


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'plan'
    if cmd == 'plan':
        plan(sys.argv[2:] or ['openai/gpt-4o-mini', 'openai/gpt-5-mini', 'openai/gpt-5.4-mini', 'openai/gpt-6-luna'])
    elif cmd == 'selftest':
        selftest()
    elif cmd == 'run':
        run(sys.argv[2], sys.argv[3])
    elif cmd == 'fit':
        recs = [json.loads(l) for l in open(sys.argv[2])]
        for row in fit(recs):
            print('%6.1f мин  n=%d  доля=%.2f  PAV=%.2f  [%.2f; %.2f]' % row)
