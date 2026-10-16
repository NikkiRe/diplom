# Оценка бюджета экспериментов по ценам RouterAI на 06.10.2026 (руб. за 1M токенов, /api/v1/models)
P = {  # in, cache_read, out
 'gpt-4o-mini': (16.56, 8.28, 66.25),
 'gpt-4.1-nano': (11.04, 2.76, 44.16),
 'gpt-4.1-mini': (44.16, 11.04, 176.66),
 'gpt-5-nano': (5.52, 0.55, 44.16),
 'gpt-5-mini': (27.60, 2.76, 220.82),
 'gpt-5.4-mini': (82.81, 8.28, 496.85),
}
REASON = {'gpt-5-nano':300,'gpt-5-mini':300,'gpt-5.4-mini':300}  # скрытые reasoning-токены на вызов (допущение, effort=low)
def cost(model, calls, tin, tout=150, cached=0.0):
    pi, pc, po = P[model]
    out = tout + REASON.get(model,0)
    return calls*(tin*((1-cached)*pi + cached*pc) + out*po)/1e6
tot = {}
def add(name, v): tot[name]=tot.get(name,0)+v
# E0a калибровка TTL (GAP1: 3–27 руб. на пару модель×провайдер; 4 модели × 2 провайдера)
add('E0a TTL', 8*27)
# E0b BFCL-подмножество: 600 задач × 1.3 вызова × 1200 вх / 80 вых, 6 моделей, +повтор A/A на 2 моделях
for m in P: add('E0b BFCL', cost(m, 600*1.3, 1200, 80))
for m in ['gpt-4o-mini','gpt-5-mini']: add('E0b BFCL', cost(m, 600*1.3, 1200, 80))
# E1 простой/фон: 3 агента × 5 прогонов × 8 ч, heartbeat 30 мин = 16 тиков, 6k (своё) / 15k (PicoClaw, nanobot) вх., gpt-4o-mini
add('E1 idle', 5*(cost('gpt-4o-mini',16,6000,30) + 2*cost('gpt-4o-mini',16,15000,30)))
# E2 представление контекста оборудования: 6 предст. × 3 k × 3 модели × 120 задач × 3 вызова; вх. 2.5k/4k/10k; без кэша (худший случай)
for m in ['gpt-4o-mini','gpt-5-mini','gpt-4.1-nano']:
    for tin in (2500,4000,10000):
        add('E2 hw-context', cost(m, 6*120*3, tin))
# E3 политики контекста пользователя, T1 (сжатый живой день): 6 политик × 2 модели × 10 суток × 155 вызовов × 5k, кэш 50%
for m in ['gpt-4o-mini','gpt-5-mini']: add('E3 user-context', cost(m, 6*10*155, 5000, cached=0.5))
# E4 когда будить LLM, T1: 4 политики × 2 модели × 10 суток × (32 тика hb × 6k + 120 вызовов × 5k)
for m in ['gpt-4o-mini','gpt-5-mini']: add('E4 wake-policy', 4*10*(cost(m,32,6000,30)+cost(m,120,5000,cached=0.5)))
# E5 рабочий день T2: свой агент, PicoClaw, nanobot (+OpenClaw 1 прогон) × 5 прогонов × 8 ч, 110 вызовов; 5k/12k/12k/28k вх.
for m in ['gpt-4o-mini','gpt-5-mini']:
    add('E5 day T2', 5*(cost(m,110,5000,cached=0.3)+2*cost(m,110+16,12000,cached=0.3)) + cost(m,110+16,28000,cached=0.3))
# E6 камера: 3 режима × 150 запросов × 2 модели; кадр ~3000 вх. токенов (допущение), метки ~300, ссылка по запросу ~1500 в среднем
for m in ['gpt-4o-mini','gpt-5-mini']:
    add('E6 camera', cost(m,150*2,3000+2500)+cost(m,150*2,300+2500)+cost(m,150*2,1500+2500))
s = sum(tot.values())
for k,v in tot.items(): print(f'{k:18s} {v:8.0f} руб.')
print(f'{"итого":18s} {s:8.0f} руб.;  с резервом 50% на отладку и повторы: {s*1.5:.0f} руб.')
